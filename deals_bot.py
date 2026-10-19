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
  SEND_ALL=1               1 = invia TUTTE le segnalazioni dei canali Telegram (senza filtri di categoria, negozio, paese, duplicati)
  MAX_PER_RUN=40           massimo di messaggi per esecuzione (il resto va al giro successivo); SEND_DELAY=2.1 = pausa in secondi
  CURATED_SEND=1           1 = invia le segnalazioni dei canali Telegram curati; 0 = solo sconti/prezzi anomali
  COUNTRIES=it,es,fr,de    marketplace ammessi per i canali Telegram (de = Amazon.de)
  EXTRA_CATEGORIES=1       1 = include accessori PC e rete; 0 = solo le categorie principali
  EXCLUDE_USED=0           1 = scarta le offerte Amazon Seconda Mano
  DEDUPE_HOURS=24          finestra anti-duplicati tra fonti; REPRICE_DROP=3 = % di ribasso per rinviare l'offerta
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
    # Feed RSS/Atom (verificati)
    {"name": "Chollometro (ES)", "url": "https://www.chollometro.com/rss"},
    {"name": "Dealabs (FR)", "url": "https://www.dealabs.com/rss"},
    {"name": "Tom's Hardware (IT)", "url": "https://www.tomshw.it/feed/offerte"},
    # Canali Telegram pubblici di offerte Amazon, letti dall'anteprima web pubblica (t.me/s/<canale>).
    # "curated": le segnalazioni sono selezionate dal canale -> vengono inviate anche senza sconto calcolabile.
    {"name": "Prodigeek Offerte (Telegram)", "type": "tgchannel", "channel": "prodigeekOfferte", "curated": True, "hw": True},
    {"name": "JustTrack It (Telegram)", "type": "tgchannel", "channel": "JustTrackIt", "curated": True, "hw": True},
    {"name": "AssemblarePCOnline (Telegram)", "type": "tgchannel", "channel": "assemblarepconline", "curated": True, "hw": True},
    {"name": "VereOfferteTech (Telegram)", "type": "tgchannel", "channel": "vereoffertetech", "curated": True, "hw": True},
    {"name": "MoreThanTech (Telegram)", "type": "tgchannel", "channel": "MTTVIDEO", "curated": True, "hw": True},
    {"name": "Prophecy Tech (Telegram)", "type": "tgchannel", "channel": "nexthardwareofferte", "curated": True, "hw": True},
    {"name": "Prezz.one (Telegram)", "type": "tgchannel", "channel": "Prezzone", "curated": True},
    {"name": "PCBuilder Hardware Offerte (Telegram)", "type": "tgchannel", "channel": "pcbuilderofferte", "curated": True, "hw": True},
    {"name": "MiglioriPC (Telegram)", "type": "tgchannel", "channel": "miglioripc", "curated": True, "hw": True},
    {"name": "OutOfBit Offerte Tech (Telegram)", "type": "tgchannel", "channel": "offertetechoutofbit", "curated": True},
    {"name": "Tempo Di Sconti (Telegram)", "type": "tgchannel", "channel": "tempodisconti", "curated": True},
    {"name": "Offerte (Telegram)", "type": "tgchannel", "channel": "offerte", "curated": True},
    {"name": "Errori di Prezzo (Telegram)", "type": "tgchannel", "channel": "erroridiprezzo", "curated": True},
    {"name": "Offertazze Errori a Raffica (Telegram)", "type": "tgchannel", "channel": "offertazze", "curated": True},
    {"name": "Probabili Errori (Telegram)", "type": "tgchannel", "channel": "probabilierrori", "curated": True},
    {"name": "Bombe ed Errori di Prezzo (Telegram)", "type": "tgchannel", "channel": "bombeimperdibili", "curated": True},
    {"name": "AmazPrice (Telegram)", "type": "tgchannel", "channel": "amazprice", "curated": True},
    {"name": "Sconty Online (Telegram)", "type": "tgchannel", "channel": "sconty_online", "curated": True},
    {"name": "PC Hardware & Offerte (Telegram)", "type": "tgchannel", "channel": "pc_hardware_offerte", "curated": True, "hw": True},
    {"name": "Codici Sconto Tech (Telegram)", "type": "tgchannel", "channel": "codiciscontotech", "curated": True},
    {"name": "CervOfferte (Telegram)", "type": "tgchannel", "channel": "cervofferte", "curated": True},
    {"name": "TrovaPromozioni (Telegram)", "type": "tgchannel", "channel": "trovapromozioni", "curated": True},
    {"name": "Mister Affare (Telegram)", "type": "tgchannel", "channel": "mister_affare", "curated": True},
    {"name": "Digital Offerte (Telegram)", "type": "tgchannel", "channel": "digitalofferte", "curated": True},
    {"name": "Offerte Coupon Sconti (Telegram)", "type": "tgchannel", "channel": "couponscontiofferte", "curated": True},
    {"name": "Offerte Curious (Telegram)", "type": "tgchannel", "channel": "offertecurious", "curated": True},
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
MAX_SEEN = 8000
_sa = env("SEND_ALL", "all").lower()
SEND_ALL = "all" if _sa in ("1", "true", "all") else ("hw" if _sa == "hw" else "0")
# all = invia TUTTE le segnalazioni di tutti i canali Telegram | hw = tutto dai soli canali dedicati all'hardware | 0 = filtri attivi
MAX_PER_RUN = int(env("MAX_PER_RUN", "40"))              # massimo di messaggi per esecuzione; il resto va al giro successivo
SEND_DELAY = float(env("SEND_DELAY", "2.1"))             # pausa tra messaggi (Discord: max 30 al minuto per webhook)
CURATED_SEND = env("CURATED_SEND", "1") == "1"       # 1 = invia le segnalazioni dei canali curati anche senza sconto calcolabile
EXCLUDE_USED = env("EXCLUDE_USED", "0") == "1"            # 1 = scarta le offerte Amazon Seconda Mano (canali Telegram)
DEDUPE_HOURS = float(env("DEDUPE_HOURS", "24"))          # finestra anti-duplicati tra fonti diverse
REPRICE_DROP = float(env("REPRICE_DROP", "3"))           # % di ulteriore ribasso per rinviare la stessa offerta
ALLOWED_COUNTRIES = [c.strip().lower() for c in env("COUNTRIES", "it,es,fr,de").split(",") if c.strip()]  # solo canali Telegram
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
    "RAM": [r"memoria\s+ram", r"m[eé]moire\s+(?:vive|ram)", r"\bddr[45]\b", r"\bs?o?-?dimm\b",
            r"\bram\b.*\b(?:kit|mhz|mt/s)\b", r"\b(?:kit|mhz|mt/s)\b.*\bram\b"],
    "Scheda video": [r"tarjeta\s+gr[aá]fica", r"carte\s+graphique", r"scheda\s+(?:video|grafica)",
                     r"\brtx\s?\d{4}", r"\bgtx\s?\d{3,4}", r"geforce", r"radeon\s+rx",
                     r"\brx\s?\d{4}", r"\bgpu\b"],
    "Alimentatore": [r"fuente\s+(?:de\s+alimentaci[oó]n|modular|atx|pc)", r"bloc\s+d.alimentation",
                     r"alimentation\s+(?:modulaire|atx|pc|80)", r"alimentatore", r"\bpsu\b",
                     r"80\s?\+?\s?plus"],
    "Tastiera": [r"teclado", r"clavier", r"tastiera", r"keyboard"],
    "Cuffie": [r"auriculares", r"\bcasque\b", r"cuffi[ae]", r"headset", r"headphones"],
    "Mouse": [r"\bmouse\b", r"\brat[oó]n\b", r"\bsouris\b"],
    "Microfono": [r"micr[oó]fono", r"microphone", r"microfono"],
    "SSD / disco": [r"\bssd\b", r"\bnvme\b", r"\bm\.2\b", r"hard\s?disk", r"disque\s+dur", r"disco\s+duro"],
    "Raffreddamento": [r"dissipatore", r"raffreddamento\s+(?:a\s+)?liquido", r"\baio\b", r"ventola", r"ventilador",
                       r"ventirad", r"watercooling"],
    "Case": [r"\bcase\s+(?:pc|gaming)", r"pc\s+(?:gaming\s+)?case", r"mid[- ]?tower", r"full[- ]?tower",
             r"bo[iî]tier\s+pc", r"torre\s+pc"],
    "Webcam": [r"\bwebcam\b"],
}
EXTRA_CATEGORIES = env("EXTRA_CATEGORIES", "1") == "1"   # 1 = include accessori PC e rete (tappetini, hub, cavi, router...)
if EXTRA_CATEGORIES:
    CATEGORIES["Accessori PC"] = [
        r"tappetino\s+(?:per\s+|da\s+)?(?:mouse|gaming|scrivania)", r"mouse\s?pad", r"alfombrilla", r"supporto\s+(?:per\s+)?monitor", r"braccio\s+(?:per\s+)?monitor",
        r"bras\s+articul", r"soporte\s+(?:para\s+)?monitor", r"\bhub\s+usb", r"docking\s+station",
        r"cavo\s+(?:displayport|hdmi|thunderbolt)", r"cable\s+(?:displayport|hdmi)", r"pasta\s+termica",
        r"\bgamepad\b", r"controller\s+(?:wireless\s+)?(?:per\s+|di\s+gioco\s+)?(?:pc|xbox|playstation)", r"controller.{0,25}(?:xbox|playstation)", r"capture\s+card", r"stream\s?deck",
        r"\bkvm\b", r"\bpen\s?drive\b", r"chiavetta\s+usb", r"gruppo\s+di\s+continuit", r"\bups\b"]
    CATEGORIES["Rete"] = [r"\brouter\b", r"\bmesh\b", r"access\s+point", r"\bpowerline\b",
                          r"switch\s+(?:di\s+rete|ethernet|gigabit|poe)", r"scheda\s+di\s+rete", r"ripetitore\s+wi-?fi", r"\bnas\b"]
_PRIORITY = ("Case", "Alimentatore", "Scheda video", "Scheda madre", "Raffreddamento", "Rete", "Accessori PC", "SSD / disco")
_PRIORITY = tuple(k for k in _PRIORITY if k in CATEGORIES)
CATEGORIES = {**{k: CATEGORIES[k] for k in _PRIORITY}, **{k: v for k, v in CATEGORIES.items() if k not in _PRIORITY}}
CATEGORY_RE = {c: re.compile("|".join(p), re.I) for c, p in CATEGORIES.items()}

# Falsi positivi comuni (monitor per bebe', per pressione ecc.)
EXCLUDE_RE = re.compile(
    r"beb[eé]|\bbaby\b|b[eé]b[eé]|tensi[oó]n\s+arterial|presi[oó]n\s+arterial|gluc[oó]|"
    r"tension\s+art[eé]rielle|pressione|\bgatos?\b|\bperros?\b|"
    r"\bmini\s?pc\b|gaming\s+pc\b|pc\s+(?:desktop|fisso|gaming\s+completo)|desktop\s+pc|barebone|bosgame|chuwi|minisforum|beelink|geekom|\bwatch\b|ciotola|animali|\bcane\b|\bgatto\b|\btablet\b|smartphone|cellulare|\biphone\b|\bkindle\b|\bfire\s?tv\b|smart\s?watch|controller\s+(?:di\s+gioco\s+)?(?:mobile|smartphone|per\s+(?:smartphone|telefono))|geforce\s+now|pc\s+portable|port[aá]til|portatile|\blaptop\b|\bnotebook\b|\bzenbook\b|\bmacbook\b|"
    r"\bfunda\b|housse|cover\s+per|custodia", re.I)

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


FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
FLAG_MAP = {"\U0001F1EE\U0001F1F9": "it", "\U0001F1EA\U0001F1F8": "es", "\U0001F1EB\U0001F1F7": "fr",
            "\U0001F1E9\U0001F1EA": "de", "\U0001F1EC\U0001F1E7": "uk", "\U0001F1FA\U0001F1F8": "us"}
DOMAIN_COUNTRY = {"amazon.it": "it", "amazon.es": "es", "amazon.fr": "fr", "amazon.de": "de", "amazon.co.uk": "uk"}
INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060-\u2064\ufeff\ufe0f]")
AMAZON_LINK_RE = re.compile(r"amzlink|link\.amazon|amzn\.|amazon\.(?:it|es|fr|de|co\.uk|com)")
EURO = r"(\d[\d.,]*)\s?\u20ac"
TG_FINAL_PRICE_RE = re.compile(r"\u27a1\ufe0f?\s*" + EURO)                       # prezzo finale dopo coupon
TG_PAIR_RE = re.compile(EURO + r"\s+invece\s+di\s+" + EURO, re.I)             # "53,18 EUR invece di 79,99 EUR"
TG_PRICE_RE = re.compile(r"Prezzo:\s*" + EURO, re.I)
TG_STATE_RE = re.compile(r"(?:usato|nuovo)\s+(?:a\s+)?" + EURO, re.I)          # "Disponibile usato a 707,45 EUR"
TG_ANY_EURO_RE = re.compile(EURO)


def _is_marker(line):
    """Riga fatta solo di emoji/simboli (separatori dei messaggi dei canali)."""
    clean = re.sub(r"\s", "", INVISIBLE_RE.sub("", line))
    return bool(clean) and not re.search(r"[A-Za-z0-9]", clean) and clean not in ("\u2122", "\u00ae")


def _is_banner(text):
    """Intestazione in maiuscolo senza numeri, max 3 parole (es. "PREZZONE", "MINIMO STORICO")."""
    t = text.strip()
    if re.fullmatch(r"(?:[A-Z]\s){2,}[A-Z]", t):   # lettere spaziate: "A F F A R E"
        return True
    return bool(t) and len(t.split()) <= 3 and t == t.upper() and not re.search(r"\d", t)


def _tg_split(lines):
    """Divide il messaggio in (titolo, righe_successive). Il titolo e' il primo blocco di testo che non e' un banner."""
    blocks, cur, end_idx = [], [], 0
    for i, ln in enumerate(lines):
        if _is_marker(ln) or ln.startswith("http"):
            if cur:
                blocks.append((" ".join(cur), i)); cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append((" ".join(cur), len(lines)))
    for k, (txt, end_idx) in enumerate(blocks):
        nxt = blocks[k + 1][0] if k + 1 < len(blocks) else ""
        looks_like_title = len(nxt) >= 6 and not re.search(r"\u20ac|http|Prezzo", nxt)
        if _is_banner(txt) and looks_like_title:
            continue
        title = re.sub(r"\s+", " ", txt).replace("\u2122", "").replace("\u00ae", "").strip()
        return title, lines[end_idx:]
    return "", []


def fetch_tg(feed):
    """Legge l'anteprima web pubblica di un canale Telegram (https://t.me/s/<canale>)."""
    r = requests.get(f"https://t.me/s/{feed['channel']}", headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    pass_all = SEND_ALL == "all" or (SEND_ALL == "hw" and bool(feed.get("hw")))
    for m in soup.select("div.tgme_widget_message"):
        text_el = m.select_one(".tgme_widget_message_text")
        post = m.get("data-post")
        if not text_el or not post:
            continue
        lines = [INVISIBLE_RE.sub("", ln).strip() for ln in text_el.get_text("\n", strip=True).split("\n")]
        lines = [ln for ln in lines if ln]
        full = " ".join(lines)
        if len(re.findall(r"Prezzo:", full)) >= 2 and not pass_all:
            continue   # messaggio con piu' prodotti: non assegnabile a un solo prezzo
        title, rest = _tg_split(lines)
        rest_txt = " ".join(rest)
        if len(title) < 6:
            continue
        hrefs = [a.get("href", "") for a in m.select("a[href]")]
        shop = next((u for u in hrefs if AMAZON_LINK_RE.search(u)), None)
        post_url = f"https://t.me/{post}"
        link = shop or (post_url if re.search(r"acquista su amazon", full, re.I) else None)
        if not link:
            continue   # nessun link di acquisto (video, promemoria, ecc.)
        mp = (TG_FINAL_PRICE_RE.search(rest_txt) or TG_PAIR_RE.search(rest_txt) or TG_PRICE_RE.search(rest_txt)
              or TG_STATE_RE.search(rest_txt) or TG_ANY_EURO_RE.search(rest_txt))
        price_txt = (mp.group(1) + "\u20ac") if mp else None
        flag = FLAG_RE.search(full)
        country = FLAG_MAP.get(flag.group(0)) if flag else None
        if not country and shop:
            country = next((c for dom, c in DOMAIN_COUNTRY.items() if dom in shop), None)
        if country and ALLOWED_COUNTRIES and country not in ALLOWED_COUNTRIES and not pass_all:
            continue   # marketplace non richiesto (es. Amazon.de)
        used = bool(re.search(r"\busato\b|seconda mano", full, re.I)) and not re.search(r"nuovo a\s", full, re.I)
        if used and EXCLUDE_USED and not pass_all:
            continue
        is_amazon = bool(shop) or "amazon" in full.lower()
        merchant = ("Amazon" + (" (usato)" if used else "")) if is_amazon else ("AliExpress" if "aliexpress" in " ".join(hrefs) else "")
        items.append({
            "guid": post, "title": title, "link": link, "post": post_url,
            "feed_category": "", "merchant": merchant, "price_txt": price_txt,
            "price": parse_price(price_txt), "desc": full, "source": feed["name"],
            "curated": bool(feed.get("curated")), "pass_all": pass_all,
            "ts": (m.select_one("time").get("datetime") if m.select_one("time") else ""),
        })
    items.reverse()   # dal piu' recente al piu' vecchio, come i feed RSS
    return items


def fetch_items(feed):
    if feed.get("type") == "tgchannel":
        return fetch_tg(feed)
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


SENT = []   # offerte gia' inviate: [timestamp, prezzo, categoria, condizione, model_key, token]


def _tokens(title):
    return {t for t in re.findall(r"[a-z0-9]{3,}", title.lower()) if not GENERIC_TOKEN_RE.match(t)}


def _condition(item):
    return "u" if "(usato)" in item["merchant"] else "n"


def is_duplicate(item, cat):
    """Stessa offerta gia' inviata (anche da un'altra fonte) nelle ultime DEDUPE_HOURS ore, salvo ulteriore ribasso."""
    if item.get("pass_all"):
        return False   # modalita' "invia tutto": nessuna deduplicazione
    now, price, cond = time.time(), item["price"], _condition(item)
    mk, tk = model_key(item["title"], cat), _tokens(item["title"])
    for ts, p, c, cd, k, toks in SENT:
        if now - ts > DEDUPE_HOURS * 3600 or c != cat or cd != cond:
            continue
        close_price = bool(price and p) and abs(price - p) <= 0.01 * p
        overlap = len(tk & set(toks)) / max(1, min(len(tk), len(toks)))
        if (bool(mk) and mk == k) or (close_price and overlap >= 0.4):
            if price and p and price <= p * (1 - REPRICE_DROP / 100):
                continue   # ulteriore ribasso: non e' un duplicato
            return True
    return False


def mark_sent(item, cat):
    SENT.append([time.time(), item["price"] or 0, cat, _condition(item),
                 model_key(item["title"], cat), sorted(_tokens(item["title"]))])
    del SENT[:-400]


# Prezzi minimi plausibili per prodotti NUOVI di una categoria. Sotto soglia = possibile errore di prezzo.
# Controllo solo con modelli riconoscibili nel titolo, per evitare accessori (es. "supporto GPU").
PRICE_FLOORS = [
    ("Scheda video", 120, re.compile(r"\b(?:rtx|gtx)\s?\d{3,4}|\brx\s?\d{4}|radeon\s+rx|geforce", re.I)),
    ("Scheda madre", 40, re.compile(r"scheda\s+madre|placa\s+base|carte\s+m[eè]re|motherboard", re.I)),
    ("CPU", 45, re.compile(r"\bryzen\s+\d|core\s+(?:ultra\s+)?i[3579]|core\s+ultra\s+\d", re.I)),
    ("Monitor", 45, re.compile(r"\bmonitor\b|\becran\b|schermo", re.I)),
    ("Alimentatore", 15, re.compile(r"alimentatore|fuente\s+de\s+alimentaci|bloc\s+d.alimentation", re.I)),
]


def below_floor(item, cat):
    if not item["price"] or "(usato)" in item["merchant"]:
        return None
    for c, floor, rx in PRICE_FLOORS:
        if c == cat and item["price"] < floor and rx.search(item["title"]):
            return floor
    return None


def evaluate(item):
    """Ritorna None oppure dict con la decisione di invio."""
    pass_all = bool(item.get("pass_all"))
    cat = classify(item["title"]) or ("Altro" if pass_all else None)
    if not cat or not (pass_all or merchant_allowed(item["merchant"])):
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
    # Sconto molto alto rispetto a un listino DICHIARATO dalla fonte: e' un errore solo se lo storico lo conferma
    # (i listini possono essere gonfiati); senza listino dichiarato resta il segnale di possibile errore.
    floor = below_floor(item, cat)
    # Per i prodotti non PC ("Altro") un forte sconto dichiarato non basta a parlare di errore: resta "sconto".
    is_error = keyword_error or bool(floor) or (cat != "Altro" and discount >= ERROR_DISCOUNT and (not orig or below >= PRICE_DROP))
    is_anomaly = below >= PRICE_DROP
    is_deal = discount >= MIN_DISCOUNT
    is_curated = bool(item.get("curated")) and (CURATED_SEND or bool(item.get("pass_all")))
    if not (is_error or is_anomaly or is_deal or is_curated or SEND_UNKNOWN):
        return None
    kind = ("error" if is_error else "anomaly" if is_anomaly else "deal" if is_deal
            else "curated" if is_curated else "unknown")
    return {"category": cat, "discount": discount, "orig": orig, "kind": kind,
            "base": base, "base_note": base_note, "below": below,
            "confidence": conf, "lowest": lowest, "floor": floor, "keyword": keyword_error}

# ----------------------------------------------------------------------------
# MESSAGGI E NOTIFICHE
# ----------------------------------------------------------------------------
def build_message(item, d):
    head = {"error": "🚨 POSSIBILE ERRORE DI PREZZO",
            "anomaly": f"⚠️ PREZZO ANOMALO (-{d['below']:.0f}% sotto il riferimento)",
            "deal": f"🔥 SCONTO -{d['discount']:.0f}%",
            "curated": f"📣 OFFERTA SEGNALATA ({item['source']})",
            "unknown": "🛒 NUOVA OFFERTA"}[d["kind"]]
    lines = [f"{head}" + ("" if d["category"] == "Altro" else f" [{d['category']}]"), item["title"]]
    price = item["price_txt"] or "prezzo n/d"
    if d["orig"]:
        lines.append(f"Prezzo: {price} (prima {d['orig']:.2f} EUR, circa -{d['discount']:.0f}%)")
    elif d["discount"]:
        lines.append(f"Prezzo: {price} (sconto indicato circa -{d['discount']:.0f}%)")
    else:
        lines.append(f"Prezzo: {price}")
    if d["base"]:
        lines.append(f"Riferimento: {d['base']:.2f} EUR ({d['base_note']})")
    if d.get("floor"):
        lines.append(f"Prezzo sotto la soglia minima plausibile per la categoria ({d['floor']} EUR): possibile errore di prezzo.")
    if d.get("keyword") and not d.get("floor"):
        lines.append("Il canale la segnala come errore di prezzo (non verificato).")
    if d.get("lowest"):
        lines.append("La fonte indica un minimo storico.")
    lines.append(f"Affidabilita' della valutazione: {d.get('confidence', 'bassa')} (non e' una conferma dell'errore)")
    lines.append(f"Negozio: {item['merchant'] or 'n/d'} | Fonte: {item['source']}")
    lines.append(item["link"])
    if item.get("post"):
        lines.append(f"Segnalazione originale: {item['post']}")
    lines.append("Verifica la spedizione in Italia prima di acquistare.")
    return "\n".join(lines)


def send_discord(text):
    hook = env("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    for attempt in range(4):
        r = requests.post(hook, json={"content": text[:1990]}, timeout=15)
        if r.status_code == 429:   # limite di Discord: attendi quanto richiesto e riprova
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            print(f"  Discord 429: attendo {wait:.1f}s")
            time.sleep(min(wait, 20) + 0.5)
            continue
        if r.status_code >= 300:
            print(f"  Discord HTTP {r.status_code}")
        return


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
    time.sleep(SEND_DELAY)  # rispetta i rate limit dei canali

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
    SENT[:] = [e for e in data.get("sent", []) if isinstance(e, list) and len(e) == 6]
    return data.get("seen", [])


def save_state(seen):
    prices = dict(list(PRICE_HISTORY.items())[-3000:])
    STATE_FILE.write_text(json.dumps({"seen": seen[-MAX_SEEN:], "prices": prices, "sent": SENT[-400:]}))

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
        sent_now, processed = 0, []
        for it in reversed(new_items):  # dalla piu' vecchia alla piu' recente
            if sent_now >= MAX_PER_RUN:
                break   # il resto resta "non visto" e viene inviato al giro successivo
            d = evaluate(it)
            if d and is_duplicate(it, d["category"]):
                print(f"  duplicato saltato: {it['title'][:70]}")
                d = None
            elif d:
                print(f"  INVIO [{d['kind']}] {it['title'][:80]}")
                notify(build_message(it, d))
                mark_sent(it, d["category"])
                sent_now += 1
            if not d or d["kind"] not in ("error", "anomaly"):
                record_price(it)   # i prezzi anomali non entrano nello storico
            processed.append(it["guid"])
        if sent_now >= MAX_PER_RUN:
            print(f"Limite di {MAX_PER_RUN} messaggi raggiunto: i rimanenti vanno al prossimo giro.")
        new_items = [it for it in new_items if it["guid"] in set(processed)]
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
