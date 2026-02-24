#!/usr/bin/env python3
"""
VibeGuard AI - упрощенная версия для быстрого запуска
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from telebot.async_telebot import AsyncTeleBot
from web3 import Web3

load_dotenv()

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PRIMARY_OWNER_ID = int(os.getenv("PRIMARY_OWNER_ID", "449160262"))
OPBNB_HTTP_URL = os.getenv("OPBNB_HTTP_URL", "https://opbnb-mainnet-rpc.bnbchain.org")

if not TELEGRAM_TOKEN:
    print("❌ TELEGRAM_TOKEN не найден!")
    exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vibeguard")

bot = AsyncTeleBot(TELEGRAM_TOKEN)

# База данных SQLite
DB_PATH = Path("vibeguard.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

@bot.message_handler(commands=["start"])
async def start(message):
    await bot.reply_to(
        message,
        "🛡️ VibeGuard AI активирован!\n\n"
        "Бот для мониторинга opBNB транзакций.\n"
        "Подключи кошелек через WebApp (в разработке)."
    )

@bot.message_handler(commands=["status"])
async def status(message):
    if message.from_user.id != PRIMARY_OWNER_ID:
        await bot.reply_to(message, "❌ Только для админа")
        return
    
    # Проверяем подключение к блокчейну
    try:
        w3 = Web3(Web3.HTTPProvider(OPBNB_HTTP_URL))
        if w3.is_connected():
            block = w3.eth.get_block('latest')
            await bot.reply_to(
                message,
                f"✅ Бот работает\n"
                f"📦 Блок: {block.number}\n"
                f"🌐 RPC: Подключен"
            )
        else:
            await bot.reply_to(message, "❌ RPC не отвечает")
    except Exception as e:
        await bot.reply_to(message, f"❌ Ошибка: {str(e)[:100]}")

async def main():
    print("🚀 Запуск VibeGuard AI (упрощенная версия)...")
    init_db()
    print("✅ База данных инициализирована")
    
    # Проверяем подключение к блокчейну
    try:
        w3 = Web3(Web3.HTTPProvider(OPBNB_HTTP_URL))
        if w3.is_connected():
            print("✅ Подключено к opBNB")
        else:
            print("⚠️ Проблемы с подключением к opBNB")
    except Exception as e:
        print(f"❌ Ошибка RPC: {e}")
    
    print("🤖 Бот запущен...")
    await bot.polling()

if __name__ == "__main__":
    asyncio.run(main())
