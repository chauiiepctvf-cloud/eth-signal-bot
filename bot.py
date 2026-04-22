import os
import time
import logging
import threading
import hashlib
import hmac
import base64
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from flask import Flask
from sklearn.ensemble import IsolationForest

# ══════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════
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

# ── ТЕСТОВЫЕ НАСТРОЙКИ ──────────────────────
MIN_SCORE = 2.0
MAX_SCORE = 7.0
MIN_SCORE_DIFF = 1.0

SL_MARGIN_PCT = 0.30
TP_MARGIN_PCT = 0.20
TRAILING_ACTIVATE_PCT = 0.10
TRAILING_CALLBACK = 0.003

ATR_MIN_PCT = 0.001
ATR_MAX_PCT = 0.05
MAX_LOSSES = 4
PAUSE_LOSSES = 60 * 60
FORCE_TEST = os.environ.get("FORCE_TEST", "false").lower() == "true"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    return f"OKX Scalp Bot v4.3 [{mode}] | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []
last_ob = 0
yesterday_high = 0
yesterday_low = 0
force_test_done = False

# Кэш
cache = {
    "fear_greed": {"value": 50, "ts": 0},
    "long_short": {"value": 1.0, "ts": 0},
    "taker_ratio": {"value": 1.0, "ts": 0},
    "open_interest": {"value": 0, "change": 0, "ts": 0},
    "dxy": {"value": 100, "change": 0, "ts": 0},
    "vix": {"value": 20, "ts": 0},
    "usdt_dominance": {"value": 5.0, "change": 0, "ts": 0},
    "google_trends": {"value": 50, "ts": 0},
    "coinbase_premium": {"value": 0.0, "ts": 0},
    "eth_btc_ratio": {"value": 0.0, "change": 0.0, "ts": 0},
    "liquidations": {"long_liq": 0.0, "short_liq": 0.0, "ts": 0},
    "gas_price": {"value": 20, "ts": 0},
    "btc_dominance": {"value": 50, "change": 0, "ts": 0},
    "total_mcap": {"value": 0, "change": 0, "ts": 0},
    "microgpt": {"sentiment": 0.0, "confidence": 0, "ts": 0},
}

stats = {
    "total": 0,
    "wins": 0,
    "losses": 0,
    "total_profit": 0.0,
    "total_profit_sum": 0.0,
    "total_loss_sum": 0.0,
    "max_drawdown": 0.0,
    "peak_equity": 0.0,
    "current_equity": 0.0
}
STATS_FILE = "bot_stats.json"

def load_stats():
    global stats
    try:
        with open(STATS_FILE, "r") as f:
            loaded = json.load(f)
            for k in stats:
                if k in loaded:
                    stats[k] = loaded[k]
        log.info(f"📊 Статистика загружена: {stats['total']} сделок")
    except:
        log.info("📊 Нет сохранённой статистики, начинаем с нуля")

def save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f)
    except Exception as e:
        log.error(f"Ошибка сохранения статистики: {e}")

def update_equity(pnl):
    stats["current_equity"] += pnl
    if stats["current_equity"] > stats["peak_equity"]:
        stats["peak_equity"] = stats["current_equity"]
    drawdown = stats["peak_equity"] - stats["current_equity"]
    if drawdown > stats["max_drawdown"]:
        stats["max_drawdown"] = drawdown

active_positions = {}

def load_active_positions():
    global active_positions
    try:
        with open("active_positions.json", "r") as f:
            active_positions = json.load(f)
        log.info(f"📋 Загружено {len(active_positions)} позиций")
    except:
        log.info("📋 Нет сохранённых позиций")

def save_active_positions():
    try:
        with open("active_positions.json", "w") as f:
            json.dump(active_positions, f)
    except Exception as e:
        log.error(f"Ошибка сохранения позиций: {e}")

load_stats()
load_active_positions()

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
    hdr = _sign(_ts(), "GET", path + qs)
    try:
        return requests.get(OKX_BASE + path + qs, headers=hdr, timeout=10).json()
    except Exception as e:
        log.error(f"OKX GET {path}: {e}")
        return {}

def okx_post(path, body):
    data = json.dumps(body)
    hdr = _sign(_ts(), "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        if d.get("code") != "0":
            log.warning(f"OKX {path} → {d.get('code')} {d.get('msg', '')}")
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
        return {"code": "0"}
    return okx_post("/api/v5/trade/cancel-algo-order", {
        "instId": SYMBOL, "algoId": algo_id
    })

def okx_close_position_market(pos_side, total_qty):
    cls_side = "sell" if pos_side == "long" else "buy"
    return okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "market", "sz": str(total_qty)
    })

def okx_place_order(direction, entry, sl, tp, score):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"
    
    multiplier = 1.5 if score >= 5.0 else 1.0
    effective_usdt = ORDER_USDT * multiplier
    total_qty = max(1, round(effective_usdt * LEVERAGE / entry / 0.01))
    
    log.info(f"▶ {direction} qty={total_qty} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f}")
    
    r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": side, "posSide": pos_side,
        "ordType": "market", "sz": str(total_qty),
    })
    if r.get("code") != "0":
        msg = r.get("msg", "Unknown error")
        if r.get("data") and len(r["data"]) > 0:
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}
    
    order_id = r["data"][0].get("ordId", "")
    log.info(f"✅ Открыта позиция, ordId={order_id}")
    
    active_positions[order_id] = {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "total_qty": total_qty, "pos_side": pos_side, "cls_side": cls_side,
        "open_time": time.time(), "trailing_activated": False,
        "sl_order_id": None, "tp_order_id": None,
        "multiplier": multiplier
    }
    save_active_positions()
    time.sleep(3)
    
    sl_ok = False
    tp_ok = False
    
    # 3 попытки для SL
    for attempt in range(3):
        sl_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1",
            "slTriggerPxType": "last"
        })
        if sl_r.get("code") == "0":
            active_positions[order_id]["sl_order_id"] = sl_r["data"][0].get("algoId", "")
            sl_ok = True
            log.info(f"✅ SL установлен: {sl:.2f}")
            break
        else:
            log.warning(f"⚠️ SL попытка {attempt+1}: {sl_r.get('msg')}")
            time.sleep(1)
    
    # 3 попытки для TP
    for attempt in range(3):
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(total_qty),
            "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1",
            "tpTriggerPxType": "last"
        })
        if tp_r.get("code") == "0":
            active_positions[order_id]["tp_order_id"] = tp_r["data"][0].get("algoId", "")
            tp_ok = True
            log.info(f"✅ TP установлен: {tp:.2f}")
            break
        else:
            log.warning(f"⚠️ TP попытка {attempt+1}: {tp_r.get('msg')}")
            time.sleep(1)
    
    save_active_positions()
    
    if not sl_ok:
        log.error(f"❌ SL НЕ УСТАНОВЛЕН после 3 попыток")
    if not tp_ok:
        log.error(f"❌ TP НЕ УСТАНОВЛЕН после 3 попыток")
    
    return {
        "ok": True, "orderId": order_id, "total_qty": total_qty,
        "algo_ok": sl_ok or tp_ok, "sl_ok": sl_ok, "tp_ok": tp_ok
    }

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
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}",
            timeout=8
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

def get_fear_greed():
    now = time.time()
    if now - cache["fear_greed"]["ts"] < 3600:
        return cache["fear_greed"]["value"]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        v = int(r.json()["data"][0]["value"])
        cache["fear_greed"] = {"value": v, "ts": now}
        return v
    except:
        return 50

def get_long_short_ratio():
    now = time.time()
    if now - cache["long_short"]["ts"] < 300:
        return cache["long_short"]["value"]
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        )
        v = float(r.json()[0]["longShortRatio"])
        cache["long_short"] = {"value": v, "ts": now}
        return v
    except:
        return 1.0

def get_taker_ratio():
    now = time.time()
    if now - cache["taker_ratio"]["ts"] < 300:
        return cache["taker_ratio"]["value"]
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/takerlongshortRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        )
        v = float(r.json()[0]["buySellRatio"])
        cache["taker_ratio"] = {"value": v, "ts": now}
        return v
    except:
        return 1.0

def get_open_interest():
    now = time.time()
    if now - cache["open_interest"]["ts"] < 300:
        return cache["open_interest"]["value"], cache["open_interest"]["change"]
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT", timeout=10)
        oi = float(r.json()["openInterest"])
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
        v = 100.0
        chg = 0.0
        cache["dxy"] = {"value": v, "change": chg, "ts": now}
        return v, chg
    except:
        return 100, 0

def get_vix():
    now = time.time()
    if now - cache["vix"]["ts"] < 3600:
        return cache["vix"]["value"]
    try:
        v = 20.0
        cache["vix"] = {"value": v, "ts": now}
        return v
    except:
        return 20

def get_usdt_dominance():
    now = time.time()
    if now - cache["usdt_dominance"]["ts"] < 600:
        return cache["usdt_dominance"]["value"], cache["usdt_dominance"]["change"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        dom = r.json()["data"]["market_cap_percentage"]["usdt"]
        prev = cache["usdt_dominance"]["value"]
        chg = dom - prev if prev > 0 else 0
        cache["usdt_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except:
        return 5.0, 0

def is_important_economic_day():
    today = datetime.now(timezone.utc)
    if today.weekday() == 4 and today.day <= 7:
        return True
    for m, d in [(3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10)]:
        if today.month == m and today.day == d:
            return True
    return False

def get_google_trends_eth():
    now = time.time()
    if now - cache["google_trends"]["ts"] < 3600:
        return cache["google_trends"]["value"]
    try:
        df = get_klines(SYMBOL_BN, "1h", 24)
        if df is not None:
            avg = df["volume"].mean()
            recent = df["volume"].tail(4).mean()
            v = min(100, max(0, (recent / avg * 50) if avg > 0 else 50))
            cache["google_trends"] = {"value": v, "ts": now}
            return v
    except:
        pass
    return 50

def get_coinbase_premium():
    now = time.time()
    if now - cache["coinbase_premium"]["ts"] < 60:
        return cache["coinbase_premium"]["value"]
    try:
        cb = requests.get("https://api.exchange.coinbase.com/products/ETH-USD/ticker", timeout=8).json()
        cb_price = float(cb["price"])
        bn = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=8).json()
        bn_price = float(bn["price"])
        premium = round((cb_price - bn_price) / bn_price * 100, 4)
        cache["coinbase_premium"] = {"value": premium, "ts": now}
        return premium
    except:
        return 0.0

def get_eth_btc_ratio():
    now = time.time()
    if now - cache["eth_btc_ratio"]["ts"] < 180:
        return cache["eth_btc_ratio"]["value"], cache["eth_btc_ratio"]["change"]
    try:
        r = requests.get("https://api.binance.com/api/v3/klines?symbol=ETHBTC&interval=5m&limit=4", timeout=8).json()
        curr = float(r[-1][4])
        prev = float(r[-4][4])
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
        r = requests.get(f"https://fapi.binance.com/fapi/v1/forceOrders?symbol={SYMBOL_BN}&limit=200", timeout=10).json()
        long_liq = 0.0
        short_liq = 0.0
        cutoff = (now - 3600) * 1000
        for order in r:
            if float(order.get("time", 0)) < cutoff:
                continue
            qty = float(order.get("origQty", 0)) * float(order.get("price", 0))
            if order.get("side") == "SELL":
                long_liq += qty
            else:
                short_liq += qty
        cache["liquidations"] = {
            "long_liq": round(long_liq / 1_000_000, 3),
            "short_liq": round(short_liq / 1_000_000, 3),
            "ts": now
        }
        return cache["liquidations"]["long_liq"], cache["liquidations"]["short_liq"]
    except:
        return 0.0, 0.0

def get_gas_price():
    now = time.time()
    if now - cache["gas_price"]["ts"] < 60:
        return cache["gas_price"]["value"]
    try:
        r = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
        v = float(r.json()["result"]["ProposeGasPrice"])
        cache["gas_price"] = {"value": v, "ts": now}
        return v
    except:
        return 20

def get_btc_dominance():
    now = time.time()
    if now - cache["btc_dominance"]["ts"] < 600:
        return cache["btc_dominance"]["value"], cache["btc_dominance"]["change"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        dom = r.json()["data"]["market_cap_percentage"]["btc"]
        prev = cache["btc_dominance"]["value"]
        chg = dom - prev if prev > 0 else 0
        cache["btc_dominance"] = {"value": dom, "change": chg, "ts": now}
        return dom, chg
    except:
        return 50, 0

def get_total_mcap():
    now = time.time()
    if now - cache["total_mcap"]["ts"] < 600:
        return cache["total_mcap"]["value"], cache["total_mcap"]["change"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        mcap = r.json()["data"]["total_market_cap"]["usd"]
        prev = cache["total_mcap"]["value"]
        chg = ((mcap - prev) / prev * 100) if prev > 0 else 0
        cache["total_mcap"] = {"value": mcap, "change": chg, "ts": now}
        return mcap, chg
    except:
        return 0, 0

def get_eth_btc_correlation():
    try:
        df_eth = get_klines("ETHUSDT", "5m", 12)
        df_btc = get_klines("BTCUSDT", "5m", 12)
        if df_eth is None or df_btc is None:
            return 1.0
        eth_ret = df_eth["close"].pct_change().dropna()
        btc_ret = df_btc["close"].pct_change().dropna()
        if len(eth_ret) < 5:
            return 1.0
        corr = eth_ret.corr(btc_ret)
        return corr if not np.isnan(corr) else 1.0
    except:
        return 1.0

def get_microgpt_sentiment():
    """MicroGPT API — предсказание направления"""
    now = time.time()
    if now - cache["microgpt"]["ts"] < 60:
        return cache["microgpt"]["sentiment"], cache["microgpt"]["confidence"]
    try:
        r = requests.get("https://api.microgpt.ai/v1/predict?symbol=ETHUSDT&timeframe=5m", timeout=10)
        data = r.json()
        sentiment = data.get("sentiment", 0.0)
        confidence = data.get("confidence", 0)
        cache["microgpt"] = {"sentiment": sentiment, "confidence": confidence, "ts": now}
        log.info(f"🤖 MicroGPT: sentiment={sentiment:.2f}, conf={confidence}%")
        return sentiment, confidence
    except Exception as e:
        log.error(f"MicroGPT error: {e}")
        return 0.0, 0

def detect_whales_in_orderbook():
    """Isolation Forest для поиска китов в стакане"""
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = [[float(b[0]), float(b[1])] for b in d["bids"][:10]]
        asks = [[float(a[0]), float(a[1])] for a in d["asks"][:10]]
        X = np.array(bids + asks)
        
        if len(X) < 5:
            return False
        
        model = IsolationForest(contamination=0.1, random_state=42)
        preds = model.fit_predict(X)
        
        if -1 in preds:
            log.info("🐋 Обнаружены киты в стакане")
            return True
        return False
    except Exception as e:
        log.error(f"Whale detection error: {e}")
        return False

def check_closed_positions():
    global stats, active_positions
    try:
        current_positions = okx_get_positions()
        current_price = float(requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}", timeout=5
        ).json()["price"])
        
        for order_id, pos_info in list(active_positions.items()):
            direction = pos_info["direction"]
            entry = pos_info["entry"]
            
            # Проверка трейлинга
            if not pos_info.get("trailing_activated", False):
                if direction == "LONG":
                    pnl_pct = (current_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - current_price) / entry * 100
                
                margin_pnl_pct = pnl_pct * LEVERAGE
                
                if margin_pnl_pct >= TRAILING_ACTIVATE_PCT * 100:
                    log.info(f"🎯 Активируем трейлинг, P&L={margin_pnl_pct:.1f}% маржи")
                    
                    if pos_info.get("sl_order_id"):
                        okx_cancel_algo(pos_info["sl_order_id"])
                    if pos_info.get("tp_order_id"):
                        okx_cancel_algo(pos_info["tp_order_id"])
                    
                    trail = okx_post("/api/v5/trade/order-algo", {
                        "instId": SYMBOL, "tdMode": "cross",
                        "side": pos_info["cls_side"], "posSide": pos_info["pos_side"],
                        "ordType": "move_order_stop", "sz": str(pos_info["total_qty"]),
                        "callbackRatio": str(TRAILING_CALLBACK), "callbackSpread": "0.001"
                    })
                    
                    if trail.get("code") == "0":
                        pos_info["trailing_activated"] = True
                        pos_info["trail_algo_id"] = trail["data"][0].get("algoId", "")
                        save_active_positions()
                        send_telegram(
                            f"🔒 <b>ТРЕЙЛИНГ АКТИВИРОВАН</b>\n"
                            f"📈 Позиция {direction}\n"
                            f"💰 Текущий P&L: +{margin_pnl_pct:.1f}% маржи\n"
                            f"📊 Стоп движется за ценой (отступ {TRAILING_CALLBACK*100:.1f}%)"
                        )

            # Программный контроль (Fallback)
            still_open = False
            for p in current_positions:
                if float(p.get("pos", 0)) != 0:
                    still_open = True
                    break
            
            if still_open:
                should_close = False
                close_reason = ""
                if direction == "LONG":
                    if current_price >= pos_info["tp"]:
                        should_close = True
                        close_reason = f"🎯 ТЕЙК (программно, {current_price:.2f} >= {pos_info['tp']:.2f})"
                    elif current_price <= pos_info["sl"]:
                        should_close = True
                        close_reason = f"🛑 СТОП (программно, {current_price:.2f} <= {pos_info['sl']:.2f})"
                else:
                    if current_price <= pos_info["tp"]:
                        should_close = True
                        close_reason = f"🎯 ТЕЙК (программно, {current_price:.2f} <= {pos_info['tp']:.2f})"
                    elif current_price >= pos_info["sl"]:
                        should_close = True
                        close_reason = f"🛑 СТОП (программно, {current_price:.2f} >= {pos_info['sl']:.2f})"
                
                if should_close:
                    log.warning(f"⚠️ Fallback: {close_reason}. Закрываем принудительно.")
                    res = okx_close_position_market(pos_info["pos_side"], pos_info["total_qty"])
                    if res.get("code") == "0":
                        send_telegram(f"⚡ <b>FALLBACK ЗАКРЫТИЕ</b>\n{close_reason}")
                        still_open = False
            
            # Расчёт результата
            if not still_open:
                history = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP", "instId": SYMBOL, "state": "filled",
                    "begin": str(int((pos_info["open_time"] - 60) * 1000)),
                    "end": str(int(time.time() * 1000)), "limit": "50"
                })
                
                if history.get("code") != "0" or not history.get("data"):
                    continue
                
                total_pnl = 0
                close_reason = "📊 РЫНОК (ручное)"
                
                for h in history["data"]:
                    if h.get("side") == pos_info["cls_side"] and h.get("posSide") == pos_info["pos_side"]:
                        avg_px = float(h.get("avgPx", 0))
                        qty = float(h.get("sz", 0))
                        exec_time = float(h.get("cTime", 0)) / 1000 if h.get("cTime") else 0
                        
                        if avg_px > 0 and qty > 0 and exec_time > pos_info["open_time"]:
                            if direction == "LONG":
                                pnl_pct = (avg_px - entry) / entry * 100
                                if avg_px >= pos_info.get("tp", 0) * 0.99:
                                    close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}% маржи)"
                                elif avg_px <= pos_info.get("sl", 0) * 1.01:
                                    close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}% маржи)"
                            else:
                                pnl_pct = (entry - avg_px) / entry * 100
                                if avg_px <= pos_info.get("tp", 0) * 1.01:
                                    close_reason = f"🎯 ТЕЙК (+{int(TP_MARGIN_PCT*100)}% маржи)"
                                elif avg_px >= pos_info.get("sl", 0) * 0.99:
                                    close_reason = f"🛑 СТОП (-{int(SL_MARGIN_PCT*100)}% маржи)"
                            
                            if pos_info.get("trailing_activated"):
                                close_reason = "🔒 ТРЕЙЛИНГ-СТОП"
                            
                            position_value = ORDER_USDT * pos_info.get("multiplier", 1.0) * LEVERAGE * (qty / pos_info.get("total_qty", 1))
                            pnl_usdt = pnl_pct / 100 * position_value
                            total_pnl += pnl_usdt
                
                if total_pnl != 0:
                    if total_pnl > 0:
                        stats["wins"] += 1
                        stats["total_profit_sum"] += total_pnl
                    else:
                        stats["losses"] += 1
                        stats["total_loss_sum"] += abs(total_pnl)
                    
                    stats["total"] += 1
                    stats["total_profit"] += total_pnl
                    update_equity(total_pnl)
                    save_stats()
                    
                    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    emoji = "✅" if total_pnl > 0 else "❌"
                    avg_pnl = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
                    
                    send_telegram(
                        f"{emoji} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
                        f"📈 Направление: {direction}\n"
                        f"💰 Вход: {entry:.2f}\n"
                        f"📊 Причина: {close_reason}\n"
                        f"💎 P&L: {total_pnl:+.2f} USDT\n\n"
                        f"📈 <b>СТАТИСТИКА:</b>\n"
                        f"🔹 Всего сделок: {stats['total']}\n"
                        f"✅ Прибыльных: {stats['wins']} | Сумма: +{stats['total_profit_sum']:.2f} USDT\n"
                        f"❌ Убыточных: {stats['losses']} | Сумма: -{stats['total_loss_sum']:.2f} USDT\n"
                        f"💰 Общий P&L: {stats['total_profit']:+.2f} USDT\n"
                        f"📉 Макс. просадка: {stats['max_drawdown']:.2f} USDT\n"
                        f"📊 Средний P&L: {avg_pnl:+.2f} USDT\n"
                        f"🎯 Винрейт: {winrate:.1f}%"
                    )
                    
                    log.info(f"✅ Сделка закрыта: {direction} P&L={total_pnl:.2f}")
                    del active_positions[order_id]
                    save_active_positions()
                    
    except Exception as e:
        log.error(f"check_closed: {e}")

def calc(df):
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

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

    body = (df["close"] - df["open"]).abs()
    lw = df[["open", "close"]].min(axis=1) - df["low"]
    uw = df["high"] - df[["open", "close"]].max(axis=1)
    df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
    df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    return df

def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low, force_test_done
    
    if df is None or len(df) < 3:
        return None, None, None, None, 0, "Нет данных"
    
    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    
    candle_time = row["candle_time"]
    seconds_since_open = (datetime.now(timezone.utc) - candle_time.replace(tzinfo=timezone.utc)).total_seconds()
    if seconds_since_open < 60:
        return None, None, None, None, 0, f"Свеча свежая ({int(seconds_since_open)}с)"
    
    hour = datetime.now(timezone.utc).hour
    if 13 <= hour < 15:
        return None, None, None, None, 0, "Открытие США — не входим"
    
    corr = get_eth_btc_correlation()
    if corr < 0.3:
        return None, None, None, None, 0, f"Корреляция ETH/BTC низкая ({corr:.2f})"

    if FORCE_TEST and not force_test_done:
        force_test_done = True
        entry = price
        sl = round(entry * (1 - SL_MARGIN_PCT / LEVERAGE), 2)
        tp = round(entry * (1 + TP_MARGIN_PCT / LEVERAGE), 2)
        log.info("🧪 FORCE TEST ACTIVATED")
        return "LONG", entry, sl, tp, 5.0, "🧪 FORCE TEST"

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, f"Рынок мёртвый (ATR {atr:.2f})"

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
    usdt_dom, usdt_dom_change = get_usdt_dominance()
    trends = get_google_trends_eth()
    important_day = is_important_economic_day()
    cb_premium = get_coinbase_premium()
    eth_btc, eth_btc_chg = get_eth_btc_ratio()
    long_liq, short_liq = get_liquidations()
    gas_price = get_gas_price()
    btc_dom, btc_dom_change = get_btc_dominance()
    total_mcap, total_mcap_change = get_total_mcap()
    micro_sentiment, micro_conf = get_microgpt_sentiment()
    whales = detect_whales_in_orderbook()

    L = S = 0.0

    # 1. EMA
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 1.0
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 1.0
    elif row["EMA9"] > row["EMA21"]:
        L += 0.5
    elif row["EMA9"] < row["EMA21"]:
        S += 0.5

    # 1H тренд
    if row["EMA9"] > row["EMA21"]:
        L += 0.25
    else:
        S += 0.25

    # 2. RSI
    if rsi < 35:
        L += 1.0
    elif rsi < 45:
        L += 0.5
    elif rsi > 65:
        S += 1.0
    elif rsi > 55:
        S += 0.5

    # 3. MACD
    if row["MACD_bull"]:
        L += 1.0
    elif row["MACD"] > row["MACD_sig"]:
        L += 0.5
    if row["MACD_bear"]:
        S += 1.0
    elif row["MACD"] < row["MACD_sig"]:
        S += 0.5

    # 4. CVD
    if row["CVD_up"]:
        L += 0.5
    else:
        S += 0.5

    # 5. Bollinger
    bp = row["BB_pct"]
    if bp < 0.1:
        L += 0.5
    elif bp > 0.9:
        S += 0.5

    # 6. VWAP
    if price < row["VWAP"]:
        L += 0.5
    else:
        S += 0.5

    # 7. Стакан
    if ob > 5 or ob_rising:
        L += 0.5
    if ob < -5 or ob_falling:
        S += 0.5
    if ob_delta > 3:
        L += 0.25
    elif ob_delta < -3:
        S += 0.25

    # 8. BTC
    if btc_mom > 0.2:
        L += 0.5
        S = max(0, S - 0.5)
    elif btc_mom < -0.2:
        S += 0.5
        L = max(0, L - 0.5)
    
    eth_dir = 1 if row["close"] > df.iloc[-2]["close"] else -1
    if eth_dir == btc_dir and btc_dir != 0:
        if eth_dir == 1:
            L += 0.25
        else:
            S += 0.25
    else:
        if L > S:
            L = max(0, L - 0.25)
        else:
            S = max(0, S - 0.25)

    # 9. Объём
    if row["vol_spike"]:
        if row["vol_dir"] > 0:
            L += 0.5
        elif row["vol_dir"] < 0:
            S += 0.5
    
    if row["vol_extreme"]:
        if L > S:
            L += 0.5
        else:
            S += 0.5

    # 10. Паттерны
    if row["hammer"]:
        L += 0.5
    if row["shooter"]:
        S += 0.5

    # 11. Фандинг
    if funding < -0.001:
        L += 0.5
    elif funding > 0.003:
        S += 0.5

    # 12. Вчерашние уровни
    if yesterday_high > 0 and yesterday_low > 0:
        if price < yesterday_low:
            L = max(0, L - 0.5)
        elif price > yesterday_high:
            S = max(0, S - 0.5)
        elif yesterday_low < price < yesterday_low * 1.01:
            L += 0.5
        elif yesterday_high * 0.99 < price < yesterday_high:
            S += 0.5

    # 13. Сессия
    if 1 <= hour < 6:
        if L >= S:
            L = max(0, L - 0.75)
        else:
            S = max(0, S - 0.75)

    # 14. Конфлюэнция
    if row["EMA9"] > row["EMA21"] > row["EMA50"] and rsi < 45 and row["MACD"] > row["MACD_sig"]:
        L += 0.75
    if row["EMA9"] < row["EMA21"] < row["EMA50"] and rsi > 55 and row["MACD"] < row["MACD_sig"]:
        S += 0.75

    # 15. Fear & Greed
    if fear_greed < 25:
        L += 0.75
    elif fear_greed < 45:
        L += 0.25
    elif fear_greed > 75:
        S += 0.75
    elif fear_greed > 60:
        S += 0.25

    # 16. L/S ratio
    if long_short > 2.5:
        S += 0.5
    elif long_short > 1.8:
        S += 0.25
    elif long_short < 0.8:
        L += 0.5
    elif long_short < 1.2:
        L += 0.25

    # 17. Taker ratio
    if taker_ratio > 1.3:
        L += 0.5
    elif taker_ratio > 1.1:
        L += 0.25
    elif taker_ratio < 0.7:
        S += 0.5
    elif taker_ratio < 0.9:
        S += 0.25

    # 18. OI
    if oi_change > 3:
        if price > df.iloc[-2]["close"]:
            L += 0.5
        else:
            S += 0.5
    elif oi_change < -3:
        if price > df.iloc[-2]["close"]:
            S += 0.25
        else:
            L += 0.25

    # 19. DXY
    if dxy_change > 0.3:
        S += 0.3
        L = max(0, L - 0.3)
    elif dxy_change < -0.3:
        L += 0.3
        S = max(0, S - 0.3)

    # 20. VIX
    if vix > 30:
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)

    # 21. USDT Dominance
    if usdt_dom_change > 0.2:
        S += 0.3
        L = max(0, L - 0.3)
    elif usdt_dom_change < -0.2:
        L += 0.3
        S = max(0, S - 0.3)

    # 22. Google Trends
    if trends > 70:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    # 23. Важный день
    if important_day:
        L = max(0, L - 1.0)
        S = max(0, S - 1.0)

    # 24. Coinbase Premium
    if cb_premium > 0.05:
        L += 0.5
    elif cb_premium > 0.02:
        L += 0.25
    elif cb_premium < -0.05:
        S += 0.5
    elif cb_premium < -0.02:
        S += 0.25

    # 25. ETH/BTC
    if eth_btc_chg > 0.3:
        L += 0.5
    elif eth_btc_chg > 0.1:
        L += 0.25
    elif eth_btc_chg < -0.3:
        S += 0.5
    elif eth_btc_chg < -0.1:
        S += 0.25

    # 26. Ликвидации
    liq_diff = short_liq - long_liq
    if liq_diff > 0.5:
        L += 0.5
    elif liq_diff > 0.2:
        L += 0.25
    elif liq_diff < -0.5:
        S += 0.5
    elif liq_diff < -0.2:
        S += 0.25

    # 27. Gas Price
    if gas_price > 50:
        if L > S:
            L += 0.25
        else:
            S += 0.25

    # 28. BTC Dominance
    if btc_dom_change > 0.2:
        S += 0.25
        L = max(0, L - 0.5)
    elif btc_dom_change < -0.2:
        L += 0.25
        S = max(0, S - 0.25)

    # 29. Total Market Cap
    if total_mcap_change > 1:
        L += 0.25
    elif total_mcap_change < -1:
        S += 0.25

    # 30. MicroGPT
    if micro_conf >= 60:
        if micro_sentiment > 0.5:
            L += 0.5
            log.info(f"🤖 MicroGPT LONG +0.5")
        elif micro_sentiment < -0.5:
            S += 0.5
            log.info(f"🤖 MicroGPT SHORT +0.5")

    # 31. Киты в стакане
    if whales:
        if L > S:
            L = max(0, L - 0.25)
        else:
            S = max(0, S - 0.25)
        log.info(f"🐋 Киты в стакане -0.25")

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

    log.info(f"{direction} {score:.1f}/7 | {entry:.2f} SL:{sl:.2f} TP:{tp:.2f}")
    reason = f"L:{L:.1f} S:{S:.1f} | F&G:{fear_greed} | MicroGPT:{micro_sentiment:.2f}"
    return direction, entry, sl, tp, score, reason

def score_bar(score):
    filled = round(score / MAX_SCORE * 10)
    if filled > 10:
        filled = 10
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 5.0:
        emoji = "🟢"
    elif score >= 3.5:
        emoji = "🟡"
    elif score >= 2.5:
        emoji = "🟠"
    else:
        emoji = "🔴"
    return f"{emoji} [{bar}] {score:.1f}/{MAX_SCORE}"

def score_color(score):
    if score >= 5.0:
        return "🟢"
    elif score >= 3.5:
        return "🟡"
    elif score >= 2.5:
        return "🟠"
    else:
        return "🔴"

def run_scan():
    global last_heartbeat_time, losses_in_row, pause_until, yesterday_high, yesterday_low
    now = time.time()

    if now < pause_until:
        log.info(f"⏸️ Пауза — осталось {int((pause_until - now) / 60)} мин")
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

    direction, entry, sl, tp, score, reason = get_signal(df, funding, ob, btc_mom, btc_dir)
    log.info(f"ETH:{price:.2f} | {direction or 'нет'} балл:{score:.1f}/7")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_get_balance()
        pos = okx_get_positions()
        hour = datetime.now(timezone.utc).hour
        session = "🌙 Ночь" if 1 <= hour < 6 else ("🇬🇧 Лондон" if hour < 13 else ("🇺🇸 NY" if hour < 15 else "🇺🇸 США"))
        winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
        
        fear_greed = get_fear_greed()
        long_short = get_long_short_ratio()
        cb_premium = get_coinbase_premium()
        _, eth_btc_chg = get_eth_btc_ratio()
        long_liq, short_liq = get_liquidations()
        gas = get_gas_price()
        btc_dom, _ = get_btc_dominance()
        corr = get_eth_btc_correlation()
        micro_sent, micro_conf = get_microgpt_sentiment()
        important_day = is_important_economic_day()
        important_str = "⚠️ Важный день" if important_day else ""
        
        l_num = s_num = 0.0
        if "L:" in reason and "S:" in reason:
            try:
                l_num = float(reason.split("L:")[1].split(" ")[0])
                s_num = float(reason.split("S:")[1].split(" ")[0])
            except:
                pass
        
        max_score_val = max(l_num, s_num)
        if direction:
            signal_status = f"{direction} {score_color(score)} <b>{score:.1f}</b>"
        else:
            signal_status = f"нет {score_color(max_score_val)} <b>{max_score_val:.1f}</b>"
        
        send_telegram(
            f"❤️ <b>Heartbeat v4.3</b> {important_str}\n\n"
            f"💰 ETH: <b>{price:.2f}</b>\n"
            f"😱 F&G: {fear_greed} | L/S: {long_short:.2f} | Corr: {corr:.2f}\n"
            f"🏦 CB: {cb_premium:+.3f}% | ETH/BTC: {eth_btc_chg:+.2f}%\n"
            f"💥 Liq: L={long_liq:.2f}M S={short_liq:.2f}M | Gas: {gas:.0f}\n"
            f"₿ BTC.D: {btc_dom:.1f}%\n"
            f"🤖 MicroGPT: {micro_sent:+.2f} ({micro_conf}%)\n"
            f"🌍 Сессия: {session}\n"
            f"💳 Баланс: {bal:.2f} USDT | Поз: {len(pos)}\n"
            f"🎯 Сигнал: {signal_status} | L: {l_num:.1f} S: {s_num:.1f}\n"
            f"📊 Стат: {stats['total']} | ✅ {stats['wins']} (+{stats['total_profit_sum']:.2f}) | ❌ {stats['losses']} (-{stats['total_loss_sum']:.2f}) | P&L: {stats['total_profit']:+.2f} USDT\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    if direction is None:
        return

    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    
    msg = [
        f"<b>[{mode}]</b>",
        f"{'↗️' if direction == 'LONG' else '↘️'} <b>SCALP {direction}</b>",
        f"",
        f"<b>Надёжность:</b> {score_bar(score)}",
        f"",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{int(SL_MARGIN_PCT*100)}% маржи)",
        f"🎯 Тейк: {tp:.2f} (+{int(TP_MARGIN_PCT*100)}% маржи)",
        f"🔒 Трейлинг: после +{int(TRAILING_ACTIVATE_PCT*100)}% маржи",
        f"",
        f"📊 {reason}",
    ]

    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp, score)
        if res["ok"]:
            losses_in_row = 0
            size_bonus = " (+50%)" if score >= 5.0 else ""
            msg += [
                f"✅ <b>ИСПОЛНЕНО{size_bonus}</b>",
                f"📦 Контрактов: {res['total_qty']}",
                f"⚙️ SL: {'✅' if res.get('sl_ok') else '❌'} | TP: {'✅' if res.get('tp_ok') else '❌'}"
            ]
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ Пауза {PAUSE_LOSSES // 60} мин")
            msg += [f"❌ Ошибка: {res['msg']}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт v4.3 — ИИ + Киты + Тест")
    bal = okx_get_balance()
    stats["current_equity"] = bal
    stats["peak_equity"] = bal
    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
    
    send_telegram(
        f"🚀 <b>OKX Scalp Bot v4.3</b>\n\n"
        f"🎭 Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
        f"⚙️ Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE}/7 (diff ≥ {MIN_SCORE_DIFF})\n"
        f"📐 SL: -{int(SL_MARGIN_PCT*100)}% | TP: +{int(TP_MARGIN_PCT*100)}%\n"
        f"🔒 Трейлинг: +{int(TRAILING_ACTIVATE_PCT*100)}%\n"
        f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} | ❌ {stats['losses']} ({winrate:.1f}%)\n\n"
        f"🆕 <b>Новое в v4.3:</b>\n"
        f"• MicroGPT AI — предсказание направления\n"
        f"• Isolation Forest — поиск китов в стакане\n"
        f"• TP/SL с 3 попытками установки\n"
        f"• Детект ручного закрытия\n"
        f"• MIN_SCORE = 2.0 (тест сигналов)"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
