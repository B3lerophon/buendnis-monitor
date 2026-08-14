#!/usr/bin/env python3
"""
Bündnisverpflichtungs-Monitor
==============================

Ruft konfigurierte Quellen ab (Bundestag DIP-API, RSS-Feeds, einfache
HTML-Seiten), prüft neue Einträge gegen die in config/zusagen.yaml
definierten Bündnis-Zusagen und schreibt eine nachvollziehbare
Ampel-Bewertung nach docs/data/status.json.

Design-Prinzip: keine Blackbox. Jede Ampel trägt eine Begründung,
eine Quelle (Link) und ein Datum. Nichts wird ohne sichtbaren Beleg bewertet.

Kosten: 0€. Läuft rein regelbasiert (Keyword-Matching), keine externen
Bezahl-APIs nötig. Optional lässt sich später eine LLM-Bewertung
ergänzen (siehe Kommentar bei `assess_item`).
"""

from __future__ import annotations

import json
import os
import re
import sys
import gzip
import hashlib
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "docs" / "data"
STATE_PATH = DATA_DIR / "state.json"          # gemerkte html_scan-Links (persistiert im Repo)
OUTPUT_PATH = DATA_DIR / "status.json"          # Dashboard liest diese Datei
LOG_PATH = DATA_DIR / "log.jsonl"               # Rohliste aller je bewerteten Fundstellen

MIN_DATUM = date(2025, 1, 1)  # harte Grenze: nichts Älteres wird als "aktuell" gewertet
LOOKBACK_TAGE_FUER_AMPEL = 120  # wie weit zurück für die aktuelle Ampel geschaut wird

DIP_API_BASE = "https://search.dip.bundestag.de/api/v1"
DIP_API_KEY_FALLBACK = "OSOegLs.PR2lwJ1dwCeje9vTj7FPOt3hvpYKtwKkhw"  # öffentlicher Test-Key lt. DIP-Doku

HEADERS = {
    # Manche Regierungs-/Institutionsseiten blockieren offensichtliche Bot-User-Agents.
    # Wir identifizieren uns trotzdem ehrlich (kein Fake-Browser-UA), aber mit realistischeren
    # Zusatz-Headern, die viele einfache Bot-Filter schon durchlässt.
    "User-Agent": "Buendnisverpflichtungs-Monitor/1.0 (+NGO-Themen-Tracking, nicht-kommerziell)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_links": {}}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_date_safe(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return dateparser.parse(value).date()
    except Exception:
        return None


def item_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Abruf: DIP-API (Bundestag)
# ---------------------------------------------------------------------------

def fetch_dip_api(source: dict) -> list[dict]:
    # os.environ.get(..., default) greift NUR, wenn die Variable komplett fehlt.
    # GitHub Actions setzt DIP_API_KEY aber immer (als leeren String, wenn kein
    # Secret hinterlegt ist) - daher hier explizit auf leeren String prüfen.
    api_key = os.environ.get("DIP_API_KEY") or DIP_API_KEY_FALLBACK
    resource = source["resource"]
    items = []
    for begriff in source.get("suchbegriffe", [""]):
        params = {
            "apikey": api_key,
            "f.datum.start": MIN_DATUM.isoformat(),
        }
        if begriff:
            params["f.titel"] = begriff
        try:
            resp = session.get(f"{DIP_API_BASE}/{resource}", params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            print(f"[WARN] DIP-API Fehler ({resource}, '{begriff}'): {exc}", file=sys.stderr)
            continue

        for doc in payload.get("documents", []):
            titel = doc.get("titel") or doc.get("dokumentnummer") or "(ohne Titel)"
            datum = parse_date_safe(doc.get("datum"))
            fundstelle = doc.get("fundstelle", {}) or {}
            url = fundstelle.get("pdf_url") or fundstelle.get("xml_url") or ""
            text_parts = [titel]
            if doc.get("text"):
                text_parts.append(str(doc["text"])[:2000])
            items.append({
                "quelle_id": source["id"],
                "titel": titel,
                "text": " ".join(text_parts),
                "url": url,
                "datum": datum.isoformat() if datum else None,
                "datum_ist_naeherung": False,
            })
    return items


# ---------------------------------------------------------------------------
# Abruf: RSS/Atom
# ---------------------------------------------------------------------------

def fetch_rss(source: dict) -> list[dict]:
    items = []
    try:
        feed = feedparser.parse(source["url"])
    except Exception as exc:
        print(f"[WARN] RSS-Fehler ({source['id']}): {exc}", file=sys.stderr)
        return items

    for entry in feed.entries:
        datum = None
        for field in ("published", "updated", "pubDate"):
            if hasattr(entry, field):
                datum = parse_date_safe(getattr(entry, field))
                if datum:
                    break
        titel = getattr(entry, "title", "(ohne Titel)")
        summary = getattr(entry, "summary", "")
        items.append({
            "quelle_id": source["id"],
            "titel": titel,
            "text": f"{titel} {summary}",
            "url": getattr(entry, "link", ""),
            "datum": datum.isoformat() if datum else None,
            "datum_ist_naeherung": False,
        })
    return items


# ---------------------------------------------------------------------------
# Abruf: einfache HTML-Seiten ohne Feed
# ---------------------------------------------------------------------------

def fetch_html_scan(source: dict, state: dict) -> list[dict]:
    items = []
    url = source["url"]
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[WARN] HTML-Scan-Fehler ({source['id']}): {exc}", file=sys.stderr)
        return items

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_links = state.setdefault("seen_links", {}).setdefault(source["id"], {})
    today_iso = datetime.now(timezone.utc).date().isoformat()

    # Optionale Filter/Extraktions-Regeln aus der Quellen-Konfiguration:
    # url_pattern      -> Link wird nur berücksichtigt, wenn die volle URL matcht.
    #                     Damit werden Navigations-/Themenseiten (die dauerhaft
    #                     existieren und kein Aktualitätsdatum haben) ausgeschlossen -
    #                     nur echte Presseartikel/-mitteilungen zählen.
    # url_date_regex   -> falls die URL selbst ein Datum enthält (z.B. NATO:
    #                     /news/2026/07/07/...), wird das echte Datum extrahiert
    #                     statt der "first seen"-Näherung.
    url_pattern = re.compile(source["url_pattern"]) if source.get("url_pattern") else None
    url_date_regex = re.compile(source["url_date_regex"]) if source.get("url_date_regex") else None

    alle_links = soup.select(source.get("link_selector", "a"))
    anzahl_zu_kurz = 0
    anzahl_muster_verfehlt = 0
    anzahl_neu = 0

    for a in alle_links:
        href = a.get("href")
        text = a.get_text(strip=True)
        if not href or not text or len(text) < 15:
            anzahl_zu_kurz += 1
            continue  # zu kurze Linktexte sind meist Navigation, keine Artikel
        full_url = urljoin(url, href)

        if url_pattern and not url_pattern.search(full_url):
            anzahl_muster_verfehlt += 1
            continue  # keine echte Pressemitteilung/Artikel-URL (z.B. Themen-/Navigationsseite)

        key = item_hash(full_url)
        if key in seen_links:
            continue  # schon in einem früheren Lauf gesehen

        # Datum bestimmen: aus der URL, falls möglich, sonst Näherung über "first seen"
        echtes_datum = None
        if url_date_regex:
            m = url_date_regex.search(full_url)
            if m:
                try:
                    echtes_datum = date(int(m.group("y")), int(m.group("m")), int(m.group("d"))).isoformat()
                except (ValueError, IndexError):
                    echtes_datum = None

        anzahl_neu += 1
        seen_links[key] = {"first_seen": today_iso, "titel": text, "url": full_url}
        items.append({
            "quelle_id": source["id"],
            "titel": text,
            "text": text,
            "url": full_url,
            "datum": echtes_datum or today_iso,
            "datum_ist_naeherung": echtes_datum is None,
        })

    # Diagnose-Ausgabe: hilft zu unterscheiden zwischen "Seite liefert nichts
    # Sinnvolles" (alle_links niedrig), "Selektor passt nicht zur Seitenstruktur"
    # (alle_links = 0 trotz Status 200), "url_pattern zu streng/falsch"
    # (anzahl_muster_verfehlt hoch, anzahl_neu = 0) und "alles schon bekannt"
    # (anzahl_neu = 0, aber die anderen Zahlen normal).
    print(
        f"[INFO]   {source['id']}: {len(alle_links)} Links gesamt, "
        f"{anzahl_zu_kurz} zu kurz/ohne href, {anzahl_muster_verfehlt} durch url_pattern verworfen, "
        f"{anzahl_neu} neu seit letztem Lauf."
    )
    return items


# ---------------------------------------------------------------------------
# Abruf: XML-Sitemap (statisch, kein JavaScript nötig, oft mit echtem Datum)
# ---------------------------------------------------------------------------

def fetch_sitemap(source: dict, state: dict) -> list[dict]:
    """
    Manche Seiten (v.a. mit JavaScript-basierter Artikel-Übersicht, wo ein
    einfacher HTML-Scan nichts findet, weil die Liste erst nachträglich per
    Skript geladen wird) veröffentlichen trotzdem eine klassische, statische
    sitemap.xml mit allen URLs und meist deren Änderungsdatum (<lastmod>).
    Das ist zuverlässiger als HTML-Scraping.

    Nicht jede Sitemap befüllt <lastmod> für jede URL. Fehlt es, greifen wir
    auf dieselbe "beim ersten Mal gesehen"-Näherung zurück wie beim HTML-Scan,
    statt den Eintrag ganz zu verwerfen.
    """
    items = []
    url = source["url"]
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        content = resp.content
        if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
    except Exception as exc:
        print(f"[WARN] Sitemap-Fehler ({source['id']}): {exc}", file=sys.stderr)
        return items

    soup = BeautifulSoup(content, "xml")
    url_pattern = re.compile(source["url_pattern"]) if source.get("url_pattern") else None
    seen_links = state.setdefault("seen_links", {}).setdefault(source["id"], {})
    today_iso = datetime.now(timezone.utc).date().isoformat()

    alle_eintraege = soup.find_all("url")
    anzahl_muster_verfehlt = 0
    anzahl_naeherung = 0

    for entry in alle_eintraege:
        loc_tag = entry.find("loc")
        if not loc_tag or not loc_tag.text:
            continue
        loc = loc_tag.text.strip()

        if url_pattern and not url_pattern.search(loc):
            anzahl_muster_verfehlt += 1
            continue

        lastmod_tag = entry.find("lastmod")
        datum = parse_date_safe(lastmod_tag.text.strip()) if lastmod_tag else None
        datum_ist_naeherung = False

        if not datum:
            # Kein <lastmod> vorhanden -> "beim ersten Mal gesehen"-Näherung,
            # genau wie beim HTML-Scan, damit der Fund nicht verloren geht.
            key = item_hash(loc)
            if key not in seen_links:
                seen_links[key] = {"first_seen": today_iso, "url": loc}
            datum = date.fromisoformat(seen_links[key]["first_seen"])
            datum_ist_naeherung = True
            anzahl_naeherung += 1

        # Titel gibt es in einer Sitemap nicht - wir leiten einen lesbaren
        # Platzhalter aus der URL ab. Das schränkt die inhaltliche Keyword-
        # Bewertung etwas ein (siehe Hinweis in README).
        slug = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        items.append({
            "quelle_id": source["id"],
            "titel": slug,
            "text": slug,
            "url": loc,
            "datum": datum.isoformat(),
            "datum_ist_naeherung": datum_ist_naeherung,
        })

    print(
        f"[INFO]   {source['id']}: {len(alle_eintraege)} Sitemap-Einträge, "
        f"{anzahl_muster_verfehlt} durch url_pattern verworfen, "
        f"{anzahl_naeherung} ohne lastmod (Näherung genutzt), {len(items)} übernommen."
    )
    return items


# ---------------------------------------------------------------------------
# Bewertung gegen Zusagen
# ---------------------------------------------------------------------------

def normalisiere_umlaute(text: str) -> str:
    """
    URL-Slugs (z.B. aus Sitemaps) transliterieren deutsche Umlaute meist zu
    ae/oe/ue/ss (z.B. "sondervermoegen" statt "sondervermögen"). Damit
    Keywords mit echten Umlauten trotzdem greifen, normalisieren wir beide
    Seiten des Vergleichs auf dieselbe Schreibweise.
    """
    ersetzungen = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "ae", "Ö": "oe", "Ü": "ue",
    }
    for original, ersatz in ersetzungen.items():
        text = text.replace(original, ersatz)
    return text


def contains_any(text: str, keywords: list[str]) -> list[str]:
    text_norm = normalisiere_umlaute(text.lower())
    return [kw for kw in keywords if normalisiere_umlaute(kw.lower()) in text_norm]


def assess_item(item: dict, zusagen: list[dict]) -> list[dict]:
    """
    Regelbasierte Bewertung eines Fundes gegen jede Zusage.

    Optionaler Ausbau: Statt (oder zusätzlich zu) reinem Keyword-Matching
    könnte hier ein Aufruf an die Claude-API erfolgen, um Kontext besser
    zu verstehen (z.B. Verneinungen, Ironie, komplexe Satzstellungen).
    Das kostet pro Aufruf einen kleinen Betrag und ist bewusst NICHT
    Teil der 0€-Basisversion. Falls gewünscht, hier ansetzen.
    """
    treffer = []
    text = item["text"]

    for zusage in zusagen:
        kontext_treffer = contains_any(text, zusage["keywords_kontext"])
        if not kontext_treffer:
            continue  # Fund betrifft diese Zusage nicht erkennbar

        positiv_treffer = contains_any(text, zusage.get("keywords_positiv", []))
        negativ_treffer = contains_any(text, zusage.get("keywords_negativ", []))

        if negativ_treffer and not positiv_treffer:
            richtung = "schwaecht_ab"
        elif positiv_treffer and not negativ_treffer:
            richtung = "verstaerkt"
        else:
            richtung = "unklar"

        treffer.append({
            "zusage_id": zusage["id"],
            "richtung": richtung,
            "kontext_treffer": kontext_treffer,
            "positiv_treffer": positiv_treffer,
            "negativ_treffer": negativ_treffer,
        })
    return treffer


# ---------------------------------------------------------------------------
# Aggregation zu Ampel je Zusage
# ---------------------------------------------------------------------------

def aggregiere_ampel(zusage: dict, bewertete_funde: list[dict]) -> dict:
    heute = datetime.now(timezone.utc).date()
    relevante = [
        f for f in bewertete_funde
        if f["zusage_id"] == zusage["id"]
        and f["datum"] is not None
        and (heute - date.fromisoformat(f["datum"])).days <= LOOKBACK_TAGE_FUER_AMPEL
    ]
    relevante.sort(key=lambda f: f["datum"], reverse=True)

    if not relevante:
        return {
            "id": zusage["id"],
            "name": zusage["name"],
            "ebene": zusage["ebene"],
            "beschreibung": zusage["beschreibung"].strip(),
            "ampel": "neutral",
            "begruendung": f"Keine aktuellen Funde (letzte {LOOKBACK_TAGE_FUER_AMPEL} Tage, ab {MIN_DATUM.isoformat()}) zu dieser Zusage.",
            "letzte_aenderung_datum": None,
            "quelle_url": None,
            "aenderung": "keine_neue_information",
            "historie": [],
        }

    neuester = relevante[0]
    richtung_zu_ampel = {
        "schwaecht_ab": "rot",
        "unklar": "gelb",
        "verstaerkt": "gruen",
    }
    aenderung_text = {
        "schwaecht_ab": "schwächt ab",
        "unklar": "keine klare Richtung erkennbar",
        "verstaerkt": "verstärkt / bestätigt",
    }

    begruendungs_teile = []
    if neuester["kontext_treffer"]:
        begruendungs_teile.append("Bezug erkannt an: " + ", ".join(sorted(set(neuester["kontext_treffer"]))))
    if neuester["positiv_treffer"]:
        begruendungs_teile.append("bestätigende Formulierungen: " + ", ".join(sorted(set(neuester["positiv_treffer"]))))
    if neuester["negativ_treffer"]:
        begruendungs_teile.append("abschwächende Formulierungen: " + ", ".join(sorted(set(neuester["negativ_treffer"]))))

    return {
        "id": zusage["id"],
        "name": zusage["name"],
        "ebene": zusage["ebene"],
        "beschreibung": zusage["beschreibung"].strip(),
        "ampel": richtung_zu_ampel[neuester["richtung"]],
        "begruendung": (
            f"Neuester relevanter Fund vom {neuester['datum']}"
            + (" (Datum genähert, kein echtes Veröffentlichungsdatum verfügbar)" if neuester.get("datum_ist_naeherung") else "")
            + f": {aenderung_text[neuester['richtung']]}. " + "; ".join(begruendungs_teile)
        ),
        "letzte_aenderung_datum": neuester["datum"],
        "quelle_url": neuester["url"],
        "aenderung": aenderung_text[neuester["richtung"]],
        "historie": [
            {
                "datum": f["datum"],
                "datum_ist_naeherung": f.get("datum_ist_naeherung", False),
                "titel": f["titel"],
                "url": f["url"],
                "quelle_id": f["quelle_id"],
                "richtung": f["richtung"],
            }
            for f in relevante[:10]
        ],
    }


def sammle_medienberichte(zusage: dict, bewertete_nachrichten: list[dict], max_eintraege: int = 15) -> list[dict]:
    """
    Rein informative Liste von Medienberichten zu einer Zusage - beeinflusst
    KEINE Ampelfarbe (die basiert ausschließlich auf offiziellen Quellen,
    siehe aggregiere_ampel). Dient nur der Einordnung im "Nachrichten"-Reiter
    des Dashboards.
    """
    heute = datetime.now(timezone.utc).date()
    relevante = [
        f for f in bewertete_nachrichten
        if f["zusage_id"] == zusage["id"]
        and f["datum"] is not None
        and (heute - date.fromisoformat(f["datum"])).days <= LOOKBACK_TAGE_FUER_AMPEL
    ]
    relevante.sort(key=lambda f: f["datum"], reverse=True)
    return [
        {
            "datum": f["datum"],
            "datum_ist_naeherung": f.get("datum_ist_naeherung", False),
            "titel": f["titel"],
            "url": f["url"],
            "quelle_id": f["quelle_id"],
        }
        for f in relevante[:max_eintraege]
    ]


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    zusagen_cfg = load_yaml(CONFIG_DIR / "zusagen.yaml")["zusagen"]
    quellen_cfg = load_yaml(CONFIG_DIR / "quellen.yaml")["quellen"]
    state = load_state()

    alle_funde = []
    for source in quellen_cfg:
        if not source.get("enabled", True):
            continue
        print(f"[INFO] Rufe Quelle ab: {source['id']} ({source['type']}, Kategorie: {source.get('kategorie', 'offiziell')})")
        if source["type"] == "dip_api":
            neue_funde = fetch_dip_api(source)
        elif source["type"] == "rss":
            neue_funde = fetch_rss(source)
        elif source["type"] == "html_scan":
            neue_funde = fetch_html_scan(source, state)
        elif source["type"] == "sitemap":
            neue_funde = fetch_sitemap(source, state)
        else:
            print(f"[WARN] Unbekannter Quellentyp: {source['type']}", file=sys.stderr)
            neue_funde = []

        # Kategorie an jeden Fund anhängen, damit wir später trennen können:
        # "offiziell" bestimmt die Ampel, "nachrichten" ist nur Kontext im
        # separaten Dashboard-Reiter und beeinflusst keine Farbe.
        kategorie = source.get("kategorie", "offiziell")
        for f in neue_funde:
            f["kategorie"] = kategorie
        alle_funde.extend(neue_funde)

    # Harte Datumsgrenze: alles vor MIN_DATUM raus (fehlendes Datum bleibt drin,
    # wird aber im Frontend als "Datum unsicher" markiert statt verworfen)
    gefiltert = []
    for f in alle_funde:
        if f["datum"] is None:
            continue
        if date.fromisoformat(f["datum"]) < MIN_DATUM:
            continue
        gefiltert.append(f)

    print(f"[INFO] {len(alle_funde)} Funde insgesamt, {len(gefiltert)} nach Datumsfilter (>= {MIN_DATUM}).")

    # Bewertung gegen Zusagen (beide Kategorien laufen durch dieselbe Logik,
    # werden aber getrennt weiterverarbeitet)
    bewertete_funde = []
    for item in gefiltert:
        treffer = assess_item(item, zusagen_cfg)
        for t in treffer:
            bewertete_funde.append({**item, **t})

    bewertete_offiziell = [f for f in bewertete_funde if f["kategorie"] == "offiziell"]
    bewertete_nachrichten = [f for f in bewertete_funde if f["kategorie"] == "nachrichten"]

    print(f"[INFO] {len(bewertete_funde)} Treffer gegen Zusagen erkannt "
          f"({len(bewertete_offiziell)} offiziell, {len(bewertete_nachrichten)} Nachrichten).")

    # Log aller Treffer (append-only, für Nachvollziehbarkeit/Debugging).
    # Deduplizierung: Sitemap-Quellen liefern bei jedem Lauf die komplette
    # Historie seit MIN_DATUM erneut (nicht nur "was ist neu"), sonst würde
    # das Log bei jedem Tageslauf dieselben Alt-Treffer erneut aufschreiben
    # und unkontrolliert wachsen. Wir merken uns daher pro (URL, Zusage), ob
    # der Treffer schon einmal geloggt wurde.
    bereits_geloggt = state.setdefault("bereits_geloggt", {})
    neue_log_eintraege = 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for bf in bewertete_funde:
            log_key = item_hash(bf["url"] + "|" + bf["zusage_id"])
            if log_key in bereits_geloggt:
                continue
            bereits_geloggt[log_key] = True
            neue_log_eintraege += 1
            f.write(json.dumps({**bf, "bewertet_am": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
    print(f"[INFO] {neue_log_eintraege} neue Log-Einträge geschrieben (Rest bereits bekannt).")

    # Aggregation je Zusage - Ampel basiert NUR auf offiziellen Quellen,
    # Medienberichte werden separat angehängt (rein informativ)
    ergebnis_zusagen = []
    for z in zusagen_cfg:
        eintrag = aggregiere_ampel(z, bewertete_offiziell)
        eintrag["medienberichte"] = sammle_medienberichte(z, bewertete_nachrichten)
        ergebnis_zusagen.append(eintrag)

    output = {
        "generiert_am": datetime.now(timezone.utc).isoformat(),
        "min_datum_filter": MIN_DATUM.isoformat(),
        "lookback_tage": LOOKBACK_TAGE_FUER_AMPEL,
        "zusagen": ergebnis_zusagen,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    save_state(state)
    print(f"[INFO] status.json geschrieben: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
