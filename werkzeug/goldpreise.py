#!/usr/bin/env python3
"""Holt einmal am Tag Kauf- und Ankaufspreise von gold.de und schreibt sie nach preise.json.

Läuft auf GitHub (siehe .github/workflows/goldpreise.yml).

Kaufpreis  = günstigstes Händlerangebot. Kommt per einfachem Seitenabruf.
Ankaufspreis = höchstes Gebot eines Händlers. Die Liste lädt gold.de erst nach, wenn man
               auf "verkaufen" tippt; deshalb öffnet das Programm die Seite dafür in einem
               echten Browser (Playwright).

Findet das Programm für ein Produkt keinen plausiblen Preis, bleibt der alte Wert stehen
und das Produkt bekommt einen Fehlervermerk. Was bei jedem Lauf passiert ist, steht in
werkzeug/protokoll.txt.
"""
import datetime
import html
import json
import pathlib
import re
import sys
import urllib.request

OZ = 31.1034768
ORDNER = pathlib.Path(__file__).resolve().parent
ZIEL = ORDNER.parent / "preise.json"
PROTOKOLL = ORDNER / "protokoll.txt"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

# feinunzen: Feingewicht des Produkts. band: erlaubter Kaufpreis im Verhältnis zum reinen Goldwert.
PRODUKTE = {
    "kr110": {
        "name": "Krügerrand 1/10 oz",
        "url": "https://www.gold.de/kaufen/goldmuenzen/kruegerrand/?stkl=1%2F10+oz",
        "feinunzen": 0.1,
        "band": (0.99, 1.30),
        "je_gramm": False,
        # Ankauf: nur gewöhnliche Stücke, keine Sammlerware, keine aktuellen Jahrgänge (die bringen mehr).
        "muss": "1/10 oz",
        "ohne": r"\bPP\b|Proof|Privy|Etui|Zertifikat|Polierte",
        "nur_aeltere_jahrgaenge": True,
    },
    "cb50": {
        "name": "CombiBar 50 × 1 g",
        "url": "https://www.gold.de/kaufen/goldbarren/tafeln-kombi-barren/",
        "feinunzen": 50 / OZ,
        "band": (0.99, 1.25),
        "je_gramm": True,  # Die Seite nennt Preise je 1-g-Stück; ersatzweise Grammpreis × 50.
        "muss": "50 x 1 g",
        "ohne": None,
        "nur_aeltere_jahrgaenge": False,
    },
}
ANKAUF_UNTEN = 0.85   # Ankaufspreise unter 85 % des Goldwerts gelten als Lesefehler.
ANKAUF_OBEN = 1.15    # ... über 115 % ebenfalls (Sammlerstücke, andere Größen).
ANBIETER = "zum Anbieter"
BEWERTUNG = re.compile(r"^\d[.,]\d{1,2}/5\s*\(\d+\)$")
BLOCKSTART = re.compile(r"^(Einheit:|Paket:|Gewicht:)")
JAHRGANG = re.compile(r"Jahrgang:\s*(\S+)")

PREIS = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:€|EUR)")
SPOT = re.compile(r"Gold:?\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")
ZUSTIMMEN = re.compile(r"alle akzeptieren|akzeptieren|zustimmen|einverstanden|alle zulassen|accept all", re.I)

LOG = []


def log(zeile=""):
    print(zeile)
    LOG.append(zeile)


def zahl(text):
    return float(text.replace(".", "").replace(",", "."))


def betraege(text):
    return [zahl(m) for m in PREIS.findall(text or "")]


def lade(url):
    """Einfacher Seitenabruf ohne Browser; liefert den sichtbaren Text."""
    anfrage = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    with urllib.request.urlopen(anfrage, timeout=30) as antwort:
        roh = antwort.read().decode("utf-8", "replace")
    roh = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", roh)
    titel = re.search(r"(?is)<title>(.*?)</title>", roh)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", roh))
    return (html.unescape(titel.group(1)) if titel else "") + " \n " + text


def guenstigster(text, spot, produkt):
    """Kaufpreis: kleinster Preis auf der Seite, der zum Goldwert des Produkts passt."""
    wert = spot * produkt["feinunzen"]
    unten, oben = wert * produkt["band"][0], wert * produkt["band"][1]
    preise = betraege(text)
    treffer = [p for p in preise if unten <= p <= oben]
    if treffer:
        return round(min(treffer), 2)
    if produkt["je_gramm"]:
        gramm = produkt["feinunzen"] * OZ
        je_g = [p for p in preise if unten / gramm <= p <= oben / gramm]
        if je_g:
            return round(min(je_g) * gramm, 2)
    return None


def hoechster_ankauf(text_verkauf, text_kauf, spot, produkt, kaufpreis):
    """Ankaufspreis: höchster Preis der Verkaufen-Ansicht, der zum Goldwert passt,
    unter dem günstigsten Kaufpreis liegt und in der Kaufen-Ansicht nicht vorkommt."""
    wert = spot * produkt["feinunzen"]
    unten = wert * ANKAUF_UNTEN
    oben = min(kaufpreis, wert * ANKAUF_OBEN) if kaufpreis else wert * 1.03
    aus_kauf = set(betraege(text_kauf))
    preise = [p for p in betraege(text_verkauf) if p not in aus_kauf]
    treffer = [p for p in preise if unten <= p < oben]
    if treffer:
        return round(max(treffer), 2), sorted(set(treffer), reverse=True)[:8]
    if produkt["je_gramm"]:
        gramm = produkt["feinunzen"] * OZ
        je_g = [p for p in preise if unten / gramm <= p < oben / gramm]
        if je_g:
            return round(max(je_g) * gramm, 2), sorted(set(je_g), reverse=True)[:8]
    return None, []


def gebote(text):
    """Zerlegt die Verkaufen-Ansicht in einzelne Gebote: Beschreibung, Händler, Preis.
    Jedes Gebot endet auf der Seite mit der Schaltfläche "zum Anbieter"."""
    liste = []
    stuecke = (text or "").split(ANBIETER)[:-1]
    for stueck in stuecke:
        zeilen = [z.strip() for z in stueck.splitlines() if z.strip() and z.strip() != "»"]
        start = None
        for i in range(len(zeilen) - 1, -1, -1):
            if BLOCKSTART.match(zeilen[i]):
                start = i - 1 if zeilen[i].startswith("Einheit:") and i > 0 else i
                break
        block = zeilen[start:] if start is not None else zeilen[-12:]
        werte = betraege(" ".join(block))
        if not werte:
            continue
        haendler = ""
        for i, z in enumerate(block):
            if BEWERTUNG.match(z) and i > 0:
                haendler = block[i - 1]
                break
        grenze = block.index(haendler) if haendler in block else len(block)
        jahr = JAHRGANG.search(" ".join(block))
        liste.append({
            "text": " ".join(block),
            "was": ", ".join(block[:grenze]),
            "haendler": haendler,
            "jahrgang": jahr.group(1) if jahr else "",
            "preis": werte[-1],
        })
    return liste


def bestes_gebot(text_verkauf, spot, produkt, jahr_heute):
    """Höchstes Gebot für gewöhnliche Ware. Liefert (Gebot oder None, engere Auswahl)."""
    wert = spot * produkt["feinunzen"]
    unten, oben = wert * ANKAUF_UNTEN, wert * ANKAUF_OBEN
    auswahl = []
    for g in gebote(text_verkauf):
        if produkt["muss"] not in g["text"]:
            continue
        if produkt["ohne"] and re.search(produkt["ohne"], g["text"]):
            continue
        if produkt["nur_aeltere_jahrgaenge"] and g["jahrgang"].isdigit() and int(g["jahrgang"]) >= jahr_heute - 1:
            continue
        if not unten <= g["preis"] <= oben:
            continue
        auswahl.append(g)
    auswahl.sort(key=lambda g: -g["preis"])
    return (auswahl[0] if auswahl else None), auswahl[:6]


# ---------- Browser ----------

def _zustimmen(seite):
    """Schließt ein Cookie-Fenster, falls eines erscheint."""
    for rahmen in seite.frames:
        try:
            knopf = rahmen.get_by_role("button", name=ZUSTIMMEN).first
            if knopf.is_visible(timeout=800):
                knopf.click(timeout=2000)
                seite.wait_for_timeout(800)
                return True
        except Exception:
            pass
    return False


def _ruhe(seite, ms=2500):
    try:
        seite.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    seite.wait_for_timeout(ms)


def _text(seite):
    try:
        return seite.inner_text("body", timeout=10000)
    except Exception:
        return ""


def _klick_verkaufen(seite, pfad, text_kauf):
    """Tippt nacheinander auf sichtbare Schaltflächen, die auf "verkaufen" enden,
    bis sich die Preisliste ändert. Liefert den neuen Text oder None."""
    anzahl = seite.evaluate(
        """() => {
            const alle = [...document.querySelectorAll('a,button,li,span,label,div')];
            const passt = alle.filter(e => {
                const t = (e.innerText || '').trim();
                if (!/verkaufen$/i.test(t) || t.length > 40) return false;
                if ([...e.children].some(c => /verkaufen$/i.test((c.innerText || '').trim()))) return false;
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            const rang = e => (e.closest('[id*=preis i],[class*=preis i],[class*=tab i]') ? 0 : 1);
            passt.sort((a, b) => rang(a) - rang(b));
            passt.slice(0, 6).forEach((e, i) => e.setAttribute('data-vb', String(i)));
            return Math.min(passt.length, 6);
        }"""
    )
    log(f"  Schaltflächen mit 'verkaufen': {anzahl}")
    for i in range(anzahl):
        try:
            ziel = seite.locator(f'[data-vb="{i}"]').first
            beschriftung = (ziel.inner_text(timeout=2000) or "").strip()
            ziel.click(timeout=4000)
            _ruhe(seite, 2500)
        except Exception as grund:
            log(f"  Tippen {i} fehlgeschlagen: {type(grund).__name__}")
            continue
        if pfad not in seite.url:
            log(f"  '{beschriftung}' führte weg zu {seite.url}; zurück")
            try:
                seite.go_back(wait_until="domcontentloaded", timeout=30000)
                _ruhe(seite, 1500)
            except Exception:
                return None
            continue
        neu = _text(seite)
        if betraege(neu) != betraege(text_kauf):
            log(f"  Liste geändert nach Tippen auf '{beschriftung}'")
            return neu
    return None


def ansichten_per_browser():
    """Liefert je Produkt (Text Kaufen-Ansicht, Text Verkaufen-Ansicht oder None)."""
    from playwright.sync_api import sync_playwright  # fehlt Playwright, fängt main() das ab

    ergebnis = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        kontext = browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin", user_agent=UA, viewport={"width": 1280, "height": 1800}
        )
        for kennung, produkt in PRODUKTE.items():
            log(f"[Browser] {produkt['name']}")
            seite = kontext.new_page()
            abrufe = []
            seite.on("request", lambda r, a=abrufe: a.append(r.url) if r.resource_type in ("xhr", "fetch") else None)
            try:
                url = produkt["url"]
                pfad = url.split("?")[0].split("#")[0]
                seite.goto(url, wait_until="domcontentloaded", timeout=45000)
                if _zustimmen(seite):
                    log("  Cookie-Fenster geschlossen")
                _ruhe(seite)
                text_kauf = _text(seite)
                log(f"  Kaufen-Ansicht: {len(text_kauf)} Zeichen, {len(betraege(text_kauf))} Beträge")

                seite.goto(url + ("&" if "?" in url else "?") + "load=verkaufen#preise", wait_until="domcontentloaded", timeout=45000)
                _zustimmen(seite)
                _ruhe(seite)
                text_verkauf = _text(seite)
                if betraege(text_verkauf) != betraege(text_kauf):
                    log("  Liste geändert durch Adresse mit load=verkaufen")
                else:
                    text_verkauf = _klick_verkaufen(seite, pfad, text_kauf)
                if text_verkauf is None:
                    log("  Verkaufen-Ansicht NICHT erreicht")
                else:
                    log(f"  Verkaufen-Ansicht: {len(text_verkauf)} Zeichen, {len(gebote(text_verkauf))} Gebote erkannt")
                ergebnis[kennung] = (text_kauf, text_verkauf)
            except Exception as grund:
                log(f"  Browser-Fehler: {type(grund).__name__}: {grund}")
            eigene = sorted({u.split("?")[0] for u in abrufe if "gold.de" in u})[:25]
            log("  Nachgeladene Adressen bei gold.de: " + (", ".join(eigene) if eigene else "keine"))
            seite.close()
        browser.close()
    return ergebnis


# ---------- Ablauf ----------

def main():
    alt = {}
    if ZIEL.exists():
        try:
            alt = json.loads(ZIEL.read_text(encoding="utf-8"))
        except ValueError:
            alt = {}
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    heute = jetzt.strftime("%Y-%m-%d")
    log(f"Lauf vom {jetzt.strftime('%Y-%m-%d %H:%M')} UTC")
    ergebnis = {"quelle": "gold.de", "stand": heute, "spot_eur": alt.get("spot_eur"), "produkte": {}}
    fehler = 0

    # 1. Kaufpreise per einfachem Abruf
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
                raise RuntimeError("kein plausibler Kaufpreis gefunden")
            eintrag.update({"preis": preis, "stand": heute, "fehler": False})
            log(f"[Kauf] {produkt['name']}: {preis:.2f} EUR (Gold {spot:.2f} EUR/oz)")
        except Exception as grund:  # Der alte Preis bleibt stehen.
            fehler += 1
            eintrag["fehler"] = True
            log(f"[Kauf] {produkt['name']}: FEHLER – {grund}")
        ergebnis["produkte"][kennung] = eintrag

    # 2. Ankaufspreise per Browser
    try:
        ansichten = ansichten_per_browser()
    except Exception as grund:
        ansichten = {}
        log(f"[Browser] nicht verfügbar: {type(grund).__name__}: {grund}")
    for kennung, produkt in PRODUKTE.items():
        eintrag = ergebnis["produkte"][kennung]
        text_kauf, text_verkauf = ansichten.get(kennung, ("", None))
        spot = ergebnis["spot_eur"]
        ankauf, kandidaten = None, []
        if text_verkauf and spot:
            gebot, auswahl = bestes_gebot(text_verkauf, spot, produkt, jetzt.year)
            if gebot:
                ankauf = round(gebot["preis"], 2)
                eintrag["ankauf_haendler"] = gebot["haendler"]
                for g in auswahl:
                    log(f"    {g['preis']:>9.2f} EUR | {g['haendler']} | {g['was']}")
            else:
                # Seitenaufbau nicht erkannt: altes Verfahren (höchster Betrag unter dem Kaufpreis)
                ankauf, kandidaten = hoechster_ankauf(text_verkauf, text_kauf, spot, produkt, eintrag.get("preis"))
                eintrag.pop("ankauf_haendler", None)
                log(f"  Gebote nicht einzeln erkannt, Ersatzverfahren. Kandidaten: {kandidaten}")
                stelle = text_verkauf.find("Fehler melden")
                log("  --- Ausschnitt ---")
                log(text_verkauf[max(0, stelle - 600):max(0, stelle - 600) + 1800])
                log("  --- Ende Ausschnitt ---")
        if ankauf:
            eintrag.update({"ankauf": ankauf, "ankauf_stand": heute, "ankauf_fehler": False})
            log(f"[Ankauf] {produkt['name']}: {ankauf:.2f} EUR" + (f" bei {eintrag['ankauf_haendler']}" if eintrag.get("ankauf_haendler") else ""))
        else:
            eintrag["ankauf_fehler"] = True
            log(f"[Ankauf] {produkt['name']}: kein Ankaufspreis gefunden")

    ZIEL.write_text(json.dumps(ergebnis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROTOKOLL.write_text("\n".join(LOG)[:60000] + "\n", encoding="utf-8")
    # Rückgabewert 1 nur, wenn kein einziger Kaufpreis kam; dann zeigt GitHub den Lauf rot an.
    return 1 if fehler == len(PRODUKTE) else 0


if __name__ == "__main__":
    sys.exit(main())
