from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import asyncio
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode

# ========================
# CONFIG
# ========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "6582678746:AAFcuzidqLZ3gJqwjaEU1SrKNm8mGNwoBCM")

# Replace with your Telegram user ID(s)
ADMIN_IDS = {8226659957}  # set of ints


# ========================
# DATABASE HELPER
# ========================

DB_PATH = "questions.db"
#DB_PATH = "/data/questions.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(get_db_connection()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board TEXT,
                year INTEGER,
                exam TEXT,
                subject TEXT,
                topic TEXT,
                subtopic TEXT,
                question_text TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                option4 TEXT NOT NULL,
                correct_option INTEGER NOT NULL,
                explanation TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id INTEGER PRIMARY KEY,
                board TEXT,
                year TEXT,
                exam TEXT,
                subject TEXT,
                topic TEXT,
                subtopic TEXT
            )
            """
        )


def get_or_create_user_filters(user_id: int) -> dict:
    with closing(get_db_connection()) as conn, conn:
        cur = conn.execute(
            "SELECT * FROM user_filters WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        conn.execute(
            "INSERT INTO user_filters (user_id, board, year, exam, subject, topic, subtopic) "
            "VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL)",
            (user_id,),
        )
        return {
            "user_id": user_id,
            "board": None,
            "year": None,
            "exam": None,
            "subject": None,
            "topic": None,
            "subtopic": None,
        }


def update_user_filter(user_id: int, field: str, value: str | None):
    # all fields that can be set from UI
    if field not in {"board", "year", "exam", "subject", "topic", "subtopic"}:
        return
    with closing(get_db_connection()) as conn, conn:
        conn.execute(
            f"UPDATE user_filters SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )


def reset_user_filters(user_id: int):
    with closing(get_db_connection()) as conn, conn:
        conn.execute(
            "UPDATE user_filters "
            "SET board = NULL, year = NULL, exam = NULL, "
            "subject = NULL, topic = NULL, subtopic = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )


def get_distinct_values(field: str, filters: dict | None = None) -> list[str]:
    """
    field: 'board' | 'year' | 'exam' | 'subject' | 'topic' | 'subtopic'
    filters: can restrict by previous selections
    """
    if field not in {"board", "year", "exam", "subject", "topic", "subtopic"}:
        return []

    clauses = []
    params: list = []

    if filters:
        # Narrow down based on currently chosen filters
        for f_name in ["board", "year", "exam", "subject", "topic", "subtopic"]:
            if f_name in filters and filters[f_name] and f_name != field:
                clauses.append(f"{f_name} = ?")
                params.append(filters[f_name])

    # Don't return empty/null values
    clauses.append(f"{field} IS NOT NULL")
    clauses.append(f"{field} != ''")

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    with closing(get_db_connection()) as conn, conn:
        cur = conn.execute(
            f"SELECT DISTINCT {field} FROM questions {where_sql}",
            params,
        )
        return [str(row[field]) for row in cur.fetchall() if row[field] is not None]


def get_questions_for_filters(filters: dict, limit: int = 10) -> list[sqlite3.Row]:
    where_clauses = []
    params: list = []

    for field in ["board", "year", "exam", "subject", "topic", "subtopic"]:
        val = filters.get(field)
        if val:
            where_clauses.append(f"{field} = ?")
            params.append(val)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    sql = (
        "SELECT * FROM questions "
        f"{where_sql} "
        "ORDER BY RANDOM() "
        "LIMIT ?"
    )
    params.append(limit)
    with closing(get_db_connection()) as conn, conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def insert_question(data: dict) -> int:
    with closing(get_db_connection()) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO questions (
                board, year, exam, subject, topic, subtopic,
                question_text,
                option1, option2, option3, option4,
                correct_option, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("board"),
                data.get("year"),
                data.get("exam"),
                data.get("subject"),
                data.get("topic"),
                data.get("subtopic"),
                data["question_text"],
                data["option1"],
                data["option2"],
                data["option3"],
                data["option4"],
                data["correct_option"],
                data.get("explanation"),
            ),
        )
        return cur.lastrowid


# ========================
# FSM FOR ADMIN ADD QUESTION
# ========================

class AddQuestion(StatesGroup):
    waiting_board = State()
    waiting_year = State()
    waiting_exam = State()
    waiting_subject = State()
    waiting_topic = State()
    waiting_subtopic = State()
    waiting_question_text = State()
    waiting_option1 = State()
    waiting_option2 = State()
    waiting_option3 = State()
    waiting_option4 = State()
    waiting_correct_option = State()
    waiting_explanation = State()
    waiting_confirm = State()


# ========================
# KEYBOARDS
# ========================

def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    filters = get_or_create_user_filters(user_id)

    def val_or_dash(v):
        return v if v else "All"

    text_board = f"📚 Board: {val_or_dash(filters.get('board'))}"
    text_year = f"📅 Year: {val_or_dash(filters.get('year'))}"
    text_exam = f"🧪 Exam: {val_or_dash(filters.get('exam'))}"
    text_subject = f"📖 Subject: {val_or_dash(filters.get('subject'))}"
    text_topic = f"🧩 Topic: {val_or_dash(filters.get('topic'))}"
    text_subtopic = f"🔹 Subtopic: {val_or_dash(filters.get('subtopic'))}"

    kb = [
        [InlineKeyboardButton(text=text_board, callback_data="choose_board")],
        [InlineKeyboardButton(text=text_year, callback_data="choose_year")],
        [InlineKeyboardButton(text=text_exam, callback_data="choose_exam")],
        [InlineKeyboardButton(text=text_subject, callback_data="choose_subject")],
        [InlineKeyboardButton(text=text_topic, callback_data="choose_topic")],
        [InlineKeyboardButton(text=text_subtopic, callback_data="choose_subtopic")],
        [InlineKeyboardButton(text="🎯 Generate quiz", callback_data="generate_quiz")],
        [InlineKeyboardButton(text="♻️ Reset filters", callback_data="reset_filters")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def values_list_kb(prefix: str, values: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for v in values:
        rows.append(
            [
                InlineKeyboardButton(
                    text=v,
                    callback_data=f"{prefix}:{v}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_main")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ========================
# BOT & DISPATCHER
# ========================

#bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
from aiogram.client.default import DefaultBotProperties

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()


# ========================
# HANDLERS – GENERAL
# ========================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    get_or_create_user_filters(message.from_user.id)
    await message.answer(
        "👋 Welcome!\n"
        "Use the buttons below to set filters and generate PYQ quizzes.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ You are not an admin.")
        return
    await message.answer(
        "👑 Admin panel:\n"
        "/addquestion – add new PYQ\n"
        "(you can extend with more commands later)"
    )


# ========================
# ADMIN – ADD QUESTION FLOW
# ========================

@dp.message(Command("addquestion"))
async def cmd_addquestion(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ You are not an admin.")
        return

    await state.clear()
    await state.set_state(AddQuestion.waiting_board)
    await message.answer("📝 Adding new question.\n\nSend <b>Board</b> (e.g. GSEB, CBSE):")


@dp.message(AddQuestion.waiting_board)
async def addq_board(message: Message, state: FSMContext):
    await state.update_data(board=message.text.strip())
    await state.set_state(AddQuestion.waiting_year)
    await message.answer("Send <b>Year</b> (e.g. 2023). If not applicable, send 0:")


@dp.message(AddQuestion.waiting_year)
async def addq_year(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        year = int(text)
    except ValueError:
        await message.answer("❌ Please send a valid number for year (e.g. 2023 or 0).")
        return
    await state.update_data(year=year)
    await state.set_state(AddQuestion.waiting_exam)
    await message.answer("Send <b>Exam name</b> (e.g. Talati, DYSO):")


@dp.message(AddQuestion.waiting_exam)
async def addq_exam(message: Message, state: FSMContext):
    await state.update_data(exam=message.text.strip())
    await state.set_state(AddQuestion.waiting_subject)
    await message.answer("Send <b>Subject</b> (e.g. Polity, Science):")


@dp.message(AddQuestion.waiting_subject)
async def addq_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddQuestion.waiting_topic)
    await message.answer("Send <b>Topic</b>:")


@dp.message(AddQuestion.waiting_topic)
async def addq_topic(message: Message, state: FSMContext):
    await state.update_data(topic=message.text.strip())
    await state.set_state(AddQuestion.waiting_subtopic)
    await message.answer("Send <b>Subtopic</b> (or '-' if not used):")


@dp.message(AddQuestion.waiting_subtopic)
async def addq_subtopic(message: Message, state: FSMContext):
    subtopic = message.text.strip()
    if subtopic == "-":
        subtopic = ""
    await state.update_data(subtopic=subtopic)
    await state.set_state(AddQuestion.waiting_question_text)
    await message.answer("Send the <b>Question text</b>:")


@dp.message(AddQuestion.waiting_question_text)
async def addq_question_text(message: Message, state: FSMContext):
    await state.update_data(question_text=message.text.strip())
    await state.set_state(AddQuestion.waiting_option1)
    await message.answer("Send <b>Option 1</b>:")


@dp.message(AddQuestion.waiting_option1)
async def addq_option1(message: Message, state: FSMContext):
    await state.update_data(option1=message.text.strip())
    await state.set_state(AddQuestion.waiting_option2)
    await message.answer("Send <b>Option 2</b>:")


@dp.message(AddQuestion.waiting_option2)
async def addq_option2(message: Message, state: FSMContext):
    await state.update_data(option2=message.text.strip())
    await state.set_state(AddQuestion.waiting_option3)
    await message.answer("Send <b>Option 3</b>:")


@dp.message(AddQuestion.waiting_option3)
async def addq_option3(message: Message, state: FSMContext):
    await state.update_data(option3=message.text.strip())
    await state.set_state(AddQuestion.waiting_option4)
    await message.answer("Send <b>Option 4</b>:")


@dp.message(AddQuestion.waiting_option4)
async def addq_option4(message: Message, state: FSMContext):
    await state.update_data(option4=message.text.strip())
    await state.set_state(AddQuestion.waiting_correct_option)
    await message.answer("Send <b>correct option number</b> (1–4):")


@dp.message(AddQuestion.waiting_correct_option)
async def addq_correct_option(message: Message, state: FSMContext):
    text = message.text.strip()
    if text not in {"1", "2", "3", "4"}:
        await message.answer("❌ Please send a number between 1 and 4.")
        return
    await state.update_data(correct_option=int(text))
    await state.set_state(AddQuestion.waiting_explanation)
    await message.answer("Send <b>Explanation</b> (or '-' to skip):")


@dp.message(AddQuestion.waiting_explanation)
async def addq_explanation(message: Message, state: FSMContext):
    expl = message.text.strip()
    if expl == "-":
        expl = ""
    await state.update_data(explanation=expl)

    data = await state.get_data()
    preview = (
        "<b>Preview:</b>\n"
        f"Board: {data.get('board')}\n"
        f"Year: {data.get('year')}\n"
        f"Exam: {data.get('exam')}\n"
        f"Subject: {data.get('subject')}\n"
        f"Topic: {data.get('topic')}\n"
        f"Subtopic: {data.get('subtopic')}\n\n"
        f"<b>Q:</b> {data.get('question_text')}\n"
        f"1️⃣ {data.get('option1')}\n"
        f"2️⃣ {data.get('option2')}\n"
        f"3️⃣ {data.get('option3')}\n"
        f"4️⃣ {data.get('option4')}\n"
        f"✅ Correct: {data.get('correct_option')}\n\n"
        f"Explanation: {data.get('explanation') or '(none)'}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Save", callback_data="addq_save"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="addq_cancel"),
            ]
        ]
    )

    await state.set_state(AddQuestion.waiting_confirm)
    await message.answer(preview, reply_markup=kb)


@dp.callback_query(AddQuestion.waiting_confirm, F.data == "addq_cancel")
async def addq_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Question creation cancelled.")
    await cb.answer()


@dp.callback_query(AddQuestion.waiting_confirm, F.data == "addq_save")
async def addq_save(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    qid = insert_question(data)
    await state.clear()
    await cb.message.edit_text(f"✅ Question saved with ID <b>{qid}</b>.")
    await cb.answer()


# ========================
# USER FILTERS HANDLERS
# ========================

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(cb: CallbackQuery):
    await cb.message.edit_text(
        "Use the buttons below to set filters and generate PYQ quizzes:",
        reply_markup=main_menu_kb(cb.from_user.id),
    )
    await cb.answer()


@dp.callback_query(F.data == "reset_filters")
async def cb_reset_filters(cb: CallbackQuery):
    reset_user_filters(cb.from_user.id)
    await cb.message.edit_text(
        "♻️ Filters reset.\n\nUse buttons to set filters:",
        reply_markup=main_menu_kb(cb.from_user.id),
    )
    await cb.answer("Filters cleared.")


@dp.callback_query(F.data == "choose_board")
async def cb_choose_board(cb: CallbackQuery):
    values = get_distinct_values("board")
    if not values:
        await cb.answer("No boards in database yet.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Board</b>:", reply_markup=values_list_kb("set_board", values)
    )
    await cb.answer()

@dp.callback_query(F.data == "choose_year")
async def cb_choose_year(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    values = get_distinct_values("year", filters)
    if not values:
        await cb.answer("No years for current filters.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Year</b>:", reply_markup=values_list_kb("set_year", values)
    )
    await cb.answer()


@dp.callback_query(F.data == "choose_exam")
async def cb_choose_exam(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    values = get_distinct_values("exam", filters)
    if not values:
        await cb.answer("No exams for current filters.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Exam</b>:", reply_markup=values_list_kb("set_exam", values)
    )
    await cb.answer()


@dp.callback_query(F.data == "choose_subject")
async def cb_choose_subject(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    values = get_distinct_values("subject", filters)
    if not values:
        await cb.answer("No subjects for current filters.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Subject</b>:", reply_markup=values_list_kb("set_subject", values)
    )
    await cb.answer()


@dp.callback_query(F.data == "choose_topic")
async def cb_choose_topic(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    values = get_distinct_values("topic", filters)
    if not values:
        await cb.answer("No topics for current filters.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Topic</b>:", reply_markup=values_list_kb("set_topic", values)
    )
    await cb.answer()

@dp.callback_query(F.data == "choose_subtopic")
async def cb_choose_subtopic(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    values = get_distinct_values("subtopic", filters)
    if not values:
        await cb.answer("No subtopics for current filters.", show_alert=True)
        return
    await cb.message.edit_text(
        "Select <b>Subtopic</b>:", reply_markup=values_list_kb("set_subtopic", values)
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("set_board:"))
async def cb_set_board(cb: CallbackQuery):
    value = cb.data.split("set_board:", 1)[1]
    update_user_filter(cb.from_user.id, "board", value)
    await cb.answer("Board set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )

@dp.callback_query(F.data.startswith("set_year:"))
async def cb_set_year(cb: CallbackQuery):
    value = cb.data.split("set_year:", 1)[1]
    update_user_filter(cb.from_user.id, "year", value)
    await cb.answer("Year set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )


@dp.callback_query(F.data.startswith("set_exam:"))
async def cb_set_exam(cb: CallbackQuery):
    value = cb.data.split("set_exam:", 1)[1]
    update_user_filter(cb.from_user.id, "exam", value)
    await cb.answer("Exam set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )


@dp.callback_query(F.data.startswith("set_subject:"))
async def cb_set_subject(cb: CallbackQuery):
    value = cb.data.split("set_subject:", 1)[1]
    update_user_filter(cb.from_user.id, "subject", value)
    await cb.answer("Subject set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )


@dp.callback_query(F.data.startswith("set_topic:"))
async def cb_set_topic(cb: CallbackQuery):
    value = cb.data.split("set_topic:", 1)[1]
    update_user_filter(cb.from_user.id, "topic", value)
    await cb.answer("Topic set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )

@dp.callback_query(F.data.startswith("set_subtopic:"))
async def cb_set_subtopic(cb: CallbackQuery):
    value = cb.data.split("set_subtopic:", 1)[1]
    update_user_filter(cb.from_user.id, "subtopic", value)
    await cb.answer("Subtopic set.")
    await cb.message.edit_text(
        "Filters updated:", reply_markup=main_menu_kb(cb.from_user.id)
    )


# ========================
# GENERATE QUIZ
# ========================

@dp.callback_query(F.data == "generate_quiz")
async def cb_generate_quiz(cb: CallbackQuery):
    filters = get_or_create_user_filters(cb.from_user.id)
    questions = get_questions_for_filters(filters, limit=10)

    if not questions:
        await cb.answer("No questions for these filters.", show_alert=True)
        return

    await cb.answer("Sending questions...")
    await cb.message.answer(
        f"🎯 Found <b>{len(questions)}</b> questions. Sending as quiz polls..."
    )

    for q in questions:
        options = [q["option1"], q["option2"], q["option3"], q["option4"]]
        correct_idx = int(q["correct_option"]) - 1  # convert 1-4 -> 0-3

        await cb.message.answer_poll(
            question=q["question_text"],
            options=options,
            type="quiz",
            correct_option_id=correct_idx,
            explanation=q["explanation"] or None,
            is_anonymous=False,
        )


# ========================
# MAIN
# ========================

async def main():
    logging.basicConfig(level=logging.INFO)

    # Initialize database
    init_db()

    # Create aiohttp web app
    app = web.Application()

    # Register Telegram webhook handler on path /webhook
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path="/webhook")

    # Let aiogram attach its startup/shutdown handlers
    setup_application(app, dp, bot=bot)

    return app


if __name__ == "__main__":
    web.run_app(
        main(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
