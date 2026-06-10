"""
S.Tater Trading — Live Backend Server
======================================
Three public endpoints:

  POST /alert   ← TradingView sends VCP alert here (webhook)
  GET  /signals ← Dashboard reads stored alerts every 5 minutes
  GET  /quotes  ← Dashboard reads live Nifty / Sensex / VIX / INR

Host this file on Render.com (free tier).
Author: built for Mr. Sudhinder Tater, June 2026.
"""

import json
import os
import time
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ── Database path ─────────────────────────────────────────────────────────────
# Render's free tier: files in the project directory survive restarts
# but are wiped on fresh deploys. Good enough for real-time signals.
DB_PATH = os.path.join(os.path.dirname(__file__), 'signals.db')


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the signals table if it doesn't already exist."""
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stock       TEXT    NOT NULL,
                signal      TEXT,
                price       TEXT,
                bar_time    TEXT,
                received_at TEXT
            )
        ''')
        conn.commit()


# Run once when gunicorn imports this module
init_db()


# ── CORS helper ───────────────────────────────────────────────────────────────
# The dashboard HTML is opened as a local file (file://) or a different domain,
# so every response needs the Access-Control-Allow-Origin header.

def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ── Quotes cache ──────────────────────────────────────────────────────────────
_cache = {'data': None, 'ts': 0}
CACHE_TTL = 60  # seconds — don't hit Yahoo more than once per minute


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    """Health-check — dashboard pings this every 14 min to keep server awake."""
    return add_cors(jsonify({
        'status': 'ST Trading server running',
        'time':   datetime.utcnow().isoformat() + 'Z',
        'db':     DB_PATH
    }))


@app.route('/alert', methods=['POST', 'OPTIONS'])
def receive_alert():
    """
    TradingView POSTs here when a VCP alert fires.

    Expected JSON body (set this as the alert message on TradingView):
    {
      "stock":  "{{ticker}}",
      "signal": "VCP Buy",
      "price":  "{{close}}",
      "time":   "{{time}}"
    }
    """
    # Respond to CORS preflight
    if request.method == 'OPTIONS':
        return add_cors(jsonify({})), 200

    try:
        payload = request.get_json(force=True, silent=True) or {}

        # Clean up ticker — TradingView sends "NSE:BEL", we store "BEL"
        stock    = str(payload.get('stock', 'UNKNOWN')).upper().replace('NSE:', '').strip()
        signal   = str(payload.get('signal', 'Alert')).strip()
        price    = str(payload.get('price', '')).strip()
        bar_time = str(payload.get('time',  '')).strip()
        received = datetime.utcnow().isoformat() + 'Z'

        with get_db() as conn:
            conn.execute(
                '''INSERT INTO signals
                   (stock, signal, price, bar_time, received_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (stock, signal, price, bar_time, received)
            )
            conn.commit()

        print(f'[ALERT] {received} | {stock} | {signal} | ₹{price}')
        return add_cors(jsonify({'status': 'ok', 'stock': stock})), 200

    except Exception as e:
        print(f'[ALERT ERROR] {e}')
        return add_cors(jsonify({'status': 'error', 'detail': str(e)})), 500


@app.route('/signals', methods=['GET'])
def get_signals():
    """
    Returns the 100 most recent VCP alerts, newest first.
    Dashboard polls this every 5 minutes and rebuilds the swing candidates list.
    """
    try:
        with get_db() as conn:
            rows = conn.execute(
                '''SELECT stock, signal, price, bar_time, received_at
                   FROM signals
                   ORDER BY id DESC
                   LIMIT 100'''
            ).fetchall()

        result = [dict(r) for r in rows]
        return add_cors(jsonify(result))

    except Exception as e:
        print(f'[SIGNALS ERROR] {e}')
        return add_cors(jsonify({'error': str(e)})), 500


@app.route('/quotes', methods=['GET'])
def get_quotes():
    """
    Returns live Nifty 50, Sensex, India VIX and USD/INR.
    Result is cached for 60 seconds so Yahoo Finance is not hammered.

    Sources:
      - Yahoo Finance via yfinance library (works server-side, no API key)
      - Frankfurter.app for USD/INR (genuinely free, no key, reliable)
    """
    global _cache
    now = time.time()

    # Serve from cache if fresh
    if _cache['data'] and (now - _cache['ts']) < CACHE_TTL:
        return add_cors(jsonify(_cache['data']))

    result = {}

    # ── Yahoo Finance ─────────────────────────────────────────────────────────
    try:
        import yfinance as yf

        symbols = {
            'nifty':  '^NSEI',
            'sensex': '^BSESN',
            'vix':    '^INDIAVIX',
        }

        for key, sym in symbols.items():
            try:
                ticker = yf.Ticker(sym)
                fi     = ticker.fast_info
                lp     = round(float(fi.last_price),      2)
                pc     = round(float(fi.previous_close),  2)
                chg    = round((lp - pc) / pc * 100, 2) if pc else 0
                result[key] = {'val': lp, 'chg': chg, 'prev': pc}
                print(f'[QUOTES] {sym}: {lp} ({chg:+.2f}%)')
            except Exception as ex:
                print(f'[QUOTES WARN] {sym}: {ex}')
                result[key] = {'val': None, 'chg': None, 'error': str(ex)}

    except ImportError:
        result['yfinance_missing'] = True
        print('[QUOTES ERROR] yfinance not installed — check requirements.txt')

    # ── USD / INR via Frankfurter (free, no key) ──────────────────────────────
    try:
        import urllib.request
        url = 'https://api.frankfurter.app/latest?from=USD&to=INR'
        with urllib.request.urlopen(url, timeout=6) as r:
            fx = json.loads(r.read())
            result['usdinr'] = {'val': round(fx['rates']['INR'], 2)}
    except Exception as ex:
        print(f'[QUOTES WARN] USD/INR: {ex}')
        result['usdinr'] = {'val': None, 'error': str(ex)}

    result['fetched_at'] = datetime.utcnow().isoformat() + 'Z'

    # Store in cache
    _cache = {'data': result, 'ts': now}
    return add_cors(jsonify(result))


# ── Local dev entry point ─────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Starting on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
