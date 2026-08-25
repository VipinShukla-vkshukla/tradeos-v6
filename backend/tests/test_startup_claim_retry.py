"""
F-83, 25-Aug-2026 — claim_startup_lock_with_retry(), the bounded retry
wrapper around the single-shot startup lock (migration 077,
tests/test_daemon_lock.py).

THE GAP THIS CLOSES: every caller of the single-shot check — the systemd
timer (once a day) and a manual `tradeos vcn fix` (once per invocation) —
treated a refusal as final. Confirmed live: stopping the process that held
the lease did not bring Oracle back, because Oracle had already exited
from its earlier refusal and nothing was left running to notice the lease
had freed up.

WHAT THIS FILE DOES NOT RE-TEST: `claim_startup_lock()`'s own correctness —
that a live holder refuses, a stale one does not, primary status is never
consulted against a live holder — is `tests/test_daemon_lock.py`'s job,
proven there against real row/table fakes. This file tests ONLY the retry
wrapper's own behaviour, with `claim_startup_lock` itself replaced by a
scripted stand-in, because the property under test here is WHEN the
question gets asked, not what it is allowed to answer.
"""

from __future__ import annotations

from tests import cfg_ctx


def _lock(granted: bool, code: str = "HELD", detail: str = "test") -> "object":
    from intraday.lease import LockResult
    return LockResult(granted, code, "someone" if not granted else "me", detail)


class _Scripted:
    """Returns each LockResult in `results` in order, then repeats the last
    one forever — a call-count-based fake, matching this project's usual
    shape for a function invoked more than once per test."""
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, sb=None):
        self.calls += 1
        i = min(self.calls - 1, len(self.results) - 1)
        return self.results[i]


def _no_sleep_calls():
    """A time.sleep replacement that records calls instead of blocking —
    keeps every test here instant regardless of poll_s/timeout_s."""
    calls = []

    def _sleep(s):
        calls.append(s)
    return _sleep, calls


def test_grants_immediately_without_sleeping():
    """The common case — nobody else holds it. Must not cost a single
    sleep; a retry wrapper that pauses even on an immediate grant would
    slow down every ordinary daily start for no reason."""
    import time
    from intraday import lease

    fake = _Scripted([_lock(True, "FREE")])
    orig_claim, orig_sleep = lease.claim_startup_lock, time.sleep
    sleep_fn, calls = _no_sleep_calls()
    lease.claim_startup_lock = fake
    time.sleep = sleep_fn
    try:
        with cfg_ctx({}):
            r = lease.claim_startup_lock_with_retry(sb=None, timeout_s=100, poll_s=10)
    finally:
        lease.claim_startup_lock = orig_claim
        time.sleep = orig_sleep

    assert r.granted is True
    assert fake.calls == 1
    assert calls == [], "an immediate grant must not sleep at all"


def test_retries_across_a_transition_from_held_to_free():
    """The exact shape of the incident this closes: HELD, HELD, then FREE
    once the other holder actually goes away. Must claim on the attempt
    where the answer changes, not before and not by giving up first."""
    import time
    from intraday import lease

    fake = _Scripted([_lock(False, "HELD"), _lock(False, "HELD"), _lock(True, "STALE")])
    orig_claim, orig_sleep = lease.claim_startup_lock, time.sleep
    sleep_fn, calls = _no_sleep_calls()
    lease.claim_startup_lock = fake
    time.sleep = sleep_fn
    try:
        with cfg_ctx({}):
            r = lease.claim_startup_lock_with_retry(sb=None, timeout_s=100, poll_s=10)
    finally:
        lease.claim_startup_lock = orig_claim
        time.sleep = orig_sleep

    assert r.granted is True
    assert r.code == "STALE"
    assert fake.calls == 3, "must have asked again after each HELD, not stopped at the first"
    assert len(calls) == 2, "one sleep between each of the three attempts"


def test_gives_up_after_the_timeout_elapses():
    """A holder that is genuinely alive forever must not be retried
    forever — the bounded window is what keeps this from becoming a silent
    hang when the other daemon is legitimately still supposed to be
    running."""
    import time
    from intraday import lease

    fake = _Scripted([_lock(False, "HELD")])   # never grants
    orig_claim, orig_sleep, orig_monotonic = lease.claim_startup_lock, time.sleep, time.monotonic
    sleep_fn, calls = _no_sleep_calls()

    # A fake clock that advances by poll_s on every read after the sleep
    # call, so the loop's own elapsed-time check terminates without this
    # test needing to sleep for real.
    clock = {"t": 0.0}

    def _monotonic():
        return clock["t"]

    def _sleep(s):
        calls.append(s)
        clock["t"] += s

    lease.claim_startup_lock = fake
    time.sleep = _sleep
    time.monotonic = _monotonic
    try:
        with cfg_ctx({}):
            r = lease.claim_startup_lock_with_retry(sb=None, timeout_s=25, poll_s=10)
    finally:
        lease.claim_startup_lock = orig_claim
        time.sleep = orig_sleep
        time.monotonic = orig_monotonic

    assert r.granted is False
    assert r.code == "HELD", "must return the LAST real refusal, not invent a new code"
    # 25s timeout / 10s poll: attempts at t=0, t=10, t=20 all HELD; the
    # remaining window at t=20 is 5s, so the loop sleeps min(10, 5)=5s
    # before its 4th attempt at t=25, finds elapsed >= timeout and stops.
    assert fake.calls == 4
    assert calls[-1] == 5, "the final wait must be clamped to what remains, not the full poll_s"


def test_respects_a_zero_timeout_as_a_single_attempt():
    """timeout_s=0 must behave like the old single-shot call — one attempt,
    no sleep, an immediate refusal reported rather than a wait for a
    window that was never granted to exist."""
    import time
    from intraday import lease

    fake = _Scripted([_lock(False, "HELD")])
    orig_claim, orig_sleep = lease.claim_startup_lock, time.sleep
    sleep_fn, calls = _no_sleep_calls()
    lease.claim_startup_lock = fake
    time.sleep = sleep_fn
    try:
        with cfg_ctx({}):
            r = lease.claim_startup_lock_with_retry(sb=None, timeout_s=0, poll_s=10)
    finally:
        lease.claim_startup_lock = orig_claim
        time.sleep = orig_sleep

    assert r.granted is False
    assert fake.calls == 1
    assert calls == []


def test_an_explicit_argument_overrides_config():
    """Config (intraday_startup_claim_retry_seconds/_poll_seconds) must only
    fill in when the caller omits the argument — a test or a future caller
    that wants a specific window must not have it silently overridden by
    whatever happens to be in system_config."""
    import time
    from intraday import lease

    fake = _Scripted([_lock(False, "HELD")])
    orig_claim, orig_sleep = lease.claim_startup_lock, time.sleep
    sleep_fn, calls = _no_sleep_calls()
    lease.claim_startup_lock = fake
    time.sleep = sleep_fn
    try:
        # A config that would normally allow a long retry window — the
        # explicit timeout_s=0 argument must win anyway.
        with cfg_ctx({"intraday_startup_claim_retry_seconds": "9999",
                      "intraday_startup_claim_poll_seconds": "9999"}):
            r = lease.claim_startup_lock_with_retry(sb=None, timeout_s=0, poll_s=10)
    finally:
        lease.claim_startup_lock = orig_claim
        time.sleep = orig_sleep

    assert r.granted is False
    assert fake.calls == 1, "timeout_s=0 must mean one attempt regardless of the config value"


def test_the_sigterm_handler_raises_keyboard_interrupt():
    """intraday/run.py's own graceful-shutdown path is entirely built
    around `except KeyboardInterrupt`. The SIGTERM handler's only job is to
    convert into that exact exception so systemctl stop/restart reaches the
    SAME `finally: lease.release(sb)` Ctrl+C already reaches — proven here
    by calling the handler directly rather than sending a real OS signal,
    which a test process cannot safely do to itself."""
    from intraday.run import _handle_sigterm
    try:
        _handle_sigterm(15, None)   # 15 == SIGTERM, unused by the handler itself
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("_handle_sigterm must raise KeyboardInterrupt")


TESTS = [
    ("grants immediately without sleeping",
     test_grants_immediately_without_sleeping),
    ("retries across a transition from HELD to FREE",
     test_retries_across_a_transition_from_held_to_free),
    ("gives up after the timeout elapses",
     test_gives_up_after_the_timeout_elapses),
    ("timeout_s=0 behaves as a single attempt",
     test_respects_a_zero_timeout_as_a_single_attempt),
    ("an explicit argument overrides config",
     test_an_explicit_argument_overrides_config),
    ("the SIGTERM handler raises KeyboardInterrupt",
     test_the_sigterm_handler_raises_keyboard_interrupt),
]
