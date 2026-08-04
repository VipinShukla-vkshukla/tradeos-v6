"""
TradeOS v7 — Intraday subsystem
================================
A SEPARATE package, deliberately.

Everything under backend/intraday/ is additive: the evening pipeline, the
morning brief and the existing control/ modules run exactly as before whether or
not this subsystem is running. It imports FROM the main framework (config,
kite_client, analysis.risk_model) and is imported BY nothing in it. Deleting
this folder returns the system to its previous behaviour with no other change.

That isolation is the point. This code holds a live broker session for hours at
a time and — at Phase 3 — can place real orders. It should be possible to stop
it, break it, or throw it away without touching the pipeline that produces the
signals.

  config       market hours, phase gates, tunables
  notifier     one action -> Telegram + Discord + dashboard, de-duplicated
  price_feed   KiteTicker websocket with a polling fallback
  gtt_manager  Phase 2.5 — broker-side stops that survive this process dying
  order_manager Phase 3 — order placement behind a kill switch and hard caps
  engine       the loop: evaluate positions and candidates, act, notify
  run          entrypoint; market-hours daemon
"""
