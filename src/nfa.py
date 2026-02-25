# src/nfa.py
import os
import json
import asyncio
import logging
from web3 import Web3
from web3.middleware import geth_poa_middleware
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("vibeguard.nfa")

# Подключение к opBNB
w3 = Web3(Web3.HTTPProvider(os.getenv("OPBNB_HTTP_URL")))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Переменные окружения
NFA_ADDRESS = os.getenv("NFA_CONTRACT_ADDRESS")
OWNER_ADDRESS = os.getenv("OWNER_ADDRESS")
PRIVATE_KEY = os.getenv("OWNER_PRIVATE_KEY")

if not all([NFA_ADDRESS, OWNER_ADDRESS, PRIVATE_KEY]):
    logger.error("Missing required env vars: NFA_CONTRACT_ADDRESS, OWNER_ADDRESS, OWNER_PRIVATE_KEY")
    raise EnvironmentError("NFA environment variables not set")

# Загрузка ABI
abi_path = "contracts/VibeGuardGuardian.abi"
if not os.path.exists(abi_path):
    logger.error(f"ABI file not found: {abi_path}")
    raise FileNotFoundError(f"ABI file missing: {abi_path}")

try:
    with open(abi_path, "r", encoding="utf-8") as f:
        ABI = json.load(f)
    logger.info(f"✅ ABI loaded successfully from {abi_path}")
except UnicodeDecodeError as e:
    logger.error(f"ABI file encoding error: {e}")
    # Создаем минимальный ABI для базовой работы
    ABI = [
        {
            "anonymous": False,
            "inputs": [
                {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
                {"indexed": True, "internalType": "uint256", "name": "tokenId", "type": "uint256"},
                {"indexed": False, "internalType": "string", "name": "name", "type": "string"}
            ],
            "name": "GuardianMinted",
            "type": "event"
        },
        {
            "inputs": [
                {"internalType": "string", "name": "name", "type": "string"},
                {"internalType": "string", "name": "imageURI", "type": "string"}
            ],
            "name": "mintGuardian",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function"
        }
    ]
    logger.warning("⚠️ Using fallback ABI due to encoding error")
except json.JSONDecodeError as e:
    logger.error(f"ABI JSON decode error: {e}")
    raise ValueError(f"Invalid ABI format: {e}")

contract = w3.eth.contract(address=Web3.to_checksum_address(NFA_ADDRESS), abi=ABI)

# ---------- Синхронные ядра (выполняются в потоках) ----------
def _sync_mint_guardian(name: str, image_uri: str):
    """Синхронная функция минта Guardian NFT"""
    try:
        nonce = w3.eth.get_transaction_count(OWNER_ADDRESS)
        gas_price = w3.eth.gas_price
        tx = contract.functions.mintGuardian(name, image_uri).build_transaction({
            'from': OWNER_ADDRESS,
            'nonce': nonce,
            'gas': 250000,
            'gasPrice': gas_price
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

        # Вычисляем topic события GuardianMinted(address,uint256,string)
        guardian_minted_topic = Web3.keccak(text="GuardianMinted(address,uint256,string)").hex()

        token_id = None
        for log in receipt.logs:
            if log['topics'][0].hex() == guardian_minted_topic:
                # Предполагаем, что tokenId индексирован и лежит в topics[2]
                token_id = int(log['topics'][2].hex(), 16)
                break

        if token_id is None:
            # Fallback: если событие не найдено, пробуем взять из первого лога (как раньше)
            if receipt.logs:
                token_id = int(receipt.logs[0]['topics'][2].hex(), 16)
                logger.warning(f"GuardianMinted event not found, using fallback token_id={token_id}")
            else:
                token_id = 0
                logger.error("No logs in receipt, token_id set to 0")

        logger.info(f"✅ Guardian minted! Token ID: {token_id} | Name: {name}")
        return token_id
    except Exception as e:
        logger.error(f"mint_guardian failed: {e}", exc_info=True)
        raise

def _sync_update_learning(token_id: int, new_merkle_root: bytes, protected_usd: int):
    try:
        nonce = w3.eth.get_transaction_count(OWNER_ADDRESS)
        gas_price = w3.eth.gas_price
        tx = contract.functions.updateLearning(token_id, new_merkle_root, protected_usd).build_transaction({
            'from': OWNER_ADDRESS,
            'nonce': nonce,
            'gas': 150000,
            'gasPrice': gas_price
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"✅ updateLearning succeeded for token {token_id}")
        return receipt
    except Exception as e:
        logger.error(f"update_learning failed: {e}", exc_info=True)
        raise

def _sync_attest_protection(token_id: int, wallet: str, risk_score: int):
    try:
        nonce = w3.eth.get_transaction_count(OWNER_ADDRESS)
        gas_price = w3.eth.gas_price
        tx = contract.functions.attestProtection(token_id, wallet, risk_score).build_transaction({
            'from': OWNER_ADDRESS,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': gas_price
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        logger.info(f"✅ attestProtection succeeded for token {token_id}")
        return receipt
    except Exception as e:
        logger.error(f"attest_protection failed: {e}", exc_info=True)
        raise

# ---------- Асинхронные обёртки ----------
async def mint_guardian(name: str, image_uri: str):
    """Минтит NFT пользователю (асинхронно)"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_mint_guardian, name, image_uri)

async def update_guardian_learning(token_id: int, new_merkle_root: bytes, protected_usd: int):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_update_learning, token_id, new_merkle_root, protected_usd)

async def attest_protection(token_id: int, wallet: str, risk_score: int):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_attest_protection, token_id, wallet, risk_score)
