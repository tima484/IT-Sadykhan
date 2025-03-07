import requests
import time
import os
import threading
from datetime import datetime, timedelta
from flask import Flask

###########################
#  КОНСТАНТЫ И НАСТРОЙКИ  #
###########################

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SDP_API_KEY = os.getenv("SDP_API_KEY", "")

# Основной URL API для получения заявок (JSON)
SDP_URL = "https://sd.sadykhan.kz/api/v3/requests"

# Формируем "глубокую" ссылку на заявку через WorkOrder.do
# (подставим req_id в woID=)
DEEP_LINK_TEMPLATE = "https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={}&PORTALID=1"

# Проверяем каждые N секунд
CHECK_INTERVAL = 60

app = Flask(__name__)

# Сеты и словари в памяти (обнулятся при перезапуске)
subscribed_chats = set()

# Известные заявки: { request_id: {"id":..., "subject":..., "requester":..., "technician":..., "status":..., "created_time":...} }
known_requests = {}

##################################
#     HELPER: ОТПРАВКА В TELEGRAM
##################################

def send_telegram_message(chat_id, text):
    """
    Отправить сообщение в Telegram.
    При 403-ошибке (бот заблокирован) удаляем пользователя из подписок.
    """
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
            if chat_id in subscribed_chats:
                subscribed_chats.remove(chat_id)
            print(f"❌ 403 FORBIDDEN. Удаляем {chat_id} из подписок.")
        else:
            print(f"❌ Ошибка {r.status_code} при отправке: {e}")
    except Exception as ex:
        print(f"❌ Ошибка при отправке: {ex}")

def send_to_subscribers(message):
    """Разослать сообщение всем подписанным чатам."""
    for chat_id in list(subscribed_chats):
        send_telegram_message(chat_id, message)

##################################
#     ПОЛУЧЕНИЕ ЗАЯВОК ИЗ SDP
##################################

def get_all_requests():
    """
    Получаем список всех заявок из ServiceDesk (JSON-формат).
    Можно улучшить (фильтровать, пагинация), но для примера тянем всё.
    """
    try:
        resp = requests.get(
            SDP_URL,
            headers={"Authtoken": SDP_API_KEY, "Accept": "application/json"},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("requests", [])
    except Exception as e:
        print(f"Ошибка при запросе к SDP: {e}")
        return []

def parse_request_data(r):
    """
    Извлекаем нужные поля (ID, Тема, Автор, Назначено, Статус, Дата создания) 
    и возвращаем их в виде словаря.
    """
    req_id = str(r.get("id", "???"))

    subject = r.get("subject") or "Без темы"

    requester_info = r.get("requester") or {}
    requester = requester_info.get("name", "Неизвестный автор")

    tech_info = r.get("technician") or {}
    technician = tech_info.get("name", "Не назначен")

    status_info = r.get("status") or {}
    status = status_info.get("name", "N/A")

    created_val = r.get("created_time", {}).get("display_value", "")

    return {
        "id": req_id,
        "subject": subject,
        "requester": requester,
        "technician": technician,
        "status": status,
        "created_time": created_val
    }

def build_deep_link(req_id):
    """
    Возвращаем "глубокую" ссылку для заявки:
    https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={req_id}&PORTALID=1
    """
    return DEEP_LINK_TEMPLATE.format(req_id)

def request_to_msg(req_data, prefix="Новая заявка"):
    """
    Формируем текст уведомления, где указываем ID, Тему, Автора, Назначено, Статус, Дату.
    И добавляем "глубокую" ссылку WorkOrder.do?woID=...
    """
    rid = req_data["id"]
    subject = req_data["subject"]
    requester = req_data["requester"]
    technician = req_data["technician"]
    status = req_data["status"]
    created_time = req_data["created_time"]

    deep_link = build_deep_link(rid)

    msg = (
        f"🆕 <b>{prefix} #{rid}</b>\n"
        f"📌 <b>Тема:</b> {subject}\n"
        f"👤 <b>Автор:</b> {requester}\n"
        f"🔧 <b>Назначено:</b> {technician}\n"
        f"⚙️ <b>Статус:</b> {status}\n"
        f"📅 <b>Дата создания:</b> {created_time}\n"
        f"🔗 <a href='{deep_link}'>Открыть заявку</a>"
    )
    return msg

##########################
#   ПЕРИОДИЧЕСКАЯ ПРОВЕРКА
##########################

def check_sdp():
    """
    Фоновый поток, каждые 60 сек:
      - получаем все заявки
      - для каждой:
        * если её нет в known_requests => "новая"
        * иначе сверяем 6 полей: тема, автор, техник, статус, дата создания 
          и если что-то изменилось => "Изменения"
    """
    while True:
        all_reqs = get_all_requests()
        for r in all_reqs:
            current = parse_request_data(r)
            rid = current["id"]

            if rid not in known_requests:
                # Новая заявка
                known_requests[rid] = current
                msg = request_to_msg(current, prefix="Новая заявка")
                send_to_subscribers(msg)
            else:
                # Уже знали про заявку, проверим изменения
                old = known_requests[rid]
                changed_fields = []

                # subject
                if old["subject"] != current["subject"]:
                    changed_fields.append(f"Тема: {old['subject']} → {current['subject']}")
                # requester
                if old["requester"] != current["requester"]:
                    changed_fields.append(f"Автор: {old['requester']} → {current['requester']}")
                # technician
                if old["technician"] != current["technician"]:
                    changed_fields.append(f"Назначено: {old['technician']} → {current['technician']}")
                # status
                if old["status"] != current["status"]:
                    changed_fields.append(f"Статус: {old['status']} → {current['status']}")
                # created_time
                if old["created_time"] != current["created_time"]:
                    changed_fields.append(f"Дата создания: {old['created_time']} → {current['created_time']}")

                if changed_fields:
                    diffs = "\n".join(changed_fields)
                    deep_link = build_deep_link(rid)
                    msg = (
                        f"✏️ <b>Изменения по заявке #{rid}</b>\n"
                        f"{diffs}\n"
                        f"🔗 <a href='{deep_link}'>Открыть заявку</a>"
                    )
                    send_to_subscribers(msg)
                    known_requests[rid] = current

        time.sleep(CHECK_INTERVAL)

##################################
#   ЗАЯВКИ ЗА ПОСЛЕДНИЙ ЧАС (/START)
##################################

def get_requests_last_hour():
    """
    Возвращаем заявки, созданные за последний час (UTC).
    """
    cutoff = datetime.utcnow() - timedelta(hours=1)
    all_reqs = get_all_requests()
    results = []
    for r in all_reqs:
        ctime_str = r.get("created_time", {}).get("display_value", "")
        try:
            # Формат "dd/MM/yyyy hh:mm AM/PM" (напр. "07/03/2025 04:31 PM")
            dt = datetime.strptime(ctime_str, "%d/%m/%Y %I:%M %p")
        except:
            continue
        if dt >= cutoff:
            results.append(r)
    return results

def requests_list_to_text(requests_data):
    """Возвращаем короткий список (ID, Тема, Автор, Назначено, Статус, Дата)."""
    if not requests_data:
        return "За последний час заявок не найдено."
    lines = []
    for r in requests_data:
        parsed = parse_request_data(r)
        line = (
            f"🔹 #{parsed['id']} | {parsed['subject']} | {parsed['requester']} | "
            f"{parsed['technician']} | {parsed['status']} | {parsed['created_time']}"
        )
        lines.append(line)
    return "\n".join(lines)

############################
#   TELEGRAM LONG POLLING
############################

def telegram_bot():
    """
    /start -> подписывает, показывает заявки за последний час
    /stop -> отписывает
    """
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params={"offset": offset}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1

            if "message" in upd:
                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text", "").strip().lower()

                if text in ("/start", "start"):
                    subscribed_chats.add(chat_id)
                    send_telegram_message(chat_id, "✅ Вы подписаны на уведомления по заявкам.")
                    
                    # Покажем заявки за последний час
                    last_hour = get_requests_last_hour()
                    msg = "Заявки за последний час:\n"
                    msg += requests_list_to_text(last_hour)
                    send_telegram_message(chat_id, msg)

                elif text in ("/stop", "stop"):
                    if chat_id in subscribed_chats:
                        subscribed_chats.remove(chat_id)
                        send_telegram_message(chat_id, "❌ Вы отписаны от уведомлений.")
                    else:
                        send_telegram_message(chat_id, "Вы и так не подписаны.")
                else:
                    send_telegram_message(chat_id, "Используйте /start или /stop.")

        time.sleep(2)

############################
#  FLASK - веб-сервер
############################

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # 1) Запустим потоки: телеграм-бот, проверка SDP
    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()

    # 2) Flask-приложение
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
