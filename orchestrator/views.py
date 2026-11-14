"""
TicketNova orchestrator — fixed.

Changes are marked with "FIX". Everything else is unchanged from the original.

FIX 1: the domain router is now told which domain the conversation is already in,
       and told to use it when a follow-up does not name a mode.
FIX 2: with no pending fields, an "ambiguous" classification now falls back to the
       active domain instead of returning the dead-end ambiguous response.
FIX 3: with pending fields, an "ambiguous" classification now continues with the
       active domain instead of returning the dead-end ambiguous response.
FIX 4: agent_required_fields is initialised once, so no branch can leave it unbound.
"""

from typing import Literal

from pydantic import BaseModel

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from movies.movie_agent import handle_movie_query
from buses.bus_agent import handle_bus_query
from flights.flight_agent import handle_flight_query

from ai_observability import (
    trace_scope,
    log_ai_event,
    start_timer,
    elapsed_ms,
)

# ==================================================
# 1. DOMAIN DEFINITIONS
# ==================================================

Domain = Literal["movie", "bus", "flight"]


class DomainDecision(BaseModel):
    domain: Literal[
        "movie",
        "bus",
        "flight",
        "greeting",
        "general",
        "ambiguous",
    ]


class ConversationDecision(BaseModel):
    action: Literal[
        "continue",
        "new_request",
        "greeting",
        "general",
        "ambiguous",
    ]


router_llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
)


# ==================================================
# 2. DOMAIN ROUTER
# ==================================================

domain_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
        You are the domain router for TicketNova.

        TicketNova supports:

        1. Movie ticket booking
        2. Bus ticket booking
        3. Flight ticket booking

        Classify the user's latest message.

        movie:
        Movie search, showtimes, seat selection, booking,
        cancellation and booking lookup.

        bus:
        Bus search, trip details, seat selection, booking,
        cancellation and booking lookup.

        flight:
        Flight search, flight details, seat selection,
        booking, cancellation and booking lookup.

        greeting:
        Simple greetings such as hi, hello, good morning.

        general:
        Questions unrelated to TicketNova's supported
        movie, bus and flight services.

        ambiguous:
        Messages that cannot confidently be classified.

        PREVIOUS DOMAIN IN THIS CONVERSATION:
        {previous_domain}

        FOLLOW-UP RULE:

        A follow-up does not have to name its mode. Messages like
        "book 1 ticket for me", "book it", "one seat", "the first
        one", "show seats", "cancel it", "where is my ticket" are
        follow-ups to whatever was being discussed.

        If {previous_domain} is one of movie, bus or flight, classify
        such a message as {previous_domain}, even when the message
        contains no keyword naming that mode.

        Only answer ambiguous when no domain can be inferred even
        after taking {previous_domain} into account.

        Do not extract booking details.
        Do not answer the user.
        Return only the appropriate classification.
        """
    ),
    ("human", "{query}"),
])

domain_router = (
    domain_prompt
    | router_llm.with_structured_output(DomainDecision)
)


def identify_domain(query, previous_domain=None):
    # FIX 1: the router now receives the active domain as context.
    start = start_timer()

    try:
        result = domain_router.invoke({
            "query": query,
            "previous_domain": previous_domain or "none",
        })

        log_ai_event(
            stage="orchestrator_domain_router",
            input_data={
                "query": query,
                "previous_domain": previous_domain,
            },
            output_data={
                "domain": result.domain,
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(start),
            status="success",
            metadata={
                "prompt_version": "domain_router_v2",
            },
        )

        return result.domain

    except Exception as exc:

        log_ai_event(
            stage="orchestrator_domain_router",
            input_data={
                "query": query,
                "previous_domain": previous_domain,
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(start),
            status="failed",
            error=exc,
        )

        raise


# ==================================================
# 3. CONVERSATION ROUTER
# ==================================================

conversation_prompt = ChatPromptTemplate.from_messages([
    (
        "system",
        """
        You are the conversation router for TicketNova.

        Your task is to determine whether the user's latest
        message answers a previously requested field or
        represents something different.

        The previous domain is the agent that handled the
        previous request.

        The previous agent was waiting for these fields:
        {required_fields}

        Previous domain:
        {previous_domain}

        Decide between:

        1. continue
        2. new_request
        3. greeting
        4. general
        5. ambiguous

        CONTINUE:

        Select continue ONLY when the latest message is
        a plausible answer to one or more pending fields.

        Examples:

        Pending field: departure_time
        Message: 7 PM
        Decision: continue

        Pending field: flight_date
        Message: Tomorrow
        Decision: continue

        Pending field: movie_title
        Message: Interstellar
        Decision: continue

        Pending field: seat_count
        Message: 3
        Decision: continue

        Pending field: city
        Message: Hyderabad
        Decision: continue

        NEW_REQUEST:

        Select new_request when the user starts a different
        request or asks something that is not an answer
        to the pending fields.

        Examples:

        Pending field: departure_time
        Message: Book a flight from VJD to HYD
        Decision: new_request

        Pending field: flight_date
        Message: Book a bus to Chennai
        Decision: new_request

        Pending field: movie_title
        Message: Show me Hindi movies instead
        Decision: new_request

        Pending field: departure_time
        Message: Cancel my booking
        Decision: new_request

        GREETING:

        If the user says hi, hello, good morning, etc.,
        select greeting.

        GENERAL:

        If the user asks something unrelated to TicketNova's
        movie, bus and flight services, select general.

        AMBIGUOUS:

        If you cannot confidently determine whether the
        message answers a pending field or starts something
        different, select ambiguous.

        IMPORTANT RULES:

        - Do not assume every short message is a field answer.
        - Consider the meaning of the pending fields.
        - A clear new booking request must be new_request.
        - A greeting must be greeting.
        - An unrelated question must be general.
        - When uncertain, prefer ambiguous over an incorrect
          continuation.
        """
    ),
    (
        "human",
        """
        Latest user message:

        {query}

        Classify this message.
        """
    ),
])

conversation_router = (
    conversation_prompt
    | router_llm.with_structured_output(ConversationDecision)
)


def identify_conversation_action(
    query,
    required_fields,
    previous_domain,
):

    start = start_timer()

    try:
        result = conversation_router.invoke({
            "query": query,
            "required_fields": required_fields,
            "previous_domain": previous_domain,
        })

        log_ai_event(
            stage="orchestrator_conversation_router",
            input_data={
                "query": query,
                "required_fields": required_fields,
                "previous_domain": previous_domain,
            },
            output_data={
                "action": result.action,
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(start),
            status="success",
            metadata={
                "prompt_version": "conversation_router_v1",
            },
        )

        return result.action

    except Exception as exc:

        log_ai_event(
            stage="orchestrator_conversation_router",
            input_data={
                "query": query,
                "required_fields": required_fields,
                "previous_domain": previous_domain,
            },
            model="gpt-4o-mini",
            latency_ms=elapsed_ms(start),
            status="failed",
            error=exc,
        )

        raise


# ==================================================
# 4. DOMAIN TO AGENT MAPPING
# ==================================================

AGENTS = {
    "movie": handle_movie_query,
    "bus": handle_bus_query,
    "flight": handle_flight_query,
}


def log_routing_decision(
    query,
    domain,
    action,
    decision,
    previous_domain=None,
    required_fields=None,
):
    log_ai_event(
        stage="orchestrator_routing_decision",
        input_data={
            "query": query,
            "previous_domain": previous_domain,
            "required_fields": required_fields,
        },
        output_data={
            "selected_domain": domain,
            "conversation_action": action,
            "decision": decision,
        },
    )


# ==================================================
# 5. RESPONSE HELPERS
# ==================================================

def greeting_response():

    return {
        "message": (
            "Hello! Welcome to TicketNova. "
            "I can help you search and book movie, bus "
            "and flight tickets. How can I help you today?"
        ),
        "required_fields": [],
        "intent": "greeting",
        "intent_done": True,
        "options": [],
        "data": {},
        "action": None,
        "payment": {},
    }


def general_response():

    return {
        "message": (
            "I'm here to help with TicketNova's services. "
            "You can ask me to search movies and showtimes, "
            "find buses and flights, select seats, book tickets, "
            "or manage your existing bookings."
        ),
        "required_fields": [],
        "intent": "general",
        "intent_done": True,
        "options": [],
        "data": {},
        "action": None,
        "payment": {},
    }


def ambiguous_response():

    return {
        "message": (
            "I'm not sure what you mean. Could you please "
            "clarify your request?"
        ),
        "required_fields": [],
        "intent": "ambiguous",
        "intent_done": False,
        "options": [],
        "data": {},
        "action": None,
        "payment": {},
    }


# ==================================================
# 6. ORCHESTRATOR MEMORY
# ==================================================

STATE_KEY = "ticketnova_chat_state"


def get_chat_state(request):

    state = request.session.get(
        STATE_KEY,
        {
            "active_domain": None,
        },
    )

    return state


def save_chat_state(request, domain):

    request.session[STATE_KEY] = {
        "active_domain": domain,
    }

    request.session.modified = True


# ==================================================
# 7. MAIN ORCHESTRATION
# ==================================================

def is_flight_cabin_class_answer(query):
    return query.strip().lower() in {
        "economy",
        "economy class",
        "business",
        "business class",
        "first",
        "first class",
    }


def orchestrate_query(
    request,
    query,
    required_fields=None,
):
    state = get_chat_state(request)

    previous_domain = state.get("active_domain")

    log_ai_event(
        stage="orchestrator_state",
        input_data={
            "query": query,
            "required_fields": required_fields,
        },
        output_data={
            "previous_domain": previous_domain,
        },
    )

    # FIX 4: always bound, so no branch below can raise UnboundLocalError.
    agent_required_fields = []
    action = "new_request"

    # --------------------------------------------------
    # CASE 1: No pending fields
    # --------------------------------------------------

    if not required_fields:

        domain = identify_domain(query, previous_domain)

        if domain == "greeting":
            log_routing_decision(
                query=query,
                domain=domain,
                action="greeting",
                decision="greeting_response",
                previous_domain=previous_domain,
                required_fields=required_fields,
            )
            return greeting_response()

        if domain == "general":
            log_routing_decision(
                query=query,
                domain=domain,
                action="general",
                decision="general_response",
                previous_domain=previous_domain,
                required_fields=required_fields,
            )
            return general_response()

        if domain == "ambiguous":

            # FIX 2: an unclear follow-up belongs to the conversation
            # already in progress. Only a conversation with no active
            # domain gets the dead-end clarification.
            if previous_domain in AGENTS:

                log_routing_decision(
                    query=query,
                    domain=previous_domain,
                    action="new_request",
                    decision="ambiguous_fallback_to_previous_domain",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )

                domain = previous_domain
                action = "new_request"

            else:

                log_routing_decision(
                    query=query,
                    domain=domain,
                    action="ambiguous",
                    decision="ambiguous_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return ambiguous_response()

        else:
            action = "new_request"

    # --------------------------------------------------
    # CASE 2: Pending fields exist
    # --------------------------------------------------

    else:

        # No valid previous domain: identify a new request.
        if previous_domain not in AGENTS:

            domain = identify_domain(query, previous_domain)

            if domain == "greeting":
                log_routing_decision(
                    query=query,
                    domain=domain,
                    action="greeting",
                    decision="greeting_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return greeting_response()

            if domain == "general":
                log_routing_decision(
                    query=query,
                    domain=domain,
                    action="general",
                    decision="general_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return general_response()

            if domain == "ambiguous":
                log_routing_decision(
                    query=query,
                    domain=domain,
                    action="ambiguous",
                    decision="ambiguous_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return ambiguous_response()

            action = "new_request"

        else:

            # Special handling for flight cabin class.
            if (
                previous_domain == "flight"
                and is_flight_cabin_class_answer(query)
            ):
                action = "continue"

                agent_required_fields = list(
                    dict.fromkeys(
                        required_fields + ["cabin_class"]
                    )
                )

            else:
                action = identify_conversation_action(
                    query=query,
                    required_fields=required_fields,
                    previous_domain=previous_domain,
                )

            # Handle conversation-level responses.
            if action == "greeting":
                log_routing_decision(
                    query=query,
                    domain=None,
                    action=action,
                    decision="greeting_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return greeting_response()

            elif action == "general":
                log_routing_decision(
                    query=query,
                    domain=None,
                    action=action,
                    decision="general_response",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )
                return general_response()

            elif action == "ambiguous":

                # FIX 3: keep the flow alive. Re-asking the pending
                # question is recoverable; a dead-end is not.
                log_routing_decision(
                    query=query,
                    domain=previous_domain,
                    action="continue",
                    decision="ambiguous_fallback_to_continue",
                    previous_domain=previous_domain,
                    required_fields=required_fields,
                )

                action = "continue"
                domain = previous_domain

            elif action == "continue":

                # Resume the previous domain agent.
                domain = previous_domain

            else:

                # New request: identify its domain.
                domain = identify_domain(query, previous_domain)

                if domain == "greeting":
                    log_routing_decision(
                        query=query,
                        domain=domain,
                        action="greeting",
                        decision="greeting_response",
                        previous_domain=previous_domain,
                        required_fields=required_fields,
                    )
                    return greeting_response()

                if domain == "general":
                    log_routing_decision(
                        query=query,
                        domain=domain,
                        action="general",
                        decision="general_response",
                        previous_domain=previous_domain,
                        required_fields=required_fields,
                    )
                    return general_response()

                if domain == "ambiguous":

                    # FIX 2: same fallback as CASE 1. The user is
                    # mid-flow; an unclear message resumes the flow
                    # rather than abandoning it.
                    if previous_domain in AGENTS:

                        log_routing_decision(
                            query=query,
                            domain=previous_domain,
                            action="continue",
                            decision="ambiguous_fallback_to_continue",
                            previous_domain=previous_domain,
                            required_fields=required_fields,
                        )

                        action = "continue"
                        domain = previous_domain

                    else:

                        log_routing_decision(
                            query=query,
                            domain=domain,
                            action="ambiguous",
                            decision="ambiguous_response",
                            previous_domain=previous_domain,
                            required_fields=required_fields,
                        )
                        return ambiguous_response()

                else:
                    action = "new_request"

    # --------------------------------------------------
    # CASE 3: Select agent pending fields
    # --------------------------------------------------

    if action == "continue":

        if not (
            previous_domain == "flight"
            and is_flight_cabin_class_answer(query)
        ):
            agent_required_fields = required_fields

    else:
        # Fresh request: do not forward old pending fields.
        agent_required_fields = []

    # --------------------------------------------------
    # CASE 4: Call selected domain agent
    # --------------------------------------------------

    log_routing_decision(
        query=query,
        domain=domain,
        action=action,
        decision="agent_selected",
        previous_domain=previous_domain,
        required_fields=agent_required_fields,
    )

    agent = AGENTS[domain]

    agent_start = start_timer()

    try:
        result = agent(
            request=request,
            query=query,
            required_fields=agent_required_fields,
        )

        log_ai_event(
            stage="orchestrator_agent_execution",
            input_data={
                "query": query,
                "selected_domain": domain,
                "required_fields": agent_required_fields,
            },
            output_data={
                "intent": result.get("intent"),
                "intent_done": result.get("intent_done"),
                "required_fields": result.get("required_fields"),
                "action": result.get("action"),
            },
            latency_ms=elapsed_ms(agent_start),
            status="success",
            metadata={
                "selected_domain": domain,
                "conversation_action": action,
            },
        )

    except Exception as exc:
        log_ai_event(
            stage="orchestrator_agent_execution",
            input_data={
                "query": query,
                "selected_domain": domain,
                "required_fields": agent_required_fields,
            },
            latency_ms=elapsed_ms(agent_start),
            status="failed",
            error=exc,
            metadata={
                "selected_domain": domain,
                "conversation_action": action,
            },
        )
        raise

    # --------------------------------------------------
    # CASE 5: Update orchestrator routing memory
    # --------------------------------------------------

    save_chat_state(
        request=request,
        domain=domain,
    )

    return result


# ==================================================
# 8. SINGLE CHATBOT API
# ==================================================

@api_view(["POST"])
def chatbot(request):

    query = request.data.get("query", "")

    if not isinstance(query, str):
        return Response(
            {"error": "query must be a string."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    query = query.strip()

    required_fields = request.data.get(
        "required_fields",
        None,
    )

    if not query:

        return Response(
            {"error": "query is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if (
        required_fields is not None
        and (
            not isinstance(required_fields, list)
            or not all(
                isinstance(field, str)
                for field in required_fields
            )
        )
    ):

        return Response(
            {
                "error": (
                    "required_fields must be a list "
                    "of strings or null."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    with trace_scope() as trace_id:

        request_start = start_timer()

        try:
            result = orchestrate_query(
                request=request,
                query=query,
                required_fields=required_fields,
            )

            log_ai_event(
                stage="orchestrator_request",
                input_data={
                    "query": query,
                    "required_fields": required_fields,
                },
                output_data={
                    "intent": result.get("intent"),
                    "intent_done": result.get("intent_done"),
                    "required_fields": result.get("required_fields"),
                    "action": result.get("action"),
                },
                latency_ms=elapsed_ms(request_start),
                status="success",
            )

            return Response(
                result,
                status=status.HTTP_200_OK,
            )

        except Exception as exc:
            log_ai_event(
                stage="orchestrator_request",
                input_data={
                    "query": query,
                    "required_fields": required_fields,
                },
                latency_ms=elapsed_ms(request_start),
                status="failed",
                error=exc,
            )

            return Response(
                {
                    "error": "Something went wrong while processing your request.",
                    "trace_id": trace_id,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@api_view(["POST"])
def verify_payment_dispatcher(request):
    """
    Unified payment verification endpoint.
    Routes to the correct service (movies, buses, flights) based on razorpay_order_id.
    """
    order_id = request.data.get("razorpay_order_id")

    if order_id:
        try:
            from movies.models import MoviePayment
            if MoviePayment.objects.filter(razorpay_order_id=order_id).exists():
                from movies import payments as movie_payments
                return movie_payments.verify_payment(request)
        except Exception:
            pass

        try:
            from buses.models import BusPayment
            if BusPayment.objects.filter(razorpay_order_id=order_id).exists():
                from buses import payments as bus_payments
                return bus_payments.verify_payment(request)
        except Exception:
            pass

        try:
            from flights.models import FlightPayment
            if FlightPayment.objects.filter(razorpay_order_id=order_id).exists():
                from flights import payments as flight_payments
                return flight_payments.verify_payment(request)
        except Exception:
            pass

    from movies import payments as movie_payments
    return movie_payments.verify_payment(request)
