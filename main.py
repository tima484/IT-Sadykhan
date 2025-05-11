import telebot
import requests
from flask import Flask, request as flask_request
import threading
import time

# === Конфигурация ===
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"        # токен Telegram-бота
SDP_TOKEN = "YOUR_SDP_AUTH_TOKEN"            # authtoken для ServiceDesk Plus API
SDP_URL   = "https://sd.sadykhan.kz/api/v3/requests"  # Endpoint API v3 заявок

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
    hours   = minutes // 60
    days    = hours // 24
    seconds_rem = seconds % 60
    minutes_rem = minutes % 60
    hours_rem   = hours % 24
    result_parts = []
    if days > 0:
        result_parts.append(f"{days} д")
    if hours_rem > 0:
        result_parts.append(f"{hours_rem} ч")
    if minutes_rem > 0:
        result_parts.append(f"{minutes_rem} мин")
    # секундную точность не указываем, если прошло >= 1 мин
    return " ".join(result_parts)

# Функция для выполнения запроса к API ServiceDesk Plus с заданными параметрами
def fetch_requests(input_data):
    try:
        headers = {'authtoken': SDP_TOKEN}
        response = requests.post(SDP_URL, headers=headers, json=input_data, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('requests', [])
    except Exception as e:
        print(f"Ошибка при запросе к SDP API: {e}")
        return []

# Первоначальная загрузка существующих заявок (не закрытых) чтобы не дублировать уведомления
def load_initial_requests():
    # Получаем все заявки со статусом не "Закрыто"
    input_data = {
        "list_info": {
            "fields_required": [
                "id", "subject", "requester", "technician", "status",
                "created_time", "assigned_time", "resolved_time", "completed_time"
            ],
            "search_criteria": {
                "field": "status.name",
                "condition": "is not",
                "values": ["Закрыто"]
            },
            "start_index": 1,
            "row_count": 100
        }
    }
    start_index = 1
    while True:
        input_data["list_info"]["start_index"] = start_index
        requests_batch = fetch_requests(input_data)
        if not requests_batch:
            break
        # Сохраняем каждую заявку в словарь известных без отправки уведомления
        for req in requests_batch:
            req_id = str(req.get('id', ''))
            if not req_id:
                continue
            # Текущий техник или "не назначен"
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
        # Проверяем, вернулось ли меньше row_count записей (значит, это последняя страница)
        if len(requests_batch) < input_data["list_info"]["row_count"]:
            break
        start_index += len(requests_batch)
    print(f"Initial load: {len(known_requests)} requests tracked.")

# Фоновая функция опроса API каждые 60 секунд
def poll_sdp():
    last_check = startup_time_ms  # метка времени последней проверки (мс)
    while True:
        now_ms = int(time.time() * 1000)
        # Подготовка фильтра: заявки, обновлённые после last_check
        input_data = {
            "list_info": {
                "fields_required": [
                    "id", "subject", "requester", "technician", "status",
                    "created_time", "assigned_time", "resolved_time", "completed_time", "last_updated_time"
                ],
                "search_criteria": {
                    "field": "last_updated_time",
                    "condition": "greater than",
                    "value": str(last_check)
                },
                "row_count": 100
            }
        }
        updates = fetch_requests(input_data)
        last_check = now_ms  # обновляем метку времени сразу после запроса
        # Обрабатываем каждую полученную заявку
        for req in updates:
            req_id = str(req.get('id', ''))
            if not req_id:
                continue
            subject = req.get('subject', '')
            author  = req.get('requester', {}).get('name', '')
            # Текущий статус и техник
            status_name = req.get('status', {}).get('name', '')
            tech = req.get('technician')
            tech_name = tech.get('name') if tech and tech.get('name') else "не назначен"
            # Метки времени (в мс) для вычислений
            created_val   = req.get('created_time', {}).get('value')
            created_disp  = req.get('created_time', {}).get('display_value', '')
            assigned_val  = req.get('assigned_time', {}).get('value') if req.get('assigned_time') else None
            resolved_val  = req.get('resolved_time', {}).get('value') if req.get('resolved_time') else None
            completed_val = req.get('completed_time', {}).get('value') if req.get('completed_time') else None

            if req_id not in known_requests:
                # Новая заявка (создана после старта бота) или обновление ранее закрытой (неизвестной) заявки
                if created_val and created_val < startup_time_ms:
                    # Заявка существовала до запуска бота (например, была закрыта и теперь обновлена/переоткрыта).
                    # Постараемся отправить только соответствующие изменения.
                    old_status = "Закрыто" if (completed_val and status_name != "Закрыто") else "неизвестно"
                    old_tech   = "не назначен"
                    changes = []
                    if old_status != status_name:
                        changes.append(f"Статус: {escape_md(old_status)} → {escape_md(status_name)}")
                    # Если техник сейчас назначен, а ранее считался "не назначен"
                    if tech_name != old_tech:
                        changes.append(f"Техник: {old_tech} → {escape_md(tech_name)}")
                        if created_val:
                            # Время реакции (приблизительно, т.к. точное старое значение неизвестно)
                            assign_time = assigned_val or now_ms
                            reaction_ms = int(assign_time) - int(created_val)
                            changes.append(f"Время реакции: {format_duration(reaction_ms)}")
                    # Если заявка теперь закрыта
                    if status_name == "Закрыто":
                        if created_val:
                            close_time = completed_val or resolved_val or now_ms
                            closure_ms = int(close_time) - int(created_val)
                            changes.append(f"Время до закрытия: {format_duration(closure_ms)}")
                    # Отправляем уведомление об обновлении, если есть что сообщить
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
                                print(f"Error sending update to {chat_id}: {e}")
                    # Добавляем в известные (с текущим состоянием) без отметки как "новая"
                    known_requests[req_id] = {
                        "subject": subject, "author": author,
                        "tech": tech_name, "status": status_name,
                        "created_time": created_val or 0,
                        "assigned_time": assigned_val, 
                        "resolved_time": resolved_val,
                        "completed_time": completed_val
                    }
                else:
                    # Новая заявка (создана после запуска бота)
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
                            print(f"Error sending new ticket to {chat_id}: {e}")
                    known_requests[req_id] = {
                        "subject": subject, "author": author,
                        "tech": tech_name, "status": status_name,
                        "created_time": created_val or 0,
                        "assigned_time": assigned_val, 
                        "resolved_time": resolved_val,
                        "completed_time": completed_val
                    }
            else:
                # Обновление уже известной заявки
                old = known_requests[req_id]
                changes = []
                # Проверка смены статуса
                if status_name != old['status']:
                    changes.append(f"Статус: {escape_md(old['status'])} → {escape_md(status_name)}")
                # Проверка смены/назначения техника
                if tech_name != old['tech']:
                    if old['tech'] == "не назначен" and tech_name != "не назначен":
                        changes.append(f"Техник: не назначен → {escape_md(tech_name)}")
                        # вычисляем время реакции
                        assign_time = assigned_val or now_ms
                        try:
                            react_ms = int(assign_time) - int(old['created_time'])
                        except:
                            react_ms = 0
                        changes.append(f"Время реакции: {format_duration(react_ms)}")
                    else:
                        changes.append(f"Техник: {escape_md(old['tech'])} → {escape_md(tech_name)}")
                # Проверка закрытия заявки
                if status_name == "Закрыто" and old['status'] != "Закрыто":
                    if old.get('created_time'):
                        close_time = completed_val or resolved_val or now_ms
                        try:
                            closure_ms = int(close_time) - int(old['created_time'])
                        except:
                            closure_ms = 0
                        changes.append(f"Время до закрытия: {format_duration(closure_ms)}")
                # Отправка уведомления об изменениях
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
                            print(f"Error sending update to {chat_id}: {e}")
                # Обновляем сохранённое состояние заявки
                old['status'] = status_name
                old['tech'] = tech_name
                old['assigned_time'] = assigned_val or old.get('assigned_time')
                old['resolved_time'] = resolved_val or old.get('resolved_time')
                old['completed_time'] = completed_val or old.get('completed_time')
        # Пауза до следующего опроса
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
    # Формируем критерий: заявки за последние 24 часа
    since_ms = int(time.time() * 1000) - 24*60*60*1000
    input_data = {
        "list_info": {
            "fields_required": [
                "id", "subject", "requester", "technician", "status", "created_time"
            ],
            "search_criteria": {
                "field": "created_time",
                "condition": "greater or equal",
                "value": str(since_ms)
            },
            "sort_field": "created_time",
            "sort_order": "ascending",
            "row_count": 100
        }
    }
    recent_reqs = fetch_requests(input_data)
    if not recent_reqs:
        bot.send_message(chat_id, "❕ За последние 24 часа заявок не найдено.")
        return
    # Компонуем сообщение со списком заявок
    header = "*Заявки за последние 24 часа:*\\n\\n"
    result_text = header
    messages = []
    for req in recent_reqs:
        subject = req.get('subject', '')
        author  = req.get('requester', {}).get('name', '')
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
        # Если текущее сообщение станет слишком длинным, отправляем его и начинаем новое
        if len(result_text) + len(entry) > 4000:
            messages.append(result_text)
            result_text = ""
        result_text += entry
    # Добавляем последний собранный блок
    if result_text:
        messages.append(result_text)
    # Отправляем блоки сообщений пользователю
    for text in messages:
        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            # В случае ошибки форматирования - отправляем без parse_mode
            print(f"Error sending sutki message: {e}")
            bot.send_message(chat_id, text, parse_mode=None)

# --- Запуск потоков для бота и опроса API ---
# Загружаем начальные данные заявок
load_initial_requests()

# Поток бота Telegram (polling)
bot_thread = threading.Thread(target=lambda: bot.polling(none_stop=True, timeout=60))
bot_thread.daemon = True
bot_thread.start()

# Поток опроса ServiceDesk Plus
sdp_thread = threading.Thread(target=poll_sdp)
sdp_thread.daemon = True
sdp_thread.start()

# Flask-приложение (для запуска веб-сервера)
app = Flask(__name__)

@app.route('/')
def index():
    return "ServiceDesk Plus Telegram Bot is running.", 200

# Запуск Flask на порту 8080
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
