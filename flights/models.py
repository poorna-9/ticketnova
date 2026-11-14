import uuid
from django.db import models

def gen_flight_pnr():
    return uuid.uuid4().hex[:6].upper()

class FlightRoute(models.Model):
    flight_number = models.CharField(max_length=10, unique=True)
    airline = models.CharField(max_length=60)
    origin_iata = models.CharField(max_length=5, db_index=True)
    destination_iata = models.CharField(max_length=5, db_index=True)
    departure_time = models.TimeField()
    arrival_time = models.TimeField()

    image = models.ImageField(
        upload_to="airline_images/",
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.airline} - {self.flight_number}"

class FlightInstance(models.Model):
    route = models.ForeignKey(FlightRoute, on_delete=models.CASCADE, related_name="instances")
    flight_date = models.DateField(db_index=True)
    cabin_class = models.CharField(max_length=20, default="ECONOMY")
    base_fare = models.DecimalField(max_digits=9, decimal_places=2)
    seats_remaining = models.PositiveIntegerField()

    class Meta:
        unique_together = ("route", "flight_date", "cabin_class")


class FlightSeat(models.Model):
    STATUS_CHOICES = [("AVAILABLE", "Available"), ("LOCKED", "Locked"), ("BOOKED", "Booked")]
    instance = models.ForeignKey(FlightInstance, on_delete=models.CASCADE, related_name="seats")
    seat_number = models.CharField(max_length=6)
    seat_type = models.CharField(max_length=15)
    extra_fee = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="AVAILABLE", db_index=True)
    locked_by_session = models.CharField(max_length=100, null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("instance", "seat_number")


class FlightBooking(models.Model):
    pnr = models.CharField(
        max_length=8,
        unique=True,
        default=gen_flight_pnr
    )

    idempotency_key = models.CharField(max_length=100, unique=True)
    session_id = models.CharField(max_length=100, db_index=True)

    instance = models.ForeignKey(
        FlightInstance,
        on_delete=models.PROTECT,
        related_name="bookings"
    )

    passengers = models.JSONField()
    seat_numbers = models.JSONField(default=list)

    total_fare = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(max_length=20, default="CONFIRMED")
    created_at = models.DateTimeField(auto_now_add=True)

class FlightPayment(models.Model):

    STATUS_CHOICES = [
        ("CREATED", "Created"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("EXPIRED", "Expired"),
        ("SEATS_LOST", "Seats lost"),
    ]

    idempotency_key = models.CharField(max_length=100, unique=True)
    session_id = models.CharField(max_length=100, db_index=True)

    instance = models.ForeignKey(
        FlightInstance,
        on_delete=models.PROTECT,
        related_name="payments"
    )

    passengers = models.JSONField()
    seat_numbers = models.JSONField(default=list)

    amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default="CREATED",
        db_index=True
    )

    razorpay_order_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    razorpay_payment_id = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    razorpay_refund_id = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    refund_status = models.CharField(
        max_length=20,
        null=True,
        blank=True
    )

    refunded_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0
    )

    booking = models.OneToOneField(
        FlightBooking,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment"
    )

    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    seat_versions = models.JSONField(default=dict, blank=True)