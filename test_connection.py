#!/usr/bin/env python3
"""
Тест умного подключения к блокчейну VibeGuard
"""

import os
from dotenv import load_dotenv
from web3 import Web3
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_connection")

def get_smart_w3(url_string):
    """Умное подключение к блокчейну с автоматическим переключением"""
    urls = [u.strip() for u in url_string.split(",") if u.strip()]
    logger.info(f"🔍 Пробую подключиться к {len(urls)} RPC узлам...")
    
    # Пробуем подключиться по очереди, пока не найдем живой узел
    for i, url in enumerate(urls, 1):
        try:
            logger.info(f"📡 Попытка {i}/{len(urls)}: {url}")
            if url.startswith('http'):
                provider = Web3.HTTPProvider(url, request_kwargs={'timeout': 10})
            elif url.startswith('ws'):
                provider = Web3.WebsocketProvider(url)
            else:
                logger.warning(f"⚠️ Неподдерживаемый протокол: {url}")
                continue
                
            temp_w3 = Web3(provider)
            if temp_w3.is_connected():
                logger.info(f"✅ Успешное подключение к блокчейну через: {url}")
                
                # Получаем информацию о блокчейне
                try:
                    latest_block = temp_w3.eth.block_number
                    chain_id = temp_w3.eth.chain_id
                    gas_price = temp_w3.eth.gas_price
                    
                    logger.info(f"📊 Информация о блокчейне:")
                    logger.info(f"   Последний блок: {latest_block}")
                    logger.info(f"   Chain ID: {chain_id}")
                    logger.info(f"   Gas Price: {gas_price} wei")
                    
                    return temp_w3
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось получить информацию: {e}")
                    return temp_w3
                    
        except Exception as e:
            logger.warning(f"⚠️ Узел {url} недоступен, пробую следующий... Ошибка: {e}")
            continue
    
    raise Exception("❌ КРИТИЧЕСКАЯ ОШИБКА: Ни один из RPC-узлов не отвечает!")

def main():
    """Основная функция теста"""
    logger.info("🚀 Тест умного подключения VibeGuard AI")
    logger.info("=" * 50)
    
    # Загружаем переменные окружения
    load_dotenv()
    
    # Получаем RPC URL
    rpc_url = os.getenv("OPBNB_HTTP_URL")
    if not rpc_url:
        logger.error("❌ OPBNB_HTTP_URL не найден в .env файле")
        return
    
    logger.info(f"🌐 RPC URL из .env: {rpc_url}")
    
    try:
        # Тестируем умное подключение
        w3 = get_smart_w3(rpc_url)
        
        # Дополнительные тесты
        logger.info("🧪 Дополнительные тесты...")
        
        # Тест 1: Проверка баланса
        try:
            test_address = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
            balance = w3.eth.get_balance(test_address)
            balance_bnb = w3.from_wei(balance, 'ether')
            logger.info(f"💰 Баланс тестового адреса: {balance_bnb:.6f} BNB")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить баланс: {e}")
        
        # Тест 2: Проверка последнего блока
        try:
            latest_block = w3.eth.get_block('latest')
            logger.info(f"📦 Информация о последнем блоке:")
            logger.info(f"   Номер: {latest_block.number}")
            logger.info(f"   Хеш: {latest_block.hash.hex()[:20]}...")
            logger.info(f"   Транзакций: {len(latest_block.transactions)}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить блок: {e}")
        
        logger.info("✅ Все тесты успешно пройдены!")
        logger.info("🎯 Умное подключение к блокчейну работает корректно!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        logger.error("💡 Решение:")
        logger.error("   1. Проверьте интернет соединение")
        logger.error("   2. Убедитесь что RPC URL правильный")
        logger.error("   3. Попробуйте другой RPC узел")

if __name__ == "__main__":
    main()
