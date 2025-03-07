import requests
import time
import os
import threading
from datetime import datetime
from flask import Flask

###########################
#  КОНСТАНТЫ И НАСТРОЙКИ  #
###########################

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SDP_API_KEY = os.getenv("SDP_API_KEY", "")

# Базовый URL ManageEngine SDP
SDP_URL = "https://sd.sadykhan.kz/api/v3/requests"
SDP_REQUEST_URL = "https://sd.sadykhan.kz/requests"

app = Flask(__name__)

# Здесь храним, кто подписан на уведомления (в памяти)
subscribed_chats = set()

# Здесь храним уже известные заявки { request_id: { "status":..., "technician":..., ... } }
known_requests = {}

##################################
#  ОТПРАВКА СООБЩЕНИЙ В TELEGRAM  #
##################################

def send_telegram_message(chat_id, text):
    """
    Универсальная функция для отправки текста в чат.
    При 403-ошибке (бот заблокирован) удаляем chat_id из подписчиков.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 403:
            # Пользователь заблокировал бота
            if chat_id in subscribed_chats:
                subscribed_chats.remove(chat_id)
            print(f"❌ 403 FORBIDDEN: {chat_id} убираем из подписок.")
        else:
            print(f"❌ Ошибка {resp.status_code} при отправке: {e}")
    except Exception as ex:
        print(f"❌ Ошибка при отправке: {ex}")


def send_to_subscribers(message):
    """Рассылка всем подписанным чатам."""
    for chat_id in list(subscribed_chats):
        send_telegram_message(chat_id, message)

##################################
#  ПОЛУЧЕНИЕ ВСЕХ ЗАЯВОК ИЗ SDP  #
##################################

def get_all_requests():
    """
    Тянем все заявки (без фильтра).
    Можно улучшить: передавать параметры для фильтрации.
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


#########################################
#  ПРОВЕРКА И ОТСЛЕЖИВАНИЕ ИЗМЕНЕНИЙ   #
#########################################

def check_sdp():
    """
    Фоновый процесс: каждые 60 сек получаем все заявки.
    Сравниваем со словарём known_requests:
      - если новая (id нет в known_requests) => сообщаем «Новая заявка»
      - если есть, но статус (или техник) изменился => сообщаем «Изменение»
    """
    while True:
        all_reqs = get_all_requests()

        for r in all_reqs:
            req_id = str(r.get("id", "???"))
            subject = r.get("subject", "Без темы") or "Без темы"
            desc = r.get("description", "Нет описания") or "Нет описания"

            # Бывает technician=None => подстрахуемся
            technician_info = r.get("technician") or {}
            technician = technician_info.get("name", "Не назначен")

            status_info = r.get("status") or {}
            status = status_info.get("name", "N/A")

            requester_info = r.get("requester") or {}
            requester = requester_info.get("name", "Неизвестный автор")

            created_time = r.get("created_time", {}).get("display_value", "")

            current_data = {
                "status": status,
                "technician": technician,
                "subject": subject,
                "description": desc,
                "requester": requester,
                "created_time": created_time
            }

            # 1) Если не знаем про эту заявку => новая
            if req_id not in known_requests:
                known_requests[req_id] = current_data

                msg = (
                    f"🆕 <b>Новая заявка #{req_id}</b>\n"
                    f"👤 <b>Автор:</b> {requester}\n"
                    f"📌 <b>Тема:</b> {subject}\n"
                    f"📝 <b>Описание:</b> {desc}\n"
                    f"🔧 <b>Ответственный:</b> {technician}\n"
                    f"🕑 <b>Статус:</b> {status}\n"
                    f"📅 <b>Дата:</b> {created_time}\n"
                    f"🔗 <a href='{SDP_REQUEST_URL}/{req_id}'>Открыть заявку</a>"
                )
                send_to_subscribers(msg)

            else:
                # 2) Заявка уже известна: проверим изменения
                old_data = known_requests[req_id]
                changed_fields = []

                # Сменился статус?
                if old_data["status"] != current_data["status"]:
                    changed_fields.append(
                        f"Статус: {old_data['status']} → {current_data['status']}"
                    )

                # Сменился техник?
                if old_data["technician"] != current_data["technician"]:
                    changed_fields.append(
                        f"Техник: {old_data['technician']} → {current_data['technician']}"
                    )

                # Если есть изменения, формируем сообщение
                if changed_fields:
                    changes_text = "\n".join(changed_fields)
                    msg = (
                        f"✏️ <b>Изменения по заявке #{req_id}</b>\n"
                        f"{changes_text}\n"
                        f"🔗 <a href='{SDP_REQUEST_URL}/{req_id}'>Открыть заявку</a>"
                    )
                    send_to_subscribers(msg)

                # Обновляем данные заявки
                known_requests[req_id] = current_data

        time.sleep(60)


############################
#  TELEGRAM LONG POLLING   #
############################

def telegram_bot():
    """
    Фоновый поток: каждые 2 сек дергает getUpdates.
    /start => подписываем, /stop => отписываем.
    """
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = requests.get(url, params={"offset": offset}, timeout=30)
            resp.raise_for_status()
            updates = resp.json()
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        for upd in updates.get("result", []):
            offset = upd["update_id"] + 1

            if "message" in upd:
                chat_id = upd["message"]["chat"]["id"]
                text = upd["message"].get("text", "").strip().lower()

                if text in ("/start", "start"):
                    subscribed_chats.add(chat_id)
                    send_telegram_message(chat_id, "✅ Вы подписаны на уведомления по заявкам (новые и изменения статусов).")
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
#  FLASK - веб-сервер      #
############################

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    # 1) Запускаем фоновые потоки
    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()

    # 2) Запускаем Flask
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
