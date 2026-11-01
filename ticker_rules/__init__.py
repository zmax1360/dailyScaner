"""ticker_rules — per-ticker trading rules, kept in a database, run as paper trades.

    rules.py     the rule library (pure functions: bars in, signal out)
    market.py    5-minute bars -> 15-minute bars, VWAP, EMAs, opening relative volume
    store.py     data/ticker_rules.db: append-only rule settings + paper trades
    recorder.py  called once per scan: opens and resolves paper debit spreads

Phase 1 is paper only: nothing here changes scoring, ranking or the Game Plan.
"""
