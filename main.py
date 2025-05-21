import requests
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import logging

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///requests.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Модель заявки
class Request(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200))
    author = db.Column(db.String(100))
    technician = db.Column(db.String(100))
    status = db.Column(db.String(50))
    created_time = db.Column(db.String(50))
    updated_time = db.Column(db.String(50))

    def get_changes(self, new_data):
        changes = []
        if self.subject != new_data['subject']:
            changes.append(f"📌 Тема: {self.subject} → {new_data['subject']}")
        if self.technician != new_data['technician']:
            changes.append(f"🔧 Назначено: {self.technician} → {new_data['technician']}")
        if self.status != new_data['status']:
            changes.append(f"⚙️ Статус: {self.status} → {new_data['status']}")
        return changes

# Отправка сообщения в Telegram
def send_telegram_message(chat_id, text):
    bot_token = 'YOUR_BOT_TOKEN'
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        logging.error(f"Ошибка отправки: {response.text}")

# Форматирование сообщений
def format_new_request(request):
    return (
        f"🆕 Новая заявка #{request.id}\n"
        f"📌 Тема: {request.subject}\n"
        f"👤 Автор: {request.author}\n"
        f"🔧 Назначено: {request.technician}\n"
        f"⚙️ Статус: {request.status}\n"
        f"📅 Дата создания: {request.created_time}\n"
        f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={request.id})"
    )

def format_update_message(request_id, changes):
    message = f"♻️ Обновлена заявка #{request_id}\n"
    for change in changes:
        message += f"{change}\n"
    message += f"🔗 [Открыть заявку](https://sd.sadykhan.kz/WorkOrder.do?woMode=viewWO&woID={request_id})"
    return message

# Обработка заявок
def process_requests(api_data, chat_id):
    for item in api_data:
        request_id = item['id']
        existing_request = Request.query.get(request_id)

        if not existing_request:
            # Новая заявка
            new_request = Request(
                id=request_id,
                subject=item['subject'],
                author=item['author'],
                technician=item['technician'],
                status=item['status'],
                created_time=item['created_time'],
                updated_time=item['updated_time']
            )
            db.session.add(new_request)
            db.session.commit()
            message = format_new_request(new_request)
            send_telegram_message(chat_id, message)
        else:
            # Проверка изменений
            changes = existing_request.get_changes(item)
            if changes:
                existing_request.subject = item['subject']
                existing_request.technician = item['technician']
                existing_request.status = item['status']
                existing_request.updated_time = item['updated_time']
                db.session.commit()
                message = format_update_message(request_id, changes)
                send_telegram_message(chat_id, message)

# Пример вызова
if __name__ == '__main__':
    db.create_all()
    # Пример данных из API
    api_data = [{
        'id': 829,
        'subject': 'Начальник отдела экспедиции Светлана Иннокентьевна на складе СФЛ не ожет зайти в программу 1С.',
        'author': 'Контролер3',
        'technician': 'Иван Иванов',  # Обновлено с "Не назначен"
        'status': 'В работе',         # Обновлено с "Открыто"
        'created_time': '27/03/2025 09:02 AM',
        'updated_time': '27/03/2025 09:10 AM'
    }]
    process_requests(api_data, 'YOUR_CHAT_ID')
