# ai_client.py
import requests
import json
import re
import logging
from config import GROQ_API_KEY, CLINIC_SYSTEM_PROMPT_TEMPLATE
from utils import get_msk_time, validate_phone_number

logger = logging.getLogger(__name__)

def get_groq_response(user_message, chat_history=[]):
    """
    Основная функция общения с Groq API.
    1. Подставляет текущее МСК время в промпт.
    2. Отправляет историю диалога.
    3. Форсирует ответ в формате JSON Object для надежности.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    # 1. Генерация динамического промпта со временем
    time_data = get_msk_time()
    dynamic_system_prompt = CLINIC_SYSTEM_PROMPT_TEMPLATE.replace(
        "{current_time}", time_data['time']
    ).replace(
        "{current_date}", time_data['date']
    )

    # 2. Сборка истории сообщений
    # Системное сообщение (всегда первое)
    messages = [{"role": "system", "content": dynamic_system_prompt}]
    
    # Добавляем хвост истории (последние 20 сообщений для глубокого контекста)
    messages.extend(chat_history[-20:]) 
    
    # Текущий запрос пользователя
    messages.append({"role": "user", "content": user_message})

    # 3. Параметры модели
    data = {
        "model": "llama-3.3-70b-versatile", # Мощнейшая модель на сегодня
        "messages": messages,
        "temperature": 0.5, # Баланс между креативом и точностью
        "max_completion_tokens": 1024,
        "response_format": {"type": "json_object"} # ЖЕСТКОЕ требование JSON
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=25)
        
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']
            return content
        else:
            logger.error(f"⚠️ Ошибка API Groq: {response.status_code} - {response.text}")
            return "Извините, сервер клиники сейчас перегружен. Повторите ваш вопрос через секунду."
            
    except Exception as e:
        logger.error(f"❌ Критическая ошибка сети: {e}")
        return "Проблема со связью. Пожалуйста, попробуйте позже."

def parse_server_response(ai_text):
    """
    Парсер ответа нейросети.
    Ожидает, что ответ может быть:
    1. С префиксом Wait_DATA_JSON: {...}
    2. Чистым JSON: {...}
    3. Обычным текстом (резервный вариант)
    """
    data = None
    
    # 1. Попытка найти блок с префиксом Wait_DATA_JSON
    json_match = re.search(r'Wait_DATA_JSON:\s*(\{.*?\})', ai_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Если префикса нет, пробуем распарсить весь текст как JSON
    if not data:
        try:
            # Находим первую открывающую и последнюю закрывающую скобки
            # Это помогает, если вокруг JSON есть какой-то мусор
            json_candidate_match = re.search(r'(\{.*\})', ai_text, re.DOTALL)
            if json_candidate_match:
                data = json.loads(json_candidate_match.group(1))
            else:
                # Попытка "в лоб"
                data = json.loads(ai_text)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Если удалось извлечь структуру (dict), обрабатываем её
    if data and isinstance(data, dict):
        return process_json_action(data)
    
    # 3. Сценарий: Это просто текст (или парсинг не удался)
    clean_text = ai_text.replace("Wait_DATA_JSON:", "").strip()
    
    # Защита от пустого ответа
    if not clean_text:
        clean_text = "Извините, я задумался. Повторите, пожалуйста."
        
    return 'TEXT', None, clean_text


def process_json_action(data):
    """
    Логика принятия решений на основе JSON от нейросети.
    """
    # Если ключа action нет, считаем это просто текстом
    action = data.get('action', 'text')
    
    # --- 1. ПРОСТО ТЕКСТ (Разговор) ---
    if action == 'text':
        # Нейросеть может вернуть текст в полях 'text', 'message' или 'response'
        # Мы ищем во всех, чтобы не потерять ответ
        message_text = data.get('text') or data.get('message') or data.get('response')
        
        if not message_text:
            message_text = "..." # Заглушка, если поле пустое
            
        return 'TEXT', None, message_text

    # --- 2. ЗАПИСЬ НА ПРИЕМ ---
    elif action == 'booking':
        # Валидация: проверяем наличие критических данных
        if not data.get('name') or not data.get('surname'):
            return 'TEXT', None, "😁 Для записи мне нужны ваше Имя и Фамилия. Напишите их, пожалуйста!"
        
        phone = data.get('phone')
        if not phone:
            return 'TEXT', None, "Напишите ваш контактный номер телефона 📱"
        
        if not validate_phone_number(phone):
             return 'TEXT', None, "Пожалуйста, введите корректный номер телефона (например, +7 978 ...)."
        
        # Если всё есть
        return 'BOOKING', data, "Данные приняты. Формирую заявку... ⏳"
        
    # --- 3. СБРОС ТЕКУЩЕГО ДИАЛОГА ---
    elif action == 'cancel':
        return 'CANCEL', None, "Хорошо, сбрасываю наш диалог."

    # --- 4. ОТМЕНА СУЩЕСТВУЮЩЕЙ ЗАПИСИ ---
    elif action == 'cancel_record':
        if not data.get('phone'):
            return 'TEXT', None, "Чтобы найти вашу запись и отменить её, напишите номер телефона, на который оформляли бронь."
        
        return 'CANCEL_RECORD', data, "Ищу вашу запись в базе..."
        
    # --- 5. НЕИЗВЕСТНОЕ ДЕЙСТВИЕ ---
    # Если пришел какой-то странный JSON, просто пытаемся вывести его текст
    fallback_text = data.get('text') or data.get('message') or "Я вас слушаю."
    return 'TEXT', None, fallback_text