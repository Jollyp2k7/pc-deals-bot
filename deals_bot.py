#!/usr/bin/env python3
"""
PC deals bot - feed community (Pepper network) -> Discord / Telegram

Fonti: Chollometro (ES), Dealabs (FR), Tom's Hardware (IT, offerte redazionali).
Filtra componenti PC (schede madri, monitor, CPU, RAM, GPU, alimentatori,
tastiere, cuffie, mouse, microfoni) e invia solo:
  - errori di prezzo (parole chiave o sconto anomalo)
  - sconti >= MIN_DISCOUNT %

Uso:
  python deals_bot.py --dry-run   # mostra cosa verrebbe inviato, senza inviare ne' salvare stato
  python deals_bot.py --test      # invia un messaggio di prova ai canali configurati
  python deals_bot.py             # singolo controllo (modalita' GitHub Actions)
  LOOP=1 python deals_bot.py      # loop continuo (modalita' PC locale)

Configurazione: variabili d'ambiente o file .env
  DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (canali, almeno uno)
  MIN_DISCOUNT=25          soglia sconto %
  ERROR_DISCOUNT=60        sconto % oltre il quale scatta "possibile errore di prezzo"
  PRICE_DROP=35            % sotto il prezzo di riferimento oltre cui scatta "prezzo anomalo"
  MIN_SAMPLES=4            prezzi osservati minimi per costruire il riferimento storico
  BASELINE_PCT=75          percentile dei prezzi osservati usato come "prezzo normale"
  HISTORY_DAYS=90          finestra temporale dello storico (giorni)
  REF_FILE=reference_prices.json   prezzi di riferimento manuali (opzionale, vedi sotto)
  SEND_UNKNOWN=0           1 = invia anche componenti PC di cui non si ricava lo sconto
  MERCHANT_ALLOW=amazon    negozi ammessi (separati da virgola); "*" = tutti i negozi
  MERCHANT_BLOCK=          negozi esclusi (separati da virgola)
  INTERVAL_SECONDS=300     pausa in modalita' loop (minimo 60)
"""
import html
import json
import os
import random
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
FEEDS = [
    # Verificati: Chollometro (ES) e Dealabs (FR) -> RSS Pepper; Tom's Hardware IT -> Atom (offerte redazionali)
    {"name": "Chollometro (ES)", "url": "https://www.chollometro.com/rss"},
    {"name": "Dealabs (FR)", "url": "https://www.dealabs.com/rss"},
    {"name": "Tom's Hardware (IT)", "url": "https://www.tomshw.it/feed/offerte"},
]

def env(key, default=None):
    value = os.environ.get(key, "").strip()
    return value if value else default

MIN_DISCOUNT = float(env("MIN_DISCOUNT", "25"))
ERROR_DISCOUNT = float(env("ERROR_DISCOUNT", "60"))
SEND_UNKNOWN = env("SEND_UNKNOWN", "0") == "1"
_allow_raw = env("MERCHANT_ALLOW", "amazon")   # default: solo Amazon ("*" = tutti i negozi)
MERCHANT_ALLOW = [] if _allow_raw.strip() == "*" else [m.strip().lower() for m in _allow_raw.split(",") if m.strip()]
MERCHANT_BLOCK = [m.strip().lower() for m in env("MERCHANT_BLOCK", "").split(",") if m.strip()]
STATE_FILE = Path(env("STATE_FILE", "deals_state.json"))
LOOP = env("LOOP", "0") == "1"
INTERVAL = max(60, int(env("INTERVAL_SECONDS", "300")))
MAX_SEEN = 3000
PRICE_DROP = float(env("PRICE_DROP", "35"))      # % sotto il prezzo di riferimento -> "prezzo anomalo"
MIN_SAMPLES = int(env("MIN_SAMPLES", "4"))       # prezzi osservati minimi per costruire il riferimento
HISTORY_LEN = 60                                 # voci di storico conservate per prodotto
HISTORY_DAYS = int(env("HISTORY_DAYS", "90"))    # finestra temporale dello storico
BASELINE_PCT = float(env("BASELINE_PCT", "75"))  # percentile dei prezzi osservati usato come "prezzo normale"
REF_FILE = Path(env("REF_FILE", "reference_prices.json"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}
NS = {"pepper": "http://www.pepper.com/rss"}

# Categorie PC: parole chiave multilingua (ES / FR / IT / EN) cercate nel TITOLO
CATEGORIES = {
    "Scheda madre": [r"placa\s+base", r"carte\s+m[eè]re", r"scheda\s+madre", r"motherboard",
                     r"\b[bxah][5-8]\d0e?\b"],
    "Monitor": [r"\bmonitor\b", r"\b[eé]cran\s+(?:pc|gaming|gamer|incurv[eé]|plat|\d{2})",
                r"pantalla\s+(?:gaming|pc|curva)", r"schermo\s+(?:gaming|pc|curvo)"],
    "CPU": [r"procesador", r"processeur", r"processore", r"\bryzen\b",
            r"\bcore\s+(?:ultra\s+)?i[3579]\b", r"\bcore\s+ultra\b", r"threadripper", r"\bcpu\b"],
    "RAM": [r"memoria\s+ram", r"m[eé]moire\s+(?:vive|ram)", r"\bddr[45]\b", r"\bram\b"],
    "Scheda video": [r"tarjeta\s+gr[aá]fica", r"carte\s+graphique", r"scheda\s+(?:video|grafica)",
                     r"\brtx\s?\d{4}", r"\bgtx\s?\d{3,4}", r"geforce", r"radeon\s+rx",
                     r"\brx\s?\d{4}\b", r"\bgpu\b"],
    "Alimentatore": [r"fuente\s+(?:de\s+alimentaci[oó]n|modular|atx|pc)", r"bloc\s+d.alimentation",
                     r"alimentation\s+(?:modulaire|atx|pc|80)", r"alimentatore", r"\bpsu\b",
                     r"80\s?\+?\s?plus"],
    "Tastiera": [r"teclado", r"clavier", r"tastiera", r"keyboard"],
    "Cuffie": [r"auriculares", r"\bcasque\b", r"cuffie", r"headset", r"headphones"],
    "Mouse": [r"\bmouse\b", r"\brat[oó]n\b", r"\bsouris\b"],
    "Microfono": [r"micr[oó]fono", r"microphone", r"microfono"],
}
CATEGORY_RE = {c: re.compile("|".join(p), re.I) for c, p in CATEGORIES.items()}

# Falsi positivi comuni (monitor per bebe', per pressione ecc.)
EXCLUDE_RE = re.compile(
    r"beb[eé]|\bbaby\b|b[eé]b[eé]|tensi[oó]n\s+arterial|presi[oó]n\s+arterial|gluc[oó]|"
    r"tension\s+art[eé]rielle|pressione|\bgatos?\b|\bperros?\b|"
    r"bras\s+articul|\bbrazo\b|\bsoporte\b|\bsupporto\b|braccio\s+(?:per|monitor)|alfombrilla|\bfunda\b|housse", re.I)

ERROR_KEYWORDS_RE = re.compile(
    r"error\s+(?:de\s+)?precio|erreur\s+de\s+prix|bug\s+de\s+prix|errore\s+(?:di\s+)?prezzo|"
    r"price\s+(?:error|mistake)|pricing\s+error|glitch|prix\s+erron", re.I)

ORIG_RE = re.compile(
    r"(?:antes|pvp|precio\s+(?:habitual|original|anterior|recomendado|medio)|"
    r"prix\s+(?:habituel|initial|conseill[eé]|moyen)|au\s+lieu\s+de|pvc|"
    r"invece\s+(?:di|dei|del|degli)|prezzo\s+(?:di\s+listino|consigliato|precedente|originale|medio)|"
    r"listino|rrp|msrp|normalmente)"
    r"\s*:?\s*(?:di\s+|dei\s+)?(\d[\d.,]*)", re.I)
ORIG_BEFORE_RE = re.compile(r"(\d[\d.,]*)\s?(?:€|eur|euro)\s+di\s+listino", re.I)
ORIG_FROM_TO_RE = re.compile(r"\bda\s+(\d[\d.,]*)\s?€\s+a\s+\d", re.I)
ORIG_PATTERNS = [ORIG_RE, ORIG_BEFORE_RE, ORIG_FROM_TO_RE]

PCT_RE = re.compile(
    r"-\s?(\d{2})\s?%|"
    r"(\d{2})\s?%\s*(?:de\s+)?(?:descuento|dto|d\.?to|r[eé]duc|remise|sconto|off)|"
    r"(?:sconto|scontat[oa]|ribasso|calo)\s+(?:del\s+|di\s+|al\s+)?(\d{2})\s?%|"
    r"(?:al\s+)?(\d{2})\s?%\s+in\s+meno", re.I)

ATOM = "{http://www.w3.org/2005/Atom}"
COSTA_RE = re.compile(r"costa\s+(\d[\d.,]*)\s?€", re.I)
EURO_RE = re.compile(r"(\d[\d.,]*)\s?(?:€|euro\b|eur\b)", re.I)
MERCHANT_RE = re.compile(r"\bsu\s+([A-Z][\w.&-]+(?:\s[A-Z][\w.&-]+)?)")

# ----------------------------------------------------------------------------
# PARSING
# ----------------------------------------------------------------------------
def parse_price(text):
    """'1.999,00 EUR' / 'EUR1,999.00' -> 1999.0"""
    digits = re.sub(r"[^\d.,]", "", text or "")
    if not digits:
        return None
    if "," in digits and "." in digits:
        decimal = "," if digits.rfind(",") > digits.rfind(".") else "."
    elif "," in digits:
        decimal = "," if len(digits.split(",")[-1]) <= 2 else None
    elif "." in digits:
        decimal = "." if len(digits.split(".")[-1]) <= 2 else None
    else:
        decimal = None
    if decimal:
        integer, _, frac = digits.rpartition(decimal)
        integer = re.sub(r"[.,]", "", integer)
        digits = f"{integer}.{frac or '0'}"
    else:
        digits = re.sub(r"[.,]", "", digits)
    try:
        return float(digits)
    except ValueError:
        return None


def fetch_items(feed):
    r = requests.get(feed["url"], headers=HEADERS, timeout=25)
    r.raise_for_status()
    head = r.content[:400].lower()
    if b"<rss" not in head and b"<feed" not in head and b"<?xml" not in head:
        ctype = r.headers.get("content-type", "?")
        raise ValueError(f"risposta non XML (tipo {ctype}, url finale {r.url})")
    root = ET.fromstring(r.content)
    items = []

    if root.tag == ATOM + "feed":   # formato Atom (es. Tom's Hardware)
        for e in root.findall(ATOM + "entry"):
            link_el = e.find(ATOM + "link")
            link = (link_el.get("href") if link_el is not None else "").strip()
            title = (e.findtext(ATOM + "title") or "").strip()
            desc = BeautifulSoup(e.findtext(ATOM + "summary") or "", "html.parser").get_text(" ", strip=True)
            m = COSTA_RE.search(desc)
            if m:
                price_txt = m.group(1) + "€"
            else:
                found = EURO_RE.findall(title) or EURO_RE.findall(desc)
                price_txt = (found[-1] + "€") if found else None
            mm = MERCHANT_RE.search(desc)
            merchant = mm.group(1) if mm else ("Amazon" if re.search("amazon", f"{title} {desc}", re.I) else "")
            items.append({
                "guid": (e.findtext(ATOM + "id") or link).strip(),
                "title": title, "link": link, "feed_category": "",
                "merchant": merchant, "price_txt": price_txt,
                "price": parse_price(price_txt), "desc": desc, "source": feed["name"],
            })
        return items

    for it in root.iter("item"):   # formato RSS (Pepper network)
        merchant_el = it.find("pepper:merchant", NS)
        merchant = merchant_el.get("name", "") if merchant_el is not None else ""
        price_txt = merchant_el.get("price") if merchant_el is not None else None
        desc = BeautifulSoup(it.findtext("description") or "", "html.parser").get_text(" ", strip=True)
        link = (it.findtext("link") or "").strip()
        items.append({
            "guid": (it.findtext("guid") or link).strip(),
            "title": (it.findtext("title") or "").strip(),
            "link": link,
            "feed_category": (it.findtext("category") or "").strip(),
            "merchant": merchant,
            "price_txt": price_txt,
            "price": parse_price(price_txt),
            "desc": desc,
            "source": feed["name"],
        })
    return items


def classify(title):
    if EXCLUDE_RE.search(title):
        return None
    for cat, rx in CATEGORY_RE.items():
        if rx.search(title):
            return cat
    return None


def extract_discount(title, desc, price):
    """Ritorna (sconto_percentuale, prezzo_originale|None). Euristica: vedi README."""
    best, orig = 0.0, None
    text = f"{title} {desc[:600]}"
    if price:
        for rx in ORIG_PATTERNS:
            m = rx.search(text)
            if not m:
                continue
            o = parse_price(m.group(1).rstrip(".,"))
            if o and o > price:
                orig, best = o, (1 - price / o) * 100
                break
    for m in PCT_RE.finditer(f"{title} {desc[:300]}"):
        pct = int(next(g for g in m.groups() if g))
        if 5 <= pct <= 95:
            best = max(best, pct)
    return best, orig


def merchant_allowed(merchant):
    name = merchant.lower()
    if MERCHANT_ALLOW and not any(a in name for a in MERCHANT_ALLOW):
        return False
    return not any(b in name for b in MERCHANT_BLOCK)


TOKEN_RE = re.compile(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]{3,}\b", re.I)
GENERIC_TOKEN_RE = re.compile(
    r"^(?:ddr[45]|\d+(?:gb|tb|hz|mhz|mts|w|mm|cm|ms)|\d+x\d+|usb\d*|rgb|hdmi|wifi\d*|4k|2k|"
    r"1440p|1080p|cl\d+|\d{3,4}p)$", re.I)
GPU_JOIN_RE = re.compile(r"\b(rtx|gtx|rx|gt)\s?(\d{3,4})\s?(ti\s?super|super|ti|xtx|xt)?\b", re.I)


def model_key(title, cat):
    """Chiave approssimata del prodotto: categoria + fino a 3 codici modello alfanumerici."""
    t = GPU_JOIN_RE.sub(lambda m: (m.group(1) + m.group(2) + (m.group(3) or "")).replace(" ", ""), title)
    toks = []
    for tok in TOKEN_RE.findall(t):
        tok = tok.lower().replace("-", "")
        if GENERIC_TOKEN_RE.match(tok) or tok in toks:
            continue
        toks.append(tok)
    if not toks:
        return None
    return f"{cat}:{'|'.join(sorted(toks[:3]))}"


def load_refs():
    """Prezzi di riferimento manuali: [{"pattern": "rtx\\s?5070\\s?ti", "ref": 800, "name": "RTX 5070 Ti"}]"""
    try:
        data = json.loads(REF_FILE.read_text())
        return [(re.compile(r["pattern"], re.I), float(r["ref"]), r.get("name", r["pattern"])) for r in data]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, KeyError, ValueError, re.error) as exc:
        print(f"ATTENZIONE: {REF_FILE} non valido ({exc}); ignorato.")
        return []


REFS = load_refs()
PRICE_HISTORY = {}   # chiave prodotto -> lista di [timestamp, prezzo, tipo]; tipo "p" = prezzo osservato, "l" = listino dichiarato
MIN_HIST_RE = re.compile(r"minimo\s+storico|prezzo\s+pi[uù]\s+basso|mai\s+(?:costato|visto)", re.I)


def _percentile(values, pct):
    v = sorted(values)
    return v[int(round(pct / 100 * (len(v) - 1)))]


def _recent(key):
    cutoff = time.time() - HISTORY_DAYS * 86400
    return [e for e in PRICE_HISTORY.get(key, []) if e[0] >= cutoff]


def baseline_for(title, cat):
    """Ritorna (prezzo_di_riferimento, nota, affidabilita) oppure (None, None, None).

    Ordine: 1) prezzo manuale da reference_prices.json; 2) storico costruito dal bot.
    Nello storico i prezzi osservati sono gia' scontati, quindi come "prezzo normale" si usa
    un percentile alto (BASELINE_PCT); se esiste anche il listino dichiarato dalle fonti si
    sceglie il valore piu' basso dei due (approccio prudente contro i listini gonfiati).
    """
    for rx, ref, name in REFS:
        if rx.search(title):
            return ref, f"riferimento manuale {name}", "alta"
    key = model_key(title, cat)
    if not key:
        return None, None, None
    hist = _recent(key)
    observed = [p for _, p, k in hist if k == "p"]
    listed = [p for _, p, k in hist if k == "l"]
    candidates, notes = [], []
    if len(observed) >= MIN_SAMPLES:
        candidates.append(_percentile(observed, BASELINE_PCT))
        notes.append(f"{BASELINE_PCT:.0f}\u00b0 percentile di {len(observed)} prezzi osservati")
    if len(listed) >= 2:
        candidates.append(statistics.median(listed))
        notes.append(f"listino mediano di {len(listed)} segnalazioni")
    if not candidates:
        return None, None, None
    conf = "media" if (len(candidates) == 2 or len(observed) >= 10) else "bassa"
    return min(candidates), " + ".join(notes) + " (storico del bot)", conf


def record_price(item):
    cat = classify(item["title"])
    if not cat or not item["price"]:
        return
    key = model_key(item["title"], cat)
    if not key:
        return
    now = time.time()
    hist = PRICE_HISTORY.setdefault(key, [])
    hist.append([now, item["price"], "p"])
    _, orig = extract_discount(item["title"], item["desc"], item["price"])
    if orig:
        hist.append([now, orig, "l"])
    del hist[:-HISTORY_LEN]


def evaluate(item):
    """Ritorna None oppure dict con la decisione di invio."""
    cat = classify(item["title"])
    if not cat or not merchant_allowed(item["merchant"]):
        return None
    discount, orig = extract_discount(item["title"], item["desc"], item["price"])
    text = f"{item['title']} {item['desc'][:400]}"
    keyword_error = bool(ERROR_KEYWORDS_RE.search(text))
    lowest = bool(MIN_HIST_RE.search(text))
    base, base_note, conf, below = None, None, None, 0.0
    if item["price"]:
        base, base_note, conf = baseline_for(item["title"], cat)
        if base and item["price"] < base:
            below = (1 - item["price"] / base) * 100
    if not conf:
        conf = "media" if orig else "bassa"
    is_error = keyword_error or discount >= ERROR_DISCOUNT
    is_anomaly = below >= PRICE_DROP
    is_deal = discount >= MIN_DISCOUNT
    if not (is_error or is_anomaly or is_deal or SEND_UNKNOWN):
        return None
    kind = "error" if is_error else ("anomaly" if is_anomaly else ("deal" if is_deal else "unknown"))
    return {"category": cat, "discount": discount, "orig": orig, "kind": kind,
            "base": base, "base_note": base_note, "below": below,
            "confidence": conf, "lowest": lowest}

# ----------------------------------------------------------------------------
# MESSAGGI E NOTIFICHE
# ----------------------------------------------------------------------------
def build_message(item, d):
    head = {"error": "🚨 POSSIBILE ERRORE DI PREZZO",
            "anomaly": f"⚠️ PREZZO ANOMALO (-{d['below']:.0f}% sotto il riferimento)",
            "deal": f"🔥 SCONTO -{d['discount']:.0f}%",
            "unknown": "🛒 NUOVA OFFERTA"}[d["kind"]]
    lines = [f"{head} [{d['category']}]", item["title"]]
    price = item["price_txt"] or "prezzo n/d"
    if d["orig"]:
        lines.append(f"Prezzo: {price} (prima {d['orig']:.2f} EUR, circa -{d['discount']:.0f}%)")
    elif d["discount"]:
        lines.append(f"Prezzo: {price} (sconto indicato circa -{d['discount']:.0f}%)")
    else:
        lines.append(f"Prezzo: {price}")
    if d["base"]:
        lines.append(f"Riferimento: {d['base']:.2f} EUR ({d['base_note']})")
    if d.get("lowest"):
        lines.append("La fonte indica un minimo storico.")
    lines.append(f"Affidabilita' della valutazione: {d.get('confidence', 'bassa')} (non e' una conferma dell'errore)")
    lines.append(f"Negozio: {item['merchant'] or 'n/d'} | Fonte: {item['source']}")
    lines.append(item["link"])
    lines.append("Verifica la spedizione in Italia prima di acquistare.")
    return "\n".join(lines)


def send_discord(text):
    hook = env("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    r = requests.post(hook, json={"content": text[:1990]}, timeout=15)
    if r.status_code >= 300:
        print(f"  Discord HTTP {r.status_code}")


def send_telegram(text):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": html.escape(text)[:4000],
                            "parse_mode": "HTML"}, timeout=15)
    if r.status_code >= 300:
        print(f"  Telegram HTTP {r.status_code}: {r.text[:120]}")


def notify(text):
    for fn in (send_discord, send_telegram):
        try:
            fn(text)
        except Exception as exc:
            print(f"  {fn.__name__} fallita: {exc}")
    time.sleep(1.2)  # rispetta i rate limit dei canali

# ----------------------------------------------------------------------------
# STATO
# ----------------------------------------------------------------------------
def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if isinstance(data, list):   # formato precedente
        data = {"seen": data, "prices": {}}
    now = time.time()
    for key, entries in data.get("prices", {}).items():   # migra il formato precedente (solo prezzi)
        PRICE_HISTORY[key] = [e if isinstance(e, list) else [now, float(e), "p"] for e in entries]
    return data.get("seen", [])


def save_state(seen):
    prices = dict(list(PRICE_HISTORY.items())[-3000:])
    STATE_FILE.write_text(json.dumps({"seen": seen[-MAX_SEEN:], "prices": prices}))

# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def collect():
    items = []
    for feed in FEEDS:
        try:
            got = fetch_items(feed)
            print(f"[{datetime.now():%H:%M:%S}] {feed['name']}: {len(got)} offerte lette")
            items.extend(got)
        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] {feed['name']}: ERRORE {exc}")
    return items


def run_once(dry=False):
    items = collect()
    if dry:
        load_state()   # carica lo storico prezzi (senza modificarlo)
        shown = 0
        for it in items:
            cat = classify(it["title"])
            if not cat:
                continue
            d = evaluate(it)
            tag = d["kind"].upper() if d else "scartata"
            extra = f" | -{d['below']:.0f}% vs rif." if d and d["below"] else ""
            print(f"  [{cat}] {tag} | {it['price_txt']} | {it['merchant']}{extra} | {it['title'][:90]}")
            shown += 1
        print(f"Componenti PC trovati: {shown}")
        return

    seen = load_state()
    first_run = seen is None
    seen = seen or []
    seen_set = set(seen)
    new_items = [it for it in items if it["guid"] not in seen_set]

    if first_run:
        print("Primo avvio: segno le offerte attuali come gia' viste (nessun invio).")
        for it in reversed(new_items):
            record_price(it)
    else:
        for it in reversed(new_items):  # dalla piu' vecchia alla piu' recente
            d = evaluate(it)
            if d:
                print(f"  INVIO [{d['kind']}] {it['title'][:80]}")
                notify(build_message(it, d))
            if not d or d["kind"] not in ("error", "anomaly"):
                record_price(it)   # i prezzi anomali non entrano nello storico
    seen.extend(it["guid"] for it in new_items)
    save_state(seen)


def main():
    if "--test" in sys.argv:
        notify("🧪 Test PC deals bot: canali configurati correttamente.")
        print("Messaggio di test inviato (controlla i canali).")
        return
    dry = "--dry-run" in sys.argv
    if LOOP and not dry:
        while True:
            run_once()
            time.sleep(INTERVAL * random.uniform(0.8, 1.2))
    else:
        run_once(dry=dry)


if __name__ == "__main__":
    main()
