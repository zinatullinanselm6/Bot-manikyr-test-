import asyncio
import logging
import sqlite3
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
BOT_TOKEN = "8871725908:AAF6Bq-mGh5Ik1AtjNQ1qn_1f0Uz74kI8uI" # Токен вернул прямо в код
TZ_MOSCOW = pytz.timezone('Europe/Moscow')
ADMINS = set() # Здесь будут временно храниться ID админов

# Укажи здесь ссылку на твою главную картинку (баннер салона)
MAIN_MENU_PHOTO = "https://i.postimg.cc/rsh5828D/IMG-20260623-193407.png"

from aiogram.client.default import DefaultBotProperties

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('salon.db') as db:
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
        await db.commit()

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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ДАТЫ И ВРЕМЯ) ---
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

# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться 🖊️", callback_data="menu_book")],
        [InlineKeyboardButton(text="Мои записи 🟢", callback_data="menu_my_bookings")],
        [InlineKeyboardButton(text="Контакты 👥", callback_data="menu_contacts")],
        [InlineKeyboardButton(text="Наше портфолио💄", callback_data="menu_portfolio")]
    ])

def masters_kb(prefix="book"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Настя", callback_data=f"{prefix}_master_Настя"),
         InlineKeyboardButton(text="Лера", callback_data=f"{prefix}_master_Лера")],
        [InlineKeyboardButton(text="Арина", callback_data=f"{prefix}_master_Арина")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])

def dates_kb(prefix="book"):
    dates = get_date_buttons()
    kb = []
    for i in range(0, len(dates), 2):
        row = [InlineKeyboardButton(text=dates[i][0], callback_data=f"{prefix}_date_{dates[i][1]}")]
        if i+1 < len(dates):
            row.append(InlineKeyboardButton(text=dates[i+1][0], callback_data=f"{prefix}_date_{dates[i+1][1]}"))
        kb.append(row)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def times_kb(master, date):
    all_times = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT time FROM bookings WHERE master=? AND date=?", (master, date)) as cursor:
            booked_rows = await cursor.fetchall()
            booked_times = [row[0] for row in booked_rows]

    available_times = [t for t in all_times if t not in booked_times]
    
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

# --- ОСНОВНОЕ МЕНЮ С ФОТО ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋Здравствуйте, это бот для записи на маникюр салона Mirayy, "
        "по адресу <code>Дом пушкина 123</code>.\n\n"
        "Выберите нужное вам действие:"
    )
    try:
        photo = URLInputFile(MAIN_MENU_PHOTO)
        await message.answer_photo(photo=photo, caption=text, reply_markup=main_menu_kb())
    except Exception as e:
        logging.error(f"Ошибка отправки фото в старте: {e}")
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
    except Exception as e:
        logging.error(f"Ошибка отправки фото при возврате: {e}")
        await message.answer(text, reply_markup=main_menu_kb())

# --- КНОПКА 3: КОНТАКТЫ ---
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

# --- КНОПКА 4: ПОРТФОЛИО ---
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

# --- КНОПКА 1: ПРОЦЕСС ЗАПИСИ ---
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
    await call.message.edit_text(f"✳️ Выбранный мастер <b>{master}</b>.\n\nВыберите свободный день для записи:", reply_markup=dates_kb("book"))

@router.callback_query(BookingState.choosing_date, F.data.startswith("book_date_"))
async def book_date_chosen(call: CallbackQuery, state: FSMContext):
    date = call.data.split("_")[2]
    data = await state.get_data()
    master = data['master']
    await state.update_data(date=date)
    
    kb = await times_kb(master, date)
    
    if not kb:
        await call.message.edit_text(
            "😔 У этого мастера нету свободных ячеек в этот день, пожалуйста выберите другого мастера или другой день для записи.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    await state.set_state(BookingState.choosing_time)
    await call.message.edit_text(
        f"✳️ Выбранный мастер <b>{master}</b>.\n"
        f"📅 Выбранный день <b>{date}</b>\n\n"
        f"Выберите свободное время для записи:",
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
        f"✳️ Выбранный мастер <b>{data['master']}</b>.\n"
        f"📅 Выбранный день <b>{data['date']}</b>\n"
        f"🕘 Выбранное время <b>{time}</b>\n\n"
        f"📲 Пожалуйста напишите своё имя и номер телефона в таком формате (Имя 81234567890)."
    )

@router.message(BookingState.entering_contact)
async def process_contact(message: Message, state: FSMContext):
    contact_info = message.text
    data = await state.get_data()
    await state.update_data(contact_info=contact_info)
    
    await message.delete()
    
    text = (
        f"✳️Мастер: {data['master']}\n"
        f"📅День: {data['date']}\n"
        f"🕟Время: {data['time']}\n"
        f"📲Имя и номер: {contact_info}\n\n"
        f"Подтверждаете запись?"
    )
    
    await state.set_state(BookingState.confirming)
    await bot.edit_message_text(chat_id=message.chat.id, message_id=data['msg_id'], text=text, reply_markup=confirm_kb())

@router.callback_query(BookingState.confirming, F.data.in_(["confirm_yes", "confirm_no"]))
async def confirm_booking(call: CallbackQuery, state: FSMContext):
    if call.data == "confirm_no":
        await call.message.edit_text("Запись отклонена❌", reply_markup=main_menu_kb())
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

    await call.message.edit_text(f"Запись создана✅\nВаш ID записи: Id{booking_id}", reply_markup=main_menu_kb())
    
    admin_text = (
        f"🔔 <b>Новая запись в салон!</b>\n\n"
        f"Запись Id{booking_id}\n"
        f"✳️Мастер: {data['master']}\n"
        f"📅День: {data['date']}\n"
        f"🕟Время: {data['time']}\n"
        f"📲Имя и номер: {data['contact_info']}"
    )
    for admin_id in ADMINS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception:
            pass
            
    await state.clear()

# --- КНОПКА 2: МОИ ЗАПИСИ ---
@router.callback_query(F.data == "menu_my_bookings")
async def my_bookings(call: CallbackQuery):
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, master, date, time, contact_info FROM bookings WHERE user_id=?", (call.from_user.id,)) as cursor:
            bookings = await cursor.fetchall()
            
    if not bookings:
        try:
            await call.message.delete()
            await call.message.answer("У вас нет активных записей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]))
        except:
            await call.message.edit_text("У вас нет активных записей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]]))
        return

    text = "📝 <b>Ваши записи:</b>\n\n"
    for i, b in enumerate(bookings, 1):
        text += (
            f"  {i}️⃣\n"
            f"Запись Id{b[0]}\n"
            f"✳️Мастер: {b[1]}\n"
            f"📅День: {b[2]}\n"
            f"🕟Время: {b[3]}\n"
            f"📲Имя и номер: {b[4]}\n"
            f"    🗑Для удаления этой записи нажмите сюда /Delete_Id{b[0]}\n\n"
        )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")]])
    try:
        await call.message.delete()
        await call.message.answer(text, reply_markup=kb)
    except:
        await call.message.edit_text(text, reply_markup=kb)

# --- УДАЛЕНИЕ ЗАПИСЕЙ ---
@router.message(F.text.startswith("/Delete_Id"))
async def delete_booking(message: Message):
    try:
        b_id = int(message.text.split("_Id")[1])
    except:
        return
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT user_id FROM bookings WHERE id=?", (b_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            await message.answer("Запись не найдена.")
            return
            
        owner_id = row[0]
        is_admin = message.from_user.id in ADMINS
        
        if owner_id == message.from_user.id or is_admin:
            await db.execute("DELETE FROM bookings WHERE id=?", (b_id,))
            await db.commit()
            
            if is_admin and owner_id != message.from_user.id:
                await message.answer(f"Запись ID{b_id} удалена из базы🗑.")
                try:
                    await bot.send_message(owner_id, f"Ваша запись ID{b_id} была удалена администратором бота❗")
                except:
                    pass
            else:
                await message.answer(f"Запись ID{b_id} удалена🗑.")
        else:
            await message.answer("У вас нет прав для удаления этой записи.")

# --- АДМИН ПАНЕЛЬ ---
@router.message(Command("Admin170311"))
async def admin_login(message: Message):
    ADMINS.add(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Посмотреть записи", callback_data="admin_view")]])
    await message.answer("Вы стали администратором бота и теперь можете просматривать все записи❗", reply_markup=kb)

@router.callback_query(F.data == "admin_view")
async def admin_view_masters(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS:
        return
    await state.set_state(AdminState.choosing_master)
    try:
        await call.message.delete()
        await call.message.answer("👩‍🎤Выберите мастера для просмотра записей:", reply_markup=masters_kb("admin"))
    except:
        await call.message.edit_text("👩‍🎤Выберите мастера для просмотра записей:", reply_markup=masters_kb("admin"))

@router.callback_query(AdminState.choosing_master, F.data.startswith("admin_master_"))
async def admin_master_chosen(call: CallbackQuery, state: FSMContext):
    master = call.data.split("_")[2]
    await state.update_data(master=master)
    await state.set_state(AdminState.choosing_date)
    await call.message.edit_text(f"Мастер: {master}\nВыберите день:", reply_markup=dates_kb("admin"))

@router.callback_query(AdminState.choosing_date, F.data.startswith("admin_date_"))
async def admin_date_chosen(call: CallbackQuery, state: FSMContext):
    date = call.data.split("_")[2]
    data = await state.get_data()
    master = data['master']
    
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, time, contact_info FROM bookings WHERE master=? AND date=? ORDER BY time", (master, date)) as cursor:
            bookings = await cursor.fetchall()
            
    if not bookings:
        await call.message.edit_text(f"На {date} у мастера {master} нет записей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_view")]]))
        await state.clear()
        return

    text = f"Записи к <b>{master}</b> на <b>{date}</b>:\n\n"
    for i, b in enumerate(bookings, 1):
        text += (
            f"  {i}️⃣\n"
            f"Запись Id{b[0]}\n"
            f"🕟Время: {b[1]}\n"
            f"📲Контакт: {b[2]}\n"
            f"    🗑Удалить: /Delete_Id{b[0]}\n\n"
        )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Скрыть панель", callback_data="back_to_main")]])
    await call.message.edit_text(text, reply_markup=kb)
    await state.clear()

# --- СИСТЕМА НАПОМИНАНИЙ ---
async def check_reminders():
    now = get_moscow_time()
    async with aiosqlite.connect('salon.db') as db:
        async with db.execute("SELECT id, user_id, date, time, remind_24, remind_6, remind_2 FROM bookings") as cursor:
            bookings = await cursor.fetchall()
            
        for b in bookings:
            b_id, user_id, date_str, time_str, r24, r6, r2 = b
            try:
                dt_str = f"{date_str} {time_str}"
                appt_time = TZ_MOSCOW.localize(datetime.strptime(dt_str, "%Y-%m-%d %H:%M"))
            except Exception as e:
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
        await bot.send_message(user_id, f"🔔Напоминание, до вашей записи осталось {hours} часа. Подробнее в разделе Мои записи.")
    except:
        pass

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
    scheduler.start()
    
    await start_web_server()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
