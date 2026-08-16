# TradeOS Findings Ledger

Append-only. Never edit a prior entry. A finding later proved wrong gets a NEW
entry saying so.

Every number carries the command that produced it and the raw output. A number
without a command behind it does not belong here. "Could not determine" is a
required section of every entry.

Format (from `docs/TRADEOS_ROADMAP.md`):

```markdown
## <date> — Stage <n> — <one-line result>

**Ran:** <exact commands>
**Raw output:** <pasted, or path to the file it was written to>
**Found:** <what the numbers say>
**Could not determine:** <what failed, what was missing, what n was too small>
**Recommends:** <action, or "no action">
**Gate:** PASS / BLOCKED / NEEDS DECISION
```

---

## 2026-08-14 — Stage 0 — environment confirmed, Supabase reachable, verify green

**Ran:**

```bash
git checkout -b diagnostic/stage-1-population
cd backend && python -m tools.verify 2>&1 | tail -25
cd backend && python -c "from config import get_supabase; print(get_supabase().table('system_config').select('key').limit(1).execute())"
```

**Raw output:**

```
  all 422 checks passed across 54 modules
```

```
00:35:37 | INFO | TradeOS v7 config loaded | Capital=₹30,000 | DRY_RUN=False | Phase=4 | Today's date=2026-08-14
data=[{'key': 'intraday_min_runway_min'}] count=None
```

**Found:** 422 of 422 checks pass across 54 modules — zero failures, so nothing
to classify as environmental versus real. Supabase reachable. `validate_config`
raised 3 questionable (not incoherent) config findings inside its own self-test:
`intraday_capital` ₹100,000 against a ₹30,000 account (harmless while intraday is
PAPER, fatal on the day it goes LIVE — swing would be left ₹−70,000 and refuse
every entry), `paper_capacity_transfer` (paper capacity is 3.3x the real account,
so paper results do not fully transfer), and `paper_max_open_positions` 10 where
capacity funds 4. **These are pre-existing and were not changed** — Stage 1 is
read-only.

**Could not determine:** nothing at this stage.

**Recommends:** no action. Note `intraday_capital` for whoever runs Stage 4 — it
is a config-only landmine that arms itself the moment intraday goes live.

**Gate 0:** PASS

---

## 2026-08-14 — Stage 1 — the three tools never disagreed; two measure the book, one measures detections

**Ran:**

```bash
cd backend && python -m tools.taken_reconciliation --days 30
cd backend && python -m tools.exit_audit --days 30            # FAILED, see below
cd backend && python -m tools.exit_audit --book INTRADAY
cd backend && python -m tools.engine_scorecard --book INTRADAY
cd backend && python -m tools.expectancy_ledger
cd backend && python -m tools.discover_engines --days 30
```

Plus four read-only queries written to the scratchpad (no source file touched):
`coverage_query.py`, `short_exclusion.py`, `phantom_outcomes.py`,
`closed_book.py`. Their full output is pasted below.

---

### A.1 — how many TAKEN detections became real positions, and what the rest did

**Ran:** `cd backend && python -m tools.taken_reconciliation --days 30`

**Raw output:**

```
  date          taken symbols  real positions     gap   ratio
  2026-07-28               12               0      12   0% became a position
  2026-07-29                1               1       0   100% became a position
  2026-07-30                7               6       1   86% became a position
  2026-07-31               15               5      10   33% became a position
  2026-08-03               10               5       5   50% became a position
  2026-08-04                8               3       5   38% became a position
  2026-08-05               10               0      10   0% became a position
  2026-08-06               10               9       1   90% became a position
  2026-08-07               15               5      10   33% became a position
  2026-08-10               17               4      13   24% became a position
  2026-08-11                4               4       0   100% became a position
  2026-08-12               30              10      20   33% became a position
  2026-08-13               13               2      11   15% became a position
  TOTAL: 152 distinct TAKEN symbol-days, 54 matched to a real position (36%)

  Of the 110 that did not open:
      39  ALLOCATOR_DECLINED — the allocator vetoed them, by design
      17  REJECTED_COST
      14  BLOCKED_STRUCTURE
      10  BELOW_CONVICTION
       5  VETOED_AI
       4  BLOCKED_PAPER_CAPACITY
       1  BLOCKED_SHORTABILITY
       1  BLOCKED_SHORTS_OFF
      19  still genuinely unexplained (no verdict row at all beyond TAKEN)
  The gap is DOMINATED BY OPERATIONAL blocks (52 of 110), not allocator vetoes
```

The tool reports the **size** of the gap but not its **quality**, which is what
A.1 actually asks. Second query, deduplicated to one observation per
(symbol, engine, trade_date), counting a key as TAKEN if **any** of its rows
through the day is TAKEN:

**Ran:** `PYTHONPATH=$PWD python <scratchpad>/phantom_outcomes.py`

**Raw output:**

```
intraday_setups rows since 2026-07-15: 6035
deduplicated (symbol, engine, trade_date): 840
of those, cost_verdict='TAKEN': 159
INTRADAY closed positions on record: 56  (54 distinct symbol-days)

  TAKEN and became a real position
    n (dedup)          53
    resolved           53
    target-hit rate    11/53 = 21%
    mean outcome_pct   -0.262%
    median outcome_pct -0.797%
    outcomes           {'STOP': 31, 'TIMEOUT': 11, 'TARGET': 11}

  TAKEN but never opened (the phantoms)
    n (dedup)          106
    resolved           106
    target-hit rate    16/106 = 15%
    mean outcome_pct   -0.420%
    median outcome_pct -0.799%
    outcomes           {'STOP': 62, 'TIMEOUT': 28, 'TARGET': 16}
```

Independent cross-check: `discover_engines` computed its own taken baseline as
**21% (n=53)** — the same n and the same rate, from different code.

**Found:** the phantoms resolved **worse** than the trades that opened —
15% target rate vs 21%, mean outcome −0.420% vs −0.262%. This is the answer A.1
was built to get: `engine_scorecard`'s negative reading is partly **measuring the
capacity gate's refusals and reporting them as the book's performance**. The
gates are declining the worse half.

---

### A.2 — which number is the real closed book

**Ran:** `cd backend && python -m tools.exit_audit --book INTRADAY`

**Raw output:**

```
  CLOSED TRADES — INTRADAY: 56 with a stop and an R on record
    winners              18   avg +1.099R
    losers               38   avg -0.590R
    capture on winners        51% of the move that was available (n=15)
    losers that were up >0.3R first   8 of 20, peaking at +0.65R
    losers worse than -1R      4 of 38, by 0.24R on average
  exit reason                 n    avg R  avg MFE R   capture
  TARGET_HIT                  4   +2.333     +1.699       91%
  TRAIL_SL_HIT                3   +0.968     +1.806       54%
  MUST_EXIT_TIME              2   +0.415     +1.267       79%
  GAVE_BACK_THE_MOVE          3   +0.357     +0.803       33%
  TIME_EXIT                   9   -0.031     +0.612      -33%
  MULTI_LEG                   5   -0.033     +1.588       19%
  SETUP_INVALIDATED          23   -0.498     +0.275     -316%
  STOP_LOSS_HIT               7   -0.698     +0.192    -1405%
```

**Ran:** `cd backend && python -m tools.engine_scorecard --book INTRADAY`

**Raw output:**

```
  INTRADAY — 170 deduplicated gate-passed trades across 13 session(s)
  bucket              n  days   win%   gross R   cost R    NET R   stop%   verdict
  GAP                15     4    20%    +0.061    0.163   -0.102   1.27%   too thin (needs 30)
  SDN                24     5    21%    +0.050    0.280   -0.229   0.79%   too thin (needs 30)
  ORB                41    11    15%    -0.242    0.188   -0.430   1.14%   NO EDGE — signal problem
  PDL                 8     3    25%    -0.052    0.429   -0.481   0.48%   too thin (needs 30)
  VWR                49    12    16%    -0.261    0.313   -0.573   0.68%   NO EDGE — signal problem
  VCE                30    10    13%    -0.317    0.268   -0.585   0.81%   NO EDGE — signal problem
  PBK                 2     1     0%    -0.682    0.347   -1.029   0.59%   too thin (needs 30)
  RNG                 1     1     0%    -1.101    0.476   -1.577   0.43%   too thin (needs 30)
  BY DIRECTION
  SHORT              24     5    21%    +0.050    0.280   -0.229   0.79%   too thin (needs 30)
  LONG              146    13    16%    -0.234    0.261   -0.495   0.88%   NO EDGE — signal problem
  BOOK-LEVEL: gross -0.194R before costs.
```

**Ran:** `PYTHONPATH=$PWD python <scratchpad>/closed_book.py`

**Raw output:**

```
closed_positions rows: 134

INTRADAY — all
  n                  56
  gross Rs           -43.08
  charges Rs         449.03
  net Rs             -492.11
  win rate           17/56 = 30%
  profit factor      0.97
  GROSS R  n=56   mean -0.047  median -0.315
  NET R    n=56   mean -0.176  median -0.449

INTRADAY — MIS only
  n=47   gross Rs -411.98   charges 427.84   net -839.82   PF 0.65
  GROSS R mean -0.122   NET R mean -0.246
INTRADAY — LONG
  n=48   gross Rs +32.47    charges 374.43   net -341.96   PF 1.03
  GROSS R mean -0.039   NET R mean -0.161
INTRADAY — SHORT
  n=8    gross Rs -75.55    charges  74.60   net -150.15   PF 0.60
  GROSS R mean -0.096   NET R mean -0.265
SWING — all
  n=78   gross Rs -9,146.84 charges 105.87   net -9,252.71 PF 0.62
  GROSS R n=8 mean +0.482   NET R n=8 mean +0.320
```

**Ran:** the dashboard's 10-Aug window, against the same table:

```bash
cd backend && PYTHONPATH="$PWD" python -c "
from config import get_supabase
rows = get_supabase().table('closed_positions').select('symbol,framework,realized_pnl,charges,r_multiple,exit_date').eq('framework','INTRADAY').lte('exit_date','2026-08-10').execute().data
..."
```

**Raw output:**

```
INTRADAY closed on/before 2026-08-10: n=40
  gross Rs +355.34   charges Rs 213.54   net Rs +141.80
  profit factor (gross) 1.55
  gross R mean +0.0180 (n=40)
```

**Found — the three tools were never in conflict.** Two of them measure the same
40 rows in different units, and the third measures a different population:

| Source | population | n | reading | same rows? |
|---|---|---|---|---|
| Dashboard, 10-Aug | `closed_positions` INTRADAY, exit ≤ 10-Aug | 40 | **+₹141.80 net, PF 1.55** | ← |
| `exit_audit`, 10-Aug | identical rows, in R | 40 | **+0.018R gross** | ← same 40 |
| `engine_scorecard` | `intraday_setups` gate-passed **detections** | 170 | −0.194R gross | different |

The dashboard's +₹141.80/PF 1.55 and `exit_audit`'s +0.018R reproduce **to the
paisa and to the fourth decimal** off one query on one table. They are one
number in two units, not two opinions. The entire "three tools disagree" premise
in `EDGE_DIAGNOSTIC.md` A.2 reduces to a single population error in
`engine_scorecard`, which scores detections — two thirds of which never became
positions, and which resolved worse (A.1).

**AUTHORITATIVE POPULATION, declared:** `closed_positions` where
`framework='INTRADAY'` — **n=56**, gross **−0.047R**, net **−0.176R**, gross
₹−43.08, charges ₹449.03, net **₹−492.11**, win rate 30%, PF 0.97. Its R column
(`r_multiple`, `position_lifecycle.py:880`) is `(realized_pnl/qty) / risk_per_share`,
`realized_pnl` is gross, and `risk_per_share` is direction-aware, so shorts are
scored correctly. **Use only this population from Stage 2 onward.**

The book has gone from +₹141.80 net on 40 trades (10-Aug) to −₹492.11 net on 56.
The 16 trades since 10-Aug lost ₹633.91 net between them.

---

### A.3 — column coverage, the Gate 1 number

**Ran:** `cd backend && python -m tools.expectancy_ledger`

**Raw output (extract):**

```
  SINCE AUTOMATION   n=65   (9 real money)  gross ₹      478  friction ₹    866  net ₹     -388
  legacy / manual    n=69   (69 real money)  gross ₹   -9,667  friction ₹  3,052  net ₹  -12,720
  56 of 134 closed trades carry planned_stop_at_entry.
  · INTRADAY / CNC   (n=9)   NET R  mean -1.110
  · INTRADAY / MIS   (n=39)  NET R  mean -0.251
  · SWING / CNC      (n=8)   NET R  mean +0.278
```

That headline is wrong in both directions — it understates the column and
overstates the problem. Measured directly against the table:

**Ran:** `PYTHONPATH=$PWD python <scratchpad>/coverage_query.py`

**Raw output:**

```
closed_positions total rows: 134

framework  product      n    stop_at_entry        charges
----------------------------------------------------------
ALL        ALL        134      64   47.8%     64   47.8%
INTRADAY   ALL         56      56  100.0%     56  100.0%
INTRADAY   CNC          9       9  100.0%      9  100.0%
INTRADAY   MIS         47      47  100.0%     47  100.0%
SWING      ALL         78       8   10.3%      8   10.3%
SWING      CNC         78       8   10.3%      8   10.3%

By exit month (is the write happening NOW?):
  2025-12  n=   1  stop    0 (  0.0%)   charges    0 (  0.0%)
  2026-01  n=  25  stop    0 (  0.0%)   charges    0 (  0.0%)
  2026-02  n=  28  stop    0 (  0.0%)   charges    0 (  0.0%)
  2026-03  n=  15  stop    0 (  0.0%)   charges    0 (  0.0%)
  2026-07  n=  17  stop   16 ( 94.1%)   charges   16 ( 94.1%)
  2026-08  n=  48  stop   48 (100.0%)   charges   48 (100.0%)

Same split, INTRADAY vs SWING, exits in the last 30 days:
  INTRADAY   n=  56  stop   56 (100.0%)   charges   56 (100.0%)
  SWING      n=   9  stop    8 ( 88.9%)   charges    8 ( 88.9%)
```

And split by whether this system produced the trade (`expectancy_ledger`'s own
attribution rule, so the two are comparable):

```
Coverage of the columns R depends on, attributed rows only
  ALL        n=65   stop  64 ( 98.5%)  charges  64 ( 98.5%)  r_multiple  64 ( 98.5%)
  INTRADAY   n=56   stop  56 (100.0%)  charges  56 (100.0%)  r_multiple  56 (100.0%)
  SWING      n=9    stop   8 ( 88.9%)  charges   8 ( 88.9%)  r_multiple   8 ( 88.9%)

Nulls by framework — are the missing rows legacy or current?
  INTRADAY   attributed      n=56   missing stop   0
  SWING      attributed      n=9    missing stop   1
  SWING      legacy/manual   n=69   missing stop  69
```

**Found:** the 70 missing rows are **69 pre-automation manual trades plus one**.
Not a random quarter of the book — a clean, dated block. Every null sits in
Dec-2025 → Mar-2026, before the write existed. `charges` coverage is identical to
`planned_stop_at_entry` coverage in every single bucket; the two columns are
written together and neither is independently broken.

The one attributed exception is identified:

```
MISSING STOP -> {'symbol': 'VIJAYA', 'framework': 'SWING', 'product': 'CNC',
 'entry_date': '2026-07-13', 'exit_date': '2026-07-23',
 'planned_stop_at_entry': None, 'charges': None, 'r_multiple': None,
 'signal_id': 4954, 'signal_date': '2026-07-10', 'source': 'manual', 'mode': 'LIVE'}
```

`source='manual'` — hand-entered against signal 4954, so no entry path wrote the
column. It is the *only* attributed row missing it, and it is not evidence of a
broken write.

**The write is live and working.** July 94.1%, August 100.0%, INTRADAY
attributed 56/56. Stage 1b's acceptance test ("a new paper entry writes both
columns, verified by query not by reading the code") is **already satisfied by
this data** — 48 of 48 August closes carry both.

---

### `tools.discover_engines --days 30` — hypotheses only, acted on: none

**Ran:** `cd backend && python -m tools.discover_engines --days 30`

**Raw output:**

```
A · REFUSED BUT RIGHT — gates that decline winners (30d)
  1000 resolved detections -> 236 distinct setups -> 99 that clear the 0.59% cost floor
  baseline: taken setups reach target 21% (n=53) — a refused slice must beat 31%
  no refused slice beats the taken baseline — the gates are declining worse setups
  than they allow, which is their job

B · MOVED BUT UNSEEN — real moves no engine detected (30d)
  scanner: 120 of 501 symbols qualify for intraday (rejected — price 16, liquidity 134, movement 0, delivery 11, flagged 13)
  880 symbol-days loaded for 40 universe names
  a 'big move' here is 2.82%+ — chosen so roughly 20% of this universe's symbol-days qualify
  background: 168 of 840 symbol-days produced a 2.82%+ move (20%) — a bucket must beat 30% to be named
  ! gap up > 1%              lift 1.6x  (32% of 95)  24 missed, avg 6.06%
      e.g. CHENNPETRO 2026-07-16 +9.4%, DATAPATTNS 2026-07-22 +11.8%, FINCABLES 2026-08-11 +14.4%
  ! gap down > 1%            lift 1.9x  (39% of 31)  11 missed, avg 4.86%
      e.g. ADANIGREEN 2026-07-24 +3.6%, CHENNPETRO 2026-08-07 +4.0%, DATAPATTNS 2026-07-23 +3.3%
  2 candidate(s) raised — read them with `tradeos learn show`
```

**Recorded as HYPOTHESES, not findings, and nothing was acted on:**

- **H1 — gap up >1% (lift 1.6x, 24 missed moves averaging 6.06%).** A GAP engine
  already exists and is the *best* gross performer in the scorecard (+0.061R,
  n=15, only 4 days). The hypothesis is not "build a gap engine" but "the
  existing GAP engine sees a narrow slice of the gaps that move."
- **H2 — gap down >1% (lift 1.9x, 11 missed, avg 4.86%).** Higher lift on a
  smaller base (n=31). Gap-down is short-side, and SDN is the only bucket with
  positive gross R. Both point the same direction, which is worth noting and
  nothing more at n=31.
- **Pass A is independent evidence that the gates are not inverted** — no refused
  slice beats the 21% taken baseline. This partially pre-answers B.3, but B.3
  asks per *blocking reason* and Pass A does not; do not treat this as B.3 done.

---

### BUGS FOUND — recorded, not fixed (Stage 1 is read-only)

**BUG-1 — `tools/expectancy_ledger.py:122` drops every closed SHORT from every R
statistic, and mislabels the exclusion as missing data.**

```python
risk_amt = (entry - float(stop)) * qty if stop not in (None, "") else 0.0   # line 122
...
"gross_r": gross / risk_amt if risk_amt > 0 else None,                      # line 134
"net_r":   (gross - modelled) / risk_amt if risk_amt > 0 else None,         # line 135
```

`entry - stop` is negative for a short (stop sits above entry), so `risk_amt <= 0`
and all three R fields become `None`. Line 208 then prints
`f"{len(scored)} of {len(rows)} closed trades carry planned_stop_at_entry"` where
`scored` is filtered on `net_r is not None` — **a computability count reported as
a column-presence count.**

Verified:

```
rows fetched: 134
planned_stop_at_entry present: 64
rows the (entry - stop) formula drops: 8
symbol       fw        prod      entry      stop  stop>entry?       pnl    exit_date   engine
COFORGE      INTRADAY  MIS     1769.90   1794.45         True    -28.80   2026-08-07      SDN
KAYNES       INTRADAY  MIS     3717.40   3743.49         True    -51.20   2026-08-10      SDN
SAPPHIRE     INTRADAY  MIS      199.70    200.88         True    -51.90   2026-08-06      SDN
SONACOMS     INTRADAY  MIS      818.00    822.97         True     49.75   2026-08-10      SDN
TATACHEM     INTRADAY  MIS      672.25    676.41         True      3.25   2026-08-13      SDN
BLUESTARCO   INTRADAY  MIS     1541.00   1556.26         True     61.00   2026-08-07      SDN
HINDCOPPER   INTRADAY  MIS     540.85    544.90         True    -15.40   2026-08-07      SDN
DEVYANI      INTRADAY  MIS      133.46    134.26         True    -42.25   2026-08-10      SDN
realized_pnl on the dropped rows: Rs -75.55
closed_positions has a 'direction' column?
  YES — sample: [{'direction': 'LONG'}]
```

All 8 are SDN, the short engine — i.e. **100% of closed shorts**. Consequences:

1. It reported coverage as 56/134 (41.8%) when the column is present on 64/134
   (47.8%) — **and Gate 1 is a coverage gate.** The one tool the spec names as
   the coverage authority is the one that miscounts it.
2. `INTRADAY / MIS (n=39)` should be n=47. Every R stat in that block is
   long-only.
3. The short book is the one bucket with positive gross R in the scorecard
   (+0.050 vs LONG −0.234), and it is **structurally invisible** in the tool
   Phase B.4 uses for the friction-vs-signal call.
4. `closed_positions.direction` **exists and is populated** — the fix has a
   column to read, and `D.risk_per_share` (already used correctly at
   `position_lifecycle.py:880`, with a comment explaining this exact trap) is the
   function to call. This is the CLAUDE.md landmine "a direction-aware function's
   correctness proves nothing about its callers", found a fifth time.

**BUG-2 — `tools/taken_reconciliation.py` sums two different units into one
sentence.** The table's `gap` column (line 154) counts distinct **symbols** per
day and totals **98**. The breakdown below it (line 170) iterates
`(sym, eng)` **pairs** and totals **110**, then prints "Of the 110 that did not
open". A symbol two engines flagged counts once in the table and twice in the
breakdown. The direction of its conclusion survives; the denominator does not —
"52 of 110 operational blocks" is not a share of the 98-symbol gap.

**BUG-3 — the spec's own command does not exist.** `EDGE_DIAGNOSTIC.md` A.2 and
`TRADEOS_ROADMAP.md` Stage 1 both give `python -m tools.exit_audit --days 30`:

```
usage: exit_audit.py [-h] [--book {SWING,INTRADAY}] [--open-only]
exit_audit.py: error: unrecognized arguments: --days 30
```

Exit code 2. Substituted `--book INTRADAY`, which reads **all history, not a
30-day window** — so the exit-reason table above is lifetime, not 30-day. The
docs should be corrected or the flag added; not done here (read-only).

---

**Could not determine:**

- **A 30-day-windowed `exit_audit`.** The flag does not exist (BUG-3). Every
  `exit_audit` number in this entry is **lifetime**, and is therefore not
  directly comparable to the 30-day `taken_reconciliation` and
  `discover_engines` windows above it. It happens not to matter much here — the
  intraday book only starts in July — but it is not the window the spec asked for.
- **Per-engine gross/cost/net R on the authoritative 56-row population.**
  `engine_scorecard` decomposes only the 170-detection population, which A.1 just
  showed is not the book. The engine table above is therefore **not** a verdict on
  any engine, and no engine verdict should be quoted from it. Producing that
  table on the real closed book is Stage 2 work and was deliberately not done.
- **Whether the dashboard itself shows +₹141.80.** I reproduced that figure from
  `closed_positions`, the table the dashboard reads. I did not open the frontend.
  The agreement is an inference from a shared source, not a UI verification.
- **Whether the 106 phantoms would really have resolved as measured.**
  `outcome_pct` is simulated against bars by `outcomes.resolve_day`. No fill,
  slippage, or queue position is modelled. They are counterfactuals, and the
  A.1 comparison inherits that.
- **The 19 TAKEN detections with no verdict row at all.** `taken_reconciliation`
  calls them "still genuinely unexplained" and I did not chase them. They are
  ~12% of the gap and their cause is unknown.
- **SWING edge, in R, at all.** n=8 with `r_multiple` (10.3% of 78 rows). The
  +0.482 gross R mean on 8 trades is not a measurement. The other 69 rows are
  pre-automation manual trades that will never produce an R.
- **The 9 INTRADAY rows recorded as CNC.** `expectancy_ledger` flags them
  (ATHERENERG, GODFRYPHLP, LALPATHLAB, M&MFIN, PATANJALI, SYRMA, VBL) and prices
  them as delivery, giving a 1.451R friction that dominates any pooled intraday
  mean. Why an intraday trade closed as delivery was not investigated.
- **Whether `r_multiple` is arithmetically right on each of the 8 shorts.** I
  verified the *formula* is direction-aware (`risk_per_share`,
  `position_lifecycle.py:880`); I did not recompute all 8 by hand.
- **`intraday_giveback_pct` / MFE questions.** `runner state recorded on 0 of 95`
  — nothing before migration 031 knows whether a runner ran, so "do runners
  capture more" remains unanswerable, as the tool itself says.

**Found (summary):**

1. The three-way disagreement is **one** population error, not three readings.
   Dashboard and `exit_audit` are the same 40 rows in different units and agree
   exactly. `engine_scorecard` scores detections, two thirds of which never
   opened.
2. Authoritative closed book: **INTRADAY n=56, gross −0.047R, net −0.176R, net
   ₹−492.11, PF 0.97, win rate 30%.**
3. The refused setups resolved **worse** than the taken ones (15% vs 21% target),
   confirmed independently by `discover_engines` Pass A. The gates are not
   inverted on this evidence.
4. `planned_stop_at_entry` coverage is **47.8% of all rows but 98.5% of rows this
   system produced**, and 100% of the intraday book. The write is live.
5. Three tool defects found, none fixed (BUG-1, BUG-2, BUG-3). BUG-1 is the
   material one: it hides the entire short book from the R analysis and it
   miscounts the very number Gate 1 turns on.

**Recommends:**

- **Proceed to Stage 2, not Stage 1b** — with the reasoning below stated
  explicitly so the decision is auditable, because the literal headline number is
  on the other side of the gate.
- **Fix BUG-1 before Stage 2 runs**, or Stage 2 will compute its friction-vs-signal
  verdict on a long-only population while the short book is the only one with
  positive gross R. This is a one-line change (`abs()`, or better,
  `D.risk_per_share(entry, stop, direction)`) plus a corrected log line, but it is
  a code change and this session is read-only. **It is the natural content of a
  short Stage 1b** — a different fix than the one Stage 1b was written to do.
- Do not act on H1/H2. They are hypotheses at n=95 and n=31.
- No engine retirements. No config changes. Nothing in this entry supports one.

**Gate 1: NEEDS DECISION.** Evidence and recommendation below; the call is Vipin's.

| Reading of `planned_stop_at_entry` coverage | Value | Side of the 60% gate |
|---|---|---|
| All 134 closed rows | **47.8%** | **BELOW** |
| Rows this system produced (n=65) | **98.5%** | ABOVE |
| INTRADAY, the book Stages 2–3 analyse (n=56) | **100.0%** | ABOVE |
| August closes (n=48) | **100.0%** | ABOVE |

The 47.8% is a **denominator artifact**: 69 of the 70 nulls are pre-automation
manual trades, and Stage 1b's own charter says "historic rows stay as they are —
backfilling an invented stop is worse than a null." Stage 1b would therefore
change nothing about the 47.8% figure. Its stated acceptance test — "a new paper
entry writes both columns, verified by query" — is already met at 48/48 for
August. The Gate 1 fear ("everything downstream rests on a quarter of the book")
does not hold: downstream analysis reads the attributed population, where
coverage is 98.5%, and the intraday book, where it is 100%.

**Recommendation: treat Gate 1 as PASS and go to Stage 2 — after BUG-1 is fixed.**

---

## 2026-08-14 — Stage 1b (scoped to tooling) — three tool defects fixed; the short book is visible, and it is not what the detection scorecard said it was

Gate 1 was called **PASS** by the operator, and the original Stage 1b (backfill
`planned_stop_at_entry`) was **skipped by their decision** — INTRADAY coverage is
100%, and the 47.8% headline is 69 pre-automation manual rows that 1b's own
charter refuses to backfill. This entry is the substituted work: fix the three
tool defects Stage 1 recorded but was read-only for.

Branch `fix/expectancy-ledger-shorts` off `main`. **Changes are confined to
`backend/tools/` plus one new check under `backend/tests/`** (which is where
CLAUDE.md directs verification code to live, instead of a throwaway script). No
engine, no gate, no live trading path, no migration, no config key.

**Ran:**

```bash
git checkout -b fix/expectancy-ledger-shorts
cd backend && python -m tools.verify --module expectancy_ledger_shorts
git stash push backend/tools/expectancy_ledger.py backend/tools/exit_audit.py
cd backend && python -m tools.verify --module expectancy_ledger_shorts   # must FAIL
git stash pop
cd backend && python -m tools.verify
cd backend && python -m tools.expectancy_ledger
cd backend && python -m tools.exit_audit --days 30 --book INTRADAY
cd backend && python -m tools.exit_audit --days 30 --book SWING
```

Plus one read-only scratchpad script, `split.py`, which **imports the fixed
`expectancy_ledger.load()`** rather than recomputing any R of its own.

---

### BUG-1 — fixed. `tools/expectancy_ledger.py`, the risk denominator

`risk_amt = (entry - float(stop)) * qty` replaced with
`D.risk_per_share(entry, float(stop), r.get("direction")) * qty`.
`intraday.direction.risk_per_share` is **imported, not reimplemented** — it is
the same function `position_lifecycle.py:880` already uses to write
`r_multiple`, and it still returns `0.0` for a stop genuinely on the wrong side
in either direction, which is the one case the old guard was right about.
`direction` added to the `SELECT`; it exists and is populated on every row
(`Counter({('LONG','SWING'): 78, ('LONG','INTRADAY'): 48, ('SHORT','INTRADAY'): 8})`).

**Raw output — before / after, same command:**

```
before:  56 of 134 closed trades carry planned_stop_at_entry.
         · INTRADAY / MIS   (n=39)  NET R  mean -0.251

after:   planned_stop_at_entry PRESENT on  64 of 134 closed trades (47.8%)  ← column coverage
         R COMPUTABLE on                   64 of 134 closed trades (47.8%)  ← usable for R stats
         · INTRADAY / MIS   (n=47)
           gross R                    n=47   mean -0.128 ±0.108   median -0.274
           friction, in R             n=47   mean +0.124 ±0.006   median +0.118
           NET R  ← the number        n=47   mean -0.252 ±0.110   median -0.382
```

64/134 is **exactly** the figure Stage 1 measured independently against the
table with `coverage_query.py`. The tool and the direct query now agree.

### BUG-2 — fixed. Two counts, separately labelled

The line printed `len(scored)` — rows whose R could be **computed** — under the
words "carry planned_stop_at_entry", which is **column presence**. They now
print as two labelled lines, and any row that has the column but cannot be
scored is **named**, because a stop on the wrong side for the direction traded
is a data defect and not a rounding loss:

```
  {n} row(s) carry a stop that R cannot be computed from — the stop is on the
  wrong side of entry for the direction recorded, or sits exactly at it: {symbols}
```

On today's book the two counts are both 64 and that warning does not fire. It
would have named all 8 shorts before the BUG-1 fix.

### BUG-3 — fixed. `tools/exit_audit.py --days N`

Added, windowing on `exit_date`, stated on the header line
(`(last 30d, exits since 2026-07-15)` vs `(ALL history)`) so no future entry can
repeat Stage 1's mismatch of a lifetime table printed beside 30-day tables.
`check_open_stops` is deliberately **not** windowed — an open position has no
exit date, and a 30-day flag must not hide a breached stop on a trade opened 40
days ago. Rows with no `exit_date` are excluded and counted out loud.

**Raw output — the flag genuinely filters:**

```
$ python -m tools.exit_audit --days 30 --book INTRADAY
  window: exit_date >= 2026-07-15 (30d) — 56 of 56 closed row(s)
  CLOSED TRADES — INTRADAY (last 30d, exits since 2026-07-15): 56 ...

$ python -m tools.exit_audit --days 30 --book SWING
  window: exit_date >= 2026-07-15 (30d) — 9 of 78 closed row(s)
  CLOSED TRADES — SWING (last 30d, exits since 2026-07-15): 8 ...
  BROKER_EXIT                 8   +0.482     +0.728       63%
```

**This resolves a Stage 1 "could not determine" in the harmless direction, and
proves it rather than assuming it.** Stage 1 wrote "it happens not to matter
much here — the intraday book only starts in July." That is now measured:
INTRADAY is **56 of 56**, so every `exit_audit` INTRADAY figure in the Stage 1
entry is unchanged and was accidentally correct. SWING is **9 of 78**, which is
what demonstrates the flag is not a no-op — and it independently reproduces
Stage 1's "69 pre-automation rows" from a different code path.

---

### The test, and it FAILS FIRST

`backend/tests/test_expectancy_ledger_shorts.py`, 10 checks, registered in
`tools/verify.py::MODULES`. Pinned to **DEVYANI**, a real closed SDN short —
the same trade CLAUDE.md's `alloc_edge_absolute_floor` landmine already names.
Hand computation, from entry/stop/exit/direction only:

```
  direction SHORT, entry 133.46, stop 134.26, exit 134.11, qty 65
  risk/share = 134.26 - 133.46       = 0.80
  risk       = 0.80 x 65             = 52.00
  gross P&L  = (133.46 - 134.11)x65  = -42.25    (= realized_pnl, exactly)
  gross R    = -42.25 / 52.00        = -0.8125
```

The tool now returns `risk_amt 52.00`, `gross_r -0.8125` — and
`round(gross_r,3) == -0.813`, the `r_multiple` the lifecycle stored from the
same `D.risk_per_share`. Two independent computations of one number agree.

**Demonstrated failing before trusting it to pass**, per the house rule —
`git stash` on the two tool files, same command:

```
  ✗  expectancy ledger scores shorts  (7/10 failed)
         the short was dropped from every R statistic — BUG-1 has regressed
         TypeError: type NoneType doesn't define __round__ method
       column presence and computability are reported separately
       exit_audit accepts --days and windows on exit_date
         TypeError: audit_closed() got an unexpected keyword argument 'days'
  7 of 10 checks FAILED.
```

The 3 that pass in both states are the **invariants**, and they are supposed to:
the long path must be bit-identical at sign=+1, a missing direction must still
read as LONG, and `(entry - stop) * qty < 0` is a statement about the pinned row
that must hold whatever the tool does with it.

**Full suite after:** `all 432 checks passed across 55 modules` (was 422/54).

---

### The corrected INTRADAY numbers, all 8 shorts included

```
INTRADAY — every closed row, R now computable on both directions
  ALL        n=56  gross -0.052 ±0.136  cost +0.338  NET -0.390 ±0.160  median net -0.412  | ₹ gross -43  friction 681  net -724  net-win 15/56
  LONG       n=48  gross -0.043 ±0.152  cost +0.369  NET -0.412 ±0.180  median net -0.387  | ₹ gross +32  friction 616  net -583  net-win 13/48
  SHORT      n=8   gross -0.109 ±0.302  cost +0.148  NET -0.256 ±0.306  median net -0.478  | ₹ gross -76  friction  65  net -140  net-win  2/8

Same split, MIS only (the 9 CNC rows carry a 1.45R DP fee and swamp a pooled mean)
  MIS ALL    n=47  gross -0.128 ±0.108  cost +0.124  NET -0.252 ±0.110  median net -0.382
  MIS LONG   n=39  gross -0.132 ±0.116  cost +0.120  NET -0.251 ±0.119  median net -0.371
  MIS SHORT  n=8   gross -0.109 ±0.302  cost +0.148  NET -0.256 ±0.306  median net -0.478

The 8 shorts, individually (all SDN):
  BLUESTARCO  entry  1541.00  stop  1556.26  risk ₹45.78  gross +1.332  cost +0.106  net +1.226
  COFORGE     entry  1769.90  stop  1794.45  risk ₹73.65  gross -0.391  cost +0.077  net -0.468
  DEVYANI     entry   133.46  stop   134.26  risk ₹52.00  gross -0.813  cost +0.178  net -0.990
  HINDCOPPER  entry   540.85  stop   544.90  risk ₹44.55  gross -0.346  cost +0.142  net -0.488
  KAYNES      entry  3717.40  stop  3743.49  risk ₹52.18  gross -0.981  cost +0.152  net -1.133
  SAPPHIRE    entry   199.70  stop   200.88  risk ₹70.80  gross -0.733  cost +0.180  net -0.913
  SONACOMS    entry   818.00  stop   822.97  risk ₹49.70  gross +1.001  cost +0.174  net +0.827
  TATACHEM    entry   672.25  stop   676.41  risk ₹54.08  gross +0.060  cost +0.172  net -0.112
```

**Found — and this corrects the reason Stage 1 gave for the fix, not the fix.**

1. **The short book's gross R is NEGATIVE on the real closed book: −0.109R
   (n=8).** Stage 1 recommended fixing BUG-1 partly because "the short book is
   the one bucket with positive gross R (+0.050)". That +0.050 came from
   `engine_scorecard`'s **24 SDN detections**, not the **8 SDN closed
   positions** — the identical population error Stage 1 itself identified in
   that tool, reaching one stage further than it was caught. The fix was still
   required; the argument for it was measuring detections. **Recorded here so
   Stage 2 does not inherit "shorts are the profitable side" as a premise.**
2. **On NET R the shorts are the better half anyway, for a different reason.**
   SHORT net −0.256 against LONG net −0.412 pooled — but that gap is almost
   entirely the 9 CNC-priced longs. MIS-only the two are indistinguishable
   (−0.256 vs −0.251). Short friction is genuinely lower (+0.148R vs +0.369R
   pooled) because every short is MIS and pays no DP fee. **n=8. This is not a
   measurement of short edge and must not be quoted as one.**
3. **Two SDN winners carry the whole distribution** — BLUESTARCO +1.332R and
   SONACOMS +1.001R against six losers. The ±0.302 standard error on gross R is
   larger than the mean itself.
4. **The ledger's INTRADAY gross R (−0.052, n=56) and Stage 1's authoritative
   −0.047 are not a discrepancy, and Stage 2 must not chase one.** They use
   different denominators by design: the ledger divides by
   `planned_stop_at_entry × actual_qty`; `r_multiple` divides by
   `pos["planned_stop"]` — the CURRENT, possibly trailed stop at close — times
   `qty + booked_qty`. SAPPHIRE is the visible case: ledger −0.733, stored
   −0.633, a partial book. Both are correct answers to different questions.

**Could not determine:**

- **Whether shorts have edge.** n=8, ±0.302 SE, two trades carrying the mean.
  No engine verdict follows from this table and none is offered.
- **Whether the friction figures are right in rupees.** The ledger recomputes at
  today's rates; the reconciliation on record is `2026-08-04: -0.01% across 4
  round trip(s)`, and the spec asks for ≥20. The RATE TABLE is validated; the
  SAMPLE is not. Every net R above inherits that.
- **Why 9 INTRADAY rows are recorded as CNC.** Still not investigated — carried
  forward unchanged from Stage 1. They cost 1.451R of friction each and they are
  what makes pooled LONG (−0.412) differ from MIS LONG (−0.251).
- **Whether `--days 30` is the right window for anything.** The flag now exists
  and is honest about what it did; whether 30 days is a useful window on a book
  that is 13 sessions old is a separate question, unasked here.
- **The two spec documents were NOT edited.** BUG-3's Stage 1 note offered
  "corrected or the flag added"; the flag was added, so
  `docs/EDGE_DIAGNOSTIC.md` A.2 and `docs/TRADEOS_ROADMAP.md` Stage 1 now run as
  written. Their text was not otherwise reviewed.
- **Nothing was re-run to regenerate Stage 1's tables.** Only the INTRADAY
  `exit_audit` window was checked (56 of 56, unchanged). `engine_scorecard`,
  `taken_reconciliation` and `discover_engines` were not re-run and their Stage 1
  numbers stand as recorded — including BUG-2's separate defect in
  `taken_reconciliation`, which is **not** the BUG-2 fixed here.

**Recommends:**

- **Stage 2 may now run.** Its friction-versus-signal call reads the whole
  population, both directions, and the coverage line it quotes is the column,
  not the computability.
- **Do not carry "SDN is the profitable bucket" into Stage 2.** On the closed
  book it is not, and the number that said so was scoring detections.
- No engine retirements, no config changes, no gate changes. Nothing here
  supports one, and nothing here touched one.

**Gate:** PASS — three defects fixed, each demonstrated failing first, full
suite green at 432/432.

---

## 2026-08-14 — Stage 2 (B.2, B.3, B.5, B.6, C.1–C.4) — no gate is inverted at the 2-SE bar, but conviction is noise pointing the wrong way, and the swing ranking layer does not order outcomes at n=1386

Branch `diagnostic/edge-decomposition` off `main`. **READ-ONLY — no source file
was modified.** Per the session brief, **B.1 is not re-derived** and **B.4's
friction path was not run**: Stage 1b already established gross R is negative on
both directions MIS-only (LONG −0.132 n=39, SHORT −0.109 n=8), so this is a
signal problem, and that entry stands as the citation.

**Ran:**

```bash
git checkout -b diagnostic/edge-decomposition main
cd backend && python -m tools.exit_ladder_replay --min-r 0.5
```

Plus seven read-only scratchpad scripts, no source file touched: `schema_probe.py`,
`pop_probe.py`, `caveats.py`, `b3_counterfactual.py`, `b2_b6.py`, `b5_c.py`,
`c3_c4b.py`. Each imports `tools.expectancy_ledger.load()` for closed-book R
rather than recomputing it — the same function Stage 1b fixed and pinned a test
to. Raw output pasted below.

---

### POPULATIONS — declared once, never mixed inside a comparison

Stage 1 and Stage 1b were each derailed by a population error. Every table in
this entry is headed with which of these three it reads.

| tag | table | n | what a row is |
|---|---|---|---|
| **[CLOSED]** | `closed_positions` | 56 INTRADAY / 78 SWING | a real trade that opened and closed |
| **[DET-INTRA]** | `intraday_setups` | 6035 raw / 890 dedup / 13 sessions | an intraday **detection** and its counterfactual |
| **[PLANS-SW]** | `signal_output_daily` | 1386 with a resolved return / 35 dates | a swing **plan** that triggered |

[DET-INTRA] dedup key is `(symbol, strategy, trade_date)` → **890**. Keyed on
`meta.sub_engine` instead it is 941; Stage 1 reported 840 on its own keying. The
three differ because `meta.sub_engine` is null on 631 raw rows. `strategy` is
used here because a null bucket is not an engine.

---

### PREFLIGHT — two corrections that govern how every table below may be read

**PRE-1 — `cost_pct` is written only on rows that reached the cost gate. The
spec's own B.3 recipe is therefore biased in favour of the gates.**

`EDGE_DIAGNOSTIC.md` B.3 says "report hit rate and mean `outcome_pct`". But
`outcomes.py:182` writes `outcome_pct = gain_pct − cost_pct`, and `cost_pct` is
**zero on 100% of rows for ten of the fourteen verdicts**:

```
  cost_verdict                 rows  cost_pct=0   share  mean cost_pct
  TAKEN                        1048           0    0.0%         0.2062
  REJECTED_COST                 804           0    0.0%         0.2062
  ALLOCATOR_DECLINED            745           0    0.0%         0.2063
  BLOCKED_PAPER_CAPACITY         24           0    0.0%         0.2063
  BLOCKED_SHORTS_MARKET         707         707  100.0%         0.0000
  BELOW_CONVICTION              626         626  100.0%         0.0000
  VETOED_AI                     587         587  100.0%         0.0000
  BLOCKED_STRUCTURE             451         451  100.0%         0.0000
  BLOCKED_SHORTABILITY          402         402  100.0%         0.0000
  BLOCKED_SHORTS_OFF            397         397  100.0%         0.0000
  BLOCKED_CROSS_FRAMEWORK        76          76  100.0%         0.0000
  BLOCKED_REENTRY                73          73  100.0%         0.0000
  BLOCKED_EVENT                  56          56  100.0%         0.0000
  SHADOW                         39          39  100.0%         0.0000
```

So a bucket blocked before the cost gate has `net = gross`, while TAKEN carries a
real +0.264R of modelled friction. Comparing mean `outcome_pct` across that line
**hands every early-firing gate a free round trip — about 0.21% of price, ≈0.26R
at typical `risk_pct`** — and would manufacture an inversion out of nothing.
Pooled B.3a: TAKEN mean `outcome_pct` −0.336 vs BELOW_CONVICTION +0.005 looks
like a +0.34pp gap; on **gross R**, the only quantity both sides actually carry,
it is +0.19R. **Every B.3 comparison below is made on gross R.** This is the
CLAUDE.md landmine "a gate and the thing it gates must be the SAME QUANTITY",
found in the diagnostic spec rather than in the code.

**PRE-2 — `regime_at_detection` exists on 2 of 13 sessions. Every regime
conclusion is confounded with date.**

```
  trade_date       rows  regime set   share   values
  2026-07-28        236           0    0.0%   {}
  ... (28-Jul through 10-Aug all 0.0%) ...
  2026-08-10        921           0    0.0%   {}
  2026-08-11        275          19    6.9%   {'RISK_OFF': 15, 'NEUTRAL': 4}
  2026-08-12       1527        1527  100.0%   {'CAUTION': 358, 'NEUTRAL': 342, 'RISK_OFF': 827}
  2026-08-13       1167        1167  100.0%   {'NEUTRAL': 782, 'CAUTION': 240, 'RISK_OFF': 145}
```

Every `NULL` is 28-Jul → 11-Aug; every labelled row is 11–13 Aug. "NEUTRAL
underperforms unlabelled" is therefore indistinguishable from "12–13 Aug were bad
days". **The regime axis of B.6 cannot be answered and is reported as such.**

---

### B.2 — which engines earn their existence

**B.2a — POPULATION [CLOSED]: `closed_positions`, `framework='INTRADAY'`, n=56.**

```
  engine        n  R-able   gross R     ±SE   cost R     net R     ±SE    win   gross Rs     net Rs  verdict
  ORB          11      11    +0.188   0.216   +0.089    +0.100   0.216   4/11       +168        +46  INSUFFICIENT (n<20)
  GAP           9       9    +0.075   0.320   +0.093    -0.018   0.325   4/9          -3        -75  INSUFFICIENT (n<20)
  VWR           9       9    -0.200   0.299   +0.180    -0.380   0.267   1/9         -61       -183  INSUFFICIENT (n<20)
  SDN           8       8    -0.109   0.302   +0.148    -0.256   0.306   2/8         -76       -140  INSUFFICIENT (n<20)
  PDL           7       7    +0.327   0.784   +1.626    -1.299   0.963   2/7        +206        +25  INSUFFICIENT (n<20)
  VCE           7       7    -0.579   0.124   +0.288    -0.868   0.256   0/7        -236       -328  INSUFFICIENT (n<20)
  PBK           5       5    -0.251   0.365   +0.179    -0.430   0.365   2/5         -41        -68  INSUFFICIENT (n<20)
  ------------------------------------------------------------------------------------------------
  ALL          56      56    -0.052   0.136   +0.338    -0.390   0.160  15/56        -43       -724  NO EDGE — gross<0
  ALL/MIS      47      47    -0.128   0.108   +0.124    -0.252   0.110  12/47       -412       -843  NO EDGE — gross<0
```

**The authoritative closed book cannot rank a single engine.** The largest bucket
is ORB at **n=11**; the roadmap's retirement bar is 30. Every ±SE is larger than
the mean it decorates. RNG has **zero** closed positions and does not appear.

**PDL's +1.626R cost is not a cost-model result — all 7 PDL closes are CNC.** This
resolves half of a Stage 1 "could not determine" (why 9 INTRADAY rows are recorded
as CNC):

```
INTRADAY rows NOT MIS: 9
  LALPATHLAB   PDL    CNC  cost_r  +2.133  gross_r  -0.321  net_r  -2.454
  ATHERENERG   PDL    CNC  cost_r  +0.712  gross_r  +1.931  net_r  +1.219
  ATHERENERG   PDL    CNC  cost_r  +1.578  gross_r  -0.796  net_r  -2.374
  SYRMA        VCE    CNC  cost_r  +1.183  gross_r  -1.143  net_r  -2.326
  PATANJALI    PDL    CNC  cost_r  +1.114  gross_r  +4.371  net_r  +3.257
  GODFRYPHLP   VWR    CNC  cost_r  +0.498  gross_r  +1.924  net_r  +1.425
  M&MFIN       PDL    CNC  cost_r  +2.592  gross_r  -1.313  net_r  -3.905
  M&MFIN       PDL    CNC  cost_r  +1.741  gross_r  -0.805  net_r  -2.546
  VBL          PDL    CNC  cost_r  +1.511  gross_r  -0.776  net_r  -2.287
```

7 PDL, 1 VCE, 1 VWR. **PDL's closed book is 7 of 7 CNC** — the engine has never
been measured at intraday friction, and its net R of −1.299 is a delivery-charge
artifact, not an engine verdict. Do not carry −1.299 forward as PDL's number.

**B.2b — POPULATION [DET-INTRA]: `cost_verdict='TAKEN'`, deduplicated, n=170.** A
different population. Roughly a third became positions (Stage 1: 53 of 159 on its
keying).

```
  VWR                        n=  49 res=  49  target 16.3%  grossR -0.310±0.163  costR +0.314  netR -0.624±0.164   NO EDGE — gross<0
  ORB                        n=  41 res=  41  target 14.6%  grossR -0.156±0.169  costR +0.187  netR -0.344±0.168   NO EDGE — gross<0
  VCE                        n=  30 res=  30  target 20.0%  grossR -0.201±0.264  costR +0.268  netR -0.470±0.265   NO EDGE — gross<0
  SDN                        n=  24 res=  24  target 20.8%  grossR +0.017±0.238  costR +0.284  netR -0.267±0.241   gross>0, net<0 — friction
  GAP                        n=  15 res=  15  target 20.0%  grossR +0.063±0.328  costR +0.163  netR -0.101±0.329   INSUFFICIENT (n<20)
  PDL                        n=   8 res=   8  target 25.0%  grossR -0.064±0.615  costR +0.427  netR -0.491±0.609   INSUFFICIENT (n<20)
  PBK                        n=   2 res=   2  target  0.0%  grossR -1.000±0.000  costR +0.347  netR -1.347±0.002   INSUFFICIENT (n<20)
  RNG                        n=   1 res=   1  target  0.0%  grossR -1.000±0.000  costR +0.448  netR -1.448±0.000   INSUFFICIENT (n<20)
  ------------------------------------------------------------------------------------------------
  ALL TAKEN                  n= 170 res= 170  target 17.6%  grossR -0.175±0.093  costR +0.264  netR -0.440±0.094   NO EDGE — gross<0
```

**The two populations disagree per engine and both are within noise.** ORB is
+0.188 gross on [CLOSED] and −0.156 on [DET-INTRA]; PDL is +0.327 and −0.064.
Only three [DET-INTRA] buckets clear n=20 (VWR 49, ORB 41, VCE 30) and all three
are negative gross. **This is not a retirement list** — it is the reason Stage 3
exists.

---

### B.3 — THE COUNTERFACTUAL: are the gates backwards? *(the priority)*

**POPULATION [DET-INTRA] throughout. 890 dedup keys: 170 TAKEN, 720 never-TAKEN.**

Construction, stated because it is load-bearing: a TAKEN key is represented by its
**first TAKEN row** — the trade that would have opened. A never-taken key is
credited to **every** blocking reason it carried, at the row where that reason
first fired. Keys carrying more than one reason appear in more than one bucket:

```
reasons carried per never-taken key: {1: 347, 2: 161, 3: 106, 4: 58, 5: 34, 6: 14}
```

`dR = ... SE` is the gap in **gross R** divided by the pooled standard error of
the two means. It is not a p-value; it exists so a +0.19R gap on n=170 vs n=270
cannot be read as a verdict.

**B.3a — pooled over both directions**

```
  TAKEN  [baseline]          n= 170  res= 170  target  30/170  = 17.6%  net%  -0.336  med  -0.798  grossR -0.175±0.093  netR -0.440
  --------------------------------------------------------------------------
  BELOW_CONVICTION           n= 270  res= 267  target  54/267  = 20.2%  net%  +0.005  med  -0.211  grossR +0.014±0.090  netR +0.014  |  tgt +2.6%   dR +0.190 = +1.5 SE
  REJECTED_COST              n= 252  res= 246  target  36/246  = 14.6%  net%  -0.307  med  -0.471  grossR -0.220±0.105  netR -0.913  |  tgt -3.0%   dR -0.044 = -0.3 SE
  BLOCKED_STRUCTURE          n= 237  res= 235  target  47/235  = 20.0%  net%  -0.063  med  -0.248  grossR -0.118±0.089  netR -0.118  |  tgt +2.4%   dR +0.057 = +0.4 SE
  VETOED_AI                  n= 227  res= 224  target  40/224  = 17.9%  net%  -0.010  med  -0.198  grossR +0.028±0.103  netR +0.028  |  tgt +0.2%   dR +0.203 = +1.5 SE
  BLOCKED_SHORTS_MARKET      n= 174  res= 174  target  36/174  = 20.7%  net%  +0.075  med  -0.191  grossR +0.105±0.116  netR +0.105  |  tgt +3.0%   dR +0.280 = +1.9 SE
  BLOCKED_SHORTABILITY       n= 149  res= 149  target  11/149  =  7.4%  net%  -0.071  med  -0.177  grossR -0.291±0.099  netR -0.291  |  tgt -10.3%  dR -0.116 = -0.8 SE
  BLOCKED_SHORTS_OFF         n=  61  res=  61  target   8/61   = 13.1%  net%  -0.107  med  -0.233  grossR -0.424±0.145  netR -0.424  |  tgt -4.5%   dR -0.248 = -1.4 SE
  BLOCKED_EVENT              n=  30  res=  29  target   2/29   =  6.9%  net%  -0.350  med  -0.494  grossR -0.651±0.168  netR -0.651  |  tgt -10.8%  dR -0.476 = -2.5 SE
  SHADOW                     n=  26  res=  26  target   5/26   = 19.2%  net%  +0.032  med  -0.379  grossR +0.094±0.311  netR +0.094  |  tgt +1.6%   dR +0.270 = +0.8 SE
  BLOCKED_CROSS_FRAMEWORK    n=  24  res=  24  target   4/24   = 16.7%  net%  -0.205  med  -0.222  grossR -0.340±0.213  netR -0.340  |  tgt -1.0%   dR -0.165 = -0.7 SE
  BLOCKED_REENTRY            n=  23  res=  23  target   4/23   = 17.4%  net%  -0.228  med  -0.370  grossR -0.485±0.221  netR -0.485  |  tgt -0.3%   dR -0.309 = -1.3 SE
```

**Exactly one bucket separates from TAKEN by more than 2 SE, and it separates in
the direction that says the gate is working: `BLOCKED_EVENT` at −2.5 SE** (6.9%
target vs 17.6%, −0.476R). Nothing is inverted at that bar.

**B.3b — direction-matched.** Three reasons are structurally short-only
(`BLOCKED_SHORTS_MARKET`, `BLOCKED_SHORTABILITY`, `BLOCKED_SHORTS_OFF` are 100%
SHORT) while TAKEN is 85.9% LONG. Comparing across that line is two questions
wearing one number, so it is done separately:

```
LONG only
  TAKEN LONG [baseline]      n= 146  res= 146  target  25/146  = 17.1%  grossR -0.207±0.101
  BELOW_CONVICTION           n= 190  res= 188  target  30/188  = 16.0%  grossR -0.145±0.098  |  tgt -1.2%   dR +0.062 = +0.4 SE
  REJECTED_COST              n= 152  res= 150  target  16/150  = 10.7%  grossR -0.480±0.102  |  tgt -6.5%   dR -0.273 = -1.9 SE
  BLOCKED_STRUCTURE          n= 139  res= 139  target  21/139  = 15.1%  grossR -0.175±0.111  |  tgt -2.0%   dR +0.032 = +0.2 SE
  VETOED_AI                  n=  94  res=  93  target  17/93   = 18.3%  grossR +0.110±0.157  |  tgt +1.2%   dR +0.317 = +1.7 SE
  BLOCKED_EVENT              n=  25  res=  24  target   2/24   =  8.3%  grossR -0.579±0.200  |  tgt -8.8%   dR -0.371 = -1.7 SE
  SHADOW                     n=  26  res=  26  target   5/26   = 19.2%  grossR +0.094±0.311  |  tgt +2.1%   dR +0.301 = +0.9 SE
  BLOCKED_CROSS_FRAMEWORK    n=   8   << n<20, INSUFFICIENT
  BLOCKED_REENTRY            n=  17   << n<20, INSUFFICIENT

SHORT only
  TAKEN SHORT [baseline]     n=  24  res=  24  target   5/24   = 20.8%  grossR +0.017±0.238
  BELOW_CONVICTION           n=  80  res=  79  target  24/79   = 30.4%  grossR +0.393±0.192  |  tgt +9.5%   dR +0.376 = +1.2 SE
  REJECTED_COST              n= 100  res=  96  target  20/96   = 20.8%  grossR +0.186±0.210  |  tgt +0.0%   dR +0.169 = +0.5 SE
  BLOCKED_STRUCTURE          n=  98  res=  96  target  26/96   = 27.1%  grossR -0.037±0.145  |  tgt +6.2%   dR -0.054 = -0.2 SE
  VETOED_AI                  n= 133  res= 131  target  23/131  = 17.6%  grossR -0.030±0.136  |  tgt -3.3%   dR -0.047 = -0.2 SE
  BLOCKED_SHORTS_MARKET      n= 174  res= 174  target  36/174  = 20.7%  grossR +0.105±0.116  |  tgt -0.1%   dR +0.088 = +0.3 SE
  BLOCKED_SHORTABILITY       n= 149  res= 149  target  11/149  =  7.4%  grossR -0.291±0.099  |  tgt -13.5%  dR -0.308 = -1.2 SE
  BLOCKED_SHORTS_OFF         n=  61  res=  61  target   8/61   = 13.1%  grossR -0.424±0.145  |  tgt -7.7%   dR -0.441 = -1.6 SE
  BLOCKED_EVENT n=5 / BLOCKED_CROSS_FRAMEWORK n=16 / BLOCKED_REENTRY n=6   << all n<20, INSUFFICIENT
```

Direction-matching **destroys the two largest apparent inversions**.
`BLOCKED_SHORTS_MARKET` goes from +1.9 SE pooled to **+0.3 SE** against a short
baseline — its pooled advantage was almost entirely the difference between
shorting and going long, not the gate. `BELOW_CONVICTION` LONG falls to +0.4 SE.
What survives is `BELOW_CONVICTION` **on shorts**: 30.4% target vs 20.8%, +0.376R
— but at **+1.2 SE on n=80 against a 24-row baseline.** Directional, not a
verdict, and B.5 below is the same finding from the other side.

**B.3e — the gate B.3a structurally cannot see.** `ALLOCATOR_DECLINED` (745 raw
rows, the second-largest verdict) and `BLOCKED_PAPER_CAPACITY` are **absent from
B.3a entirely** — not because they are rare, but because **63 of 63 and 11 of 11
of their dedup keys also carry a TAKEN row**, so the never-taken filter absorbs
them into the baseline. The TAKEN bucket above is therefore not "trades the system
made"; it is "setups that cleared the **cost** gate". Split from inside
[DET-INTRA] only, with no join to `closed_positions`:

```
  TAKEN, allocator never declined n= 107  res= 107  target  19/107  = 17.8%  grossR -0.202±0.117
  TAKEN, but ALSO declined        n=  63  res=  63  target  11/63   = 17.5%  grossR -0.130±0.154  |  tgt -0.3%  dR +0.072 = +0.4 SE
  TAKEN, but ALSO cap-blocked     n=  11   << n<20, INSUFFICIENT
```

**The allocator is neither selecting nor harming: +0.4 SE.** On this evidence it
costs opportunity and returns nothing measurable — the spec's "TAKEN ≈ BLOCKED,
gates are noise" reading. n=63.

**B.3d — robustness, and a trap worth recording.** The same comparison on raw
undeduplicated rows flips several buckets past 3 SE:

```
  TAKEN [baseline]           n=1048  res=1048  target 143/1048 = 13.6%  grossR -0.160±0.035
  BELOW_CONVICTION           n= 626  res= 623  target 124/623  = 19.9%  grossR +0.118±0.067  |  tgt +6.3%   dR +0.278 = +3.7 SE
  BLOCKED_SHORTS_MARKET      n= 707  res= 635  target  98/635  = 15.4%  grossR +0.052±0.063  |  tgt +1.8%   dR +0.213 = +3.0 SE
  BLOCKED_SHORTS_OFF         n= 397  res= 397  target  90/397  = 22.7%  grossR +0.102±0.083  |  tgt +9.0%   dR +0.262 = +2.9 SE
  ALLOCATOR_DECLINED         n= 745  res= 745  target  82/745  = 11.0%  grossR -0.138±0.040  |  tgt -2.6%   dR +0.023 = +0.4 SE
  REJECTED_COST              n= 804  res= 732  target  97/732  = 13.3%  grossR -0.246±0.056  |  tgt -0.4%   dR -0.085 = -1.3 SE
  BLOCKED_STRUCTURE          n= 451  res= 448  target  84/448  = 18.8%  grossR -0.094±0.064  |  tgt +5.1%   dR +0.066 = +0.9 SE
  VETOED_AI                  n= 587  res= 571  target  87/571  = 15.2%  grossR -0.111±0.060  |  tgt +1.6%   dR +0.050 = +0.7 SE
  BLOCKED_SHORTABILITY       n= 402  res= 402  target  46/402  = 11.4%  grossR -0.048±0.069  |  tgt -2.2%   dR +0.112 = +1.4 SE
  BLOCKED_REENTRY            n=  73  res=  73  target   8/73   = 11.0%  grossR -0.519±0.108  |  tgt -2.7%   dR -0.359 = -3.1 SE
  BLOCKED_EVENT              n=  56  res=  55  target   4/55   =  7.3%  grossR -0.562±0.126  |  tgt -6.4%   dR -0.402 = -3.1 SE
  BLOCKED_PAPER_CAPACITY     n=  24  res=  24  target   4/24   = 16.7%  grossR -0.442±0.187  |  tgt +3.0%   dR -0.282 = -1.5 SE
```

**Those SEs are fake.** One symbol re-detected forty times through a session
counts forty times, so the raw view pseudo-replicates and understates the standard
error by roughly √(6035/890) ≈ 2.6×. It manufactures significance the
deduplicated view does not support. Recorded because a naive
`GROUP BY cost_verdict` — which is literally what the spec asks for — lands
exactly here and would have concluded that three gates are inverted.

**B.3 answer: no gate is inverted at the 2-SE bar.** `BLOCKED_EVENT` is measurably
correct (−2.5 SE). `ALLOCATOR_DECLINED` is measurably neutral (+0.4 SE, n=63).
`BELOW_CONVICTION` is the one candidate for inversion and it is +1.5 SE pooled /
+1.2 SE on shorts — **suggestive, not established.** This is consistent with, and
sharper than, `discover_engines` Pass A in Stage 1, which asked the aggregate
question and found no refused slice beating the taken baseline.

---

### B.5 — does conviction predict anything?

**POPULATION [DET-INTRA], dedup.**

```
  TAKEN only   (n=170)
    band               n  target  mean out%   gross R     ±SE
    0.00-0.55         4   50.0%     +0.193    +0.501   0.867   << n<20
    0.55-0.65        26   23.1%     -0.222    -0.112   0.259
    0.65-0.75        63   17.5%     -0.354    -0.200   0.156
    0.75-0.85        38   13.2%     -0.473    -0.354   0.165
    0.85-2.00        39   15.4%     -0.304    -0.073   0.201
    Spearman rho(confidence, gross R) = +0.0137   z = +0.18   DIRECTIONLESS

  ALL detections (every verdict), first row of each key   (n=887)
    band               n  target  mean out%   gross R     ±SE
    0.00-0.55        67   20.9%     -0.046    -0.197   0.162
    0.55-0.65       180   16.1%     -0.084    -0.097   0.102
    0.65-0.75       424   16.5%     -0.115    -0.167   0.068
    0.75-0.85       138   13.8%     -0.323    -0.377   0.099
    0.85-2.00        78   10.3%     -0.420    -0.400   0.124
    Spearman rho(confidence, gross R) = +0.0025   z = +0.07   DIRECTIONLESS
```

**Outcome does not rise with confidence. Across the top four bands it falls
monotonically, on both metrics, in the larger sample:** target rate 16.1% → 16.5%
→ 13.8% → 10.3%, gross R −0.097 → −0.167 → −0.377 → −0.400. The 0.55–0.65 vs
0.85+ gap is 0.303R at **1.9 SE** (n=180 vs n=78). Spearman is ~0 only because the
sub-0.55 band (n=67) is also poor, which breaks monotonicity over the full range.

**The conviction floor that rises 0.55 → 0.80 through the session is tightening
along an axis whose measured slope is flat-to-negative.** That is the same finding
as B.3's `BELOW_CONVICTION` bucket, reached from the opposite side and on a larger
sample — two independent constructions agreeing. Neither reaches 2 SE
individually.

---

### B.6 — when, not what

**Preflight on [CLOSED]:** `regime_at_entry` is populated on **0 of 56** intraday
closed rows, and there is **no entry-time column at all** — `entry_date` is a
DATE. **Phase and regime are not computable on the closed book.** B.6 therefore
runs on [DET-INTRA], stated rather than silently substituted.

**By phase — PRIME is not better than DRIFT. It is worse, in both views.**

```
  TAKEN only (n=170)
  PRIME                      n= 127  target 15.7%  grossR -0.217±0.103  netR -0.454
  DRIFT                      n=  34  target 23.5%  grossR -0.092±0.228  netR -0.441
  AFTERNOON                  n=   9  target 22.2%  grossR +0.099±0.507  << n<20

  ALL detections (n=890 dedup)
  PRIME                      n= 666  target 16.4%  grossR -0.233±0.051  netR -0.370
  DRIFT                      n= 157  target 14.2%  grossR -0.175±0.111  netR -0.325
  AFTERNOON                  n=  67  target 13.6%  grossR -0.033±0.156  netR -0.191
```

The ordering is the same in both views and is the **opposite** of the assumption
the phase weighting encodes: gross R improves monotonically as the session ages
(PRIME −0.233 → DRIFT −0.175 → AFTERNOON −0.033 on n=890). **No pair separates by
2 SE** — PRIME vs AFTERNOON is 1.2 SE, PRIME vs DRIFT 0.5 SE. Consistent direction
across two independent slicings, insufficient magnitude. Worth a Stage 3
hypothesis; not worth a config change.

**By regime — NOT ANSWERABLE.** See PRE-2. For the record and for no other
purpose: TAKEN `NEUTRAL` n=34 grossR −0.548±0.156 vs `NULL` n=123 −0.132±0.114
(2.2 SE) — but 100% of `NEUTRAL` is 11–13 Aug and 100% of `NULL` is
28-Jul–11-Aug, so that 2.2 SE measures two date ranges, not two regimes. `CAUTION`
n=13 (+0.387) is below the n bar and is carried by one VCE detection at +2.500R.

**Engine × regime — NOT ANSWERABLE.** Only two cells clear n=20 (VWR×NULL n=35,
ORB×NULL n=30) and `NULL` is not a regime.

```
  VWR x None                 n=  35  target 20.0%  grossR -0.144±0.214
  VWR x NEUTRAL              n=  14  << n<20
  ORB x None                 n=  30  target 16.7%  grossR -0.101±0.206
  ORB x NEUTRAL              n=  10  << n<20
  VCE x None n=19 / VCE x NEUTRAL n=10 / SDN x None n=13 / SDN x CAUTION n=11
  GAP x None n=15 / PDL x None n=8 / PBK x None n=2 / RNG x None n=1   << all n<20
```

**This directly settles the roadmap's Stage 6 conditional:** `HARDENING_BRIEF`
Phase 5 (breadth) requires Stage 3 regime segmentation across 200+ observations;
the entire labelled regime corpus is **2 sessions**. `regime_fit_multiplier()`
stays at weight 0.0 — it is not waiting for tuning, it is waiting for data that
does not exist yet. The 12-Aug hypothesis in the spec ("continuation engines may
only pay in continuation regimes") cannot be tested across 30 sessions, because
12-Aug is one of only two sessions carrying a regime label at all.

**By direction, [CLOSED], MIS only** — B.1 territory, **cited from Stage 1b, not
re-derived**; reproduced only to confirm the load path is identical: LONG n=39
gross −0.132 net −0.251; SHORT n=8 gross −0.109 net −0.256 (n<20, insufficient).

---

### C.1 — do the swing engines produce differentiated outcomes?

**POPULATION [PLANS-SW]: `signal_output_daily` rows with a resolved
`outcome_return_pct`, n=1386, dates 2026-06-25 → 2026-08-11.** These are plans
that triggered, resolved by the pipeline — **not closed trades.** `strategy` is a
combination string (`CTL+MOM+SEC`), so a row is credited to **every** engine token
it carries and the n column sums past 1386.

```
    engine           n  TARGET   STOP  mean ret%     ±SE   median   verdict
    SEC            819    7.2%   1.2%     +1.460   0.264   +1.183
    CTL            676   23.2%   5.6%     +2.166   0.238   +2.774
    MOM            429   10.0%   0.5%     +0.566   0.366   +0.151
    RVS             76    6.6%   3.9%     +1.193   0.934   +0.615
    RSB             71   25.4%   7.0%     +0.332   0.673   +0.228
    IAD             48    8.3%   2.1%     +3.153   1.040   +1.353
    TPO             33   39.4%  18.2%     +3.663   1.126   +4.197
    SBS             30   20.0%   3.3%     +2.920   1.286   +3.023
    VBD             23   17.4%   0.0%     +3.051   1.695   +4.509
    BOOK          1386   19.8%   4.5%     +1.106   0.190   +1.346
```

**Yes — unlike intraday, the swing engines separate.** CTL (+2.166, n=676) vs MOM
(+0.566, n=429) is **3.7 SE**; CTL vs the book mean is 3.5 SE. CTL also carries the
highest target rate of the three large buckets (23.2% vs SEC 7.2% and MOM 10.0%).
MOM vs book is −1.3 SE — weakest of the three, not yet separated from it.
TPO/SBS/VBD/IAD look strong but sit at n=23–48 with SEs of 1.0–1.7; above the
n≥20 bar, far below anything that supports an action.

**C.1b — POPULATION [CLOSED]: `closed_positions`, `framework='SWING'`, n=78, R
computable on 8.**

```
    strategy            n  R-able   gross R     net R  mean pnl Rs   verdict
    CTL                47       1    +0.289    +0.184          +33
    CTL (Legacy)       21       0         —         —         -644
    None                2       0         —         —         -283   INSUFFICIENT (n<20)
    TPO                 2       2    +0.423    -0.008          +36   INSUFFICIENT (n<20)
    SBS                 2       2    +0.178    +0.025           +3   INSUFFICIENT (n<20)
    CTL+MOM+SEC         1       1    +0.081    -0.086          -10   INSUFFICIENT (n<20)
    AI PICK             1       0         —         —         -306   INSUFFICIENT (n<20)
    MOM+SEC             1       1    +0.192    +0.118          +34   INSUFFICIENT (n<20)
    SEC                 1       1    +2.095    +1.974         +345   INSUFFICIENT (n<20)
```

Every bucket insufficient; the largest is CTL n=47 with **one** R-computable row.
No swing engine can be judged on real trades.

---

### C.2 — does `final_score` correlate with realised return?

**POPULATION [PLANS-SW], n=1386.**

```
  final_score quintile cuts: [57.3, 63.7, 68.4, 73.3]
    quintile                   n  TARGET   STOP  mean ret%     ±SE   median
    Q1 [   0.0,  57.3)    277   17.0%   5.4%     +1.697   0.513   +2.141
    Q2 [  57.3,  63.7)    272   16.9%   4.4%     +1.304   0.442   +0.632
    Q3 [  63.7,  68.4)    277   23.8%   4.7%     +0.480   0.420   +0.997
    Q4 [  68.4,  73.3)    280   19.6%   2.1%     +0.693   0.371   +1.090
    Q5 [  73.3, 999.0)    280   21.4%   6.1%     +1.363   0.361   +1.683

  Spearman rho(final_score, outcome_return_pct) = -0.0175   z = -0.65   n=1386
```

**No monotone relationship, on the largest clean sample in this entry.** The
**lowest**-scored quintile has the highest mean return (+1.697%) and the highest
median (+2.141%); Q3 is the worst (+0.480%). Spearman is −0.018 at z=−0.65 —
indistinguishable from zero on 1386 observations, which is enough n that this is a
real negative result rather than an underpowered one.

**The swing ranking layer does not order outcomes.** `swing_max_new_per_day`
(**3** in `system_config`, not the 2 the spec assumes) is choosing arbitrarily
from the top of a list whose order carries no measured information. This is the
same shape as B.5's intraday conviction result, in a different framework, on a
sample 8× larger.

---

### C.3 — are the exit rungs helping?

**Ran:** `cd backend && python -m tools.exit_ladder_replay --min-r 0.5`

```
  EXIT-LADDER ESTIMATE — give-back at 50% / min 0.5R, 56 measurable position(s) (0 unmeasurable, excluded)
  11 position(s) WOULD HAVE been cut early by give-back:
  symbol        actual exit           actual R   peak R  giveback R
  KAYNES        SETUP_INVALIDATED       -0.981    +0.61      +0.305
  SAPPHIRE      MULTI_LEG               -0.633    +1.06      +0.530
  ADANIGREEN    SETUP_INVALIDATED       -0.474    +0.63      +0.316
  IFCI          TIME_EXIT               -0.396    +0.66      +0.332
  HINDCOPPER    SETUP_INVALIDATED       -0.346    +0.65      +0.327
  SWIGGY        SETUP_INVALIDATED       -0.282    +0.54      +0.268
  RKFORGE       GAVE_BACK_THE_MOVE      -0.022    +0.56      +0.280
  GRAPHITE      TIME_EXIT               +0.269    +0.84      +0.422
  GVT&D         TRAIL_SL_HIT            +0.780    +1.82      +0.912
  FSL           MULTI_LEG               +0.550    +1.35      +0.677
  GVT&D         GAVE_BACK_THE_MOVE      +0.168    +0.53      +0.263
  ACTUAL exits (as they really closed):   total -2.64R  (56 positions)
  GIVE-BACK ceiling estimate:              total +3.36R  (11 would have changed)
  Estimated ceiling on the improvement:    +6.00R
  WARNING: This is a CEILING, not a forecast — it assumes give-back always wins any
           race with another exit rung, which the summary data cannot rule out or confirm.
```

**The number the spec says "was never measured" is now measured — for intraday.
Give-back would cut 4 winners out of 11 (36%).** Winners cut: GRAPHITE +0.269,
GVT&D +0.780, FSL +0.550, GVT&D +0.168 = **+1.767R surrendered**. Losers avoided:
7 rows totalling **−3.134R**. Net favourable on this sample, at a 36% false-cut
rate, against the tool's own explicit ceiling caveat.

**But C.3 is a PHASE C question — it is asked about the SWING ladder — and this
tool cannot answer it.** `tools/exit_ladder_replay.py:99` hardcodes
`.eq("framework", "INTRADAY")`. The 56 rows above are the intraday book. The swing
side is not merely unqueried, it is **not computable**:

```
  SWING: 78 closed rows
    r_multiple + high_water_mark + stop all present (replay-measurable): 8
```

**And the two rungs C.3 asks about have never fired on the swing book:**

```
  config: exit_giveback_pct=50  exit_giveback_min_r=0.5  exit_stall_days=10  exit_stall_peak_r=0.5
          intraday_giveback_pct=50.0  intraday_giveback_min_r=0.5
  SWING     n=78   give-back rung fired: 0   stall rung fired: 0
  INTRADAY  n=56   give-back rung fired: 3   stall rung fired: 0
      GIVEBACK  RKFORGE  GAVE_BACK_THE_MOVE  R=-0.022
      GIVEBACK  GVT&D    GAVE_BACK_THE_MOVE  R=+0.168
      GIVEBACK  MCX      GAVE_BACK_THE_MOVE  R=+0.926
  SWING exit_reason histogram: TRAIL SL HIT 37, STRATEGY ROTATION 12, TIME EXIT 10,
                               STOP LOSS HIT 9, BROKER_EXIT 8, "4.5% loss" 1,
                               MANUAL DISCIPLINE EXIT 1
  SWING hold_days: n=78  min=0  median=5.0  max=34
                   held 11-15 sessions: 11    held >15 sessions: 7
```

**`EXIT_STALL` has fired zero times in either book, ever.** `EXIT_GIVEBACK` has
fired zero times on swing and three times on intraday under a different name.
Every swing exit reason on record is a legacy space-separated string. **The
question "is `EXIT_GIVEBACK` net-positive versus holding to the stop on the swing
book" has no data behind it at all** — the rung is configured and has never closed
a swing trade. The spec's contamination warning (`fix/session-count-parity`) is
moot here: the reading is not contaminated, it is absent. `exit_stall_days=10`
against a swing book whose **median hold is 5.0 days** is at least consistent with
a rung that rarely reaches its own trigger.

---

### C.4 — expectancy in rupees

```
  capital_snapshot.configured  Rs 30,000
  risk_pct_per_trade           1.0%   -> Rs 300 risk per position
  max_positions_risk_on/off    8 / 6
  swing_max_new_per_day        3
  intraday_max_new_per_day     20
```

**MEASURED — POPULATION [CLOSED]:**

```
    bucket                  n     hit   mean win  mean loss   EXPECTANCY    net R   cost R
    INTRADAY all           56   26.8%        +65        -41       -12.92   -0.390   +0.338
    INTRADAY MIS           47   25.5%        +56        -43       -17.93   -0.252   +0.124
    SWING all              78   39.7%       +442       -555      -158.78   +0.278   +0.204
    SWING attributed        9   55.6%       +102        -44       +37.26   +0.278   +0.204
```

(The two SWING R columns are the 8-row subsample — see "Could not determine".)

**TO SPECIFICATION — the spec's own shape (1% risk, 40% hit, 2R winner), with
MEASURED friction rather than an assumed one:**

```
      capital  risk/trade book              exp R   Rs/trade   Rs/mo @20  %/mo @20   Rs/mo @40  %/mo @40
       20,000         200 intraday MIS     +0.076     +15.12        +302     +1.51        +605     +3.02
       20,000         200 swing CNC        -0.004      -0.85         -17     -0.08         -34     -0.17
       30,000         300 intraday MIS     +0.076     +22.68        +454     +1.51        +907     +3.02
       30,000         300 swing CNC        -0.004      -1.27         -25     -0.08         -51     -0.17
```

**This is the finding C.4 was written to surface, and it is worse than "small".**

At the framework's **own design target** — 40% hit rate, 2R average winner, 1%
risk — the arithmetic is `0.40 × 2 − 0.60 × 1 = +0.200R` gross. Intraday MIS
friction (+0.124R measured) leaves **+0.076R**, or **₹15–23 per trade**, or
**+1.51% of capital per month at 20 trades**. Swing CNC friction (+0.204R
measured) leaves **−0.004R — the design target is a break-even coin flip on the
swing book.**

So "profits are not significant" is confirmed as **partly structural and not
fixable by engine work**: even with every rule performing to specification,
intraday returns ~1.5%/month at ₹20,000 and swing returns approximately zero. The
swing book does not need better selection to reach its design target — **at its
design target it still makes nothing**, because CNC friction consumes the entire
edge that 40%/2R produces. The levers are clip size (the flat ₹15.04 DP fee is a
fixed cost fighting a percentage edge), a higher target multiple, or capital — not
engine selection. Note the swing +0.204R friction figure inherits the 8-row
caveat below.

---

### BUGS AND DRIFT FOUND — recorded, not fixed (Stage 2 is read-only)

**BUG-4 — `tools/exit_ladder_replay.py:99` is hardcoded to INTRADAY, but C.3 is a
swing question.** `.eq("framework", "INTRADAY")`. The spec names this tool for the
swing exit-rung question and it cannot see the swing book. Not a wrong number — a
silently narrowed population, the same class of defect Stage 1 found in
`engine_scorecard`. Adding `--book` would mirror the `--days` fix of Stage 1b.

**BUG-5 — `EXIT_STALL` has never fired in either book, and `EXIT_GIVEBACK` has
never fired on swing.** Configured (`exit_stall_days=10`, `exit_stall_peak_r=0.5`,
`exit_giveback_pct=50`, `exit_giveback_min_r=0.5`) and producing zero closes across
134 rows. This is the CLAUDE.md pattern "a check that cannot PASS" applied to an
exit rung. Whether it is unreachable or merely never reached is **not** established
here, and the distinction matters before Stage 4 tunes it.

**DRIFT-1 — documented config values do not match `system_config`.**
`intraday_max_new_per_day` is **20**; CLAUDE.md says 4. `swing_max_new_per_day` is
**3**; `EDGE_DIAGNOSTIC.md` C.2 says 2. `capital_snapshot.configured` is
**₹30,000**; CLAUDE.md's header says ₹20,000 and `EDGE_DIAGNOSTIC` C.4 computes on
₹20,000. Both capital figures are given in C.4 so the reader can pick. Nothing was
changed.

---

**Could not determine:**

- **Any engine verdict, either book.** [CLOSED] intraday: largest bucket ORB n=11.
  [CLOSED] swing: largest CTL n=47 with **1** R-computable row. This is precisely
  what Stage 3 is for and no retirement is proposed.
- **Whether any regime affects anything.** `regime_at_detection` covers 2 of 13
  sessions (PRE-2). Engine × regime has two cells above n=20 and both are the
  `NULL` bucket. **`HARDENING_BRIEF` Phase 5 cannot be evaluated** and its
  200-observation precondition is nowhere near met.
- **Whether the swing exit ladder helps.** BUG-4, plus 8 replay-measurable swing
  rows, plus zero rung firings. Three independent reasons, any one sufficient.
- **Whether the B.3 counterfactuals would have filled.** `outcome_pct` is
  simulated against bars by `outcomes.resolve_day`; no fill, slippage or queue
  position is modelled. Inherited from Stage 1 and it applies to every B.3, B.5 and
  B.6 number here.
- **Whether `BELOW_CONVICTION` is genuinely inverted.** +1.5 SE pooled, +1.2 SE
  direction-matched on shorts. Two independent constructions (B.3 and B.5) agree on
  the sign. Neither reaches 2 SE. **This is the single most valuable thing for
  Stage 3 to settle** and it is not settled here.
- **Any swing R statistic whatsoever.** All 8 R-computable swing rows are gross
  winners — 8 of 8 — while the book they come from lost ₹9,253 net across 78 trades
  at a 39.7% hit rate:

```
  PPLPHARMA SEC +2.095 | VIJAYA TPO +0.452 | KIMS TPO +0.394 | ETERNAL CTL +0.289
  TRAVELFOOD SBS +0.267 | GABRIEL MOM+SEC +0.192 | CIPLA SBS +0.089 | BHEL CTL+MOM+SEC +0.081
  of these, gross_r > 0: 8 of 8
```

  Stage 1's "+0.482 gross R (n=8)" and this entry's swing `cost_r +0.204` /
  `net_r +0.278` are all computed on a subsample containing **zero losers**. They
  are not estimates of swing edge and must not be quoted as any. The C.4 "SWING
  attributed" row (100% gross hit rate) is included above precisely to make that
  visible, and C.4's swing friction figure inherits it.
- **Why PDL's closed book is 7 of 7 CNC.** Identified — it explains PDL's +1.626R
  cost R entirely — but the cause, why `intraday_product` was CNC for that engine,
  was not investigated. Carried forward from Stage 1 in sharper form.
- **The 631 `intraday_setups` rows with a null `meta.sub_engine`.** They are why
  three dedup keyings give 840 / 890 / 941. Not chased.
- **Whether `outcome_return_pct` on [PLANS-SW] equals a tradeable return.** It is
  the pipeline's own resolution of a plan, not a fill. C.1 and C.2 inherit that,
  and it is why C.2's null result is stated as "the ranking does not order
  outcomes" rather than "the ranking does not order P&L".

---

**Found (summary):**

1. **Signal problem confirmed, not re-derived** — cited from Stage 1b per the
   session brief. B.2 adds that the book-level [CLOSED] reading is gross −0.052R
   (n=56), MIS-only gross −0.128R (n=47).
2. **No gate is inverted at 2 SE.** One bucket clears that bar and it clears it in
   the working direction: `BLOCKED_EVENT` at −2.5 SE. `ALLOCATOR_DECLINED` is
   measurably neutral at +0.4 SE (n=63) — the spec's "gates are noise" reading.
3. **`BELOW_CONVICTION` is the one inversion candidate**, at +1.5 SE pooled and
   +1.2 SE on shorts (30.4% target vs 20.8%). Not established.
4. **B.5 reaches the same conclusion independently and more cleanly**: gross R
   falls monotonically across the top four confidence bands (−0.097 → −0.167 →
   −0.377 → −0.400) and target rate falls with it (16.1% → 10.3%). Spearman +0.003,
   z=+0.07. **The conviction score is noise, and its slope leans negative.**
5. **The swing ranking layer is decorative** — Spearman(final_score, return) =
   −0.018, z=−0.65 on **n=1386**. The lowest quintile has the best mean and median
   return. Largest and cleanest sample in this entry.
6. **Swing engines DO differentiate, unlike intraday** — CTL +2.166% (n=676) vs MOM
   +0.566% (n=429) at 3.7 SE on [PLANS-SW].
7. **PRIME is not better than DRIFT** — the ordering is monotone in the opposite
   direction across both slicings, at 0.5–1.2 SE.
8. **Two methodological traps recorded** so no future session falls in: the spec's
   own B.3 recipe gives early-firing gates a free round trip (PRE-1), and the
   undeduplicated view manufactures 3-SE results the dedup view does not support
   (B.3d).
9. **At its own design target the swing book earns ≈₹0** and intraday earns
   ~1.5%/month at ₹20,000. Capital and friction, not selection.
10. Two tool defects (BUG-4, BUG-5) and three doc/config drifts (DRIFT-1).

---

**Recommends:**

- **No retirements.** That is Gate 3, after the replay, and this entry contains no
  bucket meeting the bar (n≥30 deduplicated **and** negative gross R **and** failure
  against a random baseline). Stated explicitly because B.2b shows three intraday
  buckets above n=20 with negative gross R — two of three criteria, which the
  roadmap already says is not enough.
- **No config changes, no gate removals.** `BELOW_CONVICTION` is the one thing the
  evidence points at and it points at 1.2–1.5 SE. Removing a gate on that is the
  mirror image of adding one.
- **Stage 3 should be scoped to settle three specific questions**, none of which
  more sessions of the current book will answer quickly: (a) is the conviction
  score inverted or merely flat; (b) does phase ordering really run PRIME < DRIFT <
  AFTERNOON; (c) do the swing engines' [PLANS-SW] differences survive on real fills.
- **Carry PRE-1 into any future B.3-style query.** Compare gross R, never
  `outcome_pct`, across the cost gate.
- **Do not quote any swing R number from this ledger.** The 8-row sample is 8
  winners out of 8.
- Flag for whoever runs Stage 4: `HARDENING_BRIEF` Phase 5 has no evidence base,
  and the roadmap already authorises deleting it if Stage 3 does not support it.
  Nothing here supports it.

**Gate 2: PASS.** The diagnosis (signal, from Stage 1b), per-engine gross/cost/net
R with n on both populations, and the inverted-gate answer (none at 2 SE; one
candidate at 1.5) are all recorded. No retirements, per the gate's own terms.

---

## 2026-08-14 — Stage 2b (unit economics) — the swing book's break-even hit rate is 39.66% and it hits 39.7%

**Ran:**

```bash
git checkout -b diagnostic/unit-economics
cd backend && python -m tools.verify                    # 432/432 before and after
cd backend && python -m tools.expectancy_ledger | grep -E "friction, in R"
cd backend && python -m tools.unit_economics            # NEW, read-only, committed with this entry
```

`tools/unit_economics.py` is new and is the only thing this stage adds. It is
read-only — no writes, no orders, no config. It exists because the ledger's own
rule is that every number carries the command that produced it, and a model
living in a scratch directory satisfies that for exactly one session. Registered
nowhere, called by nothing; it is run by hand.

---

### PREFLIGHT — three corrections that govern how every number below reads

**P.1 — friction is STATUTORY here, and `round_trip()` would double-count.**
`intraday/cost_model.round_trip()` adds `cost_slippage_bps` (5) to both legs.
That is correct for a PRE-TRADE gate, which decides against a price it has not
yet paid. It is a double count against a REALISED outcome, because slippage is
already inside the fill on both books: `execution/paper_broker.py:90` fills
MARKET orders at `ltp * (1 ± slip)`, so every intraday row's `entry_price` and
`exit_price` already carry it, and live fills embed real slippage by
construction. `tools/expectancy_ledger.py` uses `entry_leg + exit_leg` for this
reason. This stage matches it, so every figure below is directly comparable to
Stage 2. **Not a defect in either** — but note the asymmetry it creates and that
nothing currently states: the intraday cost GATE prices an MIS round trip at
**0.206%** of position and the expectancy LEDGER prices the same trade at
**0.106%**. Same trade, two numbers, because they answer two different
questions. Recorded here so the next reader does not "reconcile" them.

**P.2 — Stage 2's `+0.204R` is a mean of eight ratios and one row is 0.767R.**
Reproduced exactly, then decomposed:

```
   symbol           clip   stop%  risk Rs  cost Rs   cost%  friction R
   KIMS              802   2.74%       22    16.83  2.097%       0.767   <<
   BHEL             2037   5.77%      118    19.58  0.961%       0.167
   PPLPHARMA        2609   6.69%      175    21.25  0.814%       0.122
   TRAVELFOOD       2713   4.81%      130    21.11  0.778%       0.162
   ETERNAL          2754   7.34%      202    21.23  0.771%       0.105
   VIJAYA           2780   8.07%      224    21.33  0.767%       0.095
   GABRIEL          2788  10.43%      291    21.30  0.764%       0.073
   CIPLA            2940   5.11%      150    21.59  0.734%       0.144
   friction R   mean +0.204   median +0.133
```

KIMS is a one-share ₹802 position risking ₹22, against a ₹15.04 DP fee. Its
friction is 0.767R and it drags the mean of the other seven (+0.124) to +0.204.
A mean over eight heterogeneous ratios is a statement about that one row.
**Stage 2's C.4 headline of −0.004R at the design target is therefore the
KIMS-weighted figure.** At the median clip and median stop the same arithmetic
gives **+0.068R**, and at the geometry the pipeline is producing today it gives
**+0.010R**. Stage 2's direction survives — the swing book at its design target
is inside noise of zero — but −0.004R overstates the deficit, and the reason it
overstates it is a single ₹802 clip.

**P.3 — the model is validated three ways before anything is built on it.**
(i) `expectancy_ledger` reconciles it against the broker's own contract notes:
`-0.01% across 4 round trip(s), Rs 25,596 turnover`. (ii) It reproduces Stage 2
exactly — SWING/CNC mean **+0.204R**, INTRADAY/MIS mean **+0.124R**, both to
three decimals. (iii) Per-row, all eight swing rows above.

---

### THE IDENTITY THIS STAGE IS ABOUT

```
    friction_R = statutory_rupees(clip) / risk_rupees = cost_pct(clip) / stop_pct
```

and because CNC's DP fee is flat, `cost_pct(clip) = k + DP/clip`. So friction in
R is a function of **two** things, and which one is held fixed **inverts the
advice**:

| framing | what moves | effect of a BIGGER clip |
|---|---|---|
| stop % fixed | risk in ₹ rises with clip | DP amortises → friction **falls** |
| risk ₹ fixed | stop narrows as 1/clip | stop narrows faster → friction **RISES** |

**The system holds risk fixed** (`risk_pct_per_trade` = 1.0). Measured, at
₹20,000:

```
      clip   stop%  cost Rs   fricR │    @2.0R    @2.5R    @3.0R │  Rs/tr@2R
     2,000  10.00%    19.48   0.097 │   +0.103   +0.303   +0.503 │    +20.52
     4,000   5.00%    23.94   0.120 │   +0.080   +0.280   +0.480 │    +16.06   << ceiling
    10,000   2.00%    37.28   0.186 │   +0.014   +0.214   +0.414 │     +2.72
    20,000   1.00%    59.54   0.298 │   -0.098   +0.102   +0.302 │    -19.54
```

**This contradicts `TRADEOS_ROADMAP.md` Stage 4 item 2, which lists "clip-size
floor" as a friction fix.** Under the sizing rule the system actually uses, a
clip-size floor makes friction *worse*. A clip floor only helps if the stop
width is held — i.e. if `risk_pct_per_trade` rises with it. Raising one without
the other is a change that reads as a fix and is not one.

---

### GEOMETRY, MEASURED — the inputs the grid is anchored on

```
  SWING closed book   n=8    clip median Rs2,733   stop median 6.23%   planned target median 1.44R
  SWING plans 13-Aug  n=82                         stop median 4.34%   planned target median 1.90R
  INTRADAY MIS        n=47   clip median Rs6,525   stop median 0.90%   planned target median 2.00R
  swing close rate    10.6/month  (78 closes, 2025-12-31 to 2026-08-12, 224 days)
```

The two swing stop widths disagree — 6.23% against 4.34% — and **the
disagreement decides the answer**, so both are carried through every table
rather than one being picked.

### FRICTION BY CLIP

```
       clip │   CNC Rs    CNC %  DP share │   MIS Rs    MIS % │  CNC/MIS
      2,000 │    19.48   0.974%     77.2% │     2.12   0.106% │    9.19x
      2,733 │    20.60   0.824%     73.0% │     2.65   0.106% │    7.77x
      4,000 │    23.94   0.599%     62.8% │     4.26   0.106% │    5.62x
     10,000 │    37.28   0.373%     40.3% │    10.62   0.106% │    3.51x
     20,000 │    59.54   0.298%     25.3% │    21.26   0.106% │    2.80x
     50,000 │   126.28   0.253%     11.9% │    53.14   0.106% │    2.38x
    100,000 │   237.52   0.238%      6.3% │    82.68   0.083% │    2.87x
  asymptote, clip → ∞:  CNC 0.2225%   MIS 0.0355%
```

**MIS cost% is FLAT at 0.106% across this entire ladder.** Brokerage is
`min(₹20, 0.03%)` per order and the percentage branch wins below ₹66,667 per
leg, so nothing amortises until a position worth ~₹133,000. **Clip size is not
an intraday lever at any size this account can reach.** CNC's slope is 77% DP
fee at ₹2,000 and 6% at ₹100,000 — that is the whole of the swing clip argument.

---

### THE TWO ANSWERS ASKED FOR

**Minimum clip at which swing CNC clears friction at 2R (40% hit):**

| stop geometry | minimum clip | vs current ₹2,733 clip |
|---|---|---|
| 6.23% — closed book median | **₹1,250** | already clears, net **+0.068R** |
| 4.34% — current plans, n=82 | **₹2,250** | already clears, net **+0.010R** |

**The clip is not the binding constraint.** The book is already above both
thresholds. It clears by ₹483 at the geometry it is currently planning.

**Target multiple that clears friction at the current clip:**

| clip | stop | required target | friction | net @2R |
|---|---|---|---|---|
| ₹2,733 | 6.23% | **1.831R** | 0.132R | +0.068R |
| ₹2,733 | 4.34% | **1.975R** | 0.190R | +0.010R |
| ₹4,000 | 6.23% | 1.740R | 0.096R | +0.104R |
| ₹4,000 | 4.34% | 1.845R | 0.138R | +0.062R |

**The pipeline's median planned target is 1.90R (n=82) and its break-even is
1.975R. The book is planning targets below its own break-even.**

```
   target 1.90R (plans median n=82) stop 4.34% : gross +0.160R  friction 0.190R  net -0.030R
   target 2.00R (design           ) stop 4.34% : gross +0.200R  friction 0.190R  net +0.010R
```

**INTRADAY MIS, the same two questions:**

| question | answer |
|---|---|
| minimum clip clearing 2R | **ANY clip** — cost% is constant, so clip is not a constraint |
| required target at ₹6,525 clip | **1.795R** (friction 0.118R, net @2R **+0.082R**) |

Stage 2 reported +0.076R; the +0.082R here is the same number computed on the
median 0.90% stop rather than the 0.94% mean. Both stand.

---

### THE FORM THAT IS HARDEST TO ARGUE WITH — break-even hit rate

`h* = (1 + friction_R) / (1 + target_R)`. No assumption about the win rate
enters, so this survives even if the 40% design target never does.

```
   measured: SWING 39.7% (n=78) · INTRADAY MIS 25.5% (n=47), Stage 2 C.4
   book         clip  stop%  fricR │  h* @2.0R  h* @2.5R  h* @3.0R
   SWING       2,733  6.23%  0.132 │   37.74%   32.35%   28.31%
   SWING       2,733  4.34%  0.190 │   39.66%   34.00%   29.75%   <<
   SWING       4,000  6.23%  0.096 │   36.54%   31.32%   27.40%
   SWING       4,000  4.34%  0.138 │   37.93%   32.51%   28.45%
   INTRADAY    6,525  0.90%  0.118 │   37.27%   31.95%   27.95%
```

**At the clip it takes and the stops it currently plans, the swing book's
break-even hit rate at 2R is 39.66%. It hits 39.7%.** Four hundredths of a
percentage point of edge, on n=78. That is not a book with a small edge; it is
a book sitting on its own fee schedule.

At 2.5R the same configuration breaks even at 34.00% — **5.7 points of headroom
against the same measured hit rate.** The target multiple is the lever; the clip
is not, because it is already above threshold and capped anyway (next section).

---

### AT ₹20,000 — every configuration the constraints permit

`max_position_pct` = 20% is **enforced** (`analysis/portfolio_constraints.py:223`,
`qty_by_maxpos = int((capital * max_position_pct / 100.0) // entry_price)`), so
the clip ceiling at ₹20,000 is **₹4,000**. CNC is full cash — no leverage — so
concurrent slots are ₹20,000 ÷ clip.

```
  monthly at the MEASURED close rate of 10.6 trades/month
     clip   stop%  riskRs  risk%   fricR    @2.0R    @2.5R   reqTgt  Rs/mo@2R  %/mo@2R  Rs/mo@2.5R  slots
    2,000   6.23%     125  0.62%   0.156   +0.044   +0.244   1.891R       +58   +0.29%        +322    10
    2,000   4.34%      87  0.43%   0.224   -0.024   +0.176   2.061R       -22   -0.11%        +162    10
    2,733   6.23%     170  0.85%   0.132   +0.068   +0.268   1.831R      +122   +0.61%        +483     7
    2,733   4.34%     119  0.59%   0.190   +0.010   +0.210   1.975R       +13   +0.06%        +264     7
    3,000   6.23%     187  0.93%   0.116   +0.084   +0.284   1.790R      +166   +0.83%        +562     6
    3,000   4.34%     130  0.65%   0.167   +0.033   +0.233   1.917R       +46   +0.23%        +322     6
    4,000   6.23%     249  1.25%   0.096   +0.104   +0.304   1.740R      +275   +1.37%        +803     5
    4,000   4.34%     174  0.87%   0.138   +0.062   +0.262   1.845R      +114   +0.57%        +482     5
```

Note the ₹4,000 / 6.23% row risks ₹249 — **1.25% of capital, above the
configured 1.0%.** The best 2R cell available inside the config as written is
₹4,000 / 4.34%: **+₹114 per month, +0.57%.**

**A config incoherence found on the way:** `max_positions_risk_on` is 8 and the
clip ceiling is ₹4,000. 8 × ₹4,000 = ₹32,000 against ₹20,000 of cash. At the
₹4,000 ceiling the account funds **5** positions, not 8. Not changed.

---

### THE QUESTION, ANSWERED PLAINLY

> At ₹20,000, is there a configuration where the swing framework is worth running?

**At 2R — no.**

Not because expectancy is negative. Because the entire achievable edge is
**+₹114 to +₹275 per month** — 0.57% to 1.37% — and it is earned by paying
**₹23.94 of charges per trade to keep ₹10.78 of edge**. The account pays more
than twice its own edge in fees. And the margin that produces it is 0.04
percentage points of hit rate: break-even 39.66%, measured 39.7%. Any
degradation the book has already shown — a 1.90R median planned target instead
of 2.00R — takes it to **−0.030R**. It is already there.

Three independent reasons, any one sufficient:

1. **The break-even hit rate and the measured hit rate are the same number.**
   39.66% vs 39.7%, n=78. There is no edge to be eroded because there is none
   to begin with.
2. **The book plans below its own break-even.** Required 1.975R, planning 1.90R
   median across 82 current plans. The design target of 2R is not what the
   pipeline is asking for.
3. **The clip lever is exhausted and capped.** The book is already above the
   ₹2,250 threshold, and `max_position_pct` caps it at ₹4,000 regardless. Under
   fixed-rupee risk, raising it further makes friction worse, not better.

**At 2.5R — yes, conditionally, and it is the only lever that works at this
capital.** ₹4,000 clip at 4.34% stops gives **+0.262R, +₹482/month, +2.41%**,
with a break-even hit rate of 32.51% against a measured 39.7% — 7.2 points of
headroom instead of 0.04. At ₹2,733 it is +₹264/month.

The condition, stated because the grid does not model it: **the grid holds the
hit rate at 40% while raising the target, and that is generous.** A target
further away is reached less often. The break-even table is the honest form of
this — 2.5R needs 34.00% rather than 39.66% — but whether moving the target from
1.90R to 2.5R costs more than 5.7 points of hit rate is **not established here
and cannot be without Stage 3.**

**Intraday MIS, by contrast, nets +0.082R at 2R at ANY clip**, with a break-even
of 37.27% — but its measured hit rate is 25.5%, twelve points below that. Its
friction problem is solved and its selection problem is not. The two books fail
for opposite reasons, and the same fix helps neither.

---

**Could not determine:**

- **Whether 40% at 2R is achievable at all.** Every grid in this entry is
  conditional on it. The measured book is 39.7% hit and −₹12,385 net over 78
  trades, at a median PLANNED target of 1.44R. The specification has never been
  demonstrated on a single closed trade, and this stage does not demonstrate it
  — it prices it.
- **What raising the target to 2.5R costs in hit rate.** The decisive unknown
  for the only recommendation this entry makes. Stage 3.
- **Whether the 4.34% or the 6.23% stop geometry is the right anchor.** n=82
  current plans against n=8 taken trades, and they disagree by enough to flip
  the 2R answer. The n=8 are the trades that were actually SELECTED for entry,
  so the gap may be a selection effect rather than drift — untestable at n=8.
- **Slippage as actually realised.** Every figure assumes fills at the modelled
  price, and the 5 bps in `cost_slippage_bps` is an assumption, not a
  measurement. It is inside the paper book's fills by construction and inside
  the live book's by definition, but nothing here measures live slippage against
  intended entry.
- **The intraday monthly figures at ₹20,000.** The 109.9 closes/month rate is
  extrapolated from 9 sessions over 13 days, the book is PAPER, and
  `intraday_capital` is ₹100,000 against a ₹30,000 account — the landmine Stage
  0 flagged. Per-trade R stands; the monthly rupee figures do not, and are
  omitted for that reason.
- **Whether `max_position_pct` 20% is the right ceiling.** It is enforced and it
  binds; whether it should is a Stage 4 question.

**Recommends:**

- **No config change in this stage.** Read-only, and the one change worth making
  depends on an unknown (target vs hit-rate trade-off) that Stage 3 exists to
  settle.
- **For Stage 4, correct the roadmap's item 2.** "Clip-size floor" as written
  makes friction worse under fixed-rupee sizing. The friction lever at ₹20,000
  is `risk_target_atr_mult` (currently 3.0 against `risk_stop_atr_mult` 1.5,
  i.e. exactly 2R), not clip size.
- **For Stage 3, add one question to the replay spec:** what does the hit rate do
  when the target moves from 1.9R to 2.5R on the same detections? It is the only
  number that decides whether the swing book has a configuration at ₹20,000.
- **Note for whoever revisits sizing:** `max_positions_risk_on` 8 cannot be
  funded at the ₹4,000 clip ceiling on ₹20,000. Five, not eight.

**Gate: NEEDS DECISION.** The unit economics are established and reproducible.
The decision they force — run the swing book at 2.5R, or stop running it at
₹20,000 — is Vipin's, and it should not be taken before Stage 3 prices the
target-versus-hit-rate trade.

---

## 2026-08-14 — Stage 2c (planned-target shortfall) — the planned target is not below break-even by drift; 1.9048R is a CONSTANT set by the regime stop multiplier, and it carries no cost basis at all

Branch `diagnostic/planned-target-shortfall` off `main`. **READ-ONLY — no source
file, config key or database row was modified.** `git status --porcelain` empty
at the end of the session.

**Ran:**

```bash
git checkout -b diagnostic/planned-target-shortfall main
```

Plus seven read-only scratchpad scripts, no source file touched: `probe.py`,
`planned_r.py`, `basis.py`, `implied.py`, `actionable.py`, `gatebar.py`,
`reconcile.py` / `reconcile2.py`, `hitrate.py`, `payoff.py`. Cost figures are
taken from the production functions themselves — `intraday.cost_model.entry_leg`
/ `exit_leg` / `round_trip` imported, never reimplemented — and the mechanism in
§1 is verified by **calling `compute_trade_levels()`**, not by reading it.

---

### POPULATIONS

| tag | table | n | what a row is |
|---|---|---|---|
| **[PLANS]** | `signal_output_daily`, last 30 dates present | 1002 raw / **995** with a coherent geometry / 13 dates (28-Jul → 13-Aug) | one evening plan |
| **[CLOSED-SW]** | `closed_positions`, `framework='SWING'` | **80** rows / **10** with a full planned geometry | a real swing trade |

**[CLOSED-SW] has grown from 78 to 80 since Stage 2 and 2b ran earlier today.**
MANAPPURAM (−₹144.45) and PPLPHARMA (+₹171.78) both closed on 2026-08-14. Rows
carrying `planned_stop_at_entry` went 8 → 10. Every Stage 2b figure anchored on
n=78 is now one session stale; that is not an error in 2b, but no number below
is quoted at n=78.

---

### 1 — WHAT SETS THE PLANNED TARGET *(the first question)*

The whole chain, and it contains no cost model anywhere:

```
compute_msl.compute_trade_plan()            swing/compute/compute_msl.py:2121
  -> analysis.risk_model.compute_trade_levels(                      :2151-2157
         entry_price = ez_low, anchor_price = ez_low,
         structure_stop = supertrend, regime = regime_ctx["regime"])
  -> levels.target                                                  :2168
  -> signal_output_daily.planned_target
```

Inside `compute_trade_levels`, two lines decide everything:

```python
regime_k  = REGIME_STOP_MULT.get((regime or "NEUTRAL").upper(), 1.0)   # :158
atr_stop  = anchor - (p["stop_atr_mult"] * regime_k * atr_abs)         # :161
target    = anchor + (p["target_atr_mult"] * atr_abs)                  # :188
```

**The stop is scaled by `regime_k`. The target is not.** `compute_trade_plan`
passes `entry_price = anchor_price = ez_low`, so when the ATR stop is taken the
planned R is not a distribution at all — it is arithmetic:

```
planned R = target_atr_mult / (stop_atr_mult * regime_k) = 3.0 / (1.5 * regime_k)
```

Verified **through the function**, entry = anchor = 100, ATR = 3:

```
     TRENDING     stop   95.72  target  109.00  rr 2.1050
     RISK ON      stop   95.50  target  109.00  rr 2.0000
     NEUTRAL      stop   95.28  target  109.00  rr 1.9050   <<
     RECOVERING   stop   94.83  target  109.00  rr 1.7390
     RISK OFF     stop   94.38  target  109.00  rr 1.6000
```

`risk_stop_atr_mult` 1.5 and `risk_target_atr_mult` 3.0 are both **present in
`system_config`** at their code defaults. **All 1000 rows of
`signal_output_daily.regime` in the window read `NEUTRAL`**, and every one of the
13 dates is 100% NEUTRAL. `market_regime.computed_regime` agrees. No regime value
in the data falls outside `REGIME_STOP_MULT`, so nothing is silently taking
`k=1.0` through the `.get(..., 1.0)` fallback.

**So the "1.90R median planned target" is not drift, not degradation, and not a
distribution whose middle happens to sit at 1.90. It is the single number
`3.0/(1.5×1.05) = 1.9048`, produced identically on every plan that takes the ATR
stop, and it has been that number on all 13 sessions.** The design target of 2R
is only reachable at `regime_k = 1.00`, i.e. **`RISK ON` — one of five regimes,
and not the one the market has been in for any session on record.**

The docstring at `risk_model.py:70-71` states the intent — "tighter stops in
trending markets keep R:R attractive" — so regime moving R:R is deliberate. What
is not stated anywhere is that the DEFAULT regime lands the book below its own
2R design point, and Stage 2b established that this is also below break-even.

---

### 2 — THE DISTRIBUTION OF PLANNED R *(the second question)*

**POPULATION [PLANS], n=995 of 1002 with a coherent geometry.** The 7 excluded
are `risk_too_wide_*` rejects (8.0–10.3% stops against `risk_max_risk_pct` 8.0)
which correctly carry NULL stop and target.

`expected_r` reproduces `(target−entry)/(entry−stop)` on all 995 — max |diff|
**0.0023**, median 0.000244, zero rows over 0.01. The stored column and the
geometry agree; nothing downstream is reading a stale R.

```
                          n     min    p10    p25    MED    p75    p90     max   mean
  ALL plans             995   1.903  1.905  1.905  1.905  2.139  3.066   7.121  2.224
  stop % (planned)      995   1.530  2.603  3.360  4.284  5.269  6.124   7.853  4.329
```

It is **bimodal, not spread**, and the split is exactly the stop source:

```
  stop_source=atr       685   1.903  1.905  1.905  1.905  1.905  1.905   1.907  1.905
  stop_source=structure 310   1.911  2.013  2.192  2.623  3.349  4.407   7.121  2.930
```

**694 of 995 (69.7%) sit in the band 1.90 ≤ R < 1.95** — the ATR-stop constant
(685 rows) plus a handful of structure stops that land just inside it. The 310
supertrend-stop plans are the only source of any R above it, because a tighter
structural stop raises R against an unmoved target.

The median is 1.905 on **every single one of the 13 dates**, without exception:

```
    2026-08-13  n=  82  MED 1.905  p25 1.905  p75 2.414  mean 2.387
    2026-08-12  n=  81  MED 1.905  p25 1.905  p75 2.118  mean 2.151
    2026-08-11  n=  76  MED 1.905  p25 1.905  p75 2.289  mean 2.234
    2026-08-10  n=  78  MED 1.905  p25 1.905  p75 1.905  mean 2.145
    2026-08-07  n=  78  MED 1.905  p25 1.905  p75 2.287  mean 2.197
    2026-08-06  n=  81  MED 1.905  p25 1.905  p75 2.022  mean 2.223
    2026-08-05  n=  81  MED 1.905  p25 1.905  p75 2.108  mean 2.293
    2026-08-04  n=  79  MED 1.905  p25 1.905  p75 1.936  mean 2.207
    2026-08-03  n=  68  MED 1.905  p25 1.905  p75 2.139  mean 2.217
    2026-07-31  n=  73  MED 1.905  p25 1.905  p75 1.940  mean 2.215
    2026-07-30  n=  76  MED 1.905  p25 1.905  p75 2.149  mean 2.216
    2026-07-29  n=  82  MED 1.905  p25 1.905  p75 2.112  mean 2.233
    2026-07-28  n=  60  MED 1.905  p25 1.905  p75 1.906  mean 2.174
```

Stage 2b's `13-Aug n=82, planned target median 1.90R` reproduces exactly.

---

### 3 — WHAT FRACTION FALLS BELOW BREAK-EVEN *(the third question)*

Each plan judged against **its own** planned stop and **its own** clip, sized by
the production rule at ₹20,000 with an empty book
(`analysis/portfolio_constraints.py:220-226`: `min(qty_by_risk, qty_by_maxpos)`,
`risk_pct_per_trade` 1.0 → ₹200 risk, `max_position_pct` 20% → ₹4,000 ceiling).
Friction per plan is `(entry_leg+exit_leg)/risk_rupees` at the plan's own entry
price and quantity — CNC, the swing product.

**207 of 995 plans (20.8%) cannot be funded at ₹20,000 at all:**

```
   unfundable n=207
      151  risk budget Rs200 < risk/share & clip ceiling Rs4,000 < share price
       56  clip ceiling Rs4,000 < share price
   their share price: median Rs7,678  max Rs45,554
   their risk/share : median Rs292.51  (budget is Rs200)
```

The remaining **788** are scored:

```
  clip Rs                  n=788  min 1397  p25 2830  MED 3326  p75 3760  max 4000
  friction R (ledger)      n=788  min 0.106 p25 0.131 MED 0.157 p75 0.200 max 0.447
  friction R (gate)        n=788  min 0.119 p25 0.150 MED 0.179 p75 0.231 max 0.501
  required target (ledger) n=788  min 1.765 p25 1.827 MED 1.893 p75 2.000 max 2.617
  required target (gate)   n=788  min 1.798 p25 1.874 MED 1.948 p75 2.077 max 2.753
  net R @planned (ledger)  n=788  min -0.117 p25 +0.002 MED +0.034 p75 +0.131 max +1.878
  net R @planned (gate)    n=788  min -0.161 p25 -0.020 MED +0.016 p75 +0.104 max +1.820
  break-even hit% (ledger) n=788  p25 36.04  MED 38.81  p75 39.93  max 44.04
  break-even hit% (gate)   n=788  p25 36.75  MED 39.47  p75 40.70  max 45.53
```

**THE ANSWER, both bases, at the 40% design hit rate:**

| basis | plans below break-even |
|---|---|
| **LEDGER** (statutory only) | **191 of 788 = 24.2%** |
| **GATE** (statutory + 5 bps slippage) | **289 of 788 = 36.7%** |

By planned-R band (shares are of the 788 fundable), which shows where the loss
sits:

```
    1.90 <= R <  1.95 :  526 ( 66.8%)  med net(ledger) +0.0198  med net(gate) -0.0013
    1.95 <= R <  2.00 :   15 (  1.9%)  med net(ledger) +0.0467  med net(gate) +0.0306
    2.00 <= R <  2.50 :   93 ( 11.8%)  med net(ledger) +0.1213  med net(gate) +0.1001
    2.50 <= R < 99.00 :  154 ( 19.5%)  med net(ledger) +0.4380  med net(gate) +0.3989
```

**Almost all of the loss is in the 1.90-band, but not all of it** — checked
rather than assumed:

```
  ledger: 191 below break-even of 788   planned-R range 1.903 .. 2.289
          183 in the 1.90-1.95 band, 8 outside it
  gate:   289 below break-even of 788   planned-R range 1.903 .. 2.289
          274 in the 1.90-1.95 band, 15 outside it
```

The 8 (resp. 15) exceptions reach up to a planned 2.289R and still fail, because
a wide stop or a small clip pushes their own required target above it. **No plan
above 2.289R fails on either basis.**

That band is two thirds of the book and its median net is `+0.0198R` on the
ledger basis and **`−0.0013R` on the gate basis — it straddles zero.** The
1.9048R constant is not
comfortably profitable or clearly unprofitable; it sits ON the line, and which
side it lands on is decided by the cost basis chosen and by the individual
plan's stop width.

This is a **less severe** reading than Stage 2b's headline. 2b compared one
median (1.90R) against one break-even computed at one clip and one stop
(₹2,733 / 4.34% → 1.975R) and concluded the book plans below break-even.
Matching each plan to its OWN required target, 75.8% of fundable plans clear on
the ledger basis. 2b's number is a median-against-median comparison; both are
correct answers to different questions, and the per-plan form is the one that
says how much of the book is affected.

---

### 4 — WHICH COST BASIS EACH SIDE USES *(the fourth question)*

**The planner uses NEITHER. It has no cost basis at all.**
`analysis/risk_model.py` — the module that produces `planned_target` — imports
only `dataclasses`. Every cost token is absent:

```
   'cost_model' appears in risk_model.py: False
   'round_trip' appears in risk_model.py: False
   'entry_leg'  appears in risk_model.py: False
   'exit_leg'   appears in risk_model.py: False
   'charges' / 'brokerage' / 'stt' / 'slippage' / 'dp_per_sell' / 'friction': all False
   module-level imports: ['from __future__ import annotations',
                          'from dataclasses import dataclass, asdict']
```

| side | what it uses | where |
|---|---|---|
| **planned_target** | **no cost model** — `anchor + 3.0×ATR` | `analysis/risk_model.py:188` |
| **Stage 2b break-even** | **LEDGER** basis, statutory only | `tools/unit_economics.py:102` → `entry_leg`+`exit_leg` → `friction_r` at `:110-111` |
| **expectancy ledger** | **LEDGER** basis | `tools/expectancy_ledger.py:80` imports `entry_leg, exit_leg` |
| **intraday cost gate** | **GATE** basis, +slippage | `intraday/cost_model.py:128` `round_trip`, slippage added at `:161`, summed at `:170` |
| **swing sizing cost gate** | GATE basis — but **DISABLED** | `analysis/portfolio_constraints.py:297`; `sizing_max_cost_r = 0` in `system_config` |

**So the 1.90 vs 1.975 gap is NOT a units mismatch.** It cannot be: a units
mismatch needs two cost models, and the planning side uses zero. The brief's
hypothesis is refuted, and refuted in the adverse direction — priced on the
GATE basis instead, the required target at the same anchors **rises**:

```
      clip prod  stop% |  ledger%  fricR  reqTgt  net@1.905 |    gate%  fricR  reqTgt  net@1.905
     2,733  CNC   4.34 |   0.8240  0.190   1.975    -0.0279 |   0.9240  0.213   2.032    -0.0510
     2,733  CNC   6.23 |   0.8240  0.132   1.831    +0.0297 |   0.9240  0.148   1.871    +0.0136
     4,000  CNC   4.34 |   0.5985  0.138   1.845    +0.0240 |   0.6985  0.161   1.902    +0.0010
     4,000  CNC   6.23 |   0.5985  0.096   1.740    +0.0659 |   0.6985  0.112   1.780    +0.0498
```

At Stage 2b's own anchors the shortfall goes from **−0.028R to −0.051R**, not to
zero. (Stage 2b printed −0.030R; −0.0279R here is the same figure computed at
1.9048R rather than a rounded 1.90R.)

**A correction to how the 0.206% / 0.106% pair should be read.** Those are **MIS**
numbers and the ratio between them does not transfer to the swing book:

```
   MIS  Rs 2,000   ledger 0.1060%   gate 0.2065%   ratio 1.95x   delta +0.1005pp
   MIS  Rs 6,500   ledger 0.1063%   gate 0.2063%   ratio 1.94x   delta +0.1000pp
   CNC  Rs 2,000   ledger 0.9740%   gate 1.0745%   ratio 1.10x   delta +0.1005pp
   CNC  Rs 2,733   ledger 0.8240%   gate 0.9240%   ratio 1.12x   delta +0.1000pp
   CNC  Rs 4,000   ledger 0.5985%   gate 0.6985%   ratio 1.17x   delta +0.1000pp
```

The two bases differ by a **constant +0.100pp of position** — slippage is
`turnover × 5bps/10000` = 0.10% of position on both products
(`cost_model.py:161`). On MIS that doubles the cost (1.94x); on CNC it is a
1.10–1.17x adjustment, because CNC's statutory base is 6–9x larger. **The "two
cost models disagree by 2x" framing is an MIS fact and is nearly irrelevant to
the swing book.**

---

### 5 — RECORDED, NOT CHANGED: the slot/clip funding arithmetic

As the brief asks, and it is worse than the one line 2b recorded — `NEUTRAL` and
`RISK OFF` are over-committed too, and NEUTRAL is the only regime observed:

```
  max_positions_risk_on    = 8  ->  8 x Rs4,000 = Rs32,000 vs Rs20,000 cash (OVER by Rs12,000)
  max_positions_neutral    = 6  ->  6 x Rs4,000 = Rs24,000 vs Rs20,000 cash (OVER by Rs 4,000)
  max_positions_risk_off   = 6  ->  6 x Rs4,000 = Rs24,000 vs Rs20,000 cash (OVER by Rs 4,000)
  clip ceiling funds 5 concurrent CNC positions.
  at the MEDIAN planned clip of Rs3,326, cash funds 6 concurrent positions.
```

`max_position_pct` 20.0 and `risk_pct_per_trade` 1.0 confirmed present in
`system_config`; the ceiling is enforced at
`analysis/portfolio_constraints.py:223`. **Not changed.**

---

### 6 — FOUND ALONG THE WAY

**F-1 — Stage 2 C.4's `SWING all 78 39.7%` does not reproduce, and Stage 2b's
headline conclusion rests on it.** On the 78 rows Stage 2 would have seen, six
natural definitions of "hit" were tested and none returns 39.7%:

```
n=78 (Stage 2's view)
  realized_pnl > 0                              35/ 78 =  44.87%
  realized_pnl - charges > 0                    32/ 78 =  41.03%
  r_multiple > 0                                 8/ 78 =  10.26%
  pnl_pct > 0                                   35/ 78 =  44.87%
  exit_price > entry_price                      35/ 78 =  44.87%
  realized_pnl >= 0                             35/ 78 =  44.87%
  (39.7% would require 31/78 = 39.74%. No predicate tested yields 31.)
```

Stage 2b's conclusion is "break-even 39.66% vs measured 39.7% — four hundredths
of a percentage point." Against the nearest reproducible reading, **44.87%
gross**, the same break-even leaves **5.2 points of headroom, not 0.04.** I
cannot show Stage 2's number is wrong — its scratchpad scripts are gone and I
did not rerun them — only that it does not reproduce from `closed_positions`
today under any definition I tested. **This is flagged, not resolved.**

**F-2 — and the deeper problem: the break-even identity's payoff assumptions are
both violated, in the same direction.** `h* = (1+friction)/(1+target)` assumes
winners pay exactly `target` R and losers exactly 1R. **POPULATION [CLOSED-SW],
the 10 rows with a full planned geometry:**

```
   symbol        plannedR   grossR  storedR  target?   stop?
   PPLPHARMA        1.905    2.229    2.095     True   False
   BHEL             1.905    0.081    0.081    False   False
   GABRIEL          1.067    0.203    0.192    False   False
   ETERNAL          1.235    0.289    0.289    False   False
   CIPLA            1.291    0.089    0.089    False   False
   PPLPHARMA        1.036    0.665    0.863    False   False
   TRAVELFOOD       1.597    0.267    0.267    False   False
   MANAPPURAM       1.132   -0.750   -0.750    False   False
   KIMS             2.797    0.394    0.394    False   False
   VIJAYA           1.079    0.452    0.452    False   False

   positive-R rate         9/10 = 90.0%
   PLANNED-TARGET hit rate 1/10 = 10.0%   <- the 'h' the identity actually means
   planned-STOP hit rate   0/10 =  0.0%   <- the '1R loser' the identity assumes
   mean winner  +0.519R  median +0.289R  (identity assumes +1.263R)
   mean loser   -0.750R  median -0.750R  (identity assumes -1.000R)
```

**One of ten trades reached its planned target. None reached its planned stop.**
Every other exit was resolved by the exit ladder somewhere in between. A "hit
rate" counting `realized_pnl > 0` and a break-even assuming a `1.9R` winner are
**not the same quantity** — this is the CLAUDE.md landmine "a gate and the thing
it gates must be the SAME QUANTITY", now found in the break-even identity itself.

The direct consequence for Stage 3: **the planned target is very nearly
irrelevant to what this book actually realises.** Moving it from 1.9R to 2.5R
changes the exit price of trades that reach it, and 1 of 10 did. Whatever Stage 3
measures about target-versus-hit-rate must be measured against the **exit
ladder**, not against the target in isolation.

**F-3 — `implied_rr`, the quantity the entry gate actually tests, has a median of
0.777 and its bar is 0.8.** `expected_r`/`planned_target` describe R at the entry
zone; `generate_signals.py:800` gates on `implied_rr`, which re-anchors at the
quoted price (`risk_model.py:44-56` — a deliberate chase penalty).

```
  planned R (at zone low)        n=991  p25 1.905  MED 1.905  p75 2.139
  implied_rr (at quoted price)   n=991  p25 0.681  MED 0.777  p75 0.787
  implied_rr BELOW planned R: 953 of 991 = 96.2%   (median dist_entry_pct +3.25%)
  implied_rr < 1.975 : 951 of 991 = 96.0%
```

The bar it is tested against, read from `system_config`, **not** the code default:

```
   min_rr_to_enter                =   1.0   (system_config)
   min_rr_to_enter_NEUTRAL        =   0.8   (system_config)   <- the effective bar
   min_rr_to_enter_TRENDING       =   0.9      min_rr_to_enter_RISK_ON    = 1.1
   min_rr_to_enter_RECOVERING     =   1.3      min_rr_to_enter_RISK_OFF   = 1.5
```

**`min_rr_to_enter_NEUTRAL` is 0.8 in `system_config`, overriding the 1.0 code
default at `generate_signals.py:191`.** So in the only regime on record, a plan
whose R:R at the quoted price is 0.8 clears the entry gate, against a break-even
that needs ~1.9–2.0R. The in-zone slice is better but still short — n=47,
implied_rr median 1.741, 66% below 1.975R. **Caveat: `implied_rr` is computed
against the evening close, not against the fill; the daemon re-evaluates at live
price. This is not a claim about realised R.** It is a claim about the bar.

---

**Could not determine:**

- **Whether Stage 2 C.4's 39.7% was ever right.** F-1 shows it does not
  reproduce; it does not show what produced it. Stage 2's `b5_c.py` / `c3_c4b.py`
  were scratchpad scripts and are gone. **Until this is settled, Stage 2b's
  "break-even hit rate and measured hit rate are the same number" should not be
  quoted as established.**
- **What the correct hit rate for the break-even identity even is.** F-2 says the
  honest `h` is the planned-target hit rate, which is **1 of 10**. At n=10 that
  is not a measurement, and the identity cannot be evaluated against it.
- **Whether the ledger or the gate cost basis is right for judging a PLAN.**
  Stage 2b's P.1 argued convincingly that `entry_leg+exit_leg` is right for a
  REALISED outcome and `round_trip` for a PRE-TRADE gate. A planned target is a
  pre-trade object, which argues for the gate basis and a 36.7% below-break-even
  figure rather than 24.2%. Both are reported above; **the choice is not made
  here** and it moves the answer by 12.5 percentage points.
- **Whether the 207 unfundable plans matter.** They are 20.8% of what the
  pipeline publishes each evening and cannot be taken at ₹20,000 under the
  current sizing rule. Whether they are also the ones the ranking layer would
  have chosen is not tested — Stage 2 C.1/C.2 already found the swing ranking
  layer does not order outcomes at n=1386.
- **Whether the supertrend stop's higher R is real or a selection effect.** The
  310 `stop_source=structure` plans carry median 2.623R purely because a tighter
  stop divides an unchanged target distance. Nothing here tests whether those
  tighter stops survive contact.
- **Live slippage.** Unmeasured, exactly as Stage 2b recorded. The 0.100pp gap
  between the two cost bases IS the slippage assumption, so the 24.2% vs 36.7%
  spread is entirely a function of an unvalidated 5 bps.
- **Whether `regime_k` scaling the stop but not the target is intended.**
  `risk_model.py:70-71` documents regime moving R:R deliberately. Nothing
  documents that the default regime lands below the 2R design point. I did not
  find a decision record either way and did not assume one.
- **Anything about the intraday book.** Not examined; this stage is swing-only.

**Recommends:**

**No action, and specifically no change to `risk_target_atr_mult`,
`REGIME_STOP_MULT` or `min_rr_to_enter_NEUTRAL`.** The brief forbids it and the
evidence does not support it yet. Recorded for whoever runs Stage 3:

1. **The lever is one constant, and it is not the one 2b named.** 2b proposed
   `risk_target_atr_mult` (currently 3.0). The measurement above says planned R
   is `target_atr_mult / (stop_atr_mult × regime_k)` — **`regime_k` = 1.05 in the
   only regime on record is the whole of the 2.0 → 1.9048 gap.** Raising the
   target multiplier and neutralising the regime multiplier are different
   changes with different side effects: `regime_k` also sets the STOP WIDTH,
   hence position size, hence friction in R. Neither should be moved before
   Stage 3.
2. **Stage 3 must price the target against the EXIT LADDER, not in isolation**
   (F-2). One trade in ten reached its target. A replay that moves the target
   and holds the exit policy fixed will measure almost nothing.
3. **Settle F-1 first.** Stage 2b's decision-grade conclusion rests on a number
   that does not reproduce. That is a one-query check and it should precede any
   target change, because 44.87% and 39.7% point to opposite decisions.
4. Note for whoever revisits sizing: the over-commitment in §5 applies to
   `max_positions_neutral` (6) as well, not only `max_positions_risk_on` (8).

**Gate: PASS** — the four questions asked are answered with commands and raw
output behind every number. **F-1 is escalated: it is a live contradiction with
the prior entry's headline, and it is not resolved here.**

---

## 2026-08-14 — Stage 2d (diagnostic, swing exit ladder) — `closed_positions` records the wrong exit reason for every swing trade; the ladder fired on 11 of 11 and give-back is the rung

**Brief:** parameterise `exit_ladder_replay`'s hardcoded framework (test first,
no live exit logic), then answer five questions on the swing book. Measurement
only, no recommendations, `n<20` flagged as insufficient rather than ranked.

**Read-only throughout.** The only files changed are
`backend/tools/exit_ladder_replay.py` and its test. No exit logic, no config,
no migration. `python -m tools.verify` — **all 434 checks pass across 55
modules.**

---

### 0 — THE TOOL FIX, AND WHY SIX TESTS DID NOT CATCH THE BUG

`_rows()` was hardcoded `.eq("framework", "INTRADAY")` at :99, while the tool's
name, its `--days` flag and every line it prints are book-agnostic. Pointed at
a swing question it returned a confident, correctly-formatted answer about the
intraday book.

Six tests passed over it because the fake Supabase they used **accepted `.eq()`
and ignored it**. A filter that is never applied cannot be caught by a mock
that never applies filters — the same shape as the `check_selects` defect in
CLAUDE.md, one layer down. Both are fixed: `--framework {INTRADAY,SWING}`, and
a fake `_Q.eq()` that actually filters. Demonstrated failing first:

```
✗  exit ladder replay  (2/8 failed)
     the framework filter selects the book that was asked for
       TypeError: _rows() takes 2 positional arguments but 3 were given
     a swing-only book is empty to an intraday replay
       TypeError: replay() got an unexpected keyword argument 'framework'
```

then passing (8 checks). `GIVEBACK_KEYS` was added with it: the two books do
**not** share threshold keys (`exit_giveback_*` min 0.5R vs
`intraday_giveback_*` min 1.0R), so parameterising the framework alone would
have replayed the swing book against the intraday threshold and labelled the
result SWING.

---

### 1 — THE POPULATION. The book is 81 rows, not 80, and it is two books

```
  SWING closed rows: 81
    source=manual  70   2025-12-31 - 2026-03-09   r_multiple 0/70, planned_stop 0/70, charges 0/70
    source=kite    11   2026-07-13 - 2026-08-14   r_multiple 11/11, planned_stop 11/11, charges 11/11
```

**Three positions closed 14-Aug, not two: MANAPPURAM, PPLPHARMA *and AIIL*.**
The brief said 78 -> 80; it is 78 -> 81.

The 70 legacy rows carry **no plan geometry at all** — no stop, no target, no
`r_multiple`, no `charges`. Every R-denominated and net-of-cost question in this
brief is therefore unanswerable on them, and they are never pooled below. Their
`exit_reason` values are free-text human labels (`TRAIL SL HIT`,
`STRATEGY ROTATION`, `4.5% loss`), not the ladder's action codes. Only the 11
`source=kite` rows were ever under the exit ladder.

For completeness, the legacy book in rupees — **not R, and not comparable to
anything below**:

```
  exit_reason                  n  sum Rs gross   mean Rs  win rate
  TRAIL SL HIT                37         -2702       -73     40.5%
  STRATEGY ROTATION           12         +7062      +588     83.3%
  TIME EXIT                   10        -11069     -1107      0.0%
  STOP LOSS HIT                9         -2569      -285     22.2%
  MANUAL DISCIPLINE EXIT       1          -388      -388      0.0%
  4.5% loss                    1          -127      -127      0.0%
```

---

### 2 — Q1: THE EXIT-REASON HISTOGRAM. The stored one is wrong on all 11

`closed_positions.exit_reason` for the system-managed book has exactly one
bucket:

```
  exit_reason        n  sum grossR  sum netR  mean grossR   median  sum Rs net
  BROKER_EXIT       11      +4.349    +2.802       +0.395   +0.289        +621
```

That reads as "the ladder never fired." **It is an artefact, and it is false.**

`reconcile_with_broker` writes `pos.get("exit_signal") or "BROKER_EXIT"`
(`position_lifecycle.py:1233`). `manage_open_positions` sets `exit_signal`
(`:1634`) — but the **daemon**, the process that actually evaluates swing exits
every 15s during market hours, does not. Its live-exit branch writes
`{"current_qty": 0, "status": "CLOSING"}` (`intraday/engine.py:1969`) and no
`exit_signal`. So every exit the daemon executes reconciles as `BROKER_EXIT`,
indistinguishable from an operator's manual sale.

This is the *same* two-callers-of-one-decision defect that `engine.py:1884`
documents having fixed for the order whitelist, surviving in the attribution
write beside it.

**The real attribution survives in `intraday_broker_log`, which records the
rung name in the order's own reason string.** Joining it to the 11 closes:

```
symbol      entry       exit        closed_positions  WHAT ACTUALLY FIRED                    R
PPLPHARMA   2026-07-13  2026-07-30  BROKER_EXIT       BOOK_PARTIAL->BOOK_PARTIAL->TRAIL_SL   +2.095
GABRIEL     2026-07-30  2026-07-31  BROKER_EXIT       TRAIL_SL                               +0.192
TRAVELFOOD  2026-08-04  2026-08-06  BROKER_EXIT       EXIT_GIVEBACK                          +0.267
BHEL        2026-07-23  2026-08-03  BROKER_EXIT       EXIT_STALL                             +0.081
KIMS        2026-08-05  2026-08-06  BROKER_EXIT       EXIT_GIVEBACK                          +0.394
ETERNAL     2026-07-30  2026-08-10  BROKER_EXIT       EXIT_GIVEBACK                          +0.289
CIPLA       2026-07-30  2026-08-10  BROKER_EXIT       EXIT_STALL                             +0.089
VIJAYA      2026-08-10  2026-08-12  BROKER_EXIT       EXIT_GIVEBACK                          +0.452
MANAPPURAM  2026-08-04  2026-08-14  BROKER_EXIT       EXIT_STALL                             -0.750
PPLPHARMA   2026-07-31  2026-08-14  BROKER_EXIT       BOOK_PARTIAL->EXIT_GIVEBACK            +0.863
AIIL        2026-08-12  2026-08-14  BROKER_EXIT       EXIT_GIVEBACK                          +0.377

   terminal rung        n     sum R    mean R
   EXIT_GIVEBACK        6    +2.642    +0.440     <<  the rung that fires most
   EXIT_STALL           3    -0.580    -0.193
   TRAIL_SL             2    +2.287    +1.144
   TOTAL               11    +4.349
```

**ANSWER TO Q1: `EXIT_GIVEBACK`, 6 of 11 (55%). The ladder decided 11 of 11
outcomes — the brief's premise is correct and the stored exit reason is the
only thing that says otherwise.** `EXIT_STOP`, `EXIT_TARGET`, `EXIT_TIME` and
`EXIT_DETERIORATION` fired **zero** times.

---

### 3 — Q2: WHAT GIVE-BACK CUT. All six were in profit; only one has resolved

The live rung logs its own arithmetic, so "in profit at the time" is read
directly off the decision, not inferred:

```
  TRAVELFOOD  peaked 0.61R (+2.92%)   back to +0.27R (+1.28%)  56% handed back
  KIMS        peaked 0.79R (+2.15%)   back to +0.39R (+1.07%)  50%
  ETERNAL     peaked 0.60R (+4.41%)   back to +0.28R (+2.06%)  53%
  VIJAYA      peaked 0.92R (+7.40%)   back to +0.45R (+3.67%)  50%
  PPLPHARMA   peaked 1.34R (+12.49%)  back to +0.61R (+5.67%)  55%
  AIIL        peaked 0.74R (+4.17%)   back to +0.37R (+2.08%)  50%
```

**6 of 6 cuts were in profit.** Continuing each position past its real exit
over real daily bars, with give-back disabled and everything else held:

```
symbol      cut on      R at cut  forward outcome          R then   delta  sess
TRAVELFOOD  2026-08-06    +0.267  STILL OPEN @08-13        +0.583  +0.316     5
KIMS        2026-08-06    +0.394  EXIT_STOP  (resolved)    +0.073  -0.321     2
ETERNAL     2026-08-10    +0.289  STILL OPEN @08-13        +0.534  +0.245     3
VIJAYA      2026-08-12    +0.452  STILL OPEN @08-13        +0.861  +0.409     1
PPLPHARMA   2026-08-14    +0.863  NO FORWARD BARS               -       -     -
AIIL        2026-08-14    +0.377  NO FORWARD BARS               -       -     -
```

**ANSWER TO Q2 — INSUFFICIENT, and this is the honest answer.** Six cuts, all
in profit, but **exactly one (KIMS) has a resolved forward outcome, and it says
the cut was right** (+0.321R saved). Two are unmeasurable — they closed today
and `stock_data_daily` ends 2026-08-13. The remaining three are marked to
market on **one to five sessions** of follow-through against a 1–3 week
horizon; they are not results. Nominal sum over the four measurable rows is
**+0.649R against the rung**, and it inverts if any of those three
mark-to-market positions gives it back.

n=6 against the brief's n<20 floor. **Flagged insufficient, not ranked.** This
is the number that decides whether the rung pays and the book is still too
young to produce it.

---

### 4 — Q3: THE BREAKEVEN RUNG. n=0, twice over

```
  EXIT_STOP terminal on 0/11 — no swing position has ever exited on a stop
  positions reaching planned stop: 0/11 (checked against daily bar lows)
```

**ANSWER TO Q3: zero trades were stopped at breakeven, so "how many later
reached 1.5R" has no population.** Not a small number — no number.

It is unanswerable a second way, which matters more. The standalone breakeven
ratchet (`3c`) is a `TRAIL_SL` action; it places no order, so it leaves no row
in `intraday_broker_log`, and it writes `breakeven_moved` to `open_positions` —
**a column `closed_positions` does not have.** Confirmed live:

```
  breakeven_moved        open=True   closed=False
  trail_activated        open=True   closed=False
  active_sl              open=True   closed=False
  partial_booked_qty     open=True   closed=False
  exit_signal            open=True   closed=False
```

Whether the ratchet ever engaged on a closed position is **destroyed at close**
and cannot be recovered from any table. The mechanism does work — intraday
SWIGGY currently carries `breakeven_moved=True` — but no closed swing row can
show it. Of the six current live SWING positions (SUMICHEM, SCI, CARBORUNIV,
TRAVELFOOD, GABRIEL, AUBANK), none has `breakeven_moved=True` and all six have
`active_sl == planned_stop`.

---

### 5 — Q4: THE COUNTERFACTUAL, and a method that had to be thrown away

**A full-path daily-bar replay was built, run, and discarded. It is reported
here because it looked right and was not.** It drives the real `evaluate_exit`
over daily OHLC from entry, probing low/high/close, carrying `active_sl`,
`high_water_mark` and partial state. Scored against the order log:

```
  daily-bar replay vs the live order log: 2/11 agree
  give-back: live fired 6, replay predicted 3, overlap 1 (ETERNAL)
```

It called give-back on PPLPHARMA-1 and BHEL where the live system actually
fired `BOOK_PARTIAL`/`TRAIL_SL` and `EXIT_STALL`, and missed five real
give-backs. Daily OHLC cannot see the tick path the 15s loop sees, and the live
rung reads the daemon's *running* `high_water_mark`, which understates the true
bar high on 6 of 11 (median 0.030R, worst 0.565R; GABRIEL's never left the
entry price). **Its counterfactual totals are not reported. A replay that
disagrees with ground truth on 9 of 11 cannot price a rung.**

What survives is the counterfactual anchored on ground truth — start from the
real exit, disable the rung that caused it, walk forward:

```
   rung             fired   D book R if disabled        basis
   EXIT_GIVEBACK        6   +0.649R over 4 of 6         1 resolved, 3 mark-to-market, 2 no bars
   EXIT_STALL           3   -0.537R over 2 of 3         1 resolved, 1 mark-to-market, 1 no bars
   BOOK_PARTIAL         3   NOT REPORTED                needs a full-path replay (above)
   TRAIL_SL             2   NOT REPORTED                needs a full-path replay (above)
   EXIT_STOP            0   +0.000R exactly             never fired
   EXIT_TARGET          0   +0.000R exactly             never fired
   EXIT_TIME            0   +0.000R exactly             never fired
   EXIT_DETERIORATION   0   +0.000R exactly             never fired
   breakeven ratchet    ?   +0.000R on the book         no stop-out exists for it to have changed
```

**ANSWER TO Q4:** four of the eight rungs are provably inert on this book —
they have never fired, so disabling them changes nothing, exactly. Two more
(partial, trail) cannot be priced without a replay fidelity this book does not
support. Only give-back and stall have measurable effects, both on n<=6 with
1–5 sessions of follow-through, and **they point in opposite directions and
roughly cancel (+0.111R net).** Every measurable bucket is below the n<20
floor. **Nothing here ranks.**

Book context for the 11, recomputed at n=11:

```
  reached planned TARGET (intraday touch)  3/11      exited at or above target  1/11
  reached planned STOP                     0/11
  median planned R 1.291 | median actual R +0.289 | median winner +0.333 (n=10 winners)
```

The brief's "1 of 10 reached its planned target" is the *exit-price* reading;
3 of 11 touched the target intraday and gave it back. Both are true and they
are different predicates — the same failure mode §6 is about.

---

### 6 — Q5: THE 39.7% DISCREPANCY IS RESOLVED, AND STAGE 2b IS STILL WRONG

**F-1 was right that 39.7% does not reproduce under any of the six predicates
it tested, and wrong that it does not reproduce.** The seventh predicate — the
one F-1 did not test — returns it exactly:

```
n=78 (Stage 2's view)
  realized_pnl > 0                              35/78 = 44.87%   <- gross
  realized_pnl - charges > 0                    32/78 = 41.03%   <- treats NULL charges as zero
  net, NULL charges MODELLED at CNC rates       31/78 = 39.74%   <<  C.4's number
```

`charges` is NULL on all 70 legacy rows. Filling them from
`cost_model.round_trip(..., product="CNC")` gives **31/78 = 39.74% ~ 39.7%**.
It is robust: three different charge treatments (fill-missing, entry-priced
both legs, all-modelled) all return 31/78. **C.4's hit rate is correct and its
predicate is net-of-cost.** The rupee means do not reproduce as exactly
(+433/-567 vs C.4's +442/-555), so C.4's exact charge model is not recoverable,
but the hit rate is not in doubt.

**The correct figure, with its predicate, on today's book:**

```
  SWING, n=81, NET of cost (charges modelled where NULL)   33/81 = 40.74%
  SWING, n=81, GROSS                                       37/81 = 45.68%
  SWING, n=78, NET / GROSS                                 39.74% / 44.87%
```

**But resolving F-1 does not rescue Stage 2b — it relocates the error.**
`h* = (1 + friction_R)/(1 + target_R)` is derived in `unit_economics.py:388`
from `h*T - (1-h) = friction_R`, where winners pay `T` R **gross** and losers
1R **gross** and friction is subtracted separately. **h* is therefore a GROSS
hit-rate threshold.** Stage 2b compared it against C.4's **NET** 39.7%.

That is the CLAUDE.md landmine — *a gate and the thing it gates must be the
same quantity* — in a new place. The like-for-like comparison is:

```
   h* @2.0R (clip 2,733, stop 4.34%, friction 0.190R)   39.66%   gross threshold
   measured GROSS hit rate, n=78                        44.87%   -> 5.2 points of headroom
   measured GROSS hit rate, n=81                        45.68%   -> 6.0 points of headroom
```

**Stage 2b's "four hundredths of a percentage point" is an artefact of
comparing a gross bar against a net measurement.** F-1's numerical instinct
(44.87% is the right comparand) was correct; its stated reason (the number does
not reproduce) was not.

**Downstream conclusions that used 39.7% and must be re-read:**

1. **Stage 2b headline — "the swing book's break-even hit rate is 39.66% and it
   hits 39.7%" (ledger heading, and the section "THE FORM THAT IS HARDEST TO
   ARGUE WITH"). WITHDRAWN.** Correct: break-even 39.66% gross vs 44.87%
   measured gross.
2. **Stage 2b "That is not a book with a small edge; it is a book sitting on
   its own fee schedule." WITHDRAWN** — it rests entirely on (1).
3. **Stage 2b's 2.5R argument** ("34.00% vs 39.7%, 5.7 points of headroom") —
   arithmetic unchanged but the baseline moves: against 44.87% gross the
   headroom at 2.5R is **10.9 points**, and the 2R case already has 5.2. The
   *relative* case for a higher target is weaker than 2b presented, because 2R
   is not marginal.
4. **Stage 2c's "the measured book is 39.7% hit and -Rs 12,385 net over 78"** —
   the hit figure is net, the rupee figure is net; internally consistent, but it
   must not be compared to a gross h*.
5. **Stage 2 C.4's own table** — correct as printed, but the column is a NET
   hit rate and is not labelled as one. That mislabelling is the whole defect.
6. **Stage 2c's Recommends item 3 ("Settle F-1 first ... 44.87% and 39.7% point
   to opposite decisions")** — settled here. They are the same book measured
   gross and net; the decision-relevant one against h* is **44.87%**.

Unchanged by all of this: C.4's **-0.004R at the design target**, which is a
specification calculation (40% assumed hit, 2R winner, measured friction) and
never used the measured hit rate at all.

---

### 7 — FOUND ALONG THE WAY

**F-3 — every swing exit is mislabelled `BROKER_EXIT` in `closed_positions`,
and the learning loop reads that column.** §2. The daemon's exit branch
(`engine.py:1969`) omits the `exit_signal` write that
`position_lifecycle.py:1634` performs. Cost: exit attribution for the entire
live swing book is recoverable only by joining `intraday_broker_log` and
parsing an order's free-text reason string. Any tool reading
`closed_positions.exit_reason` to score rungs sees one bucket and concludes the
ladder is inert.

**F-4 — `exit_ladder_replay --framework SWING` reports +0.05R where the truth
is a rung firing six times.**

```
  3 position(s) WOULD HAVE been cut early by give-back:   TRAVELFOOD ETERNAL VIJAYA
  Estimated ceiling on the improvement:    +0.05R
```

The tool floors an affected position at the give-back threshold and never asks
what happened next, so **it is structurally incapable of reporting a give-back
loss** — its output is bounded below by the actual result. Its docstring
already warns it overstates the guard's help; this quantifies that on the swing
book. It also reads the stored `high_water_mark`, which understates the true
peak on 6 of 11.

**F-5 — 844 blocked SELL attempts on the swing book.**

```
   Kite IP allowlist                345
   qty mismatch (already sold)      301
   market closed                    181
   duplicate window                  17
```

The IP-allowlist bucket is the CLAUDE.md `_force_ipv4()` landmine still biting
— `IP (103.197.74.141) is not allowed to place orders for this app` — and on
06-Aug and 10-Aug it blocked exits for a session before they went out. KIMS's
give-back was blocked at 04:44 and placed at 07:02, ~2.5 hours later, at a
price the rung did not choose. **Exit slippage from blocked orders is not
measured anywhere and is not in any R figure in this entry.**

**F-6 — `exit_slip_bps` has no `system_config` row.** It falls to the code
default of 30 in `position_lifecycle.py`. Every other exit key is present.
Harmless today (the default matches `intraday_exit_slip_bps`), noted because a
key nobody writes is the dominant failure mode in this repo.

---

### 8 — COULD NOT DETERMINE

- **Whether the give-back rung pays.** Q2, n=6, one resolved outcome. The
  measurement exists and is stated; the sample does not support a verdict.
- **What `BOOK_PARTIAL` and `TRAIL_SL` are worth.** Both need a full-path
  replay, and the replay built here agrees with ground truth on 2 of 11.
- **Whether the breakeven ratchet ever engaged on a closed position.**
  Structurally unrecoverable — the column is not carried to `closed_positions`.
- **Anything about the 70 legacy rows in R or net terms.** No stop, no target,
  no charges. They are counted and their rupees reported; nothing else is
  honest.
- **Exit slippage from the 844 blocked orders** (F-5). Real, unmeasured, and it
  sits inside every R figure above as noise of unknown sign.
- **Whether the daemon was continuously up during July.** `high_water_mark`
  understates the true peak on 6 of 11, which is consistent with gaps in
  observation, but uptime is not recorded anywhere I could query.
- **The intraday book.** Not examined; this stage is swing-only.

**Recommends: nothing.** The brief forbids it and, with every measurable bucket
below n=20, the evidence would not support one. Recorded for whoever runs
Stage 3:

1. **Q1's premise is confirmed but its answer inverts the stored data.** Any
   Stage 3 work that scores exit rungs must join `intraday_broker_log`, not read
   `closed_positions.exit_reason`. F-3 is the prerequisite for that being
   unnecessary.
2. **Stage 2b's headline is withdrawn (§6).** The swing book has 5.2 points of
   gross headroom over its 2R break-even, not 0.04. Any target-multiple
   decision must start from 44.87%.
3. **The give-back rung fires at 0.5R peak.** On this book 1R is 4–9% of price,
   so the protected band is ~2% wide and six of six cuts were in profit. That
   is a measurement, not a recommendation; it is the thing Stage 3 should be
   powered to test.

**Gate: PASS** — five questions asked, five answered with raw output behind
every number, and three of them answered "insufficient" with the reason stated.
**Q1's stored answer was wrong and the correct one is escalated as F-3.**

---

## 2026-08-14 — Stage 2d-i (diagnostic, blocked exits) — F-5's 844 is 6: only six orders ever reached Zerodha and were refused, every blocked exit eventually filled, realised displacement is **+₹21.24 / +0.27R FAVOURABLE**, and the log proves two daemons were writing to one live account

**Branch:** `diagnostic/blocked-exit-slippage`. **Read-only** — no execution
logic touched, no config written, no order placed. `python -m tools.verify` →
**all 434 checks passed across 55 modules**.

Follows the exit-ladder session's **F-5** and its open item *"exit slippage from
the 844 blocked orders — real, unmeasured, and it sits inside every R figure
above as noise of unknown sign."* It is now measured. The sign is positive and
the magnitude is ₹21.

---

### 0 — TWO CORRECTIONS TO F-5, BEFORE ANY NUMBER BELOW IS READ

**C-1 — `intraday_broker_log.ts` comes back in UTC, and F-5 read it as wall
clock.** `_log()` writes `datetime.now(IST).isoformat()`; the column is
`timestamptz`, so PostgREST returns `+00:00`. F-5's *"KIMS's give-back was
blocked at 04:44 and placed at 07:02"* is **10:14:29 → 12:32:28 IST** — both
inside the session, not before dawn. Every timestamp in this entry is converted.

**C-2 — "844 blocked SELL attempts" counts preflight refusals, not broker
rejections.** 886 of the 892 blocked rows never reached Zerodha: they are
`preflight()` returning locally on a latched flag, once per 15-second cycle, per
symbol. The count is log volume, not lost exits. Today's snapshot reads 845
because the daemon is live and still appending this morning; F-5's 844 was the
same population one session earlier.

---

### 1 — POPULATION

```
total intraday_broker_log rows          989   (under the 1000-row PostgREST cap;
channel  ORDER 939 · GTT 50                    paged anyway, nothing truncated)
action   BLOCKED 886 · PLACED 55 · CANCELLED 19 · FAILED 17 ·
         BLOCKED_PERMANENT 6 · MODIFIED 6
side     SELL 876 · BUY 63 · None 50
ts span  2026-07-28 → 2026-08-14   framework SWING 921 · None 68
```

Snapshot taken 2026-08-14 11:52 IST. Reproduce:

```python
from config import get_supabase
rows = get_supabase().table("intraday_broker_log").select("*").order("id").execute().data
```

---

### 2 — Q1: BLOCKED ATTEMPTS BY REASON AND DATE, AND WHAT BECAME OF THEM

```
  blocked SELL 845   blocked BUY 47   of 939 ORDER rows
  reached the broker and were refused: 6
  refused locally by preflight (latch echo, no broker call): 886
```

Blocked **SELL** attempts by reason — F-5's four buckets, with the IP bucket
split by what actually happened:

```
    334  IP allowlist (per-symbol latch echo)      <- local, pre-fix code
    301  qty mismatch (broker shows less/none)
    182  market closed
     17  duplicate window
      6  IP allowlist (BROKER REJECTION)           <- the only real ones
      5  IP allowlist (account latch echo)         <- local, post-fix code
```

By IST trading date:

```
  2026-07-28  n=   1   market closed:1
  2026-07-29  n= 326   IP allowlist:284 | market closed:39 | IP rejection:3
  2026-07-30  n=  43   qty mismatch:26 | duplicate window:17
  2026-07-31  n=  15   qty mismatch:15
  2026-08-03  n=  57   market closed:51 | qty mismatch:6
  2026-08-04  n= 260   qty mismatch:254 | market closed:6
  2026-08-06  n=  52   IP allowlist:50 | IP rejection:2
  2026-08-10  n=   6   IP allowlist:5 | IP rejection:1
  2026-08-14  n=  85   market closed:85
```

Eight symbols carry all 845: PPLPHARMA 416, TMCV 260, BHEL 57, MANAPPURAM 56,
TRAVELFOOD 51, ETERNAL 2, CIPLA 2, **KIMS 1**. F-5's headline example was
blocked exactly **once**.

**Outcome per distinct (symbol, side, reason) decision** — a block whose
placement came within 120s *before* it is scored filled, because that is the
same decision already executed:

```
reason         attempts decisions filled never  waits(h)
IP_ALLOWLIST        349         7      7     0  0.00, 0.00, 0.00, 0.01, 0.36, 2.30, 22.13
QTY_MISMATCH        301         3      2     1  0.00, 0.04
MARKET_CLOSED       216         6      5     1  0.04, 0.24, 0.27, 0.27, 36.97
DUPLICATE            23         4      3     1  0.00, 0.00, 0.00
OTHER                 3         2      2     0  0.01, 0.01
```

**Every exit decision blocked by the IP allowlist eventually filled — 7 of 7,
none lost.** The three `never` rows are not lost exits: two are TMCV (§7, F-9 —
nothing was held to sell) and one is CARBORUNIV, whose order had gone out
seconds earlier.

---

### 3 — Q2: LTP AT FIRST ATTEMPT vs ACTUAL FILL

Anchored on the **6 `BLOCKED_PERMANENT` rows** — the only events where an order
reached Zerodha — deduplicated to the **4 distinct exit decisions** they
represent (29-Jul refused the same PPLPHARMA `BOOK_PARTIAL` three times). Each
is matched to the position that was *open on that date*, because PPLPHARMA has
two closed positions with different R values (₹11.64 and ₹18.09) and matching on
symbol alone silently picks the wrong one.

`price` in the log is the marketable limit, not the LTP:
`SELL limit = round(ltp × (1 − 30bps), 1)` — so `ltp = price / 0.997`, exact to
the ₹0.1 rounding. `src=TRUE` is `closed_positions.exit_price`, the reconciled
broker average; `src=RECON` is a partial book, where the true fill is not stored
and the placement's own limit is inverted instead.

```
symbol      rejected (IST)   filled (IST)         hrs   ltp@rej      fill src     q    d/sh      1R   rupees       R
PPLPHARMA   07-29 12:57:32  07-30 11:05:07    22.13    194.58    197.69 RECON   7    3.11   11.64    21.77   0.267
KIMS        08-06 10:14:29  08-06 12:32:28     2.30    811.03    811.15 TRUE    1    0.12   21.95     0.12   0.005
TRAVELFOOD  08-06 10:56:04  08-06 11:17:26     0.36   1374.02   1373.70 TRUE    2   -0.32   65.21    -0.64  -0.005
PPLPHARMA   08-10 09:36:01  08-10 09:36:12     0.00    214.64    214.64 RECON   5    0.00   18.09     0.00   0.000

  TOTAL realised displacement: Rs +21.24   +0.27 R   (+ = filled BETTER than the rung's price)
```

**The answer: +₹21.24, +0.27R, favourable.** Three qualifications, all of which
cut against reading that as good news:

1. **n=4, and one decision is 102% of the total.** PPLPHARMA's 22.13-hour block
   spanned an overnight the ladder did not choose and gapped up. The other three
   sum to **−₹0.52**. This is one favourable draw, not a property of blocking.
2. **R is per-share and quantity does not enter it, so the +0.267R is on the 7
   shares booked, not the position.** At position level (15 shares, 1R = ₹11.64)
   it is `21.77 / (15 × 11.64)` = **+0.125R** — PPLPHARMA's recorded **2.095R
   contains ~0.12R of block luck** and would read ~1.97R without it.
3. **The exposure was one-sided in variance, not in mean.** A 22-hour unplanned
   overnight on a 15-share position is a gap risk the give-back rung had
   explicitly decided to stop carrying.

**Consequence for the ledger: F-5's open item is closed.** +₹21.24 across the
entire live swing book's history is not material at ₹20,000, and **no R figure
in Stage 2, 2b, 2c or 2d needs restating** — except PPLPHARMA's own 2.095R,
noted above. The noise was real; it was small.

**KIMS, specifically** — the trade F-5 named: blocked once at 10:14:29 IST,
placed at 12:32:28, filled at **811.15 against 811.03** at the moment the rung
fired. The give-back cost **₹0.12 on one share**.

---

### 4 — Q3: THE LATCH HOLDS PER PROCESS, AND TWO PROCESSES WERE RUNNING

```
  date          rejected  acct-latch  sym-latch  placed   code
  2026-07-29           3           0        284       0   pre-fix (per-symbol)
  2026-08-06           2           0         50       3   pre-fix (per-symbol)
  2026-08-10           1           9          0       6   post-fix (account-wide)
```

**It is not retrying at the broker.** 3 rejections producing 284 echoes, and 2
producing 50, are the **per-(symbol,side) latch** — the exact defect
`_blocked_account` was written to fix. `git log -S"_blocked_account"` dates that
fix to **`ad4a861`, 2026-08-06**, *after* both sessions. On 10-Aug, post-fix, one
rejection produced **nine** echoes, not 284. The fix works.

**But the echoes are still DB writes.** `_blocked_account` short-circuits the
broker round trip, not the call: `engine.py` keeps calling `place()` every 15s
and every refusal writes a row via `_log()`. That is the whole of F-5's 844.

**The serious finding — two writers on one live account.** The 10-Aug timeline:

```
  id=  860 09:36:01.03  BLOCKED_PERMANENT  SELL PPLPHARMA   IP (103.197.75.33) is not allowed...
  id=  861 09:36:02.34  BLOCKED            SELL ETERNAL     ALL orders blocked for this session
  id=  862 09:36:03.70  BLOCKED            SELL CIPLA       ALL orders blocked for this session
  id=  863 09:36:06.77  BLOCKED            BUY  AUBANK      ALL orders blocked for this session
  id=  864 09:36:07.02  BLOCKED            BUY  SCI         ALL orders blocked for this session
  id=  865 09:36:12.81  PLACED             SELL PPLPHARMA   BOOK_PARTIAL: 1.13R >= the plan's target
  id=  866 09:36:16.59  PLACED             SELL ETERNAL     EXIT_GIVEBACK: peaked at 0.60R
  id=  867 09:36:19.67  PLACED             SELL CIPLA       EXIT_STALL: 11 sessions
  id=  868 09:36:20.83  BLOCKED            SELL PPLPHARMA   ALL orders blocked for this session
  id=  869 09:36:20.86  BLOCKED            SELL ETERNAL     ALL orders blocked for this session
  id=  870 09:36:20.88  BLOCKED            SELL CIPLA       ALL orders blocked for this session
  id=  872 09:36:22.25  BLOCKED            BUY  AUBANK      ALL orders blocked for this session
  id=  873 09:36:22.52  BLOCKED            BUY  SCI         ALL orders blocked for this session
  id=  871 09:36:24.14  PLACED             BUY  AUBANK      AUTO_ENTRY: stop 1013.36
  id=  874 09:36:26.52  PLACED             BUY  SCI         AUTO_ENTRY: stop 279.14
  id=  878 09:36:46.26  BLOCKED            BUY  AUBANK      an identical BUY for AUBANK was placed 22s ago
```

`_blocked_account` is a module global with **exactly one assignment site and no
reset anywhere in the tree**:

```
backend/execution/order_manager.py:89:_blocked_account: str | None = None
backend/execution/order_manager.py:262:    if _blocked_account:
backend/execution/order_manager.py:483:            _blocked_account = msg
```

A single process therefore cannot place an order after 09:36:01. Six were placed
between 09:36:12 and 09:37:03, **interleaved with nine echoes of the latch that
would have forbidden them**. Two independent sweeps of the same book, one
latched and one not.

Second, independent confirmation from a *different* module global: id=878 says
*"an identical BUY for AUBANK was placed 22s ago"* — 09:36:46 − 22s = 09:36:24,
exactly id=871. `_recent` is process-local, so **the process that placed at
09:36:24 is the one that blocked at 09:36:46**, and it is not the process that
wrote the latch echo at 09:36:22. Note also that id=871 carries a lower id than
id=872/873 but a later `ts` — insert order and clock order disagree, which is
what concurrent writers look like.

**Why this outranks everything else here.** Every guard in `order_manager` is
process-local: `_blocked`, `_blocked_account`, `_recent` (the 5-minute duplicate
window), and the daily caps read through `_today_totals`. With two daemons all
four are effectively doubled — and *"PPLPHARMA sold twice this way"* is already a
CLAUDE.md landmine. This is the mechanism that produces it.

**`intraday_broker_log` has no pid, host or session column**, so *which* two
processes these were is not recoverable from the log. Recorded as unrecoverable,
not guessed.

---

### 5 — Q4: ENTRIES ARE AFFECTED — BY DESIGN, AND THEY FAIL SAFER

Yes. The block is account-wide and cannot distinguish a BUY from a SELL. 47
blocked BUY attempts:

```
      34  market closed
       6  duplicate window
       4  IP allowlist
       1  order value ₹3,062 exceeds available cash ₹0
       1  order value ₹3,060 exceeds available cash ₹0
       1  could not read available cash from the broker
```

The 4 IP-blocked BUYs are **2 entry decisions**, each blocked twice by the two
processes of §4. `BUY limit = round(ltp × (1 + 20bps), 1)`, so `ltp = price /
1.002`:

```
    AUBANK  blocked 08-10 09:36:06 -> placed 09:36:24 (+17s)  ltp 1061.08 -> 1061.58  +0.50/sh x5  = Rs  +2.50
    SCI     blocked 08-10 09:36:07 -> placed 09:36:26 (+19s)  ltp  300.10 ->  300.00  -0.10/sh x14 = Rs  -1.40
```

**Net entry displacement: ₹1.10 paid more than the plan's price, across two
entries delayed by under 20 seconds.** No entry was blocked long enough to
matter.

**And the entry path has a protection the exit path does not.** `engine.py:2434`:

```python
        max_entry = getattr(d, "max_entry", None)
        if max_entry and limit > float(max_entry):
            logger.info(f"  {sym}: a {limit} limit would exceed the plan's max entry "
                        f"{max_entry} — not chasing past its own R:R")
            return
```

A long block makes an entry **not happen** rather than happen at a price the
plan rejected. The exit side has no such guard, and structurally cannot have the
same one — refusing to exit because the price moved is how a give-back becomes a
stop-out. The asymmetry is correct; it is recorded because it means Q4's answer
is *"yes, and it is the safer direction"*.

---

### 6 — Q5: ROOT CAUSE. TWO DIFFERENT FAULTS, ONE FIXED, ONE LIVE

Every rejection, with the source address Zerodha saw:

```
  2026-07-29 12:57:32 IST  IPv4  52.159.247.226                             PPLPHARMA
  2026-07-29 13:05:20 IST  IPv6  2402:e280:3e1a:670:5ccb:7a4a:ecad:a1fa     PPLPHARMA
  2026-07-29 14:20:49 IST  IPv6  2402:e280:3e1a:670:5ccb:7a4a:ecad:a1fa     PPLPHARMA
  2026-08-06 10:14:29 IST  IPv4  103.197.74.141                             KIMS
  2026-08-06 10:56:04 IST  IPv4  103.197.74.141                             TRAVELFOOD
  2026-08-10 09:36:01 IST  IPv4  103.197.75.33                              PPLPHARMA
```

Message text is identical in all six, and names an IP and nothing else:

```
IP (X) is not allowed to place orders for this app. Update allowed IPs on the
Kite developer console. Learn more - https://support.zerodha.com/.../static-ip
```

**Cause A — IPv6 leakage. Fixed, has not recurred.** The two `2402:e280:…`
rejections are the dual-stack fault `config._force_ipv4()` documents.
`git log -S"_force_ipv4"` → **`074c355`, 2026-07-29**, the same day. No v6
address appears after it.

**Cause B — the daemon's public IPv4 changes between sessions. LIVE, UNFIXED.**
Three distinct v4 addresses across three sessions, two of them
(`103.197.74.141`, `103.197.75.33`) in the same ISP /22 but different hosts. The
allowlist entry is stale because the address moved, not because anything in the
code is wrong.

**It is not a session or token problem, and the log shows this rather than my
assuming it.** On 10-Aug orders were **PLACED successfully 12 seconds after** the
rejection (§4, id=865). A dead `access_token` or a wrong `api_key` does not
self-heal in 12 seconds, and Zerodha reports those conditions with different
messages — `_ACCOUNT_WIDE` lists them separately. The error names an address.

**One address is not explained by either cause and is reported as-is:**
`52.159.247.226` (29-Jul 12:57) is a Microsoft Azure range, not the operator's
ISP, and is the only rejection from it. That is consistent with an order attempt
originating from a cloud runner rather than the local daemon — but
`intraday_broker_log` records no host (§4, F-10), so **this is what the log
shows, and I am not claiming which process it was.**

---

### 7 — FOUND ALONG THE WAY

**F-7 — `swing_entry_slip_bps` has no `system_config` row**, exactly as F-6
found for `exit_slip_bps`. Both fall to code defaults (20 and 30 bps).

```
intraday_exit_slip_bps = '30'
exit_slip_bps          = ''      <- F-6
swing_entry_slip_bps   = ''      <- F-7
```

Harmless today; every entry and exit price in §3 and §5 is reconstructed through
these constants, so a future edit to either silently invalidates this entry's
arithmetic.

**F-8 — 216 of the 892 blocked rows are `market is closed`, written between
~09:00 and 09:15 IST.** The daemon calls `place()` every ~15s before the open and
`preflight()` refuses each one; 85 such rows were written this morning alone,
and BHEL took 57 on 08-03 before filling at 09:17. No money, but it is a quarter
of F-5's headline number and it buries real events in the log.

**F-9 — TMCV: a live entry was placed, recorded as a position, never filled, and
then the daemon tried to sell it 254 times.** BUY 7 @ 437.2 placed 08-03
09:17:03. From 08-04 12:24:56 to 15:31:28 there are **254 `cannot sell 7 —
broker shows 0 held`** rows. TMCV appears in **neither `open_positions` nor
`closed_positions`** today — 0 rows in each. This is the entry-side mirror of the
optimistic write `engine.py` documents for exits: the position was booked on
`PLACED`, the fill never came, and nothing reconciled the entry away until the
row simply vanished, unrecorded. It is also the whole of the `qty mismatch`
bucket's 254.

**F-10 — `intraday_broker_log` carries no process, host or session identity.**
It is why §4 can prove two writers existed but not name them, and why §6's Azure
address cannot be attributed.

---

### 8 — COULD NOT DETERMINE

- **True fill prices for the two partial books** (PPLPHARMA 29-Jul and 10-Aug).
  `closed_positions.exit_price` is the blended exit of the whole position, and
  `open_positions.partial_booked_price` is overwritten by each subsequent
  partial and gone once the position closes. Both rows in §3 marked `RECON` are
  the placement's own limit inverted through the 30bps constant — a
  reconstruction of the decision price, not a broker fill. Kite's order history
  covers the current day only, so 29-Jul and 06-Aug are unrecoverable.
- **Which two processes were writing on 10-Aug** (§4, F-10). Their existence is
  proven; their identity is not in the data.
- **Whether the 22.13-hour PPLPHARMA block's favourable outcome is typical.**
  n=1 at that duration, n=4 overall. The sign of the total is set by a single
  overnight gap.
- **Whether any block occurred in a session that produced no rejection row at
  all** — i.e. whether `_log()` itself ever failed. `_log` swallows its exception
  to `logger.debug` (`order_manager.py:539`), so a silent write failure would be
  invisible here. Nothing suggests it happened; nothing rules it out.
- **The intraday book.** Not examined — every blocked row in this population is
  `framework=SWING` or null.

---

### 9 — RECOMMENDS

Not implemented, per the brief. Ranked by money at risk, not by effort.

**R-1 — Enforce one daemon per account. This is the only finding here with real
money behind it.** §4 proves two processes swept the same live book within one
minute, and every safety guard in `order_manager` — `_blocked`,
`_blocked_account`, `_recent`, and the daily caps via `_today_totals` — is
process-local and therefore was doubled. The recorded consequence of exactly
this is already in CLAUDE.md: *"PPLPHARMA sold twice this way."* Proposed: a
Postgres advisory lock keyed on the account, taken at `run.py` startup, where a
second daemon **refuses to start and says so** rather than quietly trading
alongside the first. Add a `host`/`pid` column to `intraday_broker_log` (F-10)
in the same change, so the next occurrence is attributable instead of inferred.

**R-2 — Turn the IP allowlist from a post-mortem into a preflight.** Cause B
(§6) is knowable *before* the market opens: the daemon can read its own public
v4 and compare it against the address the operator last put in the Kite console,
stored as a new `system_config` key. Mismatch → loud alert at startup and at
09:00, not a rejected exit at 10:14. This must be built to the CLAUDE.md rule
that killed the last version of it — *"`tradeos ip` reports the v4 address
matching, so the check passes and the orders still fail"* — meaning the check
has to compare against **what Kite was told**, which no API exposes, so the
config key is the only honest reference. Demonstrate it FAILING on a wrong
address before trusting it.

**R-3 — Do not call `place()` when the market is shut** (F-8). Hoist
`is_market_open()` into the caller's loop. Removes ~216 rows of noise and a
quarter of F-5's number, changes no decision.

**R-4 — Reconcile entries, not just exits** (F-9). An entry written
optimistically on `PLACED` should be verified against broker holdings on the
next reconcile and withdrawn if it never filled, instead of generating 254 sell
attempts against shares that do not exist and then disappearing from both
tables.

**R-5 — Give `exit_slip_bps` and `swing_entry_slip_bps` real `system_config`
rows** (F-6, F-7).

**Explicitly NOT recommended: anything about exit slippage itself.** §3 measures
it at **+₹21.24 / +0.27R favourable across the entire book history**, on n=4,
and no exit was ever permanently lost. Widening a gate, adding a retry ladder or
changing the exit price model to chase this would be optimising noise — and the
one real hazard it exposed (a 22-hour unplanned overnight) is prevented by R-2
stopping the block, not by changing how exits are priced.

**Gate: PASS** — five questions asked, five answered with raw output behind
every number. Two of F-5's own claims are corrected (§0), its open slippage item
is closed (§3), and the investigation surfaced a concurrency defect (§4) that
outranks the question it was sent to answer. Nothing on the trading path was
changed; `tools.verify` is 434/434.

---


## 2026-08-15 — R-2/R-3 (preflight identity + lease) — the IP check was watching the wrong machine; the guard it was reaching for is HOST IDENTITY, and F-11 is not hypothetical — a GitHub Actions runner placed a live SELL on 29-Jul and only Zerodha's allowlist stopped it

**Branch:** `fix/preflight-lease-and-host`, off `main` (not off
`fix/single-daemon-lease` — the two are independent and either may land first).
`python -m tools.verify` → **all 462 checks passed across 56 modules** (434
baseline + 28 new). `python -m tools.health` → **21/21**. `python -m
tools.simulate` → clean, nothing written.

---

### 0 — THE CORRECTION THIS STARTS FROM, AND WHAT IT COST TO GET WRONG

The operator's correction, in full: the Oracle VCN holds the lease and has a
**static public IP that is correctly allowlisted**. The IP mismatches
`tools.health` reported were the **laptop's dynamic ISP address**. It was
checking the machine it ran on, not the machine that places orders.

That invalidates **F-12** ("the IP allowlist is stale RIGHT NOW, live, before
tomorrow's open") and the R-2 stage it proposed. The four "distinct addresses"
across four sessions were four DHCP leases on a machine that has never sent an
order. This session confirms the mechanism directly — from this laptop:

```
this host: Vipin                      kite_allowlisted_ip         = 103.197.75.33
lease row: hostname 'tradeos-vcn'     intraday_lease_primary_host = tradeos-vcn
public IP: 103.197.74.232             <- the laptop, still 103.197.74.x,
                                         still not allowlisted, still irrelevant
```

**A wrong alarm is not a lesser fault than a missing one.** It is the fault that
teaches an operator to ignore the output. `check_kite` produced a RED line about
imminent live-trading failure every time it ran from the laptop, and the line was
literally true and said nothing whatsoever about whether tomorrow's exits fill.
No IP-vs-allowlist check was built here, by instruction and on the merits: it is
meaningless from any host but the VCN and nearly always trivially true on it.

---

### 1 — WHAT WAS BUILT — TWO GUARDS, BOTH IN `preflight()`

`preflight()` is where every order path in this codebase already converges, which
is the only reason one edit covers the paths `run.py`'s startup lock does not.

**Guard 1 — WHICH MACHINE (`host_permits_live`, pure).** Identity, not address.
The question a machine can answer about *itself*, offline, correctly, from
anywhere: am I the one that is supposed to be doing this? Correct on every host
including the ones where an IP check is meaningless, and it does not decay when
the ISP hands out a new lease. LIVE only — a paper fill never reaches the broker,
so no address is involved, and refusing paper on the laptop would refuse the one
thing a laptop is for.

**Guard 2 — WHICH PROCESS (`lease_permits`, pure).** F-11. Reads the lease
through a new `lease.observe()`, which is **the only function in `lease.py` that
does not write** — `acquire()` and `renew()` both upsert, so calling either from
preflight would have the pipeline *steal* the lease from the daemon it is meant
to defer to. A test asserts `observe()` issues exactly `['select']` and raises on
any write. Applies to BOTH modes: two processes writing the same paper position
poisons the learning loop exactly as two live orders empty an account — the money
differs, the doubling does not.

**Ordering, as asked.** Host is free (one `socket` call) and sits above every
check that costs a round trip; lease is one database read and sits below the
session latches, above the three broker calls. Two tests assert this by pointing
preflight at a fake broker that **raises on contact** — so "the guard fired before
any broker call" is an assertion, not a claim. Pre-fix, both surfaced as
`broker state unavailable: preflight reached the broker`.

---

### 2 — EXITS ARE NOT EXEMPT. THEY ARE HELD TO A WEAKER FORM OF THE SAME CHECK

Stated explicitly because it was asked, and because exempting them was the
tempting wrong answer:

| lease state | BUY | SELL |
|---|---|---|
| this process holds it | allow | allow |
| **another process holds it, UNEXPIRED** | refuse | **refuse** |
| lapsed (holder named, expired) | refuse | **allow** |
| free (clean `release()`) | refuse | **allow** |
| unreadable (database failure) | refuse | **allow** |

A blanket exit exemption would leave F-11 open on the exact path that has it —
`position_lifecycle.main(manage=True)` sells, it does not buy. So exits are
refused in the **one** state where refusing is safe: something else is
demonstrably alive and will act on the position on its own cycle.

**Why that makes a handover safe.** When an active daemon dies its lease is not
transferred, it **lapses** — for up to `intraday_lease_ttl_seconds` (120) the row
still names a holder that no longer exists. A symmetric check would spend that
entire window refusing exits on behalf of a dead process: the wrong answer at the
worst moment, and the same class of error as the daily-cap bug where exits were
exempt from being *blocked* by the cap but not from *consuming* it. Here the lapse
reads as `held_by_other=False` the instant it expires and the exit goes straight
through, while an entry in that same state stays refused because nothing is
watching the book. `observe()` also treats an **unreadable timestamp** as expired
for this reason — a corrupt row must not be able to forbid every exit for as long
as it stays corrupt — and separates "nobody holds it" from "I could not find
out", which are opposite facts that a single empty holder would collapse into
permission.

---

### 3 — F-11 IS NOT HYPOTHETICAL. IT FIRED ON 29-JUL, FROM A CLOUD RUNNER

The previous session recorded F-11 as a path that *could* double an exit. The
broker log says it already did. `intraday_broker_log`, PPLPHARMA SELL, 29-Jul
(ts is UTC; IST in brackets):

```
07:27:32 [12:57 IST]  BLOCKED_PERMANENT  IP (52.159.247.226) is not allowed ...
07:35:20 [13:05 IST]  BLOCKED_PERMANENT  IP (2402:e280:3e1a:670:...) is not allowed ...
08:50:49 [14:20 IST]  BLOCKED_PERMANENT  IP (2402:e280:3e1a:670:...) is not allowed ...
```

`52.159.247.226` is a Microsoft **Azure** address — where GitHub-hosted runners
live — and `2402:e280:...` is a residential IPv6 line. **One position, one
afternoon, two different machines attempting the same live SELL.** Neither was
the VCN.

The Azure one has a name: `.github/workflows/pipeline_intraday.yml` runs
`python -m control.position_lifecycle --manage-only --require-live` **every 30
minutes during market hours**. Its own header says it *"Records state and alerts;
**never places an order**"* — and `position_lifecycle.py:1691` places one,
gated on `auto_exit_enabled("SWING")`, which is **armed right now**:

```
intraday_autonomy_phase = 4.0    intraday_orders_enabled = true
swing_auto_exit         = true   swing_trading_mode      = LIVE
```

So the live book currently has a **third scheduled order-placing process** that
holds no lease, is not the daemon, and runs from an address Zerodha will never
accept. The only thing that has been preventing duplicate swing exits on that
path is the IP allowlist rejecting it — an accidental safety net that R-2 as
originally framed would have "fixed" by making the runner's orders go through.
That is the strongest argument against the IP check and for this one.

Both guards refuse that runner now, independently: wrong host **and** no lease.

---

### 4 — EACH TEST FAILED FIRST

`tests/test_preflight_host_and_lease.py`, 28 checks, registered in
`tools/verify.py::MODULES`. Demonstrated failing **twice**, deliberately:

**(a) Against pre-change source** (`git stash` of `order_manager.py` +
`lease.py`, test file kept) — **28 of 28 failed**, on `ImportError: cannot import
name 'host_permits_live'` / `'lease_permits'` / `'LeaseView'` and
`AttributeError: module 'intraday.lease' has no attribute 'observe'`.

**(b) The demonstration that actually matters** — pure functions present, both
guards' defaults flipped to `False` so the logic exists and is *not wired*:
**7 of 28 failed**, and they are exactly the behavioural ones:

```
preflight refuses a live order from the wrong host   -> broker state unavailable: preflight reached the broker
the wrong host is refused before the broker          -> AssertionError
preflight refuses when another process holds lease   -> broker state unavailable: preflight reached the broker
the lease guard also fires before the broker         -> AssertionError
preflight refuses an entry through the same handover -> 5 SWING orders already placed today (cap 5)
paper is NOT exempt from the lease check             -> broker state unavailable: preflight reached the broker
both switches default ON                             -> must fall back to intraday_lease_primary_host and still fire
```

(b) is the one worth keeping. A pure function's correctness proves nothing about
its callers — this project has been bitten by that four times in one feature —
so the guards are asserted **through `preflight()` itself**, with the clock, kill
switch, phase gate and broker all replaced by known answers so the tests stay
offline and clock-free as `tests/__init__.py` requires. The suite also pins that
each guard **can pass** (`the right host gets past the host guard`), because a
check that cannot PASS is the same defect wearing a different hat.

---

### 5 — THE HEALTH CHECK NOW NAMES THE MACHINE

`check_kite` calls **the same `host_permits_live()` preflight gates on**, not a
second copy of the rule — a health check that disagrees with the gate it reports
on is how you get a green board over a refused order. Before and after, same
laptop, same minute:

```
x kite  public IP is 103.197.74.232 but only 103.197.75.33 is recorded as
        allowlisted — order placement will be REJECTED from this address

v kite  session live for DSY688, read-only from 'Vipin' (103.197.74.232) — this
        machine does not place orders, so its address is not compared against
        the allowlist; live orders go out from 'tradeos-vcn', no broker config
        rejection today
```

**It can still fail.** Same code, same stale IP, asked as if it ran on the VCN
(`socket.gethostname` patched to `tradeos-vcn`):

```
x kite  'tradeos-vcn' — the machine that places live orders — has public IP
        103.197.74.232, but only 103.197.75.33 is recorded as allowlisted.
        Order placement will be REJECTED from this address
```

Two things were deliberately **not** done. The first draft returned early on a
non-order-placing host — which would have skipped the broker-verdict section
below it and printed *"no broker config rejection today"* **without checking**,
trading one wrong answer for a missing one. It now only skips the IP comparison
and falls through, because `BLOCKED_PERMANENT` is read from a table both machines
share and IS meaningful from any host. Second, the IPv6-resolution check now
names the host too, for the same reason: DNS is a property of the machine asking.

---

### 6 — CONFIG, AND WHY IT IS LIVE BEFORE THE MIGRATION

Migration **078** (not 077 — that number is taken on the unmerged
`fix/single-daemon-lease` branch; the two are independent and may be applied in
either order). Three keys, both switches **defaulting ON in code as well as in
SQL**, because a guard inert until a migration runs is no guard at all on the day
it ships:

- `live_order_host_check` — bool, **true**
- `live_order_host` — comma-separated hostname prefixes, seeded `tradeos-vcn`
- `live_order_lease_check` — bool, **true**

`live_order_host` falls back in code to `intraday_lease_primary_host`, which
**already holds `tradeos-vcn` in the live book** — the machine that runs the
daemon and the machine whose IP is allowlisted are the same machine by
construction. So the host guard is correct the moment the code deploys, with no
migration dependency. A test pins that fallback against an empty config, which is
exactly the pre-migration state. **Empty means the check is ABSENT, not that
everything is denied** — a blank key must not brick live trading on a fresh
install, the same rule as the allocator's cold start.

---

### 7 — FOUND ALONG THE WAY

**F-15 — `pipeline_intraday.yml`'s header contradicts the code it runs.** It
says the step *"never places an order"*; `position_lifecycle.py:1691` places one,
armed. Now blocked by both guards, but the comment is still wrong and the next
person to read it will be misled the same way. Doc-only, not touched here.

**F-16 — `TOTAL_CAPITAL` on this laptop is ₹30,000; the account is ₹20,000.**
Printed on every command run this session (`Capital=₹30,000`). This is the
2026-08-06 split brain that `check_daemon` already carries a check for — it
stayed silent because `capital_snapshot` was last written by this same host, so
the `who != gethostname()` condition never fired. The check compares against
whoever wrote the snapshot, not against a declared truth, so a laptop that both
writes and reads it can never disagree with itself. Not fixed — it is a check
that cannot fail, of the exact kind this project keeps finding, and it deserves
its own stage.

**F-13 stands unchanged** (`renew()`'s primary override is still a mid-run steal
path) and is now *more* relevant, not less: `_is_primary()` prefix-matches
`intraday_lease_primary_host`, the same key this session made the host guard fall
back to. A host renamed to start with `tradeos-vcn` would gain both the lease
override and live-order rights in one step.

---

### 8 — NOT DONE

- **Migration 078 has NOT been applied.** Migrations run against a live book;
  applying one was not in this brief. Consequence is deliberate and stated in §6:
  both guards are live on deploy regardless.
- **No IP-vs-allowlist check was built**, by instruction and on the merits (§0).
- **F-15 and F-16 recorded, not fixed.** F-16 in particular means every position
  size computed on this laptop is 50% too large; it is not a code defect and a
  one-line `.env` change fixes it, but it is live right now.
- **Not exercised against two real concurrent processes.** The guards are
  verified offline against fakes that enforce the contract. Confirming the
  daemon still trades normally needs a live session on the VCN — the daemon holds
  its own lease and runs on `tradeos-vcn`, so it passes both guards by
  construction, but that is reasoning, not a measurement.

**Gate: PASS** — both changes are in `preflight()` as specified, the exit/entry
asymmetry is stated and tested rather than assumed, every test was demonstrated
failing first (twice, the second time in the form that catches an unwired guard),
`tools.verify` is 462/462, `tools.health` 21/21. Three live-money items found
along the way (F-15 armed unguarded runner path, F-16 sizing split, F-13
unchanged) are recorded and not silently fixed.

---

## 2026-08-15 — Stage 2e (planner: regime symmetry + a cost model) — the regime knob was a knob on R and it pointed the wrong way; the planner now prices its own friction on the LEDGER basis, and both fixes ship inert

Both defects are in `analysis/risk_model.py`, the module that writes
`signal_output_daily.planned_target` on the **LIVE** swing book. Both are fixed
behind config keys that default to today's behaviour, so **merging this changes
nothing**. Arming either is a separate decision.

---

### 0 — THE TWO DEFECTS, RESTATED FROM THE CODE

```
regime_k  = REGIME_STOP_MULT.get((regime or "NEUTRAL").upper(), 1.0)   # :158
atr_stop  = anchor - (p["stop_atr_mult"] * regime_k * atr_abs)         # :161
target    = anchor + (p["target_atr_mult"] * atr_abs)                  # :188
```

**1. The stop was scaled by `regime_k`; the target was not.** Both are ATR
distances — the two sides of one ratio — so a knob whose stated purpose is
volatility was silently a knob on reward-per-unit-risk:

```
planned R = target_atr_mult / (stop_atr_mult * regime_k) = 3.0 / (1.5 * k)
   TRENDING 2.1050 · RISK ON 2.0000 · NEUTRAL 1.9048 · RECOVERING 1.7391 · RISK OFF 1.6000
```

R **shrank as conditions worsened** — more risk per share for identical reward,
exactly when the market is least likely to pay for it — and the 2R design point
was reachable only in `RISK ON`, which this book has never traded.

**2. The planner had no cost model.** `risk_model.py` imported `dataclasses` and
nothing else while every other gate in the system prices its own friction.
## 2026-08-14 — R-1 (change, single-daemon lock) — the lease is a role, not a mutex: three separate paths let both daemons act, one of them by design since 06-Aug. A startup compare-and-swap now refuses the second daemon, and `intraday_broker_log` records who wrote every row

Implements R-1 from Stage 2d-i §9. R-2 (the IP allowlist pre-market check) was
explicitly deferred and is NOT in this change — see §5, where it is now also a
LIVE failure rather than a recommendation.

---

### 0 — WHY THE EXISTING LEASE DID NOT PREVENT 10-AUG *(asked before anything was changed)*

**Is `lease.acquire()` on the swing/order path?** Only indirectly, through one
variable, and never at the order itself. `may_act` appears in exactly one file:

```
intraday/run.py:102   if ls.may_act:
intraday/run.py:138   was_active = ls.may_act
intraday/run.py:193   if ls.may_act and not was_active:
intraday/run.py:197   was_active = ls.may_act
intraday/run.py:202   if prices and not was_active:   <- standby, read-only
```

`engine.cycle()` routes both books, so swing entries and exits *are* behind the
lease inside the daemon. But `grep -rn lease backend/execution/` returns
**nothing** — `preflight()` and `place()` do not know a lease exists. It is a
loop-level gate, not an order-level one.

**Is it called at startup?** Yes — `run.py:101`, after the holiday check, before
`load_state()`. The check ran. It did not hold.

**What happens on a stale lease?** It is claimed. `lease.py:138` defers only when
`holder and holder != _INSTANCE_ID and expires > now`; an expired lease falls
through to the upsert. That is correct, and preserving it is half of this work.

**So why did two daemons coexist? Three structural reasons, none a typo.**

**(a) `acquire()` is read-then-write, not compare-and-swap.** Its own docstring
admits it — *"Two daemons starting in the same second could briefly both believe
they are active."* The write at `lease.py:153` is an unconditional `upsert`; it
never re-asserts the row read at line 118.

**(b) The loser keeps trading for up to 30 seconds after losing.** `run.py:191`
re-reads the lease on a 30s timer while `eval_interval_s()` is 15s, so a demoted
daemon runs one or two more full cycles — placing orders — before it notices.
**The 10-Aug window is 09:36:01→09:37:03: 62 seconds. Two renew intervals.**

**(c) Migration 050's primary override makes the overlap deliberate.**
`_is_primary()` gates `acquire()` line 129 as `if rows and not am_primary:` — a
configured primary **skips the deference check entirely and claims a live,
unexpired lease held by a running daemon.** `renew()` line 181 does the same
mid-run. `intraday_lease_primary_host='tradeos-vcn'` shipped in **`a4c20b9`,
2026-08-06 — four days before the incident.**

**And the root of it: a role is not a mutex.** Migration 023's design is that the
second daemon *starts and keeps running* in STANDBY — one config read, one
exception, or one renew interval from acting. Both `acquire()` (lines 127, 156)
and `renew()` (line 197) fail **OPEN to ACTIVE** on any database error.
Exclusivity that depends on a live process voluntarily re-reading a flag every
30 seconds is not exclusivity.

**Both 10-Aug writers were daemons, not the pipeline.** `AUTO_ENTRY:` is emitted
only by `engine.py:2447` and appears on both sides of the interleave (id=863
BLOCKED AUBANK, id=871 PLACED AUBANK), so the second writer was `intraday/run.py`
and not `position_lifecycle.main()`. That distinction decided where the lock
goes; see F-11 for the path it therefore does *not* close.

---

### 1 — WHAT WAS BUILT


**Regime symmetry** (`risk_regime_scales_target`, default **false**). `regime_k`
now multiplies the target distance too — **on the ATR branch only**:

```python
target_k = regime_k if (p["regime_scales_target"] and stop_source == "atr") else 1.0
target   = anchor + (p["target_atr_mult"] * target_k * atr_abs)
```

Scaling it on the structure branch as well was the obvious alternative and is
wrong. A structural stop is a **price**, so `regime_k` never touched its risk;
scaling only its target would raise R with **no offsetting risk change** — a free
+5% on 308 of 995 plans. `k` is applied to the target exactly where it was
applied to the stop, which is what "symmetric" means here. This is not an
argument, it is a test: patching the line to scale unconditionally makes
`test_symmetry_leaves_a_structure_stop_plan_alone` fail with
`target moved from 545.0 to 547.25 on a structure stop`. Demonstrated, then
reverted.

**A cost model** (`risk_min_planned_r_enabled`, default **false**). Every plan
now sizes its own clip by the production rule, prices that clip's round trip,
and reports `friction_r` / `required_rr` on `TradeLevels` **whether or not the
floor is armed**. When armed, a plan below `(1 - h + friction_R)/h + margin` is
rejected with `below_min_planned_r_{rr}_needs_{bar}`.

`plan_clip()` holds the two capital terms and `compute_position_size()` was
refactored to **call it** rather than restate them — two copies of a sizing rule
is how a plan and the position it becomes drift apart, and friction is only
meaningful if computed on the clip the account will actually take. The refactor
is behaviour-identical: **200,000 randomised sizings against the pre-refactor
arithmetic, 0 mismatches**, across quantity *and* `capped_by`.

---

### 2 — WHICH COST BASIS, AND WHY

**LEDGER** (`entry_leg + exit_leg`, statutory only), **not GATE**
(`round_trip`, which adds `cost_slippage_bps` 5 on both legs).

The two differ by a constant **+0.100pp of position** — on CNC clips that is
**1.10–1.17x**, on MIS 1.94x. Measured here at each plan's own clip:

```
  friction R (ledger)  n=784  min 0.1062  p25 0.1305  MED 0.1568  p75 0.1997  max 0.4467
  friction R (gate)    n=784  min 0.1192  p25 0.1497  MED 0.1786  p75 0.2306  max 0.5011
```

The reason is the CLAUDE.md rule that **a gate and the thing it gates must be the
SAME QUANTITY**. `planned_target` is ultimately judged against **realised** R —
`expectancy_ledger`, `weekly_review`, and every prior built from
`closed_positions` — and all of those price friction statutorily, because
slippage is already inside the fill price on both books (`paper_broker.py:90`
fills at `ltp * (1 ± slip)`; live fills embed it by construction). Charging it
again in the planner would be a **double count against the very number this floor
exists to protect**, and would put the plan and its own outcome on two different
rulers. The GATE basis is right where it lives — a pre-trade decision against a
quoted price not yet paid — and is reported alongside below, not used.

---

### 3 — PLANNED R, BEFORE AND AFTER

**POPULATION [PLANS-2e]:** `signal_output_daily`, **2026-07-28 → 2026-08-13**,
13 dates, **995 rows with a coherent geometry** — Stage 2c's window and its
exact count, pinned by date rather than by "last 13" because 14-Aug has since
landed. **All 995 read `NEUTRAL`** (`k = 1.05`).

Every "after" figure is produced by **calling `compute_trade_levels()` with the
switch on**, not by multiplying the stored R by `k`. Reconstruction fidelity
against the stored plan: **target max |err| ₹0.000, `expected_r` max |err| 0.000,
stop max |err| ₹0.010** (2-dp storage rounding).

```
                       n     min     p25     MED     p75     max    mean
  ALL      before    995  1.9050  1.9050  1.9050  2.1390  7.1210  2.2241
  ALL      after     995  1.9110  2.0000  2.0000  2.1390  7.1210  2.2897

  atr      before    687  1.9050  1.9050  1.9050  1.9050  1.9050  1.9050
  atr      after     687  2.0000  2.0000  2.0000  2.0000  2.0000  2.0000

  structure before   308  1.9110  2.2130  2.6370  3.3490  7.1210  2.9359
  structure after    308  1.9110  2.2130  2.6370  3.3490  7.1210  2.9359   << unchanged, by design
```

```
  in the 1.90 <= R < 1.95 band :  before 696   after 9   (the 9 are all structure stops)
  exactly 2.0000              :  before   2   after 689  (687 atr + the 2 already there)
```

The median planned R is **1.9050 before and 2.0000 after on every one of the 13
dates, without exception** — because it was never a distribution. The constant
is gone; the structure-stop tail is untouched.

---

### 4 — HOW MANY MOVE ABOVE BREAK-EVEN

Each plan against **its own** stop and **its own** clip, sized by the production
rule at **₹20,000** with an empty book. (₹20,000 is stated explicitly, not
inherited: this laptop's `TOTAL_CAPITAL` env reads ₹30,000 — F-16 — and
`capital_for("SWING")` would have handed the planner that. The tests pin it for
the same reason.)

```
FUNDING at Rs 20,000  —  unfundable 211, fundable 784
  clip Rs              n=784  min 1396.57  p25 2856.79  MED 3328.66  p75 3760.10  max 3999.60
  required R (ledger)  n=784  min 1.7654   p25 1.8263   MED 1.8920   p75 1.9994   max 2.6169
  required R (gate)    n=784  min 1.7979   p25 1.8743   MED 1.9466   p75 2.0764   max 2.7527
```

**THE ANSWER, at the 40% design hit rate:**

| basis | below break-even BEFORE | AFTER | moved above |
|---|---|---|---|
| **LEDGER** (statutory) | **185 of 784 (23.6%)** | **72 (9.2%)** | **113** |
| **GATE** (+5 bps) | 285 of 784 (36.4%) | 154 (19.6%) | 131 |

Expectancy `h·R − (1−h) − f` on the ledger basis moves from a median of
**+0.0347R to +0.0706R**, and its p25 from **+0.0037R to +0.0367R** — the
quartile that was straddling zero clears it.

**The 72 that remain below** are not a residue of the 1.90 band — that band is
gone. They are plans whose own friction demands more than 2.0R:

```
  planned R after : 1.9370 .. 2.2890      stop % : 2.17 .. 4.71
  clip Rs         : 2,024 .. 4,000        by source: atr 61, structure 11
```

A tight stop is what does it: friction in R is `cost_pct / stop_pct`, so a 2.17%
stop on a ₹2,024 clip pays 0.45R to open and close and needs 2.62R to break even.
**This is the case for the floor and against arming it alone** — it refuses
narrow-stop plans, which is the opposite selection from the wide-stop one the
`max_risk_pct` 8.0 ceiling makes.

**Through the shipped switch itself**, not the report's arithmetic:

```
  floor only                 refuses  185 of 784 (23.6%)
  floor + regime symmetry    refuses   71 of 784 ( 9.1%)
```

71 against the table's 72 — one plan sits inside the rounding. `friction_r` is
stored to 4 dp and `required_rr` is derived **from the rounded figure**, so that
the two numbers a human reads reconcile through `(1−h+f)/h` instead of
disagreeing in the fourth decimal. The shipped number is 71.

---

### 5 — EACH TEST FAILED FIRST

`backend/tests/test_planner_regime_and_cost.py`, 19 checks, registered in
`tools/verify.py::MODULES`. Run against the untouched module first:

```
  ✗  planner regime symmetry and cost floor (shipped inert)  (14/19 failed)
```

The 5 that passed are the regression pins on *today's* behaviour — the 1.9048
constant, the unscaled target, the regime ladder — which must pass before and
after, because the shipped default is inertness. The other 14 failed, then
passed.

Two of them are the mirror pair CLAUDE.md demands, on **one setup** (entry ₹500,
ATR 2%, ₹4,000 clip, 3.15% stop, friction 0.19R, own break-even 1.975R):

- `floor CAN FAIL` — at the unfixed 1.9048R the plan is refused, and the reason
  names both numbers.
- `floor CAN PASS` — the regime fix alone lifts the same plan to 2.0R, over its
  own bar. A threshold no realistic input clears is the allocator defect wearing
  a different hat, so this is asserted, not assumed.

Two more pin the permissive direction, because **"no opinion" and "measured bad"
must not give the same answer**: an **unfundable** plan (no clip → no friction)
and a **broken cost model** (`_statutory_round_trip` replaced with a raiser) are
both left `valid`, tagged `unfunded` / `unavailable`. Refusing to fund a share is
`portfolio_constraints`' job and it names that reason itself; a cost verdict
never computed must not stand in front of it.

```
cd backend && python -m tools.verify     ->  all 481 checks passed across 57 modules
cd backend && python -m tools.simulate   ->  SWING LIVE 6 positions, 8 buyable plans (unchanged)
```

---

### 6 — FOUND ALONG THE WAY

**F-17 — Stage 2c's `788 fundable / 207 unfundable` does not reproduce.** The
stored geometry gives **784 / 211**, while the 995-row denominator, the 13 dates
and `expected_r` all reproduce exactly — so this is the funding split alone.
Neither price basis nor any rounding rule tested yields 788:

```
  price=entry_zone_low  risk=geom -> 784      floor // (shipped) -> 784
  price=entry_zone_low  risk=pct  -> 784      round()            -> 899
  price=current_price   risk=geom -> 779      ceil()             -> 995
  price=current_price   risk=pct  -> 779      capital 30,000     -> 858
```

Consequently **every count in §4 is quoted against 784, not 788.** 2c's
scratchpad is gone and I did not rerun it; I can show only that its split does
not reproduce from `signal_output_daily` today under any rule I tested. Same
shape as F-1. **Flagged, not resolved.**

**F-18 — the max-position ceiling is the *sole* binding funding term.** Testing
each term alone, `int(4000 // entry)` refuses exactly the same 211 plans as
`min(risk, maxpos)`, while the risk-budget term alone refuses 155. So no plan in
this window is unfundable because ₹200 cannot buy one share — every one of the
211 is unfundable because the share costs more than the ₹4,000 ceiling (2c
measured their median price at ₹7,678). `risk_pct_per_trade` sets the *size* of a
funded position; it never decides *whether* there is one. Not a defect; it means
`max_position_pct` is the only lever that widens the tradeable universe, and 2c's
§5 already records that the slot count is over-committed against cash.

**F-19 — `tools.health` reports 1 pre-existing problem, unrelated to this work.**
`learning: 1000 detection(s) across 1 past session(s) were never scored
(2026-08-14)`. `outcomes.resolve_day` did not run for 14-Aug, so the weekly
review would judge engines on a session that was never collected. Untouched here
— it is not in this brief and it is not caused by it — but it is live, and it
poisons exactly the priors this floor's hit-rate assumption would eventually be
recalibrated from.

---

### 7 — NOT DONE

- **Neither switch is armed.** `risk_regime_scales_target` and
  `risk_min_planned_r_enabled` are both `false` in code and in migration 079.
  §4 says what arming would do; deciding to is a separate stage.
- **Migration 079 has NOT been applied.** Migrations run against a live book.
  Unlike 078, nothing here needs it: the code defaults already reproduce current
  behaviour exactly, so the file exists to make the keys visible and editable on
  the dashboard, not to make the change take effect. Numbered 079 because 077 is
  on the unmerged `fix/single-daemon-lease` branch and 078 is on main.
- **The break-even identity's own assumptions are still violated.** It assumes
  winners pay exactly the planned R and losers exactly 1R; of the 10 closed swing
  trades with a full planned geometry, **one reached its planned target and none
  reached its planned stop** (F-2). The floor is therefore a *planning*
  discipline — do not write down a plan that cannot pay for itself — and not a
  forecast of realised expectancy, which the exit ladder decides. This is stated
  in the module docstring and in the `risk_plan_hit_rate` config description so
  it cannot be armed without reading it.
- **Not measured against realised outcomes.** Every number in §3 and §4 is over
  *plans*. Whether a 2.0R target is reached more or less often than a 1.9048R one
  needs the exit-ladder work F-2 points at, not this stage.

**Gate: PASS** — both defects fixed behind keys defaulting to current behaviour,
the cost basis chosen is the ledger's and the reason is stated, the
structure-branch alternative was rejected on a demonstrated failing test rather
than an argument, the sizing refactor is proven behaviour-identical over 200,000
randomised inputs, 14 of 19 tests were demonstrated failing first, `tools.verify`
is 481/481 and `tools.simulate` is unchanged. Three items found along the way
(F-17 non-reproducing 2c split, F-18 funding lever, F-19 live unscored session)
are recorded and not silently fixed.

---

## 2026-08-15 — F-19 (outcomes resolve gap) — `resolve_day` DID run on 14-Aug; "1000 unscored" was the PostgREST row cap wearing a count's clothing, and the same cap was on the weekly review, which was judging every engine on 1000 of 8324 rows

**Branch:** `fix/outcomes-resolve-gap`, off `main`. `python -m tools.verify` →
**all 500 checks passed across 58 modules** (481 baseline + 19 new).
`python -m tools.health` → 2 problems remain, both requiring a broker token the
operator must refresh (`kite`, `learning`). `python -m tools.simulate` → SWING
LIVE 6 positions, 8 buyable plans — unchanged.

---

### 0 — THE PREMISE WAS WRONG, AND THE TRUE ANSWER IS WORSE

The brief asked why `outcomes.resolve_day` **did not run** on 14-Aug. It ran.

`intraday_setups` for 2026-08-14 holds **2289 rows, of which exactly 1000 carry
an outcome and 1289 do not**. Exactly one thousand is not a number a market
produces. It is `resolve_day`'s work queue being served through PostgREST's
silent 1000-row cap:

```
resolve_day's own query, run against the live book today
  sb.table("intraday_setups").select("*")
    .eq("trade_date","2026-08-14").is_("outcome","null")     ->  1000 rows
  actually unresolved (paged)                                ->  1289 rows
```

So the day was not unscored. It was **half scored**, which is worse, because a
half-scored day is indistinguishable from a finished one: `resolve_day` logged
`1000 resolved` through `logger.success` and returned `{"resolved": 1000}`,
with no field any caller could have read to learn that 1289 rows were left.

The resolved rows are **not an id-ordered prefix** — resolved ids run 6058-8346
and unresolved 7037-8337, interleaved — which is the signature of an unordered
capped read rather than a run that stopped part-way.

### 1 — THE FULL LIST OF UNSCORED DATES

Asked for before any fix. Established by paging every row in the table with no
date filter (8324 rows), then confirmed against server-side `count='exact'`:

```
date         total  unresolved  resolved
2026-07-28     236           0       236
2026-07-29       5           0         5
2026-07-30      45           0        45
2026-07-31     174           0       174
2026-08-03     135           0       135
2026-08-04      36           0        36
2026-08-05     530           0       530
2026-08-06     523           0       523
2026-08-07     461           0       461
2026-08-10     921           0       921
2026-08-11     275           0       275
2026-08-12    1527           0      1527
2026-08-13    1167           0      1167
2026-08-14    2289        1289      1000   <== the only gap

server-side exact total        8324
server-side exact unresolved   1289
unresolved on any OTHER date      0
empty-string outcomes             0
```

**2026-08-14 is the only date carrying unresolved rows, and it carries 1289 of
them — not 1000.** No other date in the table is affected.

That "no other date" is a claim the shipped code could not have made. See §3.

### 2 — WHAT SCHEDULES `resolve_day`: NOTHING

`resolve_day` is reached from exactly one place — `intraday/run.py`'s `finally`
block, and only when `was_active` (the daemon held the lease). A session is
scored if and only if the daemon **started, acquired the lease, and exited
cleanly that day**. It is a side-effect of a shutdown, not a scheduled step.

`backfill()` runs from the same block, and `unresolved_days` deliberately
excludes today — correct on its own terms, since today's setups are legitimately
unresolved until the close. The consequence is that **a session cannot repair
its own remainder**; the earliest repair is the *next* trading day's daemon exit.

14-Aug was a **Friday**. Today, 15-Aug, is a **Saturday** (and Independence Day).
`brain_full_weekly` runs **Sunday 19:30 IST** (`0 14 * * 0`). The next daemon
exit is **Monday**. So the sequence was:

```
Fri 14-Aug  daemon exits, resolve_day scores 1000 of 2289, backfill skips today
Sat 15-Aug  no market, no daemon, no backfill
Sun 16-Aug  weekly_review consumes the book WITH THE HOLE STILL IN IT
Mon 17-Aug  daemon exits, backfill finally clears 14-Aug — one day too late
```

This was not bad luck. For any session producing more than 1000 detections it
is **structural and weekly**, and the last three sessions all qualify (12-Aug
1527, 13-Aug 1167, 14-Aug 2289). The swing book has no such exposure: the
evening pipeline scores it as an explicit step. Intraday had no equivalent.

### 3 — THE CAP WAS ON FIVE READERS, NOT ONE

The same unpaged idiom sat on every consumer of this table.

| reader | saw | should see | consequence |
|---|---|---|---|
| `resolve_day` work queue | 1000 | 2289 | a day can never be finished in one pass |
| `unresolved_days` | 1000 | 1289 | the health check's count IS the cap |
| `review_engines` (weekly) | **1000** | **8324** | engines judged on 12% of the evidence |
| `engine_scorecard` | 1000 | 8324 | same table, same truncation |
| `_rehydrate_recorded` | 1000 | 2289 | a restart re-records what it cannot see |

**`unresolved_days` could hide a whole DATE.** It returns the first 1000
unresolved rows and tallies the dates it happens to find. 14-Aug alone has 1289,
so the cap was **fully consumed by one date** — any other unscored session would
have been invisible not only to the health check but to `backfill()`, which
iterates precisely this list and would therefore never have gone back for it.
The §1 table required paging to produce; the shipped code could not have.

**The weekly review is the worst of these and it is the one the health message
names.** It read 1000 of 8324 rows, and the truncation was not a random 12% —
it favoured the oldest sessions, because that is the order rows come back in:

```
date        review saw    actually
2026-07-28       236         236     <- 100% of late July
2026-07-31       174         174
2026-08-05       270         530
2026-08-12         3        1527     <- 0.2% of the last three sessions
2026-08-13         2        1167
2026-08-14        37        2289
```

An engine's **recent** form — the only part that could justify changing its
lifecycle state — was precisely the part being discarded. Backfilling 14-Aug
without this fix would have changed nothing for the review: it would still have
seen ~37 of that day's rows.

`_rehydrate_recorded` is the CLAUDE.md landmine returning through a different
door. Capped, a restart rehydrates only the part of the morning that fitted and
re-records the rest — re-inflating both the prior population and the allocator's
arrival bar, which is the exact failure that function was written to prevent.

### 4 — THE TRAP INSIDE THE FIX FOR THE TRAP

The first `fetch_all` paged with `LIMIT/OFFSET` and **no `ORDER BY`**. Measured
against the live book before shipping:

```
returned 8324   distinct 5000   true 8324   rows never returned 3324
```

The right count, made of the wrong rows: 3324 duplicates and 3324 rows missed.
PostgreSQL guarantees no row order without an `ORDER BY`, and each page is a
separate query, so successive windows overlap and drop rows.

**This is worse than the truncation it replaced.** A truncated read is at least
made of real, distinct observations; this one silently over-weights whatever the
planner repeated, so every hit rate and prior computed from it is biased and
nothing about the result's shape says so. Caught only because the per-date
distribution was re-checked against §1 rather than the total. `fetch_all` now
sorts on a unique key (`order_by`, default `id`, overridable — `signal_output_daily`
has no `id` column). Re-verified: **8324 returned, 8324 distinct, 0 missed**,
per-date distribution identical to §1.

### 5 — MAKING IT LOUD

`tools.health` has reported this correctly since it was written. Reporting is
not being read: the gap surfaced three days late because an unrelated session
happened to run the sweep.

**The alert cannot live in the daemon, because the daemon not running IS the
failure.** An alert fired from `run.py`'s `finally` is silent under exactly the
conditions that make it necessary. So:

- **`outcomes.alert_unscored()`** — one CRITICAL push naming the dates, the true
  count, and the fix command. Routed through `intraday.notifier.Notifier` with
  `framework="SWING"`, mirroring `kite.token_manager.alert_if_stale()`; a broken
  learning loop is a whole-account event. Never raises.
- **`--check-and-alert`** — standalone CLI, writes nothing, exits 1 when it
  alerts.
- **`outcomes_watch`** — a new job in `brain_scheduler.yml` on `30 3 * * *`,
  **daily including weekends**, because a check that runs only on trading days
  shares the blind spot it exists to cover. It would have said so on Saturday
  morning. The non-zero exit makes a red Actions run a second, independent
  channel.
- **`run_pipeline.py` step `28a_resolve_intraday`** — the structural fix. Puts
  intraday resolution on the same scheduled footing as its two sibling steps
  (swing outcomes, allocator outcomes) instead of a daemon shutdown. Non-fatal,
  alerts on anything it could not finish.
- **`review_engines`** now warns and alerts when its own window contains
  unscored sessions, at the moment of harm.
- **`resolve_day`** returns `date` / `remaining` / `complete`, logs a partial day
  through `logger.warning` rather than `logger.success`, and `backfill` reports
  `incomplete` — because "no broker" and "nothing to do" both used to return 0.

Verified against the live book with delivery stubbed (nothing sent, nothing
written): headline `1289 intraday detection(s) across 1 session(s) were never
scored`, urgency `CRITICAL`, detail naming `2026-08-14 (1289)` and the backfill
command.

### 6 — THE BACKFILL IS BLOCKED, AND NO DATE IS LOST

```
python -m intraday.outcomes --backfill
  outcomes: 1 past session(s) never scored — 1289 detection(s) ...
    backfilling 2026-08-14 (1289 unresolved)
  Kite access token EXPIRED (issued 2026-08-14 02:56 IST, 36.1h ago)
  outcomes: no broker session — 1289 setup(s) on 2026-08-14 stay unresolved
  outcomes: backfill could NOT finish 2026-08-14 (1289 left, no_broker)
```

That last line is new. Pre-fix this returned `{"days": 1, "resolved": 0}` —
identical to what a fully-scored book returns.

**No date is permanently lost.** 14-Aug is one calendar day old, far inside
Kite's minute-bar retention, and this system has replayed older bars routinely
(28-29 July were backfilled on 31 July). The sole blocker is the expired access
token, and refreshing it is a Zerodha login the operator must perform. Two steps:

```
python -m kite.token_manager --login-url     # operator completes the login
python -m intraday.outcomes --backfill       # then this finishes 14-Aug
```

Nothing was approximated and no outcome was fabricated. `health` will stay red
on `learning` until that runs, which is correct.

### 7 — EACH TEST FAILED FIRST

`tests/test_outcome_resolution_gap.py`, 19 checks, registered in
`tools/verify.py::MODULES`. **14 of 17 failed against the unfixed code** on
first run. Two of the three that passed are the guards asserting the fake
itself truncates — without them every paging test would pass against the broken
code and prove nothing.

Re-run with paging reverted to the shipped idiom, the tests reproduce the live
symptom verbatim:

```
FAIL  resolve_day resolves every row      -> resolved 1000 of 2289
FAIL  unresolved_days counts past the cap -> reported {'2026-08-14': 1000}
FAIL  unresolved_days cannot hide a date  -> 13-Aug vanished behind 14-Aug's 1200
FAIL  weekly review reads every row       -> handed 1000 of 2400
FAIL  restart dedup map reads every row   -> rehydrated 1000 of 1800
FAIL  the alert carries the true count    -> "1000 detection(s)"
```

And with the sort key removed, the two §4 tests fail:

```
FAIL  paging without a sort key -> 2289 rows but only 2215 distinct
FAIL  a table without an id     -> rows lost under a custom key
```

### 8 — FOUND ALONG THE WAY

**F-20 — `resolve_day` destroyed its own date variable.** `d` was bound to the
trade date at the top of the function; the row loop then ran
`d = D.normalise(r.get("direction"))`, so from the first row onward the date was
gone and the success log — `f"outcomes {d}: ..."` — printed the last setup's
**direction**: `outcomes LONG: 1000 resolved`. The direction arithmetic was
always correct. The one line that tells you *which session was scored* was not,
and it is the line you go looking for when asking why a day never was. Renamed
to `dirn`; `health`'s `_SHORT_SPINE` marker caught the rename immediately, which
is that check working.

**F-21 — `health.check_learning_loop`'s success message could never move.** Its
"N resolved outcomes on hand" was `len()` of a `.limit(1000)` read, so it would
have read exactly "1000" forever once the table passed a thousand rows. Now a
`count='exact'` header.

**F-22 — `test_setup_rehydration`'s fake swallowed a real breakage.** Its
`_FakeQuery` had no `range`/`order`, so once `_rehydrate_recorded` was paged the
call raised `AttributeError`, was caught by that function's non-fatal `except`,
and every test in the module saw an empty map — 4 of 7 failing. Both methods
added with the reason recorded. Worth noting the failure mode: a fake missing a
method the production code now calls turns into a **silent empty result**, not
an error, because the code under test is deliberately non-fatal there.

### 9 — NOT DONE

- **14-Aug is still unscored.** Blocked on the operator's Kite login (§6). Every
  code path needed to finish it is in place and tested; nothing else stands
  between the token and a complete book.
- **`outcomes_watch` has never fired.** The YAML parses and the job is wired
  (`python -m intraday.outcomes --check-and-alert`), but a GitHub Actions cron
  cannot be exercised from here. First real proof is its 09:00 IST run.
- **No alert was actually delivered.** The live verification in §5 stubbed
  `Notifier.send`. Delivery itself is exercised only by the mocked tests, and
  sending a real push was not mine to do unasked.
- **`engine.py:952` (runway requeue) is still unpaged.** One day, filtered to
  `cost_verdict=BLOCKED_SHORTABILITY`, so it is far from 1000 today — but it is
  the same idiom on the same growing table. Left alone deliberately: it is
  outside this brief and changing it needs its own test.
- **Nothing was done about WHY 14-Aug produced 2289 detections** when 28-Jul
  produced 236. A 10x rise in detections per session in three weeks is either a
  universe change, a dedup regression, or a genuinely busier tape, and it is
  what pushed every reader past the cap. Worth a stage of its own.

**Gate: PASS** — root cause identified as the row cap rather than the assumed
missing run, the full unscored-date list established by paging and confirmed
server-side before any fix, five capped readers repaired, a worse bug introduced
by the first fix caught before shipping and pinned with its own failing test,
the failure made loud on a schedule that does not depend on the daemon that
failed, 14 of 17 tests demonstrated failing first, `tools.verify` 500/500 and
`tools.simulate` unchanged. The backfill is blocked on an operator credential
step and is reported as blocked rather than approximated.

---

## 2026-08-15 — Engine re-score on the complete population — the truncation never reached Stage 2, which read all 6035 rows and reproduces to the third decimal; what it silenced was the WEEKLY REVIEW, which has held every engine since 5-Aug and on the full book says RETIRE/SHADOW/SHADOW and has never once seen the short engine

Branch `diagnostic/engine-rescore-complete` off `main`. **READ-ONLY — no source
file was modified.** Four read-only scratchpad scripts: `fetch_full.py`,
`replicate_stage2.py`, `reader_probe.py`, `rescore.py` / `engine_split.py` /
`finish.py` / `review_forecast.py`. Every table below is drawn from one cached
paged read of `intraday_setups`, verified before use.

---

### 0 — THE READER CHECK THE BRIEF ASKED FOR, BEFORE ANY NUMBER

The brief said not to use `review_engines` or `engine_scorecard` until their
readers were confirmed paged. Both were checked by replicating each one's own
idiom verbatim against the live table. **They are not in the same state, and the
F-19 ledger entry above is wrong about one of them.**

```
true row count: 8324

weekly_review.py:267  -> config.fetch_all(...)          [SORTED paging]
  trial 1: returned 8324  distinct 8324  missed 0
  trial 2: returned 8324  distinct 8324  missed 0

engine_scorecard.py:55 -> _fetch(): .range(), NO .order()  [UNSORTED paging]
  trial 1: returned 8324  distinct 8324  duplicates 0  rows never returned 0
  trial 2: returned 8324  distinct 8324  duplicates 0  rows never returned 0
  trial 3: returned 8324  distinct 8324  duplicates 0  rows never returned 0
```

**`review_engines` is sound** — `fetch_all` with `order_by` defaulting to `id`.
Fixed by F-19, verified here.

**`engine_scorecard` is NOT capped at 1000.** F-19 §3's table lists it as
`saw 1000 / should see 8324`; that is incorrect. `_fetch` has paged with
`.range()` since it was written. What it does not do is sort — it is the §4 trap
still standing in the one reader F-19 did not open. On three trials today the
planner happened to be stable and returned all 8324 distinct, so it is not
corrupting output right now; the *guarantee* is absent, and F-19 measured this
exact idiom on this exact table returning 5000 distinct of 8324 one day earlier.
**Neither tool was used to produce any number below.** Everything is computed
from `config.fetch_all`.

The cached read was verified before a single statistic was taken from it:

```
server-side count : 8324
rows returned     : 8324
distinct ids      : 8324
rows never seen   : 0
```

---

### 1 — THE PREMISE IS HALF RIGHT, AND THE HALF THAT IS WRONG MATTERS MOST

The brief states that every engine-level conclusion in this ledger was computed
on the biased 1000-row window. **That is not true of Stage 2, and it is
demonstrable rather than arguable.** Stage 2 declared its population as
`6035 raw / 890 dedup / 13 sessions`. The complete table today is 8324 rows, of
which 14-Aug is 2289. 8324 − 2289 = **6035, exactly**. Restricting the verified
paged read to `trade_date < 2026-08-14` and applying Stage 2's stated
construction:

```
raw rows        6035      Stage 2 said 6035
dedup keys      890       Stage 2 said 890
TAKEN keys      170       Stage 2 B.2b said 170
never-TAKEN     720       Stage 2 B.3 said 720
sessions        13        Stage 2 said 13
```

B.2b reproduces on every engine, on every column:

```
  eng     n  S2 n    tgt%     S2    grossR       S2    costR      S2     netR      S2  match
  VWR    49    49   16.3%   16.3    -0.310   -0.310   +0.314  +0.314   -0.624  -0.624  YES
  ORB    41    41   14.6%   14.6    -0.156   -0.156   +0.187  +0.187   -0.344  -0.344  YES
  VCE    30    30   20.0%   20.0    -0.201   -0.201   +0.268  +0.268   -0.470  -0.470  YES
  SDN    24    24   20.8%   20.8    +0.017   +0.017   +0.284  +0.284   -0.267  -0.267  YES
  GAP    15    15   20.0%   20.0    +0.063   +0.063   +0.163  +0.163   -0.101  -0.101  YES
  PDL     8     8   25.0%   25.0    -0.064   -0.064   +0.427  +0.427   -0.491  -0.491  YES
  PBK     2     2    0.0%    0.0    -1.000   -1.000   +0.347  +0.347   -1.347  -1.347  YES
  RNG     1     1    0.0%    0.0    -1.000   -1.000   +0.448  +0.448   -1.448  -1.448  YES
  ALL   170   170   17.6%   17.6    -0.175   -0.175   +0.264  +0.264   -0.440  -0.440  SE 0.093 vs 0.093
```

B.3a reproduces **every n exactly** (270, 252, 237, 227, 174, 149, 61, 30, 26,
24, 23). Four buckets differ in the third decimal of gross R, and the cause is
visible in Stage 2's own printing: it reported `res` < `n` on precisely those
four (`BELOW_CONVICTION` 267/270, `REJECTED_COST` 246/252, `BLOCKED_STRUCTURE`
235/237, `VETOED_AI` 224/227, `BLOCKED_EVENT` 29/30). Those rows were unresolved
on 14-Aug and are resolved now. **Newly-scored outcomes, not missing rows.**

**Stage 2 read the whole population for its window.** Its scratchpad scripts
paged; the capped idiom lived in the shipped tooling, not in that analysis. The
truncation is real, it is serious, and it did its damage somewhere else.

---

### 2 — POPULATION [DET-INTRA-FULL]

| tag | table | n | what a row is |
|---|---|---|---|
| **[DET-INTRA-FULL]** | `intraday_setups`, complete | 8324 raw / 1102 dedup / 14 sessions | an intraday detection and its counterfactual |

```
raw rows                     8324      resolved 8324      unresolved 0
sessions                     14
dedup keys (date,sym,engine) 1102   of which TAKEN 211, never-TAKEN 891
R-computable dedup keys      1102 of 1102        rows with no risk_pct>0: 0
14-Aug adds 2289 rows (27.5% of the table) and 212 keys (19.2% of the keys)
```

**Two constructions are used below and they are not interchangeable.** Table 3
represents each key by its **first detection by `ts`** (`dedupe_setups`' rule).
Tables 4–6 represent a TAKEN key by its **first TAKEN row** — Stage 2's B.2b/B.3
rule — so the comparison is like-for-like. The two disagree on 3 of 1102 keys,
where the first detection and the first TAKEN detection resolved differently
(179/1102 = 16.2% target vs 176/1102 = 16.0%). Stated because a future session
that mixes them will chase a 0.2pp ghost.

PRE-1 from Stage 2 still governs: `cost_pct` is 0 on every bucket blocked before
the cost gate, so **gross R is the only quantity both sides of a gate carry**,
and every cross-gate comparison below is made on it.

---

### 3 — PER ENGINE, COMPLETE POPULATION, ALL DEDUP KEYS

Every detection the engine produced, taken or refused. `n<30` flagged per the
brief.

```
  engine    n   target          grossR         costR      netR
  SDN     398   67/398 = 16.8%  -0.077±0.072  +0.052    -0.129
  VWR     307   53/307 = 17.3%  -0.345±0.071  +0.229    -0.575
  VCE     138   25/138 = 18.1%  -0.155±0.118  +0.130    -0.284
  ORB     119   12/119 = 10.1%  -0.241±0.100  +0.136    -0.377
  RNG      60    7/60  = 11.7%  -0.137±0.157  +0.040    -0.177
  PBK      32    4/32  = 12.5%  -0.276±0.244  +0.329    -0.604
  PDL      25    6/25  = 24.0%  +0.064±0.332  +0.285    -0.221   << n<30 INSUFFICIENT
  GAP      22    5/22  = 22.7%  +0.100±0.276  +0.119    -0.019   << n<30 INSUFFICIENT
  GDB       1    0/1   =  0.0%  -1.000±0.000  +0.000    -1.000   << n<30 INSUFFICIENT
  ---------------------------------------------------------------------------
  ALL    1102  179/1102 = 16.2% -0.182±0.040  +0.134    -0.317
```

**Six engines now clear n≥30 on all keys** (SDN 398, VWR 307, VCE 138, ORB 119,
RNG 60, PBK 32) where Stage 2's TAKEN-only view cleared three at n≥20. Every one
of the six is negative on gross R. Only ORB separates from zero by more than 2 SE
(−0.241 ± 0.100, −2.4 SE), and VWR by −4.9 SE (−0.345 ± 0.071).

**Engine and direction are collinear.** This was not visible at Stage 2's sample
and it governs how every table here may be read:

```
  SDN    n=398   LONG    0 (  0.0%)  SHORT  398 (100.0%)
  VWR    n=307   LONG  307 (100.0%)  SHORT    0 (  0.0%)
  VCE    n=138   LONG  138 (100.0%)  SHORT    0 (  0.0%)
  ORB    n=119   LONG  119 (100.0%)  SHORT    0 (  0.0%)
  RNG    n=60    LONG   60 (100.0%)  SHORT    0 (  0.0%)
  PBK    n=32    LONG   32 (100.0%)  SHORT    0 (  0.0%)
  PDL    n=25    LONG   25 (100.0%)  SHORT    0 (  0.0%)
  GAP    n=22    LONG   22 (100.0%)  SHORT    0 (  0.0%)
  GDB    n=1     LONG    1 (100.0%)  SHORT    0 (  0.0%)
```

**SDN *is* the short book — every other engine is 100% LONG.** So the per-engine
TAKEN-vs-blocked splits in §4 are already direction-matched and need no
adjustment; but any comparison *between* SDN and another engine is also a
long-versus-short comparison, and cannot be read as an engine verdict. This is
the B.3b lesson relocated to the engine axis.

---

### 4 — PER ENGINE: TAKEN vs BLOCKED, DIRECTION-MATCHED

`dR` is printed only when **both** sides clear n=30. Without that guard an n=1 or
n=2 baseline has SE=0 by construction, the pooled SE collapses, and the first
pass of this analysis produced "RNG blocked beats RNG taken by +5.5 SE" out of a
single observation and "+2.8 SE" for PBK out of two identical stop-outs. Neither
is a result; both are printed here as the reason the guard exists.

```
  SDN  [SHORT]  all keys n=398
    TAKEN     n=  42  days= 6  target   7/42   = 16.7%  grossR +0.108±0.167  costR +0.273  netR -0.165
    blocked   n= 356  days= 7  target  60/356  = 16.9%  grossR -0.116±0.076  costR +0.043  netR -0.159  |  tgt +0.2pp  dR -0.225 = -1.2 SE

  VWR  [LONG]   all keys n=307
    TAKEN     n=  57  days=13  target   9/57   = 15.8%  grossR -0.332±0.149  costR +0.314  netR -0.645
    blocked   n= 250  days=14  target  43/250  = 17.2%  grossR -0.353±0.081  costR +0.212  netR -0.565  |  tgt +1.4pp  dR -0.022 = -0.1 SE

  VCE  [LONG]   all keys n=138
    TAKEN     n=  37  days=11  target   9/37   = 24.3%  grossR -0.069±0.251  costR +0.269  netR -0.338
    blocked   n= 101  days=12  target  15/101  = 14.9%  grossR -0.221±0.131  costR +0.080  netR -0.301  |  tgt -9.5pp  dR -0.152 = -0.5 SE

  ORB  [LONG]   all keys n=119
    TAKEN     n=  49  days=12  target   6/49   = 12.2%  grossR -0.210±0.150  costR +0.188  netR -0.398
    blocked   n=  70  days=10  target   6/70   =  8.6%  grossR -0.278±0.135  costR +0.109  netR -0.387  |  tgt -3.7pp  dR -0.068 = -0.3 SE

  RNG  [LONG]   all keys n=60
    TAKEN     n=   1  << n<30
    blocked   n=  59  days=11  target   7/59   = 11.9%  grossR -0.122±0.159  costR +0.033  netR -0.155  |  dR SUPPRESSED — a side is under n=30

  PBK  [LONG]   all keys n=32
    TAKEN     n=   2  << n<30
    blocked   n=  30  days= 3  target   4/30   = 13.3%  grossR -0.278±0.261  costR +0.325  netR -0.603  |  dR SUPPRESSED

  PDL n=25 (TAKEN 8 / blocked 17) · GAP n=22 (TAKEN 15 / blocked 7) · GDB n=1   << all n<30 INSUFFICIENT

  BOOK LEVEL
    TAKEN     n= 211  days=14  target  36/211  = 17.1%  grossR -0.141±0.083  costR +0.263  netR -0.404
    blocked   n= 891  days=14  target 140/891  = 15.7%  grossR -0.207±0.045  costR +0.112  netR -0.320  |  tgt -1.3pp  dR -0.066 = -0.7 SE
```

**Not one engine separates its taken half from its refused half at 2 SE, and
four of the six cannot be tested at all** because their TAKEN or blocked side is
under 30. The two that can be tested (SDN −1.2 SE, VWR −0.1 SE) both lean the
working direction. At book level the gates are +0.7 SE of nothing.

---

### 5 — B.3 COUNTERFACTUAL AT FULL SCALE

```
reasons carried per never-taken key: {1: 412, 2: 195, 3: 140, 4: 87, 5: 43, 6: 14}
```

**POOLED**

```
  TAKEN [baseline]         n= 211  target  36/211  = 17.1%  grossR -0.141±0.083  costR +0.263  netR -0.404
  ---------------------------------------------------------------------------------------------------
  BELOW_CONVICTION         n= 366  target  77/366  = 21.0%  grossR +0.015±0.076  |  tgt +4.0pp   dR +0.156 = +1.4 SE
  REJECTED_COST            n= 337  target  55/337  = 16.3%  grossR -0.168±0.087  |  tgt -0.7pp   dR -0.027 = -0.2 SE
  BLOCKED_STRUCTURE        n= 302  target  58/302  = 19.2%  grossR -0.152±0.082  |  tgt +2.1pp   dR -0.011 = -0.1 SE
  VETOED_AI                n= 276  target  47/276  = 17.0%  grossR -0.034±0.089  |  tgt -0.0pp   dR +0.107 = +0.9 SE
  BLOCKED_SHORTS_MARKET    n= 258  target  48/258  = 18.6%  grossR +0.015±0.094  |  tgt +1.5pp   dR +0.156 = +1.2 SE
  BLOCKED_SHORTABILITY     n= 154  target  12/154  =  7.8%  grossR -0.288±0.099  |  tgt -9.3pp   dR -0.147 = -1.1 SE
  BLOCKED_SHORTS_OFF       n=  61  target   8/61   = 13.1%  grossR -0.424±0.145  |  tgt -3.9pp   dR -0.283 = -1.7 SE
  BLOCKED_EVENT            n=  37  target   4/37   = 10.8%  grossR -0.513±0.193  |  tgt -6.3pp   dR -0.372 = -1.8 SE
  BLOCKED_REENTRY n=26 · SHADOW n=26 · BLOCKED_CROSS_FRAMEWORK n=26   << all n<30 INSUFFICIENT
```

**LONG only** — baseline TAKEN LONG n=169, 17.2%, grossR −0.203±0.095

```
  BELOW_CONVICTION         n= 243  target  43/243  = 17.7%  grossR -0.120±0.083  |  tgt +0.5pp   dR +0.083 = +0.7 SE
  REJECTED_COST            n= 180  target  23/180  = 12.8%  grossR -0.402±0.098  |  tgt -4.4pp   dR -0.199 = -1.5 SE
  BLOCKED_STRUCTURE        n= 172  target  27/172  = 15.7%  grossR -0.143±0.113  |  tgt -1.5pp   dR +0.060 = +0.4 SE
  VETOED_AI                n= 112  target  19/112  = 17.0%  grossR +0.034±0.138  |  tgt -0.2pp   dR +0.237 = +1.4 SE
  BLOCKED_EVENT            n=  30  target   4/30   = 13.3%  grossR -0.399±0.233  |  tgt -3.8pp   dR -0.196 = -0.8 SE
  SHADOW n=26 · BLOCKED_REENTRY n=20 · BLOCKED_CROSS_FRAMEWORK n=8   << all n<30 INSUFFICIENT
```

**SHORT only** — baseline TAKEN SHORT n=42, 16.7%, grossR +0.108±0.167. The
short baseline clears n=30 for the first time (Stage 2 had n=24).

```
  BLOCKED_SHORTS_MARKET    n= 258  target  48/258  = 18.6%  grossR +0.015±0.094  |  tgt +1.9pp   dR -0.093 = -0.5 SE
  VETOED_AI                n= 164  target  28/164  = 17.1%  grossR -0.080±0.116  |  tgt +0.4pp   dR -0.188 = -0.9 SE
  REJECTED_COST            n= 157  target  32/157  = 20.4%  grossR +0.099±0.148  |  tgt +3.7pp   dR -0.009 = -0.0 SE
  BLOCKED_SHORTABILITY     n= 154  target  12/154  =  7.8%  grossR -0.288±0.099  |  tgt -8.9pp   dR -0.396 = -2.0 SE
  BLOCKED_STRUCTURE        n= 130  target  31/130  = 23.8%  grossR -0.164±0.117  |  tgt +7.2pp   dR -0.272 = -1.3 SE
  BELOW_CONVICTION         n= 123  target  34/123  = 27.6%  grossR +0.281±0.151  |  tgt +11.0pp  dR +0.173 = +0.8 SE
  BLOCKED_SHORTS_OFF       n=  61  target   8/61   = 13.1%  grossR -0.424±0.145  |  tgt -3.6pp   dR -0.532 = -2.4 SE
  BLOCKED_CROSS_FRAMEWORK n=18 · BLOCKED_EVENT n=7 · BLOCKED_REENTRY n=6   << all n<30 INSUFFICIENT
```

**B.3e — the gates the counterfactual structurally cannot see.** Unchanged in
kind, sharper in degree: **91 of 91** `ALLOCATOR_DECLINED` dedup keys and **11 of
11** `BLOCKED_PAPER_CAPACITY` keys also carry a TAKEN row, so both are absorbed
into the baseline rather than appearing as buckets.

```
  TAKEN, allocator never declined  n= 120  target  21/120  = 17.5%  grossR -0.143±0.110
  TAKEN, but ALSO declined         n=  91  target  15/91   = 16.5%  grossR -0.138±0.128  |  tgt -1.0pp  dR +0.004 = +0.0 SE
  TAKEN, but ALSO cap-blocked      n=  11   << n<30 INSUFFICIENT
```

**The allocator is measurably nothing at +0.0 SE on n=91**, up from +0.4 SE on
n=63. The null got cleaner, not weaker.

---

### 6 — WHAT CHANGES AGAINST STAGE 2's B.2, AND WHAT HOLDS

**Nothing here changes because Stage 2 was truncated — it was not (§1). Every
delta below is 14-Aug's 2289 rows arriving, plus the handful of rows Stage 2
recorded as unresolved.**

**CHANGES:**

1. **`BLOCKED_EVENT` loses the only 2-SE result in the ledger.** Stage 2's
   headline was "exactly one bucket separates from TAKEN by more than 2 SE, and
   it separates in the direction that says the gate is working: −2.5 SE". At full
   scale it is **−1.8 SE pooled** (n=37) and **−0.8 SE on LONG** (n=30). The sign
   holds; the significance does not. **"BLOCKED_EVENT is measurably correct" must
   not be quoted forward.**
2. **Two short gates cross 2 SE in the working direction, direction-matched.**
   `BLOCKED_SHORTABILITY` on shorts moves −1.2 → **−2.0 SE** (7.8% target vs
   16.7%), and `BLOCKED_SHORTS_OFF` on shorts −1.6 → **−2.4 SE**. These are now
   the only two buckets clearing the bar, and both say the gate is refusing worse
   setups than it takes.
3. **SDN is no longer thin and it is no longer the best bucket.** Stage 2 had it
   at n=24 TAKEN, gross **+0.017**, and both Stage 1 and Stage 2 warned against
   calling it profitable. At n=42 TAKEN it is **+0.108±0.167**, and on all 398 of
   its keys **−0.077±0.072**. The engine-level reading is negative; the TAKEN
   subset's positive number is a 42-row slice of it and is within 1 SE of zero.
4. **VCE's taken half improves and its refused half does not.** Stage 2: n=30,
   20.0% target, gross −0.201. Now n=37, **24.3%**, gross **−0.069**, against a
   blocked half at 14.9% / −0.221. Still −0.5 SE; directionally the one engine
   whose gates look like they are selecting.
5. **ORB gets worse and is now the weakest bucket that can be measured.** Stage 2
   TAKEN: n=41, 14.6%, −0.156. Now TAKEN n=49, **12.2%**, −0.210; on all 119 keys
   **10.1% target, −0.241±0.100 (−2.4 SE from zero)**.
6. **RNG and PBK stop being footnotes.** Stage 2 saw them only through TAKEN
   (n=1, n=2). On all keys they are n=60 and n=32 — both above the brief's bar,
   both negative, and **RNG has 60 detections of which exactly one was ever
   taken.** That is not an engine verdict, it is a stop-placement finding (§9).
7. **A ninth bucket exists.** `GDB` — 1 row, NATIONALUM, 14-Aug, `VETOED_AI`.
   This is the "gap down > 1%" candidate approved on 5-Aug and launched on
   10-Aug per `brain_proposals`. First detection recorded 14-Aug.
8. **`BELOW_CONVICTION` on shorts weakens.** +1.2 → **+0.8 SE** (27.6% vs 16.7%
   target, n=123 against a now-n=42 baseline). Pooled it holds at +1.4 SE.

**HOLDS:**

1. **No gate is inverted at the 2-SE bar.** The full-scale answer is the same as
   Stage 2's, and the two buckets that now clear 2 SE clear it in the *working*
   direction. Nothing points backwards at that bar.
2. **`BELOW_CONVICTION` remains the single inversion candidate and remains
   unestablished** — +1.4 SE pooled on n=366, +0.8 SE direction-matched. Stage
   2's "the most valuable thing for Stage 3 to settle" stands, unsettled.
3. **The allocator is neutral** — now +0.0 SE on n=91.
4. **Every measurable engine is negative on gross R.** Six clear n=30 and all six
   are negative. This is a signal problem, unchanged.
5. **PRE-1 holds and is load-bearing** — `cost_pct` is still 0 on every
   pre-cost-gate bucket (visible as `costR +0.000` throughout §5).
6. **B.3d's warning holds.** Raw target rate is 13.9% against 16.2% deduplicated
   on the same 8324 rows; the pseudo-replication factor is now √(8324/1102) ≈ 2.7.
7. **No bucket meets the retirement bar.** n≥30 **and** negative gross R **and**
   failure against a random baseline: the third condition is met by nothing here.

---

### 7 — B.3's QUESTION AT PROPER SCALE: THE 13.5% vs 21% GAP

**The gap does not hold, and the two numbers were never in the same unit.** Four
rates, of which only two are comparable:

```
  RAW ROWS (pseudo-replicated: one setup re-detected every 15s counts N times)
  14-Aug, full day, raw                                  257/2289  =  11.2%
  whole population, raw                                 1156/8324  =  13.9%
  whole population, raw, TAKEN rows only                 152/1382  =  11.0%

  DEDUP KEYS (symbol, strategy, date) — the honest denominator
  whole population, all dedup keys                       179/1102  =  16.2%
  14-Aug, all dedup keys                                  39/212   =  18.4%
  whole population, TAKEN                                 36/211   =  17.1%
  whole population, never-TAKEN                          140/891   =  15.7%
  14-Aug, TAKEN                                            6/41    =  14.6%
```

**`174/1289 = 13.5%` is not 14-Aug's full-day rate.** It is the rate of the
**backfilled subset** — the 1289 rows `resolve_day` could not reach behind the
cap. 14-Aug is 2289 rows and its full-day raw rate is **11.2%** (257 TARGET /
1465 STOP / 567 TIMEOUT). The 1000 rows already scored before the backfill hit
~8.3%; the backfilled remainder hit ~13.5%. Deduplicated — the only construction
in which the number means anything — **14-Aug is 18.4%, the best session in the
back half of the book**, not the worst.

**`21%` is `11/53` from Stage 1 A.1** — TAKEN detections that became *real
positions*. It is a dedup rate on n=53, not a raw rate, and not the TAKEN
population. At full scale the TAKEN population is **17.1% (36/211)**.

Same-unit, direction-matched, on all 8324 rows:

```
  -- LONG --
  TAKEN LONG               29/169  = 17.2%   grossR -0.203±0.095
  never-TAKEN LONG         80/535  = 15.0%   grossR -0.268±0.055   dR -0.065 = -0.6 SE   tgt -2.2pp
  -- SHORT --
  TAKEN SHORT               7/42   = 16.7%   grossR +0.108±0.167
  never-TAKEN SHORT        60/356  = 16.9%   grossR -0.116±0.076   dR -0.225 = -1.2 SE   tgt +0.2pp
```

**Direction-matched, the taken and refused halves of the book are the same
population.** −0.6 SE on longs, −1.2 SE on shorts, both leaning the working way.
There is no 7.5-point gap to explain: the gap was an artifact of comparing a raw
subset rate against a dedup sub-sub-population rate, which is exactly the trap
B.3d was written to record.

Per-session, deduplicated, for whoever wants the shape:

```
  date          keys    tgt%  taken    tgt%  grossR(all)  grossR(taken)
  2026-07-28      18   16.7%     15   20.0%       -0.307         -0.168
  2026-07-29       5   60.0%      1  100.0%        1.288          2.503
  2026-07-30      15   13.3%      7   28.6%       -0.419         -0.072
  2026-07-31      59   22.0%     21   14.3%        0.012         -0.317
  2026-08-03      50   14.0%     11   27.3%       -0.233          0.024
  2026-08-04      26    3.8%      8    0.0%       -0.630         -0.360
  2026-08-05      25   12.0%     12   16.7%       -0.344         -0.375
  2026-08-06      78   12.8%     10   10.0%       -0.305         -0.358
  2026-08-07      57   17.5%     16   31.2%       -0.096          0.366
  2026-08-10      77   13.0%     18   16.7%       -0.347         -0.204
  2026-08-11      50   14.0%      4   25.0%       -0.230         -0.136
  2026-08-12     251   19.5%     33   12.1%       -0.126         -0.275
  2026-08-13     179   12.3%     14   14.3%       -0.247         -0.323
  2026-08-14     212   18.4%     41   14.6%       -0.077          0.002
```

---

### 8 — WHAT THE TRUNCATION ACTUALLY INVALIDATES

Named, per the brief. The list is shorter than expected in one direction and
worse in another.

**INVALIDATED — every verdict the shipped `review_engines` has ever produced,
and more importantly every verdict it FAILED to produce.** Replicating the
review's own logic (`dedupe_setups`, the 0.589% tradeable floor, MIN_SAMPLE=20,
MIN_SESSIONS=10, the hit-rate ladder) on three windows:

```
--- COMPLETE population, 14 sessions: 8324 raw -> 1102 setups ---
  engine     all   hit  | tradeable   hit    avg%  days   verdict
  SDN        398  17%  |        84  10%   -0.06     7   hold     84 tradeable but 7 session(s)
  VWR        307  17%  |        84  21%   -0.23    13   SHADOW   21% of 84 — detects, does not deliver
  VCE        138  18%  |        71  23%   -0.12    13   SHADOW   23% of 71 — detects, does not deliver
  ORB        119  10%  |        68   7%   -0.42    12   RETIRE   7% of 68 over 12 sessions
  RNG         60  12%  |         0   0%    0.00     0   hold     all 60 below the 0.59% floor
  PBK         32  12%  |         1   0%   -0.59     1   hold     only 1 tradeable (31 below floor)
  PDL         25  24%  |         0   0%    0.00     0   hold     all 25 below the 0.59% floor
  GAP         22  23%  |        21  19%   -0.10     4   hold     21 tradeable but 4 session(s)
  GDB          1   0%  |         1   0%   -0.60     1   hold     only 1 tradeable

--- the TRUNCATED window the shipped review actually read: 1000 raw -> 187 setups ---
  VWR         44  18%  |        18  17%   -0.32     6   hold
  VCE         34  15%  |        21  19%   -0.31     6   hold
  PBK         32  12%  |         1   0%   -0.59     1   hold
  PDL         25  24%  |         0   0%    0.00     0   hold
  ORB         23  13%  |        21  10%   -0.51     5   hold
  GAP         22  23%  |        21  19%   -0.10     4   hold
  RNG          7   0%  |         0   0%    0.00     0   hold

  ORB  hold -> RETIRE      VWR  hold -> SHADOW      VCE  hold -> SHADOW
  SDN  absent -> hold      GDB  absent -> hold
```

**The truncation was not producing wrong verdicts. It was producing NO
verdicts** — `hold` on every engine, every week, for the same reason each time:
too few tradeable outcomes, too few sessions. A review that always holds is
indistinguishable from a review that is working and finding nothing, which is
why this ran for ten days without anyone noticing.

**It also made two engines invisible.** The first 1000 rows by id end mid-5-Aug:

```
  2026-07-28  236/236   2026-07-29  5/5     2026-07-30  45/45
  2026-07-31  174/174   2026-08-03  135/135 2026-08-04  36/36   2026-08-05  369/530
  engines present: GAP ORB PBK PDL RNG VCE VWR
  engines ABSENT:  SDN (first seen 06-Aug)   GDB (first seen 14-Aug)
```

**`SDN` — the short engine, the largest bucket in the book at n=398 — has never
once appeared in a weekly review.** The same window that hid it is the reason
the review has never had an opinion on the entire short programme.

**INVALIDATED — the silence in `brain_proposals` since 5-Aug.** The last
`ENGINE_LIFECYCLE` proposal was `GAP -> SHADOW` on 5-Aug. The last review run was
`weekly_20260810_225957`:

```
  created 2026-07-30  run weekly_20260731_013855  ENGINE_LIFECYCLE  PDL -> RETIRE       SUPERSEDED
  created 2026-07-31  run weekly_20260731_154933  ENGINE_LIFECYCLE  VWR -> PROMOTE      SUPERSEDED
  created 2026-07-31  run weekly_20260731_154933  ENGINE_PARAMETERS RNG -> widen stop   PENDING
  created 2026-08-05  run weekly_20260805_170312  ENGINE_LIFECYCLE  GAP -> SHADOW       REJECTED
  created 2026-07-31  run weekly_20260810_225957  ENGINE_PARAMETERS PDL -> widen stop   PENDING
```

The 10-Aug run refreshed **PDL's** parameter proposal and not **RNG's**. That is
the truncation's fingerprint, and it is exact: in the truncated window RNG had 7
detections, below the MIN_SAMPLE=20 that gates the "every detection is below the
cost floor" branch, so it could not fire. On the complete population RNG has
**60** detections, **all 60 below the floor** — the branch fires. **A real,
actionable RNG stop-placement proposal was suppressed by the row cap on at least
the 10-Aug run**, and the model that predicts this also predicts exactly the
proposal set that was observed. Nothing else in the ledger explains that row.

**NOT INVALIDATED — Stage 2's B.2, B.3, B.5, B.6, C.1–C.4.** §1 reproduces B.2b
and B.3a from the complete population, to the third decimal, on every n. Stage 2
paged. Its conclusions move only where 14-Aug moved them (§6), and it named its
own limits correctly.

**NOT INVALIDATED, BUT NOT USABLE EITHER — Stage 1's `engine_scorecard` table.**
It was not truncated; its n per engine (15/24/41/8/49/30/2/1) match the complete
pre-14-Aug population exactly. Its **R values** never matched Stage 2's on those
identical n, and the cause is an estimator difference, not a population one:

```
  eng     n   scorecard estimator   Stage 1 printed    |  Stage 2 estimator
  VWR    49                -0.261            -0.261                  -0.310
  ORB    41                -0.241            -0.242                  -0.156
  VCE    30                -0.337            -0.317                  -0.201
  SDN    24                +0.032            +0.050                  +0.017
  GAP    15                +0.061            +0.061                  +0.063
  PDL     8                -0.048            -0.052                  -0.064
  PBK     2                -0.682            -0.682                  -1.000
  RNG     1                -1.035            -1.101                  -1.000
```

`_intraday_observations` takes `grp[0]` — whichever row arrived first from an
**unsorted** read, not the earliest by `ts` — derives `risk_pct` from *that* row's
entry/stop, then averages `outcome_pct` and `cost_pct` over the *whole* group.
Reproducing that estimator recovers Stage 1's printed numbers (VWR −0.261 and
GAP +0.061 and PBK −0.682 exactly); the residuals on VCE/SDN/RNG are the
arrival-order tiebreak, **which is the point** — the estimator is not
reproducible run to run. Stage 1 already said no engine verdict should be quoted
from that table. That instruction now has a second, independent reason behind it.

---

### 9 — FOUND ALONG THE WAY

**F-23 — NINE readers of `intraday_setups` are still unpaged or unsorted at
HEAD, and one of them builds the priors.** F-19 fixed five and recorded
`engine.py:952` as the only survivor. Enumerating every reader of this table at
HEAD (`git grep 'table("intraday_setups")'`) and reading each one:

```
UNSORTED PAGING — right count, no guarantee which rows (the F-19 §4 hazard)
  tools/engine_scorecard.py:55     _fetch
  allocation/scoring.py:166        intraday_priors
  allocation/scoring.py:1077       regime-segmented priors
  tools/allocator_replay.py:74     _fetch_setups
  tools/control_room.py:475        _load_setups

UNPAGED — hard 1000-row cap, the original defect
  tools/weekly_review.py:457       review_gates
  tools/discover_engines.py:144    Pass A, refused-but-right
  tools/discover_engines.py:274    Pass B
  tools/simulate.py:101            the intraday engine scorecard
  intraday/engine.py:959           runway requeue (already flagged by F-19)
```

**`allocation/scoring.py` is the serious one.** It builds `intraday_priors()` —
the thing that prices every candidate the allocator sees — and it pages without
a sort key. F-19 §4 states the consequence for exactly this case: the count is
right so every sanity check passes, and the rows are silently over-weighted
toward whatever the planner repeated, so **every prior is biased and nothing
about its shape says so**. This is the same table, past the same cap, in the one
consumer where a biased read reaches live sizing decisions.

**`tools/simulate.py` is the one CLAUDE.md tells you to run first**, and its
engine scorecard reads 1000 of 8324 with no paging at all — the same truncation,
biased toward the oldest sessions, in the read-only preview the operator is
directed to trust before changing anything.

**`review_gates` decides whether a REFUSAL was correct.** Loosening a gate is a
one-way door toward more risk, and it was deciding that on a twelfth of the
evidence, weighted away from the recent sessions.

`engine_scorecard` compounds its unsorted read with an order-dependent
estimator: `grp[0]` is whichever row arrived first, so even a sound read leaves
the tool non-reproducible run to run (§8). Three trials today returned 8324
distinct, so nothing is corrupting output at this moment; F-19 measured this
same idiom on this same table returning 5000 distinct of 8324 one day earlier.

Nothing was fixed — this stage is read-only. **The count of unrepaired readers
was checked against HEAD, not against the working tree**, which was being
modified by another process during this session (§13).

**F-24 — no intraday engine is registered in `strategy_config`.** CLAUDE.md says
"The seven intraday engines — ORB, GAP, PDL, VCE, PBK, VWR, RNG — registered in
`strategy_config` with a lifecycle state". The table holds twelve rows and every
one is a swing family:

```
  ACC CTL EAP IAD MOM PEAD RSB RVS SBS SEC TPO VBD   (all ACTIVE, all enabled)
```

`review_engines` does `cur = (cfg.get(eng) or {}).get("lifecycle") or "ACTIVE"`,
so every intraday engine reads as ACTIVE regardless of its real state, and its
`lifecycle -> proposal` column has been printing a default rather than a fact for
the entire life of this tool. This is the silent-default landmine. Whether
anything downstream consumes an intraday lifecycle was **not** traced, so the
consequence is unknown — but the printed column is not evidence of anything.

**F-25 — RNG detects 60 setups and could take exactly one; PDL 25 and could take
none.** The tradeable stop floor is 0.589% and it excludes 60/60 of RNG, 25/25 of
PDL, 31/32 of PBK and 314/398 of SDN. These engines are not failing to deliver;
they are proposing trades the cost model correctly refuses before they are ever
scored. Every §3 R statistic for RNG/PDL/PBK is therefore drawn almost entirely
from setups the system would not take today. The review's own code says this
better than a ledger line can — its `g["n"] == 0` branch calls it a stop-placement
problem, "not the lifecycle" — and the truncation is why it has not said so about
RNG since 31-July.

**F-26 — 14-Aug produced 2289 detections against 28-Jul's 236, and nothing has
explained it.** Carried forward from F-19 §9 unchanged; this stage measured it
again (14-Aug is 27.5% of the whole table and 19.2% of the dedup keys) and did
not chase it. It is now the largest single influence on every pooled number in
this ledger.

---

### 10 — INSUFFICIENT, DECLARED

`n<30` on the deduplicated population, per the brief:

- **Engines, all keys:** PDL (25), GAP (22), GDB (1).
- **Engines, TAKEN side:** RNG (1), PBK (2), PDL (8), GAP (15) — four of nine
  engines cannot have their taken half compared to their refused half at all.
- **B.3 buckets, pooled:** BLOCKED_REENTRY (26), SHADOW (26),
  BLOCKED_CROSS_FRAMEWORK (26).
- **B.3 buckets, LONG:** SHADOW (26), BLOCKED_REENTRY (20),
  BLOCKED_CROSS_FRAMEWORK (8).
- **B.3 buckets, SHORT:** BLOCKED_CROSS_FRAMEWORK (18), BLOCKED_EVENT (7),
  BLOCKED_REENTRY (6).
- **B.3e:** TAKEN-but-cap-blocked (11).
- **BLOCKED_EVENT pooled is n=37** — it clears the bar, barely, and it is the
  bucket whose Stage 2 headline result did not survive.

---

### 11 — COULD NOT DETERMINE

- **Whether the counterfactual outcomes would have filled.** Inherited from
  Stage 1 and Stage 2 and it applies to every number here: `outcome_pct` is
  simulated against bars by `outcomes.resolve_day`, with no fill, slippage or
  queue position modelled.
- **Whether any engine's edge is real.** Six engines clear n=30 and all six are
  negative on gross R, but not one separates its taken half from its blocked half
  at 2 SE, and four cannot be tested at all. This is Gate 3's question.
- **Whether the RETIRE/SHADOW verdicts §8 forecasts are correct.** They are what
  the review's *own logic* returns on the complete population. Whether that logic
  is right — a hit-rate ladder on a floor-filtered subset, with no cost or R term
  in the verdict at all — is not established here and is a separate question.
- **Whether the intraday lifecycle column drives anything** (F-24).
- **Whether `engine_scorecard`'s unsorted read has ever actually corrupted a
  published number.** Three trials today were clean. Past runs cannot be replayed.
- **Why 14-Aug produced 10× 28-Jul's detections** (F-26).
- **Whether the regime axis is answerable yet.** Not re-checked; PRE-2 said 2 of
  13 sessions carried `regime_at_detection` and nothing in this brief touched it.

---

### 12 — RECOMMENDS

- **No retirements. That is Gate 3.** Stated plainly because §8 contains the
  string "RETIRE" next to ORB, and because ORB is genuinely the weakest
  measurable bucket (10.1% target on 119 keys, −0.241±0.100 gross R, −2.4 SE from
  zero). It still fails the third retirement condition — failure against a random
  baseline — which nothing in this entry tested.
- **Expect the Sunday 16-Aug `brain_sunday_chain` to propose `ORB -> RETIRE`,
  `VWR -> SHADOW` and `VCE -> SHADOW`.** These will be the first engine lifecycle
  proposals since 5-Aug and the first ever written from a complete read. They go
  to `brain_proposals` as PENDING and change nothing on their own. **Do not
  approve them on this entry** — read them as the truncation fix working, not as
  a verdict.
- **Treat F-23's nine readers as one job, and start with `allocation/scoring.py`.**
  It is the only one of them whose output reaches a live sizing decision, and an
  unsorted paged read there biases every prior in the system with no symptom.
  `tools/simulate.py` is second, because CLAUDE.md directs the operator to run it
  before changing anything and it currently reads 1000 of 8324.
- **Fix `engine_scorecard` before quoting it again** (F-23): add `.order("id")`
  to `_fetch`, and replace `grp[0]` with the earliest row by `ts` so it agrees
  with `dedupe_setups` and with itself between runs. Small and testable.
- **Stop quoting `BLOCKED_EVENT` as the gate that measurably works.** It was the
  ledger's only 2-SE result and it is now −1.8 SE. Two short gates took its place
  and they should be quoted with their direction attached:
  `BLOCKED_SHORTABILITY` −2.0 SE and `BLOCKED_SHORTS_OFF` −2.4 SE, **on shorts**.
- **Carry the engine/direction collinearity forward** (§3). SDN is the short
  book. Any future per-engine comparison that puts SDN beside a LONG engine is
  B.3b's error on a new axis.
- **`BELOW_CONVICTION` still needs settling and still is not settled** — +1.4 SE
  pooled on n=366. Two full stages have now pointed at it without reaching 2 SE.
  More sessions of the current book are not obviously going to close it; a
  designed test might.
- **Register the intraday engines in `strategy_config` or correct CLAUDE.md**
  (F-24). One of the two is wrong today.

### 13 — A CONCURRENT PROCESS WAS MODIFYING THIS REPO DURING THE SESSION

Recorded because it governs how §0 and F-23 may be read, and because a ledger
that does not say this leaves a future session unable to reconcile the entry
against the commit.

The working tree was clean at `git checkout -b diagnostic/engine-rescore-complete
main`. By the time this entry was written, 30 source files were modified —
including `backend/tools/weekly_review.py` (paging added to `review_gates`),
`backend/allocation/hurdle.py`, `backend/allocation/scoring.py` and
`backend/tests/test_static_analysis.py` (a new guard against unpaged reads).
File mtimes fall inside this session's window (20:59–21:07 IST). **None of it is
mine — this stage touched no source file** — and it was not committed here.

Consequences for what is above:

- **Every code claim in §0 and F-23 was verified against `HEAD`**, not the
  working tree, and re-verified after the concurrent edits were noticed.
  `engine_scorecard.py` is untouched by that work, so F-23's reading of it
  stands. `review_engines`' `fetch_all` at `weekly_review.py:267` is committed in
  `956a38b`, not a working-tree change, so §0's clearance of it stands.
- **`review_gates` (F-23's unpaged list) is being fixed by that other work as
  this is written.** The list is a statement about HEAD at 2026-08-15 21:00 IST
  and will date faster than the rest of this entry. Re-derive it, do not quote
  it.
- **The 8324-row read predates all of it** (20:54 IST) and is a database read in
  any case, so no number in this entry is affected.

---

**Gate: PASS** — both readers checked and reported before use, with the F-19
ledger's own claim about one of them corrected; the complete 8324-row population
paged and verified returned == distinct == server count; per-engine gross/cost/net
R with n, deduplicated, target rates, and the TAKEN-vs-blocked split
direction-matched, with `n<30` declared throughout and `dR` suppressed rather
than printed where a side is too thin; Stage 2's B.2b/B.3a reproduced exactly to
establish which prior findings the truncation does and does not invalidate; B.3's
question answered at scale with both of the brief's input numbers restated in one
unit; no retirement recommended.

---

## 2026-08-15 — F-19 follow-up (the cap, everywhere else) — 14-Aug is scored; the same unpaged idiom was on eleven more readers, the worst losing 91% of the price history every swing outcome is scored against, and a static check now fails the next one

**Branch:** `fix/outcomes-resolve-gap` (continued). `python -m tools.verify` →
**all 501 checks passed across 58 modules**. `python -m tools.health` → **21/21,
fully green** — `learning` now reads `every past session scored (8324 resolved
outcomes on hand)`. `python -m tools.simulate` → SWING LIVE 6 positions, 8
buyable plans, unchanged.

---

### 0 — THE BACKFILL LANDED

Operator refreshed the Kite token and ran the backfill. Confirmed against the
live book, not reported from the command's own output:

```
total rows in intraday_setups   8324
unresolved (all dates)             0
2026-08-14  total 2289   unresolved 0
outcomes.unresolved_days()        []
```

`health` also demonstrates the F-21 fix: its success line reads **8324**, a
number that moves. Before, it was `len()` of a `.limit(1000)` read and would
have said "1000" forever.

### 1 — THE QUESTION THIS STAGE ANSWERS

Fixing six readers does not stop the seventh being written, and it says nothing
about the readers of the *other* nine tables past the cap. So: which unpaged
reads are actually truncating today, and what stops the next one?

Every table counted, not guessed:

```
stock_data_daily 55963 · chartink_raw_data 41496 · allocation_decisions 20873
industry_strength 9382 · intraday_setups 8324 · master_shortlist 7212
signal_log 4563 · sector_strength 2716 · signal_output_daily 2430 · lessons 1114
```

Ten tables over the cap, 173 unpaged reads across them. But most filter to one
day, so the decisive measurement is **rows in the busiest single day**:

```
intraday_setups   2289   <== the only table over the cap in ONE day
stock_data_daily   501
chartink_raw_data  501
master_shortlist   100 · industry_strength 83 · signal_output_daily 82
signal_log          82 · lessons 36 · sector_strength 23
```

That is what makes this tractable. A `.eq("date", …)` read is bounded evidence
everywhere except `intraday_setups`, so ~80 of the 173 sites needed nothing.

### 2 — WHAT WAS ACTUALLY TRUNCATING

Eleven readers, each measured before being touched.

| reader | got | should get | lost |
|---|---|---|---|
| `data_aggregator` prices | 1000 | 10712 | **91%** |
| `performance_tracker` prices | 1000 | 10712 | **91%** |
| `simulate` engine scorecard | 1000 | 8324 | 88% |
| `weekly_review.review_gates` | 1000 | 8324 | 88% |
| `discover_engines` pass A | 1000 | 8324 | 88% |
| `discover_engines` pass B | 1000 | 8324 | 88% |
| `performance_tracker` signal_log | 1000 | 3676 | 73% |
| `lessons` × 5 (AI + alerts + DQM) | 1000 | 1102 | 9% |

**The two 91% losses are the worst thing found in this stage.** Both chunk
`in_("symbol", chunk)` at 250 — a size chosen for PostgREST's IN-list limit,
which has nothing to do with the row cap. 250 symbols across a 60-day window is
10,712 rows and 1000 came back. That is the forward-price history *every swing
outcome is scored against*, and a symbol whose rows fell outside the returned
1000 simply had no forward return at all. Nothing raised; the frame was short.

**`discover_engines` pass B is the one whose failure inverts the tool.** `seen`
is the set of symbols an engine already detected, and every symbol *missing*
from it becomes a "moved but unseen" discovery candidate. Truncated, ~7,300
detections vanish from `seen`, so names the engines *did* fire on get reported
as opportunities they missed. The tool manufactures its own findings, and the
better the engines get the more it invents.

`simulate` is the read-only preview CLAUDE.md tells you to run before changing
anything. Its scorecard was an arbitrary twelfth of the book.

### 3 — THREE HAND-ROLLED PAGERS THAT PAGED WITHOUT SORTING

`hurdle.py` and `scoring.py` (×2) already paged — and none of them called
`.order()`. That is the §4 defect from the previous entry, sitting in
production code, including on the population the allocator's bar is a
percentile of.

Checked live: they came back clean this time (INTRADAY 1163/1163 distinct,
SWING 19710/19710). **That is not a property to rest a live gate on.** The same
idiom demonstrably broke on `intraday_setups` — 8324 rows, 5000 distinct — and
the SWING window is 19,710 rows, twenty pages, taken while the allocator is
flushing new verdicts into the same table, which is exactly when offset paging
drifts. All three now go through `config.fetch_all`, which is a net deletion.

One trap in doing it: `hurdle.py`'s local helper was itself named `fetch_all`,
so importing the shared one shadowed it and the pooled-retry call
`fetch_all(filtered=False)` would have hit the new signature. Renamed to
`_page`; caught by grepping the callers, not by the tests.

### 4 — THE CHECK THAT STOPS THE NEXT ONE

`tests/test_static_analysis.py::test_no_unpaged_read_of_a_table_that_exceeds_
the_row_cap`. A read of a table known to exceed the cap must page, bound itself
(`.limit`/`.single`/`count=`), or carry an explicit `paging-exempt: <why>`
marker. The marker is deliberate friction — it makes an exemption a reviewed
decision rather than an omission nobody noticed.

**Demonstrated failing** by reverting `simulate.py` to the unpaged form:

```
guard FIRED as it should:
  tools/simulate.py:106 reads intraday_setups unpaged and unbounded
```

Two ways it was built so it cannot pass vacuously:

- It asserts `scanned > 40` first. A regex that stops recognising this
  codebase's query style would otherwise report zero violations forever —
  the exact shape of the five dead health checks this project has already
  found.
- Its first draft *did* misreport: after a read was converted to `fetch_all`
  the statement has no `.execute()` of its own, so the regex ran past the end
  and latched onto the next one in the file, re-flagging work already done.
  A check that cries wolf about completed fixes is one that gets muted. Fixed
  by treating a preceding `fetch_all(` as paged.

Four reads are exempt, each carrying its measurement: `.eq("id", …)` on
`signal_log` (one row, ×2), `ml_provider`'s resolved WIN/LOSS population (12
rows), and `engine.py`'s runway requeue (15 rows — one day AND
`cost_verdict=BLOCKED_SHORTABILITY`; the day filter alone would *not* be enough
on that table).

### 5 — THE FAKES, AGAIN (F-22 THREE MORE TIMES)

`fetch_all` calls `.order()`, and 14 test fakes plus `health.py`'s own
`_StubQuery` did not have it. None of them failed loudly:

- The 14 test fakes raised `AttributeError` — 25 of 500 checks red, which is
  the good case.
- `health.py::_StubQuery` raised *inside* `hurdle()`'s own `except`, which
  falls back to the cold-start bar. So `check_allocator_hurdle` went red
  reporting `the bar came back -inf, under the floor 0.0` — a symptom three
  steps downstream of a missing stub method. The check caught the breakage,
  which is it working; the message just pointed at the clamp rather than at
  the read that never happened.

All 15 now have `.order()`, with the reason recorded in each.

### 6 — MEASURED AFTER

```
consumer                       before   after
simulate scorecard               1000    8324
weekly review_engines            1000    8324
weekly review_gates              1000    8324
discover_engines A+B             1000    8324
AI lessons (is_active)           1000    1102
```

The intraday scorecard now reads over the whole book, and it is not a cosmetic
change: SDN 4567 detections at 17%, ORB 1833 at 4%, VWR 1050 at 18%. Four
engines carry a `<- review` flag that the truncated view could not have
supported either way.

### 7 — NOT DONE

- **`swing/ingestion/ingest_sheets - Copy.py` is dead code in the tree.**
  Nothing imports it (checked across `backend/`, `.github/`, `tradeos.cmd`).
  Excluded from the scanner by name rather than annotated, because putting a
  considered paging exemption into dead code implies the code is live. **It
  should be deleted — that is the operator's call, not a check's.**
- **The engine scorecard's numbers are now trustworthy and have not been
  acted on.** Four engines flagged `<- review` on the full population is a
  weekly-review decision, not this stage's.
- **Nothing addresses why 14-Aug produced 2289 detections** against 28-Jul's
  236. Still the open question from the previous entry, and still what pushed
  every reader past the cap in the first place.
- **`outcomes_watch` still has not fired.** Unchanged from the previous entry;
  first proof is its 09:00 IST run.

**Gate: PASS** — backfill confirmed complete against the live book rather than
from the command's output, every remaining exposure measured before being
touched, eleven truncating readers repaired including two losing 91% of the
swing book's price history, three production pagers that sorted on nothing now
sorted, and a static check — demonstrated failing, and guarded against passing
vacuously — that fails the next unpaged read instead of waiting for a session
to stumble on it. `tools.verify` 501/501, `tools.health` 21/21, `tools.simulate`
unchanged.

---

### 8 — CORRECTION TO §2 OF THIS ENTRY (same session, appended not edited)

The count is wrong. §2's table says **eleven** readers and collapses the
`lessons` rows as "x5"; the commit message repeats both. Counted from the diff
rather than from the working notes, `7a3d94b` converts **15 previously-unpaged
reads**, of which **8** are `lessons`:

```
lessons (8)   ai_decision_engine:544 · post_trade_analysis:275, 700, 716,
              1406, 1416 · send_alerts:1039 · data_quality_monitor:641
intraday_setups (4)  simulate:101 · weekly_review:457 (review_gates)
                     discover_engines:144 (pass A) · :274 (pass B)
signal_log (1)       performance_tracker:82
stock_data_daily (2) performance_tracker:102 · data_aggregator:147
```

Plus the 3 hand-rolled pagers replaced (hurdle x1, scoring x2) — 18 `fetch_all`
call sites in total.

Nothing else in §2 changes: every per-reader measurement quoted there was taken
against the live book and is unaffected, and the two 91% losses are still the
two `stock_data_daily` chunked reads. Only the tally was wrong. Recorded here
rather than by editing §2, because this ledger is append-only and a silently
corrected number is worth less than a visibly corrected one — F-17 and F-1 are
both in this file because a figure that would not reproduce was quoted forward.

**Migration 077** — two columns and one switch. No change to the lease row's
shape, deliberately: the startup lock and the runtime lease are the **same row**,
so the two cannot drift into disagreeing about who is running.

```
intraday_broker_log.host   text      socket.gethostname()
intraday_broker_log.pid    integer   os.getpid()
idx_broker_log_host_ts     (host, ts DESC)
system_config.intraday_single_daemon_lock = 'true'   CRITICAL
```

**`lease.claim_startup_lock()`** — read the row, decide, then write a
**compare-and-swap**: the UPDATE carries `.eq("holder", <the holder just read>)`,
so a competitor that claimed in between leaves zero rows matching and this
process refuses. Verified that this is a real signal and not an assumption —
`postgrest.SyncRequestBuilder.update()` defaults to
`returning=ReturnMethod.representation`, so empty `.data` genuinely means zero
rows matched.

**Why not a Postgres advisory lock, which is what R-1 proposed.**
`pg_advisory_lock()` is session-scoped, and this system reaches Postgres only
through PostgREST, which hands out **pooled** connections and returns them after
each request. A session lock taken that way is held by a connection nobody owns
and released at a moment nobody controls — a lock that cannot be trusted to
fail, which is the exact defect shape this repo has now found five times. A
single conditional UPDATE *is* atomic: Postgres takes the row lock and
serialises the writers. That is the advisory lock R-1 wanted, built out of the
one primitive PostgREST actually exposes.

**Failure is CLOSED throughout** — unreadable row, missing table, lost race and
write error all refuse. This is the single most important line in the change:
every other path in `lease.py` fails open to ACTIVE, and that is how the failure
survived.

**`order_manager._log()`** writes `host`/`pid`, and on an unknown-column error
**strips them and retries** rather than losing the row — this table is the money
trail, `_log` swallows its exception to `logger.debug`, and *"PostgREST fails the
WHOLE update on one unknown column"* is already a CLAUDE.md landmine. The retry
is gated on the error text naming a column, so an ordinary timeout is not
retried into a phantom duplicate row.

---

### 2 — THE TESTS, DEMONSTRATED FAILING FIRST

`tests/test_daemon_lock.py`, 14 checks, registered in `tools/verify.py`.

**Required test 1 — a live lease refuses a second daemon.** Broken by reverting
the HELD branch to migration 023's "start anyway":

```
✗  single daemon startup lock  (4/13 failed)
     a live lease refuses a second daemon
       a live lease was claimed anyway: LockResult(granted=True, code='CLAIMED',
       holder='Vipin-14912-605658', detail='lock claimed (DEMO-A: pre-fix
       behaviour — tradeos-vcn-4411-a1b2c3 holds it for 90s and we start anyway)')
     a configured primary may not barge in at startup
     the switch defaults on when the row is missing
     lock verdict is pure over a table of rows
```

**Required test 2 — a genuinely stale lease must not block a restart.** Broken by
refusing whenever a holder is named, which is the plausible over-tight version:

```
✗  single daemon startup lock  (3/13 failed)
     a stale lease does not block a restart
       a legitimate restart was blocked: LockResult(granted=False, code='HELD',
       holder='laptop-9812-ffeedd', detail='DEMO-B: over-tight — ... even though
       its lease lapsed 300s ago')
```

That second one is not symmetry for its own sake. *"A check that cannot PASS is
the same defect wearing a different hat"* — a lock that refuses a restart after a
crash leaves a live book unattended **while looking like a safety feature**,
which is strictly worse than no lock.

**Third demonstration — the call site, not just the function.** A correct guard
proves nothing about its callers; the shorting work found that same gap four
times in one feature. `test_the_daemon_claims_the_lock_before_it_can_act` pins
the *ordering* in `run.main()` — claim, then `lease.acquire`, then
`engine.load_state()`, then ever `engine.cycle(`. Broken by replacing the
refusal's `return` with `pass`:

```
✗  the daemon claims the lock before it can act
     run.main() does not return when the lock is refused
```

**`test_a_configured_primary_may_not_barge_in_at_startup` is the one that pins
the incident itself.** On one row it asserts that `acquire()` **still claims**
(migration 050's mid-run reclaim is unchanged, by design) and that
`claim_startup_lock()` **refuses**. If those two ever agree again, the lock has
been quietly rewired to the policy that caused 10-Aug.

**`tools.verify` 448/448 across 56 modules** (was 434/55). `tools.health` 21 of
22 (the exception is F-12, below, which pre-dates this change). `tools.simulate`
unchanged in shape: swing 6 positions / 8 buyable, intraday 0 takeable.

---

### 3 — THE BEHAVIOUR CHANGE THE OPERATOR MUST KNOW

**There is no hot standby any more.** Migration 023 promised one; with this
switch on, the second daemon prints why and exits. Failover becomes the
broker-side GTT stops — which is what `lease.py`'s own docstring already calls
*the real safety net* — plus a restart once the lease lapses.

**Whoever starts first wins, including against the configured primary.** The
startup lock deliberately does **not** honour `intraday_lease_primary_host`: a
preference for which machine should normally run is not authority to barge in on
one that already is, and honouring it would reproduce 10-Aug exactly. So
**migration 050's intent is partly reversed at startup** and untouched mid-run.
To hand the book to the server while the laptop is running: **stop the laptop
first** — a clean shutdown calls `lease.release()` and frees the row instantly.

**A hard kill costs up to `intraday_lease_ttl_seconds` (120s) before a restart is
allowed.** That is correct rather than unfortunate: the system cannot distinguish
"SIGKILLed" from "still running and renewing", and the refusal message states the
holder and the exact seconds remaining. A same-host/dead-pid fast path would
remove the wait and was deliberately not built — it needs platform-specific
liveness code (`os.kill(pid, 0)` on Windows calls `TerminateProcess` and would
**kill the process it is asking about**), and one change at a time.

---

### 4 — FOUND ALONG THE WAY

**F-11 — the pipeline's exit path places live orders with no lease check at
all.** `position_lifecycle.main(manage=True)` → `manage_open_positions()` →
`place()` at `position_lifecycle.py:1691` sells real shares and never consults
the lease, the lock, or the daemon. It is **not** what happened on 10-Aug (§0),
so it is recorded and not fixed — but a startup lock in `run.py` does not close
it, and "daemon + pipeline concurrently" produces the identical doubling of
`_recent`, `_blocked_account` and the daily caps. The honest place for the check
is `preflight()`, where every path already converges.

**F-12 — the IP allowlist is stale RIGHT NOW, live, before tomorrow's open.**
`tools.health` this session:

```
✗ kite  public IP is 103.197.74.232 but only 103.197.75.33 is recorded as
        allowlisted — order placement will be REJECTED from this address
```

This is Stage 2d-i §6 Cause B recurring for the **fourth** distinct address, and
it is not history — it is the state the daemon will boot into. Every exit
tomorrow would be refused exactly as PPLPHARMA's was on 10-Aug. Unrelated to
this change and untouched by it; it is R-2's case, now with a live instance
behind it rather than a post-mortem.

**F-13 — `renew()`'s primary override remains a mid-run steal path.** A
configured primary still reclaims unconditionally at `lease.py:181`. That is
neutralised *only because* startup is now exclusive — there is no longer a second
daemon for it to steal from. The coupling is worth stating plainly: turning
`intraday_single_daemon_lock` off restores **both** holes at once, not just the
startup one.

**F-14 — a refused start is a log line nobody is watching.** The 21-minute
blindness at the 10-Aug open went unnoticed for exactly this reason and was
given a push notification on 11-Aug. A daemon that exits at 09:00 because it
thinks another is running is the same shape of silence. Not built — the notifier
is constructed after this point in `main()` and moving it is more churn than this
change should carry.

---

### 5 — NOT DONE

- **R-2, the IP allowlist pre-market check** — deferred by instruction. F-12
  says it should be the next stage, not a later one.
- **Migration 077 has NOT been applied.** Migrations run against a live book and
  applying one was not part of this brief. Note the consequence, which is
  deliberate: `cfg_bool("intraday_single_daemon_lock", True)` defaults ON, so
  **the lock is live the moment this code is deployed, before the migration
  runs** — that is the intended direction. Until 077 is applied, `_log()` will
  take one rejected insert per order and retry without `host`/`pid`, logging a
  WARNING naming the migration. No row is lost.
- **F-11 and F-13 were recorded, not fixed.**

---

### 6 — COULD NOT DETERMINE

- **Whether 10-Aug's two daemons were laptop+server or two on one host.** Still
  unrecoverable — that is precisely what the new `host`/`pid` columns exist to
  answer next time, and they cannot answer it retroactively.
- **Whether the lock behaves correctly against real PostgREST concurrency.** The
  compare-and-swap is verified offline against a fake that enforces the filter
  contract, and the client's `returning=representation` default was confirmed by
  inspecting the installed `postgrest` package. It has **not** been exercised
  against the live database with two real processes; that needs the migration
  applied and two daemons started deliberately.

**Gate: PASS** — the question asked before changing anything is answered with
three named mechanisms and the commit that introduced the third; both required
tests were demonstrated failing before they were trusted to pass; `tools.verify`
is 448/448. Two live-money items found along the way (F-11 unguarded pipeline
path, F-12 stale allowlist) are recorded and not silently fixed.

---

## 2026-08-16 — Stage 2f (change, `fetch_all` sort keys) — the paging fix broke the two readers it was fixing: `stock_data_daily` has no `id`, so both price readers raised 42703 on page one. Confirmed live, fixed, and a check that resolves every paged read's sort key back to its table

### 0 — THE CLAIM, INDEPENDENTLY CONFIRMED

Session 4 reported it and did not verify it. It is real. Calling the production
function, live book, 16-Aug:

```
0. schema probe: stock_data_daily
   select('id') RAISED: APIError: {'code': '42703', ...
              'message': 'column stock_data_daily.id does not exist'}
   columns (86): ['close', 'date', 'high', 'low', 'symbol']
   has 'id': False

2. performance_tracker._load_outcomes_for_date_range (PRODUCTION)
   RAISED: APIError: {'code': '42703', 'details': None, 'hint': None,
                      'message': 'column stock_data_daily.id does not exist'}
```

The traceback lands on `performance_tracker.py:108` -> `config.py:331`, which is
`fetch_all`'s first `.range(0, 999)`. **Page one.** Not a truncation, not a
degradation — the read returns nothing and the exception propagates.

`stock_data_daily` has 86 columns and `id` is not one of them. `fetch_all`'s
`order_by` defaults to `"id"`, and the previous stage converted both price
readers without passing one.

### 1 — WHAT IT WOULD HAVE COST, AND WHY IT COST NOTHING

The irony is exact. The previous entry's §2 names these two readers as the
**worst thing found in that stage** — 91% of the forward-price history every
swing outcome is scored against, silently missing. The fix for that loss turned
a 9%-complete read into a 0%-complete one that raises.

It never ran in production. `7a3d94b` landed **Sat 15-Aug 21:12 IST**; the
latest `performance_metrics` row is **2026-08-14**, the Friday — the last
trading day before it. The evening pipeline has not run the brain since the
break, and today is Sunday. The first run that would have hit this is Monday
17-Aug. Caught inside the weekend, ~14 hours after it was written, before it
could cost a single metric.

Worth stating plainly because it is luck, not design: nothing in the previous
stage's own verification would have caught it. `tools.verify` is offline by
construction, so no test in it can see a live schema; `tools.health` does not
exercise the brain's readers; and the fakes the tests use answer `.order()` for
any column name at all.

### 2 — THE CAPABILITY WAS TESTED. THE CALLERS WERE NOT.

This is the sharpest part, and it is a landmine this project already has
written down in another form.

`tests/test_outcome_resolution_gap.py::test_fetch_all_lets_a_table_without_an_id_name_its_own_key`
exists. It passes. Its docstring reads *"`signal_output_daily` has no `id`
column. A hardcoded sort key would make this function unusable there, or worse,
silently wrong."* Somebody thought about tables without an `id`, built the
parameter for it, and proved the parameter works.

`config.fetch_all`'s docstring says the same thing again, in capitals:
`order_by` MUST NAME A UNIQUE COLUMN — and names `signal_output_daily` as the
motivating case.

Both were true and neither helped, because **nothing checked the call sites**.
Across the whole backend, exactly one `order_by=` was ever passed, and it was in
that test. Every one of the 23 production paged reads took the default.

That is CLAUDE.md's *"a direction-aware function's correctness proves nothing
about its callers"* recurring on a different parameter. The generalisation is
now two-for-two: when a function grows a parameter with a backwards-compatible
default, the default is where the defect hides, and testing the parameter is
not testing the callers.

### 3 — THE OTHER SITES: 23 READS, 5 TABLES, 2 BROKEN

Every production `fetch_all` call resolved to the table it reads and probed live
with `.order("id").range(0,0)` — the exact request `fetch_all` issues for page
one:

```
table                  .order(id) works?    sites
lessons                YES                  8
allocation_decisions   YES                  1
intraday_setups        YES                  11
signal_log             YES                  1
stock_data_daily       NO (42703)           2   <== data_aggregator:151
                                                    performance_tracker:108
```

**2 of 23 broken, both on `stock_data_daily`, both fixed. The other 21 were
already correct** — `id` exists on all four remaining tables. Fixed only the
broken ones, as instructed.

**Existence is only half the claim, so uniqueness was measured too.** A sort key
that exists but is not unique is the strictly worse failure: it pages with no
error at all and lets rows repeat and vanish across page boundaries — the
8324-rows/5000-distinct shape from the previous entry. Each key paged over the
WHOLE table, returned count checked against both the server-side count and its
own distinct count:

```
table                    server    paged  distinct  key
lessons                    1114     1114      1114  id                OK
allocation_decisions      20873    20873     20873  id                OK
intraday_setups            8324     8324      8324  id                OK
signal_log                 4563     4563      4563  id                OK
stock_data_daily          55963    55963     55963  symbol,date       OK
```

`(symbol, date)` on the full 55,963-row table across 56 pages: 55,963 returned,
55,963 distinct, exactly the server count. `postgrest-py` sends
`order_by="symbol,date"` as `order=symbol,date`, which PostgREST reads as two
sort terms — confirmed on the wire (`{'select': 'date,symbol', 'order':
'symbol,date'}`) rather than assumed.

### 4 — MEASURED AFTER

Both production readers, live, same calls that raised in §0:

```
performance_tracker._load_outcomes_for_date_range
   OK -> 3676 rows x 120 cols
   outcome_win:     3676/3676 populated
   max_fwd_return:  2889/3676 populated

data_aggregator._compute_forward_returns   (600 signals, 140 symbols)
   Outcomes: 600 evaluated, 285 wins (47.5%), coverage=100%
   ret_fwd_5d  600/600 · ret_fwd_10d 600/600 · ret_fwd_20d 593/600
```

`_compute_forward_returns` needed signals older than its own eval cutoff
(`max_horizon * 1.5` = 30 days) to reach the fixed line at all — the first
attempt returned early on *"No signals old enough for forward return
evaluation"* and proved nothing. Re-run against signals from 19-Jun–02-Jul.
**Coverage 100%** is the number the previous entry could not produce; it was 9%.

### 5 — THE CHECK, DEMONSTRATED FAILING FIRST

`tests/test_static_analysis.py::test_fetch_all_sorts_on_a_key_that_exists_on_the_table_it_reads`.
Written before the fix, run against the broken tree:

```
✗  static analysis  (1/3 failed)
     fetch_all sorts on a key that exists on the table it reads
       2 paged read(s) sort on a column that is not that table's verified
       unique key...
         swing/brain/data_aggregator.py:151 pages stock_data_daily sorted on
           'id', but that table's verified unique key is 'symbol,date' — `id`
           does not exist there and PostgREST raises 42703 on page one
         swing/brain/performance_tracker.py:108 pages stock_data_daily ...
```

Three deliberate choices in it:

**`ast`, not a regex.** The sibling paging check is a regex and learned the hard
way that a text window wide enough to catch a statement latches onto the next
one. It also cannot follow a `fetch_all(build, ...)` where `build` is a named
function — and two sites pass one.

**One extra hop of resolution, because the first draft silently skipped a site.**
`hurdle.py:462` pages `allocation_decisions` through a `build()` that returns
`base_query()`; the `.table()` call is a function away. A single-level walk
resolved nothing there and **skipped the site rather than checking it** — 22
sites seen instead of 23. An unresolvable site is invisible, not loud, which is
the failure mode of a check that watches less than it claims. Now chases local
helpers to depth 3.

**The map is keyed by table, not by the broken ones.** An allowlist of
known-bad tables goes stale in silence the day someone points `fetch_all` at a
sixth table. Requiring *every* table to carry a measured key means a new one
fails here until somebody probes it.

Guarded against passing vacuously: it asserts `scanned >= 12` before judging
anything. If `fetch_all` is ever renamed, re-exported or wrapped, the walk would
match zero calls and pass forever while watching an empty set — the shape of the
five dead health checks this project has already found. 23 resolve today.

`tools.verify` **516/516 across 59 modules**, up from 515 on `main` (baseline
measured by stashing, not assumed). `tools.health` unchanged at 1 problem, §6.

### 6 — FOUND ALONG THE WAY

**F-23 — `docs/FINDINGS.md` has unresolved merge-conflict markers committed to
`main`.** Six of them, from `ed77595 "merging everything"` (16-Aug 11:07):

```
```

Two whole ledger entries are interleaved inside conflict blocks. The content
appears to be present on both sides, so nothing is obviously lost, but this is
the append-only ledger CLAUDE.md tells every session to read first, and it
currently cannot be read straight through. **Recorded, not fixed** — out of this
stage's scope, and resolving a conflict inside an append-only ledger is a
judgement call about which side is authoritative that belongs to the operator.

**F-24 — `tools.health` is red on `kite` right now.**

```
✗  kite      Kite call failed: 'NoneType' object has no attribute 'profile'
```

The broker object is `None` — no session, distinct from F-12's stale IP
allowlist. **Confirmed pre-existing**: identical failure on stashed `main`, so
it is not this change. It is Sunday and the token has almost certainly expired;
it needs a fresh login before Monday's open. Unrelated to paging, untouched.

### 7 — NOT DONE

- **No live schema probe was added to `tools.health`.** The map in §5 is a
  static claim — "the code says `stock_data_daily` has no `id`" — and CLAUDE.md
  already records that *"the code mentions this column" and "this column exists"
  are different claims*, which is why `open_positions.direction` is a live probe
  rather than a grep. The same argument applies here and the same remedy would
  be a `check_fetch_all_sort_keys()` in `health.py` that probes each key against
  the running database. Deliberately out of this stage's narrow scope; it is the
  natural next thing.
- **The 4 non-`stock_data_daily` tables were not changed**, per instruction —
  they were verified correct, not left unexamined.
- **F-23 and F-24 recorded, not fixed.**

### 8 — COULD NOT DETERMINE

- **Whether any earlier pipeline run consumed a partially-broken read.** The
  break window is bounded by commit time (Sat 21:12) and the last
  `performance_metrics` row (Fri 14-Aug), which is strong evidence it never ran
  — but the pipeline writes no "brain step attempted and crashed" record, so the
  absence of a 15-Aug row is consistent with both "never ran" and "ran and
  died". The weekend makes the former overwhelmingly likely; it is not proven.

**Gate: PASS** — the reported claim was verified by calling the production
function rather than taken on report, and it was real: 42703 on page one, not a
degraded read. Both readers fixed with a key measured unique over the full
55,963-row table; all 23 production paged reads audited against their own
table's schema with the count reported (2 broken, 21 already correct, only the
broken ones touched); the new check demonstrated failing before it was trusted
to pass and guarded against passing vacuously. Two items found along the way
(F-23 conflict markers in this ledger, F-24 live `kite` failure) are recorded
and not silently fixed.

---
