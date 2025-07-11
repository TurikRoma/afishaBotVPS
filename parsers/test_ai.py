import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

API_KEY = os.getenv('MY_SECRET_KEY')

async def getArtist(title: str) -> list: # Добавил type hint для наглядности
    
    artists = []
    try:
        genai.configure(api_key=API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash') # Используем flash, он быстрее и дешевле для таких задач
        
        prompt = f""""Твоя задача — извлечь из текста ключевые имена собственные (названия команд, имена участников, названия мероприятий).

Правила:
1.  Если в тексте есть явное противостояние (например, "Команда А - Команда Б"), верни только имена участников этого противостояния.
2.  Если противостояния нет, верни полное название мероприятия без мусорны слов и цифер если это непосредственно не относится к мероприятию или виду мероприятия.
3.  Твой ответ стоит из перечисления ключевых имён собственных, через запятую.

Так же если это большое описание ивента, найти имена артистов, которые ВЫСТУПАЮТ на этом мероприятии, так же там могу быть названия мероприятия.Если ничего не найдёшь верни НИЧЕГО.
Теперь обработай следующий текст.



Вход: {title} """
        response = await model.generate_content_async(prompt)

        # Обрабатываем ответ
        if response.text:
            for artist in response.text.split(','):
                clean_artist = artist.strip().lower()
                if clean_artist: # Добавляем, только если строка не пустая после очистки
                    artists.append(clean_artist)
        
        return artists # <--- ИЗМЕНЕНИЕ 1: Возвращаем готовый список

    except Exception as e:
        print(f"Произошла ошибка: {e}")
        return [] # В случае ошибки возвращаем пустой список


async def getArtistkvitki(description: str) -> list: # Добавил type hint для наглядности
    
    artists = []
    try:
        genai.configure(api_key=API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash') # Используем flash, он быстрее и дешевле для таких задач
        
        prompt = f"Из этого текста вычлени только имена и фамилии людей, которые являются артистами, композиторами, дирижерами или постановщиками. Ответь списком через запятую. Если никого нет, оставь ответ пустым. Текст: {description}"
        response = await model.generate_content_async(prompt)

        # Обрабатываем ответ
        if response.text:
            for artist in response.text.split(','):
                clean_artist = artist.strip().lower()
                if clean_artist: # Добавляем, только если строка не пустая после очистки
                    artists.append(clean_artist)
        
        return artists # <--- ИЗМЕНЕНИЕ 1: Возвращаем готовый список

    except Exception as e:
        print(f"Произошла ошибка: {e}")
        return [] # В случае ошибки возвращаем пустой список
