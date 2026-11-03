"""Dünner Client für die LinkedIn Marketing API (versionierte REST-Endpunkte).

Begriffe: Die API nennt die Ebenen anders als der Campaign Manager.
  API campaignGroup  = Campaign Manager „Kampagne“
  API campaign       = Campaign Manager „Anzeigengruppe“
  API creative       = Campaign Manager „Anzeige“
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from .auth import get_access_token

BASE = "https://api.linkedin.com/rest"
BASE_V2 = "https://api.linkedin.com/v2"

# LinkedIn unterstützt jede Version mindestens ein Jahr. Jährlich nachziehen.
LINKEDIN_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202610")

# LinkedIn liefert pro adAnalytics-Antwort höchstens so viele Zeilen, ohne Seiten.
ANALYTICS_MAX_ELEMENTS = 15000


class LinkedInError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


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


def creative_urn(creative_id: int | str) -> str:
    return f"urn:li:sponsoredCreative:{creative_id}"


def urn_id(urn: str) -> str:
    return str(urn).rsplit(":", 1)[-1]


async def get(
    path: str,
    query: str = "",
    *,
    base: str = BASE,
    versioned: bool = True,
    method: str | None = None,
) -> dict[str, Any]:
    """GET mit fertig kodiertem Query-String (Rest.li-Syntax darf nicht doppelt kodiert werden)."""
    token = await get_access_token()
    url = f"{base}{path}" + (f"?{query}" if query else "")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    if versioned:
        headers["LinkedIn-Version"] = LINKEDIN_VERSION
    if method:
        headers["X-RestLi-Method"] = method
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 200:
        return r.json()
    detail = r.text[:500]
    s = r.status_code
    if s == 401:
        raise LinkedInError(
            "LinkedIn lehnt die Anmeldung ab (401). Bitte neu anmelden (Werkzeug `login`).", s
        )
    if s == 403:
        raise LinkedInError(
            f"Kein Zugriff (403). Entweder fehlt der App das Recht, oder dein LinkedIn-Konto "
            f"hat keine Rolle auf diesem Werbekonto. Details: {detail}", s
        )
    if s == 426:
        raise LinkedInError(
            f"API-Version {LINKEDIN_VERSION} wird nicht mehr unterstützt (426). "
            f"LINKEDIN_API_VERSION aktualisieren. Details: {detail}", s
        )
    if s == 429:
        raise LinkedInError("LinkedIn drosselt die Anfragen (429). Kurz warten und erneut versuchen.", s)
    raise LinkedInError(f"LinkedIn-Fehler {s}: {detail}", s)


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


# --- Kampagnen (API: campaign groups) und Anzeigengruppen (API: campaigns) ----

ALL_VISIBLE_STATUSES = ["ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED", "CANCELED", "DRAFT"]
GROUP_STATUSES = ["ACTIVE", "PAUSED", "ARCHIVED", "CANCELED", "DRAFT"]


async def _search_all(path: str, search: str, page_size: int = 500) -> list[dict]:
    out, token = [], None
    while True:
        q = f"q=search&search={search}&pageSize={page_size}"
        if token:
            q += f"&pageToken={enc(token)}"
        data = await get(path, q)
        out.extend(data.get("elements", []))
        token = data.get("metadata", {}).get("nextPageToken")
        if not token:
            return out


async def search_campaigns(account_id: int | str, statuses: list[str] | None = None) -> list[dict]:
    """Anzeigengruppen (API: campaigns) eines Kontos."""
    statuses = statuses or ALL_VISIBLE_STATUSES
    return await _search_all(
        f"/adAccounts/{account_id}/adCampaigns",
        f"(status:(values:List({','.join(statuses)})))",
    )


async def search_campaign_groups(account_id: int | str) -> list[dict]:
    """Kampagnen (API: campaign groups) eines Kontos."""
    return await _search_all(
        f"/adAccounts/{account_id}/adCampaignGroups",
        f"(status:(values:List({','.join(GROUP_STATUSES)})))",
    )


# --- Anzeigen (API: creatives) ------------------------------------------------

async def _creatives_finder(account_id: int | str, extra: str) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        q = f"q=criteria{extra}&pageSize=100"
        if token:
            q += f"&pageToken={enc(token)}"
        data = await get(f"/adAccounts/{account_id}/creatives", q, method="FINDER")
        out.extend(data.get("elements", []))
        token = data.get("metadata", {}).get("nextPageToken")
        if not token:
            return out


async def creatives_by_ids(account_id: int | str, creative_urns: list[str]) -> list[dict]:
    """Anzeigen eines Kontos nach URN.

    Gezielte Abfrage nach URN. Enthält die Liste Anzeigen anderer Konten, lehnt
    LinkedIn das teils mit einem Serverfehler ab; dann werden alle Anzeigen des
    Kontos geholt und gefiltert.
    """
    urns = list(dict.fromkeys(creative_urns))
    wanted = set(urns)
    try:
        out: list[dict] = []
        for i in range(0, len(urns), 50):  # URL-Länge begrenzen
            out += await _creatives_finder(account_id, f"&creatives={rest_list(urns[i : i + 50])}")
        return out
    except LinkedInError as e:
        if e.status in (401, 429):
            raise
    everything = await _creatives_finder(account_id, "")
    return [c for c in everything if str(c.get("id")) in wanted]


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
