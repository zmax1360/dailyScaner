"""Game Plan → Strategy: pick the ticker's strategy and see what it says for today.

The card is the one the last scan built (``payload["ticker_rules"]["plan"]``); a new choice
is written to data/ticker_rules.db (append-only) and shows from the next scan on."""

from __future__ import annotations

import streamlit as st

from ticker_rules import rules, store

BADGE = {"paper": "📝 PAPER — not a trade signal", "live": "🟢 LIVE", "off": "⚪ OFF"}
NONE = "(none)"


def _money(v) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _selector(ticker: str, cur: dict) -> None:
    normal = cur.get("normal")
    ip = cur.get("in_play")
    options = [n for n, s in rules.RULES.items() if "normal" in s["slots"]]
    labels = {n: rules.RULES[n]["label"] for n in options}
    labels[NONE] = "No strategy"
    c1, c2, c3 = st.columns([2.2, 1.4, 1.4])
    now_rule = normal["rule"] if normal and normal["status"] != store.OFF else NONE
    choice = c1.selectbox("Strategy (normal days)", options + [NONE],
                          index=(options + [NONE]).index(now_rule) if now_rule in options else len(options),
                          format_func=labels.get, key=f"gp_strategy_{ticker}")
    status = c2.radio("Mode", [store.PAPER, store.LIVE], horizontal=True,
                      index=1 if normal and normal["status"] == store.LIVE else 0,
                      key=f"gp_strategy_status_{ticker}", disabled=choice == NONE)
    breakout = c3.toggle("Breakout on in-play days", key=f"gp_breakout_{ticker}",
                         value=bool(ip and ip["status"] != store.OFF))
    changed = []
    want_rule = choice if choice != NONE else (normal or {}).get("rule", options[0])
    want_status = store.OFF if choice == NONE else status
    if not normal or normal["rule"] != want_rule or normal["status"] != want_status:
        changed.append(("normal", want_rule, want_status))
    want_ip = store.PAPER if breakout else store.OFF
    if (ip or {}).get("status", store.OFF) != want_ip:
        changed.append(("in_play", "orb_in_play", want_ip))
    if not changed:
        return
    warn = None
    if want_status == store.LIVE:
        rec = store.record_for(want_rule, ticker)
        if rec["trades"] < store.PROMOTE_MIN_TRADES:
            warn = (f"{labels[want_rule]} has {rec['trades']} closed paper trades on {ticker}. "
                    f"Fewer than {store.PROMOTE_MIN_TRADES}: not proven.")
    if warn:
        st.warning(warn)
    if st.button("Use this strategy", key=f"gp_strategy_save_{ticker}", type="primary"):
        for slot, rule, stat in changed:
            row = cur.get(slot)
            params = (row or {}).get("params") if row and row["rule"] == rule else \
                dict(rules.RULES[rule]["defaults"])
            store.add_setting(ticker, slot, rule, params, stat, "Chosen on the Game Plan")
        st.success("Saved. The Game Plan card updates at the next scan.")
        st.rerun()


def _card(card: dict) -> None:
    head = f"**{card.get('label') or 'No strategy'}** · {BADGE.get(card.get('status'), '')}"
    if card.get("in_play"):
        head += f" · in-play day (opening volume {card['rvol']:.1f}x normal)"
    st.markdown(head)
    sp = card.get("spread")
    if not sp:
        st.info(card.get("waiting") or "No signal.")
        return
    side = "Calls" if sp["side"] == "CALL" else "Puts"
    leg = "C" if sp["side"] == "CALL" else "P"
    st.markdown(f"**{side}:** buy {sp['long_strike']:g}{leg} / sell {sp['short_strike']:g}{leg} "
                f"· expiry {sp['expiry']} · debit **{sp['entry_cost']:.2f}**")
    st.caption(f"Why: {card['signal']['reason']}")
    m = st.columns(4)
    m[0].metric("Risk (max loss)", _money(sp["risk"]))
    m[1].metric("Max gain", _money(sp["max_gain"]), f"{sp['reward_risk']:.1f} : 1")
    m[2].metric("Break-even", f"${sp['break_even']:,.2f}")
    m[3].metric("Chance of profit", _pct(sp.get("p_profit")),
                f"max: {_pct(sp.get('p_max'))}", delta_color="off")
    ex = card.get("exit") or {}
    stop_spot = (f" or {card['ticker']} {'below' if sp['side'] == 'CALL' else 'above'} "
                 f"${sp['stop_spot']:.2f}") if sp.get("stop_spot") else ""
    st.markdown(f"**Exit:** sell at **{sp['target_value']:.2f}** ({_money(sp['target_gain'])}) · "
                f"stop at **{sp['stop_value']:.2f}** ({_money(sp['stop_loss'])}){stop_spot} · "
                f"out by **{ex.get('time_stop', '15:30')}**")
    if sp.get("expected_move"):
        st.caption(f"Market's expected move to the close: ±${sp['expected_move']:.2f}. "
                   "Odds are the options' own implied odds, not a forecast.")


def render(ticker: str, saved: dict | None) -> None:
    """``saved``: the scan's ``ticker_rules`` block (may be None for older scans)."""
    ticker = ticker.upper()
    store.seed_if_empty()
    cur = store.current(ticker)
    st.markdown(f"#### 📐 Strategy — {ticker}")
    _selector(ticker, cur)
    card = (saved or {}).get("plan")
    if not card:
        st.caption("No strategy card in this scan yet (it appears from the next scan on).")
        return
    if card.get("settings_id") not in {r["id"] for r in cur.values() if r}:
        st.caption("The strategy changed after this scan; the card below is the old one "
                   "until the next scan.")
    _card(card)
    t = store.trade_for_day(ticker, card["as_of"][:10])
    if t:
        pnl = "" if t["pnl"] is None else f" ${t['pnl']:+.0f}"
        res = f" → {t['status']}{pnl}"
        st.caption(f"Today's paper trade: {t['long_strike']:g}/{t['short_strike']:g} "
                   f"{t['side'].lower()} spread at {t['entry_cost']:.2f} ({t['entry_at'][11:16]})"
                   + (res if t["status"] != "OPEN" else " · open"))
