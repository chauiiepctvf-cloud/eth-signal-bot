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
MIN_SCORE = 6      # повышенный порог
MAX_SCORE = 14       # максимум очков (увеличили из-за новых факторов)
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

def okx_close_partial(pos_side, cls_side, qty):
    """Частичное закрытие позиции по рынку"""
    return okx_post("/api/v5/trade/order", {
        "instId": SYMBOL,
        "tdMode": "cross",
        "side": cls_side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(qty)
    })

def okx_place_order(direction, entry, sl, tp1, tp2):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"
    
    # Общий размер позиции в контрактах
    total_qty = max(1, round(ORDER_USDT * LEVERAGE / entry / 0.01))
    qty1 = max(1, int(total_qty * TP1_RATIO))
    qty2 = total_qty - qty1
    
    log.info(f"▶ {direction} total:{total_qty} qty1:{qty1} qty2:{qty2} entry:{entry:.2f} sl:{sl:.2f} tp1:{tp1:.2f} tp2:{tp2:.2f}")

    # Шаг 1: открываем позицию
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
    
    # Сохраняем информацию о позиции
    active_positions[order_id] = {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "qty1": qty1,
        "qty2": qty2,
        "total_qty": total_qty,
        "pos_side": pos_side,
        "cls_side": cls_side,
        "open_time": time.time(),
        "stage": "open",  # open, tp1_hit, trailing
        "sl_order_id": None
    }
    save_active_positions()
    
    time.sleep(2)

    # Шаг 2: Ставим SL на ВСЮ позицию
    sl_r = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(total_qty),
        "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last",
    })
    
    if sl_r.get("code") == "0":
        sl_order_id = sl_r["data"][0].get("algoId", "")
        active_positions[order_id]["sl_order_id"] = sl_order_id
        save_active_positions()
    
    # Шаг 3: Ставим TP1 на ПЕРВУЮ часть
    tp1_r = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(qty1),
        "tpTriggerPx": str(round(tp1, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
    })
    
    # Шаг 4: Ставим TP2 на ВТОРУЮ часть
    tp2_r = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "conditional", "sz": str(qty2),
        "tpTriggerPx": str(round(tp2, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
    })
    
    algo_ok = sl_r.get("code") == "0" or tp1_r.get("code") == "0" or tp2_r.get("code") == "0"
    
    return {
        "ok": True, 
        "orderId": order_id, 
        "total_qty": total_qty,
        "qty1": qty1,
        "qty2": qty2,
        "algo_ok": algo_ok
    }

# ══════════════════════════════════════════════
# ДАННЫЕ
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
        
        # Время свечи для проверки «свежести»
        df["candle_time"] = pd.to_datetime(df["time"], unit="ms")
        
        return df
    except Exception as e:
        log.error(f"klines {sym} {interval}: {e}")
        return None

def get_yesterday_levels():
    """Получает вчерашние High/Low"""
    try:
        df = get_klines(SYMBOL_BN, "1d", 2)
        if df is not None and len(df) >= 2:
            yesterday = df.iloc[-2]
            return float(yesterday["high"]), float(yesterday["low"])
    except:
        pass
    return 0, 0

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
        # Направление BTC (растёт или падает)
        btc_dir = 1 if df.iloc[-1]["close"] > df.iloc[-2]["close"] else -1
        return round(chg, 3), btc_dir
    except:
        return 0.0, 0

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
                                new_sl = entry
                                okx_amend_sl(
                                    pos_info["sl_order_id"], 
                                    new_sl, 
                                    pos_info["pos_side"],
                                    pos_info["cls_side"]
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
                # Позиция полностью закрыта
                history = okx_get("/api/v5/trade/orders-history-archive", {
                    "instType": "SWAP",
                    "instId": SYMBOL,
                    "state": "filled",
                    "begin": str(int((time.time() - 86400) * 1000)),
                    "end": str(int(time.time() * 1000)),
                    "limit": "50"
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
                            else:
                                pnl_pct = (entry - avg_px) / entry * 100
                            
                            if direction == "LONG":
                                if avg_px >= pos_info.get("tp2", 0) * 0.99:
                                    close_reason = "🎯 ТЕЙК-2 (+100%)"
                                elif avg_px >= pos_info.get("tp1", 0) * 0.99:
                                    close_reason = "🎯 ТЕЙК-1 (+40%)"
                                elif avg_px <= pos_info.get("sl", 0) * 1.01:
                                    close_reason = "🛑 СТОП-ЛОСС"
                            else:
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
                    
                    msg = (
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
                    send_telegram(msg)
                    
                    log.info(f"Сделка полностью закрыта: {direction} P&L: {total_pnl:.2f} USDT")
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

    # Объём и направление объёма
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
# СИГНАЛ
# ══════════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom, btc_dir):
    global ob_history, last_ob, yesterday_high, yesterday_low
    
    if df is None or len(df) < 3:
        return None, None, None, None, None, 0, "Недостаточно данных"
    
    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    
    # Проверка «свежести» свечи
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
    reason = f"Балл {score:.1f} | L:{L:.1f} S:{S:.1f} | RSI {rsi:.0f} | OB {ob:+.1f}%"
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
    
    # Обновляем вчерашние уровни раз в час
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
        send_telegram(
            f"❤️ <b>Heartbeat</b>\n\n"
            f"💰 ETH: <b>{price:.2f}</b>\n"
            f"₿ BTC: {btc_mom:+.2f}% OB: {ob:+.1f}%\n"
            f"🌍 Сессия: {session}\n"
            f"💳 Баланс: {bal:.2f} USDT\n"
            f"📊 Позиций: {len(pos)}\n"
            f"📊 Статистика: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%) | P&L: {stats['total_profit']:.2f} USDT"
        )

    direction, entry, sl, tp1, tp2, score, reason = get_signal(df, funding, ob, btc_mom, btc_dir)
    log.info(f"ETH:{price:.2f} | {direction or 'нет'} балл:{score:.1f}")

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
        f"🚀 <b>OKX Scalp Bot v2.0</b>\n\n"
        f"🎭 Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
        f"⚙️ Плечо: x{LEVERAGE}\n"
        f"💰 Сделка: {ORDER_USDT} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE} (diff ≥ {MIN_SCORE_DIFF})\n"
        f"📐 SL: -{int(SL_PCT*100)}% | TP1: +{int(TP1_PCT*100)}% | TP2: +{int(TP2_PCT*100)}%\n"
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
