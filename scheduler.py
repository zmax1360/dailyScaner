#!/usr/bin/env python3
"""
scheduler.py — Standalone options scanner scheduler.

Can be run directly OR auto-launched by app.py on startup.

  python3 scheduler.py          # run with defaults from scheduler_config.json
  python3 scheduler.py --once   # scan all tickers once and exit (useful for cron)

Behaviour
─────────
• Every 30 s it checks which tickers are due for a scan.
• Scans only run Mon–Fri between market_open and eod_time (ET clocks only; F-23).
• At eod_time (default 16:20 ET) one --eod scan per ticker runs with volume convergence.
• Per-ticker intervals are read from scheduler_config.json.
• After each successful scan a Telegram alert is sent (if bot is configured).
• A PID file (scheduler.pid) prevents duplicate instances.
• All activity is written to logs/scheduler.log (rotating).

scheduler_config.json schema
─────────────────────────────
{
  "market_open":          "09:30",   # ET, HH:MM
  "market_close":         "16:00",   # ET, HH:MM
  "default_interval_min": 5,         # minutes between scans (all tickers)
  "notify_telegram":      true,      # send Telegram alert after each scan
  "tickers": {                       # per-ticker overrides (optional)
    "AAPL": { "interval_min": 3  },
    "TSLA": { "interval_min": 10 }
  }
}
"""

import argparse
import glob
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from logging_config import LOG_DIR, setup_logging

ET       = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "scheduler.pid")
CFG_FILE = os.path.join(BASE_DIR, "scheduler_config.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
EXC_FILE = os.path.join(BASE_DIR, "tickers_excluded.json")
LOG_FILE = os.path.join(LOG_DIR, "scheduler.log")

# ── Logging ────────────────────────────────────────────────────────────────────
# Rotating logs/scheduler.log — console echo only on a TTY (avoids double lines
# when app.py / launchd already redirect stdout).
log = setup_logging("scheduler")

# ── Config helpers ─────────────────────────────────────────────────────────────
_DEFAULT_CFG: dict = {
    "market_open":          "09:30",
    "market_close":         "16:00",
    "eod_time":             "16:20",  # ET — last daily run (configurable)
    "post_close_buffer_min": 20,     # used only if eod_time omitted
    "default_interval_min": 5,
    "notify_telegram":      True,
    "tickers":              {},
}


def load_config() -> dict:
    cfg = dict(_DEFAULT_CFG)
    try:
        with open(CFG_FILE) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning(f"Could not read {CFG_FILE}: {exc} — using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    with open(CFG_FILE, "w") as fh:
        json.dump(cfg, fh, indent=2)


def _load_env() -> dict:
    out: dict = {}
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _load_excluded() -> set[str]:
    try:
        with open(EXC_FILE) as fh:
            data = json.load(fh)
            return set(data if isinstance(data, list) else [])
    except Exception:
        return set()

# ── Ticker discovery ───────────────────────────────────────────────────────────
def discover_tickers() -> list[str]:
    """Return tickers that have at least one archive JSON and are not excluded."""
    excluded = _load_excluded()
    tickers:  set[str] = set()
    for path in glob.glob(os.path.join(BASE_DIR, "archive", "*.json")):
        parts = os.path.basename(path).split("_")
        if len(parts) >= 3:
            tickers.add(parts[0])
    return sorted(tickers - excluded)

# ── Market hours ───────────────────────────────────────────────────────────────
def _now_et() -> datetime:
    """Always America/New_York — never the machine local clock (F-23)."""
    return datetime.now(ET)


def market_is_open(cfg: dict, now_et: datetime | None = None) -> bool:
    """
    Return True if scans should run right now (ET clock).

    Window: market_open → eod_time (default 16:20 ET). Prefer explicit
    ``eod_time`` over market_close + post_close_buffer so the last slot is
    configurable without hardcoding.
    """
    from eod_settlement import scan_window_end_et

    now_et = now_et or _now_et()
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    now_et = now_et.astimezone(ET)
    if now_et.weekday() >= 5:          # Saturday / Sunday
        return False
    open_h, open_m = map(int, str(cfg.get("market_open", "09:30")).split(":"))
    end = scan_window_end_et(cfg)
    # Inclusive of the eod_time minute so the EOD run can start at 16:20
    end_exclusive = (
        dtime(end.hour, end.minute + 1)
        if end.minute < 59
        else dtime(end.hour + 1, 0)
    )
    t = now_et.time()
    return dtime(open_h, open_m) <= t < end_exclusive


def should_run_eod(cfg: dict, now_et: datetime | None = None) -> bool:
    """True once we have reached configured eod_time ET today."""
    from eod_settlement import is_eod_slot
    return is_eod_slot(now_et or _now_et(), cfg)

# ── Telegram notify ────────────────────────────────────────────────────────────
# Process-local refusal streak (resets on success; one alert at streak==3).
_refusal_streak: dict[str, int] = {}
_refusal_alert_sent: dict[str, bool] = {}


def _reset_refusal_streak(ticker: str) -> None:
    _refusal_streak[ticker] = 0
    _refusal_alert_sent[ticker] = False


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning(f"Telegram notify failed: {exc}")
        return False


def _notify_success(ticker: str, env: dict, elapsed: float) -> None:
    """After exit 0 only — format from the archive the scan just wrote."""
    token   = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    try:
        from telegram_bot import _fmt_report, _load_latest
        payload, prev = _load_latest(ticker)
        if not payload:
            log.warning(f"[{ticker}] notify: no archive found after scan")
            return
        top_n = int((load_config().get("tickers") or {})
                    .get(ticker, {})
                    .get("notify_top_n", 5))
        text = _fmt_report(
            payload, prev, ticker, top_n,
            include={
                "best_value":    True,
                "magnets":       True,
                "volume_expiry": True,
                "deltas":        True,
                "catalyst":      True,
                "session":       False,
                "mtf":           False,
                "orb":           False,
                "market_news":   False,
            },
            expiry_drill=[],
        )
        if not text or not text.strip():
            log.info(f"[{ticker}] notify skipped (empty report after gates)")
            return
        ok = _send_telegram(token, chat_id, text)
        if ok:
            log.info(
                f"[{ticker}] notify sent "
                f"(Best Value + Magnets + Vol/Expiry + Changes + Catalyst)"
            )
        else:
            log.warning(f"[{ticker}] notify send failed")
    except Exception as exc:
        log.warning(f"[{ticker}] notify error: {exc}")


def _notify_refusal_streak(ticker: str, reason: str | None, env: dict) -> None:
    """One alert when consecutive refusals hit 3 — then silent until success."""
    from notify_delivery import REFUSAL_STREAK_ALERT_AT

    token   = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    why = reason or "unknown"
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    text = (
        f"⚠️ <b>{ticker}</b> scanner stopped publishing\n"
        f"{REFUSAL_STREAK_ALERT_AT} consecutive refusals "
        f"(<code>{why}</code>).\n"
        f"No Best Value alerts until a scan succeeds.\n"
        f"<i>{now_et}</i>"
    )
    if _send_telegram(token, chat_id, text):
        log.info(f"[{ticker}] refusal-streak notify sent (reason={why})")
    else:
        log.warning(f"[{ticker}] refusal-streak notify failed")


def handle_scan_result(
    ticker: str,
    exit_code: int,
    reason: str | None,
    elapsed: float,
    *,
    notify: bool,
    env: dict,
) -> None:
    """
    Log scan outcome and decide whether to notify.

    exit 0 → OK + notify
    exit 3 → REFUSED, skip notify (one streak alert at 3 consecutive)
    else   → FAILED, skip notify
    """
    from notify_delivery import REFUSAL_STREAK_ALERT_AT

    if exit_code == 0:
        _reset_refusal_streak(ticker)
        log.info(f"[{ticker}] scan OK ({elapsed:.0f}s)")
        if notify:
            _notify_success(ticker, env, elapsed)
        return

    if exit_code == 3:
        why = reason or "unknown"
        streak = _refusal_streak.get(ticker, 0) + 1
        _refusal_streak[ticker] = streak
        log.info(f"[{ticker}] scan REFUSED ({why}) ({elapsed:.0f}s)")
        if (
            notify
            and streak == REFUSAL_STREAK_ALERT_AT
            and not _refusal_alert_sent.get(ticker)
        ):
            _notify_refusal_streak(ticker, why, env)
            _refusal_alert_sent[ticker] = True
        return

    log.error(
        f"[{ticker}] scan FAILED (rc={exit_code}) ({elapsed:.0f}s)"
        + (f" reason={reason}" if reason else "")
    )


# ── Scanner runner ─────────────────────────────────────────────────────────────
def run_scan(
    ticker: str, timeout: int = 300, *, eod: bool = False,
) -> tuple[int, str | None, float]:
    """
    Invoke dailyScaner.py <ticker> [--eod].

    Returns (exit_code, abort_reason|None, elapsed_seconds).
      0 — success
      3 — deliberate refusal (ABORT_REASON on stderr)
      1 — failure / timeout / exception
    """
    from notify_delivery import parse_abort_reason

    python = sys.executable
    scanner = os.path.join(BASE_DIR, "dailyScaner.py")
    cmd = [python, scanner, ticker]
    if eod:
        cmd.append("--eod")
    start = time.monotonic()
    env = os.environ.copy()
    env.setdefault("OPTIONTRADING_PROCESS", "scheduler")
    try:
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.monotonic() - start
        reason = parse_abort_reason(result.stderr)
        if result.returncode not in (0, 3) and result.stderr:
            log.error(
                f"[{ticker}] scan FAILED (rc={result.returncode})\n"
                f"  stderr: {result.stderr[-500:]}"
            )
        return int(result.returncode), reason, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        log.error(f"[{ticker}] scan TIMED OUT after {elapsed:.0f}s")
        return 1, None, elapsed
    except Exception as exc:
        elapsed = time.monotonic() - start
        log.error(f"[{ticker}] scan error: {exc}")
        return 1, None, elapsed

# ── PID management ─────────────────────────────────────────────────────────────
def _write_pid() -> None:
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))


def _clear_pid() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def already_running() -> bool:
    """Return True if another live scheduler instance owns the PID file."""
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
        if pid == os.getpid():
            return False
        os.kill(pid, 0)   # raises if process does not exist
        try:
            cmd = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="], text=True
            ).strip()
            if "scheduler.py" not in cmd:
                log.warning(
                    f"Stale pidfile pid={pid} is not scheduler.py ({cmd!r}) — ignoring"
                )
                return False
            state = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "state="], text=True
            ).strip()
            if state.startswith("T"):
                log.warning(f"Found suspended scheduler pid={pid} — treating as not running")
                return False
        except Exception:
            return False
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False


_LOCK_FH = None


def _acquire_lock() -> bool:
    """Exclusive lock so two launches can't both pass already_running()."""
    global _LOCK_FH
    lock_path = PID_FILE + ".lock"
    try:
        import fcntl
        _LOCK_FH = open(lock_path, "w")
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH.write(str(os.getpid()))
        _LOCK_FH.flush()
        return True
    except BlockingIOError:
        return False
    except Exception as exc:
        log.warning(f"Lock acquire failed ({exc}) — continuing with PID check only")
        return True

# ── Main loop ──────────────────────────────────────────────────────────────────
def main(once: bool = False) -> None:
    if already_running():
        log.warning("Scheduler already running — exiting.")
        sys.exit(0)

    if not _acquire_lock():
        log.warning("Scheduler lock held by another process — exiting.")
        sys.exit(0)

    # Re-check after lock (TOCTOU)
    if already_running():
        log.warning("Scheduler already running after lock — exiting.")
        sys.exit(0)

    _write_pid()
    log.info(f"Scheduler started (pid={os.getpid()})")
    log.info(f"Config: {CFG_FILE}")
    log.info(f"Log:    {LOG_FILE}")

    def _shutdown(sig, frame):
        log.info("Scheduler stopping …")
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # last_scan[ticker] = monotonic time of last scan
    last_scan: dict[str, float] = {}
    # eod_done_day[ticker] = YYYY-MM-DD ET when EOD already ran
    eod_done_day: dict[str, str] = {}

    try:
        while True:
            cfg     = load_config()
            env     = _load_env()
            tickers = discover_tickers()
            now_dt  = _now_et()
            today_s = now_dt.date().isoformat()

            if not tickers:
                log.info("No tickers found — waiting …")
                if once:
                    break
                time.sleep(60)
                continue

            open_now = market_is_open(cfg, now_dt)
            now_et   = now_dt.strftime("%H:%M ET")
            eod_now  = should_run_eod(cfg, now_dt)
            # Convergence can run past eod_time; keep scheduling until each
            # ticker has completed its once-per-day EOD for today (ET).
            pending_eod = eod_now and any(
                eod_done_day.get(t) != today_s for t in tickers
            )

            if not open_now and not pending_eod and not once:
                log.info(f"Market closed ({now_et}) — sleeping 60 s")
                time.sleep(60)
                continue

            notify   = bool(cfg.get("notify_telegram", True))
            now_mono = time.monotonic()

            for ticker in tickers:
                ticker_cfg = (cfg.get("tickers") or {}).get(ticker, {})
                interval   = int(ticker_cfg.get("interval_min",
                                  cfg.get("default_interval_min", 5))) * 60

                # One EOD run per ticker per ET day once eod_time is reached
                if eod_now and eod_done_day.get(ticker) != today_s and not once:
                    log.info(f"[{ticker}] starting EOD scan @ {now_et}")
                    # Convergence: up to 3 reads with 10 min gaps → ~25 min
                    code, reason, elapsed = run_scan(
                        ticker, timeout=2400, eod=True,
                    )
                    eod_done_day[ticker] = today_s
                    last_scan[ticker] = time.monotonic()
                    handle_scan_result(
                        ticker, code, reason, elapsed,
                        notify=notify, env=env,
                    )
                    continue

                last = last_scan.get(ticker, 0)
                due  = (now_mono - last) >= interval

                # Skip new intraday scans once we are in/after the EOD slot
                if eod_now and not once:
                    continue
                if not open_now and not once:
                    continue

                if due or once:
                    mode = "manual" if once else "open"
                    log.info(
                        f"[{ticker}] starting scan "
                        f"(interval={interval//60}min, market={mode})"
                    )
                    code, reason, elapsed = run_scan(ticker, eod=False)
                    last_scan[ticker] = time.monotonic()
                    handle_scan_result(
                        ticker, code, reason, elapsed,
                        notify=notify, env=env,
                    )
            if once:
                break

            time.sleep(30)   # check again in 30 s

    except Exception as exc:
        log.exception(f"Fatal error: {exc}")
    finally:
        _clear_pid()
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Options Scanner Scheduler")
    parser.add_argument(
        "--once", action="store_true",
        help="Scan all tickers once and exit (useful for cron)",
    )
    args = parser.parse_args()
    main(once=args.once)
