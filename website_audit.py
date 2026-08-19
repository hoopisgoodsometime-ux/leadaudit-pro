#!/usr/bin/env python3
"""
Website Audit Engine — LeadAudit Pro
====================================
Accepts a URL → fetches page + SEO signals → returns score + recommendations.
No external API required — uses requests + BeautifulSoup only.
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

# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class AuditResult:
    url: str
    final_url: str
    score: int  # 0-100
    grade: str  # A/B/C/D/F

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

    # Recommendations
    recommendations: list = field(default_factory=list)
    critical_issues: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Core functions ───────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Optional[BeautifulSoup], int, dict]:
    """Fetch URL, follow redirects, return (soup, status_code, redirect_chain)."""
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


def check_ssl(url: str) -> bool:
    return url.startswith("https://")


def audit_seo(soup: BeautifulSoup, url: str) -> dict:
    result = {}

    # Title
    title_tag = soup.find("title")
    result["title"] = title_tag.get_text(strip=True) if title_tag else ""
    result["title_length"] = len(result["title"])

    # Meta description
    desc_tag = soup.find("meta", attrs={"name": "description"})
    result["meta_description"] = desc_tag.get("content", "").strip() if desc_tag else ""
    result["meta_description_length"] = len(result["meta_description"])

    # Headings
    h1s = soup.find_all("h1")
    result["h1_count"] = len(h1s)
    result["h1_texts"] = [h.get_text(strip=True) for h in h1s[:5]]
    result["h2_count"] = len(soup.find_all("h2"))

    # Canonical
    canon = soup.find("link", attrs={"rel": "canonical"})
    result["canonical"] = canon.get("href", "").strip() if canon else ""

    # OG tags
    result["og_tags"] = {}
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        prop = tag.get("property", "").replace("og:", "")
        content = tag.get("content", "")
        if prop and content:
            result["og_tags"][prop] = content[:200]

    # Robots
    robots_tag = soup.find("meta", attrs={"name": "robots"})
    result["robots"] = robots_tag.get("content", "").strip() if robots_tag else ""

    # Viewport / mobile
    vp = soup.find("meta", attrs={"name": "viewport"})
    result["viewport"] = vp.get("content", "").strip() if vp else ""
    result["mobile_friendly"] = bool(result["viewport"] and "width" in result["viewport"])

    return result


def audit_security(soup: BeautifulSoup, final_url: str) -> dict:
    result = {}

    # SSL
    result["ssl"] = final_url.startswith("https://")

    # Mixed content
    mixed = False
    for tag in soup.find_all(src=re.compile(r"^http://")):
        if tag.name not in ("img", "script", "link", "iframe"):
            continue
        src = tag.get("src", "")
        if src.startswith("http://"):
            mixed = True
            break
    for tag in soup.find_all(href=re.compile(r"^http://")):
        href = tag.get("href", "")
        if href.startswith("http://") and tag.name == "link":
            mixed = True
            break
    result["mixed_content"] = mixed

    # Security headers via requests
    try:
        resp = requests.get(final_url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        result["x_frame_options"] = resp.headers.get("X-Frame-Options", "")
        result["x_content_type_options"] = resp.headers.get("X-Content-Type-Options", "")
    except Exception:
        result["x_frame_options"] = ""
        result["x_content_type_options"] = ""

    return result


def audit_tracking(soup: BeautifulSoup) -> dict:
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

    return result


def score_seo(seo: dict, security: dict, tracking: dict, perf: dict) -> tuple[int, list, list]:
    """
    Calculate overall score 0-100.
    Returns (score, recommendations, critical_issues).
    """
    score = 100
    recs = []
    critical = []

    # SEO (up to -50 points)
    if not seo.get("title"):
        recs.append("Missing <title> tag — add a descriptive page title")
        score -= 25
    elif seo["title_length"] < 30:
        recs.append(f"Title too short ({seo['title_length']} chars) — aim for 50-60 characters")
        score -= 10
    elif seo["title_length"] > 70:
        recs.append(f"Title too long ({seo['title_length']} chars) — keep under 60 characters")
        score -= 5

    if not seo.get("meta_description"):
        recs.append("Missing meta description — add a 150-160 character summary")
        score -= 15
    elif seo["meta_description_length"] < 100:
        recs.append(f"Meta description too short ({seo['meta_description_length']} chars) — expand to 150-160")
        score -= 5
    elif seo["meta_description_length"] > 160:
        recs.append(f"Meta description too long ({seo['meta_description_length']} chars) — trim to 160 or less")
        score -= 5

    if seo.get("h1_count", 0) == 0:
        recs.append("No <h1> tag found — every page needs exactly one H1")
        score -= 15
        critical.append("No H1 heading — search engines use H1 to understand page topic")
    elif seo["h1_count"] > 1:
        recs.append(f"Multiple H1 tags ({seo['h1_count']}) — use only one per page")
        score -= 10

    if not seo.get("canonical") and not seo.get("robots"):
        recs.append("No canonical URL or robots meta — add one to prevent duplicate content issues")
        score -= 5

    # Security (up to -20 points)
    if not security.get("ssl"):
        critical.append("No HTTPS — switch to SSL immediately (free via Let's Encrypt)")
        score -= 20
    elif security.get("mixed_content"):
        recs.append("Mixed content detected — some resources loaded over HTTP on HTTPS page")
        score -= 10
        critical.append("Mixed HTTP/HTTPS content — browsers block insecure resources")

    if not security.get("x_frame_options"):
        recs.append("Missing X-Frame-Options header — add to prevent clickjacking")
        score -= 5

    # Performance (up to -15 points)
    if perf.get("load_ms", 0) > 3000:
        recs.append(f"Page load slow ({perf['load_ms']}ms) — target under 3 seconds")
        score -= 10
    elif perf.get("load_ms", 0) > 1500:
        recs.append(f"Page load could be faster ({perf['load_ms']}ms) — aim for under 1.5s")
        score -= 5

    if perf.get("page_size_bytes", 0) > 3_000_000:
        recs.append(f"Page size large ({perf['page_size_bytes']//1024}KB) — consider compressing images and lazy loading")
        score -= 5

    # Tracking (up to -10 points)
    if tracking["tracking_count"] == 0:
        recs.append("No analytics detected — add Google Analytics 4 to measure traffic")
        score -= 10
    elif not tracking.get("has_ga4"):
        recs.append("No GA4 found — upgrade to Google Analytics 4 for better insights")
        score -= 5

    score = max(0, min(100, score))
    return score, recs, critical


def grade_from_score(score: int) -> str:
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


def audit_website(url: str) -> AuditResult:
    """Main entry point."""
    # Normalize URL
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url

    # Fetch
    soup, status_code, perf = fetch_page(url)
    final_url = url  # will be updated by resp.url after redirects

    result = AuditResult(url=url, final_url=final_url, score=0, grade="F")

    if soup is None:
        result.critical_issues.append(f"Could not reach site: {perf.get('error', 'unknown error')}")
        return result

    result.status_code = status_code
    result.load_time_ms = perf.get("load_ms", 0)
    result.page_size_bytes = perf.get("size_bytes", 0)
    result.redirects = perf.get("redirects", 0)
    result.final_url = final_url

    # SEO audit
    seo = audit_seo(soup, url)
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

    # Security audit
    security = audit_security(soup, result.final_url)
    result.ssl = security.get("ssl", False)
    result.mixed_content = security.get("mixed_content", False)
    result.x_frame_options = security.get("x_frame_options", "")
    result.x_content_type_options = security.get("x_content_type_options", "")

    # Tracking audit
    tracking = audit_tracking(soup)
    result.has_ga4 = tracking.get("has_ga4", False)
    result.has_fb_pixel = tracking.get("has_fb_pixel", False)
    result.has_gtm = tracking.get("has_gtm", False)
    result.tracking_count = tracking.get("tracking_count", 0)

    # Score
    result.score, recs, critical = score_seo(seo, security, tracking, perf)
    result.grade = grade_from_score(result.score)
    result.recommendations = recs
    result.critical_issues = critical

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

    print(f"\nScore: {r.score}/100  Grade: {r.grade}")
    print(f"Title: {r.title[:80]}")
    print(f"H1s: {r.h1_count} | SSL: {r.ssl} | GA4: {r.has_ga4}")
    print(f"\nCritical issues ({len(r.critical_issues)}):")
    for i in r.critical_issues:
        print(f"  [!] {i}")
    print(f"\nRecommendations ({len(r.recommendations)}):")
    for i in r.recommendations:
        print(f"  → {i}")
