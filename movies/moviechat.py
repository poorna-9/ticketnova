"""
Movie booking AI layer: pydantic schemas + prompts + chains.
Mirrors the structure of ai/chatcric.py (normal / missing / route / frame / answer chains).
"""
import json
import logging
from typing import List, Literal, Optional

from django.conf import settings
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
from ai_observability import (
    log_ai_event,
    start_timer,
    elapsed_ms,
)
# =====================================================================
# 1. PYDANTIC SCHEMAS
# =====================================================================

MOVIE_INTENTS = Literal[
    "search_movies",   # what's playing / list movies
    "show_showtimes",  # timings / theaters for a movie
    "show_seats",      # seat availability for a show
    "book",            # reserve seats + pay
    "cancel_booking",  # cancel + refund
    "my_bookings",     # booking history / status
    "movie_info",      # duration, rating, language, genre of a movie
    "general",         # hi / thanks / what can you do
    "unknown",
    "",
]


class MovieFilters(BaseModel):
    movie_title: str = ""
    language: str = ""          # full English name: Hindi, English, Tamil ...
    genre: str = ""
    city: str = ""
    area: str = ""
    theater_name: str = ""
    date: str = ""              # resolved to YYYY-MM-DD (we give the LLM today's date)
    show_time: str = ""         # 24h "HH:MM" only when an explicit clock time is given
    time_shift: str = ""        # morning / afternoon / evening / night
    seat_count: str = ""        # digits as string
    seat_labels: List[str] = Field(default_factory=list)   # ["B4", "B5"]
    tier: str = ""              # CLASSIC / PRIME / RECLINER
    max_price: str = ""         # numeric string, per seat
    price_semantic: str = ""    # cheaper / expensive
    booking_code: str = ""      # 8 char code, for cancel / status
    screen: str = ""


class MovieQuerySchema(BaseModel):
    booking_type: Literal["movie_booking"]
    intent: MOVIE_INTENTS
    query_text: str
    filters: MovieFilters


class MovieRouteDecision(BaseModel):
    route: Literal["missing_fields", "full_parse"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class MovieMessageSchema(BaseModel):
    message: str


movie_parser = PydanticOutputParser(pydantic_object=MovieQuerySchema)
movie_route_parser = PydanticOutputParser(pydantic_object=MovieRouteDecision)
movie_message_parser = PydanticOutputParser(pydantic_object=MovieMessageSchema)

# =====================================================================
# 2. LLMs
# =====================================================================

movie_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1, openai_api_key=settings.OPENAI_API_KEY)
movie_route_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, openai_api_key=settings.OPENAI_API_KEY)
movie_answer_llm = ChatOpenAI(
    model="gpt-4o-mini", temperature=0.1, max_tokens=220, openai_api_key=settings.OPENAI_API_KEY
)
movie_general_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.4, openai_api_key=settings.OPENAI_API_KEY)

# =====================================================================
# 3. MAIN PROMPT  (full parse of a fresh / changed request)
# =====================================================================

movie_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a STRICT JSON generator for a MOVIE TICKET booking system.

CRITICAL RULES:
- Output ONLY valid JSON. No explanations, no questions, no extra keys.
- booking_type MUST be "movie_booking".
- Missing / unknown values MUST be empty strings "" (seat_labels: empty list []).
- NEVER guess. Only output what the user's CURRENT message states or changes.
- query_text MUST be the original user message, unchanged.
- ALL extracted values go inside "filters".

TODAY: {today} ({weekday}).

CURRENT KNOWN CONTEXT (use ONLY to resolve references like "same theater", "make it 3 seats",
"tomorrow instead". Do NOT copy these values into the output unless the user restates or changes them):
{context}

INTENT RULES:
- "search_movies"  -> what's playing, list/show movies, movies in a city, hindi movies, action movies
- "show_showtimes" -> showtimes, timings, which theaters, when is <movie> playing
- "show_seats"     -> seat availability, which seats are free, seat map
- "book"           -> wants tickets / seats / to book / to watch a movie at a time
- "cancel_booking" -> cancel a booking / ticket / refund
- "my_bookings"    -> my tickets, my bookings, booking history, booking status
- "movie_info"     -> duration, how long, certification/rating (UA/A), language, genre of a specific movie
- "general"        -> hi, hello, thanks, what can you do
- "unknown"        -> unclear

DATE RULES:
- Resolve dates to ISO format YYYY-MM-DD using TODAY above.
- "today" -> today, "tomorrow" -> today+1, "day after tomorrow" -> today+2.
- Resolve weekday names and explicit dates correctly.
- NEVER assume today as the booking date.
- If the CURRENT USER MESSAGE does not mention a date or refer to an existing date, filters.date MUST be "".
- Do not copy a date from CURRENT KNOWN CONTEXT automatically.
- "same date" or "keep the date" may reuse the existing date.
- If no date is provided, leave filters.date as "".

TIME RULES:
- show_time = 24h "HH:MM" ONLY if a clock time is given ("6:30 pm" -> "18:30", "7 pm" -> "19:00").
  A bare hour with a shift ("7 evening") -> "19:00". A bare hour with no shift and no am/pm: leave "".
- time_shift = morning / afternoon / evening / night when mentioned ("night show", "matinee" -> afternoon).

MOVIE / THEATER RULES:
- movie_title = a film title. theater_name = a cinema / multiplex (PVR, INOX, Cinepolis, Carnival,
  or a venue name). Chain names alone (e.g. "PVR") are theater_name.
- Location words: a city goes to "city", a locality/neighbourhood goes to "area". Neither is a theater name.
- language: full English name, capitalised (Hindi, English, Tamil, Telugu, Kannada, Malayalam ...).
- genre: single word capitalised (Action, Comedy, Sci-Fi, Thriller ...).

SEAT RULES:
- seat_count: digits as a string ("two tickets" -> "2", "a couple" -> "2", "family of four" -> "4").
- seat_labels: explicit labels only, uppercase ("b4 and b5" -> ["B4","B5"]). Also set seat_count? NO, leave it.
- tier: "recliner(s)" -> RECLINER, "classic"/"economy"/"regular" -> CLASSIC, "prime"/"premium" -> PRIME.
- max_price: numeric per-seat budget ("under 300 each" -> "300"). Do NOT divide totals.
- price_semantic: "cheap/cheapest" -> "cheaper"; "expensive/premium price" -> "expensive".

BOOKING CODE:
- booking_code = the 8-character code the user quotes for a booking ("cancel 9F2C41AB" -> "9F2C41AB").

SCREEN RULES:
- If the user says "screen 3", "screen number 3", etc.,
  extract it into the screen field.
- Never interpret a screen identifier as a theater_name.
- theater_name must contain an actual theater or cinema name.


{format_instructions}
"""),
    ("human", "{query}"),
]).partial(format_instructions=movie_parser.get_format_instructions())

movie_chain = movie_prompt | movie_llm | movie_parser

# =====================================================================
# 4. MISSING-FIELDS PROMPT  (user is answering a question we asked)
# =====================================================================

movie_missing_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a STRICT JSON generator for a MOVIE TICKET booking system.

The system just asked the user for these missing fields:
{required_fields}

RULES (VERY IMPORTANT):
- Output the FULL JSON schema.
- Fill the missing fields ONLY IF the reply clearly contains them.
- You MAY also extract extra context if explicitly present: city, area, language, date, show_time, seat_count.
- NEVER populate or extract movie_title unless "movie_title" is explicitly listed in {required_fields}. Theater names, showtimes, and cities (such as "Aparna Cinemas at 6pm") are NEVER movie titles! Leave movie_title as "".
- If required_fields is "theater_name", the user's reply is the theater name (e.g. "Aparna Cinemas at 6pm" -> theater_name="Aparna Cinemas", show_time="18:00", movie_title="").
- If a field is not clearly present, leave it "" (seat_labels: []).
- Do NOT infer or change intent. Always output intent "unknown". booking_type MUST be "movie_booking".
- Do NOT hallucinate.

TODAY: {today} ({weekday}).
Date rules:
- Resolve explicit date expressions to YYYY-MM-DD.
- "tomorrow" -> today+1, "today" -> today.
- NEVER assume today when the user does not mention a date.
- If no date is provided, leave date as "".
- Do not invent or copy a date automatically from CURRENT CONTEXT.

INTERPRETING SHORT REPLIES BY WHAT WAS ASKED:
- required "movie_title": the reply (minus any city/language words) IS the title, even if it looks unusual.
- required "theater_name": the reply (minus city/area) IS the theater name. "PVR" alone is a valid theater_name.
- required "city": the reply is a city.
- required "date": a date expression -> YYYY-MM-DD.
- required "show_time": a clock time -> "HH:MM" (24h); a word like "evening" -> time_shift.
- required "seat_count": a number or number word -> digits string.
- required "seat_labels": labels like "B4, B5" -> ["B4","B5"].
- required "language": a language name.
- required "booking_code": an 8-character code.

KNOWN CITIES (use to decide city vs area; accept common aliases such as Bombay=Mumbai, Bengaluru=Bangalore):
{known_cities}

COMMA-SEPARATED REPLIES:
- The 1st segment is the field currently being asked for. Classify the remaining segments:
  a KNOWN CITY -> "city", otherwise a locality -> "area".
  Example (required theater_name): "PVR Phoenix, Mumbai" -> theater_name="PVR Phoenix", city="Mumbai".
- NEVER leave commas or location words inside theater_name or movie_title.

CURRENT CONTEXT (reference only):
{context}

{format_instructions}
"""),
    ("human", "{query}"),
]).partial(format_instructions=movie_parser.get_format_instructions())

movie_missing_chain = movie_missing_prompt | movie_llm | movie_parser

# =====================================================================
# 5. ROUTE PROMPT  (is the reply answering our question, or a new request?)
# =====================================================================

movie_route_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You decide whether the user's reply is:
1) answering the fields the system just asked for ("missing_fields"), or
2) changing the request / starting something new ("full_parse").

Rules:
- Output JSON with keys: route ("missing_fields" OR "full_parse") and confidence (0 to 1).
- Reply only supplies values for the missing fields (possibly with city/area/date/time) -> "missing_fields".
- Reply introduces a new action or intent ("show me hindi movies instead", "cancel my booking",
  "what's playing tomorrow", "actually book for a different movie") -> "full_parse".
- A short noun phrase like "PVR Phoenix", "tomorrow", "2", "Interstellar" when the system asked
  for exactly that kind of field -> "missing_fields".
- STRICT JSON ONLY.

{format_instructions}
"""),
    ("human", """
Missing fields: {required_fields}
User reply: {query}
"""),
]).partial(format_instructions=movie_route_parser.get_format_instructions())

movie_route_chain = movie_route_prompt | movie_route_llm | movie_route_parser

# =====================================================================
# 6. RESPONSE FRAMING  (backend instruction -> natural message)
# =====================================================================

movie_frame_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a friendly movie ticket booking assistant.
- Rewrite the backend instruction as one natural chatbot message.
- Keep EVERY option / number / name that appears in the instruction.
- Do NOT change the meaning. Do NOT invent details. Do NOT ask extra questions.
- Return ONLY valid JSON with one key: message.

{format_instructions}
"""),
    ("human", """
User query:
{query}

Current booking context:
{context}

Backend instruction:
{backend_message}
"""),
]).partial(format_instructions=movie_message_parser.get_format_instructions())

movie_frame_chain = movie_frame_prompt | movie_llm | movie_message_parser

movie_answer_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a movie ticket booking assistant. Answer the user's question using ONLY the data below.
Do NOT invent movies, theaters, prices or statuses. If the data says nothing was found, say so naturally.

LENGTH RULES:
- ONE item (a movie or a booking): answer in detail with the fields relevant to the question.
- MANY items: summarise (counts by status, which is upcoming) - never list every field of every item.
- Under about 120 words. Never dump raw JSON or field names.

Return ONLY valid JSON with one key: message.

{format_instructions}
"""),
    ("human", """
User query:
{query}

Data (JSON):
{data}
"""),
]).partial(format_instructions=movie_message_parser.get_format_instructions())

movie_answer_chain = movie_answer_prompt | movie_answer_llm | movie_message_parser

movie_general_prompt = ChatPromptTemplate.from_messages([
    ("system", """
You are a friendly movie ticket booking assistant.
For greetings / thanks / "what can you do": be warm and concise. You can help users:
find movies, see showtimes and theaters, check seat availability, book tickets and pay,
view their bookings, and cancel a booking with a refund.
If a city is known, use it naturally and never ask for it again. Guide the user to a next step.
"""),
    ("human", "User Query: {query}\nKnown City: {city}"),
])
movie_general_chain = movie_general_prompt | movie_general_llm

# =====================================================================
# 7. PUBLIC FUNCTIONS
# =====================================================================

CONTEXT_KEYS_FOR_PROMPT = (
    "intent", "movie_title", "language", "genre", "city", "area", "date",
    "theater_name", "show_time", "time_shift", "seat_count", "seat_labels", "tier",
)


def _ctx_view(context):
    return json.dumps({k: context.get(k) for k in CONTEXT_KEYS_FOR_PROMPT if context.get(k)}, default=str)


def _empty_result(query):
    return {
        "booking_type": "movie_booking",
        "intent": "unknown",
        "query_text": query,
        "filters": MovieFilters().dict(),
    }



def interpret_movie_query(query, required_fields, context, today, known_cities):
    """
    Returns structured movie intent and filters.
    Logs route classification and structured extraction separately.
    """

    weekday = today.strftime("%A")

    base = {
        "query": query,
        "today": today.isoformat(),
        "weekday": weekday,
        "context": _ctx_view(context),
    }

    route = "full_parse"

    # --------------------------------------------------
    # 1. Route classification
    # --------------------------------------------------

    if required_fields:
        route_start = start_timer()

        try:
            decision = movie_route_chain.invoke({
                "required_fields": ", ".join(required_fields),
                "query": query,
            })

            if decision.confidence >= 0.7:
                route = decision.route

            log_ai_event(
                stage="movie_ai_route_classification",
                input_data={
                    "query_length": len(query),
                    "required_fields": required_fields,
                },
                output_data={
                    "route": route,
                    "confidence": decision.confidence,
                },
                model="gpt-4o-mini",
                latency_ms=elapsed_ms(route_start),
                status="success",
                metadata={
                    "domain": "movie",
                    "prompt": "movie_route_prompt",
                },
            )

        except Exception as exc:
            log_ai_event(
                stage="movie_ai_route_classification",
                input_data={
                    "required_fields": required_fields,
                },
                latency_ms=elapsed_ms(route_start),
                status="failed",
                error=exc,
                model="gpt-4o-mini",
                metadata={
                    "domain": "movie",
                },
            )

            logger.exception("Movie route classification failed")

    # --------------------------------------------------
    # 2. Structured extraction
    # --------------------------------------------------

    parse_start = start_timer()

    try:
        if route == "missing_fields":
            out = movie_missing_chain.invoke({
                **base,
                "required_fields": ", ".join(required_fields),
                "known_cities": ", ".join(known_cities) or "none",
            })

            data = out.dict()
            data["intent"] = "unknown"

            # SAFETY GUARD: If movie_title was not requested in required_fields,
            # never allow missing_fields parser to extract or overwrite movie_title.
            if "movie_title" not in required_fields and "filters" in data:
                data["filters"]["movie_title"] = ""

            prompt_name = "movie_missing_prompt"

        else:
            out = movie_chain.invoke(base)

            data = out.dict()
            data["intent"] = (
                data.get("intent") or "unknown"
            ).lower()

            prompt_name = "movie_prompt"

        data["booking_type"] = "movie_booking"
        data["_route"] = route

        log_ai_event(
            stage="movie_ai_structured_extraction",
            input_data={
                "query_length": len(query),
                "required_fields": required_fields,
                "route": route,
            },
            output_data={
                "intent": data.get("intent"),
                "extracted_field_names": list(
                    (data.get("filters") or {}).keys()
                ),
                "route": route,
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(parse_start),
            status="success",
            metadata={
                "domain": "movie",
                "prompt": prompt_name,
            },
        )

        return data

    except Exception as exc:
        log_ai_event(
            stage="movie_ai_structured_extraction",
            input_data={
                "query_length": len(query),
                "required_fields": required_fields,
                "route": route,
            },
            latency_ms=elapsed_ms(parse_start),
            status="failed",
            error=exc,
            model="gpt-4o-mini",
            metadata={
                "domain": "movie",
            },
        )

        logger.exception("Movie LangChain extraction failed")

        data = _empty_result(query)
        data["_route"] = route

        return data


def frame_movie_message(query, context, backend_message):
    timer = start_timer()

    try:
        result = movie_frame_chain.invoke({
            "query": query,
            "context": _ctx_view(context),
            "backend_message": backend_message,
        })

        message = result.message

        log_ai_event(
            stage="movie_ai_response_framing",
            input_data={
                "query_length": len(query),
            },
            output_data={
                "message_length": len(message),
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(timer),
            status="success",
            metadata={
                "domain": "movie",
                "prompt": "movie_frame_prompt",
            },
        )

        return message

    except Exception as exc:
        log_ai_event(
            stage="movie_ai_response_framing",
            latency_ms=elapsed_ms(timer),
            status="failed",
            error=exc,
            model="gpt-4o-mini",
            metadata={
                "domain": "movie",
            },
        )

        return backend_message




def movie_answer_from_data(query, data):
    timer = start_timer()

    try:
        result = movie_answer_chain.invoke({
            "query": query,
            "data": json.dumps(data, default=str),
        })

        message = result.message

        log_ai_event(
            stage="movie_ai_answer_generation",
            input_data={
                "query_length": len(query),
                "data_field_names": (
                    list(data.keys())
                    if isinstance(data, dict)
                    else []
                ),
            },
            output_data={
                "message_length": len(message),
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(timer),
            status="success",
            metadata={
                "domain": "movie",
                "prompt": "movie_answer_prompt",
            },
        )

        return message

    except Exception as exc:
        log_ai_event(
            stage="movie_ai_answer_generation",
            latency_ms=elapsed_ms(timer),
            status="failed",
            error=exc,
            model="gpt-4o-mini",
            metadata={
                "domain": "movie",
            },
        )

        logger.exception("Movie answer chain failed")
        return "Here's what I found."




def handle_movie_general(query, city=None):
    timer = start_timer()

    try:
        result = movie_general_chain.invoke({
            "query": query,
            "city": city or "Not provided",
        })

        message = result.content

        log_ai_event(
            stage="movie_ai_general_response",
            input_data={
                "query_length": len(query),
                "city_provided": bool(city),
            },
            output_data={
                "message_length": len(message),
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(timer),
            status="success",
            metadata={
                "domain": "movie",
                "prompt": "movie_general_prompt",
            },
        )

        return message

    except Exception as exc:
        log_ai_event(
            stage="movie_ai_general_response",
            latency_ms=elapsed_ms(timer),
            status="failed",
            error=exc,
            model="gpt-4o-mini",
            metadata={
                "domain": "movie",
            },
        )

        logger.exception("Movie general chain failed")

        return (
            "Hi! I can help you find movies, check showtimes and seats, "
            "book tickets, or cancel a booking. What would you like to do?"
        )
