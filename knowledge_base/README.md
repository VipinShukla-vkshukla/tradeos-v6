# TradeOS Institutional Knowledge Base

What today's trading teaches TradeOS about tomorrow's decisions — kept
separate from `docs/` because those describe what the system *is*; this
describes what it has *learned*, and that changes daily while the
architecture mostly doesn't.

## Files

- **`KNOWLEDGE_BASE.md`** — the living state. Read this before reviewing a
  new day, update it after. Four buckets: Validated Rules, Promising
  Hypotheses, Ideas Requiring More Evidence, Rules That Did Not Work. An
  item moves between buckets as evidence accumulates — it is never deleted,
  because "we tried this and it didn't hold" is itself a fact worth keeping.
- **`daily/YYYY-MM-DD.md`** — the full six-section review for one session,
  written once and never edited afterward. The permanent record; the living
  file above is a synthesis of these, not a replacement for them.
- **`REVIEW_PROMPT.md`** — the exact prompt structure to reuse for every
  future review, so the format stays comparable across sessions instead of
  drifting.

## The rule this whole folder exists to enforce

**Never judge a trading rule on one session.** A hypothesis needs multiple
days of evidence — matching the project's own `MIN_SAMPLE`/`MIN_SESSIONS`
standard elsewhere in the codebase — before it may move from "Ideas
Requiring More Evidence" into "Validated Rules." The daily review's own
"Learning for Tomorrow" section is bound by the same discipline: recommend
"Observe More Days" far more often than "Implement Tomorrow."

## Tomorrow's workflow

1. Read `KNOWLEDGE_BASE.md` — know what's already been learned before
   reading the new log, so today's log is read looking for confirmation or
   contradiction of standing hypotheses, not from a blank slate.
2. Run the review in `REVIEW_PROMPT.md` against the new day's log.
3. Save the full output to `daily/YYYY-MM-DD.md`.
4. Update `KNOWLEDGE_BASE.md`: add evidence to existing items, promote or
   retire hypotheses whose sample size just changed, add anything new.
