
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from datetime import timedelta

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import (
    FlightRoute,
    FlightInstance,
    FlightSeat,
    FlightBooking,
)

from .serializer import (
    FlightSearchSerializer,
    FlightSeatSelectionSerializer,
    FlightInstanceSerializer,
    FlightSeatSerializer,
    FlightBookingSerializer,
)


LOCK_DURATION = 5


@api_view(["GET"])
def flight_search(request):
    serializer = FlightSearchSerializer(data=request.query_params)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    flights = FlightInstance.objects.filter(
        route__origin_iata=data["origin_iata"],
        route__destination_iata=data["destination_iata"],
        flight_date=data["flight_date"],
        cabin_class=data["cabin_class"],
    ).select_related("route").order_by("base_fare")

    return Response(
        FlightInstanceSerializer(flights, many=True).data
    )


@api_view(["GET"])
def flight_details(request, instance_id):
    try:
        flight = FlightInstance.objects.select_related(
            "route"
        ).get(id=instance_id)
    except FlightInstance.DoesNotExist:
        return Response(
            {"error": "Flight not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response(FlightInstanceSerializer(flight).data)


@api_view(["GET"])
def flight_seat_map(request, instance_id):
    try:
        flight = FlightInstance.objects.get(id=instance_id)
    except FlightInstance.DoesNotExist:
        return Response(
            {"error": "Flight not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    now = timezone.now()

    # Expired locks are logically available.
    seats = FlightSeat.objects.filter(
        instance=flight
    ).order_by("seat_number")

    result = []

    for seat in seats:
        seat_data = FlightSeatSerializer(seat).data

        if (
            seat.status == "LOCKED"
            and (
                seat.locked_until is None
                or seat.locked_until <= now
            )
        ):
            seat_data["status"] = "AVAILABLE"
            seat_data["locked_by_session"] = None
            seat_data["locked_until"] = None

        result.append(seat_data)

    return Response({
        "flight_id": flight.id,
        "flight_number": flight.route.flight_number,
        "flight_date": flight.flight_date,
        "seats": result,
    })


@api_view(["POST"])
def lock_seats(request, instance_id):
    serializer = FlightSeatSelectionSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data
    session_id = data["session_id"]
    seat_numbers = data["seat_numbers"]

    now = timezone.now()
    lock_until = now + timedelta(minutes=LOCK_DURATION)

    try:
        with transaction.atomic():

            flight = FlightInstance.objects.select_for_update().get(
                id=instance_id
            )

            seats = list(
                FlightSeat.objects.select_for_update().filter(
                    instance=flight,
                    seat_number__in=seat_numbers,
                ).order_by("id")
            )

            if len(seats) != len(seat_numbers):
                return Response(
                    {"error": "One or more selected seats do not exist."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for seat in seats:

                # Expired locks can be reclaimed.
                if (
                    seat.status == "LOCKED"
                    and seat.locked_until
                    and seat.locked_until <= now
                ):
                    seat.status = "AVAILABLE"
                    seat.locked_by_session = None
                    seat.locked_until = None

                # Booked seats cannot be locked again.
                if seat.status == "BOOKED":
                    return Response(
                        {
                            "error": f"Seat {seat.seat_number} is already booked."
                        },
                        status=status.HTTP_409_CONFLICT
                    )

                # Another session currently owns the lock.
                if (
                    seat.status == "LOCKED"
                    and seat.locked_by_session != session_id
                ):
                    return Response(
                        {
                            "error": f"Seat {seat.seat_number} is locked by another user."
                        },
                        status=status.HTTP_409_CONFLICT
                    )

            # All seats are valid. Lock them together.
            for seat in seats:
                seat.status = "LOCKED"
                seat.locked_by_session = session_id
                seat.locked_until = lock_until
                seat.version = F("version") + 1

                seat.save(update_fields=[
                    "status",
                    "locked_by_session",
                    "locked_until",
                    "version",
                ])

    except FlightInstance.DoesNotExist:
        return Response(
            {"error": "Flight not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response({
        "message": "Seats locked successfully.",
        "flight_id": instance_id,
        "seat_numbers": seat_numbers,
        "session_id": session_id,
        "locked_until": lock_until,
    })


@api_view(["POST"])
def unlock_seats(request, instance_id):
    serializer = FlightSeatSelectionSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    with transaction.atomic():
        seats = FlightSeat.objects.select_for_update().filter(
            instance_id=instance_id,
            seat_number__in=data["seat_numbers"],
            status="LOCKED",
            locked_by_session=data["session_id"],
        )

        count = seats.update(
            status="AVAILABLE",
            locked_by_session=None,
            locked_until=None,
            version=F("version") + 1,
        )

    return Response({
        "message": "Seats released successfully.",
        "released_count": count,
    })


@api_view(["GET"])
def booking_list(request):
    session_id = request.query_params.get("session_id")

    if not session_id:
        return Response(
            {"error": "session_id is required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    bookings = FlightBooking.objects.filter(
        session_id=session_id
    ).select_related(
        "instance",
        "instance__route"
    ).order_by("-created_at")

    return Response(
        FlightBookingSerializer(bookings, many=True).data
    )


@api_view(["GET"])
def booking_detail(request, pnr):
    session_id = request.query_params.get("session_id")

    if not session_id:
        return Response(
            {"error": "session_id is required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        booking = FlightBooking.objects.select_related(
            "instance",
            "instance__route"
        ).get(
            pnr=pnr,
            session_id=session_id
        )
    except FlightBooking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response(FlightBookingSerializer(booking).data)
