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

## 2026-08-15 — F-23 reconciliation + the price re-score — the priority reader was already fixed, three survivors were not, the guard left behind could not see either of the two families it was built for, and the previous session's headline fix does not run at all

**Branch:** `diagnostic/rescore-complete-prices`, off `main` at `03b3529` (all
session work merged to main first; `fix/single-daemon-lease` deliberately NOT
merged — see §9). `python -m tools.verify` → **all 502 checks passed across 58
modules**. `python -m tools.health` → **22 checks, 1 problem: `quote_parity`,
which is this entry's F-27 and is the check finally telling the truth**.
`python -m tools.simulate` → SWING LIVE 6 positions, 8 buyable plans, unchanged.

READ-ONLY on outcome DATA: nothing re-resolved, nothing written back. Every
number below is computed and printed.

---

### 0 — F-23, RECONCILED AGAINST HEAD

F-23 listed its readers under a heading saying **NINE**; the code block beneath
it holds **TEN** entries. The tenth (`engine.py:959`) is annotated "already
flagged by F-19", so the count is either an off-by-one or an unstated exclusion
— the same species of tally error §8 of the previous entry corrected about
itself. Recorded, not guessed at.

Each of the ten, checked against `HEAD` rather than against the prose:

```
FIXED BY SESSION 2 (6)
  allocation/scoring.py:166   intraday_priors       <== F-23's OWN PRIORITY
  allocation/scoring.py:1077  regime-segmented priors
  tools/weekly_review.py:457  review_gates
  tools/discover_engines.py:144  pass A
  tools/discover_engines.py:274  pass B
  tools/simulate.py:101       engine scorecard

EXEMPT, WITH A MEASUREMENT (1)
  intraday/engine.py:959      runway requeue — `paging-exempt:`, 15 rows

STILL OPEN AT HEAD (3)  — all three fixed by this entry
  tools/engine_scorecard.py:55   _fetch
  tools/allocator_replay.py:74   _fetch_setups
  tools/control_room.py:475      _load_setups
```

**The brief asked to prioritise `allocation/scoring.py:166` and fix it if still
open. It is not still open.** Session 2 converted it to `config.fetch_all` and
left the reasoning in place above the call. The thing F-23 called "the serious
one" — the prior that prices every candidate the allocator sees — was repaired
before this session started. Stating that plainly matters more than finding
something to do to it.

### 1 — THE THREE SURVIVORS, MEASURED BEFORE BEING TOUCHED

Each reproduced exactly as it stood at HEAD, three trials, against
`count=exact`:

| reader | true rows | unsorted trials (rows / distinct) | lost |
|---|---|---|---|
| `engine_scorecard._fetch` | 8324 | 8324/8324 · 8324/8324 · 8324/8324 | — |
| `allocator_replay._fetch_setups` | 8324 | **8324/5000** · 8324/8324 · 8324/8324 | **3324** |
| `control_room._load_setups` | 7864 | **7864/5000** · 7864/7864 · 7864/7864 | **2864** |

**Two of the three broke live, on the first trial.** The previous entry could
only say of this idiom that it "came back clean this time"; here it did not.
The count is right in both the good and the bad trial, which is the entire
defect — 3,324 rows arrived twice and 3,324 never arrived, and nothing about
the shape of the result says so.

`control_room`'s companion read of `closed_positions` carries `.limit(1000)`
and is bounded in fact: 148 rows over the 365-day filter. Checked, not assumed.

### 2 — F-23 CENSUSED ONE TABLE. THE IDIOM WAS ON SIX.

F-23 enumerated its readers with `git grep 'table("intraday_setups")'`. That is
a census of one table, and the finding's scope silently became the scope of its
grep. Scanning for the *idiom* instead — a `.range()` pager with no `.order()`,
any table — found **21 sites**, of which ten read a table over the cap:

```
PRODUCTION
  allocation/scoring.py:528   swing_priors()      <== THE LIVE SWING BOOK'S PRIOR
  allocation/scoring.py:847   tier weight
  allocation/scoring.py:955   rr fallback
  swing/signals/engines_stage8.py:195  PEAD results-day bars (stock_data_daily)
  swing/signals/outcomes.py:272        status report
  allocation/outcomes.py:93 · allocation/swing_hold_days.py:71  (closed_positions)
TOOLS
  hurdle_population_audit:67 · swing_family_maturity_audit:59
  weekly_review:626 · :714 · :796 · benchmark:82 · exit_ladder_replay:118
  expectancy_ledger:102 · exit_audit:78 · taken_reconciliation:71
  quote_parity:229 · backup:84 · :124 · health:684
```

**`swing_priors()` is the exact counterpart of the reader F-23 called "the
serious one", on the LIVE book, and F-23 could not see it because it reads a
different table.** Measured: its population is 1,681 rows (two pages), clean on
three trials — lower exposure than the intraday side, not zero.

**`taken_reconciliation._rows` pages `intraday_setups` itself** — 8,324 rows,
the very table F-23 censused — and was invisible to that census because the
table name is a *parameter*, `sb.table(table)`. So was
`engine_scorecard._fetch`, one of the two readers that measured a live loss.

**`intraday_quote_parity` is 178,545 rows** — by a wide margin the largest table
in this schema, absent from the previous session's `_LARGE_TABLES` list, and
paged unsorted across 179 pages by `quote_parity.report()`. A "known large
tables" list is only as good as the census behind it.

All 21 now go through `config.fetch_all`. Four kept their hand-rolled form
behind an explicit `sort-exempt:` marker with its measurement (`v_storage_usage`
is a 56-row catalogue view).

**`signal_output_daily` has no `id` column**, so eight of these conversions had
to pass `order_by="symbol,date"` — verified unique, 2430 distinct of 2430.
`fetch_all`'s default would have raised on every one of them. That detail is
not incidental; it is §6.

### 3 — THE GUARD SESSION 2 LEFT COULD NOT SEE EITHER FAMILY

`test_no_unpaged_read_of_a_table_that_exceeds_the_row_cap` is a good check for
the defect it was built for, and it has two structural blind spots:

- **`.range(` is treated as evidence of boundedness** (`if re.search(r'\.(limit|
  range|single|maybe_single)\(...')`). Correct for truncation; it means the
  entire *unsorted-paging* family — every reader in §1 and §2 — is invisible to
  it. F-23 named that family and the guard written alongside F-23 does not
  watch it.
- **It matches `.table("literal")` only**, so the two variable-name readers
  above are not merely unflagged, they are not even counted in `scanned`.

`tests/test_static_analysis.py::test_no_range_pager_without_a_sort_key` closes
both. **Demonstrated failing** by reverting `quote_parity.py`:

```
guard FIRED as it should:
  tools/quote_parity.py:236 pages intraday_quote_parity with no sort key
```

**Its anti-vacuous guard is a positive control, not a row count, and that is a
correction to the pattern this project has been using.** The sibling check
asserts `scanned > 40`, which is sound because reads of large tables are
plentiful and stay plentiful. That reasoning does not transfer: *this* check's
own fixes DELETE `.range()` sites, so the population it counts shrinks every
time it succeeds. The scan went **19 → 10** in this session. A floor set from
the "before" number fails on the "after" — and the obvious repair, lowering the
floor until it passes, is how a threshold becomes decoration. It asserts
instead that the detector still flags a known-bad sample and still passes a
known-good one, neither of which erodes.

### 4 — F-27: `health.check_quote_parity` WAS GREEN ON 0.6% OF ITS EVIDENCE

Adding `intraday_quote_parity` to the large-table list immediately surfaced an
unpaged read the previous census never had a reason to look at:
`tools/health.py:895`. Its 5-day window is **167,025 rows**. Unpaged, PostgREST
returned 1,000.

Both verdicts computed from the same cutoff, same function, 15-Aug-2026:

```
truncated (1000 rows)   True   "400 day_high/day_low comparisons, all clean"
complete  (100,215)     False  "176 of 66810 day_high/day_low comparisons behind"
```

**The check has been reporting RANGE clean while RANGE was regressed, and
`intraday_quote_mode_range` is ON and trusted on that all-clear.** This is the
sixth green-while-broken check found in this project and it fits the house
description exactly: it *could* fail, but never on the evidence it was handed.

Fixed by filtering to the three fields the verdicts actually read — the other
~66,810 rows were fetched and discarded in Python — and paging. Costs ~8s. A
health check that takes eight seconds and tells the truth is worth more than an
instant one that says what you hoped.

`health` now reports **1 PROBLEM: quote_parity**. That is not a regression
introduced here; it is a real fault that was already true this morning and is
now visible. **It is unexplained and worth the operator's attention.**

### 5 — F-28: SESSION 2's HEADLINE FIX DOES NOT RUN

The previous entry's worst finding was the two 91% price losses —
`data_aggregator` and `performance_tracker`, "the forward-price history every
swing outcome is scored against". Both were converted to `fetch_all(...)` with
no `order_by`.

**`stock_data_daily` has no `id` column.** `fetch_all` defaults to `id`.
PostgREST answers a sort on a missing column with 42703 and fails the WHOLE
query.

Verified by calling the production function directly, not by reading it:

```
>>> _load_outcomes_for_date_range(sb, date(2026,5,1), date(2026,8,10), 5)
RAISED: APIError {'code': '42703', 'message': 'column stock_data_daily.id does not exist'}
```

So the swing brain's forward-return scorer went from silently reading 9% of the
prices to reading none and raising. Both call sites fixed with
`order_by="symbol,date"` (unique: 16,489 of 16,489).

Audited against the live schema, every `fetch_all` site in the backend: **2 of
45 wrong, 43 fine** — the ratio that survives review by eye.

`health.check_sort_keys` is new and is a live schema probe, because "the code
names this column" and "this column exists" are different claims and only one is
answerable offline. `check_selects` cannot cover this: it validates columns
named in `.select()`, and a sort key is a Python argument supplied by a default
the call site never writes down. **Demonstrated failing**, then passing:

```
REVERTED -> FAIL: performance_tracker.py:114 sorts stock_data_daily on ['id'] which it does not have
RESTORED -> PASS: all 45 fetch_all reads sort on a column that exists
```

### 6 — THE RE-SCORE: THE PREMISE IS WRONG, AND THE REAL ANSWER IS NARROWER

The brief states the truncation means "every swing R figure in this ledger may
be wrong, including the live book's". That is a claim about a DEPENDENCY, and it
was tested rather than assumed. There are two different swing "R" populations:

**A — `closed_positions.r_multiple`.** From `position_lifecycle.py:880`:

```python
risk   = D.risk_per_share(entry, stop0, d)
r_mult = round((realized_pnl / total_qty) / risk, 3)
```

`realized_pnl` is built from the ACTUAL FILL prices; `risk` from
`planned_stop`. **No `stock_data_daily` anywhere in it.** And neither
`tools/expectancy_ledger.py` nor `tools/unit_economics.py` — the two tools every
swing R figure in this ledger is quoted from — reads that table at all (0
occurrences; they touch `closed_positions`, `signal_output_daily`,
`system_config`).

⇒ **No closed swing position's R changes. Not one, gross or net. The 91%
truncation cannot have touched any swing R figure in this ledger, including the
live book's.**

**B — plan-level `outcome_*`,** scored FROM prices by `performance_tracker`.
That is the population the truncated readers actually feed, and there the effect
is not bias but annihilation. Same function, same window
(2026-05-01..08-10, horizon 5), price fetch switched:

```
TRUNCATED   2,000 price rows    0 of 3896 signals scored   (0.0%)
COMPLETE   21,656 price rows  3818 of 3896 signals scored  (98.0%)
                              mean fwd +0.152%  hit 21.5%  loss 10.4%
```

Note this is the path that has been RAISING since §5's defect landed, so it has
most recently produced neither number.

The swing outcome resolver that actually writes `outcome_category` —
`swing/signals/outcomes.py:82` — was never in this blast radius: it pages, and
it sorts. See §9 for the one thing wrong with it.

**Per trade.** 82 closed SWING positions; 12 carry the `planned_stop_at_entry`
needed to express R. "recorded" is the fill-derived figure; "re-scored" walks
the complete price frame to the close on the recorded exit date — a DIFFERENT
measurement, not a correction of the first.

```
symbol       exit         recorded   truncated   complete
PPLPHARMA    2026-07-30      2.095       n/a        1.956
GABRIEL      2026-07-31      0.192       n/a        0.330
BHEL         2026-08-03      0.081       n/a        0.160
TRAVELFOOD   2026-08-06      0.267       n/a       -0.293
KIMS         2026-08-06      0.394       n/a        0.278
ETERNAL      2026-08-10      0.289       n/a        0.189
CIPLA        2026-08-10      0.089       n/a       -0.028
VIJAYA       2026-08-12      0.452       n/a        1.076
MANAPPURAM   2026-08-14     -0.750       n/a       -0.659
PPLPHARMA    2026-08-14      0.863       n/a        0.694
AIIL         2026-08-14      0.377       n/a        0.238
AUBANK       2026-08-14      0.366       n/a        0.409
```

**The truncated frame priced ZERO of the twelve.** Under truncation these trades
have no re-scored R at all — again absent, not wrong.

**Aggregate.**

```
recorded (fills)        n=12   gross R +0.3929   hit 91.7%
re-scored COMPLETE      n=12   gross R +0.3625   hit 75.0%
re-scored TRUNCATED     n=0    —
net R (recorded)        n=12   net   R +0.2568   hit 66.7%
```

`tools.expectancy_ledger`, authoritative, agreeing to the third decimal:

```
· SWING / CNC (n=12)
  gross R          n=12  mean +0.393 ±0.188  median +0.328
  friction, in R   n=12  mean +0.171 ±0.055  median +0.113
  NET R  <- number n=12  mean +0.222 ±0.197  median +0.151
  SWING CNC by rupees: n=82  net-win 34/82
```

### 7 — LEDGER FINDINGS WHOSE NUMBERS CHANGE, AND WHY

**None of them change because of the truncation.** They change because the book
grew from 8 R-computable swing trades to 12.

| finding | was | is now |
|---|---|---|
| Stage 1 "+0.482 gross R (n=8)" | +0.482, n=8 | **+0.393, n=12** |
| line 297 "SWING / CNC (n=8) NET R mean +0.278" | +0.278, n=8 | **+0.222, n=12** |
| Stage 2b / C.4 "SWING 39.7% (n=78)" | 39.7%, n=78 | **41.5% (34/82)** |
| the "zero losers" caveat (line ~1467) | 8 of 8 gross winners | **VOID — 11 of 12** |

**The "zero losers" caveat is the one that matters.** The ledger has repeatedly
and correctly refused to call +0.482R an estimate of swing edge because the
subsample contained no losing trade. It now contains one — MANAPPURAM, −0.750,
14-Aug. The caveat as written is no longer true; the *caution* it encodes still
is, at n=12 with one loser.

**`tools/unit_economics.py` hardcodes `SWING 39.7% (n=78)` in a log line.** It
prints a measured-sounding figure that is now a literal. Not fixed here — it is
a one-line change in a tool this ledger quotes, and it should be changed
deliberately rather than as a side effect of a paging session.

### 8 — DISCOVER_ENGINES: BOTH HYPOTHESES SURVIVE

Re-run at `--days 30` to match Stage 1's window, because the tool now defaults
to 14 and comparing across windows would measure the window, not the fix.

```
                        STAGE 1 (truncated)        NOW (complete)
pass A population       1000 -> 236 -> 99          8324 -> 1102 -> 330
taken baseline          21% (n=53)                 19% (n=121)
refused slices beating  none                       BLOCKED_EVENT/VWR 33% of 6
gap up   > 1%           lift 1.6x (32% of 95)      lift 1.8x (35% of 89)
                        24 missed, avg 6.06%       23 missed, avg 5.10%
gap down > 1%           lift 1.9x (39% of 31)      lift 1.9x (38% of 32)
                        11 missed, avg 4.86%        7 missed, avg 4.06%
```

**H1 survives and strengthens: 1.6x → 1.8x. H2 survives unchanged at 1.9x.
Neither is void.**

**And the reason is worth recording, because it corrects the previous entry's
reasoning about its own fix.** Session 2 argued pass B's truncation "inverts the
tool — the better the engines get the more it invents". Directionally right;
quantitatively small here. The LIFT is computed from `stock_data_daily` bars and
never touched `intraday_setups` at all — only the MISSED counts read `seen`. So
truncation could never have manufactured a lift, only inflated a miss count, and
it did: gap-down 11 → 7 (36% overstated), gap-up 24 → 23 (4%). The hypotheses
were never the fragile part.

**What did change is Stage 1's pass A conclusion.** It read "no refused slice
beats the taken baseline — the gates are declining worse setups than they
allow, which is their job", and was cited as "independent evidence that the
gates are not inverted". On the complete population one slice does beat it
(`BLOCKED_EVENT`/VWR, 33% of 6; at 14d also `BLOCKED_STRUCTURE`/VCE, 27% of 11).
At n=6 and n=11 this is not evidence of anything and must not be acted on — but
the sentence as written is no longer supported.

Two `ENGINE_CANDIDATE` rows written (id 192, 193), both `PENDING`. Nothing
auto-applied.

### 9 — `outcomes_watch`: INSTALLED, CORRECTLY WIRED, HAS NOT FIRED

**Installed — verified against the remote, not the working tree.** Only the
default branch is ever scheduled, so the working tree proves nothing:

```
origin/main blob .github/workflows/brain_scheduler.yml
  cron: '30 3 * * *'   # 09:00 IST, daily
  job outcomes_watch:  if github.event.schedule == '30 3 * * *'   <- exact match
  workflow_dispatch options include outcomes_watch  <- manually triggerable
  runs: python -m intraday.outcomes --check-and-alert
```

The gate string and the cron string match exactly. In a multi-cron workflow a
mismatch there is the standard way a job is scheduled and never runs; it is not
the failure here.

The entrypoint exists and was executed:
`outcomes: every past session is scored`.

**Has not fired, and could not have.** The commit that added it, `956a38b`,
landed **2026-08-15 20:38:56 IST** — about eleven and a half hours after today's
09:00 window. First possible run is **16-Aug 09:00 IST**.

**Not verified from run history.** There is no `gh` CLI and no GitHub token in
this environment, so the Actions log was not read. "Has never fired" is inferred
from the commit timestamp — which is decisive for today and is not the same
claim as having seen an empty run list. First proof remains its 09:00 IST run.

### 10 — RECOMMENDS

**No retirements**, per the brief and independently on the evidence: the two
pass-A slices that beat baseline sit at n=6 and n=11, and F-25's stop-floor
finding still means several engines' R statistics describe setups the cost model
would refuse today.

### 11 — NOT DONE

- **`swing/signals/outcomes.py:82` pages on `.order("date")` — a NON-UNIQUE
  key.** Ties within a date are ordered arbitrarily across requests, so the same
  drift is available to it; it is a weaker version of the §1 defect, not an
  instance of safety. Measured over a 250-symbol / 14-page window: 13,451 of
  13,451 distinct on two trials, both orderings. **Not fixed — this is the
  outcome resolver and the brief is READ-ONLY on outcomes.** The new static
  check does NOT catch it: it tests for the presence of `.order(`, not for the
  uniqueness of what is ordered on. That is the next member of this family.
- **The `quote_parity` RANGE regression (§4) is real and unexplained.** 176 of
  66,810 day_high/day_low comparisons are behind, against a clean 07-Aug
  baseline. `intraday_quote_mode_range` is ON.
- **`tools/unit_economics.py`'s hardcoded `SWING 39.7% (n=78)`** (§7).
- **`local main is 3 commits ahead of origin/main` and unpushed**, so none of
  this session's or the previous session's fixes are on the branch GitHub
  actually schedules from. `outcomes_watch` itself IS there; the paging fixes
  the weekly jobs depend on are not.
- **`fix/single-daemon-lease` is still unmerged** — one commit, `e5738a7`,
  carrying migration 077 and `order_manager` changes. Promoting a live-order
  path and a DB migration is a different decision from consolidating diagnostic
  work, and it conflicts in `FINDINGS.md`. Left for the operator.
- **Nothing addresses why 14-Aug produced 2289 detections** against 28-Jul's
  236. Unchanged and still the largest single influence on every pooled number
  in this ledger.

**Gate: PASS** — F-23 reconciled against HEAD line by line with its own count
corrected, the three survivors measured breaking live before being repaired, the
idiom traced past F-23's one-table census to twenty-one sites on six tables
including the live swing prior, a guard added for the family the previous guard
could not see and given a positive control instead of a threshold its own
successes erode, a health check found green on 0.6% of its evidence and now
failing truthfully, the previous session's headline fix found non-functional and
repaired with a schema probe that would have caught it, the re-score's premise
tested and shown not to reach closed-position R, and both discovery hypotheses
confirmed to survive. `tools.verify` 502/502, `tools.health` 22 checks / 1 real
problem, `tools.simulate` unchanged.

---
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

## 2026-08-16 — Replay harness (build + verify) — the design's 6/3/3 split was void and is rebuilt on 75 measured days; the harness reproduces the OUTCOME rule exactly but MISSES the detection bar at 79.7% vs 85%, so it is NOT trusted and no replay was run

**Ran:**

```bash
git checkout -b feat/replay-harness
# depth + window census (scratch scripts, read-only, paged on (symbol,date))
python -m tools.replay.independence
python -m tools.verify --module replay_harness
python -m tools.verify
python -m tools.replay.verify_known_day --date 2026-08-14
```

### 1 — DEPTH MEASURED, AND THE DESIGN WAS WRONG ABOUT ITS CENTRAL PARAMETER

`stock_data_daily`: **55,963 rows, 108 distinct dates, 2026-03-06 .. 2026-08-14.**
The design's `IN-SAMPLE 2025-07-01 .. 2025-12-31` window **does not exist**.

```
                                                  trading   fully
window          from          to                    days    usable
--------------------------------------------------------------------------
IN-SAMPLE       2026-03-06 .. 2026-04-30              36        30
VALIDATION      2026-05-01 .. 2026-05-31              18        18
HOLDOUT         2026-06-01 .. 2026-06-30              21        21
--------------------------------------------------------------------------
clean total                                           75        69
CONTAMINATED    2026-07-01 .. 2026-08-14              33        33
```

In-sample loses 6 days whose `value_cr` is null on **every** row (2026-03-06,
03-09, 03-10, 03-11, 03-12, 04-28). `value_cr` is 45% of `build_universe`'s rank
and one of its hard filters, so those dates cannot be reconstructed at all.

**2026-04-29 carries 2,476 rows** against a 499-502 norm; 1,976 are shells with
`value_cr` but a null `close`. The harness drops null-`close` rows, restoring a
normal 500-name universe rather than losing the day.

REPLAY_DESIGN was revised and committed as `fa9b9fe` **before** any code was
written, per instruction. §8.1 states plainly, before the run, that a 21-day
holdout is thin and that the SWING arm may be uninformative for a structural
reason rather than for want of rows: Q1's paired estimator is exactly zero for
any plan that never reaches 1.905R, so `n_effective` is the count of plans
reaching the lower target — and on the live book that was **0 of 11**, peak
1.34R.

### 2 — TWO CORRECTIONS THE MEASUREMENT FORCED

**F-25 — `stock_data_daily` is NOT the full NSE EQ cross-section.** The design
asserted it was, citing the `SERIES == 'EQ'` filter at `ingest_bhavcopy.py:184`.
That filter governs the bhavcopy dataframe, not what is stored. The unfiltered
set goes to `raw_prices`; `stock_data_daily` receives a **Chartink-screened
subset** (`ingest_bhavcopy.py:624-633`) — measured **499-502 symbols per date
against 2,463 in `raw_prices`** on the same date.

It *is* point-in-time (`.eq("date", trade_date)`, written by that evening's own
run) and 32 of March's names are gone by August, so it is not one static list
applied backwards. But it is **93% static** — 465 symbols present on all 108
dates, median day-over-day churn **0** — and it bounds the replay to names
Chartink surfaced. That is a ceiling on scope, not a bias in the estimate: the
live system trades the same filtered universe.

**`raw_prices` is not an escape hatch** — checked: it starts **2026-04-16**,
*shallower* than `stock_data_daily`, and carries no indicator columns. So
`stock_data_daily` is a hard floor for BOTH arms. Kite bars go back years; the
screener inputs do not. Backfill is out of scope.

**F-26 — `fetch_chartink_universe` is an unpaged read** (`ingest_bhavcopy.py:393`)
capped at 1000 rows by PostgREST, and its return value decides which symbols
reach `stock_data_daily` **at all**. ~500 today so it is not truncating, but it
is the F-19 idiom sitting on production ingest. **Recorded, not fixed.**

### 3 — WHAT WAS BUILT

`backend/tools/replay/` — a new tool. **No live engine, gate, exit rule, config
key or migration was touched.**

```
independence.py     the static forbidden-reader scan
universe.py         point-in-time COPY of scanner.build_universe
swing_inputs.py     point-in-time COPY of screen_stocks.load_data
bars.py             Kite fetch, disk cache, coverage manifest
contexts.py         SymbolContext assembly + the lookahead assertion
detect.py           the evaluate_all loop, reproduced
ladder.py           imports evaluate_exit AND evaluate_intraday_exit
outcomes_port.py    the resolver loop, ported
verify_known_day.py the ONLY module permitted to read the live record
```

Imported, never reimplemented: the nine engines, `_invalidation_is_reachable`,
`family_of`, `evaluate_exit`, `evaluate_intraday_exit`, `intraday.direction`,
`cost_model.round_trip`. `evaluate_all` is **reproduced, not called** — it reads
live `system_config` through `enabled_engines()`, and an engine retired today
would otherwise show zero detections across all of history, which is the exact
inversion of what the tool is for.

Copied rather than imported, each for a stated reason: `build_universe` resolves
its own date internally and has no injection point; `load_data` is not
point-in-time on five of nine reads; the outcome loop is welded to Supabase I/O.
**Production untouched in all three cases.**

15 checks in `tests/test_replay_harness.py`, registered in `tools/verify.py`.

### 4 — THE INDEPENDENCE CHECK, DEMONSTRATED FAILING FIRST

It failed **twice on real input before it passed**, which is the only reason to
believe it:

```
# 1. on the package's own prose, unprompted:
  [X] __init__.py:12 references 'engine_scorecard'
  [X] detect.py:27  references 'intraday_setups'
  [X] detect.py:95  references 'intraday_setups'
FAIL: 3 forbidden reference(s), 0 exemption breach(es)
```

Reworded rather than weakening the grep — a literal grep is the only kind that
cannot be talked out of a match. Then a deliberate injection of a real read:

```
# 2. appended to contexts.py:
  [X] contexts.py:157 references 'intraday_setups' - the live detection record
      return sb.table("intraday_setups").select("*").eq("trade_date", day)...
FAIL: 1 forbidden reference(s)          EXIT=1
# removed:
OK: the harness reads no forbidden table or module    EXIT=0
```

The exemption is guarded rather than trusted: `independence.py` is exempt only
because it names the tables as data, and `check_exempt_files_are_inert()`
asserts that file can reach no database at all.
`check_scan_is_not_vacuous()` asserts the walk saw at least 4 files, so an empty
scan cannot report clean.

### 5 — VERIFICATION AGAINST 2026-08-14 (2,289 stored rows / 212 dedup keys)

**Check 1 — the outcome rule: PASS.**

```
   stored rows      : 2289
   compared         : 2289
   reproduced       : 2263  (98.9%)
   RESIDUALS (26), classified:
     stored value matches a WHOLE-DAY window : 26
     UNEXPLAINED                             : 0
   -> PASS   (2263 reproduced, 26 stored-side anomalies, 0 unexplained)
```

The ported resolver reproduces the production rule exactly. All 26 residuals are
positively identified (see F-27), not explained away by elimination.

**Check 2 — detections: FAIL. The harness is NOT TRUSTED.**

```
   coverage: 2422/2422 symbol-days (100.0%), 0 missing
   symbols replayed : 133
   stored keys      : 212
   replayed keys    : 198
   reproduced       : 169  (79.7%)   bar: >= 85%     <-- MISSED
   missed           : 43
   extras           : 29             bar: 0          <-- MISSED
   level mismatches : 157
```

**No full replay was run, and the bar was not moved.** It was fixed in the
design before the number existed, and the number did not clear it.

Residuals classified — REPLAY_DESIGN §10.2 requires this rather than a shrug:

```
EXTRAS by producing-engine lifecycle:
  PBK ACTIVE 9 - VWR ACTIVE 6 - SDN ACTIVE 6 - RNG ACTIVE 4 - PDL 2 - GAP 1 - GDB 1
  -> produced by RETIRED engines: 0 of 29        (all nine read ACTIVE)

MISSED by family:   VCE 17 - VWR 17 - ORB 4 - RNG 4 - SDN 1
MISSED by verdict:  BELOW_CONVICTION 14 - REJECTED_COST 8 - TAKEN 8 -
                    BLOCKED_STRUCTURE 7 - BLOCKED_EVENT 2 - others 4

LEVEL MISMATCHES by relative entry difference:
  <= 5 bps 71 - <= 25 bps 64 - <= 100 bps 14 - > 100 bps 1
  largest: LLOYDSME/VWR 151bps, ADANIGREEN/VWR 96bps, URBANCO/VWR 89bps
```

**Found:** misses and extras both concentrate in **VCE and the VWR family
(VWR+PBK)** — 34 of 43 misses, 15 of 29 extras. Those are the two engine groups
most sensitive to intra-minute price and to VWAP, and the live daemon overlays
tick-derived `day_high`, `day_low`, VWAP and `session_volume` onto every context
(`apply_live_quotes`, `merge_live_bars`) on a 15 s cadence, while the replay
computes all four from completed minute bars only. 135 of 150 level deltas are
within 25 bps, which is the same signature. This is §10.2 cause 1 (tick vs bar)
— **predicted by the design, but materially larger than the 85% bar assumed.**

The lifecycle hypothesis is **dead, not unexamined**: all nine engines read
ACTIVE from live config, so not one extra is explained by the harness
deliberately ignoring lifecycle state.

### 6 — FOUND ALONG THE WAY

**F-27 — 26 of 2,289 stored outcomes on 2026-08-14 were scored from the SESSION
OPEN, not from the setup's own timestamp.** All 26 reproduce exactly under a
whole-day window and none reproduce under the post-detection window that
`outcomes.py:142-150` actually implements. The clearest case is two GODREJCP SDN
shorts 15 seconds apart with **identical entry (935.8) and identical target
(932.3)**:

```
stop 938.12  ts 04:02:46Z  stored STOP   (-0.248%)   <- the FARTHER stop
stop 937.12  ts 04:03:01Z  stored TARGET (+0.168%)   <- the NEARER stop
```

The farther stop cannot be reached first. **At least one of those two stored
outcomes is wrong under any single consistent window**, so this is not a window
disagreement being misread — it is bad data in the table `intraday_priors` and
`hurdle`'s arrival distribution are both built from. `outcomes.py:210` is the
only writer of the column, and its window logic is unchanged since commit
`042217e`, so the mechanism is **not yet explained**. 1.1% of one session.
**Recorded, not fixed.**

**F-28 — the Stage 2f static check caught two real bugs in this session's own
new code, before either ran.** Both in `swing_inputs.py`:

```
tools/replay/swing_inputs.py:133 pages sector_strength, whose sort key has never been measured
tools/replay/swing_inputs.py:151 pages event_calendar, whose sort key has never been measured
```

Probed against the live tables:

```
sector_strength   2,716 rows, NO `id` column (the stock_data_daily trap again)
  sector          1000 rows,   25 distinct, 975 duplicates -> NOT UNIQUE
  date,sector     1000 rows, 1000 distinct,   0 duplicates -> UNIQUE
event_calendar    51 rows, `id` present and UNIQUE
  event_date      COLUMN ABSENT -> would have raised 42703 on page one
```

I had written `order_by="sector"` — non-unique, which is the *worse* failure
because it pages without error while letting rows repeat and vanish across page
boundaries — and filtered on `event_date`, **a column that does not exist**
(the table has `start_date`/`end_date`). Both fixed; both keys added to
`_FETCH_ALL_SORT_KEY` with the measurement recorded beside them. This is the
check paying for itself on the first outside code it ever saw.

**F-24 is resolved.** `kite` was red on 2026-08-15 (`'NoneType' object has no
attribute 'profile'`); it now returns `profile OK: DSY688`.

### 7 — NOT DONE

- **No full replay, and no Q1/Q2 numbers.** The harness did not clear its own
  detection bar, and running analysis on an unverified harness is precisely the
  failure this verification step exists to prevent.
- **The swing arm was built but not exercised end to end.** `swing_inputs.py`
  smoke-tests clean — 500 stocks, 23 sectors, regime and FII both resolving
  as-of-date rather than latest, verified on two consecutive dates — but no
  swing plan has been replayed through `ladder.step_swing`.
- **Parameter freezing (REPLAY_DESIGN §8) is not implemented.** The engines read
  `cfg_*` at call time — `base.confirmation_pct` does, for one — so this
  verification ran against **today's** config. For a two-day-old session that is
  near-harmless; for March it is not, and the freeze must exist before any
  window is scored.
- **The bar-cache format deviates from the design**: gzipped JSON per symbol-day
  rather than parquet per symbol-month, to avoid introducing a `pyarrow`
  dependency into this project for a backtest. Stated in `bars.py`.
- **F-25, F-26 and F-27 recorded, not fixed.**

### 8 — COULD NOT DETERMINE

- **Why those 26 rows were scored from the session open.** The single writer's
  window logic is unchanged since the original commit and reads correctly.
  Whether this affects sessions other than 2026-08-14 was **not measured** — it
  costs a full bar fetch per session — so the finding is recorded rather than
  extrapolated to the book.
- **Whether reproducing the live quote overlay would clear the 85% bar.** The
  concentration in VCE/VWR/PBK is strong circumstantial evidence with a named
  mechanism, and nothing more. It is a hypothesis, not a measurement.
- **Whether the 29 extras are genuinely absent from the live record, or were
  recorded and then collapsed by `_setup_is_new` under a different entry drift.**
  Not separated.
- **Whether any historical date's stored universe came from
  `fetch_chartink_universe`'s fallback path** (F-26) rather than its own date.
  Nothing records which path was taken, so it cannot be recovered from the
  database.

**Recommends:** before any window is scored — (a) reproduce the live quote
overlay in `contexts.py`, or re-derive the acceptance bar from a measured
tick-vs-bar floor instead of the assumed 85%; (b) replace the 2 dp level
tolerance with a relative one, since a 2 dp match on a Rs 37,550 stock demands
the identical tick and 90% of current deltas are under 25 bps; (c) implement the
§8 parameter freeze before touching any date older than this week. **F-27
deserves its own stage** — it is bad data in the table every prior is built from,
and it is the only finding here that touches money already at risk.

**Gate: BLOCKED** — the harness reproduces the outcome rule with zero unexplained
residuals across 2,289 rows, but reproduces only 79.7% of stored detections
against a pre-registered 85%, and produces 29 detections the live system did not
against a bar of zero. It is not trusted, no replay was run, and the bar was not
moved to make it pass. `python -m tools.verify`: **all 531 checks passed across
60 modules.**

---

## 2026-08-16 — Replay reproduction — the gap is intra-minute PRICE and it does not close: cadence buys exactly zero, VWAP is ruled out, and every convention that clears 85% does so by lookahead. Two real harness defects fixed; §8 freeze built with three refusals, each demonstrated refusing

**Ran:**

```bash
git checkout -b fix/replay-reproduction
python -m tools.replay.verify_known_day --date 2026-08-14            # both checks
python -m tools.replay.verify_known_day --date 2026-08-14 --sweep    # 5 conventions
python -m tools.replay.freeze
python -m tools.replay.holdout --dry-run                             # x4, see §5
python -m tools.verify
```

No full replay. No window scored. The 85% and 0-extras bars were not moved.

### 1 — THE LEVER: A STORED `entry` IS THE LIVE LTP

Every one of the nine engines ends with `entry=round(ctx.ltp, 2)` — `orb.py:146`,
`gap_and_go.py:122`, `prev_day_levels.py:157`, `squeeze.py:126`,
`pullback.py:137`, `vwap_reclaim.py:162`, `range_fade.py:121`,
`short_distribution.py:195/270/314`, `gap_down_bounce.py:187`. So the `entry`
column on a recorded detection is not a derived level. **It is the tick price the
live system was looking at, to two decimals.**

That turns "tick versus bar" from a story into a query. Over all 212 stored
dedup keys on 2026-08-14, asking whether that number appears as a completed
minute-bar CLOSE near its own timestamp:

```
   stored entry == close of the bar it was detected in       18   ( 8.5%)
   == a close within +/- 1 bar                               22   (10.4%)
   == a close within +/- 2                                    7   ( 3.3%)
   == a close within +/- 3                                    6   ( 2.8%)
   == a close within +/- 5                                   12   ( 5.7%)
   == a close within +/- 10                                  10   ( 4.7%)
   NO bar close within +/- 10 bars                          137   (64.6%)
```

The replay's `ltp` is a completed bar's close by construction. **On roughly 91%
of recorded detections the live system decided on a price that is not any bar
close in the neighbourhood.** The number is not in the data, so no schedule for
reading the data can recover it.

### 2 — THE SWEEP: WHAT EACH HYPOTHESIS IS ACTUALLY WORTH

`tools/replay/conventions.py` makes "when does the replay look, and what may it
know" an explicit, named object instead of an assumption buried in a loop. Five,
same day, same bars, same code:

```
convention       repro     pct  missed  extras   keys   reads a price
                                                        before it printed?
bar_close          169   79.7%      43      18    187   no
cadence_15s        169   79.7%      43      18    187   no
next_open          172   81.1%      40      21    193   no
ltp_bracket        197   92.9%      15      39    236   YES
full_overlay       197   92.9%      15      40    237   YES
```

**Hypothesis 1 — a 15 s cadence over minute bars. Worth exactly zero.** Not
"little": zero. 169/43/18 under both, to the key. Four looks a minute at an
identical context are four identical answers, and
`test_cadence_alone_cannot_change_a_single_detection` asserts the mechanism
directly — every extra look `cadence_15s` emits carries the same `(upto, ltp)`
pair as the bar-close look it follows.

**Hypothesis 2 — interpolated VWAP. Ruled out, twice.** `full_overlay` adds the
whole live quote overlay — VWAP, day range and session volume advanced into the
forming bar — on top of `ltp_bracket`, and reproduces the *same 197 keys*. And
`intraday_quote_parity` recorded that session directly, 9,154 samples per field:

```
   field         n   mean|diff|%     p50     p90      max   identical
   day_high   9154       0.0066  0.0000  0.0000   3.3896     8737 (95.4%)
   day_low    9154       0.0037  0.0000  0.0000   2.9162     8744 (95.5%)
   prev_close 9154       0.0032  0.0000  0.0000   4.8995     9148 (99.9%)
   vwap       9154       0.0020  0.0000  0.0031   0.3431     5745 (62.8%)
   volume     9154       4.7658  2.3003  6.7068  99.9508        0
```

Live VWAP and bar VWAP are median-identical and 0.003% apart at p90. Only
`session_volume` genuinely diverges (4.8% mean), and it is the bar-sum
approximation the design already flagged. **The residual is the LTP alone.**

**Hypothesis 3 — a different bar-close convention. Worth 3 keys.** The only
sub-bar sample that is *not* lookahead is the forming bar's OPEN: at 09:16:15
the first trade of 09:16 has already printed, while its high and low may not
print until 09:16:50. `next_open` reaches 81.1%. Its high/low siblings reach
92.9% by asserting a price before it happened.

### 3 — THE ANSWER: IT DOES NOT CLOSE, AND THE TWO BARS MOVE APART

**Every convention that reads no price before it printed stays under 82%. Every
convention that clears 85% does so by lookahead — and its extras rise as its
misses fall: 18 → 21 → 39.** There is no point in the family where 85% and zero
extras hold together, and extras are the bar the design says matters more,
because an extra means the replay is more permissive than live and inflates
every downstream n.

Minute bars bound the live information set (`bars` known exactly; `ltp` known
only to lie in `[low, high]`) but do not determine it. **Closing this needs
sub-minute data.** Kite's smallest historical interval is `minute`, so it cannot
come from history; it can only be recorded going forward from the tick stream
the daemon already receives. That is a separate piece of work and it does not
help March.

**What the residual means for every number the replay would produce**, at the
shipped `bar_close` convention:

```
   fam     stored  repro  missed  extra   net
   SDN        102    101       1      3    +2      100% SHORT
   VWR         54     37      17     10    -7      LONG
   ORB         21     17       4      1    -3      LONG
   VCE         17      0      17      0   -17      LONG
   RNG         17     13       4      3    -1      LONG
   GDB          1      1       0      1    +1      LONG
```

- **The bias is toward FEWER detections** — 43 missed against 18 extra, net −25
  on 212, ~12% short before any gate.
- **It is not evenly spread, and it is collinear with direction.** SDN, the only
  short engine, reproduces 101 of 102 and is net +2. Every long engine is net
  negative. A long-versus-short comparison out of this replay would favour the
  short book for a reason that is purely an artefact of the harness — the same
  collinearity REPLAY_DESIGN §1 flags for Q2, biting somewhere new.
- **VCE reproduces 0 of 17, and it is structural rather than statistical.**
  `squeeze.py:57-77` takes `r_hi = max(b.high for b in bars[-n:])` and then
  refuses unless `ctx.ltp > r_hi`. At a bar close `ctx.ltp` is `bars[-1].close`
  and `bars[-1]` is inside that window, so `ltp <= bars[-1].high <= r_hi` holds
  on every bar of every symbol on every day. **`SqueezeExpansion` cannot fire at
  all under a bar-close replay.** A full replay would have reported "VCE: no
  detections in 75 days" — which is the exact inversion the design warns about
  for lifecycle filtering, arriving through a different door.
  `test_the_bar_close_convention_cannot_fire_the_squeeze_engine` pins both
  halves: never at a close, and it MUST fire at a price 20 bps above its own
  coil, so "the convention blinds the engine" stays distinguishable from "the
  engine never fires".

### 4 — TWO REAL HARNESS DEFECTS, FOUND WHILE DIAGNOSING

**F-29 — the universe was ranked on the replayed day's OWN rows, which is
lookahead.** `scanner._latest_date()` returns the newest `stock_data_daily` date
carrying a non-null `value_cr`, and `value_cr` comes from the bhavcopy, which is
not published until after the close. At 09:15 on D the newest usable date is
**D-1**, so the 40 names the daemon streamed on D were ranked on D-1's close,
turnover and ATR. `build_universe_at(D)` used D's own row — selecting the day's
universe with the day's own outcome. Measured:

```
   universe from 2026-08-14 (own rows) , top 40 : 30 of 40 in the live record (75%)
   universe from 2026-08-13 (prior)    , top 40 : 35 of 40                    (88%)
   the two top-40s share 27 names; symmetric difference 26
```

`universe.scan_date_for_session()` now resolves it, strictly `< day`.

**F-30 — `skip_flagged` was forced off by reasoning about the wrong table.** The
harness disabled the ASM/F&O filter citing REPLAY_DESIGN §4.2 — `safety_lists`
has no date column and cannot be reconstructed. True, and irrelevant here:
`build_universe` never reads `safety_lists`. It reads `asm_flag` and
`fo_ban_flag` **off the `stock_data_daily` row it is already ranking**
(`scanner.py:209`), and those are written by
`ingest_asm_gsm.update_stock_data_flags` with `.eq("date", today)` — that date's
own row, on that date. **Point-in-time all along.** On 2026-08-13: 10 rows carry
`asm_flag`, 4 carry `fo_ban_flag`, none null. Forcing the filter off admitted
HFCL and KALYANKJIL (ASM) and MANAPPURAM and SAIL (F&O ban) into a universe the
daemon had excluded. §4.2's warning still stands for the SWING arm, which does
read `safety_lists`; it should not have been carried across.

Together: **extras 29 → 18.** Reproduction is unchanged at 169/212, and that is
correct rather than disappointing — the replayed symbol set is the union of the
universe and every symbol the live record names, so universe membership cannot
cause a miss. An earlier version of the classifier made it one and would have
credited the universe fix with 25 misses it does not touch.

Full §10.2 classification, 2026-08-14, `bar_close`:

```
   MISSES (43)   live LTP is intra-minute .................. 30   cause 1
                 engine silent on a reachable price ........ 13   residual
   EXTRAS (18)   symbol watched live, family never fired ... 15
                 symbol absent from the live record ........  3
   cause 2  live_rerank promotion ......................... 0 (the union covers it)
   cause 3  config drift ................................... 0  MEASURED
   cause 4  code drift ..................................... 0  MEASURED
```

Cause 3 and 4 are measurements, not assumptions: `git log --since=2026-08-14`
over `intraday/strategies/`, `intraday/session.py` and
`intraday/market_context.py` is empty, and of the 89 engine/scanner/session
config keys present in `system_config`, **0 have an `updated_at` later than the
replayed session**. The previous entry called running without a freeze
"near-harmless for a two-day-old session"; that is now a number rather than a
hope.

### 5 — THE §8 PARAMETER FREEZE, AND FOUR DEMONSTRATIONS

`tools/replay/freeze.py` + `tools/replay/holdout.py`.

```
   wrote backend/tools/replay/params/frozen.json
     sha  : ecca2add0ff4129c8d43a47d2ea99e90c481c4e0bf47a6f3692367de1ddf2ae2
     keys : 177  {'system_config': 150, 'source_default': 27}
     backend/intraday/strategies            6695d6b952559eb0560733cbb4fb7955fcb7a872
     backend/intraday/exit_policy.py        5f54e6a557349bfdf3235e1bb237e5dc35c53526
     backend/control/position_lifecycle.py  95541123ca80582d07f8081c29fc4115d15830e7
     backend/analysis/risk_model.py         ef5d80e79bb63438e1a0f6569d7006084b76ebf5
     backend/intraday/scanner.py            c4177dd3eee381577adb3b12a50bae057a2a9e3e
```

**The seam is one dict, not four patched functions.** `cfg`, `cfg_bool`,
`cfg_int` and `cfg_float` all resolve through `config._sys_config`
(`config.py:369-401`), so `frozen_config` substitutes that dict and restores it
on exit. No engine is modified, wrapped, or aware of it.

**27 of the 177 keys are not in `system_config` at all** — `cfg` returns the
caller's literal, so the freeze records the RESOLVED value with its source.
Freezing only the table would have left those 27 free to move whenever someone
edited a default inside an engine, silently changing a "frozen" replay.

Refusals, each demonstrated REFUSING before it was believed:

```
A. clean tree, committed params, no prior result
     all preconditions met                                          EXIT=0

B. R3  a result already exists for this params SHA
     [X] R3 a holdout result already exists for params ecca2add0ff4
         (holdout_ecca2add0ff4.json, run 2026-06-30T18:00:00+05:30)
         — the holdout is ONE look, and this would be the second     EXIT=1

C. R1  dirty working tree
     [X] R1 working tree is dirty (1 path(s)):
         M backend/intraday/strategies/squeeze.py                    EXIT=1

D. R2  params tracked but EDITED since commit
       (vce_max_contraction_ratio 0.65 -> 0.99, no re-freeze)
     [X] R1 working tree is dirty (1 path(s)): M .../frozen.json
     [X] R2 frozen.json differs from its committed version
         (HEAD 9fd14542e7b2, disk aa39ec380f1a)
     [X] R2 recorded sha ecca2add0ff4 does not match its own contents
         (5d7dc0b5fdb9) — the file was edited by hand                EXIT=1
```

D catches the quiet tweak twice over, by two independent routes — the git blob
comparison and the file's own self-hash.

**F-31 — the freeze could not have worked, because `frozen.json` was
unstageable.** The root `.gitignore` blanket-ignores `*.json` as a credentials
guard (line 19). `backend/tools/replay/.gitignore` carried a paragraph saying
"params/ is DELIBERATELY NOT ignored ... ignoring it would silently disable that
gate" — **as a comment, with no rule under it.** So R2, "the parameters must
resolve to a committed git object", was unsatisfiable by any sequence of
actions. A gate that cannot PASS is the same defect as one that cannot fail, and
this project has now shipped one of each. Negation rules added and proven with
`git check-ignore -v`. The root rule is untouched everywhere else, and the
narrow exemption is paired with a refusal: `freeze.build()` aborts on any key
whose `system_config` row is marked `is_secret`, because this file is committed.

**F-32 — `config.get_system_config()` is an unpaged `.select("key,value")`** —
the F-19 idiom on the reader every `cfg()` call in the system goes through. 510
rows today, under the 1000 cap, so nothing is truncating. But a freeze silently
missing 400 keys would resolve every one of them to a source default and look
entirely normal. `freeze.build()` now counts the returned rows against an exact
server count and refuses. **The production reader is recorded, not fixed** — it
is out of this stage's scope.

### 6 — WHAT WAS BUILT OR CHANGED

```
 NEW  tools/replay/conventions.py   the evaluation-point generator + 5 conventions
 NEW  tools/replay/freeze.py        §8 freeze, 177 keys, 5 code SHAs, secret guard
 NEW  tools/replay/holdout.py       preflight R1/R2/R3, one-look enforcement
      tools/replay/contexts.py      `upto` split from `now`; apply_forming_bar
      tools/replay/detect.py        takes a Convention; Detection carries `look`
      tools/replay/universe.py      scan_date_for_session; skip_flagged honoured
      tools/replay/verify_known_day.py  residual classification, --sweep, bps
      tools/replay/.gitignore       the negation that makes R2 satisfiable
      tests/test_replay_harness.py  15 -> 25 checks
```

**No live engine, gate, exit rule, config key, migration or `system_config` row
was touched.** `python -m tools.verify`: **all 541 checks passed across 60
modules** (was 531).

### 7 — NOT DONE

- **The harness is still NOT TRUSTED and no window was scored.** 79.7% against
  85%, 18 extras against 0. `tools/replay/holdout.py` refuses to score anything
  for that reason and says so in its own output.
- **F-27 not investigated** — out of scope for this session by instruction. The
  outcome check re-ran unchanged: 2263 of 2289 (98.9%), all 26 residuals
  reproducing exactly under a whole-day window, **0 unexplained**.
- **The one-look record is local.** `results/holdout_<sha>.json` is caught by the
  root `*.json` ignore, so deleting it restores the second look and nothing in
  git would show that it happened. R3 is a filesystem guarantee, not an audited
  one.
- **F-30's correction was not applied to the swing arm.** `swing_inputs.py`
  still runs with empty ASM/F&O sets, correctly — that path reads `safety_lists`,
  which genuinely has no history.
- **The 13 `engine_silent_on_a_reachable_price` misses are unexplained.** Config
  drift and code drift are both measured at zero, so the cause is elsewhere —
  most likely the remaining scalar differences (`session_volume` diverges 4.8%)
  or the stored LTP matching a nearby close by coincidence rather than because
  the replay stood at that instant. Not separated.

### 8 — COULD NOT DETERMINE

- **Whether sub-minute reproduction would clear both bars**, because sub-minute
  history does not exist to test it with. `ltp_bracket` shows 92.9% is reachable
  from the bar envelope; it does not show that a real tick path would have
  produced those same keys, and it produced 23 detections that exist only at a
  sub-bar look.
- **Whether the 3 `symbol_absent_from_live_record` extras are genuine
  permissiveness** or names the daemon's bench never reached that day. The live
  watch list is not recorded anywhere, so it cannot be reconstructed.
- **Whether 2026-08-14 is representative.** One session. The entry-versus-bar-
  close measurement costs a full bar fetch per session and was not extended.

**Recommends:** the detection arm cannot be verified against the live record at
minute resolution, and no amount of convention design changes that — so either
(a) record ticks forward from the daemon and re-verify on a session captured
that way, or (b) scope the replay to questions that do not depend on reproducing
individual detections, and say plainly in every output that per-engine detection
counts are biased low for long engines and unusable for VCE. **Q1 (the swing
target sweep) is untouched by all of this** — it replays plans, not intraday
detections — and is the one arm that could proceed on the current harness.

**Gate: BLOCKED, for a now-diagnosed reason.** The harness reproduces the
outcome rule with zero unexplained residuals across 2,289 rows and reproduces
79.7% of stored detections against a pre-registered 85%. The shortfall is
measured, not guessed: 30 of 43 misses are prices that exist only inside a
minute, cadence is worth zero, VWAP is worth zero, and every convention that
clears the bar does so by reading a price before it printed. **The bar was not
moved.**

---
## 2026-08-16 — Replay scope decision (diagnostic, read-only) — the intraday arm is abandoned: of six engines, two are already answered NO on live data, two can never reach n, one cannot fire, leaving ONE — the short one. The swing arm's reproduction anchor reproduces 1181/1181 EXACTLY and its holdout n_effective is 46, not the single digits §8.1 feared. Two holdout dates carry intra-session OHLC

**Ran:** read-only SQL against the live database, plus one scratch script over the
EXISTING bar cache. No replay. No fetch. No window scored. Nothing written to the
repo outside this ledger.

### 0 — A PREMISE CORRECTION, BECAUSE IT CHANGES THE QUESTION

The brief says the swing arm "reads `signal_output_daily`". It does not, and may
not: `tools/replay/independence.py:55` lists that table as FORBIDDEN — *"the day's
stored plans; replaying against them replays their conclusions"*. `swing_inputs.py`
reads `stock_data_daily`, `sector_strength`, `market_regime`, `fii_dii_flow` and
`event_calendar`, and the harness **regenerates** plans through the nine screener
engines. So the swing arm does not merely avoid the `ltp` problem — it has a
*harder* job than the brief assumes (it must reproduce plan SELECTION, not just
read it) and a *better* anchor to check itself against. Both halves matter below.

### 1 — INTRADAY ARM: NO. AND THE REASON IS NOT THE 79.7%

The reproduction shortfall is the known blocker. It is not the decisive one. Apply
the design's OWN pre-registered Q2 rule — n>=100 per window, gross R>0 by >=2 SE in
in-sample AND validation, positive in holdout — to the six engines, and the arm is
already empty before fidelity is argued at all.

**First: two of the six are already answered, on live data, with no replay.**
Complete-population figures (2026-08-15 re-score), 2 SE interval on gross R:

```
  engine    n    gross R +/- SE     2 SE interval        verdict
  VWR     307   -0.345 +/- 0.071  [-0.487, -0.203]   ALREADY NO   (-4.9 SE)
  ORB     119   -0.241 +/- 0.100  [-0.441, -0.041]   ALREADY NO   (-2.4 SE)
  SDN     398   -0.077 +/- 0.072  [-0.221, +0.067]   open
  VCE     138   -0.155 +/- 0.118  [-0.391, +0.081]   open
  RNG      60   -0.137 +/- 0.157  [-0.451, +0.177]   open
  PBK      32   -0.276 +/- 0.244  [-0.764, +0.212]   open
```

VWR and ORB exclude positive gross R at 2 SE **already**. A replay cannot add
information to a question the live book has closed; it can only disagree with it,
and if it did, the harness would be the suspect.

**Second: of the four still open, three cannot be reached.** Detection rates from
the live book (14 sessions), PROJECTED onto the clean windows (30 / 18 / 21 usable
days, §8) — projections, not measurements:

```
  engine  per-session   in-sample  validation  holdout   n>=100 in all three?
  SDN         28.4         853         514       597     yes
  VCE          9.9         297         178       208     yes -- but 0 of 17
                                                          reproduce (structural)
  RNG          4.3         129          77        90     NO  (2 of 3 windows)
  PBK          2.3          69          41        48     NO  (0 of 3 windows)
```

- **PBK** prints INSUFFICIENT in every window. It can never be ranked.
- **RNG** prints INSUFFICIENT in validation and holdout, so it fails rules (b)
  and (c) by arithmetic, whatever the bars say.
- **VCE** has ample n and reproduces **none of it**. `squeeze.py:57-77` takes
  `r_hi = max(b.high for b in bars[-n:])` then requires `ctx.ltp > r_hi`; at a bar
  close `ctx.ltp` IS `bars[-1].close <= bars[-1].high <= r_hi`, so the engine
  cannot fire at any close, on any symbol, on any day.

**That leaves exactly one engine: SDN. The short one.** The only engine with
adequate n in every window and an open question is the single engine that is 100%
SHORT — which is precisely where the harness's composition bias is concentrated
(SDN net +2 on 102; every long engine net negative). A "yes" from that arm would be
one engine, in the one direction the harness over-produces, and indistinguishable
from the artefact by any test the harness contains.

**Third — and this one CORRECTS an assumption I would otherwise have carried in.**
It is natural to assume the replay's bar-close entry is systematically *better*
than the live tick entry (a breakout entered on the retrace rather than the spike),
which would inflate gross R. **Measured, and it is not true.** All 220 dedup keys
on 2026-08-14, each matched to the minute bar containing its own timestamp, from
the bars **already in the cache** — no fetch. Sign convention: positive = the
replay's entry is more favourable than live.

```
  engine  dir      n   mean_bps   median   %replay_better   %exactly_at_close
  SDN     SHORT  102     -0.94     -1.87        38.2%             4.9%
  VWR     LONG    35     +0.20     +1.18        51.4%            11.4%
  PBK     LONG    22     -0.31      0.00        36.4%            22.7%
  VCE     LONG    17     -0.57      0.00        41.2%            11.8%
  RNG     LONG    17     -1.06     +0.59        52.9%             5.9%
  PDL     LONG    15     -0.11     +0.75        53.3%             0.0%
  ORB     LONG     9     -2.76     -2.92        33.3%            22.2%
  ------------------------------------------------------------------------
  ALL            220     -0.65      0.00        42.7%             8.6%
  pooled LONG    118     -0.39      0.00
  pooled SHORT   102     -0.94     -1.87
```

**The entry-PRICE channel is worth about two thirds of one basis point, against a
~21 bp MIS round trip — roughly 3% of one round trip, and pointing mildly AGAINST
the replay.** So conditional on a key reproducing, its entry is faithful. The
damage is entirely **composition** — which keys exist — not price. That is a
narrower defect than assumed and it still kills the arm, because composition is
exactly what a per-engine mean is computed over.

**Answer to Q1: no, the intraday replay cannot answer "do any of these engines show
positive gross R".** Two engines are already answered NO without it; two can never
reach the pre-registered n; one cannot fire; and the remainder is a single short
engine sitting on the harness's own bias. **Recommend abandoning the intraday arm
rather than running it with caveats.** Caveats cannot repair a denominator.

### 2 — SWING ARM: IT HAS AN ANCHOR, IT REACHES EVERY WINDOW, AND THE ARITHMETIC REPRODUCES EXACTLY

**The equivalent reproduction test exists in two halves, and the half that killed
the intraday arm is the half the swing arm passes outright.**

The intraday arm died because the stored `entry` is `round(ctx.ltp, 2)` — a tick
price, matching a bar close on 8.5% of detections. **The swing planner stores no
tick price anywhere.** `compute_msl.py:2151` calls `compute_trade_levels(entry_price
= ez_low, atr_abs = atr_14, anchor_price = ez_low, structure_stop = supertrend,
regime = ...)`. Every argument is a stored daily column.

Rebuilt from `stock_data_daily.atr_14`, `.supertrend` and `market_regime`, against
every stored plan that carries one:

```
  stored plans with planned_stop (2026-07-24 .. 2026-08-14)   1181
  rebuilt stop matches stored, to 2 dp                        1181   (100.0%)
  mean relative error vs risk                                 0.0001
```

**1181 of 1181, exact.** Where the intraday anchor scored 8.5%, this scores 100%.
The R:R constant falls straight out of it and confirms Stage 2c: 3.0 / (1.5 x 1.05)
= 1.90476 — `risk_target_atr_mult` over `risk_stop_atr_mult` times the NEUTRAL
regime multiplier, with `regime_scales_target` off.

**The second half — plan SELECTION — has an anchor too, and unlike the intraday one
it reaches the clean windows.** Two candidate anchors, and only one is usable:

```
  table                  rows   dates  from         levels present from
  signal_output_daily    2430      36  2026-06-25   planned_stop 2026-07-24
  master_shortlist       7212     116  2026-03-09   entry_zone_low throughout
```

`signal_output_daily` — the 114-column plan record — **begins 2026-06-25 and carries
`planned_stop`/`planned_target` only from 2026-07-24**, which is entirely inside the
CONTAMINATED window. It cannot verify a clean window at all. `master_shortlist`
carries `entry_zone_low`, `final_score` and `engines_list` across **116 dates from
2026-03-09**, covering in-sample, validation and holdout, and it is **not** on the
forbidden list (`independence.py:49-70`) — it is the screener's own output, so
comparing against it is verification in exactly the sense §10.1 permits for
`intraday_setups`. It would need the same explicit whitelist exemption.

**Can it pass? Not demonstrated — it has not been built or run, and I will not
claim otherwise.** What is established is that the failure mode that killed the
other arm is absent by construction, and that the level arithmetic is exact. The
open risk is symbol selection, not price.

**Point-in-time, verified rather than assumed.** Every June `stock_data_daily` row
was written on its own session date (`created_at` max = min, 0 days after). Note
what that does and does not prove: it rules out a late INSERT, not a late UPDATE,
since an upsert preserves `created_at`. Which is how the next item surfaced.

### 3 — n_effective FOR Q1'S 6x4 SWEEP OVER THE 21-DAY HOLDOUT: **46**, NOT SINGLE DIGITS

Measured on the **actual** 2026-06-01..06-30 holdout — 21 trading days — not
extrapolated. Forward bars run to 2026-08-14, so every June plan gets its full
15-session hold: **nothing is right-censored.** Plans from `master_shortlist`,
stops rebuilt by the arithmetic proven exact above, ladder walked per §7.3 with the
bad fill (stop wins ties):

```
  plans with valid levels (21 days, ~73/day)                      1541
  triggered (price traded into the entry zone within 10 sessions)  883   57%
  reached 1.905R before the stop                                   188
  give-back (50%, live) fired                                      612   69% of triggered
```

`n_effective` is not "reached 1.905R" — it is the count whose **paired difference is
non-zero**, i.e. plans where the target actually BINDS before another rung takes the
trade. Give-back and the 1.0R move-to-breakeven both cut ahead of it; the trail does
NOT, because `exit_trail_after_r` is **2.0**, above 1.905R, so it can never bite
before the target at the baseline. Per give-back column:

```
  give-back setting      n_effective (m-axis)
  50%  (live baseline)         46
  65%                          60
  OFF                         130
  give-back axis itself       612 trades on which the rung fired
```

**n_effective ~= 46 in the weakest cell of the grid — above 30, so the swing arm is
NOT uninformative before it is run.** This contradicts REPLAY_DESIGN §8.1, which
projected "tens of observations, possibly single digits" from `EXIT_TARGET` firing
0 of 11 live. That projection was drawn from 11 trades the ENTRY RANKER had already
filtered to a handful of names; the replay scores all ~73 plans/day, and the reach
rate on the full plan population is an order of magnitude more productive than the
traded book implied. §8.1's caution was right in method and wrong in magnitude.

**46 is an UPPER bound and should be read as one.** Modelled: stop, target,
give-back, breakeven. Not modelled: stall, time stop, partial booking, and
`EXIT_DETERIORATION` — which needs live trend context daily bars cannot supply.
Each can only cut trades before the target, never add them. The stall and time-stop
rungs both gate on peak < 0.5R and so cannot touch a trade that reached 1.905R; the
realistic residual is deterioration and partial, and the honest statement is
**46 with a floor no lower than the high 30s**, still clear of 30.

Robustness: recomputed with the forward walk on `raw_prices` (complete bhavcopy)
instead of `stock_data_daily`, **46 vs 47** — insensitive to which price table
walks the ladder.

### 4 — F-33: TWO HOLDOUT DATES CARRY INTRA-SESSION OHLC, NOT THE SESSION'S

Found while testing whether `created_at` proved point-in-time. It did not, and this
is what it was hiding.

`stock_data_daily` on **2026-06-17** and **2026-06-18** disagrees with `raw_prices`
on the same (symbol, date):

```
  date         n    close differs   high too low   mean |diff|
  2026-06-17  500      497 (99.4%)      152          58.2 bps
  2026-06-18  500      493 (98.6%)      136          59.4 bps
```

The signature is diagnostic, not ambiguous. On 2026-06-17, `open` and `low` agree
**exactly** while `high` and `close` do not, and the stored `high` is never above
the bhavcopy's:

```
  symbol      sdd O/H/L/C                         raw_prices O/H/L/C
  SBIN     1017.0 / 1023.0 / 1013.45 / 1020.85    1017.0 / 1028.1 / 1013.45 / 1026.5
  TITAN    4338.0 / 4380.5 / 4327.5  / 4367.9     4338.0 / 4395.0 / 4327.5  / 4380.5
  RELIANCE 1333.0 / 1334.0 / 1317.0  / 1327.2     1333.0 / 1334.0 / 1317.0  / 1332.7
```

That is a **truncated session**: the row was written at **13:07 IST**, before the
close — open and low already made, high and close not yet. `raw_prices` for the same
date was written the next morning (2026-06-18 07:33 UTC) from the complete bhavcopy.
`created_at` showed max = min on both dates, so no second INSERT ever corrected them.

**Scope: exactly two dates** across the whole checkable overlap
(2026-04-16..2026-08-14, where `raw_prices` exists). Both fall inside the 21-day
holdout. Consequences:

- Every one of the 86 indicators on those two dates is computed from a partial
  session, and the rolling ones (ATR, the moving averages, supertrend) carry that
  error FORWARD for their window length. So the exposure is wider than two dates.
- Plans GENERATED on 06-17/06-18 rest on wrong indicators. That is a different and
  larger exposure than the ladder walk, which measured insensitive (46 vs 47).
- **2026-03-06 .. 2026-04-15 cannot be checked this way at all** — `raw_prices`
  begins 2026-04-16 — so roughly the first third of in-sample is unverifiable for
  this defect. Its rate elsewhere is 2 in 84 dates; that is not a guarantee.

**Recorded, not fixed.** This is production ingest and out of this diagnostic's
scope. It is not a replay defect — it is a data defect the replay would have
silently inherited, and would have shown up as an unexplained June anomaly nobody
could source.

### 5 — IF BOTH FAILED: WHAT WOULD ACTUALLY ANSWER Q2

They did not both fail, so this is scoped to the intraday question alone. **Not a
proposal — a costing, as asked.**

- **Tick capture going forward.** The daemon already receives the stream; recording
  it makes reproduction decidable at the resolution the engines actually decide at.
  Order of magnitude: ~95 symbols, ~2.1M ticks/session, ~85 MB/session packed,
  ~1.8 GB/month. **It cannot retro-fix March–June.** It makes FUTURE sessions
  verifiable and does nothing for the replayable history, so it does not answer Q2
  as posed — it only lets a later harness be trusted.
- **A longer live sample — and this is the honest route.** The intraday book is
  **already PAPER**. It costs time, not money, and it has perfect fidelity because
  it IS the system. At the live rate (~28 SDN detections/session), halving SDN's SE
  from 0.072 to 0.036 needs 4x the n: ~1,600 detections ~= **57 sessions ~= 11-12
  trading weeks.**
- **The ceiling both routes share, which decides this.** More n buys PRECISION, not
  SIGN. If an engine's true gross R is ~0 or negative, no sample size makes it
  positive; it only narrows the interval around the wrong side of zero. Two engines
  have already narrowed past the point of doubt. The replay was an attempt to buy 75
  days of history cheaply; the paper book delivers the same evidence at full
  fidelity, just slower — and it is already running.

### 6 — RECOMMENDATION: RUN THE SWING ARM ONLY

**Abandon the intraday arm.** Not "run with caveats" — §1 is not a fidelity
complaint that better labelling repairs. Of six engines: two answered NO already,
two structurally short of n, one unable to fire, one short engine standing alone in
the direction of the harness's own bias.

**Run the swing arm**, subject to four conditions, each of which can fail:

1. **Build the §10.1-equivalent reproduction gate against `master_shortlist` FIRST,
   with a bar fixed before the number exists, and score no window until it passes.**
   The level arithmetic is proven (1181/1181); plan SELECTION is not, and that is
   the whole remaining risk. `master_shortlist` needs the explicit whitelist
   exemption `verify_known_day.py` has for `intraday_setups`, or the §2 static check
   will (correctly) fail the build.
2. **Handle 2026-06-17 and 2026-06-18 before scoring the holdout** — exclude them,
   or rebuild them from `raw_prices`. Excluding costs 2 of 21 holdout days; leaving
   them silently prices two days of plans off a partial session.
3. **Print `n_effective` per cell**, per §8.1. Baseline (m x give-back 50) ~= 46;
   give-back 65 ~= 60; give-back OFF ~= 130; the give-back axis ~= 612. The grid is
   informative, and its weakest cell is the one the question is literally about.
4. **Label in-sample as partly unverifiable** for F-33: 2026-03-06..04-15 predates
   `raw_prices` and cannot be cross-checked.

**Do not report Q1 as "the replay's verdict" if condition 1 fails.** The swing arm
earns its answer by reproducing plan selection, exactly as the intraday arm was
required to and did not.

### 7 — NOT DONE / COULD NOT DETERMINE

- **The swing reproduction gate was not built or run.** Nothing here demonstrates
  the swing arm PASSING; it demonstrates that its arithmetic reproduces exactly and
  that an anchor spanning every window exists. Those are necessary, not sufficient.
- **n_effective assumes the replay regenerates a plan population resembling
  `master_shortlist`.** If the regenerated screener selects materially different
  symbols, 46 moves. That is condition 1 restated as a number.
- **The 220-key entry-bias measurement is ONE session** (2026-08-14, the only one
  cached). Whether the sub-1-bp figure holds in March is untested, and testing it
  costs a bar fetch per session.
- **The 13 `engine_silent_on_a_reachable_price` misses remain unexplained** —
  unchanged from the prior entry; the entry-price measurement here does not touch
  them, since it measures matched keys rather than missing ones.
- **F-33's forward contamination was not quantified** — how many trading days of
  rolling indicators the two bad dates corrupt, and by how much.

**Gate: SCOPE DECIDED. Intraday arm ABANDONED. Swing arm APPROVED to proceed to its
reproduction gate — and no further until that gate passes.** No replay was run, no
window scored, no parameter moved, nothing written outside this ledger.

---

## 2026-08-16 — F-27 follow-up (diagnostic, outcome-writer integrity) — it is systemic and it is NOT about stop distance: 111 groups of IDENTICAL setups carry contradictory outcomes, and 58 of them are impossible inside a single run. Resolution is a function of WHEN `resolve_day` happened to fire, not of the setup. VWR survives; ORB survives by 0.01R

READ-ONLY. Nothing was written outside this ledger, no row re-scored, no fix
applied. Branch `diagnostic/outcome-writer-integrity`.

### 1 — IS IT SYSTEMIC? YES, AND THE F-27 DETECTOR UNDERSTATES IT

F-27's definition — same symbol/engine/direction, identical entry and target,
**different** stops, nearer stop resolving better — across all 14 sessions and
all 8324 resolved rows:

```
  date         comparable pairs   inverted   (nearer stop resolved better)
  2026-07-28          11              0
  2026-07-31           1              0
  2026-08-06          78             10
  2026-08-07          40              4
  2026-08-10          28              1
  2026-08-11          15              2
  2026-08-12         181              6
  2026-08-13         154              5
  2026-08-14         306              5
  ------------------------------------------
  TOTAL              814             33      = 4.1% of comparable pairs
```

**33 inversions across 7 of 14 sessions.** 14-Aug is not special; 08-06 is the
worst by rate (12.8%). So: systemic, and present since at least 06-Aug.

**But the stop-distance framing is a red herring.** Dropping the "different
stops" requirement and grouping on setups that are identical in *every* field
the writer reads — `(trade_date, symbol, strategy, direction, entry, stop,
target)` — finds a much larger and cleaner defect. These are the same setup
recorded more than once, so they have no legitimate way to differ at all:

```
  date         duplicate groups   groups that CONTRADICT themselves   rows
  2026-07-28          49                    0                           -
  2026-07-30           1                    0                           -
  2026-07-31           7                    3                           7
  2026-08-03           6                    0                           -
  2026-08-05         165                   20                          72
  2026-08-06          35                    2                           4
  2026-08-07          49                    7                          27
  2026-08-10         210                   44                         173
  2026-08-11          16                    1                           2
  2026-08-12         128                   15                          34
  2026-08-13         134                    7                          21
  2026-08-14         293                   12                          46
  ---------------------------------------------------------------------
  TOTAL            1,093                  111  (10.2%)                386
```

**386 rows = 4.6% of the 8324-row population; 49 of the 1102 deduplicated
observations = 4.4%.** The cleanest single example, 12-Aug MAXHEALTH/SDN, needs
no stop-distance argument at all — ids **3634** and **3637**, 2.3 seconds apart,
entry 1017.4, stop 1026.65, target 1003.49, *every field identical*:

```
  id 3634  ts 04:08:57.893697Z   ->  TARGET   +1.161
  id 3637  ts 04:09:00.221930Z   ->  STOP     -1.115
```

Same setup. Opposite answers. 2.276R apart.

### 2 — MECHANISM: TWO OF THEM, AND THE DOMINANT ONE IS NOT THE WINDOW

Split the 1093 duplicate groups by whether their rows share a replay window:

```
  class                          groups   contradict   rate
  all rows inside ONE minute       627        58       9.3%
  straddles a minute boundary      466        53      11.4%
```

**A — Resolution is not reproducible across runs. (dominant, ~9.3pp of 11.4pp)**

The 58 same-minute contradictions are *arithmetically impossible within a single
`resolve_day` call*, and that is a proof, not an inference. Inside one run, rows
in such a group share: symbol, direction, entry, stop, target; one per-symbol
`bar_cache` entry (`outcomes.py:148-149`); and the same bar slice, because
`bars = [b for b in bar_cache[sym] if b["date"] >= after]` (`outcomes.py:150`)
quantises every `after` in the same minute onto the same first bar. The one edge
case that could break that — a `ts` of exactly `.000000`, which would admit its
own minute's bar — occurs in **0 of 8324 rows** (checked). The scan at
`outcomes.py:181-198` is pure and deterministic. Identical inputs, identical
scan, identical output. They contradict anyway, so **they were scored by
different runs against different bar series.**

What differs between runs is *how much of the session existed yet*:

- `_session_bars` calls `kite.historical_data(token, day, day)` at whatever
  wall-clock moment the run happens (`outcomes.py:60`). Mid-session, that
  returns a truncated series.
- `resolve_day` is reached only from the intraday daemon's `finally` block
  (`intraday/run.py:416`). **Every** daemon exit fires it — crash, hard kill,
  restart, lid — not just the 15:20 square-off. CLAUDE.md already records that
  mid-session restarts happen and re-record the morning.
- On a truncated series, `outcome, exit_px = "TIMEOUT", float(bars[-1]["close"])`
  (`outcomes.py:180`) prices TIMEOUT at *the mid-session price*, and any
  stop/target hit after that instant is invisible.
- The work queue is `.is_("outcome", "null")` (`outcomes.py:100-101`), so once a
  row is scored it is **never revisited**. Idempotence — the property that makes
  re-running free — is what freezes the truncated answer in permanently, while
  its twin, still NULL, gets the full-day answer from a later pass.

The signature confirms it: **STOP+TIMEOUT is 42 of the 58** same-minute
contradictions (and 41 of 53 cross-minute) — exactly the shape of "one pass saw
the whole day and found the stop, the other ended early and found nothing".
Note 08-05 has 530 rows, well under the PostgREST cap, and still carries 14 of
them, so this is **not** a re-run of F-19's row cap; it is daemon lifecycle.

**B — The replay window is quantised to the whole minute. (minor, ~2.1pp)**

`b["date"] >= after` compares a bar's **open** timestamp against a detection
timestamp carrying sub-second precision, so the detection's own minute is always
discarded entire: a mean ~30s, and up to 60s, of price action that occurred
*after the setup existed* is never replayed. Two detections seconds apart on
opposite sides of `:00` therefore replay bar sets differing by one whole bar.

This is F-27's own pair. 14-Aug GODREJCP/SDN, ids 6136 and 6155:

```
  id 6136  ts 09:32:46.010 IST  stop 938.12 (farther)  first bar 09:33  -> STOP
  id 6155  ts 09:33:01.426 IST  stop 937.12 (nearer)   first bar 09:34  -> TARGET
```

The 09:33 bar is replayed by one row and not the other, which is the only way
the farther stop can resolve worse. The control group agrees: of 253 pairs where
the nearer stop starts **earlier** (sees more bars), **0** invert; of 507 where
it starts **later**, 28 do.

Mechanism B is real and touches every row in the table, not only duplicated
ones — but it explains only about 2 percentage points of the contradiction rate.
A is the defect that matters.

*Reasoned, not measured:* B should bias gross R **upward**. Discarding the first
partial minute drops the earliest hits, and in these engines the stop sits
nearer than the target (the SDN example above: stop +0.91%, target -1.37%), so
more early stop-outs are dropped than early target hits. Correcting B should
therefore make engines look **worse**, not better. This is geometry, not a
measurement — see §5.

### 3 — WHAT IT CONTAMINATES

**Not `tools/expectancy_ledger.py`.** That reads `closed_positions`
(`expectancy_ledger.py:94,102`), whose intraday rows are PAPER fills written by
the live 15s loop, not by this writer. Its intraday population is small — SDN
17, ORB 11, VWR 10, GAP 9, PDL 7, VCE 7, PBK 5 — and untouched by F-27.

**The six-engine gross-R table F-27 cites is a different artefact**, derived
from `intraday_setups`, and it *is* contaminated. Confirmed by reproducing it:
dedup key `(symbol, strategy, trade_date)`, population = all rows with
`outcome_pct` not null. n matches the published table **exactly on all nine
engines** (SDN 398, VWR 307, VCE 138, ORB 119, RNG 60, PBK 32, PDL 25, GAP 22,
GDB 1 = 1102).

Also reading this table, and so also contaminated: `allocation/scoring.py`
(`intraday_priors`), `allocation/hurdle.py` (the arrival distribution),
`outcomes.engine_scorecard`, `tools/engine_scorecard.py`, `tools/weekly_review.py`,
`tools/discover_engines.py`, `tools/allocator_replay.py`,
`tools/proposal_backtest.py`, `ai/post_trade_analysis.py`.

**Could a verdict flip?** Each self-contradicting group proves at least one
member is wrong and the truth is one of the values observed, so replacing every
member with the group's worst (pessimistic) and best (optimistic) gross figure
brackets that error exactly:

```
  engine  n     as-is          pessimistic     optimistic      contaminated
  VWR    307  -0.335 (-5.1 SE) -0.343 (-5.2)  -0.332 (-5.0)      1.0%
  ORB    119  -0.238 (-2.4 SE) -0.278 (-2.9)  -0.209 (-2.1)     11.8%
  SDN    398  -0.047 (-0.8 SE) -0.058 (-1.0)  -0.037 (-0.6)      5.8%
  VCE    138  -0.261 (-2.6 SE) -0.290 (-2.9)  -0.232 (-2.2)      3.6%
  RNG     60  +0.061 (+0.4 SE) +0.061 (+0.4)  +0.062 (+0.4)      1.7%
  PBK     32  -0.288 (-1.1 SE) -0.288 (-1.1)  -0.288 (-1.1)      0.0%
```

- **VWR survives comfortably.** Worst case -5.0 SE; the 2 SE interval reaches
  only -0.199. "ALREADY NO" holds under any resolution of the observed error.
- **ORB survives by 0.01R.** Its optimistic bound is -0.209 ± 0.0995, so the
  2 SE upper limit is **-0.010** — still excluding positive gross R, by one
  hundredth of an R. ORB is also the **most contaminated engine at 11.8%**. It
  is not robust in any meaningful sense; it is on the right side of the line by
  a rounding error.
- **No verdict flips on the observable error.** §6182's scope decision stands as
  written — but ORB's margin should not be described as settled.

**A separate defect on the same table, found in passing.** The estimator takes
`risk_pct` from `grp[0]` while averaging `outcome_pct` across the whole group
(`tools/engine_scorecard.py:86-104`), and the dedup key
`(symbol, strategy, trade_date)` pools setups at genuinely different price
levels — 14-Aug GODREJCP/SDN is **11 rows with 7 distinct entries in one
group**. The published numbers therefore depend on row order within the group.
Ordering by `id`, VWR (-0.335 vs -0.345), ORB (-0.238 vs -0.241), SDN and PBK
reproduce closely, but **VCE reads -0.261 against a published -0.155, and RNG
reads +0.061 against a published -0.137 — a sign flip.** Not F-27, but it sits
on the same table and moves the same verdicts.

### 4 — RECOMMENDED FIX (NOT IMPLEMENTED)

In priority order. 1 is the one that matters.

1. **Refuse to score a session that is not over.** `resolve_day` should hard-
   refuse unless the day has closed — wall clock past the square-off, or
   `bars[-1]` at/after the close bar — returning the `complete=False,
   reason="session_open"` shape it already uses for `no_broker`. This kills
   mechanism A at the source and needs no schema change.
2. **Record provenance.** Add `resolved_at` (and `resolved_bars_to`) to
   `intraday_setups`. Today, "which run scored this row" is unanswerable; every
   claim in §2 had to be *inferred* from contradictions rather than read. A
   number whose origin cannot be stated cannot be defended.
3. **Make re-resolution possible.** Idempotence currently means "never revisit",
   which is what froze the truncated answers in. Add `--rescore` to recompute
   rows whose `resolved_at` precedes the session close.
4. **Make the window a function of the setup, not the fractional second.** Filter
   on bar *close* (`bar_open + interval > ts`) so the detection minute is
   replayed from the detection instant; or anchor deliberately to the next bar
   open and record which bar was used. Either is defensible. A rule whose answer
   changes with the sub-second component is not.
5. **A check that FAILS on contradiction.** Group resolved setups by
   `(trade_date, symbol, strategy, direction, entry, stop, target)` and assert
   one distinct outcome per group. It fails on today's data (111 groups) —
   demonstrate it failing before trusting it, per CLAUDE.md. Logic check into
   `backend/tests/` + `tools/verify.py::MODULES`; the live-book variant into
   `tools/health.py`.
6. **Separately, fix the dedup estimator** (§3) — narrow the key to include
   entry/stop/target, or carry risk per row. RNG's *sign* currently depends on
   fetch order.

**Do not re-score the book until 1-4 land.** Re-resolving with the current
writer would replace one arbitrary answer with another and destroy the
contradictions that are currently the only evidence the defect exists.

### 5 — NOT DONE / COULD NOT DETERMINE

- **No bar-level confirmation of any single row.** There is no broker session in
  this environment (`kite` returned "Please log in first"), so `_session_bars`
  could not be exercised and no claim here rests on observed OHLC. Mechanism B's
  per-row effect is inferred from the code path and the timestamps; the §2 proof
  of mechanism A does not need bars.
- **Which run scored which row is inferred, never read** — there is no
  `resolved_at`. That is recommendation 2.
- **The optimistic-bias direction of mechanism B is reasoning from stop/target
  geometry, not a measurement.**
- **The unobservable error is not bounded.** §3's bracket covers only rows that
  contradict a surviving twin. Rows with no duplicate, and groups where every
  member was scored in the same truncated pass, carry the same defect and leave
  no trace. **True uncertainty on every engine figure is strictly wider than the
  table shows**, and only a full re-resolution against complete bars measures it.
- **`meta.sub_engine` is NULL in all 8324 rows**, so "same engine" could only be
  tested at family (`strategy`) granularity. CLAUDE.md states `_setup_is_new`
  dedups on `meta.sub_engine`; if that field is genuinely never written, the
  dedup key is degenerate. Not investigated — it is adjacent, not F-27.

**Gate: MEASURED. No fix implemented, no row re-scored, nothing written outside
this ledger.** F-27 is confirmed systemic but re-characterised: the defect is
non-reproducible resolution across runs, of which stop-distance inversion is one
visible symptom.

---

## 2026-08-16 — F-27 mechanism A (change, `resolve_day` session guard) — the scorer had no clock: it is called from the daemon's `finally` block, prices TIMEOUT at whatever bar it was last handed, and never revisits a row it has written. Guard + provenance built, 15 of 18 checks demonstrated failing first. Task 2's config changes are WRITTEN AS MIGRATION 081 AND NOT APPLIED — live database writes were denied to this session

**Ran:**

```bash
git checkout -b fix/resolve-day-session-guard
python -m tools.verify --module resolve_day_session_guard   # x3, see section 3
python -m tools.verify                                      # 562 checks
python -m tools.health                                      # all green
python -m tools.rollback --status
```

No row was re-scored. No row was repaired. Nothing was written to the database.

### 0 — A PREMISE CORRECTION, BEFORE ANYTHING BELOW IS READ

The brief says "follow section 4 of the F-27 entry". **There is no section 4, and
no F-27 entry with sections.** F-27 exists in this ledger only as a 19-line note
inside section 6 (FOUND ALONG THE WAY) of the 2026-08-16 replay-harness entry,
and that note explicitly records the mechanism as **unexplained**:
*"`outcomes.py:210` is the only writer of the column, and its window logic is
unchanged since commit `042217e`, so the mechanism is not yet explained."* The
later entries carry it forward as `F-27 not investigated`.

So the diagnosis in the brief — the `finally` block, `bars[-1]["close"]`, the
never-revisited row, 58 contradictions of which 42 are STOP+TIMEOUT — is **not
in the repository**. It is taken as given from the operator and **verified in
the code below**, which is the half I can check. The 58/42 counts are the
operator's measurement and are quoted, not reproduced: database reads were
available at the start of this session and denied partway through, so no
independent count was possible.

**What this means for the work:** the design decisions below are mine, derived
from the mechanism, not transcribed from a spec. Anywhere a section 4 would
have decided something differently, this entry is the place that record lives.

### 1 — THE MECHANISM, CONFIRMED IN THE CODE

Three properties, each defensible alone:

```
intraday/run.py:416          resolve_day(sb=sb) in the daemon's `finally` block
intraday/outcomes.py:180     outcome, exit_px = "TIMEOUT", float(bars[-1]["close"])
intraday/outcomes.py:100-101 work queue = .eq(trade_date, d).is_("outcome","null")
```

`finally` runs on **every** exit — a clean 15:40 cool-down, yes, but equally a
crash at 10:12, a Ctrl-C at 11:30, a closed laptop, a restart to pick up a
config change. On any of those `historical_data` returns the session **so far**,
every still-open setup is scored TIMEOUT at a mid-morning close, and the third
property makes it permanent: the row is no longer NULL, so the evening
pipeline's `backfill` will not come back for it.

That table is what `scoring.intraday_priors()` and `hurdle`'s arrival
distribution are both built from, so one frozen row prices every candidate that
arrives after it. This is the same class of defect as the priors-built-from-
refused-rows landmine already in `CLAUDE.md`: the learning loop is fed a
population that is an artefact of the system's own operations.

**The `--once` path shares it.** `run.py --once` runs one cycle and breaks into
the same `finally`, so a single diagnostic invocation at any hour would have
scored and frozen the whole outstanding book. Nothing in the tool says so.

### 2 — WHAT WAS BUILT

**`outcomes.session_is_over(trade_date, now=None) -> (ok, why)`.** A past date is
always over — that is the entire `backfill` population and refusing it would
blind the one mechanism that repairs unscored sessions. A future date never is,
which is a real case: a host whose clock or timezone is behind would otherwise
score a day whose bars do not exist and write TIMEOUT across the whole book.
Today's date is over at `MARKET_CLOSE` plus a settling buffer.

**The bar reads `intraday/config`'s `MARKET_CLOSE` and `COOLDOWN_TO`, not copies
of them,** because of the case that decides whether this fix is inert or
harmful. The daemon leaves its loop when `is_trading_session()` goes false, at
`COOLDOWN_TO` = 15:40, and calls `resolve_day` on the way out. A bar at or past
15:40 would mean **the daemon can never score its own session again** and every
day silently waits for the next evening's pipeline — a guard that cannot pass,
which this project has already paid for twice. `outcomes_close_buffer_min`
defaults to 5 (bar at 15:35, five minutes of headroom) and is **clamped to 9
with a WARNING** if set higher, rather than being allowed to disable the path it
exists to protect.

**Provenance (migration 080):**

| column | what it answers |
|---|---|
| `scored_at` | WHEN — separates a re-score from the original write |
| `scored_by` | WHICH RUN — `lease.instance_id()`, host-pid-uuid |
| `scored_through` | THROUGH WHICH BAR — the last bar in the window that priced it |

`scored_through` is the one that matters. A TIMEOUT is priced at that bar's
close, so a TIMEOUT whose `scored_through` reads 11:30 on a session that ran to
15:30 **is** a frozen row — findable in one query instead of by reasoning about
which daemon died when. `scored_by` is the process id and not the hostname
because F-5 and R-1 both established that two daemons run on one machine here,
and a hostname cannot separate them.

**No row is re-scored or repaired, by instruction and on merit.** The
contradictory pairs are the only evidence the defect exists and the population
that will measure whether the guard worked. NULL provenance on an existing row
is itself the marker for "scored before 16-Aug-2026, window end unknown".

### 3 — EACH CHECK FAILED FIRST, AND ONE CHECK CHANGED THE DESIGN

18 checks written before the implementation, run against unchanged code:

```
  15 of 18 checks FAILED.
```

The three that passed are the two fake-vacuity guards and
`backfill still scores every past session` — the regression guard, which must
pass before and after or it is not testing what it claims.

**Then `tools.health` rejected the first implementation, and it was right.** The
schema probe was a one-off `select("scored_at,scored_by,scored_through")`, which
turned the `selects` check RED:

```
X  selects   intraday\outcomes.py:171 — intraday_setups has no column(s):
             scored_at, scored_by, scored_through
```

That is `tools/validate_selects.py` doing exactly its job — and a health check
that is red for a known pending migration is how a real warning stops being
read. Three ways to ask the schema question and only one is free of its own
defect:

```
  strip-and-retry (the position_lifecycle idiom)  3 failed round trips PER ROW
                                                   = ~6,900 on a 2,289-row day
  a one-off probe select                          1 call, but names columns that
                                                   do not exist in a SELECT list
  read the KEYS of a work-queue row               ZERO calls
```

The work queue is already `.select("*")`, so every column of every row is in
hand and `set(rows[0])` answers it for free. `selects` is green again.
`test_the_work_queue_still_selects_star` pins the property that makes it
correct, because narrowing that select would switch provenance off silently.

Final: **21 checks, `tools.verify` all 562 passed across 61 modules** (was 541),
`tools.health` all green.

### 4 — TASK 2: BASELINE RECORDED, MIGRATION WRITTEN, **NOT APPLIED**

`python -m tools.rollback --status` — all four Phase 4 switches ON
(`alloc_live_swing`, `alloc_live_intraday`, `alloc_shadow_enabled`,
`storage_rolloff_enabled`); it reports those four only.

**Values before, read directly:**

```
  intraday_engine_vwr_lifecycle    ACTIVE      intraday_min_confidence         0.55
  intraday_engine_orb_lifecycle    ACTIVE      intraday_min_confidence_scarce  0.80
  overlay_liquidity_enabled        true  <-- ALREADY ARMED, see section 5
  risk_regime_scales_target        ABSENT FROM system_config  <-- see section 5
  all nine engines                 ACTIVE
```

**The comparison baseline — last 5 sessions (10,11,12,13,14-Aug), 6,179
detections:**

```
  BLOCKED_SHORTS_MARKET  1343  21.7%    VETOED_AI               589   9.5%
  REJECTED_COST           847  13.7%    BLOCKED_STRUCTURE       418   6.8%
  TAKEN                   816  13.2%    BLOCKED_SHORTABILITY    253   4.1%
  BELOW_CONVICTION        809  13.1%    BLOCKED_SHORTS_OFF      138   2.2%
  ALLOCATOR_DECLINED      748  12.1%    BLOCKED_REENTRY          66   1.1%
                                        BLOCKED_EVENT            61   1.0%
                                        BLOCKED_CROSS_FRAMEWORK  53   0.9%
                                        BLOCKED_PAPER_CAPACITY   24   0.4%
                                        BLOCKED_ENTRY_RESERVED   14   0.2%

  entries/session   14-Aug 334 - 13-Aug 93 - 12-Aug 108 - 11-Aug 4 - 10-Aug 277
  by engine (detections/taken)
    ORB 1189/572 - SDN 3837/104 - PBK 334/23 - VWR 279/40 - VCE 230/71
    RNG 160/0 - PDL 98/0 - GAP 51/6 - GDB 1/0
```

**ORB is 572 of 816 entries — 70.1%.** With VWR's 40 that is **three quarters of
the intraday book's entries** being withdrawn. The book is PAPER so it costs no
money; it costs detections-that-become-trades, and the histogram above is how
that is measured rather than assumed.

**Migration 081 was written with all four changes and the reasoning behind each,
and it is NOT APPLIED.** Live database writes were denied to this session
partway through — the DDL for migration 080 first, then read access as well. The
repo has no migration runner (both 077 and 079 are also written-and-unapplied on
`main`), so a written migration is the normal completed form of a config change
here; but **nothing in section 4 or 5 is in force on the book.**

Recorded in 081, so the reasoning is not lost:

- **VWR to SHADOW is settled.** 307 detections, gross -0.345R +/- 0.071 SE,
  -4.9 SE from zero, optimistic 2 SE upper limit -0.199 — still negative. No
  reading of this sample makes VWR positive-expectancy.
- **ORB to SHADOW is NOT settled, and that difference must survive the two
  engines sharing a state.** 119 detections, -0.241R +/- 0.100, optimistic upper
  limit **-0.010** — it survives the bound by one hundredth of an R, inside the
  width of every measurement error this book has. And ORB is the **most
  contaminated engine at 11.8%**, the highest share of rows carrying the very
  defect section 1 stops. So: *VWR answered NO with room to spare; ORB answered
  NO by 0.01R on the dirtiest data in the book.* Provisional stand-down.
  **Revisit on sessions scored entirely after migration 080**, identifiable by
  `scored_through` landing at the session close. Neither is RETIRED, because
  retiring destroys the evidence that would reverse it.
- **The conviction floor is flattened by setting `scarce` equal to `base`
  (0.55).** `engine._confidence_floor()` returns `base + (scarce-base)*used`, so
  the bar rises 0.55 -> 0.80 as the entry budget is spent, on the premise that a
  scarcer slot deserves a more convinced setup. Gross R falls **monotonically**
  across the top four confidence bands (-0.097 -> -0.400), so conviction does not
  merely fail to order outcomes, it orders them backwards — and a rising floor
  spends the last slots of the day on the worst population. Setting the two keys
  equal zeroes the linear term with **no code change**, and leaves re-arming the
  ramp one UPDATE away. 0.55 is the current base, unchanged: this removes a
  slope, it does not lower a bar.
- **`risk_regime_scales_target` to true** — the only item that touches the LIVE
  swing book. Migration 079 shipped it inert and said arming it "changes what
  the account does with money and is a separate decision on separate evidence".
  This is that decision. Effect in NEUTRAL — the only regime all 1,000 plans in
  the 28-Jul->13-Aug window have ever read — is 1.9048R -> 2.0R, **+5.0% on
  planned R, ATR-stop plans only, no stop moves.**

### 5 — FOUND ALONG THE WAY

**`overlay_liquidity_enabled` was ALREADY `true`.** It has been since migration
040 (05-Aug). There was nothing to arm. Recorded because "we armed it" and "it
was already armed" are different facts and only one is true; migration 081
carries a no-op UPSERT that states the intended value rather than leaving it
assumed.

**Migration 079 is entirely unapplied on this book.** All six of its keys —
`risk_regime_scales_target`, `risk_min_planned_r_enabled`, `risk_plan_hit_rate`,
`risk_plan_r_margin`, `risk_plan_product`, `risk_plan_capital` — are absent from
`system_config`. `risk_model.py` reads each through `cfg_*` with an in-code
default, so behaviour today is **identical to 079 having been applied and left
inert**, and nothing is broken. But it means arming `risk_regime_scales_target`
is an INSERT, not an UPDATE, and 081 does that. 079 uses `ON CONFLICT DO
NOTHING`, so applying it afterwards will not reset the armed value to false.

**`intraday_max_new_per_day` is 20, not the 4 its in-code fallback assumes.**
`_entry_cap()` falls back to 4 and `_confidence_floor()` divides by it, so on any
book where that key were absent the floor would reach `scarce` after four
entries instead of twenty — a fifth of the way through the budget. Not a defect
today (the key is present at 20), and untouched here. Noted because it is the
same shape as the `_entry_cap` bug already documented in that function's own
docstring.

### 6 — NOT DONE

- **Migrations 080 and 081 are NOT APPLIED.** The guard in section 2 is live in
  code the moment this branch is deployed and needs no migration; **provenance
  records nothing until 080 is applied**, and every config change in section 4 is
  inert until 081 is. Both were written to be applied by hand in the Supabase
  SQL editor, which is this repo's only mechanism.
- **No existing row was re-scored, repaired, or counted.** By instruction, and
  because those rows are the measurement.
- **The 58/42 contradiction counts were not independently reproduced.** Quoted
  from the operator's measurement; database access was withdrawn before a
  verifying query could be run.
- **`run.py --once` still routes through the same `finally`.** Harmless now that
  the guard refuses an open session, but the tool's own help does not say it
  attempts to score the day.

### 7 — COULD NOT DETERMINE

- **Whether any session other than 2026-08-14 carries frozen rows, and how
  many.** It needs a per-session query the session no longer has access to. The
  guard stops new ones regardless; `scored_through` is what will make the
  question answerable in one query from the next scored session onward.
- **Whether the 42 STOP+TIMEOUT pairs are all mechanism A.** A STOP is priced at
  the stop, not at `bars[-1]`, so a truncated window explains the TIMEOUT half
  cleanly and the STOP half only if the two rows were scored by different runs.
  Provenance answers this prospectively and cannot answer it retrospectively.
- **Whether ORB's 11.8% contamination rate is the reason its interval reaches
  -0.010 rather than clearly negative.** That is precisely the question a clean
  window answers and no current window can.

**Recommends:** apply 080 before 081. The provenance columns are what make the
ORB decision reversible on evidence, and shadowing 70% of the book's entries
without them means the next session inherits the same unanswerable question this
one did. After the first session scored entirely under the guard, the single
query worth running is `scored_through` versus the session close on every
TIMEOUT — that is the check that this fix worked, and it did not exist before
today.

**Gate: TASK 1 COMPLETE AND COMMITTED. TASK 2 WRITTEN, NOT IN FORCE.** The guard
and its provenance are in code with 21 checks, 15 of 18 demonstrated failing
first, `tools.verify` 562 across 61 modules and `tools.health` all green. No
outcome row was touched. The five config changes exist only as migration 081 on
this branch and change nothing until an operator applies it.

---

## 2026-08-16 — F-28 (change, dedup estimator risk basis) — the defect is real and indefensible (42.9% of multi-row groups scored outside their own members' range) but it is NOT what moved VCE and RNG off the published table: that is a REPRESENTATION choice, first-detection vs group-mean, and it survives the fix. No engine verdict changes. VWR survives at −4.70 SE

**Ran:**

```bash
git checkout -b fix/dedup-estimator main
cd backend && python -m tools.verify --module gated_priors     # before, and after
cd backend && python -m tools.verify                           # full suite
cd backend && python -m tools.simulate
# read-only scratch, all four import the REAL functions, none writes:
python <scratch>/rederive.py          # old vs corrected, nine engines
python <scratch>/constructions.py     # four ways to represent a group
python <scratch>/production_view.py   # the live-config prior, plus tails
python <scratch>/outside_hull.py      # per-group error of the old form
```

The "old" estimator throughout is `git show main:backend/allocation/scoring.py`
loaded as a module — never a retyped copy, because a retyped copy is exactly the
thing that would not reproduce a defect about which row a denominator came from.

---

### 1 — THE MECHANISM, CONFIRMED

`backend/allocation/scoring.py::_intraday_priors_from_rows`. The dedup block
collapsed a (symbol, strategy, trade_date) group by averaging `outcome_pct`
across its members and writing that one mean onto `base = dict(src[0])`. The
loop below then divided it by `base`'s risk — **`src[0]`'s entry and stop**, i.e.
whichever row `fetch_all(order_by="id")` sorted first.

Numerator from every row in the group. Denominator from one of them.

A group cannot be one price level, **by the mechanism that creates it**:
`_setup_is_new` re-records a setup precisely WHEN its entry has drifted past
`intraday_setup_dedup_pct`. Drift is the admission criterion, so every multi-row
group holds multiple entries by construction. GODREJCP/SDN on 2026-08-14:

```
     id     entry      stop   risk%     out%   cost%        R  verdict
   6136    935.80    938.12   0.248   -0.248   0.000  -1.0003  BLOCKED_SHORTS_MARKET
   6155    935.80    937.12   0.141    0.168   0.206  +2.6536  REJECTED_COST
   6279    936.00    937.72   0.184    0.395   0.000  +2.1495  BLOCKED_SHORTS_MARKET
   6430    932.50    938.12   0.603   -0.603   0.000  -1.0005  BLOCKED_SHORTS_MARKET
   6529    932.60    934.32   0.184   -0.184   0.000  -0.9977  BELOW_CONVICTION
   6561    932.10    934.32   0.238   -0.444   0.206  -0.9980  REJECTED_COST
   6599    931.00    934.32   0.357   -0.357   0.000  -1.0011  BLOCKED_SHORTS_MARKET
   6980    933.00    934.52   0.163   -0.369   0.206  -0.9987  REJECTED_COST
   7049    933.00    934.62   0.174   -0.174   0.000  -1.0021  BLOCKED_SHORTS_MARKET
   7074    933.00    934.62   0.174   -0.380   0.206  -1.0004  REJECTED_COST
distinct entries: 7      risk spread: 0.141% .. 0.603%  (4.3x)
```

**Correction to the flag as filed:** the group is **10 rows, not 11** — 10 total
in `intraday_setups`, all resolved. 7 distinct entries as stated. The old form
applied 0.248% (row 6136's risk) to all ten outcomes and reported −0.553R; the
mean of the members' own R is −0.320R.

R is a **ratio**. The mean of ratios is not the ratio of means unless every
denominator is equal, which is the one thing this grouping guarantees is false.

### 2 — HOW WRONG, PER GROUP (the aggregate hides it)

Per-engine means barely move (§4), which invites the conclusion that this was
cosmetic. It was not. The old per-group arithmetic was replicated in three lines
and **validated against the old module first** — all nine engine means matched to
`delta 0.00e+00` — before anything below was concluded from it.

```
multi-row groups: 664
  old estimate OUTSIDE its own members' [min,max]: 285 (42.9%)
  every member a stop-out (~-1R) yet old scored below -1.02R: 73

worst 8 by distance outside the hull:
  symbol      eng  date              OLD  members min     max  true mean  rows
  CARBORUNIV  SDN  2026-08-13     -2.641       -1.002   2.865     -0.773    17
  HSCL        PBK  2026-08-04     -2.062       -1.002  -1.000     -1.001     2
  MUTHOOTFIN  VWR  2026-08-13     -1.990       -1.002  -0.999     -1.000     6
  CHENNPETRO  PBK  2026-07-31     +4.976        2.500   4.079     +3.290     2
  CEMPRO      SDN  2026-08-11     -1.724       -1.002  -0.999     -1.000    10
  HINDZINC    VWR  2026-08-14     -1.683       -1.001  -0.997     -1.000     5
  RADICO      SDN  2026-08-14     -0.328       -1.002  -0.998     -1.000    17
  PNBHOUSING  SDN  2026-08-07     -1.636       -1.002  -0.999     -1.000     9
```

**73 groups in which every member row was a plain stop-out — R ≈ −1.000 for all
of them — were scored below −1.02R.** HSCL/PBK: two rows, −1.002 and −1.000, and
the estimator said the setup lost **−2.062R**. That is not an imprecision; it is
a number the trade cannot produce, and the reason it appears is that a stop-out's
R is exactly −1 *at its own risk*, so the moment the denominator comes from a
different row the identity breaks. RADICO/SDN is the same error facing the other
way: 17 rows all ≈ −1.0, scored **−0.328R**, a total loss recorded as a third of
one.

The aggregate survives this because the errors are near-symmetric across 285
groups and cancel. That is luck, not design.

### 3 — THE FIX

Each row is reduced to R against **its own** entry and stop first; the group mean
is taken of those. `_row_gross_r()` is now the single home of that arithmetic —
the pre-fix code had the formula written out twice, once in the dedup block and
once in the loop, which is how a numerator and a denominator came from different
rows without either line looking wrong on its own.

Tests were written first and **three of the four fail on the unfixed estimator:**

```
✗  gated intraday priors  (3/14 failed)
   mean_r -0.2500 — expected +2.0R, the mean of each row's own R. -0.25 means
     the group's outcomes were divided by the FIRST row's risk
   the same three rows gave ['-0.0833', '-0.8333', '-0.3333'] depending on
     which one came first
   mean_r +1.500000 — both rows are +1.0R gross at their own levels; anything
     else means cost or risk crossed rows
```

The middle one is the property that matters most: **the same three rows produced
three different priors depending on which arrived first.** An estimator whose
answer moves when nothing about the evidence moves is not measuring the evidence.

The fourth check ("a group at one price level is unchanged") passes on BOTH
sides by design — it pins the no-op. Per this project's rule that a check which
cannot fail is not a check, it was demonstrated failing against a deliberately
wrong central-tendency choice (median-of-group instead of mean): `mean_r
+0.500000`, caught. It is the only one of the four that catches that class.

After: `all 545 checks passed across 60 modules`. `tools.simulate` runs clean.

### 4 — RE-DERIVED GROSS R, NINE ENGINES, OLD vs CORRECTED

Complete resolved population, 8324 rows → 1102 dedup keys. `taken_only=false`,
floor 1, so every engine returns a real number and the population matches the
15-Aug §3 table. **n matches §3 exactly for all nine engines.**

```
engine      n   published           OLD estimator           NEW estimator      shift
------------------------------------------------------------------------------------
SDN       398      -0.077       -0.0471 +/- 0.0580        -0.0584 +/- 0.0594    -0.0113
VWR       307      -0.345       -0.3279 +/- 0.0656        -0.3134 +/- 0.0667    +0.0144
VCE       138      -0.155       -0.2522 +/- 0.1054        -0.2511 +/- 0.1053    +0.0010
ORB       119      -0.241       -0.2654 +/- 0.0967        -0.2925 +/- 0.0951    -0.0271
RNG        60      -0.137       +0.0549 +/- 0.1574        +0.0467 +/- 0.1613    -0.0082
PBK        32      -0.276       -0.3063 +/- 0.2655        -0.3230 +/- 0.2291    -0.0168
PDL        25      +0.064       +0.0981 +/- 0.3295        +0.0733 +/- 0.3270    -0.0248
GAP        22      +0.100       +0.0762 +/- 0.2533        +0.0716 +/- 0.2558    -0.0046
GDB         1      -1.000       -1.0003 +/- nan           -1.0003 +/- nan       +0.0000

OLD  INTRADAY/ALL   n=704  -0.2421 +/- 0.0452     NEW  INTRADAY/ALL   n=704  -0.2426 +/- 0.0451
OLD  ALL/SHORT      n=398  -0.0471 +/- 0.0580     NEW  ALL/SHORT      n=398  -0.0584 +/- 0.0594
```

Verdicts at the 2-SE bar:

```
engine    OLD SE from 0   NEW SE from 0   verdict
SDN               -0.81           -0.98   open  -> open
VWR               -5.00           -4.70   NO    -> NO
VCE               -2.39           -2.38   NO    -> NO
ORB               -2.75           -3.08   NO    -> NO
RNG               +0.35           +0.29   open  -> open
PBK               -1.15           -1.41   open  -> open
PDL               +0.30           +0.22   open  -> open
GAP               +0.30           +0.28   open  -> open
```

**NO VERDICT CHANGES. Not one, on any engine, at any n.** Largest shift is ORB at
−0.027R. The production-configured prior (`taken_only=true`, floor 30, 90-day
window) — the number that actually prices a trade — moves no further:

```
key                      n   OLD mean   NEW mean    shift  usable
INTRADAY/ALL           169    -0.2305    -0.2401  -0.0097  True
INTRADAY/ALL/SHORT      42    +0.1181    +0.1175  -0.0007  True
INTRADAY/ORB            49    -0.2806    -0.3065  -0.0259  True
INTRADAY/PBK            32    -0.3063    -0.3230  -0.0168  True
INTRADAY/RNG            60    +0.0549    +0.0467  -0.0082  True
INTRADAY/SDN/SHORT      42    +0.1181    +0.1175  -0.0007  True
INTRADAY/VCE            37    -0.1770    -0.1799  -0.0028  True
INTRADAY/VWR            57    -0.2943    -0.2900  +0.0043  True
INTRADAY/GAP,GDB,PDL          below floor, NEUTRAL, unchanged
```

### 5 — THE FLAG'S DIAGNOSIS WAS WRONG, AND THE REAL CAUSE IS A DECISION

The flag attributed the VCE and RNG divergence from the published table to this
defect. **It is not the cause.** Fixing it moved VCE by +0.001 and RNG by −0.008;
the divergence is fully intact afterwards. VCE reads −0.251 against a published
−0.155, RNG reads **+0.047 against −0.137, still a sign flip, after the fix.**

The cause is that §3 and the estimator **represent a group differently**, and
both were correct in their own terms. Four representations, one population:

```
=== TAKEN-preferred (the estimator's rule) ===
engine     n  published   first-ts   first-id     mean-R    last-ts
SDN      398     -0.077    -0.0926    -0.0926    -0.0584    -0.1442
VWR      307     -0.345    -0.3491    -0.3491    -0.3134    -0.2814
VCE      138     -0.155    -0.1803    -0.1803    -0.2511    -0.3767
ORB      119     -0.241    -0.2498    -0.2498    -0.2925    -0.3339
RNG       60     -0.137    -0.1366    -0.1366    +0.0467    +0.1914
PBK       32     -0.276    -0.3234    -0.3234    -0.3230    -0.3455
PDL       25     +0.064    +0.0635    +0.0635    +0.0733    +0.0886
GAP       22     +0.100    +0.0958    +0.0958    +0.0716    +0.1187
GDB        1     -1.000    -1.0003    -1.0003    -1.0003    -1.0003

=== no TAKEN preference ===
SDN      398     -0.077    -0.0767    -0.0767    -0.0654    -0.1687
VWR      307     -0.345    -0.3456    -0.3456    -0.3024    -0.2585
VCE      138     -0.155    -0.1550    -0.1550    -0.2678    -0.3564
ORB      119     -0.241    -0.2413    -0.2413    -0.2627    -0.2474
RNG       60     -0.137    -0.1366    -0.1366    +0.0467    +0.1914
PBK       32     -0.276    -0.2758    -0.2758    -0.3039    -0.3498
PDL       25     +0.064    +0.0635    +0.0635    +0.0748    +0.0993
GAP       22     +0.100    +0.1001    +0.1001    +0.0710    +0.1188
GDB        1     -1.000    -1.0003    -1.0003    -1.0003    -1.0003
```

**`first-ts` with no TAKEN preference reproduces the published table on all nine
engines** — VCE −0.1550 vs −0.155, RNG −0.1366 vs −0.137, VWR −0.3456 vs −0.345,
GAP +0.1001 vs +0.100. That is §3's stated rule (`dedupe_setups`: first detection
by `ts`) confirmed, and it is also an independent validation of the corrected
`_row_gross_r`: the same per-row arithmetic reproduces a table computed by
different code in a different session to four decimals.

Decomposing RNG's −0.137 → +0.047:

- TAKEN preference: **0.000** (RNG is identical in both halves)
- first-ts → group-mean: **+0.183, the entire gap**

VCE's −0.155 → −0.251: −0.025 from TAKEN preference, −0.071 from first-ts →
group-mean, −0.001 from this fix.

`first-ts` and `first-id` are identical on every engine, so sorting by `id` was
never the issue — id order and ts order agree on which row is first. The issue is
that the old code used the first row for the **denominator only**.

**The drift across the columns is monotone in every engine** — ts → mean → last
walks one direction, and for RNG it walks from −0.137 to +0.191. Which row of a
group you believe is worth up to 0.33R on a 60-key engine. That is a live,
undecided estimator question and it is bigger than the defect this entry fixes.

### 6 — DOES VWR STILL SURVIVE AT −4.9 SE?

**Yes — stated explicitly as asked. VWR remains decisively negative and its NO
verdict is unchanged.**

```
construction                              mean      SE      SE from 0
published §3 (first-ts, no TAKEN pref)   -0.345   0.071      -4.86
corrected estimator, complete pop        -0.3134  0.0667     -4.70
old estimator, complete pop              -0.3279  0.0656     -5.00
corrected, production config (n=57)      -0.2900     --        --
```

The published "−4.9 SE" is −4.86 on its own construction. The corrected estimator
reads **−4.70 SE (n=307)**. It weakened by 0.16 SE and did not come close to the
−2 bar; the 2-SE optimistic upper limit is −0.180, still negative. **All three
constructions and both estimators agree VWR is negative — it is the only engine
about which no representation choice makes any difference.**

### 7 — TAILS: THE GROUP MEAN DAMPENS, IT DOES NOT AMPLIFY

A member row's R can be extreme when a late re-record leaves a very tight stop
(GODREJCP holds a +2.65R row off 0.141% risk). Averaging could have imported
those into the prior. Measured, it does the opposite:

```
member rows 8324 in 1102 groups; median group size 3, max 258
  per member row   min -1.00  p10 -1.00  med -1.00  p90 +2.00  max +10.35  |R|>3: 250
  per group mean   min -1.00  p10 -1.00  med -0.94  p90 +1.73  max  +6.30  |R|>3: 19
  groups with >1 member: 729 of 1102 (66.2%)
```

250 extreme member rows collapse to 19 extreme group means. **66.2% of groups are
multi-row, so the defect touched two thirds of the population** and still moved
no engine mean by more than 0.027R.

### 8 — COULD NOT DETERMINE

- **Which representation is CORRECT is not decided here, and this change does not
  decide it.** The remit was to make `risk_pct` match the row being scored. It
  now does, under the group-mean representation that was already in place. Whether
  a prior should represent a setup by its first detection, its taken row, or the
  mean of its restatements is a separate question worth up to 0.33R on RNG, and
  changing it silently inside a defect fix is exactly the kind of move this ledger
  exists to prevent.
- **VCE's verdict is representation-dependent and that is unresolved.** Published
  −0.155 ± 0.118 is −1.31 SE → *open*. The estimator, old and new alike, is
  −2.38 SE → *NO*. The engine's answer depends on a choice nobody has made
  deliberately. RNG has the same exposure with the sign attached: −0.137 (open,
  negative) vs +0.047 (open, positive).
- **Why the ts → mean → last drift is monotone was not established.** It is
  consistent with later re-records chasing price, but that is a hypothesis; no
  test here separates it from a resolution artefact in how a re-recorded row's
  outcome is computed against its own later entry.
- **The SE under group-mean is not corrected for within-group correlation.** A
  group's members are restatements of one setup, so its mean has less independent
  information than n=1 of a fresh observation implies in one direction and more
  than a single row implies in the other. Every SE above is the plain
  `stdev/sqrt(n)` over group means. Unquantified.
- **No claim is made about the 2026-08-14 / 08-13 unscored rows.** `tools.verify`
  reports 1329 detections across 2 past sessions never scored; they were not
  touched, and if they are later resolved every number in §4 moves.
- **The production-config figures use the live 90-day window**, which currently
  contains the whole table (8324 rows). If `priors_intraday_lookback_days` ever
  bites, §4's bottom block and §4's top block diverge.

### 9 — NOT DONE, DELIBERATELY

**No `intraday_setups` row was re-scored, repaired, or written.** No migration.
No config change. This is an estimator fix and nothing else, per the brief. The
four scratch scripts are read-only and live outside the repo.

**Recommends:**

1. **Land the fix.** Order-dependence is indefensible on its own terms regardless
   of how little it moves the aggregate, and 73 groups scored below the worst
   outcome a trade can produce is a defect of kind, not of degree.
2. **Do not restate any engine verdict on the strength of this change.** Nothing
   moved. The 15-Aug §3 conclusions stand: VWR NO, ORB NO, and the rest open or
   under n.
3. **Open a separate decision on group representation** — first detection vs
   TAKEN row vs group mean. It is worth more than this fix was, it decides VCE's
   verdict, and it flips RNG's sign. It should be decided on what the prior is
   FOR ("if I take this engine's next setup, what R should I expect?") rather
   than on which number looks better.
4. **Note for the merge:** this branch is off `main`, so the F-27 entry from
   `fix/resolve-day-session-guard` is not in this tree. Both branches append to
   `docs/FINDINGS.md` and will conflict at the tail; both entries are keepers and
   the resolution is to keep both in date order, never to drop one.

**Gate: PASS on the estimator fix — 545 verify checks green, three of four new
checks demonstrated failing first, no verdict changed. NEEDS DECISION on group
representation (§5, §8), which is a bigger lever than the defect and is
deliberately left untouched.**

---

## 2026-08-16 — F-29 (diagnostic, group representation) — a group is NOT one opportunity observed many times: 50.9% of multi-row groups contain more than one OUTCOME, and 111 of them stop out first and hit target later on a re-entry the live config REFUSES. First detection is the correct representation. One verdict changes — VCE reverts to open. VWR NO, ORB NO, both under every representation. RNG's sign flip does not survive

**READ-ONLY. No estimator, no config, no database row was changed.**
`git status` clean, `python -m tools.verify` → **all 545 checks passed across 60
modules**, unchanged from `main`.

**Ran:**

```bash
git checkout -b diagnostic/group-representation main
cd backend && python -m tools.verify                  # 545/545, tree untouched
# read-only scratch, all four import the REAL allocation.scoring._row_gross_r:
python <scratch>/fetch_pop.py     # 8324 resolved rows, 8324 distinct ids, cached
python <scratch>/represent.py     # four representations x nine engines + 2 controls
python <scratch>/anatomy.py       # is a group one opportunity? group size, rank, RNG
python <scratch>/lookahead.py     # entry drift, winner/loser asymmetry, span
python <scratch>/decide.py        # outcome-mixing counts, final verdict table
python <scratch>/production.py    # what it does to the LIVE prior
```

Population identical to F-28: **8324 resolved rows → 1102 dedup keys**, `n`
matching the 15-Aug §3 table on all nine engines.

---

### 1 — TWO CONTROLS FIRST, BECAUSE EVERYTHING BELOW DEPENDS ON THEM

Neither table below is retyped. `_row_gross_r` is imported from
`allocation/scoring.py`, and the two constructions are asserted against tables
computed by *different code in different sessions* before anything is concluded.

```
CONTROL 1 — does first-ts reproduce the PUBLISHED §3 table?
  SDN -0.0767 vs -0.077   VWR -0.3456 vs -0.345   VCE -0.1550 vs -0.155
  ORB -0.2413 vs -0.241   RNG -0.1366 vs -0.137   PBK -0.2758 vs -0.276
  PDL +0.0635 vs +0.064   GAP +0.1001 vs +0.100   GDB -1.0003 vs -1.000
  max |delta| = 0.0006  (3dp rounding)                        REPRODUCED

CONTROL 2 — does the shipped estimator reproduce F-28 §4 "NEW"?
  max |delta| = 0.0000 on all nine engines                    REPRODUCED
```

Also re-asserted rather than inherited from F-28: the two consumers group on the
same key (`weekly_review.dedupe_setups` uses `(trade_date, symbol, strategy)`,
`scoring._intraday_priors_from_rows` uses `(symbol, strategy, trade_date)` —
1102 either way), and **first-by-ts equals first-by-id in 0 of 1102 groups'
disagreement**, i.e. they never disagree.

---

### 2 — THE PREMISE IS FALSE, AND THE DATA SAYS SO DIRECTLY

The brief's framing — *"a group is one opportunity observed many times"* — is
the intent of `_setup_is_new`. It is not what the table contains.

**If the members of a group were repeated readings of one opportunity, they
would share an outcome. They do not.**

```
multi-row groups                                        729 of 1102 (66.2%)
  groups containing MORE THAN ONE distinct outcome      371  (50.9%)
  first row STOP,   a later row TARGET                  111
  first row TARGET, a later row STOP                     81
group span, first row to last:  median 98.1 min   p90 270.3 min   max 303.4 min
  groups spanning > 60 min                              459
```

A 303-minute group covers the entire session of a book that is flat by 15:15.
These are not fifty readings of one 09:30 breakout; they are **every level the
engine proposed on that name all day**, and half of them resolved differently
from each other because they *are* different trades at different prices with
different stops.

The worked example is unambiguous:

```
ICICIGI  SDN  2026-08-06   15 rows
  outcomes: STOP STOP STOP STOP TARG TARG TARG TARG TARG TARG TARG TARG TARG TARG TARG
  first row R  -1.000   ->   GROUP MEAN R  +3.046

BLUESTARCO RNG 2026-08-10   2 rows   STOP, TARG      -0.999  ->  +2.675
HEROMOTOCO SDN 2026-08-13  12 rows   STOP...TARG     -1.000  ->  +2.511
```

The system shorted ICICIGI, **was stopped out, and was flat at −1R**. The
estimator records that setup as **+3.046R**, on the strength of eleven later
re-entries that never happened.

### 3 — AND THEY ARE RE-ENTRIES THE LIVE CONFIG EXPLICITLY REFUSES

This is not a statistical preference. `intraday/engine.py:2888`:

```python
if cfg_bool("intraday_block_reentry_after_loss", True) \
        and sym in self._failed_today():
    self._record_setup(best, st.phase, 0.0, "BLOCKED_REENTRY", 0, ...)
```

`_failed_today`'s own docstring records why it exists: ACMESOLAR stopped out for
−1.35R and re-bought two minutes later at the same conviction, ZEEL invalidated
and re-entered, seven entries against a cap of five, 31-Jul. Its conclusion is
the exact sentence this diagnostic needs — *a level that has already failed once
today is not the same setup at a discount.*

**The group mean prices every engine as though that switch were off.** Over the
111 STOP-then-TARGET groups:

```
                     first-ts    grp-mean     swing
STOP-then-TARGET      -1.0001     +0.2854   +1.2855R  over 111 groups
TARGET-then-STOP      +2.1688     +0.6172   -1.5516R  over  81 groups
```

### 4 — THE ASYMMETRY: IT DEFLATES WINNERS AND INFLATES LOSERS

Measured over all 729 multi-row groups, `grp-mean − firstR`:

```
group's first row WON  (R>0)   n=223   -0.7854R   (160 deflated, 43 inflated)
group's first row LOST (R<=0)  n=506   +0.3647R   (184 inflated, 36 deflated)
```

The mechanism, measured: when the setup worked, later re-records **chase** — by
the last row the entry has moved −0.4540% *against* the trade (median −0.3482%),
because price has already run. When it failed, the entry barely moves (−0.0281%).

So the group mean shrinks every group toward the middle, and it does so by an
amount that depends on **what price did afterwards**. That is precisely the
dispersion an edge test exists to measure, removed by the estimator that is
supposed to measure it. `VCE/PINELABS 10-Aug`: a setup that hit target at
**+2.491R** is recorded as **−0.751R**. `RNG/BLUESTARCO`: a stop-out at −0.999R
is recorded as **+2.675R**.

Group size is itself an outcome, which is why this cannot be waved off as noise
that cancels:

```
first-row R by group size:  1 row -0.2433 | 2-3 -0.2508 | 4-9 -0.2801 | 10+ +0.0767
```

A setup that dies on the first tick writes one row. One that runs writes 258.
**The number of members the mean is taken over is chosen by the outcome.**

### 5 — ANSWER 1: WHICH REPRESENTATION IS CORRECT

**The FIRST DETECTION by `ts`.** Not because it is the published rule — because
it is the only row the system could have acted on.

- The engine proposes an entry and a stop at a moment. If the book trades that
  engine, the order goes in **then**. There is no mechanism by which one setup
  becomes a position at ten prices.
- Every later row is one of two things, and **neither is the setup being
  judged**: a restatement while the money is already committed, or a fresh
  attempt at a level that already failed — which `intraday_block_reentry_after_
  loss` refuses outright.
- Confirmed against real executions: of the 211 groups that contain a `TAKEN`
  row, **the TAKEN row is the group's first row in 134**. Where it is not, the
  "earliest TAKEN row, else first" representation gives the same verdict as
  first-ts on **every engine** (§6), so the recommendation does not rest on the
  choice between them.

The one honest argument for the group mean is variance reduction — averaging
repeated noisy readings of one number. **That argument requires the members to
be readings of one number, and §2 disproves it at 50.9%.** Averaging different
trades is not variance reduction; it is a portfolio the system is forbidden to
hold. And it buys no sample: **n is 1102 under both representations.** The group
mean does not add observations, it only changes what each one is worth.

**Correction to a claim this repo relies on.** `dedupe_setups`' docstring
justifies first-detection with *"the engine skips a symbol once it holds a
position in it, and every later row describes a chance that was already spent."*
The first half is **not what the table shows**: **2982 rows post-date their
group's first TAKEN row, across 173 of the 211 TAKEN groups, up to 257 of them**
(those rows average −0.2910R against −0.1410R at the TAKEN row itself). The
conclusion is right; the stated reason is not the operative one. The operative
reason is §2 and §3 — the later rows are different trades, and the live config
refuses the profitable half of them.

### 6 — ANSWER 2: GROSS R, NINE ENGINES, UNDER THE CORRECT REPRESENTATION

`taken_only=false`, floor 1, complete resolved population. SE is plain
`stdev/sqrt(n)` over group representatives, the same construction the published
table used. Verdict bar is ±2 SE, `under-n` below 30.

```
FIRST DETECTION BY ts  (RECOMMENDED)
engine    n       mean       SE   SE from 0   verdict
SDN     398    -0.0767   0.0724       -1.06   open
VWR     307    -0.3456   0.0714       -4.84   NO
VCE     138    -0.1550   0.1184       -1.31   open
ORB     119    -0.2413   0.0997       -2.42   NO
RNG      60    -0.1366   0.1566       -0.87   open
PBK      32    -0.2758   0.2440       -1.13   open
PDL      25    +0.0635   0.3319       +0.19   under-n
GAP      22    +0.1001   0.2761       +0.36   under-n
GDB       1    -1.0003      nan          --   under-n
```

**Against the published 15-Aug §3 table: identical** — max |delta| 0.0006, which
is 3-decimal rounding. §3 was right, and it was right for the right reason.

**Against F-28 §4's estimator table** (`first-ts − shipped estimator`):

```
engine   first-ts   estimator (F-28 NEW)    delta   SE from 0: first-ts -> est
SDN       -0.0767      -0.0584 +/- 0.0594  -0.0183      -1.06  ->  -0.98
VWR       -0.3456      -0.3134 +/- 0.0667  -0.0322      -4.84  ->  -4.70
VCE       -0.1550      -0.2511 +/- 0.1053  +0.0961      -1.31  ->  -2.38   ** flips
ORB       -0.2413      -0.2925 +/- 0.0951  +0.0512      -2.42  ->  -3.08
RNG       -0.1366      +0.0467 +/- 0.1613  -0.1833      -0.87  ->  +0.29   ** sign
PBK       -0.2758      -0.3230 +/- 0.2291  +0.0472      -1.13  ->  -1.41
PDL       +0.0635      +0.0733 +/- 0.3270  -0.0098      +0.19  ->  +0.22
GAP       +0.1001      +0.0716 +/- 0.2558  +0.0285      +0.36  ->  +0.28
GDB       -1.0003      -1.0003              0.0000       under-n
```

The intermediate constructions, for completeness — note that **"earliest TAKEN
row, else first" agrees with first-ts on every verdict**, which is what makes the
recommendation robust to the 77 groups where the entry was not the first row:

```
engine   first-ts   taken>first   grp-mean(no pref)   shipped est.   last-ts
SDN       -0.0767      -0.0926          -0.0654         -0.0584      -0.1687
VWR       -0.3456      -0.3491          -0.3024         -0.3134      -0.2585
VCE       -0.1550      -0.1803          -0.2678         -0.2511      -0.3564
ORB       -0.2413      -0.2498          -0.2627         -0.2925      -0.2474
RNG       -0.1366      -0.1366          +0.0467         +0.0467      +0.1914
PBK       -0.2758      -0.3234          -0.3039         -0.3230      -0.3498
PDL       +0.0635      +0.0635          +0.0748         +0.0733      +0.0993
GAP       +0.1001      +0.0958          +0.0710         +0.0716      +0.1188
```

### 7 — ANSWER 3: EVERY VERDICT THAT CHANGES

**Exactly one: VCE.**

```
engine     first-ts (correct)        shipped estimator        verdict
VCE      -0.1550 (-1.31 SE) open   -0.2511 (-2.38 SE) NO     CHANGES: NO -> open
```

Every other engine holds its verdict across all four representations.

**VWR lands NO. Unambiguously, and it is the one engine no representation
touches:**

```
first-ts  -0.3456 +/- 0.0714  =  -4.84 SE     NO
taken>first  -0.3491 +/- 0.0711  =  -4.91 SE  NO
grp-mean  -0.3024 +/- 0.0667  =  -4.54 SE     NO
shipped   -0.3134 +/- 0.0667  =  -4.70 SE     NO
published §3  -0.345          =  -4.86 SE     NO
```

The optimistic 2-SE upper limit under the recommended representation is
**−0.2028**, still decisively negative. VWR is negative on every construction
anyone has computed.

**ORB lands NO — but by a thinner margin than the estimator implies:**

```
first-ts  -0.2413 +/- 0.0997  =  -2.42 SE     NO   <-- the correct figure
taken>first  -0.2498 +/- 0.1000 = -2.50 SE    NO
shipped   -0.2925 +/- 0.0951  =  -3.08 SE     NO
```

ORB stays NO, and it stays NO on the actual-execution representation too. Worth
recording that the estimator was flattering the *confidence* of that call by
0.66 SE, not its direction.

**RNG: the verdict does not change, but the sign flip does not survive.** RNG is
`open` either way (−0.87 SE vs +0.29 SE, n=60, well inside the bar). Under the
correct representation RNG is **−0.1366, negative**, not the +0.0467 the
estimator reports. The +0.183R the estimator adds is entirely §4's mechanism:
RNG has 60 groups, 40 multi-row, and **only one of them was ever TAKEN**, so its
number is wholly counterfactual and wholly exposed to this choice.
`RNG/BLUESTARCO 10-Aug` alone — a stop-out scored +2.675R — is +3.674R of group
delta on a 60-key engine.

### 8 — ANSWER 4: WHICH ONE TO ACT ON

**Act on first detection. Do not average the two, and do not treat the
disagreement as uncertainty to be split.**

The two representations are not two estimates of one quantity with different
noise. They answer different questions:

- first-ts answers *"if I take this engine's next setup, what R should I
  expect?"* — which is what `allocation/scoring.py`'s own header says a prior is
  for, and what `score()` consumes.
- the group mean answers *"what is the average R over every level this engine
  proposed on this name today, including re-entries after a loss?"* — a
  question nobody asked, whose answer the book is configured never to be able to
  realise.

Averaging them would produce a number that answers neither. On VCE that would
land near −0.203 / −2.0 SE, i.e. **exactly on the bar** — the worst possible
place to be for a decision that is meant to be evidence-driven, and arrived at
by construction rather than measurement.

The tie-break rule that generalises: **a representation is admissible only if the
system could have held the thing it describes.** first-ts always passes.
"Earliest TAKEN row, else first" also passes and agrees on every verdict. The
group mean fails it on 371 groups outright.

### 9 — FLAGGED, UNASKED: THIS ALSO MOVES THE LIVE PRIOR

Not part of the brief, and it costs money, so it is recorded. Under the shipped
production config (`priors_intraday_taken_only=True`, floor 30, 90-day window,
all read from `system_config`, none hardcoded), the same choice applied to the
TAKEN subset:

```
key                    n   first-TAKEN   mean-TAKEN     shift   usable
INTRADAY/ALL         211       -0.1410      -0.1690   -0.0279   True
INTRADAY/VWR          57       -0.3315      -0.2900   +0.0415   True
INTRADAY/ORB          49       -0.2101      -0.3065   -0.0964   True
INTRADAY/SDN/SHORT    42       +0.1083      +0.1175   +0.0092   True
INTRADAY/ALL/SHORT    42       +0.1083      +0.1175   +0.0092   True
INTRADAY/VCE          37       -0.0687      -0.1799   -0.1112   True
INTRADAY/GAP          15       +0.0626      +0.0494             below floor
INTRADAY/PDL           8       -0.0641      -0.0944             below floor
INTRADAY/PBK           2       -0.9993      -0.6823             below floor
INTRADAY/RNG           1       -1.0000      -1.0001             below floor
```

Every TAKEN group is a position that really opened, so `first-TAKEN` is not a
counterfactual — **it is the R the paper book actually booked**. The live VCE
prior is 0.111R more negative than it should be and the live ORB prior 0.096R,
both on the pessimistic side, which under `alloc_edge_absolute_floor` means the
allocator is refusing setups on evidence that overstates how badly they did.

**RNG and PBK are below the floor of 30 TAKEN rows**, so they fall through to
the per-engine all-detection fallback — which is the §6 number. That is where
RNG's **−0.1366 vs +0.0467** lands directly on a live prior: the estimator
currently tells the allocator RNG is a *positive-expectancy* engine. It is not,
on any row the system could have traded.

### 10 — COULD NOT DETERMINE

- **Whether the first detection's recorded `entry` was actually obtainable at
  that `ts`.** first-ts is the right ROW; this diagnostic does not establish it
  is a fillable PRICE. F-25/F-26/F-27 found the replay residual is intra-minute
  price, and that exposure applies here unchanged. Every number in §6 is a
  paper entry at a recorded level.
- **SE is still not corrected for cross-sectional correlation.** first-ts
  *removes* the within-group correlation problem F-28 §8 flagged — each group
  contributes one row, not a mean over correlated restatements — but same-day,
  same-sector correlation across the 1102 groups is unquantified in both
  constructions. VWR at −4.84 SE has room to absorb it; **ORB at −2.42 SE does
  not**, and that is the one verdict here whose margin is thin enough for it to
  matter.
- **The 1329 unscored detections across 2 past sessions** (F-28 §8,
  `tools.verify`) are still unscored. Every number above moves if they resolve.
- **Why VCE and ORB drift the opposite way to SDN, VWR and RNG** between the two
  representations was not established. F-28 reported the ts→mean→last drift as
  monotone in every engine; it is not — ORB runs −0.2413 / −0.2627 / −0.2474,
  down then up. No mechanism separating the two directions was tested.
- **GDB is n=1.** It appears in every table because §3 does; it is not evidence
  of anything and no verdict should ever be read off it.

### 11 — NOT DONE, DELIBERATELY

**Nothing was changed.** No estimator edit, no config, no migration, no
`intraday_setups` row. `git status` clean; `tools.verify` 545/545, identical to
`main`. The five scratch scripts are read-only and live outside the repo. One
pre-existing warning (`intraday_broker_log.host` missing, migration 077) was
present before this session and is untouched.

**Recommends:**

1. **Adopt first detection by `ts` as the representation for judging engine
   edge**, and say so in one place both consumers read. `dedupe_setups` already
   implements it; `_intraday_priors_from_rows` does not. They currently
   disagree, which is why one table says VCE is dead and the other says it is
   open.
2. **Restate VCE's verdict to `open`** (−0.1550 ± 0.1184, −1.31 SE, n=138). It
   was never NO; the NO was an artefact of averaging in re-entries the live
   config forbids. **VWR stays NO** (−4.84 SE) and **ORB stays NO** (−2.42 SE).
3. **RNG is negative, not positive** (−0.1366, n=60, open). Retire the +0.047
   figure wherever it is quoted. The engine has one TAKEN group in the entire
   history and cannot support a verdict either way — but it must not be carried
   as positive-expectancy in the meantime.
4. **When the estimator is changed, fix `_row_gross_r`'s consumer, not
   `_row_gross_r`.** The per-row arithmetic F-28 landed is correct and both
   controls above depend on it. What needs replacing is the `statistics.fmean`
   over the group in the dedup block — the representative, not the ratio.
5. **Write the test as an invariant, not as an expected number:** a group whose
   members disagree about their outcome must not be summarised by a value no
   member holds. That check fails on the current estimator (371 groups) and
   passes on first-detection, and it is the only form that stays true when the
   population grows.
6. **Merge note stands from F-28** — this branch is off `main`, so F-27 from
   `fix/resolve-day-session-guard` is not in this tree. `docs/FINDINGS.md` will
   conflict at the tail. Keep both in date order; drop neither.

**Gate: PASS on the diagnosis — the published §3 table reproduced to 0.0006, the
F-28 estimator reproduced to 0.0000, and the representation question is decided
on what the system could have held rather than on which number reads better.
NEEDS DECISION from the operator on recommendations 1–3, which restate one
verdict (VCE) and one sign (RNG). Nothing was implemented.**

---

## 2026-08-17 — F-30 (change, quote parity) — RANGE did not regress: the LIVE feed was right on every one of the 212 faulting comparisons and the FETCHED side held the previous close, because the pre-open call auction was being folded into the tick-built bar series. The "clean at baseline" it was measured against was a day the daemon started at 10:08

**The health check said the wrong thing twice.** `quote_parity` reported *"RANGE
REGRESSED — 19 of 402 day_high/day_low comparisons behind"*. The feed had not
regressed, the denominator was not 402, and the baseline it was defending was
not a measurement of the window the defect lives in.

**Which side was wrong.** Pulled the faulting rows for 17-Aug and compared both
sides against `kite.historical_data(token, today, today, "minute")` fetched
live:

| symbol | field | live | fetched | true, from today's bars at 09:26 |
|---|---|---|---|---|
| BELRISE | day_high | 247.77 | **255.35** | 247.77 — and 255.35 is its 14-Aug **close** |
| SBIN | day_high | 1064.20 | **1067.70** | 1064.20 — 1067.70 is its 14-Aug close |
| HINDPETRO | day_high | 372.00 | **373.50** | 372.00 — 373.50 is its 14-Aug close |
| MAZDOCK | day_low | 2593.00 | **2580.00** | 2593.00 — 2580.00 is its 14-Aug close |
| SYRMA | day_low | 1472.00 | **1465.20** | 1472.00 — 1465.20 is its 14-Aug close |

The live value matched the session's own bars exactly in every case. In 13 of
19 faulting names on 17-Aug the fetched HIGH equalled the previous close (a
gap-down name), and in the other 6 the fetched LOW did (a gap-up name) — one
extra bar sitting at yesterday's close, extending the range in whichever
direction the stock gapped. Same signature on 12-Aug and 14-Aug: 18 of 24, and
15 of 15 in the 13:12–13:22 cluster.

**Mechanism.** The socket is subscribed from 09:00. Through the pre-open call
auction Kite delivers ticks whose `last_price` is the previous close, and
`BarBuilder.record_tick` folded them like any other tick, so every tick-built
series began with a ~09:00 bar priced at yesterday's close. `merge_live_bars()`
takes `max`/`min` over that series for a bench-only context's day_high/day_low.
`base.range_between()` was immune by accident — it anchors on a hardcoded 09:15
and offsets from there, so a 09:00 bar lands at minute −15 and falls out of
every window. Nothing else was.

**Why it read clean for ten days: the daemon's start time, not the market's.**

| date | first parity sample | verdict |
|---|---|---|
| 07-Aug | 10:08 | clean ← *this is the "baseline"* |
| 10-Aug | 09:41 | clean |
| 11-Aug | 09:30 | clean |
| 12-Aug | **09:20** | FAULT |
| 13-Aug | 09:52 | clean |
| 14-Aug | **09:21** | FAULT |
| 17-Aug | **09:21** | FAULT |

Perfect correlation. The artifact is washed out of the series within ~15
minutes of the open as real prices ratchet past the previous close, so a late
start never sees it. The check was defending a number measured an hour after
the only window in which the defect is observable.

**The larger finding: the check could not fail.** `apply_live_quotes._overlay()`
overwrites `ctx.day_high/day_low/vwap/prev_close` **in place** with the tick
values, because that is what the engines must read — and the parity logger then
read those same attributes as the "fetched" side. From the second cycle onward
it was comparing the feed against a value it had itself written 300 seconds
earlier. Measured over 38,931 comparisons:

```
day_high      37183 of  38931 identical to the paisa ( 95.5%)
day_low       37152 of  38931 identical to the paisa ( 95.4%)
prev_close    38916 of  38931 identical to the paisa (100.0%)  <- degenerate
vwap          22864 of  38931 identical to the paisa ( 58.7%)
```

`prev_close` differed on **zero** rows in every sample on 17-Aug. The 4.5% of
`day_high` rows that did differ land almost entirely in the two samples right
after a context is first built — the only moment the attribute still held a
fetched number. That is also why the defect showed up at all, and why it showed
up at 09:26/09:31 and then appeared to "heal".

This reframes the vwap conclusion this module has carried since 07-Aug. `vwap`
is 58.7% degenerate rather than 100% only because a live VWAP moves during the
300 seconds between samples; the comparison was live-now against live-then, a
staleness measurement, not the live-versus-bar-formula difference the docstring
attributes it to. **The 848 comparisons said to cross `vwr_stop_buffer_pct` are
not evidence of a formula gap.** They are not evidence of anything yet.

**Two more defects found on the way, both in how the evidence was read.**

- `health.check_quote_parity` selected the 5-day window **unpaged**. PostgREST
  caps at 1000 rows silently, so "19 of 402" was an arbitrary, unordered 0.5%
  sample of ~190,000 rows. Paged now: the same window is **212 of 57,620**.
- `quote_parity.report()` paged on `.range()` with **no ORDER BY** — the exact
  failure `config.fetch_all`'s docstring documents. It returned 38,559 day_high
  rows of the 38,683 that existed, and not as a truncation: an arbitrary subset
  with repeats. Both readers now go through `fetch_all`; `intraday_quote_parity`
  probed 2026-08-17 (191,775 rows, `.order("id")` page one → 1000 rows, 1000
  distinct, ids 1..1000) and recorded in `_FETCH_ALL_SORT_KEY`.

**Changed:**

- `intraday/bar_builder.py` — `SESSION_OPEN`/`SESSION_CLOSE` (09:15–15:30);
  `record_tick` drops out-of-session prints. The day-rollover reset stays
  *outside* that filter deliberately: returning early before it would leave
  `closed_bars()` serving yesterday's session to anything reading between 09:00
  and 09:15, which is strictly worse than the bug being fixed.
- `intraday/strategies/base.py` — `SymbolContext.fetched`, written only by the
  bar/database side and never by the overlay.
- `intraday/engine.py` — `_fetched_snapshot()`, populated by `refresh_contexts`
  and by BOTH branches of `merge_live_bars` (recomputed as bars extend, so a
  bench-only context — never rebuilt — cannot freeze it); parity now logs
  against the snapshot, and counts and WARNS on any context that carries none,
  because a parity table that quietly stops filling looks exactly like a feed
  that agrees.
- `tools/health.py`, `tools/quote_parity.py` — paged reads; the degeneracy
  report; and both remediation strings corrected. Both used to advise turning
  `intraday_quote_mode_range` OFF, which is backwards — that switch is what
  keeps the bad number out of the engines.
- `tests/test_quote_parity.py` (+6 checks), `tests/test_apply_live_quotes.py`,
  `tests/test_static_analysis.py`.

**Verified:**

```bash
cd backend && python -m tools.verify        # all 551 checks passed across 60 modules
cd backend && python -m tools.simulate      # clean, nothing written
cd backend && python -m tools.quote_parity  # 191,775 rows now read (was 189,915)
```

The new checks were demonstrated FAILING with the fix backed out — the pre-open
test reports `got 255.35`, reproducing BELRISE's recorded fetched value exactly.

**Not verified in this session:** that a live session now logs clean. The
daemon runs on `tradeos-vcn` and must be restarted to pick this up. Until then,
and for five days after, `health` will keep reporting the 212 pre-fix
comparisons — they are real observations of a real defect and were not deleted.

**Still open:** the vwap verdict needs re-measuring from scratch once genuinely
independent comparisons exist; `prev_close`'s "FAULT" rests on 15 rows and the
`stock_data_daily` global-LIMIT bug behind it is untouched.

---

## 2026-08-18 — F-31 (change, six gates) — GABRIEL was REFUSED by the pipeline on 3, 4 and 5 August and bought on the 6th, when it was more extended than on any of them. Every gate needed to stop it already existed: three were inert and one — min_rr_to_enter — was arithmetically incapable of firing, because implied_rr is pinned at a constant whenever the stop is re-anchored to price

**The operator closed GABRIEL by hand.** That is the finding that reframes the
rest. The broker log has one `PLACED` and no `MODIFY`: left alone, the ₹1,460.60
limit placed at 09:15:13 would still be resting above a market that has since
traded to ₹1,420, with the protective GTT cancelled since 09:16:01. Not a
slippage problem with a 1.92% price tag — **an exit path that does not
terminate**, applying to every LIVE swing exit in the book.

SCI is on the same path: entered 10-Aug at 300.60, peak 301.60 (+0.05R),
currently −3.31% and −0.46R at 5 sessions.

**Why the R:R gate could not fire.** `analysis/risk_model.py`'s own docstring
states the discipline — *"the stop is a property of the SETUP, not of what you
paid… that is the correct, honest penalty for chasing"* — and it did not hold,
because `compute_trade_plan` derives every level from `ez_low`, recomputed
nightly from the current price:

| date | price | stop | source | implied_rr | vs 0.80 bar |
|---|---|---|---|---|---|
| 29-Jul | 1414.80 | 1248.49 | structure | 0.806 | passes |
| 30-Jul | 1392.00 | 1248.49 | structure | **0.925** | passes |
| 03-Aug | 1527.40 | 1347.61 | atr | **0.777** | refused |
| 04-Aug | 1530.80 | 1346.20 | atr | **0.777** | refused |
| 05-Aug | 1587.70 | 1403.97 | atr | **0.777** | refused |
| 06-Aug | 1531.80 | 1353.73 | structure | 0.805 | **passes by 0.005** — bought |

With `stop = price × (1−a)` and `target = price × (1+b)` the ratio is `b/a` and
the price cancels. Reproduced offline with the switch off: `expected_r` returns
**1.905 at ₹1,414.80 and 1.905 at ₹1,527.40**. A gate comparing a threshold
against a constant does not discriminate; GABRIEL was finally admitted on the
one evening the stop source flipped back to `structure`.

`filter_reason` read `insufficient_rr_0.78x` on 03, 04 AND 05 August, and
03-Aug also returned `eap_action = AVOID_ENTRY`. Nothing in the entry path read
either column.

**The event was not considered, and could not be.** `pre_results_flag` false and
`upcoming_event_type` null on all 12 GABRIEL plan rows; `upcoming_events` null on
all 15 sessions; **`event_calendar` has no `symbol` column** — it is keyed on
`event_category` and `affected_sectors`. There is no per-stock event feed. The
only event that reached the plan was "Southwest Monsoon · POSITIVE · moderate",
a sector tailwind. The volume recorded it exactly: vol_ratio 0.24 → 1.01/0.84/
0.84 (₹134/117/123 cr, 3–5 Aug) → **0.38 on 6 Aug, the day of entry** (₹55 cr) →
0.15 by 14 Aug, with delivery% rising 29.7 → 55.0 as price fell.

**Changed** (migration 080, all six switch-gated):

1. **`EXIT_FASTFAIL`** — `sessions_held ≥ 4 AND peak_r < 0.25 AND gain_r ≤ −0.5`,
   above the 10-session stall. GABRIEL qualified on 10-Aug at −5.5%/−0.56R with
   a peak of 0.00R. **OFF by default** (`exit_fastfail_enabled`): it is the only
   rule in the ladder that sells while the ordinary stop is still far away.
2. **Frozen plan levels** (`plan_levels_frozen`, ON). `planned_stop`/
   `planned_target` are inherited from the live plan and expire only when price
   passes the target or breaks the stop — `plan_levels_still_live()`. Expected_r
   is recomputed against the frozen stop, so it decays as price runs. That decay
   is the chase penalty `min_rr_to_enter` was always supposed to read. Against
   the frozen 29-Jul levels, a ₹1,554.80 fill is above the plan's own target.
3. **Swing liquidity floor** (`swing_min_value_cr` 200, ON). The existing
   share-of-turnover test passes a 2-share position in almost any name — it is a
   floor on the POSITION; this is a floor on the NAME. Intraday is unaffected.
4. **AI refusals bind** (`entry_rank_respect_ai_avoid`, ON). `entry_refusals()`
   is a separate pure function, deliberately NOT inside `score_plan()` — a veto
   that is an additive term can be outvoted, which is exactly how a screener
   score of 82 drowned out everything else. The asymmetry is intentional: the AI
   can veto, never promote. `ai_risks` costs rank points via `rank_w_ai_risk`.
   `entry_respect_filter_reason` is built and left OFF pending a sweep.
5. **The exit terminates.** New `execution/exit_orders.py`:
   - `exit_limit_price` = `ltp × (1 − max(exit_slip_bps, 0.25 × atr_pct))`. On
     GABRIEL that is ₹1,447.9, not ₹1,460.6 — and the 09:15 tape traded ₹1,447
     inside the first minute.
   - `reprice_stale_exits` on the slow timer: reprice after 60s, MARKET after 3
     attempts, attempt count read from the broker's own order history so it
     survives a daemon restart. Cancel-before-market is fail-closed — if the
     cancel errors it does NOT place, because two live sells is worse.
   - `symbols_with_open_exit` — `gtt_manager.sync()` now releases a stop on FILL
     CONFIRMATION, never on placement. An unreachable broker cancels nothing.
   - health check `exits_open`: FAILS on any SELL open > 5 min.
6. **One clock.** `sessions_between()` is pure and shared; the daemon caches the
   session calendar once per day rather than reading per position per cycle.
   GABRIEL was reported as "11 sessions" when held 8.

**Verified:**

```bash
cd backend && python -m tools.verify        # all 574 checks passed across 61 modules
cd backend && python -m tools.health --quick # green except quote_parity (F-30 pre-fix rows)
cd backend && python -m tools.simulate      # 6 swing, 0 needing action; nothing written
cd backend && python -m tools.validate_selects  # 362 sites match the live schema
```

All 23 new checks in `tests/test_gabriel_gap.py` were demonstrated FAILING with
each fix backed out. Two pre-existing checks caught real mistakes in this work:
`validate_selects` rejected `planned_entry` (not a column on
`signal_output_daily`), and the `fetch_all` sort-key check rejected `symbol`
as a paging key on a table with ten rows per name — `(symbol, date)` probed
unique at 2,511 of 2,511 and is recorded.

**Live state after this session.** Nothing has been sold. `exit_fastfail_enabled`
is OFF; run the dry-run before switching it on. Today's book under the rule:
AARTIIND, CARBORUNIV, HINDCOPPER, TATATECH, TRAVELFOOD all HOLD, and **SCI at
−0.46R sits just inside the −0.5 bar** — it does not qualify yet.
`exit_order_reprice_enabled` IS on and will place real MARKET orders on an exit
that will not fill at a limit; that is the behaviour the operator asked for and
the alternative is the 17-Aug state.

**Not verified in this session:** no swing exit has run under the new pricing or
the reprice pass, and no evening pipeline has run under frozen levels. Both need
one live session before they are trusted.

## 2026-08-18 — F-33 (change, intraday stop geometry + per-engine priors) — eight engines falsified their own stop whenever the structure was unaffordable, and it cost 0.5348R per affected trade across 798 of 1,766 rows. Refusing instead of clamping moves the measured book from −0.2403R to +0.0154R gross. Separately: engines were priced on their FAMILY's record (GAP +0.587R scored on ORB's −0.534R), and the prior builder never fetched the column its keying reads. SDN's confidence is inversely related to its own outcomes

### 1 — THE OPERATOR'S QUESTION, AND WHY THE ANSWER IS "A, NOT B"

Asked why `alloc_edge_absolute_floor` refuses every intraday proposal on a new
system, and whether the prior feeding it is wrong.

The floor is not negative. `alloc_edge_absolute_floor` is `0.0`. The number in
the warning line (~−0.80) is `base`, the unclamped percentile of the arrival
population, confirmed independently by `tools.hurdle_population_audit
--framework INTRADAY`: raw p75 −0.7726.

The population is genuinely negative, and the resolver is sound — 1-minute
bars, direction-aware, pessimistic when stop and target fall inside one bar.
Joined against the 66 closed positions where both exist, the naive resolver and
the real exit ladder agree to 0.028R in the mean (−0.071 vs −0.043), so the
prior is not measuring a strategy the book does not trade.
`tools.expectancy_ledger` states it directly:

    INTRADAY / MIS (n=62)
      gross R    mean -0.131 +/-0.089     <- indistinguishable from zero
      friction   mean +0.126 +/-0.005
      NET R      mean -0.257 +/-0.091     <- significantly negative

The engines have no measurable gross edge and friction converts that into a
real loss. The allocator was correct. The defect was upstream of it.

### 2 — THE STOP WAS BEING FALSIFIED, AND THAT IS WHERE THE MONEY WENT

Eight engines carried the same four lines: find a structural stop, then, if it
is wider than `*_max_risk_pct`, move the stop to a price the structure never
named — usually inside the range that created the setup.

Over 1,766 TAKEN-and-resolved rows, split on whether the stop survived:

    stop pinned to the cap   n=798   gross mean R  -0.5348
    structural stop kept     n=968   gross mean R  +0.0154
    whole book              n=1766   gross mean R  -0.2403

    pinned vs structural, per engine
    GAP   139 @ -0.235   vs   144 @ +0.587
    VWR    23 @ -0.245   vs   148 @ +0.123
    VCE    30 @ -0.598   vs   120 @ -0.175
    ORB   604 @ -0.631   vs   186 @ -0.534

GAP is the clean experiment: one engine, one universe, one set of sessions,
separated only by whether its own stop survived. 0.82R.

`base.risk_from_structure()` refuses instead of clamping.
`intraday_stop_cap_mode=tighten` reverts it. Commit 4f859cd.

### 3 — WHAT WAS DELIBERATELY NOT SHIPPED

**The ATR-anchored stop.** No row in `intraday_setups` stores ATR or the
pre-cap stop, so an ATR-multiple rule cannot be calibrated from anything on
disk, and "refuse vs size down" cannot be settled either. Instrumentation is
owed before this question is answerable at all.

**A minimum stop distance.** The 0.0-0.6% band stops out 84.3% of the time
(n=172) and every engine in it is negative — but that band is populated
entirely by the four engines with the tightest caps (PBK 0.80, PDL 0.80, RNG
0.70, VCE 1.00), so this data cannot separate "stop too tight" from "engine is
bad". `intraday_min_risk_pct` ships inert.

**A target-distance filter.** The apparent effect does not survive the stop
fix. On structural-stop rows only, every filter makes the book worse:

    refuse target > 1.5% away   keeps 335/794   meanR -0.092
    refuse target > 2.0% away   keeps 567/794   meanR -0.066
    refuse target > 2.5% away   keeps 782/794   meanR -0.054
    no filter                         794       meanR -0.051

What it removes is near-flat timeouts. The raw-data signal lived in the capped
rows.

### 4 — AN ENGINE WAS PRICED ON ITS FAMILY'S RECORD, AND THE BUILDER NEVER FETCHED THE COLUMN ITS KEYING READS

`registry.FAMILIES` merges GAP and PDL into ORB, PBK into VWR. Its own comment
calls that reversible reporting. It became PRICING because `_prior_for()` looks
up `p.source`, which `from_intraday` sets to the family. GAP (+0.587R) was
scored on ORB's record (−0.534R) — the difference between clearing a 0.0 floor
and never clearing it, decided by another engine's evidence.

Priors are now keyed per engine with the family as fallback. Fixing that
surfaced a second defect: `intraday_priors`' select string did not fetch
`meta`, so `_engine_of` fell back to `strategy` — which since the merge holds
the FAMILY. Every per-engine key was built from pre-merge July rows while
August filed silently under its family. A key nobody fetched: one word long,
invisible in every log, and the reason the first version of this change was
entirely inert.

Asserted through the CONSUMER's lookup, never by reading the dict:

    GAP -> INTRADAY/GAP  n= 42     PDL -> INTRADAY/PDL  n=111
    ORB -> INTRADAY/ORB  n= 85     PBK -> INTRADAY/PBK  n=204
    VWR -> INTRADAY/VWR  n= 61     SDN -> INTRADAY/SDN/SHORT  n=69
    GDB -> INTRADAY/ALL  n=256     (GDB n=2, correctly falls through)

### 5 — SDN'S CONFIDENCE RUNS BACKWARDS, AND CONFIDENCE IS THE SELECTOR

All 265 TAKEN-and-resolved SDN rows, bucketed by the confidence assigned at
detection:

    confidence      n    STOP%    TGT%   mean gross R
    0.55 - 0.62    33    15.2%   42.4%      +0.769
    0.62 - 0.66    44     9.1%   38.6%      +0.880
    0.66 - 0.70    68    36.8%   16.2%      +0.326
    0.70 - 0.75    79    30.4%   27.8%      +0.411
    0.75 +         41    63.4%   12.2%      -0.273

`registry.evaluate_all` sorts by `-s.confidence`, so the book funded SDN's
worst detections first — and SDN receives most of the paper book's slots
through `floor_only_rank`. The operator reported SDN "fires but does not pick
the right trades" before this was measured; that was a correct read of the book
from the outside.

`intraday_short_max_confidence` ships INERT. One cut, one engine, 41 rows
carrying the decision, no out-of-sample confirmation. The real repair is to the
confidence FORMULA — a score that predicts its own failure is mis-specified,
not merely mis-thresholded — which needs the per-condition split
(VWAP-rejection vs trap vs breakdown) this table does not separate.

### 6 — THE GIVE-BACK GUARD WAS VALIDATED, NOT CHANGED

Migration 059 armed it at 50% / min 0.5R by borrowing SWING's number — which
`exit_policy.py`'s own comment said not to do. Calibrated now on the 49
intraday positions carrying usable excursion:

    peak reached    n   final meanR   median kept   ended negative
    0.0-0.5R       26      -0.429        -238%          22/26
    0.5-1.0R       10      -0.152         -28%           6/10
    1.0-1.5R        7      +0.691        69.1%           1/7
    1.5-2.0R        4      +0.973        59.1%           0/4
    2.0R+           2      +1.746        80.2%           0/2

50% is well placed: winners peaking 1.0-2.0R keep 59-69%, so the guard does not
clip them, while 30% would. No change made. The trail (`trail_after_r=1.5`) was
left alone — only 11 trades ever exceeded a 1.0R peak, and below 2R the
give-back guard already binds tighter than the trail.

### 7 — COULD NOT DETERMINE

- **Why SDN detections exploded on 12-Aug** — 88-102 symbols per session since,
  against 25-30 before. SDN now fires on essentially the whole universe daily.
  Not investigated; it dominates the paper book through `floor_only_rank`.
- **Whether the 9 INTRADAY trades closed as CNC** (NET R -1.110, friction
  1.451R) were a square-off failure or product tagging. The operator reports
  this is since fixed and new trades record as MIS; the historical rows remain
  and still carry the loss.
- **Whether refusing a capped setup beats sizing down.** The counterfactual for
  refusal is on disk — those rows ARE the -0.5348R population. The
  counterfactual for a widened stop is not, and cannot be without
  instrumentation.
- **Whether the per-engine priors survive out of sample.** They are built from
  rows dominated by the OLD geometry and will move as structural-stop trades
  accumulate. Nothing in this entry is a forward result.

### 8 — NOT DONE, AND ONE PROCESS FINDING

Instrumentation (`atr_pct_daily`, pre-cap structural stop, target distance at
detection) is designed and not written. Migrations 082 and 077 remain
outstanding. `quote_parity` is red at 274 of 70,698 comparisons against a check
with no tolerance band, and is another session's active work area.

**Recorded because it destroyed work twice.** A second session was committing
to this repository concurrently — it committed onto this session's branch,
cherry-picked to `main`, and reset, discarding the whole change set on two
occasions roughly fifteen minutes apart. The set was rebuilt from scripts held
outside the repo and committed immediately the third time. Two agents in one
working tree with no lock is not a merge problem, it is a data-loss problem,
and nothing in this repository currently prevents it.

## 2026-08-18 — F-34 (correction + change, follow-up to F-33) — F-33 §8's "migrations 082 and 077 remain outstanding" was never checked against the live database and was wrong; both are applied. Detection instrumentation (ATR, pre-cap structural stop) is now shipped. quote_parity's warm-up hypothesis explains 61% of the residual and stops there — a distinct, unexplained cluster remains and was not touched

### 1 — THE MIGRATION CLAIM IN F-33 WAS AN ASSERTION, NOT A VERIFICATION

F-33 §8 stated "Migrations 082 and 077 remain outstanding," sourced from
`tools.verify`'s console output during that session — specifically the
warning `outcomes: intraday_setups is missing scored_at, scored_by,
scored_through — apply migration 082`. That warning did not come from a live
database read. It came from `tests/test_resolve_day_session_guard.py:375`,
which deliberately constructs fake rows *without* those columns to exercise
`_provenance_supported()`'s pre-migration fallback path offline — the test
harness working exactly as designed, not a live gap.

A direct schema probe against the actual Supabase project:

    intraday_setups.scored_at        OK
    intraday_setups.scored_by        OK
    intraday_setups.scored_through   OK
    intraday_broker_log.host         OK
    intraday_broker_log.pid          OK

Both migrations are applied. This project's own rule is "verify, never
assert" — checking `tools.verify`'s test-harness log instead of the database
is exactly the mistake that rule exists to prevent, applied to itself. F-33
§8's migration line is superseded; nothing there needs action.

### 2 — DETECTION INSTRUMENTATION, SHIPPED

F-33 §3/§8 named the gap in `base.risk_from_structure`'s own docstring: "no
row stores ATR or the pre-cap stop, so what a widened stop would have done
cannot be reconstructed from any row on disk." Two stamps, closed separately
because they need different justifications for why they are safe to add:

**`atr_pct_daily`**, stamped once in `registry.evaluate_all` — the same hook
that already writes `sub_engine`/`family`/`lifecycle` — so it covers all nine
engines uniformly, including RNG and SDN, which never call
`risk_from_structure` at all. Recorded as an explicit `None` when the context
has no ATR, not omitted: an absent key and a recorded-unknown value must not
collapse into the same reading on a later query, per this project's own
cold-start rule (`hurdle._cold_start`, `intraday_min_risk_pct`).

**`RiskFrame.meta()`**, merged into each of the eight engines' own `meta={}`
dict at the point they already build one. It returns `{}` under the default
`refuse` mode — under `refuse`, `structural_stop` is always identical to the
`stop` column already on the row, and a field that always duplicates another
column is the "silent default" this project warns against, not
instrumentation. It only carries data under the LEGACY `intraday_stop_cap_
mode=tighten` branch, where the two diverge — which is the one case this
project has already lost information in once, inside this same change (see
F-33 §2's own "what it replaced" arithmetic).

**Target distance was deliberately NOT stamped.** `entry` and `target` are
already direct columns on `intraday_setups` (migration 014); target distance
is `(target - entry) / entry`, fully recoverable from data already on disk.
Adding a third copy of arithmetic every reader can already do is duplication,
not instrumentation.

`tests/test_detection_instrumentation.py`, 8 checks, end to end through the
real ORB engine (not a stub) for both stamps, plus a fake-engine check that
the ATR stamp cannot be skipped by a new engine. Every assertion demonstrated
against a one-line removal of the stamp it pins.

`tools.verify`: 619 checks, 66 modules, all green.

### 3 — QUOTE_PARITY: THE WARM-UP HYPOTHESIS IS CONFIRMED, PARTIALLY

Tested against `intraday_quote_parity` directly rather than against
`health.py`'s summary. `day_high`/`day_low` comparisons, ranked by position
within each `(symbol, field, trade_date)` sequence:

    sample #    n       behind    rate
    0          1688       106    6.28%
    1          1688        57    3.38%
    2          1688        36    2.13%
    3          1688         2    0.12%
    4-9        ~10,100      0    0.00%
    10+       91,454       124    0.14%

The first three samples of every symbol-day carry 199 of 325 behind-flags
(61%) at rates far above the steady-state 0.14% — base.py's own 17-Aug
comment ("almost entirely in the two samples right after a context is first
built") is directionally right and now measured precisely: it is the first
THREE samples, not two, and the decay to near-zero by sample 4 is sharp
rather than gradual.

**It does not explain the rest.** The residual 126 comparisons beyond
warm-up do not scatter — they cluster at two repeating sample-index bands
(42-44 and 57-59) across many different symbols and three separate trading
days (14-Aug: 55, 17-Aug: 18, 18-Aug: 51), and 84 of 126 are NOT each
symbol-day's first post-warm-up occurrence, meaning the same (symbol, field)
pair goes behind more than once in a session. Magnitude: median 0.224%, p90
0.737%, worst 3.39% — not all of these are noise-sized. Sample-index bands
repeating across unrelated symbols on specific days reads as a scheduled or
systematic event (a periodic re-fetch, a reconnect, a cache-refresh cadence)
rather than incidental drift, but which mechanism was not identified and the
hypothesis was not tested further.

`quote_parity.py`/`health.py` were NOT edited. That file is another
session's active work area (`fix/quote-parity-and-gabriel-gap-gates`); the
mandate here was to test the warm-up hypothesis, not to fix the check, and
the residual finding is handed off rather than acted on.

### 4 — NOT DONE

- The quote_parity residual's mechanism (sample-index clustering at 42-44 and
  57-59) is unidentified.
- Whether the residual affects any live trading decision, or only the parity
  audit's own bookkeeping, was not checked.
- No change was made to `intraday_stop_cap_mode`, `intraday_min_risk_pct`, or
  `priors_intraday_lookback_days` — all three remain at their shipped
  defaults (`refuse`, unset/0.0, 90 days) per the operator's explicit
  decision this session: no change until roughly two weeks of
  structural-stop trading has given each engine that matters ~30 TAKEN rows
  of its own.

## 2026-08-19 — F-35 (change, allocator queue + prior segmentation + exit cadence) — `edge` is keyed on the ENGINE, so a pooled sort ranked ENGINES and handed the whole slot budget to one: 29 of 32 closed positions came from SDN while ORB wrote 561 TAKEN rows and closed one. Fairness ships ARMED because it cannot admit anything that fails the bar. Confidence is not comparable across engines — inverted for three, noise for ORB at n=1030 — so bands ship INERT on a pre-F-33 sample. The exit ladder leaves the 15s timer; entries do not

**Ran:** read-only SQL against the live database, the log's own cycle
timestamps, `tools.verify` (636 checks), `tools.simulate`, `tools.health`.

### 1 — THE OPERATOR'S QUESTION, AND THE MEASUREMENT THAT ANSWERS IT

Asked why six of seven engines cannot take a trade, having rejected the framing
that this is about engine QUALITY.

`tools/taken_reconciliation.py` — the tool built for exactly this question on
10-Aug — could not run: it paged `open_positions` on `order_by="id"`, and
migration 028 keyed that table on `(symbol, product)`; it has never had an `id`
column. PostgREST returned 42703 and the tool had been dead since. Fixed
(`order_by=date_col`, real on both tables). Last six sessions:

    date          taken symbols   real positions   ratio
    2026-08-12         30              10           33%
    2026-08-13         13               2           15%
    2026-08-14         37              10           27%
    2026-08-17         14               0            0%
    2026-08-18         42              12           29%
    2026-08-19         13               8           62%
    TOTAL: 149 TAKEN symbol-days, 42 became a position (28%)

    of the 112 that did not open:
      29 ALLOCATOR_DECLINED   17 REJECTED_COST      17 BLOCKED_STRUCTURE
      16 BLOCKED_SHORTABILITY 14 BELOW_CONVICTION   10 BLOCKED_SHORTS_MARKET
       6 VETOED_AI             3 BLOCKED_PAPER_CAPACITY
       0 unexplained

Zero unexplained — `_maybe_open_paper`'s 10-Aug verdict instrumentation holds.
Restricted to ORB+VCE alone the shape is the same and ALLOCATOR_DECLINED is
again the largest single bucket (53 of ~170). Scanning is not the constraint:
114-123 distinct symbols per session, 850-2,289 detections.

**The first hypothesis was wrong and is recorded as such.** Confidence-sorting
in `registry.evaluate_all` was assumed to crowd the other engines out. It does
not: mean confidence of TAKEN rows is ORB 0.751 and VCE 0.777 against SDN
0.678. The engines being starved carry the HIGHER confidence.

### 2 — THE MECHANISM: A POOLED SORT ON A NUMBER THAT ONLY VARIES BY ENGINE

`policies.intraday_stopping` sorted every candidate into one queue by `edge`.
`score()` computes `edge = (prior.mean_r * regime_mult - cost_r) / hold_days`,
and `prior` is keyed on the engine — so within one cycle, candidates from one
engine differ ONLY through `cost_r` (friction over that setup's own risk).

A pooled descending sort therefore does not rank SETUPS. It ranks ENGINES, and
then fills the slot budget from the top engine's candidates in whatever order
friction happens to break their near-tie. There was no mechanism by which a
second engine's BEST idea could compete with a first engine's fifth-best.
13-19 Aug: SDN 29 of 32 closed positions, VWR 2, GAP 1, and ORB — 561 TAKEN
rows — one.

`_interleave_by_engine` queues every engine's best before any engine's second,
each round internally ordered by edge.

**ARMED, not inert, and the reason is a safety property rather than
confidence in the idea.** Interleaving reorders the QUEUE; `bar` is untouched
and the caller still declines every proposal beneath it. The only outcomes that
can change are those where a lower-ranked engine's candidate ALREADY cleared
the bar and lost its slot to a same-engine sibling. It cannot admit a proposal
that fails the bar, and `test_fairness_cannot_admit_a_proposal_that_fails_the_bar`
pins exactly that. `alloc_intraday_engine_fairness=false` restores the pooled
sort exactly.

### 3 — CONFIDENCE IS NOT ONE QUANTITY, AND THAT IS WHY BANDS SHIP INERT

Every TAKEN-and-resolved row, terciled WITHIN each engine so no engine's level
contaminates another's slope. Gross R, low -> high tercile:

    engine     n      low      mid     high    reading
    ORB     1030   -0.424   -0.641   -0.220   noise, not monotone
    PDL       56   -0.606   -0.816   -1.000   inverted
    SDN      272   +0.725   +0.265   +0.191   inverted
    VWR      204   +0.123   +0.347   -0.529   inverted at the top
    VCE      159   -0.599   -0.510   +0.430   ordered as intended
    GAP       46   +0.158   +0.366   +0.276   thin, roughly flat

Confidence means something different in every engine that computes it. So the
one number the allocator ranks on cannot be confidence — but what confidence
has been WORTH, per engine, can be, and that is a prior key:
`INTRADAY/{ENGINE}@{BAND}`, falling through to the engine, then the family,
then the book, each rung still gated on `priors_min_sample_intraday`.

**SHIPPED INERT (`alloc_intraday_confidence_bands=false`), and the reason is
the sample, not the mechanism.** That table is dominated by rows recorded under
the PRE-F-33 stop geometry. Post-fix TAKEN-and-resolved rows on 19-Aug: VCE 9,
SDN 7, ORB 3, every other engine 0 — 19 rows total. Arming this now would pin
each engine's slope to a strategy the book no longer trades.
`priors_intraday_since` (also inert) is the operator's lever for that: a HARD
floor date, combined with the rolling `priors_intraday_lookback_days` by taking
the later of the two, because a rolling window cannot express "everything
before this date measured different rules".

**`confidence` had to be added to the prior builder's SELECT.** Without it
every row bands as None, no band key is built, the feature is an elaborate
no-op, and NOTHING IN ANY LOG SAYS SO — the ladder simply misses its first rung
and falls through to the key it already used, indistinguishable from the switch
being off. That is the same one-word defect as `meta` in F-33 §4, caught before
shipping this time rather than after. `health.selects` passes strictly, which
is the live schema probe that the column is real.

### 4 — THE EXIT LADDER LEAVES THE 15-SECOND TIMER; ENTRIES DO NOT

The operator's other concern: a decision arriving after the move has gone.
Measured rather than assumed. Today's log, 09:30-15:00, 1,239 cycles:

    median gap 16.0s · p90 18s · p99 28s · max 44s · none over 60s

So the loop is healthy and the decision itself costs ~1s; the latency IS the
interval. The AI advisor is already off the hot path (`refresh_advisory` on the
slow timer — a DeepSeek call measured at 88.6s could never have sat in a 15s
loop).

Finding a setup and defending an open position were sharing one timer sized for
the expensive one, and they are opposite on both cost and urgency. A scan is
~120 symbols x 9 engines and writes detection rows; every entry engine also
carries its own chase guard (`vce_max_chase_pct`, `confirmation_pct`) that
refuses a move already gone, so scanning faster largely re-derives the same
answer for more rows. The exit check reads `self.positions` — in memory, 0-4
rows, no database read — and writes only when a rung fires, a handful of events
per session.

`engine.guard_positions()` runs the exit ladder alone, on
`intraday_position_guard_interval_s` (3s), between full cycles and never
instead of one. It cannot change which trades are ENTERED: it does not refresh
contexts, merge bars, re-rank the universe, evaluate candidates or setups, or
run the allocator. Setting it >= `intraday_eval_interval_s` disables it exactly.

**Deliberately NOT done: lowering `intraday_eval_interval_s` itself.** That is
the change that would address entry latency directly, and it is also the one
that multiplies detection writes. `health.storage` reports 288 MB of 500 MB
(57.7%) growing 71 MB/month, forecast 80% on 2026-10-05. Whether dedup
(`_setup_is_new`, 0.35% drift) absorbs a 3x cycle rate is a guess, and it is
not one to make against a bounded free tier without measuring first.

### 5 — TWO SMALLER THINGS FOUND ON THE WAY

**An edge of exactly 0.0 sorted below every loser.** The queue's key was
`-(x.get("edge") or float("-inf"))`, and `0.0 or -inf` is `-inf` because 0.0 is
falsy. A NEUTRAL prior against zero modelled cost would have ranked beneath a
measured -0.9R. Vanishing in floating point and completely silent when it
happens, which is why it survived. `_edge_key()` treats only `None` as absent.

**A check that could not fail, caught by the demonstration rather than by
review.** `test_turning_bands_on_does_not_move_the_pooled_fallback` stayed
green with the BAND_SEP filter deleted from the `longs` comprehension, because
every row in its fixture is TAKEN and `_prior_for("ALL", ...)` therefore only
ever exercised the `by_taken` branch. The ungated branch was untested.
`..._on_the_UNGATED_path_either` routes the same population through
`priors_intraday_taken_only=false`. All six breaks are now detected.

### 6 — NOT DONE / COULD NOT DETERMINE

- **Whether any of this improves the book.** Nothing here is a forward result.
  Fairness changes which bar-clearing candidates get slots; it does not create
  edge, and `expectancy_ledger`'s INTRADAY/MIS gross R (-0.131 +/- 0.089) is
  still indistinguishable from zero before friction.
- **Whether the pre-F-33 verdicts on ORB and VWR survive re-measurement.** The
  16-Aug "already NO at 2 SE" figures were computed on the same clamped-stop
  population F-33 corrected two days later. They are not re-run here and should
  not be treated as settled until they are.
- **The engine+family double-count in the pooled fallback.** A GAP row lands in
  both `GAP` and `ORB`, so `longs` counts it twice today. Real, pre-existing,
  and left alone — correcting it moves every fallback prior in the book and
  deserves its own before/after rather than a ride-along. Band keys are
  excluded from the pool so this change does not make it worse.
- **Whether `_setup_is_new`'s dedup absorbs a faster entry scan** — see §4.
- **Gate 3 remains unmade.** No engine was retired, shadowed or promoted; the
  operator's explicit decision this session was that every engine stays ACTIVE,
  on the ground that `priors_intraday_taken_only` means a SHADOW engine can
  never write the TAKEN row its own prior would need to recover. That reasoning
  is correct and is recorded here because it changes what SHADOW MEANS in this
  system: it is not a pause, it is a permanent freeze.

### 7 — ADDENDUM, SAME SESSION: THE FAIRNESS FIX SAT DOWNSTREAM OF A FILTER I HAD NOT TRACED

`_interleave_by_engine` (section 2) fixes competition BETWEEN symbols. It does
nothing about competition WITHIN one, and that is where the operator's concern
actually lived. `registry.evaluate_all` sorts `found` by `-confidence` and
returns ONE setup per symbol, so when two engines fire on the same name the
loser never reaches the allocator at all — and the tie-break is the number
section 3 measures as inverted for SDN/PDL/VWR and noise for ORB at n=1030.

Sequencing error, recorded because it cost a round trip: the fix was proposed
and built before the full path was traced, and the filter was found afterwards.
The correct order is trace, then propose.

`_arbitrate_symbol` ranks the ACTIVE setups for a symbol on
`Allocator.expected_r_for()` — the allocator's OWN prior ladder, so arbitration
and selection cannot disagree about which engine is better regarded — with
confidence and rr surviving as tie-breaks. Switch: `intraday_symbol_arbitration`
(`prior` | `confidence`).

**It degrades to today's behaviour whenever evidence is absent.**
`expected_r_for` returns None, never 0.0, for a below-floor prior, and None
cannot win the comparison. With no usable prior on either engine the tie-break
falls through to confidence exactly as before. That branch is the common one
right now: most of the population predates F-33, which is precisely why
arbitration must not manufacture a preference it cannot support.

**One test in this module cannot fail from a single break, and that is
recorded rather than tidied away.** `..._never_promotes_a_shadowed_engine` is
held by two independent guards — `_arbitrate_symbol`'s LIFECYCLE_ACTIVE filter
and `proposal.from_intraday`'s own SHADOW refusal. Breaking either alone leaves
it green; breaking both turns it red, which was demonstrated. It pins the
PROPERTY across a deliberately redundant pair rather than either guard, and the
redundancy is intentional on the one rule here that concerns capital rather
than ranking.

`tools.verify`: 642 checks, 67 modules, green. Six deliberate breaks
demonstrated failing across sections 2, 3 and this one, plus the two-guard case
above.

**Still not addressed, and it is the third part of the operator's ask.** Slots
are consumed in ARRIVAL order across the session: the day's budget can be spent
by 10:00 on candidates that merely cleared the bar, and an excellent 14:00
setup then competes for whatever is left. `hurdle`'s `time_mult` decays the bar
DOWNWARD as the session runs out (deliberately, so the budget is not left
unspent), which is the opposite of reserving capacity for a better arrival.
`order_manager.entry_reserved()` and `intraday_max_entries_before_time` (0, off
for intraday) are the existing lever and were NOT armed here — arming them
trades a known quantity (fewer early entries) for an unknown one (whether later
arrivals are better), and nothing measured in this session says they are.


## 2026-08-19 — F-36 (change, arrival-aware pick label) — the operator's own instinct was right and mine was wrong: shrinking intraday_max_new_per_day fights priors_intraday_taken_only directly, since fewer TAKEN rows means every thin engine's prior converges SLOWER, not faster. Volume and selectivity are different levers. Built the second one without touching the first: a TOP_PICK/EXPLORATION label, additive to every verdict, driven by a real arrival curve read from history rather than a guessed one — caught reading it in the wrong timezone and unpaged before either reached the database live

**Ran:** `tools.verify` (652 checks, 67 modules), `tools.simulate`, `tools.health`,
one live smoke test of the new query against the real database.

### 1 — THE OPERATOR CAUGHT A REAL CONTRADICTION IN F-35's OWN RECOMMENDATION

F-35 proposed lowering `intraday_max_new_per_day` from 20 toward ~6 so the
allocator's scarcity term would engage. Pushed back on directly: this is a
PAPER book, more trades cost nothing and are free evidence, and
`priors_intraday_taken_only` (this project's own design) means a prior can
ONLY learn from TAKEN rows — so shrinking the budget doesn't create
selectivity, it starves every prior of the exact data the rest of this
session's work (bands, arbitration) depends on to mature. `allocator.py`'s own
comment on the PAPER/LIVE floor carve-out says this already: *"the honest fix
... is to make the floor stop binding — a positive prior — not to remove the
only path that can produce one."* The recommendation was wrong on the
project's own stated terms, not merely unwelcome.

**Correction: volume and selectivity are two different questions and were
being answered with one dial.** "How many trades does paper take" should stay
generous — it is a learning-speed question with a free answer. "Which of
today's trades were genuinely the best, versus kept mainly so a thin prior
keeps learning" is a labelling question, and `intraday_max_new_per_day`
cannot answer it no matter where it is set.

### 2 — THE LABEL ALREADY PARTLY EXISTED, AND WASN'T VISIBLE

`floor_only_rank` (12-Aug, `policies.intraday_stopping`) already computes
almost this exact distinction for the exploration carve-out. Checked live: the
last 8 declined proposals in `allocation_decisions` all show
`hurdle_inputs->>'floor_only_rank'` as `null`. It exists, it is correct as far
as it goes, and it is buried inside a JSON column on a table the operator does
not query — not a defect, but not an answer to "was THIS trade a good one"
either.

### 3 — WHAT WAS BUILT, AND WHY IT CANNOT CHANGE WHICH TRADES ARE TAKEN

`allocation/hurdle.py::arrival_histogram()` — average TAKEN-quality detections
per IST hour, read from real history, cached once per calendar day. Live
shape (18-19 Aug, timezone-corrected, see §4):

    IST hour   9      10     11    12    13    14
    avg n     11.57   5.79   1.50  0.64  0.93  0.43

Front-loaded, matching the operator's own — and this session's — earlier
measurement almost exactly.

`label_quantile(slots_left, remaining_expected)` = `1 - slots_left/remaining`,
clamped `[floor, cap]`. High when much more is coming and few slots remain
(strict — only a genuine best-of-day counts as a pick); low when supply is
nearly exhausted (permissive — whatever is left IS the best available by
definition, not merely acceptable). `remaining=None` (no curve yet) and
`remaining<=0` (nothing left expected) both land on `floor`, deliberately, for
opposite reasons — see the function's own docstring; collapsing them would
either over-label a data-poor system's early trades or under-label its last
ones, the same cold-start distinction this module already draws elsewhere.

Wired into `hurdle()` as `label_bar` — a STRICTER quantile of the SAME
arrival-edge population `bar` is drawn from, returned in `inputs` alongside
it. `Allocator.select()` stamps `pick_label` (`TOP_PICK` / `EXPLORATION`) onto
any verdict already TAKE, using `edge >= label_bar`. **Nothing about TAKE vs
DECLINE changes** — `bar` and the verdict branch it drives are untouched; this
runs strictly after that decision and only annotates it.

Threaded onto the actual trade record, not left in
`allocation_decisions.hurdle_inputs`: `engine.act_on_setups` reads the label
off the SAME verdict `allocator_permits` already consulted (so it cannot
disagree with the decision that let the trade through), passes it to
`_maybe_open_paper` -> `paper_broker.open_position` -> `open_positions.
pick_label`, and `control.position_lifecycle.close_position` carries it onto
`closed_positions.pick_label` the same way `sector` already is. Migration 085.

**Ships INERT** (`alloc_intraday_pick_label=false`). The arrival curve above
is built from `intraday_setups`, and F-33's stop-geometry fix is one day old
— arming this now would label trades against an arrival shape recorded partly
under a strategy the book no longer runs. Same posture as
`intraday_short_max_confidence` and the confidence bands: built, tested, and
deliberately not yet trusted with the current sample.

### 4 — TWO DEFECTS FOUND BY RUNNING THE NEW CODE ONCE, BEFORE TRUSTING IT

**Unpaged and then wrongly paged, caught by `tools.verify` itself, in two
steps.** First build read `intraday_setups` with no paging at all —
`static_analysis` failed immediately: TAKEN detections alone ran
1,000-2,289/session in mid-August, so a 20-day window is routinely tens of
thousands of rows against PostgREST's 1,000-row cap. Fixed with `fetch_all`,
sorted on `trade_date` for readability — `static_analysis` failed AGAIN:
`trade_date` is not unique, so LIMIT/OFFSET paging on it can repeat and skip
rows across page boundaries with no error, the identical defect
`intraday_priors()` carried before its 15-Aug fix. Corrected to sort on `id`,
this table's verified unique key. Both were caught by the project's own
tooling before either reached a live database call — recorded because it is
the tooling working exactly as designed, not despite it.

**The histogram was built in UTC and never converted.** `ts` comes back from
PostgREST as a UTC timestamp; the first version read its hour by string slice
and produced a histogram peaking at 04:00-05:00 — 09:15 IST genuinely stores
as roughly 03:45 UTC. Not caught by any offline test, because every offline
test supplies its own hour directly rather than parsing a timestamp — it was
caught by running `arrival_histogram()` once against the real database before
trusting it, exactly this project's own "verify, never assert" rule, applied
to code that had just been written rather than only to code under suspicion.
Fixed: parsed with `datetime.fromisoformat`, converted via `.astimezone(IST)`.
Re-run live: 9:00 IST onward now reads 11.57, matching the shape measured by
hand in §3 rather than a UTC-shifted one.

### 5 — NOT DONE / COULD NOT DETERMINE

- **Whether TOP_PICK trades actually outperform EXPLORATION ones.** That is
  the entire point of building this, and it is unmeasured — the switch is
  off and no trade has ever been labelled. The plan, stated to the operator:
  watch the two populations separately once armed, not as a single blended
  number.
- **When to arm `alloc_intraday_pick_label`.** No date is set. The honest
  gate is the same one `priors_intraday_since` is waiting on: enough post-F-33
  sessions that the arrival curve itself is not still shaped by the old stop
  geometry.
- **`intraday_max_new_per_day` was left at 20, deliberately, and should stay
  there.** Restated because F-35 recommended the opposite and this entry
  reverses that specific recommendation, not the surrounding work.


## 2026-08-19 — F-37 (change, ORB engine — retest confirmation + measured-move target) — validated the STRATEGY against established Opening Range Breakout practice before touching the ENGINE, per the operator's explicit request. The concept is sound and this codebase's own filters are not naive; two gaps were specific and named in the code's own docstring rather than guessed at, one of which was skipped on a stale premise. Both closed. A third (regime awareness) named and deliberately left alone

**Ran:** `tools.verify` (666 checks, 68 modules), `tools.simulate`, `tools.health`.

### 1 — THE STRATEGY, NOT JUST THE OUTCOMES

Asked to validate whether ORB works as a strategy before enhancing the engine,
rather than continuing to tune against outcome data (which the operator was
right to distrust — most of it predates F-33's stop-geometry fix). Read
`intraday/strategies/orb.py` against established Opening Range Breakout
practice (Crabel; standard retail/professional day-trading treatment) rather
than against `intraday_setups` again.

**Verdict: the strategy concept is legitimate and this implementation is not
naive.** Volume confirmation, range sanity scaled to the stock's own ATR, a
chase limit, a previous-day-high filter and a structural stop at the range
low are all real, standard discipline — more than most public ORB
implementations carry. This is not a "bad idea" story.

### 2 — THE RETEST ARM: NAMED, SKIPPED, AND THE REASON WAS FALSE

The module's own docstring has specified `retest OR strength` since 12-Aug —
"either the break has real distance behind it, or price has come back to the
level and held" — and only ever implemented the first half. The comment
explaining the omission said the retest arm "needs bar history this engine is
not given."

**Checked, not assumed. False.** `ctx.bars` is the same field `range_between()`
two lines above already reads — every engine receives it. There was no data
gap; there was an unbuilt filter with a stale comment attached.

Built `_retest_and_held(bars, level, tolerance_pct)`: true when price already
probed above the range high on an earlier CLOSED bar, came back within
`orb_retest_tolerance_pct` (0.15) of that level, and every bar since closed at
or above it — one failure anywhere in the sequence voids the whole thing, a
level "held" once and lost later is not held. Wired as an ALTERNATIVE to the
existing strength gate, not a relaxation of it: a strength-confirmed break is
entirely unaffected; this only rescues a break that would otherwise be refused
as "a quote, not a signal" but shows a genuine retest behind it — which
day-trading practice generally treats as the HIGHER-confidence pattern, not a
consolation prize for a weak break.

`orb_retest_confirmed` stamped into `meta`, unread by anything yet — same
instrument-first discipline as F-33's ATR stamp. Whether retest-confirmed
ORBs actually outperform strength-only ones is a real, open, measurable
question this makes askable, not a claim being made here.

Switch: `orb_retest_confirmation_enabled` (true). `false` restores the
strength-only gate exactly.

### 3 — THE TARGET WAS A FLAT MULTIPLE WITH NO RELATIONSHIP TO THE STRUCTURE THAT PRODUCED THE TRADE

Classic ORB practice projects the target from the range's own height — the
"measured move." ORB's target was `entry + risk x orb_target_r` only, no
relationship to the range at all. **VCE, a sibling breakout-style engine in
this same codebase, already does this** (`squeeze.py`'s
`measured = ctx.ltp + (p_hi - p_lo)`) — ORB was missing a feature its own
better-performing sibling already had.

`target = max(flat_R_target, entry + range_height)`. `max()` only: can widen a
target the flat multiple already set, never shrink one — no existing trade's
target moves closer.

**Found while testing it, and recorded because it changes what to expect:**
this rarely wins at ORB's own shipped default (`orb_target_r=2.0`). ORB's
stop sits at the range low, so `risk = (entry - range_high) + range_height`
— already at least the range height before the 2x multiple is even applied.
The measured-move target is real and correctly wired, but under this engine's
own stop placement it will only bind when a trade's risk is small relative to
the range that produced it. Proven both ways in tests: the wider branch is
real (demonstrated at `orb_target_r=0.3`), and the shipped default correctly
does NOT reach it on the same fixture — `max()` is doing its job; it simply
has little to act on given how this engine places its stop.

Switch: `orb_measured_move_target_enabled` (true). `false` restores the flat
multiple exactly.

### 4 — WHAT WAS NAMED AND DELIBERATELY NOT BUILT

**Regime awareness.** ORB is a trend-continuation pattern and the literature
is consistent that it works in trending tape and fails in chop — a real gap,
named in the module's own docstring now. Not built: the mechanism already
exists (`allocation.scoring.regime_fit_multiplier`) and ships at weight 0.0
specifically because arming a regime effect on theory rather than measurement
is the mistake `hurdle.py`'s own docstring already paid for twice (05-Aug,
10-Aug STRONG-bucket self-reference failures). Closing this gap on the same
theoretical basis that has already cost this project real evidence-discipline
twice would be repeating the mistake, not fixing ORB.

**Breakdown (short) side of the pattern.** ORB is long-only by design — MIS
squaring off and the long-only swing framework this feeds are real
constraints, not oversights — but that structurally excludes the breakdown
half of the same pattern family, which the literature treats as symmetric.
Named in the docstring. Not addressed: this is a new engine (SDN already owns
intraday-short architecture — `can_short()`, the cover-deadline runway), not
a fix to this one.

### 5 — VERIFIED, INCLUDING THE FIXTURE THAT WAS WRONG

`tests/test_orb_retest_and_target.py`, 14 checks: 8 pure `_retest_and_held`
cases (probe-then-hold, no-probe, probe-with-no-retest, a retest that fails to
hold, a LATER bar breaking back below voiding an earlier good retest,
out-of-tolerance pullback, empty bars, multiple holding retests), 3 through
`evaluate()` end to end (weak break refused with the switch off, the SAME weak
break rescued with it on, a weak break with NO retest still refused even with
the switch on — the safety property), 3 for the target (widens, switch off
restores the flat multiple, and the honest counterpart proving it does NOT
widen at the shipped default, for the structural reason in §3).

Two of the three target tests failed on first run — not the code, the
fixture: `by_r > measured` under `orb_target_r=2.0` given the same stop-at-
range-low relationship, exactly the fact §3 records. Recorded because it is
the same "verify, never assert" discipline this project runs on, applied to a
test rather than to production code — a wrong assumption about how the two
numbers would compare would have shipped as two failing tests it might have
been tempting to loosen instead of understanding.

Six deliberate breaks demonstrated failing (both `_retest_and_held` guards,
`evaluate()`'s wiring to each, `max()` itself). `tools.verify`: 666 checks, 68
modules, green. `health.selects`/`sort_keys` unaffected.

### 6 — NOT DONE / COULD NOT DETERMINE

- **Whether either fix helps.** No post-fix ORB trade has been evaluated
  under either switch yet — both ship armed by explicit instruction this
  session, not because outcome evidence supports them; the evidence they can
  be checked against is the same clean, still-thin post-F-33 sample every
  other gate in this session is waiting on.
- **The 15-minute opening-range window itself** is untested against
  alternatives (5/30 min) — a single hardcoded default, not validated in
  this session.
- **Regime-split performance for ORB specifically** — named in §4, requires
  `regime_at_detection` to accumulate real rows under the corrected stop
  geometry before it is askable at all.


## 2026-08-19 — F-38 (change, full engine audit + SDN structural stop) — all 9 intraday engine files read against established practice for each pattern type. One correction owed to the operator (VWR is SHADOW, not ACTIVE — a real 16-Aug decision this session should have checked before asserting otherwise). One real, live gap found and closed: SDN was the one engine exempt from F-33's anti-falsification fix and this session's own min-risk floor, by construction. Two watch items named, not fixed: RNG's 100% stop rate at n=11, PDL's narrowed band against the new floor

**Ran:** `tools.verify` (671 checks, 68 modules), `tools.simulate`, `tools.health`,
direct schema queries against `system_config` with `updated_at` timestamps.

### 1 — A CORRECTION OWED, FOUND BY THE AUDIT ITSELF

Told the operator three turns earlier "every engine stays ACTIVE... no one
gets shadowed" without checking each engine's live lifecycle state first.
False. `intraday_engine_vwr_lifecycle = SHADOW`, set 2026-08-16 11:47:56 UTC.

Traced with timestamps rather than assumed: migration 083 (16-Aug, "VWR/ORB
shadow, flat conviction") set BOTH engines to SHADOW at 11:47:56. Two minutes
later, 11:49:59, `intraday_engine_orb_lifecycle` was updated back to ACTIVE —
someone reviewed the migration's own differentiated reasoning (VWR: −4.9 SE
on clean data, "no reading under which VWR is positive"; ORB: −2.4 SE on the
MOST CONTAMINATED data in the book, surviving its bound by 0.01R) and made
exactly the split call the file argued for. VWR's shadow stands on real
evidence, made four days before this session started. Not a gap. My earlier
blanket assertion was wrong to make without checking, and is corrected here.

### 2 — THE FULL AUDIT: ALL 9 ENGINE FILES, READ AGAINST ESTABLISHED PRACTICE

| Engine | Verdict |
|---|---|
| ORB | Fixed F-37 (retest arm, measured-move target) |
| GAP | Solid — already has the one-sided-bound fix (NATIONALUM incident, cited). No gap. |
| PDL | Most rigorously hardened file in the package — three real incidents already fixed and documented. No gap. Watch: own cap 0.80%, new floor 0.6% — a narrow 0.6-0.8% band. |
| PBK | Solid, real incident history (day-high target, invalidation_level meta). No gap. |
| RNG | Design sound; a real bug (inverted invalidation level, "RNG has never completed a trade") already fixed 12-Aug. **Live result since: n=11, 100% STOP.** Too thin to diagnose — watch, not fix. |
| VWR | SHADOW since 16-Aug, correctly — §1. Code itself is excellent. |
| VCE | Already has the measured-move target ORB was missing. No gap. |
| GDB | Solid, data-driven origin (`brain_proposals#190`, cited), ACTIVE. |
| SDN | **One real gap — §3.** |

No grep-able "unbuilt/TODO"-style marker existed on any engine but ORB —
every other file's gaps (if any) needed reading against practice, not
searching for a stale comment a second time.

### 3 — SDN NEVER CALLED THE FUNCTION EVERY OTHER ENGINE CALLS

`base.risk_from_structure()` is where F-33's anti-falsification fix lives and
where `intraday_min_risk_pct` (armed this session) actually gates. SDN built
its stop directly in three places (`level * (1 + buffer)`) and called neither.
Consequence: SDN — the large majority of this book's live volume — was the
one engine exempt from both, by construction, not by choice, and had no
explicit maximum-risk cap at all, only the indirect bound `_not_chasing()`'s
distance-to-level check happens to produce.

Routed all three conditions through `risk_from_structure(ctx.ltp,
structural_stop, "SHORT", max_risk_pct=cfg_float("intraday_short_max_risk_pct",
1.50))`. Confirmed `risk_from_structure(..., "SHORT", ...)` is already
exercised at the base-function level (`tests/test_structural_stop.py`) before
wiring a caller to it.

**A real, measured live consequence, not hypothetical.** On this project's
own "trap"/"vwap_reject" fixtures: risk_pct 0.573% and 0.576% — both now
refused by the 0.6% floor once it reaches SDN. `breakdown` (0.645%) is
unaffected. Recorded plainly rather than tuned away: the floor engaging on
two of SDN's own three conditions in their default shape is the floor doing
exactly what it was armed to do, and it should be watched over the next few
sessions, not assumed benign from the earlier "7 of 265" reassurance — that
number describes SDN's UN-gated history, not what happens now that the gate
is live.

**`intraday_short_max_risk_pct=1.50`, set above SDN's own best band, not at
it.** SDN's measured n=80 "wide" bucket (>=0.9% risk) is its BEST, +0.442R —
the opposite of every long engine, where wide means worse. Setting the cap
near that band would have cut SDN's strongest cohort in the name of
"symmetry" with engines it is not symmetric with.

**Found while testing, not assumed: this cap is mostly a secondary gate
today.** `_target()`'s ATR-capped reward (~1.2% of price at this account's
settings) combined with `intraday_short_min_rr` (1.3) already refuses
anything wider than roughly 0.9% risk before the new cap has a chance to
bind. A first test built an 8.7%-risk fixture specifically to isolate the cap
and it stayed refused even at cap=20% — not a test bug, a real property,
recorded in the test itself rather than quietly reworked away. The cap is
still worth having: defense in depth against a future change to the R:R
floor or target multiplier, exactly the posture this project took with the
redundant SHADOW-engine guard in F-35.

**One side fix, found while giving this a single auditable stop construction
instead of three bespoke ones.** `_trap`'s old stop —
`min(day_high, prev_high * buf) * buf` — applied a SECOND buffer to the
prev_high branch whenever it won the min (buf applied once going in, once
more coming out). Small (buf is 0.12%, so ~0.024% of stacked, unintended
buffer) and now applied exactly once per branch, matching what the comment at
that stop has always said it does. Verified both branches directly: the
tighter (day_high-wins) and the wider (prev_high-wins) case each land on the
single-buffered value, not the old double-buffered one.

`**frame.meta()` merged into all three conditions' `Setup.meta`, matching
every sibling engine — empty under the default `refuse` mode, present for
consistency and for the day `intraday_stop_cap_mode=tighten` is ever armed.

### 4 — VERIFIED

`tests/test_short_engine.py`, 5 new checks: the floor reaching SDN on real
project fixtures (and its off-counterpart restoring exactly what the
pre-existing three-conditions test expects), the cap refusing a stop wider
than itself and passing the shipped default, both `_trap` buffer branches
landing on the corrected single-buffer value, and `frame.meta()` reaching
every condition (source-inspected, not just one instance). Three deliberate
breaks demonstrated failing: the refusal path removed, `_trap` reverted to
the double-buffer formula, `frame.meta()` dropped from one condition.
`tools.verify`: 671 checks, 68 modules, green. `health.shorts` unaffected
("all 27 direction-aware sites present"); `health.selects`/`sort_keys`
unaffected (no new queries added).

### 5 — NOT DONE / COULD NOT DETERMINE

- **Whether the floor engaging on TRP/VWR meaningfully cuts SDN's real
  volume.** The two synthetic fixtures sitting at 0.573%/0.576% are simple,
  illustrative shapes built to test the chase rule — not calibrated to match
  the true live distribution. The real historical data (265 rows, only 7
  ever under 0.6%) argues most live detections already clear it naturally,
  but that number describes the UN-gated history. Needs watching on real
  post-fix sessions, not inferred from either number alone.
- **RNG's 100% stop rate (n=11)** — named again, still not diagnosed. The
  sample is too thin to separate a real problem from noise, and guessing at
  a fix here would be exactly the "cleverness, not evidence" mistake this
  project's own hurdle.py docstring already paid for twice.
- **PDL's narrowed 0.6-0.8% permissible band** against the newly-armed floor
  — a real interaction, not yet measured against a live session.
- **Gate 3 remains unmade** for every engine but VWR (already decided) and
  ORB (provisionally reverted, per §1). Nothing here retires, shadows, or
  promotes RNG, PDL, PBK, GAP, GDB, or VCE.


## 2026-08-20 — F-39 (correction + change, sub_engine overwritten since introduction) — registry.evaluate_all()'s own comment says sub_engine is "which condition actually fired"; the line under it overwrote every engine's value with the family name. Harmless for eight engines whose condition and family are the same word; silently destroyed SDN's three-way VWR/TRP/ORB distinction on every row it has ever written. Found live, same session, trying to answer the operator's own question about it

**Ran:** `tools.verify` (674 checks, 68 modules), `tools.health` (selects strict,
shorts unaffected), live schema check on `allocation_decisions`.

### 1 — HOW THIS SURFACED

Asked (1) whether SDN's confidence inversion is sharper split by its own
condition (VWR/TRP/ORB) rather than pooled, and (2) why GAP's good day
couldn't be read apart from ORB's. Both needed `meta.sub_engine` to hold what
its own comment already claims it holds. Querying it directly: every one of
SDN's historical TAKEN rows reads `sub_engine="SDN"` — never "VWR", "TRP", or
"ORB" — despite `short_distribution.py`'s three methods explicitly setting
exactly those three values.

`registry.py:249`, before: `s.meta["sub_engine"] = s.strategy`. Its own
comment two lines up: *"sub_engine is which condition actually fired."* The
code did the opposite — unconditionally overwrote whatever the engine had
set with `s.strategy`, which for SDN is always "SDN" (the class-level name
every one of its three Setup constructions carries), regardless of which
condition produced it.

### 2 — BLAST RADIUS, MEASURED NOT ASSUMED

Every engine but SDN has exactly one condition, so `sub_engine == strategy`
was already the honest answer for ORB, GAP, PDL, VCE, PBK, RNG, VWR, GDB —
the overwrite was a no-op for all eight. SDN is the only engine built as
three conditions inside one class, and it is the one this line has been
silently flattening since `sub_engine` was introduced.

**Consequence, and it reaches further than the two questions that found
it.** F-33's own §5 named the real repair for SDN's confidence inversion:
*"needs the per-condition split (VWAP-rejection vs trap vs breakdown) this
table does not separate."* That split was never possible — not because the
data was thin, but because the column meant to carry it was being
overwritten before the row was ever saved. Worse: this session's own
banded-prior and arbitration machinery (`allocator._prior_for`,
`expected_r_for`) already reads `meta.sub_engine` to key SDN's priors — both
have been silently pricing every SDN condition as one pooled family the
entire time they have existed, never once reaching the per-condition
resolution they were built for. Neither was caught by this session's own
tests, because every test fixture constructs `meta={"sub_engine": ...}`
directly rather than running the real detection through `registry.
evaluate_all()` — which is exactly where the overwrite lived.

Fixed: `s.meta.setdefault("sub_engine", s.strategy)`. Sets the family-as-
fallback for any engine that provides nothing of its own; never overwrites
one that already has.

**Historical rows are NOT backfilled.** Every SDN row written before this
fix still reads `sub_engine="SDN"` and cannot be un-mixed after the fact —
the three conditions' outcomes were pooled at write time, not merely
mislabelled at read time. Per-condition SDN analysis is possible only from
here forward.

### 3 — SEPARATELY: `allocation_decisions` COULD NEVER SHOW GAP APART FROM ORB

`source` on that table is the FAMILY (`proposal.from_intraday` sets it that
way deliberately, for the prior fallback ladder) — so GAP/PDL/ORB and
PBK/VWR have never been distinguishable in the one table every allocator
decision is logged to. Found trying to check whether GAP's good 20-Aug
session reflected a genuinely better prior than ORB's — no query could
answer it.

`Allocator._record()` now copies `p.meta.get("sub_engine")` through as its
own column, mirroring what the prior ladder already reads. **Column added
BEFORE the code that writes it** — `_record()`'s output goes through a raw
`.insert()` with no unknown-column resilience (unlike `_upsert_position`'s
strip-and-retry), so shipping the field first would have silently failed
every allocator flush this session's own landmine list already warns about.
Migration 088 adds it; verified present via live schema query before the
code path could ever reach it.

### 4 — VERIFIED

Three new checks in `tests/test_short_engine.py`: sub_engine surviving the
REAL `registry.evaluate_all()` path (not `ShortDistribution()` called
directly, which is where the earlier tests in this file — and this bug —
lived unnoticed), the no-regression case for single-condition engines, and
`_record()` carrying `sub_engine` through separately from `source`. One
deliberate break (revert `setdefault` to unconditional overwrite) detected.
`tools.verify`: 674 checks, 68 modules, green. `health.selects` strict-passes
(confirms the new column is real); `health.shorts` unaffected.

### 5 — WHAT THIS CHANGES ABOUT EVERYTHING ELSE THIS SESSION SAID

The per-condition SDN confidence split proposed as "the fix" for today's
inversion (three highest-confidence trades all losses, three lowest all
wins) is now buildable for the first time — it was not buildable before this
fix landed, regardless of how much data existed. Same for confirming whether
`alloc_intraday_confidence_bands` (still inert) would actually differentiate
SDN's three conditions once armed: before this fix, arming it would have
banded confidence WITHIN one undifferentiated "SDN" pool exactly as `_prior_
for` already keys it — the band and the condition-mixing problem were
independent defects, and only one of them was being addressed.

### 6 — NOT DONE / COULD NOT DETERMINE

- **No per-condition SDN measurement was run yet.** Clean, correctly-labelled
  sub_engine data only exists from the moment this fix deploys — zero rows
  as of writing.
- **Whether `alloc_intraday_confidence_bands` should now be armed.** Still
  gated on a clean, sufficient, NOW-correctly-labelled sample — further
  behind than previously believed, not closer.
- **`priors_intraday_since`** — the reset lever named to the operator this
  session — was discussed but not armed. Remains the operator's call.


## 2026-08-20 — F-40 (change, JSON double-encoding on two jsonb columns) — every row this project has ever written to intraday_setups.meta and allocation_decisions.hurdle_inputs stored a JSON STRING inside a jsonb column, not a JSON OBJECT. Found live, not from a comment, while trying to verify F-39's own fix actually reached the database. One defensive reader survived it by accident; every plain-SQL diagnostic this session ran against either column returned NULL, not absence

**Ran:** live insert+select+delete against `intraday_setups` (real DB, real
verification, immediately cleaned up), `tools.verify` (675 checks, 68
modules), `tools.health` (selects strict).

### 1 — HOW THIS SURFACED, DIRECTLY FROM THE OPERATOR'S OWN NEW INSTRUCTION

Asked to verify F-39's sub_engine fix against real code and live data rather
than trust it. Tried to read `meta->>'atr_pct_daily'` on today's TAKEN rows
via plain SQL to cross-check the ORB retest/measured-move mechanisms — got
NULL on every row. `jsonb_typeof(meta)` returned `'string'`, not `'object'`,
on every checked date back to 18-Aug. The Python client the live daemon
actually uses was checked directly, not assumed: `type(row['meta'])` is
`str`, confirming this is not a SQL-only artifact.

### 2 — ROOT CAUSE, FOUND BY READING THE WRITE CODE, NOT A COMMENT

`intraday/engine.py::_record_setup`: `"meta": json.dumps({**s.meta, "qty":
qty}, default=str)`. `allocation/allocator.py::_record`: `"hurdle_inputs":
json.dumps(v.get("hurdle_inputs") or {}, default=str)`. Both columns are
jsonb; the Supabase client already serializes a native dict into jsonb
correctly. Pre-serializing to a string first means the client stores THAT
STRING as the jsonb value — a JSON string scalar, not an object — and every
`->>'key'` extraction against it, in SQL or in a client that doesn't already
defend against it, returns NULL regardless of whether the key exists.

**One function survived this by construction, not by design intent.**
`allocation.scoring._engine_of()` already does `if isinstance(meta, str):
meta = json.loads(meta)` before reading `sub_engine` — written, per its own
history, to tolerate meta arriving as a string for other reasons. That
defensive line is the entire reason the live prior-building and
arbitration pipeline (which depends on `_engine_of`/`_family_of_row`) has
been reading `sub_engine` correctly this whole session despite the
underlying column being wrong. No other reader — including every ad-hoc SQL
query this session has run, and `hurdle_inputs`, which has no equivalent
defense anywhere — had that protection.

### 3 — FIXED AT BOTH WRITE SITES

`json.loads(json.dumps(x, default=str))` in place of `json.dumps(x,
default=str)`. Same `default=str` sanitising for anything non-JSON-native
in the dict (numpy floats, Decimal, datetime); the round trip lands on a
plain dict, which the client then serializes as a native object exactly
once, correctly.

**Verified against the real database, not assumed from the code change
alone.** Inserted a disposable row (`symbol='ZZFIXTEST'`) using the fixed
construction, confirmed `jsonb_typeof(meta)='object'` and
`meta->>'sub_engine'` resolves directly with no defensive re-parse needed,
then deleted the row. This is the standard this session has held every
other change to — F-39's own fix and this one both needed a live check, not
a code-reading, to be trusted.

**One stale test caught and corrected.** `test_paper_entry_verdicts.py`'s
own assertion `json.loads(sb.inserted[0]["meta"])` was written against the
bug and passed BECAUSE of it — a mocked insert path, not the real database,
so it never surfaced the double-encoding, only exercised code that assumed
it. Updated to assert `meta` is a dict directly.

### 4 — VERIFIED

One new check (`hurdle_inputs` is a dict at the point `_record()` hands it
to the client) plus the live DB round-trip above for `meta`, which is not
unit-testable offline — `_record_setup` calls `.execute()` inline with no
injected client. One deliberate break (revert `hurdle_inputs` to
`json.dumps()` alone) demonstrated failing. `tools.verify`: 675 checks, 68
modules, green. `health.selects` strict-passes.

### 5 — NOT DONE / COULD NOT DETERMINE

- **`notifier.py:289`** (`"meta": json.dumps(a.meta) if a.meta else None`,
  the alerts table) has the identical shape and was not checked live in
  this pass — named, not assumed fixed, not assumed broken.
- **Historical rows are not backfilled**, on both columns. Every diagnostic
  this session ran against pre-20-Aug `meta`/`hurdle_inputs` content was
  reading a JSON string, not an object — conclusions drawn from raw SQL
  against those columns before this fix should be re-checked, not trusted,
  if they mattered.
- **Whether any OTHER consumer, beyond `_engine_of`, silently swallowed an
  exception reading `meta` as a dict** was not audited exhaustively. Given
  `_engine_of` is the one function the prior/arbitration/banding pipeline
  actually routes through, this is believed to be the only load-bearing
  path — not independently confirmed for every reader in the codebase.


## 2026-08-20 — F-41 (change, naming collision) — SDN's internal condition
labels "VWR" and "ORB" collided with the standalone long engines of the same
name in `sub_engine`; renamed to VREJ/BRKD

**Ran:** `tools.verify` (675 checks, 68 modules, green), `tools.health` (all
green except a pre-existing, unrelated `quote_parity` drift — not touched by
this change, not introduced by it).

### 1 — WHAT SURFACED IT

Reviewing SDN's three-condition structure after F-39's sub_engine fix, in
preparation for a per-condition confidence/prior split. `short_distribution.py`
writes `sub_engine` values `"VWR"` (VWAP rejection), `"TRP"` (the trap), `"ORB"`
(range breakdown) — but two of those three strings are ALSO the `strategy`
names of two unrelated, standalone LONG engines: `vwap_reclaim.py` (VWR) and
`orb.py` (ORB). Any query, dashboard, or future per-condition prior that
groups by `sub_engine` without ALSO checking `strategy`/`family` cannot tell
SDN's VWAP-rejection SHORT from the standalone VWR engine's mean-reversion
LONG, or SDN's breakdown SHORT from the standalone ORB engine's breakout LONG.
`_engine_of()`/`_family_of_row()` (the one path that reads `sub_engine` today)
key off `INTRADAY/<family>/<sub_engine>` — family is `SDN` for all three SDN
conditions and `ORB`/`VWR` for the standalone engines, so the CURRENT
allocator pipeline does not actually collide (the family prefix disambiguates
it). The risk was entirely in future/ad-hoc use: any confidence-band key,
weekly-review breakdown, or raw SQL grouped on `sub_engine` alone, without the
family qualifier, silently merges two unrelated strategies. Confirmed via grep
that nothing today pattern-matches these specific SDN string values outside
of dict-key/label context — this was a landmine being defused, not a live bug
being fixed. No allocator/prior mechanism produced a wrong number because of
this; it is closed before it became one, per the operator's "fix it
holistically" instruction.

### 2 — FIX

`short_distribution.py`: `_vwap_rejection`'s `meta["sub_engine"]` "VWR" →
"VREJ"; `_range_breakdown`'s "ORB" → "BRKD"; `_trap`'s "TRP" unchanged (no
collision — "this one has no long equivalent worth trading", per the module's
own docstring). Module docstring's "THE THREE CONDITIONS" section headers
updated to match, plus a note dating the rename. `registry.py`'s F-39 comment
(describing the historical overwrite bug) left as an accurate historical
record, with an addendum noting the same-day rename and that pre-rename rows
still read the old labels.

**Two test fixtures that hardcoded the actual SDN condition strings** (not
the unrelated standalone-engine ones) updated to match:
`test_short_engine.py::test_all_three_conditions_fire_on_their_own_shape`
(asserted `{"TRP","VWR","ORB"}`, now `{"TRP","VREJ","BRKD"}`) and its sibling
`test_frame_meta_reaches_every_conditions_setup`; and
`test_break_confirmation.py::test_sdn_range_breakdown_uses_the_low_it_broke`'s
fixture. Every other hit on the strings `"VWR"`/`"ORB"` in the codebase
(`vwap_reclaim.py`, `scoring.py`'s regime map, five more test files) checked
individually and confirmed to be the unrelated standalone engines, not SDN —
left untouched.

**Not touched:** migration 088's SQL comment, which describes the F-39 bug as
it stood at the time it was written (`sub_engine` WAS "VWR"/"TRP"/"ORB" then)
— an applied migration is a historical record, not live code; rewriting it
would misrepresent what that migration actually did when it ran.

### 3 — VERIFIED

`tools.verify`: 675 checks, 68 modules, all green — same count as before the
rename (no test added or removed, only string literals inside existing
assertions changed). `tools.health`: all green except `quote_parity`, which
this change does not touch (a live-quote-vs-historical field drift, flagged
separately, not chased in this pass).

### 4 — WHAT THIS DOES NOT CHANGE

Historical `intraday_setups.meta.sub_engine` rows written before this commit
still read `"VWR"`/`"ORB"` for SDN's conditions. Any future per-condition
query spanning both sides of this rename must handle both spellings, or
filter on `trade_date` first. The allocator's live behaviour is unchanged —
`_engine_of`/priors/arbitration key off `family` + `sub_engine` together, and
`family` was never ambiguous; this closes a landmine in anything that reads
`sub_engine` alone, before it could cost anything.


## 2026-08-20 — F-42 (change, missing floor date) — the BAR's own arrival
population had no equivalent of `priors_intraday_since`; added
`alloc_hurdle_since` with the identical contract and armed it to 2026-08-20

**Ran:** `tools.verify` (679 checks, 69 modules, green — 4 new), one
deliberate break-then-fix demonstration, `tools.health` (green except the
pre-existing, unrelated `quote_parity`), live config confirmed via `cfg()`.

### 1 — WHAT SURFACED IT

Operator's own question, following the correction on F-41's "1,062 TAKEN"
number: *"bar's historical trades were before we put TradeOS in place so
should we not reset bar too?"* Read as "before the fixes were in place," this
was worth checking against the actual code rather than answered from memory.

### 2 — ROOT CAUSE, CONFIRMED BY READING THE CODE

`scoring.intraday_priors()` already has `priors_intraday_since` — a hard
floor date stopping the per-ENGINE prior from averaging across a structural
change (F-33's 18-Aug stop-clamping fix). `hurdle._empirical_base()` builds
the BAR from a *different* table, `allocation_decisions.edge` — but that
column is COMPUTED at write time using whatever engine prior was in force
that cycle, so it inherits the identical contamination. It had no floor of
its own: only `alloc_hurdle_lookback_days`, a 90-day ROLLING window. Checked
live: `allocation_decisions` for INTRADAY only goes back to 2026-08-05 (15
calendar days), well inside that 90-day window — every fix that shipped this
month (F-33 on 18-Aug, F-39/F-40 on 20-Aug) sat inside the bar's own
population with nothing to say so.

### 3 — FIX

`alloc_hurdle_since` added to `_empirical_base()`, same `since = max(rolling,
floor)` arithmetic as `priors_intraday_since`. Migration 089, armed to
`2026-08-20` in the same migration (not shipped inert) — the risk profile is
different from the per-engine floor: that one can take days to clear its
30-sample floor per engine; this one clears `alloc_hurdle_min_sample` (40)
from a single trading day's volume (~1000+ INTRADAY rows/session, confirmed
live today). Arming it mid-session forces `settled_n` to 0 for the rest of
TODAY specifically (today's own rows are never "settled" evidence — see
hurdle.py's 10-Aug comment on why), pushing the bar to `-inf` (fully
permissive) until the next trading day — but the market is already closed
for today, so the practical cost of arming now is zero, and from tomorrow
the bar computes cleanly off post-fix data alone.

### 4 — VERIFIED

New file `tests/test_hurdle_since.py`, 4 checks: unset behaves exactly as
before (regression guard), a floor date inside the rolling window wins, an
ancient floor date does NOT loosen the window (guards `max()` against a
future accidental `min()`), and a blank string is treated as unset (matches
how the migration ships the key). Demonstrated failing first: neutered the
`floor_date` line, watched `test_floor_date_later_than_the_rolling_window_
wins` fail with the un-floored rolling date instead of 2026-08-20, restored
the fix, re-ran green. `tools.verify`: 679 checks / 69 modules. Live config
confirmed via the app's own `cfg()` reader, not raw SQL alone.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

**This ships in code only — not yet on the running daemon.** Same gap as
F-39/F-40/F-41: the live daemon is still on commit `8e1b673`, predating all
four of today's fixes. Arming `alloc_hurdle_since` in `system_config` has no
live effect until `hurdle.py`'s wiring is deployed too. The config value is
already armed in the database regardless, so the moment the daemon is
updated, this takes effect immediately with no further action needed.

Historical `allocation_decisions` rows before 2026-08-20 are not deleted or
relabeled — they simply stop being read by `_empirical_base()` once this is
live. `hurdle_population_audit.py` and any other tool reading that table
directly still sees the full, unfiltered history.

## 2026-08-20 — F-43 (change, full swing-book review) — reviewed the last 15
closed swing trades against a live-trading-desk lens (entries, exit ladder,
sizing, ranking). Five findings, all from real data: the give-back guard was
doing 100% of the book's profit-taking at a flat 50% because the ladder above
it was priced for a distribution this book never produces; every swing exit
placed by the daemon (nearly all of them) was unlabelled, "BROKER_EXIT",
because the 15s loop never stamped `exit_signal`; the evening pipeline's own
R:R refusal was coded 18-Aug and never armed, so 5 of 6 open positions were
entered on plans the pipeline itself had refused that same night; TRAVELFOOD
entered 14-Aug at composite rank -16 because the only rank check in the swing
order path is a RELATIVE top-N gate that switches itself off once the
allocator is live; and the book was fragmented across 6 small positions
(avg. Rs.2,989) paying a flat Rs.15.04 DP fee each, 36.6% of gross profit
in charges over 15 trades.

**Ran:** `tools.verify` (690 checks, 70 modules, green — 11 new, in
`tests.test_f43_swing_review`, each demonstrated FAILING against the pre-fix
source via `git stash` before being trusted to pass — see §4).
`tools.simulate` (read-only, confirms live): HINDCOPPER, the one position
currently past 1R, now shows `EXIT_GIVEBACK` — "peaked at 1.44R (+9.96%) and
is back to +1.00R (+6.96%) — 30% of the move handed back" — where the
pre-fix flat 50% guard would have stayed HOLD. No intraday-framework file
(strategies, exit_policy.py, gates, registry) touched.

### 1 — WHAT SURFACED IT

Operator's request: evaluate the swing framework end to end from a live
professional swing-trader's perspective, using the last 10-15 days of actual
closed and open trades as evidence rather than reading the code's own
comments at face value.

### 2 — FIVE FINDINGS, EACH CONFIRMED AGAINST LIVE DATA

**(a) The exit ladder was priced above where the book's trades actually
peak.** Reconstructing peak-R for the 13 closed swing trades that carry a
favourable-excursion figure: median 0.67R, only 2/13 ever cleared 1.0R, 1.5R
reached once. The ladder's rungs (partial 1.5R, breakeven 1.0R, trail-on
2.0R) were mostly unreachable, so `exit_giveback_pct` — built as a
loss-prevention backstop, not a profit-taking rule — ended up the only rung
any winner ever touched: 7 of the last 9 winning exits gave back within 1-6%
of exactly half their peak.

**(b) Every swing exit the daemon placed carries no rule label.** All 14
recent `closed_positions.exit_reason` rows for SWING read `BROKER_EXIT`.
`manage_open_positions()` (the once-a-day batch path) stamps `exit_signal`;
the 15s daemon loop, which places nearly every real exit, never did —
confirmed by grep, zero occurrences of `exit_signal` in `intraday/engine.py`
before this fix. `weekly_review`, `post_trade_analysis` and `exit_audit` all
group by `exit_reason`; the most heavily reasoned exit logic in this project
had no feedback loop.

**(c) The pipeline's own refusal was coded and never armed.** Five of six
open swing positions (CARBORUNIV, TRAVELFOOD, HINDCOPPER, TATATECH,
AARTIIND) carry `filter_reason = insufficient_rr_0.78x` (or similar) on the
`signal_output_daily` row for their entry date. `entry_ranking.
entry_refusals()` has honoured `filter_reason` since the 18-Aug GABRIEL fix,
gated on `entry_respect_filter_reason` — which does not exist in
`system_config`, so `cfg_bool` silently defaulted to `False`. The GABRIEL
gap, reopened by an unset switch rather than a missing check.

**(d) The only rank check in the swing order path is relative, and it
switches itself off.** `_legacy_rank_gate_blocks` is a top-N-of-today gate,
correctly disabled whenever `alloc_live_swing` is true (it is) because the
allocator's edge is then the live veto. But edge is an opportunity-cost
question — better than what else arrived this cycle — not a verdict on a
plan's own quality, and `act_on_candidates()` sends every candidate outside
the top `swing_alert_top_n` contenders straight to `_maybe_enter_swing` with
no rank check of any kind. TRAVELFOOD entered 14-Aug at composite rank -16.

**(e) The book is fragmented finer than the account's cost structure
supports.** Average position Rs.2,989 against a Rs.6,000 order-value cap
that was never binding; `risk_pct_per_trade` (1% of capital) was the actual
constraint given 5-9%-wide ATR stops. Charges were 36.6% of gross profit
over 15 closed trades (Rs.218.57 of Rs.597.29). `_replacement_case()` — the
"swap a weak holding for a better one" logic — sends an alert only; nothing
in the codebase executes a rotation. `STRATEGY ROTATION` appears 12 times in
`closed_positions.exit_reason` with no writer anywhere in the codebase — it
is a manual label the operator has been entering by hand.

### 3 — FIX

(a)+(b) reprice, in `control/position_lifecycle.py::load_exit_policy()`:
`exit_partial_book_r` 1.5→1.0, `exit_breakeven_at_r` 1.0→0.5,
`exit_trail_after_r` 2.0→1.5, `exit_trail_r` 1.5→1.0,
`swing_setup_target_min_r` 1.0→0.6. The give-back guard is tiered rather
than replaced: below the new `exit_giveback_runner_min_r` (1.0R, matched to
the repriced partial) it stays at the existing loose 50% — the only thing
protecting a winner before a partial can fire — and at or above that line it
tightens to `exit_giveback_pct_runner` (30%), because a partial should
already be banked and the stop at breakeven by then, so what remains is
risking profit, not capital. `intraday/engine.py::_auto_exit` now stamps
`exit_signal = d["reason"]` on every EXIT_* order it places, matching the
convention `manage_open_positions()` already used.

(c) migration 090 arms `entry_respect_filter_reason = true`. No code change
— `entry_refusals()` was already correct and already tested
(`tests/test_gabriel_gap.py`).

(d) new pure function `intraday/engine.py::_rank_floor_blocks`, called from
`_maybe_enter_swing` unconditionally (independent of `alloc_live_swing` and
of the relative gate), reading `swing_min_rank_to_enter` (migration 090,
default 0). Refuses a plan on its own composite score regardless of daily
quota room or what the allocator scored it on a different axis.

(e) migration 090: `risk_pct_per_trade` 1.0%→1.5%, `max_positions_neutral`
6→4, `max_position_pct` 20%→25%, `swing_max_order_value` 6000→8000,
`swing_max_new_per_day` 3→2 (also closes a docs-vs-config drift —
`0_SYSTEM_BLUEPRINT.md` had always documented 2). Net effect: SAME total
deployed risk (4 × 1.5% = 6%, same as the old 6 × 1.0% = 6%, against an
unchanged 8% portfolio ceiling) concentrated into fewer, larger positions
instead of split thin enough for transaction costs to dominate the
outcome. Auto-rotation (closing the mechanism behind (e)'s alert-only swap
logic) was deliberately NOT built this session — a new axis on which money
would move automatically is exactly the kind of change this project's own
"propose, never auto-apply" / staged-gate discipline exists for, not
something to ship inside a same-session bundle of bug fixes.

Also added: `analysis/trade_decision.py::regime_min_rr()` — the evening
pipeline has computed `min_rr_to_enter_<REGIME>` since before this session
(NEUTRAL 1.0, RISK_OFF 1.5, etc.) but `decide()`'s two live-daemon call
sites never read it, entering every regime at the same flat 1.0R bar. Wired
at both (`evaluate_candidates()`, the real entry gate, and `_log_swing_
state()`, the preview line, so the two cannot disagree).

### 4 — VERIFIED

New file `tests/test_f43_swing_review.py`, 11 checks, registered in
`tools.verify::MODULES`. Demonstrated failing first per this project's
standing rule: `git stash push --keep-index` on the three touched source
files (keeping the new test file staged), re-ran the 11 checks against the
UNFIXED source — 9 of 11 failed (2 import errors for functions that did not
exist yet, `regime_min_rr` and `_rank_floor_blocks`; 3 behavioural failures
showing the OLD ladder holding where the new one must act) — then `git
stash pop` restored the fix and all 11 passed. `tools.verify`: 690/690.
`tools.simulate` (read-only, live data): HINDCOPPER — the one open position
past 1R — now correctly returns `EXIT_GIVEBACK` where the pre-fix code
would have returned `HOLD` (peak 1.44R, now 1.00R, 30.3% given back; the old
flat 50% limit would not have tripped until 42.8% given back — a 40%-worse
outcome).

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

**Migration 090 was blocked by the auto-mode classifier from being applied
directly to the live database** — parameter changes to a live-trading
account's risk config are not something this session pushes through
unattended. The SQL is written (`db/migrations/090_swing_exit_reprice_and_
entry_discipline.sql`) and needs the operator's own hand or explicit
in-session approval to run. Until it runs, `entry_respect_filter_reason`,
`swing_min_rank_to_enter`, and the sizing keys (`risk_pct_per_trade`,
`max_positions_neutral`, `max_position_pct`, `swing_max_order_value`,
`swing_max_new_per_day`) stay at their PRE-fix values, because `cfg()`
always prefers an existing database row over the code's own default. The
two new give-back keys (`exit_giveback_pct_runner`,
`exit_giveback_runner_min_r`) are the exception — they had no prior row, so
the code's new defaults (30%, 1.0R) are already live, which is what let
`tools.simulate` catch HINDCOPPER above without the migration.

**Same gap as every recent finding: this ships in code only, not yet on the
running daemon.** `tools.simulate` imports the current source directly and
reflects every fix; the long-running `intraday/run.py` process does not
pick any of it up until it is restarted. Market is closed for the day the
data in this entry was pulled, so the practical cost of that gap is zero
until the next session — but the daemon must be redeployed before market
open for any of this to protect the book live.

**Auto-rotation was scoped and deliberately not built** — see §3(e). Left
as a named gap for a future, separately-gated session, in the style this
project's own roadmap uses for anything that would let capital move on a
new axis.


## 2026-08-20 — F-44 (correction + new tool) — give-back guard was ALREADY
armed (I told the operator otherwise), re-verified on fresh data; new
`tools.feature_edge_study` mines per-setup FEATURES against outcome, wired
into the weekly chain, first live run raised 42 findings

**Ran:** `tools.exit_ladder_replay` (grid sweep + a fresh winners/losers
quantile check), `tools.feature_edge_study` (live, `--since 2026-08-11`,
42 findings written), `tools.verify` (707 checks, 71 modules, green — 17
new).

### 1 — CORRECTION: THE GIVE-BACK GUARD WAS NEVER OFF

Told the operator this session that `intraday_giveback_pct` "ships OFF,
worth arming" — based on reading `exit_policy.py`'s in-code DEFAULT
(`cfg_float("intraday_giveback_pct", 0.0)`), not the live `system_config`
value. The live value is `50.0`, armed in migration/commit `0cdd3c8`
("Enable the intraday give-back guard") from a session before this one.
This is exactly the class of mistake the operator's own standing
instruction exists to catch — read the code that decides, not the
fallback that only matters if nobody set it. Correcting the record rather
than letting it stand.

### 2 — RE-VERIFIED THE ARMED THRESHOLD ON FRESH, POST-FIX DATA

The original calibration (11-Aug, n=27 closed positions) is now a small
fraction of what exists. Pulled 90 days of closed INTRADAY positions and
found 22 of 94 carry `high_water_mark == entry_price` exactly — every one
dated 29-Jul to 3-Aug, before the engine started maintaining MFE at all
(the same gap the 11-Aug comment names: "the engine never maintained it").
Excluded that pre-fix residue (floor at 2026-08-11) rather than let stale
zeros pull a live threshold.

**Clean sample, n=51 (21 winners / 30 losers):** winners' peak_r runs
0.25-1.50R; losers who reached a real peak top out at 0.56R. Less clean
separation than the original n=27 read ("coincide almost exactly") — a
real, honest finding: more data reveals more of the true overlap, not
less. `giveback_min_r=0.5` still sits inside the gap (above 28 of 30
losers' peaks, below 15 of 21 winners' — the ones above it are protected
by trail/breakeven regardless). A grid sweep (`exit_ladder_replay --pct
{30,40,50,60} --min-r {0.5,0.75,1.0}`) shows every combination estimating
a positive ceiling, monotonically larger at tighter thresholds — flagged
rather than chased, since the tool's own docstring names that as its
overstatement bias (a trigger-happy guard "wins more races" in an
estimate that cannot see rung order). **No config change** — 50%/0.5R
remains defensible on the fresh data; re-run
`exit_ladder_replay` again once the daemon has been on tonight's fixes
for a few weeks.

### 3 — NEW: `tools/feature_edge_study.py`

Every prior/allocator mechanism in this project answers "is this ENGINE
worth a trade" — one number per engine, blind to whether the specific
candidate is the engine's best work or its worst. This asks the question
a discretionary trader asks constantly: of the trades actually taken,
what did winners have in common that losers didn't? Mines `volume_ratio`,
`atr_pct_daily`, `confidence`, `sector`, `regime_at_detection`, and
hour-of-detection (OPEN/MID/LATE) against realised TARGET/STOP outcome,
per (engine, sub_engine) — reusing `scoring._engine_of()` rather than a
second definition.

**Same discipline as `discover_engines.py`, deliberately reused, not
reinvented:** propose-never-apply, writes `brain_proposals` with
`proposal_type='FEATURE_FILTER'`, a sample floor per engine (40) and per
segment (15) before any split is even attempted, and a reporting bar
(20pp win-rate gap OR 0.15pp mean-outcome gap) high enough that a flat
relationship reports nothing — demonstrated in tests, not assumed.
Terciles (bottom third vs top third, middle dropped), matching the exact
convention this project's own 19-Aug confidence-band measurement already
used, rather than a fresh median-split invention.

**One bug caught before this went live, not after:** the first version
keyed a `brain_proposals` dedup on `f"{engine}/{feature}"` alone. A
categorical feature (sector) that fires for MULTIPLE categories in one
pass — SDN disliking both "i.t." and "metals & mining" — collided on that
one key, and each subsequent category's `_propose()` call overwrote the
PENDING row the previous category had just written, silently discarding
every finding but the last one processed for that feature. Caught by
inspecting the live run's actual written rows (42 findings logged, first
attempt would have left far fewer than 42 distinct rows in the table),
not by reading the code and assuming it was right. Fixed via
`target_key_for()`, which folds the category into the key when present;
re-ran live and confirmed 42 of 42 written rows have distinct
`target_key`.

**First live run, `--since 2026-08-11`** (the floor `priors_intraday_since`
itself would use tonight is `2026-08-20`, i.e. almost no data yet post the
F-39/F-40/F-41 redeploy — the wider window is a deliberate one-off for a
real first look, not the tool's own default going forward): 2,004
TAKEN-and-resolved rows, 42 findings written, largest and most credible
being sector-conditioned — GAP in "i.t." 6% win rate (n=357) vs 59% (n=372)
everywhere else; SDN in "auto" 85% (n=18) vs 38% (n=318) elsewhere; SDN in
"metals & mining" 0% (n=23) vs 44% elsewhere. Every one is a PENDING
proposal, nothing acted on.

**Wired into `.github/workflows/brain_sunday_chain.yml`** as its own
`continue-on-error: true` step alongside `weekly_review`/`discover_engines`
— runs automatically every Sunday, honouring `priors_intraday_since` as
its default floor from here forward (so it naturally goes quiet until
enough post-fix data exists, then resumes reporting on clean data only).

### 4 — VERIFIED

17 new offline checks (`test_feature_edge_study.py`): a real separation
fires, a flat relationship is silent, a huge-looking gap on n=3 is refused,
a feature absent from every row fabricates nothing, `meta`-as-JSON-string
is tolerated (same defence as `_engine_of`), `confidence` reads the real
column not a `meta` shadow, the hour-bucket boundaries land exactly on
09:15/10:00/13:00 IST, the `priors_intraday_since` floor is honoured the
same way `alloc_hurdle_since` honours it, and the category-collision fix
is demonstrated directly. `tools.verify`: 707 checks, 71 modules, green.
Live run's 42 written rows independently confirmed to have 42 distinct
`target_key` values via a fresh query, not inferred from the log.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

No `brain_proposals` row here changes anything by itself — every finding
needs an operator decision (build a floor on the feature, or file it as
noise) before it touches a gate. This tool ships in code only tonight;
like F-39 through F-42, it takes effect on the next daemon deploy and the
next Sunday chain run, whichever comes first. `giveback_pct`/`giveback_
min_r` are unchanged — re-verified, not re-armed.


## 2026-08-20 — F-45 (new, guarded) — a LEADING volume-decay signal, rung
7a of the intraday exit ladder: tighten the stop when follow-through
volume fades below what the trade opened on, ahead of the fixed-clock
time stop. Ships correctly wired, OFF by default (migration 091)

**Ran:** `tools.verify` (718 checks, 72 modules, green — 11 new),
`tools.health` (green except the pre-existing, unrelated `quote_parity`),
live config confirmed via `cfg()`.

### 1 — WHY THIS EXISTS

Every rung in `evaluate_intraday_exit` before this one is LAGGING —
elapsed time (rung 7), a level already broken (rung 3), a realised
pullback (rung 5b). None watches for a trade's thesis fading BEFORE price
or the clock says so. A discretionary trader watches follow-through
volume constantly for exactly this: a breakout that cleared its level on
3x volume and is still running on 3x ten minutes later is a different
trade than one now running on 0.5x, same price action, opposite
conviction it continues.

### 2 — MECHANISM

`_volume_decay_ratio(bars, entry_ts, now, window_min)`: average per-bar
volume in the last `window_min` minutes, divided by the average in the
FIRST `window_min` minutes after entry — self-referential against the
trade's own opening pace, not `SymbolContext.volume_ratio()`'s "is today
busy against the 20-day average" (a different, detection-time question).
None until both windows are genuinely non-overlapping (>= 2*window_min
minutes held) and each has >= 2 closed bars.

New rung 7a: only considered while `gain_r < partial_book_r` (the trade
has not yet proven itself — once it has, breakeven/trail/giveback are
already the right protection and this would just fight them for the same
stop). When armed and the ratio falls under
`intraday_volume_decay_floor_pct` (40%), tightens the stop toward the
live price by `intraday_volume_decay_tighten_pct` (50%) of the original
risk width — same linear-toward-price shape as rung 6b's
`short_runway_tighten`, reused rather than a third tightening formula in
one file. Never loosens (`is_better_price` guard, same invariant every
other rung holds). `bars` threaded through from `engine.py`'s existing
live `SymbolContext` — no new data source, the same list `last_completed_
close` already reads for the invalidation rung.

**OFF BY DEFAULT, DELIBERATELY** — same posture `intraday_giveback_pct`
shipped with before migration 059 armed it. Every other rung here is
priced off something already measured (a structural level, an MFE
quantile from the closed book's own numbers, a fixed time floor). This one
is a plausible, professionally-grounded hypothesis with zero hours of
calibration against this book's own resolved trades. Arm once
`tools.exit_ladder_replay`-style evidence exists for this specific
signal — the same arc, not skipped.

### 3 — TWO REAL BUGS CAUGHT BY THE TESTS, NOT ASSUMED CORRECT

**pytz's classic LMT gotcha**, in the test file, not production code:
`datetime(2026, 8, 20, 9, 30, tzinfo=IST)` attaches the pre-1945 Kolkata
LMT offset (+05:53:20) instead of the standard +05:30 — `IST.localize()`
is required. Three tests failed with plausible-looking `HOLD`s instead of
an exception: the test's own bars were timestamped 23 minutes off from
the `entry_ts` `evaluate_intraday_exit` computes via
`.astimezone(IST)` from `pos["entry_date"]`, shifting the comparison
window clean off the bars. Checked whether the sibling file using the
same risky pattern (`test_intraday_short_runway_tighten.py`) is actually
affected: it is not — the function it exercises reads `.hour`/`.minute`
directly, never doing cross-timestamp arithmetic — so it produces a
correct answer despite the same construction, and was left alone rather
than "fixed" for a bug that isn't live there.

**A real logic gap in `_volume_decay_ratio` itself**: with under
2×window_min minutes held, the "initial" and "recent" windows overlap,
and the SAME bars satisfied both filters — the function would return a
near-1.0 "ratio" comparing a window to a copy of itself, exactly when
there is genuinely nothing yet to compare. Fixed with an explicit
`recent_start < initial_end` guard before either filter runs.

### 4 — VERIFIED

11 new offline checks (`test_intraday_volume_decay.py`): the ratio
computes correctly in both directions, None on too-few-bars and on
overlapping windows, the rung is silent when the switch is off (the most
important test here — a textbook-decaying-volume trade must behave
IDENTICALLY to today's shipped code with the switch unset), fires
correctly when armed with the exact expected tightened price, stays
silent once the trade has proven itself (isolated from rungs 5/5c via a
single-share qty and a stop already past breakeven, so an earlier rung
firing can't make the assertion trivially true), never loosens the stop,
correct direction for a short, and a missing `bars` argument does not
crash any existing caller. `tools.verify`: 718 checks, 72 modules, green.
`tools.health`: clean except `quote_parity` (pre-existing, unrelated).
Live config confirmed via `cfg()`, not raw SQL alone.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

**Ships inert.** No live position is tightened by this until an operator
arms `intraday_volume_decay_enabled`, and there is currently no
resolved-trade evidence to arm it on — this signal has never existed
before tonight, so nothing in `closed_positions` carries it yet. Building
the calibration data requires either running with the switch on for a
shadow period and measuring the outcome the way `giveback_pct` was, or
extending `exit_ladder_replay`-style tooling to estimate it from bar
history directly; neither attempted tonight. Like F-39 through F-44, this
takes effect on the next daemon deploy, not before.

## 2026-08-21 — F-46 (change, swing predictive/learning capability) —
answered the operator's direct question: does the swing book actually
PREDICT a trade will work, then LEARN from resolved decisions to
self-improve? Pre-trade: yes, real, live — the allocator's edge/hurdle/
priors already retune daily from resolved outcomes, no defect found.
In-trade: no — the exit ladder is one fixed clock for every setup, and the
one signal that reads like an in-trade prediction (`assess_trend`'s
STRONG/INTACT/BROKEN read) had never once been graded against an outcome,
because its own read never survived to a closed trade. Built and ARMED
LIVE (operator's explicit instruction, overriding this project's usual
shadow-first default for a new capability — see §5) a family-calibrated
stall clock and the continuous telemetry needed to eventually grade
`assess_trend` itself. No file under `allocation/` touched — shared with
intraday, off-limits per the operator's explicit instruction this session.

**Ran:** `tools.verify`: 726/726 across 73 modules (19 new checks this
session — 11 from F-43, 8 from F-46 — running alongside a concurrent
session's own 18 intraday-side checks, F-44/F-45, confirmed no collision).
19 checks demonstrated failing against the pre-fix source first (git
stash + a temporary hide of the new pace_calibration.py module, so the
import errors were real, not assumed) before being trusted to pass.
`tools.simulate` (read-only, live data): HINDCOPPER now correctly shows
`BOOK_PARTIAL` — "1.00R >= 1.0R — book 4/8" — confirming migration 090
(reported blocked in F-43, applied by the operator since) is fully live
end to end.

### 1 — THE QUESTION, ANSWERED WITH EVIDENCE

Sitting at the desk and taking these trades live, from entry to exit:

**Pre-trade — real, working, already self-updating.** `allocation/
scoring.py::swing_priors()` builds a live R-distribution per engine family
and regime bucket from every plan's resolved outcome (traded or not);
`hurdle()` gates against it, rising through the day. Pulled live:
STRONG-bucket TAKE averaged edge 0.045 against DECLINE's 0.035; WEAK-bucket
TAKE 0.051 against DECLINE's 0.038 — discriminating in the right direction,
at real sample sizes (prior_n 125-181). Not the gap; not touched.

**In-trade — nothing predicts pace, and the one signal that looks
predictive has never been graded.** `control/exit_rules.py::assess_trend()`
scores STRONG/INTACT/FADING/BROKEN on structure/momentum/participation/RS/
sector, but only ever RUNS inside `evaluate_exit()` when gain_r >= 1.0 (by
design — the deterioration gate exists specifically to stop trend noise
cutting a position below that floor). Its output — `runner_evidence`/
`runner_verdict` — is written only by `manage_open_positions()`, the
once-a-day batch path; the 15s daemon, which places nearly every real
exit, never persisted it. Checked live: the last 20 closed swing trades —
100% NULL on both fields. A well-reasoned rule that has never once been
checked against what actually happened next.

**The strategy currently holding 5 of 6 open positions has a measured
negative edge of its own.** From `signal_output_daily`'s full entered-
outcome record (every plan whose zone was touched, traded or not): MOM
n=188, avg return **-0.56%**, 68% TIMEOUT (drift, neither target nor
stop). CTL by contrast: n=360, avg **+1.57%**. Consistent with the KB's
own pre-existing MOM finding (50% win / +0.05%), measured fresh and worse.

### 2 — BUILT (armed live, not shadow, per explicit instruction)

**(a) Family-calibrated stall clock.** New `swing/signals/pace_
calibration.py::build_family_stall_days()` — from every plan resolved
TARGET, grouped by `swing_family()` (imported read-only from `allocation/
scoring.py`, never edited), p75 of `outcome_hold_days`, capped at the
configured `exit_stall_days` (can only TIGHTEN, never loosen) and floored
at `swing_stall_pace_floor_days` (3). Measured 21-Aug-2026:

    family          n     median days-to-target   p75 (armed value)
    CONTINUATION   296             3                6
    MOM             86             4                7
    RVS              6         too thin — stays at the flat default (10)

Built once per daemon start (`intraday/engine.py`, same lifetime
`load_exit_policy()` already has) and wired into `evaluate_exit()`'s
existing STALL EXIT rung — no new exit rule, the existing one recalibrated
per family. A CONTINUATION trade 7 sessions in, never above 0.3R, now
stalls at session 6 instead of waiting until session 10; MOM gets 7 not
10; RVS (and any family with no calibration) is unchanged. Self-sharpens
with no code change as `swing/signals/outcomes.py` resolves more plans.

**(b) Continuous trend telemetry.** New `intraday/engine.py::_track_
trend_quality()`, called every cycle for every SWING position at ANY
gain_r (not gated at 1.0 the way the deterioration check itself must
stay) — pure additive telemetry, changes no decision. This is what makes
(c) below eventually answerable.

**(c) Runner-field forwarding.** Whenever `evaluate_exit()` DOES compute
`runner_evidence`/`runner_verdict`/`runner_since_r` (the RUN/EXIT_TARGET/
EXIT_DETERIORATION branches), the daemon now persists them — same gap
class as F-43's `exit_signal` fix. Both (b) and (c) are SWING-only reads
(`evaluate_intraday_exit` never sets these keys), so both are inert for
every intraday action, not merely harmless.

### 3 — VERIFIED

`tests/test_f46_pace_calibration.py`, 8 checks: `_calibrate()` (pure,
matches the measured CONTINUATION=6/MOM=7 numbers, respects the sample
floor, the cap-never-loosen contract, and the floor-days minimum) plus
`evaluate_exit()` wiring (a CTL position the flat 10-day default would
still hold correctly stalls under the calibrated 6-day clock; a family
absent from the calibration dict falls back to the flat default
unchanged; a policy dict with no `stall_days_by_family` key at all —
every pre-existing caller — behaves exactly as before). Demonstrated
failing first: `git stash --keep-index` on the two touched source files
plus a temporary rename hiding the new module, all 8 failed (5 on import,
3 on reverted behaviour), then restored and re-ran green.

A CONCURRENT SESSION landed two commits on `main` mid-session (F-44
correcting a giveback claim + a feature-edge-study tool, F-45 an
intraday-only volume-decay rung) — both properly tested, properly
findings-logged, on files this session never touched. Collided only on
finding numbers (this entry was drafted as F-44, renumbered to F-46) and
on `tools/verify.py`'s MODULES list, which merged cleanly with no manual
resolution needed since both sessions only ever appended.

### 4 — NOT DONE / WHAT THIS DOES NOT CHANGE

**`assess_trend` itself is still unvalidated — this session makes it
measurable, not measured.** (b)/(c) close the recording gap; whether
STRONG-labelled positions actually outperform BROKEN-labelled ones is a
question that needs weeks of accumulated telemetry against resolved
trades before it has an answer. Revisit once the current book has enough
closes carrying non-NULL `runner_evidence`/`runner_verdict` to say
something real.

**Armed without a shadow period — the operator's explicit call, stated
plainly rather than argued with.** This project's own default posture
for a new capability is shadow-log-then-arm (see the swing-chase-ceiling
stages in `docs/TRADEOS_ROADMAP.md`); this shipped straight to live on
direct instruction. The mitigations built into the design instead of a
shadow period: the calibration can only ever tighten an existing,
already-live rule (never loosen it, never invent a new one), and every
fallback path — thin sample, missing family, a fetch failure — resolves
to the exact flat-default behaviour this book has always had.

**Same daemon-deploy gap as every recent finding.** `tools.simulate`
proves the code correct by importing it fresh; `intraday/run.py`'s
long-running process does not pick any of this up until restarted.


## 2026-08-22 — F-47 (new column, backend + frontend) — `sub_engine` never
survived the paper-entry write; open_positions/closed_positions could not
distinguish SDN's 83%-win VREJ from its 8%-win BRKD. Migration 094 adds it
end to end, dashboard's "Strategy P&L Breakdown" now groups by it

**Ran:** `tools.verify` (737 checks, 74 modules, green — 7 new), live
disposable insert against `open_positions` confirming the column round-
trips, `npx tsc --noEmit` confirming no new type errors in the changed
frontend files.

### 1 — WHAT SURFACED IT

Operator's own ask, reviewing 21-Aug's numbers: "can we merge this view in
Strategy P&L Breakdown... I want to identify what is differentiating
winners vs losers." Checked the actual panel (`PerformanceTab.tsx`'s
`StrategyBreakdown`) against the actual schema rather than assume it
already showed this.

### 2 — ROOT CAUSE

`intraday_setups.meta.sub_engine` has correctly separated SDN's three
conditions since F-39/F-41 — but `open_positions` and `closed_positions`
never had an equivalent column. `paper_broker.open_position()` wrote
`strategy` (the family) from the setup dict and nothing else; `position_
lifecycle.close()` carried whatever `open_positions` had, which was never
the condition. The correctness fix from two nights ago was real and
verified at the detection layer, and evaporated at the exact boundary
where a human would go to see it.

### 3 — FIX, THREE WRITE SITES PLUS ONE SCHEMA CHANGE

Migration 094: `sub_engine TEXT` added to both `open_positions` and
`closed_positions`, additive, NULL on every historical row (nothing to
backfill from — the value was never captured, not merely dropped).

`paper_broker.py::open_position()`: writes `setup.get("sub_engine")` for
INTRADAY, explicit `None` for SWING (sub_engine is an intraday-only
vocabulary per `docs/TERMINOLOGY.md`).

`engine.py::_maybe_open_paper()`: supplies the value from the setup's own
meta — `st.meta.get("sub_engine") or st.strategy` — the identical fallback
expression F-39's own `setdefault()` uses, so a setup that somehow bypassed
the registry still records something sensible rather than None.

`position_lifecycle.py::close()`: carries `pos.get("sub_engine")` from
`open_positions` through to the `closed` dict, same pattern as `sector`/
`pick_label` above it.

**Frontend** (`PerformanceTab.tsx`): `StrategyBreakdown` now groups by
`sub_engine || strategy` instead of `strategy` alone. Family shown as a
secondary label only when it differs from the row's own name (i.e. only
for SDN's three conditions — every other engine's row is exactly as
compact as before). A "thin" badge appears under 10 closed trades so an
early, small-sample row is not read with the same confidence as an
established one — `MIN_MEANINGFUL_SAMPLE` existed for the tab's overall
KPI already but was never applied per-row here. Dollar amount unchanged.
`ClosedPosition` type updated; the query layer already selects `'*'`, so no
query change was needed for the new column to reach the frontend.

### 4 — VERIFIED

New file `tests/test_sub_engine_on_positions.py`, 7 checks: `open_position`
carries `sub_engine` for INTRADAY, falls back sensibly when the setup
dict lacks it (a caller-level concern, checked separately from `open_
position` itself, which records exactly what it is handed), never writes
it for SWING even if a setup dict happens to carry the key, the `_maybe_
open_paper` fallback expression itself, and `close()`'s carry-through for
both a populated and a pre-migration (None) case. `tools.verify`: 737
checks, 74 modules. Live round-trip: inserted and read back a disposable
`open_positions` row with `sub_engine='VREJ'`, cleaned up. `npx tsc
--noEmit`: zero new errors in `PerformanceTab.tsx`/`types/database.ts`
(the wider codebase has pre-existing, unrelated type errors in other files
— confirmed by diffing which files' errors appeared, not assumed absent).

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

No historical row is backfilled — every closed position before this
migration reads `sub_engine=NULL` and falls back to `strategy`, same as it
always displayed. Could not visually verify the rendered dashboard in a
live browser — this requires the operator's own configured Supabase
environment, which this session does not have credentials for; verified
via type-checking and full data-path tracing (query → type → component)
instead. `EngineLeaderboard` (the adjacent panel, reading `performance_
metrics.engine_stats`, a different pipeline) was not touched — out of
scope, not what was asked.


## 2026-08-22 — F-48 (new, shipped ARMED) — a same-engine tie-break
prioritising retest-confirmed candidates in the exploration queue, without
touching edge, the bar, or admission counts

**Ran:** `tools.verify` (737 checks, 74 modules, green — 4 new),
demonstrated failing before the fix landed, live config confirmed via
`cfg_bool()`.

### 1 — WHAT SURFACED IT

Operator's own question, following the POWERGRID-vs-the-rest finding in
21-Aug's ORB trades: "do we have this retest logic working for all
engines? If yes, can we prioritize where retest is true but not block
anything." Checked every engine file (`orb.py`, `short_distribution.py`,
`squeeze.py`, `pullback.py`, `vwap_reclaim.py`, `gap_and_go.py`, `gap_
down_bounce.py`, `range_fade.py`, `prev_day_levels.py`) rather than assume.

**Inventory, not assumed:** only ORB has an optional, meta-stamped
`retest_confirmed` signal (F-37). PDL and RNG require an equivalent
confirmation as a hard PREREQUISITE for detection at all — every setup
they produce already has it, nothing to prioritize. SDN's three
conditions, VCE, PBK, VWR, GAP and GDB have no such signal today. SDN's
BRKD condition (8% win rate over the post-fix sample, structurally the
same "raw breakout, no hold check" shape as ORB's unconfirmed fallback,
which measured 0% win rate over the same window) is the clear next
candidate for one — not built tonight, named for a future pass.

### 2 — MECHANISM

`allocation/policies.py::_confirmation_key(s)`: reads `retest_confirmed`
from the scored proposal's own `meta`; 0 when `True`, 1 otherwise
(including absent — an engine with no signal at all is not scored as
worse than one that checked and failed). Wired into `_interleave_by_
engine`'s TWO sort calls as a secondary key, always after `-_edge_key(x)`:
`sorted(scored, key=lambda x: (-_edge_key(x), _confirmation_key(x)))`.

**Why this is a tie-break and not a gate.** `_interleave_by_engine`'s own
19-Aug finding is that same-engine candidates in one cycle carry very
nearly the SAME edge (they share a prior; only `cost_r` separates them),
so a tie already exists today and is broken by whatever arbitrary order
`cost_r` happens to produce. This replaces that arbitrary tie-break with
an evidence-backed one. A worse-edge confirmed candidate can never jump a
better-edge unconfirmed one — edge is checked first, always.

### 3 — WHY THIS SHIPS ARMED, NOT INERT LIKE EVERY OTHER NEW RULE THIS
SESSION

`giveback_pct`, `short_runway_tighten_enabled` and `intraday_volume_decay_
enabled` all ship OFF because each can ADMIT or DECLINE a trade with zero
calibration behind its threshold. This cannot, by construction — it only
reorders candidates edge already tied, never changes whether anything
clears the bar, and the aggregate TAKE count for a cycle is unaffected. It
also already has real, if thin, evidence behind it: of 21-Aug's 6
unconfirmed ORB trades, 0 won; the 1 confirmed trade (POWERGRID) closed
+1.65R; the broader post-18-Aug sample agrees in direction (confirmed 33%
win / +0.18% mean vs unconfirmed 0% / -0.45% mean, n=7 vs 17). Named as a
deliberate departure from this session's own "ship inert" pattern, not a
quiet exception to it — `alloc_intraday_confirmation_priority` remains a
one-flag rollback if the operator would rather it start OFF.

### 4 — VERIFIED

4 new checks in `tests/test_engine_fairness_and_bands.py`: the exact live
shape (two ORB candidates, one confirmed, one not, equal edge) orders the
confirmed one first without touching either proposal's own `edge`; a
worse-edge confirmed candidate never jumps a better-edge unconfirmed one;
an engine with no `retest_confirmed` field at all is not penalised
relative to an explicit `False`; the switch off restores plain edge order
with no confirmation influence. Demonstrated failing first — reverted the
sort key to `-_edge_key(x)` alone, watched the live-shape test fail
(`['ABCAPITAL', 'POWERGRID']` instead of the expected order), restored the
fix, re-ran green. `tools.verify`: 737 checks, 74 modules. Live config
confirmed via `cfg_bool()`, armed `true`.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

Does not extend retest-style confirmation to any other engine — BRKD is
named as the highest-value next candidate given the direct parallel to
ORB's own unconfirmed-fallback finding, but reusing `_retest_and_held()`
inside `short_distribution.py` was not attempted tonight; a real design
pass, not a copy-paste. Like every other change tonight, ships in code
only until the daemon is redeployed.


## 2026-08-22 — F-49 (new, priority-only) — SDN's BRKD condition gets a
retest-and-held signal, the short mirror of ORB's own (F-37), informational
only per the operator's explicit "priority criteria, not a hard filter"

**Ran:** `tools.verify` (746 checks, 76 modules, green — 7 new), one test
corrected after a wrong assumption about the shared "breakdown" fixture's
actual shape, not the code.

### 1 — WHY

BRKD's 8% win rate over the post-fix sample is structurally the same "raw
break, no hold requirement" shape ORB's own unconfirmed-fallback path
measured 0% on. The operator asked directly whether retest logic exists
for every engine — checked every strategy file rather than assume: only
ORB has it, PDL and RNG require an equivalent as a hard detection
prerequisite (nothing to prioritize, every setup already has it), and
BRKD, VCE, PBK, VWR, GAP, GDB have nothing. BRKD is the clear next
candidate given the direct parallel to ORB's own finding.

### 2 — FIX

`_retest_and_held_short()`, a new pure function in
`short_distribution.py` — the SHORT mirror of `orb.py::_retest_and_held`,
written as a SEPARATE function rather than a `direction` parameter on the
original. This project's own history (`open_positions.direction`,
`allocation/proposal.py`'s missing direction field, the cost-gate call in
`evaluate_intraday_setups`) is a repeated pattern of a direction-aware
function shipping LONG-default and every pre-shorting call site silently
inheriting it — a shared, branching version of a bar-by-bar, sign-
sensitive function this specific is exactly the shape that risk repeats
in. Two small, obviously-mirrored functions instead.

**Wired as pure information, never a gate** — `_range_breakdown` computes
`retest_confirmed` and stamps it into `meta`, but the setup is returned
regardless of the result. The only consumer is F-48's existing
`_confirmation_key` priority tie-break, which already reads
`retest_confirmed` generically — BRKD needed no new ranking mechanism,
only to start populating the same field ORB already does.

### 3 — VERIFIED

7 new checks (`test_sdn_breakdown_retest.py`): the pure function's four
core cases (never probed, probed-but-no-retest, retest-and-held,
retest-then-reclaimed) plus wiring checks. **One test's own assumption
was wrong, not the code**: the first version assumed the shared
'breakdown' fixture would not retest and asserted `False`; it measured
`True` — a post-range bar's high genuinely clears the retest tolerance
band, matching ORB's own "no upper bound on the retest touch" shape
exactly, mirrored deliberately. Corrected to isolate wiring from
detection via mocking (matching the True-case test's own approach)
rather than depend on a fixture's specific numeric shape. `tools.verify`:
746 checks, 76 modules. Migration 096: `intraday_short_breakdown_retest_
tolerance_pct=0.15`, armed live.

---

## 2026-08-22 — F-50 (new, ARMED, two safety boundaries) — out-of-sample
validation for feature-study findings, and a generalised priority
tie-break for VALIDATED, FAVOURABLE ones — never a hard filter

**Ran:** `tools.verify` (757 checks, 76 modules, green — 11 new),
demonstrated failing before the fix landed, live run against real data
(2 validated, 1 rejected, 29 not-enough-fresh-data-yet, correctly
produced an EMPTY priority cache because both validated findings that
round were unfavourable).

### 1 — WHY

Operator's own question: "how can we make the system smarter so every
win is being evaluated to find similar instances and prioritize them...
or is that not a good choice?" Answered directly: the undisciplined
version of this — finding a pattern in past wins and immediately chasing
it — is the single most common way systematic books curve-fit to their
own recent history and then act on noise. The guardrail that makes it
safe is checking whether a finding predicts data it has not seen yet,
before anything acts on it.

### 2 — MECHANISM, TWO PARTS

**Out-of-sample validation** (`feature_edge_study.py::validate_pending`):
every PENDING, CATEGORICAL `FEATURE_FILTER` proposal (3-part `target_key`
— numeric 2-part findings are explicitly out of scope, see below) gets
re-checked against rows that closed strictly AFTER the day the proposal
was created — data the original finding could not have seen. THREE
outcomes, not two: same direction on fresh data at the same significance
bar → VALIDATED; opposite direction or below the bar → REJECTED, kept as
a record, never deleted; not enough fresh data yet → stays PENDING,
unchanged. Reuses the existing `categorical_splits`/`is_favourable`
machinery directly rather than a second definition of "significant."
Runs automatically at the start of every `feature_edge_study` cycle
(weekly, via the Sunday chain), validating older findings before this
run's new ones join the queue behind them.

**Generalised priority tie-break** (`allocation/policies.py`):
`build_priority_criteria()` turns VALIDATED, FAVOURABLE categorical rows
into `{engine: {feature: {category, ...}}}`; `_confirmation_key` now
checks a candidate's `meta` against this cache IN ADDITION TO
`retest_confirmed` — either signal is enough to rank first, same "0 or
1" tie-break shape F-48 shipped, no new admission or decline path.
Reuses `alloc_intraday_confirmation_priority` (already armed) rather than
a second switch — one config question ("is any evidence-backed tie-break
active"), not two.

**FAVOURABLE ONLY, BY DESIGN.** A categorical split can validate in
either direction — a category the data prefers, or one it avoids (GAP in
"i.t.", 6% win rate). Only the first kind reaches the priority cache. The
operator's own words: "add it as priority criteria and not the hard
filter to block everything." De-prioritising a category is a soft block
by another name — materially different from "prefer this when there is
a choice" — and was not asked for. Unfavourable, validated findings stay
fully visible via `tradeos learn show` for a human to act on directly.

**NUMERIC FINDINGS NOT READ HERE.** A validated numeric split (e.g.
GAP/atr_pct_daily) needs the exact tercile boundary that produced it to
be matched consistently against a live candidate; that boundary exists
today only inside a human-readable `evidence` string, not a structured
field. Named as real, separate follow-on work rather than guessed at.

**PURE FUNCTIONS STAY PURE.** `_confirmation_key`/`_interleave_by_engine`
are documented "Pure" and must never do I/O on the 15s hot path.
`Allocator.refresh_priority_criteria()` loads the cache on the SAME slow
timer (300s, `intraday/run.py`) `refresh_priors()` already uses, and
`select()` passes the cached dict down as a plain parameter — the ranking
functions never query anything themselves.

### 3 — BUG CAUGHT BEFORE COMMIT, NOT ASSUMED AWAY

`tools.verify`'s own `static_analysis` check flagged `validate_pending`'s
new paged read of `brain_proposals` for having no verified sort key —
this project's own established trap (LIMIT/OFFSET with no stable ORDER BY
repeats and drops rows across pages). Probed `id` live (103 rows, all
distinct) before trusting it, added `.order("id")`, registered it in
`_FETCH_ALL_SORT_KEY`. Caught by the check that exists specifically to
catch it, not found by inspection.

### 4 — VERIFIED

11 new checks across two files: `build_priority_criteria` reads only
3-part (categorical) target keys and merges multiple categories per
feature correctly; the interleave queue actually reorders on a validated
sector match; an engine/feature absent from the criteria cache is never
treated as a default match; retest and priority-criteria signals both
independently produce rank 0; a caller passing no criteria at all (every
site written before this session) behaves identically to before F-50;
`is_favourable` decides correctly from win rate, falls back to mean_pct
on a tie, and returns None — never a guessed direction — when nothing
can decide it. Demonstrated failing: reverted the priority-criteria check
to a bare `return 1`, watched the live-shape test fail
(`['INFY', 'MARUTI']` instead of the expected order), restored, re-ran
green. Live run against real `brain_proposals`: validated 2 ORB findings
on fresh data, rejected 1, left 29 PENDING (genuinely not enough fresh
data since creation) — and confirmed the priority cache came back EMPTY,
correctly, because both validated findings that round happened to be
unfavourable (RISK_ON and the OPEN hour both predict WORSE ORB outcomes
on the fresh sample) — the exact boundary this design exists to hold.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

No proposal's numeric findings feed the priority cache yet (see above).
No hard filter exists anywhere in this mechanism — an unfavourable,
validated finding is visible, never acted on automatically. Ships in
code only until the daemon is redeployed, same as every other change
tonight.

---

## 2026-08-22 — F-51 (bug fix) — `tools.weekly_review`'s own proposal
viewer only ever showed its own proposals; 74 of 81 PENDING rows across
three other sources were invisible to the command meant to surface them

**Ran:** `tools.verify` (739 checks at the time, green — 2 new), live run
against real `brain_proposals` confirming all sources now display.

### 1 — WHAT SURFACED IT

Operator, trying to review tonight's 42 feature-study proposals: "where
do I see it? I am unable to find it to validate this part." Checked the
actual command (`tradeos learn show` → `weekly_review.py --show` →
`show_open()`) rather than assume it worked because `discover_engines.py`
and this file's own weekly-review functions had been telling the operator
to run it for weeks.

### 2 — ROOT CAUSE

`show_open()` read `brain_proposals` with `.eq("source", "weekly_review")`
hardcoded — presumably written when this was the only source that
existed, never revisited as `discover_engines.py` (`source=
"discover_engines"`), `feature_edge_study.py`
(`source="feature_edge_study"`), and an unrelated pre-existing tool
(`source="script_profiler"`) all started writing to the same table.
Measured live: 81 PENDING rows total, 7 shown. `discover_engines.py`'s
own log line — "read them with `tradeos learn show`" — has been a
promise this command never kept.

### 3 — FIX

Removed the source filter entirely; groups output by source instead
(confidence-sorted within each group) so provenance stays visible without
excluding anything. `brain_proposals` is deliberately ONE table so a
discovery is reviewed exactly like a retirement rather than through a
side channel (discover_engines.py's own docstring) — a viewer that shows
only one source was that side channel by accident.

### 4 — VERIFIED

2 new checks (`test_weekly_review_show_open.py`): the built query never
carries a `source` filter (the exact regression), and a clean zero-rows
case reports cleanly. `tools.verify`'s `static_analysis` check caught a
second, real issue in the same change — the new paged `fetch_all` call
had no verified sort key; fixed with `.order("id")` and a probed,
registered key, same as F-50 above. Live run: all 81 PENDING rows now
print, grouped by source (feature_edge_study 42, script_profiler 28,
discover_engines 4, weekly_review 7).

### 5 — NOT DONE

`script_profiler`'s 28 proposals are now visible but were not reviewed —
a tool this session did not otherwise touch tonight, named rather than
investigated.


## 2026-08-22 -- F-52 (decision, Gate 3) -- PDL, PBK, RNG, GDB: KEEP, all
four, explicit operator sign-off

Gate 3 (docs/TRADEOS_ROADMAP.md) requires an explicit per-engine
retire/keep decision, never batched, never assumed by silence. Raised as
an open question this session (thin samples: PBK/GDB near-zero rows,
PDL/RNG rare-by-design). Operator's answer, verbatim in substance: keep
all four -- retiring was never the right question. The mechanism this
session built (F-48 confirmation priority, F-50 out-of-sample-validated
priority) IS the intended alternative to retirement: evaluate what each
engine's wins have in common, prioritize toward it, without needing to
remove an engine that simply has not fired often yet. PDL and RNG are
additionally understood to be RARE BY DESIGN (both require a hard
confirmation prerequisite before they detect anything at all -- see
F-49's own inventory) -- low frequency is not the same claim as low
quality for either of them.

No code change. No engine's lifecycle state was touched. Logged here
because Gate 3 requires the decision recorded, not because anything
needed to move.


## 2026-08-23 — F-53 (bug fix + data reset) — `validate_pending()` read a
pre-F-50 proposal's placeholder `current_value` as an explicit
"unfavourable" claim nobody made; one finding was already wrongly
REJECTED live. Fixed the comparison, superseded the 42 contaminated
legacy findings, regenerated 33 clean ones from the Aug-20 floor

**Ran:** `tools.verify` (763 checks, 76 modules, green — 6 new),
demonstrated failing before the fix landed, live `brain_proposals` audit
before and after, `tools.feature_edge_study` run live with its own
default (non-override) floor.

### 1 — WHAT SURFACED IT

Operator's own question: "should [feature-study learning] not also reset
based on the prior date? I want a clean intraday and accurate
calculations from the date we reset." Read as a preference at first;
checking the live `brain_proposals` table before acting turned it into a
confirmed bug report.

### 2 — ROOT CAUSE

The 42 `FEATURE_FILTER` findings written the night before last (F-44)
used a deliberate one-off override, `--since 2026-08-11`, to get a first
real look — explicitly flagged at the time as non-default, not the
floor-respecting behaviour `main()` uses. F-50 (built later that same
session) added a structured `current_value` field
("favourable"/"unfavourable"/"unclear") that `_propose()` stamps on every
NEW finding — but the 42 existing rows predate that field and still
carried the old placeholder text, `"no feature-level filter"`.

`validate_pending()`'s comparison was `r.get("current_value") ==
"favourable"` — so any OTHER value, including the placeholder AND the
genuine "unclear" no-opinion state, silently read as an explicit
UNFAVOURABLE claim. Confirmed live, not assumed: `ORB/_hour_bucket/MID`'s
real Aug-20 finding said MID was the GOOD side of the split; the fresh
out-of-sample check agreed; `validate_pending()` still emitted REJECTED,
because a row written before the field existed was read as if it had
confidently claimed the opposite. Two other legacy rows (`ORB/_hour_
bucket/OPEN`, `ORB/regime_at_detection/RISK_ON`) landed on VALIDATED by
the same broken comparison, coincidentally correctly this time — the bug
does not fail loudly, it is right or wrong depending on what the
original, never-recorded direction happened to be.

**Not live-money-facing.** `Allocator.refresh_priority_criteria()`
separately filters `.eq("current_value", "favourable")`, so the two
falsely-VALIDATED legacy rows (whose `current_value` was still the
placeholder text, not literally `"favourable"`) were never actually
reachable by the priority cache — the bug corrupted the ledger's record
of what held up, not a live trading decision.

### 3 — FIX

Extracted the comparison into its own pure function, `_validation_outcome
(current_value, fresh_favourable)`, returning `None` — "cannot validate",
caller must skip — unless `current_value` is literally `"favourable"` or
`"unfavourable"`. `validate_pending()` now skips (does not guess at) any
row whose original direction was never actually recorded, instead of
defaulting it to "unfavourable" by omission.

**Data reset, not a silent purge.** The 42 legacy rows were not deleted —
marked `status='SUPERSEDED'` with a `backtest_result` note explaining why,
so the row and its history stay queryable but no longer interfere with
`validate_pending()`, `refresh_priority_criteria()`, or `tradeos learn
show` (all three already filter to `PENDING`/`VALIDATED` states that
`SUPERSEDED` falls outside of — confirmed by reading each consumer's own
query before relying on it). Ran `tools.feature_edge_study` fresh with no
override, honouring its own default floor (`priors_intraday_since` =
2026-08-20): **33 new findings**, every one correctly tagged `favourable`
(17) or `unfavourable` (16) from creation — structurally compatible with
`validate_pending()` from day one this time.

### 4 — VERIFIED

6 new offline checks in `tests/test_feature_edge_study.py`: directions
that agree confirm, directions that disagree reject, the exact pre-F-50
placeholder value returns "cannot validate" (not a guessed direction),
`"unclear"` likewise, a fresh check that itself cannot decide a direction
returns "cannot validate", and a direct regression pin on the real
`ORB/_hour_bucket/MID` case (`_validation_outcome("no feature-level
filter", True)` must be `None`, never a verdict). Demonstrated failing
first — reverted the placeholder guard, watched 3 of the 6 new checks
fail against the un-fixed comparison, restored the fix, re-ran green.
`tools.verify`: 763 checks, 76 modules. Live: confirmed all 42 legacy
rows reached `SUPERSEDED` (42 of 42, zero remaining as `PENDING`/
`VALIDATED`/`REJECTED`), confirmed the 33 regenerated rows all carry a
real `current_value` tag. `git status` confirms only `tools/feature_edge_
study.py` and its test file changed — nothing under `swing/`, per the
operator's explicit instruction this pass touch none of it.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

The 33 fresh findings are new PENDING proposals, same as any other —
nothing acts on them until a future `validate_pending()` run checks them
against data closed after today, same out-of-sample discipline as
always. No change to `discover_engines.py` or `weekly_review.py`'s own
proposal tables — this pass was scoped to `FEATURE_FILTER` rows only, the
ones the operator's question was actually about.

## 2026-08-23 — F-54 (new, shipped OFF) — Stage D2, live universe
re-qualification: a name outside today's ~120-name bench that moved and
traded enough TODAY, not yesterday, can now be admitted mid-session

**Ran:** `tools.verify` (783 checks, 77 modules, green — 20 new),
demonstrated failing before the fix landed, live integration check
against production `stock_data_daily` (real candidate found: HDFCBANK),
live check of the Kite REST quote path under a real expired-token
condition (confirmed graceful degradation, not assumed), `tools.health`
(clean except the two pre-existing, unrelated items).

### 1 — WHAT THIS ADDRESSES

docs/TRADEOS_ROADMAP.md, Track D, Stage D2. `scanner.live_rerank()` can
only reorder names already in the ~120-name daily bench; the bench itself
is built once a day from YESTERDAY's turnover/ATR (`build_universe()`). A
name too quiet yesterday to qualify, then gapping hard today, was
invisible for the whole session regardless of the bench size — the
constraint was never the number, it was WHEN the eligibility list gets
decided.

### 2 — WHAT GOT BUILT

`intraday/scanner.py`: extracted the four static gates (price, ASM/F&O
flag, liquidity, delivery) plus the ATR/movement gate into one shared
`_qualifies()` function, `require_movement` togglable — so `build_universe()`
and the new candidate-pool function can never independently drift on what
"otherwise tradeable" means (this project's own repeated failure shape:
hurdle/edge units, the sub_engine overwrite). `build_universe()` rewired
to call it; behaviour proven unchanged via a direct fixture test, not
assumed from the diff.

New `movement_rejected_candidates()`: the population a live re-check may
touch — qualifies on price/liquidity/delivery, failed ONLY yesterday's
ATR band. New `live_requalify()`, pure: admits a candidate whose TODAY's
own |% move from previous close| and today's-so-far turnover (crore)
clear the same magnitude/liquidity floors, direction-agnostic (a stock
down 10% qualifies exactly like one up 10%, matching the existing ATR
band's own magnitude-only test). Both new config thresholds default to
the SAME values `intraday_min_atr_pct`/`intraday_min_turnover_cr`
already use — the same question, asked of today's live number instead of
yesterday's full-day one.

New `intraday/engine.py::live_requalify_universe()`: fetches quotes via
the EXISTING `kite_client.fetch_quotes()` (already-established 200-batch
REST helper, not the tick feed — a bench-excluded name has no websocket
subscription yet, so `feed.quote()` cannot see it). COMPUTES AND LOGS
UNCONDITIONALLY; only appends to `self._bench` (and so only reaches
`watch_symbols()`/the websocket) when `intraday_live_requalify_enabled`
is armed — same "propose before it can act" shape `floor_only_rank`
already uses.

Its own timer (`intraday_live_requalify_interval_s`, 45s default,
`intraday/config.py::live_requalify_interval_s()`) — separate from the
300s bench rebuild for the same reason the position guard has its own
timer: the bench rebuild is a genuinely expensive ~500-row historical
scan, this is a handful of REST calls against a small, pre-filtered list.
Wired into `intraday/run.py` as its own independent block, calling
`feed.resubscribe()` immediately when something is admitted rather than
waiting for the next slow cycle — the entire point of a faster clock.

### 3 — VERIFIED, PRECISELY WHAT WAS AND WAS NOT DEMONSTRATED LIVE

20 new offline checks (`tests/test_scanner_live_requalify.py`): every
`_qualifies()` rejection path individually, the `build_universe()`
refactor's behaviour proven unchanged via a fixture, `movement_rejected_
candidates()` correctly isolating the ATR-only-failure population and
respecting `exclude`, and thorough `live_requalify()` coverage — both
floors required independently, direction-agnostic admission, a missing
quote skipped (not treated as evidence either way, same rule
`live_rerank()` already applies), a zero previous-close defended against
divide-by-zero. Demonstrated failing first: dropped the turnover check,
watched the exact test built to catch that fail, restored, re-ran green.

**Live-verified, for real:** `movement_rejected_candidates()` run against
production `stock_data_daily` found a genuine real case —
HDFCBANK, ATR 1.16% against the 1.20% floor, missed by four hundredths of
a point, exactly the "just barely too quiet yesterday" shape this stage
exists to catch. `fetch_quotes()` checked live under a REAL current
condition (the Kite session is expired right now, confirmed via
`tools.health`) and confirmed to degrade gracefully — returns `{}`,
which `live_requalify()` correctly turns into zero admissions, no
exception anywhere in the chain.

**Not demonstrated live, and said so rather than implied otherwise:** an
actual mid-session admission with a valid Kite token and the market open
— this ran after hours, with an expired token, so the live-quote leg of
the mechanism could only be proven to fail safely, not to succeed. That
needs a session with both conditions true, which this was not.

### 4 — A REAL FINDING SURFACED WHILE VERIFYING, NAMED NOT ASSUMED AWAY

`stock_data_daily` (499 rows for 21-Aug) is narrower than the full daily
bhavcopy ingest (`raw_prices`, 2,633 rows for the same date) — `build_
universe()` and everything in this stage can only ever see the smaller,
derived table. The Track D roadmap's own "not Nifty 500" correction
(traced to `ingest_bhavcopy.py`, ~1,800-2,000 EQ names) describes the RAW
ingest correctly; `stock_data_daily` itself is a further-filtered,
indicator-computed table (`compute_indicators.py`) that only 499 of those
names made it into on this date — for reasons not investigated this
pass (likely history-length or a swing-pipeline-specific inclusion rule,
not confirmed). This is a real, deeper gap beyond Stage D2's own scope —
named here for Stage D2's own record and as a candidate for a future,
separate look, not silently absorbed into today's claim.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

**Ships OFF** (`intraday_live_requalify_enabled=false`) — the check
computes and logs on its own new timer the moment the daemon is
redeployed, but admits nothing to the live bench until armed on the
evidence that log accumulates, same posture as every other new rule this
project ships. **The IPO/newly-listed path (Stage D2's own documented
"genuinely separate case") is not built in this pass** — a stock with no
`stock_data_daily` row at all (new listing, or excluded per finding #4
above) needs Kite's instrument master and an absolute, not
history-relative, qualification rule; named as the clear next piece of
Stage D2, not silently dropped. This is on the `feat/intraday-live-
universe` branch, not merged to `main` — Gate D2 is the operator's own
sign-off, per the roadmap's own rule that the human decides at every
gate, and the branch is where that evidence sits until given.

## 2026-08-23 — F-55 (new, shipped OFF) — Stage D2b, closes the IPO/
unreferenced-name gap F-54 named and left open; corrects F-54's own
`stock_data_daily` population claim

**Ran:** `tools.verify` (788 checks, 77 modules, green — 7 more than
F-54's 20), live query confirming the 253-name `nifty_total_market` gap
still holds today, live run of `unreferenced_candidates()` against
production Supabase (real result: exactly 253, all Population B — Kite
session expired, Population C degraded to empty exactly as designed, no
exception), a deliberate demonstration that a penny-priced candidate
clearing move%/turnover-cr alone is rejected once `min_price` is passed
and admitted when it is not (the exact gap this stage exists to close),
migration 098 applied live and read back to confirm.

### 1 — THE CORRECTION TO F-54 ITSELF

F-54's own "WHAT THIS ADDRESSES"/finding-#4 section, and the Track D
roadmap's original Stage D2 text, both stated `stock_data_daily` was
close to the full NSE EQ bhavcopy (~1,800–2,000 symbols) via `ingest_
bhavcopy.py`. **This was wrong, caught by the operator, not internally.**
`ingest_bhavcopy.py` ENRICHES rows already in `stock_data_daily`
(`value_cr`/`delivery_pct`/`delivery_qty`) for symbols already present —
it does not create new rows there. `stock_data_daily` is swing's own
sheet-baseline table (`compute_indicators.py`'s own docstring: "sheet
baseline"), confirmed 499 rows for 21-Aug — genuinely ~Nifty 500, the
operator's original claim from before F-54 was written. The full bhavcopy
ingest is `raw_prices` (2,633 rows, same date), read only to feed those
three columns back. Per this ledger's own append-only rule, F-54 is not
edited; this entry is the correction of record.

### 2 — WHAT GOT BUILT

`intraday/scanner.py::unreferenced_candidates()`: two populations F-54
left as "not built in this pass" —

- **Population B** — `nifty_total_market` (751 rows) members absent from
  `stock_data_daily` (253 of them, live-confirmed today). Known NSE
  names outside swing's own sheet-baseline table, not new listings.
- **Population C** — names in NEITHER table: the genuine IPO/new-listing
  case, visible only through `kite_client.fetch_nse_eq_symbols()`
  (`kite.instruments("NSE")`, once-per-day cached) — the one source
  current from a listing's first tradeable day, since `nifty_total_
  market` lags an actual listing by NSE's index-reconstitution cycle.

Both return `UniverseEntry` objects with `atr_pct=0.0`, honestly — no
history exists to put there instead — and a `reason` naming exactly
which source found the name.

`live_requalify()` extended with an optional `min_price` argument, checked
against the LIVE quote's `ltp`. Population A candidates already cleared
`_qualifies()`'s own price gate on yesterday's close, so this is a no-op
for them; Population B/C candidates never ran through `_qualifies()` at
all (no `stock_data_daily` row to read a price from), so without this a
penny-priced name could clear the move%/turnover-cr floors on raw share
count alone. Delivery% and ASM/F&O-ban have no live-quote equivalent and
stay unchecked for B/C — named here, not silently assumed covered. Its
reason-string construction was also revised to quote the candidate's OWN
`reason` as context instead of hardcoding "yesterday ATR" — that framing
was only ever true for Population A.

`intraday/engine.py::live_requalify_universe()` now merges all three
populations, fetches quotes for the combined list, and applies `live_
requalify()` once — but keeps the admission decision SEPARATE per
population: Population A still gates on `intraday_live_requalify_enabled`
alone (the switch the operator already armed for that specific,
narrower, already-reviewed population); Population B/C gates on a NEW,
independent switch, `intraday_live_requalify_unreferenced_enabled`
(migration 098, ships FALSE) — folding a wider, less-vetted population
into a switch armed for a narrower one would be exactly the "silently
widen a gate" failure this project's rules forbid.

### 3 — VERIFIED, PRECISELY WHAT WAS AND WAS NOT DEMONSTRATED LIVE

7 new offline checks added to `tests/test_scanner_live_requalify.py`
(now 27 total, all green): both populations found correctly, dedup
across sources, `exclude` respected, graceful degradation when Kite's
instrument master call raises, and the `min_price` gate demonstrated
BOTH ways — rejects a penny-priced candidate that clears move%/turnover-
cr, and is a true no-op (existing behaviour unchanged) when the argument
is omitted.

**Live-verified, for real:** `SELECT count(*)` against production
confirmed the 253-name `nifty_total_market`/`stock_data_daily` gap still
holds today (23-Aug), independent of the Python path. Running `unrefer
enced_candidates()` itself against production returned exactly 253
results, all correctly attributed to Population B; Population C
degraded to empty with a logged warning under the SAME real expired-
token condition `tools.health` independently confirms right now — not
an exception, not a silent zero, a warned one. A hand-built penny-stock
quote (₹8.50 ltp, 13% move, ~32cr turnover on volume alone) was shown
clearing `live_requalify()`'s move/turnover floors and then being
rejected once `min_price=50.0` was passed — the exact failure mode this
stage exists to close, demonstrated on both sides of the fix.

**Not demonstrated live:** an actual Population C admission with a valid
Kite token and the market open — same limitation F-54 already named for
Population A, unchanged here; this ran after hours with an expired
token, so Population C could only be proven to degrade safely, not to
find a real new listing.

### 4 — NOT DONE / WHAT THIS DOES NOT CHANGE

**`nifty_total_market` itself stays static, unrefreshed, this pass.** The
operator asked for a process to refresh it from niftyindices.com's own
CSV; a direct `WebFetch` against that URL timed out (60s, consistent
with anti-automation protection on that host). Deliberately scoped OUT
of tonight's build: Population C (Kite's own instrument master) already
closes the actual IPO-visibility gap a stale `nifty_total_market` would
otherwise leave open, so this is a freshness improvement to Population B
only, not a blocking one — and writing to a table `compute_indicators.
py::fetch_index_membership()` depends on for `nifty_200`/`nifty_500`
tagging is a genuine swing-boundary risk that deserves its own build-and-
test pass, not a rushed addition to this one. Named as the clear next
piece, not silently dropped.

**Ships OFF.** Both `intraday_live_requalify_enabled` (Population A,
armed by the operator directly in Supabase — confirmed by direct query
to still read `false` as of this entry, `updated_at` unchanged since
migration 097's insert, so despite the operator's belief the arming did
not take effect; needs re-doing) and `intraday_live_requalify_
unreferenced_enabled` (Population B/C, new, migration 098) gate live
admission independently; the check computes and logs on its own timer
the moment the daemon is redeployed regardless of either switch. This is
on the `feat/intraday-live-universe` branch, not merged to `main` — Gate
D2 remains the operator's own sign-off.

## 2026-08-23 — F-56 (new, shipped OFF except one live write) — Stage
D2c: real `nifty_total_market` refresh (corrects a wrong "blocked"
finding in F-55/roadmap), the ASM/GSM/F&O-ban gap on Population B/C
closed, Kite instrument filter corrected after live measurement, real
`kite.instruments` count obtained

**Ran:** `tools.verify` (799 checks, 78 modules, green — 11 more than
F-55's 788), `tools.health` (Kite session now live — DSY688, confirmed
via `k.profile()` — only the pre-existing, unrelated `quote_parity`
item remains), a live `kite.instruments("NSE")` pull (10,222 rows) with
its own suffix breakdown computed and sanity-checked against known real
symbols including a live rename (ZOMATO → ETERNAL), three live
niftyindices.com CSV fetches (all 200 OK), a dry-run then a REAL write
of `nifty_total_market` against production with before/after row counts
confirmed by direct SQL, and a live end-to-end run of the full
Population A+B+C → quote-fetch → admission path with Kite connected.

### 1 — THE CORRECTION: THE NIFTYINDICES.COM FETCH WAS NEVER BLOCKED

F-55 and the roadmap both stated a direct fetch of
`ind_niftytotalmarket_list.csv` had timed out and read that as
anti-automation protection, deferring the refresh pipeline on that
basis. **This was never re-tested with a real HTTP client before being
written down as a finding — only assumed from one `WebFetch`-tool
timeout.** A plain `requests.get()` with a browser `User-Agent` header —
no session warmup, unlike `ingest_asm_gsm.py` needs for nseindia.com —
returned 200 with the full CSV on the first try, and so did the Nifty
200 and Nifty 500 constituent CSVs the refresh also needs. Per this
project's own "own mistakes plainly" rule: the earlier finding was
wrong, not merely incomplete, and is corrected here rather than quietly
worked around.

### 2 — WHAT GOT BUILT

`swing/ingestion/ingest_nifty_total_market.py` (new), modelled on `ingest_
asm_gsm.py`'s established shape (DRY_RUN, kill-switch guard, chunked
upserts). Fetches all three CSVs; Total Market failing aborts the run
(nothing to upsert), either index CSV failing independently OMITS that
one boolean column from every row in the run's payload rather than
writing it False from a failed fetch — Postgres/PostgREST then leaves
the existing DB value for that column untouched on upsert. Recomputes
`nifty_200`/`nifty_500` as an explicit boolean from fresh set membership
for every row in the Total Market payload (not "set true if newly
found, leave old trues alone") so a name dropped from an index this
cycle is correctly cleared. Upsert-only, never deletes.

Migration 099: `nifty_total_market` had no freshness column at all —
added `refreshed_at`, nullable, no default.

`intraday/scanner.py::unreferenced_candidates()` now also excludes any
symbol present on `safety_lists` (list_type ASM/GSM/FO_BAN) — that table
is keyed on bare symbol independent of `stock_data_daily`, closing the
one vetting gap F-55 named and left open (Population B/C has no
`asm_flag`/`fo_ban_flag` column to read at all, since that lives only on
`stock_data_daily` rows). Reuses `intraday_skip_flagged`, not a new
switch.

`kite/kite_client.py::fetch_nse_eq_symbols()` filter corrected. F-55's
own proposed fix (`instrument_type == "EQ"`) was measured live and found
to filter NOTHING — every one of 10,086 `segment=="NSE"` rows on this
specific endpoint carries that same tag; F&O/currency instrument types
only appear from a *different* exchange argument. The real signal,
found by inspecting real tradingsymbol suffixes: 7,107 of those 10,086
carry a `-XX` suffix — by far the largest single group is bonds/SDLs/
SGBs/T-Bills (`-SG`, `-N0`..`-N9`/`-NA`..`-NE`, `-GS`, `-TB`, `-GB`;
~5,600 rows alone), plus SME/Emerge board (`-SM`/`-ST`) and
trade-to-trade (`-BE`/`-BZ`) names — real, individually-tradeable NSE
instruments, just not the "is this a new STOCK" question Population C
exists to answer. Filtering to plain (no-suffix) symbols leaves 2,979 —
checked against known real names (RELIANCE, TCS, HDFCBANK all present)
and a genuine edge case: ZOMATO's own old tradingsymbol is correctly
ABSENT and ETERNAL — its 2024 rebrand — is present, confirming the
filter tracks real listing changes rather than a fixed snapshot.

### 3 — THE REAL NUMBER, ANSWERING THE OPERATOR'S OWN QUESTION

`kite.instruments("NSE")` returns **10,222 rows** (136 `segment==
"INDICES"`, excluded; 10,086 `segment=="NSE"`). This is NOT "similar to
Total Market" (751 rows before this session's refresh) — it is roughly
13x larger, because Kite's dump is *everything currently tradeable* on
that exchange segment (every bond ISIN, SGB, T-Bill, SME listing,
trade-to-trade name), while Total Market is NSE Indices' own *curated*
subset by market-cap/liquidity eligibility. After the suffix filter
above, the genuinely comparable figure is 2,979 plain mainboard/ETF
symbols — still ~4x Total Market's size, which is expected: Total
Market excludes illiquid/small names Kite still lists as tradeable.

### 4 — LIVE-VERIFIED, PRECISELY

Real write against production: **3 new symbols added, 749 refreshed, 2
pre-existing rows correctly left untouched** (confirmed by direct SQL
before and after) — one of the two, `DUMMYALCAR`, is a hand-inserted
test fixture; the other, `JBCHEPHARM`, a real company absent from this
week's fresh Total Market list, whose stale `nifty_500=true` from
before this run was correctly NOT overwritten, since it was not part of
this run's payload. `nifty_200` count after: exactly 200, matching the
fetched CSV. `nifty_500` count after: 501 — 500 from this run plus the
one stale pre-existing `JBCHEPHARM` row, exactly the "upsert-only, never
silently correct a row this run didn't touch" behaviour intended.

End-to-end, Kite connected: Population A = 1 (HDFCBANK, same case F-54
found), Population B/C = 2,312 (post safety_lists filter), live quotes
returned for 394 of a 400-symbol sample, 0 admissions — run after market
close, so a zero here is the CORRECT answer for an after-hours check,
not evidence the mechanism works; an in-session run with real intraday
movement is still the one demonstration this stage has not yet produced.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

`brain_sunday_chain.yml` gained one new `continue-on-error: true` step
running the refresher weekly — not yet observed running on that
schedule (added this session, next Sunday is the first real test).
Delivery% remains the one Population B/C gap nothing in this entry
closes — it is a bhavcopy-only figure with no live-quote or safety_lists
equivalent, named in F-55 and still true. `intraday_live_requalify_
enabled` (Population A) was re-confirmed still `false` this session —
not re-armed here, left for the operator. `intraday_live_requalify_
unreferenced_enabled` (Population B/C) also still `false`. This is on
`feat/intraday-live-universe`, not merged — Gate D2 remains the
operator's own sign-off. The one exception to "ships OFF" in this
entry's own header: the `nifty_total_market` WRITE itself is real and
live, not gated by a switch — reference-data hygiene, not a trading
decision, and the operator's own request this session.

## 2026-08-23 — F-57 (new, shipped OFF except one live table seed) —
Stage D2d: Population C redefined from "everything Kite knows we don't
track" (2,081 names) to "genuinely new to Kite since the last check"
(0-few) — the operator's own catch, same session as F-56

**Ran:** `tools.verify` (804 checks, 78 modules, green — 5 more than
F-56's 799), a live before/after measurement of Population C's actual
size, a live confirmation the new `kite_symbol_baseline` table seeded
correctly (2,979 rows, matching the plain-symbol Kite universe exactly).

### 1 — THE QUESTION THAT SURFACED THIS: "we cannot scan 2900 stocks"

Immediately after F-56 shipped, the operator asked the right follow-up:
if Population C is meant to catch IPOs, how does quoting ~2,100 names
every 45s make sense, and how does that even differentiate an IPO from
anything else? It doesn't — checked live: Population C (as F-55/F-56
defined it — "Kite mainboard symbol not in nifty_total_market or stock_
data_daily") measured **2,081 names**, against Population A+B's
combined **232**. That population was never actually "new listings"; a
name can sit outside both reference tables for years without ever being
one. The DEFINITION was wrong, not just the size — narrowing the
threshold or sampling fewer of the 2,081 would still be scanning the
wrong population.

### 2 — WHAT GOT BUILT

New `intraday/scanner.py::new_listings()`: diffs TODAY's live `kite_
client.fetch_nse_eq_symbols()` against `kite_symbol_baseline` (migration
100, new table, symbol + first_seen_date), which the function maintains
itself — a symbol present today but never recorded before is genuinely
new, written to the baseline the moment it's found so it is never
reported new again. **Bootstrap handled explicitly:** the very first run
ever (empty baseline) seeds the whole ~2,979-name universe as
already-known and reports ZERO as new — there is no listing history yet
to diff against, so "new" is undefined, not "all of it". Wrapped in its
own try/except (F-56's version left the Kite fetch call inside
`new_listings()` unguarded, relying only on the caller's try/except —
caught and fixed in this same pass, before it shipped, once the test
written to prove the contract failed against the actual code).

`unreferenced_candidates()`'s Population C loop now calls `new_listings()`
instead of `fetch_nse_eq_symbols()` directly — reason string changed from
"Kite instrument master only (likely a recent listing)" to "never seen
in Kite's instrument master before today", now honestly true rather than
a guess.

Also added `_ref_cache`, a once-per-day cache for the three reference
reads Population B needs (`stock_data_daily`, `nifty_total_market`,
`safety_lists`) — none of them change intraday, so re-querying full
tables every 45s across a session (500+ times) was pure waste even
before Population C's own problem. Mirrors `kite_client.py`'s existing
`_instr_cache` pattern exactly.

### 3 — LIVE-VERIFIED, PRECISELY

Before: Population A = 1, Population B = 231, Population C = 2,081
(total quote-fetch burden 2,313). After: Population A = 1, Population B
= 231, **Population C = 0** (total burden 232) — this session's
bootstrap run, confirmed by a direct count against `kite_symbol_
baseline` showing exactly 2,979 rows, matching `fetch_nse_eq_symbols()`'s
own live count exactly. The mechanism has NOT yet been observed
reporting a real, non-zero "new" count, because today's run WAS the
bootstrap — the next session (or the next time a genuinely new symbol
appears in Kite's dump) is the first real test of the diff itself firing
non-trivially. Named as not yet demonstrated, not assumed working.

11 new offline tests (34 total in the scanner module, 804 across the
suite) — bootstrap-reports-zero, steady-state-reports-only-the-diff,
a previously-seen symbol never re-reported within the same process, and
the graceful-degradation contract, demonstrated the same way as the rest
of this stage: written, watched fail against the version that didn't
guard the Kite call, then fixed and re-run green. Existing `unreferenced_
candidates()` tests needed real rework, not just new fixtures — the OLD
tests asserted Kite-only names appeared with an empty baseline fixture,
which is now the bootstrap case (reports nothing) rather than the
steady-state case (reports the diff); left unfixed, they would have
falsely certified behaviour the redesign specifically removed.

### 4 — NOT DONE / WHAT THIS DOES NOT CHANGE

Delivery% and the ASM/GSM/F&O-ban check (closed for B/C in F-56) are
unaffected by this entry — Population C candidates that DO surface from
here forward still go through the same `min_price`/`safety_lists`
checks as before. Both live-requalify switches remain `false`,
unchanged this session. The one write outside a switch, same as F-56:
`kite_symbol_baseline`'s bootstrap seed is real and live — reference-data
bookkeeping with zero trading impact, not a decision gated behind
`propose, never auto-apply`. On `feat/intraday-live-universe`, not
merged — Gate D2 remains the operator's own sign-off.

## 2026-08-24 — F-58 (new, three real fixes shipped; ONE mechanism
deliberately NOT completed, contamination found and not yet resolved) —
Stage D2e: the Milky Mist gap — closes it for the exact case checked,
finds a second production bug and a data-quality problem in the same pass

**Ran:** `tools.verify` (820 checks, 79 modules, green — 16 more than
F-57's 804), live checks against production at every step rather than
trusting the previous step's output, `tools.health` clean.

### 1 — WHAT THIS ADDRESSES

The operator asked directly: is Milky Mist (confirmed live: first traded
18-Aug-2026, five days before F-57's `new_listings()` existed) covered
by the mechanism just built? Checked, not assumed: **no** — its bootstrap
seed had already silently marked it "already known," permanently
indistinguishable from RELIANCE. The operator green-lit fixing this,
scoped explicitly to `raw_prices` (NSE's own bhavcopy) rather than
`stock_data_daily` (gated by an external Chartink dashboard export this
project cannot reconfigure) or `nifty_total_market` (index membership,
an NSE-timeline question, not a "backfill" question) — both traced and
ruled out with the operator in the same conversation, not assumed.

### 2 — THREE REAL FIXES, EACH FOUND BY CHECKING THE PREVIOUS ONE RATHER
THAN TRUSTING IT

**Fix 1 — the actual Milky Mist mechanism.** New `intraday/scanner.py::
_recently_listed()` + RPC `get_raw_prices_first_seen()` (migration 101):
a symbol's own earliest `raw_prices` row, compared against a cutoff.
Wired into `new_listings()`'s bootstrap path so a symbol that predates
the code but is itself recent gets one real look, instead of being
silently absorbed.

**Fix 2 — the INAV false-positive class.** Live-testing fix 1 against
the FULL Kite universe (not a handful of hand-picked symbols) surfaced
~190 non-Milky-Mist "recent" hits in the first batch checked, dominated
by symbols ending `INAV` (Indicative NAV — an ETF's reference/creation-
redemption feed, not a tradeable instrument). Checked against `raw_
prices`' ENTIRE history: zero of 343 have EVER appeared there — not
"recent", structurally never present. Fixed at the source,
`kite_client.py::fetch_nse_eq_symbols()`, so every consumer benefits,
not just this check. Extracted the whole filter into a pure
`_is_mainboard_symbol()` predicate and gave it its own offline test file
(`tests/test_kite_client.py`, 9 checks) — this exact function has now
been wrong twice in one session (`instrument_type=="EQ"` filtered
nothing; `-XX`-suffix-only left INAV in) in ways only live data caught,
and a pure predicate is the only way to make it checkable without a
live Kite session every time.

**Fix 3 — a second silent-truncation production bug, caught before it
ever ran.** While diagnosing fix 2, `sb.table("kite_symbol_baseline").
select("symbol").execute()` — `new_listings()`'s own read of its own
baseline table, 2,979 rows and growing — returned exactly 1,000. The
same PostgREST cap this project has hit and fixed five times before
(`config.py::fetch_all()`'s own docstring lists the casualties). Had
this shipped, `new_listings()` would have re-discovered ~1,979 already-
known symbols as "new" every single day, undoing F-57's entire point on
its first real production run — never triggered live, because the
switch this feeds is still off, but it was moments from being committed
with the bug in it. Fixed with `fetch_all()`, the project's own
established primitive; registered `kite_symbol_baseline`'s verified sort
key (`symbol`, its primary key) in `test_static_analysis.py`'s own
`_FETCH_ALL_SORT_KEY` map — the check built for exactly this failure
mode caught the new call site immediately (`"whose sort key has never
been measured"`) and would not pass until the key was recorded, not
merely used.

### 3 — A REDESIGN WITHIN THE SAME PASS, ALSO CAUGHT BY CHECKING RATHER
THAN TRUSTING

Fix 1's first version compared a symbol's first `raw_prices` row against
`raw_prices`' OWN retention-window start (`rolloff_staging()`, migration
032) plus a 14-day buffer — the idea being that a stock trading since
before the window opened looks the same at 2 years old or 20. Checked
against the full Kite universe rather than trusted: **ZEEMEDIA**, a
long-listed company, has a genuine ~4-week GAP in `raw_prices` coverage
starting 19-May-2026 — nothing to do with a listing event — and was
misclassified as a fresh IPO. `raw_prices` has real per-symbol coverage
gaps the window-relative design had no way to distinguish from a genuine
first trade. **Corrected before it was ever committed**: `intraday_
recent_listing_window_days` (30, replacing the wrong `_buffer_days` key
— the old key deleted, not left stale) is now a fixed, TODAY-relative
cutoff instead — real IPOs are rare enough that trading history older
than 30 days is far more likely an established stock with a coverage
gap than a fresh listing.

### 4 — WHAT THIS ENTRY DOES **NOT** CLAIM, NAMED RATHER THAN GLOSSED
OVER

Re-checking the corrected 30-day design against the full live universe
still returned **179 "recent" symbols**, not the small handful a real
weekly IPO rate would produce. Investigated rather than shipped anyway:
`raw_prices`' own distinct-symbol-per-day count jumped from 2,463
(14-Aug) to 2,624 (17-Aug) to 2,632 (18-Aug) — a ~170-symbol coverage
WIDENING, confirmed via `git log` to be UNRELATED to any code change in
this repo (`ingest_bhavcopy.py` untouched across that window), most
likely an NSE-side bhavcopy source or coverage change. Milky Mist's own
first-seen date (18-Aug) sits inside that same jump, making it currently
indistinguishable from the ~170 names swept in by the coverage change
alone using `raw_prices` data by itself.

**Consequence, stated plainly: the retroactive correction to `kite_
symbol_baseline` — removing Milky Mist and any other genuinely recent
name so `new_listings()`'s normal diff can report them going forward —
was NOT performed this pass.** Doing it against the current 179-name
list would seed real false positives into the one table the whole
mechanism depends on staying accurate. `kite_symbol_baseline` is
unchanged from F-57's bootstrap seed; Milky Mist remains, for now, in
the same blind spot F-57 left it in. The three fixes above (recency
mechanism, INAV filter, pagination) are real, tested, and shipped — the
retroactive backfill they were meant to enable is not, and this is
flagged for the operator's own direction on how to resolve the `raw_
prices` discontinuity (narrow the window further, investigate the NSE-
side cause, or cross-check survivors against Kite's own historical-data
API) rather than guessed at silently.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

Both live-requalify switches remain `false`, unchanged this session —
none of this reaches the live bench regardless of the section 4 finding.
`nifty_total_market` and `stock_data_daily` are untouched by this entry.
On `feat/intraday-live-universe`, not merged — Gate D2 remains the
operator's own sign-off.

## 2026-08-24 — F-59 (new, real fix shipped) — Stage D2f: the Milky Mist
gap closed for real, via NSE's own confirmed IPO archive — replaces
F-58's `raw_prices` approach after the operator scrapped it directly

**Ran:** `tools.verify` (826 checks, 80 modules, green — 6 more than
F-58's 820), `tools.health` clean, live checks against production at
every step — the live fetch, the live write, the live end-to-end
Population C read — not trusted from a dry run alone.

### 1 — THE OPERATOR'S OWN CALL, ACTED ON DIRECTLY

F-58 shipped three real fixes but deliberately did NOT close the Milky
Mist gap itself — the `raw_prices`-based recency signal it depended on
had just been shown to produce 179 false positives from a genuine
coverage discontinuity in `raw_prices`. The operator's response was not
"narrow the window further" — it was to reject the whole approach:
**"you cannot use raw prices count to identify the new listings, it has
n number of different records... unnecessarily complicating the
things."** Instructed to load NSE's own confirmed-IPO data from
`groww.in/ipo` instead and asked how the mechanism should operate going
forward "using kite but I do not want the list of 100 or 200 stocks."

### 2 — GROWW CHECKED FIRST, FOUND INSUFFICIENT, NSE'S OWN API USED
INSTEAD

`groww.in/ipo` was fetched and inspected before building anything: it
shows only 5 closed IPOs via a plain fetch (JS-rendered "View All" not
reachable), and critically **exposes no NSE/BSE trading symbol at all —
company names only**. Using it would have required fuzzy company-name
matching against Kite's own instrument dump, a real source of
misattributed listing dates. Rather than build that, `https://www.
nseindia.com/api/public-past-issues?index=equity` was found and
verified live: a real JSON API behind NSE's own "All Upcoming Issues"
page, **1,411 records back to 2003-01-02, every one carrying the actual
NSE tradingsymbol directly**. The operator independently pasted a
~50-row excerpt of this exact same NSE page mid-conversation, before the
API was confirmed reachable — cross-validated: MILKYMIST's entry in both
matches exactly (listed 18-Aug-2026, EQ, issue price ₹140).

### 3 — WHAT GOT BUILT

New table `ipo_listings` (migration 102: symbol, company_name,
security_type, issue_price, price_range_low/high, issue_start/end_date,
listing_date, source, refreshed_at) + `swing/ingestion/ingest_ipo_
listings.py`, reusing `ingest_asm_gsm.py`'s proven nseindia.com session-
warmup pattern rather than reinventing one. One real bug found and fixed
before the first live write: NSE's archive carries one row per BOND/NCD
TRANCHE, not per company — IBULHSG alone appeared 13 times, all sharing
one symbol — so a raw upsert against `symbol` as primary key raised
Postgres error 21000 ("cannot affect row a second time") before writing
anything. Fixed with a dedup-on-symbol pass in `build_rows()`, kept
pure and covered by 8 offline tests including the exact IBULHSG case.

New `scanner.py::recent_ipo_candidates()`: mainboard (`security_type ==
'EQ'`) listings within `intraday_ipo_recency_days` (45, migration 102) —
measured **17 real names live**, 24-Aug-2026, matching real IPO cadence
directly, not an artifact of a proxy signal. Wired into `unreferenced_
candidates()` as a SECOND, independent Population C source alongside
`new_listings()`'s Kite-diff — deliberately redundant: Kite's own dump
updates daily and can flag a listing the same day it starts trading;
`ipo_listings` only refreshes weekly (matching `nifty_total_market`'s
cadence) but is authoritative. Either source missing a name on a given
day does not silently drop it — `unreferenced_candidates()` merges and
dedupes both. `new_listings()`'s bootstrap reverted to its simple F-57
shape (seed everything, report nothing on the very first run) — the
raw_prices-based recency-aware bootstrap logic is gone entirely, not
patched; `recent_ipo_candidates()` covers the same gap correctly from
authoritative data instead.

Migration 103 drops the now-dead `get_raw_prices_first_seen` RPC and
deletes `intraday_recent_listing_window_days` from `system_config` — F-58's
migration 101 is committed history and is not rewritten; this is the
correction of record, the same pattern migration 098 used for 097's
stale text after it, too, was already committed.

### 4 — LIVE-VERIFIED, PRECISELY

Real write against production: **1,359 rows** (1,411 fetched, 52
deduped bond/NCD tranches). MILKYMIST confirmed present: `listing_date
= 2026-08-18`, `security_type = EQ` — exactly matching both the
operator's own pasted NSE excerpt and F-58's earlier `raw_prices`
finding. `recent_ipo_candidates()` run live returned exactly 17 names,
MILKYMIST among them, zero contamination — no INAV, no ETF, no bond
codes, no long-listed companies misclassified. Full `unreferenced_
candidates()` end-to-end: Population A = 1, Population B = 231,
Population C(i) Kite-diff = 0, Population C(ii) NSE IPO archive = 17 —
**249 total, down from the 2,313 F-55 originally measured and the
179-contaminated design F-58 correctly refused to ship.**

No retroactive correction to `kite_symbol_baseline` was needed —
`recent_ipo_candidates()` is a fully independent source that does not
route through the baseline table at all, so Milky Mist is covered
immediately, in this session, without touching the table F-57's
bootstrap had already (harmlessly, as it turns out) seeded it into.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

`ipo_listings` covers listings NSE's own archive has recorded — SME/BE/
IV/bond-series rows exist in the table (1,359 total vs 17 EQ-recent) but
are deliberately excluded from Population C; a future stage could widen
this if SME-board intraday coverage is ever wanted, not assumed needed
now. Both live-requalify switches remain `false`, unchanged this
session. On `feat/intraday-live-universe`, not merged — Gate D2 remains
the operator's own sign-off.

## 2026-08-24 — F-60 (new, real gap closed) — Stage D2g: the Kite
same-day diff was letting ETF launches through as "new listings" — the
operator's own question, checked live rather than assumed answered by
F-59

**Ran:** `tools.verify` (832 checks, 80 modules, green — 6 more than
F-59's 826), `tools.health` clean, live checks against production
before writing any code.

### 1 — THE QUESTION

Immediately after F-59, the operator asked two things: why keep the
Kite same-day diff at all now that `ipo_listings` exists, and — sharper
— how is that diff avoiding noise like ETFs and surfacing purely real
newly-listed stocks? The honest answer required checking, not asserting
the design was already sound.

### 2 — WHAT WAS FOUND: IT WAS NOT AVOIDING THEM

Checked live: `kite.instruments("NSE")`'s own `instrument_type` field
reads `"EQ"` for NIFTYBEES and GOLDBEES (both ETFs) exactly as it does
for RELIANCE and MILKYMIST. Nothing in `fetch_nse_eq_symbols()`'s
existing filter (suffix pattern, INAV exclusion) can tell an ETF from a
stock — Kite's structured data carries no field for that distinction at
all. Counted: **294 ETFs** currently sit in the same 2,636-symbol "plain
mainboard" universe real stocks occupy. Every one is harmlessly already
in `kite_symbol_baseline` from F-57's original bootstrap, but a NEW ETF
launch tomorrow would have been diffed and reported by `new_listings()`
exactly like a genuine stock IPO — this was a real, live gap, not a
hypothetical one, sitting in code already on this branch.

### 3 — WHY `ipo_listings` DOES NOT HAVE THIS PROBLEM, CHECKED NOT
ASSUMED

Queried the 1,359-row archive for anything ETF-like: two hits, both
false alarms on inspection — `SBIFUNDS` (SBI Funds Management Limited,
the asset-management COMPANY's own equity IPO, correctly `EQ`) and
`IRBINVIT` (an InvIT, correctly `security_type='IV'`, already excluded
by `recent_ipo_candidates()`'s own `EQ`-only filter). Zero true ETFs.
This is structural, not incidental: ETFs list on NSE via an NFO (New
Fund Offer), a completely different mechanism from an IPO, so they can
never appear in an IPO archive at all — this is the actual reason
`ipo_listings` is the source that answers "genuine new listing"
precisely, and the Kite diff, however useful for same-day latency,
cannot.

### 4 — THE FIX

`kite_client.py`: `_instr_cache` now also caches each symbol's Kite
`name` (already being fetched, previously discarded) alongside the
existing symbol set — no extra API call. New `is_etf_name(symbol)`:
the one signal Kite's data offers, a text check for "ETF" in that
`name` field. Not perfect (text-based), but real, and the only field
that distinguishes NIPPON INDIA ETF NIFTY 50 BEES from RELIANCE
INDUSTRIES at all.

`scanner.py::new_listings()`: filters `is_etf_name()` hits OUT of what
gets REPORTED as new — but a filtered-out ETF is still written to
`kite_symbol_baseline` exactly as before, so it is recorded once and
never re-evaluated on a later run either; only reporting changes, not
bookkeeping. Live-verified: `NIFTYBEES`/`GOLDBEES` correctly classified
`True`, `RELIANCE`/`MILKYMIST` correctly `False`.

6 new offline tests (4 for `is_etf_name()` in `test_kite_client.py`,
covering the exact NIFTYBEES-vs-RELIANCE case plus the "unknown symbol
defaults to not-ETF, not silently dropped" cold-start rule this project
applies everywhere; 2 for `new_listings()`'s reporting-vs-seeding split
in `test_scanner_live_requalify.py`).

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

The ETF check is text-based against Kite's `name` field — an ETF whose
name happens not to contain the literal substring "ETF" (uncommon on
NSE, not verified to be impossible) would still pass through
unfiltered; `recent_ipo_candidates()`'s structural exclusion (ETFs
cannot appear in an IPO archive at all) remains the stronger of the two
guarantees, which is the answer to the operator's own "why do we need
both" question. Both live-requalify switches remain `false`, unchanged
this session. On `feat/intraday-live-universe`, not merged — Gate D2
remains the operator's own sign-off.

## 2026-08-24 — F-61 (new, three real fixes shipped) — Stage D2h: is the
~270-name universe actually SAFE to pick from, not just wide — audited,
three real gaps found and closed, none of them assumed away

**Ran:** `tools.verify` (852 checks, 82 modules, green — 19 more than
F-60's 833), `tools.health` clean, every isolation property demonstrated
through the REAL consumer (`Allocator._prior_for()`, `simulate_fill()`),
not asserted from the key-builder alone.

### 1 — THE QUESTION

The operator's own follow-up, after F-60 closed the ETF gap: now that
intraday's watched universe has grown to ~270 names spanning IPOs, small
caps and large/mid caps, are the mechanisms that actually PICK a trade
from that pool built for it — or only for the ~40-120 indicator-rich
names it used to be? Dispatched to a dedicated audit (6 numbered
questions: do the 7 engines misread Population B/C's missing history;
does sizing scale with liquidity; does the paper broker model worse
slippage for thin names; do priors segment by population; do engines
need history a fresh IPO lacks; is anything treated more cautiously
post-admission) rather than answered from memory.

### 2 — WHAT THE AUDIT FOUND, VERIFIED AGAINST THE LIVE CONFIG

Good news first: the 7 engines do NOT misread missing history as zero —
a missing ATR falls back to a fixed 2.0%/1.5% assumption, a missing
volume average disables (not corrupts) the volume-confirmation check,
and no engine sizes a stop off ATR at all (`risk_from_structure()` uses
structural price levels exclusively). Nothing crashes, nothing computes
garbage.

Three real, unaddressed gaps, confirmed rather than inferred:

1. **Position sizing was flat** — `intraday_max_position_pct`, the same
   fraction of capital for RELIANCE and a 5-day IPO alike.
2. **Paper slippage was flat** — one `cost_slippage_bps` figure for
   every symbol, so Population B/C's paper P&L (the population least
   liquid, needing the MOST caution) would read more optimistic than a
   real fill in those names would achieve.
3. **Priors were unsegmented** — `_engine_of()`'s grouping had no
   population axis, so a noisy run of newly-admitted trades on one
   engine would move that engine's mean R for every established
   proposal too — the same "borrowed from a neighbouring class" failure
   this module's own SHORT/LONG split already exists to forbid, on a
   different axis.

One incidental finding, checked against the live `system_config` table
rather than left as a static-analysis guess: `ctx.value_cr` was
permanently `None` for any Population B/C name (no `stock_data_daily`
row to source it from), and `overlay_liquidity_strict=true` (confirmed
live) meant `liquidity_ok()` would refuse EVERY one of them outright —
"no traded-value data — cannot confirm an exit is possible". Today this
happens to double as a safety net (nothing armed can size a position at
all), but it also meant the widened universe would not have actually
DONE anything yet even with both switches armed, until fixed.

### 3 — THREE FIXES, EACH REUSING AN EXISTING MECHANISM RATHER THAN
INVENTING A NEW ONE

**Sizing.** New `analysis/overlays.py::liquidity_capped_budget()` reuses
`liquidity_ok()`'s OWN math (a position's share of the name's daily
traded value) to cap the budget BEFORE quantity is computed, instead of
sizing flat and refusing outright afterward. A thin name is now sized
DOWN to what it can absorb rather than all-or-nothing. Fixed the root
cause of the incidental finding in the same pass:
`intraday/engine.py::refresh_contexts()` now falls back to the bench's
own live-admission-time `value_cr` (`scanner.UniverseEntry.value_cr`,
real for anything `live_requalify()` has admitted) when `stock_data_
daily` has no row — so Population B/C names are sizeable at all, not
just sizeable-and-immediately-refused.

**Slippage.** `execution/paper_broker.py::_slippage_pct()` gained an
optional `value_cr` parameter (`None` preserves the exact prior flat
behaviour — every pre-existing call site, including swing's own entry,
is unaffected) that scales slippage up (`cost_slippage_thin_multiplier`,
3x default) below a liquidity threshold (`cost_slippage_thin_threshold_
cr`, 25cr default — matching `intraday_min_turnover_cr`'s own figure
rather than inventing a second one). Threaded from `Setup.meta["value_
cr"]` (stamped at detection, registry.py, mirroring the existing `atr_
pct_daily` pattern exactly) through the intraday paper-entry call site.

**Priors.** The most delicate of the three, given this exact module's
own documented history of prior-key bugs (the 10-Aug prefix mismatch,
the 16-Aug dedup-arithmetic fix, the taken-only inversion). Deliberately
NOT rewritten in place — `_intraday_priors_from_rows()` was RENAMED to
`_intraday_priors_single_population()`, byte-identical internals, and a
new thin `_intraday_priors_from_rows()` wrapper splits rows by `meta.
universe_population` (via `_population_class()`, same defensive JSON-
string read `_engine_of()` already needs) and calls the unchanged
function once per population, remapping the admitted call's keys with
`/ADMITTED` inserted before any `/SHORT` suffix — mirroring exactly how
this module already isolates SHORT from LONG. `Allocator._prior_for()`
(allocator.py) gained a parallel ladder for an admitted proposal (`meta.
universe_population` threaded through `Proposal.meta`, `allocation/
proposal.py::from_intraday()`) that NEVER falls through to established's
own numbers — ending at the same neutral cold-start distribution the
rest of the ladder ends at, exactly the same isolation the SHORT ladder
already enforces for direction, just on a new axis.

A real bug caught before it ever ran: the first version of the key-remap
rebuilt `Prior` objects field-by-field, silently dropping `median_r`/
`stderr`/`p10`/`p90`/`trigger_rate`/`below_floor` (the dataclass has more
fields than `mean_r`/`n`/`note`) and passing `usable` as a constructor
argument — it is a read-only `@property`, not a field. Fixed with
`dataclasses.replace()`, which copies every field except the one named.

### 4 — VERIFIED THROUGH THE ACTUAL CONSUMER, NOT THE KEY-BUILDER

19 new offline tests. The population-isolation claim specifically is
tested by building `Allocator._prior_for()` calls, not by inspecting
`intraday_priors()`'s output dict — same discipline this file's own
`test_the_prior_dict_is_keyed_the_way_the_allocator_looks_it_up` already
established, for the same reason (the 10-Aug bug was two strings that
looked interchangeable). Demonstrated the isolation test actually
detects a regression, not just passing trivially: patched `_population_
class` to always return "established" (simulating the split disabled)
and confirmed the admitted-proposal test fails — it degrades to the
neutral cold-start prior rather than silently reading established's
positive one, because `Allocator._prior_for()`'s own admitted-ladder
isolation is a second, independent backstop even when scoring.py's
classification is wrong.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

Priors for Population B/C's OWN engines start at zero samples the day
the switches are armed — correctly cold-start-permissive (neutral, not
punitive), per this project's own hard-won rule, but genuinely unproven
until real admitted trades accumulate; this fix does not and cannot
manufacture that history early. Sizing and slippage changes apply
uniformly by liquidity, not by population label specifically — an
established name that happens to be thin gets the same treatment,
which is correct (the real question was always liquidity, not which
population found the name) but worth naming as a broader effect than
"only Population B/C changed". Both live-requalify switches remain
`false`, unchanged this session. On `feat/intraday-live-universe`, not
merged — Gate D2 remains the operator's own sign-off.

## 2026-08-24 — F-62 (new, shadow mechanism built) — Stage D3: the
event-driven core, in shadow only. RENUMBERED FROM F-54 AT MERGE TIME
(24-Aug-2026, integration branch `feat/intraday-evolution`) — this entry
was originally written on `feat/intraday-event-core`, branched off `main`
in parallel with `feat/intraday-live-universe` (Track D Stage D2, F-54
through F-61 immediately above), and both entries independently claimed
F-54 exactly as each one's own text anticipated ("renumbering happens
once branches actually merge"). This is that renumbering: D2's sequence
was left untouched since it was already internally consistent (F-54–61),
and this entry — along with Stage D4's and D5's own F-54 entries further
below — was shifted to continue the SAME single sequence in merge order.
No content below this line was altered, only the header number and this
paragraph.

**Ran:** `tools.verify` (789 checks, 79 modules, green), `tools.health`
clean, `tools.event_core_compare` run live against production (honest
zero — nothing has run in shadow yet).

### 1 — WHAT THIS ADDRESSES

`docs/TRADEOS_ROADMAP.md`, Track D, Stage D3. The existing loop evaluates
every watched symbol on a fixed 15s timer (`intraday_eval_interval_s`)
regardless of when it actually moved — `intraday/price_feed.py`'s own
docstring says so outright: "TICKS UPDATE STATE; A TIMER DECIDES... It
deliberately does not call back into decision logic on every tick." This
stage measures what a tick-triggered alternative would have looked like,
side by side, before ever proposing to replace it — per the operator's
own instruction, "no scope for error," it does not replace anything yet.

### 2 — THE ARCHITECTURAL DECISION: SAME THREAD, NOT A WORKER THREAD

Considered and rejected: a dedicated background thread reacting to ticks
in real time. `intraday/engine.py`'s mutable state (`self._contexts`,
`self._bench`, open positions) was never built for concurrent access —
introducing a second thread that reads it would be a genuinely new class
of bug this project has never had to guard against, for a feature whose
entire purpose is measuring a latency improvement, not chasing the
smallest possible one. Instead: `intraday/price_feed.py` gained thread-
safe "dirty symbol" tracking (`drain_dirty()`, fed by the SAME websocket
thread that already writes `_px`/`_at`, itself unchanged), and
`intraday/event_core.py::check()` runs from `intraday/run.py`'s own main
loop on its own tight timer (`intraday_event_core_interval_s`, 2s
default) — still far tighter than the 15s polling cycle, with zero new
concurrency surface beyond the queue itself.

### 3 — WHAT GOT BUILT

`price_feed.py`: a symbol is marked dirty when its price has moved
`intraday_event_core_dirty_pct` (0.05% default) since the LAST DRAIN —
not the last tick, which would fire on ordinary noise. O(1), no I/O, no
logging — the same rule `on_ticks()` itself is already built on.

`event_core.py::check()`: drains dirty symbols, reuses (not
reimplements) `apply_live_quotes()`/`merge_live_bars()` — the SAME two
calls the polling cycle already makes every 15s — then calls the SAME
`registry.evaluate_all()` the polling loop trusts. Writes ONLY to the
new `intraday_event_shadow` table (migration 105) — never
`intraday_setups`, never `execution.paper_broker`, never
`allocation.allocator`. A bug here can pollute only its own shadow log.

A real gap found while building the comparison tool, not before: Gate
D3's own stated criterion is "measured latency improvement in seconds",
and `intraday_setups` carried NO detection timestamp at all — only
`scored_at` (end of day). Fixed with migration 106
(`intraday_setups.detected_at`, nullable, stamped going forward only,
no backfill of history that genuinely does not have it).

`tools/event_core_compare.py`: matches a shadow detection to a trusted-
loop detection by (symbol, sub_engine) within a 60s window, reports
matched/shadow-only counts, mean latency gap, and any matched pair whose
direction disagreed. A real bug caught by this project's OWN static-
analysis check before it ever ran: the first version read
`intraday_setups` with a plain day-filtered `.select()` — this project's
own `test_static_analysis.py` explicitly excludes that table from "a day
filter alone is enough" (it re-records a lingering setup on every cycle
it is still near its level; 234 rows for 23 distinct ORB setups,
measured live elsewhere this project), so even one day's rows can exceed
PostgREST's 1000-row cap. Fixed with `config.fetch_all()`.

### 4 — VERIFIED, PRECISELY WHAT WAS AND WAS NOT DEMONSTRATED

35 new offline tests. `tools.event_core_compare` run live against
production: correctly reports zero matches, zero shadow-only, because
`intraday_event_core_enabled` ships `false` and nothing has run in
shadow yet — an honest zero, not a fabricated one.

**Not demonstrated, and cannot be from a single session:** Gate D3
itself needs 10 trading sessions or 200 directly-comparable decisions,
real elapsed market time no amount of building tonight can substitute
for. This entry documents the mechanism being READY to start
accumulating that evidence, not the evidence itself.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

`intraday_event_core_enabled` ships `false`. Nothing in the trusted
polling loop changed — `cycle()`, `_record_setup()`,
`evaluate_intraday_setups()` are byte-identical except the one additive
`detected_at` field. Written on `feat/intraday-event-core`, since merged
into `feat/intraday-evolution` — per the operator's own stated plan, Gate
D3 (like Gate D2 before it) is deferred to a single holistic pass across
every Track D stage, not cleared stage-by-stage.

## 2026-08-24 — F-63 (new mechanism built + a real gate bug caught before
shipping) — Stage D4: execution-quality depth gate. RENUMBERED FROM F-54
AT MERGE TIME (24-Aug-2026, integration branch `feat/intraday-evolution`)
— written on `feat/intraday-depth-gate`, branched off `main` independently
of D2/D3/D5, each of which also claimed F-54 on their own branch exactly
as this entry's own text anticipated. See F-62's own renumbering note
immediately above for the general explanation; this entry continues the
same single merged sequence. No content below this line was altered, only
the header number and this paragraph.

**Ran:** `tools.verify` — 790 checks, 79 modules, green (main's own 763
plus 27 new). `tools.health` clean, 24/24. `tools.simulate` clean, no
regression (session CLOSED at run time, so the entry path was exercised
but nothing fired to reach the new gate).

### 1 — WHAT THIS ADDRESSES

`docs/TRADEOS_ROADMAP.md`, Track D, Stage D4: "a sanity gate at the
moment of an already-decided entry — refuse or flag when the spread is
abnormally wide relative to the stock's own norm, or resting depth cannot
absorb the intended quantity without material slippage. Not prediction,
protection — same shape as the existing `BLOCKED_LIQUIDITY`/
`BLOCKED_STRUCTURE` gates, one more row in the same table."

### 2 — SCOPE DECISION: FULL MODE ON context_symbols() ONLY, NOT THE BENCH

Verified live (23-Aug-2026) rather than assumed: Kite's 3,000-instrument
per-connection subscription ceiling applies uniformly across LTP/QUOTE/
FULL — there is no separate, stricter cap for FULL mode. FULL mode is
still scoped to `context_symbols()` (positions ∪ the live universe,
~40-120 names) rather than the whole ~270-name bench for bandwidth, not
subscription count: `price_feed.py`'s own existing comment already named
"large bandwidth cost" as the reason FULL mode was never requested for
everyone, and only `context_symbols()` can ever actually generate an
entry decision — the engines only evaluate `self._contexts`, built from
that same set.

### 3 — DESIGN: DEPTH FLOWS THROUGH SymbolContext, NOT A THREADED PARAMETER

`evaluate_intraday_setups()` does not receive `feed` in its own
signature, and this codebase's established pattern for handing a live
value to the engines is "carry it on the context, never call across"
(the same shape `quote()`/`apply_live_quotes()` already use for
day-range/VWAP). `SymbolContext.depth: dict | None = None` follows that
precedent instead of adding a new parameter path.

### 4 — TWO REAL BUGS, BOTH CAUGHT BEFORE SHIPPING ARMED

**a) A capture that could not capture.** `on_ticks()`'s depth-capture
block was originally written AFTER `if not self._capture_quote:
continue`. `_capture_quote` is `intraday_quote_mode_range OR
intraday_quote_mode_vwap` — a switch about OHLC/VWAP overlay that has
nothing to do with FULL mode. Left as written, arming
`intraday_depth_mode_enabled` alone would have put a symbol into FULL
mode, real depth-carrying ticks would have arrived from Kite, and the
handler would still never have stored one into `self._depth` unless an
unrelated quote-capture switch also happened to be on. Fixed by moving
the depth-capture block above the `_capture_quote` gate, unconditional
like the `bars.record_tick()` call beside it.

**b) A gate that could not gate — found while moving this work onto its
own branch, before anything was committed.** `set_depth_symbols()` had NO
config check of its own; `run.py`'s slow-timer call to it was
unconditional. That means arming nothing at all — `intraday_depth_mode_
enabled` left at its shipped `false` — would still have put every
`context_symbols()` name into REAL Kite FULL mode on every daemon cycle,
the moment this code ran. Migration 107's own comment describes both
switches as "ships FALSE... pure capture, nothing changes downstream by
itself" — that claim was false for the first version of this code. Fixed
by checking `intraday_depth_mode_enabled` inside `set_depth_symbols()`
itself (not only at the call site, so no future caller can reintroduce
the gap), and by treating a disabled switch as "the desired depth set is
empty" rather than an early return — the same revert-to-baseline path
already used for a symbol dropped from `context_symbols()` now also
un-asks for depth if the switch is turned off mid-session, not just
refuses new asks. Both bugs are the identical shape CLAUDE.md already
names — "a check that cannot fail is not a check" — applied here to a
capture and a gate instead of a verdict.

### 5 — WHAT GOT BUILT

`price_feed.py`: `depth(symbol)` accessor (same "None means no data yet,
never an empty book" contract as `quote()`, same shallow-copy-on-read
defensive pattern); `set_depth_symbols(symbols)` — gated on
`intraday_depth_mode_enabled`, self-contained (does NOT rely on
`resubscribe()` having just run — that method only re-applies mode when
the overall watch LIST changed; the depth-worthy subset can change
composition on its own faster cadence without the watch list changing at
all) — diffs against the current FULL-mode set, puts added symbols into
`MODE_FULL`, reverts removed ones to the connection's own baseline mode
and drops their now-stale depth.

`engine.py::apply_live_depth(feed)`: same shape as `apply_live_quotes()`
— gated on `intraday_depth_mode_enabled`, copies `feed.depth(sym)` onto
`ctx.depth` for every context with data, returns the touched count,
wired into `cycle()` beside the existing quote/bar overlays.

`run.py`: `feed.set_depth_symbols(engine.context_symbols())` added to the
existing 300s slow timer, right after `feed.resubscribe(...)`.

`analysis/overlays.py::depth_ok(depth, side, planned_qty)`: same shape as
`liquidity_ok()` — refuses when the live bid/ask spread exceeds
`intraday_max_spread_pct` (0.25% default), or when the resting quantity
on the CONSUMING side (asks for a BUY, bids for a SELL) summed across the
top `intraday_depth_levels_checked` (3 default) levels is less than the
planned quantity. `None`/empty/zero-price depth is waved through as
advisory-only — capture-side plumbing must never be why a trade is
blocked, only a measured bad book may block it. Wired into
`evaluate_intraday_setups()` immediately after the existing
`BLOCKED_LIQUIDITY` gate, recording a new `BLOCKED_DEPTH` verdict through
the same `_record_setup()` path every other gate uses.

Migration 107: `intraday_depth_mode_enabled` and `overlay_depth_enabled`
as two SEPARATE switches (capture vs. act — the same split this project
already uses for `intraday_event_core_enabled` vs. what reads it on the
D3 branch), plus `intraday_max_spread_pct` and `intraday_depth_levels_
checked`. All four ship at their stated defaults with the two bools
`false`.

27 new offline tests across three modules (`test_overlays_depth.py`,
`test_price_feed_depth.py`, `test_apply_live_depth.py`), registered in
`tools/verify.py`. `test_price_feed_depth.py` carries five tests aimed
directly at bug (b) above: switch-off-by-default takes zero live action
even when fully wired and connected, and a symbol already in FULL mode is
actively reverted the next time the switch reads false.

### 6 — A BRANCHING CORRECTION MADE MID-SESSION

This work was first written directly onto `feat/intraday-event-core`
(Stage D3's own branch), which would have carried D3's commit into a
stage the roadmap specifies as independent. Caught before committing:
the uncommitted D4 diff was stashed, `feat/intraday-depth-gate` was
branched fresh off `main` per the roadmap's own naming, and the diff was
reapplied there — two merge conflicts (D3's dirty-symbol-tracking `__init__`
fields and its `tools/verify.py` registrations, both absent from `main`)
resolved by keeping only the D4 content. `git log main..feat/intraday-
depth-gate --oneline` was empty before this branch's first commit,
confirming the base is clean.

### 7 — VERIFIED, PRECISELY WHAT WAS AND WAS NOT DEMONSTRATED

`tools.verify`: 790/790. `tools.health`: 24/24, including `kite` (live
session confirmed) and `feed` (tick handler confirmed I/O-free at the
current QUOTE mode). `tools.simulate`: ran the real entry path
end-to-end with no exception — session was CLOSED at run time (02:10
IST), so zero setups fired and the depth gate was never reached live.
The offline tests demonstrate `depth_ok()` actually REFUSING a realistic
wide-spread and thin-resting-depth book (not only passing a good one),
and demonstrate `set_depth_symbols()` actually taking ZERO live action
while its switch is off (not only acting correctly once armed) — this
project's own "a check that cannot fail is not a check" rule, satisfied
offline since no live thin book or live-armed session was available to
test against tonight.

**Not demonstrated, and cannot be from a single session:** Gate D4 itself
— "depth data confirmed flowing and logged for the live universe; a
demonstrated refusal on a real thin-book case; no change to any
candidate that already had a healthy spread" — needs a live market
session with `intraday_depth_mode_enabled` armed, which this session did
not run during. Deferred to the same single holistic pass across every
Track D stage as Gate D2 and Gate D3, per the operator's own stated plan.

### 8 — NOT DONE / WHAT THIS DOES NOT CHANGE

Both `intraday_depth_mode_enabled` and `overlay_depth_enabled` ship
`false`. No FULL-mode subscription is requested, no context carries
depth, and `depth_ok()` returns `(True, "depth gate disabled")`
unconditionally until both are armed. Nothing in the existing entry path
changed for any candidate — `BLOCKED_DEPTH` is a new possible verdict,
not a replacement for any existing one. Written on `feat/intraday-depth-
gate`, since merged into `feat/intraday-evolution`.

## 2026-08-24 — F-64 (new mechanism built + two real bugs caught before
shipping) — Stage D5, Stage 1 only: the same-day self-monitor,
CALIBRATION-ONLY. RENUMBERED FROM F-54 AT MERGE TIME (24-Aug-2026,
integration branch `feat/intraday-evolution`) — written on `feat/intraday-
regression-shadow`, branched off `main` independently of D2/D3/D4, each of
which also claimed F-54 on their own branch exactly as this entry's own
text anticipated. See F-62's own renumbering note above for the general
explanation; this is the fourth and final entry in that same merged
sequence (F-54–61 D2, F-62 D3, F-63 D4, F-64 this one). No content below
this line was altered, only the header number and this paragraph.

**Ran:** `tools.verify` — 788 checks, 78 modules, green (main's own 763
plus 25 new). `tools.health` clean, 24/24 (surfaced one unrelated real
finding — see §6). `tools.same_day_calibration` run live against
production twice (once exposing each of the two bugs below, once clean
after both fixes).

### 1 — SCOPE DECISION, MADE WITH THE OPERATOR BEFORE BUILDING

D5's own roadmap text bundles two different mechanisms under one branch
without specifying either's shape — a general regression model AND a
same-day self-monitor. Asked the operator directly rather than guessing;
answer was "both, self-monitor first" — build the well-specified same-day
monitor this session, treat the general regression as a separate later
session once there is real calibration evidence to answer its own open
design questions (target variable, feature set, sample-size floor) with,
rather than guessing at them tonight.

### 2 — RE-READING STAGE 1 CORRECTLY: NOTHING TOUCHES LIVE SIZING YET

D5's own three sub-stages are calibration → proposal → armed, and Stage
1's own words are "the model computes predictions against ALREADY-
RESOLVED HISTORY and logs its own predicted-vs-actual accuracy... nothing
here is visible outside this pipeline yet." That means Stage 1 does not
wire anything into `engine.py`'s entry path at all — it backtests the
statistic against history to see whether the flag has any real predictive
validity BEFORE it is even allowed to become a `brain_proposals` row for
human review (Stage 2), let alone armed (Stage 3, the operator's own
decision). This session built exactly that and nothing more.

### 3 — WHAT GOT BUILT

`allocation/scoring.py`: `Prior.hit_rate` (fraction of a group's own
observations with R > 0), computed in `_dist()` from the exact same
`values` list `mean_r`/`median_r` already come from, appended as the
dataclass's LAST field so no existing positional `Prior(...)` construction
anywhere in this codebase needs to change.

`same_day_fit_multiplier(engine_family, historical, today_wins, today_n)`:
a bounded, ONE-DIRECTIONAL dampener — an exact one-sided binomial test
(`scipy.stats.binomtest`, already a project dependency, previously unused)
asking whether today's win rate for one engine is a genuine statistical
outlier BELOW its own historical rate, never a boost for a good day (this
project has already been burned once by treating "looks good on a small
same-session sample" as signal — hurdle.py's STRONG-bucket history).
Ships at weight 0.0, an exact no-op, same "shipped inert pending
validation" precedent `regime_fit_multiplier` and `rank_weight_tier`
already set. Pure — no I/O — for the identical reason `score()` itself is
protected as pure arithmetic (CLAUDE.md).

`tools/same_day_calibration.py`: walks every trading day `intraday_setups`
has resolved history for and asks, per engine, walk-forward (historical
prior built from STRICTLY earlier days only — the identical non-
negotiable `PHASE_E_HISTORICAL_REPLAY.md` states for Stage 3, applied
here for the same reason): would the same-day monitor have flagged this
day, and does the flag actually correlate with a day that was unusual?
Writes to `intraday_same_day_calibration` (migration 108). Explicitly
scoped in its own docstring as a SAME-DAY-level calibration, not a
within-day one — the true design question ("once the flag fires mid-
session, does the REST of the day do worse") needs a real chronological
ordering of same-day resolutions, and `intraday_setups.detected_at`
(migration 106) exists only on the unmerged `feat/intraday-event-core`
branch, stamped going forward from 24-Aug-2026 only. Recorded as a
limitation rather than answered by guessing.

### 4 — TWO REAL BUGS, BOTH CAUGHT BY ACTUALLY RUNNING THE TOOL AGAINST
PRODUCTION, NOT BY INSPECTION

**a) One setup's re-records counted as hundreds of independent trades.**
The first version computed today's win/loss count by calling
`_row_gross_r()` directly on every raw TAKEN row for an engine on a day,
without the (symbol, engine, trade_date) collapse `_intraday_priors_
from_rows` already performs for the historical side. `intraday_setups`
carries one row per (setup, evaluation cycle) — a setup lingering near
its level is re-recorded roughly every 15s while it stays live, the exact
"ONE SETUP IS ONE OBSERVATION, NOT ONE PER 15s CYCLE" landmine this
file's own CLAUDE.md entry already documents (RNG's n=11 once being one
setup counted eleven times). The first live run reported GAP at
`today_n=670` on 20-Aug-2026 — no engine takes 670 trades in one session;
average `today_hit_rate` across all 53 un-deduped pairs was a suspicious
0.354 with `today_n` running as high as 670. Fixed by feeding the day's
TAKEN rows through `_intraday_priors_from_rows(taken, floor=1)` — the
SAME trusted dedup/R-conversion machinery the historical side already
uses — rather than a second, separately-written copy of that arithmetic.
Pre-filtering to TAKEN-only first makes that function's own taken-only/
fallback branch a true no-op (every input row already qualifies), so this
gets the correct dedup without inheriting any risk of silently borrowing
from refused detections. Re-running against the (now correctly cleared)
production table dropped the pair count from 53 to 22 and every `today_n`
into a plausible 5–19 range. A pin test
(`test_repeated_same_day_rerecords_of_one_setup_collapse_to_one_trade`)
asserts 20 re-records of one setup collapse to n=1, not 20.

**b) A calibration that could never flag anything.** The second live run
(post-fix-(a)) reported `0 of 22 pair(s) would have been flagged`, and
every single row's `reason` read "weight 0 or no engine" — the shipped
live `intraday_same_day_fit_weight` is 0.0 (Stage 1's whole point is that
nothing is armed yet), and `same_day_fit_multiplier()`'s first guard
clause reads that config value directly, so every call in the calibration
walk hit the no-op branch before the binomial test ever ran. A
calibration that structurally cannot flag anything is not a calibration,
it is a tautology — the identical "a check that cannot fail is not a
check" principle applied to a check that could not even RUN. Considered
and rejected: wrapping the calibration in `tests.cfg_ctx`, which fully
REPLACES the in-process config cache rather than overriding one key — a
production tool run against real data doing that would silently
substitute defaults for every OTHER live-configured switch during its
scope (`priors_intraday_taken_only`, `alloc_intraday_confidence_bands`),
a test-isolation tool leaking into runtime code. Fixed with an explicit
`probe_weight` parameter on `same_day_fit_multiplier()` — the same
"supply the population instead of fetching it" shape `intraday_priors(sb,
rows=...)` already uses in this same file — defaulting to `None` (read
config exactly as before, so every live call site is unaffected) but
letting the calibration tool pass `1.0` explicitly ("what would this have
flagged at the most a real arm could ever do"), visible in the call
itself rather than hidden in a context manager.

### 5 — VERIFIED, PRECISELY WHAT WAS AND WAS NOT DEMONSTRATED

25 new offline tests across two modules (`test_same_day_fit.py`,
`test_same_day_calibration.py`), including a pin for each bug in §4.
`tools.same_day_calibration` run live against production (15,845 resolved
detections loaded): **22 (engine, day) pairs ever reached the 5-trade
same-day floor, across 4 engines and 10 distinct days; 19 of those had a
usable (30+ sample) historical prior; 0 of 22 were flagged even at full
probe weight (1.0).** The worst same-day pair in the whole book's history
— ORB 0-for-5 against a 29% historical hit-rate — reached p=0.18, well
short of the 0.05 significance bar. This is a real, honest Stage 1
result, not an absence of one: at the same-day sample sizes this book has
actually generated so far (max 19 trades for one engine in one day) and
against historical hit-rates that are themselves already low (22%–40%
across these four engines), no day has been a statistical outlier by this
test's own definition. The mechanism runs correctly; the book has not yet
produced a day extreme enough for it to have anything to say.

**Not demonstrated, and cannot be from a single session:** Gate D5's own
"calibration log covers a stated minimum window with a stated accuracy
bar met" needs real elapsed sessions accumulating more (engine, day)
pairs than 22 — the same "real elapsed market time no amount of building
tonight can substitute for" every prior Track D gate has been deferred
for. Whether a same-day flag, once it DOES fire, actually predicts the
rest of that session — the true design question — needs migration 106's
`detected_at` merged from `feat/intraday-event-core` first; recorded as a
scope limitation in §3, not answered by approximation.

### 6 — AN UNRELATED FINDING SURFACED IN PASSING

`tools.health`'s `capital` check: configured `TOTAL_CAPITAL` is Rs 30,000
but the live account holds Rs 24,983 (Rs 4,994 cash + Rs 19,989 invested)
— short by Rs 5,017 (17%). New entries are being sized against headroom
that does not exist; the check's own words are "expect orders to be
rejected at the broker." Flagged here per this file's own standing rule
("Flag anything found along the way that costs money, even when
unasked") — not investigated or corrected this session, out of D5's own
scope.

### 7 — NOT DONE / WHAT THIS DOES NOT CHANGE

`intraday_same_day_fit_weight` ships `0.0`. `same_day_fit_multiplier()`
is called from exactly one place in this stage —
`tools/same_day_calibration.py` — never from `engine.py`'s entry path,
never from `score()`. No live sizing decision is affected by anything in
this entry. Written on `feat/intraday-regression-shadow`, since merged
into `feat/intraday-evolution`.

## 2026-08-24 — F-65 (consolidation, no new mechanism) — Track D, Stages
D2 through D5 merged into one branch, `feat/intraday-evolution`, off
`main`. Per the operator's own explicit instruction: complete every D
stage independently first, then consolidate and holistically check before
arming anything — this is that consolidation.

### 1 — WHY, AND WHY NOW RATHER THAN AFTER D6

D2 (`feat/intraday-live-universe`), D3 (`feat/intraday-event-core`), D4
(`feat/intraday-depth-gate`) and D5 (`feat/intraday-regression-shadow`)
were each branched fresh off the SAME `main` commit and built with zero
awareness of one another, by construction — `git merge-base` confirmed
identical for all four before this session started. That was the right
call while each stage was still being built (isolates risk, keeps each
branch's own `tools.verify` meaningful), but a fifth branch for D6 would
have compounded the eventual reconciliation rather than deferred it —
raised with the operator directly rather than assumed; the answer was to
consolidate now and build D6 on top of the result.

### 2 — WHAT ACTUALLY CONFLICTED, MEASURED BEFORE MERGING ANYTHING

`git diff --name-only main <branch>` for all four, compared, before the
first merge: `intraday/engine.py` and `intraday/run.py` touched by
D2+D3+D4; `intraday/price_feed.py` by D3+D4; `analysis/overlays.py` and
`intraday/strategies/base.py` by D2+D4; `allocation/scoring.py` by D2+D5;
`tools/verify.py`, `docs/FINDINGS.md`, `docs/TRADEOS_ROADMAP.md` by all
four. Merged in stage order (D2 → D3 → D4 → D5), one `git merge` per
stage, `tools.verify` run after each before proceeding to the next.

**Real conflicts, resolved by hand:** `overlays.py` (D2's
`liquidity_capped_budget()` and D4's new section-4 `depth_ok()` — both
kept, D2's left where it was as a companion to `liquidity_ok()`, D4's
kept as its own numbered section); `price_feed.py`'s `__init__` (D3's
`_dirty`/`_dirty_baseline` fields and D4's `_depth`/`_depth_symbols`
fields — both kept, concatenated); `verify.py`'s `MODULES` list, every
merge (mechanical — list concatenation).

**Auto-merged clean by git, verified correct by inspection, not
assumed:** `engine.py`, `run.py`, `strategies/base.py`, `allocation/
scoring.py`. Checked directly rather than trusted: `SymbolContext` carries
both `universe_population` (D2) and `depth` (D4); the entry-sizing
pipeline reads `budget = liquidity_capped_budget(...)` (D2) → qty →
`BLOCKED_LIQUIDITY` (existing) → `BLOCKED_DEPTH` (D4) in the correct,
non-contradictory order; `cycle()` calls `apply_live_quotes()` →
`merge_live_bars()` → `apply_live_depth()` (D4) in sequence, with D3's
`event_core.check()` wired separately into `run.py`'s own slow timer;
`Prior.hit_rate` (D5) sits alongside the established/admitted prior-split
machinery (D2) without either touching the other's fields.

### 3 — THE F-NUMBER COLLISION, RESOLVED

Every one of D2's F-54–61, D3's F-54, D4's F-54 and D5's F-54 correctly
anticipated this in their own text ("renumbering happens once branches
actually merge"). Resolved in merge order: D2's F-54–61 left untouched
(already internally sequential); D3's F-54 → F-62; D4's F-54 → F-63; D5's
F-54 → F-64. Only each entry's header line and its own collision-
explaining preamble paragraph were edited to record the renumbering and
point back to F-62's own explanation; no other content in any entry was
touched, per this ledger's own append-only rule — this is documented
renumbering of a known, pre-announced collision, not a rewrite of a past
finding.

### 4 — VERIFIED, PRECISELY

`tools.verify`: **930/930 across 90 modules** — exactly `main`'s own
763/76 plus D2's own +89/+6, D3's +26/+3, D4's +27/+3, D5's +25/+2,
confirming every stage's own offline test suite survived the merge
byte-for-byte, not just that the file compiled. `tools.health`: 24/24,
including `kite` (live session), `feed` (I/O-free tick handler
confirmed), `broker`/`stops`/`qty_fields` (swing book unaffected, per
this track's own "Rule for this whole track" requiring exactly these
swing-specific checks on any change touching the shared position loop).
`tools.simulate`: ran clean end-to-end through the FULL combined
pipeline — Population A/B/C universe (D2), the event-core module present
though gated off (D3), the depth gate wired into the live sizing path
though gated off (D4) — against a LIVE session with three real intraday
positions open (NMDC, HINDALCO, NATIONALUM) and four real swing
positions, "nothing was written."

**One unrelated finding, surfaced again by `health.capital`:** the same
17% capital shortfall first flagged in F-54 (this branch's own) persists
— configured `TOTAL_CAPITAL` Rs 30,000 vs. an account actually holding
Rs 24,942 today. Still out of this session's scope; still real; still
unfixed.

### 5 — NOT DONE / WHAT THIS DOES NOT CHANGE

Every Track D switch remains at its shipped-off default —
`intraday_live_requalify_enabled`, `intraday_live_requalify_
unreferenced_enabled`, `intraday_event_core_enabled`, `intraday_depth_
mode_enabled`, `overlay_depth_enabled`, `intraday_same_day_fit_weight`.
This is source consolidation, not arming. Gate D2 through Gate D5 each
remain individually unproven — none of them can be cleared by a merge;
every one of them needs real elapsed market sessions with its own switch
armed, which is still the operator's own decision, still deferred to a
single pass across all of them together rather than stage-by-stage. Old
branches (`feat/intraday-live-universe`, `feat/intraday-event-core`,
`feat/intraday-depth-gate`, `feat/intraday-regression-shadow`) left in
place, not deleted — kept as a safety net until this branch is verified
in a live session, per the operator's own preference. On
`feat/intraday-evolution`, not merged into `main`.

## 2026-08-24 — F-66 (new mechanism built + a real status-literal
collision caught before shipping) — Stage D6: automatic discovery-to-
shadow-strategy pipeline. Branch `feat/intraday-evolution` (built directly
on the just-consolidated Track D branch, not a fifth separate branch).

**Ran:** `tools.verify` — 963 checks, 93 modules, green (F-65's own 930
plus 33 new). `tools.health` clean, 24/24. Live: `tools.discover_engines
--days 30` run for real, producing a fresh structured Pass B candidate
(proposal #186, `gap up > 1%`); `tools.approve_candidate --id 186 --dry`
confirmed the full read→parse→approve chain against that real row without
error. Three more real refusal paths confirmed live against existing
production rows — see §5.

### 1 — SCOPE, AGREED WITH THE OPERATOR BEFORE BUILDING

The roadmap's own D6 text under-specifies two real forks: which of the 11
raw discovered features to template (only 3 map onto SymbolContext fields
that already exist; the other 8 need new plumbing), and whether a
templated candidate may ever go SHORT (the raw feature name never
specifies direction — see gap_down_bounce.py's own docstring warning
about exactly this). Asked directly rather than assumed. Answers: all 11
features (the operator chose the larger scope over my own gap-only
recommendation), LONG only.

### 2 — WHAT "TEMPLATED" ACTUALLY MEANS, AND WHY

`tools/discover_engines.py`'s Pass B (`moved_but_unseen`) measures, from
`stock_data_daily` (one row per symbol-day), whether a prior-day condition
preceded a big move no engine caught — a POPULATION worth testing, not an
intraday entry rule on its own. GDB's own docstring is explicit that
turning such a finding into a real engine needed genuine judgment (which
mechanism to reuse, where to place the stop) a template cannot invent. So
every templated candidate reuses ONE fixed, generic shape instead of
inventing new mechanism per candidate: the discovered daily-bar condition
as a FILTER (`intraday/candidate_template.py::FEATURE_TRANSLATORS`, the
same 11 keys as `discover_engines.py`'s own `feats` dict, hoisted to
module level there specifically so a test can pin the two in sync), a
single-bar VWAP reclaim — GDB's own reused mechanism, reused a second time
— as the TRIGGER (most of the 11 conditions describe YESTERDAY and do not
change intraday; without a live trigger they would fire on every
evaluation of every qualifying name all session), a structural stop via
`risk_from_structure()` under the swing low made below VWAP (GDB's own
stop mechanic, reused verbatim), and a fixed R-multiple target
(deliberately simpler than any hand-tuned engine's target logic — the
point of shadow here is testing whether auto-generated code runs and
detects sensibly, a lower bar than testing whether it trades well).
`"gap down > 1%"` is explicitly excluded — GDB already covers exactly
that population; templating a duplicate tests nothing new.

### 3 — A REAL GAP CLOSED FIRST: STRUCTURED EVIDENCE

`brain_proposals.evidence` is JSONB but had only ever been written a bare
string. A template reading a free-text sentence to recover which feature
fired and how strong the evidence was would be exactly the "reading a
validated split back out of prose" this codebase has already refused once
(`allocation/allocator.py::refresh_priority_criteria()`'s own docstring).
Fixed at the source: `discover_engines.py::_propose()` now accepts a dict
as well as a string, and Pass B passes one — `feature_name` (the literal
`feats` key), `rate`, `lift`, `n_tot`, `n_miss`, `closed_strong_rate`,
`avg_move_pct`, `move_threshold_pct`, plus `summary` (the same sentence,
for the review display). `weekly_review.py`'s own display line updated to
show `.summary` rather than a raw dict repr. Pass A keeps writing a plain
string, unchanged — this is additive, not a format migration.

### 4 — A SECOND REAL GAP: NO APPROVAL MECHANISM EXISTED FOR THIS
PROPOSAL TYPE, AND THE FIRST FIX FOR IT WAS WRONG

`tools/proposal_backtest.py`'s own docstring already establishes
`ENGINE_CANDIDATE` proposals can never reach `status=VALIDATED` through
the existing automated out-of-sample re-check ("proposes a pattern with
NO engine built yet — nothing exists to replay"). The FIRST version of
`tools/approve_candidate.py` therefore invented a new status,
`SHADOW_APPROVED`, reasoning that nothing existing applied. WRONG, caught
before it shipped: querying real `brain_proposals` rows directly showed
`status='APPROVED'` is ALREADY the real, precedented human-approval
mechanism for this exact proposal type — proposals #188 and #190 became
GDB this way — and `swing/brain/backtester_and_change_manager.py`'s own
`REVIEW_ONLY` set (which `ENGINE_CANDIDATE` already belongs to) is
specifically what makes `APPROVED` safe here: `apply_proposal()`
acknowledges and returns for any `REVIEW_ONLY` type, never reaching the
`system_config`/`strategy_config` write path. Inventing a second,
parallel "approved" status would have fragmented one real human decision
into two fields nothing kept in sync — the identical near-homophone risk
`docs/TERMINOLOGY.md` exists to prevent, just for a `status` column
instead of a regime string. Fixed by deleting the invented status
entirely: `tools/approve_candidate.py` now calls the EXISTING
`approve_proposal()` directly (never reimplement a decision, import it),
adding exactly one thing that function does not have on its own — it
refuses to approve a row `candidate_template.py` cannot actually use, so
"approved" and "will produce shadow activity" never diverge silently.

### 5 — VERIFIED, PRECISELY, INCLUDING FOUR LIVE REFUSAL PATHS

963/963 offline checks (27 new pin the two gap-closures above: one
asserting `FEATURE_TRANSLATORS`' 11 keys stay a byte-identical mirror of
`discover_engines.feats`, others asserting `from_proposal()` refuses
every malformed shape — Pass A subjects, the GDB-covered feature,
unrecognised feature names, missing ids, and (the one this session's own
first-draft mistake would have needed) old-shape string evidence — never
guessing at any of them). Live, against real production `brain_proposals`
rows, not synthetic fixtures: `#186` (`UNSEEN/gap up > 1%`, still
old-shape evidence at the time) correctly refused as un-templatable;
`#191` (`UNSEEN/gap down > 1%`) correctly refused as GDB-covered; `#190`
correctly refused as already `APPROVED`. Then `tools.discover_engines
--days 30` run for real, producing a genuine fresh Pass B finding
(`gap up > 1%`, 1.6x lift, 16 missed, 88% closed strong, avg +5.39%) that
upserted structured evidence onto proposal `#186` — confirmed by direct
query. `tools.approve_candidate --id 186 --dry` then read that SAME real
row and correctly built a valid candidate end to end
(`feature=gap up > 1%, avg_move_pct=5.39, lift=1.6x`). Proposal `#186`
was deliberately left `PENDING`, not actually approved — per this
project's own "the human decides at every gate" rule, approving a
specific candidate for shadow testing is the operator's call to make, not
mine, even though its only consequence is a shadow-only detection log.

### 6 — NOT DONE / WHAT THIS DOES NOT CHANGE

`intraday_candidate_shadow_enabled` ships `false`. No candidate is
currently `APPROVED` (proposal #186 is ready and PENDING — the operator
can run `python -m tools.approve_candidate --id 186` to approve it, which
alone still produces nothing until the switch above is also armed).
`candidate_shadow.check()` writes ONLY to `intraday_candidate_shadow`
(migration 109) — never `intraday_setups`, `paper_broker`, or the
allocator. Gate D6 ("a stated minimum of shadow detections logged")
needs a real armed session with at least one approved candidate, the
same evidence-accumulation deferral every prior Track D gate has carried.
On `feat/intraday-evolution`, not merged into `main`.

## 2026-08-24 — F-67 (change, swing-only) — HINDCOPPER was bought twice
within 5 minutes on its 24-Aug re-entry. Root cause: `_maybe_enter_swing`
set `self._pending_fills[sym]` then immediately called `self.load_state()`,
which rebuilds that whole dict from a fresh DB read — a read that had not
yet caught up with the PENDING_FILL row this exact call just wrote silently
erased the guard one line after it was set. Only `order_manager`'s own
5-minute duplicate-order cooldown was then standing between the daemon and
a second real order; 15 retries were blocked by it over 5 minutes, then the
window lapsed and a second BUY landed for real.

**Ran:** `tools.verify`: 964/964 excluding one pre-existing, unrelated
failure — see §3.

### 1 — WHAT SURFACED IT

Operator's own observation: "System sold hindcopper then again bought some
quantity — was it the right behavior?" The ORIGINAL position (14→24 Aug)
had in fact closed correctly — `BOOK_PARTIAL` at 1.07R (+7.4%), then
`EXIT_GIVEBACK` at the F-43 tiered 30% threshold (+8.3%) — validating that
work live. The re-entry that followed the same day was itself a legitimate,
independent decision on a fresh signal. The order log underneath it was not
legitimate: two real BUY orders 5 minutes apart for the same name.

### 2 — ROOT CAUSE

`intraday/engine.py::_maybe_enter_swing`, immediately after placing a live
entry order:

```python
self._pending_fills[sym] = str(res.order_id)
self.load_state()
```

`load_state()`'s own docstring is explicit that it rebuilds
`self._pending_fills` from scratch from `open_positions` — necessary in
general, so a daemon restart between placing an order and confirming its
fill does not forget the attempt. But called in THIS order, immediately
after a manual set, any read that has not yet caught up with the row just
written wipes the guard the line above just set. With the guard gone,
`_maybe_enter_swing`'s own `if sym in self._pending_fills: return` check
(the one thing meant to stop a repeat decision at the DECISION layer) never
engaged — the daemon kept deciding "buy HINDCOPPER" every 15s cycle, and
only `order_manager.place()`'s separate 5-minute duplicate-order cooldown
(an order-PLACEMENT-layer safety net, never meant to be the only one) stood
between that and a second fill. `reconcile_with_broker` later corrected the
position to the true holding (4 shares, MATCHED) — no double-size position
resulted — but `partial_booked_qty`/`original_qty` were left corrupted by
the collision, and the near-miss is the same shape as the PPLPHARMA
double-sell landmine this project has already paid for once.

### 3 — FIX AND VERIFIED

Reordered: `self.load_state()` now runs first, and `self._pending_fills[sym]
= str(res.order_id)` is set AFTER — immune to the rebuild by construction,
since no read can erase an assignment that happens after it.

New `tests/test_pending_fill_race.py`, 3 checks. `_maybe_enter_swing` is not
independently callable in a unit test (it needs a live Kite session, order
placement, the allocator — the same reason this class of engine method has
no direct test elsewhere in this project), so the guard's ORDERING PRINCIPLE
is exercised directly: a stub DB returning no rows stands in for a read that
has not caught up, and the test proves the fixed order (`load_state()` then
set) survives it while the pre-fix order (set then `load_state()`) loses the
guard — the second test is a sanity check on the fixture, demonstrating the
failure mode is real rather than assumed. A third test confirms the rebuild
still correctly forgets a GENUINELY resolved pending fill once it reaches
ACTIVE — this fix changes only the ordering relative to a fresh placement,
not the rebuild's own general correctness.

`tools.verify`: 964/964. One PRE-EXISTING, unrelated failure was found
during this session's run — "price feed depth (FULL-mode) plumbing (Stage
D4)" fails inside the full suite but passes standalone (`--module
apply_live_depth`, 4/4), and still fails with this session's new test file
removed entirely — confirmed neither caused by nor related to this fix. It
is intraday-side Stage D4 work from a concurrent session, explicitly out of
scope for this session per the operator's instruction not to touch intraday
code; named here rather than silently worked around, for whoever picks up
Track D next.

## 2026-08-24 — F-68 (change, Track E Stage E2) — Quantify, closing Gate
E2 (`docs/TRADEOS_ROADMAP.md`). Real numbers for all three E2 questions.
Branch `feat/swing-evolution`, off `main`.

**Ran:** `tools.verify`: 978/979 (the one failure is F-67's already-named,
pre-existing, unrelated Stage D4 test-ordering issue — confirmed still
absent-of-relation, same module, same isolated-pass behaviour). New
`tools/swing_feature_edge_study.py` run live, full history, both dry-run
and for real: 31 findings written to `brain_proposals` as `PENDING`.

### 1 — Q1: do features separate winners from losers per engine?

Yes, clearly, on real sample sizes. New `tools/swing_feature_edge_study.py`
— independent of `tools/feature_edge_study.py` and everything under
`intraday/` (see the module's own docstring for why it does not import
either, even though the statistical method is the same proven shape) —
mined every resolved (TARGET/STOP) `signal_output_daily` row, grouped by
`swing_family()` (read-only import from `allocation/scoring.py`, the same
dependency F-46 already established as safe), for tercile/bucket-vs-rest
splits across 12 numeric and 8 categorical fields already sitting on that
table. Live run: **CONTINUATION n=427, 20 findings; MOM n=118, 11
findings; RVS n=12, below the 40-sample floor, correctly skipped.**

Two findings land directly on this session's own trades. CONTINUATION's
`sector` split: **metals & mining wins 31% (5T/11S) against 75% for every
other sector (44pp gap, mean −3.14% vs +2.61%)** — HINDCOPPER's own
family and sector. MOM's `sector_rank_at_entry` split: **rank ≤4 wins
100% (39/39, mean +6.43%) against rank ≥10's 82% (32/39)** — HAL entered
at sector rank **2**, inside the strong band on this specific split, so
this particular finding does not explain HAL's loss; the CTL/HAL story is
the zone-chase (Stage E5), a different mechanism this stage was not built
to find. Both are `HYPOTHESES`, not new rules — see §3.

### 2 — Q2: does the lesson engine's own grade predict anything?

**Unanswerable from existing data, and the reason is itself the
finding.** `ai/post_trade_analysis.py::grade_trade_entry()` computes an
A–F grade per closed trade — confirmed by reading the code, not assumed
— but the grade is used ONLY to word the generated lesson's prose
(`generate_rule_based_lesson()`) and is never written to any column
anywhere. Checked directly: the `lessons` table (19 columns) has no grade
field of any kind. There is nothing to correlate against outcome because
the grade was never captured past the moment it was computed. This
changes Stage E6's own plan: reconnecting the lesson engine needs the
grade PERSISTED first (a new column, written whenever `post_trade_
analysis` runs — additive, measurement-only, the same shape as F-43's
`exit_signal` and F-46's `runner_evidence` fixes) before any correlation
study is possible at all.

### 3 — Q3: is there enough resolved history to fit anything safely?

Per-engine: yes for CONTINUATION and MOM (427 and 118), no yet for RVS
(12) or any other isolated family — confirmed by the same 40-sample floor
`tools/swing_feature_edge_study.py` already enforces before attempting a
split. **Per-regime: no — every single resolved row in `signal_output_
daily` carries `regime = 'NEUTRAL'`.** The book has not yet traded through
a resolved RISK_ON/RISK_OFF/TRENDING/RECOVERING session, so Stage E3's
regime-aware exit ladder can be built and reasoned about, but cannot yet
be validated against this account's own resolved outcomes in a different
regime — it will need to accumulate evidence over time, same as any other
finding in this track.

### 4 — WHAT WAS WRITTEN, AND WHAT WAS NOT

`brain_proposals`, `proposal_type='FEATURE_FILTER'`, `source=
'swing_feature_edge_study'`, `target_key` prefixed `SWING/` so it can
never collide with an intraday engine's own key in the shared table —
31 rows, all `PENDING`. Nothing was changed in any live decision path;
`entry_ranking.score_plan()` does not read any of this yet, per Stage E2's
own scope. One known, bounded interaction with the shared queue, named in
the tool's own docstring rather than hidden: `tools/feature_edge_study.py
::validate_pending()` reads every `PENDING FEATURE_FILTER` row regardless
of prefix and will attempt to re-validate these against `intraday_setups`
— confirmed by reading that function's filter, not assumed — where they
will simply find no matching engine and stay `PENDING`, harmless. Not
fixed, because fixing it means editing an intraday file, which this track
does not do; Stage E6 builds this tool's own independent validator instead
of relying on the intraday one ever reaching a `SWING/` row correctly.

### 5 — GATE E2

Closed. Real numbers for all three questions, one of them ("is there
regime diversity to validate against") a genuine "not yet" rather than a
guess, and one (the lesson-engine grade) surfacing a concrete prerequisite
for Stage E6 that was not visible before this session. Stage E3 can begin.

## 2026-08-24 — F-69 (change + correction, Track E, F-68 follow-up) —
persisted the lesson engine's A–F grade for real (migration 093 + a
backfill of every existing closed SWING trade), which is what made
Q2 answerable at all — and it came back unconfirmed, not confirmed:
grade C (the dominant grade, n=76) sits near breakeven while grade D
(the *worse* grade, n=10) outperforms it. Separately, and directly
because the operator asked for it: re-ran both of F-68's two strongest
findings (CONTINUATION's metals & mining sector split, MOM's
sector_rank_at_entry cliff) against the last two weeks alone before
agreeing to wire either into a live decision — neither replicated.
Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 990/991, the one failure being F-67's
already-named, pre-existing, unrelated Stage D4 issue, confirmed
unrelated a third time (same module, same isolated-pass behaviour,
still failing with nothing of this session's touching it). Migration
093 applied live. `ai.post_trade_analysis --backfill-grades` run dry
then for real: 88/88 SWING closed trades written.

### 1 — GRADE PERSISTENCE (the operator's "#2")

`grade_trade_entry()` — real, existing code, confirmed by reading it —
computes an A–F grade for every analysed closed trade and always has.
Traced exactly where that value went: popped into a run-level
`grade_dist` tally for one log line, used to word the generated
lesson's prose, then discarded. The `lessons` table's 19 columns hold
no grade of any kind. F-68's own Q2 ("does the grade predict outcome")
was unanswerable for exactly this reason.

Fixed on two paths, both SWING-only — a deliberate narrowing of scope
that `main()`'s own pre-existing loop does not itself apply (it already
processes both frameworks' closed trades without discriminating; an
intraday row simply has no `signal_log` match and grades a
context-free default "C" — pre-existing behaviour, unchanged here):

- **Forward.** `main()`'s loop now writes `entry_grade` back to
  `closed_positions` immediately after computing it, gated on
  `framework == 'SWING'` and the row carrying an `id`.
- **Backfill.** New `backfill_entry_grades()` — deliberately narrower
  than a full re-analysis: no AI call, no `lessons` insert, no dedup
  bookkeeping, just `grade_trade_entry()` (already pure) over
  `load_signal_context()`'s result, for every closed SWING trade
  missing the column. Run live: 88 trades, distribution
  `{A:1, B:1, C:76, D:10, F:0}`. GABRIEL — bought despite the
  pipeline's own `AVOID_ENTRY` three sessions running, this project's
  own landmine — graded **D**, exactly the shape the grade exists to
  catch.

**The correlation, now that it can be run:** grade C (n=76, the
population's dominant grade) shows avg R **−0.01**, effectively
breakeven; grade D (n=10) shows avg R **+0.30** — better, not worse.
A and B are single trades each, too thin to read at all. This is the
opposite of what the grade claims to measure, and — same discipline as
§2 below — n=10 is nowhere near enough to trust either direction. **Not
wired into anything.** The honest state: the question moved from
"cannot be asked" to "asked, and the answer is not yet, on either
side" — real progress, not a null result to be embarrassed about. Stage
E6 revisits this once more graded closes accumulate.

### 2 — THE RECENCY CHECK (the operator's "#1", and why it did not ship)

Operator's own instruction, verbatim: before wiring either of F-68's
two strongest findings into a live decision, confirm they hold using
the last 1–2 weeks of data alone, not just the full historical sample
— named concern: not retiring or derating anything on data that might
not represent the system as it actually behaves today.

Checked first whether that concern applied literally: `signal_output_
daily`'s resolved population spans 24-Jul to 20-Aug-2026 only, entirely
within TradeOS's own operating history (first commit 04-Mar-2026) — no
true pre-TradeOS legacy data is mixed into this specific study, unlike
the closed-trade "55 trades, most from a legacy manually-sized book"
population `position_lifecycle.py`'s own stall-rule comments already
name. So the concrete worry did not apply here — but the underlying
instinct did, and the check itself proved it right:

Re-ran `tools.swing_feature_edge_study --since 2026-08-10
--min-engine-sample 10` (96 of 483 total rows, the last ~2 weeks):

- **Metals & mining does not reappear.** The 44pp-gap finding from the
  full sample (n=16 metals & mining rows total) does not have enough
  recent rows to even form its own category at the last-2-weeks
  min-segment floor. **Healthcare** shows up instead as the recent
  underperformer (31% vs 83%, n=16) — a *different* sector than the
  one F-68 flagged.
- **MOM's `sector_rank_at_entry` cliff produces zero findings** on the
  41-row recent MOM sample — the split that looked cleanest on the
  full 118-row sample (rank ≤4: 100%, n=39) does not survive being
  narrowed to fresh data, at least not yet at this sample size.

**Verdict: neither finding is wired into `entry_ranking.score_plan()`
or anywhere else live.** Both stay `PENDING` in `brain_proposals`
exactly as F-68 left them — not `REJECTED` (that would claim the
opposite direction was shown, which is not what a too-thin recent
sample demonstrates) — annotated here as the record of why they were
not promoted, for whoever revisits them once more recent data has
accumulated or Stage E6's own out-of-sample validator is built.

### 3 — WHAT THIS ADDS TO STAGE E6'S OWN PLAN

Both halves of this session land in the same place: a finding's
significance on the full historical sample is not sufficient on its
own before it touches a live decision. Stage E6's validator was already
scoped to re-check PENDING findings against data created strictly after
they were found (the F-50 pattern, adopted for swing); this session adds
a second, narrower check worth building into the same harness — does
the pattern also hold in a short, RECENT window, not just "some data
after the original finding." A finding can validate on the F-50 sense
(later data agrees) while still resting mostly on stale evidence if the
recent slice alone is too thin to say anything — exactly what happened
to both findings here.

## 2026-08-24 — F-70 (change, Track E Stage E3) — closed the "knows but
doesn't act" gaps: a standing health check for the F-67 shape, real
execution of `ai_recommended_action=TIGHTEN_SL` (shadow-first), and a
regime-aware exit-ladder multiplier (shadow-first, per E2's own finding
that no regime diversity exists yet to validate it against). Building
the health check surfaced a SECOND, previously unknown double-buy —
**HAL, 21-Aug, three days before HINDCOPPER, same shape, both orders
filled this time.** Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 998/999, F-67's already-named pre-existing Stage
D4 issue, confirmed unrelated a fourth time. New health check run live
against real data — caught both real incidents on the first try, no
synthetic fixture needed. `tools.simulate`: HINDCOPPER's real shadow log
fired correctly — `sl 503.85 -> 539.00`, the actual `ai_action_reason`
text from its own 20-Aug review, decision unchanged (`HOLD`) with the
switch off, exactly as designed.

### 1 — A SECOND F-67 INCIDENT, FOUND BY THE CHECK BUILT TO CATCH IT

New `tools.health::check_pending_fill_duplicates()` — distinct from the
existing `check_pending_fills` (which asks whether a row is stuck
unresolved; this asks whether an order actually doubled up) — flags two
`PLACED` BUY events for the same symbol with no `SELL` between them
inside 10 minutes. Run against real data: it caught HINDCOPPER
(24-Aug, the incident F-67 fixed) — and **HAL, 21-Aug, three days
earlier**, previously unknown.

HAL's order log: `BUY 1 @ 5021.80` (08:13:57 UTC), 10 blocked retries
over the next 5 minutes (`order_manager`'s own duplicate cooldown, same
mechanism that limited HINDCOPPER's damage), then `BUY 1 @ 5020.70`
(08:19:08) — the moment the cooldown lapsed. Unlike HINDCOPPER, where
reconcile corrected the excess down to the true broker holding, **HAL's
own small 1-share order size meant both fills went through cleanly** —
confirmed live: `current_qty=actual_qty=kite_qty=2, reconcile_status=
MATCHED`. This was never a 2-share sizing decision. `risk_pct_per_trade`
(1.5%) against HAL's own risk-per-share sizes to 1 share; the account is
carrying 2, roughly double the intended per-trade risk, and `invested_
value` (₹10,022) is ~44% of the whole portfolio in one name — a real
concentration nothing decided on purpose. **Left exactly as found — this
is the operator's own position and call to make (trim back to 1 share,
or hold), not something this session closes unilaterally.**

### 2 — `ai_recommended_action=TIGHTEN_SL` NOW EXECUTES (shadow-first)

New rung 2c in `evaluate_exit()`. `ai_recommended_action` — confirmed by
grep before this session touched it — was written by `ai/ai_decision_
engine.py` and read by exactly one place, `alerts/send_alerts.py`, to
display it. HINDCOPPER's own 20-Aug review recommended `TIGHTEN_SL` over
a live geopolitical risk in metals & mining; nothing executed it.

One-directional only, the same asymmetry every rung in this ladder
already respects: moves the stop a configurable fraction (`swing_ai_
tighten_fraction`, default 0.5 — halfway) from its current level toward
the live price, never loosens it, checked regardless of profit level
(unlike the 1R-gated deterioration check). Deliberately scoped to
`TIGHTEN_SL` only — `HOLD`/`TRIM`/`EXIT`/`NO_ACTION` stay informational.
Automating `TRIM`/`EXIT` would mean acting on AI judgement the same way
`ai_tier`/`ai_conviction` already were, and that channel was correctly
demoted to zero ranking weight on 04-Aug once evidence showed it was not
predictive — `TIGHTEN_SL` is safe to automate on a different basis
entirely (it can only ever protect capital, never spend it), not because
the AI's judgement earned more trust.

Ships OFF (`swing_ai_tighten_enabled`, migration 094). While off, the
condition is still evaluated every cycle and shadow-logged — confirmed
live via `tools.simulate` against HINDCOPPER's real position.

### 3 — REGIME-AWARE EXIT LADDER (shadow-first, no calibration behind it)

`evaluate_exit()` has only ever read `regime_at_entry`, frozen the day a
position opened. New: fetch the market's CURRENT regime once per daemon
start (`market_regime`, same cadence F-46's stall calibration already
uses) and apply a single multiplier to both the giveback allowance and
the (possibly family-calibrated) stall clock — one number, because the
same direction is "tighter" for both a percent allowance and a day
count. RISK OFF tightens (`swing_regime_mult_risk_off`, default 0.7);
RISK ON/TRENDING loosens slightly (`swing_regime_mult_risk_on`, default
1.2) — more patience in a genuinely strong tape is a legitimate
professional response, not permissiveness for its own sake. NEUTRAL/
RECOVERING apply no adjustment.

Ships OFF (`swing_regime_aware_exits_enabled`, migration 094) —
deliberately with NO calibration behind the 0.7/1.2 defaults at all,
unlike F-43/F-46's ladder work. E2's own quantify pass (F-68) found
every resolved swing outcome on record reads `regime='NEUTRAL'`; there
is no historical diversity to validate this against yet, and arming it
before that exists would be exactly the "not enough data" mistake this
session already avoided once this session (§2, the metals & mining /
MOM sector-rank findings). The mechanism is real and tested; the
specific multiplier values are a placeholder until real regime diversity
accumulates.

### 4 — VERIFIED

`tests/test_stage_e3_ai_tighten_and_regime.py`, 8 checks, demonstrated
failing first: `git stash` on the two touched source files, 3 of 8
failed (the two armed-behavior tests, plus the loosen-in-RISK-ON case),
restored and all 8 passed. `check_pending_fill_duplicates` was proven
against real incidents rather than a synthetic fixture — a stronger
demonstration than the usual git-stash pattern, since the failure mode
it catches already happened twice and both are still inside its 7-day
window.

### 5 — NOT DONE

**HAL's doubled position is not trimmed.** Named in §1, left for the
operator. **The regime multiplier's specific values (0.7/1.2) are
unvalidated** — Stage E6 or a future E3 revisit should re-derive them
once real regime diversity exists in resolved outcomes, the same way
F-46's stall-clock numbers were derived from real data rather than
guessed. Same daemon-deploy gap as every finding this session: `tools.
simulate` proves the code correct; `intraday/run.py`'s running process
does not pick any of this up until restarted.

## 2026-08-24 — F-71 (change, Track E Stage E4) — structural break checked
from day one (not gated at 1R), and live sector-decay tightening using
`sector_strength`'s already-computed state rather than a frozen entry-day
snapshot. Building the live-data verification caught a real gap of its
own: `tools/simulate.py` had been building an incomplete policy dict
since F-46 — none of `stall_days_by_family`, `_current_regime`, or
`_sector_state` ever reached it, only `load_exit_policy()`'s pure config
read. Factored into one shared function so the two can no longer drift.
Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1006/1007, F-67's already-named pre-existing
Stage D4 issue, confirmed unrelated a seventh time. `tools.simulate`
against the real book, post-fix: **two of three open positions —
HINDCOPPER (metals & mining) and AARTIIND (chemicals) — are currently
sitting in sectors reading WEAKENING today**, both correctly shadow-
logged.

### 1 — STRUCTURAL BREAK FROM DAY ONE

`deterioration_check()` (`control/exit_rules.py`) only ever ran at
`gain_r >= exit_deterioration_min_r` (1.0) — a trade going wrong from day
one got zero structural evidence read until fastfail (day 4) or the
calibrated stall clock (day 6–10), pure price-and-time until then.

Parametrized rather than duplicated: `deterioration_check()` now accepts
optional `floor`/`action`/`reason`, defaulting to the exact existing
behaviour (`1.0`/`EXIT_DETERIORATION`/`THESIS_BROKEN`) for every caller
that passes none of them. New rung 2b2 in `evaluate_exit()` calls it a
second time with `floor=-inf`, labelled `EXIT_INVALIDATED`/
`THESIS_BROKEN_EARLY` — so a trade that gave back a real gain and one
whose thesis broke before it ever worked are told apart in the record,
not folded into one bucket. Cannot manufacture an exit from an ordinary
losing position by itself: `tq.verdict` must independently read `BROKEN`
on the same structural evidence (structure, momentum, RS, sector) the
profitable case already trusts, and stop-breach is checked first in the
ladder, so this can only ever act on a position still above its own
stop. Ships OFF (`swing_early_invalidation_enabled`, migration 095),
shadow-logged.

### 2 — LIVE SECTOR-DECAY TIGHTENING

`sector_rank_at_entry` is read in exactly one place in the whole ladder
— the 3R runner decision — using the frozen entry-day snapshot.
`sector_strength` already computes a live `sector_state` every session
(`LEADING`/`IMPROVING`/`WEAKENING`/`NORMAL`/`TOO_SMALL`) and nothing
during ordinary holding ever read it.

New multiplier, composing with Stage E3's regime multiplier
(`applied_mult = applied_regime_mult * applied_sector_mult`) rather than
overriding it — both apply if both are armed. Deliberately **tighten-only**,
unlike the regime multiplier: a sector still `LEADING` is already why the
trade was taken and does not additionally earn extra patience — stacking
two independent "be more patient" signals is how a ladder drifts toward
never cutting anything. Ships OFF (`swing_sector_decay_enabled`,
migration 095), shadow-logged.

### 3 — A REAL GAP FOUND WHILE VERIFYING: `tools/simulate.py` WAS INCOMPLETE

Building the live-data check for §2 surfaced this directly: the AI-tighten
shadow line appeared correctly against HINDCOPPER's real position (reads
a plain column, no supplementary context needed), but the sector-decay
line did not — `tools/simulate.py::simulate_swing()` built its policy
dict from `load_exit_policy()` alone and never called the daemon's own
inline fetch of `stall_days_by_family`/`_current_regime`/`_sector_state`.
This means **F-46's own stall-clock calibration, verified against
`tools.simulate` in that session's writeup, was never actually exercised
by that verification** — the tool was silently falling back to the flat
default the whole time, and the "live proof" cited then was real for the
daemon but not for what `tools.simulate` itself was showing.

Fixed by factoring the three fetches into one new function,
`control/position_lifecycle.py::load_live_exit_context()`, called by
BOTH the daemon (`intraday/engine.py`, replacing its own inline copy) and
`tools/simulate.py` — the "decision reuse is load-bearing" rule applied
one level up: not a second decision, but a second, incomplete COPY of
the context one decision function needs. Confirmed post-fix: `tools.
simulate` now shows the sector-decay shadow line for both real positions.
A secondary shadow-log severity issue was caught in the same pass — the
regime/sector shadow lines were logged at `.debug()` (silent by default)
while the AI-tighten line used `.info()`; raised both to `.info()`, since
a shadow log nobody's default log level shows defeats its own purpose.

### 4 — VERIFIED

`tests/test_stage_e4_early_invalidation_and_sector_decay.py`, 8 checks,
demonstrated failing first: `git stash` on the three touched source
files, 3 of 8 failed (the two armed-behaviour tests plus the
multiplier-composition test), restored and all 8 passed. `deterioration_
check()`'s own parametrization is covered by a dedicated test proving
every existing call shape (no new args) is byte-identical to before.

### 5 — NOT DONE

Day-by-day participation/delivery decay and sector rotation as a
book-wide (not per-position) signal — the remaining two items in Stage
E4's own plan — were not built this session; scoped clearly enough to
pick up next without re-deriving anything. Same daemon-deploy gap as
every finding this session.

## 2026-08-24 — F-72 (change, Track E Stage E4 — closes it) — the two
remaining pieces from F-71 §5: participation/delivery decay per position,
and a book-wide sector-concentration health check. Branch
`feat/swing-evolution`.

**Ran:** `tools.verify`: 1012/1012 offline logic checks (5 new), the same
pre-existing unrelated Stage D4 issue confirmed still isolated. `tools.
simulate` and `tools.health --quick` against the real book.

### 1 — PARTICIPATION/DELIVERY DECAY

The swing-cadence version of the intraday F-45 volume-decay idea:
`vol_ratio` on the entry-day session vs. the latest available session,
per held SWING symbol, from `stock_data_daily`. A stall clock counts
SESSIONS, not conviction — a name stalling on thinning volume is a
different animal from one stalling on thick, contested volume, and the
fixed clock cannot tell them apart.

Fourth fetch added to `control/position_lifecycle.py::
load_live_exit_context()` (now shared by the daemon and `tools/
simulate.py`, per F-71 §3's fix): for every ACTIVE SWING position, entry-
day `vol_ratio` vs. the latest, as a `{symbol: ratio}` dict, fails safe to
`{}` on any error — same resilience the other three fetches already have.
New multiplier in `evaluate_exit()`, composing into the existing chain
(`applied_mult = applied_regime_mult * applied_sector_mult *
applied_participation_mult`) rather than replacing it. Tighten-only, same
reasoning as sector-decay: participation that has NOT decayed is already
priced into why the trade was taken. Gated by a 2-session floor — never
flags on entry day or day one, when a fresh position's own volume has had
no chance to establish a baseline yet. Ships OFF (`swing_participation_
decay_enabled`, migration 110), shadow-logged via `logger.info()` from
the start — the F-71 §3 log-level lesson applied up front this time
rather than found and fixed after the fact.

Live check against the real book (24-Aug-2026): no shadow line fired for
any of the three open positions, and the reason is itself informative
rather than a gap — HINDCOPPER re-entered *today*, so `stock_data_daily`
has no row yet for its own entry day (the fetch correctly skips a symbol
with no usable entry-day baseline rather than guessing); AARTIIND's
volume has actually *increased* since entry (ratio 1.25, not decayed);
HAL's latest available session is still its entry day itself (ratio
1.0, nothing to compare against yet). Confirmed via direct SQL against
`stock_data_daily`, not inferred — the honest result is "no signal today
on this book," not a manufactured one. The mechanism itself is proven by
five synthetic tests below, not by today's book.

### 2 — BOOK-WIDE SECTOR-CONCENTRATION HEALTH CHECK

`evaluate_exit()`'s own sector-decay multiplier (F-71 §2) reads
`sector_strength` per position, against that position's own sector only —
it has no view of the BOOK. Three positions each individually tolerable
at x0.75 tightening can still mean the whole book is leaning into one
fading rotation at once, which no per-position check can see by
construction.

New `tools/health.py::check_sector_concentration_risk()`, registered as
`sector_risk`, mirroring `check_pending_fill_duplicates`'s shape: group
currently-ACTIVE SWING `open_positions` by sector, cross-reference each
sector's live `sector_state`/`rank_delta_5d`, flag when >=50% of the book
sits in sectors reading WEAKENING today. Read-only diagnostic — changes
nothing, gates nothing.

Confirmed live: **2 of 3 open SWING positions (67%) — HINDCOPPER (metals
& mining) and AARTIIND (chemicals) — read WEAKENING today**, correctly
flagged. Not something a per-position check could have surfaced as a
BOOK-level fact; each position's own shadow line exists (F-71 §2), but
neither says "this is now most of what you hold."

### 3 — INVESTIGATED IN THE SAME PASS: `pending_dup` ALSO FIRED — NOT A NEW INCIDENT

Running `tools.health` to prove §2 also surfaced `pending_dup` (F-70)
flagging HINDCOPPER again, timestamped 04:05–04:10 UTC (09:35–09:40 IST)
today — the same 15-blocked-retries-then-a-real-second-BUY shape as the
original F-67 incident. Traced before reporting rather than assumed:
the F-67 fix itself was committed at 15:03:34 IST *today* (`4ecfd0c`),
**after** this HINDCOPPER incident (09:35 IST) — this is the same
already-diagnosed, already-fixed incident from earlier in today's session,
still inside the check's 7-day lookback window, not a recurrence.
Confirmed no SWING BUY was placed for any symbol after the fix commit
today (`intraday_broker_log`, empty result) — though that is a weak
negative, since only ~27 minutes of market time remained after 15:03 IST
before the 15:30 close. The fix has not yet had a real live re-test
window; tomorrow's session is the first one that will actually exercise
it. Flagging this explicitly rather than letting a clean `tools.verify`
run imply more than it proved.

### 4 — VERIFIED

`tests/test_stage_e4_participation_decay.py`, 5 checks, demonstrated
failing first: `git stash` on the three touched source files
(`control/position_lifecycle.py`, `intraday/engine.py`, `tools/
health.py`), the one test that depends on the new tighten-only behaviour
("tightens when armed") failed as expected against the reverted source;
the other four passed even pre-change because they assert the off/no-op
paths, which the old code already satisfied — that asymmetry is expected,
not a weak test. Restored, all 5 passed. `check_sector_concentration_
risk()` has no offline unit test — it is DB-backed by construction, same
as its sibling `check_pending_fill_duplicates`, and is verified live via
`tools.health` instead, per this project's own rule that a test needing
the live book belongs there.

Migration 110 (not the sequential 096): the concurrent intraday-track
session had already claimed 096–109 on disk by the time this was
written. Numbered after the highest in use to avoid a second collision
in the same shared ledger the `093`/`094`/`095` numbers already collided
on once this session.

**Track E, Stage E4 is now fully built.** All four planned pieces —
early/structural invalidation, live sector-decay tightening,
participation/delivery decay, and the book-wide sector-concentration
check — ship OFF, shadow-logged, verified. Next: Stage E5 (entry-side
intelligence), on explicit go-ahead only.

## 2026-08-24 — F-73 (change, Track E Stage E4 refinement) — sector-decay
strength exemption, plus a full investigation of the HINDCOPPER
double-order into a definitive answer. Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1014/1015 (5 new checks; same pre-existing
unrelated Stage D4 issue). `tools.simulate` against the real book.

### 1 — OPERATOR'S POINT: DON'T PUNISH A REAL CANDIDATE FOR ITS GROUP

"We should not be blocking the real candidates having the potential to
move upwards e.g. with strong volumes and other data points." The F-71
sector-decay multiplier fired purely off `sector_state == WEAKENING` —
a GROUP-level read — with zero regard for whether the position's OWN
data contradicted it. A genuine leader outrunning a lagging sector
("buy the strongest stock in a weak group" — O'Neil/Minervini) would
get tightened anyway, for a reason that had nothing to do with the
stock itself.

`control/position_lifecycle.py::evaluate_exit()`: the sector-decay block
now checks the SAME `_participation_decay` ratio F-72 already computes.
When a WEAKENING-sector position's own `vol_ratio` is at or above
`swing_sector_decay_strength_exempt_floor` (1.0 — participation holding
or rising vs. entry day), the sector tighten is skipped entirely for
that position; the giveback/stall thresholds fall back to whatever the
regime and participation multipliers alone would produce. Deliberately
**asymmetric**: only the sector (group) signal defers to the
participation (stock) signal — the regime multiplier is untouched,
because a real risk-off regime is systemic and not something one
stock's own volume diversifies away from.

Deliberately requires POSITIVE evidence, not absence of it — a symbol
with no participation data at all (no entry-day `stock_data_daily` row
yet, same gap F-72 §1 already documented for a same-day re-entry) is
NOT exempted; "no data" and "measured strong" must not collapse to the
same answer, the same principle CLAUDE.md's own landmines already state
for a cold-start hurdle. No new switch — governed by the existing
`swing_sector_decay_enabled`; one new tunable,
`swing_sector_decay_strength_exempt_floor` (migration 111).

**Live proof, same book:** AARTIIND (chemicals, reading WEAKENING) now
shows `sector-decay EXEMPTED — ... own vol_ratio (1.25x entry-day) is at
or above the 1.00x strength floor` — its own volume has genuinely risen
since entry, and the refinement correctly stops treating it like a name
whose participation is fading. HINDCOPPER, re-entered today with no
entry-day baseline yet, correctly gets NO exemption and falls back to
the plain F-71 shadow — absence of data does not manufacture strength.

### 2 — HINDCOPPER: FULLY TRACED, NOT A HAL REPEAT

Operator's second point: confirm HINDCOPPER's double order was not
"wrong... like HAL." Traced with real broker-log and order-history data
rather than assumed:

- The two BUY orders (04:05:24 and 04:10:31 UTC, 24-Aug) are the SAME
  incident F-67 already root-caused and fixed this session — not a new
  one. F-67 §2 already states the outcome plainly: **`reconcile_with_
  broker` corrected the position to the true holding (4 shares, MATCHED)
  — no double-size position resulted.** The order-log collision was
  real and is exactly what the fix (commit `4ecfd0c`) closes; the
  POSITION SIZE was never wrong.
- This is the material difference from HAL: HAL's double position is
  REAL and stands at the operator's own explicit instruction ("let's
  retain HAL's double position"). HINDCOPPER's never existed as a real
  doubled holding — it self-corrected via reconcile before this session
  ever started investigating it.
- The fix commit (15:03:34 IST, 24-Aug) landed AFTER this incident
  (09:35 IST) chronologically, so it has not yet had a live re-test —
  only ~27 minutes of market time remained after the commit before the
  15:30 close. No SWING BUY was placed for any symbol after the fix
  landed today (checked directly against `intraday_broker_log`). Stated
  plainly as an untested-live fix, not a proven one, in F-72 §3 and
  again here — tomorrow's session is the first real test.

### 3 — VERIFIED

3 new checks in `tests/test_stage_e4_early_invalidation_and_sector_decay.
py` (now 11), demonstrated failing-first. The first attempt at the
"exempt when strong" test asserted `EXIT_STALL` — vacuously true against
BOTH the reverted and the new source, because a stall at session 8 with
mult=0.75 (old code, always applies) and mult=1.0 (new code, exempted,
clock never shortens) land on different session numbers, not the same
action at the same session — a "check that cannot fail" caught by its
own failing-first requirement before being trusted. Corrected to assert
session 8 must NOT stall when exempted (mult stays 1.0, clock stays at
its flat 10-day default); re-run against reverted source failed
correctly, restored and passed. `tools.verify`: 1014/1015.

## 2026-08-24 — F-74 (change, Track E Stage E5, piece 1 of 3) — the
entry-ranking call sites now rank on `decide()`'s truly live R:R, not the
evening pipeline's stale snapshot. Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1019/1020 (5 new checks; same pre-existing
unrelated Stage D4 issue). `tools.simulate` against the real book —
byte-identical output to before, confirming no regression.

### 1 — WHAT THE QUANTIFY PASS FOUND

Stage E5's own roadmap text names a "zone-drift penalty" scoped as
percentage distance above `entry_zone_high`. Quantified first, per this
project's own "quantify before build" pattern (Gate E2): joined
`closed_positions` to `signal_output_daily`/`signal_log` for the 19 most
recent SWING exits with usable zone data (69 of 88 closed rows have no
`signal_id` at all — a real, separate attribution gap, noted but out of
scope here) and bucketed by raw `%` drift above zone. **The naive metric
does not cleanly separate outcomes on this sample** — it was the wrong
proxy.

Checked HAL's own real numbers instead (`docs/TRADEOS_ROADMAP.md`'s own
motivating example): filled at 5010.20 against a zone whose low drifted
4779 → 4808 across three prior daily signal snapshots — only ~1-2% raw
price drift. But `planned_stop` (4740.22) and `planned_target` (5325.17)
stayed FIXED across all three snapshots while the zone caught up to
price, so the reward:risk the plan was originally sized on collapsed:
`rr_at_zone_low` ranged 7.63–14.09 across those three snapshots; `rr` at
the actual fill was 1.17. **R:R retention, not raw price distance, is
what "chase" actually costs.** Re-bucketed the same 16-position sample
by R:R-retained fraction: bottom half (worst retention) averaged
**−0.003R** with 3 of 4 total losses in this set; top half (best
retention) averaged **+0.227R** with 1 loss. Small sample (n=8/bucket),
not monotonic (PPLPHARMA was the single best winner despite moderate
retention), but directionally real and mechanistically exactly what
HAL's own numbers show live.

### 2 — THE REAL BUG THIS SURFACED: NOT A MISSING FEATURE, A DEAD ONE

`entry_ranking.py::score_plan()`'s own comment already claims the R:R
term reads "the live figure... a plan that has already run is a worse
trade than it was when written, and only implied_rr knows that." It
does not: `implied_rr` is written ONLY by the evening pipeline
(`final_snapshot.py`/`generate_signals.py`) and nothing refreshes it
before either place that ranks candidates for entry. `analysis.
trade_decision.decide()` already computes the real thing — `rr_live`,
reward:risk at the live price — and both call sites had it sitting in
scope (`d.rr_live`) and never used it: `intraday/engine.py::
_maybe_enter_swing` (the live daemon) and `tools/simulate.py::
simulate_swing_entries` (the read-only preview tool — which additionally
called `rank(plans)` BEFORE its own per-plan `decide()` loop even ran,
so it could not have used a live figure even if one had been threaded
through). This is not the roadmap's originally-scoped "new penalty" —
it is an EXISTING mechanism that was never actually doing what its own
comment already claimed, closer in shape to the tools.simulate gap
F-71 §3 already found once this session than to a new feature.

### 3 — FIX

New pure function `analysis/entry_ranking.py::live_ranking_input(p,
rr_live)` — overrides `implied_rr` with the live figure when present,
no-ops when `rr_live` is `None` (a plan can legitimately have no live
figure, e.g. a `CHASE_LIMIT` priced off the limit; the stale fallback
beats a fabricated zero). Factored into ONE shared function BEFORE two
independent inline copies could drift, not after — both `_maybe_enter_
swing` and `simulate_swing_entries` call it identically. `tools/
simulate.py` additionally reordered: `decide()` now runs per-plan before
`rank()`, not after, so its own `rr_live` values are available in time
to feed the ranking rather than only the post-rank BUY/WAIT filter.

### 4 — VERIFIED

5 new checks in `tests/test_stage_e5_live_rr_ranking.py`, demonstrated
failing first: `git stash` on all three touched source files (`analysis/
entry_ranking.py`, `intraday/engine.py`, `tools/simulate.py`), all 5
failed (3 on the missing import, 2 structural call-site checks) against
fully reverted source, restored and all 5 passed. Two are direct unit
tests of the new pure function and its effect through `score_plan()`
(HAL's own numbers: live rr=1.17 ranks materially below stale zone-low
rr=14.09); the other two are source-inspection checks confirming both
call sites actually use the shared function — the same class of
call-site gap `check_shorts()` already greps for, because `_maybe_enter_
swing` cannot be called directly in a unit test (needs a live Kite
session, same reason no other method in that class has one) and a
return-value test cannot see which function a call site used.
`tools.verify`: 1019/1020. `tools.simulate` live: byte-identical output
to pre-fix — this day's own stale `current_price` happened to already
be close to its `implied_rr`'s own reference point, so no visible swing
today; the mechanism itself is proven by the HAL anchor and the unit
tests, not overclaimed from a day that doesn't happen to show it.

### 5 — NOT DONE

Stage E5's remaining two pieces — the AI's own lessons as checkable
predicates, and weekly-structure confirmation pulled into the entry
gate — were not built this session.

## 2026-08-24 — F-75 (change, Track E Stage E5) — investigated pieces 2
and 3; found and fixed a serious, 100%-reproducible pre-existing bug in
piece 3's own underlying mechanism along the way. Branch
`feat/swing-evolution`.

**Ran:** `tools.verify`: 1024/1025 (5 new checks; same pre-existing
unrelated Stage D4 issue). `tools.simulate` against the real 3-position
book.

### 1 — PIECE 2 INVESTIGATED, NOT BUILT: THE EVIDENCE DOES NOT SUPPORT IT YET

The roadmap's own motivating anecdote — "HAL's 20-Aug note literally read
'avoiding chasing after a sharp rally' ... one day before the same AI
approved a trade that did exactly that" — does not survive a literal
check. That lesson's stated trigger was `RSI-W > 85`; HAL's real
`rsi_weekly` on its 21-Aug entry day was 66.2. No self-contradiction on
the lesson's own stated terms.

What IS real: `ai_max_chase_pct` (the AI's own per-candidate chase
ceiling, already wired into `decide()`) was 2.0 on 20-Aug and **NULL on
21-Aug, the actual entry day** — `ai_tier` also dropped `TIER_1` ->
`WATCH_CLOSELY` the same day. `decide()` silently treats a null cap as
no cap at all. But HAL's actual raw chase was only 0.94% — under even
the prior day's 2.0% cap — so carrying that value forward would not have
stopped this specific trade either; the damage was in R:R retention
(F-74), not raw chase distance, again.

The `lessons` table itself has a real track record (1369 rows, 526 with
`times_applied > 0` — larger than an earlier, recency-biased 15-row
sample suggested) but is applied ENTIRELY through LLM self-judgment
(fed as prompt context, self-reported back as `lessons_applied`) with no
deterministic enforcement — HAL's own entry day is a live example of
that self-application silently regressing with nothing else to catch it.
Hard-coding a refusal on R:R retention was considered and rejected: on
the 16-position quantify sample (F-74), the winners and losers sit close
enough together in retention-fraction space (AIIL 0.134 WIN vs.
TRAVELFOOD 0.057 LOSS) that any threshold tight enough to exclude
TRAVELFOOD also risks excluding real winners — the "check that cannot
PASS" failure mode this project explicitly guards against, on a sample
far too thin to set a hard line with confidence. Not built. A genuine
validated track-record study of `lessons.times_worked/times_applied`
belongs to Stage E6 ("the learning core"), not E5's entry-gate scope.

### 2 — PIECE 3: THE UNDERLYING MECHANISM WAS DEAD, FIXED FIRST

Piece 3 asked to pull `weekly_structure` into the entry gate, conditioned
on it being "a real signal, not decoration." Checking that turned up
something more serious: `control/exit_rules.py::assess_trend()` — the
EXISTING exit-side consumer of `weekly_structure`, read by
`deterioration_check()`, the 3R runner decision, and this session's own
Stage E4 early-invalidation rung — string-matched the field against
`HIGHER/UPTREND/BULLISH/HH` and `LOWER/DOWNTREND/BEARISH/LL`. Confirmed
live via SQL: `compute_msl.py` has never emitted any of those four
strings. Its real, and only, four values are `STRONG`/`CONSOLIDATING`/
`CAUTION`/`WEAK` — 1886/395/263/228 rows respectively. Verified in
Python directly against all four real values: zero matches, either
branch, always. Same shape as the documented "RISK ON" vs "RISK_ON"
collision (migration 048) and the unreachable STRONG regime bucket.

Worse than inert: `checks += 1` fired regardless of whether the value
matched, for every one of the ~2.7k rows carrying a non-empty
`weekly_structure` — the majority of all trend assessments. `score =
len(for_) / checks`, so this silently DEFLATED the score for most
positions: a check that could structurally never contribute to the
numerator was still inflating the denominator. A position with real
bullish evidence elsewhere (e.g. RSI in trend) plus a genuinely STRONG
weekly structure scored 0.5 instead of the clean 1.0 both signals
actually earned.

`compute_msl.py`'s own classification (lines 1153-1156) gave the correct
mapping directly, not guessed: `STRONG` = weekly higher-high AND
higher-low (score 90) -> for_. `WEAK` = neither (15) -> against.
`CAUTION` = higher-high WITHOUT the higher-low sequence (42) — a new
high with the underlying structure already broken, a real distribution
warning, not a reason for patience -> against. `CONSOLIDATING` =
higher-low only, not yet confirming (65) — genuinely ambiguous, kept
NEUTRAL and no longer incremented into `checks` at all, matching this
function's own stated philosophy ("missing inputs count as neither for
nor against") extended to an ambiguous value rather than reinterpreting
it as bullish. Quantify pass against real closed-position outcomes was
attempted first but the sample was too thin to validate directionally
(n=15 STRONG / 2 CONSOLIDATING / 1 WEAK, zero CAUTION rows) — the fix
instead rests on `compute_msl.py`'s own already-computed numeric
ordering, not a fresh backtest.

Applied as a direct correctness fix, not gated behind a new switch —
matching this session's own F-67/F-69 precedent for restoring already-
live logic to its intended behaviour, distinct from the Stage E3/E4
features that were genuinely NEW capability and shipped OFF by design.

**Live proof, real book:** HINDCOPPER's verdict actually flipped —
`INTACT (67%)` before this fix, `STRONG (78%)` after — it was being
under-credited. AARTIIND and HAL both rose in confidence (`78%` ->
`89%`) with unchanged verdicts. No position's score fell. Confirmed no
consumer distinguishes `STRONG` from `INTACT` individually (`should_run`
groups both), so nothing on today's book actually changes DECISION —
this session's fix improves accuracy for the borderline cases (FADING
vs. INTACT, `has_evidence` threshold crossings) it will matter for going
forward, not today's specific book.

Pulling the now-correctly-working signal INTO the entry gate — piece
3's original literal scope — was not done this session: the same thin-
sample problem that blocked piece 2 applies here too, and fixing the
prerequisite bug first, rather than building a new consumer on top of a
mechanism that was silently broken, was judged the higher-value use of
this session's remaining time.

### 3 — VERIFIED

5 new checks in `tests/test_stage_e5_weekly_structure_vocabulary.py`,
demonstrated failing first: `git stash` on `control/exit_rules.py`, all
5 failed against the reverted (buggy) source with the exact predicted
failure shapes (STRONG scoring 0.0 instead of 1.0, the dilution case
scoring 0.5 instead of 1.0), restored and all 5 passed. `tools.verify`:
1024/1025.

### 4 — STAGE E5 STATUS

Piece 1 (F-74) built and verified. Piece 2 investigated, not built —
evidence does not support it yet. Piece 3's prerequisite bug found and
fixed; the entry-gate extension itself not built, same reason as piece
2. All three conclusions are evidence-based "not yet," not oversights —
consistent with this project's own quantify-before-build discipline.

## 2026-08-24 — F-76 (change, Track E Stage E5, pieces 2-3 — shadow
build) — operator's own instruction: "go ahead and build it" (shadow
versions of pieces 2 and 3, off by default) — plus a real, pre-existing
`tools.simulate` gap found wiring the shadow logs in so they could
actually be observed. Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1032/1033 (8 new checks; same pre-existing
unrelated Stage D4 issue). `tools.simulate` against the real book.

### 1 — WHY SHADOW, NOT A HARD LINE — AND WHY BUILD IT ANYWAY

F-75 concluded pieces 2/3 should stay not-built because n=16 is too thin
to set a confident refusal threshold. Operator pushed back correctly:
that reasoning justifies staying OFF, not staying UNBUILT — this
session's own Stage E3/E4 precedent (participation-decay shipped OFF
with shadow logging despite showing zero live signal on its first day)
already established that a sound mechanism with thin evidence should
accumulate real data via a shadow log, not wait indefinitely for a
sample that will not grow on its own. Built on that basis.

### 2 — PIECE 2: R:R RETENTION AS A SHADOW REFUSAL

`analysis/entry_ranking.py::entry_refusals()` gained two new optional
params, `rr_live`/`rr_at_zone_low` (decide()'s own already-computed
numbers, passed in — the function stays pure, no I/O added). When both
are available and `rr_live / rr_at_zone_low` falls below `entry_rr_
retention_floor`, shadow-logs (armed: `entry_refuse_low_rr_retention`,
off) a refusal.

Floor chosen deliberately at 0.20, not the more natural-looking 0.15:
HAL's own real numbers at its actual entry-day zone snapshot (rr_live
1.17, rr_at_zone_low 7.63) retain exactly 0.1533 — a hair ABOVE 0.15,
which would have silently failed to flag this session's own repeatedly-
cited anchor case. Caught before committing the migration, not after.

### 3 — PIECE 3: BROKEN TREND AT ENTRY, REUSING THE NOW-FIXED FUNCTION

Rather than a second, narrower weekly-structure-only check,
`entry_refusals()` now calls `control.exit_rules.assess_trend()` — the
SAME function F-75 fixed — directly on the CANDIDATE. That function had
only ever been asked about an ALREADY-HELD position (deterioration_
check, the 3R runner decision, the Stage E4 early-invalidation rung);
nothing had ever asked it about a plan before taking it. A plan whose
own trend evidence already reads BROKEN with real evidence is now
shadow-logged (armed: `entry_refuse_broken_trend`, off) — "decision
reuse is load-bearing" applied to a brand-new consumer of an existing
function, not a parallel implementation.

### 4 — THE REAL GAP FOUND WIRING IT IN: `tools.simulate` NEVER CALLED `entry_refusals()`

To make either shadow log observable outside the live daemon, `tools/
simulate.py::simulate_swing_entries()` needed to call `entry_refusals()`
— and it never had, for ANY of its existing checks either, not just the
two new ones. Confirmed live: `entry_respect_filter_reason` is `true` in
`system_config` right now — the daemon has genuinely been refusing
plans on this basis in production — while `tools.simulate` (the tool
CLAUDE.md names as what to run "before changing anything") was silently
reporting a DIFFERENT result. Concretely, on 2026-08-21's real plan set:
SIEMENS ranked #1 (11.2) and showed as the top TAKE before this fix;
after wiring `entry_refusals()` in, SIEMENS correctly shows **REFUSED —
the evening pipeline refused this plan: insufficient_rr_for_reentry_
setup**, and the simulated take list changes from {SIEMENS, CHALET} to
{CHALET, HONASA}. Same shape as F-71 §3 (the incomplete exit-policy
dict) — a second, independent case of this exact tool silently drifting
from what the daemon actually does. Fixed by adding the call, mirroring
`intraday/engine.py::_maybe_enter_swing`'s own placement (after the
`d.action` check, before counting the candidate as "considered").

**Live proof, same run:** GLAND's own real numbers lit up the R:R-
retention shadow independently of the HAL anchor — `R:R has retained
only 10% of its zone-low value (2.02 vs 19.48)`. No broken-trend shadow
fired for any real candidate today — an honest "no signal on this day's
pool," not a gap; the mechanism is proven by the synthetic BROKEN-
evidence tests below.

### 5 — VERIFIED

8 new checks in `tests/test_stage_e5_entry_shadow_checks.py`,
demonstrated failing first: `git stash` on all three touched source
files (`analysis/entry_ranking.py`, `intraday/engine.py`, `tools/
simulate.py`), 5 of 8 failed against the reverted source (4 on the
missing `rr_live` kwarg, 1 on the missing broken-trend refusal) — the
other 3 correctly passed either way because they test the off/absence
paths, the same asymmetry established as expected practice earlier this
session. Restored, all 8 passed. Migration 112: both switches off,
`entry_rr_retention_floor` shipped at 0.20 with the HAL near-miss
documented directly in the migration's own comment.

### 6 — STAGE E5 STATUS

All three pieces now have real code behind them: piece 1 (F-74) live-
armed by the ranking fix itself (no switch — see F-74), pieces 2-3
(F-76) shadow-logging real candidates, off by default, pending the
quantify pass their own accumulated data will eventually support.
Stage E5 is complete for this pass. Next: Stage E6, the learning core.

## 2026-08-24 — F-77 (change, Track E Stage E6, scoped subset) — the
recency validator (F-68/F-69's own explicit request) and a living
engine lifecycle review — the two of Stage E6's five pieces not blocked
by this session's own prior findings. Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1044/1045 (12 new checks; same pre-existing
unrelated Stage D4 issue). Both mechanisms run live against the real
book — the validator against F-68's 31 real PENDING findings, the
lifecycle review against all 9 real swing strategies.

### 0 — WHY THIS SUBSET, NOT ALL FIVE OF E6's PIECES

Operator asked "why can't we build all pieces of E6?" Checked each
against evidence this session already produced, not against a general
sense of caution:

- **Lesson grade -> score_plan()** — currently BACKWARDS, not merely
  unready. F-69's own correlation check: grade C (n=76, the dominant
  grade) averages −0.01R; grade D (n=10, the WORSE grade) averages
  +0.30R. Wiring this in now means coding a scoring term that rewards
  the grade the data says is worse.
- **Anticipatory model (probability-of-target)** — mathematically
  undefined, not merely risky. Every single resolved row in `signal_
  output_daily` carries `regime='NEUTRAL'` (F-68 Q3). A model fit on
  this and asked to score a candidate the next time the market is
  actually RISK_OFF has zero examples to extrapolate from for that
  regime — it would still emit a confident-looking probability, and
  that confidence would be fiction.
- **Per-engine feature tuning / swing discovery engine** — the raw
  material (F-68's 31 findings) does not hold up yet; F-69 re-checked
  the two strongest against the last two weeks and neither reproduced.

**Living engine lifecycle** was the one piece with no explicit blocker —
grouped with the others too conservatively in the first pass, corrected
after the operator's own question. Built both it and the recency
validator this session; the other three stay flagged with the reasons
above, not silently dropped.

### 1 — THE RECENCY VALIDATOR

`tools/swing_feature_edge_study.py::validate_pending_swing()` — promised
in this module's own docstring since F-68 rather than relying on `tools/
feature_edge_study.py::validate_pending()` ever correctly reaching a
`SWING/`-prefixed row (it cannot, by construction — confirmed in that
module's own filter). Independent reimplementation of the intraday
tool's F-50 METHOD (three outcomes — VALIDATED / REJECTED / stays
PENDING, "no opinion" never collapsed into "measured bad" — new pure
`_validation_outcome()`, mirroring the intraday one's own semantics
without importing it), plus a genuinely new second check F-69 explicitly
asked to be built into the same harness: does the pattern ALSO hold in a
short, RECENT window alone, not just "some data created after the
finding" (which can still be dominated by stale rows if the elapsed gap
is small). A finding is VALIDATED only when BOTH windows independently
confirm the same direction; a real disagreement on EITHER window
REJECTS it; insufficient data on either (with no actual disagreement)
leaves the row exactly where it was. Handles both numeric AND
categorical findings — F-69's own manual check covered one of each
(`sector_rank_at_entry`, `sector`), and the intraday reference only
handles categorical.

**Live proof:** ran against F-68's real 31 PENDING findings —
`--validate --dry-run` returns 0/0/31 (all skipped), which is the
CORRECT day-one answer, not a gap: every finding was created TODAY, so
the "strictly after creation" window has no data yet by construction (no
tomorrow exists). Checked the recent-window half independently, direct
inspection: the SAME two findings F-69 manually found do not survive a
recent check (`SWING/MOM/sector_rank_at_entry`, `SWING/CONTINUATION/
sector/metals & mining`) show **zero fresh match** on the automated
recent-window re-run too — the automated validator independently
reproduces F-69's own by-hand conclusion exactly. Three other findings
(`rsi_daily`, `delivery_pct`, `atr_pct`) DO reproduce on the recent
window alone, in the same direction originally claimed — real,
independent corroborating evidence the manual check never looked at.

### 2 — LIVING ENGINE LIFECYCLE

`tools/weekly_review.py::review_swing_engine_lifecycle()` — all 9 swing
strategies have sat at `lifecycle=ACTIVE` in `strategy_config` since
25-Jul (MOM/RVS re-touched 07-Aug only) with nothing re-asking whether
current evidence still supports it. Mirrors `review_engines()`'s
intraday shape (measure on resolved outcomes, PROMOTE/SHADOW/RETIRE/
keep/hold, `_propose`/`_supersede` into `brain_proposals`, never apply)
but keyed on the RAW `strategy` column — `strategy_config` holds one
lifecycle row per raw strategy (CTL/SEC/TPO/SBS/RSB/IAD/VBD/MOM/RVS),
not `swing_family()`'s pooled grouping, which only exists for the
feature-edge study's own sample-size needs. Sample floor reused from
`swing_feature_edge_study.MIN_ENGINE_SAMPLE` (40) rather than a second,
arbitrary number for the same underlying question. No rolling time
window — same reasoning the feature-edge study's own header already
gives for having no floor date: swing's absolute trade volume is small
enough that a 30-day window (`review_engines()`'s own intraday default)
would shrink RVS/TPO's already-thin samples further, making them
permanently unmeasurable rather than just currently thin.

Caught by this function's OWN test before it shipped: a healthy-CTL-
shaped fixture (78% hit, +1.91% avg, already ACTIVE) generated a
nonsensical `ACTIVE -> PROMOTE` proposal — PROMOTE is a real state
transition (SHADOW -> ACTIVE) and an already-ACTIVE strategy reading
healthy has nowhere higher to go. Fixed: PROMOTE now requires `cur !=
"ACTIVE"`; an already-ACTIVE healthy strategy correctly reads `keep`.

**Live proof, real book, 24-Aug-2026:** only CTL (n=292), MOM (n=78) and
SEC (n=53) clear the 40-sample floor — all three read healthy (77-82%
hit, +2.6% to +3.5% avg) and correctly `keep` ACTIVE. RVS (n=10, avg
**−0.97%**) and TPO (n=35, avg only +0.36%) are the two names with real
cause for concern and both correctly `hold` — too thin to act on with
confidence, the same "no opinion must not read as measured bad"
discipline the intraday reviewer already applies. Three strategy_config
rows with zero resolved history (ACC/EAP/PEAD) surfaced and correctly
held without crashing — handles an unexpected/new strategy gracefully.
Zero proposals written today; the mechanism is now standing, wired into
`tools.weekly_review`'s own `main()`, and will start proposing real
changes once RVS/TPO accumulate enough resolved trades to judge.

### 3 — VERIFIED

12 new checks across two files, demonstrated failing first: `git stash`
on `tools/swing_feature_edge_study.py` and `tools/weekly_review.py`
together, both test modules failed completely against reverted source
(7/7 and 5/5 — every test ImportErrors on a function that does not yet
exist), restored and all 12 passed. `tools.verify`: 1044/1045.

## 2026-08-24 — F-78 (change, Track E Stage E7, detection only) —
position scaling: quantified, then built the full detection/sizing
decision, deliberately stopping short of execution. Branch
`feat/swing-evolution`.

**Ran:** `tools.verify`: 1052/1053 (8 new checks; same pre-existing
unrelated Stage D4 issue). `tools.simulate` against the real 3-position
book — runs cleanly, correctly shows no signal (none of the three are
past the runner line yet).

### 1 — WHY THIS STAGE GOT A SEPARATE CONFIRMATION

E7 is explicitly the only stage in the whole track that ADDS capital
risk rather than sharpening a decision already being made, and its own
roadmap text says it benefits from E6's validated-finding mechanism
existing first — which is only 2/5 built (F-77). Confirmed with the
operator before starting, given SWING is LIVE, rather than reading
"then move forward" as extending that far by default.

### 2 — QUANTIFY FIRST

Of 17 recent closed SWING trades with usable `max_favorable_excursion`
data, only 2 (both PPLPHARMA, different entries) ever crossed the 1.0R
runner line at their peak (1.87R and 1.34R). Every other trade topped
out below 1.0R. **Scale-in opportunities are rare on this book** —
consistent with how rarely trades reach the 3R hard target the existing
RUN decision already governs (F-43's own "5% of trades that reach 3R").
This is not a reason not to build the mechanism; it is the reason to
build it now, shadow-first, so real evidence accumulates before the
rare day it actually matters, rather than designing execution logic
from scratch under time pressure the first time it fires live.

### 3 — WHAT WAS BUILT: THE FOUR RAILS

New `control/position_lifecycle.py::evaluate_scale_in()`, mirroring
`evaluate_exit()`'s shape but answering a different question — deliberately
NOT folded into the exit ladder, because "is this position still okay to
hold" and "should NEW risk be added to it" are different questions and
conflating them is how a ladder drifts. Four rails, each the roadmap's
own explicit condition:

1. `gain_r >= giveback_runner_min_r` (1.0R) — the same line F-43's
   tiered giveback guard already uses to mean the original risk should
   already be secured (partial banked, stop at/above breakeven).
2. `assess_trend()` verdict `STRONG` with real evidence — STRICTER than
   `target_decision()`'s own `should_run` (STRONG-or-INTACT): new risk
   deserves more conviction than continuing to hold an existing runner.
3. Capped at one add (`pos.get("scaled_in")`) — the roadmap's own
   explicit limit.
4. Sized through `analysis.portfolio_constraints.check_new_entry()` —
   the SAME function and `risk_pct_per_trade` budget any fresh entry
   uses, priced off the position's CURRENT stop (`active_sl`), never its
   unrealized profit — the guard against pyramiding on paper gains. An
   add competes for the same slot/sector/risk-budget caps any new
   candidate would; `open_positions` is passed with the position itself
   still in it, unfiltered.

### 4 — WHY EXECUTION STOPS HERE, DELIBERATELY

`evaluate_scale_in()` returns a decision; it never places an order,
never writes to `open_positions`, has no config switch to arm. The
roadmap's own text requires "the combined position's risk is measured
from the add forward, not blended with the original entry's now-stale
number" — a genuinely unresolved accounting question (does `entry_price`
become a weighted average, or does the add's own economics govern the
R-multiple going forward while the original tranche's already-secured
gain stays untouched?) that this session did not answer. Shipping
execution before it is answered risks corrupting the exact R-multiple/
giveback math this whole track has spent five stages getting right.
Shadow-logged unconditionally instead — no switch, because a switch that
arms nothing (execution does not exist yet) is its own kind of footgun,
an operator arming it and getting silence.

Wired into both consumers, matching every other Stage E mechanism this
session: `intraday/engine.py::_shadow_scale_in()` (swing-only branch,
called once per SWING position per cycle, right after `evaluate_exit()`/
`_track_trend_quality()`) and `tools/simulate.py::simulate_swing()`
(reusing the SAME `tq` that loop already computes, no duplicate call).

### 5 — VERIFIED

8 new checks in `tests/test_stage_e7_scale_in.py`, demonstrated failing
first: `git stash` on all three touched source files, all 8 ImportError
against reverted source, restored and all 8 passed. Sizing tested
against the REAL `check_new_entry()` (not mocked) — a hand-calculated
qualifying case, and a real refusal case (book at its position-count
cap) correctly propagates. `tools.verify`: 1052/1053. `tools.simulate`
live: all three real positions process without error; none currently
past the 1.0R line, so no shadow line fires today — the correct, honest
result given §2's own base rate, not a gap.

### 6 — NOT DONE

Order execution (placing the actual add, updating `open_positions`
quantity/`invested_value`, marking `scaled_in`) and the R-multiple/
entry-price accounting question §4 names — both explicitly out of scope
this session, pending that design question being answered on its own,
not rushed to unblock a stage.

## 2026-08-24 — F-79 (change, Track E — arming pass) — every Stage
E3-E5 shadow switch armed live, on the operator's own explicit
instruction ("arm all the shadow Es to live mode"), given full
awareness of the evidence split — plus a real, previously-latent test-
isolation bug the arming itself surfaced. Branch `feat/swing-evolution`.

**Ran:** `tools.verify`: 1052/1053 immediately after arming (4 NEW
failures beyond the known Stage D4 one — see §2), 1052/1053 again after
the isolation fix (only the known D4 failure). `tools.simulate` and
`tools.health --quick` against the real 3-position book, both live.

### 1 — WHAT WAS ARMED, AND THE EVIDENCE BEHIND EACH

Flagged to the operator before executing, given SWING is LIVE and
several of these switches were built literal minutes earlier with zero
real-world observation — not a refusal, a surfaced fact, per this
project's own "flag anything found along the way that costs money"
rule. Confirmed to proceed with all seven regardless. Migration 113:

**Seasoned** (fired repeatedly against real open positions this
session): `swing_ai_tighten_enabled`, `swing_regime_aware_exits_
enabled`, `swing_sector_decay_enabled`.

**Zero or near-zero real firings** (built this session; thresholds
explicitly documented in their own migration comments as "starting
point, not calibrated"): `swing_early_invalidation_enabled`,
`swing_participation_decay_enabled` (never fired live all session),
`entry_refuse_broken_trend` (never fired live), `entry_refuse_low_rr_
retention` (fired once, on GLAND).

### 2 — THE REAL BUG THE ARMING ITSELF SURFACED

`tools.verify` immediately went from 1052/1053 to 1048/1053 the moment
the migration landed — 4 NEW failures, all in exactly the Stage E3/E4
test modules whose switches had just flipped. Traced before assuming
the arming broke something: `tests.cfg_ctx({})` correctly sandboxes
`config._sys_config` (sets it to an empty dict, `get_system_config()`
sees it is not `None` and never refetches) — but four specific tests
across three files called `evaluate_exit()` with NO `cfg_ctx` wrapper
at all, meaning `cfg_bool("swing_..._enabled", False)` fell through to
whatever `config._sys_config` already held in the live process — the
REAL database value, not the code's own default. These tests were
silently coupled to the live system_config staying off the whole time
they existed, invisible until the live value actually changed:

- `test_stage_e3_ai_tighten_and_regime.py::test_ai_tighten_shadow_only_
  by_default_does_not_change_the_action`
- `test_stage_e3_ai_tighten_and_regime.py::test_regime_multiplier_is_a_
  noop_by_default`
- `test_stage_e4_early_invalidation_and_sector_decay.py::test_early_
  invalidation_shadow_only_by_default`
- `test_stage_e4_participation_decay.py::test_participation_decay_
  shadow_only_by_default`

A FIFTH, `test_stage_e4_early_invalidation_and_sector_decay.py::test_
sector_decay_shadow_only_by_default`, had the identical gap but did NOT
fail — session 7 checked against a 10-day default × 0.75 armed
multiplier rounds to 8 (Python's round-half-to-even), one session past
where the check happens to look. It was passing for the wrong reason —
reading the live value and getting lucky on the exact session number
chosen, not genuine isolation. Fixed anyway, on the same principle: a
check that passes by coincidence is the same defect wearing a different
hat as one that fails for the wrong reason.

All five fixed by wrapping the `evaluate_exit()` call in `with cfg_ctx
({}):`, matching the pattern every OTHER test in these files (and
everywhere else in this session's work) already used. This restores
the tests' original protective intent — proving the mechanism's CODE-
LEVEL default is a safe HOLD, independent of whatever the live account
happens to be running — rather than the accidental, fragile assertion
they had actually been making ("the live database currently says
off"). `tools.verify`: back to 1052/1053, only the known D4 issue.

### 3 — LIVE PROOF, POST-ARM

`tools.simulate` against the real 3-position book: **HINDCOPPER's
action changed from `HOLD` to an executed `TRAIL_SL`**, tightening its
stop on the AI's own live geopolitical-risk flag — the exact mechanism
F-70 built and shadow-logged, now genuinely acting rather than logging.
AARTIIND correctly stays `HOLD`, its own sector-decay tighten
EXEMPTED (F-73's own refinement) since its volume is up 2.43x since
entry — group-level sector weakness still does not override
demonstrated stock-level strength, now for real, not just in shadow.
HAL unaffected (below the 1.0R runner line, no scale-in eligible
either). `tools.health --quick`: every check clean except `pending_dup`
— confirmed the SAME already-traced F-67 historical incident (predates
today's fix commit, inside its 7-day lookback), not a new issue from
arming.

### 4 — WHAT THIS MEANS FOR THE THIN-EVIDENCE SWITCHES

Four of the seven now armed had little or no real-world confirmation
before this migration. That is the operator's own explicit, informed
call, not a default this project assumes going forward — the same
one-time-exception framing `docs/TRADEOS_ROADMAP.md`'s own non-
negotiables section already uses for F-43/F-46. Watching these four in
particular over the coming sessions — do they fire sensibly, does
`entry_rr_retention_floor`'s 0.20 starting point need recalibrating
once real refusals accumulate — is the natural next check, not
something this session can itself provide more evidence for today.

---

## 2026-08-25 — F-80 (bug fix, real gap closed) — the recency validator's
"automatic trigger" from the F-79 follow-up commit was itself dead on
arrival: EVERY step in the Sunday brain chain past the Telegram digest
had been silently no-op-ing for 9 days on a `cd backend` double-path
bug, `continue-on-error: true` reporting green the whole time.

**Ran:** `mcp__github__actions_list`/`get_job_logs` against the real
repo's most recent `TradeOS Brain Sunday Chain` run (id 32807877090,
25-Aug-2026 04:09 UTC, head_sha `a6e734a` — the exact commit that had
just wired the validator in). `python3 -c "import yaml; yaml.safe_load(...)"`
on the fixed file. Local reproduction: `cd backend && python -m
tools.swing_feature_edge_study --validate` from the repo root (the
broken shape) vs from `backend/` directly (the fixed shape). SQL via
the Supabase MCP against the real `Tradeos` project
(`dbjfwpamxudnolfalpfm`): `brain_proposals` PENDING/VALIDATED/REJECTED
counts for `target_key LIKE 'SWING/%' AND proposal_type='FEATURE_FILTER'`.

### 1 — WHAT WAS CLAIMED, AND WHY IT WAS WRONG

The operator's own screenshot (attached to this session's request) drew
on a PRIOR session's claim that the swing engine lifecycle review "already
runs... I added the lifecycle review directly inside that same function —
so it will run automatically this coming Sunday without anyone doing
anything." That claim was checked against the code (`tools/weekly_review.py`
does call `review_swing_engine_lifecycle()` from its own `main()` — true)
but never checked against whether the SCHEDULING MECHANISM carrying
`weekly_review` to production actually executes it. It does not, and has
not since 16-Aug.

### 2 — THE BUG, AND HOW IT WAS FOUND

The F-79 follow-up commit (`a6e734a`, wiring `swing_feature_edge_study
--validate` into `.github/workflows/brain_sunday_chain.yml`) was reviewed
for correctness before this session touched anything else — same env
vars, `continue-on-error: true`, positioned right after `weekly_review`,
exactly matching the file's own established pattern. It looked right.
Checking whether it had actually EXECUTED (this project's own "verify,
never assert" rule — a green step is not evidence, per the very finding
this file exists to record) surfaced the real defect: the workflow's own
`defaults: run: working-directory: backend` (added in commit `ee52dd4`,
16-Aug-2026) already runs every step from `backend/`. Eight steps across
five separate commits since then — `weekly_review`, the new recency
validator, `discover_engines`, `feature_edge_study`,
`intraday.outcomes --backfill`, `control_room --propose`,
`ingest_nifty_total_market`, `ingest_ipo_listings` — ALL additionally
prefixed their own `run:` command with `cd backend &&`, trying to enter
`backend/backend`, which does not exist.

Real log from the run at `a6e734a`, the step this session's own prior
commit had just added:

```
2026-08-25T04:10:14.8450Z /home/runner/work/_temp/....sh: line 1: cd: backend: No such file or directory
2026-08-25T04:10:14.8511Z ##[error]Process completed with exit code 1.
```

Every one of the 8 affected steps shows the identical shape — started
and "completed" within the same second, `conclusion: success` at the
step level ONLY because `continue-on-error: true` converts an actual
shell failure into a reported pass. The ONE step that does NOT have this
bug — "Send weekly Telegram digest" (`python -c "from swing.brain...`,
no `cd backend`) — is also the only one of the eight-plus that ever
visibly worked, which is exactly why a green Telegram message every
Sunday made the whole chain look healthy.

### 3 — SCOPE: THIS IS NOT JUST THE RECENCY VALIDATOR

`tools.weekly_review` — carrying F-77's `review_swing_engine_lifecycle()`
— has been silently not running automatically since 16-Aug-2026 (9 days,
commit `ee52dd4`). `discover_engines`, `feature_edge_study`,
`intraday.outcomes --backfill` and `control_room --propose` share the
same fate from the same commit. `ingest_nifty_total_market` (since
`33846ef`) and `ingest_ipo_listings` (since `2a49a13`) too. All of it
`continue-on-error: true`, all of it invisible unless someone opened a
run and read the raw step output rather than the green checkmark — the
exact "check that cannot fail is not a check" shape this file has
recorded five times before, now a sixth, in a place none of those five
looked (CI scheduling, not application logic).

### 4 — THE FIX

Stripped the redundant `cd backend && ` prefix from all 8 `run:` blocks
in `.github/workflows/brain_sunday_chain.yml`, matching the one step that
was already written correctly. Added a comment on the `defaults:` block
itself naming the failure mode, so the next added step does not repeat
it. Confirmed no other workflow file in `.github/workflows/` combines
`working-directory: backend` with a `cd backend &&` inside a `run:` block
(checked all of them). Local reproduction: `SUPABASE_URL=... python -m
tools.swing_feature_edge_study --validate` run from the repo root
reproduces `cd: backend: No such file or directory` when a `cd backend
&&` prefix is present; run from `backend/` directly (the fixed shape)
gets past that point and fails only on the dummy credentials this
sandbox has no real ones for — the correct next failure, proving the
fix removes the ONE thing that was wrong.

### 5 — RECENCY VALIDATOR ITSELF: STILL CORRECT, STILL UNPROVEN AUTOMATICALLY

`brain_proposals` for `SWING/%` FEATURE_FILTER rows: still 31 PENDING, 0
VALIDATED, 0 REJECTED — unchanged from F-77's 24-Aug baseline, which is
consistent with the fix (this session cannot make the NEXT scheduled or
dispatched run happen early) rather than a sign anything is still wrong.
`validate_pending_swing()`'s own logic is untouched by this fix — F-77's
tests (`tests/test_swing_recency_validator.py`) still pass, `tools.verify`
confirms it below.

### 6 — COULD NOT DETERMINE

Could not observe a genuinely successful automatic run of the fixed
workflow this session — the next scheduled firing depends on "TradeOS
Brain Scheduler" completing (event-driven, not on a clock this session
controls), and manually dispatching it (`workflow_dispatch` is enabled)
would also fire the real "Send weekly Telegram digest" step, sending an
unplanned message to the operator's phone and writing real
`brain_proposals` rows outside the normal schedule — not done without
asking first. This session has no Python-level Supabase credentials
(`.env` absent from this container) and could not run `tools.health`/
`tools.simulate` against the live account directly; all live evidence in
this entry came through the GitHub Actions API and the Supabase MCP
tools instead, and is called out as such rather than implied to have
come from a local run.

**Recommends:** next Sunday's automatic run (or an explicit operator-
approved manual dispatch) is the real confirmation — check that all 8
previously-broken steps show non-trivial duration and real log output,
not another instant green pass.

**Gate:** PASS — the wiring bug is fixed and verified as far as this
session's tools reach; full end-to-end confirmation is deferred to the
next real firing, named above rather than assumed.

---

## 2026-08-25 — F-81 (change, Track E Stage E7 continuation) — position
scale-in EXECUTION built: the accounting question F-78 left unresolved
is answered, migration 114 ships two switches (both OFF), and the full
submit → pending → confirm order-placement path now exists in
`intraday/engine.py`, mirroring `_maybe_enter_swing`/
`_resolve_pending_fills` exactly.

**Ran:** `tools.verify`: 1060/1063 (10 new checks in
`tests/test_stage_e7_scale_in_execution.py`, all pass; the 3 pre-existing
failures are in `stale token alerting`, confirmed unrelated and present
BEFORE any change this session made — `git stash` on this session's
edits reproduces the identical 3/11 failure with zero diff applied).
Demonstrated failing first: the same `git stash` also reproduces 6 of 10
new checks failing with `AttributeError`/`KeyError` against the
pre-session code (the other 4 pass on both sides — they assert nothing
happens, which was already true before execution existed). Migration 114
applied directly to the real `Tradeos` Supabase project
(`dbjfwpamxudnolfalpfm`) via the Supabase MCP tools (this session's
Python environment has no DB credentials — see F-80 §6) and verified
column-by-column and switch-by-switch afterward.

### 1 — WHAT WAS ALREADY BUILT, READ FIRST

Confirmed via `docs/TRADEOS_ROADMAP.md` Stage E7 and `docs/FINDINGS.md`
F-78 before writing anything: `control/position_lifecycle.py::
evaluate_scale_in()` already implements all four rails (runner line,
STRONG trend, capped at one add, sized through the REAL
`check_new_entry()`) and was already wired into both `tools.simulate`
and `intraday/engine.py::_shadow_scale_in()` as an unconditional shadow
log. F-78 stopped there on purpose, naming one specific unresolved
question: "does a combined position's risk get measured from the add
forward, or does entry_price become a weighted average" — and declined
to guess at it with SWING live. This session's job was to resolve that
question and build ONLY the execution layer on top of the untouched
detection function, not to redesign what was already working.

### 2 — THE ACCOUNTING QUESTION, RESOLVED FROM AN EXISTING PRECEDENT

Read `control/position_lifecycle.py::reconcile_with_broker()`'s own
QTY_INCREASED branch before proposing anything new: it has, since before
this track began, grown `current_qty`/`kite_qty`/`actual_qty`/
`invested_value` on any quantity increase WITHOUT ever writing
`entry_price`. That is the exact "add's own economics kept separate from
the original tranche" shape F-78 asked for — already live, already
proven, just never named or extended to a system-initiated add. Migration
114 follows it exactly: `entry_price`/`planned_stop`/`active_sl`/
`planned_target`/`target_price` — the five fields `evaluate_exit()`'s
gain_r/giveback-tier/trailing math reads — are in NO patch this session's
code writes, ever. There is still one `active_sl` per row (one broker-
side GTT per symbol); the add's own risk-per-share at decision time
(`ltp - active_sl`, already computed and previously discarded by
`evaluate_scale_in()`) is now persisted as `scaled_in_stop`, an audit/
learning column, never a second live stop.

### 3 — WHAT WAS BUILT

**Migration 114** — 7 new `open_positions` columns
(`scaled_in`/`scaled_in_qty`/`scaled_in_price`/`scaled_in_stop`/
`scaled_in_at`/`scale_in_order_id`/`scale_in_status`) and two switches,
`swing_scale_in_auto_entry`/`swing_scale_in_live_auto_entry`, both
`'false'` — mirroring `swing_auto_entry`/`swing_live_auto_entry`'s
name and shape exactly. Applied to the real project; verified live:

```
scale_in_order_id  text | scale_in_status  text | scaled_in boolean
scaled_in_at  timestamptz | scaled_in_price numeric | scaled_in_qty integer
scaled_in_stop numeric
swing_scale_in_auto_entry = false | swing_scale_in_live_auto_entry = false
```

**`intraday/engine.py`** — `_shadow_scale_in()` extended (still
byte-for-byte the same shadow log when either switch is off) to call new
`_execute_scale_in()` once armed, which places a BUY through the SAME
`execution.order_manager.place()`/`paper_broker.simulate_fill()` every
other entry uses — no new order-placement machinery invented. PAPER
fills merge immediately via new `_merge_scale_in_fill()`; LIVE submits,
writes `scale_in_status='PENDING_FILL'` (deliberately NOT the row's own
`status` — that would hide the original tranche from every exit reader
while only the add is unresolved) and `scale_in_order_id`, guard set
AFTER the write exactly matching the F-67 fix's own ordering rationale.
New `_resolve_pending_scale_ins()` (slow timer, wired into
`intraday/run.py` next to `_resolve_pending_fills()`) confirms COMPLETE
fills via `_merge_scale_in_fill()` and clears REJECTED/CANCELLED ones —
the row SURVIVES a discard (unlike a fresh entry's pending row): this is
an add to a real position, not a speculative new one. `load_state()`
rebuilds `self._pending_scale_ins` from `scale_in_status='PENDING_FILL'`
rows, the restart-survival half `_pending_fills` already has.

**`tools/health.py`** — new `check_pending_scale_ins`, mirroring
`check_pending_fills` (a stuck add must not be invisible just because
`scale_in_status` is a column nothing else in the registry reads),
registered in `CHECKS`.

**`tools/simulate.py`** — message text only; still calls
`evaluate_scale_in()` for preview and never `_execute_scale_in`, so it
stays read-only regardless of the switches' state.

### 4 — VERIFIED

10 new tests in `tests/test_stage_e7_scale_in_execution.py`, all against
the REAL `evaluate_scale_in()` and `paper_broker.simulate_fill()` (not
mocked — matching F-78's own "sized through the REAL check_new_entry(),
not mocked" standard), with only the Supabase client and Kite session
faked (the "live book" `tests/__init__.py` says does not belong here — a
fake in-memory table is not that). Covers: switches-off is zero
regression (never reaches `place()`); the in-flight guard (both the DB
column and the in-memory dict) skips a qualifying position; an armed
PAPER fill grows qty/invested_value and sets `scaled_in_*` while
`entry_price`/`planned_stop`/`active_sl`/`planned_target`/`target_price`
stay byte-for-byte unchanged; the one-add cap holds under execution, not
just detection; a LIVE submission leaves the row's own `status='ACTIVE'`
throughout and only `scale_in_status` pending; the second switch alone is
not enough once SWING is LIVE; confirm and reject both resolve correctly
without touching the baseline; `load_state()` rebuilds the pending guard.
Demonstrated failing first (§ above). `tools.verify`: 1060/1063, only the
pre-existing unrelated `stale token alerting` failures remain.

### 5 — WHAT THIS SESSION DID NOT DO

Did not arm either switch — both ship `false`, exactly the same
build-then-arm cadence this whole track has used since Stage C2, and the
same one this session's OTHER finding (F-80) shows is worth trusting only
once verified working, not assumed from a green checkmark. Arming is a
separate, later, explicit operator decision — this session only closed
the gap named in the operator's own screenshot ("no 'enable' available
because the code to actually place an add-on order doesn't exist yet").
It now exists, off, tested, and reusing every piece of proven order-
placement machinery this project already has rather than inventing new.

### 6 — COULD NOT DETERMINE

No real scale-in has ever fired (by construction — both switches are
off, and F-78's own quantify pass found only 2 of 17 recent trades ever
crossed the runner line). This session's tests prove the MECHANISM is
correct against real functions in a controlled harness; they cannot and
do not claim a real add-on order has been placed or confirmed against
the live broker. That evidence can only come from arming the switches
and watching a real qualifying trade, which is future work, not this
session's.

**Recommends:** watch `check_pending_scale_ins` and the existing
`sector_risk`/`stops` checks for a session or two after arming, the same
way F-79 named its four thin-evidence switches for post-arm observation
rather than treating "builds cleanly" as "behaves correctly under real
capital." Arming itself needs the operator's own explicit go-ahead,
matching F-78's own precedent of confirming before this stage's first
step and F-79's precedent of confirming before flipping any switch live.

**Gate:** PASS — execution built, tested, migration applied, both
switches OFF.
