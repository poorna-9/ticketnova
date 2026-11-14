
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import MovieShow, ShowSeat


@receiver(post_save, sender=MovieShow)
def create_show_seats(sender, instance, created, **kwargs):
    """
    Generate seats whenever a new movie show is created.
    Layout: 25 rows (A-Y), 10 seats per row.
    """

    if not created:
        return

    seats = []

    for row_number in range(25):
        row_label = chr(ord("A") + row_number)

        for seat_number in range(1, 11):
            seats.append(
                ShowSeat(
                    show=instance,
                    seat_label=f"{row_label}{seat_number}",
                    tier="PRIME",
                    price=instance.base_price,
                    status="AVAILABLE",
                )
            )

    ShowSeat.objects.bulk_create(seats, ignore_conflicts=True)
