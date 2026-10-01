# -*- coding: utf-8 -*-
"""
Positive-control check for the strict C1 adjudicator (2026-09-28, diagnosing-bugs Phase 4 probe).
"Zero CONTRADICTION verdicts in ~11,500 pairs" only means something if the adjudicator CAN return
CONTRADICTION. This feeds it invented but unambiguous conflicts in realistic Dutch drafting (and two
negative controls that must NOT fire), in both A/B orders. Fictional instruments, no cache read or write.
Calls: 2 orders x 6 pairs = 12, plus a challenge call per proposed CONTRADICTION.
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
import detect_c1_contradiction as c1
from dotenv import load_dotenv


def rec(inst, art, text, deontic, action, addressee="aanbieders"):
    norm = {"deontic": deontic, "action": action, "conditions": [], "addressee": addressee,
            "addressee_type": "REGULATED_ENTITY", "trigger_event": None, "number": "1"}
    return c1.NormRecord(norm, {}, inst, art, f"Artikel {art}", text, None)


CASES = [
    ("MUST keep 10y vs MUST delete after 2y", "positive",
     rec("FICT-A", "1", "Aanbieders van AI-systemen met een hoog risico bewaren de door hun systemen automatisch gegenereerde logbestanden gedurende ten minste tien jaar.", "OBLIGATION", "bewaren logbestanden gedurende ten minste tien jaar"),
     rec("FICT-B", "2", "Aanbieders van AI-systemen met een hoog risico vernietigen de door hun systemen automatisch gegenereerde logbestanden uiterlijk twee jaar nadat zij zijn gegenereerd.", "OBLIGATION", "vernietigen logbestanden uiterlijk twee jaar na het genereren")),
    ("report NOT BEFORE 72h vs report WITHIN 24h", "positive",
     rec("FICT-A", "3", "Een entiteit meldt een significant incident niet eerder dan 72 uur nadat zij daarvan kennis heeft genomen aan de bevoegde autoriteit.", "OBLIGATION", "melden incident niet eerder dan 72 uur na kennisname"),
     rec("FICT-B", "4", "Een entiteit meldt een significant incident binnen 24 uur nadat zij daarvan kennis heeft genomen aan de bevoegde autoriteit.", "OBLIGATION", "melden incident binnen 24 uur na kennisname")),
    ("MUST provide copy vs MUST NOT provide copy", "positive",
     rec("FICT-A", "5", "De verwerkingsverantwoordelijke verstrekt de betrokkene op diens verzoek een kopie van alle persoonsgegevens die over hem worden verwerkt.", "OBLIGATION", "verstrekken kopie van alle persoonsgegevens aan de betrokkene", "verwerkingsverantwoordelijke"),
     rec("FICT-B", "6", "Het is de verwerkingsverantwoordelijke verboden om aan de betrokkene een kopie van de over hem verwerkte persoonsgegevens te verstrekken.", "PROHIBITION", "verstrekken kopie van persoonsgegevens aan de betrokkene", "verwerkingsverantwoordelijke")),
    ("MUST publish documentation vs MUST keep confidential", "positive",
     rec("FICT-A", "7", "Aanbieders publiceren de volledige technische documentatie van hun AI-systeem op hun website.", "OBLIGATION", "publiceren volledige technische documentatie op website"),
     rec("FICT-B", "8", "Aanbieders houden de technische documentatie van hun AI-systeem vertrouwelijk en geven deze niet vrij aan derden.", "OBLIGATION", "vertrouwelijk houden technische documentatie en niet vrijgeven aan derden")),
    ("CONTROL stricter deadline satisfies looser", "control",
     rec("FICT-A", "9", "Een entiteit meldt een significant incident uiterlijk 72 uur nadat zij daarvan kennis heeft genomen aan de bevoegde autoriteit.", "OBLIGATION", "melden incident uiterlijk 72 uur na kennisname"),
     rec("FICT-B", "10", "Een entiteit meldt een significant incident binnen 24 uur nadat zij daarvan kennis heeft genomen aan de bevoegde autoriteit.", "OBLIGATION", "melden incident binnen 24 uur na kennisname")),
    ("CONTROL unrelated duties, different actors", "control",
     rec("FICT-A", "11", "De toezichthouder publiceert jaarlijks een verslag over zijn werkzaamheden.", "OBLIGATION", "publiceren jaarverslag", "toezichthouder"),
     rec("FICT-B", "12", "Aanbieders bewaren de logbestanden van hun systemen gedurende ten minste zes maanden.", "OBLIGATION", "bewaren logbestanden gedurende ten minste zes maanden")),
]


def main():
    load_dotenv(c1.ROOT / ".env")
    from openai import OpenAI
    client = OpenAI()
    fired = {"positive": 0, "control": 0}
    n = {"positive": 0, "control": 0}
    for name, kind, a, b in CASES:
        for label, (x, y) in (("A,B", (a, b)), ("B,A", (b, a))):
            r = c1.adjudicate_duty_conflict(client, c1.DEFAULT_MODEL, x, y)
            v = r.verdict
            ch = r.challenge.verdict if r.challenge is not None and hasattr(r.challenge, "verdict") else None
            n[kind] += 1
            fired[kind] += v.verdict == "CONTRADICTION"
            print(f"[{kind:8s}] {name:48s} {label}: {v.verdict:20s} joint_compliance_possible={v.joint_compliance_possible!s:5s}"
                  f" challenge={ch} recheck={bool(r.needs_recheck_reason)}")
    print(f"\npositives returned CONTRADICTION: {fired['positive']}/{n['positive']}   controls (must be 0): {fired['control']}/{n['control']}")


if __name__ == "__main__":
    main()
