"""
The swing framework — the 1-3 week book.

WHAT LIVES HERE
    ingestion/  market data, bhavcopy, events, flows, global cues
    compute/    indicators, regime, sector strength, master shortlist
    signals/    the ten rule engines, signal generation, the daily snapshot
    brain/      self-tuning: performance tracking, quant analysis, proposals
    history/    long-horizon append-only records

WHAT DELIBERATELY DOES NOT
    config.py, db/, tools/      shared infrastructure, used by both frameworks
    kite/, analysis/            shared domain — one broker client and ONE risk
                                model. Three divergent copies of the R:R model
                                once produced three answers for the same stock
                                on the same day; splitting them per framework
                                would rebuild that failure by design.
    control/                    position lifecycle, shared — both frameworks
                                open and manage positions in the same book
    alerts/                     the evening digest is swing-specific, but the
                                channel senders are shared
    intraday/                   the live-session framework, its own package

WHY THE SPLIT IS HERE AND NOT DEEPER
------------------------------------
The boundary that matters is the one where the two frameworks would otherwise
grow into each other: screening, signal generation and self-tuning are entirely
swing concepts with no intraday meaning, so they move. Risk sizing, the broker
and the position book are genuinely common, so they stay — and the moment they
were duplicated per framework, the two would start disagreeing about how much
money is at risk.
"""
