"""
Do the risk numbers tell one consistent story? And is each one actually read?

    python -m tools.validate_config          report
    python -m tools.validate_config --fix    apply the suggested coherent values

TWO SEPARATE QUESTIONS, BOTH EASY TO GET WRONG
-----------------------------------------------
1. COHERENCE. A per-order cap of Rs 25,000 on a Rs 20,000 account is not
   conservative, it is INERT — it can never bind, so it protects nothing. The
   entire purpose of a hard rupee ceiling is to catch a sizing bug, and every
   sizing input in this project (capital, risk percent, ATR) has been wrong at
   some point. A cap above the account cannot catch anything.

2. WIRING. A value that no script reads is a setting you believe is in force
   and is not. That is the same class of failure as a column nobody writes, and
   it has bitten this project repeatedly — swing_auto_entry existed as a config
   key for days before any code read it.

The check reads TOTAL_CAPITAL from .env, because that is what sizing actually
uses, and derives what each cap SHOULD be from it rather than asserting fixed
numbers that go stale the moment capital changes.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import TOTAL_CAPITAL, get_supabase, cfg_float, capital_for

BASE = Path(__file__).parent.parent


@dataclass
class Finding:
    key: str
    severity: str        # ERROR | WARN | OK
    current: str
    suggested: str | None
    why: str


def _read(sb, keys: list[str]) -> dict:
    rows = (sb.table("system_config").select("key,value")
              .in_("key", keys).execute().data or [])
    return {r["key"]: r["value"] for r in rows}


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def check_coherence(sb=None) -> list[Finding]:
    """
    Every cap expressed against the capital that actually funds it.

    The derived ceilings are not arbitrary. max_position_pct is what the sizing
    model may propose; a per-order cap should sit modestly ABOVE that — high
    enough never to block a legitimate trade, low enough to catch a sizing bug
    before it spends the account. Above TOTAL_CAPITAL it catches nothing at all.
    """
    sb = sb or get_supabase()
    cap = TOTAL_CAPITAL
    keys = ["swing_max_order_value", "swing_max_orders_per_day",
            "swing_max_notional_per_day", "intraday_max_order_value",
            "intraday_max_orders_per_day", "intraday_max_notional_per_day",
            "risk_pct_per_trade", "max_position_pct", "portfolio_min_position_pct",
            "paper_starting_capital", "capital_tolerance_pct",
            # Every key any check below reads must be listed here. A key that is
            # checked but not fetched silently falls back to its default, and the
            # report then describes a configuration that is not the one running —
            # which is the same silent-default failure this tool exists to find.
            "intraday_max_position_pct", "paper_max_open_positions",
            # Read by the sleeve-fits-the-account check below. intraday_capital
            # is the operator's dashboard sleeve; intraday_trading_mode decides
            # whether an oversized one is harmless (PAPER) or starves the swing
            # book (LIVE).
            "intraday_capital", "intraday_trading_mode"]
    c = _read(sb, keys)
    missing = [k for k in keys if k not in c]
    if missing:
        logger.warning(f"  not present in system_config, using defaults: "
                       f"{', '.join(missing)}")
    out: list[Finding] = []

    risk_pct = _f(c.get("risk_pct_per_trade"), 1.0)
    min_pos_pct = _f(c.get("portfolio_min_position_pct"), 3.0) / 100.0

    # Each book sizes against its OWN capital (config.capital_for) since the
    # swing/intraday split — swing_capital/intraday_capital default to
    # TOTAL_CAPITAL until set explicitly, so this is a no-op until they diverge.
    # Checking both frameworks against the single pooled TOTAL_CAPITAL here was
    # exactly the kind of drift the split was meant to prevent: intraday's caps
    # would be validated against swing's real account instead of intraday's own
    # (possibly much larger, paper) sleeve.
    book_cap = {"swing": capital_for("SWING"), "intraday": capital_for("INTRADAY")}

    # The two frameworks size differently, and checking both against one model
    # is how intraday came to have no per-position fraction at all:
    #   swing    — analysis/risk_model.py: min(risk budget, max_position_pct)
    #   intraday — intraday/engine.py: intraday_max_position_pct x market mult
    swing_pct = _f(c.get("max_position_pct"), 20.0)
    intra_pct = _f(c.get("intraday_max_position_pct"), 25.0)
    sized = {"swing": book_cap["swing"] * swing_pct / 100.0,
             "intraday": book_cap["intraday"] * intra_pct / 100.0}

    out.append(Finding("TOTAL_CAPITAL", "OK", f"₹{cap:,.0f}", None,
                       f"from .env — swing sizes to at most ₹{sized['swing']:,.0f} "
                       f"({swing_pct:g}%), intraday to ₹{sized['intraday']:,.0f} "
                       f"({intra_pct:g}%), risking ₹{cap * risk_pct / 100:,.0f} "
                       f"({risk_pct:g}%) per swing trade"))

    # DOES THE SLEEVE FIT INSIDE THE ACCOUNT — AND IF NOT, WHEN DOES THAT BITE?
    #
    # capital_for() hands swing the WHOLE account while intraday is PAPER,
    # because a simulated position holds no rupees. That makes an oversized
    # intraday_capital completely harmless today and fatal the moment intraday
    # is switched LIVE: swing's sleeve becomes TOTAL_CAPITAL - intraday_capital,
    # which goes to zero and refuses every entry.
    #
    # A ₹100,000 paper sleeve on a ₹30,000 account is a real, current example.
    # It is deliberate — paper is sized bigger for realism — so this does not
    # fail while intraday is paper. It says so plainly instead, because the day
    # the switch flips is the day nobody re-reads a sizing key.
    sleeve = _f(c.get("intraday_capital"), cap)
    intraday_live = (c.get("intraday_trading_mode") or "PAPER").upper() == "LIVE"
    if sleeve >= cap:
        if intraday_live:
            out.append(Finding(
                "intraday_capital", "ERROR", f"₹{sleeve:,.0f}",
                f"below ₹{cap:,.0f}",
                f"intraday is LIVE and its sleeve is >= the whole ₹{cap:,.0f} "
                f"account, so swing has ₹0 to size against and refuses EVERY "
                f"entry. The two books cannot both spend the same rupee"))
        else:
            out.append(Finding(
                "intraday_capital", "WARN", f"₹{sleeve:,.0f}",
                f"below ₹{cap:,.0f} before going live",
                f"harmless while intraday is PAPER — swing gets the full "
                f"₹{cap:,.0f} because a simulated position reserves nothing. "
                f"But switching intraday to LIVE with this value leaves swing "
                f"₹{cap - sleeve:,.0f} and it will refuse every entry"))

    for fw in ("swing", "intraday"):
        cap_fw = book_cap[fw]
        min_pos = cap_fw * min_pos_pct
        sized_max = sized[fw]
        # A cap should bind on a BUG, not on normal sizing. 1.5x the largest
        # legal position is enough headroom for rounding and a chase, and still
        # far below anything that would empty the account.
        want_order = round(min(sized_max * 1.5, cap_fw * 0.5), -2)

        k = f"{fw}_max_order_value"
        v = _f(c.get(k))
        if v >= cap_fw:
            out.append(Finding(
                k, "ERROR", f"₹{v:,.0f}", f"₹{want_order:,.0f}",
                f"₹{v:,.0f} is {v / cap_fw:.0%} of a ₹{cap_fw:,.0f} {fw} sleeve — this "
                f"cap can never bind, so it protects nothing. Sizing tops out at "
                f"₹{sized_max:,.0f}; the cap exists to catch a bug above that."))
        elif v > sized_max * 2.5:
            out.append(Finding(
                k, "WARN", f"₹{v:,.0f}", f"₹{want_order:,.0f}",
                f"₹{v:,.0f} is {v / sized_max:.1f}x the largest position sizing can "
                f"propose (₹{sized_max:,.0f}) — it only binds on a severe bug."))
        elif v < min_pos:
            out.append(Finding(
                k, "ERROR", f"₹{v:,.0f}", f"₹{want_order:,.0f}",
                f"₹{v:,.0f} is below the ₹{min_pos:,.0f} minimum position size — every "
                f"legitimate order would be blocked."))
        else:
            out.append(Finding(k, "OK", f"₹{v:,.0f}", None,
                               f"binds above normal sizing (₹{sized_max:,.0f}), "
                               f"below the {fw} sleeve"))

        # Daily notional: what could plausibly be committed in a day, capped at
        # this book's OWN sleeve. Deliberately allowed to sit BELOW
        # want_order x n_orders — that product is the ceiling implied by the
        # other two caps, not a target. A notional cap tighter than it is a
        # real, independent choice ("cap total daily deployment even if no
        # single order is oversized and the count is normal"), not redundancy.
        n_orders = _f(c.get(f"{fw}_max_orders_per_day"), 5)
        nk = f"{fw}_max_notional_per_day"
        nv = _f(c.get(nk))
        want_notional = round(min(cap_fw, want_order * max(n_orders, 1)), -2)
        if nv > cap_fw:
            out.append(Finding(
                nk, "ERROR", f"₹{nv:,.0f}", f"₹{want_notional:,.0f}",
                f"₹{nv:,.0f} exceeds the whole ₹{cap_fw:,.0f} {fw} sleeve — it can "
                f"never bind. A daily notional cap should stop the sleeve being "
                f"recycled repeatedly into a bad day."))
        else:
            out.append(Finding(nk, "OK", f"₹{nv:,.0f}", None,
                               f"{nv / cap_fw:.0%} of the {fw} sleeve"))

    # Paper capital far above real capital makes paper results untransferable:
    # the simulation would take positions the real account could never fund.
    pc = _f(c.get("paper_starting_capital"), 100000)
    if pc > cap * 1.5:
        out.append(Finding(
            "paper_starting_capital", "ERROR", f"₹{pc:,.0f}", f"₹{cap:,.0f}",
            f"₹{pc:,.0f} is {pc / cap:.1f}x the real account. Paper would take "
            f"positions you could never fund, so its results would not transfer — "
            f"which is the only thing paper trading is for."))
    else:
        out.append(Finding("paper_starting_capital", "OK", f"₹{pc:,.0f}", None,
                           "matches the real account, so paper results transfer"))

    # The paper book must fit inside paper capital. Otherwise the simulation
    # stops taking setups partway through the day for a reason that has nothing
    # to do with whether they were good — and the results are quietly truncated.
    n_paper = _f(c.get("paper_max_open_positions"), 5)
    biggest = max(sized.values())
    if n_paper * biggest > pc * 1.05:
        out.append(Finding(
            "paper_max_open_positions", "WARN", f"{n_paper:g}",
            f"{max(1, int(pc // biggest)):g}",
            f"{n_paper:g} positions x ₹{biggest:,.0f} needs ₹{n_paper * biggest:,.0f}, "
            f"more than the ₹{pc:,.0f} paper account — the last setups of the day "
            f"would be skipped for lack of cash rather than lack of quality."))
    else:
        out.append(Finding("paper_max_open_positions", "OK", f"{n_paper:g}", None,
                           f"{n_paper:g} x ₹{biggest:,.0f} fits inside ₹{pc:,.0f}"))
    return out


def check_wiring(sb=None) -> list[Finding]:
    """
    Is every risk key actually read by code?

    Greps for the key name across the backend, excluding migrations (which
    only ever WRITE defaults) and this tool. A key present only in SQL is a
    setting nobody consults.
    """
    sb = sb or get_supabase()
    rows = (sb.table("system_config").select("key,risk_level")
              .eq("risk_level", "CRITICAL").execute().data or [])
    critical = [r["key"] for r in rows]

    src = []
    for p in BASE.rglob("*.py"):
        parts = set(p.parts)
        if parts & {"__pycache__"} or p.name in ("validate_config.py",):
            continue
        try:
            src.append(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    blob = "\n".join(src)

    # Some keys are never written literally — they are built at runtime:
    #     cfg_float(f"portfolio_sector_cap_{regime}")
    #     cfg_float(f"{prefix}allow_reversal")
    # A literal-only search calls those unread when they are read on every
    # decision, and a checker that cries wolf on working code gets ignored —
    # taking the one real finding down with it.
    #
    # Rather than guess from prefixes (which passes anything sharing a first
    # word, so an invented "intraday_pineapple_gate" would look wired), each
    # f-string passed to a cfg*() call is turned into an anchored pattern with
    # {...} replaced by .+, and keys are matched against THAT. A key only counts
    # as read if some call site could actually have produced it.
    patterns = []
    for lit in re.findall(r'cfg\w*\(\s*f(["\'])(.+?)\1', blob):
        tmpl = lit[1]
        if "{" not in tmpl:
            continue
        rx = "".join(".+" if part.startswith("{") else re.escape(part)
                     for part in re.split(r"(\{[^{}]*\})", tmpl) if part)
        patterns.append(re.compile(rf"^{rx}$"))

    out = []
    for k in sorted(critical):
        if re.search(rf'["\']{re.escape(k)}["\']', blob):
            continue
        if any(p.match(k) for p in patterns):
            continue
        out.append(Finding(
            k, "ERROR", "(unread)", None,
            "marked CRITICAL but no Python reads it — a setting you believe is "
            "in force and is not"))
    if not out:
        out.append(Finding("wiring", "OK", f"{len(critical)} CRITICAL keys", None,
                           "every one is read by at least one module"))
    return out


def apply_fixes(findings: list[Finding], sb=None) -> int:
    sb = sb or get_supabase()
    n = 0
    for f in findings:
        if f.severity == "OK" or not f.suggested:
            continue
        val = re.sub(r"[₹,]", "", f.suggested)
        try:
            sb.table("system_config").update({"value": val}).eq("key", f.key).execute()
            logger.success(f"  {f.key}: {f.current} -> {f.suggested}")
            n += 1
        except Exception as e:
            logger.error(f"  {f.key}: could not update — {e}")
    return n


def main(fix: bool = False) -> int:
    sb = get_supabase()
    logger.info("═" * 72)
    logger.info("Config coherence — do the risk numbers tell one story?")
    logger.info("═" * 72)

    coh = check_coherence(sb)
    wir = check_wiring(sb)
    all_f = coh + wir

    for f in all_f:
        icon = {"ERROR": "✗", "WARN": "!", "OK": "✓"}[f.severity]
        log = {"ERROR": logger.error, "WARN": logger.warning, "OK": logger.info}[f.severity]
        log(f"  {icon} {f.key:<32} {f.current}"
            + (f"  ->  {f.suggested}" if f.suggested else ""))
        if f.severity != "OK":
            logger.info(f"       {f.why}")

    bad = [f for f in all_f if f.severity == "ERROR"]
    warn = [f for f in all_f if f.severity == "WARN"]
    logger.info("")
    logger.info(f"  {len(bad)} incoherent, {len(warn)} questionable, "
                f"{len(all_f) - len(bad) - len(warn)} consistent")

    if fix and (bad or warn):
        logger.info("")
        logger.info("  applying suggested values:")
        n = apply_fixes(all_f, sb)
        logger.success(f"  {n} value(s) updated — re-run to confirm")
    elif bad or warn:
        logger.info("")
        logger.info("  run with --fix to apply the suggestions")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate risk config coherence and wiring")
    ap.add_argument("--fix", action="store_true", help="apply the suggested values")
    sys.exit(main(ap.parse_args().fix))
