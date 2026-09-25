# -*- coding: utf-8 -*-
"""
Table-driven tests for extract_norms._parse_deadline and the unrecognised-phrase log.

Place next to extract_norms.py and run:  pytest test_deadline_parser.py -v

Structure:
  RECOGNISED / FROM_CASES -- behaviour the parser has TODAY. Regression guard.
  KNOWN_GAPS              -- desired behaviour it does NOT have yet, xfail(strict=True).
                             When you implement a fix the test flips to XPASS and strict
                             mode fails the run: move the row into RECOGNISED.
  Add a row for every real phrase from data/stage6_unrecognised_deadlines.json once you
  have decided how it should parse.

Every test runs against a temporary log path (autouse fixture), so nothing here can
touch the real data/ folder.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import extract_norms as en


@pytest.fixture(autouse=True)
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "unrecognised.json"
    monkeypatch.setattr(en, "UNRECOGNISED_LOG_PATH", path)
    monkeypatch.setattr(en, "_unrecognised_seen", None)   # force a fresh lazy load
    monkeypatch.setattr(en, "_log_warned", False)
    return path


def core(parsed):
    """(value, unit, from) -- the fields comparisons actually rely on."""
    if parsed is None:
        return None
    return (parsed["value"], parsed["unit"], parsed["from"])


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Current behaviour
# ---------------------------------------------------------------------------
RECOGNISED = [
    # (id, raw phrase, expected (value, unit, from))
    ("hours_binnen",        "binnen 24 uur",                (24, "hour", None)),
    ("hours_uiterlijk",     "uiterlijk 72 uur",             (72, "hour", None)),
    ("days",                "binnen 5 dagen",               (5, "day", None)),
    ("weeks_plural",        "binnen 2 weken",               (2, "week", None)),
    ("week_singular",       "binnen 1 week",                (1, "week", None)),
    ("months",              "binnen 6 maanden",             (6, "month", None)),
    ("case_insensitive",    "BINNEN 24 UUR",                (24, "hour", None)),
    # Cbw art. 26 shape: vague qualifier first, number second -> number must win.
    ("number_beats_qualifier",
     "onverwijld of, indien dat niet mogelijk is, binnen 24 uur na kennisname",
                                                            (24, "hour", "kennisname")),
    ("qualitative_onverwijld",   "onverwijld",              (None, "qualitative_urgent", None)),
    ("qualitative_onmiddellijk", "onmiddellijk",            (None, "qualitative_urgent", None)),
    ("asap",                "zo spoedig mogelijk",          (None, "asap", None)),
    ("asap_extra_whitespace", "zo  spoedig\nmogelijk",      (None, "asap", None)),
]


@pytest.mark.parametrize("raw,expected", [pytest.param(r, e, id=i) for i, r, e in RECOGNISED])
def test_recognised(raw, expected):
    assert core(en._parse_deadline(raw)) == expected


# ---------------------------------------------------------------------------
# Fix 1: clock-start ("from") capture -- verbatim, subject NOT stripped
# ---------------------------------------------------------------------------
FROM_CASES = [
    ("event_at_end",        "binnen 24 uur na de melding",                       "de melding"),   # was 'g'
    ("event_with_tail",     "binnen 24 uur na de melding van het incident",      "de melding van het incident"),
    ("nadat_hij",           "uiterlijk 72 uur nadat hij er kennis van heeft genomen",
                                                                                 "hij er kennis van heeft genomen"),
    ("nadat_de_aanbieder",  "binnen 24 uur nadat de aanbieder kennis heeft genomen",
                                                                                 "de aanbieder kennis heeft genomen"),
    ("na_het_incident",     "binnen 24 uur na het incident",                     "het incident"),
    ("stops_at_comma",      "binnen 24 uur na kennisname, tenzij anders bepaald", "kennisname"),
    ("stops_at_period",     "binnen 24 uur na kennisname.",                      "kennisname"),
    ("no_false_match_nadere", "binnen 24 uur, nadere regels volgen",             None),
    ("no_from_at_all",      "binnen 24 uur",                                     None),
]


@pytest.mark.parametrize("raw,expected_from", [pytest.param(r, f, id=i) for i, r, f in FROM_CASES])
def test_from_capture(raw, expected_from):
    assert en._parse_deadline(raw)["from"] == expected_from


# ---------------------------------------------------------------------------
# Fix 2: earliest match in the text wins, not first pattern in the list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("binnen 24 uur, uiterlijk 72 uur",  (24, "hour", None)),
    ("binnen 3 dagen, uiterlijk 72 uur", (3, "day", None)),    # was 72 hour (list order)
    ("binnen 72 uur, uiterlijk 3 dagen", (72, "hour", None)),
    ("binnen 2 weken, uiterlijk 5 dagen", (2, "week", None)),  # was 5 day (list order)
])
def test_earliest_bound_wins_and_dropped_bound_is_flagged(raw, expected, log_path):
    assert core(en._parse_deadline(raw)) == expected
    (rec,) = read(log_path)
    assert rec["kind"] == "partial"                            # the second bound is not silent


# ---------------------------------------------------------------------------
# Fix 3: partial parses are logged, not silent
# ---------------------------------------------------------------------------
# NOTE: this phrase relies on "zes" (a spelled-out number) being unsupported. Once number
# words are added to the lexicon, swap it for another still-unsupported figure.
PARTIAL_PHRASE = "onverwijld en uiterlijk binnen zes uur"


def test_qualifier_with_unparsed_number_is_flagged_partial(log_path):
    assert core(en._parse_deadline(PARTIAL_PHRASE)) == (None, "qualitative_urgent", None)
    (rec,) = read(log_path)
    assert rec["kind"] == "partial"
    assert rec["parsed_as"] == {"value": None, "unit": "qualitative_urgent"}


@pytest.mark.parametrize("raw", [
    "onverwijld",
    "zo spoedig mogelijk",
    "onmiddellijk na de melding",
    "onverwijld nadat hij kennis heeft genomen van een incident",   # article 'een' is not a number
])
def test_clean_qualifier_is_not_flagged(raw, log_path):
    en._parse_deadline(raw)
    assert not log_path.exists()


def test_nothing_matched_is_kind_unrecognised(log_path):
    en._parse_deadline(UNPARSEABLE)
    (rec,) = read(log_path)
    assert rec["kind"] == "unrecognised" and rec["parsed_as"] is None


# ---------------------------------------------------------------------------
# Known gaps -- desired behaviour, not implemented yet
# ---------------------------------------------------------------------------
def gap(id_, raw, expected, reason):
    return pytest.param(raw, expected, id=id_, marks=pytest.mark.xfail(strict=True, reason=reason))


KNOWN_GAPS = [
    gap("spelled_out_number",  "binnen zes weken",              (6, "week", None),  "number words unsupported"),
    gap("een_maand",           "binnen een maand",              (1, "month", None), "'een' unsupported"),
    gap("uren_plural",         "binnen 48 uren",                (48, "hour", None), "'uur\\b' does not match 'uren'"),
    gap("year",                "binnen 1 jaar",                 (1, "year", None),  "year unit missing"),
    gap("wrapper_phrase",      "binnen een termijn van 24 uur", (24, "hour", None), "'termijn van' breaks adjacency"),
    gap("working_days",        "binnen 3 werkdagen",            (3, "working_day", None),
        "werkdag currently collapsed into day"),
    gap("calendar_days",       "binnen 3 kalenderdagen",        (3, "day", None),   "kalenderdag unsupported"),
    # Unit names below are proposals from the design discussion, not settled.
    gap("undue_delay_nl",      "zonder onredelijke vertraging", (None, "undue_delay", None), "qualifier not in lexicon"),
    gap("terstond",            "terstond",                      (None, "immediate", None),   "qualifier not in lexicon"),
]


@pytest.mark.parametrize("raw,expected", KNOWN_GAPS)
def test_known_gaps(raw, expected):
    assert core(en._parse_deadline(raw)) == expected


# ---------------------------------------------------------------------------
# Edge inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, ""])
def test_empty_returns_none(raw):
    assert en._parse_deadline(raw) is None


def test_raw_is_preserved():
    assert en._parse_deadline("binnen 24 uur")["raw"] == "binnen 24 uur"


# ---------------------------------------------------------------------------
# Unrecognised-phrase log
# ---------------------------------------------------------------------------
# Deliberately free-form ("at a time to be set by the supervisor"): unlikely to ever
# become a real pattern, so this test won't break as the lexicon grows.
UNPARSEABLE = "op een door de toezichthouder te bepalen tijdstip"


def test_unrecognised_phrase_is_logged(log_path):
    parsed = en._parse_deadline(UNPARSEABLE)
    assert parsed["value"] is None and parsed["unit"] is None
    assert [r["phrase"] for r in read(log_path)] == [UNPARSEABLE]
    assert read(log_path)[0]["first_seen"]


def test_log_is_deduplicated_across_calls_and_whitespace(log_path):
    for variant in (UNPARSEABLE, UNPARSEABLE, UNPARSEABLE.upper(), UNPARSEABLE.replace(" ", "  ")):
        en._parse_deadline(variant)
    assert len(read(log_path)) == 1


def test_recognised_phrases_are_not_logged(log_path):
    for _, raw, _ in RECOGNISED:
        en._parse_deadline(raw)
    assert not log_path.exists()


def test_empty_input_is_not_logged(log_path):
    en._parse_deadline(None)
    en._parse_deadline("")
    assert not log_path.exists()


def test_log_survives_restart(log_path, monkeypatch):
    en._parse_deadline(UNPARSEABLE)
    monkeypatch.setattr(en, "_unrecognised_seen", None)  # simulate a new process
    en._parse_deadline(UNPARSEABLE)                      # must not duplicate
    en._parse_deadline("nog een onherkenbare termijn")
    assert len(read(log_path)) == 2


def test_first_sighting_returns_true_then_false(log_path):
    assert en._log_unrecognised_deadline(UNPARSEABLE) is True
    assert en._log_unrecognised_deadline(UNPARSEABLE) is False


def test_old_format_records_still_load(log_path):
    """Records written before 'kind'/'parsed_as' existed must not be treated as corrupt."""
    log_path.write_text(json.dumps([{"phrase": UNPARSEABLE, "first_seen": "2026-09-24"}]), encoding="utf-8")
    assert en._log_unrecognised_deadline(UNPARSEABLE) is False
    assert not list(log_path.parent.glob("*.corrupt*"))


# ---------------------------------------------------------------------------
# Fix 4: the log can never break extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("content", ["{not json", "{}", '[{"no_phrase_key": 1}]', ""],
                         ids=["invalid_json", "not_a_list", "record_without_phrase", "empty_file"])
def test_corrupt_log_is_set_aside_not_fatal(log_path, content, capsys):
    log_path.write_text(content, encoding="utf-8")
    parsed = en._parse_deadline(UNPARSEABLE)                     # must not raise
    assert parsed is not None
    assert [r["phrase"] for r in read(log_path)] == [UNPARSEABLE]  # fresh, valid log
    assert list(log_path.parent.glob("unrecognised.json.corrupt-*"))  # old content kept
    assert "[deadline log]" in capsys.readouterr().err


def test_unwritable_location_does_not_raise(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, so nothing can be created beneath me")
    monkeypatch.setattr(en, "UNRECOGNISED_LOG_PATH", blocker / "log.json")
    parsed = en._parse_deadline(UNPARSEABLE)                     # must not raise
    assert parsed["value"] is None
    en._parse_deadline("nog een onherkenbare termijn")
    assert capsys.readouterr().err.count("[deadline log]") == 1   # warned once, not per phrase


def test_persist_failure_is_retried_on_next_write(log_path, monkeypatch):
    real = en._persist_unrecognised_log
    calls = {"n": 0}

    def flaky(records):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        real(records)

    monkeypatch.setattr(en, "_persist_unrecognised_log", flaky)
    en._parse_deadline(UNPARSEABLE)                  # write fails, record kept in memory
    en._parse_deadline("nog een onherkenbare termijn")   # next write succeeds and includes both
    assert {r["phrase"] for r in read(log_path)} == {UNPARSEABLE, "nog een onherkenbare termijn"}


def test_write_is_atomic_no_temp_file_left_behind(log_path):
    en._parse_deadline(UNPARSEABLE)
    assert list(log_path.parent.glob("*.tmp")) == []


def test_concurrent_logging_keeps_every_phrase(log_path):
    phrases = [f"onherkenbare termijn nummer {n}" for n in range(60)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(en._parse_deadline, phrases * 2))    # each phrase twice, interleaved
    assert len(read(log_path)) == 60                      # valid JSON, none lost, none duplicated