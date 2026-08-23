#!/usr/bin/env python3
"""
Schwab market-data probe (standalone — not wired into the scanner).

  python schwab.py login           # one-time OAuth; save tokens
  python schwab.py chain AAPL      # fetch option chain summary
  python schwab.py chain AAPL --raw

Credentials from .env:
  Client_ID_schwab, Client_Secret_schwab
  SCHWAB_REDIRECT_URI  (default https://127.0.0.1:8182)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
CHAINS_URL = "https://api.schwabapi.com/marketdata/v1/chains"

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
TOKEN_PATH = ROOT / "data" / "schwab_tokens.json"
DEFAULT_REDIRECT = "https://developer.schwab.com/oauth2-redirect.html"


def _load_env(path: Path = ENV_PATH) -> None:
    if not path.is_file():
        return
    # Always prefer .env for Schwab keys (callback URL changes often).
    prefer_file = {
        "Client_ID_schwab",
        "Client_Secret_schwab",
        "SCHWAB_CLIENT_ID",
        "SCHWAB_CLIENT_SECRET",
        "SCHWAB_REDIRECT_URI",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        if k in prefer_file or k not in os.environ:
            os.environ[k] = v


def _creds() -> tuple[str, str, str]:
    _load_env()
    client_id = (
        os.environ.get("Client_ID_schwab")
        or os.environ.get("SCHWAB_CLIENT_ID")
        or ""
    ).strip()
    client_secret = (
        os.environ.get("Client_Secret_schwab")
        or os.environ.get("SCHWAB_CLIENT_SECRET")
        or ""
    ).strip()
    redirect = (
        os.environ.get("SCHWAB_REDIRECT_URI") or DEFAULT_REDIRECT
    ).strip()
    if not client_id or not client_secret:
        sys.exit(
            "Missing Client_ID_schwab / Client_Secret_schwab in .env"
        )
    return client_id, client_secret, redirect


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _authorize_url(client_id: str, redirect: str) -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            # Schwab apps typically use the default API scope; omit if rejected.
        }
    )
    return f"{AUTH_URL}?{q}"


def _extract_code(pasted: str) -> str:
    text = pasted.strip().strip('"').strip("'")
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urllib.parse.urlparse(text)
        # Common mistake: pasting the authorize URL instead of the redirect.
        if "oauth/authorize" in (parsed.path or ""):
            sys.exit(
                "That is the AUTHORIZE url (step 1), not the redirect.\n"
                "Open that link in a browser, log in / Approve, then wait until the\n"
                "address bar shows https://127.0.0.1:8182/?code=XXXX — paste THAT.\n"
                "(The page will fail to load; copy from the address bar anyway.)"
            )
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" not in qs or not qs["code"]:
            # Some browsers put code in the fragment
            qs = urllib.parse.parse_qs(parsed.fragment)
        if "code" not in qs or not qs["code"]:
            sys.exit(
                "No ?code= found in the pasted URL.\n"
                "Need something like: https://127.0.0.1:8182/?code=AbC123...\n"
                f"Got host={parsed.hostname!r} path={parsed.path!r} query={parsed.query!r}"
            )
        return _normalize_auth_code(qs["code"][0])
    # Raw code pasted
    return _normalize_auth_code(text)


def _save_tokens(payload: dict[str, Any]) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    expires_in = float(payload.get("expires_in") or 1800)
    record = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": now + expires_in - 60,  # refresh 60s early
        "obtained_at": now,
    }
    # Preserve refresh_token if refresh response omits a new one
    if not record["refresh_token"] and TOKEN_PATH.is_file():
        old = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        record["refresh_token"] = old.get("refresh_token")
    TOKEN_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Tokens saved to {TOKEN_PATH} (do not commit)")


def _load_tokens() -> dict[str, Any]:
    if not TOKEN_PATH.is_file():
        sys.exit(f"No token file at {TOKEN_PATH}. Run: python schwab.py login")
    return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))


def _normalize_auth_code(code: str) -> str:
    """
    Schwab returns codes URL-encoded; the trailing '%40' must become '@'.
    parse_qs usually decodes already — normalize defensively.
    """
    c = (code or "").strip()
    # If still encoded, decode once
    if "%40" in c or "%2" in c.upper():
        c = urllib.parse.unquote(c)
    # Space from '+' mishandling — Schwab codes should not contain spaces
    c = c.replace(" ", "+")
    return c


def _is_real_auth_code(code: str) -> bool:
    """Reject placeholders like '...' from docs / truncated address bars."""
    c = (code or "").strip()
    if len(c) < 20:
        return False
    if c in (".", "..", "...") or set(c) <= {".", "…"}:
        return False
    return True


def _token_request(
    data: dict[str, str],
    client_id: str,
    client_secret: str,
    *,
    fatal: bool = True,
) -> dict[str, Any]:
    headers = {
        "Authorization": _basic_auth(client_id, client_secret),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        # Some Schwab edge responses mis-handle gzip clients
        "Accept-Encoding": "identity",
    }
    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)
    if not r.ok:
        print(f"Token request failed: HTTP {r.status_code}")
        print(r.text[:800])
        if "invalid_grant" in r.text or "expired" in r.text.lower():
            print(
                "\nSchwab auth codes expire in ~30–60 seconds. Re-run:\n"
                "  python schwab.py login\n"
                "When the browser warns about the certificate, click "
                "Advanced → Proceed immediately (do not wait)."
            )
        if fatal:
            sys.exit(1)
        raise RuntimeError(r.text[:800])
    return r.json()


def exchange_code(code: str, *, fatal: bool = True) -> dict[str, Any]:
    client_id, client_secret, redirect = _creds()
    code = _normalize_auth_code(code)
    print(f"Exchanging code (len={len(code)}, ends_with_@={code.endswith('@')})…")
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
        },
        client_id,
        client_secret,
        fatal=fatal,
    )


def refresh_access_token(tokens: dict[str, Any]) -> dict[str, Any]:
    client_id, client_secret, _ = _creds()
    rt = tokens.get("refresh_token")
    if not rt:
        sys.exit("No refresh_token on file. Run: python schwab.py login")
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": rt,
        },
        client_id,
        client_secret,
    )
    _save_tokens(payload)
    return _load_tokens()


def get_access_token() -> str:
    tokens = _load_tokens()
    if float(tokens.get("expires_at") or 0) <= time.time():
        print("Access token expired — refreshing…")
        tokens = refresh_access_token(tokens)
    return str(tokens["access_token"])


def fetch_chains(symbol: str, *, strike_count: int = 20) -> dict[str, Any]:
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {
        "symbol": symbol.upper(),
        "contractType": "ALL",
        "strikeCount": int(strike_count),
        "includeUnderlyingQuote": "true",
    }
    r = requests.get(CHAINS_URL, headers=headers, params=params, timeout=60)
    if r.status_code == 401:
        print("401 — refreshing token and retrying…")
        tokens = refresh_access_token(_load_tokens())
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        r = requests.get(CHAINS_URL, headers=headers, params=params, timeout=60)
    if not r.ok:
        print(f"Chain request failed: HTTP {r.status_code}")
        print(r.text[:800])
        sys.exit(1)
    return r.json()


def _iter_contracts(exp_map: dict[str, Any] | None):
    if not isinstance(exp_map, dict):
        return
    for exp_key, strikes in exp_map.items():
        expiry = str(exp_key).split(":", 1)[0]
        if not isinstance(strikes, dict):
            continue
        for _strike_key, contracts in strikes.items():
            if isinstance(contracts, dict):
                contracts = [contracts]
            if not isinstance(contracts, list):
                continue
            for c in contracts:
                if isinstance(c, dict):
                    yield expiry, c


def print_chain_summary(data: dict[str, Any], *, limit: int = 8) -> None:
    symbol = data.get("symbol")
    status = data.get("status")
    delayed = data.get("isDelayed")
    spot = data.get("underlyingPrice")
    calls = list(_iter_contracts(data.get("callExpDateMap")))
    puts = list(_iter_contracts(data.get("putExpDateMap")))

    print(f"\n=== Schwab OptionChain: {symbol} ===")
    print(f"status:           {status}")
    print(f"isDelayed:        {delayed}")
    if delayed is True:
        print("  (delayed — check Real-Time Market Data agreements on Schwab)")
    elif delayed is False:
        print("  (live quotes)")
    print(f"underlyingPrice:  {spot}")
    print(f"call contracts:   {len(calls)}")
    print(f"put contracts:    {len(puts)}")

    def _row(expiry: str, c: dict[str, Any]) -> str:
        strike = c.get("strikePrice", c.get("strike"))
        bid = c.get("bid", c.get("bidPrice"))
        ask = c.get("ask", c.get("askPrice"))
        last = c.get("last", c.get("lastPrice"))
        vol = c.get("totalVolume", c.get("volume"))
        oi = c.get("openInterest")
        delta = c.get("delta")
        iv = c.get("volatility")
        side = c.get("putCall", "?")
        return (
            f"  {side:4} {expiry}  K={strike}  "
            f"bid={bid} ask={ask} last={last}  "
            f"vol={vol} oi={oi}  delta={delta} iv={iv}"
        )

    print("\nSample CALLs:")
    for expiry, c in calls[:limit]:
        print(_row(expiry, c))
    print("\nSample PUTs:")
    for expiry, c in puts[:limit]:
        print(_row(expiry, c))


def _make_self_signed_cert(tmpdir: Path) -> tuple[Path, Path]:
    """Create a short-lived self-signed cert for https://127.0.0.1."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime as dt
    except ImportError:
        # Fallback: openssl CLI if present
        cert = tmpdir / "cert.pem"
        key = tmpdir / "key.pem"
        import subprocess
        subprocess.check_call(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "1", "-nodes",
                "-subj", "/CN=127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return cert, key

    key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert_obj = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key_obj.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key_obj, hashes.SHA256())
    )
    key_path = tmpdir / "key.pem"
    cert_path = tmpdir / "cert.pem"
    key_path.write_bytes(
        key_obj.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert_obj.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _code_from_path(path: str) -> str | None:
    """Pull ?code= from a callback path; decode %40 → @."""
    q = urllib.parse.urlparse(path).query
    # Prefer raw split so we control decoding (Schwab trailing %40 → @)
    for part in q.split("&"):
        if part.startswith("code="):
            return _normalize_auth_code(part[5:])
    qs = urllib.parse.parse_qs(q)
    if qs.get("code"):
        return _normalize_auth_code(qs["code"][0])
    return None


def _wait_for_redirect_and_exchange(
    redirect: str, timeout: float = 300.0
) -> dict[str, Any] | None:
    """
    Listen on the callback URL, then exchange the code IMMEDIATELY
    (Schwab codes expire in ~30–60s — cert warnings burn that window).
    """
    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    box: dict[str, Any] = {"payload": None, "error": None, "done": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            # Log length only — never echo the full code
            qlen = len(urllib.parse.urlparse(self.path).query)
            print(f"  ← browser hit path={self.path[:40]!r}… query_len={qlen}")
            code = _code_from_path(self.path)
            if not code or not _is_real_auth_code(code):
                # Favicon / cert probe / bare "/" / placeholder "..." — keep listening
                if code and not _is_real_auth_code(code):
                    print(
                        f"  (ignored fake/short code len={len(code)!r} — "
                        "do not open the example URL; wait for Schwab redirect)"
                    )
                body = (
                    b"<html><body><h2>Waiting for Schwab authorization</h2>"
                    b"<p>Do not type this URL yourself. Finish Login+Approve on "
                    b"Schwab; the browser will land here with a long ?code=.</p>"
                    b"</body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                # Exchange NOW — before the browser even paints the page
                print("Code received — exchanging immediately…")
                box["payload"] = exchange_code(code, fatal=False)
                body = (
                    b"<html><body><h2>Schwab login OK</h2>"
                    b"<p>Tokens saved. You can close this tab.</p></body></html>"
                )
                self.send_response(200)
            except Exception as exc:
                box["error"] = str(exc)
                body = (
                    b"<html><body><h2>Token exchange failed</h2>"
                    b"<p>See the terminal. Re-run python schwab.py login</p>"
                    b"</body></html>"
                )
                self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            box["done"] = True

        def log_message(self, fmt, *args):  # noqa: A003
            return

    try:
        httpd = HTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"(Could not bind {host}:{port}: {exc} — will ask you to paste the URL)")
        return None

    if parsed.scheme == "https":
        tmp = Path(tempfile.mkdtemp(prefix="schwab_oauth_"))
        try:
            cert, key = _make_self_signed_cert(tmp)
        except Exception as exc:
            httpd.server_close()
            print(f"(Could not create TLS cert: {exc} — will ask you to paste the URL)")
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"Listening on {redirect} for the OAuth redirect…")

    def _serve():
        httpd.timeout = 1.0
        deadline = time.time() + timeout
        while not box["done"] and time.time() < deadline:
            httpd.handle_request()
        httpd.server_close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    t.join(timeout + 5)
    if box["payload"] is not None:
        return box["payload"]
    if box["error"]:
        print(f"Callback error: {box['error']}")
    return None


def _is_local_callback(redirect: str) -> bool:
    host = (urllib.parse.urlparse(redirect).hostname or "").lower()
    return host in ("127.0.0.1", "localhost")


def cmd_login(*, open_browser: bool = True, paste_only: bool = False) -> None:
    client_id, _, redirect = _creds()
    url = _authorize_url(client_id, redirect)
    use_listener = (not paste_only) and _is_local_callback(redirect)

    print("\n=== Schwab OAuth login ===")
    print(f"Callback (must match Dev Portal exactly):\n  {redirect}\n")
    print("1) Open this authorize URL, log in, Approve:\n")
    print(url)
    print(
        "\n2) After Approve you land on the Schwab redirect page.\n"
        "   Copy the FULL address-bar URL (has ?code= then a LONG string).\n"
        "   Paste it below within ~30 seconds.\n"
    )

    payload: dict[str, Any] | None = None
    if use_listener:
        result: dict[str, Any] = {"payload": None}
        listen_thread = threading.Thread(
            target=lambda: result.__setitem__(
                "payload", _wait_for_redirect_and_exchange(redirect)
            ),
            daemon=True,
        )
        listen_thread.start()
        time.sleep(0.5)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        print("Local listener active — waiting for redirect…\n")
        listen_thread.join(timeout=310)
        payload = result["payload"]
    elif open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if payload is None:
        pasted = input("Paste redirect URL here: ").strip()
        if not pasted:
            sys.exit("No input")
        code = _extract_code(pasted)
        if not _is_real_auth_code(code):
            sys.exit(
                f"That is not a real Schwab code (len={len(code)}). "
                "Approve again, then copy the full address bar (not an example)."
            )
        payload = exchange_code(code)

    _save_tokens(payload)
    print("Login OK. Trying a tiny SPY chain…")
    data = fetch_chains("SPY", strike_count=1)
    print_chain_summary(data, limit=2)


def cmd_chain(symbol: str, *, raw: bool, strike_count: int) -> None:
    data = fetch_chains(symbol, strike_count=strike_count)
    if raw:
        text = json.dumps(data, indent=2)
        print(text[:12000])
        if len(text) > 12000:
            print(f"\n… truncated ({len(text)} chars total)")
    else:
        print_chain_summary(data)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Schwab market-data probe")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="OAuth login; save tokens")
    p_login.add_argument(
        "--no-browser", action="store_true", help="Do not open the browser"
    )
    p_login.add_argument(
        "--paste",
        action="store_true",
        help="Do not start local HTTPS listener; paste redirect URL only",
    )

    p_chain = sub.add_parser("chain", help="Fetch option chain")
    p_chain.add_argument("symbol", nargs="?", default="AAPL")
    p_chain.add_argument("--raw", action="store_true", help="Print JSON")
    p_chain.add_argument(
        "--strikes", type=int, default=20, help="strikeCount (default 20)"
    )

    args = p.parse_args(argv)
    if args.cmd == "login":
        cmd_login(
            open_browser=not args.no_browser,
            paste_only=args.paste,
        )
    elif args.cmd == "chain":
        cmd_chain(args.symbol, raw=args.raw, strike_count=args.strikes)
    else:
        p.error(f"unknown command {args.cmd}")


if __name__ == "__main__":
    main()
