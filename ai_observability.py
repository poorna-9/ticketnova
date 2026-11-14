
import json
import logging
import time
import uuid

from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone


# ==================================================
# 1. LOGGER
# ==================================================

ai_logger = logging.getLogger("ticketnova.ai")

trace_id_var = ContextVar(
    "ticketnova_trace_id",
    default=None,
)


# ==================================================
# 2. TRACE MANAGEMENT
# ==================================================

def new_trace_id():
    return str(uuid.uuid4())


def get_trace_id():
    return trace_id_var.get()


@contextmanager
def trace_scope(trace_id=None):
    """
    Creates one trace scope for a complete chatbot request.
    Nested functions automatically inherit the same trace ID.
    """

    trace_id = trace_id or new_trace_id()

    token = trace_id_var.set(trace_id)

    try:
        yield trace_id
    finally:
        trace_id_var.reset(token)


# ==================================================
# 3. SERIALIZATION
# ==================================================

def serialize_data(value):
    """
    Converts Pydantic models, dictionaries and other
    objects into JSON-compatible data.
    """

    if value is None:
        return None

    try:
        if hasattr(value, "model_dump"):
            value = value.model_dump()

        elif hasattr(value, "dict"):
            value = value.dict()

        elif hasattr(value, "content"):
            value = value.content

        return json.loads(
            json.dumps(value, default=str)
        )

    except Exception:
        return str(value)


# ==================================================
# 4. TIMING
# ==================================================

def start_timer():
    return time.perf_counter()


def elapsed_ms(start_time):
    return round(
        (time.perf_counter() - start_time) * 1000,
        2,
    )


# ==================================================
# 5. CENTRAL EVENT LOGGER
# ==================================================

def log_ai_event(
    stage,
    input_data=None,
    output_data=None,
    model=None,
    latency_ms=None,
    status="success",
    error=None,
    metadata=None,
):
    """
    Stores a structured event for AI observability
    and future DeepEval evaluation.

    Logging failures must never interrupt booking flows.
    """

    event = {
        "event_id": str(uuid.uuid4()),
        "trace_id": get_trace_id(),
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),

        "stage": stage,
        "model": model,

        "input": serialize_data(input_data),
        "output": serialize_data(output_data),

        "latency_ms": latency_ms,
        "status": status,

        "error": str(error) if error else None,

        "metadata": serialize_data(metadata or {}),
    }

    try:
        ai_logger.info(
            json.dumps(event, default=str)
        )

    except Exception:
        # Observability must not break the chatbot.
        logging.getLogger(__name__).exception(
            "Failed to write AI observability event"
        )
