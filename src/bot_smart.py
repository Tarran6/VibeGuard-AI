# =============================================================================
#  VibeGuard Sentinel — src/bot_smart.py (v24.4 Smart Connection)
#  Умное подключение к блокчейну с автоматическим переключением
# =============================================================================

import asyncio
import html
import json
import logging
import os
import random
import secrets
import signal
import time
from asyncio import Lock, Queue, Semaphore
from typing import Optional

import aiohttp
import asyncpg
from dotenv import load_dotenv
from eth_account.messages import encode_defunct
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from web3 import Web3

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vibeguard")


def _require(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v:
        raise EnvironmentError(f"Переменная окружения не задана: {key}")
    return v


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# Обязательные
TELEGRAM_TOKEN   = _require("TELEGRAM_TOKEN")
OPBNB_HTTP_URL = _require("OPBNB_HTTP_URL")
DATABASE_URL      = _require("DATABASE_URL")
PRIMARY_OWNER_ID = int(_require("PRIMARY_OWNER_ID"))

# Парсинг пула RPC ссылок с резервными узлами
_RAW_HTTP_URL = _require("OPBNB_HTTP_URL")
HTTP_URLS = [u.strip() for u in _RAW_HTTP_URL.split(",") if u.strip()]
if not HTTP_URLS:
    raise EnvironmentError("OPBNB_HTTP_URL пуст или содержит невалидные данные")

# Резервные RPC (public nodes)
FALLBACK_RPCS = [
    "https://opbnb-mainnet-rpc.bnbchain.org",
    "https://opbnb-mainnet.nodereal.io/v1/your-key",  # нужно заменить на реальный ключ
]

# Объединяем основные и резервные RPC
ALL_RPC_URLS = HTTP_URLS + FALLBACK_RPCS


# ---------------------------------------------------------------------------
# УМНОЕ ПОДКЛЮЧЕНИЕ К БЛОКЧЕЙНУ
# ---------------------------------------------------------------------------

def get_smart_w3(url_string):
    """Умное подключение к блокчейну с автоматическим переключением"""
    urls = [u.strip() for u in url_string.split(",") if u.strip()]
    # Пробуем подключиться по очереди, пока не найдем живой узел
    for url in urls:
        try:
            if url.startswith('http'):
                provider = Web3.HTTPProvider(url, request_kwargs={'timeout': 10})
            elif url.startswith('ws'):
                provider = Web3.WebsocketProvider(url)
            else:
                continue
                
            temp_w3 = Web3(provider)
            if temp_w3.is_connected():
                logger.info(f"✅ Успешное подключение к блокчейну через: {url}")
                return temp_w3
        except Exception as e:
            logger.warning(f"⚠️ Узел {url} недоступен, пробую следующий... Ошибка: {e}")
            continue
    raise Exception("❌ КРИТИЧЕСКАЯ ОШИБКА: Ни один из RPC-узлов не отвечает!")


# Опциональные
GEMINI_KEYS = [k for k in _optional("GEMINI_API_KEY").split(",") if k.strip()]
GROQ_KEYS   = [k for k in _optional("GROQ_API_KEY").split(",") if k.strip()]
XAI_KEYS    = [k for k in _optional("XAI_API_KEY").split(",")    if k.strip()]

# AI модели (можно переопределить через env)
XAI_MODEL = _optional("XAI_MODEL", "grok-2-latest")
GROQ_MODEL = _optional("GROQ_MODEL", "llama-3.1-70b-versatile")
GEMINI_MODEL = _optional("GEMINI_MODEL", "gemini-2.0-flash-exp")

# GoPlus API
GOPLUS_APP_KEY    = _optional("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _optional("GOPLUS_APP_SECRET")

# On-chain логирование
ENABLE_ONCHAIN    = _optional("ENABLE_ONCHAIN_LOG") == "true"
ONCHAIN_PRIVKEY   = _optional("WEB3_PRIVATE_KEY")
ONCHAIN_CONTRACT  = _optional("VIBEGUARD_CONTRACT")

# URL веб-приложения (Telegram WebApp для Connect Wallet)
WEBAPP_URL = _optional("WEBAPP_URL", "")
REOWN_PROJECT_ID = _optional("REOWN_PROJECT_ID", "")
BOT_PUBLIC_URL = _optional("BOT_PUBLIC_URL", "")

LOGO_URL = _optional(
    "LOGO_URL",
    "https://raw.githubusercontent.com/Tarran6/VibeGuard-AI/main/assets/logo.png"
)

# Multi-sig владельцы (можно расширить через .env)
_ADDITIONAL_OWNERS = [int(uid) for uid in _optional("ADDITIONAL_OWNERS", "").split(",") if uid.strip().isdigit()]
OWNERS: set[int] = {PRIMARY_OWNER_ID} | set(_ADDITIONAL_OWNERS)

# Требуемые подтверждения для критических действий
MULTISIG_THRESHOLD = max(1, int(_optional("MULTISIG_THRESHOLD", "1")))

ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

bot = AsyncTeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

if not any([XAI_KEYS, GROQ_KEYS, GEMINI_KEYS]):
    logger.warning("⚠️  Ни один AI-ключ не задан — AI-функции отключены")

if not WEBAPP_URL:
    logger.warning("⚠️  WEBAPP_URL не задан — кнопка Connect Wallet будет недоступна")

# ---------------------------------------------------------------------------
# СТРУКТУРА БД И ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ---------------------------------------------------------------------------

_DB_DEFAULT: dict = {
    "stats": {"blocks": 0, "whales": 0, "threats": 0},
    "cfg":   {"limit_usd": 10_000.0, "watch": [], "ignore": []},
    "last_block": 0,
    "connected_wallets": {},
    "pending_verifications": {},
}

db: dict = {}

pool: Optional[asyncpg.Pool] = None
http_session: Optional[aiohttp.ClientSession] = None
start_time = time.time()

rpc_sem  = Semaphore(10)
ai_sem   = Semaphore(3)
tg_sem   = Semaphore(20)
db_lock  = Lock()
price_lock = Lock()

tx_queue:  Queue = Queue(maxsize=8_000)
log_queue: Queue = Queue(maxsize=8_000)

_shutdown    = False
_main_tasks: list[asyncio.Task] = []

_price_cache: dict[str, float] = {}
_price_cache_ts: float = 0.0
PRICE_TTL = 120

_token_price_cache: dict[str, tuple[float, float]] = {}

LIMIT_MIN_USD = 100.0

_decimals_cache: dict[str, int] = {}

_user_states: dict[int, dict] = {}
STATE_TTL = 600

# ---------------------------------------------------------------------------
# УТИЛИТЫ
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    return html.escape(str(text))

def get_state(uid: int) -> Optional[str]:
    e = _user_states.get(uid)
    if not e:
        return None
    if time.time() - e["ts"] > STATE_TTL:
        _user_states.pop(uid, None)
        return None
    return e["state"]

def set_state(uid: int, state: str) -> None:
    _user_states[uid] = {"state": state, "ts": time.time()}

def clear_state(uid: int) -> None:
    _user_states.pop(uid, None)

# ---------------------------------------------------------------------------
# МУЛЬТИПОДПИСИ И GOVERNANCE
# ---------------------------------------------------------------------------

_pending_actions: dict[str, dict] = {}

def create_action_id(action_type: str, target: str) -> str:
    return f"{action_type}:{target}:{int(time.time())}"

async def require_multisig(action_type: str, target: str, initiator: int) -> tuple[bool, str]:
    if len(OWNERS) == 1:
        return True, "Одиночный владелец - действие выполнено"
    
    action_id = create_action_id(action_type, target)
    
    async with db_lock:
        _pending_actions[action_id] = {
            "type": action_type,
            "target": target,
            "initiator": initiator,
            "confirmations": {initiator},
            "required": MULTISIG_THRESHOLD,
            "ts": time.time()
        }
    
    await save_db()
    return False, f"Требуется {MULTISIG_THRESHOLD} подтверждений. Получено: 1/{MULTISIG_THRESHOLD}"

async def confirm_action(action_id: str, user_id: int) -> tuple[bool, str]:
    async with db_lock:
        action = _pending_actions.get(action_id)
        if not action:
            return False, "Действие не найдено"
        
        if user_id in action["confirmations"]:
            return False, "Вы уже подтвердили это действие"
        
        action["confirmations"].add(user_id)
        
        if len(action["confirmations"]) >= action["required"]:
            # Достаточно подтверждений - выполняем действие
            del _pending_actions[action_id]
            await save_db()
            return True, f"Действие подтверждено ({len(action['confirmations'])}/{action['required']})"
        
        await save_db()
        return False, f"Подтверждено: {len(action['confirmations'])}/{action['required']}"

def is_owner(uid: int) -> bool:
    return uid in OWNERS

# ---------------------------------------------------------------------------
# POSTGRESQL
# ---------------------------------------------------------------------------

async def init_db() -> None:
    global pool, db
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_data "
            "(id INTEGER PRIMARY KEY, data JSONB NOT NULL)"
        )
        row = await conn.fetchrow("SELECT data FROM bot_data WHERE id = 1")
        if row:
            raw_data = row["data"]
            loaded = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            db = {**_DB_DEFAULT, **loaded}
            db["stats"] = {**_DB_DEFAULT["stats"], **loaded.get("stats", {})}
            db["cfg"]   = {**_DB_DEFAULT["cfg"],   **loaded.get("cfg",   {})}
            if db["cfg"]["limit_usd"] < LIMIT_MIN_USD:
                db["cfg"]["limit_usd"] = LIMIT_MIN_USD
            db.setdefault("connected_wallets", {})
            db.setdefault("pending_verifications", {})
            logger.info("✅ БД загружена")
        else:
            import copy
            db = copy.deepcopy(_DB_DEFAULT)
            await conn.execute(
                "INSERT INTO bot_data (id, data) VALUES (1, $1)",
                json.dumps(db),
            )
            logger.info("🆕 Создана новая БД")

async def save_db() -> None:
    if not pool:
        return
    for attempt in range(3):
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_data (id, data) VALUES (1, $1) "
                    "ON CONFLICT (id) DO UPDATE SET data = $1",
                    json.dumps(db),
                )
            return
        except Exception as e:
            logger.warning(f"save_db попытка {attempt+1}/3: {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    logger.error("❌ save_db: все 3 попытки провалились")

# ---------------------------------------------------------------------------
# RPC с умным подключением
# ---------------------------------------------------------------------------

async def rpc(payload: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=12)
    async with rpc_sem:
        last_error = None
        # Пробуем все RPC включая резервные
        for url in ALL_RPC_URLS:
            try:
                async with http_session.post(url, json=payload, timeout=timeout) as r:
                    if r.status == 429:
                        last_error = "RPC 429"
                        continue
                    r.raise_for_status()
                    result = await r.json()
                    # Логируем успешный RPC для мониторинга
                    if url in FALLBACK_RPCS:
                        logger.info(f"✅ Fallback RPC работает: {url}")
                    return result
            except Exception as e:
                last_error = str(e)
                # Если основной RPC не работает, пробуем резервные
                if url in HTTP_URLS and HTTP_URLS.index(url) == 0:
                    logger.warning(f"🔴 Основной RPC недоступен, переключаемся на резервные")
                continue
        
        if last_error == "RPC 429":
            raise RuntimeError("RPC 429 - все узлы перегружены")
        raise RuntimeError(f"Все RPC узлы недоступны. Последняя ошибка: {last_error}")

# ---------------------------------------------------------------------------
# ВЕРИФИКАЦИЯ КОШЕЛЬКА с умным подключением
# ---------------------------------------------------------------------------

async def verify_wallet(user_id: int, address: str, signature: str) -> tuple[bool, str]:
    uid_str = str(user_id)

    # Валидация входных данных
    if not isinstance(user_id, int) or user_id <= 0:
        return False, "Невалидный ID пользователя"
    
    if not isinstance(address, str) or len(address) != 42 or not address.startswith('0x'):
        return False, "Невалидный формат адреса кошелька"
    
    if not isinstance(signature, str) or len(signature) < 130:
        return False, "Невалидный формат подписи"

    if not Web3.is_address(address):
        return False, "Невалидный адрес кошелька"

    async with db_lock:
        pending = db["pending_verifications"].get(uid_str)

    if not pending:
        return False, "Сессия верификации не найдена. Нажми Connect Wallet заново."

    if time.time() - pending["ts"] > STATE_TTL:
        async with db_lock:
            db["pending_verifications"].pop(uid_str, None)
        return False, "Сессия истекла. Нажми Connect Wallet заново."

    nonce   = pending["nonce"]
    message = f"VibeGuard verification: {nonce}"

    try:
        # ИСПОЛЬЗУЕМ УМНОЕ ПОДКЛЮЧЕНИЕ!
        w3_local = get_smart_w3(_RAW_HTTP_URL)
        msg_defunct = encode_defunct(text=message)
        recovered   = w3_local.eth.account.recover_message(
            msg_defunct, signature=signature
        )
    except Exception as e:
        return False, f"Невалидная подпись: {str(e)[:80]}"

    if recovered.lower() != address.lower():
        return False, (
            f"Подпись не совпадает с адресом.\n"
            f"Ожидался: {address[:8]}...\n"
            f"Подпись от: {recovered[:8]}..."
        )

    addr_lower = address.lower()
    async with db_lock:
        wallets  = db["connected_wallets"].setdefault(uid_str, [])
        existing = [w["address"].lower() for w in wallets]

        if addr_lower in existing:
            return False, "Этот кошелёк уже подключён"

        if len(wallets) >= 5:
            return False, "Максимум 5 кошельков на аккаунт"

        label = f"Wallet {len(wallets) + 1}"
        wallets.append({"address": addr_lower, "label": label})
        db["pending_verifications"].pop(uid_str, None)

    await save_db()
    return True, f"✅ Кошелёк подключён: {addr_lower[:8]}...{addr_lower[-6:]}"

# ---------------------------------------------------------------------------
# ДЕМО: Тест умного подключения
# ---------------------------------------------------------------------------

async def test_smart_connection():
    """Тест умного подключения к блокчейну"""
    try:
        logger.info("🧪 Тест умного подключения к блокчейну...")
        w3 = get_smart_w3(_RAW_HTTP_URL)
        
        # Проверяем подключение
        if w3.is_connected():
            latest_block = w3.eth.block_number
            logger.info(f"✅ Подключение успешно! Последний блок: {latest_block}")
            return True
        else:
            logger.error("❌ Не удалось подключиться")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        return False

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main():
    """Основная функция с демонстрацией умного подключения"""
    logger.info("🚀 VibeGuard Sentinel запускается...")
    
    # Инициализация сессии
    http_session = aiohttp.ClientSession()
    
    # Тест умного подключения
    await test_smart_connection()
    
    logger.info("✅ VibeGuard Sentinel готов к работе!")

if __name__ == "__main__":
    asyncio.run(main())
