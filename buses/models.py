import uuid
from django.db import models

def gen_bus_ticket():
    return uuid.uuid4().hex[:9].upper()

class BusService(models.Model):
    operator_name = models.CharField(max_length=100)
    bus_type = models.CharField(max_length=60)
    source_city = models.CharField(max_length=60, db_index=True)
    destination_city = models.CharField(max_length=60, db_index=True)
    departure_time = models.TimeField()
    arrival_time = models.TimeField()
    image = models.ImageField(
        upload_to="bus_services/",
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.operator_name}: {self.source_city} to {self.destination_city}"


class BusTrip(models.Model):
    service = models.ForeignKey(BusService, on_delete=models.CASCADE, related_name="trips")
    travel_date = models.DateField(db_index=True)
    base_fare = models.DecimalField(max_digits=8, decimal_places=2)
    boarding_points = models.JSONField(default=list)
    dropping_points = models.JSONField(default=list)


class BusSeat(models.Model):
    STATUS_CHOICES = [("AVAILABLE", "Available"), ("LOCKED", "Locked"), ("BOOKED", "Booked")]
    trip = models.ForeignKey(BusTrip, on_delete=models.CASCADE, related_name="seats")
    seat_code = models.CharField(max_length=8)
    deck = models.CharField(max_length=10)
    is_sleeper = models.BooleanField(default=True)
    fare = models.DecimalField(max_digits=8, decimal_places=2)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="AVAILABLE", db_index=True)
    locked_by_session = models.CharField(max_length=100, null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("trip", "seat_code")


class BusBooking(models.Model):
    ticket_id = models.CharField(max_length=12, unique=True, default=gen_bus_ticket)
    idempotency_key = models.CharField(max_length=100, unique=True)
    session_id = models.CharField(max_length=100, db_index=True)
    trip = models.ForeignKey(BusTrip, on_delete=models.PROTECT)
    seat_codes = models.JSONField()
    boarding_point = models.CharField(max_length=100)
    total_amount = models.DecimalField(max_digits=9, decimal_places=2)
    status = models.CharField(max_length=20, default="CONFIRMED")
    created_at = models.DateTimeField(auto_now_add=True)


class BusPayment(models.Model):
    STATUS_CHOICES = [
        ("CREATED", "Created"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("EXPIRED", "Expired"),
        ("SEATS_LOST", "Seats lost"),
    ]

    idempotency_key = models.CharField(
        max_length=100,
        unique=True
    )

    session_id = models.CharField(
        max_length=100,
        db_index=True
    )

    trip = models.ForeignKey(
        BusTrip,
        on_delete=models.PROTECT,
        related_name="payments"
    )

    seat_codes = models.JSONField()
    boarding_point = models.CharField(max_length=100)

    amount = models.DecimalField(
        max_digits=9,
        decimal_places=2
    )

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
        max_length=12,
        null=True,
        blank=True
    )

    refunded_amount = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        default=0
    )

    booking = models.OneToOneField(
        BusBooking,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment"
    )

    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
