
from rest_framework import serializers
from .models import BusService, BusTrip, BusSeat, BusBooking


# ---------- output serializers ----------

class BusServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusService
        fields = [
            "id",
            "operator_name",
            "bus_type",
            "source_city",
            "destination_city",
            "departure_time",
            "arrival_time",
        ]


class BusTripSerializer(serializers.ModelSerializer):
    service = BusServiceSerializer(read_only=True)
    available_seats = serializers.IntegerField(read_only=True)

    class Meta:
        model = BusTrip
        fields = [
            "id",
            "service",
            "travel_date",
            "base_fare",
            "boarding_points",
            "dropping_points",
            "available_seats",
        ]


# ---------- input serializers ----------

class BusSeatSelectionSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    seat_codes = serializers.ListField(
        child=serializers.CharField(max_length=8),
        min_length=1,
        max_length=10
    )

    def validate_seat_codes(self, value):
        cleaned = [v.strip().upper() for v in value]

        if len(set(cleaned)) != len(cleaned):
            raise serializers.ValidationError("Duplicate seat codes.")

        return cleaned


class BusBookingCreateSerializer(BusSeatSelectionSerializer):
    idempotency_key = serializers.CharField(max_length=100)
    trip_id = serializers.IntegerField()
    boarding_point = serializers.CharField(max_length=100)


class BusBookingCancelSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)


# ---------- output helper ----------

def bus_booking_payload(booking: BusBooking) -> dict:
    trip = booking.trip
    service = trip.service

    return {
        "ticket_id": booking.ticket_id,
        "status": booking.status,
        "bus": {
            "operator_name": service.operator_name,
            "bus_type": service.bus_type,
            "source_city": service.source_city,
            "destination_city": service.destination_city,
            "departure_time": service.departure_time.strftime("%H:%M"),
            "arrival_time": service.arrival_time.strftime("%H:%M"),
        },
        "trip": {
            "id": trip.id,
            "travel_date": trip.travel_date.isoformat(),
            "boarding_points": trip.boarding_points,
            "dropping_points": trip.dropping_points,
        },
        "seat_codes": booking.seat_codes,
        "boarding_point": booking.boarding_point,
        "total_amount": str(booking.total_amount),
        "created_at": booking.created_at.isoformat(),
    }



class BusBookingCreateSerializer(BusSeatSelectionSerializer):
    idempotency_key = serializers.CharField(max_length=100)
    trip_id = serializers.IntegerField()
    boarding_point = serializers.CharField(max_length=100)


class BusPaymentVerifySerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)

    razorpay_order_id = serializers.CharField(max_length=100)

    razorpay_payment_id = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True
    )

    razorpay_signature = serializers.CharField(
        max_length=300,
        required=False,
        allow_blank=True
    )

    error = serializers.DictField(required=False)
