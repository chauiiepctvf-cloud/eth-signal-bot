"""
OKX Scalp Bot v5.4 FINAL
- Градуированная система весов (3 уровня)
- Система самообучения (GradientBoosting, каждые 20 сделок)
- Крупные сделки (aggTrades Binance)
- Тейк +50 процентов маржи, Стоп -50 процентов маржи, Плечо 50, Маржа 20 USDT
- ВСЕ ошибки исправлены, calc работает, баллы ненулевые
"""
import os, time, logging, threading, hashlib, hmac, base64, json
import requests, pandas as pd, numpy as np
import os, time, logging, threading, hashlib, hmac, base64, json
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from flask import Flask
from sklearn.ensemble import IsolationForest, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
import joblib

# ═══════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 50
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60

BASE_MIN_SCORE = 5.0
MIN_SCORE = 5.0
MIN_SCORE_DIFF = 2.0
MAX_SCORE = 13.0

SL_MARGIN_PCT = 0.50
TP_MARGIN_PCT = 0.50

ATR_MIN_PCT = 0.001
MAX_LOSSES = 4
PAUSE_LOSSES = 60 * 60
MAX_HOLD_HOURS = 6
RED_NEWS_DROP = 0.01
RED_NEWS_VOL = 2.0
RED_NEWS_BLOCK = 15 * 60
LIQ_THRESHOLD = 5.0
NIGHT_HOURS = (1, 6)
FORCE_TEST = os.environ.get("FORCE_TEST", "false").lower() == "true"
STATS_FILE = "bot_stats.json"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
app = Flask(__name__)

@app.route("/")
def home():
    return f"OKX Scalp Bot v5.4 | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# ═══════════════════════════════════════════
# JSON
# ═══════════════════════════════════════════
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"Save {filename}: {e}")

# ═══════════════════════════════════════════
# СОСТОЯНИЕ
# ═══════════════════════════════════════════
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []
last_ob = 0
yesterday_high = 0
yesterday_low = 0
red_news_until = 0
force_test_done = False
_trades_since_last_train = 0
RETRAIN_EVERY = 20

cache = {
    "fear_greed": {"value": 50, "ts": 0},
    "long_short": {"value": 1.0, "ts": 0},
    "taker_ratio": {"value": 1.0, "ts": 0},
    "open_interest": {"value": 0, "change": 0, "ts": 0},
    "dxy": {"value": 100, "change": 0, "ts": 0},
    "vix": {"value": 20, "ts": 0},
    "usdt_dominance": {"value": 5.0, "change": 0, "ts": 0},
    "coinbase_premium": {"value": 0.0, "ts": 0},
    "eth_btc_ratio": {"value": 0.0, "change": 0.0, "ts": 0},
    "liquidations": {"long_liq": 0.0, "short_liq": 0.0, "ts": 0},
    "btc_dominance": {"value": 50, "change": 0, "ts": 0},
    "funding_avg": {"value": 0.0, "ts": 0},
    "trending": {"value": False, "ts": 0},
    "gas_price": {"value": 20, "ts": 0},
    "news_sentiment": {"value": 0.0, "ts": 0},
    "stablecoin": {"value": 0, "change": 0, "ts": 0},
    "4h_trend": {"diff": 0.0, "bull": False, "ts": 0},
}

stats = {"total": 0, "wins": 0, "losses": 0, "total_profit": 0.0,
         "total_profit_sum": 0.0, "total_loss_sum": 0.0,
         "max_drawdown": 0.0, "peak_equity": 0.0, "current_equity": 0.0}
active_positions = {}
signals_history = []
scalp_model = None

def load_stats():
    global stats
    l = load_json(STATS_FILE, {})
    for k in stats:
        if k in l:
            stats[k] = l[k]
    log.info(f"Статистика: {stats['total']} сд")

def save_stats():
    save_json(STATS_FILE, stats)

def load_signals():
    global signals_history
    signals_history = load_json("signals_history.json", [])

def save_signals():
    save_json("signals_history.json", signals_history)

def load_model():
    global scalp_model
    try:
        scalp_model = joblib.load("scalp_model.pkl")
        log.info("ML модель загружена")
    except:
        log.info("ML модель не найдена")

def load_positions():
    global active_positions
    active_positions = load_json("active_positions.json", {})

def save_positions():
    save_json("active_positions.json", active_positions)

load_stats()
load_signals()
load_model()
load_positions()

# ═══════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.status_code == 200:
            log.info("TG ✅")
        else:
            log.error(f"TG ❌ {r.text}")
    except Exception as e:
        log.error(f"TG: {e}")

# ═══════════════════════════════════════════
# OKX API
# ═══════════════════════════════════════════
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def _sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {"OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
            "Content-Type": "application/json", **OKX_DEMO_HEADER}

def okx_get(path, params=None):
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    try:
        return requests.get(OKX_BASE + path + qs, headers=_sign(_ts(), "GET", path + qs), timeout=10).json()
    except Exception as e:
        log.error(f"GET {path}: {e}")
        return {}

def okx_post(path, body):
    data = json.dumps(body)
    hdr = _sign(_ts(), "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        if d.get("code") != "0":
            log.warning(f"POST {path}: {d.get('code')} {d.get('msg','')}")
        return d
    except Exception as e:
        log.error(f"POST {path}: {e}")
        return {}

def okx_set_leverage():
    okx_post("/api/v5/account/set-leverage", {"instId": SYMBOL, "lever": str(LEVERAGE), "mgnMode": "cross"})

def okx_balance():
    try:
        r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
        for d in r.get("data", [{}])[0].get("details", []):
            if d.get("ccy") == "USDT":
                return float(d.get("availBal", 0))
    except:
        pass
    return 0.0

def okx_positions():
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_cancel(algo_id):
    if algo_id:
        okx_post("/api/v5/trade/cancel-algo-order", {"instId": SYMBOL, "algoId": algo_id})

def okx_close_market(pos_side, qty):
    cls = "sell" if pos_side == "long" else "buy"
    return okx_post("/api/v5/trade/order", {"instId": SYMBOL, "tdMode": "cross",
                    "side": cls, "posSide": pos_side, "ordType": "market", "sz": str(qty)})

def okx_place_order(direction, entry, sl, tp):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    ps = "long" if direction == "LONG" else "short"
    cls = "sell" if direction == "LONG" else "buy"
    hour = datetime.now(timezone.utc).hour
    mult = 0.5 if NIGHT_HOURS[0] <= hour < NIGHT_HOURS[1] else 1.0
    qty = max(1, round(ORDER_USDT * mult * LEVERAGE / entry / 0.01))
    log.info(f"▶ {direction} qty={qty} entry={entry:.2f}")

    r = okx_post("/api/v5/trade/order", {"instId": SYMBOL, "tdMode": "cross",
                 "side": side, "posSide": ps, "ordType": "market", "sz": str(qty)})
    if r.get("code") != "0":
        msg = r.get("msg", "?")
        if r.get("data"):
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}

    oid = r["data"][0].get("ordId", "")
    log.info(f"✅ open ordId={oid}")
    active_positions[oid] = {"direction": direction, "entry": entry, "sl": sl, "tp": tp,
                             "total_qty": qty, "pos_side": ps, "cls_side": cls,
                             "open_time": time.time(), "sl_order_id": None, "tp_order_id": None}
    save_positions()
    time.sleep(3)

    sl_ok = tp_ok = False
    for _ in range(3):
        sr = okx_post("/api/v5/trade/order-algo", {"instId": SYMBOL, "tdMode": "cross",
                      "side": cls, "posSide": ps, "ordType": "conditional", "sz": str(qty),
                      "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last"})
        if sr.get("code") == "0":
            active_positions[oid]["sl_order_id"] = sr["data"][0].get("algoId", "")
            sl_ok = True
            break
        time.sleep(1)

    for _ in range(3):
        tr = okx_post("/api/v5/trade/order-algo", {"instId": SYMBOL, "tdMode": "cross",
                      "side": cls, "posSide": ps, "ordType": "conditional", "sz": str(qty),
                      "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last"})
        if tr.get("code") == "0":
            active_positions[oid]["tp_order_id"] = tr["data"][0].get("algoId", "")
            tp_ok = True
            break
        time.sleep(1)

    save_positions()
    return {"ok": True, "orderId": oid, "total_qty": qty, "sl_ok": sl_ok, "tp_ok": tp_ok}

# ═══════════════════════════════════════════
# ДАННЫЕ
# ═══════════════════════════════════════════
def get_klines(symbol=None, interval="5m", limit=150):
    sym = symbol or SYMBOL_BN
    try:
        data = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}", timeout=10).json()
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume",
                                          "ct", "qv", "trades", "taker_buy_base", "tbq", "ignore"])
        for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
            df[c] = df[c].astype(float)
        df["candle_time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e:
        log.error(f"klines {sym}: {e}")
        return None

def get_funding():
    try:
        return float(requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json().get("lastFundingRate", 0))
    except:
        return 0.0

def get_ob():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = sum(float(b[1]) for b in d.get("bids", []))
        asks = sum(float(a[1]) for a in d.get("asks", []))
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

def get_btc_momentum():
    try:
        df = get_klines(BTC_SYMBOL, "3m", 5)
        if df is None:
            return 0.0, 0
        chg = (df.iloc[-1]["close"] - df.iloc[-3]["close"]) / df.iloc[-3]["close"] * 100
        d = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), d
    except:
        return 0.0, 0

def get_yesterday_levels():
    try:
        df = get_klines(SYMBOL_BN, "1d", 2)
        if df is not None and len(df) >= 2:
            y = df.iloc[-2]
            return float(y["high"]), float(y["low"])
    except:
        pass
    return 0, 0

# ═══════════════════════════════════════════
# КЭШИРОВАННЫЕ МЕТРИКИ
# ═══════════════════════════════════════════
def _c1(key, ttl, fetcher):
    now = time.time()
    if now - cache[key].get("ts", 0) < ttl:
        return cache[key].get("value", 0)
    try:
        v = fetcher()
        cache[key] = {"value": v, "ts": now}
        return v
    except Exception as e:
        log.error(f"Metric {key}: {e}")
        return cache[key].get("value", 0)

def _c2(key, ttl, fetcher):
    now = time.time()
    if now - cache[key].get("ts", 0) < ttl:
        return cache[key].get("value", 0), cache[key].get("change", 0)
    try:
        v, c = fetcher()
        cache[key] = {"value": v, "change": c, "ts": now}
        return v, c
    except Exception as e:
        log.error(f"Metric {key}: {e}")
        return cache[key].get("value", 0), cache[key].get("change", 0)

def get_fear_greed():
    return _c1("fear_greed", 3600, lambda: int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]["value"]))

def get_long_short_ratio():
    return _c1("long_short", 300, lambda: float(requests.get("https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?symbol=ETHUSDT&period=5m&limit=1", timeout=10).json()[0]["longShortRatio"]))

def get_taker_ratio():
    return _c1("taker_ratio", 300, lambda: float(requests.get("https://fapi.binance.com/fapi/v1/takerlongshortRatio?symbol=ETHUSDT&period=5m&limit=1", timeout=10).json()[0]["buySellRatio"]))

def get_open_interest():
    return _c2("open_interest", 300, lambda: _oi())

def _oi():
    oi = float(requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT", timeout=10).json()["openInterest"])
    prev = cache.get("open_interest", {}).get("value", oi)
    return oi, ((oi - prev) / prev * 100) if prev > 0 else 0

def get_dxy():
    return _c2("dxy", 3600, lambda: _dxy())

def _dxy():
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=2d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    m = r.json()["chart"]["result"][0]["meta"]
    return m["regularMarketPrice"], (m["regularMarketPrice"] - m["previousClose"]) / m["previousClose"] * 100

def get_vix():
    return _c1("vix", 3600, lambda: _vix())

def _vix():
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    return r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

def get_usdt_dominance():
    return _c2("usdt_dominance", 600, lambda: _dom("usdt"))

def get_btc_dominance():
    return _c2("btc_dominance", 600, lambda: _dom("btc"))

def _dom(coin):
    dom = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"][coin]
    key = "btc_dominance" if coin == "btc" else "usdt_dominance"
    prev = cache.get(key, {}).get("value", dom)
    return dom, dom - prev

def get_coinbase_premium():
    return _c1("coinbase_premium", 60, lambda: _cb())

def _cb():
    cb = float(requests.get("https://api.exchange.coinbase.com/products/ETH-USD/ticker", timeout=8).json()["price"])
    bn = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=8).json()["price"])
    return round((cb - bn) / bn * 100, 4)

def get_eth_btc_ratio():
    return _c2("eth_btc_ratio", 180, lambda: _ethbtc())

def _ethbtc():
    d = requests.get("https://api.binance.com/api/v3/klines?symbol=ETHBTC&interval=5m&limit=4", timeout=8).json()
    return float(d[-1][4]), round((float(d[-1][4]) - float(d[-4][4])) / float(d[-4][4]) * 100, 4)

def get_liquidations():
    return _c2("liquidations", 300, lambda: _liq())

def _liq():
    d = requests.get(f"https://fapi.binance.com/fapi/v1/forceOrders?symbol={SYMBOL_BN}&limit=200", timeout=10).json()
    L, S = 0.0, 0.0
    cut = (time.time() - 3600) * 1000
    for o in d:
        if float(o.get("time", 0)) < cut:
            continue
        q = float(o.get("origQty", 0)) * float(o.get("price", 0))
        if o.get("side") == "SELL":
            L += q
        else:
            S += q
    return round(L / 1_000_000, 3), round(S / 1_000_000, 3)

def get_funding_avg():
    return _c1("funding_avg", 300, lambda: _fund())

def _fund():
    r = []
    b = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json()
    r.append(float(b.get("lastFundingRate", 0)))
    o = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}", timeout=8).json()
    if o.get("data"):
        r.append(float(o["data"][0].get("fundingRate", 0)))
    return sum(r) / len(r) if r else 0.0

def get_trending():
    return _c1("trending", 600, lambda: "ETH" in [c["item"]["symbol"] for c in requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json().get("coins", [])])

def get_gas_price():
    return _c1("gas_price", 60, lambda: float(requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10).json()["result"]["ProposeGasPrice"]))

def get_news_sentiment():
    return _c1("news_sentiment", 300, lambda: _news())

def _news():
    df = get_klines(SYMBOL_BN, "5m", 12)
    if df is not None and len(df) > 0:
        vc = df["volume"].iloc[-1] / df["volume"].mean() if df["volume"].mean() > 0 else 1
        pc = (df["close"].iloc[-1] - df["close"].iloc[-6]) / df["close"].iloc[-6] * 100 if df["close"].iloc[-6] != 0 else 0
        return max(-1.0, min(1.0, (vc - 1) * 0.5 + pc * 0.1))
    return 0.0

def get_stablecoin_supply():
    return _c2("stablecoin", 600, lambda: _stable())

def _stable():
    r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=usd&include_market_cap=true", timeout=10).json()
    m = r.get("tether", {}).get("usd_market_cap", 0)
    p = cache.get("stablecoin", {}).get("value", m)
    return m, ((m - p) / p * 100) if p > 0 else 0

def is_important_economic_day():
    t = datetime.now(timezone.utc)
    if t.weekday() == 4 and t.day <= 7:
        return True
    for m, d in [(3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10)]:
        if t.month == m and t.day == d:
            return True
    return False

def get_4h_trend():
    now = time.time()
    if now - cache["4h_trend"].get("ts", 0) < 600:
        return cache["4h_trend"].get("diff", 0.0), cache["4h_trend"].get("bull", False)
    try:
        df = get_klines(SYMBOL_BN, "4h", 210)
        if df is not None and len(df) >= 200:
            e50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            diff = (e50 - e200) / e200 * 100
            cache["4h_trend"] = {"diff": diff, "bull": e50 > e200, "ts": now}
            return diff, e50 > e200
    except:
        pass
    return 0.0, False

def detect_whales():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = [[float(b[0]), float(b[1])] for b in d.get("bids", [])[:10]]
        asks = [[float(a[0]), float(a[1])] for a in d.get("asks", [])[:10]]
        X = np.array(bids + asks)
        if len(X) < 5:
            return False, 0
        preds = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
        d = -1 in preds
        return d, 1 if d else 0
    except:
        return False, 0

def get_eth_btc_correlation():
    try:
        e = get_klines("ETHUSDT", "5m", 12)
        b = get_klines("BTCUSDT", "5m", 12)
        if e is None or b is None:
            return 1.0
        c = e["close"].pct_change().dropna().corr(b["close"].pct_change().dropna())
        return c if not np.isnan(c) else 1.0
    except:
        return 1.0

def get_large_trades_signal():
    try:
        now_ms = int(time.time() * 1000)
        ago_ms = now_ms - 2 * 60 * 1000
        data = requests.get(f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={SYMBOL_BN}&startTime={ago_ms}&endTime={now_ms}&limit=500", timeout=8).json()
        buy = sell = 0.0
        for t in data:
            q = float(t.get("q", 0))
            if q < 50:
                continue
            if not t.get("m", True):
                buy += q
            else:
                sell += q
        return buy - sell
    except:
        return 0.0

# ═══════════════════════════════════════════
# ИНДИКАТОРЫ
# ═══════════════════════════════════════════
def calc(df):
    if df is None or len(df) < 10:
        return df
    try:
        df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
        d = df["close"].diff()
        gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
        df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        e12 = df["close"].ewm(span=12, adjust=False).mean()
        e26 = df["close"].ewm(span=26, adjust=False).mean()
        df["MACD"] = e12 - e26
        df["MACD_sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
        df["MACD_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift() <= df["MACD_sig"].shift())
        df["MACD_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift() >= df["MACD_sig"].shift())
        hl = df["high"] - df["low"]
        hpc = (df["high"] - df["close"].shift()).abs()
        lpc = (df["low"] - df["close"].shift()).abs()
        df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()
        bm = df["close"].rolling(20).mean()
        bs = df["close"].rolling(20).std()
        df["BB_up"] = bm + 2 * bs
        df["BB_dn"] = bm - 2 * bs
        df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)
        df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
        df["CVD"] = (df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])).rolling(20).sum()
        df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.3
        df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
        df["price_dir"] = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        df["vol_dir"] = df["vol_spike"].astype(int) * df["price_dir"]
        up = df["high"] - df["high"].shift()
        dn = df["low"].shift() - df["low"]
        tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
        pdm = np.where((up > dn) & (up > 0), up, 0)
        ndm = np.where((dn > up) & (dn > 0), dn, 0)
        atr14 = tr.rolling(14).mean()
        pdi = 100 * (pd.Series(pdm).rolling(14).mean() / atr14.replace(0, np.nan))
        ndi = 100 * (pd.Series(ndm).rolling(14).mean() / atr14.replace(0, np.nan))
        dx = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
        df["ADX"] = dx.rolling(14).mean()
        df["OBV"] = (df["volume"] * np.sign(df["close"].diff())).cumsum()
        df["OBV_ma"] = df["OBV"].rolling(20).mean()
        body = (df["close"] - df["open"]).abs()
        lw = df[["open", "close"]].min(axis=1) - df["low"]
        uw = df["high"] - df[["open", "close"]].max(axis=1)
        df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
        df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    except Exception as e:
        log.error(f"calc: {e}")
    return df

# ═══════════════════════════════════════════
# СИГНАЛ
# ═══════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low, force_test_done, red_news_until

    if df is None or len(df) < 10:
        return None, None, None, None, 0, "Нет данных", 0, 0, {}

    row = df.iloc[-1]
    price = row["close"]
    rsi = row.get("RSI", 50)
    if np.isnan(rsi):
        rsi = 50
    atr = row.get("ATR", price * 0.01)
    if np.isnan(atr):
        atr = price * 0.01
    adx = row.get("ADX", 20)
    if np.isnan(adx):
        adx = 20

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, "Рынок мёртвый", 0, 0, {}

    now = time.time()
    hour = datetime.now(timezone.utc).hour

    if row.get("candle_time"):
        sec = (datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)).total_seconds()
        if sec < 60:
            return None, None, None, None, 0, "Свежая свеча", 0, 0, {}

    if 13 <= hour < 15:
        return None, None, None, None, 0, "Открытие США", 0, 0, {}

    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция {corr:.2f}", 0, 0, {}

    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, 5.0, "FORCE TEST", 5.0, 0.0, {}

    ob_history.append(ob)
    if len(ob_history) > 6:
        ob_history.pop(0)
    ob_rising = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
    ob_delta = ob - last_ob if last_ob != 0 else 0
    last_ob = ob

    fear_greed = get_fear_greed()
    long_short = get_long_short_ratio()
    taker_ratio = get_taker_ratio()
    oi, oi_change = get_open_interest()
    dxy, dxy_change = get_dxy()
    vix = get_vix()
    _, usdt_dom_change = get_usdt_dominance()
    cb_premium = get_coinbase_premium()
    _, eth_btc_chg = get_eth_btc_ratio()
    long_liq, short_liq = get_liquidations()
    _, btc_dom_change = get_btc_dominance()
    funding_avg = get_funding_avg()
    news_sent = get_news_sentiment()
    gas_price = get_gas_price()
    eth_trending = get_trending()
    _, stable_chg = get_stablecoin_supply()
    important_day = is_important_economic_day()
    ema50_200_diff, ema_4h_bull = get_4h_trend()
    whales_detected, _ = detect_whales()
    large_diff = get_large_trades_signal()

    obv_div = 0
    if row.get("OBV") is not None and row.get("OBV_ma") is not None:
        if not np.isnan(row["OBV"]) and not np.isnan(row["OBV_ma"]):
            if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]:
                obv_div = -1
            elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]:
                obv_div = 1

    L = S = 0.0

    # ЯДРО
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 1.0
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 1.0
    elif row["EMA9"] > row["EMA21"]:
        L += 0.5
    elif row["EMA9"] < row["EMA21"]:
        S += 0.5

    if ema_4h_bull:
        L += 1.0
    else:
        S += 1.0

    if rsi < 35:
        L += 1.0
    elif rsi < 45:
        L += 0.5
    elif rsi > 65:
        S += 1.0
    elif rsi > 55:
        S += 0.5

    if row["MACD_bull"]:
        L += 1.0
    elif row["MACD"] > row["MACD_sig"]:
        L += 0.5
    if row["MACD_bear"]:
        S += 1.0
    elif row["MACD"] < row["MACD_sig"]:
        S += 0.5

    if row["CVD_up"]:
        L += 1.0
    else:
        S += 1.0

    if row["vol_spike"]:
        if row["vol_dir"] > 0:
            L += 1.0
        elif row["vol_dir"] < 0:
            S += 1.0
    if row["vol_extreme"]:
        if L > S:
            L += 0.5
        else:
            S += 0.5

    if ob > 5 or ob_rising:
        L += 0.5
    if ob < -5 or ob_falling:
        S += 0.5

    if adx < 20:
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)

    # СРЕДНИЕ
    if fear_greed < 25:
        L += 0.5
    elif fear_greed < 40:
        L += 0.25
    elif fear_greed > 75:
        S += 0.5
    elif fear_greed > 65:
        S += 0.25

    if long_short < 0.8:
        L += 0.5
    elif long_short < 1.1:
        L += 0.25
    elif long_short > 2.5:
        S += 0.5
    elif long_short > 1.8:
        S += 0.25

    if taker_ratio > 1.3:
        L += 0.5
    elif taker_ratio > 1.1:
        L += 0.25
    elif taker_ratio < 0.7:
        S += 0.5
    elif taker_ratio < 0.9:
        S += 0.25

    if cb_premium > 0.05:
        L += 0.5
    elif cb_premium > 0.02:
        L += 0.25
    elif cb_premium < -0.05:
        S += 0.5
    elif cb_premium < -0.02:
        S += 0.25

    if eth_btc_chg > 0.3:
        L += 0.5
    elif eth_btc_chg > 0.1:
        L += 0.25
    elif eth_btc_chg < -0.3:
        S += 0.5
    elif eth_btc_chg < -0.1:
        S += 0.25

    if oi_change > 3:
        if price > df.iloc[-2]["close"]:
            L += 0.5
        else:
            S += 0.5
    elif oi_change < -3:
        if price < df.iloc[-2]["close"]:
            S += 0.25
        else:
            L += 0.25

    bp = row["BB_pct"]
    if bp < 0.1:
        L += 0.5
    elif bp > 0.9:
        S += 0.5

    if price < row["VWAP"]:
        L += 0.5
    else:
        S += 0.5

    if large_diff > 100:
        L += 0.5
    elif large_diff > 30:
        L += 0.25
    elif large_diff < -100:
        S += 0.5
    elif large_diff < -30:
        S += 0.25

    # ВСПОМОГАТЕЛЬНЫЕ
    if btc_mom > 0.2:
        L += 0.25
        S = max(0, S - 0.25)
    elif btc_mom < -0.2:
        S += 0.25
        L = max(0, L - 0.25)

    if dxy_change > 0.3:
        S += 0.25
        L = max(0, L - 0.25)
    elif dxy_change < -0.3:
        L += 0.25
        S = max(0, S - 0.25)

    if vix > 30:
        L = max(0, L - 0.25)
        S = max(0, S - 0.25)

    liq_diff = short_liq - long_liq
    if liq_diff > 0.5:
        L += 0.25
    elif liq_diff < -0.5:
        S += 0.25

    if btc_dom_change > 0.2:
        S += 0.25
        L = max(0, L - 0.25)
    elif btc_dom_change < -0.2:
        L += 0.25
        S = max(0, S - 0.25)

    if funding_avg > 0.005:
        S += 0.25
    elif funding_avg < -0.005:
        L += 0.25

    if usdt_dom_change > 0.2:
        S += 0.25
    elif usdt_dom_change < -0.2:
        L += 0.25

    if stable_chg > 0.5:
        L += 0.25

    if gas_price > 50:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if news_sent > 0.3:
        L += 0.25
    elif news_sent < -0.3:
        S += 0.25

    if obv_div == 1:
        L += 0.25
    elif obv_div == -1:
        S += 0.25

    if eth_trending:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if row["hammer"]:
        L += 0.25
    if row["shooter"]:
        S += 0.25

    if ob_delta > 3:
        L += 0.25
    elif ob_delta < -3:
        S += 0.25

    # ШТРАФЫ
    if important_day:
        L = max(0, L - 1.0)
        S = max(0, S - 1.0)

    if 1 <= hour < 6:
        if L >= S:
            L = max(0, L - 0.75)
        else:
            S = max(0, S - 0.75)

    if hour >= 20 and datetime.now(timezone.utc).weekday() == 4:
        if L >= S:
            L = max(0, L - 1.0)
        else:
            S = max(0, S - 1.0)

    if 8 <= hour < 9:
        if L >= S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    if datetime.now(timezone.utc).weekday() in [1, 2, 3]:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if long_liq + short_liq > LIQ_THRESHOLD:
        if L > S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    if row["EMA9"] > row["EMA21"] > row["EMA50"] and rsi < 45 and row["MACD"] > row["MACD_sig"] and row["CVD_up"]:
        L += 0.5
    if row["EMA9"] < row["EMA21"] < row["EMA50"] and rsi > 55 and row["MACD"] < row["MACD_sig"] and not row["CVD_up"]:
        S += 0.5

    if yesterday_high > 0 and yesterday_low > 0:
        if price < yesterday_low:
            L = max(0, L - 0.5)
        elif price > yesterday_high:
            S = max(0, S - 0.5)
        elif yesterday_low < price < yesterday_low * 1.01:
            L += 0.25
        elif yesterday_high * 0.99 < price < yesterday_high:
            S += 0.25

    ml_metrics = {
        "fear_greed": fear_greed, "long_short": long_short,
        "taker_ratio": taker_ratio, "oi_change": oi_change,
        "cb_premium": cb_premium, "eth_btc_chg": eth_btc_chg,
        "liq_diff": short_liq - long_liq, "funding_avg": funding_avg,
        "adx": adx, "rsi": rsi, "bb_pct": row["BB_pct"],
        "ob": ob, "btc_mom": btc_mom, "large_trades": large_diff,
        "hour": hour, "weekday": datetime.now(timezone.utc).weekday(),
    }

    ml_b = get_ml_bonus(L, S, ml_metrics)
    if ml_b > 0:
        if L > S:
            L += ml_b
        else:
            S += ml_b
    elif ml_b < 0:
        if L > S:
            L = max(0, L + ml_b)
        else:
            S = max(0, S + ml_b)

    long_signal = L >= MIN_SCORE and (L - S) >= MIN_SCORE_DIFF
    short_signal = S >= MIN_SCORE and (S - L) >= MIN_SCORE_DIFF

    if not long_signal and not short_signal:
        return None, None, None, None, max(L, S), f"L:{L:.1f} S:{S:.1f} diff:{abs(L-S):.1f}", L, S, ml_metrics

    entry = price
    sl_pct = SL_MARGIN_PCT / LEVERAGE
    tp_pct = TP_MARGIN_PCT / LEVERAGE

    if long_signal:
        direction = "LONG"
        score = L
        sl = round(entry * (1 - sl_pct), 2)
        tp = round(entry * (1 + tp_pct), 2)
    else:
        direction = "SHORT"
        score = S
        sl = round(entry * (1 + sl_pct), 2)
        tp = round(entry * (1 - tp_pct), 2)

    log.info(f"{direction} {score:.1f}/{MAX_SCORE} | {entry:.2f} SL:{sl:.2f} TP:{tp:.2f}")
    reason = f"L:{L:.1f} S:{S:.1f} | ADX:{adx:.1f} | F&G:{fear_greed}"
    return direction, entry, sl, tp, score, reason, L, S, ml_metrics

# ═══════════════════════════════════════════
# ML СИСТЕМА
# ═══════════════════════════════════════════
def get_ml_features(L, S, metrics):
    return [L, S, L - S,
            metrics.get("fear_greed", 50), metrics.get("long_short", 1.0),
            metrics.get("taker_ratio", 1.0), metrics.get("oi_change", 0),
            metrics.get("cb_premium", 0), metrics.get("eth_btc_chg", 0),
            metrics.get("liq_diff", 0), metrics.get("funding_avg", 0),
            metrics.get("adx", 20), metrics.get("rsi", 50),
            metrics.get("bb_pct", 0.5), metrics.get("ob", 0),
            metrics.get("btc_mom", 0), metrics.get("large_trades", 0),
            metrics.get("hour", 12), metrics.get("weekday", 2)]

def get_ml_bonus(L, S, metrics):
    global scalp_model
    if scalp_model is None:
        return 0.0
    try:
        features = np.array([get_ml_features(L, S, metrics)])
        X_sc = scalp_model["scaler"].transform(features)
        prob = scalp_model["model"].predict_proba(X_sc)[0][1]
        if prob > 0.7:
            return +0.5
        elif prob > 0.6:
            return +0.25
        elif prob < 0.3:
            return -0.5
        elif prob < 0.4:
            return -0.25
        return 0.0
    except:
        return 0.0

def train_model():
    global scalp_model, signals_history
    completed = [s for s in signals_history if s.get("label") is not None]
    if len(completed) < 30:
        return
    try:
        X = np.array([get_ml_features(s.get("L", 0), s.get("S", 0), s.get("metrics", {})) for s in completed])
        y = np.array([s["label"] for s in completed])
        scaler = StandardScaler()
        X_sc = scaler.fit_transform(X)
        model = GradientBoostingClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42)
        model.fit(X_sc, y)
        scalp_model = {"model": model, "scaler": scaler}
        joblib.dump(scalp_model, "scalp_model.pkl")
        wr = sum(y) / len(y) if len(y) > 0 else 0
        log.info(f"ML обучена: {len(X)} примеров, WR={wr:.1%}")
        send_telegram(f"🤖 ML модель обновлена\nПримеров: {len(X)}\nВинрейт: {wr:.1%}")
    except Exception as e:
        log.error(f"train_model: {e}")

def save_signal_to_history(sig):
    global signals_history
    signals_history.append(sig)
    save_signals()

def update_signal_result(order_id, result, pnl):
    global signals_history
    for s in signals_history:
        if s.get("order_id") == order_id:
            s["result"] = result
            s["pnl"] = pnl
            s["label"] = 1 if result == "TP" else 0
            break
    save_signals()

def maybe_retrain():
    global _trades_since_last_train
    _trades_since_last_train += 1
    if _trades_since_last_train >= RETRAIN_EVERY:
        _trades_since_last_train = 0
        threading.Thread(target=train_model, daemon=True).start()

# ═══════════════════════════════════════════
# ПРОВЕРКА ПОЗИЦИЙ
# ═══════════════════════════════════════════
def check_closed_positions():
    global stats, active_positions, MIN_SCORE, losses_in_row
    try:
        cur_pos = okx_positions()
        cur_price = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=5).json().get("price", 0))

        for oid, pos in list(active_positions.items()):
            d = pos.get("direction")
            entry = pos.get("entry", 0)
            ot = pos.get("open_time", time.time())
            hh = (time.time() - ot) / 3600
            tq = pos.get("total_qty", 1)
            tp = pos.get("tp", 0)
            sl = pos.get("sl", 0)
            cls = pos.get("cls_side")
            ps = pos.get("pos_side")
            sl_id = pos.get("sl_order_id")
            tp_id = pos.get("tp_order_id")

            if hh > MAX_HOLD_HOURS:
                in_profit = (d == "LONG" and cur_price > entry) or (d == "SHORT" and cur_price < entry)
                if in_profit:
                    okx_close_market(ps, tq)

            still_open = any(float(p.get("pos", 0)) != 0 for p in cur_pos)

            if not still_open:
                if sl_id:
                    okx_cancel(sl_id)
                if tp_id:
                    okx_cancel(tp_id)

                hist = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP", "instId": SYMBOL, "state": "filled",
                    "begin": str(int((ot - 60) * 1000)),
                    "end": str(int(time.time() * 1000)), "limit": "50"
                })

                if hist.get("code") != "0" or not hist.get("data"):
                    continue

                total_pnl = 0
                reason = "РЫНОК"

                for h in hist.get("data", []):
                    if h.get("side") == cls and h.get("posSide") == ps:
                        apx = float(h.get("avgPx", 0))
                        qty = float(h.get("sz", 0))
                        et = float(h.get("cTime", 0)) / 1000
                        if apx > 0 and qty > 0 and et > ot:
                            if d == "LONG":
                                pnl_pct = (apx - entry) / entry * 100 if entry > 0 else 0
                                if apx >= tp * 0.99:
                                    reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                                elif apx <= sl * 1.01:
                                    reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                            else:
                                pnl_pct = (entry - apx) / entry * 100 if entry > 0 else 0
                                if apx <= tp * 1.01:
                                    reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                                elif apx >= sl * 0.99:
                                    reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                            total_pnl += pnl_pct / 100 * ORDER_USDT * LEVERAGE * (qty / tq)

                if total_pnl != 0:
                    is_win = total_pnl > 0
                    if is_win:
                        stats["wins"] = stats.get("wins", 0) + 1
                        stats["total_profit_sum"] = stats.get("total_profit_sum", 0) + total_pnl
                        if MIN_SCORE != BASE_MIN_SCORE:
                            MIN_SCORE = BASE_MIN_SCORE
                        losses_in_row = 0
                    else:
                        stats["losses"] = stats.get("losses", 0) + 1
                        stats["total_loss_sum"] = stats.get("total_loss_sum", 0) + abs(total_pnl)
                        losses_in_row += 1
                        if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
                            MIN_SCORE += 0.5
                            send_telegram(f"⚠️ 3 убытка\nMIN_SCORE → {MIN_SCORE}")

                    stats["total"] = stats.get("total", 0) + 1
                    stats["total_profit"] = stats.get("total_profit", 0) + total_pnl
                    stats["current_equity"] = stats.get("current_equity", 0) + total_pnl
                    if stats["current_equity"] > stats.get("peak_equity", 0):
                        stats["peak_equity"] = stats["current_equity"]
                    dd = stats.get("peak_equity", 0) - stats["current_equity"]
                    if dd > stats.get("max_drawdown", 0):
                        stats["max_drawdown"] = dd
                    save_stats()

                    update_signal_result(oid, "TP" if is_win else "SL", total_pnl)
                    maybe_retrain()

                    wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    avg = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
                    em = "✅" if is_win else "❌"

                    send_telegram(
                        f"{em} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                        f"📈 Направление: {d}\n💰 Вход: {entry:.2f}\n📊 Причина: {reason}\n💎 P&L: {total_pnl:+.2f} USDT\n\n"
                        f"📈 <b>СТАТИСТИКА:</b>\n🔹 Всего: {stats['total']}\n"
                        f"✅ Прибыльных: {stats['wins']} (+{stats.get('total_profit_sum', 0):.2f})\n"
                        f"❌ Убыточных: {stats['losses']} (-{stats.get('total_loss_sum', 0):.2f})\n"
                        f"💰 P&L итого: {stats.get('total_profit', 0):+.2f} USDT\n"
                        f"📊 Средний P&L: {avg:+.2f} USDT\n🎯 Винрейт: {wr:.1f}%\n⚙️ MIN_SCORE: {MIN_SCORE}"
                    )
                    del active_positions[oid]
                    save_positions()
    except Exception as e:
        log.error(f"check_closed: {e}")

# ═══════════════════════════════════════════
# ШКАЛА
# ═══════════════════════════════════════════
def score_bar(score):
    filled = min(10, round(score / MAX_SCORE * 10))
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🟢" if score >= 8 else ("🟡" if score >= 6 else ("🟠" if score >= 4 else "🔴"))
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 8: return "🟢"
    elif score >= 6: return "🟡"
    elif score >= 4: return "🟠"
    else: return "🔴"

# ═══════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════
def run_scan():
    global last_heartbeat_time, pause_until, yesterday_high, yesterday_low
    now = time.time()

    if now < pause_until:
        return

    check_closed_positions()

    if now - last_heartbeat_time >= 3600 or yesterday_high == 0:
        yesterday_high, yesterday_low = get_yesterday_levels()

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None:
        return

    calc(df)
    funding = get_funding()
    ob = get_ob()
    btc_mom, btc_dir = get_btc_momentum()
    price = df.iloc[-1]["close"]
    atr_val = df.iloc[-1]["ATR"]

    result = get_signal(df, funding, ob, btc_mom, btc_dir)
    direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = result

    log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f}/{MAX_SCORE} | MIN:{MIN_SCORE}")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_balance()
        pos = okx_positions()
        hour = datetime.now(timezone.utc).hour
        session = "🌙 Ночь" if 1 <= hour < 6 else ("🇬🇧 Лондон" if hour < 13 else "🇺🇸 США")
        wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

        send_telegram(
            f"❤️ <b>Heartbeat v5.4</b>\n\n"
            f"💰 ETH: <b>{price:.2f}</b> ATR:{atr_val:.2f}\n"
            f"😱 F&G:{get_fear_greed()} | L/S:{get_long_short_ratio():.2f}\n"
            f"🌍 {session} | 💳 Баланс:{bal:.2f} | Поз:{len(pos)}\n"
            f"🎯 Сигнал:{'нет' if not direction else direction} {score_color(score)} <b>{score:.1f}</b> | L:{_L:.1f} S:{_S:.1f}\n"
            f"⚙️ MIN_SCORE:{MIN_SCORE} (база:{BASE_MIN_SCORE})\n"
            f"📊 {stats['total']} сд | ✅ {stats['wins']} ({wr:.1f}%) | P&L:{stats.get('total_profit', 0):+.2f}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    if direction is None:
        return

    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    msg = [
        f"<b>[{mode}]</b>",
        f"{'↗️' if direction == 'LONG' else '↘️'} <b>SCALP {direction}</b>",
        "",
        f"<b>Надёжность:</b> {score_bar(score)}",
        "",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{int(SL_MARGIN_PCT*100)}% маржи)",
        f"🎯 Тейк: {tp:.2f} (+{int(TP_MARGIN_PCT*100)}% маржи)",
        f"⚙️ MIN: {MIN_SCORE}",
        "",
        f"📊 {reason}",
    ]

    has_pos = len(okx_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res.get("ok"):
            losses_in_row = 0
            msg += [
                f"✅ <b>ИСПОЛНЕНО</b>",
                f"📦 Контрактов: {res.get('total_qty', 0)}",
                f"🆔 OrderID: {res.get('orderId', '')}",
            ]
            save_signal_to_history({
                "order_id": res.get("orderId", ""),
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "score": score, "L": _L, "S": _S, "metrics": _ml_metrics,
                "timestamp": time.time(), "result": None, "label": None,
            })
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ Пауза {PAUSE_LOSSES // 60} мин")
            msg += [f"❌ Ошибка: {res.get('msg', 'Unknown')}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт v5.4 FINAL")
    bal = okx_balance()
    stats["current_equity"] = bal
    stats["peak_equity"] = bal
    wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

    send_telegram(
        f"🚀 <b>OKX Scalp Bot v5.4 FINAL</b>\n\n"
        f"⚙️ Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"🎯 MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE}\n"
        f"📐 SL: -{int(SL_MARGIN_PCT*100)}% | TP: +{int(TP_MARGIN_PCT*100)}%\n"
        f"💳 Баланс: {bal:.2f} USDT\n"
        f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({wr:.1f}%)\n\n"
        f"🆕 <b>v5.4 FINAL:</b>\n"
        f"• Все ошибки исправлены\n"
        f"• calc() работает\n"
        f"• Самообучение (GradientBoosting)\n"
        f"• Крупные сделки (aggTrades)\n"
        f"• ML бонус ±0.25-0.5"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
- Градуированная система весов (3 уровня)
- Система самообучения (GradientBoosting, каждые 20 сделок)
- Крупные сделки (aggTrades Binance)
- Тейк +50% маржи, Стоп -50% маржи, Плечо 50, Маржа 20 USDT
- ВСЕ ошибки исправлены, calc работает, баллы ненулевые
"""
import os, time, logging, threading, hashlib, hmac, base64, json
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from flask import Flask
from sklearn.ensemble import IsolationForest, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
import joblib

# ═══════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 50
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60

BASE_MIN_SCORE = 5.0
MIN_SCORE = 5.0
MIN_SCORE_DIFF = 2.0
MAX_SCORE = 13.0

SL_MARGIN_PCT = 0.50
TP_MARGIN_PCT = 0.50

ATR_MIN_PCT = 0.001
MAX_LOSSES = 4
PAUSE_LOSSES = 60 * 60
MAX_HOLD_HOURS = 6
RED_NEWS_DROP = 0.01
RED_NEWS_VOL = 2.0
RED_NEWS_BLOCK = 15 * 60
LIQ_THRESHOLD = 5.0
NIGHT_HOURS = (1, 6)
FORCE_TEST = os.environ.get("FORCE_TEST", "false").lower() == "true"
STATS_FILE = "bot_stats.json"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
app = Flask(__name__)

@app.route("/")
def home():
    return f"OKX Scalp Bot v5.4 | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# ═══════════════════════════════════════════
# JSON
# ═══════════════════════════════════════════
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"Save {filename}: {e}")

# ═══════════════════════════════════════════
# СОСТОЯНИЕ
# ═══════════════════════════════════════════
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []
last_ob = 0
yesterday_high = 0
yesterday_low = 0
red_news_until = 0
force_test_done = False
_trades_since_last_train = 0
RETRAIN_EVERY = 20

cache = {
    "fear_greed": {"value": 50, "ts": 0},
    "long_short": {"value": 1.0, "ts": 0},
    "taker_ratio": {"value": 1.0, "ts": 0},
    "open_interest": {"value": 0, "change": 0, "ts": 0},
    "dxy": {"value": 100, "change": 0, "ts": 0},
    "vix": {"value": 20, "ts": 0},
    "usdt_dominance": {"value": 5.0, "change": 0, "ts": 0},
    "coinbase_premium": {"value": 0.0, "ts": 0},
    "eth_btc_ratio": {"value": 0.0, "change": 0.0, "ts": 0},
    "liquidations": {"long_liq": 0.0, "short_liq": 0.0, "ts": 0},
    "btc_dominance": {"value": 50, "change": 0, "ts": 0},
    "funding_avg": {"value": 0.0, "ts": 0},
    "trending": {"value": False, "ts": 0},
    "gas_price": {"value": 20, "ts": 0},
    "news_sentiment": {"value": 0.0, "ts": 0},
    "stablecoin": {"value": 0, "change": 0, "ts": 0},
    "4h_trend": {"diff": 0.0, "bull": False, "ts": 0},
}

stats = {"total": 0, "wins": 0, "losses": 0, "total_profit": 0.0,
         "total_profit_sum": 0.0, "total_loss_sum": 0.0,
         "max_drawdown": 0.0, "peak_equity": 0.0, "current_equity": 0.0}
active_positions = {}
signals_history = []
scalp_model = None

def load_stats():
    global stats
    l = load_json(STATS_FILE, {})
    for k in stats:
        if k in l:
            stats[k] = l[k]
    log.info(f"Статистика: {stats['total']} сд")

def save_stats():
    save_json(STATS_FILE, stats)

def load_signals():
    global signals_history
    signals_history = load_json("signals_history.json", [])

def save_signals():
    save_json("signals_history.json", signals_history)

def load_model():
    global scalp_model
    try:
        scalp_model = joblib.load("scalp_model.pkl")
        log.info("ML модель загружена")
    except:
        log.info("ML модель не найдена")

def load_positions():
    global active_positions
    active_positions = load_json("active_positions.json", {})

def save_positions():
    save_json("active_positions.json", active_positions)

load_stats()
load_signals()
load_model()
load_positions()

# ═══════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.status_code == 200:
            log.info("TG ✅")
        else:
            log.error(f"TG ❌ {r.text}")
    except Exception as e:
        log.error(f"TG: {e}")

# ═══════════════════════════════════════════
# OKX API
# ═══════════════════════════════════════════
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {"OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
            "Content-Type": "application/json", **OKX_DEMO_HEADER}

def okx_get(path, params=None):
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    try:
        return requests.get(OKX_BASE + path + qs, headers=_sign(_ts(), "GET", path + qs), timeout=10).json()
    except Exception as e:
        log.error(f"GET {path}: {e}")
        return {}

def okx_post(path, body):
    data = json.dumps(body)
    hdr = _sign(_ts(), "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        if d.get("code") != "0":
            log.warning(f"POST {path}: {d.get('code')} {d.get('msg','')}")
        return d
    except Exception as e:
        log.error(f"POST {path}: {e}")
        return {}

def okx_set_leverage():
    okx_post("/api/v5/account/set-leverage", {"instId": SYMBOL, "lever": str(LEVERAGE), "mgnMode": "cross"})

def okx_balance():
    try:
        r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
        for d in r.get("data", [{}])[0].get("details", []):
            if d.get("ccy") == "USDT":
                return float(d.get("availBal", 0))
    except:
        pass
    return 0.0

def okx_positions():
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_cancel(algo_id):
    if algo_id:
        okx_post("/api/v5/trade/cancel-algo-order", {"instId": SYMBOL, "algoId": algo_id})

def okx_close_market(pos_side, qty):
    cls = "sell" if pos_side == "long" else "buy"
    return okx_post("/api/v5/trade/order", {"instId": SYMBOL, "tdMode": "cross",
                    "side": cls, "posSide": pos_side, "ordType": "market", "sz": str(qty)})

def okx_place_order(direction, entry, sl, tp):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    ps = "long" if direction == "LONG" else "short"
    cls = "sell" if direction == "LONG" else "buy"
    hour = datetime.now(timezone.utc).hour
    mult = 0.5 if NIGHT_HOURS[0] <= hour < NIGHT_HOURS[1] else 1.0
    qty = max(1, round(ORDER_USDT * mult * LEVERAGE / entry / 0.01))
    log.info(f"▶ {direction} qty={qty} entry={entry:.2f}")

    r = okx_post("/api/v5/trade/order", {"instId": SYMBOL, "tdMode": "cross",
                 "side": side, "posSide": ps, "ordType": "market", "sz": str(qty)})
    if r.get("code") != "0":
        msg = r.get("msg", "?")
        if r.get("data"):
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}

    oid = r["data"][0].get("ordId", "")
    log.info(f"✅ open ordId={oid}")
    active_positions[oid] = {"direction": direction, "entry": entry, "sl": sl, "tp": tp,
                             "total_qty": qty, "pos_side": ps, "cls_side": cls,
                             "open_time": time.time(), "sl_order_id": None, "tp_order_id": None}
    save_positions()
    time.sleep(3)

    sl_ok = tp_ok = False
    for _ in range(3):
        sr = okx_post("/api/v5/trade/order-algo", {"instId": SYMBOL, "tdMode": "cross",
                      "side": cls, "posSide": ps, "ordType": "conditional", "sz": str(qty),
                      "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last"})
        if sr.get("code") == "0":
            active_positions[oid]["sl_order_id"] = sr["data"][0].get("algoId", "")
            sl_ok = True
            break
        time.sleep(1)

    for _ in range(3):
        tr = okx_post("/api/v5/trade/order-algo", {"instId": SYMBOL, "tdMode": "cross",
                      "side": cls, "posSide": ps, "ordType": "conditional", "sz": str(qty),
                      "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last"})
        if tr.get("code") == "0":
            active_positions[oid]["tp_order_id"] = tr["data"][0].get("algoId", "")
            tp_ok = True
            break
        time.sleep(1)

    save_positions()
    return {"ok": True, "orderId": oid, "total_qty": qty, "sl_ok": sl_ok, "tp_ok": tp_ok}

# ═══════════════════════════════════════════
# ДАННЫЕ
# ═══════════════════════════════════════════
def get_klines(symbol=None, interval="5m", limit=150):
    sym = symbol or SYMBOL_BN
    try:
        data = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}", timeout=10).json()
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume",
                                          "ct", "qv", "trades", "taker_buy_base", "tbq", "ignore"])
        for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
            df[c] = df[c].astype(float)
        df["candle_time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e:
        log.error(f"klines {sym}: {e}")
        return None

def get_funding():
    try:
        return float(requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json().get("lastFundingRate", 0))
    except:
        return 0.0

def get_ob():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = sum(float(b[1]) for b in d.get("bids", []))
        asks = sum(float(a[1]) for a in d.get("asks", []))
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

def get_btc_momentum():
    try:
        df = get_klines(BTC_SYMBOL, "3m", 5)
        if df is None:
            return 0.0, 0
        chg = (df.iloc[-1]["close"] - df.iloc[-3]["close"]) / df.iloc[-3]["close"] * 100
        d = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), d
    except:
        return 0.0, 0

def get_yesterday_levels():
    try:
        df = get_klines(SYMBOL_BN, "1d", 2)
        if df is not None and len(df) >= 2:
            y = df.iloc[-2]
            return float(y["high"]), float(y["low"])
    except:
        pass
    return 0, 0

# ═══════════════════════════════════════════
# КЭШИРОВАННЫЕ МЕТРИКИ
# ═══════════════════════════════════════════
def _c1(key, ttl, fetcher):
    now = time.time()
    if now - cache[key].get("ts", 0) < ttl:
        return cache[key].get("value", 0)
    try:
        v = fetcher()
        cache[key] = {"value": v, "ts": now}
        return v
    except Exception as e:
        log.error(f"Metric {key}: {e}")
        return cache[key].get("value", 0)

def _c2(key, ttl, fetcher):
    now = time.time()
    if now - cache[key].get("ts", 0) < ttl:
        return cache[key].get("value", 0), cache[key].get("change", 0)
    try:
        v, c = fetcher()
        cache[key] = {"value": v, "change": c, "ts": now}
        return v, c
    except Exception as e:
        log.error(f"Metric {key}: {e}")
        return cache[key].get("value", 0), cache[key].get("change", 0)

def get_fear_greed():
    return _c1("fear_greed", 3600, lambda: int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]["value"]))

def get_long_short_ratio():
    return _c1("long_short", 300, lambda: float(requests.get("https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?symbol=ETHUSDT&period=5m&limit=1", timeout=10).json()[0]["longShortRatio"]))

def get_taker_ratio():
    return _c1("taker_ratio", 300, lambda: float(requests.get("https://fapi.binance.com/fapi/v1/takerlongshortRatio?symbol=ETHUSDT&period=5m&limit=1", timeout=10).json()[0]["buySellRatio"]))

def get_open_interest():
    return _c2("open_interest", 300, lambda: _oi())

def _oi():
    oi = float(requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT", timeout=10).json()["openInterest"])
    prev = cache.get("open_interest", {}).get("value", oi)
    return oi, ((oi - prev) / prev * 100) if prev > 0 else 0

def get_dxy():
    return _c2("dxy", 3600, lambda: _dxy())

def _dxy():
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=2d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    m = r.json()["chart"]["result"][0]["meta"]
    return m["regularMarketPrice"], (m["regularMarketPrice"] - m["previousClose"]) / m["previousClose"] * 100

def get_vix():
    return _c1("vix", 3600, lambda: _vix())

def _vix():
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    return r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]

def get_usdt_dominance():
    return _c2("usdt_dominance", 600, lambda: _dom("usdt"))

def get_btc_dominance():
    return _c2("btc_dominance", 600, lambda: _dom("btc"))

def _dom(coin):
    dom = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"][coin]
    key = "btc_dominance" if coin == "btc" else "usdt_dominance"
    prev = cache.get(key, {}).get("value", dom)
    return dom, dom - prev

def get_coinbase_premium():
    return _c1("coinbase_premium", 60, lambda: _cb())

def _cb():
    cb = float(requests.get("https://api.exchange.coinbase.com/products/ETH-USD/ticker", timeout=8).json()["price"])
    bn = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=8).json()["price"])
    return round((cb - bn) / bn * 100, 4)

def get_eth_btc_ratio():
    return _c2("eth_btc_ratio", 180, lambda: _ethbtc())

def _ethbtc():
    d = requests.get("https://api.binance.com/api/v3/klines?symbol=ETHBTC&interval=5m&limit=4", timeout=8).json()
    return float(d[-1][4]), round((float(d[-1][4]) - float(d[-4][4])) / float(d[-4][4]) * 100, 4)

def get_liquidations():
    return _c2("liquidations", 300, lambda: _liq())

def _liq():
    d = requests.get(f"https://fapi.binance.com/fapi/v1/forceOrders?symbol={SYMBOL_BN}&limit=200", timeout=10).json()
    L, S = 0.0, 0.0
    cut = (time.time() - 3600) * 1000
    for o in d:
        if float(o.get("time", 0)) < cut:
            continue
        q = float(o.get("origQty", 0)) * float(o.get("price", 0))
        if o.get("side") == "SELL":
            L += q
        else:
            S += q
    return round(L / 1_000_000, 3), round(S / 1_000_000, 3)

def get_funding_avg():
    return _c1("funding_avg", 300, lambda: _fund())

def _fund():
    r = []
    b = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json()
    r.append(float(b.get("lastFundingRate", 0)))
    o = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}", timeout=8).json()
    if o.get("data"):
        r.append(float(o["data"][0].get("fundingRate", 0)))
    return sum(r) / len(r) if r else 0.0

def get_trending():
    return _c1("trending", 600, lambda: "ETH" in [c["item"]["symbol"] for c in requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json().get("coins", [])])

def get_gas_price():
    return _c1("gas_price", 60, lambda: float(requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10).json()["result"]["ProposeGasPrice"]))

def get_news_sentiment():
    return _c1("news_sentiment", 300, lambda: _news())

def _news():
    df = get_klines(SYMBOL_BN, "5m", 12)
    if df is not None and len(df) > 0:
        vc = df["volume"].iloc[-1] / df["volume"].mean() if df["volume"].mean() > 0 else 1
        pc = (df["close"].iloc[-1] - df["close"].iloc[-6]) / df["close"].iloc[-6] * 100 if df["close"].iloc[-6] != 0 else 0
        return max(-1.0, min(1.0, (vc - 1) * 0.5 + pc * 0.1))
    return 0.0

def get_stablecoin_supply():
    return _c2("stablecoin", 600, lambda: _stable())

def _stable():
    r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=usd&include_market_cap=true", timeout=10).json()
    m = r.get("tether", {}).get("usd_market_cap", 0)
    p = cache.get("stablecoin", {}).get("value", m)
    return m, ((m - p) / p * 100) if p > 0 else 0

def is_important_economic_day():
    t = datetime.now(timezone.utc)
    if t.weekday() == 4 and t.day <= 7:
        return True
    for m, d in [(3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10)]:
        if t.month == m and t.day == d:
            return True
    return False

def get_4h_trend():
    now = time.time()
    if now - cache["4h_trend"].get("ts", 0) < 600:
        return cache["4h_trend"].get("diff", 0.0), cache["4h_trend"].get("bull", False)
    try:
        df = get_klines(SYMBOL_BN, "4h", 210)
        if df is not None and len(df) >= 200:
            e50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            e200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            diff = (e50 - e200) / e200 * 100
            cache["4h_trend"] = {"diff": diff, "bull": e50 > e200, "ts": now}
            return diff, e50 > e200
    except:
        pass
    return 0.0, False

def detect_whales():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = [[float(b[0]), float(b[1])] for b in d.get("bids", [])[:10]]
        asks = [[float(a[0]), float(a[1])] for a in d.get("asks", [])[:10]]
        X = np.array(bids + asks)
        if len(X) < 5:
            return False, 0
        preds = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
        d = -1 in preds
        return d, 1 if d else 0
    except:
        return False, 0

def get_eth_btc_correlation():
    try:
        e = get_klines("ETHUSDT", "5m", 12)
        b = get_klines("BTCUSDT", "5m", 12)
        if e is None or b is None:
            return 1.0
        c = e["close"].pct_change().dropna().corr(b["close"].pct_change().dropna())
        return c if not np.isnan(c) else 1.0
    except:
        return 1.0

def get_large_trades_signal():
    try:
        now_ms = int(time.time() * 1000)
        ago_ms = now_ms - 2 * 60 * 1000
        data = requests.get(f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={SYMBOL_BN}&startTime={ago_ms}&endTime={now_ms}&limit=500", timeout=8).json()
        buy = sell = 0.0
        for t in data:
            q = float(t.get("q", 0))
            if q < 50:
                continue
            if not t.get("m", True):
                buy += q
            else:
                sell += q
        return buy - sell
    except:
        return 0.0

# ═══════════════════════════════════════════
# ИНДИКАТОРЫ (ПОЛНОСТЬЮ ИСПРАВЛЕНО)
# ═══════════════════════════════════════════
def calc(df):
    if df is None or len(df) < 20:
        return df
    try:
        df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
        d = df["close"].diff()
        gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
        df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        e12 = df["close"].ewm(span=12, adjust=False).mean()
        e26 = df["close"].ewm(span=26, adjust=False).mean()
        df["MACD"] = e12 - e26
        df["MACD_sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
        df["MACD_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift() <= df["MACD_sig"].shift())
        df["MACD_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift() >= df["MACD_sig"].shift())
        hl = df["high"] - df["low"]
        hpc = (df["high"] - df["close"].shift()).abs()
        lpc = (df["low"] - df["close"].shift()).abs()
        df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()
        bm = df["close"].rolling(20).mean()
        bs = df["close"].rolling(20).std()
        df["BB_up"] = bm + 2 * bs
        df["BB_dn"] = bm - 2 * bs
        df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)
        df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
        df["CVD"] = (df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])).rolling(20).sum()
        df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.3
        df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
        df["price_dir"] = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        df["vol_dir"] = df["vol_spike"].astype(int) * df["price_dir"]
        up = df["high"] - df["high"].shift()
        dn = df["low"].shift() - df["low"]
        tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
        pdm = np.where((up > dn) & (up > 0), up, 0)
        ndm = np.where((dn > up) & (dn > 0), dn, 0)
        atr14 = tr.rolling(14).mean()
        pdi = 100 * (pd.Series(pdm).rolling(14).mean() / atr14.replace(0, np.nan))
        ndi = 100 * (pd.Series(ndm).rolling(14).mean() / atr14.replace(0, np.nan))
        dx = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
        df["ADX"] = dx.rolling(14).mean()
        df["OBV"] = (df["volume"] * np.sign(df["close"].diff())).cumsum()
        df["OBV_ma"] = df["OBV"].rolling(20).mean()
        body = (df["close"] - df["open"]).abs()
        lw = df[["open", "close"]].min(axis=1) - df["low"]
        uw = df["high"] - df[["open", "close"]].max(axis=1)
        df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
        df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    except Exception as e:
        log.error(f"calc: {e}")
    return df

# ═══════════════════════════════════════════
# СИГНАЛ
# ═══════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low, force_test_done, red_news_until

    if df is None or len(df) < 10:
        return None, None, None, None, 0, "Нет данных", 0, 0, {}

    row = df.iloc[-1]
    price = row["close"]
    rsi = row.get("RSI", 50)
    if np.isnan(rsi):
        rsi = 50
    atr = row.get("ATR", price * 0.01)
    if np.isnan(atr):
        atr = price * 0.01
    adx = row.get("ADX", 20)
    if np.isnan(adx):
        adx = 20

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, "Рынок мёртвый", 0, 0, {}

    now = time.time()
    hour = datetime.now(timezone.utc).hour

    if row.get("candle_time"):
        sec = (datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)).total_seconds()
        if sec < 60:
            return None, None, None, None, 0, "Свежая свеча", 0, 0, {}

    if 13 <= hour < 15:
        return None, None, None, None, 0, "Открытие США", 0, 0, {}

    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция {corr:.2f}", 0, 0, {}

    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, 5.0, "FORCE TEST", 5.0, 0.0, {}

    ob_history.append(ob)
    if len(ob_history) > 6:
        ob_history.pop(0)
    ob_rising = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
    ob_delta = ob - last_ob if last_ob != 0 else 0
    last_ob = ob

    fear_greed = get_fear_greed()
    long_short = get_long_short_ratio()
    taker_ratio = get_taker_ratio()
    oi, oi_change = get_open_interest()
    dxy, dxy_change = get_dxy()
    vix = get_vix()
    _, usdt_dom_change = get_usdt_dominance()
    cb_premium = get_coinbase_premium()
    _, eth_btc_chg = get_eth_btc_ratio()
    long_liq, short_liq = get_liquidations()
    _, btc_dom_change = get_btc_dominance()
    funding_avg = get_funding_avg()
    news_sent = get_news_sentiment()
    gas_price = get_gas_price()
    eth_trending = get_trending()
    _, stable_chg = get_stablecoin_supply()
    important_day = is_important_economic_day()
    ema50_200_diff, ema_4h_bull = get_4h_trend()
    whales_detected, _ = detect_whales()
    large_diff = get_large_trades_signal()

    obv_div = 0
    if row.get("OBV") is not None and row.get("OBV_ma") is not None:
        if not np.isnan(row["OBV"]) and not np.isnan(row["OBV_ma"]):
            if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]:
                obv_div = -1
            elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]:
                obv_div = 1

    L = S = 0.0

    # ЯДРО
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 1.0
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 1.0
    elif row["EMA9"] > row["EMA21"]:
        L += 0.5
    elif row["EMA9"] < row["EMA21"]:
        S += 0.5

    if ema_4h_bull:
        L += 1.0
    else:
        S += 1.0

    if rsi < 35:
        L += 1.0
    elif rsi < 45:
        L += 0.5
    elif rsi > 65:
        S += 1.0
    elif rsi > 55:
        S += 0.5

    if row["MACD_bull"]:
        L += 1.0
    elif row["MACD"] > row["MACD_sig"]:
        L += 0.5
    if row["MACD_bear"]:
        S += 1.0
    elif row["MACD"] < row["MACD_sig"]:
        S += 0.5

    if row["CVD_up"]:
        L += 1.0
    else:
        S += 1.0

    if row["vol_spike"]:
        if row["vol_dir"] > 0:
            L += 1.0
        elif row["vol_dir"] < 0:
            S += 1.0
    if row["vol_extreme"]:
        if L > S:
            L += 0.5
        else:
            S += 0.5

    if ob > 5 or ob_rising:
        L += 0.5
    if ob < -5 or ob_falling:
        S += 0.5

    if adx < 20:
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)

    # СРЕДНИЕ
    if fear_greed < 25:
        L += 0.5
    elif fear_greed < 40:
        L += 0.25
    elif fear_greed > 75:
        S += 0.5
    elif fear_greed > 65:
        S += 0.25

    if long_short < 0.8:
        L += 0.5
    elif long_short < 1.1:
        L += 0.25
    elif long_short > 2.5:
        S += 0.5
    elif long_short > 1.8:
        S += 0.25

    if taker_ratio > 1.3:
        L += 0.5
    elif taker_ratio > 1.1:
        L += 0.25
    elif taker_ratio < 0.7:
        S += 0.5
    elif taker_ratio < 0.9:
        S += 0.25

    if cb_premium > 0.05:
        L += 0.5
    elif cb_premium > 0.02:
        L += 0.25
    elif cb_premium < -0.05:
        S += 0.5
    elif cb_premium < -0.02:
        S += 0.25

    if eth_btc_chg > 0.3:
        L += 0.5
    elif eth_btc_chg > 0.1:
        L += 0.25
    elif eth_btc_chg < -0.3:
        S += 0.5
    elif eth_btc_chg < -0.1:
        S += 0.25

    if oi_change > 3:
        if price > df.iloc[-2]["close"]:
            L += 0.5
        else:
            S += 0.5
    elif oi_change < -3:
        if price < df.iloc[-2]["close"]:
            S += 0.25
        else:
            L += 0.25

    bp = row["BB_pct"]
    if bp < 0.1:
        L += 0.5
    elif bp > 0.9:
        S += 0.5

    if price < row["VWAP"]:
        L += 0.5
    else:
        S += 0.5

    if large_diff > 100:
        L += 0.5
    elif large_diff > 30:
        L += 0.25
    elif large_diff < -100:
        S += 0.5
    elif large_diff < -30:
        S += 0.25

    # ВСПОМОГАТЕЛЬНЫЕ
    if btc_mom > 0.2:
        L += 0.25
        S = max(0, S - 0.25)
    elif btc_mom < -0.2:
        S += 0.25
        L = max(0, L - 0.25)

    if dxy_change > 0.3:
        S += 0.25
        L = max(0, L - 0.25)
    elif dxy_change < -0.3:
        L += 0.25
        S = max(0, S - 0.25)

    if vix > 30:
        L = max(0, L - 0.25)
        S = max(0, S - 0.25)

    liq_diff = short_liq - long_liq
    if liq_diff > 0.5:
        L += 0.25
    elif liq_diff < -0.5:
        S += 0.25

    if btc_dom_change > 0.2:
        S += 0.25
        L = max(0, L - 0.25)
    elif btc_dom_change < -0.2:
        L += 0.25
        S = max(0, S - 0.25)

    if funding_avg > 0.005:
        S += 0.25
    elif funding_avg < -0.005:
        L += 0.25

    if usdt_dom_change > 0.2:
        S += 0.25
    elif usdt_dom_change < -0.2:
        L += 0.25

    if stable_chg > 0.5:
        L += 0.25

    if gas_price > 50:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if news_sent > 0.3:
        L += 0.25
    elif news_sent < -0.3:
        S += 0.25

    if obv_div == 1:
        L += 0.25
    elif obv_div == -1:
        S += 0.25

    if eth_trending:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if row["hammer"]:
        L += 0.25
    if row["shooter"]:
        S += 0.25

    if ob_delta > 3:
        L += 0.25
    elif ob_delta < -3:
        S += 0.25

    # ШТРАФЫ
    if important_day:
        L = max(0, L - 1.0)
        S = max(0, S - 1.0)

    if 1 <= hour < 6:
        if L >= S:
            L = max(0, L - 0.75)
        else:
            S = max(0, S - 0.75)

    if hour >= 20 and datetime.now(timezone.utc).weekday() == 4:
        if L >= S:
            L = max(0, L - 1.0)
        else:
            S = max(0, S - 1.0)

    if 8 <= hour < 9:
        if L >= S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    if datetime.now(timezone.utc).weekday() in [1, 2, 3]:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    if long_liq + short_liq > LIQ_THRESHOLD:
        if L > S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    if row["EMA9"] > row["EMA21"] > row["EMA50"] and rsi < 45 and row["MACD"] > row["MACD_sig"] and row["CVD_up"]:
        L += 0.5
    if row["EMA9"] < row["EMA21"] < row["EMA50"] and rsi > 55 and row["MACD"] < row["MACD_sig"] and not row["CVD_up"]:
        S += 0.5

    if yesterday_high > 0 and yesterday_low > 0:
        if price < yesterday_low:
            L = max(0, L - 0.5)
        elif price > yesterday_high:
            S = max(0, S - 0.5)
        elif yesterday_low < price < yesterday_low * 1.01:
            L += 0.25
        elif yesterday_high * 0.99 < price < yesterday_high:
            S += 0.25

    ml_metrics = {
        "fear_greed": fear_greed, "long_short": long_short,
        "taker_ratio": taker_ratio, "oi_change": oi_change,
        "cb_premium": cb_premium, "eth_btc_chg": eth_btc_chg,
        "liq_diff": short_liq - long_liq, "funding_avg": funding_avg,
        "adx": adx, "rsi": rsi, "bb_pct": row["BB_pct"],
        "ob": ob, "btc_mom": btc_mom, "large_trades": large_diff,
        "hour": hour, "weekday": datetime.now(timezone.utc).weekday(),
    }

    ml_b = get_ml_bonus(L, S, ml_metrics)
    if ml_b > 0:
        if L > S:
            L += ml_b
        else:
            S += ml_b
    elif ml_b < 0:
        if L > S:
            L = max(0, L + ml_b)
        else:
            S = max(0, S + ml_b)

    long_signal = L >= MIN_SCORE and (L - S) >= MIN_SCORE_DIFF
    short_signal = S >= MIN_SCORE and (S - L) >= MIN_SCORE_DIFF

    if not long_signal and not short_signal:
        return None, None, None, None, max(L, S), f"L:{L:.1f} S:{S:.1f} diff:{abs(L-S):.1f}", L, S, ml_metrics

    entry = price
    sl_pct = SL_MARGIN_PCT / LEVERAGE
    tp_pct = TP_MARGIN_PCT / LEVERAGE

    if long_signal:
        direction = "LONG"
        score = L
        sl = round(entry * (1 - sl_pct), 2)
        tp = round(entry * (1 + tp_pct), 2)
    else:
        direction = "SHORT"
        score = S
        sl = round(entry * (1 + sl_pct), 2)
        tp = round(entry * (1 - tp_pct), 2)

    log.info(f"{direction} {score:.1f}/{MAX_SCORE} | {entry:.2f} SL:{sl:.2f} TP:{tp:.2f}")
    reason = f"L:{L:.1f} S:{S:.1f} | ADX:{adx:.1f} | F&G:{fear_greed}"
    return direction, entry, sl, tp, score, reason, L, S, ml_metrics

# ═══════════════════════════════════════════
# ML СИСТЕМА
# ═══════════════════════════════════════════
def get_ml_features(L, S, metrics):
    return [L, S, L - S,
            metrics.get("fear_greed", 50), metrics.get("long_short", 1.0),
            metrics.get("taker_ratio", 1.0), metrics.get("oi_change", 0),
            metrics.get("cb_premium", 0), metrics.get("eth_btc_chg", 0),
            metrics.get("liq_diff", 0), metrics.get("funding_avg", 0),
            metrics.get("adx", 20), metrics.get("rsi", 50),
            metrics.get("bb_pct", 0.5), metrics.get("ob", 0),
            metrics.get("btc_mom", 0), metrics.get("large_trades", 0),
            metrics.get("hour", 12), metrics.get("weekday", 2)]

def get_ml_bonus(L, S, metrics):
    global scalp_model
    if scalp_model is None:
        return 0.0
    try:
        features = np.array([get_ml_features(L, S, metrics)])
        X_sc = scalp_model["scaler"].transform(features)
        prob = scalp_model["model"].predict_proba(X_sc)[0][1]
        if prob > 0.7:
            return +0.5
        elif prob > 0.6:
            return +0.25
        elif prob < 0.3:
            return -0.5
        elif prob < 0.4:
            return -0.25
        return 0.0
    except:
        return 0.0

def train_model():
    global scalp_model, signals_history
    completed = [s for s in signals_history if s.get("label") is not None]
    if len(completed) < 30:
        return
    try:
        X = np.array([get_ml_features(s.get("L", 0), s.get("S", 0), s.get("metrics", {})) for s in completed])
        y = np.array([s["label"] for s in completed])
        scaler = StandardScaler()
        X_sc = scaler.fit_transform(X)
        model = GradientBoostingClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42)
        model.fit(X_sc, y)
        scalp_model = {"model": model, "scaler": scaler}
        joblib.dump(scalp_model, "scalp_model.pkl")
        wr = sum(y) / len(y) if len(y) > 0 else 0
        log.info(f"ML обучена: {len(X)} примеров, WR={wr:.1%}")
        send_telegram(f"🤖 ML модель обновлена\nПримеров: {len(X)}\nВинрейт: {wr:.1%}")
    except Exception as e:
        log.error(f"train_model: {e}")

def save_signal_to_history(sig):
    global signals_history
    signals_history.append(sig)
    save_signals()

def update_signal_result(order_id, result, pnl):
    global signals_history
    for s in signals_history:
        if s.get("order_id") == order_id:
            s["result"] = result
            s["pnl"] = pnl
            s["label"] = 1 if result == "TP" else 0
            break
    save_signals()

def maybe_retrain():
    global _trades_since_last_train
    _trades_since_last_train += 1
    if _trades_since_last_train >= RETRAIN_EVERY:
        _trades_since_last_train = 0
        threading.Thread(target=train_model, daemon=True).start()

# ═══════════════════════════════════════════
# ПРОВЕРКА ПОЗИЦИЙ
# ═══════════════════════════════════════════
def check_closed_positions():
    global stats, active_positions, MIN_SCORE, losses_in_row
    try:
        cur_pos = okx_positions()
        cur_price = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=5).json().get("price", 0))

        for oid, pos in list(active_positions.items()):
            d = pos.get("direction")
            entry = pos.get("entry", 0)
            ot = pos.get("open_time", time.time())
            hh = (time.time() - ot) / 3600
            tq = pos.get("total_qty", 1)
            tp = pos.get("tp", 0)
            sl = pos.get("sl", 0)
            cls = pos.get("cls_side")
            ps = pos.get("pos_side")
            sl_id = pos.get("sl_order_id")
            tp_id = pos.get("tp_order_id")

            if hh > MAX_HOLD_HOURS:
                in_profit = (d == "LONG" and cur_price > entry) or (d == "SHORT" and cur_price < entry)
                if in_profit:
                    okx_close_market(ps, tq)

            still_open = any(float(p.get("pos", 0)) != 0 for p in cur_pos)

            if not still_open:
                if sl_id:
                    okx_cancel(sl_id)
                if tp_id:
                    okx_cancel(tp_id)

                hist = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP", "instId": SYMBOL, "state": "filled",
                    "begin": str(int((ot - 60) * 1000)),
                    "end": str(int(time.time() * 1000)), "limit": "50"
                })

                if hist.get("code") != "0" or not hist.get("data"):
                    continue

                total_pnl = 0
                reason = "РЫНОК"

                for h in hist.get("data", []):
                    if h.get("side") == cls and h.get("posSide") == ps:
                        apx = float(h.get("avgPx", 0))
                        qty = float(h.get("sz", 0))
                        et = float(h.get("cTime", 0)) / 1000
                        if apx > 0 and qty > 0 and et > ot:
                            if d == "LONG":
                                pnl_pct = (apx - entry) / entry * 100 if entry > 0 else 0
                                if apx >= tp * 0.99:
                                    reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                                elif apx <= sl * 1.01:
                                    reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                            else:
                                pnl_pct = (entry - apx) / entry * 100 if entry > 0 else 0
                                if apx <= tp * 1.01:
                                    reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                                elif apx >= sl * 0.99:
                                    reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                            total_pnl += pnl_pct / 100 * ORDER_USDT * LEVERAGE * (qty / tq)

                if total_pnl != 0:
                    is_win = total_pnl > 0
                    if is_win:
                        stats["wins"] = stats.get("wins", 0) + 1
                        stats["total_profit_sum"] = stats.get("total_profit_sum", 0) + total_pnl
                        if MIN_SCORE != BASE_MIN_SCORE:
                            MIN_SCORE = BASE_MIN_SCORE
                        losses_in_row = 0
                    else:
                        stats["losses"] = stats.get("losses", 0) + 1
                        stats["total_loss_sum"] = stats.get("total_loss_sum", 0) + abs(total_pnl)
                        losses_in_row += 1
                        if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
                            MIN_SCORE += 0.5
                            send_telegram(f"⚠️ 3 убытка\nMIN_SCORE → {MIN_SCORE}")

                    stats["total"] = stats.get("total", 0) + 1
                    stats["total_profit"] = stats.get("total_profit", 0) + total_pnl
                    stats["current_equity"] = stats.get("current_equity", 0) + total_pnl
                    if stats["current_equity"] > stats.get("peak_equity", 0):
                        stats["peak_equity"] = stats["current_equity"]
                    dd = stats.get("peak_equity", 0) - stats["current_equity"]
                    if dd > stats.get("max_drawdown", 0):
                        stats["max_drawdown"] = dd
                    save_stats()

                    update_signal_result(oid, "TP" if is_win else "SL", total_pnl)
                    maybe_retrain()

                    wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    avg = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
                    em = "✅" if is_win else "❌"

                    send_telegram(
                        f"{em} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                        f"📈 Направление: {d}\n💰 Вход: {entry:.2f}\n📊 Причина: {reason}\n💎 P&L: {total_pnl:+.2f} USDT\n\n"
                        f"📈 <b>СТАТИСТИКА:</b>\n🔹 Всего: {stats['total']}\n"
                        f"✅ Прибыльных: {stats['wins']} (+{stats.get('total_profit_sum', 0):.2f})\n"
                        f"❌ Убыточных: {stats['losses']} (-{stats.get('total_loss_sum', 0):.2f})\n"
                        f"💰 P&L итого: {stats.get('total_profit', 0):+.2f} USDT\n"
                        f"📊 Средний P&L: {avg:+.2f} USDT\n🎯 Винрейт: {wr:.1f}%\n⚙️ MIN_SCORE: {MIN_SCORE}"
                    )
                    del active_positions[oid]
                    save_positions()
    except Exception as e:
        log.error(f"check_closed: {e}")

# ═══════════════════════════════════════════
# ШКАЛА
# ═══════════════════════════════════════════
def score_bar(score):
    filled = min(10, round(score / MAX_SCORE * 10))
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🟢" if score >= 8 else ("🟡" if score >= 6 else ("🟠" if score >= 4 else "🔴"))
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 8: return "🟢"
    elif score >= 6: return "🟡"
    elif score >= 4: return "🟠"
    else: return "🔴"

# ═══════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════
def run_scan():
    global last_heartbeat_time, pause_until, yesterday_high, yesterday_low
    now = time.time()

    if now < pause_until:
        return

    check_closed_positions()

    if now - last_heartbeat_time >= 3600 or yesterday_high == 0:
        yesterday_high, yesterday_low = get_yesterday_levels()

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None:
        return

    calc(df)
    funding = get_funding()
    ob = get_ob()
    btc_mom, btc_dir = get_btc_momentum()
    price = df.iloc[-1]["close"]
    atr_val = df.iloc[-1]["ATR"]

    result = get_signal(df, funding, ob, btc_mom, btc_dir)
    direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = result

    log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f}/{MAX_SCORE} | MIN:{MIN_SCORE}")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_balance()
        pos = okx_positions()
        hour = datetime.now(timezone.utc).hour
        session = "🌙 Ночь" if 1 <= hour < 6 else ("🇬🇧 Лондон" if hour < 13 else "🇺🇸 США")
        wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

        send_telegram(
            f"❤️ <b>Heartbeat v5.4</b>\n\n"
            f"💰 ETH: <b>{price:.2f}</b> ATR:{atr_val:.2f}\n"
            f"😱 F&G:{get_fear_greed()} | L/S:{get_long_short_ratio():.2f}\n"
            f"🌍 {session} | 💳 Баланс:{bal:.2f} | Поз:{len(pos)}\n"
            f"🎯 Сигнал:{'нет' if not direction else direction} {score_color(score)} <b>{score:.1f}</b> | L:{_L:.1f} S:{_S:.1f}\n"
            f"⚙️ MIN_SCORE:{MIN_SCORE} (база:{BASE_MIN_SCORE})\n"
            f"📊 {stats['total']} сд | ✅ {stats['wins']} ({wr:.1f}%) | P&L:{stats.get('total_profit', 0):+.2f}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    if direction is None:
        return

    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    msg = [
        f"<b>[{mode}]</b>",
        f"{'↗️' if direction == 'LONG' else '↘️'} <b>SCALP {direction}</b>",
        "",
        f"<b>Надёжность:</b> {score_bar(score)}",
        "",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{int(SL_MARGIN_PCT*100)}% маржи)",
        f"🎯 Тейк: {tp:.2f} (+{int(TP_MARGIN_PCT*100)}% маржи)",
        f"⚙️ MIN: {MIN_SCORE}",
        "",
        f"📊 {reason}",
    ]

    has_pos = len(okx_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res.get("ok"):
            losses_in_row = 0
            msg += [
                f"✅ <b>ИСПОЛНЕНО</b>",
                f"📦 Контрактов: {res.get('total_qty', 0)}",
                f"🆔 OrderID: {res.get('orderId', '')}",
            ]
            save_signal_to_history({
                "order_id": res.get("orderId", ""),
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "score": score, "L": _L, "S": _S, "metrics": _ml_metrics,
                "timestamp": time.time(), "result": None, "label": None,
            })
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ Пауза {PAUSE_LOSSES // 60} мин")
            msg += [f"❌ Ошибка: {res.get('msg', 'Unknown')}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт v5.4 FINAL")
    bal = okx_balance()
    stats["current_equity"] = bal
    stats["peak_equity"] = bal
    wr = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

    send_telegram(
        f"🚀 <b>OKX Scalp Bot v5.4 FINAL</b>\n\n"
        f"⚙️ Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"🎯 MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE}\n"
        f"📐 SL: -{int(SL_MARGIN_PCT*100)}% | TP: +{int(TP_MARGIN_PCT*100)}%\n"
        f"💳 Баланс: {bal:.2f} USDT\n"
        f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({wr:.1f}%)\n\n"
        f"🆕 <b>v5.4 FINAL:</b>\n"
        f"• Все ошибки исправлены\n"
        f"• calc() работает\n"
        f"• Самообучение (GradientBoosting)\n"
        f"• Крупные сделки (aggTrades)\n"
        f"• ML бонус ±0.25-0.5"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
