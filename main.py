# ✅ Railway-Ready, GitHub-Friendly SDP Telegram Bot
# Учитывает: переменные окружения, long polling, Flask endpoint, адаптирован для облачного деплоя

import requests
import time
import os
import threading
from datetime import datetime, timedelta
from flask import Flask

# --- Переменные окружения (Railway, Render, Heroku совместимо) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
SDP_API_KEY = os.getenv("SDP_API_KEY")
SDP_URL = os.getenv("SDP_URL", "https://sd.sadykhan.kz/api/v3/requests")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # по умолчанию 60 сек
PORT = int(os.getenv("PORT", 5000))

# --- Ссылка на заявку ---
DEEP_LINK_TEMPLATE = "https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={}&PORTALID=1"

app = Flask(__name__)
subscribed_chats = set()
known_requests = {}

# --- Telegram ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if r.status_code == 403:
            subscribed_chats.discard(chat_id)
            print(f"❌ 403. Удалён chat_id: {chat_id}")
        else:
            print(f"Telegram HTTPError: {e}")
    except Exception as ex:
        print(f"Telegram Exception: {ex}")

def send_to_subscribers(message):
    for chat_id in list(subscribed_chats):
        send_telegram_message(chat_id, message)

# --- SDP ---
def get_all_requests():
    try:
        headers = {"Authtoken": SDP_API_KEY, "Accept": "application/json"}
        r = requests.get(SDP_URL, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json().get("requests", [])
    except Exception as e:
        print(f"SDP Error: {e}")
        return []

def parse_request_data(r):
    return {
        "id": str(r.get("id", "???")),
        "subject": r.get("subject", "Без темы"),
        "requester": r.get("requester", {}).get("name", "Неизвестный автор"),
        "technician": r.get("technician", {}).get("name", "Не назначен"),
        "status": r.get("status", {}).get("name", "N/A"),
        "created_time": r.get("created_time", {}).get("display_value", "")
    }

def build_deep_link(req_id):
    return DEEP_LINK_TEMPLATE.format(req_id)

def request_to_msg(req_data, prefix="Новая заявка"):
    msg = (
        f"🆕 <b>{prefix} #{req_data['id']}</b>\n"
        f"📌 <b>Тема:</b> {req_data['subject']}\n"
        f"👤 <b>Автор:</b> {req_data['requester']}\n"
        f"🔧 <b>Назначено:</b> {req_data['technician']}\n"
        f"⚙️ <b>Статус:</b> {req_data['status']}\n"
        f"📅 <b>Дата создания:</b> {req_data['created_time']}\n"
        f"🔗 <a href='{build_deep_link(req_data['id'])}'>Открыть заявку</a>"
    )
    return msg

# --- Проверка заявок ---
def check_sdp():
    while True:
        for r in get_all_requests():
            current = parse_request_data(r)
            rid = current["id"]

            if rid not in known_requests:
                known_requests[rid] = current
                send_to_subscribers(request_to_msg(current))
            else:
                old = known_requests[rid]
                diffs = []
                for field in ["subject", "requester", "technician", "status", "created_time"]:
                    if current[field] != old[field]:
                        diffs.append(f"{field}: {old[field]} → {current[field]}")

                if diffs:
                    msg = (
                        f"✏️ <b>Изменения по заявке #{rid}</b>\n"
                        f"{chr(10).join(diffs)}\n"
                        f"🔗 <a href='{build_deep_link(rid)}'>Открыть заявку</a>"
                    )
                    send_to_subscribers(msg)
                    known_requests[rid] = current

        time.sleep(CHECK_INTERVAL)

# --- Последний час ---
def get_requests_last_hour():
    cutoff = datetime.utcnow() - timedelta(hours=1)
    result = []
    for r in get_all_requests():
        t = r.get("created_time", {}).get("display_value", "")
        try:
            dt = datetime.strptime(t, "%d/%m/%Y %I:%M %p")
            if dt >= cutoff:
                result.append(r)
        except:
            continue
    return result

def requests_list_to_text(reqlist):
    if not reqlist:
        return "За последний час заявок не найдено."
    lines = []
    for r in reqlist:
        p = parse_request_data(r)
        lines.append(f"🔹 #{p['id']} | {p['subject']} | {p['requester']} | {p['technician']} | {p['status']} | {p['created_time']}")
    return "\n".join(lines)

# --- Telegram Polling ---
def telegram_bot():
    offset = None
    while True:
        try:
            resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"offset": offset}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    chat_id = upd["message"]["chat"]["id"]
                    text = upd["message"].get("text", "").strip().lower()
                    if text in ("/start", "start"):
                        subscribed_chats.add(chat_id)
                        send_telegram_message(chat_id, "✅ Подписка оформлена.")
                        send_telegram_message(chat_id, "Заявки за последний час:\n" + requests_list_to_text(get_requests_last_hour()))
                    elif text in ("/stop", "stop"):
                        subscribed_chats.discard(chat_id)
                        send_telegram_message(chat_id, "❌ Вы отписаны.")
                    else:
                        send_telegram_message(chat_id, "Введите /start или /stop.")
        except Exception as e:
            print(f"Ошибка Telegram: {e}")
            time.sleep(5)
        time.sleep(2)

# --- Flask Ping Route ---
@app.route("/")
def home():
    return "SDP Bot is running!"

# --- Main Entry Point ---
if __name__ == "__main__":
    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
