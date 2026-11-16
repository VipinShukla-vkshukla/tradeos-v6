"""
TradeOS v7 — Intraday Daemon Log Formatter
============================================
Produces structured, traffic-light summaries for the intraday daemon that answer:
  1. Did the daemon start healthy and stay healthy?
  2. What entries were taken, blocked, or deferred?
  3. What exits were executed, trailed, or held?
  4. Where are the gaps and loopholes?
  5. What improvement areas should be investigated?

Designed as a drop-in enhancement for intraday/run.py and intraday/engine.py.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from loguru import logger

# ── Step/group emojis ─────────────────────────────────────────────────────────

GROUP_EMOJI = {
    "SESSION": "📊",
    "FEED": "🔌",
    "REGIME": "🎯",
    "ENTRY": "🟢",
    "EXIT": "🔴",
    "ALLOCATOR": "⚖️",
    "SAME_DAY": "🆕",
    "REQUALIFY": "🔄",
    "CAPITAL": "💰",
    "GAPS": "⚠️",
    "HEARTBEAT": "💓",
}

# ── Thresholds ────────────────────────────────────────────────────────────────

VIX_CAUTION = 20.0
VIX_DANGER = 25.0


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    rem = s % 60
    return f"{m}m {rem:.0f}s"


def _status_emoji(ok: bool) -> str:
    return "✅" if ok else "❌"


# ─────────────────────────────────────────────────────────────────────────────
# SESSION START BANNER
# ─────────────────────────────────────────────────────────────────────────────

def format_session_start(
    phase: float,
    gtt: bool,
    orders: bool,
    dry_run: bool,
    regime: str | None = None,
    vix: float | None = None,
    fii_flag: str | None = None,
    entries_blocked: bool = False,
    entries_restricted: bool = False,
    sizing_override: str | None = None,
) -> str:
    """
    Build the intraday daemon start-of-session banner.
    """
    tl = "🟢"
    reasons: List[str] = []

    if not orders:
        tl = "🔴"
        reasons.append("ORDERS_DISABLED")
    if dry_run:
        tl = "🟡"
        reasons.append("DRY_RUN")
    if entries_blocked:
        tl = "🔴"
        reasons.append("ENTRIES_BLOCKED")
    elif entries_restricted:
        tl = "🟡"
        reasons.append("ENTRIES_RESTRICTED")

    lines: List[str] = []
    lines.append("")
    lines.append("═" * 72)
    lines.append(f"  TRADEOS v7 — INTRADAY DAEMON | {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    lines.append("═" * 72)
    lines.append("")

    # ── Health ──────────────────────────────────────────────────────────────
    lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
    lines.append("│ 📊 DAEMON HEALTH                                                        │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append(f"│  {tl} Phase: {phase}   GTT: {'ON' if gtt else 'off'}   Orders: {'ON' if orders else 'off'}   DRY_RUN: {dry_run}   │")
    regime_str = regime or "unknown"
    vix_str = f"{vix:.1f}" if vix is not None else "n/a"
    fii_str = fii_flag or "n/a"
    lines.append(f"│     Regime: {regime_str:<12} VIX: {vix_str:>6}   FII: {fii_str:<10}   │")
    if entries_blocked:
        lines.append("│     ⛔ SWING ENTRIES BLOCKED — pre-market gate refused new entries     │")
    elif entries_restricted:
        lines.append(f"│     ⚠️  SWING ENTRIES RESTRICTED — {sizing_override or 'HALF'} sizing, max 1 entry   │")
    else:
        lines.append("│     ✅ SWING ENTRIES ALLOWED                                            │")
    lines.append("│                                                                         │")
    lines.append("└─────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PER-CYCLE SUMMARY (throttled, e.g., every 5 minutes or on change)
# ─────────────────────────────────────────────────────────────────────────────

def format_cycle_summary(
    cycle_count: int,
    positions_held: int,
    candidates_watched: int,
    buyable: int,
    entries_taken: int,
    entry_cap: int,
    exits_executed: int,
    stops_trailed: int,
    regime: str,
    vix: float,
    block_reason: str | None = None,
    restricted: bool = False,
    same_day_discoveries: int = 0,
    live_requalify_admitted: int = 0,
    gaps: List[str] | None = None,
) -> str:
    """
    Build a per-cycle summary line block.
    """
    tl = "🟢"
    if block_reason:
        tl = "🔴"
    elif restricted:
        tl = "🟡"

    lines: List[str] = []
    lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
    lines.append(f"│ 🔄 CYCLE {cycle_count:>4} | {tl} Regime: {regime:<10} VIX: {vix:>5.1f}   │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")

    # Entry funnel
    entries_left = max(0, entry_cap - entries_taken)
    lines.append(
        f"│ 🟢 ENTRIES: {entries_taken}/{entry_cap} used ({entries_left} left)  "
        f"{buyable} buyable of {candidates_watched} watched   │"
    )

    # Exit funnel
    lines.append(
        f"│ 🔴 EXITS:  {exits_executed} executed  {stops_trailed} stops trailed   │"
    )

    # Same-day & requalify
    if same_day_discoveries or live_requalify_admitted:
        lines.append(
            f"│ 🆕 SAME-DAY: {same_day_discoveries} discovered  "
            f"🔄 REQUALIFY: {live_requalify_admitted} admitted   │"
        )

    if block_reason:
        lines.append(f"│ ⛔ BLOCKED: {block_reason}                                             │")
    elif restricted:
        lines.append("│ ⚠️  RESTRICTED: VIX > 25 — structural-edge only, min_rr=2.0              │")

    lines.append("└─────────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION END SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def format_session_end(
    total_cycles: int,
    total_duration: float,
    entries_taken: int,
    entry_cap: int,
    exits_executed: int,
    stops_trailed: int,
    positions_at_close: int,
    unfilled_entries: int,
    unscored_outcomes: int,
    gaps: List[str],
    improvements: List[str],
) -> str:
    """
    Build the end-of-session summary banner.
    """
    tl = "🟢"
    if unscored_outcomes > 0 or gaps:
        tl = "🔴"
    elif unfilled_entries > 0:
        tl = "🟡"

    lines: List[str] = []
    lines.append("")
    lines.append("═" * 72)
    lines.append(f"  TRADEOS v7 — INTRADAY DAEMON — SESSION END | {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    lines.append("═" * 72)
    lines.append("")

    # ── Session health ──────────────────────────────────────────────────────
    lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
    lines.append("│ 📊 SESSION HEALTH                                                       │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append(
        f"│  {tl} Cycles: {total_cycles:>5}   ⏱ {_fmt_secs(total_duration)}   "
        f"Positions at close: {positions_at_close}   │"
    )
    lines.append("│                                                                         │")

    # ── Entry/exit funnel ──────────────────────────────────────────────────
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append("│ 📈 ENTRY / EXIT FUNNEL                                                  │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append(f"│  Entries taken:  {entries_taken}/{entry_cap}   "
                 f"{'✅' if entries_taken <= entry_cap else '❌'}   │")
    lines.append(f"│  Exits executed: {exits_executed}   Stops trailed: {stops_trailed}   │")
    lines.append(f"│  Unfilled entries: {unfilled_entries}   "
                 f"{'⚠️' if unfilled_entries > 0 else '✅'}   │")
    lines.append("│                                                                         │")

    # Zero entries today — only at session end, not every cycle.
    if entries_taken == 0 and entry_cap > 0:
        lines.append("│                                                                         │")
        lines.append("│ 💡 Zero entries today — review entry criteria, regime gate, or          │")
        lines.append("│    max_new_per_day. See refusal histogram below for why.                │")
        lines.append("│                                                                         │")

    # ── Gaps & loopholes ───────────────────────────────────────────────────
    if gaps:
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ ⚠️  GAPS & LOOPHOLES (investigate these)                               │")
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        for gap in gaps[:8]:  # cap display
            lines.append(f"│  • {gap:<69}│")
        lines.append("│                                                                         │")

    # ── Improvement areas ──────────────────────────────────────────────────
    if improvements:
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ 💡 IMPROVEMENT AREAS                                                    │")
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        for imp in improvements[:8]:
            lines.append(f"│  • {imp:<69}│")
        lines.append("│                                                                         │")

    # ── Refusal histogram (why entries were refused) ─────────────────────
    hist = refusal_histogram()
    if hist:
        total_refusals = sum(c for _, c in hist)
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ 🔍 REFUSAL HISTOGRAM (why entries were refused)                       │")
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        lines.append(f"│  Total refusals: {total_refusals}                                               │")
        for reason, count in hist[:8]:
            pct = (count / total_refusals * 100) if total_refusals else 0
            lines.append(f"│  • {reason:<25}: {count:>4} ({pct:.0f}%)                                      │")
        lines.append("│                                                                         │")

    lines.append("└─────────────────────────────────────────────────────────────────────────┘")
    lines.append("")
    lines.append(f"  Total: {_fmt_secs(total_duration)} | {tl} Session complete")
    lines.append("═" * 72)
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# GAP DETECTION — IMMEDIATE (during daemon loop)
# ─────────────────────────────────────────────────────────────────────────────

# Module-level dedup state so immediate alerts don't spam every cycle.
# A gap is logged once when it first appears, and again only if it clears
# and reappears, or every IMMEDIATE_GAP_REPEAT_CYCLES cycles while active.
_IMMEDIATE_GAP_REPEAT_CYCLES = 60  # ~15 minutes at 15s eval interval
_immediate_gap_state: Dict[str, dict] = {}

# Per-symbol refusal dedup — same symbol/refusal reason logs once per
# REFUSAL_REPEAT_CYCLES instead of every cycle. First occurrence always logs.
_REFUSAL_REPEAT_CYCLES = 240  # ~1 hour at 15s eval interval
_refusal_state: Dict[str, int] = {}
# Refusal reason histogram — counted for the session-end summary.
_refusal_histogram: Dict[str, int] = {}


def log_refusal(symbol: str, reason_key: str) -> bool:
    """
    True when a refusal should be logged now, False when suppressed.

    Same (symbol, reason_key) pair logs once on first occurrence, then again
    only after REFUSAL_REPEAT_CYCLES cycles. This keeps the log readable
    while still surfacing the refusal reason periodically.

    The cycle counter is set by log_immediate_gaps() each cycle via
    _refusal_cycle_count[0]; when called outside that context the counter
    reads 0 and dedup still works (just less precisely).
    """
    key = f"{symbol}|{reason_key}"
    now = _refusal_cycle_count[0]
    last = _refusal_state.get(key)
    if last is None or now - last >= _REFUSAL_REPEAT_CYCLES:
        _refusal_state[key] = now
        return True
    return False


# Module-level cycle counter, set by log_immediate_gaps() each cycle.
_refusal_cycle_count: list[int] = [0]


def record_refusal(reason_key: str) -> None:
    """
    Count a refusal reason for the session-end histogram.

    Call this at every refusal site (liquidity block, edge floor, AI avoid,
    regime gate, etc.) so the end-of-session banner shows the breakdown.
    """
    _refusal_histogram[reason_key] = _refusal_histogram.get(reason_key, 0) + 1


def refusal_histogram() -> list[tuple[str, int]]:
    """Return refusal reasons sorted by count, descending."""
    return sorted(_refusal_histogram.items(), key=lambda x: -x[1])


# Cross-day state for tracking rejections and entry cap trends.
# Persisted in system_config so it survives daemon restarts.
_CROSS_DAY_KEYS = {
    "gap_setup_rejection_count": "gap_setup_rejection_count",
    "gap_entry_cap_days": "gap_entry_cap_days",
    "gap_allocator_decline_days": "gap_allocator_decline_days",
    "gap_slippage_exceed_days": "gap_slippage_exceed_days",
}

# Slippage threshold (bps) above which we flag a gap
SLIPPAGE_THRESHOLD_BPS = 30  # 30 bps = 0.30%

# Allocator decline threshold (% of candidates declined)
ALLOCATOR_DECLINE_THRESHOLD_PCT = 80  # 80% declined = gap


# ── Cross-day state persistence ──────────────────────────────────────────

def _get_cross_day_state(sb, key: str) -> int:
    """Read an integer counter from system_config (persists across daemon restarts)."""
    try:
        row = sb.table("system_config").select("value").eq("key", key).execute().data
        if row and row[0].get("value"):
            return int(row[0]["value"])
    except Exception:
        pass
    return 0


def _set_cross_day_state(sb, key: str, value: int) -> None:
    """Persist an integer counter to system_config."""
    try:
        existing = sb.table("system_config").select("key").eq("key", key).execute().data
        payload = {"key": key, "value": str(value),
                   "description": f"Cross-day tracking — {key}"}
        if existing:
            sb.table("system_config").update({"value": str(value)}).eq("key", key).execute()
        else:
            sb.table("system_config").insert(payload).execute()
    except Exception:
        pass


# ── Immediate gap detection (enhanced) ─────────────────────────────────────

def detect_immediate_gaps(
    cycle_count: int,
    websocket_connected: bool,
    prices_available: bool,
    regime_gate_blocked: bool,
    entries_taken: int,
    entry_cap: int,
    unscored_intraday: int,
    stale_contexts: int,
    failed_entries: int,
    same_day_discoveries: int = 0,
    allocator_total_candidates: int = 0,
    allocator_declined_count: int = 0,
    slippage_exceed_count: int = 0,
    kill_switch_active: bool = False,
    same_setup_rejected_days: int = 0,
    sb=None,
) -> Tuple[List[str], List[str]]:
    """
    Detect gaps that need IMMEDIATE attention during the daemon loop.

    Returns (gaps, improvements) — gaps are logged as WARNING immediately,
    improvements as INFO. Uses dedup so the same gap doesn't spam every
    cycle: it fires on first detection, then repeats every
    IMMEDIATE_GAP_REPEAT_CYCLES cycles while still active.

    ENHANCED: Now covers all 5 previously-uncaptured limitations:
      1. Allocator consistently declining all entries
      2. Entry cap reached every day (cross-day trend)
      3. Same setup rejected N days in a row (cross-day tracking)
      4. Slippage exceeding threshold
      5. Kill switch activated mid-session
    """
    gaps: List[str] = []
    improvements: List[str] = []
    now = cycle_count

    def _should_fire(key: str) -> bool:
        """True if this gap should fire now (first time, or repeat interval)."""
        state = _immediate_gap_state.get(key)
        if state is None:
            return True  # first time seeing this gap
        if state.get("resolved", False):
            # Was resolved, now back — fire again
            return True
        cycles_since = now - state.get("last_fired", 0)
        return cycles_since >= _IMMEDIATE_GAP_REPEAT_CYCLES

    def _mark_fired(key: str) -> None:
        _immediate_gap_state[key] = {"last_fired": now, "resolved": False}

    def _mark_resolved(key: str) -> None:
        _immediate_gap_state[key] = {"last_fired": now, "resolved": True}

    # ── Immediate gaps (fire NOW when detected) ────────────────────────────

    if kill_switch_active:
        if _should_fire("kill_switch_active"):
            gaps.append("🔴 KILL SWITCH ACTIVE — all trading halted immediately")
            _mark_fired("kill_switch_active")
    else:
        _mark_resolved("kill_switch_active")

    if not websocket_connected:
        if _should_fire("websocket_disconnected"):
            gaps.append("🔴 Websocket disconnected — daemon on REST polling (latency risk)")
            _mark_fired("websocket_disconnected")
    else:
        _mark_resolved("websocket_disconnected")

    if not prices_available:
        if _should_fire("no_live_prices"):
            gaps.append("🔴 No live prices — cannot evaluate entries or exits safely")
            _mark_fired("no_live_prices")
    else:
        _mark_resolved("no_live_prices")

    if regime_gate_blocked and entries_taken == 0:
        if _should_fire("regime_gate_blocked"):
            gaps.append("🔴 Regime gate blocked all entries — new swing entries suspended")
            _mark_fired("regime_gate_blocked")
    else:
        _mark_resolved("regime_gate_blocked")

    # ── NEW: Allocator consistently declining all entries ──────────────────
    if allocator_total_candidates > 0 and allocator_declined_count > 0:
        decline_pct = (allocator_declined_count / allocator_total_candidates) * 100
        if decline_pct >= ALLOCATOR_DECLINE_THRESHOLD_PCT:
            if _should_fire("allocator_declining_all"):
                gaps.append(
                    f"🔴 Allocator declined {allocator_declined_count}/{allocator_total_candidates} "
                    f"candidates ({decline_pct:.0f}%) — no entries possible"
                )
                _mark_fired("allocator_declining_all")
        else:
            _mark_resolved("allocator_declining_all")
    else:
        _mark_resolved("allocator_declining_all")

    # ── NEW: Same setup rejected N days in a row (cross-day) ─────────────
    if same_setup_rejected_days >= 3:
        if _should_fire("setup_rejected_multi_day"):
            gaps.append(
                f"🔴 Same setup rejected {same_setup_rejected_days} days in a row — "
                f"review entry criteria or regime alignment"
            )
            _mark_fired("setup_rejected_multi_day")
    else:
        _mark_resolved("setup_rejected_multi_day")

    # ── NEW: Slippage exceeding threshold ────────────────────────────────
    if slippage_exceed_count > 0:
        if _should_fire("slippage_exceed"):
            gaps.append(
                f"🔴 {slippage_exceed_count} entry/exit fills exceeded {SLIPPAGE_THRESHOLD_BPS} bps "
                f"slippage — review entry/exit pricing"
            )
            _mark_fired("slippage_exceed")
    else:
        _mark_resolved("slippage_exceed")

    # ── Existing gaps ──────────────────────────────────────────────────────

    if unscored_intraday > 0:
        if _should_fire("unscored_outcomes"):
            gaps.append(f"🔴 {unscored_intraday} intraday outcomes unscored — learning loop incomplete")
            _mark_fired("unscored_outcomes")
    else:
        _mark_resolved("unscored_outcomes")

    if stale_contexts > 0:
        if _should_fire("stale_contexts"):
            gaps.append(f"🔴 {stale_contexts} stale contexts — decisions may use outdated prices")
            _mark_fired("stale_contexts")
    else:
        _mark_resolved("stale_contexts")

    if failed_entries > 0:
        if _should_fire("failed_entries"):
            gaps.append(f"🔴 {failed_entries} entries failed to fill — review slip/chase settings")
            _mark_fired("failed_entries")
    else:
        _mark_resolved("failed_entries")

    # ── Immediate improvements (fire once per cycle block) ─────────────────
    # "Zero entries today" moved to session-end banner — no per-cycle spam.

    if entries_taken >= entry_cap and entry_cap > 0:
        improvements.append("💡 Entry cap fully consumed — consider raising swing_max_new_per_day")

    # ── NEW: Cross-day entry cap trend ───────────────────────────────────
    if sb is not None:
        cap_days = _get_cross_day_state(sb, _CROSS_DAY_KEYS["gap_entry_cap_days"])
        if entries_taken >= entry_cap and entry_cap > 0:
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_entry_cap_days"], cap_days + 1)
            if cap_days >= 2:
                improvements.append(
                    f"💡 Entry cap reached {cap_days + 1} days in a row — "
                    f"review regime tolerance or increase cap"
                )
        else:
            # Reset counter if cap not hit today
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_entry_cap_days"], 0)

        # ── NEW: Cross-day allocator decline trend ───────────────────────
        alloc_decline_days = _get_cross_day_state(sb, _CROSS_DAY_KEYS["gap_allocator_decline_days"])
        if allocator_total_candidates > 0 and allocator_declined_count > 0:
            decline_pct = (allocator_declined_count / allocator_total_candidates) * 100
            if decline_pct >= ALLOCATOR_DECLINE_THRESHOLD_PCT:
                _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_allocator_decline_days"], alloc_decline_days + 1)
                if alloc_decline_days >= 2:
                    improvements.append(
                        f"💡 Allocator declined >{ALLOCATOR_DECLINE_THRESHOLD_PCT}% of candidates "
                        f"for {alloc_decline_days + 1} days — review regime or edge model"
                    )
            else:
                _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_allocator_decline_days"], 0)
        else:
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_allocator_decline_days"], 0)

        # ── NEW: Cross-day slippage trend ────────────────────────────────
        slippage_days = _get_cross_day_state(sb, _CROSS_DAY_KEYS["gap_slippage_exceed_days"])
        if slippage_exceed_count > 0:
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_slippage_exceed_days"], slippage_days + 1)
            if slippage_days >= 2:
                improvements.append(
                    f"💡 Slippage exceeded {SLIPPAGE_THRESHOLD_BPS} bps on "
                    f"{slippage_days + 1} days — review entry timing or chase limits"
                )
        else:
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_slippage_exceed_days"], 0)

        # ── NEW: Cross-day same-setup rejection ──────────────────────────
        rej_days = _get_cross_day_state(sb, _CROSS_DAY_KEYS["gap_setup_rejection_count"])
        if same_setup_rejected_days > 0:
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_setup_rejection_count"], rej_days + same_setup_rejected_days)
        else:
            # No rejection today — reset
            _set_cross_day_state(sb, _CROSS_DAY_KEYS["gap_setup_rejection_count"], 0)

    return gaps, improvements


def log_immediate_gaps(
    cycle_count: int,
    websocket_connected: bool,
    prices_available: bool,
    regime_gate_blocked: bool,
    entries_taken: int,
    entry_cap: int,
    unscored_intraday: int,
    stale_contexts: int,
    failed_entries: int,
    same_day_discoveries: int = 0,
    allocator_total_candidates: int = 0,
    allocator_declined_count: int = 0,
    slippage_exceed_count: int = 0,
    kill_switch_active: bool = False,
    same_setup_rejected_days: int = 0,
    sb=None,
) -> Tuple[List[str], List[str]]:
    """
    Detect and LOG immediate gaps during the daemon loop.

    Gaps are logged as WARNING immediately. Improvements as INFO.
    Uses dedup so the same gap doesn't spam every cycle.
    """
    # Set the cycle counter so refusal dedup (log_refusal()) knows which
    # cycle we are in.
    _refusal_cycle_count[0] = cycle_count

    gaps, improvements = detect_immediate_gaps(
        cycle_count=cycle_count,
        websocket_connected=websocket_connected,
        prices_available=prices_available,
        regime_gate_blocked=regime_gate_blocked,
        entries_taken=entries_taken,
        entry_cap=entry_cap,
        unscored_intraday=unscored_intraday,
        stale_contexts=stale_contexts,
        failed_entries=failed_entries,
        same_day_discoveries=same_day_discoveries,
        allocator_total_candidates=allocator_total_candidates,
        allocator_declined_count=allocator_declined_count,
        slippage_exceed_count=slippage_exceed_count,
        kill_switch_active=kill_switch_active,
        same_setup_rejected_days=same_setup_rejected_days,
        sb=sb,
    )

    if gaps:
        logger.warning("═" * 72)
        logger.warning("  ⚠️  IMMEDIATE GAPS DETECTED (during daemon cycle)")
        logger.warning("═" * 72)
        for gap in gaps:
            logger.warning(f"  • {gap}")
        logger.warning("")

    if improvements:
        logger.info("═" * 72)
        logger.info("  💡 IMPROVEMENT AREAS")
        logger.info("═" * 72)
        for imp in improvements:
            logger.info(f"  • {imp}")
        logger.info("")

    return gaps, improvements


# ─────────────────────────────────────────────────────────────────────────────
# DROP-IN REPLACEMENT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def log_session_start(**kwargs) -> None:
    """Log the session start banner."""
    logger.info(format_session_start(**kwargs))


def log_cycle_summary(**kwargs) -> None:
    """Log a per-cycle summary."""
    logger.info(format_cycle_summary(**kwargs))


def log_session_end(**kwargs) -> None:
    """Log the session end banner."""
    logger.info(format_session_end(**kwargs))


def log_gaps_and_improvements(gaps: List[str], improvements: List[str]) -> None:
    """Log detected gaps and improvement areas."""
    if gaps:
        logger.warning("═" * 72)
        logger.warning("  ⚠️  GAPS & LOOPHOLES DETECTED")
        logger.warning("═" * 72)
        for gap in gaps:
            logger.warning(f"  • {gap}")
        logger.warning("")

    if improvements:
        logger.info("═" * 72)
        logger.info("  💡 IMPROVEMENT AREAS")
        logger.info("═" * 72)
        for imp in improvements:
            logger.info(f"  • {imp}")
        logger.info("")
