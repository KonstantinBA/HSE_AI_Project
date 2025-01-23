import asyncio
import logging
import os
from docx import Document

from dotenv import load_dotenv
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict

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

# ================= LANGCHAIN И LANGGRAPH =================
from langchain_core.messages import HumanMessage
from langchain_gigachat.chat_models import GigaChat
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from datetime import datetime, timedelta, timezone
# Загружаем переменные из файла .env
load_dotenv()

API_TOKEN = os.getenv('API_TOKEN')
API_KEY = os.getenv('GIGACHAT_KEY')
DB_PATH = "database/users.db"
gmt_plus_3 = timezone(timedelta(hours=3))
print(gmt_plus_3)
# --- Logging configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация GigaChat (если необходимо, замените параметры)
model = GigaChat(
    credentials=API_KEY,
    scope="GIGACHAT_API_PERS",
    model="GigaChat",
    verify_ssl_certs=False,
)

# Промпт для взаимодействия с моделью
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "Ты — помощник. Отвечай кратко, но полезно."),
        MessagesPlaceholder(variable_name="messages"),
    ]
)

workflow = StateGraph(state_schema=MessagesState)

# Асинхронная функция для вызова модели
async def call_model(state: MessagesState):
    # Создаем цепочку: сначала используется prompt, затем модель
    chain = prompt | model
    response = await chain.ainvoke(state)
    return {"messages": response}

# Добавляем вершину графа
workflow.add_edge(START, "model")
workflow.add_node("model", call_model)

# Инициализируем персистентность через MemorySaver
memory = MemorySaver()

# Компилируем граф, получая приложение для вызова модели
app = workflow.compile(checkpointer=memory)

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
class DialogForm(StatesGroup):
    in_dialog = State()

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
        [KeyboardButton(text="Добавить запись в дневник"), KeyboardButton(text="Получить рекомендацию")],
        [KeyboardButton(text="Посмотреть дневник"), KeyboardButton(text="Экспортировать дневник")],
        [KeyboardButton(text="Оставить отзыв"), KeyboardButton(text="Настройки")]
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

dialog_buttons = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Продолжить диалог", callback_data="continue_dialog")],
    [InlineKeyboardButton(text="Завершить диалог", callback_data="end_dialog")]
])

# --- Middleware for registration check ---
class RegistrationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        print(isinstance(event, Message))
        if isinstance(event, Message) and event.text != '/start':
            user_id = event.chat.id
            fsm_context: FSMContext = data["state"]
            state = await fsm_context.get_state()

            # Если идёт процесс регистрации, пропускаем
            if state and state.startswith("RegistrationForm:"):
                return await handler(event, data)

            # Иначе проверяем, есть ли пользователь в БД
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

# --- Reminder Function ---
async def send_reminders():
    current_time = datetime.now(gmt_plus_3).strftime("%H:%M")
    current_date = datetime.now(gmt_plus_3).date()

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
                # Если уже отправляли сегодня
                if last_sent_date == str(current_date):
                    continue

                try:
                    await bot.send_message(
                        user_id,
                        "Напоминание: Не забудьте добавить запись в дневник!"
                    )
                    # Обновляем дату последней отправки
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
            # Если пользователя нет, начинаем регистрацию
            if user is None:
                await state.set_state(RegistrationForm.name)
                await message.answer("Необходимо пройти регистрацию!")
                await message.answer("Введите ваше имя:")
            else:
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
    await message.answer("Запись успешно сохранена!", reply_markup=main_menu)

# --- Обработчик рекомендаций ---
@dp.message(Command(commands=["get_recommendation"]))
@dp.message(lambda m: m.text == "Получить рекомендацию")
async def handle_menu_get_recommendation(message: Message):
    user_id = str(message.from_user.id)

    await message.answer("Сейчас посмотрим, подождите...")

    # Получение последней записи (функция получения записи из БД)
    last_entry = await get_last_diary_entry(int(user_id))
    if not last_entry:
        await message.answer("Нет записей для анализа. Добавьте запись в дневник.")
        return

    situation, thought, emotion, reaction, entry_id = last_entry

    # Формируем цепочку сообщений
    input_messages = [
        HumanMessage(
            content=f"""Ты — квалифицированный психолог с глубоким пониманием когнитивно-поведенческой терапии и эмоционального интеллекта.
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
                Дай рекомендации и советы, адаптированные к этой записи.
                Не более 2000 символов."""
        )
    ]

    try:
        # Передаем в вызов конфигурацию с нужным thread_id,
        # что обеспечивает поддержку отдельных разговоров
        output = await app.ainvoke(
            {"messages": input_messages},
            config={"configurable": {"thread_id": user_id}}
        )
        analysis = output["messages"][-1].content

        # Сохраняем рекомендации в БД (пример)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE diary SET recommendation = ? WHERE id = ?",
                (analysis, entry_id)
            )
            await db.commit()

        await message.answer(analysis, reply_markup=dialog_buttons)

    except Exception as e:
        await message.answer(f"Ошибка анализа: {e}")

# Генерация настроек (InlineKeyboard) динамически
async def generate_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled FROM reminders WHERE user_id = ?", (user_id,)
        ) as cursor:
            result = await cursor.fetchone()
            reminders_enabled = result[0] if result else 0

    buttons = [
        [
            InlineKeyboardButton(
                text="\u2705 Напоминания: Включить" if not reminders_enabled else "\u274C Напоминания: Выключить",
                callback_data="toggle_reminder_on" if not reminders_enabled else "toggle_reminder_off"
            )
        ]
    ]

    if reminders_enabled:
        buttons.append([
            InlineKeyboardButton(
                text="Установить время напоминания",
                callback_data="set_reminder_time"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(lambda c: c.data == "continue_dialog")
async def continue_dialog(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.answer("Продолжайте диалог. Напишите ваш вопрос:", reply_markup=ReplyKeyboardRemove())
    user_id = str(callback_query.from_user.id)
    await state.set_state(DialogForm.in_dialog)
    await callback_query.answer()

@dp.message(DialogForm.in_dialog)
async def dialog_interaction(message: Message):
    input_messages = [HumanMessage(content=message.text)]
    user_id = str(message.from_user.id)
    
    try:
        output = await app.ainvoke(
            {"messages": input_messages},
            config={"configurable": {"thread_id": user_id}}
        )
        response = output["messages"][-1].content
        await message.answer(response, reply_markup=dialog_buttons)
    except Exception as e:
        await message.answer(f"Ошибка во время диалога: {e}")

@dp.callback_query(lambda c: c.data == "end_dialog")
async def end_dialog(callback_query: CallbackQuery, state: FSMContext):
    user_id = str(callback_query.from_user.id)

    # Убираем inline-кнопки
    await callback_query.message.edit_reply_markup(reply_markup=None)

    # Очищаем FSM состояние
    await state.clear()

    # Уведомляем пользователя о завершении
    await callback_query.answer("Диалог завершён.")
    await callback_query.message.answer(
        "Диалог завершён. Если понадобится помощь снова, выберите нужный пункт меню.",
        reply_markup=main_menu
    )

@dp.message(Command(commands=["help"]))
async def cmd_help(message: Message):
    help_text = (
        "Привет! 👋 Я — твой личный дневник и помощник по психическому здоровью.\n"
        "Я помогу фиксировать события, мысли и эмоции, анализировать их и получать полезные рекомендации по КПТ.\n\n"
        "✨ Основные команды:\n"
        "/start — начать работу и зарегистрироваться.\n"
        "/new_entry — добавить новую запись в дневник.\n"
        "/get_recommendation — получить рекомендации.\n"
        "/view_diary — посмотреть дневник.\n"
        "/export_diary — скачать дневник.\n"
        "/feedback — оставить отзыв.\n"
        "/settings — открыть настройки.\n"
        "/help — описание команд.\n\n"

        "🗂 Главное меню:\n"
        "🔹 Добавить запись в дневник – записывай мысли и переживания.\n"
        "🔹 Получить рекомендацию – анализируй свою последнюю запись с помощью искусственного интеллекта.\n"
        "🔹 Посмотреть дневник – перечитывай записи и следи за прогрессом.\n"
        "🔹 Экспортировать дневник – загружай записи в удобном формате.\n"
        "🔹 Оставить отзыв – помоги сделать бота лучше.\n"
        "🔹 Настройки – настрой уведомления, чтобы не забыть заполнить дневник (по умолчанию 18:00, если не указывали ранее)"
    )
    await message.answer(help_text)

@dp.message(lambda m: m.text == "Экспортировать дневник")
@dp.message(Command(commands=["export_diary"]))
async def handle_export_diary(message: Message):
    user_id = message.from_user.id
    
    # Получение всех записей пользователя
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

    file_path = f"tmp/diary_{user_id}.docx"
    document.save(file_path)

    input_file = FSInputFile(file_path)
    await message.answer_document(
        document=input_file,
        caption="Ваш дневник в формате DOCX"
    )
    os.remove(file_path)

@dp.message(lambda m: m.text == "Посмотреть дневник")
@dp.message(Command(commands=["view_diary"]))
async def handle_view_diary(message: Message):
    user_id = message.from_user.id

    # Получение всех записей пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, situation, thought, emotion, reaction, recommendation, created_at
            FROM diary
            WHERE user_id = ?
            ORDER BY created_at ASC
            """,
            (user_id,)
        ) as cursor:
            entries = await cursor.fetchall()

    if not entries:
        await message.answer("Ваш дневник пуст. Добавьте записи, чтобы их увидеть.")
        return

    for entry in entries:
        entry_id, situation, thought, emotion, reaction, recommendation, created_at = entry

        # Формируем текст записи
        diary_text = (
            f"<b>Дата:</b> {created_at}\n"
            f"<b>Ситуация:</b> {situation}\n"
            f"<b>Мысль:</b> {thought}\n"
            f"<b>Эмоция:</b> {emotion}\n"
            f"<b>Реакция:</b> {reaction}\n"
            f"<b>Рекомендация:</b> {recommendation or 'Не получена'}"
        )

        # Создаем inline-кнопку для удаления записи
        delete_button = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить запись",
                    callback_data=f"delete_diary_{entry_id}"
                )
            ]
        ])

        await message.answer(diary_text, reply_markup=delete_button, parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data.startswith("delete_diary_"))
async def handle_delete_diary(callback_query: CallbackQuery):
    entry_id = int(callback_query.data.split("_")[2])

    # Удаляем запись из базы данных
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM diary WHERE id = ?",
            (entry_id,)
        )
        await db.commit()

    # Уведомляем пользователя об успешном удалении
    await callback_query.answer("Запись успешно удалена!")

    # Обновляем сообщение, убирая inline-кнопки
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.message.answer("Запись была удалена.")

@dp.message(lambda m: m.text == "Настройки")
@dp.message(Command(commands=["settings"]))
async def handle_menu_settings(message: Message):
    user_id = message.from_user.id
    settings_menu = await generate_settings_menu(user_id)
    await message.answer("Настройки напоминаний:", reply_markup=settings_menu)

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

    settings_menu = await generate_settings_menu(user_id)
    await callback_query.message.edit_reply_markup(reply_markup=settings_menu)

@dp.callback_query(lambda c: c.data == "set_reminder_time")
async def set_reminder_time(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(ReminderForm.time)
    await callback_query.message.answer(
        "Пожалуйста, укажите время для напоминания в формате ЧЧ:ММ (по умолчанию 18:00)",
        reply_markup=ReplyKeyboardRemove()
    )
    await callback_query.answer()

@dp.message(ReminderForm.time)
async def process_reminder_time(message: Message, state: FSMContext):
    try:
        reminder_time = message.text if message.text else "18:00"
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
        await message.answer(f"Ошибка: {e}. Попробуйте ещё раз в формате ЧЧ:ММ.")

# Анкета обратной связи
class FeedbackForm(StatesGroup):
    feedback = State()

@dp.message(lambda m: m.text == "Оставить отзыв")
@dp.message(Command(commands=["feedback"]))
async def handle_menu_feedback(message: Message, state: FSMContext):
    await state.set_state(FeedbackForm.feedback)
    await message.answer("Пожалуйста, оставьте ваш отзыв:", reply_markup=ReplyKeyboardRemove())

@dp.message(FeedbackForm.feedback)
async def process_feedback(message: Message, state: FSMContext):
    feedback = message.text
    user_id = message.from_user.id

    # Сохранение отзыва в БД
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

@dp.message(lambda message: message.text.lower() in ["годжо сатору", "gojo satoru", "gojo", "satoru gojo",
                                                     "сатору годжо", "годжо", "сатору", "satoru",
                                                     "годжо сатори", "сатори годжо", "сатори"])
async def send_gojo_image(message: Message):
    try:
        # Путь к изображению
        image_path = "pic/gojo.jpg"
        
        # Проверка наличия файла
        if not os.path.exists(image_path):
            await message.answer("Эх, пасхалка потерялась")
            return

        # Отправка изображения пользователю
        photo = FSInputFile(image_path)
        await message.answer_photo(photo, caption="Вот он мистер пасхалкин!")

    except Exception as e:
        await message.answer(f"Ошибка при отправке изображения: {e}")



@dp.message()
async def unknown_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [DiaryForm.situation, DiaryForm.thought, DiaryForm.emotion,
                         DiaryForm.reaction, ReminderForm.time, DialogForm.in_dialog,
                         FeedbackForm.feedback, ReminderForm.time]:
        return

    await message.answer(
        "Ой, кажется вы попали в неизвестное место, попробуйте другую команду или кнопку :)",
        reply_markup=main_menu
    )
# ------------------------------------------------------------------------------
# Инициализация базы данных
# ------------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                age INTEGER,
                email TEXT,
                created_at DATETIME DEFAULT (DATETIME('now', '+3 hours'))
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
                created_at DATETIME DEFAULT (DATETIME('now', '+3 hours')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                time TEXT DEFAULT '18:00',
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
                created_at DATETIME DEFAULT (DATETIME('now', '+3 hours')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        await db.commit()

# ------------------------------------------------------------------------------
# Main Entry Point
# ------------------------------------------------------------------------------
async def main():
    # Инициализируем базу данных, команды бота, планировщик и т.д.
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="new_entry", description="Добавить запись в дневник"),
        BotCommand(command="get_recommendation", description="Получить рекомендацию"),
        BotCommand(command="view_diary", description="Посмотреть дневник"),
        BotCommand(command="export_diary", description="Экспортировать дневник"),
        BotCommand(command="feedback", description="Оставить отзыв"),
        BotCommand(command="settings", description="Открыть настройки"),
        BotCommand(command="help", description="Полное описание команд и кнопок бота")
    ])
    if not scheduler.get_jobs():
        scheduler.remove_all_jobs()
        scheduler.add_job(send_reminders, "cron", second=0)
    if not scheduler.running:
        scheduler.start()
    await dp.start_polling(bot, timeout=60)

if __name__ == "__main__":
    try:
        nest_asyncio.apply()
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error("Бот остановлен!")