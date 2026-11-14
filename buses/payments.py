
import json
import logging

from datetime import datetime, timedelta
from decimal import Decimal

import razorpay

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from rest_framework import status as http
from rest_framework.decorators import api_view

from .models import (
    BusBooking,
    BusPayment,
    BusTrip,
    BusSeat,
)

from .serializer import (
    BusBookingCreateSerializer,
    BusPaymentVerifySerializer,
    BusBookingCancelSerializer,
    bus_booking_payload,
)

from .views import fail, ok

logger = logging.getLogger(__name__)

razorpay_client = razorpay.Client(
    auth=(
        settings.RAZORPAY_KEY_ID,
        settings.RAZORPAY_KEY_SECRET
    )
)

PAYMENT_WINDOW = timedelta(minutes=10)
LOCK_GRACE = timedelta(minutes=2)


def point_names(points):
    """boarding_points can be ["Silk Board", ...] or [{"name": "Silk Board"}, ...]."""
    names = []

    for point in points or []:
        if isinstance(point, str):
            name = point
        elif isinstance(point, dict):
            name = (
                point.get("name")
                or point.get("point")
                or point.get("location")
                or ""
            )
        elif point is None:
            name = ""
        else:
            name = str(point)

        name = str(name).strip()

        if name:
            names.append(name)

    return names


# ---------- helpers ----------

def payment_payload(pay):

    remaining = max(
        0,
        int((pay.expires_at - timezone.now()).total_seconds())
    )

    return {
        "razorpay_order_id": pay.razorpay_order_id,
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        "amount": int(pay.amount * 100),
        "currency": "INR",
        "trip_id": pay.trip_id,
        "seat_codes": pay.seat_codes,
        "boarding_point": pay.boarding_point,
        "expires_at": pay.expires_at.isoformat(),
        "remaining_seconds": remaining,
    }


def release_payment_seats(pay):
    """
    Release seats still locked by this payment's session.
    """

    return BusSeat.objects.filter(
        trip_id=pay.trip_id,
        seat_code__in=pay.seat_codes,
        status="LOCKED",
        locked_by_session=pay.session_id,
    ).update(
        status="AVAILABLE",
        locked_by_session=None,
        locked_until=None,
        version=F("version") + 1,
    )


def expire_payment(pay_id, new_status="EXPIRED"):
    """
    CREATED -> EXPIRED / FAILED.
    Release associated seat locks.
    """

    with transaction.atomic():

        pay = BusPayment.objects.select_for_update().get(
            id=pay_id
        )

        if pay.status != "CREATED":
            return pay

        pay.status = new_status

        pay.save(update_fields=["status"])

        release_payment_seats(pay)

    return pay


def process_refund(pay, amount, notes=None):

    if not pay or not pay.razorpay_payment_id:
        return False, "No captured payment found to refund"

    if pay.refund_status == "INITIATED":
        return True, pay.razorpay_refund_id

    try:

        refund = razorpay_client.payment.refund(
            pay.razorpay_payment_id,
            {
                "amount": int(
                    Decimal(str(amount)) * 100
                ),
                "speed": "normal",
                "notes": notes or {},
            }
        )

        pay.razorpay_refund_id = refund.get("id")
        pay.refund_status = "INITIATED"
        pay.refunded_amount = amount

        pay.save(
            update_fields=[
                "razorpay_refund_id",
                "refund_status",
                "refunded_amount"
            ]
        )

        return True, refund.get("id")

    except Exception:

        logger.exception(
            "Razorpay refund failed for payment %s",
            pay.id
        )

        pay.refund_status = "FAILED"

        pay.save(update_fields=["refund_status"])

        return False, "Refund initiation failed"


# ---------- finalize payment ----------

def finalize_payment(pay_id, razorpay_payment_id):

    refund_needed = False

    with transaction.atomic():

        pay = BusPayment.objects.select_for_update().get(
            id=pay_id
        )

        # Idempotency: already processed.
        if pay.status in ("PAID", "SEATS_LOST"):
            return pay

        codes = list(pay.seat_codes)

        seats = list(
            BusSeat.objects.select_for_update()
            .filter(
                trip_id=pay.trip_id,
                seat_code__in=codes
            )
            .order_by("id")
        )

        # Payment is captured.
        # Check whether the seats are still available
        # or still locked by this payment's session.

        seats_ok = (
            len(seats) == len(codes)
            and all(
                s.status == "AVAILABLE"
                or (
                    s.status == "LOCKED"
                    and s.locked_by_session == pay.session_id
                )
                for s in seats
            )
        )

        pay.razorpay_payment_id = razorpay_payment_id

        if seats_ok:

            for seat in seats:

                seat.status = "BOOKED"
                seat.locked_by_session = None
                seat.locked_until = None
                seat.version += 1

            BusSeat.objects.bulk_update(
                seats,
                [
                    "status",
                    "locked_by_session",
                    "locked_until",
                    "version"
                ]
            )

            booking = BusBooking.objects.create(
                idempotency_key=pay.idempotency_key,
                session_id=pay.session_id,
                trip_id=pay.trip_id,
                seat_codes=sorted(codes),
                boarding_point=pay.boarding_point,
                total_amount=pay.amount,
                status="CONFIRMED",
            )

            pay.booking = booking
            pay.status = "PAID"

        else:

            pay.status = "SEATS_LOST"
            refund_needed = True

        pay.save()

    # Never call Razorpay while holding database row locks.
    if refund_needed:

        process_refund(
            pay,
            pay.amount,
            {"reason": "seats_unavailable"}
        )

    return pay


# ---------- cleanup ----------

def release_expired_holds():

    now = timezone.now()

    payment_ids = BusPayment.objects.filter(
        status="CREATED",
        expires_at__lte=now
    ).values_list("id", flat=True)

    for pid in payment_ids:
        try:
            expire_payment(pid)
        except Exception:
            logger.exception(
                "Failed to expire bus payment %s",
                pid
            )

    # Release any remaining stale seat locks.
    return BusSeat.objects.filter(
        status="LOCKED",
        locked_until__lt=now
    ).update(
        status="AVAILABLE",
        locked_by_session=None,
        locked_until=None,
        version=F("version") + 1
    )


# ---------- create order ----------

@api_view(["POST"])
def create_payment_order(request):
    """
    POST /api/payments/create-order/
    """

    ser = BusBookingCreateSerializer(data=request.data)

    if not ser.is_valid():
        return fail(
            "VALIDATION_ERROR",
            "Invalid input.",
            details=ser.errors
        )

    d = ser.validated_data

    codes = sorted(d["seat_codes"])
    now = timezone.now()

    # 1. Idempotent replay by key.

    by_key = BusPayment.objects.filter(
        idempotency_key=d["idempotency_key"]
    ).first()

    if by_key:

        if by_key.session_id != d["session_id"]:
            return fail(
                "FORBIDDEN",
                "Key belongs to another session.",
                http.HTTP_403_FORBIDDEN
            )

        if by_key.status == "PAID":
            return ok({
                "status": "PAID",
                "booking": bus_booking_payload(by_key.booking)
            }, idempotent_replay=True)

        if (
            by_key.status == "CREATED"
            and by_key.expires_at > now
            and by_key.razorpay_order_id
        ):
            return ok(
                payment_payload(by_key),
                idempotent_replay=True
            )

        return fail(
            "PAYMENT_NOT_REUSABLE",
            f"Payment is {by_key.status}. "
            "Lock seats again and use a new idempotency key.",
            http.HTTP_409_CONFLICT
        )

    trip = get_object_or_404(BusTrip, pk=d["trip_id"])
    names = point_names(trip.boarding_points)
    boarding_point = d["boarding_point"].strip()

    if names:
        matched = next(
            (n for n in names if n.lower() == boarding_point.lower()),
            None,
        )

        if not matched:
            return fail(
                "INVALID_BOARDING_POINT",
                "Selected boarding point is invalid.",
                details={"boarding_points": names},
            )

        boarding_point = matched
    # 2. Reuse active order for same session and seats.

    for p in BusPayment.objects.filter(
        session_id=d["session_id"],
        trip=trip,
        status="CREATED",
        expires_at__gt=now
    ).exclude(razorpay_order_id=None):

        if (
            sorted(p.seat_codes) == codes
            and p.boarding_point == d["boarding_point"]
        ):
            return ok(
                payment_payload(p),
                idempotent_replay=True
            )

    # 3. Validate seat locks.

    expires_at = now + PAYMENT_WINDOW

    with transaction.atomic():

        seats = list(
            BusSeat.objects.select_for_update()
            .filter(
                trip=trip,
                seat_code__in=codes
            )
            .order_by("id")
        )

        if len(seats) != len(codes):
            return fail(
                "SEAT_NOT_FOUND",
                "Some seats do not exist.",
                http.HTTP_404_NOT_FOUND
            )

        invalid = sorted(
            s.seat_code
            for s in seats
            if not (
                s.status == "LOCKED"
                and s.locked_by_session == d["session_id"]
                and s.locked_until
                and s.locked_until > now
            )
        )

        if invalid:
            return fail(
                "LOCK_INVALID",
                "Seats are not locked by this session "
                "or the lock has expired.",
                http.HTTP_409_CONFLICT,
                {"seat_codes": invalid}
            )


        total = sum(
            (s.fare for s in seats),
            Decimal("0")
        )

        if total <= 0:
            return fail(
                "INVALID_AMOUNT",
                "Invalid booking amount."
            )

        for seat in seats:
            seat.locked_until = expires_at + LOCK_GRACE
            seat.version += 1

        BusSeat.objects.bulk_update(
            seats,
            ["locked_until", "version"]
        )

        pay = BusPayment.objects.create(
            idempotency_key=d["idempotency_key"],
            session_id=d["session_id"],
            trip=trip,
            seat_codes=codes,
            boarding_point=d["boarding_point"],
            amount=total,
            status="CREATED",
            expires_at=expires_at
        )

    # 4. Create Razorpay order outside DB transaction.

    try:

        order = razorpay_client.order.create({
            "amount": int(total * 100),
            "currency": "INR",
            "receipt": f"bus_{pay.id}",
            "notes": {
                "payment_id": str(pay.id),
                "session_id": d["session_id"],
                "trip_id": str(trip.id),
                "seat_codes": json.dumps(
                    codes,
                    separators=(",", ":")
                ),
                "type": "bus"
            }
        })

    except Exception:

        logger.exception(
            "Bus Razorpay order creation failed"
        )

        expire_payment(pay.id, "FAILED")

        return fail(
            "GATEWAY_ERROR",
            "Payment gateway error. Please try again.",
            http.HTTP_502_BAD_GATEWAY
        )

    pay.razorpay_order_id = order["id"]

    pay.save(update_fields=["razorpay_order_id"])

    return ok(
        payment_payload(pay),
        http.HTTP_201_CREATED
    )


# ---------- client callback ----------

@api_view(["POST"])
def verify_payment(request):
    """
    POST /api/payments/verify/
    """

    ser = BusPaymentVerifySerializer(data=request.data)

    if not ser.is_valid():
        return fail(
            "VALIDATION_ERROR",
            "Invalid input.",
            details=ser.errors
        )

    d = ser.validated_data

    pay = BusPayment.objects.filter(
        razorpay_order_id=d["razorpay_order_id"],
        session_id=d["session_id"]
    ).first()

    if not pay:
        return fail(
            "PAYMENT_NOT_FOUND",
            "Payment not found.",
            http.HTTP_404_NOT_FOUND
        )

    # Checkout reported a failure.
    if d.get("error"):

        if (
            pay.status == "CREATED"
            and pay.expires_at > timezone.now()
        ):
            return fail(
                "PAYMENT_FAILED",
                "Payment failed. You can retry until "
                "the seat hold expires.",
                http.HTTP_400_BAD_REQUEST,
                {
                    "remaining_seconds": payment_payload(pay)[
                        "remaining_seconds"
                    ],
                    "can_retry": True
                }
            )

        expire_payment(pay.id)

        return fail(
            "PAYMENT_EXPIRED",
            "Seat hold expired. Please select seats again.",
            http.HTTP_410_GONE
        )

    if not (
        d.get("razorpay_payment_id")
        and d.get("razorpay_signature")
    ):
        return fail(
            "VALIDATION_ERROR",
            "razorpay_payment_id and razorpay_signature "
            "are required."
        )

    # Verify Razorpay signature.
    try:

        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": d["razorpay_order_id"],
            "razorpay_payment_id": d["razorpay_payment_id"],
            "razorpay_signature": d["razorpay_signature"]
        })

    except Exception:

        return fail(
            "INVALID_SIGNATURE",
            "Invalid payment signature."
        )

    # Finalize booking.
    try:

        pay = finalize_payment(
            pay.id,
            d["razorpay_payment_id"]
        )

    except Exception:

        logger.exception(
            "Bus payment finalize failed"
        )

        return fail(
            "FINALIZE_FAILED",
            "Payment received but confirmation is pending. "
            "Check payment status shortly.",
            http.HTTP_500_INTERNAL_SERVER_ERROR
        )

    if pay.status == "PAID":

        return ok({
            "status": "PAID",
            "booking": bus_booking_payload(pay.booking)
        })

    return fail(
        "SEATS_LOST",
        "Seats were no longer available. "
        "Your payment is being refunded.",
        http.HTTP_409_CONFLICT,
        {
            "refund_status": pay.refund_status,
            "refund_id": pay.razorpay_refund_id
        }
    )


# ---------- webhook ----------

@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """
    POST /api/payments/webhook/

    Configure payment.captured and payment.failed
    in Razorpay dashboard.
    """

    payload = request.body

    signature = request.META.get(
        "HTTP_X_RAZORPAY_SIGNATURE"
    )

    try:

        razorpay_client.utility.verify_webhook_signature(
            payload.decode(),
            signature,
            settings.RAZORPAY_WEBHOOK_SECRET
        )

    except Exception:

        return HttpResponse(status=400)

    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    name = event.get("event")

    entity = (
        event.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    order_id = entity.get("order_id")
    payment_id = entity.get("id")

    pay = BusPayment.objects.filter(
        razorpay_order_id=order_id
    ).first()

    if not pay:
        return HttpResponse(status=200)

    try:

        if name == "payment.captured":

            finalize_payment(
                pay.id,
                payment_id
            )

        elif name == "payment.failed":

            if pay.expires_at <= timezone.now():
                expire_payment(pay.id)

            logger.info(
                "Bus payment failed for order %s",
                order_id
            )

    except Exception:

        logger.exception(
            "Bus webhook handling failed"
        )

        return HttpResponse(status=500)

    return HttpResponse(status=200)



# ---------- status polling ----------

@api_view(["GET"])
def payment_status(request, razorpay_order_id):
    """
    GET /api/payments/<order_id>/status/?session_id=abc
    """

    sid = request.query_params.get("session_id")

    if not sid:
        return fail(
            "SESSION_REQUIRED",
            "Query param 'session_id' is required."
        )

    pay = BusPayment.objects.filter(
        razorpay_order_id=razorpay_order_id,
        session_id=sid
    ).first()

    if not pay:
        return fail(
            "PAYMENT_NOT_FOUND",
            "Payment not found.",
            http.HTTP_404_NOT_FOUND
        )

    if (
        pay.status == "CREATED"
        and pay.expires_at <= timezone.now()
    ):
        pay = expire_payment(pay.id)

    if pay.status == "PAID":

        return ok({
            "status": "success",
            "ticket_id": pay.booking.ticket_id
        })

    if pay.status == "SEATS_LOST":

        return ok({
            "status": "refunded",
            "refund_status": pay.refund_status,
            "refund_id": pay.razorpay_refund_id,
            "message": "Seats were unavailable; "
                       "payment is being refunded."
        })

    if pay.status == "CREATED":

        return ok({
            "status": "pending",
            "remaining_seconds": payment_payload(pay)[
                "remaining_seconds"
            ]
        })

    return ok({
        "status": pay.status.lower()
    })



# ---------- cancel booking + refund ----------

@api_view(["POST"])
def cancel_booking(request, ticket_id):
    """
    POST /api/bookings/<ticket_id>/cancel/
    """

    ser = BusBookingCancelSerializer(data=request.data)

    if not ser.is_valid():
        return fail(
            "VALIDATION_ERROR",
            "Invalid input.",
            details=ser.errors
        )

    pay = None

    with transaction.atomic():

        booking = get_object_or_404(
            BusBooking.objects.select_for_update().select_related(
                "trip__service"
            ),
            ticket_id=ticket_id.upper()
        )

        if booking.session_id != ser.validated_data["session_id"]:
            return fail(
                "FORBIDDEN",
                "Booking belongs to another session.",
                http.HTTP_403_FORBIDDEN
            )

        if booking.status != "CONFIRMED":
            return fail(
                "INVALID_STATE",
                f"Booking is already {booking.status}.",
                http.HTTP_409_CONFLICT
            )

        trip = booking.trip
        service = trip.service

        departure_at = timezone.make_aware(
            datetime.combine(
                trip.travel_date,
                service.departure_time
            )
        )

        if departure_at <= timezone.now():
            return fail(
                "TRIP_STARTED",
                "This bus has already departed; "
                "it cannot be cancelled."
            )

        # Release booked seats.
        BusSeat.objects.filter(
            trip=trip,
            seat_code__in=booking.seat_codes,
            status="BOOKED"
        ).update(
            status="AVAILABLE",
            locked_by_session=None,
            locked_until=None,
            version=F("version") + 1
        )

        booking.status = "CANCELLED"

        booking.save(update_fields=["status"])

        pay = BusPayment.objects.select_for_update().filter(
            booking=booking,
            status="PAID"
        ).first()

    refund = {
        "status": "NOT_APPLICABLE"
    }

    if pay:

        success, info = process_refund(
            pay,
            booking.total_amount,
            {"ticket_id": booking.ticket_id}
        )

        refund = {
            "status": "INITIATED" if success else "FAILED",
            "refund_id": info if success else None,
            "amount": (
                str(booking.total_amount)
                if success else None
            ),
            "message": (
                None if success
                else "Refund could not be initiated; "
                     "it will be retried."
            )
        }

    return ok({
        "booking": bus_booking_payload(booking),
        "refund": refund
    })
