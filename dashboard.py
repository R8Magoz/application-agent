"""Job search dashboard for international development and decent work roles."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

# Install dependencies if missing
_REQUIRED_PACKAGES = ("feedparser", "beautifulsoup4", "streamlit", "anthropic", "requests")
for _package in _REQUIRED_PACKAGES:
    try:
        __import__(_package.replace("beautifulsoup4", "bs4"))
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", _package, "-q"])

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    import anthropic
except ImportError:
    anthropic = None

DATA_DIR = Path(__file__).parent / "data"
SAVED_JOBS_FILE = DATA_DIR / "saved_jobs.json"
STATUS_FILE = DATA_DIR / "application_status.json"
SETTINGS_FILE = DATA_DIR / "dashboard_settings.json"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

KEYWORD_PRESETS = {
    "Living Wages & Decent Work": "living wage decent work labour standards supply chain",
    "International Development": "programme officer international development sustainability NGO",
    "UN & Multilateral": "programme officer united nations ILO UNDP Geneva",
    "Custom": "",
}

PRIMARY_SOURCES = {
    "ReliefWeb (API)": {"type": "reliefweb_api", "enabled": True},
    "ILO Jobs (RSS)": {"type": "rss", "url": "https://jobs.ilo.org/rss/vacancies.rss", "enabled": True},
    "Devex (RSS)": {"type": "rss", "url_template": "https://www.devex.com/jobs/search.rss?q={query}", "enabled": True},
    "UN Jobs (RSS)": {"type": "rss", "url": "https://unjobs.org/feeds/duty-station/geneva", "enabled": True},
    "OECD Careers (web)": {"type": "oecd", "url": "https://careers.smartrecruiters.com/OECD/oecd---en", "enabled": True},
    "World Bank Jobs (web)": {"type": "worldbank", "url": "https://jobs.worldbank.org/en/jobs", "enabled": True},
    "UNDP Jobs (web)": {"type": "undp", "url": "https://jobs.undp.org/cj_jobs.cfm", "enabled": True},
}

SECONDARY_SOURCES = {
    "EU Careers / EPSO (RSS)": {
        "type": "rss",
        "url": "https://epso.europa.eu/en/job-opportunities/open-competitions/rss",
        "enabled": False,
    },
    "Indeed Netherlands (RSS)": {
        "type": "rss",
        "url_template": "https://www.indeed.com/rss?q={query}&l=Netherlands",
        "enabled": False,
    },
    "ReliefWeb RSS": {
        "type": "rss",
        "url_template": "https://reliefweb.int/jobs/rss.xml?search={query}",
        "enabled": False,
    },
    "Euractiv Jobs (web)": {"type": "euractiv", "url": "https://jobs.euractiv.com", "enabled": False},
    "GIZ Jobs (web)": {"type": "giz", "url": "https://jobs.giz.de", "enabled": False},
    "Oxfam Jobs (web)": {"type": "oxfam", "url": "https://jobs.oxfam.org", "enabled": False},
    "IDH Sustainable Trade (web)": {
        "type": "idh",
        "url": "https://www.idhsustainabletrade.com/jobs",
        "enabled": False,
    },
    "BSR Jobs (web)": {"type": "bsr", "url": "https://www.bsr.org/en/about/jobs", "enabled": False},
    "Fair Wear Foundation (web)": {
        "type": "fairwear",
        "url": "https://www.fairwear.org/about-us/vacancies",
        "enabled": False,
    },
    "Rainforest Alliance (web)": {
        "type": "rainforest",
        "url": "https://www.rainforest-alliance.org/careers",
        "enabled": False,
    },
}

STATUS_OPTIONS = ["Saved", "Applied", "Interview", "Offer", "Rejected", "Withdrawn"]


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    ensure_data_dir()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path: Path, data: Any) -> None:
    ensure_data_dir()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_job(
    title: str,
    url: str,
    source: str,
    organization: str = "",
    location: str = "",
    description: str = "",
    posted: str = "",
) -> dict[str, Any]:
    return {
        "id": re.sub(r"[^a-z0-9]+", "-", f"{source}-{title}-{url}".lower())[:120],
        "title": title.strip(),
        "url": url.strip(),
        "source": source,
        "organization": organization.strip(),
        "location": location.strip(),
        "description": description.strip(),
        "posted": posted.strip(),
        "score": None,
        "score_reason": "",
    }


def keyword_tokens(query: str) -> list[str]:
    return [token.lower() for token in re.split(r"\s+", query.strip()) if len(token) > 2]


def matches_query(job: dict[str, Any], query: str) -> bool:
    if not query.strip():
        return True
    haystack = " ".join(
        [
            job.get("title", ""),
            job.get("organization", ""),
            job.get("location", ""),
            job.get("description", ""),
        ]
    ).lower()
    tokens = keyword_tokens(query)
    if not tokens:
        return True
    return any(token in haystack for token in tokens)


def dedupe_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for job in jobs:
        key = job.get("url") or job.get("title", "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def fetch_url(url: str, timeout: int = 30) -> requests.Response:
    return requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)


def parse_rss_content(content: str | bytes, source_name: str) -> tuple[list[dict[str, Any]], str | None]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        exc = getattr(feed, "bozo_exception", None)
        return [], f"RSS parse error: {exc}"

    content_text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content
    if "<html" in content_text[:500].lower() and not feed.entries:
        return [], "RSS feed returned HTML instead of XML (site may block automated access)"

    jobs: list[dict[str, Any]] = []
    for entry in feed.entries:
        title = getattr(entry, "title", "") or ""
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue
        description = ""
        if getattr(entry, "summary", ""):
            description = BeautifulSoup(entry.summary, "html.parser").get_text(" ", strip=True)
        elif getattr(entry, "description", ""):
            description = BeautifulSoup(entry.description, "html.parser").get_text(" ", strip=True)
        organization = ""
        if getattr(entry, "author", ""):
            organization = entry.author
        posted = ""
        if getattr(entry, "published", ""):
            posted = entry.published
        jobs.append(
            normalize_job(
                title=title,
                url=link,
                source=source_name,
                organization=organization,
                description=description,
                posted=posted,
            )
        )

    if not jobs:
        return [], "RSS feed parsed successfully but contained 0 job entries"
    return jobs, None


def fetch_rss_source(source_name: str, config: dict[str, Any], query: str) -> tuple[list[dict[str, Any]], str | None]:
    if config.get("url_template"):
        url = config["url_template"].format(query=quote_plus(query))
    else:
        url = config["url"]

    try:
        response = fetch_url(url)
    except requests.RequestException as exc:
        return [], f"Network error: {exc}"

    if response.status_code >= 400:
        return [], f"HTTP {response.status_code} from feed URL"

    jobs, error = parse_rss_content(response.content, source_name)
    if error:
        return [], error
    return [job for job in jobs if matches_query(job, query)], None


def _get_secret(key: str) -> str | None:
    env_key = key.upper()
    if os.environ.get(env_key):
        return os.environ.get(env_key)
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def get_reliefweb_appname() -> str:
    return _get_secret("reliefweb_appname") or os.environ.get("RELIEFWEB_APPNAME") or "job-dashboard"


def fetch_reliefweb_api(query: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch ReliefWeb jobs via POST API, with fallbacks when API access fails."""
    logs: list[str] = []
    appname = get_reliefweb_appname()
    payload = {
        "query": {"value": query, "operator": "AND"} if query.strip() else {"value": ""},
        "limit": 50,
        "fields": {"include": ["title", "url", "source", "date", "body", "country"]},
    }

    # User-specified v1 endpoint (auto-upgrades to v2 when decommissioned)
    for version in ("v1", "v2"):
        api_url = f"https://api.reliefweb.int/{version}/jobs?appname={quote_plus(appname)}"
        try:
            response = requests.post(
                api_url,
                json=payload,
                headers={**REQUEST_HEADERS, "Content-Type": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            logs.append(f"ReliefWeb API {version}: network error ({exc})")
            continue

        if response.status_code == 410 and version == "v1":
            logs.append("ReliefWeb API v1 decommissioned — retrying with v2")
            continue

        if response.status_code == 403:
            logs.append(
                "ReliefWeb API: unapproved appname — set RELIEFWEB_APPNAME or st.secrets['reliefweb_appname']"
            )
            break

        if response.status_code != 200:
            logs.append(f"ReliefWeb API {version}: HTTP {response.status_code}")
            continue

        data = response.json()
        items = data.get("data", [])
        total = data.get("totalCount", len(items))
        if not items:
            logs.append(f"ReliefWeb API: 0 results for query '{query or 'all'}' (totalCount={total})")
            return [], logs

        jobs: list[dict[str, Any]] = []
        for item in items:
            fields = item.get("fields", {})
            title = fields.get("title", "")
            url = fields.get("url", "")
            if not title or not url:
                continue
            org = ""
            source_field = fields.get("source", [])
            if source_field and isinstance(source_field, list):
                org = source_field[0].get("name", "") if source_field[0] else ""
            location = ""
            countries = fields.get("country", [])
            if countries and isinstance(countries, list):
                location = ", ".join(c.get("name", "") for c in countries if c.get("name"))
            description = fields.get("body", "") or ""
            if description:
                description = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:1500]
            posted = ""
            if fields.get("date", {}).get("created"):
                posted = fields["date"]["created"]
            jobs.append(
                normalize_job(
                    title=title,
                    url=url,
                    source="ReliefWeb (API)",
                    organization=org,
                    location=location,
                    description=description,
                    posted=posted,
                )
            )

        logs.append(f"ReliefWeb API: {len(jobs)} jobs fetched")
        return jobs, logs

    # Fallback: scrape ReliefWeb HTML listings
    try:
        search_url = "https://reliefweb.int/jobs"
        if query.strip():
            search_url += f"?search={quote_plus(query)}"
        response = fetch_url(search_url)
        if response.status_code != 200:
            logs.append(f"ReliefWeb HTML fallback: HTTP {response.status_code}")
            return [], logs

        soup = BeautifulSoup(response.text, "html.parser")
        jobs = []
        for article in soup.select("article.rw-river-article, article"):
            link = article.select_one("a[href*='/job/']")
            if not link:
                continue
            title_el = article.select_one("h3, h2, .rw-river-article__title")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            org_el = article.select_one(".rw-river-article__source, .source")
            org = org_el.get_text(strip=True) if org_el else ""
            href = urljoin("https://reliefweb.int", link.get("href", ""))
            jobs.append(
                normalize_job(
                    title=title,
                    url=href,
                    source="ReliefWeb (API)",
                    organization=org,
                )
            )

        jobs = [job for job in jobs if matches_query(job, query)]
        if jobs:
            logs.append(f"ReliefWeb HTML fallback: {len(jobs)} jobs fetched")
            return jobs, logs
        logs.append(f"ReliefWeb HTML fallback: 0 results for query '{query or 'all'}'")
    except requests.RequestException as exc:
        logs.append(f"ReliefWeb HTML fallback failed: {exc}")

    # Fallback: ReliefWeb RSS URL
    rss_jobs, rss_error = fetch_rss_source(
        "ReliefWeb (API)",
        {"url_template": "https://reliefweb.int/jobs/rss.xml?search={query}"},
        query,
    )
    if rss_jobs:
        logs.append(f"ReliefWeb RSS fallback: {len(rss_jobs)} jobs fetched")
        return rss_jobs, logs
    if rss_error:
        logs.append(f"ReliefWeb RSS fallback: {rss_error}")

    return [], logs


def fetch_ilo(query: str) -> tuple[list[dict[str, Any]], str | None]:
    rss_config = {"url": "https://jobs.ilo.org/rss/vacancies.rss"}
    jobs, error = fetch_rss_source("ILO Jobs (RSS)", rss_config, query)
    if jobs:
        return jobs, None

    errors = [error] if error else []

    # ILO RSS often returns HTML; scrape jobsearch page as fallback
    try:
        search_url = "https://jobs.ilo.org/jobsearch/"
        if query.strip():
            search_url += f"?q={quote_plus(query)}"
        response = fetch_url(search_url)
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.select("a[href]"):
            title = link.get_text(strip=True)
            href = urljoin("https://jobs.ilo.org", link.get("href", ""))
            if len(title) > 12 and any(token in href.lower() for token in ("job", "requisition", "vacanc")):
                jobs.append(
                    normalize_job(
                        title=title,
                        url=href,
                        source="ILO Jobs (RSS)",
                        organization="ILO",
                    )
                )
        jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
        if jobs:
            return jobs, None
        errors.append(
            "ILO RSS returned HTML and jobsearch page has no static listings "
            "(site likely requires JavaScript rendering)"
        )
    except requests.RequestException as exc:
        errors.append(f"ILO web fallback failed: {exc}")

    return [], "; ".join(err for err in errors if err)


def fetch_oecd(query: str) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://careers.smartrecruiters.com/OECD/oecd---en"
    try:
        response = fetch_url(url)
    except requests.RequestException as exc:
        return [], f"Network error: {exc}"

    if response.status_code != 200:
        return [], f"HTTP {response.status_code}"

    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []
    for item in soup.select("li.opening-job"):
        link = item.select_one("a")
        if not link:
            continue
        title_el = item.select_one(".job-title, h4")
        location_el = item.select_one(".job-desc")
        title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
        jobs.append(
            normalize_job(
                title=title,
                url=link.get("href", ""),
                source="OECD Careers (web)",
                organization="OECD",
                location=location_el.get_text(strip=True) if location_el else "",
            )
        )

    jobs = [job for job in jobs if matches_query(job, query)]
    if not jobs:
        return [], f"0 job openings matched query '{query or 'all'}' on OECD careers page"
    return jobs, None


def fetch_worldbank(query: str) -> tuple[list[dict[str, Any]], str | None]:
    candidate_urls = [
        "https://jobs.worldbank.org/en/jobs",
        "https://worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup",
        "https://www.worldbank.org/en/about/careers/professional-careers",
    ]
    errors: list[str] = []

    for url in candidate_urls:
        try:
            response = fetch_url(url)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue

        if response.status_code >= 400:
            errors.append(f"{url}: HTTP {response.status_code}")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        jobs = []

        for item in soup.select("li.opening-job"):
            link = item.select_one("a")
            if not link:
                continue
            title_el = item.select_one(".job-title, h4")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin(url, link.get("href", "")),
                    source="World Bank Jobs (web)",
                    organization="World Bank Group",
                )
            )

        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if not text or len(text) < 8:
                continue
            if any(token in href.lower() for token in ("requisition", "job", "position", "career")):
                jobs.append(
                    normalize_job(
                        title=text,
                        url=urljoin(url, href),
                        source="World Bank Jobs (web)",
                        organization="World Bank Group",
                    )
                )

        jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
        if jobs:
            return jobs, None
        errors.append(f"{url}: page loaded but 0 jobs matched query")

    return [], "; ".join(errors) if errors else "No World Bank job listings found"


def fetch_undp(query: str) -> tuple[list[dict[str, Any]], str | None]:
    candidate_urls = [
        "https://jobs.undp.org/cj_jobs.cfm",
        "https://jobs.undp.org/cj_view_jobs.cfm",
    ]
    errors: list[str] = []

    for url in candidate_urls:
        try:
            response = fetch_url(url)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue

        if response.status_code == 404:
            errors.append(f"{url}: not found (404)")
            continue
        if response.status_code >= 400:
            errors.append(f"{url}: HTTP {response.status_code}")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        jobs = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(" ", strip=True)
            if not text:
                continue
            if "oraclecloud.com" in href or "CandidateExperience" in href:
                title = re.sub(r"^Job Title", "", text)
                title = re.split(r"Post level|Apply by", title)[0].strip()
                if len(title) < 5:
                    continue
                jobs.append(
                    normalize_job(
                        title=title,
                        url=href,
                        source="UNDP Jobs (web)",
                        organization="UNDP",
                    )
                )
            elif "view_job" in href or "cj_view" in href:
                if len(text) > 8:
                    jobs.append(
                        normalize_job(
                            title=text,
                            url=urljoin(url, href),
                            source="UNDP Jobs (web)",
                            organization="UNDP",
                        )
                    )

        jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
        if jobs:
            return jobs, None
        errors.append(f"{url}: 0 jobs matched query")

    return [], "; ".join(errors)


def fetch_euractiv(query: str) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://jobs.euractiv.com"
    try:
        response = fetch_url(url)
    except requests.RequestException as exc:
        return [], f"Network error: {exc}"

    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []
    for link in soup.select("a[href*='/jobs/']"):
        title = link.get_text(strip=True)
        href = urljoin(url, link.get("href", ""))
        if title and href.count("/jobs/") == 1:
            jobs.append(
                normalize_job(
                    title=title,
                    url=href,
                    source="Euractiv Jobs (web)",
                    organization="Euractiv",
                )
            )

    jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
    if not jobs:
        return [], "0 jobs found on Euractiv (page may require JavaScript rendering)"
    return jobs, None


def fetch_giz(query: str) -> tuple[list[dict[str, Any]], str | None]:
    base = "https://jobs.giz.de"
    try:
        response = fetch_url(f"{base}/index.php?lang=en")
    except requests.RequestException as exc:
        return [], f"Network error: {exc}"

    soup = BeautifulSoup(response.text, "html.parser")
    jobs = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        title = link.get_text(strip=True)
        if len(title) < 12:
            continue
        if any(token in href.lower() for token in ("job", "stelle", "position", "id=")):
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin(base, href),
                    source="GIZ Jobs (web)",
                    organization="GIZ",
                )
            )

    jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
    if not jobs:
        return [], "0 jobs found on GIZ portal (listings may load via JavaScript)"
    return jobs, None


def fetch_generic_careers(
    source_name: str,
    url: str,
    organization: str,
    query: str,
    selectors: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = fetch_url(url)
    except requests.RequestException as exc:
        return [], f"Network error: {exc}"

    if response.status_code == 403:
        return [], "Access blocked (HTTP 403, likely Cloudflare protection)"
    if response.status_code >= 400:
        return [], f"HTTP {response.status_code}"

    soup = BeautifulSoup(response.text, "html.parser")
    selectors = selectors or ["a[href*='job']", "a[href*='career']", "a[href*='vacanc']", "article a"]
    jobs = []
    for selector in selectors:
        for link in soup.select(selector):
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if len(title) < 8 or not href or href.startswith("#"):
                continue
            jobs.append(
                normalize_job(
                    title=title,
                    url=urljoin(url, href),
                    source=source_name,
                    organization=organization,
                )
            )

    jobs = dedupe_jobs([job for job in jobs if matches_query(job, query)])
    if not jobs:
        return [], f"0 jobs matched query on {organization} careers page"
    return jobs, None


def fetch_source(source_name: str, config: dict[str, Any], query: str) -> tuple[list[dict[str, Any]], str | None]:
    source_type = config.get("type", "rss")

    if source_type == "reliefweb_api":
        jobs, logs = fetch_reliefweb_api(query)
        if jobs:
            return jobs, None
        return [], "; ".join(logs) if logs else "ReliefWeb returned 0 jobs"

    if source_type == "rss":
        if source_name == "ILO Jobs (RSS)":
            return fetch_ilo(query)
        return fetch_rss_source(source_name, config, query)

    if source_type == "oecd":
        return fetch_oecd(query)

    if source_type == "worldbank":
        return fetch_worldbank(query)

    if source_type == "undp":
        return fetch_undp(query)

    if source_type == "euractiv":
        return fetch_euractiv(query)

    if source_type == "giz":
        return fetch_giz(query)

    if source_type == "oxfam":
        return fetch_generic_careers(source_name, config["url"], "Oxfam", query)

    if source_type == "idh":
        return fetch_generic_careers(
            source_name,
            config["url"],
            "IDH",
            query,
            selectors=["a[href*='job']", "a[href*='career']", "h3 a"],
        )

    if source_type == "bsr":
        return fetch_generic_careers(source_name, config["url"], "BSR", query)

    if source_type == "fairwear":
        return fetch_generic_careers(
            source_name,
            config["url"],
            "Fair Wear Foundation",
            query,
            selectors=["a[href*='vacanc']", "a[href*='job']", "article a", "li a"],
        )

    if source_type == "rainforest":
        return fetch_generic_careers(
            source_name,
            config["url"],
            "Rainforest Alliance",
            query,
            selectors=["article a", "a[href*='career']", "h3 a"],
        )

    return [], f"Unknown source type: {source_type}"


def get_anthropic_client() -> Any | None:
    api_key = _get_secret("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or anthropic is None:
        return None
    return anthropic.Anthropic(api_key=api_key)


def build_scoring_prompt(jobs: list[dict[str, Any]], profile: str) -> str:
    lines = [
        "Score each job 0-100 for fit with this candidate profile.",
        "Return ONLY valid JSON: a list of objects with keys: index (0-based), score (integer), reason (short string).",
        f"Profile: {profile or 'International development professional interested in living wages, decent work, and sustainability.'}",
        "Jobs:",
    ]
    for idx, job in enumerate(jobs):
        lines.append(
            f"{idx}. {job['title']} at {job.get('organization') or job['source']} | {job.get('location', '')} | {job['url']}"
        )
        if job.get("description"):
            lines.append(f"   Description: {job['description'][:500]}")
    return "\n".join(lines)


def score_jobs_batch(
    client: Any,
    jobs: list[dict[str, Any]],
    profile: str,
    model: str = "claude-sonnet-4-20250514",
) -> list[dict[str, Any]]:
    prompt = build_scoring_prompt(jobs, profile)
    message = client.messages.create(
        model=model,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text if message.content else "[]"
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise ValueError("Claude response did not contain JSON list")

    parsed = json.loads(match.group())
    score_map = {item.get("index"): item for item in parsed if isinstance(item, dict)}
    for idx, job in enumerate(jobs):
        result = score_map.get(idx, {})
        job["score"] = result.get("score")
        job["score_reason"] = result.get("reason", "")
    return jobs


def score_all_jobs(
    jobs: list[dict[str, Any]],
    profile: str,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    client = get_anthropic_client()
    if not client:
        for job in jobs:
            job["score"] = None
            job["score_reason"] = "No Anthropic API key configured"
        return jobs

    progress = st.progress(0.0, text="Scoring jobs with Claude...")
    total_batches = max(1, (len(jobs) + batch_size - 1) // batch_size)

    for batch_index, start in enumerate(range(0, len(jobs), batch_size)):
        batch = jobs[start : start + batch_size]
        try:
            score_jobs_batch(client, batch, profile)
            for job in batch:
                if job.get("score") is None:
                    job["score"] = "?"
                    job["score_reason"] = "No score returned"
        except Exception as exc:
            for job in batch:
                job["score"] = "?"
                job["score_reason"] = f"Scoring failed: {exc}"
        progress.progress((batch_index + 1) / total_batches, text=f"Scored batch {batch_index + 1}/{total_batches}")

    progress.empty()
    return jobs


def init_session_state() -> None:
    defaults = {
        "jobs": [],
        "scan_log": [],
        "last_query": "",
        "source_settings": {},
        "saved_jobs": load_json(SAVED_JOBS_FILE, []),
        "application_status": load_json(STATUS_FILE, {}),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_enabled_sources() -> dict[str, dict[str, Any]]:
    settings = st.session_state.get("source_settings", {})
    all_sources = {**PRIMARY_SOURCES, **SECONDARY_SOURCES}
    enabled: dict[str, dict[str, Any]] = {}
    for name, config in all_sources.items():
        cfg = dict(config)
        cfg["enabled"] = settings.get(name, config.get("enabled", False))
        if cfg["enabled"]:
            enabled[name] = cfg
    return enabled


def run_job_scan(query: str, profile: str, score_jobs: bool) -> None:
    enabled_sources = get_enabled_sources()
    if not enabled_sources:
        st.warning("No sources enabled. Go to the Sources tab to enable at least one.")
        return

    all_jobs: list[dict[str, Any]] = []
    scan_log: list[str] = []

    for source_name, config in enabled_sources.items():
        with st.spinner(f"Fetching from {source_name}..."):
            jobs, error = fetch_source(source_name, config, query)
        if error:
            scan_log.append(f"❌ {source_name} failed: {error}")
        else:
            scan_log.append(f"✅ {source_name}: {len(jobs)} jobs")
            all_jobs.extend(jobs)

    all_jobs = dedupe_jobs(all_jobs)
    if not all_jobs:
        scan_log.append(
            f"⚠️ Total: 0 jobs across all sources for query '{query or 'all'}'. "
            "Try different keywords, enable more sources, or check failed sources above."
        )
    else:
        scan_log.append(f"📊 Total: {len(all_jobs)} unique jobs from {len(enabled_sources)} sources")

    if score_jobs and all_jobs:
        all_jobs = score_all_jobs(all_jobs, profile)

    st.session_state.jobs = all_jobs
    st.session_state.scan_log = scan_log
    st.session_state.last_query = query


def render_job_card(job: dict[str, Any]) -> None:
    score = job.get("score")
    score_display = "?" if score in (None, "?") else str(score)
    header = f"**{job['title']}** — Score: {score_display}"
    st.markdown(header)
    meta = f"{job.get('organization') or 'Unknown org'} · {job.get('location') or 'Location n/a'} · {job['source']}"
    st.caption(meta)
    if job.get("score_reason"):
        st.caption(job["score_reason"])
    if job.get("description"):
        st.write(job["description"][:400] + ("..." if len(job["description"]) > 400 else ""))
    col1, col2, col3 = st.columns(3)
    with col1:
        st.link_button("View job", job["url"], use_container_width=True)
    with col2:
        if st.button("Save", key=f"save-{job['id']}"):
            saved = st.session_state.saved_jobs
            if not any(item.get("url") == job["url"] for item in saved):
                saved.append({**job, "saved_at": datetime.utcnow().isoformat()})
                st.session_state.saved_jobs = saved
                save_json(SAVED_JOBS_FILE, saved)
                st.toast("Job saved")
    with col3:
        status_key = f"status-{job['id']}"
        current = st.session_state.application_status.get(job["url"], "Saved")
        new_status = st.selectbox("Status", STATUS_OPTIONS, index=STATUS_OPTIONS.index(current), key=status_key)
        if new_status != current:
            st.session_state.application_status[job["url"]] = new_status
            save_json(STATUS_FILE, st.session_state.application_status)


def render_search_tab(profile: str) -> None:
    st.subheader("Search jobs")

    preset = st.selectbox("Keyword profile", list(KEYWORD_PRESETS.keys()), index=0)
    if preset == "Custom":
        query = st.text_input("Custom keywords", value=st.session_state.get("last_query", ""))
    else:
        query = KEYWORD_PRESETS[preset]
        st.text_input("Keywords", value=query, disabled=True)

    score_jobs = st.checkbox("Score jobs with Claude", value=True)
    if st.button("Scan for jobs", type="primary"):
        run_job_scan(query, profile, score_jobs)

    if st.session_state.scan_log:
        with st.expander("Scan log", expanded=True):
            for line in st.session_state.scan_log:
                st.write(line)

    jobs = st.session_state.jobs
    if jobs:
        min_score = st.slider("Minimum score filter", 0, 100, 0)
        filtered = []
        for job in jobs:
            score = job.get("score")
            if score in (None, "?"):
                filtered.append(job)
            elif isinstance(score, (int, float)) and score >= min_score:
                filtered.append(job)
        st.write(f"Showing {len(filtered)} of {len(jobs)} jobs")
        for job in sorted(
            filtered,
            key=lambda item: (item.get("score") if isinstance(item.get("score"), (int, float)) else -1),
            reverse=True,
        ):
            with st.container(border=True):
                render_job_card(job)
    elif st.session_state.scan_log:
        st.info("No jobs to display. Check the scan log above for details from each source.")


def render_sources_tab() -> None:
    st.subheader("Job sources")
    st.caption("Priority sources are on by default. Secondary sources are off by default.")

    settings = dict(st.session_state.get("source_settings", {}))

    st.markdown("#### Priority sources")
    for name, config in PRIMARY_SOURCES.items():
        default = settings.get(name, config.get("enabled", True))
        settings[name] = st.toggle(name, value=default, key=f"source-{name}")

    st.markdown("#### Secondary sources")
    for name, config in SECONDARY_SOURCES.items():
        default = settings.get(name, config.get("enabled", False))
        settings[name] = st.toggle(name, value=default, key=f"secondary-{name}")

    if st.button("Save source settings"):
        st.session_state.source_settings = settings
        save_json(SETTINGS_FILE, settings)
        st.success("Source settings saved")


def render_saved_tab() -> None:
    st.subheader("Saved jobs")
    saved = st.session_state.saved_jobs
    if not saved:
        st.info("No saved jobs yet. Save jobs from the Search tab.")
        return

    for job in saved:
        with st.container(border=True):
            render_job_card(job)

    if st.button("Clear all saved jobs"):
        st.session_state.saved_jobs = []
        save_json(SAVED_JOBS_FILE, [])
        st.rerun()


def render_status_tab() -> None:
    st.subheader("Application status tracker")
    status_map: dict[str, int] = {option: 0 for option in STATUS_OPTIONS}
    for status in st.session_state.application_status.values():
        if status in status_map:
            status_map[status] += 1

    cols = st.columns(len(STATUS_OPTIONS))
    for col, (status, count) in zip(cols, status_map.items()):
        col.metric(status, count)

    rows = []
    for job in st.session_state.saved_jobs:
        rows.append(
            {
                "Title": job.get("title"),
                "Organization": job.get("organization"),
                "Source": job.get("source"),
                "Status": st.session_state.application_status.get(job.get("url"), "Saved"),
                "URL": job.get("url"),
            }
        )

    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("Save jobs to track application status here.")


def render_profile_sidebar() -> str:
    st.sidebar.header("Profile")
    default_profile = (
        "Professional focused on living wages, decent work, labour standards, "
        "supply chain sustainability, and international development programme roles."
    )
    profile = st.sidebar.text_area("Candidate profile for scoring", value=default_profile, height=160)
    st.sidebar.markdown("---")
    st.sidebar.caption("API keys: set `ANTHROPIC_API_KEY` and optionally `RELIEFWEB_APPNAME`.")
    if st.sidebar.button("Save profile"):
        save_json(SETTINGS_FILE, {"profile": profile, "source_settings": st.session_state.source_settings})
        st.sidebar.success("Profile saved")
    return profile


def main() -> None:
    st.set_page_config(page_title="Job Search Dashboard", page_icon="🌍", layout="wide")
    ensure_data_dir()
    init_session_state()

    saved_settings = load_json(SETTINGS_FILE, {})
    if saved_settings.get("source_settings") and not st.session_state.source_settings:
        st.session_state.source_settings = saved_settings["source_settings"]

    profile = render_profile_sidebar()

    st.title("🌍 International Development Job Dashboard")
    st.caption("Living wages · Decent work · Supply chain sustainability · UN & multilateral")

    tabs = st.tabs(["Search", "Sources", "Saved", "Status"])
    with tabs[0]:
        render_search_tab(profile)
    with tabs[1]:
        render_sources_tab()
    with tabs[2]:
        render_saved_tab()
    with tabs[3]:
        render_status_tab()


if __name__ == "__main__":
    main()
