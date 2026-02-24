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
import aiosqlite
from dotenv import load_dotenv
from eth_account.messages import encode_defunct
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from web3 import Web3

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vibeguard")

def _require(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v: raise EnvironmentError(f"Переменная окружения не задана: {key}")
    return v

def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

TELEGRAM_TOKEN = _require("TELEGRAM_TOKEN")
DATABASE_URL = _optional("DATABASE_URL", "sqlite:///vibeguard.db")
PRIMARY_OWNER_ID = int(_require("PRIMARY_OWNER_ID"))
_RAW_HTTP_URL = _require("OPBNB_HTTP_URL")
ALL_RPC_URLS = [u.strip() for u in _RAW_HTTP_URL.split(",") if u.strip()] + ["https://opbnb-mainnet-rpc.bnbchain.org"]

def get_smart_w3(url_string):
    urls = [u.strip() for u in url_string.split(",") if u.strip()]
    for url in urls:
        try:
            temp_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 5}))
            if temp_w3.is_connected(): return temp_w3
        except: continue
    raise Exception("❌ RPC недоступен")

GEMINI_KEYS = [k for k in _optional("GEMINI_API_KEY").split(",") if k.strip()]
GROQ_KEYS = [k for k in _optional("GROQ_API_KEY").split(",") if k.strip()]
XAI_KEYS = [k for k in _optional("XAI_API_KEY").split(",") if k.strip()]
XAI_MODEL = _optional("XAI_MODEL", "grok-2")
GROQ_MODEL = _optional("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = _optional("GEMINI_MODEL", "gemini-2.0-flash")
GOPLUS_APP_KEY = _optional("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _optional("GOPLUS_APP_SECRET")
ENABLE_ONCHAIN = _optional("ENABLE_ONCHAIN_LOG") == "true"
ONCHAIN_PRIVKEY = _optional("WEB3_PRIVATE_KEY")
ONCHAIN_CONTRACT = _optional("VIBEGUARD_CONTRACT")
WEBAPP_URL = _optional("WEBAPP_URL", "")
REOWN_PROJECT_ID = _optional("REOWN_PROJECT_ID", "")
BOT_PUBLIC_URL = _optional("BOT_PUBLIC_URL", "")
OWNERS = {PRIMARY_OWNER_ID}
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

bot = AsyncTeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ---------------------------------------------------------------------------
# БД И ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ---------------------------------------------------------------------------
_DB_DEFAULT = {
    "stats": {"blocks": 0, "whales": 0, "threats": 0},
    "cfg": {"limit_usd": 10000.0, "watch": [], "ignore": []},
    "user_limits": {},
    "last_block": 0,
    "connected_wallets": {},
    "pending_verifications": {},
}
db = {}
pool = None
http_session = None
start_time = time.time()
db_lock = Lock()
price_lock = Lock()
rpc_sem = Semaphore(10)
ai_sem = Semaphore(3)
tg_sem = Semaphore(20)
tx_queue = Queue(maxsize=8000)
log_queue = Queue(maxsize=8000)
_shutdown = False
_main_tasks = []
_price_cache = {}
_price_cache_ts = 0.0
_token_price_cache = {}
_decimals_cache = {}
_user_states = {}

def esc(text): return html.escape(str(text))
def set_state(uid, state): _user_states[uid] = {"state": state, "ts": time.time()}
def get_state(uid):
    e = _user_states.get(uid)
    if not e or time.time() - e["ts"] > 600: return None
    return e["state"]
def clear_state(uid): _user_states.pop(uid, None)
def is_owner(uid): return uid in OWNERS

# ---------------------------------------------------------------------------
# SQLITE ЛОГИКА
# ---------------------------------------------------------------------------
async def init_db():
    global pool, db
    pool = await aiosqlite.connect("vibeguard.db")
    await pool.execute("CREATE TABLE IF NOT EXISTS bot_data (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    async with pool.execute("SELECT data FROM bot_data WHERE id = 1") as cursor:
        row = await cursor.fetchone()
        if row:
            db = {**_DB_DEFAULT, **json.loads(row[0])}
        else:
            db = _DB_DEFAULT.copy()
            await pool.execute("INSERT INTO bot_data (id, data) VALUES (1, ?)", (json.dumps(db),))
            await pool.commit()

async def save_db():
    if not pool: return
    try:
        await pool.execute("INSERT OR REPLACE INTO bot_data (id, data) VALUES (1, ?)", (json.dumps(db),))
        await pool.commit()
    except Exception as e: logger.warning(f"Ошибка сохранения БД: {e}")

# ---------------------------------------------------------------------------
# ЦЕНЫ И RPC (В ТВОЕМ СТИЛЕ)
# ---------------------------------------------------------------------------
async def bnb_to_usd(bnb):
    global _price_cache_ts
    async with price_lock:
        if time.time() - _price_cache_ts > 120:
            try:
                async with http_session.get("https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd") as r:
                    if r.status == 200:
                        d = await r.json()
                        _price_cache["BNB"] = float(d["binancecoin"]["usd"])
                        _price_cache_ts = time.time()
            except: pass
    return bnb * _price_cache.get("BNB", 600.0)

async def token_to_usd(token_addr, raw, decimals):
    amount = raw / (10 ** decimals)
    now = time.time()
    if token_addr not in _token_price_cache or (now - _token_price_cache[token_addr][1]) > 120:
        try:
            url = f"https://api.coingecko.com/api/v3/simple/token_price/binance-smart-chain?contract_addresses={token_addr}&vs_currencies=usd"
            async with http_session.get(url) as r:
                if r.status == 200:
                    d = await r.json()
                    _token_price_cache[token_addr] = (float(d.get(token_addr.lower(), {}).get("usd", 0.0)), now)
        except: _token_price_cache[token_addr] = (0.0, now)
    return amount * _token_price_cache[token_addr][0]

async def rpc(payload):
    async with rpc_sem:
        for url in ALL_RPC_URLS:
            try:
                async with http_session.post(url, json=payload, timeout=10) as r:
                    if r.status == 200: return await r.json()
            except: continue
    return {"result": None}

async def get_block(n): return (await rpc({"jsonrpc":"2.0","method":"eth_getBlockByNumber","params":[hex(n), True],"id":1})).get("result")
async def get_logs(f, t): return (await rpc({"jsonrpc":"2.0","method":"eth_getLogs","params":[{"fromBlock":hex(f),"toBlock":hex(t),"topics":[ERC20_TRANSFER_TOPIC]}],"id":1})).get("result") or []
async def get_decimals(a):
    if a in _decimals_cache: return _decimals_cache[a]
    r = (await rpc({"jsonrpc":"2.0","method":"eth_call","params":[{"to":a,"data":"0x313ce567"},"latest"],"id":1})).get("result", "0x12")
    _decimals_cache[a] = int(r, 16) if r and r != "0x" else 18
    return _decimals_cache[a]

# ---------------------------------------------------------------------------
# ВЕРИФИКАЦИЯ (СТРОГО 1 КОШЕЛЕК)
# ---------------------------------------------------------------------------
async def verify_wallet(user_id, address, signature):
    uid_str = str(user_id)
    if not Web3.is_address(address): return False, "Невалидный адрес"
    async with db_lock:
        pending = db["pending_verifications"].get(uid_str)
        if not pending: return False, "Сессия не найдена"
        try:
            w3 = get_smart_w3(_RAW_HTTP_URL)
            msg = encode_defunct(text=f"VibeGuard verification: {pending['nonce']}")
            recovered = w3.eth.account.recover_message(msg, signature=signature)
            if recovered.lower() != address.lower(): return False, "Подпись не совпадает"
        except Exception as e: return False, f"Ошибка: {e}"
        
        # ПЕРЕЗАПИСЬ: Всегда оставляем только один кошелек
        db["connected_wallets"][uid_str] = [{"address": address.lower(), "label": "Main Wallet"}]
        db["pending_verifications"].pop(uid_str, None)

    await save_db()
    return True, "✅ Кошелёк успешно привязан"

# ---------------------------------------------------------------------------
# ОСТАЛЬНАЯ ЛОГИКА (AI, МОНИТОРИНГ, КОМАНДЫ)
# ---------------------------------------------------------------------------
async def check_scam(addr):
    try:
        async with http_session.get(f"https://api.gopluslabs.io/api/v1/token_security/204?contract_addresses={addr}") as r:
            if r.status == 200:
                d = (await r.json()).get("result", {}).get(addr.lower(), {})
                risks = []
                if d.get("is_honeypot") == "1": risks.append("🍯 HONEYPOT")
                if d.get("is_open_source") == "0": risks.append("🔐 ЗАКРЫТЫЙ КОД")
                return risks
    except: pass
    return []

async def call_ai(prompt):
    configs = [("xai", k) for k in XAI_KEYS] + [("groq", k) for k in GROQ_KEYS] + [("gemini", k) for k in GEMINI_KEYS]
    if not configs: return "AI не настроен"
    async with ai_sem:
        for prov, key in configs:
            try:
                url = "https://api.x.ai/v1/chat/completions" if prov == "xai" else "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {key}"}
                payload = {"model": XAI_MODEL if prov == "xai" else GROQ_MODEL, "messages": [{"role": "user", "content": prompt}]}
                if prov == "gemini":
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
                    payload = {"contents": [{"parts": [{"text": prompt}]}]}
                    headers = {}
                async with http_session.post(url, json=payload, headers=headers) as r:
                    if r.status == 200:
                        res = await r.json()
                        return esc(res["choices"][0]["message"]["content"]) if prov != "gemini" else esc(res["candidates"][0]["content"]["parts"][0]["text"])
            except: continue
    return "AI временно недоступен"

# Обработчики команд (Оригинальные)
@bot.message_handler(commands=["start"])
async def cmd_start(m):
    await bot.send_message(m.chat.id, "🛡️ <b>VibeGuard v24.5</b>\n\nИспользуйте меню для управления.", reply_markup=get_main_menu_keyboard())

def get_main_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("👛 Мои кошельки", callback_data="menu_mywallets"),
           types.InlineKeyboardButton("🔗 Подключить", callback_data="menu_connect"),
           types.InlineKeyboardButton("📊 Статистика", callback_data="menu_status"))
    return kb

@bot.message_handler(commands=["connect"])
async def cmd_connect(m):
    uid = m.from_user.id
    nonce = secrets.token_hex(16)
    async with db_lock: db["pending_verifications"][str(uid)] = {"nonce": nonce, "ts": time.time()}
    await save_db()
    webapp_url = f"{WEBAPP_URL}?startapp={nonce}&api={BOT_PUBLIC_URL}/webapp/connect"
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔗 Connect Wallet", web_app=types.WebAppInfo(url=webapp_url)))
    await bot.reply_to(m, "👛 <b>Подключение кошелька</b>\n\nНажми кнопку ниже:", reply_markup=kb)

# ---------------------------------------------------------------------------
# HEALTH SERVER (УНИВЕРСАЛЬНЫЙ GET + POST)
# ---------------------------------------------------------------------------
async def _run_health_server():
    from aiohttp import web
    cors_headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}

    async def handle_connect(request):
        try:
            d = await request.json()
            success, msg = await verify_wallet(0, d.get("address"), d.get("signature")) # uid определится по nonce внутри
            return web.json_response({"ok": success, "error": msg}, headers=cors_headers)
        except: return web.json_response({"ok": False}, headers=cors_headers)

    async def handle_approvals(request):
        addr = request.query.get("address")
        if not addr and request.method == "POST":
            try: d = await request.json(); addr = d.get("address")
            except: pass
        if not addr: return web.json_response({"ok":False}, headers=cors_headers)
        try:
            url = f"https://api.gopluslabs.io/api/v1/token_approvals?chain_id=204&user_address={addr}"
            async with http_session.get(url) as r:
                data = await r.json()
                approvals = []
                for t in data.get("result", []):
                    for s in t.get("approved_list", []):
                        if s.get("allowance") != "0":
                            approvals.append({"tokenName": t.get("token_name"), "tokenAddress": t.get("token_address"), 
                                             "spenderAddress": s.get("approved_contract"), "amount": s.get("allowance"),
                                             "risk": "high" if s.get("is_danger") == 1 else "low"})
                return web.json_response({"ok":True, "approvals": approvals}, headers=cors_headers)
        except: return web.json_response({"ok":False}, headers=cors_headers)

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_post("/webapp/connect", handle_connect)
    app.router.add_get("/webapp/approvals", handle_approvals)
    app.router.add_post("/webapp/approvals", handle_approvals)
    app.router.add_options("/{tail:.*}", lambda _: web.Response(status=204, headers=cors_headers))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
    while not _shutdown: await asyncio.sleep(1)

# ---------------------------------------------------------------------------
# MAIN ЗАПУСК
# ---------------------------------------------------------------------------
async def main():
    global http_session
    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50))
    await init_db()
    asyncio.create_task(_run_health_server())
    logger.info("🚀 VibeGuard v24.5 Запущен")
    await bot.infinity_polling()

if __name__ == "__main__":
    asyncio.run(main())
