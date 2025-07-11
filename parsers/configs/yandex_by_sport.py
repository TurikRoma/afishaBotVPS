# --- START OF FILE parsers/configs/yandex_by_sport.py ---
CONFIG = {
    'site_name': 'Yandex.Afisha (Спорт)',
    'url': 'https://afisha.yandex.ru/moscow/sport',
    'RUCAPTCHA_API_KEY': '863b567c1d376398b65ad121498f89a1',
    'event_type': 'Спорт',
    'period': 365,
    'parser_key': 'yandex_afisha', # <--- ДОБАВИТЬ ЭТО
    'max_pages': 30,
} 

TARGETS = [
    {
        'url_slug': 'moscow',
        'city_name': 'Москва',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'saint-petersburg',
        'city_name': 'Санкт-Петербург',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'kazan',
        'city_name': 'Казань',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'yekaterinburg',
        'city_name': 'Екатерингбург',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'nizhny-novgorod',
        'city_name': 'Нижний Новгород',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'krasnodar',
        'city_name': 'Краснодар',
        'country_name': 'Россия'
    },
    {
        'url_slug': 'chelyabinsk',
        'city_name': 'Челябинск',
        'country_name': 'Россия'
    },
    
    
]

# Общий шаблон конфига
BASE_CONFIG = {
    'parser_key': 'yandex_afisha',
    'event_type': 'Спорт',
    'period': 365,
    'max_pages': 50,
    'RUCAPTCHA_API_KEY': '863b567c1d376398b65ad121498f89a1',
}   