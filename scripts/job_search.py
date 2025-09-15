# scripts/job_search.py — v1.9.1 (HTML only)
# Sources conservées: Adecco, Saisonnier.fr, L'Hôtellerie-Restauration, Jobagri
# Règles: logement requis, pas de diplôme obligatoire, expérience ≤ 1 an,
# métiers ciblés, priorité aux offres sans permis, dédup avancée

import os
import smtplib
import ssl
import math
import time
import re
import random
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -------------------- Config --------------------
ORIGIN_CITY = os.getenv("ORIGIN_CITY", "Clermont-Ferrand, France")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))
HOUSING_REQUIRED = os.getenv("HOUSING_REQUIRED", "true").lower() == "true"
FALLBACK_CONTACTS = os.getenv("FALLBACK_CONTACTS", "true").lower() == "true"
FRANCE_WIDE = os.getenv("FRANCE_WIDE", "true").lower() == "true"

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
RECIPIENTS = [e.strip() for e in os.getenv("RECIPIENTS", "").split(",") if e.strip()]

NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "example@example.com")
PROXY = os.getenv("PROXY")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
]
BASE_HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9", "Cache-Control": "no-cache"}

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# -------------------- HTTP Session --------------------
SESSION = requests.Session()
retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

def safe_get(url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    headers = dict(BASE_HEADERS)
    headers["User-Agent"] = random.choice(UA_POOL)
    r = SESSION.get(url, params=params, headers=headers, proxies=PROXIES, timeout=25)
    r.raise_for_status()
    return r

# -------------------- Geocoding --------------------
def geocoder():
    geolocator = Nominatim(user_agent=f"jobsearch-bot-{NOMINATIM_EMAIL}")
    return geolocator, RateLimiter(geolocator.geocode, min_delay_seconds=1, swallow_exceptions=True)

GEOCODE_CACHE: Dict[str, Tuple[float, float]] = {}
def geocode(text: Optional[str], geo, rate) -> Optional[Tuple[float, float]]:
    if not text:
        return None
    key = text.strip().lower()
    if key in GEOCODE_CACHE:
        return GEOCODE_CACHE[key]
    try:
        loc = rate(text + ", France")
        if loc:
            coords = (loc.latitude, loc.longitude)
            GEOCODE_CACHE[key] = coords
            return coords
    except Exception:
        return None
    return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.asin(math.sqrt(a))

# -------------------- Métier ciblé --------------------
SERVICE_BAR_POLY = [
    "serveur", "serveuse", "chef de rang", "runner",
    "bar", "barman", "barmaid",
    "polyvalent", "polyvalente", "employé polyvalent", "employée polyvalente",
    "réceptionniste", "accueil", "accueillant", "accueillante", "hôtesse", "hôte"
]
VENTE_OBJETS = [
    "vendeur", "vendeuse", "conseiller de vente", "conseillère de vente",
    "prêt-à-porter", "pret-a-porter", "mode", "boutique", "chaussures", "sport", "magasin",
    "papeterie", "maison", "décoration", "electronique", "high-tech", "jouet", "bricolage"
]
RAYON_MERCH = [
    "mise en rayon", "remise en rayon", "els", "employé libre-service", "employée libre-service",
    "merchandising", "réassort", "reassort", "facing", "inventaire", "magasinier"
]
CUISINE_HARD = [
    "cuisinier", "cuisinière", "cuisine", "commis de cuisine", "commis cuisine",
    "chef de partie", "plongeur batterie", "préparateur culinaire", "snack", "pizzaiolo"
]
ALIMENTAIRE_HARD = [
    "boucher", "bouchère", "boucherie", "charcutier", "charcuterie",
    "poissonnier", "poissonnerie", "fromager", "fromagerie",
    "boulanger", "boulangerie", "pâtissier", "pâtisserie", "patissier", "patisserie",
    "primeur", "traiteur", "restauration rapide", "sandwicherie"
]
HYPER_CHAINS = [
    "carrefour", "auchan", "leclerc", "intermarché", "lidl", "aldi",
    "monoprix", "casino", "super u", "géant", "cora", "match", "spar"
]
CLEANING_SOFT = [
    "agent d'entretien", "agent de nettoyage", "entretien", "nettoyage",
    "femme de chambre", "valet de chambre", "gouvernante", "laveur", "laveuse"
]
PLONGE_SOFT = ["plonge", "plongeur", "plongeuse"]

def _has_any(text: str, words: List[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)

def role_allowed(text: str) -> bool:
    t = text.lower()
    if _has_any(t, CUISINE_HARD) or _has_any(t, ALIMENTAIRE_HARD) or _has_any(t, HYPER_CHAINS):
        return False
    return (
        _has_any(t, SERVICE_BAR_POLY) or
        _has_any(t, VENTE_OBJETS) or
        _has_any(t, RAYON_MERCH)
    )

# -------------------- Logement requis --------------------
HOUSING_KEYS = ["logé", "loge", "logement fourni", "hébergement"]
def matches_housing(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in HOUSING_KEYS)

# -------------------- Filtrage expérience --------------------
RE_YEARS = re.compile(r"(\d+)\s*(?:an|ans)\s+d[' ]?exp", re.I)
def experience_ok(text: str) -> bool:
    t = text.lower()
    if "débutant accepté" in t or "sans expérience" in t:
        return True
    m_years = RE_YEARS.search(t)
    if m_years:
        return int(m_years.group(1)) <= 1
    return True

def mentions_permit(text: str) -> bool:
    return "permi" in text.lower()

# -------------------- Scrapers HTML --------------------
def fetch_saisonnier_fr() -> List[Dict[str, Any]]:
    base = "https://www.saisonnier.fr"
    url = base + "/emplois"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job-card, li, .job, .search-item"):
        a = card.select_one("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        link = urllib.parse.urljoin(base, a.get("href", ""))
        meta = card.get_text(" ", strip=True)
        offers.append({
            "title": title,
            "employer": "",
            "city": "",
            "link": link,
            "raw": meta
        })
    return offers

def fetch_adecco() -> List[Dict[str, Any]]:
    base = "https://www.adecco.fr"
    url = base + "/resultats-offres-emploi/"
    params = {"k": "saisonnier logé"} if FRANCE_WIDE else {"k": "saisonnier logé", "l": "Clermont-Ferrand"}
    r = safe_get(url, params=params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .result-item, li, .job-tile, .offer-card"):
        a = card.select_one("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        link = urllib.parse.urljoin(base, a.get("href", ""))
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title,
            "employer": "Adecco",
            "city": "",
            "link": link,
            "raw": desc
        })
    return offers

HTML_PROVIDERS = [
    fetch_saisonnier_fr,
    fetch_adecco
]

# -------------------- Collecte --------------------
def collect_offers() -> List[Dict[str, Any]]:
    items = []
    for provider in HTML_PROVIDERS:
        try:
            batch = provider()
            items.extend(batch)
            print(f"[PROVIDER] {provider.__name__}: {len(batch)} offres")
        except Exception as e:
            print(f"[WARN] Provider {provider.__name__} failed: {e}")
    print(f"[INFO] Total offres collectées (brut): {len(items)}")
    return items

# -------------------- Filtrage --------------------
def enrich_and_filter(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for it in items:
        text = it["raw"].lower()
        if HOUSING_REQUIRED and not matches_housing(text):
            continue
        if not experience_ok(text):
            continue
        if not role_allowed(text):
            continue
        filtered.append(it)

    print(f"[INFO] Offres après filtrage: {len(filtered)} / {len(items)}")
    return filtered

# -------------------- Email --------------------
def make_email(offers: List[Dict[str, Any]]):
    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"[{len(offers)}] Offres saisonnières - {today}"
    text_lines = []
    html_lines = []
    for i, o in enumerate(offers, 1):
        text_lines.append(f"{i}. {o['title']} - {o['link']}")
        html_lines.append(f"<li><a href='{o['link']}'>{o['title']}</a></li>")
    text = "\n".join(text_lines)
    html = "<ul>" + "".join(html_lines) + "</ul>"
    return subject, text, html

def send_email(subject, text, html):
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM, RECIPIENTS, msg.as_string())

# -------------------- Main --------------------
def main():
    all_offers = collect_offers()
    filtered = enrich_and_filter(all_offers)
    subject, text, html = make_email(filtered)
    send_email(subject, text, html)
    print(f"Sent {len(filtered)} offers to: {', '.join(RECIPIENTS)}")

if __name__ == "__main__":
    main()