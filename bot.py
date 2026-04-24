"""
OKX Scalp Bot v5.2
- Градуированная система весов (3 уровня приоритета)
- Исправлены ВСЕ синтаксические ошибки v5.0/v5.1
- Убраны нерабочие зависимости (XGBoost, FinBERT, TimescaleDB)
- Добавлен IsolationForest для детекции китов (sklearn встроен)
- Оптимизированы баллы для повышения винрейта
- Восстановлена функция send_telegram
- Исправлена okx_post
- Починена get_liquidations
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
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 50
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60

# ── СИСТЕМА БАЛЛОВ ─────────────────────────────────
BASE_MIN_SCORE = 5.5
MIN_SCORE = 5.5
MIN_SCORE_DIFF = 2.0
MAX_SCORE = 13.0

# ── TP / SL ────────────────────────────────────────
SL_MARGIN_PCT = 0.40
TP_MARGIN_PCT = 0.60
ATR_MIN_PCT = 0.001
ATR_MAX_PCT = 0.05

# ── ЗАЩИТЫ ─────────────────────────────────────────
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

# ══════════════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)
app = Flask(__name__)

@app.route("/")
def home():
    return f"OKX Scalp Bot v5.2 | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════════════
# СОСТОЯНИЕ
# ══════════════════════════════════════════════════════
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []
last_ob = 0
yesterday_high = 0
yesterday_low = 0
red_news_until = 0
force_test_done = False

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
    "trending": {"eth_in_top": False, "ts": 0},
    "gas_price": {"value": 20, "ts": 0},
    "news_sentiment": {"value": 0.0, "ts": 0},
    "stablecoin": {"value": 0, "change": 0, "ts": 0},
    "4h_trend": {"diff": 0.0, "bull": False, "ts": 0},
}

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
                if k in loaded:
                    stats[k] = loaded[k]
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

# ══════════════════════════════════════════════════════
# TELEGRAM (ИСПРАВЛЕНО)
# ══════════════════════════════════════════════════════
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        if r.status_code == 200:
            log.info("Telegram ✅")
        else:
            log.error(f"Telegram ❌: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════════════
# OKX API
# ══════════════════════════════════════════════════════
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        **OKX_DEMO_HEADER
    }

def okx_get(path, params=None):
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    try:
        return requests.get(OKX_BASE + path + qs, headers=_sign(_ts(), "GET", path + qs), timeout=10).json()
    except Exception as e:
        log.error(f"OKX GET {path}: {e}")
        return {}

def okx_post(path, body):
    data = json.dumps(body)
    try:
        r = requests.post(OKX_BASE + path, headers=_sign(_ts(), "POST", path, data), data=data, timeout=10)
        d = r.json()
        if d.get("code") != "0":
            log.warning(f"OKX {path} → {d.get('code')} {d.get('msg','')}")
        return d
    except Exception as e:
        log.error(f"OKX POST {path}: {e}")
        return {}

def okx_set_leverage():
    return okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL, "lever": str(LEVERAGE), "mgnMode": "cross"
    })

def okx_get_balance():
    try:
        r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
        for d in r["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["availBal"])
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
    log.info(f"▶ {direction} qty={total_qty} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} mult={multiplier}")
    r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": side, "posSide": pos_side,
        "ordType": "market", "sz": str(total_qty)
    })
    if r.get("code") != "0":
        msg = r.get("msg", "Unknown")
        if r.get("data"):
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}
    order_id = r["data"][0].get("ordId", "")
    log.info(f"✅ Открыта ordId={order_id}")
    active_positions[order_id] = {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "total_qty": total_qty, "pos_side": pos_side, "cls_side": cls_side,
        "open_time": time.time(), "sl_order_id": None, "tp_order_id": None
    }
    save_active_positions()
    time.sleep(3)
    sl_ok = tp_ok = False
    for attempt in range(3):
        sl_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last"
        })
        if sl_r.get("code") == "0":
            active_positions[order_id]["sl_order_id"] = sl_r["data"][0].get("algoId", "")
            sl_ok = True
            log.info(f"✅ SL={sl:.2f}")
            break
        time.sleep(1)

    for attempt in range(3):
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last"
        })
        if tp_r.get("code") == "0":
            active_positions[order_id]["tp_order_id"] = tp_r["data"][0].get("algoId", "")
            tp_ok = True
            log.info(f"✅ TP={tp:.2f}")
            break
        time.sleep(1)

    save_active_positions()
    if not sl_ok:
        log.error("❌ SL не установлен!")
    if not tp_ok:
        log.error("❌ TP не установлен!")
    return {"ok": True, "orderId": order_id, "total_qty": total_qty, "sl_ok": sl_ok, "tp_ok": tp_ok}

# ══════════════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════════════
def get_klines(symbol=None, interval="5m", limit=150):
    sym = symbol or SYMBOL_BN
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
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8
        ).json()["lastFundingRate"])
    except:
        return 0.0

def get_ob():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
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
        btc_dir = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), btc_dir
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

# ── Метрики с кэшем ────────────────────────────────
def get_fear_greed():
    now = time.time()
    if now - cache["fear_greed"]["ts"] < 3600:
        return cache["fear_greed"]["value"]
    try:
        v = int(requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]["value"])
        cache["fear_greed"] = {"value": v, "ts": now}
        return v
    except:
        return 50

def get_long_short_ratio():
    now = time.time()
    if now - cache["long_short"]["ts"] < 300:
        return cache["long_short"]["value"]
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
    if now - cache["taker_ratio"]["ts"] < 300:
        return cache["taker_ratio"]["value"]
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
        oi = float(requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT", timeout=10).json()["openInterest"])
        prev = cache["open_interest"]["value"]
        chg = ((oi - prev) / prev * 100) if prev > 0 else 0
        cache["open_interest"] = {"value": oi, "change": chg, "ts": now}
        return oi, chg
    except:
        return 0, 0

def get_dxy():
    now = time.time()
    if now - cache["dxy"]["ts"] < 3600:
        return cache["dxy"]["value"], cache["dxy"]["change"]
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
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=2d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
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
    except:
        pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
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
        dom = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"]["usdt"]
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
        dom = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]["market_cap_percentage"]["btc"]
        prev = cache["btc_dominance"]["value"]
        chg = dom - prev if prev > 0 else 0
        cache["btc_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except:
        return 50, 0

def get_coinbase_premium():
    now = time.time()
    if now - cache["coinbase_premium"]["ts"] < 60:
        return cache["coinbase_premium"]["value"]
    try:
        cb = float(requests.get("https://api.exchange.coinbase.com/products/ETH-USD/ticker", timeout=8).json()["price"])
        bn = float(requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=8).json()["price"])
        p = round((cb - bn) / bn * 100, 4)
        cache["coinbase_premium"] = {"value": p, "ts": now}
        return p
    except:
        return 0.0

def get_eth_btc_ratio():
    now = time.time()
    if now - cache["eth_btc_ratio"]["ts"] < 180:
        return cache["eth_btc_ratio"]["value"], cache["eth_btc_ratio"]["change"]
    try:
        data = requests.get("https://api.binance.com/api/v3/klines?symbol=ETHBTC&interval=5m&limit=4", timeout=8).json()
        curr = float(data[-1][4])
        prev = float(data[-4][4])
        chg = (curr - prev) / prev * 100
        cache["eth_btc_ratio"] = {"value": curr, "change": round(chg, 4), "ts": now}
        return curr, round(chg, 4)
    except:
        return 0.0, 0.0

def get_liquidations():
    now = time.time()
    if now - cache["liquidations"]["ts"] < 300:
        return cache["liquidations"]["long_liq"], cache["liquidations"]["short_liq"]
    try:
        data = requests.get(f"https://fapi.binance.com/fapi/v1/forceOrders?symbol={SYMBOL_BN}&limit=200", timeout=10).json()
        long_l = short_l = 0.0
        cutoff = (now - 3600) * 1000
        for o in data:
            if float(o.get("time", 0)) < cutoff:
                continue
            qty = float(o.get("origQty", 0)) * float(o.get("price", 0))
            if o.get("side") == "SELL":
                long_l += qty
            else:
                short_l += qty
        cache["liquidations"] = {
            "long_liq": round(long_l / 1_000_000, 3),
            "short_liq": round(short_l / 1_000_000, 3),
            "ts": now
        }
        return cache["liquidations"]["long_liq"], cache["liquidations"]["short_liq"]
    except:
        return 0.0, 0.0

def get_funding_avg():
    now = time.time()
    if now - cache["funding_avg"]["ts"] < 300:
        return cache["funding_avg"]["value"]
    try:
        rates = []
        b = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}", timeout=8).json()
        rates.append(float(b.get("lastFundingRate", 0)))
        o = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}", timeout=8).json()
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
        coins = [c["item"]["symbol"] for c in requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json().get("coins", [])]
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
        v = float(requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10).json()["result"]["ProposeGasPrice"])
        cache["gas_price"] = {"value": v, "ts": now}
        return v
    except:
        return 20

def get_news_sentiment():
    now = time.time()
    if now - cache["news_sentiment"]["ts"] < 300:
        return cache["news_sentiment"]["value"]
    try:
        df = get_klines(SYMBOL_BN, "5m", 12)
        if df is not None:
            vol_chg = df["volume"].iloc[-1] / df["volume"].mean() if df["volume"].mean() > 0 else 1
            price_chg = (df["close"].iloc[-1] - df["close"].iloc[-6]) / df["close"].iloc[-6] * 100
            sent = max(-1.0, min(1.0, (vol_chg - 1) * 0.5 + price_chg * 0.1))
            cache["news_sentiment"] = {"value": sent, "ts": now}
            return sent
    except:
        pass
    return 0.0

def get_stablecoin_supply():
    now = time.time()
    if now - cache["stablecoin"]["ts"] < 600:
        return cache["stablecoin"]["value"], cache["stablecoin"]["change"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=usd&include_market_cap=true",
            timeout=10
        ).json()
        mcap = r["tether"]["usd_market_cap"]
        prev = cache["stablecoin"]["value"]
        chg = ((mcap - prev) / prev * 100) if prev > 0 else 0
        cache["stablecoin"] = {"value": mcap, "change": chg, "ts": now}
        return mcap, chg
    except:
        return 0, 0

def is_important_economic_day():
    today = datetime.now(timezone.utc)
    if today.weekday() == 4 and today.day <= 7:
        return True
    for m, d in [(3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10)]:
        if today.month == m and today.day == d:
            return True
    return False

def get_4h_trend():
    now = time.time()
    if now - cache["4h_trend"]["ts"] < 600:
        return cache["4h_trend"]["diff"], cache["4h_trend"]["bull"]
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
        bids = [[float(b[0]), float(b[1])] for b in d["bids"][:10]]
        asks = [[float(a[0]), float(a[1])] for a in d["asks"][:10]]
        X = np.array(bids + asks)
        if len(X) < 5:
            return False, 0
        preds = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
        detected = -1 in preds
        return detected, 1 if detected else 0
    except:
        return False, 0

def get_eth_btc_correlation():
    try:
        e = get_klines("ETHUSDT", "5m", 12)
        b = get_klines("BTCUSDT", "5m", 12)
        if e is None or b is None:
            return 1.0
        corr = e["close"].pct_change().dropna().corr(b["close"].pct_change().dropna())
        return corr if not np.isnan(corr) else 1.0
    except:
        return 1.0

# ══════════════════════════════════════════════════════
# ИНДИКАТОРЫ
# ══════════════════════════════════════════════════════
def calc(df):
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
    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
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
    pdi = 100 * (pd.Series(pdm).rolling(14).mean() / atr14)
    ndi = 100 * (pd.Series(ndm).rolling(14).mean() / atr14)
    dx = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
    df["ADX"] = dx.rolling(14).mean()
    df["OBV"] = (df["volume"] * np.sign(df["close"].diff())).cumsum()
    df["OBV_ma"] = df["OBV"].rolling(20).mean()
    body = (df["close"] - df["open"]).abs()
    lw = df[["open", "close"]].min(axis=1) - df["low"]
    uw = df["high"] - df[["open", "close"]].max(axis=1)
    df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
    df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    return df

# ══════════════════════════════════════════════════════
# ГРАДУИРОВАННАЯ СИСТЕМА БАЛЛОВ
# ══════════════════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low
    global force_test_done, red_news_until

    if df is None or len(df) < 10:
        return None, None, None, None, 0, "Нет данных"

    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    adx = row["ADX"] if not np.isnan(row.get("ADX", float("nan"))) else 20

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, "Рынок мёртвый"

    now = time.time()
    hour = datetime.now(timezone.utc).hour

    sec = (datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)).total_seconds()
    if sec < 60:
        return None, None, None, None, 0, f"Свеча свежая ({int(sec)}с)"

    if 13 <= hour < 15:
        return None, None, None, None, 0, "Открытие США — пропуск"

    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция низкая ({corr:.2f})"

    if red_news_until > now:
        prev_price = df.iloc[-2]["close"]
        if ((price - prev_price) / prev_price < -RED_NEWS_DROP and
                row["volume"] > df.iloc[-2]["volume"] * RED_NEWS_VOL):
            red_news_until = now + RED_NEWS_BLOCK
            log.warning("🔴 Красные новости!")

    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        return "LONG", entry, sl, tp, 5.0, "🧪 FORCE TEST"

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
    funding_avg = get_funding_avg()
    news_sent = get_news_sentiment()
    _, btc_dom_change = get_btc_dominance()
    gas_price = get_gas_price()
    eth_trending = get_trending()
    _, stable_chg = get_stablecoin_supply()
    important_day = is_important_economic_day()
    ema50_200_diff, ema_4h_bull = get_4h_trend()
    whales_detected, _ = detect_whales()

    obv_div = 0
    if not np.isnan(row.get("OBV", float("nan"))) and not np.isnan(row.get("OBV_ma", float("nan"))):
        if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]:
            obv_div = -1
        elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]:
            obv_div = 1

    L = S = 0.0

    # ═══════════════════════════════════════════════
    # ЯДРО — вес 1.0
    # ═══════════════════════════════════════════════
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

    # ═══════════════════════════════════════════════
    # СРЕДНИЕ — вес 0.5
    # ═══════════════════════════════════════════════
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

    # ═══════════════════════════════════════════════
    # ВСПОМОГАТЕЛЬНЫЕ — вес 0.25
    # ═══════════════════════════════════════════════
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

    # ═══════════════════════════════════════════════
    # ШТРАФЫ И БОНУСЫ
    # ═══════════════════════════════════════════════
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

    if red_news_until > now:
        L = 0

    if long_liq + short_liq > LIQ_THRESHOLD:
        if L > S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    ema_l = row["EMA9"] > row["EMA21"] > row["EMA50"]
    ema_s = row["EMA9"] < row["EMA21"] < row["EMA50"]
    if ema_l and rsi < 45 and row["MACD"] > row["MACD_sig"] and row["CVD_up"]:
        L += 0.5
    if ema_s and rsi > 55 and row["MACD"] < row["MACD_sig"] and not row["CVD_up"]:
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

    # ── Проверка порога ────────────────────────────
    long_signal = L >= MIN_SCORE and (L - S) >= MIN_SCORE_DIFF
    short_signal = S >= MIN_SCORE and (S - L) >= MIN_SCORE_DIFF

    if not long_signal and not short_signal:
        return None, None, None, None, max(L, S), f"L:{L:.1f} S:{S:.1f} diff:{abs(L-S):.1f}"

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

    log.info(f"{direction} {score:.1f}/{MAX_SCORE} | {entry:.2f} SL:{sl:.2f} TP:{tp:.2f} ADX:{adx:.1f}")
    return direction, entry, sl, tp, score, f"L:{L:.1f} S:{S:.1f} | ADX:{adx:.1f} | F&G:{fear_greed}"

# ══════════════════════════════════════════════════════
# ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ
# ══════════════════════════════════════════════════════
def check_closed_positions():
    global stats, active_positions, MIN_SCORE, losses_in_row
    try:
        current_pos = okx_get_positions()
        current_price = float(requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=5
        ).json()["price"])

        for order_id, pos in list(active_positions.items()):
            direction = pos["direction"]
            entry = pos["entry"]
            hold_hours = (time.time() - pos["open_time"]) / 3600

            if hold_hours > MAX_HOLD_HOURS:
                in_profit = (direction == "LONG" and current_price > entry) or \
                            (direction == "SHORT" and current_price < entry)
                if in_profit:
                    log.info("⏰ Закрытие по времени (в плюс)")
                    okx_close_market(pos["pos_side"], pos["total_qty"])

            still_open = any(float(p.get("pos", 0)) != 0 for p in current_pos)

            if not still_open:
                if pos.get("sl_order_id"):
                    okx_cancel_algo(pos["sl_order_id"])
                if pos.get("tp_order_id"):
                    okx_cancel_algo(pos["tp_order_id"])

                history = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP", "instId": SYMBOL, "state": "filled",
                    "begin": str(int((pos["open_time"] - 60) * 1000)),
                    "end": str(int(time.time() * 1000)), "limit": "50"
                })

                if history.get("code") != "0" or not history.get("data"):
                    continue

                total_pnl = 0
                close_reason = "РЫНОК"

                for h in history["data"]:
                    if h.get("side") == pos["cls_side"] and h.get("posSide") == pos["pos_side"]:
                        avg_px = float(h.get("avgPx", 0))
                        qty = float(h.get("sz", 0))
                        exec_t = float(h.get("cTime", 0)) / 1000
                        if avg_px > 0 and qty > 0 and exec_t > pos["open_time"]:
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
                            pos_val = ORDER_USDT * LEVERAGE * (qty / pos.get("total_qty", 1))
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
                            log.info(f"⚠️ 3 убытка — MIN_SCORE → {MIN_SCORE}")
                            send_telegram(f"⚠️ 3 убытка подряд\nMIN_SCORE → {MIN_SCORE}")

                    stats["total"] += 1
                    stats["total_profit"] += total_pnl
                    update_equity(total_pnl)
                    save_stats()

                    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    avg_pnl = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
                    emoji = "✅" if is_win else "❌"

                    send_telegram(
                        f"{emoji} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                        f"📈 Направление: {direction}\n"
                        f"💰 Вход: {entry:.2f}\n"
                        f"📊 Причина: {close_reason}\n"
                        f"💎 P&L: {total_pnl:+.2f} USDT\n\n"
                        f"📈 <b>СТАТИСТИКА:</b>\n"
                        f"🔹 Всего: {stats['total']}\n"
                        f"✅ Прибыльных: {stats['wins']} (+{stats['total_profit_sum']:.2f})\n"
                        f"❌ Убыточных: {stats['losses']} (-{stats['total_loss_sum']:.2f})\n"
                        f"💰 P&L итого: {stats['total_profit']:+.2f} USDT\n"
                        f"📉 Макс просадка: {stats['max_drawdown']:.2f} USDT\n"
                        f"📊 Средний P&L: {avg_pnl:+.2f} USDT\n"
                        f"🎯 Винрейт: {winrate:.1f}%\n"
                        f"⚙️ MIN_SCORE: {MIN_SCORE}"
                    )
                    del active_positions[order_id]
                    save_active_positions()
    except Exception as e:
        log.error(f"check_closed: {e}")

# ══════════════════════════════════════════════════════
# ШКАЛА БАЛЛОВ
# ══════════════════════════════════════════════════════
def score_bar(score):
    filled = min(10, round(score / MAX_SCORE * 10))
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 8:
        emoji = "🟢"
    elif score >= 6:
        emoji = "🟡"
    elif score >= 4:
        emoji = "🟠"
    else:
        emoji = "🔴"
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 8:
        return "🟢"
    elif score >= 6:
        return "🟡"
    elif score >= 4:
        return "🟠"
    else:
        return "🔴"

# ══════════════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════════════
def run_scan():
    global last_heartbeat_time, pause_until, yesterday_high, yesterday_low
    now = time.time()

    if now < pause_until:
        log.info(f"⏸️ Пауза {int((pause_until-now)/60)} мин")
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

    direction, entry, sl, tp, score, reason = get_signal(df, funding, ob, btc_mom, btc_dir)
    log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f}/{MAX_SCORE} | MIN:{MIN_SCORE}")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_get_balance()
        pos = okx_get_positions()
        hour = datetime.now(timezone.utc).hour
        session = "🌙 Ночь" if 1 <= hour < 6 else ("🇬🇧 Лондон" if hour < 13 else "🇺🇸 США")
        fear_greed = get_fear_greed()
        long_short = get_long_short_ratio()
        cb_premium = get_coinbase_premium()
        _, eth_btc_chg = get_eth_btc_ratio()
        long_liq, short_liq = get_liquidations()
        gas = get_gas_price()
        _, btc_dom = get_btc_dominance()
        important_day = is_important_economic_day()
        winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

        l_num = s_num = 0.0
        if "L:" in reason:
            try:
                l_num = float(reason.split("L:")[1].split(" ")[0])
                s_num = float(reason.split("S:")[1].split(" ")[0])
            except:
                pass

        sig_status = (
            f"{direction} {score_color(score)} <b>{score:.1f}</b>"
            if direction else
            f"нет {score_color(max(l_num, s_num))} <b>{max(l_num, s_num):.1f}</b>"
        )

        send_telegram(
            f"❤️ <b>Heartbeat v5.2</b> {'⚠️' if important_day else ''}\n\n"
            f"💰 ETH: <b>{price:.2f}</b> ATR:{atr_val:.2f}\n"
            f"😱 F&G:{fear_greed} | L/S:{long_short:.2f}\n"
            f"🏦 CB:{cb_premium:+.3f}% | ETH/BTC:{eth_btc_chg:+.2f}%\n"
            f"💥 Liq: L={long_liq:.2f}M S={short_liq:.2f}M\n"
            f"⛽ Gas:{gas:.0f} | BTC.D:{btc_dom:.1f}%\n"
            f"🌍 {session} | 💳 Баланс:{bal:.2f} USDT | Поз:{len(pos)}\n"
            f"🎯 Сигнал:{sig_status} L:{l_num:.1f} S:{s_num:.1f}\n"
            f"⚙️ MIN_SCORE:{MIN_SCORE} (база:{BASE_MIN_SCORE})\n"
            f"📊 {stats['total']} сд | ✅ {stats['wins']} ({winrate:.1f}%) | P&L:{stats['total_profit']:+.2f}\n"
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
        f"📊 R/R: {TP_MARGIN_PCT/SL_MARGIN_PCT:.1f}:1",
        f"⚙️ MIN: {MIN_SCORE}",
        "",
        f"📊 {reason}",
    ]

    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res["ok"]:
            losses_in_row = 0
            sl_s = "✅" if res.get("sl_ok") else "❌"
            tp_s = "✅" if res.get("tp_ok") else "❌"
            msg += [
                f"✅ <b>ИСПОЛНЕНО НА OKX</b>",
                f"📦 Контрактов: {res['total_qty']}",
                f"⚙️ SL: {sl_s} | TP: {tp_s}",
                f"🆔 OrderID: {res['orderId']}",
            ]
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ {MAX_LOSSES} ошибки — пауза {PAUSE_LOSSES//60} мин")
            msg += [f"❌ Ошибка [{res['step']}]: {res['msg']}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт v5.2")
    bal = okx_get_balance()
    stats["current_equity"] = bal
    stats["peak_equity"] = bal
    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0

    send_telegram(
        f"🚀 <b>OKX Scalp Bot v5.2</b>\n\n"
        f"⚙️ Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"🎯 MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE} (diff ≥ {MIN_SCORE_DIFF})\n"
        f"📐 SL: -{int(SL_MARGIN_PCT*100)}% | TP: +{int(TP_MARGIN_PCT*100)}% R/R:{TP_MARGIN_PCT/SL_MARGIN_PCT:.1f}:1\n"
        f"💳 Баланс: {bal:.2f} USDT\n"
        f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%)\n\n"
        f"🆕 <b>Система v5.2:</b>\n"
        f"• Градуированные веса (Ядро/Средние/Вспомог)\n"
        f"• ADX фильтр слабых трендов\n"
        f"• Автосужение MIN_SCORE\n"
        f"• Корреляция ETH/BTC\n"
        f"• IsolationForest (киты)\n"
        f"• Ночной множитель позиции ×0.5\n"
        f"• Защита от красных новостей\n"
        f"• Исправлены send_telegram и okx_post"
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
