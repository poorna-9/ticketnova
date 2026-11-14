
from django.contrib import admin
from .models import (
    BusService,
    BusTrip,
    BusSeat,
    BusBooking,
    BusPayment,
)


@admin.register(BusService)
class BusServiceAdmin(admin.ModelAdmin):
    list_display = (
    "id",
    "operator_name",
    "bus_type",
    "source_city",
    "destination_city",
    "departure_time",
    "arrival_time",
    "image",
    )
    search_fields = ("operator_name", "source_city", "destination_city")


@admin.register(BusTrip)
class BusTripAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "service",
        "travel_date",
        "base_fare",
    )
    list_filter = ("travel_date",)
    search_fields = (
        "service__operator_name",
        "service__source_city",
        "service__destination_city",
    )


@admin.register(BusSeat)
class BusSeatAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "trip",
        "seat_code",
        "deck",
        "is_sleeper",
        "fare",
        "status",
    )
    list_filter = ("status", "deck", "is_sleeper")
    search_fields = ("seat_code",)


@admin.register(BusBooking)
class BusBookingAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_id",
        "trip",
        "session_id",
        "total_amount",
        "status",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("ticket_id", "session_id")


@admin.register(BusPayment)
class BusPaymentAdmin(admin.ModelAdmin):
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
