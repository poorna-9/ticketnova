
from django.contrib import admin
from .models import (
    FlightRoute,
    FlightInstance,
    FlightSeat,
    FlightBooking,
    FlightPayment,
)


@admin.register(FlightRoute)
class FlightRouteAdmin(admin.ModelAdmin):
    list_display = (
    "id",
    "flight_number",
    "airline",
    "origin_iata",
    "destination_iata",
    "departure_time",
    "arrival_time",
    "image",
)
    search_fields = (
        "flight_number",
        "airline",
        "origin_iata",
        "destination_iata",
    )


@admin.register(FlightInstance)
class FlightInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "route",
        "flight_date",
        "cabin_class",
        "base_fare",
        "seats_remaining",
    )
    list_filter = ("flight_date", "cabin_class")
    search_fields = (
        "route__flight_number",
        "route__airline",
        "route__origin_iata",
        "route__destination_iata",
    )


@admin.register(FlightSeat)
class FlightSeatAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "instance",
        "seat_number",
        "seat_type",
        "extra_fee",
        "status",
    )
    list_filter = ("status", "seat_type")
    search_fields = ("seat_number",)


@admin.register(FlightBooking)
class FlightBookingAdmin(admin.ModelAdmin):
    list_display = (
        "pnr",
        "instance",
        "total_fare",
        "status",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("pnr", "session_id")


@admin.register(FlightPayment)
class FlightPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "razorpay_order_id",
        "razorpay_payment_id",
        "amount",
        "status",
        "refund_status",
        "created_at",
    )
    list_filter = ("status", "refund_status", "created_at")
    search_fields = (
        "razorpay_order_id",
        "razorpay_payment_id",
        "session_id",
    )
