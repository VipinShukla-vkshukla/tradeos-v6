"""
TradeOS v6 — Central Configuration
All environment loading, Supabase client, shared utilities
"""
import os
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
if env_file.exists():
    load_dotenv(env_file, override=False)

# ── Paths ────────────────────────────────────────────────────
DATA      = BASE_DIR / "data"
MODELS    = BASE_DIR / "models"
LOGS      = BASE_DIR / "logs"
DATA.mkdir(exist_ok=True)
MODELS.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logger.remove()
logger.add(
    LOGS / "tradeos_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="30 days",
    level=LOG_LEVEL, format="{time:HH:mm:ss} | {level:<8} | {name}:{line} - {message}"
)
logger.add(lambda msg: print(msg, end=""), level="INFO",
           format="{time:HH:mm:ss} | {level:<8} | {message}")

# ── Core settings ─────────────────────────────────────────────
IST          = pytz.timezone("Asia/Kolkata")
DRY_RUN      = os.getenv("DRY_RUN", "False").lower() == "true"
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL") or "200000")

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

def get_strategy_config(refresh: bool = False) -> dict:
    """Load strategy_config table from Supabase."""
    global _strategy_config
    if _strategy_config is None or refresh:
        try:
            sb = get_supabase()
            rows = sb.table("strategy_config").select("*").eq("enabled", True).execute().data
            _strategy_config = {r["strategy"]: r["params"] for r in rows}
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
    f"TradeOS v6 config loaded | Capital=₹{TOTAL_CAPITAL:,.0f} | "
    f"DRY_RUN={DRY_RUN} | Phase={cfg('autonomy_phase', '0')}"
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
def get_trade_date(sb=None) -> str:
    """
    Canonical trade date for all pipeline steps.
    Probes stock_data_daily — only written after bhavcopy (step 5).
    Handles: holidays, weekends, pre-market runs, post-market runs.
    """
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
            if td != str(today_ist()):
                logger.info(f"  trade_date={td} (calendar={today_ist()})")
            return td
    except Exception as e:
        logger.warning(f"get_trade_date probe failed: {e} — using today_ist()")
    return str(today_ist())