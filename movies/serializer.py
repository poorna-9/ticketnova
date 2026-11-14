from rest_framework import serializers
from .models import Movie, MovieShow, MovieBooking


class MovieSerializer(serializers.ModelSerializer):
    class Meta:
        model = Movie
        fields = [
            "id",
            "title",
            "language",
            "genre",
            "duration_mins",
            "certification",
            "poster",
        ]


class ShowSerializer(serializers.ModelSerializer):
    available_seats = serializers.IntegerField(read_only=True)

    class Meta:
        model = MovieShow
        fields = ["id", "show_date", "start_time", "base_price", "available_seats"]


# ---------- input serializers ----------

class SeatSelectionSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    seat_labels = serializers.ListField(
        child=serializers.CharField(max_length=10), min_length=1, max_length=10
    )

    def validate_seat_labels(self, value):
        cleaned = [v.strip().upper() for v in value]
        if len(set(cleaned)) != len(cleaned):
            raise serializers.ValidationError("Duplicate seat labels.")
        return cleaned


class BookingCreateSerializer(SeatSelectionSerializer):
    idempotency_key = serializers.CharField(max_length=100)
    show_id = serializers.IntegerField()


class BookingCancelSerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)


# ---------- output helper ----------

def _payment_summary(booking):
    if not hasattr(booking, "payment"):
        return None
    p = booking.payment
    return {
        "razorpay_payment_id": p.razorpay_payment_id,
        "amount": str(p.amount),
        "status": p.status,
        "refund_status": p.refund_status,
        "refunded_amount": str(p.refunded_amount),
    }


def booking_payload(booking: MovieBooking) -> dict:
    show = booking.show
    return {
        "booking_code": booking.booking_code,
        "status": booking.status,
        "show": {
            "id": show.id,
            "movie": show.movie.title,
            "language": show.movie.language,
            "theater": show.screen.theater_name,
            "screen": show.screen.screen_name,
            "city": show.screen.city,
            "show_date": show.show_date.isoformat(),
            "start_time": show.start_time.strftime("%H:%M"),
        },
        "seat_labels": booking.seat_labels,
        "total_amount": str(booking.total_amount),
        "created_at": booking.created_at.isoformat(),
        "payment": _payment_summary(booking),
    }


class PaymentVerifySerializer(serializers.Serializer):
    session_id = serializers.CharField(max_length=100)
    razorpay_order_id = serializers.CharField(max_length=100)
    razorpay_payment_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    razorpay_signature = serializers.CharField(max_length=300, required=False, allow_blank=True)
    error = serializers.DictField(required=False)