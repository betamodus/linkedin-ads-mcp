"""Betamodus LinkedIn-Ads-MCP: Kampagnendaten aus LinkedIn lesen.

Jede Person meldet sich mit ihrem eigenen LinkedIn-Konto an und sieht genau
die Werbekonten, auf denen sie dort eine Rolle hat. Nur lesend.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import api
from .auth import AuthError, delete_token, login_interactive, token_status

mcp = FastMCP(
    "linkedin-ads",
    instructions=(
        "Liest LinkedIn-Werbedaten (Konten, Kampagnen, Kennzahlen) der Werbekonten, "
        "auf denen die angemeldete Person eine Rolle hat. Nur lesen, keine Änderungen. "
        "Typischer Ablauf: list_ad_accounts, dann get_analytics. Kosten sind in der "
        "Kontowährung. Fehlt ein Kunde in list_ad_accounts, hat die Person auf dessen "
        "Werbekonto keine Rolle: das offen sagen statt Zahlen zu raten."
    ),
)

# --- Kennzahlen-Katalog ---------------------------------------------------------

DEFAULT_METRICS = [
    "impressions",
    "clicks",
    "landingPageClicks",
    "costInLocalCurrency",
    "totalEngagements",
    "oneClickLeadFormOpens",
    "oneClickLeads",
    "externalWebsiteConversions",
    "approximateMemberReach",
]

# Laut LinkedIn bei demografischen Aufschlüsselungen (MEMBER_*) nicht verfügbar.
NON_DEMOGRAPHIC_ONLY = {
    "approximateMemberReach",
    "cardClicks",
    "cardImpressions",
    "conversionValueInLocalCurrency",
    "viralCardClicks",
    "viralCardImpressions",
}

METRIC_GROUPS = {
    "Kosten & Reichweite": [
        "costInLocalCurrency", "costInUsd", "impressions", "approximateMemberReach",
        "audiencePenetration", "averageDwellTime",
    ],
    "Klicks & Interaktionen": [
        "clicks", "landingPageClicks", "companyPageClicks", "totalEngagements",
        "otherEngagements", "likes", "reactions", "comments", "shares", "follows",
        "actionClicks", "adUnitClicks", "textUrlClicks", "downloadClicks",
        "cardClicks", "cardImpressions",
    ],
    "Leads & Conversions": [
        "oneClickLeadFormOpens", "oneClickLeads", "qualifiedLeads", "costPerQualifiedLead",
        "validWorkEmailLeads", "externalWebsiteConversions",
        "externalWebsitePostClickConversions", "externalWebsitePostViewConversions",
        "conversionValueInLocalCurrency", "registrations", "jobApplications",
    ],
    "Video": [
        "videoStarts", "videoViews", "videoFirstQuartileCompletions",
        "videoMidpointCompletions", "videoThirdQuartileCompletions", "videoCompletions",
        "averageVideoWatchTime", "fullScreenPlays",
    ],
    "Dokument-Anzeigen": [
        "documentFirstQuartileCompletions", "documentMidpointCompletions",
        "documentThirdQuartileCompletions", "documentCompletions",
    ],
    "Viral (organische Weiterverbreitung bezahlter Inhalte)": [
        "viralImpressions", "viralClicks", "viralReactions", "viralComments",
        "viralShares", "viralFollows", "viralOneClickLeads", "viralExternalWebsiteConversions",
    ],
}

PIVOTS = [
    "ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE",
    "MEMBER_JOB_TITLE", "MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY",
    "MEMBER_COMPANY_SIZE", "MEMBER_COMPANY", "MEMBER_COUNTRY_V2", "MEMBER_REGION_V2",
]

Pivot = Literal[
    "ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE",
    "MEMBER_JOB_TITLE", "MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY",
    "MEMBER_COMPANY_SIZE", "MEMBER_COMPANY", "MEMBER_COUNTRY_V2", "MEMBER_REGION_V2",
]

MAX_METRICS = 18  # LinkedIn erlaubt 20 Felder, 2 gehen an dateRange und pivotValues.


# --- Hilfsfunktionen ------------------------------------------------------------

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ratio(a: float, b: float, factor: float = 1.0, digits: int = 2):
    return round(a / b * factor, digits) if b else None


def _date_str(d: dict | None) -> str | None:
    if not d:
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _ms_to_date(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _money(m: dict | None) -> str | None:
    if not m:
        return None
    return f"{m.get('amount')} {m.get('currencyCode', '')}".strip()


def _error(e: Exception) -> str:
    return json.dumps({"fehler": str(e)}, ensure_ascii=False)


# --- Werkzeuge ------------------------------------------------------------------

@mcp.tool
async def login() -> str:
    """Bei LinkedIn anmelden. Öffnet den Browser; die Person meldet sich an und bestätigt.
    Nur nötig beim ersten Start oder wenn ein anderes Werkzeug meldet, dass die Anmeldung fehlt
    oder abgelaufen ist. Wartet bis zu 3 Minuten auf die Bestätigung."""
    try:
        await login_interactive(timeout_s=170)
        return json.dumps({"ergebnis": "Angemeldet.", **token_status()}, ensure_ascii=False)
    except AuthError as e:
        return _error(e)


@mcp.tool
async def auth_status() -> str:
    """Zeigt, ob eine LinkedIn-Anmeldung besteht und wie lange sie noch gültig ist."""
    return json.dumps(token_status(), ensure_ascii=False)


@mcp.tool
async def list_ad_accounts() -> str:
    """Alle LinkedIn-Werbekonten, auf die die angemeldete Person Zugriff hat, mit Name,
    Kunde (Unternehmen), Währung, Status und eigener Rolle. Grundlage für alle weiteren Abfragen."""
    try:
        roles = await api.ad_account_roles()
        ids = [api.urn_id(r["account"]) for r in roles]
        details = await asyncio.gather(*(api.ad_account(i) for i in ids), return_exceptions=True)
        out = []
        for r, d in zip(roles, details):
            row = {"id": api.urn_id(r["account"]), "rolle": r.get("role")}
            if isinstance(d, Exception):
                row["hinweis"] = f"Details nicht lesbar: {d}"
            else:
                row.update(
                    name=d.get("name"),
                    waehrung=d.get("currency"),
                    status=d.get("status"),
                    typ=d.get("type"),
                    unternehmen=d.get("reference"),
                )
            out.append(row)
        out.sort(key=lambda x: (x.get("name") or "").lower())
        return json.dumps({"anzahl": len(out), "konten": out}, ensure_ascii=False)
    except Exception as e:
        return _error(e)


@mcp.tool
async def list_campaigns(
    account_id: Annotated[str, Field(description="Werbekonto-ID (Zahl aus list_ad_accounts)")],
    statuses: Annotated[
        list[Literal["ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED", "CANCELED", "DRAFT"]] | None,
        Field(description="Nur diese Status. Leer = alle außer gelöschten."),
    ] = None,
) -> str:
    """Kampagnen eines Werbekontos mit Status, Ziel, Laufzeit, Budget und Gebotsart."""
    try:
        camps = await api.search_campaigns(account_id, statuses)
        out = [
            {
                "id": str(c.get("id")),
                "name": c.get("name"),
                "status": c.get("status"),
                "kampagnengruppe": api.urn_id(c["campaignGroup"]) if c.get("campaignGroup") else None,
                "ziel": c.get("objectiveType"),
                "format": c.get("type"),
                "start": _ms_to_date((c.get("runSchedule") or {}).get("start")),
                "ende": _ms_to_date((c.get("runSchedule") or {}).get("end")),
                "tagesbudget": _money(c.get("dailyBudget")),
                "gesamtbudget": _money(c.get("totalBudget")),
                "gebotsart": c.get("costType"),
                "gebot": _money(c.get("unitCost")),
            }
            for c in camps
        ]
        return json.dumps({"anzahl": len(out), "kampagnen": out}, ensure_ascii=False)
    except Exception as e:
        return _error(e)


@mcp.tool
async def get_analytics(
    account_ids: Annotated[list[str], Field(description="Eine oder mehrere Werbekonto-IDs")],
    start_date: Annotated[str, Field(description="Erster Tag, YYYY-MM-DD")],
    end_date: Annotated[str, Field(description="Letzter Tag (einschließlich), YYYY-MM-DD")],
    pivot: Annotated[
        Pivot,
        Field(
            description="Aufschlüsselung: ACCOUNT, CAMPAIGN_GROUP, CAMPAIGN, CREATIVE oder "
            "demografisch (MEMBER_*). Demografische Werte sind von LinkedIn gerundet, "
            "kleine Gruppen fehlen, nur die größten Ausprägungen werden geliefert."
        ),
    ] = "CAMPAIGN",
    time_granularity: Annotated[
        Literal["ALL", "DAILY", "MONTHLY", "YEARLY"],
        Field(description="ALL = Summe über den Zeitraum, sonst je Tag/Monat/Jahr. "
              "Tageswerte gibt LinkedIn nur rund 6 Monate rückwirkend."),
    ] = "ALL",
    metrics: Annotated[
        list[str] | None,
        Field(description=f"LinkedIn-Feldnamen, max. {MAX_METRICS}. Leer = Standardsatz "
              f"({', '.join(DEFAULT_METRICS)}). Alle Namen: list_metrics."),
    ] = None,
    campaign_ids: Annotated[
        list[str] | None,
        Field(description="Optional nur diese Kampagnen (IDs aus list_campaigns)"),
    ] = None,
) -> str:
    """Kennzahlen aus LinkedIn für Zeitraum und Aufschlüsselung. Liefert Rohwerte und daraus
    berechnet CTR (%), CPC, CPM, Kosten pro Lead (CPL) und Kosten pro Conversion.
    Kosten in der Währung des jeweiligen Werbekontos."""
    hinweise: list[str] = []
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            return _error(ValueError("end_date liegt vor start_date."))

        fields = list(dict.fromkeys(metrics or DEFAULT_METRICS))
        if pivot.startswith("MEMBER_"):
            dropped = [f for f in fields if f in NON_DEMOGRAPHIC_ONLY]
            if dropped:
                fields = [f for f in fields if f not in NON_DEMOGRAPHIC_ONLY]
                hinweise.append(
                    f"Bei demografischer Aufschlüsselung nicht verfügbar und weggelassen: {', '.join(dropped)}."
                )
            hinweise.append(
                "Demografische Werte sind von LinkedIn geschätzt, Gruppen unter 3 Ereignissen "
                "fehlen, nur die größten Ausprägungen werden geliefert. Summen können daher unter "
                "den Gesamtwerten liegen. Kennungen (urn:li:…) werden in dieser Fassung noch nicht "
                "in Klartext übersetzt."
            )
        if len(fields) > MAX_METRICS:
            return _error(ValueError(f"Höchstens {MAX_METRICS} Kennzahlen pro Abfrage."))

        rows = await api.analytics(
            account_ids=account_ids,
            start=start,
            end=end,
            pivot=pivot,
            granularity=time_granularity,
            fields=fields + ["dateRange", "pivotValues"],
            campaign_ids=campaign_ids,
        )
        if len(rows) >= api.ANALYTICS_MAX_ELEMENTS:
            hinweise.append(
                f"LinkedIn hat die Höchstzahl von {api.ANALYTICS_MAX_ELEMENTS} Zeilen geliefert. "
                "Ergebnis ist vermutlich unvollständig: Zeitraum oder Konten verkleinern."
            )

        # Namen und Währungen nachschlagen
        names: dict[str, str] = {}
        currency_by_account: dict[str, str] = {}
        account_of_campaign: dict[str, str] = {}
        accounts = await asyncio.gather(*(api.ad_account(a) for a in account_ids), return_exceptions=True)
        for a_id, a in zip(account_ids, accounts):
            if not isinstance(a, Exception):
                currency_by_account[str(a_id)] = a.get("currency")
                if pivot == "ACCOUNT":
                    names[str(a_id)] = a.get("name")
        if pivot == "CAMPAIGN":
            camp_lists = await asyncio.gather(
                *(api.search_campaigns(a) for a in account_ids), return_exceptions=True
            )
            for a_id, camps in zip(account_ids, camp_lists):
                if isinstance(camps, Exception):
                    hinweise.append(f"Kampagnennamen für Konto {a_id} nicht lesbar: {camps}")
                    continue
                for c in camps:
                    names[str(c["id"])] = c.get("name")
                    account_of_campaign[str(c["id"])] = str(a_id)
        currencies = set(filter(None, currency_by_account.values()))
        if len(currencies) > 1:
            hinweise.append(
                f"Konten mit unterschiedlichen Währungen ({', '.join(sorted(currencies))}): "
                "Kosten nicht über Konten hinweg addieren."
            )

        out = []
        for r in rows:
            pv = (r.get("pivotValues") or [None])[0]
            key = api.urn_id(pv) if pv else None
            row = {
                "von": _date_str((r.get("dateRange") or {}).get("start")),
                "bis": _date_str((r.get("dateRange") or {}).get("end")),
                "wert": pv,
            }
            if key in names:
                row["name"] = names[key]
            acc = key if pivot == "ACCOUNT" else account_of_campaign.get(key or "")
            if acc and acc in currency_by_account:
                row["waehrung"] = currency_by_account[acc]
            elif len(currencies) == 1:
                row["waehrung"] = next(iter(currencies))
            for f in fields:
                if f in r:
                    row[f] = r[f]
            cost = _num(r.get("costInLocalCurrency"))
            imps, clicks = _num(r.get("impressions")), _num(r.get("clicks"))
            leads, conv = _num(r.get("oneClickLeads")), _num(r.get("externalWebsiteConversions"))
            if "impressions" in r and "clicks" in r:
                row["ctr_prozent"] = _ratio(clicks, imps, 100)
            if "costInLocalCurrency" in r:
                row["cpc"] = _ratio(cost, clicks)
                row["cpm"] = _ratio(cost, imps, 1000)
                if "oneClickLeads" in r:
                    row["kosten_pro_lead"] = _ratio(cost, leads)
                if "externalWebsiteConversions" in r:
                    row["kosten_pro_conversion"] = _ratio(cost, conv)
            if "approximateMemberReach" in fields and "approximateMemberReach" in r and "impressions" in r:
                row["frequenz"] = _ratio(imps, _num(r.get("approximateMemberReach")))
            out.append(row)

        out.sort(key=lambda x: (x.get("von") or "", -_num(x.get("costInLocalCurrency"))))
        if not out:
            hinweise.append(
                "Keine Daten. Entweder lief im Zeitraum nichts, oder die angemeldete Person hat "
                "auf den Konten keine Leserechte (LinkedIn meldet das nicht als Fehler)."
            )
        return json.dumps(
            {
                "zeitraum": {"von": start_date, "bis": end_date},
                "aufschluesselung": pivot,
                "zeitraster": time_granularity,
                "anzahl_zeilen": len(out),
                "zeilen": out,
                "hinweise": hinweise,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return _error(e)


@mcp.tool
async def list_metrics() -> str:
    """Verfügbare Kennzahlen (LinkedIn-Feldnamen) nach Gruppen, mögliche Aufschlüsselungen
    und Einschränkungen."""
    return json.dumps(
        {
            "kennzahlen": METRIC_GROUPS,
            "standardsatz": DEFAULT_METRICS,
            "aufschluesselungen": PIVOTS,
            "nicht_bei_demografie": sorted(NON_DEMOGRAPHIC_ONLY),
            "einschraenkungen": [
                f"Höchstens {MAX_METRICS} Kennzahlen pro Abfrage.",
                "Tageswerte nur rund 6 Monate rückwirkend, Monatswerte bis 10 Jahre, Demografie 2 Jahre.",
                "Demografie: geschätzt, mind. 3 Ereignisse pro Wert, nur größte Ausprägungen, 12 bis 24 Stunden Verzug.",
                "Berechnet: ctr_prozent, cpc, cpm, kosten_pro_lead, kosten_pro_conversion, frequenz.",
            ],
            "api_version": api.LINKEDIN_VERSION,
        },
        ensure_ascii=False,
    )


# --- Einstieg -------------------------------------------------------------------

def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "login":
        try:
            asyncio.run(login_interactive())
            print(json.dumps(token_status(), ensure_ascii=False, indent=2))
        except AuthError as e:
            print(f"Fehler: {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "logout":
        delete_token()
        print("Abgemeldet.")
    elif cmd == "status":
        print(json.dumps(token_status(), ensure_ascii=False, indent=2))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
