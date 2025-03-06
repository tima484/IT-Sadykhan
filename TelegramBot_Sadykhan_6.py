import requests
import time
from datetime import datetime, timedelta

# API-токен Telegram-бота
BOT_TOKEN = "8070102538:AAHXGz9P80XjSbW3pmDZK2iXwcn_9q6Nouk"

# API-ключ ServiceDesk Plus
SDP_API_KEY = "654D6008-14AA-4C89-8D48-0370953C713A"

# URL API ServiceDesk Plus
SDP_URL = "https://sd.sadykhan.kz/api/v3/requests"

# Храним обработанные заявки и ответственных
processed_requests = {}
subscribed_chats = set()

# Функция для отправки сообщений в Telegram
def send_telegram_message(chat_id, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        print(f"✅ Уведомление отправлено в {chat_id}: {message}")
    except requests.exceptions.Timeout:
        print("⚠️ Ошибка: Тайм-аут при отправке в Telegram.")
    except requests.RequestException as e:
        print(f"❌ Ошибка при отправке в Telegram: {e}")

# Функция для обработки команд от пользователей
def get_updates():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        updates = response.json()
        if "result" in updates:
            for update in updates["result"]:
                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                    text = update["message"].get("text", "")
                    if text == "/start" and chat_id not in subscribed_chats:
                        subscribed_chats.add(chat_id)
                        send_telegram_message(chat_id, "✅ Вы подписаны на уведомления о заявках.")
    except requests.RequestException as e:
        print(f"❌ Ошибка при получении обновлений: {e}")

# Функция для получения заявок за последний день из ServiceDesk Plus
def get_requests_for_today():
    headers = {
        "Authtoken": SDP_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    payload = {
        "list_info": {
            "row_count": 50  # Получаем до 50 заявок за день
        }
    }

    try:
        response = requests.get(SDP_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "requests" in data and len(data["requests"]) > 0:
            return data["requests"]  # Возвращаем все заявки
        else:
            print("⚠️ Нет новых заявок за сегодня в ServiceDesk Plus.")
    
    except requests.exceptions.Timeout:
        print("⚠️ Ошибка: Тайм-аут при запросе к ServiceDesk Plus.")
    except requests.RequestException as e:
        print(f"❌ Ошибка при запросе к ServiceDesk Plus: {e}")
    
    return []

# Основной бесконечный цикл проверки заявок
while True:
    get_updates()
    requests_today = get_requests_for_today()

    for request in requests_today:
        request_id = request.get("id")
        request_subject = request.get("subject", "Без темы")
        request_description = request.get("description", "Нет описания")
        requester_name = request.get("requester", {}).get("name", "Неизвестный автор")
        technician_info = request.get("technician", {}) or {}
        technician_name = technician_info.get("name", "Не назначен")
        created_time = request.get("created_time", {}).get("display_value", "")
        
        # Преобразуем дату заявки в datetime объект
        try:
            created_dt = datetime.strptime(created_time, "%d-%m-%Y %H:%M:%S")
        except ValueError:
            created_dt = datetime.utcnow()  # Если ошибка парсинга, используем текущее время
        
        if created_dt >= datetime.utcnow() - timedelta(days=1):
            if request_id not in processed_requests:
                message = (
                    f"🆕 <b>Новая заявка #{request_id}</b>\n"
                    f"👤 <b>Автор:</b> {requester_name}\n"
                    f"📌 <b>Тема:</b> {request_subject}\n"
                    f"📝 <b>Описание:</b> {request_description}\n"
                    f"🔧 <b>Ответственный:</b> {technician_name}"
                )
                for chat_id in subscribed_chats:
                    send_telegram_message(chat_id, message)
                processed_requests[request_id] = technician_name
            
            elif processed_requests[request_id] != technician_name:
                message = (
                    f"🔄 <b>Обновление заявки #{request_id}</b>\n"
                    f"🔧 <b>Новый ответственный:</b> {technician_name}"
                )
                for chat_id in subscribed_chats:
                    send_telegram_message(chat_id, message)
                processed_requests[request_id] = technician_name
    
    # Проверяем заявки каждые 60 секунд
    time.sleep(60)
