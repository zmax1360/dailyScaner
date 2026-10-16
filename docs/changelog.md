# Changelog

One line per change that alters what the system does or shows, newest first, in the
same commit as the change. Scoring-math changes also go in `CHANGES.md` with the new
`config_hash` (see `AGENTS.md`). Commit ids are from `main`.

## 2026-10-15

- **Settings → Scheduler & background jobs** (`jobs_status.py`, `ui/scheduler_panel.py`):
  every launchd job in `plist/` with how often it runs and whether it is running, the
  Telegram bot process, what the scheduler scans (interval, last and next scan, Telegram
  route), change a ticker's interval, restart the scheduler, log tail. (05d49ee, 7b7ad26)
- **Telegram per ticker** (`scheduler.ticker_chat_id`, `ticker_bot_token`):
  `TELEGRAM_CHAT_ID_<TICKER>` and `TELEGRAM_BOT_TOKEN_<TICKER>` in `.env` send that
  ticker's messages to its own chat with its own bot. `"own_chat": true` in
  `scheduler_config.json` means it never falls back to the shared chat. SPY scans every
  15 min into its own chat. (c2e467e, a90e25b)
- **Game Plan: 1H and 4H trend line** (`htf_trend.py`): each scan saves EMA 21/50, the
  last two swings and up / down / range for 1H and 4H; the plan shows one line, with or
  against the allowed side. Context only: blocks nothing, moves no target. (258ce42)
- **Game Plan: no plan on the wrong side of VWAP** (`game_plan.vwap_hold`): calls need
  price above VWAP, puts below; window and conflict holds are checked first. No entry
  zone when the entry limit falls past the stop's level. Intraday VWAP and EMAs use
  09:30–16:00 ET bars only; a session whose 09:30 bar has almost no volume is flagged.
  `scripts/vwap_check.py` compares the scanner's VWAP with 1-minute bars. The Oct 15
  gap to TradingView (344.37 vs 344.99) came from TradingView's partial-volume feed,
  not from the scanner. (948db7c)

## 2026-10-14

- Telegram: the not-ranked reason is HTML-escaped, and a message Telegram rejects with
  HTTP 400 is resent as plain text instead of being lost. (b3c98f4)

## 2026-10-11

- Price chart: expected move from the at-the-money straddle for the two nearest
  expiries, with edge labels for levels beyond the drawn range. (360c0e7, ea96a94, 6816a92)

## 2026-10-09

- Scheduler: `"notify": false` per ticker scans and records without Telegram. (053bd87)
- Game Plan: no plan outside 10:00–15:00 ET, or when the scanner's top pick (last 6
  scans) disagrees with the EMA trend rule. (d089eae)
- Gamma: one IV per strike, solved at the price the option quotes imply. (baccaae)

## 2026-10-08

- Report page: the stock, the top pick, gamma and your trades for a period. (e961486)
- Telegram: Game Plan and gamma sections in the automatic scan message. (5106a13)
- Scorecard: a break-even trade counts as a scratch, not a loss. (93b8d9c)

## 2026-10-07

- Scorecard page: reward to risk, the latest trades in R, targets and stops as
  traded. (12818cc, e513a55, c5dd4a5)
- Game Plan uses the live 5-minute price, and stops at usable levels. (12818cc)

## 2026-10-05

- Game Plan: the scan turned into a plan by fixed rules (side, type of day, levels,
  candidates, trade plan). (6eeeda9)
- Gamma: Profile layout; gamma levels as a price-chart option. (efa6c1e, c48f5a4)

## 2026-10-04

- UI split out of `app.py` into a left-hand menu of pages (`ui/pages/`), a ticker bar
  and a Settings page; Best Value gets its own page. Line references into `app.py` in
  older docs predate this. (a042deb … 97606f9)
- All-in-one price chart: candles, EMA 9/21/50, VWAP, volume, stochastic, ATR. (9d51715)
- Settings are saved to disk. (d5e5e30)
- Gamma: call wall split into support (below spot) and resistance (above); an
  in-the-money contract with an unusable quote takes its out-of-the-money pair's IV.
  (ac3d7ab, b1746d2)
- Telegram push: optional gamma section. (968a397)

## 2026-10-03

- Gamma page: net gamma exposure by strike and expiry (display only). (c36c32a …)
- Quote-repaired IV for delta (engine-v1.5), landed but **not active**; see
  `CHANGES.md`. (40bb7a4)
