
from django.contrib import admin
from .models import (
    Movie,
    TheaterScreen,
    MovieShow,
    ShowSeat,
    MovieBooking,
    MoviePayment,
)


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "language",
        "genre",
        "duration_mins",
        "certification",
    )
    list_filter = ("language", "genre", "certification")
    search_fields = ("title",)


@admin.register(TheaterScreen)
class TheaterScreenAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "theater_name",
        "city",
        "screen_name",
        "total_seats",
    )
    list_filter = ("city",)
    search_fields = ("theater_name", "city")


@admin.register(MovieShow)
class MovieShowAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "movie",
        "screen",
        "show_date",
        "start_time",
        "base_price",
    )
    list_filter = ("show_date",)
    search_fields = ("movie__title", "screen__theater_name")


@admin.register(ShowSeat)
class ShowSeatAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "show",
        "seat_label",
        "tier",
        "price",
        "status",
    )
    list_filter = ("tier", "status")
    search_fields = ("seat_label",)


@admin.register(MovieBooking)
class MovieBookingAdmin(admin.ModelAdmin):
    list_display = (
        "booking_code",
        "show",
        "total_amount",
        "status",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("booking_code", "session_id")


@admin.register(MoviePayment)
class MoviePaymentAdmin(admin.ModelAdmin):
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
