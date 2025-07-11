import asyncio
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import logging

from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError

# --- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---
CONCURRENT_EVENTS = 5
logger = logging.getLogger()

# --- МОДЕЛЬ ДЛЯ СЫРЫХ ДАННЫХ ---
@dataclass
class EventData:
    link: str
    title: Optional[str] = None
    place: Optional[str] = None
    time_string: Optional[str] = None # <-- Переименовано для унификации
    full_description: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    tickets_available: Optional[int] = None
    status: str = "ok"


async def parse_single_event(browser: Browser, event_url: str) -> Dict:
    """Собирает ВСЕ сырые данные со страницы события, но НЕ вызывает AI."""
    page = None
    try:
        page = await browser.new_page()
        await page.goto(event_url, timeout=60000)

        # 1. Извлекаем базовую информацию из JSON на странице
        try:
            details_json = await page.evaluate('() => window.concertDetails')
        except Exception:
            raise ValueError("Не удалось найти 'window.concertDetails' на странице.")

        if not details_json:
            raise ValueError("Объект 'window.concertDetails' пуст.")

        title = details_json.get('title')
        place = details_json.get('venueDescription')
        time_string = details_json.get('localisedStartDate') # <-- Исходная строка с датой
        
        price_min_raw = details_json.get('minPrice')
        price_min = float(price_min_raw) if price_min_raw is not None else None
        
        price_max = None
        prices_str = details_json.get('prices', '')
        if prices_str:
            prices_list = [float(p) for p in re.findall(r'\d+\.?\d*', prices_str.replace(',', '.'))]
            if len(prices_list) > 1:
                price_max = max(prices_list)

        # 2. Извлекаем ПОЛНОЕ описание
        full_description = None
        description_selector = 'div.concert_details_description_description_inner'
        if await page.locator(description_selector).count() > 0:
            raw_text = await page.locator(description_selector).inner_text()
            lines = [line.strip() for line in raw_text.split('\n')]
            full_description = '\n'.join(line for line in lines if line)

        # 3. Получаем количество билетов
        tickets_available = 0
        shop_url_button = page.locator('button[data-shopurl]').first
        if await shop_url_button.count() > 0:
            shop_url = await shop_url_button.get_attribute('data-shopurl')
            # Переходим по ссылке для проверки билетов
            await page.goto(shop_url, timeout=60000)
            
            ticket_cells_selector = '[data-cy="price-zone-free-places"], .cdk-column-freePlaces'
            
            async def find_and_sum_tickets(search_context) -> Optional[int]:
                try:
                    await search_context.wait_for_selector(ticket_cells_selector, state='visible', timeout=15000)
                    await search_context.wait_for_timeout(500) # Даем время на прорисовку
                    all_counts_text = await search_context.locator(ticket_cells_selector).all_inner_texts()
                    if not all_counts_text: return 0
                    return sum(int(match.group(0)) for text in all_counts_text if (match := re.search(r'\d+', text)))
                except PlaywrightTimeoutError:
                    return None

            tickets_available = await find_and_sum_tickets(page)
            if tickets_available is None:
                for frame in page.frames[1:]:
                    frame_tickets = await find_and_sum_tickets(frame)
                    if frame_tickets is not None:
                        tickets_available = frame_tickets
                        break
            if tickets_available is None: tickets_available = 0
        
        event = EventData(
            link=event_url, # Сохраняем исходную ссылку, а не ссылку магазина
            title=title, 
            place=place, 
            time_string=time_string,
            full_description=full_description, 
            price_min=price_min, 
            price_max=price_max, 
            tickets_available=tickets_available
        )
        logger.info(f"✅ [Kvitki] Сырые данные собраны: {title}")
        return asdict(event)

    except Exception as e:
        logger.error(f"❌ [Kvitki] Ошибка при сборе данных для {event_url}: {e}")
        return asdict(EventData(link=event_url, title=f"Ошибка обработки", status="error"))
    finally:
        if page:
            await page.close()


async def parse_site(config: Dict) -> List[Dict]:
    """Основная функция-парсер для сайта Kvitki.by с использованием Playwright."""
    base_url = config.get('url')
    logging.info(f"\n[INFO] Запуск Playwright-парсера для: '{config.get('site_name')}'")
    
    pages_to_parse_limit = config.get('pages_to_parse_limit', float('inf'))
    max_events_limit = config.get('max_events_to_process_limit', float('inf'))
    
    event_links = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page_for_lists = await browser.new_page()

        page_num = 1
        while page_num <= pages_to_parse_limit:
            url = f"{base_url}page:{page_num}/"
            logger.info(f"📄 Сканирую страницу: {url}")
            try:
                await page_for_lists.goto(url, timeout=30000)
                await page_for_lists.wait_for_selector('a.event_short', timeout=10000, state='attached')
                locators = page_for_lists.locator('a.event_short')
                
                new_links_on_page = 0
                for i in range(await locators.count()):
                    if len(event_links) >= max_events_limit: break
                    link = await locators.nth(i).get_attribute('href')
                    if link and link not in event_links:
                        event_links.add(link)
                        new_links_on_page += 1

                if new_links_on_page == 0:
                    logger.info(f"   - Новые события на странице {page_num} не найдены. Завершаю сбор.")
                    break
                
                logger.info(f"   - Найдено {new_links_on_page} новых ссылок. Всего собрано: {len(event_links)}")
                if len(event_links) >= max_events_limit:
                    logger.info("   - Достигнут лимит событий. Завершаю сбор.")
                    break
                page_num += 1
            except PlaywrightTimeoutError:
                logger.info(f"   - Карточки событий на странице {page_num} не найдены. Завершаю сбор.")
                break
            except Exception as e:
                logger.error(f"   - Произошла непредвиденная ошибка при сборе ссылок: {e}")
                break
        
        await page_for_lists.close()
        
        event_links_list = list(event_links)
        logger.info(f"\n🔗 Всего собрано {len(event_links_list)} уникальных ссылок для детальной обработки.")
        
        if not event_links_list:
            await browser.close()
            return []

        semaphore = asyncio.Semaphore(config.get('concurrent_events', CONCURRENT_EVENTS))
        tasks = []
        async def run_with_semaphore(link):
            async with semaphore:
                return await parse_single_event(browser, link)

        for link in event_links_list:
            tasks.append(asyncio.create_task(run_with_semaphore(link)))
        
        results = await asyncio.gather(*tasks)
        await browser.close()

    final_results = [res for res in results if res.get('status') == 'ok']
    logger.info(f"🎉 Сбор сырых данных для '{config.get('site_name')}' завершен. Собрано: {len(final_results)} событий.")
    return final_results

# --- Блок для автономного тестирования файла (без изменений) ---
if __name__ == '__main__':
    print("--- ЗАПУСК АВТОНОМНОГО ТЕСТА ПАРСЕРА ---")
    print("Парсер будет работать в режиме с ограничениями.")

    test_config = {
        'category_name': 'Музыка (Тест)',
        'url': 'https://www.kvitki.by/rus/bileti/muzyka/',
        'event_type': 'Концерт',
        'parsing_method': 'playwright_kvitki',
        'pages_to_parse_limit': 1,
        'max_events_to_process_limit': 3,
    }

    async def run_test():
        results = await parse_site(test_config)
        print("\n--- ИТОГОВЫЙ РЕЗУЛЬТАТ ТЕСТА (СЫРЫЕ ДАННЫЕ) ---")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\nВсего получено: {len(results)} событий.")

    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
        print("\n\nПроцесс парсинга остановлен пользователем.")
    except Exception as e:
        print(f"\nВо время тестового запуска произошла ошибка: {e}")