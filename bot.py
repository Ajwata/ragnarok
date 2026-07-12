import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
CUSTOM_BOSSES_FILE = os.path.join(DATA_DIR, "custom_bosses.json")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Kyiv"))

ADDBOSS_FORMAT = "код|Имя|карта|локация|мин_минут|макс_минут"
ADDBOSS_EXAMPLE = "baphomet|Baphomet|prt_maze03|Maze Dungeon 3F|60|70"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("ragnarok-boss-bot")


def now() -> datetime:
    return datetime.now(TZ)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_bosses():
    return load_json(BOSSES_FILE, {})


def load_data():
    return load_json(DATA_FILE, {})


def save_data(data):
    save_json(DATA_FILE, data)


def load_custom_bosses():
    return load_json(CUSTOM_BOSSES_FILE, {})


def save_custom_bosses(data):
    save_json(CUSTOM_BOSSES_FILE, data)


BOSSES = load_bosses()  # built-in defaults, shared starting point for everyone


def effective_bosses(chat_id: int) -> dict:
    """Built-in bosses plus this chat's own additions/overrides."""
    merged = dict(BOSSES)
    merged.update(load_custom_bosses().get(str(chat_id), {}))
    return merged


def resolve_boss(chat_id: int, code: str):
    code = code.lower().strip()
    bosses = effective_bosses(chat_id)
    return code if code in bosses else None


# ---------- keyboards ----------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚔️ Отметить убийство", callback_data="kill_menu")],
            [InlineKeyboardButton("📊 Мой статус", callback_data="status")],
            [InlineKeyboardButton("📋 Список боссов", callback_data="list")],
            [InlineKeyboardButton("🗑 Отменить отметку", callback_data="cancel_menu")],
            [InlineKeyboardButton("⚙️ Мои боссы", callback_data="myboss_menu")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main")]])


def boss_pick_keyboard(chat_id: int, prefix: str, codes=None) -> InlineKeyboardMarkup:
    bosses = effective_bosses(chat_id)
    codes = codes or bosses.keys()
    rows = [
        [InlineKeyboardButton(f"⚔️ {bosses[code]['name']}", callback_data=f"{prefix}:{code}")]
        for code in codes
    ]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def myboss_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    custom = load_custom_bosses().get(str(chat_id), {})
    rows = [
        [InlineKeyboardButton(f"🗑 {b['name']}", callback_data=f"delboss:{code}")]
        for code, b in custom.items()
    ]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)


# ---------- text rendering ----------

def render_boss_list(chat_id: int) -> str:
    custom_codes = load_custom_bosses().get(str(chat_id), {}).keys()
    bosses = effective_bosses(chat_id)
    lines = ["📋 <b>Боссы</b>\n"]
    for code, b in bosses.items():
        mark = "✏️ " if code in custom_codes else ""
        lines.append(
            f"{mark}⚔️ <b>{b['name']}</b>  <code>{code}</code>\n"
            f"    🗺 {b['location']} ({b['map']})\n"
            f"    ⏱ респавн {b['min_minutes']}–{b['max_minutes']} мин\n"
        )
    lines.append("✏️ — твой добавленный/изменённый босс. Смотри ⚙️ Мои боссы, чтобы добавить свой.")
    return "\n".join(lines)


def render_status(chat_id: int) -> str:
    data = load_data()
    chat_entries = data.get(str(chat_id), {})
    bosses = effective_bosses(chat_id)
    current = now()
    lines = ["📊 <b>Твой статус</b>\n"]
    for code, b in bosses.items():
        entry = chat_entries.get(code)
        if not entry:
            lines.append(f"⚪ <b>{b['name']}</b> — нет отметки")
            continue
        start_at = datetime.fromisoformat(entry["start_at"])
        end_at = datetime.fromisoformat(entry["end_at"])
        if current < start_at:
            mins = int((start_at - current).total_seconds() // 60)
            lines.append(f"🟡 <b>{b['name']}</b> — окно через {mins} мин ({start_at.strftime('%H:%M')})")
        elif current <= end_at:
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


def schedule_for_boss(app: Application, chat_id: int, code: str, boss: dict, killed_at: datetime):
    warn_at = killed_at + timedelta(minutes=boss["min_minutes"] - 10)
    start_at = killed_at + timedelta(minutes=boss["min_minutes"])
    end_at = killed_at + timedelta(minutes=boss["max_minutes"])
    current = now()

    for job in (
        app.job_queue.get_jobs_by_name(job_name(chat_id, code, "warn"))
        + app.job_queue.get_jobs_by_name(job_name(chat_id, code, "start"))
    ):
        job.schedule_removal()

    if warn_at > current:
        app.job_queue.run_once(
            send_warning,
            when=warn_at,
            chat_id=chat_id,
            name=job_name(chat_id, code, "warn"),
            data={"name": boss["name"], "location": boss["location"]},
        )
    if start_at > current:
        app.job_queue.run_once(
            send_window_start,
            when=start_at,
            chat_id=chat_id,
            name=job_name(chat_id, code, "start"),
            data={"name": boss["name"], "location": boss["location"], "end_at": end_at},
        )
    return warn_at, start_at, end_at


async def send_warning(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏳ <b>{payload['name']}</b> — окно респавна откроется через 10 минут ({payload['location']}).",
        parse_mode=ParseMode.HTML,
    )


async def send_window_start(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data
    end_at = payload["end_at"]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            f"🔔 <b>{payload['name']}</b> — окно респавна открыто! ({payload['location']})\n"
            f"Может появиться в любой момент до {end_at.strftime('%H:%M')}."
        ),
        parse_mode=ParseMode.HTML,
    )


def record_kill(app: Application, chat_id: int, code: str, boss: dict, killed_at: datetime):
    warn_at, start_at, end_at = schedule_for_boss(app, chat_id, code, boss, killed_at)
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


def upsert_custom_boss(chat_id: int, code: str, name: str, map_: str, location: str, min_m: int, max_m: int):
    custom = load_custom_bosses()
    custom.setdefault(str(chat_id), {})[code] = {
        "name": name,
        "map": map_,
        "location": location,
        "min_minutes": min_m,
        "max_minutes": max_m,
    }
    save_custom_bosses(custom)


def remove_custom_boss(chat_id: int, code: str) -> bool:
    custom = load_custom_bosses()
    chat_key = str(chat_id)
    if chat_key in custom and code in custom[chat_key]:
        del custom[chat_key][code]
        save_custom_bosses(custom)
        return True
    return False


# ---------- commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def list_bosses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        render_boss_list(update.effective_chat.id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        render_status(update.effective_chat.id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
    )


async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /kill <код> [ЧЧ:ММ]. Смотри /list")
        return

    chat_id = update.effective_chat.id
    code = resolve_boss(chat_id, context.args[0])
    if code is None:
        await update.message.reply_text(f"Неизвестный код босса: {context.args[0]}. Смотри /list")
        return

    if len(context.args) > 1:
        try:
            hh, mm = map(int, context.args[1].split(":"))
            current = now()
            killed_at = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if killed_at > current:
                killed_at -= timedelta(days=1)
        except ValueError:
            await update.message.reply_text("Неверный формат времени. Используй ЧЧ:ММ, например 14:35")
            return
    else:
        killed_at = now()

    boss = effective_bosses(chat_id)[code]
    start_at, end_at = record_kill(context.application, chat_id, code, boss, killed_at)
    await update.message.reply_text(
        f"✅ <b>{boss['name']}</b> отмечен убитым в {killed_at.strftime('%H:%M')}.\n"
        f"Напомню за 10 мин до окна и в момент открытия ({start_at.strftime('%H:%M')}–{end_at.strftime('%H:%M')}).",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /cancel <код>")
        return
    chat_id = update.effective_chat.id
    code = resolve_boss(chat_id, context.args[0])
    if code is None:
        await update.message.reply_text(f"Неизвестный код босса: {context.args[0]}. Смотри /list")
        return

    clear_kill(context.application, chat_id, code)
    name = effective_bosses(chat_id)[code]["name"]
    await update.message.reply_text(
        f"🗑 Отметка и напоминания для <b>{name}</b> отменены.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def addboss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await update.message.reply_text(
            "Использование:\n<code>/addboss " + ADDBOSS_FORMAT + "</code>\n\n"
            "Пример:\n<code>/addboss " + ADDBOSS_EXAMPLE + "</code>\n\n"
            "Можно так же переопределить встроенного босса — просто укажи его код (eddga, atroce, rsx0806).",
            parse_mode=ParseMode.HTML,
        )
        return

    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) != 6:
        await update.message.reply_text(
            "Нужно ровно 6 полей через '|': " + ADDBOSS_FORMAT
        )
        return

    code, name, map_, location, min_str, max_str = parts
    code = code.lower().replace(" ", "_")
    if not code or not name:
        await update.message.reply_text("Код и имя не могут быть пустыми.")
        return
    try:
        min_m = int(min_str)
        max_m = int(max_str)
    except ValueError:
        await update.message.reply_text("Минуты должны быть целыми числами.")
        return
    if min_m <= 0 or max_m < min_m:
        await update.message.reply_text("Проверь минуты: минимум > 0 и максимум >= минимума.")
        return
    if min_m <= 10:
        await update.message.reply_text("Минимальный респавн должен быть больше 10 минут (нужно место для предупреждения за 10 мин).")
        return

    chat_id = update.effective_chat.id
    upsert_custom_boss(chat_id, code, name, map_, location, min_m, max_m)
    await update.message.reply_text(
        f"✅ Босс <b>{name}</b> (<code>{code}</code>) сохранён: респавн {min_m}–{max_m} мин.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def delboss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delboss <код>")
        return
    chat_id = update.effective_chat.id
    code = context.args[0].lower().strip()
    if remove_custom_boss(chat_id, code):
        await update.message.reply_text(f"🗑 Босс <code>{code}</code> удалён из твоего списка.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("У тебя нет такого добавленного/изменённого босса.")


async def myboss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    custom = load_custom_bosses().get(str(chat_id), {})
    if not custom:
        await update.message.reply_text(
            "У тебя пока нет своих боссов.\n\n"
            "Добавь командой:\n<code>/addboss " + ADDBOSS_FORMAT + "</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )
        return
    lines = ["⚙️ <b>Твои боссы</b>\n"]
    for code, b in custom.items():
        lines.append(
            f"✏️ <b>{b['name']}</b>  <code>{code}</code> — {b['location']}, "
            f"респавн {b['min_minutes']}–{b['max_minutes']} мин"
        )
    lines.append("\nУдалить: <code>/delboss код</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())


# ---------- callback (inline menu) ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "main":
        await query.edit_message_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

    elif data == "list":
        await query.edit_message_text(
            render_boss_list(chat_id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
        )

    elif data == "status":
        await query.edit_message_text(
            render_status(chat_id), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
        )

    elif data == "kill_menu":
        await query.edit_message_text(
            "⚔️ Кого убили только что?", reply_markup=boss_pick_keyboard(chat_id, "kill")
        )

    elif data.startswith("kill:"):
        code = data.split(":", 1)[1]
        bosses = effective_bosses(chat_id)
        if code not in bosses:
            await query.edit_message_text("Неизвестный босс.", reply_markup=main_menu_keyboard())
            return
        killed_at = now()
        boss = bosses[code]
        start_at, end_at = record_kill(context.application, chat_id, code, boss, killed_at)
        await query.edit_message_text(
            f"✅ <b>{boss['name']}</b> отмечен убитым в {killed_at.strftime('%H:%M')}.\n"
            f"Напомню за 10 мин до окна и в момент открытия ({start_at.strftime('%H:%M')}–{end_at.strftime('%H:%M')}).\n\n"
            f"Если время не совпадает с реальным — уточни через <code>/kill {code} ЧЧ:ММ</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    elif data == "cancel_menu":
        active = [c for c in effective_bosses(chat_id) if c in load_data().get(str(chat_id), {})]
        if not active:
            await query.edit_message_text("Нет активных отметок для отмены.", reply_markup=back_keyboard())
            return
        await query.edit_message_text("🗑 Что отменить?", reply_markup=boss_pick_keyboard(chat_id, "cancel", active))

    elif data.startswith("cancel:"):
        code = data.split(":", 1)[1]
        clear_kill(context.application, chat_id, code)
        name = effective_bosses(chat_id)[code]["name"]
        await query.edit_message_text(
            f"🗑 Отметка для <b>{name}</b> отменена.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    elif data == "myboss_menu":
        custom = load_custom_bosses().get(str(chat_id), {})
        text = (
            "⚙️ <b>Мои боссы</b>\n\n"
            "Добавить или изменить таймер (в том числе встроенного босса):\n"
            f"<code>/addboss {ADDBOSS_FORMAT}</code>\n"
            f"Пример: <code>/addboss {ADDBOSS_EXAMPLE}</code>\n\n"
        )
        if custom:
            text += "Нажми, чтобы удалить свой боссовский таймер:"
        else:
            text += "Своих боссов пока нет."
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=myboss_keyboard(chat_id))

    elif data.startswith("delboss:"):
        code = data.split(":", 1)[1]
        remove_custom_boss(chat_id, code)
        await query.edit_message_text("🗑 Удалено.", reply_markup=myboss_keyboard(chat_id))


def reschedule_pending(app: Application):
    data = load_data()
    current = now()
    restored = 0
    for chat_key, entries in data.items():
        chat_id = int(chat_key)
        bosses = effective_bosses(chat_id)
        for code, entry in entries.items():
            end_at = datetime.fromisoformat(entry["end_at"])
            if end_at < current:
                continue
            boss = bosses.get(code)
            if boss is None:
                continue
            killed_at = datetime.fromisoformat(entry["killed_at"])
            schedule_for_boss(app, chat_id, code, boss, killed_at)
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
            BotCommand("addboss", "Добавить/изменить своего босса"),
            BotCommand("delboss", "Удалить своего босса: /delboss код"),
            BotCommand("myboss", "Список своих добавленных боссов"),
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
    app.add_handler(CommandHandler("addboss", addboss))
    app.add_handler(CommandHandler("delboss", delboss))
    app.add_handler(CommandHandler("myboss", myboss))
    app.add_handler(CallbackQueryHandler(on_callback))

    reschedule_pending(app)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
