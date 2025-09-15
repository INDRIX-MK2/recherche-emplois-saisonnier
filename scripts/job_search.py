# scripts/job_search.py — v1.9 (API-first: France Travail + Adzuna + Jooble; HTML fallback)
# Règles: logement requis, pas de formation obligatoire, exp <= 1 an, ciblage métier,
# accepter "Permis B" mais prioriser sans permis, dédup avancée, tri récence -> sans permis -> distance

import os
import smtplib
import ssl
import math
import time
import re
import random
import json
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
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))  # seuil minimum visé (on ne tronque pas)
HOUSING_REQUIRED = os.getenv("HOUSING_REQUIRED", "true").lower() == "true"
FALLBACK_CONTACTS = os.getenv("FALLBACK_CONTACTS", "true").lower() == "true"
FRANCE_WIDE = os.getenv("FRANCE_WIDE", "true").lower() == "true"  # API-first: par défaut France entière

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
RECIPIENTS = [e.strip() for e in os.getenv("RECIPIENTS", "").split(",") if e.strip()]

NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "example@example.com")
PROXY = os.getenv("PROXY")  # optionnel

# APIs — secrets
FT_CLIENT_ID = os.getenv("FT_CLIENT_ID")
FT_CLIENT_SECRET = os.getenv("FT_CLIENT_SECRET")
FT_TOKEN_URL = os.getenv("FT_TOKEN_URL", "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire")
FT_API_URL = os.getenv("FT_API_URL", "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search")  # v2 search

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
]
BASE_HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9", "Cache-Control": "no-cache"}
DOMAIN_REFERERS = {
    "francetravail.fr": "https://candidat.francetravail.fr/offres/recherche",
    "api.francetravail.io": "https://api.gouv.fr/",
    "adzuna.com": "https://developer.adzuna.com/overview",
    "jooble.org": "https://jooble.org/",
    "indeed.com": "https://fr.indeed.com/",
    "vitijob.com": "https://www.vitijob.com/",
    "saisonnier.fr": "https://www.saisonnier.fr/",
    "lhotellerie-restauration.fr": "https://www.lhotellerie-restauration.fr/emploi/",
    "jobagri.com": "https://www.jobagri.com/offres",
    "adecco.fr": "https://www.adecco.fr/",
    "manpower.fr": "https://www.manpower.fr/Offres",
    "randstad.fr": "https://www.randstad.fr/offres/"
}
def _headers_for(url: str) -> dict:
    h = dict(BASE_HEADERS)
    h["User-Agent"] = random.choice(UA_POOL)
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        host = ""
    for d, ref in DOMAIN_REFERERS.items():
        if d in host:
            h["Referer"] = ref
            break
    return h

PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# -------------------- HTTP Session --------------------
SESSION = requests.Session()
retry = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

def safe_get(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[dict] = None) -> requests.Response:
    hdrs = _headers_for(url)
    if headers: hdrs.update(headers)
    r = SESSION.get(url, params=params, headers=hdrs, proxies=PROXIES, timeout=25)
    r.raise_for_status()
    return r

def safe_post(url: str, data=None, json_data=None, headers: Optional[dict] = None) -> requests.Response:
    hdrs = _headers_for(url)
    if headers: hdrs.update(headers)
    r = SESSION.post(url, data=data, json=json_data, headers=hdrs, proxies=PROXIES, timeout=25)
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
    "carrefour", "auchan", "leclerc", "e.leclerc", "intermarché", "intermarche", "lidl", "aldi",
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

# -------------------- Autres filtres --------------------
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
    t = text.lower()
    return "permi" in t  # captures 'permis', 'permis b', etc.

# -------------------- URL helpers --------------------
URL_EXTRACT_RE = re.compile(r"(https?://[^\s'\"<>)]+)", re.I)
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
def extract_url_from_onclick(onclick_val: Optional[str]) -> str:
    if not onclick_val:
        return ""
    m = URL_EXTRACT_RE.search(onclick_val)
    return m.group(1) if m else ""
def extract_best_link(elem: BeautifulSoup, base: str) -> str:
    for attr in ("href", "data-href", "data-url", "data-link"):
        raw = elem.get(attr)
        url = sanitize_link(base, raw)
        if is_http_url(url):
            return url
    a = elem.find("a", href=True)
    if a:
        url = sanitize_link(base, a.get("href"))
        if is_http_url(url):
            return url
    onclick_raw = elem.get("onclick") or (a.get("onclick") if a else None)
    if onclick_raw:
        candidate = extract_url_from_onclick(onclick_raw)
        url = sanitize_link(base, candidate)
        if is_http_url(url):
            return url
    return ""

# -------------------- Contacts --------------------
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+33\s?|0)(?:\s|\.|-)?)?[1-9](?:[\s\.\-]?\d{2}){4}")
def normalize_phone(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s
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
        if not is_http_url(url): return None, None
        r = safe_get(url)
        phone, email = extract_contacts_from_html(r.text)
        time.sleep(0.4)
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

# -------------------- Fetch page text (HTML) --------------------
def fetch_page_text(url: str) -> str:
    try:
        if not is_http_url(url): return ""
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        return soup.get_text(" ", strip=True)
    except Exception as e:
        print(f"[INFO] fetch_page_text failed for {url}: {e}")
        return ""

# ===============================================================
#                       PROVIDERS — API
# ===============================================================

# ---- France Travail API ----
def fetch_france_travail_api() -> List[Dict[str, Any]]:
    if not (FT_CLIENT_ID and FT_CLIENT_SECRET):
        return []
    offers: List[Dict[str, Any]] = []
    try:
        # OAuth2 client_credentials
        data = {
            "grant_type": "client_credentials",
            "client_id": FT_CLIENT_ID,
            "client_secret": FT_CLIENT_SECRET,
            # scope: la v2 n'exige généralement pas si configurée côté compte; on garde générique
        }
        tok = safe_post(FT_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        access_token = tok.json().get("access_token")
        if not access_token:
            print("[WARN] FT API: no access_token")
            return []

        params = {}
        # recherche mots clés France entière
        params["motsCles"] = "saisonnier logé nourri OR logement fourni OR logé"
        # Filtre domaine? on garde large; on filtre côté code ensuite.
        # Taille page: la v2 autorise 'range' ou 'page' selon version; on pagine simple
        params["page"] = 1
        params["per_page"] = 100

        headers = {"Authorization": f"Bearer {access_token}"}
        r = safe_get(FT_API_URL, params=params, headers=headers)
        data_json = r.json()

        items = data_json.get("resultats") or data_json.get("offres") or []
        for it in items:
            title = it.get("intitule") or it.get("titre") or "Offre"
            employeur = (it.get("entreprise") or {}).get("nom") or ""
            lieu = (it.get("lieuTravail") or {}).get("libelle") or it.get("lieuTravail") or ""
            lien = it.get("origineOffre", {}).get("urlOrigine") or it.get("url") or ""
            desc = it.get("description") or ""

            offers.append({
                "title": title,
                "employer": employeur,
                "city": lieu,
                "link": lien,
                "raw": " ".join([title, employeur, lieu, desc])
            })
    except Exception as e:
        print(f"[WARN] France Travail API failed: {e}")
    return offers

# ---- Adzuna API ----
def fetch_adzuna_api() -> List[Dict[str, Any]]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    offers: List[Dict[str, Any]] = []
    try:
        base = "https://api.adzuna.com/v1/api/jobs/fr/search/1"
        query = "saisonnier (logé OR logement OR loge) (serveur OR serveuse OR barman OR polyvalent OR vendange OR cueillette OR plonge)"
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": query,
            "results_per_page": 50,  # première page large
            "content-type": "application/json"
        }
        r = safe_get(base, params=params)
        js = r.json()
        for res in js.get("results", []):
            title = res.get("title") or "Offre"
            company = (res.get("company") or {}).get("display_name") or ""
            location = (res.get("location") or {}).get("display_name") or ""
            url = res.get("redirect_url") or res.get("adref") or ""
            desc = (res.get("description") or "") + " " + (res.get("category", {}).get("label", "") or "")
            offers.append({
                "title": title,
                "employer": company,
                "city": location,
                "link": url,
                "raw": " ".join([title, company, location, desc])
            })
    except Exception as e:
        print(f"[WARN] Adzuna API failed: {e}")
    return offers

# ---- Jooble API ----
def fetch_jooble_api() -> List[Dict[str, Any]]:
    if not JOOBLE_API_KEY:
        return []
    offers: List[Dict[str, Any]] = []
    try:
        url = f"https://jooble.org/api/{JOOBLE_API_KEY}"
        body = {
            "keywords": "saisonnier logé OR logement OR loge OR hébergement (serveur OR serveuse OR barman OR polyvalent OR vendange OR cueillette OR plonge)",
            # pas de 'location' => France entière
            "page": 1
        }
        r = safe_post(url, json_data=body, headers={"Content-Type": "application/json"})
        js = r.json()
        for res in js.get("jobs", []):
            title = res.get("title") or "Offre"
            company = res.get("company") or ""
            location = res.get("location") or ""
            link = res.get("link") or ""
            desc = res.get("snippet") or res.get("description") or ""
            offers.append({
                "title": title,
                "employer": company,
                "city": location,
                "link": link,
                "raw": " ".join([title, company, location, desc])
            })
    except Exception as e:
        print(f"[WARN] Jooble API failed: {e}")
    return offers

# ===============================================================
#                   PROVIDERS — HTML fallback
# ===============================================================
def fetch_france_travail_html() -> List[Dict[str, Any]]:
    base = "https://candidat.francetravail.fr"
    url = base + "/offres/recherche"
    params = {"motsCles": "saisonnier logé nourri"} if FRANCE_WIDE else \
             {"motsCles": "saisonnier logé nourri", "lieu": "Clermont-Ferrand (63)", "rayon": "200"}
    try:
        r = safe_get(url, params)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        for card in soup.select("[data-id-offre]"):
            title = card.select_one("h3")
            employer = card.select_one(".t4.color-dark-blue")
            city = card.select_one(".subtext")
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
    except Exception as e:
        print(f"[WARN] FT HTML failed: {e}")
        return []

def fetch_saisonnier_fr() -> List[Dict[str, Any]]:
    base = "https://www.saisonnier.fr"
    url = base + "/emplois"
    try:
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        for card in soup.select("article, .job-card, li, .job, .search-item"):
            title_el = card.select_one("a")
            if not title_el: continue
            title = title_el.get_text(strip=True)
            raw_link = title_el.get("href", "")
            link = sanitize_link(base, raw_link)
            meta = card.get_text(" ", strip=True)
            city = ""
            loc = card.select_one(".job-location, .location, .lieu")
            if loc: city = loc.get_text(strip=True)
            offers.append({
                "title": title or "Offre Saisonnier.fr",
                "employer": "",
                "city": city or pick_city_from_text(meta),
                "link": link,
                "raw": meta
            })
        return offers
    except Exception as e:
        print(f"[WARN] Saisonnier.fr failed: {e}")
        return []

def fetch_lhotellerie() -> List[Dict[str, Any]]:
    base = "https://www.lhotellerie-restauration.fr"
    url = base + "/emploi/"
    try:
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        for card in soup.select("article, .offre, .annonce"):
            title_el = card.select_one("a")
            if not title_el: continue
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
    except Exception as e:
        print(f"[WARN] LHR failed: {e}")
        return []

def fetch_anefa_jobsagri() -> List[Dict[str, Any]]:
    base = "https://www.jobagri.com"
    url = base + "/offres"
    try:
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        for card in soup.select("article, .job, .offre"):
            title_el = card.select_one("a")
            if not title_el: continue
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
    except Exception as e:
        print(f"[WARN] Jobagri failed: {e}")
        return []

def fetch_adecco() -> List[Dict[str, Any]]:
    base = "https://www.adecco.fr"
    url = base + "/resultats-offres-emploi/"
    params = {"k": "saisonnier logé"} if FRANCE_WIDE else {"k": "saisonnier logé", "l": "Clermont-Ferrand"}
    try:
        r = safe_get(url, params)
        soup = BeautifulSoup(r.text, "lxml")
        offers = []
        cards = soup.select("article, .result-item, li, .job-tile, .offer-card") or soup.select("article, .result-item, li")
        for card in cards:
            a = card.select_one("a")
            title = a.get_text(strip=True) if a else (card.get_text(" ", strip=True)[:60] or "Offre Adecco")
            link = extract_best_link(card, base)
            if not link and a: link = sanitize_link(base, a.get("href", ""))
            if not is_http_url(link): continue
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
    except Exception as e:
        print(f"[WARN] Adecco failed: {e}")
        return []

# ===============================================================
#                          PIPELINE
# ===============================================================
API_PROVIDERS = [
    fetch_france_travail_api,
    fetch_adzuna_api,
    fetch_jooble_api
]
HTML_FALLBACKS = [
    fetch_france_travail_html,
    fetch_saisonnier_fr,
    fetch_lhotellerie,
    fetch_anefa_jobsagri,
    fetch_adecco
]

def collect_offers() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    # APIs d'abord
    for provider in API_PROVIDERS + HTML_FALLBACKS:
        try:
            batch = provider()
            items += batch
            print(f"[PROVIDER] {provider.__name__}: {len(batch)} offres")
            time.sleep(0.8)
        except Exception as e:
            print(f"[WARN] Provider {provider.__name__} failed: {e}")
    print(f"[INFO] Total offres collectées (brut): {len(items)}")
    return items

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

        text_preview = " ".join([it.get("title",""), it.get("employer",""), it.get("city",""), it.get("raw","")]).strip()
        title = it.get("title","")
        employer = it.get("employer","")
        city = it.get("city","") or pick_city_from_text(text_preview)

        # Formation / Expérience (listing)
        if requires_training(text_preview): continue
        if not experience_ok(text_preview): continue

        # Charger détail si besoin (métier/logement)
        detail_text = ""
        need_detail = (not role_allowed(text_preview)) or (HOUSING_REQUIRED and not matches_housing(text_preview))
        if need_detail:
            detail_text = fetch_page_text(link)

        check_text = (text_preview + " " + (detail_text or "")).strip()

        # Métier
        if not role_allowed(check_text):
            continue

        # Logement requis
        if HOUSING_REQUIRED and not matches_housing(check_text):
            continue

        # Formation / Expérience (sur détail)
        if requires_training(check_text): continue
        if not experience_ok(check_text): continue

        # Permis: on accepte, mais on note pour priorisation
        requires_permit = bool(mentions_permit(check_text))

        # Améliorations depuis le détail
        if detail_text:
            if not employer:
                m_emp = re.search(r"(?:Société|Entreprise|Employeur)\s*[:\-]\s*([^\n|]+)", detail_text, re.I)
                if m_emp: employer = m_emp.group(1).strip()
            if not city:
                c2 = pick_city_from_text(detail_text)
                if c2: city = c2

        # Distance
        coords = geocode(city or pick_city_from_text(text_preview), geo, rate)
        dist_km = 99999
        if coords and origin:
            dist_km = round(haversine_km(origin[0], origin[1], coords[0], coords[1]))

        # Contacts
        phone, email = fetch_contact_details(link)
        if not phone and FALLBACK_CONTACTS and employer:
            fb_phone, _ = fallback_contacts(employer, city)
            if fb_phone: phone = fb_phone

        candidates.append({
            "title": title,
            "employer": employer,
            "city": city,
            "distance_km": dist_km,
            "link": link,
            "phone": phone,
            "email": email,
            "raw": check_text,
            "recent_bias": -1 if looks_recent(check_text) else 0,
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
    <p>Sources: APIs France Travail / Adzuna / Jooble (+ fallback HTML). Règles: pas de formation obligatoire, expérience ≤ 1 an, métiers ciblés (service/bar/polyvalent, vente non alimentaire, rayon). Priorité aux offres <strong>sans permis</strong>.</p>
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