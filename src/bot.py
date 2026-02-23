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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import asyncpg
from aiohttp import web
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

BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "").strip().rstrip("/")

# Парсинг пула RPC ссылок (Архитектура "Гидра")
_RAW_HTTP_URL = _require("OPBNB_HTTP_URL")
HTTP_URLS = [u.strip() for u in _RAW_HTTP_URL.split(",") if u.strip()]
if not HTTP_URLS:
    raise EnvironmentError("Переменная OPBNB_HTTP_URL пуста или содержит невалидные данные")

# Опциональные
GEMINI_KEYS = [k for k in _optional("GEMINI_API_KEY").split(",") if k.strip()]
GROQ_KEYS   = [k for k in _optional("GROQ_API_KEY").split(",")   if k.strip()]
XAI_KEYS    = [k for k in _optional("XAI_API_KEY").split(",")    if k.strip()]

XAI_MODEL    = os.getenv("XAI_MODEL", "grok-beta").strip() or "grok-beta"
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"

GOPLUS_APP_KEY    = _optional("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _optional("GOPLUS_APP_SECRET")

# On-chain логирование (ТОЛЬКО для китов, не для подключённых кошельков)
ENABLE_ONCHAIN    = _optional("ENABLE_ONCHAIN_LOG").strip().lower() in {"true", "1", "yes", "y"}
ONCHAIN_PRIVKEY   = _optional("WEB3_PRIVATE_KEY")
ONCHAIN_CONTRACT  = _optional("VIBEGUARD_CONTRACT")

# URL веб-приложения (Telegram WebApp для Connect Wallet)
# Обязательно https:// — иначе в Telegram будет ERR_UNKNOWN_URL_SCHEME
_raw_webapp = _optional("WEBAPP_URL", "").strip()
if (_raw_webapp.startswith('"') and _raw_webapp.endswith('"')) or (
    _raw_webapp.startswith("'") and _raw_webapp.endswith("'")
):
    _raw_webapp = _raw_webapp[1:-1].strip()
_raw_webapp = _raw_webapp.rstrip("/")
if _raw_webapp and not _raw_webapp.startswith("https://"):
    logger.error(
        "⚠️ WEBAPP_URL должен начинаться с https://. "
        "Сейчас: %s — Telegram не откроет (ERR_UNKNOWN_URL_SCHEME). Исправь .env",
        _raw_webapp[:50],
    )
WEBAPP_URL = _raw_webapp if (_raw_webapp and _raw_webapp.startswith("https://")) else ""

LOGO_URL = _optional(
    "LOGO_URL",
    "https://raw.githubusercontent.com/Tarran6/VibeGuard-AI/main/assets/logo.png"
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


async def _run_health_server() -> None:
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw:
        return
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning("Invalid PORT value: %s", port_raw)
        return

    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }

    async def handle(_request: web.Request) -> web.Response:
        return web.Response(text="ok", headers=cors_headers)

    async def handle_webapp_connect_options(_request: web.Request) -> web.Response:
        return web.Response(status=204, headers=cors_headers)

    async def handle_webapp_connect(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            logger.info(f"🌐 WebApp connect request: {payload}")
        except Exception as e:
            logger.error(f"❌ WebApp JSON error: {e}")
            return web.json_response(
                {"ok": False, "error": "bad json"},
                status=400,
                headers=cors_headers,
            )

        nonce = str(payload.get("nonce", "")).strip()
        address = str(payload.get("address", "")).strip()
        signature = str(payload.get("signature", "")).strip()
        
        logger.info(f"🔍 WebApp data: nonce={nonce[:8]}..., address={address[:10]}..., signature={signature[:20]}...")

        if not nonce or not address or not signature:
            logger.warning(f"❌ Missing fields: nonce={bool(nonce)}, address={bool(address)}, signature={bool(signature)}")
            return web.json_response(
                {"ok": False, "error": "missing fields"},
                status=400,
                headers=cors_headers,
            )

        uid: Optional[int] = None
        async with db_lock:
            for uid_str, p in db.get("pending_verifications", {}).items():
                if str(p.get("nonce", "")) == nonce:
                    try:
                        uid = int(uid_str)
                        logger.info(f"✅ Found user_id: {uid} for nonce: {nonce[:8]}...")
                    except Exception:
                        uid = None
                    break

        if uid is None:
            logger.warning(f"❌ Session not found for nonce: {nonce[:8]}...")
            return web.json_response(
                {"ok": False, "error": "session not found"},
                status=404,
                headers=cors_headers,
            )

        logger.info(f"🔄 Calling verify_wallet for user {uid}")
        success, message = await verify_wallet(uid, address, signature)
        logger.info(f"📊 verify_wallet result: success={success}, message={message}")
        
        if success:
            await safe_send(
                uid,
                f"✅ <b>Кошелёк подключён!</b>\n"
                f"<code>{esc(address.lower())}</code>\n\n"
                f"Теперь ты получаешь личные алерты о всех транзакциях этого адреса.",
            )
            return web.json_response({"ok": True}, headers=cors_headers)

        return web.json_response(
            {"ok": False, "error": str(message)[:200]},
            status=400,
            headers=cors_headers,
        )

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


def calculate_vibe_score(tx_count: int, wallet_age_days: int, balance_usd: float) -> int:
    score = 0

    # 1. Активность (tx_count) — до 40
    if tx_count > 1000:
        score += 40
    elif tx_count > 100:
        score += 20
    elif tx_count > 10:
        score += 10

    # 2. Возраст (wallet_age_days) — до 30
    if wallet_age_days > 365:
        score += 30
    elif wallet_age_days > 90:
        score += 15
    elif wallet_age_days > 30:
        score += 5

    # 3. Баланс — до 30
    if balance_usd > 100_000:
        score += 30
    elif balance_usd > 10_000:
        score += 15
    elif balance_usd > 1_000:
        score += 5

    return min(score, 100)


def get_vibe_label(score: int) -> str:
    if score >= 80:
        return "🟢 TRUSTED WHALE (Безопасно)"
    if score >= 50:
        return "🟡 NEUTRAL (Средний риск)"
    if score >= 20:
        return "🟠 SUSPICIOUS (Подозрительно)"
    return "🔴 HIGH DANGER (Скам/Флэш-бот)"


async def get_tx_count(address: str) -> int:
    data = await rpc({
        "jsonrpc": "2.0",
        "method": "eth_getTransactionCount",
        "params": [address, "latest"],
        "id": 1,
    })
    res = data.get("result", "0x0")
    return int(res, 16) if res and res != "0x" else 0


async def get_bnb_balance(address: str) -> float:
    data = await rpc({
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    })
    res = data.get("result", "0x0")
    wei = int(res, 16) if res and res != "0x" else 0
    return wei / 10 ** 18


async def get_wallet_vibe(address: str) -> tuple[int, str, int, float]:
    """Возвращает (score, label, tx_count, balance_usd). wallet_age_days пока недоступен без explorer API."""
    tx_count = await get_tx_count(address)
    bal_bnb = await get_bnb_balance(address)
    bal_usd = await bnb_to_usd(bal_bnb)
    score = calculate_vibe_score(tx_count=tx_count, wallet_age_days=0, balance_usd=bal_usd)
    return score, get_vibe_label(score), tx_count, bal_usd


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
    if not ENABLE_ONCHAIN:
        return

    if not ONCHAIN_PRIVKEY:
        logger.warning("On-chain log skipped: WEB3_PRIVATE_KEY is not set")
        return

    if not ONCHAIN_CONTRACT:
        logger.warning("On-chain log skipped: VIBEGUARD_CONTRACT is not set")
        return

    if not Web3.is_address(target):
        logger.warning("On-chain log skipped: invalid target address: %s", str(target)[:16])
        return

    if not Web3.is_address(ONCHAIN_CONTRACT):
        logger.warning("On-chain log skipped: invalid VIBEGUARD_CONTRACT: %s", str(ONCHAIN_CONTRACT)[:16])
        return

    # Запускаем в отдельном потоке — синхронный Web3
    def _do_log(rpc_url: str):
        w3 = Web3(Web3.HTTPProvider(rpc_url))
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
        signed = w3.eth.account.sign_transaction(tx, acct.key)
        raw_tx = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw_tx is None:
            raise AttributeError(
                "SignedTransaction missing raw transaction bytes (expected rawTransaction/raw_transaction)"
            )
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        return tx_hash.hex(), acct.address

    loop = asyncio.get_running_loop()
    urls = list(HTTP_URLS)
    random.shuffle(urls)
    urls = urls[: min(len(urls), 3)]

    last_err: Optional[Exception] = None
    for attempt in range(5):
        for rpc_url in urls:
            try:
                tx_hash, from_addr = await loop.run_in_executor(None, _do_log, rpc_url)
                logger.info(
                    "On-chain log OK: %s... (from %s..., contract %s..., rpc %s...)",
                    tx_hash[:20],
                    from_addr[:10],
                    str(ONCHAIN_CONTRACT)[:10],
                    str(rpc_url)[:30],
                )
                return
            except Exception as e:
                last_err = e
                msg = str(e)
                if "429" in msg or "Too Many Requests" in msg or "rate" in msg.lower():
                    continue
                break

        delay = min(2 ** attempt, 20)
        await asyncio.sleep(delay)

    logger.warning(
        "On-chain log failed: %s | fromKeySet=%s contract=%s rpcs=%s",
        str(last_err)[:180] if last_err else "unknown",
        bool(ONCHAIN_PRIVKEY),
        str(ONCHAIN_CONTRACT)[:16],
        ",".join([u[:25] for u in urls]),
    )


def _build_webapp_url_with_nonce(base_url: str, nonce: str) -> str:
    p = urlparse(base_url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["nonce"] = nonce
    q["v"] = str(int(time.time()))
    if BOT_PUBLIC_URL:
        q["api"] = BOT_PUBLIC_URL + "/webapp/connect"
    return urlunparse(p._replace(query=urlencode(q)))


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
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": XAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "groq":
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
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
            body = ""
            try:
                body = (await r.text())[:500]
            except Exception:
                body = ""
            raise RuntimeError(f"HTTP {r.status} {body}".strip())
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

        sender_score, sender_label, sender_txc, sender_bal_usd = await get_wallet_vibe(sender)
        target_score, target_label, target_txc, target_bal_usd = await get_wallet_vibe(target)

        whale_text = (
            "🐳 <b>КИТ</b>\n\n"
            f"💰 <b>{val_bnb:.4f} BNB</b> (≈ ${val_usd:,.0f})\n"
            f"From: <code>{esc(sender)}</code>\n"
            f"To:   <code>{esc(target)}</code>\n\n"
            f"📈 <b>VibeScore отправителя:</b> <b>{sender_score}/100</b> — {esc(sender_label)}\n"
            f"   tx={sender_txc:,} | bal≈${sender_bal_usd:,.0f}\n"
            f"📈 <b>VibeScore получателя:</b> <b>{target_score}/100</b> — {esc(target_label)}\n"
            f"   tx={target_txc:,} | bal≈${target_bal_usd:,.0f}"
        )

        if sender in watch or target in watch:
            await notify_owners(f"🎯 <b>WATCHLIST HIT</b>\n\n{whale_text}")

        # Скам-проверка перед AI анализом
        risks = await check_scam(target)
        
        # Улучшенный AI анализ с детализацией
        async with ai_sem:
            # Формируем расширенный промпт для AI
            tx_details = {
                'value': str(int(val_bnb * 10**18)),
                'gas': tx.get('gas', '0x5208'),
                'gasPrice': tx.get('gasPrice', '0x0'),
                'to': target,
                'from': sender,
                'hash': tx.get('hash', ''),
                'blockNumber': tx.get('blockNumber', '')
            }
            
            # Используем улучшенный AI анализ если доступен
            try:
                from agent_bot import analyze_event_ai
                ai_analysis = await analyze_event_ai(
                    status=f"Перевод {val_bnb:.4f} BNB (≈ ${val_usd:,.0f}) от {sender[:10]}... к {target[:10]}...",
                    risk=1 if not risks else 5 if len(risks) > 2 else 3,
                    tx_data=tx_details,
                    user_address=str(watchers[0]) if watchers else None
                )
                verdict = ai_analysis
            except ImportError:
                # Fallback на старый метод если agent_bot не доступен
                verdict = await call_ai(
                    f"Анализ транзакции на русском языке:\n\n"
                    f"💰 Сумма: {val_bnb:.4f} BNB (≈ ${val_usd:,.0f})\n"
                    f"📤 Отправитель: {sender[:10]}...{sender[-6:]}\n"
                    f"📥 Получатель: {target[:10]}...{target[-6:]}\n"
                    f"⚠️ Риски: {', '.join(risks) if risks else 'Не обнаружены'}\n"
                    f"📊 VibeScore отправителя: {sender_score}/100 ({sender_label})\n"
                    f"📊 VibeScore получателя: {target_score}/100 ({target_label})\n\n"
                    f"Проанализируй эту транзакцию, определи возможные риски и дай рекомендации. "
                    f"Будь конкретным и детальным. Без HTML-тегов."
                )
        await notify_owners(f"{whale_text}\n\n🧠 <b>AI АНАЛИЗ:</b>\n{verdict}")

        # Обнаруженные риски + on-chain логирование (только для китов!)
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

        # Улучшенный AI анализ для токенов
        async with ai_sem:
            # Формируем детальные данные о транзакции токена
            token_tx_details = {
                'value': str(raw_amount),
                'decimals': str(decimals),
                'to': receiver,
                'from': sender,
                'token_address': token_addr,
                'amount': str(amount),
                'method': 'transfer'
            }
            
            # Используем улучшенный AI анализ если доступен
            try:
                from agent_bot import analyze_event_ai
                ai_analysis = await analyze_event_ai(
                    status=f"Перевод {amount:,.2f} токенов (≈ ${val_usd:,.0f}) контракта {token_addr[:10]}...",
                    risk=1 if not risks else 5 if len(risks) > 2 else 3,
                    tx_data=token_tx_details,
                    user_address=str(watchers[0]) if watchers else None
                )
                verdict = ai_analysis
            except ImportError:
                # Fallback на старый метод
                verdict = await call_ai(
                    f"Анализ транзакции токена на русском языке:\n\n"
                    f"💰 Сумма: {amount:,.2f} токенов (≈ ${val_usd:,.0f})\n"
                    f"🪙 Контракт: {token_addr[:10]}...{token_addr[-6:]}\n"
                    f"📤 Отправитель: {sender[:10]}...{sender[-6:]}\n"
                    f"📥 Получатель: {receiver[:10]}...{receiver[-6:]}\n"
                    f"⚠️ Риски токена: {', '.join(risks) if risks else 'Не обнаружены'}\n\n"
                    f"Проанализируй эту транзакцию токена, определи возможные риски и дай рекомендации. "
                    f"Будь конкретным и детальным. Без HTML-тегов."
                )
        await notify_owners(f"{whale_text}\n\n🧠 <b>AI АНАЛИЗ:</b>\n{verdict}")

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
    logger.info(f"🔍 verify_wallet called: user_id={user_id}, address={address[:10]}...{address[-6:]}")

    if not Web3.is_address(address):
        logger.warning(f"❌ Invalid address: {address}")
        return False, "Невалидный адрес кошелька"

    async with db_lock:
        pending = db["pending_verifications"].get(uid_str)

    if not pending:
        logger.warning(f"❌ No pending verification for user {user_id}")
        return False, "Сессия верификации не найдена. Нажми Connect Wallet заново."

    if time.time() - pending["ts"] > STATE_TTL:
        logger.warning(f"❌ Verification session expired for user {user_id}")
        async with db_lock:
            db["pending_verifications"].pop(uid_str, None)
        return False, "Сессия истекла. Нажми Connect Wallet заново."

    nonce   = pending["nonce"]
    message = f"VibeGuard verification: {nonce}"
    logger.info(f"📝 Verifying message: {message}")

    # Восстанавливаем адрес из подписи
    try:
        w3_local    = Web3()
        msg_defunct = encode_defunct(text=message)
        recovered   = w3_local.eth.account.recover_message(
            msg_defunct, signature=signature
        )
        logger.info(f"🔐 Signature recovered: {recovered[:10]}...{recovered[-6:]}")
    except Exception as e:
        logger.error(f"❌ Signature recovery error: {e}")
        return False, f"Невалидная подпись: {str(e)[:80]}"

    if recovered.lower() != address.lower():
        logger.warning(f"❌ Address mismatch: expected={address[:10]}..., got={recovered[:10]}...")
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
        logger.info(f"💼 User {user_id} has {len(wallets)} wallets, existing: {len(existing)}")

        if addr_lower in existing:
            logger.warning(f"❌ Wallet already connected: {addr_lower[:10]}...")
            return False, "Этот кошелёк уже подключён"

        if len(wallets) >= 5:
            logger.warning(f"❌ Too many wallets for user {user_id}: {len(wallets)}")
            return False, "Максимум 5 кошельков на аккаунт"

        label = f"Wallet {len(wallets) + 1}"
        wallets.append({"address": addr_lower, "label": label})
        db["pending_verifications"].pop(uid_str, None)
        logger.info(f"✅ Wallet saved: {addr_lower[:10]}... as {label} for user {user_id}")

    await save_db()
    logger.info(f"💾 Database saved for user {user_id}")
    return True, f"✅ Кошелёк подключён: {addr_lower[:8]}...{addr_lower[-6:]}"


# ---------------------------------------------------------------------------
# TELEGRAM — КЛАВИАТУРЫ
# ---------------------------------------------------------------------------

def kb_main() -> types.ReplyKeyboardMarkup:
    """Профессиональная главная клавиатура"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    # Основные функции
    kb.add("� Мои кошельки", "🔗 Подключить кошелёк")
    kb.add("� Статистика", "🔍 Проверить контракт")
    kb.add("🧠 AI Ассистент", "⚙️ Настройки")
    kb.add("🛡️ Поддержка")
    
    return kb


def kb_connect_wallet() -> types.InlineKeyboardMarkup:
    """Кнопка открывает Telegram WebApp."""
    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton(
            "🔗 Подключить кошелёк",
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
    
    # Профессиональное приветственное сообщение
    welcome_text = (
        "🛡️ <b>VibeGuard AI Sentinel</b> v24.0\n\n"
        "🚀 <b>Интеллектуальная защита крипто-активов</b>\n\n"
        "✨ <b>Основные возможности:</b>\n"
        "🔔 Персональные алерты о транзакциях\n"
        "🐳 Мониторинг крупных перемещений\n"
        "🤖 AI-анализ рисков и угроз\n"
        "🛡️ Проверка скам-контрактов\n\n"
        "<b>Начните работу:</b>\n"
        "👛 Нажмите «Мои кошельки» для управления\n"
        "🔗 Подключите кошелёк для алертов\n"
        "📊 Изучайте статистику китов"
    )
    
    await bot.send_photo(
        m.chat.id, LOGO_URL,
        caption=welcome_text,
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
    webapp_url_with_nonce = _build_webapp_url_with_nonce(WEBAPP_URL, nonce) if WEBAPP_URL else ""

    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton(
            "🔗 Подключить кошелёк",
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
    
    # Добавляем кнопки управления
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


@bot.callback_query_handler(func=lambda c: c.data.startswith("dc:") or c.data == "connect_new")
async def cb_wallet_action(c: types.CallbackQuery) -> None:
    if c.data == "connect_new":
        # Обработка кнопки "Подключить кошелёк"
        await cmd_connect(types.Message(
            message_id=c.message.message_id,
            from_user=c.from_user,
            date=int(time.time()),
            chat=c.message.chat,
            content_type="text",
            options={},
            json_string="",
            text="/connect"
        ))
        await bot.answer_callback_query(c.id)
        return
    
    # Обработка отключения кошелька
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
    
    # Показываем обновленный список кошельков
    await cmd_mywallets(types.Message(
        message_id=c.message.message_id,
        from_user=c.from_user,
        date=int(time.time()),
        chat=c.message.chat,
        content_type="text",
        options={},
        json_string="",
        text="/mywallets"
    ))


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
        f"📈 <b>Оценка:</b> <b>{score}/100</b>\n\n"
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
    "📊 Статистика", "🧠 AI Ассистент", "👛 Мои кошельки", "🔍 Проверить контракт", "🔗 Подключить кошелёк", "🛡️ Поддержка"
})
async def handle_menu(m: types.Message) -> None:
    t = m.text
    if t == "📊 Статистика":
        await cmd_status(m)
    elif t == "🧠 AI Ассистент":
        set_state(m.from_user.id, "ask_ai")
        await bot.reply_to(
            m,
            "🤖 Задай любой вопрос о крипте или контрактах.\n/cancel — выйти.",
        )
    elif t == "👛 Мои кошельки":
        await cmd_mywallets(m)
    elif t == "🔍 Проверить контракт":
        set_state(m.from_user.id, "check_contract")
        await bot.reply_to(m, "Отправь адрес контракта для проверки:")
    elif t == "🔗 Подключить кошелёк":
        await cmd_connect(m)
    elif t == "⚙️ Настройки":
        await bot.reply_to(m, "⚙️ Настройки в разработке...")
    elif t == "🛡️ Поддержка":
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
    global http_session, _shutdown

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

    # Проверка доступа к Telegram API (диагностика)
    try:
        me = await bot.get_me()
        logger.info("✅ Telegram API OK: @%s (%s)", getattr(me, "username", "?"), getattr(me, "id", "?"))
    except Exception as e:
        logger.error("❌ Telegram API check failed: %s", str(e)[:200], exc_info=True)

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

    async def _polling_forever() -> None:
        logger.info("🛰️ Polling task initialized (shutdown=%s)", _shutdown)
        while not _shutdown:
            try:
                logger.info("📡 Polling started")
                await bot.infinity_polling(
                    timeout=30,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e)
                if "409" in msg and "Conflict" in msg:
                    logger.warning(
                        "Telegram 409 conflict (another instance polling). "
                        "Backing off and retrying... (%s)",
                        msg[:200],
                    )
                    await asyncio.sleep(12)
                    continue

                logger.error("Polling crashed: %s", msg[:200], exc_info=True)
                await asyncio.sleep(3)

    # Задачи
    logger.info("🛰️ Creating polling task...")
    polling_task = asyncio.create_task(_polling_forever())

    def _log_task_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "❌ Polling task finished with error: %s",
                    str(exc)[:200],
                    exc_info=exc,
                )
            else:
                logger.warning("⚠️ Polling task finished without error (shutdown=%s)", _shutdown)
        except asyncio.CancelledError:
            logger.info("ℹ️ Polling task cancelled")
        except Exception as e:
            logger.error("❌ Polling task done-callback error: %s", str(e)[:200], exc_info=True)

    polling_task.add_done_callback(_log_task_done)

    health_task  = asyncio.create_task(_run_health_server())
    monitor_task = asyncio.create_task(monitor())
    tx_workers   = [asyncio.create_task(tx_worker(i))  for i in range(6)]
    log_workers  = [asyncio.create_task(log_worker(i)) for i in range(4)]

    # Регистрируем для отмены при shutdown
    _main_tasks.extend([polling_task, health_task, monitor_task])

    try:
        await asyncio.gather(
            polling_task,
            health_task,
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
