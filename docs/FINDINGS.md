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
