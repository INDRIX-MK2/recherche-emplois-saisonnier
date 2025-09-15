# scripts/job_search.py — v1.5
# Fix: sanitisation des liens (javascript:void(0), ';', relatifs, onclick)
# + extraction data-href/data-url/onclick pour Adecco & co
# + skip fetch détail si URL non-http
# + garde filtres v1.4: no permis, no formation obligatoire, expérience ≤ 1 an, logement requis, secteurs
import os
import smtplib
import ssl
import math
import time
import re
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

# -------- Config from env --------
ORIGIN_CITY = os.getenv("ORIGIN_CITY", "Clermont-Ferrand, France")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))  # seuil minimum visé (on ne tronque pas)
SECTORS = [s.strip().lower() for s in os.getenv("SECTORS", "hotellerie-restauration,agri-vendanges").split(",")]
HOUSING_REQUIRED = os.getenv("HOUSING_REQUIRED", "true").lower() == "true"
FALLBACK_CONTACTS = os.getenv("FALLBACK_CONTACTS", "true").lower() == "true"

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
RECIPIENTS = [e.strip() for e in os.getenv("RECIPIENTS", "").split(",") if e.strip()]

NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "example@example.com")
PROXY = os.getenv("PROXY")  # optionnel

HEADERS = {
    "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  f"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9"
}
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# -------- HTTP session with retries --------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

def safe_get(url: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    r = SESSION.get(url, params=params, proxies=PROXIES, timeout=25)
    r.raise_for_status()
    return r

# -------- Geocoding helpers (with cache) --------
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

# -------- Filters & parsing --------
HOUSING_KEYS = [
    "logé", "loge", "logement fourni", "logement inclus", "logement possible",
    "nourri", "logé nourri", "loge nourri", "hébergement fourni", "hébergement", "logement sur place"
]
SECTOR_KEYS = {
    "hotellerie-restauration": ["serveur", "serveuse", "commis", "plonge", "réceptionniste", "barman", "barmaid", "chef de rang", "hôtel", "restaurant"],
    "agri-vendanges": ["vendange", "vendanges", "cueillette", "viticole", "viticulture", "agricole", "maraîchage", "saisonnier agricole", "exploitation agricole"]
}

TRAINING_KEYWORDS = [
    "diplôme", "diplome", "cap", "bep", "bac", "bts", "dut", "licence", "master", "bac+",
    "certificat", "certification", "caces", "ssiap", "haccp", "h a c c p", "titre professionnel"
]
TRAINING_REQUIRE_WORDS = ["exig", "requis", "obligatoire", "indispensable", "nécessaire"]

RE_YEARS = re.compile(r"(\d+)\s*(?:an|ans)\s+d[' ]?exp[ée]rience", re.I)
RE_RANGE = re.compile(r"(\d+)\s*(?:à|-|–|—)\s*(\d+)\s*(?:an|ans)\s+d[' ]?exp[ée]rience", re.I)
RE_MIN = re.compile(r"(?:au moins|minimum|min\.?)\s*(\d+)\s*(?:an|ans)", re.I)

def matches_housing(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in HOUSING_KEYS)

def matches_sectors(text: str) -> bool:
    t = text.lower()
    keys: List[str] = []
    if "hotellerie-restauration" in SECTORS:
        keys += SECTOR_KEYS["hotellerie-restauration"]
    if "agri-vendanges" in SECTORS:
        keys += SECTOR_KEYS["agri-vendanges"]
    return any(k in t for k in keys)

def looks_recent(text: str) -> bool:
    t = text.lower()
    today = datetime.now().strftime("%d/%m/%Y")
    hints = ["aujourd’hui", "aujourd'hui", "today", "il y a ", "nouvelle offre", today]
    return any(h in t for h in hints)

def pick_city_from_text(text: str) -> str:
    m = re.search(r"([A-ZÉÈÎÏÔÂÇa-zÀ-ÿ' -]+)\s*\((\d{2,3})\)", text)
    if m:
        return f"{m.group(1).strip()} ({m.group(2)})"
    m2 = re.search(r"\b(?:à|sur|près de|proche de)\s+([A-ZÉÈÎÏÔÂÇa-zÀ-ÿ' -]{3,40})", text)
    if m2:
        return m2.group(1).strip()
    return ""

# ---- New filters: permis / formation / experience ----
def mentions_permit(text: str) -> bool:
    t = text.lower()
    return ("permi" in t)  # capture "permis", "permis b", etc.

def requires_training(text: str) -> bool:
    t = text.lower()
    has_training = any(k in t for k in TRAINING_KEYWORDS)
    has_requirement = any(w in t for w in TRAINING_REQUIRE_WORDS)
    return has_training and has_requirement

def experience_ok(text: str) -> bool:
    t = text.lower()
    if "débutant accepté" in t or "debutant accepté" in t or "sans expérience" in t or "sans experience" in t:
        return True
    if ("expérience exigée" in t or "experience exigée" in t or
        "expérience requise" in t or "experience requise" in t or
        "expérience indispensable" in t or "experience indispensable" in t or
        "expérience obligatoire" in t or "experience obligatoire" in t or
        "expérience significative" in t or "experience significative" in t):
        return False
    m_range = RE_RANGE.search(t)
    if m_range:
        x = int(m_range.group(1)); y = int(m_range.group(2))
        return max(x, y) <= 1
    m_min = RE_MIN.search(t)
    if m_min:
        x = int(m_min.group(1)); return x <= 1
    m_years = RE_YEARS.search(t)
    if m_years:
        x = int(m_years.group(1)); return x <= 1
    if "première expérience" in t or "premiere experience" in t or "1ère expérience" in t:
        return True
    return True  # pas d'info => on autorise

# -------- URL sanitization helpers --------
URL_EXTRACT_RE = re.compile(r"(https?://[^\s'\"<>)]+)", re.I)

def is_http_url(u: Optional[str]) -> bool:
    return bool(u) and (u.startswith("http://") or u.startswith("https://"))

def sanitize_link(base: str, raw: Optional[str]) -> str:
    if not raw:
        return ""
    href = raw.strip().strip(";").strip()
    if not href or href == "#" or href.lower().startswith("javascript:"):
        return ""
    # protocol-relative
    if href.startswith("//"):
        href = "https:" + href
    # relative
    if not href.startswith("http"):
        return urllib.parse.urljoin(base, href)
    return href

def extract_url_from_onclick(onclick_val: Optional[str]) -> str:
    if not onclick_val:
        return ""
    m = URL_EXTRACT_RE.search(onclick_val)
    return m.group(1) if m else ""

def extract_best_link(elem: BeautifulSoup, base: str) -> str:
    # Essayer plusieurs attributs
    for attr in ("href", "data-href", "data-url", "data-link"):
        raw = elem.get(attr)
        url = sanitize_link(base, raw)
        if is_http_url(url):
            return url
    # Essayer un <a> enfant
    a = elem.find("a", href=True)
    if a:
        url = sanitize_link(base, a.get("href"))
        if is_http_url(url):
            return url
    # Chercher dans onclick
    onclick_raw = elem.get("onclick") or (a.get("onclick") if a else None)
    if onclick_raw:
        candidate = extract_url_from_onclick(onclick_raw)
        url = sanitize_link(base, candidate)
        if is_http_url(url):
            return url
    return ""

# -------- Contact extraction --------
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+33\s?|0)(?:\s|\.|-)?)?[1-9](?:[\s\.\-]?\d{2}){4}")

def normalize_phone(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s

def extract_contacts_from_html(html: str) -> Tuple[Optional[str], Optional[str]]:
    phone = None
    email = None
    soup = BeautifulSoup(html, "lxml")
    a_mail = soup.select_one("a[href^='mailto:']")
    if a_mail:
        mail_href = a_mail.get("href", "")
        m = re.search(EMAIL_RE, mail_href)
        if m:
            email = m.group(0)
    a_tel = soup.select_one("a[href^='tel:']")
    if a_tel:
        tel_href = a_tel.get("href", "")
        m = re.search(PHONE_RE, tel_href)
        if m:
            phone = normalize_phone(m.group(0))
    if not email:
        m = EMAIL_RE.search(html)
        if m:
            email = m.group(0)
    if not phone:
        m = PHONE_RE.search(html)
        if m:
            phone = normalize_phone(m.group(0))
    return phone, email

def fetch_contact_details(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        if not is_http_url(url):
            return None, None
        r = safe_get(url)
        phone, email = extract_contacts_from_html(r.text)
        time.sleep(0.4)
        return phone, email
    except Exception as e:
        print(f"[INFO] No direct contacts extracted from {url}: {e}")
        return None, None

# -------- Fallback annuaires publics --------
def pj_phone_lookup(employer: str, city_hint: str) -> Optional[str]:
    if not employer:
        return None
    q = f"{employer} {city_hint}".strip()
    url = "https://www.pagesjaunes.fr/recherche"
    params = {"quoiqui": q}
    try:
        r = safe_get(url, params=params)
        soup = BeautifulSoup(r.text, "lxml")
        for tel in soup.select("a.tel, span.number, div.bi-bloc-contact a"):
            text = tel.get_text(" ", strip=True)
            m = PHONE_RE.search(text)
            if m:
                return normalize_phone(m.group(0))
        m = PHONE_RE.search(soup.get_text(" ", strip=True))
        if m:
            return normalize_phone(m.group(0))
    except Exception as e:
        print(f"[INFO] PagesJaunes lookup failed: {e}")
    return None

def ae_phone_lookup(employer: str, city_hint: str) -> Optional[str]:
    if not employer:
        return None
    q = urllib.parse.quote_plus(f"{employer} {city_hint}".strip())
    url = f"https://annuaire-entreprises.data.gouv.fr/rechercher?q={q}"
    try:
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        first = soup.select_one("a[href*='/entreprise/']")
        if first:
            href = first.get("href", "")
            if href and href.startswith("/entreprise/"):
                fiche = safe_get("https://annuaire-entreprises.data.gouv.fr" + href)
                m = PHONE_RE.search(fiche.text)
                if m:
                    return normalize_phone(m.group(0))
    except Exception as e:
        print(f"[INFO] Annuaire-Entreprises lookup failed: {e}")
    return None

def fallback_contacts(employer: str, city: str) -> Tuple[Optional[str], Optional[str]]:
    if not FALLBACK_CONTACTS or not employer:
        return None, None
    phone = pj_phone_lookup(employer, city) or ae_phone_lookup(employer, city)
    return phone, None

# -------- Utility: fetch page detail text --------
def fetch_page_text(url: str) -> str:
    try:
        if not is_http_url(url):
            return ""
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        return soup.get_text(" ", strip=True)
    except Exception as e:
        print(f"[INFO] fetch_page_text failed for {url}: {e}")
        return ""

# -------- Providers --------
def fetch_france_travail() -> List[Dict[str, Any]]:
    base = "https://candidat.francetravail.fr"
    url = base + "/offres/recherche"
    params = {"motsCles": "saisonnier logé nourri", "lieu": "Clermont-Ferrand (63)", "rayon": "200"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("[data-id-offre]"):
        title = card.select_one("h3")
        employer = card.select_one(".t4.color-dark-blue")
        city = card.select_one(".subtext")
        # lien via data-href ou <a>
        raw_link = card.get("data-href") or (card.find("a", href=True).get("href", "") if card.find("a", href=True) else "")
        link = sanitize_link(base, raw_link)
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title.get_text(strip=True) if title else "Offre",
            "employer": employer.get_text(strip=True) if employer else "",
            "city": (city.get_text(strip=True) if city else "") or pick_city_from_text(desc),
            "link": link,
            "raw": desc
        })
    return offers

def fetch_indeed() -> List[Dict[str, Any]]:
    base = "https://fr.indeed.com"
    url = base + "/jobs"
    params = {
        "q": "saisonnier (logé OR logement OR loge) (serveur OR serveuse OR vendange OR cueillette OR plonge)",
        "l": "Clermont-Ferrand (63)",
        "radius": "200"
    }
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("div.job_seen_beacon"):
        title_el = card.select_one("h2 a")
        title = title_el.get_text(strip=True) if title_el else "Offre Indeed"
        raw_link = title_el.get("href", "") if title_el else ""
        link = sanitize_link(base, raw_link)
        company = card.select_one("span.companyName")
        city = card.select_one("div.companyLocation")
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title,
            "employer": company.get_text(strip=True) if company else "",
            "city": (city.get_text(strip=True) if city else "") or pick_city_from_text(desc),
            "link": link,
            "raw": desc
        })
    return offers

def fetch_vitijob() -> List[Dict[str, Any]]:
    base = "https://www.vitijob.com"
    url = base + "/fr/recherche"
    params = {"q": "vendanges logé", "l": "Auvergne"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select(".job-offer-card, .search-item, article"):
        title_el = card.select_one("a")
        title = title_el.get_text(strip=True) if title_el else "Offre Vitijob"
        raw_link = title_el.get("href", "") if title_el else ""
        link = sanitize_link(base, raw_link)
        employer = card.select_one(".company, .entreprise, .company-name")
        city = card.select_one(".location, .lieu")
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title,
            "employer": employer.get_text(strip=True) if employer else "",
            "city": (city.get_text(strip=True) if city else "") or pick_city_from_text(desc),
            "link": link,
            "raw": desc
        })
    return offers

def fetch_saisonnier_fr() -> List[Dict[str, Any]]:
    base = "https://www.saisonnier.fr"
    url = base + "/emplois"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job-card, li, .job, .search-item"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        raw_link = title_el.get("href", "")
        link = sanitize_link(base, raw_link)
        meta = card.get_text(" ", strip=True)
        city = ""
        loc = card.select_one(".job-location, .location, .lieu")
        if loc:
            city = loc.get_text(strip=True)
        offers.append({
            "title": title or "Offre Saisonnier.fr",
            "employer": "",
            "city": city or pick_city_from_text(meta),
            "link": link,
            "raw": meta
        })
    return offers

def fetch_lhotellerie() -> List[Dict[str, Any]]:
    base = "https://www.lhotellerie-restauration.fr"
    url = base + "/emploi/"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .offre, .annonce"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        raw_link = title_el.get("href", "")
        link = sanitize_link(base, raw_link)
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Hôtellerie-Restauration",
            "employer": "",
            "city": city,
            "link": link,
            "raw": desc
        })
    return offers

def fetch_anefa_jobsagri() -> List[Dict[str, Any]]:
    base = "https://www.jobagri.com"
    url = base + "/offres"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job, .offre"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        raw_link = title_el.get("href", "")
        link = sanitize_link(base, raw_link)
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre agricole",
            "employer": "",
            "city": city,
            "link": link,
            "raw": desc
        })
    return offers

def fetch_adecco() -> List[Dict[str, Any]]:
    base = "https://www.adecco.fr"
    url = base + "/resultats-offres-emploi/"
    params = {"k": "saisonnier logé", "l": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    cards = soup.select("article, .result-item, li, .job-tile, .offer-card")
    if not cards:
        cards = soup.select("article, .result-item, li")
    for card in cards:
        # Titre
        a = card.select_one("a")
        title = a.get_text(strip=True) if a else (card.get_text(" ", strip=True)[:60] or "Offre Adecco")
        # Lien: essaye multiples sources
        link = extract_best_link(card, base)
        if not link and a:
            link = sanitize_link(base, a.get("href", ""))
        # Sanity check: ignorer javascript:void(0)
        if not is_http_url(link):
            continue

        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Adecco",
            "employer": "Adecco",
            "city": city,
            "link": link,
            "raw": desc
        })
    return offers

def fetch_manpower() -> List[Dict[str, Any]]:
    base = "https://www.manpower.fr"
    url = base + "/Offres"
    params = {"Keywords": "saisonnier logement", "Location": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .search-result, li"):
        a = card.select_one("a")
        title = a.get_text(strip=True) if a else "Offre Manpower"
        raw_link = (a.get("href", "") if a else "") or card.get("data-href") or card.get("data-url")
        link = sanitize_link(base, raw_link)
        if not is_http_url(link):
            continue
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title,
            "employer": "Manpower",
            "city": city,
            "link": link,
            "raw": desc
        })
    return offers

def fetch_randstad() -> List[Dict[str, Any]]:
    base = "https://www.randstad.fr"
    # Certaines pages utilisent k / l comme params
    url = base + "/offres/"
    params = {"k": "saisonnier logement", "l": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .result-list__item, li"):
        a = card.select_one("a")
        title = a.get_text(strip=True) if a else "Offre Randstad"
        raw_link = (a.get("href", "") if a else "") or card.get("data-href") or card.get("data-url")
        link = sanitize_link(base, raw_link)
        if not is_http_url(link):
            continue
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title,
            "employer": "Randstad",
            "city": city,
            "link": link,
            "raw": desc
        })
    return offers

PROVIDERS = [
    fetch_france_travail,
    fetch_indeed,          # peut 403 (antibot) -> géré par try/except dans collect_offers
    fetch_vitijob,
    fetch_saisonnier_fr,
    fetch_lhotellerie,
    fetch_anefa_jobsagri,
    fetch_adecco,
    fetch_manpower,        # peut 403 selon cookies -> géré
    fetch_randstad         # 404 résolu via k/l + sanitisation
]

# -------- Pipeline --------
def collect_offers() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for provider in PROVIDERS:
        try:
            batch = provider()
            items += batch
            print(f"[PROVIDER] {provider.__name__}: {len(batch)} offres")
            time.sleep(1.0)
        except Exception as e:
            print(f"[WARN] Provider {provider.__name__} failed: {e}")
    print(f"[INFO] Total offres collectées (brut): {len(items)}")
    return items

def enrich_and_filter(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    geo, rate = geocoder()
    origin = geocode(ORIGIN_CITY, geo, rate)
    if not origin:
        raise RuntimeError("Impossible de géocoder la ville d'origine.")

    enriched: List[Dict[str, Any]] = []
    seen_links = set()

    for it in items:
        link = it.get("link") or ""
        if not is_http_url(link):
            continue
        if link in seen_links:
            continue
        seen_links.add(link)

        # Texte listing
        text_preview = " ".join([it.get("title",""), it.get("employer",""), it.get("city",""), it.get("raw","")]).strip()
        title = it.get("title","")
        employer = it.get("employer","")
        city = it.get("city","") or pick_city_from_text(text_preview)

        # Exclusions immédiates
        if mentions_permit(text_preview):
            continue
        if requires_training(text_preview):
            continue
        if not experience_ok(text_preview):
            continue

        # Secteurs / Logement: charge détail si nécessaire
        detail_text = ""
        need_detail = (not matches_sectors(text_preview)) or (HOUSING_REQUIRED and not matches_housing(text_preview))
        if need_detail:
            detail_text = fetch_page_text(link)

        check_text = text_preview + " " + (detail_text or "")
        if mentions_permit(check_text):
            continue
        if requires_training(check_text):
            continue
        if not experience_ok(check_text):
            continue

        if not matches_sectors(text_preview) and not (detail_text and matches_sectors(detail_text)):
            continue

        if HOUSING_REQUIRED and not (matches_housing(text_preview) or (detail_text and matches_housing(detail_text))):
            continue

        # Améliorations depuis le détail
        if detail_text:
            if not employer:
                m_emp = re.search(r"(?:Société|Entreprise|Employeur)\s*[:\-]\s*([^\n|]+)", detail_text, re.I)
                if m_emp:
                    employer = m_emp.group(1).strip()
            if not city:
                c2 = pick_city_from_text(detail_text)
                if c2:
                    city = c2

        # Distance
        coords = geocode(city or pick_city_from_text(text_preview), geo, rate)
        dist_km = 99999
        if coords and origin:
            dist_km = round(haversine_km(origin[0], origin[1], coords[0], coords[1]))

        # Contacts
        phone, email = fetch_contact_details(link)
        if not phone and FALLBACK_CONTACTS and employer:
            fb_phone, _ = fallback_contacts(employer, city)
            if fb_phone:
                phone = fb_phone

        enriched.append({
            "title": title,
            "employer": employer,
            "city": city,
            "distance_km": dist_km,
            "link": link,
            "phone": phone,
            "email": email,
            "raw": check_text.strip(),
            "recent_bias": -1 if looks_recent(check_text) else 0
        })

    # Tri: récence -> distance
    enriched.sort(key=lambda x: (x["recent_bias"], x["distance_km"]))

    if len(enriched) < MAX_RESULTS:
        print(f"[INFO] Seulement {len(enriched)} offres trouvées après filtrage (seuil min {MAX_RESULTS}).")

    return enriched

def make_email(offres: List[Dict[str, Any]]) -> tuple[str, str, str]:
    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"[{len(offres)}] Offres saisonnières - Clermont-Ferrand - {today}"

    lines_txt: List[str] = []
    rows_html: List[str] = []
    for i, o in enumerate(offres, 1):
        dist = f"{o['distance_km']} km" if o["distance_km"] != 99999 else "—"
        contact_txt = []
        if o.get("phone"):
            contact_txt.append(f"Téléphone: {o['phone']}")
        if o.get("email"):
            contact_txt.append(f"Email: {o['email']}")
        contact_line = " | ".join(contact_txt) if contact_txt else "Contact: via lien"
        line = (
            f"{i}. {o['title']} - {o.get('employer','')}\n"
            f"   📍 {o.get('city','')} - {dist}\n"
            f"   {contact_line}\n"
            f"   🔗 {o['link']}\n"
        )
        lines_txt.append(line)

        contact_html_parts = []
        if o.get("phone"):
            contact_html_parts.append(f"<div>Téléphone: <a href='tel:{o['phone']}'>{o['phone']}</a></div>")
        if o.get("email"):
            contact_html_parts.append(f"<div>Email: <a href='mailto:{o['email']}'>{o['email']}</a></div>")
        if not contact_html_parts:
            contact_html_parts.append("<div>Contact: via lien</div>")

        rows_html.append(
            f"<tr>"
            f"<td style='padding:6px 8px;'>{i}</td>"
            f"<td style='padding:6px 8px;'><a href='{o['link']}'>{o['title']}</a></td>"
            f"<td style='padding:6px 8px;'>{o.get('employer','')}</td>"
            f"<td style='padding:6px 8px;'>{o.get('city','')}</td>"
            f"<td style='padding:6px 8px; text-align:right;'>{dist}</td>"
            f"<td style='padding:6px 8px;'>{''.join(contact_html_parts)}</td>"
            f"</tr>"
        )

    text = "\n".join(lines_txt) if lines_txt else "Aucune offre trouvée aujourd'hui avec les filtres."
    html = f"""
<!DOCTYPE html>
<html>
  <body>
    <h2>Offres saisonnières (logement requis) — Départ: {ORIGIN_CITY}</h2>
    <p>Règles: sans permis, sans formation obligatoire, expérience ≤ 1 an. Seuil min: {MAX_RESULTS}. Fallback contacts: {"ON" if FALLBACK_CONTACTS else "OFF"}.</p>
    <table style="border-collapse:collapse; width:100%; border:1px solid #ddd;">
      <thead>
        <tr style="background:#f5f5f5;">
          <th style="padding:6px 8px; text-align:left;">#</th>
          <th style="padding:6px 8px; text-align:left;">Titre</th>
          <th style="padding:6px 8px; text-align:left;">Employeur</th>
          <th style="padding:6px 8px; text-align:left;">Ville</th>
          <th style="padding:6px 8px; text-align:right;">Distance</th>
          <th style="padding:6px 8px; text-align:left;">Contact</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
  </body>
</html>
"""
    return subject, text, html

def send_email(subject: str, text: str, html: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM and RECIPIENTS):
        raise RuntimeError("Config SMTP incomplète (voir secrets).")

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

def main():
    all_items = collect_offers()
    offers = enrich_and_filter(all_items)
    subject, text, html = make_email(offers)
    send_email(subject, text, html)
    print(f"Sent {len(offers)} offers to: {', '.join(RECIPIENTS)}")

if __name__ == "__main__":
    main()