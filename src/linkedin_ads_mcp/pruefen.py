"""Abgleich des Servers gegen Exporte aus dem Campaign Manager.

Nur für die Pflege des Servers, nicht für die tägliche Nutzung:

    linkedin-ads-mcp pruefen <ordner>

Im Ordner liegen Exporte aus dem Campaign Manager: "Performance der Anzeigengruppe",
Spaltenansicht "Alle Spalten", Zeitaufschlüsselung "nach Monat sortiert"
(Dateien account_<id>_campaign_performance_report.csv).

Geprüft wird über dieselbe Funktion, die Claude aufruft (server.get_analytics),
also genau der Code, den alle benutzen:
  1. je Anzeigengruppe und Monat alle Rohwerte und berechneten Kennzahlen,
  2. für die letzten Monate je Monat Reichweite, Häufigkeit, Verweilzeit und
     durchschnittliche Videowiedergabezeit (Reichweite gibt es nur für kurze Zeiträume).

Die Exporte enthalten Kundendaten und gehören nicht ins Repository.
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import io
import json
import re
from datetime import date
from pathlib import Path

from . import server

# Spalte im Campaign-Manager-Export -> (Feld in get_analytics, Umrechnung, Toleranz)
RAW = {
    "Gesamtausgaben": ("costInLocalCurrency", 1, 0.011),
    "Impressions": ("impressions", 1, 0),
    "Klicks": ("clicks", 1, 0),
    "Reaktionen": ("reactions", 1, 0),
    "Kommentare": ("comments", 1, 0),
    "Shares": ("shares", 1, 0),
    "Folgen": ("follows", 1, 0),
    "Andere Klicks": ("otherEngagements", 1, 0),
    "Engagement insgesamt": ("totalEngagements", 1, 0),
    "Klicks auf die Zielseite": ("landingPageClicks", 1, 0),
    "Klicks auf die LinkedIn Unternehmensseite": ("companyPageClicks", 1, 0),
    "Conversions": ("externalWebsiteConversions", 1, 0),
    "Post-Klick-Conversions": ("externalWebsitePostClickConversions", 1, 0),
    "View-Through-Conversions": ("externalWebsitePostViewConversions", 1, 0),
    "Gesamtwert aller Conversions": ("conversionValueInLocalCurrency", 1, 0.011),
    "Leads": ("oneClickLeads", 1, 0),
    "Geöffnete Lead-Formulare": ("oneClickLeadFormOpens", 1, 0),
    "Leads (berufliche E-Mail-Adresse)": ("validWorkEmailLeads", 1, 0),
    "Hochwertige Leads": ("qualifiedLeads", 1, 0),
    "Event-Registrierungen": ("registrations", 1, 0),
    "Videowiedergaben": ("videoStarts", 1, 0),
    "Videoaufrufe": ("videoViews", 1, 0),
    "Videoaufrufe bis 25 %": ("videoFirstQuartileCompletions", 1, 0),
    "Videoaufrufe bis 50 %": ("videoMidpointCompletions", 1, 0),
    "Videoaufrufe bis 75 %": ("videoThirdQuartileCompletions", 1, 0),
    "Vollständige Videowiedergabe": ("videoCompletions", 1, 0),
    # CM: Sekunden, abgerundet; API: Millisekunden
    "Videowiedergabezeit insgesamt (in Sekunden)": ("videoWatchTime", 0.001, 1.0),
    "Vollbildwiedergaben": ("fullScreenPlays", 1, 0),
    "Virale Impressions": ("viralImpressions", 1, 0),
    "Virale Klicks": ("viralClicks", 1, 0),
    "Virale Reaktionen": ("viralReactions", 1, 0),
    "Virale Kommentare": ("viralComments", 1, 0),
    "Virale Shares": ("viralShares", 1, 0),
    "Virale „Folgen“-Klicks": ("viralFollows", 1, 0),
}

# Berechnete Kennzahlen: CM-Spalte -> Feld des Servers. CM rundet Prozente auf 3,
# Geld auf 2 Stellen, der Server auf 2.
DERIVED = {
    "Klickrate": ("ctr_prozent", 0.011),
    "Engagement Rate": ("engagement_rate_prozent", 0.011),
    "Durchschn. CPM": ("cpm", 0.011),
    "Durchschn. CPC": ("cpc", 0.011),
    "Kosten pro Lead": ("kosten_pro_lead", 0.011),
    "Kosten pro Conversion": ("kosten_pro_conversion", 0.011),
    "Rate ausgefüllter Lead-Formulare": ("lead_formular_abschlussquote_prozent", 0.011),
    "Videoaufrufrate": ("video_view_rate_prozent", 0.011),
    "Rate vollständig angesehener Videos": ("video_abschlussquote_prozent", 0.011),
}

# Nur je Einzelmonat abfragbar (Reichweite höchstens rund 92 Tage).
PER_MONTH = {
    "Reichweite": ("approximateMemberReach", 1, 0),
    "Durchschnittliche Häufigkeit": ("frequenz", 1, 0.006),
    "Durchschnittliche Verweilzeit (in Sekunden)": ("averageDwellTime", 1, 0.006),
    "Durchschnittliche Videowiedergabezeit (in Sekunden)": ("averageVideoWatchTime", 0.001, 0.006),
}

MONTHS_DE = {m: i for i, m in enumerate(
    ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
     "September", "Oktober", "November", "Dezember"], 1)}

MAX_FIELDS = server.MAX_METRICS


# Spalten, in denen der Campaign Manager einen Punkt als Dezimaltrennzeichen schreibt
# (z. B. Verweilzeit "5.914" = 5,914 Sekunden), sonst gilt deutsches Format.
DOT_DECIMAL = {"Durchschnittliche Verweilzeit (in Sekunden)"}


def _num(s: str | None, dot_decimal: bool = False) -> float | None:
    s = (s or "").replace("\xa0", " ").replace("%", "").strip()
    if s in ("", "-"):
        return None
    if dot_decimal:
        s = s.replace(",", ".")
    elif "," in s:                                 # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):    # 1.234
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _de_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", text)
    if not m or m.group(2) not in MONTHS_DE:
        return None
    return date(int(m.group(3)), MONTHS_DE[m.group(2)], int(m.group(1)))


def _month_key(cm_start: str) -> str:
    d, mo, y = cm_start.split(".")
    return f"{int(y):04d}-{int(mo):02d}"


def read_export(path: Path) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    lines = text.splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith("Startdatum (in UTC)\t"))
    meta = lines[:head]
    start = next((_de_date(l) for l in meta if l.startswith("Startdatum des Berichts")), None)
    end = next((_de_date(l) for l in meta if l.startswith("Enddatum des Berichts")), None)
    reader = csv.reader(io.StringIO("\n".join(lines[head:])), delimiter="\t")
    header = [h.replace("\xa0", " ") for h in next(reader)]
    rows = [dict(zip(header, r)) for r in reader if r]
    account_id = re.search(r"account_(\d+)_", path.name).group(1)
    return {"account_id": account_id, "start": start, "end": end, "rows": rows,
            "name": rows[0].get("Account-Name") if rows else account_id}


async def _query(account_id: str, start: date, end: date, granularity: str, fields: list[str]) -> dict:
    """Server-Antwort je (Anzeigengruppen-ID, Monat), in Feldblöcken."""
    out: dict = {}
    # Kosten und Impressionen in jedem Block, damit alle Quoten (CPM, Video-Quoten …)
    # in jedem Block berechnet werden können.
    base = ["costInLocalCurrency", "impressions"]
    rest = [f for f in fields if f not in base]
    step = MAX_FIELDS - len(base)
    for i in range(0, max(len(rest), 1), step):
        block = base + rest[i:i + step]
        res = json.loads(await server.get_analytics(
            account_ids=[account_id], start_date=start.isoformat(), end_date=end.isoformat(),
            pivot="CAMPAIGN", time_granularity=granularity, metrics=block))
        if "fehler" in res:
            raise RuntimeError(res["fehler"])
        for z in res.get("zeilen", []):
            key = (z["wert"].rsplit(":", 1)[-1], (z.get("von") or "")[:7])
            out.setdefault(key, {}).update(z)
    return out


class Report:
    def __init__(self):
        self.checked: dict[str, int] = {}
        self.diffs: list[tuple] = []
        self.missing: dict[str, int] = {}

    def compare(self, label, konto, gruppe, monat, cm, srv, tol):
        if cm is None and srv is None:
            return
        if srv is None:
            if cm:  # CM hat einen Wert, der Server nicht
                self.missing[label] = self.missing.get(label, 0) + 1
            return
        self.checked[label] = self.checked.get(label, 0) + 1
        if abs((cm or 0) - srv) > tol:
            self.diffs.append((label, konto, gruppe, monat, cm, round(srv, 4)))


async def run(folder: Path, recent_months: int = 6) -> int:
    server.MAX_CHARS = 10**9  # beim Prüfen nichts kürzen
    files = sorted(folder.glob("account_*_campaign_performance_report.csv"))
    if not files:
        print(f"Keine Exporte in {folder} gefunden (account_<id>_campaign_performance_report.csv).")
        return 2
    rep = Report()
    for f in files:
        ex = read_export(f)
        print(f"Prüfe {ex['name']} ({ex['account_id']}), {ex['start']} bis {ex['end']}, "
              f"{len(ex['rows'])} Monatszeilen …")
        raw_fields = [v[0] for v in RAW.values()]
        srv = await _query(ex["account_id"], ex["start"], ex["end"], "MONTHLY", raw_fields)

        for r in ex["rows"]:
            key = (r["Anzeigengruppen-ID"], _month_key(r["Startdatum (in UTC)"]))
            z = srv.get(key, {})
            ctx = (ex["name"], r.get("Name der Anzeigengruppe", "")[:40], key[1])
            for col, (field, factor, tol) in RAW.items():
                if col in r:
                    v = z.get(field)
                    rep.compare(field, *ctx, _num(r[col]), None if v is None else v * factor, tol)
            for col, (field, tol) in DERIVED.items():
                cmv = _num(r.get(col))
                if col in r and not (cmv == 0 and field not in z):  # CM zeigt 0 statt "nicht berechenbar"
                    rep.compare(field, *ctx, cmv, z.get(field), tol)

        # Reichweite & Co. je Einzelmonat, nur die letzten Monate
        months = sorted({_month_key(r["Startdatum (in UTC)"]) for r in ex["rows"]})[-recent_months:]
        for mk in months:
            y, m = map(int, mk.split("-"))
            ms = max(date(y, m, 1), ex["start"])
            me = min(date(y, m, calendar.monthrange(y, m)[1]), ex["end"])
            fields = ["impressions"] + [v[0] for v in PER_MONTH.values() if v[0] != "frequenz"]
            one = await _query(ex["account_id"], ms, me, "ALL", fields)
            by_gid = {k[0]: v for k, v in one.items()}
            for r in ex["rows"]:
                if _month_key(r["Startdatum (in UTC)"]) != mk:
                    continue
                z = by_gid.get(r["Anzeigengruppen-ID"], {})
                ctx = (ex["name"], r.get("Name der Anzeigengruppe", "")[:40], mk)
                for col, (field, factor, tol) in PER_MONTH.items():
                    if col in r:
                        v = z.get(field)
                        cmv = _num(r[col], dot_decimal=col in DOT_DECIMAL)
                        if cmv == 0 and v is None:
                            continue
                        rep.compare(field, *ctx, cmv, None if v is None else v * factor, tol)

    total = sum(rep.checked.values())
    print(f"\n{total} Werte geprüft, {len(rep.diffs)} Abweichungen.\n")
    for label, n in sorted(rep.checked.items()):
        bad = sum(1 for d in rep.diffs if d[0] == label)
        print(f"  {label:42s} {n:6d}  {'OK' if not bad else f'{bad} Abweichungen'}")
    if rep.missing:
        print("\nIm Campaign Manager vorhanden, vom Server nicht geliefert:")
        for label, n in sorted(rep.missing.items()):
            print(f"  {label:42s} {n:6d}")
    if rep.diffs:
        print("\nAbweichungen (Feld, Konto, Anzeigengruppe, Monat, Campaign Manager, Server):")
        for d in rep.diffs[:60]:
            print("  ", d)
        if len(rep.diffs) > 60:
            print(f"   … und {len(rep.diffs) - 60} weitere")
    return 1 if rep.diffs else 0


def main(argv: list[str]) -> int:
    if not argv:
        print("Aufruf: linkedin-ads-mcp pruefen <ordner mit Campaign-Manager-Exporten> [monate]")
        return 2
    months = int(argv[1]) if len(argv) > 1 else 6
    return asyncio.run(run(Path(argv[0]).expanduser(), months))
