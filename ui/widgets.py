"""Small widget helpers shared by the shell and pages."""

from __future__ import annotations

import streamlit as st


def _streamlit_ge(major: int, minor: int) -> bool:
    try:
        parts = [int(x) for x in st.__version__.split(".")[:2]]
        return tuple(parts) >= (major, minor)
    except Exception:
        return False


def _choice_control(
    label: str,
    options: list[str],
    *,
    default: str | None = None,
    key: str | None = None,
    help: str | None = None,
) -> str:
    """
    Prefer st.pills / st.segmented_control (Streamlit ≥1.40); fall back to
    horizontal radio on older versions.

    If ``key`` is already in session_state (including a jump set this run),
    do not also pass default — Streamlit rejects both on the same widget.
    """
    default = default if default in options else (options[0] if options else "")
    use_default = key is None or key not in st.session_state
    extra: dict = {}
    if key is not None:
        extra["key"] = key
    if help is not None:
        extra["help"] = help
    if use_default:
        extra["default"] = default
    if hasattr(st, "pills"):
        val = st.pills(label, options, **extra)
        return val if val is not None else default
    if hasattr(st, "segmented_control"):
        val = st.segmented_control(label, options, **extra)
        return val if val is not None else default
    radio_kw: dict = {"horizontal": True}
    if key is not None:
        radio_kw["key"] = key
    if help is not None:
        radio_kw["help"] = help
    if use_default:
        if key is not None and default in options:
            # Seed through session state, not ``index``. On Streamlit < 1.40 the index is
            # part of the widget's identity, so passing it on the first run only made the
            # radio forget the next selection whenever the default was not the first option.
            st.session_state[key] = default
        else:
            radio_kw["index"] = options.index(default) if default in options else 0
    return st.radio(label, options, **radio_kw)


TABLE_STYLES = [
    {"selector": "", "props": "width:100%; border-collapse:collapse; font-size:0.9rem; "
                              "font-variant-numeric:tabular-nums;"},
    {"selector": "th, td", "props": "text-align:center; padding:7px 10px; border:0; "
                                    "border-bottom:1px solid rgba(255,255,255,0.07);"},
    {"selector": "th", "props": "font-weight:600; color:#b0bec5;"},
    {"selector": "td:first-child, th:first-child",
     "props": "text-align:left; font-weight:500; white-space:nowrap;"},
]


def centered_table_html(frame, uuid: str = "tbl") -> str:
    """A small table as HTML: values centred, first column left-aligned, no index.

    st.dataframe right-aligns numbers and left-aligns text, which looks uneven for a
    short summary table. Dollar signs are written as entities so Markdown never reads
    two amounts as LaTeX, and the HTML is one line so no row becomes a code block."""
    html = (frame.style.hide(axis="index").set_uuid(uuid)
            .set_table_styles(TABLE_STYLES).to_html())
    flat = "".join(line.strip() for line in html.splitlines() if line.strip())
    return '<div style="overflow-x:auto;">' + flat.replace("$", "&#36;") + "</div>"
