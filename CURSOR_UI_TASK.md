# Cursor Task — Best Value table: replace per-row ＋ grid with native selection

**Scope:** presentation only. `_render_add_position_form`, `portfolio_store`, journal
writes, and the attribution path must all behave identically after this change.

---

```
Rework the Best Value table in app.py. This is a UI-only change — do not touch
scoring, attribution, portfolio_store, or the journal.

## Problem

_render_best_value_table_with_plus currently renders the table as one st.columns()
row per contract, with a markdown <div> per cell. This produces:
  - every cell as a separate rounded box with column gutters between them, so
    there is no shared grid and nothing aligns vertically
  - unpredictable text truncation ("IRON ...", "(0) Ou...", "Strike ...")
  - no sorting
  - a full st.rerun() per ＋ button, and 8+ identical ＋ glyphs with no indication
    of which contract each one belongs to

## Required change

Replace the st.columns grid with a single st.dataframe using native row selection:
    on_select="rerun", selection_mode="single-row"

This same pattern is already used elsewhere in app.py (search for on_select="rerun")
— match that existing usage, including its fallback for older Streamlit versions.

Behaviour after selection must be UNCHANGED from today:
  - selecting a row and confirming sets st.session_state["_pending_add_pos"] with
    exactly the same keys it sets now: Ticker, Side, Strike, Expiry, default_price
  - _render_add_position_form then collects entry price + quantity as it does today
  - portfolio_store.append_position and the journal write are untouched

Do NOT change _render_add_position_form. Do NOT change what gets persisted.

## Specifics

1. Replace the ＋-per-row column with a single primary button BELOW the table that
   names the selected contract, e.g.:
       ＋  Add PUT $337.5 · 2026-07-29 to Open Positions
   When no row is selected, show a quiet caption instead: "Select a row to add it
   to Open Positions." Do not show a disabled button.

2. Keep the BEST VALUE row highlight. Use a pandas Styler applied to the display
   frame, keyed off the Status column of the underlying frame (not the display
   frame). Preserve the existing colours (#1e4620 background, white text).

3. Drop the "Velocity" and "Target" columns from this table. Both are currently
   uniform ("+0.0000" and "—") across every row and are consuming width that
   Optimal Strategy needs.

   IMPORTANT: the st.caption below the table currently documents exit rules based
   on Velocity ("CLOSE if Velocity ≤ −0.15", "HOLD FOR RUNNER if Velocity > +0.20").
   Do not silently leave that caption describing a column that is no longer shown
   and is uniformly zero. Either:
     (a) confirm Score_Velocity is genuinely producing zeros and remove those two
         clauses from the caption, or
     (b) if you find Score_Velocity is actually populated and the zeros are a
         display/formatting bug, STOP and report that as a finding rather than
         changing the caption.
   Report which of these you found and what you did.

4. Pin column widths via column_config so nothing truncates:
       Side / Strike / DTE / Price  -> width="small"
       Volume / OI / ΔVol / IV / Value_Score -> width="small"
       Signal -> width="medium"
       Optimal Strategy -> width="large"

5. hide_index=True, use_container_width=True. Give the dataframe a stable key
   scoped to the ticker so switching tickers does not carry a stale selection.

6. Selection index maps to the UNDERLYING frame (top5), not the display frame.
   Both must be .reset_index(drop=True) before indexing, or the wrong contract
   gets added. Add a test for this.

## Tests

Add to the existing test file for app helpers (or create tests/test_best_value_ui.py):

- test_selected_row_maps_to_correct_contract: build a top5/disp pair where display
  order differs from the original index, select row 2, assert the pending payload
  matches the correct underlying contract (side, strike, expiry, last)
- test_pending_payload_keys_unchanged: assert the dict written to
  _pending_add_pos has exactly the keys {Ticker, Side, Strike, Expiry,
  default_price} — this is the contract with _render_add_position_form
- test_no_selection_writes_nothing: assert no session_state mutation when the
  selection is empty

Extract the payload-building into a small pure helper so these are testable
without a Streamlit runtime.

## Constraints

- Do NOT modify scoring math in best_value.py
- Do NOT modify attribution.py, portfolio_store.py, or journal writes
- Do NOT weaken or delete an existing test
- Report the actual test output, not a summary
```

---

## Verify yourself

```bash
python -m pytest tests/ -q                      # all green, still 8 xfailed
grep -n "st.columns(weights)" app.py            # expect: no hits
```

Then in the app:
1. Select a row that is **not** the first — confirm the button names that exact contract
2. Sort by a column, then select — confirm it still adds the right one (this is the
   index-mapping bug the test guards)
3. Add it, check it lands in My Open Positions with the right strike/expiry
4. Confirm the journal file for today has the event

---

## Note on the caption

Item 3 is written so Cursor cannot quietly "fix" the caption to match a broken column.
If `Score_Velocity` is genuinely dead, the caption is documenting exit rules that can
never fire — worth knowing. If it's a formatting bug, that's a real defect and you want
it reported, not papered over. Either answer is useful; a silent edit is not.
