from datetime import datetime
import config
import pytz
import re

def get_msk_time():
    """Возвращает текущее время и дату в МСК"""
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    return {
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%d.%m.%Y (%A)")
    }

def normalize_phone(phone_text):
    """
    Приводит номер к формату 7XXXXXXXXXX.
    Если не получилось, возвращает только цифры.
    """
    digits = re.sub(r'\D', '', phone_text)
    
    # Если начинается с 8 и длина 11, меняем 8 на 7
    if len(digits) == 11 and digits.startswith('8'):
        return '7' + digits[1:]
    
    # Если длина 10 (978...), добавляем 7
    if len(digits) == 10:
        return '7' + digits
        
    return digits

def validate_phone_number(phone_text):
    """
    Проверяет, похож ли текст на номер телефона.
    """
    digits = re.sub(r'\D', '', phone_text)
    # Проверка длины (7-15 цифр)
    if 7 <= len(digits) <= 15:
        return True
    return False