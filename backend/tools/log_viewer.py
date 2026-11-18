"""
tradeos log viewer — tiny stdlib-only HTTP server that tails the daemon log file.

Runs as its own systemd service on the Oracle server (24/7, unlike the intraday
daemon which exits at 15:40). Serve today's log over HTTP so you can open one
bookmarked URL from your phone or laptop — no Supabase writes, no DB migration.

Endpoints (all under BASE_PATH, default /):
    GET {BASE_PATH}/                         HTML page — last N lines of today's log, auto-refresh
    GET {BASE_PATH}/api/logs                 JSON — {"date": ..., "lines": [...], "count": ...}
                                              Query: ?date=YYYY-MM-DD  (default today)
                                                     ?lines=200        (default 200, max 2000)
    GET {BASE_PATH}/health                   plain "ok" — for uptime checks

Auth:
    Basic Auth. Password from LOG_VIEWER_PASSWORD env var. If unset, no auth.

Env vars:
    LOG_VIEWER_PORT        port to listen on (default 8765)
    LOG_VIEWER_BASE_PATH  URL path prefix (default "", e.g. "/logs")
    LOG_VIEWER_PASSWORD   basic auth password (required if you expose this publicly)
    LOG_LEVEL             forwarded only for display, daemon still owns logging

Run:
    LOG_VIEWER_PASSWORD=changeme LOG_VIEWER_BASE_PATH=/logs LOG_VIEWER_PORT=3000 \
        python -m tools.log_viewer
"""

from __future__ import annotations

import base64
import datetime as _dt_module
import html
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Config ───────────────────────────────────────────────────────────────────

BACKEND = Path(__file__).resolve().parent.parent
LOGS_DIR = BACKEND / "logs"
DEFAULT_PORT = int(os.getenv("LOG_VIEWER_PORT", "8765"))
BASE_PATH = os.getenv("LOG_VIEWER_BASE_PATH", "").strip()
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH
if BASE_PATH and BASE_PATH.endswith("/"):
    BASE_PATH = BASE_PATH.rstrip("/")
PASSWORD = os.getenv("LOG_VIEWER_PASSWORD", "")
MAX_LINES = 2000
DEFAULT_LINES = 200
REFRESH_SEC = 3


def _auth_ok(headers) -> bool:
    if not PASSWORD:
        return True
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
    except Exception:
        return False
    return decoded.split(":", 1)[-1] == PASSWORD


def _today_ist() -> str:
    import pytz  # already a project dependency
    ist = pytz.timezone("Asia/Kolkata")
    return datetime.now(ist).strftime("%Y-%m-%d")


def _log_path(date_str: str) -> Path:
    return LOGS_DIR / f"tradeos_{date_str}.log"


def _read_tail(date_str: str, lines: int) -> list[str]:
    path = _log_path(date_str)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
    except OSError:
        return []
    if lines > 0:
        all_lines = all_lines[-lines:]
    return all_lines


# ── HTML ─────────────────────────────────────────────────────────────────────

def _html_page(date_str: str, lines: int, content: list[str], base_path: str) -> str:
    body = "\n".join(html.escape(line) for line in content) or "<em>(no log lines)</em>"
    body = body.replace("| ERROR    |", '| <span class="err">ERROR</span> |')
    body = body.replace("| WARNING  |", '| <span class="warn">WARNING</span> |')
    body = body.replace("| CRITICAL |", '| <span class="err">CRITICAL</span> |')

    dates = _available_dates()
    date_options = "".join(
        f'<option value="{d}"{" selected" if d == date_str else ""}>{d}</option>'
        for d in dates
    )
    line_options = "".join(
        f'<option value="{n}"{" selected" if n == lines else ""}>{n}</option>'
        for n in (50, 100, 200, 500, 1000)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TradeOS Daemon Logs — {date_str}</title>
<meta http-equiv="refresh" content="{REFRESH_SEC}">
<style>
  body {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          background: #0b0c10; color: #c5c6c7; margin: 0; padding: 1rem; }}
  .bar {{ display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.75rem;
          flex-wrap: wrap; }}
  select, input {{ background: #1f2833; color: #c5c6c7; border: 1px solid #45a29e;
                   padding: 0.35rem 0.6rem; border-radius: 4px; font-size: 0.9rem; }}
  select option {{ background: #1f2833; color: #c5c6c7; }}
  .err {{ color: #ff6b6b; font-weight: bold; }}
  .warn {{ color: #feca57; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; margin: 0;
         line-height: 1.4; font-size: 0.85rem; }}
  .muted {{ color: #6c757d; font-size: 0.85rem; }}
  a {{ color: #45a29e; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .count {{ margin-left: auto; }}
</style>
</head>
<body>
<div class="bar">
  <strong style="font-size:1.1rem;">🪵 tradeos daemon log</strong>
  <form id="f" style="display:flex;gap:0.5rem;align-items:center;margin-left:auto;">
    <label>Date:
      <select name="date" onchange="document.getElementById('f').submit()">{date_options}</select>
    </label>
    <label>Lines:
      <select name="lines" onchange="document.getElementById('f').submit()">{line_options}</select>
    </label>
    <input type="hidden" name="date" value="{html.escape(date_str)}">
    <input type="hidden" name="lines" value="{lines}">
  </form>
  <div class="count">{len(content)} line(s) showing</div>
</div>
<pre id="log">{body}</pre>
<div class="muted">
  {html.escape(str(_log_path(date_str)))} &nbsp;·&nbsp;
  auto-refresh every {REFRESH_SEC}s &nbsp;·&nbsp;
  <a href="{base_path}/health">health</a> &nbsp;·&nbsp;
  <a href="{base_path}/?date={html.escape(date_str)}&lines=2000">max lines</a>
</div>
<script>
  window.onload = () => {{
    const pre = document.getElementById('log');
    pre.scrollTop = pre.scrollHeight;
  }};
</script>
</body>
</html>"""


def _available_dates() -> list[str]:
    if not LOGS_DIR.exists():
        return []
    dates = []
    for p in LOGS_DIR.glob("tradeos_*.log"):
        name = p.name
        if name.startswith("tradeos_") and name.endswith(".log"):
            dates.append(name[len("tradeos_"):-len(".log")])
    return sorted(dates, reverse=True)


def _strip_base(path: str, base_path: str) -> str:
    if base_path and path.startswith(base_path + "/"):
        return path[len(base_path):] or "/"
    if base_path and path == base_path:
        return "/"
    return path


# ── Handler ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "TradeOSLogViewer/1.0"

    def log_message(self, fmt, *args):  # silence default stderr logging
        return

    def _check_auth(self) -> bool:
        if not _auth_ok(self.headers):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="TradeOS Logs"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"auth required\n")
            return False
        return True

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = _strip_base(self.path, BASE_PATH)
        parsed = urlparse(path)
        rpath = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if rpath == "/health":
            self._send(200, "text/plain", b"ok\n")
            return

        if rpath == "/api/logs":
            if not self._check_auth():
                return
            date_str = qs.get("date", [_today_ist()])[0]
            try:
                lines = min(int(qs.get("lines", [DEFAULT_LINES])[0]), MAX_LINES)
            except ValueError:
                lines = DEFAULT_LINES
            content = _read_tail(date_str, lines)
            payload = json.dumps({"date": date_str, "count": len(content), "lines": content})
            self._send(200, "application/json", payload.encode("utf-8"))
            return

        if rpath == "/":
            if not self._check_auth():
                return
            date_str = qs.get("date", [_today_ist()])[0]
            try:
                lines = min(int(qs.get("lines", [DEFAULT_LINES])[0]), MAX_LINES)
            except ValueError:
                lines = DEFAULT_LINES
            content = _read_tail(date_str, lines)
            html_body = _html_page(date_str, lines, content, BASE_PATH)
            self._send(200, "text/html", html_body.encode("utf-8"))
            return

        self._send(404, "text/plain", b"not found\n")


def main() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    host = "0.0.0.0"
    port = DEFAULT_PORT
    server = ThreadingHTTPServer((host, port), Handler)
    display_path = BASE_PATH or "/"
    print(f"[tradeos-log-viewer] serving on http://{host}:{port}{display_path}")
    print(f"[tradeos-log-viewer] logs dir: {LOGS_DIR}")
    if PASSWORD:
        print("[tradeos-log-viewer] basic auth enabled")
    else:
        print("[tradeos-log-viewer] WARNING: no LOG_VIEWER_PASSWORD set — open access")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
