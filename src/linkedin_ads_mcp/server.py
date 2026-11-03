"""Betamodus LinkedIn-Ads-MCP: Werbedaten aus LinkedIn lesen.

Jede Person meldet sich mit ihrem eigenen LinkedIn-Konto an und sieht genau
die Werbekonten, auf denen sie dort eine Rolle hat. Nur lesend.

Begriffe wie im Campaign Manager:
  Kampagne        = API campaignGroup   (Aufschlüsselung CAMPAIGN_GROUP)
  Anzeigengruppe  = API campaign        (Aufschlüsselung CAMPAIGN)
  Anzeige         = API creative        (Aufschlüsselung CREATIVE)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import api, names
from .auth import AuthError, delete_token, login_interactive, token_status

mcp = FastMCP(
    "linkedin-ads",
    instructions=(
        "Liest LinkedIn-Werbedaten der Werbekonten, auf denen die angemeldete Person eine "
        "Rolle hat. Nur lesen, keine Änderungen. Begriffe wie im Campaign Manager: "
        "Kampagne (API: campaign group) > Anzeigengruppe (API: campaign) > Anzeige "
        "(API: creative). Diese Begriffe auch in Antworten verwenden. "
        "Typischer Ablauf: list_ad_accounts, dann get_analytics oder list_campaign_structure. "
        "Kosten sind in der Kontowährung. Fehlt ein Kunde in list_ad_accounts, hat die "
        "Person auf dessen Werbekonto keine Rolle: das offen sagen statt Zahlen zu raten. "
        "Hinweise im Feld 'hinweise' immer an die Person weitergeben."
    ),
)

# Obergrenze für die Größe einer Antwort (Zeichen). Darüber wird gekürzt statt
# den Chat zu überlasten. Per Umgebungsvariable einstellbar.
MAX_CHARS = int(os.environ.get("LINKEDIN_MCP_MAX_CHARS", "35000"))

PIVOT_LABEL = {
    "ACCOUNT": "Werbekonto",
    "CAMPAIGN_GROUP": "Kampagne",
    "CAMPAIGN": "Anzeigengruppe",
    "CREATIVE": "Anzeige",
    "MEMBER_JOB_TITLE": "Jobtitel",
    "MEMBER_JOB_FUNCTION": "Jobfunktion",
    "MEMBER_SENIORITY": "Karrierestufe",
    "MEMBER_INDUSTRY": "Branche",
    "MEMBER_COMPANY_SIZE": "Unternehmensgröße",
    "MEMBER_COMPANY": "Unternehmen",
    "MEMBER_COUNTRY_V2": "Land",
    "MEMBER_REGION_V2": "Region",
}

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

# Geldbeträge: Anzeige auf 2 Nachkommastellen, gerechnet wird mit genauen Werten.
MONEY_FIELDS = {
    "costInLocalCurrency",
    "costInUsd",
    "conversionValueInLocalCurrency",
    "costPerQualifiedLead",
}

# Nicht addierbar (Reichweite, Durchschnitte, Quoten): keine Summen bilden.
NON_ADDITIVE = {
    "approximateMemberReach",
    "audiencePenetration",
    "averageDwellTime",
    "averageVideoWatchTime",
    "costPerQualifiedLead",
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


def _clean(field: str, v):
    """Zahlen als Zahlen, Geld auf 2 Stellen. Nur für die Anzeige."""
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return v
    if isinstance(v, float):
        if field in MONEY_FIELDS:
            return round(v, 2)
        if v.is_integer():
            return int(v)
        return round(v, 2)
    return v


def _ratio(a: float, b: float, factor: float = 1.0, digits: int = 2):
    return round(a / b * factor, digits) if b else None


def _derived(m: dict, fields: list[str]) -> dict:
    """Berechnete Kennzahlen aus genauen Rohwerten."""
    out = {}
    cost = _num(m.get("costInLocalCurrency"))
    imps, clicks = _num(m.get("impressions")), _num(m.get("clicks"))
    if "impressions" in m and "clicks" in m:
        out["ctr_prozent"] = _ratio(clicks, imps, 100)
    if "costInLocalCurrency" in m:
        if "clicks" in m:
            out["cpc"] = _ratio(cost, clicks)
        if "impressions" in m:
            out["cpm"] = _ratio(cost, imps, 1000)
        if "oneClickLeads" in m:
            out["kosten_pro_lead"] = _ratio(cost, _num(m.get("oneClickLeads")))
        if "externalWebsiteConversions" in m:
            out["kosten_pro_conversion"] = _ratio(cost, _num(m.get("externalWebsiteConversions")))
    if "oneClickLeadFormOpens" in m and "oneClickLeads" in m:
        out["lead_formular_abschlussquote_prozent"] = _ratio(
            _num(m.get("oneClickLeads")), _num(m.get("oneClickLeadFormOpens")), 100
        )
    if "approximateMemberReach" in fields and "approximateMemberReach" in m and "impressions" in m:
        out["frequenz"] = _ratio(imps, _num(m.get("approximateMemberReach")))
    return out


def _date_str(d: dict | None) -> str | None:
    if not d:
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _ms_to_date(ms) -> date | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _money(m: dict | None) -> float | None:
    if not m or m.get("amount") is None:
        return None
    return round(_num(m.get("amount")), 2)


def _error(e: Exception) -> str:
    return json.dumps({"fehler": str(e)}, ensure_ascii=False)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


async def _structure(account_ids: list[str]) -> tuple[dict, dict, dict]:
    """Anzeigengruppen und Kampagnen aller Konten: (gruppen_by_id, campaigns_by_id, account_of_campaign)."""
    groups: dict[str, dict] = {}
    camps: dict[str, dict] = {}
    acc_of: dict[str, str] = {}
    res = await asyncio.gather(
        *(api.search_campaigns(a) for a in account_ids),
        *(api.search_campaign_groups(a) for a in account_ids),
        return_exceptions=True,
    )
    n = len(account_ids)
    for a, cl in zip(account_ids, res[:n]):
        if isinstance(cl, Exception):
            continue
        for c in cl:
            camps[str(c["id"])] = c
            acc_of[str(c["id"])] = str(a)
    for gl in res[n:]:
        if isinstance(gl, Exception):
            continue
        for g in gl:
            groups[str(g["id"])] = g
    return groups, camps, acc_of


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
    Währung, Status und eigener Rolle. Grundlage für alle weiteren Abfragen."""
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
                )
            out.append(row)
        out.sort(key=lambda x: (x.get("name") or "").lower())
        return _dump({"anzahl": len(out), "konten": out})
    except Exception as e:
        return _error(e)


@mcp.tool
async def list_campaign_structure(
    account_id: Annotated[str, Field(description="Werbekonto-ID (Zahl aus list_ad_accounts)")],
    statuses: Annotated[
        list[Literal["ACTIVE", "PAUSED", "ARCHIVED", "COMPLETED", "CANCELED", "DRAFT"]] | None,
        Field(description="Nur Anzeigengruppen mit diesem Status. Leer = alle außer gelöschten."),
    ] = None,
    include_spend: Annotated[
        bool,
        Field(description="Ausgaben seit Start und Budgetauslastung mitliefern (eine zusätzliche Abfrage)."),
    ] = True,
) -> str:
    """Kampagnen eines Werbekontos mit ihren Anzeigengruppen (Begriffe wie im Campaign Manager):
    Status, Ziel, Laufzeit, Budgets, Gebot und auf Wunsch Ausgaben seit Start und
    Budgetauslastung. Bei Tagesbudgets ist die Auslastung ein Durchschnitt über die Laufzeit."""
    hinweise: list[str] = []
    try:
        camps_l, groups_l = await asyncio.gather(
            api.search_campaigns(account_id, statuses),
            api.search_campaign_groups(account_id),
        )
        groups = {str(g["id"]): g for g in groups_l}
        today = datetime.now(timezone.utc).date()

        spend_c: dict[str, float] = {}
        spend_g: dict[str, float] = {}
        if include_spend and camps_l:
            starts = [
                _ms_to_date((c.get("runSchedule") or {}).get("start")) for c in camps_l
            ] + [_ms_to_date((g.get("runSchedule") or {}).get("start")) for g in groups_l]
            first = min([s for s in starts if s] or [today])
            rows_c, rows_g = await asyncio.gather(
                api.analytics(account_ids=[account_id], start=first, end=today, pivot="CAMPAIGN",
                              granularity="ALL", fields=["costInLocalCurrency", "pivotValues"]),
                api.analytics(account_ids=[account_id], start=first, end=today, pivot="CAMPAIGN_GROUP",
                              granularity="ALL", fields=["costInLocalCurrency", "pivotValues"]),
            )
            for r in rows_c:
                spend_c[api.urn_id(r["pivotValues"][0])] = _num(r.get("costInLocalCurrency"))
            for r in rows_g:
                spend_g[api.urn_id(r["pivotValues"][0])] = _num(r.get("costInLocalCurrency"))

        def run_info(obj: dict) -> tuple[date | None, date | None, int | None]:
            rs = obj.get("runSchedule") or {}
            s, e = _ms_to_date(rs.get("start")), _ms_to_date(rs.get("end"))
            if not s:
                return s, e, None
            last = min(e, today) if e else today
            return s, e, max((last - s).days + 1, 1) if last >= s else None

        by_group: dict[str, list[dict]] = {}
        for c in camps_l:
            cid = str(c["id"])
            s, e, days = run_info(c)
            row = {
                "id": cid,
                "anzeigengruppe": c.get("name"),
                "status": c.get("status"),
                "ziel": c.get("objectiveType"),
                "format": c.get("type"),
                "start": s.isoformat() if s else None,
                "ende": e.isoformat() if e else None,
                "tagesbudget": _money(c.get("dailyBudget")),
                "gesamtbudget": _money(c.get("totalBudget")),
                "waehrung": (c.get("dailyBudget") or c.get("totalBudget") or {}).get("currencyCode"),
                "gebotsart": c.get("costType"),
                "gebot": _money(c.get("unitCost")),
            }
            if include_spend:
                spent = spend_c.get(cid, 0.0)
                row["ausgegeben_seit_start"] = round(spent, 2)
                if row["gesamtbudget"]:
                    row["gesamtbudget_auslastung_prozent"] = _ratio(spent, row["gesamtbudget"], 100, 1)
                if row["tagesbudget"] and days:
                    row["durchschnitt_tagesausgaben"] = round(spent / days, 2)
                    row["tagesbudget_auslastung_prozent"] = _ratio(spent / days, row["tagesbudget"], 100, 1)
            gid = api.urn_id(c["campaignGroup"]) if c.get("campaignGroup") else "ohne"
            by_group.setdefault(gid, []).append(_drop_none(row))

        out = []
        for gid, members in by_group.items():
            g = groups.get(gid, {})
            s, e, _ = run_info(g) if g else (None, None, None)
            row = {
                "id": gid,
                "kampagne": g.get("name") if g else None,
                "status": g.get("status") if g else None,
                "start": s.isoformat() if s else None,
                "ende": e.isoformat() if e else None,
                "gesamtbudget": _money(g.get("totalBudget")) if g else None,
            }
            if include_spend and g:
                spent = spend_g.get(gid, 0.0)
                row["ausgegeben_seit_start"] = round(spent, 2)
                if row["gesamtbudget"]:
                    row["gesamtbudget_auslastung_prozent"] = _ratio(spent, row["gesamtbudget"], 100, 1)
            row["anzeigengruppen"] = members
            out.append(_drop_none(row))
        out.sort(key=lambda x: str(x.get("start") or ""), reverse=True)
        if include_spend:
            hinweise.append(f"Ausgaben bis heute ({today.isoformat()}, UTC).")
        return _dump({
            "anzahl_kampagnen": len(out),
            "anzahl_anzeigengruppen": len(camps_l),
            "kampagnen": out,
            "hinweise": hinweise,
        })
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
            description="Aufschlüsselung. CAMPAIGN_GROUP = Kampagne, CAMPAIGN = Anzeigengruppe, "
            "CREATIVE = Anzeige (Begriffe wie im Campaign Manager), ACCOUNT = Werbekonto, "
            "MEMBER_* = Demografie der erreichten Mitglieder. Demografische Werte sind von "
            "LinkedIn geschätzt, kleine Gruppen fehlen, nur die größten Ausprägungen werden geliefert."
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
        Field(description="Optional nur diese Anzeigengruppen (IDs aus list_campaign_structure)"),
    ] = None,
    max_rows: Annotated[
        int | None,
        Field(description="Optional nur die N Zeilen mit den höchsten Kosten (z. B. Top 10). "
              "Summen enthalten trotzdem alle Zeilen."),
    ] = None,
) -> str:
    """Kennzahlen aus LinkedIn für Zeitraum und Aufschlüsselung, mit Namen in Klartext.
    Liefert Rohwerte, daraus berechnet CTR (%), CPC, CPM, Kosten pro Lead, Kosten pro Conversion,
    Lead-Formular-Abschlussquote und Frequenz, sowie Summen über alle Zeilen.
    Kosten in der Währung des jeweiligen Werbekontos. Sehr große Ergebnisse werden gekürzt
    (Hinweis im Feld 'hinweise'); dann gröber oder enger abfragen."""
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
                "den Gesamtwerten liegen."
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

        # Währungen
        accounts = await asyncio.gather(*(api.ad_account(a) for a in account_ids), return_exceptions=True)
        currency_by_account: dict[str, str] = {}
        account_names: dict[str, str] = {}
        for a_id, a in zip(account_ids, accounts):
            if not isinstance(a, Exception):
                currency_by_account[str(a_id)] = a.get("currency")
                account_names[str(a_id)] = a.get("name")
        currencies = set(filter(None, currency_by_account.values()))
        multi_currency = len(currencies) > 1
        if multi_currency:
            hinweise.append(
                f"Konten mit unterschiedlichen Währungen ({', '.join(sorted(currencies))}): "
                "Kosten werden nicht über Konten hinweg summiert."
            )

        # Namen
        values = list(dict.fromkeys((r.get("pivotValues") or [None])[0] for r in rows))
        labels: dict[str, dict] = {}
        if pivot == "ACCOUNT":
            for v in values:
                labels[v] = {"werbekonto": account_names.get(api.urn_id(v))}
        elif pivot in ("CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE"):
            groups, camps, acc_of = await _structure(account_ids)

            def camp_label(cid: str) -> dict:
                c = camps.get(cid, {})
                gid = api.urn_id(c["campaignGroup"]) if c.get("campaignGroup") else None
                return {
                    "anzeigengruppe": c.get("name"),
                    "kampagne": groups.get(gid, {}).get("name") if gid else None,
                }

            if pivot == "CAMPAIGN_GROUP":
                for v in values:
                    labels[v] = {"kampagne": groups.get(api.urn_id(v), {}).get("name")}
            elif pivot == "CAMPAIGN":
                for v in values:
                    labels[v] = camp_label(api.urn_id(v))
            else:
                creatives = await asyncio.gather(
                    *(api.creatives_by_ids(a, [v for v in values if v]) for a in account_ids),
                    return_exceptions=True,
                )
                found = {}
                for cl in creatives:
                    if isinstance(cl, Exception):
                        hinweise.append(f"Anzeigennamen teilweise nicht lesbar: {cl}")
                        continue
                    for cr in cl:
                        found[str(cr.get("id"))] = cr
                unnamed = 0
                for v in values:
                    cr = found.get(v, {})
                    cid = api.urn_id(cr["campaign"]) if cr.get("campaign") else None
                    lab = camp_label(cid) if cid else {}
                    name = cr.get("name")
                    if not name:
                        unnamed += 1
                        name = f"Anzeige {api.urn_id(v)}" + (
                            f" in „{lab.get('anzeigengruppe')}“" if lab.get("anzeigengruppe") else ""
                        )
                    labels[v] = {"anzeige": name, **lab, "status": cr.get("intendedStatus")}
                if unnamed:
                    hinweise.append(
                        f"{unnamed} Anzeige(n) ohne gepflegten Namen: bezeichnet mit ID und "
                        "Anzeigengruppe. Den Post-Text liefert LinkedIn nur mit Rechten auf der "
                        "Unternehmensseite."
                    )
        else:
            resolved, missing = await names.resolve([v for v in values if v])
            for v in values:
                labels[v] = {"name": resolved.get(v)}
            unresolved = sum(1 for v in values if v and v.startswith("urn:li:") and not resolved.get(v))
            if unresolved:
                hinweise.append(
                    f"{unresolved} von {len(values)} Werten ohne Klartext von LinkedIn, dort steht nur "
                    "die Kennung (urn:li:…)."
                )

        # Zeilen aufbauen (Anzeige gerundet, Rechnen mit Rohwerten)
        def build(r: dict) -> dict:
            pv = (r.get("pivotValues") or [None])[0]
            row = {
                "von": _date_str((r.get("dateRange") or {}).get("start")),
                "bis": _date_str((r.get("dateRange") or {}).get("end")),
                **(labels.get(pv) or {}),
                "wert": pv,
            }
            for f in fields:
                if f in r:
                    row[f] = _clean(f, r[f])
            row.update(_derived(r, fields))
            return _drop_none(row)

        rows.sort(key=lambda r: (_date_str((r.get("dateRange") or {}).get("start")) or "",
                                 -_num(r.get("costInLocalCurrency"))))
        out_rows = [build(r) for r in rows]

        # Summen über alle Zeilen
        totals: dict = {}
        for f in fields:
            if f in NON_ADDITIVE or (multi_currency and f in MONEY_FIELDS):
                continue
            if any(f in r for r in rows):
                totals[f] = sum(_num(r.get(f)) for r in rows)
        summen = {f: _clean(f, v) for f, v in totals.items()}
        summen.update(_derived(totals, [f for f in fields if f not in NON_ADDITIVE]))
        if "approximateMemberReach" in fields:
            hinweise.append("Reichweite lässt sich nicht über Zeilen summieren und fehlt deshalb in den Summen.")

        # Je Wert über die Zeit (nur bei Zeitreihen)
        je_wert = None
        if time_granularity != "ALL" and len(values) > 1:
            agg: dict[str, dict] = {}
            for r in rows:
                pv = (r.get("pivotValues") or [None])[0]
                a = agg.setdefault(pv, {})
                for f in fields:
                    if f in r and f not in NON_ADDITIVE:
                        a[f] = a.get(f, 0.0) + _num(r.get(f))
            je_wert = []
            for pv, a in agg.items():
                row = {**(labels.get(pv) or {}), "wert": pv}
                row.update({f: _clean(f, v) for f, v in a.items()})
                row.update(_derived(a, [f for f in fields if f not in NON_ADDITIVE]))
                je_wert.append(_drop_none(row))
            je_wert.sort(key=lambda x: -_num(x.get("costInLocalCurrency")))

        # Top-N und Größenbremse
        total_rows = len(out_rows)
        if max_rows and total_rows > max_rows:
            out_rows = sorted(out_rows, key=lambda x: -_num(x.get("costInLocalCurrency")))[:max_rows]
            if je_wert:
                je_wert = je_wert[:max_rows]
            hinweise.append(f"Auf die {max_rows} Zeilen mit den höchsten Kosten begrenzt (von {total_rows}).")

        result = {
            "zeitraum": {"von": start_date, "bis": end_date},
            "aufschluesselung": f"{PIVOT_LABEL.get(pivot, pivot)} ({pivot})",
            "zeitraster": time_granularity,
            "anzahl_zeilen_gesamt": total_rows,
            "summen": summen,
            "summen_je_wert": je_wert,
            "zeilen": out_rows,
            "hinweise": hinweise,
        }
        if len(_dump(result)) > MAX_CHARS:
            budget = MAX_CHARS - len(_dump({**result, "zeilen": [], "summen_je_wert": None})) - 600
            if je_wert:
                kept_w, used = [], 0
                for w in je_wert:
                    s = len(_dump(w)) + 1
                    if used + s > budget // 2:
                        break
                    kept_w.append(w)
                    used += s
                result["summen_je_wert"] = kept_w
                budget -= used
            by_cost = sorted(out_rows, key=lambda x: -_num(x.get("costInLocalCurrency")))
            kept, used = [], 0
            for row in by_cost:
                s = len(_dump(row)) + 1
                if used + s > budget:
                    break
                kept.append(row)
                used += s
            kept.sort(key=lambda x: (x.get("von") or "", -_num(x.get("costInLocalCurrency"))))
            result["zeilen"] = kept
            msg = (
                f"Antwort gekürzt: {len(kept)} von {total_rows} Zeilen gezeigt (die mit den höchsten "
                "Kosten). Die Summen enthalten alle Zeilen."
            )
            if je_wert:
                msg += f" summen_je_wert zeigt {len(result['summen_je_wert'])} von {len(je_wert)} Einträgen."
            msg += " Für Details enger abfragen: kürzerer Zeitraum, weniger Konten, gröberes Zeitraster oder max_rows."
            hinweise.append(msg)
        if result["summen_je_wert"] is None:
            del result["summen_je_wert"]

        if not out_rows:
            hinweise.append(
                "Keine Daten. Entweder lief im Zeitraum nichts, oder die angemeldete Person hat "
                "auf den Konten keine Leserechte (LinkedIn meldet das nicht als Fehler)."
            )
        return _dump(result)
    except Exception as e:
        return _error(e)


@mcp.tool
async def list_metrics() -> str:
    """Verfügbare Kennzahlen (LinkedIn-Feldnamen) nach Gruppen, mögliche Aufschlüsselungen
    und Einschränkungen."""
    return _dump(
        {
            "kennzahlen": METRIC_GROUPS,
            "hinweis_katalog": "Der Katalog ist eine Nachschlagehilfe. Jede gültige LinkedIn-Kennzahl kann abgefragt werden.",
            "standardsatz": DEFAULT_METRICS,
            "aufschluesselungen": {k: v for k, v in PIVOT_LABEL.items()},
            "nicht_bei_demografie": sorted(NON_DEMOGRAPHIC_ONLY),
            "einschraenkungen": [
                f"Höchstens {MAX_METRICS} Kennzahlen pro Abfrage.",
                "Tageswerte nur rund 6 Monate rückwirkend, Monatswerte bis 10 Jahre, Demografie 2 Jahre.",
                "Demografie: geschätzt, mind. 3 Ereignisse pro Wert, nur größte Ausprägungen, 12 bis 24 Stunden Verzug.",
                "Berechnet: ctr_prozent, cpc, cpm, kosten_pro_lead, kosten_pro_conversion, "
                "lead_formular_abschlussquote_prozent, frequenz.",
                f"Antworten über {MAX_CHARS} Zeichen werden gekürzt (einstellbar per LINKEDIN_MCP_MAX_CHARS).",
            ],
            "api_version": api.LINKEDIN_VERSION,
        }
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
