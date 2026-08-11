"""
Stale Kite token now ALERTS instead of only logging (11-Aug-2026).

WHAT THIS CATCHES
------------------
On 10-Aug the token had been stale since Friday 04:33 IST, and nothing
surfaced that until an operator happened to read the log after the fact —
the daemon logged "no prices yet" as a routine WARNING for 21 minutes at
the open. kite.token_manager.alert_if_stale() sends a push instead, called
both proactively (--check-and-alert, for a scheduled pre-market run) and
reactively (intraday/run.py, once a stretch of blindness crosses
intraday_token_alert_after_cycles).

ROUTED THROUGH intraday.notifier.Notifier, NOT control.telegram_bot. The
first version of this fix called control.telegram_bot.send_message(), which
turned out to be dead code — that module imports TELEGRAM_BOT_TOKEN from
config, a name config.py has never exported (its own constant is
TELEGRAM_TOKEN), so importing it raises on every call and nothing has ever
sent through it. test_alert_if_stale_does_not_depend_on_the_broken_
telegram_bot_module below is a permanent regression guard for exactly that
class of mistake recurring.

This module still does NOT automate the Zerodha login — storing a password
and TOTP seed to drive it would hand an unattended script full trading
authority. These tests otherwise pin: the pure "alert exactly once per
stretch of blindness" gate; alert_if_stale() sends nothing when the token
is actually valid (so a websocket blip does not spam the channel); it sends
the login URL when the token is invalid; and a notifier failure degrades to
"no alert sent", never a raised exception, the same never-raises contract
get_access_token() itself carries.
"""

from __future__ import annotations

from unittest.mock import patch


# ── the pure "alert exactly once" gate ──────────────────────────────────────

def test_below_threshold_does_not_alert():
    from kite.token_manager import should_alert_now
    assert should_alert_now(blind_cycles=3, threshold=4, already_sent=False) is False


def test_at_threshold_alerts_once():
    from kite.token_manager import should_alert_now
    assert should_alert_now(blind_cycles=4, threshold=4, already_sent=False) is True


def test_already_sent_does_not_alert_again():
    from kite.token_manager import should_alert_now
    assert should_alert_now(blind_cycles=9, threshold=4, already_sent=True) is False, (
        "must not re-alert every cycle for the rest of a long blind stretch")


def test_past_threshold_still_alerts_if_not_yet_sent():
    """Covers a threshold change or a missed cycle landing past the boundary."""
    from kite.token_manager import should_alert_now
    assert should_alert_now(blind_cycles=12, threshold=4, already_sent=False) is True


# ── control.telegram_bot must never be relied on again ──────────────────────

def test_control_telegram_bot_now_imports_cleanly():
    """
    control.telegram_bot imported TELEGRAM_BOT_TOKEN from config, a name
    config.py has never exported (its own constant is TELEGRAM_TOKEN — the
    mismatch was between the module attribute and the identically-spelled
    ENV VAR it reads FROM), so the module raised ImportError on load and
    every one of its functions (send_message, the /approve Telegram bot)
    was unreachable. Fixed in the same session this was found — this pins
    the fix rather than the original break. alert_if_stale() does NOT
    depend on this module either way (see the test below): it is routed
    through intraday.notifier.Notifier, the path every other intraday alert
    already uses, so this file working or not working cannot affect it.
    """
    import importlib
    importlib.import_module("control.telegram_bot")   # must not raise


def test_alert_if_stale_does_not_depend_on_the_broken_telegram_bot_module():
    """Regression guard: alert_if_stale() must keep working even though
    control.telegram_bot cannot be imported at all — proof that fixing (or
    re-breaking) that unrelated module cannot silently affect this alert."""
    from kite import token_manager
    with patch("kite.token_manager.is_token_valid", return_value=False), \
         patch("intraday.notifier.Notifier.send", return_value=True) as mock_send:
        sent = token_manager.alert_if_stale()
    assert sent is True
    mock_send.assert_called_once()


# ── alert_if_stale() ─────────────────────────────────────────────────────────

def test_no_alert_when_token_is_actually_valid():
    """A websocket blip with a fine token must send nothing — alert_if_stale()
    is safe to call on ANY blindness, not just a token one."""
    from kite.token_manager import alert_if_stale
    with patch("kite.token_manager.is_token_valid", return_value=True), \
         patch("intraday.notifier.Notifier.send") as mock_send:
        sent = alert_if_stale()
    assert sent is False
    mock_send.assert_not_called()


def test_alert_includes_the_login_url_when_invalid():
    from kite.token_manager import alert_if_stale
    with patch("kite.token_manager.is_token_valid", return_value=False), \
         patch("kite.token_manager.KITE_API_KEY", "testkey123"), \
         patch("intraday.notifier.Notifier.send", return_value=True) as mock_send:
        sent = alert_if_stale()
    assert sent is True
    assert mock_send.call_count == 1
    action = mock_send.call_args[0][0]
    assert "kite.zerodha.com/connect/login" in action.detail, (
        "the operator must not have to go hunting for the login link")
    assert action.urgency == "CRITICAL"
    assert action.framework == "SWING"


def test_reason_prefix_is_included_when_given():
    from kite.token_manager import alert_if_stale
    with patch("kite.token_manager.is_token_valid", return_value=False), \
         patch("kite.token_manager.KITE_API_KEY", "testkey123"), \
         patch("intraday.notifier.Notifier.send", return_value=True) as mock_send:
        alert_if_stale("Intraday daemon: no live prices for 4 cycles (~60s).")
    action = mock_send.call_args[0][0]
    assert "no live prices for 4 cycles" in action.detail


def test_notifier_failure_never_raises():
    """The never-raises contract get_access_token() itself carries."""
    from kite.token_manager import alert_if_stale
    with patch("kite.token_manager.is_token_valid", return_value=False), \
         patch("intraday.notifier.Notifier.send", side_effect=RuntimeError("boom")):
        sent = alert_if_stale()   # must not raise
    assert sent is False


def test_send_returning_false_is_reported_as_not_sent():
    from kite.token_manager import alert_if_stale
    with patch("kite.token_manager.is_token_valid", return_value=False), \
         patch("kite.token_manager.KITE_API_KEY", "testkey123"), \
         patch("intraday.notifier.Notifier.send", return_value=False):
        sent = alert_if_stale()
    assert sent is False, "no channel configured must read as not sent"


TESTS = [
    ("below threshold does not alert", test_below_threshold_does_not_alert),
    ("at threshold alerts once", test_at_threshold_alerts_once),
    ("already sent does not alert again", test_already_sent_does_not_alert_again),
    ("past threshold still alerts if not yet sent",
     test_past_threshold_still_alerts_if_not_yet_sent),
    ("control.telegram_bot now imports cleanly (regression pin for the fix)",
     test_control_telegram_bot_now_imports_cleanly),
    ("alert_if_stale does not depend on the broken telegram_bot module",
     test_alert_if_stale_does_not_depend_on_the_broken_telegram_bot_module),
    ("no alert when token is actually valid", test_no_alert_when_token_is_actually_valid),
    ("alert includes the login URL when invalid",
     test_alert_includes_the_login_url_when_invalid),
    ("reason prefix is included when given", test_reason_prefix_is_included_when_given),
    ("notifier failure never raises", test_notifier_failure_never_raises),
    ("send returning False is reported as not sent",
     test_send_returning_false_is_reported_as_not_sent),
]
