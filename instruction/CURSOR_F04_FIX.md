# Cursor Task — Fix F-04: bearish strategy multipliers and the unknown-bias trap

Commit under review: `268fd62`
**Scope:** `best_value.py` strategy-multiplier block, `strategy_engine.py`, `config.py`.
Do not touch attribution, mark_runner, portfolio_store, greeks, or UI code.

---

```
F-04 has three related defects in the strategy-multiplier block at
best_value.py:378-403. They only appear together, so fix them together.

## Defect 1 — bearish outlooks have no branch at all

strategy_engine.recommend_strategy can return SIX outcomes:

    "🚀 LONG CALL (+2) - Explosive Upside"
    "📈 BULL CALL SPREAD (+1) - High Probability Up"
    "🦅 IRON CONDOR (0) - Range Bound Premium"
    "💥 STRADDLE/STRANGLE - Volatility Expansion"
    "📉 BEAR PUT SPREAD (-1) - High Probability Down"
    "🩸 LONG PUT (-2) - Explosive Downside"

The multiplier block branches on only three of them:

    if   "(+2)" in strat:  ...
    elif "(+1)" in strat:  ...
    elif "(0)"  in strat:  ...
    # (-1), (-2) and STRADDLE: no branch — fall through with NO adjustment

So on a HEAVY BEARISH day the engine correctly recommends a bear put spread and then
applies zero strategy adjustment to anything. Bearish regimes get no strategy signal.

Also note: the STRADDLE string contains no "(N)" token at all, so it silently falls
through too. Decide explicitly what a volatility-expansion outlook should do (a
reasonable answer is: boost near-ATM on BOTH sides, since a straddle is non-
directional) and implement it, or document deliberately that it applies no multiplier.
Do not leave it as an accidental fall-through.

## Defect 2 — parsing by substring is fragile

`"(0)" in strat` is a substring test against a display string containing emoji and
prose. It is one copy-edit away from breaking silently, and "(+1)" vs "(-1)" differ by
one character.

Replace with an explicit outlook token. Add to strategy_engine.py a function
`strategy_outlook(strat: str) -> int | None` returning +2, +1, 0, -1, -2, or None for
STRADDLE/UNKNOWN. Better: have recommend_strategy return a small dataclass or
(label, outlook) tuple so the outlook is never re-derived from display text. Keep the
display string unchanged for the UI and Telegram output.

best_value.py must branch on the integer outlook, not on substrings.

## Defect 3 — unknown bias is punished harder than adverse bias

recommend_strategy line 100: `bias = (daily_bias or "NEUTRAL").strip().upper()`

daily_bias=None therefore resolves to NEUTRAL, returns IRON CONDOR (0), and the "(0)"
branch multiplies EVERY directional contract by mult_zero_outlook = 0.3.

Meanwhile resolve_biases_for_ticker ends in a bare `except Exception: pass` returning
(None, None). So one yfinance failure silently collapses every directional score by 70%
with no log line, no banner, and no non-zero exit code. Missing data is punished harder
than adverse data.

Fix:
  - Distinguish "genuinely NEUTRAL" from "bias unknown". Add an UNKNOWN outlook
    (return None from strategy_outlook, or an explicit STRAT_UNKNOWN_BIAS constant).
  - When bias is unknown, apply NO strategy multiplier — neither boost nor penalty.
    An unknown regime is not a range-bound regime.
  - Surface it: log a warning, set a flag on the run, and tag affected rows so the UI
    can show "bias unavailable" rather than presenting an Iron Condor recommendation
    the engine did not actually derive.
  - resolve_biases_for_ticker must not swallow the exception silently. Log it with the
    exception type and message. Do not change its return contract.

## Required multipliers

Add to config.py (so they are covered by config_hash). Mirror the bullish side:

    "mult_minus2_boost": 1.5,     # slightly-OTM PUTs inside the 1SD downside band
    "mult_minus1_otm":   0.5,     # OTM PUTs penalised, mirroring plus1_otm
    "mult_minus1_itm":   1.3,     # ITM/ATM PUTs boosted, mirroring plus1_itm

Implement (-2) and (-1) as exact mirrors of (+2) and (+1), with PUT/CALL swapped and
l1_f (lower 1SD) used where u1_f (upper 1SD) is used on the bullish side. Add the
matching tags, mirroring the existing tag strings.

Do NOT retune any existing multiplier value in this task. Mirroring only.

## Tests

Remove the xfail markers from these three, which should now XPASS:
  - test_daily_bias_penalises_the_opposing_side[HEAVY BEARISH-CALL]
  - test_daily_bias_penalises_the_opposing_side[HEAVY BULLISH-PUT]
  - test_bearish_strategy_boosts_puts_the_way_bullish_boosts_calls

If any of them still fails after the fix, STOP and report — do not re-add the marker.

Add:
  - test_all_six_strategies_have_a_branch: parametrise over every STRAT_* constant;
    assert each produces a defined outlook and that the multiplier block handles it
    (no silent fall-through). This is the regression guard for the whole defect class.
  - test_unknown_bias_applies_no_multiplier: daily_bias=None must leave scores equal to
    the no-strategy baseline — NOT multiplied by 0.3
  - test_unknown_bias_differs_from_explicit_neutral: daily_bias=None and
    daily_bias="NEUTRAL" must produce DIFFERENT scores
  - test_bearish_mirrors_bullish: a PUT under HEAVY BEARISH gets the same multiplier a
    CALL gets under HEAVY BULLISH, given mirrored strike placement
  - test_strategy_outlook_parsing: every STRAT_* constant maps to its expected integer

## Constraints

- Do NOT modify greeks.py, attribution.py, mark_runner.py, portfolio_store.py, or UI
- Do NOT retune existing multiplier values — mirror them
- Do NOT change the display strings; the UI and Telegram bot depend on them
- Do NOT remove or weaken any other xfail(strict=True). Three should remain in
  test_best_value_engine.py after this (F-02 score comparability, F-05 dVol NaN, and
  the [0,1] range defect) plus 1 in test_golden_master.py. If any OTHER one changes
  status, STOP and report which.
- Regenerate the golden master and report the new config_hash
- Report actual command output, not a summary
```

---

## Verify yourself

```bash
python -m pytest tests/ -q                  # expect green, 4 xfailed (3 + golden)
python -c "
from strategy_engine import *
for s in (STRAT_LONG_CALL, STRAT_BULL_CALL_SPREAD, STRAT_IRON_CONDOR,
          STRAT_STRADDLE, STRAT_BEAR_PUT_SPREAD, STRAT_LONG_PUT, STRAT_UNKNOWN):
    print(repr(s), '->', strategy_outlook(s))
"
```

Then in the dashboard: on a bearish ticker, confirm rows no longer all read
`(0) Outlook — Premium Penalty`, and that PUTs carry a bearish tag.

---

## Why this one matters

You have seen this live. Every row of the 16:37 Best Value table read
`⚠️ (0) Ou…` and `🦅 IRON …` — every directional contract multiplied by 0.3, which is
why no Value_Score exceeded 0.18. Either the bias genuinely was neutral all day, or
resolve_biases_for_ticker failed and returned (None, None) and you could not tell the
difference. That indistinguishability is the actual defect.

With F-01 fixed, this is the largest remaining distortion in the score.

**After this lands, the engine is settled enough to start collecting.** F-02
(score is a rank not a level) and F-05 (NaN dVol handling) remain, but both are better
judged with real data in hand than fixed blind. Every further engine change forks the
sample, so once you start the 15–20 day window, stop changing scoring.
