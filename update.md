# Options Scanner Dashboard — Complete Reference

**File:** `app.py`  
**Run:** `streamlit run app.py`  
**Last updated:** 2026-07-17  

---

## Architecture

The dashboard is a **pure display layer**. It never recomputes market signals.
Every analytical value (direction, RSI, MACD, magnets, P/C ratio, Greeks) comes from one of two sources:

| Source | Used for |
|---|---|
| `archive/{TICKER}_*.json` | All live and historical display data |
| Black-Scholes formula (stdlib `math` only) | Theta & Gamma computed inside the UI from archived IV |

**Hard rules enforced in code:**
- `grep "yfinance" app.py` returns nothing — only `data_adapter.py` touches yfinance
- No indicator computation inside `app.py`
- All timestamps displayed in `America/New_York` (ET)
- Scanner subprocess always runs in a **background daemon thread** — the event loop is never blocked
- Deltas are colour-coded everywhere: green = positive change, red = negative change

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit dashboard (this document) |
| `dailyScaner.py` | Core scanner engine — accepts `sys.argv[1]` ticker, default AAPL |
| `data_adapter.py` | Only file that calls yfinance directly |
| `snapshot_store.py` | File-based persistence for `flow_snapshot.json` and `gate_history.json` |
| `spread_gate.py` | `evaluate_spread_gate()` — hard NO-TRADE gate using optionlab |
| `weekly.py` / `weeklyv1.py` | Weekly scanner with 5-point checklist and spread gate integration |
| `archive/{TICKER}_*.json` | Daily scanner output files (30 top calls + 30 top puts each) |
| `archive_weekly/{TICKER}_*.json` | Weekly scanner output files |
| `tests/test_dashboard_adapters.py` | 17 unit tests for adapters and source-level checks |

---

## Sidebar (always visible)

### Focus ticker
A `st.selectbox` populated automatically from `_discover_tickers()`.
Tickers are detected by globbing `archive/*.json` filenames — only symbols that already have at least one archive file appear. No stored list, no manual config.

Switching the focus ticker immediately changes the data shown in Tab 1 and Tab 2.

### Daily scan button
`🚀 Run Scan for {ticker}` — executes `python dailyScaner.py {TICKER}` as a subprocess.
A spinner shows while running. On completion:
- **Success** → green banner + scanner output in a collapsible expander
- **Failure** → red banner + output expanded for debugging

### Auto-scan watcher
A `@st.fragment(run_every=timedelta(minutes=5))` fragment that fires every 5 minutes.

- **Market closed** (before 09:30 or after 16:00 ET, weekends) → shows `⏸ Auto-scan paused` and exits
- **Market open** → iterates over every discovered ticker and launches a background daemon thread for each one that is not already running and has passed its 4.5-minute cooldown
- Status captions per ticker: `⏳ AAPL (47s)…` / `✅ TSLA: 14:32 ET` / `⚠ NVDA: 14:35 ET` (failure)
- The event loop is never blocked; `subprocess.run()` executes inside `threading.Thread(daemon=True)`

### Flow filters
Controls that affect the Options Flow tab:

| Control | Effect |
|---|---|
| **Min DTE** | Minimum days-to-expiry filter |
| **Top N contracts/side** | How many top calls and puts to show in the Magnets panel (1–30, default 5). Archive stores 30 so no re-fetch is needed |
| **Sort by** | Volume / Premium $ / Strike |

### Last archive
Shows the timestamp and spot price of the most recently saved scan for the focus ticker.

### Market closed banner
When `market_is_open()` returns False a red `🔴 MARKET CLOSED — DATA IS END-OF-DAY` error banner appears at the top of every tab. Direction labels gain a `(historical)` suffix wherever displayed.

---

## Tab 1 — 📈 Options Flow

Loads the **two most recent** `archive/{ticker}_*.json` files (current + previous run for deltas). Everything is read-only from the archive.

### Direction banner
Full-width coloured header showing the scanner's direction verdict:
- `▲ BULLISH` in green, `▼ BEARISH` in red, `─ NEUTRAL` in grey
- Spot price, P/C ratio, and P/C bias label (`BULLISH SKEW` / `BEARISH SKEW` / `NEUTRAL`)
- When market is closed: direction suffixed with `(historical)`

### Four metric tiles
Spot · Call Volume · Put Volume · P/C Ratio — pulled verbatim from the archive.

### 📊 Multi-Timeframe Detail (collapsible, open by default)
Table of six timeframes: 5M · 10M · 15M · 1H · 4H · 1D

| Column | Content |
|---|---|
| TF | Timeframe label |
| RSI | RSI value with label (OVERBOUGHT / BULLISH / NEUTRAL / BEARISH / OVERSOLD) |
| ΔRSI | RSI change vs previous run — 🟢 green if rose, 🔴 red if fell |
| MACD hist | Raw histogram value |
| ΔMACD | Histogram change vs previous run — 🟢/🔴 |
| Vol Spike | Volume spike multiplier |
| Support / Resist | Key levels from the scanner |

Delta columns are hidden if there is no previous archive run.

### The Magnets — Top N Calls / Top N Puts
Two equal-width columns side by side. N is controlled by the sidebar "Top N" input.

Each table row is one contract:

| Column | Content |
|---|---|
| STRIKE | Strike price |
| PRICE | Last traded price |
| VOLUME | Total volume (formatted) |
| OI | Open interest |
| VOL/OI | Volume-to-OI ratio — values ≥ 2.0× show 🔥 heatmap colour |

These are the strikes with the greatest institutional activity and act as likely price magnets.

### Volume by Expiry — P/C term structure

**Table** — every expiry found across the top-30 calls and puts:

| Column | Content |
|---|---|
| EXPIRY | Expiry date string |
| DTE | Days to expiry |
| CALL VOL | Total call volume for this expiry |
| PUT VOL | Total put volume |
| P/C | Put/call ratio (n/a if either side is zero) |
| BIAS | ▲ BULLISH / ▲ MILD BULLISH / ─ NEUTRAL / ▼ MILD BEARISH / ▼ BEARISH |
| NOTABLE | ◄ notable if \|P/C − overall_P/C\| > 0.25; ⚠ data gap if either side is zero |
| CALL Δ / PUT Δ | Volume change vs previous run — 🟢 green if higher, 🔴 red if lower |

**Altair bar chart** — P/C ratio per expiry:
- Blue bars: P/C < 1 (call-heavy)
- Red bars: P/C ≥ 1 (put-heavy)
- Dashed grey reference line at y = 1.0
- Expiries with zero volume on either side are excluded from bars and shown as `⚠` text markers
- Caption: `< 1 call-heavy · > 1 put-heavy [· ⚠ = data gap: ...]`

### 📈 Changes vs last run (collapsible, open by default)
- Spot delta with absolute and percentage change
- P/C ratio delta
- RSI shifts per timeframe (prev → curr with signed delta)
- Magnet strike changes flagged with `← STRIKE CHANGE`

### ⏰ Opening Range Breakout (collapsible, open by default)
Table for the 5M and 15M opening range windows (09:30 ET):

| Column | Content |
|---|---|
| TF | 5M or 15M |
| Open time | Window start time |
| Open / High / Low | OHLC of the range |
| Range | Range size and percentage |
| Current | Current spot price |
| Bias | ▲ BULLISH BREAKOUT / ▼ BEARISH BREAKDOWN / ─ inside range — colour-coded |

---

## Tab 2 — 📋 Scanner Archive

Loads the **two most recent** daily archive files for the focus ticker (current + previous for deltas).

### Volume by Expiry (interactive)
Same aggregated expiry table as Tab 1 but rendered with `on_select="rerun"` and `selection_mode="single-row"`.

Clicking any row triggers the contract drill-down below the table.

Caption: "Click a row to see the contracts for that expiry."

### Contract drill-down (appears on row click)
Two side-by-side columns — CALLS left, PUTS right — filtered to the selected expiry.

| Column | Content |
|---|---|
| Strike | Strike price |
| Price | Last price |
| ΔPrice | Price change vs previous run — 🟢/🔴 |
| Volume | Total volume |
| ΔVol | Volume change vs previous run — 🟢/🔴 |
| OI | Open interest |
| VOL/OI | Volume-to-OI ratio |
| IV | Implied volatility |

Contracts that did not exist in the previous run show `new` instead of a delta.
Delta columns are omitted entirely if no previous archive is available.

### Theta & Gamma by Strike

**Table** — all top-30 calls and puts with Black-Scholes greeks:

| Column | Content |
|---|---|
| Side | CALL (green) or PUT (red) |
| Strike | Strike price |
| Expiry / DTE | Expiry date and days remaining |
| Price | Last traded price |
| IV | Implied volatility from archive |
| Gamma | dΔ/dS — rate of delta change per $1 spot move |
| ΔGamma | Gamma change vs previous run — 🟢/🔴 |
| Theta/d | Option value lost per calendar day (negative = cost) |
| ΔTheta | Theta change vs previous run — 🟢/🔴 |

0DTE contracts show `—` (Greeks are undefined at expiry).  
Delta columns appear only when a previous archive exists.  
Previous greeks are recomputed using the previous run's own spot and IV so the Δ reflects real exposure change, not just time passage.

**Two Altair bar charts** (side by side):
- **Gamma by Strike** — grouped bars (green = CALL, red = PUT), x-offset so both sides are visible at the same strike
- **Theta by Strike** — same layout, y-axis is $/day

Caption: `Gamma: rate of delta change per $1 move in spot. Theta: option value lost per calendar day. Computed from Black-Scholes using archived IV — not live quotes.`

---

## Tab 3 — 🔬 Spread Gate

A form to evaluate a specific bull call spread against hard criteria.

### Input form

| Field | Description |
|---|---|
| Spot | Current spot price (pre-filled from latest archive) |
| Long strike | Long call strike |
| Short strike | Short call strike |
| Long premium | Long leg mid price |
| Short premium | Short leg mid price |
| Implied volatility | Long leg IV |
| Expiration date | Contract expiry |
| Exit date | Hard stop date (day before earnings if earnings ≤ expiry) |

On submit: calls `evaluate_spread_gate()` from `spread_gate.py` verbatim. No UI logic scores or approves trades.

### Verdict display
- `TRADE` in green or `NO-TRADE` in red
- Probability of Profit (PoP)
- Expected Value per contract (EV)
- Each reason listed individually

### Gate history
Every evaluation is persisted to `gate_history.json` (last 20 kept via `snapshot_store`). A table below the form shows previous evaluations with verdict, PoP, EV, and timestamp — enabling comparison of different setups over time.

---

## Tab 4 — 🗂 Tickers

Manage which tickers are tracked. No stored config file — tickers appear automatically once they have archive data.

### Run scanner for a new ticker
- Text input for any valid symbol (e.g. `TSLA`, `NVDA`, `SPY`)
- `🚀 Run scan` button executes `python dailyScaner.py {TICKER}`
- On success the ticker immediately appears in the sidebar focus-ticker selector
- Scanner output shown in a collapsible expander

### Known tickers table
All tickers with at least one daily archive file:

| Column | Content |
|---|---|
| Ticker | Symbol |
| Last scan | Timestamp of most recent archive file (ET) |
| Spot | Spot price at last scan |
| Direction | ▲ BULLISH / ▼ BEARISH / ─ NEUTRAL — colour-coded |
| Auto-scan | Current background thread status for this ticker |
| # files | Number of archive files on disk |

### Rescan picker
Selectbox + `🔄 Rescan {ticker}` button to re-run the scanner for any existing ticker on demand.

---

## Data flow summary

```
dailyScaner.py {TICKER}
      │
      ▼
archive/{TICKER}_YYYYMMDD_HHMM.json
      │
      ├── Tab 1: read 2 most recent → display + delta columns
      ├── Tab 2: read 2 most recent → expiry table + drill-down + BS greeks
      ├── Tab 3: latest spot pre-fills form
      └── Tab 4: scan filenames → known ticker list
```

```
spread_gate.py → evaluate_spread_gate() → Tab 3 verdict + gate_history.json
```

---

## Running the project

```bash
# Install dependencies
pip install streamlit pandas altair optionlab yfinance

# Start dashboard
streamlit run app.py

# Run scanner manually for a ticker
python dailyScaner.py AAPL
python dailyScaner.py TSLA

# Run tests
pytest tests/test_dashboard_adapters.py -v
```

---

## Design invariants

| Invariant | Enforcement |
|---|---|
| No yfinance in app.py | `grep "yfinance" app.py` returns nothing; test `test_app_no_yfinance_import` asserts this |
| No UI-computed signals | Direction, RSI, MACD, magnets all read verbatim from JSON |
| Non-blocking scanner | `subprocess.run()` inside `threading.Thread(daemon=True)` |
| All times in ET | Every `datetime.fromisoformat()` followed by `.astimezone(ET)` |
| Market-closed banner on every tab | `_market_banner()` called inside each `with tab:` block |
| Delta colour rule | Green = positive change, red = negative — RSI, MACD, volume, price, Greeks |
| 0DTE never recommended | Greeks show `—` for 0DTE; no highlight or recommendation logic |
| Gate verdict verbatim | Tab 3 displays the raw return value of `evaluate_spread_gate()` unchanged |
