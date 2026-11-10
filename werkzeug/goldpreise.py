#!/usr/bin/env python3
"""Holt einmal am Tag die günstigsten Kaufpreise von gold.de und schreibt sie nach preise.json.

Läuft auf GitHub (siehe .github/workflows/goldpreise.yml). Braucht nur die Python-Standardbibliothek.
Findet das Programm für ein Produkt keinen plausiblen Preis, bleibt der alte Wert stehen
und das Produkt bekommt den Vermerk "fehler": true.
"""
import datetime
import html
import json
import pathlib
import re
import sys
import urllib.request

OZ = 31.1034768
ZIEL = pathlib.Path(__file__).resolve().parent.parent / "preise.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

# feinunzen: Feingewicht des Produkts. band: erlaubter Preis im Verhältnis zum reinen Goldwert.
PRODUKTE = {
    "kr110": {
        "name": "Krügerrand 1/10 oz",
        "url": "https://www.gold.de/kaufen/goldmuenzen/kruegerrand/?stkl=1%2F10+oz",
        "feinunzen": 0.1,
        "band": (0.99, 1.30),
        "je_gramm": False,
    },
    "cb50": {
        "name": "CombiBar 50 × 1 g",
        "url": "https://www.gold.de/kaufen/goldbarren/tafeln-kombi-barren/",
        "feinunzen": 50 / OZ,
        "band": (0.99, 1.25),
        "je_gramm": True,  # Die Seite nennt Preise je 1-g-Stück; ersatzweise Grammpreis × 50.
    },
}

PREIS = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:€|EUR)")
SPOT = re.compile(r"Gold:?\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")


def zahl(text):
    return float(text.replace(".", "").replace(",", "."))


def lade(url):
    anfrage = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    with urllib.request.urlopen(anfrage, timeout=30) as antwort:
        roh = antwort.read().decode("utf-8", "replace")
    roh = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", roh)
    titel = re.search(r"(?is)<title>(.*?)</title>", roh)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", roh))
    return (html.unescape(titel.group(1)) if titel else "") + " \n " + text


def guenstigster(text, spot, produkt):
    """Kleinster Preis auf der Seite, der zum Goldwert des Produkts passt."""
    wert = spot * produkt["feinunzen"]
    unten, oben = wert * produkt["band"][0], wert * produkt["band"][1]
    preise = [zahl(m) for m in PREIS.findall(text)]
    treffer = [p for p in preise if unten <= p <= oben]
    if treffer:
        return round(min(treffer), 2)
    if produkt["je_gramm"]:
        gramm = produkt["feinunzen"] * OZ
        g_unten, g_oben = unten / gramm, oben / gramm
        je_g = [p for p in preise if g_unten <= p <= g_oben]
        if je_g:
            return round(min(je_g) * gramm, 2)
    return None


def main():
    alt = {}
    if ZIEL.exists():
        try:
            alt = json.loads(ZIEL.read_text(encoding="utf-8"))
        except ValueError:
            alt = {}
    heute = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    ergebnis = {"quelle": "gold.de", "stand": heute, "spot_eur": alt.get("spot_eur"), "produkte": {}}
    fehler = 0

    for kennung, produkt in PRODUKTE.items():
        eintrag = dict(alt.get("produkte", {}).get(kennung, {}))
        eintrag.update({"name": produkt["name"], "url": produkt["url"], "feinunzen": round(produkt["feinunzen"], 6)})
        try:
            text = lade(produkt["url"])
            gefunden = SPOT.search(text)
            spot = zahl(gefunden.group(1)) if gefunden else ergebnis["spot_eur"]
            if not spot:
                raise RuntimeError("kein Goldpreis auf der Seite gefunden")
            ergebnis["spot_eur"] = spot
            preis = guenstigster(text, spot, produkt)
            if preis is None:
                raise RuntimeError("kein plausibler Preis gefunden")
            eintrag.update({"preis": preis, "stand": heute, "fehler": False})
            print(f"{produkt['name']}: {preis:.2f} EUR (Gold {spot:.2f} EUR/oz)")
        except Exception as grund:  # Der alte Preis bleibt stehen.
            fehler += 1
            eintrag["fehler"] = True
            print(f"{produkt['name']}: FEHLER – {grund}", file=sys.stderr)
        ergebnis["produkte"][kennung] = eintrag

    ZIEL.write_text(json.dumps(ergebnis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Rückgabewert 1 nur, wenn gar nichts geklappt hat; dann zeigt GitHub den Lauf rot an.
    return 1 if fehler == len(PRODUKTE) else 0


if __name__ == "__main__":
    sys.exit(main())
