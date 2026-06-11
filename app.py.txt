"""
S.Tater Trading — Live Backend Server (v3)
===========================================
NEW IN V3: ICICI Direct Breeze API as the primary data source.
Yahoo Finance remains the automatic fallback, so the dashboard
works even on days the Breeze session is not refreshed.

Endpoints:
  POST /alert     ← TradingView VCP alert webhook
  GET  /signals   ← stored alerts
  GET  /quotes    ← indices + commodities  (Breeze first, Yahoo fallback)
  GET  /fno       ← PCR / Max Pain / Max OI (Breeze chain first, NSE fallback)
  GET  /holdings  ← your ICICI Direct demat holdings (auto-loads portfolio)
  GET  /news      ← latest headline per stock (?symbols=BEL,ONGC)
  GET  /session   ← paste the daily Breeze session token (?token=XXXX&pin=YYYY)
  GET  /status    ← is Breeze connected right now?

CREDENTIALS — set these in Render > your service > Environment tab.
NEVER write the actual keys inside this file:
  BREEZE_API_KEY     = your App Key from api.icicidirect.com
  BREEZE_API_SECRET  = your Secret Key
  ADMIN_PIN          = any 4-6 digit number you choose (protects /session)

DAILY MORNING STEP (SEBI requires a fresh session every day, ~1 minute):
  1. Open:  https://api.icicidirect.com/apiuser/login?api_key=YOUR_APP_KEY
  2. Log in with your ICICI Direct user ID + password
  3. After login the address bar shows ...apisession=XXXXXXXX — copy that number
  4. Open:  https://your-server.onrender.com/session?token=XXXXXXXX&pin=YOUR_PIN
  5. You should see {"status": "breeze connected"} — done for the day.

Built for Mr. Sudhinder Tater, June 2026.
"""

import json
import os
import time
import calendar
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'signals.db')

# ── Credentials from environment (placeholders — set real values on Render) ──
BREEZE_API_KEY    = os.environ.get('BREEZE_API_KEY', '')
BREEZE_API_SECRET = os.environ.get('BREEZE_API_SECRET', '')
ADMIN_PIN         = os.environ.get('ADMIN_PIN', '')

# ── MCX calibration (estimates from international prices, see v2 notes) ──────
CAL_GOLD, CAL_SILVER, CAL_CRUDE = 1.167, 1.179, 0.977
TROY_OZ_G = 31.1035

# Breeze stock codes for indices (ICICI's own codes, not NSE symbols)
BREEZE_NIFTY     = 'NIFTY'
BREEZE_BANKNIFTY = 'CNXBAN'


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock TEXT NOT NULL, signal TEXT, price TEXT,
            bar_time TEXT, received_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)''')
        conn.commit()


def get_setting(key):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None


def set_setting(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, value))
        conn.commit()


init_db()


def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# BREEZE CONNECTION (primary data source)
# ══════════════════════════════════════════════════════════════════════════════

_breeze = {'client': None, 'token': None, 'err': 'not connected yet'}


def get_breeze():
    """Return a live Breeze client, or None (callers then fall back to Yahoo)."""
    token = get_setting('breeze_session')
    if not (BREEZE_API_KEY and BREEZE_API_SECRET):
        _breeze['err'] = 'API key/secret not set in environment'
        return None
    if not token:
        _breeze['err'] = 'no session token yet — do the morning /session step'
        return None
    # Reuse existing client if the token hasn't changed
    if _breeze['client'] is not None and _breeze['token'] == token:
        return _breeze['client']
    try:
        from breeze_connect import BreezeConnect
        b = BreezeConnect(api_key=BREEZE_API_KEY)
        b.generate_session(api_secret=BREEZE_API_SECRET, session_token=token)
        _breeze.update(client=b, token=token, err=None)
        print('[BREEZE] session connected')
        return b
    except Exception as e:
        _breeze.update(client=None, err=str(e))
        print(f'[BREEZE ERR] {e}')
        return None


def _success_rows(resp):
    """Breeze responses wrap data in {'Success': [...]}; be defensive."""
    if isinstance(resp, dict):
        rows = resp.get('Success')
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict):
            return [rows]
    return []


def _num(x):
    try:
        v = float(str(x).replace(',', ''))
        return v
    except (TypeError, ValueError):
        return None


def _pick(row, *names):
    """Return the first present, non-empty field among candidate key names."""
    for n in names:
        if n in row and row[n] not in (None, '', 0, '0'):
            return row[n]
    for n in names:           # second pass: allow zero values
        if n in row and row[n] not in (None, ''):
            return row[n]
    return None


def breeze_quote(stock_code, exchange_code='NSE'):
    """(last, chg_pct, prev_close) via Breeze, or (None, None, None)."""
    b = get_breeze()
    if not b:
        return None, None, None
    try:
        r = b.get_quotes(stock_code=stock_code, exchange_code=exchange_code,
                         expiry_date='', product_type='cash', right='', strike_price='')
        rows = _success_rows(r)
        if not rows:
            return None, None, None
        row = rows[0]
        ltp  = _num(_pick(row, 'ltp', 'last_traded_price', 'close'))
        prev = _num(_pick(row, 'previous_close', 'prev_close', 'open'))
        chg = round((ltp - prev) / prev * 100, 2) if (ltp and prev) else None
        return (round(ltp, 2) if ltp else None), chg, (round(prev, 2) if prev else None)
    except Exception as e:
        print(f'[BREEZE QUOTE WARN] {stock_code}: {e}')
        return None, None, None


# ── expiry helpers for option chain ───────────────────────────────────────────

def next_thursday(d: date) -> date:
    return d + timedelta(days=(3 - d.weekday()) % 7)        # Thu = weekday 3


def last_thursday_of_month(y: int, m: int) -> date:
    last = date(y, m, calendar.monthrange(y, m)[1])
    return last - timedelta(days=(last.weekday() - 3) % 7)


def expiry_iso(d: date) -> str:
    return d.isoformat() + 'T06:00:00.000Z'


def breeze_option_chain(stock_code, expiry: date):
    """{strike: {'ce': oi, 'pe': oi}} from Breeze, or None on failure."""
    b = get_breeze()
    if not b:
        return None
    chain = {}
    try:
        for right in ('call', 'put'):
            r = b.get_option_chain_quotes(
                stock_code=stock_code, exchange_code='NFO',
                product_type='options', expiry_date=expiry_iso(expiry),
                right=right, strike_price='')
            for row in _success_rows(r):
                k = _num(_pick(row, 'strike_price', 'strikePrice'))
                oi = _num(_pick(row, 'open_interest', 'openInterest', 'oi')) or 0
                if k is None:
                    continue
                k = int(k)
                chain.setdefault(k, {'ce': 0, 'pe': 0})
                chain[k]['ce' if right == 'call' else 'pe'] = int(oi)
            time.sleep(0.4)   # stay well inside the 100 calls/min limit
        return chain if chain else None
    except Exception as e:
        print(f'[BREEZE CHAIN WARN] {stock_code}: {e}')
        return None


def analyse_strikes(chain, expiry_label, source):
    strikes = sorted(chain.keys())
    if not strikes:
        return {'error': 'empty chain'}
    tot_ce = sum(v['ce'] for v in chain.values())
    tot_pe = sum(v['pe'] for v in chain.values())
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else None
    max_call = max(strikes, key=lambda k: chain[k]['ce'])
    max_put  = max(strikes, key=lambda k: chain[k]['pe'])

    def writer_payout(s):
        pay = 0
        for k in strikes:
            pay += chain[k]['ce'] * max(0, s - k)
            pay += chain[k]['pe'] * max(0, k - s)
        return pay

    max_pain = min(strikes, key=writer_payout)
    return {'pcr': pcr, 'max_pain': max_pain, 'max_call': max_call,
            'max_put': max_put, 'expiry': expiry_label, 'source': source,
            'total_ce_oi': tot_ce, 'total_pe_oi': tot_pe}


# ══════════════════════════════════════════════════════════════════════════════
# YAHOO FALLBACK
# ══════════════════════════════════════════════════════════════════════════════

def yf_quote(sym):
    try:
        import yfinance as yf
        fi = yf.Ticker(sym).fast_info
        lp, pc = float(fi.last_price), float(fi.previous_close)
        chg = round((lp - pc) / pc * 100, 2) if pc else None
        return round(lp, 2), chg, round(pc, 2)
    except Exception as ex:
        print(f'[YF WARN] {sym}: {ex}')
        return None, None, None


def nse_chain_fallback(symbol):
    """v2's NSE scraping, kept as last resort for the option chain."""
    import requests
    s = requests.Session()
    s.headers.update({
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
        'Accept-Language': 'en-US,en;q=0.9', 'Accept': 'application/json',
        'Referer': 'https://www.nseindia.com/option-chain'})
    s.get('https://www.nseindia.com', timeout=10)
    r = s.get(f'https://www.nseindia.com/api/option-chain-indices?symbol={symbol}',
              timeout=12)
    r.raise_for_status()
    records = r.json().get('records', {})
    expiries = records.get('expiryDates') or []
    rows = records.get('data') or []
    if not expiries or not rows:
        return {'error': 'empty NSE chain'}
    expiry = expiries[0]
    chain = {}
    for row in rows:
        if row.get('expiryDate') != expiry:
            continue
        k = row.get('strikePrice')
        if k is None:
            continue
        chain.setdefault(int(k), {'ce': 0, 'pe': 0})
        chain[int(k)]['ce'] = (row.get('CE') or {}).get('openInterest', 0) or 0
        chain[int(k)]['pe'] = (row.get('PE') or {}).get('openInterest', 0) or 0
    return analyse_strikes(chain, expiry, 'nse-fallback')


# ══════════════════════════════════════════════════════════════════════════════
# CACHES
# ══════════════════════════════════════════════════════════════════════════════
_q_cache    = {'data': None, 'ts': 0}
_fno_cache  = {'data': None, 'ts': 0}
_hold_cache = {'data': None, 'ts': 0}
_news_cache = {}
_name_cache = {}    # ICICI stock code -> NSE symbol


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return add_cors(jsonify({'status': 'ST Trading server v3 running',
                             'time': datetime.utcnow().isoformat() + 'Z'}))


@app.route('/status')
def status():
    connected = get_breeze() is not None
    return add_cors(jsonify({
        'breeze_connected': connected,
        'breeze_error': None if connected else _breeze['err'],
        'keys_set': bool(BREEZE_API_KEY and BREEZE_API_SECRET),
        'data_mode': 'ICICI Breeze (live)' if connected else 'Yahoo fallback'}))


@app.route('/session')
def set_session():
    """Morning step: /session?token=XXXXXXXX&pin=YOUR_PIN"""
    pin = request.args.get('pin', '')
    token = request.args.get('token', '').strip()
    if ADMIN_PIN and pin != ADMIN_PIN:
        return add_cors(jsonify({'status': 'wrong pin'})), 403
    if not token:
        return add_cors(jsonify({'status': 'no token given',
                                 'usage': '/session?token=XXXXXXXX&pin=YOUR_PIN'})), 400
    set_setting('breeze_session', token)
    _breeze['client'] = None           # force reconnect with the new token
    ok = get_breeze() is not None
    # Clear data caches so the next calls use Breeze immediately
    _q_cache['ts'] = _fno_cache['ts'] = _hold_cache['ts'] = 0
    return add_cors(jsonify({'status': 'breeze connected' if ok
                             else f"saved, but connect failed: {_breeze['err']}"}))


# ── TradingView webhook (unchanged from v2) ───────────────────────────────────

@app.route('/alert', methods=['POST', 'OPTIONS'])
def receive_alert():
    if request.method == 'OPTIONS':
        return add_cors(jsonify({})), 200
    try:
        p = request.get_json(force=True, silent=True) or {}
        stock = str(p.get('stock', 'UNKNOWN')).upper().replace('NSE:', '').strip()
        with get_db() as conn:
            conn.execute('INSERT INTO signals (stock,signal,price,bar_time,received_at) '
                         'VALUES (?,?,?,?,?)',
                         (stock, str(p.get('signal', 'Alert')).strip(),
                          str(p.get('price', '')).strip(),
                          str(p.get('time', '')).strip(),
                          datetime.utcnow().isoformat() + 'Z'))
            conn.commit()
        print(f'[ALERT] {stock}')
        return add_cors(jsonify({'status': 'ok', 'stock': stock})), 200
    except Exception as e:
        return add_cors(jsonify({'status': 'error', 'detail': str(e)})), 500


@app.route('/signals')
def get_signals():
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT stock,signal,price,bar_time,received_at '
                                'FROM signals ORDER BY id DESC LIMIT 100').fetchall()
        return add_cors(jsonify([dict(r) for r in rows]))
    except Exception as e:
        return add_cors(jsonify({'error': str(e)})), 500


# ── QUOTES: Breeze first, Yahoo fallback ──────────────────────────────────────

@app.route('/quotes')
def get_quotes():
    global _q_cache
    now = time.time()
    if _q_cache['data'] and (now - _q_cache['ts']) < 60:
        return add_cors(jsonify(_q_cache['data']))

    out = {}
    breeze_on = get_breeze() is not None

    # NIFTY and BANK NIFTY — Breeze first
    for key, bcode, ysym in [('nifty', BREEZE_NIFTY, '^NSEI'),
                             ('banknifty', BREEZE_BANKNIFTY, '^NSEBANK')]:
        v = c = p = None
        if breeze_on:
            v, c, p = breeze_quote(bcode, 'NSE')
        if v is None:
            v, c, p = yf_quote(ysym)
            src = 'yahoo'
        else:
            src = 'breeze'
        out[key] = {'val': v, 'chg': c, 'prev': p, 'source': src}

    # SENSEX and VIX — Yahoo (not exposed cleanly on Breeze)
    for key, ysym in [('sensex', '^BSESN'), ('vix', '^INDIAVIX')]:
        v, c, p = yf_quote(ysym)
        out[key] = {'val': v, 'chg': c, 'prev': p, 'source': 'yahoo'}

    # GIFT Nifty — no free feed exists (NSE IX licenses it)
    out['gift'] = {'val': None, 'chg': None,
                   'note': 'No free GIFT feed; check nseix.com pre-market'}

    # USD/INR
    usdinr = None
    try:
        import urllib.request
        with urllib.request.urlopen(
                'https://api.frankfurter.app/latest?from=USD&to=INR', timeout=6) as r:
            usdinr = round(json.loads(r.read())['rates']['INR'], 2)
    except Exception as ex:
        print(f'[FX WARN] {ex}')
    out['usdinr'] = {'val': usdinr}

    # Commodities — international (Yahoo) with MCX estimates
    gold, gchg, _ = yf_quote('GC=F')
    silver, schg, _ = yf_quote('SI=F')
    wti, wchg, _ = yf_quote('CL=F')
    brent, _, _ = yf_quote('BZ=F')
    out['gold_spot']   = {'val': gold,   'chg': gchg}
    out['silver_spot'] = {'val': silver, 'chg': schg}
    out['crude_wti']   = {'val': wti,    'chg': wchg}
    out['brent']       = {'val': brent}
    out['gold_mcx']   = {'val': round(gold * usdinr / TROY_OZ_G * 10 * CAL_GOLD) if (gold and usdinr) else None, 'chg': gchg, 'est': True}
    out['silver_mcx'] = {'val': round(silver * usdinr / TROY_OZ_G * 1000 * CAL_SILVER) if (silver and usdinr) else None, 'chg': schg, 'est': True}
    out['crude_mcx']  = {'val': round(wti * usdinr * CAL_CRUDE) if (wti and usdinr) else None, 'chg': wchg, 'est': True}

    out['data_mode'] = 'breeze' if breeze_on else 'yahoo-fallback'
    out['fetched_at'] = datetime.utcnow().isoformat() + 'Z'
    _q_cache = {'data': out, 'ts': now}
    return add_cors(jsonify(out))


# ── F&O: Breeze official chain first, NSE scrape fallback ─────────────────────

@app.route('/fno')
def get_fno():
    global _fno_cache
    now = time.time()
    if _fno_cache['data'] and (now - _fno_cache['ts']) < 180:
        return add_cors(jsonify(_fno_cache['data']))

    today = date.today()
    nifty_exp = next_thursday(today)
    bn_exp = last_thursday_of_month(today.year, today.month)
    if bn_exp < today:
        nxt = today.replace(day=1) + timedelta(days=32)
        bn_exp = last_thursday_of_month(nxt.year, nxt.month)

    out = {}
    for key, bcode, nse_sym, exp in [('nifty', BREEZE_NIFTY, 'NIFTY', nifty_exp),
                                     ('banknifty', BREEZE_BANKNIFTY, 'BANKNIFTY', bn_exp)]:
        result = None
        chain = breeze_option_chain(bcode, exp)          # 1) official Breeze
        if chain:
            result = analyse_strikes(chain, exp.strftime('%d-%b-%Y'), 'breeze')
        if not result or result.get('error'):
            try:                                          # 2) NSE fallback
                result = nse_chain_fallback(nse_sym)
            except Exception as ex:
                result = {'error': str(ex)}
        out[key] = result
        print(f'[FNO] {key}: {result}')

    out['fetched_at'] = datetime.utcnow().isoformat() + 'Z'
    if not out.get('nifty', {}).get('error') or not out.get('banknifty', {}).get('error'):
        _fno_cache = {'data': out, 'ts': now}
    return add_cors(jsonify(out))


# ── HOLDINGS: auto-load your ICICI Direct portfolio ───────────────────────────

def icici_to_nse(code, breeze):
    """Map ICICI's internal stock code to the NSE symbol (cached)."""
    if code in _name_cache:
        return _name_cache[code]
    nse = code
    try:
        r = breeze.get_names(exchange_code='NSE', stock_code=code)
        if isinstance(r, dict):
            cand = _pick(r, 'nse_stock_code', 'NSE_StockCode', 'exchange_stock_code',
                         'exchange_code_name', 'stock_code')
            if cand:
                nse = str(cand).strip().upper()
    except Exception as ex:
        print(f'[NAMES WARN] {code}: {ex}')
    _name_cache[code] = nse
    return nse


@app.route('/holdings')
def get_holdings():
    global _hold_cache
    now = time.time()
    if _hold_cache['data'] and (now - _hold_cache['ts']) < 300:
        return add_cors(jsonify(_hold_cache['data']))

    b = get_breeze()
    if not b:
        return add_cors(jsonify({'error': 'breeze not connected',
                                 'detail': _breeze['err']})), 503
    out = []
    try:
        r = b.get_demat_holdings()
        rows = _success_rows(r)
        for row in rows[:60]:
            code = str(_pick(row, 'stock_code', 'stockCode', 'symbol') or '').strip()
            if not code:
                continue
            qty = _num(_pick(row, 'quantity', 'total_quantity', 'demat_total_bulk_quantity'))
            avg = _num(_pick(row, 'average_price', 'avg_price', 'average_cost', 'cost_price'))
            cmp_ = _num(_pick(row, 'current_market_price', 'ltp', 'close_price',
                              'market_price', 'previous_close'))
            co = str(_pick(row, 'company_name', 'stock_name', 'companyName') or '').strip()
            nse = icici_to_nse(code, b)
            # Enrich missing CMP via a live quote (only for the first few, rate-limit safe)
            if cmp_ is None and len(out) < 20:
                v, _, _ = breeze_quote(code, 'NSE')
                cmp_ = v
            out.append({'sym': nse, 'icici_code': code, 'co': co or nse,
                        'qty': qty, 'avg': avg, 'cmp': cmp_})
        payload = {'holdings': out, 'count': len(out), 'source': 'breeze',
                   'fetched_at': datetime.utcnow().isoformat() + 'Z'}
        _hold_cache = {'data': payload, 'ts': now}
        return add_cors(jsonify(payload))
    except Exception as e:
        print(f'[HOLDINGS ERR] {e}')
        return add_cors(jsonify({'error': str(e)})), 500


# ── NEWS (unchanged from v2) ──────────────────────────────────────────────────

@app.route('/news')
def get_news():
    syms = [s.strip().upper() for s in request.args.get('symbols', '').split(',')
            if s.strip()][:25]
    if not syms:
        return add_cors(jsonify({'error': 'pass ?symbols=BEL,ONGC'})), 400
    now = time.time()
    out = {}
    try:
        import yfinance as yf
        for sym in syms:
            c = _news_cache.get(sym)
            if c and (now - c['ts']) < 600:
                out[sym] = c['data']
                continue
            item = None
            try:
                news = yf.Ticker(sym + '.NS').news or []
                if news:
                    n0 = news[0]
                    content = n0.get('content', n0)
                    title = content.get('title') or n0.get('title')
                    link = (content.get('canonicalUrl') or {}).get('url') \
                        if isinstance(content.get('canonicalUrl'), dict) else n0.get('link')
                    ts = n0.get('providerPublishTime')
                    when = datetime.fromtimestamp(int(ts)).strftime('%d %b') if ts \
                        else (str(content.get('pubDate'))[:10] if content.get('pubDate') else None)
                    item = {'title': title, 'link': link, 'time': when}
            except Exception as ex:
                print(f'[NEWS WARN] {sym}: {ex}')
            out[sym] = item
            _news_cache[sym] = {'data': item, 'ts': now}
    except ImportError:
        return add_cors(jsonify({'error': 'yfinance missing'})), 500
    return add_cors(jsonify(out))


# ── local dev ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'Starting v3 on http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
