"""
TradeOS v6 — YAML Scheduler Runner
=====================================
Reads scheduler.yaml and runs all jobs via APScheduler.
Single process manages both pipeline and brain schedules.

Usage:
  python -m brain.scheduler_yaml              # start blocking scheduler
  python -m brain.scheduler_yaml --cron       # print equivalent crontab
  python -m brain.scheduler_yaml --list       # list all configured jobs
  python -m brain.scheduler_yaml --run <job>  # run a specific job immediately
"""

import argparse
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron      import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False


SCHEDULER_YAML = Path(__file__).parent / "scheduler.yaml"
BACKEND_ROOT   = Path(__file__).parent.parent


def load_config() -> dict:
    with open(SCHEDULER_YAML, "r") as f:
        # Skip the second YAML document (the crontab comment block)
        content = f.read().split("---")[0]
        return yaml.safe_load(content)


def make_job_runner(job_name: str, job_cfg: dict):
    """Create a callable that runs the job command in a subprocess."""
    def runner():
        cmd     = job_cfg["command"]
        cwd     = str(BACKEND_ROOT / job_cfg.get("working_dir", "."))
        env_f   = job_cfg.get("env_file")
        timeout = job_cfg.get("timeout_minutes", 60) * 60

        logger.info(f"▶ Starting job: {job_name}")
        start = datetime.now()

        env = os.environ.copy()
        if env_f:
            env_path = BACKEND_ROOT / env_f
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()

        try:
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            elapsed = (datetime.now() - start).seconds
            if result.returncode == 0:
                logger.success(f"✓ Job {job_name} completed in {elapsed}s")
            else:
                logger.error(f"✗ Job {job_name} failed (code {result.returncode}) "
                             f"after {elapsed}s:\n{result.stderr[:500]}")
                _send_failure_alert(job_name, result.stderr[:500],
                                    job_cfg.get("on_failure", "log_only"))
        except subprocess.TimeoutExpired:
            logger.error(f"✗ Job {job_name} timed out after {timeout}s")
            _send_failure_alert(job_name, "TIMEOUT", job_cfg.get("on_failure","log_only"))
        except Exception as e:
            logger.error(f"✗ Job {job_name} exception: {e}")

    return runner


def _send_failure_alert(job_name: str, error: str, alert_type: str):
    """Send Telegram alert on job failure."""
    if alert_type != "telegram_alert":
        return
    try:
        import urllib.request, json
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        msg  = f"🚨 *TradeOS Job FAILED*: `{job_name}`\n{datetime.now():%Y-%m-%d %H:%M}\nError: {error[:200]}"
        data = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
        req  = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"Failed to send Telegram failure alert: {e}")


def start_scheduler(config: Optional[dict] = None):
    if not HAS_APSCHEDULER:
        logger.error("APScheduler required: pip install apscheduler pyyaml")
        sys.exit(1)

    config    = config or load_config()
    tz        = config.get("timezone", "Asia/Kolkata")
    jobs_cfg  = config.get("jobs", {})

    scheduler = BlockingScheduler(timezone=tz)

    for job_name, job_cfg in jobs_cfg.items():
        sched     = job_cfg.get("schedule", {})
        sched_kw  = {k: v for k, v in sched.items() if k != "type"}
        runner    = make_job_runner(job_name, job_cfg)

        scheduler.add_job(
            runner,
            CronTrigger(timezone=tz, **sched_kw),
            id=job_name,
            name=job_cfg.get("description", job_name),
            misfire_grace_time=3600,
            replace_existing=True,
        )
        logger.info(f"  Scheduled: {job_name} — {sched}")

    logger.info(f"\n🕐 TradeOS scheduler started ({tz}) — {len(jobs_cfg)} jobs")
    logger.info("Press Ctrl+C to stop\n")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped")


def print_cron_table(config: Optional[dict] = None):
    config   = config or load_config()
    tz       = config.get("timezone", "Asia/Kolkata")
    jobs_cfg = config.get("jobs", {})

    print(f"\n# TradeOS crontab (timezone: {tz} = UTC+5:30)")
    print("# All times shown in UTC\n")

    IST_TO_UTC_HOUR_OFFSET = -5
    IST_TO_UTC_MIN_OFFSET  = -30

    for job_name, job_cfg in jobs_cfg.items():
        s = job_cfg.get("schedule", {})
        h = int(s.get("hour", 0))
        m = int(s.get("minute", 0))
        # Rough IST → UTC conversion
        utc_m  = (m + IST_TO_UTC_MIN_OFFSET) % 60
        h_adj  = (m + IST_TO_UTC_MIN_OFFSET) // 60
        utc_h  = (h + IST_TO_UTC_HOUR_OFFSET + h_adj) % 24
        dow    = s.get("day_of_week", "*")
        cmd    = job_cfg["command"]
        cwd    = job_cfg.get("working_dir", ".")

        print(f"# {job_cfg.get('description','')}")
        print(f"{utc_m} {utc_h} * * {dow} cd /path/to/backend/{cwd} && {cmd}")
        print()


def list_jobs(config: Optional[dict] = None):
    config   = config or load_config()
    jobs_cfg = config.get("jobs", {})
    print(f"\n{'JOB':<30} {'SCHEDULE':<30} TAGS")
    print("-" * 80)
    for job_name, job_cfg in jobs_cfg.items():
        s      = job_cfg.get("schedule", {})
        sched  = f"{s.get('day_of_week','*')} {s.get('hour','?')}:{s.get('minute','00'):02}"
        tags   = ", ".join(job_cfg.get("tags", []))
        print(f"{job_name:<30} {sched:<30} {tags}")


def run_job_now(job_name: str, config: Optional[dict] = None):
    config   = config or load_config()
    jobs_cfg = config.get("jobs", {})
    if job_name not in jobs_cfg:
        print(f"Job '{job_name}' not found. Available: {list(jobs_cfg.keys())}")
        sys.exit(1)
    runner = make_job_runner(job_name, jobs_cfg[job_name])
    runner()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradeOS YAML Scheduler")
    parser.add_argument("--cron",  action="store_true", help="Print crontab equivalent")
    parser.add_argument("--list",  action="store_true", help="List all jobs")
    parser.add_argument("--run",   type=str,  metavar="JOB", help="Run a job immediately")
    args = parser.parse_args()

    cfg = load_config()
    if args.cron:
        print_cron_table(cfg)
    elif args.list:
        list_jobs(cfg)
    elif args.run:
        run_job_now(args.run, cfg)
    else:
        start_scheduler(cfg)
