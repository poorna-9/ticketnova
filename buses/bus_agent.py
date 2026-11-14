
"""
Bus booking agent.

The central ROUTER calls:
    result = handle_bus_query(request, query, required_fields)

The orchestrator owns continuation vs new-request classification.

Session memory:
    request.session["buscontext"]

The backend remains the source of truth for:
- Bus trips
- Seat availability
- Seat locks
- Payments
- Bookings
- Cancellation and refunds
"""

import json
import logging
import re
import uuid

from datetime import datetime, timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rest_framework.test import APIRequestFactory

from buses import views as bv
from buses import payments as bp
from buses.models import BusService
from difflib import get_close_matches

from .bus_chat import (
    interpret_bus_query,
    frame_bus_message,
    bus_answer_from_data,
    handle_bus_general,
)

logger = logging.getLogger(__name__)


MODE = "bus_booking"
CONTEXT_KEY = "buscontext"
CONTEXT_TTL_MIN = 15
MAX_SEATS = 6

SHIFT_RANGES = {
    "morning": (0, 12),
    "afternoon": (12, 16),
    "evening": (16, 20),
    "night": (20, 24),
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}

ASK = {
    "source_city": "Which city are you travelling from?",
    "destination_city": "Which city are you travelling to?",
    "date": "On which date would you like to travel?",
    "seat_count": "How many seats would you like?",
    "seat_codes": "Which seats would you like?",
    "boarding_point": "Which boarding point would you prefer?",
    "booking_code": "Please provide your bus ticket ID.",
}


# ============================================================
# 1. SESSION CONTEXT
# ============================================================

def load_context(request):
    ctx = request.session.get(CONTEXT_KEY) or {}

    raw = ctx.get("last_modified_at")
    last = parse_datetime(raw) if raw else None

    if last and timezone.now() > last + timedelta(
        minutes=CONTEXT_TTL_MIN
    ):
        ctx = {}

    ctx["mode"] = MODE
    return ctx


def save_context(request, ctx):
    ctx["last_modified_at"] = timezone.now().isoformat()
    request.session[CONTEXT_KEY] = ctx
    request.session.modified = True

REQUEST_SCOPED = (
    "operator_name",
    "bus_type",
    "departure_time",
    "time_shift",
    "max_price",
    "price_semantic",
    "booking_code",
    "trip_choices",
    "bad_date",
)


def reset_request_scope(ctx):
    for key in REQUEST_SCOPED:
        ctx.pop(key, None)

    invalidate_booking_selection(ctx)


def reset_bus_context(request):
    request.session.pop(CONTEXT_KEY, None)
    request.session.modified = True


def chat_session_id(request):
    """
    Use the same ID for seat locks, payment orders,
    booking history and cancellation.
    """
    if not request.session.session_key:
        request.session.create()

    return f"chat-{request.session.session_key}"


def known_cities():
    sources = BusService.objects.values_list(
        "source_city", flat=True
    )

    destinations = BusService.objects.values_list(
        "destination_city", flat=True
    )

    return sorted(
        set(sources) | set(destinations),
        key=str.lower,
    )

CITY_GROUPS = [
    {"bangalore", "bengaluru", "banglore", "blr"},
    {"mumbai", "bombay"},
    {"hyderabad", "hyd"},
    {"chennai", "madras"},
    {"kolkata", "calcutta"},
    {"delhi", "new delhi"},
]


def canonical_city(value, cities):
    """Map what the user/LLM said to the spelling stored in the DB."""
    v = (value or "").strip().lower()
    by_lower = {c.lower(): c for c in cities}

    if v in by_lower:
        return by_lower[v]

    for group in CITY_GROUPS:
        if v in group:
            for name in group:
                if name in by_lower:
                    return by_lower[name]

    close = get_close_matches(v, list(by_lower), n=1, cutoff=0.75)
    return by_lower[close[0]] if close else (value or "").strip()



# ============================================================
# 2. IN-PROCESS API CALLS
# ============================================================

_factory = APIRequestFactory()


def call_api(view, method="get", data=None, **kwargs):
    """
    These internal API calls pass the explicit session_id
    required by the bus views. They do not depend on the
    original HTTP request's session.
    """
    if method.lower() == "get":
        req = _factory.get("/", data or {})
    else:
        req = _factory.post(
            "/",
            data or {},
            format="json",
        )

    response = view(req, **kwargs)

    return response.status_code, response.data


def api_error(resp, default="Something went wrong."):
    if not isinstance(resp, dict):
        return default

    error = resp.get("error")

    if isinstance(error, dict):
        return error.get("message", default)

    if isinstance(error, str):
        return error

    return default


def response_data(resp):
    """
    Backend success response:
        {"success": True, "data": ...}
    """
    if isinstance(resp, dict):
        return resp.get("data", resp)

    return resp


# ============================================================
# 3. RESPONSE BUILDERS
# ============================================================

def ask(
    request,
    ctx,
    backend_message,
    required_fields=None,
    query="",
    frame=True,
    **extra,
):
    required_fields = required_fields or []

    ctx["required_fields"] = required_fields
    save_context(request, ctx)

    message = (
        frame_bus_message(
            query,
            ctx,
            backend_message,
        )
        if frame
        else backend_message
    )

    return {
        "message": message,
        "required_fields": required_fields,
        "intent": ctx.get("intent"),
        "intent_done": False,
        **extra,
    }


def done(
    request,
    ctx,
    message,
    clear_trip=False,
    **extra,
):
    ctx["required_fields"] = []

    for key in (
        "intent",
        "trip_choices",
        "bad_date",
        "booking_code",
    ):
        ctx.pop(key, None)

    if clear_trip:
        for key in (
            "service_id",
            "trip_id",
            "operator_name",
            "bus_type",
            "departure_time",
            "arrival_time",
            "seat_codes",
            "seat_count",
            "boarding_point",
            "dropping_point",
            "idempotency_key",
            "booking_code",
        ):
            ctx.pop(key, None)

    save_context(request, ctx)

    return {
        "message": message,
        "required_fields": [],
        "intent": None,
        "intent_done": True,
        **extra,
    }


# ============================================================
# 4. NORMALISATION AND FILTER MERGING
# ============================================================

def normalize_date(value):
    try:
        date_value = datetime.strptime(
            (value or "").strip(),
            "%Y-%m-%d",
        ).date()

    except (ValueError, TypeError):
        return None

    if date_value < timezone.localdate():
        return None

    return date_value.isoformat()


def normalize_time(value):
    match = re.fullmatch(
        r"([01]?\d|2[0-3]):([0-5]\d)",
        (value or "").strip(),
    )

    if not match:
        return None

    return (
        f"{int(match.group(1)):02d}:"
        f"{match.group(2)}"
    )


BUS_FIELDS = (
    "source_city",
    "destination_city",
    "date",
    "operator_name",
    "bus_type",
    "boarding_point",
    "dropping_point",
    "departure_time",
    "time_shift",
)

TRIP_FILTER_FIELDS = {
    "source_city",
    "destination_city",
    "date",
    "operator_name",
    "bus_type",
    "departure_time",
    "time_shift",
}


def invalidate_booking_selection(ctx):
    """
    A changed trip or seat selection must not reuse
    an old payment idempotency key.
    """
    for key in (
        "trip_id",
        "service_id",
        "seat_codes",
        "seat_count",
        "boarding_point",
        "dropping_point",
        "trip_choices",
        "idempotency_key",
    ):
        ctx.pop(key, None)

REQUEST_SCOPED = (
    "operator_name",
    "bus_type",
    "departure_time",
    "time_shift",
    "max_price",
    "price_semantic",
    "booking_code",
    "trip_choices",
    "bad_date",
)


def reset_request_scope(ctx):
    """
    A NEW request must not inherit filters or selections from the previous one.
    The route (source_city, destination_city, date) stays on purpose so
    follow-ups like "book the 9 pm one" still work.
    """
    for key in REQUEST_SCOPED:
        ctx.pop(key, None)

    invalidate_booking_selection(ctx)



def merge_filters(ctx, filters):
    filters = filters or {}

    for key in BUS_FIELDS:
        value = filters.get(key)

        if value in ("", None):
            continue

        if key == "date":
            value = normalize_date(value)

            if not value:
                ctx.pop("date", None)
                ctx["bad_date"] = True
                invalidate_booking_selection(ctx)
                continue

            ctx.pop("bad_date", None)

        elif key == "departure_time":
            value = normalize_time(value)

            if not value:
                continue

        else:
            value = str(value).strip()

        if not value:
            continue

        if key in TRIP_FILTER_FIELDS:
            if ctx.get(key) != value:
                invalidate_booking_selection(ctx)

        ctx[key] = value

    # Explicit trip selection.
    trip_id = filters.get("trip_id")

    if trip_id not in ("", None):
        trip_id = str(trip_id).strip()

        if trip_id:
            if str(ctx.get("trip_id", "")) != trip_id:
                for key in (
                    "seat_codes",
                    "seat_count",
                    "boarding_point",
                    "idempotency_key",
                ):
                    ctx.pop(key, None)

            ctx["trip_id"] = trip_id

    # Maximum per-seat price.
    max_price = filters.get("max_price")

    if max_price not in ("", None):
        try:
            ctx["max_price"] = float(max_price)
        except (ValueError, TypeError):
            pass

    price_semantic = filters.get("price_semantic")

    if price_semantic:
        ctx["price_semantic"] = str(price_semantic).strip()

    # Ticket ID.
    booking_code = filters.get("booking_code")

    if booking_code:
        ctx["booking_code"] = str(
            booking_code
        ).strip().upper()

    # Seat count.
    count = filters.get("seat_count")

    if count:
        count = str(count).strip().lower()

        if count.isdigit():
            count = int(count)
        else:
            count = NUMBER_WORDS.get(count)

        if count and 1 <= count <= MAX_SEATS:
            if ctx.get("seat_count") != count:
                ctx.pop("seat_codes", None)
                ctx.pop("idempotency_key", None)

            ctx["seat_count"] = count

    # Explicit seat codes.
    codes = filters.get("seat_codes") or []

    codes = [
        str(code).strip().upper()
        for code in codes
        if str(code).strip()
    ]

    if codes:
        if ctx.get("seat_codes") != codes:
            ctx.pop("idempotency_key", None)

        ctx["seat_codes"] = codes
        ctx["seat_count"] = len(codes)


# ============================================================
# 5. DETERMINISTIC FALLBACKS
# ============================================================

SEAT_CODE_RE = re.compile(
    r"\b([A-Za-z]{1,2}\d{1,2})\b"
)

TICKET_ID_RE = re.compile(
    r"\b([A-Fa-f0-9]{9})\b"
)


def apply_fallbacks(
    data,
    required_fields,
    query,
    ctx,
    cities,
):
    filters = data.setdefault("filters", {})
    q = (query or "").strip()

    # Numeric or word-based seat count.
    if (
        "seat_count" in required_fields
        and not filters.get("seat_count")
    ):
        match = re.search(r"\b(\d+)\b", q)

        if match:
            filters["seat_count"] = match.group(1)

        else:
            word_match = re.search(
                r"\b(" + "|".join(NUMBER_WORDS) + r")\b",
                q.lower(),
            )

            if word_match:
                filters["seat_count"] = str(
                    NUMBER_WORDS[word_match.group(1)]
                )

    # Explicit seat labels.
    if (
        "seat_codes" in required_fields
        and not filters.get("seat_codes")
    ):
        codes = [
            code.upper()
            for code in SEAT_CODE_RE.findall(q)
        ]

        if codes:
            filters["seat_codes"] = codes

    # Ticket ID.
    if (
        "booking_code" in required_fields
        and not filters.get("booking_code")
    ):
        match = TICKET_ID_RE.search(q)

        if match:
            filters["booking_code"] = match.group(1).upper()

    # Trip selection from previously returned options.
    if (
        "trip_id" in required_fields
        and not filters.get("trip_id")
    ):
        choices = ctx.get("trip_choices", [])
        lower_query = q.lower().strip()

        ordinal_words = {
            "first": 0,
            "1st": 0,
            "second": 1,
            "2nd": 1,
            "third": 2,
            "3rd": 2,
            "fourth": 3,
            "4th": 3,
            "fifth": 4,
            "5th": 4,
        }

        for word, index in ordinal_words.items():
            if (
                lower_query == word
                or lower_query == f"{word} one"
                or lower_query == f"the {word} one"
            ):
                if index < len(choices):
                    filters["trip_id"] = str(
                        choices[index]["value"]
                    )
                break

        if not filters.get("trip_id"):
            for choice in choices:
                if lower_query == str(
                    choice.get("value", "")
                ).lower():
                    filters["trip_id"] = str(
                        choice["value"]
                    )
                    break

    # For city fields, accept only known city names.
    if len(required_fields) == 1:
        field = required_fields[0]

        if field in ("source_city", "destination_city") and not filters.get(field):
            guess = canonical_city(q, cities)
            if guess.lower() in {c.lower() for c in cities}:
                filters[field] = guess
            if not filters.get(field):
                matches = [
                    city for city in cities
                    if city.lower() == q.lower()
                ]

                if len(matches) == 1:
                    filters[field] = matches[0]

    # Short operator/boarding/drop replies can be used directly.
    if len(required_fields) == 1:
        field = required_fields[0]

        if field in (
            "operator_name",
            "boarding_point",
            "dropping_point",
        ):
            if (
                not filters.get(field)
                and 0 < len(q.split()) <= 8
            ):
                filters[field] = q

    return data


# ============================================================
# 6. FETCH SERVICES AND TRIPS
# ============================================================

def fetch_services(ctx):
    """
    Actual backend expects:
        source
        destination
        date
    """

    params = {
        "source": ctx["source_city"],
        "destination": ctx["destination_city"],
    }

    if ctx.get("date"):
        params["date"] = ctx["date"]

    code, resp = call_api(
        bv.bus_service_list,
        "get",
        params,
    )

    if code != 200:
        logger.warning(
            "Bus service list failed: %s",
            api_error(resp),
        )
        return []

    data = response_data(resp)

    return data if isinstance(data, list) else []


def fetch_trips_for_service(service_id, date):
    """
    Actual backend expects date as a query parameter.
    Its response is:
        data = {
            "bus": {...},
            "date": "...",
            "trips": [...]
        }
    """

    code, resp = call_api(
        bv.bus_trips,
        "get",
        {"date": date},
        service_id=service_id,
    )

    if code != 200:
        logger.warning(
            "Bus trips request failed for service %s: %s",
            service_id,
            api_error(resp),
        )
        return []

    data = response_data(resp)

    if isinstance(data, dict):
        return data.get("trips", [])

    return []


def fetch_route_trips(ctx):
    services = fetch_services(ctx)
    results = []

    for service in services:
        service_id = service.get("id")

        if service_id is None:
            continue

        trips = fetch_trips_for_service(
            service_id,
            ctx["date"],
        )

        for trip in trips:
            travel_date = str(
                trip.get("travel_date", "")
            )[:10]

            if travel_date != ctx["date"]:
                continue

            results.append({
                **service,
                **trip,
                "service_id": service_id,
                "trip_id": trip.get("id"),
                "operator_name": service.get(
                    "operator_name", ""
                ),
                "bus_type": service.get("bus_type", ""),
                "departure_time": str(
                    service.get("departure_time", "")
                )[:5],
                "arrival_time": str(
                    service.get("arrival_time", "")
                )[:5],
                "travel_date": travel_date,
            })

    return results


def time_in_minutes(value):
    try:
        return (
            int(value[:2]) * 60
            + int(value[3:5])
        )
    except (ValueError, TypeError, IndexError):
        return 0


def filter_trips(trips, ctx):
    results = trips

    if ctx.get("operator_name"):
        name = ctx["operator_name"].lower()

        results = [
            trip for trip in results
            if name in trip.get(
                "operator_name", ""
            ).lower()
        ]

    if ctx.get("bus_type"):
        bus_type = ctx["bus_type"].lower()

        results = [
            trip for trip in results
            if bus_type in trip.get(
                "bus_type", ""
            ).lower()
        ]

    if ctx.get("time_shift") in SHIFT_RANGES:
        lo, hi = SHIFT_RANGES[ctx["time_shift"]]

        results = [
            trip for trip in results
            if lo <= int(
                trip.get("departure_time", "00:00")[:2]
            ) < hi
        ]

    if ctx.get("departure_time"):
        target = time_in_minutes(
            ctx["departure_time"]
        )

        results = [
            trip for trip in results
            if abs(
                time_in_minutes(
                    trip.get("departure_time", "00:00")
                ) - target
            ) <= 60
        ]

    if ctx.get("max_price") is not None:
        results = [
            trip for trip in results
            if float(trip.get("base_fare", 0))
            <= float(ctx["max_price"])
        ]

    if ctx.get("price_semantic") == "cheaper":
        results.sort(
            key=lambda trip: float(
                trip.get("base_fare", 0)
            )
        )

    elif ctx.get("price_semantic") == "expensive":
        results.sort(
            key=lambda trip: float(
                trip.get("base_fare", 0)
            ),
            reverse=True,
        )

    else:
        results.sort(
            key=lambda trip: (
                time_in_minutes(
                    trip.get("departure_time", "00:00")
                ),
                float(trip.get("base_fare", 0)),
            )
        )

    return results

def get_matching_trips(ctx):
    """
    Return matching bus trips and the first missing required field.
    """

    required = (
        "source_city",
        "destination_city",
        "date",
    )

    for field in required:
        if not ctx.get(field):
            return [], field

    trips = fetch_route_trips(ctx)
    trips = filter_trips(trips, ctx)

    return trips, None


def trip_options(trips):
    return [
        {
            "label": (
                f"{trip.get('operator_name')} · "
                f"{trip.get('departure_time')} - "
                f"{trip.get('arrival_time')} · "
                f"₹{trip.get('base_fare')} · "
                f"{trip.get('bus_type')}"
            ),
            "value": str(trip.get("trip_id")),
        }
        for trip in trips
    ]


def resolve_trip(ctx):
    if not ctx.get("source_city"):
        return "ask", {
            "field": "source_city",
            "message": ASK["source_city"],
            "options": [],
        }

    if not ctx.get("destination_city"):
        return "ask", {
            "field": "destination_city",
            "message": ASK["destination_city"],
            "options": [],
        }

    if not ctx.get("date"):
        message = ASK["date"]

        if ctx.get("bad_date"):
            message = (
                "That date is invalid or has already passed. "
                + message
            )

        return "ask", {
            "field": "date",
            "message": message,
            "options": [],
        }

    trips = filter_trips(
        fetch_route_trips(ctx),
        ctx,
    )

    if not trips:
        return "none", {
            "field": "date",
            "message": (
                f"I couldn't find buses from "
                f"{ctx['source_city']} to "
                f"{ctx['destination_city']} on "
                f"{ctx['date']} matching those preferences. "
                "Would you like to try another date or change "
                "your preferences?"
            ),
            "options": [],
        }

    if ctx.get("trip_id"):
        selected = next(
            (
                trip for trip in trips
                if str(trip.get("trip_id"))
                == str(ctx["trip_id"])
            ),
            None,
        )

        if selected:
            ctx.pop("trip_choices", None)
            return "ok", selected

        ctx.pop("trip_id", None)

    if len(trips) > 1:
        options = trip_options(trips)

        # Save the choices so the next reply can be mapped
        # deterministically to the correct trip ID.
        ctx["trip_choices"] = options

        return "ask", {
            "field": "trip_id",
            "message": (
                f"I found {len(trips)} matching buses. "
                "Which trip would you like?"
            ),
            "options": options,
        }

    ctx.pop("trip_choices", None)
    return "ok", trips[0]


# ============================================================
# 7. SEAT MAP
# ============================================================

def get_seat_map(trip_id, session_id):
    code, resp = call_api(
        bv.bus_seat_map,
        "get",
        {"session_id": session_id},
        trip_id=trip_id,
    )

    if code != 200:
        logger.warning(
            "Seat map request failed: %s",
            api_error(resp),
        )
        return None

    return response_data(resp)


def flatten_seats(seat_map):
    if not isinstance(seat_map, dict):
        return []

    if isinstance(seat_map.get("seats"), list):
        return seat_map["seats"]

    groups = (
        seat_map.get("decks")
        or seat_map.get("rows")
        or []
    )

    result = []

    for group in groups:
        result.extend(group.get("seats", []))

    return result


def pick_seats(seats, count, max_price=None):
    usable = []

    for seat in seats:
        available = (
            seat.get("status") == "AVAILABLE"
            or seat.get("locked_by_me")
        )

        if not available:
            continue

        fare = float(seat.get("fare", 0))

        if max_price is not None and fare > max_price:
            continue

        usable.append(seat)

    usable.sort(
        key=lambda seat: (
            float(seat.get("fare", 0)),
            seat.get("seat_code", ""),
        )
    )

    if len(usable) < count:
        return None

    return [
        seat["seat_code"]
        for seat in usable[:count]
    ]


def boarding_point_name(point):
    if isinstance(point, dict):
        return str(point.get("name", "")).strip()

    return str(point).strip()

def match_boarding_point(value, valid_points):
    """Case-insensitive / partial / fuzzy match to one of the trip's boarding points."""
    v = (value or "").strip().lower()

    if not v:
        return None

    for point in valid_points:
        if point.lower() == v:
            return point

    partial = [
        p for p in valid_points
        if v in p.lower() or p.lower() in v
    ]

    if len(partial) == 1:
        return partial[0]

    close = get_close_matches(
        v, [p.lower() for p in valid_points], n=1, cutoff=0.7
    )

    if close:
        return next(p for p in valid_points if p.lower() == close[0])

    return None


# ============================================================
# 8. SEARCH HANDLERS
# ============================================================

def handle_search_buses(request, ctx, query):
    trips, missing_field = get_matching_trips(ctx)

    if missing_field:
        return ask(
            request,
            ctx,
            ASK[missing_field],
            [missing_field],
            query,
        )

    if not trips:
        return done(
            request,
            ctx,
            (
                f"I couldn't find buses from "
                f"{ctx['source_city']} to "
                f"{ctx['destination_city']} on "
                f"{ctx['date']}."
            ),
            data={"trips": []},
        )

    return done(
        request,
        ctx,
        f"I found {len(trips)} matching buses.",
        data={"trips": trips},
    )

def handle_show_bus_trips(request, ctx, query):
    status, result = resolve_trip(ctx)

    if status == "ask":
        return ask(
            request,
            ctx,
            result["message"],
            [result["field"]],
            query,
            options=result["options"],
        )

    if status == "none":
        return done(request, ctx, result["message"])

    return done(
        request,
        ctx,
        "Here are the matching bus trips.",
        data={"trips": [result]},
    )


def handle_show_bus_seats(request, ctx, query):
    status, trip = resolve_trip(ctx)

    if status == "ask":
        return ask(
            request,
            ctx,
            trip["message"],
            [trip["field"]],
            query,
            options=trip["options"],
        )

    if status == "none":
        return done(request, ctx, trip["message"])

    sid = chat_session_id(request)

    seat_map = get_seat_map(
        trip["trip_id"],
        sid,
    )

    if not seat_map:
        return done(
            request,
            ctx,
            "I couldn't load the seat map right now. Please try again.",
        )

    ctx.update(
        trip_id=str(trip["trip_id"]),
        service_id=trip["service_id"],
        operator_name=trip.get("operator_name"),
        bus_type=trip.get("bus_type"),
        departure_time=trip.get("departure_time"),
        arrival_time=trip.get("arrival_time"),
    )

    seats = flatten_seats(seat_map)

    available = sum(
        1
        for seat in seats
        if (
            seat.get("status") == "AVAILABLE"
            or seat.get("locked_by_me")
        )
    )

    return done(
        request,
        ctx,
        f"There are {available} available seats on this bus.",
        data={
            "seat_map": seat_map,
            "trip": trip,
        },
    )


# ============================================================
# 9. BOOKING
# ============================================================

def handle_book_bus(request, ctx, query):
    status, trip = resolve_trip(ctx)

    if status == "ask":
        return ask(
            request,
            ctx,
            trip["message"],
            [trip["field"]],
            query,
            options=trip["options"],
        )

    if status == "none":
        return done(request, ctx, trip["message"])

    # Save the selected trip before collecting seats.
    ctx.update(
        trip_id=str(trip["trip_id"]),
        service_id=trip["service_id"],
        operator_name=trip.get("operator_name"),
        bus_type=trip.get("bus_type"),
        departure_time=trip.get("departure_time"),
        arrival_time=trip.get("arrival_time"),
    )

    sid = chat_session_id(request)

    seat_map = get_seat_map(
        trip["trip_id"],
        sid,
    )

    if not seat_map:
        return done(
            request,
            ctx,
            "I couldn't load the seats for this trip. Please try again.",
        )

    seats = flatten_seats(seat_map)

    seat_index = {
        seat.get("seat_code"): seat
        for seat in seats
        if seat.get("seat_code")
    }

    # 1. Validate explicitly selected seats.
    if ctx.get("seat_codes"):
        codes = ctx["seat_codes"]

        bad = [
            code
            for code in codes
            if (
                code not in seat_index
                or not (
                    seat_index[code].get("status") == "AVAILABLE"
                    or seat_index[code].get("locked_by_me")
                )
            )
        ]

        if bad:
            ctx.pop("seat_codes", None)
            ctx.pop("idempotency_key", None)

            return ask(
                request,
                ctx,
                (
                    f"These seats are unavailable: "
                    f"{', '.join(bad)}. Please choose other seats."
                ),
                ["seat_codes"],
                query,
                data={"seat_map": seat_map},
            )

    # 2. If no explicit seats, choose seats using seat_count.
    else:
        count = ctx.get("seat_count")

        if not count:
            return ask(
                request,
                ctx,
                ASK["seat_count"],
                ["seat_count"],
                query,
                data={"seat_map": seat_map},
            )

        selected = pick_seats(
            seats,
            int(count),
            ctx.get("max_price"),
        )

        if not selected:
            return ask(
                request,
                ctx,
                (
                    f"I couldn't find {count} available seats "
                    "matching your budget. Please choose a different "
                    "seat count or change your budget."
                ),
                ["seat_count"],
                query,
                data={"seat_map": seat_map},
            )

        ctx["seat_codes"] = selected

    codes = ctx["seat_codes"]

    # 3. Validate selected seats again against the seat map.
    total = sum(
        float(seat_index[code].get("fare", 0))
        for code in codes
    )

    # 4. Validate boarding point against the trip's options.
    boarding_points = trip.get("boarding_points") or []

    valid_points = [
        boarding_point_name(point)
        for point in boarding_points
        if boarding_point_name(point)
    ]

    if not valid_points:
        return done(
            request,
            ctx,
            "This trip has no configured boarding points. "
            "Please contact support.",
        )

    if ctx.get("boarding_point"):
        canonical = match_boarding_point(ctx["boarding_point"], valid_points)

        if canonical:
            ctx["boarding_point"] = canonical
        else:
            ctx.pop("boarding_point", None)

    

    if not ctx.get("boarding_point"):
        if len(valid_points) == 1:
            ctx["boarding_point"] = valid_points[0]

        else:
            return ask(
                request,
                ctx,
                ASK["boarding_point"],
                ["boarding_point"],
                query,
                options=[
                    {"label": point, "value": point}
                    for point in valid_points
                ],
            )
    if ctx["boarding_point"] not in valid_points:
        ctx.pop("boarding_point", None)

        return ask(
            request,
            ctx,
            "Please select a valid boarding point.",
            ["boarding_point"],
            query,
            options=[
                {"label": point, "value": point}
                for point in valid_points
            ],
        )

    # 5. All required details are available.
    # No confirmation step. Start payment immediately.
    return execute_book(
        request,
        ctx,
        query,
    )


def execute_book(request, ctx, query):
    sid = chat_session_id(request)

    if not all((
        ctx.get("trip_id"),
        ctx.get("seat_codes"),
        ctx.get("boarding_point"),
    )):
        return ask(
            request,
            ctx,
            "Some booking details are missing. Please provide them again.",
            ["seat_count"],
            query,
        )

    try:
        trip_id = int(ctx["trip_id"])
    except (TypeError, ValueError):
        return done(
            request,
            ctx,
            "The selected bus trip is invalid. Please search again.",
        )

    # Step 1: Lock seats.
    lock_code, lock_resp = call_api(
        bv.lock_seats,
        "post",
        {
            "session_id": sid,
            "seat_codes": ctx["seat_codes"],
        },
        trip_id=trip_id,
    )

    if lock_code != 200:
        ctx.pop("seat_codes", None)
        ctx.pop("idempotency_key", None)

        return ask(
            request,
            ctx,
            (
                f"{api_error(lock_resp)} "
                "Please select your seats again."
            ),
            ["seat_codes"],
            query,
        )

    # Step 2: Generate/reuse idempotency key.
    key = ctx.get("idempotency_key")

    if not key:
        key = uuid.uuid4().hex
        ctx["idempotency_key"] = key

    # Step 3: Create payment order.
    payment_code, payment_resp = call_api(
        bp.create_payment_order,
        "post",
        {
            "session_id": sid,
            "trip_id": trip_id,
            "seat_codes": ctx["seat_codes"],
            "boarding_point": ctx["boarding_point"],
            "idempotency_key": key,
        },
    )

    if payment_code not in (200, 201):
        # The order was not created. Release seats locked by this session.
        call_api(
            bv.unlock_seats,
            "post",
            {
                "session_id": sid,
                "seat_codes": ctx["seat_codes"],
            },
            trip_id=trip_id,
        )

        ctx.pop("idempotency_key", None)

        message = (
            f"I couldn't start the payment: "
            f"{api_error(payment_resp)} "
            "Please try selecting your seats again."
        )

        return ask(
            request,
            ctx,
            message,
            ["seat_codes"],
            query,
        )

    payment = response_data(payment_resp)

    if not isinstance(payment, dict):
        return done(
            request,
            ctx,
            "The payment service returned an unexpected response. "
            "Please check your bookings before trying again.",
        )

    # An idempotent replay may return an already-paid booking.
    if payment.get("status") == "PAID":
        booking = payment.get("booking", {})

        return done(
            request,
            ctx,
            (
                "Your bus is already booked. "
                f"Ticket ID: {booking.get('ticket_id', '')}."
            ),
            clear_trip=True,
            data={"booking": booking},
        )

    # The frontend needs this session ID for payment verification.
    payment["session_id"] = sid

    ctx["pending_order_id"] = payment.get(
        "razorpay_order_id"
    )

    remaining = int(
        payment.get("remaining_seconds", 0) or 0
    )

    mins = max(1, (remaining + 59) // 60)

    return done(
        request,
        ctx,
        (
            f"Your seats {', '.join(ctx['seat_codes'])} "
            "are held. Please complete the payment "
            f"within {mins} minute(s)."
        ),
        clear_trip=True,
        action="open_payment",
        payment=payment,
    )


# ============================================================
# 10. BOOKING HISTORY
# ============================================================

def handle_my_bus_bookings(request, ctx, query):
    code, resp = call_api(
        bv.booking_list,
        "get",
        {"session_id": chat_session_id(request)},
    )

    if code != 200:
        return done(
            request,
            ctx,
            f"I couldn't retrieve your bookings: {api_error(resp)}",
        )

    bookings = response_data(resp)

    if not isinstance(bookings, list):
        bookings = []

    if ctx.get("booking_code"):
        bookings = [
            booking
            for booking in bookings
            if str(booking.get("ticket_id", "")).upper()
            == ctx["booking_code"]
        ]

    if not bookings:
        return done(
            request,
            ctx,
            "I couldn't find any matching bus bookings for you.",
        )

    return done(
        request,
        ctx,
        bus_answer_from_data(
            query,
            {"bookings": bookings},
        ),
        data={"bookings": bookings},
    )


# ============================================================
# 11. CANCELLATION
# ============================================================

def handle_cancel_bus_booking(request, ctx, query):
    code, resp = call_api(
        bv.booking_list,
        "get",
        {"session_id": chat_session_id(request)},
    )

    if code != 200:
        return done(
            request,
            ctx,
            f"I couldn't retrieve your bookings: {api_error(resp)}",
        )

    bookings = response_data(resp)

    if not isinstance(bookings, list):
        bookings = []

    confirmed = [
        booking
        for booking in bookings
        if booking.get("status") == "CONFIRMED"
    ]

    # If the user has not selected a ticket, show confirmed bookings.
    if not ctx.get("booking_code"):
        if not confirmed:
            return done(
                request,
                ctx,
                "You don't have any confirmed bus bookings to cancel.",
            )

        options = []

        for booking in confirmed:
            bus = booking.get("bus") or {}
            trip = booking.get("trip") or {}

            options.append({
                "label": (
                    f"{booking.get('ticket_id')} · "
                    f"{bus.get('source_city', '')} to "
                    f"{bus.get('destination_city', '')} · "
                    f"{trip.get('travel_date', '')}"
                ),
                "value": booking.get("ticket_id"),
            })

        return ask(
            request,
            ctx,
            "Which confirmed bus booking would you like to cancel?",
            ["booking_code"],
            query,
            options=options,
        )

    # Find the selected booking in this session's bookings.
    ticket_id = ctx["booking_code"].upper()

    booking = next(
        (
            item
            for item in confirmed
            if str(item.get("ticket_id", "")).upper()
            == ticket_id
        ),
        None,
    )

    if not booking:
        ctx.pop("booking_code", None)

        return ask(
            request,
            ctx,
            (
                "I couldn't find that confirmed ticket in your bookings. "
                "Please select a valid ticket."
            ),
            ["booking_code"],
            query,
        )

    # No separate confirmation. Call the backend cancellation API.
    return execute_cancel(
        request,
        ctx,
        query,
    )


def execute_cancel(request, ctx, query):
    ticket_id = ctx.get("booking_code")

    if not ticket_id:
        return ask(
            request,
            ctx,
            "Please select the bus ticket you want to cancel.",
            ["booking_code"],
            query,
        )

    code, resp = call_api(
        bp.cancel_booking,
        "post",
        {
            "session_id": chat_session_id(request),
        },
        ticket_id=ticket_id,
    )

    if code != 200:
        return done(
            request,
            ctx,
            f"I couldn't cancel that ticket: {api_error(resp)}",
        )

    data = response_data(resp)

    if not isinstance(data, dict):
        return done(
            request,
            ctx,
            "The cancellation service returned an unexpected response.",
        )

    booking = data.get("booking", {})
    refund = data.get("refund", {})

    refund_status = refund.get("status")

    if refund_status == "INITIATED":
        message = (
            f"Your bus ticket {ticket_id} has been cancelled. "
            f"A refund of ₹{refund.get('amount')} has been initiated."
        )

    elif refund_status == "FAILED":
        message = (
            f"Your bus ticket {ticket_id} has been cancelled, "
            "but the refund could not be initiated. "
            "It needs to be retried."
        )

    else:
        message = (
            f"Your bus ticket {ticket_id} has been cancelled."
        )

    return done(
        request,
        ctx,
        message,
        clear_trip=True,
        data={
            "booking": booking,
            "refund": refund,
        },
    )


# ============================================================
# 12. BUS INFORMATION AND GENERAL CHAT
# ============================================================

def handle_bus_info(request, ctx, query):
    status, trip = resolve_trip(ctx)

    if status == "ask":
        return ask(
            request,
            ctx,
            trip["message"],
            [trip["field"]],
            query,
            options=trip["options"],
        )

    if status == "none":
        return done(
            request,
            ctx,
            trip["message"],
        )

    return done(
        request,
        ctx,
        bus_answer_from_data(
            query,
            {"trip": trip},
        ),
        data={"trip": trip},
    )


def handle_general(request, ctx, query):
    return done(
        request,
        ctx,
        handle_bus_general(
            query,
            ctx.get("source_city"),
            ctx.get("destination_city"),
        ),
    )


# ============================================================
# 13. INTENT DISPATCH
# ============================================================

HANDLERS = {
    "search_buses": handle_search_buses,
    "show_bus_trips": handle_show_bus_trips,
    "show_bus_seats": handle_show_bus_seats,
    "book_bus": handle_book_bus,
    "cancel_bus_booking": handle_cancel_bus_booking,
    "my_bus_bookings": handle_my_bus_bookings,
    "bus_info": handle_bus_info,
    "general": handle_general,
}


# ============================================================
# 14. MAIN ENTRY POINT
# ============================================================

def handle_bus_query(
    request,
    query,
    required_fields=None,
):
    query = (query or "").strip()
    ctx = load_context(request)

    # None means the caller did not supply required_fields.
    # [] means the orchestrator classified this as a new request.
    if required_fields is None:
        required_fields = ctx.get("required_fields", [])

    required_fields = [
        field
        for field in required_fields
        if isinstance(field, str)
    ]

    # --------------------------------------------------------
    # 1. Interpret using the orchestrator's continuation decision
    # --------------------------------------------------------

    cities = known_cities()
    if not required_fields:
        reset_request_scope(ctx)

    parsed = interpret_bus_query(
        query,
        required_fields,
        ctx,
        timezone.localdate(),
        cities,
    )

    if required_fields:
        parsed = apply_fallbacks(
            parsed,
            required_fields,
            query,
            ctx,
            cities,
        )
    filters = parsed.setdefault("filters", {})
    for field in ("source_city", "destination_city"):
        if filters.get(field):
            filters[field] = canonical_city(filters[field], cities)
    # --------------------------------------------------------
    # 2. Resolve intent
    # --------------------------------------------------------

    intent = parsed.get("intent") or "unknown"

    # Only inherit the previous intent when the orchestrator
    # says this message answers pending bus fields.
    if (
        required_fields
        and intent in ("unknown", "")
        and ctx.get("intent")
    ):
        intent = ctx["intent"]

    # A new request must not inherit the previous bus intent.
    if not required_fields and intent in ("unknown", ""):
        intent = "unknown"

    # --------------------------------------------------------
    # 3. Merge extracted filters
    # --------------------------------------------------------

    merge_filters(
        ctx,
        parsed.get("filters", {}),
    )

    ctx["intent"] = intent

    # --------------------------------------------------------
    # 4. Dispatch
    # --------------------------------------------------------

    handler = HANDLERS.get(intent)

    if not handler:
        return ask(
            request,
            ctx,
            (
                "Would you like to search buses, check available seats, "
                "book tickets, view your bookings or cancel a ticket?"
            ),
            [],
            query,
        )

    try:
        result = handler(
            request,
            ctx,
            query,
        )

        # All handler responses must expose required_fields.
        result.setdefault("required_fields", [])

        return result

    except Exception:
        logger.exception(
            "Bus handler failed. intent=%s",
            intent,
        )

        return done(
            request,
            ctx,
            (
                "Sorry, something went wrong while handling "
                "your bus request. Please try again."
            ),
        )


# ============================================================
# 15. OPTIONAL DIRECT HTTP WRAPPER
# ============================================================

def bus_chat_view(request):
    try:
        body = json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        body = {}

    result = handle_bus_query(
        request,
        body.get("query", ""),
        body.get("required_fields"),
    )

    return JsonResponse(result)
