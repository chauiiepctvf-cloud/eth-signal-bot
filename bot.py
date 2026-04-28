"""
OKX Scalp Bot v5.1 — Исправленная система баллов
Что изменено vs v5.0:
1. CVD считается накопительно (rolling sum), а не только одна свеча
2. OBV дивергенция исправлена (была перепутана логика)
3. Taker ratio — добавлен шорт-сигнал (был только лонг)
4. Bollinger — добавлены промежуточные уровни 0.2/0.8
5. Убран фильтр 13-15 UTC (открытие США) — пропускаем лучшие сигналы дня
6. Добавлен фильтр: не входить против 4h тренда с малым запасом
7. RSI дивергенция — новый фактор ядра
8. Конфлюэнция — бонус только если совпадают 4 из 5 ядер
9. Убран двойной счёт BTC моментума (был и в ядре и в вспомогательных)
10. MIN_SCORE_DIFF повышен до 2.5 — меньше ложных сигналов
"""
import os, time, logging, threading, hashlib, hmac, base64, json
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from flask import Flask
from sklearn.ensemble import IsolationForest

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
OKX_API_KEY    = os.environ.get("OKX_API_KEY")
OKX_SECRET     = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "")

OKX_BASE        = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}

SYMBOL    = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"

# ── НЕ ТРОГАЕМ (плечо, стоп, тейк) ──────────────
LEVERAGE      = 50
ORDER_USDT    = 20
SL_MARGIN_PCT = 0.30
TP_MARGIN_PCT = 0.25
# ─────────────────────────────────────────────────

SCAN_INTERVAL      = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60

# ── ИСПРАВЛЕННЫЕ ПОРОГИ ───────────────────────────
BASE_MIN_SCORE = 6.0    # было 5.5 — повышаем качество входов
MIN_SCORE      = 6.0
MIN_SCORE_DIFF = 2.5    # было 2.0 — меньше ложных сигналов
MAX_SCORE      = 13.0

STATS_FILE    = "stats.json"
ATR_MIN_PCT   = 0.0003
FORCE_TEST    = False
force_test_done = False

NIGHT_HOURS    = (22, 6)
RED_NEWS_DROP  = -0.01
RED_NEWS_VOL   = 1.5
RED_NEWS_BLOCK = 1800
MAX_LOSSES     = 3
PAUSE_LOSSES   = 1800

last_heartbeat_time = 0
app = Flask(__name__)

@app.route('/')
def home():
    return "OK", 200

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

cache = {
    "fear_greed":       {"value": 50,   "ts": 0},
    "long_short":       {"value": 1.0,  "ts": 0},
    "taker_ratio":      {"value": 1.0,  "ts": 0},
    "open_interest":    {"value": 0,    "change": 0, "ts": 0},
    "dxy":              {"value": 100,  "change": 0, "ts": 0},
    "vix":              {"value": 20,   "ts": 0},
    "usdt_dominance":   {"value": 5.0,  "change": 0, "ts": 0},
    "coinbase_premium": {"value": 0.0,  "ts": 0},
    "eth_btc_ratio":    {"value": 0.0,  "change": 0.0, "ts": 0},
    "liquidation":      {"long_liqu": 0.0, "short_liqu": 0.0, "ts": 0},
    "btc_dominance":    {"value": 50,   "change": 0, "ts": 0},
    "funding_avg":      {"value": 0.0,  "ts": 0},
    "trending":         {"eth_in_top": False, "ts": 0},
    "gas_price":        {"value": 20,   "ts": 0},
    "news_sentiment":   {"value": 0.0,  "ts": 0},
    "stablecoin":       {"value": 0,    "change": 0, "ts": 0},
    "4h_trend":         {"diff": 0.0,   "bull": False, "ts": 0},
    "1h_trend":         {"price_vs_ema": 0.0, "bull": True, "ts": 0},
}

ob_history = []
last_ob    = 0.0
red_news_until = 0
pause_until    = 0
losses_in_row  = 0
yesterday_high = yesterday_low = 0

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
        log.info(f"Статистика: {stats['total']} сделок")
    except:
        log.info("Нет статистики")

def save_stats():
    try:
        with open(STATS_FILE, "w") as f: json.dump(stats, f)
    except Exception as e: log.error(f"save_stats: {e}")

def update_equity(pnl):
    stats["current_equity"] += pnl
    if stats["current_equity"] > stats["peak_equity"]:
        stats["peak_equity"] = stats["current_equity"]
    dd = stats["peak_equity"] - stats["current_equity"]
    if dd > stats["max_drawdown"]: stats["max_drawdown"] = dd

active_positions = {}

def load_active_positions():
    global active_positions
    try:
        with open("active_positions.json", "r") as f:
            active_positions = json.load(f)
    except: pass

def save_active_positions():
    try:
        with open("active_positions.json", "w") as f:
            json.dump(active_positions, f)
    except Exception as e: log.error(f"save_pos: {e}")

load_stats()
load_active_positions()

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        log.info("TG OK" if r.status_code == 200 else f"TG FAIL: {r.text}")
    except Exception as e: log.error(f"TG: {e}")

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sign(ts, method, path, body=""):
    if not OKX_API_KEY or not OKX_SECRET or not OKX_PASSPHRASE: return ""
    prehash = str(ts) + str.upper(method) + path + str(body)
    mac = hmac.new(OKX_SECRET.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_get(path, params=None):
    if not OKX_API_KEY: return {}
    ts    = _ts()
    query = f"?{requests.compat.urlencode(params)}" if params else ""
    sign  = _sign(ts, "GET", path + query)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    headers.update(OKX_DEMO_HEADER)
    try:
        return requests.get(f"{OKX_BASE}{path}{query}", headers=headers, timeout=15).json()
    except Exception as e: log.error(f"OKX GET: {e}"); return {}

def okx_post(path, data):
    if not OKX_API_KEY: return {}
    ts   = _ts()
    body = json.dumps(data) if data else ""
    sign = _sign(ts, "POST", path, body)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    headers.update(OKX_DEMO_HEADER)
    try:
        return requests.post(f"{OKX_BASE}{path}", data=body, headers=headers, timeout=15).json()
    except Exception as e: log.error(f"OKX POST: {e}"); return {}

def okx_set_leverage():
    return okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL, "lever": str(LEVERAGE), "mgnMode": "cross"
    })

def okx_get_balance():
    try:
        r = okx_get("/api/v5/account/balance")
        for d in r.get("data", []):
            for dt in d.get("details", []):
                if dt.get("ccy") == "USDT": return float(dt.get("eq", 0))
    except: pass
    return 0.0

def okx_get_positions():
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_cancel_algo(algo_id):
    if not algo_id: return
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
    side     = "buy"  if direction == "LONG"  else "sell"
    pos_side = "long" if direction == "LONG"  else "short"
    cls_side = "sell" if direction == "LONG"  else "buy"

    hour       = datetime.now(timezone.utc).hour
    multiplier = 0.5 if NIGHT_HOURS[0] <= hour or hour < NIGHT_HOURS[1] else 1.0
    effective  = ORDER_USDT * multiplier
    total_qty  = max(1, round(effective * LEVERAGE / entry / 0.01))

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

    # SL и TP через algo — 3 попытки каждый
    tp_ok = sl_ok = False
    for _ in range(3):
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last"
        })
        if tp_r.get("code") == "0": tp_ok = True; break
        time.sleep(1)

    for _ in range(3):
        sl_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last"
        })
        if sl_r.get("code") == "0": sl_ok = True; break
        time.sleep(1)

    active_positions[order_id] = {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "total_qty": total_qty, "pos_side": pos_side, "cls_side": cls_side,
        "open_time": time.time(), "tp_ok": tp_ok, "sl_ok": sl_ok
    }
    save_active_positions()
    return {"ok": True, "orderId": order_id, "total_qty": total_qty, "tp_ok": tp_ok, "sl_ok": sl_ok}

# ── ДАННЫЕ ───────────────────────────────────────
def get_klines(sym, interval, limit=150):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
            timeout=10
        ).json()
        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","qv","trades","taker_buy_base","tbq","ignore"
        ])
        for c in ("open","high","low","close","volume","taker_buy_base"):
            df[c] = df[c].astype(float)
        df["candle_time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e: log.error(f"klines {sym}: {e}"); return None

def get_funding():
    try:
        return float(requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8
        ).json()["lastFundingRate"])
    except: return 0.0

def get_ob():
    try:
        d    = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=50", timeout=8).json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
        tot  = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except: return 0.0

def get_btc_momentum():
    try:
        df = get_klines(BTC_SYMBOL, "3m", 5)
        if df is None: return 0.0, 0
        chg     = (df.iloc[-1]["close"] - df.iloc[-3]["close"]) / df.iloc[-3]["close"]
        btc_dir = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), btc_dir
    except: return 0.0, 0

def get_yesterday_levels():
    try:
        df = get_klines(SYMBOL_BN, "1d", 2)
        if df is not None and len(df) >= 2:
            y = df.iloc[-2]
            return float(y["high"]), float(y["low"])
    except: pass
    return 0, 0

def _cached(key, ttl, fetch_fn, default):
    now = time.time()
    if now - cache[key]["ts"] < ttl:
        return cache[key]
    try:
        result = fetch_fn()
        if result is not None:
            if isinstance(result, dict):
                cache[key] = result
                cache[key]["ts"] = now
            else:
                cache[key]["value"] = result
                cache[key]["ts"] = now
    except: pass
    return cache[key]

def get_fear_greed():
    now = time.time()
    if now - cache["fear_greed"]["ts"] < 3600: return cache["fear_greed"]["value"]
    try:
        v = int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]["value"])
        cache["fear_greed"] = {"value": v, "ts": now}
        return v
    except: return 50

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
    except: return 1.0

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
    except: return 1.0

def get_open_interest():
    now = time.time()
    if now - cache["open_interest"]["ts"] < 300:
        return cache["open_interest"]["value"], cache["open_interest"]["change"]
    try:
        oi   = float(requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT", timeout=8).json()["openInterest"])
        prev = cache["open_interest"]["value"]
        chg  = ((oi - prev) / prev * 100) if prev > 0 else 0
        cache["open_interest"] = {"value": oi, "change": chg, "ts": now}
        return oi, chg
    except: return 0, 0

def get_dxy():
    now = time.time()
    if now - cache["dxy"]["ts"] < 3600: return cache["dxy"]["value"], cache["dxy"]["change"]
    try:
        if TWELVE_API_KEY:
            data = requests.get(f"https://api.twelvedata.com/quote?symbol=DXY&apikey={TWELVE_API_KEY}", timeout=10).json()
            v    = float(data.get("close", 100))
            prev = float(data.get("previous_close", 100))
            chg  = ((v - prev) / prev * 100) if prev > 0 else 0
            cache["dxy"] = {"value": v, "change": chg, "ts": now}
            return v, chg
        r    = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=5d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        meta = r.json()["chart"]["result"][0]["meta"]
        v    = meta["regularMarketPrice"]
        chg  = (v - meta["previousClose"]) / meta["previousClose"] * 100
        cache["dxy"] = {"value": v, "change": chg, "ts": now}
        return v, chg
    except: return 100, 0

def get_vix():
    now = time.time()
    if now - cache["vix"]["ts"] < 3600: return cache["vix"]["value"]
    try:
        if TWELVE_API_KEY:
            v = float(requests.get(f"https://api.twelvedata.com/quote?symbol=VIX&apikey={TWELVE_API_KEY}", timeout=10).json().get("close", 20))
            cache["vix"] = {"value": v, "ts": now}; return v
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        v = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        cache["vix"] = {"value": v, "ts": now}; return v
    except: return 20

def get_usdt_dominance():
    now = time.time()
    if now - cache["usdt_dominance"]["ts"] < 600:
        return cache["usdt_dominance"]["value"], cache["usdt_dominance"]["change"]
    try:
        dom  = float(requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"]["usdt"])
        prev = cache["usdt_dominance"]["value"]
        chg  = dom - prev if prev > 0 else 0
        cache["usdt_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except: return 5.0, 0

def get_btc_dominance():
    now = time.time()
    if now - cache["btc_dominance"]["ts"] < 600:
        return cache["btc_dominance"]["value"], cache["btc_dominance"]["change"]
    try:
        dom  = float(requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"]["btc"])
        prev = cache["btc_dominance"]["value"]
        chg  = dom - prev if prev > 0 else 0
        cache["btc_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except: return 50, 0

def get_coinbase_premium():
    now = time.time()
    if now - cache["coinbase_premium"]["ts"] < 60: return cache["coinbase_premium"]["value"]
    try:
        cb = float(requests.get("https://api.exchange.coinbase.com/products/ETH-USD/ticker", timeout=8).json()["price"])
        bn = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=8).json()["price"])
        p  = round((cb - bn) / bn * 100, 4)
        cache["coinbase_premium"] = {"value": p, "ts": now}
        return p
    except: return 0.0

def get_eth_btc_ratio():
    now = time.time()
    if now - cache["eth_btc_ratio"]["ts"] < 300:
        return cache["eth_btc_ratio"]["value"], cache["eth_btc_ratio"]["change"]
    try:
        eth = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=8).json()["price"])
        btc = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8).json()["price"])
        ratio = eth / btc if btc > 0 else 0.0
        prev  = cache["eth_btc_ratio"]["value"]
        chg   = ((ratio - prev) / prev * 100) if prev > 0 else 0.0
        cache["eth_btc_ratio"] = {"value": ratio, "change": round(chg, 4), "ts": now}
        return ratio, round(chg, 4)
    except: return 0.0, 0.0

def get_liquidations():
    return 0.0, 0.0

def get_funding_avg():
    now = time.time()
    if now - cache["funding_avg"]["ts"] < 300: return cache["funding_avg"]["value"]
    try:
        rates = []
        b = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json()
        rates.append(float(b.get("lastFundingRate", 0)))
        o = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}", timeout=8).json()
        if o.get("data"): rates.append(float(o["data"][0].get("fundingRate", 0)))
        avg = sum(rates) / len(rates) if rates else 0.0
        cache["funding_avg"] = {"value": avg, "ts": now}
        return avg
    except: return 0.0

def get_trending():
    now = time.time()
    if now - cache["trending"]["ts"] < 600: return cache["trending"]["eth_in_top"]
    try:
        coins   = [c["item"]["symbol"] for c in requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json().get("coins", [])]
        eth_top = "ETH" in coins
        cache["trending"] = {"eth_in_top": eth_top, "ts": now}
        return eth_top
    except: return False

def get_gas_price():
    now = time.time()
    if now - cache["gas_price"]["ts"] < 60: return cache["gas_price"]["value"]
    try:
        v = float(requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10).json()["result"]["ProposeGasPrice"])
        cache["gas_price"] = {"value": v, "ts": now}; return v
    except: return 20

def get_news_sentiment():  return 0.0
def get_stablecoin_supply(): return 0.0, 0.0
def is_important_economic_day(): return False

def get_4h_trend():
    now = time.time()
    if now - cache["4h_trend"]["ts"] < 3600:
        return cache["4h_trend"]["diff"], cache["4h_trend"]["bull"]
    try:
        df = get_klines(SYMBOL_BN, "4h", 200)
        if df is None or len(df) < 200: return 0.0, False
        e50  = df["close"].ewm(span=50,  adjust=False).mean().iloc[-1]
        e200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        diff = (e50 - e200) / e200 * 100
        cache["4h_trend"] = {"diff": diff, "bull": e50 > e200, "ts": now}
        return diff, e50 > e200
    except: return 0.0, False

def get_1h_trend():
    """
    Фильтр по 1h EMA50.
    Возвращает: (price_vs_ema_pct, is_bullish)
    price_vs_ema_pct > 0 = цена выше EMA50 на 1h = бычий тренд
    price_vs_ema_pct < 0 = цена ниже EMA50 на 1h = медвежий тренд
    """
    now = time.time()
    if now - cache["1h_trend"]["ts"] < 300:  # кэш 5 минут
        return cache["1h_trend"]["price_vs_ema"], cache["1h_trend"]["bull"]
    try:
        df = get_klines(SYMBOL_BN, "1h", 60)
        if df is None or len(df) < 50:
            return 0.0, True
        ema50        = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        current_price = df["close"].iloc[-1]
        pct          = (current_price - ema50) / ema50 * 100
        bull         = current_price > ema50
        cache["1h_trend"] = {"price_vs_ema": round(pct, 3), "bull": bull, "ts": now}
        log.info(f"1h тренд: EMA50={ema50:.2f} цена={current_price:.2f} отклонение={pct:+.2f}%")
        return round(pct, 3), bull
    except Exception as e:
        log.error(f"get_1h_trend: {e}")
        return 0.0, True


def detect_whales():
    try:
        d    = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=10", timeout=8).json()
        bids = [[float(b[0]), float(b[1])] for b in d["bids"][:10]]
        asks = [[float(a[0]), float(a[1])] for a in d["asks"][:10]]
        X    = np.array(bids + asks)
        if len(X) < 5: return False, 0
        preds    = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
        detected = -1 in preds
        return detected, 1 if detected else 0
    except: return False, 0

def get_eth_btc_correlation():
    try:
        e = get_klines("ETHUSDT", "5m", 12)
        b = get_klines("BTCUSDT", "5m", 12)
        if e is None or b is None: return 1.0
        corr = e["close"].pct_change().dropna().corr(b["close"].pct_change().dropna())
        return corr if not np.isnan(corr) else 1.0
    except: return 1.0

# ── ИНДИКАТОРЫ ───────────────────────────────────
def calc(df):
    df["EMA9"]   = df["close"].ewm(span=9,   adjust=False).mean()
    df["EMA21"]  = df["close"].ewm(span=21,  adjust=False).mean()
    df["EMA50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

    d    = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    e12 = df["close"].ewm(span=12, adjust=False).mean()
    e26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"]      = e12 - e26
    df["MACD_sig"]  = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift() <= df["MACD_sig"].shift())
    df["MACD_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift() >= df["MACD_sig"].shift())
    df["MACD_hist"] = df["MACD"] - df["MACD_sig"]

    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()

    bm          = df["close"].rolling(20).mean()
    bs          = df["close"].rolling(20).std()
    df["BB_up"] = bm + 2*bs
    df["BB_dn"] = bm - 2*bs
    df["BB_pct"]= (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)

    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # FIX 1: CVD накопительно за 20 свечей, а не одна свеча
    df["CVD_raw"] = df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])
    df["CVD"]     = df["CVD_raw"].rolling(20).sum()
    df["CVD_up"]  = df["CVD"] > df["CVD"].shift(3)
    # Ускорение CVD — растёт ли давление
    df["CVD_accel"] = df["CVD"] - df["CVD"].shift(6)

    df["vol_ma"]      = df["volume"].rolling(20).mean()
    df["vol_spike"]   = df["volume"] > df["vol_ma"] * 1.3
    df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
    df["price_dir"]   = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["vol_dir"]     = df["vol_spike"].astype(int) * df["price_dir"]

    up   = df["high"] - df["high"].shift()
    dn   = df["low"].shift() - df["low"]
    tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    pdm  = np.where((up > dn) & (up > 0), up, 0)
    ndm  = np.where((dn > up) & (dn > 0), dn, 0)
    atr14 = tr.rolling(14).mean()
    pdi  = 100 * (pd.Series(pdm, index=df.index).rolling(14).mean() / atr14)
    ndi  = 100 * (pd.Series(ndm, index=df.index).rolling(14).mean() / atr14)
    dx   = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
    df["ADX"] = dx.rolling(14).mean()
    df["+DI"] = pdi
    df["-DI"] = ndi

    df["OBV"]    = (df["volume"] * np.sign(df["close"].diff())).cumsum()
    df["OBV_ma"] = df["OBV"].rolling(20).mean()

    body         = (df["close"] - df["open"]).abs()
    lw           = df[["open","close"]].min(axis=1) - df["low"]
    uw           = df["high"] - df[["open","close"]].max(axis=1)
    df["hammer"] = (lw > body*2) & (uw < body*0.5) & (df["close"] > df["open"])
    df["shooter"]= (uw > body*2) & (lw < body*0.5) & (df["close"] < df["open"])

    # FIX 2: RSI дивергенция (новый индикатор)
    # Бычья: цена падает, RSI растёт — потенциальный разворот вверх
    # Медвежья: цена растёт, RSI падает — потенциальный разворот вниз
    df["rsi_div_bull"] = (df["close"] < df["close"].shift(5)) & (df["RSI"] > df["RSI"].shift(5)) & (df["RSI"] < 50)
    df["rsi_div_bear"] = (df["close"] > df["close"].shift(5)) & (df["RSI"] < df["RSI"].shift(5)) & (df["RSI"] > 50)

    return df

# ══════════════════════════════════════════════════════
# ИСПРАВЛЕННАЯ СИСТЕМА БАЛЛОВ
# ══════════════════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low
    global force_test_done, red_news_until, MIN_SCORE

    if df is None or len(df) < 15:
        return None, None, None, None, 0, "Нет данных", 0, 0, {}

    row   = df.iloc[-1]
    price = row["close"]
    rsi   = row["RSI"]
    atr   = row["ATR"]
    adx   = row["ADX"] if not np.isnan(row.get("ADX", float("nan"))) else 20

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, "Рынок мёртвый", 0, 0, {}

    now  = time.time()
    hour = datetime.now(timezone.utc).hour

    sec = (datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)).total_seconds()
    if sec < 30:
        return None, None, None, None, 0, f"Свеча свежая ({int(sec)}с)", 0, 0, {}

    # FIX 3: Убрали блок 13-15 UTC — там часто лучшие движения дня

    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция {corr:.2f}", 0, 0, {}

    if red_news_until > now:
        prev_price = df.iloc[-2]["close"]
        if ((price - prev_price) / prev_price < RED_NEWS_DROP and
                row["volume"] > df.iloc[-2]["volume"] * RED_NEWS_VOL):
            red_news_until = now + RED_NEWS_BLOCK
            log.warning("🔴 Красные новости!")

    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, 5.0, "FORCE TEST", 5.0, 0, {}

    ob_history.append(ob)
    if len(ob_history) > 6: ob_history.pop(0)
    ob_rising  = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
    ob_delta   = ob - last_ob if last_ob != 0 else 0
    last_ob    = ob

    # Внешние метрики
    fear_greed          = get_fear_greed()
    long_short          = get_long_short_ratio()
    taker_ratio         = get_taker_ratio()
    oi, oi_change       = get_open_interest()
    dxy, dxy_change     = get_dxy()
    vix                 = get_vix()
    _, usdt_dom_change  = get_usdt_dominance()
    cb_premium          = get_coinbase_premium()
    _, eth_btc_chg      = get_eth_btc_ratio()
    long_liqu, short_liqu = get_liquidations()
    funding_avg         = get_funding_avg()
    news_sent           = get_news_sentiment()
    _, btc_dom_change   = get_btc_dominance()
    gas_price           = get_gas_price()
    eth_trending        = get_trending()
    _, stable_chg       = get_stablecoin_supply()
    important_day       = is_important_economic_day()
    ema50_200_diff, ema_4h_bull = get_4h_trend()
    price_vs_1h_ema, trend_1h_bull = get_1h_trend()
    whales_detected, _ = detect_whales()

    liq_diff = short_liqu - long_liqu

    # FIX 4: OBV дивергенция — исправлена логика (была перепутана)
    # Если цена растёт (new high) а OBV падает — медвежий сигнал
    # Если цена падает (new low) а OBV растёт — бычий сигнал
    obv_div = 0
    if not np.isnan(row.get("OBV", float("nan"))) and not np.isnan(row.get("OBV_ma", float("nan"))):
        if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]: obv_div = -1  # медвежья
        elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]: obv_div = 1  # бычья

    L = S = 0.0

    # ═══════════════════════════════════════════════
    # ЯДРО (макс ~8 баллов)
    # ═══════════════════════════════════════════════

    # 1. EMA тренд 5m
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:    L += 1.0
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]: S += 1.0
    elif row["EMA9"] > row["EMA21"]:                  L += 0.5
    elif row["EMA9"] < row["EMA21"]:                  S += 0.5

    # 2. 4H тренд (самый важный фактор)
    if ema_4h_bull:   L += 1.0
    else:              S += 1.0

    # FIX 5: Фильтр против 4h тренда — не входим в противоположную сторону
    # если разрыв EMA50/200 на 4h значительный
    if abs(ema50_200_diff) > 2.0:
        if ema_4h_bull and S > L:     S = max(0, S - 1.5)  # штраф шорту в бычьем 4h
        if not ema_4h_bull and L > S: L = max(0, L - 1.5)  # штраф лонгу в медвежьем 4h

    # НОВЫЙ: Фильтр по 1h EMA50 (градуированный)
    # Боковик (отклонение < 0.3%) — без штрафов, торгуем оба направления
    # Тренд вверх — штраф шортам, тренд вниз — штраф лонгам
    if abs(price_vs_1h_ema) >= 0.3:
        if trend_1h_bull:
            # Цена выше EMA50 на 1h — бычий тренд
            S = max(0, S - 1.5)    # шортам штраф
            if L > S: L += 0.25   # лонгам небольшой бонус
        else:
            # Цена ниже EMA50 на 1h — медвежий тренд
            L = max(0, L - 1.5)    # лонгам штраф
            if S > L: S += 0.25   # шортам небольшой бонус

    # 3. RSI
    if rsi < 35:    L += 1.0
    elif rsi < 45:  L += 0.5
    elif rsi > 65:  S += 1.0
    elif rsi > 55:  S += 0.5

    # 4. MACD + гистограмма
    if row["MACD_bull"]:                          L += 1.0
    elif row["MACD"] > row["MACD_sig"]:           L += 0.5
    if row["MACD_bear"]:                          S += 1.0
    elif row["MACD"] < row["MACD_sig"]:           S += 0.5
    # Нарастающая гистограмма — дополнительный балл
    if row["MACD_hist"] > 0 and row["MACD_hist"] > df.iloc[-3]["MACD_hist"]: L += 0.25
    if row["MACD_hist"] < 0 and row["MACD_hist"] < df.iloc[-3]["MACD_hist"]: S += 0.25

    # 5. CVD (исправлен — rolling sum)
    if row["CVD_up"]: L += 1.0
    else:              S += 1.0
    # Ускорение CVD
    if not np.isnan(row.get("CVD_accel", float("nan"))):
        if row["CVD_accel"] > 0:   L += 0.25
        elif row["CVD_accel"] < 0: S += 0.25

    # 6. Объём + направление
    if row["vol_spike"]:
        if row["vol_dir"] > 0:   L += 1.0
        elif row["vol_dir"] < 0: S += 1.0
    if row["vol_extreme"]:
        if L > S: L += 0.5
        else:     S += 0.5

    # 7. Стакан
    if ob > 5 or ob_rising:    L += 0.5
    if ob < -5 or ob_falling:  S += 0.5

    # FIX 6: RSI дивергенция — новый фактор ядра
    if row.get("rsi_div_bull", False): L += 0.75
    if row.get("rsi_div_bear", False): S += 0.75

    # ADX фильтр
    if adx < 20:
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)
    # ADX направление через DI
    if adx > 25:
        if not np.isnan(row.get("+DI", float("nan"))):
            if row["+DI"] > row["-DI"]: L += 0.25
            else:                        S += 0.25

    # ═══════════════════════════════════════════════
    # СРЕДНИЕ (макс ~4 балла)
    # ═══════════════════════════════════════════════

    # 8. Fear & Greed
    if fear_greed < 25:    L += 0.5
    elif fear_greed < 40:  L += 0.25
    elif fear_greed > 75:  S += 0.5
    elif fear_greed > 65:  S += 0.25

    # 9. Long/Short Ratio
    if long_short < 0.8:    L += 0.5
    elif long_short < 1.1:  L += 0.25
    elif long_short > 2.5:  S += 0.5
    elif long_short > 1.8:  S += 0.25

    # FIX 7: Taker ratio — добавлен шорт-сигнал (раньше был только лонг)
    if taker_ratio > 1.3:    L += 0.5
    elif taker_ratio > 1.1:  L += 0.25
    elif taker_ratio < 0.7:  S += 0.5   # ← было пропущено
    elif taker_ratio < 0.9:  S += 0.25  # ← было пропущено

    # 10. Coinbase Premium
    if cb_premium > 0.05:    L += 0.5
    elif cb_premium > 0.02:  L += 0.25
    elif cb_premium < -0.05: S += 0.5
    elif cb_premium < -0.02: S += 0.25

    # 11. ETH/BTC
    if eth_btc_chg > 0.3:    L += 0.5
    elif eth_btc_chg > 0.1:  L += 0.25
    elif eth_btc_chg < -0.3: S += 0.5
    elif eth_btc_chg < -0.1: S += 0.25

    # 12. OI
    if oi_change > 3:
        if price > df.iloc[-2]["close"]: L += 0.5
        else:                             S += 0.5
    elif oi_change < -3:
        if price < df.iloc[-2]["close"]: S += 0.25
        else:                             L += 0.25

    # FIX 8: Bollinger — добавлены промежуточные уровни
    bp = row["BB_pct"]
    if bp < 0.1:    L += 0.5
    elif bp < 0.2:  L += 0.25  # ← новый уровень
    elif bp > 0.9:  S += 0.5
    elif bp > 0.8:  S += 0.25  # ← новый уровень

    # 13. VWAP
    if price < row["VWAP"]: L += 0.5
    else:                    S += 0.5

    # ═══════════════════════════════════════════════
    # ВСПОМОГАТЕЛЬНЫЕ (макс ~2 балла)
    # ═══════════════════════════════════════════════

    # FIX 9: BTC моментум — убран из двойного счёта, оставлен только здесь
    if btc_mom > 0.002:    L += 0.25; S = max(0, S - 0.25)
    elif btc_mom < -0.002: S += 0.25; L = max(0, L - 0.25)

    if dxy_change > 0.3:    S += 0.25; L = max(0, L - 0.25)
    elif dxy_change < -0.3: L += 0.25; S = max(0, S - 0.25)

    if vix > 35:
        L = max(0, L - 0.5); S = max(0, S - 0.5)  # экстремальный страх — не торгуем
    elif vix > 25:
        L = max(0, L - 0.25); S = max(0, S - 0.25)

    if liq_diff > 0.5:    L += 0.25
    elif liq_diff < -0.5: S += 0.25

    if btc_dom_change > 0.2:    S += 0.25; L = max(0, L - 0.25)
    elif btc_dom_change < -0.2: L += 0.25; S = max(0, S - 0.25)

    if funding_avg > 0.005:    S += 0.25
    elif funding_avg < -0.005: L += 0.25

    if usdt_dom_change > 0.2:    S += 0.25
    elif usdt_dom_change < -0.2: L += 0.25

    if gas_price > 50:
        if L > S: L += 0.25
        else:     S += 0.25

    if news_sent > 0.3:    L += 0.25
    elif news_sent < -0.3: S += 0.25

    # FIX 10: OBV дивергенция — исправленная
    if obv_div == 1:    L += 0.25   # бычья дивергенция
    elif obv_div == -1: S += 0.25   # медвежья дивергенция

    if eth_trending:
        if L > S: L += 0.25
        else:     S += 0.25

    if row["hammer"]:  L += 0.25
    if row["shooter"]: S += 0.25

    if ob_delta > 3:    L += 0.25
    elif ob_delta < -3: S += 0.25

    # ═══════════════════════════════════════════════
    # ШТРАФЫ
    # ═══════════════════════════════════════════════

    if important_day:
        L = max(0, L - 1.0); S = max(0, S - 1.0)

    # Ночная сессия
    if NIGHT_HOURS[0] <= hour or hour < NIGHT_HOURS[1]:
        if L >= S: L = max(0, L - 0.75)
        else:      S = max(0, S - 0.75)

    # Пятница вечером
    if hour >= 20 and datetime.now(timezone.utc).weekday() == 4:
        if L >= S: L = max(0, L - 1.0)
        else:      S = max(0, S - 1.0)

    # Первый час Лондона
    if 8 <= hour < 9:
        if L >= S: L = max(0, L - 0.5)
        else:      S = max(0, S - 0.5)

    # Вторник-четверг — бонус
    if datetime.now(timezone.utc).weekday() in [1, 2, 3]:
        if L > S: L += 0.25
        else:     S += 0.25

    if red_news_until > now:
        L = 0   # запрет лонгов при красных новостях

    # Вчерашние уровни
    if yesterday_high > 0 and yesterday_low > 0:
        if price < yesterday_low:                               L = max(0, L - 0.5)
        elif price > yesterday_high:                            S = max(0, S - 0.5)
        elif yesterday_low < price < yesterday_low * 1.01:     L += 0.25
        elif yesterday_high * 0.99 < price < yesterday_high:   S += 0.25

    # FIX 11: Конфлюэнция — только если совпадают 4+ ядра (было 3)
    ema_l   = row["EMA9"] > row["EMA21"] > row["EMA50"]
    ema_s   = row["EMA9"] < row["EMA21"] < row["EMA50"]
    macd_l  = row["MACD"] > row["MACD_sig"]
    macd_s  = row["MACD"] < row["MACD_sig"]
    cvd_l   = row["CVD_up"]
    vol_l   = row["vol_dir"] > 0
    vol_s   = row["vol_dir"] < 0

    core_long  = sum([ema_l, ema_4h_bull, rsi < 45, macd_l, cvd_l, vol_l])
    core_short = sum([ema_s, not ema_4h_bull, rsi > 55, macd_s, not cvd_l, vol_s])

    if core_long >= 4:  L += 0.75   # было 3 условия
    if core_short >= 4: S += 0.75

    # ── ML бонус ────────────────────────────────────
    ml_metrics = {
        "fear_greed": fear_greed, "long_short": long_short, "taker_ratio": taker_ratio,
        "oi_change": oi_change, "cb_premium": cb_premium, "eth_btc_chg": eth_btc_chg,
        "liq_diff": liq_diff, "funding_avg": funding_avg, "adx": adx, "rsi": rsi,
        "bb_pct": bp, "ob": ob, "btc_mom": btc_mom, "large_trades": 0,
        "hour": hour, "weekday": datetime.now(timezone.utc).weekday()
    }
    ml_b = get_ml_bonus(L, S, ml_metrics)
    if ml_b > 0:
        if L > S: L += ml_b
        else:     S += ml_b
    elif ml_b < 0:
        if L > S: L = max(0, L + ml_b)
        else:     S = max(0, S + ml_b)

    L = round(L, 2)
    S = round(S, 2)

    reason_str = (
        f"L:{L} S:{S} D:{L-S:.1f} | "
        f"ADX:{adx:.0f} CVD:{'↑' if row['CVD_up'] else '↓'} "
        f"RSI:{rsi:.0f} 1h:{price_vs_1h_ema:+.2f}% | ML:{ml_b:+.2f}"
    )

    if L - S >= MIN_SCORE_DIFF and L >= MIN_SCORE:
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, L, reason_str, L, S, ml_metrics
    elif S - L >= MIN_SCORE_DIFF and S >= MIN_SCORE:
        entry = price
        sl = round(entry * (1 + SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 - TP_MARGIN_PCT / LEVERAGE), 2)
        return "SHORT", entry, sl, tp, S, reason_str, L, S, ml_metrics

    return None, None, None, None, max(L, S), reason_str, L, S, ml_metrics

# ── ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ ────────────────────
def check_closed_positions():
    global active_positions, losses_in_row, MIN_SCORE
    if not active_positions: return
    try:
        r       = okx_get("/api/v5/trade/orders-history", {"instType": "SWAP", "instId": SYMBOL, "limit": 100})
        history = r.get("data", [])
        if not history: return

        for order_id in list(active_positions.keys()):
            pos = active_positions.get(order_id)
            if not pos: continue

            direction = pos["direction"]
            entry     = pos["entry"]
            total_pnl = 0.0
            close_reason = ""

            for h in history:
                if h.get("side") == pos["cls_side"] and h.get("posSide") == pos["pos_side"]:
                    avg_px = float(h.get("avgPx", 0))
                    qty    = float(h.get("sz", 0))
                    exec_t = float(h.get("cTime", 0)) / 1000
                    if avg_px > 0 and qty > 0 and exec_t > pos["open_time"]:
                        if direction == "LONG":
                            pnl_pct = (avg_px - entry) / entry * 100
                            if avg_px >= pos.get("tp", 0) * 0.99:   close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                            elif avg_px <= pos.get("sl", 0) * 1.01: close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                        else:
                            pnl_pct = (entry - avg_px) / entry * 100
                            if avg_px <= pos.get("tp", 0) * 1.01:   close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}%)"
                            elif avg_px >= pos.get("sl", 0) * 0.99: close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}%)"
                        pos_val    = ORDER_USDT * LEVERAGE * (qty / pos.get("total_qty", qty))
                        total_pnl += pnl_pct / 100 * pos_val

            if total_pnl != 0:
                is_win = total_pnl > 0
                if is_win:
                    stats["wins"] += 1; stats["total_profit_sum"] += total_pnl
                    if MIN_SCORE != BASE_MIN_SCORE: MIN_SCORE = BASE_MIN_SCORE
                    losses_in_row = 0
                else:
                    stats["losses"] += 1; stats["total_loss_sum"] += abs(total_pnl)
                    losses_in_row += 1
                    if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
                        MIN_SCORE += 0.5
                        send_telegram(f"⚠️ 3 убытка подряд\nMIN_SCORE → {MIN_SCORE}")

                stats["total"] += 1; stats["total_profit"] += total_pnl
                update_equity(total_pnl); save_stats()

                winrate = (stats["wins"]/stats["total"]*100) if stats["total"] > 0 else 0
                avg_pnl = stats["total_profit"]/stats["total"] if stats["total"] > 0 else 0
                send_telegram(
                    f"{'✅' if is_win else '🔴'} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                    f"Направление: {direction}\n"
                    f"Вход: {entry:.2f}\n"
                    f"Причина: {close_reason}\n"
                    f"P&L: {total_pnl:+.2f} USDT\n\n"
                    f"<b>СТАТИСТИКА:</b>\n"
                    f"Всего: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%)\n"
                    f"P&L итого: {stats['total_profit']:+.2f} | Ср: {avg_pnl:+.2f}\n"
                    f"Просадка: {stats['max_drawdown']:.2f} | MIN: {MIN_SCORE}"
                )
                update_signal_result(order_id, "TP" if is_win else "SL", total_pnl)
                maybe_retrain()
                del active_positions[order_id]
                save_active_positions()
    except Exception as e: log.error(f"check_closed: {e}")

def score_bar(score):
    filled = min(10, round(score / MAX_SCORE * 10))
    bar    = "🟩" * filled + "⬜" * (10 - filled)
    emoji  = "🟢" if score >= 8 else ("🟡" if score >= 6 else ("🟠" if score >= 4 else "🔴"))
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 8: return "🟢"
    elif score >= 6: return "🟡"
    elif score >= 4: return "🟠"
    return "🔴"

def run_scan():
    global last_heartbeat_time, pause_until, yesterday_high, yesterday_low

    now = time.time()
    if now < pause_until:
        log.info(f"⏸ Пауза {int((pause_until-now)/60)} мин"); return

    check_closed_positions()

    if now - last_heartbeat_time >= 3600 or yesterday_high == 0:
        yesterday_high, yesterday_low = get_yesterday_levels()

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None: send_telegram("❌ Ошибка свечей"); return
    calc(df)

    funding          = get_funding()
    ob               = get_ob()
    btc_mom, btc_dir = get_btc_momentum()
    price            = df.iloc[-1]["close"]
    atr_val          = df.iloc[-1]["ATR"]

    sig = get_signal(df, funding, ob, btc_mom, btc_dir)
    direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = sig

    log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f}/{MAX_SCORE} | {reason}")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal         = okx_get_balance()
        pos         = okx_get_positions()
        hour        = datetime.now(timezone.utc).hour
        session     = "🌙" if (hour >= 22 or hour < 6) else ("🌅" if hour < 13 else "🌤")
        fear_greed  = get_fear_greed()
        long_short  = get_long_short_ratio()
        winrate     = (stats["wins"]/stats["total"]*100) if stats["total"] > 0 else 0
        sig_status  = (
            f"{direction} {score_color(score)} <b>{score:.1f}</b>"
            if direction else f"НЕТ {score_color(max(_L,_S))} <b>{max(_L,_S):.1f}</b>"
        )
        send_telegram(
            f"<b>❤️ Heartbeat v5.1</b>\n\n"
            f"💰 ETH:{price:.2f} ATR:{atr_val:.2f}\n"
            f"😱 F&G:{fear_greed} L/S:{long_short:.2f}\n"
            f"📈 1h тренд: {'🟢 Бычий' if get_1h_trend()[1] else '🔴 Медвежий'} ({get_1h_trend()[0]:+.2f}% от EMA50)\n"
            f"{session} Баланс:{bal:.2f} USDT Поз:{len(pos)}\n"
            f"🎯 {sig_status} L:{_L:.1f} S:{_S:.1f}\n"
            f"⚙️ MIN:{MIN_SCORE} База:{BASE_MIN_SCORE}\n"
            f"📊 {stats['total']} сд ✅{stats['wins']} ({winrate:.1f}%) P&L:{stats['total_profit']:+.2f}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    if direction is None: return

    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    msg  = [
        f"<b>[{mode}] v5.1</b>",
        f"{'🟢' if direction=='LONG' else '🔴'} <b>SCALP {direction}</b>",
        f"",
        f"<b>Надёжность:</b> {score_bar(score)}",
        f"",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} ({int(SL_MARGIN_PCT*100)}% маржи)",
        f"🎯 Тейк: {tp:.2f} ({int(TP_MARGIN_PCT*100)}% маржи)",
        f"⚙️ MIN:{MIN_SCORE} | {reason}",
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
                f"✅ <b>ИСПОЛНЕНО</b>",
                f"📦 Контрактов: {res['total_qty']}",
                f"SL:{'✅' if res['sl_ok'] else '❌'} TP:{'✅' if res['tp_ok'] else '❌'}",
            ]
            save_signal_to_history({
                "order_id": res["orderId"], "direction": direction,
                "entry": entry, "sl": sl, "tp": tp, "score": score,
                "L": _L, "S": _S, "metrics": _ml_metrics,
                "timestamp": time.time(), "result": None, "label": None,
            })
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸ Пауза {PAUSE_LOSSES//60} мин")
            msg += [f"❌ {res['step']}: {res['msg']}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 v5.1")
    bal = okx_get_balance()
    stats["current_equity"] = bal
    stats["peak_equity"]    = bal
    winrate = (stats["wins"]/stats["total"]*100) if stats["total"] > 0 else 0
    send_telegram(
        f"<b>🚀 OKX Scalp Bot v5.1</b>\n\n"
        f"Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE} diff≥{MIN_SCORE_DIFF}\n"
        f"SL:-{int(SL_MARGIN_PCT*100)}% TP:+{int(TP_MARGIN_PCT*100)}%\n"
        f"Баланс: {bal:.2f} USDT\n"
        f"Стат: {stats['total']} | ✅{stats['wins']} ({winrate:.1f}%)\n\n"
        f"<b>Исправлено в v5.1:</b>\n"
        f"• CVD: накопительный rolling sum\n"
        f"• OBV дивергенция: исправлена логика\n"
        f"• Taker ratio: добавлен шорт-сигнал\n"
        f"• RSI дивергенция: новый фактор ядра\n"
        f"• Bollinger: промежуточные уровни\n"
        f"• Фильтр против 4h тренда\n"
        f"• Конфлюэнция: порог 4 из 6\n"
        f"• Убран блок 13-15 UTC\n"
        f"• MIN_SCORE: 5.5→6.0, DIFF: 2.0→2.5\n"
        f"• Фильтр 1h EMA50 тренда\n"
        f"• Свежесть свечи: 60→30с"
    )

    try:
        existing = okx_get_positions()
        if existing:
            for p in existing:
                side = "LONG" if p.get("posSide") == "long" else "SHORT"
                active_positions[f"rec_{int(time.time())}"] = {
                    "direction": side, "entry": float(p.get("avgPx", 0)),
                    "sl": 0, "tp": 0, "total_qty": abs(int(float(p.get("pos", 0)))),
                    "pos_side": p.get("posSide"),
                    "cls_side": "sell" if side == "LONG" else "buy",
                    "open_time": time.time() - 3600
                }
            save_active_positions()
    except Exception as e: log.error(f"Восстановление позиций: {e}")

    while True:
        try: run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

# ── ML СИСТЕМА ───────────────────────────────────
SIGNALS_FILE  = "signals_history.json"
ML_MODEL_FILE = "scalp_model.pkl"
signals_history = []
try:
    with open(SIGNALS_FILE, "r") as f:
        signals_history = json.load(f)
    log.info(f"История: {len(signals_history)} записей")
except: pass

scalp_model = None
try:
    import joblib
    scalp_model = joblib.load(ML_MODEL_FILE)
    log.info("ML модель загружена")
except: pass

def save_signal_to_history(d): 
    signals_history.append(d)
    try:
        with open(SIGNALS_FILE, "w") as f: json.dump(signals_history, f)
    except: pass

def update_signal_result(order_id, result, pnl):
    for s in signals_history:
        if s.get("order_id") == order_id:
            s.update({"result": result, "pnl": pnl, "label": 1 if result=="TP" else 0})
            break
    try:
        with open(SIGNALS_FILE, "w") as f: json.dump(signals_history, f)
    except: pass

def get_ml_features(L, S, m):
    return [L, S, L-S, m.get("fear_greed",50), m.get("long_short",1.0),
            m.get("taker_ratio",1.0), m.get("oi_change",0), m.get("cb_premium",0),
            m.get("eth_btc_chg",0), m.get("liq_diff",0), m.get("funding_avg",0),
            m.get("adx",20), m.get("rsi",50), m.get("bb_pct",0.5),
            m.get("ob",0), m.get("btc_mom",0), m.get("large_trades",0),
            m.get("hour",12), m.get("weekday",2)]

def train_model():
    global scalp_model
    done = [s for s in signals_history if "label" in s]
    if len(done) < 30: return False
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        import joblib
        X  = np.array([get_ml_features(s.get("L",0), s.get("S",0), s.get("metrics",{})) for s in done])
        y  = np.array([s["label"] for s in done])
        sc = StandardScaler()
        Xs = sc.fit_transform(X)
        m  = GradientBoostingClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42)
        m.fit(Xs, y)
        scalp_model = {"model": m, "scaler": sc}
        joblib.dump(scalp_model, ML_MODEL_FILE)
        send_telegram(f"🧠 ML обновлена: {len(X)} примеров | WR:{sum(y)/len(y):.1%}")
        return True
    except Exception as e: log.error(f"train: {e}"); return False

def get_ml_bonus(L, S, m):
    if scalp_model is None: return 0.0
    try:
        X    = np.array([get_ml_features(L, S, m)])
        Xs   = scalp_model["scaler"].transform(X)
        prob = scalp_model["model"].predict_proba(Xs)[0][1]
        if prob > 0.7:   return +0.5
        elif prob > 0.6: return +0.25
        elif prob < 0.3: return -0.5
        elif prob < 0.4: return -0.25
        return 0.0
    except: return 0.0

_n = 0
def maybe_retrain():
    global _n
    _n += 1
    if _n >= 20:
        _n = 0
        threading.Thread(target=train_model, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
