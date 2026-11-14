
from django.urls import path
import buses.payments as payments
from . import views

urlpatterns = [

    # Browse
    path("buses/", views.bus_service_list),
    path("buses/<int:service_id>/trips/", views.bus_trips),

    # Seats
    path("trips/<int:trip_id>/seats/", views.bus_seat_map),
    path("trips/<int:trip_id>/seats/lock/", views.lock_seats),
    path("trips/<int:trip_id>/seats/unlock/", views.unlock_seats),

    # Bookings
    path("bookings/", views.booking_list),
    path("bookings/<str:ticket_id>/", views.booking_detail),
    path("bookings/<str:ticket_id>/cancel/",payments.cancel_booking),

    # Payments
    path("payments/create-order/", payments.create_payment_order),
    path("payments/verify/", payments.verify_payment),
    path("payments/webhook/", payments.razorpay_webhook),
    path("payments/<str:razorpay_order_id>/status/", payments.payment_status),
]
