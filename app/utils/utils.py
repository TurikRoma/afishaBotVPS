# app/handlers/common.py

from collections import defaultdict
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.utils.markdown import hbold,hitalic

from ..database.requests import requests as db
from ..database.models import async_session
from app import keyboards as kb
from ..lexicon import Lexicon, LEXICON_COMMANDS_RU, LEXICON_COMMANDS_EN, EVENT_TYPE_EMOJI
from app.handlers.onboarding import start_onboarding_process


COUNTRY_CURRENCY_MAP = {
    'Беларусь': 'BYN',
    'Россия': 'RUB',
    # Можно будет добавлять другие страны сюда
}

DEFAULT_CURRENCY = '$'

def format_price(event, min_price, max_price, country_name: str, lexicon: Lexicon) -> str:
    """
    Универсально форматирует цену события, определяя валюту по стране.
    - event: объект события, у которого есть event.min_price, event.max_price и event.venue.
    """
    # Определяем валюту
    currency = COUNTRY_CURRENCY_MAP.get(country_name, DEFAULT_CURRENCY)

    min_price_str = f"{int(min_price)}" if min_price and min_price % 1 == 0 else f"{min_price}"
    max_price_str = f"{int(max_price)}" if max_price and max_price % 1 == 0 else f"{max_price}"

    if min_price and max_price and min_price != max_price:
        price_info = f"от {min_price_str} до {max_price_str} {currency}"
    elif min_price:
        price_info = f"от {min_price_str} {currency}"
    else:
        price_info = "—"
        
    return price_info

async def set_main_menu(bot: Bot, lang: str):
    commands = LEXICON_COMMANDS_RU if lang in ('ru', 'be') else LEXICON_COMMANDS_EN
    main_menu_commands = [BotCommand(command=cmd, description=desc) for cmd, desc in commands.items()]
    await bot.set_my_commands(main_menu_commands)

async def format_events_with_headers(events_by_category: dict,city_name: str, lexicon) -> tuple[str, list[int]]:
    """
    Форматирует словарь {категория: [события]} в единый текст с заголовками
    и возвращает сквозной список ID.
    """
    if not events_by_category:
        return "По вашему запросу ничего не найдено.", []
    
    country_name = await db.get_country_by_city_name(city_name)

    response_parts = []
    event_ids_in_order = []
    counter = 1  # Сквозной счетчик для нумерации

    for category_name, events in events_by_category.items():
        # Добавляем заголовок категории
        emoji = EVENT_TYPE_EMOJI.get(category_name, "🔹")
        response_parts.append(f"\n\n--- {emoji} {hbold(category_name)} ---\n")

        # Форматируем события внутри этой категории
        for event in events:
            event_ids_in_order.append(event.event_id)
            
            # Нумеруем с помощью сквозного счетчика
            title_text = hbold(f"{counter}. {event.title}")
            counter += 1

            # Остальная логика форматирования одной карточки события
            url = event.links[0] if hasattr(event, 'links') and event.links and event.links[0] else None
            title_link = f'<a href="{url}">{title_text}</a>' if url else title_text
            
            place_info = event.venue_name or "—"
            
            dates_str = sorted(list(set(format_event_date(d, lexicon) for d in event.dates if d))) # НОВАЯ СТРОКА
            dates_info = "\n".join(f"▫️ {d}" for d in dates_str) if dates_str else "—"
            
            price_info = format_price(event, event.min_price, event.max_price, country_name, lexicon)
            
            event_card = (f"{title_link}\n\n"
                          f"📍 <b>Место:</b> <i>{place_info}</i>\n"
                          f"💰 <b>Цена:</b> <i>{price_info}</i>\n\n"
                          f"📅 <b>Доступные даты:</b>\n<i>{dates_info}</i>")
            response_parts.append(event_card)

    # Собираем все части в один большой текст
    return "\n".join(response_parts), event_ids_in_order


async def format_events_for_response(events: list,lexicon) -> str:
    if not events: return "По вашему запросу ничего не найдено."
    response_parts = []
    event_ids_in_order = []
    for event in events:
        event_ids_in_order.append(event.event_id)
        url = event.links[0].url if event.links else None
        title = hbold(event.title)
        title_link = f'<a href="{url}">{title}</a>' if url else title
        place_info = f"{event.venue.name}, {event.venue.city.name}" if event.venue and event.venue.city else (
            event.venue.name if event.venue else "—")
        date_start_info = format_event_date(event.date_start, lexicon)
        price_info = "—"
        if event.price_min and event.price_max:
            price_info = f"от {event.price_min} до {event.price_max} BYN"
        elif event.price_min:
            price_info = f"от {event.price_min} BYN"
        when_info = event.description or "—"
        event_card = (f"{title_link}\n\n"
                      f"📍 <b>Место:</b> <i>{place_info}</i>\n"
                      f"🕒 <b>Время:</b> <i>{when_info}</i>\n"
                      f"📅 <b>Начало:</b> <i>{date_start_info}</i>\n"
                      f"💰 <b>Цена:</b> <i>{price_info}</i>")
        response_parts.append(event_card)
    separator = "\n\n" + "—" * 15 + "\n\n"
    return separator.join(response_parts)

async def format_events_by_artist(
    events: list,
    target_artist_names: list[str], # <-- НОВЫЙ АРГУМЕНТ
    lexicon: Lexicon
) -> tuple[str | None, list[int] | None]:
    # ...
    if not events:
        return None, None

    # 1. Группируем события по имени артиста, но только по тем, кого мы искали
    events_by_artist = defaultdict(list)
    
    # Создаем set для быстрой проверки
    target_artist_set = set(name.lower() for name in target_artist_names)
    for event in events:
        for event_artist in event.artists:
            # Проверяем, является ли артист события одним из тех, кого мы искали
            if event_artist.artist.name.lower() in target_artist_set:
                # Группируем по имени в правильном регистре из БД
                events_by_artist[event_artist.artist.name].append(event)
    
    # Сортируем артистов по алфавиту для предсказуемого вывода
    sorted_artist_names = sorted(events_by_artist.keys())

    response_parts = []
    event_ids_in_order = []
    counter = 1  # Сквозной счетчик для нумерации

    # 2. Формируем текстовые блоки для каждого артиста
    for artist_name in sorted_artist_names:
        # Заголовок для группы событий артиста
        response_parts.append(f"\n\n——— 🎤 {hbold(artist_name.upper())} ———\n")

        # Получаем уникальные события для этого артиста, чтобы не было дублей
        unique_events_for_artist = sorted(
            list(set(events_by_artist[artist_name])), 
            key=lambda e: (e.date_start is None, e.date_start) # Сортируем по дате
        )

        for event in unique_events_for_artist:
            # Проверяем, не добавили ли мы уже это событие под эгидой другого артиста
            if event.event_id in event_ids_in_order:
                continue

            event_ids_in_order.append(event.event_id)
            
            # --- Форматируем карточку одного события (почти как в reminder) ---
            
            # Дата
            date_str = format_event_date(event.date_start, lexicon)
            
            # Место
            place_info = "—"
            if event.venue:
                city_name = event.venue.city.name if event.venue.city else ""
                country_name = event.venue.city.country.name if event.venue.city and event.venue.city.country else ""
                place_info = f"{event.venue.name}, {city_name} ({country_name})"

            # Билеты
            tickets_str = event.tickets_info if event.tickets_info and event.tickets_info != "В наличии" else lexicon.get('no_info') # Добавьте 'no_info': 'Нет информации' в лексикон
            
            # Ссылка
            url = event.links[0].url if event.links else None
            title_text = f"{counter}. {event.title}"
            title_with_link = f'<a href="{url}">{hbold(title_text)}</a>' if url else hbold(title_text)
            
            # Собираем карточку
            event_card = (
                f"{title_with_link}\n"
                f"📅 {date_str}\n"
                f"📍 {hitalic(place_info)}\n"
                f"🎟️ Билеты: {hitalic(tickets_str)}"
            )
            response_parts.append(event_card)
            counter += 1

    if not response_parts:
        return None, None
        
    return "\n".join(response_parts), event_ids_in_order


async def format_events_by_artist_with_region_split(
    events: list,
    tracked_regions: list[str],
    lexicon: Lexicon
) -> tuple[str | None, list[int] | None]:
    """
    Форматирует события, разделяя их на две группы: в отслеживаемых регионах и в остальных.
    Возвращает готовый текст и сквозной список ID событий.
    """
    if not events:
        return None, None

    # 1. Разделяем события на две группы
    events_in_tracked_regions = []
    events_in_other_regions = []
    tracked_regions_set = set(r.lower() for r in tracked_regions)

    for event in events:
        # Проверяем, не было ли это событие уже добавлено (на случай дублей в исходных данных)
        is_already_added = any(e.event_id == event.event_id for e in events_in_tracked_regions + events_in_other_regions)
        if is_already_added:
            continue
            
        event_country = event.venue.city.country.name.lower() if event.venue and event.venue.city and event.venue.city.country else ""
        event_city = event.venue.city.name.lower() if event.venue and event.venue.city else ""
        
        if event_country in tracked_regions_set or event_city in tracked_regions_set:
            events_in_tracked_regions.append(event)
        else:
            events_in_other_regions.append(event)

    # Сортируем обе группы по дате
    events_in_tracked_regions.sort(key=lambda e: (e.date_start is None, e.date_start))
    events_in_other_regions.sort(key=lambda e: (e.date_start is None, e.date_start))

    # 2. Собираем единый список для форматирования с заголовками-разделителями
    # Мы используем словари-маркеры, чтобы знать, где вставлять заголовки
    items_to_format = []
    if events_in_tracked_regions:
        items_to_format.append({'type': 'header', 'text': lexicon.get('favorite_events_in_tracked_regions')})
        items_to_format.extend(events_in_tracked_regions)

    if events_in_other_regions:
        # Если были события в отслеживаемых, добавим визуальный разделитель
        if events_in_tracked_regions:
             items_to_format.append({'type': 'separator'})
        items_to_format.append({'type': 'header', 'text': lexicon.get('favorite_events_in_other_regions')})
        items_to_format.extend(events_in_other_regions)

    if not items_to_format:
        return None, None

    # 3. Единый цикл форматирования
    response_parts = []
    event_ids_in_order = []
    counter = 1

    for item in items_to_format:
        # Если это маркер заголовка
        if isinstance(item, dict) and item['type'] == 'header':
            response_parts.append(hbold(item['text']))
            continue
        # Если это маркер разделителя
        if isinstance(item, dict) and item['type'] == 'separator':
            response_parts.append("\n" + "—" * 15 + "\n")
            continue
            
        # Если это обычный объект события (event)
        event = item
        event_ids_in_order.append(event.event_id)

        # --- Блок форматирования карточки (теперь он здесь один раз) ---
        date_str = format_event_date(event.date_start, lexicon)
        place_info = "—"
        if event.venue:
            city_name = event.venue.city.name if event.venue.city else ""
            country_name = event.venue.city.country.name if event.venue.city and event.venue.city.country else ""
            place_info = f"{event.venue.name}, {city_name} ({country_name})"
        tickets_str = event.tickets_info if event.tickets_info and event.tickets_info != "В наличии" else lexicon.get('no_info')
        url = event.links[0].url if event.links else None
        title_text = f"{counter}. {event.title}"
        title_with_link = f'<a href="{url}">{hbold(title_text)}</a>' if url else hbold(title_text)
        
        event_card = (
            f"{title_with_link}\n"
            f"📅 {date_str}\n"
            f"📍 {hitalic(place_info)}\n"
            f"🎟️ Билеты: {hitalic(tickets_str)}"
        )
        response_parts.append(event_card)
        counter += 1
        
    return "\n\n".join(response_parts), event_ids_in_order

def format_event_date(date_obj: datetime | None, lexicon: Lexicon) -> str:
    """
    Универсально форматирует дату события.
    - Если дата отсутствует, возвращает 'Дата не указана'.
    - Если время 00:00, возвращает только дату 'ДД.ММ.ГГГГ'.
    - Если время указано, возвращает 'ДД.ММ.ГГГГ в ЧЧ:ММ'.
    """
    if not date_obj:
        return lexicon.get('date_not_specified')
    
    # Проверяем, является ли время "нулевым"
    if date_obj.hour == 0 and date_obj.minute == 0 and date_obj.second == 0:
        return date_obj.strftime('%d.%m.%Y')
    else:
        # Используем 'в' для русского и 'at' для английского
        separator = ' в ' if lexicon.lang_code == 'ru' else ' at '
        return date_obj.strftime(f'%d.%m.%Y{separator}%H:%M')