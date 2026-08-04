# TradeOS — Investment Committee Review of Trading Methodology

**Scope discipline:** trading methodology, statistical validity, and durability of
edge only. Software is out of scope. The repository is the sole source of truth;
where the committee applies market knowledge the repository does not contain
(fee schedules, exchange mechanics), it is labelled as committee estimate.

**Evidence base drawn on throughout:** ~70 closed swing trades, 14 closed
intraday trades; caps of 2 swing entries/day at ≤₹4,000 on a ₹20,000 book,
intraday ≤₹6,000 in paper; the exit module's own docstring recording *median
capture ratio 3% across 70 realised trades*; a conviction-tier validation
routine that has never produced a verdict because its input column was never
populated; detection-level outcome resolution of every intraday setup; a
weekly learning cadence spanning nine screening engines, seven intraday
engines, a thirteen-analysis quant module and an LLM synthesis pass.

---

## 1. Executive Summary

This is a long-only momentum-confluence system: a screener-derived universe is
scored on technical confluence, enriched with sentiment and an LLM-assigned
conviction tier, entered on next-day zone touches with broker-side stops, and
exited on a 3R target with a runner conversion that its own telemetry suggests
rarely fires. An intraday sleeve of seven continuation-style engines runs in
paper. Risk plumbing is genuinely strong for the capital class. The learning
apparatus is large, earnest, and starved.

The committee's view in three sentences. **The system's realised expectancy is
currently dominated not by signal quality but by two self-inflicted wounds:
a cost structure that consumes on the order of a fifth of risk per trade at
current position sizes, and an exit process that its own measurement shows
captures 3% of the favourable move.** The central conviction layer — the AI
tiering that decides what is bought — has never once been validated against
forward outcomes, because the validation loop was built and its input never
connected. And the adaptation cadence runs roughly an order of magnitude faster
than information arrives, which converts an honest learning system into an
overfitting engine with excellent documentation.

None of these is fatal. All three are fixable without a single new alpha idea.
That is simultaneously the most damning and most hopeful thing this committee
can say: the largest CAGR improvements available do not require better
predictions — they require the system to stop leaking the edge it may already
have.

On durability: the signal base (screener breakout confluence) is the most
crowded retail alpha in India and should be presumed decaying. The genuinely
differentiated assets are the measurement spine — point-in-time feature capture
at signal time, and resolution of *every* detection rather than only taken
trades — and the ingestion of delivery percentage and surveillance lists, which
most retail systems ignore. The path to durable edge runs through those assets,
not through the confluence score.

---

## 2. Top 10 Findings — ranked by expected long-term CAGR improvement

All CAGR figures are committee estimates on the current capital and trade rate,
stated as ranges with confidence. They are judgments, not computations.

---

**F1 — The cost structure at current position size consumes most of any
plausible edge.**
Severity: **Critical** · Estimated CAGR impact: **+3 to +6pp** · Confidence: High

Evidence: position caps of ≤₹4,000 CNC are repository fact; the project's own
notes record a ~1.0% CNC round trip on a ₹2,000 position. Committee arithmetic
on top: delivery STT of 0.1% each side, stamp and exchange charges, and — the
killer — the flat depository participant charge of roughly ₹15–16 per scrip per
selling day, which alone is ~40bps on a ₹4,000 position. Total friction on a
₹3–4k CNC round trip is plausibly 1.1–1.4%. Against the system's typical ~6%
stop distance, that is **0.18–0.23R of structural drag on every swing trade
before the market moves**. A swing methodology of this style might honestly
generate 0.2–0.35R gross expectancy per trade; the fee schedule is eating
half to all of it. Why it matters: this is not an alpha problem, it is a
denominator problem — the same signals at 2–3× position size (fewer, larger
positions within the same total risk) or with the mix shifted toward the
intraday sleeve (MIS friction ~0.21%, no DP charge) mechanically transform net
expectancy. No prediction improvement in this entire report is worth as much as
this arithmetic.

---

**F2 — The exit process gives back the move: median capture ratio 3%.**
Severity: **Critical** · CAGR impact: **+3 to +5pp** · Confidence: High (the
number is the system's own measurement)

Evidence: the exit module's docstring states the motivation plainly — median
capture of favourable excursion was 3% across 70 realised trades. A runner
conversion and deterioration exit were built in response, but the giveback
guard on the intraday side is disabled for lack of excursion data (null on all
14 closed positions), and nothing in the record demonstrates the runner path
has ever fired in production. Why it matters: a momentum system's entire
economic thesis is the right tail. Capturing 3% of MFE means the system
routinely rides winners to near-breakeven exits — it is executing a momentum
entry with a mean-reversion exit. Moving median capture from 3% to even 35–50%
— an unambitious institutional norm for trend-following exits — is worth more
than any new engine. The committee notes with some incredulity that the
system *measured* this correctly and then could not confirm whether its own
remedy operates.

---

**F3 — The adaptation cadence exceeds the information arrival rate by roughly
an order of magnitude.**
Severity: **Critical (statistical)** · CAGR impact: **+2 to +4pp preserved**
(prevention of decay-by-overfitting) · Confidence: High

Evidence: weekly review, mid-week brain modes, a Sunday chain, and a change
manager with per-type auto-apply — against ~2 swing closes per day and 14
intraday closes *in total*. A week contributes perhaps 5–10 closed-trade
observations to a system tuning sixteen engines, nine ranking inputs, and
multiple gates. Why it matters: at this ratio, weekly parameter movement is
mathematically guaranteed to chase noise; the system will perpetually fit last
fortnight's regime and arrive at each new regime freshly mis-tuned. This is
p-hacking through time, performed sincerely. What professionals do at this
trade rate: freeze parameters per component for a quarter minimum; evaluate on
detection-level outcomes (which the system, to its great credit, records for
intraday) where n is 20–50× larger; and treat any change that cannot show
out-of-sample confirmation as decoration. The learning apparatus is not the
problem — its clock speed is.

---

**F4 — The central conviction layer has never been validated against outcomes.**
Severity: **Critical** · CAGR impact: **±2 to 3pp — sign unknown, which is the
finding** · Confidence: High that it is unvalidated; none on its actual value

Evidence: buying decisions rank on a composite in which the LLM-assigned tier
and conviction are first-class inputs; the routine built to test whether
higher-ranked entries outperform lower-ranked ones has never produced a verdict
because its input column was structurally empty. Why it matters: the system's
most load-bearing judgment — *which* of 56 plans deserves one of 2 slots — is
an untested hypothesis. An LLM's conviction, formed from enriched features plus
a rolling "lessons" digest of recent mistakes, is plausibly a recency-biased
re-weighting of features the score already contains; it could as easily be
negative alpha as positive. Until tier-by-tier forward returns exist, the
committee treats ai_tier as unpriced risk sitting at the top of the decision
stack. The validation is one measurement away and the measurement was designed
years of effort ago; run it before trusting another tier.

---

**F5 — Events are modelled exclusively as risk, never as alpha: no
post-earnings drift engine.**
Severity: **High** · CAGR impact: **+1.5 to +3pp** · Confidence: Medium-High

Evidence: the events pipeline ingests results calendars and corporate filings;
a news gate blocks entries carrying event risk; a morning monitor warns on held
names. Nowhere does an engine *trade* the event. Why it matters: post-earnings
announcement drift is among the most persistent, best-documented anomalies in
Indian equities — results-day gap plus elevated delivery percentage plus
continued drift is a classical institutional swing setup, and this system
already ingests every input it requires (results dates, gaps, delivery %,
volume). The philosophical stance — treat information arrival as danger — is a
retail reflex. Prop desks treat scheduled information arrival as the most
honest liquidity and mispricing window in the calendar. This is the cheapest
genuinely *new* alpha available to the system: the data is already flowing.

---

**F6 — Long-only with no bear-market expression beyond exposure reduction.**
Severity: **High** · CAGR impact: **+1 to +2pp over a full cycle; larger effect
on drawdown and psychological survivability** · Confidence: Medium-High

Evidence: regime gates (breadth-driven, with an ML classifier gated behind
autonomy phase) reduce or halt entries in adverse regimes; there is no
mechanism to profit from decline — no intraday short book despite MIS shorting
being fully available, and no explicit cash-yield posture when flat. Why it
matters: cash-market swing shorting is structurally unavailable in India
(fair), but the intraday sleeve's long bias is a choice, not a constraint —
gap-down continuation and VWAP-rejection shorts are the mirror of engines
already built. Over a 10-year horizon containing at least one 25%+ drawdown
year, a system whose only bear response is abstinence cedes both return and —
the committee stresses this — operator conviction, which is the true capital
that gets withdrawn at bottoms.

---

**F7 — The signal base is the most crowded retail alpha in India, and the
differentiated data the system already owns is under-weighted.**
Severity: **High** · CAGR impact: **+1 to +2pp** · Confidence: Medium

Evidence: the universe originates from a public screener; the scoring is
technical confluence (breakout structure, volume ratio, trend indicators) of
the style promoted to millions of Indian retail traders since 2020. Meanwhile
the pipeline ingests delivery percentage and surveillance lists — and delivery
%, the closest thing the cash market has to a daily institutional-accumulation
signal, appears as one indicator among 86 rather than as a pillar. Why it
matters: crowded signals do not merely decay; in the Indian small/mid-cap
segment they *invert* — visible breakout levels become retail stop pools that
operators and prop desks harvest. The escape is not a better breakout filter
but a different primary sort: accumulation evidence (sustained delivery
elevation, block/bulk deal prints — public daily data not currently ingested)
*confirmed* by structure, rather than structure confirmed by whatever is
handy. Same universe, inverted burden of proof.

---

**F8 — Expiry-day and derivative-spillover behaviour is entirely absent from
the intraday sleeve.**
Severity: **Medium-High** · CAGR impact: **+0.5 to +1.5pp on the intraday
sleeve** · Confidence: Medium

Evidence: no reference to expiry calendars anywhere in the decision path; the
F&O ban list is ingested for exclusion only. Why it matters: on index expiry
days the cash tape is a different animal — pinning, unwinding flows, and
late-session gamma effects systematically alter the behaviour of opening-range
breakouts and VWAP reversion, the exact archetypes this sleeve trades. A
continuation engine calibrated on all days is mis-calibrated on the ~20% of
days when derivative flows dominate. Minimum viable coverage is a day-type
flag (index expiry / stock-expiry week / normal) conditioning engine priors —
the detection-level outcome data to calibrate it already exists.

---

**F9 — Circuit bands and gap-through risk are unmodelled at entry and at the
stop.**
Severity: **Medium** · CAGR impact: **+0.5 to +1pp, expressed mostly as
avoided left-tail** · Confidence: Medium-High

Evidence: no reference to price-band status in candidate evaluation; overnight
protection is a broker-side stop, which converts to a market order on trigger
and fills wherever the gap opens. Why it matters: the screener universe reaches
into band-limited small caps, where a breakout signal into a 5% band cannot
travel and a held position gapping through its stop realises a loss multiple
of the planned R. The event monitor warns; sizing does not respond. A stop is
a plan for continuous prices; Indian small caps do not always offer continuous
prices. Band-aware candidate filtering and gap-exposure-aware sizing (smaller
positions across binary events, which the calendar already identifies) is
risk-management hygiene the philosophy currently lacks.

---

**F10 — Every engine is a continuation engine: no mean-reversion sleeve.**
Severity: **Medium** · CAGR impact: **+0.5 to +1pp, primarily as regime
diversification** · Confidence: Medium

Evidence: nine screening engines and seven intraday engines all express
trend-continuation or breakout logic; the sole reversion-flavoured component
is VWAP-related intraday behaviour. Why it matters: a book of sixteen engines
that all monetise the same market state (trend persistence) is one engine with
sixteen costumes — the "diversification" is cosmetic, and sideways regimes
(historically 30–40% of Indian market months) are pure bleed. A small,
liquid-large-cap oversold-quality reversion sleeve is negatively correlated
with everything currently running and would smooth the equity curve more than
any additional momentum variant. The committee flags the current engine count
as diversification theatre.

---

## 3. Statistical Risks

**Iteration outpacing information (F3)** is the umbrella risk; the specific
exposures beneath it:

- **Selection-biased priors.** Any win-rate or conviction estimate derived from
  executed trades inherits the old policy's taste; the ~70-trade closed book is
  both tiny and non-random. The system's own detection-level records (every
  intraday setup resolved; every daily plan's forward path recoverable) are the
  unbiased alternative and should be the default denominator for all learning.
- **Multiple testing across 114 recorded features.** Tercile-separation mining
  at n≈70 will surface ~10 spurious "material" relationships by chance;
  anything promoted without an out-of-sample confirmation window is noise
  wearing a lab coat.
- **Uncontrolled feedback in the lessons loop.** Feeding a rolling digest of
  recent mistakes into the LLM that assigns conviction is a recency amplifier
  with no measurable transfer function — the one component of the stack whose
  bias cannot even be audited. Contain it or instrument it.
- **Sample starvation of the ML components** is at least *documented* honestly
  (a conviction model requiring 90 samples, holding 70; a fitted artifact whose
  feature width no longer matches its inputs). The risk is not the models; it
  is the temptation to use their outputs anyway.
- **Hygiene done right, for the record:** point-in-time feature capture at
  signal time, evening-compute-for-next-day separation (no same-day leakage),
  quality gates that block a bad-data day, and detection-level outcome
  resolution. This is better bias hygiene than most retail systems the
  committee has reviewed, and it deserves saying.

## 4. Missing Market Behaviours

**Well covered:** ASM/GSM and F&O-ban screening · breadth-driven regime ·
sector strength ranking · FII/DII as context · scheduled-event *risk* ·
holiday calendar · delivery % (ingested) · India VIX (ingested).

**Partially covered:** gap behaviour (traded intraday, unmodelled as overnight
swing risk) · VWAP dynamics (one engine, no institutional-defence framing) ·
volatility clustering (VIX ingested, does not modulate sizing) · sector
rotation (ranked as filter, not traded as signal) · opening auction (post-9:15
range used; auction volume/price ignored) · operator behaviour (surveillance
lists yes; delivery-collapse pump signatures no).

**Missing entirely:** expiry-day and expiry-week effects · circuit/price-band
mechanics · closing-session dynamics (15:20–15:30) · post-earnings drift as
alpha · block/bulk-deal and insider-disclosure flow · buyback/corporate-action
setups · large/mid/small-cap behavioural segmentation (one rulebook for three
different markets) · any cash-yield posture when flat.

## 5. Missing Alpha Sources

Ranked by fit to existing data and capital scale: **(1)** post-earnings drift
(all inputs already ingested); **(2)** accumulation-led selection — delivery %
persistence plus block/bulk-deal prints as the primary sort (one new public
dataset); **(3)** intraday short mirror of existing engines; **(4)** large-cap
mean-reversion sleeve; **(5)** expiry-day specialisation of intraday engines;
**(6)** sector-rotation as a traded signal rather than a filter. Deliberately
excluded as scale-inappropriate: index/stat arb, options structures, pairs —
these require capital and infrastructure the mandate does not have.

## 6. Fragile Assumptions

1. *"Confluence of public technical signals retains edge"* — the load-bearing
   assumption, and the one most exposed to crowding decay.
2. *"An LLM's conviction adds information beyond the features it reads"* —
   unfalsified in either direction; currently taken on faith at the top of the
   stack.
3. *"A broker-side stop bounds loss"* — true only in continuous markets;
   gap-throughs and bands break it exactly when it matters.
4. *"Weekly adaptation makes the system current"* — at this trade rate it makes
   the system perpetually fitted to the recent past.
5. *"More engines equals more diversification"* — sixteen expressions of trend
   persistence are one bet.
6. *"Reduced exposure is a sufficient bear-market answer"* — solvent, but
   ceding both return and operator conviction through drawdowns.

## 7. Prop Firm Assessment

**Would pass a desk review:** the risk plumbing (hard caps, broker-side stops,
kill switches, surveillance screening, event monitor); detection-level outcome
resolution — resolving every setup, not just taken trades, is genuinely
institutional and rarer than it should be; point-in-time feature discipline;
the refusal to trade on failed data-quality gates; the documented scar-driven
decision to keep swing and intraday exits separate.

**Would be rejected on sight:** an unvalidated LLM tier as the apex ranking
input; weekly parameter movement at this sample rate; auto-apply authority of
any kind coexisting with a "propose, never apply" doctrine; expectancy
accounting that has not internalised the flat-fee arithmetic at current clip
sizes.

**Would require evidence before proceeding:** that the runner conversion fires
and helps (F2); tier-by-tier forward returns (F4); engine-level P&L net of
realistic friction; behaviour across at least one full adverse regime.

**Institutional vs retail character:** the *measurement philosophy* is
institutional; the *alpha philosophy* is retail. The system was built by
someone who learned risk like a professional and signals like the internet.

## 8. TradeOS v7 Vision (methodology only)

**Stays, and becomes the foundation:** the measurement spine — every detection
and every plan resolved to an outcome, point-in-time features, the regime
gate, the risk caps, event-risk awareness, surveillance screening.

**Disappears:** the LLM as a ranking input (demoted to annotator/veto with an
audited veto scorecard); weekly retuning (quarterly, per component, with
out-of-sample confirmation); the assumption that one rulebook serves large,
mid and small caps; engine proliferation without regime diversification.

**The v7 alpha stack, in order of capital priority:** (1) event-drift book —
PEAD long bias with delivery confirmation; (2) accumulation book — delivery
persistence + block-deal follow, structure as confirmation only; (3) trend
book — the current confluence system, cost-rationalised into fewer, larger
positions with the capture-ratio fix as its centrepiece; (4) intraday book —
existing engines conditioned on day-type, plus the short mirror; (5) reversion
sleeve — small, large-cap only, as the decorrelator. Sizing becomes
volatility-normalised at the trade level and regime-scaled at the book level;
conviction becomes empirical priors from the unbiased detection record;
capital-growth tiering is written down in advance so position-size arithmetic
(F1) is re-solved at each capital level rather than inherited.

**Completely missing today and required in v7:** an explicit expectancy ledger
per book, net of true friction, reviewed quarterly — the single document that
answers "where does the money actually come from," which no artefact in the
current repository can answer.

## 9. Overall Investment Committee Verdict

| Dimension | Score /10 | Note |
|---|---|---|
| Trading Philosophy | 6 | Coherent and honest; long-only crowded core |
| Statistical Rigor | 4 | Superb instrumentation intent; broken validation, cadence too fast |
| Robustness | 5 | Gates real; one-regime alpha book |
| Adaptability | 6 | Apparatus exists; starved and over-clocked |
| Risk Management | 7 | Genuinely strong for the capital class |
| Long-Term Edge | 3 | Crowded signals + cost drag; differentiated data unused |
| Institutional Readiness | 2 | Capacity in lakhs, not crores; core layer unvalidated |

Probability estimates (structured judgment, stated with humility): the
operation *survives* 5 years with capital intact — 60–70% (the risk caps are
the reason); the strategy as currently constituted carries positive net edge
over a full cycle — 15–25%; with F1–F4 remediated — 35–45%; probability the
tuned components are materially overfit today — ~70%; probability of material
NIFTY outperformance over 10 years in current form — 10–15%. Confidence in all
of the above: low-to-moderate, as honesty requires at n=70.

"If I were allocating institutional capital today, I would not invest in this
strategy because its capacity is measured in lakhs rather than crores, its
central conviction layer has never been validated against a single forward
outcome, and its cost structure at current position sizes consumes most of the
edge its signals could plausibly generate — but I would fund its measurement
discipline to maturity, because a system that records every rejection and
resolves every detection is one honest quarter away from knowing exactly what
it is."
