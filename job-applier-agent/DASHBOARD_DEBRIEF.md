# Dashboard implementation debrief (for audit)

## SOURCES IMPLEMENTED:

- ReliefWeb: POST v2 API (`https://api.reliefweb.int/v2/jobs`) + HTML fallback — **partial** — v2 needs approved `RELIEFWEB_APPNAME`; HTML scrape works when API denied
- LinkedIn: guest API + search fallback (2s delay, max 25 results) — **working** — rate-limit possible on heavy use
- UN Jobs: RSS (Geneva + root feed) — **partial** — Cloudflare HTTP 403 from some networks
- ILO Jobs: web scrape `job-search-results/?keyword=` — **partial** — page loads but listings are JavaScript-rendered; static scrape often returns 0
- DevNetJobs: RSS + homepage scrape — **partial** — RSS 404; homepage scrape returns ~96 job links
- OECD Careers: BeautifulSoup `li.opening-job` — **working**
- World Bank Jobs: scrape + fallbacks — **partial** — shows ⚠️ manual URL warning when 0 jobs (cloud IP likely)
- UNDP Jobs: scrape `cj_jobs.cfm` + `cj_view_jobs.cfm` — **working** — primary URL 404; view page returns Oracle links
- EU Careers (EPSO): scrape open-competitions page — **partial** — few static links
- Impactpool: scrape search page with Referer — **working** when not 403
- Devex Jobs: BeautifulSoup search page — **partial** — HTTP 403 common
- Global Jobs: search scrape + RSS fallback — **working** — RSS has 273 entries
- Idealist: `__NEXT_DATA__` JSON + link scrape — **partial** — mostly JS-rendered
- Bond: scrape `jobs.bond.org.uk` — **partial** — ⚠️ manual URL warning when 0 jobs; DNS may fail
- Oxfam Jobs: vacancy search + fallback — **partial** — ⚠️ manual URL warning when 0 jobs; DNS may fail
- GIZ Jobs: `index_en_jobs.html` + career/jobs fallback — **partial** — ⚠️ manual URL warning when 0 jobs
- Fair Wear Foundation: vacancies page scrape — **partial** — ⚠️ manual URL warning when 0 jobs
- IDH Sustainable Trade: jobs page scrape — **partial** — ⚠️ manual URL warning when 0 jobs; redirects to HiBob portal

## SOURCES SKIPPED:

- None — all 18 sources implemented

## KNOWN LIMITATIONS:

- ReliefWeb v2 requires pre-approved appname (`RELIEFWEB_APPNAME` env var)
- ILO `job-search-results` page is JS-heavy — may return 0 jobs from cloud IPs even after scrape switch
- World Bank, Bond, Oxfam, GIZ, Fair Wear, IDH show ⚠️ warnings (not ❌ failures) when 0 jobs, with manual URLs
- UN Jobs, Devex, Impactpool may block datacenter IPs (403)
- LinkedIn capped at 25 results with 2s sleep between guest API and fallback request
- Location filter uses expanded aliases (Geneva↔Genève/Switzerland, Netherlands↔Amsterdam/NL, Remote↔home-based/virtual) but is not geocoded
- Scoring requires `ANTHROPIC_API_KEY`; uses strict 0–10 rubric with colour badges

## PACKAGES REQUIRED:

- `pip install streamlit anthropic requests feedparser beautifulsoup4 lxml`

## QUESTIONS FOR REVIEWER:

- Should ILO use Playwright given `job-search-results` is JS-rendered?
- Is ReliefWeb HTML fallback acceptable as default when appname is not approved?
- Should World Bank CSOD be added to manual-open list with a different URL?
- Confirm cloud-IP warning sources list is complete for the user's deployment environment
