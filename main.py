import requests
import time
import os
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SDP_API_KEY = os.getenv("SDP_API_KEY", "").strip()
SDP_URL = os.getenv("SDP_URL", "https://sd.sadykhan.kz/api/v3/requests").strip()

DEEP_LINK_TEMPLATE = "https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={}&PORTALID=1"
CHECK_INTERVAL = 60

app = Flask(__name__)
subscribed_chats = set()
known_requests = {}

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if r.status_code == 403:
            subscribed_chats.discard(chat_id)
            print(f"❌ 403 FORBIDDEN. Удаляем {chat_id} из подписок.")
        else:
            print(f"❌ Ошибка {r.status_code} при отправке: {e}")
    except Exception as ex:
        print(f"❌ Ошибка при отправке: {ex}")

def send_to_subscribers(message):
    for chat_id in list(subscribed_chats):
        send_telegram_message(chat_id, message)

def get_all_requests():
    try:
        headers = {
            "authtoken": SDP_API_KEY,
            "Accept": "application/json"
        }
        response = requests.get(SDP_URL, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        print(f"Успешный ответ от SDP: {data}")
        return data.get("requests", [])
    except Exception as e:
        print(f"Ошибка при запросе к SDP: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Ответ сервера: {e.response.text}")
        return []

def parse_request_data(r):
    return {
        "id": str(r.get("id", "???")),
        "subject": r.get("subject") or "Без темы",
        "requester": (r.get("requester") or {}).get("name", "Неизвестный автор"),
        "technician": (r.get("technician") or {}).get("name", "Не назначен"),
        "status": (r.get("status") or {}).get("name", "N/A"),
        "created_time": r.get("created_time", {}).get("display_value", "")
    }

def build_deep_link(req_id):
    return DEEP_LINK_TEMPLATE.format(req_id)

def request_to_msg(req_data, prefix="Новая заявка"):
    return (
        f"🆕 <b>{prefix} #{req_data['id']}</b>\n"
        f"📌 <b>Тема:</b> {req_data['subject']}\n"
        f"👤 <b>Автор:</b> {req_data['requester']}\n"
        f"🔧 <b>Назначено:</b> {req_data['technician']}\n"
        f"⚙️ <b>Статус:</b> {req_data['status']}\n"
        f"📅 <b>Дата создания:</b> {req_data['created_time']}\n"
        f"🔗 <a href='{build_deep_link(req_data['id'])}'>Открыть заявку</a>"
    )

def check_sdp():
    while True:
        all_reqs = get_all_requests()
        if not all_reqs:
            print("Нет новых заявок или ошибка при получении данных.")
        for r in all_reqs:
            current = parse_request_data(r)
            rid = current["id"]

            if rid not in known_requests:
                known_requests[rid] = current
                send_to_subscribers(request_to_msg(current, prefix="Новая заявка"))
            else:
                old = known_requests[rid]
                changes = []

                if old["subject"] != current["subject"]:
                    changes.append(f"Тема: {old['subject']} → {current['subject']}")
                if old["requester"] != current["requester"]:
                    changes.append(f"Автор: {old['requester']} → {current['requester']}")
                if old["technician"] != current["technician"]:
                    changes.append(f"Назначено: {old['technician']} → {current['technician']}")
                if old["status"] != current["status"]:
                    changes.append(f"Статус: {old['status']} → {current['status']}")
                if old["created_time"] != current["created_time"]:
                    changes.append(f"Дата создания: {old['created_time']} → {current['created_time']}")

                if changes:
                    msg = (
                        f"✏️ <b>Изменения по заявке #{rid}</b>\n" +
                        "\n".join(changes) +
                        f"\n🔗 <a href='{build_deep_link(rid)}'>Открыть заявку</a>"
                    )
                    send_to_subscribers(msg)
                    known_requests[rid] = current

        time.sleep(CHECK_INTERVAL)

def get_requests_last_hour():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    results = []
    for r in get_all_requests():
        ctime_str = r.get("created_time", {}).get("display_value", "")
        try:
            dt = datetime.strptime(ctime_str, "%d/%m/%Y %I:%M %p").replace(tzinfo=timezone.utc)
        except:
            continue
        if dt >= cutoff:
            results.append(r)
    return results

def requests_list_to_text(requests_data):
    if not requests_data:
        return "За последний час заявок не найдено."
    lines = []
    for r in requests_data:
        parsed = parse_request_data(r)
        lines.append(
            f"🔹 #{parsed['id']} | {parsed['subject']} | {parsed['requester']} | "
            f"{parsed['technician']} | {parsed['status']} | {parsed['created_time']}"
        )
    return "\n".join(lines)

def telegram_bot():
    offset = None
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "").strip().lower()

            if not chat_id or not text:
                continue

            if text in ("/start", "start"):
                subscribed_chats.add(chat_id)
                send_telegram_message(chat_id, "✅ Вы подписаны на уведомления по заявкам.")
                last_hour = get_requests_last_hour()
                send_telegram_message(chat_id, "Заявки за последний час:\n" + requests_list_to_text(last_hour))
            elif text in ("/stop", "stop"):
                subscribed_chats.discard(chat_id)
                send_telegram_message(chat_id, "❌ Вы отписаны от уведомлений.")
            else:
                send_telegram_message(chat_id, "Используйте /start или /stop.")

        time.sleep(2)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    if not BOT_TOKEN or not SDP_API_KEY:
        print("❌ Ошибка: BOT_TOKEN или SDP_API_KEY не установлены.")
        exit(1)

    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
