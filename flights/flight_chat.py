
from typing import Optional, List, Literal

from pydantic import BaseModel, Field

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate


llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
)


# --------------------------------------------------
# 1. Passenger schema
# --------------------------------------------------

class FlightPassenger(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[Literal["M", "F", "OTHER"]] = None
    seat_number: Optional[str] = None


# --------------------------------------------------
# 2. Flight filters
# --------------------------------------------------


class FlightFilters(BaseModel):
    origin_iata: Optional[str] = None
    destination_iata: Optional[str] = None
    flight_date: Optional[str] = None

    # Flight search preferences
    airline: Optional[str] = None
    departure_time: Optional[str] = None
    cabin_class: Optional[Literal[
        "ECONOMY",
        "BUSINESS",
        "FIRST",
    ]] = None

    # Booking information
    passenger_count: Optional[int] = None
    instance_id: Optional[int] = None

    seat_numbers: List[str] = Field(default_factory=list)
    passengers: List[FlightPassenger] = Field(default_factory=list)

    pnr: Optional[str] = None
    confirmed: Optional[bool] = None


# --------------------------------------------------
# 3. Flight intent and interpretation schemas
# --------------------------------------------------

FlightIntent = Literal[
    "search",
    "details",
    "seats",
    "book",
    "cancel",
    "my_bookings",
    "general",
    "unknown",
]


class FlightRouteDecision(BaseModel):
    intent: FlightIntent
    filters: FlightFilters = Field(
        default_factory=FlightFilters
    )


class FlightParseRoute(BaseModel):
    route: Literal["missing_fields", "full_parse"]
    confidence: float = Field(ge=0, le=1)


class FlightInterpretation(BaseModel):
    route: Literal["missing_fields", "full_parse"]
    intent: FlightIntent
    filters: FlightFilters = Field(
        default_factory=FlightFilters
    )


# --------------------------------------------------
# 4. Final message schema
# --------------------------------------------------

class FlightMessageSchema(BaseModel):
    message: str


# --------------------------------------------------
# 5. Route classification
# --------------------------------------------------

flight_route_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
        You decide how a flight booking assistant should
        interpret the latest user message.

        There are two possible routes:

        1. missing_fields:
           The user is answering one of the currently pending
           flight booking questions.

        2. full_parse:
           The user is starting a new flight request,
           changing the booking intent, or asking a separate
           flight-related question.

        Consider:
        - The latest user message.
        - The pending fields.
        - Previously collected flight information.
        - The current flight workflow stage.

        Examples:

        Pending field: flight_date
        User: Tomorrow
        Route: missing_fields

        Pending field: origin_iata
        User: VJD
        Route: missing_fields

        Pending field: seat_numbers
        User: 12A and 12B
        Route: missing_fields

        Pending field: passengers
        User: Book a flight from BLR to DEL
        Route: full_parse

        Pending field: flight_date
        User: Show my bookings
        Route: full_parse

        Pending field: pnr
        User: I want to search for flights to Mumbai
        Route: full_parse

        Important:
        - Do not classify every short message as a missing-field response.
        - If the user clearly starts a different flight workflow,
          choose full_parse.
        - If the message clearly answers a pending field,
          choose missing_fields.
        - Do not invent information.
        IMPORTANT CONVERSATION HANDLING:

        - A user may ask a question about a pending field instead
        of immediately providing its value.

        - If the pending field is passengers and the user asks:
        "What details are required for each passenger?"
        "What information should I provide?"
        "What details do you need?"
        these are informational questions about the current
        booking workflow, NOT passenger data.

        - Do not interpret these questions as passenger details.
        - Do not invent passenger names, ages or genders.
        - Do not discard the pending passenger requirement.
        - The booking assistant should explain the required fields
        and then allow the user to continue the same booking.

        - Distinguish between:
        1. Answering a pending field.
        2. Asking for clarification about a pending field.
        3. Starting a different workflow.
        """

        
    ),
    (
        "human",
        """
        Current flight workflow stage:
        {stage}

        Pending fields:
        {required_fields}

        Previously collected flight information:
        {context}

        Available flight options:
        {options}

        Latest user message:
        {query}

        Decide the appropriate route.
        """
    ),
])

flight_route_chain = (
    flight_route_prompt
    | llm.with_structured_output(FlightParseRoute)
)


# --------------------------------------------------
# 6. Full flight interpretation
# --------------------------------------------------

FLIGHT_SYSTEM_PROMPT = """
You are an assistant for a flight booking platform.

Extract information from the user's latest message.

Supported intents:
- search: Search flights between two airports.
- details: Get details about a selected flight.
- seats: View or select flight seats.
- book: Book a flight.
- cancel: Cancel an existing flight booking.
- my_bookings: Show the user's bookings.
- general: General flight-related questions.
- unknown: The intent cannot be confidently identified.

Rules:

1. Extract airport codes when explicitly provided.
2. Convert airport city names into standard IATA codes
   only when confident about the mapping.
3. Never invent travel dates, passenger details, seat numbers,
   flight IDs or PNRs.
4. Do not assume that a user has confirmed a booking merely
   because they express interest in booking.
5. If the user says tomorrow or today, use the current date
   supplied in the context.
6. Preserve previously collected information unless the user
   explicitly changes it.
7. Extract passenger information only when supplied.
8. Gender must be M, F or OTHER when explicitly provided.
9. Normalize cabin class to ECONOMY, BUSINESS or FIRST.
10. If the user selects a flight using an option number (e.g. 1, 2, "first flight"),
    flight number (e.g. "6E-206", "AI-101") or airline name,
    resolve its instance_id using the available flight options.
11. Never invent an instance_id.
12. A cancellation request is not cancellation confirmation.
13. Set confirmed=true only when the user clearly confirms
    the pending booking or cancellation.
14. If the user's message is unrelated to flight booking,
    choose general.
15. If the intent cannot be identified, choose unknown.

16. Extract airline names when explicitly provided.
17. Extract departure time when explicitly provided.
18. Normalize departure time to 24-hour HH:MM format.
19. Extract passenger_count from expressions such as
    "2 tickets", "three passengers", or "1 ticket".
20. Never assume passenger_count if the user does not
    explicitly provide it.
21. Preserve airline, departure_time, passenger_count
    and cabin_class across subsequent messages.

"""


flight_interpret_prompt = ChatPromptTemplate.from_messages([
    ("system", FLIGHT_SYSTEM_PROMPT),
    (
        "human",
        """
        Current date: {current_date}

        Previously collected flight information:
        {context}

        Available flight options:
        {options}

        User message:
        {query}

        Extract the intent and information.
        """
    ),
])

flight_interpret_chain = (
    flight_interpret_prompt
    | llm.with_structured_output(FlightRouteDecision)
)


# --------------------------------------------------
# 7. Missing-field interpretation
# --------------------------------------------------

flight_missing_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
        You extract information for currently pending fields
        in a flight booking conversation.

        Extract ONLY information clearly supplied in the
        latest user message.

        Rules:

        1. Do not extract fields that are not pending.
        2. Do not overwrite existing information unless the
           user explicitly provides a replacement.
        3. Never invent airport codes, dates, passenger details,
           seat numbers or PNRs.
        4. Resolve "today", "tomorrow" and relative dates using
           the supplied current date.
        5. Resolve flight option numbers, flight numbers (e.g. "6E-206"), or airlines
           using available flight options to find instance_id.
        6. Extract passenger details only when explicitly provided.
        7. For confirmation, use confirmed=true only for a clear
           affirmative response to the pending confirmation.
        8. If the message does not provide a pending field,
           leave that field empty.
        
        9. A cabin class such as Economy, Business or First
        may be provided even when another booking field
        is currently pending, if cabin_class is included
        in the pending fields.

        10. Extract passenger_count, airline and departure_time
            only when those fields are pending.

        11. Never discard previously collected booking details.

        """
    ),
    (
        "human",
        """
        Current date:
        {current_date}

        Pending fields:
        {required_fields}

        Previously collected flight information:
        {context}

        Available flight options:
        {options}

        Latest user message:
        {query}

        Extract only the pending information.
        """
    ),
])

flight_missing_chain = (
    flight_missing_prompt
    | llm.with_structured_output(FlightFilters)
)


# --------------------------------------------------
# 8. Interpret user query
# --------------------------------------------------

def interpret_flight_query(
    query,
    context=None,
    current_date=None,
    options=None,
    required_fields=None,
    stage="START",
):
    context = context or {}
    options = options or []
    required_fields = required_fields or []

    base_input = {
        "query": query,
        "context": context,
        "current_date": str(current_date),
        "options": options,
    }

    # ----------------------------------------------
    # No pending fields: full parse
    # ----------------------------------------------

    if not required_fields:
        parsed = flight_interpret_chain.invoke(base_input)

        return FlightInterpretation(
            route="full_parse",
            intent=parsed.intent,
            filters=parsed.filters,
        )

    # ----------------------------------------------
    # Pending fields: classify the latest message
    # ----------------------------------------------

    route_result = flight_route_chain.invoke({
        **base_input,
        "required_fields": required_fields,
        "stage": stage,
    })

    route = (
        route_result.route
        if route_result.confidence >= 0.70
        else "full_parse"
    )

    # ----------------------------------------------
    # Missing-field parsing
    # ----------------------------------------------

    if route == "missing_fields":
        filters = flight_missing_chain.invoke({
            **base_input,
            "required_fields": required_fields,
        })

        return FlightInterpretation(
            route="missing_fields",
            intent=context.get("intent", "unknown"),
            filters=filters,
        )

    # ----------------------------------------------
    # Full parsing
    # ----------------------------------------------

    parsed = flight_interpret_chain.invoke(base_input)

    return FlightInterpretation(
        route="full_parse",
        intent=parsed.intent,
        filters=parsed.filters,
    )


# --------------------------------------------------
# 9. Missing field definitions
# --------------------------------------------------


FLIGHT_FIELDS = {
    "origin_iata": "departure airport",
    "destination_iata": "destination airport",
    "flight_date": "travel date",
    "airline": "preferred airline",
    "departure_time": "preferred departure time",
    "cabin_class": "cabin class",
    "passenger_count": "number of passengers",
    "instance_id": "flight selection",
    "seat_numbers": "seat selection",
    "passengers": "passenger details",
    "pnr": "booking PNR",
}



def get_missing_flight_fields(data, required_fields):
    missing = []

    for field in required_fields:
        value = data.get(field)

        if field in ["passengers", "seat_numbers"]:
            if not value:
                missing.append(field)

        elif value is None or value == "":
            missing.append(field)

    return missing


# --------------------------------------------------
# 10. Frame missing information question
# --------------------------------------------------

def frame_flight_message(required_fields, context=None):
    questions = {
        "origin_iata": "Which airport are you departing from?",
        "destination_iata": "Which airport are you travelling to?",
        "flight_date": "What date would you like to travel?",
        "instance_id": "Which flight would you like to select?",
        "seat_numbers": "Which seats would you like to select?",
        "passengers": (
            "Please provide the name, age and gender "
            "of each passenger."
        ),
        "pnr": "Please provide your flight PNR.",
    }

    if not required_fields:
        return "How can I help you with your flight booking?"

    field = required_fields[0]

    return questions.get(
        field,
        "Could you provide a little more information?"
    )


# --------------------------------------------------
# 11. Generate answer from API data
# --------------------------------------------------

def flight_answer_from_data(query, data):
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            """
            You are a helpful flight booking assistant.

            Answer the user's question using only the supplied
            flight information.

            Never invent prices, flight numbers, timings,
            availability or booking status.

            Keep the answer concise and clear.
            """
        ),
        (
            "human",
            """
            User question:
            {query}

            Flight information:
            {data}
            """
        ),
    ])

    chain = prompt | llm.with_structured_output(
        FlightMessageSchema
    )

    result = chain.invoke({
        "query": query,
        "data": data,
    })

    return result.message


# --------------------------------------------------
# 12. General flight-related questions
# --------------------------------------------------

def handle_flight_general(query):
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            """
            You are a helpful flight booking assistant.

            Answer general questions about flights, airports,
            cabin classes, baggage concepts and travel.

            Do not claim to have searched or booked a flight
            unless the backend has actually done so.

            For a booking request, guide the user to provide
            departure city, destination and travel date.
            """
        ),
        ("human", "{query}"),
    ])

    result = (
        prompt
        | llm.with_structured_output(FlightMessageSchema)
    ).invoke({"query": query})

    return result.message
