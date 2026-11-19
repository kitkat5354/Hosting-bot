import os
import re
import time
import asyncio
import html as H
import logging
import threading
from urllib.parse import quote

from flask import Flask, Response
from upstash_redis import Redis
from telegram import (
    Update, InlineKeyboardButton as _InlineBtn, InlineKeyboardMarkup as Markup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhost-bot")

# ---------------- CONFIG (sab Render ke ENV se aata hai) ----------------
# NOTE: BOT_TOKEN / ADMIN_ID / Redis ki settings pehle jaisi hi hain - code me kuch mat daalo,
# bas hosting ke ENV / settings me daalo.
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000").rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x}
DEV_NAME = os.environ.get("DEV_NAME", "Developer")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "").lstrip("@")
MAX_SITES = int(os.environ.get("MAX_SITES", "10"))          # free user ki limit
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "1")) * 1024 * 1024)
PREMIUM_INFO = os.environ.get("PREMIUM_INFO", "")            # optional: premium ka rate/details text

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")
RESERVED = {"admin", "api", "health", "static", "login", "bot", "www", "index", "favicon-ico"}

# ---------------- REDIS KEYS ----------------
# site:<name>          -> html (live)
# owner:<name>         -> user id
# user:<id>:sites      -> set of site names
# users                -> set of all user ids
# banned               -> set of banned user ids
# channels / chan:<id> -> force-join channels
# uinfo:<id>           -> "name\tusername"
# --- naye keys ---
# pend:<name>          -> html (approval ka wait)
# pendowner:<name>     -> user id jisne upload kiya
# pendtype:<name>      -> "new" ya "update"
# pending              -> set of pending site names
# user:<id>:pending    -> set of user ke pending (new) sites
# prem:<id>            -> premium expiry (unix) ya "0" = lifetime
# premium_users        -> set of premium user ids
# bonus:<id>           -> referral se mile extra site slots
# refs:<id>            -> set of invited user ids
# refby:<id>           -> kis ne invite kiya (credit baaki)
# cfg:<key>            -> admin settings


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    return bool(redis.sismember("banned", str(uid)))


def site_url(name: str) -> str:
    return f"{BASE_URL}/{name}"


# ---------------- SETTINGS (admin panel se badalti hain) ----------------
def cfg(key: str, default: str = "1") -> str:
    v = redis.get(f"cfg:{key}")
    return default if v is None else str(v)


def cfg_set(key: str, val) -> None:
    redis.set(f"cfg:{key}", str(val))


def cfg_on(key: str, default: str = "1") -> bool:
    return cfg(key, default) == "1"


# ---------------- COLORFUL BUTTONS ----------------
# Har button ko auto color: success = green | primary = blue | danger = red
_DANGER_WORDS = ("stop", "delete", "remove", "ban", "reject", "cancel")
_DANGER_EMOJI = ("🔴", "🗑", "🚫", "❌", "➖", "⛔")
_SUCCESS_WORDS = ("buy", "start", "add", "approve", "verify", "confirm", "upload", "unban", "unlock")
_SUCCESS_EMOJI = ("🟢", "✅", "➕", "📤", "▶")


def auto_style(text: str) -> str:
    t = (text or "").lower()
    if "back" in t or "🔙" in t or "⬅" in t:
        return "primary"
    if any(e in text for e in _DANGER_EMOJI) or any(w in t for w in _DANGER_WORDS):
        return "danger"
    if any(e in text for e in _SUCCESS_EMOJI) or any(w in t for w in _SUCCESS_WORDS):
        return "success"
    return "primary"


def Btn(text, *args, style=None, **kwargs):
    kwargs.setdefault("api_kwargs", {"style": style or auto_style(text)})
    return _InlineBtn(text, *args, **kwargs)


# ---------------- PREMIUM / LIMITS ----------------
def prem_value(uid):
    v = redis.get(f"prem:{uid}")
    if v is None:
        return None
    try:
        return int(str(v))          # 0 = lifetime
    except ValueError:
        return None


def is_premium(uid: int) -> bool:
    return prem_value(uid) is not None


def prem_text(uid: int) -> str:
    v = prem_value(uid)
    if v is None:
        return "Free"
    if v == 0:
        return "Premium (Lifetime)"
    days = max(0, v - int(time.time())) // 86400
    return f"Premium ({days} din baaki)"


def grant_premium(uid: int, days: int) -> None:
    if days <= 0:
        redis.set(f"prem:{uid}", "0")
    else:
        redis.set(f"prem:{uid}", str(int(time.time()) + days * 86400), ex=days * 86400)
    redis.sadd("premium_users", str(uid))


def revoke_premium(uid: int) -> None:
    redis.delete(f"prem:{uid}")
    redis.srem("premium_users", str(uid))


def bonus_slots(uid: int) -> int:
    try:
        return int(str(redis.get(f"bonus:{uid}") or 0))
    except ValueError:
        return 0


def slot_limit(uid: int):
    """None = unlimited (admin / premium)."""
    if is_admin(uid) or is_premium(uid):
        return None
    return MAX_SITES + bonus_slots(uid)


def slots_used(uid: int) -> int:
    return redis.scard(f"user:{uid}:sites") + redis.scard(f"user:{uid}:pending")


def at_limit(uid: int) -> bool:
    lim = slot_limit(uid)
    return lim is not None and slots_used(uid) >= lim


def limit_text(uid: int) -> str:
    lim = slot_limit(uid)
    return "∞" if lim is None else str(lim)


def needs_approval(uid: int) -> bool:
    if is_admin(uid):
        return False
    if not cfg_on("approval"):
        return False
    if is_premium(uid) and cfg_on("premauto"):
        return False
    return True


# ---------------- SITES / APPROVAL ----------------
def publish(name: str, content: str, uid: int) -> None:
    redis.set(f"site:{name}", content)
    owner = redis.get(f"owner:{name}")
    if not owner:
        redis.set(f"owner:{name}", str(uid))
        owner = uid
    redis.sadd(f"user:{owner}:sites", name)


def queue_pending(name: str, content: str, uid: int, kind: str) -> None:
    redis.set(f"pend:{name}", content)
    redis.set(f"pendowner:{name}", str(uid))
    redis.set(f"pendtype:{name}", kind)
    redis.sadd("pending", name)
    if kind == "new":
        redis.sadd(f"user:{uid}:pending", name)


def clear_pending(name: str) -> None:
    owner = redis.get(f"pendowner:{name}")
    if owner:
        redis.srem(f"user:{owner}:pending", name)
    redis.delete(f"pend:{name}", f"pendowner:{name}", f"pendtype:{name}")
    redis.srem("pending", name)


def delete_site(name: str):
    owner = redis.get(f"owner:{name}")
    if owner:
        redis.srem(f"user:{owner}:sites", name)
    redis.delete(f"site:{name}", f"owner:{name}")
    if redis.exists(f"pend:{name}"):
        clear_pending(name)


# ---------------- FLASK (sites serve karta hai) ----------------
web = Flask(__name__)

HOME_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Web Hosting Bot</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}}
h1{{font-size:2rem}}p{{color:#94a3b8}}</style></head>
<body><div><h1>🌐 Free HTML Hosting</h1>
<p>Telegram bot se apni HTML site live karo.</p>
<p>Developed by {H.escape(DEV_NAME)}</p></div></body></html>"""

NOT_FOUND = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>404</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}</style></head>
<body><div><h1>404</h1><p>Ye site exist nahi karti.</p></div></body></html>"""


@web.route("/")
def home():
    return Response(HOME_HTML, mimetype="text/html")


@web.route("/health")
def health():
    return "ok"


@web.route("/<name>")
def serve_site(name):
    name = name.lower()
    content = redis.get(f"site:{name}")
    if not content:
        return Response(NOT_FOUND, status=404, mimetype="text/html")
    return Response(content, mimetype="text/html")


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------- KEYBOARDS ----------------
def main_menu(uid: int) -> Markup:
    rows = [
        [Btn("📤 Site Upload Karo", callback_data="upload")],
        [Btn("📂 Meri Sites", callback_data="mysites"), Btn("🎁 Refer & Earn", callback_data="refer")],
        [Btn("💎 Premium", callback_data="premium"), Btn("ℹ️ Help", callback_data="help")],
    ]
    dev_row = []
    if DEV_USERNAME:
        dev_row.append(Btn("👨‍💻 Developer", url=f"https://t.me/{DEV_USERNAME}"))
    if is_admin(uid):
        dev_row.append(Btn("🛡 Admin Panel", callback_data="admin"))
    if dev_row:
        rows.append(dev_row)
    return Markup(rows)


def back_btn() -> Markup:
    return Markup([[Btn("🏠 Home", callback_data="home")]])


WELCOME = (
    "👋 <b>Welcome, {name}!</b>\n\n"
    "Main ek <b>Free HTML Hosting Bot</b> hu.\n"
    "Apni <code>.html</code> file bhejo, naam do, aur turant tumhari site live! 🚀\n\n"
    "{status}\n\n"
    "👇 Neeche button se shuru karo"
)


def welcome_text(uid: int, first_name) -> str:
    status = (
        f"🆔 <code>{uid}</code>\n"
        f"💎 Plan: <b>{prem_text(uid)}</b>\n"
        f"🌐 Sites: <b>{slots_used(uid)}/{limit_text(uid)}</b>"
    )
    return WELCOME.format(name=H.escape(first_name or "dost"), status=status)


def help_text() -> str:
    t = (
        "ℹ️ <b>Kaise use karein</b>\n\n"
        "1️⃣ <b>📤 Site Upload Karo</b> dabao\n"
        "2️⃣ Apni <code>.html</code> file bhejo\n"
        "3️⃣ Site ka naam bhejo (a-z, 0-9, -)\n"
        "4️⃣ Link mil jayega ✅\n\n"
        f"📏 Max file size: <b>{MAX_BYTES // (1024 * 1024)} MB</b>\n"
        f"🌐 Free sites per user: <b>{MAX_SITES}</b>\n"
        "✏️ Same naam se dobara upload karoge to site <b>update</b> ho jayegi.\n"
    )
    if cfg_on("approval"):
        t += "\n🛡 Site admin ke approve karne ke baad live hoti hai.\n"
    if cfg_on("referral"):
        t += "🎁 Dost ko invite karo aur extra site slots pao.\n"
    t += "💎 Premium me unlimited sites milti hain."
    return t


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:  # message same ho to error aata hai, ignore
        log.debug("edit skipped: %s", e)


async def edit_any(q, text, markup=None):
    """Text message ho ya document (caption wala) - dono edit kar deta hai."""
    m = q.message
    try:
        if m is not None and (m.document or m.caption is not None):
            await q.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await safe_edit(q, text, markup)
    except Exception as e:
        log.debug("edit_any skipped: %s", e)


JOIN_TEXT = (
    "🔒 <b>Bot use karne ke liye pehle ye channels join karo</b>\n\n"
    "Join karne ke baad neeche <b>✅ Maine Join Kar Liya</b> dabao."
)
PAGE = 8


def chan_info(cid):
    raw = redis.get(f"chan:{cid}")
    if not raw:
        return str(cid), ""
    title, _, link = str(raw).partition("\t")
    return title or str(cid), link


def user_label(uid):
    raw = redis.get(f"uinfo:{uid}")
    if raw:
        name, _, un = str(raw).partition("\t")
        return (name or str(uid)), un
    return str(uid), ""


async def missing_channels(bot, uid: int):
    out = []
    for cid in sorted(redis.smembers("channels") or []):
        try:
            m = await bot.get_chat_member(int(cid), uid)
            if m.status in ("left", "kicked"):
                out.append(cid)
        except Exception as e:
            log.warning("channel check failed %s: %s", cid, e)
    return out


def join_markup(missing) -> Markup:
    rows = []
    for cid in missing:
        title, link = chan_info(cid)
        if link:
            rows.append([Btn(f"📢 {title[:30]}", url=link)])
    rows.append([Btn("✅ Maine Join Kar Liya", callback_data="chk")])
    return Markup(rows)


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = aage badho. False = banned ya channel join nahi kiya."""
    u = update.effective_user
    uid = u.id
    if is_banned(uid):
        if update.callback_query:
            await update.callback_query.answer("🚫 Tum banned ho.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 Tumhe is bot se ban kar diya gaya hai.")
        return False
    redis.sadd("users", str(uid))
    redis.set(f"uinfo:{uid}", f"{(u.full_name or '').replace(chr(9), ' ')}\t{u.username or ''}")

    if not is_admin(uid):
        missing = await missing_channels(ctx.bot, uid)
        if missing:
            markup = join_markup(missing)
            q = update.callback_query
            if q:
                await q.answer("❌ Pehle saare channel join karo", show_alert=True)
                if q.data != "chk":
                    await safe_edit(q, JOIN_TEXT, markup)
            else:
                await update.effective_message.reply_text(
                    JOIN_TEXT, parse_mode=ParseMode.HTML, reply_markup=markup)
            return False
    return True


# ---------------- REFERRAL ----------------
async def credit_referral(ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    """Naya user jab channel check pass kar le tab referrer ko reward milta hai."""
    ref = redis.get(f"refby:{uid}")
    if not ref:
        return
    redis.delete(f"refby:{uid}")
    ref = str(ref)
    if not cfg_on("referral") or not ref.isdigit() or int(ref) == uid:
        return
    if is_banned(int(ref)) or not redis.sismember("users", ref):
        return
    if redis.sadd(f"refs:{ref}", str(uid)) != 1:
        return
    reward = int(cfg("ref_reward", "1"))
    redis.incrby(f"bonus:{ref}", reward)
    try:
        name, _ = user_label(uid)
        await ctx.bot.send_message(
            int(ref),
            f"🎉 <b>{H.escape(name)}</b> tumhare link se join hua!\n➕ Tumhe <b>+{reward}</b> extra site slot mila.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.debug("referral notify failed: %s", e)


async def show_refer(q, ctx):
    uid = q.from_user.id
    if not cfg_on("referral"):
        return await safe_edit(q, "🎁 Refer & Earn abhi band hai.", back_btn())
    link = f"https://t.me/{ctx.bot.username}?start=ref_{uid}"
    reward = cfg("ref_reward", "1")
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Free HTML hosting bot - apni site live karo!')}"
    await safe_edit(
        q,
        "🎁 <b>Refer & Earn</b>\n\n"
        f"Har naye dost par tumhe <b>+{reward}</b> extra site slot milega.\n\n"
        f"🔗 Tumhara link:\n<code>{link}</code>\n\n"
        f"👥 Referrals: <b>{redis.scard(f'refs:{uid}')}</b>\n"
        f"➕ Bonus slots: <b>{bonus_slots(uid)}</b>\n"
        f"🌐 Sites: <b>{slots_used(uid)}/{limit_text(uid)}</b>",
        Markup([
            [Btn("📤 Link Share Karo", url=share, style="success")],
            [Btn("🏠 Home", callback_data="home")],
        ]),
    )


async def show_premium(q):
    uid = q.from_user.id
    text = (
        "💎 <b>Premium</b>\n\n"
        f"Tumhara plan: <b>{prem_text(uid)}</b>\n\n"
        "<b>Fayde:</b>\n"
        "• ♾ Unlimited sites\n"
        + ("• ⚡ Admin approval ka wait nahi\n" if cfg_on("premauto") and cfg_on("approval") else "")
        + "\n"
        + (H.escape(PREMIUM_INFO) + "\n" if PREMIUM_INFO else "")
    )
    rows = []
    if DEV_USERNAME and not is_premium(uid):
        rows.append([Btn("💳 Premium Kharido", url=f"https://t.me/{DEV_USERNAME}", style="success")])
    rows.append([Btn("🏠 Home", callback_data="home")])
    await safe_edit(q, text, Markup(rows))


# ---------------- APPROVAL (admin review) ----------------
async def send_review(bot, chat_id: int, name: str) -> bool:
    content = redis.get(f"pend:{name}")
    if not content:
        return False
    owner = redis.get(f"pendowner:{name}") or "?"
    kind = redis.get(f"pendtype:{name}") or "new"
    uname, un = user_label(owner)
    cap = (
        "⏳ <b>Approval chahiye</b>\n\n"
        f"🌐 Site: <code>{H.escape(name)}</code> ({'update' if kind == 'update' else 'new'})\n"
        f"👤 {H.escape(uname)}" + (f" (@{H.escape(un)})" if un else "") + "\n"
        f"🆔 <code>{owner}</code>"
    )
    await bot.send_document(
        chat_id, document=str(content).encode("utf-8"), filename=f"{name}.html",
        caption=cap, parse_mode=ParseMode.HTML,
        reply_markup=Markup([[
            Btn("✅ Approve", callback_data=f"apv:{name}"),
            Btn("❌ Reject", callback_data=f"rej:{name}"),
        ]]),
    )
    return True


async def notify_admins(bot, name: str):
    for aid in ADMIN_IDS:
        try:
            await send_review(bot, aid, name)
        except Exception as e:
            log.warning("admin notify failed %s: %s", aid, e)


# ---------------- ADMIN SCREENS ----------------
async def show_admin(q):
    users = redis.scard("users")
    banned = redis.scard("banned")
    sites = len(redis.keys("site:*") or [])
    chans = redis.scard("channels")
    pend = redis.scard("pending")
    prem = sum(1 for x in (redis.smembers("premium_users") or []) if is_premium(int(x)))
    await safe_edit(
        q,
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Total users: <b>{users}</b>\n"
        f"🌐 Total sites: <b>{sites}</b>\n"
        f"⏳ Pending approval: <b>{pend}</b>\n"
        f"💎 Premium users: <b>{prem}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📡 Force-join channels: <b>{chans}</b>\n\n"
        "<b>Commands:</b>\n"
        "<code>/ban user_id</code>\n"
        "<code>/unban user_id</code>\n"
        "<code>/delsite name</code>\n"
        "<code>/addprem user_id days</code> (0 = lifetime)\n"
        "<code>/removeprem user_id</code>",
        Markup([
            [Btn("👥 Users & Sites", callback_data="users:0")],
            [Btn("⏳ Pending Approval", callback_data="pending"), Btn("💎 Premium Users", callback_data="premlist")],
            [Btn("📡 Channels", callback_data="channels"), Btn("📢 Broadcast", callback_data="broadcast")],
            [Btn("⚙️ Settings", callback_data="settings")],
            [Btn("🔄 Refresh", callback_data="admin"), Btn("🏠 Home", callback_data="home")],
        ]),
    )


async def show_settings(q):
    def mark(k):
        return "✅" if cfg_on(k) else "❌"

    await safe_edit(
        q,
        "⚙️ <b>Settings</b>\n\n"
        "🛡 <b>Admin Approval</b> - nayi site / update admin approve kare tabhi live ho\n"
        "⚡ <b>Premium Auto-Approve</b> - premium users ko approval nahi lagta\n"
        "🎁 <b>Refer & Earn</b> - referral system on/off\n"
        f"➕ Reward per referral: <b>{cfg('ref_reward', '1')}</b> slot\n\n"
        "Button dabake on/off karo.",
        Markup([
            [Btn(f"{mark('approval')} Admin Approval", callback_data="tg:approval")],
            [Btn(f"{mark('premauto')} Premium Auto-Approve", callback_data="tg:premauto")],
            [Btn(f"{mark('referral')} Refer & Earn", callback_data="tg:referral")],
            [Btn("✏️ Referral Reward Badlo", callback_data="setreward")],
            [Btn("⬅️ Back", callback_data="admin")],
        ]),
    )


async def show_pending(q):
    names = sorted(redis.smembers("pending") or [])
    rows = [[Btn(f"⏳ {n}", callback_data=f"pv:{n}")] for n in names[:25]]
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        f"⏳ <b>Pending Approval ({len(names)})</b>\n\nKisi par tap karo, file review ke liye bhej di jayegi."
        if names else "⏳ Abhi koi site approval ke liye pending nahi hai. ✅"
    )
    await safe_edit(q, text, Markup(rows))


async def show_premlist(q):
    ids = sorted(redis.smembers("premium_users") or [], key=lambda x: int(x))
    active = []
    for x in ids:
        if is_premium(int(x)):
            active.append(x)
        else:
            redis.srem("premium_users", x)
    rows = []
    for x in active[:30]:
        name, _ = user_label(x)
        rows.append([Btn(f"💎 {name[:18]} • {prem_text(int(x)).replace('Premium ', '')}", callback_data=f"u:{x}:0")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(
        q,
        f"💎 <b>Premium Users ({len(active)})</b>\n\nNaya premium dene ke liye <code>/addprem user_id days</code>"
        " ya Users me user kholo.",
        Markup(rows),
    )


async def show_users(q, page: int):
    ids = sorted(redis.smembers("users") or [], key=lambda x: int(x))
    total = len(ids)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE:(page + 1) * PAGE]
    rows = []
    for x in chunk:
        name, _ = user_label(x)
        cnt = redis.scard(f"user:{x}:sites")
        flag = "🚫 " if is_banned(int(x)) else ("💎 " if is_premium(int(x)) else "")
        rows.append([Btn(f"{flag}{name[:18]} • 🌐 {cnt}", callback_data=f"u:{x}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("Next ➡️", callback_data=f"users:{page + 1}"))
    rows.append(nav)
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(
        q, f"👥 <b>Users ({total})</b>\n\nKisi user par tap karo, uski sites dikhengi.\n"
           "Format: naam • 🌐 sites ki sankhya", Markup(rows))


async def show_user(q, x: str, page: int):
    name, un = user_label(x)
    sites = sorted(redis.smembers(f"user:{x}:sites") or [])
    banned = is_banned(int(x))
    text = (
        f"👤 <b>{H.escape(name)}</b>\n"
        f"🆔 <code>{x}</code>\n"
        + (f"🔗 @{H.escape(un)}\n" if un else "")
        + f"🌐 Sites: <b>{len(sites)}</b>\n"
        f"💎 Plan: <b>{prem_text(int(x))}</b>\n"
        f"👥 Referrals: <b>{redis.scard(f'refs:{x}')}</b> • ➕ Bonus: <b>{bonus_slots(int(x))}</b>\n"
        f"🚦 Status: <b>{'Banned 🚫' if banned else 'Active ✅'}</b>\n"
    )
    if sites:
        text += "\n" + "\n".join(f'• <a href="{site_url(n)}">{H.escape(n)}</a>' for n in sites)
    toggle = (Btn("✅ Unban", callback_data=f"unban:{x}:{page}") if banned
              else Btn("🚫 Ban", callback_data=f"ban:{x}:{page}"))
    prem_row = [
        Btn("➕ Premium 30d", callback_data=f"gp:{x}:30:{page}"),
        Btn("➕ Lifetime", callback_data=f"gp:{x}:0:{page}"),
    ]
    rows = [[toggle], prem_row]
    if is_premium(int(x)):
        rows.append([Btn("➖ Premium Hatao", callback_data=f"rp:{x}:{page}")])
    rows.append([Btn("⬅️ Back", callback_data=f"users:{page}")])
    await safe_edit(q, text, Markup(rows))


async def show_channels(q):
    chans = sorted(redis.smembers("channels") or [])
    rows = []
    for c in chans:
        title, _ = chan_info(c)
        rows.append([Btn(f"❌ {title[:28]}", callback_data=f"chdel:{c}")])
    rows.append([Btn("➕ Channel Add Karo", callback_data="addch")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        "📡 <b>Force-Join Channels</b>\n\n"
        "Users ko bot use karne se pehle ye channels join karne honge.\n"
        "Hatane ke liye channel par tap karo.\n\n"
        + (f"Total: <b>{len(chans)}</b>" if chans else "Abhi koi channel add nahi hai.")
    )
    await safe_edit(q, text, Markup(rows))


# ---------------- COMMANDS ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    # referral: sirf bilkul naye user ke liye (pehle se registered ke liye nahi)
    if ctx.args and ctx.args[0].startswith("ref_") and not redis.sismember("users", str(u.id)):
        ref = ctx.args[0][4:]
        if ref.isdigit() and int(ref) != u.id:
            redis.set(f"refby:{u.id}", ref)
    if not await guard(update, ctx):
        return
    await credit_referral(ctx, u.id)
    ctx.user_data.clear()
    await update.message.reply_text(
        welcome_text(u.id, u.first_name),
        parse_mode=ParseMode.HTML, reply_markup=main_menu(u.id),
    )


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /ban <user_id>")
    redis.sadd("banned", ctx.args[0])
    await update.message.reply_text(f"🚫 {ctx.args[0]} ban ho gaya.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /unban <user_id>")
    redis.srem("banned", ctx.args[0])
    await update.message.reply_text(f"✅ {ctx.args[0]} unban ho gaya.")


async def cmd_delsite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        return await update.message.reply_text("Use: /delsite <name>")
    name = ctx.args[0].lower()
    if not redis.exists(f"site:{name}"):
        return await update.message.reply_text("❌ Aisi site nahi mili.")
    delete_site(name)
    await update.message.reply_text(f"🗑 <code>{H.escape(name)}</code> delete ho gayi.", parse_mode=ParseMode.HTML)


async def cmd_addprem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /addprem <user_id> <days>  (0 = lifetime)")
    days = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else 30
    uid = int(ctx.args[0])
    grant_premium(uid, days)
    await update.message.reply_text(
        f"💎 {uid} ko Premium mil gaya ({'lifetime' if days == 0 else f'{days} din'}).")
    try:
        await ctx.bot.send_message(uid, "💎 <b>Mubarak ho!</b> Tumhe Premium mil gaya. Ab unlimited sites banao! 🚀",
                                   parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_removeprem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /removeprem <user_id>")
    revoke_premium(int(ctx.args[0]))
    await update.message.reply_text(f"➖ {ctx.args[0]} ka Premium hata diya.")


# ---------------- BUTTONS ----------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()

    if data in ("home", "chk"):
        ctx.user_data.clear()
        if data == "chk":
            await credit_referral(ctx, uid)
        await safe_edit(q, welcome_text(uid, q.from_user.first_name), main_menu(uid))

    elif data == "help":
        await safe_edit(q, help_text(), back_btn())

    elif data == "refer":
        await show_refer(q, ctx)

    elif data == "premium":
        await show_premium(q)

    elif data == "upload":
        if at_limit(uid):
            return await safe_edit(
                q, f"❌ Tumhari limit ({limit_text(uid)} sites) poori ho gayi.\n"
                   "Pehle koi site delete karo, dost ko invite karo (🎁) ya 💎 Premium lo.",
                Markup([
                    [Btn("📂 Meri Sites", callback_data="mysites"), Btn("🎁 Refer & Earn", callback_data="refer")],
                    [Btn("🏠 Home", callback_data="home")],
                ]),
            )
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        await safe_edit(
            q, "📤 <b>Ab apni .html file bhejo</b>\n\n(File ko document ki tarah attach karke bhejo)",
            Markup([[Btn("❌ Cancel", callback_data="home")]]),
        )

    elif data == "mysites":
        names = sorted(redis.smembers(f"user:{uid}:sites") or [])
        pend = sorted(redis.smembers(f"user:{uid}:pending") or [])
        if not names and not pend:
            return await safe_edit(
                q, "📂 Tumhari koi site nahi hai abhi.",
                Markup([[Btn("📤 Upload Karo", callback_data="upload")], [Btn("🏠 Home", callback_data="home")]]),
            )
        rows = [[Btn(f"🌐 {n}", callback_data=f"site:{n}")] for n in names]
        rows.append([Btn("🏠 Home", callback_data="home")])
        text = f"📂 <b>Tumhari Sites ({len(names)})</b>\n\nKisi par tap karo:"
        if pend:
            text += "\n\n⏳ <b>Approval ka wait:</b>\n" + "\n".join(f"• <code>{H.escape(n)}</code>" for n in pend)
        await safe_edit(q, text, Markup(rows))

    elif data.startswith("site:"):
        name = data[5:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        await safe_edit(
            q, f"🌐 <b>{H.escape(name)}</b>\n\n🔗 {site_url(name)}",
            Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("♻️ Update", callback_data=f"upd:{name}"), Btn("🗑 Delete", callback_data=f"del:{name}")],
                [Btn("⬅️ Back", callback_data="mysites")],
            ]),
        )

    elif data.startswith("upd:"):
        name = data[4:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        ctx.user_data["update_name"] = name
        await safe_edit(
            q, f"♻️ <b>{H.escape(name)}</b> update karne ke liye nayi .html file bhejo.",
            Markup([[Btn("❌ Cancel", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("del:"):
        name = data[4:]
        await safe_edit(
            q, f"⚠️ Pakka <b>{H.escape(name)}</b> delete karni hai?",
            Markup([[Btn("✅ Haan, Delete", callback_data=f"delok:{name}"), Btn("❌ Nahi", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("delok:"):
        name = data[6:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        delete_site(name)
        await safe_edit(q, f"🗑 <b>{H.escape(name)}</b> delete ho gayi.",
                        Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]))

    # ----- ADMIN -----
    elif data == "noop":
        pass

    elif data == "admin" and is_admin(uid):
        await show_admin(q)

    elif data == "settings" and is_admin(uid):
        await show_settings(q)

    elif data.startswith("tg:") and is_admin(uid):
        key = data[3:]
        if key in ("approval", "premauto", "referral"):
            cfg_set(key, "0" if cfg_on(key) else "1")
        await show_settings(q)

    elif data == "setreward" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "set_reward"
        await safe_edit(q, "✏️ Har referral par kitne extra site slot milein? Number bhejo (1-50).",
                        Markup([[Btn("❌ Cancel", callback_data="settings")]]))

    elif data == "pending" and is_admin(uid):
        await show_pending(q)

    elif data.startswith("pv:") and is_admin(uid):
        ok = await send_review(ctx.bot, uid, data[3:])
        if not ok:
            await q.answer("Ye ab pending nahi hai.", show_alert=True)
            await show_pending(q)

    elif (data.startswith("apv:") or data.startswith("rej:")) and is_admin(uid):
        act, name = data.split(":", 1)
        content = redis.get(f"pend:{name}")
        if not content:
            await q.answer("Ye pehle hi handle ho chuka hai.", show_alert=True)
            return await edit_any(q, "ℹ️ Ye request pehle hi handle ho chuki hai.", None)
        owner = int(str(redis.get(f"pendowner:{name}") or 0))
        if act == "apv":
            publish(name, str(content), owner)
            clear_pending(name)
            await edit_any(q, f"✅ <b>{H.escape(name)}</b> approve ho gayi.\n🔗 {site_url(name)}", None)
            try:
                await ctx.bot.send_message(
                    owner,
                    f"✅ <b>Approve ho gayi!</b>\n\n🌐 <code>{H.escape(name)}</code> ab live hai.\n🔗 {site_url(name)}",
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    reply_markup=Markup([[Btn("🔗 Open Site", url=site_url(name))]]),
                )
            except Exception as e:
                log.debug("approve notify failed: %s", e)
        else:
            clear_pending(name)
            await edit_any(q, f"❌ <b>{H.escape(name)}</b> reject ho gayi.", None)
            try:
                await ctx.bot.send_message(
                    owner,
                    f"❌ Tumhari site <code>{H.escape(name)}</code> admin ne reject kar di.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                log.debug("reject notify failed: %s", e)

    elif data == "premlist" and is_admin(uid):
        await show_premlist(q)

    elif data.startswith("gp:") and is_admin(uid):
        _, x, days, pg = data.split(":")
        grant_premium(int(x), int(days))
        try:
            await ctx.bot.send_message(
                int(x), "💎 <b>Mubarak ho!</b> Tumhe Premium mil gaya. Ab unlimited sites banao! 🚀",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await show_user(q, x, int(pg))

    elif data.startswith("rp:") and is_admin(uid):
        _, x, pg = data.split(":")
        revoke_premium(int(x))
        await show_user(q, x, int(pg))

    elif data.startswith("users:") and is_admin(uid):
        await show_users(q, int(data.split(":")[1]))

    elif data.startswith("u:") and is_admin(uid):
        _, x, pg = data.split(":")
        await show_user(q, x, int(pg))

    elif (data.startswith("ban:") or data.startswith("unban:")) and is_admin(uid):
        act, x, pg = data.split(":")
        if act == "ban":
            redis.sadd("banned", x)
        else:
            redis.srem("banned", x)
        await show_user(q, x, int(pg))

    elif data == "channels" and is_admin(uid):
        await show_channels(q)

    elif data == "addch" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_channel"
        await safe_edit(
            q,
            "📡 <b>Channel add karo</b>\n\n"
            "1️⃣ Pehle bot ko us channel mein <b>Admin</b> banao\n"
            "2️⃣ Phir channel ka <code>@username</code> ya ID bhejo,\n"
            "    ya channel ka koi message yahan <b>forward</b> karo.",
            Markup([[Btn("❌ Cancel", callback_data="channels")]]),
        )

    elif data.startswith("chdel:") and is_admin(uid):
        cid = data[6:]
        redis.srem("channels", cid)
        redis.delete(f"chan:{cid}")
        await show_channels(q)

    elif data == "broadcast" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "broadcast"
        await safe_edit(q, "📢 Jo message sabko bhejna hai wo ab likh kar bhejo.",
                        Markup([[Btn("❌ Cancel", callback_data="admin")]]))


# ---------------- FILE RECEIVE ----------------
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if not fname.endswith((".html", ".htm")):
        return await update.message.reply_text("❌ Sirf <code>.html</code> file bhejo.", parse_mode=ParseMode.HTML)
    if doc.file_size and doc.file_size > MAX_BYTES:
        return await update.message.reply_text(f"❌ File bahut badi hai. Max {MAX_BYTES // (1024 * 1024)} MB.")

    if ctx.user_data.get("state") != "await_file":
        if at_limit(uid):
            return await update.message.reply_text(f"❌ Limit ({limit_text(uid)} sites) poori ho gayi.")

    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    try:
        content = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        content = bytes(raw).decode("latin-1")

    ctx.user_data["html"] = content
    update_name = ctx.user_data.get("update_name")

    if update_name:
        if needs_approval(uid):
            queue_pending(update_name, content, uid, "update")
            ctx.user_data.clear()
            await notify_admins(ctx.bot, update_name)
            return await update.message.reply_text(
                f"⏳ <b>{H.escape(update_name)}</b> ka update admin approval ke liye bhej diya.\n"
                "Approve hote hi site update ho jayegi (tab tak purani site chalti rahegi).",
                parse_mode=ParseMode.HTML, reply_markup=back_btn(),
            )
        publish(update_name, content, uid)
        ctx.user_data.clear()
        return await update.message.reply_text(
            f"✅ <b>{H.escape(update_name)}</b> update ho gayi!\n\n🔗 {site_url(update_name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup([[Btn("🔗 Open Site", url=site_url(update_name))], [Btn("🏠 Home", callback_data="home")]]),
        )

    ctx.user_data["state"] = "await_name"
    await update.message.reply_text(
        "✅ File mil gayi!\n\n✍️ Ab <b>site ka naam</b> bhejo.\n"
        "Rules: 3-30 characters, sirf <code>a-z 0-9 -</code>\n"
        "Example: <code>my-portfolio</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=Markup([[Btn("❌ Cancel", callback_data="home")]]),
    )


# ---------------- TEXT ----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    if state == "set_reward" and is_admin(uid):
        if not text.isdigit() or not (1 <= int(text) <= 50):
            return await update.message.reply_text("❌ 1 se 50 ke beech number bhejo.")
        cfg_set("ref_reward", int(text))
        ctx.user_data.clear()
        return await update.message.reply_text(
            f"✅ Ab har referral par <b>+{int(text)}</b> slot milega.", parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("⚙️ Settings", callback_data="settings")]]),
        )

    if state == "add_channel" and is_admin(uid):
        ref = None
        fo = getattr(update.message, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            ref = fo.chat.id
        elif text.startswith("@"):
            ref = text
        elif text.lstrip("-").isdigit():
            ref = int(text)
        if ref is None:
            return await update.message.reply_text("❌ @username, ID ya forwarded message bhejo.")
        try:
            chat = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi hai")
        except Exception as e:
            log.warning("add channel failed: %s", e)
            return await update.message.reply_text(
                "❌ Channel nahi mila ya bot us channel mein admin nahi hai.\n"
                "Bot ko admin banao aur dobara bhejo.")
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try:
                link = chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id)
            except Exception:
                link = ""
        title = (chat.title or str(chat.id)).replace("\t", " ")
        redis.sadd("channels", str(chat.id))
        redis.set(f"chan:{chat.id}", f"{title}\t{link}")
        ctx.user_data.clear()
        warn = "" if link else "\n\n⚠️ Invite link nahi mila. Bot ko 'Invite users' permission do, warna users ko join button nahi dikhega."
        return await update.message.reply_text(
            f"✅ <b>{H.escape(title)}</b> add ho gaya!{warn}",
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("📡 Channels", callback_data="channels")], [Btn("🛡 Admin Panel", callback_data="admin")]]),
        )

    if state == "broadcast" and is_admin(uid):
        users = redis.smembers("users") or []
        ok = fail = 0
        await update.message.reply_text(f"📢 Bhej raha hu... ({len(users)} users)")
        for u in users:
            try:
                await ctx.bot.send_message(int(u), text)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)   # Telegram flood limit se bachne ke liye
        ctx.user_data.clear()
        return await update.message.reply_text(f"✅ Done\nSent: {ok}\nFailed: {fail}", reply_markup=back_btn())

    if state == "await_name":
        name = text.lower()
        if not NAME_RE.match(name):
            return await update.message.reply_text(
                "❌ Galat naam. 3-30 characters, sirf <code>a-z 0-9 -</code>, shuru letter/number se.",
                parse_mode=ParseMode.HTML,
            )
        if name in RESERVED:
            return await update.message.reply_text("❌ Ye naam reserved hai, koi aur naam do.")
        if redis.exists(f"site:{name}") or redis.exists(f"pend:{name}"):
            return await update.message.reply_text("❌ Ye naam pehle se le liya gaya hai. Koi aur naam do.")
        if at_limit(uid):
            ctx.user_data.clear()
            return await update.message.reply_text(f"❌ Limit ({limit_text(uid)} sites) poori ho gayi.",
                                                   reply_markup=main_menu(uid))

        html_content = ctx.user_data.get("html")
        if not html_content:
            ctx.user_data.clear()
            return await update.message.reply_text("⚠️ File nahi mili, dobara upload karo.", reply_markup=main_menu(uid))

        if needs_approval(uid):
            queue_pending(name, html_content, uid, "new")
            ctx.user_data.clear()
            await notify_admins(ctx.bot, name)
            return await update.message.reply_text(
                f"⏳ <b>Site admin approval ke liye bhej di gayi!</b>\n\n🌐 Naam: <code>{name}</code>\n"
                f"🔗 {site_url(name)}\n\nApprove hote hi link live ho jayega, tumhe message aa jayega.",
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=Markup([[Btn("📂 Meri Sites", callback_data="mysites"), Btn("🏠 Home", callback_data="home")]]),
            )

        publish(name, html_content, uid)
        ctx.user_data.clear()

        return await update.message.reply_text(
            f"🎉 <b>Site live ho gayi!</b>\n\n🌐 Naam: <code>{name}</code>\n🔗 {site_url(name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("📂 Meri Sites", callback_data="mysites"), Btn("🏠 Home", callback_data="home")],
            ]),
        )

    await update.message.reply_text("👇 Menu se option chuno", reply_markup=main_menu(uid))


# ---------------- RUN ----------------
def main():
    threading.Thread(target=run_web, daemon=True).start()
    # Python 3.14 mein event loop khud banana padta hai
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delsite", cmd_delsite))
    app.add_handler(CommandHandler("addprem", cmd_addprem))
    app.add_handler(CommandHandler("removeprem", cmd_removeprem))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
