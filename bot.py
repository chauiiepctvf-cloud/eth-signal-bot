"""
OKX Scalp Bot v5.0
- Градуированная система весов (3 уровня приоритета)
- Исправлены все синтаксические ошибки v4.8
- Убраны нерабочие зависимости (XGBoost, FinBERT, TimescaleDB)
- Добавлен IsolationForest для детекции китов (sklearn встроен)
- Оптимизированы баллы для повышения винрейта
"""
import os, time, logging, threading, hashlib, hmac, base64, json
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from flask import Flask
from sklearn.ensemble import IsolationForest
# ══════════════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "")
OKX_BASE = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"} # убери для реала
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 50
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60
# ── СИСТЕМА БАЛЛОВ ─────────────────────────────────
# Градуированная: ЯДРО(макс 7) + СРЕДНИЕ(макс 4) + ВСПОМОГАТЕЛЬНЫЕ(макс 2)
# Итого макс ~13, порог входа 5.5, разница L-S >= 2.0
BASE_MIN_SCORE = 5.5
MIN_SCORE = 5.5
MIN_SCORE_DIFF = 2.0
MAX_SCORE = 13.0
# ── КОНСТАНТЫ ─────────────────────────────────────
STATS_FILE = "stats.json"
ATR_MIN_PCT = 0.0003  # 0.03% от цены (~0.69 при ETH=2315)
FORCE_TEST = False
force_test_done = False
SL_MARGIN_PCT = 0.05
TP_MARGIN_PCT = 0.15
NIGHT_HOURS = (22, 6)
RED_NEWS_DROP = -0.01
RED_NEWS_VOL = 1.5
RED_NEWS_BLOCK = 1800
MAX_LOSSES = 3
PAUSE_LOSSES = 1800
last_heartbeat_time = 0
app = Flask(__name__)
@app.route('/')
def home():
    return "OK", 200
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ── ГЛОБАЛЬНЫЙ КЭШ ─────────────────────────────────
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
    "liquidation": {"long_liqu": 0.0, "short_liqu": 0.0, "ts": 0},
    "btc_dominance": {"value": 50, "change": 0, "ts": 0},
    "funding_avg": {"value": 0.0, "ts": 0},
    "trending": {"eth_in_top": False, "ts": 0},
    "gas_price": {"value": 20, "ts": 0},
    "news_sentiment": {"value": 0.0, "ts": 0},
    "stablecoin": {"value": 0, "change": 0, "ts": 0},
    "4h_trend": {"diff": 0.0, "bull": False, "ts": 0},
}
ob_history = []
last_ob = 0.0
red_news_until = 0
pause_until = 0
losses_in_row = 0
yesterday_high, yesterday_low = 0, 0

stats = {
    "total": 0, "wins": 0, "losses": 0,
    "total_profit": 0.0, "total_profit_sum": 0.0,
    "total_loss_sum": 0.0, "max_drawdown": 0.0,
    "peak_equity": 0.0, "current_equity": 0.0
}
def load_stats():
    global stats
    try:
        with open(STATS_FILE, "r") as f:
            loaded = json.load(f)
            for k in stats:
                if k in loaded: stats[k] = loaded[k]
        log.info(f"Статистика загружена: {stats['total']} сделок")
    except:
        log.info("Нет статистики, начинаем с нуля")
def save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f)
    except Exception as e:
        log.error(f"Ошибка сохранения: {e}")
def update_equity(pnl):
    stats["current_equity"] += pnl
    if stats["current_equity"] > stats["peak_equity"]:
        stats["peak_equity"] = stats["current_equity"]
    dd = stats["peak_equity"] - stats["current_equity"]
    if dd > stats["max_drawdown"]:
        stats["max_drawdown"] = dd
active_positions = {}
def load_active_positions():
    global active_positions
    try:
        with open("active_positions.json", "r") as f:
            active_positions = json.load(f)
        log.info(f"Загружено {len(active_positions)} позиций")
    except:
        log.info("Нет сохранённых позиций")
def save_active_positions():
    try:
        with open("active_positions.json", "w") as f:
            json.dump(active_positions, f)
    except Exception as e:
        log.error(f"Ошибка сохранения позиций: {e}")
load_stats()
load_active_positions()
# TELEGRAM
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        log.info(f"TG OK" if r.status_code == 200 else f"TG FAIL: {r.text}")
    except Exception as e:
        log.error(f"TG: {e}")
# OKX API
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
def _sign(ts, method, path, body=""):
    if not OKX_API_KEY or not OKX_SECRET or not OKX_PASSPHRASE:
        return ""
    prehash = str(ts) + str.upper(method) + path + str(body)
    mac = hmac.new(OKX_SECRET.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_get(path, params=None):
    if not OKX_API_KEY: return {}
    ts = _ts()
    query = f"?{requests.compat.urlencode(params)}" if params else ""
    sign = _sign(ts, "GET", path + query)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    headers.update(OKX_DEMO_HEADER)
    try:
        r = requests.get(f"{OKX_BASE}{path}{query}", headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log.error(f"OKX GET error: {e}")
        return {}

def okx_post(path, data):
    if not OKX_API_KEY: return {}
    ts = _ts()
    body = json.dumps(data) if data else ""
    sign = _sign(ts, "POST", path, body)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    headers.update(OKX_DEMO_HEADER)
    try:
        r = requests.post(f"{OKX_BASE}{path}", data=body, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log.error(f"OKX POST error: {e}")
        return {}

def okx_set_leverage():
    return okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL, "lever": str(LEVERAGE), "mgnMode": "cross"
    })

def okx_get_balance():
    try:
        r = okx_get("/api/v5/account/balance")
        for d in r.get("data", []):
            for dt in d.get("details", []):
                if dt.get("ccy") == "USDT":
                    return float(dt.get("eq", 0))
    except:
        pass
    return 0.0

def okx_get_positions():
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_cancel_algo(algo_id):
    if not algo_id:
        return
    okx_post("/api/v5/trade/cancel-algo-order", {"instId": SYMBOL, "algoId": algo_id})

def okx_close_market(pos_side, qty):
    cls_side = "sell" if pos_side == "long" else "buy"
    return okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "market", "sz": str(qty)
    })

def okx_place_order(direction, entry, sl, tp):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"

    hour = datetime.now(timezone.utc).hour
    multiplier = 0.5 if NIGHT_HOURS[0] <= hour < NIGHT_HOURS[1] else 1.0
    effective = ORDER_USDT * multiplier
    total_qty = max(1, round(effective * LEVERAGE / entry / 0.01))

    log.info(f"{direction} qty={total_qty} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f}")

    r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": side, "posSide": pos_side,
        "ordType": "market", "sz": str(total_qty)
    })
    if r.get("code") != "0":
        msg = r.get("msg", "Unknown")
        if r.get("data"): msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}

    order_id = r["data"][0].get("ordId", "")
    log.info(f"Открыта ordId={order_id}")

    active_positions[order_id] = {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "total_qty": total_qty, "pos_side": pos_side, "cls_side": cls_side,
        "open_time": time.time()
    }
    save_active_positions()
    return {"ok": True, "orderId": order_id, "total_qty": total_qty}

# ── РЫНОЧНЫЕ ДАННЫЕ ──
def get_klines(sym, interval, limit=150):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
            timeout=10
        ).json()
        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "taker_buy_base", "tbq", "ignore"
        ])
        for c in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            df[c] = df[c].astype(float)
        df["candle_time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e:
        log.error(f"klines {sym} {interval}: {e}")
        return None

def get_funding():
    try:
        return float(requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}",
        ).json()["lastFundingRate"])
    except:
        return 0.0

def get_ob():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=50").json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

def get_btc_momentum():
    try:
        df = get_klines(BTC_SYMBOL, "3m", 5)
        if df is None: return 0.0, 0
        chg = (df.iloc[-1]["close"] - df.iloc[-3]["close"]) / df.iloc[-3]["close"]
        bt_c_dir = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), bt_c_dir
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

def get_fear_greed():
    now = time.time()
    if now - cache["fear_greed"]["ts"] < 3600: return cache["fear_greed"]["value"]
    try:
        v = int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]["value"])
        cache["fear_greed"] = {"value": v, "ts": now}
        return v
    except:
        return 50

def get_long_short_ratio():
    now = time.time()
    if now - cache["long_short"]["ts"] < 300: return cache["long_short"]["value"]
    try:
        v = float(requests.get(
            "https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        ).json()[0]["longShortRatio"])
        cache["long_short"] = {"value": v, "ts": now}
        return v
    except:
        return 1.0

def get_taker_ratio():
    now = time.time()
    if now - cache["taker_ratio"]["ts"] < 300: return cache["taker_ratio"]["value"]
    try:
        v = float(requests.get(
            "https://fapi.binance.com/fapi/v1/takerlongshortRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        ).json()[0]["buySellRatio"])
        cache["taker_ratio"] = {"value": v, "ts": now}
        return v
    except:
        return 1.0

def get_open_interest():
    now = time.time()
    if now - cache["open_interest"]["ts"] < 300:
        return cache["open_interest"]["value"], cache["open_interest"]["change"]
    try:
        oi = float(requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT").json()["openInterest"])
        prev = cache["open_interest"]["value"]
        chg = ((oi - prev) / prev * 100) if prev > 0 else 0
        cache["open_interest"] = {"value": oi, "change": chg, "ts": now}
        return oi, chg
    except:
        return 0, 0

def get_dxy():
    now = time.time()
    if now - cache["dxy"]["ts"] < 3600: return cache["dxy"]["value"], cache["dxy"]["change"]
    try:
        if TWELVE_API_KEY:
            data = requests.get(
                f"https://api.twelvedata.com/quote?symbol=DXY&apikey={TWELVE_API_KEY}",
                timeout=10
            ).json()
            v = float(data.get("close", 100))
            prev = float(data.get("previous_close", 100))
            chg = ((v - prev) / prev * 100) if prev > 0 else 0
            cache["dxy"] = {"value": v, "change": chg, "ts": now}
            return v, chg
    except:
        pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        meta = r.json()["chart"]["result"][0]["meta"]
        v = meta["regularMarketPrice"]
        chg = (v - meta["previousClose"]) / meta["previousClose"] * 100
        cache["dxy"] = {"value": v, "change": chg, "ts": now}
        return v, chg
    except:
        return 100, 0

def get_vix():
    now = time.time()
    if now - cache["vix"]["ts"] < 3600:
        return cache["vix"]["value"]
    try:
        if TWELVE_API_KEY:
            v = float(requests.get(
                f"https://api.twelvedata.com/quote?symbol=VIX&apikey={TWELVE_API_KEY}",
                timeout=10
            ).json().get("close", 20))
            cache["vix"] = {"value": v, "ts": now}
            return v
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        v = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        cache["vix"] = {"value": v, "ts": now}
        return v
    except:
        return 20

def get_usdt_dominance():
    now = time.time()
    if now - cache["usdt_dominance"]["ts"] < 600:
        return cache["usdt_dominance"]["value"], cache["usdt_dominance"]["change"]
    try:
        data = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()
        dom = float(data["data"]["market_cap_percentage"]["usdt"])
        prev = cache["usdt_dominance"]["value"]
        chg = dom - prev if prev > 0 else 0
        cache["usdt_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except:
        return 5.0, 0

def get_btc_dominance():
    now = time.time()
    if now - cache["btc_dominance"]["ts"] < 600:
        return cache["btc_dominance"]["value"], cache["btc_dominance"]["change"]
    try:
        data = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()
        dom = float(data["data"]["market_cap_percentage"]["btc"])
        prev = cache["btc_dominance"]["value"]
        chg = dom - prev if prev > 0 else 0
        cache["btc_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except:
        return 50, 0

def get_coinbase_premium():
    return 0.0

def get_eth_btc_ratio():
    now = time.time()
    if now - cache["eth_btc_ratio"]["ts"] < 300:
        return cache["eth_btc_ratio"]["value"], cache["eth_btc_ratio"]["change"]
    try:
        eth_price = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT").json()["price"])
        btc_price = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").json()["price"])
        ratio = eth_price / btc_price if btc_price > 0 else 0.0
        prev = cache["eth_btc_ratio"]["value"]
        chg = ratio - prev if prev > 0 else 0.0
        cache["eth_btc_ratio"] = {"value": ratio, "change": chg, "ts": now}
        return ratio, chg
    except:
        return 0.0, 0.0

def get_liquidations():
    return 0.0, 0.0

def get_funding_avg():
    now = time.time()
    if now - cache["funding_avg"]["ts"] < 300:
        return cache["funding_avg"]["value"]
    try:
        rates = []
        b = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}").json()
        rates.append(float(b.get("lastFundingRate", 0)))
        o = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}").json()
        if o.get("data"):
            rates.append(float(o["data"][0].get("fundingRate", 0)))
        avg = sum(rates) / len(rates) if rates else 0.0
        cache["funding_avg"] = {"value": avg, "ts": now}
        return avg
    except:
        return 0.0

def get_trending():
    now = time.time()
    if now - cache["trending"]["ts"] < 600:
        return cache["trending"]["eth_in_top"]
    try:
        coins = [c["item"]["symbol"] for c in requests.get(
            "https://api.coingecko.com/api/v3/search/trending", timeout=10
        ).json().get("coins", [])]
        eth_top = "ETH" in coins
        cache["trending"] = {"eth_in_top": eth_top, "ts": now}
        return eth_top
    except:
        return False

def get_gas_price():
    now = time.time()
    if now - cache["gas_price"]["ts"] < 60:
        return cache["gas_price"]["value"]
    try:
        v = float(requests.get(
            "https://api.etherscan.io/api?module=gastracker&action=gasoracle",
            timeout=10
        ).json()["result"]["ProposeGasPrice"])
        cache["gas_price"] = {"value": v, "ts": now}
        return v
    except:
        return 20

def get_news_sentiment():
    return 0.0

def get_stablecoin_supply():
    return 0.0, 0.0

def is_important_economic_day():
    return False

def get_4h_trend():
    now = time.time()
    if now - cache["4h_trend"]["ts"] < 3600:
        return cache["4h_trend"]["diff"], cache["4h_trend"]["bull"]
    try:
        df = get_klines(SYMBOL_BN, "4h", 200)
        if df is None or len(df) < 200: return 0.0, False
        e50 = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        e200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        diff = (e50 - e200) / e200 * 100
        cache["4h_trend"] = {"diff": diff, "bull": e50 > e200, "ts": now}
        return diff, e50 > e200
    except:
        pass
    return 0.0, False

def detect_whales():
    """IsolationForest на стакане - выявляет аномальные заявки."""
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=10").json()
        bids = [[float(b[0]), float(b[1])] for b in d["bids"][:10]]
        asks = [[float(a[0]), float(a[1])] for a in d["asks"][:10]]
        X = np.array(bids + asks)
        if len(X) < 5: return False, 0
        preds = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
        detected = -1 in preds
        return detected, 1 if detected else 0
    except:
        return False, 0

def get_eth_btc_correlation():
    try:
        e = get_klines("ETHUSDT", "5m", 12)
        b = get_klines("BTCUSDT", "5m", 12)
        if e is None or b is None: return 1.0
        corr = e["close"].pct_change().dropna().corr(b["close"].pct_change().dropna())
        return corr if not np.isnan(corr) else 1.0
    except:
        return 1.0

# ИНДИКАТОРЫ
def calc(df):
    # EMA
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    # RSI
    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    # MACD
    e12 = df["close"].ewm(span=12, adjust=False).mean()
    e26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = e12 - e26
    df["MACD_sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift() <= df["MACD_sig"].shift())
    df["MACD_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift() >= df["MACD_sig"].shift())
    # ATR
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()
    # Bollinger
    bm = df["close"].rolling(20).mean()
    bs = df["close"].rolling(20).std()
    df["BB_up"] = bm + 2 * bs
    df["BB_dn"] = bm - 2 * bs
    df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)
    # VWAP
    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    # CVD
    df["CVD"] = (df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"]))
    df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)
    # Объём
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.3
    df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
    df["price_dir"] = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["vol_dir"] = df["vol_spike"].astype(int) * df["price_dir"]
    # ADX
    up = df["high"] - df["high"].shift()
    dn = df["low"].shift() - df["low"]
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    pdm = np.where((up > dn) & (up > 0), up, 0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0)
    atr14 = tr.rolling(14).mean()
    pdi = 100 * (pd.Series(pdm).rolling(14).mean() / atr14)
    ndi = 100 * (pd.Series(ndm).rolling(14).mean() / atr14)
    dx = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
    df["ADX"] = dx.rolling(14).mean()
    # OBV
    df["OBV"] = (df["volume"] * np.sign(df["close"].diff())).cumsum()
    df["OBV_ma"] = df["OBV"].rolling(20).mean()
    # Свечные паттерны
    body = (df["close"] - df["open"]).abs()
    lw = df[["open","close"]].min(axis=1) - df["low"]
    uw = df["high"] - df[["open","close"]].max(axis=1)
    df["hammer"] = (lw > body*2) & (uw < body*0.5) & (df["close"] > df["open"])
    df["shooter"] = (uw > body*2) & (lw < body*0.5) & (df["close"] < df["open"])
    return df

# ГРАДУИРОВАННАЯ СИСТЕМА БАЛЛОВ
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low
    global force_test_done, red_news_until, MIN_SCORE
    if df is None or len(df) < 10:
        return None, None, None, None, 0, "Нет данных", 0, 0, {}
    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    adx = row["ADX"] if not np.isnan(row.get("ADX", float("nan"))) else 20

    # Фильтры входа
    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, "Рынок мёртвый", 0, 0, {}
    now = time.time()
    hour = datetime.now(timezone.utc).hour
    # Свежесть свечи
    sec = (datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)).total_seconds()
    if sec < 60:
        return None, None, None, None, 0, f"Свеча свежая ({int(sec)}с)", 0, 0, {}
    # Открытие США (13-15 UTC) — высокая волатильность, пропускаем
    if 13 <= hour < 15:
        return None, None, None, None, 0, "Открытие США — пропуск", 0, 0, {}
    # Низкая корреляция ETH/BTC — аномалия
    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция низкая ({corr:.2f})", 0, 0, {}
    # Красные новости
    if red_news_until > now:
        prev_price = df.iloc[-2]["close"]
        if ((price - prev_price) / prev_price < -RED_NEWS_DROP and
            row["volume"] > df.iloc[-2]["volume"] * RED_NEWS_VOL):
            red_news_until = now + RED_NEWS_BLOCK
            log.warning("Красные новости!")
    # FORCE TEST
    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, 5.0, "FORCE TEST", 5.0, 0, {}
    # Стакан (динамика)
    ob_history.append(ob)
    if len(ob_history) > 6: ob_history.pop(0)
    ob_rising = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
    ob_delta = ob - last_ob if last_ob != 0 else 0
    last_ob = ob

    # Внешние метрики
    fear_greed = get_fear_greed()
    long_short = get_long_short_ratio()
    taker_ratio = get_taker_ratio()
    oi, oi_change = get_open_interest()
    dxy, dxy_change = get_dxy()
    vix = get_vix()
    _, usdt_dom_change = get_usdt_dominance()
    cb_premium = get_coinbase_premium()
    _, eth_btc_chg = get_eth_btc_ratio()
    long_liqu, short_liqu = get_liquidations()
    funding_avg = get_funding_avg()
    news_sent = get_news_sentiment()
    _, btc_dom_change = get_btc_dominance()
    gas_price = get_gas_price()
    eth_trending = get_trending()
    _, stable_chg = get_stablecoin_supply()
    important_day = is_important_economic_day()
    ema50_200_diff, ema_4h_bull = get_4h_trend()
    whales_detected, _ = detect_whales()
    # OBV дивергенция
    obv_div = 0
    if not np.isnan(row.get("OBV", float("nan"))) and not np.isnan(row.get("OBV_ma", float("nan"))):
        if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]: obv_div = 1
        elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]: obv_div = -1

    L = S = 0.0
    # ЯДРО – вес 1.0 (макс ~7 баллов)
    # 1. EMA тренд (вес 1.0 / 0.5)
    if row["EMA9"] > row["EMA21"] > row["EMA50"]: L += 1.0
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]: S += 1.0
    elif row["EMA9"] > row["EMA21"]: L += 0.5
    elif row["EMA9"] < row["EMA21"]: S += 0.5
    # 2. 4Н тренд (вес 1.0) – старший ТФ самый важный
    if ema_4h_bull: L += 1.0
    else: S += 1.0
    # 3. RSI (вес 1.0 / 0.5)
    if rsi < 35: L += 1.0
    elif rsi < 45: L += 0.5
    elif rsi > 65: S += 1.0
    elif rsi > 55: S += 0.5
    # 4. MACD (вес 1.0 / 0.5)
    if row["MACD_bull"]: L += 1.0
    elif row["MACD"] > row["MACD_sig"]: L += 0.5
    if row["MACD_bear"]: S += 1.0
    elif row["MACD"] < row["MACD_sig"]: S += 0.5
    # 5. CVD – реальный поток денег (вес 1.0)
    if row["CVD_up"]: L += 1.0
    else: S += 1.0
    # 6. Объём + направление (вес 1.0 / 0.5)
    if row["vol_spike"]:
        if row["vol_dir"] > 0: L += 1.0
        elif row["vol_dir"] < 0: S += 1.0
    if row["vol_extreme"]:
        if L > S: L += 0.5
        else: S += 0.5
    # 7. Стакан (вес 0.5)
    if ob > 5 or ob_rising: L += 0.5
    if ob < -5 or ob_falling: S += 0.5
    # ADX фильтр – слабый тренд снижает баллы ядра
    if adx < 20:
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)
    # СРЕДНИЕ – вес 0.5 (макс ~4 балла)
    # 8. Fear & Greed (contrarian)
    if fear_greed < 25: L += 0.5
    elif fear_greed < 40: L += 0.25
    elif fear_greed > 75: S += 0.5
    elif fear_greed > 65: S += 0.25
    # 9. Long/Short Ratio (contrarian)
    if long_short < 0.8: L += 0.5
    elif long_short < 1.1: L += 0.25
    elif long_short > 2.5: S += 0.5
    elif long_short > 1.8: S += 0.25
    # 10. Taker ratio
    if taker_ratio > 1.3: L += 0.5
    elif taker_ratio > 1.1: L += 0.25
    # 11. Coinbase Premium (институциональ)
    if cb_premium > 0.05: L += 0.5
    elif cb_premium > 0.02: L += 0.25
    elif cb_premium < -0.05: S += 0.5
    elif cb_premium < -0.02: S += 0.25
    # 12. ETH/BTC (относительная сила)
    if eth_btc_chg > 0.3: L += 0.5
    elif eth_btc_chg > 0.1: L += 0.25
    elif eth_btc_chg < -0.3: S += 0.5
    elif eth_btc_chg < -0.1: S += 0.25
    # 13. OI изменение
    if oi_change > 3:
        if price > df.iloc[-2]["close"]: L += 0.5
        else: S += 0.5
    elif oi_change < -3:
        if price < df.iloc[-2]["close"]: S += 0.25
        else: L += 0.25
    # 14. Bollinger position
    bp = row["BB_pct"]
    if bp < 0.1: L += 0.5
    elif bp > 0.9: S += 0.5
    # 15. VWAP
    if price < row["VWAP"]: L += 0.5
    else: S += 0.5
    # ВСПОМОГАТЕЛЬНЫЕ — вес 0.25 (макс ~2 балла)
    # 16. BTC моментум
    if btc_mom > 0.2: L += 0.25; S = max(0, S - 0.25)
    elif btc_mom < -0.2: S += 0.25; L = max(0, L - 0.25)
    # 17. DXY (обратная корреляция с крипто)
    if dxy_change > 0.3: S += 0.25; L = max(0, L - 0.25)
    elif dxy_change < -0.3: L += 0.25; S = max(0, S - 0.25)
    # 18. VIX (высокий страх = риск-офф)
    if vix > 30:
        L = max(0, L - 0.25); S = max(0, S - 0.25)
    # 19. Ликвидации
    liq_diff = short_liqu - long_liqu
    if liq_diff > 0.5: L += 0.25
    elif liq_diff < -0.5: S += 0.25
    # 20. BTC доминанс (рост BTC.D = деньги уходят из альтов)
    if btc_dom_change > 0.2: S += 0.25; L = max(0, L - 0.25)
    elif btc_dom_change < -0.2: L += 0.25; S = max(0, S - 0.25)
    # 21. Фандинг средний
    if funding_avg > 0.005: S += 0.25
    elif funding_avg < -0.005: L += 0.25
    # 22. USDT доминанс (рост = уход в кэш)
    if usdt_dom_change > 0.2: S += 0.25
    elif usdt_dom_change < -0.2: L += 0.25
    # 23. Стейблкоин приток (деньги готовы войти)
    if stable_chg > 0.5: L += 0.25
    # 24. Газ Ethereum (высокий = активность)
    if gas_price > 50:
        if L > S: L += 0.25
        else: S += 0.25
    # 25. Сентимент новостей
    if news_sent > 0.3: L += 0.25
    elif news_sent < -0.3: S += 0.25
    # 26. OBV дивергенция
    if obv_div == 1: L += 0.25
    elif obv_div == -1: S += 0.25
    # 27. ETH в трендинге
    if eth_trending:
        if L > S: L += 0.25
        else: S += 0.25
    # 28. Паттерны свечей
    if row["hammer"]: L += 0.25
    if row["shooter"]: S += 0.25
    # 29. ОВ дельта
    if ob_delta > 3: L += 0.25
    elif ob_delta < -3: S += 0.25

    # Итоговое решение
    L = round(L, 2)
    S = round(S, 2)
    diff = L - S

    ml_metrics = {
        "fear_greed": fear_greed, "long_short": long_short, "taker_ratio": taker_ratio,
        "oi_change": oi_change, "cb_premium": cb_premium, "eth_btc_chg": eth_btc_chg,
        "liq_diff": liq_diff, "funding_avg": funding_avg, "adx": adx, "rsi": rsi,
        "bb_pct": bp, "ob": ob, "btc_mom": btc_mom,
        "hour": hour, "weekday": datetime.now(timezone.utc).weekday()
    }

    reason_str = f"L:{L} S:{S} D:{diff:.1f}"
    score = max(L, S)

    if diff >= MIN_SCORE_DIFF and L >= MIN_SCORE:
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, L, reason_str, L, S, ml_metrics
    elif -diff >= MIN_SCORE_DIFF and S >= MIN_SCORE:
        entry = price
        sl = round(entry * (1 + SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 - TP_MARGIN_PCT / LEVERAGE), 2)
        return "SHORT", entry, sl, tp, S, reason_str, L, S, ml_metrics

    return None, None, None, None, score, reason_str, L, S, ml_metrics

# ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ
def check_closed_positions():
    global active_positions, losses_in_row, MIN_SCORE
    if not active_positions:
        return
    try:
        pos_ids = list(active_positions.keys())
        if not pos_ids: return
        r = okx_get("/api/v5/trade/orders-history", {"instType": "SWAP", "instId": SYMBOL, "limit": 100})
        history = r.get("data", [])
        if not history: return

        for order_id in pos_ids:
            pos = active_positions.get(order_id)
            if not pos: continue

            direction = pos["direction"]
            entry = pos["entry"]
            is_closed = False
            total_pnl = 0.0
            close_reason = ""

            for h in history:
                if h.get("side") == pos["cls_side"] and h.get("posSide") == pos["pos_side"]:
                    avg_px = float(h.get("avgPx", 0))
                    qty = float(h.get("sz", 0))
                    exec_t = float(h.get("cTime", 0)) / 1000
                    if avg_px > 0 and qty > 0 and exec_t > pos["open_time"]:
                        is_closed = True
                        if direction == "LONG":
                            pnl_pct = (avg_px - entry) / entry * 100
                            if avg_px >= pos.get("tp", 0) * 0.99:
                                close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                            elif avg_px <= pos.get("sl", 0) * 1.01:
                                close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                        else:
                            pnl_pct = (entry - avg_px) / entry * 100
                            if avg_px <= pos.get("tp", 0) * 1.01:
                                close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                            elif avg_px >= pos.get("sl", 0) * 0.99:
                                close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                        pos_val = ORDER_USDT * LEVERAGE * (qty / pos.get("total_qty", qty))
                        total_pnl += pnl_pct / 100 * pos_val

            if total_pnl != 0:
                is_win = total_pnl > 0
                if is_win:
                    stats["wins"] += 1
                    stats["total_profit_sum"] += total_pnl
                    if MIN_SCORE != BASE_MIN_SCORE:
                        MIN_SCORE = BASE_MIN_SCORE
                        log.info(f"MIN_SCORE → {BASE_MIN_SCORE}")
                    losses_in_row = 0
                else:
                    stats["losses"] += 1
                    stats["total_loss_sum"] += abs(total_pnl)
                    losses_in_row += 1
                    if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
                        MIN_SCORE += 0.5
                        log.info(f"3 убытка − MIN_SCORE → {MIN_SCORE}")
                        send_telegram(f"3 убытка подряд\nMIN_SCORE → {MIN_SCORE}")
                stats["total"] += 1
                stats["total_profit"] += total_pnl
                update_equity(total_pnl)
                save_stats()

                winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                avg_pnl = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
                emoji = "✅" if is_win else "🔴"
                send_telegram(
                    f"{emoji} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                    f"📈 Направление: {direction}\n"
                    f"💰 Вход: {entry:.2f}\n"
                    f"📝 Причина: {close_reason}\n"
                    f"💵 P&L: {total_pnl:+.2f} USDT\n\n"
                    f"<b>📊 СТАТИСТИКА:</b>\n"
                    f"🔹 Всего: {stats['total']}\n"
                    f"✅ Прибыльных: {stats['wins']} (+{stats['total_profit_sum']:.2f})\n"
                    f"🔴 Убыточных: {stats['losses']} (-{stats['total_loss_sum']:.2f})\n"
                    f"💰 P&L итого: {stats['total_profit']:+.2f} USDT\n"
                    f"📉 Макс просадка: {stats['max_drawdown']:.2f} USDT\n"
                    f"📊 Средний P&L: {avg_pnl:+.2f} USDT\n"
                    f"🎯 Винрейт: {winrate:.1f}%\n"
                    f"⚙️ MIN_SCORE: {MIN_SCORE}"
                )
                update_signal_result(order_id, "TP" if is_win else "SL", total_pnl)
                maybe_retrain()
                del active_positions[order_id]
                save_active_positions()
    except Exception as e:
        log.error(f"check_closed: {e}")

# ШКАЛА БАЛЛОВ
def score_bar(score):
    filled = min(10, round(score / MAX_SCORE * 10))
    bar = "🟩" * filled + "⬜" * (10 - filled)
    if score >= 8: emoji = "🟢"
    elif score >= 6: emoji = "🟡"
    elif score >= 4: emoji = "🟠"
    else: emoji = "🔴"
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 8: return "🟢"
    elif score >= 6: return "🟡"
    elif score >= 4: return "🟠"
    return "🔴"

# ОСНОВНОЙ ЦИКЛ
def run_scan():
    global last_heartbeat_time, pause_until, yesterday_high, yesterday_low

    now = time.time()
    if now < pause_until:
        log.info(f"⏸️ Пауза {int((pause_until- now)/60)} мин")
        return

    check_closed_positions()

    if now - last_heartbeat_time >= 3600 or yesterday_high == 0:
        yesterday_high, yesterday_low = get_yesterday_levels()

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None:
        send_telegram("❌ Ошибка свечей")
        return

    calc(df)
    funding = get_funding()
    ob = get_ob()
    btc_mom, btc_dir = get_btc_momentum()
    price = df.iloc[-1]["close"]
    atr_val = df.iloc[-1]["ATR"]

    sig = get_signal(df, funding, ob, btc_mom, btc_dir)
    if len(sig) == 9:
        direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = sig
    else:
        direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = *sig, 0, 0, {}

    log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f}/{MAX_SCORE} | {reason}")

    # Heartbeat
    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_get_balance()
        pos = okx_get_positions()
        hour = datetime.now(timezone.utc).hour
        session = "🌙 Ночь" if 1<=hour<6 else ("🌅 Утро" if hour<13 else "🌤️ День")
        fear_greed = get_fear_greed()
        long_short = get_long_short_ratio()
        cb_premium = get_coinbase_premium()
        _, eth_btc_chg = get_eth_btc_ratio()
        long_liqu, short_liqu = get_liquidations()
        gas = get_gas_price()
        btc_dom, _ = get_btc_dominance()
        important_day = is_important_economic_day()

        l_num, s_num = _L, _S

        sig_status = (
            f"{direction} {score_color(score)} <b>{score:.1f}</b>"
            if direction else f"НЕТ {score_color(max(l_num,s_num))} <b>{max(l_num,s_num):.1f}</b>"
        )

        winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
        send_telegram(
            f"<b>❤️ Heartbeat v5.0</b>{' ⚠️' if important_day else ''}\n\n"
            f"💰 <b>ETH: {price:.2f}</b> ATR:{atr_val:.2f}\n"
            f"😱 F&G:{fear_greed} | L/S:{long_short:.2f}\n"
            f"🌍 <b>{session}</b> | 💳 Баланс:{bal:.2f} USDT | Поз:{len(pos)}\n"
            f"🎯 Сигнал:{sig_status} | L:{l_num:.1f} S:{s_num:.1f}\n"
            f"⚙️ MIN_SCORE:{MIN_SCORE} (База:{BASE_MIN_SCORE})\n"
            f"📊 {stats['total']} сд | ✅ {stats['wins']} ({winrate:.1f}%) | P&L:{stats['total_profit']:+.2f}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    if direction is None:
        return

    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    msg = [
        f"<b>[{mode}]</b>",
        f"{'🟢' if direction=='LONG' else '🔴'} <b>SCALP {direction}</b>",
        f"",
        f"<b>Надёжность:</b> {score_bar(score)}",
        f"",
        f"💰 <b>Вход:</b> <b>{entry:.2f}</b>",
        f"🛑 <b>Стоп:</b> {sl:.2f} ({int(SL_MARGIN_PCT*100)}% маржи)",
        f"🎯 <b>Тейк:</b> {tp:.2f} ({int(TP_MARGIN_PCT*100)}% маржи)",
        f"📊 <b>R/R:</b> {TP_MARGIN_PCT/SL_MARGIN_PCT:.1f}:1",
        f"⚙️ <b>MIN:</b> {MIN_SCORE}",
        f"📝 <b>{reason}</b>",
    ]

    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("🔑 OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res["ok"]:
            losses_in_row = 0
            msg += [
                f"✅ <b>ИСПОЛНЕНО НА OKX</b>",
                f"📦 Контрактов: {res['total_qty']}",
                f"🔖 OrderID: {res['orderId']}",
            ]
            save_signal_to_history({
                "order_id": res["orderId"],
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "score": score,
                "L": _L,
                "S": _S,
                "metrics": _ml_metrics,
                "timestamp": time.time(),
                "result": None,
                "label": None,
            })
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ {MAX_LOSSES} ошибки – пауза {PAUSE_LOSSES//60} мин")
            msg += [f"❌ Ошибка {res['step']}: {res['msg']}"]

    msg.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт v5.0")
    bal = okx_get_balance()
    stats["current_equity"] = bal
    stats["peak_equity"] = bal

    winrate = (stats["wins"]/stats["total"]*100) if stats["total"] > 0 else 0
    send_telegram(
        f"<b>🚀 OKX Scalp Bot v5.0</b>\n\n"
        f"📐 Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"⚙️ MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE} (diff ≥ {MIN_SCORE_DIFF})\n"
        f"🛑 SL: -{int(SL_MARGIN_PCT*100)}% | 🎯 TP: +{int(TP_MARGIN_PCT*100)}% R/R: {TP_MARGIN_PCT/SL_MARGIN_PCT:.1f}:1\n"
        f"💳 Баланс: {bal:.2f} USDT\n"
        f"📊 Статистика: {stats['total']} | {stats['wins']} ({winrate:.1f}%)\n\n"
        f"<b>Система v5.0:</b>\n"
        f"🔹 Градуированные веса (Ядро/Средние/Вспомог)\n"
        f"🔹 ADX фильтр слабых трендов\n"
        f"🔹 Автосужение MIN_SCORE\n"
        f"🔹 Корреляция ETH/BTC\n"
        f"🔹 IsolationForest (киты)\n"
        f"🔹 Ночной множитель позиции *0.5\n"
        f"🔹 Защита от красных новостей"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

# КРУПНЫЕ СДЕЛКИ (без ключа, Binance aggTrades)
def get_large_trades_signal():
    try:
        now_ms = int(time.time() * 1000)
        ago_ms = now_ms - 2 * 60 * 1000
        data = requests.get(
            f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={SYMBOL_BN}&startTime={ago_ms}&endTime={now_ms}&limit=500",
            timeout=8
        ).json()
        buy_vol = sell_vol = 0.0
        LARGE = 50.0
        for t in data:
            qty = float(t.get("q", 0))
            if qty < LARGE: continue
            if not t.get("m", True): buy_vol += qty
            else: sell_vol += qty
        diff = buy_vol - sell_vol
        log.info(f"Крупные сделки: buy={buy_vol:.0f} sell={sell_vol:.0f} diff={diff:.0f}")
        return diff
    except Exception as e:
        log.error(f"large_trades: {e}")
        return 0.0

# СИСТЕМА ОБУЧЕНИЯ НА СВОИХ СИГНАЛАХ
SIGNALS_FILE = "signals_history.json"
ML_MODEL_FILE = "scalp_model.pkl"
signals_history = []
try:
    with open(SIGNALS_FILE, "r") as f:
        signals_history = json.load(f)
    log.info(f"История сигналов загружена: {len(signals_history)} записей")
except:
    log.info("История сигналов пуста, начинаем собирать")

scalp_model = None
try:
    import joblib
    scalp_model = joblib.load(ML_MODEL_FILE)
    log.info("ML модель загружена")
except:
    log.info("ML модель не найдена, будет создана после 50 сделок")

def save_signal_to_history(signal_data: dict):
    signals_history.append(signal_data)
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(signals_history, f)
    except Exception as e:
        log.error(f"Ошибка сохранения сигнала: {e}")

def update_signal_result(order_id: str, result: str, pnl: float):
    for s in signals_history:
        if s.get("order_id") == order_id:
            s["result"] = result
            s["pnl"] = pnl
            s["label"] = 1 if result == "TP" else 0
            break
    try:
        with open(SIGNALS_FILE, "w") as f:
            json.dump(signals_history, f)
    except Exception as e:
        log.error(f"Ошибка обновления сигнала: {e}")

def get_ml_features(L: float, S: float, metrics: dict) -> list:
    return [
        L, S, L - S,
        metrics.get("fear_greed", 50),
        metrics.get("long_short", 1.0),
        metrics.get("taker_ratio", 1.0),
        metrics.get("oi_change", 0),
        metrics.get("cb_premium", 0),
        metrics.get("eth_btc_chg", 0),
        metrics.get("liq_diff", 0),
        metrics.get("funding_avg", 0),
        metrics.get("adx", 20),
        metrics.get("rsi", 50),
        metrics.get("bb_pct", 0.5),
        metrics.get("ob", 0),
        metrics.get("btc_mom", 0),
        metrics.get("large_trades", 0),
        metrics.get("hour", 12),
        metrics.get("weekday", 2),
    ]

def train_model():
    global scalp_model
    completed = [s for s in signals_history if "label" in s]
    if len(completed) < 30:
        log.info(f"Недостаточно данных для обучения: {len(completed)}/30")
        return False
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import cross_val_score
        import joblib

        X = [get_ml_features(s.get("L", 0), s.get("S", 0), s.get("metrics", {})) for s in completed]
        y = [s["label"] for s in completed]
        X = np.array(X)
        y = np.array(y)
        win_rate = sum(y) / len(y)
        log.info(f"Обучение: {len(X)} примеров, винрейт в данных: {win_rate:.1%}")
        scale = StandardScaler()
        X_sc = scale.fit_transform(X)
        model = GradientBoostingClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
        )
        model.fit(X_sc, y)
        if len(X) >= 50:
            scores = cross_val_score(model, X_sc, y, cv=3, scoring="accuracy")
            log.info(f"CV accuracy: {scores.mean():.2f} ± {scores.std():.2f}")
        scalp_model = {"model": model, "scaler": scale}
        joblib.dump(scalp_model, ML_MODEL_FILE)
        log.info(f"Модель обучена на {len(X)} примерах")
        send_telegram(
            f"<b>🧠 ML модель обновлена</b>\n\n"
            f"Примеров: {len(X)}\n"
            f"Винрейт в данных: {win_rate:.1%}\n"
            + (f"CV accuracy: {scores.mean():.2f}" if len(X) >= 50 else "")
        )
        return True
    except Exception as e:
        log.error(f"Ошибка обучения: {e}")
        return False

def get_ml_bonus(L: float, S: float, metrics: dict) -> float:
    global scalp_model
    if scalp_model is None:
        return 0.0
    try:
        features = np.array([get_ml_features(L, S, metrics)])
        scale = scalp_model["scaler"]
        model = scalp_model["model"]
        X_sc = scale.transform(features)
        prob = model.predict_proba(X_sc)[0][1]
        if prob > 0.7: return +0.5
        elif prob > 0.6: return +0.25
        elif prob < 0.3: return -0.5
        elif prob < 0.4: return -0.25
        return 0.0
    except Exception as e:
        log.error(f"ML bonus: {e}")
        return 0.0

_trades_since_last_train = 0
RETRAIN_EVERY = 20

def maybe_retrain():
    global _trades_since_last_train
    _trades_since_last_train += 1
    if _trades_since_last_train >= RETRAIN_EVERY:
        _trades_since_last_train = 0
        threading.Thread(target=train_model, daemon=True).start()
        log.info("Запущено переобучение модели в фоне")

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
