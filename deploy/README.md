# Running the intraday daemon on a free static IP

Zerodha requires an IP allowlist for order placement. Home broadband rotates its
address, so the allowlist goes stale without warning and the failure surfaces
mid-session when a stop needs acting on. A machine with a fixed address removes
the problem permanently.

You have **two allowlist slots**. Use one for home (manual orders, the dashboard,
the evening pipeline) and one for the daemon.

## Why this is less work than it looks

**The Kite token lives in Supabase, not on disk.** The dashboard on your laptop
performs the daily login and writes the token to `system_config`; the daemon on
the server reads it from there. There is no second login, no file to copy, and
no credential on the server that is not already in your `.env`.

That is also why the split works cleanly: the **daemon** goes on the server, the
**dashboard stays local**. The dashboard is a development server that wants more
memory than a free tier offers, and you are the only person who looks at it.

## Which free provider

**Oracle Cloud Always Free** — the only one that is genuinely free indefinitely
*and* offers a fixed IP near the exchange.

| | |
|---|---|
| Instance | `VM.Standard.E2.1.Micro` — 1 OCPU, 1 GB RAM |
| IP | 2 reserved public IPv4 included |
| Region | **Mumbai** or **Hyderabad** — single-digit ms to NSE |
| Cost | Always Free, not a 12-month trial |
| Catch | A card is required for identity verification. Always Free resources are not charged, but *do not* upgrade to Pay As You Go by accident. |

1 GB is ample: the daemon is Python, one websocket and a Supabase client — a few
hundred MB. It is not enough for the Next.js dashboard, which is why that stays
on your laptop.

**Why not the others.** Google's free `e2-micro` exists only in US regions —
roughly 250 ms to NSE, which is unusable for a system reacting to ticks. AWS
`t2.micro` is free for twelve months and then bills you. Fly.io charges about
$2/month for a dedicated IPv4, so it is cheap but not free.

If ARM capacity is unavailable in Mumbai (it frequently is), take the AMD micro.
It is more than enough.

## Setup

**1. Create the instance** — Oracle Cloud console, Compute → Instances → Create.
Ubuntu 22.04, shape `VM.Standard.E2.1.Micro`, region Mumbai. Download the SSH
key when offered; it is shown once.

**2. Find its IP** and add BOTH addresses at
<https://developers.kite.trade/apps> → your app → allowed IPs:

- the server's public IP (shown in the Oracle console)
- your home IP, from `tradeos ip`

**3. Deploy:**

```bash
scp -i your-key.pem -r tradeos-v6 ubuntu@<SERVER_IP>:~/
ssh -i your-key.pem ubuntu@<SERVER_IP>
cd ~/tradeos-v6/deploy && bash setup.sh
```

`setup.sh` installs Python, creates a virtualenv, installs requirements, and
registers a systemd service that starts the daemon at 09:00 IST on weekdays and
stops it after the close.

**4. Copy your `.env`** to `~/tradeos-v6/backend/.env` on the server. It needs
the same values as locally — the token is not among them, because that comes
from Supabase.

**5. Confirm:**

```bash
sudo systemctl status tradeos-intraday
journalctl -u tradeos-intraday -f
```

## After it is running

`tradeos check` on your laptop still verifies the home IP, because you place
manual entries from there and the evening pipeline runs there.

The daemon posts to the **intraday** Telegram and Discord channels, so a message
arriving proves the server is alive independently of your laptop being on.

## What still runs at home

| Where | What |
|---|---|
| Laptop | Kite login, dashboard, evening pipeline, manual entries |
| Server | Intraday daemon: engines, monitoring, exits, GTT sync |

Both read and write the same Supabase, so neither has a private view of the
book. If the server is down the daemon simply stops — positions keep their
broker-side GTT stops, which is the reason those exist.
