# Cursor Task — Fix two defects in the new Best Value row-select table

Commit under review: `5050b22`

**Scope:** `app.py::_render_best_value_table_with_plus`, `best_value_ui.py`, and their tests.
Do not touch scoring, attribution, `portfolio_store`, journal writes, or
`_render_add_position_form`.

---

## Defect 1 — Selection maps by display POSITION, not contract identity 🔴

```
In the Best Value table (app.py::_render_best_value_table_with_plus, commit 5050b22),
row selection is mapped to the underlying frame by positional index:

    payload = pending_add_pos_payload(ticker, top5_r, sel)   # sel = event.selection.rows
    ...
    raw = top5_r.iloc[idx]

This is only correct while the displayed order equals top5 order. st.dataframe lets the
user sort by clicking a column header. After a sort, event.selection.rows returns indices
into the SORTED view, so iloc into top5_r resolves to a DIFFERENT contract than the one
the user highlighted — and the confirm button then names that wrong contract. The user
adds a position they did not select, with a plausible-looking confirmation.

Fix by carrying contract identity instead of position.

Requirements:

1. Build a stable key per contract: f"{side}|{strike:.4f}|{expiry}" (uppercase side,
   fixed-precision strike so float formatting cannot drift). Derive it from the
   UNDERLYING frame.

2. Add that key as a column on the display frame passed to st.dataframe. Hide it from
   the user — either via column_config with a hidden/None config, or by keeping a
   parallel lookup keyed on a column the user already sees. Do NOT show a raw key
   column in the UI.

3. Change pending_add_pos_payload to resolve by key, not by iloc. Signature should
   accept the selected key (or the display frame plus selection so it can read the key
   out), look it up in a dict built from the underlying frame, and return None if the
   key is absent.

4. If duplicate keys are possible (same side/strike/expiry appearing twice in top5),
   detect it and fall back to positional mapping for that case rather than silently
   picking the first. Log a warning. Report whether duplicates are actually possible
   given how top5 is built.

Tests (these must FAIL against the current code and pass after the fix):

- test_selection_survives_display_reordering: build top5 and a disp frame whose row
  order is REVERSED relative to top5. Select display row 0. Assert the payload matches
  the contract that is displayed at row 0 — i.e. the LAST row of top5, not the first.
  This is the regression the current positional code gets wrong.
- test_selection_by_key_missing_returns_none
- test_duplicate_keys_fall_back_to_position
- Keep the existing tests in tests/test_best_value_ui.py passing, including
  test_pending_payload_keys_unchanged — the payload contract with
  _render_add_position_form must not change.

Note: the existing test_selected_row_maps_to_correct_contract cannot catch this bug,
because a pure helper given an index can only trust that index. Do not delete it, but
the new reordering test is the one that matters.
```

---

## Defect 2 — Styler and `column_config` may not compose 🟡

```
The same function passes a pandas Styler (from style_best_value_rows) to st.dataframe
AND a column_config full of st.column_config.TextColumn(width=...) entries.

These are two different rendering paths — Streamlit renders the Styler's own cell
formatting, while column_config applies to the underlying dataframe schema. Depending
on Streamlit version, one of the two silently wins: either the BEST VALUE green row
highlight is lost, or the width pinning is ignored and Optimal Strategy truncates back
to "IRON ..." — which was the original complaint.

Determine empirically which happens on the pinned Streamlit version in
requirements.txt. Do not guess from documentation. Write a minimal repro script if
needed, or check the Streamlit source for how a Styler input interacts with
column_config.

Report what you find, then:

- If BOTH work: leave as is, and add a comment recording the version you verified on
  and how, so a future upgrade does not silently regress it.
- If they CONFLICT: keep column_config (widths matter more than the green row) and
  replace the row highlight with a leading text column — a "★" or "●" on BEST VALUE
  rows, blank otherwise, pinned width="small". Remove the Styler entirely; do not leave
  dead code.

Also verify: does column-header sorting still work when a Styler is passed? If sorting
is disabled by the Styler, that removes the main benefit of moving to st.dataframe, and
is a second reason to drop it. Report the answer.

All values in the display frame are already pre-formatted to strings upstream (Price,
Volume, IV, Value_Score etc. are formatted in the caller), so the TextColumn entries are
doing width work only. Do not add numeric column configs — that would double-format.
```

---

## Constraints (paste with either task)

```
- Do NOT modify scoring math in best_value.py
- Do NOT modify attribution.py, mark_runner.py, portfolio_store.py, or journal writes
- Do NOT modify _render_add_position_form or the keys of the _pending_add_pos payload
- Do NOT weaken, skip, or delete an existing test; do NOT remove an xfail(strict=True)
- If a fix requires changing behaviour outside this scope, STOP and report instead
- Report actual command output, not a summary. If you did not run a command, say so.
```

---

## Verify yourself — in the browser, not just pytest

pytest cannot catch either of these. Both need a real render.

**Defect 1:**
1. Click the **Value_Score** column header to sort ascending (reverses the order)
2. Select the third visible row
3. Confirm the button names *that* contract — same side, strike, expiry as the row you clicked
4. Add it, then confirm My Open Positions holds the same contract
5. Repeat after sorting by **Volume**

**Defect 2:**
1. Confirm the Optimal Strategy column shows full text, not `IRON …`
2. Confirm BEST VALUE row is still visually distinct (green row, or ★ marker)
3. Click a column header — confirm sorting actually works

```bash
python -m pytest tests/ -q      # expect all green, 8 xfailed
```

---

## Note

Defect 1 is the one worth being careful about. A mis-add is quiet: the confirmation names
the wrong contract, the ledger records the wrong contract, and the journal writes the
wrong contract — all consistently, so nothing looks broken. You would only catch it by
noticing later that a position you never chose is being tracked.
