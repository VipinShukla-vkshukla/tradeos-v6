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
