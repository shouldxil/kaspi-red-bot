import asyncio
import logging
import random
import os
import json
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiohttp import web
import psycopg2
from psycopg2 import pool

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 7934547554
MIN_BET = 10
BOT_USERNAME = "ТВОЙ_ЮЗЕРНЕЙМ_БОТА" # укажи юзернейм бота без @

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

CACHED_ANIMATION = None

def get_cached_animation():
    global CACHED_ANIMATION
    if CACHED_ANIMATION is None and os.path.exists("red-1.mp4"):
        CACHED_ANIMATION = FSInputFile("red-1.mp4")
    return CACHED_ANIMATION

DB_POOL = None

def init_db_pool():
    global DB_POOL
    DB_POOL = pool.ThreadedConnectionPool(
        minconn=1, maxconn=20,
        dsn=DATABASE_URL
    )
    logging.info("Connection Pool PostgreSQL создан.")

@contextmanager
def get_db():
    conn = DB_POOL.getconn()
    try: yield conn
    finally: DB_POOL.putconn(conn)

def init_db():
    init_db_pool()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT, last_name TEXT, username TEXT,
                    balance BIGINT DEFAULT 1000, last_bonus TEXT
                )""")
            for col, col_def in [
                ("games_played", "INT DEFAULT 0"),
                ("games_won", "INT DEFAULT 0"),
                ("referrer_id", "BIGINT"),
                ("referred_count", "INT DEFAULT 0"),
                ("last_daily", "TEXT")
            ]:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    rank TEXT DEFAULT 'moder'
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS disabled_games (
                    game_name TEXT PRIMARY KEY
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS roulette_log (
                    id SERIAL PRIMARY KEY, roll INT, color TEXT
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_last_bets (
                    user_id BIGINT PRIMARY KEY, bets_json TEXT
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promocodes (
                    code TEXT PRIMARY KEY, amount BIGINT NOT NULL,
                    uses INT NOT NULL DEFAULT 1
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS used_promocodes (
                    user_id BIGINT, code TEXT,
                    PRIMARY KEY (user_id, code)
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS secret_powers (
                    user_id BIGINT, command_name TEXT,
                    PRIMARY KEY (user_id, command_name)
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_log (
                    id SERIAL PRIMARY KEY,
                    admin_id BIGINT, action TEXT,
                    target_id BIGINT, amount BIGINT,
                    timestamp TEXT
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS balance_checkpoints (
                    user_id BIGINT, balance BIGINT,
                    checkpoint_time TEXT
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_tasks (
                    user_id BIGINT, date TEXT,
                    task_type TEXT, progress INT DEFAULT 0,
                    completed BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (user_id, date, task_type)
                )""")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    user_id BIGINT PRIMARY KEY,
                    referrer_id BIGINT,
                    bonus_claimed BOOLEAN DEFAULT FALSE
                )""")
            conn.commit()

            cursor.execute("SELECT user_id FROM admins WHERE user_id = %s", (ADMIN_ID,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s, 'owner')", (ADMIN_ID,))

            cursor.execute("INSERT INTO settings (key, value) VALUES ('bonus_amount', '3000') ON CONFLICT (key) DO NOTHING")
            cursor.execute("INSERT INTO settings (key, value) VALUES ('bonus_cooldown', '8') ON CONFLICT (key) DO NOTHING")
            conn.commit()
    logging.info("БД инициализирована.")

# ---------- Хелперы ----------
def get_user(user_id: int, first_name: str = "", last_name: str = "", username: str = "") -> dict:
    safe_name = first_name if first_name else "Игрок"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO users (user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won) VALUES (%s,%s,%s,%s,%s,%s,0,0)",
                    (user_id, safe_name, last_name or "", username or "", 4000, None))
                conn.commit()
                return {"user_id": user_id, "first_name": safe_name, "last_name": last_name or "", "username": username or "", "balance": 4000, "last_bonus": None, "games_played": 0, "games_won": 0, "referrer_id": None, "referred_count": 0, "last_daily": None}
            return {"user_id": row[0], "first_name": row[1], "last_name": row[2], "username": row[3], "balance": row[4], "last_bonus": row[5], "games_played": row[6], "games_won": row[7], "referrer_id": row[8], "referred_count": row[9], "last_daily": row[10]}

def update_balance(user_id: int, amount: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

def find_user_by_identifier(identifier: str):
    clean_id = identifier.replace("@", "").strip()
    with get_db() as conn:
        with conn.cursor() as cursor:
            if clean_id.isdigit():
                cursor.execute("SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE user_id = %s", (int(clean_id),))
            else:
                cursor.execute("SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE LOWER(username) = LOWER(%s)", (clean_id,))
            row = cursor.fetchone()
            if row:
                return {"user_id": row[0], "first_name": row[1], "last_name": row[2], "username": row[3], "balance": row[4], "last_bonus": row[5], "games_played": row[6], "games_won": row[7], "referrer_id": row[8], "referred_count": row[9], "last_daily": row[10]}
    return None

def get_rank(user_id: int) -> str:
    if user_id == ADMIN_ID: return "owner"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT rank FROM admins WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else "user"

def get_rank_emoji(rank: str) -> str:
    return {"owner": "💎 Владелец", "head": "👑 Главный администратор", "admin": "🔰 Администратор", "moder": "⭐ Модератор"}.get(rank, "")

def is_moder_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin", "moder")

def is_admin_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin")

def is_head_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head")

def is_game_disabled(game_name: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM disabled_games WHERE game_name = %s", (game_name,))
            return cursor.fetchone() is not None

def get_setting(key: str, default: str) -> str:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

def set_setting(key: str, value: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
            conn.commit()

def add_roulette_log(roll: int, color: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO roulette_log (roll, color) VALUES (%s,%s)", (roll, color))
            cursor.execute("DELETE FROM roulette_log WHERE id NOT IN (SELECT id FROM roulette_log ORDER BY id DESC LIMIT 10)")
            conn.commit()

def get_roulette_history():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT roll, color FROM roulette_log ORDER BY id DESC LIMIT 10")
            return [{"roll": r[0], "color": r[1]} for r in reversed(cursor.fetchall())]

def save_last_bets(user_id: int, bets: list):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_last_bets (user_id, bets_json) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET bets_json = EXCLUDED.bets_json",
                (user_id, json.dumps(bets)))
            conn.commit()

def get_last_bets(user_id: int) -> list:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT bets_json FROM user_last_bets WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and row[0]:
                try: return json.loads(row[0])
                except: return []
    return []

def get_promo(code: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT code, amount, uses FROM promocodes WHERE code = %s", (code,))
            row = cursor.fetchone()
            if row: return {"code": row[0], "amount": row[1], "uses": row[2]}
    return None

def use_promo(user_id: int, code: str, amount: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM used_promocodes WHERE user_id=%s AND code=%s", (user_id, code))
            if cursor.fetchone(): return False
            cursor.execute("UPDATE promocodes SET uses = uses - 1 WHERE code = %s", (code,))
            cursor.execute("INSERT INTO used_promocodes (user_id, code) VALUES (%s,%s)", (user_id, code))
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()
            return True

def delete_promo(code: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM promocodes WHERE code = %s", (code,))
            conn.commit()

def log_admin_action(admin_id: int, action: str, target_id: int = 0, amount: int = 0):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO admin_log (admin_id, action, target_id, amount, timestamp) VALUES (%s,%s,%s,%s,%s)",
                (admin_id, action, target_id, amount, datetime.now().isoformat()))
            conn.commit()

def get_mention(user_id: int, first_name: str) -> str:
    safe_name = first_name if first_name else "Игрок"
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

def in_group(message: Message) -> bool:
    return message.chat.type in ["group", "supergroup"]

def check_group_only(message: Message, game_name: str) -> bool:
    if message.from_user:
        get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if not in_group(message):
        asyncio.create_task(message.answer("🎮 Игры доступны только в групповых чатах."))
        return False
    if is_game_disabled(game_name):
        asyncio.create_task(message.answer(f"❌ Игра {game_name} отключена администратором."))
        return False
    return True

def format_balance(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₸"

# ---------- Клавиатуры и бонус ----------
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎮 Мини-игры")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="💬 Чаты")],
            [KeyboardButton(text="📋 Команды"), KeyboardButton(text="🛒 Донат")],
            [KeyboardButton(text="📢 Новости"), KeyboardButton(text="💬 Чат")]
        ], resize_keyboard=True)

def get_balance_keyboard(user_data: dict, is_in_group: bool):
    b_cool = int(get_setting("bonus_cooldown", "8"))
    bonus_available = True
    if user_data.get("last_bonus"):
        try:
            last_time = datetime.fromisoformat(user_data["last_bonus"])
            if datetime.now() < last_time + timedelta(hours=b_cool):
                bonus_available = False
        except: pass
    if bonus_available:
        if is_in_group:
            url = f"https://t.me/{BOT_USERNAME}?start=bonus"
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Бонус 💰", url=url)]])
        else:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Бонус 💰", callback_data="get_bonus_lc")]])
    return None

async def process_bonus_logic(user_id: int, first_name: str, is_group_context: bool):
    if is_group_context:
        return "❌ Ежедневный бонус можно получить только в личных сообщениях с ботом!"
    user = get_user(user_id, first_name)
    b_amt = int(get_setting("bonus_amount", "3000"))
    b_cool = int(get_setting("bonus_cooldown", "8"))
    if user.get("last_bonus"):
        try:
            last_time = datetime.fromisoformat(user["last_bonus"])
            cooldown_period = timedelta(hours=b_cool)
            if datetime.now() < last_time + cooldown_period:
                diff = (last_time + cooldown_period) - datetime.now()
                total_seconds = int(diff.total_seconds())
                hours, rem = divmod(total_seconds, 3600)
                mins, secs = divmod(rem, 60)
                time_str = f"{hours}:{mins:02d}:{secs:02d}"
                return f"⏳ Бонус уже получен.\n\nСледующий бонус через\n\n{time_str}"
        except: pass
    update_balance(user_id, b_amt)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET last_bonus = %s WHERE user_id = %s", (datetime.now().isoformat(), user_id))
            conn.commit()
    mention = get_mention(user_id, user['first_name'])
    updated_user = get_user(user_id)
    return f"🎁 {mention} получил бонус <b>{format_balance(b_amt)}</b> 💰!\n\n👤 {mention}\n💰 Баланс: {format_balance(updated_user['balance'])}"

# ---------- Основные команды ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    fname = message.from_user.first_name or ""
    get_user(user_id, fname, message.from_user.last_name or "", message.from_user.username or "")
    args = message.text.split()
    if len(args) > 1 and args[1] == "bonus":
        if in_group(message):
            await message.answer("❌ Получить бонус можно только в личных сообщениях.")
            return
        result_text = await process_bonus_logic(user_id, fname, False)
        await message.answer(result_text)
        return
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1].replace("ref", ""))
            if ref_id != user_id:
                with get_db() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM referrals WHERE user_id = %s", (user_id,))
                        if not cursor.fetchone():
                            cursor.execute("INSERT INTO referrals (user_id, referrer_id) VALUES (%s, %s)", (user_id, ref_id))
                            cursor.execute("UPDATE users SET referred_count = referred_count + 1 WHERE user_id = %s", (ref_id,))
                            conn.commit()
        except: pass
    if in_group(message):
        await message.answer("Привет! Я бот CreditMania. Напиши мне в личные сообщения для главного меню.")
        return
    welcome_text = (
        "👋 Добро пожаловать в CreditMania!\n\n"
        "🎰 Игры: Рулетка, Джокер, Мины, Дуэли\n"
        "💎 Валюта: ₸ (тенге)\n"
        "💰 Начальный баланс: 4 000 ₸\n\n"
        "Используй кнопки ниже для навигации."
    )
    await message.answer(welcome_text, reply_markup=get_main_menu())

@router.message(F.text == "📢 Новости")
async def cmd_news_btn(message: Message):
    await message.answer("Подпишись на канал @creditmania_news, чтобы первым узнавать об обновлениях и акциях!")

@router.message(F.text == "💬 Чат")
async def cmd_chat_btn(message: Message):
    await message.answer("Присоединяйся в общий чат @creditmania_chat, общайся с игроками!")

@router.callback_query(F.data == "get_bonus_lc")
async def callback_get_bonus(callback: CallbackQuery):
    res = await process_bonus_logic(callback.from_user.id, callback.from_user.first_name or "", in_group(callback.message))
    await callback.message.answer(res)
    await callback.answer()

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📋 <b>Справка по CreditMania</b>\n\n"
        "• [ставка] [объекты...] — Рулетка\n"
        "• отмена — Отменить ставку рулетки\n"
        "• лог — История рулетки\n"
        "• ставки — Текущие ставки\n"
        "• джокер [ставка] — Джокер\n"
        "• мины [ставка] — Минное поле\n"
        "• дуэль [ставка] — Дуэль (ответом)\n"
        "• coinflip [ставка] — Орёл/Решка\n"
        "• п [сумма] [@user] [коммент] — Перевод\n"
        "• б / баланс — Баланс\n"
        "• /top — Топ игроков\n"
        "• /promo [код] — Активировать промокод\n"
        "• /daily — Ежедневное задание\n"
        "• /referral — Пригласить друга"
    )
    await message.answer(text)

@router.message(F.text == "📋 Команды")
async def cmd_commands_btn(message: Message):
    await cmd_help(message)

@router.message(F.text == "🎮 Мини-игры")
async def cmd_minigames(message: Message):
    text = (
        "🎮 <b>Мини-игры</b>\n\n"
        "🎰 <b>Рулетка</b>: ставь на числа и цвета.\n"
        "🃏 <b>Джокер</b>: открывай карты, избегай скелетов.\n"
        "💣 <b>Мины</b>: сапёр с множителями.\n"
        "⚔️ <b>Дуэли</b>: камень-ножницы-бумага.\n"
        "🪙 <b>Coinflip</b>: орёл или решка."
    )
    await message.answer(text)

@router.message(F.text == "💬 Чаты")
async def cmd_chats(message: Message):
    await message.answer("💬 Общий чат: @creditmania_chat\n📢 Новости: @creditmania_news")

@router.message(F.text == "🛒 Донат")
async def cmd_donate(message: Message):
    await message.answer("🛒 Для покупки донат-валюты обратись к администрации.")

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    rank = get_rank(user_id)
    rank_str = get_rank_emoji(rank)
    mention = get_mention(user_id, user['first_name'])
    text = f"👤 Профиль {mention}\n"
    if rank_str: text += f"{rank_str}\n"
    text += f"💰 Баланс: <b>{format_balance(user['balance'])}</b>\n"
    text += f"🎮 Всего игр: <b>{user['games_played']}</b> | Побед: <b>{user['games_won']}</b>\n"
    if user.get('referrer_id'):
        ref_user = get_user(user['referrer_id'])
        text += f"👥 Пригласил: {get_mention(user['referrer_id'], ref_user['first_name'])}\n"
    kb = get_balance_keyboard(user, in_group(message))
    await message.answer(text, reply_markup=kb)

@router.message(F.text.lower().in_(["б", "баланс", "👤 профиль"]))
async def cmd_balance_text(message: Message):
    await cmd_balance(message)

@router.message(Command("top"))
async def cmd_top(message: Message):
    limit = 10
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit(): limit = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, first_name, balance FROM users ORDER BY balance DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
    if not rows:
        await message.answer("🏆 Топ пуст.")
        return
    text = f"🏆 <b>Топ {len(rows)} игроков:</b>\n\n"
    for i, row in enumerate(rows, 1):
        mention = get_mention(row[0], row[1])
        text += f"{i}. {mention} — <b>{format_balance(row[2])}</b>\n"
    await message.answer(text)

@router.message(F.text == "🏆 Топ")
async def cmd_top_btn(message: Message):
    await cmd_top(message)

@router.message(Command("bonus"))
async def cmd_bonus(message: Message):
    res = await process_bonus_logic(message.from_user.id, message.from_user.first_name or "", in_group(message))
    await message.answer(res)

@router.message(F.text.lower() == "бонус")
async def cmd_bonus_text(message: Message):
    await cmd_bonus(message)

@router.message(Command("daily"))
async def cmd_daily(message: Message):
    if in_group(message): return
    user_id = message.from_user.id
    user = get_user(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user.get('last_daily') == today:
        await message.answer("✅ Вы уже получили ежедневное задание сегодня. Заходите завтра!")
        return
    tasks = [
        {"type": "roulette_play", "desc": "Сыграйте в рулетку 3 раза", "target": 3, "reward": 500},
        {"type": "joker_play", "desc": "Сыграйте в Джокера 2 раза", "target": 2, "reward": 400},
        {"type": "win_game", "desc": "Выиграйте 1 раз в любую игру", "target": 1, "reward": 600}
    ]
    task = random.choice(tasks)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO daily_tasks (user_id, date, task_type) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (user_id, today, task['type']))
            cursor.execute("UPDATE users SET last_daily = %s WHERE user_id = %s", (today, user_id))
            conn.commit()
    text = f"📋 <b>Ежедневное задание:</b>\n\n{task['desc']}\n\n🎁 Награда: <b>{format_balance(task['reward'])}</b>\n\nПрогресс: 0/{task['target']}"
    await message.answer(text)

@router.message(Command("promo"))
async def cmd_promo(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /promo [код]")
        return
    code = args[1]
    promo = get_promo(code)
    if not promo:
        await message.answer("❌ Промокод не найден.")
        return
    if promo['uses'] <= 0:
        await message.answer("❌ Лимит использований промокода исчерпан.")
        return
    user_id = message.from_user.id
    if use_promo(user_id, code, promo['amount']):
        await message.answer(f"✅ Промокод активирован! Начислено <b>{format_balance(promo['amount'])}</b>.")
    else:
        await message.answer("❌ Вы уже активировали этот промокод.")

@router.message(F.text.lower().startswith("п "))
async def cmd_transfer(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit(): return
    amount = int(parts[1])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return
    user = get_user(message.from_user.id)
    if user['balance'] < amount:
        await message.answer("❌ Недостаточно средств.")
        return
    target = None
    comment = ""
    if message.reply_to_message and message.reply_to_message.from_user:
        target = get_user(message.reply_to_message.from_user.id)
        if len(parts) > 2: comment = " ".join(parts[2:])
    elif len(parts) > 2:
        target = find_user_by_identifier(parts[2])
        if len(parts) > 3: comment = " ".join(parts[3:])
    if not target:
        await message.answer("❌ Получатель не найден. Укажите юзернейм или ответьте на сообщение.")
        return
    if target['user_id'] == message.from_user.id:
        await message.answer("❌ Нельзя перевести самому себе.")
        return
    update_balance(user['user_id'], -amount)
    update_balance(target['user_id'], amount)
    mention_from = get_mention(user['user_id'], user['first_name'])
    mention_to = get_mention(target['user_id'], target['first_name'])
    msg = f"💸 {mention_from} перевел {mention_to} <b>{format_balance(amount)}</b>."
    if comment: msg += f"\n💬 Комментарий: <i>{comment}</i>"
    await message.answer(msg)

# ---------- Админ-команды ----------
@router.message(Command("addpromo"))
async def admin_addpromo(message: Message):
    if not is_admin_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 4 or not args[2].isdigit() or not args[3].isdigit():
        await message.answer("Использование: /addpromo [код] [сумма] [кол-во]"); return
    code, amount, uses = args[1].strip(), int(args[2]), int(args[3])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET amount=EXCLUDED.amount, uses=EXCLUDED.uses", (code, amount, uses))
            conn.commit()
    log_admin_action(message.from_user.id, f"addpromo {code} {amount} {uses}")
    await message.answer(f"✅ Промокод <b>{code}</b> создан/обновлён.")

@router.message(Command("delpromo"))
async def admin_delpromo(message: Message):
    if not is_admin_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2: await message.answer("Использование: /delpromo [код]"); return
    code = args[1].strip()
    delete_promo(code)
    log_admin_action(message.from_user.id, f"delpromo {code}")
    await message.answer(f"✅ Промокод <b>{code}</b> удалён.")

@router.message(Command("setbal"))
async def admin_setbal(message: Message):
    if not is_moder_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("Использование: /setbal @username [сумма]"); return
    target = find_user_by_identifier(args[1])
    if not target: await message.answer("❌ Пользователь не найден."); return
    new_balance = int(args[2])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (new_balance, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"setbal {target['user_id']} {new_balance}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} установлен на <b>{format_balance(new_balance)}</b>.")

@router.message(F.text.lower().startswith("выдать "))
async def admin_quick_give(message: Message):
    if not is_moder_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("❌ Использование: выдать @username 5000"); return
    target_str, amount = args[1], int(args[2])
    target = find_user_by_identifier(target_str)
    if not target: await message.answer("❌ Пользователь не найден."); return
    update_balance(target["user_id"], amount)
    log_admin_action(message.from_user.id, f"выдать {target['user_id']} {amount}")
    await message.answer(f"✅ Пользователю {get_mention(target['user_id'], target['first_name'])} выдано <b>{format_balance(amount)}</b>.")

@router.message(Command("resetbal"))
async def admin_resetbal(message: Message):
    if not is_admin_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2: await message.answer("❌ Использование: /resetbal @username"); return
    target = find_user_by_identifier(args[1])
    if not target: await message.answer("❌ Пользователь не найден."); return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000 WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"resetbal {target['user_id']}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} сброшен до 4000 ₸.")

@router.message(Command("setallbal"))
async def admin_setallbal(message: Message):
    if not is_head_or_above(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: /setallbal [сумма]"); return
    amount = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s", (amount,))
            conn.commit()
    log_admin_action(message.from_user.id, f"setallbal {amount}")
    await message.answer(f"✅ Всем пользователям установлен баланс <b>{format_balance(amount)}</b>.")

@router.message(Command("resetallbal"))
async def admin_resetallbal(message: Message):
    if not is_head_or_above(message.from_user.id): return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000")
            conn.commit()
    log_admin_action(message.from_user.id, "resetallbal 4000")
    await message.answer("✅ Баланс всех пользователей сброшен до 4000 ₸.")

@router.message(Command("clearlog"))
async def admin_clearlog(message: Message):
    if not is_head_or_above(message.from_user.id): return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM admin_log")
            conn.commit()
    await message.answer("✅ Журнал действий администраторов очищен.")

# ==================== ИГРЫ ====================

# ---------- Рулетка ----------
REDS = [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]
chat_roulette_bets = {}
chat_last_bet_time = {}

def parse_roulette_target(target_str: str):
    t = target_str.lower()
    if t in ["к","red","красное"]: return "к","RED"
    elif t in ["ч","black","черное"]: return "ч","BLACK"
    elif t in ["even","чет"]: return "even","EVEN"
    elif t in ["odd","нечет"]: return "odd","ODD"
    elif t in ["1-12", "13-24", "25-36", "1-18", "19-36"]: return t,t
    elif t.isdigit():
        val = int(t)
        if 0 <= val <= 36: return str(val),str(val)
    # Произвольные диапазоны запрещены
    return None,None

def choice_emoji(choice):
    if choice in ["RED","к","red","красное"]: return "🔴"
    elif choice in ["BLACK","ч","black","черное"]: return "⚫"
    elif choice in ["EVEN","even","чет"]: return "🔵"
    elif choice in ["ODD","odd","нечет"]: return "🟠"
    elif choice == "0": return "🟢"
    else: return ""

@router.message(F.text.lower() == "ставки")
async def roulette_bets_list(message: Message):
    if not check_group_only(message, "рулетка"): return
    chat_id = message.chat.id
    bets = chat_roulette_bets.get(chat_id, [])
    if not bets: await message.answer("📋 Нет активных ставок."); return
    text = "📋 <b>Активные ставки</b>\n\n"
    for b in bets:
        mention = get_mention(b["user_id"], b["user_name"])
        emoji = choice_emoji(b["choice_display"])
        text += f"• {mention} — {format_balance(b['bet'])} на {emoji} {b['choice_display']}\n"
    await message.answer(text)

@router.message(F.text.lower().in_(["отмена", "cancel"]))
async def roulette_cancel(message: Message):
    if not check_group_only(message, "рулетка"): return
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id not in chat_roulette_bets: return
    
    user_bets = [b for b in chat_roulette_bets[chat_id] if b["user_id"] == user_id]
    if not user_bets:
        await message.answer("❌ У вас нет активных ставок для отмены.")
        return
    
    refund_amount = sum(b["bet"] for b in user_bets)
    update_balance(user_id, refund_amount)
    chat_roulette_bets[chat_id] = [b for b in chat_roulette_bets[chat_id] if b["user_id"] != user_id]
    
    await message.answer(f"✅ Ваши ставки успешно отменены. Возвращено <b>{format_balance(refund_amount)}</b>.")

@router.message(F.text.lower() == "лог")
async def cmd_roulette_log(message: Message):
    if not check_group_only(message, "рулетка"): return
    history = get_roulette_history()
    if not history: await message.answer("📜 История пуста."); return
    lines = [f"{item['roll']}{item['color']}" for item in history]
    await message.answer("📜 <b>История рулетки</b>\n\n" + "\n".join(lines))

@router.message(F.text.lower().in_(["го","старт"]))
async def roulette_go(message: Message):
    if not check_group_only(message, "рулетка"): return
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if chat_id not in chat_roulette_bets or not chat_roulette_bets[chat_id]: return
    
    last_time = chat_last_bet_time.get((chat_id,user_id))
    if last_time:
        diff = (datetime.now()-last_time).total_seconds()
        if diff < 15:
            rem = int(15-diff)
            await message.answer(f"⏳ Раунд можно закончить через {rem} сек."); return
            
    valid_bets = chat_roulette_bets[chat_id]
    chat_roulette_bets[chat_id] = [] # Очищаем ставки
    if not valid_bets: return
    
    try:
        animation_file = get_cached_animation()
        if animation_file: sent_msg = await message.answer_animation(animation=animation_file)
        else: sent_msg = await message.answer("🎰 Крутим рулетку...")
        await asyncio.sleep(5)
        try: await bot.delete_message(chat_id, sent_msg.message_id)
        except: pass
    except: await asyncio.sleep(5)
    
    roll = random.randint(0,36)
    color = "🔴" if roll in REDS else ("🟢" if roll==0 else "⚫")
    add_roulette_log(roll, color)
    res_lines = [f"Рулетка: {roll}{color}"]
    
    for b in valid_bets:
        mention = get_mention(b["user_id"], b["user_name"])
        emoji = choice_emoji(b["choice_display"])
        res_lines.append(f"{mention} {format_balance(b['bet'])} на {emoji} {b['choice_display']}")
    res_lines.append("")
    
    for b in valid_bets:
        choice = b["choice"]; bet = b["bet"]
        is_win = False; multi = 0
        
        if choice in ["к","red"]: is_win = (roll in REDS); multi = 2
        elif choice in ["ч","black"]: is_win = (roll not in REDS and roll!=0); multi = 2
        elif choice in ["even","чет"]: is_win = (roll!=0 and roll%2==0); multi = 2
        elif choice in ["odd","нечет"]: is_win = (roll!=0 and roll%2!=0); multi = 2
        elif choice in ["1-12", "13-24", "25-36"]:
            s,e = map(int, choice.split("-")); is_win = (s<=roll<=e); multi = 3
        elif choice in ["1-18", "19-36"]:
            s,e = map(int, choice.split("-")); is_win = (s<=roll<=e); multi = 2
        else: is_win = (roll==int(choice)); multi = 36
        
        mention = get_mention(b["user_id"], b["user_name"])
        if is_win:
            win_amount = bet * multi
            update_balance(b["user_id"], win_amount)
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (b["user_id"],))
                    conn.commit()
            res_lines.append(f"{mention} ставка {format_balance(bet)} выиграл <b>{format_balance(win_amount)}</b> на {b['choice_display']}")
        else:
            with get_db() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (b["user_id"],))
                    conn.commit()
            res_lines.append(f"{mention} ставка {format_balance(bet)} проиграл")
            
    keyboard_rows = []
    unique_users_in_bets = {}
    for b in valid_bets:
        uid = b["user_id"]
        if uid not in unique_users_in_bets: unique_users_in_bets[uid] = b["user_name"]
    for uid, uname in unique_users_in_bets.items():
        keyboard_rows.append([
            InlineKeyboardButton(text=f"🔄 Повторить ({uname})", callback_data=f"rl_rep_{uid}"),
            InlineKeyboardButton(text=f"2️⃣ Удвоить ({uname})", callback_data=f"rl_dbl_{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    await message.answer("\n".join(res_lines), reply_markup=kb)

@router.callback_query(F.data.startswith("rl_rep_") | F.data.startswith("rl_dbl_"))
async def roulette_action_callback(callback: CallbackQuery):
    data = callback.data; parts = data.split("_"); action = parts[1]; target_uid = int(parts[2])
    if callback.from_user.id != target_uid: await callback.answer("❌ Не ваша ставка!", show_alert=True); return
    
    user_id = target_uid
    user = get_user(user_id, callback.from_user.first_name or "", callback.from_user.last_name or "", callback.from_user.username or "")
    last_bets = get_last_bets(user_id)
    if not last_bets: await callback.answer("❌ Нет сохранённой ставки!", show_alert=True); return
    
    multiplier = 2 if action=="dbl" else 1
    total_cost = sum(b["bet"]*multiplier for b in last_bets)
    if user["balance"] < total_cost: await callback.answer(f"❌ Недостаточно средств! Требуется {format_balance(total_cost)}", show_alert=True); return
    
    # Списываем баланс сразу
    update_balance(user_id, -total_cost)
    
    chat_id = callback.message.chat.id
    if chat_id not in chat_roulette_bets: chat_roulette_bets[chat_id] = []
    updated_last_bets = []; displays = []
    for b in last_bets:
        new_bet_amt = b["bet"] * multiplier
        chat_roulette_bets[chat_id].append({"user_id":user_id,"user_name":user["first_name"],"bet":new_bet_amt,"choice":b["choice"],"choice_display":b["choice_display"]})
        updated_last_bets.append({"bet":new_bet_amt,"choice":b["choice"],"choice_display":b["choice_display"]})
        emoji = choice_emoji(b["choice_display"])
        displays.append(f"{format_balance(new_bet_amt)} на {emoji} {b['choice_display']}")
        
    chat_last_bet_time[(chat_id,user_id)] = datetime.now()
    save_last_bets(user_id, updated_last_bets)
    await callback.answer("✅ Ставка сделана!")
    mention = get_mention(user_id, user["first_name"])
    await callback.message.answer(f"✅ Ставка принята: {mention} всего {format_balance(total_cost)} ({', '.join(displays)})")

@router.message(F.text)
async def generic_message_handler(message: Message):
    if message.text.startswith("/"): return
    text = message.text.strip().lower()
    parts = text.split()
    if not parts or not parts[0].isdigit():
        return

    bet_per_item = int(parts[0])
    targets = parts[1:]
    if not targets:
        return

    valid_targets = []
    for tgt in targets:
        code, display = parse_roulette_target(tgt)
        if code is None:
            # Отклоняем всё сообщение, если хотя бы один объект ставки не распознан
            return
        valid_targets.append((code, display))

    if not valid_targets:
        return

    if not check_group_only(message, "рулетка"): return

    if bet_per_item < MIN_BET:
        await message.answer(f"❌ Минимальная ставка на один объект: {MIN_BET} ₸")
        return

    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    total_bet = bet_per_item * len(valid_targets)

    if user["balance"] < total_bet:
        await message.answer(f"❌ Недостаточно средств. Требуется {format_balance(total_bet)} для {len(valid_targets)} ставок.")
        return

    # Списание баланса сразу!
    update_balance(user["user_id"], -total_bet)

    chat_id = message.chat.id
    if chat_id not in chat_roulette_bets:
        chat_roulette_bets[chat_id] = []

    new_user_bets = []
    displays = []

    for code, display in valid_targets:
        chat_roulette_bets[chat_id].append({
            "user_id": user["user_id"],
            "user_name": user["first_name"],
            "bet": bet_per_item,
            "choice": code,
            "choice_display": display
        })
        new_user_bets.append({
            "bet": bet_per_item,
            "choice": code,
            "choice_display": display
        })
        emoji = choice_emoji(display)
        displays.append(f"{format_balance(bet_per_item)} на {emoji} {display}")

    chat_last_bet_time[(chat_id, user["user_id"])] = datetime.now()
    save_last_bets(user["user_id"], new_user_bets)

    mention = get_mention(user["user_id"], user["first_name"])
    displays_str = ", ".join(displays)
    await message.answer(f"✅ Ставка принята: {mention} всего {format_balance(total_bet)} ({displays_str})")

# ---------- Джокер ----------
joker_sessions = {}
JOKER_MULTIS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]

@router.message(F.text.lower().startswith("джокер"))
async def game_joker(message: Message):
    if not check_group_only(message, "джокер"): return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: джокер [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET: await message.answer(f"Минимальная ставка {MIN_BET} ₸"); return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet: await message.answer("Недостаточно средств"); return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    skull_pos = random.randint(0,2)
    joker_sessions[session_id] = {
        "user_id": user["user_id"], "user_name": user["first_name"],
        "bet": bet, "level": 0, "skull_pos": skull_pos, "history": []
    }
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        f"{mention}, вы начали игру Джокер!\n💰 Ставка: {format_balance(bet)}\n💵 Выигрыш: x{JOKER_MULTIS[0]} = {format_balance(bet)}",
        reply_markup=get_joker_kb(session_id, finished=False)
    )

def get_joker_kb(session_id, finished=False):
    sess = joker_sessions.get(session_id)
    kb = []
    if sess and "history" in sess:
        for row in sess["history"]:
            kb.append(row)
    if not finished:
        row = [
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_0"),
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_1"),
            InlineKeyboardButton(text="🎴", callback_data=f"jk_{session_id}_2")
        ]
        kb.append(row)
        # Зелёная кнопка после первого успешного хода
        if sess and sess["level"] > 0:
            btn_text = "🟢 💰 Забрать выигрыш"
        else:
            btn_text = "💰 Забрать выигрыш"
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=f"jk_cash_{session_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data.startswith("jk_"))
async def joker_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    if parts[1] == "noop":
        await callback.answer("Этаж пройден.")
        return
    if parts[1] == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in joker_sessions:
            await callback.answer("Игра завершена.")
            return
        sess = joker_sessions[session_id]
        if sess["user_id"] != callback.from_user.id:
            await callback.answer("Чужая игра!")
            return
        lvl = sess["level"]
        win = int(sess["bet"] * JOKER_MULTIS[lvl])
        update_balance(sess["user_id"], win)
        del joker_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(f"{mention}, вы забрали выигрыш <b>{format_balance(win)}</b>!")
        return

    session_id = f"{parts[1]}_{parts[2]}"
    choice = int(parts[3])
    if session_id not in joker_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = joker_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("Чужая игра!")
        return

    skull_pos = sess["skull_pos"]
    mention = get_mention(sess["user_id"], sess["user_name"])

    if choice == skull_pos:
        row_buttons = []
        for i in range(3):
            if i == skull_pos:
                row_buttons.append(InlineKeyboardButton(text="💀", callback_data="jk_noop"))
            else:
                row_buttons.append(InlineKeyboardButton(text="🃏", callback_data="jk_noop"))
        sess["history"].append(row_buttons)
        del joker_sessions[session_id]
        await callback.message.edit_text(
            f"{mention}, вы проиграли! Проиграно {format_balance(sess['bet'])}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=sess["history"])
        )
        return

    row_buttons = []
    for i in range(3):
        if i == choice:
            row_buttons.append(InlineKeyboardButton(text="🃏", callback_data="jk_noop"))
        elif i == skull_pos:
            row_buttons.append(InlineKeyboardButton(text="💀", callback_data="jk_noop"))
        else:
            row_buttons.append(InlineKeyboardButton(text="🃏", callback_data="jk_noop"))
    sess["history"].append(row_buttons)
    sess["level"] += 1
    lvl = sess["level"]
    sess["skull_pos"] = random.randint(0,2)

    if lvl >= len(JOKER_MULTIS) - 1:
        win = int(sess["bet"] * JOKER_MULTIS[-1])
        update_balance(sess["user_id"], win)
        del joker_sessions[session_id]
        await callback.message.edit_text(
            f"{mention}, максимальный множитель! Выигрыш <b>{format_balance(win)}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=sess["history"])
        )
    else:
        cur_win = int(sess["bet"] * JOKER_MULTIS[lvl])
        await callback.message.edit_text(
            f"{mention}, вы продолжаете игру Джокер!\n"
            f"💰 Ставка: {format_balance(sess['bet'])}\n"
            f"💵 Выигрыш: x{JOKER_MULTIS[lvl]} = {format_balance(cur_win)}",
            reply_markup=get_joker_kb(session_id, finished=False)
        )

# ---------- Мины ----------
mines_sessions = {}
MINES_MULTIS = [1.25, 1.60, 2.15, 3.20, 5.30, 8.50, 10.50]

@router.message(F.text.lower().startswith("мины"))
async def game_mines(message: Message):
    if not check_group_only(message, "мины"): return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: мины [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET: await message.answer(f"Минимальная ставка {MIN_BET} ₸"); return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet: await message.answer("Недостаточно средств"); return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    mines_sessions[session_id] = {"user_id":user["user_id"],"user_name":user["first_name"],"bet":bet,"opened":[],"mines":random.sample(range(25),5),"game_over":False}
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(
        get_mines_text(mention, bet, 1.0, bet),
        reply_markup=get_mines_kb(session_id, [], False))

def get_mines_text(mention, bet, multi, current_win):
    return f"{mention}, вы начали игру Минное поле!\n💰 Ставка: {format_balance(bet)}\n💵 Выигрыш: x{multi} = {format_balance(current_win)}"

def get_mines_kb(session_id, opened, game_over, mines=None):
    buttons = []
    for i in range(25):
        if not game_over:
            text = "ᅠ" if i in opened else "❓"
            buttons.append(InlineKeyboardButton(text=text, callback_data=f"mn_{session_id}_{i}"))
        else:
            if i in mines: text = "💣"
            elif i in opened: text = "ᅠ"
            else: text = "❓"
            buttons.append(InlineKeyboardButton(text=text, callback_data=f"mn_noop_{i}"))
    kb = [buttons[r*5:(r+1)*5] for r in range(5)]
    if not game_over:
        kb.append([InlineKeyboardButton(text="💰 Забрать выигрыш", callback_data=f"mn_cash_{session_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data.startswith("mn_"))
async def mines_callback(callback: CallbackQuery):
    parts = callback.data.split("_"); action = parts[1]
    if action == "noop": await callback.answer("Игра завершена."); return
    if action == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in mines_sessions: await callback.answer("Игра не активна."); return
        sess = mines_sessions[session_id]
        if sess["user_id"] != callback.from_user.id: await callback.answer("Чужая игра!"); return
        opened_cnt = len(sess["opened"]); multi = MINES_MULTIS[opened_cnt-1] if opened_cnt>0 else 1.0
        win = int(sess["bet"] * multi); update_balance(sess["user_id"], win)
        del mines_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(f"{mention}, вы забрали выигрыш <b>{format_balance(win)}</b>!")
        return
    session_id = f"{parts[1]}_{parts[2]}"; cell = int(parts[3])
    if session_id not in mines_sessions: await callback.answer("Игра завершена."); return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id: await callback.answer("Чужая игра!"); return
    if cell in sess["opened"]: await callback.answer("Уже открыто!"); return
    mention = get_mention(sess["user_id"], sess["user_name"])
    if cell in sess["mines"]:
        sess["game_over"] = True; del mines_sessions[session_id]
        await callback.message.edit_text(f"{mention}, вы подорвались! Проиграно {format_balance(sess['bet'])}.",
                                         reply_markup=get_mines_kb(session_id, sess["opened"], True, sess["mines"]))
    else:
        sess["opened"].append(cell)
        opened_cnt = len(sess["opened"]); multi = MINES_MULTIS[opened_cnt-1]; current_win = int(sess["bet"] * multi)
        if opened_cnt >= len(MINES_MULTIS):
            win = int(sess["bet"] * MINES_MULTIS[-1]); update_balance(sess["user_id"], win)
            del mines_sessions[session_id]
            await callback.message.edit_text(f"{mention}, максимальный множитель! Выигрыш <b>{format_balance(win)}</b>",
                                             reply_markup=get_mines_kb(session_id, sess["opened"], True, sess["mines"]))
        else:
            await callback.message.edit_text(
                get_mines_text(mention, sess["bet"], multi, current_win),
                reply_markup=get_mines_kb(session_id, sess["opened"], False))

# ---------- Coinflip ----------
coinflip_sessions = {}

@router.message(F.text.lower().startswith("coinflip"))
async def game_coinflip(message: Message):
    if not check_group_only(message, "coinflip"): return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: coinflip [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET: await message.answer(f"Минимальная ставка {MIN_BET} ₸"); return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet: await message.answer("Недостаточно средств"); return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    coinflip_sessions[session_id] = {"user_id":user["user_id"],"bet":bet}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🦅 Орёл", callback_data=f"cf_{session_id}_heads"),
         InlineKeyboardButton(text="🪙 Решка", callback_data=f"cf_{session_id}_tails")]
    ])
    mention = get_mention(user["user_id"], user["first_name"])
    await message.answer(f"🪙 {mention} подбрасывает монетку!\n💰 Ставка: <b>{format_balance(bet)}</b>\nВыберите сторону:", reply_markup=kb)

@router.callback_query(F.data.startswith("cf_"))
async def coinflip_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = f"{parts[1]}_{parts[2]}"
    choice = parts[3]
    if session_id not in coinflip_sessions: await callback.answer("Игра завершена."); return
    sess = coinflip_sessions[session_id]
    if callback.from_user.id != sess["user_id"]: await callback.answer("Чужая игра!"); return
    del coinflip_sessions[session_id]
    result = random.choice(["heads","tails"])
    emoji = "🦅" if result == "heads" else "🪙"
    if choice == result:
        win = sess["bet"] * 2
        update_balance(sess["user_id"], win)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(f"🪙 Выпало: {emoji}\n🎉 Вы выиграли <b>{format_balance(win)}</b>!")
    else:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (sess["user_id"],))
                conn.commit()
        await callback.message.edit_text(f"🪙 Выпало: {emoji}\n❌ Вы проиграли <b>{format_balance(sess['bet'])}</b>.")
    await callback.answer()

# ---------- Дуэли ----------
duels = {}

@router.message(F.text.lower().startswith("дуэль"))
async def game_duel(message: Message):
    if not check_group_only(message, "дуэль"): return
    if not message.reply_to_message: await message.answer("❌ Ответьте на сообщение соперника."); return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id or target.is_bot: return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: дуэль [ставка] в ответ на сообщение.")
        return
    bet = int(args[1])
    if bet < MIN_BET: await message.answer(f"Минимальная ставка {MIN_BET} ₸"); return
    p1_data = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    p2_data = get_user(target.id, target.first_name or "", target.last_name or "", target.username or "")
    if p1_data["balance"] < bet or p2_data["balance"] < bet:
        await message.answer("❌ У одного из участников недостаточно средств."); return
        
    duel_id = f"{message.chat.id}_{message.message_id}"
    duels[duel_id] = {"p1_id":p1_data["user_id"],"p1_name":p1_data["first_name"],"p1_choice":None,
                      "p2_id":p2_data["user_id"],"p2_name":p2_data["first_name"],"p2_choice":None,
                      "bet":bet,"chat_id":message.chat.id}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Сражаться", callback_data=f"duel_acc_{duel_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"duel_den_{duel_id}")]])
    p1_mention = get_mention(p1_data["user_id"], p1_data["first_name"])
    p2_mention = get_mention(p2_data["user_id"], p2_data["first_name"])
    msg = await message.answer(f"⚔️ {p1_mention} вызывает {p2_mention} на дуэль! Ставка: {format_balance(bet)}", reply_markup=kb)
    asyncio.create_task(duel_accept_timeout(duel_id, msg))

async def duel_accept_timeout(duel_id, msg: Message):
    await asyncio.sleep(60)
    if duel_id in duels:
        if not duels[duel_id].get("accepted"):
            del duels[duel_id]
            try: await msg.edit_text("⏱ Время вышло! Ничья, ставки возвращены.")
            except: pass

@router.callback_query(F.data.startswith("duel_"))
async def duel_init_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    # Защита от отрицательных ID: берем элементы как есть (parts[2] - чат, parts[3] - сообщение)
    duel_id = f"{parts[2]}_{parts[3]}"
    
    if duel_id not in duels: await callback.answer("Дуэль устарела."); return
    duel = duels[duel_id]; uid = callback.from_user.id
    
    if action == "den":
        if uid not in (duel["p1_id"], duel["p2_id"]): await callback.answer("Вы не участник!"); return
        del duels[duel_id]; await callback.message.edit_text("❌ Дуэль отклонена.")
        return
        
    if action == "acc":
        if uid != duel["p2_id"]: await callback.answer("Сражаться может только вызываемый!"); return
        u1 = get_user(duel["p1_id"]); u2 = get_user(duel["p2_id"])
        if u1["balance"] < duel["bet"] or u2["balance"] < duel["bet"]:
            await callback.message.edit_text("❌ Недостаточно средств."); del duels[duel_id]; return
            
        duel["accepted"] = True
        update_balance(duel["p1_id"], -duel["bet"]); update_balance(duel["p2_id"], -duel["bet"])
        p1_mention = get_mention(duel["p1_id"], duel["p1_name"]); p2_mention = get_mention(duel["p2_id"], duel["p2_name"])
        
        kb1 = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🪨", callback_data=f"rps_{duel_id}_rock"),
            InlineKeyboardButton(text="📄", callback_data=f"rps_{duel_id}_paper"),
            InlineKeyboardButton(text="✂️", callback_data=f"rps_{duel_id}_scissors")]])
        kb2 = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🪨", callback_data=f"rps_{duel_id}_rock"),
            InlineKeyboardButton(text="📄", callback_data=f"rps_{duel_id}_paper"),
            InlineKeyboardButton(text="✂️", callback_data=f"rps_{duel_id}_scissors")]])
            
        try:
            await bot.send_message(duel["p1_id"], f"⚔️ Дуэль против {p2_mention}! Ставка: {format_balance(duel['bet'])}.", reply_markup=kb1)
            await bot.send_message(duel["p2_id"], f"⚔️ Дуэль против {p1_mention}! Ставка: {format_balance(duel['bet'])}.", reply_markup=kb2)
            await callback.message.edit_text("⏳ Игроки делают выбор...")
            asyncio.create_task(duel_choice_timeout(duel_id, callback.message))
        except:
            update_balance(duel["p1_id"], duel["bet"]); update_balance(duel["p2_id"], duel["bet"])
            del duels[duel_id]
            await callback.message.edit_text("❌ Дуэль отменена, так как кто-то заблокировал бота.")

async def duel_choice_timeout(duel_id, msg: Message):
    await asyncio.sleep(60)
    if duel_id in duels:
        d = duels[duel_id]; update_balance(d["p1_id"], d["bet"]); update_balance(d["p2_id"], d["bet"])
        del duels[duel_id]
        try: await bot.edit_message_text("⏱ Время вышло! Ничья.", chat_id=msg.chat.id, message_id=msg.message_id)
        except: pass

@router.callback_query(F.data.startswith("rps_"))
async def rps_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    # rps_{chat_id}_{message_id}_{choice}
    duel_id = f"{parts[1]}_{parts[2]}"
    choice = parts[3]
    
    if duel_id not in duels: await callback.answer("Дуэль устарела."); return
    duel = duels[duel_id]; uid = callback.from_user.id
    
    if uid == duel["p1_id"]: duel["p1_choice"] = choice
    elif uid == duel["p2_id"]: duel["p2_choice"] = choice
    else: return
    
    await callback.message.edit_text("✅ Выбор сделан. Ожидаем соперника...")
    
    if duel["p1_choice"] and duel["p2_choice"]:
        c1, c2 = duel["p1_choice"], duel["p2_choice"]; bank = duel["bet"]*2
        if c1 == c2:
            update_balance(duel["p1_id"], duel["bet"]); update_balance(duel["p2_id"], duel["bet"])
            res_text = "🤝 Ничья! Ставки возвращены."
        else:
            rules = {"rock":"scissors","scissors":"paper","paper":"rock"}
            if rules[c1] == c2: win_id = duel["p1_id"]; winner_mention = get_mention(duel["p1_id"], duel["p1_name"])
            else: win_id = duel["p2_id"]; winner_mention = get_mention(duel["p2_id"], duel["p2_name"])
            update_balance(win_id, bank); res_text = f"🏆 Победитель: {winner_mention} забирает <b>{format_balance(bank)}</b>!"
            
        try: await bot.send_message(duel["chat_id"], f"⚔️ <b>Результат дуэли</b>\n{res_text}")
        except: pass
        del duels[duel_id]


# ---------- Web-сервер для Render и запуск бота ----------
async def handle_ping(request):
    return web.Response(text="Бот CreditMania успешно работает!")

async def main():
    # Запускаем простой HTTP-сервер для того, чтобы Render не усыплял бота
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")

    # Запуск поллинга самого бота Telegram
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот CreditMania запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
