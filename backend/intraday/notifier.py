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
import os
import re
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
        self._warned_fallback = False

    # ── gate ────────────────────────────────────────────────────────────────
    @staticmethod
    def _material(headline: str) -> str:
        """
        The headline with tick noise removed, for comparison only.

        Headlines embed the live price, so "book 7/15 @ 195.27 (+12.26%)" and
        "@ 195.15 (+12.19%)" are different strings describing the SAME decision.
        Comparing raw text therefore routed every repeat down the "something
        changed" path and re-sent it every 5 minutes: one unresolved partial
        book produced 17 alerts in a session, and the swing entry watch another
        28. 45 of 48 alerts in a day were the same three situations restated.

        That is not a cosmetic problem. The reflex you build for the frequent
        message is the reflex you apply to the rare one, so a channel that cries
        every five minutes is a channel where a real stop breach goes unread.

        Rounding every number to the nearest integer collapses tick drift while
        keeping genuine changes visible: 7-of-15 becoming 12-of-15 still differs,
        and a rupee of real movement still differs. Only the display text is
        left alone — you still see the exact price.
        """
        return re.sub(r"\d+\.\d+",
                      lambda m: str(round(float(m.group()))), headline)

    def _should_send(self, a: Action) -> bool:
        rearm_min = cfg_int("intraday_rearm_minutes", 45)
        key = a.state_key()
        prev = self._last.get(key)
        now = datetime.now(IST)

        if prev is None:
            return True
        prev_headline, prev_at = prev

        # Same action AND same substance -> nothing has changed. Re-arm only
        # after a timeout so a stop that has been breached for an hour says so
        # again rather than being announced once at 09:31 and never repeated.
        if self._material(prev_headline) == self._material(a.headline):
            if a.urgency == "CRITICAL":
                rearm_min = min(rearm_min, cfg_int("intraday_rearm_critical_minutes", 15))
            return now - prev_at >= timedelta(minutes=rearm_min)

        # Same kind, genuinely different numbers (size changed, level crossed) —
        # worth sending, but not at tick rate.
        return now - prev_at >= timedelta(minutes=cfg_int("intraday_restate_minutes", 15))

    # ── render ──────────────────────────────────────────────────────────────
    def _format(self, a: Action) -> str:
        icon = {"CRITICAL": "🚨", "NORMAL": "📌", "INFO": "ℹ️"}.get(a.urgency, "📌")
        # Prefix every intraday message. Channel separation is the primary
        # mechanism, but a label survives a misrouted webhook — and knowing
        # which system is talking is the whole point of the split.
        lines = [f"{icon} <b>[INTRADAY] {a.kind.replace('_', ' ')} — {a.symbol}</b>",
                 a.headline]
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
        ok_any = self._deliver(text)

        # The dashboard row is written regardless of chat success. If Telegram
        # is rate-limited the alert must still exist somewhere you can see it.
        self._write_dashboard(a)

        self._last[a.state_key()] = (a.headline, datetime.now(IST))
        self._sent_today += 1
        logger.info(f"  🔔 {a.kind} {a.symbol}: {a.headline}")
        return ok_any

    # ── delivery, on the INTRADAY channels ──────────────────────────────────
    def _deliver(self, text: str) -> bool:
        """
        Send to the intraday bot and webhook, NOT the swing ones.

        The two streams demand different things of you. A swing digest is
        "read this tonight and decide tomorrow's entries"; an intraday alert is
        "a stop is being approached right now". Delivering both to one chat
        means the only way to mute the noisy stream is to mute the urgent one,
        and the reflex you build for the frequent message is the reflex you
        apply to the rare one.

        Falls back to the swing channel when no intraday credentials are set,
        so nothing is silently lost — but says so once, because a fallback that
        looks like success is how you end up believing the split is working.
        """
        import requests
        ok = False

        # One resolver, one documented order: environment, then system_config.
        # Previously each caller invented its own lookup, which is how the swing
        # Discord webhook ended up living only in system_config while the swing
        # Telegram token lived only in .env — both working, neither documented.
        from credentials_resolver import resolve
        token = resolve("TELEGRAM_INTRADAY_BOT_TOKEN")
        chat  = resolve("TELEGRAM_INTRADAY_CHAT_ID")
        hook  = resolve("DISCORD_INTRADAY_WEBHOOK_URL")

        if token and chat:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}, timeout=10)
                ok = (r.status_code == 200) or ok
                if r.status_code != 200:
                    logger.warning(f"  intraday telegram {r.status_code}: {r.text[:140]}")
            except Exception as e:
                logger.warning(f"  intraday telegram failed: {e}")

        if hook:
            try:
                md = (text.replace("<b>", "**").replace("</b>", "**")
                          .replace("<i>", "_").replace("</i>", "_"))
                r = requests.post(hook, json={"content": md[:1900]}, timeout=10)
                ok = (r.status_code in (200, 204)) or ok
            except Exception as e:
                logger.warning(f"  intraday discord failed: {e}")

        if not (token and chat) and not hook:
            if not self._warned_fallback:
                logger.warning(
                    "  No intraday chat credentials configured — falling back to the "
                    "SWING channel, so intraday and swing alerts are arriving in the "
                    "same place. Set TELEGRAM_INTRADAY_BOT_TOKEN + "
                    "TELEGRAM_INTRADAY_CHAT_ID and/or DISCORD_INTRADAY_WEBHOOK_URL."
                )
                self._warned_fallback = True
            try:
                from alerts.send_alerts import _send_telegram, _send_discord
                channel = (cfg("alert_channel", "all") or "all").lower()
                if channel in ("telegram", "all"):
                    ok = _send_telegram(text) or ok
                if channel in ("discord", "all"):
                    ok = _send_discord(text) or ok
            except Exception as e:
                logger.warning(f"  notifier: fallback channels failed — {e}")
        return ok

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
