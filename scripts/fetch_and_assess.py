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
