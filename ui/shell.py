"""App shell: the left-hand menu, the ticker bar, and settings that persist across pages.

No scoring, no network, no file I/O (saving to disk is ui/settings_store.py, passed in). ``st`` is passed in so this module never holds
Streamlit state of its own and can be exercised with AppTest.

The menu is data: edit ``MENU`` to add, remove or regroup entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

NAV_KEY = "nav_page"


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    material: str      # Material icon name (Streamlit >= 1.40)
    emoji: str         # fallback for older Streamlit

    def label(self, *, material_icons: bool) -> str:
        if material_icons:
            return f":material/{self.material}: {self.title}"
        return f"{self.emoji} {self.title}"


# ── the menu ────────────────────────────────────────────────────────────────
# (group title, entries). The first entry of the first group is the landing page.
MENU: list[tuple[str, list[Entry]]] = [
    ("Trade", [
        Entry("game_plan", "Game Plan", "flag", "🎯"),
        Entry("flow", "Options Flow", "candlestick_chart", "📈"),
        Entry("best_value", "Best Value", "star", "⭐"),
        Entry("spread_gate", "Spread Gate", "science", "🔬"),
        Entry("pretrade", "Pre-Trade Check", "fact_check", "✅"),
    ]),
    ("Market", [
        Entry("price_chart", "Chart", "show_chart", "📉"),
        Entry("flow_magnets", "Flow Magnets", "attractions", "🧲"),
        Entry("expiry_breakdown", "Expiration Breakdown", "calendar_month", "📅"),
        Entry("cost_distribution", "Cost Distribution", "stacked_bar_chart", "💰"),
        Entry("gamma", "Gamma", "grid_on", "🧱"),
        Entry("volume", "Volume", "bar_chart", "📊"),
        Entry("news", "Market News", "newspaper", "📰"),
    ]),
    ("Review", [
        Entry("report", "Report", "summarize", "🧾"),
        Entry("scorecard", "Scorecard", "insights", "🏅"),
        Entry("archive", "Scanner Archive", "database", "📋"),
        Entry("journal", "Journal", "menu_book", "📓"),
        Entry("tickers", "Tickers", "list_alt", "📁"),
    ]),
    ("", [
        Entry("settings", "Settings", "settings", "⚙️"),
    ]),
]


def entries() -> list[Entry]:
    return [e for _, group in MENU for e in group]


def page_ids() -> list[str]:
    return [e.id for e in entries()]


def default_page() -> str:
    return entries()[0].id


def current_page(st) -> str:
    """The selected page id; an unknown or missing value falls back to the landing page."""
    page = st.session_state.get(NAV_KEY)
    if page not in page_ids():
        page = default_page()
        st.session_state[NAV_KEY] = page
    return page


def go(st, page_id: str) -> None:
    """Select a page programmatically (deep links, 'open in Pre-Trade Check')."""
    if page_id not in page_ids():
        raise ValueError(f"unknown page {page_id!r}")
    st.session_state[NAV_KEY] = page_id


MENU_CSS = """
<style>
section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] { gap: 0.15rem; }
section[data-testid="stSidebar"] div.stButton > button {
    justify-content: flex-start; text-align: left; border: 0; border-radius: 6px;
    background: transparent; padding: 0.45rem 0.75rem; font-weight: 500;
    border-left: 3px solid transparent;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    background: rgba(255, 255, 255, 0.06);
}
section[data-testid="stSidebar"] div.stButton > button[kind="primary"] {
    background: rgba(45, 212, 191, 0.12); color: rgb(94, 234, 212);
    border-left: 3px solid rgb(45, 212, 191);
}
section[data-testid="stSidebar"] div.stButton > button p { font-size: 0.92rem; }
</style>
"""


def render_menu(st, *, material_icons: bool = False, title: str = "Options Scanner") -> str:
    """Draw the menu in the sidebar and return the current page id."""
    page = current_page(st)
    sb = st.sidebar
    sb.markdown(MENU_CSS, unsafe_allow_html=True)
    sb.markdown(f"### {title}")
    for group, group_entries in MENU:
        if group:
            sb.caption(group.upper())
        else:
            sb.divider()
        for e in group_entries:
            clicked = sb.button(
                e.label(material_icons=material_icons),
                key=f"nav_btn_{e.id}",
                use_container_width=True,
                type="primary" if e.id == page else "secondary",
            )
            if clicked and e.id != page:
                go(st, e.id)
                st.rerun()
    return page


# ── settings that survive leaving the Settings page ─────────────────────────
# Streamlit drops a widget's state when the widget is not drawn, so each setting
# is mirrored into a plain session key that every page can read.

DEFAULTS: dict[str, Any] = {"min_dte": 1, "top_n": 5, "sort_by": "Volume",
                            "show_all_ranked": False, "stop_buffer_pct": 0.10,
                            "min_reward_to_risk": 1.5}
STOP_BUFFER_RANGE = (0.0, 2.0)
REWARD_TO_RISK_RANGE = (0.5, 5.0)
SORT_OPTIONS = ["Volume", "Premium $", "Strike"]
LOADED_KEY = "cfg_loaded"
SAVED_KEY = "cfg_saved"


def clean(values: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only known settings with valid values; anything else is dropped (and the
    default applies). Protects against a hand-edited or outdated settings file."""
    out: dict[str, Any] = {}
    v = values or {}
    if isinstance(v.get("min_dte"), int) and not isinstance(v.get("min_dte"), bool) \
            and v["min_dte"] >= 0:
        out["min_dte"] = v["min_dte"]
    if isinstance(v.get("top_n"), int) and not isinstance(v.get("top_n"), bool) \
            and 1 <= v["top_n"] <= 30:
        out["top_n"] = v["top_n"]
    if v.get("sort_by") in SORT_OPTIONS:
        out["sort_by"] = v["sort_by"]
    if isinstance(v.get("show_all_ranked"), bool):
        out["show_all_ranked"] = v["show_all_ranked"]
    buf = v.get("stop_buffer_pct")
    if isinstance(buf, (int, float)) and not isinstance(buf, bool) \
            and STOP_BUFFER_RANGE[0] <= buf <= STOP_BUFFER_RANGE[1]:
        out["stop_buffer_pct"] = float(buf)
    rr = v.get("min_reward_to_risk")
    if isinstance(rr, (int, float)) and not isinstance(rr, bool) \
            and REWARD_TO_RISK_RANGE[0] <= rr <= REWARD_TO_RISK_RANGE[1]:
        out["min_reward_to_risk"] = float(rr)
    return out


def hydrate(st, saved: dict[str, Any] | None) -> None:
    """Once per session, copy saved settings into the session (never over a value the
    session already holds)."""
    if st.session_state.get(LOADED_KEY):
        return
    st.session_state[LOADED_KEY] = True
    good = clean(saved)
    for name, value in good.items():
        st.session_state.setdefault(_store_key(name), value)
    st.session_state[SAVED_KEY] = {**DEFAULTS, **good}


def all_settings(st) -> dict[str, Any]:
    return {name: setting(st, name) for name in DEFAULTS}


def save_if_changed(st, save: Callable[[dict[str, Any]], None]) -> bool:
    """Write the settings through ``save`` when they differ from what was last saved."""
    current = all_settings(st)
    if st.session_state.get(SAVED_KEY) == current:
        return False
    save(current)
    st.session_state[SAVED_KEY] = current
    return True


def _store_key(name: str) -> str:
    return f"cfg_{name}"


def setting(st, name: str) -> Any:
    if name not in DEFAULTS:
        raise KeyError(name)
    return st.session_state.get(_store_key(name), DEFAULTS[name])


def flow_settings(st) -> dict[str, Any]:
    """The flow filters as every page sees them."""
    return {
        "min_dte": int(setting(st, "min_dte")),
        "top_n": int(setting(st, "top_n")),
        "sort_by": str(setting(st, "sort_by")),
        "show_all_ranked": bool(setting(st, "show_all_ranked")),
        "stop_buffer_pct": float(setting(st, "stop_buffer_pct")),
        "min_reward_to_risk": float(setting(st, "min_reward_to_risk")),
    }


def render_flow_filters(st, choice_control: Callable[..., str]) -> dict[str, Any]:
    """Draw the flow filters (Settings page) and persist what the user picks."""
    min_dte = st.number_input("Min DTE", min_value=0, step=1,
                              value=int(setting(st, "min_dte")), key="w_min_dte")
    top_n = st.number_input(
        "Top N results", min_value=1, max_value=30, step=1,
        value=int(setting(st, "top_n")), key="w_top_n",
        help="How many rows to show in Best Value and how many calls/puts in The Magnets",
    )
    cur_sort = setting(st, "sort_by")
    sort_by = choice_control("Sort by", SORT_OPTIONS,
                             default=cur_sort if cur_sort in SORT_OPTIONS else SORT_OPTIONS[0],
                             key="flow_sort_by")
    st.session_state[_store_key("min_dte")] = int(min_dte)
    st.session_state[_store_key("top_n")] = int(top_n)
    st.session_state[_store_key("sort_by")] = sort_by
    return flow_settings(st)


def render_best_value_settings(st) -> bool:
    """Draw the Best Value display settings (Settings page) and persist the choice."""
    show_all = st.toggle(
        "Show all ranked contracts",
        value=bool(setting(st, "show_all_ranked")), key="w_show_all_ranked",
        help="Off: Best Value hides rows outside the Pre-Trade |δ| band 0.35–0.50, "
             "including missing delta. Does not change scoring or attribution.",
    )
    st.session_state[_store_key("show_all_ranked")] = bool(show_all)
    return bool(show_all)


def render_game_plan_settings(st) -> float:
    """Draw the Game Plan settings (Settings page) and persist the choice."""
    buf = st.number_input(
        "Stop buffer (%)", min_value=STOP_BUFFER_RANGE[0], max_value=STOP_BUFFER_RANGE[1],
        step=0.05, format="%.2f", value=float(setting(st, "stop_buffer_pct")),
        key="w_stop_buffer_pct",
        help="How far beyond the support or resistance level the Game Plan places the stop, "
             "as a percentage of that level. 0.10% of $333 is about $0.33.",
    )
    st.session_state[_store_key("stop_buffer_pct")] = float(buf)
    rr = st.number_input(
        "Minimum reward to risk", min_value=REWARD_TO_RISK_RANGE[0],
        max_value=REWARD_TO_RISK_RANGE[1], step=0.1, format="%.1f",
        value=float(setting(st, "min_reward_to_risk")), key="w_min_reward_to_risk",
        help="The entry zone ends at the price where the reward to target 1 falls below "
             "this many times the risk to the stop. Higher means a tighter zone.",
    )
    st.session_state[_store_key("min_reward_to_risk")] = float(rr)
    return float(buf)


TICKER_STORE = "cfg_ticker"


def stored_ticker(st, tickers: list[str], *, preferred: str = "AAPL") -> str:
    """The ticker in effect before the bar is drawn (last choice, else the preferred one)."""
    if not tickers:
        raise ValueError("no tickers to choose from")
    for key in ("w_ticker", TICKER_STORE):      # a just-changed widget wins over the mirror
        cur = st.session_state.get(key)
        if cur in tickers:
            return cur
    return preferred if preferred in tickers else tickers[0]


def render_ticker_bar(st, tickers: list[str], *, preferred: str = "AAPL",
                      status: str | None = None) -> str:
    """Slim bar at the top of every page: ticker selector on the left, status on the right.

    The choice is mirrored into a plain session key: a menu click reruns the app
    before this widget is drawn, and Streamlit would otherwise forget the selection.
    """
    cur = stored_ticker(st, tickers, preferred=preferred)
    left, right = st.columns([1, 4])
    ticker = left.selectbox("Ticker", tickers, index=tickers.index(cur), key="w_ticker",
                            label_visibility="collapsed",
                            help="Every page shows data for this ticker. "
                                 "Add new tickers on the Tickers page.")
    st.session_state[TICKER_STORE] = ticker
    if status:
        right.caption(status)
    return ticker
