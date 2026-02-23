# =============================================================================
#  VibeGuard Sentinel — src/bot.py
#  Version: 24.0
#  Python: 3.11+
#
#  Архитектура:
#    • Мониторинг opBNB: нативный BNB + все ERC-20 (через eth_getLogs)
#    • Лимит уведомлений в USD (CoinGecko, кэш 2 мин)
#    • Подключение кошелька через Telegram WebApp + ethers.js (WalletConnect UX)
#    • Личные алерты владельцам кошельков — БЕЗ on-chain логирования
#    • On-chain логирование (logScan) — ТОЛЬКО для чужих китов
#    • Graceful shutdown: сохранение БД гарантировано при SIGTERM/SIGINT
#    • Очереди TX + Transfer-логов с воркерами фиксированного числа
#    • Все исключения логируются, нет голых except
#    • Архитектура "Гидра": поддержка пула RPC-ссылок через запятую
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
    """Читает обязательную переменную окружения. Падает с понятной ошибкой."""
    v = os.getenv(key, "").strip()
    if not v:
        raise EnvironmentError(f"Переменная окружения не задана: {key}")
    return v


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# Обязательные
TELEGRAM_TOKEN   = _require("TELEGRAM_TOKEN")
DATABASE_URL     = _require("DATABASE_URL")
PRIMARY_OWNER_ID = int(_require("PRIMARY_OWNER_ID"))

# Парсинг пула RPC ссылок (Архитектура "Гидра")
_RAW_HTTP_URL = _require("OPBNB_HTTP_URL")
HTTP_URLS = [u.strip() for u in _RAW_HTTP_URL.split(",") if u.strip()]
if not HTTP_URLS:
    raise EnvironmentError("Переменная OPBNB_HTTP_URL пуста или содержит невалидные данные")

# Опциональные
GEMINI_KEYS = [k for k in _optional("GEMINI_API_KEY").split(",") if k.strip()]
GROQ_KEYS   = [k for k in _optional("GROQ_API_KEY").split(",")   if k.strip()]
XAI_KEYS    = [k for k in _optional("XAI_API_KEY").split(",")    if k.strip()]

GOPLUS_APP_KEY    = _optional("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _optional("GOPLUS_APP_SECRET")

# On-chain логирование (ТОЛЬКО для китов, не для подключённых кошельков)
ENABLE_ONCHAIN    = _optional("ENABLE_ONCHAIN_LOG") == "true"
ONCHAIN_PRIVKEY   = _optional("WEB3_PRIVATE_KEY")
ONCHAIN_CONTRACT  = _optional("VIBEGUARD_CONTRACT")

# URL веб-приложения (Telegram WebApp для Connect Wallet)
WEBAPP_URL = _optional("WEBAPP_URL", "")

LOGO_URL = _optional(
    "LOGO_URL",
    "https://raw.githubusercontent.com/Tarran6/VibeGuard-AI/main/logo.png"
)

OWNERS: set[int] = {PRIMARY_OWNER_ID}

# ERC-20 Transfer(address,address,uint256) topic
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

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
    "last_block": 0,
    # {str(telegram_user_id): [{"address": "0x...", "label": "Wallet N"}]}
    "connected_wallets": {},
    # Временные nonce для верификации: {str(user_id): {"nonce": str, "ts": float}}
    "pending_verifications": {},
}

db: dict = {}

# ---------------------------------------------------------------------------
# ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ---------------------------------------------------------------------------

bot        = AsyncTeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
pool:       Optional[asyncpg.Pool]          = None
http_session: Optional[aiohttp.ClientSession] = None
start_time = time.time()

# Семафоры
rpc_sem  = Semaphore(10)
ai_sem   = Semaphore(3)
tg_sem   = Semaphore(20)
db_lock  = Lock()
price_lock = Lock()

# Очереди
tx_queue:  Queue = Queue(maxsize=8_000)
log_queue: Queue = Queue(maxsize=8_000)

# Флаг остановки и ссылки на задачи для graceful shutdown
_shutdown    = False
_main_tasks: list[asyncio.Task] = []

# Кэш цен {symbol_or_address: price_usd}
_price_cache:    dict[str, float] = {}
_price_cache_ts: float            = 0.0
PRICE_TTL = 120  # секунд

# Кэш цен токенов с TTL: {token_addr: (price_usd, timestamp)}
_token_price_cache: dict[str, tuple[float, float]] = {}

# Минимальный лимит китов в USD (владелец может ставить от 100 и выше)
LIMIT_MIN_USD = 100.0

# Кэш decimals токенов
_decimals_cache: dict[str, int] = {}

# user_states: {user_id: {"state": str, "ts": float}}
_user_states: dict[int, dict] = {}
STATE_TTL = 600  # 10 минут

# ---------------------------------------------------------------------------
# УТИЛИТЫ
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    """HTML-экранирование (включая &)."""
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
            # Превращаем строку из базы в словарь
            loaded = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            
            # Глубокий merge с дефолтом — защита от неполных данных
            db = {**_DB_DEFAULT, **loaded}
            db["stats"] = {**_DB_DEFAULT["stats"], **loaded.get("stats", {})}
            db["cfg"]   = {**_DB_DEFAULT["cfg"],   **loaded.get("cfg",   {})}
            if db["cfg"]["limit_usd"] < LIMIT_MIN_USD:
                db["cfg"]["limit_usd"] = LIMIT_MIN_USD
            db.setdefault("connected_wallets",     {})
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
    """Сохраняет db с retry x3."""
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
    """Обновляет цену BNB не чаще раза в PRICE_TTL секунд."""
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
# RPC (С поддержкой пула ключей)
# ---------------------------------------------------------------------------

async def rpc(payload: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=12)
    async with rpc_sem:
        last_error = None
        for url in HTTP_URLS:
            try:
                async with http_session.post(url, json=payload, timeout=timeout) as r:
                    if r.status == 429:
                        last_error = "RPC 429"
                        continue  # 429 лимит — пробуем следующую ссылку в списке
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                last_error = str(e)
                continue  # Ошибка соединения — пробуем следующую ссылку
        
        # Если код дошел сюда, значит ни одна из ссылок не сработала
        if last_error == "RPC 429":
            raise RuntimeError("RPC 429")
        raise RuntimeError(f"Все RPC узлы недоступны. Последняя ошибка: {last_error}")


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
    """Все ERC-20 Transfer события за диапазон блоков."""
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
# ON-CHAIN ЛОГИРОВАНИЕ (только для китов, не для подключённых кошельков)
# ---------------------------------------------------------------------------

_SCAN_ABI = [{
    "inputs": [
        {"name": "_contract", "type": "address"},
        {"name": "_score",    "type": "uint256"},
        {"name": "_isSafe",   "type": "bool"},
        {"name": "_user",     "type": "address"},
    ],
    "name": "logScan",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]


async def log_onchain(target: str, score: int, is_safe: bool) -> None:
    """
    Логирует факт скана в смарт-контракт VibeGuard.
    Вызывается ТОЛЬКО для китовых транзакций.
    Для подключённых кошельков НЕ вызывается.
    """
    if not ENABLE_ONCHAIN or not ONCHAIN_PRIVKEY or not ONCHAIN_CONTRACT:
        return
    if not Web3.is_address(target) or not Web3.is_address(ONCHAIN_CONTRACT):
        return

    # Запускаем в отдельном потоке — синхронный Web3
    def _do_log():
        # Используем первый URL из пула для on-chain логов
        w3 = Web3(Web3.HTTPProvider(HTTP_URLS[0]))
        acct     = w3.eth.account.from_key(ONCHAIN_PRIVKEY)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ONCHAIN_CONTRACT),
            abi=_SCAN_ABI,
        )
        tx = contract.functions.logScan(
            Web3.to_checksum_address(target),
            score, is_safe, acct.address,
        ).build_transaction({
            "from":     acct.address,
            "nonce":    w3.eth.get_transaction_count(acct.address),
            "gas":      130_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed  = w3.eth.account.sign_transaction(tx, acct.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    try:
        loop    = asyncio.get_running_loop()
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
            "model": "grok-2-latest",
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "groq":
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
        }
    else:  # gemini
        url     = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key}"
        )
        headers = {}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

    async with http_session.post(
        url, json=payload, headers=headers, timeout=timeout
    ) as r:
        if r.status == 429:
            raise RuntimeError("Rate limit 429")
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
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
            d    = data.get("result", {}).get(addr.lower(), {})
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
    """Telegram user_id всех кто подключил данный адрес."""
    addr = address.lower()
    result = []
    for uid_str, wallets in db.get("connected_wallets", {}).items():
        if any(w["address"].lower() == addr for w in wallets):
            result.append(int(uid_str))
    return result


def _is_connected_wallet(address: str) -> bool:
    """True если адрес зарегистрирован как подключённый кошелёк."""
    addr = address.lower()
    for wallets in db.get("connected_wallets", {}).values():
        if any(w["address"].lower() == addr for w in wallets):
            return True
    return False


# ---------------------------------------------------------------------------
# ОБРАБОТКА BNB-ТРАНЗАКЦИЙ
# ---------------------------------------------------------------------------

async def process_bnb_tx(tx: dict) -> None:
    try:
        val_bnb = int(tx.get("value", "0x0"), 16) / 10 ** 18
        if val_bnb == 0:
            return  # Токены идут через логи

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

        # ── Персональные алерты для подключённых кошельков ──────────────────
        # Порог не применяется — любое движение по подключённому кошельку важно
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
            # On-chain логирование для подключённых кошельков НЕ делаем
            return  # ← выходим, не идём в логику китов

        # ── Логика китов (только чужие адреса) ──────────────────────────────
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

        async with ai_sem:
            verdict = await call_ai(
                f"Кратко на русском: перевод {val_bnb:.2f} BNB "
                f"(${val_usd:,.0f}) от {sender} к {target}. "
                f"Что это может значить? Без HTML-тегов."
            )
        await notify_owners(f"{whale_text}\n\n🧠 <b>AI:</b> {verdict}")

        # Скам-проверка + on-chain логирование (только для китов!)
        risks = await check_scam(target)
        if risks:
            async with db_lock:
                db["stats"]["threats"] += 1
            threat = (
                f"🚨 <b>УГРОЗА СКАМ</b>\n"
                f"<code>{esc(target)}</code>\n"
                f"Риски: {esc(', '.join(risks))}"
            )
            await notify_owners(threat)

        score   = 25 if risks else 85
        is_safe = not bool(risks)
        # On-chain только для китов
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

        # ── Персональные алерты для подключённых кошельков ──────────────────
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
            # On-chain для подключённых кошельков НЕ делаем
            return

        # ── Логика китов ─────────────────────────────────────────────────────
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

        async with ai_sem:
            verdict = await call_ai(
                f"Кратко на русском: перевод {amount:,.0f} токенов "
                f"(${val_usd:,.0f}) контракта {token_addr}. "
                f"Что это может значить? Без HTML-тегов."
            )
        await notify_owners(f"{whale_text}\n\n🧠 <b>AI:</b> {verdict}")

        risks = await check_scam(token_addr)
        if risks:
            async with db_lock:
                db["stats"]["threats"] += 1
            await notify_owners(
                f"🚨 <b>СКАМ-ТОКЕН</b>\n"
                f"<code>{esc(token_addr)}</code>\n"
                f"Риски: {esc(', '.join(risks))}"
            )

        # On-chain только для китов
        asyncio.create_task(
            log_onchain(token_addr, 25 if risks else 85, not bool(risks))
        )

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

BLOCK_BATCH   = 2      # Качаем по 2 блока за раз, а не по 5
POLL_INTERVAL = 5.0    # Пауза между проверками сети
MAX_CATCHUP   = 50     # Догоняем максимум 50 блоков за цикл
SAVE_EVERY    = 20     # Чаще сохраняем базу (каждые 20 блоков)


async def monitor() -> None:
    logger.info("🔍 Мониторинг блокчейна запущен")
    save_counter = 0

    while not _shutdown:
        try:
            data    = await rpc({"jsonrpc": "2.0", "method": "eth_blockNumber", "id": 1})
            current = int(data.get("result", "0x0"), 16)

            async with db_lock:
                last = db.get("last_block", 0)

            # При первом запуске или большом отставании — стартуем с -5
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

            # Батчевая загрузка
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
# ВЕРИФИКАЦИЯ КОШЕЛЬКА (вызывается из WebApp через /api/verify)
# ---------------------------------------------------------------------------

async def verify_wallet(user_id: int, address: str, signature: str) -> tuple[bool, str]:
    """
    Проверяет подпись и регистрирует кошелёк.
    Возвращает (success: bool, message: str).
    Используется веб-приложением через callback /webapp_verify.
    """
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

    # Восстанавливаем адрес из подписи
    try:
        w3_local    = Web3()
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

    # Сохраняем кошелёк
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
# TELEGRAM — КЛАВИАТУРЫ
# ---------------------------------------------------------------------------

def kb_main() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📊 Status", "🧠 Ask AI")
    kb.add("👛 My Wallets", "🔍 Check Contract")
    kb.add("🛡️ Support")
    return kb


def kb_connect_wallet() -> types.InlineKeyboardMarkup:
    """Кнопка открывает Telegram WebApp."""
    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton(
            "🔗 Connect Wallet",
            web_app=types.WebAppInfo(url=WEBAPP_URL),
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "⚠️ WebApp не настроен (см. WEBAPP_URL)",
            callback_data="webapp_not_configured",
        ))
    return kb


# ---------------------------------------------------------------------------
# TELEGRAM — ОБРАБОТЧИКИ
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
async def cmd_start(m: types.Message) -> None:
    clear_state(m.from_user.id)
    await bot.send_photo(
        m.chat.id, LOGO_URL,
        caption=(
            "🛡️ <b>VibeGuard Sentinel v24.0</b>\n\n"
            "Мониторинг китов и скам-контрактов на opBNB.\n\n"
            "<b>Основные команды:</b>\n"
            "/connect — подключить кошелёк\n"
            "/mywallets — мои кошельки\n"
            "/disconnect — отвязать кошелёк\n"
            "/check 0x... — проверить контракт\n"
            "/limit — порог уведомлений\n"
            "/status — статистика бота"
        ),
        reply_markup=kb_main(),
    )


@bot.message_handler(commands=["connect"])
async def cmd_connect(m: types.Message) -> None:
    """
    Генерирует nonce, сохраняет в БД, отправляет кнопку WebApp.
    WebApp считывает nonce через Telegram.WebApp.initData,
    делает подпись в кошельке и отправляет обратно боту.
    """
    uid     = m.from_user.id
    nonce   = secrets.token_hex(16)
    uid_str = str(uid)

    async with db_lock:
        db["pending_verifications"][uid_str] = {
            "nonce": nonce,
            "ts":    time.time(),
        }
    await save_db()

    # Формируем URL с nonce как query-параметр
    webapp_url_with_nonce = f"{WEBAPP_URL}?nonce={nonce}" if WEBAPP_URL else ""

    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton(
            "🔗 Connect Wallet",
            web_app=types.WebAppInfo(url=webapp_url_with_nonce),
        ))
    else:
        kb.add(types.InlineKeyboardButton(
            "⚠️ WebApp не настроен",
            callback_data="webapp_not_configured",
        ))

    await bot.reply_to(
        m,
        "👛 <b>Подключение кошелька</b>\n\n"
        "Нажми кнопку ниже, выбери кошелёк (MetaMask, Trust Wallet и др.) "
        "и подтверди подпись одним тапом.\n\n"
        "<i>Сессия действительна 10 минут.</i>",
        reply_markup=kb,
    )


@bot.message_handler(content_types=["web_app_data"])
async def handle_webapp_data(m: types.Message) -> None:
    """
    Telegram отправляет результат WebApp сюда.
    WebApp передаёт JSON: {"address": "0x...", "signature": "0x..."}
    """
    uid = m.from_user.id
    try:
        data    = json.loads(m.web_app_data.data)
        address = data.get("address", "").strip()
        sig     = data.get("signature", "").strip()
    except Exception as e:
        logger.warning(f"webapp_data parse error uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка данных от WebApp. Попробуй ещё раз.")
        return

    if not address or not sig:
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
    else:
        await safe_send(uid, f"❌ {esc(message)}")


@bot.callback_query_handler(func=lambda c: c.data == "webapp_not_configured")
async def cb_webapp_not_configured(c: types.CallbackQuery) -> None:
    await bot.answer_callback_query(
        c.id,
        "WEBAPP_URL не задан в .env — см. README",
        show_alert=True,
    )


@bot.message_handler(commands=["mywallets"])
async def cmd_mywallets(m: types.Message) -> None:
    uid = m.from_user.id
    async with db_lock:
        wallets = list(db["connected_wallets"].get(str(uid), []))

    if not wallets:
        await bot.reply_to(
            m,
            "👛 У тебя нет подключённых кошельков.\n"
            "/connect — подключить.",
        )
        return

    async with db_lock:
        limit = db["cfg"]["limit_usd"]

    lines = "\n".join(
        f"{i+1}. <b>{esc(w['label'])}</b>\n   <code>{esc(w['address'])}</code>"
        for i, w in enumerate(wallets)
    )
    await bot.reply_to(
        m,
        f"👛 <b>Твои кошельки ({len(wallets)}/5):</b>\n\n"
        f"{lines}\n\n"
        f"🔔 Алерты при любом движении.\n"
        f"🐳 Глобальный лимит китов: <b>${limit:,.0f}</b>",
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

    # Защита: только сам пользователь может отключить свои кошельки
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
        f"<b>Статус:</b> {esc(status)}\n\n"
        f"🧠 <b>AI:</b> {verdict}"
    )
    try:
        await bot.edit_message_text(result_text, m.chat.id, wait.message_id)
    except Exception:
        await safe_send(m.chat.id, result_text)


@bot.message_handler(commands=["status", "stats"])
async def cmd_status(m: types.Message) -> None:
    uptime  = time.time() - start_time
    hours   = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)

    async with db_lock:
        s         = db["stats"]
        limit_usd = db["cfg"]["limit_usd"]
        last_b    = db.get("last_block", 0)
        wc        = len(db["cfg"]["watch"])
        ic        = len(db["cfg"]["ignore"])
        total_w   = sum(len(v) for v in db["connected_wallets"].values())

    bnb_price = _price_cache.get("BNB", 0.0)

    await bot.reply_to(
        m,
        f"🛡️ <b>VibeGuard Sentinel v24.0</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"Блоков:         <b>{s['blocks']:,}</b>\n"
        f"Последний блок: <b>{last_b:,}</b>\n"
        f"Китов:          <b>{s['whales']}</b>\n"
        f"Угроз:          <b>{s['threats']}</b>\n\n"
        f"⚙️ <b>Конфиг:</b>\n"
        f"Лимит китов:    <b>${limit_usd:,.0f}</b>\n"
        f"BNB цена:       <b>${bnb_price:.2f}</b>\n"
        f"Watchlist:      <b>{wc}</b> адресов\n"
        f"Ignore:         <b>{ic}</b> адресов\n"
        f"Кошельков:      <b>{total_w}</b>\n\n"
        f"📬 TX queue:  <b>{tx_queue.qsize()}</b>\n"
        f"📬 Log queue: <b>{log_queue.qsize()}</b>\n\n"
        f"⏱️ Uptime: <code>{hours}ч {minutes}м</code>"
    )


@bot.message_handler(commands=["limit"])
async def cmd_limit(m: types.Message) -> None:
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
        async with db_lock:
            cur = db["cfg"]["limit_usd"]
        await bot.reply_to(
            m,
            f"Лимит уведомлений о китах: <b>${cur:,.0f}</b>\n"
            f"Алерты о подключённых кошельках — при любых суммах.\n\n"
            f"Изменить (владелец): /limit 100 … /limit 1000000",
        )


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


@bot.message_handler(func=lambda m: m.text in {
    "📊 Status", "🧠 Ask AI", "👛 My Wallets", "🔍 Check Contract", "🛡️ Support"
})
async def handle_menu(m: types.Message) -> None:
    t = m.text
    if t == "📊 Status":
        await cmd_status(m)
    elif t == "🧠 Ask AI":
        set_state(m.from_user.id, "ask_ai")
        await bot.reply_to(
            m,
            "🤖 Задай любой вопрос о крипте или контрактах.\n/cancel — выйти.",
        )
    elif t == "👛 My Wallets":
        await cmd_mywallets(m)
    elif t == "🔍 Check Contract":
        set_state(m.from_user.id, "check_contract")
        await bot.reply_to(m, "Отправь адрес контракта для проверки:")
    elif t == "🛡️ Support":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Связаться с менеджером", url="https://t.me/tarran6"))
        await bot.send_message(m.chat.id, "Нужна помощь?", reply_markup=kb)


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
    # Переиспользуем логику команды /check
    m.text = f"/check {m.text.strip()}"
    await cmd_check(m)


# ---------------------------------------------------------------------------
# GRACEFUL SHUTDOWN
# ---------------------------------------------------------------------------

async def graceful_shutdown(sig_name: str) -> None:
    global _shutdown
    logger.info(f"🛑 {sig_name} — начинаем завершение...")
    _shutdown = True

    # Ждём дообработки очередей (до 30 сек)
    try:
        await asyncio.wait_for(
            asyncio.gather(tx_queue.join(), log_queue.join()),
            timeout=30,
        )
        logger.info("✅ Очереди опустошены")
    except asyncio.TimeoutError:
        logger.warning("⚠️  Очереди не опустели за 30 сек — принудительно")

    # Сохраняем БД ДО отмены задач
    await save_db()
    logger.info("✅ БД сохранена")

    # Отменяем бесконечные задачи → gather() разблокируется → выполняется finally
    for task in _main_tasks:
        if not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main() -> None:
    global http_session

    # Сигналы — регистрируем первыми (на Windows SIGTERM может быть недоступен)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(graceful_shutdown(s.name)),
            )
        except (NotImplementedError, OSError):
            if sig == signal.SIGINT:
                pass  # Ctrl+C на Windows обрабатывается иначе
            logger.debug(f"Signal {sig} не зарегистрирован (возможно Windows)")

    # Удаляем webhook
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

    # БД
    await init_db()
    logger.info("✅ PostgreSQL подключена")

    # Первичное обновление цены BNB
    await refresh_bnb_price()

    logger.info(
        f"🚀 VibeGuard v24.0 ЗАПУЩЕН | "
        f"limit=${db['cfg']['limit_usd']:,.0f} | "
        f"BNB=${_price_cache.get('BNB', 0):.2f} | "
        f"onchain={'ON' if ENABLE_ONCHAIN else 'OFF'}"
    )

    # Задачи
    polling_task = asyncio.create_task(
        bot.infinity_polling(allowed_updates=["message", "callback_query"])
    )
    monitor_task = asyncio.create_task(monitor())
    tx_workers   = [asyncio.create_task(tx_worker(i))  for i in range(6)]
    log_workers  = [asyncio.create_task(log_worker(i)) for i in range(4)]

    # Регистрируем для отмены при shutdown
    _main_tasks.extend([polling_task, monitor_task])

    try:
        await asyncio.gather(
            polling_task,
            monitor_task,
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


if __name__ == "__main__":
    asyncio.run(main())
