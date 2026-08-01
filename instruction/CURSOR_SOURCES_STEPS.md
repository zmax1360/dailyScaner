# Sources Migration — Step-by-Step Cursor Tasks

**Seven steps. One Cursor session each. Suite green + committed after every step.**

Actual call-site inventory (verified on `attribution` branch): `yf.Ticker` appears in
**nine** modules, not four —

```
dailyScaner.py:381        chain + history   ← the scan path
data_adapter.py:64,141    chain + daily     ← Streamlit
attribution.py:787        option mid        ← mark_runner
weekly.py:71,87,95        history
volume_analysis.py:110,270  history
cost_distribution.py:100  history
news_service.py:222       news (special — not market data)
```

Steps 1–4 cover the scan and mark paths. Step 5 covers the rest. Do NOT try to convert
all nine at once.

---

## Step 0 — Housekeeping (do this first, 10 min, no Cursor needed)

- [ ] Rotate the Massive API key — it was pasted into a chat and hardcoded in
      `files/testmassiv.py`
- [ ] `git log -p -- files/testmassiv.py | grep -i apikey` — if it was ever committed,
      rotating is mandatory, not optional
- [ ] `echo ".env" >> .gitignore` and confirm
- [ ] **Restart the scheduler** so it holds current code (EOD has never run)

---

## Step 1 — `sources/base.py`: the Protocol only

```
Create a sources/ package containing ONLY the interface. No implementations yet.

sources/__init__.py
sources/base.py

In base.py define:

    CHAIN_COLUMNS = ["side", "strike", "expiry", "dte", "bid", "ask", "last",
                     "volume", "openInterest", "iv", "delta"]

    class MarketDataSource(Protocol):
        name: str
        volume_is_session_scoped: bool   # True = volume resets each session (clean).
                                         # False = may carry prior session (Yahoo).
        def fetch_chain(self, ticker: str, *, max_dte: int) -> pd.DataFrame: ...
        def fetch_history(self, ticker: str, *, interval: str, period: str) -> pd.DataFrame: ...
        def fetch_spot(self, ticker: str) -> float | None: ...
        def fetch_option_mid(self, ticker: str, side: str, strike: float,
                             expiry: str) -> float | None: ...

    def validate_chain(df: pd.DataFrame) -> pd.DataFrame:
        """Assert exact columns in exact order and stable dtypes. Return df."""

Rules to enforce in validate_chain and document in the module docstring:
  - `delta` is ALWAYS a column. Sources that cannot supply it emit NaN.
    Never 0.5, never any default.
  - `iv`, `volume`, `bid`, `ask` likewise: NaN when unavailable, never 0.
  - `side` is uppercase "CALL" / "PUT".
  - `dte` is computed in ET.

`volume_is_session_scoped` exists so the rollover detectors in chain_quality.py can
consult the source instead of assuming Yahoo semantics. Do not wire that up yet — just
declare the attribute.

Tests: test_validate_chain_rejects_missing_column, _rejects_wrong_order,
_accepts_nan_delta, _rejects_zero_substituted_for_missing_iv.

Do NOT touch any existing module in this step.
```

**Verify:** `pytest -q` green. Nothing else changed.

---

## Step 2 — `sources/yahoo.py`: pure move

```
Move the existing yfinance logic into sources/yahoo.py implementing MarketDataSource.
This is a MOVE, not a rewrite.

Preserve EXACTLY, byte-for-byte where possible:
  - `_yf_retry` and its attempts / base_sleep / backoff values
  - the expiry cap and any TTL caching already present
  - the bid/ask fillna(0) behaviour — do NOT "fix" it here; chain_quality owns that
  - the current column set and dtypes

Set:
    name = "yahoo"
    volume_is_session_scoped = False    # Yahoo carries prior-session volume

fetch_chain must end with `return validate_chain(df)`.

Do NOT repoint any caller yet. dailyScaner.py, data_adapter.py and attribution.py keep
working exactly as they do now. This step only adds a parallel implementation.

Tests:
  - test_yahoo_source_satisfies_protocol
  - test_yahoo_chain_passes_validate_chain (use a recorded fixture, not a live call)
  - test_yahoo_emits_nan_delta_not_default

CRITICAL: config_hash must be UNCHANGED after this step. Report it before and after.
If it moves, you changed behaviour — STOP and report what.
```

**Verify:**
```bash
python -c "from config import SCORING; from attribution import config_hash; print(config_hash(SCORING))"
```
Must still be `cdeaf5120620fbce` (or whatever your current value is — record it first).

---

## Step 3 — `sources/fixture.py`: make tests network-free

```
Add sources/fixture.py implementing MarketDataSource by reading the archives in
tests/golden/ (AAPL_20260727_160721.txt, AAPL_20260728_093102.json,
AAPL_20260728_095049.json, and any recorded snapshots).

    name = "fixture"
    volume_is_session_scoped = False

Constructor takes an explicit archive path or dict — no filesystem globbing, no implicit
"latest file" behaviour. A test must be able to pin exactly which snapshot it gets.

Then convert existing tests that currently reach Yahoo to use this source instead.

Tests:
  - test_fixture_source_serves_recorded_chain
  - test_fixture_source_is_deterministic (same input -> identical frame twice)
  - test_no_test_reaches_network: assert no test imports yfinance or requests directly
```

**Verify:** `pytest -q` with network disabled should still pass.

---

## Step 4 — Repoint the scan and mark paths

```
Convert the three call sites that matter for data collection:

  1. dailyScaner.py:381  (fetch_data)
  2. data_adapter.py:64,141  (fetch_full_chain, fetch_daily_ohlc)
  3. attribution.py:787  (fetch_option_mid — used by mark_runner)

Each must accept a MarketDataSource. Pass it in explicitly as a parameter with a
default constructed at the entry point. Do NOT use a module-level global, a singleton,
or an import-time instantiation.

Add to config.py:
    "market_data_source": "yahoo"

and a single factory `sources.get_source(name)` that the CLI entry points and the
scheduler use. One factory, one place.

Leave weekly.py, volume_analysis.py, cost_distribution.py and news_service.py on direct
yfinance for now — they are Step 5.

Rewrite the architecture-guard test: the current one greps app.py source text for
"yfinance" and passes while being false, because app.py imports dailyScaner which
imports yfinance. Replace it with an IMPORT GRAPH check using ast or importlib that
asserts yfinance is reachable only from sources.yahoo (allowing the Step-5 modules as
explicit, listed exceptions until they are converted).

CRITICAL: config_hash must be UNCHANGED except for the one new "market_data_source" key.
Golden master must still pass. Report the before/after hash and explain the delta.
```

**Verify:** run a live scan, confirm rows land in `attribution.db` as before.

---

## Step 5 — Convert the remaining five modules

```
Convert weekly.py, volume_analysis.py, cost_distribution.py to take a
MarketDataSource for their history calls.

news_service.py:222 uses yf.Ticker(ticker).news — that is NOT market data. Either add
a separate `fetch_news` to a distinct NewsSource protocol, or leave it on yfinance with
an explicit comment saying why it is exempt. Do not force it into MarketDataSource.

Then remove the exception list from the import-graph test so it asserts strictly.
```

---

## Step 6 — `sources/massive.py`

```
Add sources/massive.py implementing MarketDataSource against api.massive.com.
Keep sources/yahoo.py fully working. Default source stays "yahoo".

    name = "massive"
    volume_is_session_scoped = True     # day.volume is today's only

## Chain endpoint

    GET /v3/snapshot/options/{underlyingAsset}

Verified response shape (Options Starter, AFTER HOURS):

    {"results": [{
      "day": {"volume": 150, "close": 137.81, "previous_close": 133.99,
              "vwap": 135.9729, "last_updated": 1785297600000000000},
      "details": {"contract_type": "call", "expiration_date": "2026-07-29",
                  "strike_price": 205, "ticker": "O:AAPL260729C00205000"},
      "greeks": {},
      "open_interest": 8
    }], "status": "OK", "next_url": "..."}

## Requirements

1. Auth from env var MASSIVE_API_KEY. Never hardcode, never log, never write to an
   archive or committed file.

2. Server-side expiry filter: `expiration_date.lte` = today+max_dte in ET, `limit=250`.
   Follow next_url only while results stay in window. Cap at 5 pages, log if hit.

3. Mapping:
       side          <- details.contract_type.upper()
       strike        <- details.strike_price
       expiry        <- details.expiration_date
       dte           <- ET days to expiry
       volume        <- day.volume
       openInterest  <- open_interest
       last          <- day.close
       bid / ask     <- last_quote.bid / .ask, NaN if last_quote absent
       iv            <- implied_volatility, NaN if absent
       delta         <- greeks.delta, NaN if greeks == {} or absent

   Every "absent" is NaN. Never 0, never a default.

4. TIMESTAMPS ARE UNIX NANOSECONDS UTC — note the magnitude 1785297600000000000, which
   is 1e9 times a seconds-epoch value. Convert to ET explicitly using the existing ET
   helper. Do not add a second time helper. This is the same class of unit bug that
   made 0DTE delta a step function; be deliberate.

5. Greeks fallback: verify during RTH (09:35-15:55 ET) whether greeks / last_quote /
   implied_volatility populate, and REPORT what you observe. If greeks are unavailable
   on this plan even during RTH, fall back to greeks.bs_delta — but ONLY when a usable
   iv is present. Do not invent an iv to make delta computable.

6. History: Massive uses aggregates, not Ticker.history —
   GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
   Map to the same DataFrame shape yahoo.fetch_history returns.

7. Retry/backoff mirroring _yf_retry, plus:
       429 -> retry with backoff
       403 -> FAIL LOUDLY with "plan does not cover this endpoint". Do NOT retry.

Tests — all against a recorded fixture, no live calls:
  - save a real RTH response to tests/golden/massive_aapl_snapshot.json
  - test_massive_maps_to_chain_contract
  - test_empty_greeks_yields_nan_delta
  - test_missing_last_quote_yields_nan_bid_ask
  - test_nanosecond_timestamp_converted_to_et
  - test_403_raises_with_plan_message_and_does_not_retry
  - test_429_retries
  - test_expiry_filter_sent_server_side
  - test_api_key_absent_from_logs_and_archive
```

---

## Step 7 — A/B comparison (the payoff)

```
Add tools/compare_sources.py. Fetch the same ticker from BOTH sources at the same
moment and report per contract:

  - volume: yahoo vs massive, absolute and % difference
  - count where yahoo_volume >= massive_volume * 5
      ^ this is the fingerprint of Yahoo serving a prior-session cumulative total
  - bid/ask present in one source and absent in the other
  - iv difference
  - contracts present in one and missing from the other
  - delta present in one and absent in the other

Run at 09:35, 10:30 and 15:30 ET and report all three.

Then update chain_quality.py to consult source.volume_is_session_scoped:
  - source is session-scoped -> rollover detectors go DORMANT (log that they are
    skipped and why)
  - not session-scoped -> current behaviour

Do NOT delete chain_quality.py, greeks.py, or the rollover detectors. Dormant is not
wrong. Leave them in place until the A/B data proves the new feed does not need them.
```

**This is the number that matters.** You built staleness detection without ever
measuring the staleness. The `yahoo_vol >= massive_vol * 5` count tells you directly how
bad it was — and therefore whether the 605 runs already in `attribution.db` are
salvageable or should be written off.

---

## Standing constraints — paste with every step

```
- Do NOT modify scoring math in best_value.py or strategy_engine.py
- Do NOT modify the attribution schema, portfolio_store, or journal writes
- Do NOT delete chain_quality.py, greeks.py, or the rollover detectors
- Do NOT substitute a default for missing data anywhere. NaN and exclude.
- Do NOT weaken, skip, or delete an existing test. Four xfail(strict=True) must remain:
  F-02, F-05, the [0,1] range defect, and the golden pin. If any changes status, STOP.
- Do NOT commit an API key. Check `git diff` before committing.
- Report actual command output, not a summary. If you did not run it, say so.
```

---

## Checkpoint after every step

```bash
python -m pytest tests/ -q          # green, 4 xfailed
python -c "from config import SCORING; from attribution import config_hash; print(config_hash(SCORING))"
git log --oneline -1                # confirm it actually committed
```

That last line matters: twice this week Cursor reported green results against an
uncommitted working tree.

**config_hash must not move in Steps 1–5.** Those are refactors. If it moves, behaviour
changed and you have forked your sample by accident.
