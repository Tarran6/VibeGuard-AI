#!/usr/bin/env python3
"""
Быстрая проверка контракта VibeGuard
"""
import os
from dotenv import load_dotenv
from web3 import Web3

# Загрузка переменных
load_dotenv()

# Настройки
CONTRACT_ADDRESS = "0x6e5e4E9E9C4F5E498393c4b6216781a28e15902F"
PRIVATE_KEY = os.getenv("WEB3_PRIVATE_KEY")
RPC_URL = os.getenv("OPBNB_HTTP_URL", "https://opbnb-mainnet.nodereal.io/v1/409025609faa9f0b509ef6dbeffe2837")

# ABI контракта
SCAN_ABI = [{
    "inputs": [
        {"name": "_contract", "type": "address"},
        {"name": "_score",    "type": "uint256"},
        {"name": "_isSafe",   "type": "bool"},
        {"name": "_user",      "type": "address"},
    ],
    "name": "logScan",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]

def check_contract():
    """Проверка контракта без отправки транзакций"""
    
    print("🔍 Проверка контракта VibeGuard...")
    print(f"Адрес контракта: {CONTRACT_ADDRESS}")
    print(f"RPC URL: {RPC_URL}")
    
    # Подключение к блокчейну
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not w3.is_connected():
        print("❌ Не удалось подключиться к RPC")
        return False
    
    chain_id = w3.eth.chain_id
    print(f"✅ Подключены к блокчейну. Chain ID: {chain_id}")
    
    if chain_id != 204:
        print(f"⚠️  Ожидается opBNB (204), получено {chain_id}")
    
    # Проверка адреса контракта
    if not w3.is_address(CONTRACT_ADDRESS):
        print("❌ Невалидный адрес контракта")
        return False
    
    # Получение кода контракта
    try:
        code = w3.eth.get_code(CONTRACT_ADDRESS)
        if code == b'':
            print("❌ По адресу нет контракта (EOA)")
            return False
        print(f"✅ Контракт найден. Размер кода: {len(code)} байт")
    except Exception as e:
        print(f"❌ Ошибка получения кода контракта: {e}")
        return False
    
    # Проверка приватного ключа
    if not PRIVATE_KEY:
        print("❌ WEB3_PRIVATE_KEY не найден в .env")
        return False
    
    try:
        account = w3.eth.account.from_key(PRIVATE_KEY)
        print(f"✅ Аккаунт: {account.address}")
        
        # Проверка баланса
        balance = w3.eth.get_balance(account.address)
        balance_bnb = w3.from_wei(balance, 'ether')
        print(f"💰 Баланс: {balance_bnb} BNB")
        
        if balance < w3.to_wei(0.005, 'ether'):
            print("⚠️  Маленький баланс для газа (нужно ~0.005 BNB)")
        
    except Exception as e:
        print(f"❌ Ошибка приватного ключа: {e}")
        return False
    
    # Создание экземпляра контракта
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=SCAN_ABI,
        )
        print("✅ Экземпляр контракта создан")
    except Exception as e:
        print(f"❌ Ошибка создания контракта: {e}")
        return False
    
    # Оценка газа для функции logScan
    try:
        test_target = "0x742d35Cc6634C0532925a3b8D4E7E0E0e9e0dF5D"  # Тестовый адрес
        
        # Проверка функции
        gas_estimate = contract.functions.logScan(
            Web3.to_checksum_address(test_target),
            85,  # score
            True,  # isSafe
            account.address,
        ).estimate_gas({'from': account.address})
        
        print(f"⛽ Оценка газа для logScan: {gas_estimate:,}")
        
        # Получение цены газа
        gas_price = w3.eth.gas_price
        gas_cost = gas_estimate * gas_price
        gas_cost_bnb = w3.from_wei(gas_cost, 'ether')
        
        print(f"💸 Стоимость газа: {gas_cost_bnb} BNB")
        
        if balance_bnb < float(gas_cost_bnb) * 2:
            print("⚠️  Недостаточно BNB для газа")
        
        print("✅ Все проверки пройдены! Контракт готов к работе.")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка оценки газа: {e}")
        print("Возможные причины:")
        print("- Контракт не имеет функции logScan")
        print("- Неправильный ABI")
        print("- Контракт на другой сети")
        return False

if __name__ == "__main__":
    check_contract()
