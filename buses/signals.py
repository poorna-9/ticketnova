"""
flights/signals.py: per-cabin seat layouts + seat generation (signal and backfill).

Register in flights/apps.py:

    class FlightsConfig(AppConfig):
        name = "flights"

        def ready(self):
            from . import signals  # noqa: F401

Optional override in settings.py: FLIGHT_SEAT_LAYOUTS = {"ECONOMY": {...}, "BUSINESS": {...}}

Notes
  * Seats belong to an instance, and an instance is one cabin, so each cabin gets its own layout.
  * bulk_create() and loaddata fixtures do NOT trigger the signal. For instances that exist
    already, run:  python manage.py seed_flights   (it backfills) or call
    generate_seats_for_instance(instance) from the shell.
  * seats_remaining is recalculated from the seats every time, so it cannot drift.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import FlightBooking, FlightInstance, FlightSeat

logger = logging.getLogger(__name__)

DEFAULT_LAYOUTS = {
    # 6 abreast, 30 rows = 180 seats
    "ECONOMY": {"rows": 30, "letters": "ABCDEF", "front_rows": 3,
                "front_fee": "300.00", "window_fee": "150.00", "aisle_fee": "100.00"},
    # 4 abreast, 5 rows = 20 seats
    "BUSINESS": {"rows": 5, "letters": "ABCD", "front_rows": 0,
                 "front_fee": "0.00", "window_fee": "500.00", "aisle_fee": "0.00"},
    # 2 abreast, 2 rows = 4 seats
    "FIRST": {"rows": 2, "letters": "AB", "front_rows": 0,
              "front_fee": "0.00", "window_fee": "0.00", "aisle_fee": "0.00"},
}


def get_layout(cabin_class):
    layouts = getattr(settings, "FLIGHT_SEAT_LAYOUTS", DEFAULT_LAYOUTS)
    return layouts.get((cabin_class or "").upper(), layouts["ECONOMY"])


def seat_type_for(letter, letters):
    """First and last letter are windows; the two middle letters are aisles; the rest are middle seats."""
    if letter in (letters[0], letters[-1]):
        return "WINDOW"

    middle = len(letters) // 2

    if letter in (letters[middle - 1], letters[middle]):
        return "AISLE"

    return "MIDDLE"


def build_seats(instance, layout=None):
    """Unsaved FlightSeat objects for an instance (no database access)."""
    layout = layout or get_layout(instance.cabin_class)
    letters = layout["letters"]
    seats = []

    for row in range(1, layout["rows"] + 1):
        for letter in letters:
            seat_type = seat_type_for(letter, letters)
            fee = Decimal("0.00")

            if row <= layout["front_rows"]:
                fee += Decimal(layout["front_fee"])

            if seat_type == "WINDOW":
                fee += Decimal(layout["window_fee"])
            elif seat_type == "AISLE":
                fee += Decimal(layout["aisle_fee"])

            seats.append(FlightSeat(
                instance=instance,
                seat_number=f"{row}{letter}",
                seat_type=seat_type,
                extra_fee=fee,
            ))

    return seats


@transaction.atomic
def generate_seats_for_instance(instance):
    """
    Create the missing seats of one flight instance. Safe to call repeatedly.
    Existing seats are never modified, except: seats owned by confirmed bookings become BOOKED.
    """
    instance = FlightInstance.objects.select_for_update(of=("self",)).get(pk=instance.pk)

    existing = set(
        FlightSeat.objects.filter(instance=instance).values_list("seat_number", flat=True)
    )

    new_seats = [s for s in build_seats(instance) if s.seat_number not in existing]
    FlightSeat.objects.bulk_create(new_seats, ignore_conflicts=True)

    booked = set()
    sold = FlightBooking.objects.filter(
        instance=instance, status__in=["CONFIRMED", "CANCEL_REQUESTED"]
    ).values_list("seat_numbers", flat=True)

    for seat_numbers in sold:
        booked.update(str(n).strip().upper() for n in (seat_numbers or []))

    if booked:
        FlightSeat.objects.filter(
            instance=instance, seat_number__in=booked, status="AVAILABLE"
        ).update(status="BOOKED", locked_by_session=None, locked_until=None,
                 version=F("version") + 1)

    remaining = FlightSeat.objects.filter(instance=instance, status="AVAILABLE").count()
    FlightInstance.objects.filter(pk=instance.pk).update(seats_remaining=remaining)

    return {"created": len(new_seats), "seats_remaining": remaining}


@receiver(post_save, sender=FlightInstance)
def create_seats_for_new_instance(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return

    generate_seats_for_instance(instance)