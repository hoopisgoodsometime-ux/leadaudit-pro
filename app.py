#!/usr/bin/env python3
"""
Influencer Audit Service -- JARVIS Powered + Monetized (AI)️
Stripe-powered SaaS: $5/report or $29/month unlimited
"""

import datetime
import json
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import stripe
from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from website_audit import audit_website, AuditResult

# ── Config ─────────────────────────────────────────────────────────────────
stripe.api_key = "sk_test_placeholder_replace_with_real_key"  # Set via env STRIPE_SECRET_KEY
endpoint_secret = "whsec_placeholder_replace_with_real_webhook_secret"  # Set via env STRIPE_WEBHOOK_SECRET

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
REPORTS_DIR = BASE_DIR / "reports"
AUDITS_DB = BASE_DIR / "audits.db"

TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))
app.config["STRIPE_PUBLISHABLE_KEY"] = "pk_test_placeholder_replace_with_real_key"  # Set via env

FFMPEG_PATH = r"C:\Users\TRUSTY\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"

# ── Pricing ─────────────────────────────────────────────────────────────────
PRICES = {
    "single": "price_placeholder_single_report",   # Replace with real Stripe Price ID
    "monthly": "price_placeholder_monthly_unlimited",  # Replace with real Stripe Price ID
}
PRODUCT_NAME = "Influencer Audit Pro"
PRODUCT_URL = "http://localhost:5001"

# ── Free audit tracking (1 free per IP per day) ─────────────────────────────
FREE_AUDITS = {}  # {ip: {"count": 0, "date": "2026-07-24"}}

def get_free_audit_allowed(ip):
    today = datetime.date.today().isoformat()
    if ip not in FREE_AUDITS or FREE_AUDITS[ip].get("date") != today:
        FREE_AUDITS[ip] = {"count": 0, "date": today}
    return FREE_AUDITS[ip]["count"] < 1

def use_free_audit(ip):
    today = datetime.date.today().isoformat()
    if ip not in FREE_AUDITS or FREE_AUDITS[ip].get("date") != today:
        FREE_AUDITS[ip] = {"count": 0, "date": today}
    FREE_AUDITS[ip]["count"] += 1

# ── Database ────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(AUDITS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY,
                platform TEXT,
                handle TEXT,
                status TEXT,
                report_path TEXT,
                created_at TEXT,
                completed_at TEXT,
                result_json TEXT,
                paid INTEGER DEFAULT 0,
                payment_id TEXT DEFAULT '',
                customer_ip TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                customer_ip TEXT,
                plan TEXT,
                amount_cents INTEGER,
                status TEXT,
                created_at TEXT,
                audit_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_outcomes (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                score INTEGER,
                grade TEXT,
                confidence TEXT,
                gene_trace TEXT,
                alternatives_considered TEXT,
                recommendations TEXT,
                outcome_result TEXT DEFAULT 'pending',
                outcome_verified_at TEXT DEFAULT '',
                outcome_notes TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
        """)

def save_audit(audit_id, platform, handle, status, report_path="", result_json="", paid=0, payment_id="", customer_ip=""):
    with sqlite3.connect(AUDITS_DB) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO audits (id, platform, handle, status, report_path, created_at, result_json, paid, payment_id, customer_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (audit_id, platform, handle, status, report_path, datetime.datetime.now().isoformat(), result_json, paid, payment_id, customer_ip))
        conn.commit()

def update_audit(audit_id, status, report_path="", result_json=""):
    with sqlite3.connect(AUDITS_DB) as conn:
        conn.execute("""
            UPDATE audits SET status=?, report_path=?, result_json=?, completed_at=? WHERE id=?
        """, (status, report_path, result_json, datetime.datetime.now().isoformat(), audit_id))
        conn.commit()

def get_audit(audit_id):
    with sqlite3.connect(AUDITS_DB) as conn:
        row = conn.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
        if not row:
            return None
        cols = ["id","platform","handle","status","report_path","created_at","completed_at","result_json","paid","payment_id","customer_ip"]
        return dict(zip(cols, row))

def get_audits():
    with sqlite3.connect(AUDITS_DB) as conn:
        rows = conn.execute("SELECT * FROM audits ORDER BY created_at DESC").fetchall()
        cols = ["id","platform","handle","status","report_path","created_at","completed_at","result_json","paid","payment_id","customer_ip"]
        return [dict(zip(cols, r)) for r in rows]

def save_payment(payment_id, customer_ip, plan, amount_cents, status, audit_id=""):
    with sqlite3.connect(AUDITS_DB) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO payments (payment_id, customer_ip, plan, amount_cents, status, created_at, audit_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (payment_id, customer_ip, plan, amount_cents, status, datetime.datetime.now().isoformat(), audit_id))
        conn.commit()

# ── Website Audit Outcomes ──────────────────────────────────────────────────

def save_website_audit_outcome(outcome_id: str, url: str, result: dict):
    """Save a website audit result BEFORE the outcome is known.
    This ordering — logging reasoning before outcome — is the critical mechanism
    that makes the calibration dataset real, not theater (Kailash, 2026-08-19)."""
    with sqlite3.connect(AUDITS_DB) as conn:
        now = datetime.datetime.now().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO audit_outcomes
            (id, url, score, grade, confidence, gene_trace, alternatives_considered,
             recommendations, outcome_result, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            outcome_id,
            url,
            result.get("score"),
            result.get("grade"),
            result.get("confidence"),
            json.dumps(result.get("gene_trace", [])),
            json.dumps(result.get("alternatives_considered", [])),
            json.dumps(result.get("recommendations", [])),
            now,
            now,
        ))
        conn.commit()

def update_website_audit_outcome(outcome_id: str, outcome_result: str, outcome_notes: str = ""):
    """Update an audit outcome once the delayed signal arrives.
    outcome_result: 'confirmed' | 'refuted' | 'inconclusive'"""
    valid = {"confirmed", "refuted", "inconclusive", "pending"}
    if outcome_result not in valid:
        raise ValueError(f"outcome_result must be one of {valid}")
    with sqlite3.connect(AUDITS_DB) as conn:
        conn.execute("""
            UPDATE audit_outcomes
            SET outcome_result=?, outcome_notes=?, outcome_verified_at=?, updated_at=?
            WHERE id=?
        """, (outcome_result, outcome_notes, datetime.datetime.now().isoformat(),
                datetime.datetime.now().isoformat(), outcome_id))
        conn.commit()

def get_website_audit_outcome(outcome_id: str):
    with sqlite3.connect(AUDITS_DB) as conn:
        row = conn.execute("SELECT * FROM audit_outcomes WHERE id=?", (outcome_id,)).fetchone()
        if not row:
            return None
        cols = ["id","url","score","grade","confidence","gene_trace",
                "alternatives_considered","recommendations","outcome_result",
                "outcome_verified_at","outcome_notes","created_at","updated_at"]
        d = dict(zip(cols, row))
        for key in ["gene_trace", "alternatives_considered", "recommendations"]:
            if d.get(key):
                try: d[key] = json.loads(d[key])
                except: pass
        return d

def get_outcomes_summary():
    """Return calibration stats: gene combos correlated with confirmed outcomes."""
    with sqlite3.connect(AUDITS_DB) as conn:
        rows = conn.execute("SELECT * FROM audit_outcomes WHERE outcome_result != 'pending'").fetchall()
        if not rows:
            return {"total": 0, "message": "No verified outcomes yet. Feedback loop not yet closed."}
        cols = ["id","url","score","grade","confidence","gene_trace",
                "alternatives_considered","recommendations","outcome_result",
                "outcome_verified_at","outcome_notes","created_at","updated_at"]
        outcomes = []
        for row in rows:
            d = dict(zip(cols, row))
            for k in ["gene_trace", "recommendations"]:
                try: d[k] = json.loads(d[k]) if d.get(k) else []
                except: pass
            outcomes.append(d)

        total = len(outcomes)
        confirmed = [o for o in outcomes if o["outcome_result"] == "confirmed"]
        refuted   = [o for o in outcomes if o["outcome_result"] == "refuted"]

        # Gene frequency analysis
        from collections import Counter
        gene_freq = Counter()
        for o in outcomes:
            for entry in o.get("gene_trace", []):
                gene_freq[entry.get("gene","unknown")] += 1

        confirmed_gene_freq = Counter()
        for o in confirmed:
            for entry in o.get("gene_trace", []):
                confirmed_gene_freq[entry.get("gene","unknown")] += 1

        return {
            "total": total,
            "confirmed": len(confirmed),
            "refuted": len(refuted),
            "inconclusive": total - len(confirmed) - len(refuted),
            "gene_frequency": dict(gene_freq),
            "confirmed_gene_frequency": dict(confirmed_gene_freq),
            "confidence_stats": {
                str(o.get("confidence","unknown")): o["outcome_result"]
                for o in outcomes
            }
        }

# ── YouTube Scraper ─────────────────────────────────────────────────────────
def _run_scrape(args, timeout=30):
    scr_path = str(BASE_DIR / "yt_scrape.py")
    try:
        result = subprocess.run(
            [sys.executable, scr_path] + args,
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except subprocess.TimeoutExpired:
        print(f"Scrape timed out after {timeout}s: {' '.join(args)}")
    except Exception as e:
        print(f"Scrape error: {e}")
    return None

def scrape_youtube(handle):
    result = _run_scrape(["scrape", handle], timeout=45)
    if result:
        result["platform"] = "YouTube"
        result["handle"] = handle.strip().replace("@", "")
        result["latest_videos"] = []
    return result

def get_channel_videos(channel_id, limit=10):
    result = _run_scrape(["videos", channel_id, str(limit)], timeout=30)
    return result if result is not None else []

def calculate_audit_score(channel_data, videos):
    if not channel_data:
        return 0, []
    score = 100
    reasons = []
    sub_count = channel_data.get("subscriber_count", 0)
    view_count = channel_data.get("view_count", 0)
    if sub_count > 0 and view_count > 0:
        ratio = view_count / sub_count
        if ratio < 1:
            score -= 40
            reasons.append(f"Very low view-to-sub ratio ({ratio:.1f}x) -- possible fake subscribers")
        elif ratio < 3:
            score -= 20
            reasons.append(f"Low view-to-sub ratio ({ratio:.1f}x) -- may indicate inflated followers")
        elif ratio > 50:
            score -= 5
            reasons.append(f"Very high ratio ({ratio:.0f}x) -- exceptionally engaging or old account")
    if videos:
        total_views = sum(v.get("views", 0) for v in videos)
        total_likes = sum(v.get("likes", 0) for v in videos)
        total_comments = sum(v.get("comments", 0) for v in videos)
        avg_views = total_views / len(videos) if videos else 0
        if total_views > 0:
            eng_rate = ((total_likes + total_comments) / total_views) * 100
            if eng_rate < 1:
                score -= 30
                reasons.append(f"Very low engagement rate ({eng_rate:.2f}%) -- likely bot followers")
            elif eng_rate < 3:
                score -= 15
                reasons.append(f"Low engagement rate ({eng_rate:.2f}%) -- below average")
            elif eng_rate >= 10:
                score += 5
                reasons.append(f"Excellent engagement rate ({eng_rate:.2f}%)")
    if sub_count > 1_000_000:
        if view_count / sub_count < 0.5:
            score -= 20
            reasons.append("Million+ followers but minimal views -- high bot probability")
    score = max(0, min(100, score))
    return score, reasons

# ── Report Generator ────────────────────────────────────────────────────────
def generate_text_report(audit_id, data, score, reasons, videos):
    lines = [
        "=" * 60,
        "  INFLUENCER AUDIT REPORT",
        "  Powered by JARVIS AI -- Device Fleet Analysis",
        "=" * 60,
        "",
        f"  Platform    : {data.get('platform', 'N/A')}",
        f"  Handle      : @{data.get('handle', 'N/A')}",
        f"  Channel     : {data.get('title', 'N/A')}",
        f"  Report ID   : {audit_id}",
        f"  Generated   : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "-" * 60,
        "  AUDIENCE AUTHENTICITY SCORE",
        "-" * 60,
    ]
    if score >= 80:
        grade = "A -- HIGH AUTHENTICITY"
        color_note = "Followers appear genuine and engaged"
    elif score >= 60:
        grade = "B -- LIKELY AUTHENTIC"
        color_note = "Mostly genuine with minor irregularities"
    elif score >= 40:
        grade = "C -- UNCLEAR"
        color_note = "Some signs of inauthentic activity"
    elif score >= 20:
        grade = "D -- SUSPICIOUS"
        color_note = "Strong indicators of fake followers"
    else:
        grade = "F -- HIGH RISK"
        color_note = "Likely heavily inflated follower count"
    lines.extend([
        f"  Score       : {score}/100",
        f"  Grade       : {grade}",
        f"  Verdict     : {color_note}",
        "",
    ])
    if reasons:
        lines.append("  RISK FLAGS:")
        for r in reasons:
            lines.append(f"    - {r}")
        lines.append("")
    lines.extend([
        "-" * 60,
        "  CHANNEL METRICS",
        "-" * 60,
        f"  Subscribers : {data.get('subscriber_count', 0):,}",
        f"  Total Views : {data.get('view_count', 0):,}",
        f"  Videos      : {data.get('video_count', 0):,}",
        "",
    ])
    if videos:
        lines.extend([
            "-" * 60,
            "  RECENT VIDEO PERFORMANCE (Sample)",
            "-" * 60,
            f"  {'Title':<40} {'Views':>10} {'Likes':>10} {'Eng%':>8}",
            f"  {'-'*40} {'-'*10} {'-'*10} {'-'*8}",
        ])
        for v in videos[:10]:
            title = (v.get("title", "") or "N/A")[:40]
            views = v.get("views", 0) or 0
            likes = v.get("likes", 0) or 0
            eng = ((likes) / views * 100) if views > 0 else 0
            lines.append(f"  {title:<40} {views:>10,} {likes:>10,} {eng:>7.1f}%")
        lines.append("")
    lines.extend([
        "=" * 60,
        "  DISCLAIMER",
        "=" * 60,
        "  This report is generated by AI analysis of publicly available",
        "  social media data. Results are estimates and should not be",
        "  used as the sole basis for financial or business decisions.",
        "  Generated by JARVIS AI -- Influencer Audit Module v2.0",
        "  © Device Fleet Revenue Systems -- Licensed Product",
        "=" * 60,
    ])
    report_path = REPORTS_DIR / f"{audit_id}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return str(report_path)

# ── Audit Worker ────────────────────────────────────────────────────────────
def run_audit(audit_id, platform, handle, paid, payment_id, customer_ip):
    save_audit(audit_id, platform, handle, "running", paid=paid, payment_id=payment_id, customer_ip=customer_ip)
    timer = threading.Timer(120.0, lambda: _timeout_audit(audit_id))
    timer.daemon = True
    timer.start()
    try:
        if platform == "YouTube":
            channel_data = scrape_youtube(handle)
            if not channel_data:
                timer.cancel()
                update_audit(audit_id, "failed", "", json.dumps({"error": "Channel not found"}))
                return
            videos = get_channel_videos(channel_data.get("channel_id"), limit=10)
            score, reasons = calculate_audit_score(channel_data, videos)
            channel_data["latest_videos"] = videos
            report_path = generate_text_report(audit_id, channel_data, score, reasons, videos)
            result = {"score": score, "reasons": reasons, "channel": channel_data}
            timer.cancel()
            update_audit(audit_id, "complete", report_path, json.dumps(result, default=str))
        else:
            timer.cancel()
            update_audit(audit_id, "failed", "", json.dumps({"error": f"Platform {platform} not yet supported"}))
    except Exception as e:
        timer.cancel()
        import traceback
        traceback.print_exc()
        update_audit(audit_id, "failed", "", json.dumps({"error": str(e)}))

def _timeout_audit(audit_id):
    try:
        with sqlite3.connect(AUDITS_DB) as conn:
            conn.execute("UPDATE audits SET status=? WHERE id=?", ("failed", audit_id))
            conn.commit()
    except:
        pass

# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/pricing")
def pricing():
    return render_template("pricing.html")

@app.route("/success")
def success():
    return render_template("success.html")

@app.route("/cancel")
def cancel():
    return render_template("cancel.html")

@app.route("/website-audit")
def website_audit():
    return render_template("website-audit.html")

@app.route("/sitemap.xml")
def sitemap():
    response = send_file(BASE_DIR / "templates" / "sitemap.xml", mimetype="application/xml")
    response.headers["Cache-Control"] = "no-cache"
    return response

# ── Blog ────────────────────────────────────────────────────────────────────
POSTS = {
    "fake-influencer-red-flags": {
        "title": "7 Red Flags That Signal a Fake Influencer (Free Checklist)",
        "category": "Influencer Analysis",
        "date": "August 2026",
        "excerpt": "Most fake influencers are obvious if you know what to look for. Here are the 7 signals that matter most — and how to check each one in under 60 seconds.",
        "content": """<p>You wouldn't hand your marketing budget to someone who rents their audience. But that's exactly what happens every day when brands pay for influencer campaigns without running the numbers first.</p><p>Fake influencers are a billion-dollar problem. Fake followers cost brands an estimated $1.3 billion in wasted sponsorship spend every year. And the scary part? Most fakes look real until you dig.</p><p>Here's how to spot them.</p><hr class="divider"><h2>1. The View-to-Subscriber Ratio Is Below 1x</h2><p>This is the single most reliable signal. If a channel has 500K subscribers but their videos consistently get under 500K views, something is seriously wrong. Real, engaged subscribers actually watch the content.</p><p><strong>The benchmark:</strong> Most healthy channels have a view-to-subscriber ratio between 3x and 20x. Below 1x is a serious red flag. Below 0.5x is almost certain bot activity.</p><div class="callout"><div class="callout-title">💡 Quick Check</div>Look at the subscriber count on the channel page, then check the view count on their most recent 5 videos. If the views are consistently below the subscriber count, walk away.</div><h2>2. Engagement Rate Is Below 1%</h2><p>Engagement rate is likes + comments divided by total views, expressed as a percentage. The formula: <strong>(likes + comments) / views × 100</strong>.</p><p>Real audiences engage. Fake ones don't. Here's what engagement looks like across follower tiers:</p><ul><li><strong>Macro influencers (1M+ followers):</strong> 1–3% is normal. Anything below 0.5% is suspicious.</li><li><strong>Mid-tier (100K–1M):</strong> 3–6% is healthy. Below 1% is a warning sign.</li><li><strong>Micro influencers (10K–100K):</strong> 6–10% is typical. Below 2% warrants suspicion.</li></ul><p>If the numbers don't add up, the followers probably aren't real.</p><hr class="divider"><h2>3. Subscriber Count Jumped Suddenly</h2><p>Organic subscriber growth is gradual. If you look at a creator's social blade or similar tracker and see massive single-day or single-week jumps, they almost certainly bought those followers.</p><p>Real growth curves are smooth. Bot growth is lumpy and obvious.</p><h2>4. Comments Are Generic or Empty</h2><p>Scroll past the first three comments. If you see "great video," "love this," "so cool," with no specific reference to the content — those comments might be from bots or comment pods.</p><p>Real viewers talk about what was actually in the video.</p><h2>5. The Account Has No Other Social Presence</h2><p>Most real influencers distribute across multiple platforms — YouTube, Instagram, Twitter, TikTok. A channel with 2 million subscribers but zero presence anywhere else is... unusual.</p><p>This isn't a dealbreaker on its own, but combined with other signals, it matters.</p><h2>6. The Content Volume Doesn't Match the Audience Size</h2><p>A channel with 2M subscribers that posts twice a year isn't building an organic community — they're sitting on a purchased subscriber base.</p><p>Real influencers — even the lazy ones — post consistently.</p><h2>7. No Visible Website or Business Presence</h2><p>Established real influencers almost always have a brand, a business entity, a website, or at minimum a Linktree with functioning links. Fake accounts don't bother with this because they were built purely for the numbers.</p><hr class="divider"><div class="checklist"><div class="checklist-title">✓ Your Fake Influencer Checklist</div><ul><li>View-to-subscriber ratio above 1x (ideally above 3x)</li><li>Engagement rate above 1% (higher for smaller accounts)</li><li>Gradual, smooth subscriber growth over time</li><li>Specific, content-related comments from real people</li><li>Consistent content posting schedule</li><li>Multi-platform presence</li><li>Active website or business links</li></ul></div><h2>What To Do Next</h2><p>Run every influencer through a free audit before signing any contract. Our tool checks all seven of these signals automatically — view ratios, engagement rates, subscriber patterns — and gives you a plain-English authenticity score in under 30 seconds.</p><p>It takes longer to pour your coffee than to get scammed by a fake influencer. Do the audit first.</p>"""},
    "view-to-subscriber-ratio": {
        "title": "The View-to-Subscriber Ratio: The Single Best Fake-Follower Signal",
        "category": "Influencer Analysis",
        "date": "August 2026",
        "excerpt": "Subscriber count means nothing without the view history to back it up. Here's the math, the benchmarks, and how to use this ratio on every channel you audit.",
        "content": """<p>Walk into a room of 10,000 people. Now tell me how many of them actually showed up because they wanted to be there.</p><p>That's the view-to-subscriber ratio problem in a nutshell. The subscriber count tells you how many people <em>said</em> they care. The view count tells you how many actually showed up.</p><h2>Why Subscriber Count Is Almost Useless Alone</h2><p>YouTube subscriber counts can be inflated through:</p><ul><li>Contests that incentivize follow-for-follow behavior</li><li>Giveaways that attract people who don't actually want the content</li><li>Bot purchases (yes, it's a real service you can buy)</li><li>Sub4Sub communities where real people subscribe to inflate numbers</li></ul><p>But views are harder to fake at scale. Real people have to actually click. Real view counts require real attention.</p><h2>The Formula</h2><p><strong>View-to-Subscriber Ratio = Average Views per Video / Total Subscribers</strong></p><p>For example: A channel with 100,000 subscribers whose last 10 videos averaged 15,000 views has a ratio of 0.15x — well below healthy.</p><h2>Healthy Ratio Benchmarks</h2><p>These are based on analysis across hundreds of thousands of YouTube channels:</p><ul><li><strong>0.0x – 0.5x:</strong> Severe red flag. Almost certainly inflated or purchased followers.</li><li><strong>0.5x – 1x:</strong> Warning zone. Could be a declining channel or a subscriber inflation issue.</li><li><strong>1x – 3x:</strong> Normal for established channels with large subscriber bases.</li><li><strong>3x – 10x:</strong> Healthy, engaged audience. Active subscriber base.</li><li><strong>10x+:</strong> Exceptional. Strong organic reach, subscriber base actively watches new content.</li></ul><div class="callout"><div class="callout-title">⚠️ Important Caveat</div>Channels that primarily publish short-form content (YouTube Shorts) will naturally show higher ratios because Shorts get discovery views from non-subscribers. For long-form channels, use this ratio on videos over 5 minutes.</div><h2>How To Use This In Your Audits</h2><p>Pick the channel's 5 most recent videos. Average their view counts. Divide by subscriber count. That's your ratio.</p><p>If it's below 1x, do not work with that creator until you understand why. The most common explanations are not good ones: follower inflation, inactive/left subscribers, or content that stopped resonating.</p><p>None of those make for a good brand partnership.</p><p>Run the full audit at LeadAudit Pro — it calculates this ratio automatically and flags it in the score.</p>"""},
    "engagement-rate-benchmarks": {
        "title": "Real Engagement Rate Benchmarks by Follower Count (2026)",
        "category": "Industry Data",
        "date": "August 2026",
        "excerpt": "What counts as 'good' engagement changes completely depending on follower count. We break down the real numbers so you stop comparing a 10K account to a 10M account.",
        "content": """<p>Comparing the engagement rate of a 10K-follower micro-creator to a 10M-follower mega-influencer is like comparing the gas mileage of a bicycle to a freight truck. They're different machines for different purposes.</p><p>Yet brands do this all the time. Then they get confused about why a macro influencer's engagement looked "low" but actually made sense for their size.</p><h2>The Engagement Rate Formula</h2><p><strong>Engagement Rate = (Likes + Comments) / Views × 100</strong></p><p>Some people use followers as the denominator. Views as denominator is more honest — it measures actual content resonance, not just passive following.</p><h2>2026 Benchmarks by Platform</h2><p>For YouTube specifically:</p><ul><li><strong>Followers under 10K:</strong> 6–12% is excellent. Under 2% is a red flag.</li><li><strong>Followers 10K–100K:</strong> 3–8% is healthy. Under 1% is worth questioning.</li><li><strong>Followers 100K–1M:</strong> 2–5% is normal. Under 0.5% is suspicious.</li><li><strong>Followers 1M+:</strong> 1–3% is typical. Under 0.3% suggests inflated followers.</li></ul><div class="callout"><div class="callout-title">📊 Why It Drops With Size</div>As audiences grow, the percentage of actively-engaged followers always decreases. A 10K channel likely has 2-3K genuine superfans who watch and engage with everything. A 10M channel might have 500K superfans — still a lot, but they're 5% of the total, not 25%.</div><h2>What Good Engagement Actually Looks Like</h2><p>Volume matters. A 3% engagement rate on a video with 50,000 views is 1,500 interactions. A 5% engagement rate on a video with 2,000 views is only 100 interactions.</p><p>Always look at both the rate AND the absolute numbers together.</p><h2>How We Calculate It</h2><p>LeadAudit Pro averages engagement across the creator's last 10 published videos, not just the most recent one. This prevents cherry-picking and gives you a real picture of sustained audience engagement — not a single viral outlier.</p><p>No tool catches everything. But engagement rate analysis combined with view-to-subscriber ratio will catch 90% of obviously inflated influencer accounts.</p>"""},
    "website-speed-conversion": {
        "title": "Why Website Speed Is a Revenue Problem, Not a Vanity Metric",
        "category": "Web Performance",
        "date": "August 2026",
        "excerpt": "Every 1 second of load time costs you roughly 7% in conversions. Run a free website audit to see where you stand — then use this guide to fix what matters most.",
        "content": """<p>Nobody celebrates a fast-loading website. But everyone notices a slow one.</p><p>Google's data shows that 53% of mobile users leave a page if it takes more than 3 seconds to load. And for every 1-second delay in page load time, conversions drop by approximately 7%.</p><p>A business doing $10,000 a month in revenue, losing 7% to a 1-second delay — that's $700 a month, or $8,400 a year. A slow website isn't a technical problem. It's a revenue leak.</p><h2>What Actually Slows Websites Down</h2><p>The usual suspects, in order of frequency:</p><ul><li><strong>Unoptimized images:</strong> Sending 2MB JPEG when a 200KB WebP would do</li><li><strong>Too many JavaScript files:</strong> Render-blocking scripts that pause everything</li><li><strong>No CDN:</strong> Serving from a single server location instead of edge nodes</li><li><strong>Render-blocking CSS:</strong> Stylesheets that prevent the page from displaying</li><li><strong>Excessive third-party scripts:</strong> Chat widgets, analytics, tag managers</li></ul><h2>The Speed Numbers That Matter</h2><p>Run your site through our free website auditor and look for these:</p><ul><li><strong>Load time:</strong> Under 1.5 seconds is good. Over 3 seconds is costing you.</li><li><strong>Page size:</strong> Under 1MB is healthy. Over 3MB is too heavy.</li><li><strong>TTFB (Time to First Byte):</strong> Under 800ms is acceptable. Over 1.5s suggests a server problem.</li><li><strong>Total blocking time:</strong> Under 200ms. Over 500ms will feel sluggish.</li></ul><div class="callout"><div class="callout-title">⚡ Quick Win</div>Compress your largest images with Squoosh.app or TinyPNG before uploading. This one change alone can cut load time by 40–60% on image-heavy pages.</div><h2>The Mobile Imperative</h2><p>More than 60% of web traffic comes from mobile devices. But mobile performance is often an afterthought. Test your site on a mid-range Android phone on a 4G connection — not your iPhone 15 Pro on fiber.</p><p>If it feels slow to you, it's slower for your actual users.</p><p>Run a free website audit now to see your actual performance numbers. The report takes 20 seconds and gives you plain-English recommendations, not technical jargon.</p>"""},
    "building-web-family": {
        "title": "How to Build a Web Family That Compounds Over Time",
        "category": "Strategy",
        "date": "August 2026",
        "excerpt": "SEO, backlinks, blog content, and community — not as separate tactics, but as one interconnected system. Here's the framework for building it right.",
        "content": """<p>Most people think of web presence as a checklist: a website, some social accounts, maybe a blog. Check, check, check. Done.</p><p>That approach gets you a website that exists but nobody visits. A collection of social accounts with no followers. A blog that nobody reads.</p><p>A real web family is different. It's a system where every part makes the other parts stronger — where the blog feeds the community, the community feeds the backlinks, the backlinks feed the SEO, and the SEO brings new people into the family.</p><h2>The Four Pillars</h2><p>Think of your web family as four interconnected pillars. Each one reinforces the others.</p><h3>1. Blog — The Voice</h3><p>Your blog is where you prove you know what you're talking about. Not marketing speak. Not keyword stuffing. Real, useful content that makes someone smarter or better at their job after reading it.</p><p>The goal: every post should answer a real question someone has, better than anything else on the internet.</p><h3>2. Community — The Presence</h3><p>You build community by showing up where your people already gather — not by waiting for them to find your website.</p><p>This means Reddit threads, Twitter conversations, LinkedIn posts, Hacker News comments. Genuine participation, not self-promotion. Help first. Pitch second.</p><h3>3. Backlinks — The Signal</h3><p>When another site links to you, they're vouching for you. Each quality backlink is a vote of confidence in the eyes of search engines — and real humans who discover you through those links.</p><p>Good backlinks come from: guest posts on relevant blogs, podcast appearances, original research that journalists cite, directories with editorial standards.</p><p>Bad backlinks — link farms, PBNs, bought links — can get you penalized. Never buy links. Ever.</p><h3>4. SEO — The Discovery Layer</h3><p>SEO is what makes your web family findable. Not just by search engines — but by making your content organized, fast, and technically sound enough that it deserves to rank.</p><p>Technical SEO (site speed, SSL, mobile-friendly) is table stakes. Content SEO is what makes you win.</p><h2>How They Connect</h2><p>Here's the compounding loop: A genuinely useful blog post earns links naturally because people cite useful things. Those links improve your search rankings. Better rankings bring more readers. Some of those readers join your community. Community members share your content and create more linking opportunities.</p><p>Round and round. Each element makes the whole system stronger.</p><div class="callout"><div class="callout-title">🕉️ The Principle</div>The web family works when every element is genuinely valuable on its own — not just as a tactic. Build for people first. The search engines and backlinks follow naturally.</div><h2>Start Here</h2><p>Audit your current website with our free tool to understand where you stand technically. Then pick one pillar to start building — probably the blog, because that's where your voice lives.</p><p>You don't need all four pillars before you start. You need one pillar that's genuinely good. Everything else grows from there.</p>"""},
}


@app.route("/blog")
def blog():
    return render_template("blog.html")


@app.route("/blog/<slug>")
def blog_post(slug):
    post = POSTS.get(slug)
    if not post:
        return render_template("blog.html"), 404
    return render_template("post.html", **post)

# ── Website Audit Outcomes API ──────────────────────────────────────────────

@app.route("/api/report-outcome", methods=["POST"])
def api_report_outcome():
    """Report the delayed outcome of a website audit.
    Call this weeks later when you know if the recommendations worked.

    Body: { "outcome_id": "<id>", "result": "confirmed|refuted|inconclusive", "notes": "..." }
    """
    data = request.json or {}
    outcome_id = data.get("outcome_id", "").strip()
    result_str = data.get("result", "").strip()
    notes = data.get("notes", "").strip()

    if not outcome_id:
        return jsonify({"error": "outcome_id is required"}), 400
    if result_str not in ("confirmed", "refuted", "inconclusive"):
        return jsonify({"error": "result must be 'confirmed', 'refuted', or 'inconclusive'"}), 400

    existing = get_website_audit_outcome(outcome_id)
    if not existing:
        return jsonify({"error": "Outcome ID not found"}), 404

    update_website_audit_outcome(outcome_id, result_str, notes)
    return jsonify({
        "outcome_id": outcome_id,
        "url": existing["url"],
        "audit_score": existing["score"],
        "outcome_result": result_str,
        "verified_at": datetime.datetime.now().isoformat(),
        "notes": notes,
        "message": f"Outcome logged as {result_str}. Calibration dataset updated."
    })


@app.route("/api/outcomes-summary", methods=["GET"])
def api_outcomes_summary():
    """Return calibration stats: which gene combos correlated with confirmed outcomes."""
    summary = get_outcomes_summary()
    return jsonify(summary)


@app.route("/api/outcome/<outcome_id>", methods=["GET"])
def api_get_outcome(outcome_id):
    outcome = get_website_audit_outcome(outcome_id)
    if not outcome:
        return jsonify({"error": "Outcome not found"}), 404
    return jsonify(outcome)

# ── Payment Routes ──────────────────────────────────────────────────────────
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json or {}
    plan = data.get("plan", "single")  # "single" or "monthly"
    
    if plan not in PRICES:
        return jsonify({"error": "Invalid plan"}), 400
    
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price": PRICES[plan],
                "quantity": 1,
            }],
            mode="subscription" if plan == "monthly" else "payment",
            success_url=f"{PRODUCT_URL}/success?session_id={{CHECKOUT_SESSION_ID}}&plan={plan}",
            cancel_url=f"{PRODUCT_URL}/cancel",
            metadata={"plan": plan}
        )
        return jsonify({"url": checkout_session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return jsonify({"error": f"Webhook error: {e}"}), 400
    
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        payment_id = session.get("id", "")
        customer_ip = session.get("client_reference_id", session.get("customer_details", {}).get("email", ""))
        plan = session.get("metadata", {}).get("plan", "single")
        amount = session.get("amount_total", 0)
        save_payment(payment_id, customer_ip, plan, amount, "completed")
    
    return jsonify({"status": "received"})

@app.route("/check-payment", methods=["GET"])
def check_payment():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        return jsonify({
            "status": session.payment_status,
            "plan": session.metadata.get("plan", "single") if session.metadata else "single"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Website Audit Routes ───────────────────────────────────────────────────

@app.route("/api/website-audit", methods=["POST"])
def api_website_audit():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        result = audit_website(url)
        return jsonify(result.to_dict())
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Audit API Routes ───────────────────────────────────────────────────────
@app.route("/api/audit", methods=["POST"])
def api_audit():
    data = request.json
    platform = data.get("platform", "YouTube").strip()
    raw = data.get("handle", "").strip()
    m = re.search(r"(?:youtube\.com/(?:channel/|@|c/|user/)|@)([^/?\s]+)", raw, re.I)
    handle = m.group(1) if m else raw.replace("@", "")
    client_ip = request.remote_addr or "127.0.0.1"
    
    if not handle:
        return jsonify({"error": "Handle is required"}), 400
    
    audit_id = str(uuid.uuid4())[:8]
    paid = 0
    payment_id = ""
    
    # Check free trial
    if get_free_audit_allowed(client_ip):
        paid = 1
        payment_id = "free_trial"
        use_free_audit(client_ip)
    
    t = threading.Thread(target=run_audit, args=(audit_id, platform, handle, paid, payment_id, client_ip), daemon=True)
    t.start()
    
    return jsonify({
        "audit_id": audit_id,
        "status": "started",
        "free_trial": paid == 1,
        "message": f"Audit for @{handle} started. Results in ~30 seconds."
    })

@app.route("/api/status/<audit_id>")
def api_status(audit_id):
    result = get_audit(audit_id)
    if not result:
        return jsonify({"error": "Audit not found"}), 404
    if result["result_json"]:
        result["result"] = json.loads(result["result_json"])
    return jsonify(result)

@app.route("/api/audits")
def api_audits():
    return jsonify(get_audits())

@app.route("/api/report/<audit_id>")
def api_report(audit_id):
    result = get_audit(audit_id)
    if not result or not result["report_path"]:
        return "Report not found", 404
    # For paid reports only
    if result["paid"] != 1:
        return jsonify({"error": "Payment required to download this report"}), 402
    return send_file(result["report_path"], as_attachment=True)

@app.route("/api/quick-audit", methods=["POST"])
def api_quick_audit():
    data = request.json
    raw = data.get("handle", "").strip()
    m = re.search(r"(?:youtube\.com/(?:channel/|@|c/|user/)|@)([^/?\s]+)", raw, re.I)
    handle = m.group(1) if m else raw.replace("@", "")
    client_ip = request.remote_addr or "127.0.0.1"
    
    if not handle:
        return jsonify({"error": "Handle required"}), 400
    
    # Check free trial
    free = get_free_audit_allowed(client_ip)
    
    try:
        channel = scrape_youtube(handle)
        if not channel:
            return jsonify({"error": "Channel not found"}), 404
        videos = get_channel_videos(channel.get("channel_id"), limit=20)
        score, reasons = calculate_audit_score(channel, videos)
        return jsonify({
            "platform": "YouTube",
            "handle": handle,
            "title": channel.get("title"),
            "subscribers": channel.get("subscriber_count", 0),
            "score": score,
            "reasons": reasons,
            "videos": videos[:10],
            "free_trial_used": not free,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Init ─────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print('\n   Influencer Audit Service -- JARVIS SaaS v2.0\n   Open: http://localhost:5001\n   Pricing: $5/report | $29/month unlimited\n   Ctrl+C to stop\n')
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
