import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from sqlalchemy import Column, Integer, Float, DateTime, Text, Boolean, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TimedOut, NetworkError
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
TELEGRAM_MSG_LIMIT = 4096
CHUNK_SIZE = 3800

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

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_dm(application, user_id: int, text: str, use_html: bool = True):
    """Safe send with HTML + fallback to plain text."""
    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML if use_html else None
        )
    except BadRequest as e:
        log.warning("BadRequest sending message, retrying without HTML: %s", e)
        try:
            stripped = re.sub(r"</?[^>]+>", "", text)
            await application.bot.send_message(chat_id=user_id, text=stripped)
        except Exception:
            log.exception("Fallback send also failed")
    except (Forbidden, TimedOut, NetworkError):
        log.exception("Telegram send failed")
    except Exception:
        log.exception("Unexpected error during send")

async def send_long_message(application, user_id: int, text: str):
    """Chunk long messages to stay under Telegram limits."""
    if len(text) <= TELEGRAM_MSG_LIMIT:
        await send_dm(application, user_id, text)
        return
    parts = []
    remaining = text
    while len(remaining) > CHUNK_SIZE:
        split_at = remaining.rfind("\n\n", 0, CHUNK_SIZE)
        if split_at == -1:
            split_at = CHUNK_SIZE
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
    parts.append(remaining)
    for p in parts:
        await send_dm(application, user_id, p)

def ensure_aware_utc(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def next_fire_from(last_trigger_utc: datetime, interval_minutes: int) -> datetime:
    now = datetime.now(timezone.utc)
    last_trigger_utc = ensure_aware_utc(last_trigger_utc)
    if interval_minutes <= 0:
        return now + timedelta(days=365*100)
    if now <= last_trigger_utc:
        return last_trigger_utc
    delta = now - last_trigger_utc
    k = int(delta.total_seconds() // (interval_minutes * 60)) + 1
    return last_trigger_utc + timedelta(minutes=interval_minutes * k)

def parse_utc_timestamp(ts: str) -> datetime:
    """Accepts 2025-09-23T16:10:00Z, 2025-09-23 16:10:00Z, or with +00:00 offset."""
    ts = ts.strip().replace(" ", "T")
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include timezone (Z or +00:00)")
    return dt.astimezone(timezone.utc)

def valid_url(u: str) -> bool:
    return u.startswith("http://") or u.startswith("https://")

# -------------------------
# Job functions
# -------------------------

async def run_alarm(application, alarm_id: int):
    try:
        async with AsyncSessionLocal() as session:
            alarm = (await session.execute(
                select(Alarm).where(Alarm.id == alarm_id)
            )).scalar_one_or_none()
            if not alarm or not alarm.enabled:
                return
            await send_dm(application, alarm.owner_telegram_id, f"🔔 <b>Alarm</b>: {html_escape(alarm.message)}")
            alarm.last_trigger_utc = datetime.now(timezone.utc)
            await session.commit()
    except Exception:
        log.exception("run_alarm failed (alarm_id=%s)", alarm_id)

async def run_endpoint_check(application, endpoint_id: int):
    try:
        async with AsyncSessionLocal() as session:
            ep = (await session.execute(
                select(Endpoint).where(Endpoint.id == endpoint_id)
            )).scalar_one_or_none()
            if not ep or not ep.enabled:
                return
            try:
                async with httpx.AsyncClient(timeout=ep.timeout_seconds, follow_redirects=True) as client:
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
                    f"🚨 <b>Unreachable</b>: <code>{html_escape(ep.url)}</code>\n<code>{html_escape(type(e).__name__ + ': ' + str(e))}</code>")
    except Exception:
        log.exception("run_endpoint_check failed (endpoint_id=%s)", endpoint_id)

# -------------------------
# Scheduling
# -------------------------

def schedule_alarm_job(alarm: Alarm, app):
    try:
        first_run = next_fire_from(alarm.last_trigger_utc, alarm.interval_minutes)
        trig = IntervalTrigger(minutes=max(1, alarm.interval_minutes), start_date=first_run, timezone=timezone.utc)
        job_id = f"alarm:{alarm.id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(run_alarm, trig, args=[app, alarm.id],
                          id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)
    except Exception:
        log.exception("schedule_alarm_job failed (alarm_id=%s)", alarm.id)

def schedule_endpoint_job(ep: Endpoint, app):
    try:
        trig = IntervalTrigger(minutes=max(1, ep.interval_minutes), timezone=timezone.utc)
        job_id = f"endpoint:{ep.id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(run_endpoint_check, trig, args=[app, ep.id],
                          id=job_id, misfire_grace_time=60, coalesce=True, max_instances=1)
    except Exception:
        log.exception("schedule_endpoint_job failed (endpoint_id=%s)", ep.id)

async def load_and_schedule_all(app):
    try:
        async with AsyncSessionLocal() as session:
            alarms = (await session.execute(select(Alarm).where(Alarm.enabled == True))).scalars().all()
            endpoints = (await session.execute(select(Endpoint).where(Endpoint.enabled == True))).scalars().all()
        for a in alarms:
            schedule_alarm_job(a, app)
        for e in endpoints:
            schedule_endpoint_job(e, app)
        log.info("Scheduled %d alarms and %d endpoints", len(alarms), len(endpoints))
    except Exception:
        log.exception("load_and_schedule_all failed")

# -------------------------
# Decorator: Auth guard
# -------------------------

def guard(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user = update.effective_user
            if not user or not is_allowed(user.id):
                return
            return await func(update, context)
        except Exception:
            log.exception("Handler %s crashed", getattr(func, "__name__", "unknown"))
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
        "/add_alarm \"&lt;msg&gt;\" &lt;UTC_ISO8601&gt; &lt;mins&gt;\n"
        "/list_alarms, /enable_alarm id, /disable_alarm id, /delete_alarm id\n"
        "/add_endpoint &lt;url&gt; &lt;mins&gt; [timeout] [latency]\n"
        "/list_endpoints, /enable_endpoint id, /disable_endpoint id, /delete_endpoint id\n"
        "/ping — quick reply\n/status — backend health"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@guard
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅", parse_mode=ParseMode.HTML)

@guard
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        jobs = scheduler.get_jobs()
        n_alarms = len([j for j in jobs if j.id.startswith("alarm:")])
        n_eps = len([j for j in jobs if j.id.startswith("endpoint:")])
        nexts = [j.next_run_time for j in jobs if j.next_run_time is not None]
        next_run = min(nexts).isoformat() if nexts else "—"
        text = (
            "<b>Status</b>\n"
            f"Scheduler: {'running ✅' if scheduler.running else 'stopped ❌'}\n"
            f"Jobs: {len(jobs)} (alarms: {n_alarms}, endpoints: {n_eps})\n"
            f"Next run: <code>{html_escape(next_run)}</code>\n"
            f"UTC now: <code>{datetime.now(timezone.utc).isoformat()}</code>"
        )
    except Exception:
        log.exception("/status failed")
        text = "⚠️ Failed to collect status."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ---------- Alarm commands ----------

@guard
async def add_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    m = re.match(r'^/add_alarm\s+(?:"([^"]+)"|(\S+))\s+(\S+)\s+(\d+)\s*$', raw)
    if not m:
        await update.message.reply_text("❌ Usage:\n<code>/add_alarm \"msg\" 2025-09-23T16:10:00Z 72</code>", parse_mode=ParseMode.HTML)
        return
    message = m.group(1) or m.group(2)
    ts_str = m.group(3)
    try:
        interval = int(m.group(4))
        if interval <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ interval_mins must be positive integer.", parse_mode=ParseMode.HTML)
        return
    try:
        dt = parse_utc_timestamp(ts_str)
    except Exception:
        await update.message.reply_text("❌ Invalid timestamp. Use e.g. 2025-09-23T16:10:00Z", parse_mode=ParseMode.HTML)
        return
    async with AsyncSessionLocal() as session:
        alarm = Alarm(owner_telegram_id=update.effective_user.id, message=message, last_trigger_utc=dt, interval_minutes=interval, enabled=True)
        session.add(alarm)
        await session.commit()
        await session.refresh(alarm)
    schedule_alarm_job(alarm, context.application)
    await update.message.reply_text(f"✅ Alarm <b>#{alarm.id}</b> added.", parse_mode=ParseMode.HTML)

@guard
async def list_alarms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Alarm).where(Alarm.owner_telegram_id == update.effective_user.id))).scalars().all()
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
    await send_long_message(context.application, update.effective_chat.id, "\n\n".join(lines))

@guard
async def delete_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /delete_alarm id", parse_mode=ParseMode.HTML)
        return
    alarm_id = int(parts[1])
    async with AsyncSessionLocal() as session:
        alarm = (await session.execute(select(Alarm).where(Alarm.id==alarm_id, Alarm.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not alarm:
            await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML)
            return
        await session.delete(alarm); await session.commit()
    if scheduler.get_job(f"alarm:{alarm_id}"): scheduler.remove_job(f"alarm:{alarm_id}")
    await update.message.reply_text(f"🗑️ Deleted alarm #{alarm_id}.", parse_mode=ParseMode.HTML)

@guard
async def enable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts)!=2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /enable_alarm id", parse_mode=ParseMode.HTML); return
    alarm_id=int(parts[1])
    async with AsyncSessionLocal() as session:
        alarm=(await session.execute(select(Alarm).where(Alarm.id==alarm_id, Alarm.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not alarm: await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML); return
        alarm.enabled=True; await session.commit()
    schedule_alarm_job(alarm, context.application)
    await update.message.reply_text(f"✅ Enabled alarm #{alarm_id}", parse_mode=ParseMode.HTML)

@guard
async def disable_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts)!=2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /disable_alarm id", parse_mode=ParseMode.HTML); return
    alarm_id=int(parts[1])
    async with AsyncSessionLocal() as session:
        alarm=(await session.execute(select(Alarm).where(Alarm.id==alarm_id, Alarm.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not alarm: await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML); return
        alarm.enabled=False; await session.commit()
    if scheduler.get_job(f"alarm:{alarm_id}"): scheduler.remove_job(f"alarm:{alarm_id}")
    await update.message.reply_text(f"⏸️ Disabled alarm #{alarm_id}", parse_mode=ParseMode.HTML)

# ---------- Endpoint commands ----------

@guard
async def add_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.strip().split()
    try:
        if len(parts)<3: raise ValueError
        url=parts[1]; interval=int(parts[2]); timeout=float(parts[3]) if len(parts)>=4 else DEFAULT_TIMEOUT; lat=float(parts[4]) if len(parts)>=5 else DEFAULT_LAT_THR
        if not valid_url(url) or interval<=0 or timeout<=0 or lat<=0: raise ValueError
    except Exception:
        await update.message.reply_text("❌ Usage: /add_endpoint https://example.com/health 20 [timeout] [latency]", parse_mode=ParseMode.HTML); return
    async with AsyncSessionLocal() as session:
        ep=Endpoint(owner_telegram_id=update.effective_user.id,url=url,interval_minutes=interval,timeout_seconds=timeout,latency_threshold_seconds=lat,enabled=True)
        session.add(ep); await session.commit(); await session.refresh(ep)
    schedule_endpoint_job(ep, context.application)
    await update.message.reply_text(f"✅ Endpoint <b>#{ep.id}</b> added.", parse_mode=ParseMode.HTML)

@guard
async def list_endpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        rows=(await session.execute(select(Endpoint).where(Endpoint.owner_telegram_id==update.effective_user.id))).scalars().all()
    if not rows: await update.message.reply_text("No endpoints.", parse_mode=ParseMode.HTML); return
    lines=[]
    for e in rows:
        job=scheduler.get_job(f"endpoint:{e.id}")
        next_time=job.next_run_time.isoformat() if job and job.next_run_time else "—"
        lines.append(
            f"<b>#{e.id}</b> {'✅' if e.enabled else '⏸️'} | every {e.interval_minutes} min | next: <code>{html_escape(next_time)}</code>\n"
            f"• timeout={e.timeout_seconds}s | latency={e.latency_threshold_seconds}s\n"
            f"• url: <code>{html_escape(e.url)}</code>"
        )
    await send_long_message(context.application, update.effective_chat.id, "\n\n".join(lines))

@guard
async def delete_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.strip().split()
    if len(parts)!=2 or not parts[1].isdigit(): await update.message.reply_text("Usage: /delete_endpoint id", parse_mode=ParseMode.HTML); return
    ep_id=int(parts[1])
    async with AsyncSessionLocal() as session:
        ep=(await session.execute(select(Endpoint).where(Endpoint.id==ep_id, Endpoint.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not ep: await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML); return
        await session.delete(ep); await session.commit()
    if scheduler.get_job(f"endpoint:{ep_id}"): scheduler.remove_job(f"endpoint:{ep_id}")
    await update.message.reply_text(f"🗑️ Deleted endpoint #{ep_id}.", parse_mode=ParseMode.HTML)

@guard
async def enable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.strip().split()
    if len(parts)!=2 or not parts[1].isdigit(): await update.message.reply_text("Usage: /enable_endpoint id", parse_mode=ParseMode.HTML); return
    ep_id=int(parts[1])
    async with AsyncSessionLocal() as session:
        ep=(await session.execute(select(Endpoint).where(Endpoint.id==ep_id, Endpoint.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not ep: await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML); return
        ep.enabled=True; await session.commit()
    schedule_endpoint_job(ep, context.application)
    await update.message.reply_text(f"✅ Enabled endpoint #{ep_id}", parse_mode=ParseMode.HTML)

@guard
async def disable_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts=update.message.text.strip().split()
    if len(parts)!=2 or not parts[1].isdigit(): await update.message.reply_text("Usage: /disable_endpoint id", parse_mode=ParseMode.HTML); return
    ep_id=int(parts[1])
    async with AsyncSessionLocal() as session:
        ep=(await session.execute(select(Endpoint).where(Endpoint.id==ep_id, Endpoint.owner_telegram_id==update.effective_user.id))).scalar_one_or_none()
        if not ep: await update.message.reply_text("Not found.", parse_mode=ParseMode.HTML); return
        ep.enabled=False; await session.commit()
    if scheduler.get_job(f"endpoint:{ep_id}"): scheduler.remove_job(f"endpoint:{ep_id}")
    await update.message.reply_text(f"⏸️ Disabled endpoint #{ep_id}", parse_mode=ParseMode.HTML)

# -------------------------
# Global error handler
# -------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_user and is_allowed(update.effective_user.id):
            await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ An internal error occurred. Check server logs.")
    except Exception:
        pass

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
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_start).build()

    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("status", status_cmd))

    # Alarms
    app.add_handler(CommandHandler("add_alarm", add_alarm))
    app.add_handler(CommandHandler("list_alarms", list_alarms))
    app.add_handler(CommandHandler("delete_alarm", delete_alarm))
    app.add_handler(CommandHandler("enable_alarm", enable_alarm))
    app.add_handler(CommandHandler("disable_alarm", disable_alarm))

    # Endpoints
    app.add_handler(CommandHandler("add_endpoint", add_endpoint))
    app.add_handler(CommandHandler("list_endpoints", list_endpoints))
    app.add_handler(CommandHandler("delete_endpoint", delete_endpoint))
    app.add_handler(CommandHandler("enable_endpoint", enable_endpoint))
    app.add_handler(CommandHandler("disable_endpoint", disable_endpoint))

    # Fallback: any non-command text shows help
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), start))

    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    application = build_app()
    # Important: no asyncio.run here; let PTB manage the loop
    application.run_polling(close_loop=False)
