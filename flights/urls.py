
from django.urls import path
from . import views, payments

urlpatterns = [
    # Flight APIs
    path("flights/search/", views.flight_search),
    path("flights/<int:instance_id>/", views.flight_details),
    path("flights/<int:instance_id>/seats/", views.flight_seat_map),
    path("flights/<int:instance_id>/seats/lock/", views.lock_seats),
    path("flights/<int:instance_id>/seats/unlock/", views.unlock_seats),

    # Payment APIs
    path("payments/create-order/", payments.create_payment_order),
    path("payments/verify/", payments.verify_payment),
    path("payments/webhook/", payments.razorpay_webhook),
    path(
        "payments/<str:razorpay_order_id>/status/",
        payments.payment_status
    ),

    # Booking APIs
    path("bookings/", views.booking_list),
    path("bookings/<str:pnr>/", views.booking_detail),
    path("bookings/<str:pnr>/cancel/", payments.cancel_booking),
]
