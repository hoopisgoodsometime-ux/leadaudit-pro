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
        "content": """<p>You know that feeling when something just feels... off? Like the applause at a standup comedy show that sounds a little too rehearsed, a little too perfectly timed?</p><p>That's your nervous system's threat detection working. It's telling you something isn't genuine.</p><p>Most fake influencers set off that same alarm — if you know how to listen for it.</p><p>Here's how.</p><hr class="divider"><h2>1. The subscriber count and the view count live in different universes.</h2><p>Go to a YouTube channel. Note the subscriber count. Then look at their last 10 videos. Add up the views, divide by 10.</p><p>If the average view count is less than the subscriber count — that's a problem.</p><p>A channel with 300,000 subscribers where videos scrape together 40,000 views? Those subscribers aren't watching. They may not even be real.</p><p>The ratio you want: videos getting at least as many views as the channel has subscribers. Anything below that needs an explanation.</p><h2>2. The comments are... weird.</h2><p>Open the comments on a video. Read five at random.</p><p>Do they sound like actual humans who watched the content? "Amazing video 🔥" and "So inspiring 🙏" aren't bad on their own. But when every comment reads like it was written by someone who absorbed zero information from the video — that's a flag.</p><p>Real comments reference specifics. Fake ones are vague and vaguely positive. Look for comments that say something only someone who watched could say.</p><h2>3. The engagement rate is suspiciously low.</h2><p>Engagement rate is just (likes + comments) / views. It's the ratio of people who actually did something to people who just watched.</p><p>For a channel under 100K subscribers, you want to see at least 2-3% engagement. Under 1%? That's a ghost town. Nobody who follows this person actually cares enough to like or comment.</p><p>That says everything about what those follower numbers are worth.</p><h2>4. The subscriber count has suspicious growth spurts.</h2><p>Go to Social Blade or similar. Look at the channel's subscriber graph over time.</p><p>Real channels grow gradually. Fake channels grow in sudden, sharp jumps — 50,000 new subscribers in a week, then flat for months. Those spikes almost always come from purchased followers, sub4sub communities, or giveaways that attracted people who wanted free stuff, not your content.</p><p>Smooth growth curves = real people. Lumpy ones = artificial inflation.</p><h2>5. There's no other footprint.</h2><p>Established creators almost always have an Instagram, a Twitter, a business email, something. When a channel has 2 million YouTube subscribers but zero presence anywhere else — including no website, no link in bio, nothing — that's strange.</p><p>Real influencers monetize. And you can't monetize without directing people somewhere.</p><h2>6. The content volume doesn't match the audience size.</h2><p>A channel with a million subscribers that posts twice a year isn't a channel with a million fans. It's a channel that accumulated followers at some point and then stopped giving people a reason to stay.</p><p>Real influencers — even lazy ones — post every couple of weeks minimum. If the upload schedule is barren, the audience probably is too.</p><h2>7. The account has nothing to sell and no way to buy it.</h2><p>Real influencers have a product, a brand, a sponsorship page, a Patreon, a course, a consultation booking link — something that tells you this person is running a business.</p><p>An account with massive follower counts and nothing to buy means the followers were never meant to become customers. They were meant to look impressive on a media kit.</p><hr class="divider"><div class="checklist"><div class="checklist-title">✓ Your Fake Influencer Checklist</div><ul><li>Average video views ≥ subscriber count</li><li>Comments reference specific content, not generic positivity</li><li>Engagement rate above 1% (higher for smaller accounts)</li><li>Smooth subscriber growth curve on Social Blade</li><li>Multi-platform presence</li><li>Consistent posting schedule</li><li>Active product, brand, or business link</li></ul></div><p>If you find yourself checking more than two of these boxes, walk away. The math isn't going to work out in your favour.</p><p>And if you want to skip the manual work — run a free audit at LeadAudit Pro. We calculate all of this automatically and give you a score in under 30 seconds. It's the difference between knowing and guessing.</p>"""},
    "view-to-subscriber-ratio": {
        "title": "The View-to-Subscriber Ratio: The Single Best Fake-Follower Signal",
        "category": "Influencer Analysis",
        "date": "August 2026",
        "excerpt": "Subscriber count means nothing without the view history to back it up. Here's the math, the benchmarks, and how to use this ratio on every channel you audit.",
        "content": """<p>Imagine you're a promoter. Someone tells you they've got 10,000 people in a crowd outside ready to buy your product.</p><p>You walk outside. Three people are there.</p><p>That's the subscriber count problem in a nutshell.</p><p>The subscriber number is what the influencer claims. The views are who actually showed up. And in most industries, who actually shows up is all that matters.</p><hr class="divider"><h2>Why subscriber counts lie</h2><p>Buying followers is cheap and fast. There are services you can pay $50 for 10,000 followers right now. These followers are bots, inactive accounts, or people who followed-for-follow and immediately forgot about the channel.</p><p>YouTube's algorithm doesn't delete subscriber counts from people who leave. That 500K figure includes everyone who ever clicked subscribe and then stopped caring.</p><p>Contests and giveaways attract followers who want free stuff — not people who care about your content.</p><p>Views, on the other hand, require actual human attention. You can't bot views at scale without getting caught. A view has to come from a real device, a real person, real time on page.</p><p>That's why the view-to-subscriber ratio is the most reliable signal in influencer auditing.</p><hr class="divider"><h2>The formula</h2><p><strong>Average views on last 10 videos / Total subscribers = Ratio</strong></p><hr class="divider"><h2>Healthy benchmarks</h2><ul><li><strong>Below 0.5x:</strong> Almost certainly purchased or deeply inflated followers. High risk.</li><li><strong>0.5x–1x:</strong> Significant red flag. Most subscribers aren't engaging.</li><li><strong>1x–3x:</strong> Normal for established channels. Acceptable.</li><li><strong>3x–10x:</strong> Healthy. Real, engaged subscriber base.</li><li><strong>10x+:</strong> Excellent. Active subscribers who consistently watch new content.</li></ul><div class="callout"><div class="callout-title">⚠️ One important caveat</div>Shorts confuse this metric. YouTube Shorts get massive discovery from non-subscribers — so channels that rely on Shorts will show inflated ratios that have nothing to do with their subscriber loyalty. Apply this ratio to videos longer than 5 minutes for the truest signal.</div><hr class="divider"><h2>The practical version</h2><p>Pick the channel's 5 most recent videos. Check their view counts. Divide by subscribers. If you're below 1x, you need a very good explanation before spending any money with that creator.</p><p>Run the full audit at LeadAudit Pro — it does this calculation automatically across the creator's last 20 videos, so you get a real picture of their actual audience, not a cherry-picked one.</p>"""},
    "engagement-rate-benchmarks": {
        "title": "Real Engagement Rate Benchmarks by Follower Count (2026)",
        "category": "Industry Data",
        "date": "August 2026",
        "excerpt": "What counts as 'good' engagement changes completely depending on follower count. We break down the real numbers so you stop comparing a 10K account to a 10M account.",
        "content": """<p>A 10,000-person music festival and a 10,000-person village aren't the same thing. One has 10,000 people who chose to be there. The other has 10,000 people who happen to live near each other.</p><p>Yet brands do this with influencer accounts all the time. They see "500K followers" and get excited without asking: how many of those half-million people actually give a damn?</p><p>That's what engagement rate measures. Not the crowd size — the crowd that cares.</p><hr class="divider"><h2>The formula</h2><p><strong>(Likes + Comments) / Views × 100</strong></p><p>Some tools calculate this against followers. We calculate it against views, because views represent actual people who chose to watch. Followers are a wishlist. Views are a headcount.</p><hr class="divider"><h2>Why engagement drops as channels grow</h2><p>When someone has 5,000 subscribers, they're likely friends, family, and early fans who found the content and actually watch every upload. Almost everyone who follows them is a genuine superfan.</p><p>When someone has 5 million subscribers, they're a media company. Most of those millions followed years ago, unsubscribed emotionally, and never clicked the button. The superfan percentage shrinks. That's just how scale works — the 500K remaining superfans are still a massive audience, but they're a smaller percentage of the total.</p><hr class="divider"><h2>The 2026 benchmarks (for YouTube, calculated on views)</h2><ul><li><strong>Under 10K subscribers:</strong> 6–12% is excellent. Under 2% is worth questioning.</li><li><strong>10K–100K:</strong> 3–8% is healthy. Under 1% suggests inflated followers.</li><li><strong>100K–1M:</strong> 2–5% is normal. Under 0.5% is suspicious.</li><li><strong>1M+ subscribers:</strong> 1–3% is typical. Under 0.3% strongly suggests purchased followers.</li></ul><hr class="divider"><h2>Context matters as much as the number</h2><p>A 2% engagement rate on a video with 200,000 views means 4,000 people interacted. A 10% engagement rate on a video with 2,000 views means 200 people interacted. The second creator has a more engaged audience relative to their size, but the first creator reaches more actual humans.</p><p>Always look at engagement rate and raw engagement together.</p><hr class="divider"><p>We calculate this automatically at LeadAudit Pro — pulling the last 20 videos, averaging engagement across all of them, and giving you one clear picture instead of one data point you might cherry-pick.</p>"""},
    "website-speed-conversion": {
        "title": "Why Website Speed Is a Revenue Problem, Not a Vanity Metric",
        "category": "Web Performance",
        "date": "August 2026",
        "excerpt": "Every 1 second of load time costs you roughly 7% in conversions. Run a free website audit to see where you stand — then use this guide to fix what matters most.",
        "content": """<p>You click a link. The page loads. You wait.</p><p>That pause — even three seconds — has a physiological effect on your nervous system. Your cortisol rises slightly. Your attention wavers. A small bit of trust evaporates before the page even loads.</p><p>This happens to your potential customers. Every day. On your website.</p><p>Google's research is blunt: 53% of mobile users abandon a page that takes more than 3 seconds to load. Three seconds. Less time than it takes to brew a cup of tea.</p><p>And here's the part most people miss — it compounds. Every visitor who leaves because your site is slow is a person who didn't convert, didn't read your content, didn't sign up. That lost traffic is gone. And Google's ranking factors penalize slow sites, so slow loading times mean fewer people find you organically in the first place.</p><p>It's a double penalty.</p><hr class="divider"><h2>The most common culprits</h2><ul><li><strong>Images that haven't been compressed.</strong> A 3MB photo when a 150KB WebP would have done the job. This is the single most common cause of slow sites.</li><li><strong>Too many JavaScript files</strong> loading before the page renders. Your browser has to download, parse, and execute all of it before showing anything.</li><li><strong>No CDN.</strong> Your server is in Sydney and someone's loading your site from London. Every byte is making a long round trip.</li><li><strong>Render-blocking CSS.</strong> Stylesheets that tell the browser to wait before showing anything.</li><li><strong>Excessive third-party scripts.</strong> Analytics, chat widgets, tag managers — each one adds weight.</li></ul><hr class="divider"><h2>The numbers that matter</h2><p>Run a free audit to get yours:</p><ul><li><strong>Load time:</strong> under 1.5 seconds is good. Over 3 seconds is actively costing you money.</li><li><strong>Page size:</strong> under 1MB per page is healthy. Over 3MB is too heavy for most connections.</li><li><strong>TTFB (Time to First Byte):</strong> under 800ms is acceptable. Over 1.5s means your server is struggling.</li><li><strong>Total Blocking Time:</strong> under 200ms. Over 500ms and the page will feel sluggish even if it eventually loads.</li></ul><div class="callout"><div class="callout-title">⚡ One fix that accounts for 40–60% of speed improvements</div>Compress your images before uploading. Use TinyPNG or Squoosh.app. It takes 30 seconds. The performance gain is enormous.</div><hr class="divider"><p>Run your free website audit now. See what your actual numbers are. Then decide what to fix first — starting with whatever costs you the most conversions.</p>"""},
    "building-web-family": {
        "title": "How to Build a Web Family That Compounds Over Time",
        "category": "Strategy",
        "date": "August 2026",
        "excerpt": "SEO, backlinks, blog content, and community — not as separate tactics, but as one interconnected system. Here's the framework for building it right.",
        "content": """<p>Most people build a website, open a Twitter account, maybe write a blog post, and call it a web presence.</p><p>Then they wonder why nothing much happens.</p><p>The problem isn't effort. It's architecture. When your website, your content, your social presence, and your backlinks operate as separate, disconnected things — they each do a little bit, but none of them do anything powerful.</p><p>A real web family works completely differently. Each piece makes the other pieces stronger. They form a loop. And over time, that loop compounds.</p><hr class="divider"><h2>The four pillars</h2><p><strong>Blog — The Voice</strong></p><p>Your blog is where you prove you actually know something. Not by saying "we're experts" — by writing posts that make readers genuinely smarter or more capable after reading them. Every post should answer a question someone actually has, better than anything else on the internet. That's the standard. Meet it or don't publish.</p><p><strong>Community — The Presence</strong></p><p>Your people are somewhere online already. Your job is to show up there — on Reddit, Twitter, LinkedIn, Hacker News — and be genuinely useful before you ever mention your own product. Help first. Pitch second. Every time. The people who remember you helped them will remember you when they need what you sell.</p><p><strong>Backlinks — The Signal</strong></p><p>When a credible website links to yours, that's a third party vouching for you. Search engines treat this as a signal of authority. Real humans treat it as a reason to click. Good backlinks come from being genuinely worth linking to: original research, genuinely useful guides, podcast appearances, guest posts on sites your audience already reads. Buying links or using link farms is short-term greedy that gets you long-term punished.</p><p><strong>SEO — The Discovery Layer</strong></p><p>Technical SEO — site speed, mobile-friendliness, SSL, clean structure — is the foundation that determines whether any of the above is findable. Content SEO is what makes you rank. The two work together. Neglect the technical layer and your brilliant blog posts will never be discovered. Nail the technical layer without the content and you'll rank for nothing worth ranking for.</p><hr class="divider"><h2>How they connect</h2><p>A genuinely useful blog post earns links naturally — because people cite useful things. Those links improve your search rankings. Better rankings bring more readers. Some of those readers join your community. Community members share your content, creating more linking opportunities. More links, better rankings, more readers, bigger community.</p><p>Round and round. The system feeds itself.</p><hr class="divider"><h2>The principle underneath all of it</h2><p>Build for real humans first. Write content you'd actually read. Show up in communities because you genuinely have something to contribute. Make something worth linking to. The backlinks and rankings are the output of doing those things right — not the goal you're chasing directly.</p><p>Start with one pillar that's genuinely excellent. Everything else grows from there.</p><p>Run your free website audit to understand where you stand technically. Then pick the pillar that excites you most and start building.</p>"""},
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
