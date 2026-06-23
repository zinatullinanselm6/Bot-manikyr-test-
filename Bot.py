import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
import pytz
import os
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, URLInputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")
TZ_MOSCOW = pytz.timezone('Europe/Moscow')

# Кэш администраторов в оперативной памяти
ADMINS = set()

# Ссылка на баннер салона
MAIN_MENU_PHOTO = "https://i.postimg.cc/rsh5828D/IMG-20260623-193407.png"

from aiogram.client.default import DefaultBotProperties

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- БАЗА ДАННЫХ ---
async def init_db():
    global ADMINS
    async with aiosqlite.connect('salon.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                master TEXT,
                date TEXT,
                time TEXT,
                contact_info TEXT,
                remind_24 INTEGER DEFAULT 0,
                remind_6 INTEGER DEFAULT 0,
                remind_2 INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS blocked_times (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master TEXT,
                date TEXT,
                time TEXT
            )
        ''')
        await db.commit()
        
        async with db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            ADMINS = {row[0] for row in rows}

# --- СОСТОЯНИЯ (FSM) ---
class BookingState(StatesGroup):
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    entering_contact = State()
    confirming = State()

class AdminState(StatesGroup):
    choosing_master = State()
    choosing_date = State()
    # Блокировка
    block_master = State()
    block_date = State()
    block_time = State()
    # Рассылка
    broadcast_text = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_moscow_time():
    return datetime.now(TZ_MOSCOW)

def get_date_buttons():
    now = get_moscow_time()
    months = ["", "Января", "Февраля", "Марта", "Апреля", "Мая", "Июня", "Июля", "Августа", "Сентября", "Октября", "Ноября", "Декабря"]
    dates = []
    
    dates.append(("Сегодня", now.strftime("%Y-%m-%d")))
    dates.append(("Завтра", (now + timedelta(days=1)).strftime("%Y-%m-%d")))
    
    for i in range(2, 6):
        future_date = now + timedelta(days=i)
        btn_text = f"{future_date.day} {months[future_date.month]}"
        dates.append((btn_text, future_date.strftime("%Y-%m-%d")))
        
    return dates

# --- КЛАВИАТУРЫ КЛИЕНТА ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться 🖊️", callback_data="menu_book")],
        [InlineKeyboardButton(text="Мои записи 🟢", callback_data="menu_my_bookings")],
        [InlineKeyboardButton(text="Контакты 👥", callback_data="menu_contacts")],
        [InlineKeyboardButton(text="Наше портфолио 💄", callback_data="menu_portfolio")]
    ])

def masters_kb(prefix="book"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Настя", callback_data=f"{prefix}_master_Настя"),
         InlineKeyboardButton(text="Лера", callback_data=f"{prefix}_master_Лера")],
        [InlineKeyboardButton(text="Арина", callback_data=f"{prefix}_master_Арина")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main" if prefix == "book" else "admin_menu")]
    ])

def dates_kb(prefix="book"):
    dates = get_date_buttons()
    kb = []
    for i in range(0, len(dates), 2):
        row = [InlineKeyboardButton(text=dates[i][0], callback_data=f"{prefix}_date_{dates[i][1]}")]
        if i+1 < len(dates):
            row.append(InlineKeyboardButton(text=dates[i+1][0], callback_data=f"{prefix}_date_{dates[i+1][1]}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main" if prefix == "book" else "admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def times_kb(master, date):
    all_times = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]
    async with aiosqlite.connect('salon.db') as db:
        # Проверка записей
        async with db.execute("SELECT time FROM bookings WHERE master=? AND date=?", (master, date)) as cursor:
            booked_times = [row[0] for row in await cursor.fetchall()]
        
        # Проверка блокировок
        async with db.execute("SELECT time FROM blocked_times WHERE master=? AND date=?", (master, date)) as cursor:
            blocked_times = [row[0] for row in await cursor.fetchall()]

    if "ALL" in blocked_times:
        return None

    available_times = [t for t in all_times if t not in booked_times and t not in blocked_times]
    
    if not available_times:
        return None

    kb = []
    for i in range(0, len(available_times), 2):
        row = [InlineKeyboardButton(text=f"🟢 {available_times[i]}", callback_data=f"time_{available_times[i]}")]
        if i+1 < len(available_times):
            row.append(InlineKeyboardButton(text=f"🟢 {available_times[i+1]}", callback_data=f"time_{available_times[i+1]}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить ✅", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="Отклонить ❌", callback_data="confirm_no")]
    ])

# --- КЛАВИАТУРЫ АДМИНА ---
def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Просмотр записей", callback_data="admin_view")],
        [InlineKeyboardButton(text="⛔ Блокировка времени", callback_data="admin_block_start")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(text="⬅️ Выйти в клиентское меню", callback_data="back_to_main")]
    ])

def admin_records_kb(master, date, bookings):
    kb = []
    # Добавляем кнопку отмены для каждой записи
    for b in bookings:
        b_id = b[0]
        kb.append([InlineKeyboardButton(text=f"❌ Отменить Id{b_id}", callback_data=f"adm_can_{b_id}")])
        
    kb.append([InlineKeyboardButton(text="🔃 Обновить", callback_data=f"admin_refresh_{master}_{date}")])
    kb.append([InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def admin_block_times_kb(master, date):
    all_times = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT time FROM blocked_times WHERE master=? AND date=?", (master, date)) as cursor:
            blocked_times = [row[0] for row in await cursor.fetchall()]

    kb = []
    
    # Кнопка "Весь день"
    text_all = "⛔ РАЗБЛОКИРОВАТЬ ДЕНЬ" if "ALL" in blocked_times else "🛑 ЗАБЛОКИРОВАТЬ ВЕСЬ ДЕНЬ"
    kb.append([InlineKeyboardButton(text=text_all, callback_data="adm_blk_ALL")])

    if "ALL" not in blocked_times:
        for i in range(0, len(all_times), 2):
            t1 = all_times[i]
            t2 = all_times[i+1] if i+1 < len(all_times) else None
            
            btn1_text = f"⛔ {t1} (Снять)" if t1 in blocked_times else f"🟢 {t1}"
            row = [InlineKeyboardButton(text=btn1_text, callback_data=f"adm_blk_{t1}")]
            
            if t2:
                btn2_text = f"⛔ {t2} (Снять)" if t2 in blocked_times else f"🟢 {t2}"
                row.append(InlineKeyboardButton(text=btn2_text, callback_data=f"adm_blk_{t2}"))
            kb.append(row)
            
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_block_start")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# --- ОСНОВНОЕ МЕНЮ ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    # Сохраняем пользователя для рассылок
    async with aiosqlite.connect('salon.db') as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        await db.commit()

    if message.from_user.id in ADMINS:
        await message.answer("🛠 <b>Панель администратора</b>\nВыберите нужное действие:", reply_markup=admin_main_kb())
        return

    text = (
        "👋Здравствуйте, это бот для записи на маникюр салона Mirayy, "
        "по адресу <code>Дом пушкина 123</code>.\n\n"
        "Выберите нужное вам действие:"
    )
    try:
        photo = URLInputFile(MAIN_MENU_PHOTO)
        await message.answer_photo(photo=photo, caption=text, reply_markup=main_menu_kb())
    except Exception:
        await message.answer(text, reply_markup=main_menu_kb())

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except:
        pass

    text = (
        "👋Здравствуйте, это бот для записи на маникюр салона Mirayy, "
        "по адресу <code>Дом пушкина 123</code>.\n\n"
        "Выберите нужное вам действие:"
    )
    try:
        photo = URLInputFile(MAIN_MENU_PHOTO)
        await call.message.answer_photo(photo=photo, caption=text, reply_markup=main_menu_kb())
    except Exception:
        await call.message.answer(text, reply_markup=main_menu_kb())

@router.callback_query(F.data == "admin_menu")
async def go_to_admin_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.clear()
    try:
        await call.message.edit_text("🛠 <b>Панель администратора</b>\nВыберите нужное действие:", reply_markup=admin_main_kb())
    except:
        try: await call.message.delete()
        except: pass
        await call.message.answer("🛠 <b>Панель администратора</b>\nВыберите нужное действие:", reply_markup=admin_main_kb())

# --- КНОПКИ: КОНТАКТЫ И ПОРТФОЛИО ---
@router.callback_query(F.data == "menu_contacts")
async def show_contacts(call: CallbackQuery):
    text = (
        "👥 <b>Контакты:</b>\n"
        "Менеджер: 89999999999\n"
        "Мастер Настя: 88888888888\n"
        "Мастер Лера: 87777777777\n"
        "Мастер Арина: 86666666666\n"
        "Поддержка бота: @mirayy_code"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
        await call.message.answer(text, reply_markup=kb)
    except:
        await call.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == "menu_portfolio")
async def show_portfolio(call: CallbackQuery):
    text = (
        "💄 Наше портфолио вы можете посмотреть в наших сотсетях:\n"
        "VK: ссылка_на_вк\n"
        "INSTAGRAM: ссылка_на_inst\n"
        "TELEGRAM: ссылка_на_tg"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
        await call.message.answer(text, reply_markup=kb)
    except:
        await call.message.edit_text(text, reply_markup=kb)

# --- ПРОЦЕСС ЗАПИСИ (КЛИЕНТ) ---
@router.callback_query(F.data == "menu_book")
async def start_booking(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingState.choosing_master)
    try:
        await call.message.delete()
        await call.message.answer("👩‍🎤Выберите мастера:", reply_markup=masters_kb("book"))
    except:
        await call.message.edit_text("👩‍🎤Выберите мастера:", reply_markup=masters_kb("book"))

@router.callback_query(BookingState.choosing_master, F.data.startswith("book_master_"))
async def book_master_chosen(call: CallbackQuery, state: FSMContext):
    master = call.data.split("_")[2]
    await state.update_data(master=master)
    await state.set_state(BookingState.choosing_date)
    await call.message.edit_text(f"✳️ Выбранный мастер <b>{master}</b>.\n\nВыберите свободный день:", reply_markup=dates_kb("book"))

@router.callback_query(BookingState.choosing_date, F.data.startswith("book_date_"))
async def book_date_chosen(call: CallbackQuery, state: FSMContext):
    date = call.data.split("_")[2]
    data = await state.get_data()
    master = data['master']
    await state.update_data(date=date)
    
    kb = await times_kb(master, date)
    if not kb:
        await call.message.edit_text(
            "😔 Извините, на этот день нет свободного времени. Выберите другой день.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    await state.set_state(BookingState.choosing_time)
    await call.message.edit_text(
        f"✳️ Мастер: <b>{master}</b>\n📅 День: <b>{date}</b>\n\nВыберите время:",
        reply_markup=kb
    )

@router.callback_query(BookingState.choosing_time, F.data.startswith("time_"))
async def book_time_chosen(call: CallbackQuery, state: FSMContext):
    time = call.data.split("_")[1]
    data = await state.get_data()
    await state.update_data(time=time)
    await state.set_state(BookingState.entering_contact)
    await state.update_data(msg_id=call.message.message_id)
    
    await call.message.edit_text(
        f"✳️ Мастер: <b>{data['master']}</b>\n📅 День: <b>{data['date']}</b>\n🕘 Время: <b>{time}</b>\n\n"
        f"📲 Пожалуйста, напишите своё имя и номер телефона (Например: Имя 81234567890)."
    )

@router.message(BookingState.entering_contact)
async def process_contact(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(contact_info=message.text)
    await message.delete()
    
    text = (
        f"✳️Мастер: {data['master']}\n📅День: {data['date']}\n🕟Время: {data['time']}\n"
        f"📲Имя и номер: {message.text}\n\nПодтверждаете запись?"
    )
    await state.set_state(BookingState.confirming)
    await bot.edit_message_text(chat_id=message.chat.id, message_id=data['msg_id'], text=text, reply_markup=confirm_kb())

@router.callback_query(BookingState.confirming, F.data.in_(["confirm_yes", "confirm_no"]))
async def confirm_booking(call: CallbackQuery, state: FSMContext):
    menu_text = "👋Здравствуйте, это бот салона Mirayy...\nВыберите действие:"
    photo = URLInputFile(MAIN_MENU_PHOTO)

    if call.data == "confirm_no":
        try: await call.message.delete()
        except: pass
        await call.answer("Запись отклонена ❌", show_alert=True)
        await call.message.answer_photo(photo=photo, caption=menu_text, reply_markup=main_menu_kb())
        await state.clear()
        return

    data = await state.get_data()
    async with aiosqlite.connect('salon.db') as db:
        cursor = await db.execute(
            "INSERT INTO bookings (user_id, master, date, time, contact_info) VALUES (?, ?, ?, ?, ?)",
            (call.from_user.id, data['master'], data['date'], data['time'], data['contact_info'])
        )
        await db.commit()
        booking_id = cursor.lastrowid

    try: await call.message.delete()
    except: pass
    await call.answer(f"Запись создана ✅\nВаш ID: {booking_id}", show_alert=True)
    await call.message.answer_photo(photo=photo, caption=menu_text, reply_markup=main_menu_kb())
    
    # Уведомление админов
    admin_text = f"🔔 <b>Новая запись! (Id{booking_id})</b>\nМастер: {data['master']}\nДата: {data['date']} {data['time']}\nКонтакт: {data['contact_info']}"
    for admin_id in ADMINS:
        try: await bot.send_message(admin_id, admin_text)
        except: pass
    await state.clear()

# --- МОИ ЗАПИСИ (КЛИЕНТ) ---
@router.callback_query(F.data == "menu_my_bookings")
async def my_bookings(call: CallbackQuery):
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, master, date, time, contact_info FROM bookings WHERE user_id=? ORDER BY date, time", (call.from_user.id,)) as cursor:
            bookings = await cursor.fetchall()
            
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
    if not bookings:
        try: await call.message.edit_text("У вас нет активных записей.", reply_markup=kb)
        except: 
            await call.message.delete()
            await call.message.answer("У вас нет активных записей.", reply_markup=kb)
        return

    text = "📝 <b>Ваши записи:</b>\n\n"
    for i, b in enumerate(bookings, 1):
        text += f"  {i}️⃣ Запись Id{b[0]}\n✳️Мастер: {b[1]}\n📅Дата: {b[2]} в {b[3]}\n🗑 Отмена: /Delete_Id{b[0]}\n\n"
    
    try: await call.message.edit_text(text, reply_markup=kb)
    except: 
        await call.message.delete()
        await call.message.answer(text, reply_markup=kb)

# --- УДАЛЕНИЕ ЗАПИСЕЙ ПО КОМАНДЕ ---
@router.message(F.text.startswith("/Delete_Id"))
async def delete_booking_cmd(message: Message):
    try: b_id = int(message.text.split("_Id")[1])
    except: return
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT user_id FROM bookings WHERE id=?", (b_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row: return await message.answer("Запись не найдена.")
        
        if row[0] == message.from_user.id or message.from_user.id in ADMINS:
            await db.execute("DELETE FROM bookings WHERE id=?", (b_id,))
            await db.commit()
            await message.answer(f"Запись ID{b_id} удалена🗑.")
        else:
            await message.answer("Нет прав для удаления этой записи.")

# ==========================================
# ============ АДМИН ПАНЕЛЬ ================
# ==========================================

@router.message(Command("admin170311", "Admin170311"))
async def admin_login(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with aiosqlite.connect('salon.db') as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()
    ADMINS.add(user_id)
    await state.clear()
    await message.answer("Вы авторизованы как администратор 🛠", reply_markup=admin_main_kb())

# --- 1. ПРОСМОТР ЗАПИСЕЙ И ОТМЕНА ---
@router.callback_query(F.data == "admin_view")
async def admin_view_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.set_state(AdminState.choosing_master)
    await call.message.edit_text("👩‍🎤Выберите мастера для просмотра:", reply_markup=masters_kb("admin"))

@router.callback_query(AdminState.choosing_master, F.data.startswith("admin_master_"))
async def admin_master_chosen(call: CallbackQuery, state: FSMContext):
    master = call.data.split("_")[2]
    await state.update_data(master=master)
    await state.set_state(AdminState.choosing_date)
    await call.message.edit_text(f"Мастер: <b>{master}</b>\nВыберите день:", reply_markup=dates_kb("admin"))

async def render_admin_records(message, master, date):
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, time, contact_info FROM bookings WHERE master=? AND date=? ORDER BY time", (master, date)) as cursor:
            bookings = await cursor.fetchall()
            
    if not bookings:
        text = f"На <b>{date}</b> у мастера <b>{master}</b> нет записей."
    else:
        text = f"Записи к <b>{master}</b> на <b>{date}</b>:\n\n"
        for i, b in enumerate(bookings, 1):
            text += f"  {i}️⃣ Id{b[0]} | {b[1]}\n📞Контакт: {b[2]}\n\n"
            
    kb = admin_records_kb(master, date, bookings)
    try: await message.edit_text(text, reply_markup=kb)
    except Exception: pass

@router.callback_query(AdminState.choosing_date, F.data.startswith("admin_date_"))
async def admin_date_chosen(call: CallbackQuery, state: FSMContext):
    date = call.data.split("_")[2]
    data = await state.get_data()
    await render_admin_records(call.message, data['master'], date)
    await state.clear()

@router.callback_query(F.data.startswith("admin_refresh_"))
async def admin_refresh_records(call: CallbackQuery):
    if call.from_user.id not in ADMINS: return
    master, date = call.data.split("_")[2], call.data.split("_")[3]
    await render_admin_records(call.message, master, date)
    await call.answer("Данные обновлены 🔄")

# ОТМЕНА ЗАПИСИ КНОПКОЙ ИЗ СПИСКА
@router.callback_query(F.data.startswith("adm_can_"))
async def admin_cancel_booking_inline(call: CallbackQuery):
    if call.from_user.id not in ADMINS: return
    b_id = int(call.data.split("_")[2])
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT user_id, master, date, time FROM bookings WHERE id=?", (b_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            return await call.answer("Запись уже удалена!", show_alert=True)
            
        user_id, master, date, time = row
        await db.execute("DELETE FROM bookings WHERE id=?", (b_id,))
        await db.commit()
        
    # Уведомляем клиента
    try:
        await bot.send_message(user_id, f"❗ Ваша запись к мастеру <b>{master}</b> на <b>{date}</b> в <b>{time}</b> была отменена администратором.")
    except Exception:
        pass
        
    await call.answer("Запись удалена, клиент уведомлен ✅", show_alert=True)
    await render_admin_records(call.message, master, date)

# --- 2. БЛОКИРОВКА ВРЕМЕНИ ---
@router.callback_query(F.data == "admin_block_start")
async def admin_block_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.set_state(AdminState.block_master)
    await call.message.edit_text("⛔ Блокировка времени.\nВыберите мастера:", reply_markup=masters_kb("admin"))

@router.callback_query(AdminState.block_master, F.data.startswith("admin_master_"))
async def admin_block_master(call: CallbackQuery, state: FSMContext):
    master = call.data.split("_")[2]
    await state.update_data(master=master)
    await state.set_state(AdminState.block_date)
    await call.message.edit_text(f"Блокировка. Мастер <b>{master}</b>.\nВыберите день:", reply_markup=dates_kb("admin"))

@router.callback_query(AdminState.block_date, F.data.startswith("admin_date_"))
async def admin_block_date(call: CallbackQuery, state: FSMContext):
    date = call.data.split("_")[2]
    await state.update_data(date=date)
    data = await state.get_data()
    await state.set_state(AdminState.block_time)
    
    kb = await admin_block_times_kb(data['master'], date)
    await call.message.edit_text(f"Управление временем: <b>{data['master']}</b> | <b>{date}</b>\nВыберите время для изменения статуса:", reply_markup=kb)

@router.callback_query(AdminState.block_time, F.data.startswith("adm_blk_"))
async def admin_toggle_block(call: CallbackQuery, state: FSMContext):
    time_val = call.data.split("_")[2]
    data = await state.get_data()
    master, date = data['master'], data['date']
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id FROM blocked_times WHERE master=? AND date=? AND time=?", (master, date, time_val)) as cursor:
            row = await cursor.fetchone()
            
        if row: # Если заблокировано - снимаем блок
            await db.execute("DELETE FROM blocked_times WHERE id=?", (row[0],))
            await call.answer("Блокировка снята 🟢")
        else: # Если свободно - ставим блок
            await db.execute("INSERT INTO blocked_times (master, date, time) VALUES (?, ?, ?)", (master, date, time_val))
            await call.answer("Время заблокировано ⛔")
        await db.commit()
        
    kb = await admin_block_times_kb(master, date)
    await call.message.edit_text(f"Управление временем: <b>{master}</b> | <b>{date}</b>\nВыберите время для изменения статуса:", reply_markup=kb)

# --- 3. МАССОВАЯ РАССЫЛКА ---
@router.callback_query(F.data == "admin_broadcast_start")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.set_state(AdminState.broadcast_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_menu")]])
    await call.message.edit_text("📢 <b>Режим рассылки</b>\nОтправьте сюда текст, фото или видео, которое получат ВСЕ клиенты бота:", reply_markup=kb)

@router.message(AdminState.broadcast_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS: return
    await state.clear()
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
            
    success_count = 0
    await message.answer("⏳ Рассылка начата...")
    
    for u in users:
        user_id = u[0]
        try:
            await message.send_copy(chat_id=user_id)
            success_count += 1
            await asyncio.sleep(0.05) # Защита от спам-блока Telegram
        except Exception:
            pass # Пользователь заблокировал бота
            
    await message.answer(f"✅ <b>Рассылка завершена!</b>\nУспешно доставлено: {success_count} пользователям.", reply_markup=admin_main_kb())


# --- СИСТЕМА НАПОМИНАНИЙ И ОЧИСТКИ БАЗЫ ---
async def check_reminders():
    now = get_moscow_time()
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, user_id, date, time, remind_24, remind_6, remind_2 FROM bookings") as cursor:
            bookings = await cursor.fetchall()
            
        for b in bookings:
            b_id, user_id, date_str, time_str, r24, r6, r2 = b
            try:
                appt_time = TZ_MOSCOW.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
            except Exception:
                continue
                
            time_left = appt_time - now
            hours_left = time_left.total_seconds() / 3600
            
            if 0 < hours_left <= 24 and not r24:
                await send_reminder(user_id, 24)
                await db.execute("UPDATE bookings SET remind_24=1 WHERE id=?", (b_id,))
            elif 0 < hours_left <= 6 and not r6:
                await send_reminder(user_id, 6)
                await db.execute("UPDATE bookings SET remind_6=1 WHERE id=?", (b_id,))
            elif 0 < hours_left <= 2 and not r2:
                await send_reminder(user_id, 2)
                await db.execute("UPDATE bookings SET remind_2=1 WHERE id=?", (b_id,))
        await db.commit()

async def send_reminder(user_id, hours):
    try:
        await bot.send_message(user_id, f"🔔 Напоминание! До вашей записи осталось {hours} часа. Подробности в разделе 'Мои записи'.")
    except: pass

async def cleanup_old_data():
    """Удаляет прошедшие записи и истекшие блокировки времени"""
    today_str = get_moscow_time().strftime("%Y-%m-%d")
    try:
        async with aiosqlite.connect('salon.db') as db:
            await db.execute("DELETE FROM bookings WHERE date < ?", (today_str,))
            await db.execute("DELETE FROM blocked_times WHERE date < ?", (today_str,))
            await db.commit()
    except Exception as e:
        logging.error(f"Ошибка очистки базы: {e}")

# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT & RENDER ---
async def web_handler(request):
    return web.Response(text="Bot is running and awake!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', web_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- ЗАПУСК БОТА ---
async def main():
    await init_db()
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, 'interval', minutes=10)
    # Очистка базы раз в сутки (в фоне)
    scheduler.add_job(cleanup_old_data, 'interval', hours=24)
    scheduler.start()
    
    # Запуск первичной очистки базы при старте
    await cleanup_old_data()
    
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
