#!/usr/bin/env python3
"""
Website Audit Engine — LeadAudit Pro
====================================
Accepts a URL → fetches page + SEO signals → returns score + recommendations + gene trace.
No external API required — uses requests + BeautifulSoup only.

The gene_trace is logged BEFORE the outcome is known — this ordering is critical
for building a real calibration dataset (Kailash's insight, 2026-08-19).
"""

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

# ── Gene definitions ──────────────────────────────────────────────────────────
GENES = {
    "decompose":   "break problem into independent parts",
    "world_model": "build mental model of system under audit",
    "constraint":  "find hard boundaries and limits",
    "analogical":  "transfer patterns from known domains",
    "evolutionary":"try variants, score each, keep best",
    "direct":      "apply known solution directly",
    "meta":        "reason about the reasoning process itself",
    "abstraction": "find higher-order patterns across instances",
}


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class GeneEntry:
    gene: str
    fired: bool
    reasoning: str
    confidence: str  # high / medium / low
    alternatives: list = field(default_factory=list)


@dataclass
class AuditResult:
    url: str
    final_url: str
    score: int          # 0-100
    grade: str          # A/B/C/D/F
    confidence: str      # high / medium / low — overall self-assessed

    # Gene trace — logged BEFORE outcome is known
    gene_trace: list = field(default_factory=list)  # List[GeneEntry]
    alternatives_considered: list = field(default_factory=list)

    # SEO
    title: str = ""
    title_length: int = 0
    meta_description: str = ""
    meta_description_length: int = 0
    h1_count: int = 0
    h1_texts: list = field(default_factory=list)
    h2_count: int = 0
    canonical: str = ""
    og_tags: dict = field(default_factory=dict)
    robots: str = ""

    # Performance
    status_code: int = 0
    load_time_ms: int = 0
    page_size_bytes: int = 0
    redirects: int = 0

    # Security
    ssl: bool = False
    mixed_content: bool = False
    x_frame_options: str = ""
    x_content_type_options: str = ""

    # Tracking
    has_ga4: bool = False
    has_fb_pixel: bool = False
    has_gtm: bool = False
    tracking_count: int = 0

    # Mobile
    viewport: str = ""
    mobile_friendly: bool = False

    # Recommendations — logged BEFORE outcome is known
    recommendations: list = field(default_factory=list)
    critical_issues: list = field(default_factory=list)

    # Outcome tracking — filled in later via POST /api/report-outcome
    outcome_result: str = ""         # confirmed / refuted / inconclusive / pending
    outcome_verified_at: str = ""    # ISO timestamp
    outcome_notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gene_trace"] = [asdict(g) for g in self.gene_trace]
        return d


# ── Core functions ───────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Optional[BeautifulSoup], int, dict]:
    """Fetch URL, follow redirects, return (soup, status_code, perf_stats)."""
    try:
        start = time.time()
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        load_ms = int((time.time() - start) * 1000)
        final_url = resp.url
        content = resp.text
        return BeautifulSoup(content, "lxml"), resp.status_code, {
            "load_ms": load_ms,
            "size_bytes": len(content.encode()),
            "redirects": len(resp.history),
        }
    except requests.exceptions.Timeout:
        return None, 0, {"error": "timeout"}
    except requests.exceptions.ConnectionError:
        return None, 0, {"error": "connection_error"}
    except Exception as e:
        return None, 0, {"error": str(e)}


def audit_seo(soup: BeautifulSoup, url: str, gene_trace: list) -> dict:
    """Audit SEO signals. Appends gene reasoning to gene_trace."""
    result = {}
    alt_considered = []

    # Title — direct: well-understood SEO signal
    gene_trace.append(GeneEntry(gene="direct", fired=True,
        reasoning="title tag is a direct ranking factor — length and presence are well-documented signals",
        confidence="high"))
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    result["title"] = title
    result["title_length"] = len(title)

    if not title:
        gene_trace.append(GeneEntry(gene="decompose", fired=True,
            reasoning="no title found — decomposed SEO signals now have one missing element",
            confidence="high"))
        alt_considered.append("title_missing_possible_causes: cms_error / scrape_failure / cloaked_page")

    # Meta description — direct signal
    gene_trace.append(GeneEntry(gene="direct", fired=True,
        reasoning="meta description is a direct snippet signal in SERPs — presence and length matter",
        confidence="high"))
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag.get("content", "").strip() if desc_tag else ""
    result["meta_description"] = desc
    result["meta_description_length"] = len(desc)

    if not desc:
        gene_trace.append(GeneEntry(gene="world_model", fired=True,
            reasoning="no meta description — search engines will auto-generate snippet from page content, losing editorial control",
            confidence="medium",
            alternatives=["let_engines_auto_generate / write_manual_description / use_og_description"]))

    # Headings — decompose: structural hierarchy matters independently
    h1s = soup.find_all("h1")
    h2s = soup.find_all("h2")
    result["h1_count"] = len(h1s)
    result["h1_texts"] = [h.get_text(strip=True) for h in h1s[:5]]
    result["h2_count"] = len(h2s)

    if len(h1s) == 0:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="H1 constraint violated: every indexable page must have exactly one H1 — zero is a hard failure",
            confidence="high"))
    elif len(h1s) > 1:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning=f"multiple H1s ({len(h1s)}) — constraint violated: exactly one H1 is the standard",
            confidence="high"))
    else:
        gene_trace.append(GeneEntry(gene="decompose", fired=True,
            reasoning=f"single H1 confirmed — structural hierarchy is valid (text: '{h1s[0].get_text(strip=True)[:40]}')",
            confidence="medium"))

    # H2 count as abstraction signal — very high or very low h2 count can indicate thin content
    if len(h2s) == 0 and len(h1s) > 0:
        gene_trace.append(GeneEntry(gene="abstraction", fired=True,
            reasoning="zero H2s on a page with H1 suggests thin content structure or improper heading hierarchy",
            confidence="medium",
            alternatives=["page_is_landing_page / headings_used_incorrectly / content_underdeveloped"]))
    else:
        gene_trace.append(GeneEntry(gene="abstraction", fired=True,
            reasoning=f"H2 density ({len(h2s)} h2s) within normal range for a {'content-heavy' if len(h2s) > 3 else 'light'} page",
            confidence="low"))

    # Canonical
    canon = soup.find("link", attrs={"rel": "canonical"})
    result["canonical"] = canon.get("href", "").strip() if canon else ""

    # OG tags as abstraction of social/brand presence
    og_tags = {}
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        prop = tag.get("property", "").replace("og:", "")
        content = tag.get("content", "")
        if prop and content:
            og_tags[prop] = content[:200]
    result["og_tags"] = og_tags

    if og_tags:
        gene_trace.append(GeneEntry(gene="abstraction", fired=True,
            reasoning=f"OG tags present ({len(og_tags)} tags) — signals social sharing intent and brand awareness",
            confidence="medium"))
    else:
        gene_trace.append(GeneEntry(gene="meta", fired=True,
            reasoning="no OG tags — page missing social sharing metadata, will render poorly when shared",
            confidence="low"))

    # Robots
    robots_tag = soup.find("meta", attrs={"name": "robots"})
    result["robots"] = robots_tag.get("content", "").strip() if robots_tag else ""

    # Viewport / mobile
    vp = soup.find("meta", attrs={"name": "viewport"})
    result["viewport"] = vp.get("content", "").strip() if vp else ""
    result["mobile_friendly"] = bool(result["viewport"] and "width" in result["viewport"])

    if result["mobile_friendly"]:
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning="viewport meta present — mobile-first requirement satisfied",
            confidence="high"))
    else:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="no viewport meta — Google uses mobile-first indexing, this is a hard requirement",
            confidence="high"))

    return result, alt_considered


def audit_security(soup: BeautifulSoup, final_url: str, gene_trace: list) -> dict:
    """Audit security signals. Appends to gene_trace."""
    result = {}

    # SSL — direct constraint
    result["ssl"] = final_url.startswith("https://")
    if result["ssl"]:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="HTTPS confirmed — SSL is a hard requirement for modern SEO and user trust",
            confidence="high"))
    else:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="no SSL — HTTPS is a confirmed ranking factor and Chrome flags non-HTTPS pages",
            confidence="high",
            alternatives=["install_lets_encrypt / force_https_redirect / cdn_ssl"]))

    # Mixed content
    mixed_sources = []
    for tag in soup.find_all(src=re.compile(r"^http://")):
        mixed_sources.append({"tag": tag.name, "src": tag.get("src", "")[:80]})
    result["mixed_content"] = len(mixed_sources) > 0

    if result["mixed_content"]:
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning=f"mixed content detected — {len(mixed_sources)} HTTP resources on HTTPS page — browsers block these",
            confidence="high"))
    else:
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning="no mixed content — all resources load securely",
            confidence="high"))

    # Security headers via requests
    try:
        resp = requests.get(final_url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        x_frame = resp.headers.get("X-Frame-Options", "")
        x_content = resp.headers.get("X-Content-Type-Options", "")
        result["x_frame_options"] = x_frame
        result["x_content_type_options"] = x_content

        if x_frame:
            gene_trace.append(GeneEntry(gene="direct", fired=True,
                reasoning="X-Frame-Options header present — clickjacking protection active",
                confidence="high"))
        else:
            gene_trace.append(GeneEntry(gene="meta", fired=True,
                reasoning="X-Frame-Options missing — clickjacking vector exists but low severity for most sites",
                confidence="low"))
    except Exception:
        result["x_frame_options"] = ""
        result["x_content_type_options"] = ""

    return result


def audit_tracking(soup: BeautifulSoup, gene_trace: list) -> dict:
    """Audit analytics/tracking signals."""
    html = str(soup)
    result = {
        "has_ga4": False,
        "has_fb_pixel": False,
        "has_gtm": False,
        "tracking_count": 0,
    }

    patterns = {
        "has_ga4": [
            r"gtag\s*\(",
            r"googletagmanager\.com.*id=GTM-",
            r"analytics\.js",
            r"gtm\.js",
        ],
        "has_fb_pixel": [
            r"connect\.facebook\.net.*fbevents",
            r"fbq\s*\(",
        ],
        "has_gtm": [
            r"googletagmanager\.com.*gtm\.js",
            r"dataLayer\s*=",
        ],
    }

    for key, pats in patterns.items():
        for pat in pats:
            if re.search(pat, html, re.I):
                result[key] = True
                result["tracking_count"] += 1
                break

    if result["tracking_count"] > 0:
        gene_trace.append(GeneEntry(gene="world_model", fired=True,
            reasoning=f"tracking detected ({result['tracking_count']} signals) — client values data-driven decisions",
            confidence="medium"))
    else:
        gene_trace.append(GeneEntry(gene="evolutionary", fired=True,
            reasoning="no tracking found — cannot measure SEO impact without analytics in place",
            confidence="high",
            alternatives=["add_ga4_free / add_gtm / add_fb_pixel"]))

    if result.get("has_ga4"):
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning="GA4 detected — Google's current analytics platform, good signal of professional setup",
            confidence="high"))
    else:
        gene_trace.append(GeneEntry(gene="abstraction", fired=True,
            reasoning="no GA4 — missing the standard analytics platform most SEO professionals rely on",
            confidence="medium",
            alternatives=["upgrade_to_ga4 / use_privacy_friendly_alternative / add_gtm_first"]))

    return result


def score_seo(seo: dict, security: dict, tracking: dict, perf: dict,
              gene_trace: list, alt_considered: list) -> tuple[int, list, list]:
    """
    Score 0-100. Gene-trace-aware scoring — logs WHY each deduction was made.
    Returns (score, recommendations, critical_issues).
    """
    score = 100
    recs = []
    critical = []
    all_alts = list(alt_considered)

    # ── SEO deductions ──────────────────────────────────────────────────────
    if not seo.get("title"):
        recs.append("Missing <title> tag — add a descriptive page title immediately")
        score -= 25
        gene_trace.append(GeneEntry(gene="decompose", fired=True,
            reasoning="score deduction -25: title missing is the single highest-impact SEO omission",
            confidence="high"))
    elif seo["title_length"] < 30:
        recs.append(f"Title too short ({seo['title_length']} chars) — aim for 50-60 characters")
        score -= 10
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning=f"score deduction -10: title only {seo['title_length']} chars — truncated in SERPs",
            confidence="high"))
    elif seo["title_length"] > 70:
        recs.append(f"Title too long ({seo['title_length']} chars) — search engines may truncate at 60")
        score -= 5
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning=f"score deduction -5: title {seo['title_length']} chars — SERP truncation likely",
            confidence="medium"))

    if not seo.get("meta_description"):
        recs.append("Missing meta description — add a 150-160 character summary")
        score -= 15
        critical.append("No meta description — search engines will auto-generate, losing editorial control")
        gene_trace.append(GeneEntry(gene="world_model", fired=True,
            reasoning="score deduction -15: no meta description — search engine controls the SERP snippet",
            confidence="high"))
    elif seo["meta_description_length"] < 100:
        recs.append(f"Meta description too short ({seo['meta_description_length']} chars) — expand to 150-160")
        score -= 5
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning=f"score deduction -5: meta description {seo['meta_description_length']} chars — too short to influence click-through",
            confidence="medium"))
    elif seo["meta_description_length"] > 160:
        recs.append(f"Meta description too long ({seo['meta_description_length']} chars) — trim to 160 or less")
        score -= 5
        gene_trace.append(GeneEntry(gene="direct", fired=True,
            reasoning=f"score deduction -5: meta description {seo['meta_description_length']} chars — truncated in SERPs",
            confidence="medium"))

    h1_count = seo.get("h1_count", 0)
    if h1_count == 0:
        recs.append("No <h1> tag found — every page needs exactly one H1")
        score -= 15
        critical.append("No H1 heading — search engines use H1 to understand page topic")
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="score deduction -15: H1 constraint violated — zero H1s found",
            confidence="high"))
    elif h1_count > 1:
        recs.append(f"Multiple H1 tags ({h1_count}) — use only one per page")
        score -= 10
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning=f"score deduction -10: {h1_count} H1s found — constraint is exactly one",
            confidence="high"))

    if not seo.get("canonical") and not seo.get("robots"):
        recs.append("No canonical URL or robots meta — add one to prevent duplicate content issues")
        score -= 5
        gene_trace.append(GeneEntry(gene="world_model", fired=True,
            reasoning="score deduction -5: no canonical — duplicate content risk unmitigated",
            confidence="medium"))

    # ── Security deductions ────────────────────────────────────────────────
    if not security.get("ssl"):
        critical.append("No HTTPS — switch to SSL immediately (free via Let's Encrypt)")
        score -= 20
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="score deduction -20: no SSL — hard requirement violated",
            confidence="high",
            alternatives=["letsencrypt_free / cloudflare_free_ssl / hosting_provider_ssl"]))
    elif security.get("mixed_content"):
        recs.append("Mixed content detected — some resources loaded over HTTP on HTTPS page")
        score -= 10
        critical.append("Mixed HTTP/HTTPS content — browsers block insecure resources")
        gene_trace.append(GeneEntry(gene="constraint", fired=True,
            reasoning="score deduction -10: mixed content — browsers block HTTP resources on HTTPS pages",
            confidence="high"))

    if not security.get("x_frame_options"):
        recs.append("Missing X-Frame-Options header — add to prevent clickjacking")
        score -= 5
        gene_trace.append(GeneEntry(gene="meta", fired=True,
            reasoning="score deduction -5: X-Frame-Options missing — low severity but demonstrates security awareness",
            confidence="low"))

    # ── Performance deductions ────────────────────────────────────────────────
    load_ms = perf.get("load_ms", 0)
    if load_ms > 3000:
        recs.append(f"Page load slow ({load_ms}ms) — target under 3 seconds for SEO")
        score -= 10
        critical.append(f"Slow load time ({load_ms}ms) — Core Web Vitals may fail")
        gene_trace.append(GeneEntry(gene="world_model", fired=True,
            reasoning=f"score deduction -10: load {load_ms}ms exceeds 3s threshold — Core Web Vitals likely failing",
            confidence="high"))
    elif load_ms > 1500:
        recs.append(f"Page load could be faster ({load_ms}ms) — aim for under 1.5s")
        score -= 5
        gene_trace.append(GeneEntry(gene="evolutionary", fired=True,
            reasoning=f"score deduction -5: load {load_ms}ms above 1.5s — improvement possible with compression/CDN",
            confidence="medium"))

    page_size = perf.get("page_size_bytes", 0)
    if page_size > 3_000_000:
        recs.append(f"Page size large ({page_size//1024}KB) — compress images and enable lazy loading")
        score -= 5
        gene_trace.append(GeneEntry(gene="analogical", fired=True,
            reasoning=f"score deduction -5: page {page_size//1024}KB — similar sites that compressed images saw 40% load improvement",
            confidence="medium"))

    # ── Tracking deductions ─────────────────────────────────────────────────
    if tracking["tracking_count"] == 0:
        recs.append("No analytics detected — add Google Analytics 4 to measure SEO impact")
        score -= 10
        gene_trace.append(GeneEntry(gene="evolutionary", fired=True,
            reasoning="score deduction -10: no tracking — cannot measure SEO ROI, making optimization blind",
            confidence="high"))
    elif not tracking.get("has_ga4"):
        recs.append("No GA4 found — upgrade to Google Analytics 4 for better insights")
        score -= 5
        gene_trace.append(GeneEntry(gene="abstraction", fired=True,
            reasoning="score deduction -5: GA4 absent — using deprecated Universal Analytics",
            confidence="medium"))

    score = max(0, min(100, score))

    # ── Overall confidence ──────────────────────────────────────────────────
    critical_count = len(critical)
    if critical_count >= 3 or score < 50:
        overall_confidence = "high"  # confident something is wrong
    elif critical_count >= 1 or score < 75:
        overall_confidence = "medium"
    else:
        overall_confidence = "high"  # well-calibrated: clean site deserves high confidence

    return score, recs, critical, overall_confidence


def grade_from_score(score: int) -> str:
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


def audit_website(url: str) -> AuditResult:
    """
    Main entry point.
    Logs gene_trace BEFORE outcome is known — this ordering is the mechanism
    that makes the calibration dataset real rather than theater.
    """
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url

    # Gene trace starts empty — all entries are logged BEFORE we know the outcome
    gene_trace: list = []
    alternatives_considered: list = []

    # ── Fetch ────────────────────────────────────────────────────────────────
    soup, status_code, perf = fetch_page(url)

    result = AuditResult(
        url=url,
        final_url=url,
        score=0,
        grade="F",
        confidence="medium",
        gene_trace=gene_trace,
    )

    if soup is None:
        result.critical_issues.append(f"Could not reach site: {perf.get('error', 'unknown error')}")
        gene_trace.append(GeneEntry(gene="meta", fired=True,
            reasoning=f"fetch failed: {perf.get('error', 'unknown')} — cannot audit unreachable site",
            confidence="high"))
        return result

    result.status_code = status_code
    result.load_time_ms = perf.get("load_ms", 0)
    result.page_size_bytes = perf.get("size_bytes", 0)
    result.redirects = perf.get("redirects", 0)
    result.final_url = url

    gene_trace.append(GeneEntry(gene="direct", fired=True,
        reasoning=f"fetch succeeded: {status_code}, {perf.get('load_ms')}ms, {perf.get('size_bytes',0)//1024}KB, {len(soup.find_all())} elements parsed",
        confidence="high"))

    # ── SEO audit ────────────────────────────────────────────────────────────
    seo, alt = audit_seo(soup, url, gene_trace)
    alternatives_considered.extend(alt)
    result.title = seo.get("title", "")
    result.title_length = seo.get("title_length", 0)
    result.meta_description = seo.get("meta_description", "")
    result.meta_description_length = seo.get("meta_description_length", 0)
    result.h1_count = seo.get("h1_count", 0)
    result.h1_texts = seo.get("h1_texts", [])
    result.h2_count = seo.get("h2_count", 0)
    result.canonical = seo.get("canonical", "")
    result.og_tags = seo.get("og_tags", {})
    result.robots = seo.get("robots", "")
    result.viewport = seo.get("viewport", "")
    result.mobile_friendly = seo.get("mobile_friendly", False)

    # ── Security audit ───────────────────────────────────────────────────────
    security = audit_security(soup, result.final_url, gene_trace)
    result.ssl = security.get("ssl", False)
    result.mixed_content = security.get("mixed_content", False)
    result.x_frame_options = security.get("x_frame_options", "")
    result.x_content_type_options = security.get("x_content_type_options", "")

    # ── Tracking audit ───────────────────────────────────────────────────────
    tracking = audit_tracking(soup, gene_trace)
    result.has_ga4 = tracking.get("has_ga4", False)
    result.has_fb_pixel = tracking.get("has_fb_pixel", False)
    result.has_gtm = tracking.get("has_gtm", False)
    result.tracking_count = tracking.get("tracking_count", 0)

    # ── Score with gene awareness ────────────────────────────────────────────
    result.score, recs, critical, overall_confidence = score_seo(
        seo, security, tracking, perf, gene_trace, alternatives_considered)
    result.grade = grade_from_score(result.score)
    result.confidence = overall_confidence
    result.recommendations = recs
    result.critical_issues = critical
    result.gene_trace = gene_trace
    result.alternatives_considered = alternatives_considered
    result.outcome_result = "pending"  # ← the critical ordering: logged before we know

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python website_audit.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Auditing: {url}")
    r = audit_website(url)

    print(f"\nScore: {r.score}/100  Grade: {r.grade}  Confidence: {r.confidence}")
    print(f"Title: {r.title[:80]}")
    print(f"H1s: {r.h1_count} | SSL: {r.ssl} | GA4: {r.has_ga4}")
    print(f"Gene trace entries: {len(r.gene_trace)}")
    print(f"\nCritical issues ({len(r.critical_issues)}):")
    for i in r.critical_issues:
        print(f"  [!] {i}")
    print(f"\nRecommendations ({len(r.recommendations)}):")
    for i in r.recommendations:
        print(f"  -> {i}")
    print(f"\nGene trace:")
    for g in r.gene_trace:
        print(f"  [{g.gene}] ({g.confidence}) {g.reasoning[:80]}")
