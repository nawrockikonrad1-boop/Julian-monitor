import os, re, time, threading, smtplib, concurrent.futures
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request
import requests
import xml.etree.ElementTree as ET
import yfinance as yf

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
EMAIL_FROM   = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS   = os.environ.get("EMAIL_PASS", "")
EMAIL_TO     = os.environ.get("EMAIL_TO", "")
BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

JULIAN_YT_RSS = [
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCWhMvstLR9kBcpR6Q2q4v4A",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UClFnvzK4aRwqY3oWsPYRWkw",
]
JULIAN_X = "julianpetroulas"

KNOWN_TICKERS = set([
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","TSLA","NVDA","AMD","INTC",
    "PLTR","RKLB","FLY","ASTS","LUNR","KRMN","ACHR","JOBY","SPCE",
    "COIN","HOOD","SOFI","SQ","PYPL","AFRM","CRWD","NET","PANW","ZS",
    "OKTA","SHOP","TTD","DDOG","SNOW","MDB","UBER","ABNB","DASH",
    "NIO","RIVN","LCID","BABA","JD","SPY","QQQ","ARKK","SOXL",
])

NAME_MAP = {
    "NVIDIA":"NVDA","APPLE":"AAPL","MICROSOFT":"MSFT","AMAZON":"AMZN",
    "GOOGLE":"GOOGL","TESLA":"TSLA","PALANTIR":"PLTR","ROCKETLAB":"RKLB",
    "ROCKET LAB":"RKLB","FIREFLY":"FLY","COINBASE":"COIN","ROBINHOOD":"HOOD",
    "ARCHER":"ACHR","JOBY":"JOBY","AST SPACEMOBILE":"ASTS",
    "INTUITIVE MACHINES":"LUNR","CROWDSTRIKE":"CRWD","CLOUDFLARE":"NET",
    "SHOPIFY":"SHOP","SNOWFLAKE":"SNOW","DATADOG":"DDOG","UBER":"UBER","AIRBNB":"ABNB",
}

# ── State ──────────────────────────────────────────────────────────────────────
feed_items     = []
scored_stocks  = []
email_log      = []
seen_ids       = set()
monitor_status = {"last_check": None, "running": False,
                  "next_check": None, "current_step": ""}


# ══════════════════════════════════════════════════════════════════════════════
# TICKER EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════
def extract_tickers(text: str) -> list:
    found = set()
    upper = text.upper()
    for t in re.findall(r'\$([A-Z]{1,5})\b', upper):
        if t in KNOWN_TICKERS: found.add(t)
    for w in re.findall(r'\b([A-Z]{2,5})\b', upper):
        if w in KNOWN_TICKERS: found.add(w)
    for name, ticker in NAME_MAP.items():
        if name in upper: found.add(ticker)
    return list(found)


# ══════════════════════════════════════════════════════════════════════════════
# SCORING — with hard 8-second timeout per ticker
# ══════════════════════════════════════════════════════════════════════════════
def _fetch_info(ticker):
    return yf.Ticker(ticker).info

def julian_score(ticker: str) -> dict:
    base = {"ticker": ticker, "name": ticker, "price": 0, "market_cap_b": 0,
            "analyst_upside": 0, "signal": "ERROR", "passed": 0,
            "timestamp": datetime.now().isoformat()}
    try:
        # Hard 8-second timeout so it never hangs
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_fetch_info, ticker)
            try:
                info = future.result(timeout=8)
            except concurrent.futures.TimeoutError:
                base["error"] = "Timeout fetching data"
                return base

        name          = info.get("longName", ticker)
        sector        = info.get("sector", "Unknown")
        revenue_gr    = info.get("revenueGrowth", 0) or 0
        gross_margin  = info.get("grossMargins", 0) or 0
        market_cap    = info.get("marketCap", 0) or 0
        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0) or 0
        analyst_target= info.get("targetMeanPrice", 0) or 0
        rec_mean      = info.get("recommendationMean", 3) or 3
        week52_low    = info.get("fiftyTwoWeekLow", 0) or 0
        week52_high   = info.get("fiftyTwoWeekHigh", 1) or 1
        total_cash    = info.get("totalCash", 0) or 0
        total_debt    = info.get("totalDebt", 0) or 0
        beta          = info.get("beta", 1) or 1
        earnings_ts   = info.get("earningsTimestamp", None)

        mc_b     = market_cap / 1e9
        price_pos= (current_price - week52_low) / (week52_high - week52_low) if (week52_high - week52_low) > 0 else 0.5
        upside   = ((analyst_target - current_price) / current_price * 100) if current_price > 0 else 0

        earnings_days, has_earnings = None, False
        if earnings_ts:
            ed = datetime.fromtimestamp(earnings_ts)
            earnings_days = (ed - datetime.now()).days
            has_earnings  = 0 < earnings_days <= 30

        # Q1
        q1,q1r = 0,[]
        if revenue_gr > 0.5:   q1+=3; q1r.append(f"Revenue up {revenue_gr*100:.0f}% YoY")
        elif revenue_gr > 0.2: q1+=2; q1r.append(f"Revenue up {revenue_gr*100:.0f}% YoY")
        elif revenue_gr > 0:   q1+=1; q1r.append("Modest revenue growth")
        if gross_margin > 0.4: q1+=2; q1r.append(f"Strong margins {gross_margin*100:.0f}%")
        elif gross_margin>0.2: q1+=1; q1r.append(f"Margins {gross_margin*100:.0f}%")
        if sector in ["Technology","Healthcare","Industrials","Consumer Cyclical","Communication Services"]:
            q1+=1; q1r.append(f"{sector} tailwinds")

        # Q2
        q2,q2r = 0,[]
        if has_earnings:    q2+=4; q2r.append(f"Earnings in {earnings_days} days ⚡")
        if price_pos < 0.35:q2+=2; q2r.append("Near 52-week low")
        if upside > 30:     q2+=2; q2r.append(f"Analysts see +{upside:.0f}% upside")

        # Q3
        q3,q3r = 0,[]
        if mc_b < 20:    q3+=3; q3r.append(f"Small-cap ${mc_b:.1f}B")
        elif mc_b < 100: q3+=2; q3r.append(f"Mid-cap ${mc_b:.1f}B")
        if total_cash > 0 and total_debt < total_cash*2:
            q3+=2; q3r.append("Healthy balance sheet")

        # Q4
        q4,q4r = 0,[]
        if rec_mean <= 2.0:   q4+=3; q4r.append("Strong buy consensus")
        elif rec_mean <= 2.5: q4+=2; q4r.append("Buy consensus")
        if price_pos < 0.4 and upside > 20:
            q4+=3; q4r.append("Price dipped, analysts still bullish")
        elif upside > 15: q4+=1; q4r.append(f"+{upside:.0f}% analyst target")

        # Q5
        q5,q5r = 0,[]
        if upside > 50:   q5+=3; q5r.append(f"+{upside:.0f}% analyst upside")
        elif upside > 25: q5+=2; q5r.append(f"+{upside:.0f}% analyst upside")
        if beta < 2.5:    q5+=1; q5r.append(f"Beta {beta:.1f}")
        if price_pos < 0.3: q5+=2; q5r.append("Near 52-week low floor")

        passed = sum([q1>=4, q2>=3, q3>=3, q4>=3, q5>=3])
        signal = "STRONG BUY" if passed>=4 else "WATCHLIST" if passed==3 else "WEAK" if passed==2 else "SKIP"

        return {
            "ticker":ticker,"name":name,"price":round(current_price,2),
            "market_cap_b":round(mc_b,1),"analyst_upside":round(upside,1),
            "signal":signal,"passed":passed,
            "has_earnings":has_earnings,"earnings_days":earnings_days,"sector":sector,
            "q1":{"pass":q1>=4,"score":q1,"reasons":q1r},
            "q2":{"pass":q2>=3,"score":q2,"reasons":q2r},
            "q3":{"pass":q3>=3,"score":q3,"reasons":q3r},
            "q4":{"pass":q4>=3,"score":q4,"reasons":q4r},
            "q5":{"pass":q5>=3,"score":q5,"reasons":q5r},
            "timestamp":datetime.now().isoformat(),"error":None
        }
    except Exception as e:
        base["error"] = str(e)[:80]
        return base


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def send_email(subject: str, html_body: str) -> bool:
    if not all([EMAIL_FROM, EMAIL_PASS, EMAIL_TO]):
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False


def build_email(result, source_item):
    labels = ["Long-term story","Clear catalyst","Macro risk","Research+gut","Upside/downside"]
    rows = ""
    for i, q in enumerate(["q1","q2","q3","q4","q5"]):
        icon   = "✅" if result[q]["pass"] else "❌"
        reason = result[q]["reasons"][0] if result[q]["reasons"] else "—"
        rows  += f"<tr><td style='padding:8px 12px;border-bottom:1px solid #2a2a2a'>{icon} <b>{labels[i]}</b></td><td style='padding:8px 12px;border-bottom:1px solid #2a2a2a;color:#aaa'>{reason}</td></tr>"

    sc = "#22c55e" if result["signal"]=="STRONG BUY" else "#f59e0b"
    earn = f'<span style="background:#f59e0b22;color:#f59e0b;padding:3px 10px;border-radius:20px;font-size:12px;margin-left:8px">⚡ Earnings in {result["earnings_days"]}d</span>' if result.get("has_earnings") else ""

    return f"""<!DOCTYPE html><html><body style="background:#0f0f0f;color:#f2f2f2;font-family:sans-serif;padding:24px;max-width:560px;margin:0 auto">
<div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:14px;overflow:hidden">
  <div style="background:#111;padding:20px 24px;border-bottom:1px solid #2a2a2a">
    <div style="font-size:11px;color:#666;margin-bottom:6px">JULIAN PETROULAS ALERT</div>
    <div style="font-size:28px;font-weight:700">{result['ticker']}</div>
    <div style="color:#888;font-size:13px;margin-top:2px">{result['name']}</div>
    <div style="margin-top:10px"><span style="background:{sc}22;color:{sc};font-size:12px;font-weight:700;padding:4px 12px;border-radius:20px">{result['signal']}</span>{earn}</div>
  </div>
  <div style="padding:20px 24px;display:flex;gap:20px;border-bottom:1px solid #2a2a2a;flex-wrap:wrap">
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Price</div><div style="font-size:20px;font-weight:600">${result['price']}</div></div>
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Mkt Cap</div><div style="font-size:20px;font-weight:600">${result['market_cap_b']}B</div></div>
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Analyst Upside</div><div style="font-size:20px;font-weight:600;color:#22c55e">+{result['analyst_upside']}%</div></div>
    <div><div style="font-size:10px;color:#666;text-transform:uppercase">Score</div><div style="font-size:20px;font-weight:600">{result['passed']}/5</div></div>
  </div>
  <div style="padding:16px 24px;border-bottom:1px solid #2a2a2a">
    <div style="font-size:11px;color:#666;text-transform:uppercase;margin-bottom:10px">Julian's 5-Question Framework</div>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
  </div>
  <div style="padding:16px 24px;background:#111;border-bottom:1px solid #2a2a2a">
    <div style="font-size:11px;color:#666;text-transform:uppercase;margin-bottom:6px">Julian mentioned this on</div>
    <div style="font-size:13px;color:#ccc"><b>{source_item.get('source','YouTube')}</b> — {source_item.get('title','')[:80]}</div>
    <a href="{source_item.get('url','#')}" style="color:#3b82f6;font-size:12px;margin-top:6px;display:inline-block">View original post →</a>
  </div>
  <div style="padding:14px 24px;font-size:11px;color:#555">Not financial advice. Julian's framework applied automatically. Always do your own research.</div>
</div></body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# FEED FETCHERS — all with timeouts
# ══════════════════════════════════════════════════════════════════════════════
def fetch_youtube() -> list:
    items = []
    ns = {"atom":"http://www.w3.org/2005/Atom",
          "yt":"http://www.youtube.com/xml/schemas/2015",
          "media":"http://search.yahoo.com/mrss/"}
    for url in JULIAN_YT_RSS:
        try:
            r = requests.get(url, timeout=8)
            root = ET.fromstring(r.content)
            for entry in root.findall("atom:entry", ns):
                vid_id = entry.findtext("yt:videoId", namespaces=ns) or ""
                title  = entry.findtext("atom:title", namespaces=ns) or ""
                desc   = ""
                mg = entry.find("media:group", ns)
                if mg is not None:
                    desc = mg.findtext("media:description", namespaces=ns) or ""
                if vid_id and vid_id not in seen_ids:
                    items.append({
                        "id": vid_id, "source": "YouTube",
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                        "text": f"{title} {desc}",
                        "timestamp": datetime.now().isoformat()
                    })
        except Exception as e:
            print(f"YouTube error: {e}")
    return items


def fetch_x_nitter() -> list:
    items = []
    for base in ["https://nitter.poast.org", "https://nitter.privacydev.net"]:
        try:
            r = requests.get(f"{base}/{JULIAN_X}/rss",
                             timeout=6, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            ch = root.find("channel")
            if ch is None: continue
            for item in ch.findall("item")[:8]:
                title = item.findtext("title") or ""
                link  = item.findtext("link") or ""
                desc  = item.findtext("description") or ""
                guid  = re.sub(r'[^a-zA-Z0-9]','', item.findtext("guid") or link)[-20:]
                if guid and guid not in seen_ids:
                    items.append({
                        "id": guid, "source": "X (Twitter)",
                        "title": title[:80], "url": link,
                        "text": f"{title} {desc}",
                        "timestamp": datetime.now().isoformat()
                    })
            break
        except Exception as e:
            print(f"Nitter error: {e}")
    return items


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR
# ══════════════════════════════════════════════════════════════════════════════
def run_monitor():
    global monitor_status
    if monitor_status["running"]:
        return
    monitor_status["running"] = True
    monitor_status["last_check"] = datetime.now().isoformat()
    monitor_status["current_step"] = "Fetching Julian's posts..."

    try:
        # 1. Fetch feeds (fast — both under 10 sec combined)
        new_items = fetch_youtube() + fetch_x_nitter()
        monitor_status["current_step"] = f"Found {len(new_items)} new posts — scanning for tickers..."

        for item in new_items:
            seen_ids.add(item["id"])
            tickers = extract_tickers(item["text"])
            item["tickers_found"] = tickers
            feed_items.insert(0, item)

            # 2. Score each ticker (max 8 sec each)
            for ticker in tickers:
                recent = any(
                    s["ticker"]==ticker and
                    (datetime.now()-datetime.fromisoformat(s["timestamp"])).total_seconds() < 21600
                    for s in scored_stocks
                )
                if recent: continue

                monitor_status["current_step"] = f"Scoring {ticker} through Julian's framework..."
                result = julian_score(ticker)
                result["source_item"] = item
                scored_stocks.insert(0, result)

                # 3. Email if passes
                if result["signal"] in ["STRONG BUY","WATCHLIST"] and not result.get("error"):
                    already = any(
                        e["ticker"]==ticker and
                        (datetime.now()-datetime.fromisoformat(e["timestamp"])).total_seconds() < 86400
                        for e in email_log
                    )
                    if not already:
                        monitor_status["current_step"] = f"Sending email alert for {ticker}..."
                        subject = f"🚀 Julian Alert: {ticker} — {result['signal']} ({result['passed']}/5)"
                        sent    = send_email(subject, build_email(result, item))
                        email_log.insert(0, {
                            "ticker": ticker, "signal": result["signal"],
                            "subject": subject,
                            "timestamp": datetime.now().isoformat(),
                            "sent": sent,
                            "source": item["source"],
                            "source_title": item["title"]
                        })

        # Trim lists
        feed_items[:] = feed_items[:100]
        scored_stocks[:] = scored_stocks[:200]

    except Exception as e:
        print(f"Monitor error: {e}")

    monitor_status["running"]      = False
    monitor_status["current_step"] = "Done"
    monitor_status["next_check"]   = (datetime.now()+timedelta(hours=1)).isoformat()


def schedule_monitor():
    while True:
        try:
            run_monitor()
        except Exception as e:
            print(f"Schedule error: {e}")
            monitor_status["running"] = False
        time.sleep(3600)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    strong_buys = [s for s in scored_stocks if s.get("signal")=="STRONG BUY"]
    watchlist   = [s for s in scored_stocks if s.get("signal")=="WATCHLIST"]
    return render_template("index.html",
        feed_items=feed_items[:20],
        scored_stocks=scored_stocks[:30],
        strong_buys=strong_buys[:10],
        watchlist_stocks=watchlist[:10],
        email_log=email_log[:20],
        monitor_status=monitor_status,
        emails_sent=sum(1 for e in email_log if e["sent"]),
        configured=bool(EMAIL_FROM and EMAIL_PASS and EMAIL_TO),
        has_x=bool(BEARER_TOKEN)
    )

@app.route("/run", methods=["POST"])
def run_now():
    t = threading.Thread(target=run_monitor, daemon=True)
    t.start()
    return jsonify({"status":"started"})

@app.route("/test_email", methods=["POST"])
def test_email():
    sent = send_email(
        "✅ Julian Alert System — Test",
        "<body style='font-family:sans-serif;padding:24px;background:#0f0f0f;color:#f2f2f2'><h2 style='color:#22c55e'>Julian Alert System is live! ✅</h2><p style='color:#aaa;margin-top:12px'>You'll get an email like this whenever Julian mentions a stock that passes his 5-question framework.</p></body>"
    )
    return jsonify({"sent": sent})

@app.route("/api/status")
def api_status():
    return jsonify({
        "running": monitor_status["running"],
        "current_step": monitor_status.get("current_step",""),
        "last_check": monitor_status["last_check"],
        "next_check": monitor_status["next_check"],
        "feed_count": len(feed_items),
        "scored_count": len(scored_stocks),
        "emails_sent": sum(1 for e in email_log if e["sent"])
    })

if __name__ == "__main__":
    threading.Thread(target=schedule_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
