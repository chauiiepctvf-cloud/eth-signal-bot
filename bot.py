# “””
OKX Scalp Bot v6.0 (ETH-USDT-SWAP, 5m)
 
Главные изменения vs v5.1:
 
ХРАНИЛИЩЕ (исправление потери данных при деплое):
• JSONBin.io как основное хранилище — переживает редеплой
• Локальные файлы как backup (не основной источник)
• Авто-миграция локальных файлов в JSONBin при старте
 
ТОРГОВЛЯ (по запросу: без трейлинга / без переноса / без частичного):
• Плечо снижено x50 → x20 (безопаснее для скальпа)
• SL = 1.5 × ATR (динамический, не фиксированный %)
• TP = 3.0 × ATR (RR = 2.0 — нужен WR ≥ 33% для безубытка)
• Один TP, один SL — больше ничего
 
ИСПРАВЛЕННЫЕ БАГИ:
• check_closed_positions: PnL теперь привязан к конкретному order_id
• Старые алго-ордера отменяются при закрытии (раньше висели)
• VWAP с дневным reset (раньше копился непрерывно)
• detect_whales кэшируется 60с (раньше обучал IsolationForest каждый скан)
• ml_train_counter сбрасывается ТОЛЬКО при успешном обучении
• Telegram теперь в отдельных потоках — не блокирует основной цикл
• Watchdog: алерт если скан застрял >10 мин
 
ML УЛУЧШЕНИЯ:
• sample_weight с экспоненциальным затуханием (старые сделки меньше влияют)
• Переобучение каждые 10 закрытий (было 20)
• Feature importance в Telegram после каждого обучения
• Cross-validation accuracy в логах
• Модель не сохраняется на диск — обучается заново на старте из истории
 
МЕТРИКИ И БАЛЛЫ:
• Веса CVD и vol_dir снижены до 0.75 (было 1.0+0.5 — частичное дублирование)
• Спред-фильтр (>0.05% — пропуск)
• Расширенная статистика: best/worst trade, profit factor, equity
• Ежедневный отчёт в 23:00 UTC
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
 
# ════════════════════════════════════════════════════════
 
# КОНФИГ
 
# ════════════════════════════════════════════════════════
 
TELEGRAM_TOKEN  = os.environ.get(“TELEGRAM_TOKEN”)
CHAT_ID         = os.environ.get(“CHAT_ID”)
OKX_API_KEY     = os.environ.get(“OKX_API_KEY”)
OKX_SECRET      = os.environ.get(“OKX_SECRET”)
OKX_PASSPHRASE  = os.environ.get(“OKX_PASSPHRASE”)
TWELVE_API_KEY  = os.environ.get(“TWELVE_API_KEY”, “”)
JSONBIN_KEY     = os.environ.get(“JSONBIN_API_KEY”)
JSONBIN_BIN_ID  = os.environ.get(“JSONBIN_BIN_ID”)
 
OKX_BASE        = “https://www.okx.com”
OKX_DEMO_HEADER = {“x-simulated-trading”: “1”}  # убери для боевого
 
SYMBOL          = “ETH-USDT-SWAP”
SYMBOL_BN       = “ETHUSDT”
BTC_SYMBOL      = “BTCUSDT”
 
# Торговля (статические TP/SL, без трейлинга/перемещения)
 
LEVERAGE        = 20            # снижено с 50
ORDER_USDT      = 20
SL_ATR_MULT     = 1.5           # SL = 1.5 × ATR
TP_ATR_MULT     = 3.0           # TP = 3.0 × ATR (RR=2.0)
SL_PCT_MIN      = 0.0025        # минимум 0.25% от цены
SL_PCT_MAX      = 0.012         # максимум 1.2% от цены
TP_PCT_MAX      = 0.025         # максимум 2.5% от цены
 
SCAN_INTERVAL   = 3 * 60
HEARTBEAT_INTERVAL = 60 * 60
WATCHDOG_THRESHOLD = 10 * 60
DAILY_REPORT_HOUR  = 23         # UTC
 
# Балльная система
 
BASE_MIN_SCORE  = 6.0
MIN_SCORE       = 6.0
MIN_SCORE_DIFF  = 2.5
MAX_SCORE       = 13.0
 
# Фильтры
 
ATR_MIN_PCT     = 0.0003
SPREAD_MAX_PCT  = 0.0005        # 0.05% макс
NIGHT_HOURS     = (22, 6)
RED_NEWS_DROP   = -0.01
RED_NEWS_VOL    = 1.5
RED_NEWS_BLOCK  = 1800
MAX_LOSSES      = 3
PAUSE_LOSSES    = 1800
ETH_BTC_CORR_MIN= 0.3
 
# ML
 
ML_RETRAIN_EVERY= 10            # было 20
ML_MIN_SAMPLES  = 30
ML_DECAY_DAYS   = 7
 
FORCE_TEST      = False
 
# ════════════════════════════════════════════════════════
 
# ЛОГИРОВАНИЕ
 
# ════════════════════════════════════════════════════════
 
logging.basicConfig(level=logging.INFO, format=’%(asctime)s [%(levelname)s] %(message)s’)
log = logging.getLogger(**name**)
 
app = Flask(**name**)
 
# ════════════════════════════════════════════════════════
 
# JSONBIN STORAGE
 
# ════════════════════════════════════════════════════════
 
JSONBIN_URL = f”https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}” if JSONBIN_BIN_ID else None
_save_lock = threading.Lock()
 
def jsonbin_load():
“”“Читает весь record из JSONBin. Возвращает dict или {}.”””
if not JSONBIN_KEY or not JSONBIN_URL:
log.warning(“⚠️ JSONBin не настроен — данные потеряются при деплое”)
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
 
def jsonbin_save(data: dict) -> bool:
“”“Сохраняет record в JSONBin.”””
if not JSONBIN_KEY or not JSONBIN_URL:
return False
try:
r = requests.put(
JSONBIN_URL,
headers={
“X-Master-Key”: JSONBIN_KEY,
“Content-Type”: “application/json”,
},
json=data,
timeout=15,
)
if r.status_code == 200:
return True
log.error(f”JSONBin PUT {r.status_code}: {r.text[:200]}”)
except Exception as e:
log.error(f”JSONBin PUT error: {e}”)
return False
 
# ════════════════════════════════════════════════════════
 
# СОСТОЯНИЕ
 
# ════════════════════════════════════════════════════════
 
stats = {
“total”: 0, “wins”: 0, “losses”: 0,
“total_profit”: 0.0,
“total_profit_sum”: 0.0, “total_loss_sum”: 0.0,
“max_drawdown”: 0.0, “peak_equity”: 0.0, “current_equity”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0,
“ml_trains_count”: 0, “ml_last_train_ts”: 0.0,
“ml_last_accuracy”: 0.0, “ml_samples_at_last_train”: 0,
}
 
active_positions = {}    # order_id → meta
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
force_test_done  = False
scalp_model      = None
ml_train_counter = 0
last_vwap_reset_day = -1
 
# Кэш внешних метрик
 
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
}
 
# ════════════════════════════════════════════════════════
 
# ХРАНИЛИЩЕ: ЗАГРУЗКА И СОХРАНЕНИЕ
 
# ════════════════════════════════════════════════════════
 
def storage_load_all():
“”“Грузит данные. Приоритет: JSONBin > локальные файлы (миграция).”””
global stats, active_positions, signals_history, MIN_SCORE
 
```
data = jsonbin_load()
 
# Если JSONBin пуст — пробуем локальные файлы (одноразовая миграция)
if not data:
   log.info("JSONBin пустой — пытаюсь мигрировать локальные файлы")
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
 
# Применяем
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
 
log.info(
   f"Загружено: signals={len(signals_history)} "
   f"trades={stats['total']} "
   f"active={len(active_positions)} "
   f"min_score={MIN_SCORE}"
)
```
 
def storage_save_all():
“”“Сохраняет всё в JSONBin + локальный бэкап.”””
with _save_lock:
data = {
“signals_history”:  signals_history[-500:],  # последние 500
“stats”:            stats,
“active_positions”: active_positions,
“min_score”:        MIN_SCORE,
“last_updated”:     datetime.now(timezone.utc).isoformat(),
}
ok = jsonbin_save(data)
# Локальный бэкап (если контейнер выживет)
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
 
# ════════════════════════════════════════════════════════
 
# TELEGRAM (асинхронно)
 
# ════════════════════════════════════════════════════════
 
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
 
# ════════════════════════════════════════════════════════
 
# OKX API
 
# ════════════════════════════════════════════════════════
 
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
 
def okx_place_order(direction, entry, sl, tp):
“”“Открывает позицию + ставит SL и TP. Возвращает meta.”””
okx_set_leverage()
 
```
side     = "buy"  if direction == "LONG"  else "sell"
pos_side = "long" if direction == "LONG"  else "short"
cls_side = "sell" if direction == "LONG"  else "buy"
 
hour = datetime.now(timezone.utc).hour
in_night = NIGHT_HOURS[0] <= hour or hour < NIGHT_HOURS[1]
multiplier = 0.5 if in_night else 1.0
effective = ORDER_USDT * multiplier
# 1 контракт ETH-USDT-SWAP = 0.01 ETH
total_qty = max(1, round(effective * LEVERAGE / entry / 0.01))
 
log.info(f"{direction} qty={total_qty} entry={entry:.2f} sl={sl:.2f} tp={tp:.2f}")
 
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
 
# TP
tp_algo_id = None
for _ in range(3):
   tp_r = okx_post("/api/v5/trade/order-algo", {
       "instId":   SYMBOL,
       "tdMode":   "cross",
       "side":     cls_side,
       "posSide":  pos_side,
       "ordType":  "conditional",
       "sz":       str(total_qty),
       "tpTriggerPx":     str(round(tp, 2)),
       "tpOrdPx":         "-1",
       "tpTriggerPxType": "last",
   })
   if tp_r.get("code") == "0":
       tp_algo_id = tp_r["data"][0].get("algoId", "")
       break
   time.sleep(1)
 
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
   "sl_ok":      sl_algo_id is not None,
   "tp_ok":      tp_algo_id is not None,
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
 
# ════════════════════════════════════════════════════════
 
# ПРОВЕРКА ЗАКРЫТЫХ ПОЗИЦИЙ — фикс багов из v5.1
 
# ════════════════════════════════════════════════════════
 
def check_closed_positions():
“”“Проверяет какие из активных позиций закрылись.
Привязка PnL к конкретному order_id (баг v5.1 исправлен).
Отменяет оставшийся алго-ордер при закрытии.”””
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
 
   for order_id in list(active_positions.keys()):
       pos = active_positions.get(order_id)
       if not pos:
           continue
 
       # Позиция всё ещё открыта?
       if pos.get("pos_side") in open_sides:
           continue
 
       # Закрылась — обрабатываем
       _handle_position_close(order_id, pos)
 
except Exception as e:
   log.error(f"check_closed_positions: {e}")
```
 
def _handle_position_close(order_id, pos):
“”“Обрабатывает закрытие позиции по конкретному order_id.”””
global losses_in_row, MIN_SCORE
 
```
direction = pos["direction"]
entry     = pos["entry"]
cls_side  = pos["cls_side"]
pos_side  = pos["pos_side"]
qty       = pos.get("total_qty", 0)
open_ms   = int(pos["open_time"] * 1000)
 
# Запрашиваем fills с момента открытия
r = okx_get_fills_history(open_ms - 1000)
fills = r.get("data", [])
 
# Только закрывающие fills для нашей стороны
close_fills = [
   f for f in fills
   if f.get("side") == cls_side
   and f.get("posSide") == pos_side
   and float(f.get("ts", 0)) >= open_ms - 1000
]
 
if not close_fills:
   # Возможно ещё не зафиксировались — отложим
   log.info(f"Позиция {order_id} закрыта но fills не найдены — жду")
   return
 
# Взвешенная средняя цена закрытия
total_close_qty = sum(float(f["fillSz"]) for f in close_fills)
if total_close_qty <= 0:
   return
avg_close = sum(float(f["fillPx"]) * float(f["fillSz"]) for f in close_fills) / total_close_qty
 
# PnL в USDT (1 контракт = 0.01 ETH)
if direction == "LONG":
   pnl_usdt = (avg_close - entry) * total_close_qty * 0.01
else:
   pnl_usdt = (entry - avg_close) * total_close_qty * 0.01
 
# Причина закрытия
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
 
# Отменяем оставшиеся алго (фикс бага v5.1: висели после закрытия)
okx_cancel_algo(pos.get("sl_algo_id"))
okx_cancel_algo(pos.get("tp_algo_id"))
 
# Обновляем статистику
is_win = pnl_usdt > 0
_update_stats(pnl_usdt, is_win)
_update_signal_result(order_id, "TP" if is_win else "SL", pnl_usdt)
 
# Адаптация MIN_SCORE
global losses_in_row, MIN_SCORE
if is_win:
   losses_in_row = 0
   if MIN_SCORE != BASE_MIN_SCORE:
       MIN_SCORE = BASE_MIN_SCORE
else:
   losses_in_row += 1
   if losses_in_row >= 3 and MIN_SCORE < BASE_MIN_SCORE + 1.0:
       MIN_SCORE += 0.5
       send_telegram(f"⚠️ 3 убытка подряд → MIN_SCORE = {MIN_SCORE}")
 
# Уведомление
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
   f"MIN: {MIN_SCORE}"
)
 
# Удаляем
del active_positions[order_id]
storage_save_async()
 
# Триггер ML переобучения
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
 
# Best/worst
if pnl > stats["best_trade"]:
   stats["best_trade"] = pnl
if pnl < stats["worst_trade"]:
   stats["worst_trade"] = pnl
 
# Equity & drawdown
stats["current_equity"] += pnl
if stats["current_equity"] > stats["peak_equity"]:
   stats["peak_equity"] = stats["current_equity"]
dd = stats["peak_equity"] - stats["current_equity"]
if dd > stats["max_drawdown"]:
   stats["max_drawdown"] = dd
```
 
# ════════════════════════════════════════════════════════
 
# ДАННЫЕ С БИРЖ
 
# ════════════════════════════════════════════════════════
 
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
“”“Возвращает (imbalance_pct, spread_pct).”””
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
 
# ── Внешние метрики (с кэшем) ──────────────────────────
 
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
“”“Кэшируется на 60с (фикс v5.1: запускалось каждый скан).”””
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
try:
e = get_klines(“ETHUSDT”, “5m”, 30)
b = get_klines(“BTCUSDT”, “5m”, 30)
if e is None or b is None:
return 1.0
corr = e[“close”].pct_change().dropna().corr(b[“close”].pct_change().dropna())
return corr if not np.isnan(corr) else 1.0
except Exception:
return 1.0
 
# ════════════════════════════════════════════════════════
 
# ИНДИКАТОРЫ (с дневным VWAP reset — фикс v5.1)
 
# ════════════════════════════════════════════════════════
 
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
 
# VWAP с дневным reset (фикс v5.1: копился непрерывно)
df["day"] = df["candle_time"].dt.day
df["vp"]  = df["close"] * df["volume"]
df["VWAP"] = (
   df.groupby("day")["vp"].cumsum() /
   df.groupby("day")["volume"].cumsum().replace(0, np.nan)
)
 
# CVD накопительно
df["CVD_raw"]   = df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])
df["CVD"]       = df["CVD_raw"].rolling(20).sum()
df["CVD_up"]    = df["CVD"] > df["CVD"].shift(3)
df["CVD_accel"] = df["CVD"] - df["CVD"].shift(6)
 
# Объём
df["vol_ma"]      = df["volume"].rolling(20).mean()
df["vol_spike"]   = df["volume"] > df["vol_ma"] * 1.3
df["vol_extreme"] = df["volume"] > df["vol_ma"] * 3.0
df["price_dir"]   = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
df["vol_dir"]     = df["vol_spike"].astype(int) * df["price_dir"]
 
# ADX
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
 
# OBV
df["OBV"]    = (df["volume"] * np.sign(df["close"].diff())).cumsum()
df["OBV_ma"] = df["OBV"].rolling(20).mean()
 
# Свечные паттерны
body = (df["close"] - df["open"]).abs()
lw = df[["open", "close"]].min(axis=1) - df["low"]
uw = df["high"] - df[["open", "close"]].max(axis=1)
df["hammer"]  = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
 
# RSI дивергенции
df["rsi_div_bull"] = (df["close"] < df["close"].shift(5)) & (df["RSI"] > df["RSI"].shift(5)) & (df["RSI"] < 50)
df["rsi_div_bear"] = (df["close"] > df["close"].shift(5)) & (df["RSI"] < df["RSI"].shift(5)) & (df["RSI"] > 50)
 
return df
```
 
# ════════════════════════════════════════════════════════
 
# СИГНАЛ
 
# ════════════════════════════════════════════════════════
 
def get_signal(df, funding, ob, spread_pct, btc_mom, btc_dir):
global ob_history, last_ob, yesterday_high, yesterday_low
global force_test_done, red_news_until
 
```
if df is None or len(df) < 50:
   return None, None, None, None, 0, "Нет данных", 0, 0, {}
 
row   = df.iloc[-1]
price = row["close"]
rsi   = row["RSI"]
atr   = row["ATR"]
adx   = row["ADX"] if not np.isnan(row.get("ADX", float("nan"))) else 20
 
# Жёсткие фильтры
if atr < price * ATR_MIN_PCT:
   return None, None, None, None, 0, "Рынок мёртв (ATR)", 0, 0, {}
 
if spread_pct > SPREAD_MAX_PCT:
   return None, None, None, None, 0, f"Спред {spread_pct*100:.3f}%", 0, 0, {}
 
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
 
# FORCE TEST
if FORCE_TEST and not force_test_done:
   force_test_done = True
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
 
# OBV дивергенция (исправленная логика)
obv_div = 0
if not np.isnan(row.get("OBV", float("nan"))) and not np.isnan(row.get("OBV_ma", float("nan"))):
   if price > df.iloc[-5]["close"] and row["OBV"] < row["OBV_ma"]:
       obv_div = -1
   elif price < df.iloc[-5]["close"] and row["OBV"] > row["OBV_ma"]:
       obv_div = 1
 
L = S = 0.0
 
# ═════ ЯДРО ═════════════════════════════════════════
# 1. EMA 5m
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
 
# 4. RSI
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
 
# 6. CVD (вес снижен с 1.0 до 0.75 — частично дублирует vol_dir)
if row["CVD_up"]: L += 0.75
else:             S += 0.75
 
if not np.isnan(row.get("CVD_accel", float("nan"))):
   if row["CVD_accel"] > 0: L += 0.25
   elif row["CVD_accel"] < 0: S += 0.25
 
# 7. Объём (вес снижен с 1.0 до 0.75)
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
 
# ADX
if adx < 20:
   L = max(0, L - 0.5)
   S = max(0, S - 0.5)
if adx > 25:
   if not np.isnan(row.get("+DI", float("nan"))):
       if row["+DI"] > row["-DI"]: L += 0.25
       else:                       S += 0.25
 
# ═════ СРЕДНИЕ ═════════════════════════════════════
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
 
bp = row["BB_pct"]
if   bp < 0.1: L += 0.5
elif bp < 0.2: L += 0.25
elif bp > 0.9: S += 0.5
elif bp > 0.8: S += 0.25
 
if price < row["VWAP"]: L += 0.5
else:                   S += 0.5
 
# ═════ ВСПОМОГАТЕЛЬНЫЕ ═════════════════════════════
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
 
# ═════ ШТРАФЫ ═══════════════════════════════════════
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
   if   price < yesterday_low:                  L = max(0, L - 0.5)
   elif price > yesterday_high:                 S = max(0, S - 0.5)
   elif yesterday_low < price < yesterday_low * 1.01:    L += 0.25
   elif yesterday_high * 0.99 < price < yesterday_high:  S += 0.25
 
# Конфлюэнция (4 из 6)
ema_l = row["EMA9"] > row["EMA21"] > row["EMA50"]
ema_s = row["EMA9"] < row["EMA21"] < row["EMA50"]
macd_l = row["MACD"] > row["MACD_sig"]
macd_s = row["MACD"] < row["MACD_sig"]
cvd_l = row["CVD_up"]
vol_l = row["vol_dir"] > 0
vol_s = row["vol_dir"] < 0
 
core_long  = sum([ema_l, ema_4h_bull,    rsi < 45, macd_l, cvd_l,       vol_l])
core_short = sum([ema_s, not ema_4h_bull, rsi > 55, macd_s, not cvd_l, vol_s])
 
if core_long  >= 4: L += 0.75
if core_short >= 4: S += 0.75
 
# ═════ ML БОНУС ═════════════════════════════════════
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
   f"L:{L} S:{S} D:{L-S:+.1f} | "
   f"ADX:{adx:.0f} CVD:{'↑' if row['CVD_up'] else '↓'} "
   f"RSI:{rsi:.0f} 1h:{p_vs_1h:+.2f}% sp:{spread_pct*100:.3f}% | ML:{ml_b:+.2f}"
)
 
# ВЫХОД С ATR-BASED SL/TP
if L - S >= MIN_SCORE_DIFF and L >= MIN_SCORE:
   sl_dist = max(price * SL_PCT_MIN, min(atr * SL_ATR_MULT, price * SL_PCT_MAX))
   tp_dist = min(atr * TP_ATR_MULT, price * TP_PCT_MAX)
   entry = price
   sl    = round(entry - sl_dist, 2)
   tp    = round(entry + tp_dist, 2)
   return "LONG", entry, sl, tp, L, reason_str, L, S, ml_metrics
 
if S - L >= MIN_SCORE_DIFF and S >= MIN_SCORE:
   sl_dist = max(price * SL_PCT_MIN, min(atr * SL_ATR_MULT, price * SL_PCT_MAX))
   tp_dist = min(atr * TP_ATR_MULT, price * TP_PCT_MAX)
   entry = price
   sl    = round(entry + sl_dist, 2)
   tp    = round(entry - tp_dist, 2)
   return "SHORT", entry, sl, tp, S, reason_str, L, S, ml_metrics
 
return None, None, None, None, max(L, S), reason_str, L, S, ml_metrics
```
 
# ════════════════════════════════════════════════════════
 
# ML — с весами по времени, feature importance
 
# ════════════════════════════════════════════════════════
 
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
“”“Обучает модель на истории. С весами по времени.”””
global scalp_model
 
```
done = [s for s in signals_history if "label" in s and s.get("label") is not None]
if len(done) < ML_MIN_SAMPLES:
   log.info(f"ML: мало данных ({len(done)}/{ML_MIN_SAMPLES})")
   return False
 
try:
   from sklearn.ensemble import GradientBoostingClassifier
   from sklearn.preprocessing import StandardScaler
   from sklearn.model_selection import cross_val_score
 
   X = np.array([
       _ml_features(s.get("L", 0), s.get("S", 0), s.get("metrics", {}))
       for s in done
   ])
   y = np.array([s["label"] for s in done])
 
   # Веса по времени (старые сделки меньше влияют)
   now = time.time()
   ts_arr = np.array([s.get("timestamp", now) for s in done])
   ages_days = (now - ts_arr) / 86400
   weights = np.exp(-ages_days / ML_DECAY_DAYS)
 
   sc = StandardScaler()
   Xs = sc.fit_transform(X)
 
   m = GradientBoostingClassifier(
       n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42
   )
 
   # CV для оценки качества
   cv_acc = 0.0
   try:
       if len(set(y)) >= 2 and len(y) >= 10:
           cv_acc = float(cross_val_score(m, Xs, y, cv=min(5, len(y) // 5),
                                          scoring="accuracy").mean())
   except Exception as e:
       log.error(f"CV: {e}")
 
   m.fit(Xs, y, sample_weight=weights)
 
   scalp_model = {"model": m, "scaler": sc}
 
   # Feature importance топ-5
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
   return True
except Exception as e:
   log.error(f"train_model: {e}")
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
“”“Триггер переобучения. Счётчик сбрасывается ТОЛЬКО при успехе.”””
global ml_train_counter
ml_train_counter += 1
if ml_train_counter >= ML_RETRAIN_EVERY:
def _do():
global ml_train_counter
if _train_model():
ml_train_counter = 0
threading.Thread(target=_do, daemon=True).start()
 
# ════════════════════════════════════════════════════════
 
# UI
 
# ════════════════════════════════════════════════════════
 
def score_bar(score):
filled = min(10, max(0, round(score / MAX_SCORE * 10)))
bar = “█” * filled + “░” * (10 - filled)
emoji = “🟢” if score >= 8 else (“🟡” if score >= 6 else (“🟠” if score >= 4 else “🔴”))
return f”{emoji} [{bar}] {score:.1f}/{MAX_SCORE}”
 
# ════════════════════════════════════════════════════════
 
# СКАН
 
# ════════════════════════════════════════════════════════
 
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
 
df = get_klines(SYMBOL_BN, "5m", 200)
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
 
# Heartbeat
if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
   last_heartbeat_time = now
   _send_heartbeat(price, atr_val, _L, _S, score, direction)
 
# Daily report
today = datetime.now(timezone.utc).day
if (datetime.now(timezone.utc).hour == DAILY_REPORT_HOUR
   and today != last_daily_report_day):
   last_daily_report_day = today
   _send_daily_report()
 
if direction is None:
   return
 
# Открытие
msg = [
   f"<b>[{'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}] v6.0</b>",
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
   res = okx_place_order(direction, entry, sl, tp)
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
       losses_in_row += 1
       if losses_in_row >= MAX_LOSSES:
           pause_until = now + PAUSE_LOSSES
           msg.append(f"⏸ Пауза {PAUSE_LOSSES // 60} мин")
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
   f"<b>❤️ Heartbeat v6.0</b>\n\n"
   f"💰 ETH: {price:.2f} | ATR: {atr_val:.2f}\n"
   f"😨 F&G:{fg} L/S:{ls:.2f}\n"
   f"📈 1h: {'🟢 Бычий' if b1h else '🔴 Медвежий'} ({p1h:+.2f}%)\n"
   f"{session} Баланс: {bal:.2f} USDT | Поз:{len(pos)}\n"
   f"🎯 {sig_status} L:{L:.1f} S:{S:.1f}\n"
   f"⚙️ MIN:{MIN_SCORE} База:{BASE_MIN_SCORE}\n"
   f"📊 {stats['total']} сд ✅{stats['wins']} ({winrate:.1f}%) "
   f"PF:{pf:.2f}\n"
   f"💵 P&L: {stats['total_profit']:+.2f} USDT"
   f"{ml_info}\n"
   f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
)
```
 
def _send_daily_report():
“”“Ежедневный отчёт в 23:00 UTC.”””
winrate = (stats[“wins”] / stats[“total”] * 100) if stats[“total”] > 0 else 0
pf = (stats[“total_profit_sum”] / stats[“total_loss_sum”]) if stats[“total_loss_sum”] > 0 else 0
avg = stats[“total_profit”] / stats[“total”] if stats[“total”] > 0 else 0
bal = okx_get_balance()
 
```
last_train_str = "ещё не было"
if stats["ml_last_train_ts"] > 0:
   last_train_str = datetime.fromtimestamp(stats["ml_last_train_ts"]).strftime("%d.%m %H:%M UTC")
 
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
   f"🧠 <b>ML:</b>\n"
   f"  Обучений: {stats['ml_trains_count']}\n"
   f"  Последнее: {last_train_str}\n"
   f"  Accuracy: {stats['ml_last_accuracy']:.1%}\n"
   f"  Сделок до след.: {ML_RETRAIN_EVERY - ml_train_counter}"
)
```
 
# ════════════════════════════════════════════════════════
 
# WATCHDOG
 
# ════════════════════════════════════════════════════════
 
def watchdog_loop():
“”“Алерт если скан не работает дольше WATCHDOG_THRESHOLD.”””
last_alert = 0
while True:
time.sleep(60)
now = time.time()
if last_scan_time > 0 and now - last_scan_time > WATCHDOG_THRESHOLD:
if now - last_alert > 600:
last_alert = now
send_telegram(
f”🚨 <b>WATCHDOG</b>\n”
f”Бот не сканирует {int((now - last_scan_time) / 60)} мин!”
)
 
# ════════════════════════════════════════════════════════
 
# FLASK
 
# ════════════════════════════════════════════════════════
 
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
 
# ════════════════════════════════════════════════════════
 
# MAIN LOOP
 
# ════════════════════════════════════════════════════════
 
def bot_loop():
log.info(“🚀 OKX Scalp Bot v6.0 starting”)
 
```
# Загрузка данных
storage_load_all()
 
# Тренировка ML на старте если есть данные
if len([s for s in signals_history if s.get("label") is not None]) >= ML_MIN_SAMPLES:
   log.info("Тренировка ML на старте...")
   _train_model()
 
# Восстановление активных позиций (свериться с биржей)
try:
   existing = okx_get_positions()
   actual_sides = {p.get("posSide") for p in existing if abs(float(p.get("pos", 0))) > 0}
   # Удаляем из active те которых уже нет на бирже
   for oid in list(active_positions.keys()):
       if active_positions[oid].get("pos_side") not in actual_sides:
           log.info(f"Удаляю стейл: {oid}")
           del active_positions[oid]
   # Добавляем в active то что есть на бирже но не в стейте
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
           }
   storage_save_async()
except Exception as e:
   log.error(f"recover positions: {e}")
 
bal = okx_get_balance()
if stats["peak_equity"] == 0:
   stats["current_equity"] = bal
   stats["peak_equity"] = bal
 
winrate = (stats["wins"] / stats["total"] * 100) if stats["total"] > 0 else 0
 
send_telegram(
   f"🚀 <b>OKX Scalp Bot v6.0</b>\n\n"
   f"💼 OKX | {SYMBOL}\n"
   f"📈 Плечо: x{LEVERAGE} | Сделка: {ORDER_USDT} USDT\n"
   f"🎯 SL: {SL_ATR_MULT}×ATR | TP: {TP_ATR_MULT}×ATR (RR={TP_ATR_MULT/SL_ATR_MULT:.1f})\n"
   f"⚙️ MIN_SCORE: {BASE_MIN_SCORE}/{MAX_SCORE} diff≥{MIN_SCORE_DIFF}\n"
   f"💰 Баланс: {bal:.2f} USDT\n"
   f"📊 История: {stats['total']} сд | ✅ {stats['wins']} ({winrate:.1f}%)\n"
   f"📜 Сигналов в базе: {len(signals_history)}\n\n"
   f"<b>Что в v6.0:</b>\n"
   f"• Хранилище JSONBin — данные не теряются\n"
   f"• ATR-based SL/TP, RR=2.0, плечо x20\n"
   f"• Фикс багов: PnL по order_id, отмена алго\n"
   f"• ML с весами по времени, retrain × 10\n"
   f"• Watchdog + ежедневный отчёт\n"
   f"• Спред-фильтр + фиксы метрик"
)
 
while True:
   try:
       run_scan()
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
