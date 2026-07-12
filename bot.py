import json
import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOSSES_FILE = "bosses.json"
DATA_FILE = "data.json"
CHAT_ID_FILE = "chat_id.txt"

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


def load_chat_id():
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return int(content) if content else None
    return None


def save_chat_id(chat_id: int):
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(chat_id))


BOSSES = load_bosses()


def resolve_boss(code: str):
    code = code.lower().strip()
    return code if code in BOSSES else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "Бот запущен и запомнил этот чат для напоминаний.\n\n"
        "Команды:\n"
        "/kill <код> [ЧЧ:ММ] — отметить убийство босса (без времени — берётся текущее)\n"
        "/list — список боссов и их коды\n"
        "/status — текущее состояние всех боссов\n"
        "/cancel <код> — отменить отметку и запланированные напоминания"
    )


async def list_bosses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Доступные боссы:"]
    for code, b in BOSSES.items():
        lines.append(
            f"• {code} — {b['name']} ({b['map']}, {b['location']}), "
            f"респавн {b['min_minutes']}-{b['max_minutes']} мин"
        )
    await update.message.reply_text("\n".join(lines))


def schedule_for_boss(app: Application, chat_id: int, code: str, killed_at: datetime):
    b = BOSSES[code]
    warn_at = killed_at + timedelta(minutes=b["min_minutes"] - 10)
    start_at = killed_at + timedelta(minutes=b["min_minutes"])
    end_at = killed_at + timedelta(minutes=b["max_minutes"])
    now = datetime.now()

    for job in app.job_queue.get_jobs_by_name(f"{code}_warn") + app.job_queue.get_jobs_by_name(f"{code}_start"):
        job.schedule_removal()

    if warn_at > now:
        app.job_queue.run_once(
            send_warning, when=warn_at, chat_id=chat_id, name=f"{code}_warn", data=code
        )
    if start_at > now:
        app.job_queue.run_once(
            send_window_start,
            when=start_at,
            chat_id=chat_id,
            name=f"{code}_start",
            data={"code": code, "end_at": end_at},
        )
    return warn_at, start_at, end_at


async def send_warning(context: ContextTypes.DEFAULT_TYPE):
    code = context.job.data
    b = BOSSES[code]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"⏳ {b['name']} — окно респавна откроется через 10 минут ({b['location']}).",
    )


async def send_window_start(context: ContextTypes.DEFAULT_TYPE):
    payload = context.job.data
    code = payload["code"]
    end_at = payload["end_at"]
    b = BOSSES[code]
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=(
            f"🔔 {b['name']} — окно респавна открыто! ({b['location']})\n"
            f"Может появиться в любой момент до {end_at.strftime('%H:%M')}."
        ),
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

    chat_id = update.effective_chat.id
    save_chat_id(chat_id)

    warn_at, start_at, end_at = schedule_for_boss(context.application, chat_id, code, killed_at)

    data = load_data()
    data[code] = {
        "killed_at": killed_at.isoformat(),
        "warn_at": warn_at.isoformat(),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
    }
    save_data(data)

    b = BOSSES[code]
    await update.message.reply_text(
        f"✅ {b['name']} отмечен убитым в {killed_at.strftime('%H:%M')}.\n"
        f"Напомню за 10 мин до окна и в момент открытия окна ({start_at.strftime('%H:%M')} - {end_at.strftime('%H:%M')})."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /cancel <код>")
        return
    code = resolve_boss(context.args[0])
    if code is None:
        await update.message.reply_text(f"Неизвестный код босса: {context.args[0]}. Смотри /list")
        return

    for job in (
        context.application.job_queue.get_jobs_by_name(f"{code}_warn")
        + context.application.job_queue.get_jobs_by_name(f"{code}_start")
    ):
        job.schedule_removal()

    data = load_data()
    if code in data:
        del data[code]
        save_data(data)

    await update.message.reply_text(f"Отметка и напоминания для {BOSSES[code]['name']} отменены.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    now = datetime.now()
    lines = ["Статус боссов:"]
    for code, b in BOSSES.items():
        entry = data.get(code)
        if not entry:
            lines.append(f"• {b['name']}: нет отметки об убийстве")
            continue
        start_at = datetime.fromisoformat(entry["start_at"])
        end_at = datetime.fromisoformat(entry["end_at"])
        if now < start_at:
            remaining = start_at - now
            mins = int(remaining.total_seconds() // 60)
            lines.append(f"• {b['name']}: окно откроется через {mins} мин ({start_at.strftime('%H:%M')})")
        elif now <= end_at:
            lines.append(f"• {b['name']}: окно ОТКРЫТО, до {end_at.strftime('%H:%M')}")
        else:
            lines.append(f"• {b['name']}: окно уже прошло ({end_at.strftime('%H:%M')}), отметь заново")
    await update.message.reply_text("\n".join(lines))


def reschedule_pending(app: Application):
    chat_id = load_chat_id()
    if chat_id is None:
        return
    data = load_data()
    now = datetime.now()
    for code, entry in data.items():
        end_at = datetime.fromisoformat(entry["end_at"])
        if end_at < now:
            continue
        killed_at = datetime.fromisoformat(entry["killed_at"])
        schedule_for_boss(app, chat_id, code, killed_at)
    log.info("Restored %d pending boss timers", len(data))


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN в .env или переменных окружения")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_bosses))
    app.add_handler(CommandHandler("kill", kill))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status))

    reschedule_pending(app)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
