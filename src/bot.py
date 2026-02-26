# =============================================================================
#  VibeGuard Sentinel — src/bot.py (v24.4 Fixed)
#  Исправления:
#    • Объединен обработчик web_app_data для исключения конфликтов.
#    • Добавлена корректная обработка nonce для верификации подписи.
#    • Добавлено принудительное сохранение базы данных после привязки.
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

# NFA импорт (относительный, так как bot.py в папке src)
from nfa import mint_guardian, update_guardian_learning, attest_protection, contract

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
# Временно для теста - ЗАМЕНИТЬ НА РЕАЛЬНЫЙ ТОКЕН!
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz") 
if TELEGRAM_TOKEN == "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz":
    print("❌ Вставьте реальный TELEGRAM_TOKEN в .env файл!")
    print("📍 Формат: 1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789")
    exit(1)
DATABASE_URL      = _optional("DATABASE_URL", "sqlite:///vibeguard.db")
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
                provider = Web3.HTTPProvider(url, request_kwargs={'timeout': 3})
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
DEEPSEEK_KEYS = [k for k in _optional("DEEPSEEK_API_KEY").split(",") if k.strip()]

# AI модели
XAI_MODEL = _optional("XAI_MODEL", "grok-2-latest")
GROQ_MODEL = _optional("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = _optional("GEMINI_MODEL", "gemini-2.0-flash")
DEEPSEEK_MODEL = _optional("DEEPSEEK_MODEL", "deepseek-chat")

GOPLUS_APP_KEY    = _optional("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _optional("GOPLUS_APP_SECRET")

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

OWNERS: set[int] = {PRIMARY_OWNER_ID}

ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

bot = AsyncTeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

if not any([XAI_KEYS, GROQ_KEYS, GEMINI_KEYS, DEEPSEEK_KEYS]):
    logger.warning("⚠️  Ни один AI-ключ не задан — AI-функции отключены")

if not WEBAPP_URL:
    logger.warning("⚠️  WEBAPP_URL не задан — кнопка Connect Wallet будет недоступна")

# ---------------------------------------------------------------------------
# СТРУКТУРА БД
# ---------------------------------------------------------------------------

_DB_DEFAULT: dict = {
    "stats": {"blocks": 0, "whales": 0, "threats": 0},
    "cfg":   {"limit_usd": 10_000.0, "watch": [], "ignore": []},
    "user_limits": {}, # <-- Добавили хранилище персональных лимитов
    "user_guardians": {},   # <-- добавить сюда
    "last_block": 0,
    "connected_wallets": {},
    "pending_verifications": {},
}

db: dict = {}

# ---------------------------------------------------------------------------
# ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ---------------------------------------------------------------------------

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


def score_emoji(score: int) -> str:
    if score >= 80:
        return "🟢"
    elif score >= 50:
        return "🟡"
    else:
        return "🔴"


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


def is_owner(uid: int) -> bool:
    return uid in OWNERS


# ---------------------------------------------------------------------------
# POSTGRESQL
# ---------------------------------------------------------------------------

async def init_db():
    global pool, db
    db_url = os.getenv("DATABASE_URL")
    
    # Railway иногда дает ссылки 'postgres://', а asyncpg любит 'postgresql://'
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    try:
        # Создаем пул соединений к твоему Postgres на Railway
        pool = await asyncpg.create_pool(db_url)
        
        async with pool.acquire() as conn:
            # Создаем таблицу, если её нет (используем тип JSONB для скорости)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_data (
                    id INTEGER PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)
            
            row = await conn.fetchrow("SELECT data FROM bot_data WHERE id = 1")
            if row:
                # Загружаем данные из Postgres
                loaded_data = json.loads(row['data'])
                db.update({**_DB_DEFAULT, **loaded_data})
                logger.info("✅ Статистика успешно загружена из PostgreSQL")
                logger.info(f"🔍 init_db: загруженный лимит из БД = {db['cfg']['limit_usd']}")
            else:
                # Если база пустая, создаем первую запись
                db.update(_DB_DEFAULT.copy())
                await conn.execute("INSERT INTO bot_data (id, data) VALUES (1, $1)", json.dumps(db))
                logger.info("🆕 Создана новая запись в PostgreSQL")
                logger.info(f"🔍 Лимит по умолчанию: {db['cfg']['limit_usd']}")
            
            # Убедимся что audit_cache существует
            if "audit_cache" not in db:
                db["audit_cache"] = {}
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Postgres: {e}")
        # Fallback на пустую базу в памяти, если Postgres лег
        db.update(_DB_DEFAULT.copy())

async def save_db():
    if not pool: 
        logger.warning("⚠️ save_db: pool отсутствует, сохранение пропущено")
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_data (id, data) VALUES (1, $1) "
                "ON CONFLICT (id) DO UPDATE SET data = $1",
                json.dumps(db)
            )
        logger.info("✅ БД сохранена")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сохранения в Postgres: {e}")


# ---------------------------------------------------------------------------
# ЦЕНЫ
# ---------------------------------------------------------------------------

async def _fetch_bnb_price() -> float:
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with http_session.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=binancecoin&vs_currencies=usd",
            timeout=timeout,
        ) as r:
            if r.status == 200:
                data = await r.json()
                return float(data["binancecoin"]["usd"])
    except Exception as e:
        logger.warning(f"BNB price fetch error: {e}")
    return 600.0  # fallback


async def fetch_source_code(contract_address: str) -> Optional[str]:
    """Выкачивает исходный код контракта через API BscScan/opBNBScan"""
    api_key = os.getenv("BSCSCAN_API_KEY")
    if not api_key:
        return None
    
    # URL для opBNB (или измени на bsc для основной сети)
    url = f"https://api-opbnb.bscscan.com/api?module=contract&action=getsourcecode&address={contract_address}&apikey={api_key}"
    
    try:
        async with http_session.get(url, timeout=10) as r:
            data = await r.json()
            if data['status'] == '1':
                # Извлекаем код (он может быть в разном формате, берем первый файл)
                source = data['result'][0].get('SourceCode', '')
                return source[:15000] # Ограничиваем длину, чтобы ИИ не подавился
            return None
    except Exception as e:
        logger.error(f"Ошибка выкачивания кода: {e}")
        return None

async def _fetch_token_price(token_addr: str) -> float:
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        url = (
            "https://api.coingecko.com/api/v3/simple/token_price/binance-smart-chain"
            f"?contract_addresses={token_addr}&vs_currencies=usd"
        )
        async with http_session.get(url, timeout=timeout) as r:
            if r.status == 200:
                data = await r.json()
                entry = data.get(token_addr.lower(), {})
                return float(entry.get("usd", 0.0))
    except Exception as e:
        logger.warning(f"Token price fetch error {token_addr[:10]}: {e}")
    return 0.0


async def refresh_bnb_price() -> None:
    global _price_cache_ts
    async with price_lock:
        if time.time() - _price_cache_ts < PRICE_TTL:
            return
        price = await _fetch_bnb_price()
        _price_cache["BNB"] = price
        _price_cache_ts = time.time()
        logger.info(f"💰 BNB = ${price:.2f}")


async def bnb_to_usd(bnb: float) -> float:
    await refresh_bnb_price()
    return bnb * _price_cache.get("BNB", 600.0)


async def token_to_usd(token_addr: str, raw: int, decimals: int) -> float:
    amount = raw / (10 ** decimals)
    now = time.time()
    cached = _token_price_cache.get(token_addr)
    if cached is None or (now - cached[1]) > PRICE_TTL:
        price = await _fetch_token_price(token_addr)
        _token_price_cache[token_addr] = (price, now)
        cached = (price, now)
    return amount * cached[0]


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

async def rpc(payload: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=12)
    async with rpc_sem:
        last_error = None
        for url in ALL_RPC_URLS: # <-- Используем все ссылки по очереди
            try:
                async with http_session.post(url, json=payload, timeout=timeout) as r:
                    if r.status == 429:
                        last_error = "RPC 429"
                        continue
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                last_error = str(e)
                continue
        
        if last_error == "RPC 429":
            raise RuntimeError("RPC 429 - все узлы перегружены")
        raise RuntimeError(f"Все RPC узлы недоступны. Ошибка: {last_error}")


async def get_block(number: int) -> Optional[dict]:
    try:
        data = await rpc({
            "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
            "params": [hex(number), True], "id": 1,
        })
        return data.get("result")
    except Exception as e:
        logger.warning(f"get_block {number}: {e}")
        return None


async def get_logs(from_bn: int, to_bn: int) -> list[dict]:
    try:
        data = await rpc({
            "jsonrpc": "2.0", "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(from_bn),
                "toBlock":   hex(to_bn),
                "topics":    [ERC20_TRANSFER_TOPIC],
            }],
            "id": 1,
        })
        return data.get("result") or []
    except Exception as e:
        logger.warning(f"get_logs {from_bn}-{to_bn}: {e}")
        return []


async def get_decimals(token_addr: str) -> int:
    if token_addr in _decimals_cache:
        return _decimals_cache[token_addr]
    try:
        data = await rpc({
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": token_addr, "data": "0x313ce567"}, "latest"],
            "id": 1,
        })
        result = data.get("result", "0x12")
        dec = int(result, 16) if result and result != "0x" else 18
    except Exception:
        dec = 18
    _decimals_cache[token_addr] = dec
    return dec


# ---------------------------------------------------------------------------
# ON-CHAIN ЛОГИРОВАНИЕ (только для китов)
# ---------------------------------------------------------------------------

_SCAN_ABI = [{
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

async def log_onchain(target: str, score: int, is_safe: bool) -> None:
    if not ENABLE_ONCHAIN or not ONCHAIN_PRIVKEY or not ONCHAIN_CONTRACT:
        return
    if not Web3.is_address(target) or not Web3.is_address(ONCHAIN_CONTRACT):
        return

    def _do_log():
        w3 = get_smart_w3(_RAW_HTTP_URL)
        acct = w3.eth.account.from_key(ONCHAIN_PRIVKEY)
        
        # Проверяем баланс
        balance = w3.eth.get_balance(acct.address)
        required = w3.eth.gas_price * 130_000
        if balance < required:
            logger.warning(f"Insufficient balance: {balance} wei, required: {required}")
            return None
        
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ONCHAIN_CONTRACT),
            abi=_SCAN_ABI,
        )
        nonce = w3.eth.get_transaction_count(acct.address, 'pending')
        tx = contract.functions.logScan(
            Web3.to_checksum_address(target),
            score, is_safe, acct.address,
        ).build_transaction({
            "from":     acct.address,
            "nonce":    nonce,
            "gas":      130_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, acct.key)
        
        # Пытаемся получить сырую транзакцию из разных атрибутов
        raw_tx = (
            getattr(signed, 'raw_transaction', None) or 
            getattr(signed, 'rawTransaction', None) or 
            getattr(signed, 'transaction', None)
        )
        if raw_tx is None:
            raise AttributeError("Cannot find raw transaction attribute in signed object")
        
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        return tx_hash.hex()

    try:
        loop = asyncio.get_running_loop()
        tx_hash = await loop.run_in_executor(None, _do_log)
        logger.info(f"On-chain log OK: {tx_hash[:20]}...")
    except Exception as e:
        logger.warning(f"On-chain log failed: {str(e)[:100]}")


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------

async def call_ai(prompt: str) -> str:
    configs = (
        # [("xai",    k) for k in XAI_KEYS]  +   # ← xAI отключён
        [("groq",   k) for k in GROQ_KEYS] +
        [("gemini", k) for k in GEMINI_KEYS] +
        [("deepseek", k) for k in DEEPSEEK_KEYS]
    )
    if not configs:
        return "AI-ключи не настроены."

    async with ai_sem:
        for provider, key in configs:
            logger.info(f"🤖 Пробуем AI провайдера: {provider}")
            try:
                result = await _ai_request(provider, key, prompt)
                if result:
                    logger.info(f"✅ AI [{provider}] успешно ответил")
                    return esc(result)
                else:
                    logger.warning(f"⚠️ AI [{provider}] вернул пустой ответ")
            except Exception as e:
                logger.warning(f"❌ AI [{provider}] ошибка: {e}")

    return "Все AI-провайдеры временно недоступны."


async def _ai_request(provider: str, key: str, prompt: str) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=20)

    if provider == "xai":
        url     = "https://api.x.ai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": XAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "groq":
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000
        }
    else:  # gemini
        url     = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={key}"
        )
        headers = {}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

    async with http_session.post(
        url, json=payload, headers=headers, timeout=timeout
    ) as r:
        if r.status == 429:
            raise RuntimeError("Rate limit 429")
        if r.status != 200:
            txt = await r.text()
            raise RuntimeError(f"HTTP {r.status}: {txt[:200]}")
        data = await r.json()

    if provider == "gemini":
        candidates = data.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            if parts and isinstance(parts[0], dict) and "text" in parts[0]:
                return parts[0]["text"]
        raise RuntimeError("Gemini: неверный формат ответа")
    return data.get("choices", [{}])[0].get("message", {}).get("content") or ""


# ---------------------------------------------------------------------------
# СКАМ-ПРОВЕРКА
# ---------------------------------------------------------------------------

async def check_scam(addr: str) -> list[str]:
    if not Web3.is_address(addr):
        return []
    url = (
        f"https://api.gopluslabs.io/api/v1/token_security/204"
        f"?contract_addresses={addr}"
    )
    if GOPLUS_APP_KEY:
        url += f"&app_key={GOPLUS_APP_KEY}&app_secret={GOPLUS_APP_SECRET}"
    try:
        async with http_session.get(
            url, timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            d = data.get("result", {}).get(addr.lower(), {})
            risks: list[str] = []
            if d.get("is_honeypot")          == "1": risks.append("🍯 HONEYPOT")
            if d.get("is_open_source")        == "0": risks.append("🔐 ЗАКРЫТЫЙ КОД")
            if d.get("is_proxy")              == "1": risks.append("👤 PROXY")
            if d.get("can_take_back_ownership") == "1": risks.append("👑 СМЕНА ВЛАДЕЛЬЦА")
            if d.get("hidden_owner")          == "1": risks.append("🕵️ СКРЫТЫЙ ВЛАДЕЛЕЦ")
            return risks
    except Exception as e:
        logger.warning(f"GoPlus error {addr[:10]}: {e}")
        return []


# ---------------------------------------------------------------------------
# TELEGRAM УТИЛИТЫ
# ---------------------------------------------------------------------------

async def safe_send(chat_id: int, text: str, **kwargs) -> None:
    # Пропускаем ботов
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type == 'private' and getattr(chat, 'is_bot', False):
            logger.debug(f"Skipping bot chat {chat_id}")
            return
    except Exception as e:
        logger.warning(f"Failed to get chat {chat_id}: {e}")
    
    async with tg_sem:
        try:
            await bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            logger.warning(f"safe_send → {chat_id}: {e}")


async def notify_owners(text: str) -> None:
    await asyncio.gather(
        *[safe_send(uid, text) for uid in OWNERS],
        return_exceptions=True,
    )


def _wallet_watchers(address: str) -> list[int]:
    addr = address.lower()
    result = []
    for uid_str, wallets in db.get("connected_wallets", {}).items():
        if any(w["address"].lower() == addr for w in wallets):
            result.append(int(uid_str))
    return result


def _is_connected_wallet(address: str) -> bool:
    addr = address.lower()
    for wallets in db.get("connected_wallets", {}).values():
        if any(w["address"].lower() == addr for w in wallets):
            return True
    return False


# ---------------------------------------------------------------------------
# SaaS ДВИЖОК РАССЫЛКИ
# ---------------------------------------------------------------------------

def get_whale_markup(token_addr: str = None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    btns = []
    if token_addr:
        # Кнопка "График" (DexScreener)
        btns.append(types.InlineKeyboardButton(
            "📊 График", 
            url=f"https://dexscreener.com/bsc/{token_addr}"
        ))
        # 🔥 Новая кнопка для ИИ-аудита
        btns.append(types.InlineKeyboardButton(
            "🧠 Deep Audit", 
            callback_data=f"ai_audit:{token_addr}"
        ))
    btns.append(types.InlineKeyboardButton(
        "⚙️ Мой лимит", 
        callback_data="menu_settings"
    ))
    markup.add(*btns)
    return markup

async def broadcast_whale(amount_usd: float, text: str, token_addr: str = None):
    markup = get_whale_markup(token_addr)
    # 1. Админы получают всё
    for admin_id in OWNERS:
        await safe_send(admin_id, text, reply_markup=markup)
        
    # 2. Юзеры получают только если сумма больше ИХ лимита
    async with db_lock:
        user_limits = db.get("user_limits", {})
        global_limit = db["cfg"]["limit_usd"]
        all_users = set(db.get("connected_wallets", {}).keys()) | set(user_limits.keys())

    for uid_str in all_users:
        uid = int(uid_str)
        if uid in OWNERS: continue
        
        limit = user_limits.get(uid_str, global_limit)
        if amount_usd >= limit:
            await safe_send(uid, text, reply_markup=markup)


# ---------------------------------------------------------------------------
# ОБРАБОТКА BNB-ТРАНЗАКЦИЙ
# ---------------------------------------------------------------------------

async def process_bnb_tx(tx: dict) -> None:
    try:
        val_bnb = int(tx.get("value", "0x0"), 16) / 10 ** 18
        if val_bnb == 0:
            return

        sender = (tx.get("from") or "").lower()
        target = (tx.get("to")   or "").lower()
        if not target:
            return

        async with db_lock:
            limit_usd = db["cfg"]["limit_usd"]
            ignore    = list(db["cfg"]["ignore"])
            watch     = list(db["cfg"]["watch"])

        if sender in ignore or target in ignore:
            return

        val_usd = await bnb_to_usd(val_bnb)

        watchers = _wallet_watchers(sender) + _wallet_watchers(target)
        if watchers:
            wallet_alert = (
                f"🔔 <b>Активность кошелька</b>\n\n"
                f"💸 <b>{val_bnb:.4f} BNB</b> (≈ ${val_usd:,.0f})\n"
                f"From: <code>{esc(sender[:8] + '...' + sender[-4:])}</code>\n"
                f"To:   <code>{esc(target[:8] + '...' + target[-4:])}</code>"
            )
            for uid in set(watchers):
                await safe_send(uid, wallet_alert)
            return

        if val_usd < limit_usd:
            return

        async with db_lock:
            db["stats"]["whales"] += 1

        whale_text = (
            f"🐳 <b>WHALE — BNB</b>\n"
            f"💰 <b>{val_bnb:.4f} BNB</b> (≈ ${val_usd:,.0f})\n"
            f"From: <code>{esc(sender[:8] + '...' + sender[-4:])}</code>\n"
            f"To:   <code>{esc(target[:8] + '...' + target[-4:])}</code>"
        )

        if sender in watch or target in watch:
            await notify_owners(f"🎯 <b>WATCHLIST HIT</b>\n\n{whale_text}")

        # СНАЧАЛА проверяем контракт на скам
        risks = await check_scam(target)
        score = 25 if risks else 85
        is_safe = not bool(risks)
        
        # ФОРМИРУЕМ УМНЫЙ ПРОМПТ ДЛЯ ИИ на основе данных блокчейна
        if risks:
            prompt = (
                f"🚨 ТРЕВОГА! КИТ ПЕРЕВЕЛ {val_bnb:.2f} BNB (${val_usd:,.0f}) НА ПОДОЗРИТЕЛЬНЫЙ КОНТРАКТ {target[:8]}...\n"
                f"Риски: {', '.join(risks)}.\n"
                f"Напиши жёсткое предупреждение для инвесторов (2 предложения), с эмодзи. Без паники, но чётко."
            )
        else:
            prompt = (
                f"🐋 КИТ ПЕРЕВЕЛ {val_bnb:.2f} BNB (${val_usd:,.0f})!\n"
                f"От {sender[:8]}... к {target[:8]}...\n"
                f"Контракт чист. Как думаешь, это арбитраж, покупка или просто перекладывание?\n"
                f"Ответь коротко и с огоньком (1-2 предложения), используй эмодзи. На русском."
            )

        # ТЕПЕРЬ зовем ИИ с готовым отчетом
        async with ai_sem:
            verdict = await call_ai(prompt)
        
        # Собираем красивый итоговый алерт
        full_report = (
            f"{whale_text}\n\n"
            f"🛡️ <b>VibeScore: {score}/100</b> {score_emoji(score)}\n"
            f"{'🚨 <b>КРИТИЧЕСКИЙ РИСК:</b> ' + ', '.join(risks) if risks else '✅ Базовые проверки пройдены'}\n\n"
            f"🧠 <b>Deep AI Audit:</b>\n{verdict}"
        )
        
        await broadcast_whale(val_usd, full_report)
        asyncio.create_task(log_onchain(target, score, is_safe))

    except Exception as e:
        logger.error(f"process_bnb_tx: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# ОБРАБОТКА ERC-20 TRANSFER ЛОГОВ
# ---------------------------------------------------------------------------

async def process_erc20_log(log: dict) -> None:
    try:
        topics = log.get("topics", [])
        if len(topics) < 3:
            return

        token_addr = log.get("address", "").lower()
        sender     = ("0x" + topics[1][-40:]).lower()
        receiver   = ("0x" + topics[2][-40:]).lower()
        raw_data   = log.get("data", "0x0")
        raw_amount = int(raw_data, 16) if raw_data and raw_data != "0x" else 0

        if raw_amount == 0:
            return

        async with db_lock:
            limit_usd = db["cfg"]["limit_usd"]
            ignore    = list(db["cfg"]["ignore"])
            watch     = list(db["cfg"]["watch"])

        if sender in ignore or receiver in ignore:
            return

        decimals = await get_decimals(token_addr)
        val_usd  = await token_to_usd(token_addr, raw_amount, decimals)
        amount   = raw_amount / (10 ** decimals)

        watchers = _wallet_watchers(sender) + _wallet_watchers(receiver)
        if watchers:
            wallet_alert = (
                f"🔔 <b>Активность кошелька (Token)</b>\n\n"
                f"💸 <b>{amount:,.2f} токенов</b> (≈ ${val_usd:,.0f})\n"
                f"Токен: <code>{esc(token_addr[:8] + '...' + token_addr[-4:])}</code>\n"
                f"From:  <code>{esc(sender[:8] + '...' + sender[-4:])}</code>\n"
                f"To:    <code>{esc(receiver[:8] + '...' + receiver[-4:])}</code>"
            )
            for uid in set(watchers):
                await safe_send(uid, wallet_alert)
            return

        if val_usd < limit_usd:
            return

        async with db_lock:
            db["stats"]["whales"] += 1

        whale_text = (
            f"🐋 <b>WHALE — TOKEN</b>\n"
            f"💰 <b>{amount:,.2f} токенов</b> (≈ ${val_usd:,.0f})\n"
            f"Токен: <code>{esc(token_addr[:8] + '...' + token_addr[-4:])}</code>\n"
            f"From:  <code>{esc(sender[:8] + '...' + sender[-4:])}</code>\n"
            f"To:    <code>{esc(receiver[:8] + '...' + receiver[-4:])}</code>"
        )

        if sender in watch or receiver in watch:
            await notify_owners(f"🎯 <b>WATCHLIST TOKEN</b>\n\n{whale_text}")

        # Проверяем токен на скам
        risks = await check_scam(token_addr)
        score = 25 if risks else 85
        is_safe = not bool(risks)
        
        # Умный промпт для токенов
        if risks:
            prompt = (
                f"🚨 КРИТИЧЕСКИЙ РИСК! КИТ ПЕРЕВЕЛ {amount:,.0f} токенов (${val_usd:,.0f}) КОНТРАКТА {token_addr[:8]}...\n"
                f"Угрозы: {', '.join(risks)}.\n"
                f"Напиши срочное предупреждение трейдерам на русском (2 предложения), с эмодзи. Чётко и жёстко."
            )
        else:
            prompt = (
                f"🐋 КИТ ДВИГАЕТ {amount:,.0f} токенов (${val_usd:,.0f})!\n"
                f"Контракт {token_addr[:8]}... чист.\n"
                f"Это OTC-сделка, перекладка или подготовка к пампингу? Ответь коротко, с эмодзи."
            )

        async with ai_sem:
            verdict = await call_ai(prompt)
        
        full_report = (
            f"{whale_text}\n\n"
            f"🛡️ <b>VibeScore: {score}/100</b> {score_emoji(score)}\n"
            f"{'🚨 <b>КРИТИЧЕСКИЙ РИСК:</b> ' + ', '.join(risks) if risks else '✅ Код токена чист'}\n\n"
            f"🧠 <b>Deep AI Audit:</b>\n{verdict}"
        )
        
        await broadcast_whale(val_usd, full_report, token_addr)
        asyncio.create_task(log_onchain(token_addr, score, is_safe))

    except Exception as e:
        logger.error(f"process_erc20_log: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# ВОРКЕРЫ
# ---------------------------------------------------------------------------

async def tx_worker(wid: int) -> None:
    logger.info(f"TX worker #{wid} started")
    while not _shutdown:
        try:
            item = await asyncio.wait_for(tx_queue.get(), timeout=1.0)
            await process_bnb_tx(item)
            tx_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"tx_worker#{wid}: {e}")


async def log_worker(wid: int) -> None:
    logger.info(f"Log worker #{wid} started")
    while not _shutdown:
        try:
            item = await asyncio.wait_for(log_queue.get(), timeout=1.0)
            await process_erc20_log(item)
            log_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"log_worker#{wid}: {e}")


# ---------------------------------------------------------------------------
# МОНИТОРИНГ БЛОКЧЕЙНА
# ---------------------------------------------------------------------------

BLOCK_BATCH   = 2
POLL_INTERVAL = 5.0
MAX_CATCHUP   = 50
SAVE_EVERY    = 20


async def monitor() -> None:
    logger.info("🔍 Мониторинг блокчейна запущен")
    save_counter = 0

    while not _shutdown:
        try:
            data    = await rpc({"jsonrpc": "2.0", "method": "eth_blockNumber", "id": 1})
            current = int(data.get("result", "0x0"), 16)

            async with db_lock:
                last = db.get("last_block", 0)

            if last == 0 or current - last > 1_000:
                last = current - 5
                async with db_lock:
                    db["last_block"] = last
                logger.info(f"🆕 Стартуем с блока {last}")

            if current <= last:
                await asyncio.sleep(POLL_INTERVAL + random.uniform(0, 1))
                continue

            to_proc  = min(current - last, MAX_CATCHUP)
            start_bn = last + 1
            end_bn   = last + to_proc

            for b_start in range(start_bn, end_bn + 1, BLOCK_BATCH):
                if _shutdown:
                    break
                b_end = min(b_start + BLOCK_BATCH - 1, end_bn)

                blocks, logs = await asyncio.gather(
                    asyncio.gather(
                        *[get_block(bn) for bn in range(b_start, b_end + 1)],
                        return_exceptions=True,
                    ),
                    get_logs(b_start, b_end),
                )

                for block in blocks:
                    if isinstance(block, Exception) or not block:
                        continue
                    for tx in block.get("transactions", []):
                        if tx_queue.full():
                            logger.warning("TX queue full — пропускаем")
                        else:
                            await tx_queue.put(tx)

                for log in logs:
                    if log_queue.full():
                        logger.warning("Log queue full — пропускаем")
                    else:
                        await log_queue.put(log)

            async with db_lock:
                db["stats"]["blocks"] += to_proc
                db["last_block"]       = end_bn

            save_counter += to_proc
            if save_counter >= SAVE_EVERY:
                await save_db()
                save_counter = 0

        except Exception as e:
            if "429" in str(e):
                logger.error("🔴 RPC 429 — пауза 60 сек")
                await asyncio.sleep(60)
            else:
                logger.error(f"monitor: {e}", exc_info=True)
                await asyncio.sleep(10)
            continue

        await asyncio.sleep(POLL_INTERVAL + random.uniform(0, 1))


# ---------------------------------------------------------------------------
# ВЕРИФИКАЦИЯ КОШЕЛЬКА
# ---------------------------------------------------------------------------

def get_cached_audit(addr: str) -> Optional[str]:
    """Возвращает закешированный результат аудита, если он не старше 1 часа."""
    cache = db.get("audit_cache", {})
    entry = cache.get(addr.lower())
    if entry:
        age = time.time() - entry["timestamp"]
        if age < 3600:  # 1 час
            return entry["result"]
    return None

async def verify_wallet(user_id: int, address: str, signature: str) -> tuple[bool, str]:
    uid_str = str(user_id)
    if not Web3.is_address(address):
        return False, "Невалидный адрес"

    async with db_lock:
        pending = db["pending_verifications"].get(uid_str)
        if not pending: return False, "Сессия не найдена"
        
        # Проверка подписи (оставляем твою рабочую логику)
        try:
            w3_l = get_smart_w3(_RAW_HTTP_URL)
            msg = encode_defunct(text=f"VibeGuard verification: {pending['nonce']}")
            recovered = w3_l.eth.account.recover_message(msg, signature=signature)
            if recovered.lower() != address.lower():
                return False, "Подпись не совпадает"
        except Exception as e:
            return False, f"Ошибка подписи: {e}"

# СТРОГО 1 КОШЕЛЕК: Перезаписываем список, старые удаляются
        db["connected_wallets"][uid_str] = [{"address": address.lower(), "label": "Main Wallet"}]
        db["pending_verifications"].pop(uid_str, None)

    await save_db()
    return True, "✅ Кошелёк успешно привязан"


async def mint_guardian_for_user(uid: int):
    """Фоновая задача для минта Guardian NFT пользователю (только если ещё нет)"""
    logger.info(f"🚀 mint_guardian_for_user: uid={uid}")
    
    # Проверяем, есть ли уже NFT у пользователя
    async with db_lock:
        existing_token = db.get("user_guardians", {}).get(str(uid))
    if existing_token:
        logger.info(f"ℹ️ У пользователя {uid} уже есть Guardian NFT (token_id={existing_token}), пропускаем минт")
        return

    try:
        token_id = await mint_guardian(
            name=f"Guardian_{uid}",
            image_uri="https://raw.githubusercontent.com/Tarran6/VibeGuard-AI/main/assets/logo.png"
        )
        await safe_send(
            uid,
            f"🛡️ <b>Вам выдан Guardian NFT!</b>\n"
            f"Token ID: <code>{token_id}</code>\n\n"
            f"Теперь ваш персональный Neural Guardian следит за безопасностью активов!"
        )
        async with db_lock:
            if "user_guardians" not in db:
                db["user_guardians"] = {}
            db["user_guardians"][str(uid)] = token_id
        await save_db()
        logger.info(f"🛡️ Guardian NFT заминчен: token_id={token_id} для user_id={uid}")
    except Exception as e:
        logger.error(f"❌ Ошибка минта Guardian для user_id={uid}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТЕКСТА
# ---------------------------------------------------------------------------

async def clean_and_send(chat_id: int, text: str, reply_markup=None, delete_previous: types.Message = None):
    """Удаляет предыдущее сообщение (если есть) и отправляет новое"""
    if delete_previous:
        try:
            await bot.delete_message(chat_id, delete_previous.message_id)
        except:
            pass
    await bot.send_message(chat_id, text, reply_markup=reply_markup)

async def get_status_text() -> str:
    uptime = time.time() - start_time
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    async with db_lock:
        s = db["stats"]
        limit_usd = db["cfg"]["limit_usd"]
        logger.info(f"🔍 get_status_text: загружен limit_usd={limit_usd}")
        last_b = db.get("last_block", 0)
        wc = len(db["cfg"]["watch"])
        ic = len(db["cfg"]["ignore"])
        total_w = sum(len(v) for v in db["connected_wallets"].values())
    bnb_price = _price_cache.get("BNB", 0.0)
    return (
        f"🛡️ <b>VibeGuard Sentinel v24.4</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"Блоков:          <b>{s['blocks']:,}</b>\n"
        f"Последний блок: <b>{last_b:,}</b>\n"
        f"Китов:           <b>{s['whales']}</b>\n"
        f"Угроз:           <b>{s['threats']}</b>\n\n"
        f"⚙️ <b>Конфиг:</b>\n"
        f"Лимит китов:    <b>${limit_usd:,.0f}</b>\n"
        f"BNB цена:        <b>${bnb_price:.2f}</b>\n"
        f"Watchlist:      <b>{wc}</b> адресов\n"
        f"Ignore:          <b>{ic}</b> адресов\n"
        f"Кошельков:      <b>{total_w}</b>\n\n"
        f"📬 TX queue:  <b>{tx_queue.qsize()}</b>\n"
        f"📬 Log queue: <b>{log_queue.qsize()}</b>\n\n"
        f"⏱️ Uptime: <code>{hours}ч {minutes}м</code>"
    )


async def get_limit_text() -> str:
    async with db_lock:
        cur = db["cfg"]["limit_usd"]
    return (
        f"⚙️ <b>Настройки лимита</b>\n\n"
        f"Текущий лимит китов: <b>${cur:,.0f}</b>\n"
        f"Алерты о подключённых кошельках — при любых суммах.\n\n"
        f"Изменить (только владелец): /limit 100 … /limit 1000000"
    )


# ---------------------------------------------------------------------------
# ИНЛАЙН-КЛАВИАТУРА ГЛАВНОГО МЕНЮ
# ---------------------------------------------------------------------------

def get_main_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton("👛 Мои кошельки", callback_data="menu_mywallets")
    btn2 = types.InlineKeyboardButton("🔗 Подключить кошелёк", callback_data="menu_connect")
    btn3 = types.InlineKeyboardButton("📊 Статистика", callback_data="menu_status")
    btn4 = types.InlineKeyboardButton("🧠 AI Ассистент", callback_data="menu_ai")
    btn5 = types.InlineKeyboardButton("🔍 Проверить контракт", callback_data="menu_check")
    btn6 = types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")
    btn7 = types.InlineKeyboardButton("🛡️ Поддержка", callback_data="menu_support")
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6, btn7)
    return markup


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
async def cmd_start(m: types.Message) -> None:
    logger.info(f"🔍 /start вызван от user_id={m.from_user.id}")
    clear_state(m.from_user.id)
    
    # Убираем reply-клавиатуру, если она была
    try:
        await bot.delete_message(m.chat.id, m.message_id)
    except:
        pass
    
    # Принудительно обновляем лимит из БД
    async with db_lock:
        current_limit = db["cfg"]["limit_usd"]
        logger.info(f"� Текущий лимит из БД: {current_limit}")
    
    text = await get_status_text()
    await bot.send_message(
        m.chat.id,
        text,
        reply_markup=get_main_menu_keyboard(),
    )


@bot.message_handler(commands=["connect"])
async def cmd_connect(m: types.Message) -> None:
    logger.info(f"🔗 /connect вызван user_id={m.from_user.id}")
    uid = m.from_user.id
    nonce = secrets.token_hex(16)

    async with db_lock:
        db["pending_verifications"][str(uid)] = {
            "nonce": nonce,
            "ts": time.time(),
        }
    await save_db()

    # Формируем URL с параметрами startapp и wc_project_id
    parts = [f"startapp={nonce}", f"wc_project_id={REOWN_PROJECT_ID}"]
    if BOT_PUBLIC_URL:
        parts.append(f"api={BOT_PUBLIC_URL}/webapp/connect")
    webapp_url = f"{WEBAPP_URL}?{'&'.join(parts)}"
    logger.info(f"🔗 WebApp URL: {webapp_url}")

    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL and REOWN_PROJECT_ID:
        kb.add(types.InlineKeyboardButton(
            "🔗 Connect Wallet",
            web_app=types.WebAppInfo(url=webapp_url),
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "⚠️ WebApp не настроен",
            callback_data="webapp_not_configured",
        ))

    await bot.reply_to(
        m,
        "👛 <b>Подключение кошелька</b>\n\n"
        "Нажми кнопку ниже и выбери любой кошелёк из списка.\n\n"
        "<i>Сессия действительна 10 минут.</i>",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ ИНЛАЙН-КНОПОК
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "webapp_not_configured")
async def cb_webapp_not_configured(c: types.CallbackQuery) -> None:
    await bot.answer_callback_query(
        c.id,
        "WEBAPP_URL или REOWN_PROJECT_ID не заданы в .env — см. README",
        show_alert=True,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("menu_"))
async def handle_menu_callback(c: types.CallbackQuery):
    action = c.data[5:]
    user_id = c.from_user.id
    message = c.message

    if action == "mywallets":
        await bot.answer_callback_query(c.id)
        # Удаляем сообщение с меню
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        # Создаем объект message для вызова cmd_mywallets
        class FakeMessage:
            def __init__(self, chat_id, from_user):
                self.chat = type('Chat', (), {'id': chat_id})()
                self.from_user = type('User', (), {'id': from_user})()
        
        fake_msg = FakeMessage(message.chat.id, user_id)
        await cmd_mywallets(fake_msg)
    elif action == "connect":
        # Генерируем nonce и редактируем текущее сообщение
        await bot.answer_callback_query(c.id)
        nonce = secrets.token_hex(16)
        async with db_lock:
            db["pending_verifications"][str(user_id)] = {
                "nonce": nonce,
                "ts": time.time(),
            }
        await save_db()
        parts = [f"startapp={nonce}", f"wc_project_id={REOWN_PROJECT_ID}"]
        if BOT_PUBLIC_URL:
            parts.append(f"api={BOT_PUBLIC_URL}/webapp/connect")
        webapp_url = f"{WEBAPP_URL}?{'&'.join(parts)}"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "🔗 Connect Wallet",
            web_app=types.WebAppInfo(url=webapp_url),
        ))
        await bot.edit_message_text(
            "👛 <b>Подключение кошелька</b>\n\nНажми кнопку ниже и выбери любой кошелёк из списка.\n\n<i>Сессия действительна 10 минут.</i>",
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=kb
        )
    elif action == "status":
        await bot.answer_callback_query(c.id)
        text = await get_status_text()
        await clean_and_send(message.chat.id, text, get_main_menu_keyboard(), delete_previous=message)
    elif action == "ai":
        await bot.answer_callback_query(c.id)
        set_state(user_id, "ask_ai")
        await bot.send_message(
            message.chat.id,
            "🤖 Задай любой вопрос о крипте или контрактах.\n/cancel — выйти.",
        )
    elif action == "check":
        await bot.answer_callback_query(c.id)
        set_state(user_id, "check_contract")
        await bot.send_message(message.chat.id, "Отправь адрес контракта для проверки:")
    elif action == "settings":
        await bot.answer_callback_query(c.id)
        async with db_lock:
            user_limit = db.get("user_limits", {}).get(str(user_id), db["cfg"]["limit_usd"])
        
        set_state(user_id, "wait_limit")
        text = (
            f"⚙️ <b>Настройки лимита</b>\n\n"
            f"Твой порог алертов: <b>${user_limit:,.0f}</b>\n\n"
            f"👇 <b>Напиши новую сумму числом</b> (мин. $3,000).\n"
            f"<i>Админам разрешено любое число.</i>"
        )
        await clean_and_send(message.chat.id, text, get_main_menu_keyboard(), delete_previous=message)
    elif action == "support":
        await bot.answer_callback_query(c.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Связаться с менеджером", url="https://t.me/tarran6"))
        await clean_and_send(message.chat.id, "🛡️ Нужна помощь? Напишите менеджеру:", kb, delete_previous=message)
    else:
        await bot.answer_callback_query(c.id, "Неизвестная команда")


@bot.callback_query_handler(func=lambda c: c.data.startswith("dc:"))
async def cb_disconnect(c: types.CallbackQuery) -> None:
    parts = c.data.split(":")
    if parts[1] == "cancel":
        await bot.answer_callback_query(c.id, "Отменено")
        await bot.edit_message_reply_markup(
            c.message.chat.id, c.message.message_id, reply_markup=None
        )
        return

    uid = parts[1]  # оставляем строкой
    idx = int(parts[2])

    if str(c.from_user.id) != uid:
        await bot.answer_callback_query(c.id, "⛔ Нет доступа", show_alert=True)
        return

    async with db_lock:
        wallets = db["connected_wallets"].get(str(c.from_user.id), [])
        if idx >= len(wallets):
            await bot.answer_callback_query(c.id, "Кошелёк не найден")
            return
        removed = wallets.pop(idx)
        if not wallets:
            del db["connected_wallets"][str(c.from_user.id)]

    await save_db()
    await bot.answer_callback_query(c.id, "✅ Кошелёк отключён")
    await bot.edit_message_text(
        f"✅ Кошелёк отключён:\n<code>{esc(removed['address'])}</code>",
        c.message.chat.id,
        c.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data == "connect_new")
async def cb_connect_new(c: types.CallbackQuery) -> None:
    await bot.answer_callback_query(c.id)
    await cmd_connect(c.message)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ai_audit:"))
async def cb_ai_audit_whale(c: types.CallbackQuery):
    addr = c.data.split(":", 1)[1]
    
    # Убираем "часики" на кнопке и даём обратную связь
    await bot.answer_callback_query(c.id, "🔍 Запускаю глубокий аудит кода...")
    
    # Вызываем общую функцию аудита
    # Используем chat_id из исходного сообщения и не привязываемся к reply_to_message_id,
    # чтобы результат пришёл новым сообщением (или можно ответить на то же сообщение)
    await perform_audit(addr, c.message.chat.id, c.message.message_id)


# ---------------------------------------------------------------------------
# ОБРАБОТКА ДАННЫХ FROM WEBAPP
# ---------------------------------------------------------------------------

@bot.message_handler(content_types=["web_app_data"])
async def handle_webapp_data(m: types.Message) -> None:
    """
    Telegram отправляет результат WebApp сюда.
    WebApp передаёт JSON: {"address": "0x...", "signature": "0x...", "nonce": "..."}
    """
    uid = m.from_user.id
    logger.info(f"� handle_webapp_data: uid из сообщения = {uid}")
    logger.info(f"� Получены данные WebApp от user_id={uid}")
    
    try:
        data = json.loads(m.web_app_data.data)
        address = data.get("address", "").strip()
        sig = data.get("signature", "").strip()
        nonce = data.get("nonce", "").strip()
        logger.info(f"� Данные: address={address[:8]}..., nonce={nonce[:8]}...")
    except Exception as e:
        logger.warning(f"webapp_data parse error uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка данных от WebApp. Попробуй ещё раз.")
        return

    if not address or not sig or not nonce:
        logger.warning(f"Неполные данные от {uid}")
        await safe_send(uid, "❌ Неполные данные от WebApp.")
        return

    # Вызываем verify_wallet
    logger.info(f"🔐 Вызываем verify_wallet для user_id={uid}")
    success, message = await verify_wallet(uid, address, sig)
    logger.info(f"✅ verify_wallet вернул: success={success}, message={message}")

    if success:
        logger.info(f"✅ Кошелёк успешно верифицирован для user_id={uid}")
        await safe_send(
            uid,
            f"✅ <b>Кошелёк подключён!</b>\n"
            f"<code>{esc(address.lower())}</code>\n\n"
            f"Теперь ты получаешь личные алерты о всех транзакциях этого адреса.",
        )
        
        # Начинаем минт
        logger.info(f"🔄 Начинаем минт Guardian для user_id={uid}")
        try:
            token_id = await mint_guardian(
                name=f"Guardian_{uid}",
                image_uri="https://raw.githubusercontent.com/Tarran6/VibeGuard-AI/main/assets/logo.png"
            )
            logger.info(f"✅ mint_guardian вернул token_id={token_id}")
            
            await safe_send(
                uid,
                f"🛡️ <b>Вам выдан Guardian NFT!</b>\n"
                f"Token ID: <code>{token_id}</code>\n\n"
                f"Теперь ваш персональный Neural Guardian следит за безопасностью активов!"
            )
            
            # Сохраняем token_id в БД
            async with db_lock:
                if "user_guardians" not in db:
                    db["user_guardians"] = {}
                db["user_guardians"][str(uid)] = token_id
                logger.info(f"💾 token_id={token_id} сохранён в БД для user_id={uid}")
            
            await save_db()
            logger.info(f"🎉 Guardian NFT успешно заминчен и сохранён для user_id={uid}")
        except Exception as e:
            logger.error(f"❌ Ошибка минта Guardian для user_id={uid}: {e}", exc_info=True)
            # Не прерываем основной поток, просто логируем
    else:
        logger.warning(f"❌ Ошибка верификации: {message}")
        await safe_send(uid, f"❌ {esc(message)}")


# ---------------------------------------------------------------------------
# КОМАНДЫ БЕЗ ИЗМЕНЕНИЙ
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["mywallets"])
async def cmd_mywallets(m: types.Message) -> None:
    uid = m.from_user.id
    async with db_lock:
        wallets = list(db["connected_wallets"].get(str(uid), []))

    if not wallets:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔗 Подключить кошелёк", callback_data="connect_new"))
        await bot.send_message(
            m.chat.id,
            "👛 У тебя нет подключённых кошельков.\n"
            "Нажми кнопку ниже чтобы подключить:",
            reply_markup=kb
        )
        return

    async with db_lock:
        limit = db["cfg"]["limit_usd"]

    lines = "\n".join(
        f"{i+1}. <b>{esc(w['label'])}</b>\n   <code>{esc(w['address'])}</code>"
        for i, w in enumerate(wallets)
    )

    kb = types.InlineKeyboardMarkup(row_width=2)
    for i, w in enumerate(wallets):
        short = f"{w['address'][:6]}...{w['address'][-4:]}"
        kb.add(types.InlineKeyboardButton(
            f"❌ {w['label']} ({short})",
            callback_data=f"dc:{str(uid)}:{i}",
        ))

    kb.add(types.InlineKeyboardButton("🔗 Добавить кошелёк", callback_data="connect_new"))

    await bot.send_message(
        m.chat.id,
        f"👛 <b>Твой подключённый кошелёк:</b>\n\n"
        f"{lines}\n\n"
        f"🔔 Алерты при любом движении.\n"
        f"🐳 Глобальный лимит китов: <b>${limit:,.0f}</b>",
        reply_markup=kb
    )


# =============================================================================
# КОМАНДА /myguardian — персональный Guardian NFT
# =============================================================================
@bot.message_handler(commands=["myguardian", "guardian"])
async def cmd_myguardian(m: types.Message) -> None:
    uid = m.from_user.id
    logger.info(f"🔍 /guardian вызвана с user_id={uid}")

    async with db_lock:
        token_id = db.get("user_guardians", {}).get(str(uid))
        if not token_id:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔗 Получить Guardian", callback_data="connect_new"))
            await bot.reply_to(
                m,
                "👛 У тебя пока нет Guardian NFT.\n\n"
                "Подключи кошелёк и получи своего персонального Neural Guardian!",
                reply_markup=kb
            )
            return

    # Читаем данные с контракта
    try:
        protected = contract.functions.protectedAmount(token_id).call()
        scans = contract.functions.scanCount(token_id).call()
    except Exception as e:
        logger.warning(f"Не удалось прочитать данные Guardian {token_id}: {e}")
        protected = 0
        scans = 0

    protected_usd = protected / 1_000_000   # 6 decimals для USD (можно сделать динамически)

    text = f"""
🛡️ <b>Твой Guardian NFT</b>

Token ID: <code>{token_id}</code>

💰 Защищено: <b>${protected_usd:,.2f}</b>
📊 Сканов сделано: <b>{scans:,}</b>

🔗 <a href="https://opbnbscan.com/token/{os.getenv('NFA_CONTRACT_ADDRESS')}?a={token_id}">Посмотреть на opbnbscan</a>
"""

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Обновить данные", callback_data="refresh_guardian"))

    await bot.reply_to(m, text, reply_markup=kb, disable_web_page_preview=True)


# Callback для кнопки "Обновить данные"
@bot.callback_query_handler(func=lambda c: c.data == "refresh_guardian")
async def cb_refresh_guardian(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db_lock:
        token_id = db.get("user_guardians", {}).get(str(uid))
    if not token_id:
        await bot.answer_callback_query(c.id, "❌ NFT не найден", show_alert=True)
        return
    try:
        protected = contract.functions.protectedAmount(token_id).call()
        scans = contract.functions.scanCount(token_id).call()
        protected_usd = protected / 1_000_000
        text = f"""
🛡️ <b>Твой Guardian NFT</b>

Token ID: <code>{token_id}</code>

💰 Защищено: <b>${protected_usd:,.2f}</b>
📊 Сканов сделано: <b>{scans:,}</b>

🔗 <a href="https://opbnbscan.com/token/{os.getenv('NFA_CONTRACT_ADDRESS')}?a={token_id}">Посмотреть на opbnbscan</a>
"""
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Обновить данные", callback_data="refresh_guardian"))
        try:
            await bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, disable_web_page_preview=True)
            await bot.answer_callback_query(c.id, "✅ Данные обновлены")
        except Exception as e:
            if "message is not modified" in str(e):
                await bot.answer_callback_query(c.id, "✅ Данные актуальны", show_alert=False)
            else:
                raise e
    except Exception as e:
        logger.error(f"refresh_guardian error: {e}")
        await bot.answer_callback_query(c.id, "❌ Ошибка обновления", show_alert=True)


@bot.message_handler(commands=["disconnect"])
async def cmd_disconnect(m: types.Message) -> None:
    uid = m.from_user.id
    async with db_lock:
        wallets = list(db["connected_wallets"].get(str(uid), []))

    if not wallets:
        await bot.reply_to(m, "У тебя нет подключённых кошельков.")
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, w in enumerate(wallets):
        short = f"{w['address'][:6]}...{w['address'][-4:]}"
        kb.add(types.InlineKeyboardButton(
            f"❌ {w['label']} ({short})",
            callback_data=f"dc:{uid}:{i}",
        ))
    kb.add(types.InlineKeyboardButton("Отмена", callback_data="dc:cancel"))
    await bot.reply_to(m, "Выбери кошелёк для отключения:", reply_markup=kb)


@bot.message_handler(commands=["stats"])
async def cmd_stats(m: types.Message):
    async with db_lock:
        whales = db["stats"]["whales"]
        blocks = db["stats"]["blocks"]
        threats = db["stats"]["threats"]
        limit = db["cfg"]["limit_usd"]
    
    text = (
        f"📊 <b>VibeGuard Stats</b>\n\n"
        f"🐳 Китов обнаружено: <b>{whales}</b>\n"
        f"🛡️ Угроз выявлено: <b>{threats}</b>\n"
        f"📦 Блоков обработано: <b>{blocks:,}</b>\n"
        f"⚙️ Текущий лимит: <b>${limit}</b>\n"
        f"🧠 AI: Groq / DeepSeek\n"
        f"🔗 Сеть: opBNB"
    )
    await bot.reply_to(m, text)


@bot.message_handler(commands=["check"])
async def cmd_check(m: types.Message) -> None:
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Пример: /check 0xКОНТРАКТ")
        return
    addr = args[1].strip()
    if not Web3.is_address(addr):
        await bot.reply_to(m, "❌ Невалидный адрес.")
        return

    wait = await bot.reply_to(m, "🔍 Проверяю контракт...")
    risks = await check_scam(addr)

    score = 25 if risks else 85
    is_safe = not bool(risks)

    if risks:
        icon, status = "🚨", f"Риски: {', '.join(risks)}"
        prompt = (
            f"Объясни на русском языке риски {risks} "
            f"для контракта {addr}. Кратко, без HTML."
        )
    else:
        icon, status = "✅", "Явных угроз не обнаружено"
        prompt = (
            f"Кратко на русском: что известно о контракте {addr} на opBNB? "
            f"Без HTML-тегов."
        )

    async with ai_sem:
        verdict = await call_ai(prompt)

    result_text = (
        f"{icon} <b>Проверка контракта</b>\n"
        f"<code>{esc(addr)}</code>\n\n"
        f"🛡️ <b>VibeScore: {score}/100</b> ({'Безопасно' if is_safe else 'Риск'})\n"
        f"<b>Статус:</b> {esc(status)}\n\n"
        f"🧠 <b>AI:</b> {verdict}"
    )
    try:
        await bot.edit_message_text(result_text, m.chat.id, wait.message_id)
    except Exception:
        await safe_send(m.chat.id, result_text)


async def perform_audit(addr: str, chat_id: int, reply_to_message_id: int = None):
    """
    Универсальная функция аудита.
    addr – адрес контракта,
    chat_id – куда отправлять результат,
    reply_to_message_id – если нужно ответить на конкретное сообщение.
    """
    # 0. Проверка кеша
    cached = get_cached_audit(addr)
    if cached:
        report = (
            f"🔍 <b>Результат ИИ-Аудита (из кеша)</b>\n"
            f"<code>{esc(addr)}</code>\n\n"
            f"{cached}"
        )
        await bot.send_message(
            chat_id, 
            report,
            reply_to_message_id=reply_to_message_id
        )
        return
    
    # Отправляем начальное сообщение
    status_msg = await bot.send_message(
        chat_id,
        "🕵️‍♂️ <b>Шаг 1/2:</b> Получаю исходный код контракта...",
        reply_to_message_id=reply_to_message_id
    )
    
    # 1. Получаем код
    code = await fetch_source_code(addr)
    if not code:
        await bot.edit_message_text(
            "❌ Код не верифицирован или контракт не найден.",
            chat_id,
            status_msg.message_id
        )
        return
    
    # Обновляем статус
    await bot.edit_message_text(
        "🕵️‍♂️ <b>Шаг 2/2:</b> Анализирую код с помощью ИИ...",
        chat_id,
        status_msg.message_id
    )
    
    # 2. Формируем промпт
    prompt = f"""
    Ты - эксперт по безопасности Solidity. Проанализируй этот код контракта на наличие бэкдоров:
    {code[:15000]}  # ограничение длины
    
    Найди: 
    1. Функции Mint (печать новых токенов).
    2. Функции Pause (остановка торгов).
    3. Скрытую смену владельца.
    4. Логику Honeypot.
    
    Ответь кратко на русском:
    - Вердикт (Безопасно/Опасно/Внимание).
    - Список критических уязвимостей (если есть).
    - Можно ли это покупать?
    """
    
    # 3. Зовём AI
    async with ai_sem:
        verdict = await call_ai(prompt)
    
    # 4. Сохраняем в кеш
    async with db_lock:
        db.setdefault("audit_cache", {})[addr.lower()] = {
            "result": verdict,
            "timestamp": time.time()
        }
    await save_db()
    
    # 5. Финальный отчёт
    report = (
        f"🔍 <b>Результат ИИ-Аудита</b>\n"
        f"<code>{esc(addr)}</code>\n\n"
        f"{verdict}"
    )
    await bot.edit_message_text(report, chat_id, status_msg.message_id)


@bot.message_handler(commands=["audit"])
async def cmd_audit(m: types.Message):
    args = m.text.split()
    if len(args) < 2:
        return await bot.reply_to(m, "Пример: `/audit 0x...`")
    
    addr = args[1].strip()
    await perform_audit(addr, m.chat.id, m.message_id)


@bot.message_handler(commands=["status", "stats"])
async def cmd_status(m: types.Message) -> None:
    text = await get_status_text()
    await bot.reply_to(m, text)


@bot.message_handler(commands=["limit"])
async def cmd_limit(m: types.Message) -> None:
    if m.text is None:
        text = await get_limit_text()
        await bot.reply_to(m, text)
        return

    args = m.text.split()
    if len(args) > 1:
        if not is_owner(m.from_user.id):
            await bot.reply_to(m, "⛔ Только для владельца бота.")
            return
        try:
            v = float(args[1])
            if v < LIMIT_MIN_USD:
                await bot.reply_to(
                    m,
                    f"❌ Минимальный лимит: <b>${LIMIT_MIN_USD:,.0f}</b>. "
                    f"Пример: /limit 100",
                )
                return
            async with db_lock:
                db["cfg"]["limit_usd"] = v
                logger.info(f"🔍 /limit: внутри db_lock значение установлено = {db['cfg']['limit_usd']}")
            await save_db()
            logger.info(f"🔍 /limit: после save_db, значение в db = {db['cfg']['limit_usd']}")
            await bot.reply_to(m, f"✅ Лимит китов изменён: <b>${v:,.0f}</b>")
        except ValueError:
            await bot.reply_to(m, f"❌ Укажите число от {LIMIT_MIN_USD:.0f}. Пример: /limit 100")
    else:
        text = await get_limit_text()
        await bot.reply_to(m, text)


@bot.message_handler(commands=["debug_limit"])
async def cmd_debug_limit(m: types.Message):
    """Показывает текущее значение лимита в памяти и в БД"""
    async with db_lock:
        mem_limit = db["cfg"]["limit_usd"]
    # Читаем напрямую из PostgreSQL
    db_limit = None
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM bot_data WHERE id = 1")
                if row:
                    data = json.loads(row['data'])
                    db_limit = data.get("cfg", {}).get("limit_usd")
        except Exception as e:
            db_limit = f"Ошибка: {e}"
    else:
        db_limit = "pool не инициализирован"
    
    await bot.reply_to(
        m,
        f"🧠 Лимит в памяти: <b>{mem_limit}</b>\n"
        f"💾 Лимит в PostgreSQL: <b>{db_limit}</b>"
    )


@bot.message_handler(commands=["set_limit_test"])
async def cmd_set_limit_test(m: types.Message):
    """Тестовая установка лимита (только для владельца)"""
    if not is_owner(m.from_user.id):
        return
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Использование: /set_limit_test 5000")
        return
    try:
        new_limit = float(args[1])
        async with db_lock:
            old = db["cfg"]["limit_usd"]
            db["cfg"]["limit_usd"] = new_limit
            logger.info(f"🧪 Тестовый лимит в памяти изменён с {old} на {new_limit}")
        await save_db()
        await bot.reply_to(m, f"✅ Лимит в памяти установлен: {new_limit}, БД сохранена")
    except Exception as e:
        await bot.reply_to(m, f"Ошибка: {e}")


@bot.message_handler(commands=["watch"])
async def cmd_watch(m: types.Message) -> None:
    if not is_owner(m.from_user.id): return
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Пример: /watch 0xADDRESS"); return
    addr = args[1].lower()
    if not Web3.is_address(addr):
        await bot.reply_to(m, "❌ Невалидный адрес"); return
    async with db_lock:
        if addr not in db["cfg"]["watch"]:
            db["cfg"]["watch"].append(addr)
    await save_db()
    await bot.reply_to(m, f"✅ Watchlist:\n<code>{esc(addr)}</code>")


@bot.message_handler(commands=["unwatch"])
async def cmd_unwatch(m: types.Message) -> None:
    if not is_owner(m.from_user.id): return
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Пример: /unwatch 0xADDRESS"); return
    addr = args[1].lower()
    async with db_lock:
        found = addr in db["cfg"]["watch"]
        if found: db["cfg"]["watch"].remove(addr)
    if found:
        await save_db()
        await bot.reply_to(m, f"✅ Удалён из watchlist:\n<code>{esc(addr)}</code>")
    else:
        await bot.reply_to(m, "Адрес не найден в watchlist")


@bot.message_handler(commands=["ignore"])
async def cmd_ignore(m: types.Message) -> None:
    if not is_owner(m.from_user.id): return
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Пример: /ignore 0xADDRESS"); return
    addr = args[1].lower()
    if not Web3.is_address(addr):
        await bot.reply_to(m, "❌ Невалидный адрес"); return
    async with db_lock:
        if addr not in db["cfg"]["ignore"]:
            db["cfg"]["ignore"].append(addr)
    await save_db()
    await bot.reply_to(m, f"✅ Ignore:\n<code>{esc(addr)}</code>")


@bot.message_handler(commands=["unignore"])
async def cmd_unignore(m: types.Message) -> None:
    if not is_owner(m.from_user.id): return
    args = m.text.split()
    if len(args) < 2:
        await bot.reply_to(m, "Пример: /unignore 0xADDRESS"); return
    addr = args[1].lower()
    async with db_lock:
        found = addr in db["cfg"]["ignore"]
        if found: db["cfg"]["ignore"].remove(addr)
    if found:
        await save_db()
        await bot.reply_to(m, f"✅ Удалён из ignore:\n<code>{esc(addr)}</code>")
    else:
        await bot.reply_to(m, "Адрес не найден")


@bot.message_handler(commands=["cancel"])
async def cmd_cancel(m: types.Message) -> None:
    clear_state(m.from_user.id)
    await bot.reply_to(m, "✅ Отменено.")


@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "ask_ai")
async def handle_ask_ai(m: types.Message) -> None:
    clear_state(m.from_user.id)
    wait = await bot.reply_to(m, "⏳ AI думает...")
    async with ai_sem:
        answer = await call_ai(
            f"{m.text}\n\nОтвечай на русском языке. Без HTML-тегов."
        )
    try:
        await bot.edit_message_text(
            f"🧠 <b>Ответ AI:</b>\n\n{answer}", m.chat.id, wait.message_id
        )
    except Exception:
        await safe_send(m.chat.id, f"🧠 <b>Ответ AI:</b>\n\n{answer}")


@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "check_contract")
async def handle_check_state(m: types.Message) -> None:
    clear_state(m.from_user.id)
    m.text = f"/check {m.text.strip()}"
    await cmd_check(m)


# ---------------------------------------------------------------------------
# GRACEFUL SHUTDOWN
# ---------------------------------------------------------------------------

async def graceful_shutdown(sig_name: str) -> None:
    global _shutdown
    logger.info(f"🛑 {sig_name} — начинаем завершение...")
    _shutdown = True

    try:
        await asyncio.wait_for(
            asyncio.gather(tx_queue.join(), log_queue.join()),
            timeout=30,
        )
        logger.info("✅ Очереди опустошены")
    except asyncio.TimeoutError:
        logger.warning("⚠️  Очереди не опустели за 30 сек — принудительно")

    await save_db()
    logger.info("✅ БД сохранена")

    for task in _main_tasks:
        if not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# APPROVE SCANNING
# ---------------------------------------------------------------------------

async def scan_approvals(address: str) -> list[dict]:
    """Сканирует approve разрешения для адреса"""
    try:
        # Получаем все трансферы токенов (ERC20 Transfer)
        logs = await rpc({
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": "0x0",
                "toBlock": "latest",
                "address": None,  # Все адреса
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",  # Transfer(topic0)
                    None,  # from (topic1)
                    None,  # to (topic2)
                ]
            }],
            "id": 1
        })
        
        # Находим токены, которыми владел пользователь
        user_tokens = set()
        for log in logs.get("result", []):
            topics = log.get("topics", [])
            if len(topics) >= 3:
                to_addr = "0x" + topics[2][-40:]  # Получаем to адрес из topic2
                if to_addr.lower() == address.lower():
                    token_addr = log.get("address", "")
                    user_tokens.add(token_addr.lower())
        
        # Теперь сканируем approve для этих токенов
        approvals = []
        for token_addr in user_tokens:
            try:
                # Получаем все approve для этого токена
                approve_logs = await rpc({
                    "jsonrpc": "2.0",
                    "method": "eth_getLogs",
                    "params": [{
                        "fromBlock": "0x0",
                        "toBlock": "latest",
                        "address": token_addr,
                        "topics": [
                            "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",  # Approval(topic0)
                            None,  # owner (topic1)
                            None,  # spender (topic2)
                        ]
                    }],
                    "id": 1
                })
                
                for log in approve_logs.get("result", []):
                    topics = log.get("topics", [])
                    if len(topics) >= 3:
                        owner = "0x" + topics[1][-40:]
                        spender = "0x" + topics[2][-40:]
                        
                        if owner.lower() == address.lower():
                            # Получаем данные из log.data
                            data = log.get("data", "0x")
                            if len(data) >= 66:  # 0x + 32 bytes
                                amount = int(data[-64:], 16)
                                
                                # Проверяем если allowance > 0
                                if amount > 0:
                                    # Получаем информацию о токене
                                    token_info = await get_token_info(token_addr)
                                    
                                    # Проверяем spender на скам
                                    spender_risks = await check_scam(spender)
                                    
                                    approvals.append({
                                        "tokenAddress": token_addr,
                                        "tokenName": token_info.get("name", "Unknown"),
                                        "tokenSymbol": token_info.get("symbol", "???"),
                                        "spenderAddress": spender,
                                        "amount": amount,
                                        "amountFormatted": format_amount(amount, token_info.get("decimals", 18)),
                                        "risk": "high" if spender_risks else "medium",
                                        "risks": spender_risks,
                                        "txHash": log.get("transactionHash", ""),
                                        "blockNumber": int(log.get("blockNumber", "0x0"), 16)
                                    })
            except Exception as e:
                logger.warning(f"Error scanning token {token_addr}: {e}")
                continue
        
        # Сортируем по риску и дате
        approvals.sort(key=lambda x: (x["risk"] != "high", -x["blockNumber"]))
        return approvals[:20]  # Возвращаем топ 20
        
    except Exception as e:
        logger.error(f"scan_approvals error: {e}")
        return []

async def get_token_info(token_addr: str) -> dict:
    """Получает информацию о токене"""
    try:
        # Пробуем получить name и symbol
        name_result = await rpc({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": token_addr,
                "data": "0x06fdde03"  # name()
            }, "latest"],
            "id": 1
        })
        
        symbol_result = await rpc({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": token_addr,
                "data": "0x95d89b41"  # symbol()
            }, "latest"],
            "id": 1
        })
        
        decimals_result = await rpc({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": token_addr,
                "data": "0x313ce567"  # decimals()
            }, "latest"],
            "id": 1
        })
        
        def decode_hex_string(hex_str):
            if hex_str.startswith("0x"):
                hex_str = hex_str[2:]
            if len(hex_str) >= 64:
                hex_str = hex_str[64:]
            try:
                return bytes.fromhex(hex_str).decode('utf-8').rstrip('\x00')
            except:
                return ""
        
        name = decode_hex_string(name_result.get("result", "0x"))
        symbol = decode_hex_string(symbol_result.get("result", "0x"))
        
        decimals_hex = decimals_result.get("result", "0x")
        if decimals_hex.startswith("0x"):
            decimals = int(decimals_hex, 16)
        else:
            decimals = 18
            
        return {
            "name": name or "Unknown Token",
            "symbol": symbol or "???",
            "decimals": decimals
        }
        
    except Exception as e:
        logger.warning(f"get_token_info error for {token_addr}: {e}")
        return {"name": "Unknown", "symbol": "???", "decimals": 18}

def format_amount(amount: int, decimals: int) -> str:
    """Форматирует количество токенов"""
    try:
        if amount == 115792089237316195423570985008687907853269984665640564039457584007913129639935:
            return "Unlimited"
        
        value = amount / (10 ** decimals)
        
        if value >= 1000000:
            return f"{value/1000000:.1f}M"
        elif value >= 1000:
            return f"{value/1000:.1f}K"
        elif value >= 1:
            return f"{value:.2f}"
        else:
            return f"{value:.6f}"
    except:
        return str(amount)

# HEALTH SERVER (POST /webapp/connect)
# ---------------------------------------------------------------------------

async def _run_health_server() -> None:
    logger.info("🚀 _run_health_server: попытка запуска...")
    try:
        from aiohttp import web
        port = int(os.getenv("PORT", "8080"))
        logger.info(f"🔄 _run_health_server: порт {port}")
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }

        async def handle(_):
            return web.Response(text="ok", headers=cors_headers)

        async def handle_webapp_connect(request):
            logger.info(f"📥 POST /webapp/connect вызван от {request.remote}")
            try:
                payload = await request.json()
            except Exception:
                logger.warning("❌ Ошибка парсинга JSON в /webapp/connect")
                return web.json_response({"ok": False, "error": "bad json"}, status=400, headers=cors_headers)

            nonce = str(payload.get("nonce", "")).strip()
            address = str(payload.get("address", "")).strip()
            signature = str(payload.get("signature", "").strip())
            logger.info(f"📦 /webapp/connect данные: nonce={nonce[:8]}..., address={address[:8]}...")

            if not nonce or not address or not signature:
                logger.warning("❌ Отсутствуют обязательные поля в /webapp/connect")
                return web.json_response({"ok": False, "error": "missing fields"}, status=400, headers=cors_headers)

            uid: Optional[int] = None
            async with db_lock:
                for uid_str, p in db.get("pending_verifications", {}).items():
                    if str(p.get("nonce", "")) == nonce:
                        try:
                            uid = int(uid_str)
                        except Exception:
                            uid = None
                        break

            logger.info(f"🔍 handle_webapp_connect: найден uid из nonce: {uid}")

            if uid is None:
                logger.warning(f"❌ Сессия не найдена для nonce={nonce[:8]}...")
                return web.json_response({"ok": False, "error": "session not found"}, status=404, headers=cors_headers)

            success, message = await verify_wallet(uid, address, signature)
            if success:
                await safe_send(
                    uid,
                    f"✅ <b>Кошелёк подключён!</b>\n"
                    f"<code>{esc(address.lower())}</code>\n\n"
                    f"Теперь ты получаешь личные алерты о всех транзакциях "
                    f"этого адреса.",
                )
                # После успешной верификации запускаем минт Guardian в фоне
                logger.info(f"🔍 Запускаем mint_guardian_for_user с uid={uid}")
                asyncio.create_task(mint_guardian_for_user(uid))
                logger.info(f"✅ Кошелёк подключен и минт Guardian запущен для user_id={uid}")
                return web.json_response({"ok": True}, headers=cors_headers)

            return web.json_response({"ok": False, "error": str(message)[:200]}, status=400, headers=cors_headers)

        async def handle_approvals(request):
            logger.info(f"📥 {request.method} /webapp/approvals вызван от {request.remote}")
            address = None
            if request.method == "POST":
                try:
                    data = await request.json()
                    address = data.get("address")
                except: pass
            elif request.method == "GET":
                address = request.query.get("address")
        
            if not address or not Web3.is_address(address):
                logger.warning(f"❌ Невалидный адрес: {address}")
                return web.json_response({"ok": False, "error": "Invalid address"}, headers=cors_headers)

            try:
                # Используем GoPlus (Сеть 204 = opBNB)
                url = f"https://api.gopluslabs.io/api/v1/token_approvals?chain_id=204&user_address={address}"
                async with http_session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    raw_approvals = data.get("result", [])
                    
                    clean_approvals = []
                    for token in raw_approvals:
                        token_addr = token.get("token_address")
                        token_name = token.get("token_name", "Unknown")
                        for spender in token.get("approved_list", []):
                            allowance = spender.get("allowance")
                            if allowance and allowance != "0":
                                clean_approvals.append({
                                    "tokenAddress": token_addr,
                                    "tokenName": token_name,
                                    "spenderAddress": spender.get("approved_contract"),
                                    "amount": allowance,
                                    "risk": "high" if spender.get("is_danger") == 1 else "low"
                                })
                    logger.info(f"✅ Найдено {len(clean_approvals)} approvals для {address[:8]}...")
                    return web.json_response({"ok": True, "approvals": clean_approvals}, headers=cors_headers)
            except Exception as e:
                logger.error(f"❌ Ошибка в /webapp/approvals: {e}")
                return web.json_response({"ok": False, "error": str(e)}, headers=cors_headers)

        async def handle_webapp_approvals(request):
            return await handle_approvals(request)

        async def handle_webapp_connect_options(_):
            return web.Response(headers=cors_headers)

        logger.info("🔧 Создание приложения и регистрация роутов...")
        app = web.Application()
        app.router.add_get("/", handle)
        app.router.add_post("/webapp/connect", handle_webapp_connect)
        app.router.add_get("/webapp/approvals", handle_webapp_approvals)
        app.router.add_post("/webapp/approvals", handle_webapp_approvals)
        app.router.add_options("/{tail:.*}", handle_webapp_connect_options)
        
        logger.info("🚀 Запуск AppRunner и TCPSite...")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        logger.info(f"✅ Health server listening on 0.0.0.0:{port}")

        try:
            while not _shutdown:
                await asyncio.sleep(1)
        finally:
            await runner.cleanup()
            logger.info("✅ Health server stopped")
            
    except Exception as e:
        logger.error(f"❌ _run_health_server упал с ошибкой: {e}", exc_info=True)
        raise  # можно не выбрасывать, чтобы задача завершилась, но ошибка залогирована


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main() -> None:
    global http_session

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(graceful_shutdown(s.name)),
            )
        except (NotImplementedError, OSError):
            if sig == signal.SIGINT:
                pass
            logger.debug(f"Signal {sig} не зарегистрирован (возможно Windows)")

    logger.info("🧹 Удаляем webhook...")
    for attempt in range(3):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook удалён")
            break
        except Exception as e:
            logger.warning(f"Webhook попытка {attempt+1}/3: {e}")
            if attempt < 2:
                await asyncio.sleep(3)

    # HTTP сессия
    connector    = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(connector=connector)

    # Health сервер для /webapp/connect
    health_task = asyncio.create_task(_run_health_server())

    # БД
    await init_db()
    logger.info("✅ SQLite подключена")

    try:
        # ВРЕМЕННО: пропускаем проверку блокчейна для быстрого старта
        logger.info("⚡ Быстрый старт без проверки блокчейна")
        # w3 = get_smart_w3(_RAW_HTTP_URL)
        # chain_id = w3.eth.chain_id
        # if chain_id != 204:
        #     logger.error(f"❌ Неверная сеть! Ожидается opBNB (204), получено {chain_id}")
        # else:
        #     logger.info("✅ Умное подключение к opBNB Mainnet установлено")
    except Exception as e:
        logger.warning(f"Не удалось проверить chainId: {e}")

    await refresh_bnb_price()

    logger.info(
        f"🚀 VibeGuard v24.4 ЗАПУЩЕН | "
        f"limit=${db['cfg']['limit_usd']:,.0f} | "
        f"BNB=${_price_cache.get('BNB', 0):.2f} | "
        f"onchain={'ON' if ENABLE_ONCHAIN else 'OFF'}"
    )

    polling_task = asyncio.create_task(
        bot.infinity_polling(allowed_updates=["message", "callback_query"])
    )
    monitor_task = asyncio.create_task(monitor())
    tx_workers   = [asyncio.create_task(tx_worker(i))  for i in range(6)]
    log_workers  = [asyncio.create_task(log_worker(i)) for i in range(4)]

    _main_tasks.extend([polling_task, monitor_task, health_task])

    try:
        await asyncio.gather(
            polling_task,
            monitor_task,
            health_task,
            *tx_workers,
            *log_workers,
            return_exceptions=True,
        )
    finally:
        _shutdown = True
        for t in tx_workers + log_workers:
            t.cancel()
        await save_db()
        if http_session and not http_session.closed:
            await http_session.close()
        if pool:
            await pool.close()
        logger.info("✅ Все ресурсы освобождены")


@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "wait_limit")
async def handle_limit_input(m: types.Message) -> None:
    uid = m.from_user.id
    try:
        val = float(m.text.strip().replace("$", "").replace(",", ""))
        min_allowed = 1.0 if is_owner(uid) else 3000.0
        
        if val < min_allowed:
            await bot.reply_to(m, f"❌ Минимальный лимит: ${min_allowed:,.0f}")
            return

        async with db_lock:
            if "user_limits" not in db: db["user_limits"] = {}
            db["user_limits"][str(uid)] = val
        await save_db()
        clear_state(uid)
        await bot.reply_to(m, f"✅ Твой личный лимит установлен: <b>${val:,.0f}</b>", reply_markup=get_main_menu_keyboard())
    except ValueError:
        await bot.reply_to(m, "❌ Введи просто число (например: 5000)")


if __name__ == "__main__":
    asyncio.run(main())
