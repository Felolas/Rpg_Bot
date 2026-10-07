import html
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any

import telebot
from google import genai
from google.genai import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("telegram-rpg-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KEY_1 = os.environ.get("GEMINI_API_KEY")
KEY_2 = os.environ.get("GEMINI_API_KEY_2")
ACTIVE_KEY = KEY_1 if KEY_1 else KEY_2

if not ACTIVE_KEY:
    raise RuntimeError("Не найден ни один API ключ Gemini.")

ai_client = genai.Client(api_key=ACTIVE_KEY
gemini_clients = [ai_client]

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
BOT_USERNAME = ""

SYSTEM_PROMPT = """
Ты — ведущий текстовой ролевой игры о диких волках.
Пиши только на русском языке. Стиль — реалистичное мрачное тёмное фэнтези:
холод, голод, запахи, раны, усталость и территориальные конфликты имеют последствия.
Не используй вычурный лоск, героические клише и шаблонные пророчества.
Вся история текущего чата — канон лора. Перед каждым ответом учитывай события,
персонажей, места, предметы и последствия. Не противоречь уже установленным фактам.
Не описывай решение или действие игрока за него.
"""

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
user_sessions: dict[int, "GameSession"] = {}
pending_questions: dict[int, dict[str, Any]] = {}
active_rolls: dict[int, "RollState"] = {}
secure_random = random.SystemRandom()


@dataclass
class GameSession:
    chat: Any
    client_index: int


@dataclass
class Outcome:
    start: int
    end: int
    text: str


@dataclass
class RollState:
    question: str
    variant_count: int
    yes_no: bool
    outcomes: list[Outcome]
    roll: int
    message_id: int


def roll_d20() -> int:
    return secure_random.randint(1, 20)


def new_game(client_index: int = 0) -> GameSession:
    chat = gemini_clients[client_index].chats.create(
        model=MODEL_NAME,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.9,
        ),
    )
    return GameSession(chat=chat, client_index=client_index)


def is_retryable_ai_error(error: Exception) -> bool:
    details = str(error)
    return any(
        marker in details
        for marker in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")
    )


def send_gemini_message(chat_id: int, prompt: str) -> Any:
    session = user_sessions[chat_id]
    try:
        return session.chat.send_message(prompt)
    except Exception as error:
        next_client_index = session.client_index + 1
        if (
            not is_retryable_ai_error(error)
            or next_client_index >= len(gemini_clients)
        ):
            raise

        logger.warning(
            "Gemini client %s failed; trying backup client %s.",
            session.client_index + 1,
            next_client_index + 1,
        )
        fallback_session = new_game(next_client_index)
        session.chat = fallback_session.chat
        session.client_index = next_client_index
        return session.chat.send_message(prompt)


def extract_trigger_text(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text:
        return None

    bracket_match = re.search(r"\[([^\[\]\n]{2,})\]", text)
    mentioned = bool(
        BOT_USERNAME
        and re.search(rf"@{re.escape(BOT_USERNAME)}\b", text, re.IGNORECASE)
    )
    if mentioned:
        question = re.sub(
            rf"@{re.escape(BOT_USERNAME)}\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        return question or None

    judo_pattern = r"(?<!\w)д\s*ж\s*у\s*д\s*о(?!\w)"
    if re.search(judo_pattern, text, re.IGNORECASE):
        question = re.sub(
            judo_pattern,
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        return question or None

    if bracket_match:
        question = bracket_match.group(1).strip()
        context = re.sub(r"\[[^\[\]\n]{2,}\]", "", text).strip()
        return f"Контекст поста:\n{context}\n\nВопрос: {question}" if context else question

    return None


def game_prompt(question: str, variant_count: int, yes_no: bool) -> str:
    variant_rule = (
        "Подготовь ровно 2 исхода: один с меткой «ДА», второй с меткой «НЕТ»."
        if yes_no
        else f"Подготовь ровно {variant_count} коротких исхода."
    )
    return f"""
Пересмотри весь лор игровой сессии и ответь на текущий вопрос по существу.
Не пересказывай предыдущую ситуацию и не добавляй вступление.
{variant_rule}
Распредели все значения D20 от 1 до 20 на непрерывные диапазоны без пропусков
и пересечений. Каждый исход — одна короткая строка в формате:
«1–6 — конкретное событие для этой ситуации».
Верни только строки исходов, без заголовка и строки результата.
Весь ответ только на русском языке.

Текущий вопрос или контекст:
{question}
"""


def parse_outcomes(text: str, expected_count: int) -> list[Outcome]:
    line_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(\d{1,2})\s*[-–—]\s*(\d{1,2})"
        r"\s*[-–—:]\s*(.+?)\s*$"
    )
    outcomes: list[Outcome] = []
    for line in text.splitlines():
        match = line_pattern.match(line)
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if 1 <= start <= end <= 20:
            outcomes.append(Outcome(start, end, match.group(3).strip()))

    outcomes.sort(key=lambda item: item.start)
    valid = (
        len(outcomes) == expected_count
        and bool(outcomes)
        and outcomes[0].start == 1
        and outcomes[-1].end == 20
        and all(
            previous.end + 1 == current.start
            for previous, current in zip(outcomes, outcomes[1:])
        )
    )
    if not valid:
        raise ValueError("Gemini вернул исходы в неподдерживаемом формате.")
    return outcomes


def generate_outcomes(
    chat_id: int,
    question: str,
    variant_count: int,
    yes_no: bool,
) -> list[Outcome]:
    response = send_gemini_message(
        chat_id,
        game_prompt(question, variant_count, yes_no),
    )
    return parse_outcomes(response.text or "", 2 if yes_no else variant_count)


def selected_outcome(state: RollState) -> Outcome:
    for outcome in state.outcomes:
        if outcome.start <= state.roll <= outcome.end:
            return outcome
    raise ValueError(f"Бросок {state.roll} не попал в диапазоны.")


def render_roll(state: RollState) -> str:
    lines = [
        f"{outcome.start}–{outcome.end} — {html.escape(outcome.text)}"
        for outcome in state.outcomes
    ]
    outcome = selected_outcome(state)
    lines.append(
        "\n⚠️ <b>Результат: "
        f"{state.roll} — {html.escape(outcome.text)} ⚠️</b>"
    )
    return "\n".join(lines)


def variant_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        *[
            InlineKeyboardButton(str(number), callback_data=f"variants:{number}")
            for number in range(2, 7)
        ]
    )
    keyboard.row(
        *[
            InlineKeyboardButton(str(number), callback_data=f"variants:{number}")
            for number in range(7, 11)
        ]
    )
    keyboard.row(
        InlineKeyboardButton("да/нет", callback_data="variants:yes_no")
    )
    return keyboard


def outcome_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("🎲", callback_data="roll:reroll"),
        InlineKeyboardButton("♻️", callback_data="roll:refresh"),
    )
    return keyboard


def send_error(chat_id: int, message: str) -> None:
    bot.send_message(chat_id, message)


def generation_error_message(error: Exception) -> str:
    details = str(error)
    if "429" in details or "RESOURCE_EXHAUSTED" in details or "quota" in details.lower():
        return (
            "Gemini временно не может обработать ход: у API закончился доступный "
            "лимит запросов."
        )
    if "503" in details or "UNAVAILABLE" in details:
        return (
            "Gemini временно перегружен. Текст не потерян — попробуйте повторить "
            "этот ход через несколько секунд."
        )
    return "Произошла ошибка при подготовке исходов. Попробуйте ещё раз."


@bot.message_handler(commands=["start", "reset"])
def start_game(message: Message) -> None:
    chat_id = message.chat.id
    try:
        user_sessions[chat_id] = new_game()
        pending_questions.pop(chat_id, None)
        active_rolls.pop(chat_id, None)
        command = (message.text or "").split()[0].split("@")[0].lower()
        bot.send_message(chat_id, "🔃" if command == "/reset" else "✅")
    except Exception as error:
        logger.exception("Start failed for chat %s: %s", chat_id, error)
        bot.reply_to(message, "Не удалось запустить игру. Проверьте настройки.")


@bot.message_handler(commands=["help"])
def show_help(message: Message) -> None:
    bot.reply_to(
        message,
        "Напишите /start, чтобы начать игру.\n"
        "В группе: @имя_бота вопрос, «Джудо вопрос» или вопрос в [квадратных скобках].\n"
        "В слове «Джудо» регистр и пробелы не важны.\n"
        "После вопроса выберите число исходов.",
    )


@bot.callback_query_handler(
    func=lambda call: bool(call.data and call.data.startswith("variants:"))
)
def handle_variant_choice(call: Any) -> None:
    chat_id = call.message.chat.id
    pending = pending_questions.pop(chat_id, None)
    if pending is None:
        bot.answer_callback_query(call.id, "Вопрос уже обработан или устарел.")
        return
    if chat_id not in user_sessions:
        bot.answer_callback_query(call.id, "Сначала начните игру командой /start.")
        return

    choice = call.data.removeprefix("variants:")
    yes_no = choice == "yes_no"
    variant_count = 2 if yes_no else int(choice)

    try:
        bot.answer_callback_query(call.id)
        bot.delete_message(chat_id, pending["message_id"])
        outcomes = generate_outcomes(
            chat_id,
            pending["question"],
            variant_count,
            yes_no,
        )
        state = RollState(
            question=pending["question"],
            variant_count=variant_count,
            yes_no=yes_no,
            outcomes=outcomes,
            roll=roll_d20(),
            message_id=0,
        )
        result_message = bot.send_message(
            chat_id,
            render_roll(state),
            parse_mode="HTML",
            reply_markup=outcome_keyboard(),
        )
        state.message_id = result_message.message_id
        active_rolls[chat_id] = state
    except Exception as error:
        logger.exception("Variant choice failed for chat %s: %s", chat_id, error)
        send_error(chat_id, generation_error_message(error))


@bot.callback_query_handler(
    func=lambda call: call.data in {"roll:reroll", "roll:refresh"}
)
def handle_roll_action(call: Any) -> None:
    chat_id = call.message.chat.id
    state = active_rolls.get(chat_id)
    if state is None:
        bot.answer_callback_query(call.id, "Результат уже устарел.")
        return

    try:
        bot.answer_callback_query(call.id)
        if call.data == "roll:reroll":
            new_roll = roll_d20()
            while new_roll == state.roll:
                new_roll = roll_d20()
            state.roll = new_roll
        else:
            state.outcomes = generate_outcomes(
                chat_id,
                state.question,
                state.variant_count,
                state.yes_no,
            )
            state.roll = roll_d20()

        bot.edit_message_text(
            render_roll(state),
            chat_id,
            state.message_id,
            parse_mode="HTML",
            reply_markup=outcome_keyboard(),
        )
    except Exception as error:
        logger.exception("Roll action failed for chat %s: %s", chat_id, error)
        send_error(chat_id, generation_error_message(error))


@bot.message_handler(func=lambda message: True)
def handle_game_turn(message: Message) -> None:
    chat_id = message.chat.id
    question = extract_trigger_text(message)
    if question is None:
        return
    if chat_id not in user_sessions:
        bot.reply_to(message, "Чтобы начать игру, напишите /start")
        return

    previous = pending_questions.get(chat_id)
    if previous:
        try:
            bot.delete_message(chat_id, previous["message_id"])
        except Exception:
            logger.info("Previous variant selector was already unavailable.")

    selector = bot.reply_to(message, "❔", reply_markup=variant_keyboard())
    pending_questions[chat_id] = {
        "question": question,
        "message_id": selector.message_id,
    }


def main() -> None:
    global BOT_USERNAME
    BOT_USERNAME = (bot.get_me().username or "").strip()
    logger.info("Бот-гейммастер запущен: @%s. Модель: %s", BOT_USERNAME, MODEL_NAME)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    # Запускаємо фейковий веб-сервер для безкоштовного тарифу Render
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive!")

        def log_message(self, format, *args):
            return  # вимикаємо зайві логи сервера

    def run_web_server():
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(('0.0.0.0', port), SimpleHandler)
        server.serve_forever()

    # Запускаємо сервер в окремому потоці
    threading.Thread(target=run_web_server, daemon=True).start()
    
    # Запускаємо нашого бота
    main()
    
