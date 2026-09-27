"""Locked read-modify-write + atomic JSON writes (safe_io) and their call sites."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import threading
import time

import pytest

import safe_io


def _run_threads(target, n_threads: int, per_thread: int) -> None:
    threads = [
        threading.Thread(target=lambda i=i: [target(i, j) for j in range(per_thread)])
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "writer thread hung (deadlock?)"


# ── pre_trade_check ──────────────────────────────────────────────────────────

def test_concurrent_save_check_loses_no_rows(tmp_path, monkeypatch):
    import pre_trade_check as ptc

    monkeypatch.setattr(ptc, "CHECKS_PATH", str(tmp_path / "checks.json"))
    real_load = ptc.load_checks

    def slow_load():  # widen the read→write window so an unlocked version loses rows
        rows = real_load()
        time.sleep(0.002)
        return rows

    monkeypatch.setattr(ptc, "load_checks", slow_load)
    _run_threads(lambda i, j: ptc.save_check({"verdict": f"{i}-{j}"}), n_threads=4, per_thread=10)

    rows = json.loads((tmp_path / "checks.json").read_text())
    assert len(rows) == 40
    assert len({r["check_id"] for r in rows}) == 40


def test_update_check_does_not_drop_concurrent_saves(tmp_path, monkeypatch):
    import pre_trade_check as ptc

    monkeypatch.setattr(ptc, "CHECKS_PATH", str(tmp_path / "checks.json"))
    first = ptc.save_check({"verdict": "seed"})
    real_load = ptc.load_checks

    def slow_load():
        rows = real_load()
        time.sleep(0.002)
        return rows

    monkeypatch.setattr(ptc, "load_checks", slow_load)

    def work(i, j):
        if i == 0:
            ptc.update_check(first["check_id"], taken=bool(j % 2))
        else:
            ptc.save_check({"verdict": f"{i}-{j}"})

    _run_threads(work, n_threads=3, per_thread=10)
    rows = json.loads((tmp_path / "checks.json").read_text())
    assert len(rows) == 1 + 2 * 10


# ── journal ──────────────────────────────────────────────────────────────────

def test_concurrent_append_fills_loses_no_fills(tmp_path, monkeypatch):
    import scanner.journal_io as jio

    monkeypatch.setattr(jio, "JOURNAL_DIR", str(tmp_path))
    real_read = jio._read_raw

    def slow_read(day):
        rows = real_read(day)
        time.sleep(0.002)
        return rows

    monkeypatch.setattr(jio, "_read_raw", slow_read)

    def fill(i, j):
        jio.append_fills("2026-09-01", [{
            "Action": "BUY", "Ticker": "AAPL", "Side": "CALL", "Strike": 200.0 + i,
            "Expiry": "2026-09-02", "Quantity": 1, "Price": 1.0,
            "At": f"2026-09-01T10:{i:02d}:{j:02d}-04:00", "Source": "scanner",
        }])

    _run_threads(fill, n_threads=4, per_thread=10)
    raw = json.loads((tmp_path / "2026-09-01.json").read_text())
    assert len(raw) == 40


# ── cross-process ────────────────────────────────────────────────────────────

def _increment(path: str, times: int) -> None:
    for _ in range(times):
        with safe_io.locked(path):
            n = json.load(open(path))["n"] if os.path.exists(path) else 0
            time.sleep(0.001)
            safe_io.write_json_atomic(path, {"n": n + 1})


@pytest.mark.skipif("fork" not in mp.get_all_start_methods(), reason="needs fork")
def test_lock_serialises_separate_processes(tmp_path):
    path = str(tmp_path / "counter.json")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_increment, args=(path, 15)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert json.load(open(path))["n"] == 60


# ── primitives ───────────────────────────────────────────────────────────────

def test_locked_is_reentrant_within_a_thread(tmp_path):
    path = str(tmp_path / "x.json")
    done = threading.Event()

    def nested():
        with safe_io.locked(path):
            with safe_io.locked(path):
                done.set()

    t = threading.Thread(target=nested)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "nested locked() deadlocked"


def test_failed_write_keeps_previous_file_and_no_temp(tmp_path):
    path = tmp_path / "state.json"
    safe_io.write_json_atomic(str(path), {"ok": 1})
    with pytest.raises(TypeError):
        safe_io.write_json_atomic(str(path), {"bad": object()})
    assert json.loads(path.read_text()) == {"ok": 1}
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_portfolio_save_is_atomic(tmp_path, monkeypatch):
    import pandas as pd

    import portfolio_store as ps

    monkeypatch.setattr(ps, "PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    df = pd.DataFrame([{"Ticker": "AAPL", "Side": "CALL", "Strike": 230.0, "Expiry": "2026-10-02",
                        "Quantity": 1, "Entry_Price": 1.2}])
    ps.save_portfolio(df)
    data = json.loads((tmp_path / "portfolio.json").read_text())
    assert data[0]["Ticker"] == "AAPL"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
