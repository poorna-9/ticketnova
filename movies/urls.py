from django.urls import path
from . import views, payments

urlpatterns = [
    # browse
    path("movies/", views.movie_list),
    path("movies/<int:movie_id>/shows/", views.movie_shows),

    # seats (DB-level locking)
    path("shows/<int:show_id>/seats/", views.show_seat_map),
    path("shows/<int:show_id>/seats/lock/", views.lock_seats),
    path("shows/<int:show_id>/seats/unlock/", views.unlock_seats),

    # payments (Razorpay)
    path("payments/create-order/", payments.create_payment_order),
    path("payments/verify/", payments.verify_payment),
    path("payments/webhook/", payments.razorpay_webhook),
    path("payments/<str:razorpay_order_id>/status/", payments.payment_status),

    # bookings
    path("bookings/", views.booking_list),
    path("bookings/<str:booking_code>/", views.booking_detail),
    path("bookings/<str:booking_code>/cancel/", payments.cancel_booking),
]