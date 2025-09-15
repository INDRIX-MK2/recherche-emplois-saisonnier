import os, smtplib, ssl, math, time, re, urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# -------- Config from env --------
ORIGIN_CITY = os.getenv("ORIGIN_CITY", "Clermont-Ferrand, France")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))
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
PROXY = os.getenv("PROXY")

HEADERS = {
    "User-Agent": f"Mozilla/5.0 (compatible; JobSearchBot/1.2; +{NOMINATIM_EMAIL})",
    "Accept-Language": "fr-FR,fr;q=0.9"
}
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# -------- Geocoding helpers --------
def geocoder():
    geolocator = Nominatim(user_agent=f"jobsearch-bot-{NOMINATIM_EMAIL}")
    return geolocator, RateLimiter(geolocator.geocode, min_delay_seconds=1, swallow_exceptions=True)

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.asin(math.sqrt(a))

def geocode(text, geo, rate):
    if not text:
        return None
    try:
        loc = rate(text + ", France")
        if loc:
            return (loc.latitude, loc.longitude)
    except Exception:
        return None
    return None

# -------- Filters --------
HOUSING_KEYS = [
    "logé", "loge", "logement fourni", "logement inclus", "logement possible",
    "nourri", "logé nourri", "loge nourri", "hébergement fourni", "hébergement", "logement sur place"
]
SECTOR_KEYS = {
    "hotellerie-restauration": ["serveur", "serveuse", "commis", "plonge", "réceptionniste", "barman", "barmaid", "chef de rang", "hôtel", "restaurant"],
    "agri-vendanges": ["vendange", "vendanges", "cueillette", "viticole", "viticulture", "agricole", "maraîchage", "saisonnier agricole", "exploitation agricole"]
}

def matches_housing(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in HOUSING_KEYS)

def matches_sectors(text: str) -> bool:
    t = text.lower()
    keys = []
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

# -------- HTTP helpers --------
def safe_get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, proxies=PROXIES, timeout=25)
    r.raise_for_status()
    return r

def pick_city_from_text(text: str) -> str:
    m = re.search(r"([A-ZÉÈÎÏÔÂÇa-zÀ-ÿ' -]+)\s*\((\d{2,3})\)", text)
    if m:
        return f"{m.group(1).strip()} ({m.group(2)})"
    m2 = re.search(r"\b(?:à|sur|près de|proche de)\s+([A-ZÉÈÎÏÔÂÇa-zÀ-ÿ' -]{3,40})", text)
    if m2:
        return m2.group(1).strip()
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
        if not url or not url.startswith("http"):
            return None, None
        r = safe_get(url)
        phone, email = extract_contacts_from_html(r.text)
        time.sleep(0.5)
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

# -------- Providers --------
def fetch_france_travail() -> List[Dict[str, Any]]:
    url = "https://candidat.francetravail.fr/offres/recherche"
    params = {"motsCles": "saisonnier logé nourri", "lieu": "Clermont-Ferrand (63)", "rayon": "200"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("[data-id-offre]"):
        title = card.select_one("h3")
        employer = card.select_one(".t4.color-dark-blue")
        city = card.select_one(".subtext")
        link = card.get("data-href") or (card.find("a", href=True).get("href", "") if card.find("a", href=True) else "")
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title.get_text(strip=True) if title else "Offre",
            "employer": employer.get_text(strip=True) if employer else "",
            "city": (city.get_text(strip=True) if city else "") or pick_city_from_text(desc),
            "link": ("https://candidat.francetravail.fr" + link) if link and link.startswith("/") else link,
            "raw": desc
        })
    return offers

def fetch_indeed() -> List[Dict[str, Any]]:
    url = "https://fr.indeed.com/jobs"
    params = {
        "q": "saisonnier (logé OR logement OR loge) (serveur OR serveuse OR vendange OR cueillette OR plonge)",
        "l": "Clermont-Ferrand (63)",
        "radius": "150"
    }
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("div.job_seen_beacon"):
        title_el = card.select_one("h2 a")
        title = title_el.get_text(strip=True) if title_el else "Offre Indeed"
        link = "https://fr.indeed.com" + title_el.get("href", "") if title_el else ""
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
    url = "https://www.vitijob.com/fr/recherche"
    params = {"q": "vendanges logé", "l": "Auvergne"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select(".job-offer-card, .search-item, article"):
        title_el = card.select_one("a")
        title = title_el.get_text(strip=True) if title_el else "Offre Vitijob"
        link = title_el.get("href", "") if title_el else ""
        employer = card.select_one(".company, .entreprise, .company-name")
        city = card.select_one(".location, .lieu")
        desc = card.get_text(" ", strip=True)
        offers.append({
            "title": title,
            "employer": employer.get_text(strip=True) if employer else "",
            "city": (city.get_text(strip=True) if city else "") or pick_city_from_text(desc),
            "link": link if link.startswith("http") else ("https://www.vitijob.com" + link),
            "raw": desc
        })
    return offers

def fetch_saisonnier_fr() -> List[Dict[str, Any]]:
    url = "https://www.saisonnier.fr/emplois"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job-card, li"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        meta = card.get_text(" ", strip=True)
        city = ""
        loc = card.select_one(".job-location, .location, .lieu")
        if loc:
            city = loc.get_text(strip=True)
        offers.append({
            "title": title or "Offre Saisonnier.fr",
            "employer": "",
            "city": city or pick_city_from_text(meta),
            "link": link if link.startswith("http") else ("https://www.saisonnier.fr" + link),
            "raw": meta
        })
    return offers

def fetch_lhotellerie() -> List[Dict[str, Any]]:
    url = "https://www.lhotellerie-restauration.fr/emploi/"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .offre, .annonce"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Hôtellerie-Restauration",
            "employer": "",
            "city": city,
            "link": link if link.startswith("http") else ("https://www.lhotellerie-restauration.fr" + link),
            "raw": desc
        })
    return offers

def fetch_anefa_jobsagri() -> List[Dict[str, Any]]:
    url = "https://www.jobagri.com/offres"
    r = safe_get(url)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .job, .offre"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre agricole",
            "employer": "",
            "city": city,
            "link": link if link.startswith("http") else ("https://www.jobagri.com" + link),
            "raw": desc
        })
    return offers

def fetch_adecco() -> List[Dict[str, Any]]:
    url = "https://www.adecco.fr/resultats-offres-emploi/"
    params = {"k": "saisonnier logé", "l": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .result-item, li"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Adecco",
            "employer": "Adecco",
            "city": city,
            "link": link if link.startswith("http") else ("https://www.adecco.fr" + link),
            "raw": desc
        })
    return offers

def fetch_manpower() -> List[Dict[str, Any]]:
    url = "https://www.manpower.fr/Offres"
    params = {"Keywords": "saisonnier logement", "Location": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .search-result, li"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Manpower",
            "employer": "Manpower",
            "city": city,
            "link": link if link.startswith("http") else ("https://www.manpower.fr" + link),
            "raw": desc
        })
    return offers

def fetch_randstad() -> List[Dict[str, Any]]:
    url = "https://www.randstad.fr/offres/"
    params = {"q": "saisonnier logement", "l": "Clermont-Ferrand"}
    r = safe_get(url, params)
    soup = BeautifulSoup(r.text, "lxml")
    offers = []
    for card in soup.select("article, .result-list__item, li"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        desc = card.get_text(" ", strip=True)
        city = pick_city_from_text(desc)
        offers.append({
            "title": title or "Offre Randstad",
            "employer": "Randstad",
            "city": city,
            "link": link if link.startswith("http") else ("https://www.randstad.fr" + link),
            "raw": desc
        })
    return offers

PROVIDERS = [
    fetch_france_travail,
    fetch_indeed,
    fetch_vitijob,
    fetch_saisonnier_fr,
    fetch_lhotellerie,
    fetch_anefa_jobsagri,
    fetch_adecco,
    fetch_manpower,
    fetch_randstad
]

# -------- Pipeline --------
def collect_offers() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for provider in PROVIDERS:
        try:
            items += provider()
            time.sleep(1.2)
        except Exception as e:
            print(f"[WARN] Provider {provider.__name__} failed: {e}")
    return items

def enrich_and_filter(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    geo, rate = geocoder()
    origin = geocode(ORIGIN_CITY, geo, rate)
    if not origin:
        raise RuntimeError("Impossible de géocoder la ville d'origine.")

    enriched = []
    seen_links = set()
    for it in items:
        link = it.get("link") or ""
        if not link or link in seen_links:
            continue
        seen_links.add(link)

        text = " ".join([it.get("title",""), it.get("employer",""), it.get("city",""), it.get("raw","")])
        if HOUSING_REQUIRED and not matches_housing(text):
            continue
        if not matches_sectors(text):
            continue

        coords = geocode(it.get("city") or pick_city_from_text(text), geo, rate)
        dist_km = 99999
        if coords and origin:
            dist_km = round(haversine_km(origin[0], origin[1], coords[0], coords[1]))

        phone, email = fetch_contact_details(link)
        if not phone and FALLBACK_CONTACTS and it.get("employer"):
            fb_phone, _ = fallback_contacts(it.get("employer",""), it.get("city",""))
            if fb_phone:
                phone = fb_phone

        enriched.append({
            "title": it.get("title",""),
            "employer": it.get("employer",""),
            "city": it.get("city","") or pick_city_from_text(text),
            "distance_km": dist_km,
            "link": link,
            "phone": phone,
            "email": email,
            "raw": it.get("raw",""),
            "recent_bias": -1 if looks_recent(text) else 0
        })

    enriched.sort(key=lambda x: (x["recent_bias"], x["distance_km"]))
    return enriched[:MAX_RESULTS]

def make_email(offres: List[Dict[str, Any]]) -> tuple[str, str, str]:
    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"Offres saisonnières - Clermont-Ferrand - {today}"

    lines_txt = []
    rows_html = []
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
    <p>Secteurs: {', '.join(SECTORS)} — Max: {MAX_RESULTS} — Fallback contacts: {"ON" if FALLBACK_CONTACTS else "OFF"}</p>
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