#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# One-time setup for the intraday daemon on a Linux server.
#
#     cd ~/tradeos-v6/deploy && bash setup.sh
#
# Installs Python, builds a virtualenv, and registers a systemd service that
# starts the daemon before the open and stops it after the close.
#
# WHY A TIMER AND NOT A PERMANENT SERVICE
# ---------------------------------------
# The daemon exits on its own at 15:40 IST because there is nothing to watch
# outside market hours. A plain always-restart service would fight that,
# restarting it every few seconds all night against a market that is closed.
# The timer starts it once a morning and lets it finish.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
VENV="$ROOT/.venv"
USER_NAME="$(whoami)"

echo "── TradeOS intraday daemon setup ──"
echo "  root: $ROOT"

if [[ ! -d "$BACKEND" ]]; then
  echo "  ✗ backend/ not found under $ROOT — copy the whole tradeos-v6 folder." >&2
  exit 1
fi

echo "── system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip tzdata

# The daemon reasons in IST throughout. Leaving the server on UTC makes every
# session-phase decision silently wrong by five and a half hours.
echo "── timezone -> Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

echo "── virtualenv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$BACKEND/requirements.txt"

if [[ ! -f "$BACKEND/.env" ]]; then
  echo
  echo "  ⚠ $BACKEND/.env is missing."
  echo "    Copy it from your laptop. The Kite ACCESS TOKEN is not needed —"
  echo "    the daemon reads that from Supabase, written by your dashboard."
  echo
fi

echo "── systemd service"
sudo tee /etc/systemd/system/tradeos-intraday.service >/dev/null <<UNIT
[Unit]
Description=TradeOS intraday monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$BACKEND
Environment=PYTHONUNBUFFERED=1
# Pull before every session, so a commit on main is running the next morning
# without a manual deploy. Prefixed with '-' so a pull failure (no network,
# local edits, detached HEAD) does NOT stop the daemon: yesterday's working code
# monitoring your positions beats today's code not running at all.
ExecStartPre=-/usr/bin/git -C $ROOT pull --ff-only
# Dependencies can change with the code. Also non-fatal, same reasoning.
ExecStartPre=-$VENV/bin/pip install --quiet -r $BACKEND/requirements.txt
# Prove the machine can still do what the daemon needs, and record it. Non-fatal
# by design — it reports, it does not gate.
ExecStartPre=-$VENV/bin/python $ROOT/deploy/validate_server.py
ExecStart=$VENV/bin/python -m intraday.run
# The daemon exits itself at the close. Restart only on genuine failure, and
# never in a tight loop — a crash during the session should recover, but a
# clean exit at 15:40 must be allowed to stand.
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/tradeos-intraday.timer >/dev/null <<'TIMER'
[Unit]
Description=Start the TradeOS intraday monitor on trading mornings

[Timer]
# 09:00 IST, weekdays. The daemon itself skips NSE holidays, which systemd
# cannot know about.
OnCalendar=Mon..Fri 09:00
Persistent=false

[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable tradeos-intraday.timer
sudo systemctl start tradeos-intraday.timer

echo
echo "── done"
echo "  Public IP to allowlist at https://developers.kite.trade/apps :"
curl -s https://api.ipify.org || echo "  (could not determine)"
echo
echo
echo "  start now:     sudo systemctl start tradeos-intraday"
echo "  watch logs:    journalctl -u tradeos-intraday -f"
echo "  check timer:   systemctl list-timers tradeos-intraday.timer"
echo "  stop trading:  cd $BACKEND && $VENV/bin/python control_panel.py --intraday off"
