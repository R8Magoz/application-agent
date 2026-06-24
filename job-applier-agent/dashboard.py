"""International development job dashboard — multi-source fetch, score, and track applications."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin

_REQUIRED = ("streamlit", "anthropic", "requests", "feedparser", "bs4", "lxml")
for _pkg in _REQUIRED:
    _import_name = "bs4" if _pkg == "bs4" else _pkg
    try:
        __import__(_import_name)
    except ImportError:
        _pip_name = "beautifulsoup4" if _pkg == "bs4" else _pkg
        subprocess.check_call([sys.executable, "-m", "pip", "install", _pip_name, "-q"])

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    import anthropic
except ImportError:
    anthropic = None

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
PROFILE_FILE = DATA_DIR / "profile.json"
SAVED_JOBS_FILE = DATA_DIR / "saved_jobs.json"
STATUS_FILE = DATA_DIR / "application_status.json"
SETTINGS_FILE = DATA_DIR / "source_settings.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
TIMEOUT = 15

KEYWORD_PRESETS = {
    "Living Wages & Decent Work": "living wage decent work labour standards supply chain",
    "International Development": "programme officer international development sustainability",
    "UN & Multilateral": "programme officer UN ILO UNDP WHO Geneva",
}

STATUS_OPTIONS = ["Saved", "Applied", "Interview", "Offer", "Rejected", "Withdrawn"]

FetchResult = tuple[list[dict[str, Any]], str | None]


# ── Persistence ──────────────────────────────────────────────────────────────


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    ensure_data_dir()
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    ensure_data_dir()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_profile_text() -> str:
    profile = load_json(PROFILE_FILE, {})
    if not profile:
        return "International development professional interested in living wages, decent work, and sustainability."
    parts = [
        profile.get("headline", ""),
        profile.get("summary", ""),
        "Skills: " + ", ".join(profile.get("top_skills", [])),
        "Locations: " + ", ".join(profile.get("preferences", {}).get("location_preference", [])),
    ]
    return "\n".join(p for p in parts if p)


# ── Job helpers ──────────────────────────────────────────────────────────────


def normalize_job(
    title: str,
    url: str,
    source: str,
    organization: str = "",
    location: str = "",
    description: str = "",
    posted: str = "",
) -> dict[str, Any]:
    clean_url = url.strip()
    if clean_url and not clean_url.startswith("http"):
        clean_url = "https:" + clean_url if clean_url.startswith("//") else clean_url
    return {
        "id": re.sub(r"[^a-z0-9]+", "-", f"{source}-{title}-{clean_url}".lower())[:120],
        "title": title.strip(),
        "url": clean_url,
        "source": source,
        "organization": organization.strip(),
        "location": location.strip(),
        "description": description.strip(),
        "posted": posted.strip(),
        "score": None,
        "score_reason": "",
    }


def keyword_tokens(query: str) -> list[str]:
    return [t.lower() for t in re.split(r"\s+", query.strip()) if len(t) > 2]


def matches_keywords(job: dict[str, Any], query: str) -> bool:
    if not query.strip():
        return True
    haystack = " ".join(
        job.get(k, "") for k in ("title", "organization", "location", "description")
    ).lower()
    tokens = keyword_tokens(query)
    return any(t in haystack for t in tokens) if tokens else True


def location_tokens(location_filter: str) -> list[str]:
    return [t.strip().lower() for t in location_filter.split(",") if t.strip()]


LOCATION_ALIASES: dict[str, list[str]] = {
    "geneva": ["genève", "geneve", "switzerland", "ch"],
    "netherlands": ["amsterdam", "utrecht", "the hague", "den haag", "nl", "holland"],
    "remote": ["home-based", "home based", "telecommute", "virtual", "work from home", "wfh"],
}


def expand_location_tokens(tokens: list[str]) -> list[str]:
    expanded: set[str] = set()
    for token in tokens:
        expanded.add(token)
        for key, aliases in LOCATION_ALIASES.items():
            if token == key or token in aliases:
                expanded.add(key)
                expanded.update(aliases)
    return list(expanded)


def matches_location(job: dict[str, Any], location_filter: str) -> bool:
    tokens = expand_location_tokens(location_tokens(location_filter))
    if not tokens:
        return True
    haystack = " ".join(job.get(k, "") for k in ("title", "location", "description")).lower()
    return any(t in haystack for t in tokens)


def filter_jobs(
    jobs: list[dict[str, Any]], query: str, location_filter: str
) -> list[dict[str, Any]]:
    return [
        j
        for j in jobs
        if matches_keywords(j, query) and matches_location(j, location_filter)
    ]


def dedupe_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for job in jobs:
        key = (job.get("url") or "").lower() or job.get("title", "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def get_html(url: str, extra_headers: dict[str, str] | None = None) -> requests.Response:
    headers = {**HEADERS, **(extra_headers or {})}
    return requests.get(url, headers=headers, timeout=TIMEOUT)


def clean_html_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


# Sources that often block cloud/datacenter IPs — show manual URL when 0 jobs
CLOUD_IP_MANUAL_URLS: dict[str, str] = {
    "World Bank Jobs": "https://jobs.worldbank.org/en/jobs",
    "Bond": "https://jobs.bond.org.uk/",
    "Oxfam Jobs": "https://jobs.oxfam.org/vacancy/search/",
    "GIZ Jobs": "https://www.giz.de/en/jobs/index_en_jobs.html",
    "Fair Wear Foundation": "https://www.fairwear.org/about-us/vacancies/",
    "IDH Sustainable Trade": "https://www.idhsustainabletrade.com/jobs/",
}


def fetch_reliefweb(query: str, _location: str) -> FetchResult:
    """POST ReliefWeb v2 API with HTML fallback when API access fails."""
    payload: dict[str, Any] = {
        "query": {"value": query, "operator": "AND"} if query.strip() else {"value": ""},
        "limit": 50,
        "fields": {"include": ["title", "url", "source", "date", "body", "country"]},
    }
    appname = os.environ.get("RELIEFWEB_APPNAME", "job-dashboard")
    api_url = f"https://api.reliefweb.int/v2/jobs?appname={quote_plus(appname)}"

    try:
        resp = requests.post(
            api_url,
            json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            jobs = []
            for item in data.get("data", []):
                fields = item.get("fields", {})
                title = fields.get("title", "")
                url = fields.get("url", "")
                if not title or not url:
                    continue
                org = ""
                src = fields.get("source", [])
                if isinstance(src, list) and src:
                    org = src[0].get("name", "")
                loc = ", ".join(
                    c.get("name", "")
                    for c in fields.get("country", [])
                    if isinstance(c, dict) and c.get("name")
                )
                body = fields.get("body", "") or ""
                if body:
                    body = clean_html_text(body)[:1500]
                jobs.append(
                    normalize_job(
                        title=title,
                        url=url,
                        source="ReliefWeb",
                        organization=org,
                        location=loc,
                        description=body,
                        posted=(fields.get("date") or {}).get("created", ""),
                    )
                )
            if jobs:
                return jobs, None
            return [], f"v2 API returned 0 results for query '{query or 'all'}'"
    except requests.RequestException as exc:
        pass  # fall through to HTML scrape
    else:
        if resp.status_code != 200:
            pass  # fall through to HTML scrape

    try:
        url = "https://reliefweb.int/jobs"
        if query.strip():
            url += f"?search={quote_plus(query)}"
        resp = get_html(url)
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for article in soup.select("article"):
            link = article.select_one("a[href*='/job/']")
            if not link:
                continue
            title_el = article.select_one("h3, h2")
            org_el = article.select_one(".rw-river-article__source, .source")
            jobs.append(
                normalize_job(
                    title=title_el.get_text(strip=True) if title_el else link.get_text(strip=True),
                    url=urljoin("https://reliefweb.int", link["href"]),
                    source="ReliefWeb",
                    organization=org_el.get_text(strip=True) if org_el else "",
                )
            )
        jobs = filter_jobs(jobs, query, "")
        if jobs:
            return jobs, None
        return [], f"HTML fallback returned 0 results for query '{query or 'all'}'"
    except requests.RequestException as exc:
        return [], str(exc)


def fetch_linkedin(query: str, location: str) -> FetchResult:
    loc = location.split(",")[0].strip() or "Netherlands"
    max_results = 25
    guest_url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(query)}&location={quote_plus(loc)}&start=0&count={max_results}"
    )

    def parse_linkedin_html(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "lxml")
        jobs = []
        for card in soup.select("li"):
            link = card.select_one("a[href*='/jobs/view/']")
            if not link:
                continue
            title_el = card.select_one(".base-search-card__title, h3")
            company_el = card.select_one(".base-search-card__subtitle, h4")
            loc_el = card.select_one(".job-search-card__location")
            href = link.get("href", "")
            if href.startswith("/"):
                href = urljoin("https://www.linkedin.com", href)
            jobs.append(
                normalize_job(
                    title=title_el.get_text(strip=True) if title_el else "",
                    url=href,
                    source="LinkedIn",
                    organization=company_el.get_text(strip=True) if company_el else "",
                    location=loc_el.get_text(strip=True) if loc_el else "",
                )
            )
            if len(jobs) >= max_results:
                break
        return [j for j in jobs if j["title"] and j["url"]]

    try:
        resp = get_html(guest_url)
        jobs = parse_linkedin_html(resp.text)
        if jobs:
            return jobs[:max_results], None
    except requests.RequestException as exc:
        first_error = str(exc)
    else:
        first_error = f"guest API returned 0 cards (HTTP {resp.status_code})"

    time.sleep(2)

    fallback_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(query)}&location={quote_plus(loc)}&f_TPR=r604800"
    )
    try:
        resp = get_html(fallback_url)
        jobs = parse_linkedin_html(resp.text)[:max_results]
        if jobs:
            return jobs, None
        return [], f"{first_error}; fallback page also returned 0 jobs"
    except requests.RequestException as exc:
        return [], f"{first_error}; fallback failed: {exc}"


def fetch_rss_urls(source_name: str, urls: list[str], query: str, location: str) -> FetchResult:
    errors: list[str] = []
    for url in urls:
        try:
            resp = get_html(url)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            errors.append(f"{url}: RSS parse error ({feed.bozo_exception})")
            continue
        content = resp.text[:500].lower()
        if "<html" in content and not feed.entries:
            errors.append(f"{url}: returned HTML instead of RSS")
            continue
        jobs = []
        for entry in feed.entries:
            title = getattr(entry, "title", "") or ""
            link = getattr(entry, "link", "") or ""
            if not title or not link:
                continue
            summary = ""
            if getattr(entry, "summary", ""):
                summary = clean_html_text(entry.summary)[:1000]
            org = getattr(entry, "author", "") or ""
            posted = getattr(entry, "published", "") or ""
            jobs.append(
                normalize_job(
                    title=title,
                    url=link,
                    source=source_name,
                    organization=org,
                    description=summary,
                    posted=posted,
                )
            )
        jobs = filter_jobs(jobs, query, location)
        if jobs:
            return jobs, None
        errors.append(f"{url}: 0 entries matched filters")
    return [], "; ".join(errors) if errors else "No RSS entries found"


def fetch_un_jobs(query: str, location: str) -> FetchResult:
    return fetch_rss_urls(
        "UN Jobs",
        ["https://unjobs.org/feeds/duty-station/geneva", "https://unjobs.org/feeds/"],
        query,
        location,
    )


def fetch_ilo(query: str, location: str) -> FetchResult:
    url = f"https://jobs.ilo.org/job-search-results/?keyword={quote_plus(query)}"
    try:
        resp = get_html(url)
        if resp.status_code >= 400:
            return [], f"HTTP {resp.status_code}"
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if len(title) < 8:
                continue
            if any(
                token in href.lower()
                for token in ("job", "requisition", "vacanc", "opening", "posting")
            ):
                jobs.append(
                    normalize_job(
                        title=title,
                        url=urljoin("https://jobs.ilo.org", href),
                        source="ILO Jobs",
                        organization="ILO",
                    )
                )
        for row in soup.select("tr, li, article, [class*='job'], [class*='result']"):
            link = row.select_one("a[href]")
            if not link:
                continue
            title_el = row.select_one("h2, h3, h4, .job-title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            if len(title) < 8:
                continue
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin("https://jobs.ilo.org", link["href"]),
                    source="ILO Jobs",
                    organization="ILO",
                    location=row.get_text(" ", strip=True)[:120],
                )
            )
        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        return jobs, None
    except requests.RequestException as exc:
        return [], str(exc)


def fetch_devnetjobs(query: str, location: str) -> FetchResult:
    jobs, err = fetch_rss_urls(
        "DevNetJobs",
        ["https://devnetjobs.org/rss/jobs.rss", "https://www.devnetjobs.org/rss/jobs.rss"],
        query,
        location,
    )
    if jobs:
        return jobs, None

    errors = [err] if err else []
    try:
        resp = get_html("https://devnetjobs.org/")
        soup = BeautifulSoup(resp.text, "lxml")
        scraped = []
        for link in soup.select("a[href*='jobdescription.aspx']"):
            title = link.get_text(strip=True)
            if len(title) < 10:
                continue
            scraped.append(
                normalize_job(
                    title=title[:120],
                    url=urljoin("https://devnetjobs.org/", link["href"]),
                    source="DevNetJobs",
                    organization="",
                )
            )
        scraped = filter_jobs(dedupe_jobs(scraped), query, location)
        if scraped:
            return scraped, None
        errors.append("homepage scrape returned 0 matching jobs")
    except requests.RequestException as exc:
        errors.append(f"homepage scrape: {exc}")
    return [], "; ".join(e for e in errors if e)


# ── GROUP B fetchers ─────────────────────────────────────────────────────────


def fetch_oecd(query: str, location: str) -> FetchResult:
    url = "https://careers.smartrecruiters.com/OECD/oecd---en"
    try:
        resp = get_html(url)
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for item in soup.select("li.opening-job"):
            link = item.select_one("a")
            if not link:
                continue
            title_el = item.select_one(".job-title, h4")
            dept_el = item.select_one(".job-department")
            loc_el = item.select_one(".job-desc")
            jobs.append(
                normalize_job(
                    title=title_el.get_text(strip=True) if title_el else link.get_text(strip=True),
                    url=link["href"],
                    source="OECD Careers",
                    organization="OECD",
                    location=loc_el.get_text(strip=True) if loc_el else "",
                    description=dept_el.get_text(strip=True) if dept_el else "",
                )
            )
        jobs = filter_jobs(jobs, query, location)
        return jobs, None
    except requests.RequestException as exc:
        return [], str(exc)


def fetch_worldbank(query: str, location: str) -> FetchResult:
    primary = f"https://jobs.worldbank.org/en/jobs?term={quote_plus(query)}"
    fallbacks = [
        primary,
        "https://worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup",
        "https://www.worldbank.org/ext/en/careers",
    ]
    errors: list[str] = []

    for url in fallbacks:
        try:
            resp = get_html(url)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for item in soup.select("li.opening-job, [class*='job-listing'], [class*='job-card'], article"):
            link = item.select_one("a[href]")
            if not link:
                continue
            title_el = item.select_one("h2, h3, h4, .job-title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            if len(title) < 5:
                continue
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin(url, link["href"]),
                    source="World Bank Jobs",
                    organization="World Bank Group",
                    location=item.get_text(" ", strip=True)[:120],
                )
            )

        if not jobs:
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                if len(text) < 12:
                    continue
                if any(x in href.lower() for x in ("requisition", "/job", "position")):
                    jobs.append(
                        normalize_job(
                            title=text,
                            url=urljoin(url, href),
                            source="World Bank Jobs",
                            organization="World Bank Group",
                        )
                    )

        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        if jobs:
            return jobs, None
        errors.append(f"{url}: page loaded but 0 jobs matched filters")

    return [], "; ".join(errors) if errors else "No World Bank listings found"


def fetch_undp(query: str, location: str) -> FetchResult:
    urls = [
        "https://jobs.undp.org/cj_jobs.cfm",
        "https://jobs.undp.org/cj_view_jobs.cfm",
    ]
    errors: list[str] = []

    for url in urls:
        try:
            resp = get_html(url)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if resp.status_code == 404:
            errors.append(f"{url}: not found (404)")
            continue
        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for row in soup.select("table tr"):
            link = row.select_one("a[href]")
            if not link:
                continue
            title = link.get_text(strip=True)
            if len(title) < 5:
                continue
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin(url, link["href"]),
                    source="UNDP Jobs",
                    organization="UNDP",
                )
            )

        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(" ", strip=True)
            if "oraclecloud.com" in href or "CandidateExperience" in href:
                title = re.sub(r"^Job Title", "", text)
                title = re.split(r"Post level|Apply by", title)[0].strip()
                if len(title) < 5:
                    continue
                jobs.append(
                    normalize_job(title=title, url=href, source="UNDP Jobs", organization="UNDP")
                )

        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        if jobs:
            return jobs, None
        errors.append(f"{url}: 0 rows matched query")

    return [], "; ".join(errors)


def fetch_epso(query: str, location: str) -> FetchResult:
    url = "https://epso.europa.eu/en/job-opportunities/open-competitions"
    try:
        resp = get_html(url)
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for card in soup.select("article, .ecl-content-block, li, div"):
            link = card.select_one("a[href*='competition'], a[href*='competitions/']")
            if not link:
                continue
            title = link.get_text(strip=True)
            if len(title) < 8:
                continue
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin("https://epso.europa.eu", link["href"]),
                    source="EU Careers (EPSO)",
                    organization="European Commission / EPSO",
                )
            )
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if "competition" in href and len(title) > 10:
                jobs.append(
                    normalize_job(
                        title=title,
                        url=urljoin("https://epso.europa.eu", href),
                        source="EU Careers (EPSO)",
                        organization="EPSO",
                    )
                )
        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        return jobs, None
    except requests.RequestException as exc:
        return [], str(exc)


def fetch_impactpool(query: str, location: str) -> FetchResult:
    url = f"https://www.impactpool.org/search?q={quote_plus(query)}"
    try:
        resp = get_html(url, {"Referer": "https://www.impactpool.org/", "Accept": "text/html"})
        if resp.status_code == 403:
            return [], "HTTP 403 — access blocked without browser session"
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for link in soup.select("a[href*='/jobs/']"):
            href = link.get("href", "")
            if not re.search(r"/jobs/\d+", href):
                continue
            card = link.find_parent(["div", "article", "li"]) or link
            text = card.get_text(" ", strip=True)
            org = ""
            if " - " in text:
                parts = text.split(" - ", 1)
                title, org = parts[0], parts[1][:80]
            else:
                title = link.get_text(strip=True) or text[:100]
            jobs.append(
                normalize_job(
                    title=title.strip(),
                    url=urljoin("https://www.impactpool.org", href),
                    source="Impactpool",
                    organization=org,
                    location=location.split(",")[0].strip(),
                )
            )
        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        return jobs, None
    except requests.RequestException as exc:
        return [], str(exc)


# ── GROUP C fetchers ─────────────────────────────────────────────────────────


def scrape_job_links(
    source_name: str,
    page_url: str,
    organization: str,
    query: str,
    location: str,
    link_selectors: list[str],
    base_url: str | None = None,
) -> FetchResult:
    base = base_url or page_url
    try:
        resp = get_html(page_url, {"Referer": base})
        if resp.status_code == 403:
            return [], "HTTP 403 — bot protection active"
        if resp.status_code >= 400:
            return [], f"HTTP {resp.status_code}"
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []
        for selector in link_selectors:
            for link in soup.select(selector):
                title = link.get_text(strip=True)
                href = link.get("href", "")
                if len(title) < 8 or not href or href.startswith("#"):
                    continue
                jobs.append(
                    normalize_job(
                        title=title,
                        url=urljoin(base, href),
                        source=source_name,
                        organization=organization,
                    )
                )
        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        return jobs, None
    except requests.RequestException as exc:
        return [], str(exc)


def fetch_devex(query: str, location: str) -> FetchResult:
    return scrape_job_links(
        "Devex Jobs",
        f"https://www.devex.com/jobs/search?q={quote_plus(query)}",
        "Devex",
        query,
        location,
        ["a[href*='/jobs/']", "article a", ".job-card a"],
        "https://www.devex.com",
    )


def fetch_globaljobs(query: str, location: str) -> FetchResult:
    jobs, err = scrape_job_links(
        "Global Jobs",
        f"https://globaljobs.org/?s={quote_plus(query)}",
        "Global Jobs",
        query,
        location,
        ["h2 a", ".entry-title a", "a[href*='/jobs/']"],
        "https://globaljobs.org",
    )
    if jobs:
        return jobs, None
    rss_jobs, _ = fetch_rss_urls(
        "Global Jobs",
        ["https://www.globaljobs.org/jobs/feed.rss"],
        query,
        location,
    )
    if rss_jobs:
        return rss_jobs, None
    return [], err or "No results from search page or RSS feed"


def fetch_idealist(query: str, location: str) -> FetchResult:
    url = f"https://www.idealist.org/en/jobs?q={quote_plus(query)}&type=JOB"
    try:
        resp = get_html(url)
        soup = BeautifulSoup(resp.text, "lxml")
        jobs = []

        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            data = json.loads(nd.string)

            def walk(obj: Any) -> None:
                if isinstance(obj, dict):
                    title = obj.get("title") or obj.get("name")
                    link = obj.get("url") or obj.get("publicUrl") or obj.get("slug")
                    org = obj.get("organizationName") or obj.get("orgName") or ""
                    if title and link and isinstance(title, str) and len(title) > 8:
                        if isinstance(link, str) and not link.startswith("http"):
                            link = urljoin("https://www.idealist.org", link)
                        if isinstance(link, str) and link.startswith("http"):
                            jobs.append(
                                normalize_job(
                                    title=title,
                                    url=link,
                                    source="Idealist",
                                    organization=str(org),
                                )
                            )
                    for value in obj.values():
                        walk(value)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item)

            walk(data)

        for link in soup.select("a[href*='/jobs/']"):
            title = link.get_text(strip=True)
            if len(title) > 8:
                jobs.append(
                    normalize_job(
                        title=title,
                        url=urljoin("https://www.idealist.org", link["href"]),
                        source="Idealist",
                        organization="",
                    )
                )

        jobs = filter_jobs(dedupe_jobs(jobs), query, location)
        return jobs, None
    except (requests.RequestException, json.JSONDecodeError) as exc:
        return [], str(exc)


def fetch_bond(query: str, location: str) -> FetchResult:
    return scrape_job_links(
        "Bond",
        f"https://jobs.bond.org.uk/?search={quote_plus(query)}",
        "Bond",
        query,
        location,
        ["a[href*='job']", "article a", ".job-title a", "h2 a"],
        "https://jobs.bond.org.uk",
    )


def fetch_oxfam(query: str, location: str) -> FetchResult:
    urls = [
        f"https://jobs.oxfam.org/vacancy/search/?q={quote_plus(query)}",
        f"https://www.oxfam.org/en/jobs?q={quote_plus(query)}",
    ]
    errors: list[str] = []
    for url in urls:
        jobs, err = scrape_job_links(
            "Oxfam Jobs",
            url,
            "Oxfam",
            query,
            location,
            ["a[href*='vacancy']", "a[href*='job']", "article a"],
            url,
        )
        if jobs:
            return jobs, None
        if err:
            errors.append(f"{url}: {err}")
    return [], "; ".join(errors) if errors else "Oxfam jobs unavailable"


def fetch_giz(query: str, location: str) -> FetchResult:
    urls = [
        "https://www.giz.de/en/jobs/index_en_jobs.html",
        "https://www.giz.de/en/career/jobs",
    ]
    errors: list[str] = []
    for url in urls:
        jobs, err = scrape_job_links(
            "GIZ Jobs",
            url,
            "GIZ",
            query,
            location,
            ["a[href*='job']", "a[href*='stelle']", "a[href*='position']"],
            "https://www.giz.de",
        )
        if jobs:
            return jobs, None
        if err:
            errors.append(f"{url}: {err}")
    return [], "; ".join(errors)


def fetch_fairwear(query: str, location: str) -> FetchResult:
    return scrape_job_links(
        "Fair Wear Foundation",
        "https://www.fairwear.org/about-us/vacancies/",
        "Fair Wear Foundation",
        query,
        location,
        ["a[href*='vacanc']", "a[href*='job']", "article a", "li a"],
        "https://www.fairwear.org",
    )


def fetch_idh(query: str, location: str) -> FetchResult:
    return scrape_job_links(
        "IDH Sustainable Trade",
        "https://www.idhsustainabletrade.com/jobs/",
        "IDH",
        query,
        location,
        ["a[href*='job']", "a[href*='career']", "h3 a"],
        "https://www.idhsustainabletrade.com",
    )


# ── Source registry ───────────────────────────────────────────────────────────

SourceConfig = dict[str, Any]
FetchFn = Callable[[str, str], FetchResult]

GROUP_A: dict[str, SourceConfig] = {
    "ReliefWeb": {"group": "A", "enabled": True, "fetch": fetch_reliefweb},
    "LinkedIn": {"group": "A", "enabled": True, "fetch": fetch_linkedin},
    "UN Jobs": {"group": "A", "enabled": True, "fetch": fetch_un_jobs},
    "ILO Jobs": {"group": "A", "enabled": True, "fetch": fetch_ilo},
    "DevNetJobs": {"group": "A", "enabled": True, "fetch": fetch_devnetjobs},
}

GROUP_B: dict[str, SourceConfig] = {
    "OECD Careers": {"group": "B", "enabled": True, "fetch": fetch_oecd},
    "World Bank Jobs": {"group": "B", "enabled": True, "fetch": fetch_worldbank},
    "UNDP Jobs": {"group": "B", "enabled": True, "fetch": fetch_undp},
    "EU Careers (EPSO)": {"group": "B", "enabled": True, "fetch": fetch_epso},
    "Impactpool": {"group": "B", "enabled": True, "fetch": fetch_impactpool},
}

GROUP_C: dict[str, SourceConfig] = {
    "Devex Jobs": {"group": "C", "enabled": False, "fetch": fetch_devex},
    "Global Jobs": {"group": "C", "enabled": False, "fetch": fetch_globaljobs},
    "Idealist": {"group": "C", "enabled": False, "fetch": fetch_idealist},
    "Bond": {"group": "C", "enabled": False, "fetch": fetch_bond},
    "Oxfam Jobs": {"group": "C", "enabled": False, "fetch": fetch_oxfam},
    "GIZ Jobs": {"group": "C", "enabled": False, "fetch": fetch_giz},
    "Fair Wear Foundation": {"group": "C", "enabled": False, "fetch": fetch_fairwear},
    "IDH Sustainable Trade": {"group": "C", "enabled": False, "fetch": fetch_idh},
}

ALL_SOURCES: dict[str, SourceConfig] = {**GROUP_A, **GROUP_B, **GROUP_C}


# ── Scoring ──────────────────────────────────────────────────────────────────

SCORE_BANDS = [
    {
        "emoji": "🔴",
        "range": "0–2",
        "short": "Poor match",
        "hint": "do not apply",
        "word": "POOR",
        "min": 0.0,
        "max": 2.99,
        "bg": "#c0392b",
    },
    {
        "emoji": "🟠",
        "range": "3–4",
        "short": "Weak match",
        "hint": "significant gaps",
        "word": "WEAK",
        "min": 3.0,
        "max": 4.99,
        "bg": "#e67e22",
    },
    {
        "emoji": "🟡",
        "range": "5–6",
        "short": "Partial match",
        "hint": "read carefully",
        "word": "PARTIAL",
        "min": 5.0,
        "max": 6.99,
        "bg": "#f1c40f",
        "text": "#1a1a1a",
    },
    {
        "emoji": "🟢",
        "range": "7–8",
        "short": "Strong match",
        "hint": "worth applying",
        "word": "STRONG",
        "min": 7.0,
        "max": 8.99,
        "bg": "#27ae60",
    },
    {
        "emoji": "⭐",
        "range": "9–10",
        "short": "Perfect match",
        "hint": "apply immediately",
        "word": "PERFECT",
        "min": 9.0,
        "max": 10.0,
        "bg": "#6c3483",
    },
]

SCORING_RUBRIC = """Score each job 0–10 using this rubric:
  10 = candidate meets every requirement, ideal seniority, preferred location, domain is an exact match
  7–9 = strong match, minor gaps only
  5–6 = relevant background but missing 1–2 key requirements
  3–4 = adjacent field, transferable but not direct
  0–2 = different sector or seniority mismatch

Be strict. Reserve 9–10 only for roles that read like they were written for this candidate's exact profile.

Score bands for display:
  9–10 = PERFECT — apply immediately
  7–8  = STRONG — worth applying
  5–6  = PARTIAL — read carefully before applying
  3–4  = WEAK — significant gaps
  0–2  = POOR — do not apply"""


def get_score_band(score: float) -> dict[str, Any]:
    for band in SCORE_BANDS:
        if band["min"] <= score <= band["max"]:
            return band
    return SCORE_BANDS[0]


def render_score_legend() -> None:
    st.markdown("**Match score key (0–10)**")
    cells = []
    for band in SCORE_BANDS:
        text_color = band.get("text", "white")
        cells.append(
            f'<div style="flex:1;background:{band["bg"]};color:{text_color};padding:10px 6px;'
            f'text-align:center;font-size:0.85rem;line-height:1.35">'
            f'<strong>{band["emoji"]} {band["range"]}</strong><br>'
            f'{band["short"]}<br>'
            f'<span style="opacity:0.9;font-size:0.75rem">{band["hint"]}</span></div>'
        )
    html = (
        '<div style="display:flex;gap:2px;border-radius:8px;overflow:hidden;margin:4px 0 14px 0">'
        + "".join(cells)
        + "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def format_score_badge_html(score: float | None) -> str:
    if score is None or not isinstance(score, (int, float)):
        return '<span style="opacity:0.7">⬜ not scored</span>'
    band = get_score_band(float(score))
    text_color = band.get("text", "white")
    return (
        f'<span style="background:{band["bg"]};color:{text_color};padding:4px 12px;'
        f'border-radius:6px;font-weight:700;font-size:0.95rem;white-space:nowrap">'
        f'{band["emoji"]} {float(score):.1f} {band["word"]}</span>'
    )


def get_anthropic_client() -> Any | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        if "anthropic_api_key" in st.secrets:
            api_key = st.secrets["anthropic_api_key"]
    except Exception:
        pass
    if not api_key or anthropic is None:
        return None
    return anthropic.Anthropic(api_key=api_key)


def build_scoring_prompt(jobs: list[dict[str, Any]], profile: str) -> str:
    lines = [
        SCORING_RUBRIC,
        'Return ONLY valid JSON: [{"index": 0, "score": 7.5, "reason": "short reason"}, ...]',
        f"Candidate profile:\n{profile}",
        "Jobs to score:",
    ]
    for idx, job in enumerate(jobs):
        lines.append(
            f"{idx}. {job['title']} | {job.get('organization') or job['source']} | "
            f"{job.get('location', '')} | {job['url']}"
        )
        if job.get("description"):
            lines.append(f"   {job['description'][:400]}")
    return "\n".join(lines)


def score_batch(client: Any, jobs: list[dict[str, Any]], profile: str) -> None:
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        messages=[{"role": "user", "content": build_scoring_prompt(jobs, profile)}],
    )
    text = message.content[0].text if message.content else "[]"
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise ValueError("No JSON array in Claude response")
    parsed = json.loads(match.group())
    score_map = {item.get("index"): item for item in parsed if isinstance(item, dict)}
    for idx, job in enumerate(jobs):
        item = score_map.get(idx, {})
        raw_score = item.get("score")
        job["score"] = float(raw_score) if raw_score is not None else None
        job["score_reason"] = item.get("reason", "")


def score_jobs(jobs: list[dict[str, Any]], profile: str, batch_size: int = 10) -> list[dict[str, Any]]:
    client = get_anthropic_client()
    if not client:
        for job in jobs:
            job["score"] = None
            job["score_reason"] = "No ANTHROPIC_API_KEY configured"
        return jobs

    total_batches = max(1, (len(jobs) + batch_size - 1) // batch_size)
    progress = st.progress(0.0, text="Scoring jobs with Claude...")

    for batch_idx, start in enumerate(range(0, len(jobs), batch_size)):
        batch = jobs[start : start + batch_size]
        try:
            score_batch(client, batch, profile)
        except Exception as exc:
            for job in batch:
                job["score"] = None
                job["score_reason"] = f"Scoring failed: {exc}"
        progress.progress((batch_idx + 1) / total_batches, text=f"Scored batch {batch_idx + 1}/{total_batches}")

    progress.empty()
    return jobs


# ── Session / scan ───────────────────────────────────────────────────────────


def init_session_state() -> None:
    defaults = {
        "jobs": [],
        "unscored_jobs": [],
        "scan_log": [],
        "search_query": "",
        "location_filter": "Netherlands, Geneva, Remote",
        "source_settings": load_json(SETTINGS_FILE, {}),
        "saved_jobs": load_json(SAVED_JOBS_FILE, []),
        "application_status": load_json(STATUS_FILE, {}),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def source_enabled(name: str, config: SourceConfig) -> bool:
    settings = st.session_state.get("source_settings", {})
    if name in settings:
        return bool(settings[name])
    return bool(config.get("enabled", False))


def run_scan(query: str, location: str, profile: str, do_score: bool) -> None:
    enabled = {n: c for n, c in ALL_SOURCES.items() if source_enabled(n, c)}
    if not enabled:
        st.warning("No sources enabled. Open the Sources tab to turn some on.")
        return

    log_box = st.empty()
    all_jobs: list[dict[str, Any]] = []
    scan_log: list[str] = []

    for name, config in enabled.items():
        scan_log.append(f"⏳ {name}: fetching...")
        log_box.code("\n".join(scan_log))
        fetch_fn: FetchFn = config["fetch"]
        try:
            jobs, error = fetch_fn(query, location)
        except Exception as exc:
            jobs, error = [], str(exc)

        if name in CLOUD_IP_MANUAL_URLS and not jobs:
            manual_url = CLOUD_IP_MANUAL_URLS[name]
            scan_log[-1] = (
                f"⚠️ {name}: 0 jobs — site may block cloud IPs. Open manually: {manual_url}"
            )
        elif error:
            scan_log[-1] = f"❌ {name}: failed ({error})"
        else:
            scan_log[-1] = f"✅ {name}: {len(jobs)} jobs"
        log_box.code("\n".join(scan_log))
        all_jobs.extend(jobs)

    before = len(all_jobs)
    all_jobs = dedupe_jobs(all_jobs)
    after = len(all_jobs)
    if before > after:
        scan_log.append(f"🔄 Deduplicated: {before} → {after} unique jobs")
    elif after:
        scan_log.append(f"📊 Total: {after} unique jobs from {len(enabled)} sources")
    else:
        scan_log.append(
            f"⚠️ Total: 0 unique jobs for '{query or 'all'}' — see per-source errors above"
        )
    log_box.code("\n".join(scan_log))

    unscored: list[dict[str, Any]] = []
    if do_score and all_jobs:
        all_jobs = score_jobs(all_jobs, profile)
        unscored = [j for j in all_jobs if j.get("score") is None]

    st.session_state.jobs = all_jobs
    st.session_state.unscored_jobs = unscored
    st.session_state.scan_log = scan_log
    st.session_state.search_query = query


# ── UI components ────────────────────────────────────────────────────────────


def render_job_card(job: dict[str, Any], prefix: str = "") -> None:
    score = job.get("score")
    badge = format_score_badge_html(score if isinstance(score, (int, float)) else None)
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:4px">'
        f'<span style="font-size:1.1rem;font-weight:600">{job["title"]}</span>'
        f"{badge}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"{job.get('organization') or '—'} · {job.get('location') or '—'} · {job.get('source', '')}"
    )
    if job.get("score_reason"):
        st.caption(job["score_reason"])
    if job.get("description"):
        desc = job["description"]
        st.write(desc[:350] + ("…" if len(desc) > 350 else ""))

    c1, c2, c3 = st.columns(3)
    with c1:
        st.link_button("View job", job["url"], use_container_width=True)
    with c2:
        if st.button("Save", key=f"{prefix}save-{job['id']}"):
            saved = st.session_state.saved_jobs
            if not any(s.get("url") == job["url"] for s in saved):
                saved.append({**job, "saved_at": datetime.now(timezone.utc).isoformat()})
                st.session_state.saved_jobs = saved
                save_json(SAVED_JOBS_FILE, saved)
                st.toast("Saved")
    with c3:
        current = st.session_state.application_status.get(job["url"], "Saved")
        new_status = st.selectbox(
            "Status",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(current),
            key=f"{prefix}status-{job['id']}",
        )
        if new_status != current:
            st.session_state.application_status[job["url"]] = new_status
            save_json(STATUS_FILE, st.session_state.application_status)


def render_sidebar() -> tuple[str, str, float, str]:
    st.sidebar.header("Search")
    for label, keywords in KEYWORD_PRESETS.items():
        if st.sidebar.button(label, use_container_width=True):
            st.session_state.search_query = keywords

    query = st.sidebar.text_input(
        "Keywords",
        value=st.session_state.get("search_query", ""),
        key="sidebar_keywords",
    )
    st.session_state.search_query = query

    location = st.sidebar.text_input(
        "Location filter",
        value=st.session_state.get("location_filter", "Netherlands, Geneva, Remote"),
    )
    st.session_state.location_filter = location

    min_score = st.sidebar.slider("Min score", 0.0, 10.0, 4.0, 0.5)
    do_score = st.sidebar.checkbox("Score with Claude", value=True)

    st.sidebar.markdown("---")
    st.sidebar.caption(f"Profile: `{PROFILE_FILE}`")
    st.sidebar.caption("Set `ANTHROPIC_API_KEY` for scoring.")

    return query, location, min_score, do_score


def render_scan_tab(profile: str, query: str, location: str, min_score: float, do_score: bool) -> None:
    render_score_legend()
    st.subheader("Scan for jobs")
    if st.button("Start scan", type="primary"):
        run_scan(query, location, profile, do_score)

    if st.session_state.scan_log:
        with st.expander("Scan log", expanded=True):
            st.code("\n".join(st.session_state.scan_log))

    jobs = st.session_state.jobs
    scored = []
    for job in jobs:
        s = job.get("score")
        if s is None:
            continue
        if isinstance(s, (int, float)) and s >= min_score:
            scored.append(job)

    if scored:
        st.markdown(f"### Results ({len(scored)} jobs ≥ {min_score})")
        for job in sorted(scored, key=lambda j: j.get("score") or 0, reverse=True):
            with st.container(border=True):
                render_job_card(job)

    unscored = st.session_state.get("unscored_jobs") or [j for j in jobs if j.get("score") is None]
    if unscored:
        st.markdown("### Not scored yet")
        st.caption("These jobs were fetched but Claude scoring failed or was skipped.")
        for job in unscored:
            with st.container(border=True):
                render_job_card(job, prefix="unscored-")

    if not jobs and st.session_state.scan_log:
        st.info("No jobs to show. Check the scan log for per-source details.")


def render_sources_tab() -> None:
    st.subheader("Sources")
    settings = dict(st.session_state.get("source_settings", {}))

    for group_label, group in [("Group A — API / RSS", GROUP_A), ("Group B — Web scrape", GROUP_B), ("Group C — Secondary", GROUP_C)]:
        st.markdown(f"#### {group_label}")
        for name, config in group.items():
            default = settings.get(name, config.get("enabled", False))
            settings[name] = st.toggle(name, value=default, key=f"src-{name}")

    if st.button("Save source settings"):
        st.session_state.source_settings = settings
        save_json(SETTINGS_FILE, settings)
        st.success("Saved")


def render_saved_tab() -> None:
    st.subheader("Saved jobs")
    saved = st.session_state.saved_jobs
    if not saved:
        st.info("No saved jobs yet.")
        return
    for job in saved:
        with st.container(border=True):
            render_job_card(job, prefix="saved-")
    if st.button("Clear saved jobs"):
        st.session_state.saved_jobs = []
        save_json(SAVED_JOBS_FILE, [])
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="Job Dashboard", page_icon="🌍", layout="wide")
    ensure_data_dir()
    init_session_state()

    profile = load_profile_text()
    query, location, min_score, do_score = render_sidebar()

    st.title("🌍 International Development Job Dashboard")
    st.caption("Living wages · Decent work · Supply chain sustainability · UN & multilateral")

    tab_scan, tab_saved, tab_sources = st.tabs(["Scan", "Saved Jobs", "Sources"])
    with tab_scan:
        render_scan_tab(profile, query, location, min_score, do_score)
    with tab_saved:
        render_saved_tab()
    with tab_sources:
        render_sources_tab()


if __name__ == "__main__":
    main()
