#!/usr/bin/env python3
# coding: utf-8

import os
import json
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask

# ==============================
# Настройки из окружения
# ==============================
BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
SDP_API_KEY = os.getenv("SDP_API_KEY", "").strip()
SDP_URL     = os.getenv("SDP_URL", "https://sd.sadykhan.kz/api/v3/requests").strip()
PORT        = int(os.getenv("PORT", "5000"))

if not BOT_TOKEN or not SDP_API_KEY:
    print("❌ Ошибка: BOT_TOKEN или SDP_API_KEY не установлены.")
    exit(1)

DEEP_LINK_TEMPLATE = "https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={}&PORTALID=1"
CHECK_INTERVAL    = 60   # секунд между запросами к SDP

app = Flask(__name__)
subscribed_chats = set()
known_requests   = {}

# Сессия с ретраями
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({
    "authtoken": SDP_API_KEY,
    "Accept": "application/json",
    "Content-Type": "application/json"
})

# ==============================
# Вспомогательные функции
# ==============================
def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if r.status_code == 403:
            subscribed_chats.discard(chat_id)
            print(f"❌ 403 Forbidden: удаляем чат {chat_id} из подписок")
        else:
            print(f"❌ HTTP {r.status_code} при отправке Telegram: {e}")
    except Exception as ex:
        print(f"❌ Ошибка при отправке в Telegram: {ex}")

def build_deep_link(req_id: str) -> str:
    return DEEP_LINK_TEMPLATE.format(req_id)

def parse_request_data(r: dict) -> dict:
    """Вынимаем из JSON только нужные поля"""
    return {
        "id":               r.get("id", "???"),
        "subject":          r.get("short_description", "Без темы"),
        "requester":        (r.get("requester")  or {}).get("name", "Неизвестен"),
        "technician":       (r.get("technician") or {}).get("name", "Не назначен"),
        "status":           (r.get("status")     or {}).get("name", "N/A"),
        "created_time":     r.get("created_time", {}).get("display_value", "")
    }

def request_to_msg(d: dict, prefix="Новая заявка") -> str:
    return (
        f"🆕 <b>{prefix} #{d['id']}</b>\n"
        f"📌 <b>Тема:</b> {d['subject']}\n"
        f"👤 <b>Автор:</b> {d['requester']}\n"
        f"🔧 <b>Назначено:</b> {d['technician']}\n"
        f"⚙️ <b>Статус:</b> {d['status']}\n"
        f"📅 <b>Создана:</b> {d['created_time']}\n"
        f"🔗 <a href='{build_deep_link(d['id'])}'>Открыть заявку</a>"
    )

# ==============================
# Основная логика работы с SDP
# ==============================
def get_all_requests(row_count=10) -> list:
    """Получаем все заявки, проходя по страницам."""
    all_reqs = []
    start = 1

    while True:
        payload = {
            "input_data": {
                "list_info": {
                    "row_count":      row_count,
                    "start_index":    start,
                    "get_total_count": True
                }
            }
        }
        print(f"→ SDP payload: start={start}, rows={row_count}")
        print(json.dumps(payload, ensure_ascii=False))
        resp = session.post(SDP_URL, json=payload, timeout=(5, 30))
        print(f"← HTTP {resp.status_code}, body: {resp.text}")

        resp.raise_for_status()
        data = resp.json()

        # Проверяем статус выполнения
        status_obj = data.get("response_status", [{}])[0]
        if status_obj.get("status") != "success":
            print("❌ SDP вернул ошибку:", status_obj)
            break

        page = data.get("requests", [])
        all_reqs.extend(page)

        info = data.get("list_info", {})
        if not info.get("has_more_rows"):
            break

        start += row_count

    print(f"✓ Всего заявок получено: {len(all_reqs)}")
    return all_reqs

# ==============================
# Цикл мониторинга и рассылки
# ==============================
def check_sdp():
    while True:
        try:
            all_reqs = get_all_requests()
            for r in all_reqs:
                cur = parse_request_data(r)
                rid = cur["id"]

                if rid not in known_requests:
                    known_requests[rid] = cur
                    send_to_subscribers(request_to_msg(cur, prefix="Новая заявка"))
                else:
                    old = known_requests[rid]
                    diffs = []

                    for field in ("subject","requester","technician","status","created_time"):
                        if old[field] != cur[field]:
                            diffs.append(f"{field}: {old[field]} → {cur[field]}")

                    if diffs:
                        msg = "✏️ <b>Изменения в заявке #"+rid+"</b>\n" + "\n".join(diffs)
                        msg += f"\n🔗 <a href='{build_deep_link(rid)}'>Открыть заявку</a>"
                        send_to_subscribers(msg)
                        known_requests[rid] = cur

        except Exception as e:
            print("❌ Ошибка в check_sdp:", e)

        time.sleep(CHECK_INTERVAL)

def send_to_subscribers(text: str):
    for cid in list(subscribed_chats):
        send_telegram_message(cid, text)

# ==============================
# Telegram-бот (getUpdates)
# ==============================
def get_requests_last_hour():
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = []
    for r in get_all_requests():
        ts = r.get("created_time", {}).get("display_value","")
        try:
            dt = datetime.strptime(ts, "%d/%m/%Y %I:%M %p").replace(tzinfo=timezone.utc)
        except:
            continue
        if dt >= one_hour_ago:
            recent.append(r)
    return recent

def requests_list_to_text(lst: list) -> str:
    if not lst:
        return "За последний час заявок не найдено."
    lines = []
    for r in lst:
        p = parse_request_data(r)
        lines.append(f"🔹 #{p['id']} | {p['subject']} | {p['requester']} | {p['technician']} | {p['status']} | {p['created_time']}")
    return "\n".join(lines)

def telegram_bot():
    offset = None
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset}, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("⚠️ Ошибка getUpdates:", e)
            time.sleep(5)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            cid = msg.get("chat", {}).get("id")
            txt = msg.get("text", "").strip().lower()

            if not cid or not txt:
                continue

            if txt in ("/start", "start"):
                subscribed_chats.add(cid)
                send_telegram_message(cid, "✅ Подписка на заявки активирована.")
                recent = get_requests_last_hour()
                send_telegram_message(cid, "Заявки за последний час:\n" + requests_list_to_text(recent))

            elif txt in ("/stop", "stop"):
                subscribed_chats.discard(cid)
                send_telegram_message(cid, "❌ Вы отписаны от уведомлений.")

            else:
                send_telegram_message(cid, "Команда не распознана. Используйте /start или /stop.")

        time.sleep(2)

# ==============================
# Запуск Flask и фоновых потоков
# ==============================
@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # Запускаем потоки
    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()
    print(f"🚀 Starting Flask on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)
