# Changelog

One line per change that alters what the system does or shows, newest first, in the
same commit as the change. Scoring-math changes also go in `CHANGES.md` with the new
`config_hash` (see `AGENTS.md`). Commit ids are from `main`.

## 2026-10-17

- **Game Plan type of day reads the gamma flip** (`game_plan.regime_from_gamma`). The
  sign now comes from the nearest expiry's net over every strike (`net_all_strikes`;
  older snapshots fall back to `net`), and the day is **UNSETTLED** when price is within
  0.15% of that expiry's gamma flip (`gamma_flip_nearest`). "What changes it" names the
  flip. Display only: the allowed side and the trade plan are unchanged.
- Gamma page heat map: colour scale tops out at the 90th percentile of non-zero |GEX|
  (`gex_page.color_scale_max`) instead of the largest cell, so one outlier near the
  money no longer washes out the rest. Display only.
- **Gamma flip and full-chain net** (`gex.full_chain_net`, `gex.net_gex_curve`,
  `gex.gamma_flip`). The flip re-prices every usable contract's gamma at spots within
  ±5% and interpolates the sign change nearest spot. `gex.summary` adds
  `net_all_strikes`, `net_full_chain`, `gamma_flip`, `gamma_flip_nearest`; `net` (the
  16-strike window that the Game Plan reads) is unchanged. Gamma page: a second metric row
  (all strikes, all expiries, gamma flip), the regime line reads spot vs the flip, and
  the Profile view draws the flip line. Display only; no scoring change.
- **Game Plan built once per scan and saved** (`dailyScaner._plan_snapshot`,
  `payload["game_plan"]`, `plan_report.saved_plan`). The scan also saves VWAP
  (`payload["vwap"]`), so the VWAP rule (no calls below VWAP, no puts above) now holds
  in Telegram too; before, Telegram plans had no VWAP and only the app applied it.
  Telegram (scheduled, bot, Settings push) and the app show the saved plan; none builds
  one (`tests/test_plan_report.py::test_no_reader_builds_a_plan`). The app adds a
  separate **Live now** line from the latest 5-minute bar: price against the scan's
  entry limit, stop and VWAP; it never changes the plan. Before, the app rebuilt the
  plan from the live price and current app settings, so it could disagree with the
  Telegram message for the same scan. Scans saved before this show "not saved in this
  scan". (Step 2 of 2.)
- **Gamma worked out once per scan and saved** (`dailyScaner._gamma_snapshot`,
  `payload["gamma"]`, `plan_report.saved_gamma`). The scan computes the gamma read
  (nearest live expiry: net, walls, support / resistance, every strike) from its own
  chain. The Telegram message, gamma-shift alert, Game Plan (app and Telegram), price
  chart lines and Settings push read it; none works gamma out again
  (`tests/test_plan_report.py::test_no_reader_works_gamma_out_again`). Before: each
  read the newest database snapshot, saved only every 15 min, so a 5-minute scan's
  message carried gamma up to 10 min older than the scan, and an old scan showed
  today's walls. Scans saved before this show "not saved in this scan". The Gamma page
  heatmap still reads the 15-minute history. (Step 1 of 2.)

## 2026-10-16

- **Settings in three tabs: Settings, Telegram, Scheduler** (`ui/telegram_panel.py`,
  `telegram_log.py`). Every Telegram send (scan message, gamma alert, scanner-stopped
  alert, the app's manual push) is recorded in `data/telegram_sent.jsonl`: when, ticker,
  kind, sent / failed with Telegram's own reason ("chat not found"), bot id, chat (last
  4 digits) and the start of the text; never the token. The Telegram tab shows each
  ticker's route and last send, the last 30 sends with their text, a button that asks
  Telegram which bot each ticker uses, and the manual push. The scheduler log now
  carries Telegram's reason instead of a bare "400 Bad Request".
- **Telegram: gamma-shift alert** (`gamma_shift.py`, `scheduler._gamma_shift_check`).
  After each scan, when the nearest expiry's call wall or put wall moves to another
  strike, a short message says from where to where, both strikes' gamma now, and the
  day's path (e.g. $345 → $342.5 → $340 → $345, "back to an earlier level"). A new
  strike must beat the old one by 10% (no flip-flop on near ties); the first read of a
  day or expiry only sets the baseline. Walls are kept in `data/gamma_walls.json`.
  `"gamma_alert": false` per ticker turns the message off. `gex.summary` now carries
  every strike's GEX (`by_strike`). Alert only; nothing feeds scoring.
- **The app no longer scores Best Value; it shows the scan's saved ranking**
  (`bv_saved.py`). Best Value page, Game Plan candidates, Settings "Send to Telegram"
  and the portfolio view read `best_value.rows`; the page's notes show the inputs the
  scan used (`best_value.inputs`). Removed from the score: the app-only boosts (VWAP
  sniper ×1.5, participation ×1.25, cost-profile / 1SD / 0DTE-flow inputs); VWAP and
  participation still show as context. Min Volume on the page only hides rows. Portfolio
  marks come from the scan's quotes. `ui.market._build_best_value_df` removed;
  `tests/test_ui_reads_saved.py` fails if anything under `ui/` scores. Scans saved
  before 2026-10-16 show "no saved ranking" / "inputs not recorded".
  (F-S4-01 and F-S4-01b resolved, piece 2b of 3)
- **Best Value snapshot: top 30 per DTE pool, with full rows**
  (`notify_delivery.serialize_best_value_rows`). Before, one top-30 sorted pool-first
  ("0DTE" < "1DTE+") could save no 1DTE+ row when 30+ 0DTE contracts scored (SPY in
  session). Rows now also carry delta, IV, bid/ask, extrinsic, strategy tag and
  `multipliers` (why each score is what it is), so readers need not re-score.
  (F-S4-01b, piece 2a of 3)
- **Best Value scored once per scan** (`dailyScaner._score_best_value`): one scoring
  with daily bias, market state and news; the archive snapshot and the attribution rows
  are both written from it (before: two scorings, the archive one without news).
  Attribution rows are unchanged (they already had news). Archive / Telegram scores now
  include the news multiplier (×1.2 with, ×0.8 against); snapshots before this date
  do not. The snapshot records its inputs in `best_value.inputs`. Scoring math and
  `config_hash` unchanged. (F-S4-01b, piece 1 of 3)

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
