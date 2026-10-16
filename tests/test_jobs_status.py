"""Settings → Scheduler & background jobs: status, intervals, restart."""

import json
import os
import signal
import subprocess
from datetime import datetime

import pytest

import jobs_status as js

ET = js.ET


class _Run:
    """Fake subprocess.run: maps the first two args to (returncode, stdout)."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        for key, (code, out) in self.table.items():
            if tuple(args[:len(key)]) == key:
                return subprocess.CompletedProcess(args, code, out, "")
        raise FileNotFoundError(args[0])


LAUNCHCTL = ("PID\tStatus\tLabel\n"
             "4675\t0\tcom.optiontrading.scheduler\n"
             "-\t0\tcom.optiontrading.mark-runner\n"
             "-\t78\tcom.optiontrading.nightly\n"
             "123\t0\tcom.apple.something\n")


def test_plist_schedules_in_words():
    assert js.plist_schedule({"KeepAlive": True}).startswith("Always on")
    assert js.plist_schedule({"StartInterval": 900}) == "Every 15 min"
    weekdays = [{"Weekday": d, "Hour": 17, "Minute": 15} for d in range(1, 6)]
    assert js.plist_schedule({"StartCalendarInterval": weekdays}) == \
        "Mon–Fri 17:15 (Mac's local time)"
    assert js.plist_schedule({"StartCalendarInterval": {"Minute": 35}}) == "Hourly at :35"


def test_repo_plists_are_read():
    plists = js.read_plists()
    assert js.SCHEDULER_LABEL in plists
    assert js.plist_schedule(plists[js.SCHEDULER_LABEL]).startswith("Always on")
    assert js.plist_schedule(plists["com.optiontrading.mark-runner"]) == "Every 15 min"


def test_launchctl_list_is_parsed_and_filtered():
    jobs = js.launchctl_jobs(_Run({("launchctl", "list"): (0, LAUNCHCTL)}))
    assert jobs == {"com.optiontrading.scheduler": {"pid": 4675, "last_exit": 0},
                    "com.optiontrading.mark-runner": {"pid": None, "last_exit": 0},
                    "com.optiontrading.nightly": {"pid": None, "last_exit": 78}}
    assert js.launchctl_jobs(_Run({})) is None             # not macOS
    other = js.launchctl_jobs(_Run({("launchctl", "list"): (0, "77\t0\tcom.zmax.scanner.weekly\n")}),
                              {"com.zmax.scanner.weekly"})
    assert other == {"com.zmax.scanner.weekly": {"pid": 77, "last_exit": 0}}


def test_job_rows_say_running_waiting_failed_and_not_loaded():
    run = _Run({("launchctl", "list"): (0, LAUNCHCTL),
                ("ps", "-p"): (0, "Thu Oct 15 09:01:02 2026\n")})
    rows = {r["label"]: r for r in js.jobs(run)}
    assert rows[js.SCHEDULER_LABEL]["running"]
    assert rows[js.SCHEDULER_LABEL]["status"] == \
        "Running · pid 4675 · since Thu Oct 15 09:01:02 2026"
    assert rows["com.optiontrading.mark-runner"]["status"] == "Loaded · waiting for its next run"
    assert rows["com.optiontrading.nightly"]["status"] == "Loaded · last run exited with code 78"
    assert rows["com.optiontrading.health-check"]["status"] == "Not loaded"
    unknown = {r["label"]: r for r in js.jobs(_Run({}))}
    assert unknown[js.SCHEDULER_LABEL]["status"].startswith("Unknown")


def test_find_processes_matches_python_running_the_script():
    out = ("  501 /opt/anaconda3/bin/python telegram_bot.py\n"
           "  502 vim telegram_bot.py\n  503 /usr/bin/python3 scheduler.py\n")
    assert js.find_processes("telegram_bot.py", _Run({("ps", "-ax"): (0, out)})) == [501]


@pytest.fixture
def base(tmp_path):
    (tmp_path / "archive").mkdir()
    for name in ("AAPL_20261015_131502.json", "AAPL_20261015_132002.json",
                 "SPY_20260728_160929.json", "NVDA_20261001_100000.json"):
        (tmp_path / "archive" / name).write_text("{}")
    (tmp_path / "tickers_excluded.json").write_text('["NVDA"]')
    (tmp_path / "scheduler_config.json").write_text(json.dumps({
        "market_open": "09:30", "eod_time": "16:20", "default_interval_min": 5,
        "notify_telegram": True,
        "tickers": {"AAPL": {"interval_min": 5}, "SPY": {"interval_min": 15, "own_chat": True}}}))
    return tmp_path


def test_ticker_rows_during_the_session(base):
    now = datetime(2026, 10, 15, 13, 22, tzinfo=ET)
    rows = {r["ticker"]: r for r in js.ticker_rows(now, str(base),
                                                   keys={"TELEGRAM_CHAT_ID"})}
    assert set(rows) == {"AAPL", "SPY"}                      # NVDA excluded
    assert rows["AAPL"]["last_scan"] == "Thu Oct 15 13:20 ET"
    assert rows["AAPL"]["next_scan"] == "13:25 ET"
    assert rows["SPY"]["interval_min"] == 15 and rows["SPY"]["next_scan"] == "Due now"
    assert rows["AAPL"]["telegram"] == "Shared chat · shared bot"
    assert rows["SPY"]["telegram"] == "Silent: no TELEGRAM_CHAT_ID_SPY in .env yet"


def test_ticker_rows_after_hours_and_own_chat(base):
    now = datetime(2026, 10, 15, 21, 0, tzinfo=ET)
    keys = {"TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_SPY", "TELEGRAM_BOT_TOKEN_SPY"}
    rows = {r["ticker"]: r for r in js.ticker_rows(now, str(base), keys=keys)}
    assert rows["AAPL"]["next_scan"] == "Next session, from 09:30 ET"
    assert rows["SPY"]["telegram"] == "Own chat · own bot"


def test_env_keys_never_include_values_or_empty_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=secret\n B = x\nEMPTY=\n# C=1\n")
    assert js.env_keys(str(env)) == {"A", "B"}


def test_set_interval_keeps_the_rest_of_the_config(base):
    path = str(base / "scheduler_config.json")
    js.set_interval("spy", 10, path)
    cfg = json.loads(open(path).read())
    assert cfg["tickers"]["SPY"] == {"interval_min": 10, "own_chat": True}
    assert cfg["tickers"]["AAPL"] == {"interval_min": 5} and cfg["eod_time"] == "16:20"
    with pytest.raises(ValueError):
        js.set_interval("SPY", 0, path)


def test_restart_uses_launchctl_kickstart_first(tmp_path):
    run = _Run({("launchctl", "kickstart"): (0, "")})
    ok, msg = js.restart_scheduler(run, uid=501, pid_file=str(tmp_path / "none"))
    assert ok and run.calls[0] == ["launchctl", "kickstart", "-k",
                                   "gui/501/com.optiontrading.scheduler"]


def test_restart_falls_back_to_sigterm_on_the_pid(tmp_path):
    pid_file = tmp_path / "scheduler.pid"
    pid_file.write_text("4675")
    killed = []
    ok, msg = js.restart_scheduler(_Run({}), uid=501, pid_file=str(pid_file),
                                   kill=lambda pid, sig: killed.append((pid, sig)))
    assert ok and killed == [(4675, signal.SIGTERM)] and "4675" in msg


def test_restart_reports_when_nothing_is_running(tmp_path):
    ok, msg = js.restart_scheduler(_Run({}), uid=501, pid_file=str(tmp_path / "none"))
    assert not ok and "does not seem to be running" in msg

    def gone(pid, sig):
        raise ProcessLookupError
    (tmp_path / "p").write_text("99")
    ok, msg = js.restart_scheduler(_Run({}), uid=501, pid_file=str(tmp_path / "p"), kill=gone)
    assert not ok and "not running" in msg


def test_log_tail(tmp_path):
    log = tmp_path / "s.log"
    log.write_text("\n".join(f"line {i}" for i in range(40)) + "\n")
    assert js.log_tail(3, str(log)) == ["line 37", "line 38", "line 39"]
    assert js.log_tail(3, str(tmp_path / "missing")) == []


def test_settings_page_shows_the_panel():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_string(
        "from ui import scheduler_panel\nscheduler_panel.render()\n",
        default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("Scheduler" in e.label for e in at.expander)
