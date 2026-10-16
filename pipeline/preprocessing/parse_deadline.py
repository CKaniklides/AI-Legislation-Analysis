
import json, os, re, sys
import threading
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

# Windows consoles routinely default to a legacy codepage (e.g. cp1253) that can't
# encode the Dutch legal text this script prints (curly quotes, non-breaking spaces,
# accented characters) -- confirmed directly: a real run crashed mid-way through
# resolve_queue_conservatively() on an ordinary print() over real deadline text,
# losing no data (writes had already happened) but stopping before completion for a
# reason that had nothing to do with the extraction logic itself.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # picks up OPENAI_API_KEY from a .env file in the project root


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

# ---------------------------------------------------------------------------
# Deterministic deadline parser. First-pass lexicon, same spirit as features.py's
# RARITY_CUTOFF -- covers the phrasing actually seen in the anchor set (Cbw ch. 8,
# GDPR 33/34, NIS2 23, DORA 17-23) and should be extended, not trusted as exhaustive,
# once the review queue surfaces phrasing it doesn't recognise.
# ---------------------------------------------------------------------------

_UNIT_WORD = {
    "uur": 'hour',
    "uren": 'hour',
    'dag': 'day',
    'dagen': 'day',
    'werkdag': 'working day',
    'werkdagen': 'working day',
    "kalenderdag": 'day',
    "kalenderdagen": 'day',
    'week': 'week',
    'weken': "week",
    "maand": 'month',
    "maanden": 'month',
    "jaar": 'year',
    "jaren" : 'year'
}

_NUMBER_WORD = {
    "twee": 2,
    "drie": 3,
    "vier": 4,
    "vijf": 5,
    "zes": 6,
    "zeven": 7,
    "acht": 8,
    "negen": 9,
    "tien": 10,
    "elf": 11,
    "twaalf": 12,
    "vierentwintig": 24,
    "achtenveertig": 48,
    "tweeënzeventig": 72,
    "tweeenzeventig": 72,
}
_NUMBER_WORD_RE = "|".join(re.escape(word)for word in _NUMBER_WORD)
_UNIT_WORD_RE = "|".join(re.escape(word) for word in _UNIT_WORD)
_NUMBER = rf"(?:\d+|{_NUMBER_WORD_RE})"
_WORD = rf"(?:{_UNIT_WORD_RE})"

def _parse_number(value: str) -> Optional[int]:
    """Convert a deadline number to an integer."""
    try:
        value = value.strip().lower()
        if value.isdigit():
            return int(value)
        return _NUMBER_WORD.get(value)
    except Exception as e:
        _warn_log_problem(f"could not parse deadline number {value!r}: {e!r}")
        return None

def _parse_unit(value: str):
    try:
        return _UNIT_WORD.get(value)
    except Exception as e:
        _warn_log_problem(f"could not parse unit {value!r}: {e!r}")
        return None


def _extract_numeric_deadline(m: re.Match) -> dict:
    """Extract a numeric deadline without allowing errors to escape."""
    try:
        number = _parse_number(m.group(2))
        unit = _parse_unit(m.group(3).lower())
        return {"value": number, "unit": unit, "operator": m.group(1).lower(),}
    except Exception as e:
        _warn_log_problem(f"deadline extraction failed for " f"{m.group(0)!r}: {e!r}")
        return {"value": None,"unit": None,"operator": None,}


_NUMERIC_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], dict]]] = [
    (re.compile(rf"\b(binnen|uiterlijk)\s+({_NUMBER})\s+({_WORD})\b",re.I,),
    _extract_numeric_deadline,),]

_QUALITATIVE_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], dict]]
] = [
    (re.compile(r"\bonverwijld\b", re.I), lambda m: {"value": None,"unit": "qualitative_urgent",},),
    (re.compile(r"\bonmiddellijk\b", re.I),lambda m: {"value": None,"unit": "qualitative_urgent",},),
    (re.compile(r"\bzo\s+spoedig\s+mogelijk\b",re.I,),lambda m: {"value": None,"unit": "asap",},),
]

_DEADLINE_PATTERNS = (_NUMERIC_PATTERNS + _QUALITATIVE_PATTERNS)

_FROM_RE = re.compile(r"\bna(?:dat)?\s+(.+?)(?:[,.;]|$)",re.I,)
_NUMERIC_HINT_RE = re.compile(rf"\b{_NUMBER}\b",re.I,)



UNRECOGNISED_LOG_PATH = (ROOT/ "data"/ "stage6_unrecognised_deadlines.json")

_unrecognised_lock = threading.Lock()
_unrecognised_seen: Optional[dict] = None
_log_warned = False


def _warn_log_problem(msg: str) -> None:
    """Report a logging problem once.
    Logging problems are reported to stderr but never raised.
    """
    global _log_warned
    if not _log_warned:
        _log_warned = True
        print(f"[deadline log] {msg}", file=sys.stderr,)


def _load_unrecognised_log() -> dict:
    """Load the persistent unrecognized-deadline log.

    A missing file is treated as an empty log.

    A malformed or unreadable file is renamed aside and a
    fresh empty log is returned.

    This function never raises.
    """
    try:
        if not UNRECOGNISED_LOG_PATH.exists():
            return {}
        records = json.loads(UNRECOGNISED_LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("expected a JSON list of records")
        valid_records = {}
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get("phrase"),str,)):
                raise ValueError("each log record must contain a string 'phrase'")

            valid_records[_normalize(record["phrase"])] = record
        return valid_records

    except Exception as e:
        _warn_log_problem(
            f"{UNRECOGNISED_LOG_PATH.name} unreadable "
            f"({e!r}); setting it aside and starting "
            f"a fresh log"
        )

        try:
            UNRECOGNISED_LOG_PATH.replace(
                UNRECOGNISED_LOG_PATH.with_name(
                    f"{UNRECOGNISED_LOG_PATH.name}"
                    f".corrupt-"
                    f"{date.today().isoformat()}"
                )
            )
        except Exception:
            pass

        return {}


def _persist_unrecognised_log(records: dict,) -> None:
    """Persist the log atomically.

    A temporary sibling file is written first and thenreplaced over the real log file.
    This function may raise; callers are responsible for handling persistence failures.
    """
    UNRECOGNISED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True,)

    tmp = UNRECOGNISED_LOG_PATH.with_name(f"{UNRECOGNISED_LOG_PATH.name}. {os.getpid()}.tmp")

    tmp.write_text(json.dumps(
            sorted(
                records.values(),
                key=lambda r: r["phrase"],
            ),
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    tmp.replace(UNRECOGNISED_LOG_PATH)


def _log_unrecognised_deadline(raw: str, kind: str = "unrecognised", parsed_as: Optional[dict] = None,) -> bool:
    """Record a deadline phrase once.
    Deduplication is based on normalized text.

    Returns:
        True  - phrase was newly recorded
        False - phrase was already recorded or logging failed

    This function never raises.
    kind:
        "unrecognised" - nothing matched.
        "partial"      - something matched, but additional
                         numeric information remained.

    If persistence fails, the record remains in memory and can be persisted by a later successful call.
    """
    global _unrecognised_seen

    try:
        key = _normalize(raw)

        with _unrecognised_lock:

            if _unrecognised_seen is None:
                _unrecognised_seen = (_load_unrecognised_log())

            if key in _unrecognised_seen:
                return False

            _unrecognised_seen[key] = {
                "phrase": raw,
                "kind": kind,
                "parsed_as": parsed_as,
                "first_seen": date.today().isoformat(),
            }

            try:
                _persist_unrecognised_log(_unrecognised_seen)

            except Exception as e:
                _warn_log_problem(f"could not write {UNRECOGNISED_LOG_PATH} ({e!r}); "
                                  f"continuing without persisting the log")
            return True
        
    except Exception as e:
        # Final safety net for the logging subsystem.
        _warn_log_problem(f"unexpected logging error ({e!r})")
        return False


def _earliest_hit(patterns,raw: str,):
    """Return the earliest matching pattern.

    If multiple patterns match, the match with the earliest starting position wins.
    If two matches start at the same position, the pattern appearing first in the list wins.

    Returns:
        (match, extractor)
        or None
    """
    hits = []

    for i, (pattern, extractor) in enumerate(patterns):
        m = pattern.search(raw)
        if m:
            hits.append((m.start(),i,m,extractor,))
    if not hits:
        return None

    _, _, m, extractor = min(hits,key=lambda h: (h[0], h[1]),)
    return m, extractor


def _parse_deadline(raw: Optional[str],) -> Optional[dict]:
    """Parse a natural-language deadline safely. The parser is deliberately non-fatal. It returns None for empty input.
    For recognized input, it returns the parsed structure. For unrecognized or partially recognized input, it
    returns the best-effort structure and records the problem in the diagnostic log.
    Unexpected parser errors are caught so they cannot terminate the caller, worker thread, or processing pipeline.
    """

    if not raw:
        return None

    parsed = {
        "value": None,
        "unit": None,
        "from": None,
        "raw": raw,
    }

    try:
        hit = _earliest_hit(_DEADLINE_PATTERNS,raw,)

        if hit is None:
            if _log_unrecognised_deadline(raw,kind="unrecognised",):
                print("    [deadline parser] unrecognised phrasing, logged to "
                    f"{UNRECOGNISED_LOG_PATH.name}: {raw!r}")

        else:
            m, extractor = hit
            try:
                parsed.update(extractor(m))
            except Exception as e:
                _warn_log_problem(f"deadline extractor failed for {raw!r}: {e!r}")

            # Check whether another numeric expression
            # remains outside the matched span.
            try:
                leftover = (raw[:m.start()] + " " + raw[m.end():])
                if _NUMERIC_HINT_RE.search(leftover):
                    if _log_unrecognised_deadline(raw,kind="partial",
                                                  parsed_as={"value": parsed["value"],"unit": parsed["unit"],},):
                        print(
                            "[deadline parser] partial parse (a figure was left unaccounted for), "
                            f"logged to {UNRECOGNISED_LOG_PATH.name}: "f"{raw!r}")
            except Exception as e:
                _warn_log_problem(f"deadline partial-parse check failed for {raw!r}: {e!r}")

        # Parse the "na..." / "nadat..." part.
        try:
            from_m = _FROM_RE.search(raw)
            if from_m:
                parsed["from"] = (from_m.group(1).strip())
        except Exception as e:
            _warn_log_problem(f"deadline 'from' parsing failed for {raw!r}: {e!r}")
        return parsed

    except Exception as e:
        _warn_log_problem(f"unexpected deadline parser error for {raw!r}: {e!r}")
        return parsed
