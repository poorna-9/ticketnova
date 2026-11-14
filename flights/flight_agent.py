
import uuid
import re
import json

from datetime import datetime, timedelta
from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework.test import APIRequestFactory

from .flight_chat import (
    interpret_flight_query,
    frame_flight_message,
    handle_flight_general,
)

from flights import views, payments


CONTEXT_KEY = "flightcontext"
CONTEXT_TTL_MINUTES = 15

factory = APIRequestFactory()


# --------------------------------------------------
# 1. Session management
# --------------------------------------------------

def new_flight_context():
    return {
        "filters": {},
        "stage": "START",
        "options": [],
        "required_fields": [],
        "intent": None,
        "seat_lock": None,
        "idempotency_key": None,
        "booking_attempt_signature": None,
        "payment_order_id": None,
        "last_modified_at": timezone.now().isoformat(),
    }


def get_flight_context(request):
    context = request.session.get(CONTEXT_KEY)

    if not context:
        return new_flight_context()

    last_modified = context.get("last_modified_at")

    if last_modified:
        try:
            modified_at = datetime.fromisoformat(last_modified)

            if timezone.is_naive(modified_at):
                modified_at = timezone.make_aware(modified_at)

            if timezone.now() - modified_at > timedelta(
                minutes=CONTEXT_TTL_MINUTES
            ):
                return new_flight_context()

        except (ValueError, TypeError):
            return new_flight_context()

    # Support contexts created by the earlier implementation.
    context.setdefault("filters", {})
    context.setdefault("stage", "START")
    context.setdefault("options", [])
    context.setdefault("required_fields", [])
    context.setdefault("intent", None)
    context.setdefault("seat_lock", None)
    context.setdefault("idempotency_key", None)
    context.setdefault("booking_attempt_signature", None)
    context.setdefault("payment_order_id", None)

    return context


def save_flight_context(request, context):
    context["last_modified_at"] = timezone.now().isoformat()

    request.session[CONTEXT_KEY] = context
    request.session.modified = True


def clear_flight_context(request):
    request.session.pop(CONTEXT_KEY, None)
    request.session.modified = True


def get_flight_session_id(request):
    if not request.session.session_key:
        request.session.create()

    return f"chat-{request.session.session_key}"


# --------------------------------------------------
# 2. API helper
# --------------------------------------------------

def call_flight_api(
    request,
    view,
    method="GET",
    path="/",
    data=None,
    view_kwargs=None,
):
    """
    Call DRF views internally without making an HTTP request.

    URL parameters must be passed explicitly through view_kwargs
    because APIRequestFactory does not resolve the URL automatically.
    """

    if method == "GET":
        api_request = factory.get(
            path,
            data=data or {},
        )
    else:
        api_request = factory.post(
            path,
            data=data or {},
            format="json",
        )

    api_request.session = request.session

    response = view(
        api_request,
        **(view_kwargs or {}),
    )

    return {
        "status_code": response.status_code,
        "data": response.data,
    }

# --------------------------------------------------
# 3. Response helper
# --------------------------------------------------

def flight_response(
    message,
    required_fields=None,
    intent="general",
    intent_done=False,
    options=None,
    data=None,
    action=None,
    payment=None,
):
    result = {
        "domain": "flight",
        "message": message,
        "required_fields": required_fields or [],
        "intent": intent,
        "intent_done": intent_done,
        "options": options or [],
        "data": data or {},
    }

    if action:
        result["action"] = action

    if payment:
        result["payment"] = payment

    return result


# --------------------------------------------------
# 4. Convert Pydantic data
# --------------------------------------------------

def get_filter_data(parsed):
    filters = parsed.filters

    if hasattr(filters, "model_dump"):
        return filters.model_dump(exclude_none=True)

    return filters.dict(exclude_none=True)


# --------------------------------------------------
# 5. Merge flight filters
# --------------------------------------------------


def merge_flight_filters(existing, incoming, context):
    result = existing.copy()

    search_fields = [
        "origin_iata",
        "destination_iata",
        "flight_date",
        "cabin_class",
        "airline",
        "departure_time",
    ]

    search_changed = any(
        incoming.get(field) not in (None, "", [])
        and incoming.get(field) != existing.get(field)
        for field in search_fields
    )

    selected_flight_changed = (
        incoming.get("instance_id") is not None
        and incoming.get("instance_id") != existing.get("instance_id")
    )

    selected_seats_changed = (
        incoming.get("seat_numbers") not in (None, "", [])
        and sorted(incoming["seat_numbers"])
        != sorted(existing.get("seat_numbers", []))
    )

    if search_changed:
        for field in [
            "instance_id",
            "seat_numbers",
            "passengers",
            "confirmed",
            "pnr",
        ]:
            result.pop(field, None)

        context["options"] = []
        context["seat_lock"] = None
        context["stage"] = "START"

    elif selected_flight_changed:
        for field in [
            "seat_numbers",
            "passengers",
            "confirmed",
        ]:
            result.pop(field, None)

        context["seat_lock"] = None
        context["stage"] = "FLIGHT_SELECTION"

    elif selected_seats_changed:
        result.pop("passengers", None)
        result.pop("confirmed", None)

        context["seat_lock"] = None
        context["stage"] = "SEAT_SELECTION"

    if search_changed or selected_flight_changed or selected_seats_changed:
        context["idempotency_key"] = None
        context["booking_attempt_signature"] = None
        context["payment_order_id"] = None

    for key, value in incoming.items():
        if value is None or value == "" or value == []:
            continue

        result[key] = value

    return result


# --------------------------------------------------
# 6. Flight fallback handling
# --------------------------------------------------

def apply_flight_fallbacks(
    filters,
    required_fields,
    query,
    current_date,
    options=None,
):
    """
    Apply fallback extraction only to pending fields.
    """

    if not isinstance(filters, dict):
        filters = {}

    if not isinstance(required_fields, list):
        required_fields = []

    filters = filters.copy()
    query = str(query or "").strip()
    normalized = query.lower().strip()

    options = options or []

    # ----------------------------------------------
    # Confirmation fallback
    # ----------------------------------------------

    affirmative = {
        "yes", "yes please", "confirm", "confirmed",
        "proceed", "proceed please", "sure", "okay",
        "ok", "go ahead",
    }

    negative = {
        "no", "no thanks", "don't", "dont",
        "cancel", "not now", "stop",
    }

    if "confirmed" in required_fields:
        if normalized in affirmative:
            filters["confirmed"] = True
        elif normalized in negative:
            filters["confirmed"] = False

    # ----------------------------------------------
    # Flight date fallback
    # ----------------------------------------------

    if "flight_date" in required_fields:

        if normalized == "today":
            filters["flight_date"] = current_date.isoformat()

        elif normalized == "tomorrow":
            filters["flight_date"] = (
                current_date + timedelta(days=1)
            ).isoformat()

        elif normalized in {
            "day after tomorrow",
            "the day after tomorrow",
        }:
            filters["flight_date"] = (
                current_date + timedelta(days=2)
            ).isoformat()

        else:
            match = re.fullmatch(
                r"\s*(\d{4}-\d{2}-\d{2})\s*",
                query,
            )

            if match:
                try:
                    datetime.strptime(
                        match.group(1),
                        "%Y-%m-%d",
                    )
                    filters["flight_date"] = match.group(1)
                except ValueError:
                    pass

    # ----------------------------------------------
    # IATA code fallback
    # ----------------------------------------------

    if (
        "origin_iata" in required_fields
        or "destination_iata" in required_fields
    ):
        codes = re.findall(
            r"\b[A-Z]{3}\b",
            query.upper(),
        )

        if len(codes) == 1:
            if "origin_iata" in required_fields:
                filters["origin_iata"] = codes[0]
            elif "destination_iata" in required_fields:
                filters["destination_iata"] = codes[0]

        elif len(codes) >= 2:
            if "origin_iata" in required_fields:
                filters["origin_iata"] = codes[0]

            if "destination_iata" in required_fields:
                filters["destination_iata"] = codes[-1]

    # ----------------------------------------------
    # Cabin class fallback
    # ----------------------------------------------

    if "cabin_class" in required_fields:

        if "business" in normalized:
            filters["cabin_class"] = "BUSINESS"

        elif "economy" in normalized:
            filters["cabin_class"] = "ECONOMY"

        elif "first" in normalized:
            filters["cabin_class"] = "FIRST"

    # ----------------------------------------------
    # Passenger count fallback
    # ----------------------------------------------

    if "passenger_count" in required_fields:

        match = re.search(
            r"\b(\d+)\s*(?:passengers?|people|persons?|tickets?)\b",
            normalized,
        )

        if match:
            count = int(match.group(1))

            if 1 <= count <= 9:
                filters["passenger_count"] = count

    # ----------------------------------------------
    # Flight option fallback
    # ----------------------------------------------

    if "instance_id" in required_fields:

        match = re.fullmatch(
            r"\s*(?:option\s*)?(\d+)\s*",
            normalized,
        )

        if match:
            option_number = int(match.group(1))

            if 1 <= option_number <= len(options):

                selected = options[option_number - 1]

                instance_id = selected.get(
                    "instance_id",
                    selected.get("id"),
                )

                if instance_id is not None:
                    filters["instance_id"] = int(instance_id)

    # ----------------------------------------------
    # Seat selection fallback
    # ----------------------------------------------

    if "seat_numbers" in required_fields:

        seat_pattern = r"\b(?:[A-Z]?\d{1,2}[A-Z]?)\b"

        seats = re.findall(
            seat_pattern,
            query.upper(),
        )

        if seats:
            filters["seat_numbers"] = list(
                dict.fromkeys(seats)
            )

    # ----------------------------------------------
    # PNR fallback
    # ----------------------------------------------

    if "pnr" in required_fields:

        match = re.fullmatch(
            r"\s*([A-Z0-9]{5,10})\s*",
            query.upper(),
        )

        if match:
            filters["pnr"] = match.group(1)

    return filters

# --------------------------------------------------
# 7. Missing field helper
# --------------------------------------------------

def missing_fields(filters, required):
    missing = []

    for field in required:
        value = filters.get(field)

        if field in ["seat_numbers", "passengers"]:
            if not value:
                missing.append(field)

        elif value is None or value == "":
            missing.append(field)

    return missing


def ask_for_flight_information(
    filters,
    required,
    intent,
    context,
):
    missing = missing_fields(filters, required)

    if not missing:
        return None

    message = frame_flight_message(
        missing,
        context,
    )

    return flight_response(
        message=message,
        required_fields=missing,
        intent=intent,
        intent_done=False,
        data=filters,
    )


# --------------------------------------------------
# 8. Search flights
# --------------------------------------------------


def handle_flight_search(
    request,
    filters,
    context,
    booking_search=False,
):
    required = [
        "origin_iata",
        "destination_iata",
        "flight_date",
    ]

    missing = ask_for_flight_information(
        filters,
        required,
        "book" if booking_search else "search",
        context,
    )

    if missing:
        return missing

    params = {
        "origin_iata": filters["origin_iata"],
        "destination_iata": filters["destination_iata"],
        "flight_date": filters["flight_date"],
        "cabin_class": filters.get("cabin_class", "ECONOMY"),
    }

    if filters.get("airline"):
        params["airline"] = filters["airline"]

    if filters.get("departure_time"):
        params["departure_time"] = filters["departure_time"]

    result = call_flight_api(
        request,
        views.flight_search,
        method="GET",
        path="/flights/search/",
        data=params,
    )

    if result["status_code"] != 200:
        return flight_response(
            message="I couldn't search for flights. Please try again.",
            intent="book" if booking_search else "search",
            intent_done=True,
            data=result["data"],
        )

    flights = result["data"]

    if not flights:
        context["options"] = []
        context["stage"] = "START"

        return flight_response(
            message=(
                "No matching flights were found. "
                "Please try another airline, departure time "
                "or travel date."
            ),
            intent="book" if booking_search else "search",
            intent_done=False,
            data={
                "search_filters": filters,
            },
        )

    context["options"] = flights
    context["stage"] = "FLIGHT_SELECTION"

    # Remember whether this search belongs to a booking.
    context["intent"] = "book" if booking_search else "search"

    if booking_search:
        return flight_response(
            message=(
                f"I found {len(flights)} matching flights. "
                "Please select a flight from the options."
            ),
            required_fields=["instance_id"],
            intent="book",
            intent_done=False,
            options=flights,
            data={
                "passenger_count": filters.get("passenger_count"),
                "search_filters": filters,
            },
        )

    return flight_response(
        message=(
            f"I found {len(flights)} flights. "
            "Please select a flight to view its details."
        ),
        intent="search",
        intent_done=True,
        options=flights,
        data={
            "origin_iata": filters["origin_iata"],
            "destination_iata": filters["destination_iata"],
            "flight_date": filters["flight_date"],
        },
    )


# --------------------------------------------------
# 9. Show flight seats
# --------------------------------------------------
def handle_flight_details(request, filters, context):
    instance_id = filters.get("instance_id")

    if not instance_id:
        return flight_response(
            message="Please select a flight to view its details.",
            required_fields=["instance_id"],
            intent="details",
            intent_done=False,
            options=context.get("options", []),
        )

    result = call_flight_api(
    request,
    views.flight_details,
    method="GET",
    path=f"/flights/{instance_id}/",
    view_kwargs={"instance_id": instance_id},
)

    if result["status_code"] != 200:
        return flight_response(
            message="Unable to retrieve flight details.",
            intent="details",
            intent_done=True,
            data=result["data"],
        )

    return flight_response(
        message="Here are the details of your selected flight.",
        intent="details",
        intent_done=True,
        data=result["data"],
    )



def handle_flight_seats(request, filters, context):
    instance_id = filters.get("instance_id")

    if not instance_id:
        return flight_response(
            message="Please select a flight from the available options first.",
            required_fields=["instance_id"],
            intent="book",
            intent_done=False,
            options=context.get("options", []),
        )

    result = call_flight_api(
        request,
        views.flight_seat_map,
        method="GET",
        path=f"/flights/{instance_id}/seats/",
        view_kwargs={"instance_id": instance_id},
    )

    if result["status_code"] != 200:
        return flight_response(
            message="Unable to retrieve the seat map.",
            intent="book",
            intent_done=False,
            data=result["data"],
        )

    context["stage"] = "SEAT_SELECTION"

    passenger_count = filters.get("passenger_count")

    if not passenger_count or passenger_count < 1:
        return flight_response(
            message="How many passengers will be travelling?",
            required_fields=["passenger_count"],
            intent="book",
            intent_done=False,
            options=result["data"].get("seats", []),
        )

    return flight_response(
        message=(
            f"Here is the seat map. "
            f"Please select exactly {passenger_count} seats."
        ),
        required_fields=["seat_numbers"],
        intent="book",
        intent_done=False,
        options=result["data"].get("seats", []),
        data={
            "flight_id": instance_id,
            "flight_number": result["data"].get("flight_number"),
            "passenger_count": passenger_count,
        },
    )


# --------------------------------------------------
# 10. Lock selected seats
# --------------------------------------------------

def lock_flight_seats(request, filters):
    session_id = get_flight_session_id(request)

    return call_flight_api(
        request,
        views.lock_seats,
        method="POST",
        path=f"/flights/{filters['instance_id']}/seats/lock/",
        data={
            "session_id": session_id,
            "seat_numbers": filters["seat_numbers"],
        },
        view_kwargs={"instance_id": filters["instance_id"]},
    )

def release_flight_seat_lock(request, context):
    """
    Release the previous selection when the user changes
    flight or seats.
    """

    previous_lock = context.get("seat_lock")

    if not previous_lock:
        return

    instance_id = previous_lock.get("instance_id")
    seat_numbers = previous_lock.get("seat_numbers", [])

    if not instance_id or not seat_numbers:
        context["seat_lock"] = None
        return

    session_id = get_flight_session_id(request)

    result = call_flight_api(
        request,
        views.unlock_seats,
        method="POST",
        path=f"/flights/{instance_id}/seats/unlock/",
        data={
            "session_id": session_id,
            "seat_numbers": seat_numbers,
        },
    )
    context["seat_lock"] = None


def validate_flight_selection(instance_id, options):
    if instance_id is None:
        return False

    for option in options:
        option_id = option.get("id")

        if option_id is not None and str(option_id) == str(instance_id):
            return True

    return False



def ensure_flight_seats_locked(request, filters, context):
    """
    Always ask the backend to validate/reacquire the selected seats.

    The local session context is not proof of a valid seat lock.
    """

    current_lock = {
        "instance_id": filters["instance_id"],
        "seat_numbers": sorted(filters["seat_numbers"]),
    }

    result = lock_flight_seats(request, filters)

    if result["status_code"] in [200, 201]:
        context["seat_lock"] = current_lock

    else:
        context["seat_lock"] = None

    return result


# --------------------------------------------------
# 11. Passenger validation and assignment
# --------------------------------------------------


def prepare_passengers(filters):
    passengers = filters.get("passengers", [])
    seat_numbers = filters.get("seat_numbers", [])
    passenger_count = filters.get("passenger_count")

    if not passenger_count or passenger_count < 1:
        return None, "Please provide the number of passengers."

    if len(seat_numbers) != passenger_count:
        return None, (
            f"Please select exactly {passenger_count} seats."
        )

    if len(set(seat_numbers)) != len(seat_numbers):
        return None, "Duplicate seat numbers are not allowed."

    if len(passengers) != passenger_count:
        return None, (
            f"Please provide details for exactly "
            f"{passenger_count} passengers."
        )

    prepared = []

    for index, passenger in enumerate(passengers):
        passenger = passenger.copy()

        if not passenger.get("name"):
            return None, "Please provide the name of every passenger."

        if passenger.get("age") is None:
            return None, "Please provide the age of every passenger."

        if not passenger.get("gender"):
            return None, "Please provide the gender of every passenger."

        passenger["seat_number"] = seat_numbers[index]

        prepared.append(passenger)

    return prepared, None


# --------------------------------------------------
# 12. Booking workflow
# --------------------------------------------------
def get_booking_attempt_signature(filters, passengers):
    """
    Identify one unique booking attempt.

    Same flight + seats + passengers = same attempt.
    Different booking details = new attempt.
    """

    payload = {
        "instance_id": filters["instance_id"],
        "seat_numbers": sorted(filters["seat_numbers"]),
        "passengers": sorted(
            passengers,
            key=lambda p: p["seat_number"],
        ),
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )

    return serialized

def get_booking_idempotency_key(context, filters, passengers):
    signature = get_booking_attempt_signature(
        filters,
        passengers,
    )

    if context.get("booking_attempt_signature") != signature:
        context["booking_attempt_signature"] = signature
        context["idempotency_key"] = uuid.uuid4().hex

    if not context.get("idempotency_key"):
        context["idempotency_key"] = uuid.uuid4().hex

    return context["idempotency_key"]


def handle_flight_booking(request, filters, context):

    # ----------------------------------------------
    # 1. Validate flight selection
    # ----------------------------------------------

    instance_id = filters.get("instance_id")

    if not instance_id:
        return flight_response(
            message="Please select a flight from the available flight options.",
            required_fields=["instance_id"],
            intent="book",
            intent_done=False,
        )

    if not validate_flight_selection(
        instance_id,
        context.get("options", []),
    ):
        return flight_response(
            message=(
                "That flight selection is invalid or is not part of "
                "the displayed results. Please select a flight from "
                "the available options."
            ),
            required_fields=["instance_id"],
            intent="book",
            intent_done=False,
        )

    # ----------------------------------------------
    # 2. Validate passenger count
    # ----------------------------------------------

    passenger_count = filters.get("passenger_count")

    if not passenger_count or passenger_count < 1:
        return flight_response(
            message="How many passengers are travelling?",
            required_fields=["passenger_count"],
            intent="book",
            intent_done=False,
        )

    # ----------------------------------------------
    # 3. Validate seat selection
    # ----------------------------------------------

    seat_numbers = filters.get("seat_numbers", [])

    if len(seat_numbers) != passenger_count:
        return flight_response(
            message=(
                f"Please select exactly {passenger_count} seats "
                f"for your {passenger_count} passenger(s)."
            ),
            required_fields=["seat_numbers"],
            intent="book",
            intent_done=False,
            data={
                "flight_id": instance_id,
                "passenger_count": passenger_count,
            },
        )

    if len(set(seat_numbers)) != len(seat_numbers):
        return flight_response(
            message="Duplicate seats are not allowed. Please select different seats.",
            required_fields=["seat_numbers"],
            intent="book",
            intent_done=False,
        )

    # ----------------------------------------------
    # 4. Lock selected seats
    # ----------------------------------------------

    lock_result = ensure_flight_seats_locked(
        request,
        filters,
        context,
    )

    if lock_result["status_code"] not in [200, 201]:
        return flight_response(
            message=lock_result["data"].get(
                "error",
                "Unable to lock the selected seats.",
            ),
            intent="book",
            intent_done=False,
            data=lock_result["data"],
        )

    context["stage"] = "PASSENGER_DETAILS"

    # ----------------------------------------------
    # 5. Validate passenger details
    # ----------------------------------------------

    passengers, error = prepare_passengers(filters)

    if error:
        return flight_response(
            message=error,
            required_fields=["passengers"],
            intent="book",
            intent_done=False,
            data={
                "seat_numbers": seat_numbers,
                "passenger_count": passenger_count,
            },
        )

    filters["passengers"] = passengers

    # ----------------------------------------------
    # 6. Explicit booking confirmation
    # ----------------------------------------------

    if filters.get("confirmed") is not True:
        context["stage"] = "BOOKING_CONFIRMATION"

        return flight_response(
            message=(
                "Your seats are locked and passenger details are collected. "
                "Please confirm the flight booking to proceed to payment."
            ),
            required_fields=["confirmed"],
            intent="book",
            intent_done=False,
            data={
                "flight_id": instance_id,
                "seat_numbers": seat_numbers,
                "passengers": passengers,
            },
        )

    # ----------------------------------------------
    # 7. Create payment order
    # ----------------------------------------------

    session_id = get_flight_session_id(request)

    idempotency_key = get_booking_idempotency_key(
        context,
        filters,
        passengers,
    )

    payment_result = call_flight_api(
        request,
        payments.create_payment_order,
        method="POST",
        path="/payments/create-order/",
        data={
            "session_id": session_id,
            "instance_id": instance_id,
            "seat_numbers": seat_numbers,
            "passengers": passengers,
            "idempotency_key": idempotency_key,
        },
    )

    if payment_result["status_code"] not in [200, 201]:
        return flight_response(
            message=payment_result["data"].get(
                "error",
                "Unable to create payment order.",
            ),
            intent="book",
            intent_done=False,
            data=payment_result["data"],
        )

    context["stage"] = "PAYMENT_PENDING"

    payment_data = payment_result["data"]
    payment_data["session_id"] = session_id
    payment_data["domain"] = "flight"
    payment_data["verify_url"] = "/flights/payments/verify/"
    payment_data["seat_labels"] = seat_numbers

    selected_flight = next(
        (f for f in context.get("options", []) if f.get("id") == instance_id),
        None
    )
    if selected_flight:
        route = selected_flight.get("route", {})
        airline = route.get("airline", "Airline")
        f_num = route.get("flight_number", "")
        origin = route.get("origin_iata", "")
        dest = route.get("destination_iata", "")
        payment_data["title"] = f"{airline} {f_num}".strip()
        payment_data["movie_title"] = payment_data["title"]
        payment_data["theater"] = f"{origin} → {dest}"
        payment_data["screen"] = f"Class: {selected_flight.get('cabin_class', 'Economy')}"
        payment_data["show_date"] = str(selected_flight.get("flight_date", ""))
        dep = route.get("departure_time", "")
        arr = route.get("arrival_time", "")
        payment_data["start_time"] = f"{dep} - {arr}" if dep and arr else dep

    if "amount" in payment_data:
        try:
            payment_data["amount_in_rupees"] = f"{int(payment_data['amount']) / 100:.2f}"
        except (ValueError, TypeError):
            pass

    return flight_response(
        message=(
            "Your flight seats are locked and payment order has been created. "
            "Please click Pay Now to complete your payment."
        ),
        intent="book",
        intent_done=False,
        action="open_payment",
        payment=payment_data,
        data={
            "flight_id": instance_id,
            "seat_numbers": seat_numbers,
            "passengers": passengers,
        },
    )
# --------------------------------------------------
# 13. My bookings
# --------------------------------------------------

def handle_my_flight_bookings(request):
    session_id = get_flight_session_id(request)

    result = call_flight_api(
        request,
        views.booking_list,
        method="GET",
        path="/bookings/",
        data={"session_id": session_id},
    )

    if result["status_code"] != 200:
        return flight_response(
            message="Unable to retrieve your flight bookings.",
            intent="my_bookings",
            intent_done=True,
        )

    bookings = result["data"]

    if not bookings:
        return flight_response(
            message="You don't have any flight bookings yet.",
            intent="my_bookings",
            intent_done=True,
        )

    return flight_response(
        message="Here are your flight bookings.",
        intent="my_bookings",
        intent_done=True,
        options=bookings,
    )


# --------------------------------------------------
# 14. Cancel flight booking
# --------------------------------------------------

def handle_flight_cancellation(request, filters, context):
    pnr = filters.get("pnr")

    if not pnr:
        return flight_response(
            message=(
                "Please provide the PNR of the flight "
                "you want to cancel."
            ),
            required_fields=["pnr"],
            intent="cancel",
            intent_done=False,
        )

    if filters.get("confirmed") is not True:
        context["stage"] = "CANCEL_CONFIRMATION"

        return flight_response(
            message=(
                f"Are you sure you want to cancel "
                f"flight booking {pnr}?"
            ),
            required_fields=["confirmed"],
            intent="cancel",
            intent_done=False,
            data={"pnr": pnr},
        )

    session_id = get_flight_session_id(request)

    result = call_flight_api(
        request,
        lambda api_request: payments.cancel_booking(
            api_request,
            pnr=pnr,
        ),
        method="POST",
        path=f"/bookings/{pnr}/cancel/",
        data={"session_id": session_id},
    )

    if result["status_code"] not in [200, 201]:
        return flight_response(
            message=result["data"].get(
                "error",
                "Unable to cancel the flight booking."
            ),
            intent="cancel",
            intent_done=True,
            data=result["data"],
        )

    context["stage"] = "CANCEL_REQUESTED"

    return flight_response(
        message=result["data"].get(
            "message",
            "Cancellation request submitted."
        ),
        intent="cancel",
        intent_done=True,
        data=result["data"],
    )


# --------------------------------------------------
# 15. Main flight agent
# --------------------------------------------------

def is_passenger_details_question(query):
    normalized = (query or "").lower().strip()

    patterns = [
        r"\bwhat details\b",
        r"\bwhich details\b",
        r"\bwhat information\b",
        r"\bwhat info\b",
        r"\bdetails required\b",
        r"\binformation required\b",
        r"\bwhat should .* provide\b",
        r"\bwhat do .* need to provide\b",
    ]

    return any(
        re.search(pattern, normalized)
        for pattern in patterns
    )


def handle_flight_query(request, query, required_fields=None):

    context = get_flight_context(request)
    filters = context.get("filters", {})

    # ----------------------------------------------
    # 1. Determine pending fields
    # ----------------------------------------------

    pending_fields = (
        required_fields
        if required_fields is not None
        else context.get("required_fields", [])
    )

    # ----------------------------------------------
    # 2. Handle questions about pending passenger details
    # ----------------------------------------------

    if (
        "passengers" in pending_fields
        and is_passenger_details_question(query)
    ):
        response = flight_response(
            message=(
                "For each passenger, please provide:\n"
                "1. Full name\n"
                "2. Age\n"
                "3. Gender (M, F or OTHER)\n\n"
                f"You have selected {filters.get('passenger_count')} "
                "seats. Please provide these details for each passenger."
            ),
            required_fields=["passengers"],
            intent="book",
            intent_done=False,
            data={
                "passenger_count": filters.get("passenger_count"),
                "seat_numbers": filters.get("seat_numbers", []),
            },
        )

        context["required_fields"] = ["passengers"]
        context["intent"] = "book"

        save_flight_context(request, context)

        return response

    # ----------------------------------------------
    # 3. Prepare parser context
    # ----------------------------------------------

    parser_context = {
        **filters,
        "intent": context.get("intent"),
        "stage": context.get("stage"),
    }

    # ----------------------------------------------
    # 4. Interpret query
    # ----------------------------------------------

    parsed = interpret_flight_query(
        query=query,
        context=parser_context,
        current_date=timezone.localdate(),
        options=context.get("options", []),
        required_fields=pending_fields,
        stage=context.get("stage", "START"),
    )

    print("\n========== FLIGHT PARSER DEBUG ==========")
    print("USER QUERY:", query)
    print("PENDING FIELDS:", pending_fields)
    print("CURRENT INTENT:", context.get("intent"))
    print("CURRENT STAGE:", context.get("stage"))
    print("CURRENT FILTERS:", filters)
    print("AVAILABLE OPTIONS:", context.get("options"))

    print("\nPARSED OBJECT:", parsed)
    print("PARSED INTENT:", parsed.intent)
    print("PARSED ROUTE:", parsed.route)
    print("PARSED FILTERS:", parsed.filters)

    print("=========================================\n")

    incoming_filters = get_filter_data(parsed)

    if not isinstance(incoming_filters, dict):
        incoming_filters = {}

    # ----------------------------------------------
    # 5. Apply fallback extraction
    # ----------------------------------------------

    incoming_filters = apply_flight_fallbacks(
        incoming_filters,
        pending_fields,
        query,
        timezone.localdate(),
        context.get("options", []),
    )

    # ----------------------------------------------
    # 6. Determine intent
    # ----------------------------------------------

    intent = (
        parsed.intent
        or context.get("intent")
        or "general"
    )

    if intent == "unknown":
        intent = "general"

    # Preserve booking intent only when the parser
    # classified this as a pending-field response.
    if (
        parsed.route == "missing_fields"
        and context.get("intent") == "book"
    ):
        intent = "book"

    # ----------------------------------------------
    # 7. Merge filters
    # ----------------------------------------------

    old_instance_id = filters.get("instance_id")

    filters = merge_flight_filters(
        filters,
        incoming_filters,
        context,
    )

    new_instance_id = filters.get("instance_id")

    # ----------------------------------------------
    # 8. Release old seat lock if flight changed
    # ----------------------------------------------

    if old_instance_id and (
        str(old_instance_id) != str(new_instance_id)
    ):
        release_flight_seat_lock(
            request,
            context,
        )

    # ----------------------------------------------
    # 9. Validate flight selection
    # ----------------------------------------------

    if incoming_filters.get("instance_id") is not None:

        if not validate_flight_selection(
            incoming_filters["instance_id"],
            context.get("options", []),
        ):

            filters.pop("instance_id", None)

            context["filters"] = filters
            context["intent"] = intent
            context["required_fields"] = ["instance_id"]

            response = flight_response(
                message=(
                    "Please select a valid flight from the "
                    "displayed flight options."
                ),
                required_fields=["instance_id"],
                intent=intent,
                intent_done=False,
            )

            save_flight_context(request, context)

            return response

    # ----------------------------------------------
    # 10. Booking workflow
    # ----------------------------------------------

    if intent == "book":

        passenger_count = filters.get("passenger_count")
        instance_id = filters.get("instance_id")
        seat_numbers = filters.get("seat_numbers", [])

        # First collect passenger count.
        if not passenger_count:

            response = flight_response(
                message="How many passengers are travelling?",
                required_fields=["passenger_count"],
                intent="book",
                intent_done=False,
            )

        # Search matching flights.
        elif not instance_id:

            response = handle_flight_search(
                request,
                filters,
                context,
                booking_search=True,
            )

        # Validate selected flight.
        elif not validate_flight_selection(
            instance_id,
            context.get("options", []),
        ):

            response = flight_response(
                message=(
                    "Please select a flight from the "
                    "available flight options."
                ),
                required_fields=["instance_id"],
                intent="book",
                intent_done=False,
            )

        # Show seat map until the required number of seats
        # has been selected.
        elif len(seat_numbers) != passenger_count:

            response = handle_flight_seats(
                request,
                filters,
                context,
            )

        # Continue with locking, passengers, confirmation
        # and payment.
        else:

            response = handle_flight_booking(
                request,
                filters,
                context,
            )

    # ----------------------------------------------
    # 11. Normal flight search
    # ----------------------------------------------

    elif intent == "search":

        response = handle_flight_search(
            request,
            filters,
            context,
            booking_search=False,
        )

    # ----------------------------------------------
    # 12. Flight details
    # ----------------------------------------------

    elif intent == "details":

        response = handle_flight_details(
            request,
            filters,
            context,
        )

    # ----------------------------------------------
    # 13. Seat map
    # ----------------------------------------------

    elif intent == "seats":

        response = handle_flight_seats(
            request,
            filters,
            context,
        )

    # ----------------------------------------------
    # 14. Cancellation
    # ----------------------------------------------

    elif intent == "cancel":

        response = handle_flight_cancellation(
            request,
            filters,
            context,
        )

    # ----------------------------------------------
    # 15. My bookings
    # ----------------------------------------------

    elif intent == "my_bookings":

        response = handle_my_flight_bookings(
            request,
        )

    # ----------------------------------------------
    # 16. General flight questions
    # ----------------------------------------------

    else:

        general_result = handle_flight_general(query)

        response = flight_response(
            message=str(general_result),
            intent="general",
            intent_done=True,
        )

    # ----------------------------------------------
    # 17. Normalize response
    # ----------------------------------------------

    if not isinstance(response, dict):

        response = flight_response(
            message=str(response),
            intent=intent,
            intent_done=False,
        )

    # ----------------------------------------------
    # 18. Save context
    # ----------------------------------------------

    context["filters"] = filters

    context["required_fields"] = response.get(
        "required_fields",
        [],
    )

    context["intent"] = (
        None
        if response.get("intent_done")
        else response.get("intent", intent)
    )

    context["last_modified_at"] = timezone.now().isoformat()

    save_flight_context(
        request,
        context,
    )

    return response
