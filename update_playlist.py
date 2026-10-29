#!/usr/bin/env python3
"""Playlist Maghreb V2 : Algérie, Maroc, Tunisie exclusivement.

Collecte depuis IPTV-org, son inventaire brut, IPTV Nexus (dearbulut),
Doubo et deux listes publiques communautaires. Les sources additionnelles
peuvent ne rien apporter / tomber en panne ; l'échec est signalé.
Aucune garantie que des liens HTTP valides soient lisibles par un player.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
COUNTRIES = {"dz": "Algérie", "ma": "Maroc", "tn": "Tunisie"}
USER_AGENT = "Mozilla/5.0 (compatible; IPTV-Maghreb-Auto/2.0)"
TIMEOUT = 9
WORKERS = 12
GRACE_HOURS = 24

# Pays explicite pour les playlists par pays ; None pour les sources globales.
SOURCES = [
    *( (f"https://iptv-org.github.io/iptv/countries/{c}.m3u", c, "IPTV-org public", True) for c in COUNTRIES ),
    *( (f"https://raw.githubusercontent.com/iptv-org/iptv/master/streams/{c}.m3u", c, "IPTV-org brut (alternatives)", False) for c in COUNTRIES ),
    *( (f"https://dearbulut.github.io/iptv/playlists/country/{c}.m3u", c, "IPTV Nexus", False) for c in COUNTRIES ),
    ("https://gist.githubusercontent.com/mdriss074-arch/7d352da113a0022793a9d6ce19ba03fa/raw/arabic_free_channels.m3u", None, "Gist Arabic Free", False),
    ("https://gist.githubusercontent.com/ASSAD-11/f9e7293143327658bdbcfb9f091f715c/raw/arabic_french_moroccan_channels.m3u", None, "Gist Morocco", False),
    ("https://cdn.jsdelivr.net/gh/streamline-iptv/iptv-playlists@dist/index.m3u", None, "Doubo", False),
]

ALIASES = {
    "dz": ("dz", "dza", "algeria", "algerie", "algerie dz"),
    "ma": ("ma", "mar", "morocco", "maroc"),
    "tn": ("tn", "tun", "tunisia", "tunisie"),
}


@dataclass(frozen=True)
class Entry:
    country: str
    extinf: str
    extras: tuple[str, ...]
    url: str
    source: str


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def utc_string(d):
    return d.isoformat(timespec="seconds")


def atomic_write(path: Path, data: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def download_text(url: str) -> str:
    error = None
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urlopen(req, timeout=18) as response:
                data = response.read(5_000_000)
            text = data.decode("utf-8-sig", errors="replace")
            if not text.lstrip().startswith("#EXTM3U"):
                raise ValueError("source sans en-tête M3U")
            return text
        except (URLError, HTTPError, OSError, ValueError) as exc:
            error = exc
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(str(error)[:160])


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in value if not unicodedata.combining(ch)).strip()


def attr(extinf: str, key: str) -> str:
    result = re.search(r'\b' + re.escape(key) + r'="([^"]*)"', extinf, re.I)
    return result.group(1) if result else ""


def choose_country(extinf: str, country_hint: str | None) -> str | None:
    # Important : un fichier 'dz.m3u' peut contenir des chaînes égyptiennes !
    # tvg-id suffix .eg doit donc être refusé même dans une source algérienne.
    tvg_id = attr(extinf, "tvg-id").split("@", 1)[0]
    tvg_country = attr(extinf, "tvg-country").split(";", 1)[0].split(",", 1)[0]
    group = attr(extinf, "group-title")
    code_from_id = tvg_id.rsplit(".", 1)[-1].lower() if "." in tvg_id else ""
    if code_from_id and re.fullmatch(r"[a-z]{2}", code_from_id):
        if code_from_id not in COUNTRIES:
            return None
        return code_from_id if country_hint is None or country_hint == code_from_id else None
    if tvg_country:
        country_key = normalize(tvg_country)
        for code, values in ALIASES.items():
            if country_key in values:
                return code if country_hint is None or code == country_hint else None
        return None  # Marquage pays inconnu : ne pas prendre de risque
    group_key = normalize(group)
    if group_key:
        for code, values in ALIASES.items():
            if group_key in values:
                return code if country_hint is None or code == country_hint else None
    # Une playlist par pays a déjà fait le filtrage ; on accepte sans metadata.
    return country_hint


def url_allowed(url: str) -> bool:
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    if p.hostname.lower() in ("localhost", "localhost.localdomain") or p.hostname.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(p.hostname)
        if not ip.is_global:
            return False
    except ValueError:
        pass
    # Lien vers une AUTRE playlist => risque d'inclure des pays étrangers dans le player.
    if p.path.lower().endswith(".m3u"):
        return False
    return True


def parse_m3u(text: str, country_hint: str | None, source: str) -> list[Entry]:
    entries = []
    extinf = None
    extras = []
    for raw in text.replace("\r\n", "\n").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            extinf, extras = line, []
            continue
        if extinf and line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTGRP:", "#EXTHTTP:")):
            extras.append(line)
            continue
        if extinf and line and not line.startswith("#"):
            code = choose_country(extinf, country_hint)
            if code and url_allowed(line):
                # Remplacer la catégorie pour garantir EXACTEMENT 3 groupes.
                country = COUNTRIES[code]
                if re.search(r'\bgroup-title="[^"]*"', extinf, re.I):
                    meta = re.sub(r'\bgroup-title="[^"]*"', f'group-title="{country}"', extinf, flags=re.I)
                else:
                    meta = extinf.replace(",", f' group-title="{country}",', 1)
                entries.append(Entry(country, meta, tuple(extras), line, source))
            extinf, extras = None, []
    return entries


def extra_headers(entry: Entry) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Range": "bytes=0-8191"}
    for extra in entry.extras:
        if extra.lower().startswith("#extvlcopt:"):
            key, _, value = extra[len("#EXTVLCOPT:"):].partition("=")
            if key.lower() == "http-referrer":
                headers["Referer"] = value
            elif key.lower() == "http-user-agent":
                headers["User-Agent"] = value
    return headers


def is_reachable(entry: Entry) -> tuple[bool, str]:
    try:
        with urlopen(Request(entry.url, headers=extra_headers(entry)), timeout=TIMEOUT) as response:
            status, final_url = response.status, response.geturl()
            content_type = (response.headers.get("Content-Type") or "").lower()
            sample = response.read(8192)
        if status not in (200, 206) or not sample:
            return False, "Réponse vide ou code HTTP inattendu"
        head = sample.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if head.startswith((b"<!doctype html", b"<html", b"<?xml", b"{")):
            return False, "Réponse HTML/XML/JSON, pas une vidéo"
        is_hls = urlparse(final_url).path.lower().endswith(".m3u8") or urlparse(entry.url).path.lower().endswith(".m3u8")
        if is_hls or "mpegurl" in content_type:
            if not head.startswith(b"#extm3u"):
                return False, "Manifeste HLS invalide"
        elif content_type.startswith("text/") and not head.startswith(b"#extm3u"):
            return False, "Texte au lieu d'un flux"
        return True, "HTTP accessible (lecture non garantie)"
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (URLError, OSError, ValueError, TimeoutError) as exc:
        return False, str(exc)[:130]


def render_m3u(entries: list[Entry], label: str, updated: dt.datetime) -> str:
    lines = [
        "#EXTM3U",
        f"# IPTV Maghreb V2 | {label}",
        f"# MAJ UTC: {utc_string(updated)} | Algérie, Maroc, Tunisie seulement",
        "# Vérification HTTP indicative (GitHub) - lecture non garantie",
    ]
    for entry in entries:
        lines.extend((entry.extinf, *entry.extras, entry.url, ""))
    return "\n".join(lines).rstrip() + "\n"


def load_state() -> dict[str, str]:
    path = ROOT / "etat.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("last_ok", {})
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def build() -> None:
    updated = now_utc()
    all_entries: list[Entry] = []
    source_reports = []
    primary_ok = set()
    for url, hint, source, required in SOURCES:
        try:
            text = download_text(url)
            entries = parse_m3u(text, hint, source)
            if required and not entries:
                raise ValueError("la playlist du pays est vide après filtrage")
            all_entries.extend(entries)
            if required:
                primary_ok.add(hint)
            print(f"{source} {hint or '3 pays'} : {len(entries)} flux")
            source_reports.append({"source": source, "pays": hint or "3 pays", "lignes": len(entries), "statut": "OK"})
        except Exception as exc:
            print(f"ATTENTION source {source} {hint}: {exc}")
            source_reports.append({"source": source, "pays": hint or "3 pays", "lignes": 0, "statut": str(exc)[:150]})

    # Protéger l'existant : chaque pays doit contenir au moins une source,
    # primaire ou secondaire. Les sources en échec n'effacent pas les anciens fichiers.
    found = {k for k, country in COUNTRIES.items() if any(e.country == country for e in all_entries)}
    if found != set(COUNTRIES):
        raise RuntimeError(f"Source manquante pour {set(COUNTRIES) - found}; playlists précédentes conservées")

    unique = []
    seen = set()
    for entry in all_entries:
        if entry.url not in seen:
            unique.append(entry)
            seen.add(entry.url)

    checks = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(is_reachable, entry): entry.url for entry in unique}
        for future in concurrent.futures.as_completed(jobs):
            checks[jobs[future]] = future.result()

    previous = load_state()
    last_ok = {}
    checked = []
    newly_live = 0
    grace = 0
    for entry in unique:
        ok, _reason = checks[entry.url]
        if ok:
            newly_live += 1
            last_ok[entry.url] = utc_string(updated)
            checked.append(entry)
        elif entry.url in previous:
            try:
                previous_timestamp = dt.datetime.fromisoformat(previous[entry.url])
                age = updated - previous_timestamp
                if dt.timedelta(0) <= age <= dt.timedelta(hours=GRACE_HOURS):
                    last_ok[entry.url] = previous[entry.url]
                    checked.append(entry)
                    grace += 1
            except (TypeError, ValueError):
                pass

    atomic_write(ROOT / "maghreb-complet.m3u", render_m3u(unique, "TOUS LES FLUX", updated))
    if checked:
        atomic_write(ROOT / "maghreb.m3u", render_m3u(checked, "FLUX TESTÉS HTTP + GRÂCE 24H", updated))
    else:
        print("AVERTISSEMENT : 0 flux accessible ; playlist filtrée précédente conservée")

    counts_all = dict(Counter(entry.country for entry in unique))
    counts_checked = dict(Counter(entry.country for entry in checked))
    report = {
        "mise_a_jour_utc": utc_string(updated),
        "pays": list(COUNTRIES.values()),
        "flux_source_uniques": len(unique),
        "flux_http_accessibles_ce_controle": newly_live,
        "flux_conserves_grace_24h": grace,
        "flux_publies_filtres": len(checked) if checked else "ancienne version conservée",
        "par_pays_total": counts_all,
        "par_pays_filtres": counts_checked,
        "sources": source_reports,
        "avertissement": "Contrôle HTTP sur GitHub, pas de garantie de visionnage depuis la France.",
        "last_ok": last_ok,
        "echecs": [
            {"chaine": e.extinf.rsplit(",", 1)[-1], "source": e.source, "url": e.url, "raison": checks[e.url][1]}
            for e in unique if not checks[e.url][0]
        ],
    }
    atomic_write(ROOT / "etat.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"TERMINÉ : {len(unique)} flux, {newly_live} accessibles, {grace} conservés temporairement")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
