import os, re, time, threading, smtplib, json
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, redirect
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

app = Flask(__name__)

# ── Config (set in Railway environment variables) ──────────────────────────────
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")       # your Gmail
EMAIL_PASS    = os.environ.get("EMAIL_PASS", "")       # Gmail App Password
EMAIL_TO      = os.environ.get("EMAIL_TO", "")         # where to receive alerts
BEARER_TOKEN  = os.environ.get("X_BEARER_TOKEN", "")  # optional: X/Twitter API

# ── Julian's channel IDs & handles ────────────────────────────────────────────
JULIAN_YT_ID   = "UCWhMvstLR9kBcpR6Q2q4v4A"           # main channel @julianjune
JULIAN_YT_ID2  = "UClFnvzK4aRwqY3oWsPYRWkw"           # extended / shorts
JULIAN_X       = "julianpetroulas"
JULIAN_YT_RSS  = [
    f"https://www.youtube.com/feeds/videos.xml?channel_id={JULIAN_YT_ID}",
    f"https://www.youtube.com/feeds/videos.xml?channel_id={JULIAN_YT_ID2}",
]

# ── Known stock tickers to scan for in text ───────────────────────────────────
KNOWN_TICKERS = set([
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","TSLA","NVDA","AMD","INTC",
    "PLTR","RKLB","FLY","ASTS","LUNR","KRMN","ACHR","JOBY","SPCE",
    "COIN","HOOD","SOFI","SQ","PYPL","AFRM",
    "CRWD","NET","PANW","ZS","OKTA",
    "SHOP","TTD","DDOG","SNOW","MDB","CFLT",
    "UBER","LYFT","ABNB","DASH",
    "NIO","XPEV","LI","RIVN","LCID",
    "BABA","JD","PDD","BIDU",
    "SPY","QQQ","IWM","ARKK","SOXL",
])

# ── In-memory store ────────────────────────────────────────────────────────────
feed_items     = []   # raw items from YouTube/X
scored_stocks  = []   # stocks Julian mentioned + scored
email_log      = []   # sent emails
seen_ids       = set()
monitor_status = {"last_check": None, "running": False, "next_check": None}


# ══════════════════════════════════════════════════════════════════════════════
# STOCK TICKER EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════
def extract_tickers(text: str) -> list:
    """Pull stock tickers from any text — handles $NVDA, NVDA, 'Nvidia stock' etc."""
    found = set()

    # 1. Explicit $TICKER format
    dollar_tickers = re.findall(r'\$([A-Z]{1,5})\b', text.upper())
    for t in dollar_tickers:
        if t in KNOWN_TICKERS:
            found.add(t)

    # 2. Standalone uppercase 2-5 letter words that are known tickers
    words = re.findall(r'\b([A-Z]{2,5})\b', text.upper())
    for w in words:
        if w in KNOWN_TICKERS:
            found.add(w)

    # 3. Company name → ticker mapping
    name_map = {
        "NVIDIA": "NVDA", "APPLE": "AAPL", "MICROSOFT": "MSFT",
        "AMAZON": "AMZN", "GOOGLE": "GOOGL", "META": "META",
        "TESLA": "TSLA", "PALANTIR": "PLTR", "ROCKETLAB": "RKLB",
        "ROCKET LAB": "RKLB", "FIREFLY": "FLY", "COINBASE": "COIN",
        "ROBINHOOD": "HOOD", "SOFI": "SOFI", "SPACEX": "SPCE",
        "ARCHER": "ACHR", "JOBY": "JOBY", "AST SPACEMOBILE": "ASTS",
        "INTUITIVE MACHINES": "LUNR", "CROWDSTRIKE": "CRWD",
        "CLOUDFLARE": "NET", "SHOPIFY": "SHOP", "SNOWFLAKE": "SNOW",
        "DATADOG": "DDOG", "UBER": "UBER", "AIRBNB": "ABNB",
    }
    text_upper = text.upper()
    for name, ticker in name_map.items():
        if name in text_upper:
            found.add(ticker)

    return list(found)


# ══════════════════════════════════════════════════════════════════════════════
# JULIAN'S 5-QUESTION SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════
def julian_score(ticker: str) -> dict:
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info
        name           = info.get("longName", ticker)
        sector         = info.get("sector", "Unknown")
        revenue_gr     = info.get("revenueGrowth", 0) or 0
        gross_margin   = info.get("grossMargins", 0) or 0
        market_cap     = info.get("marketCap", 0) or 0
        current_price  = info.get("currentPrice") or info.get("regularMarketPrice", 0) or 0
        analyst_target = info.get("targetMeanPrice", 0) or 0
        rec_mean       = info.get("recommendationMean", 3) or 3
        week52_low     = info.get("fiftyTwoWeekLow", 0) or 0
        week52_high    = info.get("fiftyTwoWeekHigh", 1) or 1
        total_cash     = info.get("totalCash", 0) or 0
        total_debt     = info.get("totalDebt", 0) or 0
        beta           = info.get("beta", 1) or 1
        earnings_ts    = info.get("earningsTimestamp", None)

        mc_b = market_cap / 1e9
        price_pos = (current_price - week52_low) / (week52_high - week52_low) if (week52_high - week52_low) > 0 else 0.5
        upside = ((analyst_target - current_price) / current_price * 100) if current_price > 0 else 0

        earnings_days, has_earnings = None, False
        if earnings_ts:
            ed = datetime.fromtimestamp(earnings_ts)
            earnings_days = (ed - datetime.now()).days
            has_earnings = 0 < earnings_days <= 30

        # Q1 — Long-term story
        q1, q1r = 0, []
        if revenue_gr > 0.5:   q1 += 3; q1r.append(f"Revenue up {revenue_gr*100:.0f}% YoY")
        elif revenue_gr > 0.2: q1 += 2; q1r.append(f"Revenue up {revenue_gr*100:.0f}% YoY")
        elif revenue_gr > 0:   q1 += 1; q1r.append(f"Modest revenue growth")
        if gross_margin > 0.4: q1 += 2; q1r.append(f"Strong margins {gross_margin*100:.0f}%")
        elif gross_margin>0.2: q1 += 1; q1r.append(f"Margins {gross_margin*100:.0f}%")
        if sector in ["Technology","Healthcare","Industrials","Consumer Cyclical","Communication Services"]:
            q1 += 1; q1r.append(f"{sector} has long-term tailwinds")

        # Q2 — Clear catalyst
        q2, q2r = 0, []
        if has_earnings: q2 += 4; q2r.append(f"Earnings in {earnings_days} days ⚡")
        if price_pos < 0.35: q2 += 2; q2r.append("Near 52-week low — post-dip setup")
        if upside > 30: q2 += 2; q2r.append(f"Analysts see +{upside:.0f}% upside")

        # Q3 — Macro / market risk
        q3, q3r = 0, []
        if mc_b < 20:    q3 += 3; q3r.append(f"Small-cap ${mc_b:.1f}B — contained risk")
        elif mc_b < 100: q3 += 2; q3r.append(f"Mid-cap ${mc_b:.1f}B — manageable")
        else:            q3 += 0; q3r.append(f"Large-cap ${mc_b:.1f}B — market mover risk")
        if total_cash > 0 and total_debt < total_cash * 2:
            q3 += 2; q3r.append("Healthy balance sheet")

        # Q4 — Research + gut aligned
        q4, q4r = 0, []
        if rec_mean <= 2.0:   q4 += 3; q4r.append("Strong analyst buy consensus")
        elif rec_mean <= 2.5: q4 += 2; q4r.append("Analyst buy consensus")
        if price_pos < 0.4 and upside > 20:
            q4 += 3; q4r.append("Price dipped while analysts still bullish")
        elif upside > 15: q4 += 1; q4r.append(f"+{upside:.0f}% analyst target")

        # Q5 — Upside vs downside
        q5, q5r = 0, []
        if upside > 50:   q5 += 3; q5r.append(f"+{upside:.0f}% analyst upside")
        elif upside > 25: q5 += 2; q5r.append(f"+{upside:.0f}% analyst upside")
        if beta < 2.5: q5 += 1; q5r.append(f"Beta {beta:.1f}")
        if price_pos < 0.3: q5 += 2; q5r.append("Near 52-week low — floor close")

        passed = sum([q1>=4, q2>=3, q3>=3, q4>=3, q5>=3])
        signal = "STRONG BUY" if passed>=4 else "WATCHLIST" if passed==3 else "WEAK" if passed==2 else "SKIP"

        return {
            "ticker": ticker, "name": name, "price": round(current_price,2),
            "market_cap_b": round(mc_b,1), "analyst_upside": round(upside,1),
            "signal": signal, "passed": passed,
            "has_earnings": has_earnings, "earnings_days": earnings_days,
            "sector": sector,
            "q1":{"pass":q1>=4,"score":q1,"reasons":q1r},
            "q2":{"pass":q2>=3,"score":q2,"reasons":q2r},
            "q3":{"pass":q3>=3,"score":q3,"reasons":q3r},
            "q4":{"pass":q4>=3,"score":q4,"reasons":q4r},
            "q5":{"pass":q5>=3,"score":q5,"reasons":q5r},
            "timestamp": datetime.now().isoformat(), "error": None
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e), "signal": "ERROR",
                "passed": 0, "name": ticker, "price": 0,
                "market_cap_b": 0, "analyst_upside": 0,
                "timestamp": datetime.now().isoformat()}


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def send_email(subject: str, html_body: str) -> bool:
    if not all([EMAIL_FROM, EMAIL_PASS, EMAIL_TO]):
        print("Email not configured")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


def build_email_html(result: dict, source_item: dict) -> str:
    qs = [
        ("Long-term story", result["q1"]),
        ("Clear catalyst",  result["q2"]),
        ("Macro risk",      result["q3"]),
        ("Research + gut",  result["q4"]),
        ("Upside/downside", result["q5"]),
    ]
    q_rows = ""
    for label, q in qs:
        icon  = "✅" if q["pass"] else "❌"
        reason = q["reasons"][0] if q["reasons"] else "—"
        q_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;">
            {icon} <strong>{label}</strong>
          </td>
          <td style="padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#aaa;">
            {reason}
          </td>
        </tr>"""

    earnings_row = ""
    if result.get("has_earnings"):
        earnings_row = f'<span style="background:#f59e0b22;color:#f59e0b;padding:3px 10px;border-radius:20px;font-size:12px;margin-left:8px;">⚡ Earnings in {result["earnings_days"]}d</span>'

    signal_color = "#22c55e" if result["signal"]=="STRONG BUY" else "#f59e0b"

    return f"""
<!DOCTYPE html>
<html>
<body style="background:#0f0f0f;color:#f2f2f2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px;max-width:560px;margin:0 auto;">
  <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:14px;overflow:hidden;">
    <div style="background:#111;padding:20px 24px;border-bottom:1px solid #2a2a2a;">
      <div style="font-size:12px;color:#666;margin-bottom:6px;">JULIAN PETROULAS ALERT SYSTEM</div>
      <div style="font-size:26px;font-weight:700;letter-spacing:-0.5px;">{result['ticker']}</div>
      <div style="color:#888;font-size:13px;margin-top:2px;">{result['name']}</div>
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px;">
        <span style="background:{signal_color}22;color:{signal_color};font-size:12px;font-weight:700;padding:4px 12px;border-radius:20px;">{result['signal']}</span>
        {earnings_row}
      </div>
    </div>
    <div style="padding:20px 24px;display:flex;gap:24px;border-bottom:1px solid #2a2a2a;">
      <div><div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;">Price</div><div style="font-size:20px;font-weight:600;">${result['price']}</div></div>
      <div><div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;">Mkt Cap</div><div style="font-size:20px;font-weight:600;">${result['market_cap_b']}B</div></div>
      <div><div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;">Analyst Upside</div><div style="font-size:20px;font-weight:600;color:#22c55e;">+{result['analyst_upside']}%</div></div>
      <div><div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;">Score</div><div style="font-size:20px;font-weight:600;">{result['passed']}/5</div></div>
    </div>
    <div style="padding:16px 24px;border-bottom:1px solid #2a2a2a;">
      <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px;">Julian's 5-Question Framework</div>
      <table style="width:100%;border-collapse:collapse;">
        {q_rows}
      </table>
    </div>
    <div style="padding:16px 24px;border-bottom:1px solid #2a2a2a;background:#111;">
      <div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;">Source — Julian mentioned this</div>
      <div style="font-size:13px;color:#ccc;"><strong>{source_item.get('source','YouTube')}</strong> · {source_item.get('title','')[:80]}</div>
      <div style="margin-top:6px;"><a href="{source_item.get('url','#')}" style="color:#3b82f6;font-size:12px;">View original post →</a></div>
    </div>
    <div style="padding:16px 24px;font-size:11px;color:#555;line-height:1.6;">
      Not financial advice. This is Julian Petroulas's framework applied automatically. Always do your own research before investing.
    </div>
  </div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# FEED MONITORS
# ══════════════════════════════════════════════════════════════════════════════
def fetch_youtube_rss() -> list:
    """Fetch Julian's YouTube RSS feeds and return new video items."""
    items = []
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "yt":   "http://www.youtube.com/xml/schemas/2015",
          "media":"http://search.yahoo.com/mrss/"}
    for rss_url in JULIAN_YT_RSS:
        try:
            r = requests.get(rss_url, timeout=10)
            root = ET.fromstring(r.content)
            for entry in root.findall("atom:entry", ns):
                vid_id = entry.findtext("yt:videoId", namespaces=ns) or ""
                title  = entry.findtext("atom:title", namespaces=ns) or ""
                url    = f"https://www.youtube.com/watch?v={vid_id}"
                published = entry.findtext("atom:published", namespaces=ns) or ""
                desc   = ""
                media_group = entry.find("media:group", ns)
                if media_group is not None:
                    desc = media_group.findtext("media:description", namespaces=ns) or ""
                if vid_id and vid_id not in seen_ids:
                    items.append({
                        "id": vid_id, "source": "YouTube",
                        "title": title, "url": url,
                        "text": f"{title} {desc}",
                        "published": published,
                        "timestamp": datetime.now().isoformat()
                    })
        except Exception as e:
            print(f"YouTube RSS error: {e}")
    return items


def fetch_x_posts() -> list:
    """Fetch Julian's recent X/Twitter posts via API (optional)."""
    if not BEARER_TOKEN:
        return []
    items = []
    try:
        # Get user ID first
        r = requests.get(
            f"https://api.twitter.com/2/users/by/username/{JULIAN_X}",
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            timeout=10
        )
        if r.status_code != 200:
            return []
        user_id = r.json()["data"]["id"]

        # Get recent tweets
        r2 = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            params={"max_results": 10, "tweet.fields": "created_at,text"},
            timeout=10
        )
        if r2.status_code != 200:
            return []
        for tweet in r2.json().get("data", []):
            tid = tweet["id"]
            if tid not in seen_ids:
                items.append({
                    "id": tid, "source": "X (Twitter)",
                    "title": tweet["text"][:80],
                    "url": f"https://x.com/{JULIAN_X}/status/{tid}",
                    "text": tweet["text"],
                    "published": tweet.get("created_at", ""),
                    "timestamp": datetime.now().isoformat()
                })
    except Exception as e:
        print(f"X API error: {e}")
    return items


def fetch_x_nitter() -> list:
    """Fallback: scrape X via Nitter public instance (no API key needed)."""
    items = []
    nitter_instances = [
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
    ]
    for base in nitter_instances:
        try:
            r = requests.get(
                f"{base}/{JULIAN_X}/rss",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            channel = root.find("channel")
            if channel is None:
                continue
            for item in channel.findall("item")[:10]:
                title = item.findtext("title") or ""
                link  = item.findtext("link") or ""
                desc  = item.findtext("description") or ""
                guid  = item.findtext("guid") or link
                clean_id = re.sub(r'[^a-zA-Z0-9]', '', guid)[-20:]
                if clean_id and clean_id not in seen_ids:
                    items.append({
                        "id": clean_id, "source": "X (Twitter)",
                        "title": title[:80],
                        "url": link,
                        "text": f"{title} {desc}",
                        "published": item.findtext("pubDate") or "",
                        "timestamp": datetime.now().isoformat()
                    })
            break  # got results from this instance
        except Exception as e:
            print(f"Nitter error {base}: {e}")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR LOOP
# ══════════════════════════════════════════════════════════════════════════════
def run_monitor():
    global monitor_status, feed_items, scored_stocks, seen_ids

    if monitor_status["running"]:
        return
    monitor_status["running"] = True
    monitor_status["last_check"] = datetime.now().isoformat()

    # 1. Fetch new content from Julian's channels
    new_items = []
    new_items += fetch_youtube_rss()
    if BEARER_TOKEN:
        new_items += fetch_x_posts()
    else:
        new_items += fetch_x_nitter()

    # 2. For each new item, extract tickers Julian mentioned
    for item in new_items:
        seen_ids.add(item["id"])
        tickers = extract_tickers(item["text"])
        item["tickers_found"] = tickers
        feed_items.insert(0, item)

        # 3. Score each ticker through Julian's framework
        for ticker in tickers:
            # Don't re-score same ticker within 6 hours
            recent = any(
                s["ticker"] == ticker and
                (datetime.now() - datetime.fromisoformat(s["timestamp"])).total_seconds() < 21600
                for s in scored_stocks
            )
            if recent:
                continue

            result = julian_score(ticker)
            result["source_item"] = item
            scored_stocks.insert(0, result)

            # 4. Email if passes Julian's checklist
            if result["signal"] in ["STRONG BUY", "WATCHLIST"] and not result.get("error"):
                already_emailed = any(
                    e["ticker"] == ticker and
                    (datetime.now() - datetime.fromisoformat(e["timestamp"])).total_seconds() < 86400
                    for e in email_log
                )
                if not already_emailed:
                    subject = f"🚀 Julian Alert: {ticker} — {result['signal']} ({result['passed']}/5)"
                    html    = build_email_html(result, item)
                    sent    = send_email(subject, html)
                    email_log.insert(0, {
                        "ticker": ticker,
                        "signal": result["signal"],
                        "subject": subject,
                        "timestamp": datetime.now().isoformat(),
                        "sent": sent,
                        "source": item["source"],
                        "source_title": item["title"]
                    })
        time.sleep(0.5)

    # Keep lists manageable
    feed_items[:] = feed_items[:100]
    scored_stocks[:] = scored_stocks[:200]

    monitor_status["running"] = False
    monitor_status["next_check"] = (datetime.now() + timedelta(hours=1)).isoformat()


def schedule_monitor():
    while True:
        try:
            run_monitor()
        except Exception as e:
            print(f"Monitor error: {e}")
            monitor_status["running"] = False
        time.sleep(3600)  # check every hour


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    strong_buys = [s for s in scored_stocks if s["signal"] == "STRONG BUY"]
    watchlist   = [s for s in scored_stocks if s["signal"] == "WATCHLIST"]
    emails_sent = sum(1 for e in email_log if e["sent"])
    configured  = bool(EMAIL_FROM and EMAIL_PASS and EMAIL_TO)
    return render_template("index.html",
        feed_items=feed_items[:20],
        scored_stocks=scored_stocks[:30],
        strong_buys=strong_buys[:10],
        watchlist_stocks=watchlist[:10],
        email_log=email_log[:20],
        monitor_status=monitor_status,
        emails_sent=emails_sent,
        configured=configured,
        has_x=bool(BEARER_TOKEN)
    )

@app.route("/run", methods=["POST"])
def run_now():
    t = threading.Thread(target=run_monitor, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/test_email", methods=["POST"])
def test_email():
    sent = send_email(
        "✅ Julian Alert System — Test Email",
        "<h2 style='color:#22c55e;font-family:sans-serif;'>Julian Alert System is connected!</h2><p style='font-family:sans-serif;color:#333;'>You'll receive an email whenever Julian posts about a stock that passes his 5-question framework.</p>"
    )
    return jsonify({"sent": sent})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": monitor_status["running"],
        "last_check": monitor_status["last_check"],
        "next_check": monitor_status["next_check"],
        "feed_count": len(feed_items),
        "scored_count": len(scored_stocks),
        "emails_sent": sum(1 for e in email_log if e["sent"])
    })

@app.route("/api/feed")
def api_feed():
    return jsonify(feed_items[:20])

if __name__ == "__main__":
    t = threading.Thread(target=schedule_monitor, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
