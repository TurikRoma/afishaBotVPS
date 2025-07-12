import asyncio
import base64
import io
import logging
import re
import random
import time
from datetime import datetime, timedelta
from calendar import monthrange
import requests
from PIL import Image
import tempfile
import shutil

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from selenium.webdriver.common.action_chains import ActionChains
from app.database import requests as rq
from app.database.models import async_session

# Импортируем AI функцию
from parsers.test_ai import getArtist

# --- 1. ИНИЦИАЛИЗАЦИЯ И КОНСТАНТЫ ---
logger = logging.getLogger()
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# --- 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def parse_datetime_range(date_str: str) -> tuple[datetime | None, datetime | None]:
    if not isinstance(date_str, str) or not date_str.strip(): return None, None
    cleaned_str, now = date_str.lower().strip(), datetime.now()
    months_map = {
        'январь': 1, 'января': 1, 'февраль': 2, 'февраля': 2, 'март': 3, 'марта': 3, 'апрель': 4, 'апреля': 4,
        'май': 5, 'мая': 5, 'июнь': 6, 'июня': 6, 'июль': 7, 'июля': 7, 'август': 8, 'августа': 8,
        'сентябрь': 9, 'сентября': 9, 'октябрь': 10, 'октября': 10, 'ноябрь': 11, 'ноября': 11, 'декабрь': 12, 'декабря': 12,
    }
    def _construct_date(day, month_num, year=None, time_str="00:00"):
        if year is None: year = now.year
        hour, minute = map(int, time_str.split(':'))
        try:
            if datetime(now.year, month_num, day) < now.replace(hour=0, minute=0, second=0, microsecond=0):
                year = now.year + 1
            return datetime(year, month_num, day, hour, minute)
        except ValueError: return None
    if "постоянно" in cleaned_str: return None, None
    time_match = re.search(r'(\d{1,2}:\d{2})', cleaned_str)
    time_part = time_match.group(1) if time_match else "00:00"
    if time_match: cleaned_str = cleaned_str.replace(time_match.group(0), '').strip()
    if match := re.search(r'с\s+(\d+)\s+по\s+(\d+)\s+([а-я]+)', cleaned_str):
        d_start, d_end, m_name = int(match.group(1)), int(match.group(2)), match.group(3)
        if m_name in months_map: return _construct_date(d_start, months_map[m_name], time_str=time_part), _construct_date(d_end, months_map[m_name], time_str="23:59")
    if match := re.search(r'(\d+)\s+и\s+(\d+)\s+([а-я]+)', cleaned_str):
        d_start, d_end, m_name = int(match.group(1)), int(match.group(2)), match.group(3)
        if m_name in months_map: return _construct_date(d_start, months_map[m_name], time_str=time_part), _construct_date(d_end, months_map[m_name], time_str="23:59")
    full_matches = list(re.finditer(r'(\d{1,2})\s+([а-я]+)', cleaned_str))
    if len(full_matches) > 1:
        all_found_dates = []
        last_month_num = None
        for m in full_matches:
            day, month_name = int(m.group(1)), m.group(2)
            if month_name in months_map:
                month_num = months_map[month_name]
                if date_obj := _construct_date(day, month_num, time_str=time_part): all_found_dates.append(date_obj)
                last_month_num = month_num
        temp_str = re.sub(r'(\d{1,2})\s+([а-я]+)', '', cleaned_str)
        if last_month_num:
            for day_str in re.findall(r'(\d+)', temp_str):
                if date_obj := _construct_date(int(day_str), last_month_num, time_str=time_part): all_found_dates.append(date_obj)
        if all_found_dates:
            start_date, end_date = min(all_found_dates), max(all_found_dates)
            if start_date != end_date: end_date = end_date.replace(hour=23, minute=59)
            return start_date, end_date
    if match := re.search(r'(\d+)\s+([а-я]+)', cleaned_str):
        d, m_name = int(match.group(1)), match.group(2)
        if m_name in months_map:
            single_date = _construct_date(d, months_map[m_name], time_str=time_part)
            return single_date, single_date
    unique_months = sorted(list(set(num for name, num in months_map.items() if name in cleaned_str)))
    if unique_months:
        start_m, end_m = min(unique_months), max(unique_months)
        start_date = _construct_date(1, start_m)
        if not start_date: return None, None
        end_year = start_date.year + (1 if end_m < start_m else 0)
        _, last_day = monthrange(end_year, end_m)
        end_date = datetime(end_year, end_m, last_day, 23, 59)
        return start_date, end_date
    logger.warning(f"Не удалось распознать дату из строки: '{date_str}'")
    return None, None

def _solve_token_captcha(driver: webdriver.Chrome, api_key: str) -> bool:
    """Решает Smart Captcha, которая требует текстовый токен."""
    logger.info("  -> Обнаружена токен-капча. Решаю через RuCaptcha...")
    try:
        sitekey = driver.find_element(By.ID, "captcha-container").get_attribute("data-sitekey")
        if not sitekey:
            logger.error("Не удалось найти 'data-sitekey' для токен-капчи.")
            return False
            
        payload = {
            'method': 'yandex', 
            'key': api_key, 
            'sitekey': sitekey, 
            'pageurl': driver.current_url, 
            'json': 1
        }
        response = requests.post("http://rucaptcha.com/in.php", data=payload, timeout=20).json()
        if response.get("status") != 1:
            logger.error(f"RuCaptcha вернула ошибку при отправке задания: {response}")
            return False
            
        captcha_id = response["request"]
        logger.info(f"    - Задание на получение токена отправлено. ID: {captcha_id}.")
        
        for _ in range(24): # Ожидаем до 2 минут
            time.sleep(5)
            result = requests.get(f"http://rucaptcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1", timeout=20).json()
            if result.get("status") == 1:
                logger.info("    - ✅ Токен получен!")
                token_input = driver.find_element(By.CSS_SELECTOR, 'input[name="smart-token"]')
                driver.execute_script("arguments[0].setAttribute('value', arguments[1])", token_input, result["request"])
                driver.find_element(By.ID, "j-captcha-form").submit()
                time.sleep(3) # Пауза после отправки
                return True
                
        logger.warning("    - ⏳ Время ожидания токена истекло.")
        return False
    except Exception as e:
        logger.error(f"Ошибка при решении токен-капчи: {e}")
        return False

def _solve_checkbox_captcha(driver: webdriver.Chrome) -> bool:
    """Решает простую Checkbox Captcha ('I'm not a robot')."""
    logger.info("  -> Обнаружен шаг 1: Checkbox Captcha. Пытаюсь кликнуть...")
    try:
        checkbox = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[role="checkbox"]'))
        )
        checkbox.click()
        logger.info("  -> ✅ Успешно кликнул по чекбоксу.")
        time.sleep(3) # Даем время на загрузку следующего шага
        return True
    except Exception as e:
        logger.error(f"Ошибка при клике по Checkbox Captcha: {e}")
        return False
    
def _solve_grid_captcha(driver: webdriver.Chrome, api_key: str) -> bool:
    """Решает визуальную Smart Captcha с кликами по координатам."""
    logger.info("  -> Обнаружена визуальная Grid Captcha. Решаю через RuCaptcha...")
    try:
        # 1. Находим элементы и делаем скриншоты в base64
        image_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.AdvancedCaptcha-Image'))
        )
        image_b64 = image_element.screenshot_as_base64
        
        instruction_element = driver.find_element(By.CSS_SELECTOR, '.AdvancedCaptcha-TaskIcons')
        instruction_b64 = instruction_element.screenshot_as_base64

        # 2. Отправляем задание в RuCaptcha
        payload = {
            'method': 'grid', 
            'key': api_key, 
            'body': image_b64,
            'imginstructions': instruction_b64, 
            'json': 1
        }
        resp = requests.post("http://rucaptcha.com/in.php", data=payload, timeout=20).json()
        if resp.get("status") != 1:
            logger.error(f"RuCaptcha вернула ошибку при отправке Grid-задания: {resp}")
            return False
        
        captcha_id = resp["request"]
        logger.info(f"    - Задание на распознавание координат отправлено. ID: {captcha_id}.")
        
        # 3. Ожидаем результат (координаты)
        for _ in range(24): # Ожидаем до 2 минут
            time.sleep(5)
            result = requests.get(f"http://rucaptcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1", timeout=20).json()
            if result.get("status") == 1:
                coordinates_str = result["request"]
                logger.info(f"    - ✅ Координаты получены: {coordinates_str}")
                
                # 4. Имитируем клики по координатам
                actions = ActionChains(driver)
                for coord in coordinates_str.split(';'):
                    if 'click' in coord:
                        try:
                            x, y = map(int, coord.split(':')[1:])
                            # Кликаем по картинке со смещением от ее левого верхнего угла
                            actions.move_to_element_with_offset(image_element, x, y).click()
                            time.sleep(random.uniform(0.3, 0.7))
                        except (ValueError, IndexError):
                            logger.error(f"Некорректный формат координат от RuCaptcha: {coord}")
                            continue
                
                actions.perform()
                
                # 5. Нажимаем "Submit"
                driver.find_element(By.CSS_SELECTOR, '.Button[type="submit"]').click()
                time.sleep(5) # Пауза после отправки
                return True
                
        logger.warning("    - ⏳ Время ожидания координат истекло.")
        return False
    except Exception as e:
        logger.error(f"Ошибка при решении Grid Captcha: {e}")
        return False
    
def _solve_image_captcha(driver: webdriver.Chrome, api_key: str, image_container_selector: str, instruction_selector: str) -> bool:
    """
    Универсальный решатель для визуальных капч.
    Использует multipart/form-data для более надежной отправки изображений.
    """
    logger.info("  -> Обнаружена визуальная капча. Решаю через RuCaptcha (метод отправки 'files')...")
    try:
        # --- ШАГ 1: ПОЛУЧЕНИЕ ИЗОБРАЖЕНИЙ (остается без изменений) ---
        image_container = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, image_container_selector)))
        instruction_element = driver.find_element(By.CSS_SELECTOR, instruction_selector)
        time.sleep(2)
        loc_image, size_image = image_container.location_once_scrolled_into_view, image_container.size
        loc_instr, size_instr = instruction_element.location_once_scrolled_into_view, instruction_element.size
        full_screenshot_bytes = driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(full_screenshot_bytes))
        cropped_image = img.crop((loc_image['x'], loc_image['y'], loc_image['x'] + size_image['width'], loc_image['y'] + size_image['height']))
        cropped_instruction = img.crop((loc_instr['x'], loc_instr['y'], loc_instr['x'] + size_instr['width'], loc_instr['y'] + size_instr['height']))
        
        # Конвертируем в байты, а не в base64
        with io.BytesIO() as img_byte_arr:
            cropped_image.save(img_byte_arr, format='PNG')
            image_bytes = img_byte_arr.getvalue()
        with io.BytesIO() as instr_byte_arr:
            cropped_instruction.save(instr_byte_arr, format='PNG')
            instruction_bytes = instr_byte_arr.getvalue()

        # --- ШАГ 2: ОТПРАВКА В RUCAPTCHA ЧЕРЕЗ MULTIPART/FORM-DATA ---
        
        # Параметры, которые не являются файлами
        params = {
            'method': 'grid',
            'key': api_key,
            'json': 1
        }
        # Файлы, которые мы отправляем
        files = {
            'file': ('image.png', image_bytes, 'image/png'),
            'imginstructions': ('instruction.png', instruction_bytes, 'image/png')
        }
        
        logger.info("    - Отправляю изображения в RuCaptcha как файлы...")
        resp = requests.post("http://rucaptcha.com/in.php", params=params, files=files, timeout=30).json()
        
        if resp.get("status") != 1:
            logger.error(f"RuCaptcha вернула ошибку: {resp}"); return False
        
        captcha_id = resp["request"]
        logger.info(f"    - Задание на распознавание координат отправлено. ID: {captcha_id}.")
        
        # --- ШАГ 3: ОЖИДАНИЕ И ОБРАБОТКА (остается без изменений) ---
        for _ in range(24):
            time.sleep(5)
            result = requests.get(f"http://rucaptcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1", timeout=20).json()
            if result.get("status") == 1:
                coordinates_str = result["request"]
                logger.info(f"    - ✅ Координаты получены: {coordinates_str}")
                
                actions = ActionChains(driver)
                image_container = driver.find_element(By.CSS_SELECTOR, image_container_selector)
                for coord in coordinates_str.split(';'):
                    if 'click' in coord:
                        try:
                            x, y = map(int, coord.split(':')[1:])
                            actions.move_to_element_with_offset(image_container, x, y).click()
                            time.sleep(random.uniform(0.3, 0.7))
                        except (ValueError, IndexError): continue
                
                actions.perform()
                
                # --- ФИНАЛЬНОЕ ИСПРАВЛЕНИЕ: ИСПОЛЬЗУЕМ НАДЕЖНЫЙ СЕЛЕКТОР ---
                logger.info("    - Нажимаю кнопку 'Submit'...")
                driver.find_element(By.CSS_SELECTOR, '[data-testid="submit"]').click()
                
                time.sleep(5) # Пауза после отправки
                return True
                
        logger.warning("    - ⏳ Время ожидания координат истекло.")
        return False
    except Exception as e:
        logger.error(f"Ошибка при решении визуальной капчи: {e}", exc_info=True)
        return False

def _handle_cookie_banner(driver: webdriver.Chrome):
    """
    Ищет и закрывает баннер о cookies, используя надежный клик через JavaScript,
    чтобы избежать ошибок 'element click intercepted'.
    """
    try:
        # 1. Ждем, пока элемент просто появится в DOM (не обязательно кликабельный)
        allow_button = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "gdpr-popup-v3-button-all"))
        )
        logger.info("🍪 Обнаружен баннер cookies. Использую JS-клик для закрытия.")
        
        # 2. Используем JavaScript для выполнения клика.
        # Этот метод работает, даже если элемент перекрыт другим.
        driver.execute_script("arguments[0].click();", allow_button)
        
        time.sleep(1) # Небольшая пауза после клика, чтобы баннер успел исчезнуть
        logger.info("🍪 Баннер cookies успешно закрыт.")
        
    except TimeoutException:
        # Если за 5 секунд баннер не появился, значит, его и нет.
        logger.info("🍪 Баннер cookies не найден, продолжаю.")

def _wait_for_page_load_and_solve_captcha(driver: webdriver.Chrome, rucaptcha_api_key: str | None, content_selector: str):
    """Главный обработчик. Сначала закрывает cookie-баннер, затем решает капчи. С улучшенной диагностикой."""
    _handle_cookie_banner(driver)
    for attempt in range(5):
        # Увеличим время ожидания до 20 секунд на всякий случай
        wait = WebDriverWait(driver, 20) 
        logger.info(f"Ожидаю контент или капчу (шаг {attempt + 1})...")
        try:
            # Ожидаем появления одного из известных состояний страницы
            wait.until(lambda d: 
                d.find_elements(By.CSS_SELECTOR, content_selector) or 
                d.find_elements(By.CSS_SELECTOR, '.AdvancedCaptcha') or
                d.find_elements(By.ID, "captcha-container") or
                d.find_elements(By.CSS_SELECTOR, '[data-testid="checkbox-captcha"]')
            )
        except TimeoutException as e:
            # --- БЛОК ДИАГНОСТИКИ ---
            # Если мы так и не дождались ничего знакомого, сохраняем информацию и выходим.
            error_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"error_screenshot_{error_timestamp}.png"
            html_path = f"error_page_{error_timestamp}.html"
            
            logger.error("!!! Таймаут ожидания! Не найден ни контент, ни известная капча.")
            logger.error(f"URL на момент сбоя: {driver.current_url}")
            
            # Сохраняем скриншот
            driver.save_screenshot(screenshot_path)
            logger.error(f"Скриншот страницы сохранен в файл: {screenshot_path}")
            
            # Сохраняем HTML-код страницы
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.error(f"HTML-код страницы сохранен в файл: {html_path}")
            
            # Пробрасываем исключение дальше, чтобы остановить выполнение
            raise Exception(f"Не дождался ни контента, ни капчи на URL: {driver.current_url}") from e
        
        # --- Дальнейшая логика остается без изменений ---
        if driver.find_elements(By.CSS_SELECTOR, content_selector):
            logger.info("✅ Контент найден!"); return
        if captcha_container := driver.find_elements(By.CSS_SELECTOR, '.AdvancedCaptcha'):
            captcha_class = captcha_container[0].get_attribute("class")
            image_sel, instr_sel = ('.AdvancedCaptcha-ImageWrapper', '.TaskImage') if "AdvancedCaptcha_silhouette" in captcha_class else ('.AdvancedCaptcha-Image', '.AdvancedCaptcha-TaskIcons')
            logger.info(f"  -> Тип капчи определен: {'Silhouette' if 'silhouette' in captcha_class else 'Grid'}.")
            if not rucaptcha_api_key: raise Exception("Обнаружена визуальная капча, но нет ключа.")
            if not _solve_image_captcha(driver, rucaptcha_api_key, image_sel, instr_sel):
                raise Exception("Не удалось решить визуальную капчу.")
            continue
        if driver.find_elements(By.ID, "captcha-container"):
            if not rucaptcha_api_key: raise Exception("Обнаружена токен-капча, но нет ключа.")
            if not _solve_token_captcha(driver, rucaptcha_api_key): raise Exception("Не удалось решить токен-капчу.")
            continue
        if driver.find_elements(By.CSS_SELECTOR, '[data-testid="checkbox-captcha"]'):
            if not _solve_checkbox_captcha(driver): raise Exception("Не удалось нажать на Checkbox Captcha.")
            continue
            
    raise Exception("Не удалось пройти последовательность капч.")

# --- 3. НИЗКОУРОВНЕВЫЕ ПАРСЕРЫ ---

def _parse_list_sync(config: dict) -> list[dict]:
    site_name, base_url = config['site_name'], f"{config['url']}?date={datetime.now().strftime('%Y-%m-%d')}&period={config['period']}"
    rucaptcha_api_key, max_pages = config.get('RUCAPTCHA_API_KEY'), config.get('max_pages', 365)
    options = webdriver.ChromeOptions()
    # options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    user_data_dir = tempfile.mkdtemp()
    options.add_argument(f"--user-data-dir={user_data_dir}")
    all_events_data, seen_event_links = [], set()
    logger.info(f"Начинаю парсинг списка: {site_name}")
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        for page_num in range(1, max_pages + 1):
            current_url = f"{base_url}&page={page_num}"
            logger.info(f"Обрабатываю страницу {page_num}/{max_pages}: {current_url}")
            driver.get(current_url)
            try:
                _wait_for_page_load_and_solve_captcha(driver, rucaptcha_api_key, '[data-test-id="eventCard.root"]')
            except Exception as e:
                logger.error(f"Не удалось загрузить страницу или пройти капчу: {e}"); break
            soup = BeautifulSoup(driver.page_source, 'lxml')
            event_cards = soup.find_all("div", attrs={"data-test-id": "eventCard.root"})
            if not event_cards: break
            current_page_links = {link.get('href') for card in event_cards if (link := card.find("a", attrs={"data-test-id": "eventCard.link"}))}
            if not current_page_links.difference(seen_event_links):
                logger.info("Новых событий на странице не найдено. Завершаю."); break
            for card in event_cards:
                try:
                    href = (link_element.get('href') if (link_element := card.find("a", attrs={"data-test-id": "eventCard.link"})) else None)
                    if not href or href in seen_event_links: continue
                    seen_event_links.add(href)
                    title = card.find("h2", attrs={"data-test-id": "eventCard.eventInfoTitle"}).get_text(strip=True)
                    place, date_str = "Место не указано", "Дата не указана"
                    if details_list := card.find("ul", attrs={"data-test-id": "eventCard.eventInfoDetails"}):
                        items = details_list.find_all("li")
                        if len(items) > 0: date_str = items[0].get_text(strip=True)
                        if len(items) > 1: place = items[1].find('a').get_text(strip=True) if items[1].find('a') else items[1].get_text(strip=True)
                    price_min = None
                    if price_el := card.find("span", string=re.compile(r'от \d+')):
                        if price_match := re.search(r'\d+', price_el.get_text(strip=True).replace(' ', '')):
                            price_min = float(price_match.group(0))
                    time_start, time_end = parse_datetime_range(date_str)
                    all_events_data.append({
                        'event_type': config.get('event_type', 'Другое'), 'title': title, 'place': place, 'time_string': date_str,
                        'link': "https://afisha.yandex.ru" + href, 'price_min': price_min, 'time_start': time_start, 'time_end': time_end,
                        'price_max': None, 'tickets_info': None, 'full_description': None, 'artists': []
                    })
                except Exception as e: logger.warning(f"Ошибка парсинга карточки: {e}")
            time.sleep(random.uniform(2.0, 4.0))
    except Exception as e: logger.error(f"Критическая ошибка в парсере списка: {e}", exc_info=True)
    finally:
        if driver: driver.quit()
        # Удаляем временную директорию профиля
        shutil.rmtree(user_data_dir, ignore_errors=True)
    logger.info(f"Парсер списка завершен. Найдено уникальных событий: {len(all_events_data)}")
    return all_events_data


async def _enrich_details_async(events_to_process: list[dict], rucaptcha_api_key: str | None) -> list[dict]:
    if not events_to_process: return []
    logger.info(f"Начинаю детальный парсинг для {len(events_to_process)} событий...")
    options = webdriver.ChromeOptions()
    # options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    # Добавляем уникальный user-data-dir
    user_data_dir = tempfile.mkdtemp()
    options.add_argument(f"--user-data-dir={user_data_dir}")
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        for i, event in enumerate(events_to_process):
            link = event.get('link')
            if not link: continue
            logger.info(f"  ({i+1}/{len(events_to_process)}) Обрабатываю детали: {event.get('title')}")
            try:
                driver.get(link)
                _wait_for_page_load_and_solve_captcha(driver, rucaptcha_api_key, '[data-test-id*="title"]')
                
                # Небольшая пауза для прогрузки динамических элементов
                time.sleep(random.uniform(1.5, 2.5)) 

                if "afisha.yandex.ru" not in driver.current_url or "error" in driver.current_url:
                    logger.warning(f"    -> Редирект или ошибка на странице: {driver.current_url}. Пропускаю."); continue
                
                # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
                # Шаг 1: Ищем чипы с исполнителями. НЕ используем 'continue'.
                try:
                    if performer_chips := driver.find_elements(By.CSS_SELECTOR, 'a[data-test-id="personChip.root"]'):
                        event['artists'] = [chip.text.strip() for chip in performer_chips if chip.text.strip()]
                        logger.info(f"    -> Найдены прямые исполнители (чипы): {event['artists']}")
                except NoSuchElementException:
                    logger.info("    -> Чипы с исполнителями не найдены.")

                # Шаг 2: Ищем описание. Этот блок теперь выполняется ВСЕГДА.
                try:
                    # Сначала кликаем "Читать полностью", если кнопка есть
                    try:
                        more_button = driver.find_element(By.CSS_SELECTOR, '[data-test-id="eventInfo.more"]')
                        driver.execute_script("arguments[0].click();", more_button)
                        time.sleep(0.5) # Пауза, чтобы текст успел раскрыться
                    except NoSuchElementException:
                        pass # Кнопки нет, значит, описание полное или его нет вообще

                    description_element = driver.find_element(By.CSS_SELECTOR, '[data-test-id="eventInfo.description"]')
                    event['full_description'] = description_element.text.strip()
                    logger.info("    -> Найдено и сохранено полное описание.")
                except NoSuchElementException:
                    logger.warning("    -> Полное описание на странице не найдено.")
            
            except (TimeoutException, WebDriverException) as e:
                logger.error(f"    -> Ошибка при обработке страницы {link}. Пропускаю. Ошибка: {str(e).splitlines()[0]}")
    
    except Exception as e:
        logger.error(f"Критическая ошибка в парсере деталей: {e}", exc_info=True)
    finally:
        if driver: driver.quit()
        # Удаляем временную директорию профиля
        shutil.rmtree(user_data_dir, ignore_errors=True)
    logger.info("Детальный парсинг завершен.")
    return events_to_process

# --- 4. ГЛАВНАЯ ФУНКЦИЯ-ОРКЕСТРАТОР ---

async def parse(config: dict) -> list[dict]:
    logger.info(f"--- НАЧАЛО ПОЛНОГО ЦИКЛА ПАРСИНГА ДЛЯ: {config['site_name']} ---")
    loop = asyncio.get_running_loop()
    
    # Этап 1: Получаем сырой список событий
    raw_events = await loop.run_in_executor(None, _parse_list_sync, config)
    if not raw_events:
        logger.warning("Парсер списка не вернул событий. Завершаю.")
        return []
    logger.info(f"Этап 1: Успешно собрано {len(raw_events)} сырых событий.")

    # Этап 2: Разделяем события на спортивные и остальные
    # В реальном коде здесь будет проверка по БД, чтобы не обрабатывать уже существующие
    sport_events_to_process, other_events_to_process = [], []
    for event in raw_events:
        # Здесь должна быть ваша проверка: if not event_exists_in_db(event['link']):
        if event.get('event_type') == 'Спорт':
            sport_events_to_process.append(event)
        else:
            other_events_to_process.append(event)
            
    logger.info(f"Этап 2: Разделение. Спорт на обработку: {len(sport_events_to_process)}, Остальное: {len(other_events_to_process)}.")
    
    final_results = []

    # --- Этап 3.1: Обработка СПОРТИВНЫХ событий (БЕЗ перехода на страницы) ---
    if sport_events_to_process:
        logger.info("Этап 3.1: Обработка спортивных событий...")
        for event in sport_events_to_process:
            logger.info(f"  -> Для '{event['title']}': вызываю AI для анализа заголовка.")
            artists = await getArtist(event['title'])
            event['artists'] = artists if artists else [event['title']] # Fallback на заголовок
            final_results.append(event)

    # --- Этап 3.2: Обработка НЕ-СПОРТИВНЫХ событий (С переходом на страницы) ---
    if other_events_to_process:
        logger.info("Этап 3.2: Обогащение данных для остальных событий (концерты и т.д.)...")
        # Сначала заходим на страницы и ищем чипы/описание
        enriched_events = await _enrich_details_async(other_events_to_process, config.get('RUCAPTCHA_API_KEY'))
        
        logger.info("Этап 3.3: Финальная обработка и вызов AI (при необходимости)...")
        for event in enriched_events:
            # Сценарий 1: Исполнители найдены на странице в виде чипов.
            if event.get('artists'):
                logger.info(f"  -> Для '{event['title']}': найдены прямые исполнители (чипы): {event['artists']}. AI не используется.")
            
            # Сценарий 2: Исполнители не найдены, но есть описание. Используем AI.
            elif event.get('full_description'):
                logger.info(f"  -> Для '{event['title']}': чипы не найдены. Вызываю AI для анализа ОПИСАНИЯ.")
                
                # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ ---
                # Передаём в AI только описание, как вы и просили.
                ai_artists = await getArtist(event['full_description'])
                
                if ai_artists:
                    logger.info(f"    - AI успешно извлек артистов из описания: {ai_artists}")
                    event['artists'] = ai_artists
                else:
                    # Fallback-логика, если AI ничего не нашел в описании
                    logger.warning(f"    - AI не смог извлечь артистов из описания. В качестве исполнителя используется заголовок.")
                    event['artists'] = [event.get('title')]
            
            # Сценарий 3 (Fallback): Не найдено ни чипов, ни описания.
            else:
                logger.warning(f"  -> Для '{event['title']}': не найдено ни прямых исполнителей, ни описания. Используется заголовок.")
                event['artists'] = [event.get('title')]
            
            final_results.append(event)
            
    logger.info(f"--- ПОЛНЫЙ ЦИКЛ ПАРСИНГА ЗАВЕРШЕН. Итого событий: {len(final_results)} ---")
    return final_results

# --- 5. БЛОК ДЛЯ АВТОНОМНОГО ЗАПУСКА И ТЕСТИРОВАНИЯ ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    TEST_CONFIG_SPORT = {
        'site_name': 'Yandex.Afisha (Спорт) - ТЕСТ', 'url': 'https://afisha.yandex.ru/moscow/sport',
        'event_type': 'Спорт', 'period': 365, 'max_pages': 2,
        'RUCAPTCHA_API_KEY': '863b567c1d376398b65ad121498f89a1'
    }
    TEST_CONFIG_CONCERT = {
        'site_name': 'Yandex.Afisha (Концерты) - ТЕСТ', 'url': 'https://afisha.yandex.ru/moscow/concert',
        'event_type': 'Концерт', 'period': 365, 'max_pages': 2,
        'RUCAPTCHA_API_KEY': '863b567c1d376398b65ad121498f89a1'
    }
    async def run_tests():
        print("\n" + "="*50 + "\n" + "="*15 + " ЗАПУСК ДЛЯ СПОРТА " + "="*15 + "\n" + "="*50)
        sport_results = await parse(TEST_CONFIG_SPORT)
        print("\n\n--- ✨✨✨ ИТОГОВЫЙ РЕЗУЛЬТАТ (СПОРТ) ✨✨✨ ---")
        if sport_results:
            for i, event in enumerate(sport_results, 1):
                start_str = event['time_start'].strftime('%Y-%m-%d %H:%M') if event['time_start'] else 'N/A'
                end_str = event['time_end'].strftime('%Y-%m-%d %H:%M') if event['time_end'] else 'N/A'
                print(f"\n--- Спорт. событие #{i} ---\n  Название: {event.get('title')}\n  Ссылка: {event.get('link')}\n  Место: {event.get('place')}\n  Цена от: {event.get('price_min')}\n  Исходная дата: {event.get('time_string')}\n  НАЧАЛО: {start_str}\n  КОНЕЦ: {end_str}\n  КОМАНДЫ (из title): {event.get('artists')}")
        else: print("Спортивные события не найдены.")
        print("\n" + "="*50 + "\n" + "="*15 + " ЗАПУСК ДЛЯ КОНЦЕРТОВ " + "="*13 + "\n" + "="*50)
        concert_results = await parse(TEST_CONFIG_CONCERT)
        print("\n\n--- ✨✨✨ ИТОГОВЫЙ РЕЗУЛЬТАТ (КОНЦЕРТЫ) ✨✨✨ ---")
        if concert_results:
            for i, event in enumerate(concert_results, 1):
                 start_str = event['time_start'].strftime('%Y-%m-%d %H:%M') if event['time_start'] else 'N/A'
                 end_str = event['time_end'].strftime('%Y-%m-%d %H:%M') if event['time_end'] else 'N/A'
                 desc = event.get('full_description')
                 desc_short = (desc[:100] + '...') if desc and len(desc) > 100 else desc
                 print(f"\n--- Концерт. событие #{i} ---\n  Название: {event.get('title')}\n  Ссылка: {event.get('link')}\n  Место: {event.get('place')}\n  Цена от: {event.get('price_min')}\n  Исходная дата: {event.get('time_string')}\n  НАЧАЛО: {start_str}\n  КОНЕЦ: {end_str}\n  АРТИСТЫ: {event.get('artists')}\n  ОПИСАНИЕ: {desc_short if desc_short else 'Нет'}")
        else: print("Концерты не найдены.")
    asyncio.run(run_tests())
