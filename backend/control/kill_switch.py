"""
TradeOS v7 — Kill Switch
Sets master_kill_switch=true in system_config to halt all pipeline activity
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase


def activate():
    sb = get_supabase()
    sb.table("system_config").update({"value": "true"}).eq("key", "master_kill_switch").execute()
    logger.critical("⛔ KILL SWITCH ACTIVATED — all pipeline activity halted")


def deactivate():
    sb = get_supabase()
    sb.table("system_config").update({"value": "false"}).eq("key", "master_kill_switch").execute()
    logger.info("✅ Kill switch deactivated — pipeline resumed")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "off":
        deactivate()
    else:
        activate()
