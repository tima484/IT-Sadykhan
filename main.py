import requests
import time
from datetime import datetime, timedelta
import threading
from flask import Flask

# Конфигурация Telegram Bot API
TELEGRAM_TOKEN = "<TELEGRAM_BOT_TOKEN>"  # TODO: вставить токен бота
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"

# Конфигурация ServiceDesk Plus API
SDP_API_URL = "https://sd.sadykhan.kz/api/v3/requests"
SDP_API_KEY = "<TECHNICIAN_API_KEY>"    # TODO: вставить TECHNICIAN_KEY для API SDP
SDP_HEADERS = {"TECHNICIAN_KEY": SDP_API_KEY, "Content-Type": "application/json"}

# Интервалы опроса
SDP_POLL_INTERVAL = 60        # интервал опроса API заявок (в секундах)
TELEGRAM_POLL_INTERVAL = 1    # интервал опроса обновлений Telegram (в секундах)

# Глобальные структуры данных
known_requests = {}    # словарь известных заявок {id: данные заявки}
subscribers = set()    # множество подписанных chat_id
last_update_id = 0     # ID последнего обработанного обновления Telegram

# Инициализация Flask
app = Flask(__name__)

@app.route('/')
def index():
    return "ServiceDesk Plus bot is running."

def fetch_requests():
    """Запрос списка заявок из ServiceDesk Plus через API."""
    input_data = {
        "list_info": {
            "row_count": 100,
            "sort_field": "created_time",
            "sort_order": "desc"
            # При необходимости можно добавить фильтр по статусу:
            # "search_criteria": {
            #     "field": "status.name",
            #     "condition": "not equal",
            #     "value": "Закрыто"
            # }
        }
    }
    try:
        resp = requests.post(SDP_API_URL, headers=SDP_HEADERS, json=input_data, timeout=10)
        data = resp.json()
        return data.get("requests", [])
    except Exception as e:
        print(f"Error fetching requests: {e}")
        return []

def format_request_message(req, is_new=True):
    """Форматирование данных заявки в текст уведомления."""
    subject = req.get("subject", "Без темы")
    requester_name = req.get("requester", {}).get("name", "Неизвестно")
    # Проверяем наличие назначенного техника
    technician = req.get("technician")
    tech_name = technician.get("name") if technician else None
    tech_name = tech_name if tech_name else "Не назначен"
    status_name = req.get("status", {}).get("name", "Неизвестен")
    created_time = req.get("created_time", {}).get("display_value", "")
    request_id = req.get("id", "")
    # Формируем ссылку на заявку по ID (формат WorkOrder.do)
    request_link = f"https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={request_id}"
    header = "🆕 Новая заявка" if is_new else "ℹ️ Обновление заявки"
    message = (f"{header}\n"
               f"📌 Тема: {subject}\n"
               f"👤 Автор: {requester_name}\n"
               f"🔧 Назначено: {tech_name}\n"
               f"⚙️ Статус: {status_name}\n"
               f"📅 Дата создания: {created_time}\n"
               f"🔗 Открыть заявку: [Ссылка]({request_link})")
    return message

def send_message(chat_id, text):
    """Отправка сообщения в чат Telegram."""
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        requests.get(TELEGRAM_API_URL + "sendMessage", params=payload, timeout=5)
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")

def handle_sutki_command(chat_id):
    """Обработка команды /sutki: отправка списка заявок за последние 24 часа."""
    now = datetime.now()
    since_time = now - timedelta(days=1)
    results = []
    for req_id, info in known_requests.items():
        created_val = info.get("created_time_value")
        if created_val:
            created_dt = datetime.fromtimestamp(created_val/1000.0)
            if created_dt >= since_time:
                subj = info.get("subject", "")
                status = info.get("status", {}).get("name", "")
                created_disp = info.get("created_time", {}).get("display_value", "")
                results.append(f"{req_id} – {subj} – Статус: {status} – Создана: {created_disp}")
    if results:
        message = "📝 Заявки за последние 24 часа:\n" + "\n".join(results)
    else:
        message = "📭 За последние 24 часа заявок нет."
    send_message(chat_id, message)

def bot_loop():
    """Основной цикл бота: опрос API SDP и Telegram, обработка событий."""
    global last_update_id
    # Начальная загрузка списка заявок
    initial_list = fetch_requests()
    for req in initial_list:
        rid = req.get("id")
        if not rid:
            continue
        # Сохраняем ключевые поля заявки
        created_val = None
        if req.get("created_time") and req["created_time"].get("value"):
            try:
                created_val = int(req["created_time"]["value"])
            except:
                created_val = None
        known_requests[rid] = {
            "id": rid,
            "subject": req.get("subject"),
            "requester": req.get("requester", {}),
            "technician": req.get("technician"),
            "status": req.get("status", {}),
            "created_time": req.get("created_time", {}),
            "created_time_value": created_val
        }
    print(f"Loaded {len(known_requests)} initial requests.")
    last_sdp_poll = time.time()
    # Бесконечный цикл
    while True:
        # 1. Опрос обновлений Telegram
        try:
            params = {"timeout": 1}
            if last_update_id:
                params["offset"] = last_update_id + 1
            resp = requests.get(TELEGRAM_API_URL + "getUpdates", params=params, timeout=5)
            updates = resp.json().get("result", [])
            for update in updates:
                if 'message' in update:
                    msg = update['message']
                    chat_id = msg['chat']['id']
                    text = msg.get('text', "").strip() if msg.get('text') else ""
                    if not text:
                        continue
                    if text == "/start":
                        subscribers.add(chat_id)
                        send_message(chat_id, "✅ Вы подписались на уведомления о новых заявках.")
                    elif text == "/stop":
                        subscribers.discard(chat_id)
                        send_message(chat_id, "❎ Вы отписались от уведомлений.")
                    elif text == "/sutki":
                        handle_sutki_command(chat_id)
                    else:
                        send_message(chat_id, "ℹ️ Доступные команды: /start, /stop, /sutki")
            if updates:
                last_update_id = updates[-1]['update_id']
        except Exception as e:
            print(f"Telegram polling error: {e}")
        # 2. Опрос API ServiceDesk Plus по таймеру
        if time.time() - last_sdp_poll >= SDP_POLL_INTERVAL:
            last_sdp_poll = time.time()
            current_list = fetch_requests()
            if not current_list:
                continue
            current_map = {req.get("id"): req for req in current_list if req.get("id")}
            for rid, req in current_map.items():
                if rid not in known_requests:
                    # Обнаружена новая заявка
                    created_val = None
                    if req.get("created_time") and req["created_time"].get("value"):
                        try:
                            created_val = int(req["created_time"]["value"])
                        except:
                            created_val = None
                    known_requests[rid] = {
                        "id": rid,
                        "subject": req.get("subject"),
                        "requester": req.get("requester", {}),
                        "technician": req.get("technician"),
                        "status": req.get("status", {}),
                        "created_time": req.get("created_time", {}),
                        "created_time_value": created_val
                    }
                    # Отправляем уведомление о новой заявке всем подписчикам
                    message = format_request_message(req, is_new=True)
                    for chat_id in subscribers:
                        send_message(chat_id, message)
                else:
                    # Проверка изменений в известной заявке
                    known = known_requests[rid]
                    old_status = known.get("status", {}).get("name")
                    new_status = req.get("status", {}).get("name")
                    old_tech = known.get("technician", {}).get("name") if known.get("technician") else None
                    new_tech = req.get("technician", {}).get("name") if req.get("technician") else None
                    status_changed = (old_status != new_status)
                    tech_changed = (old_tech != new_tech)
                    if status_changed or tech_changed:
                        # Обновляем сохранённые данные заявки
                        known_requests[rid]["status"] = req.get("status", {})
                        known_requests[rid]["technician"] = req.get("technician")
                        diff_lines = []
                        # Изменение статуса
                        if status_changed:
                            diff_lines.append(f"Статус: {old_status} ➡️ {new_status}")
                            if new_status and new_status.lower().startswith("закры"):
                                # Если заявка закрыта, вычисляем время до закрытия
                                created_val = known_requests[rid].get("created_time_value")
                                if created_val:
                                    created_dt = datetime.fromtimestamp(created_val/1000.0)
                                    closed_dt = datetime.now()
                                    delta = closed_dt - created_dt
                                    hours = delta.days * 24 + delta.seconds // 3600
                                    minutes = (delta.seconds % 3600) // 60
                                    diff_lines.append(f"⏱️ Время до закрытия: {hours} ч {minutes} мин")
                        # Изменение назначения техника
                        if tech_changed:
                            old_name = old_tech if old_tech else "не назначен"
                            new_name = new_tech if new_tech else "не назначен"
                            diff_lines.append(f"Техник: {old_name} ➡️ {new_name}")
                            if old_tech is None and new_tech is not None:
                                # Если техник только что назначен – время реакции
                                created_val = known_requests[rid].get("created_time_value")
                                if created_val:
                                    created_dt = datetime.fromtimestamp(created_val/1000.0)
                                    assign_dt = datetime.now()
                                    delta = assign_dt - created_dt
                                    hours = delta.days * 24 + delta.seconds // 3600
                                    minutes = (delta.seconds % 3600) // 60
                                    diff_lines.append(f"⏱️ Время реакции: {hours} ч {minutes} мин")
                        # Отправляем уведомление об изменении заявки
                        diff_text = "\n".join(diff_lines)
                        update_message = (f"⚠️ Обновление заявки #{rid}\n"
                                          f"📌 Тема: {req.get('subject')}\n"
                                          f"{diff_text}\n"
                                          f"🔗 Открыть заявку: [Ссылка](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={rid})")
                        for chat_id in subscribers:
                            send_message(chat_id, update_message)
        # Небольшая задержка цикла
        time.sleep(TELEGRAM_POLL_INTERVAL)

# Запуск фонового потока бота
bot_thread = threading.Thread(target=bot_loop, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
