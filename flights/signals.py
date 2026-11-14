"""
flights/signals.py: create the FlightSeat rows when a FlightInstance is created.

Register it in flights/apps.py:

    class FlightsConfig(AppConfig):
        name = "flights"

        def ready(self):
            from . import signals  # noqa: F401

Optional layout override in settings.py:

    FLIGHT_SEAT_LAYOUT = {"rows": 30, "letters": "ABCDEF", "front_rows": 3,
                          "front_fee": "300.00", "window_fee": "150.00", "aisle_fee": "100.00"}

Notes:
- bulk_create() and loaddata fixtures do NOT trigger this (post_save is not sent / raw=True).
  Call build_seats() yourself in those cases.
- Make FlightInstance.seats_remaining default to 0; this signal sets the real number.
"""
from decimal import Decimal

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import FlightInstance, FlightSeat

DEFAULT_LAYOUT = {
    "rows": 30,
    "letters": "ABCDEF",
    "front_rows": 3,
    "front_fee": "300.00",
    "window_fee": "150.00",
    "aisle_fee": "100.00",
}


def seat_type_for(letter, letters):
    """First and last letter are windows; the two middle letters (C, D on 6-abreast) are aisles."""
    if letter in (letters[0], letters[-1]):
        return "WINDOW"

    middle = len(letters) // 2

    if letter in (letters[middle - 1], letters[middle]):
        return "AISLE"

    return "MIDDLE"


def build_seats(instance, layout=None):
    """Return unsaved FlightSeat objects for an instance."""
    layout = layout or getattr(settings, "FLIGHT_SEAT_LAYOUT", DEFAULT_LAYOUT)

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

            seats.append(
                FlightSeat(
                    instance=instance,
                    seat_number=f"{row}{letter}",
                    seat_type=seat_type,
                    extra_fee=fee,
                )
            )

    return seats


@receiver(post_save, sender=FlightInstance)
def create_seats_for_new_instance(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return

    # Idempotent: never create seats twice.
    if FlightSeat.objects.filter(instance=instance).exists():
        return

    seats = build_seats(instance)
    FlightSeat.objects.bulk_create(seats)

    FlightInstance.objects.filter(pk=instance.pk).update(seats_remaining=len(seats))