import json
import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

BOSSES_FILE = "bosses.json"
DATA_DIR = os.environ.get("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "data.json")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("ragnarok-boss-bot")


def load_bosses():
    with open(BOSSES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


BOSSES = load_bosses()


def resolve_boss(code: str):
    code = code.lower().strip()
    return code if code in BOSSES else None


# ---------- keyboards ----------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚔️ Отметить убийство", callback_data="kill_menu")],
            [InlineKeyboardButton("📊 Мой статус", callback_data="status")],
            [InlineKeyboardButton("📋 Список боссов", callback_data="list")],
            [InlineKeyboardButton("🗑 Отменить отметку", callback_data="cancel_menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main")]])


def boss_pick_keyboard(prefix: str, codes=None) -> InlineKeyboardMarkup:
    codes = codes or BOSSES.keys()
    rows = [
        [InlineKeyboardButton(f"⚔️ {BOSSES[code]['name']}", callback_data=f"{prefix}:{code}")]
        for code in codes
    ]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)


# ---------- text rendering ----------

def render_boss_list() -> str:
    lines = ["📋 <b>Боссы</b>\n"]
    for code, b in BOSSES.items():
        lines.append(
            f"⚔️ <b>{b['name']}</b>  <code>/{code}</code>\n"
            f"    🗺 {b['location']} ({b['map']})\n"
            f"    ⏱ респавн {b['min_minutes']}–{b['max_minutes']} мин\n"
        )
    return "\n".join(lines)


def render_status(chat_id: int) -> str:
    data = load_data()
    chat_entries = data.get(str(chat_id), {})
    now = datetime.now()
    lines = ["📊 <b>Твой статус</b>\n"]
    for code, b in BOSSES.items():
        entry = chat_entries.get(code)
        if not entry:
            lines.append(f"⚪ <b>{b['name']}</b> — нет отметки")
            continue
        start_at = datetime.fromisoformat(entry["start_at"])
        end_at = datetime.fromisoformat(entry["end_at"])
        if now < start_at:
            mins = int((start_at - now).total_seconds() // 60)
            lines.append(f"🟡 <b>{b['name']}</b> — окно через {mins} мин ({start_at.strftime('%H:%M')})")
        elif now <= end_at:
            lines.append(f"🟢 <b>{b['name']}</b> — окно ОТКРЫТО, до {end_at.strftime('%H:%M')}")
        else:
            lines.append(f"🔴 <b>{b['name']}</b> — окно прошло ({end_at.strftime('%H:%M')}), отметь заново")
    return "\n".join(lines)


WELCOME_TEXT = (
    "🐲 <b>Ragnarok Boss Timer</b>\n\n"
    "Слежу за респавном боссов лично для тебя — твои отметки и напоминания "
    "не пересекаются с другими игроками.\n\n"
    "Напомню за 10 минут до открытия окна и в момент, когда оно откроется.\n\n"
    "Выбери действие ниже 👇"
)


def job_name(chat_id: int, code: str, kind: str) -> str:
    return f"{chat_id}:{code}:{kind}"


def schedule_for_boss(app: Application, chat_id: int, code: str, killed_at: datetime):
    b = BOSSES[code]
    warn_at = killed_at + timedelta(minutes=b["min_minutes"] - 10)
    start_at = killed_at + timedelta(minutes=b["min_minutes"])
    end_at = killed_at + timedelta(minutes=b["max_minutes"])
    now = datetime.now()

    for job in (
        app.job_queue.get_jobs_by_name(job_name(chat_id, code, "warn"))
        + app.job_queue.get_jobs_by_name(job_name(chat_id, code, "start"))
    ):
        job.schedule_removal()

    if warn_at > now:
        app.job_queue.run_once(
            send_warning, when=warn_at, chat_id=chat_id, name=job_name(chat_id, code, "warn"), data=code
        )
    if start_at > now:
        app.job_queue.run_once(
            send_window_start,
            when=start_at,
            chat_id=chat_id,
            name=job_name(chat_id, code, "start"),
            data={"code": code, "end_at": end_at},
        )
    return warn_at, start_at, end_at


async def send_warning(context: ContextTypes.DEFAULT_TYPE):
    code = context.job.data
    b = BOSSES[code]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏳ <b>{b['name']}</b> — окно респавна откроется через 10 минут ({b['location']}).",
        parse_mode=ParseMode.HTML,
    )


async def send_window_start(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data
    code = payload["code"]
    end_at = payload["end_at"]
    b = BOSSES[code]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            f"🔔 <b>{b['name']}</b> — окно респавна открыто! ({b['location']})\n"
            f"Может появиться в любой момент до {end_at.strftime('%H:%M')}."
        ),
        parse_mode=ParseMode.HTML,
    )


def record_kill(app: Application, chat_id: int, code: str, killed_at: datetime):
    warn_at, start_at, end_at = schedule_for_boss(app, chat_id, code, killed_at)
    data = load_data()
    chat_key = str(chat_id)
    data.setdefault(chat_key, {})[code] = {
        "killed_at": killed_at.isoformat(),
        "warn_at": warn_at.isoformat(),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
    }
    save_data(data)
    return start_at, end_at


def clear_kill(app: Application, chat_id: int, code: str):
    for job in (
        app.job_queue.get_jobs_by_name(job_name(chat_id, code, "warn"))
        + app.job_queue.get_jobs_by_name(job_name(chat_id, code, "start"))
    ):
        job.schedule_removal()
    data = load_data()
    chat_key = str(chat_id)
    if chat_key in data and code in data[chat_key]:
        del data[chat_key][code]
        save_data(data)


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def list_bosses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(render_boss_list(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        render_status(update.effective_chat.id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
    )


async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /kill <код> [ЧЧ:ММ]. Смотри /list")
        return

    code = resolve_boss(context.args[0])
    if code is None:
        await update.message.reply_text(f"Неизвестный код босса: {context.args[0]}. Смотри /list")
        return

    if len(context.args) > 1:
        try:
            hh, mm = map(int, context.args[1].split(":"))
            now = datetime.now()
            killed_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if killed_at > now:
                killed_at -= timedelta(days=1)
        except ValueError:
            await update.message.reply_text("Неверный формат времени. Используй ЧЧ:ММ, например 14:35")
            return
    else:
        killed_at = datetime.now()

    start_at, end_at = record_kill(context.application, update.effective_chat.id, code, killed_at)
    b = BOSSES[code]
    await update.message.reply_text(
        f"✅ <b>{b['name']}</b> отмечен убитым в {killed_at.strftime('%H:%M')}.\n"
        f"Напомню за 10 мин до окна и в момент открытия ({start_at.strftime('%H:%M')}–{end_at.strftime('%H:%M')}).",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /cancel <код>")
        return
    code = resolve_boss(context.args[0])
    if code is None:
        await update.message.reply_text(f"Неизвестный код босса: {context.args[0]}. Смотри /list")
        return

    clear_kill(context.application, update.effective_chat.id, code)
    await update.message.reply_text(
        f"🗑 Отметка и напоминания для <b>{BOSSES[code]['name']}</b> отменены.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


# ---------- callback (inline menu) ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "main":
        await query.edit_message_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

    elif data == "list":
        await query.edit_message_text(render_boss_list(), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())

    elif data == "status":
        await query.edit_message_text(
            render_status(chat_id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
        )

    elif data == "kill_menu":
        await query.edit_message_text(
            "⚔️ Кого убили только что?", reply_markup=boss_pick_keyboard("kill")
        )

    elif data.startswith("kill:"):
        code = data.split(":", 1)[1]
        if code not in BOSSES:
            await query.edit_message_text("Неизвестный босс.", reply_markup=main_menu_keyboard())
            return
        killed_at = datetime.now()
        start_at, end_at = record_kill(context.application, chat_id, code, killed_at)
        b = BOSSES[code]
        await query.edit_message_text(
            f"✅ <b>{b['name']}</b> отмечен убитым в {killed_at.strftime('%H:%M')}.\n"
            f"Напомню за 10 мин до окна и в момент открытия ({start_at.strftime('%H:%M')}–{end_at.strftime('%H:%M')}).\n\n"
            f"Если время не совпадает с реальным — уточни через <code>/kill {code} ЧЧ:ММ</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    elif data == "cancel_menu":
        active = [c for c in BOSSES if c in load_data().get(str(chat_id), {})]
        if not active:
            await query.edit_message_text("Нет активных отметок для отмены.", reply_markup=back_keyboard())
            return
        await query.edit_message_text("🗑 Что отменить?", reply_markup=boss_pick_keyboard("cancel", active))

    elif data.startswith("cancel:"):
        code = data.split(":", 1)[1]
        clear_kill(context.application, chat_id, code)
        await query.edit_message_text(
            f"🗑 Отметка для <b>{BOSSES[code]['name']}</b> отменена.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )


def reschedule_pending(app: Application):
    data = load_data()
    now = datetime.now()
    restored = 0
    for chat_key, entries in data.items():
        chat_id = int(chat_key)
        for code, entry in entries.items():
            end_at = datetime.fromisoformat(entry["end_at"])
            if end_at < now:
                continue
            killed_at = datetime.fromisoformat(entry["killed_at"])
            schedule_for_boss(app, chat_id, code, killed_at)
            restored += 1
    log.info("Restored %d pending boss timers across all users", restored)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "Главное меню"),
            BotCommand("kill", "Отметить убийство: /kill код [ЧЧ:ММ]"),
            BotCommand("status", "Мой статус по всем боссам"),
            BotCommand("list", "Список боссов и кодов"),
            BotCommand("cancel", "Отменить отметку: /cancel код"),
        ]
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN в .env или переменных окружения")

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("list", list_bosses))
    app.add_handler(CommandHandler("kill", kill))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_callback))

    reschedule_pending(app)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
