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

# ══════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}  # убери для реала
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 15
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60          # скан каждые 3 минуты
HEARTBEAT_INTERVAL = 60 * 60    # раз в час

# ── ПАРАМЕТРЫ СИГНАЛА ──────────────────────
MIN_SCORE = 6        # повышенный порог
MAX_SCORE = 20       # максимум очков (увеличен из-за новых факторов)
MIN_SCORE_DIFF = 3   # минимальная разница между L и S

# ── TP / SL (проценты от входа) ─────────────
SL_PCT = 0.60        # стоп = -60% от входа
TP1_PCT = 0.40       # первый тейк = +40%
TP2_PCT = 1.00       # второй тейк = +100%
TP1_RATIO = 0.5      # закрываем 50% на TP1, стоп → БУ
TP2_RATIO = 0.5      # закрываем 50% на TP2

# ── ATR ФИЛЬТР ─────────────────────────────
ATR_MIN_PCT = 0.001  # 0.1% — убираем только мёртвый рынок
ATR_MAX_PCT = 0.05   # 5% — защита от кривых данных

# ── ЗАЩИТЫ ─────────────────────────────────
MAX_LOSSES = 4
PAUSE_LOSSES = 60 * 60   # 1 час паузы

FORCE_TEST = False

# ══════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    return f"OKX Scalp Bot [{mode}] | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════
# СОСТОЯНИЕ БОТА
# ══════════════════════════════════════════════
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []
last_ob = 0
yesterday_high = 0
yesterday_low = 0

# Кэш для внешних API (чтобы не дёргать каждые 3 минуты)
cache = {
    "fear_greed": {"value": 50, "ts": 0},
    "long_short": {"value": 1.0, "ts": 0},
    "taker_ratio": {"value": 1.0, "ts": 0},
    "open_interest": {"value": 0, "change": 0, "ts": 0},
    "sp500": {"value": 0, "change": 0, "ts": 0},
    "dxy": {"value": 0, "change": 0, "ts": 0},
    "vix": {"value": 0, "ts": 0},
    "usdt_dominance": {"value": 5.0, "change": 0, "ts": 0},
    "google_trends": {"value": 50, "ts": 0}
}

# Статистика сделок
stats = {
    "total": 0,
    "wins": 0,
    "losses": 0,
    "total_profit": 0.0
}

# Файл для сохранения статистики
STATS_FILE = "bot_stats.json"

def load_stats():
    global stats
    try:
        with open(STATS_FILE, "r") as f:
            stats = json.load(f)
        log.info(f"📊 Статистика загружена: {stats}")
    except:
        log.info("📊 Нет сохранённой статистики, начинаем с нуля")

def save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f)
    except Exception as e:
        log.error(f"Ошибка сохранения статистики: {e}")

# Активные позиции для отслеживания
active_positions = {}

def load_active_positions():
    global active_positions
    try:
        with open("active_positions.json", "r") as f:
            active_positions = json.load(f)
        log.info(f"📋 Загружено {len(active_positions)} активных позиций")
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

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
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

# ══════════════════════════════════════════════
# OKX API
# ══════════════════════════════════════════════
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
        log.info(f"OKX {path} → {d.get('code')} {d.get('msg', '')}")
        return d
    except Exception as e:
        log.error(f"OKX POST {path}: {e}")
        return {}

def okx_set_leverage():
    okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
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

def okx_amend_sl(order_id, new_sl, pos_side, cls_side):
    """Изменяет стоп-лосс существующего ордера"""
    return okx_post("/api/v5/trade/amend-algo-order", {
        "instId": SYMBOL,
        "algoId": order_id,
        "newSlTriggerPx": str(round(new_sl, 2))
    })

def okx_place_order(direction, entry, sl, tp1, tp2):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"
    
    total_qty = max(1, round(ORDER_USDT * LEVERAGE / entry / 0.01))
    qty1 = max(1, int(total_qty * TP1_RATIO))
    qty2 = total_qty - qty1
    
    log.info(f"▶ {direction} total:{total_qty} qty1:{qty1} qty2:{qty2} entry:{entry:.2f}")

    r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": side, "posSide": pos_side,
        "ordType": "market", "sz": str(total_qty),
    })
    if r.get("code") != "0":
        msg = r.get("msg", "")
        if r.get("data"):
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}
    
    order_id = r["data"][0].get("ordId", "—")
    log.info(f"✅ Открыта ordId:{order_id}")
    
    active_positions[order_id] = {
        "direction": direction, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "qty1": qty1, "qty2": qty2, "total_qty": total_qty,
        "pos_side": pos_side, "cls_side": cls_side,
        "open_time": time.time(), "stage": "open", "sl_order_id": None
    }
    save_active_positions()
    time.sleep(2)

    sl_r = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(total_qty),
        "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last",
    })
    if sl_r.get("code") == "0":
        active_positions[order_id]["sl_order_id"] = sl_r["data"][0].get("algoId", "")
        save_active_positions()
    
    okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(qty1),
        "tpTriggerPx": str(round(tp1, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
    })
    okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(qty2),
        "tpTriggerPx": str(round(tp2, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
    })
    
    return {"ok": True, "orderId": order_id, "total_qty": total_qty, "qty1": qty1, "qty2": qty2, "algo_ok": True}

# ══════════════════════════════════════════════
# ДАННЫЕ С БИРЖ
# ══════════════════════════════════════════════
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
            yesterday = df.iloc[-2]
            return float(yesterday["high"]), float(yesterday["low"])
    except:
        pass
    return 0, 0

# ══════════════════════════════════════════════
# НОВЫЕ МЕТРИКИ (БЕСПЛАТНЫЕ, БЕЗ КЛЮЧЕЙ)
# ══════════════════════════════════════════════

def get_fear_greed():
    """Fear & Greed Index от alternative.me (0-100)"""
    global cache
    now = time.time()
    if now - cache["fear_greed"]["ts"] < 3600:  # кэш на 1 час
        return cache["fear_greed"]["value"]
    
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()
        value = int(data["data"][0]["value"])
        cache["fear_greed"] = {"value": value, "ts": now}
        return value
    except Exception as e:
        log.error(f"Fear&Greed error: {e}")
        return 50

def get_long_short_ratio():
    """Long/Short Ratio топ-трейдеров Binance"""
    global cache
    now = time.time()
    if now - cache["long_short"]["ts"] < 300:  # кэш на 5 минут
        return cache["long_short"]["value"]
    
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        )
        data = r.json()
        value = float(data[0]["longShortRatio"])
        cache["long_short"] = {"value": value, "ts": now}
        return value
    except Exception as e:
        log.error(f"Long/Short error: {e}")
        return 1.0

def get_taker_ratio():
    """Taker Buy/Sell Volume Ratio"""
    global cache
    now = time.time()
    if now - cache["taker_ratio"]["ts"] < 300:
        return cache["taker_ratio"]["value"]
    
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/takerlongshortRatio?symbol=ETHUSDT&period=5m&limit=1",
            timeout=10
        )
        data = r.json()
        value = float(data[0]["buySellRatio"])
        cache["taker_ratio"] = {"value": value, "ts": now}
        return value
    except:
        return 1.0

def get_open_interest():
    """Open Interest и его изменение"""
    global cache
    now = time.time()
    if now - cache["open_interest"]["ts"] < 300:
        return cache["open_interest"]["value"], cache["open_interest"]["change"]
    
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT",
            timeout=10
        )
        data = r.json()
        oi = float(data["openInterest"])
        
        # Получаем предыдущее значение для расчёта изменения
        prev_oi = cache["open_interest"]["value"]
        change = ((oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0
        
        cache["open_interest"] = {"value": oi, "change": change, "ts": now}
        return oi, change
    except:
        return 0, 0

def get_sp500():
    """S&P 500 индекс и дневное изменение"""
    global cache
    now = time.time()
    if now - cache["sp500"]["ts"] < 3600:
        return cache["sp500"]["value"], cache["sp500"]["change"]
    
    try:
        # Используем Yahoo Finance
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=2d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        value = meta["regularMarketPrice"]
        prev_close = meta["previousClose"]
        change = ((value - prev_close) / prev_close * 100)
        
        cache["sp500"] = {"value": value, "change": change, "ts": now}
        return value, change
    except:
        return 0, 0

def get_dxy():
    """Индекс доллара DXY"""
    global cache
    now = time.time()
    if now - cache["dxy"]["ts"] < 3600:
        return cache["dxy"]["value"], cache["dxy"]["change"]
    
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=2d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        value = meta["regularMarketPrice"]
        prev_close = meta["previousClose"]
        change = ((value - prev_close) / prev_close * 100)
        
        cache["dxy"] = {"value": value, "change": change, "ts": now}
        return value, change
    except:
        return 100, 0

def get_vix():
    """Индекс волатильности VIX"""
    global cache
    now = time.time()
    if now - cache["vix"]["ts"] < 3600:
        return cache["vix"]["value"]
    
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = r.json()
        value = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        cache["vix"] = {"value": value, "ts": now}
        return value
    except:
        return 20

def get_usdt_dominance():
    """USDT Dominance %"""
    global cache
    now = time.time()
    if now - cache["usdt_dominance"]["ts"] < 600:
        return cache["usdt_dominance"]["value"], cache["usdt_dominance"]["change"]
    
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10
        )
        data = r.json()
        usdt_dom = data["data"]["market_cap_percentage"]["usdt"]
        
        prev = cache["usdt_dominance"]["value"]
        change = usdt_dom - prev if prev > 0 else 0
        
        cache["usdt_dominance"] = {"value": usdt_dom, "change": change, "ts": now}
        return usdt_dom, change
    except:
        return 5.0, 0

def is_important_economic_day():
    """Проверяет, является ли сегодня важным экономическим днём (FOMC, CPI, NFP)"""
    today = datetime.now(timezone.utc)
    
    # Примерные даты (можно дополнить)
    important_days = [
        # FOMC (примерно раз в 6 недель)
        (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10),
        # CPI (обычно 10-14 числа)
        (today.month, 10), (today.month, 11), (today.month, 12), (today.month, 13), (today.month, 14),
        # NFP (первая пятница месяца)
    ]
    
    # Первая пятница месяца для NFP
    if today.weekday() == 4 and today.day <= 7:
        return True
    
    for m, d in important_days:
        if today.month == m and today.day == d:
            return True
    
    return False

def get_google_trends_eth():
    """Интерес к ETH в Google Trends (эмуляция без pytrends)"""
    global cache
    now = time.time()
    if now - cache["google_trends"]["ts"] < 3600:
        return cache["google_trends"]["value"]
    
    # Упрощённая версия — используем корреляцию с объёмами
    try:
        df = get_klines(SYMBOL_BN, "1h", 24)
        if df is not None:
            avg_volume = df["volume"].mean()
            recent_volume = df["volume"].tail(4).mean()
            ratio = (recent_volume / avg_volume * 50) if avg_volume > 0 else 50
            value = min(100, max(0, ratio))
            cache["google_trends"] = {"value": value, "ts": now}
            return value
    except:
        pass
    return 50

# ══════════════════════════════════════════════
# ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ
# ══════════════════════════════════════════════
def check_closed_positions():
    global stats, active_positions
    try:
        current_positions = okx_get_positions()
        
        for order_id, pos_info in list(active_positions.items()):
            direction = pos_info["direction"]
            entry = pos_info["entry"]
            
            still_open = False
            for p in current_positions:
                if float(p.get("pos", 0)) != 0:
                    still_open = True
                    current_qty = float(p.get("pos", 0))
                    if current_qty < pos_info.get("total_qty", 0) * 0.9:
                        if pos_info.get("stage") == "open":
                            pos_info["stage"] = "tp1_hit"
                            save_active_positions()
                            
                            if pos_info.get("sl_order_id"):
                                okx_amend_sl(
                                    pos_info["sl_order_id"], entry,
                                    pos_info["pos_side"], pos_info["cls_side"]
                                )
                                log.info(f"🔒 Стоп перенесён в БУ: {entry:.2f}")
                                send_telegram(
                                    f"🎯 <b>ТЕЙК-1 ДОСТИГНУТ (+40%)</b>\n"
                                    f"📈 Закрыто 50% позиции {direction}\n"
                                    f"💰 Стоп перенесён на вход ({entry:.2f})\n"
                                    f"🎯 Ожидаем Тейк-2 (+100%)"
                                )
                    break
            
            if not still_open:
                history = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP", "instId": SYMBOL, "state": "filled",
                    "begin": str(int((time.time() - 86400) * 1000)),
                    "end": str(int(time.time() * 1000)), "limit": "50"
                })
                
                if history.get("code") != "0" or not history.get("data"):
                    continue
                
                total_pnl = 0
                closed_parts = 0
                close_reason = "📊 РЫНОК"
                
                for h in history["data"]:
                    if h.get("side") == pos_info["cls_side"] and h.get("posSide") == pos_info["pos_side"]:
                        avg_px = float(h.get("avgPx", 0))
                        qty = float(h.get("sz", 0))
                        if avg_px > 0 and qty > 0:
                            if direction == "LONG":
                                pnl_pct = (avg_px - entry) / entry * 100
                                if avg_px >= pos_info.get("tp2", 0) * 0.99:
                                    close_reason = "🎯 ТЕЙК-2 (+100%)"
                                elif avg_px >= pos_info.get("tp1", 0) * 0.99:
                                    close_reason = "🎯 ТЕЙК-1 (+40%)"
                                elif avg_px <= pos_info.get("sl", 0) * 1.01:
                                    close_reason = "🛑 СТОП-ЛОСС"
                            else:
                                pnl_pct = (entry - avg_px) / entry * 100
                                if avg_px <= pos_info.get("tp2", 0) * 1.01:
                                    close_reason = "🎯 ТЕЙК-2 (+100%)"
                                elif avg_px <= pos_info.get("tp1", 0) * 1.01:
                                    close_reason = "🎯 ТЕЙК-1 (+40%)"
                                elif avg_px >= pos_info.get("sl", 0) * 0.99:
                                    close_reason = "🛑 СТОП-ЛОСС"
                            
                            pnl_usdt = pnl_pct / 100 * ORDER_USDT * (qty / pos_info.get("total_qty", 1))
                            total_pnl += pnl_usdt
                            closed_parts += 1
                
                if closed_parts > 0:
                    if total_pnl > 0:
                        stats["wins"] += 1
                    else:
                        stats["losses"] += 1
                    
                    stats["total"] += 1
                    stats["total_profit"] += total_pnl
                    save_stats()
                    
                    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
                    emoji = "✅" if total_pnl > 0 else "❌"
                    
                    send_telegram(
                        f"{emoji} <b>СДЕЛКА ПОЛНОСТЬЮ ЗАКРЫТА</b>\n\n"
                        f"📈 Направление: {direction}\n"
                        f"💰 Вход: {entry:.2f}\n"
                        f"📊 Причина: {close_reason}\n"
                        f"💎 Общий P&L: {total_pnl:+.2f} USDT\n\n"
                        f"📈 <b>СТАТИСТИКА:</b>\n"
                        f"🔹 Всего сделок: {stats['total']}\n"
                        f"✅ Прибыльных: {stats['wins']} ({winrate:.1f}%)\n"
                        f"❌ Убыточных: {stats['losses']}\n"
                        f"💰 Общий P&L: {stats['total_profit']:.2f} USDT"
                    )
                    
                    log.info(f"Сделка закрыта: {direction} P&L: {total_pnl:.2f} USDT")
                    del active_positions[order_id]
                    save_active_positions()
                    
    except Exception as e:
        log.error(f"Ошибка проверки закрытых позиций: {e}")

# ══════════════════════════════════════════════
# ИНДИКАТОРЫ
# ══════════════════════════════════════════════
def calc(df):
    # EMA
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

    # RSI
    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
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
    df["BB_w"] = (df["BB_up"] - df["BB_dn"]) / bm
    df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)

    # VWAP
    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # CVD
    df["CVD"] = (df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])).rolling(20).sum()
    df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)

    # Объём и направление
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.3
    df["price_dir"] = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["vol_dir"] = df["vol_spike"].astype(int) * df["price_dir"]

    # Свечные паттерны
    body = (df["close"] - df["open"]).abs()
    lw = df[["open", "close"]].min(axis=1) - df["low"]
    uw = df["high"] - df[["open", "close"]].max(axis=1)
    df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
    df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    return df

# ══════════════════════════════════════════════
# СИГНАЛ (С НОВЫМИ МЕТРИКАМИ)
# ══════════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low
    
    if df is None or len(df) < 3:
        return None, None, None, None, None, 0, "Недостаточно данных"
    
    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    
    candle_time = row["candle_time"]
    seconds_since_open = (datetime.now(timezone.utc) - candle_time.replace(tzinfo=timezone.utc)).total_seconds()
    if seconds_since_open < 60:
        return None, None, None, None, None, 0, f"Свеча слишком новая ({int(seconds_since_open)}с)"

    if FORCE_TEST:
        entry = price
        sl = round(entry * (1 - SL_PCT), 2)
        tp1 = round(entry * (1 + TP1_PCT), 2)
        tp2 = round(entry * (1 + TP2_PCT), 2)
        return "LONG", entry, sl, tp1, tp2, 10, f"🧪 ТЕСТ"

    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, None, 0, f"Рынок мёртвый (ATR {atr:.2f})"

    # Динамика стакана
    ob_history.append(ob)
    if len(ob_history) > 6:
        ob_history.pop(0)
    ob_rising = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
    ob_delta = ob - last_ob if last_ob != 0 else 0
    last_ob = ob

    # ══════════════════════════════════════════════
    # НОВЫЕ МЕТРИКИ
    # ══════════════════════════════════════════════
    fear_greed = get_fear_greed()
    long_short = get_long_short_ratio()
    taker_ratio = get_taker_ratio()
    oi, oi_change = get_open_interest()
    sp500, sp500_change = get_sp500()
    dxy, dxy_change = get_dxy()
    vix = get_vix()
    usdt_dom, usdt_dom_change = get_usdt_dominance()
    trends = get_google_trends_eth()
    important_day = is_important_economic_day()

    L = S = 0.0

    # 1. EMA тренд
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 2
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 2
    elif row["EMA9"] > row["EMA21"]:
        L += 1
    elif row["EMA9"] < row["EMA21"]:
        S += 1

    # 2. RSI
    if rsi < 35:
        L += 2
    elif rsi < 45:
        L += 1
    elif rsi > 65:
        S += 2
    elif rsi > 55:
        S += 1

    # 3. MACD
    if row["MACD_bull"]:
        L += 2
    elif row["MACD"] > row["MACD_sig"]:
        L += 1
    if row["MACD_bear"]:
        S += 2
    elif row["MACD"] < row["MACD_sig"]:
        S += 1

    # 4. CVD
    if row["CVD_up"]:
        L += 1
    else:
        S += 1

    # 5. Bollinger
    bp = row["BB_pct"]
    if bp < 0.1:
        L += 1
    elif bp > 0.9:
        S += 1

    # 6. VWAP
    if price < row["VWAP"]:
        L += 1
    else:
        S += 1

    # 7. Стакан
    if ob > 5 or ob_rising:
        L += 1
    if ob < -5 or ob_falling:
        S += 1
    
    if ob_delta > 3:
        L += 0.5
    elif ob_delta < -3:
        S += 0.5

    # 8. BTC
    if btc_mom > 0.2:
        L += 1
        S = max(0, S - 1)
    elif btc_mom < -0.2:
        S += 1
        L = max(0, L - 1)
    
    eth_dir = 1 if row["close"] > df.iloc[-2]["close"] else -1
    if eth_dir == btc_dir and btc_dir != 0:
        if eth_dir == 1:
            L += 0.5
        else:
            S += 0.5
    else:
        if L > S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)

    # 9. Объём
    if row["vol_spike"]:
        if row["vol_dir"] > 0:
            L += 1
        elif row["vol_dir"] < 0:
            S += 1

    # 10. Свечной паттерн
    if row["hammer"]:
        L += 1
    if row["shooter"]:
        S += 1

    # Фандинг
    if funding < -0.001:
        L += 1
    elif funding > 0.003:
        S += 1

    # Вчерашние уровни
    if yesterday_high > 0 and yesterday_low > 0:
        if price < yesterday_low:
            L = max(0, L - 1)
        elif price > yesterday_high:
            S = max(0, S - 1)
        elif price > yesterday_low and price < yesterday_low * 1.01:
            L += 1
        elif price < yesterday_high and price > yesterday_high * 0.99:
            S += 1

    # Сессия
    hour = datetime.now(timezone.utc).hour
    if 1 <= hour < 6:
        if L >= S:
            L = max(0, L - 1)
        else:
            S = max(0, S - 1)

    # Бонус за конфлюэнцию
    ema_long = row["EMA9"] > row["EMA21"] > row["EMA50"]
    ema_short = row["EMA9"] < row["EMA21"] < row["EMA50"]
    rsi_long = rsi < 45
    rsi_short = rsi > 55
    macd_long = row["MACD"] > row["MACD_sig"]
    macd_short = row["MACD"] < row["MACD_sig"]
    
    if ema_long and rsi_long and macd_long:
        L += 1.5
    if ema_short and rsi_short and macd_short:
        S += 1.5

    # ══════════════════════════════════════════════
    # ВЕСА НОВЫХ МЕТРИК
    # ══════════════════════════════════════════════
    
    # Fear & Greed (0-100)
    if fear_greed < 25:  # Extreme Fear
        L += 1.5
    elif fear_greed < 45:
        L += 0.5
    elif fear_greed > 75:  # Extreme Greed
        S += 1.5
    elif fear_greed > 60:
        S += 0.5
    
    # Long/Short Ratio
    if long_short > 2.5:  # Перегрев лонгов
        S += 1
    elif long_short > 1.8:
        S += 0.5
    elif long_short < 0.8:  # Перегрев шортов
        L += 1
    elif long_short < 1.2:
        L += 0.5
    
    # Taker Buy/Sell Ratio
    if taker_ratio > 1.3:
        L += 1
    elif taker_ratio > 1.1:
        L += 0.5
    elif taker_ratio < 0.7:
        S += 1
    elif taker_ratio < 0.9:
        S += 0.5
    
    # Open Interest
    if oi_change > 3:  # Резкий рост OI
        if price > df.iloc[-2]["close"]:  # Цена растёт
            L += 1
        else:  # Цена падает
            S += 1
    elif oi_change < -3:  # Резкое падение OI
        if L > S:
            L = max(0, L - 0.5)
        else:
            S = max(0, S - 0.5)
    
    # S&P 500
    if sp500_change > 1:
        L += 1
    elif sp500_change > 0.3:
        L += 0.5
    elif sp500_change < -1:
        S += 1
    elif sp500_change < -0.3:
        S += 0.5
    
    # DXY (обратная корреляция)
    if dxy_change > 0.3:  # DXY растёт
        S += 0.5
        L = max(0, L - 0.3)
    elif dxy_change < -0.3:  # DXY падает
        L += 0.5
        S = max(0, S - 0.3)
    
    # VIX (страх на рынках)
    if vix > 30:  # Высокая волатильность
        L = max(0, L - 0.5)
        S = max(0, S - 0.5)
    elif vix > 25:
        if L > S:
            L = max(0, L - 0.3)
        else:
            S = max(0, S - 0.3)
    
    # USDT Dominance
    if usdt_dom_change > 0.2:  # Растёт доля USDT → уход в кэш
        S += 0.5
        L = max(0, L - 0.3)
    elif usdt_dom_change < -0.2:  # Падает доля USDT → заходят в крипту
        L += 0.5
        S = max(0, S - 0.3)
    
    # Google Trends
    if trends > 70:  # Высокий интерес
        if L > S:
            L += 0.5
        else:
            S += 0.5
    elif trends < 30:  # Низкий интерес
        if L > S:
            L = max(0, L - 0.3)
        else:
            S = max(0, S - 0.3)
    
    # Важный экономический день
    if important_day:
        # Снижаем уверенность, но не блокируем полностью
        L = max(0, L - 1)
        S = max(0, S - 1)

    # Выбор направления
    long_signal = L >= MIN_SCORE and (L - S) >= MIN_SCORE_DIFF
    short_signal = S >= MIN_SCORE and (S - L) >= MIN_SCORE_DIFF

    if not long_signal and not short_signal:
        return None, None, None, None, None, max(L, S), f"L:{L:.1f} S:{S:.1f} diff:{abs(L-S):.1f}"

    entry = price
    if long_signal:
        direction = "LONG"
        score = L
        sl = round(entry * (1 - SL_PCT), 2)
        tp1 = round(entry * (1 + TP1_PCT), 2)
        tp2 = round(entry * (1 + TP2_PCT), 2)
    else:
        direction = "SHORT"
        score = S
        sl = round(entry * (1 + SL_PCT), 2)
        tp1 = round(entry * (1 - TP1_PCT), 2)
        tp2 = round(entry * (1 - TP2_PCT), 2)

    log.info(f"{direction} балл:{score:.1f} | {entry:.2f} → SL:{sl:.2f} TP1:{tp1:.2f} TP2:{tp2:.2f}")
    
    # Добавляем новые метрики в reason
    reason = (f"L:{L:.1f} S:{S:.1f} | F&G:{fear_greed} | L/S:{long_short:.2f} | "
              f"Taker:{taker_ratio:.2f} | OI:{oi_change:+.1f}% | SPX:{sp500_change:+.2f}%")
    
    return direction, entry, sl, tp1, tp2, score, reason

# ══════════════════════════════════════════════
# ШКАЛА БАЛЛОВ
# ══════════════════════════════════════════════
def score_bar(score):
    filled = round(score / MAX_SCORE * 10)
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 10:
        emoji = "🟢"
    elif score >= 7:
        emoji = "🟡"
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

# ══════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════
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
    atr_val = df.iloc[-1]["ATR"]

    direction, entry, sl, tp1, tp2, score, reason = get_signal(df, funding, ob, btc_mom, btc_dir)
    log.info(f"ETH:{price:.2f} | {direction or 'нет'} балл:{score:.1f}")

    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_get_balance()
        pos = okx_get_positions()
        hour = datetime.now(timezone.utc).hour
        if 1 <= hour < 6:
            session = "🌙 Ночь"
        elif hour < 13:
            session = "🇬🇧 Лондон"
        else:
            session = "🇺🇸 Нью-Йорк"
        winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
        
        # Получаем свежие данные для heartbeat
        fear_greed = get_fear_greed()
        long_short = get_long_short_ratio()
        sp500, sp500_change = get_sp500()
        important_day = is_important_economic_day()
        
        l_val = "?"
        s_val = "?"
        l_num = 0.0
        s_num = 0.0
        if "L:" in reason and "S:" in reason:
            try:
                l_part = reason.split("L:")[1].split(" ")[0]
                s_part = reason.split("S:")[1].split(" ")[0]
                l_val = l_part
                s_val = s_part
                l_num = float(l_part)
                s_num = float(s_part)
            except:
                pass
        
        l_color = "🟢" if l_num >= 6 else ("🟡" if l_num >= 4 else "🔴")
        s_color = "🟢" if s_num >= 6 else ("🟡" if s_num >= 4 else "🔴")
        max_score_val = max(l_num, s_num)
        max_color = "🟢" if max_score_val >= 6 else ("🟡" if max_score_val >= 4 else "🔴")
        
        if direction:
            signal_status = f"{direction} {score_color(score)} <b>{score:.1f}</b>"
        else:
            signal_status = f"нет {max_color} <b>{max_score_val:.1f}</b>"
        
        important_str = "⚠️ Важный день" if important_day else ""
        
        send_telegram(
            f"❤️ <b>Heartbeat</b> {important_str}\n\n"
            f"💰 ETH: <b>{price:.2f}</b>\n"
            f"😱 F&G: {fear_greed} | L/S: {long_short:.2f}\n"
            f"📊 SPX: {sp500_change:+.2f}% | DXY: {get_dxy()[1]:+.2f}%\n"
            f"🌍 Сессия: {session}\n"
            f"💳 Баланс: {bal:.2f} USDT | Поз: {len(pos)}\n"
            f"🎯 Сигнал: {signal_status} | L: {l_color} {l_val} S: {s_color} {s_val}\n"
            f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%) | P&L: {stats['total_profit']:.2f} USDT\n"
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
        f"🛑 Стоп: {sl:.2f} (-60%)",
        f"🎯 Тейк-1: {tp1:.2f} (+40%) → 50% + БУ",
        f"🎯 Тейк-2: {tp2:.2f} (+100%) → 50%",
        f"",
        f"📊 {reason}",
    ]

    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp1, tp2)
        if res["ok"]:
            losses_in_row = 0
            msg += [
                f"✅ <b>ИСПОЛНЕНО</b>",
                f"📦 Контрактов: {res['total_qty']}",
                f"📊 TP1: {res['qty1']} | TP2: {res['qty2']}",
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
    log.info("🚀 Старт")
    bal = okx_get_balance()
    winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
    send_telegram(
        f"🚀 <b>OKX Scalp Bot v3.0</b>\n\n"
        f"🎭 Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
        f"⚙️ Плечо: x{LEVERAGE}\n"
        f"💰 Сделка: {ORDER_USDT} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE} (diff ≥ {MIN_SCORE_DIFF})\n"
        f"📐 SL: -{int(SL_PCT*100)}% | TP1: +{int(TP1_PCT*100)}% | TP2: +{int(TP2_PCT*100)}%\n"
        f"📊 Новые метрики: F&G, L/S, Taker, OI, SPX, DXY, VIX, USDT.D, Trends\n"
        f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%)"
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
