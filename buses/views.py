
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import BusService, BusTrip, BusSeat, BusBooking
from django.db import models

from .serializer import (
    BusServiceSerializer,
    BusTripSerializer,
    BusSeatSelectionSerializer,
    BusBookingCreateSerializer,
    bus_booking_payload,
)

LOCK_TTL = timedelta(minutes=5)


# ---------- helpers ----------

def ok(data, code=http.HTTP_200_OK, **extra):
    return Response(
        {"success": True, "data": data, **extra},
        status=code
    )


def fail(code, message, http_status=http.HTTP_400_BAD_REQUEST, details=None):
    body = {
        "success": False,
        "error": {
            "code": code,
            "message": message
        }
    }

    if details is not None:
        body["error"]["details"] = details

    return Response(body, status=http_status)


def seat_is_free(seat, session_id, now):
    if seat.status == "AVAILABLE":
        return True

    if seat.status == "LOCKED":
        return (
            seat.locked_until is None
            or seat.locked_until <= now
            or seat.locked_by_session == session_id
        )

    return False


# ---------- browse ----------

@api_view(["GET"])
def bus_service_list(request):
    """
    GET /api/buses/?source=Hyderabad&destination=Bangalore&date=2026-11-14
    """

    qs = BusService.objects.all()
    p = request.query_params

    if p.get("source"):
        qs = qs.filter(source_city__iexact=p["source"])

    if p.get("destination"):
        qs = qs.filter(destination_city__iexact=p["destination"])

    if p.get("date"):
        qs = qs.filter(
            trips__travel_date=p["date"],
            trips__travel_date__gte=timezone.localdate()
        ).distinct()

    data = BusServiceSerializer(
        qs.order_by("departure_time"),
        many=True
    ).data

    return ok(data, count=len(data))


@api_view(["GET"])
def bus_trips(request, service_id):
    """
    GET /api/buses/<service_id>/trips/?date=2026-11-14
    """

    service = get_object_or_404(BusService, pk=service_id)

    date = (
        request.query_params.get("date")
        or timezone.localdate().isoformat()
    )

    trips = (
        BusTrip.objects.filter(
            service=service,
            travel_date=date
        )
        .annotate(
            available_seats=Count(
                "seats",
                filter=Q(seats__status="AVAILABLE")
            )
        )
        .order_by("travel_date")
    )

    data = BusTripSerializer(trips, many=True).data

    return ok({
        "bus": BusServiceSerializer(service).data,
        "date": date,
        "trips": data
    })


# ---------- seat map ----------

@api_view(["GET"])
def bus_seat_map(request, trip_id):
    """
    GET /api/trips/<trip_id>/seats/?session_id=abc
    """

    trip = get_object_or_404(
        BusTrip.objects.select_related("service"),
        pk=trip_id
    )

    session_id = request.query_params.get("session_id")
    now = timezone.now()

    decks = defaultdict(list)

    summary = {
        "AVAILABLE": 0,
        "LOCKED": 0,
        "BOOKED": 0
    }

    seats = BusSeat.objects.filter(
        trip=trip
    ).order_by("deck", "seat_code")

    for seat in seats:

        effective = seat.status

        if (
            seat.status == "LOCKED"
            and seat.locked_until
            and seat.locked_until <= now
        ):
            effective = "AVAILABLE"

        summary[effective] += 1

        decks[seat.deck].append({
            "seat_code": seat.seat_code,
            "deck": seat.deck,
            "is_sleeper": seat.is_sleeper,
            "fare": str(seat.fare),
            "status": effective,
            "locked_by_me": bool(
                session_id
                and effective == "LOCKED"
                and seat.locked_by_session == session_id
            )
        })

    service = trip.service

    return ok({
        "trip": {
            "id": trip.id,
            "operator_name": service.operator_name,
            "bus_type": service.bus_type,
            "source_city": service.source_city,
            "destination_city": service.destination_city,
            "travel_date": trip.travel_date.isoformat(),
            "departure_time": service.departure_time.strftime("%H:%M"),
            "arrival_time": service.arrival_time.strftime("%H:%M"),
            "base_fare": str(trip.base_fare),
            "boarding_points": trip.boarding_points,
            "dropping_points": trip.dropping_points,
        },
        "summary": {
            **summary,
            "total": sum(summary.values())
        },
        "decks": [
            {
                "deck": deck,
                "seats": seats
            }
            for deck, seats in sorted(decks.items())
        ]
    })



# ---------- lock / unlock ----------

@api_view(["POST"])
def lock_seats(request, trip_id):
    """
    POST /api/trips/<trip_id>/seats/lock/
    """

    ser = BusSeatSelectionSerializer(data=request.data)

    if not ser.is_valid():
        return fail(
            "VALIDATION_ERROR",
            "Invalid input.",
            details=ser.errors
        )

    session_id = ser.validated_data["session_id"]
    codes = ser.validated_data["seat_codes"]

    trip = get_object_or_404(BusTrip, pk=trip_id)

    if trip.travel_date < timezone.localdate():
        return fail("TRIP_EXPIRED", "This trip is in the past.")

    now = timezone.now()
    expires_at = now + LOCK_TTL

    with transaction.atomic():

        seats = list(
            BusSeat.objects.select_for_update()
            .filter(
                trip=trip,
                seat_code__in=codes
            )
            .order_by("id")
        )

        missing = sorted(
            set(codes) - {s.seat_code for s in seats}
        )

        if missing:
            return fail(
                "SEAT_NOT_FOUND",
                "Some seats do not exist.",
                http.HTTP_404_NOT_FOUND,
                {"seat_codes": missing}
            )

        unavailable = sorted(
            s.seat_code
            for s in seats
            if not seat_is_free(s, session_id, now)
        )

        if unavailable:
            return fail(
                "SEATS_UNAVAILABLE",
                "Some seats are no longer available.",
                http.HTTP_409_CONFLICT,
                {"seat_codes": unavailable}
            )

        for seat in seats:
            seat.status = "LOCKED"
            seat.locked_by_session = session_id
            seat.locked_until = expires_at
            seat.version += 1

        BusSeat.objects.bulk_update(
            seats,
            [
                "status",
                "locked_by_session",
                "locked_until",
                "version"
            ]
        )

    return ok({
        "trip_id": trip.id,
        "session_id": session_id,
        "locked_seats": [
            {
                "seat_code": s.seat_code,
                "fare": str(s.fare),
                "deck": s.deck,
                "is_sleeper": s.is_sleeper
            }
            for s in sorted(seats, key=lambda x: x.seat_code)
        ],
        "total_amount": str(sum(s.fare for s in seats)),
        "locked_until": expires_at.isoformat(),
        "lock_seconds": int(LOCK_TTL.total_seconds())
    })


@api_view(["POST"])
def unlock_seats(request, trip_id):
    """
    POST /api/trips/<trip_id>/seats/unlock/
    """

    ser = BusSeatSelectionSerializer(data=request.data)

    if not ser.is_valid():
        return fail(
            "VALIDATION_ERROR",
            "Invalid input.",
            details=ser.errors
        )

    session_id = ser.validated_data["session_id"]
    codes = ser.validated_data["seat_codes"]

    with transaction.atomic():

        qs = BusSeat.objects.select_for_update().filter(
            trip_id=trip_id,
            seat_code__in=codes,
            status="LOCKED",
            locked_by_session=session_id
        )

        released = list(
            qs.values_list("seat_code", flat=True)
        )

        qs.update(
            status="AVAILABLE",
            locked_by_session=None,
            locked_until=None,
            version=models.F("version") + 1
        )

    return ok({
        "released_seats": sorted(released)
    })



# ---------- create booking ----------


@api_view(["GET"])
def booking_list(request):
    """
    GET /api/bookings/?session_id=abc
    """

    session_id = request.query_params.get("session_id")

    if not session_id:
        return fail(
            "SESSION_REQUIRED",
            "Query param 'session_id' is required."
        )

    qs = (
        BusBooking.objects
        .select_related("trip__service")
        .filter(session_id=session_id)
        .order_by("-created_at")
    )

    data = [
        bus_booking_payload(booking)
        for booking in qs
    ]

    return ok(data, count=len(data))


@api_view(["GET"])
def booking_detail(request, ticket_id):
    """
    GET /api/bookings/<ticket_id>/
    """

    booking = get_object_or_404(
        BusBooking.objects.select_related("trip__service"),
        ticket_id=ticket_id.upper()
    )

    return ok(bus_booking_payload(booking))

