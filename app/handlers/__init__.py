# app/handlers/__init__.py

from aiogram import Router

# Импортируем роутеры из всех наших файлов-модулей
from .common import router as common_router
from .onboarding import router as onboarding_router
from .subscriptions import router as subscriptions_router
from .afisha import router as afisha_router
from .profile import router as profile_router
from .favorities import router as favorites_router

# Импортируем вспомогательные функции, чтобы они были доступны из пакета
# Например, для run_notifier.py


# Создаем главный роутер, который будет включать в себя все остальные
main_router = Router()

main_router.include_router(common_router)
main_router.include_router(onboarding_router)
main_router.include_router(profile_router)
# favorites_router должен идти ПЕРЕД afisha_router
main_router.include_router(favorites_router) 
main_router.include_router(subscriptions_router)
# afisha_router теперь идет позже
main_router.include_router(afisha_router)
