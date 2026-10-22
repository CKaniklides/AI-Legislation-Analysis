# -*- coding: utf-8 -*-
"""
Runs the review interface on a WSGI host such as PythonAnywhere, without changing server.py.

Each request is handed to server.Handler exactly as the local server would, with the
request and response kept in memory instead of on a network socket. Sign-in, decisions and
the files in data/review/ therefore behave the same as on a laptop.

On PythonAnywhere, the web app's WSGI configuration file only needs:

    import sys
    sys.path.insert(0, "/home/<username>/AI-Legislation-Analysis/ui")
    from wsgi import application
"""
import email.message
import io
import os
import sys
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


class _Request(server.Handler):
    """server.Handler without a socket: set up from a WSGI environ, answers into a buffer."""

    def __init__(self, environ):  # deliberately skips BaseRequestHandler.__init__ (no socket)
        self.directory = str(server.UI)
        self.client_address = (environ.get("REMOTE_ADDR", ""), 0)
        self.server = None
        self.command = environ["REQUEST_METHOD"]
        query = environ.get("QUERY_STRING")
        self.path = environ.get("PATH_INFO") or "/"
        if query:
            self.path += "?" + query
        self.request_version = "HTTP/1.1"
        self.requestline = f"{self.command} {self.path} {self.request_version}"
        self.close_connection = True
        headers = email.message.Message()
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers[key[5:].replace("_", "-").title()] = value
        if environ.get("CONTENT_TYPE"):
            headers["Content-Type"] = environ["CONTENT_TYPE"]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length:
            headers["Content-Length"] = str(length)
        self.headers = headers
        self.rfile = io.BytesIO(environ["wsgi.input"].read(length) if length else b"")
        self.wfile = io.BytesIO()


def _split(raw: bytes):
    """The handler wrote a full HTTP response; WSGI wants status, headers and body apart."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = lines[0].split(" ", 1)[1]
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.lower() not in ("connection", "server", "date"):  # the host sets these itself
            headers.append((name, value.strip()))
    return status, headers, body


def application(environ, start_response):
    req = _Request(environ)
    method = getattr(req, "do_" + req.command, None)
    if method is None:
        status = f"{HTTPStatus.METHOD_NOT_ALLOWED.value} {HTTPStatus.METHOD_NOT_ALLOWED.phrase}"
        start_response(status, [("Content-Type", "text/plain")])
        return [b"Method not allowed"]
    method()
    status, headers, body = _split(req.wfile.getvalue())
    start_response(status, headers)
    return [body] if req.command != "HEAD" else [b""]
