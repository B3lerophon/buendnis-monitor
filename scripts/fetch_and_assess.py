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
    "User-Agent": "Buendnisverpflichtungs-Monitor/1.0 (NGO-Themen-Tracking, non-commercial)"
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
    api_key = os.environ.get("DIP_API_KEY", DIP_API_KEY_FALLBACK)
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

    for a in soup.select(source.get("link_selector", "a")):
        href = a.get("href")
        text = a.get_text(strip=True)
        if not href or not text or len(text) < 15:
            continue  # zu kurze Linktexte sind meist Navigation, keine Artikel
        full_url = urljoin(url, href)
        key = item_hash(full_url)

        if key not in seen_links:
            # neuer Link seit dem letzten Lauf -> als "gerade gesehen" markieren
            seen_links[key] = {"first_seen": today_iso, "titel": text, "url": full_url}
            items.append({
                "quelle_id": source["id"],
                "titel": text,
                "text": text,
                "url": full_url,
                "datum": today_iso,
                "datum_ist_naeherung": True,  # kein echtes Veröffentlichungsdatum verfügbar
            })
    return items


# ---------------------------------------------------------------------------
# Bewertung gegen Zusagen
# ---------------------------------------------------------------------------

def contains_any(text: str, keywords: list[str]) -> list[str]:
    text_low = text.lower()
    return [kw for kw in keywords if kw.lower() in text_low]


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
        print(f"[INFO] Rufe Quelle ab: {source['id']} ({source['type']})")
        if source["type"] == "dip_api":
            alle_funde.extend(fetch_dip_api(source))
        elif source["type"] == "rss":
            alle_funde.extend(fetch_rss(source))
        elif source["type"] == "html_scan":
            alle_funde.extend(fetch_html_scan(source, state))
        else:
            print(f"[WARN] Unbekannter Quellentyp: {source['type']}", file=sys.stderr)

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

    # Bewertung gegen Zusagen
    bewertete_funde = []
    for item in gefiltert:
        treffer = assess_item(item, zusagen_cfg)
        for t in treffer:
            bewertete_funde.append({**item, **t})

    print(f"[INFO] {len(bewertete_funde)} Treffer gegen Zusagen erkannt.")

    # Log aller Treffer (append-only, für Nachvollziehbarkeit/Debugging)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for bf in bewertete_funde:
            f.write(json.dumps({**bf, "bewertet_am": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")

    # Aggregation je Zusage
    ergebnis_zusagen = [aggregiere_ampel(z, bewertete_funde) for z in zusagen_cfg]

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
