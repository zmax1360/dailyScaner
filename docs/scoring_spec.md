# Scoring spec

Behavior of the scoring pipeline as implemented on HEAD `ce1bf691f82c5f84bff93e948dfaeb77c34238e5`.
`config_hash(SCORING)` at write time: `243ecda68cfc8618`.

This document does not describe intended behavior.

---

## 1. ENTRY POINTS

### `calculate_best_value`

Signature (`best_value.py:182-200`):

```
calculate_best_value(
    df, spot_price, min_volume=None, daily_bias=None, market_state=None,
    news_bias=None, vwap_state=None, now_et=None, profited_shares_pct=None,
    *, upper_1sd=None, lower_1sd=None, optimal_strategy=None,
    has_catalyst=False, spot_below_support=False, odte_info=None, pov_info=None,
) -> pd.DataFrame
```

It copies `df`, writes score columns onto the same index, and returns that frame (`best_value.py:248-262`, `:695-716`).

Callers that invoke it **directly**:

| caller | file:line |
|---|---|
| `build_best_value_df` (production wrapper) | `best_value.py:795` |
| tests (`test_best_value_engine`, `test_attribution`, `test_golden_master`, `test_scoring_pools`, others) | grep `from best_value import calculate_best_value` |

### `build_best_value_df`

Signature (`best_value.py:719-743`):

```
build_best_value_df(
    vol_curr, spot, vol_prev, min_volume=None, daily_bias=None,
    market_state=None, news_bias=None, vwap_state=None, now_et=None,
    profited_shares_pct=None, *, eod_vol_lookup=None,
    volume_is_session_scoped=False, current_source="yahoo",
    prev_archive_source=None, eod_archive_source=None,
    upper_1sd=None, lower_1sd=None, optimal_strategy=None,
    has_catalyst=False, spot_below_support=False, odte_info=None, pov_info=None,
) -> pd.DataFrame
```

It flattens `vol_curr["top_calls"]` / `top_puts` into rows (`best_value.py:749-780`), runs `attach_dvol` (`:785`), then `calculate_best_value` (`:795`).

Production callers of `build_best_value_df`:

| caller | file:line |
|---|---|
| `dailyScaner._log_scan_attribution` → then `attribution.log_run(scored_df=bv_df)` | `dailyScaner.py:108`, `:134` |
| `dailyScaner.run` (archive Best Value snapshot) | `dailyScaner.py:1284` |
| `app._build_best_value_df` | `app.py:1638` |
| `app._render_best_value_panel` via `_build_best_value_df` | `app.py:2421` |
| Telegram HTML builder via `_build_best_value_df` | `app.py:615` |
| portfolio/live rebuild via `build_best_value_df` | `app.py:1868` |

`telegram_bot.py` has no `build_best_value_df` / `calculate_best_value` import (grep). The module docstring at `best_value.py:5` states both `app.py` and `telegram_bot.py` import from here. See F-S2-01.

---

## 2. UNIVERSE FILTERING

Rows that fail a gate stay in the returned frame with `Value_Score` NaN (`best_value.py:249`). They are removed from the working subset `work` only.

Execution order inside `calculate_best_value`:

| # | condition | config key | file:line |
|---|---|---|---|
| 1 | `volume >= mv` AND `last > min_last` | `min_volume` (or caller `min_volume` override), `min_last` | `best_value.py:229-230`, `:264` |
| 2a | If an expiry column exists: drop `expiry.date < today_et`. After 16:15 ET also drop `expiry.date == today_et`. Before 16:15, same-day expiry is kept. | none (clock) | `best_value.py:274-285` |
| 2b | Else if `dte` / `DTE` exists: after 16:15 keep `dte > 0`; before keep `dte >= 0` | none | `best_value.py:287-296` |
| 3 | Empty `work` → return the all-NaN frame | — | `best_value.py:266-267`, `:298-299` |

16:15 rule (`best_value.py:275`):

```
after_close = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 15)
```

`now_et` default is `datetime.now(US/Eastern)` (`best_value.py:269-272`). Naive `now_et` is localized as ET (`:271-272`).

Not a `calculate_best_value` drop, but applied **before** scoring when the wrapper is used:

- `attach_dvol` does not drop rows (`best_value.py:44-179`).
- `build_best_value_df` uses `int(c.get("dte") or 0)` (`best_value.py:753`), so a missing `dte` becomes `0` (0DTE pool) before scoring.

Quality-gate keys `min_iv_usable`, `quality_top_n`, `max_unusable_frac` (`config.py:55-57`) are not used as row drops in `calculate_best_value`. `min_iv_usable` is used inside `bs_delta` (`greeks.py:113-115`) and 1SD IV selection (`best_value.py:512`).

---

## 3. FEATURE COMPUTATION

### `delta`

In `calculate_best_value`, every surviving `work` row is overwritten (`best_value.py:307-326`):

```
d = bs_delta(side, float(spot_price), float(strike), effective_dte_days(dte, expiry=exp, now_et=now_et), float(iv or 0), r=r_free)
```

`r_free = SCORING["risk_free_rate"]` (`best_value.py:305`, `config.py:53`).

`bs_delta` (`greeks.py:88-142`):

```
T = dte_days / 365
d1 = (ln(S/K) + (r + iv^2/2) * T) / (iv * sqrt(T))
CALL → N(d1)    PUT → N(d1) - 1
```

Returns `None` (stored as NaN) when any of: non-finite inputs; `iv < min_iv_usable`; `dte_days <= 0`; `S<=0` or `K<=0`; unknown side (`greeks.py:110-115`, `:126-127`, `:142`).

`effective_dte_days` (`greeks.py:36-85`): if `dte > 0`, return that number of days; if expiry is today ET, return `max(seconds_to_16:00, 60) / 86400`; else `0.0` (then `bs_delta` returns None).

`build_best_value_df` also precomputes `delta` the same way (`best_value.py:757-779`). `calculate_best_value` overwrites it.

**NaN policy:** NaN delta → `_lev` stays NaN (`best_value.py:332-338`). `has_delta = work["delta"].notna()` (`:333`). Those rows are not in `eligible` (`:391`). They are not min-maxed and keep `Value_Score` NaN.

### `_lev`

```
_lev = abs(delta) * spot_price / last.replace(0, NaN)
```

(`best_value.py:335-338`). Only written where `delta` is not NaN. `last == 0` → `_lev` NaN.

### `_flow`

```
voi_raw = volume / openInterest.clip(lower=1)
if stale_volume: voi_raw = NaN
d_vol = dVol, with NaN filled to 1.0 except where stale_volume is True
_flow = (voi_raw * d_vol).clip(lower=0)
```

(`best_value.py:340-357`).

If column `dVol` is absent, `d_vol = volume` (`best_value.py:354-355`).

**`dVol` NaN and stale_volume:**

| case | `dVol` | `stale_volume` | fill | `_flow` |
|---|---|---|---|---|
| new entrant (not in prev lookup) | NaN (`best_value.py:146`, `:157`) | False | filled to `1.0` (`:350-353`) | `voi_raw * 1.0` |
| decrease vs prior scan | NaN (`:150-154`) | False | filled to `1.0` | `voi_raw * 1.0` (`dvol_suspect=True`) |
| EOD-stale (`is_volume_stale_vs_eod`: today >= `stale_volume_ratio` * prior EOD, `chain_quality.py:549-570`, ratio `config.py:59` = 0.95) | NaN (`best_value.py:159-168`) | True | **not** filled (`:349-351`) | NaN (excluded from ranking, `:357`, `:390`) |
| matched and curr >= prev | `curr - prev` (`:156`) | False | unused | `voi_raw * signed dVol`, clipped at 0 |
| session-scoped source / source mismatch | column of NaN (`:91-96`, `:115-116`) | False | fillna 1.0 if `dVol` column exists | `voi_raw * 1.0` |

`dVol` is **not** passed through `abs()` (`best_value.py:346-347`). Negative `dVol` → `_flow` clipped to 0 (`:356`).

**Ranking exclusion:** `eligible = has_delta & _flow.notna() & pool.notna()` (`best_value.py:390-391`). NaN `_flow` (stale) is excluded. Filled-`1.0` new-entrant `_flow` is included.

---

## 4. POOLING

`scoring_pool(dte)` (`scoring_pool.py:29-54`):

| `dte` | pool |
|---|---|
| `== 0` | `"0DTE"` |
| `>= 1` | `"1DTE+"` |
| `None`, NaN, unparseable, `< 0` | `None` |

`calculate_best_value` writes `work["pool"]` from that function (`best_value.py:365-385`). Missing dte column → every pool is `None` and a warning is logged (`:370-372`). Null/invalid dte is not placed in `"1DTE+"`.

`min_pool_size` (`config.py:18`, default `DEFAULT_MIN_POOL_SIZE = 5` at `scoring_pool.py:19`): for each of `"0DTE"` and `"1DTE+"`, if `eligible` count `< min_pool_size`, that pool is skipped (`best_value.py:393-404`). Those rows keep `Value_Score` / `_nlev` / `_nflow` / `_base_score` as NaN. They are **not** merged into the other pool.

Under-size pool rows do **not** receive a numeric score.

---

## 5. NORMALISATION

Verbatim (`best_value.py:359-363`):

```
def _minmax(s: pd.Series) -> pd.Series:
    mn, mx = s.min(), s.max()
    if mx <= mn:
        return pd.Series(0.5, index=s.index)
    return (s - mn) / (mx - mn)
```

- When `max == min` (including a one-row series, or every value identical): every element is `0.5`.
- Reference is **per scan, per pool**: `_minmax` is called on `work.loc[in_pool, "_lev"]` and `"_flow"` independently (`best_value.py:405-411`). Not rolling. Not an absolute scale.
- When every value in the pool is small: the same formula applies. A pool of all-small distinct values still spans `[0, 1]` after min-max. A pool of identical small values maps to `0.5`.

---

## 6. BASE SCORE

Weights (`config.py:20-21`, read at `best_value.py:227-228`):

```
w_lev = 0.4
w_flow = 0.6
```

Blend (`best_value.py:412-415`):

```
Value_Score = _nlev * w_lev + _nflow * w_flow
```

Then `_base_score = Value_Score.copy()` **before** multipliers (`best_value.py:418-419`). After multipliers, `Value_Score` is `round(4)` (`:676`). `_base_score` is not rounded at that line.

---

## 7. MULTIPLIER STACK

`_apply` (`best_value.py:459-464`):

```
work.loc[mask_s, "Value_Score"] *= value
mults[idx][key] = float(value)
```

Every `work` index starts with `{"_base": 1.0}` (`best_value.py:455-457`). Multiple `_apply` calls on the same row multiply. The result is a **product**.

Order of `_apply` call sites:

| # | condition | mask | config key | value (`config.py`) | file:line |
|---|---|---|---|---|---|
| 1 | `daily_bias == "HEAVY BEARISH"` | `side == CALL` | `mult_heavy_bias_against` | 0.5 | `best_value.py:479-480` |
| 2 | `daily_bias == "HEAVY BULLISH"` | `side == PUT` | `mult_heavy_bias_against` | 0.5 | `:481-482` |
| 3 | `market_state == "BEARISH DRAG"` | `side == CALL` | `mult_macro_against` | 0.3 | `:485-486` |
| 4 | `market_state == "BULLISH TAILWIND"` | `side == PUT` | `mult_macro_against` | 0.3 | `:487-488` |
| 5 | `news_bias == "BEARISH"` | CALL / PUT | `mult_news_against` / `mult_news_with` | 0.8 / 1.2 | `:491-493` |
| 6 | `news_bias == "BULLISH"` | CALL / PUT | `mult_news_with` / `mult_news_against` | 1.2 / 0.8 | `:494-496` |
| 7 | `vwap_state == "RECLAIMED UP"` and `daily_bias == "HEAVY BULLISH"` | CALL | `mult_vwap_sniper` | 1.5 | `:500-501` |
| 8 | `vwap_state == "RECLAIMED DOWN"` and `daily_bias == "HEAVY BEARISH"` | PUT | `mult_vwap_sniper` | 1.5 | `:502-503` |
| 9 | `upper_1sd` set; CALL strike `> u1` | `call_lottery` | `mult_outside_1sd` | 0.2 | `:570-572` |
| 10 | `lower_1sd` set; PUT strike `< l1` | `put_lottery` | `mult_outside_1sd` | 0.2 | `:575-577` |
| 11 | `strategy_outlook == 2` and `u1` set | CALL, `spot < K <= u1` | `mult_plus2_boost` | 1.5 | `:585-593` |
| 12 | outlook `== 1` | CALL `K > spot` | `mult_plus1_otm` | 0.5 | `:597-599` |
| 13 | outlook `== 1` | CALL `K <= spot` | `mult_plus1_itm` | 1.3 | `:598-600` |
| 14 | outlook `== 0` | CALL or PUT | `mult_zero_outlook` | 0.3 | `:605-607` |
| 15 | outlook `== -2` and `l1` set | PUT, `l1 <= K < spot` | `mult_minus2_boost` | 1.5 | `:610-618` |
| 16 | outlook `== -1` | PUT `K < spot` | `mult_minus1_otm` | 0.5 | `:622-624` |
| 17 | outlook `== -1` | PUT `K >= spot` | `mult_minus1_itm` | 1.3 | `:623-625` |
| 18 | `is_straddle_strategy` | strikes in `[l1,u1]` or \|K-S\|/S ≤ 0.03 | `mult_straddle_atm` | 1.3 | `:630-636` |
| 19 | `odte_info["boost_side"]` set | `apply_0dte_boost_mask`: DTE==0, \|K-S\|/S ≤ `ATM_PCT` 0.015, side matches (`zero_dte_gex.py:26`, `:215-229`) | `mult_0dte_boost` | 1.20 | `best_value.py:642-653` |
| 20 | `pov_info["urgency"]` | all CALL | `mult_pov_urgency` | 1.25 | `:663-666` |

`recommend_strategy` (`strategy_engine.py:95-145`) supplies `optimal_strategy` when the caller left it empty (`best_value.py:516-528`). Outlook integers come from `strategy_outlook` (`strategy_engine.py:54-76`). `is_unknown_strategy` (`:87-92`) skips the outlook branches (`best_value.py:582-584`).

`+2` and `+1` (or `-2` and `-1`) are `elif` — they do not stack with each other. Bias / macro / news / VWAP / 1SD / 0DTE / POV **can** apply to the same row together with one outlook branch.

`is_blue_sky_breakout` (`cost_distribution.py:298-310`) changes the BEST VALUE **Status** string (`best_value.py:691-693`), not `Value_Score`.

---

## 8. NUMERICAL VERIFICATION

Read-only: `sqlite3 file:data/attribution.db?mode=ro`.

Eligible non-control rows on `config_hash = 243ecda68cfc8618` with non-null `score`, `base_score`, `nlev`, `nflow`, `multipliers`: **156525**.

Every one of those 156525 `multipliers` JSON objects contains `"_base": 1.0`. The product loop includes that value (`×1.0`).

Five rows, lowest `flag_id` in that set:

| flag_id | nlev | nflow | 0.4 nlev + 0.6 nflow | stored base_score | res_a | product(mults) | base × product | stored score | res_b |
|---|---|---|---|---|---|---|---|---|---|
| 47578 | 0.30833223305205587 | 1.0 | 0.7233328932208223 | 0.7233328932208223 | 0 | `_base` 1.0 × `zero_outlook` 0.3 = 0.3 | 0.21699986796624668 | 0.217 | −1.32e-7 |
| 47579 | 1.0 | 0.12590669409478578 | 0.4755440164568715 | 0.4755440164568715 | 0 | 0.3 | 0.14266320493706144 | 0.1427 | −3.68e-5 |
| 47580 | 0.9756805303113052 | 0.004618230961324343 | 0.39304315070131673 | 0.39304315070131673 | 0 | 0.3 | 0.11791294521039501 | 0.1179 | +1.29e-5 |
| 47581 | 0.8818327624084602 | 0.007888562909328097 | 0.357466242708981 | 0.357466242708981 | 0 | 0.3 | 0.10723987281269429 | 0.1072 | +3.99e-5 |
| 47582 | 0.7566524923322522 | 0.003102250110694795 | 0.30452234699931774 | 0.30452234699931774 | 0 | 0.3 | 0.09135670409979532 | 0.0914 | −4.33e-5 |

(a) matches at 0 residual.

(b) residuals are the `Value_Score.round(4)` persist (`best_value.py:676`) versus an unrounded product. Magnitude ≤ 5e-5 (one unit in the 4th decimal). Logged as F-S2-02, not treated as a broken blend.

`_base: 1.0` is present and is included in the product.

---

## 9. RANK ASSIGNMENT

Inside `calculate_best_value` (`best_value.py:678-693`):

- For each of `0DTE` and `1DTE+`, take rows with that `pool` and non-null `Value_Score`.
- `sort_values(ascending=False)` on `Value_Score`.
- Enumerate that index order as `_rank` 1..n.
- Row at `pool_scored.index[0]` gets `Status = "⭐ BEST VALUE · {pool}"` (plus blue-sky suffix if `is_blue_sky_breakout`).

Rank is **within-pool**. A pool that was not scored has no ranks.

Tie break: pandas `Series.sort_values` default `kind` is quicksort (unstable). Equal scores do not have a documented stable order.

`is_control` rows are **not** ranked here. Controls are appended in `attribution.log_run` with `score=None`, `rank=None` (`attribution.py:838-844`). `log_run` filters to `Value_Score.notna()` before writing scored flags (`attribution.py:680`) and does not put controls into that sort.

`log_run` fallback re-rank (`attribution.py:683-691`) runs only when `_rank` is missing or all-NA. It groups by `pool` and sorts `Value_Score` descending. It does not include controls.

---

## 10. DISPLAY-ONLY LAYERS

Attribution writes happen in `dailyScaner._log_scan_attribution` (`dailyScaner.py:69-136`): `build_best_value_df` → `log_run(scored_df=bv_df)`. That path does not call `filter_ranked_display`, `greeks_display_columns`, or `format_exit_by_cell`. `app.py` does not call `log_run` (grep).

### Delta band filter

`DISPLAY_DELTA_MIN/MAX = 0.35 / 0.50` (`best_value_ui.py:25-26`). `filter_ranked_display` (`best_value_ui.py:140-163`) keeps rows whose **provider** `chain_delta` has `lo ≤ |δ| ≤ hi`; null delta is dropped (`:94-105`, `:118-127`).

Divergence from the scored frame:

1. `calculate_best_value` / `log_run` already finished on the scan path (`dailyScaner.py:108-136`).
2. Dashboard scores a separate frame (`app.py:2421`), keeps non-null scores (`:2441`), takes global `sort_values("Value_Score").head(show_n)` as `top5` (`:2594-2598`).
3. `log_best_value_run(top5)` (`app.py:2602`) uses **unfiltered** `top5` (CSV archive, not `attribution.db`).
4. `filter_ranked_display(top5, …)` at `app.py:2726` produces `vis_top5`. The table is `_render_best_value_table_with_plus(..., vis_top5, vis_disp, ...)` (`:2740-2742`).

The filtered frame first appears at `app.py:2726`. Attribution never receives it.

### Provider Delta / Theta/Prem

`greeks_display_columns(top5, vol_curr)` (`app.py:2656`, `best_value_ui.py:260-283`) reads chain records, not scoring `delta` (`best_value_ui.py:267-268`). Written onto `disp` at `app.py:2685-2687` after scoring. Not a `calculate_best_value` input.

### Time-stop `Exit by`

`format_exit_by_cell` is applied to `top5` rows into `disp["Exit by"]` (`app.py:2704-2717`) after scoring. The table render uses `vis_disp` (`:2740`). `time_stop.py` was not opened; clock rules of `format_exit_by_cell` are under **Unverified**.

### Global top-N sort (dashboard)

`top5` is a **cross-pool** `Value_Score` sort (`app.py:2594-2598`), not `_rank` within pool. That changes what is shown versus `flags.rank`. It happens after scoring and is not passed to `log_run`.

---

## Unverified

- What exact hold windows and 15:45 cap `format_exit_by_cell` uses (`time_stop.py` not read).
- Whether `app.py:1868` rebuild path is ever passed to attribution (no `log_run` in `app.py`).
- Whether every historical `flags` row on older hashes reconciles the same way (only `243ecda68cfc8618` was sampled).

---

## 11. Findings

| id | note |
|---|---|
| F-S1-07 | NaN `dVol` filled to 1.0 (`best_value.py:346-353`) — new entrants / decrease-suspect sit at ×1 on the raw dVol scale. |
| F-S1-08 | `_minmax` is per-pool per-scan (`best_value.py:359-411`) — `Value_Score` is a rank-like level. |
| F-S1-09 | Multipliers apply after the blend (`best_value.py:418` then `:459-666`); product is uncapped. |
| F-S2-01 | `best_value.py:5` says `telegram_bot.py` imports this module; `telegram_bot.py` does not call `calculate_best_value` / `build_best_value_df`. |
| F-S2-02 | Stored `score` is `round(4)` (`best_value.py:676`); `base_score * Π(multipliers)` differs at ~1e-5. |
| F-S2-03 | `build_best_value_df` maps missing `dte` to `0` (`best_value.py:753`), which `scoring_pool` treats as `0DTE` (`scoring_pool.py:52-53`). |
| F-S2-04 | Dashboard caption documents flow as `VOL/OI × \|ΔVol\|` (`app.py:2391`); `_flow` does not `abs()` `dVol` (`best_value.py:346-356`). |

---

## 12. Coverage

| file | read | skimmed (grep/symbol) | not opened |
|---|---|---|---|
| `best_value.py` | 1–892 | — | — |
| `config.py` | 1–75 | — | — |
| `scoring_pool.py` | 1–62 | — | — |
| `greeks.py` | 1–142 | — | — |
| `strategy_engine.py` | 1–198 | 201–399 (`resolve_has_catalyst`, `attach_optimal_strategy`) | — |
| `zero_dte_gex.py` | 1–27, 189–229 | 28–188 (GEX state machine) | — |
| `pov_leakage.py` | 21–22, 90–118 | 121–277 | 1–20, 28–89 |
| `cost_distribution.py` | 298–313 | — | 1–297, 314–end |
| `attribution.py` | 631–716, 756–845 | schema / view (73–247) | mark path |
| `dailyScaner.py` | 69–166, 1268–1285 | `def` list | 168–1267 body |
| `app.py` | 615–624, 1618–1638, 1868–1874, 2308–2743 | other tab/zone defs | remainder (~5k lines) |
| `best_value_ui.py` | 25–26, 94–163, 260–283 | — | rest |
| `chain_quality.py` | 485–505, 549–570 | attach_dvol imports | rest |
| `time_stop.py` | — | symbol `format_exit_by_cell` | entire file |
| `telegram_bot.py` | — | grep: no scoring imports | entire file |
