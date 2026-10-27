"""ui.archives spot fallback: an after-hours scan archive can hold spot NaN."""
import json

from ui.archives import latest_usable_spot, usable_spot


def _write(folder, name, spot):
    (folder / name).write_text(json.dumps({"spot": spot}))


def test_usable_spot():
    assert usable_spot(343.02) == 343.02
    assert usable_spot(float("nan")) is None
    assert usable_spot(None) is None
    assert usable_spot(0) is None
    assert usable_spot("x") is None


def test_latest_usable_spot_skips_nan_archive(tmp_path, monkeypatch):
    arch = tmp_path / "archive"
    arch.mkdir()
    _write(arch, "AAPL_20261026_163016.json", 343.02)
    (arch / "AAPL_20261026_200234.json").write_text('{"spot": NaN}')   # what the scan wrote
    monkeypatch.chdir(tmp_path)
    assert latest_usable_spot("AAPL") == (343.02, "AAPL_20261026_163016.json")


def test_latest_usable_spot_none_when_nothing_usable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert latest_usable_spot("AAPL") == (None, None)
