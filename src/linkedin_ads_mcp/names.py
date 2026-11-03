"""Kennungen (urn:li:…) aus demografischen Aufschlüsselungen in Klartext übersetzen.

Grundsatz: nichts fest eintragen. Namen kommen live von LinkedIn. Klappt eine
Übersetzung nicht, bleibt die Kennung stehen und die Antwort sagt das.

Die Nachschlage-Schnittstellen sind bei LinkedIn uneinheitlich dokumentiert.
Deshalb werden pro Typ mehrere Wege probiert; der erste funktionierende wird
für die Laufzeit des Servers gemerkt.
"""

from __future__ import annotations

import asyncio
import re

from . import api

# Ein Weg: (Basis, Pfad, versioniert?, Query). {ids} = kommagetrennte IDs.
# Query ohne {ids} = "alle holen" (nur bei kleinen Listen sinnvoll).
_LOC_REST = "(language:de,country:DE)"
_CANDIDATES: dict[str, list[tuple[str, str, bool, str]]] = {}
for _kind, _path in (
    ("function", "/functions"),
    ("seniority", "/seniorities"),
    ("industry", "/industries"),
    ("title", "/titles"),
    ("geo", "/geo"),
):
    _CANDIDATES[_kind] = [
        (api.BASE, _path, True, "ids=List({ids})&locale=de_DE"),
        (api.BASE, _path, True, "ids=List({ids})&locale=" + _LOC_REST),
        (api.BASE, _path, True, "ids=List({ids})"),
        (api.BASE_V2, _path, False, "ids=List({ids})&locale=de_DE"),
        (api.BASE_V2, _path, False, "ids=List({ids})"),
    ]
_CANDIDATES["industry"].insert(
    0, (api.BASE, "/industryTaxonomyVersions/DEFAULT/industries", True, "ids=List({ids})&locale=de_DE")
)
# Einzelabfragen ({id} im Pfad) und Gesamtlisten (ohne {ids}) für Typen,
# bei denen die Sammelabfrage fehlt.
for _kind, _path in (
    ("function", "/functions"),
    ("seniority", "/seniorities"),
    ("industry", "/industries"),
    ("title", "/titles"),
):
    _CANDIDATES[_kind] += [
        (api.BASE_V2, _path + "/{id}", False, "locale=" + _LOC_REST),
        (api.BASE_V2, _path + "/{id}", False, ""),
        (api.BASE, _path + "/{id}", True, ""),
    ]
_CANDIDATES["industry"] += [
    (api.BASE, "/industryTaxonomyVersions/DEFAULT/industries/{id}", True, ""),
]
for _kind, _path in (("function", "/functions"), ("seniority", "/seniorities"), ("industry", "/industries")):
    _CANDIDATES[_kind] += [
        (api.BASE_V2, _path, False, "locale=" + _LOC_REST),
        (api.BASE_V2, _path, False, ""),
    ]
_CANDIDATES["organization"] = [
    (api.BASE, "/organizationsLookup", True, "ids=List({ids})"),
    (api.BASE_V2, "/organizationsLookup", False, "ids=List({ids})"),
]

_PREFERRED = ("de_DE", "de", "en_US", "en")

_cache: dict[str, str] = {}
_working: dict[str, tuple] = {}
_unavailable: dict[str, str] = {}  # Typ -> letzte Fehlermeldung


def urn_type(urn: str) -> str | None:
    m = re.match(r"urn:li:([A-Za-z]+):", str(urn))
    return m.group(1) if m else None


def _staff_count(urn: str) -> str | None:
    m = re.match(r"urn:li:staffCountRange:\((\d+),(\d+)\)", urn)
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return f"{lo}+ Beschäftigte" if hi >= 2**31 - 1 or hi == 0 else f"{lo}–{hi} Beschäftigte"


def _company_size(value: str) -> str | None:
    """LinkedIn liefert Unternehmensgrößen als Aufzählung, z. B. SIZE_51_TO_200."""
    m = re.fullmatch(r"SIZE_(\d+)(?:_TO_(\d+)|_OR_MORE)?", str(value))
    if not m:
        return None
    lo, hi = int(m.group(1)), m.group(2)
    if hi:
        return f"{lo:,}–{int(hi):,} Beschäftigte".replace(",", ".")
    if str(value).endswith("_OR_MORE"):
        return f"{lo:,}+ Beschäftigte".replace(",", ".")
    return "1 Person" if lo == 1 else f"{lo} Beschäftigte"


def _label(entity: dict) -> str | None:
    for key in ("name", "defaultLocalizedName", "localizedName", "title"):
        v = entity.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            if isinstance(v.get("value"), str):
                return v["value"]
            loc = v.get("localized")
            if isinstance(loc, dict) and loc:
                for p in _PREFERRED:
                    if loc.get(p):
                        return loc[p]
                first = next(iter(loc.values()))
                if isinstance(first, str):
                    return first
    return None


def _entity_id(ent: dict) -> str | None:
    for k in ("id", "$URN", "urn", "entityUrn"):
        if ent.get(k) is not None:
            return api.urn_id(ent[k])
    return None


def _collect(kind: str, data: dict, out: dict[str, str]) -> int:
    items: list[tuple[str | None, dict]] = []
    if isinstance(data.get("results"), dict):
        items = list(data["results"].items())
    elif isinstance(data.get("elements"), list):
        items = [(_entity_id(e), e) for e in data["elements"] if isinstance(e, dict)]
    for key, ent in items:
        name = _label(ent) if isinstance(ent, dict) else None
        if key is not None and name:
            out[f"urn:li:{kind}:{key}"] = name
    return len(items)


async def _lookup(kind: str, cand, ids: list[str]) -> dict[str, str]:
    base, path, versioned, qtpl = cand
    out: dict[str, str] = {}
    if "{id}" in path:
        # Einzelabfragen, höchstens 8 gleichzeitig. Erster Fehler bricht ab.
        sem = asyncio.Semaphore(8)

        async def one(i: str):
            async with sem:
                ent = await api.get(path.replace("{id}", i), qtpl, base=base, versioned=versioned)
                name = _label(ent)
                if name:
                    out[f"urn:li:{kind}:{i}"] = name

        await one(ids[0])  # erst einen testen, dann den Rest
        await asyncio.gather(*(one(i) for i in ids[1:]), return_exceptions=True)
        return out
    if "{ids}" in qtpl:
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            data = await api.get(path, qtpl.replace("{ids}", ",".join(chunk)), base=base, versioned=versioned)
            _collect(kind, data, out)
        return out
    # Gesamtliste, seitenweise
    start = 0
    for _ in range(20):
        sep = "&" if qtpl else ""
        data = await api.get(path, f"{qtpl}{sep}start={start}&count=100", base=base, versioned=versioned)
        n = _collect(kind, data, out)
        if n < 100:
            break
        start += n
    wanted = {f"urn:li:{kind}:{i}" for i in ids}
    return {k: v for k, v in out.items() if k in wanted}


def _short(e: Exception) -> str:
    return str(e).replace("\n", " ")[:160]


_debug: list[str] = []


def debug_log() -> list[str]:
    return _debug[-40:]


async def resolve(urns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Gibt (Kennung -> Klartext, Meldungen zu Typen ohne Übersetzung) zurück."""
    found: dict[str, str] = {}
    problems: list[str] = []
    by_type: dict[str, list[str]] = {}
    for u in dict.fromkeys(urns):
        if not u:
            continue
        if u in _cache:
            found[u] = _cache[u]
            continue
        size = _company_size(u)
        if size:
            found[u] = size
            continue
        t = urn_type(u)
        if t == "staffCountRange":
            label = _staff_count(u)
            if label:
                _cache[u] = found[u] = label
            continue
        if t in _CANDIDATES:
            by_type.setdefault(t, []).append(u)

    for kind, kind_urns in by_type.items():
        if kind in _unavailable:
            problems.append(f"{kind} ({_unavailable[kind]})")
            continue
        ids = [api.urn_id(u) for u in kind_urns]
        candidates = [_working[kind]] if kind in _working else _CANDIDATES[kind]
        resolved: dict[str, str] = {}
        errors: list[str] = []
        for cand in candidates:
            try:
                resolved = await _lookup(kind, cand, ids)
            except api.LinkedInError as e:
                if e.status in (401, 429):
                    raise
                errors.append(f"{cand[0].rsplit('/', 1)[-1]}{cand[1]}?{cand[3]} → {_short(e)}")
                _debug.append(errors[-1])
                continue
            if resolved:
                _working[kind] = cand
                _debug.append(f"{kind}: OK über {cand[0].rsplit('/', 1)[-1]}{cand[1]}?{cand[3]}")
                break
            errors.append(f"{cand[0].rsplit('/', 1)[-1]}{cand[1]}?{cand[3]} → leer")
            _debug.append(errors[-1])
        if not resolved:
            msg = errors[-1] if errors else "keine Antwort"
            if kind not in _working:
                _unavailable[kind] = msg
            problems.append(f"{kind} ({msg}; {len(errors)} Wege probiert)")
            continue
        # Reste (z. B. neuere Branchen-Kennungen) über die übrigen Wege versuchen.
        left = [api.urn_id(u) for u in kind_urns if u not in resolved]
        if left:
            for cand in _CANDIDATES[kind]:
                if cand == _working.get(kind):
                    continue
                try:
                    extra = await _lookup(kind, cand, left)
                except api.LinkedInError as e:
                    if e.status in (401, 429):
                        raise
                    continue
                if extra:
                    resolved.update(extra)
                    left = [i for i in left if f"urn:li:{kind}:{i}" not in extra]
                    if not left:
                        break
        _cache.update(resolved)
        found.update(resolved)
    return found, problems
