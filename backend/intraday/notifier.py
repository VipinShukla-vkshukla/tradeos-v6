"""
One action -> Telegram + Discord + dashboard, de-duplicated.

WHY THE 30-MINUTE DIGEST WENT AWAY
----------------------------------
A fixed-interval digest gets both halves of the problem wrong at once. It is
too slow for anything urgent — a stop breached at 09:31 waits until 10:00 — and
too noisy for everything else, because it sends a message at 10:00, 10:30 and
11:00 saying the same thing has still not happened. Frequency was tuned as a
compromise between those failures instead of removing the cause.

The cause is that time is the wrong trigger. The right trigger is a CHANGE in
what you should do. HOLD -> BOOK_PARTIAL is worth interrupting you for; HOLD ->
HOLD is not worth a notification no matter how many minutes have passed.

So: evaluate continuously, notify on transition. Silence means nothing changed,
which is information rather than an absence of it.

DE-DUPLICATION IS THE WHOLE DESIGN
----------------------------------
Evaluating every 15 seconds means ~1,500 evaluations per position per session.
Without a state gate that is 1,500 messages. The gate stores the last notified
action per symbol and only sends on a genuine transition, with a re-arm timeout
so a persistent condition eventually repeats rather than being announced once
and forgotten.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg, cfg_int, get_supabase


@dataclass
class Action:
    """One thing you may need to do, in a form every channel can render."""
    symbol: str
    kind: str                       # BOOK_PARTIAL | EXIT_STOP | ENTRY | ...
    headline: str                   # one line, the message subject
    detail: str = ""                # levels, sizing, reasoning
    ltp: float | None = None
    r_multiple: float | None = None
    urgency: str = "NORMAL"         # CRITICAL | NORMAL | INFO
    meta: dict = field(default_factory=dict)

    def state_key(self) -> str:
        """What counts as 'the same alert' for de-duplication purposes."""
        return f"{self.symbol}:{self.kind}"


class Notifier:
    """
    Fan-out with a memory.

    The memory lives in this object rather than the database because it is
    per-session state: a restart SHOULD re-announce the current situation, since
    you have no way of knowing whether the earlier message was seen.
    """

    def __init__(self, sb=None):
        self.sb = sb or get_supabase()
        self._last: dict[str, tuple[str, datetime]] = {}
        self._sent_today = 0

    # ── gate ────────────────────────────────────────────────────────────────
    def _should_send(self, a: Action) -> bool:
        rearm_min = cfg_int("intraday_rearm_minutes", 45)
        key = a.state_key()
        prev = self._last.get(key)
        now = datetime.now(IST)

        if prev is None:
            return True
        prev_headline, prev_at = prev

        # Same action AND same wording -> nothing has changed. Re-arm only
        # after a timeout so a stop that has been breached for an hour says so
        # again rather than being announced once at 09:31 and never repeated.
        if prev_headline == a.headline:
            if a.urgency == "CRITICAL":
                rearm_min = min(rearm_min, cfg_int("intraday_rearm_critical_minutes", 15))
            return now - prev_at >= timedelta(minutes=rearm_min)

        # Same kind, different numbers (price moved, size changed) — worth
        # sending, but not at tick rate.
        return now - prev_at >= timedelta(minutes=cfg_int("intraday_restate_minutes", 5))

    # ── render ──────────────────────────────────────────────────────────────
    def _format(self, a: Action) -> str:
        icon = {"CRITICAL": "🚨", "NORMAL": "📌", "INFO": "ℹ️"}.get(a.urgency, "📌")
        lines = [f"{icon} <b>{a.kind.replace('_', ' ')} — {a.symbol}</b>", a.headline]
        if a.detail:
            lines.append(a.detail)
        bits = []
        if a.ltp is not None:
            bits.append(f"LTP ₹{a.ltp:,.2f}")
        if a.r_multiple is not None:
            bits.append(f"{a.r_multiple:+.2f}R")
        if bits:
            lines.append(" · ".join(bits))
        lines.append(f"<i>{datetime.now(IST):%H:%M:%S IST}</i>")
        return "\n".join(lines)

    # ── fan-out ─────────────────────────────────────────────────────────────
    def send(self, a: Action, force: bool = False) -> bool:
        if not force and not self._should_send(a):
            return False

        text = self._format(a)
        ok_any = False

        # Reuse the senders the evening digest already uses. A second
        # implementation would mean two places to fix a rate-limit bug, and
        # two different renderings of the same event.
        try:
            from alerts.send_alerts import _send_telegram, _send_discord
            channel = (cfg("alert_channel", "all") or "all").lower()
            if channel in ("telegram", "all"):
                ok_any = _send_telegram(text) or ok_any
            if channel in ("discord", "all"):
                ok_any = _send_discord(text) or ok_any
        except Exception as e:
            logger.warning(f"  notifier: chat channels failed — {e}")

        # The dashboard row is written regardless of chat success. If Telegram
        # is rate-limited the alert must still exist somewhere you can see it.
        self._write_dashboard(a)

        self._last[a.state_key()] = (a.headline, datetime.now(IST))
        self._sent_today += 1
        logger.info(f"  🔔 {a.kind} {a.symbol}: {a.headline}")
        return ok_any

    def _write_dashboard(self, a: Action) -> None:
        """
        One row per notified action, for the dashboard's live rail.

        Only NOTIFIED actions land here, not every evaluation — see the module
        docstring on storage in db/migrations/011. Writing each evaluation would
        be ~1,500 rows per position per day for no added information.
        """
        try:
            self.sb.table("intraday_alerts").insert({
                "ts":         datetime.now(IST).isoformat(),
                "symbol":     a.symbol,
                "kind":       a.kind,
                "urgency":    a.urgency,
                "headline":   a.headline,
                "detail":     a.detail or None,
                "ltp":        a.ltp,
                "r_multiple": a.r_multiple,
                "meta":       json.dumps(a.meta) if a.meta else None,
            }).execute()
        except Exception as e:
            logger.debug(f"  notifier: dashboard write failed — {e}")

    def heartbeat(self, summary: str) -> None:
        """
        Proof of life, at a human cadence.

        Without it, silence is ambiguous: a daemon doing its job correctly and a
        daemon that died at 09:20 look identical from the outside. This is the
        ONLY time-based message in the system, and it exists to make the
        event-driven silence trustworthy.
        """
        logger.info(f"  ♥ {summary}")
        try:
            self.sb.table("intraday_heartbeat").upsert({
                "id": 1,
                "ts": datetime.now(IST).isoformat(),
                "summary": summary,
                "alerts_sent": self._sent_today,
            }).execute()
        except Exception as e:
            logger.debug(f"  heartbeat write failed — {e}")
