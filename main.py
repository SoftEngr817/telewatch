import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from sqlalchemy import (
    Column, Integer, Float, DateTime, Text, Boolean, select
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler,
    filters,
)

# -------------------------
# Setup & Config
# -------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("telewatch")

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in environment")

ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
if not ALLOWED_USER_IDS:
    log.warning("ALLOWED_USER_IDS is empty. No one will be allowed to use the bot.")

DEFAULT_TIMEOUT = float(os.environ.get("DEFAULT_TIMEOUT_SECONDS", "3.0"))
DEFAULT_LAT_THR = float(os.environ.get("DEFAULT_LATENCY_THRESHOLD_SECONDS", "2.0"))

DB_URL = "sqlite+aiosqlite:///./telewatch.db"

Base = declarative_base()

# -------------------------
# DB Models
# -------------------------

class Alarm(Base):
    __tablename__ = "alarms"

    id = Column(Integer, primary_key=True)
    owner_telegram_id = Column(Integer, index=True, nullable=False)
    message = Column(Text, nullable=False)
    last_trigger_utc = Column(DateTime(timezone=True), nullable=False)
    interval_minutes = Column(Integer, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Endpoint(Base):
    __tablename__ = "endpoints"

    id = Column(Integer, primary_key=True)
    owner_telegram_id = Column(Integer, index=True, nullable=False)
    url = Column(Text, nullable=False)
    interval_minutes = Column(Integer, nullable=False)
    timeout_seconds = Column(Float, nullable=False, default=DEFAULT_TIMEOUT)
    latency_threshold_seconds = Column(Float, nullable=False, default=DEFAULT_LAT_THR)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# -------------------------
# Async DB engine/session
# -------------------------

engine = create_async_engine(DB_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# -------------------------
# Scheduler
# -------------------------

scheduler = AsyncIOScheduler(timezone=timezone.utc)

# -------------------------
# Helpers
# -------------------------

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

async def send_dm(application, user_id: int, text: str):
    try:
        await application.bot.send_message(
            chat_id=user_id, text=text, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.exception("Failed to send Telegram message: %s", e)

def next_fire_from(last_trigger_utc: datetime, interval_minutes: int) -> datetime:
    now = datetime.now(timezone.utc)
    if now <= last_trigger_utc:
        return last_trigger_utc
    delta = now - last_trigger_utc
    k = int(delta.total_seconds() // (interval_minutes * 60)) + 1
    return last_trigger_utc + timedelta(minutes=interval_minutes * k)

def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

# -------------------------
# Job functions
# -------------------------

async def run_alarm(application, alarm_id: int):
    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(
            select(Alarm).where(Alarm.id == alarm_id)
        )).scalar_one_or_none()
        if not alarm or not alarm.enabled:
            return
        await send_dm(application, alarm.owner_telegram_id, f"🔔 <b>Alarm</b>: {html_escape(alarm.message)}")
        alarm.last_trigger_utc = datetime.now(timezone.utc)
        await session.commit()

async def run_endpoint_check(application, endpoint_id: int):
    async with AsyncSessionLocal() as session:
        ep = (await session.execute(
            select(Endpoint).where(Endpoint.id == endpoint_id)
        )).scalar_one_or_none()
        if not ep or not ep.enabled:
            return

        try:
            async with httpx.AsyncClient(timeout=ep.timeout_seconds) as client:
                start = datetime.now(timezone.utc)
                resp = await client.get(ep.url)
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()

            if resp.status_code >= 400:
                await send_dm(application, ep.owner_telegram_id,
                    f"🚨 <b>API down</b>: <code>{html_escape(ep.url)}</code> returned {resp.status_code}")
                return

            if elapsed > ep.latency_threshold_seconds:
                await send_dm(application, ep.owner_telegram_id,
                    f"⚠️ <b>Slow</b>: {elapsed:.2f}s (limit {ep.latency_threshold_seconds:.2f}s) for <code>{html_escape(ep.url)}</code>")
        except httpx.RequestError as e:
            await send_dm(application, ep.owner_telegram_id,
                f"🚨 <b>Unreachable</b>: <code>{html_escape(ep.url)}</code>\n<code>{html_escape(str(type(e).__name__ + ': ' + str(e)))}</code>")

# -------------------------
# Scheduling
# -------------------------

def schedule_alarm_job(alarm: Alarm, app):
    first_run = next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes)
    trig = IntervalTrigger(minutes=alarm.interval_minutes, start_date=first_run, timezone=timezone.utc)
    job_id = f"alarm:{alarm.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_alarm, trig, args=[app, alarm.id], id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)

def schedule_endpoint_job(ep: Endpoint, app):
    trig = IntervalTrigger(minutes=ep.interval_minutes, timezone=timezone.utc)
    job_id = f"endpoint:{ep.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_endpoint_check, trig, args=[app, ep.id], id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)

async def load_and_schedule_all(app):
    async with AsyncSessionLocal() as session:
        alarms = (await session.execute(select(Alarm).where(Alarm.enabled == True))).scalars().all()
        endpoints = (await session.execute(select(Endpoint).where(Endpoint.enabled == True))).scalars().all()

    for a in alarms:
        schedule_alarm_job(a, app)
    for e in endpoints:
        schedule_endpoint_job(e, app)
    log.info("Scheduled %d alarms and %d endpoints", len(alarms), len(endpoints))

# -------------------------
# Decorator: Auth guard
# -------------------------

def guard(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_allowed(user.id):
            # Silently ignore unauthorized users (personal bot)
            return
        return await func(update, context)
    return wrapper

# -------------------------
# Command Handlers
# -------------------------

@guard
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>TeleWatch</b>\n"
        "Personal alarms & endpoint checks.\n\n"
        "<b>Commands</b>\n"
        "/add_alarm \"&lt;message&gt;\" &lt;ISO8601_UTC&gt; &lt;interval_mins&gt;\n"
        "  e.g. <code>/add_alarm \"Please check 55's network\" 2025-09-23T16:10:00Z 72</code>\n"
        "/list_alarms, /enable_alarm &lt;id&gt;, /disable_alarm &lt;id&gt;, /delete_alarm &lt;id&gt;\n\n"
        "/add_endpoint &lt;url&gt; &lt;interval_mins&gt; [timeout=3.0] [latency=2.0]\n"
        "/list_endpoints, /enable_endpoint &lt;id&gt;, /disable_endpoint &lt;id&gt;, /delete_endpoint &lt;id&gt;\n\n"
        "/ping — quick reply\n"
        "/status — backend & scheduler info\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@guard
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅", parse_mode=ParseMode.HTML)

@guard
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show scheduler health & counts
    jobs = scheduler.get_jobs()
    n_alarms = len([j for j in jobs if j.id.startswith("alarm:")])
    n_eps = len([j for j in jobs if j.id.startswith("endpoint:")])
    nexts = [j.next_run_time for j in jobs if j.next_run_time is not None]
    next_run = min(nexts).isoformat() if nexts else "—"
    text = (
        "<b>Status</b>\n"
        f"Scheduler: running ✅\n"
        f"Jobs: {len(jobs)} (alarms: {n_alarms}, endpoints: {n_eps})\n"
        f"Next run at: <code>{html_escape(next_run)}</code>\n"
        f"UTC now: <code>{datetime.now(timezone.utc).isoformat()}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ---------- Alarms ----------

@guard
async def add_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    # Expect: /add_alarm "message" 2025-09-23T16:10:00Z 72
    try:
        msg = None
        ts = None
        interval = None

        if '"' in raw:
            first = raw.find('"')
            second = raw.find('"', first + 1)
            if second > first:
                msg = raw[first + 1:second]
                parts = (raw[:first] + raw[second + 1:]).split()
                # parts example: ['/add_alarm', '2025-..', '72']
                numbers = [p for p in parts if p != "/add_alarm"]
                if len(numbers) < 2:
                    raise ValueError
                ts = numbers[0]
                interval = int(numbers[1])
        if msg is None:
            # Fallback simple parser
            parts = raw.split()
            if len(parts) < 4:
                raise ValueError
            msg = parts[1]
            ts = parts[2]
            interval = int(parts[3])

        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts).astimezone(timezone.utc)

    except Exception:
        await update.message.reply_text(
            "❌ Usage:\n<code>/add_alarm \"&lt;message&gt;\" 2025-09-23T16:10:00Z 72</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with AsyncSessionLocal() as session:
        alarm = Alarm(
            owner_telegram_id=update.effective_user.id,
            message=msg,
            last_trigger_utc=dt,
            interval_minutes=interval,
            enabled=True,
        )
        session.add(alarm)
        await session.commit()
        await session.refresh(alarm)

    schedule_alarm_job(alarm, context.application)
    await update.message.reply_text(
        f"✅ Alarm <b>#{alarm.id}</b> added.\nNext run: <code>{next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes).isoformat()}</code>",
        parse_mode=ParseMode.HTML,
    )

@guard
async def list_alarms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Alarm).where(Alarm.owner_telegram_id == update.effective_user.id).order_by(Alarm.id.asc())
        )).scalars().all()

    if not rows:
        await update.message.reply_text("No alarms.", parse_mode=ParseMode.HTML)
        return

    lines = []
    for a in rows:
        job = scheduler.get_job(f"alarm:{a.id}")
        next_time = job.next_run_time.isoformat() if job and job.next_run_time else "—"
        lines.append(
            f"<b>#{a.id}</b> {'✅' if a.enabled else '⏸️'} | every {a.interval_minutes} min | next: <code>{html_escape(next_time)}</code>\n"
            f"• last: <code>{a.last_trigger_utc.isoformat()}</code>\n"
            f"• msg: {html_escape(a.message)}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)

@guard
async def delete_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: <code>/delete_alarm &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    alarm_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(select(Alarm).where(
            Alarm.id == alarm_id, Alarm.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not alarm:
            await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML)
            return
        await session.delete(alarm)
        await session.commit()

    job_id = f"alarm:{alarm_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await update.message.reply_text(f"🗑️ Deleted alarm <b>#{alarm_id}</b>.", parse_mode=ParseMode.HTML)

async def _toggle_alarm_core(update: Update, enable: bool):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        cmd = "enable_alarm" if enable else "disable_alarm"
        await update.message.reply_text(f"Usage: <code>/{cmd} &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    alarm_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(select(Alarm).where(
            Alarm.id == alarm_id, Alarm.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not alarm:
            await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML)
            return
        alarm.enabled = enable
        await session.commit()

    if enable:
        schedule_alarm_job(alarm, update.get_application())
    else:
        job_id = f"alarm:{alarm_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await update.message.reply_text(
        f"{'✅ Enabled' if enable else '⏸️ Disabled'} alarm <b>#{alarm_id}</b>.",
        parse_mode=ParseMode.HTML,
    )

@guard
async def enable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_alarm_core(update, True)

@guard
async def disable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_alarm_core(update, False)

# ---------- Endpoints ----------

@guard
async def add_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /add_endpoint <url> <interval_mins> [timeout] [latency]
    parts = update.message.text.strip().split()
    try:
        if len(parts) < 3:
            raise ValueError
        url = parts[1]
        interval = int(parts[2])
        timeout = float(parts[3]) if len(parts) >= 4 else DEFAULT_TIMEOUT
        lat_thr = float(parts[4]) if len(parts) >= 5 else DEFAULT_LAT_THR
    except Exception:
        await update.message.reply_text(
            "❌ Usage:\n<code>/add_endpoint &lt;url&gt; &lt;interval_mins&gt; [timeout=3.0] [latency=2.0]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with AsyncSessionLocal() as session:
        ep = Endpoint(
            owner_telegram_id=update.effective_user.id,
            url=url,
            interval_minutes=interval,
            timeout_seconds=timeout,
            latency_threshold_seconds=lat_thr,
            enabled=True,
        )
        session.add(ep)
        await session.commit()
        await session.refresh(ep)

    schedule_endpoint_job(ep, context.application)
    await update.message.reply_text(
        f"✅ Endpoint <b>#{ep.id}</b> added: <code>{html_escape(url)}</code> every {interval} min",
        parse_mode=ParseMode.HTML,
    )

@guard
async def list_endpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Endpoint).where(Endpoint.owner_telegram_id == update.effective_user.id).order_by(Endpoint.id.asc())
        )).scalars().all()

    if not rows:
        await update.message.reply_text("No endpoints.", parse_mode=ParseMode.HTML)
        return

    lines = []
    for e in rows:
        job = scheduler.get_job(f"endpoint:{e.id}")
        next_time = job.next_run_time.isoformat() if job and job.next_run_time else "—"
        lines.append(
            f"<b>#{e.id}</b> {'✅' if e.enabled else '⏸️'} | every {e.interval_minutes} min | next: <code>{html_escape(next_time)}</code>\n"
            f"• timeout={e.timeout_seconds}s | latency={e.latency_threshold_seconds}s\n"
            f"• url: <code>{html_escape(e.url)}</code>"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)

@guard
async def delete_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: <code>/delete_endpoint &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    endpoint_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        ep = (await session.execute(select(Endpoint).where(
            Endpoint.id == endpoint_id, Endpoint.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not ep:
            await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML)
            return
        await session.delete(ep)
        await session.commit()

    job_id = f"endpoint:{endpoint_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await update.message.reply_text(f"🗑️ Deleted endpoint <b>#{endpoint_id}</b>.", parse_mode=ParseMode.HTML)

async def _toggle_endpoint_core(update: Update, enable: bool):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        cmd = "enable_endpoint" if enable else "disable_endpoint"
        await update.message.reply_text(f"Usage: <code>/{cmd} &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    endpoint_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        ep = (await session.execute(select(Endpoint).where(
            Endpoint.id == endpoint_id, Endpoint.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not ep:
            await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML)
            return
        ep.enabled = enable
        await session.commit()

    if enable:
        schedule_endpoint_job(ep, update.get_application())
    else:
        job_id = f"endpoint:{endpoint_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await update.message.reply_text(
        f"{'✅ Enabled' if enable else '⏸️ Disabled'} endpoint <b>#{endpoint_id}</b>.",
        parse_mode=ParseMode.HTML,
    )

@guard
async def enable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_endpoint_core(update, True)

@guard
async def disable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_endpoint_core(update, False)

# -------------------------
# App bootstrap
# -------------------------

async def on_start(app: Application):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler.start()
    await load_and_schedule_all(app)
    log.info("TeleWatch started.")

def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_start).build()

    # Core
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("status", status_cmd))

    # Alarms
    application.add_handler(CommandHandler("add_alarm", add_alarm))
    application.add_handler(CommandHandler("list_alarms", list_alarms))
    application.add_handler(CommandHandler("delete_alarm", delete_alarm))
    application.add_handler(CommandHandler("enable_alarm", enable_alarm))
    application.add_handler(CommandHandler("disable_alarm", disable_alarm))

    # Endpoints
    application.add_handler(CommandHandler("add_endpoint", add_endpoint))
    application.add_handler(CommandHandler("list_endpoints", list_endpoints))
    application.add_handler(CommandHandler("delete_endpoint", delete_endpoint))
    application.add_handler(CommandHandler("enable_endpoint", enable_endpoint))
    application.add_handler(CommandHandler("disable_endpoint", disable_endpoint))

    # Fallback: on any non-command text, show help
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), start))

    return application

if __name__ == "__main__":
    app = build_app()
    app.run_polling(close_loop=False)
