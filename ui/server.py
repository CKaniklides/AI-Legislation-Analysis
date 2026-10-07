# -*- coding: utf-8 -*-
"""
Local server for the review interface. Standard library only.

    python ui/server.py            # then open http://localhost:8765

Serves the files in ui/ and stores reviewer decisions in data/review/decisions.json.
Every change is also appended to data/review/decisions_log.jsonl, so an overwritten or
withdrawn decision is never lost.
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI = Path(__file__).resolve().parent
ROOT = UI.parent
REVIEW = ROOT / "data" / "review"
DECISIONS = REVIEW / "decisions.json"
LOG = REVIEW / "decisions_log.jsonl"
PORT = int(os.environ.get("REVIEW_PORT", "8765"))

DECLINE_REASONS = {"not_a_conflict", "wrong_provisions", "outdated_text", "needs_context", "other"}
MIN_JUSTIFICATION = 10


def load_decisions() -> dict:
    if DECISIONS.exists():
        return json.loads(DECISIONS.read_text(encoding="utf-8"))
    return {}


def save_decisions(d: dict) -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    tmp = DECISIONS.with_name(DECISIONS.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DECISIONS)


def validate(body: dict) -> "str | None":
    if not isinstance(body.get("id"), str) or not body["id"]:
        return "Missing finding id."
    decision = body.get("decision")
    if decision not in ("accept", "decline", None):
        return "Decision must be accept, decline, or null to withdraw."
    if decision == "decline":
        if body.get("reason") not in DECLINE_REASONS:
            return "Choose a reason for declining."
        if len((body.get("justification") or "").strip()) < MIN_JUSTIFICATION:
            return f"Explain why you are declining (at least {MIN_JUSTIFICATION} characters)."
    return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI), **kw)

    def _json(self, code: int, payload) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/api/decisions":
            return self._json(200, load_decisions())
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/decisions":
            return self._json(404, {"error": "Not found."})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Request was not valid JSON."})
        err = validate(body)
        if err:
            return self._json(400, {"error": err})

        decisions = load_decisions()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if body["decision"] is None:
            decisions.pop(body["id"], None)
            entry = None
        else:
            entry = {
                "decision": body["decision"],
                "reason": body.get("reason") if body["decision"] == "decline" else None,
                "justification": (body.get("justification") or "").strip() or None,
                "reviewer": (body.get("reviewer") or "").strip() or None,
                "question_id": body.get("question_id"),
                "decided_at": now,
            }
            decisions[body["id"]] = entry
        save_decisions(decisions)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": body["id"], "at": now, "entry": entry}, ensure_ascii=False) + "\n")
        return self._json(200, {"ok": True, "entry": entry})

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    if not (UI / "data" / "review_data.json").exists():
        sys.exit("ui/data/review_data.json is missing. Run: python ui/build_review_data.py")
    print(f"Review interface: http://localhost:{PORT}   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
