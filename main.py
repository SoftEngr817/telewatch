import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from pydantic import BaseModel, AnyUrl, Field, ValidationError
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, create_engine, select, Text, Boolean
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

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)  # internal numeric ID
    telegram_user_id = Column(Integer, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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
# Schedulers
# -------------------------

scheduler = AsyncIOScheduler(timezone=timezone.utc)


# -------------------------
# Models for validating command parameters
# -------------------------

class AddAlarmArgs(BaseModel):
    message: str = Field(min_length=1)
    last_trigger_utc: datetime  # must be UTC ISO8601 or y-m-d h:m:s with Z
    interval_minutes: int = Field(ge=1)

    @classmethod
    def parse_from_cli(cls, parts):
        """
        Expected format:
        /add_alarm "Please check 55's network" 2025-09-23T16:10:00Z 72
        or
        /add_alarm Please_check_55s_network 2025-09-23 16:10:00Z 72
        Tip: Best to quote the message.
        """
        if len(parts) < 4:
            raise ValueError("Usage: /add_alarm \"<message>\" <ISO8601_UTC> <interval_minutes>")
        # join everything between the first and last numeric fields as message if quoted
        # Simpler approach: parts[1] is message (possibly quoted by Telegram), so instruct users to quote it.
        msg = parts[1]
        ts = parts[2]
        interval = parts[3]
        # normalize Z
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            raise ValueError("Timestamp must include 'Z' or offset, e.g. 2025-09-23T16:10:00Z")
        dt = dt.astimezone(timezone.utc)
        return cls(message=msg, last_trigger_utc=dt, interval_minutes=int(interval))


class AddEndpointArgs(BaseModel):
    url: AnyUrl
    interval_minutes: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    latency_threshold_seconds: float = Field(gt=0)

    @classmethod
    def parse_from_cli(cls, parts):
        """
        Expected format:
        /add_endpoint https://example.com/health 20 3.0 2.0
        /add_endpoint <url> <interval_mins> [timeout=3.0] [latency_threshold=2.0]
        """
        if len(parts) < 3:
            raise ValueError("Usage: /add_endpoint <url> <interval_mins> [timeout=3.0] [latency_threshold=2.0]")
        url = parts[1]
        interval = int(parts[2])
        timeout = float(parts[3]) if len(parts) >= 4 else DEFAULT_TIMEOUT
        lat_thr = float(parts[4]) if len(parts) >= 5 else DEFAULT_LAT_THR
        return cls(url=url, interval_minutes=interval, timeout_seconds=timeout, latency_threshold_seconds=lat_thr)


# -------------------------
# Helpers
# -------------------------

def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

async def send_dm(application: Application, user_id: int, text: str):
    try:
        await application.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.exception("Failed to send Telegram message: %s", e)

def next_fire_from(last_trigger_utc: datetime, interval_minutes: int, now: Optional[datetime] = None) -> datetime:
    """
    Given the last_trigger_utc and the fixed interval, compute the next run >= now.
    """
    now = now or datetime.now(timezone.utc)
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
        alarm = (await session.execute(select(Alarm).where(Alarm.id == alarm_id))).scalar_one_or_none()
        if not alarm or not alarm.enabled:
            return
        await send_dm(application, alarm.owner_telegram_id, f"🔔 *Alarm*: {alarm.message}")
        # Update last_trigger_utc to the time we just fired
        alarm.last_trigger_utc = datetime.now(timezone.utc)
        await session.commit()

async def run_endpoint_check(application: Application, endpoint_id: int):
    async with AsyncSessionLocal() as session:
        ep = (await session.execute(select(Endpoint).where(Endpoint.id == endpoint_id))).scalar_one_or_none()
        if not ep or not ep.enabled:
            return

        try:
            async with httpx.AsyncClient(timeout=ep.timeout_seconds, follow_redirects=True) as client:
                start = datetime.now(timezone.utc)
                resp = await client.get(ep.url)
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()

            if resp.status_code >= 400:
                await send_dm(application, ep.owner_telegram_id, f"🚨 *The API is down*: `{ep.url}` returned HTTP {resp.status_code}")
                return

            if elapsed > ep.latency_threshold_seconds:
                await send_dm(application, ep.owner_telegram_id, f"⚠️ *Response time too long*: `{elapsed:.2f}s` (threshold {ep.latency_threshold_seconds:.2f}s) for `{ep.url}`")
        except httpx.RequestError as e:
            await send_dm(application, ep.owner_telegram_id, f"🚨 *The API appears down*: `{ep.url}`\n`{type(e).__name__}: {e}`")


# -------------------------
# Scheduling on startup
# -------------------------

def schedule_alarm_job(alarm: Alarm, app: Application):
    # First run: compute next fire from last_trigger + interval
    first_run = next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes)
    # Run once at computed date, then re-schedule after each run by adding an interval job
    # Simpler: use IntervalTrigger with start_date aligned to first_run
    trig = IntervalTrigger(minutes=alarm.interval_minutes, start_date=first_run, timezone=timezone.utc)
    job_id = f"alarm:{alarm.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_alarm, trig, args=[app, alarm.id], id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)

def schedule_endpoint_job(ep: Endpoint, app: Application):
    trig = IntervalTrigger(minutes=ep.interval_minutes, timezone=timezone.utc)
    job_id = f"endpoint:{ep.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(run_endpoint_check, trig, args=[app, ep.id], id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)


async def load_and_schedule_all(app: Application):
    async with AsyncSessionLocal() as session:
        alarms = (await session.execute(select(Alarm).where(Alarm.enabled == True))).scalars().all()
        endpoints = (await session.execute(select(Endpoint).where(Endpoint.enabled == True))).scalars().all()

    for a in alarms:
        schedule_alarm_job(a, app)
    for e in endpoints:
        schedule_endpoint_job(e, app)

    log.info("Scheduled %d alarms and %d endpoints", len(alarms), len(endpoints))


# -------------------------
# Bot command handlers
# -------------------------

def guard(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_allowed(user.id):
            if user:
                log.warning("Blocked unauthorized user_id=%s", user.id)
            return
        return await func(update, context)
    return wrapper


@guard
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *TeleWatch* is ready.\n\n"
        "*Commands*\n"
        "/add_alarm \"<message>\" <ISO8601_UTC> <interval_mins>\n"
        "  e.g. `/add_alarm \"Please check 55's network\" 2025-09-23T16:10:00Z 72`\n"
        "/list_alarms\n"
        "/delete_alarm <id>\n"
        "/enable_alarm <id> | /disable_alarm <id>\n\n"
        "/add_endpoint <url> <interval_mins> [timeout=3.0] [latency_threshold=2.0]\n"
        "  e.g. `/add_endpoint https://example.com/health 20 3.0 2.0`\n"
        "/list_endpoints\n"
        "/delete_endpoint <id>\n"
        "/enable_endpoint <id> | /disable_endpoint <id>\n"
        "/ping  (quick connectivity test)\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@guard
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")


@guard
async def add_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    # Try to preserve quoted message: Telegram sends command + the rest; split() breaks quotes,
    # but typically the message (second token) will include spaces if user typed via clients that preserve quotes.
    # For reliability, advise quoted messages with no inner quotes.
    try:
        # If message contains spaces, suggest they paste exactly as shown in /start help.
        # Here we accept simple case: /add_alarm "message with spaces" 2025-... 72
        # Extract quoted message if present:
        text = update.message.text
        # Find first quoted segment:
        msg = None
        if '"' in text:
            first = text.find('"')
            second = text.find('"', first + 1)
            if second > first:
                msg = text[first + 1:second]
                rest = (text[:first] + text[second + 1:]).split()
                # rest: [/add_alarm, 2025-..., 72]
                if len(rest) < 3:
                    raise ValueError
                ts = rest[1] if rest[0].endswith("/add_alarm") else rest[2]
                interval = rest[-1]
                if ts.endswith("Z"):
                    ts = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
                args = AddAlarmArgs(message=msg, last_trigger_utc=dt, interval_minutes=int(interval))
            else:
                raise ValueError
        else:
            # Fallback simple parser
            args = AddAlarmArgs.parse_from_cli(parts)
    except Exception:
        await update.message.reply_text(
            "❌ Invalid format.\nUsage:\n`/add_alarm \"Please check 55's network\" 2025-09-23T16:10:00Z 72`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    async with AsyncSessionLocal() as session:
        alarm = Alarm(
            owner_telegram_id=update.effective_user.id,
            message=args.message,
            last_trigger_utc=args.last_trigger_utc,
            interval_minutes=args.interval_minutes,
            enabled=True,
        )
        session.add(alarm)
        await session.commit()
        await session.refresh(alarm)

    schedule_alarm_job(alarm, context.application)
    await update.message.reply_text(
        f"✅ Alarm *#{alarm.id}* added.\nNext run: `{next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes).isoformat()}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@guard
async def list_alarms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Alarm).where(Alarm.owner_telegram_id == update.effective_user.id).order_by(Alarm.id.asc())
        )).scalars().all()

    if not rows:
        await update.message.reply_text("No alarms.")
        return

    lines = []
    for a in rows:
        next_run = scheduler.get_job(f"alarm:{a.id}")
        next_time = next_run.next_run_time.isoformat() if next_run else "—"
        lines.append(
            f"*#{a.id}* {'✅' if a.enabled else '⏸️'} | every {a.interval_minutes} min | next: `{next_time}`\n"
            f"• last_trigger_utc: `{a.last_trigger_utc.isoformat()}`\n"
            f"• msg: {a.message}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@guard
async def delete_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /delete_alarm <id>")
        return
    alarm_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(select(Alarm).where(
            Alarm.id == alarm_id, Alarm.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not alarm:
            await update.message.reply_text("Not found.")
            return
        await session.delete(alarm)
        await session.commit()

    job_id = f"alarm:{alarm_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await update.message.reply_text(f"🗑️ Deleted alarm #{alarm_id}.")


@guard
async def enable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_alarm(update, True)

@guard
async def disable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_alarm(update, False)

async def _toggle_alarm(update: Update, enable: bool):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text(f"Usage: /{'enable' if enable else 'disable'}_alarm <id>")
        return
    alarm_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(select(Alarm).where(
            Alarm.id == alarm_id, Alarm.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not alarm:
            await update.message.reply_text("Not found.")
            return
        alarm.enabled = enable
        await session.commit()

    if enable:
        schedule_alarm_job(alarm, update.get_application())
    else:
        job_id = f"alarm:{alarm_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await update.message.reply_text(f"{'✅ Enabled' if enable else '⏸️ Disabled'} alarm #{alarm_id}.")


@guard
async def add_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    try:
        args = AddEndpointArgs.parse_from_cli(parts)
    except (ValueError, ValidationError):
        await update.message.reply_text(
            "❌ Invalid format.\nUsage:\n`/add_endpoint <url> <interval_mins> [timeout=3.0] [latency_threshold=2.0]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    async with AsyncSessionLocal() as session:
        ep = Endpoint(
            owner_telegram_id=update.effective_user.id,
            url=str(args.url),
            interval_minutes=args.interval_minutes,
            timeout_seconds=args.timeout_seconds,
            latency_threshold_seconds=args.latency_threshold_seconds,
            enabled=True,
        )
        session.add(ep)
        await session.commit()
        await session.refresh(ep)

    schedule_endpoint_job(ep, context.application)
    await update.message.reply_text(
        f"✅ Endpoint *#{ep.id}* added: `{ep.url}` every {ep.interval_minutes} min",
        parse_mode=ParseMode.MARKDOWN,
    )


@guard
async def list_endpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Endpoint).where(Endpoint.owner_telegram_id == update.effective_user.id).order_by(Endpoint.id.asc())
        )).scalars().all()

    if not rows:
        await update.message.reply_text("No endpoints.")
        return

    lines = []
    for e in rows:
        job = scheduler.get_job(f"endpoint:{e.id}")
        next_time = job.next_run_time.isoformat() if job else "—"
        lines.append(
            f"*#{e.id}* {'✅' if e.enabled else '⏸️'} | every {e.interval_minutes} min | next: `{next_time}`\n"
            f"• timeout={e.timeout_seconds}s | latency_thr={e.latency_threshold_seconds}s\n"
            f"• url: `{e.url}`"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@guard
async def delete_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /delete_endpoint <id>")
        return
    endpoint_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        ep = (await session.execute(select(Endpoint).where(
            Endpoint.id == endpoint_id, Endpoint.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not ep:
            await update.message.reply_text("Not found.")
            return
        await session.delete(ep)
        await session.commit()

    job_id = f"endpoint:{endpoint_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await update.message.reply_text(f"🗑️ Deleted endpoint #{endpoint_id}.")


@guard
async def enable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_endpoint(update, True)

@guard
async def disable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_endpoint(update, False)

async def _toggle_endpoint(update: Update, enable: bool):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text(f"Usage: /{'enable' if enable else 'disable'}_endpoint <id>")
        return
    endpoint_id = int(parts[1])

    async with AsyncSessionLocal() as session:
        ep = (await session.execute(select(Endpoint).where(
            Endpoint.id == endpoint_id, Endpoint.owner_telegram_id == update.effective_user.id
        ))).scalar_one_or_none()
        if not ep:
            await update.message.reply_text("Not found.")
            return
        ep.enabled = enable
        await session.commit()

    if enable:
        schedule_endpoint_job(ep, update.get_application())
    else:
        job_id = f"endpoint:{endpoint_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await update.message.reply_text(f"{'✅ Enabled' if enable else '⏸️ Disabled'} endpoint #{endpoint_id}.")


# -------------------------
# App bootstrap
# -------------------------

async def on_start(app: Application):
    # DB init
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scheduler.start()
    await load_and_schedule_all(app)
    log.info("TeleWatch started.")


def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_start).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("ping", ping))

    application.add_handler(CommandHandler("add_alarm", add_alarm))
    application.add_handler(CommandHandler("list_alarms", list_alarms))
    application.add_handler(CommandHandler("delete_alarm", delete_alarm))
    application.add_handler(CommandHandler("enable_alarm", enable_alarm))
    application.add_handler(CommandHandler("disable_alarm", disable_alarm))

    application.add_handler(CommandHandler("add_endpoint", add_endpoint))
    application.add_handler(CommandHandler("list_endpoints", list_endpoints))
    application.add_handler(CommandHandler("delete_endpoint", delete_endpoint))
    application.add_handler(CommandHandler("enable_endpoint", enable_endpoint))
    application.add_handler(CommandHandler("disable_endpoint", disable_endpoint))

    # Ignore non-command messages (optional)
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), start))
    return application


if __name__ == "__main__":
    app = build_app()
    app.run_polling(close_loop=False)
