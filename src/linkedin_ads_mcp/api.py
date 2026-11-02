"""Dünner Client für die LinkedIn Marketing API (versionierte REST-Endpunkte)."""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from .auth import get_access_token

BASE = "https://api.linkedin.com/rest"

# LinkedIn unterstützt jede Version mindestens ein Jahr. Jährlich nachziehen.
LINKEDIN_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202610")

# LinkedIn liefert pro adAnalytics-Antwort höchstens so viele Zeilen, ohne Seiten.
ANALYTICS_MAX_ELEMENTS = 15000


class LinkedInError(RuntimeError):
    pass


def enc(value: str) -> str:
    """URN o. Ä. für Rest.li 2.0 kodieren (Doppelpunkte als %3A)."""
    return quote(str(value), safe="")


def rest_list(values) -> str:
    return "List(" + ",".join(enc(v) for v in values) + ")"


def rest_date(d: date) -> str:
    return f"(year:{d.year},month:{d.month},day:{d.day})"


def account_urn(account_id: int | str) -> str:
    return f"urn:li:sponsoredAccount:{account_id}"


def campaign_urn(campaign_id: int | str) -> str:
    return f"urn:li:sponsoredCampaign:{campaign_id}"


def urn_id(urn: str) -> str:
    return urn.rsplit(":", 1)[-1]


async def get(path: str, query: str = "") -> dict[str, Any]:
    """GET mit fertig kodiertem Query-String (Rest.li-Syntax darf nicht doppelt kodiert werden)."""
    token = await get_access_token()
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    headers = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 200:
        return r.json()
    detail = r.text[:500]
    if r.status_code == 401:
        raise LinkedInError(
            "LinkedIn lehnt die Anmeldung ab (401). Bitte neu anmelden (Werkzeug `login`)."
        )
    if r.status_code == 403:
        raise LinkedInError(
            f"Kein Zugriff (403). Entweder fehlt der App das Recht, oder dein LinkedIn-Konto "
            f"hat keine Rolle auf diesem Werbekonto. Details: {detail}"
        )
    if r.status_code == 426:
        raise LinkedInError(
            f"API-Version {LINKEDIN_VERSION} wird nicht mehr unterstützt (426). "
            f"LINKEDIN_API_VERSION aktualisieren. Details: {detail}"
        )
    if r.status_code == 429:
        raise LinkedInError("LinkedIn drosselt die Anfragen (429). Kurz warten und erneut versuchen.")
    raise LinkedInError(f"LinkedIn-Fehler {r.status_code}: {detail}")


# --- Konten -------------------------------------------------------------------

async def ad_account_roles() -> list[dict]:
    """Alle Werbekonten, auf denen der angemeldete Mensch eine Rolle hat."""
    out, start = [], 0
    while True:
        # Erste Seite ohne Paging-Parameter (so dokumentiert), weitere nur bei Bedarf.
        q = "q=authenticatedUser" + (f"&start={start}&count=100" if start else "")
        data = await get("/adAccountUsers", q)
        els = data.get("elements", [])
        out.extend(els)
        total = data.get("paging", {}).get("total")
        start += len(els)
        if not els or total is None or start >= total:
            return out


async def ad_account(account_id: int | str) -> dict:
    return await get(f"/adAccounts/{account_id}")


# --- Kampagnen ----------------------------------------------------------------

ALL_VISIBLE_STATUSES = ["ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED", "CANCELED", "DRAFT"]


async def search_campaigns(account_id: int | str, statuses: list[str] | None = None) -> list[dict]:
    statuses = statuses or ALL_VISIBLE_STATUSES
    search = f"(status:(values:List({','.join(statuses)})))"
    out, token = [], None
    while True:
        q = f"q=search&search={search}&pageSize=500"
        if token:
            q += f"&pageToken={enc(token)}"
        data = await get(f"/adAccounts/{account_id}/adCampaigns", q)
        out.extend(data.get("elements", []))
        token = data.get("metadata", {}).get("nextPageToken")
        if not token:
            return out


async def campaign_names(account_id: int | str, campaign_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    ids = list(dict.fromkeys(campaign_ids))
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        data = await get(f"/adAccounts/{account_id}/adCampaigns", f"ids=List({','.join(chunk)})")
        for cid, c in data.get("results", {}).items():
            names[str(cid)] = c.get("name", "")
    return names


# --- Kennzahlen ---------------------------------------------------------------

async def analytics(
    *,
    account_ids: list[int | str],
    start: date,
    end: date,
    pivot: str,
    granularity: str,
    fields: list[str],
    campaign_ids: list[int | str] | None = None,
) -> list[dict]:
    q = [
        "q=analytics",
        f"pivot={pivot}",
        f"timeGranularity={granularity}",
        f"dateRange=(start:{rest_date(start)},end:{rest_date(end)})",
        "fields=" + ",".join(fields),
    ]
    if campaign_ids:
        q.append("campaigns=" + rest_list(campaign_urn(c) for c in campaign_ids))
    else:
        q.append("accounts=" + rest_list(account_urn(a) for a in account_ids))
    data = await get("/adAnalytics", "&".join(q))
    return data.get("elements", [])
