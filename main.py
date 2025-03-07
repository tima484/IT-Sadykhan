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

# URL вашего ServiceDesk
SDP_URL = "https://sd.sadykhan.kz/api/v3/requests"
SDP_REQUEST_URL = "https://sd.sadykhan.kz/requests"

# Количество минут, в течение которых заявки считаем «свежими» 
CHECK_WINDOW_MINUTES = 5

app = Flask(__name__)

# Храним подписанные чаты только в памяти (при перезапуске сбрасываются)
subscribed_chats = set()

##################################
#  ОТПРАВКА В TELEGRAM ФУНКЦИИ   #
##################################

def send_telegram_message(chat_id, text):
    """Шлём сообщение в чат. При 403 удаляем юзера из подписок."""
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
            # Пользователь заблокировал бота
            if chat_id in subscribed_chats:
                subscribed_chats.remove(chat_id)
            print(f"❌ 403 FORBIDDEN: {chat_id} убираем из подписок.")
        else:
            print(f"❌ HTTPError {r.status_code} при отправке: {e}")
    except Exception as ex:
        print(f"❌ Ошибка при отправке: {ex}")

def send_to_subscribers(message):
    """Рассылка всем подписанным чатам."""
    for chat_id in list(subscribed_chats):
        send_telegram_message(chat_id, message)

##################################
#  ПОЛУЧЕНИЕ ЗАЯВОК ИЗ SDP       #
##################################

def get_recent_requests():
    """
    Тянем все заявки (или фильтруем на стороне SDP, если API позволяет).
    Внутри сами ограничиваемся CHECK_WINDOW_MINUTES, 
    чтобы не слать слишком старые заявки (и не спамить).
    """
    now = datetime.utcnow()
    try:
        resp = requests.get(SDP_URL, headers={"Authtoken": SDP_API_KEY, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Ошибка при запросе к SDP: {e}")
        return []

    fresh_requests = []
    for req in data.get("requests", []):
        # Попробуем взять created_time.display_value (или last_updated_time, если есть)
        created_str = req.get("created_time", {}).get("display_value", "")
        try:
            dt_created = datetime.strptime(created_str, "%d-%m-%Y %H:%M:%S")
        except:
            dt_created = now  # Если не парсится, считаем равным now

        # Считаем заявку «свежей», если она была создана меньше CHECK_WINDOW_MINUTES назад
        # (Чтобы при перезапуске не приходили заявки неделей давности)
        if (now - dt_created) < timedelta(minutes=CHECK_WINDOW_MINUTES):
            fresh_requests.append(req)

    return fresh_requests

def check_sdp():
    """
    Фоновый процесс: каждые 60 сек тянет заявки, 
    считает «новыми» всё, что младше CHECK_WINDOW_MINUTES, и шлёт уведомления.
    При «проспанном» времени более CHECK_WINDOW_MINUTES заявки не попадут в рассылку.
    """
    while True:
        requests_list = get_recent_requests()
        for r in requests_list:
            req_id = r.get("id", "???")
            subject = r.get("subject", "Без темы")
            desc = r.get("description", "Нет описания")
            requester = r.get("requester", {}).get("name", "Неизвестный автор")
            tech = r.get("technician", {}).get("name", "Не назначен")
            status = r.get("status", {}).get("name", "N/A")
            created_val = r.get("created_time", {}).get("display_value", "")

            msg = (
                f"🆕 <b>Новая заявка #{req_id}</b>\n"
                f"👤 <b>Автор:</b> {requester}\n"
                f"📌 <b>Тема:</b> {subject}\n"
                f"📝 <b>Описание:</b> {desc}\n"
                f"🔧 <b>Ответственный:</b> {tech}\n"
                f"🕑 <b>Статус:</b> {status}\n"
                f"📅 <b>Дата:</b> {created_val}\n"
                f"🔗 <a href='{SDP_REQUEST_URL}/{req_id}'>Открыть заявку</a>"
            )
            send_to_subscribers(msg)

        time.sleep(60)

##################################
#    LONG POLLING TELEGRAM       #
##################################

def telegram_bot():
    """
    Фоновый поток: каждые 2 сек дергает getUpdates,
    /start — подписка, /stop — отписка.
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
                    send_telegram_message(chat_id, "✅ Вы подписаны на уведомления о заявках.")
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
    # Запускаем фоновые потоки
    threading.Thread(target=telegram_bot, daemon=True).start()
    threading.Thread(target=check_sdp, daemon=True).start()

    # Railway устанавливает PORT автоматически; берём из переменной окружения
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
