"""
S.Tater Trading — Live Backend Server (v2)
===========================================
Endpoints:

  POST /alert    ← TradingView VCP alert webhook
  GET  /signals  ← stored alerts for the dashboard
  GET  /quotes   ← Nifty, Sensex, VIX, GIFT, USD/INR, gold/silver/crude (MCX est.)
  GET  /fno      ← PCR, Max Pain, Max Call/Put OI for NIFTY and BANKNIFTY
  GET  /news     ← latest headline per portfolio stock (?symbols=BEL,ONGC,...)

Host on Render.com free tier.
Built for Mr. Sudhinder Tater, June 2026.
"""

import json
import os
import time
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'signals.db')

# ── MCX calibration ───────────────────────────────────────────────────────────
# MCX rupee contracts are not on any free API. We estimate them from
# international prices (COMEX gold/silver, WTI crude) x USD/INR, then apply a
# calibration factor that captures import duty, GST, and local premium.
# Factors below were calibrated against verified MCX closes on 9 Jun 2026.
# If the estimate drifts from actual MCX over months, update these numbers:
#   new_factor = actual_MCX_price / raw_converted_price
CAL_GOLD   = 1.167   # MCX ₹/10g vs COMEX $/oz × INR
CAL_SILVER = 1.179   # MCX ₹/kg  vs COMEX $/oz × INR
CAL_CRUDE  = 0.977   # MCX ₹/bbl vs WTI $/bbl × INR
TROY_OZ_G  = 31.1035


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stock       TEXT NOT NULL,
                signal      TEXT,
                price       TEXT,
                bar_time    TEXT,
                received_at TEXT
            )
        ''')
        conn.commit()


init_db()


def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


# ── Caches (avoid hammering external sources) ─────────────────────────────────
_q_cache    = {'data': None, 'ts': 0}   # quotes  — 60 s
_fno_cache  = {'data': None, 'ts': 0}   # F&O     — 180 s
_news_cache = {}                        # news    — 600 s per symbol


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return add_cors(jsonify({
        'status': 'ST Trading server v2 running',
        'time': datetime.utcnow().isoformat() + 'Z'
    }))


# ── TradingView webhook ───────────────────────────────────────────────────────

@app.route('/alert', methods=['POST', 'OPTIONS'])
def receive_alert():
    if request.method == 'OPTIONS':
        return add_cors(jsonify({})), 200
    try:
        p = request.get_json(force=True, silent=True) or {}
        stock    = str(p.get('stock', 'UNKNOWN')).upper().replace('NSE:', '').strip()
        signal   = str(p.get('signal', 'Alert')).strip()
        price    = str(p.get('price', '')).strip()
        bar_time = str(p.get('time', '')).strip()
        received = datetime.utcnow().isoformat() + 'Z'
        with get_db() as conn:
            conn.execute(
                'INSERT INTO signals (stock, signal, price, bar_time, received_at) VALUES (?,?,?,?,?)',
                (stock, signal, price, bar_time, received))
            conn.commit()
        print(f'[ALERT] {stock} | {signal} | {price}')
        return add_cors(jsonify({'status': 'ok', 'stock': stock})), 200
    except Exception as e:
        print(f'[ALERT ERR] {e}')
        return add_cors(jsonify({'status': 'error', 'detail': str(e)})), 500


@app.route('/signals')
def get_signals():
    try:
        with get_db() as conn:
            rows = conn.execute(
                'SELECT stock, signal, price, bar_time, received_at FROM signals ORDER BY id DESC LIMIT 100'
            ).fetchall()
        return add_cors(jsonify([dict(r) for r in rows]))
    except Exception as e:
        return add_cors(jsonify({'error': str(e)})), 500


# ── Quotes: indices + GIFT + commodities ──────────────────────────────────────

def _yf_quote(sym):
    """Return (last, change_pct, prev_close) for a Yahoo symbol, or Nones."""
    try:
        import yfinance as yf
        fi = yf.Ticker(sym).fast_info
        lp = float(fi.last_price)
        pc = float(fi.previous_close)
        chg = round((lp - pc) / pc * 100, 2) if pc else None
        return round(lp, 2), chg, round(pc, 2)
    except Exception as ex:
        print(f'[YF WARN] {sym}: {ex}')
        return None, None, None


@app.route('/quotes')
def get_quotes():
    global _q_cache
    now = time.time()
    if _q_cache['data'] and (now - _q_cache['ts']) < 60:
        return add_cors(jsonify(_q_cache['data']))

    out = {}

    # Indian indices
    for key, sym in [('nifty', '^NSEI'), ('sensex', '^BSESN'), ('vix', '^INDIAVIX')]:
        v, c, p = _yf_quote(sym)
        out[key] = {'val': v, 'chg': c, 'prev': p}

    # GIFT Nifty — no free public feed exists (NSE IX data is licensed).
    # Best free proxy: Nifty futures move ≈ GIFT after 9:15; pre-market it is
    # unavailable, so we return null and the dashboard keeps its last value.
    out['gift'] = {'val': None, 'chg': None,
                   'note': 'No free GIFT feed; check nseix.com pre-market'}

    # USD/INR — Frankfurter (free, no key, reliable)
    usdinr = None
    try:
        import urllib.request
        with urllib.request.urlopen(
                'https://api.frankfurter.app/latest?from=USD&to=INR', timeout=6) as r:
            usdinr = round(json.loads(r.read())['rates']['INR'], 2)
    except Exception as ex:
        print(f'[FX WARN] {ex}')
    out['usdinr'] = {'val': usdinr}

    # International commodities (COMEX / NYMEX via Yahoo)
    gold_usd,   gold_chg,   _ = _yf_quote('GC=F')   # $ per troy oz
    silver_usd, silver_chg, _ = _yf_quote('SI=F')   # $ per troy oz
    wti_usd,    wti_chg,    _ = _yf_quote('CL=F')   # $ per barrel
    brent_usd,  _,          _ = _yf_quote('BZ=F')

    out['gold_spot']   = {'val': gold_usd,   'chg': gold_chg}
    out['silver_spot'] = {'val': silver_usd, 'chg': silver_chg}
    out['crude_wti']   = {'val': wti_usd,    'chg': wti_chg}
    out['brent']       = {'val': brent_usd}

    # MCX estimates (clearly estimates — see calibration note at top of file)
    if usdinr and gold_usd:
        out['gold_mcx'] = {
            'val': round(gold_usd * usdinr / TROY_OZ_G * 10 * CAL_GOLD),
            'chg': gold_chg, 'est': True}
    else:
        out['gold_mcx'] = {'val': None}

    if usdinr and silver_usd:
        out['silver_mcx'] = {
            'val': round(silver_usd * usdinr / TROY_OZ_G * 1000 * CAL_SILVER),
            'chg': silver_chg, 'est': True}
    else:
        out['silver_mcx'] = {'val': None}

    if usdinr and wti_usd:
        out['crude_mcx'] = {
            'val': round(wti_usd * usdinr * CAL_CRUDE),
            'chg': wti_chg, 'est': True}
    else:
        out['crude_mcx'] = {'val': None}

    out['fetched_at'] = datetime.utcnow().isoformat() + 'Z'
    _q_cache = {'data': out, 'ts': now}
    return add_cors(jsonify(out))


# ── F&O: PCR, Max Pain, Max OI from NSE option chain ─────────────────────────

def _nse_session():
    """NSE blocks plain requests; warm up a session with browser headers."""
    import requests
    s = requests.Session()
    s.headers.update({
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0 Safari/537.36'),
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://www.nseindia.com/option-chain',
    })
    # First hit the homepage to receive cookies
    s.get('https://www.nseindia.com', timeout=10)
    return s


def _analyse_chain(payload):
    """Compute PCR, max pain, max call/put OI strikes for the nearest expiry."""
    records = payload.get('records', {})
    expiries = records.get('expiryDates') or []
    rows = records.get('data') or []
    if not expiries or not rows:
        return {'error': 'empty chain'}

    expiry = expiries[0]                       # nearest expiry
    strikes, ce_oi, pe_oi = [], {}, {}
    for row in rows:
        if row.get('expiryDate') != expiry:
            continue
        k = row.get('strikePrice')
        if k is None:
            continue
        strikes.append(k)
        ce_oi[k] = (row.get('CE') or {}).get('openInterest', 0) or 0
        pe_oi[k] = (row.get('PE') or {}).get('openInterest', 0) or 0

    strikes = sorted(set(strikes))
    if not strikes:
        return {'error': 'no strikes for nearest expiry'}

    tot_ce = sum(ce_oi.values())
    tot_pe = sum(pe_oi.values())
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else None
    max_call = max(strikes, key=lambda k: ce_oi.get(k, 0))
    max_put  = max(strikes, key=lambda k: pe_oi.get(k, 0))

    # Max pain: settlement price where option writers' total payout is lowest
    def writer_payout(s):
        pay = 0
        for k in strikes:
            pay += ce_oi.get(k, 0) * max(0, s - k)   # calls ITM above k
            pay += pe_oi.get(k, 0) * max(0, k - s)   # puts ITM below k
        return pay

    max_pain = min(strikes, key=writer_payout)

    return {'pcr': pcr, 'max_pain': max_pain, 'max_call': max_call,
            'max_put': max_put, 'expiry': expiry,
            'total_ce_oi': tot_ce, 'total_pe_oi': tot_pe}


@app.route('/fno')
def get_fno():
    global _fno_cache
    now = time.time()
    if _fno_cache['data'] and (now - _fno_cache['ts']) < 180:
        return add_cors(jsonify(_fno_cache['data']))

    out = {}
    try:
        s = _nse_session()
        for key, symbol in [('nifty', 'NIFTY'), ('banknifty', 'BANKNIFTY')]:
            try:
                r = s.get('https://www.nseindia.com/api/option-chain-indices'
                          f'?symbol={symbol}', timeout=12)
                r.raise_for_status()
                out[key] = _analyse_chain(r.json())
                print(f'[FNO] {symbol}: {out[key]}')
            except Exception as ex:
                print(f'[FNO WARN] {symbol}: {ex}')
                out[key] = {'error': str(ex)}
            time.sleep(1)  # be polite between the two calls
    except Exception as e:
        print(f'[FNO ERR] session: {e}')
        out = {'nifty': {'error': str(e)}, 'banknifty': {'error': str(e)}}

    out['fetched_at'] = datetime.utcnow().isoformat() + 'Z'
    # Only cache successful pulls so a failure retries quickly
    if not out.get('nifty', {}).get('error') or not out.get('banknifty', {}).get('error'):
        _fno_cache = {'data': out, 'ts': now}
    return add_cors(jsonify(out))


# ── News: latest headline per portfolio stock ─────────────────────────────────

@app.route('/news')
def get_news():
    syms_raw = request.args.get('symbols', '')
    syms = [s.strip().upper() for s in syms_raw.split(',') if s.strip()][:25]
    if not syms:
        return add_cors(jsonify({'error': 'pass ?symbols=BEL,ONGC,...'})), 400

    now = time.time()
    out = {}
    try:
        import yfinance as yf
        for sym in syms:
            # serve from per-symbol cache if under 10 minutes old
            c = _news_cache.get(sym)
            if c and (now - c['ts']) < 600:
                out[sym] = c['data']
                continue
            item = None
            try:
                news = yf.Ticker(sym + '.NS').news or []
                if news:
                    n0 = news[0]
                    # yfinance news schema varies by version; handle both
                    content = n0.get('content', n0)
                    title = content.get('title') or n0.get('title')
                    link = (content.get('canonicalUrl') or {}).get('url') \
                        if isinstance(content.get('canonicalUrl'), dict) \
                        else n0.get('link')
                    ts = n0.get('providerPublishTime')
                    when = None
                    if ts:
                        when = datetime.fromtimestamp(int(ts)).strftime('%d %b')
                    elif content.get('pubDate'):
                        when = str(content['pubDate'])[:10]
                    item = {'title': title, 'link': link, 'time': when}
            except Exception as ex:
                print(f'[NEWS WARN] {sym}: {ex}')
            out[sym] = item
            _news_cache[sym] = {'data': item, 'ts': now}
    except ImportError:
        return add_cors(jsonify({'error': 'yfinance missing'})), 500

    return add_cors(jsonify(out))


# ── Local dev ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Starting on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
