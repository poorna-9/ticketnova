
import uuid
import hmac
import hashlib
import json

from decimal import Decimal
from datetime import timedelta

import razorpay

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import (
    FlightInstance,
    FlightSeat,
    FlightBooking,
    FlightPayment,
)

from .serializer import (
    FlightPaymentCreateSerializer,
    FlightPaymentVerifySerializer,
    FlightBookingCancelSerializer,
)


PAYMENT_WINDOW_MINUTES = 10
LOCK_GRACE_MINUTES = 2


def get_razorpay_client():
    return razorpay.Client(
        auth=(
            settings.RAZORPAY_KEY_ID,
            settings.RAZORPAY_KEY_SECRET,
        )
    )


def amount_to_paise(amount):
    return int(
        (Decimal(str(amount)) * 100).quantize(Decimal("1"))
    )


def payment_payload(payment):
    return {
        "payment_id": payment.id,
        "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        "razorpay_order_id": payment.razorpay_order_id,
        "amount": amount_to_paise(payment.amount),
        "currency": "INR",
        "session_id": payment.session_id,
        "expires_at": payment.expires_at,
    }


def generate_pnr():
    # Eight hexadecimal characters provide a larger namespace.
    return uuid.uuid4().hex[:8].upper()


def calculate_flight_fare(instance, seats):
    total = Decimal("0.00")

    for seat in seats:
        total += instance.base_fare + seat.extra_fee

    return total.quantize(Decimal("0.01"))


def release_payment_seats(payment):
    """
    Release only the exact seat locks associated with this payment.
    Do not release a seat that has since been locked again.
    """
    seat_versions = payment.seat_versions or {}

    for seat_number, version in seat_versions.items():
        FlightSeat.objects.filter(
            instance=payment.instance,
            seat_number=seat_number,
            status="LOCKED",
            locked_by_session=payment.session_id,
            version=version,
        ).update(
            status="AVAILABLE",
            locked_by_session=None,
            locked_until=None,
            version=F("version") + 1,
        )

def expire_payment(payment):
    """
    Expire a payment only after the payment window AND grace period.
    """
    with transaction.atomic():
        payment = FlightPayment.objects.select_for_update().get(
            id=payment.id
        )

        if payment.status != "CREATED":
            return payment

        final_expiry = payment.expires_at + timedelta(
            minutes=LOCK_GRACE_MINUTES
        )

        if timezone.now() <= final_expiry:
            return payment

        release_payment_seats(payment)

        payment.status = "EXPIRED"
        payment.save(update_fields=["status"])

    return payment


def finalize_payment(
    razorpay_order_id,
    razorpay_payment_id,
    captured_amount=None,
):
    """
    Confirm a booking only if the exact original seat locks
    are still valid.

    If payment was captured but seats were lost, initiate a refund.
    """

    lost = False
    booking = None

    with transaction.atomic():
        payment = FlightPayment.objects.select_for_update().get(
            razorpay_order_id=razorpay_order_id
        )

        # Duplicate callback/webhook after successful booking.
        if payment.status == "PAID" and payment.booking_id:
            return payment.booking

        if captured_amount is not None:
            expected_amount = amount_to_paise(payment.amount)

            if int(captured_amount) != expected_amount:
                raise ValueError(
                    "Captured payment amount does not match."
                )

        # A captured payment must not be silently ignored just
        # because a previous failure/expiry event was received.
        if payment.status in ["EXPIRED", "FAILED", "SEATS_LOST"]:
            payment.status = "SEATS_LOST"
            payment.razorpay_payment_id = razorpay_payment_id
            payment.save(update_fields=[
                "status",
                "razorpay_payment_id",
            ])
            lost = True

        else:
            now = timezone.now()

            final_expiry = payment.expires_at + timedelta(
                minutes=LOCK_GRACE_MINUTES
            )

            if now > final_expiry:
                lost = True

            else:
                seats = list(
                    FlightSeat.objects.select_for_update().filter(
                        instance=payment.instance,
                        seat_number__in=payment.seat_numbers,
                    ).order_by("id")
                )

                expected_versions = payment.seat_versions or {}

                seats_are_valid = (
                    len(seats) == len(payment.seat_numbers)
                    and all(
                        seat.status == "LOCKED"
                        and seat.locked_by_session == payment.session_id
                        and seat.locked_until is not None
                        and seat.locked_until > now
                        and expected_versions.get(seat.seat_number)
                            == seat.version
                        for seat in seats
                    )
                )

                if not seats_are_valid:
                    lost = True

                else:
                    booking = FlightBooking.objects.create(
                        idempotency_key=payment.idempotency_key,
                        session_id=payment.session_id,
                        instance=payment.instance,
                        passengers=payment.passengers,
                        seat_numbers=payment.seat_numbers,
                        total_fare=payment.amount,
                        status="CONFIRMED",
                    )

                    FlightSeat.objects.filter(
                        id__in=[seat.id for seat in seats]
                    ).update(
                        status="BOOKED",
                        locked_by_session=None,
                        locked_until=None,
                        version=F("version") + 1,
                    )

                    FlightInstance.objects.filter(
                        id=payment.instance_id
                    ).update(
                        seats_remaining=F("seats_remaining") - len(seats)
                    )

                    payment.status = "PAID"
                    payment.razorpay_payment_id = razorpay_payment_id
                    payment.booking = booking

                    payment.save(update_fields=[
                        "status",
                        "razorpay_payment_id",
                        "booking",
                    ])

        if lost:
            payment.status = "SEATS_LOST"
            payment.razorpay_payment_id = razorpay_payment_id

            payment.save(update_fields=[
                "status",
                "razorpay_payment_id",
            ])

    # Never call Razorpay's refund API inside the database transaction.
    if lost:
        initiate_lost_seat_refund(
            payment,
            razorpay_payment_id,
        )
        return None

    return booking


def initiate_lost_seat_refund(payment, razorpay_payment_id):
    """
    Initiate a full refund for a captured payment whose seats were lost.
    Prevent concurrent callbacks from initiating duplicate refunds.
    """

    with transaction.atomic():
        payment = FlightPayment.objects.select_for_update().get(
            id=payment.id
        )

        if payment.refund_status in ["INITIATED", "PROCESSED"]:
            return

        if payment.razorpay_refund_id:
            return

        if payment.refund_status == "PROCESSING":
            return

        payment.refund_status = "PROCESSING"
        payment.save(update_fields=["refund_status"])

    client = get_razorpay_client()

    try:
        refund = client.payment.refund(
            razorpay_payment_id,
            {
                "amount": amount_to_paise(payment.amount),
                "notes": {
                    "reason": "Flight seats no longer available",
                    "payment_id": str(payment.id),
                },
            },
        )

        with transaction.atomic():
            payment = FlightPayment.objects.select_for_update().get(
                id=payment.id
            )

            payment.razorpay_refund_id = refund["id"]
            payment.refund_status = "INITIATED"
            payment.refunded_amount = payment.amount

            payment.save(update_fields=[
                "razorpay_refund_id",
                "refund_status",
                "refunded_amount",
            ])

    except Exception:
        with transaction.atomic():
            payment = FlightPayment.objects.select_for_update().get(
                id=payment.id
            )

            payment.refund_status = "FAILED"
            payment.save(update_fields=["refund_status"])

@api_view(["POST"])
def create_payment_order(request):
    serializer = FlightPaymentCreateSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    session_id = data["session_id"]
    instance_id = data["instance_id"]
    seat_numbers = data["seat_numbers"]
    passengers = data["passengers"]
    idempotency_key = data["idempotency_key"]

    # Idempotency: do not create a second order for the same request.
    existing = FlightPayment.objects.filter(
        idempotency_key=idempotency_key
    ).first()

    if existing:
        same_request = (
            existing.session_id == session_id
            and existing.instance_id == instance_id
            and existing.seat_numbers == seat_numbers
            and existing.passengers == passengers
        )

        if not same_request:
            return Response(
                {"error": "Idempotency key was already used for another request."},
                status=status.HTTP_409_CONFLICT
            )

        if existing.status == "CREATED" and existing.razorpay_order_id:
            if existing.expires_at > timezone.now():
                return Response(payment_payload(existing))

        if existing.status == "PAID":
            return Response({
                "message": "Payment already completed.",
                "booking": existing.booking.pnr if existing.booking else None,
            })

        return Response(
            {"error": "This payment request can no longer be reused."},
            status=status.HTTP_409_CONFLICT
        )

    now = timezone.now()

    try:
        with transaction.atomic():
            instance = FlightInstance.objects.select_for_update().get(
                id=instance_id
            )

            seats = list(
                FlightSeat.objects.select_for_update().filter(
                    instance=instance,
                    seat_number__in=seat_numbers,
                ).order_by("id")
            )

            if len(seats) != len(seat_numbers):
                return Response(
                    {"error": "One or more selected seats do not exist."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for seat in seats:
                if (
                    seat.status != "LOCKED"
                    or seat.locked_by_session != session_id
                    or not seat.locked_until
                    or seat.locked_until <= now
                ):
                    return Response(
                        {
                            "error": f"Seat {seat.seat_number} is not locked by your session."
                        },
                        status=status.HTTP_409_CONFLICT
                    )

            total_amount = calculate_flight_fare(instance, seats)

            expires_at = now + timedelta(
                minutes=PAYMENT_WINDOW_MINUTES
            )

            payment = FlightPayment.objects.create(
                idempotency_key=idempotency_key,
                session_id=session_id,
                instance=instance,
                passengers=passengers,
                seat_numbers=seat_numbers,
                amount=total_amount,
                status="CREATED",
                expires_at=expires_at,
            )

            # Extend seat locks to cover payment time plus grace.
            lock_until = expires_at + timedelta(
                minutes=LOCK_GRACE_MINUTES
            )

            FlightSeat.objects.filter(
                id__in=[seat.id for seat in seats]
            ).update(
                locked_until=lock_until,
                version=F("version") + 1,
            )

            locked_seats = FlightSeat.objects.filter(
                id__in=[seat.id for seat in seats]
            ).values("seat_number", "version")

            payment.seat_versions = {
                seat["seat_number"]: seat["version"]
                for seat in locked_seats
            }

            payment.save(update_fields=["seat_versions"])


    except FlightInstance.DoesNotExist:
        return Response(
            {"error": "Flight not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    # Make the external Razorpay request outside the DB transaction.
    client = get_razorpay_client()

    try:
        order = client.order.create({
            "amount": amount_to_paise(total_amount),
            "currency": "INR",
            "receipt": f"flight-{payment.id}",
            "notes": {
                "payment_id": str(payment.id),
                "session_id": session_id,
                "instance_id": str(instance_id),
            },
        })

        payment.razorpay_order_id = order["id"]
        payment.save(update_fields=["razorpay_order_id"])

    except Exception:
        with transaction.atomic():
            payment = FlightPayment.objects.select_for_update().get(
                id=payment.id
            )
            release_payment_seats(payment)
            payment.status = "FAILED"
            payment.save(update_fields=["status"])

        return Response(
            {"error": "Unable to create Razorpay order."},
            status=status.HTTP_502_BAD_GATEWAY
        )

    return Response(
        payment_payload(payment),
        status=status.HTTP_201_CREATED
    )


@api_view(["POST"])
def verify_payment(request):
    serializer = FlightPaymentVerifySerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data

    try:
        payment = FlightPayment.objects.get(
            razorpay_order_id=data["razorpay_order_id"],
            session_id=data["session_id"],
        )
    except FlightPayment.DoesNotExist:
        return Response(
            {"error": "Payment not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    if payment.status == "PAID" and payment.booking_id:
        return Response({
            "message": "Payment already verified.",
            "pnr": payment.booking.pnr,
        })

    if not data.get("razorpay_payment_id") or not data.get(
        "razorpay_signature"
    ):
        return Response(
            {"error": "Payment ID and signature are required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    client = get_razorpay_client()

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": data["razorpay_order_id"],
            "razorpay_payment_id": data["razorpay_payment_id"],
            "razorpay_signature": data["razorpay_signature"],
        })

        razorpay_payment = client.payment.fetch(
            data["razorpay_payment_id"]
        )

        if razorpay_payment["order_id"] != payment.razorpay_order_id:
            raise ValueError("Payment belongs to a different order.")

        if razorpay_payment["status"] != "captured":
            return Response(
                {"error": "Payment has not been captured yet."},
                status=status.HTTP_409_CONFLICT
            )

        booking = finalize_payment(
            razorpay_order_id=payment.razorpay_order_id,
            razorpay_payment_id=data["razorpay_payment_id"],
            captured_amount=razorpay_payment["amount"],
        )

        if not booking:
            return Response(
                {
                    "error": "Payment received, but seats could not be confirmed. Refund initiated."
                },
                status=status.HTTP_409_CONFLICT
            )

        return Response({
            "message": "Payment successful. Flight booking confirmed.",
            "pnr": booking.pnr,
            "booking": {
                "pnr": booking.pnr,
                "passengers": booking.passengers,
                "seat_numbers": booking.seat_numbers,
                "total_fare": str(booking.total_fare),
            },
        })

    except Exception:
        return Response(
            {"error": "Payment verification failed."},
            status=status.HTTP_400_BAD_REQUEST
        )


@csrf_exempt
@api_view(["POST"])
def razorpay_webhook(request):
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET

    received_signature = request.headers.get(
        "X-Razorpay-Signature"
    )

    if not received_signature:
        return Response(
            {"error": "Missing webhook signature."},
            status=status.HTTP_400_BAD_REQUEST
        )

    expected_signature = hmac.new(
        webhook_secret.encode(),
        request.body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        received_signature
    ):
        return Response(
            {"error": "Invalid webhook signature."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        event = json.loads(request.body)
    except json.JSONDecodeError:
        return Response(
            {"error": "Invalid webhook payload."},
            status=status.HTTP_400_BAD_REQUEST
        )

    event_type = event.get("event")

    if event_type == "payment.captured":
        entity = event["payload"]["payment"]["entity"]

        try:
            booking = finalize_payment(
                razorpay_order_id=entity["order_id"],
                razorpay_payment_id=entity["id"],
                captured_amount=entity["amount"],
            )
        except FlightPayment.DoesNotExist:
            return Response(
                {"error": "Payment record not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        except ValueError:
            return Response(
                {"error": "Captured amount mismatch."},
                status=status.HTTP_400_BAD_REQUEST
            )

    elif event_type == "payment.failed":
        entity = event["payload"]["payment"]["entity"]

        payment = FlightPayment.objects.filter(
            razorpay_order_id=entity.get("order_id")
        ).first()

        if payment:
            with transaction.atomic():
                payment = FlightPayment.objects.select_for_update().get(
                    id=payment.id
                )

                if payment.status == "CREATED":
                    release_payment_seats(payment)
                    payment.status = "FAILED"
                    payment.save(update_fields=["status"])

    elif event_type == "refund.processed":
        entity = event["payload"]["refund"]["entity"]

        payment = FlightPayment.objects.filter(
            razorpay_refund_id=entity["id"]
        ).first()

        if payment:
            with transaction.atomic():
                payment = FlightPayment.objects.select_for_update().get(
                    id=payment.id
                )

                payment.refund_status = "PROCESSED"
                payment.save(update_fields=["refund_status"])

                if payment.booking_id:
                    booking = FlightBooking.objects.select_for_update().get(
                        id=payment.booking_id
                    )

                    if booking.status == "CANCEL_REQUESTED":
                        seats = list(
                            FlightSeat.objects.select_for_update().filter(
                                instance=booking.instance,
                                seat_number__in=booking.seat_numbers,
                                status="BOOKED",
                            )
                        )

                        FlightSeat.objects.filter(
                            id__in=[seat.id for seat in seats]
                        ).update(
                            status="AVAILABLE",
                            version=F("version") + 1,
                        )

                        FlightInstance.objects.filter(
                            id=booking.instance_id
                        ).update(
                            seats_remaining=F("seats_remaining") + len(seats)
                        )

                        booking.status = "CANCELLED"
                        booking.save(update_fields=["status"])

    return Response({"status": "ok"})


@api_view(["GET"])
def payment_status(request, razorpay_order_id):
    session_id = request.query_params.get("session_id")

    if not session_id:
        return Response(
            {"error": "session_id is required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        payment = FlightPayment.objects.get(
            razorpay_order_id=razorpay_order_id,
            session_id=session_id,
        )
    except FlightPayment.DoesNotExist:
        return Response(
            {"error": "Payment not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    final_expiry = payment.expires_at + timedelta(
        minutes=LOCK_GRACE_MINUTES
    )

    if (
        payment.status == "CREATED"
        and final_expiry <= timezone.now()
    ):
        payment = expire_payment(payment)

    return Response({
        "status": payment.status,
        "amount": str(payment.amount),
        "pnr": payment.booking.pnr if payment.booking_id else None,
        "refund_status": payment.refund_status,
    })


@api_view(["POST"])
def cancel_booking(request, pnr):
    serializer = FlightBookingCancelSerializer(data=request.data)

    if not serializer.is_valid():
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    session_id = serializer.validated_data["session_id"]

    try:
        booking = FlightBooking.objects.select_related(
            "instance"
        ).get(
            pnr=pnr,
            session_id=session_id,
        )
    except FlightBooking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    if booking.status != "CONFIRMED":
        return Response(
            {"error": "This booking cannot be cancelled."},
            status=status.HTTP_409_CONFLICT
        )

    payment = FlightPayment.objects.filter(
        booking=booking,
        status="PAID",
    ).first()

    if not payment or not payment.razorpay_payment_id:
        return Response(
            {"error": "Associated payment not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    client = get_razorpay_client()

    try:
        refund = client.payment.refund(
            payment.razorpay_payment_id,
            {
                "amount": amount_to_paise(payment.amount),
                "notes": {
                    "reason": "Flight booking cancellation",
                    "pnr": booking.pnr,
                },
            },
        )

        with transaction.atomic():
            booking = FlightBooking.objects.select_for_update().get(
                id=booking.id
            )
            payment = FlightPayment.objects.select_for_update().get(
                id=payment.id
            )

            # Do not release booked seats until refund.processed arrives.
            booking.status = "CANCEL_REQUESTED"
            booking.save(update_fields=["status"])

            payment.razorpay_refund_id = refund["id"]
            payment.refund_status = "INITIATED"
            payment.refunded_amount = payment.amount
            payment.save(update_fields=[
                "razorpay_refund_id",
                "refund_status",
                "refunded_amount",
            ])

        return Response({
            "message": "Cancellation requested. Refund has been initiated.",
            "pnr": booking.pnr,
            "refund_id": refund["id"],
            "refund_status": "INITIATED",
        })

    except Exception:
        return Response(
            {"error": "Unable to initiate refund. Please retry later."},
            status=status.HTTP_502_BAD_GATEWAY
        )
