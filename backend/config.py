"""
TradeOS v7 — Central Configuration
All environment loading, Supabase client, shared utilities
"""
import os
import sys
import json
from datetime import date, datetime
from pathlib import Path
import pytz
from dotenv import load_dotenv
from loguru import logger
from supabase import create_client, Client
import yfinance as yf
from datetime import datetime, timedelta

# ── Load .env ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
env_file = BASE_DIR / ".env"

# override=False is deliberate: on GitHub Actions there is no .env file and the
# real values arrive as environment variables from repo secrets, so the process
# environment must win there.
#
# Locally that same rule is a trap. A stale OS-level variable silently shadows
# .env with no indication anywhere — we lost real time to exactly this: a
# Windows *User* variable pinned KITE_API_KEY to a retired Kite Connect app, so
# updating .env changed nothing and every API call returned
# "Incorrect `api_key` or `access_token`", which reads like an expiry problem
# and is not.
#
# Blanket override=True would break the `DRY_RUN=True python …` idiom, since
# .env sets DRY_RUN=False. So resolve the conflict by ASKING WHERE THE VARIABLE
# CAME FROM, which on Windows is knowable: a persisted User/Machine variable
# lives in the registry, while a per-invocation override set in the shell does
# not. If the live value matches what is persisted, it is stale configuration
# and .env — the file the user just edited — wins. If it differs, the user
# typed it deliberately this session and it keeps winning.
def _persisted_os_vars() -> dict:
    """
    User- and Machine-scope environment variables as PERSISTED, read straight
    from the registry rather than from os.environ.

    This is the only way to tell "stale variable someone set in the System
    Properties dialog two months ago" apart from "value the user exported for
    this one command". os.environ shows both identically.

    Read-only: nothing here modifies the registry or system settings.
    """
    if os.name != "nt":
        return {}
    out = {}
    try:
        import winreg
        for root, path in (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(root, path) as k:
                    for i in range(winreg.QueryInfoKey(k)[1]):
                        n, v, _ = winreg.EnumValue(k, i)
                        out.setdefault(n, v)
            except OSError:
                continue
    except Exception:
        return {}
    return out


_shadowed: list[str] = []    # .env ignored — deliberate session override, left alone
_unshadowed: list[str] = []  # .env restored over a stale persisted OS variable
if env_file.exists():
    try:
        from dotenv import dotenv_values
        _file_vals = dotenv_values(env_file)
        _persisted = _persisted_os_vars()
        for _k, _v in _file_vals.items():
            if _v is None or _k not in os.environ or os.environ[_k] == _v:
                continue
            if _persisted.get(_k) == os.environ[_k]:
                _unshadowed.append(_k)   # stale persisted var — .env wins
            else:
                _shadowed.append(_k)     # set for this run — environment wins
    except Exception:
        _file_vals = {}
    load_dotenv(env_file, override=False)
    for _k in _unshadowed:
        os.environ[_k] = _file_vals[_k]

# ── Paths ────────────────────────────────────────────────────
DATA      = BASE_DIR / "data"
MODELS    = BASE_DIR / "models"
LOGS      = BASE_DIR / "logs"
DATA.mkdir(exist_ok=True)
MODELS.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

# ── Logging ──────────────────────────────────────────────────
def _force_ipv4() -> None:
    """
    Make every outbound connection use IPv4, process-wide.

    WHY THIS IS NOT OPTIONAL
    ------------------------
    Zerodha's order allowlist accepts IPv4 addresses ONLY. api.kite.trade
    resolves IPv6-first on a dual-stack network, so Python connects over v6 and
    Kite sees a source address that is not on the list and cannot be put on it:

        IP (2402:e280:3e1a:...) is not allowed to place orders for this app

    Every order is rejected while the account, the token, the session and the
    allowlisted v4 address are all correct. Nothing in the error names IPv6, and
    `tradeos ip` reports the v4 address matching — so the check passes and the
    orders still fail. That combination cost a live session: a partial book on a
    winning position retried and was refused every 15 seconds for an hour.

    Filtering getaddrinfo rather than pinning an address is deliberate: Kite sits
    behind Cloudflare and its IPs rotate, so the hostname must still resolve
    normally — just never to a v6 answer.

    It lives in config.py because config is the first import in every entry
    point. In kite_client it worked only because price_feed happened to import
    that module before constructing the websocket, which is not a guarantee
    worth depending on for the thing that decides whether exits can execute.
    """
    import socket
    if getattr(socket, "_tradeos_ipv4_only", False):
        return
    _orig = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        res = _orig(host, port, socket.AF_INET, type, proto, flags)
        if res:
            return res
        # An IPv6-only host would be unreachable if this hard-failed. Fall back
        # rather than break a service that has nothing to do with orders.
        return _orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    socket._tradeos_ipv4_only = True


_force_ipv4()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logger.remove()
logger.add(
    LOGS / "tradeos_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="30 days",
    level=LOG_LEVEL, format="{time:HH:mm:ss} | {level:<8} | {name}:{line} - {message}"
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp1252 can't print ₹/✓/—
logger.add(lambda msg: print(msg, end=""), level="INFO",
           format="{time:HH:mm:ss} | {level:<8} | {message}")

# ── Core settings ─────────────────────────────────────────────
IST          = pytz.timezone("Asia/Kolkata")
DRY_RUN      = os.getenv("DRY_RUN", "False").lower() == "true"
# A MISSING value and a DELIBERATE value must not look the same. In GitHub
# Actions `${{ secrets.TOTAL_CAPITAL }}` expands to an empty string when the
# secret does not exist, which `or "200000"` silently turned into two lakh —
# so every scheduled run sized positions for 10x the real account while the
# local runs (which read .env) sized them correctly. Nothing in the output
# distinguished the two. Position size is the single number that decides how
# much money a bad signal costs, so it defaults LOW and says so loudly.
_capital_raw = (os.getenv("TOTAL_CAPITAL") or "").strip()
CAPITAL_IS_FALLBACK = not _capital_raw
TOTAL_CAPITAL_FALLBACK = 20000.0
try:
    TOTAL_CAPITAL = float(_capital_raw) if _capital_raw else TOTAL_CAPITAL_FALLBACK
    if TOTAL_CAPITAL <= 0:
        raise ValueError("must be positive")
except ValueError:
    logger.error(f"TOTAL_CAPITAL={_capital_raw!r} is not a usable number — "
                 f"falling back to ₹{TOTAL_CAPITAL_FALLBACK:,.0f}")
    TOTAL_CAPITAL, CAPITAL_IS_FALLBACK = TOTAL_CAPITAL_FALLBACK, True


def capital_for(framework: str) -> float:
    """
    The capital ONE book sizes against — not the shared pool.

    Defaults to TOTAL_CAPITAL for both, so a book with no sleeve configured
    behaves exactly as it always has. Only diverges once swing_capital or
    intraday_capital is explicitly set in system_config, which is how paper
    intraday can be sized bigger for realism without ever touching what
    swing's real, live orders size against — they are different config keys,
    not a shared TOTAL_CAPITAL read by both. See swing_capital / intraday_capital.
    """
    key = "intraday_capital" if (framework or "").upper() == "INTRADAY" else "swing_capital"
    return cfg_float(key, TOTAL_CAPITAL)

# ── Supabase ─────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

_sb_client: Client | None = None

def get_supabase() -> Client:
    global _sb_client
    if _sb_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        _sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb_client

# ── Google Sheets ─────────────────────────────────────────────
GOOGLE_SHEET_ID       = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS    = os.getenv("GOOGLE_CREDENTIALS_JSON", str(BASE_DIR / "credentials" / "service_account.json"))

# ── AI Providers ─────────────────────────────────────────────
AI_KEYS = {
    "claude":   os.getenv("ANTHROPIC_API_KEY", ""),
    "openai":   os.getenv("OPENAI_API_KEY", ""),
    "gemini":   os.getenv("GEMINI_API_KEY", ""),
    "deepseek": os.getenv("DEEPSEEK_API_KEY", ""),
    "grok":     os.getenv("GROK_API_KEY", ""),
    "copilot":  os.getenv("AZURE_OPENAI_API_KEY", ""),
}
AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# ── Telegram ─────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Kite ─────────────────────────────────────────────────────
KITE_API_KEY    = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

# ── System config cache ───────────────────────────────────────
_sys_config: dict | None = None

def get_system_config(refresh: bool = False) -> dict:
    """Load system_config table from Supabase. Cached per run."""
    global _sys_config
    if _sys_config is None or refresh:
        try:
            sb = get_supabase()
            rows = sb.table("system_config").select("key,value").execute().data
            _sys_config = {r["key"]: r["value"] for r in rows}
        except Exception as e:
            logger.warning(f"Could not load system_config: {e} — using defaults")
            _sys_config = {}
    return _sys_config

def cfg(key: str, default: str = "") -> str:
    return get_system_config().get(key, default)

def cfg_bool(key: str, default: bool = False) -> bool:
    return cfg(key, str(default)).lower() in ("true", "1", "yes")

def cfg_int(key: str, default: int = 0) -> int:
    try:
        return int(cfg(key, str(default)))
    except ValueError:
        return default

def cfg_float(key: str, default: float = 0.0) -> float:
    try:
        return float(cfg(key, str(default)))
    except ValueError:
        return default

# ── Strategy config ───────────────────────────────────────────
_strategy_config: dict | None = None

def _coerce_params(strategy: str, raw) -> dict:
    """
    Normalise a strategy_config.params value to a plain dict.

    The column is jsonb but rows were historically written as a DOUBLE-ENCODED
    string ("{\\"max_sector_rank\\": 4.0, ...}"), so consumers each carried
    their own `json.loads(v) if isinstance(v, str)` shim. Migration 004 then
    ran `params || patch` against that string, and Postgres built an ARRAY
    rather than merging — which turned params into a list and made
    `cfg_ctl.get(...)` raise AttributeError inside run_ctl/run_sbs, both of
    which are fatal pipeline steps.

    Normalising centrally means every caller gets a dict, and a malformed row
    degrades that ONE strategy to its defaults instead of taking the pipeline
    down. Migration 005 repairs the stored shape and adds a CHECK constraint;
    this function is the belt to that braces.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning(f"strategy_config[{strategy}]: unparseable string — using defaults")
            return dict(DEFAULT_STRATEGY_PARAMS.get(strategy, {}))
    if isinstance(raw, list):
        # Array form produced by `jsonb_string || jsonb_object`. Merge left to
        # right so a later patch element still wins.
        merged: dict = {}
        for el in raw:
            part = _coerce_params(strategy, el)
            if part:
                merged.update(part)
        if merged:
            logger.warning(
                f"strategy_config[{strategy}]: params stored as an array "
                f"(see migration 005) — merged {len(raw)} elements in memory. "
                f"Run 005_fix_strategy_config_shape.sql to repair the row."
            )
            return merged
    logger.warning(f"strategy_config[{strategy}]: unusable params — using defaults")
    return dict(DEFAULT_STRATEGY_PARAMS.get(strategy, {}))


def get_strategy_config(refresh: bool = False) -> dict:
    """
    Load strategy_config from Supabase as {strategy: dict}.

    Always returns plain dicts — callers must not need to json.loads() or
    type-check the result.
    """
    global _strategy_config
    if _strategy_config is None or refresh:
        try:
            sb = get_supabase()
            rows = sb.table("strategy_config").select("*").eq("enabled", True).execute().data
            _strategy_config = {r["strategy"]: _coerce_params(r["strategy"], r["params"])
                                for r in rows}
        except Exception as e:
            logger.warning(f"Could not load strategy_config: {e} — using hardcoded defaults")
            _strategy_config = DEFAULT_STRATEGY_PARAMS
    return _strategy_config

# ── Default strategy parameters (fallback if DB empty) ───────
DEFAULT_STRATEGY_PARAMS = {
    "CTL": {
        "max_sector_rank":  4,
        "min_monthly_rsi":  58,
        "min_weekly_rsi":   58,
        "min_6m_return":    0,
        "max_atr_pct":      4,
        "min_market_cap":   500,
        "max_positions":    6,
    },
    "SBS": {
        "max_sector_rank":  7,
        "min_daily_rsi":    55,
        "min_weekly_rsi":   56,
        "min_vol_ratio":    1.3,
        "max_consol_pct":   12,
        "max_atr_pct":      5,
        "min_market_cap":   300,
    },
    "TPO": {
        "source":           "CTL",
        "min_rsi":          42,
        "max_rsi":          55,
        "max_dist_sma50":   3,
        "max_atr_pct":      4,
        "max_ctl_rank":     15,
    },
    "EAP": {
        "pre_event_days":       2,
        "pre_event_aggression": 1.0,
        "post_event_aggression":2.0,
        "risk_off_penalty":     -2.0,
    },
}

# ── Helpers ──────────────────────────────────────────────────
def today_ist() -> date:
    return datetime.now(IST).date()

def is_kill_switch_active() -> bool:
    return cfg_bool("master_kill_switch", False)

def get_max_positions(regime: str = "NEUTRAL") -> int:
    mapping = {
        "RISK ON":  cfg_int("max_positions_risk_on", 8),
        "NEUTRAL":  cfg_int("max_positions_neutral", 7),
        "RISK OFF": cfg_int("max_positions_risk_off", 6),
    }
    return mapping.get(regime, 7)

def buy_candidate_threshold() -> float:
    return cfg_float("buy_candidate_threshold_pct", 0.5) / 100

logger.info(
    f"TradeOS v7 config loaded | Capital=₹{TOTAL_CAPITAL:,.0f} | "
    f"DRY_RUN={DRY_RUN} | Phase={cfg('autonomy_phase', '0')} | "
    f"Today's date={today_ist()}"
)

if CAPITAL_IS_FALLBACK:
    logger.warning(
        f"⚠ TOTAL_CAPITAL is not set — every position size in this run is "
        f"computed against the ₹{TOTAL_CAPITAL:,.0f} fallback, not your real "
        f"account. In GitHub Actions this means the TOTAL_CAPITAL repo secret "
        f"is missing (an absent secret expands to an empty string, not an error)."
    )

if _unshadowed:
    logger.info(
        f"  .env took precedence for {', '.join(sorted(_unshadowed))} over a "
        f"stale persisted Windows variable of the same name. Remove it with: "
        f"[Environment]::SetEnvironmentVariable('<NAME>',$null,'User')"
    )

if _shadowed:
    logger.warning(
        f"⚠ {len(_shadowed)} value(s) in .env are being IGNORED because the "
        f"environment sets them for this run: {', '.join(sorted(_shadowed))}"
    )
    logger.warning(
        "  These are NOT persisted OS variables, so they look deliberate "
        "(`$env:X='…'` in this shell, or GitHub Actions secrets) and the "
        "environment wins. Unset them if that was not intended."
    )

def yf_fetch_with_cache(
    sb,
    ticker:     str,
    cache_table: str,
    cache_key:   str,
    today:       str,
    *,
    fallback_column: str = "value",
    max_stale_days:  int = 1,
) -> float | None:
    """
    Fetch a single scalar value (latest close / price) for a yfinance ticker.
    Cache-first: reads from Supabase, calls yfinance only on a cache miss.

    Args:
        sb              : Supabase client
        ticker          : yfinance ticker string e.g. "^NSEI", "^TNX", "SI=F"
        cache_table     : Supabase table to check first e.g. "macro_indicators"
        cache_key       : row identifier in that table e.g. "US_10YR_YIELD"
        today           : date string YYYY-MM-DD
        fallback_column : column name holding the cached value (default "value")
        max_stale_days  : treat cache as valid if row date >= today - N days
                          (handles weekends/holidays gracefully)

    Returns:
        float | None
    """
    # ── 1. Try Supabase cache first ───────────────────────────────────────
    stale_cutoff = str(
        (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=max_stale_days)).date()
    )
    try:
        rows = (
            sb.table(cache_table)
              .select(f"date,{fallback_column}")
              .eq("key", cache_key)
              .gte("date", stale_cutoff)
              .order("date", desc=True)
              .limit(1)
              .execute().data
        )
        if rows:
            val = rows[0].get(fallback_column)
            if val is not None:
                logger.debug(
                    f"  yf_cache HIT  | {cache_key} = {val} "
                    f"(from {cache_table}, date={rows[0]['date']})"
                )
                return float(val)
    except Exception as exc:
        logger.debug(f"  yf_cache read failed for {cache_key}: {exc}")

    # ── 2. Cache miss — call yfinance ─────────────────────────────────────
    try:
        logger.debug(f"  yf_cache MISS | {cache_key} → fetching {ticker} from yfinance")
        df = yf.download(ticker, period="5d", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty:
            return None
        val = float(df["Close"].dropna().iloc[-1])
        logger.debug(f"  yf live       | {ticker} = {val}")
        return val
    except Exception as exc:
        logger.warning(f"  yfinance failed for {ticker}: {exc}")
        return None

def get_news_window(sb, trading_day: str) -> tuple[str, str]:
    """
    Returns (window_start, window_end) for querying market_news.

    Logic:
      - window_end   = trading_day (the current trading session)
      - window_start = day after the previous trading day
                       e.g. if prev trading day was Thu Apr 30,
                            window_start = Fri May 01
                       This captures Fri + Sat + Sun news for a Monday run.

    Supabase query pattern:
      .gte("news_date", window_start).lte("news_date", window_end)
    """
    from datetime import datetime, timedelta

    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}

    candidate = datetime.strptime(trading_day, "%Y-%m-%d").date() - timedelta(days=1)

    # Walk back to find the previous trading day
    for _ in range(10):
        date_str = str(candidate)
        if candidate.weekday() not in (5, 6) and date_str not in holidays:
            # Verify it actually has data
            probe = (sb.table("stock_data_daily")
                       .select("date")
                       .eq("date", date_str)
                       .limit(1)
                       .execute().data)
            if probe:
                prev_trading_day = date_str
                break
        candidate -= timedelta(days=1)
    else:
        # Fallback: just use 3 calendar days back
        prev_trading_day = str(
            datetime.strptime(trading_day, "%Y-%m-%d").date() - timedelta(days=3)
        )

    # window_start = day AFTER previous trading day
    # This captures all weekend/holiday news between the two sessions
    window_start = str(
        datetime.strptime(prev_trading_day, "%Y-%m-%d").date() + timedelta(days=1)
    )
    return window_start, trading_day

# Add to config.py after the existing helper functions
def get_trade_date(sb=None, mode: str = "auto", caller: str = "") -> str:
    tag            = f"[{caller}] " if caller else ""
    calendar_today = str(today_ist())

    if mode == "today":
        logger.info(f"  {tag}trade_date={calendar_today} | mode=today | source=calendar")
        return calendar_today

    if sb is None:
        sb = get_supabase()

    try:
        rows = (
            sb.table("stock_data_daily")
              .select("date")
              .order("date", desc=True)
              .limit(1)
              .execute().data
        )
        if rows:
            td = rows[0]["date"]

            if td == calendar_today:
                logger.info(
                    f"  {tag}trade_date={td} | mode={mode} | "
                    f"source=stock_data_daily | bhavcopy=✅ written"
                )
            else:
                # ── Classify WHY td != today before deciding log level ──
                now_ist    = datetime.now(IST)
                is_weekday = now_ist.weekday() < 5   # Mon=0 … Fri=4

                # Check NSE holiday list
                try:
                    holiday_rows = sb.table("nse_holidays").select("date").execute().data
                    holidays     = {r["date"] for r in holiday_rows}
                    is_holiday   = calendar_today in holidays
                except Exception:
                    holidays   = set()
                    is_holiday = False

                # WHEN OUR INGEST IS EXPECTED, not when NSE publishes.
                #
                # NSE puts the bhavcopy out around 18:00 IST, but step 05 only
                # runs when the evening pipeline runs — 22:00 IST via
                # cron-job.org (the GitHub cron in pipeline_evening.yml is
                # commented out). Comparing against 18:00 therefore declared
                # "❌ MISSING — step 05 may have failed" for four hours every
                # single trading day, when nothing had failed and the run was
                # simply still ahead. A warning that fires daily on a healthy
                # system trains you to ignore the one that matters.
                eta_hour = cfg_int("pipeline_evening_hour", 22)
                bhavcopy_eta = now_ist.replace(hour=eta_hour, minute=0,
                                               second=0, microsecond=0)
                is_trading_day = is_weekday and not is_holiday

                if not is_trading_day:
                    # Weekend or holiday — previous trading day is EXPECTED
                    reason = "weekend" if not is_weekday else "holiday"
                    logger.info(
                        f"  {tag}trade_date={td} | mode={mode} | "
                        f"source=stock_data_daily | bhavcopy=✅ expected "
                        f"({reason} — previous trading day correct | calendar={calendar_today})"
                    )
                elif now_ist < bhavcopy_eta:
                    # Trading day, evening run still ahead — nothing is wrong.
                    logger.info(
                        f"  {tag}trade_date={td} | mode={mode} | "
                        f"source=stock_data_daily | bhavcopy=⏳ not yet "
                        f"(evening pipeline runs at {eta_hour:02d}:00 IST | "
                        f"calendar={calendar_today})"
                    )
                else:
                    # Trading day, the run should have happened — REAL problem.
                    logger.warning(
                        f"  {tag}trade_date={td} | mode={mode} | "
                        f"source=stock_data_daily | bhavcopy=❌ MISSING "
                        f"(evening pipeline was due at {eta_hour:02d}:00 IST — "
                        f"step 05 may have failed | calendar={calendar_today})"
                    )

            return td

    except Exception as e:
        logger.warning(
            f"  {tag}get_trade_date probe failed: {e} | "
            f"fallback=today_ist() | date={calendar_today}"
        )

    return calendar_today

def get_step_window(sb, step_symbol: str, current_td: str, fallback_days: int = 3) -> tuple[str, str, str]:
    """
    Returns (window_start, window_end_trade, window_end_calendar)

    window_end_trade    — use for: signals, MSL, regime, sectors (stock data)
    window_end_calendar — use for: news, macro, global_cues (can have weekend data)
    """
    calendar_today = str(today_ist())
    now_ist        = datetime.now(IST)

    # ── Step 1: Get last successful run date ─────────────────────────────
    last_run = None
    try:
        rows = (
            sb.table("ai_context")
              .select("date")
              .eq("symbol", step_symbol)
              .lte("date", current_td)
              .order("date", desc=True)
              .limit(1)
              .execute().data
        )
        if rows:
            last_run = rows[0]["date"]
    except Exception as e:
        logger.warning(f"  get_step_window: probe failed for {step_symbol}: {e}")

    if not last_run:
        last_run = str(
            (datetime.strptime(current_td, "%Y-%m-%d") - timedelta(days=fallback_days)).date()
        )
        logger.info(f"  {step_symbol} | no prior run found | fallback last_run={last_run}")

    # ── Step 2: Compute raw window ────────────────────────────────────────
    window_start        = str(
        (datetime.strptime(last_run, "%Y-%m-%d") + timedelta(days=1)).date()
    )
    window_end_trade    = current_td      # stock data anchor
    window_end_calendar = calendar_today  # news/macro can be today

    # ── Step 3: Classify current run context ─────────────────────────────
    is_weekday   = now_ist.weekday() < 5
    try:
        holiday_rows = sb.table("nse_holidays").select("date").execute().data
        holidays     = {r["date"] for r in holiday_rows}
    except Exception:
        holidays = set()
    is_holiday     = calendar_today in holidays
    is_trading_day = is_weekday and not is_holiday

    # Count missed trading days in window
    trading_days_missed = 0
    check = datetime.strptime(last_run, "%Y-%m-%d") + timedelta(days=1)
    end   = datetime.strptime(current_td, "%Y-%m-%d")
    while check < end:
        if check.weekday() < 5 and str(check.date()) not in holidays:
            trading_days_missed += 1
        check += timedelta(days=1)

    # ── Step 4: Apply guards ──────────────────────────────────────────────
    if window_start > window_end_trade:
        # Inverted — same-day re-run or weekend/holiday with no new trade date
        reason = "weekend" if now_ist.weekday() >= 5 else ("holiday" if is_holiday else "same-day re-run")
        logger.info(
            f"  {step_symbol} | window={window_end_calendar} (news/macro) "
            f"trade={window_end_trade} (signals) | "
            f"last_run={last_run} | reason={reason}"
        )
        window_start = window_end_trade  # clamp start to trade anchor

    else:
        span = (datetime.strptime(window_end_calendar, "%Y-%m-%d") -
                datetime.strptime(window_start, "%Y-%m-%d")).days + 1
        if trading_days_missed > 1:
            logger.warning(
                f"  {step_symbol} | window={window_start}→{window_end_calendar} span={span}d | "
                f"last_run={last_run} | ⚠️ {trading_days_missed} trading days missed"
            )
        else:
            logger.info(
                f"  {step_symbol} | window={window_start}→{window_end_calendar} span={span}d | "
                f"last_run={last_run} | normal"
            )

    return window_start, window_end_trade, window_end_calendar