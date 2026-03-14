import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import SPREADSHEET_NAME, JSON_KEY_FILE
from datetime import datetime
import os
import logging
import time
from utils import normalize_phone

logger = logging.getLogger(__name__)

# Заголовки столбцов
HEADERS = ["Дата создания", "Имя", "Фамилия", "Телефон", "Желаемое время", "Причина", "Ник Telegram", "Статус"]

# Глобальная переменная для кэширования клиента
_client = None

def get_client(force_refresh=False):
    """
    Авторизация в Google API.
    Использует кэшированный клиент, если он есть.
    force_refresh=True заставляет пересоздать соединение.
    """
    global _client
    
    if _client and not force_refresh:
        return _client

    if not os.path.exists(JSON_KEY_FILE):
        logger.error(f"Service key file not found: {JSON_KEY_FILE}")
        return None
    
    try:
        logger.info("🔄 Authenticating with Google Sheets API...")
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_KEY_FILE, scope)
        _client = gspread.authorize(creds)
        logger.info("✅ Google Sheets authenticated successfully.")
        return _client
    except Exception as e:
        logger.error(f"Failed to authorize Google Sheets: {e}")
        _client = None
        return None

def init_sheet(sheet):
    """Проверяет заголовки"""
    try:
        if not sheet.row_values(1):
            sheet.append_row(HEADERS)
    except Exception as e:
        logger.error(f"⚠️ Ошибка инициализации таблицы: {e}")

def save_booking_to_sheets(data_dict, username):
    """Сохраняет новую запись (с одной попыткой повтора при ошибке)"""
    # Нормализуем телефон перед сохранением
    if 'phone' in data_dict:
        data_dict['phone'] = normalize_phone(data_dict['phone'])

    for attempt in range(2):
        client = get_client(force_refresh=(attempt > 0)) # Сброс кэша при повторе
        if not client:
            return False

        try:
            try:
                sheet = client.open(SPREADSHEET_NAME).sheet1
            except gspread.SpreadsheetNotFound:
                logger.error(f"❌ Таблица '{SPREADSHEET_NAME}' не найдена!")
                return False

            if attempt == 0:
                init_sheet(sheet) # Инициализируем только в первый раз

            timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
            row = [
                timestamp,
                data_dict.get('name', '-'),
                data_dict.get('surname', '-'),
                data_dict.get('phone', '-'),
                data_dict.get('date', 'Не указано'),
                data_dict.get('reason', 'Осмотр'),
                f"@{username}" if username else "Скрыт",
                "НОВАЯ"
            ]
            sheet.append_row(row)
            # 🔒 Privacy: Do not log PII
            logger.info(f"Booking saved to Sheets.")
            return True

        except Exception as e:
            logger.warning(f"⚠️ Ошибка записи в Google Sheets (попытка {attempt+1}): {e}")
            if attempt == 1:
                logger.error("❌ Не удалось сохранить запись после повторной попытки.")
                return False
            time.sleep(1) # Ждем перед повтором

    return False

def cancel_booking_by_phone(phone):
    """
    Ищет запись по телефону и меняет статус на 'ОТМЕНА'.
    Возвращает: (Успех, Инфо_о_клиенте)
    """
    search_phone = normalize_phone(phone)
    if not search_phone:
        return False, "Некорректный номер."

    for attempt in range(2):
        client = get_client(force_refresh=(attempt > 0))
        if not client:
             if attempt == 1: return False, "Ошибка доступа к базе."
             continue

        try:
            sheet = client.open(SPREADSHEET_NAME).sheet1
            all_values = sheet.get_all_values()
            
            target_row_index = -1
            client_info = "Неизвестный"
            
            # Идем с конца списка к началу
            for i in range(len(all_values) - 1, 0, -1):
                row = all_values[i]
                if len(row) > 3:
                    # Нормализуем телефон из таблицы для сравнения
                    row_phone = normalize_phone(row[3])
                    
                    # Сравниваем точное совпадение нормализованных номеров
                    # или если один входит в другой (на случай 7978... и 8978...)
                    if search_phone in row_phone or row_phone in search_phone:
                        current_status = row[7] if len(row) > 7 else ""
                        if "ОТМЕНА" not in current_status:
                            target_row_index = i + 1
                            client_info = f"{row[1]} {row[2]}"
                            break
            
            if target_row_index != -1:
                sheet.update_cell(target_row_index, 8, "❌ ОТМЕНА КЛИЕНТОМ")
                # 🔒 Privacy: Masked log
                masked_phone = f"***{phone[-4:]}" if len(phone) > 4 else "***"
                logger.info(f"Booking cancelled by phone ending in {masked_phone}")
                return True, client_info
            else:
                return False, "Запись с таким номером не найдена или уже отменена."
        
        except Exception as e:
             logger.warning(f"⚠️ Ошибка отмены в Google Sheets (попытка {attempt+1}): {e}")
             if attempt == 1:
                 return False, "Ошибка базы данных."
             time.sleep(1)
             
    return False, "Неизвестная ошибка."