"""
VibeGuard Agent Bot — опциональный модуль для AI-анализа алертов.
Использует Google Gemini. Для работы: pip install google-genai
"""
import logging
import os
import json
import aiohttp
from web3 import Web3
from datetime import datetime

# Network: opBNB Mainnet
# Verified Contract (current): 0x6e5e4E9E9C4F5E498393c4b6216781a28e15902F
CONTRACT_ADDRESS = "0x6e5e4E9E9C4F5E498393c4b6216781a28e15902F"
RPC_URL = "https://opbnb-mainnet-rpc.bnbchain.org"

logger = logging.getLogger("vibeguard.agent")


async def get_crypto_prices() -> dict:
    """Получает актуальные цены криптовалют с CoinGecko API"""
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                'ids': 'bitcoin,ethereum,binancecoin,tether,usd-coin',
                'vs_currencies': 'usd',
                'include_24hr_change': 'true'
            }
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
    except Exception as e:
        logger.warning(f"Failed to fetch prices: {e}")
    return {}


def format_transaction_details(tx_data: dict) -> str:
    """Форматирует детальную информацию о транзакции"""
    details = []
    
    if 'value' in tx_data and tx_data['value']:
        value_wei = int(tx_data['value'])
        value_bnb = value_wei / 10**18
        details.append(f"Сумма: {value_bnb:.6f} BNB")
    
    if 'gas' in tx_data and 'gasPrice' in tx_data:
        gas_limit = int(tx_data['gas'])
        gas_price = int(tx_data['gasPrice'])
        gas_cost_bnb = (gas_limit * gas_price) / 10**18
        details.append(f"Газ: {gas_cost_bnb:.6f} BNB")
    
    if 'to' in tx_data and tx_data['to']:
        to_addr = tx_data['to']
        details.append(f"Получатель: {to_addr[:10]}...{to_addr[-6:]}")
    
    return "\n".join(details)


def get_risk_description(risk: int, tx_type: str = "unknown") -> str:
    """Возвращает детальное описание уровня риска"""
    risk_descriptions = {
        5: "🚨 КРИТИЧЕСКИЙ УРОВЕНЬ - Мошенничество или фишинг с высокой вероятностью. Требуется немедленная блокировка!",
        4: "⚠️ ВЫСОКИЙ РИСК - Подозрительная активность, возможная попытка взлома. Рекомендуется повышенная осторожность.",
        3: "⚡ СРЕДНИЙ РИСК - Необычная транзакция, требует дополнительной проверки.",
        2: "🔍 НИЗКИЙ РИСК - Стандартная операция с минимальными рисками.",
        1: "✅ МИНИМАЛЬНЫЙ РИСК - Обычная транзакция, угрозы не обнаружены."
    }
    
    base_desc = risk_descriptions.get(risk, "🔍 Неизвестный уровень риска")
    
    # Добавляем специфичные описания для разных типов транзакций
    type_specific = {
        "approval": f"{base_desc} Подтверждение доступа к токенам может быть опасным если получатель неизвестен.",
        "transfer": f"{base_desc} Перевод средств на новый адрес требует проверки.",
        "contract_interaction": f"{base_desc} Взаимодействие с контрактом может содержать скрытые риски.",
        "swap": f"{base_desc} Обмен токенов через децентрализованный протокол.",
        "unknown": f"{base_desc} Тип транзакции требует дополнительного анализа."
    }
    
    return type_specific.get(tx_type, base_desc)


async def analyze_event_ai(status: str, risk: int, tx_data: dict = None, user_address: str = None) -> str:
    """Генерирует детальный отчёт по алерту через Gemini с ценами и анализом транзакции"""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return "AI analysis skipped (GEMINI_API_KEY not set)."
    
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai не установлен — pip install google-genai")
        return "AI analysis failed (google-genai not installed)."
    
    try:
        # Получаем актуальные цены
        prices = await get_crypto_prices()
        price_info = ""
        if prices:
            btc_price = prices.get('bitcoin', {}).get('usd', 0)
            eth_price = prices.get('ethereum', {}).get('usd', 0)
            bnb_price = prices.get('binancecoin', {}).get('usd', 0)
            
            price_info = f"""
📊 Текущие цены:
• BTC: ${btc_price:,.2f}
• ETH: ${eth_price:,.2f}  
• BNB: ${bnb_price:,.2f}
"""
        
        # Анализируем детали транзакции
        tx_details = ""
        tx_type = "unknown"
        if tx_data:
            tx_details = format_transaction_details(tx_data)
            # Определяем тип транзакции
            if 'method' in tx_data:
                method = tx_data['method'].lower()
                if 'approve' in method:
                    tx_type = "approval"
                elif 'transfer' in method:
                    tx_type = "transfer"
                elif 'swap' in method:
                    tx_type = "swap"
                else:
                    tx_type = "contract_interaction"
        
        # Получаем описание риска
        risk_desc = get_risk_description(risk, tx_type)
        
        # Формируем контекст для AI
        context = f"""
VibeGuard Security Analysis Report
📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🎯 Оповещение: {status}
⚡ Уровень риска: {risk}/5
{risk_desc}

{price_info}

🔍 Детали транзакции:
{tx_details if tx_details else "Базовая информация о транзакции недоступна"}

👤 Адрес пользователя: {user_address[:10]}...{user_address[-6:] if user_address else "Неизвестно"}
📍 Контракт: {CONTRACT_ADDRESS[:10]}...{CONTRACT_ADDRESS[-6:]}

Проанализируй эту ситуацию и предоставь:
1. Конкретную оценку угрозы безопасности
2. Рекомендации по действиям пользователя
3. Возможные сценарии развития событий
4. Советы по защите активов

Ответ должен быть на русском языке, профессиональным и содержать конкретные детали.
"""
        
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=context
        )
        
        ai_response = response.text or "AI analysis failed (empty response)."
        
        # Добавляем структурированный заголовок
        formatted_response = f"""
🛡️ VIBEGUARD AI АНАЛИЗ
{'='*40}
{ai_response}

⚡ Сгенерировано: {datetime.now().strftime('%H:%M:%S')}
🔗 Сеть: opBNB Mainnet
"""
        
        return formatted_response
        
    except Exception as e:
        logger.warning("Agent AI error: %s", e)
        return f"❌ Ошибка AI анализа: {str(e)}"


if __name__ == "__main__":
    print(f"VibeGuard Agent Bot monitoring contract: {CONTRACT_ADDRESS}")
