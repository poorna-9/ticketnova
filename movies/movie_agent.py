"""
Movie booking agent.

The ROUTER app calls:
    result = handle_movie_query(request, query, required_fields)

and gets back a plain dict:
{
  "message": "...",              # text to show
  "required_fields": [...],      # what we still need (router sends this back next turn)
  "intent": "book",              # current movie intent
  "intent_done": False,          # True when this flow finished -> router can clear the active intent
  "options": [...],              # optional tappable choices  [{"label","value"}]
  "data": {...},                 # optional cards: movies / shows / seat_map / bookings / summary
  "action": "open_payment",      # optional UI action
  "payment": {...},              # present with action=open_payment (Razorpay checkout payload)
}

Memory lives in request.session["moviecontext"] and is updated every turn.
The agent calls the movie APIs (movie_api.views / movie_api.payments) in-process, so the REST
endpoints stay the single source of truth for locking, payments and refunds.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from difflib import get_close_matches

from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIRequestFactory

from movies import payments as mp
from movies import views as mv
from movies.models import Movie, TheaterScreen
from movies import payments as movie_payments

from .moviechat import (
    frame_movie_message,
    handle_movie_general,
    interpret_movie_query,
    movie_answer_from_data,
)

from ai_observability import (
    log_ai_event,
    start_timer,
    elapsed_ms,
)


logger = logging.getLogger(__name__)

MODE = "movie_booking"
CONTEXT_KEY = "moviecontext"
CONTEXT_TTL_MIN = 15
MAX_SEATS = 10
TIERS = {"CLASSIC", "PRIME", "RECLINER"}
SHIFT_RANGES = {"morning": (0, 12), "afternoon": (12, 16), "evening": (16, 20), "night": (20, 24)}

AFFIRMATIVE = {"yes", "y", "confirm", "ok", "okay", "sure", "go ahead", "proceed", "book it",
               "pay now", "confirm booking", "confirm cancellation", "continue"}
NEGATIVE = {"no", "n", "change", "edit", "not now", "stop", "change details", "keep booking", "dont cancel",
            "don't cancel"}

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

ASK = {
    "movie_title": "Ask which movie they want to watch.",
    "language": "Ask in which language they want to watch it.",
    "city": "Ask which city they want to watch the movie in.",
    "date": "Ask which date they want to watch it. They can say 'today', 'tomorrow' or an exact date.",
    "theater_name": "Ask which theater they prefer.",
    "show_time": "Ask which show time they want.",
    "seat_count": "Ask how many seats they want.",
    "booking_code": "Ask for the booking code of the booking they want.",
}

# ======================================================================
# memory
# ======================================================================

def load_context(request):
    ctx = request.session.get(CONTEXT_KEY) or {}
    raw = ctx.get("last_modified_at")
    last = parse_datetime(raw) if raw else None
    if last and timezone.now() > last + timedelta(minutes=CONTEXT_TTL_MIN):
        ctx = {}
    ctx["mode"] = MODE
    return ctx


def save_context(request, ctx):
    ctx["last_modified_at"] = timezone.now().isoformat()
    request.session[CONTEXT_KEY] = ctx
    request.session.modified = True


def reset_movie_context(request):
    request.session.pop(CONTEXT_KEY, None)
    request.session.modified = True


def chat_session_id(request):
    """Identity used for seat locks / payments / bookings (same value the frontend must send to /payments/verify/)."""
    if not request.session.session_key:
        request.session.create()
    return f"chat-{request.session.session_key}"


def known_cities():
    return list(TheaterScreen.objects.values_list("city", flat=True).distinct())


# ======================================================================
# calling the movie APIs in-process
# ======================================================================

_factory = APIRequestFactory()



def call_api(view, method="get", data=None, **kwargs):
    api_name = getattr(view, "__name__", view.__class__.__name__)
    api_start = start_timer()

    try:
        if method == "get":
            req = _factory.get("/", data or {})
        else:
            req = _factory.post("/", data or {}, format="json")

        resp = view(req, **kwargs)
        latency = elapsed_ms(api_start)

        response_data = getattr(resp, "data", {})

        log_ai_event(
            stage="movie_internal_api",
            input_data={
                "api": api_name,
                "method": method.upper(),
                "path_params": list(kwargs.keys()),
                "input_field_names": list((data or {}).keys()),
            },
            output_data={
                "http_status": resp.status_code,
                "success": (
                    response_data.get("success")
                    if isinstance(response_data, dict)
                    else None
                ),
                "response_field_names": (
                    list(response_data.keys())
                    if isinstance(response_data, dict)
                    else []
                ),
            },
            latency_ms=latency,
            status=(
                "success"
                if 200 <= resp.status_code < 300
                else "failed"
            ),
            metadata={
                "domain": "movie",
                "api": api_name,
            },
        )

        return resp.status_code, response_data

    except Exception as exc:
        log_ai_event(
            stage="movie_internal_api",
            input_data={
                "api": api_name,
                "method": method.upper(),
                "path_params": list(kwargs.keys()),
            },
            latency_ms=elapsed_ms(api_start),
            status="failed",
            error=exc,
            metadata={
                "domain": "movie",
                "api": api_name,
            },
        )
        raise



def api_error(resp, default="Something went wrong."):
    return (resp.get("error") or {}).get("message", default) if isinstance(resp, dict) else default


# ======================================================================
# response builders
# ======================================================================

def ask(request, ctx, backend_message, required_fields=None, query="", frame=True, **extra):
    ctx["required_fields"] = required_fields or []
    save_context(request, ctx)
    message = frame_movie_message(query, ctx, backend_message) if frame else backend_message
    return {"message": message, "required_fields": ctx["required_fields"],
            "intent": ctx.get("intent"), "intent_done": False,"domain": "movie", **extra}


def done(request, ctx, message, clear_show=False, **extra):
    ctx["required_fields"] = []
    for k in ("intent", "stage", "pending_action", "pending_summary", "bad_date"):
        ctx.pop(k, None)
    if clear_show:
        for k in ("movie_id", "movie_title", "show_id", "show_time", "time_shift", "theater_name",
                  "seat_labels", "seat_count", "tier", "max_price", "booking_code", "idempotency_key"):
            ctx.pop(k, None)
    save_context(request, ctx)
    return {"message": message, "required_fields": [], "intent": None, "intent_done": True, "domain": "movie", **extra}


# ======================================================================
# normalisation + merge into memory
# ======================================================================

def normalize_date(value):
    try:
        d = datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return d.isoformat() if d >= timezone.localdate() else None


def normalize_time(value):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_count(value):
    value = (value or "").strip().lower()
    if value.isdigit():
        n = int(value)
    else:
        n = NUMBER_WORDS.get(value)
    return n if n and 1 <= n <= MAX_SEATS else None


__SHOW_FIELDS = (
    "movie_title", "language", "city", "date",
    "theater_name", "show_time", "time_shift", "screen"
  )


def merge_filters(ctx, f):
    """Write only non-empty values into memory and invalidate anything they make stale."""
    def changed(k, v):
        return ctx.get(k) != v

    for k in __SHOW_FIELDS + ("genre", "area", "tier", "max_price", "price_semantic", "booking_code"):
        v = f.get(k)
        if v in ("", None):
            continue
        if k == "date":
            iso = normalize_date(v)
            if not iso:
                ctx.pop("date", None)
                ctx["bad_date"] = True
                continue
            v = iso
            ctx.pop("bad_date", None)
        elif k == "show_time":
            v = normalize_time(v)
            if not v:
                continue
        elif k == "tier":
            v = v.upper()
            if v not in TIERS:
                continue
        elif k == "booking_code":
            v = v.strip().upper()
        elif k in ("movie_title", "city", "language", "theater_name", "genre", "area"):
            v = v.strip()

        if k in __SHOW_FIELDS and changed(k, v):
            ctx.pop("show_id", None)
            ctx.pop("seat_labels", None)
            if k in ("movie_title", "language"):
                ctx.pop("movie_id", None)
            if k in ("movie_title", "language", "city", "date"):
                ctx.pop("show_time", None)
            if k in ("movie_title", "language", "city", "date", "theater_name", "show_time"):
               ctx.pop("screen", None)
            if k == "city":
                ctx.pop("theater_name", None)
        ctx[k] = v

    count = parse_count(f.get("seat_count", ""))
    if count:
        if ctx.get("seat_count") != count:
            ctx.pop("seat_labels", None)
        ctx["seat_count"] = count

    labels = [x.strip().upper() for x in (f.get("seat_labels") or []) if x.strip()]
    if labels:
        ctx["seat_labels"] = labels
        ctx["seat_count"] = len(labels)


LABEL_RE = re.compile(r"\b([A-Za-z]{1,2}\d{1,2})\b")
CODE_RE = re.compile(r"\b([0-9A-Fa-f]{8})\b")
TEXT_FIELDS = {"movie_title", "theater_name", "city", "language"}

def parse_natural_date(text):
    """Deterministically parse common date phrases using Django's local date."""
    q = (text or "").lower().strip()
    q = re.sub(r"\b(tmrw|tommorrow|tomorow)\b", "tomorrow", q)

    today = timezone.localdate()

    if re.search(r"\bday after (tomorrow|tmrw)\b", q):
        return (today + timedelta(days=2)).isoformat()

    if re.search(r"\btomorrow\b", q):
        return (today + timedelta(days=1)).isoformat()

    if re.search(r"\btoday\b", q):
        return today.isoformat()

    # Explicit numeric dates, such as 14-11-2026 or 14/11/2026.
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        match = re.search(
            r"\b\d{1,4}[-/]\d{1,2}[-/]\d{2,4}\b", q
        )
        if match:
            try:
                parsed = datetime.strptime(match.group(), fmt).date()
                if parsed >= today:
                    return parsed.isoformat()
            except ValueError:
                pass

    # Weekday names: "Friday", "this Friday", "next Friday".
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5,
        "sunday": 6,
    }

    for name, target in weekdays.items():
        if re.search(rf"\b{name}\b", q):
            delta = (target - today.weekday()) % 7

            if re.search(r"\bnext\b", q):
                delta = delta or 7
                delta += 7 if delta < 7 else 0
            elif delta == 0 and not re.search(r"\btoday\b", q):
                delta = 7

            return (today + timedelta(days=delta)).isoformat()

    return None

def is_passenger_details_question(query):
    normalized = (query or "").lower()

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


def apply_fallbacks(data, required_fields, query):
    """Deterministic safety net when the LLM misses an obvious answer to our own question."""
    f = data.setdefault("filters", {})
    q = (query or "").strip()
    natural_date = parse_natural_date(q)

    if natural_date:
        f["date"] = natural_date

    for shift in ("morning", "afternoon", "evening", "night"):
        if re.search(rf"\b{shift}\b", q.lower()):
            f["time_shift"] = shift
            break


    if "seat_count" in required_fields and not f.get("seat_count"):
        m = re.search(r"\b(\d+)\b", q) or re.search(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", q.lower())
        if m:
            f["seat_count"] = m.group(1)

    if ("seat_labels" in required_fields or "seat" in q.lower()) and not f.get("seat_labels"):
        labels = [m.upper() for m in LABEL_RE.findall(q)]
        if labels:
            f["seat_labels"] = labels

    if ("booking_code" in required_fields or "cancel" in q.lower()) and not f.get("booking_code"):
        m = CODE_RE.search(q)
        if m and re.search(r"[A-Fa-f]", m.group(1)):
            f["booking_code"] = m.group(1).upper()

    if len(required_fields) == 1 and required_fields[0] in TEXT_FIELDS:
        field = required_fields[0]

        if not f.get(field) and 0 < len(q.split()) <= 8:
            value = q

            if field == "theater_name":
                value = re.sub(
                    r"^(please\s+)?(book|select|choose|at|in)\s+",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )

                value = re.sub(
                    r"^(me\s+)?(please\s+)?",
                    "",
                    value,
                    flags=re.IGNORECASE,
                )

            f[field] = value.strip()

    if required_fields and "movie_title" not in required_fields and f.get("movie_title"):
        m_val = f["movie_title"].strip().lower()
        t_val = f.get("theater_name", "").strip().lower()
        if m_val == t_val or m_val == q.lower() or "cinemas" in m_val or "theater" in m_val or "theatre" in m_val:
            f["movie_title"] = ""

    return data


# ======================================================================
# resolving movie -> show
# ======================================================================

def resolve_movie(ctx):
    """-> ("ok", Movie) | ("ask", payload) | ("none", None). Writes movie_id/title/language to ctx."""
    title = (ctx.get("movie_title") or "").strip()
    qs = Movie.objects.all()
    if ctx.get("language"):
        qs = qs.filter(language__iexact=ctx["language"])

    matches = list(qs.filter(title__iexact=title)) or list(qs.filter(title__icontains=title)[:6])
    if not matches:
        by_lower = {m.title.lower(): m for m in qs}
        close = get_close_matches(title.lower(), list(by_lower), n=5, cutoff=0.6)
        matches = [by_lower[c] for c in close]
    if not matches:
        return "none", None

    if len({m.title.lower() for m in matches}) == 1:
        if len(matches) == 1:
            m = matches[0]
            ctx.update(movie_id=m.id, movie_title=m.title, language=m.language)
            return "ok", m
        langs = sorted({m.language for m in matches})
        return "ask", {
            "field": "language",
            "message": f"{ASK['language']} '{matches[0].title}' is available in: {', '.join(langs)}.",
            "options": [{"label": l, "value": l} for l in langs],
        }

    titles = sorted({m.title for m in matches})
    return "ask", {
        "field": "movie_title",
        "message": f"Ask which of these movies they mean: {', '.join(titles)}.",
        "options": [{"label": t, "value": t} for t in titles],
    }


def fetch_shows(ctx, date_iso):
    code, resp = call_api(
        mv.movie_shows,
        "get",
        {"city": ctx["city"], "date": date_iso},
        movie_id=ctx["movie_id"]
    )

    print("\n========== MOVIE SHOWS API DEBUG ==========")
    print("STATUS:", code)
    print("RESPONSE:", resp)

    flat = []
    if code == 200:
        for grp in resp["data"]["theaters"]:
            for s in grp["shows"]:
                print(
                    "SHOW:",
                    {
                        "show_id": s["id"],
                        "theater": grp["theater"],
                        "screen": grp["screen"],
                        "available_seats": s["available_seats"],
                        "base_price": s["base_price"],
                    }
                )

                flat.append({
                    "show_id": s["id"],
                    "theater": grp["theater"],
                    "screen": grp["screen"],
                    "show_date": s["show_date"],
                    "start_time": s["start_time"][:5],
                    "base_price": s["base_price"],
                    "available_seats": s["available_seats"],
                })

    return sorted(flat, key=lambda s: (s["theater"], s["start_time"]))

def match_screen(screen, shows):
    if not screen:
        return shows

    requested = re.search(r"\d+", str(screen))
    if requested:
        requested = requested.group()

        return [
            s for s in shows
            if (match := re.search(r"\d+", str(s["screen"])))
            and match.group() == requested
        ]

    return [
        s for s in shows
        if str(s["screen"]).strip().lower() == str(screen).strip().lower()
    ]

def match_theaters(name, flat):
    names = {s["theater"].lower(): s["theater"] for s in flat}
    n = name.lower().strip()
    hits = [orig for low, orig in names.items() if n in low or low in n]
    if not hits:
        hits = [names[c] for c in get_close_matches(n, list(names), n=3, cutoff=0.6)]
    return sorted(set(hits))


def _mins(t):
    return int(t[:2]) * 60 + int(t[3:5])


def filter_time(shows, show_time, shift):
    out = shows
    if shift in SHIFT_RANGES:
        lo, hi = SHIFT_RANGES[shift]
        out = [s for s in out if lo <= int(s["start_time"][:2]) < hi]
    if show_time:
        exact = [s for s in out if s["start_time"] == show_time]
        out = exact or [s for s in out if abs(_mins(s["start_time"]) - _mins(show_time)) <= 60]
    return out


def _theater_options(flat):
    opts = []
    for t in sorted({s["theater"] for s in flat}):
        times = ", ".join(s["start_time"] for s in flat if s["theater"] == t)
        opts.append({"label": f"{t} ({times})", "value": t})
    return opts


def _time_options(shows):
    return [{"label": f"{s['start_time']} · {s['screen']} · ₹{s['base_price']} · {s['available_seats']} left",
             "value": s["start_time"]} for s in shows]



def resolve_show(ctx, require_date=True):
    """
    Resolves a movie show only after the user selects:
    movie, city, date, theater and showtime.

    Returns:
        ("ok", show)
        ("ask", {"field", "message", "options"})
        ("none", {...})
    """

    if not ctx.get("movie_title"):
        return "ask", {
            "field": "movie_title",
            "message": ASK["movie_title"],
            "options": []
        }

    status, res = resolve_movie(ctx)

    if status == "ask":
        return "ask", res

    if status == "none":
        ctx.pop("movie_title", None)
        return "ask", {
            "field": "movie_title",
            "options": [],
            "message": "No movie matched that title. Please provide the movie name again."
        }

    if not ctx.get("city"):
        cities = known_cities()
        return "ask", {
            "field": "city",
            "message": f"{ASK['city']} Available cities: {', '.join(cities)}.",
            "options": [{"label": c, "value": c} for c in cities]
        }

    ctx["city"] = normalize_city(ctx["city"])

    date_iso = ctx.get("date")

    if not date_iso:
        if require_date:
            msg = ASK["date"]

            if ctx.get("bad_date"):
                msg = "That date is invalid or already passed. " + msg

            return "ask", {
                "field": "date",
                "message": msg,
                "options": []
            }

        date_iso = timezone.localdate().isoformat()

    flat = fetch_shows(ctx, date_iso)

    if not flat:
        ctx.pop("date", None)
        return "none", {
            "field": "date",
            "options": [],
            "message": (
                f"There are no shows of {ctx['movie_title']} "
                f"in {ctx['city']} on {date_iso}. "
                "Please try another date."
            )
        }

    # Require explicit theater selection.
    if not ctx.get("theater_name"):
        opts = _theater_options(flat)

        return "ask", {
            "field": "theater_name",
            "options": opts,
            "message": (
                "Here are the available theaters. "
                "Please select one."
            )
        }

    hits = match_theaters(ctx["theater_name"], flat)

    if not hits:
        ctx.pop("theater_name", None)
        opts = _theater_options(flat)

        return "ask", {
            "field": "theater_name",
            "options": opts,
            "message": (
                "That theater is not available. "
                "Please select one of these theaters."
            )
        }

    if len(hits) > 1:
        opts = _theater_options([
            s for s in flat if s["theater"] in hits
        ])

        return "ask", {
            "field": "theater_name",
            "options": opts,
            "message": "Multiple theaters match. Please select the exact theater."
        }

    cands = [
        s for s in flat
        if s["theater"] in hits
    ]

    # Require explicit showtime selection.
    if not ctx.get("show_time") and not ctx.get("time_shift"):
        opts = _time_options(cands)

        return "ask", {
            "field": "show_time",
            "options": opts,
            "message": (
                "Please select a showtime from the available options."
            )
        }

    timed = filter_time(
        cands,
        ctx.get("show_time"),
        ctx.get("time_shift")
    )

    if not timed:
        ctx.pop("show_time", None)
        ctx.pop("time_shift", None)

        opts = _time_options(cands)

        return "ask", {
            "field": "show_time",
            "options": opts,
            "message": (
                "No show matches that time. "
                "Please select an available showtime."
            )
        }

    # Never select the first show when multiple shows match.
    if len(timed) != 1:
        opts = _time_options(timed)

        return "ask", {
            "field": "show_time",
            "options": opts,
            "message": (
                "Multiple shows match your selection. "
                "Please select the exact showtime."
            )
        }

    show = timed[0]

    ctx.update(
        show_id=show["show_id"],
        theater_name=show["theater"],
        show_time=show["start_time"],
        date=show["show_date"],
        screen=show["screen"]
    )

    return "ok", show


# ======================================================================
# seats
# ======================================================================

def get_seat_map(show_id, sid):
    code, resp = call_api(
        mv.show_seat_map,
        "get",
        {"session_id": sid},
        show_id=show_id
    )

    print("\n========== SEAT MAP API DEBUG ==========")
    print("SHOW ID:", show_id)
    print("STATUS:", code)
    print("RAW RESPONSE:", resp)

    if code != 200:
        return None

    seat_map = resp["data"]
    print("SEAT MAP SUMMARY:", seat_map.get("summary"))

    for row in seat_map.get("rows", []):
        print(
            "ROW:",
            row.get("row_label"),
            [
                {
                    "seat": s.get("seat_label"),
                    "status": s.get("status"),
                    "locked_by_me": s.get("locked_by_me"),
                    "price": s.get("price"),
                }
                for s in row.get("seats", [])
            ]
        )

    return seat_map


def _seat_no(label):
    m = re.match(r"^([A-Z]+)(\d+)$", label)
    return int(m.group(2)) if m else 0


def pick_seats(rows, count, tier=None, max_price=None):
    """Best `count` seats together (middle-ish rows, centred in the row). -> (labels, together) | None"""
    def usable(s):
        if not (s["status"] == "AVAILABLE" or s.get("locked_by_me")):
            return False
        if tier and s["tier"] != tier:
            return False
        return max_price is None or float(s["price"]) <= max_price

    target_row = len(rows) * 0.6
    best, pool = None, []
    for ri, row in enumerate(rows):
        seats = sorted(row["seats"], key=lambda s: _seat_no(s["seat_label"]))
        nums = [_seat_no(s["seat_label"]) for s in seats]
        mid = (min(nums) + max(nums)) / 2 if nums else 0
        good = [s for s in seats if usable(s)]
        pool.extend((abs(ri - target_row), s) for s in good)

        run = []
        for s in good + [None]:
            if s is not None and run and _seat_no(s["seat_label"]) == _seat_no(run[-1]["seat_label"]) + 1:
                run.append(s)
                continue
            if len(run) >= count:
                for i in range(len(run) - count + 1):
                    win = run[i:i + count]
                    centre = sum(_seat_no(x["seat_label"]) for x in win) / count
                    score = abs(ri - target_row) * 2 + abs(centre - mid) * 0.1
                    if best is None or score < best[0]:
                        best = (score, [x["seat_label"] for x in win])
            run = [s] if s is not None else []

    if best:
        return best[1], True
    if len(pool) >= count:
        pool.sort(key=lambda t: t[0])
        return [s["seat_label"] for _, s in pool[:count]], False
    return None


def _seat_index(seat_map):
    return {s["seat_label"]: s for row in seat_map["rows"] for s in row["seats"]}


# ======================================================================
# intent handlers
# ======================================================================
CITY_ALIASES = {
    "hyd": "Hyderabad",
    "hyderabad": "Hyderabad",

    "blr": "Bangalore",
    "bengaluru": "Bangalore",
    "bangalore": "Bangalore",

    "mum": "Mumbai",
    "bombay": "Mumbai",
    "mumbai": "Mumbai",

    "del": "Delhi",
    "new delhi": "Delhi",
    "delhi": "Delhi",

    "chen": "Chennai",
    "madras": "Chennai",
    "chennai": "Chennai",

    "kol": "Kolkata",
    "calcutta": "Kolkata",
    "kolkata": "Kolkata",

    "pun": "Pune",
    "pune": "Pune",
}


def normalize_city(city):
    if not city:
        return city

    key = " ".join(city.strip().lower().split())
    return CITY_ALIASES.get(key, city.strip())



def handle_search_movies(request, ctx, query):

    if not ctx.get("city") and not ctx.get("movie_title"):
        cities = known_cities()

        return ask(
            request,
            ctx,
            f"{ASK['city']} Available cities: {', '.join(cities)}.",
            ["city"],
            query,
            options=[
                {"label": c, "value": c}
                for c in cities
            ]
        )

    if ctx.get("city"):
        ctx["city"] = normalize_city(ctx["city"])

    params = {
        k: ctx[k]
        for k in ("city", "language", "genre")
        if ctx.get(k)
    }

    if ctx.get("movie_title"):
        params["q"] = ctx["movie_title"]

    print("========== MOVIE SEARCH DEBUG ==========")
    print("MOVIE SEARCH CONTEXT:", ctx)
    print("MOVIE API PARAMS:", params)

    code, resp = call_api(
        mv.movie_list,
        "get",
        params
    )

    movies = resp.get("data", []) if code == 200 else []

    print("MOVIE API STATUS:", code)
    print("MOVIE API RESPONSE:", resp)
    print("MOVIES COUNT:", len(movies))

    if not movies:
        return done(
            request,
            ctx,
            "I couldn't find any movies matching that. "
            "Want to try a different language, genre or city?"
        )

    where = f" in {ctx['city']}" if ctx.get("city") else ""

    return done(
        request,
        ctx,
        f"Here are {len(movies)} movie(s) available according to your requirements{where}.",
        data={"movies": movies}
    )



def handle_showtimes(request, ctx, query):
    if not ctx.get("movie_title"):
        return ask(request, ctx, ASK["movie_title"], ["movie_title"], query)
    status, payload = resolve_show_partial(ctx)
    if status == "ask":
        return ask(request, ctx, payload["message"], [payload["field"]], query, options=payload["options"])
    flat, date_iso = payload
    if not flat:
        return done(request, ctx, f"No shows of {ctx['movie_title']} in {ctx['city']} on {date_iso}. Try another date?")
    if ctx.get("theater_name"):
        hits = match_theaters(ctx["theater_name"], flat)
        flat = [s for s in flat if s["theater"] in hits] or flat
    flat = filter_time(flat, ctx.get("show_time"), ctx.get("time_shift")) or flat
    return done(request, ctx,
                f"{ctx['movie_title']} on {date_iso} in {ctx['city']} - {len(flat)} show(s). Want to book one?",
                data={"shows": flat})


def resolve_show_partial(ctx):
    """movie + city (+ optional date, default today) only; no theater / time disambiguation."""
    status, res = resolve_movie(ctx)
    if status == "ask":
        return "ask", res
    if status == "none":
        ctx.pop("movie_title", None)
        return "ask", {"field": "movie_title", "options": [], "message": "No movie matched. Ask for the title again."}
    if not ctx.get("city"):
        cities = known_cities()
        return "ask", {"field": "city", "message": f"{ASK['city']} Available cities: {', '.join(cities)}.",
                       "options": [{"label": c, "value": c} for c in cities]}
    date_iso = ctx.get("date") or timezone.localdate().isoformat()
    return "ok", (fetch_shows(ctx, date_iso), date_iso)


def handle_show_seats(request, ctx, query):
    status, data = resolve_show(ctx, require_date=False)
    if status in ("ask", "none"):
        return ask(request, ctx, data["message"], [data["field"]], query, options=data["options"])
    seat_map = get_seat_map(data["show_id"], chat_session_id(request))
    if not seat_map:
        return done(request, ctx, "I couldn't load the seat map right now. Please try again.")
    s = seat_map["summary"]
    return done(request, ctx, f"{s['AVAILABLE']} of {s['total']} seats are available for the {data['start_time']} show "
                              f"at {data['theater']}.", data={"seat_map": seat_map, "show": data})




def handle_book(request, ctx, query):
    """
    Movie booking flow:
    1. Resolve explicitly selected show.
    2. Load and validate seats.
    3. Lock seats.
    4. Create Razorpay payment order.
    5. Return payment details to frontend.

    No separate confirmation step.
    """

    # 1. Resolve selected show.
    status, data = resolve_show(ctx, require_date=True)

    if status in ("ask", "none"):
        return ask(
            request,
            ctx,
            data["message"],
            [data["field"]],
            query,
            options=data["options"]
        )

    show = data
    sid = chat_session_id(request)

    # 2. Load seat map.
    seat_map = get_seat_map(show["show_id"], sid)

    if not seat_map:
        return done(
            request,
            ctx,
            "I couldn't load the seats for that show. Please try again."
        )

    index = _seat_index(seat_map)

    # 3. Get or validate selected seats.
    if ctx.get("seat_labels"):
        labels = ctx["seat_labels"]

        bad = [
            label for label in labels
            if (
                label not in index
                or not (
                    index[label]["status"] == "AVAILABLE"
                    or index[label].get("locked_by_me")
                )
            )
        ]

        if bad:
            ctx.pop("seat_labels", None)

            return ask(
                request,
                ctx,
                f"Seat(s) {', '.join(bad)} are unavailable. Please select other seats.",
                ["seat_count"],
                query,
                data={"seat_map": seat_map}
            )

    else:
        count = ctx.get("seat_count")

        if not count:
            return ask(
                request,
                ctx,
                ASK["seat_count"],
                ["seat_count"],
                query,
                data={"seat_map": seat_map}
            )

        max_price = (
            float(ctx["max_price"])
            if ctx.get("max_price")
            else None
        )

        picked = pick_seats(
            seat_map["rows"],
            int(count),
            ctx.get("tier"),
            max_price
        )

        if not picked:
            ctx.pop("seat_count", None)

            return ask(
                request,
                ctx,
                f"Not enough seats available for {count}. Please choose fewer seats or another tier.",
                ["seat_count"],
                query,
                data={"seat_map": seat_map}
            )

        ctx["seat_labels"], together = picked
        ctx["seats_together"] = together

        labels = ctx["seat_labels"]

    # 4. Calculate amount for display.
    total = sum(
        float(index[label]["price"])
        for label in labels
    )

    # 5. Lock seats using existing API.
    lock_payload = {
        "session_id": sid,
        "seat_labels": labels
    }

    lock_code, lock_resp = call_api(
    mv.lock_seats,
    "post",
    lock_payload,
    show_id=show["show_id"]
    )

    if lock_code not in (200, 201) or not lock_resp.get("success"):
        ctx.pop("seat_labels", None)

        return done(
            request,
            ctx,
            "Sorry, those seats could not be locked. Please select your seats again."
        )

    locked_data = lock_resp.get("data", {})

    # 6. Create payment order using existing API.
    import uuid

    idempotency_key = ctx.get("idempotency_key")

    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
        ctx["idempotency_key"] = idempotency_key

    payment_payload = {
        "session_id": sid,
        "show_id": show["show_id"],
        "seat_labels": labels,
        "idempotency_key": idempotency_key
    }

    payment_code, payment_resp = call_api(
        movie_payments.create_payment_order,
        "post",
        payment_payload
    )

    if payment_code not in (200, 201) or not payment_resp.get("success"):
        # Payment order failed; release seats.
        call_api(
            mv.unlock_seats,
            "post",
            {
                "session_id": sid,
                "seat_labels": labels
            },
            show_id=show["show_id"]
        )

        ctx.pop("seat_labels", None)
        ctx.pop("idempotency_key", None)

        return done(
            request,
            ctx,
            "Seats were locked, but payment initialization failed. Please try booking again."
        )

    payment_data = payment_resp.get("data", {})

    # 7. Preserve payment details for frontend.
    ctx["stage"] = "payment_pending"
    ctx["pending_payment_order"] = payment_data.get("razorpay_order_id")

    movie_title = show.get("movie_title") or ctx.get("movie_title") or "Movie"
    amount_rupees = f"{total:.2f}"

    payment_data["session_id"] = sid
    payment_data["domain"] = "movie"
    payment_data["movie_title"] = movie_title
    payment_data["theater"] = show.get("theater", "")
    payment_data["screen"] = show.get("screen", "")
    payment_data["show_date"] = show.get("show_date", "")
    payment_data["start_time"] = show.get("start_time", "")
    payment_data["seat_labels"] = labels
    payment_data["amount_in_rupees"] = amount_rupees
    payment_data["verify_url"] = "/movies/payments/verify/"

    summary = {
        "show": {
            **show,
            "movie": movie_title,
        },
        "seat_labels": labels,
        "total_amount": amount_rupees,
    }

    return done(
        request,
        ctx,
        (
            f"Your seats {', '.join(labels)} are locked for "
            f"{movie_title} "
            f"at {show.get('theater')}, {show.get('start_time')}. "
            "Opening secure payment checkout now."
        ),
        action="open_payment",
        payment=payment_data,
        data={
            "booking": summary,
            "payment": payment_data
        }
    )


def handle_cancel(request, ctx, query):
    sid = chat_session_id(request)
    code, resp = call_api(mv.booking_list, "get", {"session_id": sid})
    mine = resp.get("data", []) if code == 200 else []
    upcoming = [b for b in mine if b["status"] == "CONFIRMED" and b["show"]["show_date"] >= timezone.localdate().isoformat()]

    if not ctx.get("booking_code"):
        if not upcoming:
            return done(request, ctx, "You don't have any upcoming bookings to cancel.")
        opts = [{"label": f"{b['booking_code']} · {b['show']['movie']} · {b['show']['show_date']} {b['show']['start_time']}",
                 "value": b["booking_code"]} for b in upcoming]
        return ask(request, ctx, f"Ask which booking they want to cancel. Options: {', '.join(o['label'] for o in opts)}.",
                   ["booking_code"], query, options=opts)

    booking = next((b for b in mine if b["booking_code"] == ctx["booking_code"]), None)
    if not booking:
        ctx.pop("booking_code", None)
        return ask(request, ctx, "I couldn't find that booking in your account. Ask them to re-check the booking code.",
                   ["booking_code"], query)
    if booking["status"] != "CONFIRMED":
        return done(request, ctx, f"That booking is already {booking['status'].lower()}.")

    summary = (f"Cancel booking {booking['booking_code']}: {booking['show']['movie']} on {booking['show']['show_date']} at "
               f"{booking['show']['start_time']}, seats {', '.join(booking['seat_labels'])}. A refund of "
               f"₹{booking['total_amount']} will be started. Reply 'confirm cancellation' to proceed.")
    ctx.update(pending_action="cancel", stage="awaiting_confirmation", pending_summary=summary)
    return ask(request, ctx, summary, [], query, frame=False, awaiting_confirmation=True,
               options=[{"label": "Confirm cancellation", "value": "confirm cancellation"},
                        {"label": "Keep booking", "value": "keep booking"}])


def handle_my_bookings(request, ctx, query):
    code, resp = call_api(mv.booking_list, "get", {"session_id": chat_session_id(request)})
    bookings = resp.get("data", []) if code == 200 else []
    if ctx.get("booking_code"):
        bookings = [b for b in bookings if b["booking_code"] == ctx["booking_code"]]
    if not bookings:
        return done(request, ctx, "I couldn't find any bookings for you yet.")
    return done(request, ctx, movie_answer_from_data(query, {"bookings": bookings}), data={"bookings": bookings})


def handle_movie_info(request, ctx, query):
    if not ctx.get("movie_title"):
        return ask(request, ctx, ASK["movie_title"], ["movie_title"], query)
    status, res = resolve_movie(ctx)
    if status == "ask":
        return ask(request, ctx, res["message"], [res["field"]], query, options=res["options"])
    if status == "none":
        ctx.pop("movie_title", None)
        return ask(request, ctx, "No movie matched that title. Ask them to tell the name again.", ["movie_title"], query)
    info = {"title": res.title, "language": res.language, "genre": res.genre,
            "duration_mins": res.duration_mins, "certification": res.certification}
    return done(request, ctx, movie_answer_from_data(query, info), data={"movie": info})


def handle_general(request, ctx, query):
    return done(request, ctx, handle_movie_general(query, ctx.get("city")))


# ======================================================================
# confirmation step -> actually does the thing
# ======================================================================

def execute_book(request, ctx, query):
    sid = chat_session_id(request)
    code, resp = call_api(mv.lock_seats, "post",
                          {"session_id": sid, "seat_labels": ctx["seat_labels"]}, show_id=ctx["show_id"])
    if code != 200:
        ctx.pop("seat_labels", None)
        ctx.pop("pending_action", None)
        ctx["stage"] = None
        return ask(request, ctx, f"{api_error(resp)} Ask them to choose different seats or say how many they want.",
                   ["seat_count"], query)

    key = ctx.get("idempotency_key") or uuid.uuid4().hex
    ctx["idempotency_key"] = key
    code, resp = call_api(mp.create_payment_order, "post", {
        "session_id": sid, "show_id": ctx["show_id"], "seat_labels": ctx["seat_labels"], "idempotency_key": key,
    })
    if code not in (200, 201):
        call_api(mv.unlock_seats, "post", {"session_id": sid, "seat_labels": ctx["seat_labels"]}, show_id=ctx["show_id"])
        ctx.pop("idempotency_key", None)
        return done(request, ctx, f"I couldn't start the payment: {api_error(resp)} Please try again.")

    payment = resp["data"]
    if payment.get("status") == "PAID":   # idempotent replay of an already-paid order
        return done(request, ctx, f"Already booked! Your booking code is {payment['booking']['booking_code']}.",
                    clear_show=True, data={"booking": payment["booking"]})
    payment["session_id"] = sid
    ctx["pending_order_id"] = payment["razorpay_order_id"]
    mins = max(1, payment["remaining_seconds"] // 60)
    return done(request, ctx, f"Seats {', '.join(ctx['seat_labels'])} are held for you. Please complete the payment "
                              f"within {mins} minutes.", clear_show=True, action="open_payment", payment=payment)


def execute_cancel(request, ctx, query):
    code, resp = call_api(mp.cancel_booking, "post", {"session_id": chat_session_id(request)},
                          booking_code=ctx["booking_code"])
    if code != 200:
        return done(request, ctx, f"I couldn't cancel that booking: {api_error(resp)}")
    refund = resp["data"]["refund"]
    if refund["status"] == "INITIATED":
        msg = f"Your booking is cancelled and a refund of ₹{refund['amount']} has been initiated."
    elif refund["status"] == "FAILED":
        msg = "Your booking is cancelled, but the refund couldn't be started yet - it will be retried."
    else:
        msg = "Your booking is cancelled."
    return done(request, ctx, msg, clear_show=True, data=resp["data"])


# ======================================================================
# main entry point (called by the router app)
# ======================================================================

HANDLERS = {
    "search_movies": handle_search_movies,
    "show_showtimes": handle_showtimes,
    "show_seats": handle_show_seats,
    "book": handle_book,
    "cancel_booking": handle_cancel,
    "my_bookings": handle_my_bookings,
    "movie_info": handle_movie_info,
    "general": handle_general,
}


def _handle_movie_query(request, query, required_fields=None):
    query = (query or "").strip()
    if required_fields is None:
        required_fields = request.session.get(
            CONTEXT_KEY, {}
        ).get("required_fields", [])
    required_fields = [f for f in (required_fields or []) if isinstance(f, str)]
    ctx = load_context(request)
    if ctx.get("city"):
       ctx["city"] = normalize_city(ctx["city"])
    print("MOVIE DEBUG | query=%s | required_fields=%s | ctx=%s", query, required_fields, ctx)
    q = query.lower().strip(" .!")

    # 1) yes / no to a pending confirmation
    if ctx.get("stage") == "awaiting_confirmation" and ctx.get("pending_action"):
        if q in AFFIRMATIVE:
            action = ctx["pending_action"]
            ctx.pop("pending_action", None)
            ctx["stage"] = None
            return execute_book(request, ctx, query) if action == "book" else execute_cancel(request, ctx, query)
        if q in NEGATIVE:
            action = ctx.pop("pending_action")
            ctx["stage"] = None
            if action == "cancel":
                return done(request, ctx, "Okay, your booking is kept as it is.")
            ctx.pop("seat_labels", None)
            return ask(request, ctx, "Ask what they would like to change: seats, show time, theater or date.", [], query)
    parsed = interpret_movie_query(query, required_fields, ctx, timezone.localdate(), known_cities())
    # 2) understand the message (LLM + deterministic fallbacks)
    print("========== MOVIE PARSE DEBUG ==========")
    print("QUERY: %s", query)
    print("REQUIRED FIELDS: %s", required_fields)
    print("PARSED INTENT: %s", parsed.get("intent"))
    print("PARSED FILTERS: %s", parsed.get("filters"))
    print("PARSE ROUTE: %s", parsed.get("_route"))


    print("FILTERS AFTER FALLBACK: %s", parsed.get("filters"))
    logger.info("=======================================")
    parsed = apply_fallbacks(parsed, required_fields, query)
    log_ai_event(
        stage="movie_query_interpretation",
        input_data={
            "required_fields": required_fields,
            "query_length": len(query),
        },
        output_data={
            "intent": parsed.get("intent"),
            "route": parsed.get("_route"),
            "extracted_field_names": list(
                (parsed.get("filters") or {}).keys()
            ),
        },
        metadata={
            "domain": "movie",
            "model": "gpt-4o-mini",
        },
    )

    # 3) update memory
    intent = parsed.get("intent") or "unknown"
    if intent in ("unknown", "") and ctx.get("intent"):
        intent = ctx["intent"]
    if intent != ctx.get("intent") and intent not in ("general", "unknown"):
        ctx.pop("pending_action", None)          # a different request replaces any pending confirmation
        ctx["stage"] = None

    # Safety Guard: If user was answering a pending field and did NOT ask for movie_title,
    # prevent the theater name or answering query from clobbering the existing movie_title!
    filters = parsed.get("filters", {})
    if required_fields and "movie_title" not in required_fields and ctx.get("movie_title"):
        if filters.get("movie_title"):
            m_title = filters["movie_title"].strip().lower()
            t_name = filters.get("theater_name", "").strip().lower()
            if m_title == t_name or m_title == query.strip().lower() or "cinemas" in m_title or "theater" in m_title or "theatre" in m_title:
                filters["movie_title"] = ""

    merge_filters(ctx, filters)
    if ctx.get("stage") == "awaiting_confirmation":   # user edited details instead of answering yes/no
        ctx.pop("pending_action", None)
        ctx["stage"] = None
    ctx["intent"] = intent
    log_ai_event(
        stage="movie_context_update",
        input_data={
            "intent": intent,
        },
        output_data={
            "context_field_names": list(ctx.keys()),
            "required_fields": required_fields,
        },
        metadata={
            "domain": "movie",
        },
    )

    # 4) dispatch on intent
    handler = HANDLERS.get(intent)
    if not handler:
        log_ai_event(
            stage="movie_handler_selection",
            input_data={
                "intent": intent,
            },
            output_data={
                "selected_handler": None,
            },
            status="failed",
            metadata={
                "reason": "handler_not_found",
                "domain": "movie",
            },
        )
        return ask(request, ctx, "Ask whether they want to find movies, check showtimes, book tickets, "
                                 "view bookings or cancel a booking.", [], query)
    log_ai_event(
        stage="movie_handler_selection",
        input_data={
            "intent": intent,
            "required_fields": required_fields,
        },
        output_data={
            "selected_handler": handler.__name__,
        },
        metadata={
            "domain": "movie",
        },
    )
    handler_start = start_timer()

    try:
        result = handler(request, ctx, query)

        log_ai_event(
            stage="movie_handler_execution",
            input_data={
                "intent": intent,
                "handler": handler.__name__,
                "required_fields": required_fields,
            },
            output_data={
                "intent": result.get("intent"),
                "intent_done": result.get("intent_done"),
                "required_fields": result.get("required_fields"),
                "action": result.get("action"),
                "has_data": bool(result.get("data")),
                "has_payment": bool(result.get("payment")),
            },
            latency_ms=elapsed_ms(handler_start),
            status="success",
            metadata={
                "domain": "movie",
                "handler": handler.__name__,
            },
        )

        return result

    except Exception as exc:
        log_ai_event(
            stage="movie_handler_execution",
            input_data={
                "intent": intent,
                "handler": handler.__name__,
            },
            latency_ms=elapsed_ms(handler_start),
            status="failed",
            error=exc,
            metadata={
                "domain": "movie",
                "handler": handler.__name__,
            },
        )

        logger.exception("Movie handler failed intent=%s", intent)

        return done(
            request,
            ctx,
            "Sorry, something went wrong on my side. Please try again.",
        )


def handle_movie_query(request, query, required_fields=None):
    agent_start = start_timer()

    try:
        result = _handle_movie_query(
            request,
            query,
            required_fields,
        )

        log_ai_event(
            stage="movie_agent_request",
            input_data={
                "query_length": len(query or ""),
                "required_fields": required_fields or [],
            },
            output_data={
                "intent": result.get("intent"),
                "intent_done": result.get("intent_done"),
                "required_fields": result.get("required_fields"),
                "action": result.get("action"),
                "has_data": bool(result.get("data")),
                "has_payment": bool(result.get("payment")),
            },
            latency_ms=elapsed_ms(agent_start),
            status="success",
            metadata={
                "domain": "movie",
            },
        )

        return result

    except Exception as exc:
        log_ai_event(
            stage="movie_agent_request",
            input_data={
                "query_length": len(query or ""),
                "required_fields": required_fields or [],
            },
            latency_ms=elapsed_ms(agent_start),
            status="failed",
            error=exc,
            metadata={
                "domain": "movie",
            },
        )
        raise


# optional: direct HTTP wrapper if you ever want to hit the movie agent without the router
def movie_chat_view(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}
    result = handle_movie_query(request, body.get("query", ""), body.get("required_fields", []))
    return JsonResponse(result)