# main.py
import telebot
from telebot import types
import config
from ai_client import get_groq_response, parse_server_response
from sheets_client import save_booking_to_sheets, cancel_booking_by_phone
import time
import logging
import threading
from logging.handlers import TimedRotatingFileHandler
from state_manager import StateManager

# ===========================
# 🔧 НАСТРОЙКА ЛОГИРОВАНИЯ
# ===========================
# Ротация логов: каждый день (midnight), храним 7 дней
log_handler = TimedRotatingFileHandler("bot.log", when="midnight", interval=1, backupCount=7, encoding='utf-8')
log_handler.suffix = "%Y-%m-%d"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        log_handler,
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация
bot = telebot.TeleBot(config.BOT_TOKEN)
state_manager = StateManager()

SESSION_TIMEOUT = 1800 # 30 минут

# ===========================
# 🔘 КЛАВИАТУРЫ (ИНТЕРФЕЙС)
# ===========================
def main_menu():
    """Главное меню внизу экрана"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton("📞 Контакты")
    btn2 = types.KeyboardButton("🗺 Маршрут")
    btn3 = types.KeyboardButton("💰 Цены и Услуги")
    btn4 = types.KeyboardButton("🔄 Сброс диалога")
    markup.add(btn1, btn2, btn3, btn4)
    return markup

def confirm_keyboard():
    """Инлайн-кнопки под карточкой записи"""
    markup = types.InlineKeyboardMarkup()
    btn_yes = types.InlineKeyboardButton("✅ Всё верно, подтверждаю", callback_data="confirm_booking")
    btn_no = types.InlineKeyboardButton("❌ Ошибка / Отмена", callback_data="cancel_booking")
    markup.add(btn_yes)
    markup.add(btn_no)
    return markup

def route_keyboard():
    """Инлайн-кнопка ссылки на карты"""
    markup = types.InlineKeyboardMarkup()
    btn_url = types.InlineKeyboardButton("📍 Открыть Яндекс.Карты", url=config.YANDEX_MAPS_URL)
    markup.add(btn_url)
    return markup

def policy_keyboard():
    """Кнопка принятия политики"""
    markup = types.InlineKeyboardMarkup()
    btn_agree = types.InlineKeyboardButton("✅ Я даю согласие", callback_data="agree_policy")
    markup.add(btn_agree)
    return markup

def master_keyboard(masters):
    """Инлайн-кнопки выбора мастера"""
    markup = types.InlineKeyboardMarkup()
    for name, emoji in masters:
        markup.add(types.InlineKeyboardButton(
            f"{emoji} {name}",
            callback_data=f"master:{name}"
        ))
    markup.add(types.InlineKeyboardButton("🔀 Любой свободный мастер", callback_data="master:—"))
    markup.add(types.InlineKeyboardButton("❌ Отмена записи", callback_data="cancel_booking"))
    return markup

# ===========================
# 🔍 ПОИСК МАСТЕРОВ ПО УСЛУГЕ
# ===========================
def find_masters_for_service(reason: str):
    """
    Ищет мастеров по ключевым словам из названия услуги.
    Возвращает список мастеров (Имя, эмодзи) или пустой список.
    """
    if not reason:
        return []
    reason_lower = reason.lower()
    for keyword, masters in config.MASTERS_BY_SERVICE.items():
        if keyword in reason_lower:
            return masters
    return []

# ===========================
# 👋 ОБРАБОТЧИКИ КОМАНД
# ===========================
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.chat.id
    logger.info(f"User {uid} started the bot")
    
    # Сбрасываем контекст, но НЕ сбрасываем согласие на политику
    state_manager.clear_user_state(uid)
    
    # Проверка политики
    if not state_manager.is_policy_accepted(uid):
        bot.send_message(uid, config.MSG_POLICY_REQUEST, reply_markup=policy_keyboard(), parse_mode="Markdown")
        return

    bot.send_message(uid, config.MSG_START, reply_markup=main_menu(), parse_mode="Markdown")

# ===========================
# 📩 ГЛАВНЫЙ ОБРАБОТЧИК ТЕКСТА
# ===========================
@bot.message_handler(content_types=['text'])
def handle_text(message):
    uid = message.chat.id
    text = message.text
    
    # 0. БЛОКИРОВКА БЕЗ ПОЛИТИКИ
    if not state_manager.is_policy_accepted(uid):
         bot.send_message(uid, "🔒 Для начала работы необходимо принять соглашение об обработке данных.", reply_markup=policy_keyboard())
         return

    # 0.1 ПРОВЕРКА ТАЙМАУТА СЕССИИ (30 мин)
    user_meta = state_manager.get_user_meta(uid)
    last_interaction = user_meta.get("last_interaction", 0)
    
    # Если прошло больше 30 минут и нет активного оформления брони
    if time.time() - last_interaction > SESSION_TIMEOUT:
        pending = state_manager.get_pending_booking(uid)
        if not pending: # Не сбрасываем, если человек "висит" на стадии подтверждения (хотя маловероятно через 30 мин)
             state_manager.clear_history(uid)
             logger.info(f"Session timed out for user {uid}")
             # Можно уведомить пользователя, а можно тихо сбросить
             # bot.send_message(uid, "⏳ Ваша сессия обновлена.")

    # Обновляем время активности
    state_manager.update_last_interaction(uid)

    # --- ЭТАП 1: БЫСТРЫЕ ФИЛЬТРЫ (СТАТИКА) ---
    
    # 1. Сброс
    if "сброс" in text.lower() or ("отмена" in text.lower() and len(text) < 10):
        state_manager.clear_user_state(uid)
        bot.send_message(uid, config.MSG_CANCELLED, reply_markup=main_menu())
        return

    # 2. Маршрут
    if "маршрут" in text.lower() or "адрес" in text.lower() or "где вы" in text.lower():
        bot.send_message(uid, f"🏥 **Студия красоты «{config.CLINIC_NAME}»**\n{config.CLINIC_ADDRESS}.", 
                         reply_markup=route_keyboard(), parse_mode="Markdown")
        return

    # 3. Контакты
    if "contacts" in text.lower() or "контакты" in text.lower() or "телефон" in text.lower():
        bot.send_message(uid, f"📞 **Наш телефон:**\n{config.CLINIC_PHONE}\n\nРаботаем {config.CLINIC_WORKING_HOURS}.", 
                         reply_markup=main_menu(), parse_mode="Markdown")
        return
        
    # --- ЭТАП 2: ИНТЕЛЛЕКТУАЛЬНАЯ ОБРАБОТКА (AI) ---
    
    bot.send_chat_action(uid, 'typing')
    
    # Получаем историю
    history = state_manager.get_history(uid)
    
    # 1. Запрос к Groq
    try:
        ai_raw_response = get_groq_response(text, history)
    except Exception as e:
        logger.error(f"Error calling AI: {e}")
        bot.send_message(uid, "Извините, произошла ошибка связи. Попробуйте позже.")
        return
    
    # 2. Парсинг ответа
    action_type, action_data, clean_text = parse_server_response(ai_raw_response)
    
    # 3. Обновление истории
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": ai_raw_response})
    state_manager.update_history(uid, history)
    
    # --- ЭТАП 3: ВЫПОЛНЕНИЕ ДЕЙСТВИЙ ---
    
    # СЦЕНАРИЙ А: БРОНИРОВАНИЕ
    if action_type == 'BOOKING':
        state_manager.set_pending_booking(uid, action_data)
        reason = action_data.get('reason', '')
        masters = find_masters_for_service(reason)

        if masters:
            # Предлагаем выбрать мастера
            bot.send_message(
                uid,
                f"💅 *Отлично!* Услуга: *{reason}*\n\nВыбери мастера:",
                reply_markup=master_keyboard(masters),
                parse_mode="Markdown"
            )
        else:
            # Мастера не определены (неизвестная бьюти-услуга) — пропускаем шаг выбора
            action_data['master'] = '—'
            state_manager.set_pending_booking(uid, action_data)
            _show_confirm_card(uid, action_data)
        
    # СЦЕНАРИЙ Б: СБРОС ДИАЛОГА
    elif action_type == 'CANCEL':
        state_manager.clear_user_state(uid)
        bot.send_message(uid, config.MSG_CANCELLED, reply_markup=main_menu())

    # СЦЕНАРИЙ В: ОТМЕНА ЗАПИСИ
    elif action_type == 'CANCEL_RECORD':
        cancel_phone = action_data.get('phone')
        bot.send_message(uid, f"🔍 Ищу активную запись по номеру {cancel_phone}...")
        
        success, info_text = cancel_booking_by_phone(cancel_phone)
        
        if success:
            bot.send_message(uid, f"✅ Запись на имя **{info_text}** успешно отменена.", reply_markup=main_menu(), parse_mode="Markdown")
            
            admin_cancel_msg = (
                f"🗑 **ВНИМАНИЕ: ОТМЕНА ЗАПИСИ!**\n\n"
                f"👤 Клиент: {info_text}\n"
                f"📱 Телефон: `{cancel_phone}`\n"
                f"ℹ️ Статус в CRM изменен на: ОТМЕНЕНА"
            )
            try:
                if config.ADMIN_ID:
                    bot.send_message(config.ADMIN_ID, admin_cancel_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to notify admin about cancellation: {e}")
            
            state_manager.clear_history(uid)
        else:
            bot.send_message(uid, f"⚠️ {info_text}\nПожалуйста, проверьте номер или позвоните нам напрямую.", reply_markup=main_menu())
        
    # СЦЕНАРИЙ Г: ПРОСТО ОБЩЕНИЕ
    else:
        bot.send_message(uid, clean_text, reply_markup=main_menu())

# ===========================
# 🗂 ПОКАЗ КАРТОЧКИ ПОДТВЕРЖДЕНИЯ
# ===========================
def _show_confirm_card(uid, booking_data):
    """Отправляет карточку проверки данных с кнопками подтверждения."""
    confirm_msg = (
        f"📝 **ПРОВЕРКА ДАННЫХ:**\n\n"
        f"👤 **Клиент:** {booking_data.get('surname', '')} {booking_data.get('name', '')}\n"
        f"📱 **Телефон:** `{booking_data.get('phone', 'Не указан')}`\n"
        f"🕒 **Дата/Время:** {booking_data.get('date', 'В ближайшее время')}\n"
        f"💅 **Услуга:** {booking_data.get('reason', 'Не указана')}\n"
        f"🧑‍🎨 **Мастер:** {booking_data.get('master', '—')}\n\n"
        f"_Всё верно? Отправляем?_"
    )
    bot.send_message(uid, confirm_msg, reply_markup=confirm_keyboard(), parse_mode="Markdown")

# ===========================
# 🖱 ОБРАБОТЧИК КНОПОК (CALLBACK)
# ===========================
@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    uid = call.message.chat.id
    
    # 0. ОБРАБОТКА ПОЛИТИКИ
    if call.data == "agree_policy":
        state_manager.set_policy_accepted(uid)
        bot.answer_callback_query(call.id, "Спасибо! Доступ открыт.")
        bot.edit_message_reply_markup(chat_id=uid, message_id=call.message.message_id, reply_markup=None)
        bot.send_message(uid, config.MSG_START, reply_markup=main_menu(), parse_mode="Markdown")
        return

    # Блокировка остальных кнопок без политики
    if not state_manager.is_policy_accepted(uid):
        bot.answer_callback_query(call.id, "Сначала примите соглашение.")
        return

    state_manager.update_last_interaction(uid)

    # ВЫБОР МАСТЕРА
    if call.data.startswith("master:"):
        chosen_master = call.data[len("master:"):]
        booking_data = state_manager.get_pending_booking(uid)

        if not booking_data:
            bot.answer_callback_query(call.id, "Сессия истекла, начните запись заново.")
            return

        # Сохраняем выбранного мастера в данные записи
        booking_data['master'] = chosen_master
        state_manager.set_pending_booking(uid, booking_data)

        bot.answer_callback_query(call.id, f"Мастер: {chosen_master}")
        # Убираем кнопки выбора и показываем финальную карточку
        try:
            bot.edit_message_reply_markup(chat_id=uid, message_id=call.message.message_id, reply_markup=None)
        except Exception:
            pass
        _show_confirm_card(uid, booking_data)
        return

    # КНОПКА "ОТМЕНА" (под карточкой подтверждения)
    if call.data == "cancel_booking":
        # АТОМАРНО захватываем запись — если она уже захвачена другим нажатием, claim вернет None
        claimed = state_manager.claim_pending_booking(uid)
        if not claimed:
            # Запись уже захвачена другим нажатием — просто убираем жаверку
            bot.answer_callback_query(call.id)
            return
        
        # Информируем ИИ об отмене
        history = state_manager.get_history(uid)
        history.append({"role": "user", "content": "[Нажата кнопка: ОТМЕНА ЗАПИСИ (на этапе подтверждения)]"})
        history.append({"role": "assistant", "content": "Запись отменена пользователем. Ожидаю дальнейших инструкций."})
        state_manager.update_history(uid, history)
        
        bot.edit_message_text(chat_id=uid, message_id=call.message.message_id, 
                              text="❌ **Оформление записи прервано.**\nМожешь задать вопрос или начать заново.",
                              parse_mode="Markdown")
        return
    
    # КНОПКА "ПОДТВЕРЖДАЮ"
    elif call.data == "confirm_booking":
        # АТОМАРНО захватываем запись — первое нажатие получает данные, все последующие получат None
        booking_data = state_manager.claim_pending_booking(uid)
        
        if not booking_data:
            bot.answer_callback_query(call.id, "Запись уже обрабатывается или время сессии истекло.")
            return

        state_manager.clear_user_state(uid)

        username = call.from_user.username
        full_name = f"{booking_data.get('surname', '')} {booking_data.get('name', '')}"
        
        # 2. МГНОВЕННЫЙ ОТВЕТ ПОЛЬЗОВАТЕЛЮ
        bot.answer_callback_query(call.id, "Отправляю заявку...")
        master = booking_data.get('master', '—')
        success_msg = config.MSG_SUCCESS_BOOKING.format(
            full_name=full_name,
            date=booking_data.get('date'),
            reason=booking_data.get('reason'),
            master=master,
            CLINIC_PHONE=config.CLINIC_SDR_PHONE
        )
        
        try:
            bot.edit_message_text(chat_id=uid, message_id=call.message.message_id, 
                                  text=success_msg, parse_mode="Markdown")
        except Exception as e:
             logger.error(f"Failed to edit message: {e}")
             bot.send_message(uid, success_msg, parse_mode="Markdown")
             
        # 3. ФОНОВОЕ СОХРАНЕНИЕ
        # Переносим тяжеловесный запрос к Google API в отдельный поток
        def background_save():
            is_saved = save_booking_to_sheets(booking_data, username, uid)
            
            if not is_saved:
                 logger.error(f"Failed to save booking for user {uid}")
                 error_msg = "⚠️ К сожалению, произошла длительная техническая ошибка при сохранении вашей заявки в базу. Пожалуйста, напишите нам напрямую или попробуйте заново чуть позже."
                 bot.send_message(uid, error_msg, parse_mode="Markdown")
                 return

            # Уведомляем Админа только после успешного сохранения в базу
            admin_text = (
                f"🔥 **НОВАЯ ЗАЯВКА ({config.CLINIC_NAME})**\n"
                f"👤 **{full_name}**\n"
                f"📱 `{booking_data.get('phone')}`\n"
                f"🕒 {booking_data.get('date')}\n"
                f"💅 {booking_data.get('reason')}\n"
                f"🧑‍🎨 Мастер: {booking_data.get('master', '—')}\n"
                f"🔗 @{username if username else 'Нет ника'}"
            )
            try:
                if config.ADMIN_ID:
                    bot.send_message(config.ADMIN_ID, admin_text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to notify admin about new booking: {e}")

        # Запускаем сохранение в отдельном потоке
        threading.Thread(target=background_save, daemon=True).start()

# ===========================
# 🚀 ЗАПУСК (RESTART LOOP)
# ===========================
if __name__ == "__main__":
    logger.info(f"💎 Бот {config.CLINIC_NAME} запущен!")
    
    # Отключаем лишний шум TeleBot при старте (если нужно)
    # logging.getLogger('TeleBot').setLevel(logging.WARNING)

    while True:
        try:
            bot.to_step_at = -1 # Hack to prevent step handler errors on restart
            bot.polling(non_stop=True, interval=1, timeout=30)
            
        except KeyboardInterrupt:
            # Сразу глушим логгер TeleBot, чтобы не спамил "Break infinity polling"
            logging.getLogger('TeleBot').setLevel(logging.CRITICAL)
            logger.info("🛑 Бот остановлен пользователем (Ctrl+C).")
            # Корректно останавливаем
            bot.stop_bot()
            break
            
        except Exception as e:
            logger.error(f"🔄 Перезапуск бота: {e}")
            time.sleep(3)
