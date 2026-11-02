"""Anmeldung bei LinkedIn (OAuth 2.0, 3-legged) und Token-Speicherung.

Ablauf: Browser öffnet die LinkedIn-Anmeldung, LinkedIn leitet auf
http://localhost:8765/callback zurück, ein kurzlebiger lokaler Webserver nimmt
den Code entgegen und tauscht ihn gegen Zugriffs- und Erneuerungstoken.

Tokens liegen im macOS-Schlüsselbund (über `keyring`). Ist kein Schlüsselbund
verfügbar, in ~/.linkedin-ads-mcp/token.json (nur für den eigenen Benutzer lesbar).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"

# Muss exakt der bei LinkedIn registrierten Adresse entsprechen.
REDIRECT_HOST = "localhost"
REDIRECT_PORT = int(os.environ.get("LINKEDIN_REDIRECT_PORT", "8765"))
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

# Bewusst nur lesend. Ein zusätzliches Recht (z. B. rw_ads) erzwingt eine neue
# Anmeldung und entwertet bei LinkedIn bestehende Tokens derselben App.
SCOPES = ["r_ads", "r_ads_reporting"]

KEYRING_SERVICE = "betamodus-linkedin-ads-mcp"
KEYRING_USER = "token"
FALLBACK_FILE = Path.home() / ".linkedin-ads-mcp" / "token.json"

# Tokens 5 Minuten vor Ablauf erneuern.
EXPIRY_MARGIN_S = 300


class AuthError(RuntimeError):
    pass


def _log(msg: str) -> None:
    # stdout gehört dem MCP-Protokoll, deshalb nur stderr.
    print(f"[linkedin-ads-mcp] {msg}", file=sys.stderr, flush=True)


def _client_credentials() -> tuple[str, str]:
    cid = os.environ.get("LINKEDIN_CLIENT_ID", "").strip()
    secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise AuthError(
            "LINKEDIN_CLIENT_ID und LINKEDIN_CLIENT_SECRET fehlen. "
            "Beide in der MCP-Konfiguration unter \"env\" eintragen."
        )
    return cid, secret


# --- Token-Speicher -----------------------------------------------------------

def _keyring():
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def load_token() -> dict | None:
    kr = _keyring()
    raw = None
    if kr:
        try:
            raw = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception as e:  # Schlüsselbund gesperrt o. Ä.
            _log(f"Schlüsselbund nicht lesbar: {e}")
    if raw is None and FALLBACK_FILE.exists():
        raw = FALLBACK_FILE.read_text()
    return json.loads(raw) if raw else None


def save_token(token: dict) -> None:
    raw = json.dumps(token)
    kr = _keyring()
    if kr:
        try:
            kr.set_password(KEYRING_SERVICE, KEYRING_USER, raw)
            return
        except Exception as e:
            _log(f"Schlüsselbund nicht beschreibbar, nutze Datei: {e}")
    FALLBACK_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    FALLBACK_FILE.write_text(raw)
    FALLBACK_FILE.chmod(0o600)


def delete_token() -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass
    if FALLBACK_FILE.exists():
        FALLBACK_FILE.unlink()


def _normalize(resp: dict, previous: dict | None = None) -> dict:
    now = time.time()
    token = {
        "access_token": resp["access_token"],
        "expires_at": now + int(resp.get("expires_in", 0)),
        "scope": resp.get("scope", " ".join(SCOPES)),
    }
    if resp.get("refresh_token"):
        token["refresh_token"] = resp["refresh_token"]
        token["refresh_expires_at"] = now + int(resp.get("refresh_token_expires_in", 0))
    elif previous and previous.get("refresh_token"):
        token["refresh_token"] = previous["refresh_token"]
        token["refresh_expires_at"] = previous.get("refresh_expires_at", 0)
    return token


# --- Anmeldung im Browser -----------------------------------------------------

_CALLBACK_HTML = (
    "<!doctype html><meta charset=utf-8><title>LinkedIn-Anmeldung</title>"
    "<body style='font-family:sans-serif;padding:40px'>"
    "<h2>{title}</h2><p>{text}</p></body>"
)


async def login_interactive(timeout_s: int = 180) -> dict:
    """Öffnet die LinkedIn-Anmeldung im Browser und wartet auf den Rücksprung."""
    cid, secret = _client_credentials()
    state = secrets.token_urlsafe(24)
    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = (await reader.readline()).decode(errors="replace")
            # Restliche Header verwerfen
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            path = line.split(" ")[1] if " " in line else "/"
            parsed = urlparse(path)
            if parsed.path != "/callback":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            q = parse_qs(parsed.query)
            if q.get("state", [None])[0] != state:
                body, ok = ("Ungültige Anfrage", "Der Sicherheitswert passt nicht. Bitte erneut anmelden."), False
                err = AuthError("state stimmt nicht überein")
            elif "error" in q:
                desc = q.get("error_description", q["error"])[0]
                body, ok = ("Anmeldung abgebrochen", desc), False
                err = AuthError(f"LinkedIn meldet: {desc}")
            else:
                body, ok, err = ("Angemeldet", "Du kannst dieses Fenster schließen."), True, None
            html = _CALLBACK_HTML.format(title=body[0], text=body[1]).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(html)}\r\nConnection: close\r\n\r\n".encode()
                + html
            )
            await writer.drain()
            if not result.done():
                if ok:
                    result.set_result(q["code"][0])
                else:
                    result.set_exception(err)
        finally:
            writer.close()

    # "localhost" kann im Browser auf IPv4 oder IPv6 auflösen, daher beide.
    server = None
    for hosts in (["127.0.0.1", "::1"], ["127.0.0.1"]):
        try:
            server = await asyncio.start_server(handle, hosts, REDIRECT_PORT)
            break
        except OSError as e:
            last_err = e
    if server is None:
        raise AuthError(
            f"Port {REDIRECT_PORT} ist belegt ({last_err}). Läuft schon eine Anmeldung?"
        ) from last_err

    url = AUTH_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": " ".join(SCOPES),
        }
    )
    _log("Öffne LinkedIn-Anmeldung im Browser …")
    if not webbrowser.open(url):
        _log(f"Browser ließ sich nicht öffnen. Bitte manuell aufrufen:\n{url}")

    try:
        async with server:
            code = await asyncio.wait_for(result, timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise AuthError(
            f"Keine Anmeldung innerhalb von {timeout_s} Sekunden. Bitte erneut versuchen."
        ) from e

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": cid,
                "client_secret": secret,
            },
        )
    if r.status_code != 200:
        raise AuthError(f"Token-Tausch fehlgeschlagen ({r.status_code}): {r.text}")
    token = _normalize(r.json())
    save_token(token)
    _log("Anmeldung gespeichert.")
    return token


async def refresh(token: dict) -> dict:
    cid, secret = _client_credentials()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": cid,
                "client_secret": secret,
            },
        )
    if r.status_code != 200:
        raise AuthError(f"Token-Erneuerung fehlgeschlagen ({r.status_code}): {r.text}")
    new = _normalize(r.json(), previous=token)
    save_token(new)
    _log("Zugriffstoken erneuert.")
    return new


async def get_access_token() -> str:
    """Gültiges Zugriffstoken liefern, wenn nötig erneuern. Meldet sich NICHT
    selbst an, damit kein Werkzeugaufruf unbemerkt minutenlang blockiert."""
    token = load_token()
    if not token:
        raise AuthError(
            "Nicht angemeldet. Werkzeug `login` aufrufen oder im Terminal "
            "`linkedin-ads-mcp login` ausführen."
        )
    now = time.time()
    if token["expires_at"] - EXPIRY_MARGIN_S > now:
        return token["access_token"]
    if token.get("refresh_token") and token.get("refresh_expires_at", 0) > now:
        return (await refresh(token))["access_token"]
    raise AuthError("Anmeldung abgelaufen. Bitte erneut anmelden (Werkzeug `login`).")


def token_status() -> dict:
    token = load_token()
    if not token:
        return {"angemeldet": False}
    now = time.time()

    def days(ts):
        return round((ts - now) / 86400, 1) if ts else None

    return {
        "angemeldet": True,
        "rechte": token.get("scope"),
        "zugriffstoken_gueltig_noch_tage": days(token.get("expires_at")),
        "erneuerung_moeglich_noch_tage": days(token.get("refresh_expires_at")),
    }
