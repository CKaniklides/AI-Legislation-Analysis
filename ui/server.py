# -*- coding: utf-8 -*-
"""
Local server for the review interface. Standard library only.

    python ui/server.py            # then open http://localhost:8765

Serves the files in ui/ behind a sign-in (accounts in data/review/users.json, see
"accounts" below), and stores reviewer decisions in data/review/decisions.json.
Every change is also appended to data/review/decisions_log.jsonl, so an overwritten or
withdrawn decision is never lost.

Reviews are private: each file is keyed by account email, and a reviewer only ever receives
their own decisions, line reviews and chapter settings. Everyone's are kept in the files.

The plain-language laws are reviewed line by line: data/review/sheet_reviews.json holds one
entry per line (approve, decline with a reason, and/or a reworded text), with the same kind
of append-only log. data/review/sheet_settings.json holds how much of each chapter goes into
the public document.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI = Path(__file__).resolve().parent
ROOT = UI.parent
REVIEW = ROOT / "data" / "review"
DECISIONS = REVIEW / "decisions.json"
LOG = REVIEW / "decisions_log.jsonl"
SHEET_REVIEWS = REVIEW / "sheet_reviews.json"        # simplified laws: one entry per line
SHEET_LOG = REVIEW / "sheet_reviews_log.jsonl"
SHEET_SETTINGS = REVIEW / "sheet_settings.json"      # per chapter: full / gist / hide in the public document
PUBLIC_LEVELS = {"full", "gist", "hide"}
_SHEET_LOCK = threading.Lock()                       # several lines can be saved at once
PORT = int(os.environ.get("REVIEW_PORT", "8765"))


# ---------- accounts ----------
# Passwords are never stored: only a salted PBKDF2 hash. A session is a random token in an
# HttpOnly cookie; the server keeps only the token's SHA-256. Both files stay out of git.
USERS = REVIEW / "users.json"
SESSIONS = REVIEW / "sessions.json"
COOKIE = f"lr_session_{PORT}"          # per port, so two local copies of the tool keep separate sign-ins
SESSION_SECONDS = 14 * 24 * 3600
MIN_PASSWORD = 8
# Test mode (on while the tool is being tested): nobody has to sign in. A first visit gets its own
# anonymous reviewer ("Reviewer 3"), remembered by a cookie in that browser, so reviews still stay
# private per person. The sign-in form, if used, accepts any email and password. Start the server
# with REVIEW_DEMO_LOGIN=0 to require real accounts.
DEMO_LOGIN = os.environ.get("REVIEW_DEMO_LOGIN", "1") != "0"
GUEST_SECONDS = 365 * 24 * 3600         # a guest is one browser; a short session would lose their reviews
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_AUTH_LOCK = threading.Lock()
_FAILED = {}                            # email -> (wrong tries, since); five in a minute locks that email for the minute


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, obj: dict) -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def _now() -> int:
    return int(time.time())

DECLINE_REASONS = {"not_a_conflict", "wrong_provisions", "outdated_text", "needs_context", "other"}
MIN_JUSTIFICATION = 10
MAX_COMMENT = 2000                      # a free remark on a plain-language line, or a note on what a section misses


def load_decisions(path: Path = DECISIONS) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_decisions(d: dict, path: Path = DECISIONS) -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_mine(path: Path, email: str) -> dict:
    """Every reviewer's decisions are kept, but each reviewer only ever sees their own.
    The files are keyed by account email; entries from before accounts existed sit under
    keys without an "@" and are kept but shown to nobody."""
    return load_decisions(path).get(email, {})


def save_mine(path: Path, email: str, mine: dict) -> None:
    everyone = load_decisions(path)
    if mine:
        everyone[email] = mine
    else:
        everyone.pop(email, None)
    save_decisions(everyone, path)


def validate_sheet(body: dict) -> "str | None":
    """A line of a simplified law: approve, decline (reason required) and/or reword per language."""
    if not isinstance(body.get("id"), str) or not body["id"]:
        return "Missing line id."
    if body.get("decision") not in ("accept", "decline", None):
        return "Decision must be accept, decline, or null to withdraw."
    # "Decline all" (testing and demos) is the one decline that does not need a written reason.
    if body.get("decision") == "decline" and not body.get("bulk") and len((body.get("justification") or "").strip()) < MIN_JUSTIFICATION:
        return f"Explain why you are declining (at least {MIN_JUSTIFICATION} characters)."
    edit = body.get("edit")
    if edit is not None and not (isinstance(edit, dict) and edit and set(edit) <= {"nl", "en"}
                                 and all(isinstance(v, str) and v.strip() for v in edit.values())):
        return "A reworded line needs text in Dutch and/or English."
    comment = body.get("comment")
    if comment is not None and not (isinstance(comment, str) and len(comment) <= MAX_COMMENT):
        return f"A comment is text of at most {MAX_COMMENT} characters."
    return None


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

    def _json(self, code: int, payload, cookie: str = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _do_get(self):
        if self.path.split("?")[0] == "/api/decisions":
            return self._json(200, load_mine(DECISIONS, self._me["email"]))
        if self.path.split("?")[0] == "/api/sheet_reviews":
            return self._json(200, load_mine(SHEET_REVIEWS, self._me["email"]))
        if self.path.split("?")[0] == "/api/sheet_settings":
            return self._json(200, load_mine(SHEET_SETTINGS, self._me["email"]))
        return super().do_GET()

    def _post_sheet(self, body: dict):
        """One line, or {"items": [...]} to save several lines in one go (approve a whole page)."""
        items = body.get("items") if isinstance(body.get("items"), list) else [body]
        for it in items:
            err = validate_sheet(it) if isinstance(it, dict) else "Each item must be an object."
            if err:
                return self._json(400, {"error": err})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        email = self._me["email"]
        entries = {}
        with _SHEET_LOCK:
            reviews = load_mine(SHEET_REVIEWS, email)
            for it in items:
                entry = None
                comment = (it.get("comment") or "").strip() or None   # a free remark, with or without a decision
                if it.get("decision") or it.get("edit") or comment:
                    entry = {
                        "decision": it.get("decision"),
                        "justification": (it.get("justification") or "").strip() or None if it.get("decision") == "decline" else None,
                        "edit": {k: v.strip() for k, v in it["edit"].items()} if it.get("edit") else None,
                        "comment": comment,
                        "reviewer": (it.get("reviewer") or "").strip() or None,
                        "decided_at": now,
                    }
                    reviews[it["id"]] = entry
                else:
                    reviews.pop(it["id"], None)
                entries[it["id"]] = entry
            save_mine(SHEET_REVIEWS, email, reviews)
            with SHEET_LOG.open("a", encoding="utf-8") as fh:
                for key, entry in entries.items():
                    fh.write(json.dumps({"id": key, "at": now, "email": email, "entry": entry}, ensure_ascii=False) + "\n")
        if "items" in body:
            return self._json(200, {"ok": True, "entries": entries})
        return self._json(200, {"ok": True, "entry": entries[body["id"]]})

    def _post_setting(self, body: dict):
        if not isinstance(body.get("id"), str) or not body["id"] or body.get("value") not in PUBLIC_LEVELS | {None}:
            return self._json(400, {"error": "A setting needs an id and a value of full, gist or hide."})
        with _SHEET_LOCK:
            settings = load_mine(SHEET_SETTINGS, self._me["email"])
            if body["value"] is None:
                settings.pop(body["id"], None)
            else:
                settings[body["id"]] = body["value"]
            save_mine(SHEET_SETTINGS, self._me["email"], settings)
        return self._json(200, {"ok": True})

    def _do_post(self):
        if self.path not in ("/api/decisions", "/api/sheet_reviews", "/api/sheet_settings"):
            return self._json(404, {"error": "Not found."})
        try:
            body = json.loads(self._body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Request was not valid JSON."})
        if self.path == "/api/sheet_reviews":
            return self._post_sheet(body)
        if self.path == "/api/sheet_settings":
            return self._post_setting(body)
        err = validate(body)
        if err:
            return self._json(400, {"error": err})

        email = self._me["email"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _SHEET_LOCK:
            decisions = load_mine(DECISIONS, email)
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
            save_mine(DECISIONS, email, decisions)
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": body["id"], "at": now, "email": email, "entry": entry}, ensure_ascii=False) + "\n")
        return self._json(200, {"ok": True, "entry": entry})

    # ---------- accounts ----------
    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                return v
        return None

    def _user(self):
        token = self._token()
        if not token:
            return None
        s = _read(SESSIONS).get(hashlib.sha256(token.encode()).hexdigest())
        if not s or s["expires"] < _now():
            return None
        u = _read(USERS).get(s["email"])
        return {"name": u["name"], "email": s["email"], "guest": bool(u.get("guest"))} if u else None

    def _start_session(self, email: str, payload: dict, seconds: int = SESSION_SECONDS):
        token = secrets.token_urlsafe(32)
        with _AUTH_LOCK:
            sessions = {k: v for k, v in _read(SESSIONS).items() if v["expires"] > _now()}
            sessions[hashlib.sha256(token.encode()).hexdigest()] = {"email": email, "expires": _now() + seconds}
            _write(SESSIONS, sessions)
        return self._json(200, payload, cookie=f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={seconds}")

    def _start_guest(self):
        """Test mode: an anonymous reviewer for this browser, so nobody has to type anything."""
        with _AUTH_LOCK:
            users = _read(USERS)
            email = f"guest-{secrets.token_hex(6)}@guest"
            name = f"Reviewer {sum(1 for u in users.values() if u.get('guest')) + 1}"
            users[email] = {"name": name, "guest": True, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _write(USERS, users)
        return self._start_session(email, {"name": name, "email": email, "guest": True}, GUEST_SECONDS)

    def _auth_post(self, path: str, body: dict):
        if path == "/api/logout":
            token = self._token()
            if token:
                with _AUTH_LOCK:
                    sessions = _read(SESSIONS)
                    sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
                    _write(SESSIONS, sessions)
            return self._json(200, {"ok": True}, cookie=f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if DEMO_LOGIN:
            email = email or "test@example.nl"
            name = " ".join(str(body.get("name") or "").split())[:80] or email.split("@")[0].replace(".", " ").title() or "Test user"
            with _AUTH_LOCK:
                users = _read(USERS)
                if email not in users:
                    salt = secrets.token_hex(16)
                    users[email] = {"name": name, "salt": salt, "hash": _hash(password or "test", salt),
                                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "test_account": True}
                    _write(USERS, users)
                name = users[email]["name"]
            return self._start_session(email, {"name": name, "email": email})
        if not EMAIL.match(email) or not isinstance(password, str):
            return self._json(400, {"code": "invalid"})
        if path == "/api/signup":
            name = " ".join(str(body.get("name") or "").split())[:80]
            if not name:
                return self._json(400, {"code": "invalid_name"})
            if len(password) < MIN_PASSWORD:
                return self._json(400, {"code": "weak"})
            with _AUTH_LOCK:
                users = _read(USERS)
                if email in users:
                    return self._json(409, {"code": "exists"})
                salt = secrets.token_hex(16)
                users[email] = {"name": name, "salt": salt, "hash": _hash(password, salt),
                                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                _write(USERS, users)
            return self._start_session(email, {"name": name, "email": email})
        fails, since = _FAILED.get(email, (0, _now()))
        if _now() - since >= 60:
            fails, since = 0, _now()
        if fails >= 5:
            return self._json(429, {"code": "locked"})
        u = _read(USERS).get(email)
        if not u or not hmac.compare_digest(u["hash"], _hash(password, u["salt"])):
            _FAILED[email] = (fails + 1, since)
            return self._json(401, {"code": "bad_login"})
        _FAILED.pop(email, None)
        return self._start_session(email, {"name": u["name"], "email": email})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/me":
            u = self._user()
            if u:
                return self._json(200, u)
            return self._start_guest() if DEMO_LOGIN else self._json(401, {"code": "signed_out", "demo": False})
        # The page itself loads for everyone (it shows the sign-in screen); the data does not.
        self._me = self._user()
        if (path.startswith("/api/") or path.startswith("/data/")) and not self._me:
            return self._json(401, {"code": "signed_out"})
        return self._do_get()

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or "0"))
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "Request was not valid JSON."})
        if path in ("/api/signup", "/api/login", "/api/logout"):
            return self._auth_post(path, body if isinstance(body, dict) else {})
        user = self._me = self._user()
        if not user:
            return self._json(401, {"code": "signed_out"})
        # Decisions are recorded under the signed-in account, never under a name typed in the page.
        if isinstance(body, dict):
            body["reviewer"] = user["name"]
            for it in body.get("items") or []:
                if isinstance(it, dict):
                    it["reviewer"] = user["name"]
        self._body = json.dumps(body).encode("utf-8")
        return self._do_post()

    def log_message(self, fmt, *args):
        if "/api/" in (str(args[0]) if args else ""):  # args[0] is a status code when logging an error
            sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    if not (UI / "data" / "review_data.json").exists():
        sys.exit("ui/data/review_data.json is missing. Run: python ui/build_review_data.py")
    print(f"Review interface: http://localhost:{PORT}   (Ctrl+C to stop)")
    if DEMO_LOGIN:
        print("Test mode: any email and password signs in. Set REVIEW_DEMO_LOGIN=0 to require real accounts.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
