import os
import sys
import logging
import asyncio
import random
from datetime import datetime, timedelta
from psycopg2 import pool
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, 
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

# ==========================================
# ⚙️ НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) # Твой Telegram ID
PORT = int(os.environ.get("PORT", 10000))

START_BALANCE = 4000
REFERRAL_BONUS = 10000

# ==========================================
# 🗄 ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ (PostgreSQL)
# ==========================================
try:
    db_pool = pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
    if db_pool:
        logger.info("Успешное подключение к пулу БД PostgreSQL")
except Exception as e:
    logger.error(f"Ошибка подключения к БД: {e}")
    sys.exit(1)

def init_db():
    """Создает необходимые таблицы при запуске бота."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance BIGINT DEFAULT 4000,
                    nickname VARCHAR(255),
                    last_daily TIMESTAMP,
                    referrer_id BIGINT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS powers (
                    user_id BIGINT,
                    power_name VARCHAR(50),
                    PRIMARY KEY (user_id, power_name)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS promocodes (
                    code VARCHAR(50) PRIMARY KEY,
                    amount BIGINT,
                    activations_left INT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS used_promos (
                    user_id BIGINT,
                    code VARCHAR(50),
                    PRIMARY KEY (user_id, code)
                )
            """)
        conn.commit()
    finally:
        db_pool.putconn(conn)

init_db()

# Асинхронные обертки для работы с БД, чтобы не блокировать event loop
async def db_execute(query, params=(), fetchone=False, fetchall=False):
    def _sync_execute():
        conn = db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetchone:
                    res = cur.fetchone()
                    conn.commit()
                    return res
                if fetchall:
                    res = cur.fetchall()
                    conn.commit()
                    return res
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"DB Error: {e}")
            return None
        finally:
            db_pool.putconn(conn)
    return await asyncio.to_thread(_sync_execute)

async def check_user(user_id: int, username: str = None, referrer_id: int = None):
    """Проверяет наличие пользователя в БД, регистрирует при необходимости."""
    user = await db_execute("SELECT balance, nickname FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    if not user:
        nick = username if username else f"Player_{user_id}"
        await db_execute(
            "INSERT INTO users (user_id, balance, nickname, referrer_id) VALUES (%s, %s, %s, %s)",
            (user_id, START_BALANCE, nick, referrer_id)
        )
        if referrer_id and referrer_id != user_id:
            await db_execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (REFERRAL_BONUS, referrer_id))
            return True, True # Новый юзер, реферал зачислен
        return True, False # Просто новый юзер
    return False, False # Уже существует

async def get_balance(user_id: int):
    res = await db_execute("SELECT balance FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return res[0] if res else 0

async def update_balance(user_id: int, amount: int):
    await db_execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))

async def has_power(user_id: int, power_name: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    res = await db_execute("SELECT 1 FROM powers WHERE user_id = %s AND power_name = %s", (user_id, power_name), fetchone=True)
    return bool(res)

# ==========================================
# 🤖 ИНИЦИАЛИЗАЦИЯ БОТА И РОУТЕРОВ
# ==========================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# ==========================================
# 💰 ЭКОНОМИКА И БАЗОВЫЕ КОМАНДЫ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)
    
    is_new, ref_success = await check_user(message.from_user.id, message.from_user.first_name, ref_id)
    
    text = f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\nДобро пожаловать в игрового бота! Твой стартовый баланс: {START_BALANCE} CRD."
    if ref_success:
        text += f"\n🎉 Твой пригласитель получил бонус {REFERRAL_BONUS} CRD!"
        try:
            await bot.send_message(ref_id, f"🎉 По твоей ссылке зарегистрировался игрок! Начислено {REFERRAL_BONUS} CRD.")
        except:
            pass
    
    if not is_new:
        text = "С возвращением! Используй /help для списка команд."
        
    await message.answer(text)

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📚 <b>Команды бота:</b>\n\n"
        "<b>Экономика:</b>\n"
        "💳 /balance - Баланс\n"
        "🎁 /daily - Ежедневный бонус\n"
        "💸 /pay [реплай] [сумма] - Передать CRD\n"
        "🎟 /promo [код] - Ввести промокод\n\n"
        "<b>Игры:</b>\n"
        "🎰 /roulette [ставка] [цвет/число] - Рулетка\n"
        "🪙 /coinflip [ставка] [орел/решка] - Монетка\n"
        "🃏 /joker [ставка] - Джокер\n"
        "💣 /mines [ставка] [кол-во бомб] - Мины (Интерактив)\n"
        "⚔️ /duel [ставка] [реплай] - Дуэль (Камень-ножницы-бумага)\n\n"
        "<b>Админ / Powers:</b>\n"
        "👑 /givepower, /zero, /double, /curse, /bless, /nick, /globalbonus\n"
        "🖼 /change_avatar, /delete_avatar"
    )
    await message.answer(text)

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    await check_user(message.from_user.id, message.from_user.first_name)
    balance = await get_balance(message.from_user.id)
    await message.answer(f"💳 Твой баланс: <b>{balance} CRD</b>")

@router.message(Command("daily"))
async def cmd_daily(message: Message):
    user_id = message.from_user.id
    await check_user(user_id)
    res = await db_execute("SELECT last_daily FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    
    now = datetime.now()
    if res and res[0]:
        last_daily = res[0]
        if (now - last_daily).total_seconds() < 86400:
            next_time = last_daily + timedelta(days=1)
            await message.answer(f"⏳ Бонус уже получен! Следующий бонус будет доступен: {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
            return
            
    bonus = random.randint(500, 2000)
    await db_execute("UPDATE users SET balance = balance + %s, last_daily = %s WHERE user_id = %s", (bonus, now, user_id))
    await message.answer(f"🎁 Ты получил ежедневный бонус: <b>{bonus} CRD</b>!")

@router.message(Command("pay"))
async def cmd_pay(message: Message, command: CommandObject):
    if not message.reply_to_message:
        return await message.answer("⚠️ Сделай reply на сообщение игрока, которому хочешь перевести CRD.")
    if not command.args or not command.args.isdigit():
        return await message.answer("⚠️ Использование: /pay [сумма]")
        
    amount = int(command.args)
    if amount <= 0: return await message.answer("⚠️ Сумма должна быть больше 0.")
    
    sender_id = message.from_user.id
    receiver_id = message.reply_to_message.from_user.id
    
    if sender_id == receiver_id: return await message.answer("⚠️ Нельзя переводить самому себе.")
    
    await check_user(sender_id)
    await check_user(receiver_id, message.reply_to_message.from_user.first_name)
    
    sender_bal = await get_balance(sender_id)
    if sender_bal < amount:
        return await message.answer("⚠️ Недостаточно средств!")
        
    await update_balance(sender_id, -amount)
    await update_balance(receiver_id, amount)
    
    await message.answer(f"💸 Ты успешно перевел <b>{amount} CRD</b> пользователю {message.reply_to_message.from_user.first_name}!")

@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("⚠️ Укажи промокод. Пример: /promo CODE123")
    code = command.args.strip()
    user_id = message.from_user.id
    
    # Проверка использования
    used = await db_execute("SELECT 1 FROM used_promos WHERE user_id = %s AND code = %s", (user_id, code), fetchone=True)
    if used: return await message.answer("⚠️ Ты уже активировал этот промокод.")
    
    # Проверка валидности
    promo = await db_execute("SELECT amount, activations_left FROM promocodes WHERE code = %s", (code,), fetchone=True)
    if not promo: return await message.answer("⚠️ Промокод не найден.")
    amount, acts = promo[0], promo[1]
    
    if acts <= 0: return await message.answer("⚠️ Лимит активаций исчерпан.")
    
    # Активация
    await db_execute("UPDATE promocodes SET activations_left = activations_left - 1 WHERE code = %s", (code,))
    await db_execute("INSERT INTO used_promos (user_id, code) VALUES (%s, %s)", (user_id, code))
    await update_balance(user_id, amount)
    
    await message.answer(f"✅ Промокод активирован! Зачислено: <b>{amount} CRD</b>.")

@router.message(Command("createpromo"))
async def cmd_createpromo(message: Message, command: CommandObject):
    if not await has_power(message.from_user.id, "admin"):
        return await message.answer("⛔ У вас нет прав на создание промокодов.")
    
    try:
        args = command.args.split()
        code, amount, activations = args[0], int(args[1]), int(args[2])
        await db_execute("INSERT INTO promocodes (code, amount, activations_left) VALUES (%s, %s, %s)", (code, amount, activations))
        await message.answer(f"✅ Промокод <b>{code}</b> на {amount} CRD ({activations} активаций) создан!")
    except:
        await message.answer("⚠️ Использование: /createpromo [код] [сумма] [кол-во активаций]")

# ==========================================
# 🎮 ИГРЫ (Текстовые)
# ==========================================
@router.message(Command("coinflip"))
async def cmd_coinflip(message: Message, command: CommandObject):
    await check_user(message.from_user.id)
    try:
        args = command.args.split()
        bet = int(args[0])
        choice = args[1].lower()
        if choice not in ['орел', 'решка', 'heads', 'tails']: raise ValueError
    except:
        return await message.answer("⚠️ Использование: /coinflip [ставка] [орел/решка]")
        
    if bet <= 0: return await message.answer("⚠️ Ставка должна быть больше 0.")
    if await get_balance(message.from_user.id) < bet:
        return await message.answer("⚠️ Недостаточно средств!")
        
    await update_balance(message.from_user.id, -bet)
    
    is_heads_choice = choice in ['орел', 'heads']
    result_heads = random.choice([True, False])
    result_str = "Орел" if result_heads else "Решка"
    
    if is_heads_choice == result_heads:
        win = bet * 2
        await update_balance(message.from_user.id, win)
        await message.answer(f"🪙 Выпал {result_str}! Ты выиграл <b>{win} CRD</b>! 🎉")
    else:
        await message.answer(f"🪙 Выпал {result_str}. Ставка проиграна 😢")

@router.message(Command("roulette"))
async def cmd_roulette(message: Message, command: CommandObject):
    await check_user(message.from_user.id)
    try:
        args = command.args.split()
        bet = int(args[0])
        target = args[1].lower()
    except:
        return await message.answer("⚠️ Использование: /roulette [ставка] [красное/черное/зеленое/число 0-36]")
        
    if bet <= 0: return await message.answer("⚠️ Ставка должна быть больше 0.")
    if await get_balance(message.from_user.id) < bet:
        return await message.answer("⚠️ Недостаточно средств!")

    await update_balance(message.from_user.id, -bet)
    
    number = random.randint(0, 36)
    if number == 0: color = "зеленое"
    elif number in [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]: color = "красное"
    else: color = "черное"
    
    win = 0
    if target in ["красное", "red"] and color == "красное": win = bet * 2
    elif target in ["черное", "black"] and color == "черное": win = bet * 2
    elif target in ["зеленое", "green"] and color == "зеленое": win = bet * 14
    elif target.isdigit() and int(target) == number: win = bet * 36
    
    if win > 0:
        await update_balance(message.from_user.id, win)
        await message.answer(f"🎰 Выпало <b>{number} ({color})</b>!\n🎉 Ты выиграл <b>{win} CRD</b>!")
    else:
        await message.answer(f"🎰 Выпало <b>{number} ({color})</b>.\n😢 Ставка проиграна.")

@router.message(Command("joker"))
async def cmd_joker(message: Message, command: CommandObject):
    await check_user(message.from_user.id)
    try:
        bet = int(command.args)
    except:
        return await message.answer("⚠️ Использование: /joker [ставка]")
        
    if bet <= 0 or await get_balance(message.from_user.id) < bet:
        return await message.answer("⚠️ Неверная ставка или недостаточно средств!")
        
    await update_balance(message.from_user.id, -bet)
    cards = ["🃏 Джокер", "♠️ Туз", "♥️ Король", "♦️ Дама", "♣️ Валет", "7️⃣ Семерка", "☠️ Смерть"]
    weights = [5, 10, 15, 20, 20, 20, 10]
    
    result = random.choices(cards, weights=weights, k=3)
    res_str = " | ".join(result)
    
    win = 0
    if result.count("🃏 Джокер") == 3: win = bet * 50
    elif result.count("🃏 Джокер") == 2: win = bet * 5
    elif len(set(result)) == 1 and result[0] != "☠️ Смерть": win = bet * 10
    elif "☠️ Смерть" in result: win = 0
    elif len(set(result)) == 2: win = int(bet * 1.5)
    else: win = 0
    
    if win > 0:
        await update_balance(message.from_user.id, win)
        await message.answer(f"🎴 Карты: [ {res_str} ]\n🎉 Ты выиграл <b>{win} CRD</b>!")
    else:
        await message.answer(f"🎴 Карты: [ {res_str} ]\n😢 Проигрыш.")

# ==========================================
# 💣 ИГРА: МИНЫ (Интерактив на кнопках)
# ==========================================
MINES_GAMES = {} # Временное хранилище сессий мин

def get_mines_keyboard(game_id: str):
    game = MINES_GAMES[game_id]
    builder = []
    row = []
    for i in range(25):
        if i in game['opened']:
            btn_text = "💎" if i not in game['bombs'] else "💥"
        else:
            btn_text = "❓" if game['status'] == 'active' else ("💣" if i in game['bombs'] else "💎")
            
        cb_data = f"mines_open:{game_id}:{i}" if game['status'] == 'active' else "ignore"
        row.append(InlineKeyboardButton(text=btn_text, callback_data=cb_data))
        if len(row) == 5:
            builder.append(row)
            row = []
            
    kb = InlineKeyboardMarkup(inline_keyboard=builder)
    if game['status'] == 'active' and game['opened']:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"💰 Забрать ({game['current_win']})", callback_data=f"mines_take:{game_id}")])
    return kb

@router.message(Command("mines"))
async def cmd_mines(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await check_user(user_id)
    
    try:
        args = command.args.split()
        bet = int(args[0])
        bombs = int(args[1])
        if bombs < 1 or bombs > 24: raise ValueError
    except:
        return await message.answer("⚠️ Использование: /mines [ставка] [кол-во бомб (1-24)]")
        
    if bet <= 0 or await get_balance(user_id) < bet:
        return await message.answer("⚠️ Неверная ставка или недостаточно средств!")
        
    await update_balance(user_id, -bet)
    
    game_id = f"m_{user_id}_{int(datetime.now().timestamp())}"
    bomb_positions = random.sample(range(25), bombs)
    
    MINES_GAMES[game_id] = {
        'user_id': user_id,
        'bet': bet,
        'bombs': bomb_positions,
        'opened': [],
        'status': 'active',
        'current_win': bet
    }
    
    kb = get_mines_keyboard(game_id)
    await message.answer(f"💣 <b>Мины</b>\nСтавка: {bet} CRD\nБомб: {bombs}", reply_markup=kb)

@router.callback_query(F.data.startswith("mines_open:"))
async def cb_mines_open(callback: CallbackQuery):
    data = callback.data.split(":")
    game_id, pos = data[1], int(data[2])
    
    if game_id not in MINES_GAMES:
        return await callback.answer("Игра устарела.", show_alert=True)
    
    game = MINES_GAMES[game_id]
    if callback.from_user.id != game['user_id']:
        return await callback.answer("Это не твоя игра!", show_alert=True)
        
    if pos in game['opened']:
        return await callback.answer("Уже открыто!")
        
    game['opened'].append(pos)
    
    if pos in game['bombs']:
        game['status'] = 'lost'
        kb = get_mines_keyboard(game_id)
        await callback.message.edit_text(f"💥 БУМ! Ты подорвался на мине.\nПроиграно: {game['bet']} CRD.", reply_markup=kb)
        del MINES_GAMES[game_id]
    else:
        # Расчет множителя (упрощенно)
        multiplier = 1.0 + (len(game['opened']) * (len(game['bombs']) * 0.1))
        game['current_win'] = int(game['bet'] * multiplier)
        
        # Если открыл все алмазы
        if len(game['opened']) == 25 - len(game['bombs']):
            game['status'] = 'won'
            await update_balance(game['user_id'], game['current_win'])
            kb = get_mines_keyboard(game_id)
            await callback.message.edit_text(f"🏆 Невероятно! Ты нашел все алмазы!\nВыигрыш: <b>{game['current_win']} CRD</b>!", reply_markup=kb)
            del MINES_GAMES[game_id]
        else:
            kb = get_mines_keyboard(game_id)
            await callback.message.edit_reply_markup(reply_markup=kb)

@router.callback_query(F.data.startswith("mines_take:"))
async def cb_mines_take(callback: CallbackQuery):
    game_id = callback.data.split(":")[1]
    if game_id not in MINES_GAMES: return await callback.answer("Игра устарела.")
    game = MINES_GAMES[game_id]
    if callback.from_user.id != game['user_id']: return await callback.answer("Не твоя игра.")
    
    game['status'] = 'won'
    await update_balance(game['user_id'], game['current_win'])
    kb = get_mines_keyboard(game_id)
    await callback.message.edit_text(f"💰 Ты забрал деньги!\nВыигрыш: <b>{game['current_win']} CRD</b>", reply_markup=kb)
    del MINES_GAMES[game_id]

# ==========================================
# ⚔️ ДУЭЛИ (Камень-Ножницы-Бумага)
# ==========================================
DUELS = {}

@router.message(Command("duel"))
async def cmd_duel(message: Message, command: CommandObject):
    if not message.reply_to_message:
        return await message.answer("⚠️ Сделай reply на сообщение того, с кем хочешь сразиться!")
        
    try:
        bet = int(command.args)
    except:
        return await message.answer("⚠️ Укажи ставку: /duel [ставка]")
        
    p1 = message.from_user.id
    p2 = message.reply_to_message.from_user.id
    
    if p1 == p2: return await message.answer("⚠️ Нельзя играть с самим собой.")
    if bet <= 0: return await message.answer("⚠️ Ставка должна быть > 0.")
    
    await check_user(p1)
    await check_user(p2, message.reply_to_message.from_user.first_name)
    
    if await get_balance(p1) < bet: return await message.answer("⚠️ У тебя недостаточно средств.")
    if await get_balance(p2) < bet: return await message.answer("⚠️ У противника недостаточно средств.")
    
    duel_id = f"d_{p1}_{p2}_{int(datetime.now().timestamp())}"
    DUELS[duel_id] = {
        'p1': p1, 'p2': p2,
        'p1_name': message.from_user.first_name,
        'p2_name': message.reply_to_message.from_user.first_name,
        'bet': bet,
        'p1_choice': None,
        'p2_choice': None,
        'status': 'waiting_accept'
    }
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять вызов", callback_data=f"duel_accept:{duel_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel_decline:{duel_id}")
    ]])
    await message.answer(f"⚔️ {message.reply_to_message.from_user.first_name}, тебя вызвали на дуэль!\nСтавка: {bet} CRD.", reply_markup=kb)

def get_rps_keyboard(duel_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✊ Камень", callback_data=f"rps:{duel_id}:rock"),
        InlineKeyboardButton(text="✌️ Ножницы", callback_data=f"rps:{duel_id}:scissors"),
        InlineKeyboardButton(text="✋ Бумага", callback_data=f"rps:{duel_id}:paper")
    ]])

@router.callback_query(F.data.startswith("duel_"))
async def cb_duel_action(callback: CallbackQuery):
    action, duel_id = callback.data.split(":")[0], callback.data.split(":")[1]
    if duel_id not in DUELS: return await callback.answer("Дуэль устарела.", show_alert=True)
    
    duel = DUELS[duel_id]
    if action == "duel_accept":
        if callback.from_user.id != duel['p2']: return await callback.answer("Это вызов не для тебя!")
        
        # Списываем ставки
        if await get_balance(duel['p1']) < duel['bet'] or await get_balance(duel['p2']) < duel['bet']:
            del DUELS[duel_id]
            return await callback.message.edit_text("⚠️ У одного из игроков недостаточно средств. Дуэль отменена.")
            
        await update_balance(duel['p1'], -duel['bet'])
        await update_balance(duel['p2'], -duel['bet'])
        
        duel['status'] = 'playing'
        kb = get_rps_keyboard(duel_id)
        await callback.message.edit_text(f"⚔️ Дуэль началась! Ставка: {duel['bet']} CRD.\nИгроки, делайте ваш выбор!", reply_markup=kb)
        
    elif action == "duel_decline":
        if callback.from_user.id != duel['p2']: return await callback.answer("Это вызов не для тебя!")
        await callback.message.edit_text("❌ Вызов отклонен.")
        del DUELS[duel_id]

@router.callback_query(F.data.startswith("rps:"))
async def cb_rps(callback: CallbackQuery):
    _, duel_id, choice = callback.data.split(":")
    if duel_id not in DUELS: return await callback.answer("Дуэль завершена или устарела.")
    
    duel = DUELS[duel_id]
    user_id = callback.from_user.id
    
    if user_id not in [duel['p1'], duel['p2']]:
        return await callback.answer("Ты не участвуешь в дуэли!")
        
    if user_id == duel['p1'] and duel['p1_choice'] is None: duel['p1_choice'] = choice
    elif user_id == duel['p2'] and duel['p2_choice'] is None: duel['p2_choice'] = choice
    else: return await callback.answer("Ты уже сделал выбор!")
    
    await callback.answer("Выбор принят!")
    
    if duel['p1_choice'] and duel['p2_choice']:
        c1, c2 = duel['p1_choice'], duel['p2_choice']
        win_map = {'rock': 'scissors', 'scissors': 'paper', 'paper': 'rock'}
        tr = {'rock': '✊ Камень', 'scissors': '✌️ Ножницы', 'paper': '✋ Бумага'}
        
        pot = duel['bet'] * 2
        text = f"⚔️ <b>Результаты дуэли</b>\n\n{duel['p1_name']}: {tr[c1]}\n{duel['p2_name']}: {tr[c2]}\n\n"
        
        if c1 == c2:
            await update_balance(duel['p1'], duel['bet'])
            await update_balance(duel['p2'], duel['bet'])
            text += "🤝 <b>Ничья!</b> Ставки возвращены."
        elif win_map[c1] == c2:
            await update_balance(duel['p1'], pot)
            text += f"🏆 Победил <b>{duel['p1_name']}</b> и забрал {pot} CRD!"
        else:
            await update_balance(duel['p2'], pot)
            text += f"🏆 Победил <b>{duel['p2_name']}</b> и забрал {pot} CRD!"
            
        await callback.message.edit_text(text)
        del DUELS[duel_id]

# ==========================================
# 👑 POWERS & АДМИН-КОМАНДЫ
# ==========================================
@router.message(Command("givepower"))
async def cmd_givepower(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("⛔ Только создатель бота может выдавать права!")
    try:
        args = command.args.split()
        target_id = int(args[0])
        power_name = args[1].lower()
        await db_execute("INSERT INTO powers (user_id, power_name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (target_id, power_name))
        await message.answer(f"✅ Право <b>{power_name}</b> успешно выдано пользователю {target_id}.")
    except:
        await message.answer("⚠️ Использование: /givepower [user_id] [power_name]")

@router.message(Command("zero"))
async def cmd_zero(message: Message):
    if not await has_power(message.from_user.id, "zero"): return
    if not message.reply_to_message: return await message.answer("Сделай reply.")
    target = message.reply_to_message.from_user.id
    await db_execute("UPDATE users SET balance = 0 WHERE user_id = %s", (target,))
    await message.answer("💀 Баланс пользователя обнулен.")

@router.message(Command("double"))
async def cmd_double(message: Message):
    if not await has_power(message.from_user.id, "double"): return
    if not message.reply_to_message: return await message.answer("Сделай reply.")
    target = message.reply_to_message.from_user.id
    await db_execute("UPDATE users SET balance = balance * 2 WHERE user_id = %s", (target,))
    await message.answer("📈 Баланс пользователя удвоен.")

@router.message(Command("curse"))
async def cmd_curse(message: Message):
    if not await has_power(message.from_user.id, "curse"): return
    if not message.reply_to_message: return await message.answer("Сделай reply.")
    target = message.reply_to_message.from_user.id
    loss = random.randint(100, 5000)
    await update_balance(target, -loss)
    await message.answer(f"👿 Пользователь проклят и теряет <b>{loss} CRD</b>.")

@router.message(Command("bless"))
async def cmd_bless(message: Message):
    if not await has_power(message.from_user.id, "bless"): return
    if not message.reply_to_message: return await message.answer("Сделай reply.")
    target = message.reply_to_message.from_user.id
    gain = random.randint(1000, 10000)
    await update_balance(target, gain)
    await message.answer(f"👼 Пользователь благословлен и получает <b>{gain} CRD</b>.")

@router.message(Command("nick"))
async def cmd_nick(message: Message, command: CommandObject):
    if not await has_power(message.from_user.id, "nick"): return
    if not message.reply_to_message or not command.args: 
        return await message.answer("Сделай reply и укажи новый ник: /nick [имя]")
    target = message.reply_to_message.from_user.id
    new_nick = command.args[:50]
    await db_execute("UPDATE users SET nickname = %s WHERE user_id = %s", (new_nick, target))
    await message.answer(f"📝 Никнейм пользователя изменен на <b>{new_nick}</b>.")

@router.message(Command("globalbonus"))
async def cmd_globalbonus(message: Message, command: CommandObject):
    if not await has_power(message.from_user.id, "globalbonus"): return
    try:
        amount = int(command.args)
        await db_execute("UPDATE users SET balance = balance + %s", (amount,))
        await message.answer(f"🌍 <b>ГЛОБАЛЬНЫЙ БОНУС!</b> Всем игрокам начислено по <b>{amount} CRD</b>!")
    except:
        await message.answer("Укажи сумму: /globalbonus [сумма]")

# ==========================================
# 🖼 УПРАВЛЕНИЕ АВАТАРКАМИ ЧАТА
# ==========================================
@router.message(Command("change_avatar"))
async def cmd_change_avatar(message: Message, bot: Bot):
    if not await has_power(message.from_user.id, "admin"):
        return await message.answer("⛔ У вас нет прав администратора.")
    
    if not message.photo and not (message.reply_to_message and message.reply_to_message.photo):
        return await message.answer("⚠️ Прикрепите фото или сделайте reply на сообщение с фото.")
        
    photo = message.photo[-1] if message.photo else message.reply_to_message.photo[-1]
    
    try:
        file = await bot.get_file(photo.file_id)
        file_path = f"avatar_{message.chat.id}.jpg"
        await bot.download_file(file.file_path, file_path)
        
        photo_input = FSInputFile(file_path)
        await bot.set_chat_photo(chat_id=message.chat.id, photo=photo_input)
        
        os.remove(file_path) # Удаляем временный файл
        await message.answer("🖼 Аватарка чата успешно обновлена!")
    except TelegramBadRequest as e:
        await message.answer(f"⚠️ Ошибка. Убедитесь, что у бота есть права на изменение профиля группы.\nДетали: {e}")
    except Exception as e:
        logger.error(f"Avatar change error: {e}")
        await message.answer("⚠️ Произошла ошибка при смене аватарки.")

@router.message(Command("delete_avatar"))
async def cmd_delete_avatar(message: Message, bot: Bot):
    if not await has_power(message.from_user.id, "admin"):
        return await message.answer("⛔ У вас нет прав администратора.")
        
    try:
        await bot.delete_chat_photo(chat_id=message.chat.id)
        await message.answer("🗑 Аватарка чата удалена.")
    except TelegramBadRequest:
        await message.answer("⚠️ Ошибка. У бота нет прав или аватарка уже отсутствует.")

# ==========================================
# 🌐 WEB-СЕРВЕР ДЛЯ RENDER (Заглушка)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Bot is alive and kicking! 🚀")

async def run_web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Dummy Web Server started on port {PORT}")

# ==========================================
# 🚀 ЗАПУСК БОТА
# ==========================================
async def main():
    dp.include_router(router)
    
    # Запускаем веб-сервер и бота параллельно
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
