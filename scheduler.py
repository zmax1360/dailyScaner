#!/usr/bin/env python3
"""
scheduler.py — Standalone options scanner scheduler.

Can be run directly OR auto-launched by app.py on startup.

  python3 scheduler.py          # run with defaults from scheduler_config.json
  python3 scheduler.py --once   # scan all tickers once and exit (useful for cron)

Behaviour
─────────
• Every 30 s it checks which tickers are due for a scan.
• Scans only run Mon–Fri between market_open and market_close (ET).
• Per-ticker intervals are read from scheduler_config.json.
• After each successful scan a Telegram alert is sent (if bot is configured).
• A PID file (scheduler.pid) prevents duplicate instances.
• All activity is written to scheduler.log AND stdout.

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

ET       = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "scheduler.pid")
LOG_FILE = os.path.join(BASE_DIR, "scheduler.log")
CFG_FILE = os.path.join(BASE_DIR, "scheduler_config.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
EXC_FILE = os.path.join(BASE_DIR, "tickers_excluded.json")

# ── Logging ────────────────────────────────────────────────────────────────────
# FileHandler only — app.py / nohup already redirect stdout to scheduler.log;
# adding StreamHandler as well causes every line to be written twice.
log = logging.getLogger("scheduler")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    # Console only when running interactively (not redirected to the log file)
    if sys.stdout.isatty():
        _sh = logging.StreamHandler(sys.stdout)
        _sh.setFormatter(_fmt)
        log.addHandler(_sh)

# ── Config helpers ─────────────────────────────────────────────────────────────
_DEFAULT_CFG: dict = {
    "market_open":          "09:30",
    "market_close":         "16:00",
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
def market_is_open(cfg: dict) -> bool:
    """
    Return True if scans should run right now.
    Window: market_open → market_close + post_close_buffer_min (ET).
    The extra buffer captures the yfinance 15-minute delayed feed that
    is only fully settled after the official close.
    """
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:          # Saturday / Sunday
        return False
    open_h,  open_m  = map(int, cfg["market_open"].split(":"))
    close_h, close_m = map(int, cfg["market_close"].split(":"))
    buffer = int(cfg.get("post_close_buffer_min", 15))

    # Extend close by buffer minutes
    close_total_min  = close_h * 60 + close_m + buffer
    close_h2, close_m2 = divmod(close_total_min, 60)

    t = now_et.time()
    return dtime(open_h, open_m) <= t < dtime(close_h2, close_m2)

# ── Telegram notify ────────────────────────────────────────────────────────────
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


def _notify(ticker: str, success: bool, env: dict, elapsed: float) -> None:
    """
    After a successful scan, send Best Value + Magnets + Volume by Expiry
    + Changes (spot / P/C) + Catalyst news. No bare "scan complete" message.
    On failure, send a short failure alert only.
    """
    token   = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    if not success:
        _send_telegram(
            token, chat_id,
            f"⚠️ <b>{ticker}</b> scan <b>FAILED</b>\n"
            f"<i>{now_et} · {elapsed:.0f}s</i>",
        )
        return

    try:
        # Reuse the bot's formatter so notification content matches interactive reports
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
                "deltas":        True,   # Changes on every notify
                "catalyst":      True,   # Live news / catalyst sentiment
                "session":       False,
                "mtf":           False,
                "orb":           False,
                "market_news":   False,
            },
            expiry_drill=[],
        )
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

# ── Scanner runner ─────────────────────────────────────────────────────────────
def run_scan(ticker: str, timeout: int = 300) -> tuple[bool, float]:
    """
    Invoke dailyScaner.py <ticker>.
    Returns (success, elapsed_seconds).
    """
    python = sys.executable
    scanner = os.path.join(BASE_DIR, "dailyScaner.py")
    start = time.monotonic()
    try:
        result = subprocess.run(
            [python, scanner, ticker],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            log.info(f"[{ticker}] scan OK ({elapsed:.0f}s)")
            return True, elapsed
        else:
            log.error(f"[{ticker}] scan FAILED (rc={result.returncode})\n"
                      f"  stderr: {result.stderr[-500:]}")
            return False, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        log.error(f"[{ticker}] scan TIMED OUT after {elapsed:.0f}s")
        return False, elapsed
    except Exception as exc:
        elapsed = time.monotonic() - start
        log.error(f"[{ticker}] scan error: {exc}")
        return False, elapsed

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

    try:
        while True:
            cfg     = load_config()
            env     = _load_env()
            tickers = discover_tickers()

            if not tickers:
                log.info("No tickers found — waiting …")
                if once:
                    break
                time.sleep(60)
                continue

            open_now = market_is_open(cfg)
            now_et   = datetime.now(ET).strftime("%H:%M ET")

            if not open_now and not once:
                log.info(f"Market closed ({now_et}) — sleeping 60 s")
                time.sleep(60)
                continue

            default_interval = int(cfg.get("default_interval_min", 5)) * 60
            notify           = bool(cfg.get("notify_telegram", True))
            now_mono         = time.monotonic()

            for ticker in tickers:
                ticker_cfg = (cfg.get("tickers") or {}).get(ticker, {})
                interval   = int(ticker_cfg.get("interval_min",
                                  cfg.get("default_interval_min", 5))) * 60

                last = last_scan.get(ticker, 0)
                due  = (now_mono - last) >= interval

                if due or once:
                    log.info(f"[{ticker}] starting scan "
                             f"(interval={interval//60}min, market={'open' if open_now else 'manual'})")
                    ok, elapsed = run_scan(ticker)
                    last_scan[ticker] = time.monotonic()
                    if notify:
                        _notify(ticker, ok, env, elapsed)

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
