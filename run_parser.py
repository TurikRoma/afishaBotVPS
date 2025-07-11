import asyncio
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select

# --- 1. ОБНОВЛЯЕМ ИМПОРТЫ ---
from app.database.models import async_session,Artist
from parsers.configs import ALL_CONFIGS

# Импортируем наш новый парсер и даем ему понятное имя
from parsers.test_parser import parse_site as parse_kvitki_playwright
# Импортируем AI функцию
from parsers.test_ai import getArtist, getArtistkvitki
# Импортируем НОВЫЕ функции для работы с БД
from app.database.requests import requests as rq # <-- Импортируем весь модуль requests

# Импортируем старые парсеры, если они нужны
from parsers.yandex_parser import parse as parse_yandex_afisha
from parsers.test_parser import parse_site as parse_kvitki_by # (Пример)



# --- 1. НАСТРОЙКА ЛОГИРОВАНИЯ ---
# Настраиваем логирование, чтобы сообщения выводились и в консоль, и в файл.
# Это нужно сделать один раз в самом начале главного скрипта.

# Создаем основной логгер
logger = logging.getLogger()
logger.setLevel(logging.INFO) # Устанавливаем минимальный уровень для записи

# Создаем форматтер, который будет добавлять время и уровень сообщения
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Создаем обработчик для вывода в файл 'logs.txt'
# mode='a' означает 'append' (дозапись в конец файла)
# encoding='utf-8' для корректной работы с кириллицей
file_handler = logging.FileHandler('logs.txt', mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)

# Создаем обработчик для вывода в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Добавляем оба обработчика к нашему логгеру
# Проверяем, чтобы не добавить обработчики повторно при перезапусках в IDE
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

async def populate_artists_if_needed(session):
    """
    Синхронизирует список артистов из artists.txt с базой данных.
    Добавляет только тех артистов, которых еще нет в БД, приводя имена к нижнему регистру.
    """
    logging.info("Синхронизация артистов из файла artists.txt с базой данных...")
    
    try:
        # 1. Читаем всех артистов из файла и приводим к нижнему регистру
        with open('artists.txt', 'r', encoding='utf-8') as f:
            artists_from_file = {line.strip().lower() for line in f if line.strip()}
        
        if not artists_from_file:
            logging.warning("Файл artists.txt пуст. Синхронизация не требуется.")
            return

        # 2. Получаем множество ВСЕХ артистов, которые уже есть в базе (тоже в нижнем регистре)
        # Это важно, если в базе вдруг оказались артисты в разном регистре.
        stmt = select(func.lower(Artist.name))
        existing_artists_result = await session.execute(stmt)
        existing_artists = set(existing_artists_result.scalars().all())

        # 3. Находим разницу - тех, кого нужно добавить
        artists_to_add = artists_from_file - existing_artists
        
        if not artists_to_add:
            logging.info("Все артисты из файла уже присутствуют в базе данных.")
            return

        # 4. Добавляем в сессию только новых артистов
        logging.info(f"Найдено {len(artists_to_add)} новых артистов в файле. Добавляю в сессию...")
        for artist_name in artists_to_add:
            # Имена уже в нижнем регистре
            session.add(Artist(name=artist_name))
        
        # Мы не делаем commit, он будет общий в конце.
        # Просто добавляем объекты в сессию.
        logging.info(f"Успешно добавлено в сессию {len(artists_to_add)} новых артистов.")

    except FileNotFoundError:
        logging.error("ОШИБКА: Файл artists.txt не найден. Не могу синхронизировать артистов.")
    except Exception as e:
        logging.error(f"Произошла ошибка при заполнении таблицы артистов: {e}", exc_info=True)
        # Важно пробросить исключение или обработать его, чтобы не продолжать с неполными данными
        raise   

# --- 2. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПАРСИНГА ДАТЫ ---
def parse_datetime_from_str(date_str: str) -> datetime | None:
    """
    Парсит дату из строки, пробуя несколько известных форматов,
    включая относительные даты ("сегодня", "завтра") и даты без года.
    """
    if not isinstance(date_str, str):
        return None

    cleaned_str = date_str.lower().strip()
    now = datetime.now()
    
    # --- Попытка 1: Относительные даты "сегодня" и "завтра" ---
    try:
        time_part = "00:00"
        time_match = re.search(r'(\d{1,2}:\d{2})', cleaned_str)
        if time_match:
            time_part = time_match.group(1)

        target_date = None
        if "сегодня" in cleaned_str:
            target_date = now.date()
        elif "завтра" in cleaned_str:
            target_date = (now + timedelta(days=1)).date()

        if target_date:
            return datetime.strptime(f"{target_date.strftime('%Y-%m-%d')} {time_part}", "%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        pass

    # --- Попытка 2: Формат '24 июля 2024, 19:00' (Kvitki) или '28 июня 2024' ---
    try:
        months_map = {'января': '01', 'февраля': '02', 'марта': '03', 'апреля': '04', 'мая': '05', 'июня': '06', 'июля': '07', 'августа': '08', 'сентября': '09', 'октября': '10', 'ноября': '11', 'декабря': '12'}
        processed_str = cleaned_str
        for name, num in months_map.items():
            if name in processed_str:
                processed_str = processed_str.replace(name, num)
                
                # Убираем дни недели и лишние символы
                processed_str = re.sub(r'^[а-я]{2},?\s*', '', processed_str) # "сб," -> ""
                processed_str = re.sub(r'[,.]| г', '', processed_str)
                processed_str = re.sub(r'\s+', ' ', processed_str).strip()

                # Сценарий А: Есть год ('28 06 2024 19:00')
                if re.search(r'\d{4}', processed_str):
                    if ':' in processed_str:
                        return datetime.strptime(processed_str, "%d %m %Y %H:%M")
                    else:
                        return datetime.strptime(processed_str, "%d %m %Y")
                
                # Сценарий Б: Нет года ('28 06 19:00')
                else:
                    format_str = "%d %m %H:%M" if ':' in processed_str else "%d %m"
                    # Парсим без года (по умолчанию будет 1900-й год)
                    temp_date = datetime.strptime(processed_str, format_str)
                    
                    # Заменяем год на текущий
                    final_date = temp_date.replace(year=now.year)
                    
                    # Если получившаяся дата уже прошла в этом году (например, сегодня июль, а событие в июне),
                    # значит, оно будет в следующем году.
                    if final_date < now:
                        final_date = final_date.replace(year=now.year + 1)
                    
                    return final_date

    except (ValueError, IndexError):
        pass

    # --- Попытка 3: Формат 'Сб 28.06.2025' (старый формат Яндекса) ---
    try:
        # Убираем день недели (первое слово и возможную запятую)
        date_part = re.sub(r'^[а-яА-Я]+,?\s*', '', cleaned_str)
        return datetime.strptime(date_part, "%d.%m.%Y")
    except (ValueError, IndexError):
        pass

    logging.warning(f"Не удалось распознать дату ни одним из известных форматов: '{date_str}'")
    return None

# --- 3. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ ГОРОДА ---
# Твоя функция, немного оптимизированная.
def extract_city_from_place(place_string: str) -> str:
    """
    Извлекает название города из строки, сначала проверяя известные города,
    а затем пытаясь извлечь последнее слово из очищенной строки.
    """
    if not place_string:
        return "Минск"

    # 1. Самый надежный способ: ищем точное вхождение известного города
    known_cities = ["Минск", "Брест", "Витебск", "Гомель", "Гродно", "Могилев", "Лида", "Молодечно", "Сморгонь", "Несвиж"]
    for city in known_cities:
        if city.lower() in place_string.lower():
            return city
    
    # 2. Если не нашли, пытаемся извлечь из очищенной строки
    # Удаляем скобки, запятые, точки и лишние пробелы
    cleaned_string = re.sub(r'[(),.]', ' ', place_string)
    parts = cleaned_string.strip().split()

    if len(parts) > 1 and parts[-1].isalpha():
        # Берем последнее слово, если оно состоит только из букв
        return parts[-1].capitalize()

    # 3. Город по умолчанию, если ничего не помогло
    return "Минск"

# --- 4. ОСНОВНАЯ ЛОГИКА ОРКЕСТРАТОРА (ПОЛНОСТЬЮ ПЕРЕПИСАНА) ---
async def process_all_sites():
    logging.info("==============================================")
    logging.info("=== НАЧАЛО НОВОГО ЦИКЛА ПОЛНОГО ПАРСИНГА ===")
    logging.info("==============================================")

    parser_mapping = {
        'yandex_afisha': parse_yandex_afisha,
        'kvitki_by': parse_kvitki_by,
    }

    # --- Этап 1: Сбор и НОРМАЛИЗАЦИЯ данных ---
    all_normalized_events = []
    for site_config in ALL_CONFIGS:
        parser_key = site_config.get('parser_key')
        parser_func = parser_mapping.get(parser_key)
        
        if not parser_func:
            logging.warning(f"Пропущен конфиг '{site_config.get('site_name')}' с ключом парсера: '{parser_key}'")
            continue
            
        logging.info(f"\n--- Запуск парсера '{parser_key}' для '{site_config.get('site_name')}' ---")
        try:
            events_from_site = await parser_func(site_config)
            
            for event_data in events_from_site:
                # Обогащаем данными из конфига
                event_data['event_type'] = site_config.get('event_type', 'Другое')
                event_data['country_name'] = site_config.get('country_name')
                event_data['city_name'] = site_config.get('city_name') or extract_city_from_place(event_data.get('place'))

                # Парсим дату, если она еще не распарсена
                if 'time_start' not in event_data:
                    event_data['time_start'] = parse_datetime_from_str(event_data.get('time_string'))
                    event_data['time_end'] = None
                
                # Обрабатываем билеты
                if 'tickets_available' in event_data:
                    count = event_data.pop('tickets_available')
                    event_data['tickets_info'] = f"{count} билетов" if count else "Нет в наличии"

                all_normalized_events.append(event_data)
        except Exception as e:
            logging.error(f"Парсер '{parser_key}' завершился с ошибкой: {e}", exc_info=True)

    if not all_normalized_events:
        logging.info("Ни один парсер не вернул событий. Завершаю работу.")
        return

    # --- Этап 2: Работа с БД ---
    logging.info(f"\n--- Всего обработано {len(all_normalized_events)} событий. Начинаю синхронизацию с БД. ---")
    
    async with async_session() as session:
        try:
            await populate_artists_if_needed(session)

            # Массовая проверка
            signatures = [(e.get('title'), e.get('time_start')) for e in all_normalized_events if e.get('title') and e.get('time_start')]
            existing_events_map = await rq.find_events_by_signatures_bulk(session, signatures)
            
            events_to_create, events_updated_count = [], 0
            for event_data in all_normalized_events:
                sig = (event_data.get('title'), event_data.get('time_start'))
                if sig in existing_events_map:
                    event_data['event_id'] = existing_events_map[sig]
                    await rq.update_event_details(session, event_data['event_id'], event_data)
                    events_updated_count += 1
                else:
                    events_to_create.append(event_data)
            
            logging.info(f"Разделение. Новых: {len(events_to_create)}, на обновление: {events_updated_count}.")

            # Обработка новых событий
            if events_to_create:
                # Определяем артистов для всех новых событий
                for event in events_to_create:
                    # Яндекс парсер уже сам определил артистов, его не трогаем
                    if event.get('artists'): 
                        continue
                    
                    # Для Kvitki и других вызываем AI
                    if event.get('full_description'):
                        artists = await getArtistkvitki(event['full_description'])
                        event['artists'] = artists if artists else [event['title']]
                    else:
                        event['artists'] = [event['title']]
                
                # Массово создаем всех артистов
                all_artist_names = {name.lower() for e in events_to_create for name in e.get('artists', []) if name}
                artists_map = {}
                if all_artist_names:
                    artists_map = await rq.get_or_create_artists_by_name(session, list(all_artist_names))

                # Массово создаем все события
                events_created_count = 0
                for event_data in events_to_create:
                    if await rq.create_event_with_artists(session, event_data, artists_map):
                        events_created_count += 1
            
                await session.commit()
                logging.info("Изменения успешно сохранены.")
                logging.info(f"Новых событий создано: {events_created_count}")
                logging.info(f"Существующих событий обновлено: {events_updated_count}")

        except Exception as e:
            logging.error(f"Критическая ошибка в процессе обработки. Откатываю транзакцию. Ошибка: {e}", exc_info=True)
            await session.rollback()

    logging.info("\n--- Обработка завершена ---")
    logging.info(f"Новых событий создано: {events_created_count}")
    logging.info(f"Существующих событий обновлено: {events_updated_count}")

if __name__ == "__main__":
    asyncio.run(process_all_sites())