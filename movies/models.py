import uuid
from django.db import models

from buses.models import BusBooking


def gen_movie_code():
    return uuid.uuid4().hex[:8].upper()

class Movie(models.Model):
    title = models.CharField(max_length=150, db_index=True)
    language = models.CharField(max_length=30)
    genre = models.CharField(max_length=50)
    duration_mins = models.PositiveIntegerField()
    certification = models.CharField(max_length=10, default="UA")
    poster = models.ImageField(upload_to="movie_posters/",null=True,blank=True)


class TheaterScreen(models.Model):
    theater_name = models.CharField(max_length=120)
    city = models.CharField(max_length=60, db_index=True)
    screen_name = models.CharField(max_length=40)
    total_seats = models.PositiveIntegerField()


class MovieShow(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="shows")
    screen = models.ForeignKey(TheaterScreen, on_delete=models.CASCADE, related_name="shows")
    show_date = models.DateField(db_index=True)
    start_time = models.TimeField()
    base_price = models.DecimalField(max_digits=7, decimal_places=2)


class ShowSeat(models.Model):
    STATUS_CHOICES = [("AVAILABLE", "Available"), ("LOCKED", "Locked"), ("BOOKED", "Booked")]
    TIER_CHOICES = [("CLASSIC", "Classic"), ("PRIME", "Prime"), ("RECLINER", "Recliner")]

    show = models.ForeignKey(MovieShow, on_delete=models.CASCADE, related_name="seats")
    seat_label = models.CharField(max_length=10)
    tier = models.CharField(max_length=15, choices=TIER_CHOICES, default="PRIME")
    price = models.DecimalField(max_digits=7, decimal_places=2)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="AVAILABLE", db_index=True)
    locked_by_session = models.CharField(max_length=100, null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("show", "seat_label")


class MovieBooking(models.Model):
    booking_code = models.CharField(max_length=12, unique=True, default=gen_movie_code)
    idempotency_key = models.CharField(max_length=100, unique=True)
    session_id = models.CharField(max_length=100, db_index=True)
    show = models.ForeignKey(MovieShow, on_delete=models.PROTECT)
    seat_labels = models.JSONField()
    total_amount = models.DecimalField(max_digits=9, decimal_places=2)
    status = models.CharField(max_length=20, default="CONFIRMED")
    created_at = models.DateTimeField(auto_now_add=True)

class MoviePayment(models.Model):
    STATUS_CHOICES = [
        ("CREATED", "Created"),        # order created, waiting for payment
        ("PAID", "Paid"),              # captured + booking confirmed
        ("FAILED", "Failed"),          # gateway order creation failed
        ("EXPIRED", "Expired"),        # hold window ran out, seats released
        ("SEATS_LOST", "Seats lost"),  # paid but seats no longer ours -> auto refund
    ]
 
    idempotency_key = models.CharField(max_length=100, unique=True)
    session_id = models.CharField(max_length=100, db_index=True)
    show = models.ForeignKey(MovieShow, on_delete=models.PROTECT, related_name="payments")
    seat_labels = models.JSONField()
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="CREATED", db_index=True)
 
    razorpay_order_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    razorpay_payment_id = models.CharField(max_length=100, null=True, blank=True)
 
    razorpay_refund_id = models.CharField(max_length=100, null=True, blank=True)
    refund_status = models.CharField(max_length=12, null=True, blank=True)  # INITIATED / FAILED
    refunded_amount = models.DecimalField(max_digits=9, decimal_places=2, default=0)
 
    booking = models.OneToOneField(
        MovieBooking, null=True, blank=True, on_delete=models.SET_NULL, related_name="payment"
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

