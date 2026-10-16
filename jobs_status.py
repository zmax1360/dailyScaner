"""jobs_status — what the background jobs are doing, for the Settings page.

Read-only except for two explicit actions: restart the scheduler, and change one
ticker's scan interval. No Streamlit here, and it never imports scheduler.py (that
module attaches the scheduler's log file to whoever imports it).

    jobs          <- the launchd jobs in plist/ (com.optiontrading.*): how often each
                     runs (from its plist) and whether it is loaded / running now
                     (`launchctl list`)
    telegram bot  <- a running telegram_bot.py process, if any
    tickers       <- what the scheduler will scan: tickers with an archive that are
                     not in tickers_excluded.json, each with its interval, last scan
                     (newest archive), next scan, and where its Telegram goes
    restart       <- `launchctl kickstart -k` on the scheduler job; if that is not
                     available, SIGTERM to the pid in scheduler.pid (launchd's
                     KeepAlive starts it again)
"""

from __future__ import annotations

import glob
import json
import os
import plistlib
import signal
import subprocess
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")

SCHEDULER_LABEL = "com.optiontrading.scheduler"
JOB_NAMES = {
    SCHEDULER_LABEL: "Scheduler (scans + Telegram)",
    "com.optiontrading.mark-runner": "Mark runner (option marks)",
    "com.optiontrading.nightly": "Nightly job",
    "com.optiontrading.health-check": "Health check",
}
_DAYS = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}

Runner = Callable[..., subprocess.CompletedProcess]


def _run(args: list[str], run: Runner = subprocess.run) -> str | None:
    """stdout of a command, or None when it cannot run (not macOS, not found, error)."""
    try:
        res = run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


# ── launchd jobs ────────────────────────────────────────────────────────────

def plist_schedule(plist: dict) -> str:
    """How often a launchd job runs, in words, from its plist."""
    if plist.get("KeepAlive"):
        return "Always on (launchd restarts it if it stops)"
    if plist.get("StartInterval"):
        sec = int(plist["StartInterval"])
        return f"Every {sec // 60} min" if sec % 60 == 0 else f"Every {sec} s"
    cal = plist.get("StartCalendarInterval")
    if cal:
        items = cal if isinstance(cal, list) else [cal]
        times = sorted({(int(d.get("Hour", -1)), int(d.get("Minute", 0))) for d in items})
        days = sorted({int(d["Weekday"]) for d in items if "Weekday" in d})
        if times and all(h == -1 for h, _ in times):
            when = "Hourly at " + ", ".join(f":{m:02d}" for _, m in times)
        else:
            when = ", ".join(f"{h:02d}:{m:02d}" for h, m in times if h >= 0)
        if days == [1, 2, 3, 4, 5]:
            return f"Mon–Fri {when} (Mac's local time)"
        if days:
            return f"{'/'.join(_DAYS[d] for d in days)} {when} (Mac's local time)"
        return f"Every day {when} (Mac's local time)" if not when.startswith("Hourly") else when
    if plist.get("RunAtLoad"):
        return "Once, when loaded"
    return "Unknown"


def read_plists(plist_dir: str | None = None) -> dict[str, dict]:
    """{label: plist dict} for the job plists kept in the repo."""
    out: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(plist_dir or os.path.join(BASE_DIR, "plist"),
                                              "*.plist"))):
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        if data.get("Label"):
            out[str(data["Label"])] = data
    return out


def launchctl_jobs(run: Runner = subprocess.run) -> dict[str, dict] | None:
    """{label: {"pid": int | None, "last_exit": int | None}} for loaded
    com.optiontrading jobs. None when launchctl is not available."""
    text = _run(["launchctl", "list"], run)
    if text is None:
        return None
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3 or not parts[2].startswith("com.optiontrading."):
            continue
        pid = int(parts[0]) if parts[0].strip().lstrip("-").isdigit() and parts[0] != "-" else None
        code = int(parts[1]) if parts[1].strip().lstrip("-").isdigit() else None
        out[parts[2].strip()] = {"pid": pid, "last_exit": code}
    return out


def process_started(pid: int, run: Runner = subprocess.run) -> str | None:
    text = _run(["ps", "-p", str(pid), "-o", "lstart="], run)
    return " ".join(text.split()) if text and text.strip() else None


def find_processes(script: str, run: Runner = subprocess.run) -> list[int]:
    """Pids of running python processes whose command line runs ``script``."""
    text = _run(["ps", "-ax", "-o", "pid=,command="], run)
    pids = []
    for line in (text or "").splitlines():
        line = line.strip()
        pid, _, cmd = line.partition(" ")
        if script in cmd and "python" in cmd.lower() and pid.isdigit():
            pids.append(int(pid))
    return pids


def jobs(run: Runner = subprocess.run, plist_dir: str | None = None) -> list[dict[str, Any]]:
    """One row per known job: name, label, how often, status."""
    plists = read_plists(plist_dir)
    loaded = launchctl_jobs(run)
    rows = []
    for label in sorted(set(plists) | set(JOB_NAMES)):
        info = (loaded or {}).get(label)
        if loaded is None:
            status = "Unknown (launchctl not available)"
        elif info is None:
            status = "Not loaded"
        elif info["pid"]:
            started = process_started(info["pid"], run)
            status = f"Running · pid {info['pid']}" + (f" · since {started}" if started else "")
        elif info["last_exit"] in (0, None):
            status = "Loaded · waiting for its next run"
        else:
            status = f"Loaded · last run exited with code {info['last_exit']}"
        rows.append({"label": label, "name": JOB_NAMES.get(label, label),
                     "schedule": plist_schedule(plists.get(label, {})) if label in plists
                     else "No plist in the repo",
                     "status": status, "running": bool(info and info["pid"])})
    return rows


# ── what the scheduler scans ────────────────────────────────────────────────

def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def env_keys(env_path: str | None = None) -> set[str]:
    """The key names in .env (never the values)."""
    keys = set()
    try:
        with open(env_path or os.path.join(BASE_DIR, ".env")) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if v.strip().strip('"').strip("'"):
                        keys.add(k.strip())
    except FileNotFoundError:
        pass
    return keys


def _last_scan(ticker: str, archive_dir: str) -> datetime | None:
    stamps = []
    for path in glob.glob(os.path.join(archive_dir, f"{ticker}_*.json")):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            stamps.append(datetime.strptime(stem.split("_", 1)[1], "%Y%m%d_%H%M%S"))
        except (IndexError, ValueError):
            continue
    return max(stamps).replace(tzinfo=ET) if stamps else None


def telegram_route(ticker: str, cfg: dict, keys: set[str]) -> str:
    own = (cfg.get("tickers") or {}).get(ticker) or {}
    if not cfg.get("notify_telegram", True) or own.get("notify") is False:
        return "Off (recorded only)"
    t = ticker.upper()
    bot = "own bot" if f"TELEGRAM_BOT_TOKEN_{t}" in keys else "shared bot"
    if f"TELEGRAM_CHAT_ID_{t}" in keys:
        return f"Own chat · {bot}"
    if own.get("own_chat"):
        return f"Silent: no TELEGRAM_CHAT_ID_{t} in .env yet"
    return f"Shared chat · {bot}" if "TELEGRAM_CHAT_ID" in keys else "No chat set in .env"


def _hhmm(s: str, default: time) -> time:
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except Exception:
        return default


def scan_window(cfg: dict) -> tuple[time, time]:
    return _hhmm(cfg.get("market_open"), time(9, 30)), _hhmm(cfg.get("eod_time"), time(16, 20))


def next_scan(last: datetime | None, interval_min: int, cfg: dict, now: datetime) -> str:
    start, end = scan_window(cfg)
    in_window = now.weekday() < 5 and start <= now.time() < end
    if not in_window:
        return f"Next session, from {start:%H:%M} ET"
    if last is None:
        return "Within a minute"
    due = last + timedelta(minutes=interval_min)
    return "Due now" if due <= now else f"{due:%H:%M} ET"


def ticker_rows(now: datetime | None = None, base_dir: str | None = None,
                keys: set[str] | None = None) -> list[dict[str, Any]]:
    base = base_dir or BASE_DIR
    cfg = _read_json(os.path.join(base, "scheduler_config.json"), {})
    excluded = set(_read_json(os.path.join(base, "tickers_excluded.json"), []) or [])
    archive_dir = os.path.join(base, "archive")
    tickers = sorted({os.path.basename(p).split("_")[0]
                      for p in glob.glob(os.path.join(archive_dir, "*_*_*.json"))} - excluded)
    keys = env_keys(os.path.join(base, ".env")) if keys is None else keys
    now = now or datetime.now(ET)
    rows = []
    for t in tickers:
        own = (cfg.get("tickers") or {}).get(t) or {}
        interval = int(own.get("interval_min", cfg.get("default_interval_min", 5)))
        last = _last_scan(t, archive_dir)
        rows.append({"ticker": t, "interval_min": interval,
                     "last_scan": f"{last:%a %b %d %H:%M} ET" if last else "Never",
                     "next_scan": next_scan(last, interval, cfg, now),
                     "telegram": telegram_route(t, cfg, keys)})
    return rows


def set_interval(ticker: str, minutes: int, cfg_path: str | None = None) -> None:
    """Change one ticker's scan interval in scheduler_config.json, keeping everything
    else. The scheduler re-reads the file every 30 seconds: no restart needed."""
    if not 1 <= int(minutes) <= 240:
        raise ValueError("The interval must be between 1 and 240 minutes.")
    path = cfg_path or os.path.join(BASE_DIR, "scheduler_config.json")
    cfg = _read_json(path, {})
    cfg.setdefault("tickers", {}).setdefault(ticker.upper(), {})["interval_min"] = int(minutes)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ── restart ─────────────────────────────────────────────────────────────────

def restart_scheduler(run: Runner = subprocess.run, uid: int | None = None,
                      pid_file: str | None = None,
                      kill: Callable[[int, int], None] = os.kill) -> tuple[bool, str]:
    """(ok, what happened). launchd stops the job's child processes too, so a scan in
    progress is cut off; it runs again on the next cycle."""
    uid = os.getuid() if uid is None else uid
    if _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{SCHEDULER_LABEL}"], run) is not None:
        return True, "Restarted by launchd. The new scheduler picks up all code changes."
    try:
        with open(pid_file or os.path.join(BASE_DIR, "scheduler.pid")) as fh:
            pid = int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return False, ("launchctl could not restart it and there is no scheduler.pid: "
                       "the scheduler does not seem to be running.")
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False, f"scheduler.pid names pid {pid}, which is not running."
    except PermissionError:
        return False, f"Not allowed to stop pid {pid}."
    return True, (f"Stopped pid {pid}. If it runs under launchd (KeepAlive), it starts "
                  "again within seconds; otherwise start it yourself.")


def log_tail(n: int = 25, path: str | None = None) -> list[str]:
    try:
        with open(path or os.path.join(BASE_DIR, "logs", "scheduler.log"),
                  errors="replace") as fh:
            return [line.rstrip("\n") for line in fh.readlines()[-n:]]
    except FileNotFoundError:
        return []
