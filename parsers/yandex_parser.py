import asyncio
import base64
import io
import logging
import os
import re
import random
import time
from datetime import datetime, timedelta
from calendar import monthrange
import requests
from PIL import Image

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from selenium.webdriver.common.action_chains import ActionChains
from app.database.requests import requests as rq
from app.database.models import async_session

# Импортируем AI функцию
from parsers.test_ai import getArtist

import tempfile

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
    Улучшенная версия решателя, которая использует JavaScript для симуляции кликов,
    чтобы обойти проблемы с ActionChains.
    """
    logger.info("  -> Обнаружена визуальная капча. Решаю через RuCaptcha (метод JS-клик)...")
    try:
        # --- Шаги 1 и 2 (получение и отправка в RuCaptcha) остаются без изменений ---
        image_container = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, image_container_selector)))
        instruction_element = driver.find_element(By.CSS_SELECTOR, instruction_selector)
        time.sleep(2)
        loc_image, size_image = image_container.location_once_scrolled_into_view, image_container.size
        loc_instr, size_instr = instruction_element.location_once_scrolled_into_view, instruction_element.size
        full_screenshot_bytes = driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(full_screenshot_bytes))
        cropped_image = img.crop((loc_image['x'], loc_image['y'], loc_image['x'] + size_image['width'], loc_image['y'] + size_image['height']))
        cropped_instruction = img.crop((loc_instr['x'], loc_instr['y'], loc_instr['x'] + size_instr['width'], loc_instr['y'] + size_instr['height']))
        
        with io.BytesIO() as img_byte_arr:
            cropped_image.save(img_byte_arr, format='PNG')
            image_bytes = img_byte_arr.getvalue()
        with io.BytesIO() as instr_byte_arr:
            cropped_instruction.save(instr_byte_arr, format='PNG')
            instruction_bytes = instr_byte_arr.getvalue()

        params = {'method': 'grid', 'key': api_key, 'json': 1}
        files = {'file': ('image.png', image_bytes, 'image/png'), 'imginstructions': ('instruction.png', instruction_bytes, 'image/png')}
        
        resp = requests.post("http://rucaptcha.com/in.php", params=params, files=files, timeout=30).json()
        if resp.get("status") != 1:
            logger.error(f"RuCaptcha вернула ошибку: {resp}"); return False
        
        captcha_id = resp["request"]
        logger.info(f"    - Задание на распознавание координат отправлено. ID: {captcha_id}.")
        
        # --- Шаг 3: Ожидание и ОБРАБОТКА с помощью JavaScript ---
        for _ in range(24):
            time.sleep(5)
            result = requests.get(f"http://rucaptcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1", timeout=20).json()
            if result.get("status") == 1:
                coordinates_str = result["request"]
                logger.info(f"    - ✅ Координаты получены: {coordinates_str}")
                
                # --- НОВАЯ ЛОГИКА КЛИКОВ ЧЕРЕЗ JAVASCRIPT ---
                image_element = driver.find_element(By.CSS_SELECTOR, image_container_selector)
                
                # Получаем координаты самого элемента картинки на странице
                rect = driver.execute_script("return arguments[0].getBoundingClientRect();", image_element)
                
                for coord_str in coordinates_str.split(';'):
                    if 'click' in coord_str:
                        try:
                            x_offset, y_offset = map(int, coord_str.split(':')[1:])
                            
                            # Вычисляем абсолютные координаты клика на странице
                            click_x = rect['left'] + x_offset
                            click_y = rect['top'] + y_offset
                            
                            logger.info(f"      - Выполняю JS-клик по координатам: x={click_x}, y={click_y}")
                            
                            # Выполняем клик с помощью JS
                            driver.execute_script(f"document.elementFromPoint({click_x}, {click_y}).click();")
                            
                            # Небольшая пауза между кликами
                            time.sleep(random.uniform(0.4, 0.8))
                        except (ValueError, IndexError):
                            continue
                
                logger.info("    - Все JS-клики выполнены.")
                time.sleep(random.uniform(1.0, 1.5))
                
                # Нажимаем "Submit"
                submit_button = driver.find_element(By.CSS_SELECTOR, '[data-testid="submit"]')
                logger.info("    - Нажимаю кнопку 'Submit'...")
                driver.execute_script("arguments[0].click();", submit_button) # Тоже через JS для надежности
                
                logger.info("    - Пауза после отправки (5 секунд)...")
                time.sleep(5)
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
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    # Добавляем уникальный user-data-dir
    options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    all_events_data, seen_event_links = [], set()
    logger.info(f"Начинаю парсинг списка: {site_name}")
    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        
        # --- ВЫПОЛНЕНИЕ МАСКИРУЮЩЕГО СКРИПТА ---
        try:
    # Определяем путь к текущему файлу
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # Составляем путь к stealth.min.js
            stealth_path = os.path.join(current_dir, 'stealth.min.js')
            
            with open(stealth_path) as f:
                js_code = f.read()
                
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": js_code
            })
            logger.info("✅ Скрипт маскировки stealth.min.js успешно применен.")

        except FileNotFoundError:
            logger.error("❌ Не удалось найти файл stealth.min.js. Маскировка не будет применена.")
        except Exception as e:
            logger.error(f"❌ Ошибка при применении скрипта маскировки: {e}")
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
                    
                    # Парсим дату
                    time_start, time_end = parse_datetime_range(date_str)
                    
                    # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ---
                    # Если дата начала не определена (т.е. событие "постоянное"),
                    # мы просто пропускаем это событие и переходим к следующему.
                    if time_start is None:
                        logger.info(f"  -> Пропущено постоянное событие (без даты): '{title}'")
                        continue # <-- Пропускаем итерацию
                    # -------------------------

                    # Этот код выполнится только для событий с датой
                    event_dict = {
                        'event_type': config.get('event_type', 'Другое'), 
                        'title': title, 
                        'place': place,
                        'time_string': date_str,
                        'link': "https://afisha.yandex.ru" + href, 
                        'price_min': price_min, 
                        'time_start': time_start, 
                        'time_end': time_end,
                        'city_name': config.get('city_name'),
                        'country_name': config.get('country_name')
                        # Остальные поля будут добавлены позже, если нужно
                    }
                    all_events_data.append(event_dict)
                    
                except Exception as e: 
                    logger.warning(f"Ошибка парсинга карточки: {e}")    
            time.sleep(random.uniform(2.0, 4.0))
    except Exception as e: logger.error(f"Критическая ошибка в парсере списка: {e}", exc_info=True)
    finally:
        if driver: driver.quit()
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
    options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
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
    logger.info("Детальный парсинг завершен.")
    return events_to_process

# --- 4. ГЛАВНАЯ ФУНКЦИЯ-ОРКЕСТРАТОР ---

async def parse(config: dict) -> list[dict]:
    logger.info(f"--- НАЧАЛО ПОЛНОГО ЦИКЛА ПАРСИНГА ДЛЯ: {config['site_name']} ---")
    loop = asyncio.get_running_loop()

    # Этап 1: Получаем сырой список событий с сайта
    raw_events = await loop.run_in_executor(None, _parse_list_sync, config)
    if not raw_events:
        logger.warning("Парсер списка не вернул событий. Завершаю.")
        return []
    logger.info(f"Этап 1: Успешно собрано {len(raw_events)} сырых событий.")

    # Открываем сессию для всех операций с БД в рамках одного запуска
    async with async_session() as session:
        try:
            # Этап 2: Массовая проверка существования событий
            signatures = [
                (event['title'], event['time_start'])
                for event in raw_events if event.get('title') and event.get('time_start')
            ]
            existing_events_map = await rq.find_events_by_signatures_bulk(session, signatures)
            logger.info(f"Этап 2: Проверка в БД. Найдено {len(existing_events_map)} уже существующих событий.")

            # Этап 3: Разделение на новые и существующие
            events_to_create = []
            events_to_update = []
            for event in raw_events:
                sig = (event.get('title'), event.get('time_start'))
                if sig in existing_events_map:
                    event['event_id'] = existing_events_map[sig]
                    events_to_update.append(event)
                else:
                    events_to_create.append(event)
            
            logger.info(f"Этап 3: Разделение. Новых: {len(events_to_create)}, на обновление: {len(events_to_update)}.")

            # Этап 4: Обновление данных для существующих событий
            if events_to_update:
                logger.info(f"Этап 4: Обновление данных для {len(events_to_update)} существующих событий...")
                for event_data in events_to_update:
                    await rq.update_event_details(session, event_data['event_id'], event_data)

            # Этап 5: Полная обработка новых событий
            if events_to_create:
                logger.info(f"Этап 5: Обработка {len(events_to_create)} новых событий...")
                
                # 5.1. Определяем артистов для всех новых событий
                all_new_events_processed = []
                new_sport_events = [e for e in events_to_create if e.get('event_type') == 'Спорт']
                new_other_events = [e for e in events_to_create if e.get('event_type') != 'Спорт']

                for event in new_sport_events:
                    logger.info(f"  -> [Спорт] Анализ заголовка: '{event['title']}'")
                    artists = await getArtist(event['title'])
                    event['artists'] = artists if artists else [event['title']]
                    all_new_events_processed.append(event)

                if new_other_events:
                    enriched_events = await _enrich_details_async(new_other_events, config.get('RUCAPTCHA_API_KEY'))
                    for event in enriched_events:
                        if event.get('artists'):
                            logger.info(f"  -> [Концерт] Найдены чипы: {event['artists']}")
                        elif event.get('full_description'):
                            logger.info(f"  -> [Концерт] Анализ описания для: '{event['title']}'")
                            artists = await getArtist(event['full_description'])
                            event['artists'] = artists if artists else [event['title']]
                        else:
                            logger.warning(f"  -> [Концерт] Нет ни чипов, ни описания. Используется заголовок: '{event['title']}'")
                            event['artists'] = [event['title']]
                        all_new_events_processed.append(event)
                
                # 5.2. Собираем ВСЕХ уникальных артистов и создаем их ОДНИМ запросом
                all_artist_names = set()
                for event in all_new_events_processed:
                    for artist_name in event.get('artists', []):
                        if artist_name and artist_name.strip():
                            all_artist_names.add(artist_name.lower())
                
                artists_map = {}
                if all_artist_names:
                    logger.info(f"Этап 5.2: Массовое создание/проверка {len(all_artist_names)} артистов в БД...")
                    # Передаем session и получаем словарь с объектами артистов
                    artists_map = await rq.get_or_create_artists_by_name(session, list(all_artist_names))

                # 5.3. Создаем сами события в БД
                logger.info(f"Этап 5.3: Сохранение {len(all_new_events_processed)} новых событий в БД...")
                for event_data in all_new_events_processed:
                    # Передаем карту артистов в функцию создания
                    await rq.create_event_with_artists(session, event_data, artists_map)
            
            # Финальный коммит всех изменений (и обновлений, и созданий)
            await session.commit()
            logger.info("--- ПОЛНЫЙ ЦИКЛ ПАРСИНГА ЗАВЕРШЕН. Все изменения сохранены в БД. ---")

        except Exception as e:
            logger.error(f"Произошла ошибка в процессе парсинга и сохранения. Откатываю транзакцию. Ошибка: {e}", exc_info=True)
            await session.rollback()

    # Возвращаем все события, которые были в первоначальном списке (для отладки)
    return raw_events