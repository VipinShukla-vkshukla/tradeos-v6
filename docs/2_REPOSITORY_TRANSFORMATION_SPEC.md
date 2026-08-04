# TradeOS v7 — Repository Transformation Specification

**Purpose.** How the existing repository becomes the frozen architecture. This
is a migration guide, not a redesign. **Evolution over replacement is a binding
constraint**: the repository's measurement spine, risk plumbing, and operational
scars are assets, and rewriting them would discard the most valuable thing the
project owns.

---

## 1. Repository assessment

### 1.1 Existing strengths — preserve at all costs

| Asset | Why it is rare |
|---|---|
| **Detection-level outcome resolution** | Every intraday setup resolves regardless of whether it traded. This is the unbiased denominator the entire statistical framework depends on |
| **Point-in-time feature capture** | Features written at signal time, not reconstructed. Removes look-ahead by construction |
| **Immutable daily plan set** | The anchor for all forward-outcome work |
| **Risk plumbing** | Hard caps, broker-side stops, kill switch, lease arbitration, surveillance screening, event monitoring |
| **Quality gates** | The system refuses to trade on bad data — an instinct most retail systems lack |
| **Documented scars** | The codebase records failures and their causes. This is institutional memory and must not be refactored away |
| **Storage guard** | The roll-off function and usage view already exist, correctly written |
| **Cost model** | Product-aware structure already present; needs completion, not replacement |

### 1.2 Technical debt — resolve during transformation

| Debt | Nature | Resolution |
|---|---|---|
| Roll-off never invoked | Wiring | Schedule as final pipeline step |
| Win-probability computed and discarded | Wiring | Persist; establish artifact transport |
| Model artifact untracked and untransported | Environment | Versioned artifact fetched at pipeline start, or documented fallback |
| Wide state read on the 300 s timer | Efficiency | Narrow to consumed columns |
| Entry rank not carried to closed records | Wiring | Carry through |
| Sixteen engines, two bets | Structure | Consolidate to five |
| Two governance doors | Policy | Retire auto-apply |
| Bypassing entry paths | Structure | Route through or count |
| Constraint check per-proposal only | Logic | Add basket recheck |
| Flat charges absent from friction model | Economics | Complete the ledger |
| Dead file with three hardcoded exclusions | Housekeeping | Quarantine; leave exclusions until the operator decides |

### 1.3 Components to preserve unchanged

Ingestion set, indicator computation, regime classification, sector strength,
quality gate, event monitoring, surveillance ingestion, broker client and token
lifecycle, lease, kill switch, order manager, paper broker, broker-stop manager,
position lifecycle, exit rules, intraday exit policy, notifier, outcome
resolution, brain module set, health and simulate tools, launcher.

---

## 2. Transformation mapping

### 2.1 Keep unchanged

All of `swing/ingestion/*`, `swing/compute/*` (except as noted), `kite/*`,
`execution/*`, `control/*` (except monitors noted below), `intraday/exit_policy`,
`intraday/scanner`, `intraday/session`, `intraday/market_context`,
`intraday/news_gate`, `intraday/lease`, `intraday/notifier`,
`intraday/outcomes`, `swing/brain/*`, `tools/health`, `tools/simulate`,
`tools/validate_*`, `tools/audit_alert_reads`, `tools/control_room`.

### 2.2 Minor modification

| Component | Change |
|---|---|
| `intraday/price_feed` | Quote-mode subscription; handler purity preserved |
| `intraday/engine` | Allocator call-site; narrowed state read; provisional TAKE handling |
| `analysis/entry_ranking` | Output becomes a scoring input rather than the arbiter |
| `analysis/trade_decision` | Unchanged logic; output adapted to the common proposal shape |
| `ai/ml_support`, `ai/providers/ml_provider` | Persist win probability; resolve artifact transport |
| `ai/ai_decision_engine` | Conviction output demoted to annotation until validated |
| `run_pipeline` | Roll-off appended as final non-fatal step |
| `tools/weekly_review` | Cadence changed to quarterly; allocator scoring added |
| `tools/discover_engines` | Consumes allocation records as a second refused population |
| Frontend | Four new views; existing components untouched |

### 2.3 Major redesign

| Component | Nature of redesign |
|---|---|
| `intraday/cost_model` | Extend to a complete friction ledger: flat per-scrip charges, per product, at realistic clip sizes. Interface preserved; internals expanded |
| `analysis/portfolio_constraints` | Add basket-level recheck across simultaneous selections. Existing per-proposal path preserved |
| `analysis/risk_model` | Add minimum-viable-trade threshold and volatility-regime exposure scaling |
| `swing/signals/screen_stocks` | Consolidate nine engines to two; add post-earnings-drift engine |
| `intraday/strategies/*` | Consolidate seven engines to two families; absorbed engines become conditions |

### 2.4 Merge

- Seven residual swing screeners → the **continuation** engine (unified
  thresholds).
- `GAP`, `PDL` → **ORB-family** as day-type/reference conditions.
- `PBK` → **VWR-family** as a condition.

### 2.5 Split

None. Splitting live modules is prohibited under the evolution constraint. The
only new package is `allocation/`, created rather than split from anything.

### 2.6 Rename

**None.** Renaming live modules breaks the hardcoded exclusion lists, workflow
invocations, and launcher subcommands that reference them by name. Names are
frozen.

### 2.7 Deprecate (quarantine, not delete)

`VCE`, `RNG`, the seven residual screeners, `control/paper_entry` (no live
caller found), the known dead ingestion copy. Each is recorded in
`QUARANTINE.md` with the search performed and the risk if the search was wrong.
**Retirement is shadowed one quarter before any removal is even proposed.**

### 2.8 Archive

Historical price rows beyond the retention window move to the slim archive table
by the existing roll-off function. Per-minute derived data, if ever produced,
lives on the VM and is never uploaded.

### 2.9 New modules

```
backend/allocation/proposal      common shape + adapters
backend/allocation/scoring       empirical E[R], friction-charged
backend/allocation/hurdle        opportunity cost, two regime buckets
backend/allocation/policies      assignment (swing) | stopping (intraday)
backend/allocation/allocator     select, basket recheck, buffered write
backend/tools/rollback           status / allocator-off / all-off
backend/tools/expectancy_ledger  net R by product and clip size
backend/tools/allocator_report   shadow comparison, disagreement count
```

Nine new files total. Everything else is modification of existing code.

---

## 3. Architecture mapping — current subsystem to Phase 4 equivalent

| Current subsystem | Phase 4 role | Change |
|---|---|---|
| Nightly pipeline | Band 01–04, unchanged | Roll-off appended |
| Screening engine set | Two swing engines + PEAD | Consolidated |
| Ranking composite | One input to `score()` | Demoted |
| Conviction tier | Annotation | Demoted pending validation |
| Decision function | Proposal source | Output adapted |
| Intraday engine set | Two families | Consolidated |
| Confidence floor | Superseded by `hurdle()` | Replaced in role, code retained until parity proven |
| Replacement case | Superseded by allocator comparison | Retained; Phase 5 generalises it |
| Exit engines | Unchanged, per book | Instrumented only |
| Weekly review | Quarterly review + allocator scoring | Cadence and scope |
| Discovery engine | Same, second refused population | Input added |
| Brain | Unchanged | Governed by freeze calendar |
| Storage guard | Band 08, active | Invoked |
| Dashboard | Operator surface | Four views added |

---

## 4. Dependency migration

**No new external dependencies except one**: a public source for block and bulk
deal disclosures, required by the accumulation engine. It joins the existing
ingestion pattern.

**Internal dependency direction is fixed and must not invert:**

```
allocation → analysis, intraday.cost_model, ai (priors)
allocation ↛ execution        (the allocator must never reach an order path)
analysis   ↛ allocation       (no upward dependency)
intraday.engine → allocation  (call-site only)
```

The prohibition on `allocation → execution` is structural and verifiable by
inspection. It is the mechanism that makes shadow mode safe.

---

## 5. Database evolution

**Additive only. No drops, no renames, no destructive migrations.** Migrations
run against a live book with real money and there are no platform backups.

| Migration | Content | Reversibility |
|---|---|---|
| Next + 0 | Allocation record table with verdict, score components, hurdle inputs, outcome fields, and a shadow flag defaulting true | Drop is prohibited; unused table costs kilobytes |
| Next + 1 | Win-probability column on the plan set | Additive column |
| Next + 2 | Entry rank and R carried on closed records | Additive columns |
| Next + 3 | Retention support for append-only tables beyond price history | Archive tables mirror the existing pattern |
| Next + 4 | Views for allocation summary and shadow comparison | Views only |

**Rollback of a migration is never performed by dropping what it created.** A
reverted code change simply stops reading the new column.

**Retention.** The existing roll-off function is invoked, not rewritten. Windows
for other append-only tables are proposed with measured growth rates and decided
by the operator, because a retention window determines what the system can still
learn from.

---

## 6. Interface compatibility

**Preserved without exception:** the launcher's subcommand surface, workflow
invocation paths, module import paths for all live modules, the decision
function's signature, the exit evaluation signatures, the health tool's contract,
and the frontend's existing API routes.

**Extended, backward compatible:** the friction model gains parameters with
defaults matching current behaviour; the constraint checker gains a basket entry
point while the per-proposal entry point is unchanged.

**New surfaces:** the allocation package's public functions, and three new CLI
tools. Nothing existing is broken to accommodate them.

---

## 7. Configuration migration

All new behaviour is config-gated and **defaults to current behaviour**:

| Key class | Default | Effect when off |
|---|---|---|
| Allocator shadow | on | Records, changes nothing |
| Allocator live, per book | **off** | Greedy path unchanged |
| Quote-mode feed | on after parity check | Falls back to prior source |
| Measured friction | **off** until reconciled | Prior constants used |
| Volatility scaling | **off** until measured | Flat exposure |
| Engine retirement | shadowed | Retired engines still evaluated, not traded |
| Freeze calendar | on | Proposals accumulate |

**Retired:** per-type auto-apply policy keys. This is the one deliberate
configuration removal, and it closes the second governance door.

---

## 8. Testing migration

**Existing verification pattern is preserved and extended.** This repository
verifies through the health tool, the simulator, and select-validation rather
than a conventional test suite, and that pattern matches the domain.

| Layer | Evolution |
|---|---|
| Health | Gains a storage check that **FAILS**, not warns; gains allocator plumbing checks |
| Simulate | Gains allocator shadow output; remains write-free |
| Select validation | Extended to new tables and narrowed reads |
| New: parity harness | Proves allocator shadow output does not alter live-path behaviour |
| New: guard tests | Every new guard demonstrated failing before being trusted |
| New: friction reconciliation | Modelled versus realised on ≥20 real round trips, within 10% |

**Every new check must be shown failing when it should.** Four checks in this
repository's history reported green while what they watched was broken.

---

## 9. Rollout strategy

Strictly sequential. Each stage gates the next.

```
S1  Survival        roll-off invoked · storage FAIL · backup · rollback tool
S2  Economics       friction ledger · minimum viable trade · sizing policy
S3  Exits           runner verified · empirical-distribution exits · excursion
S4  Measurement     rank carried · full-field priors · allocator scoring
S5  Inputs          quote fields · staleness guards
S6  Consolidation   16 → 5 engines, retirees shadowed one quarter
S7  Governance      quarterly freeze · auto-apply retired
S8  New alpha       PEAD · accumulation, detection-shadow before capital
S9  Overlays        expiry conditioning · volatility scaling · liquidity gate
S10 Allocation      shadow → ≥30 disagreements → intraday paper → swing
```

Each stage: implement behind a switch defaulting off → verify in simulation →
enable in shadow or paper → observe → enable live. **No stage begins before its
predecessor's acceptance criteria are met and read by the operator.**

---

## 10. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Narrowed state read omits a consumed column | Medium | High — runtime failure on a live book | Derive the column list by tracing consumers; verify with the simulator before and after |
| Engine consolidation discards independent edge | Medium | Medium | One-quarter shadow; burden of proof on retention |
| Migration against a near-full database fails | Low | **Critical** | Measure headroom before any migration; S1 precedes everything |
| Shadow allocator leaks into the live path | Low | Critical | Structural prohibition on the dependency; verifiable by inspection |
| Quote-mode parity differs from prior source | Medium | Medium | Log both for one session; enable only on parity |
| Friction ledger wrong in the optimistic direction | Medium | High | Reconcile against ≥20 realised round trips within 10% |
| Operator promotes allocator on thin evidence | Medium | High | Gate denominated in disagreements, not time |
| Quarantined module was actually live | Low | High | Search includes scheduler, launcher, and remote crontab; nothing removed without operator decision |

---

## 11. Success criteria

The transformation is complete when all of the following hold:

1. Storage headroom is measured and its ceiling date is receding.
2. A written expectancy ledger exists showing net R per trade by product and
   clip size, reconciled to realised charges within 10%.
3. Median capture ratio is measurably improved on live trades.
4. Every prior in the system traces to an unbiased denominator with a stated `n`.
5. No decision executes on data of unknown age.
6. Five engines run, each generating enough detections monthly to support a
   monthly verdict.
7. A freeze calendar exists and has been honoured through one full cycle.
8. The allocator has either beaten greedy across ≥30 disagreements or been
   retired with the evidence recorded.
9. No live module was renamed, no file deleted, no schema object dropped.
10. Every new guard has been demonstrated failing.
