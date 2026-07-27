"""
Intraday strategy engines.

Separate from the swing engines in signals/screen_stocks.py on purpose. They
answer a different question on a different clock with different inputs: swing
engines read daily bars and ask "is this worth owning for a fortnight";
these read the current session and ask "is there a move worth taking before
15:20, net of costs".

Sharing one engine registry between them would force every swing engine to
carry a notion of session phase it has no use for, and every intraday engine to
carry a notion of six-month relative strength it cannot act on.
"""
