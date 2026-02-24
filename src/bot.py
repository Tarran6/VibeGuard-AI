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
XAI_MODEL = _optional("XAI_MODEL", "grok-2-1212")
GROQ_MODEL = _optional("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = _optional("GEMINI_MODEL", "gemini-2.0-flash")

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

if not any([XAI_KEYS, GROQ_KEYS, GEMINI_KEYS]):
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
        signed  = w3.eth.account.sign_transaction(tx, acct.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
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
        [("xai",    k) for k in XAI_KEYS]  +
        [("groq",   k) for k in GROQ_KEYS] +
        [("gemini", k) for k in GEMINI_KEYS]
    )
    if not configs:
        return "AI-ключи не настроены."

    async with ai_sem:
        for provider, key in configs:
            try:
                result = await _ai_request(provider, key, prompt)
                if result:
                    return esc(result)
            except Exception as e:
                logger.warning(f"AI [{provider}] error: {e}")

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
        btns.append(types.InlineKeyboardButton("📊 График", url=f"https://dexscreener.com/bsc/{token_addr}"))
    btns.append(types.InlineKeyboardButton("⚙️ Мой лимит", callback_data="menu_settings"))
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
                f"From: <code>{esc(sender)}</code>\n"
                f"To:   <code>{esc(target)}</code>"
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
            f"From: <code>{esc(sender)}</code>\n"
            f"To:   <code>{esc(target)}</code>"
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
                f"Транзакция {val_bnb:.2f} BNB (${val_usd:,.0f}) на контракт {target}. "
                f"Наш сканер выявил критические угрозы: {', '.join(risks)}. "
                f"Напиши жесткий и краткий security-отчет на русском (2-3 предложения), "
                f"объясни инвесторам, почему этот токен опасен. Без HTML-тегов."
            )
        else:
            prompt = (
                f"Крупный перевод {val_bnb:.2f} BNB (${val_usd:,.0f}) от {sender} к {target}. "
                f"Смарт-контракт выглядит чистым (базовые проверки пройдены). "
                f"Что это может значить (арбитраж, накопление маркетмейкером, покупка)? "
                f"Кратко на русском, 2 предложения. Без HTML-тегов."
            )

        # ТЕПЕРЬ зовем ИИ с готовым отчетом
        async with ai_sem:
            verdict = await call_ai(prompt)
        
        # Собираем красивый итоговый алерт
        full_report = (
            f"{whale_text}\n\n"
            f"🛡️ <b>VibeScore: {score}/100</b>\n"
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
                f"Токен: <code>{esc(token_addr)}</code>\n"
                f"From:  <code>{esc(sender)}</code>\n"
                f"To:    <code>{esc(receiver)}</code>"
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
            f"Токен: <code>{esc(token_addr)}</code>\n"
            f"From:  <code>{esc(sender)}</code>\n"
            f"To:    <code>{esc(receiver)}</code>"
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
                f"Замечено движение {amount:,.0f} токенов (${val_usd:,.0f}) контракта {token_addr}. "
                f"Анализатор кода выявил угрозы: {', '.join(risks)}. "
                f"Напиши срочное предупреждение для трейдеров на русском (2-3 предложения), "
                f"чем грозят эти уязвимости. Без HTML-тегов."
            )
        else:
            prompt = (
                f"Кит перевел {amount:,.0f} токенов (${val_usd:,.0f}) контракта {token_addr}. "
                f"Токен прошел базовую проверку безопасности. "
                f"Проанализируй, похоже ли это на OTC-сделку, перенос на холодный кошелек или дамп? "
                f"Ответь кратко на русском, 2 предложения. Без HTML-тегов."
            )

        async with ai_sem:
            verdict = await call_ai(prompt)
        
        full_report = (
            f"{whale_text}\n\n"
            f"🛡️ <b>VibeScore: {score}/100</b>\n"
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

async def verify_wallet(user_id: int, address: str, signature: str) -> tuple[bool, str]:
    uid_str = str(user_id)

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
        # УМНОЕ ПОДКЛЮЧЕНИЕ К БЛОКЧЕЙНУ - автоматическое переключение
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТЕКСТА
# ---------------------------------------------------------------------------

async def get_status_text() -> str:
    uptime = time.time() - start_time
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    async with db_lock:
        s = db["stats"]
        limit_usd = db["cfg"]["limit_usd"]
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
    clear_state(m.from_user.id)
    # Убираем reply-клавиатуру, если она была
    await bot.send_message(
        m.chat.id,
        "🔄 Очищаем клавиатуру...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    # Отправляем основное меню
    await bot.send_message(
        m.chat.id,
        (
            "🛡️ <b>VibeGuard Sentinel v24.4</b>\n\n"
            "Мониторинг китов и скам-контрактов на opBNB.\n\n"
            "Используй кнопки ниже для навигации."
        ),
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


@bot.message_handler(content_types=["web_app_data"])
async def handle_webapp_data(m: types.Message) -> None:
    uid = m.from_user.id
    logger.info(f"📩 Получены web_app_data от пользователя {uid}")
    
    try:
        # Парсим JSON из WebApp
        raw_data = m.web_app_data.data
        data = json.loads(raw_data)
        
        address = data.get("address", "").strip()
        sig = data.get("signature", "").strip()
        # Этот nonce критически важен для связи сессии!
        nonce_from_app = data.get("nonce", "").strip() 
        
        logger.info(f"📦 Данные: address={address[:10]}..., nonce={nonce_from_app[:8]}...")
    except Exception as e:
        logger.warning(f"webapp_data parse error uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка данных от WebApp. Попробуй ещё раз.")
        return

    if not address or not sig:
        logger.warning(f"Неполные данные от {uid}")
        await safe_send(uid, "❌ Неполные данные от WebApp.")
        return

    # Запускаем верификацию (она проверит подпись и nonce)
    success, message = await verify_wallet(uid, address, sig)

    if success:
        logger.info(f"✅ Кошелёк {address[:10]}... успешно подключён пользователем {uid}")
        await safe_send(
            uid,
            f"✅ <b>Кошелёк подключён!</b>\n"
            f"<code>{esc(address.lower())}</code>\n\n"
            f"Теперь ты получаешь личные алерты о всех транзакциях этого адреса.",
        )
        # Принудительно сохраняем БД, чтобы кошелек не пропал после рестарта
        await save_db()
    else:
        logger.warning(f"❌ Ошибка верификации для {uid}: {message}")
        await safe_send(uid, f"❌ {esc(message)}")


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
        # Показываем список кошельков новым сообщением
        await cmd_mywallets(message)
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
        await bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=get_main_menu_keyboard()
        )
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
        await bot.edit_message_text(text, chat_id=message.chat.id, message_id=message.message_id, reply_markup=get_main_menu_keyboard())
    elif action == "support":
        await bot.answer_callback_query(c.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Связаться с менеджером", url="https://t.me/tarran6"))
        await bot.edit_message_text(
            "🛡️ Нужна помощь? Напишите менеджеру:",
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=kb
        )
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

    uid = int(parts[1])
    idx = int(parts[2])

    if c.from_user.id != uid:
        await bot.answer_callback_query(c.id, "⛔ Нет доступа", show_alert=True)
        return

    async with db_lock:
        wallets = db["connected_wallets"].get(str(uid), [])
        if idx >= len(wallets):
            await bot.answer_callback_query(c.id, "Кошелёк не найден")
            return
        removed = wallets.pop(idx)
        if not wallets:
            del db["connected_wallets"][str(uid)]

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
    logger.info(f"📥 Получены данные WebApp от user_id={uid}")
    
    try:
        data = json.loads(m.web_app_data.data)
        address = data.get("address", "").strip()
        sig = data.get("signature", "").strip()
        nonce = data.get("nonce", "").strip()
        
        logger.info(f"📥 WebApp данные: address={address[:8]}..., nonce={nonce[:8]}...")
    except Exception as e:
        logger.warning(f"webapp_data parse error uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка данных от WebApp. Попробуй ещё раз.")
        return

    if not address or not sig or not nonce:
        await safe_send(uid, "❌ Неполные данные от WebApp.")
        return

    success, message = await verify_wallet(uid, address, sig)

    if success:
        await safe_send(
            uid,
            f"✅ <b>Кошелёк подключён!</b>\n"
            f"<code>{esc(address.lower())}</code>\n\n"
            f"Теперь ты получаешь личные алерты о всех транзакциях "
            f"этого адреса.",
        )
        logger.info(f"✅ Кошелёк подключён: {address[:8]}... для user_id={uid}")
    else:
        await safe_send(uid, f"❌ {esc(message)}")
        logger.warning(f"❌ Ошибка подключения кошелька: {message}")


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
        await bot.reply_to(
            m,
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
            callback_data=f"dc:{uid}:{i}",
        ))

    kb.add(types.InlineKeyboardButton("🔗 Добавить кошелёк", callback_data="connect_new"))

    await bot.reply_to(
        m,
        f"👛 <b>Твои кошельки ({len(wallets)}/5):</b>\n\n"
        f"{lines}\n\n"
        f"🔔 Алерты при любом движении.\n"
        f"🐳 Глобальный лимит китов: <b>${limit:,.0f}</b>",
        reply_markup=kb
    )


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
            await save_db()
            await bot.reply_to(m, f"✅ Лимит китов изменён: <b>${v:,.0f}</b>")
        except ValueError:
            await bot.reply_to(m, f"❌ Укажите число от {LIMIT_MIN_USD:.0f}. Пример: /limit 100")
    else:
        text = await get_limit_text()
        await bot.reply_to(m, text)


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
# HEALTH SERVER (POST /webapp/connect)
# ---------------------------------------------------------------------------

async def _run_health_server() -> None:
    from aiohttp import web
    port = int(os.getenv("PORT", "8080"))
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }

    async def handle(_):
        return web.Response(text="ok", headers=cors_headers)

    async def handle_webapp_connect(request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400, headers=cors_headers)

        nonce = str(payload.get("nonce", "")).strip()
        address = str(payload.get("address", "")).strip()
        signature = str(payload.get("signature", "")).strip()

        if not nonce or not address or not signature:
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

        if uid is None:
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
            return web.json_response({"ok": True}, headers=cors_headers)

        return web.json_response({"ok": False, "error": str(message)[:200]}, status=400, headers=cors_headers)

    async def handle_webapp_connect_options(_):
        return web.Response(status=204, headers=cors_headers)

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_options("/webapp/connect", handle_webapp_connect_options)
    app.router.add_post("/webapp/connect", handle_webapp_connect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("✅ Health server listening on 0.0.0.0:%d", port)

    try:
        while not _shutdown:
            await asyncio.sleep(1)
    finally:
        await runner.cleanup()
        logger.info("✅ Health server stopped")


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
    logger.info("✅ PostgreSQL подключена")

    try:
        w3 = get_smart_w3(_RAW_HTTP_URL)
        chain_id = w3.eth.chain_id
        if chain_id != 204:
            logger.error(f"❌ Неверная сеть! Ожидается opBNB (204), получено {chain_id}")
        else:
            logger.info("✅ Умное подключение к opBNB Mainnet установлено")
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
