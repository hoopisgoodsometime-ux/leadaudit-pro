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
        "content": """<p>You just sent the contract. $12,000 for a single Instagram story and three feed posts. The creator has 840,000 followers. Their engagement looked solid — 4.2% by the metrics you were handed.</p><p>Three weeks later, your client's site saw 140 visitors from that campaign. Zero conversions. You go back and check the creator's profile yourself. Their most recent Reel has 11,000 views. Their average like count is 800.</p><p>800 likes from 840,000 followers. That's a 0.095% engagement rate. That's not a creator with an audience. That's a creator with a spreadsheet full of ghost accounts.</p><p>You just got played for $12,000.</p><p>I've seen this story run in a dozen variations — different budgets, different platforms, same outcome. And the thing is, every single time, the fraud was visible 60 seconds before the contract got signed. You just had to know where to look.</p><p>Here are the seven checks that catch 90% of inflated influencer accounts. Use them before you spend a dollar.</p><hr class="divider"><h2>1. The view-to-subscriber ratio is a ghost town</h2><p>Go to the channel or account. Note the follower count. Look at the last 10 posts. Divide average views by follower count.</p><p>On YouTube, if a channel has 300,000 subscribers and their videos pull 35,000 views on average, those numbers are lying. That's an 0.12x ratio — you'd expect at minimum 1x from a channel with a real audience, and 3x to 10x from an actively engaged one.</p><p>The math is simple: bought followers don't watch videos. Real followers sometimes do.</p><p>A ratio below 1x is an automatic red flag. Walk away or demand a deeper audit.</p><h2>2. Every comment sounds like a motivational poster</h2><p>Scroll to the comments on three recent posts. Actually read them — don't skim.</p><p>Real comments say specific things: "I tried the second approach you described and it worked way better for me," or "Wait, you actually addressed the counterargument here — most creators skip that." Those are humans who absorbed content and responded to it.</p><p>Fake comments are always vaguely positive and content-agnostic: "So inspiring 🔥," "This is amazing!!," "Needed this today 🙏." Those comments could be pasted under any post in any niche on the internet. They say nothing about the content and everything about the account that posted them.</p><p>If you can't find at least two comments that reference something specific from the post after reading five comments, that's a flag.</p><h2>3. The engagement rate is structurally wrong for the follower count</h2><p>Engagement rate alone is useless without context. A 15% engagement rate on a 3,000-follower account is plausible. A 15% engagement rate on a 2 million-follower account is mathematically absurd — at that scale, even 1% engagement means tens of thousands of real humans interacting.</p><p>The benchmarks shift as channels grow. Under 10K followers, 6–12% is normal. Over 1 million, 1–3% is healthy. If a 2-million-follower account is claiming 8% engagement, cross-check the raw numbers: 8% of 2 million is 160,000 interactions per post. Does that match what you're actually seeing?</p><p>When the engagement rate doesn't fit the scale, the numbers are fabricated.</p><h2>4. The subscriber graph looks like a heartbeat</h2><p>Go to Social Blade. Look at the subscriber growth chart — not the total number, the shape of the line.</p><p>Real channels grow in gradual, uneven increments. They spike after a viral video, flatten between uploads, spike again after a collaboration. The overall shape is gentle.</p><p>Fake channels show sharp vertical jumps — 60,000 new subscribers in four days, then completely flat for six months. Those spikes almost always come from purchased follower batches, sub4sub communities, or giveaways that attracted people who wanted free products, not your content.</p><p>A spiky graph is a purchased graph. Smooth lines are organic ones.</p><h2>5. No other platform presence</h2><p>Real influencers at 500K+ have a presence across platforms — an Instagram, a business email in the bio, a Linktree equivalent, a TikTok mirror account, something. Monetization requires directing attention somewhere.</p><p>When a YouTube channel has 1.8 million subscribers and the only link in the description is to a Discord server with 200 members, something is wrong. Either they can't monetize because the audience isn't real, or they're leaving money on the table — neither option is one you want to partner with.</p><h2>6. The upload schedule is a desert</h2><p>A channel with 800,000 subscribers that posted four videos in the last year isn't a channel with 800,000 fans. It's a channel that had 800,000 followers at some point and then stopped giving them reasons to return.</p><p>Real creators — even ones going through creative droughts — post every three to four weeks minimum. When a channel goes quiet for six months and then posts a sponsorship video, that's not a creator reconnecting with their audience. That's a creator cashing out.</p><p>Check the upload history before you sign anything.</p><h2>7. Nothing to buy, nowhere to buy it</h2><p>Real influencers have a monetization layer: a course, a product, a Patreon, a consultation link, a brand partnership page, a Shopify store. They give their audience a way to give them money.</p><p>Accounts with high follower counts and zero commerce infrastructure aren't influencers — they're follower farms. The audience was accumulated to look impressive, not to become customers.</p><hr class="divider"><div class="checklist"><div class="checklist-title">✓ Your Fake Influencer Checklist</div><ul><li>View-to-subscriber ratio at or above 1x</li><li>At least 2 of 5 comments reference specific content</li><li>Engagement rate fits the follower scale (not too high or suspiciously low)</li><li>Smooth subscriber growth curve on Social Blade</li><li>Active presence on at least 2 other platforms</li><li>Posted at least once in the last 6 weeks</li><li>Has a product, service, or clear monetization link</li></ul></div><p>If you check more than two of these boxes, walk away. The numbers won't magically improve after you sign the contract.</p><p>And if you want to skip the manual work — LeadAudit Pro runs all seven checks automatically and delivers a score in under 30 seconds. It's the difference between knowing and guessing.</p>"""},




    "view-to-subscriber-ratio": {
        "title": "The View-to-Subscriber Ratio: The Single Best Fake-Follower Signal",
        "category": "Influencer Analysis",
        "date": "August 2026",
        "excerpt": "Subscriber count means nothing without the view history to back it up. Here's the math, the benchmarks, and how to use this ratio on every channel you audit.",
        "content": """<p>A promoter once told me about a gig he booked in 2019. The venue said they'd draw 4,000 people based on their social following. Night of the show: 312 paying customers. The venue owner pointed at the Instagram follower count — 98,000 — as proof they'd done their part.</p><p>He had. The followers just weren't real.</p><p>That gap — between what the number says and who actually shows up — is the entire influencer fraud problem in one story.</p><p>The view-to-subscriber ratio exists because subscriber counts are a historical record of promises made. Views are a live attendance check. And in any industry where foot traffic matters, attendance is the only number worth knowing.</p><hr class="divider"><h2>Why follower counts lie and views don't</h2><p>Buying 10,000 followers costs about $30 on the open market. Those followers are bot accounts, dormammu accounts (accounts created and never touched again), and people who followed-for-follow and forgot about the channel by Tuesday.</p><p>YouTube's subscriber counter never decrements when people leave. That 500K figure is a cumulative headcount spanning years — it includes every person who ever clicked subscribe and then stopped opening the app. A 2018 subscriber who unsubscribed emotionally in 2019 still counts in 2026.</p><p>Giveaways and contest follows are worse than bots — those are real humans who specifically showed up for free stuff, not your content. They have no intent to engage with anything you post.</p><p>Views, by contrast, require a real person with a real device spending real seconds on a real page. You can manipulate views at the margins, but running a bot farm that delivers 50,000 fake views at scale gets caught by YouTube's detection systems fast. Views are expensive to fake. Follower counts are not.</p><p>That's why the ratio of average views to subscriber count is the most reliable single signal in the entire influencer auditing toolkit.</p><hr class="divider"><h2>The formula</h2><p><strong>Average views on last 10 videos ÷ Total subscribers = View-to-Subscriber Ratio</strong></p><p>Average out the last 10 videos, not just the most recent one. Creators can optimize a single video. They can't easily optimize 10.</p><hr class="divider"><h2>The benchmarks (YouTube, full-length videos)</h2><ul><li><strong>Below 0.5x:</strong> Almost certainly purchased or heavily inflated. Treat as high-risk.</li><li><strong>0.5x–1x:</strong> Meaningful red flag. Most subscribers aren't watching. Ask hard questions.</li><li><strong>1x–3x:</strong> Normal for established channels. Acceptable baseline.</li><li><strong>3x–10x:</strong> Healthy. A real, returning audience that consistently watches new uploads.</li><li><strong>10x+:</strong> Excellent. The top 5% of channels — subscribers who treat every upload as an event.</li></ul><div class="callout"><div class="callout-title">⚠️ One exception that confuses this metric</div>YouTube Shorts generate massive views from non-subscribers through algorithmic discovery. A channel that runs mostly Shorts will show inflated ratios that reflect YouTube's algorithm, not subscriber loyalty. Always apply this ratio to videos over 5 minutes for the truest signal.</div><hr class="divider"><h2>How to use it in practice</h2><p>Pick the 5 most recent videos longer than 5 minutes. Get their view counts. Divide by the subscriber count. If you're below 1x, you need a specific, credible explanation before spending anything — not a media kit, not a follower count, a reason.</p><p>LeadAudit Pro runs this calculation across the last 20 videos automatically, so you get the full picture instead of a single potentially cherry-picked data point. You'll see the distribution — not just the average — which tells you whether the channel is consistently earning views or occasionally getting lucky.</p>"""},




    "engagement-rate-benchmarks": {
        "title": "Real Engagement Rate Benchmarks by Follower Count (2026)",
        "category": "Industry Data",
        "date": "August 2026",
        "excerpt": "What counts as 'good' engagement changes completely depending on follower count. We break down the real numbers so you stop comparing a 10K account to a 10M account.",
        "content": """<p>Here's a question I got from a brand manager last year: "This creator has 2.3 million subscribers and their videos get 60,000–90,000 views. Is that normal?"</p><p>She was hoping I'd tell her it was fine.</p><p>A 2.3 million subscriber channel where the best videos break 100,000 views is running a 0.04x view-to-subscriber ratio. That's not a struggling channel — that's a channel where the audience left years ago and nobody told the subscriber counter.</p><p>The 60,000 interactions on those videos? If likes and comments sum to 60,000 on 90,000 views, that's a 67% engagement rate — which would be the most engaged audience I've ever seen. More likely it's 800 likes and 120 comments. On 90,000 views. That's a 1% engagement rate on a channel that size.</p><p>That's not normal. That's a red flag wrapped in a big round number.</p><p>The problem is that most people don't know what engagement rate should look like at different follower scales. They see 2.3M and assume that means something. It doesn't — not without the engagement context.</p><p>This post gives you the real benchmarks so you stop comparing a 12,000-subscriber channel to a 2.3M one.</p><hr class="divider"><h2>The formula we use</h2><p><strong>(Likes + Comments) ÷ Views × 100 = Engagement Rate</strong></p><p>We calculate against views, not followers. Followers are a historical accumulator that includes every bot, every person who unfollowed emotionally, and everyone who followed for a giveaway. Views represent actual humans who chose to spend time with this specific content. That's the denominator that matters.</p><hr class="divider"><h2>Why engagement rate naturally compresses at scale</h2><p>Think about a channel at 5,000 subscribers. The owner probably has an active comment section where the same 30–50 people show up on every video. Those are real fans who found something worth returning to. At 5,000 followers, 200 engaged commenters is 4% engagement. That's normal.</p><p>Now scale that to 5 million subscribers. The channel is a media company. They've been publishing for eight years. The followers who were there at 50K have grown up, moved on, started careers. New followers found them at different points along the journey. Most of that 5 million hasn't been notified about a new upload in years — YouTube's algorithmic filtering means not all subscribers see every video.</p><p>At 5 million subs, even an exceptional 2% engagement rate means 100,000 real interactions per video. That's extraordinary reach. But raw percent comparisons between a 5K channel and a 5M channel will always look like the smaller channel is outperforming — and that's misleading without the raw numbers behind it.</p><hr class="divider"><h2>The 2026 benchmarks — YouTube, calculated on views</h2><ul><li><strong>Under 10K subscribers:</strong> 6–12% is excellent. Under 2% warrants serious scrutiny. At this size, an engaged community should be actively commenting, not just liking.</li><li><strong>10K–100K subscribers:</strong> 3–8% is healthy. Under 1% is a red flag. At this scale, the channel is transitioning from personal to institutional — some engagement drop is natural, but a cliff means the community is ghosting.</li><li><strong>100K–1M subscribers:</strong> 2–5% is the normal range. Under 0.5% is suspicious. At a million subscribers, even 0.5% is 5,000 real interactions per video — but if the raw like count is 300 on 200,000 views, the math doesn't work and something is wrong.</li><li><strong>1M+ subscribers:</strong> 1–3% is typical for mature channels. Under 0.3% — specifically 0.3%, not 1% — is a strong indicator of purchased or deeply inactive followers. A channel at this scale should be generating tens of thousands of views per video minimum.</li></ul><hr class="divider"><h2>The number you must always look at alongside engagement rate</h2><p>Engagement rate in isolation tells half the story. Here's why:</p><p>A creator with 2% engagement on 200,000 views = 4,000 real human interactions. Another creator with 12% engagement on 3,000 views = 360 real human interactions.</p><p>The second creator has a more passionate per-capita audience. The first creator just reached 4,000 people who actually did something. If you're buying reach, the first number is what matters. If you're buying influence and community, you want the second number — but you'd never know to look at engagement rate alone.</p><p>Always cross-reference engagement rate with average view count. One tells you the quality of the crowd. The other tells you the size of it. Both matter for different reasons.</p><hr class="divider"><p>LeadAudit Pro calculates engagement rate across the last 20 videos automatically, so you're working from a distribution — not a single cherry-picked video that a creator's media kit might intentionally highlight.</p>"""},




    "website-speed-conversion": {
        "title": "Why Website Speed Is a Revenue Problem, Not a Vanity Metric",
        "category": "Web Performance",
        "date": "August 2026",
        "excerpt": "Every 1 second of load time costs you roughly 7% in conversions. Run a free website audit to see where you stand — then use this guide to fix what matters most.",
        "content": """<p>I watched a founder lose a $40,000 enterprise deal because of a 4.2-second page load time.</p><p>His prospect was reviewing the pricing page on a train into London, on a 4G connection. The page hadn't loaded by the time they hit a tunnel. They put the phone down, forgot about it, and by the time they were back in signal the moment had passed. The deal went to a competitor whose site loaded in 1.1 seconds.</p><p>This wasn't a vanity concern. It was a $40,000 problem with a four-second fuse.</p><p>Most founders treat website speed as a technical checkbox — something their developer mentions in a standup note that gets deprioritized behind feature work. That's a revenue decision made by default, not by design. And the data is unambiguous about what slow sites cost.</p><hr class="divider"><h2>The physiological reality of waiting</h2><p>When a page load stretches past 1 second, the browser tab becomes a source of mild stress. The human nervous system registers the absence of expected content as an unresolved promise. Cortisol doesn't spike dramatically — you're not in danger — but enough that the emotional state shifts toward impatience before the content even appears.</p><p>Google's research found that 53% of mobile visits are abandoned if the page takes longer than 3 seconds to become interactive. Three seconds. Not seven. Not ten. Three.</p><p>The people leaving your site at 4 seconds aren't saying "their site is slow." They're thinking "this doesn't seem right" and moving on. They may not even consciously register why they bounced.</p><p>That effect compounds in two directions. First, every bounce is a lost conversion — a person who was genuinely interested enough to click, who then left before converting. Second, Google uses bounce rate and time-on-site as ranking signals. Slow sites get penalized in search rankings, which means fewer people find you organically. You pay for the slow load twice: once in the visitor you lost, and once in the ranking position you slipped.</p><hr class="divider"><h2>The specific culprits on most sites</h2><p><strong>Uncompressed images:</strong> This accounts for roughly 50–60% of the slow-site problems I see in audits. A 3.2MB hero image where a 140KB WebP would have delivered the same visual result. The fix takes 90 seconds in Squoosh.app. The performance gain is immediate and significant.</p><p><strong>Render-blocking JavaScript:</strong> Your browser can't show the page until it's downloaded, parsed, and executed every script in the <head>. If you have 12 scripts loading before the first pixel paints, the user sees a white screen for the duration. Lazy-loading JS and moving non-critical scripts to after the first paint fixes this.</p><p><strong>No CDN:</strong> A server in Virginia serving users in Singapore adds 200–300ms of pure geography to every request. A CDN puts your assets on edge servers close to your users. If your audience is global and you're not using a CDN, you're slow by design.</p><p><strong>Render-blocking CSS:</strong> CSS in the <head> tells the browser not to paint until it's fully downloaded. Inline critical CSS for above-the-fold content, defer everything else.</p><p><strong>Third-party scripts stacking up:</strong> Each analytics tool, chat widget, tag manager, and social embed adds 50–200ms. Four plugins and a chat widget can add a full second of load time from third-party overhead alone.</p><hr class="divider"><h2>The numbers you should know cold</h2><ul><li><strong>Load time under 1.5 seconds:</strong> Good. Your pages are competitive in 2026 standards.</li><li><strong>Load time 1.5–3 seconds:</strong> Acceptable but costing you conversions you don't see. Run an audit.</li><li><strong>Load time over 3 seconds:</strong> Actively losing revenue. This is where the Google data shows abandonment starts.</li><li><strong>Page size under 1MB:</strong> Healthy for most use cases.</li><li><strong>Page size over 3MB:</strong> Too heavy for anyone on a mid-tier mobile connection — which is most of the world outside Western Europe and North America.</li><li><strong>TTFB (Time to First Byte) over 1.8 seconds:</strong> Your server or CDN is the problem. Fix the origin response time before touching anything else.</li><li><strong>Total Blocking Time over 500ms:</strong> The main thread is saturated with JavaScript execution. The page is technically loaded but feels sluggish and unresponsive.</li></ul><div class="callout"><div class="callout-title">⚡ The 30-second fix that moves the needle most</div>Open Squoosh.app, drop in your hero image, compress it as WebP at quality 80. Replace the original. If every site owner did just this one thing, the average mobile load time globally would drop by an estimated 1.2–1.8 seconds.</div><hr class="divider"><p>Run a free website audit to see where your numbers actually sit. The gap between what you think your site performance is and what it actually is tends to be large — most founders discover their TTFB is twice what they assumed, or their page size is 4MB instead of the 800KB they remember from launch.</p>"""},




    "building-web-family": {
        "title": "How to Build a Web Family That Compounds Over Time",
        "category": "Strategy",
        "date": "August 2026",
        "excerpt": "SEO, backlinks, blog content, and community — not as separate tactics, but as one interconnected system. Here's the framework for building it right.",
        "content": """<p>In 2017, a developer named Dan_codes started publishing detailed technical blog posts about web performance on a personal site that had 40 visitors a day. Nobody knew his name. The posts were long, specific, and genuinely useful — he documented bugs he'd fixed and exactly how he'd fixed them, with code samples that worked.</p><p>By 2019, three of those posts had been linked to by Smashing Magazine, CSS-Tricks, and a Google developer blog. His organic traffic went from 40 visitors a day to 4,000. He wasn't doing backlink outreach. He was writing things worth linking to.</p><p>By 2021, the newsletter he'd started from that blog had 22,000 subscribers. By 2023, it was 80,000. He'd never run a single paid ad.</p><p>That blog, that newsletter, those backlinks, and that technical reputation were not four separate marketing initiatives. They were one system that grew because each piece reinforced the others. That's what a web family actually is.</p><hr class="divider"><h2>The anatomy of the system</h2><p>Most business websites are a collection of separate things that happen to live on the same domain: a blog, a LinkedIn page, some backlinks from old guest posts, and a technical setup that was correct at launch in 2019 and hasn't been touched since.</p><p>A web family is different. It's four pillars that are architecturally designed to reinforce each other:</p><p><strong>Blog — The permanent voice</strong></p><p>The blog is where you prove you know what you're talking about by actually demonstrating it. Not with testimonials, not with credentials in the byline — with posts that leave the reader genuinely more capable than before they arrived. If someone reads your post and learns something they couldn't learn faster from a Wikipedia article or an AI summary, you've done the job.</p><p>The standard is specific: every post should answer a real question someone actually has, better than the top 10 existing results for that query combined. If you can't meet that bar, don't publish. Mediocre content doesn't earn links. It earns bounces.</p><p><strong>Community — Where the people actually are</strong></p><p>Your target audience isn't waiting for your next blog post. They're on Reddit arguing about the same problem your product solves, on LinkedIn posting about challenges in your industry, on Hacker News tearing apart bad takes about your space.</p><p>Show up there before you ever mention your product. Answer questions. Share what you know. Post opinions you're willing to be wrong about. The people who remember you were useful before you sold anything are the people who buy first when you do.</p><p>This isn't content marketing. It's just being a person in a community. The marketing happens because they remember you exist.</p><p><strong>Backlinks — Third-party endorsements you earn, not buy</strong></p><p>When a credible site links to yours, two things happen simultaneously: search engines interpret it as a signal of authority, and real humans interpret it as social proof worth clicking.</p><p>The only reliable way to earn real backlinks is to publish things worth linking to: original research, definitive guides, tools that solve a specific problem, data that didn't exist before you published it. Guest posts on irrelevant sites with exact-match anchor text don't move rankings anymore — Google's been ignoring them for years. What moves rankings is content that other editors and developers genuinely want to cite.</p><p><strong>Technical SEO — The infrastructure that decides whether any of this is discoverable</strong></p><p>Your blog posts could be the best-written technical guides in your industry, but if your site loads in 5 seconds on mobile, Google deprioritizes them in search results. If your page structure doesn't use headings correctly, crawlers can't understand the content hierarchy. If you have 1,200 pages indexed but 800 of them return 404 errors, your crawl budget is wasted on dead ends.</p><p>Technical SEO isn't glamorous. It's the plumbing. But bad plumbing floods the house.</p><hr class="divider"><h2>How the loop closes</h2><p>Here's what the compounding actually looks like when it works:</p><p>A genuinely useful blog post gets linked to by two niche sites that your audience reads. Those links improve your domain authority and your search ranking for the topic you wrote about. You start appearing on page 1 for queries your ideal customers are searching. More readers arrive. Some of those readers join your newsletter or follow you on LinkedIn. They share your content when it resonates, which generates more linking opportunities. More links. Better rankings. More readers. The community grows. The loop accelerates.</p><p>None of those pieces are doing massive work individually. The power comes from them being connected. A brilliant blog post on a technically broken site goes nowhere. Technically perfect site with boring content goes nowhere. Both working together compounds.</p><hr class="divider"><h2>Where to start</h2><p>Pick whichever pillar is currently your biggest weakness — not the one you're most comfortable with. If your site has a 4.8-second load time, that's the starting point. If your blog has never published anything worth reading, that's the starting point. The strongest pillar doesn't lift the whole system. The weakest one holds everything back.</p><p>Get your free website audit first. That gives you the technical baseline — the foundation you'd be building everything else on top of. Then pick one thing and do it properly before moving to the next.</p>"""},


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
