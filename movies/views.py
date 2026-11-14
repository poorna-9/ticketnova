from collections import OrderedDict, defaultdict
from datetime import timedelta
import re

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Movie, MovieBooking, MovieShow, ShowSeat
from .serializer import (
    MovieSerializer,
    SeatSelectionSerializer,
    ShowSerializer,
    booking_payload,
)

LOCK_TTL = timedelta(minutes=5)


# ---------- helpers ----------

def ok(data, code=http.HTTP_200_OK, **extra):
    return Response({"success": True, "data": data, **extra}, status=code)


def fail(code, message, http_status=http.HTTP_400_BAD_REQUEST, details=None):
    body = {"success": False, "error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return Response(body, status=http_status)


def seat_is_free(seat, session_id, now):
    """Free if available, or locked but expired / locked by the same session."""
    if seat.status == "AVAILABLE":
        return True
    if seat.status == "LOCKED":
        return (
            seat.locked_until is None
            or seat.locked_until <= now
            or seat.locked_by_session == session_id
        )
    return False


def row_of(label):
    m = re.match(r"[A-Za-z]+", label)
    return m.group(0).upper() if m else label


# ---------- browse ----------

@api_view(["GET"])
def movie_list(request):
    """GET /api/movies/?city=&language=&genre=&q="""
    qs = Movie.objects.all()
    p = request.query_params
    if p.get("language"):
        qs = qs.filter(language__iexact=p["language"])
    if p.get("genre"):
        qs = qs.filter(genre__iexact=p["genre"])
    if p.get("q"):
        qs = qs.filter(title__icontains=p["q"])
    if p.get("city"):
        qs = qs.filter(
            shows__screen__city__iexact=p["city"],
            shows__show_date__gte=timezone.localdate(),
        ).distinct()
    data = MovieSerializer(qs.order_by("title"), many=True).data
    return ok(data, count=len(data))


@api_view(["GET"])
def movie_shows(request, movie_id):
    """GET /api/movies/<movie_id>/shows/?city=Mumbai&date=2026-11-14"""
    movie = get_object_or_404(Movie, pk=movie_id)
    city = request.query_params.get("city")
    date = request.query_params.get("date") or timezone.localdate().isoformat()
    if not city:
        return fail("CITY_REQUIRED", "Query param 'city' is required.")
    now = timezone.now()
    shows = (
        MovieShow.objects.filter(movie=movie, screen__city__iexact=city, show_date=date)
        .select_related("screen")
        .annotate(
            available_seats=Count(
                "seats",
                filter=(
                    Q(seats__status="AVAILABLE")
                    |
                    Q(
                        seats__status="LOCKED",
                        seats__locked_until__lte=now
                    )
                    |
                    Q(
                        seats__status="LOCKED",
                        seats__locked_until__isnull=True
                    )
                ),
                distinct=True
            )
        )
        .order_by("screen__theater_name", "start_time")
    )

    grouped = OrderedDict()
    for s in shows:
        key = (s.screen.theater_name, s.screen.screen_name)
        grouped.setdefault(
            key,
            {"theater": s.screen.theater_name, "screen": s.screen.screen_name, "shows": []},
        )["shows"].append(ShowSerializer(s).data)

    return ok(
        {
            "movie": MovieSerializer(movie).data,
            "city": city,
            "date": date,
            "theaters": list(grouped.values()),
        }
    )


@api_view(["GET"])
def show_seat_map(request, show_id):
    """GET /api/shows/<show_id>/seats/?session_id=abc"""
    show = get_object_or_404(MovieShow.objects.select_related("movie", "screen"), pk=show_id)
    session_id = request.query_params.get("session_id")
    now = timezone.now()

    rows = defaultdict(list)
    summary = {"AVAILABLE": 0, "LOCKED": 0, "BOOKED": 0}

    for seat in ShowSeat.objects.filter(show=show).order_by("seat_label"):
        effective = seat.status
        if seat.status == "LOCKED" and seat.locked_until and seat.locked_until <= now:
            effective = "AVAILABLE"  # expired lock counts as free
        summary[effective] += 1
        rows[row_of(seat.seat_label)].append(
            {
                "seat_label": seat.seat_label,
                "tier": seat.tier,
                "price": str(seat.price),
                "status": effective,
                "locked_by_me": bool(
                    session_id and effective == "LOCKED" and seat.locked_by_session == session_id
                ),
            }
        )

    return ok(
        {
            "show": {
                "id": show.id,
                "movie": show.movie.title,
                "theater": show.screen.theater_name,
                "screen": show.screen.screen_name,
                "show_date": show.show_date.isoformat(),
                "start_time": show.start_time.strftime("%H:%M"),
            },
            "summary": {**summary, "total": sum(summary.values())},
            "rows": [{"row": r, "seats": s} for r, s in sorted(rows.items())],
        }
    )


# ---------- lock / unlock ----------

@api_view(["POST"])
def lock_seats(request, show_id):
    """POST /api/shows/<show_id>/seats/lock/"""
    ser = SeatSelectionSerializer(data=request.data)
    if not ser.is_valid():
        return fail("VALIDATION_ERROR", "Invalid input.", details=ser.errors)
    session_id = ser.validated_data["session_id"]
    labels = ser.validated_data["seat_labels"]

    show = get_object_or_404(MovieShow, pk=show_id)
    if show.show_date < timezone.localdate():
        return fail("SHOW_EXPIRED", "This show is in the past.")

    now = timezone.now()
    expires_at = now + LOCK_TTL

    with transaction.atomic():
        # order_by("id") keeps row-lock order stable and avoids deadlocks
        seats = list(
            ShowSeat.objects.select_for_update()
            .filter(show=show, seat_label__in=labels)
            .order_by("id")
        )
        missing = sorted(set(labels) - {s.seat_label for s in seats})
        if missing:
            return fail(
                "SEAT_NOT_FOUND", "Some seats do not exist.",
                http.HTTP_404_NOT_FOUND, {"seat_labels": missing},
            )
        unavailable = sorted(s.seat_label for s in seats if not seat_is_free(s, session_id, now))
        if unavailable:
            return fail(
                "SEATS_UNAVAILABLE", "Some seats are no longer available.",
                http.HTTP_409_CONFLICT, {"seat_labels": unavailable},
            )
        for s in seats:
            s.status = "LOCKED"
            s.locked_by_session = session_id
            s.locked_until = expires_at
            s.version += 1
        ShowSeat.objects.bulk_update(
            seats, ["status", "locked_by_session", "locked_until", "version"]
        )

    return ok(
        {
            "show_id": show.id,
            "session_id": session_id,
            "locked_seats": [
                {"seat_label": s.seat_label, "tier": s.tier, "price": str(s.price)}
                for s in sorted(seats, key=lambda x: x.seat_label)
            ],
            "total_amount": str(sum(s.price for s in seats)),
            "locked_until": expires_at.isoformat(),
            "lock_seconds": int(LOCK_TTL.total_seconds()),
        }
    )


@api_view(["POST"])
def unlock_seats(request, show_id):
    """POST /api/shows/<show_id>/seats/unlock/"""
    ser = SeatSelectionSerializer(data=request.data)
    if not ser.is_valid():
        return fail("VALIDATION_ERROR", "Invalid input.", details=ser.errors)

    with transaction.atomic():
        qs = ShowSeat.objects.select_for_update().filter(
            show_id=show_id,
            seat_label__in=ser.validated_data["seat_labels"],
            status="LOCKED",
            locked_by_session=ser.validated_data["session_id"],
        )
        released = list(qs.values_list("seat_label", flat=True))
        qs.update(status="AVAILABLE", locked_by_session=None, locked_until=None)

    return ok({"released_seats": sorted(released)})


# ---------- bookings ----------

@api_view(["GET"])
def booking_list(request):
    """GET /api/bookings/?session_id=abc"""
    sid = request.query_params.get("session_id")
    if not sid:
        return fail("SESSION_REQUIRED", "Query param 'session_id' is required.")
    qs = (
        MovieBooking.objects.select_related("show__movie", "show__screen", "payment")
        .filter(session_id=sid)
        .order_by("-created_at")
    )
    data = [booking_payload(b) for b in qs]
    return ok(data, count=len(data))


@api_view(["GET"])
def booking_detail(request, booking_code):
    """GET /api/bookings/<booking_code>/"""
    booking = get_object_or_404(
        MovieBooking.objects.select_related("show__movie", "show__screen", "payment"),
        booking_code=booking_code.upper(),
    )
    return ok(booking_payload(booking))