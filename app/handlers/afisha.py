# app/handlers/afisha.py

import asyncio
import logging
from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.utils.markdown import hbold
from datetime import datetime, timedelta
from calendar import monthrange
from aiogram.exceptions import TelegramBadRequest

from ..database.requests import requests as db
from app import keyboards as kb
from ..lexicon import Lexicon,get_event_type_keys, get_event_type_storage_value
from app.utils.utils import format_events_with_headers, format_events_for_response
from aiogram.filters import or_f
from app.handlers.states import AfishaFlowFSM,AddToSubsFSM,CombinedFlow
from app.handlers.states import FavoritesFSM
from .search_cities import start_city_search, process_city_input, back_to_city_list
from aiogram.filters import StateFilter
from app.handlers.config import SUBSCRIPTIONS_LIMIT 

router = Router()

# --- Вспомогательная функция для отправки длинных сообщений ---
async def send_long_message(message: Message, text: str, lexicon: Lexicon, **kwargs) -> list[int]:
    """
    Отправляет длинный текст, разбивая его на части, и крепит клавиатуру к последней.
    ВОЗВРАЩАЕТ: Список ID всех отправленных сообщений.
    """
    sent_message_ids = []
    MESSAGE_LIMIT = 4096

    if not text.strip():
        msg = await message.answer(lexicon.get('afisha_nothing_found_for_query'), reply_markup=kwargs.get('reply_markup'))
        sent_message_ids.append(msg.message_id)
        return sent_message_ids

    if len(text) <= MESSAGE_LIMIT:
        msg = await message.answer(text, **kwargs)
        sent_message_ids.append(msg.message_id)
        return sent_message_ids

    text_parts = []
    current_part = ""
    for line in text.split('\n'):
        if len(current_part) + len(line) + 1 > MESSAGE_LIMIT:
            text_parts.append(current_part)
            current_part = ""
        current_part += line + '\n'
    if current_part:
        text_parts.append(current_part)

    for i, part in enumerate(text_parts):
        final_kwargs = kwargs if i == len(text_parts) - 1 else {
            'parse_mode': kwargs.get('parse_mode'),
            'disable_web_page_preview': kwargs.get('disable_web_page_preview')
        }
        msg = await message.answer(part, **final_kwargs)
        sent_message_ids.append(msg.message_id)
        
    return sent_message_ids # <-- ВАЖНО: Убедитесь, что эта строка есть

async def show_filter_type_choice(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    date_from_str = data.get("date_from")
    date_to_str = data.get("date_to")
    
    date_from = datetime.fromisoformat(date_from_str) if date_from_str else None
    date_to = datetime.fromisoformat(date_to_str) if date_to_str else None
    
    if not date_from or not date_to:
        # Добавим небольшую проверку на всякий случай
        await callback.answer("Date selection error. Please try again.", show_alert=True)
        return

    await state.set_state(AfishaFlowFSM.choosing_filter_type)
    lexicon = Lexicon(callback.from_user.language_code)
    
    await callback.message.edit_text(
        lexicon.get('afisha_choose_filter_type_prompt').format(
            date_from=date_from.strftime('%d.%m.%Y'),
            date_to=date_to.strftime('%d.%m.%Y')
        ),
        reply_markup=kb.get_filter_type_choice_keyboard(lexicon)
    )


# --- НОВАЯ ТОЧКА ВХОДА В АФИШУ ---
@router.message(F.text.in_(['🗓 Афиша', '🗓 Events', '🗓 Афіша']), StateFilter('*'))
async def menu_afisha_start(message: Message, state: FSMContext):
    current_data = await state.get_data()
    data_to_keep = {
        'messages_to_delete_on_expire': current_data.get('messages_to_delete_on_expire'),
        'last_shown_event_ids': current_data.get('last_shown_event_ids')
    }
    await state.clear()
    data_to_restore = {k: v for k, v in data_to_keep.items() if v is not None}
    if data_to_restore:
        await state.update_data(data_to_restore)
    await state.set_state(AfishaFlowFSM.choosing_date_period)
    lexicon = Lexicon(message.from_user.language_code)
    await message.answer(
        lexicon.get('afisha_choose_period_prompt'),
        reply_markup=kb.get_date_period_keyboard(lexicon)
    )

@router.callback_query(F.data == "back_to_date_choice")
async def cq_back_to_date_choice(callback: CallbackQuery, state: FSMContext):
    """Возвращает к самому первому экрану выбора периода."""
    await state.set_state(AfishaFlowFSM.choosing_date_period)
    lexicon = Lexicon(callback.from_user.language_code)
    await callback.message.edit_text(
        lexicon.get('afisha_choose_period_prompt'),
        reply_markup=kb.get_date_period_keyboard(lexicon)
    )

# --- НОВЫЕ ХЭНДЛЕРЫ ВЫБОРА ДАТЫ ---
@router.callback_query(AfishaFlowFSM.choosing_date_period, F.data.startswith("select_period:"))
async def process_period_choice(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":")[1]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    date_from, date_to = None, None
    
    if period == "today":
        date_from = date_to = today
    elif period == "tomorrow":
        date_from = date_to = today + timedelta(days=1)
    elif period == "this_week":
        date_from = today
        date_to = today + timedelta(days=(6 - today.weekday()))
    elif period == "this_weekend":
        saturday = today + timedelta(days=(5 - today.weekday())) if today.weekday() <= 5 else today
        sunday = today + timedelta(days=(6 - today.weekday()))
        date_from, date_to = saturday, sunday
    elif period == "this_month":
        date_from = today.replace(day=1)
        last_day_num = monthrange(today.year, today.month)[1]
        date_to = today.replace(day=last_day_num)
    elif period == "other_month":
        await state.set_state(AfishaFlowFSM.choosing_month)
        lexicon = Lexicon(callback.from_user.language_code)
        await callback.message.edit_text(
            lexicon.get('afisha_choose_month_prompt'),
            reply_markup=kb.get_month_choice_keyboard(lexicon)
        )
        return

    await state.update_data(
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None
    )
    await show_filter_type_choice(callback, state)

@router.callback_query(AfishaFlowFSM.choosing_month, F.data.startswith("select_month:"))
async def process_month_choice(callback: CallbackQuery, state: FSMContext):
    year_month_str = callback.data.split(":")[1]
    year, month = map(int, year_month_str.split('-'))
    
    date_from = datetime(year, month, 1)
    last_day_num = monthrange(year, month)[1]
    date_to = datetime(year, month, last_day_num)
    
    await state.update_data(
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None
    )
    await show_filter_type_choice(callback, state)


@router.callback_query(AfishaFlowFSM.choosing_filter_type, F.data == "filter_type:my_prefs")
async def afisha_by_my_prefs(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    lexicon = Lexicon(callback.from_user.language_code)
    user_prefs = await db.get_user_preferences(user_id)
    
    if not user_prefs or not user_prefs.get("home_city") or not user_prefs.get("preferred_event_types"):
        await callback.answer(lexicon.get('afisha_prefs_not_configured_alert'), show_alert=True)
        return

    data = await state.get_data()
    date_from_str = data.get("date_from")
    date_to_str = data.get("date_to")
    date_from = datetime.fromisoformat(date_from_str) if date_from_str else None
    date_to = datetime.fromisoformat(date_to_str) if date_to_str else None

    city_name, event_types = user_prefs["home_city"], user_prefs["preferred_event_types"]

    events_by_category = {}
    for etype in event_types:
        # Передаем в функцию БД уже готовые объекты datetime
        events = await db.get_grouped_events_by_city_and_category(city_name, etype, date_from, date_to)
        if events: events_by_category[etype] = events
            
    response_text, event_ids = await format_events_with_headers(events_by_category, city_name, lexicon)
    
    header_text = lexicon.get('afisha_results_by_prefs_header').format(city_name=hbold(city_name))
    header_message = await callback.message.edit_text(header_text, parse_mode=ParseMode.HTML)
    
    if not event_ids:
        await callback.message.answer(lexicon.get('afisha_no_results_for_prefs_period'))
        await state.clear()
        return

    sent_messages_ids = await send_long_message(
        callback.message, response_text, lexicon,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=kb.get_afisha_actions_keyboard(lexicon)
    )
    await state.update_data(
        last_shown_event_ids=event_ids,
        messages_to_delete_on_expire=[header_message.message_id] + sent_messages_ids
    )

@router.callback_query(AfishaFlowFSM.choosing_filter_type, F.data == "filter_type:temporary")
async def afisha_by_temporary_prefs_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AfishaFlowFSM.temp_choosing_city)
    await state.set_state(AfishaFlowFSM.temp_choosing_country)
    lexicon = Lexicon(callback.from_user.language_code)

    # 2. Показываем текст и клавиатуру для выбора СТРАНЫ
    text = lexicon.get('afisha_temp_select_country_prompt')
    await callback.message.edit_text(
        text,
        reply_markup=kb.get_temp_country_selection_keyboard(lexicon),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(AfishaFlowFSM.temp_choosing_country, F.data == "back_to_filter_type_choice")
async def cq_back_to_filter_type_choice(callback: CallbackQuery, state: FSMContext):
    """Возвращает пользователя с экрана выбора страны на экран выбора типа фильтра."""
    await show_filter_type_choice(callback, state)


# --- НОВЫЙ ХЭНДЛЕР: Обработка выбора страны ---
@router.callback_query(AfishaFlowFSM.temp_choosing_country, F.data.startswith("temp_select_country:"))
async def cq_temp_country_selected(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор страны и предлагает выбрать город."""
    country_name = callback.data.split(":")[1]
    await state.update_data(temp_country=country_name)
    await state.set_state(AfishaFlowFSM.temp_choosing_city)
    
    lexicon = Lexicon(callback.from_user.language_code)
    top_cities = await db.get_top_cities_for_country(country_name)
    text = lexicon.get('afisha_temp_select_city_prompt').format(country_name=hbold(country_name))
    
    # --- ИЗМЕНЕНИЕ: Используем универсальную клавиатуру с правильной кнопкой "Назад" ---
    await callback.message.edit_text(
        text,
        reply_markup=kb.get_home_city_selection_keyboard(
            top_cities, 
            lexicon, 
            back_callback_data="back_to_temp_country_choice"
        ),
        parse_mode=ParseMode.HTML
    )

# --- НОВЫЙ ХЭНДЛЕР: Возврат к выбору страны ---
@router.callback_query(
    or_f(AfishaFlowFSM.temp_choosing_city, AfishaFlowFSM.temp_waiting_city_input), 
    F.data == "back_to_temp_country_choice"
)
async def cq_back_to_temp_country_choice(callback: CallbackQuery, state: FSMContext):
# --- КОНЕЦ ИЗМЕНЕНИЯ ---
    """Возвращает пользователя с экрана выбора города на экран выбора страны."""
    # --- ИЗМЕНЕНИЕ: Устанавливаем правильное состояние ПЕРЕД вызовом ---
    await state.set_state(AfishaFlowFSM.temp_choosing_country)
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    await afisha_by_temporary_prefs_start(callback, state)

# --- ХЭНДЛЕРЫ ДЛЯ ВРЕМЕННОЙ НАСТРОЙКИ ---

@router.callback_query(AfishaFlowFSM.temp_choosing_city, F.data.startswith("select_home_city:"))
async def temp_city_selected(callback: CallbackQuery, state: FSMContext): 
    city_name = callback.data.split(":")[1]
    await state.update_data(temp_city=city_name) 
    await state.set_state(AfishaFlowFSM.temp_choosing_event_types)
    await state.update_data(temp_event_types=[]) 
    
    lexicon = Lexicon(callback.from_user.language_code)
    text = lexicon.get('afisha_temp_select_types_prompt').format(city_name=hbold(city_name))
    await callback.message.edit_text(
        text,
        reply_markup=kb.get_event_type_selection_keyboard(lexicon, []),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(AfishaFlowFSM.temp_choosing_city, F.data == "search_for_home_city")
async def cq_afisha_search_for_city(callback: CallbackQuery, state: FSMContext):
    """Запускает поиск города во временных настройках Афиши."""
    await start_city_search(
        callback, 
        state, 
        new_state=AfishaFlowFSM.temp_waiting_city_input,
        back_callback="back_to_temp_country_choice"
    )


@router.message(AfishaFlowFSM.temp_waiting_city_input, F.text)
async def process_afisha_city_search(message: Message, state: FSMContext):
    """Обрабатывает введенное пользователем название города для поиска."""
    await process_city_input(
        message=message,
        state=state,
        country_key="temp_country",
        return_state=AfishaFlowFSM.temp_choosing_city,
        found_cities_kb=kb.get_found_home_cities_keyboard,
        back_callback="back_to_temp_country_choice"
    )


@router.callback_query(AfishaFlowFSM.temp_choosing_city, F.data == "back_to_city_selection")
async def cq_afisha_back_to_city_list(callback: CallbackQuery, state: FSMContext):
    """Возвращает пользователя к списку городов по умолчанию после неудачного поиска."""
    await back_to_city_list(
        callback=callback,
        state=state,
        country_key="temp_country",
        city_prompt_key='afisha_temp_select_city_prompt',
        city_selection_kb=lambda cities, lex: kb.get_home_city_selection_keyboard(
            cities, lex, back_callback_data="back_to_temp_country_choice"
        )
    )


@router.callback_query(AfishaFlowFSM.temp_choosing_event_types, F.data.startswith("toggle_event_type:"))
async def temp_toggle_type(callback: CallbackQuery, state: FSMContext): 
    event_type_key = callback.data.split(":")[1]
    
    data = await state.get_data()
    # Обратите внимание, что здесь ключ в state называется 'temp_event_types'
    selected_values = data.get("temp_event_types", [])

    all_event_keys = get_event_type_keys()
    all_storage_values = [get_event_type_storage_value(key) for key in all_event_keys]
    
    # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
    
    if event_type_key == 'all':
        # Если нажата кнопка "Выбрать/Снять все"
        current_selection_set = set(selected_values)
        all_values_set = set(all_storage_values)
        
        # Если выбраны не все, то выбираем все. Иначе - очищаем.
        if current_selection_set != all_values_set:
            selected_values = all_storage_values
        else:
            selected_values = []
    else:
        # Если нажата обычная кнопка с типом события
        if event_type_key in selected_values:
            selected_values.remove(event_type_key)
        else:
            selected_values.append(event_type_key)
            
    # --- КОНЕЦ ИСПРАВЛЕННОЙ ЛОГИКИ ---

    # Сохраняем обновленный список в state под правильным ключом
    await state.update_data(temp_event_types=selected_values)
    
    lexicon = Lexicon(callback.from_user.language_code)
    
    try:
        # Перерисовываем клавиатуру
        await callback.message.edit_reply_markup(
            reply_markup=kb.get_event_type_selection_keyboard(lexicon, selected_values)
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            raise
    
    await callback.answer()

@router.callback_query(AfishaFlowFSM.temp_choosing_event_types, F.data.startswith("finish_preferences_selection:"))
async def temp_finish_and_display(callback: CallbackQuery, state: FSMContext): 
    data = await state.get_data()
    lexicon = Lexicon(callback.from_user.language_code)
    
    city_name = data.get("temp_city")
    event_types = data.get("temp_event_types", [])
    date_from_str = data.get("date_from")
    date_to_str = data.get("date_to")
    date_from = datetime.fromisoformat(date_from_str) if date_from_str else None
    date_to = datetime.fromisoformat(date_to_str) if date_to_str else None

    if not event_types:
        await callback.answer(lexicon.get('select_at_least_one_event_type_alert'), show_alert=True)
        return
        
    events_by_category = {}
    for etype in event_types:
        # Передаем в функцию БД уже готовые объекты datetime
        events = await db.get_grouped_events_by_city_and_category(city_name, etype, date_from, date_to)
        if events: events_by_category[etype] = events
            
    response_text, event_ids = await format_events_with_headers(events_by_category, city_name, lexicon) 
    
    header_text = lexicon.get('afisha_results_for_city_header').format(city_name=hbold(city_name))
    try:
        header_message = await callback.message.edit_text(header_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e): pass
        else: raise 

    if not event_ids:
        await callback.message.answer(lexicon.get('afisha_nothing_found_for_query'))
        await state.clear()
        return

    sent_messages_ids = await send_long_message(
        callback.message, response_text, lexicon,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=kb.get_afisha_actions_keyboard(lexicon)
    )

    # Правильно сохраняем
    await state.update_data(
        last_shown_event_ids=event_ids,
        messages_to_delete_on_expire=[header_message.message_id] + sent_messages_ids
    )
# --- ДОБАВЛЕНИЕ В ПОДПИСКИ (остается без изменений) ---

@router.callback_query(CombinedFlow.active, F.data == "add_events_to_subs")
async def cq_add_to_subs_from_combined_flow(callback: CallbackQuery, state: FSMContext):
    """
    Начинает добавление в подписки из комбинированного флоу.
    Устанавливает флаг, что мы ждем номера.
    """
    logging.warning("--- DEBUG: Сработал хэндлер cq_add_to_subs_from_combined_flow ---")
    logging.warning(f"--- DEBUG: ТЕКУЩЕЕ СОСТОЯНИЕ: {await state.get_state()} ---")
    logging.warning(f"--- DEBUG: ДАННЫЕ В STATE: {await state.get_data()} ---")
    data = await state.get_data()
    lexicon = Lexicon(callback.from_user.language_code)
    if not data.get("last_shown_event_ids"):
        await callback.answer(lexicon.get('afisha_must_find_events_first_alert'), show_alert=True)
        return

    # Устанавливаем флаг, что мы ждем номера
    await state.update_data(
        sub_flow_active=True, 
        callback_query_id_for_alert=callback.id
    )
    
    prompt_message = await callback.message.answer(lexicon.get('subs_enter_numbers_prompt'))
    await state.update_data(prompt_message_id=prompt_message.message_id)
    
    await callback.answer()

# --- ХЭНДЛЕР 2: ТОЛЬКО ДЛЯ АФИШИ И ИЗБРАННОГО ---
@router.callback_query(
    or_f(AfishaFlowFSM.choosing_filter_type, AfishaFlowFSM.temp_choosing_event_types, FavoritesFSM.viewing_artist_events),
    F.data == "add_events_to_subs"
)
async def cq_add_to_subs_start(callback: CallbackQuery, state: FSMContext):
    """
    Начинает добавление в подписки для Афиши и Избранного, переходя в состояние AddToSubsFSM.
    """
    data = await state.get_data()
    lexicon = Lexicon(callback.from_user.language_code)
    current_subs_count = await db.count_user_subscriptions(callback.from_user.id)
    if current_subs_count >= SUBSCRIPTIONS_LIMIT:
        await callback.answer(
            lexicon.get('subscriptions_limit_reached_alert').format(limit=SUBSCRIPTIONS_LIMIT),
            show_alert=True
        )
        return
    if not data.get("last_shown_event_ids"):
        await callback.answer(lexicon.get('afisha_must_find_events_first_alert'), show_alert=True)
        return

    # Определяем, в какое состояние FSM перейти
    current_state_name = await state.get_state()
    if 'CombinedFlow' not in str(current_state_name):
        # Для Афиши и Избранного переходим в AddToSubsFSM
        await state.set_state(AddToSubsFSM.waiting_for_event_numbers)
    else:
        # Для CombinedFlow остаемся в том же состоянии, но ставим флаг
        await state.update_data(sub_flow_active=True)

    # Сохраняем callback_id для будущего alert'а
    await state.update_data(callback_query_id_for_alert=callback.id)

    # ВСЕГДА отправляем новое сообщение, не редактируя старое
    prompt_message = await callback.message.answer(lexicon.get('subs_enter_numbers_prompt'))
    await state.update_data(prompt_message_id=prompt_message.message_id)

    await callback.answer()


@router.callback_query(F.data == "add_events_to_subs")
async def cq_add_to_subs_expired_session(callback: CallbackQuery, state: FSMContext):
    lexicon = Lexicon(callback.from_user.language_code)
    await callback.answer(lexicon.get('session_expired_alert'), show_alert=True)


# --- ХЭНДЛЕРЫ ОБРАБОТКИ ТЕКСТА ---

# --- ХЭНДЛЕР 3: ОБРАБОТКА ТЕКСТА ТОЛЬКО ДЛЯ COMBINED FLOW ---
@router.message(
    CombinedFlow.active, 
    F.text,
    lambda message: F.state_data.get('sub_flow_active')
)
async def process_event_numbers_for_combined_flow(message: Message, state: FSMContext):
    """
    Обрабатывает номера событий ТОЛЬКО для комбинированного флоу.
    """
    bot = message.bot
    lexicon = Lexicon(message.from_user.language_code)
    data = await state.get_data()
    
    last_shown_ids = data.get("last_shown_event_ids", [])
    prompt_message_id = data.get('prompt_message_id')
    callback_query_id = data.get('callback_query_id_for_alert')
    # --- ИЗМЕНЕНИЕ: Получаем ID только сообщений с событиями ---
    event_messages_ids = data.get('event_messages_ids', [])
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    # ... (Логика парсинга и добавления в БД остается без изменений) ...
    added_count = 0
    try:
        # ... (код парсинга)
        input_numbers = [int(num.strip()) for num in message.text.replace(',', ' ').split()]
        current_subs_count = await db.count_user_subscriptions(message.from_user.id)
        if current_subs_count + len(input_numbers) > SUBSCRIPTIONS_LIMIT:
            can_add_count = SUBSCRIPTIONS_LIMIT - current_subs_count
            # Сообщаем пользователю, что он пытается добавить слишком много
            await message.reply(
                lexicon.get('subscriptions_limit_will_be_exceeded_alert').format(
                    limit=SUBSCRIPTIONS_LIMIT,
                    can_add=max(0, can_add_count) # Используем max(0, ...) на случай, если лимит уже превышен
                )
            )
            return
        event_ids_to_add, invalid_numbers = [], []
        for num in input_numbers:
            if 1 <= num <= len(last_shown_ids): event_ids_to_add.append(last_shown_ids[num-1])
            else: invalid_numbers.append(str(num))
        if invalid_numbers:
            await message.reply(lexicon.get('subs_invalid_numbers_error').format(invalid_list=", ".join(invalid_numbers)))
            return 
        if event_ids_to_add:
            await db.add_events_to_subscriptions_bulk(message.from_user.id, event_ids_to_add)
            added_count = len(event_ids_to_add)
        if not event_ids_to_add and not invalid_numbers: 
            await message.reply(lexicon.get('subs_no_valid_numbers_provided'))
            return
    except ValueError:
        await message.reply(lexicon.get('subs_nan_error'))
        return
    
    # --- ИЗМЕНЕНИЕ: Собираем ID для удаления ---
    all_ids_to_delete = event_messages_ids
    if prompt_message_id:
        all_ids_to_delete.append(prompt_message_id)
    all_ids_to_delete.append(message.message_id)

    # Удаляем сообщения
    for msg_id in set(all_ids_to_delete):
        try:
            await bot.delete_message(message.chat.id, msg_id)
        except TelegramBadRequest:
            pass
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    # Показываем alert
    if added_count > 0 and callback_query_id:
        try:
            await bot.answer_callback_query(callback_query_id=callback_query_id, text=lexicon.get('subs_added_success').format(count=added_count), show_alert=True)
        except TelegramBadRequest:
            pass

    # --- ИЗМЕНЕНИЕ: Частичная очистка state ---
    # Удаляем только те ключи, которые относятся к завершенному флоу подписки на события.
    # Все, что связано с рекомендациями, остается.
    await state.update_data(
        sub_flow_active=False,
        prompt_message_id=None,
        callback_query_id_for_alert=None,
        last_shown_event_ids=None,
        event_messages_ids=None
    )   

# --- ХЭНДЛЕР 4: ОБРАБОТКА ТЕКСТА ТОЛЬКО ДЛЯ АФИШИ И ИЗБРАННОГО ---
@router.message(AddToSubsFSM.waiting_for_event_numbers, F.text)
async def process_event_numbers(message: Message, state: FSMContext, bot: Bot):
    """
    Обрабатывает номера событий для Афиши и Избранного.
    """
    from .favorities import show_single_favorite_menu
    logging.warning("--- DEBUG: Сработал хэндлер process_event_numbers (для Афиши/Избранного) ---")
    logging.warning(f"--- DEBUG: ТЕКУЩЕЕ СОСТОЯНИЕ: {await state.get_state()} ---")
    # bot = message.bot
    lexicon = Lexicon(message.from_user.language_code)
    data = await state.get_data()
    
    last_shown_ids = data.get("last_shown_event_ids", [])
    return_to_artist_id = data.get('return_to_favorite_artist_id')
    message_to_edit_id = data.get('message_to_edit_id')
    prompt_message_id = data.get('prompt_message_id')
    callback_query_id = data.get('callback_query_id_for_alert')

    added_count = 0
    try:
        input_numbers = [int(num.strip()) for num in message.text.replace(',', ' ').split()]
        current_subs_count = await db.count_user_subscriptions(message.from_user.id)
        if current_subs_count + len(input_numbers) > SUBSCRIPTIONS_LIMIT:
             # Считаем, сколько еще можно добавить
            can_add_count = SUBSCRIPTIONS_LIMIT - current_subs_count
            await message.reply(
                lexicon.get('subscriptions_limit_will_be_exceeded_alert').format(
                    limit=SUBSCRIPTIONS_LIMIT,
                    can_add=can_add_count
                )
            )
            return  
        event_ids_to_add, invalid_numbers = [], []
        current_subs_count = await db.count_user_subscriptions(message.from_user.id)
        if current_subs_count + len(event_ids_to_add) > SUBSCRIPTIONS_LIMIT:
            await message.reply(
                lexicon.get('subscriptions_limit_reached_alert').format(
                    limit=SUBSCRIPTIONS_LIMIT
                )
            )
            return
        for num in input_numbers:
            if 1 <= num <= len(last_shown_ids): event_ids_to_add.append(last_shown_ids[num-1])
            else: invalid_numbers.append(str(num))
        if invalid_numbers:
            await message.reply(lexicon.get('subs_invalid_numbers_error').format(invalid_list=", ".join(invalid_numbers)))
            return
        if event_ids_to_add:
            await db.add_events_to_subscriptions_bulk(message.from_user.id, event_ids_to_add)
            added_count = len(event_ids_to_add)
        if not event_ids_to_add and not invalid_numbers:
            await message.reply(lexicon.get('subs_no_valid_numbers_provided'))
            return
    except ValueError:
        await message.reply(lexicon.get('subs_nan_error'))
        return
    
    if prompt_message_id:
        try: await bot.delete_message(message.chat.id, prompt_message_id)
        except TelegramBadRequest: pass
    await message.delete()

    if return_to_artist_id and message_to_edit_id:
        await show_single_favorite_menu(chat_id=message.chat.id, message_id=message_to_edit_id, user_id=message.from_user.id, bot=bot, state=state)
    else:
        await state.clear()

    # --- ИЗМЕНЕНИЕ: Заменяем alert на самоудаляющееся сообщение ---
    if added_count > 0:
        # Отправляем сообщение об успехе
        success_message = await bot.send_message(
            chat_id=message.chat.id,
            text=lexicon.get('subs_added_success').format(count=added_count)
        )
        # Ждем 3 секунды
        await asyncio.sleep(6)
        # Удаляем сообщение об успехе
        try:
            await bot.delete_message(
                chat_id=message.chat.id,
                message_id=success_message.message_id
            )
        except TelegramBadRequest:
            pass # Если пользователь успел удалить его сам
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    