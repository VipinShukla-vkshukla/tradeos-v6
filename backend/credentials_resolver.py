"""
One place that answers "where does this credential come from".

THE INCONSISTENCY THIS REPLACES
-------------------------------
Credentials were stored three different ways with no rule:

    swing Discord webhook      system_config only
    swing Telegram token       .env / GitHub secret only
    intraday credentials       .env / GitHub secret, with a system_config fallback

All three worked, which is exactly why it went unnoticed — until you need to
rotate one and have to go looking for where it lives. Worse, two of the three
could disagree: a value in .env and a stale value in system_config both look
authoritative, and nothing reports which one won.

THE RULE, APPLIED EVERYWHERE
----------------------------
    1. environment variable   authoritative
    2. system_config          fallback
    3. default                usually empty

Environment first because that is how GitHub Actions supplies secrets and how
.env supplies them locally, and because a secret in the database is readable by
anything holding the service key — including the dashboard. system_config stays
as a fallback so a credential can be changed without a redeploy, which is
genuinely useful for a webhook URL.

WHAT DOES NOT BELONG IN system_config
-------------------------------------
Anything whose leak is unrecoverable: the Kite API secret, the Supabase service
key, the Zerodha password. Those are env-only, and resolve() refuses to look
them up in the database at all rather than trusting a convention nobody
remembers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

# Importing config is what LOADS .env into os.environ. Without it here, any
# lookup that happens before some other module imports config reads a bare
# process environment — which is how the first run of status() reported
# DEEPSEEK_API_KEY as "not set" while it sat in .env all along. The whole point
# of this module is to answer "where does this come from" correctly, so it must
# not depend on someone else having imported config first.
import config  # noqa: F401  (side effect: loads .env)

# Credentials that must NEVER be read from the database. A leak of any of these
# is not recoverable by rotating one webhook — it hands over the account.
_ENV_ONLY = {
    "KITE_API_SECRET", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY",
    "ZERODHA_PASSWORD", "ZERODHA_TOTP_SECRET", "GMAIL_APP_PASSWORD",
}

# env var -> system_config key, for the ones that legitimately live in both.
_CONFIG_ALIAS = {
    "TELEGRAM_BOT_TOKEN":           "telegram_bot_token",
    "TELEGRAM_CHAT_ID":             "telegram_chat_id",
    "DISCORD_WEBHOOK_URL":          "discord_webhook_url",
    "TELEGRAM_INTRADAY_BOT_TOKEN":  "telegram_intraday_bot_token",
    "TELEGRAM_INTRADAY_CHAT_ID":    "telegram_intraday_chat_id",
    "DISCORD_INTRADAY_WEBHOOK_URL": "discord_intraday_webhook_url",
}

_warned: set[str] = set()


def resolve(env_name: str, default: str = "", *, quiet: bool = False) -> str:
    """
    Resolve one credential: environment, then system_config, then default.

    Reports ONCE when both sources hold a different value, because two
    authoritative-looking copies that disagree is how a rotated secret appears
    to have taken effect while the old one is still being used.
    """
    env_val = (os.getenv(env_name) or "").strip()

    if env_name in _ENV_ONLY:
        if not env_val and not quiet and env_name not in _warned:
            _warned.add(env_name)
            logger.warning(f"  {env_name} is not set. It is env-only by design and "
                           f"is never read from system_config.")
        return env_val or default

    cfg_val = ""
    alias = _CONFIG_ALIAS.get(env_name)
    if alias:
        try:
            from config import cfg
            cfg_val = (cfg(alias, "") or "").strip()
        except Exception:
            cfg_val = ""

    if env_val and cfg_val and env_val != cfg_val and env_name not in _warned:
        _warned.add(env_name)
        logger.warning(
            f"  {env_name} differs between the environment and system_config."
            f"{alias!r}. The environment wins. If you just rotated this, clear "
            f"the system_config row so the two cannot drift."
        )

    return env_val or cfg_val or default


def status() -> list[dict]:
    """
    Where every known credential is coming from, without revealing any value.

    For the health dashboard: "configured" and "source" are the useful facts,
    and printing the secret itself would put it in a log.
    """
    out = []
    names = sorted(set(_CONFIG_ALIAS) | _ENV_ONLY | {
        "KITE_API_KEY", "DEEPSEEK_API_KEY", "SUPABASE_URL", "TOTAL_CAPITAL",
    })
    for n in names:
        env_val = (os.getenv(n) or "").strip()
        cfg_val = ""
        if n not in _ENV_ONLY and n in _CONFIG_ALIAS:
            try:
                from config import cfg
                cfg_val = (cfg(_CONFIG_ALIAS[n], "") or "").strip()
            except Exception:
                pass
        src = ("environment" if env_val else
               "system_config" if cfg_val else "not set")
        out.append({
            "name": n,
            "configured": bool(env_val or cfg_val),
            "source": src,
            "env_only": n in _ENV_ONLY,
            "both_and_differ": bool(env_val and cfg_val and env_val != cfg_val),
        })
    return out


if __name__ == "__main__":
    for row in status():
        flag = " ⚠ differs" if row["both_and_differ"] else ""
        lock = " (env-only)" if row["env_only"] else ""
        logger.info(f"  {row['name']:<32} {row['source']:<14}{lock}{flag}")
