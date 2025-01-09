import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import State, StatesGroup
#from aiogram.utils import executor

# Для работы с планировщиком
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Для базы данных (SQLite)
import sqlite3

# Middleware
from aiogram import BaseMiddleware
from aiogram import types

# Инициализация логгера
logging.basicConfig(level=logging.INFO)

def init_db():
    """
    Создаём таблицы, если они не существуют.
    """
    conn = sqlite3.connect("database/db.sqlite3")
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS registration (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            fullname TEXT,
            age INTEGER
        )
        """
    )

    # Таблица дневника
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            situation TEXT,
            thought TEXT,
            emotion TEXT,
            reaction TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES registration(user_id)
        )
        """
    )

    conn.commit()
    conn.close()

class RegistrationStates(StatesGroup):
    waiting_for_fullname = State()
    waiting_for_age = State()

class DiaryStates(StatesGroup):
    waiting_for_situation = State()
    waiting_for_thought = State()
    waiting_for_emotion = State()
    waiting_for_reaction = State()

class RegistrationCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Update, data: dict):
        """
        Проверяем, зарегистрирован ли пользователь в базе.
        Если пользователь не зарегистрирован, запрещаем выполнение команды,
        за исключением команды /start.
        """
        # Определяем user_id
        if event.message:
            user_id = event.message.from_user.id
            command = event.message.text.strip() if event.message.text else ""
        elif event.callback_query:
            user_id = event.callback_query.from_user.id
            command = ""
        else:
            return await handler(event, data)  # Пропускаем другие типы апдейтов

        # Разрешаем выполнение команды /start без проверки регистрации
        if command.startswith("/start"):
            return await handler(event, data)

        # Проверяем регистрацию в базе данных
        conn = sqlite3.connect("database/db.sqlite3")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM registration WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            # Если пользователь не зарегистрирован, отправляем уведомление
            if event.message:
                await event.message.answer("Пожалуйста, зарегистрируйтесь командой /start.")
            elif event.callback_query:
                await event.callback_query.message.answer("Пожалуйста, зарегистрируйтесь командой /start.")
            return  # Блокируем дальнейшую обработку команды

        # Если пользователь зарегистрирован, передаём управление обработчику
        return await handler(event, data)



# Загружаем переменные из файла .env
load_dotenv()

# Получаем токен из переменной окружения
API_TOKEN = os.getenv('API_TOKEN')

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Регистрируем Middleware
dp.update.middleware.register(RegistrationCheckMiddleware())

router = Router()
dp.include_router(router)

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    """
    Старт регистрации. Проверяем, зарегистрирован ли уже пользователь.
    Если нет - запускаем процесс регистрации.
    """
    user_id = message.from_user.id
    username = message.from_user.username

    # Проверка в БД
    conn = sqlite3.connect("database/db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM registration WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()

    if result:
        await message.answer("Вы уже зарегистрированы! Можете использовать команды /new_entry для добавления записи.")
        await state.finish()
        conn.close()
        return
    conn.close()

    # Если нет, начинаем регистрацию
    await message.answer("Добро пожаловать! Давайте начнём регистрацию.\nПожалуйста, введите ваше полное имя:")
    await state.set_state(RegistrationStates.waiting_for_fullname)


@router.message(RegistrationStates.waiting_for_fullname)
async def process_fullname(message: types.Message, state: FSMContext):
    fullname = message.text.strip()
    # Можно добавить дополнительную валидацию
    if len(fullname) < 2:
        await message.answer("Имя слишком короткое, введите ещё раз:")
        return

    await state.update_data(fullname=fullname)
    await message.answer("Спасибо! Теперь введите ваш возраст (числом):")
    await state.set_state(RegistrationStates.waiting_for_age)


@router.message(RegistrationStates.waiting_for_age)
async def process_age(message: types.Message, state: FSMContext):
    # Проверяем, что возраст - число
    try:
        age = int(message.text.strip())
        if age < 1 or age > 120:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите корректный возраст (число):")
        return

    # Сохраняем
    await state.update_data(age=age)

    # Берём из контекста все данные
    user_data = await state.get_data()
    fullname = user_data.get("fullname")

    user_id = message.from_user.id
    username = message.from_user.username

    # Пишем в БД
    conn = sqlite3.connect("database/db.sqlite3")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO registration (user_id, username, fullname, age) VALUES (?, ?, ?, ?)",
        (user_id, username, fullname, age),
    )
    conn.commit()
    conn.close()

    await message.answer(f"Регистрация завершена!\nДобро пожаловать, {fullname}!\nТеперь можете добавить запись командой /new_entry.")
    await state.clear()

@router.message(Command(commands=["new_entry"]))
async def cmd_new_entry(message: types.Message, state: FSMContext):
    """
    Начало добавления новой записи в дневник.
    """
    await message.answer("Опишите ситуацию, которая произошла:")
    await state.set_state(DiaryStates.waiting_for_situation)


@router.message(DiaryStates.waiting_for_situation)
async def process_situation(message: types.Message, state: FSMContext):
    situation = message.text.strip()
    await state.update_data(situation=situation)
    await message.answer("Какая мысль у вас возникла?")
    await state.set_state(DiaryStates.waiting_for_thought)


@router.message(DiaryStates.waiting_for_thought)
async def process_thought(message: types.Message, state: FSMContext):
    thought = message.text.strip()
    await state.update_data(thought=thought)
    await message.answer("Какие эмоции вы испытали?")
    await state.set_state(DiaryStates.waiting_for_emotion)


@router.message(DiaryStates.waiting_for_emotion)
async def process_emotion(message: types.Message, state: FSMContext):
    emotion = message.text.strip()
    await state.update_data(emotion=emotion)
    await message.answer("Какая была ваша реакция?")
    await state.set_state(DiaryStates.waiting_for_reaction)


@router.message(DiaryStates.waiting_for_reaction)
async def process_reaction(message: types.Message, state: FSMContext):
    reaction = message.text.strip()
    await state.update_data(reaction=reaction)

    # Сохраняем в БД
    user_data = await state.get_data()
    user_id = message.from_user.id

    conn = sqlite3.connect("database/db.sqlite3")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO diary (user_id, situation, thought, emotion, reaction)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            user_data.get("situation"),
            user_data.get("thought"),
            user_data.get("emotion"),
            reaction,
        ),
    )
    conn.commit()
    conn.close()

    await message.answer("Запись в дневник успешно сохранена!")
    await state.clear()

scheduler = AsyncIOScheduler()

def send_reminders():
    """
    Получаем список всех пользователей из БД и отправляем каждому напоминание.
    """
    conn = sqlite3.connect("database/db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM registration")
    users = cursor.fetchall()
    conn.close()

    for (user_id,) in users:
        asyncio.create_task(bot.send_message(chat_id=user_id, text="Не забудьте заполнить дневник СМЭР сегодня!"))


async def scheduled_job():
    send_reminders()

# Регистрируем задачу: каждый день в 10:00
scheduler.add_job(scheduled_job, CronTrigger(hour=10, minute=0))

async def main():
    # Создаём таблицы (если не существуют)
    init_db()

    # Запускаем планировщик
    scheduler.start()

    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.error("Бот был остановлен!")

