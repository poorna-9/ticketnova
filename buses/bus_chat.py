
"""
Bus booking AI layer: Pydantic schemas + prompts + chains.
The central orchestrator decides whether a message is a continuation.
"""

import json
import logging
from typing import List, Literal

from django.conf import settings
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================
# 1. PYDANTIC SCHEMAS
# ============================================================

BUS_INTENTS = Literal[
    "search_buses",
    "show_bus_trips",
    "show_bus_seats",
    "book_bus",
    "cancel_bus_booking",
    "my_bus_bookings",
    "bus_info",
    "general",
    "unknown",
    "",
]


class BusFilters(BaseModel):
    source_city: str = ""
    destination_city: str = ""
    date: str = ""
    trip_id: str = ""
    operator_name: str = ""
    bus_type: str = ""
    boarding_point: str = ""
    dropping_point: str = ""
    departure_time: str = ""
    time_shift: str = ""
    seat_count: str = ""
    seat_codes: List[str] = Field(default_factory=list)
    max_price: str = ""
    price_semantic: str = ""
    booking_code: str = ""


class BusQuerySchema(BaseModel):
    booking_type: Literal["bus_booking"]
    intent: BUS_INTENTS
    query_text: str
    filters: BusFilters


class BusMessageSchema(BaseModel):
    message: str


bus_parser = PydanticOutputParser(pydantic_object=BusQuerySchema)
bus_message_parser = PydanticOutputParser(
    pydantic_object=BusMessageSchema
)


# ============================================================
# 2. LLMs
# ============================================================

bus_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.1,
    openai_api_key=settings.OPENAI_API_KEY,
)

bus_answer_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.1,
    max_tokens=220,
    openai_api_key=settings.OPENAI_API_KEY,
)

bus_general_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0.4,
    openai_api_key=settings.OPENAI_API_KEY,
)


# ============================================================
# 3. MAIN PARSER
# ============================================================

bus_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a STRICT JSON generator for a BUS TICKET BOOKING SYSTEM.

CRITICAL RULES:
- Output ONLY valid JSON.
- booking_type MUST be "bus_booking".
- Missing or unknown values MUST be empty strings.
- seat_codes must be an empty list when not explicitly provided.
- NEVER guess.
- Extract information only from the CURRENT user message.
- query_text MUST be the original user message unchanged.
- Put extracted values inside filters.

TODAY: {today} ({weekday}).

CURRENT KNOWN CONTEXT:
{context}

INTENT RULES:
- search_buses: find buses between cities.
- show_bus_trips: show timings, operators, fares or trips.
- show_bus_seats: show seat availability or seat layout.
- book_bus: book seats or purchase bus tickets.
- cancel_bus_booking: cancel a bus ticket or booking.
- my_bus_bookings: booking history or booking status.
- bus_info: information about a bus, operator or trip.
- general: greetings, thanks, general capability questions.
- unknown: unclear request.

ROUTE:
- source_city is the departure city.
- destination_city is the arrival city.
- Never interchange them.
- "from Hyderabad to Bangalore" means source=Hyderabad,
  destination=Bangalore.
- A locality or bus stand is not a city unless clearly stated.
- operator_name is the bus operator.
KNOWN CITIES (use these exact spellings; map aliases such as
Bengaluru=Bangalore, Bombay=Mumbai, Hyd=Hyderabad):
{known_cities}
DATE:
- Resolve dates to YYYY-MM-DD using TODAY.
- tomorrow = today + 1 day.
- day after tomorrow = today + 2 days.
- Resolve weekdays to the next occurrence, including today.
- Leave unresolved dates empty.

TIME:
- Use 24-hour HH:MM.
- "9 pm" = "21:00".
- "6:30 am" = "06:30".
- time_shift can be morning, afternoon, evening or night.

BUS TYPE:
- Extract only when stated.
- Examples: AC sleeper, non-AC seater, Volvo, semi-sleeper.

SEATS:
- seat_count must be digits as a string.
- "two tickets" = "2".
- seat_codes must contain explicitly selected seat labels only.
- "L1 and L2" = ["L1", "L2"].
- Never invent seat codes.

TRIP:
- trip_id must be the exact selected trip ID if the user provides it.
- Do not invent a trip ID.

PRICE:
- max_price is a numeric per-seat budget.
- "under 800 per person" = "800".
- "cheapest" = price_semantic="cheaper".
- "expensive" = price_semantic="expensive".

BOOKING:
- booking_code is the ticket ID quoted by the user.
- Do not mistake a seat code for a ticket ID.

{format_instructions}
"""),
    ("human", "{query}"),
]).partial(
    format_instructions=bus_parser.get_format_instructions()
)

bus_chain = bus_prompt | bus_llm | bus_parser


# ============================================================
# 4. MISSING-FIELDS PARSER
# ============================================================

bus_missing_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a STRICT JSON generator for a BUS BOOKING SYSTEM.

The central orchestrator has already decided that this message
continues the current bus flow.

The system is waiting for these fields:
{required_fields}

RULES:
- Output the FULL Pydantic JSON schema.
- Extract only values clearly supplied in the CURRENT reply.
- Do not infer values.
- Always set intent="unknown".
- booking_type MUST be "bus_booking".
- Unknown strings must be "".
- Unknown seat_codes must be [].

TODAY: {today} ({weekday}).

FIELD INTERPRETATION:
- source_city: departure city.
- destination_city: arrival city.
- date: YYYY-MM-DD.
- trip_id: exact ID of the selected trip.
- operator_name: bus operator.
- bus_type: bus type.
- boarding_point: pickup location.
- dropping_point: drop location.
- departure_time: HH:MM.
- time_shift: morning, afternoon, evening or night.
- seat_count: number of seats.
- seat_codes: explicit seat labels.
- booking_code: ticket ID.

SHORT REPLIES:
- If source_city is requested, "Hyderabad" means source city.
- If destination_city is requested, "Bangalore" means destination city.
- If date is requested, resolve "tomorrow" using TODAY.
- If seat_count is requested, "3" means three seats.
- If seat_codes is requested, "L1, L2" means ["L1", "L2"].
- If trip_id is requested, extract the exact selected ID.
- If booking_code is requested, extract the ticket ID.
- If boarding_point is requested, interpret the reply as a pickup location.

TRIP SELECTION:
Available trip choices:
{trip_choices}

If the user says "first one", "second one", etc.,
select the corresponding trip ID from the choices.
If the user provides an exact trip ID, preserve it exactly.
Never invent a trip ID.

KNOWN CITIES:
{known_cities}

CURRENT CONTEXT:
{context}

{format_instructions}
"""),
    ("human", "{query}"),
]).partial(
    format_instructions=bus_parser.get_format_instructions()
)

bus_missing_chain = bus_missing_prompt | bus_llm | bus_parser


# ============================================================
# 5. RESPONSE FRAMING
# ============================================================

bus_frame_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a friendly bus ticket booking assistant.

Rewrite the backend instruction as one natural chatbot message.
- Preserve every city, operator, date, price, seat and option.
- Do not invent details.
- Do not change the meaning.
- Return only JSON with one key: message.

{format_instructions}
"""),
    ("human", """
User query:
{query}

Current context:
{context}

Backend instruction:
{backend_message}
"""),
]).partial(
    format_instructions=bus_message_parser.get_format_instructions()
)

bus_frame_chain = bus_frame_prompt | bus_llm | bus_message_parser


# ============================================================
# 6. DATA ANSWER
# ============================================================

bus_answer_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a bus booking assistant.
Answer using ONLY the supplied data.
Never invent operators, fares, timings, seats or booking statuses.

- One result: explain its relevant details.
- Multiple results: summarise useful choices.
- Keep the answer under 120 words.
- Never dump raw JSON.

Return only JSON with one key: message.

{format_instructions}
"""),
    ("human", """
User query:
{query}

Data:
{data}
"""),
]).partial(
    format_instructions=bus_message_parser.get_format_instructions()
)

bus_answer_chain = bus_answer_prompt | bus_answer_llm | bus_message_parser


# ============================================================
# 7. GENERAL CHAT
# ============================================================

bus_general_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a friendly bus ticket booking assistant.

You can help users:
- Find buses between cities.
- Compare operators, fares and timings.
- View seat layouts and availability.
- Book bus tickets and pay.
- View bookings.
- Cancel bookings and request refunds.

Be warm and concise.
If a route is already known, use it naturally.
"""),
    ("human", "User Query: {query}\nKnown Route: {route}"),
])

bus_general_chain = bus_general_prompt | bus_general_llm


# ============================================================
# 8. PUBLIC FUNCTIONS
# ============================================================

CONTEXT_KEYS_FOR_PROMPT = (
    "intent",
    "source_city",
    "destination_city",
    "date",
    "trip_id",
    "operator_name",
    "bus_type",
    "boarding_point",
    "dropping_point",
    "departure_time",
    "time_shift",
    "seat_count",
    "seat_codes",
    "max_price",
    "trip_choices",
)


def _ctx_view(context):
    return json.dumps(
        {
            key: context.get(key)
            for key in CONTEXT_KEYS_FOR_PROMPT
            if context.get(key)
        },
        default=str,
    )


def _empty_result(query):
    return {
        "booking_type": "bus_booking",
        "intent": "unknown",
        "query_text": query,
        "filters": BusFilters().model_dump(),
    }


def interpret_bus_query(
    query,
    required_fields,
    context,
    today,
    known_cities,
):
    weekday = today.strftime("%A")

    base = {
        "query": query,
        "today": today.isoformat(),
        "weekday": weekday,
        "context": _ctx_view(context),
        "known_cities": ", ".join(known_cities) or "none",
    }

    try:
        if required_fields:
            out = bus_missing_chain.invoke({
                **base,
                "required_fields": ", ".join(required_fields),
                "known_cities": ", ".join(known_cities) or "none",
                "trip_choices": json.dumps(
                    context.get("trip_choices", []),
                    default=str,
                ),
            })

            data = out.model_dump()
            data["intent"] = "unknown"

        else:
            out = bus_chain.invoke(base)
            data = out.model_dump()
            data["intent"] = (
                data.get("intent") or "unknown"
            ).lower()

        data["booking_type"] = "bus_booking"
        return data

    except Exception:
        logger.exception("Bus query interpretation failed")
        return _empty_result(query)


def frame_bus_message(query, context, backend_message):
    try:
        return bus_frame_chain.invoke({
            "query": query,
            "context": _ctx_view(context),
            "backend_message": backend_message,
        }).message

    except Exception:
        logger.exception("Bus response framing failed")
        return backend_message


def bus_answer_from_data(query, data):
    try:
        return bus_answer_chain.invoke({
            "query": query,
            "data": json.dumps(data, default=str),
        }).message

    except Exception:
        logger.exception("Bus answer chain failed")
        return "Here's what I found."


def handle_bus_general(query, source_city=None, destination_city=None):
    route = " to ".join(
        value
        for value in (source_city, destination_city)
        if value
    ) or "Not provided"

    try:
        return bus_general_chain.invoke({
            "query": query,
            "route": route,
        }).content

    except Exception:
        logger.exception("Bus general chain failed")
        return (
            "Hi! I can help you search buses, check seats, "
            "book tickets, view bookings and cancel tickets. "
            "Where would you like to travel?"
        )
