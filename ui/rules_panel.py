"""Settings → Rules: the per-ticker rules kept in data/ticker_rules.db (append-only),
their paper record, and the change log. Phase 1: paper only, the Game Plan is unchanged."""

from __future__ import annotations

import streamlit as st

from ticker_rules import rules, store

_SLOT_TITLE = {"normal": "Normal day", "in_play": "In-play day (opening volume high)",
               "exit": "Exit (every paper trade)"}
_STATUS_BADGE = {"paper": "📝 PAPER", "live": "🟢 LIVE", "off": "⚪ OFF"}


def _params_editor(key: str, defaults: dict, current: dict) -> dict:
    out = {}
    cols = st.columns(max(len(defaults), 1))
    for i, (name, dflt) in enumerate(defaults.items()):
        val = current.get(name, dflt)
        with cols[i]:
            if isinstance(dflt, (int, float)) and not isinstance(dflt, bool):
                step = 1 if isinstance(dflt, int) else 0.05
                out[name] = st.number_input(name, value=type(dflt)(val), step=step,
                                            key=f"{key}_{name}")
            else:
                out[name] = st.text_input(name, value=str(val), key=f"{key}_{name}")
    return out


def _slot_block(ticker: str, slot: str, row: dict | None) -> None:
    st.markdown(f"**{_SLOT_TITLE[slot]}**")
    if row:
        label = rules.RULES.get(row["rule"], {}).get("label", row["rule"])
        params = ", ".join(f"{k} {v}" for k, v in (row.get("params") or {}).items())
        st.write(f"{_STATUS_BADGE.get(row['status'], row['status'])} · **{label}** · {params}")
        st.caption(f"Since {row['ts'][:16].replace('T', ' ')} · settings #{row['id']}"
                   + (f" · {row['note']}" if row.get("note") else ""))
        if slot != "exit":
            rec = store.record_for(row["rule"], ticker)
            if rec["trades"]:
                st.caption(f"Paper record: {rec['trades']} trades · {rec['win_pct']}% winners · "
                           f"avg ${rec['avg']:+.2f} · net ${rec['net']:+.2f}")
            else:
                st.caption("Paper record: no closed trades yet.")
    else:
        st.write("No rule set.")

    with st.expander("Change", expanded=False):
        key = f"rules_{ticker}_{slot}"
        if slot == "exit":
            rule = "debit_spread"
            defaults = dict(store.DEFAULT_EXIT)
        else:
            options = [n for n, s in rules.RULES.items() if slot in s["slots"]]
            cur_rule = row["rule"] if row and row["rule"] in options else options[0]
            rule = st.selectbox("Rule", options, index=options.index(cur_rule),
                                format_func=lambda n: rules.RULES[n]["label"], key=f"{key}_rule")
            defaults = dict(rules.RULES[rule]["defaults"])
        current = (row or {}).get("params") or {} if row and row["rule"] == rule else {}
        params = _params_editor(key, defaults, current)
        status = st.radio("Status", store.STATUSES, horizontal=True, key=f"{key}_status",
                          index=store.STATUSES.index(row["status"]) if row else 0,
                          format_func=lambda s: _STATUS_BADGE[s])
        note = st.text_input("Note (why this change)", key=f"{key}_note")
        ok = True
        if status == store.LIVE and slot != "exit":
            rec = store.record_for(rule, ticker)
            if rec["trades"] < store.PROMOTE_MIN_TRADES:
                st.warning(f"{rules.RULES[rule]['label']} has {rec['trades']} closed paper "
                           f"trades ({rec['win_pct'] if rec['win_pct'] is not None else '–'}% "
                           f"winners). Fewer than {store.PROMOTE_MIN_TRADES}: not proven.")
                ok = st.checkbox("I understand, set it live anyway", key=f"{key}_confirm")
        if st.button("Save change", key=f"{key}_save", disabled=not ok):
            sid = store.add_setting(ticker, slot, rule, params, status, note or None)
            st.success(f"Saved as settings #{sid}.")
            st.rerun()


def render(ticker: str) -> None:
    store.seed_if_empty()
    ticker = ticker.upper()
    st.markdown(f"**Trading rules — {ticker}**")
    st.caption("Phase 1: every rule is recorded as a paper debit spread at each scan "
               "(one per ticker per day). Nothing here changes the Game Plan yet. "
               "Every change is a new row in data/ticker_rules.db, so nothing is overwritten.")
    cur = store.current(ticker)
    for slot in ("normal", "in_play", "exit"):
        _slot_block(ticker, slot, cur.get(slot))
        st.divider()

    st.markdown("**Paper trades**")
    t = store.trades(ticker)
    if t.empty:
        st.caption("None yet.")
    else:
        show = t[["day", "rule", "slot", "side", "expiry", "long_strike", "short_strike",
                  "entry_at", "entry_cost", "status", "exit_at", "exit_value", "pnl",
                  "settings_id", "reason"]]
        st.dataframe(show, hide_index=True, use_container_width=True)

    st.markdown("**Change log**")
    h = store.history(ticker)
    st.dataframe(h[["id", "ts", "slot", "rule", "params", "status", "note"]],
                 hide_index=True, use_container_width=True)
