# scripts/job_search.py — v2.0 (HTML-only multi-sources)
# Sources: France Travail (HTML), Saisonnier.fr, LHR, Jobagri, Adecco, Hellowork, Meteojob, Adzuna (HTML), Jobijoba, Indeed
# Règles: logement requis, pas de diplôme obligatoire, exp <= 1 an, métiers ciblés,
# accepte "Permis B" mais priorise sans permis, dédup avancée, tri récence -> sans permis -> distance

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
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))  # seuil minimum visé (on n'écrête pas)
HOUSING_REQUIRED = os.getenv("HOUSING_REQUIRED", "true").lower() == "true"
FALLBACK_CONTACTS = os.getenv("FALLBACK_CONTACTS", "true").lower() == "true"
FRANCE_WIDE = os.getenv("FRANCE_WIDE", "true").lower() == "true"  # France entière par défaut

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
RECIPIENTS = [e.strip() for e in os.getenv("RECIPIENTS", "").split(",") if e.strip()]

NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "example@example.com")
PROXY = os.getenv("PROXY")  # optionnel

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
    "carrefour", "auchan", "leclerc", "intermarché", "intermarche", "lidl", "aldi",
    "monoprix", "casino", "super u", "u express", "géant", "geant", "cora", "match", "spar"
]
CLEANING_SOFT = [
    "agent d'entretien", "agent de nettoyage", "entretien", "nettoyage",
    "femme de chambre", "valet de chambre", "gouvernante", "laveur", "laveuse"
]
PLONGE_SOFT = ["plonge", "plongeur", "plongeuse"]

def _has_any(text: str, words: List[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)

def _role_is_cleaning_only(text: str) -> bool:
    t = text.lower()
    if not _has_any(t, CLEANING_SOFT):
        return False
    return not (_has_any(t, SERVICE_BAR_POLY) or _has_any(t, VENTE_OBJETS) or _has_any(t, RAYON_MERCH))

def _role_is_plonge_only(text: str) -> bool:
    t = text.lower()
    if not _has_any(t, PLONGE_SOFT):
        return False
    return not (_has_any(t, SERVICE_BAR_POLY) or _has_any(t, VENTE_OBJETS) or _has_any(t, RAYON_MERCH))

def role_allowed(text: str) -> bool:
    t = text.lower()
    if _has_any(t, CUISINE_HARD) or _has_any(t, ALIMENTAIRE_HARD) or _has_any(t, HYPER_CHAINS):
        return False
    if _role_is_cleaning_only(t) or _role_is_plonge_only(t):
        return False
    allow_hit = _has_any(t, SERVICE_BAR_POLY) or _has_any(t, VENTE_OBJETS) or _has_any(t, RAYON_MERCH)
    return allow_hit

# -------------------- Logement & autres filtres --------------------
HOUSING_KEYS = [
    "logé", "loge", "logement fourni", "logement inclus", "logement possible",
    "nourri", "logé nourri", "loge nourri", "hébergement fourni", "hébergement", "logement sur place"
]
def matches_housing(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in HOUSING_KEYS)

TRAINING_KEYWORDS = [
    "diplôme", "diplome", "cap", "bep", "bac", "bts", "dut", "licence", "master", "bac+",
    "certificat", "certification", "caces", "ssiap", "haccp", "h a c c p", "titre professionnel"
]
TRAINING_REQUIRE_WORDS = ["exig", "requis", "obligatoire", "indispensable", "nécessaire"]
def requires_training(text: str) -> bool:
    t = text.lower()
    has_training = any(k in t for k in TRAINING_KEYWORDS)
    has_requirement = any(w in t for w in TRAINING_REQUIRE_WORDS)
    return has_training and has_requirement

RE_YEARS = re.compile(r"(\d+)\s*(?:an|ans)\s+d[' ]?exp[ée]rience", re.I)
RE_RANGE = re.compile(r"(\d+)\s*(?:à|-|–|—)\s*(\d+)\s*(?:an|ans)\s+d[' ]?exp[ée]rience", re.I)
RE_MIN = re.compile(r"(?:au moins|minimum|min\.?)\s*(\d+)\s*(?:an|ans)", re.I)
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
    return True

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

def mentions_permit(text: str) -> bool:
    return "permi" in text.lower()

# -------------------- Contacts --------------------
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+33\s?|0)(?:\s|\.|-)?)?[1-9](?:[\s\.\-]?\d{2}){4}")
def normalize_phone(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())
def extract_contacts_from_html(html: str) -> Tuple[Optional[str], Optional[str]]:
    phone = None; email = None
    soup = BeautifulSoup(html, "lxml")
    a_mail = soup.select_one("a[href^='mailto:']")
    if a_mail:
        mail_href = a_mail.get("href", "")
        m = re.search(EMAIL_RE, mail_href)
        if m: email = m.group(0)
    a_tel = soup.select_one("a[href^='tel:']")
    if a_tel:
        tel_href = a_tel.get("href", "")
        m = re.search(PHONE_RE, tel_href)
        if m: phone = normalize_phone(m.group(0))
    if not email:
        m = EMAIL_RE.search(html)
        if m: email = m.group(0)
    if not phone:
        m = PHONE_RE.search(html)
        if m: phone = normalize_phone(m.group(0))
    return phone, email

def fetch_contact_details(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = safe_get(url)
        phone, email = extract_contacts_from_html(r.text)
        time.sleep(0.3)
        return phone, email
    except Exception as e:
        print(f"[INFO] No direct contacts extracted from {url}: {e}")
        return None, None

# -------------------- Fallback annuaires --------------------
def pj_phone_lookup(employer: str, city_hint: str) -> Optional[str]:
    if not employer: return None
    q = f"{employer} {city_hint}".strip()
    url = "https://www.pagesjaunes.fr/recherche"
    params = {"quoiqui": q}
    try:
        r = safe_get(url, params=params)
        soup = BeautifulSoup(r.text, "lxml")
        for tel in soup.select("a.tel, span.number, div.bi-bloc-contact a"):
            text = tel.get_text(" ", strip=True)
            m = PHONE_RE.search(text)
            if m: return normalize_phone(m.group(0))
        m = PHONE_RE.search(soup.get_text(" ", strip=True))
        if m: return normalize_phone(m.group(0))
    except Exception as e:
        print(f"[INFO] PagesJaunes lookup failed: {e}")
    return None

def ae_phone_lookup(employer: str, city_hint: str) -> Optional[str]:
    if not employer: return None
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
                if m: return normalize_phone(m.group(0))
    except Exception as e:
        print(f"[INFO] Annuaire-Entreprises lookup failed: {e}")
    return None

def fallback_contacts(employer: str, city: str) -> Tuple[Optional[str], Optional[str]]:
    if not FALLBACK_CONTACTS or not employer: return None, None
    phone = pj_phone_lookup(employer, city) or ae_phone_lookup(employer, city)
    return phone, None

# -------------------- Helpers URLs --------------------
def is_http_url(u: Optional[str]) -> bool:
    return bool(u) and (u.startswith("http://") or u.startswith("https://"))

def sanitize_link(base: str, raw: Optional[str]) -> str:
    if not raw:
        return ""
    href = raw.strip().strip(";").strip()
    if not href or href == "#" or href.lower().startswith("javascript:"):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        return urllib.parse.urljoin(base, href)
    return href

# ===============================================================
#                       SCRAPERS (HTML)
# ===============================================================

def fetch_france_travail_html() -> List[Dict[str, Any]]:
    base = "https://candidat.francetravail.fr"
    search_url = base + "/offres/recherche"
    common_params = {"motsCles": "saisonnier logé OR logement fourni OR hébergement"}
    if not FRANCE_WIDE:
        common_params.update({"lieu": "Clermont-Ferrand (63)", "rayon": "200"})
    offers: List[Dict[str, Any]] = []
    visited = 0
    next_url = search_url
    next_params = dict(common_params)
    while next_url and visited < 3:
        r = safe_get(next_url, params=next_params)
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("[data-id-offre]") or soup.select("article, li, div")
        for card in cards:
            title_el = card.select_one("h3, h2, .media-heading")
            title = title_el.get_text(strip=True) if title_el else "Offre"
            employer_el = card.select_one(".t4.color-dark-blue, [data-testid='company-name'], .company, .entreprise")
            employer = employer_el.get_text(strip=True) if employer_el else ""
            city_el = card.select_one(".subtext, [data-testid='lieu'], .location, .lieu")
            city = city_el.get_text(strip=True) if city_el else ""
            raw_link = card.get("data-href")
            if not raw_link:
                a = card.find("a", href=True); raw_link = a.get("href", "") if a else ""
            link = sanitize_link(base, raw_link)
            if not is_http_url(link): continue
            desc = card.get_text(" ", strip=True)
            offers.append({"title": title, "employer": employer, "city": city or pick_city_from_text(desc), "link": link, "raw": desc})
        next_link = soup.select_one("a[rel='next'], a[aria-label*='suiv'], a[title*='suiv']")
        if next_link and next_link.get("href"):
            full = urllib.parse.urljoin(base, next_link.get("href"))
            parsed = urllib.parse.urlparse(full)
            next_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            next_params = dict(common_params)
            next_params.update(dict(urllib.parse.parse_qsl(parsed.query)))
            visited += 1
            time.sleep(0.6)
        else:
            break
    return offers

def fetch_saisonnier_fr() -> List[Dict[str, Any]]:
    base = "https://www.saisonnier.fr"
    url = base + "/emplois"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job-card, li, .job, .search-item"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True)
        link = sanitize_link(base, a.get("href", ""))
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title or "Offre Saisonnier.fr", "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_lhotellerie() -> List[Dict[str, Any]]:
    base = "https://www.lhotellerie-restauration.fr"
    url = base + "/emploi/"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .offre, .annonce"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True)
        link = sanitize_link(base, a.get("href", ""))
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title or "Offre Hôtellerie-Restauration", "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_jobagri() -> List[Dict[str, Any]]:
    base = "https://www.jobagri.com"
    url = base + "/offres"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job, .offre, li"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True)
        link = sanitize_link(base, a.get("href", ""))
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title or "Offre agricole", "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
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
        if not a: continue
        title = a.get_text(strip=True) or "Offre Adecco"
        link = sanitize_link(base, a.get("href", ""))
        if not is_http_url(link): continue
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title, "employer": "Adecco", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_hellowork() -> List[Dict[str, Any]]:
    base = "https://www.hellowork.com"
    url = base + "/fr-fr/emplois"
    params = {"k": "saisonnier logement" if FRANCE_WIDE else "saisonnier logement", "l": "" if FRANCE_WIDE else "Clermont-Ferrand"}
    r = safe_get(url, params=params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job-card, .search-results__item, li"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True) or "Offre HelloWork"
        link = sanitize_link(base, a.get("href", ""))
        if not is_http_url(link): continue
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title, "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_meteojob() -> List[Dict[str, Any]]:
    base = "https://www.meteojob.com"
    url = base + "/emploi"
    params = {"q": "saisonnier logement"}
    if not FRANCE_WIDE: params["l"] = "Clermont-Ferrand"
    r = safe_get(url, params=params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .jobCard, .search-result, li"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True) or "Offre Meteojob"
        link = sanitize_link(base, a.get("href", ""))
        if not is_http_url(link): continue
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title, "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_adzuna_html() -> List[Dict[str, Any]]:
    base = "https://www.adzuna.fr"
    # recherche simple france entière
    url = base + "/search"
    params = {"what": "saisonnier logé logement", "where": "" if FRANCE_WIDE else "Clermont-Ferrand"}
    r = safe_get(url, params=params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .search-result, .job, li"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True) or "Offre Adzuna"
        link = sanitize_link(base, a.get("href", ""))
        if not is_http_url(link): continue
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title, "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_jobijoba() -> List[Dict[str, Any]]:
    base = "https://www.jobijoba.com"
    # URL de recherche centrale; certains contenus sont côté serveur
    url = base + "/fr/recherche-emploi"
    params = {"k": "saisonnier logé", "l": "" if FRANCE_WIDE else "Clermont-Ferrand"}
    r = safe_get(url, params=params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .o-card, .result-item, li"):
        a = card.select_one("a")
        if not a: continue
        title = a.get_text(strip=True) or "Offre Jobijoba"
        link = sanitize_link(base, a.get("href", ""))
        if not is_http_url(link): continue
        desc = card.get_text(" ", strip=True)
        offers.append({"title": title, "employer": "", "city": pick_city_from_text(desc), "link": link, "raw": desc})
    return offers

def fetch_indeed() -> List[Dict[str, Any]]:
    base = "https://fr.indeed.com"
    url = base + "/jobs"
    params = {"q": "saisonnier (logé OR logement OR loge) (serveur OR serveuse OR vendange OR cueillette OR plonge)"}
    if not FRANCE_WIDE:
        params.update({"l": "Clermont-Ferrand (63)", "radius": "200"})
    try:
        r = safe_get(url, params=params)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        for card in soup.select("div.job_seen_beacon, .result, article"):
            title_el = card.select_one("h2 a, a[aria-label]")
            if not title_el: continue
            title = title_el.get_text(strip=True) or "Offre Indeed"
            link = sanitize_link(base, title_el.get("href", ""))
            if not is_http_url(link): continue
            desc = card.get_text(" ", strip=True)
            comp = card.select_one(".companyName, .company") or None
            employer = comp.get_text(strip=True) if comp else ""
            offers.append({"title": title, "employer": employer, "city": pick_city_from_text(desc), "link": link, "raw": desc})
        return offers
    except Exception as e:
        print(f"[WARN] Indeed failed: {e}")
        return []

HTML_PROVIDERS = [
    fetch_france_travail_html,
    fetch_saisonnier_fr,
    fetch_lhotellerie,
    fetch_jobagri,
    fetch_adecco,
    fetch_hellowork,
    fetch_meteojob,
    fetch_adzuna_html,
    fetch_jobijoba,
    fetch_indeed
]

# -------------------- Collecte --------------------
def collect_offers() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for provider in HTML_PROVIDERS:
        try:
            batch = provider()
            items.extend(batch)
            print(f"[PROVIDER] {provider.__name__}: {len(batch)} offres")
            time.sleep(0.6)
        except Exception as e:
            print(f"[WARN] Provider {provider.__name__} failed: {e}")
    print(f"[INFO] Total offres collectées (brut): {len(items)}")
    return items

# -------------------- Filtrage & enrichissement --------------------
def enrich_and_filter(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    geo, rate = geocoder()
    origin = geocode(ORIGIN_CITY, geo, rate)
    if not origin:
        raise RuntimeError("Impossible de géocoder la ville d'origine.")

    candidates: List[Dict[str, Any]] = []
    seen_links = set()

    for it in items:
        link = it.get("link") or ""
        if not is_http_url(link): continue
        if link in seen_links: continue
        seen_links.add(link)

        title = it.get("title","")
        employer = it.get("employer","")
        city = it.get("city","")
        text_preview = " ".join([title, employer, city, it.get("raw","")]).strip()

        # Filtrage listing
        if requires_training(text_preview): continue
        if not experience_ok(text_preview): continue

        # Détail si nécessaire
        detail_text = ""
        need_detail = (not role_allowed(text_preview)) or (HOUSING_REQUIRED and not matches_housing(text_preview))
        if need_detail:
            try:
                r = safe_get(link)
                soup = BeautifulSoup(r.text, "lxml")
                detail_text = soup.get_text(" ", strip=True)
            except Exception as e:
                print(f"[INFO] fetch_page_text failed for {link}: {e}")

        check_text = (text_preview + " " + (detail_text or "")).strip()

        # Métier
        if not role_allowed(check_text):
            continue

        # Logement requis
        if HOUSING_REQUIRED and not matches_housing(check_text):
            continue

        # Formation/exp sur détail
        if requires_training(check_text): continue
        if not experience_ok(check_text): continue

        requires_permit = bool(mentions_permit(check_text))

        # city depuis détail si vide
        if not city:
            c2 = pick_city_from_text(check_text)
            if c2: city = c2

        # Distance
        dist_km = 99999
        coords = geocode(city, geo, rate) if city else None
        if coords and origin:
            dist_km = round(haversine_km(origin[0], origin[1], coords[0], coords[1]))

        # Contacts
        phone, email = fetch_contact_details(link)
        if not phone and FALLBACK_CONTACTS and employer:
            fb_phone, _ = fallback_contacts(employer, city)
            if fb_phone: phone = fb_phone

        candidates.append({
            "title": title, "employer": employer, "city": city,
            "distance_km": dist_km, "link": link, "phone": phone, "email": email,
            "raw": check_text, "recent_bias": -1 if looks_recent(check_text) else 0,
            "requires_permit": requires_permit
        })

    # Dédup avancée (titre+employeur+ville)
    def canon(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower().strip())

    best_by_sig: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for o in candidates:
        sig = (canon(o["title"]), canon(o["employer"]), canon(o["city"]))
        prev = best_by_sig.get(sig)
        if not prev:
            best_by_sig[sig] = o
        else:
            prev_key = (prev["requires_permit"], prev["distance_km"], prev["recent_bias"])
            new_key  = (o["requires_permit"], o["distance_km"], o["recent_bias"])
            if new_key < prev_key:
                best_by_sig[sig] = o

    enriched = list(best_by_sig.values())

    # Tri: récence -> sans permis -> distance
    enriched.sort(key=lambda x: (x["recent_bias"], 1 if x["requires_permit"] else 0, x["distance_km"]))

    if len(enriched) < MAX_RESULTS:
        print(f"[INFO] Seulement {len(enriched)} offres trouvées après filtrage (seuil min {MAX_RESULTS}).")
    return enriched

# -------------------- Email --------------------
def make_email(offres: List[Dict[str, Any]]) -> tuple[str, str, str]:
    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"[{len(offres)}] Offres saisonnières - Départ {ORIGIN_CITY} - {today}"

    lines_txt: List[str] = []
    rows_html: List[str] = []
    for i, o in enumerate(offres, 1):
        dist = f"{o['distance_km']} km" if o["distance_km"] != 99999 else "—"
        contact_txt = []
        if o.get("phone"): contact_txt.append(f"Téléphone: {o['phone']}")
        if o.get("email"): contact_txt.append(f"Email: {o['email']}")
        if o.get("requires_permit"): contact_txt.append("Permis: mentionné")
        contact_line = " | ".join(contact_txt) if contact_txt else "Contact: via lien"

        lines_txt.append(
            f"{i}. {o['title']} - {o.get('employer','')}\n"
            f"   📍 {o.get('city','')} - {dist}\n"
            f"   {contact_line}\n"
            f"   🔗 {o['link']}\n"
        )

        contact_html_parts = []
        if o.get("phone"): contact_html_parts.append(f"<div>Téléphone: <a href='tel:{o['phone']}'>{o['phone']}</a></div>")
        if o.get("email"): contact_html_parts.append(f"<div>Email: <a href='mailto:{o['email']}'>{o['email']}</a></div>")
        if o.get("requires_permit"): contact_html_parts.append("<div><strong>Permis :</strong> mentionné</div>")
        if not contact_html_parts: contact_html_parts.append("<div>Contact: via lien</div>")

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
    <p>Sources: France Travail (HTML), Hellowork, Meteojob, Adzuna, Jobijoba, Indeed, Saisonnier.fr, LHR, Jobagri, Adecco. Règles: pas de formation obligatoire, expérience ≤ 1 an, métiers ciblés (service/bar/polyvalent, vente non alimentaire, rayon). Priorité aux offres <strong>sans permis</strong>.</p>
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