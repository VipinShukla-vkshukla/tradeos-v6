"""
Phase 3a of the swing framework evolution blueprint, 26-Aug-2026.

_zone_aware_slip_bps() prices the live entry chase's slip off where `ltp`
sits inside the plan's own entry zone, instead of always paying the same
flat premium — the RKFORGE complaint: quoted 720, then 717, as price
drifted, always chasing at the same distance regardless of how much runway
was left in the zone.
"""

from __future__ import annotations


def test_favourable_edge_gets_the_tight_slip():
    from intraday.engine import _zone_aware_slip_bps
    bps = _zone_aware_slip_bps(ltp=100.0, zone_low=100.0, zone_high=110.0,
                               tight_bps=5, wide_bps=20)
    assert bps == 5, f"at the zone low there is maximum runway, expected tight_bps, got {bps}"


def test_unfavourable_edge_matches_todays_flat_value_exactly():
    """No regression at the edge closest to max_entry — this must be
    byte-identical to today's behaviour, not just 'close'."""
    from intraday.engine import _zone_aware_slip_bps
    bps = _zone_aware_slip_bps(ltp=110.0, zone_low=100.0, zone_high=110.0,
                               tight_bps=5, wide_bps=20)
    assert bps == 20, f"at the zone high, expected wide_bps unchanged, got {bps}"


def test_midpoint_interpolates_linearly():
    from intraday.engine import _zone_aware_slip_bps
    bps = _zone_aware_slip_bps(ltp=105.0, zone_low=100.0, zone_high=110.0,
                               tight_bps=5, wide_bps=20)
    assert abs(bps - 12.5) < 1e-9, f"halfway through the zone, expected 12.5, got {bps}"


def test_price_outside_zone_clamps_rather_than_extrapolates():
    from intraday.engine import _zone_aware_slip_bps
    below = _zone_aware_slip_bps(ltp=90.0, zone_low=100.0, zone_high=110.0,
                                 tight_bps=5, wide_bps=20)
    above = _zone_aware_slip_bps(ltp=120.0, zone_low=100.0, zone_high=110.0,
                                 tight_bps=5, wide_bps=20)
    assert below == 5, f"below the zone must clamp to tight_bps, got {below}"
    assert above == 20, f"above the zone must clamp to wide_bps, got {above}"


def test_degenerate_or_missing_zone_falls_back_to_wide_unconditionally():
    """A zone that cannot be trusted must not be guessed at — today's flat
    figure is the safe default, not an invented one."""
    from intraday.engine import _zone_aware_slip_bps
    assert _zone_aware_slip_bps(100.0, None, 110.0, 5, 20) == 20
    assert _zone_aware_slip_bps(100.0, 100.0, None, 5, 20) == 20
    assert _zone_aware_slip_bps(100.0, 0, 0, 5, 20) == 20
    assert _zone_aware_slip_bps(100.0, 110.0, 100.0, 5, 20) == 20, (
        "an inverted zone (high <= low) must also fall back, not divide "
        "by a negative range")


TESTS = [
    ("favourable edge gets the tight slip", test_favourable_edge_gets_the_tight_slip),
    ("unfavourable edge matches today's flat value exactly",
     test_unfavourable_edge_matches_todays_flat_value_exactly),
    ("midpoint interpolates linearly", test_midpoint_interpolates_linearly),
    ("price outside zone clamps rather than extrapolates",
     test_price_outside_zone_clamps_rather_than_extrapolates),
    ("degenerate or missing zone falls back to wide unconditionally",
     test_degenerate_or_missing_zone_falls_back_to_wide_unconditionally),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
