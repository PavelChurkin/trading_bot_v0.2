"""MOEX scanner + dashboard (trading_bot_v0.2).

Один asyncio-процесс: планировщик запросов к ISS MOEX, сканер всех акций,
веб-дашборд (FastAPI) с авторизацией по ключам, web-push (VAPID) и PWA.
"""

__version__ = "0.2.0"
