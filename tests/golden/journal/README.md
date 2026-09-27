# Synthetic journal fixtures

Hand-written, not real trades. One file per on-disk format `scanner/journal_io.py` must read:

| file | format |
|---|---|
| `2026-07-28.json` | legacy closed-trade records (`trade_id`, `entry_price`, `exit_price`) |
| `2026-08-17.json` | fill rows that still carry stored `PnL_Pct` / `PnL_Dollars` / `Entry_Price` |
| `2026-08-21.json` | current fill schema (`Action`, `Price`, `At`, `Source`) |

Tests point `scanner.journal_io.JOURNAL_DIR` here via the `golden_journal` fixture in
`tests/conftest.py`. Never copy real `data/journal/` files into this repo — it is public.
