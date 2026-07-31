"""Shared logging_config helper."""

from __future__ import annotations

import logging
from pathlib import Path

from logging_config import LOG_DIR, ETFormatter, setup_logging, strip_ansi


def test_strip_ansi():
    assert strip_ansi("\033[92mhi\033[0m") == "hi"


def test_setup_logging_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setattr("logging_config.LOG_DIR", str(tmp_path))
    logging_config = __import__("logging_config")
    logging_config._configured.clear()
    # Clear root handlers from prior tests
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    log = setup_logging("unit_test", console=False)
    log.info("hello from unit_test")
    logging.getLogger("child.mod").warning("from child")

    path = Path(tmp_path) / "unit_test.log"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "hello from unit_test" in text
    assert "[unit_test]" in text
    assert "[child.mod]" in text
    assert "WARNING" in text


def test_et_formatter_uses_eastern():
    fmt = ETFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S %Z")
    record = logging.LogRecord(
        "n", logging.INFO, __file__, 1, "msg", (), None,
    )
    ts = fmt.formatTime(record, datefmt="%Z")
    assert ts in ("EST", "EDT")
