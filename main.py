import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, Boolean, select
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

async def send_dm(application: Application, user_id: int, text: str):
    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML
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

# -------------------------
# Job functions
# -------------------------

async def run_alarm(application: Application, alarm_id: int):
    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(
            select(Alarm).where(Alarm.id == alarm_id)
        )).scalar_one_or_none()
        if not alarm or not alarm.enabled:
            return
        await send_dm(application, alarm.owner_telegram_id, f"🔔 <b>Alarm</b>: {alarm.message}")
        alarm.last_trigger_utc = datetime.now(timezone.utc)
        await session.commit()

async def run_endpoint_check(application: Application, endpoint_id: int):
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
                    f"🚨 <b>API down</b>: <code>{ep.url}</code> returned HTTP {resp.status_code}")
                return

            if elapsed > ep.latency_threshold_seconds:
                await send_dm(application, ep.owner_telegram_id,
                    f"⚠️ <b>Slow response</b>: {elapsed:.2f}s (threshold {ep.latency_threshold_seconds:.2f}s) for <code>{ep.url}</code>")
        except httpx.RequestError as e:
            await send_dm(application, ep.owner_telegram_id,
                f"🚨 <b>API unreachable</b>: <code>{ep.url}</code>\n<code>{type(e).__name__}: {e}</code>")

# -------------------------
# Scheduling
# -------------------------

def schedule_alarm_job(alarm: Alarm, app: Application):
    first_run = next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes)
    trig = IntervalTrigger(minutes=alarm.interval_minutes, start_date=first_run, timezone=timezone.utc)
    job_id = f"alarm:{alarm.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_alarm, trig, args=[app, alarm.id], id=job_id)

def schedule_endpoint_job(ep: Endpoint, app: Application):
    trig = IntervalTrigger(minutes=ep.interval_minutes, timezone=timezone.utc)
    job_id = f"endpoint:{ep.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_endpoint_check, trig, args=[app, ep.id], id=job_id)

async def load_and_schedule_all(app: Application):
    async with AsyncSessionLocal() as session:
        alarms = (await session.execute(select(Alarm).where(Alarm.enabled == True))).scalars().all()
        endpoints = (await session.execute(select(Endpoint).where(Endpoint.enabled == True))).scalars().all()

    for a in alarms:
        schedule_alarm_job(a, app)
    for e in endpoints:
        schedule_endpoint_job(e, app)

# -------------------------
# Bot command handlers
# -------------------------

def guard(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_allowed(user.id):
            return
        return await func(update, context)
    return wrapper

@guard
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>TeleWatch is ready.</b>\n\n"
        "<b>Commands</b>\n"
        "/add_alarm \"<message>\" <ISO8601_UTC> <interval_mins>\n"
        "  e.g. <code>/add_alarm \"Please check 55's network\" 2025-09-23T16:10:00Z 72</code>\n"
        "/list_alarms\n"
        "/delete_alarm <id>\n"
        "/enable_alarm <id> | /disable_alarm <id>\n\n"
        "/add_endpoint <url> <interval_mins> [timeout=3.0] [latency=2.0]\n"
        "  e.g. <code>/add_endpoint https://example.com/health 20 3.0 2.0</code>\n"
        "/list_endpoints\n"
        "/delete_endpoint <id>\n"
        "/enable_endpoint <id> | /disable_endpoint <id>\n\n"
        "/ping  (quick test)\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@guard
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅", parse_mode=ParseMode.HTML)

# (for brevity, you can reuse your previous add/list/delete/enable/disable alarm & endpoint
# handlers — just make sure all reply_text/send_dm calls use parse_mode=ParseMode.HTML)

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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("ping", ping))
    # TODO: add your other handlers here (add_alarm, list_alarms, etc.)
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), start))
    return application

if __name__ == "__main__":
    app = build_app()
    app.run_polling(close_loop=False)
