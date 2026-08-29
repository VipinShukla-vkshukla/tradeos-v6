"""
TradeOS v7 — Send Alerts v6
============================
Pipeline position: final step — after step 19 (ai_decision_engine).

WHAT CHANGED FROM v5 → v6:

PATCH 16 — Multi-Channel Alert System  (system_config table driven)
  Extends the Telegram sender into a full channel router.
  Channel selection is read at runtime from the system_config Supabase table
  so you can switch channels without touching code or restarting the process.

  Channels available:
    telegram          → original behaviour (re-activates if ban lifts)
    discord           → recommended replacement; Markdown-formatted via webhook
    email             → unlimited length; HTML + plain-text via SMTP
    discord+email     → Discord primary + email simultaneous copy
    all               → all three channels at once

  system_config keys consumed (SQL seed in PATCH 16 comment block below):
    alert_channel          default: telegram
    discord_webhook_url    Discord webhook URL
    email_smtp_host        default: smtp.gmail.com
    email_smtp_port        default: 587
    email_from             sender address
    email_to               recipient(s), comma-separated
    email_password         SMTP app password (Gmail: use App Password, NOT login)
    email_subject_prefix   default: TradeOS

  Internal senders added — no changes to any build_*() or formatter:
    _send_telegram()      → original implementation, preserved unchanged
    _send_discord()       → converts Telegram HTML → Discord Markdown then sends
    _send_email()         → wraps content in dark <pre> block; HTML + plain parts
    _html_to_discord_md() → <b>→**bold**  <i>→*italic*  <code>→`code`
    _html_to_plain()      → strips all HTML tags for email plain-text fallback

PATCH 17 — load_channel_config(sb)
  New function called in main() after get_supabase().
  Reads all rows from system_config and populates _CHANNEL_CFG dict.
  _ch(key, default) helper reads _CHANNEL_CFG → cfg() → default in order,
  so env/config.py values remain as fallback if a key isn't in the table.

PATCH 18 — main(): channel-aware startup + subject routing
  load_channel_config(sb) called before load_data().
  Active channel logged at startup alongside step-19 / readiness status.
  Mode-specific subject_suffix passed to send_message() (Evening / Morning…).
  Backward-compat: 'telegram_alerts_enabled' key still gates the whole system.

PATCH 19 — AI-EXTENDED chase-zone rendering (2026-07)
  ai_decision_engine.py (step 19) now writes ai_max_chase_pct /
  ai_chase_rationale / ai_zone_high_extended to signal_output_daily, and
  entry_readiness.py now uses them to add a new zone_status tier —
  "🟡 AI-EXTENDED" — for live prices above compute_msl's mechanical zone
  but within the AI's approved chase ceiling. entry_readiness.py also now
  returns a second, chase-anchored GTT set (gtt_entry_chase/sl_chase/
  t1_chase/rr_ratio_chase) alongside the original zone_low-anchored one.
  No new query needed here — msl_map already gets these via the existing
  signal_output_daily .select("*") in load_data(). This patch only updates
  the two places that render TIER_1 candidate dicts:
    build_afternoon(): any_actionable now also true for AI-EXTENDED (was
      only "IN ZONE" — a TIER_1 pick the AI approved chasing on was being
      reported as "no picks in zone" like the mechanical-only setup this
      is fixing); chase-alt GTT line added alongside the original.
    build_morning(): same chase-alt GTT line added.
  build_evening() untouched — it runs with use_live=False (no live price
  at 6 PM pipeline-run time), so zone_status is always empty there and
  this tier structurally cannot fire.

UNCHANGED from v5:
  PATCH 1–15 — all behaviours identical.
  All build_*() functions and formatters — zero formatting change.
  send_signal_with_keyboard() — Telegram-specific; unchanged.
  load_data() — unchanged.

WHAT CHANGED FROM v4 → v5 (kept for reference):

  PATCH 1  — entry_readiness optional import + _READINESS_AVAILABLE flag
  PATCH 2  — STOP_BUFFER from stop_buffer_pct config key (default 0.03)
  PATCH 3  — build_position_block: 85-char compressed rationale (evening)
  PATCH 4  — build_evening: sb=None signature for readiness enrichment
  PATCH 5  — build_evening: echo restored at 300 chars with 📖 label
  PATCH 6  — build_evening: MACRO + FII FLOW section before Market Intel
  PATCH 7  — build_evening: single guidance line with priority cascade
  PATCH 8  — build_evening: ACT NOW bucket compressed (3 lines) + readiness score
  PATCH 9  — build_evening: WATCH bucket compressed (2 lines)
  PATCH 10 — build_evening: TOMORROW'S GTT ORDERS section
  PATCH 11 — build_morning: sb=None signature
  PATCH 12 — build_morning: single guidance line
  PATCH 13 — build_morning: ACT NOW bucket live zone status + readiness
  PATCH 14 — build_morning: WATCH bucket compact 2-line
  PATCH 15 — main(): pass sb to both builders

DATA SOURCES (all bound to confirmed schema):
  ai_context         → __FINAL_PICKS__ (conviction_reason + strategy_validation)
                        __MARKET_INTEL__ (conviction_reason + ai_note)
  signal_log         → all signals for signal_date
  open_positions     → the risk model: planned_stop, active_sl, planned_target,
                        r_multiple_current, MFE/MAE, breakeven_moved,
                        trail_activated, partial_booked_qty, attribution
                        (NOT target_1/2/3, locked_profit, event_risk,
                         upcoming_news, lifecycle or trailing_sl_pct — none of
                         those has a writer; see tools/audit_alert_reads.py)
  master_shortlist   → entry zones, expected_r, dist_entry_pct
  market_regime      → regime, nifty, vix, breadth, A/D, PCR
  fii_dii_flow       → fii_net, fii_net_5d, fii_net_20d, fii_flag
  global_cues        → gift_nifty, gap_signal, global indices, crude, INR
  nifty_upcoming_events → symbol, purpose, details, event_date, days_to_event
  lessons            → 7-day AI + rule counts
  data_anomalies     → ERROR-level alerts
  system_config      → alert_channel + channel credentials (v6 NEW)

MESSAGE STRUCTURE:
  EVENING:
    Header: regime + FII + macro snapshot (build_regime_header)
    Section 0: MACRO + FII FLOW (FII sector flow + regulatory alerts)
    Section 1: Market Intelligence (summary + echo)
    Section 2: Portfolio Health Snapshot
    Section 3: Open Positions (full lifecycle)
    Section 4: EXIT signals
    Section 5: ACT NOW — ENTER_NOW/ENTER_ON_DIP (3-line compact + readiness)
    Section 6: WATCH FOR TRIGGER — WAIT_FOR_TRIGGER (2-line compact)
    Section 7: MONITOR — SKIP
    Section 8: Near Miss
    Section 9: Sector Warnings + Correlation Groups
    Section 10: Upcoming Events
    Footer: GTT Orders + lesson count

  MORNING:
    Header: regime + Gift Nifty gap
    Gap Risk Alert: SL breach risk positions
    Position Pulse: brief position status
    EXIT Today
    ACT NOW Watchlist (live zone status + entry levels)
    WATCH Intraday Triggers (2-line compact)
    Today's Events + Next 3 days events

  AFTERNOON:
    Header: date + time
    ACT NOW zone status only (live yfinance prices)

  COMPACT:
    One-screen mobile summary
"""

import sys
import json
import re
import time
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import html

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import (
    get_supabase, today_ist, is_kill_switch_active,
    cfg, cfg_bool, DRY_RUN,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, fetch_all,
)

# ── PATCH 1: entry_readiness import ───────────────────────────────────────
try:
    from analysis.entry_readiness import compute_entry_readiness
    _READINESS_AVAILABLE = True
except ImportError:
    _READINESS_AVAILABLE = False

MESSAGE_STYLE = cfg("telegram_message_style", "structured")

# ── PATCH 2: STOP_BUFFER constant ─────────────────────────────────────────
try:
    STOP_BUFFER: float = float(cfg("stop_buffer_pct", "0.03"))
except Exception:
    STOP_BUFFER: float = 0.03

# All signal types that represent actionable entry candidates
ENTRY_TYPES = {
    "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
    "REENTRY_SETUP", "STAGED_ENTRY", "MARKET_TOP_PICK",
    "MOMENTUM_CONTINUATION",
}

def _num(v):
    """Float or None. Never coerces a missing value to 0 — a stop of 0.00 in an
    alert reads as 'no risk' rather than 'unknown', which is the more dangerous
    of the two misreadings."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def decision_line(row: dict, live_price: float | None,
                  open_pos: list | None = None, regime: str = "NEUTRAL") -> str:
    """
    One line telling the reader what to DO at the current price.

    The digest previously rendered score, conviction and an entry zone and left
    the decision entirely to the reader — every alert looked identical whether
    the setup was live or already dead. This evaluates the persisted plan
    against the price and states the action, the size, and the reason.
    """
    try:
        from analysis.trade_decision import decide
        from config import capital_for
        d = decide(row, live_price, total_capital=capital_for("SWING"),
                   open_positions=open_pos or [], regime=regime,
                   max_chase_pct=row.get("ai_max_chase_pct"))
        icon = {"BUY_NOW": "🟢", "CHASE_LIMIT": "🟡", "WAIT": "⏳",
                "SKIP": "🔴", "NO_DATA": "⚪"}.get(d.action, "•")
        out = f"  {icon} <b>{d.action.replace('_', ' ')}</b> — {d.reason}"
        # Size only on actions you would actually execute. Printing "18 sh ≈
        # ₹25,717" under a SKIP invites the reader to take the trade the line
        # above just told them not to.
        if d.qty and d.action in ("BUY_NOW", "CHASE_LIMIT"):
            out += f"\n     <code>{d.qty} sh ≈ ₹{d.invested:,.0f} · risk ₹{d.risk_amount:,.0f}</code>"
        if d.stale_price:
            out += "\n     <i>(evaluated at last close — no live feed)</i>"
        return out
    except Exception as e:
        logger.debug(f"decision_line failed for {row.get('symbol')}: {e}")
        return ""


def esc(text: str) -> str:
    """
    Escape HTML special characters in AI-generated free text.
    Prevents Telegram from misinterpreting < > & as HTML tags.
    Only apply to dynamic content — NOT to your own <b> <i> tags.
    """
    if not text:
        return ""
    return html.escape(str(text), quote=False)


# ════════════════════════════════════════════════════════════════════════════
# PATCH 16 — MULTI-CHANNEL ALERT SYSTEM
# ════════════════════════════════════════════════════════════════════════════
#
# Seed your system_config table with these rows to activate a channel.
# Only the rows you add take effect; missing keys fall back to cfg() / env.
#
#   INSERT INTO public.system_config (key, value, description) VALUES
#     ('alert_channel',
#       'discord',
#       'Active channel(s): telegram | discord | email | discord+email | all'),
#
#     ('discord_webhook_url',
#       'https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN',
#       'Server → any channel → Settings → Integrations → Webhooks → New'),
#
#     ('email_smtp_host',
#       'smtp.gmail.com',
#       'SMTP server: smtp.gmail.com | smtp.office365.com | smtp.zoho.in'),
#
#     ('email_smtp_port',
#       '587',
#       'SMTP port: 587 (STARTTLS, recommended) | 465 (SSL)'),
#
#     ('email_from',
#       'you@gmail.com',
#       'Sender email address'),
#
#     ('email_to',
#       'you@gmail.com',
#       'Recipient(s) — comma-separate for multiple: a@x.com,b@x.com'),
#
#     ('email_password',
#       '',
#       'Gmail: Account → Security → 2-Step → App Passwords (16-char code)'),
#
#     ('email_subject_prefix',
#       'TradeOS',
#       'Email subject prefix: "TradeOS Evening" / "TradeOS Morning" etc.')
#   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
#                                    updated_at = now();
#
# CHANNEL COMPARISON:
#   telegram      → Original. 4096 chars/msg. Telegram HTML. Reactivates if
#                   ban lifts — just set alert_channel = 'telegram'.
#   discord       → Recommended replacement. Free. Great mobile app.
#                   2000 chars/msg (split handled). No bot needed — webhook only.
#                   HTML auto-converted to Discord **Markdown**.
#   email         → Universal fallback. Unlimited length. Full HTML renders
#                   in Gmail/Outlook. Works even if all chat apps are blocked.
#   discord+email → Discord primary with email as simultaneous archive copy.
#   all           → All three. Useful during channel migration.
#
# ════════════════════════════════════════════════════════════════════════════

# Runtime channel config — populated once per run in main() by load_channel_config().
# Starts empty; _ch() falls back to cfg() if a key is absent (backward-compat).
_CHANNEL_CFG: dict = {}


def _ch(key: str, default: str = "") -> str:
    """
    Channel config accessor — 3-tier priority:
      1. system_config table  (_CHANNEL_CFG populated in main())
      2. cfg() / env / config.py
      3. hard-coded default

    Use this for every channel credential so system_config overrides env
    without requiring a process restart.
    """
    return _CHANNEL_CFG.get(key) or cfg(key, default) or default


# ── PATCH 17: load_channel_config ─────────────────────────────────────────

def load_channel_config(sb) -> dict:
    """
    Read all rows from system_config and return {key: value}.
    Called once in main() after get_supabase(); result stored in _CHANNEL_CFG.

    Keys not present in the table are silently absent — _ch() falls back
    to cfg() for those.  Supabase errors degrade gracefully: the module
    continues with cfg() / env defaults so alerts are never silently dropped.
    """
    try:
        rows = (
            sb.table("system_config")
              .select("key,value")
              .execute().data or []
        )
        config = {
            r["key"]: r["value"]
            for r in rows
            if r.get("key") and r.get("value") is not None
        }
        channel = config.get("alert_channel", "(not set — will use cfg() default)")
        logger.info(
            f"system_config loaded: {len(config)} keys  "
            f"| alert_channel → {channel}"
        )
        return config
    except Exception as e:
        logger.warning(f"system_config load failed — using cfg() fallbacks: {e}")
        return {}


# ── Channel message limits ─────────────────────────────────────────────────

_TELEGRAM_LIMIT = 4096    # Telegram hard per-message limit
_DISCORD_LIMIT  = 1900    # Discord 2000-char limit with 100-char safety margin


def _split_html_safe(text: str, limit: int = _TELEGRAM_LIMIT) -> list[str]:
    """
    Split a message at newline boundaries so no content is sheared mid-line.
    Accepts a per-channel limit: 4096 for Telegram, 1900 for Discord.
    Hard-splits lines that exceed the limit in isolation (pathological content).
    """
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


# ── Format converters ──────────────────────────────────────────────────────

def _html_to_discord_md(text: str) -> str:
    """
    Convert Telegram HTML markup → Discord Markdown in-place.

      Telegram   →  Discord
      <b>…</b>  →  **…**
      <i>…</i>  →  *…*
      <code>…</code> → `…`

    Section dividers (═══), emoji, and ₹ symbols render fine in Discord
    without conversion.  Residual/unmatched tags are stripped so no raw HTML
    leaks into Discord messages.
    """
    text = re.sub(r"<b>(.*?)</b>",       r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>",       r"*\1*",   text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`",   text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _html_to_plain(text: str) -> str:
    """Strip all HTML tags — plain-text fallback body for email MIME part."""
    return re.sub(r"<[^>]+>", "", text)


# ── Channel senders ────────────────────────────────────────────────────────

def _send_telegram(text: str) -> bool:
    """
    Telegram sender — original v5 implementation, completely unchanged.
    Activates when alert_channel is 'telegram' or 'all'.
    Preserved here so it reactivates automatically if/when the ban lifts:
    just update system_config.alert_channel to 'telegram' and it's live.
    """
    import requests
    token   = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not set")
        return False

    parts = _split_html_safe(text, limit=_TELEGRAM_LIMIT)
    if len(parts) > 1:
        return all(_send_telegram(p) for p in parts)

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 30))
                logger.warning(f"Telegram rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            logger.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            wait = [5, 15, 30][attempt]
            logger.warning(f"Telegram attempt {attempt+1} failed: {e} — retry in {wait}s")
            time.sleep(wait)
    return False


def _send_discord(text: str) -> bool:
    """
    Discord webhook sender.
    Converts Telegram HTML → Discord Markdown, then splits into ≤1900-char
    chunks at newline boundaries.  A 0.5s gap between chunks respects Discord's
    30-requests/min webhook rate limit comfortably.
    """
    import requests
    webhook = _ch("discord_webhook_url")
    if not webhook:
        logger.warning(
            "Discord webhook URL not configured — "
            "add discord_webhook_url to system_config table"
        )
        return False

    md_text = _html_to_discord_md(text)
    parts   = _split_html_safe(md_text, limit=_DISCORD_LIMIT)

    for i, part in enumerate(parts):
        payload = {"content": part}
        for attempt in range(3):
            try:
                resp = requests.post(webhook, json=payload, timeout=10)
                if resp.status_code in (200, 204):
                    break
                if resp.status_code == 429:
                    retry_after = resp.json().get("retry_after", 5)
                    logger.warning(f"Discord rate limit — waiting {retry_after}s")
                    time.sleep(float(retry_after))
                    continue
                logger.error(f"Discord error {resp.status_code}: {resp.text[:200]}")
                return False
            except Exception as e:
                wait = [3, 10, 20][attempt]
                logger.warning(f"Discord attempt {attempt+1} failed: {e} — retry in {wait}s")
                time.sleep(wait)
        else:
            logger.error(f"Discord: all retries exhausted for part {i+1}/{len(parts)}")
            return False

        if i < len(parts) - 1:
            time.sleep(0.5)   # polite inter-chunk gap

    return True


def _send_email(text: str, subject_suffix: str = "Alert") -> bool:
    """
    SMTP email sender.  Delivers two MIME parts per message:
      · text/html  — your existing Telegram HTML renders in Gmail / Outlook.
                     Wrapped in a dark <pre> block to preserve line layout.
      · text/plain — all tags stripped; fallback for plain-text mail clients.

    Provider quick-reference:
      Gmail        → smtp.gmail.com:587    (App Password required)
      Outlook/O365 → smtp.office365.com:587
      Zoho Mail    → smtp.zoho.in:587      (free plan supported)
      Yahoo        → smtp.mail.yahoo.com:587
    """
    smtp_host = _ch("email_smtp_host", "smtp.gmail.com")
    smtp_port = int(_ch("email_smtp_port", "587") or 587)
    from_addr = _ch("email_from")
    to_raw    = _ch("email_to")
    password  = _ch("email_password")
    prefix    = _ch("email_subject_prefix", "TradeOS")
    to_addrs  = [a.strip() for a in to_raw.split(",") if a.strip()]

    if not from_addr or not to_addrs or not password:
        logger.warning(
            "Email credentials incomplete — "
            "set email_from / email_to / email_password in system_config"
        )
        return False

    subject = f"{prefix} {subject_suffix}"
    plain   = _html_to_plain(text)

    # Dark background + monospaced pre-wrap preserves the ═══ section dividers,
    # emoji columns, and ₹ price alignment that the build_*() functions produce.
    html_body = (
        "<!DOCTYPE html>"
        '<html><body style="background:#111;color:#eee;'
        'font-family:\'Courier New\',monospace;padding:20px;">'
        '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.6;'
        'margin:0;">'
        f"{text}"
        "</pre></body></html>"
    )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(plain,     "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    for attempt in range(3):
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())
            logger.info(f"Email sent → {to_addrs}")
            return True
        except Exception as e:
            wait = [5, 15, 30][attempt]
            logger.warning(f"Email attempt {attempt+1} failed: {e} — retry in {wait}s")
            time.sleep(wait)

    logger.error("Email: all retries exhausted")
    return False


def send_message(text: str, subject_suffix: str = "Alert") -> bool:
    """
    Channel router — the only function called by the rest of send_alerts.py.

    Reads alert_channel from system_config (via _ch()) at call time, so
    changes to the table take effect on the next cron run with no code touch.

    Valid alert_channel values:
        telegram          → Telegram only  (original behaviour)
        discord           → Discord only   (recommended; use during ban)
        email             → Email only     (universal fallback)
        discord+email     → Discord + email simultaneously
        all               → All three channels

    Returns True if at least one channel delivered successfully.
    DRY_RUN mode logs a 300-char preview instead of sending to any channel.
    """
    channel = _ch("alert_channel", "telegram")

    if DRY_RUN:
        preview = text[:300].replace("\n", " ")
        logger.info(f"[DRY_RUN] send_message ({channel}): {preview}…")
        return True

    results: dict[str, bool] = {}

    if channel in ("telegram", "all"):
        results["telegram"] = _send_telegram(text)

    if channel in ("discord", "discord+email", "all"):
        results["discord"] = _send_discord(text)

    if channel in ("email", "discord+email", "all"):
        results["email"] = _send_email(text, subject_suffix=subject_suffix)

    if not results:
        logger.error(
            f"send_message: unknown alert_channel '{channel}' in system_config. "
            "Valid values: telegram | discord | email | discord+email | all"
        )
        return False

    failures = [ch for ch, ok in results.items() if not ok]
    if failures:
        logger.warning(f"Alert delivery failed on: {failures}")

    return any(results.values())

# ══ end PATCH 16 / 17 ════════════════════════════════════════════════════════


# ── Formatters ────────────────────────────────────────────────────────────

def entry_action_icon(action: str) -> str:
    """AI conviction/tier was removed system-wide 29-Aug-2026 — measured
    inversely predictive (see ai/ai_decision_engine.py's module docstring).
    This icon now colors the AI's actual surviving output, the recommended
    entry action, rather than a self-rated confidence in it. Named apart
    from action_icon() below, which colors POSITION actions (TRAIL_SL/
    EXIT/HOLD/...) — same word, different vocabulary, same TERMINOLOGY.md
    collision class this project has been burned by before."""
    return {"ENTER_NOW": "🟢", "ENTER_ON_DIP": "🟡",
            "WAIT_FOR_TRIGGER": "🔵", "SKIP": "🔴"}.get((action or "").upper(), "⚪")

def regime_icon(r: str) -> str:
    """
    `r` is always `regime.get("regime")` / `regime.get("predicted_regime")` —
    the swing regime engine's own five states (TRENDING, RISK ON, NEUTRAL,
    RECOVERING, RISK OFF; `compute_regime.py`'s `apply_hysteresis`), never the
    intraday market context's states. The two vocabularies read as similar and
    are not the same one — see allocation/hurdle.py::regime_bucket for what
    happened when a different function conflated them.

    RECOVERING had no entry and fell through to ❓ on every single session in
    that state, even though it is one of only five values this can ever be.
    CAUTION and BULLISH are removed: CAUTION is never a swing regime value —
    it belongs to intraday/market_context.py — and BULLISH is a legacy label
    from before the hysteresis engine existed
    (ai/providers/ml_regime_classifier.py's own legacy_map normalises it away).
    Neither can arrive here from `regime.get("regime")`, so keeping them was
    decoration that implied a coverage this function did not have.
    """
    return {
        "TRENDING": "🚀", "RISK ON": "🚀", "NEUTRAL": "⚖️",
        "RECOVERING": "🌤️", "RISK OFF": "🔴",
    }.get((r or "").upper(), "❓")

def fii_icon(f: str) -> str:
    return {"BUYING": "🟢", "SELLING": "🔴", "NEUTRAL": "⚪"}.get((f or "").upper(), "⚪")

def action_icon(a: str) -> str:
    return {
        "TRAIL_SL": "📈", "PARTIAL_BOOK": "💰", "EXIT": "🔴",
        "HOLD": "🟡", "ADD": "🟢", "REDUCE": "🔽",
        "WATCH_CLOSELY": "👁", "BOOK_PARTIAL": "💰",
    }.get((a or "").upper().replace(" ", "_"), "⚪")

def fmt_price(v) -> str:
    if v is None: return "—"
    try:    return f"₹{float(v):,.0f}"
    except: return str(v)

def fmt_pct(v) -> str:
    if v is None: return "—"
    try:    return f"{float(v):+.1f}%"
    except: return str(v)

def fmt_chg(v, d: int = 2) -> str:
    if v is None: return ""
    try:    return f"{float(v):+.{d}f}%"
    except: return str(v)

def fmt_inr(v) -> str:
    """Compact Indian-format currency: Cr / L / ₹."""
    if v is None: return "—"
    try:
        val = float(v)
        if abs(val) >= 1e7:   return f"₹{val/1e7:.1f}Cr"
        elif abs(val) >= 1e5: return f"₹{val/1e5:.1f}L"
        else:                 return f"₹{val:,.0f}"
    except: return str(v)

def days_held(entry_date_str) -> str:
    if not entry_date_str: return ""
    try:
        from datetime import date
        d = (today_ist() - date.fromisoformat(str(entry_date_str)[:10])).days
        return f"{d}d"
    except: return ""

def sl_proximity_pct(cp, sl) -> "float | None":
    try:
        cp, sl = float(cp), float(sl)
        if sl <= 0 or cp <= 0: return None
        return (cp - sl) / cp * 100
    except: return None

def fii_footer_line(fii: dict) -> str:
    if not fii: return ""
    flag    = str(fii.get("fii_flag") or "").upper()
    net     = fii.get("fii_net")
    net_5d  = fii.get("fii_net_5d")
    net_20d = fii.get("fii_net_20d")
    ico     = fii_icon(flag)
    parts   = [f"{ico} FII: <b>{flag}</b>"]
    if net      is not None: parts.append(f"Today ₹{float(net):.0f}Cr")
    if net_5d   is not None: parts.append(f"5d ₹{float(net_5d):.0f}Cr")
    if net_20d  is not None: parts.append(f"20d ₹{float(net_20d):.0f}Cr")
    return "  ".join(parts)


# ── Position-specific helpers ──────────────────────────────────────────────

def target_progress(cp, t1, t2, t3) -> str:
    """
    Show T1/T2/T3 with hit status.
    ✅ = CMP has reached or passed target.
    (+X.Y%) = distance remaining to target.
    """
    try:    cp = float(cp or 0)
    except: return ""
    if cp <= 0: return ""
    parts = []
    for label, t in [("T1", t1), ("T2", t2), ("T3", t3)]:
        if t is None: continue
        try:    t = float(t)
        except: continue
        if t <= 0: continue
        pct = (t - cp) / cp * 100
        if pct <= 0:
            parts.append(f"{label}:{fmt_price(t)}✅")
        else:
            parts.append(f"{label}:{fmt_price(t)}({pct:+.1f}%)")
    return "  ".join(parts)

def partial_booking_summary(partial_bookings, orig_qty, curr_qty) -> str:
    """
    Summarise partial exit from:
      - original_qty vs current_qty difference (confirms partial happened)
      - partial_bookings JSONB array for price/qty/pnl detail
    """
    orig = int(orig_qty or 0)
    curr = int(curr_qty or 0)
    qty_line = ""
    if orig > 0 and curr < orig:
        pct_out  = (orig - curr) / orig * 100
        qty_line = f"{pct_out:.0f}% booked ({orig - curr}/{orig} sh)"

    if not partial_bookings:
        return qty_line

    try:
        bookings = (partial_bookings
                    if isinstance(partial_bookings, list)
                    else json.loads(partial_bookings))
        if not isinstance(bookings, list) or not bookings:
            return qty_line
        entries = []
        for b in bookings[-2:]:
            if not isinstance(b, dict): continue
            price = b.get("price") or b.get("exit_price")
            pnl   = b.get("pnl_pct") or b.get("pnl")
            if price:
                s = f"@{fmt_price(price)}"
                if pnl: s += f"={fmt_pct(pnl)}"
                entries.append(s)
        detail = " ".join(entries)
        return f"{qty_line} {detail}".strip() if detail else qty_line
    except Exception:
        return qty_line


# ── Data loader ────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """
    v6: Primary source is signal_output_daily (step 20.5 immutable snapshot).
    Replaces the former 8-table join with a single query + 6 small utility reads.

    Tables still read live (cannot be snapshotted per-symbol):
      - ai_context.__FINAL_PICKS__  : portfolio sizing, bulk guidance, warnings
      - ai_context.__MARKET_INTEL__ : market synthesis summary
      - open_positions              : live portfolio state
      - global_cues                 : live pre-market prices
      - nifty_upcoming_events       : event calendar
      - lessons / data_anomalies    : utility counts

    Falls back to _load_data_legacy() if signal_output_daily has no rows yet.
    """
    # ── Trading date resolution ──────────────────────────────────────────────
    try:
        holidays = {
            r["date"][:10]
            for r in sb.table("nse_holidays").select("date").execute().data
        }
    except Exception:
        holidays = set()

    signal_date = today
    d = today_ist()
    for _ in range(10):
        if d.weekday() < 5 and str(d) not in holidays:
            try:
                probe = (
                    sb.table("signal_output_daily")
                      .select("date")
                      .eq("date", str(d))
                      .limit(1)
                      .execute().data
                )
                if probe:
                    signal_date = str(d)
                    if str(d) != today:
                        logger.info(
                            f"No signal_output_daily for {today} — using {d}"
                        )
                    break
            except Exception:
                signal_date = str(d)
                break
        d -= timedelta(days=1)

    # ── Primary: signal_output_daily ─────────────────────────────────────────
    sod_rows: list[dict] = []
    try:
        sod_rows = (
            sb.table("signal_output_daily")
              .select("*")
              .eq("date", signal_date)
              .execute().data
        ) or []
    except Exception as e:
        logger.warning(f"signal_output_daily read failed: {e}")

    if not sod_rows:
        logger.warning(
            "signal_output_daily empty for %s — no data to send alerts", today
        )
        return {
            "signal_date": today, "display_date": today,
            "final_picks": None, "market_intel": {}, "signals": [],
            "open_pos": [], "entry_thesis_map": {}, "regime": {},
            "fii": {}, "cues": {}, "msl_map": {}, "upcoming_events": [],
            "lessons_ai": 0, "lessons_rb": 0, "anomalies": [],
            "portfolio_summary": {},
        }

    sod_map: dict[str, dict] = {r["symbol"]: r for r in sod_rows}

    # signals list: sod_rows already has every field send_alerts reads from signal_log
    # (symbol, sector, signal_type, signal_subtype, strategy, score, score_adjusted,
    #  filter_reason, ai_conviction, ai_conviction_reason, ai_note, ai_suggested_action,
    #  holding_score, momentum_state, lifecycle, fii_flag, sector_rank_at_entry,
    #  days_to_trigger_est, near_miss_data, eap_action, vol_ratio, delivery_pct)
    signals = sod_rows

    # msl_map: built from sod_rows — contains entry_zone_low/high, current_price,
    # lifecycle, dist_entry_pct, expected_r, validity_score
    msl_map: dict[str, dict] = sod_map

    # regime: market-wide context — extract once from any row that has it
    regime: dict = {}
    for r in sod_rows:
        if r.get("regime"):
            regime = {
                "regime":                r.get("regime"),
                "predicted_regime":      r.get("predicted_regime"),
                "regime_confidence":     r.get("regime_confidence"),
                "nifty_price":           r.get("nifty_price"),
                "india_vix":             r.get("india_vix"),
                "avg_sector_breadth":    r.get("avg_sector_breadth"),
                "nifty_1d_chg_pct":      r.get("nifty_1d_chg_pct"),
                "above_200dma_pct":      r.get("above_200dma_pct"),
                "advance_decline_ratio": r.get("advance_decline_ratio"),
                "nifty_pcr":             r.get("nifty_pcr"),
            }
            break

    # fii: extract once from any row that has it
    fii: dict = {}
    for r in sod_rows:
        if r.get("fii_flag") or r.get("fii_net_20d") is not None:
            fii = {
                "fii_net":    r.get("fii_net"),
                "fii_net_5d": r.get("fii_net_5d"),
                "fii_net_20d":r.get("fii_net_20d"),
                "fii_flag":   r.get("fii_flag"),
                "dii_net":    r.get("dii_net"),
                "dii_flag":   r.get("dii_flag"),
            }
            break

    # ── __FINAL_PICKS__: portfolio guidance + ranked_candidates ──────────────
    final_picks_data = None
    try:
        fp_rows = (
            sb.table("ai_context")
              .select("conviction_reason,strategy_validation,ai_note,"
                      "conviction,suggested_action")
              .eq("date", signal_date)
              .eq("symbol", "__FINAL_PICKS__")
              .limit(1).execute().data
        )
        if fp_rows:
            row = fp_rows[0]
            if row.get("conviction_reason"):
                final_picks_data = json.loads(row["conviction_reason"])
                if row.get("strategy_validation"):
                    try:
                        guidance_data = json.loads(row["strategy_validation"])
                        final_picks_data.update(guidance_data)
                    except Exception as e:
                        logger.warning(f"strategy_validation parse failed: {e}")
            final_picks_data = final_picks_data or {}
            final_picks_data["_sizing"]  = row.get("suggested_action")
            final_picks_data["_ai_note"] = row.get("ai_note", "")
    except Exception as e:
        logger.warning(f"__FINAL_PICKS__ load failed: {e}")

    # ── __MARKET_INTEL__ ─────────────────────────────────────────────────────
    market_intel = {}
    try:
        mi_rows = (
            sb.table("ai_context")
              .select("conviction_reason,ai_note,suggested_action")
              .eq("date", signal_date)
              .eq("symbol", "__MARKET_INTEL__")
              .limit(1).execute().data
        )
        if mi_rows:
            r    = mi_rows[0]
            full = json.loads(r.get("conviction_reason") or "{}")
            market_intel = {
                "summary":     r.get("ai_note", "")[:300],
                "sizing":      r.get("suggested_action"),
                "fii_bias":    (full.get("fii_outlook") or {}).get("5session_bias"),
                "fii_sectors": (full.get("fii_outlook") or {}).get("favoured_sectors", []),
                "fii_exit":    (full.get("fii_outlook") or {}).get("exit_sectors", []),
                "echo":        (full.get("market_tone") or {}).get("echo_comparison", ""),
                "alerts":      [
                    a for a in (full.get("regulatory_alerts") or [])
                    if a.get("action") not in ("NO_ACTION", None)
                ][:5],
            }
    except Exception as e:
        logger.warning(f"__MARKET_INTEL__ load failed: {e}")

    # ── Open positions ────────────────────────────────────────────────────────
    open_pos = (
        sb.table("open_positions")
          .select(
              "symbol,company_name,sector,strategy,"
              "entry_date,entry_price,current_price,current_value,"
              # locked_profit, target_1/2/3, trailing_sl_pct, event_risk,
              # upcoming_news and lifecycle were all read here and are NULL on
              # every row — no module writes any of them to open_positions.
              # They render as blanks, which reads as "nothing to report" rather
              # than "never populated", so the digest quietly under-reported.
              # Replaced with the columns the risk model actually maintains.
              "invested_value,unrealized_pnl,pnl_pct,"
              "sl_type,high_water_mark,active_sl,"
              "exit_signal,action_required,target_price,target_hit,"
              "signal_date,signal_id,strategy,signal_subtype,"
              "original_qty,current_qty,partial_bookings,status,"
              # The live risk model. planned_stop is what the position was
              # SIZED on and never moves; active_sl is where the stop is now.
              # r_multiple_current is the unit every exit rule is written in.
              "planned_stop,planned_target,planned_risk_pct,r_multiple_current,"
              "max_favorable_excursion,max_adverse_excursion,"
              "breakeven_moved,trail_activated,partial_booked_qty,"
              "entry_signal_type,regime_at_entry,reconcile_status,kite_qty,"
              "ai_recommended_action,ai_action_reason,ai_action_confidence,"
              "ai_action_urgency,ai_action_updated_at,lifecycle_at_entry"
          )
          .eq("status", "ACTIVE")
          .execute().data
    )

    # ── Entry thesis — now from signal_output_daily (same field names) ────────
    entry_thesis_map: dict[str, str] = {}
    pos_symbols = [p.get("symbol") for p in open_pos if p.get("symbol")]
    signal_dates_needed = list({
        str(p.get("signal_date") or "")
        for p in open_pos if p.get("signal_date")
    })
    for sd in signal_dates_needed[:5]:
        if not sd:
            continue
        try:
            rows = (
                sb.table("signal_output_daily")
                  .select("symbol,ai_conviction_reason")
                  .eq("date", sd)
                  .in_("symbol", pos_symbols)
                  .execute().data
            )
            for r in rows:
                sym = r.get("symbol")
                acr = r.get("ai_conviction_reason") or ""
                if sym and acr and sym not in entry_thesis_map:
                    entry_thesis_map[sym] = acr[:200]
        except Exception as e:
            logger.warning(f"Entry thesis lookup failed for {sd}: {e}")

    # ── Global cues (always live — pre-market prices) ─────────────────────────
    cues: dict = {}
    try:
        cues_rows = (
            sb.table("global_cues")
              .select(
                  "gift_nifty,gift_nifty_chg_pct,gap_signal,brent_crude,brent_chg_pct,"
                  "gold_price,usd_inr,us_dow_chg_pct,sp500_chg_pct,us_nasdaq_chg_pct"
              )
              .eq("session", "EVENING").order("date", desc=True).limit(1).execute().data
        )
        cues = cues_rows[0] if cues_rows else {}
    except Exception as e:
        logger.warning(f"global_cues load failed: {e}")

    # ── Upcoming events ───────────────────────────────────────────────────────
    upcoming_events: list[dict] = []
    watchlist_symbols: list[str] = []
    if final_picks_data and final_picks_data.get("ranked_candidates"):
        watchlist_symbols = [
            r["symbol"] for r in final_picks_data["ranked_candidates"]
            if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP", "WAIT_FOR_TRIGGER")
            and r.get("symbol")
        ]
    all_event_symbols = list(set(pos_symbols + watchlist_symbols))
    if all_event_symbols:
        try:
            ts = str(today_ist())
            tc = str(today_ist() + timedelta(days=7))
            upcoming_events = (
                sb.table("nifty_upcoming_events")
                  .select("symbol,purpose,details,event_date,days_to_event")
                  .in_("symbol", all_event_symbols)
                  .gte("event_date", ts)
                  .lte("event_date", tc)
                  .order("event_date")
                  .execute().data
            ) or []
        except Exception as e:
            logger.warning(f"Upcoming events load failed: {e}")

    # ── Lesson counts ─────────────────────────────────────────────────────────
    lessons_ai = lessons_rb = 0
    try:
        lesson_rows = (
            fetch_all(lambda: sb.table("lessons")
              .select("source")
              .gte("date", str(today_ist() - timedelta(days=7))))
        )
        lessons_ai = sum(1 for r in lesson_rows if "AI:" in (r.get("source") or ""))
        lessons_rb = sum(1 for r in lesson_rows if "RULE" in (r.get("source") or "").upper())
    except Exception:
        pass

    # ── Data anomalies ────────────────────────────────────────────────────────
    anomalies: list[dict] = []
    try:
        _raw = (
            sb.table("data_anomalies")
              .select("check_name,message,severity,created_at")
              .eq("date", today)
              .eq("severity", "ERROR")
              .order("created_at", desc=True)
              .execute().data or []
        )
        # Keep only the newest verdict per check. data_quality_monitor now
        # supersedes the day's rows on every run, so duplicates should not
        # occur — but the digest is the one place a stale ERROR is most
        # expensive, because it is read as today's state and it contradicted a
        # populated section two lines below it. Cheap insurance.
        _seen: set[str] = set()
        for _a in _raw:
            _name = _a.get("check_name") or ""
            if _name in _seen:
                continue
            _seen.add(_name)
            anomalies.append(_a)
    except Exception:
        pass

    # ── Portfolio health snapshot ─────────────────────────────────────────────
    portfolio_summary: dict = {}
    if open_pos:
        total_invested  = sum(float(p.get("invested_value")  or 0) for p in open_pos)
        total_pnl       = sum(float(p.get("unrealized_pnl")  or 0) for p in open_pos)
        total_locked    = sum(
            (float(p.get("partial_booked_qty") or 0)
             * (float(p.get("partial_booked_price") or 0) - float(p.get("entry_price") or 0)))
            if (p.get("partial_booked_qty") and p.get("partial_booked_price")) else 0.0
            for p in open_pos)
        pnl_pct_overall = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        winners         = sum(1 for p in open_pos if float(p.get("pnl_pct") or 0) > 0)
        losers          = sum(1 for p in open_pos if float(p.get("pnl_pct") or 0) < 0)
        near_sl_count   = sum(
            1 for p in open_pos
            if (sl_proximity_pct(p.get("current_price"), p.get("active_sl")) or 99) <= 3.0
        )
        target_hit_count = sum(1 for p in open_pos if p.get("target_hit"))
        partial_done    = sum(
            1 for p in open_pos
            if int(p.get("current_qty") or 0) < int(p.get("original_qty") or 0)
        )
        portfolio_summary = {
            "total_invested":    total_invested,
            "total_pnl":         total_pnl,
            "pnl_pct_overall":   pnl_pct_overall,
            "total_locked":      total_locked,
            "winners":           winners,
            "losers":            losers,
            "near_sl_count":     near_sl_count,
            "target_hit_count":  target_hit_count,
            "partial_done":      partial_done,
        }

    return {
        "signal_date":       signal_date,
        "display_date":      today,
        "final_picks":       final_picks_data,
        "market_intel":      market_intel,
        "signals":           signals,
        "open_pos":          open_pos,
        "entry_thesis_map":  entry_thesis_map,
        "regime":            regime,
        "fii":               fii,
        "cues":              cues,
        "msl_map":           msl_map,
        "upcoming_events":   upcoming_events,
        "lessons_ai":        lessons_ai,
        "lessons_rb":        lessons_rb,
        "anomalies":         anomalies,
        "portfolio_summary": portfolio_summary,
    }

def _format_event_warning(msl: dict) -> str:
    """
    Returns a Telegram-formatted warning line if this stock has a corporate
    event within 5 days (results, board meeting, AGM, dividend, bonus, split).
    Reads pre_results_flag, upcoming_event_type, upcoming_event_days directly
    from the signal_output_daily row (passed in as the msl dict after migration).
    Returns empty string if no event — safe to call unconditionally.
    """
    if not msl.get("pre_results_flag"):
        return ""
    days  = msl.get("upcoming_event_days")
    etype = msl.get("upcoming_event_type") or "Corporate Event"
    if days == 0:
        timing = "TODAY"
    elif days == 1:
        timing = "TOMORROW"
    elif days is not None:
        timing = f"in {days}d"
    else:
        timing = "soon"
    return f"   ⚠️ <b>{esc(etype)} {timing}</b> — size smaller, widen SL or skip"

# ── Regime header ──────────────────────────────────────────────────────────

def build_regime_header(regime: dict, fii: dict, cues: dict) -> list[str]:
    r_label = regime.get("predicted_regime") or regime.get("regime", "?")
    r_conf  = float(regime.get("regime_confidence") or 0)
    r_icon  = regime_icon(r_label)
    nifty   = regime.get("nifty_price")
    vix     = regime.get("india_vix")
    breadth = regime.get("avg_sector_breadth")
    adr     = regime.get("advance_decline_ratio")
    chg_1d  = regime.get("nifty_1d_chg_pct")
    a200    = regime.get("above_200dma_pct")
    pcr     = regime.get("nifty_pcr")

    lines = [
        f"{r_icon} <b>{r_label}</b>"
        + (f" <i>({r_conf:.0%} conf)</i>" if r_conf else "")
        + (f"  Nifty: <b>{fmt_price(nifty)}</b>" if nifty else "")
        + (f" {fmt_chg(chg_1d)}" if chg_1d is not None else ""),
    ]

    details = []
    if vix:     details.append(f"VIX:{vix}")
    if breadth: details.append(f"Brdth:{breadth:.0f}%")
    if a200:    details.append(f"Ab200:{a200:.0f}%")
    if adr:     details.append(f"A/D:{adr:.1f}")
    if pcr:     details.append(f"PCR:{pcr}")
    if details: lines.append(f"  <i>{' · '.join(details)}</i>")

    if cues.get("gift_nifty"):
        g   = cues.get("gift_nifty")
        gc  = cues.get("gift_nifty_chg_pct", 0)
        gap = cues.get("gap_signal", "")
        ico = "📈" if float(gc or 0) > 0 else "📉"
        lines.append(f"  {ico} Gift Nifty: <b>{fmt_price(g)}</b> ({fmt_chg(gc)}) [{gap}]")

    global_parts = []
    if cues.get("us_dow_chg_pct"):    global_parts.append(f"DOW:{fmt_chg(cues['us_dow_chg_pct'])}")
    if cues.get("sp500_chg_pct"):     global_parts.append(f"S&P:{fmt_chg(cues['sp500_chg_pct'])}")
    if cues.get("us_nasdaq_chg_pct"): global_parts.append(f"NQ:{fmt_chg(cues['us_nasdaq_chg_pct'])}")
    if cues.get("brent_crude"):
        global_parts.append(
            f"Brent:${float(cues['brent_crude']):.0f}"
            f"({fmt_chg(cues.get('brent_chg_pct'))})"
        )
    if cues.get("usd_inr"): global_parts.append(f"₹{float(cues['usd_inr']):.1f}/$")
    if cues.get("us_10yr_yield"):
        bps = cues.get("us_10yr_chg_bps")
        bps_str = f"({float(bps):+.0f}bps)" if bps is not None else ""
        global_parts.append(f"US10Y:{float(cues['us_10yr_yield']):.2f}%{bps_str}")
    if global_parts:
        lines.append(f"  🌐 {' · '.join(global_parts)}")

    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(f"  {fii_line}")

    return lines


# ── Entry zone line (fallback — near-miss + raw signal sections only) ─────
# PATCH: simplified to plain text (no <b> tags) so esc() callers are safe.
# TIER_1 and TIER_2 blocks now compute zone inline — this function is only
# used in near-miss and raw-signal fallback displays.

def zone_line(symbol: str, msl_map: dict) -> str:
    m    = msl_map.get(symbol, {})
    lo   = m.get("entry_zone_low")
    hi   = m.get("entry_zone_high")
    dist = m.get("dist_entry_pct")
    er   = m.get("expected_r")
    if not lo:
        return ""
    lo = float(lo)
    zh = float(hi) if hi else round(lo * 1.02, 0)
    dist_str = f" ({abs(float(dist)):.1f}%↓)" if dist is not None else ""
    er_str   = f" · ExpR:{float(er):.1f}×" if er else ""
    return f"₹{lo:,.0f}–₹{zh:,.0f}{dist_str}{er_str}"


# ── Open position block ────────────────────────────────────────────────────

def build_position_block(
    p: dict,
    sig_map: dict,
    entry_thesis_map: dict,
    cues: dict,
    include_gap_risk: bool = False,
    brief: bool = False,
) -> list[str]:
    """
    Full or brief lifecycle block for a single open position.

    brief=True  → morning mode: header + SL/target + gap risk + action
    brief=False → evening mode: full detail including rationale, partial bookings

    PATCH 3: entry thesis replaced with 85-char compressed rationale.
    """
    sym    = p.get("symbol", "?")
    pnl    = float(p.get("pnl_pct")        or 0)
    cp     = float(p.get("current_price")  or 0)
    sl     = float(p.get("active_sl")      or 0)
    hwm    = float(p.get("high_water_mark") or 0)
    entry  = float(p.get("entry_price")    or 0)
    # locked_profit has no writer. Derive realised profit from the partial the
    # exit engine actually booked, which IS recorded.
    _pb_qty   = float(p.get("partial_booked_qty")   or 0)
    _pb_price = float(p.get("partial_booked_price") or 0)
    locked = (_pb_qty * (_pb_price - entry)) if (_pb_qty and _pb_price and entry) else 0.0
    ico    = "📈" if pnl >= 0 else "📉"
    prox   = sl_proximity_pct(cp, sl)
    sl_w   = "  🚨<b>Near SL!</b>" if prox is not None and prox <= 3.0 else ""

    today_sig  = sig_map.get(sym, {})
    today_type = today_sig.get("signal_type", "—")
    action_req = p.get("action_required") or "HOLD"
    exit_sig   = p.get("exit_signal") or ""

    # open_positions.lifecycle has no writer — it is always NULL, which is why
    # this used to render "[?]" on every single position. sig_map (today's
    # signal_output_daily row for this symbol, when it's still on today's
    # shortlist) carries the CURRENT lifecycle classification, the same field
    # the AI advisory below reads. When the position has since dropped off
    # today's shortlist, fall back to lifecycle_at_entry — but say so, since a
    # position bought FRESH weeks ago is not necessarily still FRESH.
    lifecycle_now = today_sig.get("lifecycle")
    lifecycle_tag = lifecycle_now or (
        f"{p['lifecycle_at_entry']}@entry" if p.get("lifecycle_at_entry") else "?"
    )

    lines: list[str] = []

    # ── Header ──
    lines.append(
        f"\n  {ico} <b>{sym}</b> [{p.get('strategy', '?')}] [{lifecycle_tag}]"
        f"  P&L: <b>{fmt_pct(pnl)}</b>  Held: {days_held(p.get('entry_date'))}{sl_w}"
    )

    # ── Price / SL / HWM ──
    price_parts = []
    if entry: price_parts.append(f"Entry:{fmt_price(entry)}")
    if cp:    price_parts.append(f"CMP:{fmt_price(cp)}")
    if sl:
        prox_str = f"({prox:+.1f}%)" if prox is not None else ""
        price_parts.append(f"SL:{fmt_price(sl)}{prox_str}")
    if hwm and hwm > cp: price_parts.append(f"HWM:{fmt_price(hwm)}")
    if locked > 0:       price_parts.append(f"🔒{fmt_inr(locked)}")
    if price_parts:
        lines.append(f"  {' · '.join(price_parts)}")

    # ── Gap risk (morning mode only) ──
    if include_gap_risk and cues.get("gift_nifty_chg_pct") and cp > 0 and sl > 0:
        gap_pct  = float(cues.get("gift_nifty_chg_pct") or 0)
        est_open = cp * (1 + gap_pct / 100)
        if est_open <= sl:
            lines.append(
                f"  🚨 <b>SL BREACH RISK AT OPEN</b> "
                f"EstOpen:{fmt_price(est_open)} vs SL:{fmt_price(sl)}"
            )
        elif sl_proximity_pct(est_open, sl) is not None and sl_proximity_pct(est_open, sl) <= 1.5:
            lines.append(
                f"  ⚠️ Gap narrows SL cushion  EstOpen:{fmt_price(est_open)}"
            )

    # ── Targets ──
    # target_1/2/3 have no writer — the risk model keeps ONE target, at 3R.
    # Passing the live one into the same renderer keeps the T1 formatting.
    tp = target_progress(cp, p.get("planned_target") or p.get("target_price"), None, None)
    if tp:
        lines.append(f"  Targets: {tp}")

    # ── Action required (pre-existing, mechanical/price-based — untouched) ──
    a_ico = action_icon(action_req)
    action_line = f"  {a_ico} Action: <b>{action_req}</b>"
    if exit_sig:
        action_line += f"  ⚡Signal: <b>{exit_sig}</b>"
    elif today_type not in ("—", ""):
        action_line += f"  Signal: {today_type}"
    lines.append(action_line)

    # ── AI advisory (2026-07, news/event-aware — separate from the line above
    # on purpose, written to its own ai_-prefixed columns so it can never
    # collide with whatever populates action_required/exit_signal/event_risk).
    # Only ever present when the AI found something specific worth flagging —
    # sparse by design, most positions on most days will have none of this.
    ai_action = p.get("ai_recommended_action")
    if ai_action and ai_action not in ("NO_ACTION", "HOLD"):
        _urg_ico = {"IMMEDIATE": "🚨", "THIS_WEEK": "⚠️"}.get(p.get("ai_action_urgency"), "🤖")

        # Advisories persist on the row until step 19 overwrites them, and step
        # 19 only writes position_actions for positions it flagged — so a
        # position it says nothing about today keeps yesterday's advice
        # indefinitely. That produced "protect +4.2% gain" on a position sitting
        # at +10.4%: not wrong when written, just silently two days old and
        # rendered as if it were today's.
        #
        # The advice can still be useful when stale, so it is dated rather than
        # dropped. What must not happen is presenting it as current.
        age_note = ""
        upd = p.get("ai_action_updated_at")
        if upd:
            try:
                # `datetime` (module) shadows the stdlib class if imported
                # unaliased in a file this size, so this aliases it to `_dt` —
                # but IST was never imported here and the two lines below kept
                # referencing the unaliased `datetime` and a bare `IST` that no
                # scope in this function ever bound. Silently caught by the
                # bare `except: pass` below, so age_note — the annotation this
                # exact block exists to attach, so a two-day-old "protect your
                # gain" advisory is never mistaken for today's — never once
                # computed. Found by pyflakes (undefined name), not by seeing
                # it fail, because it never logged anything when it did.
                from datetime import datetime as _dt
                from config import IST
                when = _dt.fromisoformat(str(upd).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = IST.localize(when)
                days = (_dt.now(IST).date() - when.astimezone(IST).date()).days
                if days >= 1:
                    age_note = (f" <i>({days}d old — from "
                                f"{when.astimezone(IST):%d %b}, P&amp;L was different then)</i>")
            except Exception as e:
                # Was a bare `pass` — the NameError above was silent at every
                # log level for as long as it existed. Logged now so the NEXT
                # time this block breaks, it is findable without pyflakes.
                logger.debug(f"  AI advisory age note skipped: {e}")
        else:
            age_note = " <i>(undated)</i>"

        lines.append(
            f"  {_urg_ico} <b>AI Advisory: {ai_action}</b>"
            + (f" — {esc(p.get('ai_action_reason',''))}" if p.get("ai_action_reason") else "")
            + age_note
        )

    if brief:
        # event_risk had no writer on open_positions and always rendered blank.
        # Per-position event risk reaches the digest through the UPCOMING EVENTS
        # section, which reads nifty_upcoming_events directly.
        return lines

    # ── Partial bookings (evening only) ──
    pb = partial_booking_summary(
        p.get("partial_bookings"),
        p.get("original_qty"),
        p.get("current_qty"),
    )
    if pb:
        lines.append(f"  📊 Partial: {pb}")

    # Event risk / upcoming news are NOT read from open_positions: neither
    # column has ever had a writer there. The UPCOMING EVENTS section below the
    # positions block carries the same information from nifty_upcoming_events,
    # which ingest_nse_events actually maintains.

    # ── PATCH 3: Compressed rationale (evening only, max 85 chars) ──
    thesis_raw = (entry_thesis_map.get(sym, "")
                  or today_sig.get("ai_conviction_reason", ""))
    if thesis_raw:
        t = thesis_raw.split("| Entry:")[0].strip()
        if "] " in t:
            t = t.split("] ", 1)[-1]
        lines.append(f"   💬 {esc(t[:85])}")

    return lines


# ── Evening digest ─────────────────────────────────────────────────────────

def build_evening(data: dict, sb=None) -> str:
    fp       = data.get("final_picks")
    mi       = data.get("market_intel", {})
    signals  = data["signals"]
    open_pos = data["open_pos"]
    regime   = data["regime"]
    fii      = data["fii"]
    cues     = data["cues"]
    msl_map  = data["msl_map"]
    ps       = data.get("portfolio_summary", {})
    eth_map  = data.get("entry_thesis_map", {})
    date_str = data["signal_date"]

    sig_map = {s.get("symbol"): s for s in signals}

    display = data.get("display_date", date_str)
    stale   = display != date_str
    lines = [
        f"<b>📊 TradeOS Evening · {display}</b>"
        + (f"  <i>(data: {date_str})</i>" if stale else ""),
        ""
    ]
    lines += build_regime_header(regime, fii, cues)

    # ── Anomalies ──
    if data.get("anomalies"):
        lines += ["", "🚨 <b>DATA ALERTS</b>"]
        for a in data["anomalies"]:
            lines.append(f"  ⚠️ {a.get('check_name','?')}: {a.get('message','')}")
    lines.append("")

    # ── Section 0: MACRO + FII FLOW ────────────────────────────────────────
    if mi.get("fii_bias") or mi.get("alerts"):
        lines.append("═══ <b>🌍 MACRO + FII FLOW</b> ═══")
        if mi.get("fii_bias"):
            _fii_line = f"  FII 5-sess: <b>{mi['fii_bias']}</b>"
            if mi.get("fii_sectors"):
                _fii_line += f"  ▲ Buying: {', '.join(mi['fii_sectors'])}"
            if mi.get("fii_exit"):
                _fii_line += f"  ▼ Selling: {', '.join(mi['fii_exit'])}"
            lines.append(_fii_line)
        for _alert in (mi.get("alerts") or []):
            _urgency = str(_alert.get("urgency_level") or _alert.get("urgency") or "INFO").upper()
            _ico     = {"HIGH": "🚨", "MEDIUM": "⚠️", "IMMEDIATE": "🚨"}.get(_urgency, "📌")
            _sym_tag = (
                f" [{', '.join(str(s) for s in _alert['affected_symbols'][:3])}]"
                if _alert.get("affected_symbols") else ""
            )
            lines.append(
                f"  {_ico} [{_urgency}] {esc(_alert.get('action', ''))} — "
                f"{esc(_alert.get('news_item', ''))}{_sym_tag}"
            )
        lines.append("")

    # ── Section 1: Market Intelligence ────────────────────────────────────
    if mi.get("summary"):
        lines.append("═══ <b>📡 MARKET INTELLIGENCE</b> ═══")
        lines.append(f"  {mi['summary']}")
        if mi.get("echo"):
            lines.append(f"  📖 <i>{esc(mi['echo'])}</i>")
        lines.append("")

    # ── Section 2: Portfolio Health Snapshot ──
    if ps and open_pos:
        pnl_ico = "📈" if ps.get("pnl_pct_overall", 0) >= 0 else "📉"
        lines.append("═══ <b>💼 PORTFOLIO HEALTH</b> ═══")
        lines.append(
            f"  {len(open_pos)} positions  "
            f"Invested: <b>{fmt_inr(ps['total_invested'])}</b>  "
            f"{pnl_ico} P&L: <b>{fmt_inr(ps['total_pnl'])}</b> "
            f"({fmt_pct(ps['pnl_pct_overall'])})"
            + (f"  🔒 Locked: {fmt_inr(ps['total_locked'])}" if ps.get("total_locked", 0) > 0 else "")
        )
        status_parts = [f"✅ {ps['winners']} win  ❌ {ps['losers']} loss"]
        if ps.get("near_sl_count", 0) > 0:
            status_parts.append(f"🚨 {ps['near_sl_count']} near SL")
        if ps.get("target_hit_count", 0) > 0:
            status_parts.append(f"🎯 {ps['target_hit_count']} target hit")
        if ps.get("partial_done", 0) > 0:
            status_parts.append(f"📊 {ps['partial_done']} partially exited")
        lines.append(f"  {'  ·  '.join(status_parts)}")
        lines.append("")

    # ── Section 3: Open Positions (full lifecycle) ──
    if open_pos:
        lines.append(f"═══ <b>📂 OPEN POSITIONS ({len(open_pos)})</b> ═══")
        for p in open_pos:
            lines += build_position_block(
                p, sig_map, eth_map, cues,
                include_gap_risk=False, brief=False,
            )
        lines.append("")

    # ── Section 4: EXIT signals ──
    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append(f"🔴 <b>EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            reason  = s.get("filter_reason") or s.get("ai_conviction_reason") or ""
            cmp_ex  = float(s.get("current_price") or msl_map.get(s.get("symbol", ""), {}).get("current_price") or 0)
            cmp_ex_str = f"  CMP:₹{cmp_ex:,.0f}" if cmp_ex else ""
            lines.append(
                f"  <b>{s['symbol']}</b>{cmp_ex_str}"
                + (f"  <i>— {esc(reason)}</i>" if reason else "")
            )
        lines.append("")

    # ── AVOID_ENTRY_EVENT warnings ──
    avoid_events = [s for s in signals if s.get("signal_type") == "AVOID_ENTRY_EVENT"]
    if avoid_events:
        lines.append("⚠️ <b>AVOID ENTRY — EVENT RISK</b>")
        for s in avoid_events:
            cmp_av     = float(s.get("current_price") or msl_map.get(s.get("symbol", ""), {}).get("current_price") or 0)
            cmp_av_str = f"  CMP:₹{cmp_av:,.0f}" if cmp_av else ""
            lines.append(
                f"  🚫 <b>{s['symbol']}</b>{cmp_av_str} [{s.get('sector','?')}]"
                + (f" — {esc(s.get('filter_reason',''))}" if s.get("filter_reason") else "")
            )
        lines.append("")

    # ── Sections 5–9: Tier structure from step 19 ──
    if fp and fp.get("ranked_candidates"):
        ranked      = fp["ranked_candidates"]
        tier1       = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
        tier2       = [r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"]
        tier3       = [r for r in ranked if r.get("action") == "SKIP"]
        guidance    = fp.get("portfolio_guidance", {})
        warnings    = fp.get("sector_exposure_warnings", [])
        corr_groups = fp.get("correlation_groups", [])

        # ── PATCH 7: Single guidance line ──
        sizing  = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        _gtext  = (guidance.get("capital_deployment_narrative")
                   or guidance.get("new_positions_guidance")
                   or fp.get("_ai_note") or "")
        _sec_parts = []
        if guidance.get("sectors_to_overweight"):
            _sec_parts.append(f"▲ {'/'.join(guidance['sectors_to_overweight'])}")
        if guidance.get("sectors_to_underweight"):
            _sec_parts.append(f"▼ {'/'.join(guidance['sectors_to_underweight'])}")
        lines.append(
            f"💼 <b>SIZING: {sizing}</b>"
            + (f"  {'  '.join(_sec_parts)}" if _sec_parts else "")
        )
        if _gtext:
            lines.append(f"  <i>{esc(_gtext[:150])}</i>")
        lines.append("")

        # ── PATCH 8: ACT NOW bucket — 3-line compact + readiness ──
        if tier1:
            if _READINESS_AVAILABLE and sb:
                try:
                    tier1 = compute_entry_readiness(
                        tier1, msl_map, sb=sb, use_live=False
                    )
                except Exception as e:
                    logger.warning(f"entry_readiness enrichment failed: {e}")

            lines.append(f"⭐ <b>ACT NOW ({len(tier1)})</b>")
            for c in tier1:
                sym    = c.get("symbol", "?")
                action = c.get("action") or ""
                alloc  = float(c.get("suggested_allocation_pct") or 0)
                corr   = c.get("correlation_group") or ""
                msl    = msl_map.get(sym, {})
                zl     = float(msl.get("entry_zone_low")  or 0)
                zh     = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                er     = float(msl.get("expected_r")      or 2.0)
                # ── Use the PERSISTED trade plan, do not re-derive it ─────
                # This used to be:
                #     sl_p = zl * (1 - STOP_BUFFER)
                #     t1_p = zl * (1 + STOP_BUFFER * er)
                # a THIRD copy of the risk model — the same flat-percent
                # version already removed from generate_signals, where it
                # collapsed the target to roughly 40% of its intended distance.
                # compute_msl anchors the plan at 1.5xATR / 3xATR and
                # final_snapshot now carries it through, so the alert must SHOW
                # that plan rather than invent a competing one. An alert whose
                # stop disagrees with the stop the signal was validated against
                # is worse than no alert at all.
                sl_p   = _num(msl.get("planned_stop"))
                t1_p   = _num(msl.get("planned_target"))
                rr     = _num(msl.get("implied_rr"))
                if rr is None and sl_p and t1_p and zl and sl_p < zl:
                    rr = round((t1_p - zl) / (zl - sl_p), 2)
                r_icon    = c.get("readiness_icon") or entry_action_icon(action)
                r_score   = c.get("readiness_score")
                r_label   = c.get("readiness_label", "")
                dist      = msl.get("dist_entry_pct")
                dist_str  = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""
                score_str = f"[{r_score}/100·{r_label}]" if r_score else ""

                cmp       = float(msl.get("current_price") or 0)
                sec_rank  = msl.get("sector_rank_at_entry")
                ind_rank  = msl.get("industry_rank")
                ind_state = (msl.get("industry_state") or "").upper()
                cmp_str   = f" (CMP: ₹{cmp:,.0f})" if cmp else ""
                rank_parts = []
                if sec_rank: rank_parts.append(f"Sec #{sec_rank}")
                if ind_rank: rank_parts.append(f"Ind #{ind_rank}" + (f"·{ind_state}" if ind_state else ""))
                rank_str = "  📊 " + " · ".join(rank_parts) if rank_parts else ""

                lines.append(
                    f"\n  {r_icon} <b>{sym}</b>{cmp_str}"
                    + (f" [{action}]" if action else "")
                    + (f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}  <i>[DB]</i>"
                       if zl else "  ⚠️ <i>Zone data unavailable</i>")
                    + (f"  <b>{alloc:.0f}%</b>" if alloc else "")
                    + (f"  📎{corr}" if corr else "")
                    + (f"  → T1₹{t1_p:,.0f} | SL₹{sl_p:,.0f} · RR {rr}×  {score_str}"
                       if (t1_p and sl_p) else f"  {score_str}")
                )
                if rank_str:
                    lines.append(rank_str)

                thesis   = c.get("thesis") or c.get("ai_conviction_reason") or ""
                catalyst = c.get("catalyst") or ""
                if "] " in thesis:
                    thesis = thesis.split("] ", 1)[-1]
                thesis = thesis.split("| Entry:")[0].strip()
                if catalyst and len(thesis) < 70:
                    thesis += f" · 💡 {esc(catalyst)}"
                if thesis:
                    lines.append(f"   💬 {esc(thesis)}")

                parts3 = []
                if c.get("entry_note"):
                    parts3.append(f"📍 {esc(c['entry_note'])} <i>[AI]</i>")
                if c.get("invalidation"):
                    parts3.append(f"❌ {esc(c['invalidation'])} <i>[AI]</i>")
                breakdown = c.get("readiness_breakdown", "")
                if breakdown:
                    parts3.append(f"<i>{breakdown}</i>")
                if parts3:
                    lines.append(f"   {' · '.join(parts3)}")

                if c.get("risks"):
                    risk_str = " · ".join(esc(str(r)) for r in (c["risks"] or [])[:2])
                    if risk_str:
                        lines.append(f"   ⚠️ {risk_str}")

                if c.get("lessons_applied"):
                    ls = " · ".join(esc(l) for l in (c["lessons_applied"] or [])[:2])
                    if ls:
                        lines.append(f"   📚 <i>{ls[:120]}</i>")

                evt_warn = _format_event_warning(msl)
                if evt_warn:
                    lines.append(evt_warn)

            lines.append("")

        # ── PATCH 9: WATCH bucket — 2-line compact ──
        if tier2:
            lines.append(f"🔭 <b>WATCH FOR TRIGGER ({len(tier2)})</b>")
            for c in tier2:
                sym   = c.get("symbol", "?")
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                dist  = msl.get("dist_entry_pct")
                dist_str = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""

                cmp_t2      = float(msl.get("current_price") or 0)
                sec_rank_t2 = msl.get("sector_rank_at_entry")
                ind_rank_t2 = msl.get("industry_rank")
                cmp_str_t2  = f" (CMP: ₹{cmp_t2:,.0f})" if cmp_t2 else ""
                rk_t2_parts = []
                if sec_rank_t2: rk_t2_parts.append(f"Sec #{sec_rank_t2}")
                if ind_rank_t2: rk_t2_parts.append(f"Ind #{ind_rank_t2}")
                rk_t2_str   = "  📊 " + " · ".join(rk_t2_parts) if rk_t2_parts else ""

                lines.append(
                    f"\n  {entry_action_icon('WAIT_FOR_TRIGGER')} <b>{sym}</b>{cmp_str_t2}"
                    f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}"
                    + (rk_t2_str)
                )

                parts2 = []
                if c.get("entry_note"):
                    parts2.append(f"📍 {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    parts2.append(f"❌ {esc(c['invalidation'])}")
                if parts2:
                    lines.append(f"   {' · '.join(parts2)}")

                evt_warn = _format_event_warning(msl)
                if evt_warn:
                    lines.append(evt_warn)

            lines.append("")

        # MONITOR bucket (SKIP)
        if tier3:
            lines.append(f"👁 <b>MONITOR ({len(tier3)})</b>")
            t3_parts = []
            for c in tier3:
                sym_t3  = c.get("symbol", "?")
                cmp_t3  = float(msl_map.get(sym_t3, {}).get("current_price") or 0)
                cmp_t3_str = f"·₹{cmp_t3:,.0f}" if cmp_t3 else ""
                t3_parts.append(f"{sym_t3}{cmp_t3_str}")
            lines.append(f"  {' · '.join(t3_parts)}")
            lines.append("")

        # Near-miss upgrades
        near_miss = [
            s for s in signals
            if s.get("signal_type") == "WATCH"
            and "NEAR_MISS_UPGRADE" in (s.get("ai_note") or "")
        ]
        if near_miss:
            lines.append(f"💡 <b>NEAR MISS — AI FLAGGED ({len(near_miss)})</b>")
            lines.append("  <i>Didn't technically qualify — AI sees macro/news tailwind</i>")
            for s in near_miss:
                note_raw = (s.get("ai_note") or "").replace("[NEAR_MISS_UPGRADE:", "").split("]")
                flag   = note_raw[0] if note_raw else "?"
                reason = note_raw[1].strip() if len(note_raw) > 1 else ""
                ez     = zone_line(s["symbol"], msl_map)
                cmp_nm = float(s.get("current_price") or msl_map.get(s.get("symbol", ""), {}).get("current_price") or 0)
                cmp_nm_str = f"  CMP:₹{cmp_nm:,.0f}" if cmp_nm else ""
                lines.append(f"\n  💡 <b>{s['symbol']}</b>{cmp_nm_str} [{flag}]  {s.get('sector', '?')}")
                if reason: lines.append(f"  {esc(reason)}")
                if ez:     lines.append(f"  {esc(ez)}")
            lines.append("")

        # Sector exposure warnings
        if warnings:
            lines.append("⚠️ <b>SECTOR CONCENTRATION</b>")
            for w in warnings:
                lines.append(
                    f"  {esc(w.get('sector', '?'))}: "
                    f"{w.get('candidate_count', '?')} candidates "
                    f"(holding {w.get('already_held', 0)}) — "
                    f"{esc(w.get('recommendation', ''))}"
                    f"  → Allow: <b>{w.get('allow_count', 1)}</b>"
                )
            lines.append("")

        # Correlation groups
        if corr_groups:
            lines.append("⚡ <b>CORRELATION GROUPS</b>")
            for g in corr_groups:
                lines.append(
                    f"  [{g.get('group_label', '?')}]"
                    f" {g.get('symbols', [])} — "
                    f"{esc(g.get('recommendation', ''))}"
                )
            lines.append("")

    else:
        # ── Fallback: step 19 unavailable — raw signal_log ──
        lines.append("<i>⚠️ Step 19 AI picks unavailable — showing raw signals</i>")
        buys = sorted(
            [s for s in signals if s.get("signal_type") in ENTRY_TYPES],
            key=lambda x: float(x.get("score_adjusted") or x.get("score") or 0),
            reverse=True,
        )
        if buys:
            lines.append(f"\n🎯 <b>RAW SIGNALS ({len(buys)})</b>")
            for s in buys:
                cmp_raw = float(s.get("current_price") or 0)
                cmp_raw_str = f" CMP:₹{cmp_raw:,.0f}" if cmp_raw else ""
                lines.append(
                    f"\n  • <b>{s['symbol']}</b>{cmp_raw_str} [{s.get('strategy', '?')}] "
                    f"Score:<b>{float(s.get('score_adjusted') or s.get('score') or 0):.0f}</b>"
                )
                if s.get("ai_conviction_reason"):
                    lines.append(f"  💬 {s['ai_conviction_reason']}")
                ez = zone_line(s["symbol"], msl_map)
                if ez: lines.append(f"  {ez}")
        lines.append("")

    # ── Section 10: Upcoming events (7 days) ──
    upcoming = data.get("upcoming_events", [])
    if upcoming:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>UPCOMING EVENTS — 7 DAYS</b>")
        for e in upcoming[:8]:
            sym     = e.get("symbol", "?")
            tag     = "📂" if sym in pos_sym_set else "🔭"
            d_to    = e.get("days_to_event")
            dt      = str(e.get("event_date", ""))
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")
            d_str   = f"({d_to}d)" if d_to is not None else f"({dt})"
            lines.append(
                f"  {tag} <b>{sym}</b>: {purpose} {d_str}"
                + (f" — {detail}" if detail else "")
            )
        lines.append("")

    # ── PATCH 10: TOMORROW'S GTT ORDERS ──
    if fp and fp.get("ranked_candidates"):
        _t1_orders = [r for r in fp["ranked_candidates"] if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
        if _t1_orders:
            lines.append("─" * 28)
            lines.append("📋 <b>TOMORROW'S GTT ORDERS</b>  <i>Place before 9:15 AM</i>")
            for _c in _t1_orders:
                _sym  = _c.get("symbol", "?")
                _msl  = msl_map.get(_sym, {})
                _zl   = float(_msl.get("entry_zone_low")  or 0)
                _er   = float(_msl.get("expected_r")      or 2.0)
                # ── Use the PERSISTED trade plan, do not re-derive it ──────
                # This used to be a flat-STOP_BUFFER re-derivation — a second,
                # uncoordinated risk model that can print a stop/target the
                # signal was never actually validated against, on the one
                # section whose entire purpose is "place this exact order
                # with your broker." Same fix already applied to the Tier-1
                # card above and to build_morning; see the comment there.
                _sl   = _num(_msl.get("planned_stop"))
                _t1   = _num(_msl.get("planned_target"))
                _rr   = _num(_msl.get("implied_rr"))
                if _rr is None and _sl and _t1 and _zl and _sl < _zl:
                    _rr = round((_t1 - _zl) / (_zl - _sl), 1)
                _sig  = sig_map.get(_sym, {})
                _vref = float(_sig.get("vol_ratio")    or 0)
                _dref = float(_sig.get("delivery_pct") or 0)
                _cmp_gtt     = float(_msl.get("current_price") or 0)
                _cmp_gtt_str = f"  CMP:₹{_cmp_gtt:,.0f}" if _cmp_gtt else ""
                if _zl and _sl and _t1:
                    lines.append(
                        f"  <b>{_sym}</b>{_cmp_gtt_str}  "
                        f"Entry₹{_zl:,.0f} | SL₹{_sl:,.0f} | T1₹{_t1:,.0f} | RR {_rr}×"
                    )
                else:
                    lines.append(
                        f"  <b>{_sym}</b>{_cmp_gtt_str}  "
                        f"⚠️ <i>Zone data unavailable — skip GTT for this symbol</i>"
                    )

                # 2026-07: chase-anchored alternative — reuses ai_max_chase_pct /
                # ai_zone_high_extended, already computed by step 19 this same
                # evening (no new query, no new AI call). Only appears when the
                # AI independently pre-approved chasing THIS stock — a stock
                # trending well enough that the mechanical pullback zone may
                # simply never get touched. Without this, the primary Entry
                # above is the only order offered, and if it never fills,
                # nothing else was ever suggested even though step 19 already
                # had an answer for it.
                _ai_chase_pct = float(_msl.get("ai_max_chase_pct") or 0)
                _ai_zone_ext  = _msl.get("ai_zone_high_extended")
                if _ai_chase_pct > 0 and _cmp_gtt:
                    _chase_entry = round(min(_cmp_gtt, _ai_zone_ext) if _ai_zone_ext else _cmp_gtt, 0)
                    # Same absolute stop/target as the primary order above —
                    # the plan is FROZEN at those price levels (compute_msl),
                    # not re-derived off the chase price. Chasing should show
                    # up as a worse R:R against the same fixed risk, which is
                    # the honest chase penalty; a flat-% re-derivation hid it.
                    _chase_sl    = _sl
                    _chase_t1    = _t1
                    _chase_rr    = (
                        round((_chase_t1 - _chase_entry) / (_chase_entry - _chase_sl), 1)
                        if _chase_sl and _chase_t1 and _chase_sl < _chase_entry else None
                    )
                    lines.append(
                        f"   🟡 If no pullback — AI-cleared chase: Entry₹{_chase_entry:,.0f}"
                        f" | SL₹{_chase_sl:,.0f} | T1₹{_chase_t1:,.0f}"
                        + (f" | RR {_chase_rr}×" if _chase_rr else "")
                        + f"  <i>(approved to +{_ai_chase_pct:.1f}% above mechanical zone)</i>"
                    )

                if _vref or _dref:
                    lines.append(
                        f"   <i>Ref: Vol {_vref:.1f}× · Del {_dref:.0f}%"
                        f" · Confirm Vol>1× at 9:45AM + price≥VWAP</i>"
                    )
            lines.append("")

    # ── Footer ──
    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(fii_line)

    ranked_total = len(fp.get("ranked_candidates", [])) if fp else 0
    lines.append(
        f"<i>━━━ {ranked_total} ranked · {len(exits)} exit · "
        f"{len(open_pos)} open  "
        f"| Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule ━━━</i>"
    )
    return "\n".join(lines)


# ── Morning brief ──────────────────────────────────────────────────────────

def build_morning(data: dict, sb=None) -> str:
    fp        = data.get("final_picks")
    regime    = data["regime"]
    fii       = data["fii"]
    cues      = data["cues"]
    open_pos  = data["open_pos"]
    signals   = data["signals"]
    msl_map   = data["msl_map"]
    eth_map   = data.get("entry_thesis_map", {})
    ps        = data.get("portfolio_summary", {})
    date_str  = data["signal_date"]
    today_str = str(today_ist())

    sig_map = {s.get("symbol"): s for s in signals}

    display = data.get("display_date", date_str)
    stale   = display != date_str
    lines = [
        f"<b>🌅 TradeOS Morning · {display}</b>"
        + (f"  <i>(data: {date_str})</i>" if stale else ""),
        ""
    ]
    lines += build_regime_header(regime, fii, cues)
    lines.append("")

    # ── Today's events at the top ──
    upcoming = data.get("upcoming_events", [])
    today_events = [e for e in upcoming if str(e.get("event_date", "")) == today_str]
    if today_events:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>TODAY'S EVENTS — ACTION REQUIRED</b>")
        for e in today_events:
            tag     = "📂" if e.get("symbol") in pos_sym_set else "🔭"
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")
            lines.append(
                f"  {tag} <b>{e.get('symbol','?')}</b>: {purpose}"
                + (f" — {detail}" if detail else "")
            )
        lines.append("")

    # ── Gap risk summary ──
    gap_pct = float(cues.get("gift_nifty_chg_pct") or 0)
    if gap_pct != 0 and open_pos:
        breach_risk = []
        watch_risk  = []
        for p in open_pos:
            cp = float(p.get("current_price") or 0)
            sl = float(p.get("active_sl") or 0)
            if cp <= 0 or sl <= 0: continue
            est_open = cp * (1 + gap_pct / 100)
            prox_at_open = sl_proximity_pct(est_open, sl)
            if est_open <= sl:
                breach_risk.append(p.get("symbol", "?"))
            elif prox_at_open is not None and prox_at_open <= 2.0:
                watch_risk.append(p.get("symbol", "?"))
        if breach_risk:
            lines.append(
                f"🚨 <b>SL BREACH RISK AT OPEN: {', '.join(breach_risk)}</b>"
            )
            lines.append(
                f"  Estimated gap: {fmt_chg(gap_pct)}  "
                f"— Review these before placing orders!"
            )
            lines.append("")
        elif watch_risk:
            lines.append(
                f"⚠️ Gap narrows SL cushion: {', '.join(watch_risk)}  ({fmt_chg(gap_pct)} gap)"
            )
            lines.append("")

    # ── Open position pulse ──
    if open_pos:
        lines.append(f"═══ <b>📂 POSITION PULSE ({len(open_pos)})</b> ═══")
        if ps:
            lines.append(
                f"  Overall: <b>{fmt_pct(ps.get('pnl_pct_overall'))}</b>  "
                f"✅{ps.get('winners', 0)}  ❌{ps.get('losers', 0)}"
                + (f"  🚨{ps.get('near_sl_count', 0)} near SL" if ps.get("near_sl_count", 0) > 0 else "")
                + (f"  🎯{ps.get('target_hit_count', 0)} target hit" if ps.get("target_hit_count", 0) > 0 else "")
            )
        for p in open_pos:
            lines += build_position_block(
                p, sig_map, eth_map, cues,
                include_gap_risk=True, brief=True,
            )
        lines.append("")

    # ── EXIT today ──
    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append("🔴 <b>EXIT TODAY</b>")
        for s in exits:
            reason      = s.get("filter_reason") or s.get("ai_conviction_reason") or ""
            cmp_ex_m    = float(s.get("current_price") or msl_map.get(s.get("symbol", ""), {}).get("current_price") or 0)
            cmp_ex_m_str = f"  CMP:₹{cmp_ex_m:,.0f}" if cmp_ex_m else ""
            lines.append(
                f"  <b>{s['symbol']}</b>{cmp_ex_m_str}"
                + (f" — {esc(reason)}" if reason else "")
            )
        lines.append("")

    # ── ACT NOW + WATCH watchlist ──
    if fp and fp.get("ranked_candidates"):
        ranked   = fp["ranked_candidates"]
        tier1    = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
        tier2    = [r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"]
        guidance = fp.get("portfolio_guidance", {})
        sizing   = guidance.get("position_sizing_override") or fp.get("_sizing", "?")

        # ── PATCH 12: Single morning guidance line ──
        _mgtext = (guidance.get("capital_deployment_narrative")
                   or guidance.get("new_positions_guidance")
                   or fp.get("_ai_note") or "")
        lines.append(
            f"💼 <b>TODAY'S APPROACH: {sizing}</b>"
            + (f"  ▲ {'/'.join(guidance['sectors_to_overweight'][:2])}"
               if guidance.get("sectors_to_overweight") else "")
        )
        if _mgtext:
            lines.append(f"  <i>{esc(_mgtext)}</i>")
        lines.append("")

        # ── PATCH 13: ACT NOW bucket live zone status + readiness ──
        if tier1:
            if _READINESS_AVAILABLE and sb:
                try:
                    tier1 = compute_entry_readiness(
                        tier1, msl_map, sb=sb, use_live=True
                    )
                except Exception as e:
                    logger.warning(f"morning readiness enrichment failed: {e}")

            lines.append(f"⭐ <b>WATCHLIST — ACT NOW ({len(tier1)} picks)</b>")
            for c in tier1:
                sym    = c.get("symbol", "?")
                action = c.get("action") or ""
                alloc  = float(c.get("suggested_allocation_pct") or 0)
                corr   = c.get("correlation_group") or ""
                msl    = msl_map.get(sym, {})
                zl     = float(msl.get("entry_zone_low")  or 0)
                zh     = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                er     = float(msl.get("expected_r")      or 2.0)
                # ── Use the PERSISTED trade plan, do not re-derive it ─────
                # This used to be:
                #     sl_p = zl * (1 - STOP_BUFFER)
                #     t1_p = zl * (1 + STOP_BUFFER * er)
                # a THIRD copy of the risk model — the same flat-percent
                # version already removed from generate_signals, where it
                # collapsed the target to roughly 40% of its intended distance.
                # compute_msl anchors the plan at 1.5xATR / 3xATR and
                # final_snapshot now carries it through, so the alert must SHOW
                # that plan rather than invent a competing one. An alert whose
                # stop disagrees with the stop the signal was validated against
                # is worse than no alert at all.
                sl_p   = _num(msl.get("planned_stop"))
                t1_p   = _num(msl.get("planned_target"))
                rr     = _num(msl.get("implied_rr"))
                if rr is None and sl_p and t1_p and zl and sl_p < zl:
                    rr = round((t1_p - zl) / (zl - sl_p), 2)
                r_icon    = c.get("readiness_icon") or entry_action_icon(action)
                r_score   = c.get("readiness_score")
                score_str = f"[{r_score}/100]" if r_score else ""
                timing    = c.get("timing_note", "Verify zone manually")

                z_status = c.get("zone_status")
                if not z_status:
                    dist = msl.get("dist_entry_pct")
                    if dist is not None:
                        d = float(dist)
                        if abs(d) <= 2:
                            z_status = f"📍 In Zone ({d:+.1f}%)"
                        elif d < 0:
                            z_status = f"⬇️ Approaching ({d:+.1f}%)"
                        else:
                            z_status = f"⬆️ Above Zone (+{d:.1f}%)"
                    else:
                        z_status = "📍 Verify zone"

                cmp_m      = float(msl.get("current_price") or 0)
                sec_rank_m = msl.get("sector_rank_at_entry")
                ind_rank_m = msl.get("industry_rank")
                ind_state_m = (msl.get("industry_state") or "").upper()
                cmp_str_m  = f" (CMP: ₹{cmp_m:,.0f})" if cmp_m else ""
                rk_m_parts = []
                if sec_rank_m: rk_m_parts.append(f"Sec #{sec_rank_m}")
                if ind_rank_m: rk_m_parts.append(f"Ind #{ind_rank_m}" + (f"·{ind_state_m}" if ind_state_m else ""))
                rk_m_str   = "  📊 " + " · ".join(rk_m_parts) if rk_m_parts else ""

                lines.append(
                    f"\n  {r_icon} <b>{sym}</b>{cmp_str_m}"
                    + (f" [{action}]" if action else "")
                    + f"  {z_status}  {score_str}"
                    + (f"  <b>{alloc:.0f}%</b>" if alloc else "")
                    + (f"  📎{corr}" if corr else "")
                )
                if zl:
                    lines.append(
                        f"   Entry₹{zl:,.0f}–₹{zh:,.0f}  <i>[DB]</i>"
                        + (f" | SL₹{sl_p:,.0f} | T1₹{t1_p:,.0f} | RR {rr}×"
                           if (sl_p and t1_p) else "")
                        + f" · <i>{timing}</i>"
                    )
                else:
                    lines.append(
                        "   ⚠️ <i>Zone data unavailable</i>"
                        + (f" · <i>{timing}</i>" if timing else "")
                    )

                # PATCH 19: AI-approved chase alternative — only set by the
                # module while zone_status is AI-EXTENDED (live price above
                # zone_high but within ai_decision_engine's approved ceiling).
                gtt_entry_c = c.get("gtt_entry_chase")
                if gtt_entry_c:
                    gtt_sl_c = c.get("gtt_sl_chase")
                    gtt_t1_c = c.get("gtt_t1_chase")
                    rr_c     = c.get("rr_ratio_chase")
                    lines.append(
                        f"   🟡 Chase alt: Entry₹{gtt_entry_c:,.0f}"
                        + (f" | SL₹{gtt_sl_c:,.0f}" if gtt_sl_c else "")
                        + (f" | T1₹{gtt_t1_c:,.0f}" if gtt_t1_c else "")
                        + (f" | RR {rr_c}×" if rr_c else "")
                        + "  <i>· reduced size, AI-approved</i>"
                    )

                if rk_m_str:
                    lines.append(rk_m_str)
                if c.get("entry_note"):
                    lines.append(f"   📍 {esc(c['entry_note'])} <i>[AI]</i>")
                if c.get("invalidation"):
                    lines.append(f"   ❌ {esc(c['invalidation'])} <i>[AI]</i>")

                evt_warn = _format_event_warning(msl)
                if evt_warn:
                    lines.append(evt_warn)

            lines.append("")

        # ── PATCH 14: WATCH bucket — 2-line compact ──
        if tier2:
            lines.append(f"🔭 <b>INTRADAY TRIGGERS — WATCH ({len(tier2)})</b>")
            for c in tier2:
                sym   = c.get("symbol", "?")
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                dist  = msl.get("dist_entry_pct")
                dist_str = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""
                cmp_m2 = float(msl.get("current_price") or 0)
                cmp_str_m2 = f" (CMP: ₹{cmp_m2:,.0f})" if cmp_m2 else ""

                lines.append(
                    f"  {entry_action_icon('WAIT_FOR_TRIGGER')} <b>{sym}</b>{cmp_str_m2}"
                    f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}"
                )
                if c.get("entry_note"):
                    lines.append(f"    📍 {esc(c['entry_note'])}")

                evt_warn = _format_event_warning(msl)
                if evt_warn:
                    lines.append(evt_warn)

            lines.append("")

    # ── Events in next 3 days ──
    near_events = [
        e for e in upcoming
        if str(e.get("event_date", ""))[:10] != today_str
        and (e.get("days_to_event") or 99) <= 3
    ]
    if near_events:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>EVENTS IN NEXT 3 DAYS</b>")
        for e in near_events[:6]:
            tag     = "📂" if e.get("symbol") in pos_sym_set else "🔭"
            d_to    = e.get("days_to_event", "?")
            purpose = e.get("purpose", "")
            lines.append(
                f"  {tag} <b>{e.get('symbol', '?')}</b>: {purpose} ({d_to}d)"
            )

    return "\n".join(lines)


# ── Afternoon zone check ───────────────────────────────────────────────────

def build_afternoon(data: dict, sb=None) -> str:
    from datetime import datetime as _dt
    fp       = data.get("final_picks")
    msl_map  = data["msl_map"]
    open_pos = data.get("open_pos", [])
    cues     = data.get("cues", {})
    now_str  = _dt.now().strftime("%I:%M %p")
    date_str = data["signal_date"]

    lines = [
        f"<b>📊 AFTERNOON REVIEW — {date_str}  |  TradeOS v7  |  {now_str}</b>",
        "═" * 35,
        "",
    ]

    if not fp or not fp.get("ranked_candidates"):
        lines.append("<i>⚠️ No ranked candidates — step 19 data missing</i>")
        return "\n".join(lines)

    ranked = fp["ranked_candidates"]
    tier1  = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
    tier2  = [r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"]

    if not tier1 and not tier2:
        lines.append("<i>No picks today</i>")
        return "\n".join(lines)

    if _READINESS_AVAILABLE and sb:
        try:
            tier1 = compute_entry_readiness(
                tier1, msl_map, sb=sb, use_live=True
            )
        except Exception as e:
            logger.warning(f"afternoon readiness enrichment failed: {e}")

    # ── Open position SL breach warning (context) ──────────────────────
    gap_pct = float(cues.get("gift_nifty_chg_pct") or 0)
    if gap_pct != 0 and open_pos:
        breach = [
            p["symbol"] for p in open_pos
            if float(p.get("current_price") or 0) > 0
            and float(p.get("active_sl") or 0) > 0
            and float(p["current_price"]) * (1 + gap_pct / 100)
               <= float(p["active_sl"])
        ]
        if breach:
            lines.append(f"🚨 <b>OPEN POSITION SL RISK: {', '.join(breach)}</b>")
            lines.append("")

    # ════════════════════════════════════════════════════════════════════
    # ACT NOW — full detail block
    # Each stock gets: CMP · zone status · SL · T1 · RR · alloc ·
    #                  thesis · entry note · timing · invalidation
    # ════════════════════════════════════════════════════════════════════
    any_actionable = False

    if tier1:
        lines.append(f"⭐ <b>ACT NOW ({len(tier1)})</b>")

        for c in tier1:
            sym    = c.get("symbol", "?")
            action = c.get("action") or ""
            alloc  = float(c.get("suggested_allocation_pct") or 0)

            msl = msl_map.get(sym, {})
            zl  = float(msl.get("entry_zone_low")  or 0)
            zh  = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))

            # ── What compute_entry_readiness actually sets ──────────────
            # zone_status  → "✅ IN ZONE (₹1,520)" / "⬇️ APPROACHING (₹X, Y% below zone)"
            #                "⚠️ ABOVE ZONE (₹X, +Y%)" / "🟡 AI-EXTENDED (₹X, +Y% — chase OK to +Z%)"
            #                "❌ MISSED (₹X, +Y% above zone)"
            # timing_note  → plain-English action ("1:30–2:30 PM · limit at zone_low",
            #                or the AI's chase_rationale when AI-EXTENDED)
            # live_vol_note→ "1.6× pace vs 20d avg (live)"  — None if unavailable
            # gtt_entry/sl/t1/rr_ratio → original zone_low-anchored GTT (PATCH 19: unchanged)
            # gtt_entry_chase/sl_chase/t1_chase/rr_ratio_chase → PATCH 19 NEW: only set
            #   while zone_status is AI-EXTENDED — entry re-anchored to live price since
            #   zone_low is already blown through; use these for the chase alternative.
            zone_status   = c.get("zone_status", "")
            timing_note   = c.get("timing_note", "")
            live_vol_note = c.get("live_vol_note")
            gtt_entry     = c.get("gtt_entry")
            gtt_sl        = c.get("gtt_sl")
            gtt_t1        = c.get("gtt_t1")
            rr            = c.get("rr_ratio")

            # CMP: live price is embedded in zone_status string, e.g., "✅ IN ZONE (₹1,520)"
            # Extract it with regex; fall back to EOD current_price from msl_map.
            # readiness_score / readiness_icon / readiness_breakdown are NOT set by this module.
            _m  = re.search(r"₹([\d,]+)", zone_status)
            cp  = float(_m.group(1).replace(",", "")) if _m else float(msl.get("current_price") or 0)
            cmp_str_aft = f"  CMP:₹{cp:,.0f}" if cp else ""

            # any_actionable flag — drive from zone_status string, not CMP arithmetic
            # PATCH 19: AI-EXTENDED counts too — it IS an approved entry, just via
            # chase instead of the disciplined zone; leaving it out here would put
            # exactly the picks this fix targets back into "no picks in zone".
            if "IN ZONE" in zone_status.upper() or "AI-EXTENDED" in zone_status.upper():
                any_actionable = True

            # ── Line 1: Header ──────────────────────────────────────────
            lines.append(
                f"\n{entry_action_icon(action)} <b>{sym}</b>{cmp_str_aft}"
                + (f" [{alloc:.0f}% alloc]" if alloc else "")
            )

            # ── Line 2: Live zone status (module string already has price + distance) ──
            if zone_status:
                lines.append(f"  {zone_status}  |  Zone: ₹{zl:,.0f}–₹{zh:,.0f}")
            else:
                lines.append(
                    f"  Zone: ₹{zl:,.0f}–₹{zh:,.0f}"
                    f"  <i>(live data unavailable — check manually)</i>"
                )

            # ── Line 3: GTT levels from module + live vol ───────────────
            # Use gtt_sl/gtt_t1/rr_ratio directly — already computed by module.
            # Fallback to manual calc only if module didn't run (no sb / no yfinance).
            if gtt_entry and gtt_sl and gtt_t1:
                gtt_line = (
                    f"  Entry:₹{gtt_entry:,.0f}"
                    f" | SL:₹{gtt_sl:,.0f}"
                    f" | T1:₹{gtt_t1:,.0f}"
                    + (f" | RR <b>{rr}×</b>" if rr else "")
                )
                if live_vol_note:
                    gtt_line += f"   📊 {live_vol_note}"
                lines.append(gtt_line)
            elif zl:
                # Fallback: module didn't run — compute manually
                er    = float(msl.get("expected_r") or 2.0)
                f_sl  = round(zl * (1 - STOP_BUFFER), 0)
                f_t1  = round(zl * (1 + STOP_BUFFER * er), 0)
                f_rr  = round((f_t1 - zl) / (zl - f_sl), 1) if f_sl < zl else None
                lines.append(
                    f"  Entry:₹{zl:,.0f} | SL:₹{f_sl:,.0f} | T1:₹{f_t1:,.0f}"
                    + (f" | RR <b>{f_rr}×</b>" if f_rr else "")
                    + "  <i>[EOD calc — live data unavailable]</i>"
                )

            # ── Line 3b: AI-approved chase alternative (PATCH 19) ────────
            # Only set by the module while zone_status is AI-EXTENDED — price
            # is above zone_high but within ai_decision_engine's approved
            # ceiling for this symbol today. zone_low is already blown
            # through, so this is entry re-anchored to the live price, not
            # a second read of the same zone.
            gtt_entry_c = c.get("gtt_entry_chase")
            if gtt_entry_c:
                gtt_sl_c = c.get("gtt_sl_chase")
                gtt_t1_c = c.get("gtt_t1_chase")
                rr_c     = c.get("rr_ratio_chase")
                lines.append(
                    f"  🟡 Chase alt: Entry:₹{gtt_entry_c:,.0f}"
                    + (f" | SL:₹{gtt_sl_c:,.0f}" if gtt_sl_c else "")
                    + (f" | T1:₹{gtt_t1_c:,.0f}" if gtt_t1_c else "")
                    + (f" | RR <b>{rr_c}×</b>" if rr_c else "")
                    + "  <i>· reduced size, AI-approved</i>"
                )

            # ── Line 4: Timing / action directive from module ───────────
            # timing_note is authored by the module for morning / afternoon context:
            # "1:30–2:30 PM · limit at zone_low" / "Skip today · re-evaluate tomorrow"
            if timing_note:
                lines.append(f"  🕐 {esc(timing_note)}")

            # ── Line 5: Thesis — the core "why" ────────────────────────
            thesis = c.get("thesis") or c.get("ai_conviction_reason") or ""
            if "] " in thesis:
                thesis = thesis.split("] ", 1)[-1]
            thesis = thesis.split("| Entry:")[0].strip()
            if thesis:
                lines.append(f"  💬 {esc(thesis[:120])}")

            # ── Line 6: Entry note (all stocks) ────────────────────────
            if c.get("entry_note"):
                lines.append(f"  📍 {esc(c['entry_note'])} <i>[AI]</i>")

            # ── Line 7: Invalidation (all stocks) ──────────────────────
            if c.get("invalidation"):
                lines.append(f"  ❌ Skip if: {esc(c['invalidation'][:90])}")

            # ── Line 8: Key risks ───────────────────────────────────────
            if c.get("risks"):
                risk_str = " · ".join(esc(str(r)) for r in (c["risks"] or [])[:2])
                if risk_str:
                    lines.append(f"  ⚠️ {risk_str}")

        lines.append("")

        if not any_actionable:
            lines.append(
                "<i>⏳ No ACT NOW picks in zone right now"
                " — monitor for 1:30–2:30 PM entry window</i>"
            )
            lines.append("")

    # ════════════════════════════════════════════════════════════════════
    # WATCH — compact trigger watch
    # compute_entry_readiness only runs on ACT NOW, so WATCH has no live data.
    # Show EOD price + distance from msl_map; entry_note for trigger condition.
    # ════════════════════════════════════════════════════════════════════
    if tier2:
        lines.append(f"🔭 <b>TRIGGER WATCH ({len(tier2)})</b>")
        lines.append(f"  <i>Zone ranges + EOD price — no live enrichment for the watch bucket</i>")
        for c in tier2:
            sym  = c.get("symbol", "?")
            msl  = msl_map.get(sym, {})
            zl   = float(msl.get("entry_zone_low")  or 0)
            zh   = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
            cp   = float(msl.get("current_price") or 0)   # EOD only — no live for T2
            dist = msl.get("dist_entry_pct")
            dist_str     = f" ({abs(float(dist)):.1f}%↓)" if dist is not None else ""
            cmp_str_t2aft = f" (CMP: ₹{cp:,.0f})" if cp > 0 else ""

            lines.append(
                f"\n  {entry_action_icon('WAIT_FOR_TRIGGER')} <b>{sym}</b>{cmp_str_t2aft}"
                f"  Zone:₹{zl:,.0f}–₹{zh:,.0f}{dist_str}"
            )
            if c.get("entry_note"):
                lines.append(f"    📍 {esc(c['entry_note'])}")

        lines.append("")

    return "\n".join(lines)


# ── Compact (mobile one-screen) ────────────────────────────────────────────

def build_compact(data: dict) -> str:
    fp       = data.get("final_picks")
    regime   = data["regime"]
    fii      = data["fii"]
    open_pos = data["open_pos"]
    signals  = data["signals"]
    date_str = data["signal_date"]
    ps       = data.get("portfolio_summary", {})
    cues     = data["cues"]

    r_label  = regime.get("predicted_regime") or regime.get("regime", "?")
    nifty    = regime.get("nifty_price")
    fii_flag = fii.get("fii_flag", "?")
    gift_chg = cues.get("gift_nifty_chg_pct")

    lines = [
        f"<b>📊 {date_str}</b>  {regime_icon(r_label)}{r_label}  "
        f"Nifty:{fmt_price(nifty)}  {fii_icon(fii_flag)}FII:{fii_flag}"
        + (f"  Gift:{fmt_chg(gift_chg)}" if gift_chg else ""),
        "",
    ]

    if ps:
        lines.append(
            f"💼 P&L: <b>{fmt_pct(ps.get('pnl_pct_overall'))}</b>  "
            f"✅{ps.get('winners', 0)} ❌{ps.get('losers', 0)}"
            + (f"  🚨{ps.get('near_sl_count', 0)} SL" if ps.get("near_sl_count", 0) > 0 else "")
            + (f"  🎯{ps.get('target_hit_count', 0)}" if ps.get("target_hit_count", 0) > 0 else "")
        )

    if fp and fp.get("ranked_candidates"):
        ranked   = fp["ranked_candidates"]
        tier1    = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
        tier2    = [r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"]
        guidance = fp.get("portfolio_guidance", {})
        sizing   = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        lines.append(f"💼 {sizing}")
        if tier1:
            lines.append("\n⭐ <b>ACT NOW</b>")
            for c in tier1:
                lines.append(
                    f"  {entry_action_icon(c.get('action'))} <b>{c['symbol']}</b> "
                    f"[{c.get('action', '?')}] {float(c.get('suggested_allocation_pct', 0)):.0f}%"
                )
        if tier2:
            t2str = " · ".join(c["symbol"] for c in tier2[:5])
            lines.append(f"\n🔭 <b>WATCH:</b> {t2str}")
    else:
        buys = sorted(
            [s for s in signals if s.get("signal_type") in ENTRY_TYPES],
            key=lambda x: float(x.get("score_adjusted") or x.get("score") or 0),
            reverse=True,
        )
        if buys:
            lines.append(f"🎯 <b>BUYS ({len(buys)})</b>")
            for s in buys[:5]:
                lines.append(
                    f"  • <b>{s['symbol']}</b> "
                    f"{float(s.get('score_adjusted') or s.get('score') or 0):.0f}pt"
                )

    if open_pos:
        lines.append(f"\n📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl   = float(p.get("pnl_pct") or 0)
            cp    = float(p.get("current_price") or 0)
            sl    = float(p.get("active_sl") or 0)
            ico   = "📈" if pnl >= 0 else "📉"
            prox  = sl_proximity_pct(cp, sl)
            sl_w  = " 🚨" if prox is not None and prox <= 3.0 else ""
            t_hit = " 🎯" if p.get("target_hit") else ""
            act   = p.get("action_required") or ""
            act_str = f"  [{act}]" if act and act != "HOLD" else ""
            lines.append(
                f"  {ico}{sl_w}{t_hit} <b>{p['symbol']}</b> "
                f"{fmt_pct(pnl)} {days_held(p.get('entry_date'))}{act_str}"
            )

    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append("\n🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))

    return "\n".join(lines)


# ── Signal keyboard (Telegram-specific — unchanged) ────────────────────────

def send_signal_with_keyboard(signal: dict) -> bool:
    """
    Sends an individual signal alert with inline Telegram confirmation keyboard.
    Telegram-specific — only call when channel includes 'telegram'.
    """
    try:
        # Was brain.position_manager, which was deleted — it wrote columns that
        # did not exist, used status='OPEN' while every reader filters 'ACTIVE',
        # and its buttons had no receiver. control.telegram_position_journal is
        # the working replacement: getUpdates polling, no webhook required.
        from control.telegram_position_journal import build_signal_keyboard, _tg_api
    except ImportError:
        logger.debug("telegram_position_journal not available — skipping keyboard send")
        return False

    try:
        sym   = signal.get("symbol", "?")
        stype = signal.get("signal_type", "")
        price = float(signal.get("current_price") or signal.get("kite_price") or 0)
        score = float(signal.get("score_adjusted") or signal.get("score") or 0)

        text = (
            f"<b>{stype}: {sym}</b>\n"
            f"Price: ₹{price:.2f}  Score: {score:.0f}\n"
            f"Tap below to confirm entry or skip."
        )
        keyboard = build_signal_keyboard(
            signal_id=signal["id"],
            symbol=sym,
            signal_price=price,
        )
        _tg_api("sendMessage", {
            "chat_id":      TELEGRAM_CHAT_ID,
            "text":         text,
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(keyboard),
        })
        return True
    except Exception as e:
        logger.warning(f"send_signal_with_keyboard failed for {signal.get('symbol')}: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────

_KNOWN_FLAGS = {
    "--morning", "--afternoon", "--evening", "--position-risk",
    "--dry-run", "--dry", "--test",
}


def main():
    logger.info(f"send_alerts v6 | argv={sys.argv}")

    # --dry-run was accepted silently and did NOTHING. The env var DRY_RUN is
    # honoured by send_message, but nothing mapped the flag to it, so a command
    # that read as a rehearsal sent real Telegram, Discord and email messages.
    # A safety flag that silently does nothing is worse than no flag at all,
    # because it is used precisely when the sender does not want to send.
    global DRY_RUN
    if {"--dry-run", "--dry", "--test"} & set(sys.argv):
        DRY_RUN = True
        logger.warning("DRY RUN — messages will be previewed to the log, not sent")

    # An unrecognised flag is not harmless here: `--dryrun` or `--dry_run` would
    # have fallen through to a live send. Refuse rather than guess.
    #
    # But only judge OUR OWN flags. run_pipeline invokes this in-process and
    # leaves its own argv in place, so `run_pipeline.py --step alerts` reached
    # here as an unknown `--step` and refused to send — the step failed for the
    # one invocation style used to re-run it after fixing something upstream.
    # The pipeline's flags are known and are not ours to validate.
    _PIPELINE_FLAGS = {"--step", "--dry-run"}
    argv = list(sys.argv[1:])
    if argv and "run_pipeline" in (sys.argv[0] or ""):
        argv = [a for i, a in enumerate(argv)
                if a not in _PIPELINE_FLAGS
                and not (i > 0 and argv[i - 1] == "--step")]
    unknown = [a for a in argv if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        raise SystemExit(
            f"\n  Unrecognised flag(s): {', '.join(unknown)}\n"
            f"  Known: {', '.join(sorted(_KNOWN_FLAGS))}\n"
            f"  Refusing to run — an unknown flag on this command sends real alerts.\n"
        )

    if is_kill_switch_active():
        logger.warning("Kill switch — send_alerts skipped")
        return {"status": "skipped"}

    # ── PATCH 18: Load channel config from system_config before anything else ──
    # Done early so _ch() is primed for all subsequent reads, including the
    # alerts_enabled check below.  'telegram_alerts_enabled' is kept as the
    # gate key for backward-compat; rename it to 'alerts_enabled' in system_config
    # if you want a channel-agnostic name going forward.
    sb = get_supabase()
    channel_cfg = load_channel_config(sb)
    _CHANNEL_CFG.update(channel_cfg)

    active_channel = _ch("alert_channel", "telegram")

    # ── Alerts gate (backward-compat: still reads telegram_alerts_enabled) ──
    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info(f"Alerts disabled (telegram_alerts_enabled=false) | channel={active_channel}")
        return {}

    if "--position-risk" in sys.argv:
        return {"status": "skipped", "reason": "covered by sl_monitor"}

    is_morning   = "--morning"   in sys.argv
    is_afternoon = "--afternoon" in sys.argv
    today = str(today_ist())
    data  = load_data(sb, today)

    has_fp       = bool(data.get("final_picks"))
    has_guidance = bool((data.get("final_picks") or {}).get("portfolio_guidance"))
    logger.info(
        f"Data loaded: step19={'✅' if has_fp else '❌ fallback'} "
        f"| guidance={'✅' if has_guidance else '❌ missing'} "
        f"| {len(data['signals'])} signals | {len(data['open_pos'])} positions "
        f"| readiness={'✅' if _READINESS_AVAILABLE else '❌ unavailable'} "
        f"| channel={active_channel}"          # ← PATCH 18: channel in startup log
    )

    # ── Build message + set subject suffix ────────────────────────────────
    # PATCH 18: subject_suffix passed so email subjects read naturally:
    #   "TradeOS Evening" / "TradeOS Morning" / "TradeOS Afternoon"
    if is_morning:
        msg            = build_morning(data, sb=sb)
        mode           = "morning"
        subject_suffix = "Morning"
    elif is_afternoon:
        msg            = build_afternoon(data, sb=sb)
        mode           = "afternoon"
        subject_suffix = "Afternoon"
    elif MESSAGE_STYLE == "compact":
        msg            = build_compact(data)
        mode           = "compact"
        subject_suffix = "Compact"
    else:
        msg            = build_evening(data, sb=sb)
        mode           = "structured"
        subject_suffix = "Evening"

    success = send_message(msg, subject_suffix=subject_suffix)

    # Signal keyboards (Telegram-only feature)
    if cfg_bool("telegram_signal_keyboards_enabled", False):
        if active_channel in ("telegram", "all"):
            entry_signals = [
                s for s in data["signals"]
                if s.get("signal_type") in ENTRY_TYPES and s.get("id")
            ]
            for sig in entry_signals:
                send_signal_with_keyboard(sig)
        else:
            logger.debug(
                "telegram_signal_keyboards_enabled=true but channel is "
                f"'{active_channel}' — skipping keyboard sends"
            )

    fp     = data.get("final_picks") or {}
    ranked = fp.get("ranked_candidates") or []
    tier1  = len([r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")])
    tier2  = len([r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"])
    exits  = len([s for s in data["signals"] if s.get("signal_type") == "EXIT"])

    if success:
        logger.success(
            f"Alert ({mode} via {active_channel}) sent: "
            f"T1:{tier1} T2:{tier2} exit:{exits} "
            f"pos:{len(data['open_pos'])} "
            f"| step19:{'ok' if has_fp else 'fallback'} "
            f"| guidance:{'ok' if has_guidance else 'missing'}"
        )

    # ── Write structured daily summary ──
    try:
        _tier1   = len([r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")])
        _tier2   = len([r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"])
        _tier3   = len([r for r in ranked if r.get("action") == "SKIP"])
        _buy_raw  = sum(1 for s in data["signals"] if s.get("signal_type") in ENTRY_TYPES)
        _watch    = sum(1 for s in data["signals"] if s.get("signal_type") == "WATCH")
        _exit_raw = sum(1 for s in data["signals"] if s.get("signal_type") == "EXIT")

        _zero_reason = None
        if _buy_raw == 0:
            _zero_reason = "NO_RAW_SIGNALS"
        elif not has_fp:
            _zero_reason = "STEP19_MISSING"
        elif _tier1 == 0 and _tier2 == 0:
            _zero_reason = "AI_PASSED_ALL_TO_MONITOR"

        sb.table("signal_daily_summary").upsert({
            "date":           today,
            "buy_raw":        _buy_raw,
            "watch_raw":      _watch,
            "exit_raw":       _exit_raw,
            "tier1":          _tier1,
            "tier2":          _tier2,
            "tier3":          _tier3,
            "open_positions": len(data["open_pos"]),
            "step19_ok":      has_fp,
            "guidance_ok":    has_guidance,
            "zero_reason":    _zero_reason,
            "sizing":         (data.get("final_picks") or {}).get("_sizing"),
            "ai_note":        ((data.get("final_picks") or {}).get("_ai_note") or ""),
            # PATCH 18: record active channel for observability
            # Requires: ALTER TABLE signal_daily_summary ADD COLUMN IF NOT EXISTS
            #           alert_channel text;
            # "alert_channel": active_channel,
        }).execute()
        logger.info(f"signal_daily_summary written: T1:{_tier1} T2:{_tier2} raw:{_buy_raw}")
    except Exception as e:
        logger.warning(f"signal_daily_summary write failed (non-fatal): {e}")

    return {
        "sent":          success,
        "mode":          mode,
        "channel":       active_channel,   # PATCH 18: included in return dict
        "tier1":         tier1,
        "tier2":         tier2,
        "step19":        has_fp,
        "guidance":      has_guidance,
    }


if __name__ == "__main__":
    main()