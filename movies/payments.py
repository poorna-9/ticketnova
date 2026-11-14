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

from .models import MovieBooking, MoviePayment, MovieShow, ShowSeat
from .serializer import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    PaymentVerifySerializer,
    booking_payload,
)
from .views import fail, ok

logger = logging.getLogger(__name__)

razorpay_client = razorpay.Client(
    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
)

PAYMENT_WINDOW = timedelta(minutes=10)   # how long the user has to pay
LOCK_GRACE = timedelta(minutes=2)        # seats stay held a bit past the window


# ---------------------------------------------------------------- helpers

def payment_payload(pay):
    remaining = max(0, int((pay.expires_at - timezone.now()).total_seconds()))
    return {
        "razorpay_order_id": pay.razorpay_order_id,
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        "amount": int(pay.amount * 100),  # paise
        "currency": "INR",
        "show_id": pay.show_id,
        "seat_labels": pay.seat_labels,
        "expires_at": pay.expires_at.isoformat(),
        "remaining_seconds": remaining,
    }


def release_payment_seats(pay):
    """Free seats still held by this payment's session."""
    return ShowSeat.objects.filter(
        show_id=pay.show_id,
        seat_label__in=pay.seat_labels,
        status="LOCKED",
        locked_by_session=pay.session_id,
    ).update(
        status="AVAILABLE",
        locked_by_session=None,
        locked_until=None,
        version=F("version") + 1,
    )


def expire_payment(pay_id, new_status="EXPIRED"):
    """CREATED -> EXPIRED/FAILED and release seats. Safe to call repeatedly."""
    with transaction.atomic():
        pay = MoviePayment.objects.select_for_update().get(id=pay_id)
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
                "amount": int(Decimal(str(amount)) * 100),
                "speed": "normal",
                "notes": notes or {},
            },
        )
        pay.razorpay_refund_id = refund.get("id")
        pay.refund_status = "INITIATED"
        pay.refunded_amount = amount
        pay.save(update_fields=["razorpay_refund_id", "refund_status", "refunded_amount"])
        return True, refund.get("id")
    except Exception as e:
        logger.exception("Razorpay refund failed for payment %s", pay.id)
        pay.refund_status = "FAILED"
        pay.save(update_fields=["refund_status"])
        return False, str(e)


def finalize_payment(pay_id, razorpay_payment_id):
    """
    Called by BOTH the client callback and the webhook, so it must be idempotent.
    Whoever arrives first confirms the booking; the other gets the same result.
    """
    refund_needed = False
    with transaction.atomic():
        pay = MoviePayment.objects.select_for_update().get(id=pay_id)
        if pay.status in ("PAID", "SEATS_LOST"):
            return pay

        labels = list(pay.seat_labels)
        seats = list(
            ShowSeat.objects.select_for_update()
            .filter(show_id=pay.show_id, seat_label__in=labels)
            .order_by("id")
        )
        # Money is already captured, so accept seats that are still ours
        # (even if locked_until passed) or were released and nobody took them.
        seats_ok = len(seats) == len(labels) and all(
            s.status == "AVAILABLE"
            or (s.status == "LOCKED" and s.locked_by_session == pay.session_id)
            for s in seats
        )

        pay.razorpay_payment_id = razorpay_payment_id

        if seats_ok:
            for s in seats:
                s.status = "BOOKED"
                s.locked_by_session = None
                s.locked_until = None
                s.version += 1
            ShowSeat.objects.bulk_update(
                seats, ["status", "locked_by_session", "locked_until", "version"]
            )
            pay.booking = MovieBooking.objects.create(
                idempotency_key=pay.idempotency_key,
                session_id=pay.session_id,
                show_id=pay.show_id,
                seat_labels=sorted(labels),
                total_amount=pay.amount,
                status="CONFIRMED",
            )
            pay.status = "PAID"
        else:
            pay.status = "SEATS_LOST"
            refund_needed = True
        pay.save()

    if refund_needed:
        process_refund(pay, pay.amount, {"reason": "seats_unavailable"})
    return pay


def release_expired_holds():
    """Run from cron/celery-beat every minute: expire stale payments and stale seat locks."""
    now = timezone.now()
    for pid in MoviePayment.objects.filter(status="CREATED", expires_at__lte=now).values_list("id", flat=True):
        expire_payment(pid)
    return ShowSeat.objects.filter(status="LOCKED", locked_until__lt=now).update(
        status="AVAILABLE", locked_by_session=None, locked_until=None, version=F("version") + 1
    )


# ---------------------------------------------------------------- create order

@api_view(["POST"])
def create_payment_order(request):
    """POST /api/payments/create-order/  (seats must already be locked by this session)"""
    ser = BookingCreateSerializer(data=request.data)
    if not ser.is_valid():
        return fail("VALIDATION_ERROR", "Invalid input.", details=ser.errors)
    d = ser.validated_data
    labels = sorted(d["seat_labels"])
    now = timezone.now()

    # 1) idempotent replay by key
    by_key = MoviePayment.objects.filter(idempotency_key=d["idempotency_key"]).first()
    if by_key:
        if by_key.session_id != d["session_id"]:
            return fail("FORBIDDEN", "Key belongs to another session.", http.HTTP_403_FORBIDDEN)
        if by_key.status == "PAID":
            return ok(
                {"status": "PAID", "booking": booking_payload(by_key.booking)},
                idempotent_replay=True,
            )
        if by_key.status == "CREATED" and by_key.expires_at > now and by_key.razorpay_order_id:
            return ok(payment_payload(by_key), idempotent_replay=True)
        return fail(
            "PAYMENT_NOT_REUSABLE",
            f"Payment is {by_key.status}. Lock seats again and use a new idempotency key.",
            http.HTTP_409_CONFLICT,
        )

    show = get_object_or_404(MovieShow, pk=d["show_id"])

    # 2) reuse an active unpaid order for the same seats
    for p in MoviePayment.objects.filter(
        session_id=d["session_id"], show=show, status="CREATED", expires_at__gt=now
    ).exclude(razorpay_order_id=None):
        if sorted(p.seat_labels) == labels:
            return ok(payment_payload(p), idempotent_replay=True)

    # 3) validate locks, extend them to cover the payment window, create payment row
    expires_at = now + PAYMENT_WINDOW
    with transaction.atomic():
        seats = list(
            ShowSeat.objects.select_for_update()
            .filter(show=show, seat_label__in=labels)
            .order_by("id")
        )
        if len(seats) != len(labels):
            return fail("SEAT_NOT_FOUND", "Some seats do not exist.", http.HTTP_404_NOT_FOUND)

        invalid = sorted(
            s.seat_label
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
                "Seats are not locked by this session or the lock has expired.",
                http.HTTP_409_CONFLICT,
                {"seat_labels": invalid},
            )

        total = sum((s.price for s in seats), Decimal("0"))
        if total <= 0:
            return fail("INVALID_AMOUNT", "Invalid booking amount.")

        for s in seats:
            s.locked_until = expires_at + LOCK_GRACE
            s.version += 1
        ShowSeat.objects.bulk_update(seats, ["locked_until", "version"])

        pay = MoviePayment.objects.create(
            idempotency_key=d["idempotency_key"],
            session_id=d["session_id"],
            show=show,
            seat_labels=labels,
            amount=total,
            status="CREATED",
            expires_at=expires_at,
        )

    # 4) create the Razorpay order (outside the DB transaction)
    try:
        order = razorpay_client.order.create(
            {
                "amount": int(total * 100),
                "currency": "INR",
                "receipt": f"movie_{pay.id}",
                "notes": {
                    "payment_id": str(pay.id),
                    "session_id": d["session_id"],
                    "show_id": str(show.id),
                    "seat_labels": json_dumps_short(labels),
                    "type": "movie",
                },
            }
        )
    except Exception:
        logger.exception("Razorpay order creation failed")
        expire_payment(pay.id, "FAILED")
        return fail(
            "GATEWAY_ERROR", "Payment gateway error. Please try again.", http.HTTP_502_BAD_GATEWAY
        )

    pay.razorpay_order_id = order["id"]
    pay.save(update_fields=["razorpay_order_id"])
    return ok(payment_payload(pay), http.HTTP_201_CREATED)


def json_dumps_short(labels):
    return json.dumps(labels, separators=(",", ":"))


# ---------------------------------------------------------------- client callback

@api_view(["POST"])
def verify_payment(request):
    """POST /api/payments/verify/   (called by the client after Razorpay checkout)"""
    ser = PaymentVerifySerializer(data=request.data)
    if not ser.is_valid():
        return fail("VALIDATION_ERROR", "Invalid input.", details=ser.errors)
    d = ser.validated_data

    pay = MoviePayment.objects.filter(
        razorpay_order_id=d["razorpay_order_id"], session_id=d["session_id"]
    ).first()
    if not pay:
        return fail("PAYMENT_NOT_FOUND", "Payment not found.", http.HTTP_404_NOT_FOUND)

    # checkout reported an error (modal closed / card declined)
    if d.get("error"):
        if pay.status == "CREATED" and pay.expires_at > timezone.now():
            return fail(
                "PAYMENT_FAILED",
                "Payment failed. You can retry until the seat hold expires.",
                http.HTTP_400_BAD_REQUEST,
                {"remaining_seconds": payment_payload(pay)["remaining_seconds"], "can_retry": True},
            )
        expire_payment(pay.id)
        return fail(
            "PAYMENT_EXPIRED", "Seat hold expired. Please select seats again.", http.HTTP_410_GONE
        )

    if not (d.get("razorpay_payment_id") and d.get("razorpay_signature")):
        return fail("VALIDATION_ERROR", "razorpay_payment_id and razorpay_signature are required.")

    try:
        razorpay_client.utility.verify_payment_signature(
            {
                "razorpay_order_id": d["razorpay_order_id"],
                "razorpay_payment_id": d["razorpay_payment_id"],
                "razorpay_signature": d["razorpay_signature"],
            }
        )
    except Exception:
        return fail("INVALID_SIGNATURE", "Invalid payment signature.")

    try:
        pay = finalize_payment(pay.id, d["razorpay_payment_id"])
    except Exception:
        logger.exception("Movie payment finalize failed")
        return fail(
            "FINALIZE_FAILED",
            "Payment received but confirmation is pending. Check status shortly.",
            http.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if pay.status == "PAID":
        return ok({"status": "PAID", "booking": booking_payload(pay.booking)})
    return fail(
        "SEATS_LOST",
        "Seats were no longer available. Your payment is being refunded.",
        http.HTTP_409_CONFLICT,
        {"refund_status": pay.refund_status, "refund_id": pay.razorpay_refund_id},
    )


# ---------------------------------------------------------------- webhook

@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """POST /api/payments/webhook/  (configure in Razorpay dashboard: payment.captured, payment.failed)"""
    payload = request.body
    sig = request.META.get("HTTP_X_RAZORPAY_SIGNATURE")
    try:
        razorpay_client.utility.verify_webhook_signature(
            payload.decode(), sig, settings.RAZORPAY_WEBHOOK_SECRET
        )
    except Exception:
        return HttpResponse(status=400)

    event = json.loads(payload)
    name = event.get("event")
    entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = entity.get("order_id")
    payment_id = entity.get("id")

    pay = MoviePayment.objects.filter(razorpay_order_id=order_id).first()
    if not pay:
        return HttpResponse(status=200)  # not ours / unknown order; nothing to retry

    try:
        if name == "payment.captured":
            finalize_payment(pay.id, payment_id)
        elif name == "payment.failed":
            # Razorpay lets the user retry on the same order, so only expire once the window is over
            if pay.expires_at <= timezone.now():
                expire_payment(pay.id)
            logger.info("Razorpay payment failed for order %s", order_id)
    except Exception:
        logger.exception("Movie webhook handling failed")
        return HttpResponse(status=500)  # Razorpay will retry

    return HttpResponse(status=200)


# ---------------------------------------------------------------- status polling

@api_view(["GET"])
def payment_status(request, razorpay_order_id):
    """GET /api/payments/<order_id>/status/?session_id=abc"""
    sid = request.query_params.get("session_id")
    if not sid:
        return fail("SESSION_REQUIRED", "Query param 'session_id' is required.")
    pay = MoviePayment.objects.filter(razorpay_order_id=razorpay_order_id, session_id=sid).first()
    if not pay:
        return fail("PAYMENT_NOT_FOUND", "Payment not found.", http.HTTP_404_NOT_FOUND)

    if pay.status == "CREATED" and pay.expires_at <= timezone.now():
        pay = expire_payment(pay.id)

    if pay.status == "PAID":
        return ok({"status": "success", "booking_code": pay.booking.booking_code})
    if pay.status == "SEATS_LOST":
        return ok({"status": "refunded", "refund_status": pay.refund_status,
                   "message": "Seats were unavailable; payment is being refunded."})
    if pay.status == "CREATED":
        return ok({"status": "pending", "remaining_seconds": payment_payload(pay)["remaining_seconds"]})
    return ok({"status": pay.status.lower()})


# ---------------------------------------------------------------- cancel + refund

@api_view(["POST"])
def cancel_booking(request, booking_code):
    """POST /api/bookings/<booking_code>/cancel/"""
    ser = BookingCancelSerializer(data=request.data)
    if not ser.is_valid():
        return fail("VALIDATION_ERROR", "Invalid input.", details=ser.errors)

    pay = None
    with transaction.atomic():
        booking = get_object_or_404(
            MovieBooking.objects.select_for_update().select_related("show__movie", "show__screen"),
            booking_code=booking_code.upper(),
        )
        if booking.session_id != ser.validated_data["session_id"]:
            return fail("FORBIDDEN", "Booking belongs to another session.", http.HTTP_403_FORBIDDEN)
        if booking.status != "CONFIRMED":
            return fail("INVALID_STATE", f"Booking is already {booking.status}.", http.HTTP_409_CONFLICT)

        show = booking.show
        starts_at = timezone.make_aware(datetime.combine(show.show_date, show.start_time))
        if starts_at <= timezone.now():
            return fail("SHOW_STARTED", "This show has already started; it can't be cancelled.")

        ShowSeat.objects.filter(
            show=show, seat_label__in=booking.seat_labels, status="BOOKED"
        ).update(
            status="AVAILABLE", locked_by_session=None, locked_until=None,
            version=F("version") + 1,
        )
        booking.status = "CANCELLED"
        booking.save(update_fields=["status"])
        pay = MoviePayment.objects.select_for_update().filter(booking=booking, status="PAID").first()

    refund = {"status": "NOT_APPLICABLE"}
    if pay:
        success, info = process_refund(pay, booking.total_amount, {"booking_code": booking.booking_code})
        refund = {
            "status": "INITIATED" if success else "FAILED",
            "refund_id": info if success else None,
            "amount": str(booking.total_amount) if success else None,
            "message": None if success else "Refund could not be initiated; it will be retried.",
        }

    return ok({"booking": booking_payload(booking), "refund": refund})