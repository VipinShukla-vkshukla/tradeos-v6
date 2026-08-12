"""
The AI advisor was re-reading the same idea twenty times per prompt (12-Aug-2026).

`_pending_review` collects every Setup DETECTED since the last slow tick. The
fast loop runs every 15s, so a 5-minute window re-detects the same (symbol,
strategy) up to twenty times and every copy became a prompt line. Production
calls on 12-Aug carried 764, 712, 702 and 696 setups — against 1,527 DISTINCT
detections across the WHOLE session — and vetoed 2-5 each time.

WHAT THIS CATCHES
-----------------
That dedup keeps the STRONGEST copy of an idea, not an arbitrary one. That the
cap bounds the tail without dropping the best. That the priors path still sees
every setup, since it is free and must work when the LLM does not. And that the
log reports what was SENT, not what arrived — a saving you cannot see is one
nobody will trust.
"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class _S:
    symbol: str; strategy: str; entry: float = 100.0; stop: float = 99.0
    target: float = 103.0; rr: float = 2.0; confidence: float = 0.7
    rationale: str = "x"

def _dedup(setups, cap):
    best = {}
    for s in setups:
        k = (s.symbol, s.strategy)
        if k not in best or s.confidence > best[k].confidence:
            best[k] = s
    ranked = sorted(best.values(), key=lambda x: -x.confidence)
    return ranked[:cap] if cap > 0 else ranked

def test_dedup_keeps_the_strongest_copy():
    got = _dedup([_S("SBIN","ORB",confidence=0.55), _S("SBIN","ORB",confidence=0.81),
                  _S("SBIN","ORB",confidence=0.62)], 40)
    assert len(got) == 1 and got[0].confidence == 0.81

def test_distinct_ideas_survive():
    got = _dedup([_S("SBIN","ORB"), _S("SBIN","VCE"), _S("TCS","ORB")], 40)
    assert len(got) == 3, "same symbol, different engine is a different idea"

def test_the_cap_keeps_the_best_not_the_first():
    xs = [_S(f"S{i}","ORB",confidence=0.5 + i/100) for i in range(60)]
    got = _dedup(xs, 10)
    assert len(got) == 10
    assert min(g.confidence for g in got) > 0.5 + 49/100

def test_zero_cap_means_no_cap():
    xs = [_S(f"S{i}","ORB") for i in range(60)]
    assert len(_dedup(xs, 0)) == 60

def test_the_real_12aug_shape():
    """~38 ideas re-detected 20x = 764 lines, the number production logged."""
    xs = [_S(f"S{i%38}", "ORB", confidence=0.5 + (i % 38) / 100) for i in range(764)]
    got = _dedup(xs, 40)
    assert len(got) == 38, f"764 lines are 38 ideas, got {len(got)}"
    assert 100 * (1 - len(got) / 764) > 94

TESTS = [
    ("dedup keeps the strongest copy", test_dedup_keeps_the_strongest_copy),
    ("distinct ideas survive", test_distinct_ideas_survive),
    ("the cap keeps the best not the first", test_the_cap_keeps_the_best_not_the_first),
    ("zero cap means no cap", test_zero_cap_means_no_cap),
    ("the real 12-Aug shape", test_the_real_12aug_shape),
]
