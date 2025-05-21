import os
import telebot
import requests
import json
from flask import Flask, request as flask_request
import threading
import time
import logging

# Настройка логирования для отладки на Railway
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SDP_TOKEN = os.getenv("SDP_API_KEY", "").strip()
SDP_URL = os.getenv("SDP_URL", "").strip()  # Исправлено: пустая строка по умолчанию

# Попытка получить публичный домен от Railway
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
if not BASE_URL:
    logging.warning("RAILWAY_PUBLIC_DOMAIN не установлен. Переход в режим опроса.")
    USE_WEBHOOK = False
else:
    WEBHOOK_URL = f"https://{BASE_URL}/bot{BOT_TOKEN}"
    USE_WEBHOOK = True
    logging.info(f"Webhook URL: {WEBHOOK_URL}")

# Проверка токенов
if not BOT_TOKEN or ':' not in BOT_TOKEN:
    logging.error("Недействительный BOT_TOKEN. Проверьте переменные окружения в Railway.")
    raise ValueError("Недействительный BOT_TOKEN. Он должен содержать двоеточие и быть установлен в переменных Railway.")
if not SDP_TOKEN:
    logging.error("SDP_API_KEY не найден или пуст в переменных окружения.")
    raise ValueError("SDP_API_KEY не найден или пуст в переменных окружения.")
if not SDP_URL:
    logging.error("SDP_URL не установлен в переменных окружения.")
    raise ValueError("SDP_URL должен быть установлен в переменных окружения Railway.")

logging.info(f"BOT_TOKEN загружен: {BOT_TOKEN[:10]}...")
logging.info(f"SDP_API_KEY загружен: {SDP_TOKEN[:5]}...")

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='Markdown')

# Множество подписчиков (chat_id пользователей)
subscribers = set()
# Словарь известных заявок: id -> {fields...}
known_requests = {}
# Время запуска (мс с эпохи) для отслеживания новых/старых заявок
startup_time_ms = int(time.time() * 1000)

# Функция экранирования специальных символов Markdown в тексте
def escape_md(text):
    if text is None:
        return ''
    for ch in ['*', '_', '`', '[', ']']:
        text = text.replace(ch, f"\\{ch}")
    return text

# Функция форматирования длительности (мс -> строка вида "X д Y ч Z мин")
def format_duration(ms):
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    seconds_rem = seconds % 60
    minutes_rem = minutes % 60
    hours_rem = hours % 24
    result_parts = []
    if days > 0:
        result_parts.append(f"{days} д")
    if hours_rem > 0:
        result_parts.append(f"{hours_rem} ч")
    if minutes_rem > 0:
        result_parts.append(f"{minutes_rem} мин")
    return " ".join(result_parts)

# Функция для выполнения запроса к API ServiceDesk Plus
def fetch_requests(list_info):
    try:
        payload = {"input_data": list_info}
        headers = {
            'authtoken': SDP_TOKEN,
            'Content-Type': 'application/json'
        }
        logging.debug(f"Отправка запроса к SDP API с заголовками: {headers}")
        logging.debug(f"Тело запроса: {json.dumps(payload, ensure_ascii=False, indent=2)}")
        response = requests.post(SDP_URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        logging.debug(f"Ответ SDP API: {data}")
        return data.get('requests', [])
    except Exception as e:
        logging.error(f"Ошибка при запросе к SDP API: {e}")
        if isinstance(e, requests.exceptions.HTTPError):
            logging.error(f"Содержимое ответа: {e.response.text}")
        return []

# Первоначальная загрузка существующих заявок (не закрытых)
def load_initial_requests():
    list_info = {
        "list_info": {
            "row_count": 100,
            "start_index": 1,
            "search_criteria": [
                {
                    "field": "status.name",
                    "condition": "is not",
                    "value": "Закрыто"
                }
            ]
        }
    }
    start_index = 1
    while True:
        list_info["list_info"]["start_index"] = start_index
        requests_batch = fetch_requests(list_info)
        if not requests_batch:
            break
        for req in requests_batch:
            req_id = str(req.get('id', ''))
            if not req_id:
                continue
            tech = req.get('technician')
            tech_name = tech.get('name') if tech and tech.get('name') else "не назначен"
            status_name = req.get('status', {}).get('name', '')
            known_requests[req_id] = {
                "subject": req.get('subject', ''),
                "author": req.get('requester', {}).get('name', ''),
                "tech": tech_name,
                "status": status_name,
                "created_time": req.get('created_time', {}).get('value', 0),
                "assigned_time": req.get('assigned_time', {}).get('value'),
                "resolved_time": req.get('resolved_time', {}).get('value'),
                "completed_time": req.get('completed_time', {}).get('value')
            }
        if len(requests_batch) < list_info["list_info"]["row_count"]:
            break
        start_index += len(requests_batch)
    logging.info(f"Начальная загрузка: {len(known_requests)} заявок отслеживается.")

# Фоновая функция опроса API каждые 60 секунд
def poll_sdp():
    last_check = startup_time_ms
    while True:
        now_ms = int(time.time() * 1000)
        list_info = {
            "list_info": {
                "row_count": 100,
                "search_criteria": [
                    {
                        "field": "last_updated_time",
                        "condition": "greater than",
                        "value": str(last_check)
                    }
                ]
            }
        }
        updates = fetch_requests(list_info)
        last_check = now_ms
        for req in updates:
            req_id = str(req.get('id', ''))
            if not req_id:
                continue
            subject = req.get('subject', '')
            author = req.get('requester', {}).get('name', '')
            status_name = req.get('status', {}).get('name', '')
            tech = req.get('technician')
            tech_name = tech.get('name') if tech and tech.get('name') else "не назначен"
            created_val = req.get('created_time', {}).get('value')
            created_disp = req.get('created_time', {}).get('display_value', '')
            assigned_val = req.get('assigned_time', {}).get('value') if req.get('assigned_time') else None
            resolved_val = req.get('resolved_time', {}).get('value') if req.get('resolved_time') else None
            completed_val = req.get('completed_time', {}).get('value') if req.get('completed_time') else None

            if req_id not in known_requests:
                if created_val and created_val < startup_time_ms:
                    old_status = "Закрыто" if (completed_val and status_name != "Закрыто") else "неизвестно"
                    old_tech = "не назначен"
                    changes = []
                    if old_status != status_name:
                        changes.append(f"Статус: {escape_md(old_status)} → {escape_md(status_name)}")
                    if tech_name != old_tech:
                        changes.append(f"Техник: {old_tech} → {escape_md(tech_name)}")
                        if created_val:
                            assign_time = assigned_val or now_ms
                            reaction_ms = int(assign_time) - int(created_val)
                            changes.append(f"Время реакции: {format_duration(reaction_ms)}")
                    if status_name == "Закрыто":
                        if created_val:
                            close_time = completed_val or resolved_val or now_ms
                            closure_ms = int(close_time) - int(created_val)
                            changes.append(f"Время до закрытия: {format_duration(closure_ms)}")
                    if changes:
                        msg_lines = [
                            "♻️ Обновлена заявка",
                            f"📌 Тема: {escape_md(subject)}",
                            *changes,
                            f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={req_id})"
                        ]
                        msg_text = "\n".join(msg_lines)
                        for chat_id in list(subscribers):
                            try:
                                bot.send_message(chat_id, msg_text)
                            except Exception as e:
                                logging.error(f"Ошибка отправки обновления на {chat_id}: {e}")
                    known_requests[req_id] = {
                        "subject": subject, "author": author,
                        "tech": tech_name, "status": status_name,
                        "created_time": created_val or 0,
                        "assigned_time": assigned_val,
                        "resolved_time": resolved_val,
                        "completed_time": completed_val
                    }
                else:
                    msg_text = (
                        "🆕 Новая заявка\n"
                        f"📌 Тема: {escape_md(subject)}\n"
                        f"👤 Автор: {escape_md(author)}\n"
                        f"🔧 Назначено: {escape_md(tech_name)}\n"
                        f"⚙️ Статус: {escape_md(status_name)}\n"
                        f"📅 Дата создания: {escape_md(created_disp)}\n"
                        f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={req_id})"
                    )
                    for chat_id in list(subscribers):
                        try:
                            bot.send_message(chat_id, msg_text)
                        except Exception as e:
                            logging.error(f"Ошибка отправки новой заявки на {chat_id}: {e}")
                    known_requests[req_id] = {
                        "subject": subject, "author": author,
                        "tech": tech_name, "status": status_name,
                        "created_time": created_val or 0,
                        "assigned_time": assigned_val,
                        "resolved_time": resolved_val,
                        "completed_time": completed_val
                    }
            else:
                old = known_requests[req_id]
                changes = []
                if status_name != old['status']:
                    changes.append(f"Статус: {escape_md(old['status'])} → {escape_md(status_name)}")
                if tech_name != old['tech']:
                    if old['tech'] == "не назначен" and tech_name != "не назначен":
                        changes.append(f"Техник: не назначен → {escape_md(tech_name)}")
                        assign_time = assigned_val or now_ms
                        try:
                            react_ms = int(assign_time) - int(old['created_time'])
                        except:
                            react_ms = 0
                        changes.append(f"Время реакции: {format_duration(react_ms)}")
                    else:
                        changes.append(f"Техник: {escape_md(old['tech'])} → {escape_md(tech_name)}")
                if status_name == "Закрыто" and old['status'] != "Закрыто":
                    if old.get('created_time'):
                        close_time = completed_val or resolved_val or now_ms
                        try:
                            closure_ms = int(close_time) - int(old['created_time'])
                        except:
                            closure_ms = 0
                        changes.append(f"Время до закрытия: {format_duration(closure_ms)}")
                if changes:
                    msg_lines = [
                        "♻️ Обновлена заявка",
                        f"📌 Тема: {escape_md(subject)}",
                        *changes,
                        f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={req_id})"
                    ]
                    msg_text = "\n".join(msg_lines)
                    for chat_id in list(subscribers):
                        try:
                            bot.send_message(chat_id, msg_text)
                        except Exception as e:
                            logging.error(f"Ошибка отправки обновления на {chat_id}: {e}")
                old['status'] = status_name
                old['tech'] = tech_name
                old['assigned_time'] = assigned_val or old.get('assigned_time')
                old['resolved_time'] = resolved_val or old.get('resolved_time')
                old['completed_time'] = completed_val or old.get('completed_time')
        time.sleep(60)

# Обработчик команды /start
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    subscribers.add(chat_id)
    bot.send_message(chat_id, "✅ Вы подписаны на уведомления о заявках ServiceDesk Plus.")

# Обработчик команды /stop
@bot.message_handler(commands=['stop'])
def cmd_stop(message):
    chat_id = message.chat.id
    subscribers.discard(chat_id)
    bot.send_message(chat_id, "❌ Вы отписаны от уведомлений ServiceDesk Plus.")

# Обработчик команды /sutki (24 часа)
@bot.message_handler(commands=['sutki'])
def cmd_sutki(message):
    chat_id = message.chat.id
    since_ms = int(time.time() * 1000) - 24*60*60*1000
    list_info = {
        "list_info": {
            "row_count": 100,
            "search_criteria": [
                {
                    "field": "created_time",
                    "condition": "greater than or equal to",
                    "value": str(since_ms)
                }
            ],
            "sort_field": "created_time",
            "sort_order": "asc"
        }
    }
    recent_reqs = fetch_requests(list_info)
    if not recent_reqs:
        bot.send_message(chat_id, "❕ За последние 24 часа заявок не найдено.")
        return
    header = "*Заявки за последние 24 часа:*\\n\\n"
    result_text = header
    messages = []
    for req in recent_reqs:
        subject = req.get('subject', '')
        author = req.get('requester', {}).get('name', '')
        status_name = req.get('status', {}).get('name', '')
        tech = req.get('technician')
        tech_name = tech.get('name') if tech and tech.get('name') else "не назначен"
        created_disp = req.get('created_time', {}).get('display_value', '')
        req_id = req.get('id', '')
        entry = (
            f"📌 Тема: {escape_md(subject)}\n"
            f"👤 Автор: {escape_md(author)}\n"
            f"🔧 Назначено: {escape_md(tech_name)}\n"
            f"⚙️ Статус: {escape_md(status_name)}\n"
            f"📅 Дата создания: {escape_md(created_disp)}\n"
            f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={req_id})\n\n"
        )
        if len(result_text) + len(entry) > 4000:
            messages.append(result_text)
            result_text = ""
        result_text += entry
    if result_text:
        messages.append(result_text)
    for text in messages:
        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения sutki: {e}")
            bot.send_message(chat_id, text, parse_mode=None)

# Flask-приложение для Railway
app = Flask(__name__)

@app.route('/')
def index():
    return "ServiceDesk Plus Telegram Bot is running.", 200

# Эндпоинт для обработки вебхуков от Telegram (если используется)
@app.route(f'/bot{BOT_TOKEN}', methods=['POST'])
def webhook():
    if flask_request.headers.get('content-type') == 'application/json':
        json_string = flask_request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        return '', 403

# --- Запуск потоков и настройка ---
if __name__ == "__main__":
    # Загружаем начальные заявки
    load_initial_requests()

    # Запускаем поток для опроса SDP API
    sdp_thread = threading.Thread(target=poll_sdp)
    sdp_thread.daemon = True
    sdp_thread.start()

    # Выбор режима работы
    if USE_WEBHOOK:
        # Удаляем существующий вебхук (на случай, если он был установлен ранее)
        bot.remove_webhook()
        time.sleep(1)  # Даём время на удаление

        # Устанавливаем новый вебхук
        webhook_response = bot.set_webhook(url=WEBHOOK_URL)
        if webhook_response:
            logging.info(f"Webhook успешно установлен: {WEBHOOK_URL}")
        else:
            logging.error("Не удалось установить вебхук.")
            raise ValueError("Не удалось установить вебхук. Проверьте WEBHOOK_URL и доступность сети.")
    else:
        logging.warning("Работа в режиме опроса из-за отсутствия RAILWAY_PUBLIC_DOMAIN.")
        bot_thread = threading.Thread(target=lambda: bot.polling(none_stop=True, timeout=60))
        bot_thread.daemon = True
        bot_thread.start()

    # Запускаем Flask
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
