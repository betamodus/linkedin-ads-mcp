"""Betamodus LinkedIn-Ads-MCP: Werbedaten aus LinkedIn lesen.

Jede Person meldet sich mit ihrem eigenen LinkedIn-Konto an und sieht genau
die Werbekonten, auf denen sie dort eine Rolle hat. Nur lesend.

Grundsätze dieses Codes:
  1. Jede Zeile ist für sich verständlich: sie trägt immer ihren vollständigen
     Pfad Werbekonto > Kampagne > Anzeigengruppe > Anzeige.
  2. Berechnet wird nur, was zwingend aus den gelieferten Daten folgt
     (zentrale Formeltabelle DERIVED), keine Annahmen.
  3. Nichts verschwindet stillschweigend: angefragte, aber nicht gelieferte
     Kennzahlen und gekürzte Antworten werden gemeldet.
  4. Alle Werkzeuge antworten über denselben Ausgabeweg (_respond) mit
     derselben Größenbremse.
  5. Fachwissen über LinkedIn steht in den Beschreibungen (INSTRUCTIONS),
     nicht als Sonderregel im Code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Annotated, Callable, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import api, names
from .auth import AuthError, delete_token, login_interactive, token_status

# --- Fachwissen (Grundsatz 5) ---------------------------------------------------

INSTRUCTIONS = """\
Liest LinkedIn-Werbedaten der Werbekonten, auf denen die angemeldete Person eine Rolle hat. \
Nur lesen, keine Änderungen.

Begriffe wie im Campaign Manager, auch in Antworten verwenden: Werbekonto > Kampagne \
(API: campaign group) > Anzeigengruppe (API: campaign) > Anzeige (API: creative). \
Gleich benannte Kampagnen kommen in mehreren Werbekonten vor; immer das Werbekonto mit nennen.

Ablauf: list_ad_accounts, dann get_analytics oder list_campaign_structure. \
Fehlt ein Kunde in list_ad_accounts, hat die Person auf dessen Werbekonto keine Rolle: das offen sagen \
statt Zahlen zu raten. Das Feld 'hinweise' immer an die Person weitergeben.

Wie LinkedIn zählt:
- Kosten sind in der Währung des Werbekontos.
- Conversions werden dem Tag zugeordnet, an dem sie stattfinden, und können noch lange nach der \
letzten Auslieferung einer Anzeige anfallen. Monate mit Conversions, aber kaum Ausgaben, \
haben deshalb unrealistisch niedrige Kosten pro Conversion. Für Bewertungen die gesamte \
Laufzeit inklusive Nachlauf betrachten und auf diesen Effekt hinweisen.
- Reichweite (approximateMemberReach) zählt eindeutige Personen. Sie lässt sich nicht über \
Zeilen oder Zeiträume addieren; Frequenz = Impressionen / Reichweite gilt nur je Zeile. \
LinkedIn liefert Reichweite nur für Zeiträume bis etwa 92 Tage (geprüft: 61 Tage ja, \
123 Tage nein) und nicht bei Demografie. Für längere Zeiträume in Abschnitte bis 3 Monate teilen.
- Kosten pro Lead oder Conversion auf Konto-, Kampagnen- oder Demografieebene enthalten auch \
Ausgaben von Anzeigengruppen mit anderem Ziel. Für Bewertungen auf Anzeigengruppen mit \
passendem Ziel eingrenzen (Feld 'ziel' in list_campaign_structure) und das dazusagen.
- Demografische Aufschlüsselungen (MEMBER_*) sind geschätzt und fassen alle abgefragten Konten \
zusammen. Die Zeilen überschneiden sich oder lassen Teile weg: Ihre Summe lag im Test bei \
Branchen bei 255 % der echten Kosten, bei Jobtiteln bei 59 %, Conversions oft unter 30 %. \
Gesamtwerte und Anteile immer aus 'gesamt_laut_konten' nehmen, nie Zeilen aufaddieren; \
'abdeckung_prozent' mit nennen.
- Video-Quoten (video_view_rate_prozent, video_abschlussquote_prozent) sind wie im Campaign \
Manager auf die Impressionen der Videoanzeigen bezogen und gibt es nur bei Aufschlüsselung nach \
Anzeigengruppe oder Anzeige. Für ein Konto oder eine Kampagne nach Anzeigengruppe aufschlüsseln \
und die Summen nehmen.
- 'gebot' in list_campaign_structure fehlt bei automatischer Gebotsstrategie (MAX_*): Dort setzt \
LinkedIn die Kosten selbst.
- Einheiten nicht selbsterklärender Felder stehen in list_metrics unter 'einheiten' \
(z. B. averageVideoWatchTime in Millisekunden, audiencePenetration als Anteil 0 bis 1).
- Tageswerte gibt es rund 6 Monate rückwirkend, Monatswerte bis 10 Jahre, Demografie 2 Jahre.
- Leere Ergebnisse bedeuten entweder keine Auslieferung oder keine Leserechte; LinkedIn meldet \
fehlende Rechte nicht als Fehler.
"""

mcp = FastMCP("linkedin-ads", instructions=INSTRUCTIONS)

# --- Ausgabe (Grundsatz 4) ------------------------------------------------------

# Obergrenze für die Größe einer Antwort (Zeichen). Per Umgebungsvariable einstellbar.
MAX_CHARS = int(os.environ.get("LINKEDIN_MCP_MAX_CHARS", "35000"))


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _respond(
    result: dict,
    lists: list[tuple[str, Callable[[dict], float] | None]] = (),
    advice: str = "",
) -> str:
    """Gemeinsamer Ausgabeweg. Passt die Antwort nicht unter MAX_CHARS, werden die
    genannten Listen gekürzt: nach dem Sortierschlüssel (höchster Wert zuerst) oder,
    ohne Schlüssel, in der vorhandenen Reihenfolge. Alles andere bleibt vollständig."""
    result = {k: v for k, v in result.items() if v is not None}
    hinweise = result.setdefault("hinweise", [])
    if len(_dump(result)) <= MAX_CHARS:
        if not hinweise:
            del result["hinweise"]
        return _dump(result)

    present = [(k, key) for k, key in lists if isinstance(result.get(k), list) and result[k]]
    full = {k: result[k] for k, _ in present}
    for k, _ in present:
        result[k] = []
    budget = MAX_CHARS - len(_dump(result)) - 400
    notes = []
    # Kleine Listen zuerst; ungenutzter Platz geht an die nächste Liste.
    present.sort(key=lambda p: len(_dump(full[p[0]])))
    for i, (k, key) in enumerate(present):
        share = budget // (len(present) - i)
        items = full[k]
        order = sorted(items, key=lambda x: -(key(x) or 0)) if key else list(items)
        kept, used = [], 0
        for it in order:
            s = len(_dump(it)) + 1
            if used + s > share:
                break
            kept.append(it)
            used += s
        budget -= used
        if key:  # ursprüngliche Reihenfolge der behaltenen Einträge
            keep_ids = {id(x) for x in kept}
            kept = [x for x in items if id(x) in keep_ids]
        result[k] = kept
        if len(kept) < len(items):
            notes.append(f"{k}: {len(kept)} von {len(items)}")
    if notes:
        hinweise.append(
            "Antwort gekürzt (" + "; ".join(notes) + "). Summen und Kopfangaben sind vollständig."
            + (" " + advice if advice else "")
        )
    return _dump(result)


def _error(e: Exception) -> str:
    return _dump({"fehler": str(e)})


# --- Kennzahlen -----------------------------------------------------------------

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
MONEY_FIELDS = {"costInLocalCurrency", "costInUsd", "conversionValueInLocalCurrency", "costPerQualifiedLead"}

# Nicht addierbar (eindeutige Personen, Durchschnitte, Quoten): keine Summen bilden.
NON_ADDITIVE = {
    "approximateMemberReach",
    "audiencePenetration",
    "averageDwellTime",
    "averageVideoWatchTime",
    "costPerQualifiedLead",
}

# Zentrale Formeltabelle (Grundsatz 2): Name, Zähler, Nenner, Faktor.
# Berechnet wird nur, wenn beide Felder geliefert wurden und der Nenner > 0 ist.
# Neue Kennzahl = eine Zeile.
DERIVED: list[tuple[str, str, str, float]] = [
    ("ctr_prozent", "clicks", "impressions", 100),
    ("engagement_rate_prozent", "totalEngagements", "impressions", 100),
    ("cpc", "costInLocalCurrency", "clicks", 1),
    ("cpm", "costInLocalCurrency", "impressions", 1000),
    ("kosten_pro_lead", "costInLocalCurrency", "oneClickLeads", 1),
    ("kosten_pro_conversion", "costInLocalCurrency", "externalWebsiteConversions", 1),
    ("lead_formular_abschlussquote_prozent", "oneClickLeads", "oneClickLeadFormOpens", 100),
    ("frequenz", "impressions", "approximateMemberReach", 1),
    # Video-Quoten wie im Campaign Manager: bezogen auf die Impressionen der Videoanzeigen
    # (videoImpressions = Impressionen von Zeilen mit Videostarts). Geprüft gegen
    # CM-Exporte. Nur bei Aufschlüsselung nach Anzeigengruppe oder Anzeige vorhanden,
    # weil Konto- und Kampagnenzeilen Bild und Video mischen.
    ("video_view_rate_prozent", "videoViews", "videoImpressions", 100),
    ("video_abschlussquote_prozent", "videoCompletions", "videoImpressions", 100),
]

# Aufschlüsselungen, deren Zeilen genau ein Anzeigenformat haben.
FORMAT_PURE_PIVOTS = {"CAMPAIGN", "CREATIVE"}

# Einheiten, die nicht selbsterklärend sind; geprüft gegen den Campaign Manager.
UNITS = {
    "averageDwellTime": "Sekunden",
    "averageVideoWatchTime": "Millisekunden (9.739 = 9,74 Sek.)",
    "videoWatchTime": "Millisekunden",
    "audiencePenetration": "Anteil von 0 bis 1 (0,737 = 73,7 %)",
    "videoImpressions": "berechnet: Impressionen der Anzeigengruppen bzw. Anzeigen mit Videostarts",
}

# Anteile (0 bis 1) brauchen mehr Nachkommastellen als 2.
FRACTION_FIELDS = {"audiencePenetration"}

# Höchstzahl demografischer Werte, deren Namen pro Antwort nachgeschlagen werden.
# Mehr passt ohnehin nicht in eine Antwort.
NAME_LOOKUP_LIMIT = 300

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
        "videoWatchTime", "averageVideoWatchTime", "fullScreenPlays",
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

Pivot = Literal[
    "ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE",
    "MEMBER_JOB_TITLE", "MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY",
    "MEMBER_COMPANY_SIZE", "MEMBER_COMPANY", "MEMBER_COUNTRY_V2", "MEMBER_REGION_V2",
]

MAX_METRICS = 18  # LinkedIn erlaubt 20 Felder, 2 gehen an dateRange und pivotValues.


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
        if field in FRACTION_FIELDS:
            return round(v, 4)
        return int(v) if v.is_integer() else round(v, 2)
    return v


def _derived(values: dict) -> dict:
    out = {}
    for name, num, den, factor in DERIVED:
        if num in values and den in values and _num(values[den]) > 0:
            out[name] = round(_num(values[num]) / _num(values[den]) * factor, 2)
    return out


def _ratio(a: float, b: float, factor: float = 1.0, digits: int = 2):
    return round(a / b * factor, digits) if b else None


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


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# --- Kontext (Grundsatz 1) ------------------------------------------------------

class Context:
    """Kennt Werbekonten, Kampagnen, Anzeigengruppen und Anzeigen der abgefragten
    Konten und liefert für jede Kennung den vollständigen Pfad."""

    def __init__(self, account_ids: list[str]):
        self.account_ids = [str(a) for a in account_ids]
        self.accounts: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.campaigns: dict[str, dict] = {}
        self.creatives: dict[str, dict] = {}
        self.problems: list[str] = []

    async def load_accounts(self) -> None:
        res = await asyncio.gather(*(api.ad_account(a) for a in self.account_ids), return_exceptions=True)
        for a, r in zip(self.account_ids, res):
            if isinstance(r, Exception):
                self.problems.append(f"Werbekonto {a} nicht lesbar: {r}")
            else:
                self.accounts[a] = r

    async def load_structure(self) -> None:
        n = len(self.account_ids)
        res = await asyncio.gather(
            *(api.search_campaign_groups(a) for a in self.account_ids),
            *(api.search_campaigns(a) for a in self.account_ids),
            return_exceptions=True,
        )
        for a, gl in zip(self.account_ids, res[:n]):
            if isinstance(gl, Exception):
                self.problems.append(f"Kampagnen von Konto {a} nicht lesbar: {gl}")
                continue
            for g in gl:
                self.groups[str(g["id"])] = {**g, "_account": a}
        for a, cl in zip(self.account_ids, res[n:]):
            if isinstance(cl, Exception):
                self.problems.append(f"Anzeigengruppen von Konto {a} nicht lesbar: {cl}")
                continue
            for c in cl:
                self.campaigns[str(c["id"])] = {**c, "_account": a}

    async def load_creatives(self, urns: list[str]) -> None:
        res = await asyncio.gather(
            *(api.creatives_by_ids(a, urns) for a in self.account_ids), return_exceptions=True
        )
        for a, cl in zip(self.account_ids, res):
            if isinstance(cl, Exception):
                self.problems.append(f"Anzeigen von Konto {a} nicht lesbar: {cl}")
                continue
            for cr in cl:
                self.creatives[str(cr.get("id"))] = {**cr, "_account": a}

    def currency(self, account_id: str | None) -> str | None:
        return (self.accounts.get(account_id or "") or {}).get("currency")

    def path(self, *, account: str | None = None, group: str | None = None,
             campaign: str | None = None, creative_urn: str | None = None) -> dict:
        """Vollständiger Pfad von unten nach oben aufgelöst."""
        out: dict = {}
        if creative_urn:
            cr = self.creatives.get(creative_urn, {})
            if cr.get("campaign"):
                campaign = api.urn_id(cr["campaign"])
            account = account or cr.get("_account")
            out["anzeige"] = cr.get("name") or f"Anzeige {api.urn_id(creative_urn)} (ohne Namen)"
            out["anzeige_status"] = cr.get("intendedStatus")
        if campaign:
            c = self.campaigns.get(campaign, {})
            if c.get("campaignGroup"):
                group = api.urn_id(c["campaignGroup"])
            account = account or c.get("_account")
            out["anzeigengruppe"] = c.get("name")
        if group:
            g = self.groups.get(group, {})
            account = account or g.get("_account")
            out["kampagne"] = g.get("name")
        if account:
            out["werbekonto"] = (self.accounts.get(account) or {}).get("name")
            out["waehrung"] = self.currency(account)
        # Reihenfolge von oben nach unten für die Lesbarkeit
        order = ["werbekonto", "kampagne", "anzeigengruppe", "anzeige", "anzeige_status", "waehrung"]
        return {k: out[k] for k in order if out.get(k) is not None}


# --- Werkzeuge ------------------------------------------------------------------

@mcp.tool
async def login() -> str:
    """Bei LinkedIn anmelden. Öffnet den Browser; die Person meldet sich an und bestätigt.
    Nur nötig beim ersten Start oder wenn ein anderes Werkzeug meldet, dass die Anmeldung fehlt
    oder abgelaufen ist. Wartet bis zu 3 Minuten auf die Bestätigung."""
    try:
        await login_interactive(timeout_s=170)
        return _respond({"ergebnis": "Angemeldet.", **token_status()})
    except AuthError as e:
        return _error(e)


@mcp.tool
async def auth_status() -> str:
    """Zeigt, ob eine LinkedIn-Anmeldung besteht und wie lange sie noch gültig ist."""
    return _respond(token_status())


@mcp.tool
async def list_ad_accounts() -> str:
    """Alle LinkedIn-Werbekonten, auf die die angemeldete Person Zugriff hat, mit Name,
    Währung, Status und eigener Rolle. Grundlage für alle weiteren Abfragen."""
    try:
        roles = await api.ad_account_roles()
        ctx = Context([api.urn_id(r["account"]) for r in roles])
        await ctx.load_accounts()
        out = []
        for r in roles:
            a = api.urn_id(r["account"])
            d = ctx.accounts.get(a, {})
            out.append(_drop_none({
                "id": a, "name": d.get("name"), "rolle": r.get("role"),
                "waehrung": d.get("currency"), "status": d.get("status"), "typ": d.get("type"),
            }))
        out.sort(key=lambda x: (x.get("name") or "").lower())
        return _respond({"anzahl": len(out), "konten": out, "hinweise": ctx.problems},
                        lists=[("konten", None)])
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
        Field(description="Ausgaben seit Start und Gesamtbudget-Auslastung mitliefern."),
    ] = True,
) -> str:
    """Kampagnen eines Werbekontos mit ihren Anzeigengruppen (Begriffe wie im Campaign Manager):
    Status, Ziel, Laufzeit, Budgets, Gebot und auf Wunsch Ausgaben seit Start und
    Auslastung des Gesamtbudgets. Neueste Kampagnen zuerst."""
    try:
        ctx = Context([account_id])
        await asyncio.gather(ctx.load_accounts(), ctx.load_structure())
        today = datetime.now(timezone.utc).date()
        camps = [c for c in ctx.campaigns.values() if not statuses or c.get("status") in statuses]

        spend: dict[str, dict[str, float]] = {"CAMPAIGN": {}, "CAMPAIGN_GROUP": {}}
        if include_spend and camps:
            starts = [_ms_to_date((o.get("runSchedule") or {}).get("start"))
                      for o in list(camps) + list(ctx.groups.values())]
            first = min([s for s in starts if s] or [today])
            res = await asyncio.gather(*(
                api.analytics(account_ids=[account_id], start=first, end=today, pivot=p,
                              granularity="ALL", fields=["costInLocalCurrency", "pivotValues"])
                for p in spend
            ))
            for p, rows in zip(spend, res):
                for r in rows:
                    spend[p][api.urn_id(r["pivotValues"][0])] = _num(r.get("costInLocalCurrency"))

        def run(obj: dict) -> tuple[str | None, str | None]:
            rs = obj.get("runSchedule") or {}
            s, e = _ms_to_date(rs.get("start")), _ms_to_date(rs.get("end"))
            return (s.isoformat() if s else None, e.isoformat() if e else None)

        def with_spend(row: dict, level: str, oid: str, budget: float | None) -> dict:
            if include_spend:
                spent = spend[level].get(oid, 0.0)
                row["ausgegeben_seit_start"] = round(spent, 2)
                if budget:
                    row["gesamtbudget_auslastung_prozent"] = _ratio(spent, budget, 100, 1)
            return row

        by_group: dict[str, list[dict]] = {}
        for c in camps:
            cid = str(c["id"])
            s, e = run(c)
            row = {
                "id": cid, "anzeigengruppe": c.get("name"), "status": c.get("status"),
                "ziel": c.get("objectiveType"), "format": c.get("type"), "start": s, "ende": e,
                "tagesbudget": _money(c.get("dailyBudget")), "gesamtbudget": _money(c.get("totalBudget")),
                "gebotsart": c.get("costType"), "gebotsstrategie": c.get("optimizationTargetType"),
                # Bei automatischer Gebotsabgabe (MAX_*) gibt es kein eigenes Gebot; LinkedIn
                # lässt dort beliebige Restwerte stehen.
                "gebot": None if str(c.get("optimizationTargetType") or "").startswith("MAX_")
                         else _money(c.get("unitCost")),
            }
            gid = api.urn_id(c["campaignGroup"]) if c.get("campaignGroup") else "ohne"
            by_group.setdefault(gid, []).append(_drop_none(with_spend(row, "CAMPAIGN", cid, row["gesamtbudget"])))

        out = []
        for gid, members in by_group.items():
            g = ctx.groups.get(gid, {})
            s, e = run(g)
            row = {"id": gid, "kampagne": g.get("name"), "status": g.get("status"), "start": s, "ende": e,
                   "gesamtbudget": _money(g.get("totalBudget"))}
            row = with_spend(row, "CAMPAIGN_GROUP", gid, row["gesamtbudget"])
            row["anzeigengruppen"] = members
            out.append(_drop_none(row))
        out.sort(key=lambda x: str(x.get("start") or ""), reverse=True)

        acc = ctx.accounts.get(str(account_id), {})
        hinweise = list(ctx.problems)
        if include_spend:
            hinweise.append(f"Ausgaben bis heute ({today.isoformat()}, UTC).")
        return _respond(
            {
                "werbekonto": acc.get("name"),
                "waehrung": acc.get("currency"),
                "anzahl_kampagnen": len(out),
                "anzahl_anzeigengruppen": len(camps),
                "kampagnen": out,
                "hinweise": hinweise,
            },
            lists=[("kampagnen", None)],
            advice="Ältere Kampagnen wurden weggelassen; mit 'statuses' (z. B. ACTIVE, PAUSED) eingrenzen.",
        )
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
            description="Aufschlüsselung. ACCOUNT = Werbekonto, CAMPAIGN_GROUP = Kampagne, "
            "CAMPAIGN = Anzeigengruppe, CREATIVE = Anzeige (Begriffe wie im Campaign Manager), "
            "MEMBER_* = Demografie der erreichten Mitglieder über alle abgefragten Konten."
        ),
    ] = "CAMPAIGN",
    time_granularity: Annotated[
        Literal["ALL", "DAILY", "MONTHLY", "YEARLY"],
        Field(description="ALL = Summe über den Zeitraum, sonst je Tag/Monat/Jahr."),
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
        Field(description="Optional nur die N Einträge (Kampagnen, Anzeigen, …) mit den höchsten "
              "Kosten, ohne Kosten mit den meisten Impressionen (z. B. Top 10). Bei Zeitreihen "
              "bleiben alle Zeiträume dieser Einträge erhalten. Summen enthalten trotzdem alles."),
    ] = None,
) -> str:
    """Kennzahlen aus LinkedIn für Zeitraum und Aufschlüsselung. Jede Zeile trägt ihren
    vollständigen Pfad (Werbekonto, Kampagne, Anzeigengruppe, Anzeige) bzw. den Klartext der
    demografischen Gruppe. Liefert Rohwerte, berechnete Kennzahlen (siehe list_metrics) und Summen
    über alle Zeilen; bei Zeitreihen zusätzlich Summen je Wert."""
    hinweise: list[str] = []
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            return _error(ValueError("end_date liegt vor start_date."))

        fields = list(dict.fromkeys(metrics or DEFAULT_METRICS))
        demographic = pivot.startswith("MEMBER_")
        if demographic:
            dropped = [f for f in fields if f in NON_DEMOGRAPHIC_ONLY]
            if dropped:
                fields = [f for f in fields if f not in NON_DEMOGRAPHIC_ONLY]
                hinweise.append(f"Bei Demografie von LinkedIn nicht verfügbar, weggelassen: {', '.join(dropped)}.")
        if len(fields) > MAX_METRICS:
            return _error(ValueError(f"Höchstens {MAX_METRICS} Kennzahlen pro Abfrage."))

        ctx = Context(account_ids)
        jobs = [
            api.analytics(account_ids=account_ids, start=start, end=end, pivot=pivot,
                          granularity=time_granularity, fields=fields + ["dateRange", "pivotValues"],
                          campaign_ids=campaign_ids),
            ctx.load_accounts(),
        ]
        # Demografie: Zeilen überschneiden sich oder fehlen. Die echten Gesamtwerte kommen
        # aus derselben Abfrage auf Kontoebene (Grundsatz 3).
        if demographic:
            jobs.append(api.analytics(account_ids=account_ids, start=start, end=end, pivot="ACCOUNT",
                                      granularity="ALL", fields=fields + ["pivotValues"],
                                      campaign_ids=campaign_ids))
        res = await asyncio.gather(*jobs)
        rows = res[0]
        account_rows = res[2] if demographic else []
        if len(rows) >= api.ANALYTICS_MAX_ELEMENTS:
            hinweise.append(
                f"LinkedIn hat die Höchstzahl von {api.ANALYTICS_MAX_ELEMENTS} Zeilen geliefert; "
                "das Ergebnis ist vermutlich unvollständig."
            )

        # Grundsatz 3: angefragt, aber nicht (überall) geliefert
        if rows:
            for f in fields:
                have = sum(1 for r in rows if f in r)
                if have == 0:
                    hinweise.append(f"Von LinkedIn für diese Abfrage nicht geliefert: {f}.")
                elif have < len(rows):
                    hinweise.append(f"{f} fehlt in {len(rows) - have} von {len(rows)} Zeilen.")

        values = list(dict.fromkeys((r.get("pivotValues") or [None])[0] for r in rows))

        # Eine Rangfolge für alles (Sortierung, max_rows, Kürzung, Namenssuche): Kosten,
        # sonst Impressionen, sonst die erste angefragte Kennzahl.
        rank = next((f for f in ("costInLocalCurrency", "impressions") if f in fields), fields[0])
        rank_label = {"costInLocalCurrency": "Kosten", "impressions": "Impressionen"}.get(rank, rank)

        # Summe je Wert über alle Zeiträume; Grundlage für Rangfolge und max_rows.
        groups: dict = {}
        for r in rows:
            groups.setdefault((r.get("pivotValues") or [None])[0], []).append(r)
        value_rank = {pv: sum(_num(r.get(rank)) for r in rs) for pv, rs in groups.items()}
        # max_rows wählt Werte (Kampagnen, Anzeigen, …), nicht einzelne Zeilen: bei
        # Zeitreihen bleiben so alle Zeiträume eines Werts erhalten.
        top = None
        if max_rows and len(groups) > max_rows:
            top = set(sorted(groups, key=lambda pv: -value_rank[pv])[:max_rows])

        # Grundsatz 1: Pfad bzw. Klartext je Wert
        labels: dict[str, dict] = {}
        if pivot in ("CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE"):
            await ctx.load_structure()
            if pivot == "CREATIVE":
                await ctx.load_creatives([v for v in values if v])
            for v in values:
                if not v:
                    continue
                if pivot == "CAMPAIGN_GROUP":
                    labels[v] = ctx.path(group=api.urn_id(v))
                elif pivot == "CAMPAIGN":
                    labels[v] = ctx.path(campaign=api.urn_id(v))
                else:
                    labels[v] = ctx.path(creative_urn=v)
        elif pivot == "ACCOUNT":
            for v in values:
                if v:
                    labels[v] = ctx.path(account=api.urn_id(v))
        else:
            # Namen nur für Einträge nachschlagen, die in der Antwort landen können;
            # bei Unternehmen oder Jobtiteln sind es sonst Tausende.
            ordered = sorted((v for v in values if v), key=lambda v: -value_rank.get(v, 0))
            wanted = [v for v in ordered if v in top] if top else ordered[:NAME_LOOKUP_LIMIT]
            resolved, missing = await names.resolve(wanted)
            for v in wanted:
                if resolved.get(v):
                    labels[v] = {"name": resolved[v]}
            unresolved = sum(1 for v in wanted if str(v).startswith("urn:li:") and not resolved.get(v))
            if unresolved:
                hinweise.append(f"{unresolved} von {len(wanted)} nachgeschlagenen Werten ohne Klartext "
                                "von LinkedIn; dort steht die Kennung.")
            if missing:
                hinweise.append("Namen nicht nachschlagbar: " + "; ".join(missing))
        hinweise += ctx.problems

        currencies = {c for c in (a.get("currency") for a in ctx.accounts.values()) if c}
        multi_currency = len(currencies) > 1
        if multi_currency:
            hinweise.append(
                f"Unterschiedliche Währungen ({', '.join(sorted(currencies))}): Geldbeträge werden nicht summiert."
            )

        def build(r: dict) -> dict:
            pv = (r.get("pivotValues") or [None])[0]
            row = {"von": _date_str((r.get("dateRange") or {}).get("start")),
                   "bis": _date_str((r.get("dateRange") or {}).get("end")),
                   **labels.get(pv, {}), "wert": pv}
            row.update({f: _clean(f, r[f]) for f in fields if f in r})
            row.update(_derived(r))
            return _drop_none(row)

        # Nenner der Video-Quoten (siehe DERIVED): nur wo jede Zeile genau ein Format hat.
        if pivot in FORMAT_PURE_PIVOTS and "videoStarts" in fields and "impressions" in fields:
            for r in rows:
                if _num(r.get("videoStarts")) > 0:
                    r["videoImpressions"] = r.get("impressions")
        elif {"videoViews", "videoCompletions"} & set(fields) and not demographic:
            hinweise.append("Video-Quoten gibt es nur bei Aufschlüsselung nach Anzeigengruppe oder "
                            "Anzeige (wie im Campaign Manager auf die Impressionen der Videoanzeigen bezogen).")

        rows.sort(key=lambda r: (_date_str((r.get("dateRange") or {}).get("start")) or "",
                                 -_num(r.get(rank))))
        out_rows = [build(r) for r in rows]

        def totals_of(rs: list[dict]) -> dict:
            t: dict = {}
            for f in fields:
                if f in NON_ADDITIVE or (multi_currency and f in MONEY_FIELDS):
                    continue
                if any(f in r for r in rs):
                    t[f] = sum(_num(r.get(f)) for r in rs)
            if any("videoImpressions" in r for r in rs):
                t["videoImpressions"] = sum(_num(r.get("videoImpressions")) for r in rs)
            return t

        tot = totals_of(rows)
        summen = gesamt = abdeckung = zeilensumme = None
        if demographic:
            real = totals_of(account_rows)
            gesamt = {f: _clean(f, v) for f, v in real.items()}
            gesamt.update(_derived(real))
            zeilensumme = {f: _clean(f, v) for f, v in tot.items()}
            abdeckung = {f: _ratio(tot[f], real[f], 100, 1) for f in tot if _num(real.get(f)) > 0}
            hinweise.append(
                "Demografie: Die Zeilen überschneiden sich oder lassen Teile weg, ihre Summe ist "
                "kein Gesamtwert. Maßgeblich ist 'gesamt_laut_konten'; 'abdeckung_prozent' zeigt, "
                "welchen Anteil davon die Zeilen zusammen erfassen."
            )
        else:
            summen = {f: _clean(f, v) for f, v in tot.items()}
            summen.update(_derived(tot))

        je_wert = None
        if time_granularity != "ALL" and len(values) > 1:
            je_wert = []
            for pv, rs in groups.items():
                t = totals_of(rs)
                je_wert.append(_drop_none({**labels.get(pv, {}), "wert": pv,
                                           **{f: _clean(f, v) for f, v in t.items()}, **_derived(t)}))
            je_wert.sort(key=lambda x: -value_rank.get(x.get("wert"), 0))

        total_rows = len(out_rows)
        if top:
            out_rows = [x for x in out_rows if x.get("wert") in top]
            if je_wert:
                je_wert = [x for x in je_wert if x.get("wert") in top]
            hinweise.append(
                f"Auf die {max_rows} Einträge mit den höchsten {rank_label} begrenzt "
                f"(von {len(groups)}). Summen enthalten alle."
            )
        if not out_rows:
            hinweise.append("Keine Daten: keine Auslieferung im Zeitraum oder keine Leserechte auf den Konten.")

        return _respond(
            {
                "zeitraum": {"von": start_date, "bis": end_date},
                "werbekonten": [_drop_none({"id": a, "name": d.get("name"), "waehrung": d.get("currency")})
                                for a, d in ctx.accounts.items()],
                "aufschluesselung": f"{PIVOT_LABEL.get(pivot, pivot)} ({pivot})",
                "zeitraster": time_granularity,
                "anzahl_zeilen_gesamt": total_rows,
                "summen": summen,
                "gesamt_laut_konten": gesamt,
                "summe_der_zeilen": zeilensumme,
                "abdeckung_prozent": abdeckung,
                "summen_je_wert": je_wert,
                "zeilen": out_rows,
                "hinweise": hinweise,
            },
            lists=[("summen_je_wert", lambda x: value_rank.get(x.get("wert"), 0)),
                   ("zeilen", lambda x: _num(x.get(rank)))],
            advice="Für Details enger abfragen: kürzerer Zeitraum, weniger Konten, gröberes Zeitraster oder max_rows.",
        )
    except Exception as e:
        return _error(e)


@mcp.tool
async def list_metrics() -> str:
    """Verfügbare Kennzahlen (LinkedIn-Feldnamen) nach Gruppen, berechnete Kennzahlen mit
    Formel, mögliche Aufschlüsselungen und Einschränkungen."""
    return _respond(
        {
            "kennzahlen": METRIC_GROUPS,
            "hinweis_katalog": "Nachschlagehilfe; jede gültige LinkedIn-Kennzahl kann abgefragt werden.",
            "standardsatz": DEFAULT_METRICS,
            "berechnet": {name: f"{num} / {den}" + (f" × {factor:g}" if factor != 1 else "")
                          for name, num, den, factor in DERIVED},
            "einheiten": UNITS,
            "aufschluesselungen": PIVOT_LABEL,
            "nicht_bei_demografie": sorted(NON_DEMOGRAPHIC_ONLY),
            "nicht_summierbar": sorted(NON_ADDITIVE),
            "max_kennzahlen_pro_abfrage": MAX_METRICS,
            "max_zeichen_pro_antwort": MAX_CHARS,
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
