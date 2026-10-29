#!/usr/bin/env python3
"""IPTV Maghreb V3 : agrégation multi-sources DZ / MA / TN uniquement.

Sans dépendance externe. GitHub Actions exécute build() toutes les 6 h.
Les flux contrôlés par HTTP/HLS ne sont PAS des garanties de lecture vidéo.
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
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
COUNTRIES = {"dz": "Algérie", "ma": "Maroc", "tn": "Tunisie"}
USER_AGENT = "Mozilla/5.0 (compatible; MaghrebPlaylistBot/3.0)"
FETCH_TIMEOUT = 17
CHECK_TIMEOUT = 10
CHECK_WORKERS = 16
GRACE_HOURS = 24
MAX_SOURCE_BYTES = 8_000_000

# Source: (nom, URL, pays explicite ou None). Aucun autre pays n'est exporté.
SOURCES = [
    *(("IPTV-org pays", f"https://iptv-org.github.io/iptv/countries/{c}.m3u", c) for c in COUNTRIES),
    *(("IPTV-org flux bruts", f"https://raw.githubusercontent.com/iptv-org/iptv/master/streams/{c}.m3u", c) for c in COUNTRIES),
    *(("IPTV Nexus", f"https://dearbulut.github.io/iptv/playlists/country/{c}.m3u", c) for c in COUNTRIES),
    ("IPTV-org arabe", "https://iptv-org.github.io/iptv/languages/ara.m3u", None),
    ("IPTV-org région Maghreb", "https://iptv-org.github.io/iptv/regions/maghreb.m3u", None),
    ("Ahmad Arabic Channels", "https://raw.githubusercontent.com/ahmad-aabunaser/iptv/main/arabic_channels.m3u", None),
    ("Sorted IPTV Arabic", "https://raw.githubusercontent.com/THEROCKSSS/sorted-iptv/main/playlists/languages/arabic.m3u", None),
    ("Arab IPTV archive", "https://raw.githubusercontent.com/yazki87/arab-iptv/main/filtered_file_05.01.25.m3u", None),
    ("Free-TV IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", None),
    ("Gist Arabic Free", "https://gist.githubusercontent.com/mdriss074-arch/7d352da113a0022793a9d6ce19ba03fa/raw/arabic_free_channels.m3u", None),
    ("Gist Morocco", "https://gist.githubusercontent.com/ASSAD-11/f9e7293143327658bdbcfb9f091f715c/raw/arabic_french_moroccan_channels.m3u", None),
    ("Doubo", "https://cdn.jsdelivr.net/gh/streamline-iptv/iptv-playlists@dist/index.m3u", None),
]

# Noms non équivoques retrouvés dans les listes arabophones SANS métadonnées pays.
# Ordre : cas particuliers AVANT noms génériques (ex: Laayoune avant Al Aoula).
NAME_RULES = [
    # Algérie
    ("dz", "EPTV 9", r"\beptv\s*(?:9|ix)\b|\bbarlamaniya\b"),
    ("dz", "EPTV 8", r"\beptv\s*8\b|\bedhakira\b"),
    ("dz", "EPTV 7", r"\beptv\s*7\b|\bel maarifa\b"),
    ("dz", "EPTV 6", r"\beptv\s*6\b|\beptv youth\b"),
    ("dz", "EPTV 5", r"\beptv\s*5\b"),
    ("dz", "EPTV 4", r"\beptv\s*4\b"),
    ("dz", "EPTV 3", r"\beptv\s*3\b|\ba3\s*news\b"),
    ("dz", "Canal Algérie", r"\bcanal\s+algerie\b|\bcanal\s+algeria\b"),
    ("dz", "EPTV 1", r"\beptv\s*1\b|\bentv\s*1\b"),
    ("dz", "Echorouk News", r"\bechorouk\s*news\b|\bchourouk\s*news\b|الشروق\s*الاخباري"),
    ("dz", "Echorouk TV", r"\bechorouk\b|\bchourouk\b|الشروق"),
    ("dz", "Ennahar News", r"\bennahar\b|\ben nahar\b|النهار"),
    ("dz", "El Bilad", r"\bel\s*bilad\b|\belbilad\b|البلاد"),
    ("dz", "El Hayat", r"\bel\s*hayat\b|\belhayat\b"),
    ("dz", "Elwatania TV", r"\bel\s*watania\b|\belwatania\b"),
    ("dz", "Bahia TV", r"\bbahia\s*tv\b|\bbahia\s*oran\b"),
    ("dz", "Essalam TV", r"\bessalam\b|\bel\s*salam\b"),
    ("dz", "El Heddaf", r"\bel\s*heddaf\b|\belheddaf\b|\bhtv\s+الهد"),
    ("dz", "Samira TV", r"\bsamira\b|سميرة"),
    ("dz", "Zahra TV", r"\bzahra\b|زهرة"),
    ("dz", "Amou Yazid", r"\bamou\s*yazid\b"),
    ("dz", "4Kids DZ", r"\b4\s*kids\b"),
    ("dz", "AL24 News", r"\bal\s*24\s*(?:news)?\b"),
    # Maroc
    ("ma", "Al Aoula Laayoune", r"\blaayoune\b|\blaayoun\b|العيون"),
    ("ma", "Al Aoula", r"\bal\s*aoula\b|\bal\s*oula\b|\bal\s*awla\b"),
    ("ma", "2M Monde", r"(?<!\w)2m(?:\s*monde|\s*international)?\b"),
    ("ma", "Arryadia", r"\barr?yadia\b|\barriadia\b"),
    ("ma", "Arrabia / Athaqafia", r"\barrabia\b|\bathaqafia\b"),
    ("ma", "Al Maghribia", r"\bal\s*maghribia\b|\bmaghribia\b"),
    ("ma", "Assadissa", r"\bassadissa\b|\bsadissa\b"),
    ("ma", "Tamazight TV", r"\btamazigh\w*\b|\btamzight\b"),
    ("ma", "Télé Maroc", r"\btele\s*maroc\b|\btele\s*morocco\b"),
    ("ma", "M24 Maroc", r"\bm24\b"),
    ("ma", "Medi 1 TV", r"\bmedi\s*1\b|\bmedi1\b"),
    ("ma", "Chada TV", r"\bchada\s*tv\b|\bchada\b|شدى"),
    # Tunisie
    ("tn", "Wataniya 1", r"\bwatania\s*1\b|\bwataniya\s*1\b|الوطنية\s*1"),
    ("tn", "Wataniya 2", r"\bwatania\s*2\b|\bwataniya\s*2\b|الوطنية\s*2"),
    ("tn", "Sahel TV", r"\bsahel\s*tv\b|الساحل\s*التونسي"),
    ("tn", "Mosaïque TV", r"\bmosaique\b|\bmosa[iï]que\b|موزاييك"),
    ("tn", "Jawhara TV", r"\bjawhara\b|جوهرة"),
    ("tn", "Elyssa TV", r"\belyssa\b|\belissa\b"),
    ("tn", "Hannibal TV", r"\bhannibal\b"),
    ("tn", "Attessia TV", r"\battessia\b|\bettassia\b"),
    ("tn", "Tunisna TV", r"\btunisna\b"),
    ("tn", "Carthage+", r"\bcarthage\s*\+?\b"),
    ("tn", "Essaida TV", r"\bessaida\b|\bsa[iï]da\s*tv\b"),
    ("tn", "Tuniscope", r"\btuniscope\b"),
]

COUNTRY_TAGS = {
    "dz": {"dz", "dza", "algerie", "algeria", "الجزائر"},
    "ma": {"ma", "mar", "maroc", "morocco", "المغرب"},
    "tn": {"tn", "tun", "tunisie", "tunisia", "تونس"},
}
COUNTRY_ORDER = {"dz": 0, "ma": 1, "tn": 2}


@dataclass(frozen=True)
class Entry:
    country: str
    name: str
    url: str
    logo: str = ""
    tvg_id: str = ""
    extras: tuple[str, ...] = ()
    source: str = ""
    canonical: str = ""


def normalize(text: str) -> str:
    """Accent/diacritic-insensitive, including Arabic vowel marks."""
    s = unicodedata.normalize("NFKD", text.casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.replace("ـ", "").replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").strip()


def country_from_name(name: str) -> tuple[str | None, str | None]:
    text = normalize(name)
    for country, canonical, pattern in NAME_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return country, canonical
    return None, None


def attribute(line: str, key: str) -> str:
    match = re.search(r'(?<![\w-])' + re.escape(key) + r'="([^"]*)"', line, flags=re.I)
    return match.group(1).strip() if match else ""


def country_from_tag(value: str) -> str | None:
    for part in re.split(r"[;,|/]", value):
        part = normalize(part).strip()
        for country, names in COUNTRY_TAGS.items():
            if part in names:
                return country
    return None


def classify(line: str, name: str, hint: str | None, source: str) -> tuple[str | None, str | None]:
    tvg_id = attribute(line, "tvg-id").split("@", 1)[0]
    code = tvg_id.rsplit(".", 1)[-1].lower() if "." in tvg_id else ""
    country_tag = attribute(line, "tvg-country")
    group = attribute(line, "group-title")
    from_name, canonical = country_from_name(name)
    # Foreign country code is an explicit veto, even if included in an .dz source.
    if re.fullmatch(r"[a-z]{2}", code):
        if code not in COUNTRIES or (hint is not None and code != hint):
            return None, None
        if country_tag and country_from_tag(country_tag) not in (None, code):
            return None, None
        return code, canonical if from_name == code else None
    if country_tag:
        by_tag = country_from_tag(country_tag)
        if not by_tag or (hint and by_tag != hint):
            return None, None
        return by_tag, canonical if from_name == by_tag else None
    by_group = country_from_tag(group)
    if by_group:
        if hint and by_group != hint:
            return None, None
        return by_group, canonical if from_name == by_group else None
    if hint:
        # Country playlists are pre-filtered. Generic names can be retained.
        if from_name and from_name != hint:
            return None, None
        return hint, canonical
    if from_name:
        return from_name, canonical
    # Ahmad's Algerian ONE TV has unambiguous Algerian logo path.
    if (source == "Ahmad Arabic Channels" and normalize(name) == "one tv"
            and "algeriapicons" in attribute(line, "tvg-logo").lower()):
        return "dz", "ONE TV Algérie"
    return None, None


def allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return False
        hostname = p.hostname.lower()
        if hostname == "localhost" or hostname.endswith((".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return False
        except ValueError:
            pass
        if p.path.lower().endswith(".m3u"):
            return False   # Never feed a nested multinational playlist to a player.
        return True
    except ValueError:
        return False


def split_inline_options(raw: str) -> tuple[str, tuple[str, ...]]:
    """M3U URL|Referer=... syntax -> VLC option (keeps URL clean)."""
    parts = raw.split("|")
    if len(parts) == 1:
        return raw, ()
    extra = []
    for item in parts[1:]:
        k, _, value = item.partition("=")
        if k.lower().strip() in ("referer", "referrer", "http-referrer") and value:
            extra.append("#EXTVLCOPT:http-referrer=" + value.strip())
        elif k.lower().strip() in ("user-agent", "http-user-agent") and value:
            extra.append("#EXTVLCOPT:http-user-agent=" + value.strip())
    return parts[0].strip(), tuple(extra)


def parse_m3u(text: str, hint: str | None, source: str) -> list[Entry]:
    entries: list[Entry] = []
    meta: str | None = None
    opts: list[str] = []
    # Some community lists do not include #EXTM3U; #EXTINF is enough.
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF:"):
            meta, opts = line, []
            continue
        if meta and line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTHTTP:")):
            opts.append(line)
            continue
        if meta and line and not line.startswith("#"):
            url, inline_opts = split_inline_options(line)
            name = meta.split(",", 1)[1].strip() if "," in meta else ""
            country, canonical = classify(meta, name, hint, source)
            if country and name and allowed_url(url):
                entries.append(Entry(country=country, name=name, url=url,
                                     logo=attribute(meta, "tvg-logo"),
                                     tvg_id=attribute(meta, "tvg-id"),
                                     extras=tuple(opts) + inline_opts,
                                     source=source, canonical=canonical or ""))
            meta, opts = None, []
    return entries


def fetch_source(record: tuple[str, str, str | None]) -> tuple[list[Entry], dict]:
    source, address, hint = record
    try:
        req = Request(address, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        with urlopen(req, timeout=FETCH_TIMEOUT) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("source trop volumineuse")
        text = data.decode("utf-8-sig", errors="replace")
        if "#EXTINF:" not in text.upper():
            raise ValueError("pas de lignes M3U détectées")
        rows = parse_m3u(text, hint, source)
        return rows, {"source": source, "pays": hint or "mixte", "retenus": len(rows), "statut": "OK"}
    except (OSError, ValueError, URLError) as exc:
        return [], {"source": source, "pays": hint or "mixte", "retenus": 0,
                    "statut": f"Échec: {str(exc)[:150]}"}


def headers_for(entry: Entry) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Range": "bytes=0-8191"}
    for extra in entry.extras:
        if extra.startswith("#EXTVLCOPT:"):
            key, _, val = extra[len("#EXTVLCOPT:"):].partition("=")
            if key.lower() == "http-referrer":
                headers["Referer"] = val
            elif key.lower() == "http-user-agent":
                headers["User-Agent"] = val
    return headers


def check_stream(entry: Entry) -> tuple[bool, str]:
    try:
        with urlopen(Request(entry.url, headers=headers_for(entry)), timeout=CHECK_TIMEOUT) as r:
            status = r.status
            content_type = (r.headers.get("Content-Type") or "").lower()
            sample = r.read(8192)
            final_url = r.geturl()
        if status not in (200, 206) or not sample:
            return False, "HTTP vide ou mauvais code"
        stripped = sample.lstrip(b"\xef\xbb\xbf \n\r\t")
        low = stripped.lower()
        if low.startswith((b"<html", b"<!doctype", b"<?xml", b"{", b"[")):
            return False, "Réponse HTML/XML/JSON"
        is_hls = urlparse(entry.url).path.lower().endswith(".m3u8") or urlparse(final_url).path.lower().endswith(".m3u8") or "mpegurl" in content_type
        if is_hls:
            if not low.startswith(b"#extm3u") or b"#ext-x-" not in low:
                return False, "Manifeste HLS absent/invalide"
            return True, "Manifeste HLS HTTP lisible (segments non testés)"
        if "text/html" in content_type:
            return False, "Page HTML, pas un flux"
        return True, "HTTP binaire accessible (lecture non garantie)"
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (URLError, OSError, ValueError, TimeoutError) as exc:
        return False, str(exc)[:160]


def canonical_key(entry: Entry) -> tuple[str, str]:
    if entry.canonical:
        return entry.country, normalize(entry.canonical)
    # Inconsistent id quality: use tvg-id only if it belongs to target country.
    if entry.tvg_id:
        candidate = entry.tvg_id.split("@", 1)[0].lower()
        if candidate.endswith("." + entry.country):
            return entry.country, candidate
    name = normalize(entry.name)
    name = re.sub(r"\([^)]*(?:\d{3,4}p|hd|sd|fhd|inter)\)", "", name, flags=re.I)
    name = re.sub(r"\[[^]]*\]", "", name)
    return entry.country, re.sub(r"\s+", " ", name).strip()


def dedupe_urls(entries: list[Entry]) -> list[Entry]:
    result = {}
    for entry in entries:
        # Prefer a copy containing referer/player headers if URLs are identical.
        if entry.url not in result or len(entry.extras) > len(result[entry.url].extras):
            result[entry.url] = entry
    return list(result.values())


def render(entries: list[Entry], subtitle: str, updated: dt.datetime) -> str:
    lines = ["#EXTM3U", f"# Maghreb V3: {subtitle}",
             f"# Dernière génération UTC : {updated.isoformat(timespec='seconds')}",
             "# Algérie / Maroc / Tunisie uniquement. Test HTTP ≠ lecture vidéo garantie."]
    for entry in sorted(entries, key=lambda e: (COUNTRY_ORDER[e.country], normalize(e.canonical or e.name), e.url)):
        # Protect playlist structure, sanitize metadata before rendering.
        name = entry.name.replace("\n", " ").replace("\r", " ").replace('"', "'")
        logo = entry.logo.replace('"', "").replace("\r", "").replace("\n", "")
        tvg_id = entry.tvg_id.replace('"', "").replace("\r", "").replace("\n", "")
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{COUNTRIES[entry.country]}",{name}')
        lines.extend(entry.extras)
        lines.append(entry.url)
    return "\n".join(lines) + "\n"


def atomic_write(name: str, contents: str) -> None:
    file = ROOT / name
    fd, path = tempfile.mkstemp(dir=ROOT, prefix=".tmp_maghreb_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(contents)
        os.replace(path, file)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def read_previous_ok() -> dict[str, str]:
    try:
        val = json.loads((ROOT / "etat.json").read_text(encoding="utf-8")).get("last_ok", {})
        return val if isinstance(val, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def build() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    source_results: list[tuple[list[Entry], dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        source_results = list(pool.map(fetch_source, SOURCES))
    rows = [r for entries, _ in source_results for r in entries]
    reports = [report for _, report in source_results]
    for report in reports:
        print(f"{report['source']} ({report['pays']}) : {report['retenus']} — {report['statut']}")
    unique = dedupe_urls(rows)
    found = {e.country for e in unique}
    if found != set(COUNTRIES):
        raise RuntimeError(f"Pays absent(s) : {set(COUNTRIES) - found}. Aucun fichier remplacé.")

    checks: dict[str, tuple[bool, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        futures = {pool.submit(check_stream, entry): entry.url for entry in unique}
        for future in concurrent.futures.as_completed(futures):
            checks[futures[future]] = future.result()

    previous_ok = read_previous_ok()
    current_ok, grace_ok, new_ok = [], [], {}
    for e in unique:
        ok, _ = checks[e.url]
        if ok:
            new_ok[e.url] = now.isoformat(timespec="seconds")
            current_ok.append(e)
        elif e.url in previous_ok:
            try:
                ts = dt.datetime.fromisoformat(previous_ok[e.url])
                age = now - ts
                if dt.timedelta(0) <= age <= dt.timedelta(hours=GRACE_HOURS):
                    new_ok[e.url] = previous_ok[e.url]
                    grace_ok.append(e)
            except (TypeError, ValueError):
                pass

    live = current_ok + grace_ok
    # One playable URL per channel; alternative URLs in maghreb-secours.m3u.
    selected, backup = [], []
    seen_keys = set()
    for e in live:
        key = canonical_key(e)
        if key in seen_keys:
            backup.append(e)
        else:
            selected.append(e)
            seen_keys.add(key)

    # Do not erase healthy main playlist when every source fails to play.
    if selected:
        atomic_write("maghreb.m3u", render(selected, "1 flux accessible par chaîne", now))
    else:
        print("AVERTISSEMENT : aucune URL accessible. Playlist principale précédente conservée.")
    # Complete playlist always retains every candidate, including failed checks.
    atomic_write("maghreb-complet.m3u", render(unique, "tous les liens découverts (NON vérifiés inclus)", now))
    atomic_write("maghreb-secours.m3u", render(backup, "alternatives HTTP accessibles", now))

    report = {
        "version": "3.0",
        "mise_a_jour_utc": now.isoformat(timespec="seconds"),
        "pays": list(COUNTRIES.values()),
        "sources_consultees": len(SOURCES),
        "sources_reussies": sum(r["statut"] == "OK" for r in reports),
        "flux_source_uniques": len(unique),
        "flux_http_accessibles_ce_controle": len(current_ok),
        "flux_conserves_grace_24h": len(grace_ok),
        "chaines_principales": len(selected),
        "liens_alternatifs_valides": len(backup),
        "par_pays_total": dict(Counter(COUNTRIES[e.country] for e in unique)),
        "par_pays_principal": dict(Counter(COUNTRIES[e.country] for e in selected)),
        "sources": reports,
        "avertissement": "Contrôle sur GitHub uniquement. Ni lecture vidéo ni droits de rediffusion garantis.",
        "last_ok": new_ok,
        "echecs": [{"chaine": e.name, "pays": COUNTRIES[e.country], "source": e.source,
                    "url": e.url, "raison": checks[e.url][1]}
                   for e in unique if not checks[e.url][0]],
    }
    atomic_write("etat.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Terminé : {len(unique)} liens candidats, {len(current_ok)} accessibles, "
          f"{len(selected)} chaînes principales, {len(backup)} secours")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ÉCHEC (fichiers précédents préservés) : {exc}", file=sys.stderr)
        sys.exit(1)
