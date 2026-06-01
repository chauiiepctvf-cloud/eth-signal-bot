“””
OKX Scalp Bot v6.4 (ETH-USDT-SWAP)
Многофакторный скальпинг с ML-обучением
Адаптивный режим тренд/канал | Weight Tuner | Backtest Analyzer

Изменения v6.4 (по сравнению с v6.3):

- ФИКС: тройное касание — поддержка даёт бонус ТОЛЬКО лонгу, сопротивление ТОЛЬКО шорту
- ФИКС: TP1 теперь считается от настоящего ATR (передаётся из сигнала), а не от мусора volume-close
- ФИКС: режим Тренд/Канал и секции RSI/BB привязаны к ОДНОМУ current_mode (убран рассинхрон 23/27 vs 25)
- ФИКС: гистерезис ADX расширен до 20/30 (меньше дёрганья на границе)
- ФИКС: Weight Tuner шаг +0.05 + затухание к 1.0 (убран однонаправленный дрейф)
- ФИКС: race condition в бэктесте — работает на копии кэша, не замораживает глобальный
- ФИКС: losses_in_row растёт только на реальном убытке, не на сбое размещения ордера
- ФИКС: 1h-подтверждение вынесено в лёгкую функцию (без повторных вызовов внешних API)
- ФИКС: MIN_SCORE_LONG/SHORT и force_test_done переживают рестарт
- ФИКС: weight_tuner.last_changes реально присваивается (heartbeat больше не врёт “ожидание”)
- ФИКС: единый флаг LIVE для переключения демо/прод
- ФИКС: версии в сообщениях приведены к v6.4
  “””

import os
import time
import logging
import threading
import hashlib
import hmac
import base64
import json
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
from flask import Flask

# ========================================================

# КОНФИГ

# ========================================================

TELEGRAM_TOKEN  = os.environ.get(“TELEGRAM_TOKEN”)
CHAT_ID         = os.environ.get(“CHAT_ID”)
OKX_API_KEY     = os.environ.get(“OKX_API_KEY”)
OKX_SECRET      = os.environ.get(“OKX_SECRET”)
OKX_PASSPHRASE  = os.environ.get(“OKX_PASSPHRASE”)
TWELVE_API_KEY  = os.environ.get(“TWELVE_API_KEY”, “”)
JSONBIN_KEY     = os.environ.get(“JSONBIN_API_KEY”)
JSONBIN_BIN_ID  = os.environ.get(“JSONBIN_BIN_ID”)

# ЕДИНЫЙ ФЛАГ боевой/демо. LIVE=False -> демо (x-simulated-trading:1).

# Для боя поставь LIVE = True (или env OKX_LIVE=1).

LIVE = os.environ.get(“OKX_LIVE”, “0”) == “1”

OKX_BASE        = “https://www.okx.com”
OKX_DEMO_HEADER = {} if LIVE else {“x-simulated-trading”: “1”}

SYMBOL          = “ETH-USDT-SWAP”
SYMBOL_BN       = “ETHUSDT”
BTC_SYMBOL      = “BTCUSDT”

# =========================================================

# ВЫБОР ТАЙМФРЕЙМА – меняй ОДНУ строку чтобы переключить режим

# =========================================================

TIMEFRAME = “15m”   # варианты: “5m”, “15m”, “1h”

TF_PRESETS = {
“5m”: {
“SL_ATR_MULT”:    2.0,
“TP_ATR_MULT”:    2.0,
“SL_PCT_MIN”:     0.003,
“SL_PCT_MAX”:     0.015,
“TP_PCT_MAX”:     0.020,
“BASE_MIN_SCORE”: 8.0,
“MIN_SCORE_DIFF”: 3.5,
“ADX_MIN”:        25,
“SCAN_INTERVAL”:  3 * 60,
“HEARTBEAT_INT”:  60 * 60,
“MAX_HOLD_HRS”:   2,
“TRADING_HOURS”:  (8, 22),
},
“15m”: {
“SL_ATR_MULT”:    1.5,
“TP_ATR_MULT”:    2.5,
“SL_PCT_MIN”:     0.005,
“SL_PCT_MAX”:     0.025,
“TP_PCT_MAX”:     0.040,
“BASE_MIN_SCORE”: 6.0,
“MIN_SCORE_DIFF”: 2.5,
“ADX_MIN”:        15,
“SCAN_INTERVAL”:  5 * 60,
“HEARTBEAT_INT”:  60 * 60,
“MAX_HOLD_HRS”:   6,
“TRADING_HOURS”:  None,
},
“1h”: {
“SL_ATR_MULT”:    1.5,
“TP_ATR_MULT”:    3.0,
“SL_PCT_MIN”:     0.010,
“SL_PCT_MAX”:     0.040,
“TP_PCT_MAX”:     0.080,
“BASE_MIN_SCORE”: 7.0,
“MIN_SCORE_DIFF”: 3.0,
“ADX_MIN”:        20,
“SCAN_INTERVAL”:  10 * 60,
“HEARTBEAT_INT”:  3 * 60 * 60,
“MAX_HOLD_HRS”:   24,
“TRADING_HOURS”:  None,
},
}

_p = TF_PRESETS[TIMEFRAME]

LEVERAGE        = 20
ORDER_USDT      = 20
SL_ATR_MULT     = _p[“SL_ATR_MULT”]
TP_ATR_MULT     = _p[“TP_ATR_MULT”]
SL_PCT_MIN      = _p[“SL_PCT_MIN”]
SL_PCT_MAX      = _p[“SL_PCT_MAX”]
TP_PCT_MAX      = _p[“TP_PCT_MAX”]
ADX_MIN         = _p[“ADX_MIN”]
MAX_HOLD_HOURS  = _p[“MAX_HOLD_HRS”]
TRADING_HOURS   = _p[“TRADING_HOURS”]

SCAN_INTERVAL      = _p[“SCAN_INTERVAL”]
HEARTBEAT_INTERVAL = _p[“HEARTBEAT_INT”]
WATCHDOG_THRESHOLD = max(30 * 60, SCAN_INTERVAL * 4)
DAILY_REPORT_HOUR  = 23

BASE_MIN_SCORE  = _p[“BASE_MIN_SCORE”]
MIN_SCORE       = _p[“BASE_MIN_SCORE”]
MIN_SCORE_LONG  = _p[“BASE_MIN_SCORE”]
MIN_SCORE_SHORT = _p[“BASE_MIN_SCORE”] - 0.5
MIN_SCORE_DIFF  = _p[“MIN_SCORE_DIFF”]
MAX_SCORE       = 13.0

# Гистерезис режима Тренд/Канал (РАСШИРЕН до 20/30 в v6.4)

# Канал активируется при ADX < REGIME_LOW, Тренд — при ADX > REGIME_HIGH.

# В зоне [REGIME_LOW, REGIME_HIGH] режим НЕ меняется (мёртвая зона).

REGIME_LOW  = 20
REGIME_HIGH = 30

# Перегрев цены относительно EMA21 (опасно входить на пике)

EMA21_MAX_DIST_ATR = 2.0

# Фильтры

ATR_MIN_PCT     = 0.0003
SPREAD_MAX_PCT  = 0.0005
NIGHT_HOURS     = (22, 6)
RED_NEWS_DROP   = -0.01
RED_NEWS_VOL    = 1.5
RED_NEWS_BLOCK  = 1800
MAX_LOSSES      = 3
PAUSE_LOSSES    = 1800
ETH_BTC_CORR_MIN= 0.3

COOLDOWN_AFTER_TP   = 10 * 60
COOLDOWN_AFTER_SL   = 20 * 60
ANTI_CHASE_PCT      = 0.005
ANTI_CHASE_WINDOW   = 30 * 60
RSI_OVERHEAT_LONG   = 75
RSI_OVERHEAT_SHORT  = 25
BB_OVERHEAT_LONG    = 0.95
BB_OVERHEAT_SHORT   = 0.5

# ML

ML_RETRAIN_EVERY= 10
ML_MIN_SAMPLES  = 30
ML_DECAY_DAYS   = 7

FORCE_TEST      = False

NEWS_EVENTS = [
(2026, 6, 12, 12, 30),
(2026, 6, 18, 18, 0),
(2026, 7, 3, 12, 30),
]

BACKTEST_DAYS_INITIAL = 30
BACKTEST_DAYS_WEEKLY = 7
BACKTEST_AUTO_DAY = 6
BACKTEST_AUTO_HOUR = 22
BACKTEST_VIRTUAL_WEIGHT = 0.3
backtest_done_initial = False

# ========================================================

# ЛОГИРОВАНИЕ

# ========================================================

logging.basicConfig(level=logging.INFO, format=’%(asctime)s [%(levelname)s] %(message)s’)
log = logging.getLogger(**name**)

app = Flask(**name**)

# ========================================================

# JSONBIN STORAGE

# ========================================================

JSONBIN_URL = f”https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}” if JSONBIN_BIN_ID else None
_save_lock = threading.Lock()

def jsonbin_load():
if not JSONBIN_KEY or not JSONBIN_URL:
log.warning(“JSONBin не настроен – данные потеряются при деплое”)
return {}
try:
r = requests.get(
JSONBIN_URL + “/latest”,
headers={“X-Master-Key”: JSONBIN_KEY},
timeout=15,
)
if r.status_code == 200:
return r.json().get(“record”, {}) or {}
log.error(f”JSONBin GET {r.status_code}: {r.text[:200]}”)
except Exception as e:
log.error(f”JSONBin GET error: {e}”)
return {}

def jsonbin_save(data: dict, max_retries: int = 3) -> bool:
if not JSONBIN_KEY or not JSONBIN_URL:
return False

```
for attempt in range(max_retries):
    try:
        r = requests.put(
            JSONBIN_URL,
            headers={
                "X-Master-Key": JSONBIN_KEY,
                "Content-Type": "application/json",
            },
            json=data,
            timeout=30,
        )
        if r.status_code == 200:
            if attempt > 0:
                log.info(f"JSONBin PUT OK с попытки {attempt + 1}")
            return True
        log.error(f"JSONBin PUT {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"JSONBin PUT error (попытка {attempt + 1}/{max_retries}): {e}")

    if attempt < max_retries - 1:
        time.sleep(2 ** (attempt + 1))

_pending_save_queue.append(data)
log.warning(f"JSONBin сохранение в очередь, всего отложено: {len(_pending_save_queue)}")
return False
```

_pending_save_queue = []

def jsonbin_flush_pending():
if not _pending_save_queue:
return
latest = _pending_save_queue[-1]
_pending_save_queue.clear()
log.info(f”JSONBin: пробую сбросить очередь”)
if not jsonbin_save(latest, max_retries=2):
log.warning(“JSONBin: сброс очереди не удался”)

# ========================================================

# СОСТОЯНИЕ

# ========================================================

stats = {
“total”: 0, “wins”: 0, “losses”: 0,
“total_profit”: 0.0,
“total_profit_sum”: 0.0, “total_loss_sum”: 0.0,
“max_drawdown”: 0.0, “peak_equity”: 0.0, “current_equity”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0,
“ml_trains_count”: 0, “ml_last_train_ts”: 0.0,
“ml_last_accuracy”: 0.0, “ml_samples_at_last_train”: 0,
}

active_positions = {}
signals_history  = []
ob_history       = []
last_ob          = 0.0
last_scan_time   = 0.0
last_heartbeat_time = 0.0
last_daily_report_day = -1
red_news_until   = 0.0
pause_until      = 0.0
losses_in_row    = 0
yesterday_high   = 0.0
yesterday_low    = 0.0

last_close_ts        = 0.0
last_close_result    = None
last_close_direction = None
last_close_price     = 0.0
force_test_done  = False
scalp_model      = None
ml_train_counter = 0
last_vwap_reset_day = -1
analyzer_status = “норма”
current_mode = “Тренд”

# Tuner-состояние (объявлено ДО первого использования)

last_analyzer_ts = 0.0
last_tuner_ts = 0.0
tuner_last_changes = “ожидание”
tuner_rsi_long_mult = 1.0
tuner_rsi_short_mult = 1.0
tuner_bb_long_mult = 1.0
tuner_bb_short_mult = 1.0

cache = {
“fear_greed”:     {“value”: 50, “ts”: 0},
“long_short”:     {“value”: 1.0, “ts”: 0},
“taker_ratio”:    {“value”: 1.0, “ts”: 0},
“open_interest”:  {“value”: 0, “change”: 0, “ts”: 0},
“dxy”:            {“value”: 100, “change”: 0, “ts”: 0},
“vix”:            {“value”: 20, “ts”: 0},
“usdt_dom”:       {“value”: 5.0, “change”: 0, “ts”: 0},
“btc_dom”:        {“value”: 50, “change”: 0, “ts”: 0},
“cb_premium”:     {“value”: 0.0, “ts”: 0},
“eth_btc”:        {“value”: 0.0, “change”: 0.0, “ts”: 0},
“funding_avg”:    {“value”: 0.0, “ts”: 0},
“trending”:       {“eth_in_top”: False, “ts”: 0},
“gas”:            {“value”: 20, “ts”: 0},
“4h_trend”:       {“diff”: 0.0, “bull”: False, “ts”: 0},
“1h_trend”:       {“price_vs_ema”: 0.0, “bull”: True, “ts”: 0},
“whales”:         {“detected”: False, “ts”: 0},
“eth_btc_corr”:   {“value”: 1.0, “ts”: 0},
}

# ========================================================

# ХРАНИЛИЩЕ: ЗАГРУЗКА И СОХРАНЕНИЕ

# ========================================================

def storage_load_all():
global stats, active_positions, signals_history, MIN_SCORE
global MIN_SCORE_LONG, MIN_SCORE_SHORT
global last_close_ts, last_close_result, last_close_direction, last_close_price
global ml_train_counter, last_analyzer_ts, last_tuner_ts
global tuner_rsi_long_mult, tuner_rsi_short_mult, tuner_bb_long_mult, tuner_bb_short_mult
global force_test_done

```
data = jsonbin_load()

if not data:
    log.info("JSONBin пустой -- пытаюсь мигрировать локальные файлы")
    migrated = {}
    for fname, key in [
        ("signals_history.json", "signals_history"),
        ("stats.json",           "stats"),
        ("active_positions.json","active_positions"),
    ]:
        try:
            if os.path.exists(fname):
                with open(fname) as f:
                    v = json.load(f)
                if v:
                    migrated[key] = v
                    log.info(f"  Найдено {fname}")
        except Exception as e:
            log.error(f"  {fname}: {e}")
    if migrated:
        log.info(f"Пушу в JSONBin {list(migrated.keys())}")
        jsonbin_save(migrated)
        data = migrated

sh = data.get("signals_history", [])
if isinstance(sh, list):
    signals_history.clear()
    signals_history.extend(sh)

s = data.get("stats")
if isinstance(s, dict):
    for k in stats:
        if k in s:
            stats[k] = s[k]

ap = data.get("active_positions", {})
if isinstance(ap, dict):
    active_positions.clear()
    active_positions.update(ap)

ms = data.get("min_score")
if isinstance(ms, (int, float)):
    MIN_SCORE = float(ms)
msl = data.get("min_score_long")
if isinstance(msl, (int, float)):
    MIN_SCORE_LONG = float(msl)
mss = data.get("min_score_short")
if isinstance(mss, (int, float)):
    MIN_SCORE_SHORT = float(mss)

last_close_ts        = data.get("last_close_ts", 0.0) or 0.0
last_close_result    = data.get("last_close_result")
last_close_direction = data.get("last_close_direction")
last_close_price     = data.get("last_close_price", 0.0) or 0.0

ml_train_counter = int(data.get("ml_train_counter", 0) or 0)
last_analyzer_ts = float(data.get("last_analyzer_ts", 0) or 0)
last_tuner_ts = float(data.get("last_tuner_ts", 0) or 0)
tuner_rsi_long_mult = float(data.get("tuner_rsi_long_mult", 1.0) or 1.0)
tuner_rsi_short_mult = float(data.get("tuner_rsi_short_mult", 1.0) or 1.0)
tuner_bb_long_mult = float(data.get("tuner_bb_long_mult", 1.0) or 1.0)
tuner_bb_short_mult = float(data.get("tuner_bb_short_mult", 1.0) or 1.0)
force_test_done = bool(data.get("force_test_done", False))

log.info(
    f"Загружено: signals={len(signals_history)} "
    f"trades={stats['total']} active={len(active_positions)} "
    f"min_score={MIN_SCORE} ml_counter={ml_train_counter}"
)
```

def storage_save_all():
with _save_lock:
data = {
“signals_history”:  signals_history[-500:],
“stats”:            stats,
“active_positions”: active_positions,
“min_score”:        MIN_SCORE,
“min_score_long”:   MIN_SCORE_LONG,
“min_score_short”:  MIN_SCORE_SHORT,
“last_close_ts”:        last_close_ts,
“last_close_result”:    last_close_result,
“last_close_direction”: last_close_direction,
“last_close_price”:     last_close_price,
“ml_train_counter”: ml_train_counter,
“last_analyzer_ts”: last_analyzer_ts,
“last_tuner_ts”: last_tuner_ts,
“tuner_rsi_long_mult”: tuner_rsi_long_mult,
“tuner_rsi_short_mult”: tuner_rsi_short_mult,
“tuner_bb_long_mult”: tuner_bb_long_mult,
“tuner_bb_short_mult”: tuner_bb_short_mult,
“force_test_done”:  force_test_done,
“last_updated”:     datetime.now(timezone.utc).isoformat(),
}
ok = jsonbin_save(data)
try:
with open(“signals_history.json”, “w”) as f:
json.dump(signals_history[-500:], f)
with open(“stats.json”, “w”) as f:
json.dump(stats, f)
with open(“active_positions.json”, “w”) as f:
json.dump(active_positions, f)
except Exception as e:
log.error(f”Local backup: {e}”)
return ok

def storage_save_async():
threading.Thread(target=storage_save_all, daemon=True).start()

# ========================================================

# TELEGRAM

# ========================================================

def _tg_send(text: str):
if not TELEGRAM_TOKEN or not CHAT_ID:
return
try:
requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={“chat_id”: CHAT_ID, “text”: text, “parse_mode”: “HTML”},
timeout=15,
)
except Exception as e:
log.error(f”TG: {e}”)

def send_telegram(text: str):
threading.Thread(target=_tg_send, args=(text,), daemon=True).start()

# ========================================================

# OKX API

# ========================================================

def _ts():
return datetime.now(timezone.utc).strftime(”%Y-%m-%dT%H:%M:%S.000Z”)

def _sign(ts, method, path, body=””):
if not OKX_SECRET:
return “”
msg = ts + method.upper() + path + body
mac = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256)
return base64.b64encode(mac.digest()).decode()

def okx_get(path, params=None):
if not OKX_API_KEY:
return {}
ts = _ts()
qs = f”?{requests.compat.urlencode(params)}” if params else “”
sign = _sign(ts, “GET”, path + qs)
h = {
“OK-ACCESS-KEY”:        OKX_API_KEY,
“OK-ACCESS-SIGN”:       sign,
“OK-ACCESS-TIMESTAMP”:  ts,
“OK-ACCESS-PASSPHRASE”: OKX_PASSPHRASE,
“Content-Type”:         “application/json”,
}
h.update(OKX_DEMO_HEADER)
try:
return requests.get(f”{OKX_BASE}{path}{qs}”, headers=h, timeout=15).json()
except Exception as e:
log.error(f”OKX GET {path}: {e}”)
return {}

def okx_post(path, data=None):
if not OKX_API_KEY:
return {}
ts = _ts()
body = json.dumps(data) if data else “”
sign = _sign(ts, “POST”, path, body)
h = {
“OK-ACCESS-KEY”:        OKX_API_KEY,
“OK-ACCESS-SIGN”:       sign,
“OK-ACCESS-TIMESTAMP”:  ts,
“OK-ACCESS-PASSPHRASE”: OKX_PASSPHRASE,
“Content-Type”:         “application/json”,
}
h.update(OKX_DEMO_HEADER)
try:
return requests.post(f”{OKX_BASE}{path}”, data=body, headers=h, timeout=15).json()
except Exception as e:
log.error(f”OKX POST {path}: {e}”)
return {}

def okx_set_leverage():
return okx_post(”/api/v5/account/set-leverage”, {
“instId”: SYMBOL, “lever”: str(LEVERAGE), “mgnMode”: “cross”,
})

def okx_get_balance():
try:
r = okx_get(”/api/v5/account/balance”)
for d in r.get(“data”, []):
for dt in d.get(“details”, []):
if dt.get(“ccy”) == “USDT”:
return float(dt.get(“eq”, 0))
except Exception:
pass
return 0.0

def okx_get_positions():
r = okx_get(”/api/v5/account/positions”, {“instId”: SYMBOL})
return [p for p in r.get(“data”, []) if float(p.get(“pos”, 0)) != 0]

def okx_cancel_algo(algo_id):
if not algo_id:
return
try:
okx_post(”/api/v5/trade/cancel-algo-order”,
[{“instId”: SYMBOL, “algoId”: algo_id}])
except Exception as e:
log.error(f”cancel_algo {algo_id}: {e}”)

def okx_get_fills_history(begin_ms):
return okx_get(”/api/v5/trade/fills-history”, {
“instType”: “SWAP”,
“instId”:   SYMBOL,
“begin”:    str(int(begin_ms)),
“limit”:    “100”,
})

def okx_place_order(direction, entry, sl, tp, atr_val=0.0):
“”“Открывает позицию + ставит SL и TP. atr_val передаётся из сигнала
(ФИКС v6.4: раньше TP1 считался от мусора volume-close через 2 лишних HTTP).”””
okx_set_leverage()

```
side     = "buy"  if direction == "LONG"  else "sell"
pos_side = "long" if direction == "LONG"  else "short"
cls_side = "sell" if direction == "LONG"  else "buy"

hour = datetime.now(timezone.utc).hour
in_night = NIGHT_HOURS[0] <= hour or hour < NIGHT_HOURS[1]
multiplier = 0.5 if in_night else 1.0
effective = ORDER_USDT * multiplier
total_qty = max(1, round(effective * LEVERAGE / entry / 0.01))

log.info(f"{direction} qty={total_qty} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f} atr={atr_val:.2f}")

r = okx_post("/api/v5/trade/order", {
    "instId":  SYMBOL,
    "tdMode":  "cross",
    "side":    side,
    "posSide": pos_side,
    "ordType": "market",
    "sz":      str(total_qty),
})

if r.get("code") != "0":
    msg = r.get("msg", "Unknown")
    if r.get("data"):
        msg = r["data"][0].get("sMsg", msg)
    return {"ok": False, "step": "open", "msg": msg}

order_id = r["data"][0].get("ordId", "")

time.sleep(3)
real_positions = okx_get_positions()
pos_actually_open = any(
    p.get("posSide") == pos_side and abs(float(p.get("pos", 0))) > 0
    for p in real_positions
)

if not pos_actually_open:
    log.error(f"SANITY FAIL: позиция {order_id} не появилась на бирже!")
    send_telegram(
        f"🚨 <b>ФАНТОМНАЯ СДЕЛКА</b>\n"
        f"Биржа приняла ордер ({order_id}), но позиция не открылась.\n"
        f"Запись в историю отменена."
    )
    return {"ok": False, "step": "sanity_check", "msg": "Position not found on exchange"}

log.info(f"SANITY OK: позиция {pos_side} qty={total_qty} подтверждена биржей")

# SL
sl_algo_id = None
for _ in range(3):
    sl_r = okx_post("/api/v5/trade/order-algo", {
        "instId":   SYMBOL,
        "tdMode":   "cross",
        "side":     cls_side,
        "posSide":  pos_side,
        "ordType":  "conditional",
        "sz":       str(total_qty),
        "slTriggerPx":     str(round(sl, 2)),
        "slOrdPx":         "-1",
        "slTriggerPxType": "last",
    })
    if sl_r.get("code") == "0":
        sl_algo_id = sl_r["data"][0].get("algoId", "")
        break
    time.sleep(1)

# TP1: 50% позиции на 1.5 ATR (ФИКС v6.4: atr берём из аргумента)
if atr_val and atr_val > 0:
    tp1_dist = atr_val * 1.5
else:
    tp1_dist = abs(entry - sl) * 0.5
tp1_price = round(entry + tp1_dist, 2) if direction == "LONG" else round(entry - tp1_dist, 2)
tp1_qty = total_qty // 2
tp1_algo_id = None

if tp1_qty > 0:
    for _ in range(3):
        tp1_r = okx_post("/api/v5/trade/order-algo", {
            "instId":   SYMBOL,
            "tdMode":   "cross",
            "side":     cls_side,
            "posSide":  pos_side,
            "ordType":  "conditional",
            "sz":       str(tp1_qty),
            "tpTriggerPx":     str(tp1_price),
            "tpOrdPx":         "-1",
            "tpTriggerPxType": "last",
        })
        if tp1_r.get("code") == "0":
            tp1_algo_id = tp1_r["data"][0].get("algoId", "")
            log.info(f"TP1={tp1_price:.2f} qty={tp1_qty}")
            break
        time.sleep(1)

# TP2: оставшиеся на основном TP
tp2_qty = total_qty - tp1_qty
tp_algo_id = None
if tp2_qty > 0:
    for _ in range(3):
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId":   SYMBOL,
            "tdMode":   "cross",
            "side":     cls_side,
            "posSide":  pos_side,
            "ordType":  "conditional",
            "sz":       str(tp2_qty),
            "tpTriggerPx":     str(round(tp, 2)),
            "tpOrdPx":         "-1",
            "tpTriggerPxType": "last",
        })
        if tp_r.get("code") == "0":
            tp_algo_id = tp_r["data"][0].get("algoId", "")
            log.info(f"TP2={tp:.2f} qty={tp2_qty}")
            break
        time.sleep(1)

if sl_algo_id is None and tp_algo_id is None:
    send_telegram(
        f"🚨🚨🚨 <b>ГОЛАЯ ПОЗИЦИЯ!</b>\n"
        f"{direction} qty={total_qty} entry={entry:.2f}\n"
        f"НИ SL НИ TP не поставились!\n"
        f"Ручное вмешательство обязательно!"
    )
elif sl_algo_id is None:
    send_telegram(
        f"⚠️ <b>SL не поставлен</b>\n"
        f"{direction} entry={entry:.2f} TP={tp:.2f}\n"
        f"Позиция без стопа!"
    )
elif tp_algo_id is None:
    send_telegram(
        f"⚠️ <b>TP не поставлен</b>\n"
        f"{direction} entry={entry:.2f} SL={sl:.2f}"
    )

active_positions[order_id] = {
    "direction":  direction,
    "entry":      entry,
    "sl":         sl,
    "tp":         tp,
    "total_qty":  total_qty,
    "pos_side":   pos_side,
    "cls_side":   cls_side,
    "open_time":  time.time(),
    "sl_algo_id": sl_algo_id,
    "tp_algo_id": tp_algo_id,
    "tp1_algo_id": tp1_algo_id,
    "sl_ok":      sl_algo_id is not None,
    "tp_ok":      tp_algo_id is not None,
    "tp1_ok":     tp1_algo_id is not None,
}
storage_save_async()

return {
    "ok": True,
    "orderId": order_id,
    "total_qty": total_qty,
    "sl_ok": sl_algo_id is not None,
    "tp_ok": tp_algo_id is not None,
}
```

# ========================================================

# ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ

# ========================================================

def check_closed_positions():
global losses_in_row, MIN_SCORE

```
if not active_positions:
    return

try:
    current = okx_get_positions()
    open_sides = set()
    for p in current:
        if abs(float(p.get("pos", 0))) > 0:
            open_sides.add(p.get("posSide"))

    now = time.time()

    for order_id in list(active_positions.keys()):
        pos = active_positions.get(order_id)
        if not pos:
            continue

        age_hrs = (now - pos.get("open_time", now)) / 3600
        if (pos.get("pos_side") in open_sides
            and age_hrs > MAX_HOLD_HOURS):
            log.warning(f"Time-stop: {order_id} висит {age_hrs:.1f}ч > {MAX_HOLD_HOURS}ч -- закрываю")
            send_telegram(
                f"⏱ <b>TIME-STOP</b>\n"
                f"Позиция {pos['direction']} висит {age_hrs:.1f}ч\n"
                f"Принудительное закрытие по рынку"
            )
            okx_cancel_algo(pos.get("sl_algo_id"))
            okx_cancel_algo(pos.get("tp_algo_id"))
            okx_cancel_algo(pos.get("tp1_algo_id"))
            okx_post("/api/v5/trade/order", {
                "instId":  SYMBOL,
                "tdMode":  "cross",
                "side":    pos["cls_side"],
                "posSide": pos["pos_side"],
                "ordType": "market",
                "sz":      str(pos["total_qty"]),
            })
            # Помечаем, чтобы не открывать в эту сторону до фиксации fills
            pos["closing"] = True
            continue

        if pos.get("pos_side") in open_sides:
            continue

        _handle_position_close(order_id, pos)

except Exception as e:
    log.error(f"check_closed_positions: {e}")
```

def _handle_position_close(order_id, pos):
global losses_in_row, MIN_SCORE
global last_close_ts, last_close_result, last_close_direction, last_close_price

```
direction = pos["direction"]
entry     = pos["entry"]
cls_side  = pos["cls_side"]
pos_side  = pos["pos_side"]
qty       = pos.get("total_qty", 0)
open_ms   = int(pos["open_time"] * 1000)

r = okx_get_fills_history(open_ms - 1000)
fills = r.get("data", [])

close_fills = [
    f for f in fills
    if f.get("side") == cls_side
    and f.get("posSide") == pos_side
    and float(f.get("ts", 0)) >= open_ms - 1000
]

if not close_fills:
    log.info(f"Позиция {order_id} закрыта но fills не найдены -- жду")
    return

total_close_qty = sum(float(f["fillSz"]) for f in close_fills)
if total_close_qty <= 0:
    return
avg_close = sum(float(f["fillPx"]) * float(f["fillSz"]) for f in close_fills) / total_close_qty

if direction == "LONG":
    pnl_usdt = (avg_close - entry) * total_close_qty * 0.01
else:
    pnl_usdt = (entry - avg_close) * total_close_qty * 0.01

tp_px = pos.get("tp", 0)
sl_px = pos.get("sl", 0)
if direction == "LONG":
    if tp_px and avg_close >= tp_px * 0.995:
        reason = "🎯 ТЕЙК"
    elif sl_px and avg_close <= sl_px * 1.005:
        reason = "🛑 СТОП"
    else:
        reason = "Закрыта"
else:
    if tp_px and avg_close <= tp_px * 1.005:
        reason = "🎯 ТЕЙК"
    elif sl_px and avg_close >= sl_px * 0.995:
        reason = "🛑 СТОП"
    else:
        reason = "Закрыта"

okx_cancel_algo(pos.get("sl_algo_id"))
okx_cancel_algo(pos.get("tp_algo_id"))
okx_cancel_algo(pos.get("tp1_algo_id"))

is_win = pnl_usdt > 0
_update_stats(pnl_usdt, is_win)
_update_signal_result(order_id, "TP" if is_win else "SL", pnl_usdt)

last_close_ts        = time.time()
last_close_result    = "TP" if is_win else "SL"
last_close_direction = direction
last_close_price     = avg_close

if is_win:
    losses_in_row = 0
    if MIN_SCORE != BASE_MIN_SCORE:
        MIN_SCORE = BASE_MIN_SCORE
else:
    losses_in_row += 1
    if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
        MIN_SCORE += 0.5
        send_telegram(f"⚠️ 3 убытка подряд → MIN_SCORE = {MIN_SCORE}")

winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
avg_pnl = stats["total_profit"] / stats["total"] if stats["total"] > 0 else 0
pf = (stats["total_profit_sum"] / stats["total_loss_sum"]) if stats["total_loss_sum"] > 0 else 0

send_telegram(
    f"{'✅' if is_win else '🔴'} <b>СДЕЛКА ЗАКРЫТА</b>\n\n"
    f"Направление: {direction}\n"
    f"Вход:   {entry:.2f}\n"
    f"Выход:  {avg_close:.2f}\n"
    f"Причина: {reason}\n"
    f"P&L: <b>{pnl_usdt:+.2f}</b> USDT\n\n"
    f"<b>СТАТИСТИКА:</b>\n"
    f"Всего: {stats['total']} | ✅ {stats['wins']} ({winrate:.1f}%)\n"
    f"P&L итого: {stats['total_profit']:+.2f} | Ср: {avg_pnl:+.2f}\n"
    f"Лучшая: {stats['best_trade']:+.2f} | Худшая: {stats['worst_trade']:+.2f}\n"
    f"Profit factor: {pf:.2f} | Просадка: {stats['max_drawdown']:.2f}\n"
    f"MIN: {MIN_SCORE}\n"
    f"🧠 До ML retrain: {ML_RETRAIN_EVERY - ml_train_counter - 1} сделок"
)

del active_positions[order_id]
storage_save_async()

_maybe_retrain()
```

def _update_stats(pnl, is_win):
if is_win:
stats[“wins”] += 1
stats[“total_profit_sum”] += pnl
else:
stats[“losses”] += 1
stats[“total_loss_sum”] += abs(pnl)

```
stats["total"] += 1
stats["total_profit"] += pnl

if pnl > stats["best_trade"]:
    stats["best_trade"] = pnl
if pnl < stats["worst_trade"]:
    stats["worst_trade"] = pnl

stats["current_equity"] += pnl
if stats["current_equity"] > stats["peak_equity"]:
    stats["peak_equity"] = stats["current_equity"]
dd = stats["peak_equity"] - stats["current_equity"]
if dd > stats["max_drawdown"]:
    stats["max_drawdown"] = dd
```

# ========================================================

# ДАННЫЕ С БИРЖ

# ========================================================

def get_klines(sym, interval, limit=150):
try:
url = f”https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}”
data = requests.get(url, timeout=10).json()
df = pd.DataFrame(data, columns=[
“time”, “open”, “high”, “low”, “close”, “volume”,
“ct”, “qv”, “trades”, “taker_buy_base”, “tbq”, “ignore”,
])
for c in (“open”, “high”, “low”, “close”, “volume”, “taker_buy_base”):
df[c] = df[c].astype(float)
df[“candle_time”] = pd.to_datetime(df[“time”], unit=“ms”)
return df
except Exception as e:
log.error(f”klines {sym}: {e}”)
return None

def get_funding():
try:
r = requests.get(
f”https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}”,
timeout=8,
).json()
return float(r[“lastFundingRate”])
except Exception:
return 0.0

def get_orderbook():
try:
d = requests.get(
f”https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=50”,
timeout=8,
).json()
bids = sum(float(b[1]) for b in d[“bids”])
asks = sum(float(a[1]) for a in d[“asks”])
tot = bids + asks
imb = round((bids - asks) / tot * 100, 1) if tot else 0.0

```
    best_bid = float(d["bids"][0][0])
    best_ask = float(d["asks"][0][0])
    mid = (best_bid + best_ask) / 2
    spread_pct = (best_ask - best_bid) / mid if mid > 0 else 0.0
    return imb, spread_pct
except Exception:
    return 0.0, 0.0
```

def get_btc_momentum():
try:
df = get_klines(BTC_SYMBOL, “3m”, 5)
if df is None or len(df) < 3:
return 0.0, 0
chg = (df.iloc[-1][“close”] - df.iloc[-3][“close”]) / df.iloc[-3][“close”]
btc_dir = 1 if df.iloc[-1][“close”] > df.iloc[-2][“close”] else -1
return round(chg, 4), btc_dir
except Exception:
return 0.0, 0

def get_yesterday_levels():
try:
df = get_klines(SYMBOL_BN, “1d”, 2)
if df is not None and len(df) >= 2:
y = df.iloc[-2]
return float(y[“high”]), float(y[“low”])
except Exception:
pass
return 0.0, 0.0

def get_fear_greed():
now = time.time()
if now - cache[“fear_greed”][“ts”] < 3600:
return cache[“fear_greed”][“value”]
try:
v = int(requests.get(“https://api.alternative.me/fng/?limit=1”, timeout=10).json()
[“data”][0][“value”])
cache[“fear_greed”] = {“value”: v, “ts”: now}
return v
except Exception:
return 50

def get_long_short_ratio():
now = time.time()
if now - cache[“long_short”][“ts”] < 300:
return cache[“long_short”][“value”]
try:
v = float(requests.get(
“https://fapi.binance.com/fapi/v1/topLongShortAccountRatio?”
“symbol=ETHUSDT&period=5m&limit=1”, timeout=10).json()[0][“longShortRatio”])
cache[“long_short”] = {“value”: v, “ts”: now}
return v
except Exception:
return 1.0

def get_taker_ratio():
now = time.time()
if now - cache[“taker_ratio”][“ts”] < 300:
return cache[“taker_ratio”][“value”]
try:
v = float(requests.get(
“https://fapi.binance.com/fapi/v1/takerlongshortRatio?”
“symbol=ETHUSDT&period=5m&limit=1”, timeout=10).json()[0][“buySellRatio”])
cache[“taker_ratio”] = {“value”: v, “ts”: now}
return v
except Exception:
return 1.0

def get_open_interest():
now = time.time()
if now - cache[“open_interest”][“ts”] < 300:
return cache[“open_interest”][“value”], cache[“open_interest”][“change”]
try:
oi = float(requests.get(
f”https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL_BN}”,
timeout=8).json()[“openInterest”])
prev = cache[“open_interest”][“value”]
chg = ((oi - prev) / prev * 100) if prev > 0 else 0
cache[“open_interest”] = {“value”: oi, “change”: chg, “ts”: now}
return oi, chg
except Exception:
return 0, 0

def get_dxy():
now = time.time()
if now - cache[“dxy”][“ts”] < 3600:
return cache[“dxy”][“value”], cache[“dxy”][“change”]
try:
if TWELVE_API_KEY:
d = requests.get(
f”https://api.twelvedata.com/quote?symbol=DXY&apikey={TWELVE_API_KEY}”,
timeout=10).json()
v = float(d.get(“close”, 100))
prev = float(d.get(“previous_close”, 100))
chg = ((v - prev) / prev * 100) if prev > 0 else 0
cache[“dxy”] = {“value”: v, “change”: chg, “ts”: now}
return v, chg
r = requests.get(
“https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=5d”,
headers={“User-Agent”: “Mozilla/5.0”}, timeout=10).json()
meta = r[“chart”][“result”][0][“meta”]
v = meta[“regularMarketPrice”]
chg = (v - meta[“previousClose”]) / meta[“previousClose”] * 100
cache[“dxy”] = {“value”: v, “change”: chg, “ts”: now}
return v, chg
except Exception:
return 100, 0

def get_vix():
now = time.time()
if now - cache[“vix”][“ts”] < 3600:
return cache[“vix”][“value”]
try:
if TWELVE_API_KEY:
v = float(requests.get(
f”https://api.twelvedata.com/quote?symbol=VIX&apikey={TWELVE_API_KEY}”,
timeout=10).json().get(“close”, 20))
cache[“vix”] = {“value”: v, “ts”: now}
return v
r = requests.get(
“https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d”,
headers={“User-Agent”: “Mozilla/5.0”}, timeout=10).json()
v = r[“chart”][“result”][0][“meta”][“regularMarketPrice”]
cache[“vix”] = {“value”: v, “ts”: now}
return v
except Exception:
return 20

def _coingecko_global():
return requests.get(“https://api.coingecko.com/api/v3/global”, timeout=10).json()[“data”]

def get_usdt_dominance():
now = time.time()
if now - cache[“usdt_dom”][“ts”] < 600:
return cache[“usdt_dom”][“value”], cache[“usdt_dom”][“change”]
try:
v = float(_coingecko_global()[“market_cap_percentage”][“usdt”])
prev = cache[“usdt_dom”][“value”]
chg = v - prev if prev > 0 else 0
cache[“usdt_dom”] = {“value”: v, “change”: chg, “ts”: now}
return v, chg
except Exception:
return 5.0, 0

def get_btc_dominance():
now = time.time()
if now - cache[“btc_dom”][“ts”] < 600:
return cache[“btc_dom”][“value”], cache[“btc_dom”][“change”]
try:
v = float(_coingecko_global()[“market_cap_percentage”][“btc”])
prev = cache[“btc_dom”][“value”]
chg = v - prev if prev > 0 else 0
cache[“btc_dom”] = {“value”: v, “change”: chg, “ts”: now}
return v, chg
except Exception:
return 50, 0

def get_coinbase_premium():
now = time.time()
if now - cache[“cb_premium”][“ts”] < 60:
return cache[“cb_premium”][“value”]
try:
cb = float(requests.get(
“https://api.exchange.coinbase.com/products/ETH-USD/ticker”,
timeout=8).json()[“price”])
bn = float(requests.get(
f”https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL_BN}”,
timeout=8).json()[“price”])
p = round((cb - bn) / bn * 100, 4)
cache[“cb_premium”] = {“value”: p, “ts”: now}
return p
except Exception:
return 0.0

def get_eth_btc_ratio():
now = time.time()
if now - cache[“eth_btc”][“ts”] < 300:
return cache[“eth_btc”][“value”], cache[“eth_btc”][“change”]
try:
eth = float(requests.get(
“https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT”, timeout=8).json()[“price”])
btc = float(requests.get(
“https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT”, timeout=8).json()[“price”])
ratio = eth / btc if btc > 0 else 0.0
prev = cache[“eth_btc”][“value”]
chg = ((ratio - prev) / prev * 100) if prev > 0 else 0.0
cache[“eth_btc”] = {“value”: ratio, “change”: round(chg, 4), “ts”: now}
return ratio, round(chg, 4)
except Exception:
return 0.0, 0.0

def get_funding_avg():
now = time.time()
if now - cache[“funding_avg”][“ts”] < 300:
return cache[“funding_avg”][“value”]
try:
rates = []
b = requests.get(
f”https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}”,
timeout=8).json()
rates.append(float(b.get(“lastFundingRate”, 0)))
o = requests.get(
f”https://www.okx.com/api/v5/public/funding-rate?instId={SYMBOL}”,
timeout=8).json()
if o.get(“data”):
rates.append(float(o[“data”][0].get(“fundingRate”, 0)))
avg = sum(rates) / len(rates) if rates else 0.0
cache[“funding_avg”] = {“value”: avg, “ts”: now}
return avg
except Exception:
return 0.0

def get_4h_trend():
now = time.time()
if now - cache[“4h_trend”][“ts”] < 3600:
return cache[“4h_trend”][“diff”], cache[“4h_trend”][“bull”]
try:
df = get_klines(SYMBOL_BN, “4h”, 200)
if df is None or len(df) < 200:
return 0.0, False
e50 = df[“close”].ewm(span=50, adjust=False).mean().iloc[-1]
e200 = df[“close”].ewm(span=200, adjust=False).mean().iloc[-1]
diff = (e50 - e200) / e200 * 100
cache[“4h_trend”] = {“diff”: diff, “bull”: e50 > e200, “ts”: now}
return diff, e50 > e200
except Exception:
return 0.0, False

def get_1h_trend():
now = time.time()
if now - cache[“1h_trend”][“ts”] < 300:
return cache[“1h_trend”][“price_vs_ema”], cache[“1h_trend”][“bull”]
try:
df = get_klines(SYMBOL_BN, “1h”, 60)
if df is None or len(df) < 50:
return 0.0, True
ema50 = df[“close”].ewm(span=50, adjust=False).mean().iloc[-1]
cur = df[“close”].iloc[-1]
pct = (cur - ema50) / ema50 * 100
cache[“1h_trend”] = {“price_vs_ema”: round(pct, 3), “bull”: cur > ema50, “ts”: now}
return round(pct, 3), cur > ema50
except Exception:
return 0.0, True

def detect_whales():
from sklearn.ensemble import IsolationForest
now = time.time()
if now - cache[“whales”][“ts”] < 60:
return cache[“whales”][“detected”]
try:
d = requests.get(
f”https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=10”,
timeout=8).json()
bids = [[float(b[0]), float(b[1])] for b in d[“bids”][:10]]
asks = [[float(a[0]), float(a[1])] for a in d[“asks”][:10]]
X = np.array(bids + asks)
if len(X) < 5:
return False
preds = IsolationForest(contamination=0.1, random_state=42).fit_predict(X)
det = -1 in preds
cache[“whales”] = {“detected”: det, “ts”: now}
return det
except Exception:
return False

def get_eth_btc_correlation():
# ФИКС v6.4: добавлен кэш на 300с (раньше 2 klines-запроса каждый скан)
now = time.time()
if now - cache[“eth_btc_corr”][“ts”] < 300:
return cache[“eth_btc_corr”][“value”]
try:
e = get_klines(“ETHUSDT”, “5m”, 30)
b = get_klines(“BTCUSDT”, “5m”, 30)
if e is None or b is None:
return 1.0
corr = e[“close”].pct_change().dropna().corr(b[“close”].pct_change().dropna())
val = corr if not np.isnan(corr) else 1.0
cache[“eth_btc_corr”] = {“value”: val, “ts”: now}
return val
except Exception:
return 1.0

# ========================================================

# ИНДИКАТОРЫ (с дневным VWAP reset)

# ========================================================

def calc(df):
global last_vwap_reset_day

```
df["EMA9"]   = df["close"].ewm(span=9,   adjust=False).mean()
df["EMA21"]  = df["close"].ewm(span=21,  adjust=False).mean()
df["EMA50"]  = df["close"].ewm(span=50,  adjust=False).mean()
df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

d = df["close"].diff()
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

bm = df["close"].rolling(20).mean()
bs = df["close"].rolling(20).std()
df["BB_up"]  = bm + 2 * bs
df["BB_dn"]  = bm - 2 * bs
df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)

df["day"] = df["candle_time"].dt.day
df["vp"]  = df["close"] * df["volume"]
df["VWAP"] = (
    df.groupby("day")["vp"].cumsum() /
    df.groupby("day")["volume"].cumsum().replace(0, np.nan)
)

df["CVD_raw"]   = df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])
df["CVD"]       = df["CVD_raw"].rolling(20).sum()
df["CVD_up"]    = df["CVD"] > df["CVD"].shift(3)
df["CVD_accel"] = df["CVD"] - df["CVD"].shift(6)

df["vol_ma"]      = df["volume"].rolling(20).mean()
df["vol_spike"]   = df["volume"] > df["vol_ma"] * 1.3
df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
df["price_dir"]   = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
df["vol_dir"]     = df["vol_spike"].astype(int) * df["price_dir"]

up = df["high"] - df["high"].shift()
dn = df["low"].shift() - df["low"]
tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
pdm = np.where((up > dn) & (up > 0), up, 0)
ndm = np.where((dn > up) & (dn > 0), dn, 0)
atr14 = tr.rolling(14).mean()
pdi = 100 * (pd.Series(pdm, index=df.index).rolling(14).mean() / atr14)
ndi = 100 * (pd.Series(ndm, index=df.index).rolling(14).mean() / atr14)
dx  = 100 * (abs(pdi - ndi) / (pdi + ndi + 1e-9))
df["ADX"] = dx.rolling(14).mean()
df["+DI"] = pdi
df["-DI"] = ndi

df["OBV"]    = (df["volume"] * np.sign(df["close"].diff())).cumsum()
df["OBV_ma"] = df["OBV"].rolling(20).mean()

body = (df["close"] - df["open"]).abs()
lw = df[["open", "close"]].min(axis=1) - df["low"]
uw = df["high"] - df[["open", "close"]].max(axis=1)
df["hammer"]  = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])

df["rsi_div_bull"] = (df["close"] < df["close"].shift(5)) & (df["RSI"] > df["RSI"].shift(5)) & (df["RSI"] < 50)
df["rsi_div_bear"] = (df["close"] > df["close"].shift(5)) & (df["RSI"] < df["RSI"].shift(5)) & (df["RSI"] > 50)

return df
```

def confirm_1h_bias():
“”“Лёгкое 1h-подтверждение: ТОЛЬКО EMA/RSI/MACD по 1h-свечам.
ФИКС v6.4: заменяет рекурсивный get_signal(df_1h), который повторно
дёргал все внешние API каждый скан. Возвращает ‘LONG’/‘SHORT’/None.”””
try:
df = get_klines(SYMBOL_BN, “1h”, 100)
if df is None or len(df) < 50:
return None
df = calc(df)
row = df.iloc[-1]
ema_bull = row[“EMA9”] > row[“EMA21”] > row[“EMA50”]
ema_bear = row[“EMA9”] < row[“EMA21”] < row[“EMA50”]
macd_bull = row[“MACD”] > row[“MACD_sig”]
rsi = row[“RSI”]
long_votes  = sum([ema_bull, macd_bull, rsi < 50])
short_votes = sum([ema_bear, not macd_bull, rsi > 50])
if long_votes >= 2 and ema_bull:
return “LONG”
if short_votes >= 2 and ema_bear:
return “SHORT”
return None
except Exception as e:
log.error(f”confirm_1h_bias: {e}”)
return None

# ========================================================

# СИГНАЛ

# ========================================================

def get_signal(df, funding, ob, spread_pct, btc_mom, btc_dir):
global ob_history, last_ob, yesterday_high, yesterday_low
global force_test_done, red_news_until, current_mode

```
if df is None or len(df) < 50:
    return None, None, None, None, 0, "Нет данных", 0, 0, {}
if len(df) < 6:
    return None, None, None, None, 0, f"Мало свечей ({len(df)})", 0, 0, {}

row   = df.iloc[-1]
price = row["close"]
rsi   = row["RSI"]
atr   = row["ATR"]
adx   = row["ADX"] if not np.isnan(row.get("ADX", float("nan"))) else 20

# ЕДИНЫЙ режим (ФИКС v6.4): гистерезис 20/30, и ВСЕ секции скоринга
# ниже смотрят на is_channel, а не на разрозненный adx<25.
if current_mode == "Тренд" and adx < REGIME_LOW:
    current_mode = "Канал"
elif current_mode == "Канал" and adx > REGIME_HIGH:
    current_mode = "Тренд"
is_channel = (current_mode == "Канал")

if not is_channel:
    current_min_score = BASE_MIN_SCORE
    sl_mult = SL_ATR_MULT
    tp_mult = TP_ATR_MULT
else:
    current_min_score = BASE_MIN_SCORE - 1.5
    sl_mult = SL_ATR_MULT * 0.7
    tp_mult = TP_ATR_MULT * 0.6

# Жёсткие фильтры
if atr < price * ATR_MIN_PCT:
    return None, None, None, None, 0, "Рынок мёртв (ATR)", 0, 0, {}
if spread_pct > SPREAD_MAX_PCT:
    return None, None, None, None, 0, f"Спред {spread_pct*100:.3f}%", 0, 0, {}
if adx < ADX_MIN:
    return None, None, None, None, 0, f"ADX={adx:.0f} < {ADX_MIN}", 0, 0, {}

if TRADING_HOURS is not None:
    h_now = datetime.now(timezone.utc).hour
    if not (TRADING_HOURS[0] <= h_now < TRADING_HOURS[1]):
        return None, None, None, None, 0, f"Вне окна {TRADING_HOURS[0]}-{TRADING_HOURS[1]} UTC", 0, 0, {}

now = time.time()
hour = datetime.now(timezone.utc).hour

sec_since_candle = (
    datetime.now(timezone.utc) - row["candle_time"].replace(tzinfo=timezone.utc)
).total_seconds()
if sec_since_candle < 30:
    return None, None, None, None, 0, f"Свеча свежая ({int(sec_since_candle)}с)", 0, 0, {}

corr = get_eth_btc_correlation()
if corr < ETH_BTC_CORR_MIN:
    return None, None, None, None, 0, f"Корреляция {corr:.2f}", 0, 0, {}

if FORCE_TEST and not force_test_done:
    force_test_done = True
    storage_save_async()
    sl_dist = max(price * SL_PCT_MIN, atr * SL_ATR_MULT)
    tp_dist = max(price * SL_PCT_MIN * 2, atr * TP_ATR_MULT)
    return "LONG", price, round(price - sl_dist, 2), round(price + tp_dist, 2), 5.0, "FORCE TEST", 5.0, 0, {}

# Стакан
ob_history.append(ob)
if len(ob_history) > 6:
    ob_history.pop(0)
ob_rising  = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2
ob_delta = ob - last_ob if last_ob != 0 else 0
last_ob = ob

# Метрики
fear_greed   = get_fear_greed()
long_short   = get_long_short_ratio()
taker_ratio  = get_taker_ratio()
oi, oi_chg   = get_open_interest()
dxy, dxy_chg = get_dxy()
vix          = get_vix()
_, usdt_chg  = get_usdt_dominance()
cb_premium   = get_coinbase_premium()
_, eth_btc_c = get_eth_btc_ratio()
funding_avg  = get_funding_avg()
_, btc_dom_c = get_btc_dominance()
ema_4h_diff, ema_4h_bull = get_4h_trend()
p_vs_1h, trend_1h_bull   = get_1h_trend()

obv_div = 0
if not np.isnan(row.get("OBV", float("nan"))) and not np.isnan(row.get("OBV_ma", float("nan"))):
    if len(df) >= 6:
        if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]:
            obv_div = -1
        elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]:
            obv_div = 1

ema21_val = row["EMA21"]
dist_from_ema21 = abs(price - ema21_val) / atr if atr > 0 else 0
overheat_long  = price > ema21_val + EMA21_MAX_DIST_ATR * atr
overheat_short = price < ema21_val - EMA21_MAX_DIST_ATR * atr

L = S = 0.0

# ===== ЯДРО =========================================
# 1. EMA
if   row["EMA9"] > row["EMA21"] > row["EMA50"]: L += 1.0
elif row["EMA9"] < row["EMA21"] < row["EMA50"]: S += 1.0
elif row["EMA9"] > row["EMA21"]: L += 0.5
elif row["EMA9"] < row["EMA21"]: S += 0.5

# 2. 4H тренд
if ema_4h_bull: L += 1.0
else:           S += 1.0
if abs(ema_4h_diff) > 2.0:
    if ema_4h_bull and S > L:
        S = max(0, S - 1.5)
    if not ema_4h_bull and L > S:
        L = max(0, L - 1.5)

# 3. 1H тренд
if abs(p_vs_1h) >= 0.3:
    if trend_1h_bull:
        S = max(0, S - 1.5)
        if L > S: L += 0.25
    else:
        L = max(0, L - 1.5)
        if S > L: S += 0.25

# 4. RSI (ФИКС v6.4: ветка по is_channel, не по adx<25)
if is_channel:
    if   rsi < 30: L += 1.0 * tuner_rsi_long_mult
    elif rsi < 35: L += 0.75 * tuner_rsi_long_mult
    elif rsi < 45: L += 0.5 * tuner_rsi_long_mult
    elif rsi > 70: S += 1.0 * tuner_rsi_short_mult
    elif rsi > 65: S += 0.75 * tuner_rsi_short_mult
    elif rsi > 55: S += 0.5 * tuner_rsi_short_mult
else:
    if   rsi < 35: L += 1.0
    elif rsi < 45: L += 0.5
    elif rsi > 65: S += 1.0
    elif rsi > 55: S += 0.5

# 5. MACD
if row["MACD_bull"]: L += 1.0
elif row["MACD"] > row["MACD_sig"]: L += 0.5
if row["MACD_bear"]: S += 1.0
elif row["MACD"] < row["MACD_sig"]: S += 0.5
if row["MACD_hist"] > 0 and row["MACD_hist"] > df.iloc[-3]["MACD_hist"]: L += 0.25
if row["MACD_hist"] < 0 and row["MACD_hist"] < df.iloc[-3]["MACD_hist"]: S += 0.25

# 6. CVD (вес 0.75 — частично дублирует vol_dir)
if row["CVD_up"]: L += 0.75
else:             S += 0.75
if not np.isnan(row.get("CVD_accel", float("nan"))):
    if row["CVD_accel"] > 0: L += 0.25
    elif row["CVD_accel"] < 0: S += 0.25

# 7. Объём (вес 0.75)
if row["vol_spike"]:
    if row["vol_dir"] > 0: L += 0.75
    elif row["vol_dir"] < 0: S += 0.75
if row["vol_extreme"]:
    if L > S: L += 0.5
    else:     S += 0.5

# 8. Стакан
if ob > 5 or ob_rising:  L += 0.5
if ob < -5 or ob_falling: S += 0.5

# 9. RSI дивергенция
if row.get("rsi_div_bull", False): L += 0.75
if row.get("rsi_div_bear", False): S += 0.75

# ADX directional
if adx < REGIME_LOW:
    L = max(0, L - 0.5)
    S = max(0, S - 0.5)
if adx > REGIME_HIGH:
    if not np.isnan(row.get("+DI", float("nan"))):
        if row["+DI"] > row["-DI"]: L += 0.25
        else:                       S += 0.25

# ===== СРЕДНИЕ =====================================
if   fear_greed < 25: L += 0.5
elif fear_greed < 40: L += 0.25
elif fear_greed > 75: S += 0.5
elif fear_greed > 65: S += 0.25

if   long_short < 0.8: L += 0.5
elif long_short < 1.1: L += 0.25
elif long_short > 2.5: S += 0.5
elif long_short > 1.8: S += 0.25

if   taker_ratio > 1.3: L += 0.5
elif taker_ratio > 1.1: L += 0.25
elif taker_ratio < 0.7: S += 0.5
elif taker_ratio < 0.9: S += 0.25

if   cb_premium >  0.05: L += 0.5
elif cb_premium >  0.02: L += 0.25
elif cb_premium < -0.05: S += 0.5
elif cb_premium < -0.02: S += 0.25

if   eth_btc_c >  0.3: L += 0.5
elif eth_btc_c >  0.1: L += 0.25
elif eth_btc_c < -0.3: S += 0.5
elif eth_btc_c < -0.1: S += 0.25

if oi_chg > 3:
    if price > df.iloc[-2]["close"]: L += 0.5
    else:                            S += 0.5
elif oi_chg < -3:
    if price < df.iloc[-2]["close"]: S += 0.25
    else:                            L += 0.25

# BB (ФИКС v6.4: ветка по is_channel)
bp = row["BB_pct"]
if is_channel:
    if   bp < 0.1: L += 1.0 * tuner_bb_long_mult
    elif bp < 0.2: L += 0.75 * tuner_bb_long_mult
    elif bp > 0.9: S += 1.0 * tuner_bb_short_mult
    elif bp > 0.8: S += 0.75 * tuner_bb_short_mult
else:
    if   bp < 0.1: L += 0.5
    elif bp < 0.2: L += 0.25
    elif bp > 0.9: S += 0.5
    elif bp > 0.8: S += 0.25

if price < row["VWAP"]: L += 0.5
else:                   S += 0.5

# ===== ВСПОМОГАТЕЛЬНЫЕ =============================
if   btc_mom >  0.002: L += 0.25; S = max(0, S - 0.25)
elif btc_mom < -0.002: S += 0.25; L = max(0, L - 0.25)

if   dxy_chg >  0.3: S += 0.25; L = max(0, L - 0.25)
elif dxy_chg < -0.3: L += 0.25; S = max(0, S - 0.25)

if vix > 35:
    L = max(0, L - 0.5); S = max(0, S - 0.5)
elif vix > 25:
    L = max(0, L - 0.25); S = max(0, S - 0.25)

if   btc_dom_c >  0.2: S += 0.25; L = max(0, L - 0.25)
elif btc_dom_c < -0.2: L += 0.25; S = max(0, S - 0.25)

if   funding_avg >  0.005: S += 0.25
elif funding_avg < -0.005: L += 0.25

if   usdt_chg >  0.2: S += 0.25
elif usdt_chg < -0.2: L += 0.25

if obv_div ==  1: L += 0.25
elif obv_div == -1: S += 0.25

if row["hammer"]:  L += 0.25
if row["shooter"]: S += 0.25

if   ob_delta >  3: L += 0.25
elif ob_delta < -3: S += 0.25

# Штраф за низкий объём
vol_24h_avg = df["volume"].iloc[-96:].mean() if len(df) >= 96 else df["volume"].mean()
vol_ratio = row["volume"] / vol_24h_avg if vol_24h_avg > 0 else 1.0
if vol_ratio < 0.5:
    L = max(0, L - 1.5); S = max(0, S - 1.5)
elif vol_ratio < 0.7:
    L = max(0, L - 1.0); S = max(0, S - 1.0)
elif vol_ratio < 1.0:
    L = max(0, L - 0.5); S = max(0, S - 0.5)

# Штраф за близость к новостям
for ev in NEWS_EVENTS:
    try:
        ev_time = datetime(ev[0], ev[1], ev[2], ev[3], ev[4], tzinfo=timezone.utc)
        dist_min = abs((datetime.now(timezone.utc) - ev_time).total_seconds()) / 60
        if dist_min < 15:
            L = max(0, L - 2.0); S = max(0, S - 2.0)
        elif dist_min < 30:
            L = max(0, L - 1.0); S = max(0, S - 1.0)
        elif dist_min < 60:
            L = max(0, L - 0.5); S = max(0, S - 0.5)
    except Exception:
        pass

# Подтверждение по 1h (ФИКС v6.4: лёгкая функция вместо рекурсии get_signal)
bias_1h = confirm_1h_bias()
if bias_1h == "LONG":
    L += 0.5
elif bias_1h == "SHORT":
    S += 0.5

# ===== ШТРАФЫ =======================================
if NIGHT_HOURS[0] <= hour or hour < NIGHT_HOURS[1]:
    if L >= S: L = max(0, L - 0.75)
    else:      S = max(0, S - 0.75)

if hour >= 20 and datetime.now(timezone.utc).weekday() == 4:
    if L >= S: L = max(0, L - 1.0)
    else:      S = max(0, S - 1.0)

if 8 <= hour < 9:
    if L >= S: L = max(0, L - 0.5)
    else:      S = max(0, S - 0.5)

if datetime.now(timezone.utc).weekday() in [1, 2, 3]:
    if L > S: L += 0.25
    else:     S += 0.25

if red_news_until > now:
    L = 0

if yesterday_high > 0 and yesterday_low > 0:
    if   price < yesterday_low:                  L = max(0, L - 0.5); S = max(0, S - 0.5)
    elif price > yesterday_high:                 S = max(0, S - 0.5); L = max(0, L - 0.5)
    elif yesterday_low < price < yesterday_low * 1.01:
        L += 1.0 if is_channel else 0.25
    elif yesterday_high * 0.99 < price < yesterday_high:
        S += 1.0 if is_channel else 0.25

# Тройное касание уровня (ФИКС v6.4: поддержка -> ТОЛЬКО лонг,
# сопротивление -> ТОЛЬКО шорт. Убраны встречные бонусы.)
if is_channel:
    lows = df["low"].iloc[-20:].values
    highs = df["high"].iloc[-20:].values
    support_level = np.median(lows)
    resistance_level = np.median(highs)
    support_touches = sum(1 for l in lows if abs(l - support_level) / support_level < 0.002)
    resistance_touches = sum(1 for h in highs if abs(h - resistance_level) / resistance_level < 0.002)
    if support_touches >= 3:
        L += 1.5
    if resistance_touches >= 3:
        S += 1.5

# Конфлюэнция (4 из 6)
ema_l = row["EMA9"] > row["EMA21"] > row["EMA50"]
ema_s = row["EMA9"] < row["EMA21"] < row["EMA50"]
macd_l = row["MACD"] > row["MACD_sig"]
macd_s = row["MACD"] < row["MACD_sig"]
cvd_l = row["CVD_up"]
vol_l = row["vol_dir"] > 0
vol_s = row["vol_dir"] < 0
core_long  = sum([ema_l, ema_4h_bull,     rsi < 45, macd_l, cvd_l,      vol_l])
core_short = sum([ema_s, not ema_4h_bull, rsi > 55, macd_s, not cvd_l, vol_s])
if core_long  >= 4: L += 0.75
if core_short >= 4: S += 0.75

# ===== ML БОНУС =====================================
ml_metrics = {
    "fear_greed": fear_greed, "long_short": long_short, "taker_ratio": taker_ratio,
    "oi_change": oi_chg, "cb_premium": cb_premium, "eth_btc_chg": eth_btc_c,
    "funding_avg": funding_avg, "adx": adx, "rsi": rsi,
    "bb_pct": bp, "ob": ob, "btc_mom": btc_mom,
    "spread_pct": spread_pct,
    "hour": hour, "weekday": datetime.now(timezone.utc).weekday(),
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
    f"L:{L} S:{S} D:{L-S:+.1f} | {current_mode} "
    f"ADX:{adx:.0f} CVD:{'↑' if row['CVD_up'] else '↓'} "
    f"RSI:{rsi:.0f} 1h:{p_vs_1h:+.2f}% sp:{spread_pct*100:.3f}% | ML:{ml_b:+.2f}"
)

if L - S >= MIN_SCORE_DIFF and L >= current_min_score and L >= MIN_SCORE_LONG:
    if overheat_long:
        return None, None, None, None, L, f"{reason_str} | Перегрев LONG +{dist_from_ema21:.1f}ATR", L, S, ml_metrics
    sl_dist = max(price * SL_PCT_MIN, min(atr * sl_mult, price * SL_PCT_MAX))
    tp_dist = min(atr * tp_mult, price * TP_PCT_MAX)
    entry = price
    sl    = round(entry - sl_dist, 2)
    tp    = round(entry + tp_dist, 2)
    return "LONG", entry, sl, tp, L, reason_str, L, S, ml_metrics

if S - L >= MIN_SCORE_DIFF and S >= current_min_score and S >= MIN_SCORE_SHORT:
    if overheat_short:
        return None, None, None, None, S, f"{reason_str} | Перегрев SHORT -{dist_from_ema21:.1f}ATR", L, S, ml_metrics
    sl_dist = max(price * SL_PCT_MIN, min(atr * sl_mult, price * SL_PCT_MAX))
    tp_dist = min(atr * tp_mult, price * TP_PCT_MAX)
    entry = price
    sl    = round(entry + sl_dist, 2)
    tp    = round(entry - tp_dist, 2)
    return "SHORT", entry, sl, tp, S, reason_str, L, S, ml_metrics

return None, None, None, None, max(L, S), reason_str, L, S, ml_metrics
```

# ========================================================

# БЭКТЕСТ (ФИКС v6.4: работает на копии кэша, не замораживает глобальный)

# ========================================================

_backtest_lock = threading.Lock()

def run_backtest(days=30):
if not _backtest_lock.acquire(blocking=False):
log.info(“Бэктест уже выполняется – пропускаю”)
return
try:
log.info(f”📊 Бэктест за {days} дней запущен”)
df = get_klines(SYMBOL_BN, TIMEFRAME, limit=days * 24 * 4)
if df is None or len(df) < 50:
send_telegram(“❌ Бэктест: не хватает свечей”)
return
df = calc(df)

```
    # ФИКС: сохраняем кэш и СТАВИМ свежие ts, чтобы геттеры брали из кэша
    # и не ходили в сеть. Главный поток продолжает читать реальные значения —
    # мы НЕ ставим inf и восстанавливаем кэш в finally.
    global cache
    cache_backup = {k: dict(v) for k, v in cache.items()}
    frozen_now = time.time()
    for k in cache:
        cache[k]["ts"] = frozen_now  # свежий ts -> геттеры вернут кэш без сети

    trades = []
    for i in range(50, len(df) - 1):
        window = df.iloc[:i+1].copy()
        try:
            sig = get_signal(window, 0.0, 0.0, 0.0, 0.0, 0)
        except Exception:
            continue
        if sig[0] is None:
            continue
        direction, entry, sl, tp, score, reason, L, S, _ = sig
        future = df.iloc[i+1:]
        close_price = None
        close_reason = None
        for j in range(len(future)):
            high = future.iloc[j]["high"]
            low = future.iloc[j]["low"]
            if direction == "LONG":
                if low <= sl:
                    close_price = sl; close_reason = "SL"; break
                if high >= tp:
                    close_price = tp; close_reason = "TP"; break
            else:
                if high >= sl:
                    close_price = sl; close_reason = "SL"; break
                if low <= tp:
                    close_price = tp; close_reason = "TP"; break
        if close_price:
            pnl = (close_price - entry) / entry * 100 if direction == "LONG" else (entry - close_price) / entry * 100
            trades.append({
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "score": score, "L": L, "S": S, "result": close_reason,
                "label": 1 if close_reason == "TP" else 0, "pnl_pct": round(pnl, 4),
                "source": "backtest", "weight": BACKTEST_VIRTUAL_WEIGHT,
                "timestamp": time.time() - (len(df) - i) * 5 * 60, "metrics": {},
            })

    # Восстановить кэш
    for k in cache_backup:
        cache[k] = cache_backup[k]

    if trades:
        wins = sum(1 for t in trades if t["label"] == 1)
        wr = wins / len(trades) * 100
        msg = (f"📊 БЭКТЕСТ за {days} дней\n"
               f"Сделок: {len(trades)} | ✅ {wins} | 🔴 {len(trades)-wins}\n"
               f"Винрейт: {wr:.1f}%\n"
               f"Добавлено в ML с весом {BACKTEST_VIRTUAL_WEIGHT}")
        log.info(msg)
        send_telegram(msg)
        for t in trades:
            signals_history.append(t)
        storage_save_async()
        log.info(f"Бэктест: {len(trades)} сделок добавлено")
    else:
        log.info(f"Бэктест за {days} дней: 0 сигналов")
        send_telegram(f"📊 Бэктест за {days} дней: 0 сигналов")
finally:
    _backtest_lock.release()
```

# ========================================================

# ML

# ========================================================

ML_FEATURE_NAMES = [
“L”, “S”, “L-S”, “FG”, “L/S”, “Taker”, “OI%”, “CB”,
“ETH/BTC”, “Fund”, “ADX”, “RSI”, “BB%”, “OB”, “BTCmom”,
“Spread”, “Hour”, “Wday”,
]

def _ml_features(L, S, m):
return [
L, S, L - S,
m.get(“fear_greed”, 50),
m.get(“long_short”, 1.0),
m.get(“taker_ratio”, 1.0),
m.get(“oi_change”, 0),
m.get(“cb_premium”, 0),
m.get(“eth_btc_chg”, 0),
m.get(“funding_avg”, 0),
m.get(“adx”, 20),
m.get(“rsi”, 50),
m.get(“bb_pct”, 0.5),
m.get(“ob”, 0),
m.get(“btc_mom”, 0),
m.get(“spread_pct”, 0),
m.get(“hour”, 12),
m.get(“weekday”, 2),
]

def _save_signal_to_history(d):
try:
signals_history.append(d)
if len(signals_history) > 1000:
del signals_history[:len(signals_history) - 1000]
storage_save_async()
except Exception as e:
log.error(f”save_signal: {e}”)

def _update_signal_result(order_id, result, pnl):
try:
for s in signals_history:
if s.get(“order_id”) == order_id:
s.update({
“result”: result,
“pnl”: pnl,
“label”: 1 if result == “TP” else 0,
“close_ts”: time.time(),
})
break
storage_save_async()
except Exception as e:
log.error(f”update_signal: {e}”)

def _train_model():
global scalp_model

```
done = [s for s in signals_history if "label" in s and s.get("label") is not None]
if len(done) < ML_MIN_SAMPLES:
    log.info(f"ML: мало данных ({len(done)}/{ML_MIN_SAMPLES})")
    return False
log.info(f"ML: начинаю обучение на {len(done)} примерах...")

try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score

    X = np.array([
        _ml_features(s.get("L", 0), s.get("S", 0), s.get("metrics", {}))
        for s in done
    ])
    y = np.array([s["label"] for s in done])

    now = time.time()
    ts_arr = np.array([s.get("timestamp", now) for s in done])
    ages_days = (now - ts_arr) / 86400
    weights = np.exp(-ages_days / ML_DECAY_DAYS)
    custom_weights = np.array([s.get("weight", 1.0) for s in done])
    weights = weights * custom_weights

    sc = StandardScaler()
    Xs = sc.fit_transform(X)

    m = GradientBoostingClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
    )

    cv_acc = 0.0
    try:
        if len(set(y)) >= 2 and len(y) >= 10:
            cv_acc = float(cross_val_score(m, Xs, y, cv=min(5, len(y) // 5),
                                           scoring="accuracy").mean())
    except Exception as e:
        log.error(f"CV: {e}")

    m.fit(Xs, y, sample_weight=weights)
    scalp_model = {"model": m, "scaler": sc}

    imp = sorted(zip(ML_FEATURE_NAMES, m.feature_importances_),
                 key=lambda x: -x[1])[:5]
    imp_str = " | ".join([f"{n}:{v*100:.0f}%" for n, v in imp])

    wr = sum(y) / len(y)
    stats["ml_trains_count"] += 1
    stats["ml_last_train_ts"] = now
    stats["ml_last_accuracy"] = cv_acc
    stats["ml_samples_at_last_train"] = len(X)

    send_telegram(
        f"🧠 <b>ML обучена</b>\n"
        f"Примеров: {len(X)} | WR в выборке: {wr:.1%}\n"
        f"CV accuracy: {cv_acc:.1%}\n"
        f"Топ признаков: {imp_str}\n"
        f"Всего обучений: {stats['ml_trains_count']}"
    )
    storage_save_async()
    log.info(f"ML: обучение завершено, accuracy={cv_acc:.1%}")
    return True
except Exception as e:
    log.error(f"ML train_model error: {e}")
    send_telegram(f"❌ ML обучение упало: {str(e)[:100]}")
    return False
```

def get_ml_bonus(L, S, m):
if scalp_model is None:
return 0.0
try:
X = np.array([_ml_features(L, S, m)])
Xs = scalp_model[“scaler”].transform(X)
prob = scalp_model[“model”].predict_proba(Xs)[0][1]
if   prob > 0.7: return +0.5
elif prob > 0.6: return +0.25
elif prob < 0.3: return -0.5
elif prob < 0.4: return -0.25
return 0.0
except Exception:
return 0.0

def _maybe_retrain():
global ml_train_counter
ml_train_counter += 1
if ml_train_counter >= ML_RETRAIN_EVERY:
def _do():
global ml_train_counter
if _train_model():
ml_train_counter = 0
storage_save_async()
threading.Thread(target=_do, daemon=True).start()

# ========================================================

# BACKTEST ANALYZER

# ========================================================

ANALYZER_MAX_CHANGE = 0.25

def backtest_analyzer():
global last_analyzer_ts, analyzer_status, MIN_SCORE, MIN_SCORE_LONG, MIN_SCORE_SHORT
now = time.time()
if now - last_analyzer_ts < 7 * 86400:
return
virtual = [s for s in signals_history if s.get(“source”) == “backtest” and “label” in s]
real = [s for s in signals_history if s.get(“source”) != “backtest” and “label” in s
and (now - s.get(“timestamp”, 0)) < 30 * 86400]
if len(virtual) < 5 or len(real) < 5:
analyzer_status = “мало данных”
return
v_wr = sum(1 for s in virtual if s.get(“label”) == 1) / len(virtual) * 100
r_wr = sum(1 for s in real if s.get(“label”) == 1) / len(real) * 100
gap = v_wr - r_wr
if gap > 15:
analyzer_status = f”🚨 разрыв {gap:.0f}% MIN_SCORE -0.25”
MIN_SCORE = max(3.0, MIN_SCORE - ANALYZER_MAX_CHANGE)
MIN_SCORE_LONG = max(3.0, MIN_SCORE_LONG - ANALYZER_MAX_CHANGE)
MIN_SCORE_SHORT = max(2.5, MIN_SCORE_SHORT - ANALYZER_MAX_CHANGE)
last_analyzer_ts = now
send_telegram(f”🚨 Analyzer: разрыв {gap:.0f}%\nMIN_SCORE снижен на {ANALYZER_MAX_CHANGE}”)
elif gap > 10:
analyzer_status = f”⚠️ разрыв {gap:.0f}% MIN_SCORE -0.15”
MIN_SCORE = max(3.0, MIN_SCORE - 0.15)
MIN_SCORE_LONG = max(3.0, MIN_SCORE_LONG - 0.15)
MIN_SCORE_SHORT = max(2.5, MIN_SCORE_SHORT - 0.15)
last_analyzer_ts = now
send_telegram(f”⚠️ Analyzer: разрыв {gap:.0f}%\nMIN_SCORE снижен на 0.15”)
elif gap < -10:
analyzer_status = f”⚠️ разрыв {gap:.0f}% MIN_SCORE +0.15”
MIN_SCORE = min(8.0, MIN_SCORE + 0.15)
MIN_SCORE_LONG = min(8.0, MIN_SCORE_LONG + 0.15)
MIN_SCORE_SHORT = min(7.5, MIN_SCORE_SHORT + 0.15)
last_analyzer_ts = now
send_telegram(f”⚠️ Analyzer: разрыв {gap:.0f}%\nMIN_SCORE повышен на 0.15”)
else:
analyzer_status = f”разрыв {gap:.0f}% — норма”
storage_save_async()

# ========================================================

# WEIGHT TUNER (ФИКС v6.4: шаг +0.05, затухание к 1.0, last_changes реально пишется)

# ========================================================

TUNER_INTERVAL_DAYS = 3
TUNER_MIN_TRADES = 5
TUNER_STEP = 0.05          # было 0.15 — слишком агрессивно
TUNER_MAX_MULT = 1.30      # потолок множителя
TUNER_DECAY = 0.02         # притяжение к 1.0 каждый цикл (убирает односторонний дрейф)

def _decay_toward_one(x):
if x > 1.0:
return max(1.0, x - TUNER_DECAY)
elif x < 1.0:
return min(1.0, x + TUNER_DECAY)
return x

def weight_tuner():
global last_tuner_ts, tuner_last_changes
global tuner_rsi_long_mult, tuner_rsi_short_mult, tuner_bb_long_mult, tuner_bb_short_mult
now = time.time()
if now - last_tuner_ts < TUNER_INTERVAL_DAYS * 86400:
return
recent = [s for s in signals_history if “label” in s and s.get(“label”) is not None
and (now - s.get(“timestamp”, 0)) < 30 * 86400]
if len(recent) < TUNER_MIN_TRADES:
return

```
# Сначала затухание к 1.0 (если фактор перестал подтверждаться — откатывается)
tuner_rsi_long_mult  = _decay_toward_one(tuner_rsi_long_mult)
tuner_rsi_short_mult = _decay_toward_one(tuner_rsi_short_mult)
tuner_bb_long_mult   = _decay_toward_one(tuner_bb_long_mult)
tuner_bb_short_mult  = _decay_toward_one(tuner_bb_short_mult)

recent.sort(key=lambda x: x.get("pnl", 0), reverse=True)
top = recent[:max(3, len(recent) // 5)]
wins = [s for s in recent if s.get("label") == 1]
if not top or not wins:
    last_tuner_ts = now
    storage_save_async()
    return
top_rsi = sum(s.get("metrics", {}).get("rsi", 50) for s in top) / len(top)
top_bb = sum(s.get("metrics", {}).get("bb_pct", 0.5) for s in top) / len(top)
top_adx = sum(s.get("metrics", {}).get("adx", 20) for s in top) / len(top)
win_rsi = sum(s.get("metrics", {}).get("rsi", 50) for s in wins) / len(wins)
win_bb = sum(s.get("metrics", {}).get("bb_pct", 0.5) for s in wins) / len(wins)
changes = []
if top_rsi < 45 and win_rsi < 50:
    tuner_rsi_long_mult = min(TUNER_MAX_MULT, tuner_rsi_long_mult + TUNER_STEP)
    changes.append(f"RSI_лонг ×{tuner_rsi_long_mult:.2f}")
if top_rsi > 55 and win_rsi > 50:
    tuner_rsi_short_mult = min(TUNER_MAX_MULT, tuner_rsi_short_mult + TUNER_STEP)
    changes.append(f"RSI_шорт ×{tuner_rsi_short_mult:.2f}")
if top_bb < 0.3 and win_bb < 0.4:
    tuner_bb_long_mult = min(TUNER_MAX_MULT, tuner_bb_long_mult + TUNER_STEP)
    changes.append(f"BB_дно ×{tuner_bb_long_mult:.2f}")
if top_bb > 0.7 and win_bb > 0.6:
    tuner_bb_short_mult = min(TUNER_MAX_MULT, tuner_bb_short_mult + TUNER_STEP)
    changes.append(f"BB_потолок ×{tuner_bb_short_mult:.2f}")
if top_adx < 25:
    changes.append("Канал_приоритет")

last_tuner_ts = now
if changes:
    tuner_last_changes = ", ".join(changes)
    send_telegram(f"🔧 Tuner: {tuner_last_changes}")
    log.info(f"Tuner: {changes}")
else:
    tuner_last_changes = "без изменений (затухание к 1.0)"
    send_telegram("🔧 Tuner: без изменений")
storage_save_async()
```

# ========================================================

# АНТИ-ЧЕЙЗИНГ / КУЛДАУН

# ========================================================

def cooldown_check(direction, current_price, df):
if last_close_ts == 0:
return True, “”

```
now = time.time()
elapsed = now - last_close_ts

if last_close_result == "TP":
    if elapsed < COOLDOWN_AFTER_TP:
        remaining = (COOLDOWN_AFTER_TP - elapsed) / 60
        return False, f"Кулдаун после TP ({remaining:.1f} мин ост)"
elif last_close_result == "SL":
    if elapsed < COOLDOWN_AFTER_SL:
        remaining = (COOLDOWN_AFTER_SL - elapsed) / 60
        return False, f"Кулдаун после SL ({remaining:.1f} мин ост)"

if (last_close_direction == direction
    and elapsed < ANTI_CHASE_WINDOW
    and last_close_price > 0):
    if direction == "LONG":
        move_pct = (current_price - last_close_price) / last_close_price
        if move_pct > ANTI_CHASE_PCT:
            return False, f"Анти-чейз LONG: цена +{move_pct*100:.2f}% от exit"
    else:
        move_pct = (last_close_price - current_price) / last_close_price
        if move_pct > ANTI_CHASE_PCT:
            return False, f"Анти-чейз SHORT: цена -{move_pct*100:.2f}% от exit"

try:
    if df is None or len(df) == 0:
        return True, ""
    row = df.iloc[-1]
    rsi = row["RSI"]
    bb_pct = row["BB_pct"]
    if direction == "LONG":
        if rsi > RSI_OVERHEAT_LONG:
            return False, f"RSI перегрет ({rsi:.0f} > {RSI_OVERHEAT_LONG})"
        if bb_pct > BB_OVERHEAT_LONG:
            return False, f"BB перегрет ({bb_pct:.2f} > {BB_OVERHEAT_LONG})"
    else:
        if rsi < RSI_OVERHEAT_SHORT:
            return False, f"RSI перепродан ({rsi:.0f} < {RSI_OVERHEAT_SHORT})"
        if bb_pct < BB_OVERHEAT_SHORT:
            return False, f"BB перепродан ({bb_pct:.2f} < {BB_OVERHEAT_SHORT})"
except Exception:
    pass

if (last_close_result == "TP"
    and last_close_direction == direction
    and elapsed < ANTI_CHASE_WINDOW
    and last_close_price > 0):
    if direction == "LONG" and current_price > last_close_price:
        return False, "Ждём отката после TP LONG"
    if direction == "SHORT" and current_price < last_close_price:
        return False, "Ждём отката после TP SHORT"

return True, ""
```

# ========================================================

# UI

# ========================================================

def score_bar(score):
filled = min(10, max(0, round(score / MAX_SCORE * 10)))
bar = “█” * filled + “░” * (10 - filled)
emoji = “🟢” if score >= 8 else (“🟡” if score >= 6 else (“🟠” if score >= 4 else “🔴”))
return f”{emoji} [{bar}] {score:.1f}/{MAX_SCORE}”

# ========================================================

# СКАН

# ========================================================

def run_scan():
global last_scan_time, last_heartbeat_time, pause_until
global yesterday_high, yesterday_low, losses_in_row, last_daily_report_day

```
last_scan_time = time.time()
now = last_scan_time

if now < pause_until:
    log.info(f"⏸ Пауза {int((pause_until - now) / 60)} мин")
    return

check_closed_positions()

if now - last_heartbeat_time >= 3600 or yesterday_high == 0:
    yesterday_high, yesterday_low = get_yesterday_levels()

df = get_klines(SYMBOL_BN, TIMEFRAME, 200)
if df is None:
    send_telegram("❌ Ошибка получения свечей")
    return
df = calc(df)

funding         = get_funding()
ob, spread_pct  = get_orderbook()
btc_mom, btc_dir= get_btc_momentum()
price           = df.iloc[-1]["close"]
atr_val         = df.iloc[-1]["ATR"]

sig = get_signal(df, funding, ob, spread_pct, btc_mom, btc_dir)
direction, entry, sl, tp, score, reason, _L, _S, _ml_metrics = sig

log.info(f"ETH:{price:.2f} | {direction or 'нет'} {score:.1f} | {reason}")

if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
    last_heartbeat_time = now
    _send_heartbeat(price, atr_val, _L, _S, score, direction)

today = datetime.now(timezone.utc).day
if (datetime.now(timezone.utc).hour == DAILY_REPORT_HOUR
    and today != last_daily_report_day):
    last_daily_report_day = today
    _send_daily_report()

if direction is None:
    return

cd_ok, cd_reason = cooldown_check(direction, price, df)
if not cd_ok:
    log.info(f"⏸ Вход заблокирован: {cd_reason}")
    send_telegram(
        f"⏸ <b>Вход заблокирован</b>\n"
        f"Сигнал: {direction} score={score:.1f}\n"
        f"Причина: {cd_reason}"
    )
    return

msg = [
    f"<b>[{'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}] v6.4 {'LIVE' if LIVE else 'DEMO'}</b>",
    f"{'🟢' if direction == 'LONG' else '🔴'} <b>SCALP {direction}</b>",
    "",
    f"<b>Надёжность:</b> {score_bar(score)}",
    "",
    f"💰 Вход: <b>{entry:.2f}</b>",
    f"🛑 Стоп: {sl:.2f} ({abs(entry-sl)/entry*100:.2f}%)",
    f"🎯 Тейк: {tp:.2f} ({abs(tp-entry)/entry*100:.2f}%)",
    f"⚖ RR: {abs(tp-entry)/max(abs(sl-entry),0.01):.1f}",
    f"⚙️ MIN:{MIN_SCORE} | {reason}",
]

has_pos = len(okx_get_positions()) > 0

if not OKX_API_KEY:
    msg.append("⚠️ OKX_API_KEY не задан")
elif has_pos:
    msg.append("⚠️ Позиция уже открыта")
else:
    res = okx_place_order(direction, entry, sl, tp, atr_val)
    if res["ok"]:
        losses_in_row = 0
        msg += [
            "✅ <b>ИСПОЛНЕНО</b>",
            f"Контрактов: {res['total_qty']}",
            f"SL:{'✅' if res['sl_ok'] else '❌'} TP:{'✅' if res['tp_ok'] else '❌'}",
        ]
        _save_signal_to_history({
            "order_id":  res["orderId"],
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "score":     score,
            "L":         _L,
            "S":         _S,
            "metrics":   _ml_metrics,
            "timestamp": time.time(),
            "result":    None,
            "label":     None,
        })
    else:
        # ФИКС v6.4: сбой РАЗМЕЩЕНИЯ ордера — это НЕ торговый убыток,
        # losses_in_row НЕ трогаем и паузу по убыткам НЕ запускаем.
        msg.append(f"❌ {res.get('step', '')}: {res.get('msg', '')}")

msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
send_telegram("\n".join(msg))
```

def _send_heartbeat(price, atr_val, L, S, score, direction):
bal = okx_get_balance()
pos = okx_get_positions()
hour = datetime.now(timezone.utc).hour
session = “🌙” if (hour >= 22 or hour < 6) else (“☀️” if hour < 13 else “🌆”)
fg = get_fear_greed()
ls = get_long_short_ratio()
p1h, b1h = get_1h_trend()
winrate = (stats[“wins”] / stats[“total”] * 100) if stats[“total”] > 0 else 0
pf = (stats[“total_profit_sum”] / stats[“total_loss_sum”]) if stats[“total_loss_sum”] > 0 else 0

```
sig_status = (
    f"{direction} <b>{score:.1f}</b>" if direction else
    f"НЕТ <b>{max(L, S):.1f}</b>"
)

ml_info = ""
if stats["ml_trains_count"] > 0:
    last_train = datetime.fromtimestamp(stats["ml_last_train_ts"]).strftime("%d.%m %H:%M")
    ml_info = (f"\n🧠 ML: {stats['ml_trains_count']} обуч | "
               f"acc:{stats['ml_last_accuracy']:.1%} | "
               f"{stats['ml_samples_at_last_train']} примеров | {last_train}")

send_telegram(
    f"<b>❤️ Heartbeat v6.4 [{'LIVE' if LIVE else 'DEMO'}]</b>\n\n"
    f"💰 ETH: {price:.2f} | ATR: {atr_val:.2f}\n"
    f"😨 F&G:{fg} L/S:{ls:.2f}\n"
    f"📈 1h: {'🟢 Бычий' if b1h else '🔴 Медвежий'} ({p1h:+.2f}%)\n"
    f"📐 Режим: {current_mode}\n"
    f"{session} Баланс: {bal:.2f} USDT | Поз:{len(pos)}\n"
    f"🎯 {sig_status} L:{L:.1f} S:{S:.1f}\n"
    f"⚙️ MIN:{MIN_SCORE} База:{BASE_MIN_SCORE}\n"
    f"📊 {stats['total']} сд ✅{stats['wins']} 🔴{stats['losses']} ({winrate:.1f}%) "
    f"PF:{pf:.2f}\n"
    f"💵 P&L: {stats['total_profit']:+.2f} USDT\n"
    f"🔧 Tuner: {tuner_last_changes}\n"
    f"📊 Analyzer: {analyzer_status}"
    f"{ml_info}\n"
    f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
)
```

def _send_daily_report():
winrate = (stats[“wins”] / stats[“total”] * 100) if stats[“total”] > 0 else 0
pf = (stats[“total_profit_sum”] / stats[“total_loss_sum”]) if stats[“total_loss_sum”] > 0 else 0
avg = stats[“total_profit”] / stats[“total”] if stats[“total”] > 0 else 0
bal = okx_get_balance()

```
send_telegram(
    f"📅 <b>ЕЖЕДНЕВНЫЙ ОТЧЁТ</b>\n\n"
    f"📊 <b>Сделки:</b>\n"
    f"  Всего: {stats['total']}\n"
    f"  ✅ {stats['wins']} | 🔴 {stats['losses']}\n"
    f"  Винрейт: {winrate:.1f}%\n\n"
    f"💵 <b>Финансы:</b>\n"
    f"  P&L: {stats['total_profit']:+.2f} USDT\n"
    f"  Средняя: {avg:+.2f}\n"
    f"  Лучшая: {stats['best_trade']:+.2f}\n"
    f"  Худшая: {stats['worst_trade']:+.2f}\n"
    f"  Profit Factor: {pf:.2f}\n"
    f"  Просадка: {stats['max_drawdown']:.2f}\n"
    f"  Баланс: {bal:.2f} USDT\n\n"
    f"🧠 <b>Обучение:</b>\n"
    f"  ML: обучений {stats['ml_trains_count']} | acc {stats['ml_last_accuracy']:.1%} | до след. {ML_RETRAIN_EVERY - ml_train_counter}\n"
    f"  🔧 Tuner: {tuner_last_changes}\n"
    f"  📊 Analyzer: {analyzer_status}"
)
```

# ========================================================

# WATCHDOG

# ========================================================

def watchdog_loop():
last_alert = 0
while True:
time.sleep(60)
now = time.time()
if _pending_save_queue:
jsonbin_flush_pending()
if last_scan_time > 0 and now - last_scan_time > WATCHDOG_THRESHOLD:
if now - last_alert > 600:
last_alert = now
send_telegram(
f”🚨 <b>WATCHDOG</b>\n”
f”Бот не сканирует {int((now - last_scan_time) / 60)} мин!”
)

# ========================================================

# FLASK

# ========================================================

@app.route(”/”)
def home():
return “OK”, 200

@app.route(”/health”)
def health():
age = time.time() - last_scan_time if last_scan_time > 0 else 99999
if age > WATCHDOG_THRESHOLD:
return f”STALE (last scan {age:.0f}s ago)”, 503
return f”ALIVE (last scan {age:.0f}s ago)”, 200

@app.route(”/stats”)
def stats_endpoint():
return json.dumps(stats, indent=2), 200, {“Content-Type”: “application/json”}

@app.route(’/backtest’)
def backtest_endpoint():
threading.Thread(target=run_backtest, args=(BACKTEST_DAYS_INITIAL,), daemon=True).start()
return “Backtest started”, 200

# ========================================================

# MAIN LOOP

# ========================================================

def bot_loop():
global backtest_done_initial
log.info(“🚀 OKX Scalp Bot v6.4 starting”)

```
storage_load_all()

labeled = [s for s in signals_history if s.get("label") is not None]
unlabeled = [s for s in signals_history if s.get("label") is None]
ml_status_msg = ""

log.info(f"ML диагностика: всего={len(signals_history)}, "
         f"с метками={len(labeled)}, без меток={len(unlabeled)}, "
         f"счётчик={ml_train_counter}/{ML_RETRAIN_EVERY}")

if len(labeled) >= ML_MIN_SAMPLES and stats.get("ml_trains_count", 0) == 0:
    log.info("Тренировка ML на старте...")
    if _train_model():
        ml_status_msg = (
            f"🧠 ML обучена на {len(labeled)} сделках\n"
            f"До следующего обучения: {ML_RETRAIN_EVERY - ml_train_counter} сделок"
        )
    else:
        ml_status_msg = "⚠️ ML обучение провалилось (см логи)"
else:
    ml_status_msg = (
        f"📊 ML: данных мало ({len(labeled)}/{ML_MIN_SAMPLES})\n"
        f"Жду ещё {max(0, ML_MIN_SAMPLES - len(labeled))} закрытых сделок до первого обучения"
    )

try:
    existing = okx_get_positions()
    actual_sides = {p.get("posSide") for p in existing if abs(float(p.get("pos", 0))) > 0}
    for oid in list(active_positions.keys()):
        if active_positions[oid].get("pos_side") not in actual_sides:
            log.info(f"Удаляю стейл: {oid}")
            del active_positions[oid]
    for p in existing:
        ps = p.get("posSide")
        if not any(ap.get("pos_side") == ps for ap in active_positions.values()):
            side = "LONG" if ps == "long" else "SHORT"
            active_positions[f"rec_{int(time.time())}_{ps}"] = {
                "direction": side,
                "entry":     float(p.get("avgPx", 0)),
                "sl":        0,
                "tp":        0,
                "total_qty": abs(int(float(p.get("pos", 0)))),
                "pos_side":  ps,
                "cls_side":  "sell" if side == "LONG" else "buy",
                "open_time": time.time() - 3600,
                "sl_algo_id": None,
                "tp_algo_id": None,
                "tp1_algo_id": None,
            }
    storage_save_async()
except Exception as e:
    log.error(f"recover positions: {e}")

bal = okx_get_balance()
if stats["peak_equity"] == 0:
    stats["current_equity"] = bal
    stats["peak_equity"] = bal

winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
rr = TP_ATR_MULT / SL_ATR_MULT
tr_hours = "круглосуточно" if TRADING_HOURS is None else f"{TRADING_HOURS[0]}-{TRADING_HOURS[1]} UTC"

send_telegram(
    f"🚀 <b>OKX Bot v6.4 [{TIMEFRAME}] {'⚔️LIVE' if LIVE else '🧪DEMO'}</b>\n\n"
    f"💼 OKX | {SYMBOL}\n"
    f"📈 Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
    f"⏱ Таймфрейм: <b>{TIMEFRAME}</b>\n"
    f"🎯 SL: {SL_ATR_MULT}xATR | TP: {TP_ATR_MULT}xATR (RR={rr:.2f})\n"
    f"⚙️ MIN: {BASE_MIN_SCORE}/{MAX_SCORE} diff>={MIN_SCORE_DIFF}\n"
    f"📐 Режим-гистерезис ADX: {REGIME_LOW}/{REGIME_HIGH}\n"
    f"📐 ADX_min={ADX_MIN} | EMA21_dist={EMA21_MAX_DIST_ATR}xATR\n"
    f"⏰ Окно: {tr_hours}\n"
    f"🕐 Time-stop: {MAX_HOLD_HOURS}ч\n"
    f"💰 Баланс: {bal:.2f} USDT\n"
    f"📊 История: {stats['total']} сд | ✅ {stats['wins']} ({winrate:.1f}%)\n"
    f"📜 Сигналов в базе: {len(signals_history)}\n\n"
    f"{ml_status_msg}\n\n"
    f"<b>Изменения v6.4:</b>\n"
    f"* Тройное касание: 1 сторона\n"
    f"* TP1 от настоящего ATR\n"
    f"* Единый режим (гистерезис {REGIME_LOW}/{REGIME_HIGH})\n"
    f"* Tuner +0.05 + откат к 1.0\n"
    f"* Бэктест без race на кэше\n"
    f"* 1h-подтверждение без рекурсии\n"
    f"* losses_in_row только на реальном убытке"
)

while True:
    try:
        run_scan()
        try:
            if time.time() - last_tuner_ts >= TUNER_INTERVAL_DAYS * 86400:
                threading.Thread(target=weight_tuner, daemon=True).start()
        except Exception as e:
            log.error(f"Tuner error: {e}")
        try:
            if time.time() - last_analyzer_ts >= 7 * 86400:
                threading.Thread(target=backtest_analyzer, daemon=True).start()
        except Exception as e:
            log.error(f"Analyzer error: {e}")
        dow = datetime.now(timezone.utc).weekday()
        hour = datetime.now(timezone.utc).hour
        if dow == BACKTEST_AUTO_DAY and hour == BACKTEST_AUTO_HOUR:
            if stats["total"] >= 50:
                if not backtest_done_initial:
                    threading.Thread(target=run_backtest, args=(BACKTEST_DAYS_INITIAL,), daemon=True).start()
                    backtest_done_initial = True
                elif datetime.now(timezone.utc).minute < 10:
                    threading.Thread(target=run_backtest, args=(BACKTEST_DAYS_WEEKLY,), daemon=True).start()
    except Exception as e:
        log.error(f"run_scan: {e}")
        send_telegram(f"⚠️ Ошибка скана: {e}")
    time.sleep(SCAN_INTERVAL)
```

if **name** == “**main**”:
threading.Thread(target=bot_loop, daemon=True).start()
threading.Thread(target=watchdog_loop, daemon=True).start()
port = int(os.environ.get(“PORT”, 5000))
app.run(host=“0.0.0.0”, port=port)