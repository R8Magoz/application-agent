# Dashboard implementation debrief (for audit)

## SOURCES IMPLEMENTED:

- ReliefWeb: POST API v1 (auto v2) + HTML fallback — partial — v1 decommissioned; v2 needs approved appname; HTML scrape works
- LinkedIn: guest API + search page fallback — working — guest endpoint returns job cards reliably
- UN Jobs: RSS (Geneva + root feed) — partial — Cloudflare HTTP 403 from some networks
- ILO Jobs: RSS + jobsearch web fallback — partial — RSS URL returns HTML not XML; static scrape usually empty (JS site)
- DevNetJobs: RSS + homepage scrape — partial — RSS 404; homepage scrape returns ~96 job links
- OECD Careers: BeautifulSoup `li.opening-job` — working — 11 openings found in testing
- World Bank Jobs: scrape primary URL + CSOD/careers fallbacks — partial — jobs.worldbank.org DNS fails in some envs; CSOD is JS-rendered
- UNDP Jobs: scrape cj_jobs.cfm + cj_view_jobs.cfm — working — cj_jobs.cfm 404; cj_view_jobs.cfm returns 98 Oracle links
- EU Careers (EPSO): scrape open-competitions page — partial — few static competition links; RSS URL returns 404
- Impactpool: scrape search page with Referer header — working — 40 job links when not 403
- Devex Jobs: BeautifulSoup search page — partial — HTTP 403 bot protection common
- Global Jobs: search page scrape + RSS fallback — working — RSS feed has 273 entries
- Idealist: __NEXT_DATA__ JSON walk + link scrape — partial — mostly JS-rendered; often 0 results
- Bond: scrape jobs.bond.org.uk — partial — DNS resolution fails in some environments
- Oxfam Jobs: vacancy search + oxfam.org fallback — partial — jobs.oxfam.org DNS fails in some environments
- GIZ Jobs: index_en_jobs.html + career/jobs fallback — partial — mostly navigation links, few listings
- Fair Wear Foundation: vacancies page scrape — partial — few/no vacancy links in static HTML
- IDH Sustainable Trade: jobs page scrape — partial — redirects to external HiBob careers portal

## SOURCES SKIPPED:

- None — all 18 sources from the spec are implemented with at least one fetch attempt and error logging

## KNOWN LIMITATIONS:

- ReliefWeb API v1 is decommissioned (HTTP 410); v2 requires a pre-approved appname (set `RELIEFWEB_APPNAME`)
- Several sites block datacenter IPs (UN Jobs 403, Devex 403, Impactpool intermittent 403)
- ILO, World Bank CSOD, Idealist, and GIZ rely heavily on JavaScript rendering — static scrape may return 0
- `jobs.worldbank.org`, `jobs.oxfam.org`, and `jobs.bond.org.uk` may fail DNS in sandbox/CI environments
- LinkedIn may rate-limit (HTTP 429) with heavy use — guest endpoint used with standard User-Agent
- Scoring requires `ANTHROPIC_API_KEY`; failed batches appear under "Not scored yet" with score=None
- Location filter is keyword-based (substring match), not geocoded

## PACKAGES REQUIRED:

- `pip install streamlit anthropic requests feedparser beautifulsoup4 lxml`

## QUESTIONS FOR REVIEWER:

- Should ReliefWeb HTML fallback be preferred over API given appname approval requirement?
- Is DevNetJobs homepage scrape acceptable given RSS URL returns 404?
- Should World Bank use a paid/scraping API or Playwright for JS-rendered CSOD listings?
- Confirm 0–10 scoring scale matches the min-score slider default of 4.0 (previous version used 0–100)
- Should Bond use an alternate URL (bond.org.uk/jobs returns 404; jobs.bond.org.uk has DNS issues in testing)?
