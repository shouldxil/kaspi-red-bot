import asyncio
import logging
import random
import os
import re
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
    FSInputFile,
    BufferedInputFile
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 7934547554
MIN_BET = 10
BOT_USERNAME = ""

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

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
    try:
        yield conn
    finally:
        DB_POOL.putconn(conn)

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
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
                conn.commit()
            except psycopg2.Error:
                conn.rollback()
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
            for col, col_type in [
                ("user_id", "BIGINT"),
                ("bet_amount", "BIGINT"),
                ("target", "TEXT"),
                ("win_amount", "BIGINT"),
                ("timestamp", "TEXT"),
            ]:
                try:
                    cursor.execute(f"ALTER TABLE roulette_log ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()
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

init_db()

# ---------- Хелперы ----------
def get_user(user_id: int, first_name: str = "", last_name: str = "", username: str = "") -> dict:
    safe_name = first_name if first_name else "Игрок"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE user_id = %s",
                (user_id,))
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
                cursor.execute(
                    "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE user_id = %s",
                    (int(clean_id),))
            else:
                cursor.execute(
                    "SELECT user_id, first_name, last_name, username, balance, last_bonus, games_played, games_won, referrer_id, referred_count, last_daily FROM users WHERE LOWER(username) = LOWER(%s)",
                    (clean_id,))
            row = cursor.fetchone()
            if row:
                return {"user_id": row[0], "first_name": row[1], "last_name": row[2], "username": row[3], "balance": row[4], "last_bonus": row[5], "games_played": row[6], "games_won": row[7], "referrer_id": row[8], "referred_count": row[9], "last_daily": row[10]}
    return None

def get_rank(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "owner"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT rank FROM admins WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else "user"

def get_rank_emoji(rank: str) -> str:
    return {"owner": "💎 Владелец", "head": "👑 Главный администратор", "admin": "🔰 Администратор", "moder": "⭐ Модератор"}.get(rank, "")

def is_admin(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin", "moder")

def is_moder_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin", "moder")

def is_admin_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head", "admin")

def is_head_or_above(user_id: int) -> bool:
    return get_rank(user_id) in ("owner", "head")

def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID

def has_secret_power(user_id: int, command: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM secret_powers WHERE user_id = %s AND command_name = %s", (user_id, command))
            return cursor.fetchone() is not None

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
            cursor.execute(
                "INSERT INTO settings (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value))
            conn.commit()

def is_cursed(user_id: int) -> bool:
    return get_setting(f"cursed_{user_id}", "0") == "1"

def add_roulette_log(roll: int | None, color: str | None, user_id=None, bet_amount=None, target=None, win_amount=None):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roulette_log (roll, color, user_id, bet_amount, target, win_amount, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (roll, color, user_id, bet_amount, target, win_amount, datetime.now().isoformat())
            )
            if user_id is None:
                cursor.execute(
                    "DELETE FROM roulette_log WHERE roll IS NOT NULL AND id NOT IN (SELECT id FROM roulette_log WHERE roll IS NOT NULL ORDER BY id DESC LIMIT 10)"
                )
            conn.commit()

def get_roulette_history():
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT roll, color FROM roulette_log WHERE roll IS NOT NULL ORDER BY id DESC LIMIT 10")
            return [{"roll": r[0], "color": r[1]} for r in cursor.fetchall()]

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
                try:
                    return json.loads(row[0])
                except:
                    return []
    return []

def get_promo(code: str):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT code, amount, uses FROM promocodes WHERE code = %s", (code,))
            row = cursor.fetchone()
            if row:
                return {"code": row[0], "amount": row[1], "uses": row[2]}
    return None

def use_promo(user_id: int, code: str, amount: int):
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM used_promocodes WHERE user_id=%s AND code=%s", (user_id, code))
            if cursor.fetchone():
                return False
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

def save_balance_checkpoint(user_id: int):
    user = get_user(user_id)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO balance_checkpoints (user_id, balance, checkpoint_time) VALUES (%s,%s,%s)",
                (user_id, user['balance'], datetime.now().isoformat()))
            conn.commit()

def restore_last_checkpoint(user_id: int) -> bool:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT balance FROM balance_checkpoints WHERE user_id=%s ORDER BY checkpoint_time DESC LIMIT 1", (user_id,))
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (row[0], user_id))
                conn.commit()
                return True
    return False

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

def clean_first_name(first_name: str, user_id: int) -> str:
    if not first_name:
        return f"Пользователь - {user_id}"
    invisible = {"ㅤ", "ᅠ"}
    visible_found = False
    for ch in first_name:
        if ch.strip() and ch not in invisible:
            visible_found = True
            break
    if not visible_found:
        return f"Пользователь - {user_id}"
    return first_name

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
        except:
            pass
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
        except:
            pass
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
        except:
            pass
    if in_group(message):
        await message.answer("Привет! Я бот CreditMania. Напиши мне в личные сообщения для главного меню.")
        return
    welcome_text = (
        "👋 Добро пожаловать в CreditMania!\n\n"
        "🎰 Игры: Рулетка, Джокер, Мины, Дуэли\n"
        "💎 Валюта: ₸ (тенге)\n"
        "💰 Начальный баланс: 4 000 ₸\n\n"
        "👨‍💻 Связь с разработчиком: @se7ze\n\n"
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
        "• лог — История рулетки\n"
        "• ставки — Текущие ставки\n"
        "• джокер [ставка] — Джокер\n"
        "• мины [ставка] — Минное поле\n"
        "• дуэль [ставка] — Дуэль (ответом на сообщение)\n"
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

@router.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer("📜 Правила нашего бота доступны по ссылке: https://teletype.in/@arrest1k/fBWeKkclC7u")

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
    await message.answer("🛒 Для покупки CRD и других услуг пиши @se7ze.")

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    rank = get_rank(user_id)
    rank_str = get_rank_emoji(rank)
    mention = get_mention(user_id, user['first_name'])
    text = f"👤 Профиль {mention}\n"
    if rank_str:
        text += f"{rank_str}\n"
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
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, balance FROM users ORDER BY balance DESC LIMIT %s",
                (limit,)
            )
            rows = cursor.fetchall()

    if not rows:
        await message.answer("🏆 Топ пуст.")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    text = f"🏆 <b>Топ {len(rows)} игроков:</b>\n\n"
    for i, row in enumerate(rows, 1):
        user_id, first_name, balance = row
        display_name = clean_first_name(first_name, user_id)
        mention = f'<a href="tg://user?id={user_id}">{display_name}</a>'
        medal = medals.get(i, "")
        text += f"{medal} {i}. {mention} — <b>{format_balance(balance)}</b>\n"

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
    if in_group(message):
        return
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

@router.message(Command("referral"))
async def cmd_referral(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"Ваша ссылка: {ref_link}\n"
        f"Приглашено: <b>{user['referred_count']}</b>\n"
        f"Бонус за каждого: <b>10 000 ₸</b>\n\n"
        f"Отправьте эту ссылку друзьям и получайте бонусы!"
    )
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
    if len(parts) < 2 or not parts[1].isdigit():
        return
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
        if len(parts) > 2:
            comment = " ".join(parts[2:])
    elif len(parts) > 2:
        target = find_user_by_identifier(parts[2])
        if len(parts) > 3:
            comment = " ".join(parts[3:])
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
    if comment:
        msg += f"\n💬 Комментарий: <i>{comment}</i>"
    await message.answer(msg)

# ---------- Админ-команды (обычные) ----------
@router.message(Command("addpromo"))
async def admin_addpromo(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 4 or not args[2].isdigit() or not args[3].isdigit():
        await message.answer("Использование: /addpromo [код] [сумма] [кол-во]")
        return
    code, amount, uses = args[1].strip(), int(args[2]), int(args[3])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO promocodes (code, amount, uses) VALUES (%s,%s,%s) ON CONFLICT (code) DO UPDATE SET amount=EXCLUDED.amount, uses=EXCLUDED.uses",
                           (code, amount, uses))
            conn.commit()
    log_admin_action(message.from_user.id, f"addpromo {code} {amount} {uses}")
    await message.answer(f"✅ Промокод <b>{code}</b> создан/обновлён: <b>{format_balance(amount)}</b>, исп: <b>{uses}</b>.")

@router.message(Command("delpromo"))
async def admin_delpromo(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /delpromo [код]")
        return
    code = args[1].strip()
    delete_promo(code)
    log_admin_action(message.from_user.id, f"delpromo {code}")
    await message.answer(f"✅ Промокод <b>{code}</b> удалён.")

@router.message(Command("setbal"))
async def admin_setbal(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("Использование: /setbal @username [сумма]")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    new_balance = int(args[2])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (new_balance, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"setbal {target['user_id']} {new_balance}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} установлен на <b>{format_balance(new_balance)}</b>.")

@router.message(Command("info"))
async def admin_info(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /info @username")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    rank = get_rank(target['user_id'])
    text = (f"👤 <b>{target['first_name']}</b>\n"
            f"🆔 ID: {target['user_id']}\n"
            f"👤 Username: @{target['username'] or 'нет'}\n"
            f"📊 Ранг: {get_rank_emoji(rank)}\n"
            f"💰 Баланс: <b>{format_balance(target['balance'])}</b>\n"
            f"🎮 Игр: {target['games_played']} | Побед: {target['games_won']}\n"
            f"🕒 Последний бонус: {target['last_bonus'] or 'никогда'}")
    await message.answer(text)

@router.message(F.text.lower().startswith("выдать "))
async def admin_quick_give(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("❌ Использование: выдать @username 5000")
        return
    target_str, amount = args[1], int(args[2])
    target = find_user_by_identifier(target_str)
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    update_balance(target["user_id"], amount)
    log_admin_action(message.from_user.id, f"выдать {target['user_id']} {amount}")
    await message.answer(f"✅ Пользователю {get_mention(target['user_id'], target['first_name'])} выдано <b>{format_balance(amount)}</b>.")

@router.message(Command("take"))
async def admin_take(message: Message):
    if not is_moder_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3 or not args[2].isdigit():
        await message.answer("❌ Использование: /take @username 500")
        return
    target_str, amount = args[1], int(args[2])
    target = find_user_by_identifier(target_str)
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    update_balance(target["user_id"], -amount)
    log_admin_action(message.from_user.id, f"take {target['user_id']} {amount}")
    await message.answer(f"✅ У пользователя {get_mention(target['user_id'], target['first_name'])} списано <b>{format_balance(amount)}</b>.")

@router.message(Command("resetbal"))
async def admin_resetbal(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /resetbal @username")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000 WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"resetbal {target['user_id']}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} сброшен до 4000 ₸.")

@router.message(Command("add_admin"))
async def admin_add(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /add_admin @username [moder/admin/head]")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return
    rank = args[2] if len(args) > 2 and args[2] in ("moder", "admin", "head") else "moder"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO admins (user_id, rank) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank", (target["user_id"], rank))
            conn.commit()
    log_admin_action(message.from_user.id, f"add_admin {target['user_id']} {rank}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} назначен {get_rank_emoji(rank)}.")

@router.message(Command("remove_admin"))
async def admin_remove(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /remove_admin @username")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("❌ Администратор не найден.")
        return
    if target["user_id"] == ADMIN_ID:
        await message.answer("❌ Нельзя удалить владельца.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM admins WHERE user_id = %s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"remove_admin {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} разжалован.")

@router.message(Command("list_admins"))
async def admin_list(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, rank FROM admins ORDER BY CASE rank WHEN 'head' THEN 1 WHEN 'admin' THEN 2 WHEN 'moder' THEN 3 END")
            rows = cursor.fetchall()
    text = "🛡 <b>Список администраторов:</b>\n"
    for r in rows:
        u = get_user(r[0])
        text += f"{get_rank_emoji(r[1])} {get_mention(r[0], u['first_name'])}\n"
    await message.answer(text)

@router.message(Command("setbonus"))
async def admin_setbonus(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(f"🎁 Текущий бонус: <b>{format_balance(int(get_setting('bonus_amount', '3000')))}</b>")
        return
    amount = int(args[1])
    set_setting("bonus_amount", str(amount))
    log_admin_action(message.from_user.id, f"setbonus {amount}")
    await message.answer(f"✅ Бонус изменён на <b>{format_balance(amount)}</b>")

@router.message(Command("setcooldown"))
async def admin_setcooldown(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: /setcooldown 8")
        return
    hours = int(args[1])
    set_setting("bonus_cooldown", str(hours))
    log_admin_action(message.from_user.id, f"setcooldown {hours}")
    await message.answer(f"✅ Кулдаун бонуса изменён на <b>{hours} ч.</b>")

@router.message(Command("stats"))
async def admin_stats(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance),0) FROM users")
            users_count, total_balance = cursor.fetchone()
    await message.answer(f"📊 <b>Статистика</b>\n👥 Пользователей: <b>{users_count}</b>\n💰 Общий баланс: <b>{format_balance(total_balance or 0)}</b>")

@router.message(Command("broadcast"))
async def admin_broadcast(message: Message):
    if not is_admin_or_above(message.from_user.id):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Использование: /broadcast текст")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users")
            users = [row[0] for row in cursor.fetchall()]
    success = 0
    failed = 0
    status_msg = await message.answer(f"📢 Рассылка началась (0/{len(users)})...")
    for u_id in users:
        try:
            await bot.send_message(int(u_id), f"📢 <b>Объявление</b>\n\n{text}")
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await status_msg.edit_text(f"✅ Рассылка завершена!\n📤 Успешно: {success}\n❌ Ошибок: {failed}")

@router.message(Command("disable"))
async def admin_disable(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /disable [рулетка/джокер/мины/дуэль/coinflip]")
        return
    game = args[1].lower()
    if game not in ["рулетка", "джокер", "мины", "дуэль", "coinflip"]:
        await message.answer("❌ Неизвестная игра.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO disabled_games (game_name) VALUES (%s) ON CONFLICT DO NOTHING", (game,))
            conn.commit()
    await message.answer(f"🚫 Игра <b>{game}</b> отключена.")

@router.message(Command("enable"))
async def admin_enable(message: Message):
    if not is_head_or_above(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: /enable [рулетка/джокер/мины/дуэль/coinflip]")
        return
    game = args[1].lower()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM disabled_games WHERE game_name = %s", (game,))
            conn.commit()
    await message.answer(f"✅ Игра <b>{game}</b> включена.")
    # ==================== ИГРЫ ====================
# ---------- Джокер ----------
joker_sessions = {}
JOKER_MULTIS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]

@router.message(F.text.lower().startswith("джокер"))
async def game_joker(message: Message):
    if not check_group_only(message, "джокер"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: джокер [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} ₸")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    skull_pos = random.randint(0, 2)
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
        btn_text = "🟢 💰 Забрать выигрыш" if sess and sess["level"] > 0 else "💰 Забрать выигрыш"
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

    if is_cursed(callback.from_user.id) and random.random() < 0.25:
        choice = skull_pos

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
    sess["skull_pos"] = random.randint(0, 2)

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
    if not check_group_only(message, "мины"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: мины [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} ₸")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    mines_sessions[session_id] = {"user_id": user["user_id"], "user_name": user["first_name"], "bet": bet, "opened": [], "mines": random.sample(range(25), 5), "game_over": False}
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
            if i in mines:
                text = "💣"
            elif i in opened:
                text = "ᅠ"
            else:
                text = "❓"
            buttons.append(InlineKeyboardButton(text=text, callback_data=f"mn_noop_{i}"))
    kb = [buttons[r*5:(r+1)*5] for r in range(5)]
    if not game_over:
        kb.append([InlineKeyboardButton(text="💰 Забрать выигрыш", callback_data=f"mn_cash_{session_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data.startswith("mn_"))
async def mines_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    if action == "noop":
        await callback.answer("Игра завершена.")
        return
    if action == "cash":
        session_id = f"{parts[2]}_{parts[3]}"
        if session_id not in mines_sessions:
            await callback.answer("Игра не активна.")
            return
        sess = mines_sessions[session_id]
        if sess["user_id"] != callback.from_user.id:
            await callback.answer("Чужая игра!")
            return
        opened_cnt = len(sess["opened"])
        multi = MINES_MULTIS[opened_cnt-1] if opened_cnt > 0 else 1.0
        win = int(sess["bet"] * multi)
        update_balance(sess["user_id"], win)
        del mines_sessions[session_id]
        mention = get_mention(sess["user_id"], sess["user_name"])
        await callback.message.edit_text(f"{mention}, вы забрали выигрыш <b>{format_balance(win)}</b>!")
        return
    session_id = f"{parts[1]}_{parts[2]}"
    cell = int(parts[3])
    if session_id not in mines_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = mines_sessions[session_id]
    if sess["user_id"] != callback.from_user.id:
        await callback.answer("Чужая игра!")
        return
    if cell in sess["opened"]:
        await callback.answer("Уже открыто!")
        return
    mention = get_mention(sess["user_id"], sess["user_name"])

    if is_cursed(callback.from_user.id) and cell not in sess["mines"] and random.random() < 0.2:
        sess["mines"].append(cell)

    if cell in sess["mines"]:
        sess["game_over"] = True
        del mines_sessions[session_id]
        await callback.message.edit_text(f"{mention}, вы подорвались! Проиграно {format_balance(sess['bet'])}.",
                                         reply_markup=get_mines_kb(session_id, sess["opened"], True, sess["mines"]))
    else:
        sess["opened"].append(cell)
        opened_cnt = len(sess["opened"])
        multi = MINES_MULTIS[opened_cnt-1]
        current_win = int(sess["bet"] * multi)
        if opened_cnt >= len(MINES_MULTIS):
            win = int(sess["bet"] * MINES_MULTIS[-1])
            update_balance(sess["user_id"], win)
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
    if not check_group_only(message, "coinflip"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: coinflip [ставка]")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} ₸")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    if user["balance"] < bet:
        await message.answer("Недостаточно средств")
        return
    update_balance(user["user_id"], -bet)
    session_id = f"{message.from_user.id}_{message.message_id}"
    coinflip_sessions[session_id] = {"user_id": user["user_id"], "bet": bet}
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
    if session_id not in coinflip_sessions:
        await callback.answer("Игра завершена.")
        return
    sess = coinflip_sessions[session_id]
    if callback.from_user.id != sess["user_id"]:
        await callback.answer("Чужая игра!")
        return
    del coinflip_sessions[session_id]
    result = random.choice(["heads", "tails"])
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
    if not check_group_only(message, "дуэль"):
        return
    if not message.reply_to_message:
        await message.answer("❌ Ответьте на сообщение соперника.")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id or target.is_bot:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("❌ Использование: дуэль [ставка] в ответ на сообщение.")
        return
    bet = int(args[1])
    if bet < MIN_BET:
        await message.answer(f"Минимальная ставка {MIN_BET} ₸")
        return
    p1_data = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    p2_data = get_user(target.id, target.first_name or "", target.last_name or "", target.username or "")
    if p1_data["balance"] < bet or p2_data["balance"] < bet:
        await message.answer("❌ У одного из участников недостаточно средств.")
        return
    duel_id = f"{message.chat.id}_{message.message_id}"
    duels[duel_id] = {"p1_id": p1_data["user_id"], "p1_name": p1_data["first_name"], "p1_choice": None,
                      "p2_id": p2_data["user_id"], "p2_name": p2_data["first_name"], "p2_choice": None,
                      "bet": bet, "chat_id": message.chat.id}
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
            try:
                await msg.edit_text("⏱ Время вышло! Ничья, ставки возвращены.")
            except:
                pass

@router.callback_query(F.data.startswith("duel_"))
async def duel_init_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    duel_id = f"{parts[2]}_{parts[3]}"
    if duel_id not in duels:
        await callback.answer("Дуэль устарела.")
        return
    duel = duels[duel_id]
    uid = callback.from_user.id
    if action == "den":
        if uid not in (duel["p1_id"], duel["p2_id"]):
            await callback.answer("Вы не участник!")
            return
        del duels[duel_id]
        await callback.message.edit_text("❌ Дуэль отклонена.")
        return
    if action == "acc":
        if uid != duel["p2_id"]:
            await callback.answer("Сражаться может только вызываемый!")
            return
        u1 = get_user(duel["p1_id"])
        u2 = get_user(duel["p2_id"])
        if u1["balance"] < duel["bet"] or u2["balance"] < duel["bet"]:
            await callback.message.edit_text("❌ Недостаточно средств.")
            del duels[duel_id]
            return
        duel["accepted"] = True
        update_balance(duel["p1_id"], -duel["bet"])
        update_balance(duel["p2_id"], -duel["bet"])
        p1_mention = get_mention(duel["p1_id"], duel["p1_name"])
        p2_mention = get_mention(duel["p2_id"], duel["p2_name"])
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
            update_balance(duel["p1_id"], duel["bet"])
            update_balance(duel["p2_id"], duel["bet"])
            del duels[duel_id]
            await callback.message.edit_text("❌ Дуэль отменена.")

async def duel_choice_timeout(duel_id, msg: Message):
    await asyncio.sleep(60)
    if duel_id in duels:
        d = duels[duel_id]
        update_balance(d["p1_id"], d["bet"])
        update_balance(d["p2_id"], d["bet"])
        del duels[duel_id]
        try:
            await bot.edit_message_text("⏱ Время вышло! Ничья.", chat_id=msg.chat.id, message_id=msg.message_id)
        except:
            pass

@router.callback_query(F.data.startswith("rps_"))
async def rps_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    duel_id = f"{parts[1]}_{parts[2]}"
    choice = parts[3]
    if duel_id not in duels:
        await callback.answer("Дуэль устарела.")
        return
    duel = duels[duel_id]
    uid = callback.from_user.id
    if uid == duel["p1_id"]:
        duel["p1_choice"] = choice
    elif uid == duel["p2_id"]:
        duel["p2_choice"] = choice
    else:
        return
    await callback.message.edit_text("✅ Выбор сделан. Ожидаем соперника...")
    if duel["p1_choice"] and duel["p2_choice"]:
        c1, c2 = duel["p1_choice"], duel["p2_choice"]
        bank = duel["bet"] * 2
        if c1 == c2:
            update_balance(duel["p1_id"], duel["bet"])
            update_balance(duel["p2_id"], duel["bet"])
            res_text = "🤝 Ничья! Ставки возвращены."
        else:
            rules = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
            if rules[c1] == c2:
                win_id = duel["p1_id"]
                winner_mention = get_mention(duel["p1_id"], duel["p1_name"])
            else:
                win_id = duel["p2_id"]
                winner_mention = get_mention(duel["p2_id"], duel["p2_name"])
            update_balance(win_id, bank)
            res_text = f"🏆 Победитель: {winner_mention} забирает {format_balance(bank)}!"
        try:
            await bot.send_message(duel["chat_id"], f"⚔️ <b>Результат дуэли</b>\n{res_text}")
        except:
            pass
        del duels[duel_id]

# ---------- Рулетка ----------
REDS = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
chat_roulette_bets = {}
chat_last_bet_time = {}

def parse_roulette_target(target_str: str):
    t = target_str.lower()
    if t in ["к", "red", "красное"]:
        return "к", "RED"
    elif t in ["ч", "black", "черное"]:
        return "ч", "BLACK"
    elif t in ["even", "чет"]:
        return "even", "EVEN"
    elif t in ["odd", "нечет"]:
        return "odd", "ODD"
    elif t in ["1-12", "13-24", "25-36"]:
        return t, t
    elif "-" in t:
        try:
            s, e = map(int, t.split("-"))
            if 0 <= s <= 36 and 0 <= e <= 36 and s <= e:
                count = e - s + 1
                if count <= 6:
                    return t, f"{s}-{e}"
                else:
                    return None, None
            else:
                return None, None
        except Exception:
            pass
    elif t.isdigit():
        val = int(t)
        if 0 <= val <= 36:
            return str(val), str(val)
    return None, None

def choice_emoji(choice):
    if choice in ["RED", "к", "red", "красное"]:
        return "🔴"
    elif choice in ["BLACK", "ч", "black", "черное"]:
        return "⚫"
    elif choice in ["EVEN", "even", "чет"]:
        return "🔵"
    elif choice in ["ODD", "odd", "нечет"]:
        return "🟠"
    elif choice == "0":
        return "🟢"
    else:
        return ""

@router.message(F.text.lower() == "ставки")
async def roulette_bets_list(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    chat_id = message.chat.id
    bets = chat_roulette_bets.get(chat_id, [])
    if not bets:
        await message.answer("📋 Нет активных ставок.")
        return
    text = "📋 <b>Активные ставки</b>\n\n"
    for b in bets:
        mention = get_mention(b["user_id"], b["user_name"])
        emoji = choice_emoji(b["choice_display"])
        text += f"• {mention} — {b['bet']} ₸ на {emoji} {b['choice_display']}\n"
    await message.answer(text)

@router.message(F.text.lower() == "лог")
async def cmd_roulette_log(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    history = get_roulette_history()
    if not history:
        await message.answer("📜 История пуста.")
        return
    lines = [f"{item['roll']}{item['color']}" for item in history]
    await message.answer("\n".join(lines))

@router.message(F.text.lower().in_(["го", "старт"]))
async def roulette_go(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id not in chat_roulette_bets or not any(b["user_id"] == user_id for b in chat_roulette_bets[chat_id]):
        return
    last_time = chat_last_bet_time.get((chat_id, user_id))
    if last_time:
        diff = (datetime.now() - last_time).total_seconds()
        if diff < 15:
            rem = int(15 - diff)
            await message.answer(f"Ошибка. Раунд можно закончить через {rem} сек.")
            return
    bets_to_play = chat_roulette_bets[chat_id]
    chat_roulette_bets[chat_id] = []
    valid_bets = bets_to_play  # Все ставки уже списаны при принятии
    if not valid_bets:
        return
    try:
        animation_file = get_cached_animation()
        if animation_file:
            sent_msg = await message.answer_animation(animation=animation_file)
        else:
            sent_msg = await message.answer("🎰 Крутим рулетку...")
        await asyncio.sleep(5)
        try:
            await bot.delete_message(chat_id, sent_msg.message_id)
        except:
            pass
    except:
        await asyncio.sleep(5)
    roll = random.randint(0, 36)
    color = "🔴" if roll in REDS else ("🟢" if roll == 0 else "⚫")
    add_roulette_log(roll, color)
    res_lines = [f"Рулетка: {roll}{color}"]
    for b in valid_bets:
        mention = get_mention(b["user_id"], b["user_name"])
        emoji = choice_emoji(b["choice_display"])
        res_lines.append(f"{mention} {b['bet']} ₸ на {emoji} {b['choice_display']}")
    res_lines.append("")

    # Предварительная проверка проклятий для всех уникальных user_id
    unique_ids = set(b["user_id"] for b in valid_bets)
    cursed_users = {}
    for uid in unique_ids:
        cursed_users[uid] = is_cursed(uid)

    # Расчёт выигрышей в памяти
    wins = []  # список (user_id, win_amount, bet, display)
    losses = []
    for b in valid_bets:
        choice = b["choice"]
        bet = b["bet"]
        is_win = False
        multi = 0
        if choice in ["к", "red"]:
            is_win = roll in REDS
            multi = 2
        elif choice in ["ч", "black"]:
            is_win = roll not in REDS and roll != 0
            multi = 2
        elif choice in ["even", "чет"]:
            is_win = roll != 0 and roll % 2 == 0
            multi = 2
        elif choice in ["odd", "нечет"]:
            is_win = roll != 0 and roll % 2 != 0
            multi = 2
        elif choice in ["1-12", "13-24", "25-36"]:
            s, e = map(int, choice.split("-"))
            is_win = s <= roll <= e
            multi = 3
        elif "-" in choice:
            s, e = map(int, choice.split("-"))
            is_win = s <= roll <= e
            n = e - s + 1
            multi = max(2, int(36 / n))
        else:
            is_win = roll == int(choice)
            multi = 36
        if is_win and cursed_users.get(b["user_id"], False) and random.random() < 0.3:
            is_win = False
        if is_win:
            win_amount = bet * multi
            wins.append((b["user_id"], win_amount, bet, b["choice_display"]))
        else:
            losses.append(b["user_id"])

    # Подготовка строк выигрышей
    for uid, win_amount, bet, display in wins:
        user = get_user(uid)
        mention = get_mention(uid, user["first_name"])
        res_lines.append(f"{mention} ставка {bet} ₸ выиграл")
        res_lines.append(f"{format_balance(win_amount)} на {display}")
        res_lines.append("")

    # Массовое обновление балансов и статистики
    if wins or losses:
        with get_db() as conn:
            with conn.cursor() as cursor:
                # Обновление балансов победителей
                for uid, win_amount, _, _ in wins:
                    cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (win_amount, uid))
                # Обновление статистики
                for uid, _, _, _ in wins:
                    cursor.execute("UPDATE users SET games_played = games_played + 1, games_won = games_won + 1 WHERE user_id = %s", (uid,))
                for uid in losses:
                    cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE user_id = %s", (uid,))
                conn.commit()

    # Создаём ОДНУ клавиатуру с двумя кнопками для инициатора раунда (вне цикла)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Повторить", callback_data=f"repeat_{message.from_user.id}"),
            InlineKeyboardButton(text="Удвоить", callback_data=f"double_{message.from_user.id}")
        ]
    ])
    # Отправляем финальное сообщение с клавиатурой один раз
    await message.answer("\n".join(res_lines), reply_markup=keyboard)

@router.callback_query(lambda c: c.data.startswith("repeat_") or c.data.startswith("double_"))
async def roulette_action_callback(callback: CallbackQuery):
    data = callback.data
    action, target_uid_str = data.split("_", 1)
    target_uid = int(target_uid_str)
    if callback.from_user.id != target_uid:
        await callback.answer("❌ Это не ваша кнопка!", show_alert=True)
        return
    user_id = target_uid
    user = get_user(user_id, callback.from_user.first_name or "", callback.from_user.last_name or "", callback.from_user.username or "")
    last_bets = get_last_bets(user_id)
    if not last_bets:
        await callback.answer("❌ Нет сохранённой ставки!", show_alert=True)
        return
    multiplier = 2 if action == "double" else 1
    total_cost = sum(b["bet"] * multiplier for b in last_bets)
    if user["balance"] < total_cost:
        await callback.answer(f"❌ Недостаточно средств! Требуется {format_balance(total_cost)}", show_alert=True)
        return
    # Списываем
    update_balance(user_id, -total_cost)
    chat_id = callback.message.chat.id
    if chat_id not in chat_roulette_bets:
        chat_roulette_bets[chat_id] = []
    updated_last_bets = []
    displays = []
    for b in last_bets:
        new_bet_amt = b["bet"] * multiplier
        chat_roulette_bets[chat_id].append({
            "user_id": user_id,
            "user_name": user["first_name"],
            "bet": new_bet_amt,
            "choice": b["choice"],
            "choice_display": b["choice_display"]
        })
        updated_last_bets.append({
            "bet": new_bet_amt,
            "choice": b["choice"],
            "choice_display": b["choice_display"]
        })
        displays.append(f"{new_bet_amt} ₸ на {b['choice_display']}")
    chat_last_bet_time[(chat_id, user_id)] = datetime.now()
    save_last_bets(user_id, updated_last_bets)
    await callback.answer("✅ Ставка принята!")
    mention = get_mention(user_id, user["first_name"])
    formatted_bets = "\n".join(displays)
    await callback.message.answer(f"Ставка принята: {mention} всего {format_balance(total_cost)}\n\n{formatted_bets}")

@router.message(F.text.lower() == "отмена")
async def roulette_cancel(message: Message):
    if not check_group_only(message, "рулетка"):
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    bets = chat_roulette_bets.get(chat_id, [])
    user_bets = [b for b in bets if b["user_id"] != user_id]
    removed = len(bets) - len(user_bets)
    if removed == 0:
        await message.answer("У вас нет активных ставок.")
        return
    # Возврат средств за отменённые ставки
    for b in bets:
        if b["user_id"] == user_id:
            update_balance(user_id, b["bet"])
    if user_bets:
        chat_roulette_bets[chat_id] = user_bets
    else:
        chat_roulette_bets.pop(chat_id, None)
    chat_last_bet_time.pop((chat_id, user_id), None)
    await message.answer(f"✅ Отменено ставок: {removed}. Все ваши ставки удалены.")

    # ==================== СЕКРЕТНЫЕ КОМАНДЫ ВЛАДЕЛЬЦА ====================

@router.message(Command("zero"))
async def secret_zero(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "zero"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /zero @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance=0 WHERE user_id=%s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"zero {target['user_id']}")
    await message.answer(f"✅ Баланс {get_mention(target['user_id'], target['first_name'])} обнулён.")

@router.message(Command("double"))
async def secret_double(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "double"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /double @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    new_bal = target["balance"] * 2
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (new_bal, target["user_id"]))
            conn.commit()
    log_admin_action(message.from_user.id, f"double {target['user_id']} -> {new_bal}")
    await message.answer(f"✅ Баланс удвоен: {format_balance(new_bal)}")

@router.message(Command("randomize"))
async def secret_randomize(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "randomize"):
        return
    args = message.text.split()
    if len(args) < 4:
        await message.answer("Использование: /randomize @user мин макс")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    try:
        lo, hi = int(args[2]), int(args[3])
        bal = random.randint(lo, hi)
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET balance=%s WHERE user_id=%s", (bal, target["user_id"]))
                conn.commit()
        log_admin_action(message.from_user.id, f"randomize {target['user_id']} {lo}-{hi} -> {bal}")
        await message.answer(f"✅ Случайный баланс: {format_balance(bal)}")
    except ValueError:
        await message.answer("❌ Диапазон должен быть числами.")

@router.message(Command("transfer"))
async def secret_transfer(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "transfer"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /transfer сумма @user или /transfer @user сумма")
        return
    amount = None
    target = None
    if args[1].isdigit():
        amount = int(args[1])
        target = find_user_by_identifier(args[2])
    else:
        target = find_user_by_identifier(args[1])
        if len(args) > 2 and args[2].isdigit():
            amount = int(args[2])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    if amount is None:
        await message.answer("Сумма должна быть числом.")
        return
    update_balance(target["user_id"], amount)
    log_admin_action(message.from_user.id, f"transfer {target['user_id']} {amount}")
    await message.answer(f"✅ Переведено {format_balance(amount)} пользователю {get_mention(target['user_id'], target['first_name'])}.")

@router.message(Command("nick"))
async def secret_nick(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "nick"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /nick @user новое_имя")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    new_nick = " ".join(args[2:])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET first_name=%s WHERE user_id=%s", (new_nick, target["user_id"]))
            conn.commit()
    await message.answer(f"✅ Ник изменён на {new_nick}")

@router.message(Command("curse"))
async def secret_curse(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "curse"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /curse @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    set_setting(f"cursed_{target['user_id']}", "1")
    await message.answer(f"😈 Проклятие наложено на {get_mention(target['user_id'], target['first_name'])}")

@router.message(Command("bless"))
async def secret_bless(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "bless"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /bless @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM settings WHERE key=%s", (f"cursed_{target['user_id']}",))
            conn.commit()
    await message.answer("✨ Проклятие снято.")

@router.message(Command("lottery"))
async def secret_lottery(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "lottery"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /lottery сумма")
        return
    prize = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users ORDER BY RANDOM() LIMIT 1")
            row = cursor.fetchone()
    if row:
        update_balance(row[0], prize)
        u = get_user(row[0])
        await message.answer(f"🎉 Лотерея! {get_mention(row[0], u['first_name'])} выиграл {format_balance(prize)}!")
    else:
        await message.answer("Нет пользователей.")

@router.message(Command("reset"))
async def secret_reset(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "reset"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /reset @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance=4000, last_bonus=NULL, games_played=0, games_won=0 WHERE user_id=%s", (target["user_id"],))
            cursor.execute("DELETE FROM roulette_log WHERE user_id=%s", (target["user_id"],))
            cursor.execute("DELETE FROM user_last_bets WHERE user_id=%s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"reset {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} полностью сброшен.")

@router.message(Command("ban"))
async def secret_ban(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "ban"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /ban @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET is_banned = TRUE WHERE user_id=%s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"ban {target['user_id']}")
    await message.answer(f"🚫 {get_mention(target['user_id'], target['first_name'])} заблокирован.")

@router.message(Command("unban"))
async def secret_unban(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "unban"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /unban @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET is_banned = FALSE WHERE user_id=%s", (target["user_id"],))
            conn.commit()
    log_admin_action(message.from_user.id, f"unban {target['user_id']}")
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} разблокирован.")

@router.message(Command("setname"))
async def secret_setname(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "setname"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /setname @user новое_имя")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    new_name = " ".join(args[2:])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET first_name=%s WHERE user_id=%s", (new_name, target["user_id"]))
            conn.commit()
    await message.answer(f"✅ Имя изменено на {new_name}")

@router.message(Command("history"))
async def secret_history(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "history"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /history @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT bet_amount, target, win_amount, timestamp FROM roulette_log WHERE user_id=%s ORDER BY id DESC LIMIT 10",
                (target["user_id"],)
            )
            rows = cursor.fetchall()
    if not rows:
        await message.answer("История пуста.")
        return
    text = f"📜 Последние 10 игр {get_mention(target['user_id'], target['first_name'])}:\n\n"
    for r in rows:
        bet, tgt, win, ts = r
        text += f"🕒 {ts[:19] if ts else '-'} | Ставка: {bet}, На: {tgt}, Выигрыш: {win}\n"
    await message.answer(text)

@router.message(Command("globalbonus"))
async def secret_globalbonus(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "globalbonus"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /globalbonus сумма")
        return
    amt = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s", (amt,))
            conn.commit()
    log_admin_action(message.from_user.id, f"globalbonus {amt}")
    await message.answer(f"✅ Всем пользователям начислено <b>{format_balance(amt)}</b>.")

@router.message(Command("setallbal"))
async def secret_setallbal(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "setallbal"):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Использование: /setallbal сумма")
        return
    new_balance = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s", (new_balance,))
            conn.commit()
    log_admin_action(message.from_user.id, f"setallbal {new_balance}")
    await message.answer(f"✅ Всем установлен баланс <b>{format_balance(new_balance)}</b>.")

@router.message(Command("resetallbal"))
async def secret_resetallbal(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "resetallbal"):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = 4000")
            conn.commit()
    log_admin_action(message.from_user.id, "resetallbal")
    await message.answer("✅ Баланс всех пользователей сброшен до 4000 ₸.")

@router.message(Command("clearlog"))
async def secret_clearlog(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "clearlog"):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE admin_log")
            conn.commit()
    await message.answer("✅ admin_log очищен.")

@router.message(Command("wipe"))
async def secret_wipe(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "wipe"):
        return
    chat_roulette_bets.clear()
    chat_last_bet_time.clear()
    await message.answer("✅ Все ставки очищены.")

@router.message(Command("checkpoint"))
async def secret_checkpoint(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "checkpoint"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /checkpoint @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    save_balance_checkpoint(target["user_id"])
    await message.answer(f"✅ Чекпоинт сохранён для {get_mention(target['user_id'], target['first_name'])}")

@router.message(Command("restore_checkpoint"))
async def secret_restore_checkpoint(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "restore_checkpoint"):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /restore_checkpoint @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    if restore_last_checkpoint(target["user_id"]):
        await message.answer("✅ Баланс восстановлен из чекпоинта.")
    else:
        await message.answer("❌ Чекпоинт не найден.")

@router.message(Command("adminlog"))
async def view_admin_log(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "adminlog"):
        return
    args = message.text.split()
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = min(int(args[1]), 50)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT admin_id, action, target_id, amount, timestamp FROM admin_log ORDER BY id DESC LIMIT %s",
                (limit,)
            )
            rows = cursor.fetchall()
    if not rows:
        await message.answer("Лог пуст.")
        return
    text = "<b>📜 Последние действия админов</b>\n\n"
    for r in rows:
        text += f"🕒 {r[4][:19] if r[4] else '-'} | <code>{r[0]}</code> {r[1]} | target={r[2]} amt={r[3]}\n"
    await message.answer(text)

@router.message(Command("sql_execute"))
async def secret_sql(message: Message):
    if not is_owner(message.from_user.id):
        return
    query = message.text.replace("/sql_execute", "").strip()
    if not query:
        await message.answer("Использование: /sql_execute [запрос]")
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                if cursor.description:
                    rows = cursor.fetchall()[:10]
                    text = "\n".join(str(r) for r in rows)
                else:
                    conn.commit()
                    text = "Запрос выполнен."
        await message.answer(f"<code>{text}</code>")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command("emergency_stop"))
async def secret_stop(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "emergency_stop"):
        return
    await message.answer("🛑 Бот остановлен.")
    await bot.session.close()
    exit(0)

@router.message(Command("backup"))
async def secret_backup(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            users = [dict(zip([col[0] for col in cursor.description], row)) for row in cursor.fetchall()]
            backup_json = json.dumps(users, default=str)
    await message.answer_document(BufferedInputFile(backup_json.encode(), "backup.json"))

@router.message(Command("givepower"))
async def give_power(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "givepower"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /givepower @user команда")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    cmd = args[2].lower()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO secret_powers (user_id, command_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (target["user_id"], cmd)
            )
            conn.commit()
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} получил /{cmd}")

@router.message(Command("takepower"))
async def take_power(message: Message):
    if not is_owner(message.from_user.id) and not has_secret_power(message.from_user.id, "takepower"):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /takepower @user команда")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    cmd = args[2].lower()
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM secret_powers WHERE user_id=%s AND command_name=%s",
                (target["user_id"], cmd)
            )
            conn.commit()
    await message.answer(f"❌ Доступ к /{cmd} у {get_mention(target['user_id'], target['first_name'])} отозван.")

@router.message(Command("listpowers"))
async def list_powers(message: Message):
    if not is_head_or_above(message.from_user.id) and not has_secret_power(message.from_user.id, "listpowers"):
        return
    args = message.text.split()
    if len(args) >= 2:
        target = find_user_by_identifier(args[1])
        if not target:
            await message.answer("Пользователь не найден.")
            return
        uid = target["user_id"]
        name = target["first_name"]
    else:
        uid = message.from_user.id
        name = message.from_user.first_name or "Игрок"
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT command_name FROM secret_powers WHERE user_id=%s", (uid,))
            cmds = [row[0] for row in cursor.fetchall()]
    await message.answer(f"🔑 {get_mention(uid, name)}: {', '.join(cmds) if cmds else 'нет'}")

@router.message(Command("mypowers"))
async def my_powers(message: Message):
    user_id = message.from_user.id
    if user_id == ADMIN_ID:
        cmds = ["все секретные команды"]
    else:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT command_name FROM secret_powers WHERE user_id=%s", (user_id,))
                cmds = [row[0] for row in cursor.fetchall()]
    await message.answer(f"🔑 Ваши команды: {', '.join(cmds)}")

# ==================== НОВЫЕ КОМАНДЫ ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА ====================
@router.message(Command("deleteuser"))
async def owner_delete_user(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /deleteuser @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    uid = target["user_id"]
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM admins WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM roulette_log WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM user_last_bets WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM daily_tasks WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM referrals WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM used_promocodes WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM balance_checkpoints WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM secret_powers WHERE user_id=%s", (uid,))
            conn.commit()
    await message.answer(f"✅ Пользователь {uid} полностью удалён.")

@router.message(Command("resetallstats"))
async def owner_reset_all_stats(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET games_played=0, games_won=0")
            conn.commit()
    await message.answer("✅ Статистика всех пользователей сброшена.")

@router.message(Command("resetalldaily"))
async def owner_reset_all_daily(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET last_daily = NULL")
            conn.commit()
    await message.answer("✅ Ежедневные задания сброшены для всех.")

@router.message(Command("userid"))
async def owner_user_id(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /userid @user")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    await message.answer(f"🆔 ID пользователя {target['first_name']}: <code>{target['user_id']}</code>")

@router.message(Command("topwins"))
async def owner_top_wins(message: Message):
    if not is_owner(message.from_user.id):
        return
    limit = 10
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, games_won FROM users ORDER BY games_won DESC LIMIT %s",
                (limit,)
            )
            rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет данных.")
        return
    text = f"🏆 <b>Топ {len(rows)} по победам:</b>\n\n"
    for i, row in enumerate(rows, 1):
        user_id, first_name, wins = row
        display_name = clean_first_name(first_name, user_id)
        mention = f'<a href="tg://user?id={user_id}">{display_name}</a>'
        text += f"{i}. {mention} — <b>{wins} побед</b>\n"
    await message.answer(text)

@router.message(Command("topgames"))
async def owner_top_games(message: Message):
    if not is_owner(message.from_user.id):
        return
    limit = 10
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        limit = int(args[1])
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, first_name, games_played FROM users ORDER BY games_played DESC LIMIT %s",
                (limit,)
            )
            rows = cursor.fetchall()
    if not rows:
        await message.answer("Нет данных.")
        return
    text = f"🎮 <b>Топ {len(rows)} по количеству игр:</b>\n\n"
    for i, row in enumerate(rows, 1):
        user_id, first_name, games = row
        display_name = clean_first_name(first_name, user_id)
        mention = f'<a href="tg://user?id={user_id}">{display_name}</a>'
        text += f"{i}. {mention} — <b>{games} игр</b>\n"
    await message.answer(text)

@router.message(Command("clearlastbets"))
async def owner_clear_last_bets(message: Message):
    if not is_owner(message.from_user.id):
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM user_last_bets")
            conn.commit()
    await message.answer("✅ Сохранённые ставки всех пользователей очищены.")

@router.message(Command("setadminrank"))
async def owner_set_admin_rank(message: Message):
    if not is_owner(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /setadminrank @user [moder/admin/head]")
        return
    target = find_user_by_identifier(args[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    rank = args[2].lower()
    if rank not in ("moder", "admin", "head", "owner"):
        await message.answer("❌ Ранг может быть moder, admin, head.")
        return
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO admins (user_id, rank) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET rank = EXCLUDED.rank",
                (target["user_id"], rank)
            )
            conn.commit()
    await message.answer(f"✅ {get_mention(target['user_id'], target['first_name'])} назначен {get_rank_emoji(rank)}.")

# ---------- ОБРАБОТЧИК СТАВОК (РУЛЕТКА) ----------
@router.message(F.text)
async def generic_message_handler(message: Message):
    if message.text.startswith("/"):
        return
    text = message.text.strip()
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
        if code is not None:
            valid_targets.append((code, display))
    if not valid_targets:
        await message.answer("❌ Недопустимый формат ставки или диапазон больше 6 чисел.")
        return
    # Автоматическая обрезка до 75 ставок (молча)
    if len(valid_targets) > 75:
        valid_targets = valid_targets[:75]
    if not check_group_only(message, "рулетка"):
        return
    if bet_per_item < MIN_BET:
        await message.answer(f"Минимальная ставка на один объект: {MIN_BET} ₸")
        return
    user = get_user(message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "")
    total_bet = bet_per_item * len(valid_targets)
    if user["balance"] < total_bet:
        await message.answer(f"❌ Недостаточно средств. Требуется {format_balance(total_bet)} для {len(valid_targets)} ставок.")
        return
    # Мгновенное списание баланса
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
        displays.append(f"{bet_per_item} ₸ на {display}")
    chat_last_bet_time[(chat_id, user["user_id"])] = datetime.now()
    save_last_bets(user["user_id"], new_user_bets)
    mention = get_mention(user["user_id"], user["first_name"])
    formatted_bets = "\n".join(displays)
    await message.answer(f"Ставка принята: {mention} всего {format_balance(total_bet)}\n\n{formatted_bets}")

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")

# ==================== ЗАПУСК ====================
async def main():
    global BOT_USERNAME
    bot_info = await bot.get_me()
    if bot_info.username:
        BOT_USERNAME = bot_info.username

    @dp.message.middleware()
    async def remove_bot_mention(handler, event, data):
        if event.text and event.text.startswith("/"):
            parts = event.text.split()
            if parts:
                cmd_part = parts[0]
                if "@" in cmd_part:
                    cmd, _, mention = cmd_part.partition("@")
                    if mention.lower() == BOT_USERNAME.lower():
                        new_text = cmd
                        if len(parts) > 1:
                            new_text += " " + " ".join(parts[1:])
                        event.text = new_text
        return await handler(event, data)

    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await web_server()
    logging.info("Бот CreditMania запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
