
from rest_framework import serializers
from django.utils import timezone

from .models import (
    FlightRoute,
    FlightInstance,
    FlightSeat,
    FlightBooking,
    FlightPayment,
)



class FlightSearchSerializer(serializers.Serializer):
    origin_iata = serializers.CharField(max_length=5)
    destination_iata = serializers.CharField(max_length=5)
    flight_date = serializers.DateField()

    cabin_class = serializers.CharField(
        max_length=20,
        required=False,
        default="ECONOMY"
    )

    airline = serializers.CharField(
        max_length=60,
        required=False,
        allow_blank=True
    )

    departure_time = serializers.TimeField(
        required=False
    )

    def validate(self, attrs):
        origin = attrs["origin_iata"].upper().strip()
        destination = attrs["destination_iata"].upper().strip()

        if origin == destination:
            raise serializers.ValidationError(
                "Origin and destination cannot be the same."
            )

        if attrs["flight_date"] < timezone.localdate():
            raise serializers.ValidationError(
                "Flight date cannot be in the past."
            )

        attrs["origin_iata"] = origin
        attrs["destination_iata"] = destination
        attrs["cabin_class"] = attrs["cabin_class"].upper().strip()

        if attrs.get("airline"):
            attrs["airline"] = attrs["airline"].strip()

        return attrs



class FlightSeatSelectionSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    seat_numbers = serializers.ListField(
        child=serializers.CharField(max_length=6),
        allow_empty=False
    )

    def validate_seat_numbers(self, value):
        value = [seat.strip().upper() for seat in value]

        if len(value) != len(set(value)):
            raise serializers.ValidationError(
                "Duplicate seat numbers are not allowed."
            )

        return value


class FlightPassengerSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    age = serializers.IntegerField(min_value=0, max_value=120)
    gender = serializers.ChoiceField(
        choices=["M", "F", "OTHER"]
    )
    seat_number = serializers.CharField(max_length=6)

    def validate(self, attrs):
        attrs["name"] = attrs["name"].strip()
        attrs["seat_number"] = attrs["seat_number"].upper().strip()

        if not attrs["name"]:
            raise serializers.ValidationError(
                "Passenger name cannot be empty."
            )

        return attrs


class FlightPaymentCreateSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    instance_id = serializers.IntegerField()
    seat_numbers = serializers.ListField(
        child=serializers.CharField(max_length=6),
        allow_empty=False
    )
    passengers = FlightPassengerSerializer(many=True)
    idempotency_key = serializers.CharField(max_length=100)

    def validate(self, attrs):
        seats = [s.strip().upper() for s in attrs["seat_numbers"]]
        attrs["seat_numbers"] = seats

        if len(seats) != len(set(seats)):
            raise serializers.ValidationError(
                "Duplicate seat numbers are not allowed."
            )

        passengers = attrs["passengers"]

        if len(passengers) != len(seats):
            raise serializers.ValidationError(
                "Passenger count must match the number of selected seats."
            )

        passenger_seats = [
            passenger["seat_number"] for passenger in passengers
        ]

        if len(passenger_seats) != len(set(passenger_seats)):
            raise serializers.ValidationError(
                "Each passenger must have a different seat."
            )

        if set(passenger_seats) != set(seats):
            raise serializers.ValidationError(
                "Every selected seat must be assigned to exactly one passenger."
            )

        return attrs


class FlightPaymentVerifySerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    razorpay_order_id = serializers.CharField(max_length=100)
    razorpay_payment_id = serializers.CharField(
        max_length=100,
        required=False
    )
    razorpay_signature = serializers.CharField(
        max_length=255,
        required=False
    )
    error_code = serializers.CharField(
        max_length=100,
        required=False
    )
    error_description = serializers.CharField(
        max_length=255,
        required=False
    )


class FlightBookingCancelSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)


class FlightBookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = FlightBooking
        fields = [
            "pnr",
            "instance",
            "passengers",
            "seat_numbers",
            "total_fare",
            "status",
            "created_at",
        ]
        read_only_fields = fields


class FlightRouteSerializer(serializers.ModelSerializer):
    class Meta:
        model = FlightRoute
        fields = [
            "id",
            "flight_number",
            "airline",
            "origin_iata",
            "destination_iata",
            "departure_time",
            "arrival_time",
        ]


class FlightInstanceSerializer(serializers.ModelSerializer):
    route = FlightRouteSerializer(read_only=True)

    class Meta:
        model = FlightInstance
        fields = [
            "id",
            "route",
            "flight_date",
            "cabin_class",
            "base_fare",
            "seats_remaining",
        ]


class FlightSeatSerializer(serializers.ModelSerializer):
    total_fare = serializers.SerializerMethodField()

    class Meta:
        model = FlightSeat
        fields = [
            "seat_number",
            "seat_type",
            "extra_fee",
            "total_fare",
            "status",
        ]

    def get_total_fare(self, obj):
        return obj.instance.base_fare + obj.extra_fee
