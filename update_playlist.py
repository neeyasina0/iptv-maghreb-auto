#!/usr/bin/env python3
"""Reconstruit les playlists Algérie/Maroc/Tunisie depuis IPTV-org.

Aucune dépendance externe. Vérification HTTP/HLS indicative (réseau GitHub Actions).
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
COUNTRIES = (
    ("dz", "Algérie"),
    ("ma", "Maroc"),
    ("tn", "Tunisie"),
)
SOURCE = "https://iptv-org.github.io/iptv/countries/{code}.m3u"
USER_AGENT = "Mozilla/5.0 (compatible; Playlist-Maghreb-Auto/1.0)"
TIMEOUT = 9
WORKERS = 12
GRACE_HOURS = 24


@dataclass(frozen=True)
class Entry:
    country: str
    extinf: str
    extras: tuple[str, ...]
    url: str


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_string(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def atomic_write(path: Path, data: str) -> None:
    fd, tmpname = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(data)
        os.replace(tmpname, path)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def download_text(url: str) -> str:
    last_exception: Exception | None = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urlopen(req, timeout=18) as response:
                data = response.read(4_000_000)
            text = data.decode("utf-8-sig", errors="replace")
            if not text.lstrip().startswith("#EXTM3U"):
                raise ValueError("la source ne contient pas de playlist M3U")
            return text
        except (URLError, HTTPError, OSError, ValueError) as exc:
            last_exception = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Téléchargement impossible : {url} ({last_exception})")


def parse_m3u(text: str, country: str) -> list[Entry]:
    entries: list[Entry] = []
    extinf: str | None = None
    extras: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            extinf = line
            extras = []
            continue
        if extinf is not None and line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTGRP:", "#EXTHTTP:")):
            extras.append(line)
            continue
        if extinf is not None and urlparse(line).scheme.lower() in ("http", "https"):
            # Reconstitue une catégorie stable, même si la source ne la définit pas.
            if re.search(r'group-title="[^"]*"', extinf, flags=re.I):
                meta = re.sub(r'group-title="[^"]*"', f'group-title="{country}"', extinf, flags=re.I)
            else:
                meta = extinf.replace(",", f' group-title="{country}",', 1)
            entries.append(Entry(country, meta, tuple(extras), line))
            extinf = None
            extras = []
            continue
        if line and not line.startswith("#"):
            extinf = None
            extras = []
    return entries


def extra_headers(entry: Entry) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Range": "bytes=0-8191",
    }
    for extra in entry.extras:
        if extra.lower().startswith("#extvlcopt:"):
            key, _, value = extra[len("#EXTVLCOPT:"):].partition("=")
            if key.lower() == "http-referrer":
                headers["Referer"] = value
            elif key.lower() == "http-user-agent":
                headers["User-Agent"] = value
    return headers


def is_reachable(entry: Entry) -> tuple[bool, str]:
    headers = extra_headers(entry)
    try:
        # GET plutôt que HEAD : de nombreux serveurs IPTV refusent HEAD.
        # Une connexion HTTP valide ne prouve pas que le flux est visionnable.
        request = Request(entry.url, headers=headers)
        with urlopen(request, timeout=TIMEOUT) as resp:
            status = resp.status
            final_url = resp.geturl()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            sample = resp.read(8192)
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
        f"# Playlist Maghreb automatique | {label}",
        f"# Mise à jour UTC : {utc_string(updated)}",
        "# Algérie, Maroc, Tunisie uniquement. Source : https://iptv-org.github.io/iptv/",
        "# Contrôles réseau indicatifs ; un flux peut être géobloqué ou interrompu.",
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
        return data.get("last_ok", {}) if isinstance(data.get("last_ok", {}), dict) else {}
    except (ValueError, OSError):
        return {}


def build() -> None:
    updated = now_utc()
    collected: list[Entry] = []
    # Exige les trois sources : jamais de suppression massive après une panne réseau.
    for code, country in COUNTRIES:
        text = download_text(SOURCE.format(code=code))
        items = parse_m3u(text, country)
        if not items:
            raise RuntimeError(f"La liste de {country} est vide/invalide : rien n'est écrasé")
        print(f"{country} : {len(items)} entrées source")
        collected.extend(items)

    # Supprime uniquement les doublons d'URL (les qualités alternatives restent).
    unique: list[Entry] = []
    urls: set[str] = set()
    for entry in collected:
        if entry.url not in urls:
            unique.append(entry)
            urls.add(entry.url)

    checks: dict[str, tuple[bool, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(is_reachable, entry): entry.url for entry in unique}
        for future in concurrent.futures.as_completed(futures):
            checks[futures[future]] = future.result()

    previous = load_state()
    last_ok: dict[str, str] = {}
    checked: list[Entry] = []
    newly_live = 0
    on_grace = 0
    for entry in unique:
        ok, _ = checks[entry.url]
        if ok:
            newly_live += 1
            last_ok[entry.url] = utc_string(updated)
            checked.append(entry)
        elif entry.url in previous:
            try:
                age = updated - dt.datetime.fromisoformat(previous[entry.url])
                if dt.timedelta(0) <= age <= dt.timedelta(hours=GRACE_HOURS):
                    last_ok[entry.url] = previous[entry.url]
                    checked.append(entry)
                    on_grace += 1
            except ValueError:
                pass

    # Publie systématiquement la liste complète tant que les 3 sources marchent.
    atomic_write(ROOT / "maghreb-complet.m3u", render_m3u(unique, "toutes les entrées source", updated))
    # Ne remplace pas la version filtrée par une playlist vide.
    if checked:
        atomic_write(ROOT / "maghreb.m3u", render_m3u(checked, "accessible depuis le robot ou validé dans les dernières 24 h", updated))
    elif not (ROOT / "maghreb.m3u").exists():
        print("ATTENTION : aucun flux vérifié ; conserver uniquement la playlist complète")
    else:
        print("ATTENTION : zéro flux vérifié ; conservation du dernier fichier filtré")

    report = {
        "mise_a_jour_utc": utc_string(updated),
        "pays": [name for _, name in COUNTRIES],
        "flux_source_uniques": len(unique),
        "flux_http_accessibles_ce_controle": newly_live,
        "flux_conserves_grace_24h": on_grace,
        "flux_publies_filtres": len(checked) if checked else "ancienne version conservée",
        "avertissement": "Contrôle HTTP sur GitHub ; la lecture en France et sur votre appareil n'est pas garantie.",
        "last_ok": last_ok,
        "echecs": [
            {"chaine": entry.extinf.rsplit(",", 1)[-1], "url": entry.url, "raison": checks[entry.url][1]}
            for entry in unique if not checks[entry.url][0]
        ],
    }
    atomic_write(ROOT / "etat.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Terminé : {len(unique)} liens source, {newly_live} répondent, {on_grace} en grâce 24 h")


if __name__ == "__main__":
    try:
        build()
    except Exception as err:
        print(f"ERREUR (anciens fichiers préservés si échec du téléchargement) : {err}", file=sys.stderr)
        sys.exit(1)
