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
