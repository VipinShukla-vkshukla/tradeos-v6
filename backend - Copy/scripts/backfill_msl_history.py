"""
One-time script: Load existing 708 rows from MASTER_SHORTLIST_HISTORY Sheet tab.
Run once during Phase 0 setup.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ingestion.ingest_sheets import get_sheets_service, ingest_msl_history
from config import get_supabase
from loguru import logger

def main():
    logger.info("One-time MSL history backfill...")
    sb      = get_supabase()
    service = get_sheets_service()
    count   = ingest_msl_history(service, sb)
    logger.info(f"✓ Loaded {count} historical MSL rows")

if __name__ == "__main__":
    main()
