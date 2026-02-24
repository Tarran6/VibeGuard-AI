#!/usr/bin/env python3
"""
Тестирование on-chain логирования для контракта VibeGuard
"""
import asyncio
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

async def test_contract():
    """Проверка контракта и отправка тестовой транзакции"""
    
    print("🔍 Проверка контракта VibeGuard...")
    print(f"Адрес контракта: {CONTRACT_ADDRESS}")
    print(f"RPC URL: {RPC_URL}")
    
    # Подключение к блокчейну
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not w3.is_connected():
        print("❌ Не удалось подключиться к RPC")
        return
    
    print(f"✅ Подключены к блокчейну. Chain ID: {w3.eth.chain_id}")
    
    # Проверка адреса контракта
    if not w3.is_address(CONTRACT_ADDRESS):
        print("❌ Невалидный адрес контракта")
        return
    
    # Получение кода контракта
    try:
        code = w3.eth.get_code(CONTRACT_ADDRESS)
        if code == b'':
            print("❌ По адресу нет контракта (EOA)")
            return
        print(f"✅ Контракт найден. Размер кода: {len(code)} байт")
    except Exception as e:
        print(f"❌ Ошибка получения кода контракта: {e}")
        return
    
    # Проверка приватного ключа
    if not PRIVATE_KEY:
        print("❌ WEB3_PRIVATE_KEY не найден в .env")
        return
    
    try:
        account = w3.eth.account.from_key(PRIVATE_KEY)
        print(f"✅ Аккаунт: {account.address}")
        
        # Проверка баланса
        balance = w3.eth.get_balance(account.address)
        print(f"💰 Баланс: {w3.from_wei(balance, 'ether')} BNB")
        
        if balance < w3.to_wei(0.01, 'ether'):
            print("⚠️  Маленький баланс для газа")
        
    except Exception as e:
        print(f"❌ Ошибка приватного ключа: {e}")
        return
    
    # Создание экземпляра контракта
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=SCAN_ABI,
        )
        print("✅ Экземпляр контракта создан")
    except Exception as e:
        print(f"❌ Ошибка создания контракта: {e}")
        return
    
    # Оценка газа для функции logScan
    try:
        test_target = "0x742d35Cc6634C0532925a3b8D4E7E0E0e9e0dF5D"  # Тестовый адрес
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
        print(f"💸 Стоимость газа: {w3.from_wei(gas_cost, 'ether')} BNB")
        
    except Exception as e:
        print(f"❌ Ошибка оценки газа: {e}")
        return
    
    # Отправка тестовой транзакции (если баланс достаточный)
    try:
        print("\n🚀 Отправка тестовой транзакции...")
        
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        
        tx = contract.functions.logScan(
            Web3.to_checksum_address(test_target),
            85,  # score
            True,  # isSafe
            account.address,
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": gas_estimate + 10000,  # + запас
            "gasPrice": gas_price,
        })
        
        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        
        print(f"✅ Транзакция отправлена: {tx_hash.hex()}")
        
        # Ожидание подтверждения
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print(f"✅ Транзакция подтверждена в блоке {receipt.blockNumber}")
            print(f"🔗 Explorer: https://opbnbscan.com/tx/{tx_hash.hex()}")
        else:
            print("❌ Транзакция не удалась")
            
    except Exception as e:
        print(f"❌ Ошибка отправки транзакции: {e}")

if __name__ == "__main__":
    asyncio.run(test_contract())
