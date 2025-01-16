import asyncio
import json
import logging
import uuid
import os
from docx import Document
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from langchain_gigachat import GigaChat
from langchain.memory import ConversationBufferMemory
from dotenv import load_dotenv
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

import aiohttp
import nest_asyncio
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone


from aiogram import Bot, Dispatcher, BaseMiddleware, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, 
    CallbackQuery, 
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    FSInputFile
)

# Загружаем переменные из файла .env
load_dotenv()

# Получаем токен из переменной окружения
API_TOKEN = os.getenv('API_TOKEN')
API_KEY = os.getenv('GIGACHAT_KEY')

DB_PATH = "database/users.db"
moscow_tz = timezone("Europe/Moscow")

# Словарь для хранения памяти диалогов для каждого пользователя
user_memories = {}

# --- Logging configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Scheduler initialization ---
scheduler = AsyncIOScheduler()

# --- Bot and Dispatcher initialization ---
bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- States ---
class RegistrationForm(StatesGroup):
    name = State()
    age = State()
    email = State()

class DiaryForm(StatesGroup):
    situation = State()
    thought = State()
    emotion = State()
    reaction = State()

class ReminderForm(StatesGroup):
    time = State()

# --- Keyboards ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Добавить запись в дневник")],
        [KeyboardButton(text="Получить рекомендацию")],
        [KeyboardButton(text="Продолжить диалог с GigaChat")],
        [KeyboardButton(text="Оставить отзыв")],
        [KeyboardButton(text="Экспортировать дневник")],
        [KeyboardButton(text="Настройки")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

settings_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="\u2705 Напоминания: Включить", 
                callback_data="toggle_reminder_on"
            ),
            InlineKeyboardButton(
                text="\u274C Напоминания: Выключить", 
                callback_data="toggle_reminder_off"
            )
        ],
        [
            InlineKeyboardButton(
                text="Установить время напоминания", 
                callback_data="set_reminder_time"
            )
        ]
    ]
)

async def generate_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Генерация главного меню с учетом наличия рекомендации."""
    has_reco = await has_recommendation(user_id)
    buttons = [
        [KeyboardButton(text="Добавить запись в дневник")],
        [KeyboardButton(text="Получить рекомендацию")],
    ]
    
    if has_reco:  # Добавляем кнопку только если есть рекомендация
        buttons.append([KeyboardButton(text="Продолжить диалог с GigaChat")])

    buttons.extend([
        [KeyboardButton(text="Оставить отзыв")],
        [KeyboardButton(text="Экспортировать дневник")],
        [KeyboardButton(text="Настройки")],
    ])

    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)


def get_end_dialog_button():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Завершить диалог", callback_data="end_dialog")]
        ]
    )

# --- Middleware for registration check ---
class RegistrationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Only check if it's a regular message and not the `/start` command
        if isinstance(event, Message) and event.text != '/start':
            user_id = event.chat.id
            fsm_context: FSMContext = data["state"]
            state = await fsm_context.get_state()

            # Allow messages if user is still in the registration states
            if state and state.startswith("RegistrationForm:"):
                return await handler(event, data)

            # Otherwise, verify if the user is in the DB
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id FROM users WHERE id = ?", (user_id,)
                ) as cursor:
                    if await cursor.fetchone() is None:
                        await bot.send_message(
                            chat_id=user_id,
                            text="Вы не зарегистрированы! Зарегистрируйтесь, используя команду /start."
                        )
                        return

        return await handler(event, data)

# --- Database Initialization ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                age INTEGER,
                email TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                situation TEXT,
                thought TEXT,
                emotion TEXT,
                reaction TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                time TEXT,
                last_sent_date DATE,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.commit()

# --- Reminder Function ---
async def send_reminders():
    current_time = datetime.now(moscow_tz).strftime("%H:%M")
    current_date = datetime.now(moscow_tz).date()
    print(f"[DEBUG] Current time: {current_time}, Current date: {current_date}")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT user_id, last_sent_date
            FROM reminders
            WHERE enabled = 1 AND time = ?
            """,
            (current_time,)
        ) as cursor:
            users = await cursor.fetchall()
            for user_id, last_sent_date in users:
                if last_sent_date == str(current_date):
                    continue

                try:
                    await bot.send_message(
                        user_id,
                        "Напоминание: Не забудьте добавить запись в дневник!"
                    )
                    # Update last_sent_date
                    await db.execute(
                        """
                        UPDATE reminders
                        SET last_sent_date = ?
                        WHERE user_id = ?
                        """,
                        (current_date, user_id)
                    )
                    await db.commit()
                except Exception as e:
                    logger.error(
                        f"Не удалось отправить напоминание пользователю {user_id}: {e}"
                    )

# --- Utility Functions ---
async def get_last_diary_entry(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, situation, thought, emotion, reaction
            FROM diary
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,)
        ) as cursor:
            entry = await cursor.fetchone()
            if entry:
                return entry[1], entry[2], entry[3], entry[4], entry[0]
            return None

def generate_prompt(situation: str, thought: str, emotion: str, reaction: str) -> str:
    """Генерация промпта для GigaChat"""
    return f"""
        Ты — квалифицированный психолог с глубоким пониманием когнитивно-поведенческой терапии и эмоционального интеллекта.
        Твоя задача — анализировать записи из дневника СМЭР пользователя и предоставлять обоснованные рекомендации
        по управлению эмоциями и реакциями, а также другие полезные психологические советы.
        При этом ты используешь только достоверные данные и избегаешь любых предположений или вымышленных фактов.

        Пользователь предоставил следующую запись:

        Ситуация: {situation}
        Мысль: {thought}
        Эмоция: {emotion}
        Реакция: {reaction}

        На основе этой информации:

        1. Проанализируй связь между ситуацией, мыслью, эмоцией и реакцией, выявив возможные когнитивные искажения или паттерны поведения.
        2. Предложи конкретные стратегии или техники для управления данными эмоциями и реакциями, опираясь на доказанные психологические методы.
        3. Используй примеры из жизни или метафоры, чтобы иллюстрировать предложенные рекомендации и сделать их более понятными и применимыми.
        4. Объясни, почему именно эти подходы эффективны в данной ситуации, ссылаясь на психологические теории или исследования.
        5. Добавь практический совет или упражнение, которое пользователь сможет легко внедрить в свою повседневную жизнь
        для улучшения эмоционального состояния и реакции.
        
        Важно: Не используй шаблонные или общие рекомендации.
        Все советы должны быть адаптированы к конкретной записи пользователя и основываться на надежных психологических принципах.
        Пиши коротко.
        Не пиши более 3000 символов.
    """

# Инициализация модели GigaChat
GIGACHAT_CREDENTIALS = API_KEY  # Используем ключ авторизации из переменных окружения

async def get_recommendation_with_memory(user_id: int, prompt: str) -> str:
    """Получение ответа от GigaChat с учетом истории диалога."""
    try:
        # Создаем или используем память пользователя
        if user_id not in user_memories:
            user_memories[user_id] = ConversationBufferMemory()

        memory = user_memories[user_id]

        # Создаем шаблон для промпта
        prompt_template = PromptTemplate(
            input_variables=["input"],  # Это переменные, используемые в вашем промпте
            template="{input}"          # Простой шаблон, который берет весь ввод как есть
        )

        # Создаем цепочку с памятью
        chain = LLMChain(
            llm=GigaChat(
                credentials=GIGACHAT_CREDENTIALS,
                verify_ssl_certs=False,
                max_tokens=900,
                model="GigaChat-Max"
            ),
            memory=memory,
            prompt=prompt_template,  # Передаем промпт
            verbose=True
        )

        # Выполняем запрос с переданным текстом
        response = await chain.arun({"input": prompt})
        return response

    except Exception as e:
        raise Exception(f"Ошибка при запросе к GigaChat: {e}")

async def has_recommendation(user_id: int) -> bool:
    """Проверяет, есть ли рекомендация к последней записи пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT recommendation
            FROM diary
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:  # Проверяем, есть ли рекомендация
                return True
    return False


# --- Register Middleware ---
dp.update.middleware.register(RegistrationMiddleware())

# --- Handlers ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            user = await cursor.fetchone()

            if user is None:  # Если пользователь не зарегистрирован
                await state.set_state(RegistrationForm.name)
                await message.answer("Введите ваше имя:")
            else:
                main_menu = await generate_main_menu(user_id)  # Динамическое меню
                await message.answer(
                    f"Привет, {user[1]}! Вы уже зарегистрированы!",
                    reply_markup=main_menu
                )

@dp.message(RegistrationForm.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(RegistrationForm.age)
    await message.answer("Введите ваш возраст:")

@dp.message(RegistrationForm.age)
async def process_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите корректный возраст:")
        return
    await state.update_data(age=int(message.text))
    await state.set_state(RegistrationForm.email)
    await message.answer("Введите ваш email:")

@dp.message(RegistrationForm.email)
async def process_email(message: Message, state: FSMContext):
    await state.update_data(email=message.text)
    user_data = await state.get_data()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (id, name, age, email)
            VALUES (?, ?, ?, ?)
            """,
            (message.from_user.id, user_data["name"], user_data["age"], user_data["email"])
        )
        await db.commit()

    await state.clear()
    await message.answer(
        f"{user_data['name']}, вы успешно зарегистрированы!",
        reply_markup=main_menu
    )

@dp.message(Command(commands=["new_entry"]))
async def cmd_new_entry(message: Message, state: FSMContext):
    await state.set_state(DiaryForm.situation)
    await message.answer("Опишите ситуацию, которая произошла:")

@dp.message(lambda m: m.text == "Добавить запись в дневник")
async def handle_menu_new_entry(message: Message, state: FSMContext):
    await state.set_state(DiaryForm.situation)
    await message.answer("Опишите ситуацию, которая произошла:", reply_markup=ReplyKeyboardRemove())

@dp.message(DiaryForm.situation)
async def process_situation(message: Message, state: FSMContext):
    await state.update_data(situation=message.text)
    await state.set_state(DiaryForm.thought)
    await message.answer("Какая мысль у вас возникла?", reply_markup=ReplyKeyboardRemove())

@dp.message(DiaryForm.thought)
async def process_thought(message: Message, state: FSMContext):
    await state.update_data(thought=message.text)
    await state.set_state(DiaryForm.emotion)
    await message.answer("Какие эмоции вы испытали?", reply_markup=ReplyKeyboardRemove())

@dp.message(DiaryForm.emotion)
async def process_emotion(message: Message, state: FSMContext):
    await state.update_data(emotion=message.text)
    await state.set_state(DiaryForm.reaction)
    await message.answer("Какая была ваша реакция?", reply_markup=ReplyKeyboardRemove())

@dp.message(DiaryForm.reaction)
async def process_reaction(message: Message, state: FSMContext):
    await state.update_data(reaction=message.text)
    user_data = await state.get_data()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO diary (user_id, situation, thought, emotion, reaction)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.from_user.id,
                user_data["situation"],
                user_data["thought"],
                user_data["emotion"],
                user_data["reaction"]
            )
        )
        await db.commit()

    await state.clear()
    main_menu = await generate_main_menu(message.from_user.id)  # Динамическое меню
    await message.answer("Запись успешно сохранена!", reply_markup=main_menu)

@dp.message(lambda m: m.text == "Получить рекомендацию")
async def handle_menu_get_recommendation(message: Message):
    await message.answer("Сейчас посмотрим, подождите...", reply_markup=main_menu)
    await cmd_get_recommendation(message)

# --- Function to generate dynamic settings menu ---
async def generate_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled FROM reminders WHERE user_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            reminders_enabled = result[0] if result else 0

    # Generate buttons based on the reminder state
    buttons = [
        [
            InlineKeyboardButton(
                text="\u2705 Напоминания: Включить" if not reminders_enabled else "\u274C Напоминания: Выключить",
                callback_data="toggle_reminder_on" if not reminders_enabled else "toggle_reminder_off"
            )
        ]
    ]

    # Only add the "Set Reminder Time" button if reminders are enabled
    if reminders_enabled:
        buttons.append([
            InlineKeyboardButton(
                text="Установить время напоминания",
                callback_data="set_reminder_time"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def split_message(message: str, chunk_size: int = 4000) -> list:
    """Разбивает сообщение на части, чтобы каждая часть была не длиннее chunk_size."""
    return [message[i:i + chunk_size] for i in range(0, len(message), chunk_size)]

@dp.message(Command(commands=["get_recommendation"]))
async def cmd_get_recommendation(message: Message):
    user_id = message.from_user.id
    
    # Получение последней записи из дневника
    last_entry = await get_last_diary_entry(user_id)
    if not last_entry:
        await message.answer("У вас еще нет записей в дневнике. Добавьте запись, чтобы получить рекомендацию.")
        return

    # Генерация промпта
    situation, thought, emotion, reaction, entry_id = last_entry
    prompt = generate_prompt(situation, thought, emotion, reaction)

    # Получение рекомендации от GigaChat
    try:
        recommendation = await get_recommendation_with_memory(user_id, prompt)  # Передаем user_id и prompt

        # Сохраняем рекомендацию в дневник
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                UPDATE diary
                SET recommendation = ?
                WHERE id = ?
                """,
                (recommendation, entry_id)
            )
            await db.commit()

        # Разбиваем текст на части, если он слишком длинный
        messages = split_message(recommendation)
        for msg in messages:
            await message.answer(msg)  # Отправляем каждую часть по отдельности

    except Exception as e:
        await message.answer(f"Произошла ошибка при запросе к GigaChat: {e}")

@dp.message(lambda m: m.text == "Экспортировать дневник")
async def handle_export_diary(message: Message):
    user_id = message.from_user.id
    
    # Получение всех записей пользователя из БД
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT situation, thought, emotion, reaction, recommendation, created_at
            FROM diary
            WHERE user_id = ?
            ORDER BY created_at ASC
            """,
            (user_id,)
        ) as cursor:
            entries = await cursor.fetchall()

    if not entries:
        await message.answer("Ваш дневник пуст. Добавьте записи, чтобы экспортировать их.")
        return

    # Генерация DOCX-файла
    document = Document()
    document.add_heading("Дневник пользователя", level=1)

    for idx, (situation, thought, emotion, reaction, recommendation, created_at) in enumerate(entries, start=1):
        document.add_heading(f"Запись #{idx}", level=2)
        document.add_paragraph(f"Дата: {created_at}")
        document.add_paragraph(f"Ситуация: {situation}")
        document.add_paragraph(f"Мысль: {thought}")
        document.add_paragraph(f"Эмоция: {emotion}")
        document.add_paragraph(f"Реакция: {reaction}")
        document.add_paragraph(f"Рекомендация: {recommendation or 'Не получена'}")
        document.add_paragraph("-" * 118)

    # Сохранение файла на сервере
    file_path = f"tmp/diary_{user_id}.docx"
    document.save(file_path)

    # Отправка файла пользователю
    input_file = FSInputFile(file_path)
    await message.answer_document(
        document=input_file,
        caption="Ваш дневник в формате DOCX"
    )

    # Удаление временного файла
    os.remove(file_path)

@dp.message(lambda m: m.text == "Продолжить диалог с GigaChat")
async def handle_continue_dialog_with_gigachat(message: Message):
    user_id = message.from_user.id
    
    # Получение последней записи из дневника
    last_entry = await get_last_diary_entry(user_id)
    if not last_entry:
        await message.answer("У вас еще нет записей в дневнике. Добавьте запись, чтобы начать диалог с GigaChat.")
        return

    # Генерация промпта на основе последней записи
    situation, thought, emotion, reaction, entry_id = last_entry
    prompt = generate_prompt(situation, thought, emotion, reaction)

    # Добавляем контекст для продолжения диалога
    continuation_prompt = f"""
    На основе анализа последней записи и рекомендаций:
    {prompt}

    Теперь ты можешь задать вопрос пользователю или предложить дополнительную поддержку.
    Сформулируй вопрос или идею для обсуждения, чтобы продолжить диалог.
    """

    try:
        # Получение ответа от GigaChat с памятью
        response = await get_recommendation_with_memory(user_id, continuation_prompt)
        await message.answer(
            f"ГигаЧат: {response}",
            reply_markup=get_end_dialog_button()
        )
    except Exception as e:
        await message.answer(f"Произошла ошибка при взаимодействии с GigaChat: {e}")

@dp.message(lambda m: m.reply_to_message and "ГигаЧат" in m.reply_to_message.text)
async def handle_follow_up_with_gigachat(message: Message):
    user_id = message.from_user.id
    user_question = message.text

    # Формируем промпт на основе вопроса пользователя
    follow_up_prompt = f"""
    Пользователь задал вопрос или продолжил диалог:
    "{user_question}"

    Ответь подробно и продолжи поддерживать диалог, опираясь на предыдущий контекст.
    """

    try:
        # Получение ответа от GigaChat с памятью
        response = await get_recommendation_with_memory(user_id, follow_up_prompt)
        await message.answer(
            f"ГигаЧат: {response}",
            reply_markup=get_end_dialog_button()
        )
    except Exception as e:
        await message.answer(f"Произошла ошибка при взаимодействии с GigaChat: {e}")

@dp.callback_query(lambda c: c.data == "end_dialog")
async def handle_end_dialog(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id

    # Очистка памяти пользователя
    if user_id in user_memories:
        user_memories[user_id].clear()

    # Уведомляем пользователя о завершении диалога
    await callback_query.answer("Диалог завершен.")

    # Отправляем новое сообщение с главным меню
    await callback_query.message.reply(
        "Диалог с GigaChat завершен. Вы вернулись в главное меню.",
        reply_markup=main_menu  # Отправляем главное меню
    )

# --- Update the settings menu handler ---
@dp.message(lambda m: m.text == "Настройки")
async def handle_menu_settings(message: Message):
    user_id = message.from_user.id
    settings_menu = await generate_settings_menu(user_id)
    await message.answer("Настройки напоминаний:", reply_markup=settings_menu)

# --- Update callback handlers to refresh the menu ---
@dp.callback_query(lambda c: c.data in ["toggle_reminder_on", "toggle_reminder_off"])
async def toggle_reminders(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    enable_reminders = callback_query.data == "toggle_reminder_on"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO reminders (user_id, enabled)
            VALUES (?, ?)
            """,
            (user_id, 1 if enable_reminders else 0)
        )
        await db.commit()

    await callback_query.answer(
        "Напоминания включены!" if enable_reminders else "Напоминания выключены!"
    )

    # Refresh the settings menu
    settings_menu = await generate_settings_menu(user_id)
    await callback_query.message.edit_reply_markup(reply_markup=settings_menu)

@dp.callback_query(lambda c: c.data == "set_reminder_time")
async def set_reminder_time(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.time)
    await callback_query.message.answer(
        "Пожалуйста, укажите время для напоминания в формате ЧЧ:ММ",
        reply_markup=ReplyKeyboardRemove()
    )
    await callback_query.answer()

@dp.message(ReminderForm.time)
async def process_reminder_time(message: Message, state: FSMContext):
    try:
        reminder_time = message.text
        # Basic format check
        if len(reminder_time) != 5 or reminder_time[2] != ":" or not reminder_time.replace(":", "").isdigit():
            raise ValueError("Неверный формат времени")

        hours, minutes = map(int, reminder_time.split(":"))
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError("Часы или минуты выходят за допустимый диапазон")

        user_id = message.from_user.id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO reminders (user_id, enabled, time)
                VALUES (?, 1, ?)
                """,
                (user_id, reminder_time)
            )
            await db.commit()

        await state.clear()
        await message.answer(
            f"Время напоминания успешно установлено на {reminder_time}!",
            reply_markup=main_menu
        )

    except ValueError as e:
        await message.answer(f"Ошибка: {e}. Попробуйте еще раз в формате ЧЧ:ММ.")

# Анкета обратной связи
class FeedbackForm(StatesGroup):
    feedback = State()

@dp.message(lambda m: m.text == "Оставить отзыв")
async def handle_menu_feedback(message: Message, state: FSMContext):
    await state.set_state(FeedbackForm.feedback)
    await message.answer("Пожалуйста, оставьте ваш отзыв:", reply_markup=ReplyKeyboardRemove())

@dp.message(FeedbackForm.feedback)
async def process_feedback(message: Message, state: FSMContext):
    feedback = message.text
    user_id = message.from_user.id

    # Сохранение отзыва в базу данных
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO feedback (user_id, feedback)
            VALUES (?, ?)
            """,
            (user_id, feedback)
        )
        await db.commit()

    await state.clear()
    await message.answer("Спасибо за ваш отзыв!", reply_markup=main_menu)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                age INTEGER,
                email TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                situation TEXT,
                thought TEXT,
                emotion TEXT,
                reaction TEXT,
                recommendation TEXT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                time TEXT,
                last_sent_date DATE,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                feedback TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.commit()


# --- Main Entry Point ---
async def main():
    # Initialize DB
    await init_db()

    # Set bot commands
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="new_entry", description="Добавить запись в дневник"),
        BotCommand(command="get_recommendation", description="Получить рекомендацию"),
    ])

    # Configure Scheduler (if no jobs, add job)
    if not scheduler.get_jobs():
        scheduler.remove_all_jobs()
        # Run 'send_reminders' every minute at second=0
        scheduler.add_job(send_reminders, "cron", second=0)
    if not scheduler.running:
        scheduler.start()

    # Start bot polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        nest_asyncio.apply()
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error("Бот остановлен!")
