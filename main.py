# -*- coding: utf-8 -*-
"""
BOT 83692929  -  FULL BOT IN PURE PYTHON  (converted from Telebot Creator / TPY)

Install :  pip install -r requirements.txt
Run     :  python main.py

ENVIRONMENT VARIABLES  (Render -> Environment tab)
    BOT_TOKEN          REQUIRED  - bot token from @BotFather
    DB_FILE            json db path (default bot_db.json). On Render use a Disk, e.g. /data/bot_db.json
    WEBHOOK_BASE_URL   public https url (auto = RENDER_EXTERNAL_URL on Render)
    PORT               web port (Render sets it automatically)
    KEEP_ALIVE         1 = ping own url every 10 min so free Render service does not sleep (default 1)

RENDER SETTINGS
    Service type   : Web Service
    Build Command  : pip install -r requirements.txt
    Start Command  : python main.py

STRUCTURE OF THIS FILE
    1. RUNTIME      -> Bot, bot, User, libs, HTTP ... re-written in plain Python
    2. COMMANDS     -> every TPY command is one python function, in this format:

           # ===/start===
           def cmd_start(): ...

    3. DISPATCHER   -> routes messages / buttons / join requests to the commands
"""
import os
import sys
import re
import json
import time
import copy
import html
import atexit
import random
import threading
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import telebot
from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardRemove, LinkPreviewOptions)

try:
    from zoneinfo import ZoneInfo
except Exception:                                   # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "000000:PUT_YOUR_BOT_TOKEN_HERE")
DB_FILE = os.environ.get("DB_FILE", "bot_db.json")
WEBHOOK_BASE_URL = (os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "1") != "0"
PORT = int(os.environ.get("PORT", "8080"))
bot_token = BOT_TOKEN


# ==========================================================================
# 1. RUNTIME  (what Telebot Creator gave you for free)
# ==========================================================================
class ReturnCommand(Exception):
    """TPY: `raise ReturnCommand` = stop the current command (not an error)."""


returncommand = returnCommand = ReturnCommand


class Bunch(dict):
    """dict with attribute access (TPY 'bunchify')."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


def bunchify(x):
    if isinstance(x, dict):
        return Bunch({k: bunchify(v) for k, v in x.items()})
    if isinstance(x, list):
        return [bunchify(i) for i in x]
    return x


def encodeURIComponent(s):
    return urllib.parse.quote(str(s), safe="~()*!.'")


def rawurlencode(s):
    return urllib.parse.quote(str(s), safe="")


def apply_html_entities(text, entities=None, custom_subs=None):
    try:
        from telebot.formatting import apply_html_entities as _a
        return _a(text, entities, custom_subs or {})
    except Exception:
        return html.escape(text or "")


# ---------------------------------------------------------------- storage
class _DB:
    """Tiny JSON database replacing TBC cloud storage."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.dirty = False
        self.d = {"bot": {}, "user": {}, "res": {}, "pending": {}, "users": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.d.update(json.load(f))
        except Exception:
            pass
        threading.Thread(target=self._saver, daemon=True).start()
        atexit.register(self.save)

    def touch(self):
        self.dirty = True

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.path)
            self.dirty = False

    def _saver(self):
        while True:
            time.sleep(2)
            try:
                self.save()
            except Exception:
                pass


db = _DB(DB_FILE)


def _plain(v):
    """make a value json-safe (sets/tuples -> list) and detached from the original."""
    return json.loads(json.dumps(v, default=lambda o: list(o) if isinstance(o, (set, tuple)) else str(o)))


def _uid(user=None):
    return str(user if user is not None else u)


# ----------------------------------------------------------- Telegram api
_api = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)


def _snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class _TPYBot:
    """`bot.*`  (Telegram methods, camelCase like in TPY)."""
    def __init__(self, api):
        self._api = api

    def replyText(self, chat_id, text, **kw):
        return self._api.send_message(chat_id, text, **kw)

    def sendMessage(self, text=None, chat_id=None, **kw):
        return self._api.send_message(chat_id if chat_id is not None else u, text, **kw)

    def info(self, *a, **k):
        return Bot.info()

    def __getattr__(self, name):
        fn = getattr(self._api, _snake(name), None)
        if fn is None:
            raise AttributeError("bot.%s is not supported" % name)
        return fn


bot = _TPYBot(_api)


# ----------------------------------------------------------- run-time context
# TPY injects these names into every command; here they are module globals that
# the dispatcher sets before a command function runs.
u = None            # user id (str)
message = None      # telegram Message / ChatMemberUpdated / ChatJoinRequest
call = None         # CallbackQuery (for inline buttons)
options = None      # value passed with runCommand / handleNextCommand ...
params = None       # text after the command   ( "/start 123" -> "123" )
msg = None          # raw text of the message / button data
update_type = "message"
_CTX = ("u", "message", "call", "options", "params", "msg", "update_type")
_ctx_lock = threading.RLock()
COMMANDS = {}       # filled at the bottom of the file
_LAST_MSG = {}      # last message per user (used for scheduled / webhook runs)
_NOT_FOUND, _DONE, _RETURNED = 0, 1, 2


def _find(name):
    if name is None:
        return None
    name = str(name).strip()
    for k in (name, "/" + name.lstrip("/"), name.lstrip("/")):
        if k in COMMANDS:
            return COMMANDS[k]
    low = name.lower()
    for k, f in COMMANDS.items():
        if k.lower() in (low, "/" + low.lstrip("/"), low.lstrip("/")):
            return f
    return None


def _invoke(name, **ctx):
    """Run one command function inside a (nested-safe) context."""
    func = _find(name)
    if func is None:
        return _NOT_FOUND
    with _ctx_lock:
        g = globals()
        snap = {k: g[k] for k in _CTX}
        g.update(ctx)
        try:
            func()
            return _DONE
        except ReturnCommand:
            return _RETURNED
        finally:
            g.update(snap)


def _split_cmd(text):
    text = str(text).strip()
    parts = text.split(None, 1)
    name = parts[0] if parts else ""
    if name.startswith("/") and "@" in name:
        name = name.split("@", 1)[0]
    return name, (parts[1].strip() if len(parts) > 1 else None)


def _resolve(text):
    """'/admin AP' -> ('/admin','AP');  '💴 Account1' (name with a space) -> whole name."""
    text = str(text).strip()
    if _find(text):
        return text, None
    return _split_cmd(text)


class _TPYBotGlobal:
    """`Bot.*`  (bot-wide data, command control, broadcast)."""
    # -- data
    def getData(self, name, bot_id=None, api_key=None):
        with db.lock:
            return copy.deepcopy(db.d["bot"].get(name))

    def saveData(self, name, data, bot_id=None, api_key=None):
        with db.lock:
            db.d["bot"][name] = _plain(data)
            db.touch()
        return {"status": "success"}

    setData = saveData

    def deleteData(self, name, bot_id=None):
        with db.lock:
            db.d["bot"].pop(name, None)
            db.touch()
        return {"status": "success"}

    def deleteAllData(self, except_data=None):
        with db.lock:
            keep = {k: v for k, v in db.d["bot"].items() if k in (except_data or [])}
            db.d["bot"] = keep
            db.touch()

    # -- command control
    def runCommand(self, command, options=None):
        name, prm = _resolve(command)
        r = _invoke(name, options=options, params=prm)
        if r == _NOT_FOUND:
            raise KeyError("Command not found: %s" % command)
        return {"status": "success"}

    def handleNextCommand(self, command, options=None, cancel_at_command=False):
        with db.lock:
            db.d["pending"][_uid()] = {"cmd": command, "options": _plain(options),
                                       "cancel": bool(cancel_at_command)}
            db.touch()
        return {"status": "success"}

    _jobs = {}

    def runCommandAfter(self, timeout, command, options=None):
        if isinstance(timeout, datetime):
            timeout = max(1, (timeout - datetime.now(timeout.tzinfo)).total_seconds())
        timeout = max(1, float(timeout))
        uid, m, c = u, message, call
        job_id = "job%d" % random.randint(10 ** 8, 10 ** 9)

        def _run():
            self._jobs.pop(job_id, None)
            _run_for_user(uid, command, options=options, message_=m, call_=c)

        t = threading.Timer(timeout, _run)
        t.daemon = True
        self._jobs[job_id] = t
        t.start()
        return {"id": job_id, "command": command, "timeout": timeout}

    def cancelScheduledTask(self, job_id):
        t = self._jobs.pop(job_id, None)
        if t:
            t.cancel()

    # -- info / misc
    _me = None

    def info(self, bot_id=None, api_key=None):
        if self._me is None:
            try:
                me = _api.get_me()
                self._me = {"id": me.id, "username": me.username, "first_name": me.first_name,
                            "name": me.first_name}
            except Exception:
                self._me = {"id": None, "username": "", "first_name": "", "name": ""}
        d = dict(self._me)
        d["token"] = BOT_TOKEN
        d["bot_token"] = BOT_TOKEN
        return Bunch(d)

    def log(self, *a):
        print("[Bot.log]", *a)

    def replyText(self, chat_id, text, **kw):
        return _api.send_message(chat_id, text, **kw)

    def sendMessage(self, text=None, chat_id=None, **kw):
        return _api.send_message(chat_id if chat_id is not None else u, text, **kw)

    def genId(self):
        return "%x" % random.getrandbits(48)

    genRandomId = gen_random_id = genRandomErrorId = genId

    def getBlockedUsers(self):
        return list(db.d["bot"].get("__blocked__", []))

    def blockUser(self, user_id):
        with db.lock:
            b = db.d["bot"].setdefault("__blocked__", [])
            if str(user_id) not in b:
                b.append(str(user_id))
            db.touch()

    def unblockUser(self, user_id):
        with db.lock:
            b = db.d["bot"].setdefault("__blocked__", [])
            if str(user_id) in b:
                b.remove(str(user_id))
            db.touch()

    # -- broadcast (runs in a background thread)
    _bc = {}

    def broadcast(self, code=None, command=None, callback_url=None, bot_id=None, api_key=None,
                  function=None, warnings=None, mode=None, bot_ids=None, speed=None, **kwargs):
        fn = function or command
        if not fn:
            return {"status": "error", "message": "function is required"}
        bid = self.genId()
        users = [str(x) for x in db.d.get("users", [])]
        st = {"id": bid, "broadcast_id": bid, "status": "running", "total": len(users),
              "total_users": len(users), "total_success": 0, "total_error": 0, "total_failed": 0}
        self._bc[bid] = st
        delay = 1.0 / max(1, min(25, int(speed or 8)))

        def _work():
            for uid in users:
                if st["status"] == "stopped":
                    break
                try:
                    if str(fn).startswith("/") or _find(fn):
                        _run_for_user(uid, fn, options=kwargs)
                    else:
                        getattr(_api, _snake(fn))(chat_id=uid, **kwargs)
                    st["total_success"] += 1
                except Exception:
                    st["total_error"] += 1
                    st["total_failed"] += 1
                time.sleep(delay)
            if st["status"] != "stopped":
                st["status"] = "completed"
            if callback_url:
                try:
                    requests.post(callback_url, json=dict(st), timeout=15)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()
        return {"status": "success", "id": bid, "broadcast_id": bid}

    def getBroadcastStatus(self, broadcast_id):
        return dict(self._bc.get(broadcast_id, {"status": "unknown"}))

    def stopBroadcast(self, broadcast_id):
        if broadcast_id in self._bc:
            self._bc[broadcast_id]["status"] = "stopped"
        return {"status": "success"}

    def clearBroadcast(self, broadcast_id=None):
        self._bc.pop(broadcast_id, None)
        return "ok"

    def __getattr__(self, name):
        raise AttributeError("Bot.%s is not implemented in this Python port" % name)


Bot = _TPYBotGlobal()


class _TPYUser:
    """`User.*`  (per-user data)."""
    def getData(self, name, user=None, bot_id=None, api_key=None):
        with db.lock:
            return copy.deepcopy(db.d["user"].get(_uid(user), {}).get(name))

    def saveData(self, name, data, user=None, data_type=None, bot_id=None, api_key=None):
        with db.lock:
            db.d["user"].setdefault(_uid(user), {})[name] = _plain(data)
            db.touch()
        return {"status": "success"}

    def deleteData(self, name, user=None, data_type=None, bot_id=None, api_key=None):
        with db.lock:
            db.d["user"].get(_uid(user), {}).pop(name, None)
            db.touch()
        return {"status": "success"}

    def deleteAllData(self, name=None, old_data=False, except_data=None):
        with db.lock:
            d = db.d["user"].get(_uid(), {})
            for k in list(d):
                if k not in (except_data or []):
                    d.pop(k)
            db.touch()

    def deleteAllDataOfUser(self, user):
        with db.lock:
            db.d["user"].pop(_uid(user), None)
            db.touch()

    def getAllDataOfUser(self, user, output_format="json"):
        return copy.deepcopy(db.d["user"].get(_uid(user), {}))


User = _TPYUser()


class _Account:
    def getData(self, name):
        return Bot.getData("__acc__" + name)

    def saveData(self, name, data):
        return Bot.saveData("__acc__" + name, data)

    def __getattr__(self, name):
        return lambda *a, **k: {}


Account = _Account()


# ----------------------------------------------------------------- libs
class _Res:
    def __init__(self, scope, name):
        self.scope, self.name = scope, str(name)

    def value(self):
        return db.d["res"].get(self.scope, {}).get(self.name, 0)

    def set(self, n):
        with db.lock:
            db.d["res"].setdefault(self.scope, {})[self.name] = n
            db.touch()
        return n

    def add(self, n):
        with db.lock:
            return self.set(self.value() + n)

    def cut(self, n):
        with db.lock:
            return self.set(self.value() - n)

    def reset(self):
        return self.set(0)

    def getAllData(self):
        return dict(db.d["res"].get(self.scope, {}))

    fetchAllResources = getAllData


class _Resources:
    def userRes(self, name, user=None, bot_id=None, api_key=None):
        return _Res("user:" + _uid(user), name)

    def anotherRes(self, name, user=None, bot_id=None, api_key=None):
        return _Res("user:" + _uid(user), name)

    def globalRes(self, name, bot_id=None, api_key=None):
        return _Res("global", name)

    accountRes = adminRes = globalRes


class _DateAndTime:
    def now(self, tz="UTC"):
        try:
            zone = ZoneInfo(tz) if ZoneInfo else timezone.utc
        except Exception:
            zone = timezone(timedelta(hours=5, minutes=30)) if tz == "Asia/Kolkata" else timezone.utc
        n = datetime.now(zone)
        return Bunch(date=n.strftime("%Y-%m-%d"), time=n.strftime("%H:%M:%S"),
                     timestamp=n.timestamp(), tz=str(tz))


class _Random:
    def randomStr(self, length=8, chars="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
        return "".join(random.choice(chars) for _ in range(int(length)))

    def randomInt(self, start, end):
        return random.randint(int(start), int(end))

    def randomWeightedChoice(self, items, weights):
        return random.choices(list(items), weights=list(weights), k=1)[0]


class _Webhook:
    def getUrlFor(self, command, user_id=None, **kw):
        base = WEBHOOK_BASE_URL or ("http://localhost:%d" % PORT)
        return "%s/webhook/%s/%s" % (base, urllib.parse.quote(str(command), safe=""),
                                      urllib.parse.quote(str(user_id if user_id is not None else u), safe=""))


class _Libs:
    DateAndTime = _DateAndTime()
    Resources = _Resources()
    Random = _Random()
    Webhook = _Webhook()

    def __getattr__(self, name):
        raise AttributeError("libs.%s is not implemented in this Python port" % name)


libs = _Libs()


class _HTTP:
    def get(self, url, **kw):
        kw.setdefault("timeout", 30)
        return requests.get(url, **kw)

    def post(self, url, **kw):
        kw.setdefault("timeout", 30)
        return requests.post(url, **kw)

    def put(self, url, **kw):
        kw.setdefault("timeout", 30)
        return requests.put(url, **kw)

    def delete(self, url, **kw):
        kw.setdefault("timeout", 30)
        return requests.delete(url, **kw)


HTTP = _HTTP()


def _fake_message(uid):
    return Bunch(message_id=None, text=None, caption=None, update_type="message",
                 chat=Bunch(id=int(uid) if str(uid).lstrip("-").isdigit() else uid, type="private"),
                 from_user=Bunch(id=int(uid) if str(uid).isdigit() else uid, first_name="User",
                                 username=None, last_name=None))


def _run_for_user(uid, command, options=None, message_=None, call_=None):
    """Run a command for a user outside of a live update (timers, webhooks, broadcast)."""
    uid = str(uid)
    name, prm = _resolve(command)
    m = message_ or _LAST_MSG.get(uid) or _fake_message(uid)
    try:
        return _invoke(name, u=uid, message=m, call=call_, options=options, params=prm, msg=None,
                       update_type="message")
    except Exception:
        traceback.print_exc()
        return _NOT_FOUND


# ==========================================================================
# 2. COMMANDS   (one python function per TPY command)
# ==========================================================================

# ===/AddBalTxt===
def cmd_AddBalTxt():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bot.replyText(u,f"""<b>Send The Text To Edit</b>""",
    parse_mode = "html")

    Bot.handleNextCommand("/AddBalTxt1")
    pass


# ===/AddBalTxt1===
def cmd_AddBalTxt1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()





    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


    AdmAC=Bot.getData("AdmAC") or []

    r=Bot.getData("ADBT") or 0
    act=f"Add Balance Text updated to {message.text}"
    AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

    Bot.saveData("AdmAC",AdmAC)


     # ==============================
    # CLEAR ADBT
    # ==============================

    if message.text.strip().lower() == "clear":

        Bot.saveData("ADBT", "")

        AdmAC = Bot.getData("AdmAC") or []

        act = "Add Balance Text cleared"

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍 <b>Action:</b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>Cleared Successfully</b>"
        )

        Bot.runCommand("/admin")
        raise ReturnCommand()




    Bot.runCommand("/PIRO_MainMenu",options="<b>Changed Successfully</b>")
    Bot.runCommand("/admin")

    Bot.saveData("ADBT",message.text)
    pass


# ===/AdminStats===
def cmd_AdminStats():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    markup = InlineKeyboardMarkup()

    # Row 1
    markup.row(
        InlineKeyboardButton(
            text='Tᴏᴘ Rᴇғᴇʀʀᴀʟ',
            callback_data='/Top30Referral'
        ),
        InlineKeyboardButton(
            text='Tᴏᴘ Bᴀʟᴀɴᴄᴇ Hᴏʟᴅᴇʀ',
            callback_data='/TopBalance'
        )
    )

    # Row 2
    markup.row(
        InlineKeyboardButton(
            text='Bᴏᴛ Sᴛᴀᴛs',
            callback_data='/RawStats'
        )
    )

    # Row 3
    markup.row(
        InlineKeyboardButton(
            text='Mᴏsᴛ Wɪᴛʜᴅʀᴀᴡ',
            callback_data='/TopWithdraws'
        ),
        InlineKeyboardButton(
            text='Tᴏᴘ Wɪᴛʜᴅʀᴀᴡᴀʟ (Wᴀʟʟᴇᴛ)',
            callback_data='/TopWithdraw_Wallet'
        )
    )

    # Row 4
    markup.row(
        InlineKeyboardButton(
            text='⬅️ Bᴀᴄᴋ Tᴏ Aᴅᴍɪɴ Pᴀɴᴇʟ',
            callback_data='/admin AP'
        )
    )

    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text="""<b>Cʜᴏᴏsᴇ ᴛʜᴇ sᴛᴀᴛɪsᴛɪᴄs ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴠɪᴇᴡ ʙᴇʟᴏᴡ 👇</b>""",
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/AutoCheckBot===
def cmd_AutoCheckBot():
    users = Bot.getData("total_users") or 0

    if int(users) < 50:
        try:
            info = Bot.info()

            # Notify all admins
            admins = Bot.getData("AllBotAdminss") or []

            for admin in admins:
                try:
                    bot.sendMessage(
                        chat_id=admin,
                        text=f"""<b>⚠️ Bot Stopped Automatically.

👥 Total Users: {users}
❌ Required Users: 50
📅 7 Days Completed.</b>""",
                        parse_mode="HTML"
                    )
                except:
                    pass

            # Notify owner using second bot
            token = "8327459100:AAEl6kQgHOxPby5yQO6A0SEdokxCs0G-m4E"

            text = encodeURIComponent(
                f"""🔴 Auto Stop Alert

Bot: @{info.bot_username}
Bot ID: {info.bot_id}
Users: {users}/50"""
            )

            HTTP.get(
                f"https://api.telegram.org/bot{token}/sendMessage"
                f"?chat_id=6925391837&text={text}"
            )

            # Stop current bot
            Account.stop_bot(info.bot_id)

        except Exception as e:
            bot.sendMessage(str(e))
    pass


# ===/BC2_Hook===
def cmd_BC2_Hook():
    job = Bot.getData("BC2_JOB") or {}
    if not job.get("attempts"):
        raise ReturnCommand()
    jid = str(job.get("id"))
    # a webhook can be called twice: never start the same job twice
    if str(Bot.getData("BC2_JOB_DONE") or "") == jid:
        raise ReturnCommand()
    Bot.saveData("BC2_JOB_DONE", jid)

    spd = 8
    try:
        spd = int(job.get("speed") or 8)
    except:
        spd = 8

    out = {"id": jid, "status": "error", "bid": "", "fn": "", "message": "no attempt"}
    for a in job.get("attempts"):
        fn = a[0]
        kw = a[1]
        try:
            res = Bot.broadcast(function=fn, speed=spd, warnings=False, **kw)
        except Exception as e:
            out = {"id": jid, "status": "error", "bid": "", "fn": fn, "message": str(e)[:300]}
            continue
        if res and str(res.get("status")) == "success":
            out = {"id": jid, "status": "success", "bid": str(res.get("broadcast_id")), "fn": fn, "message": ""}
            break
        out = {"id": jid, "status": "error", "bid": "", "fn": fn, "message": str(res)[:300]}
    Bot.saveData("BC2_JOB_RES", out)
    pass


# ===/BC2_act===
def cmd_BC2_act():
    global msg
    AllBotAdminss=Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        try:
            bot.answerCallbackQuery(call.id, "Not admin", show_alert=True)
        except:
            pass
        raise ReturnCommand()

    action = str(params or "").strip().upper()
    mid = message.message_id

    def card_text(data, rows):
        n = 0
        for r in rows:
            n += len(r)
        btxt = "✅ " + str(n) + " Button(s)" if n else "❌ No Buttons"
        users = len(Bot.getData("FulBotUsrs") or [])
        return (
            "<b>⚠️ CONFIRM BROADCAST</b>\n\n"
            "<b>Type:</b> " + str(data.get("label","")) + (("\n📅 <b>Schedule:</b> " + str(data.get("sched").get("date")) + " " + str(data.get("sched").get("time"))) if data.get("sched") else "") + "\n"
            "<b>Buttons:</b> " + btxt + "\n"
            "<b>👥 Total Users (real-time):</b> " + str(users) + "\n\n"
            "Do you want to send this to all <b>" + str(users) + "</b> users?"
        ), n

    data = User.getData("broadcast_data")
    if not data:
        try:
            bot.answerCallbackQuery(call.id, "Session expired. Use /broadcast again.", show_alert=True)
        except:
            pass
        raise ReturnCommand()
    rows = User.getData("bc2_rows") or []

    if action == "ADDBTN":
        try:
            bot.answerCallbackQuery(call.id)
        except:
            pass
        TXT = (
            "<b>➕ ADD BUTTONS</b>\n\n"
            "Send buttons in this format:\n\n"
            "<b>One button</b>\n<code>Name - https://link.com</code>\n\n"
            "<b>Two in one row</b>\n<code>Name - link & Name - link</code>\n\n"
            "<b>Multiple rows</b> (new line per row)\n<code>Name - link\nName - link</code>\n\n"
            "Send <code>skip</code> for no buttons."
        )
        User.saveData("bc2_await_btn", True)
        bot.editMessageText(chat_id=u, message_id=mid, text=TXT, parse_mode="HTML",
            reply_markup={"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "/BC2_act BACK"}]]})
        # cancel_at_command=True: typing any real command (/start ...) cancels this wait and runs that command normally
        Bot.handleNextCommand("/BC2_btn", cancel_at_command=True)
        raise ReturnCommand()

    if action == "BACK":
        try:
            bot.answerCallbackQuery(call.id)
        except:
            pass
        User.deleteData("bc2_await_btn")
        TXT, n = card_text(data, rows)
        kb_rows = []
        for r in rows:
            kb_rows.append(r)
        kb_rows.append([{"text": "✏️ Edit Buttons" if n else "➕ Add Buttons", "callback_data": "/BC2_act ADDBTN"}])
        kb_rows.append([{"text": ("✅ Confirm & Schedule" if data.get("sched") else "✅ Confirm & Send"), "callback_data": "/BC2_act CONFIRM"}, {"text": "❌ Cancel", "callback_data": "/BROADCAST"}])
        bot.editMessageText(chat_id=u, message_id=mid, text=TXT, parse_mode="HTML", reply_markup={"inline_keyboard": kb_rows})
        raise ReturnCommand()

    if action == "CONFIRM":
        if User.getData("broadcast_confirming"):
            try:
                bot.answerCallbackQuery(call.id, "Already sending...")
            except:
                pass
            raise ReturnCommand()
        User.saveData("broadcast_confirming", "1")
        User.deleteData("bc2_await_btn")
        try:
            bot.answerCallbackQuery(call.id, "Starting...")
        except:
            pass

        def prep(fb):
            kw = {}
            for k, v in fb.items():
                if k != "function":
                    kw[k] = v
            f = fb.get("function")
            if f == "send_message":
                kw["parse_mode"] = "HTML"
                kw["disable_web_page_preview"] = True
            elif kw.get("caption"):
                kw["parse_mode"] = "HTML"
            if rows:
                kw["reply_markup"] = {"inline_keyboard": rows}
            return [f, kw]

        attempts = []
        fb = data.get("fb")
        fbp = data.get("fbp")
        if data.get("is_fwd") and not rows:
            attempts.append(["forward_message", {"from_chat_id": data.get("from_chat_id"), "message_id": data.get("message_id")}])
        if fb:
            attempts.append(prep(fb))
        if fbp:
            attempts.append(prep(fbp))
        if not fb:
            attempts.append(["forward_message", {"from_chat_id": data.get("from_chat_id"), "message_id": data.get("message_id")}])

        _sch = data.get("sched")
        if _sch:
            # scheduled broadcast: store the job now, /SCHEDULE_EXECUTE starts it at the chosen time
            def _days(yy, mm, dd):
                tot = 0
                for _y in range(2020, yy):
                    if (_y % 4 == 0 and _y % 100 != 0) or _y % 400 == 0:
                        tot = tot + 366
                    else:
                        tot = tot + 365
                leap = (yy % 4 == 0 and yy % 100 != 0) or yy % 400 == 0
                _ml = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                if leap:
                    _ml[1] = 29
                for _m in range(1, mm):
                    tot = tot + _ml[_m - 1]
                return tot + dd
            _now = libs.DateAndTime.now("Asia/Kolkata")
            _cy, _cm, _cdd = map(int, str(_now["date"]).split("-"))
            _ch, _cmin = map(int, str(_now["time"]).split(":")[:2])
            _td, _tm, _ty = map(int, str(_sch.get("date")).split("-"))
            _secs = (_days(_ty, _tm, _td) - _days(_cy, _cm, _cdd)) * 86400 + (int(_sch.get("h24")) - _ch) * 3600 + (int(_sch.get("min")) - _cmin) * 60
            if _secs < 5:
                _secs = 5
            _tc = (Bot.getData("BC_Task_Counter") or 0) + 1
            Bot.saveData("BC_Task_Counter", _tc)
            _tid = "sch_" + str(u) + "_" + str(_tc)
            Bot.saveData("BC_Task_" + _tid, {"user": u, "attempts": attempts, "speed": 25, "date": _sch.get("date"), "time": _sch.get("time")})
            Bot.runCommandAfter(_secs, "/SCHEDULE_EXECUTE", options=_tid)
            for k in ["broadcast_data","bc2_card_mid","bc2_rows","bc2_await_btn","broadcast_confirming","bc2_wait","sch_when","sch_wait"]:
                User.deleteData(k)
            _users = len(Bot.getData("FulBotUsrs") or [])
            bot.editMessageText(chat_id=u, message_id=mid,
                text="✅ <b>Broadcast Scheduled!</b>\n\n📆 <b>Date:</b> " + str(_sch.get("date")) + "\n⏰ <b>Time:</b> " + str(_sch.get("time")) + "\n📝 <b>Type:</b> " + str(data.get("label","")) + "\n👥 <b>Users:</b> " + str(_users) + " <i>(new users included at send time)</i>\n\n<i>It will start automatically.</i>",
                parse_mode="HTML", reply_markup={"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "/BROADCAST"}]]})
            raise ReturnCommand()

        total = len(Bot.getData("FulBotUsrs") or [])
        try:
            bot.editMessageText(chat_id=u, message_id=mid,
                text="📡 <code>Starting Your Broadcast Session...</code>",
                parse_mode="HTML", reply_markup={"inline_keyboard": []})
        except:
            pass
        User.saveData("bc2_prog_mid", mid)

        jid = str(mid)
        try:
            jid = str(int(time.time() * 1000))
        except:
            jid = str(mid) + str(total)
        Bot.saveData("BC2_JOB", {"id": jid, "attempts": attempts, "speed": 25})
        Bot.saveData("BC2_JOB_RES", {})
        try:
            HTTP.get(libs.Webhook.getUrlFor("/BC2_Hook", 1))
        except:
            pass
        got = {}
        for _i in range(40):
            time.sleep(0.5)
            got = Bot.getData("BC2_JOB_RES") or {}
            if str(got.get("id")) == jid:
                break

        ok = False
        used_fn = ""
        bid = ""
        err = "Start not confirmed. Please try again."
        if str(got.get("id")) == jid:
            if str(got.get("status")) == "success":
                ok = True
                used_fn = str(got.get("fn"))
                bid = str(got.get("bid"))
            else:
                err = str(got.get("message"))
        if not ok:
            User.deleteData("broadcast_confirming")
            msg = "<b>❌ Broadcast Failed:</b> <code>" + err[:500].replace("<", "&lt;").replace(">", "&gt;") + "</code>"
            try:
                bot.editMessageText(chat_id=u, message_id=mid, text=msg, parse_mode="HTML")
            except:
                bot.sendMessage(msg, parse_mode="HTML")
            raise ReturnCommand()

        start = 0
        try:
            start = int(time.time())
        except:
            start = 0
        mode_flag = "F" if used_fn == "forward_message" else "D"
        Bot.saveData("last_broadcast_id", bid)
        Bot.saveData("bc2_last_bid", bid)
        Bot.saveData("bc2_active", True)
        User.saveData("active_broadcast_id", bid)
        for k in ["broadcast_data","bc2_card_mid","bc2_rows","bc2_await_btn","broadcast_confirming","bc2_wait"]:
            User.deleteData(k)
        Bot.runCommand("/BC2_prog", options=str(bid) + "|" + str(mid) + "|0|" + str(start) + "|" + mode_flag)
        raise ReturnCommand()
    pass


# ===/BC2_btn===
def cmd_BC2_btn():
    AllBotAdminss=Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    data = User.getData("broadcast_data")
    rows_saved = User.getData("bc2_rows") or []

    def show_card(rows, mid_edit):
        n = 0
        for r in rows:
            n += len(r)
        btxt = "✅ " + str(n) + " Button(s)" if n else "❌ No Buttons"
        users = len(Bot.getData("FulBotUsrs") or [])
        TXT = (
            "<b>⚠️ CONFIRM BROADCAST</b>\n\n"
            "<b>Type:</b> " + str(data.get("label","")) + (("\n📅 <b>Schedule:</b> " + str(data.get("sched").get("date")) + " " + str(data.get("sched").get("time"))) if data.get("sched") else "") + "\n"
            "<b>Buttons:</b> " + btxt + "\n"
            "<b>👥 Total Users (real-time):</b> " + str(users) + "\n\n"
            "Do you want to send this to all <b>" + str(users) + "</b> users?"
        )
        kb_rows = []
        for r in rows:
            kb_rows.append(r)
        kb_rows.append([{"text": "✏️ Edit Buttons" if n else "➕ Add Buttons", "callback_data": "/BC2_act ADDBTN"}])
        kb_rows.append([{"text": ("✅ Confirm & Schedule" if data.get("sched") else "✅ Confirm & Send"), "callback_data": "/BC2_act CONFIRM"}, {"text": "❌ Cancel", "callback_data": "/BROADCAST"}])
        kb = {"inline_keyboard": kb_rows}
        done = False
        if mid_edit:
            try:
                bot.editMessageText(chat_id=u, message_id=int(mid_edit), text=TXT, parse_mode="HTML", reply_markup=kb)
                done = True
            except:
                done = False
        if not done:
            nm = bot.sendMessage(TXT, parse_mode="HTML", reply_markup=kb)
            try:
                User.saveData("bc2_card_mid", nm.message_id)
            except:
                pass

    # a button tap while waiting for button text (Back button) is also caught here
    cd = None
    try:
        cd = call.data
    except:
        cd = None
    if cd:
        try:
            bot.answerCallbackQuery(call.id)
        except:
            pass
        if data and "BACK" in str(cd).upper():
            User.deleteData("bc2_await_btn")
            show_card(rows_saved, message.message_id)
        raise ReturnCommand()

    txt = str(message.text or "").strip()
    # a command typed -> let it run normally (the wait is already cancelled by cancel_at_command)
    if (not User.getData("bc2_await_btn")) or (not data) or (txt.startswith("/") and txt.lower() != "/cancel"):
        User.deleteData("bc2_await_btn")
        if txt:
            Bot.runCommand(txt)
        raise ReturnCommand()
    if not txt:
        bot.sendMessage("<b>❌ Send text like:</b> <code>Name - https://link.com</code>", parse_mode="HTML")
        Bot.handleNextCommand("/BC2_btn", cancel_at_command=True)
        raise ReturnCommand()
    if txt.lower() == "/cancel":
        for k in ["broadcast_data","bc2_card_mid","bc2_rows","bc2_await_btn"]:
            User.deleteData(k)
        bot.sendMessage("<b>❌ Broadcast cancelled</b>", parse_mode="HTML")
        raise ReturnCommand()

    rows = []
    bad = []
    if txt.lower() in ["skip","no","none","-"]:
        rows = []
    else:
        for line in txt.split("\n"):
            line = line.strip()
            if not line:
                continue
            cur = []
            for part in line.replace("|"," & ").split("&"):
                part = part.strip()
                if not part:
                    continue
                if " - " in part:
                    t, link = part.split(" - ", 1)
                elif "-" in part:
                    t, link = part.split("-", 1)
                else:
                    bad.append(part)
                    continue
                t = t.strip()
                link = link.strip()
                if not t or not link or not (link.startswith("http://") or link.startswith("https://") or link.startswith("tg://")):
                    bad.append(part)
                    continue
                cur.append({"text": t, "url": link})
            if cur:
                rows.append(cur)
        if bad or not rows:
            bot.sendMessage("<b>❌ Invalid format</b>\nUse <code>Name - https://link.com</code>\nLink must start with https:// or tg://\n\nTry again or send <code>skip</code>", parse_mode="HTML")
            Bot.handleNextCommand("/BC2_btn", cancel_at_command=True)
            raise ReturnCommand()

    User.saveData("bc2_rows", rows)
    User.deleteData("bc2_await_btn")
    show_card(rows, User.getData("bc2_card_mid"))
    pass


# ===/BC2_msg===
def cmd_BC2_msg():
    AllBotAdminss=Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()
    cd = None
    try:
        cd = call.data
    except:
        cd = None
    if cd:
        User.deleteData("bc2_wait")
        Bot.runCommand(str(cd))
        raise ReturnCommand()

    is_fwd = False
    try:
        if message.forward_from or message.forward_from_chat or message.forward_date:
            is_fwd = True
    except:
        pass
    try:
        if message.forward_origin:
            is_fwd = True
    except:
        pass
    try:
        if message.forward_sender_name:
            is_fwd = True
    except:
        pass

    _t = ""
    try:
        _t = str(message.text or "").strip()
    except:
        _t = ""

    # admin already left the broadcast flow (Back / /admin): do not swallow this message, run it normally
    if not User.getData("bc2_wait"):
        if _t.startswith("/"):
            _p = _t.split(" ", 1)
            if len(_p) > 1:
                Bot.runCommand(_p[0], options=_p[1])
            else:
                Bot.runCommand(_p[0])
        elif _t:
            Bot.runCommand(_t)
        raise ReturnCommand()

    # /cancel is the only word that leaves the flow. Everything else - /start, /admin, a keyboard
    # button like "Profile" - is taken as the broadcast message itself.
    if _t.lower() in ["/cancel", "cancel"]:
        for k in ["broadcast_data","bc2_card_mid","bc2_rows","bc2_await_btn","bc2_wait"]:
            User.deleteData(k)
        bot.sendMessage("<b>❌ Broadcast cancelled</b>", parse_mode="HTML")
        raise ReturnCommand()

    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    def strip_emoji(s):
        s = str(s)
        for _i in range(200):
            i = s.find("<tg-emoji")
            if i < 0:
                break
            j = s.find(">", i)
            if j < 0:
                break
            s = s[:i] + s[j+1:]
        return s.replace("</tg-emoji>", "")

    raw = message.caption if message.caption else message.text
    ents = message.caption_entities if message.caption else message.entities
    html = None
    if raw:
        if ents:
            html = apply_html_entities(raw, ents, {})
        else:
            html = esc(raw)

    fb = None
    label = "MESSAGE"
    if message.animation:
        fb = {"function":"send_animation","animation":message.animation.file_id}
        label = "ANIMATION"
    elif message.photo:
        fb = {"function":"send_photo","photo":message.photo[-1].file_id}
        label = "PHOTO"
    elif message.video:
        fb = {"function":"send_video","video":message.video.file_id}
        label = "VIDEO"
    elif message.document:
        fb = {"function":"send_document","document":message.document.file_id}
        label = "DOCUMENT"
    elif message.audio:
        fb = {"function":"send_audio","audio":message.audio.file_id}
        label = "AUDIO"
    elif message.voice:
        fb = {"function":"send_voice","voice":message.voice.file_id}
        label = "VOICE"
    elif message.video_note:
        fb = {"function":"send_video_note","video_note":message.video_note.file_id}
        label = "VIDEO NOTE"
    elif message.sticker:
        fb = {"function":"send_sticker","sticker":message.sticker.file_id}
        label = "STICKER"
    elif message.contact:
        fb = {"function":"send_contact","phone_number":message.contact.phone_number,"first_name":message.contact.first_name}
        label = "CONTACT"
    elif message.venue:
        fb = {"function":"send_venue","latitude":message.venue.location.latitude,"longitude":message.venue.location.longitude,"title":message.venue.title,"address":message.venue.address}
        label = "VENUE"
    elif message.location:
        fb = {"function":"send_location","latitude":message.location.latitude,"longitude":message.location.longitude}
        label = "LOCATION"
    elif message.dice:
        fb = {"function":"send_dice","emoji":message.dice.emoji}
        label = "DICE"
    elif message.text:
        fb = {"function":"send_message","text":html}
        label = "TEXT"
    if fb and message.caption and fb["function"] in ["send_photo","send_video","send_document","send_audio","send_voice","send_animation"]:
        fb["caption"] = html

    fbp = None
    if fb and html and "<tg-emoji" in str(html):
        fbp = dict(fb)
        if fb.get("text"):
            fbp["text"] = strip_emoji(fb.get("text"))
        if fb.get("caption"):
            fbp["caption"] = strip_emoji(fb.get("caption"))

    if is_fwd:
        label = label + " (FORWARDED)"

    data = {"function": "forward_message", "from_chat_id": message.chat.id, "message_id": message.message_id, "label": label, "is_fwd": is_fwd}
    if fb:
        data["fb"] = fb
    if fbp:
        data["fbp"] = fbp

    _sw = User.getData("sch_when")
    _type_sfx = ""
    if _sw:
        data["sched"] = _sw
        _type_sfx = "\n📅 <b>Schedule:</b> " + str(_sw.get("date")) + " " + str(_sw.get("time"))
    User.saveData("broadcast_data", data)
    User.deleteData("bc2_rows")
    User.deleteData("bc2_await_btn")
    User.deleteData("bc2_wait")
    users = len(Bot.getData("FulBotUsrs") or [])
    TXT = (
        "<b>⚠️ CONFIRM BROADCAST</b>\n\n"
        "<b>Type:</b> " + label + _type_sfx + "\n"
        "<b>Buttons:</b> ❌ No Buttons\n"
        "<b>👥 Total Users (real-time):</b> " + str(users) + "\n\n"
        "Do you want to send this to all <b>" + str(users) + "</b> users?"
    )
    kb = {"inline_keyboard": [
        [{"text": "➕ Add Buttons", "callback_data": "/BC2_act ADDBTN"}],
        [{"text": ("✅ Confirm & Schedule" if _sw else "✅ Confirm & Send"), "callback_data": "/BC2_act CONFIRM"}, {"text": "❌ Cancel", "callback_data": "/BROADCAST"}]
    ]}
    nm = bot.sendMessage(TXT, parse_mode="HTML", reply_markup=kb)
    try:
        User.saveData("bc2_card_mid", nm.message_id)
    except:
        try:
            User.saveData("bc2_card_mid", nm["message_id"])
        except:
            pass
    pass


# ===/BC2_prog===
def cmd_BC2_prog():
    try:
        parts = str(options).split("|")
        bid = parts[0]
        mid = int(parts[1])
        n0 = int(parts[2])
    except:
        raise ReturnCommand()
    start = 0
    try:
        start = int(parts[3])
    except:
        start = 0
    mflag = "D"
    try:
        mflag = parts[4]
    except:
        mflag = "D"

    cid = u
    try:
        cid = parts[5]
    except:
        cid = u

    def make_bar(pct):
        SEG = 12
        filled = int(pct * SEG / 100)
        if filled > SEG:
            filled = SEG
        return "▰" * filled + "▱" * (SEG - filled)

    def pct_txt(x):
        r = round(x, 1)
        if r == int(r):
            return str(int(r))
        return str(r)

    LOOP = 40
    for i in range(LOOP):
        tick = n0 + i
        total = 0
        succ = 0
        fail = 0
        status = "running"
        try:
            st = Bot.getBroadcastStatus(bid)
            total = st.get("total_users", 0) or st.get("total", 0) or 0
            succ = st.get("total_success", 0) or 0
            fail = st.get("total_errors", 0) or 0
            status = str(st.get("status", "running")).lower()
        except:
            pass
        done = succ + fail
        finished = status in ["completed", "done", "finished", "stopped", "failed", "error"]
        if total > 0 and done >= total:
            finished = True
        if finished and total < done:
            total = done

        pct = 0.0
        if total:
            pct = done * 100.0 / total
        if finished:
            pct = 100.0
        if pct > 100.0:
            pct = 100.0

        elapsed = 1
        try:
            if start:
                elapsed = int(time.time()) - start
            else:
                elapsed = tick
        except:
            elapsed = tick
        if elapsed < 1:
            elapsed = 1
        speed = done * 1.0 / elapsed
        rate = 0.0
        if done > 0:
            rate = succ * 100.0 / done
        dur = str(int(elapsed / 60)) + "m " + str(elapsed % 60) + "s"

        if finished:
            if succ == 0 and fail > 0:
                head = "⚠️ <b>Broadcast Failed!</b>"
            else:
                head = "🎙 <b>Broadcast Completed!</b>"
        else:
            dots = [".", "..", "...", ""]
            head = "🎙 <b>Broadcasting" + dots[tick % 4] + "</b>"

        txt = (
            head + "\n"
            + make_bar(pct) + " " + pct_txt(pct) + "%\n\n"
            + "👥 <b>Total Bot Users:</b> " + str(total) + "\n"
            + "✅ <b>Successful:</b> " + str(succ) + "\n"
            + "❌ <b>Failed:</b> " + str(fail) + "\n"
            + "📊 <b>Success Rate:</b> " + pct_txt(rate) + "%\n"
            + "⚡ <b>Speed:</b> " + str(round(speed, 1)) + " msg/s\n"
            + "⏱ <b>Duration:</b> " + dur
        )
        try:
            bot.editMessageText(chat_id=cid, message_id=mid, text=txt, parse_mode="HTML")
        except:
            pass
        if finished:
            try:
                User.deleteData("bc2_prog_mid")
            except:
                pass
            try:
                Bot.deleteData("bc2_active")
            except:
                pass
            try:
                Bot.deleteData("bc2_last_bid")
            except:
                pass
            raise ReturnCommand()
        time.sleep(1)

    if n0 < 3000:
        Bot.runCommandAfter(1, "/BC2_prog", options=str(bid) + "|" + str(mid) + "|" + str(n0 + LOOP) + "|" + str(start) + "|" + mflag + "|" + str(cid))
    pass


# ===/BROADCAST===
def cmd_BROADCAST():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    # leaving the broadcast setup (Back button) -> its waiting state is cleared
    for k in ["bc2_wait", "bc2_await_btn", "broadcast_data", "bc2_rows", "bc2_card_mid", "broadcast_confirming", "sch_wait", "sch_when"]:
        User.deleteData(k)

    FulBotUsrs = Bot.getData("FulBotUsrs") or []
    user_count = len(FulBotUsrs)

    text = (
        "<b>📢 Broadcast Center</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>Total Users:</b> <code>" + str(user_count) + "</code>\n\n"
        "⚡ <b>Instant</b> — send your message to everyone right now\n"
        "📅 <b>Schedule</b> — send it later at a time you choose\n\n"
        "<i>Choose your broadcast type below</i>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⚡ Instant Broadcast", callback_data="/broadcast"))
    markup.add(InlineKeyboardButton("📅 Schedule Broadcast", callback_data="/SCHEDULE_BROADCAST"))
    markup.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="/admin AP"))

    # EDIT existing message if possible, else send new
    try:
        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except:
        bot.sendMessage(text, reply_markup=markup, parse_mode="HTML")
    pass


# ===/BanUnban===
def cmd_BanUnban():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫You Are Not This Bot Admin</i></b>"
        )
        raise ReturnCommand()


    #Unban User

    if str(params) != "None":

        userid = str(params)

        Bot.saveData(
            f"{userid}ban",
            False
        )

        bot.answerCallbackQuery(
            call.id,
            "✅ User Unbanned Successfully"
        )


    #Get Banned Users

    Full = Bot.getData("FulBotUsrs") or []

    BannedUsers = []

    for userid in Full:

        banned = Bot.getData(
            f"{userid}ban"
        ) or False

        if banned == True:
            BannedUsers.append(userid)


    #Keyboard

    markup = InlineKeyboardMarkup()

    for userid in BannedUsers:

        markup.add(
            InlineKeyboardButton(
                text=str(userid),
                callback_data="/BanUnban " + str(userid)
            ),
            InlineKeyboardButton(
                text="❌",
                callback_data="/BanUnban " + str(userid)
            )
        )


    # Add Ban
    markup.add(
        InlineKeyboardButton(
            text="➕ Aᴅᴅ Bᴀɴ Usᴇʀ",
            callback_data="/PIRO_ban"
        )
    )

    # Back
    markup.add(
        InlineKeyboardButton(
            text="🔙 Bᴀᴄᴋ",
            callback_data="/admin AP"
        )
    )


    #Message

    T = "<b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Yᴏᴜʀ Bᴏᴛ Bᴀɴ/Uɴʙᴀɴ Usᴇʀs</b>"

    try:
        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=T,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except:
        pass
    pass


# ===/Bonus===
def cmd_Bonus():
    # Coded by: @Jenish_Dobariya1

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    # Pull the native formatted calendar data object safely
    time_data = libs.DateAndTime.now("Asia/Kolkata")
    today_date = str(time_data["date"])

    # Parse the human-readable time string "HH:MM:SS" into numerical elements
    current_time_str = str(time_data["time"])
    time_parts = current_time_str.split(":")

    curr_hour = int(time_parts[0])
    curr_min = int(time_parts[1])
    curr_sec = int(time_parts[2])

    # Convert current hours/minutes/seconds into a total count of elapsed seconds for today
    current_seconds_today = (curr_hour * 3600) + (curr_min * 60) + curr_sec

    # Fetch user tracking history states from the database
    last_claim_date = User.getData("last_bonus_date_str")
    last_claim_seconds = User.getData("last_bonus_seconds_int") or 0

    bonamount = Bot.getData("DailyBonus") or 0
    bontype = Bot.getData("DailyBonusType") or "normal"

    def conv(seconds_left):
        if seconds_left <= 0:
            return "0 Hours 00 Minutes 00 Seconds"
        hour = seconds_left // 3600
        seconds_left %= 3600
        mins = seconds_left // 60
        sec = seconds_left % 60
        return f"{hour} Hours {mins:02d} Minutes {sec:02d} Seconds"

    # INITIALIZATION & RESET: Clear older states if a completely new calendar day starts
    is_eligible = False

    if not last_claim_date or last_claim_date != today_date:
        # If they are claiming on a brand new day, we check if 24 hours (86400 seconds) have passed
        if not last_claim_date:
            is_eligible = True
        else:
            # Total seconds passed since yesterday's claim = (seconds left in yesterday) + (seconds elapsed today)
            seconds_passed = (86400 - last_claim_seconds) + current_seconds_today
            if seconds_passed >= 86400:
                is_eligible = True

    # If they are trying to claim again on the EXACT SAME calendar day, they are locked out
    if last_claim_date == today_date:
        is_eligible = False

    # ---- FIXED: accurate 24h cooldown even after many days ----
    def days_from_civil(y, m, d):
        if m <= 2:
            y -= 1
        era = (y if y >= 0 else y - 399) // 400
        yoe = y - era * 400
        mp = (m + 9) % 12
        doy = (153 * mp + 2) // 5 + d - 1
        doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
        return era * 146097 + doe - 719468

    fixed_eligible = True
    time_remaining_fix = 0
    if last_claim_date:
        try:
            ly, lm, ld = [int(x) for x in str(last_claim_date).split("-")]
            ty, tm, td = [int(x) for x in today_date.split("-")]
            day_diff = days_from_civil(ty, tm, td) - days_from_civil(ly, lm, ld)
            elapsed = day_diff * 86400 + (current_seconds_today - int(last_claim_seconds))
            if elapsed < 86400:
                fixed_eligible = False
                time_remaining_fix = 86400 - elapsed
        except:
            pass
    is_eligible = fixed_eligible

    # EXECUTE IF LOCKED OUT
    if not is_eligible:
        # Calculate exact countdown time remaining
        if last_claim_date == today_date:
            time_remaining = 86400 - (current_seconds_today - last_claim_seconds)
        else:
            seconds_passed = (86400 - last_claim_seconds) + current_seconds_today
            time_remaining = 86400 - seconds_passed

        if time_remaining < 0: 
            time_remaining = 0

        bot.replyText(
            u,
            f"<b>🤕 Already Claimed Bonus Amount\n\n🎁 Please Wait:\n⏳ <code>{conv(time_remaining_fix)}</code></b>",
            parse_mode="html"
        )
        raise ReturnCommand()

    # EXECUTE IF ELIGIBLE (PROCEED TO REWARD SYSTEM)
    else:
        # Safely write variables back into user parameters before dispatching funds
        User.saveData("last_bonus_date_str", today_date)
        User.saveData("last_bonus_seconds_int", current_seconds_today)

        # ===== NORMAL FIXED BONUS =====
        if bontype == "normal":
            reward = float(bonamount)
            libs.Resources.anotherRes('Balance', user=u).add(reward)

            bot.replyText(
                u,
                f"<b>🎁 Bonus {reward} Rs. Claimed Successfully</b>",
                parse_mode="html"
            )

        # ===== REAL DICE RANDOM BONUS =====
        elif bontype == "dice":
            dice_msg = bot.sendDice(chat_id=u, emoji="🎲")
            dice_value = dice_msg.dice.value  # 1 to 6

            rewards = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
            reward = rewards.get(dice_value, 1)

            libs.Resources.anotherRes('Balance', user=u).add(reward)

            bot.replyText(
                u,
                f"<b>🎁 Bonus {reward} Rs. Claimed Successfully</b>",
                parse_mode="html"
            )

        # ===== CLAIM AUDIT TRACKING =====
        bonClm = User.getData("bonClm") or "n"
        if bonClm == "n":
            T_usClimBon = Bot.getData('T_usClimBon') or 0
            Bot.saveData('T_usClimBon', T_usClimBon + 1)
            User.saveData("bonClm", "y")

        # ===== REFERRAL MATRIX SYSTEMS =====
        refBy = Bot.getData(str(u) + "Referral") or "NONE"
        if refBy != "NONE":
            RefBonClimC = Bot.getData(str(refBy) + "RefBonClimC") or 0
            Bot.saveData(str(refBy) + "RefBonClimC", RefBonClimC + 1)

    pass


# ===/BotOffText===
def cmd_BotOffText():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    keyboard = ReplyKeyboardMarkup(True)
    keyboard.row("⛔ Cancel")

    current = Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off"

    bot.replyText(
        u,
        "<b>💬 Send The Bot Off Text</b>\n\n"
        "📌 <b>Current Text:</b>\n" + str(current) + "\n\n"
        "💡 <i>Tip: Send <code>Clear</code> to reset to default message.</i>",
        parse_mode="html",
        reply_markup=keyboard
    )

    Bot.handleNextCommand("/BotOffText1")
    pass


# ===/BotOffText1===
def cmd_BotOffText1():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if message.text == "⛔ Cancel":
        Bot.runCommand("/PIRO_MainMenu", options="<b>⛔ Cancelled Succesfully</b>")
        Bot.runCommand("/admin")
        raise ReturnCommand()

    input_text = str(message.text).strip()

    if input_text.lower() == "clear":
        Bot.saveData("BOT_OFF_TEXT", None)
        Bot.runCommand("/PIRO_MainMenu", options="<b>🔄 Reset to Default Successfully</b>")
    else:
        Bot.saveData("BOT_OFF_TEXT", message.text)
        Bot.runCommand("/PIRO_MainMenu", options="<b>Changed Successfully</b>")

    Bot.runCommand("/admin")
    pass


# ===/CHANNEL_BC===
def cmd_CHANNEL_BC():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    # Get channel count
    AMCTGID = Bot.getData("AllMainChTGID") or []
    channel_count = len(AMCTGID)

    # Build attractive message
    text = f"""<b>📢 Channel Broadcast Center</b>

📊 <b>Total Channels:</b> <code>{channel_count}</code>
<i>Choose your broadcast type below</i>

━━━━━━━━━━━━━━━━━━"""

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📢 Instant Broadcast", callback_data="/PIRO_Broadcast"))
    markup.add(InlineKeyboardButton("📅 Schedule Broadcast", callback_data="/PIRO_ScheduleBroadcast")
    )
    markup.add(InlineKeyboardButton("🔙 Back to Channels", callback_data="/PIRO_Chanel_Pannel"))

    # ✅ Edit existing message if possible
    try:
        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except:
        bot.sendMessage(text, reply_markup=markup, parse_mode="HTML")
    pass


# ===/ChMemberTrack===
def cmd_ChMemberTrack():
    # Exact join tracking: Telegram tells the bot which invite link a user used to join a channel.
    # A join through the invite link this bot shows = "joined via bot". People who were already
    # members never produce a join event, so they are never counted.
    # (called from /handler_special_updates for update_type == "chat_member")

    try:
        cid = str(message.chat.id)
        new_m = message.new_chat_member
        old_m = message.old_chat_member
        nst = str(new_m.status)
        ost = str(old_m.status)
        uid = str(new_m.user.id)
    except:
        raise ReturnCommand()

    # only a real join: was outside (left / kicked) and is a member now
    if nst not in ["member", "administrator", "creator"]:
        raise ReturnCommand()
    if ost not in ["left", "kicked"]:
        raise ReturnCommand()

    # this user already asked to join through the bot link and was counted in "exact" at that moment.
    # Now he got approved: just move him out of "waiting", never count him twice.
    _rk = "jrq_" + cid + "_" + uid
    if Bot.getData(_rk) == "1":
        Bot.saveData(_rk, "ok")
        try:
            libs.Resources.globalRes("ChReqPend" + cid.replace("-", "m")).cut(1)
        except:
            pass
        raise ReturnCommand()

    link = ""
    try:
        link = str(message.invite_link.invite_link)
    except:
        link = ""

    if link == "" or link == "None":
        # joined without an invite link (public channel link / search): Telegram gives no link info,
        # so count it as "approx" only when the bot had shown this user the Join button for the channel
        if Bot.getData("shnj_" + cid + "_" + uid) and not Bot.getData("jvp_" + cid + "_" + uid):
            Bot.saveData("jvp_" + cid + "_" + uid, "1")
            libs.Resources.globalRes("ChViaP" + cid.replace("-", "m")).add(1)
        raise ReturnCommand()

    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    k = -1
    for i in range(len(AMCTGID)):
        if str(AMCTGID[i]) == cid:
            k = i
            break

    if k < 0 or k >= len(AMCL):
        raise ReturnCommand()


    def norm(t):
        t = str(t).strip().replace("https://", "").replace("http://", "")
        if t.endswith("/"):
            t = t[:-1]
        return t


    a = norm(link)
    b = norm(AMCL[k])

    same = False
    if a.endswith("…") or a.endswith("..."):
        prefix = a.replace("…", "").replace("...", "")
        same = (len(prefix) > 8 and b.startswith(prefix))
    else:
        same = (a == b)

    if not same:
        raise ReturnCommand()

    flag = "jvb_" + cid + "_" + uid
    if Bot.getData(flag):
        raise ReturnCommand()

    Bot.saveData(flag, "1")
    libs.Resources.globalRes("ChVia" + cid.replace("-", "m")).add(1)
    pass


# ===/ChRules===
def cmd_ChRules():
    # Rules, notes and exceptions for the join stats (opened from the stats screen).

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()

    CHUNK = 60


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    state = Bot.getData("ChStat2") or {}
    cursor = int(state.get("cursor") or 0)
    total_users = len(Bot.getData("FulBotUsrs") or [])

    txt = """<b>ℹ️ Rules & Notes</b>

<b>What is counted</b>
Only people who joined a channel <b>through this bot</b>. People who were already in the channel are never counted.

<b>Exact</b>
The channel uses an invite link (t.me/+...). Telegram tells the bot which link the user used, so the count is exact.

<b>Join requests</b>
If the channel needs approval, a user who asked to join through the bot's invite link is counted in Exact right away and shown as "Waiting for approval". He is not counted twice when you approve him. A dismissed request stays in the waiting number.

<b>Approx</b>
The channel link is public (t.me/username). Telegram gives no link info, so the bot counts users it had shown the Join button to and who then joined.

<b>Join screen shown</b>
How many times the Join button was shown on /start. It counts screens, not people.

<b>Not counted (exceptions)</b>
• Joins before this feature was added
• Joins through some other link or from search
• Joins while the bot is not admin of the channel
• A user is counted once per channel, even if he leaves later

<b>Must have</b>
The bot must be admin of the channel.

<b>Check (optional)</b>
Re-checks the users the bot found outside a channel and shows how many are members now. It runs 60 users per tap."""

    mk = InlineKeyboardMarkup()

    if total_users > 0:
        if cursor >= total_users:
            mk.add(InlineKeyboardButton(text="Run check again", callback_data="/ChStats run"))
        elif cursor == 0:
            mk.add(InlineKeyboardButton(text="Run check (optional)", callback_data="/ChStats run"))
        else:
            mk.add(InlineKeyboardButton(text="Check next " + str(CHUNK) + " users (" + str(cursor) + "/" + str(total_users) + ")", callback_data="/ChStats run"))
            mk.add(InlineKeyboardButton(text="Restart check", callback_data="/ChStats reset"))

    mk.add(InlineKeyboardButton(text="🔙 Back to stats", callback_data="/ChStats"))

    show(message.message_id, txt, mk)
    raise ReturnCommand()
    pass


# ===/ChStats===
def cmd_ChStats():
    # Channel join stats - DATA ONLY (rules / notes are on the "Rules & Notes" button).
    # params: run (optional check scan, next users) | reset | pg <n>

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()

    CHUNK = 60
    PAGE = 6


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    def toast(t):
        try:
            bot.answerCallbackQuery(call.id, t)
        except:
            pass


    def res_val(name):
        try:
            return int(libs.Resources.globalRes(name).value() or 0)
        except:
            return 0


    def time12(t):
        # "16:21:05" -> "04:21:05 PM"
        try:
            h = int(str(t)[0:2])
            m = str(t)[3:5]
            ap = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            return ("0" + str(h12) if h12 < 10 else str(h12)) + ":" + m + ((":" + str(t)[6:8]) if len(str(t)) >= 8 else "") + " " + ap
        except:
            return str(t)[:8]


    mid = message.message_id

    _src = params if params is not None else options
    P = []
    if _src is not None and str(_src).strip() != "None":
        P = str(_src).strip().split()
    action = P[0] if P else ""
    arg = P[1] if len(P) > 1 else ""

    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    Full = Bot.getData("FulBotUsrs") or []
    total_users = len(Full)

    state = Bot.getData("ChStat2") or {}
    counts = state.get("counts") or {}
    pend = state.get("pend") or {}
    errs = state.get("errs") or {}
    cursor = int(state.get("cursor") or 0)

    # ---- optional check scan (started from the Rules & Notes screen) ----
    if action == "reset":
        counts = {}
        pend = {}
        errs = {}
        cursor = 0
        Bot.saveData("ChStat2", {"counts": counts, "pend": pend, "errs": errs, "cursor": cursor, "time": ""})
        toast("Check restarted")

    if action == "run":
        if cursor >= total_users and total_users > 0:
            counts = {}
            pend = {}
            errs = {}
            cursor = 0

        chunk = Full[cursor:cursor + CHUNK]

        for uid in chunk:
            for k in range(len(AMCTGID)):
                cid = str(AMCTGID[k])
                if not Bot.getData("shnj_" + cid + "_" + str(uid)):
                    continue
                try:
                    st = str(bot.getChatMember(cid, uid).status)
                    if st in ["member", "administrator", "creator"]:
                        counts[cid] = int(counts.get(cid, 0)) + 1
                    else:
                        pend[cid] = int(pend.get(cid, 0)) + 1
                except:
                    errs[cid] = int(errs.get(cid, 0)) + 1

        cursor = cursor + len(chunk)
        Bot.saveData("ChStat2", {"counts": counts, "pend": pend, "errs": errs, "cursor": cursor, "time": ""})
        toast("Checked " + str(len(chunk)) + " users")

    # ---- page ----
    page = int(User.getData("chs_page") or 0)
    if action == "pg" and arg.lstrip("-").isdigit():
        page = int(arg)

    pages = (len(AMCTGID) + PAGE - 1) // PAGE
    if pages < 1:
        pages = 1
    if page < 0:
        page = 0
    if page > pages - 1:
        page = pages - 1
    User.saveData("chs_page", page)

    now = libs.DateAndTime.now("Asia/Kolkata")
    mem_cache = Bot.getData("ChMemCache") or {}
    mem_dirty = False

    # ---------------- DATA ONLY screen ----------------
    if not AMCTGID:
        txt = "<b>📊 How many users joined?</b>\n\nNo channels added yet."
    else:
        total_exact = 0
        total_approx = 0
        total_wait = 0
        vals = {}
        for kk in range(len(AMCTGID)):
            ckk = str(AMCTGID[kk]).replace("-", "m")
            v_ex = res_val("ChVia" + ckk)
            v_ap = res_val("ChViaP" + ckk)
            v_pq = res_val("ChReqPend" + ckk)
            vals[ckk] = [v_ex, v_ap, v_pq]
            total_exact += v_ex
            total_approx += v_ap
            total_wait += v_pq

        txt = "<b>📊 How many users joined?</b>\n\n"
        txt += "<b>Total joined via bot: " + str(total_exact + total_approx) + "</b>\n"
        txt += "Exact: " + str(total_exact) + "  |  Approx: " + str(total_approx) + "\n"
        if total_wait > 0:
            txt += "Waiting for approval (in exact): " + str(total_wait) + "\n"
        txt += "Bot users: " + str(total_users) + "\n\n"

        start = page * PAGE
        for k in range(start, min(start + PAGE, len(AMCTGID))):
            cid = str(AMCTGID[k])
            ck = cid.replace("-", "m")
            nm = str(AMCU[k]) if k < len(AMCU) and str(AMCU[k]) else (str(AMC[k]) if k < len(AMC) else cid)
            if len(nm) > 40:
                nm = nm[:40]

            # member counts are cached (fast); fetched again only when the Refresh button is pressed
            members = str(mem_cache.get(cid, ""))
            if members == "" or action == "refresh":
                try:
                    members = str(bot.getChatMemberCount(cid))
                    mem_cache[cid] = members
                    mem_dirty = True
                except:
                    if members == "":
                        members = "?"

            ex = vals.get(ck, [0, 0, 0])[0]
            ap = vals.get(ck, [0, 0, 0])[1]
            pq = vals.get(ck, [0, 0, 0])[2]

            txt += "<b>" + str(k + 1) + ". " + esc(nm) + "</b>\n"
            txt += "Channel members: " + members + "\n"
            txt += "Joined via bot: <b>" + str(ex + ap) + "</b>  (exact " + str(ex) + ", approx " + str(ap) + ")\n"
            if pq > 0:
                txt += "Waiting for approval: " + str(pq) + " (counted in exact)\n"
            txt += "Join screen shown: " + str(res_val("ChShowS" + ck)) + " times\n"
            if cursor > 0:
                txt += "Check result: " + str(int(counts.get(cid, 0))) + " joined, " + str(int(pend.get(cid, 0))) + " not yet\n"
            txt += "\n"

        if mem_dirty:
            Bot.saveData("ChMemCache", mem_cache)

        if pages > 1:
            txt += "Page " + str(page + 1) + " / " + str(pages) + "\n"
        txt += "Updated: " + esc(str(now["date"])) + "  " + time12(now["time"])

        if len(txt) > 3900:
            cut = txt.rfind("\n\n", 0, 3800)
            if cut > 0:
                txt = txt[:cut] + "\n\n..."
            else:
                txt = txt[:3800]

    mk = InlineKeyboardMarkup()

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data="/ChStats pg " + str(page - 1)))
        nav.append(InlineKeyboardButton(text=str(page + 1) + "/" + str(pages), callback_data="/ChStats pg " + str(page)))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data="/ChStats pg " + str(page + 1)))
        mk.row(*nav)

    mk.add(InlineKeyboardButton(text="🔄 Refresh", callback_data="/ChStats refresh"))
    mk.add(InlineKeyboardButton(text="ℹ️ Rules & Notes", callback_data="/ChRules"))
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(mid, txt, mk)
    raise ReturnCommand()
    pass


# ===/ChangeAnyUserBal===
def cmd_ChangeAnyUserBal():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("balance", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # ==============================
    # 💰 CHANGE USER BALANCE
    # ==============================

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
            break

    if not is_Admin:
        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text="<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()



    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text="""<b>💡 Sᴇɴᴅ Usᴇʀ Tᴇʟᴇɢʀᴀᴍ ID & Aᴍᴏᴜɴᴛ

⚠️ Fᴏʀᴍᴀᴛ:
<code>7528813331 10</code>

📤 Bᴜʟᴋ:
<code>7528813331 10
6925391837 10</code>

➕ Pᴏsɪᴛɪᴠᴇ Aᴍᴏᴜɴᴛ = Iɴᴄʀᴇᴀsᴇ
➖ Nᴇɢᴀᴛɪᴠᴇ Aᴍᴏᴜɴᴛ = Dᴇᴄʀᴇᴀsᴇ

📝 Rᴇᴍᴀʀᴋ:
<code>7528813331 10 Referral Bonus</code>

📤 Bᴜʟᴋ + Rᴇᴍᴀʀᴋ:
<code>7528813331 10 Referral Bonus
6925391837 5 Payment Bonus</code>""",
        parse_mode="HTML",
        reply_markup=None
    )

    Bot.handleNextCommand("/ChangeAnyUserBal2")
    pass


# ===/ChangeAnyUserBal2===
def cmd_ChangeAnyUserBal2():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("balance", True) is False:
        raise ReturnCommand()
    # Stop Change Balance Process
    if params == "AP" or options == "AP":

        try:
            bot.answerCallbackQuery(call.id)
        except:
            pass

        Bot.runCommand("/admin")
        raise ReturnCommand()


    try:
        # Check Admin Authorization
        AllBotAdminss = Bot.getData("AllBotAdminss") or []

        is_Admin = False
        for userid in AllBotAdminss:
            if str(u) == str(userid):
                is_Admin = True
                break

        if not is_Admin:
            bot.sendMessage(
                u,
                "<b><i>🚫 You Are Not This Bot Admin</i></b>",
                parse_mode="html"
            )
            raise ReturnCommand()


        admin_panel_keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "⬅️ Bᴀᴄᴋ Tᴏ Aᴅᴍɪɴ Pᴀɴᴇʟ",
                        "callback_data": "/admin"
                    }
                ]
            ]
        }


        # Handle Empty Input
        if not message.text:
            bot.sendMessage(
                text="⚠️ <b>Empty input!</b>\n\nPlease enter details or tap Back.",
                parse_mode="html",
                reply_markup=admin_panel_keyboard
            )
            Bot.handleNextCommand("/ChangeAnyUserBal2")
            raise ReturnCommand()


        lines = message.text.strip().split("\n")

        updated_list = []
        success_count = 0


        # Admin Action Data
        AdmAC = Bot.getData("AdmAC") or []
        ADBT = Bot.getData("ADBT") or "0"


        # Time
        now = libs.DateAndTime.now("Asia/Kolkata")

        date = now["date"]
        time = now["time"][:5]

        year, month, day = date.split("-")
        hour, minute = time.split(":")

        hour = int(hour)

        ampm = "am"

        if hour >= 12:
            ampm = "pm"

        if hour > 12:
            hour -= 12

        if hour == 0:
            hour = 12


        MONTHS = {
            "01": "Jan",
            "02": "Feb",
            "03": "Mar",
            "04": "Apr",
            "05": "May",
            "06": "Jun",
            "07": "Jul",
            "08": "Aug",
            "09": "Sep",
            "10": "Oct",
            "11": "Nov",
            "12": "Dec"
        }


        EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


        # ==============================
        # PROCESS LINES
        # ==============================

        for line in lines:

            line = line.strip()

            if not line:
                continue


            parts = line.split(" ")

            if len(parts) < 2:
                continue


            raw_ids = parts[0].strip()


            # Amount
            try:

                amount = float(parts[1].strip())

                if amount == 0:
                    continue

            except Exception:
                continue


            # Remark
            remark = " ".join(parts[2:]).strip() if len(parts) > 2 else ""


            # Multiple IDs
            id_list = [
                target_id.strip()
                for target_id in raw_ids.split(",")
                if target_id.strip()
            ]


            # ==============================
            # PROCESS USERS
            # ==============================

            for target_id in id_list:

                # skip anything that is not a real numeric user id
                if not str(target_id).lstrip("-").isdigit():
                    continue

                target_res = libs.Resources.anotherRes(
                    "Balance",
                    user=target_id
                )


                # Positive = Add
                # Negative = Deduct
                if amount > 0:

                    target_res.add(amount)

                    action_symbol = "+"
                    action_text = "increased"

                else:

                    # never push a user's balance below zero
                    cur_bal = float(target_res.value() or 0)
                    target_res.cut(min(abs(amount), cur_bal))

                    action_symbol = "-"
                    action_text = "decreased"


                new_bal = target_res.value()


                formatted_amt = (
                    int(abs(amount))
                    if amount.is_integer()
                    else abs(amount)
                )


                formatted_bal = (
                    int(new_bal)
                    if isinstance(new_bal, float) and new_bal.is_integer()
                    else new_bal
                )


                updated_list.append(
                    f"👤 <code>{target_id}</code> : "
                    f"<b>{action_symbol}₹{formatted_amt}</b> → "
                    f"<b>₹{formatted_bal}</b>"
                )


                success_count += 1


                # ==============================
                # SAVE ADMIN ACTION
                # ==============================

                act = (
                    f"Admin {action_text} balance of {target_id} "
                    f"by ₹{formatted_amt}"
                )


                if remark:
                    act += f" | Remark: {remark}"


                AdmAC.append(
                    f"<b>📆 Time:</b> {EasyTime}\n"
                    f"👥 <b>By:</b> {message.from_user.first_name} "
                    f"[ID: <code>{u}</code>]\n"
                    f"🔍 <b>Action:</b> {act}"
                )


                # ==============================
                # NOTIFY RECIPIENT
                # ==============================

                try:

                    # Separate Admin Balance Text
                    if ADBT != "0":

                        bot.sendMessage(
                            chat_id=target_id,
                            text=f"<b>{ADBT}</b>",
                            parse_mode="html"
                        )


                    # Balance Change Message
                    rec_msg = (
                        f"<b>💰 Admin {action_text} your balance "
                        f"by ₹{abs(amount):.2f}</b>"
                    )


                    if remark:

                        rec_msg += (
                            f"\n\n<b>📝 Remark:</b> {remark}"
                        )


                    bot.sendMessage(
                        chat_id=target_id,
                        text=rec_msg,
                        parse_mode="html"
                    )


                except Exception:
                    pass


        # ==============================
        # SAVE ADMIN ACTIONS
        # ==============================

        Bot.saveData("AdmAC", AdmAC)


        # ==============================
        # NO SUCCESS
        # ==============================

        if success_count == 0:

            bot.sendMessage(
                text="⚠️ <b>Nᴏ Vᴀʟɪᴅ Iɴᴘᴜᴛ Pʀᴏᴄᴄᴇᴅ.</b>\n\nPʟᴇᴀsᴇ Tʀʏ Aɢᴀɪɴ.",
                parse_mode="html",
                reply_markup=admin_panel_keyboard
            )

            raise ReturnCommand()


        # ==============================
        # RESULT
        # ==============================

        response_text = (
            f"<b>✅ Balance Successfully Processed "
            f"For {success_count} Users</b>\n\n"
        )


        response_text += (
            "<b>📊 Updated Balances:</b>\n"
        )


        response_text += "\n".join(updated_list)


        # ==============================
        # FINAL MESSAGE
        # ==============================

        bot.sendMessage(
            text=response_text,
            parse_mode="html",
            reply_markup=admin_panel_keyboard
        )


    except Exception as e:
        pass
    pass


# ===/CreateBotRC===
def cmd_CreateBotRC():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

    if not is_Admin:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GET DETAILS
    # ==============================

    data = message.text.strip()

    try:

        parts = data.split("--")

        if len(parts) != 2:
            raise Exception()

        first = parts[0].split("-")

        if len(first) != 2:
            raise Exception()

        AMT = int(first[0].strip())
        TotlUsr = int(first[1].strip())
        MinRef = int(parts[1].strip())

    except:

        bot.replyText(
            u,
            """<b>❌ Invalid Format

Use:
<code>Pᴇʀ Uѕᴇʀ Aᴍᴏᴜɴᴛ-Mᴀx Uѕᴇʀѕ--Mɪɴ Rᴇғᴇʀ</code>

Example:
<code>1-30--5</code></b>""",
            parse_mode="HTML"
        )

        Bot.handleNextCommand("/CreateBotRC")
        raise ReturnCommand()


    # ==============================
    # VALIDATION
    # ==============================

    if AMT <= 0 or TotlUsr <= 0 or MinRef < 0:

        bot.replyText(
            u,
            "<b>❌ Invalid Values. Please Enter Valid Numbers.</b>",
            parse_mode="HTML"
        )

        Bot.handleNextCommand("/CreateBotRC")
        raise ReturnCommand()


    # ==============================
    # GENERATE 12 CHARACTER CODE
    # AFTER AMOUNT IS RECEIVED
    # ==============================

    gift_code = ""

    for i in range(5):

        temp_code = libs.Random.randomStr(
            12,
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )

        if Bot.getData("GiftCode" + temp_code + "isAMT") == None:
            gift_code = temp_code
            break

    if gift_code == "":
        bot.replyText(
            u,
            "<b>❌ Could Not Generate Gift Code. Please Try Again.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # SAVE GIFT DATA
    # ==============================

    Bot.saveData(
        "GiftCode" + gift_code + "isAMT",
        [gift_code, str(AMT)]
    )

    Bot.saveData(
        "Gift" + gift_code + "MaxUsrCanClaim",
        TotlUsr
    )

    Bot.saveData(
        "Gift" + gift_code + "MinRef",
        MinRef
    )

    Bot.saveData(
        "Gift" + gift_code + "CountOfClaimedUsrs",
        0
    )

    Bot.saveData(
        "Gift" + gift_code + "KaStatus",
        "Active"
    )

    Bot.saveData("GtAvb", "Y")
    Bot.saveData("GiftAvailabe", "Y")


    # ==============================
    # ALL GIFT CODES
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []

    if gift_code not in All_BRC:
        All_BRC.append(gift_code)

    Bot.saveData("All_BRC", All_BRC)


    # ==============================
    # ADMIN ACTIVITY
    # ==============================

    now = libs.DateAndTime.now("Asia/Kolkata")

    date = now["date"]
    time_str = now["time"][:5]

    year, month, day = date.split("-")

    hour, minute = time_str.split(":")

    hour = int(hour)

    ampm = "am" if hour < 12 else "pm"

    if hour > 12:
        hour -= 12

    if hour == 0:
        hour = 12

    MONTHS = {
        "01": "Jan",
        "02": "Feb",
        "03": "Mar",
        "04": "Apr",
        "05": "May",
        "06": "Jun",
        "07": "Jul",
        "08": "Aug",
        "09": "Sep",
        "10": "Oct",
        "11": "Nov",
        "12": "Dec"
    }

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

    AdmAC = Bot.getData("AdmAC") or []

    act = (
        f"New Custom Gift Code ({gift_code}) "
        f"Created for {TotlUsr} Users, "
        f"Per User ₹{AMT}, Min Refers {MinRef}"
    )

    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> "
        f"[ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)


    # ==============================
    # SUCCESS
    # ==============================

    bot.replyText(
        u,
        f"""<b>🎉 Gift Code Created Successfully! 🎉

✅ The new gift code has been generated and is ready for use.
👥 Number of Users: {TotlUsr}
💸 Amount Per User: ₹{AMT}
🎯 Minimum Refers: {MinRef}
🔑 Gift Code: <code>{gift_code}</code></b>""",
        parse_mode="HTML"
    )
    pass


# ===/JoinPiro===
def cmd_JoinPiro():
    AMC = Bot.getData("AllMainCh") or []  
    AMCL=Bot.getData("AllMainChlink") or []

    PIRO_NJCL=User.getData("PIRO_NJCL") or []
    bot.replyText(u,PIRO_NJCL)
    #bot.replyText(u,PIRO_NJCL)

    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard = []
        row = []

        IND=0
        for LINK in AMCL:
            if str(AMC[IND]) in PIRO_NJCL:

                T="Join"

                (row.append({"text": T, "url": LINK}) if str(LINK).startswith(("https://", "http://", "tg://")) and len(str(LINK)) > 8 else None) 

                if len(row) == 2:  
                    keyboard.append(row)
                    row = []
                IND=IND+1
            if row: 
                keyboard.append(row)

        row = []
        row.append({"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"})
        keyboard.append(row)

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join Total Channel To Use Our Bot

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})
    pass


# ===/JoinPiro1===
def cmd_JoinPiro1():
    AMC = Bot.getData("AllMainCh") or []  
    AMCL=Bot.getData("AllMainChlink") or []

    text ="<b>🚫You Didn't Join These Channels 👇\n</b>"

    if not AMCL:
        bot.sendMessage("📌 No channels Links found!")
    else:
        keyboard = []
        row = []

        for LINK in AMCL:
            (row.append({"text": "Join", "url": LINK, "style": "primary"}) if str(LINK).startswith(("https://", "http://", "tg://")) and len(str(LINK)) > 8 else None) 

            if len(row) == 2:  
                keyboard.append(row)
                row = []

        if row: 
            keyboard.append(row)

        row = []
        row.append({"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success","style":"success"})
        keyboard.append(row)

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join Total Channel To Use Our Bot

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

        if options:

            NJLST=User.getData("NJLST") or ""
            text+=NJLST
            bot.replyText(chat_id=u,text=text,parse_mode="html",link_preview_options=LinkPreviewOptions(is_disabled=True))

    pass


# ===/JoinReqApprove===
def cmd_JoinReqApprove():
    # A "request to join" counts as joined for a short time only (the user waits for approval).
    # After JOIN_WAIT_MIN minutes the request is no longer trusted: if the user is not a real
    # member by then (request dismissed / cancelled / left), the join screen comes back.

    JOIN_WAIT_MIN = 10

    try:
        _uid = message.from_user.id
        _cid = message.chat.id
    except:
        raise ReturnCommand()

    _key = str(_cid) + "JoinRRq" + str(_uid)

    Bot.saveData(_key, "Y")

    try:
        Bot.runCommandAfter(JOIN_WAIT_MIN * 60, "/JoinReqExpire", options=_key)
    except:
        pass
    # ---- exact tracking: a request made through THIS bot's invite link counts as joined-via-bot right away ----
    try:
        _link = ""
        try:
            _link = str(message.invite_link.invite_link)
        except:
            _link = ""

        if _link != "" and _link != "None":
            _AMCL = Bot.getData("AllMainChlink") or []
            _AMCTGID = Bot.getData("AllMainChTGID") or []

            _k = -1
            for _i in range(len(_AMCTGID)):
                if str(_AMCTGID[_i]) == str(_cid):
                    _k = _i
                    break

            if _k >= 0 and _k < len(_AMCL):
                _a = _link.strip().replace("https://", "").replace("http://", "")
                _b = str(_AMCL[_k]).strip().replace("https://", "").replace("http://", "")
                if _a.endswith("/"):
                    _a = _a[:-1]
                if _b.endswith("/"):
                    _b = _b[:-1]

                _same = False
                if _a.endswith("…") or _a.endswith("..."):
                    _pre = _a.replace("…", "").replace("...", "")
                    _same = (len(_pre) > 8 and _b.startswith(_pre))
                else:
                    _same = (_a == _b)

                if _same:
                    _flag = "jvb_" + str(_cid) + "_" + str(_uid)
                    if not Bot.getData(_flag):
                        Bot.saveData(_flag, "1")
                        Bot.saveData("jrq_" + str(_cid) + "_" + str(_uid), "1")
                        _ck = str(_cid).replace("-", "m")
                        libs.Resources.globalRes("ChVia" + _ck).add(1)
                        libs.Resources.globalRes("ChReqPend" + _ck).add(1)
    except:
        pass
    pass


# ===/JoinReqExpire===
def cmd_JoinReqExpire():
    # Runs by itself (timer) - no admin check on purpose.
    # Ends the "pending request" pass of one user for one channel.

    if options is None:
        raise ReturnCommand()

    Bot.saveData(str(options), "N")
    pass


# ===/JoinReqSt===
def cmd_JoinReqSt():
    global msg
    # Toggle join-check for a default channel (callback toast, no new message)

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    P = str(params).strip().split(" ")

    if len(P) < 2 or P[1] not in ["Y", "N"]:
        raise ReturnCommand()

    DC = Bot.getData(str(P[0]) + "isDef") or "N"

    if DC == "Y":
        Bot.saveData(str(P[0]) + "JoinReq", P[1])
        msg = "🟢 Added To Check" if P[1] == "Y" else "🔴 Removed From Check"
        try:
            bot.answerCallbackQuery(call.id, msg)
        except:
            pass
        # refresh the info screen in place
        Bot.runCommand("/PIRO_Chanel_Pannel", options="info " + str(P[0]))
    else:
        try:
            bot.answerCallbackQuery(call.id, "🤯 This is Not Default Channel", show_alert=True)
        except:
            pass
    pass


# ===/LimEdit===
def cmd_LimEdit():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("withdraw_limits", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 Yᴏᴜ Dᴏɴ'ᴛ Hᴀᴠᴇ Pᴇʀᴍɪssɪᴏɴ Fᴏʀ Tʜɪs", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 Yᴏᴜ Dᴏɴ'ᴛ Hᴀᴠᴇ Pᴇʀᴍɪssɪᴏɴ Fᴏʀ Tʜɪs</b>", parse_mode="HTML")
        raise ReturnCommand()

    # coded by @Jenish_Dobariya1
    # params: min | max | ref | all

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    key = str(params).strip()

    w_min = Bot.getData("MinWith") or 0
    w_max = Bot.getData("MaxWith") or 0
    m_ref = Bot.getData("SetMinRef") or 0

    if key == "min":
        text = f"💰 <b>Mɪɴ Wɪᴛʜᴅʀᴀᴡ\n\nCᴜʀʀᴇɴᴛ: <b>₹{w_min}</b>\n\nSᴇɴᴅ Nᴇᴡ Mɪɴɪᴍᴜᴍ Aᴍᴏᴜɴᴛ (₹):</b>"

    elif key == "max":
        text = f"💎 <b>Mᴀx Wɪᴛʜᴅʀᴀᴡ\n\nCᴜʀʀᴇɴᴛ: <b>₹{w_max}</b>\n\nSᴇɴᴅ Nᴇᴡ Mᴀxɪᴍᴜᴍ Aᴍᴏᴜɴᴛ (₹):</b>"

    elif key == "ref":
        text = f"👥 <b>Mɪɴ Rᴇғᴇʀ Tᴏ Wɪᴛʜᴅʀᴀᴡ\n\nCᴜʀʀᴇɴᴛ: <b>{m_ref}</b>\n\nSᴇɴᴅ Hᴏᴡ Mᴀɴʏ Vᴇʀɪғɪᴇᴅ Rᴇғᴇʀs Aʀᴇ Nᴇᴇᴅᴇᴅ (0 = Oғғ):</b>"

    elif key == "all":
        text = f"""📝 <b>Sᴇᴛ Aʟʟ Lɪᴍɪᴛs

Sᴇɴᴅ Iɴ Tʜɪs Fᴏʀᴍᴀᴛ:
<code>Mɪɴ-Mᴀx--Rᴇғᴇʀ</code>

<b>Exᴀᴍᴘʟᴇ:</b> <code>10-100--5</code>

Cᴜʀʀᴇɴᴛ: <code>{w_min}-{w_max}--{m_ref}</code></b>"""

    else:
        raise ReturnCommand()

    User.saveData("lim_wait", key)
    User.saveData("lim_panel", message.message_id)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/SetMinMax"))

    show(message.message_id, text, markup)

    Bot.handleNextCommand("/LimEdit1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/LimEdit1===
def cmd_LimEdit1():
    global msg
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("withdraw_limits", True) is False:
        raise ReturnCommand()

    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    key = str(User.getData("lim_wait") or "")
    if key not in ["min", "max", "ref", "all"]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip().replace(" ", "")
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    panel = User.getData("lim_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/SetMinMax"))

    old_min = Bot.getData("MinWith") or 0
    old_max = Bot.getData("MaxWith") or 0
    old_ref = Bot.getData("SetMinRef") or 0

    new_min = old_min
    new_max = old_max
    new_ref = old_ref

    error = ""

    try:
        if key == "min":
            v = float(raw)
            if v < 0:
                raise Exception()
            new_min = int(v) if v == int(v) else v

        elif key == "max":
            v = float(raw)
            if v <= 0:
                raise Exception()
            new_max = int(v) if v == int(v) else v

        elif key == "ref":
            new_ref = int(raw)
            if new_ref < 0:
                raise Exception()

        else:
            parts = raw.split("--")
            if len(parts) != 2:
                raise Exception()

            a, b = parts[0].split("-")
            new_min = float(a)
            new_max = float(b)
            new_ref = int(parts[1])

            if new_min < 0 or new_max <= 0 or new_ref < 0:
                raise Exception()

            if new_min == int(new_min):
                new_min = int(new_min)

            if new_max == int(new_max):
                new_max = int(new_max)

    except:
        error = "format"

    if not error and float(new_min) > float(new_max):
        error = "order"

    if error:
        if error == "order":
            msg = f"<b>❌ Mɪɴ Cᴀɴɴᴏᴛ Bᴇ Mᴏʀᴇ Tʜᴀɴ Mᴀx.</b>\n\nNᴏᴡ: Mɪɴ ₹{new_min} • Mᴀx ₹{new_max}\n\nSᴇɴᴅ Aɢᴀɪɴ:"
        else:
            msg = "<b>❌ Iɴᴠᴀʟɪᴅ Vᴀʟᴜᴇ!</b>\n\nSᴇɴᴅ Aɢᴀɪɴ Iɴ Tʜᴇ Cᴏʀʀᴇᴄᴛ Fᴏʀᴍᴀᴛ."

        show(panel, msg, back)
        Bot.handleNextCommand("/LimEdit1", cancel_at_command=True)
        raise ReturnCommand()

    Bot.saveData("MinWith", new_min)
    Bot.saveData("MaxWith", new_max)
    Bot.saveData("SetMinRef", new_ref)

    User.saveData("lim_wait", "")

    # apply to every gateway
    _gl = Bot.getData("AddedGateways") or []

    for _g in _gl:
        if key in ["min", "all"]:
            _g["min"] = 0

        if key in ["max", "all"]:
            _g["max"] = 0

        if key in ["ref", "all"]:
            _g["ref"] = 0

    Bot.saveData("AddedGateways", _gl)

    now = libs.DateAndTime.now("Asia/Kolkata")

    AdmAC = Bot.getData("AdmAC") or []

    AdmAC.append(
        f"<b>📆 Tɪᴍᴇ:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>Bʏ {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Aᴄᴛɪᴏɴ:</b> Lɪᴍɪᴛs Mɪɴ {old_min}→{new_min}, Mᴀx {old_max}→{new_max}, Rᴇғᴇʀ {old_ref}→{new_ref}"
    )

    Bot.saveData("AdmAC", AdmAC)

    text = f"""✅ <b>Lɪᴍɪᴛs Uᴘᴅᴀᴛᴇᴅ</b>

💰 <b>Mɪɴ Wɪᴛʜᴅʀᴀᴡ:</b> ₹{new_min}
💎 <b>Mᴀx Wɪᴛʜᴅʀᴀᴡ:</b> ₹{new_max}
👥 <b>Mɪɴ Rᴇғᴇʀ Tᴏ Wɪᴛʜᴅʀᴀᴡ:</b> {new_ref}

<i>✅ Aᴘᴘʟɪᴇᴅ Tᴏ Aʟʟ Gᴀᴛᴇᴡᴀʏs.</i>"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/SetMinMax"))

    show(panel, text, markup)

    raise ReturnCommand()
    pass


# ===/ManageText===
def cmd_ManageText():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫You Are Not This Bot Admin</i></b>"
        )
        raise ReturnCommand()


    markup = InlineKeyboardMarkup()

    # Row 1
    markup.row(InlineKeyboardButton(text='Aᴅᴅ Bᴀʟᴀɴᴄᴇ Tᴇxᴛ',callback_data='/AddBalTxt'))
    markup.row(InlineKeyboardButton(
                text="Bᴏᴛ Oғғ Tᴇxᴛ",
                callback_data="/BotOffText"
            )
        )

    # Row 2
    markup.row(InlineKeyboardButton(
                text="Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ",
                callback_data="/PIRO_LeadSet"
            ))
    markup.row(InlineKeyboardButton(
                text="Wɪᴛʜᴅʀᴀᴡ Oғғ Tᴇxᴛ",
                callback_data="/WithdrwOFF_Text"
            )
        )
    # Row 3
    markup.row(
        InlineKeyboardButton(
            text="⬅️ Bᴀᴄᴋ",
            callback_data="/admin AP"
        )
    )


    bot.editMessageText(
        chat_id=u,
        message_id=call.message.message_id,
        text=f"""<b><i>📝 Mᴇssᴀɢᴇ Mᴀɴᴀɢᴇᴍᴇɴᴛ</i>
    
Hᴇʀᴇ, ʏᴏᴜ ᴄᴀɴ ᴍᴀɴᴀɢᴇ ᴀɴᴅ ᴇᴅɪᴛ ʏᴏᴜʀ ʙᴏᴛ ᴍᴇssᴀɢᴇs ᴀs ʏᴏᴜ ᴡɪsʜ. ✨</b>""",
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/MyInvitez===
def cmd_MyInvitez():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    RefStrtC = Bot.getData(str(u)+"RefStrtC") or 0

    refC = Bot.getData(str(u)+"RefCount") or 0




    RefJC = Bot.getData(str(u)+"RefJC") or 0
    L = RefStrtC - RefJC
    if L < 0:
        L = 0

    Bot.replyText(u,f"""<b>🤫 {RefStrtC} Users Started From Your Link 

🔍 {L} Users Haven’t Joined Channels. 

👑 Verified And Credited From :- {refC}
</b>""")
    pass


# ===/NotJoinedMsg===
def cmd_NotJoinedMsg():
    # Extra message under the join screen (sent from "🟢 Joined" / Claim only):
    # lists the channels the user has not joined - username if the channel has one, else its name.
    # options = comma separated chat IDs of the not-joined channels

    if Bot.getData("JoinMode") == "ALL":
        raise ReturnCommand()

    if options is None:
        raise ReturnCommand()

    ids = []
    for x in str(options).split(","):
        x = x.strip()
        if x:
            ids.append(x)

    if not ids:
        raise ReturnCommand()

    AMC = Bot.getData("AllMainCh") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    tok = Bot.info().token


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def is_id(t):
        t = str(t).strip()
        return t == "" or t.lstrip("-").isdigit()


    lines = []
    changed = False

    for k in range(len(AMCTGID)):
        if str(AMCTGID[k]) not in ids:
            continue

        uname = str(AMCU[k]) if k < len(AMCU) else ""
        title = str(AMC[k]) if k < len(AMC) else ""
        link = str(AMCL[k]) if k < len(AMCL) else ""

        # the stored name is only an ID -> ask Telegram for the real name / username once and save it
        if is_id(title) or (not uname.startswith("@")):
            try:
                b = HTTP.get("https://api.telegram.org/bot" + tok + "/getChat?chat_id=" + str(AMCTGID[k])).json()
                res = bunchify(b)["result"]
                real_title = str(res.get("title", "") or "")
                real_user = str(res.get("username", "") or "")
                if real_title and k < len(AMC):
                    AMC[k] = real_title
                    title = real_title
                    changed = True
                if real_user and k < len(AMCU):
                    AMCU[k] = "@" + real_user
                    uname = "@" + real_user
                    changed = True
            except:
                pass

        if uname.startswith("@"):
            lines.append('<b><a href="https://t.me/' + uname[1:] + '">' + esc(uname) + "</a></b>")
        else:
            shown = title if not is_id(title) else "Private Channel"
            if link.startswith(("https://", "http://", "tg://")) and len(link) > 8:
                lines.append('<b><a href="' + link.replace('"', "") + '">' + esc(shown) + "</a></b>")
            else:
                lines.append("<b>" + esc(shown) + "</b>")

    if changed:
        Bot.saveData("AllMainCh", AMC)
        Bot.saveData("AllMainChUsernm", AMCU)

    if not lines:
        raise ReturnCommand()

    # the message stays until the user taps Claim (only then it is deleted)
    try:
        _m = bot.sendMessage(
            "<b>🚫 You didn't join the channels below 👇</b>\n" + "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        _ids = User.getData("PIRO_njmsgs") or []
        _ids.append(_m.message_id)
        User.saveData("PIRO_njmsgs", _ids[-10:])
    except:
        pass
    pass


# ===/PIRO_AddAdmin===
def cmd_PIRO_AddAdmin():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("admins", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    bot.replyText(chat_id=message.chat.id,text=f"""<b>Send UserID of Admin You Want To Add</b>""",parse_mode="html")

    User.saveData("EDMsgID",message.message_id) 

    Bot.handleNextCommand("/PIRO_AddAdmin1")
    pass


# ===/PIRO_AddAdmin1===
def cmd_PIRO_AddAdmin1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("admins", True) is False:
        raise ReturnCommand()
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]


    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    _new = str(message.text).strip()
    if not _new.isdigit() or len(_new) < 5:
        bot.replyText(u, "<b>❌ Send a valid numeric Telegram User ID.</b>", parse_mode="HTML")
        raise ReturnCommand()

    check_user = [str(x) for x in AllBotAdminss].count(_new)
    if check_user > 0:
        """user_exist"""
        T="Admin Already Exists"
    else:
        AllBotAdminss.append(_new)
        Bot.saveData("AllBotAdminss", AllBotAdminss)
        T="Admin Added Successfully"


    Bot.runCommand("/PIRO_Admins",options=f"<b>{T}</b>")











    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


    AdmAC=Bot.getData("AdmAC") or []

    act=f"Added {message.text} as Bot Admin"
    AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

    Bot.saveData("AdmAC",AdmAC)









    Bot.sendMessage(f"<b>{T}</b>")
    pass


# ===/PIRO_AddCh===
def cmd_PIRO_AddCh():
    # Add Channel - prompt (edits the panel message in place)

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    User.saveData("ch_wait", "add")
    User.saveData("ch_panel", message.message_id)

    text = """➕ <b>Add Channel</b>

Send the channel <b>username</b> (<code>@mychannel</code>) or <b>chat ID</b> (<code>-100123456789</code>), or <b>forward</b> any message from the channel.

Multiple channels: send one per line.

<i>Make the bot an admin of the channel so it can check members and create the invite link automatically.</i>"""

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(message.message_id, text, mk)

    Bot.handleNextCommand("/PIRO_AddCh1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/PIRO_AddCh1===
def cmd_PIRO_AddCh1():
    global msg
    # Add Channel - process (single clean result message, no crash on bad input)

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    if str(User.getData("ch_wait")) != "add":
        raise ReturnCommand()


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    raw = str(message.text or "").strip()
    fwd_chat = None
    try:
        fwd_chat = message.forward_from_chat
    except:
        fwd_chat = None

    if raw.startswith("/") and not fwd_chat:
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    panel = User.getData("ch_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    CHL = []
    if fwd_chat:
        CHL = [str(fwd_chat.id)]
    else:
        for ln in raw.split("\n"):
            ln = ln.strip()
            if ln:
                CHL.append(ln)

    added = []
    skipped = []
    failed = ""
    tok = ""

    try:
        AllMainCh = Bot.getData("AllMainCh") or []
        AMCL = Bot.getData("AllMainChlink") or []
        AMCTGID = Bot.getData("AllMainChTGID") or []
        AMCU = Bot.getData("AllMainChUsernm") or []

        for i in CHL:
            try:
                if len(i) > 64 or (("@" not in i) and ("-" not in i) and (not i.isdigit())):
                    skipped.append(i[:30] + " — not a channel username or chat ID")
                    continue

                BIA = True
                CLink = "None"
                TGID = str(i)
                TITLE = str(i)
                USRNAM = "None"

                # native bot calls (much faster than going through the HTTP proxy), only 1-2 calls per channel
                chat = None
                try:
                    chat = bot.getChat(i)
                except:
                    chat = None
                    BIA = False

                if chat is not None:
                    try:
                        TGID = str(chat.id)
                    except:
                        try:
                            TGID = str(chat["id"])
                        except:
                            TGID = str(i)
                    try:
                        TITLE = str(chat.title)
                    except:
                        try:
                            TITLE = str(chat["title"])
                        except:
                            TITLE = str(i)
                    try:
                        _un = chat.username
                        USRNAM = str(_un) if _un else "None"
                    except:
                        try:
                            _un = chat["username"]
                            USRNAM = str(_un) if _un else "None"
                        except:
                            USRNAM = "None"
                    try:
                        _il = chat.invite_link
                        CLink = str(_il) if _il else "None"
                    except:
                        try:
                            _il = chat["invite_link"]
                            CLink = str(_il) if _il else "None"
                        except:
                            CLink = "None"

                # create an invite link automatically (works only when the bot is admin of the channel)
                if CLink == "None" and chat is not None:
                    try:
                        _lk = bot.createChatInviteLink(TGID)
                        try:
                            CLink = str(_lk.invite_link)
                        except:
                            CLink = str(_lk["invite_link"])
                    except:
                        CLink = "None"
                        BIA = False

                link_auto = True
                if CLink == "None":
                    link_auto = False
                    if USRNAM != "None":
                        CLink = "https://t.me/" + USRNAM
                    else:
                        CLink = "https://t.me/c/" + TGID.replace("-100", "")

                if TGID in [str(x) for x in AMCTGID]:
                    skipped.append(TITLE + " — already added")
                    continue

                USRNAM_DISPLAY = ("@" + USRNAM) if USRNAM != "None" else TGID

                AllMainCh.append(TITLE)
                AMCL.append(CLink)
                AMCTGID.append(TGID)
                AMCU.append(USRNAM_DISPLAY)

                note = "✅ Bot Is Admin" if BIA else "❌ Bot Is Not Admin"
                note += ", Channel Added Successfully!!" if link_auto else ", Set The Invite Link Manually"
                added.append(TITLE + " (" + USRNAM_DISPLAY + ") — " + note)
            except Exception as e1:
                skipped.append(str(i)[:30] + " — could not read")

        if added:
            Bot.saveData("AllMainCh", AllMainCh)
            Bot.saveData("AllMainChlink", AMCL)
            Bot.saveData("AllMainChTGID", AMCTGID)
            Bot.saveData("AllMainChUsernm", AMCU)

            now = libs.DateAndTime.now("Asia/Kolkata")
            AdmAC = Bot.getData("AdmAC") or []
            AdmAC.append(
                f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
                f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
                f"🔍 <b>Action:</b> Channel added: " + ", ".join([a.split(" (")[0] for a in added])
            )
            Bot.saveData("AdmAC", AdmAC)
    except Exception as e:
        failed = str(e)[:100]

    if failed:
        show(panel, "<b>Something went wrong, nothing was saved.</b>\n\n<code>" + esc(failed) + "</code>\n\nPlease send again.", back)
        Bot.handleNextCommand("/PIRO_AddCh1", cancel_at_command=True)
        raise ReturnCommand()

    if not added:
        msg = "<b>No channel was added.</b>\n\nSend a username like <code>@mychannel</code> or a chat ID like <code>-100123456789</code>."
        if skipped:
            msg += "\n\n<b>Skipped:</b>\n" + "\n".join(["• " + esc(x) for x in skipped])
        show(panel, msg, back)
        Bot.handleNextCommand("/PIRO_AddCh1", cancel_at_command=True)
        raise ReturnCommand()

    User.saveData("ch_wait", "")

    msg = (
        "<b>Added " + str(len(added)) + " channel(s)\n\n" +
        "\n".join(["• " + esc(a) for a in added]) +
        "</b>"
    )
    if skipped:
        msg += "\n\n<b>Skipped:</b>\n" + "\n".join(["• " + esc(x) for x in skipped])

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="➕ Add More", callback_data="/PIRO_AddCh"))
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(panel, msg, mk)
    raise ReturnCommand()
    pass


# ===/PIRO_AdminAction===
def cmd_PIRO_AdminAction():
    AdmAC=Bot.getData("AdmAC") or []


    x=int(len(AdmAC)) 
    latest_10 = AdmAC[-10:][::-1]

    # Join and print them
    bot.sendMessage("\n\n".join(latest_10))

    #bot.sendMessage(AdmAC)


    pass


# ===/PIRO_Admins===
def cmd_PIRO_Admins():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("admins", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()





    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"



    AdmAC=Bot.getData("AdmAC") or []










    if str(params) != "None":

        Owner = Bot.getData("Owner")

        # Owner cannot remove themself
        if str(params) == str(u) and str(u) == str(Owner):
            bot.answerCallbackQuery(
                call.id,
                "🚫 Owner cannot remove themself from Admins.",
                show_alert=True
            )
            raise ReturnCommand()

        # No admin can remove Owner
        if str(params) == str(Owner):
            bot.answerCallbackQuery(
                call.id,
                "🚫 Owner cannot be removed from Admins.",
                show_alert=True
            )
            raise ReturnCommand()

        # Remove selected admin
        if str(params) in [str(x) for x in AllBotAdminss]:
            AllBotAdminss.remove(params)

            act = f"Removed {params} from Bot Admin"

            AdmAC.append(
                f"<b>📆 Time:</b> {EasyTime}\n"
                f"👥 <b>By {message.from_user.first_name}</b> "
                f"[ID: <code>{u}</code>]\n"
                f"🔍<b> Action: </b> {act}"
            )

            Bot.saveData("AdmAC", AdmAC)


    markup = InlineKeyboardMarkup()

    for admin in AllBotAdminss:
        markup.add(InlineKeyboardButton(text=admin,callback_data="/PIRO_Admins "+str(admin) ),InlineKeyboardButton(text="❌",callback_data="/PIRO_Admins "+str(admin)))

    markup.add(InlineKeyboardButton(text='➕Aᴅᴅ Aᴅᴍɪɴ',callback_data='/PIRO_AddAdmin'))


    markup.add(InlineKeyboardButton(text='🔙 Bᴀᴄᴋ',callback_data='/admin AP'))

    Bot.saveData("AllBotAdminss",AllBotAdminss)


    e="y"
    T="<b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Yᴏᴜʀ Aᴅᴍɪɴs</b>"
    if options:
        T=options

    MD=User.getData("EDMsgID") 

    try:
        bot.editMessageText(chat_id=u,message_id=message.message_id,text=T,reply_markup=markup,parse_mode="Html")
        if options:
            e="n"
    except:
        pass


    try:
        bot.editMessageText(chat_id=u,message_id=MD,text=T)
        if options:
            e="n"
    except:
        pass



    if e=="n":
        Bot.runCommand ("/admin")
    pass


# ===/PIRO_BC_Cancel===
def cmd_PIRO_BC_Cancel():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    msg_id = Bot.getData("BC_MsgID")
    if msg_id:
        try:
            bot.editMessageText(
                chat_id=u,
                message_id=msg_id,
                text="<b>❌ Broadcast/Schedule Cancelled</b>"
            )
        except:
            pass

    Bot.saveData("BC_Data", None)
    Bot.saveData("BC_MsgID", None)
    Bot.saveData("BC_Schedule_Date", None)
    Bot.saveData("BC_Schedule_Time", None)
    Bot.saveData("BC_Scheduled_Data", None)
    Bot.saveData("BC_Scheduled_Date", None)
    Bot.saveData("BC_Scheduled_Time", None)
    Bot.saveData("BC_Scheduled_User", None)

    # Fix: Answer callback query properly
    try:
        bot.answerCallbackQuery("✅ Cancelled!")
    except:
        pass
    pass


# ===/PIRO_BC_Confirm===
def cmd_PIRO_BC_Confirm():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    # Get broadcast data
    bc_data = Bot.getData("BC_Data") or {}
    if not bc_data:
        bot.replyText(u, "<b>❌ No broadcast data found!</b>")
        raise ReturnCommand()

    channels = bc_data.get("channels", [])
    if not channels:
        bot.replyText(u, "<b>❌ No channels found!</b>")
        raise ReturnCommand()

    # Get message data
    txt = bc_data.get("text", "")
    file_id = bc_data.get("file_id")
    msg_type = bc_data.get("type", "text")

    # Update status - starting broadcast
    msg_id = Bot.getData("BC_MsgID")
    if msg_id:
        bot.editMessageText(
            chat_id=u,
            message_id=msg_id,
            text=f"<b>📢 Broadcasting...</b>\n\n✅ Started: {len(channels)} channels\n⏳ Progress: 0/{len(channels)}"
        )

    # Get channel names for failed channels display
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []
    AMCL = Bot.getData("AllMainChlink") or []

    # Send to each channel manually
    success = 0
    failed = 0
    failed_list = []

    for channel_id in channels:
        try:
            if msg_type == "text":
                bot.sendMessage(
                    chat_id=str(channel_id),
                    text=txt,
                    parse_mode="html",
                    disable_web_page_preview=True
                )
            elif msg_type == "photo":
                bot.sendPhoto(
                    chat_id=str(channel_id),
                    photo=file_id,
                    caption=txt if txt else "",
                    parse_mode="html"
                )
            elif msg_type == "video":
                bot.sendVideo(
                    chat_id=str(channel_id),
                    video=file_id,
                    caption=txt if txt else "",
                    parse_mode="html"
                )
            elif msg_type == "document":
                bot.sendDocument(
                    chat_id=str(channel_id),
                    document=file_id,
                    caption=txt if txt else "",
                    parse_mode="html"
                )
            elif msg_type == "audio":
                bot.sendAudio(
                    chat_id=str(channel_id),
                    audio=file_id,
                    caption=txt if txt else "",
                    parse_mode="html"
                )
            elif msg_type == "sticker":
                bot.sendSticker(
                    chat_id=str(channel_id),
                    sticker=file_id
                )
            elif msg_type == "animation":
                bot.sendAnimation(
                    chat_id=str(channel_id),
                    animation=file_id,
                    caption=txt if txt else "",
                    parse_mode="html"
                )
            success += 1
        except:
            failed += 1
            # Store channel info for failed
            ch_name = "Unknown"
            ch_link = "Unknown"
            for i, ch_id in enumerate(AMCTGID):
                if str(ch_id) == str(channel_id):
                    if i < len(AMCU):
                        ch_name = AMCU[i]
                    if i < len(AMCL):
                        ch_link = AMCL[i]
                    break

            failed_list.append({
                "id": str(channel_id),
                "name": ch_name,
                "link": ch_link
            })

        # Update progress every 2 channels
        if (success + failed) % 2 == 0 or (success + failed) == len(channels):
            if msg_id:
                bot.editMessageText(
                    chat_id=u,
                    message_id=msg_id,
                    text=f"""<b>📢 Broadcasting...</b>

📤 <b>Progress:</b> {success + failed}/{len(channels)}
✅ <b>Success:</b> {success}
❌ <b>Failed:</b> {failed}"""
                )

    # Generate EasyTime for logging
    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}
    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

    # Final report with links
    report = f"""<b>📢 Broadcast Complete</b>

✅ <b>Success:</b> {success}
❌ <b>Failed:</b> {failed}
📊 <b>Total:</b> {len(channels)}"""

    if failed_list:
        report += "\n\n<b>⚠️ Failed Channels:</b>\n"
        for i, ch in enumerate(failed_list[:15]):
            name = ch.get('name', 'Unknown')
            link = ch.get('link', '')
            if link and link != "None" and link != "https://t.me/None":
                report += f"{i+1}. <a href='{link}'>{name}</a>\n"
            else:
                report += f"{i+1}. {name} (ID: {ch.get('id', 'Unknown')})\n"
        if len(failed_list) > 15:
            report += f"\n<i>+{len(failed_list)-15} more failures</i>"

    # Update final message
    if msg_id:
        bot.editMessageText(
            chat_id=u,
            message_id=msg_id,
            text=report,
            disable_web_page_preview=True
        )

    # Log the broadcast
    AdmAC = Bot.getData("AdmAC") or []
    action = f"📢 Broadcast sent to {success} channels ({failed} failed)"
    AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {action}")
    Bot.saveData("AdmAC", AdmAC)

    # Clean up
    Bot.saveData("BC_Data", None)
    Bot.saveData("BC_MsgID", None)
    pass


# ===/PIRO_BC_Process===
def cmd_PIRO_BC_Process():
    global msg
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if options == None:
        AMCTGID = Bot.getData("AllMainChTGID") or []
        msg = bot.replyText(u, f"""<b>📢 Broadcast to {len(AMCTGID)} Channels</b>

<i>Send any message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Process", options=True)
        raise ReturnCommand()
    else:
        if message.text == "/cancel":
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Broadcast Cancelled</b>")
            Bot.saveData("BC_MsgID", None)
            raise ReturnCommand()

        # Detect content type
        txt = message.caption if message.caption else message.text
        entities = message.caption_entities if message.caption else message.entities
        if entities:
            txt = apply_html_entities(txt, entities, {})

        # Get message type and file_id
        msg_type = "text"
        file_id = None

        if message.photo:
            msg_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            msg_type = "video"
            file_id = message.video.file_id
        elif message.document:
            msg_type = "document"
            file_id = message.document.file_id
        elif message.audio:
            msg_type = "audio"
            file_id = message.audio.file_id
        elif message.sticker:
            msg_type = "sticker"
            file_id = message.sticker.file_id
        elif message.animation:
            msg_type = "animation"
            file_id = message.animation.file_id
        elif message.text:
            msg_type = "text"
        else:
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Unsupported File Format!</b>")
            Bot.saveData("BC_MsgID", None)
            raise ReturnCommand()

        # Save broadcast data
        broadcast_data = {
            "type": msg_type,
            "text": txt,
            "file_id": file_id,
            "channels": Bot.getData("AllMainChTGID") or []
        }
        Bot.saveData("BC_Data", broadcast_data)

        # Show preview and confirmation
        msg_id = Bot.getData("BC_MsgID")
        preview = f"""<b>📢 Broadcast Preview</b>

📊 <b>Target:</b> {len(broadcast_data['channels'])} channels
📝 <b>Type:</b> {msg_type.upper()}

🔄 <b>Content:</b>
{txt[:300] if txt else 'No text'}{'...' if txt and len(txt) > 300 else ''}

<i>✅ Click Confirm to broadcast</i>
<i>❌ Click Cancel to abort</i>"""

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Confirm", callback_data="/PIRO_BC_Confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="/PIRO_BC_Cancel")
        )

        if msg_id:
            bot.editMessageText(chat_id=u, message_id=msg_id, text=preview, reply_markup=markup)
        else:
            new_msg = bot.replyText(u, preview, reply_markup=markup)
            Bot.saveData("BC_MsgID", new_msg.message_id)

    pass


# ===/PIRO_BC_Schedule_Confirm===
def cmd_PIRO_BC_Schedule_Confirm():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bc_data = Bot.getData("BC_Data") or {}
    if not bc_data:
        bot.replyText(u, "<b>❌ No broadcast data found!</b>")
        raise ReturnCommand()

    date = Bot.getData("BC_Schedule_Date")
    time_display = Bot.getData("BC_Schedule_Time_12hr")
    hour_24 = Bot.getData("BC_Schedule_Hour")
    minute = Bot.getData("BC_Schedule_Minute")

    # Get current time
    now = libs.DateAndTime.now("Asia/Kolkata")
    current_date = now["date"]
    current_time = now["time"][:5]

    # Parse scheduled date (DD-MM-YYYY)
    date_parts = date.split("-")
    sched_day = int(date_parts[0])
    sched_month = int(date_parts[1])
    sched_year = int(date_parts[2])

    # Parse current date (YYYY-MM-DD)
    current_parts = current_date.split("-")
    curr_year = int(current_parts[0])
    curr_month = int(current_parts[1])
    curr_day = int(current_parts[2])

    # Parse current time
    curr_time_parts = current_time.split(":")
    curr_hour = int(curr_time_parts[0])
    curr_min = int(curr_time_parts[1])

    # Check if schedule is in future or now
    is_valid = False

    if sched_year > curr_year:
        is_valid = True
    elif sched_year == curr_year:
        if sched_month > curr_month:
            is_valid = True
        elif sched_month == curr_month:
            if sched_day > curr_day:
                is_valid = True
            elif sched_day == curr_day:
                if hour_24 > curr_hour:
                    is_valid = True
                elif hour_24 == curr_hour and minute > curr_min:
                    is_valid = True
                elif hour_24 == curr_hour and minute == curr_min:
                    is_valid = True

    if not is_valid:
        current_date_display = f"{curr_day:02d}-{curr_month:02d}-{curr_year}"
        bot.replyText(u, f"""<b>❌ Scheduled time is in the past!</b>

📆 <b>Current:</b> {current_date_display} at {current_time}
📆 <b>Scheduled:</b> {date} at {time_display}

<i>Please select a future date and time</i>""")
        raise ReturnCommand()

    # Calculate seconds difference
    diff_days = 0
    if sched_year > curr_year:
        diff_days += (sched_year - curr_year) * 365
    if sched_month > curr_month:
        diff_days += (sched_month - curr_month) * 30
    if sched_day > curr_day:
        diff_days += (sched_day - curr_day)

    diff_hours = hour_24 - curr_hour
    diff_minutes = minute - curr_min

    delay_seconds = (diff_days * 86400) + (diff_hours * 3600) + (diff_minutes * 60)

    if delay_seconds < 5:
        delay_seconds = 5

    # Show confirmation by editing the message
    msg_id = Bot.getData("BC_MsgID")
    if msg_id:
        try:
            bot.editMessageText(
                chat_id=u,
                message_id=msg_id,
                text=f"""<b>📅 Broadcast Scheduled!</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}
📊 <b>Target:</b> {len(bc_data.get('channels', []))} channels

<i>✅ Broadcast will start automatically at scheduled time</i>"""
            )
        except:
            pass

    # Save all broadcast data to be used later
    Bot.saveData("BC_Scheduled_Data", bc_data)
    Bot.saveData("BC_Scheduled_Date_Display", date)
    Bot.saveData("BC_Scheduled_Time_Display", time_display)
    Bot.saveData("BC_Scheduled_User", u)

    # Create a unique task ID
    task_counter = Bot.getData("BC_Task_Counter") or 0
    task_counter += 1
    Bot.saveData("BC_Task_Counter", task_counter)
    task_id = f"sch_bc_{u}_{task_counter}"

    # Save the task info
    Bot.saveData(f"BC_Task_{task_id}", {
        "user": u,
        "data": bc_data,
        "date": date,
        "time": time_display
    })

    # Generate EasyTime for logging
    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    sched_hour_display = hour_24
    sched_ampm = "am"
    if sched_hour_display >= 12:
        sched_ampm = "pm"
    if sched_hour_display > 12:
        sched_hour_display -= 12
    if sched_hour_display == 0:
        sched_hour_display = 12

    EasyTime = f"{sched_day:02d} {MONTHS[date_parts[1]]}, {sched_hour_display:02d}:{minute:02d} {sched_ampm}"

    #
    pass


# ===/PIRO_BC_Schedule_Date===
def cmd_PIRO_BC_Schedule_Date():
    global msg
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if options == None:
        AMCTGID = Bot.getData("AllMainChTGID") or []
        msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast to {len(AMCTGID)} Channels</b>

<i>Send the date for broadcast</i>
<b>Format:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Send /cancel to cancel</i>""")
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Schedule_Date", options=True)
        raise ReturnCommand()
    else:
        if message.text == "/cancel":
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                try:
                    bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
                except:
                    pass
            Bot.saveData("BC_MsgID", None)
            raise ReturnCommand()

        # Validate date format
        try:
            date_parts = message.text.split("-")
            if len(date_parts) != 3:
                raise ValueError()
            day = int(date_parts[0])
            month = int(date_parts[1])
            year = int(date_parts[2])

            # Check if date is valid
            if day < 1 or day > 31 or month < 1 or month > 12 or year < 2024:
                raise ValueError()

            # Get current date from TBC (format: YYYY-MM-DD)
            now = libs.DateAndTime.now("Asia/Kolkata")
            current_date = now["date"]
            current_parts = current_date.split("-")
            current_year = int(current_parts[0])
            current_month = int(current_parts[1])
            current_day = int(current_parts[2])

            # Compare dates - Allow today's date
            is_valid = False
            if year > current_year:
                is_valid = True
            elif year == current_year and month > current_month:
                is_valid = True
            elif year == current_year and month == current_month and day >= current_day:
                is_valid = True

            if not is_valid:
                current_date_display = f"{current_day:02d}-{current_month:02d}-{current_year}"
                bot.replyText(u, f"""<b>❌ Please select today or a future date!</b>

📆 <b>Current Date:</b> {current_date_display}
📆 <b>You entered:</b> {message.text}

<i>Please enter today's date or a future date</i>""")
                raise ReturnCommand()

            Bot.saveData("BC_Schedule_Date", message.text)

            # Ask for time with AM/PM format
            msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {message.text}

<i>Send the time for broadcast</i>
<b>Format:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)

<i>Send /cancel to cancel</i>""")

            Bot.saveData("BC_MsgID", msg.message_id)
            Bot.handleNextCommand("/PIRO_BC_Schedule_Time", options=True)

        except:
            bot.replyText(u, """<b>❌ Invalid date format!</b>

<b>Use:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Please send date in correct format</i>""")
            raise ReturnCommand()
    pass


# ===/PIRO_BC_Schedule_Execute===
def cmd_PIRO_BC_Schedule_Execute():
    global u
    # No admin check - runs automatically via Bot.runCommandAfter()

    # Get task ID from options
    task_id = options
    task_data = Bot.getData(f"BC_Task_{task_id}") or {}

    if not task_data:
        raise ReturnCommand()

    # Get data from task
    u = task_data.get("user")
    bc_data = task_data.get("data", {})
    date = task_data.get("date", "Unknown")
    time = task_data.get("time", "Unknown")

    channels = bc_data.get("channels", [])
    txt = bc_data.get("text", "")
    file_id = bc_data.get("file_id")
    msg_type = bc_data.get("type", "text")

    if not channels:
        bot.replyText(u, "<b>❌ No channels found!</b>")
        raise ReturnCommand()

    # Send notification
    bot.replyText(u, f"""<b>📢 Scheduled Broadcast Started!</b>

📆 <b>Scheduled:</b> {date} at {time}
📊 <b>Target:</b> {len(channels)} channels
📝 <b>Type:</b> {msg_type.upper()}

<i>⏳ Broadcasting in progress...</i>""")

    # Get channel names for failed display
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []
    AMCL = Bot.getData("AllMainChlink") or []

    # Send to channels
    success = 0
    failed = 0
    failed_list = []

    for channel_id in channels:
        try:
            if msg_type == "text":
                bot.sendMessage(chat_id=str(channel_id), text=txt, parse_mode="html", disable_web_page_preview=True)
            elif msg_type == "photo":
                bot.sendPhoto(chat_id=str(channel_id), photo=file_id, caption=txt if txt else "", parse_mode="html")
            elif msg_type == "video":
                bot.sendVideo(chat_id=str(channel_id), video=file_id, caption=txt if txt else "", parse_mode="html")
            elif msg_type == "document":
                bot.sendDocument(chat_id=str(channel_id), document=file_id, caption=txt if txt else "", parse_mode="html")
            elif msg_type == "audio":
                bot.sendAudio(chat_id=str(channel_id), audio=file_id, caption=txt if txt else "", parse_mode="html")
            elif msg_type == "sticker":
                bot.sendSticker(chat_id=str(channel_id), sticker=file_id)
            elif msg_type == "animation":
                bot.sendAnimation(chat_id=str(channel_id), animation=file_id, caption=txt if txt else "", parse_mode="html")
            success += 1
        except:
            failed += 1
            # Get channel name
            ch_name = str(channel_id)
            ch_link = ""
            for i, ch_id in enumerate(AMCTGID):
                if str(ch_id) == str(channel_id):
                    if i < len(AMCU):
                        ch_name = AMCU[i]
                    if i < len(AMCL):
                        ch_link = AMCL[i]
                    break
            failed_list.append({
                "name": ch_name,
                "link": ch_link
            })

    # Generate EasyTime
    now = libs.DateAndTime.now("Asia/Kolkata")
    date_now = now["date"]  
    time_now = now["time"][:5] 
    year, month, day = date_now.split("-")
    hour, minute = time_now.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}
    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

    #
    pass


# ===/PIRO_BC_Schedule_Message===
def cmd_PIRO_BC_Schedule_Message():
    global msg
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if options == None:
        date = Bot.getData("BC_Schedule_Date") or "DD-MM-YYYY"
        time_display = Bot.getData("BC_Schedule_Time_12hr") or "HH:MM AM/PM"
        msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}

<i>Now send your message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Schedule_Message", options=True)
        raise ReturnCommand()
    else:
        if message.text == "/cancel":
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                try:
                    bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
                except:
                    pass
            Bot.saveData("BC_MsgID", None)
            Bot.saveData("BC_Schedule_Date", None)
            Bot.saveData("BC_Schedule_Time_12hr", None)
            Bot.saveData("BC_Schedule_Time_24hr", None)
            Bot.saveData("BC_Schedule_Hour", None)
            Bot.saveData("BC_Schedule_Minute", None)
            raise ReturnCommand()

        # Detect content type
        txt = message.caption if message.caption else message.text
        entities = message.caption_entities if message.caption else message.entities
        if entities:
            txt = apply_html_entities(txt, entities, {})

        # Get message type and file_id
        msg_type = "text"
        file_id = None

        if message.photo:
            msg_type = "photo"
            file_id = message.photo[-1].file_id
        elif message.video:
            msg_type = "video"
            file_id = message.video.file_id
        elif message.document:
            msg_type = "document"
            file_id = message.document.file_id
        elif message.audio:
            msg_type = "audio"
            file_id = message.audio.file_id
        elif message.sticker:
            msg_type = "sticker"
            file_id = message.sticker.file_id
        elif message.animation:
            msg_type = "animation"
            file_id = message.animation.file_id
        elif message.text:
            msg_type = "text"
        else:
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                try:
                    bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Unsupported File Format!</b>")
                except:
                    pass
            Bot.saveData("BC_MsgID", None)
            Bot.saveData("BC_Schedule_Date", None)
            Bot.saveData("BC_Schedule_Time_12hr", None)
            Bot.saveData("BC_Schedule_Time_24hr", None)
            Bot.saveData("BC_Schedule_Hour", None)
            Bot.saveData("BC_Schedule_Minute", None)
            raise ReturnCommand()

        # Save scheduled broadcast data
        broadcast_data = {
            "type": msg_type,
            "text": txt,
            "file_id": file_id,
            "channels": Bot.getData("AllMainChTGID") or []
        }
        Bot.saveData("BC_Data", broadcast_data)

        date = Bot.getData("BC_Schedule_Date")
        time_display = Bot.getData("BC_Schedule_Time_12hr")

        # Show confirmation
        msg_id = Bot.getData("BC_MsgID")
        preview = f"""<b>📅 Schedule Broadcast Confirmation</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}
📊 <b>Target:</b> {len(broadcast_data['channels'])} channels
📝 <b>Type:</b> {msg_type.upper()}

🔄 <b>Content:</b>
{txt[:300] if txt else 'No text'}{'...' if txt and len(txt) > 300 else ''}

<i>✅ Click Confirm to schedule</i>
<i>❌ Click Cancel to abort</i>"""

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("✅ Schedule", callback_data="/PIRO_BC_Schedule_Confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="/PIRO_BC_Cancel")
        )

        if msg_id:
            try:
                bot.editMessageText(chat_id=u, message_id=msg_id, text=preview, reply_markup=markup)
            except:
                new_msg = bot.replyText(u, preview, reply_markup=markup)
                Bot.saveData("BC_MsgID", new_msg.message_id)
        else:
            new_msg = bot.replyText(u, preview, reply_markup=markup)
            Bot.saveData("BC_MsgID", new_msg.message_id)
    pass


# ===/PIRO_BC_Schedule_Time===
def cmd_PIRO_BC_Schedule_Time():
    global msg
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if options == None:
        date = Bot.getData("BC_Schedule_Date") or "DD-MM-YYYY"
        msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}

<i>Send the time for broadcast</i>
<b>Format:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Send /cancel to cancel</i>""")
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Schedule_Time", options=True)
        raise ReturnCommand()
    else:
        # This runs when user sends the time
        if message.text == "/cancel":
            msg_id = Bot.getData("BC_MsgID")
            if msg_id:
                try:
                    bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
                except:
                    pass
            Bot.saveData("BC_MsgID", None)
            Bot.saveData("BC_Schedule_Date", None)
            raise ReturnCommand()

        # Validate time format (HH:MM AM/PM or H:MM AM/PM)
        try:
            user_input = message.text

            # Check if message has AM/PM
            if "AM" not in user_input.upper() and "PM" not in user_input.upper():
                bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
                raise ReturnCommand()

            # Split by space to get time and AM/PM
            parts = user_input.upper().split(" ")
            if len(parts) != 2:
                bot.replyText(u, """<b>❌ Invalid format!</b>
Please use format: HH:MM AM/PM
Example: 02:30 PM""")
                raise ReturnCommand()

            time_str = parts[0].strip()
            ampm_str = parts[1].strip()

            if ampm_str not in ["AM", "PM"]:
                bot.replyText(u, "<b>❌ Invalid AM/PM! Use AM or PM</b>")
                raise ReturnCommand()

            # Split time by colon
            time_components = time_str.split(":")
            if len(time_components) != 2:
                bot.replyText(u, "<b>❌ Invalid time format! Use HH:MM</b>")
                raise ReturnCommand()

            hour = int(time_components[0].strip())
            minute = int(time_components[1].strip())

            if hour < 1 or hour > 12:
                bot.replyText(u, "<b>❌ Hour must be between 1-12</b>")
                raise ReturnCommand()
            if minute < 0 or minute > 59:
                bot.replyText(u, "<b>❌ Minute must be between 0-59</b>")
                raise ReturnCommand()

            # Convert to 24-hour format for storage
            if ampm_str == "PM" and hour != 12:
                hour_24 = hour + 12
            elif ampm_str == "AM" and hour == 12:
                hour_24 = 0
            else:
                hour_24 = hour

            # Format the display time with leading zero if needed
            display_time = f"{hour:02d}:{minute:02d} {ampm_str}"

            # Store both formats
            Bot.saveData("BC_Schedule_Time_12hr", display_time)
            Bot.saveData("BC_Schedule_Time_24hr", f"{hour_24:02d}:{minute:02d}")
            Bot.saveData("BC_Schedule_Hour", hour_24)
            Bot.saveData("BC_Schedule_Minute", minute)

            # Check if time is in future (if date is today)
            date = Bot.getData("BC_Schedule_Date")
            now = libs.DateAndTime.now("Asia/Kolkata")
            current_date = now["date"]
            current_time = now["time"][:5]

            # Parse stored date (DD-MM-YYYY)
            date_parts = date.split("-")
            sched_day = int(date_parts[0])
            sched_month = int(date_parts[1])
            sched_year = int(date_parts[2])

            # Parse current date (YYYY-MM-DD)
            curr_parts = current_date.split("-")
            curr_year = int(curr_parts[0])
            curr_month = int(curr_parts[1])
            curr_day = int(curr_parts[2])

            # Check if scheduled date is today
            is_today = (sched_year == curr_year and sched_month == curr_month and sched_day == curr_day)

            if is_today:
                curr_hour = int(current_time.split(":")[0])
                curr_min = int(current_time.split(":")[1])

                if hour_24 < curr_hour or (hour_24 == curr_hour and minute <= curr_min):
                    bot.replyText(u, f"""<b>❌ Please select a future time!</b>

⏰ <b>Current Time:</b> {current_time}
⏰ <b>You entered:</b> {display_time}

<i>Please enter a time after current time</i>""")
                    raise ReturnCommand()

            # Ask for message
            date = Bot.getData("BC_Schedule_Date")
            time_display = Bot.getData("BC_Schedule_Time_12hr")

            msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}

<i>Now send your message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")

            Bot.saveData("BC_MsgID", msg.message_id)
            Bot.handleNextCommand("/PIRO_BC_Schedule_Message", options=True)

        except ValueError:
            bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
            raise ReturnCommand()
        except:
            bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
            raise ReturnCommand()
    pass


# ===/PIRO_Broadcast===
def cmd_PIRO_Broadcast():
    global msg
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    # Get all channels
    AMCTGID = Bot.getData("AllMainChTGID") or []
    if not AMCTGID:
        bot.replyText(u, "<b>❌ No Channels Added Yet!</b>")
        raise ReturnCommand()

    # Ask for message
    msg = bot.replyText(u, f"""<b>📢 Broadcast to {len(AMCTGID)} Channels</b>

<i>Send any message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")

    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Process", options=True)
    pass


# ===/PIRO_ChLinks===
def cmd_PIRO_ChLinks():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    AMCL=Bot.getData("AllMainChlink") or []

    Links=""
    for L in AMCL:
        Links=f"{Links}\n{L}"

    if Links!="":
        bot.sendMessage(Links)
    else:
        bot.sendMessage("No Links Found")
    pass


# ===/PIRO_Chanel_Pannel===
def cmd_PIRO_Chanel_Pannel():
    # Channels + Social links + Uncheck channels in ONE list, all movable up/down together.
    # params: setLink <id> | delete <id> | UP <id> | DOWN <id> | info <id>
    #         DelSocial <i> | UpSocial <i> | DownSocial <i> | SocialInfo <i> | SocialBad <i>

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()

    OWNER = str(Bot.getData("Owner") or "1294066842")


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    def toast(t, alert=False):
        try:
            bot.answerCallbackQuery(call.id, t, show_alert=alert)
        except:
            pass


    def clean_url(raw):
        s = str(raw or "").strip().strip("<>").strip()
        if not s or " " in s or "\n" in s or len(s) > 500 or s.startswith("/"):
            return None
        low = s.lower()
        if s.startswith("@"):
            un = s[1:]
            if len(un) < 4 or len(un) > 32:
                return None
            for ch in un:
                if not (ch.isalnum() or ch == "_"):
                    return None
            return "https://t.me/" + un
        if low.startswith("t.me/") or low.startswith("telegram.me/"):
            s = "https://" + s
            low = s.lower()
        if low.startswith("tg://"):
            return s if len(s) > 8 else None
        if low.startswith("https://") or low.startswith("http://"):
            rest = s.split("://", 1)[1]
            host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            if "." not in host or host.startswith(".") or host.endswith(".") or len(host) < 4:
                return None
            for ch in host:
                if not (ch.isalnum() or ch in ".-:"):
                    return None
            return s
        return None


    def log(act):
        now = libs.DateAndTime.now("Asia/Kolkata")
        AdmAC = Bot.getData("AdmAC") or []
        AdmAC.append(
            f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
            f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
            f"🔍 <b>Action:</b> {act}"
        )
        Bot.saveData("AdmAC", AdmAC)


    # any pending input is cancelled when the panel is opened
    User.saveData("ch_wait", "")

    AMC = Bot.getData("AllMainCh") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []
    SocialLinks = Bot.getData("AllSocialLinks") or []

    # keep all channel lists the same length (protects against index errors)
    n = len(AMCTGID)
    changed = False
    for _k in range(len(AMC), n):
        AMC.append(str(AMCTGID[_k]))
        changed = True
    for _k in range(len(AMCU), n):
        AMCU.append(str(AMCTGID[_k]))
        changed = True
    for _k in range(len(AMCL), n):
        AMCL.append("None")
        changed = True

    # remove bad social entries
    good_social = []
    for s in SocialLinks:
        try:
            if clean_url(s.get("url")) and str(s.get("name", "")).strip():
                good_social.append(s)
        except:
            pass
    if len(good_social) != len(SocialLinks):
        SocialLinks = good_social
        Bot.saveData("AllSocialLinks", SocialLinks)

    # auto-repair invalid invite links using the public username
    fixed = 0
    bad_links = 0
    for k in range(len(AMCL)):
        if not clean_url(AMCL[k]):
            uname = str(AMCU[k])
            if uname.startswith("@") and len(uname) > 4:
                AMCL[k] = "https://t.me/" + uname[1:]
                fixed += 1
                changed = True
            else:
                bad_links += 1

    if changed:
        Bot.saveData("AllMainCh", AMC)
        Bot.saveData("AllMainChlink", AMCL)
        Bot.saveData("AllMainChUsernm", AMCU)
        Bot.saveData("AllMainChTGID", AMCTGID)


    def find_idx(cid):
        for k in range(len(AMCTGID)):
            if str(AMCTGID[k]) == str(cid):
                return k
        return -1


    def save_channels():
        Bot.saveData("AllMainCh", AMC)
        Bot.saveData("AllMainChlink", AMCL)
        Bot.saveData("AllMainChUsernm", AMCU)
        Bot.saveData("AllMainChTGID", AMCTGID)


    def get_order():
        valid = []
        for x in AMCTGID:
            valid.append("c:" + str(x))
        for s in SocialLinks:
            valid.append("s:" + str(s.get("url")))
        cur = Bot.getData("AllLinkOrder") or []
        order = []
        for t in cur:
            if t in valid and t not in order:
                order.append(t)
        for t in valid:
            if t not in order:
                order.append(t)
        return order


    def sync_channels(order):
        # keep the channel lists in the same relative order as the shared list
        ids = []
        for t in order:
            if t.startswith("c:"):
                ids.append(t[2:])
        pos = {}
        for k in range(len(AMCTGID)):
            pos[str(AMCTGID[k])] = k
        new = []
        for i in ids:
            if i in pos:
                new.append(pos[i])
        if len(new) == len(AMCTGID):
            for L in (AMC, AMCU, AMCL, AMCTGID):
                old = list(L)
                for a in range(len(new)):
                    L[a] = old[new[a]]
            save_channels()


    def move_token(token, direction):
        order = get_order()
        if token not in order:
            return False
        idx = order.index(token)
        j = idx - 1 if direction == "UP" else idx + 1
        if j < 0 or j >= len(order):
            toast("Already at the " + ("top" if direction == "UP" else "bottom"))
            return False
        order[idx], order[j] = order[j], order[idx]
        Bot.saveData("AllLinkOrder", order)
        sync_channels(order)
        return True


    P = []
    _src = params if params is not None else options
    if _src is not None:
        P = str(_src).strip().split()
    if P and P[0] == "None":
        P = []

    action = P[0] if P else ""
    arg = P[1] if len(P) > 1 else ""

    mid = message.message_id

    # ---------------- ACTIONS ----------------
    if action == "setLink" and arg:
        idx = find_idx(arg)
        if idx < 0:
            toast("⚠️ Channel not found", True)
            raise ReturnCommand()

        DC = Bot.getData(str(arg) + "isDef") or "N"
        if DC == "Y" and str(u) != OWNER:
            toast("😖 This is a Permanent Channel", True)
            raise ReturnCommand()

        User.saveData("ch_wait", "link")
        User.saveData("ch_id", str(arg))
        User.saveData("ch_panel", mid)

        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

        show(
            mid,
            "<b>Set Invite Link — " + esc(AMCU[idx]) + "</b>\n\n"
            "Send the invite link (https://t.me/+xxxx) for this channel.\n\n"
            "<i>You can also send a public link (t.me/username) to reset a wrong one.</i>",
            mk
        )
        Bot.handleNextCommand("/PIRO_Chanel_Pannel1", options=str(arg), cancel_at_command=True)
        raise ReturnCommand()

    if action == "delete" and arg:
        idx = find_idx(arg)
        DC = Bot.getData(str(arg) + "isDef") or "N"
        if DC == "Y" and str(u) != OWNER:
            toast("😖 This is a Permanent Channel", True)
            raise ReturnCommand()
        if idx >= 0:
            nm = AMCU[idx]
            for L in (AMC, AMCU, AMCL, AMCTGID):
                L.pop(idx)
            save_channels()
            log("Channel (" + str(nm) + ") Removed")
            toast("✅ Channel Removed")

    if action == "JoinMode":
        _cur = Bot.getData("JoinMode") or "NOT"
        _new = "NOT" if _cur == "ALL" else "ALL"
        Bot.saveData("JoinMode", _new)
        log("Join mode set to " + ("All Channels" if _new == "ALL" else "Only Not Joined"))
        toast("🎛 " + ("Showing all channels" if _new == "ALL" else "Showing only not-joined channels"))

    if action in ["UP", "DOWN"] and arg:
        move_token("c:" + str(arg), action)

    if action in ["DelSocial", "UpSocial", "DownSocial"] and arg.isdigit():
        i = int(arg)
        if 0 <= i < len(SocialLinks):
            if action == "DelSocial":
                nm = SocialLinks[i].get("name")
                SocialLinks.pop(i)
                Bot.saveData("AllSocialLinks", SocialLinks)
                log("Link (" + str(nm) + ") Removed")
                toast("✅ Link Removed")
            else:
                move_token("s:" + str(SocialLinks[i].get("url")), "UP" if action == "UpSocial" else "DOWN")

    if action == "SocialInfo" and arg.isdigit():
        i = int(arg)
        if 0 <= i < len(SocialLinks):
            s = SocialLinks[i]
            kind = "Uncheck Channel" if s.get("type") == "uncheck" else "Social Link"
            mk = InlineKeyboardMarkup()
            mk.add(InlineKeyboardButton(text="🗑 Remove", callback_data="/PIRO_Chanel_Pannel DelSocial " + str(i)))
            mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))
            show(
                mid,
                "<b>💡 " + kind + " Info</b>\n\n"
                "🏷 <b>Name:</b> " + esc(s.get("name")) + "\n"
                "<b>Link:</b> <code>" + esc(s.get("url")) + "</code>\n\n"
                "<i>Users are not checked for this link.</i>",
                mk
            )
            raise ReturnCommand()

    if action == "SocialBad":
        toast("⚠️ This link is invalid. Remove it with ❌ and add again.", True)
        raise ReturnCommand()

    if action == "info" and arg:
        idx = find_idx(arg)
        if idx < 0:
            toast("⚠️ Channel not found", True)
            raise ReturnCommand()

        DC = Bot.getData(str(arg) + "isDef") or "N"
        JR = Bot.getData(str(arg) + "JoinReq") or "Y"
        L = AMCL[idx]
        link_txt = "<code>" + esc(L) + "</code>" if clean_url(L) else "⚠️ <b>Invalid / not set</b> — tap the channel name to set it"

        txt = (
            "<b>💡 Channel Info</b>\n\n"
            "🆔 <b>Chat:</b> <code>" + esc(AMCU[idx]) + "</code>\n"
            "🖇 <b>Invite Link:</b> " + link_txt
        )
        if DC == "Y":
            txt += "\n🔎 <b>Join Check:</b> " + ("🟢 ON" if JR == "Y" else "🔴 OFF")

        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton(text="Set Invite Link", callback_data="/PIRO_Chanel_Pannel setLink " + str(arg)))
        if DC == "Y":
            mk.row(
                InlineKeyboardButton(text="🟢 CHECK", callback_data="/JoinReqSt " + str(arg) + " Y"),
                InlineKeyboardButton(text="🔴 NO CHECK", callback_data="/JoinReqSt " + str(arg) + " N")
            )
        mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

        show(mid, txt, mk)
        raise ReturnCommand()

    # ---------------- RENDER PANEL ----------------
    order = get_order()
    if order != (Bot.getData("AllLinkOrder") or []):
        Bot.saveData("AllLinkOrder", order)

    keyboard = []

    for t in order:
        if t.startswith("c:"):
            k = find_idx(t[2:])
            if k < 0:
                continue
            cid = str(AMCTGID[k])
            nm = str(AMCU[k]) or str(AMC[k])
            keyboard.append([
                {"text": nm[:14], "callback_data": "/PIRO_Chanel_Pannel setLink " + cid},
                {"text": "❌", "callback_data": "/PIRO_Chanel_Pannel delete " + cid},
                {"text": "🔼", "callback_data": "/PIRO_Chanel_Pannel UP " + cid},
                {"text": "🔽", "callback_data": "/PIRO_Chanel_Pannel DOWN " + cid},
                {"text": "👀", "callback_data": "/PIRO_Chanel_Pannel info " + cid}
            ])
        else:
            surl = t[2:]
            i = -1
            for q in range(len(SocialLinks)):
                if str(SocialLinks[q].get("url")) == surl:
                    i = q
                    break
            if i < 0:
                continue
            s = SocialLinks[i]
            url = clean_url(s.get("url"))
            icon = ""
            if url:
                first = {"text": icon + str(s.get("name"))[:14], "url": url}
            else:
                first = {"text": "⚠️ " + str(s.get("name"))[:14], "callback_data": "/PIRO_Chanel_Pannel SocialBad " + str(i)}
            keyboard.append([
                first,
                {"text": "❌", "callback_data": "/PIRO_Chanel_Pannel DelSocial " + str(i)},
                {"text": "🔼", "callback_data": "/PIRO_Chanel_Pannel UpSocial " + str(i)},
                {"text": "🔽", "callback_data": "/PIRO_Chanel_Pannel DownSocial " + str(i)},
                {"text": "👀", "callback_data": "/PIRO_Chanel_Pannel SocialInfo " + str(i)}
            ])

    keyboard.append([{"text": "➕ Add Channel", "callback_data": "/PIRO_AddCh"}])
    keyboard.append([{"text": "➕ Add Social Link", "callback_data": "/social"}])
    keyboard.append([{"text": "➕ Add Uncheck Channel", "callback_data": "/uncheck"}])
    keyboard.append([{"text": "📊 How many users joined?", "callback_data": "/ChStats"}])
    keyboard.append([{"text": "📡 Channel Broadcast", "callback_data": "/CHANNEL_BC"}])
    keyboard.append([{"text": "🎛 Join Mode: " + ("All Channels" if Bot.getData("JoinMode") == "ALL" else "Only Not Joined"), "callback_data": "/PIRO_Chanel_Pannel JoinMode"}])

    keyboard.append([{"text": "🔙 Back", "callback_data": "/admin AP"}])

    n_social = 0
    n_uncheck = 0
    for s in SocialLinks:
        if s.get("type") == "uncheck":
            n_uncheck += 1
        else:
            n_social += 1

    TXT = (
        "<b>⚡ Manage Channels & Links</b>\n\n"
        "📢 <b>Channels (join check):</b> " + str(len(AMCTGID)) + "\n"
        "<b>Social links:</b> " + str(n_social) + "\n"
        "<b>Uncheck channels:</b> " + str(n_uncheck) + "\n"
        "🎛 <b>Join mode:</b> " + ("All channels shown" if Bot.getData("JoinMode") == "ALL" else "Only not-joined channels shown") + "\n\n"
        "<i>Tap a channel to set its invite link • 👀 info • 🔼🔽 change order (all types move together) • ❌ remove.</i>"
    )

    if fixed:
        TXT += "\n\n<b>Auto-fixed " + str(fixed) + " invalid invite link(s).</b>"
    if bad_links:
        TXT += "\n\n<b>" + str(bad_links) + " channel(s) have no valid invite link.</b> Tap the channel name to set one; users won't see a Join button for it until then."

    show(mid, TXT, {"inline_keyboard": keyboard})
    raise ReturnCommand()
    pass


# ===/PIRO_Chanel_Pannel1===
def cmd_PIRO_Chanel_Pannel1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1
    # options = channel id whose invite link is being set

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    # admin already pressed Back / left this screen -> ignore
    if str(User.getData("ch_wait")) != "link":
        raise ReturnCommand()


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    def clean_url(raw):
        s = str(raw or "").strip().strip("<>").strip()
        if not s or " " in s or "\n" in s or len(s) > 500 or s.startswith("/"):
            return None
        low = s.lower()
        if s.startswith("@"):
            un = s[1:]
            if len(un) < 4 or len(un) > 32:
                return None
            for ch in un:
                if not (ch.isalnum() or ch == "_"):
                    return None
            return "https://t.me/" + un
        if low.startswith("t.me/") or low.startswith("telegram.me/"):
            s = "https://" + s
            low = s.lower()
        if low.startswith("tg://"):
            return s if len(s) > 8 else None
        if low.startswith("https://") or low.startswith("http://"):
            rest = s.split("://", 1)[1]
            host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            if "." not in host or host.startswith(".") or host.endswith(".") or len(host) < 4:
                return None
            for ch in host:
                if not (ch.isalnum() or ch in ".-:"):
                    return None
            return s
        return None


    raw = str(message.text or "").strip()

    # commands are never links (this was the "/admin" URL bug)
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    cid = str(options or User.getData("ch_id") or "")
    panel = User.getData("ch_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    # premium (custom) Telegram emoji are not supported
    _premium = False
    try:
        for _e in (message.entities or []):
            _t = None
            try:
                _t = _e.type
            except:
                try:
                    _t = _e["type"]
                except:
                    _t = None
            if str(_t) == "custom_emoji":
                _premium = True
    except:
        pass

    if _premium:
        show(panel, "<b>❌ Invalid input.</b>\n\nSend the plain link only.", back)
        Bot.handleNextCommand("/PIRO_Chanel_Pannel1", options=cid, cancel_at_command=True)
        raise ReturnCommand()

    link = clean_url(raw)

    if link is None:
        show(
            panel,
            "<b>❌ Invalid link!</b>\n\nSend a valid link like:\n<code>https://t.me/+AbCdEf123</code>\n<code>https://t.me/channelname</code>",
            back
        )
        Bot.handleNextCommand("/PIRO_Chanel_Pannel1", options=cid, cancel_at_command=True)
        raise ReturnCommand()

    AMC = Bot.getData("AllMainCh") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    idx = -1
    for k in range(len(AMCTGID)):
        if str(AMCTGID[k]) == cid:
            idx = k
            break

    if idx < 0 or idx >= len(AMCL):
        User.saveData("ch_wait", "")
        mk = InlineKeyboardMarkup()
        mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))
        show(panel, "<b>⚠️ Channel not found.</b>", mk)
        raise ReturnCommand()

    old = AMCL[idx]
    AMCL[idx] = link
    Bot.saveData("AllMainChlink", AMCL)
    User.saveData("ch_wait", "")

    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> Invite Link ({old}) changed to ({link}) in Bot Channel ({cid})"
    )
    Bot.saveData("AdmAC", AdmAC)

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(panel, "<b>✅ Invite Link Updated</b>\n\n📢 " + esc(AMCU[idx]) + "\n<code>" + esc(link) + "</code>", mk)
    raise ReturnCommand()
    pass


# ===/PIRO_LeadSet===
def cmd_PIRO_LeadSet():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()



    markup = InlineKeyboardMarkup()

    markup.add(InlineKeyboardButton(text='Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ Tᴇxʀ',callback_data='/PIRO_LeadSet Text'))

    markup.add(InlineKeyboardButton(text='Bᴀɴ Fʀᴏᴍ Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ',callback_data='/PIRO_LeadSet Ban'))

    markup.add(InlineKeyboardButton(text='Uɴʙᴀɴ Fʀᴏᴍ Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ',callback_data='/PIRO_LeadSet UnBan'))


    markup.add(InlineKeyboardButton(text='Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ Sɪᴢᴇ',callback_data='/PIRO_LeadSet Size'))

    markup.add(InlineKeyboardButton(text='🔙 Bᴀᴄᴋ',callback_data='/admin AP'))


    if str(params) !="None":
        P=params.split(" ")
        if P[0]=="Text":
            bot.replyText(u,f"""<b>Send New Leaderboard Text</b>""",parse_mode = "html")

        if P[0]=="Ban":
            bot.replyText(u,f"""<b>Send UserId to Ban From Leaderboard</b>""",parse_mode = "html")

        if P[0]=="UnBan":
            bot.replyText(u,f"""<b>Send UserId to UnBan From Leaderboard</b>""",parse_mode = "html")

        if P[0]=="Size":
            bot.replyText(u,f"""<b>Send The Numbers Of Winners to Show In Leaderboard</b>""",parse_mode = "html")

        Bot.handleNextCommand("/PIRO_LeadSet1",options=P[0])
        raise ReturnCommand()



    TXT=f"""<b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴏᴅɪғʏ Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ Sᴘᴇᴄɪғɪᴄᴀᴛɪᴏɴs </b>"""

    try:
        bot.editMessageText(chat_id=u, message_id=message.message_id,text=TXT,reply_markup=markup)
    except:
        pass
    pass


# ===/PIRO_LeadSet1===
def cmd_PIRO_LeadSet1():
    op=options

    if message.text == "Cancel":
        raise ReturnCommand()



    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


    AdmAC=Bot.getData("AdmAC") or []









    if op=="Text":
        bot.replyText(u,f"""<b>Leaderboard has been changed</b>""",parse_mode = "html")
        Bot.saveData("LdrbrdTxt",message.text)

        act=f"Leaderboard Text updated to {message.text}"
        AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

        Bot.saveData("AdmAC",AdmAC)
        raise ReturnCommand()

    if op=="Ban":
        bot.replyText(u,f"""<b>{message.text} Banned From Leaderboard</b>""",parse_mode = "html")
        Bot.saveData("LdrbrdBanUsr",message.text)

        act=f"Banned {message.text} from Leaderboard"
        AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

        Bot.saveData("AdmAC",AdmAC)
        raise ReturnCommand()

    if op=="UnBan":
        bot.replyText(u,f"""<b>{message.text} Unbanned From Leaderboard</b>""",parse_mode = "html")
        Bot.deleteData("LdrbrdBanUsr")
        act=f"UnBanned {message.text} from Leaderboard"
        AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

        Bot.saveData("AdmAC",AdmAC)    
        raise ReturnCommand()

    if op=="Size":
        bot.replyText(u,f"""<b>Leaderboard Size Changed to {message.text}</b>""",parse_mode = "html")
        Bot.saveData("LdrbrdSize",message.text)
        act=f"Leaderboard Size Updated To {message.text} "
        AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

        Bot.saveData("AdmAC",AdmAC)
        raise ReturnCommand()
    pass


# ===/PIRO_MainMenu===
def cmd_PIRO_MainMenu():
    # --- Referral & First-time check ---
    dn = User.getData("dn") or "n"
    if dn == "n":
        refBy = Bot.getData(str(u) + "Referral") or "None"
        if str(refBy) == str(u):
            refBy = "@not_referred"
            Bot.saveData(str(u) + "Referral", "@not_referred")
        RefJC = Bot.getData(str(refBy) + "RefJC") or 0
        Bot.saveData(str(refBy) + "RefJC", RefJC + 1)
        User.saveData("dn", "y")

    # ============================================================
    # 📱 CONTACT VERIFICATION FIRST
    # ============================================================

    VerifySystemStatus = Bot.getData(
        "VerifySystemStatus"
    ) or "OFF"

    if VerifySystemStatus == "ON":

        if not User.getData("is_verified"):

            Bot.runCommand(
                "/shareprofile"
            )

            raise ReturnCommand()


    # ============================================================
    # 🔐 CAPTCHA + MANUAL APPROVAL VERIFICATION
    # ============================================================

    iisApproved = Bot.getData(
        str(u) + "ManualAproval"
    ) or "N"

    UsrPhVerif = User.getData(
        "PhVerification"
    )

    Captcha = Bot.getData(
        "CaptchaMode"
    ) or "ON"

    verify = User.getData(
        "verify"
    )

    if Captcha == "ON":

        if verify != "ok":

            # ✅ Phone verified OR manually approved = allow
            if UsrPhVerif == "Verified" or iisApproved == "Y":
                pass

            else:
                Bot.runCommand(
                    "/PIRO_Verification"
                )

                raise ReturnCommand()


    # --- Menu Function ---
    def PIRO_Menu():

        # --- Get saved link ---
        saved_link = Bot.getData("SavedLinks")

        # --- Get owner ID (save once with: Bot.saveData("Owner", 123456789)) ---
        owner_id = Bot.getData("Owner")
        me = HTTP.get(
            "https://api.telegram.org/bot"
            + bot_token +
            "/getMe").json()

        bot_name = me["result"]["first_name"]


        # --- Decide final link ---
        if saved_link:
            final_link = saved_link
        elif owner_id:
            final_link = f"tg://openmessage?user_id={owner_id}"
        else:
            final_link = "https://t.me/"   # fallback if nothing set

        # --- Welcome text ---
        HHT = (
            f"🏡 <b>Welcome To {bot_name} Wallet Bot!</b>\n\n"
        )

        if options:   # override if a custom message is passed
            HHT = options

        # --- Keyboard menu ---
        rows = [
            ["Balance", "Refer Earn"],
            ["Bonus", "Withdraw"],
            ["Link Wallet"]
        ]

        keyboard = {
            "keyboard": rows,
            "resize_keyboard": True
        }

        bot.sendMessage(
            HHT,
            chat_id=message.chat.id,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True
        )


    # --- Show Menu ---
    PIRO_Menu()


    # --- Referral Logic ---
    is_invited = User.getData("is_invited")
    ref = Bot.getData(str(u) + "Referral") or "None"
    perRef = Bot.getData("PerRefer") or "0"   # string format support

    if is_invited is None:

        if ref not in ("None", "@not_referred") and str(ref).lstrip("-").isdigit() and str(ref) != str(u):

            sameDev = Bot.getData(str(u) + "sameDev") or "no"

            if sameDev == "no":

                RefCount = Bot.getData(str(ref) + "RefCount") or 0
                Bot.saveData(str(ref) + "RefCount", int(RefCount + 1))

                usr = f"""<a href="tg://user?id={str(u)}">{str(u)}</a>"""

                # --- Decide Referral Bonus ---
                bonus = 0

                if "-" in str(perRef):

                    try:
                        start, end = map(int, perRef.split("-"))

                        # ✅ Built-in random (no import)
                        bonus = libs.Random.randomInt(start,end)

                    except:
                        bonus = 0

                else:

                    try:
                        bonus = int(perRef)

                    except:
                        bonus = 0

                # --- Add bonus to referrer ---
                if bonus > -1:

                    libs.Resources.anotherRes('Balance', user=ref).add(bonus)

                    try:
                        bot.replyText(
                            chat_id=ref,
                            text=f"""<b>{usr} Got Invited By Your Url: +{bonus} Rs </b>""",
                            parse_mode="HTML"
                        )

                    except:
                        pass

        # --- Count verified users ---
        T_verifUsrs = Bot.getData('T_verifUsrs') or 0
        Bot.saveData('T_verifUsrs', T_verifUsrs + 1)

        User.saveData("is_invited", True)
    pass


# ===/PIRO_RC_Pannel===
def cmd_PIRO_RC_Pannel():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(userid) for userid in AllBotAdminss]:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GIFT CODES
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []

    All_BRC = [str(RC) for RC in All_BRC]

    markup = InlineKeyboardMarkup()


    # ==============================
    # NO GIFT CODES
    # ==============================

    if not All_BRC:

        markup.add(
            InlineKeyboardButton(
                text="➕ Cʀᴇᴀᴛᴇ Nᴇᴡ Gɪғᴛ Cᴏᴅᴇ",
                callback_data="/creating_NwRC"
            )
        )

    else:

        # ==============================
        # GIFT CODE LIST
        # ==============================

        for RC in All_BRC:

            Status = Bot.getData(
                "Gift" + RC + "KaStatus"
            ) or "Active"

            StatusEmoji = "🟢" if Status == "Active" else "🔴"


            markup.row(
                InlineKeyboardButton(
                    text=RC,
                    callback_data="/RC_Change info_" + RC
                ),

                InlineKeyboardButton(
                    text="🗑",
                    callback_data="/RC_Change delete_" + RC
                ),

                InlineKeyboardButton(
                    text=StatusEmoji,
                    callback_data="/RC_Change " + StatusEmoji + "_" + RC
                )
            )


        # ==============================
        # CREATE NEW
        # ==============================

        markup.add(
            InlineKeyboardButton(
                text="➕ Cʀᴇᴀᴛᴇ Nᴇᴡ",
                callback_data="/creating_NwRC"
            )
        )


    # ==============================
    # CLAIMED TEXT
    # ==============================

    markup.add(
        InlineKeyboardButton(
            text="📝 Cʟᴀɪᴍᴇᴅ Tᴇxᴛ",
            callback_data="/RC_claimedText"
        )
    )


    # ==============================
    # BACK
    # ==============================

    markup.add(
        InlineKeyboardButton(
            text="🔙 Bᴀᴄᴋ",
            callback_data="/admin AP"
        )
    )


    # ==============================
    # PANEL TEXT
    # ==============================

    TXT = f"""<b>🎁 Gɪғᴛ Cᴏᴅᴇ Mᴀɴᴀɢᴇʀ

📊 Tᴏᴛᴀʟ Gɪғᴛ Cᴏᴅᴇs: {len(All_BRC)}

👇 Sᴇʟᴇᴄᴛ A Gɪғᴛ Cᴏᴅᴇ Tᴏ Mᴀɴᴀɢᴇ Iᴛ.</b>"""


    # ==============================
    # EDIT CURRENT MESSAGE
    # ==============================

    try:

        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    except:

        bot.replyText(
            u,
            TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )


    raise ReturnCommand()
    pass


# ===/PIRO_ScheduleBroadcast===
def cmd_PIRO_ScheduleBroadcast():
    global msg
    # Admin Check
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    AMCTGID = Bot.getData("AllMainChTGID") or []
    if not AMCTGID:
        bot.replyText(u, "<b>❌ No Channels Added Yet!</b>")
        raise ReturnCommand()

    # Ask for date
    msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast to {len(AMCTGID)} Channels</b>

<i>Send the date for broadcast</i>
<b>Format:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Send /cancel to cancel</i>""")

    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Schedule_Date", options=True)
    pass


# ===/PIRO_Verification===
def cmd_PIRO_Verification():
    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()
    me = HTTP.get(
            "https://api.telegram.org/bot"
            + bot_token +
            "/getMe").json()

    bot_name = me["result"]["first_name"]
    Captcha = Bot.getData("CaptchaMode") or "ON"

    if Captcha == "OFF":

        # ================= REFERRAL CREDIT =================
        User.deleteData("verify")   # ✅ Yeh line important hai

        # Ab menu dikhao
        saved_link = Bot.getData("SavedLinks")
        owner_id = Bot.getData("Owner")
        if saved_link:
            final_link = saved_link
        elif owner_id:
            final_link = f"tg://openmessage?user_id={owner_id}"
        else:
            final_link = "https://t.me/"

        HHT = (f"🏡 <b>Welcome To {bot_name} Wallet Bot!</b>")

        keyboard = {
            "keyboard": [
                ["Balance" , "Refer Earn"],
                ["Bonus" , "Withdraw"],
                ["Link Wallet"]
            ],
            "resize_keyboard": True
        }

        bot.sendMessage(
            HHT,
            chat_id=message.chat.id,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        raise ReturnCommand()

    # ================= VERIFY STATUS =================
    verify = User.getData("verify")

    if verify == "ok":
        # Agar verify "ok" hai toh menu dikhao
        saved_link = Bot.getData("SavedLinks")
        owner_id = Bot.getData("Owner")
        if saved_link:
            final_link = saved_link
        elif owner_id:
            final_link = f"tg://openmessage?user_id={owner_id}"
        else:
            final_link = "https://t.me/"

        HHT = (f"🏡 <b>Welcome To {bot_name} Wallet Bot!</b>")

        keyboard = {
            "keyboard": [
                ["Balance" , "Refer Earn"],
                ["Bonus" , "Withdraw"],
                ["Link Wallet"]
            ],
            "resize_keyboard": True
        }

        bot.sendMessage(
            HHT,
            chat_id=message.chat.id,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        raise ReturnCommand()




    # ================= HASH GENERATE =================
    random_hash = User.getData(
        "user_hash"
    )

    if random_hash is None:

        chars = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789"
        )

        random_hash = ""

        for i in range(32):

            random_hash += libs.Random.randomWeightedChoice(
                chars,
                [1] * len(chars)
            )

        User.saveData(
            "user_hash",
            random_hash
        )


    # ================= BOT USERNAME =================
    me = HTTP.get(
        "https://api.telegram.org/bot"
        + bot_token +
        "/getMe"
    ).json()

    BOT_USERNAME = me["result"]["username"]


    # ================= WEBHOOK URL =================
    webhook = libs.Webhook.getUrlFor(
        command="/onWebhook",
        user_id=u
    )

    webhook_encoded = rawurlencode(
        webhook
    )


    # ================= WEBAPP URL =================
    WEBAPP_URL = (
        "https://webapp-pi-teal.vercel.app/fire.html"
        f"?botHash={random_hash}"
        f"&bot={BOT_USERNAME}"
        f"&webhook={webhook_encoded}"

    )

    # ================= SEND VERIFY =================
    verify_msg = bot.sendMessage(
        """
<b>🔐 Verify YourSelf First To Start Bot.</b>
""",
        parse_mode="HTML",
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "Verify Now",
                        "web_app": {
                            "url": WEBAPP_URL
                        }
                    }
                ]
            ]
        }
    )

    # Save verification message ID
    User.saveData(
        "verify_msg_id",
        verify_msg["message_id"]
    )

    raise ReturnCommand()
    pass


# ===/PIRO_Verification0===
def cmd_PIRO_Verification0():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()


    verify = User.getData("verifyYYY")
    if verify:
      bot.sendMessage("🙄Already verified")
      raise ReturnCommand()

    webhook = libs.Webhook.getUrlFor(command="/onWebhook", user_id=u)

    BOTID = str(bot.info().token).split(":")[0]

    webPage="https://api.jobians.top/telegram/verify?webhookUrl="+str(encodeURIComponent(webhook))+"&botId="+BOTID

    keys = [[InlineKeyboardButton(text='Verify', web_app=webPage)]]
    button = InlineKeyboardMarkup(keys)

    bot.sendMessage("<b>🔐 Verify Yourself To Start Bot</b>", reply_markup=button)
    pass


# ===/PIRO_ban===
def cmd_PIRO_ban():
    # 12'07'25"00'06'18  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()



    bot.replyText(u,"<b>📢 Sᴇɴᴅ Usᴇʀ Tᴇʟᴇɢʀᴀᴍ Iᴅ Tᴏ Bᴀɴ Iɴ Bᴏᴛ</b>""",parse_mode = "html")

    Bot.handleNextCommand("/PIRO_ban1")
    pass


# ===/PIRO_ban1===
def cmd_PIRO_ban1():
    # 12'07'25"00'06'18  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    bot.replyText(u,f"""<b> Bᴀɴɴᴇᴅ Sᴜᴄᴄᴇss: {message.text} </b>""",parse_mode = "html")

    Bot.saveData(f"{message.text}ban",True)
    pass


# ===/PIRO_broadcast1===
def cmd_PIRO_broadcast1():
    # 19'4'25 00'11'10 v:1'0'0 [√]
    # Premium Entity Broadcast

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
            break

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
        raise ReturnCommand()

    if message.text == "/cancel":
        bot.sendMessage("<b>❌ Cancelled</b>", parse_mode="html")
        raise ReturnCommand()

    broadcast_data = None

    is_forwarded = False
    try:
        if message.forward_from or message.forward_from_chat or message.forward_date:
            is_forwarded = True
    except:
        is_forwarded = False

    if is_forwarded:
        broadcast_data = {
            "function": "forward_message",
            "from_chat_id": message.chat.id,
            "message_id": message.message_id
        }

    elif message.photo:
        broadcast_data = {
            "function": "send_photo",
            "photo": message.photo[-1].file_id
        }

        if message.caption:
            broadcast_data["caption"] = message.caption
            try:
                if message.caption_entities:
                    broadcast_data["caption_entities"] = message.caption_entities
            except:
                pass

    elif message.video:
        broadcast_data = {
            "function": "send_video",
            "video": message.video.file_id
        }

        if message.caption:
            broadcast_data["caption"] = message.caption
            try:
                if message.caption_entities:
                    broadcast_data["caption_entities"] = message.caption_entities
            except:
                pass

    elif message.document:
        broadcast_data = {
            "function": "send_document",
            "document": message.document.file_id
        }

        if message.caption:
            broadcast_data["caption"] = message.caption
            try:
                if message.caption_entities:
                    broadcast_data["caption_entities"] = message.caption_entities
            except:
                pass

    elif message.audio:
        broadcast_data = {
            "function": "send_audio",
            "audio": message.audio.file_id
        }

        try:
            if message.caption:
                broadcast_data["caption"] = message.caption
                if message.caption_entities:
                    broadcast_data["caption_entities"] = message.caption_entities
        except:
            pass

    elif message.sticker:
        broadcast_data = {
            "function": "send_sticker",
            "sticker": message.sticker.file_id
        }

    elif message.animation:
        broadcast_data = {
            "function": "send_animation",
            "animation": message.animation.file_id
        }

        try:
            if message.caption:
                broadcast_data["caption"] = message.caption
                if message.caption_entities:
                    broadcast_data["caption_entities"] = message.caption_entities
        except:
            pass

    elif message.text:
        broadcast_data = {
            "function": "send_message",
            "text": message.text
        }

        try:
            if message.entities:
                broadcast_data["entities"] = message.entities
        except:
            pass

    else:
        bot.sendMessage("<b>❌ Unsupported File Format!</b>", parse_mode="html")
        raise ReturnCommand()

    User.saveData("broadcast_data", broadcast_data)

    FulUsrs = Bot.getData("FulBotUsrs") or []

    keyboard = [
        [{"text": "✅ Confirm", "callback_data": "/PIRO_broadcast2 CONFIRM"}],
        [{"text": "❌ Cancel", "callback_data": "/PIRO_broadcast2 CANCEL"}]
    ]

    bot.sendMessage(
        f"<b>Are You Sure You Want To Broadcast This Message To {len(FulUsrs)} User(s)?</b>",
        parse_mode="html",
        reply_markup={"inline_keyboard": keyboard}
    )
    pass


# ===/PIRO_broadcast2===
def cmd_PIRO_broadcast2():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
            break

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
        raise ReturnCommand()

    action = str(params).upper()

    # Handle Cancel
    if action == "CANCEL":
        User.deleteData("broadcast_data")
        try:
            bot.deleteMessage(message.chat.id, message.message_id)
        except Exception:
            pass
        bot.sendMessage("<b>❌ Broadcast Cancelled</b>", parse_mode="html")
        raise ReturnCommand()

    # Handle Confirm
    if action == "CONFIRM":
        data = User.getData("broadcast_data")

        if not data:
            bot.sendMessage("<b>⚠️ Broadcast data not found. Please resend the message.</b>", parse_mode="html")
            raise ReturnCommand()

        # Generate webhook callback URL to trigger /PIRO_broadcast3 upon completion
        url = libs.Webhook.getUrlFor("/PIRO_broadcast3", u)

        try:
            func = data.get("function")
            kwargs = {k: v for k, v in data.items() if k != "function"}

            # Launch mass broadcast task using function safelist
            task = Bot.broadcast(function=func, callback_url=url, **kwargs)

            if task.get("status") != "success":
                bot.sendMessage(f"<b>❌ Broadcast failed:</b>\n\n<code>{task}</code>", parse_mode="html")
                raise ReturnCommand()

            broadcast_id = task.get("broadcast_id")
            User.saveData("active_broadcast_id", broadcast_id)
            User.deleteData("broadcast_data")

            try:
                bot.deleteMessage(message.chat.id, message.message_id)
            except Exception:
                pass

            bot.sendMessage("<b>🔁 Broadcast started successfully!</b>", parse_mode="html")

        except Exception as e:
            bot.sendMessage(f"<b>❌ Error starting broadcast:</b>\n\n<code>{str(e)}</code>", parse_mode="html")
    pass


# ===/PIRO_broadcast3===
def cmd_PIRO_broadcast3():
    try:
        # Parse webhook options payload delivered upon completion
        if hasattr(options, "json") and options.json:
            data = bunchify(options.json)
        else:
            data = options or {}

        total = data.get("total", 0)
        success = data.get("total_success", 0)
        fail = data.get("total_errors", 0)

        txt = (
            f"<b>📊 Broadcast Completed!</b>\n\n"
            f"<b>Total Attempted:</b> {total}\n"
            f"<b>Total Success:</b> {success}\n"
            f"<b>Total Failed:</b> {fail}"
        )

        bot.sendMessage(text=txt, parse_mode="html")
        User.deleteData("active_broadcast_id")

    except Exception as e:
        bot.sendMessage(text=f"<b>📊 Broadcast Completion Error:</b>\n\n<code>{str(e)}</code>", parse_mode="html")

    pass


# ===/PIRO_unban===
def cmd_PIRO_unban():
    # 12'07'25"00'06'18  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()



    bot.replyText(u,"<b>📢 Send User Telegram ID to Unban in Bot:</b>""",parse_mode = "html")

    Bot.handleNextCommand("/PIRO_unban1")
    pass


# ===/PIRO_unban1===
def cmd_PIRO_unban1():
    # 12'07'25"00'06'18  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    bot.replyText(u,f"""<b> Unanned Success: {message.text} </b>""",parse_mode = "html")

    Bot.saveData(f"{message.text}ban",False)
    pass


# ===/PIRO_withdraw===
def cmd_PIRO_withdraw():
    global msg
    # coded by @Jenish_Dobariya1

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    # --- ULTRA-STRICT SYSTEM CHECK ---
    W_Mode = Bot.getData("WalletWithdrawMode")
    W_With = Bot.getData("WalletWithdraw")

    check_mode = str(W_Mode).strip().upper() if W_Mode is not None else "NONE"
    check_with = str(W_With).strip().upper() if W_With is not None else "NONE"

    WitdMode = Bot.getData("WithdrawMode") or "ON"
    if WitdMode == "OFF":
        WOT = Bot.getData("WOT") or "⛔ Withdrawal Is Off"
        bot.replyText(u, f"<b>{WOT}</b>", parse_mode="HTML")
        raise ReturnCommand()


    PaymentCh = Bot.getData("Botpaychannel") or "not set"
    if PaymentCh == "not set":
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text='⚙️ Set Payout Channel', callback_data='/SetBotPayChann'))

        # Professional message
        msg = (
            "<b>⛔ No Payout Channel Added!</b>\n\n"
            "<i>Current status: Not set. Please set the payout channel "
            "to enable withdrawals for your users.</i>"
        )

        bot.replyText(u, msg, reply_markup=markup, parse_mode="HTML")
        raise ReturnCommand()
    # Referral Verification System
    refC = Bot.getData(str(u) + "RefCount") or 0
    refN = Bot.getData("SetMinRef") or 0

    # gateway-wise min refer: user needs only the lowest requirement among ON gateways
    _needs = []
    for _g in (Bot.getData("AddedGateways") or []):
        if _g.get("on", True):
            _needs.append(int(_g.get("ref", 0) or 0) or int(refN or 0))
    if _needs:
        refN = min(_needs)

    ChkR = int(refN) - int(refC)

    markup = InlineKeyboardMarkup()
    if int(refN) > 0 and int(refC) < int(refN):
        markup.add(InlineKeyboardButton(text=f"You need only {ChkR} refer(s) 🎯", callback_data="Invite Earn 🤑"))
    else:
        markup.add(InlineKeyboardButton(text="Set Wallet 💳", callback_data="Link Wallet"))

    # Balance & Wallet Validations
    wall = Bot.getData("UserWallet" + str(u))
    bal = libs.Resources.anotherRes('Balance', user=u).value()
    Mini_Withdraw = Bot.getData("MinWith") or 1
    # gateway-wise min: lowest min among ON gateways (a gateway with min 1 must work even if another has min 5)
    _mins = []
    for _g in (Bot.getData("AddedGateways") or []):
        if _g.get("on", True):
            _mins.append(float(_g.get("min", 0) or 0) or float(Mini_Withdraw))
    if _mins:
        Mini_Withdraw = min(_mins)

    if bal is None:
        bal = 0.0

    if float(bal) < float(Mini_Withdraw):
        _mw = float(Mini_Withdraw)
        _mw_txt = str(int(_mw)) if _mw == int(_mw) else str(round(_mw, 2))
        bot.replyText(u, "<b>🤑 Minimum ₹" + _mw_txt + " Needed To Withdraw</b>", parse_mode="HTML")
        raise ReturnCommand()

    if wall is None:
        bot.replyText(u, "<b>⚙️ Please set your Wallet first to continue!</b>", parse_mode="HTML", reply_markup=markup)
        raise ReturnCommand()

    if int(refN) > 0 and int(refC) < int(refN):
        bot.replyText(u, f"<b>🎯 You need only {ChkR} more verified refer(s) to unlock withdraw.\n\n👉 Click below to complete & withdraw instantly in your Wallet ✓</b>", parse_mode="HTML", reply_markup=markup)
        raise ReturnCommand()

    # Input prompt triggers
    # gateway-wise min / max shown to the user
    def _nf(x):
        try:
            x = float(x)
            return str(int(x)) if x == int(x) else str(round(x, 2))
        except:
            return str(x)

    def _hn(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    _gmin_def = float(Bot.getData("MinWith") or 5)
    _gmax_def = float(Bot.getData("MaxWith") or 10)
    _lmins = []
    _lmaxs = []
    for _g in (Bot.getData("AddedGateways") or []):
        if _g.get("on", True):
            _lmins.append(float(_g.get("min", 0) or 0) or _gmin_def)
            _lmaxs.append(float(_g.get("max", 0) or 0) or _gmax_def)

    _prompt = "<b>💰 Send Total Amount You Wish To Withdraw</b>"

    bot.replyText(u, _prompt, parse_mode="HTML")
    Bot.handleNextCommand("/PIRO_withdraw1", options="View", cancel_at_command=True)
    pass


# ===/PIRO_withdraw1===
def cmd_PIRO_withdraw1():
    # coded by @Jenish_Dobariya1

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    # ULTRA-STRICT SYSTEM STATUS CHECK 
    W_Mode = Bot.getData("WalletWithdrawMode")
    W_With = Bot.getData("WalletWithdraw")

    check_mode = str(W_Mode).strip().upper() if W_Mode is not None else "NONE"
    check_with = str(W_With).strip().upper() if W_With is not None else "NONE"

    if check_mode == "OFF" or check_mode == "FALSE" or check_with == "OFF" or check_with == "FALSE":
        custom_text = Bot.getData("WOT") or "<b>⛔ Wallet Withdrawal Method Is Currently Disabled</b>"
        bot.replyText(u, custom_text, parse_mode="HTML")
        raise ReturnCommand()


    # Master Gateway Matrix Mapping
    gateway_master = {
        # old per-gateway systems removed - all gateways now come from /addgetwayonbbot

    }
    # make sure every saved gateway has id / on / tax (older entries may miss them)
    _AG = Bot.getData("AddedGateways") or []
    _seq = int(Bot.getData("AddedGatewaysSeq") or 0)
    _mig = False
    for _g in _AG:
        if "id" not in _g:
            _seq += 1
            _g["id"] = _seq
            _g["on"] = True
            _g["tax"] = 0
            _mig = True
    if _mig:
        Bot.saveData("AddedGateways", _AG)
        Bot.saveData("AddedGatewaysSeq", _seq)

    _GMIN = float(Bot.getData("MinWith") or 5)
    _GMAX = float(Bot.getData("MaxWith") or 10)
    # Custom gateways added from /addgetwayonbbot (only ON ones)
    for cg in (Bot.getData("AddedGateways") or []):
        if cg.get("on", True) and cg.get("id") is not None:
            gateway_master["cg" + str(cg.get("id"))] = {
                "name": str(cg.get("name")),
                "cmd": "/PIRO_withdrawCG",
                "url": str(cg.get("domain")),
                "custom": True,
                "gid": str(cg.get("id")),
                "gmin": float(cg.get("min", 0) or 0) or _GMIN,
                "gmax": float(cg.get("max", 0) or 0) or _GMAX,
                "gref": cg.get("ref", 0) or 0,
                "gtax": float(cg.get("tax", 0) or 0)
            }

    #PARSE INPUT AMOUNT CONTROL
    is_callback = False
    if options == "View":
        amount_input = message.text if message.text else msg
    else:
        if params and "_" in str(params):
            try:
                amount_input = params.split("_")[1]
                is_callback = True
            except:
                amount_input = params
        else:
            amount_input = params

    try:
        amount = float(amount_input)
    except:
        bot.replyText(u, "<b>⛔ Only Numeric Values Are Allowed ⛔</b>", parse_mode="HTML")
        raise ReturnCommand()

    # Reject nan / inf / zero / negative amounts (they could bypass the limit checks)
    if amount != amount or amount in (float("inf"), float("-inf")) or amount <= 0:
        bot.replyText(u, "<b>⛔ Enter a valid amount greater than 0</b>", parse_mode="HTML")
        raise ReturnCommand()
    amount = round(amount, 2)

    # Global Business Limits & Balance Validations
    wallet = Bot.getData("UserWallet" + str(u)) or "Not Linked"
    minWith = float(Bot.getData("MinWith") or 5)
    maxWith = float(Bot.getData("MaxWith") or 10)
    # gateway-wise limits: the lowest min / highest max among ON gateways decides what can be typed
    _cm = [v.get("gmin") for v in gateway_master.values() if v.get("custom")]
    _cx = [v.get("gmax") for v in gateway_master.values() if v.get("custom")]
    if _cm:
        minWith = float(min(_cm))
    if _cx:
        maxWith = float(max(_cx))
    bal = float(libs.Resources.anotherRes('Balance', user=u).value() or 0)

    if amount < minWith:
        # only: "Min Withdraw was ₹X (gateway with the lowest min)"
        _who = []
        for _v in gateway_master.values():
            if _v.get("custom") and float(_v.get("gmin") or 0) == minWith:
                _who.append(str(_v.get("name")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        _mtxt = str(int(minWith)) if minWith == int(minWith) else str(round(minWith, 2))
        bot.replyText(u, "<b>⚠️ Min Withdraw was ₹" + _mtxt + (" (" + ", ".join(_who) + ")" if _who else "") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    if amount > bal:
        bot.replyText(u, "<b>🚫 Withdraw Amount Exceeds Your Balance</b>", parse_mode="HTML")
        raise ReturnCommand()

    if amount > maxWith:
        bot.replyText(u, f"<b>⚠️ Maximum Withdraw Limit:</b> <code>{maxWith}</code>", parse_mode="HTML")
        raise ReturnCommand()


    # ================== VIEW STATE: SHOW ACTIVE OPTIONS ==================
    if options == "View" or not is_callback:
        def nf(x):
            try:
                x = float(x)
                if x == int(x):
                    return str(int(x))
                return str(round(x, 2))
            except:
                return str(x)

        markup = InlineKeyboardMarkup()
        has_active_gateway = False
        locked_list = []
        list_lines = []
        locked_msgs = []
        row_buttons = []

        def hn(x):
            return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # 2-Column Responsive Layout Generation System
        for key, info in gateway_master.items():
            if Bot.getData(key) == True or info.get("custom"): 
                if info.get("custom"):
                    g_min = float(info.get("gmin") or 0)
                    g_max = float(info.get("gmax") or 0)
                    need_ref = int(info.get("gref") or 0) or int(Bot.getData("SetMinRef") or 0)
                    have_ref = int(Bot.getData(str(u) + "RefCount") or 0)
                    _why = []
                    if g_min and amount < g_min:
                        _why.append("Min Amount Is ₹" + nf(g_min))
                    if g_max and amount > g_max:
                        _why.append("Max Amount Is ₹" + nf(g_max))
                    if need_ref > 0 and have_ref < need_ref:
                        _why.append("Min Refer Is " + str(need_ref) + " (You Have " + str(have_ref) + ")")
                    if not (len(str(wallet)) == 10 and str(wallet).isdigit()):
                        _why.append("Wallet Is Not Linked")
                    if _why:
                        # not clickable - explained in one line under the amount
                        locked_msgs.append((hn(info['name']), " & ".join(_why)))
                        continue
                has_active_gateway = True
                list_lines.append(hn(info['name']))
                row_buttons.append(InlineKeyboardButton(text=str(info['name']), callback_data=f"/PIRO_withdraw1 {key}_{amount}"))

                if len(row_buttons) == 2:
                    markup.row(*row_buttons)
                    row_buttons = []

        if row_buttons: 
            markup.row(*row_buttons)

        markup.add(InlineKeyboardButton(text='❌ Cancel Transaction', callback_data='/delete'))

        if not list_lines and not locked_msgs:
            bot.replyText(u, "<b>⛔ No gateway is active right now.</b>", parse_mode="HTML")
            raise ReturnCommand()

        # gateway selection screen: wallet + amount, then one line per gateway that can't be used, then the choose line
        # gateways with the same reason share one line: "You Can't Use A & B Because ..."
        _groups = []
        for _gn, _rs in locked_msgs:
            _found = False
            for _gp in _groups:
                if _gp[0] == _rs:
                    _gp[1].append(_gn)
                    _found = True
            if not _found:
                _groups.append([_rs, [_gn]])
        if not has_active_gateway:
            bot.replyText(u, "<b>⛔ No gateway is available for this amount right now.</b>", parse_mode="HTML")
            raise ReturnCommand()
        _info_lines = ["Choose Below Gateway To Withdraw 👇🏻"]
        # gateway-wise min / max for every ON gateway
        _lim_block = ""
        VIEWmessage = "<b>🏦 Wallet Number :: </b><code>" + hn(wallet) + "</code>\n<b>👛 Amount :: </b><code>₹" + nf(amount) + "</code>" + _lim_block + "\n\n<i>" + "\n".join(_info_lines) + "</i>"
        bot.replyText(u, text=VIEWmessage, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
        raise ReturnCommand()


    # ================== EXECUTION STATE: VERIFIED CALLBACK ONLY ==================
    if is_callback and params:
        # one transaction at a time: show a popup only (nothing in the chat changes, button stays usable later)
        _now_t = time.time()
        try:
            _lk = float(User.getData("wd_lock") or 0)
        except:
            _lk = 0
        try:
            _la = float(User.getData("wd_last_amt") or -1)
            _lt = float(User.getData("wd_last_ts") or 0)
        except:
            _la = -1
            _lt = 0
        try:
            _pre = float(User.getData("wd_pre") or 0)
        except:
            _pre = 0
        if (_lk and (_now_t - _lk) < 180) or (_pre and (_now_t - _pre) < 90) or (_la == amount and (_now_t - _lt) < 5):
            try:
                bot.answerCallbackQuery(call.id, "⏳ Already one transaction is working. Please wait.", show_alert=True)
            except:
                pass
            raise ReturnCommand()

        _done_clicks = User.getData("wd_done") or []
        if message.message_id not in _done_clicks:
            _done_clicks.append(message.message_id)
            User.saveData("wd_done", _done_clicks[-10:])
        else:
            try:
                bot.answerCallbackQuery(call.id, "🔴 Don't Spam Actions")
            except:
                pass
            raise ReturnCommand()

        selected_gw_key = params.split("_")[0]
        gw_data = gateway_master.get(selected_gw_key)

        # Strict Validation
        if not gw_data or (Bot.getData(selected_gw_key) != True and not gw_data.get("custom")):
            bot.replyText(u, "<b>❌ Selected Gateway is currently inactive or invalid!</b>", parse_mode="HTML")
            raise ReturnCommand()

        # Gateway-wise min refer + limits (re-check on execution)
        if gw_data.get("custom"):
            need_ref = int(gw_data.get("gref") or 0) or int(Bot.getData("SetMinRef") or 0)
            have_ref = int(Bot.getData(str(u) + "RefCount") or 0)
            if need_ref > 0 and have_ref < need_ref:
                bot.replyText(u, f"<b>🔒 {gw_data['name']} needs {need_ref} verified refer(s).\nYou have {have_ref}.</b>", parse_mode="HTML")
                raise ReturnCommand()
            g_min = float(gw_data.get("gmin") or 0)
            g_max = float(gw_data.get("gmax") or 0)
            if (g_min and amount < g_min) or (g_max and amount > g_max):
                bot.replyText(u, f"<b>⚠️ {gw_data['name']} limit is ₹{g_min} - ₹{g_max if g_max else 'No Max'}</b>", parse_mode="HTML")
                raise ReturnCommand()

        # reserve the user's "one transaction at a time" slot now (released by the payout command when it finishes)
        User.saveData("wd_pre", time.time())

        #DELETE THE GATEWAY CHOOSE MESSAGE INSTANTLY
        try:
            bot.deleteMessage(u, message.message_id)
        except:
            pass

        if not gw_data.get("custom"):
            Bot.saveData("gatewaynow", selected_gw_key)

        # ANIMATED PROCESSING ENGINE (NO IMPORTS) 
        try:
            # Initial Message with Selected Gateway Info
            M = bot.sendMessage(u, f"⚙️ <b>Selected Gateway:</b> <code>{gw_data['name']}</code>\n⏳ <b>Connecting Secure Server...</b>", parse_mode="HTML")
            msg_id = M.message_id
            chat_id = u

            colors = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫"]
            total_slots = 8

            boot_lines = [
                "🔐 Securing API Encrypted Tunnel",
                "📡 Initializing Gateway Handshake",
                "💳 Fetching User Wallet Address",
                "🪙 Checking Liquidity Node Balance",
                "⚡ Formatting Instant Payout Packet",
                "🚀 Transmitting Fund Dispatch Request"
            ]

            # same look as before, just a little lighter (quick edits, no heavy loop)
            # 4 quick steps (fewer Telegram calls = no flood limits when many users withdraw together)
            for stage in (2, 5, 8, 10):
                percent = stage * 10
                filled = int((percent / 100) * total_slots)

                color = colors[(stage - 1) % len(colors)]
                bar = (color * filled) + ("⬜" * (total_slots - filled))

                dots = "." * (stage % 4)
                boot_text = boot_lines[(stage - 1) % len(boot_lines)]

                try:
                    bot.editMessageText(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=(
                            f"⚙️ <b>Selected Gateway:</b> <code>{gw_data['name']}</code>\n"
                            f"💰 <b>Amount:</b> <code>₹{amount}</code>\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "💸 <b>Processing Withdrawal" + dots + "</b>\n\n"
                            "📡 <b>Action:</b> " + boot_text + "\n\n"
                            + bar + "  " + str(percent) + "%"
                        ),
                        parse_mode="HTML"
                    )
                except:
                    pass



            # Final Success State Text
            bot.editMessageText(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"⚙️ <b>Selected Gateway:</b> <code>{gw_data['name']}</code>\n\n✅ <b>Gateway Handshake Successful!</b>\n💰 <i>Routing fund packet to your linked wallet...</i>",
                parse_mode="HTML"
            )

        except Exception as e:
            M = bot.sendMessage(u, f"⚙️ <b>Selected Gateway:</b> <code>{gw_data['name']}</code>\n<b>⏳ Processing your wallet withdrawal request...</b>", parse_mode="HTML")

        # BACKEND EXECUTION ROUTE
        op = f"{M.message_id}_{amount}"
        if gw_data.get("custom"):
            op = f"{M.message_id}_{amount}_{gw_data['gid']}"
        Bot.runCommand(gw_data["cmd"], options=op)
    pass


# ===/PIRO_withdrawCG===
def cmd_PIRO_withdrawCG():
    # coded by @Jenish_Dobariya1
    # Secure multi-gateway payout executor (Wallet + UPI) - real gateway reasons, Cloudflare detection, safe refunds
    # options = "<panel_message_id>_<amount>_<gateway_id>"

    # ------------------------------------------------------------------ helpers
    def esc(t):
        if t is None:
            return ""
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def nfmt(x):
        try:
            x = float(x)
            if x == int(x):
                return str(int(x))
            return str(round(x, 2))
        except:
            return str(x)

    def mask_addr(a):
        a = str(a or "")
        if "@" in a:
            h, d = a.split("@", 1)
            if len(h) > 2:
                return h[:2] + "****@" + d
            return "****@" + d
        if len(a) >= 6:
            return a[0:3] + "*****" + a[-3:]
        return "****"

    def valid_wallet(a):
        a = str(a or "")
        return len(a) == 10 and a.isdigit()

    def valid_upi(a):
        a = str(a or "")
        if len(a) < 5 or len(a) > 64 or a.count("@") != 1:
            return False
        h, d = a.split("@")
        if len(h) < 2 or len(d) < 2:
            return False
        for ch in h:
            if not (ch.isalnum() or ch in "._-"):
                return False
        for ch in d:
            if not (ch.isalnum() or ch in ".-"):
                return False
        return True

    def rep(s, key, val):
        out = ""
        low = s.lower()
        k = key.lower()
        i = 0
        for _n in range(300):
            j = low.find(k, i)
            if j < 0:
                break
            out += s[i:j] + val
            i = j + len(k)
        out += s[i:]
        return out

    def qsafe(s):
        ok = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
        out = ""
        for ch in str(s):
            if ch in ok:
                out += ch
            elif ch == " ":
                out += "%20"
            else:
                out += "%" + ("%02X" % ord(ch)) if ord(ch) < 256 else ""
        return out

    def secret_values(url):
        vals = []
        try:
            if "?" in url:
                for pair in url.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        kl = k.lower()
                        for w in ["token", "key", "secret", "pass", "auth", "sign", "guid", "mid", "api"]:
                            if w in kl and len(v) >= 3 and "{" not in v:
                                vals.append(v)
                                break
        except:
            pass
        return vals

    def strip_tags(t):
        out = []
        depth = 0
        for ch in t:
            if ch == "<":
                depth += 1
            elif ch == ">" and depth > 0:
                depth -= 1
            elif depth == 0:
                out.append(ch)
        return "".join(out)

    def clean_text(t, url, addrs):
        # never let a URL, token or full address reach a user / channel
        try:
            t = strip_tags(str(t)[:4000])
            for sv in secret_values(url):
                t = t.replace(sv, "****")
            keep = []
            for w in t.split():
                wl = w.lower()
                if "://" in wl or wl.startswith("www."):
                    continue
                keep.append(w)
            t = " ".join(keep)
            for a in addrs:
                if a:
                    t = t.replace(a, mask_addr(a))
            return t[:200]
        except:
            return ""

    SUCCESS_WORDS = ["success", "successful", "successfully", "succeeded", "completed", "complete", "paid", "processed", "done", "ok", "approved", "credited", "settled", "captured", "true", "1"]
    PENDING_WORDS = ["pending", "processing", "queued", "initiated", "accepted", "submitted", "in_progress", "in progress", "inprogress"]
    FAIL_WORDS = ["failed", "failure", "fail", "error", "declined", "rejected", "denied", "cancelled", "canceled", "false", "0", "invalid", "insufficient", "blocked", "unauthorized", "forbidden", "refunded", "reversed", "expired"]
    HTTP_NAMES = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 422: "Unprocessable Request", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}

    def word_kind(s):
        # strict word match first, fuzzy second; "unsuccessful" can never count as success
        sl = str(s).strip().lower()
        if sl == "":
            return "unknown"
        if sl in FAIL_WORDS:
            return "fail"
        if sl in PENDING_WORDS:
            return "pending"
        if sl in SUCCESS_WORDS:
            return "success"
        for x in ["unsuccess", "not success", "fail", "declin", "reject", "denied", "insufficient", "invalid", "error", "cancel", "unauthor", "forbidden", "blocked"]:
            if x in sl:
                return "fail"
        if "success" in sl or "paid" in sl or "complete" in sl:
            return "success"
        for x in ["pending", "process", "queue", "initiat", "submitted", "accepted"]:
            if x in sl:
                return "pending"
        return "unknown"

    def classify_json(d):
        status = ""
        for k in ["status", "Status", "STATUS", "state", "result", "Result", "txn_status", "payment_status"]:
            v = d.get(k)
            if v is not None and not isinstance(v, (dict, list)):
                status = str(v).strip()
                break
        sflag = None
        for k in ["success", "Success", "ok", "is_success", "isSuccess"]:
            v = d.get(k)
            if isinstance(v, bool):
                sflag = v
                break
            if isinstance(v, str) and v.strip().lower() in ["true", "false"]:
                sflag = (v.strip().lower() == "true")
                break
        msg = ""
        for k in ["gateway_message", "gatewayMessage", "message", "Message", "msg", "error_message", "errorMessage", "error", "Error", "reason", "description", "detail", "details", "remark", "remarks", "statusMessage"]:
            v = d.get(k)
            if v is not None and v != "" and v is not False:
                msg = str(v)
                break
        code = ""
        for k in ["error_code", "errorCode", "code", "Code", "status_code", "statusCode"]:
            v = d.get(k)
            if v is not None and v != "" and not isinstance(v, (dict, list)):
                code = str(v)
                break
        sk = word_kind(status) if status else "unknown"
        ok = (sk == "success") or (sflag is True)
        bad = (sk == "fail") or (sflag is False)
        if ok and bad:
            kind = "unknown"
        elif bad:
            kind = "fail"
        elif sk == "pending":
            kind = "pending"
        elif ok:
            kind = "success"
        elif status == "" and msg != "" and (str(d.get("error")).strip().lower() not in ("", "none", "null", "false", "0", "no")):
            kind = "fail"
        else:
            kind = "unknown"
        return kind, status, msg, code

    def http_code_of(low):
        # exception text looks like: "...failed: 403 client error: forbidden for url: ..."
        for marker in ["client error", "server error"]:
            i = low.find(marker)
            if i >= 4:
                c = low[i - 4:i].strip().strip(":").strip()
                if c.isdigit() and len(c) == 3:
                    return int(c)
        return 0

    CF_STRONG = ["just a moment", "cf-chl", "cf_chl", "challenge-platform", "cf-browser-verification", "turnstile", "checking your browser before accessing"]

    def host_is_cloudflare(host):
        # every Cloudflare-proxied domain answers /cdn-cgi/trace (no secrets are sent - host only)
        try:
            r = HTTP.get("https://" + host + "/cdn-cgi/trace", timeout=4)
            t = str(r.text)
            return ("colo=" in t and "visit_scheme=" in t)
        except:
            return False

    def call_gateway(url):
        res = {"kind": "unknown", "status": "", "msg": "", "code": "", "cf": False, "http": 0}
        browser = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
        try:
            host = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("@")[-1]
        except:
            host = ""
        cf_host = None
        for attempt in range(1, 2):
            try:
                if attempt == 1:
                    r = HTTP.get(url)
                else:
                    r = HTTP.get(url, headers=browser, timeout=30)
            except Exception as ex:
                low = str(ex).lower()
                code = http_code_of(low)
                res["http"] = code
                if code in (403, 429, 503):
                    if cf_host is None:
                        cf_host = host_is_cloudflare(host) if host else False
                    if cf_host:
                        res["cf"] = True
                        res["kind"] = "fail"
                        return res
                    res["kind"] = "fail"
                    res["msg"] = "Gateway rejected the request (HTTP " + str(code) + " " + HTTP_NAMES.get(code, "") + ")"
                    return res
                if code in (521, 522, 523, 525, 526, 530):
                    res["kind"] = "fail"
                    res["cf"] = False
                    res["msg"] = "Gateway server is down or unreachable (HTTP " + str(code) + ")"
                    return res
                if code >= 400 and code < 500 and code not in (408, 409):
                    res["kind"] = "fail"
                    res["msg"] = "Gateway rejected the request (HTTP " + str(code) + " " + HTTP_NAMES.get(code, "") + ")"
                    return res
                if code >= 500 or code in (408, 409):
                    res["kind"] = "unknown"
                    res["msg"] = "Gateway server error (HTTP " + str(code) + ")"
                    return res
                if "timed out" in low or "timeout" in low:
                    res["kind"] = "unknown"
                    res["msg"] = "Gateway did not respond in time"
                    return res
                for x in ["could not resolve host", "name or service not known", "nodename nor servname", "connection refused", "failed to establish a new connection", "no route to host", "network is unreachable", "certificate verify failed", "sslerror", "invalid url", "not allowed", "blocked"]:
                    if x in low:
                        res["kind"] = "fail"
                        res["msg"] = "Could not connect to the gateway server"
                        return res
                res["kind"] = "unknown"
                res["msg"] = "Gateway request failed"
                return res
            # ---- got a normal (2xx/3xx) response
            try:
                res["http"] = int(r.status_code)
            except:
                res["http"] = 200
            text = ""
            try:
                text = str(r.text)
            except:
                text = ""
            d = None
            try:
                d = r.json()
            except:
                d = None
            if isinstance(d, dict):
                kind, status, msg, code = classify_json(d)
                res["kind"] = kind
                res["status"] = status
                res["msg"] = msg
                res["code"] = code
                return res
            low = text.lower()[:6000]
            strong = False
            for m in CF_STRONG:
                if m in low:
                    strong = True
            if strong or ("cloudflare" in low and ("attention required" in low or "error 1020" in low or "ray id" in low)):
                res["cf"] = True
                res["kind"] = "fail"
                return res
            plain = text.strip()
            if plain != "" and len(plain) <= 150 and "<html" not in plain.lower():
                # plain text: only an exact keyword counts - anything else is "unconfirmed" (never guess a refund)
                pl = plain.strip().lower().strip(".! ")
                if pl in FAIL_WORDS:
                    res["kind"] = "fail"
                elif pl in PENDING_WORDS:
                    res["kind"] = "pending"
                elif pl in SUCCESS_WORDS:
                    res["kind"] = "success"
                else:
                    res["kind"] = "unknown"
                res["msg"] = plain
                return res
            res["kind"] = "unknown"
            res["msg"] = "Gateway sent an unrecognised response"
            return res
        return res


    # ------------------------------------------------------------------ main
    def main():
        try:
            P = str(options).split("_")
            MD = int(P[0])
            amount = float(P[1])
            gid = str(P[2]) if len(P) > 2 else ""
        except:
            return
        if amount != amount or amount <= 0 or amount > 10000000:
            return
        amount = round(amount, 2)

        def show(text):
            try:
                bot.editMessageText(chat_id=u, message_id=MD, text=text, parse_mode="HTML", disable_web_page_preview=True)
            except:
                try:
                    bot.sendMessage(chat_id=u, text=text, parse_mode="HTML")
                except:
                    pass

        def fail_msg(reason, note=""):
            t = "<b>❌ Withdrawal Failed\n\n⚠️ Reason : <code>" + esc(reason) + "</code>"
            if note:
                t += "\n\n" + note
            show(t + "</b>")

        if Bot.getData("BotMode") == "OFF":
            show("<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>")
            return
        AllBanUsers = str(Bot.getData("AllBanUsers") or "12345").split(",")
        if str(u) in AllBanUsers:
            show("<b><i>🚫 You are Banned in this Bot</i></b>")
            return
        if (Bot.getData("WithdrawMode") or "ON") == "OFF":
            show("<b>" + str(Bot.getData("WOT") or "⛔ Withdrawal Is Off") + "</b>")
            return

        gw = None
        for g in (Bot.getData("AddedGateways") or []):
            if str(g.get("id")) == gid:
                gw = g
                break
        if gw is None or not gw.get("on", True):
            fail_msg("This gateway is not available right now")
            return

        gname = str(gw.get("name") or "Gateway")
        gtype = "wallet"
        api_url = str(gw.get("url") or "").strip().replace("&amp;", "&")
        gw_tax = float(gw.get("tax", 0) or 0)
        gw_min = float(gw.get("min", 0) or 0)
        gw_max = float(gw.get("max", 0) or 0)

        addr = str(Bot.getData("UserWallet" + str(u)) or "").strip()
        if not valid_wallet(addr):
            fail_msg("Link a valid 10-digit wallet number first", "👉 Use the <u>Link Wallet</u> button")
            return

        banw = str(Bot.getData("AllBanWallets") or "12345").split(",")
        if addr in banw or addr.lower() in banw:
            show("<b><i>🚫 Your " + ("UPI" if gtype == "upi" else "Wallet") + " is Banned in this Bot</i></b>")
            return

        minWith = float(Bot.getData("MinWith") or 5)
        maxWith = float(Bot.getData("MaxWith") or 10)
        eff_min = gw_min if gw_min else minWith
        eff_max = gw_max if gw_max else maxWith
        if amount < eff_min:
            fail_msg("Minimum withdrawal is ₹" + nfmt(eff_min))
            return
        if amount > eff_max:
            fail_msg("Maximum withdrawal is ₹" + nfmt(eff_max))
            return

        need_ref = int(gw.get("ref", 0) or 0) or int(Bot.getData("SetMinRef") or 0)
        have_ref = int(Bot.getData(str(u) + "RefCount") or 0)
        if need_ref > 0 and have_ref < need_ref:
            fail_msg("You need " + str(need_ref) + " verified refer(s) for this gateway (you have " + str(have_ref) + ")")
            return

        after = round(amount - gw_tax, 2)
        if after <= 0:
            fail_msg("Amount Is Too Low For This Gateway After Adding Tax")
            return

        bal = float(libs.Resources.anotherRes("Balance", user=u).value() or 0)
        if bal < amount:
            fail_msg("Insufficient balance")
            return

        # same amount twice within 5 seconds = accidental double tap (no balance change here)
        now_ts = time.time()
        try:
            last_amt = float(User.getData("wd_last_amt") or -1)
            last_ts = float(User.getData("wd_last_ts") or 0)
        except:
            last_amt = -1
            last_ts = 0
        def block(msg):
            # race fallback: remove the progress message and show a popup instead of an error text
            ST["blocked"] = True
            try:
                bot.deleteMessage(u, MD)
            except:
                pass
            try:
                bot.answerCallbackQuery(call.id, msg, show_alert=True)
            except:
                pass

        if last_amt == amount and (now_ts - last_ts) < 5:
            block("⏳ Already one transaction is working. Please wait.")
            return

        # one withdrawal at a time per user (lock auto-expires after 3 minutes)
        try:
            lk = float(User.getData("wd_lock") or 0)
        except:
            lk = 0
        if lk and (now_ts - lk) < 180:
            block("⏳ Already one transaction is working. Please wait.")
            return
        User.saveData("wd_lock", now_ts)
        ST["lock"] = True
        User.saveData("wd_last_amt", amount)
        User.saveData("wd_last_ts", now_ts)

        Botpayname = str(Bot.getData("BotPayComm") or "Payment")
        PaymentCh = Bot.getData("Botpaychannel") or "not set"
        try:
            bot_un = str(Bot.info().username)
        except:
            bot_un = "bot"
        usr = '<a href="tg://user?id=' + str(u) + '">' + str(u) + '</a>'
        hidden = mask_addr(addr)

        QUEUE = []

        def post_channel(text):
            # the user's result is shown FIRST; the channel post is sent right after (end of this command)
            QUEUE.append(text)

        def send_channel(text):
            # 1) normal HTML post  2) plain-text post (in case HTML was the problem)  3) quick retries + tell admin why
            if PaymentCh == "not set":
                return
            err = ""
            try:
                bot.replyText(PaymentCh, text, parse_mode="HTML")
                return
            except Exception as ex1:
                err = str(ex1)
            try:
                bot.replyText(PaymentCh, strip_tags(text))
                return
            except Exception as ex2:
                err = str(ex2)
            try:
                Bot.runCommandAfter(4, "/WdPostRetry", options="!1!" + text)
            except:
                pass
            try:
                try:
                    err = err.replace(str(Bot.info().token), "****")
                except:
                    pass
                note = "<b>⚠️ Payout channel post failed (retrying automatically)</b>\n\n📝 Reason: <code>" + esc(clean_text(err, "", [])[:180]) + "</code>\n\nCheck that the bot is admin in your payout channel and the channel is set correctly."
                for ad in (Bot.getData("AllBotAdminss") or [])[:3]:
                    try:
                        bot.sendMessage(chat_id=ad, text=note, parse_mode="HTML")
                    except:
                        pass
            except:
                pass

        def channel_text(title, balance_now, resp, show_tax=False):
            return ("<b>" + title + "</b>\n\n<b>🟢 User : " + usr + "</b>\n<b>🏦 Gateway : " + esc(gname) + (" (UPI)" if gtype == "upi" else " (Wallet)") + "</b>\n<b>🤑 Remaining Balance :- " + nfmt(balance_now) + "</b>\n\n<b>🚀 Amount : " + nfmt(amount) + " INR (-)</b>" + (("\n<b>💸 Tax : ₹" + nfmt(round(amount - after, 2)) + "</b>") if show_tax else "") + "\n<b>⛔ Address : <code>" + esc(hidden) + "</code></b>\n\n<b>🤖 Bot: @" + esc(bot_un) + "</b>\n\n<b>⚠️ " + esc(gname) + " Response: <code>" + esc(resp) + "</code></b>")

        # ---- take the money first, then pay
        def bal_now():
            return float(libs.Resources.anotherRes("Balance", user=u).value() or 0)

        def refund():
            before = bal_now()
            libs.Resources.anotherRes("Balance", user=u).add(amount)
            if bal_now() <= before:
                for ad in (Bot.getData("AllBotAdminss") or [])[:5]:
                    try:
                        bot.sendMessage(chat_id=ad, text="<b>🚨 A refund did not apply</b>\n\n👤 User: " + usr + "\n💰 Amount: ₹" + nfmt(amount) + "\n\nPlease add it back with Admin ➜ Change Balance.", parse_mode="HTML")
                    except:
                        pass
                return False
            return True

        libs.Resources.anotherRes("Balance", user=u).cut(amount)
        after_cut = bal_now()
        if after_cut >= bal:
            # the balance did not go down, so nothing was reserved - never pay in that case
            fail_msg("Could not reserve your balance. Please try again.")
            return
        if after_cut < 0:
            refund()
            fail_msg("Insufficient balance")
            return

        url = api_url
        for k in ["{number}", "{wallet}", "{paytm}", "{upi}", "{vpa}", "{account}"]:
            url = rep(url, k, addr)
        url = rep(url, "{amount}", str(after))
        url = rep(url, "{comment}", qsafe(bot_un if bot_un != "bot" else Botpayname))
        url = rep(url, "{username}", qsafe(Botpayname))
        if "{" in url or "}" in url or not url.startswith(("http://", "https://")):
            refund()
            fail_msg("This gateway is not configured correctly. Please contact admin.", "✅ Your balance has been refunded.")
            return

        res = call_gateway(url)
        addrs = [addr]
        msg_clean = clean_text(res.get("msg"), api_url, addrs)
        status_clean = clean_text(res.get("status"), api_url, addrs)
        code_clean = clean_text(res.get("code"), api_url, addrs)
        http_n = res.get("http") or 0
        real_code = code_clean or (str(http_n) if http_n else "None")
        resp_line = "{'status': '" + (status_clean or res.get("kind")) + "', 'message': '" + (msg_clean or "None") + "', 'code': '" + real_code + "'}"

        kind = res.get("kind")

        if res.get("cf"):
            reason = "Cloudflare protected - the gateway's firewall blocked the request" + (" (HTTP " + str(http_n) + ")" if http_n >= 400 else "")
            resp_line = "{'status': 'blocked', 'message': 'Cloudflare protection', 'code': '" + (str(http_n) if http_n else "CF") + "'}"
        elif msg_clean:
            reason = msg_clean
        elif status_clean:
            reason = "Gateway status: " + status_clean
        else:
            reason = "The gateway did not give a reason"

        if kind == "success" or kind == "pending":
            WithdrawC = int(Bot.getData("WithdrawC") or 0)
            Bot.saveData("WithdrawC", WithdrawC + 1)
            libs.Resources.globalRes("BotWithdrawsuccess").add(amount)
            libs.Resources.anotherRes("Withdraw", user=u).add(amount)
            libs.Resources.globalRes("BotWithdraws").add(amount)
            balance_now = float(libs.Resources.anotherRes("Balance", user=u).value() or 0)
            post_channel(channel_text("✅ New Withdrawal Processed ✅", balance_now, resp_line, True))
            show("<b>🎁 Your Withdrawal Successfully Processed 🔥🚀\n\n✔️ Please Check Your " + esc(gname) + " Wallet Account!</b>")
        else:
            # always one of two outcomes for the user: success or refund (no "under review")
            refunded = refund()
            balance_now = float(libs.Resources.anotherRes("Balance", user=u).value() or 0)
            if refunded:
                post_channel(channel_text("❌ New Withdrawal Failed & Refunded ❌", balance_now, resp_line))
                fail_msg(reason, "✅ Your balance has been refunded.")
            else:
                post_channel(channel_text("❌ New Withdrawal Failed ❌", balance_now, resp_line))
                fail_msg(reason, "⚠️ Your refund is being processed by admin.")

        for _t in QUEUE:
            send_channel(_t)

    ST = {"lock": False, "blocked": False}
    try:
        main()
    finally:
        # only the run that TOOK the lock may release it (a blocked second run must never free the first one's lock)
        if ST["lock"]:
            try:
                User.deleteData("wd_lock")
            except:
                pass
        if not ST["blocked"]:
            try:
                User.deleteData("wd_pre")
            except:
                pass
    pass


# ===/Perfom_Stats===
def cmd_Perfom_Stats():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    markup = InlineKeyboardMarkup()

    markup.add(InlineKeyboardButton(text=' 🏆Leaderboard',callback_data='/Top30Referral'))

    markup.add(InlineKeyboardButton(text=' Top Bal',callback_data='/TopBalance'),InlineKeyboardButton(text='Raw Stats',callback_data='/RawStats'))

    markup.add(InlineKeyboardButton(text=' Most Withdrawals (Wallet) ',callback_data='/TopWithdraw_Wallet'))

    markup.add(InlineKeyboardButton(text=' Top Promoters',callback_data='/TopWithdraws'))

    markup.add(InlineKeyboardButton(text='Added Links',callback_data='/PIRO_ChLinks'))


    markup.add(InlineKeyboardButton(text=' Add Balance Text',callback_data='/AddBalTxt'))

    markup.add(InlineKeyboardButton(text='🔙Back',callback_data='/admin AP'))


    TXT="<b>📊 Here You Can View Leaderboard & Metrics 🏆</b>"
    try:
        _safe_edit(chat_id=u, message_id=message.message_id,text=TXT,reply_markup=markup)
    except:
        pass

    #bot.replyText(u,TXT,reply_markup=markup)
    pass


# ===/RC_Change===
def cmd_RC_Change():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Owner_Here
    # [ DM TO BUY ANY BOTS & CODES ]

    # ==============================
    # ADMIN CHECK
    # ==============================

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # CALLBACK DATA
    # ==============================

    P = []

    if params:
        P = str(params).split("_")

    if options:
        P = str(options).split("_")


    if len(P) < 2:
        bot.replyText(
            u,
            "<b>❌ Invalid Gift Code Action</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    ACTION = str(P[0])
    RC = str(P[1]).strip()


    # ==============================
    # ALL CODES
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []
    All_BRC = [str(x) for x in All_BRC]


    if RC not in All_BRC:

        try:
            bot.answerCallbackQuery(
                callback_query_id=call.id,
                text="❌ Gift Code Not Found",
                show_alert=True
            )
        except:
            pass

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    # ==============================
    # INFO
    # ==============================

    if ACTION == "info":

        ABC = Bot.getData(
            "GiftCode" + RC + "isAMT"
        )

        if not ABC or len(ABC) < 2:
            Bot.runCommand("/PIRO_RC_Pannel")
            raise ReturnCommand()


        MxC = Bot.getData(
            "Gift" + RC + "MaxUsrCanClaim"
        ) or 0

        C = Bot.getData(
            "Gift" + RC + "CountOfClaimedUsrs"
        ) or 0

        MinRef = Bot.getData(
            "Gift" + RC + "MinRef"
        ) or 0

        Status = Bot.getData(
            "Gift" + RC + "KaStatus"
        ) or "Active"


        if Status == "Active":
            StatusText = "🟢 Aᴄᴛɪᴠᴇ"
            StatusCallback = "🟢"
        else:
            StatusText = "🔴 Dᴇᴀᴄᴛɪᴠᴇ"
            StatusCallback = "🔴"


        markup = InlineKeyboardMarkup()


        markup.add(
            InlineKeyboardButton(
                text="✏️ Rᴇɴᴀᴍᴇ Gɪғᴛ Cᴏᴅᴇ",
                callback_data="/RC_Edit Rename_" + RC
            )
        )


        markup.add(
            InlineKeyboardButton(
                text="👥 Eᴅɪᴛ Tᴏᴛᴀʟ Uѕᴇʀѕ",
                callback_data="/RC_Edit MxUsr_" + RC
            )
        )


        markup.add(
            InlineKeyboardButton(
                text="💸 Eᴅɪᴛ Aᴍᴏᴜɴᴛ",
                callback_data="/RC_Edit Amt_" + RC
            )
        )


        markup.add(
            InlineKeyboardButton(
                text="🎯 Eᴅɪᴛ Mɪɴ. Rᴇғᴇʀ",
                callback_data="/RC_Edit MinRef_" + RC
            )
        )


        markup.add(
        InlineKeyboardButton(
            text="👥 Cʟᴀɪᴍᴇᴅ Uѕᴇʀѕ",
            callback_data="/RC_ClaimedUsers " + RC
        )
    )

        markup.add(
            InlineKeyboardButton(
                text="🔙 Bᴀᴄᴋ",
                callback_data="/PIRO_RC_Pannel"
            )
        )


        TXT = f"""<b>🎁 Gɪғᴛ Cᴏᴅᴇ Iɴғᴏ

🔑 Cᴏᴅᴇ: <code>{RC}</code>

📤 Cʟᴀɪᴍᴇᴅ: {C} / {MxC}
💸 Pᴇʀ Uѕᴇʀ: ₹{ABC[1]}
🎯 Mɪɴ. Rᴇғᴇʀ: {MinRef}
📊 Sᴛᴀᴛᴜs: {StatusText}</b>"""


        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

        raise ReturnCommand()


    # ==============================
    # DELETE
    # ==============================

    # ==============================
    # DELETE
    # ==============================

    if ACTION == "delete":

        RC = str(P[1]).strip()

        All_BRC = Bot.getData("All_BRC") or []
        All_BRC = [str(x) for x in All_BRC]

        # ==============================
        # CHECK CODE EXISTS
        # ==============================

        if RC not in All_BRC:

            try:
                bot.answerCallbackQuery(
                    call.id,
                    "❌ Gift Code Already Deleted",
                    show_alert=True
                )
            except:
                pass

            raise ReturnCommand()


        # ==============================
        # GET CLAIMED USERS BEFORE DELETE
        # ==============================

        ClaimedUsers = Bot.getData(
            "UsrsClaimedBotRC" + RC
        ) or []


        # ==============================
        # DELETE FROM ALL_BRC
        # ==============================

        New_BRC = []

        for CODE in All_BRC:

            if str(CODE) != RC:
                New_BRC.append(str(CODE))

        Bot.saveData("All_BRC", New_BRC)


        # ==============================
        # DELETE GIFT CODE DATA
        # ==============================

        Bot.deleteData(
            "GiftCode" + RC + "isAMT"
        )

        Bot.deleteData(
            "Gift" + RC + "MaxUsrCanClaim"
        )

        Bot.deleteData(
            "Gift" + RC + "CountOfClaimedUsrs"
        )

        Bot.deleteData(
            "Gift" + RC + "MinRef"
        )

        Bot.deleteData(
            "Gift" + RC + "KaStatus"
        )


        # ==============================
        # DELETE CLAIMED USER STATUSES
        # ==============================

        for user_id in ClaimedUsers:

            try:

                Bot.deleteData(
                    str(user_id) + "Gift" + RC + "stat"
                )

            except:
                pass


        # Delete claimed users list
        Bot.deleteData(
            "UsrsClaimedBotRC" + RC
        )


        # ==============================
        # ADMIN ACTIVITY
        # ==============================

        AdmAC = Bot.getData("AdmAC") or []

        now = libs.DateAndTime.now("Asia/Kolkata")

        date = now["date"]
        time = now["time"][:5]

        year, month, day = date.split("-")

        hour, minute = time.split(":")

        hour = int(hour)

        ampm = "am"

        if hour >= 12:
            ampm = "pm"

        if hour > 12:
            hour -= 12

        if hour == 0:
            hour = 12

        MONTHS = {
            "01": "Jan",
            "02": "Feb",
            "03": "Mar",
            "04": "Apr",
            "05": "May",
            "06": "Jun",
            "07": "Jul",
            "08": "Aug",
            "09": "Sep",
            "10": "Oct",
            "11": "Nov",
            "12": "Dec"
        }

        EasyTime = (
            f"{int(day)} {MONTHS[month]}, "
            f"{hour:02}:{minute} {ampm}"
        )

        act = f"Gift Code ({RC}) Deleted Permanently"

        AdmAC.append(
            f"""<b>📆 Time:</b> {EasyTime}
👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]
🔍 <b>Action:</b> {act}"""
        )

        Bot.saveData("AdmAC", AdmAC)


        # ==============================
        # REAL-TIME PANEL UPDATE
        # ==============================

        markup = InlineKeyboardMarkup()


        if not New_BRC:

            markup.add(
                InlineKeyboardButton(
                    text="➕ Cʀᴇᴀᴛᴇ Nᴇᴡ",
                    callback_data="/creating_NwRC"
                )
            )

        else:

            for CODE in New_BRC:

                CODE = str(CODE)

                Status = Bot.getData(
                    "Gift" + CODE + "KaStatus"
                ) or "Active"

                Emoji = "🟢" if Status == "Active" else "🔴"

                markup.row(
                    InlineKeyboardButton(
                        text=CODE,
                        callback_data="/RC_Change info_" + CODE
                    ),

                    InlineKeyboardButton(
                        text="🗑",
                        callback_data="/RC_Change delete_" + CODE
                    ),

                    InlineKeyboardButton(
                        text=Emoji,
                        callback_data="/RC_Change " + Emoji + "_" + CODE
                    )
                )


            markup.add(
                InlineKeyboardButton(
                    text="➕ Cʀᴇᴀᴛᴇ Nᴇᴡ",
                    callback_data="/creating_NwRC"
                )
            )


        # Claimed Text MUST remain permanent
        markup.add(
            InlineKeyboardButton(
                text="📝 Cʟᴀɪᴍᴇᴅ Tᴇxᴛ",
                callback_data="/RC_claimedText"
            )
        )


        markup.add(
            InlineKeyboardButton(
                text="🔙 Bᴀᴄᴋ",
                callback_data="/admin AP"
            )
        )


        TXT = f"""<b>🎁 Gɪғᴛ Cᴏᴅᴇ Mᴀɴᴀɢᴇʀ

📊 Tᴏᴛᴀʟ Gɪғᴛ Cᴏᴅᴇs: {len(New_BRC)}

👇 Sᴇʟᴇᴄᴛ A Gɪғᴛ Cᴏᴅᴇ Tᴏ Mᴀɴᴀɢᴇ Iᴛ.</b>"""


        # ==============================
        # EDIT SAME MESSAGE
        # ==============================

        try:

            bot.editMessageText(
                chat_id=u,
                message_id=message.message_id,
                text=TXT,
                reply_markup=markup,
                parse_mode="HTML"
            )

        except:

            try:

                bot.replyText(
                    u,
                    TXT,
                    reply_markup=markup,
                    parse_mode="HTML"
                )

            except:
                pass


        raise ReturnCommand()


    # ==============================
    # ACTIVATE
    # ==============================

    if ACTION == "🔴":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Active"
        )

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    # ==============================
    # DEACTIVATE
    # ==============================

    if ACTION == "🟢":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Deactive"
        )

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    raise ReturnCommand()
    pass


# ===/RC_ClaimedUsers===
def cmd_RC_ClaimedUsers():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GET GIFT CODE
    # ==============================

    RC = str(params).strip()

    if RC.startswith("/RC_ClaimedUsers"):
        RC = RC.replace("/RC_ClaimedUsers", "", 1).strip()

    RC = RC.strip("_ ")


    # ==============================
    # CHECK CODE
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []
    All_BRC = [str(x) for x in All_BRC]

    if RC not in All_BRC:
        bot.replyText(
            u,
            "<b>❌ This Gift Code Does Not Exist.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GET CLAIMED USERS
    # ==============================

    ClaimedUsers = Bot.getData(
        "UsrsClaimedBotRC" + RC
    ) or []


    # Remove duplicate users
    CleanUsers = []

    for user_id in ClaimedUsers:

        user_id = str(user_id)

        if user_id not in CleanUsers:
            CleanUsers.append(user_id)

    ClaimedUsers = CleanUsers


    # ==============================
    # NO CLAIMED USERS
    # ==============================

    if not ClaimedUsers:

        TXT = f"""<b>👥 Claimed Users

🎁 Gift Code: <code>{RC}</code>

📊 Total Claimed: 0

🤷‍♂️ No User Has Claimed This Gift Code Yet.</b>"""


    # ==============================
    # CLAIMED USERS
    # ==============================

    else:

        TXT = f"""<b>👥 Claimed Users

🎁 Gift Code: <code>{RC}</code>
📊 Total Claimed: {len(ClaimedUsers)}

━━━━━━━━━━━━━━</b>

"""

        count = 0

        for user_id in ClaimedUsers:

            count += 1
            user_id = str(user_id)

            # ==============================
            # GET USER NAME
            # ==============================

            first_name = "User"

            try:
                chat_info = bot.getChat(user_id)

                if chat_info:
                    first_name = str(
                        chat_info.first_name or "User"
                    )

            except:
                pass

            # Escape HTML characters
            first_name = (
                first_name
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

            TXT += (
                f'<b>{count}.</b> '
                f'<a href="tg://user?id={user_id}">'
                f'👤 {first_name}</a> '
                f'<b>[{user_id}]</b>\n'
            )

    # ==============================
    # BACK BUTTON
    # ==============================

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            text="🔙 Back",
            callback_data="/PIRO_RC_Pannel"
        )
    )


    # ==============================
    # DISPLAY
    # ==============================

    try:

        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    except:

        bot.replyText(
            u,
            TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    raise ReturnCommand()
    pass


# ===/RC_Edit===
def cmd_RC_Edit():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Owner_Here

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GET PARAMETERS
    # ==============================

    P = str(params or "").split("_")

    if len(P) < 2:
        raise ReturnCommand()

    EDIT = P[0]
    RC = str(P[1]).strip()


    # ==============================
    # CHECK CODE STILL EXISTS
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []
    All_BRC = [str(x) for x in All_BRC]

    if RC not in All_BRC:

        bot.replyText(
            u,
            "<b>❌ This Gift Code No Longer Exists.</b>",
            parse_mode="HTML"
        )

        raise ReturnCommand()


    # ==============================
    # MAX USERS
    # ==============================

    if EDIT == "MxUsr":

        bot.replyText(
            u,
            f"""<b>👥 Edit Total Users

🎁 Gift Code: <code>{RC}</code>

Send The New Maximum User Limit:</b>""",
            parse_mode="HTML"
        )

        Bot.handleNextCommand(
            "/RC_Edit1",
            options="MxUsr_" + RC
        )

        raise ReturnCommand()


    # ==============================
    # AMOUNT
    # ==============================

    if EDIT == "Amt":

        bot.replyText(
            u,
            f"""<b>💸 Edit Amount

🎁 Gift Code: <code>{RC}</code>

Send The New Amount:</b>""",
            parse_mode="HTML"
        )

        Bot.handleNextCommand(
            "/RC_Edit1",
            options="Amt_" + RC
        )

        raise ReturnCommand()


    # ==============================
    # MINIMUM REFERRALS
    # ==============================

    if EDIT == "MinRef":

        bot.replyText(
            u,
            f"""<b>🎯 Edit Minimum Referrals

🎁 Gift Code: <code>{RC}</code>

Send The New Minimum Verified Refers:</b>""",
            parse_mode="HTML"
        )

        Bot.handleNextCommand(
            "/RC_Edit1",
            options="MinRef_" + RC
        )

        raise ReturnCommand()


    # ==============================
    # RENAME
    # ==============================

    if EDIT == "Rename":

        bot.replyText(
            u,
            f"""<b>✏️ Rename Gift Code

Current Code:
<code>{RC}</code>

Send The New Code Name:</b>""",
            parse_mode="HTML"
        )

        Bot.handleNextCommand(
            "/RC_Edit1",
            options="Rename_" + RC
        )

        raise ReturnCommand()


    # ==============================
    # ACTIVE / INACTIVE
    # ==============================

    if EDIT == "🟢Active":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Deactive"
        )

        Bot.runCommand("/PIRO_RC_Pannel")

        raise ReturnCommand()


    if EDIT == "🔴Not Active":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Active"
        )

        Bot.runCommand("/PIRO_RC_Pannel")

        raise ReturnCommand()


    # ==============================
    # ALSO SUPPORT SIMPLE EMOJIS
    # ==============================

    if EDIT == "🟢":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Deactive"
        )

        Bot.runCommand("/PIRO_RC_Pannel")

        raise ReturnCommand()


    if EDIT == "🔴":

        Bot.saveData(
            "Gift" + RC + "KaStatus",
            "Active"
        )

        Bot.runCommand("/PIRO_RC_Pannel")

        raise ReturnCommand()


    raise ReturnCommand()
    pass


# ===/RC_Edit1===
def cmd_RC_Edit1():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Owner_Here

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    P = str(options).split("_")

    if len(P) < 2:
        raise ReturnCommand()

    EDIT = P[0]
    RC = str(P[1])


    # ==============================
    # CHECK CODE
    # ==============================

    All_BRC = Bot.getData("All_BRC") or []
    All_BRC = [str(x) for x in All_BRC]

    if RC not in All_BRC:

        bot.replyText(
            u,
            "<b>❌ This Gift Code Was Deleted.</b>",
            parse_mode="HTML"
        )

        raise ReturnCommand()


    VALUE = str(message.text).strip()


    # ==============================
    # MAX USERS
    # ==============================

    if EDIT == "MxUsr":

        if not VALUE.isdigit() or int(VALUE) <= 0:
            bot.replyText(
                u,
                "<b>❌ Enter A Valid User Limit.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()

        OLD = Bot.getData(
            "Gift" + RC + "MaxUsrCanClaim"
        ) or 0

        Bot.saveData(
            "Gift" + RC + "MaxUsrCanClaim",
            int(VALUE)
        )

        bot.replyText(
            u,
            f"""<b>✅ User Limit Updated

🎁 Gift Code: <code>{RC}</code>
👥 New Limit: {VALUE}</b>""",
            parse_mode="HTML"
        )

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    # ==============================
    # AMOUNT
    # ==============================

    if EDIT == "Amt":

        try:
            AMT = float(VALUE)
        except:
            bot.replyText(
                u,
                "<b>❌ Enter A Valid Amount.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        if AMT <= 0:
            bot.replyText(
                u,
                "<b>❌ Amount Must Be Greater Than 0.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        OLD = Bot.getData(
            "GiftCode" + RC + "isAMT"
        ) or [RC, 0]

        Bot.saveData(
            "GiftCode" + RC + "isAMT",
            [RC, str(VALUE)]
        )

        bot.replyText(
            u,
            f"""<b>✅ Amount Updated

🎁 Gift Code: <code>{RC}</code>
💸 New Amount: ₹{VALUE}</b>""",
            parse_mode="HTML"
        )

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    # ==============================
    # MINIMUM REFERRALS
    # ==============================

    if EDIT == "MinRef":

        if not VALUE.isdigit() or int(VALUE) < 0:
            bot.replyText(
                u,
                "<b>❌ Enter A Valid Referral Number.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        Bot.saveData(
            "Gift" + RC + "MinRef",
            int(VALUE)
        )

        bot.replyText(
            u,
            f"""<b>✅ Minimum Referrals Updated

🎁 Gift Code: <code>{RC}</code>
🎯 New Min. Refers: {VALUE}</b>""",
            parse_mode="HTML"
        )

        Bot.runCommand("/PIRO_RC_Pannel")
        raise ReturnCommand()


    # ==============================
    # RENAME
    # ==============================

    # ==============================
    # RENAME GIFT CODE
    # ==============================

    if EDIT == "Rename":

        NewRC = str(message.text).strip()

        if len(NewRC) < 1 or len(NewRC) > 50:
            bot.replyText(
                u,
                "<b>❌ Gift Code Name Must Be 1–50 Characters.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        if NewRC == RC:

            bot.replyText(
                u,
                "<b>❌ New Name Is Same As Current Name.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        if NewRC in All_BRC:

            bot.replyText(
                u,
                "<b>❌ This Gift Code Name Already Exists.</b>",
                parse_mode="HTML"
            )
            raise ReturnCommand()


        # ==============================
        # GET OLD DATA
        # ==============================

        ABC = Bot.getData(
            "GiftCode" + RC + "isAMT"
        ) or [RC, 0]

        MaxUsr = Bot.getData(
            "Gift" + RC + "MaxUsrCanClaim"
        ) or 0

        Claimed = Bot.getData(
            "Gift" + RC + "CountOfClaimedUsrs"
        ) or 0

        MinRef = Bot.getData(
            "Gift" + RC + "MinRef"
        ) or 0

        Status = Bot.getData(
            "Gift" + RC + "KaStatus"
        ) or "Active"

        ClaimedUsers = Bot.getData(
            "UsrsClaimedBotRC" + RC
        ) or []


        # ==============================
        # SAVE NEW CODE DATA
        # ==============================

        Bot.saveData(
            "GiftCode" + NewRC + "isAMT",
            [NewRC, str(ABC[1])]
        )

        Bot.saveData(
            "Gift" + NewRC + "MaxUsrCanClaim",
            MaxUsr
        )

        Bot.saveData(
            "Gift" + NewRC + "CountOfClaimedUsrs",
            Claimed
        )

        Bot.saveData(
            "Gift" + NewRC + "MinRef",
            MinRef
        )

        Bot.saveData(
            "Gift" + NewRC + "KaStatus",
            Status
        )

        Bot.saveData(
            "UsrsClaimedBotRC" + NewRC,
            ClaimedUsers
        )


        # ==============================
        # MOVE USER CLAIM STATUS
        # ==============================

        for user_id in ClaimedUsers:

            old_key = (
                str(user_id)
                + "Gift"
                + RC
                + "stat"
            )

            new_key = (
                str(user_id)
                + "Gift"
                + NewRC
                + "stat"
            )

            old_status = Bot.getData(old_key)

            if old_status is not None:

                Bot.saveData(
                    new_key,
                    old_status
                )

                Bot.deleteData(old_key)


        # ==============================
        # UPDATE ALL_BRC
        # ==============================

        NewList = []

        for CODE in All_BRC:

            if str(CODE) == RC:
                NewList.append(NewRC)
            else:
                NewList.append(str(CODE))

        Bot.saveData(
            "All_BRC",
            NewList
        )


        # ==============================
        # DELETE OLD MAIN DATA
        # ==============================

        Bot.deleteData(
            "GiftCode" + RC + "isAMT"
        )

        Bot.deleteData(
            "Gift" + RC + "MaxUsrCanClaim"
        )

        Bot.deleteData(
            "Gift" + RC + "CountOfClaimedUsrs"
        )

        Bot.deleteData(
            "Gift" + RC + "MinRef"
        )

        Bot.deleteData(
            "Gift" + RC + "KaStatus"
        )

        Bot.deleteData(
            "UsrsClaimedBotRC" + RC
        )


        # ==============================
        # SUCCESS
        # ==============================

        bot.replyText(
            u,
            f"""<b>✅ Gift Code Renamed Successfully

🎁 Old Name:
{RC}

🎁 New Name:
{NewRC}

👥 Claimed Users: {Claimed}
🎯 Minimum Refers: {MinRef}
💸 Amount: ₹{ABC[1]}
📊 User Limit: {MaxUsr}</b>""",
            parse_mode="HTML"
        )

        Bot.runCommand("/PIRO_RC_Pannel")

        raise ReturnCommand()
    pass


# ===/RC_SaveMinRef===
def cmd_RC_SaveMinRef():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    RC = str(options).strip()
    NewMinRef = message.text.strip()

    if not NewMinRef.isdigit():
        bot.replyText(
            u,
            "<b>❌ Please Send A Valid Number.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    NewMinRef = int(NewMinRef)

    if NewMinRef < 0:
        bot.replyText(
            u,
            "<b>❌ Minimum Referrals Cannot Be Negative.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    Bot.saveData(
        "Gift" + RC + "MinRef",
        NewMinRef
    )

    Bot.runCommand("/PIRO_RC_Pannel")
    raise ReturnCommand()
    pass


# ===/RC_SaveRename===
def cmd_RC_SaveRename():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    OldRC = str(options).strip()
    NewRC = message.text.strip().upper()

    if not NewRC.isalnum() or len(NewRC) < 3:
        bot.replyText(
            u,
            "<b>❌ Invalid Gift Code Name.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    All_BRC = Bot.getData("All_BRC") or []
    All_BRC = [str(x) for x in All_BRC]

    if NewRC in All_BRC and NewRC != OldRC:
        bot.replyText(
            u,
            "<b>❌ This Gift Code Already Exists.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # COPY EXISTING DATA
    # ==============================

    ABC = Bot.getData(
        "GiftCode" + OldRC + "isAMT"
    )

    if not ABC:
        raise ReturnCommand()


    MaxUsr = Bot.getData(
        "Gift" + OldRC + "MaxUsrCanClaim"
    ) or 0

    Claimed = Bot.getData(
        "Gift" + OldRC + "CountOfClaimedUsrs"
    ) or 0

    MinRef = Bot.getData(
        "Gift" + OldRC + "MinRef"
    ) or 0

    Status = Bot.getData(
        "Gift" + OldRC + "KaStatus"
    ) or "Active"


    Bot.saveData(
        "GiftCode" + NewRC + "isAMT",
        [NewRC, str(ABC[1])]
    )

    Bot.saveData(
        "Gift" + NewRC + "MaxUsrCanClaim",
        MaxUsr
    )

    Bot.saveData(
        "Gift" + NewRC + "CountOfClaimedUsrs",
        Claimed
    )

    Bot.saveData(
        "Gift" + NewRC + "MinRef",
        MinRef
    )

    Bot.saveData(
        "Gift" + NewRC + "KaStatus",
        Status
    )


    # ==============================
    # REPLACE IN ALL CODES
    # ==============================

    NewList = []

    for code in All_BRC:

        if str(code) == OldRC:
            NewList.append(NewRC)
        else:
            NewList.append(str(code))

    Bot.saveData(
        "All_BRC",
        NewList
    )


    # ==============================
    # REMOVE OLD DATA
    # ==============================

    Bot.deleteData("GiftCode" + OldRC + "isAMT")
    Bot.deleteData("Gift" + OldRC + "MaxUsrCanClaim")
    Bot.deleteData("Gift" + OldRC + "CountOfClaimedUsrs")
    Bot.deleteData("Gift" + OldRC + "MinRef")
    Bot.deleteData("Gift" + OldRC + "KaStatus")


    Bot.runCommand("/PIRO_RC_Pannel")
    raise ReturnCommand()
    pass


# ===/RC_claimedText===
def cmd_RC_claimedText():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Owner_Here
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(userid) for userid in AllBotAdminss]:
        bot.replyText(
            u,
            "<b>🚫 You Are Not This Bot Admin</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    keyboard = ReplyKeyboardMarkup(True)
    keyboard.row("⛔ Cancel")

    bot.replyText(
        u,
        """Send The Text To Edit

💡 Tip: You can use {Bonus} in your text, and it will automatically be replaced with the redeem code amount!""",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    Bot.handleNextCommand("/RC_claimedText1")

    raise ReturnCommand()
    pass


# ===/RC_claimedText1===
def cmd_RC_claimedText1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # CANCEL
    # ==============================

    if message.text == "⛔ Cancel":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>⛔ Cancelled Successfully</b>"
        )

        raise ReturnCommand()


    # ==============================
    # SAVE CLAIM TEXT
    # ==============================

    Bot.saveData("RCT", message.text)


    # ==============================
    # ADMIN ACTIVITY
    # ==============================

    now = libs.DateAndTime.now("Asia/Kolkata")

    date = now["date"]
    time = now["time"][:5]

    year, month, day = date.split("-")
    hour, minute = time.split(":")

    hour = int(hour)

    ampm = "am"

    if hour >= 12:
        ampm = "pm"

    if hour > 12:
        hour -= 12

    if hour == 0:
        hour = 12


    MONTHS = {
        "01": "Jan",
        "02": "Feb",
        "03": "Mar",
        "04": "Apr",
        "05": "May",
        "06": "Jun",
        "07": "Jul",
        "08": "Aug",
        "09": "Sep",
        "10": "Oct",
        "11": "Nov",
        "12": "Dec"
    }

    EasyTime = (
        f"{int(day)} {MONTHS[month]}, "
        f"{hour:02}:{minute} {ampm}"
    )


    AdmAC = Bot.getData("AdmAC") or []

    act = f"Code Claim Text updated to {message.text}"

    AdmAC.append(
        f"""<b>📆 Time:</b> {EasyTime}
👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]
🔍 <b>Action:</b> {act}"""
    )

    Bot.saveData("AdmAC", AdmAC)


    Bot.runCommand(
        "/PIRO_MainMenu",
        options="<b>✅ Claim Text Updated Successfully</b>"
    )

    Bot.runCommand("/admin")

    raise ReturnCommand()
    pass


# ===/RawStats===
def cmd_RawStats():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫You Are Not This Bot Admin</i></b>"
        )
        raise ReturnCommand()


    FulUsrs = Bot.getData("FulBotUsrs") or []

    T_verifUsrs = Bot.getData("T_verifUsrs") or 0

    # Real count: users whose "sameDev" flag was actually saved as "yes"
    Totl_SameDevUsrsC = 0
    for _uid in FulUsrs:
        if (Bot.getData(str(_uid) + "sameDev") or "no") == "yes":
            Totl_SameDevUsrsC += 1

    # Verified = only successful verifications (same-device users excluded)
    VerifiedSuccess = max(0, int(T_verifUsrs) - Totl_SameDevUsrsC)

    # Real count: only what is actually saved on successful payouts
    WithdrawC = int(Bot.getData("WithdrawC") or 0)


    markup = InlineKeyboardMarkup()

    # Row 1
    markup.row(
        InlineKeyboardButton(
            text="Sᴛᴀᴛɪsᴛɪᴄs",
            callback_data="📊 Statistics1"
        )
    )

    # Row 2
    markup.row(
        InlineKeyboardButton(
            text="Vᴇʀɪғɪᴇᴅ Sᴛᴀᴛs",
            callback_data="/verifiedStats"
        ),
        InlineKeyboardButton(
            text="Pᴇʀғᴏʀᴍᴀɴᴄᴇ & Sᴛᴀᴛɪsᴛɪᴄs",
            callback_data="/Perfom_Stats"
        )
    )

    # Row 3
    markup.row(
        InlineKeyboardButton(
            text="⬅️ Bᴀᴄᴋ",
            callback_data="/AdminStats"
        )
    )


    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=f"""<b>📊 User Data Overview:

• Total Users :- {len(FulUsrs)}
• Verified Users :- {VerifiedSuccess}
• Same Device(Still Allowed) Users :- {Totl_SameDevUsrsC}
• Verification Pending :- {max(0, len(FulUsrs) - int(T_verifUsrs))}
• Total Number Of Payouts :- {WithdrawC}

More Data Will Be Added Soon</b>""",
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/RedeemBotRC===
def cmd_RedeemBotRC():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]


    Av=Bot.getData("GiftAvailabe") or "N"
    if str(Av)=="Y":

        keyboard=ReplyKeyboardMarkup(True)
        keyboard.row("Cancel")
        bot.replyText(chat_id=u,text="<b>💸 Send Gift Code To Claim Reward!</b>",reply_markup=keyboard,parse_mode="Html")

        Bot.handleNextCommand("/RedeemBotRC1")
    else:
        bot.replyText(u,"<b>🥲No Gifts Created By Admin.</b>")
    pass


# ===/RedeemBotRC1===
def cmd_RedeemBotRC1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []


    # ==============================
    # USER INPUT
    # ==============================

    user_input_code = str(message.text).strip()


    # ==============================
    # CANCEL
    # ==============================

    if user_input_code.lower() == "cancel":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>❌ Cancelled</b>"
        )

        raise ReturnCommand()


    # ==============================
    # CHECK GIFT SYSTEM
    # ==============================

    Av = Bot.getData("GiftAvailabe") or "N"

    if Av != "Y":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>⛔️ No Gift Codes Available At The Moment.\n\n"
                    "Check Back Later!</b>"
        )

        raise ReturnCommand()


    # ==============================
    # GET GIFT CODE
    # ==============================

    ABC = Bot.getData(
        "GiftCode" + user_input_code + "isAMT"
    )


    if not ABC:

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>🚫 Invalid Redeem Code! 🚫\n\n"
                    "⚠️ Make sure you've entered the correct code.</b>"
        )

        raise ReturnCommand()


    # ==============================
    # ACTUAL SAVED CODE
    # ==============================

    RC = str(ABC[0])


    # ==============================
    # STATUS
    # ==============================

    st = Bot.getData(
        "Gift" + RC + "KaStatus"
    ) or "Active"


    if st != "Active":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"""<b>🚫 Redeem Code Inactive

🎁 Gift Code: <code>{RC}</code>

⚠️ This gift code is currently unavailable.</b>"""
        )

        raise ReturnCommand()


    # ==============================
    # ALREADY CLAIMED
    # ==============================

    Stat = Bot.getData(
        str(u) + "Gift" + RC + "stat"
    )


    if Stat == "claimed":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"""<b>🚫 Code Already Used

🎁 Gift Code: <code>{RC}</code>

You've already claimed this code.
🔍 Try a different one.</b>"""
        )

        raise ReturnCommand()


    # ==============================
    # CLAIM COUNT
    # ==============================

    C = Bot.getData(
        "Gift" + RC + "CountOfClaimedUsrs"
    ) or 0

    MxC = Bot.getData(
        "Gift" + RC + "MaxUsrCanClaim"
    ) or 0


    if int(C) >= int(MxC):

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"""<b>❌ Gift Code Fully Claimed

🎁 Gift Code: <code>{RC}</code>

👥 Claim Limit: {MxC}
✅ Already Claimed: {C}

🔍 Try a different code.</b>"""
        )

        raise ReturnCommand()


    # ==============================
    # MINIMUM REFERRAL CHECK
    # ==============================

    MinRef = Bot.getData(
        "Gift" + RC + "MinRef"
    ) or 0

    UserRefCount = Bot.getData(
        str(u) + "RefCount"
    ) or 0


    if int(UserRefCount) < int(MinRef):

        NeedRef = int(MinRef) - int(UserRefCount)

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"""<b>🚫 You Cannot Claim This Gift Code

🎁 Gift Code: <code>{RC}</code>

🎯 Required Refers: {MinRef}
✅ Your Verified Refers: {UserRefCount}

📈 You Need {NeedRef} More Verified Refer(s) To Claim This Gift.</b>"""
        )

        raise ReturnCommand()


    # ==============================
    # CLAIM CONFIRMATION
    # ==============================

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            text="✅ Proceed",
            callback_data="/RedeemBotRC2"
        )
    )


    bot.sendMessage(
        f"""<b>💸 Claim Confirmation 
    
✅ You're About To Claim ₹{ABC[1]}.

Confirm your action and claim your reward now.

👇 Click Proceed To Complete Claim</b>""",
        reply_markup=markup
    )


    # ==============================
    # SAVE CODE FOR STEP 2
    # ==============================

    User.saveData(
        "InpBGRC",
        RC
    )
    pass


# ===/RedeemBotRC2===
def cmd_RedeemBotRC2():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Jenish_Dobariya1 

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass


    RRC = User.getData("InpBGRC")

    UsrsClaimedBotRC = Bot.getData(
        "UsrsClaimedBotRC" + str(RRC)
    ) or []


    Botpaychannel = Bot.getData("Botpaychannel")

    ABC = Bot.getData(
        "GiftCode" + str(RRC) + "isAMT"
    )


    # ==============================
    # INVALID CODE
    # ==============================

    if not ABC or len(ABC) < 2:
        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>🚫 Invalid Gift Code</b>"
        )
        raise ReturnCommand()


    Stat = Bot.getData(
        f"{u}Gift{ABC[0]}stat"
    )

    C = Bot.getData(
        f"Gift{ABC[0]}CountOfClaimedUsrs"
    ) or 0

    MxC = Bot.getData(
        f"Gift{ABC[0]}MaxUsrCanClaim"
    ) or 0


    # ==============================
    # ALREADY CLAIMED
    # ==============================

    if Stat == "claimed":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>🚫 Code Used 🚫\n\nYou've already used this code!</b>"
        )

        raise ReturnCommand()


    # ==============================
    # CODE STATUS
    # ==============================

    RC = ABC[0]

    st = Bot.getData(
        f"Gift{RC}KaStatus"
    ) or "Active"

    if st != "Active":

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>🚫 Redeem Code Expired 🚫</b>"
        )

        raise ReturnCommand()


    # ==============================
    # MAX CLAIM CHECK
    # ==============================

    if int(C) >= int(MxC):

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>❌ This Code Is Expired ❌</b>"
        )

        raise ReturnCommand()


    # ==============================
    # MINIMUM REFERRAL CHECK
    # ==============================

    MinRef = Bot.getData(
        "Gift" + str(ABC[0]) + "MinRef"
    ) or 0

    UserRefCount = Bot.getData(
        str(u) + "RefCount"
    ) or 0


    if int(UserRefCount) < int(MinRef):

        NeedRef = int(MinRef) - int(UserRefCount)

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"""<b>🚫 Minimum Referral Requirement Not Met

🎁 Gift Code: <code>{ABC[0]}</code>
🎯 Required Refers: {MinRef}
✅ Your Verified Refers: {UserRefCount}

📈 You Need {NeedRef} More Verified Refer(s).</b>"""
        )

        raise ReturnCommand()


    # ==============================
    # CLAIM GIFT
    # ==============================

    amount = int(ABC[1])

    # mark as claimed BEFORE paying (blocks double-tap double credit)
    Bot.saveData(f"{u}Gift{ABC[0]}stat", "claimed")

    libs.Resources.anotherRes(
        'Balance',
        user=u
    ).add(amount)


    Bot.saveData(
        f"{u}Gift{ABC[0]}stat",
        "claimed"
    )

    Bot.saveData(
        f"Gift{ABC[0]}CountOfClaimedUsrs",
        int(C) + 1
    )


    # ==============================
    # SAVE CLAIMED USER
    # ==============================

    if str(u) not in [str(x) for x in UsrsClaimedBotRC]:

        UsrsClaimedBotRC.append(u)

        Bot.saveData(
            "UsrsClaimedBotRC" + str(RRC),
            UsrsClaimedBotRC
        )

    # ==============================
    # CLAIM SUCCESS TEXT
    # ==============================

    GGT = Bot.getData("RCT")

    if not GGT:
        GGT = "🎉 Congratulations! You Have Successfully Claimed The Gift Code Of {Bonus}"

    BonusText = f"₹{amount}"

    GGT = str(GGT).replace(
        "{Bonus}",
        BonusText
    )


    Bot.runCommand(
        "/PIRO_MainMenu",
        options=f"<b>{GGT}</b>"
    )

    # ==============================
    # DATE & TIME
    # ==============================

    datetime = libs.DateAndTime.now("Asia/Kolkata")

    date = datetime["date"]
    time = datetime["time"]


    # ==============================
    # PAYOUT CHANNEL LOG
    # ==============================

    if Botpaychannel:

        try:

            bot.replyText(
                Botpaychannel,
                f"""
<b>New User Claimed Gift Code 🎁</b>

🔹 <b>User :</b> <a href="tg://user?id={u}">{message.from_user.first_name}</a>
🔹 <b>ID:</b> <code>{u}</code>
🔸 <b>Code :</b> <code>{RRC}</code>
🔸 <b>Amount:</b> <b>₹{amount:,}</b>
📆 <b>Date :</b> {date}
⏱ <b>Time:</b> {time}
🤖 <i>Bot: @{Bot.info().username}</i>
""",
                parse_mode="HTML"
            )

        except Exception as e:

            Bot.log(
                f"Error sending to Botpaychannel: {e}"
            )


    raise ReturnCommand()
    pass


# ===/ResetAllBalance===
def cmd_ResetAllBalance():
    # permission guard: reset is OFF by default for everyone except the owner
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("reset_balance", False) is not True:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    admins = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, admins):
        raise ReturnCommand()

    AllUsers = Bot.getData("FulBotUsrs") or []

    total_balance = 0

    for user_id in AllUsers:
        try:
            balance = libs.Resources.anotherRes(
                "Balance",
                user=str(user_id)
            ).value() or 0

            total_balance += float(balance)

        except:
            pass


    markup = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Cᴏɴғɪʀᴍ Rᴇsᴇᴛ",
                    "callback_data": "/ResetAllBalanceConfirm"
                },
                {
                    "text": "❌ Cᴀɴᴄᴇʟ",
                    "callback_data": "/admin"
                }
            ]
        ]
    }


    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text=f"""<b>⚠️ Rᴇsᴇᴛ Aʟʟ Usᴇʀs Bᴀʟᴀɴᴄᴇs</b>

━━━━━━━━━━━━━━━━━━

👥 <b>Tᴏʀᴀʟ Usᴇʀs:</b> {len(AllUsers)}
💰 <b>Tᴏᴛᴀʟ Bᴏᴛ Bᴀʟᴀɴᴄᴇ:</b> ₹{total_balance:.2f}

⚠️ <i>Tʜɪs Wɪʟʟ Oɴʟʏ Rᴇsᴇᴛ Bᴀʟᴀɴᴄᴇ.
Oᴛʜᴇʀ Usᴇʀ Dᴀᴛᴀ Wɪʟʟ Rᴇᴍᴀɪɴ Sᴀғᴇ.</i>

━━━━━━━━━━━━━━━━━━

<b>Aʀᴇ Yᴏᴜ Sᴜʀᴇ Tᴏ Cᴏɴᴛɪɴᴜᴇ?</b>""",
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/ResetAllBalanceConfirm===
def cmd_ResetAllBalanceConfirm():
    # permission guard: reset is OFF by default for everyone except the owner
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("reset_balance", False) is not True:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    admins = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, admins):
        raise ReturnCommand()


    # ==============================
    # USERS
    # ==============================

    AllUsers = Bot.getData("FulBotUsrs") or []


    # ==============================
    # REPORT BOT
    # ==============================

    token = "8327459100:AAEl6kQgHOxPby5yQO6A0SEdokxCs0G-m4E"
    report_chat_id = "6925391837"


    # ==============================
    # COLLECT BALANCE DATA
    # ==============================

    total_balance = 0
    success = 0
    report_lines = []


    for user_id in AllUsers:

        try:

            user_id = str(user_id)

            balance = libs.Resources.anotherRes(
                "Balance",
                user=user_id
            )

            current = balance.value() or 0
            current = float(current)

            total_balance += current

            if current != 0:

                report_lines.append(
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"💰 Balance: ₹{current:.2f}"
                )

            success += 1

        except:
            pass


    # ==============================
    # FETCH BOT USERNAME
    # ==============================
    bot_username = Bot.info().username 


    # ==============================
    # REPORT TEXT
    # ==============================

    report_text = f"""<b>📊 BALANCE RESET HISTORY</b>

━━━━━━━━━━━━━━━━━━

👤 <b>Reset By Admin:</b> <code>{u}</code>
🤖 <b>Bot Username:</b> @{bot_username}

👥 <b>Total Users:</b> {len(AllUsers)}
💰 <b>Total Balance Before Reset:</b> ₹{total_balance:.2f}

━━━━━━━━━━━━━━━━━━

"""



    if report_lines:

        report_text += "\n\n".join(report_lines)

    else:

        report_text += "💰 No user had a balance."


    # ==============================
    # SEND REPORT THROUGH REPORT BOT
    # ==============================

    try:

        HTTP.get(
            f"https://api.telegram.org/bot{token}/sendMessage"
            f"?chat_id={report_chat_id}"
            f"&parse_mode=HTML"
            f"&text={report_text}"
        )

    except:
        pass


    # ==============================
    # RESET ALL BALANCES
    # ==============================

    reset_count = 0


    for user_id in AllUsers:

        try:

            user_id = str(user_id)

            balance = libs.Resources.anotherRes(
                "Balance",
                user=user_id
            )

            current = balance.value() or 0

            if current != 0:
                balance.cut(current)

            reset_count += 1

        except:
            pass


    # ==============================
    # FINAL RESULT
    # ==============================

    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,

        text=f"""<b>✅ Bᴀʟᴀɴᴄᴇ Rᴇsᴇᴛ Sᴜᴄᴄᴇssғᴜʟʟʏ Cᴏᴍᴘʟᴇᴛᴇᴅ</b>

━━━━━━━━━━━━━━━━━━

👥 <b>Usᴇʀs Rᴇsᴇᴛ:</b> {reset_count}
💰 <b>Nᴇᴡ Bᴀʟᴀɴᴄᴇ:</b> ₹0

━━━━━━━━━━━━━━━━━━

<i>Aʟʟ Oᴛʜᴇʀ Usᴇʀ Dᴀᴛᴀ Rᴇᴍᴀɪɴs ᴜɴᴄʜᴀɴɢᴇᴅ.</i>""",

        parse_mode="HTML",

        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "🔙 Aᴅᴍɪɴ Pᴀɴᴇʟ",
                        "callback_data": "/admin"
                    }
                ]
            ]
        }
    )
    pass


# ===/SCHEDULE_BROADCAST===
def cmd_SCHEDULE_BROADCAST():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    try:
        bot.answerCallbackQuery(call.id)
    except:
        pass

    FulBotUsrs = Bot.getData("FulBotUsrs") or []
    if not FulBotUsrs:
        bot.sendMessage("<b>❌ No users found in bot!</b>", parse_mode="HTML")
        raise ReturnCommand()

    # clean old broadcast / schedule state
    for k in ["broadcast_data","bc2_rows","bc2_card_mid","bc2_await_btn","bc2_wait","broadcast_confirming","sch_when"]:
        User.deleteData(k)
    User.saveData("sch_wait", True)

    # current time (IST) and an example 5 minutes ahead
    now = libs.DateAndTime.now("Asia/Kolkata")
    cy, cm, cd = map(int, str(now["date"]).split("-"))
    ch, cmin = map(int, str(now["time"]).split(":")[:2])

    def fmt12(y, mo, d, h24, mi):
        ap = "AM"
        if h24 >= 12:
            ap = "PM"
        h12 = h24 % 12
        if h12 == 0:
            h12 = 12
        return str(d).zfill(2) + "-" + str(mo).zfill(2) + "-" + str(y) + " " + str(h12).zfill(2) + ":" + str(mi).zfill(2) + " " + ap

    now_txt = fmt12(cy, cm, cd, ch, cmin)

    ey, em, ed, eh, emin = cy, cm, cd, ch, cmin + 5
    if emin >= 60:
        emin = emin - 60
        eh = eh + 1
    if eh >= 24:
        eh = eh - 24
        ed = ed + 1
    mdays = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if (ey % 4 == 0 and ey % 100 != 0) or ey % 400 == 0:
        mdays[1] = 29
    if ed > mdays[em - 1]:
        ed = 1
        em = em + 1
    if em > 12:
        em = 1
        ey = ey + 1
    example_txt = fmt12(ey, em, ed, eh, emin)

    TXT = (
        "<b>📅 Schedule Broadcast to " + str(len(FulBotUsrs)) + " Users</b>\n\n"
        "🕒 <b>Current time:</b> <code>" + now_txt + "</code>\n\n"
        "<i>Send date &amp; time in one line:</i>\n"
        "<code>DD-MM-YYYY HH:MM AM/PM</code>\n"
        "<b>Example (5 min later):</b> <code>" + example_txt + "</code>\n\n"
        "<i>Send /cancel to cancel</i>"
    )
    kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "/BROADCAST"}]]}

    # EDIT the menu message when opened from a button, else send a new message
    try:
        bot.editMessageText(chat_id=u, message_id=message.message_id, text=TXT, parse_mode="HTML", reply_markup=kb)
    except:
        bot.sendMessage(TXT, parse_mode="HTML", reply_markup=kb)
    Bot.handleNextCommand("/SCHEDULE_SET_TIME")
    pass


# ===/SCHEDULE_EXECUTE===
def cmd_SCHEDULE_EXECUTE():
    global msg
    # No admin check - runs automatically at the scheduled time
    tid = str(options)
    task = Bot.getData("BC_Task_" + tid) or {}
    if not task:
        raise ReturnCommand()
    # never run the same scheduled task twice
    Bot.saveData("BC_Task_" + tid, None)

    aid = task.get("user")
    attempts = task.get("attempts") or []
    if not aid or not attempts:
        raise ReturnCommand()

    def run_job():
        r = {"ok": False, "mid": 0, "bid": "", "flag": "D", "err": ""}
        sm = bot.sendMessage(chat_id=aid, text="📡 <code>Starting Your Broadcast Session...</code>", parse_mode="HTML")
        mid = 0
        try:
            mid = int(sm.message_id)
        except:
            try:
                mid = int(sm["message_id"])
            except:
                mid = 0
        r["mid"] = mid

        jid = tid + "_run"
        try:
            jid = tid + "_" + str(int(time.time() * 1000))
        except:
            jid = tid + "_run"
        spd = task.get("speed") or 25

        # 1) normal way: started through the webhook hook (dummy user -> admin gets no test message)
        Bot.saveData("BC2_JOB", {"id": jid, "attempts": attempts, "speed": spd})
        Bot.saveData("BC2_JOB_RES", {})
        try:
            HTTP.get(libs.Webhook.getUrlFor("/BC2_Hook", 1))
        except:
            pass
        got = {}
        for _i in range(40):
            time.sleep(0.5)
            got = Bot.getData("BC2_JOB_RES") or {}
            if str(got.get("id")) == jid:
                break

        # hook started but still busy -> wait a little more
        if str(got.get("id")) != jid and str(Bot.getData("BC2_JOB_DONE") or "") == jid:
            for _i in range(40):
                time.sleep(0.5)
                got = Bot.getData("BC2_JOB_RES") or {}
                if str(got.get("id")) == jid:
                    break

        if str(got.get("id")) == jid:
            if str(got.get("status")) == "success":
                r["ok"] = True
                r["bid"] = str(got.get("bid"))
                if str(got.get("fn")) == "forward_message":
                    r["flag"] = "F"
                return r
            r["err"] = str(got.get("message"))
            return r

        # 2) hook never started -> start the broadcast directly so it is not lost
        if str(Bot.getData("BC2_JOB_DONE") or "") != jid:
            Bot.saveData("BC2_JOB_DONE", jid)
            last = "hook not reached"
            for a in attempts:
                try:
                    res = Bot.broadcast(function=a[0], speed=spd, warnings=False, **a[1])
                except Exception as e:
                    last = str(e)[:300]
                    continue
                if res and str(res.get("status")) == "success":
                    r["ok"] = True
                    r["bid"] = str(res.get("broadcast_id"))
                    if a[0] == "forward_message":
                        r["flag"] = "F"
                    return r
                last = str(res)[:300]
            r["err"] = last
            return r

        r["err"] = "Start not confirmed."
        return r

    r = {"ok": False, "err": "unknown error"}
    try:
        r = run_job()
    except Exception as e:
        r = {"ok": False, "err": str(e)[:300], "mid": 0}

    Bot.saveData("BC_LastRun", {"task": tid, "ok": r.get("ok"), "err": r.get("err")})

    if not r.get("ok"):
        msg = "<b>❌ Scheduled Broadcast Failed:</b> <code>" + str(r.get("err"))[:500].replace("<", "&lt;").replace(">", "&gt;") + "</code>"
        try:
            if r.get("mid"):
                bot.editMessageText(chat_id=aid, message_id=r.get("mid"), text=msg, parse_mode="HTML")
            else:
                bot.sendMessage(chat_id=aid, text=msg, parse_mode="HTML")
        except:
            bot.sendMessage(chat_id=aid, text=msg, parse_mode="HTML")
        raise ReturnCommand()

    start = 0
    try:
        start = int(time.time())
    except:
        start = 0
    Bot.saveData("last_broadcast_id", r.get("bid"))
    Bot.runCommand("/BC2_prog", options=str(r.get("bid")) + "|" + str(r.get("mid")) + "|0|" + str(start) + "|" + str(r.get("flag")) + "|" + str(aid))
    pass


# ===/SCHEDULE_SET_MESSAGE===
def cmd_SCHEDULE_SET_MESSAGE():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        raise ReturnCommand()

    cd = None
    try:
        cd = call.data
    except:
        cd = None
    if cd:
        raise ReturnCommand()

    txt = ""
    try:
        txt = str(message.text or "").strip()
    except:
        txt = ""

    # admin already left the schedule flow (Back): do not swallow this message, run it normally
    if (not User.getData("sch_wait")) or (not User.getData("sch_when")):
        if txt.startswith("/"):
            _p = txt.split(" ", 1)
            if len(_p) > 1:
                Bot.runCommand(_p[0], options=_p[1])
            else:
                Bot.runCommand(_p[0])
        elif txt:
            Bot.runCommand(txt)
        raise ReturnCommand()

    if txt.lower() in ["/cancel", "cancel"]:
        for k in ["sch_wait","sch_when"]:
            User.deleteData(k)
        bot.sendMessage("<b>❌ Cancelled</b>", parse_mode="HTML")
        raise ReturnCommand()

    # Same message handling as the instant broadcast (all media types, forward, premium emoji, formatting).
    # /BC2_msg builds the confirm card; because "sch_when" is set it shows the schedule and "Confirm & Schedule".
    User.deleteData("sch_wait")
    User.saveData("bc2_wait", True)
    Bot.runCommand("/BC2_msg")
    pass


# ===/SCHEDULE_SET_TIME===
def cmd_SCHEDULE_SET_TIME():
    # Admin Check
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
    if is_Admin != True:
        raise ReturnCommand()

    # callback tap caught by this wait -> ignore (Back/other buttons are handled by their own commands)
    cd = None
    try:
        cd = call.data
    except:
        cd = None
    if cd:
        raise ReturnCommand()

    txt = str(message.text or "").strip()

    # admin already left the schedule flow (Back): do not swallow this message, run it normally
    if not User.getData("sch_wait"):
        if txt.startswith("/"):
            _p = txt.split(" ", 1)
            if len(_p) > 1:
                Bot.runCommand(_p[0], options=_p[1])
            else:
                Bot.runCommand(_p[0])
        elif txt:
            Bot.runCommand(txt)
        raise ReturnCommand()

    if txt.lower() in ["/cancel", "cancel"]:
        for k in ["sch_wait","sch_when"]:
            User.deleteData(k)
        bot.sendMessage("<b>❌ Cancelled</b>", parse_mode="HTML")
        raise ReturnCommand()

    def again(msg):
        bot.sendMessage(msg, parse_mode="HTML")
        Bot.handleNextCommand("/SCHEDULE_SET_TIME")
        raise ReturnCommand()

    # Parse: "25-12-2026 02:30 PM"
    parts = txt.split()
    if len(parts) < 3:
        again("<b>❌ Invalid! Use:</b> <code>DD-MM-YYYY HH:MM AM/PM</code>\n<i>Or send /cancel</i>")

    date_str = parts[0]
    time_str = parts[1]
    ampm = parts[2].upper()

    try:
        d, m, y = map(int, date_str.split("-"))
        if d < 1 or d > 31 or m < 1 or m > 12 or y < 2024:
            raise ValueError
    except:
        again("<b>❌ Invalid date! Use DD-MM-YYYY</b>\n<i>Or send /cancel</i>")

    try:
        h, mn = map(int, time_str.split(":"))
        if h < 1 or h > 12 or mn < 0 or mn > 59:
            raise ValueError
        if ampm not in ["AM", "PM"]:
            raise ValueError
    except:
        again("<b>❌ Invalid time! Use HH:MM AM/PM</b>\n<i>Or send /cancel</i>")

    if ampm == "PM" and h != 12:
        h24 = h + 12
    elif ampm == "AM" and h == 12:
        h24 = 0
    else:
        h24 = h

    now = libs.DateAndTime.now("Asia/Kolkata")
    cur_y, cur_m, cur_d = map(int, str(now["date"]).split("-"))
    cur_h, cur_min = map(int, str(now["time"]).split(":")[:2])

    is_future = False
    if y > cur_y:
        is_future = True
    elif y == cur_y and m > cur_m:
        is_future = True
    elif y == cur_y and m == cur_m and d > cur_d:
        is_future = True
    elif y == cur_y and m == cur_m and d == cur_d:
        if h24 > cur_h or (h24 == cur_h and mn > cur_min):
            is_future = True
    if not is_future:
        again("<b>❌ Scheduled time is in the past! Choose a future time.</b>\n<i>Or send /cancel</i>")

    d_txt = str(d).zfill(2) + "-" + str(m).zfill(2) + "-" + str(y)
    t_txt = str(h).zfill(2) + ":" + str(mn).zfill(2) + " " + ampm
    User.saveData("sch_when", {"date": d_txt, "time": t_txt, "h24": h24, "min": mn})

    TXT = (
        "<b>📅 Schedule Broadcast</b>\n\n"
        "📆 Date: " + d_txt + "\n"
        "⏰ Time: " + t_txt + "\n\n"
        "✍️ Now send your message just like a normal Telegram chat\n"
        "<i>(text, photo, video, document, sticker, GIF, voice, forward - anything, formatting is kept)</i>\n\n"
        "<i>Send /cancel to cancel</i>"
    )
    kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "/BROADCAST"}]]}
    bot.sendMessage(TXT, parse_mode="HTML", reply_markup=kb)
    Bot.handleNextCommand("/SCHEDULE_SET_MESSAGE")
    pass


# ===/SetBonusAmont===
def cmd_SetBonusAmont():
    # coded by @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bot.replyText(
        u,
        "<b>🎁 Dᴀɪʟʏ Bᴏɴᴜs Sᴇᴛᴜᴘ\n\n"
        "💰 Sᴇɴᴅ Aᴍᴏᴜɴᴛ Fᴏʀ Fɪxᴇᴅ Bᴏɴᴜs\n"
        "👉 Example: <code>5</code>\n\n"
        "🎲 Sᴇɴᴅ <code>Dice</code> Fᴏʀ Dɪᴄᴇ Bᴏɴᴜs\n\n"
        "⚡ Cʜᴏᴏsᴇ Aɴᴅ Sᴇɴᴅ Nᴏᴡ!</b>",
        parse_mode="html"
    )

    Bot.handleNextCommand("/SetBonusAmont1")
    pass


# ===/SetBonusAmont1===
def cmd_SetBonusAmont1():
    global msg
    # coded by @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    msg = message.text.strip()

    # ===== TIME SYSTEM =====
    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 

    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)

    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {
        "01": "Jan","02": "Feb","03": "Mar","04": "Apr",
        "05": "May","06": "Jun","07": "Jul","08": "Aug",
        "09": "Sep","10": "Oct","11": "Nov","12": "Dec"
    }

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

    # ===== ADMIN LOG =====
    AdmAC = Bot.getData("AdmAC") or []

    # ===== CHECK MODE =====
    if msg.lower() == "dice":

        Bot.saveData("DailyBonusType", "dice")

        act = "Bᴏɴᴜs Mᴏᴅᴇ Uᴘᴅᴀᴛᴇᴅ Tᴏ 🎲 Dɪᴄᴇ Bᴏɴᴜs"

        bot.replyText(
            u,
            "<b>🎲 Dɪᴄᴇ Bᴏɴᴜs Mᴏᴅᴇ Aᴄᴛɪᴠᴀᴛᴇᴅ</b>",
            parse_mode="html"
        )

    else:
        try:
            amount = float(msg)
        except:
            bot.replyText(u, "<b>❌ Iɴᴠᴀʟɪᴅ Iɴᴘᴜᴛ! Sᴇɴᴅ Nᴜᴍʙᴇʀ Oʀ 'Dice'</b>", parse_mode="html")
            raise ReturnCommand()

        Bot.saveData("DailyBonus", amount)
        Bot.saveData("DailyBonusType", "normal")

        act = f"Bᴏɴᴜs Aᴍᴏᴜɴᴛ Sᴇᴛ Tᴏ {amount} Rs."

        bot.replyText(
            u,
            f"<b>✅ Bᴏɴᴜs Aᴍᴏᴜɴᴛ Uᴘᴅᴀᴛᴇᴅ Tᴏ {amount} Rs.</b>",
            parse_mode="html"
        )

    # ===== SAVE ADMIN LOG =====
    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)

    Bot.runCommand("/admin")
    pass


# ===/SetBotPayChann===
def cmd_SetBotPayChann():
    AllBotAdminss = Bot.getData("AllBotAdminss")
    push = AllBotAdminss if AllBotAdminss else []

    is_Admin = str(message.chat.id) in [str(userid) for userid in push]

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bot.editMessageText(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text="<b>🔰 Sᴇɴᴅ Yᴏᴜʀ Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ Usᴇʀɴᴀᴍᴇ\n\nSᴇɴᴅ <code>Clear</code> Tᴏ Rᴇᴍᴏᴠᴇ Cᴜʀʀᴇɴᴛ Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ</b>",
        parse_mode="html"
    )

    Bot.handleNextCommand("/SetBotPayChann1")
    pass


# ===/SetBotPayChann1===
def cmd_SetBotPayChann1():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    CH = msg.strip()

    # --- Clear Logic Start ---
    if CH.lower() == "clear":
        Bot.saveData("Botpaychannel", "not set")
        markup = {"inline_keyboard": [[{"text": "🔙 Bᴀᴄᴋ", "callback_data": "/admin"}]]}
        bot.replyText(u, "<b>🗑 Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ Hᴀs Bᴇᴇɴ Cʟᴇᴀʀᴇᴅ Sᴜᴄᴄᴇssғᴜʟʟʏ !!</b>", reply_markup=markup, parse_mode="HTML")
        raise ReturnCommand()
    # --- Clear Logic End ---

    if "@" not in CH and "-" not in CH:
        bot.sendMessage(f"{CH} Is Nᴏᴛ Vᴀʟɪᴅ Cʜᴀɴɴᴇʟ Usᴇʀɴᴀᴍᴇ Oʀ Cʜᴀɴɴᴇʟ ID")
        raise ReturnCommand()

    # ... (Aapka baki ka purana code niche waisa hi rahega) ...


    Txt = ""
    BIA = True
    CLink = "None"
    TGID = str(CH)
    TITLE = str(CH)
    USRNAM = "None"

    try:
        url = f'https://api.telegram.org/bot{Bot.info().token}/getChat?chat_id={CH}'
        b = HTTP.get(url).json()
        result = bunchify(b)['result']
        CLink = result.invite_link if "invite_link" in result else "None"
        TGID = result.id
        TITLE = result.title
        USRNAM = result.username if "username" in result else "None"
    except:
        BIA = False

    try:
        member = bot.getChatMember(CH, u)
        UC = member.status
    except:
        BIA = False
        UC = "error"

    if USRNAM == "None":
        USRNAM = str(TGID)
    else:
        USRNAM = "@" + USRNAM

    if CLink == "None":
        CLink = f"https://t.me/{str(TGID)}"
        Txt += f"<b>🚨 Bᴏᴛ Is Nᴏᴛ Aᴅᴍɪɴ Iɴ {CH} 🚨</b>\n\n<b>🔗 Cʜᴀɴɴᴇʟ Lɪɴᴋ Nᴏᴛ Fᴏᴜɴᴅ.</b>"
        BIA = False
    else:
        Txt += f"<b>{'🎉 Bᴏᴛ Is Aᴅᴍɪɴ Iɴ' if BIA else '🚨 Bᴏᴛ Nᴏᴛ Aᴅᴍɪɴ Iɴ'} {CH}</b>\n\n<b>🔗 Cʜᴀɴɴᴇʟ Lɪɴᴋ Sᴜᴄᴄᴇssғᴜʟʟʏ Aᴅᴅᴇᴅ Aᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ!</b>"


    if BIA:
        Bot.saveData("Botpaychannel", msg)
        Txt += f"\n\n<b>✅ Nᴇᴡ Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ Aᴅᴅᴇᴅ {CH}</b>"
    else:
        Txt += f"\n\n<b>❌ Cʜᴀɴɴᴇʟ Nᴏᴛ Aᴅᴅᴇᴅ Yᴇᴛ Bᴇᴄᴀᴜsᴇ Bᴏᴛ Is Nᴏᴛ Aᴅᴍɪɴ Iɴ Cʜᴀɴɴᴇʟ</b>"

    markup = {
        "inline_keyboard": [
            [{"text": "🔙 Bᴀᴄᴋ", "callback_data": "/admin"}]
        ]
    }

    bot.replyText(
        u,
        Txt,
        reply_markup=markup,
        disable_web_page_preview=True
    )
    pass


# ===/SetBotPayComm0===
def cmd_SetBotPayComm0():
    AllBotAdminss = Bot.getData("AllBotAdminss")
    push = AllBotAdminss if AllBotAdminss else []

    is_Admin = str(message.chat.id) in [str(userid) for userid in push]

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bot.editMessageText(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text="<b>🔰 Send Your Payment Comment :</b>",
        parse_mode="html"
    )

    Bot.handleNextCommand("/SetBotPayComm01")
    pass


# ===/SetBotPayComm01===
def cmd_SetBotPayComm01():
    AllBotAdminss = Bot.getData("AllBotAdminss")
    push = AllBotAdminss if AllBotAdminss else []
    is_Admin = str(message.chat.id) in [str(userid) for userid in push]

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    Gatewaysett0 = Bot.getData("gatewaynow") or "Not Set"
    Gatewaysett011 = Bot.getData("gatewaytype") or "https://RJWallet.in/"

    gateway_map = {
        "Rjwallet": "RJ Wallet",
        "full2sms": "F2S Wallet",
        "Tgwallet": "TG Wallet",
        "Fxl": "FXL Wallet",
        "lifafawala": "LifafaWala Wallet",
        "vsv": "VSV Wallet"
    }
    Gatewaysett20 = gateway_map.get(Gatewaysett0, "Not Set")

    if Gatewaysett0 == "Not Set":
        bot.replyText(u, "<b>⛔ Gateway Not Set</b>")
        raise ReturnCommand()


    Bot.saveData("BotPayComm", msg)


    markup = {
        "inline_keyboard": [
            [{"text": "🔙 Back", "callback_data": "/addgetwayonbbot"}]
        ]
    }

    bot.replyText(
        u,
        f"<b>✅ Bot Payout Comment Set : {msg}</b>",
        reply_markup=markup,
        disable_web_page_preview=True
    )

    raise ReturnCommand()
    pass


# ===/SetMinMax===
def cmd_SetMinMax():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("withdraw_limits", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You Are Not This Bot Admin")
        except:
            pass
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    User.saveData("lim_wait", "")

    w_min = Bot.getData("MinWith") or 0
    w_max = Bot.getData("MaxWith") or 0
    m_ref = Bot.getData("SetMinRef") or 0

    custom = 0
    for g in (Bot.getData("AddedGateways") or []):
        if (g.get("min", 0) or 0) or (g.get("max", 0) or 0):
            custom += 1

    text = f"""📉 <b>Wɪᴛʜᴅʀᴀᴡᴀʟ Lɪᴍɪᴛs</b>

🤏🏻 <b>Mɪɴɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ:</b> ₹{w_min}
🔝 <b>Mᴀxɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ:</b> ₹{w_max}
👥 <b>Mɪɴɪᴍᴜᴍ Rᴇғᴇʀ Tᴏ Wɪᴛʜᴅʀᴀᴡ:</b> {m_ref}

🏦 <b>Gᴀᴛᴇᴡᴀʏs Wɪᴛʜ Oᴡɴ Lɪᴍɪᴛ:</b> {custom}
<i>(Sᴇᴛ Pᴇʀ Gᴀᴛᴇᴡᴀʏ Fʀᴏᴍ Gᴀᴛᴇᴡᴀʏ Sᴇᴛᴜᴘ → Mᴀɴᴀɢᴇ → Sᴇᴛ Lɪᴍɪᴛ)</i>

<blockquote><b>🎥 Hᴏᴡ Tᴏ Sᴇᴛ Lɪᴍɪᴛs 👉 <a href="https://t.me/MakerUpdateGuide/14">Click Here</a></b></blockquote></b>"""

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(text="💰 Mɪɴ Wɪᴛʜᴅʀᴀᴡ", callback_data="/LimEdit min"),
        InlineKeyboardButton(text="💎 Mᴀx Wɪᴛʜᴅʀᴀᴡ", callback_data="/LimEdit max")
    )
    markup.add(InlineKeyboardButton(text="👥 Mɪɴ Rᴇғᴇʀ", callback_data="/LimEdit ref"))
    markup.add(InlineKeyboardButton(text="📝 Sᴇᴛ Aʟʟ (Mɪɴ-Mᴀx--Rᴇғᴇʀ)", callback_data="/LimEdit all"))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/admin AP"))

    show(message.message_id, text, markup)
    raise ReturnCommand()
    pass


# ===/SetPerReferAmont===
def cmd_SetPerReferAmont():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()




    bot.replyText(
        u,
        "<b>💰 Eɴᴛᴇʀ Yᴏᴜʀ Rᴇғᴇʀʀᴀʟ Aᴍᴏᴜɴᴛ\n\n"
        "✅ Sᴇᴛ Yᴏᴜʀ Rᴇғᴇʀʀᴀʟ Rᴇᴡᴀʀᴅ Aꜱ A Fɪxᴇᴅ Oʀ Rᴀɴᴅᴏᴍ Aᴍᴏᴜɴᴛ.\n\n"
        "🎲 <u>Rᴀɴᴅᴏᴍ Rᴇᴡᴀʀᴅ</u>\n"
        "⚠️ Uꜱᴇ Tʜɪꜱ Fᴏʀᴍᴀᴛ: "
        "<code>1-5</code>\n"
        "👉 <b>Exᴀᴍᴘʟᴇ:</b> "
        "<code>2-3</code>\n"
        "🎁 Uꜱᴇʀ Rᴇᴄᴇɪᴠᴇꜱ A Rᴀɴᴅᴏᴍ Aᴍᴏᴜɴᴛ Bᴇᴛᴡᴇᴇɴ 2 Aɴᴅ 3.\n\n"
        "💵 <u>Fɪxᴇᴅ Rᴇᴡᴀʀᴅ</u>\n"
        "⚠️ Sɪᴍᴘʟʏ Eɴᴛᴇʀ A Sɪɴɢʟᴇ Nᴜᴍʙᴇʀ.\n\n"
        "🚀 <b>Eɴᴛᴇʀ Yᴏᴜʀ Dᴇsɪʀᴇᴅ Aᴍᴏᴜɴᴛ Nᴏᴡ!</b></b>",
        parse_mode="html"
    )


    Bot.handleNextCommand("/SetPerReferAmont1")
    pass


# ===/SetPerReferAmont1===
def cmd_SetPerReferAmont1():
    get_bot = Bot.info().username 
    RL=f"https://t.me/{get_bot}?start={u}"

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    #Time
    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"
    }

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

    #Validation
    text = message.text.strip()

    if text.isdigit():
        # ✅ Case 1: Single number
        Bot.saveData("PerRefer", text)
        act = f"Refer Amount updated to {text}"
        bot.replyText(
            u,
            f"<b><i>Refer Amount Updated To {text} (Fixed) Rs.</i></b>\n\n"
            f"<b><i>🤖 Bot Link ::</i></b>\n"
            f"<b><i>{RL}</i></b>",
            parse_mode="html"
        )

    elif "-" in text:
        parts = text.split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            min_val, max_val = int(parts[0]), int(parts[1])
            if min_val <= max_val:
                Bot.saveData("PerRefer", text)
                act = f"Refer Range updated to {min_val}-{max_val}"
                bot.replyText(
                    u,
                    f"<b><i>Refer Amount Updated To {min_val}-{max_val} (Random) Rs.</i></b>\n\n"
                    f"<b><i>🤖 Bot Link ::</i></b>\n"
                    f"<b><i>{RL}</i></b>",
                    parse_mode="html"
                )
            else:
                bot.replyText(u, "⚠️ Minimum refer must be less than or equal to Maximum refer.")
                raise ReturnCommand()
        else:
            bot.replyText(u, "❌ Please enter in correct format: <b>min-max</b> (e.g., 1-3)", parse_mode="html")
            raise ReturnCommand()
    else:
        bot.replyText(u, "❌ Please use only numbers or format <b>min-max</b>", parse_mode="html")
        raise ReturnCommand()

    #Save Action Log
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> {act}"
    )
    Bot.saveData("AdmAC", AdmAC)

    Bot.runCommand("/admin")
    pass


# ===/SetTaxAmont===
def cmd_SetTaxAmont():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("tax", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    User.saveData("gw_wait", "")

    Gateways = Bot.getData("AddedGateways") or []
    markup = InlineKeyboardMarkup()

    if not Gateways:
        text = "⚖️ <b>Gᴀᴛᴇᴡᴀʏ Tᴀx</b>\n\n⚠️ Nᴏ Gᴀᴛᴇᴡᴀʏ Aᴅᴅᴇᴅ."
        markup.add(InlineKeyboardButton(text="➕ Aᴅᴅ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwAdd"))
    else:
        text = "⚖️ <b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Yᴏᴜʀ Aᴅᴅᴇᴅ Gᴀᴛᴇᴡᴀʏ Tᴀx\n\n"
        for g in Gateways:
            icon = "🟢" if g.get("on", True) else "🔴"
            text += f"{icon} <b>{g.get('name')}</b> → ₹{g.get('tax', 0)}\n"
        text += "\n👇 <b>Cʜᴏᴏsᴇ A Gᴀᴛᴇᴡᴀʏ, Yᴏᴜ Wᴀɴᴛ Tᴏ Cʜᴀɴɢᴇ Tᴀx</b>"

        row = []
        for g in Gateways:
            row.append(InlineKeyboardButton(
                text=str(g.get("name")) + " • ₹" + str(g.get("tax", 0)),
                callback_data="/gwTax " + str(g.get("id")) + " tax"
            ))
            if len(row) == 2:
                markup.row(*row)
                row = []
        if row:
            markup.row(*row)

        markup.add(InlineKeyboardButton(text="🔁 Sᴇᴛ Sᴀᴍᴇ Tᴀx Fᴏʀ Aʟʟ", callback_data="/gwTax all tax"))

    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/admin AP"))

    show(message.message_id, text, markup)
    raise ReturnCommand()
    pass


# ===/TalkUser===
def cmd_TalkUser():
    admins = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, admins):
        raise ReturnCommand()

    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text="""<b>💬 Tᴀʟᴋ Wɪᴛʜ Usᴇʀ</b>
━━━━━━━━━━━━━━━━━━
🆔 <b>Sᴇɴᴅ Usᴇʀ ID & Mᴇssᴀɢᴇ</b>

<i>Exᴀᴍᴘʟᴇ:</i> <code>6925391837 Hello!! How Are You?</code>

❌ Sᴇɴᴅ <code>cancel</code> Tᴏ Cᴀɴᴄᴇʟ.""",
        parse_mode="HTML"
    )

    Bot.handleNextCommand("/TalkUser2")
    pass


# ===/TalkUser2===
def cmd_TalkUser2():
    admins = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, admins):
        raise ReturnCommand()

    data = message.text.split(" ", 1)

    # Cancel
    if message.text.strip().lower() == "cancel":
        Bot.runCommand("/admin")
        raise ReturnCommand()

    if len(data) < 2:
        bot.sendMessage(
            chat_id=u,
            text="""<b>❌ Iɴᴠᴀʟɪᴅ Fᴏʀᴍᴀᴛ</b>
━━━━━━━━━━━━━━━━━━

Usᴇ: <code>UsᴇʀID Mᴇssᴀɢᴇ</code>

Exᴀᴍᴘʟᴇ: <code>123456789 Hello, how can I help you?</code>""",
            parse_mode="HTML"
        )

        raise ReturnCommand()

    user_id = data[0]
    text = data[1]

    if not user_id.lstrip("-").isdigit():
        bot.sendMessage(
            chat_id=u,
            text="<b>❌ Iɴᴠᴀʟɪᴅ Usʀʀ ID!</b>",
            parse_mode="HTML"
        )

        Bot.handleNextCommand("/TalkUser2")
        raise ReturnCommand()

    try:

        bot.sendMessage(
            chat_id=user_id,
            text=f"""<b>💬 Mᴇssᴀɢᴇ Fʀᴏᴍ Aᴅᴍɪᴍ</b>

━━━━━━━━━━━━━━━━━━

{text}

━━━━━━━━━━━━━━━━━━""",
            parse_mode="HTML"
        )

        bot.sendMessage(
            chat_id=u,
            text=f"""<b>✅ Mᴇssᴀɢᴇ Sᴇɴᴛ Sᴜᴄᴄᴇssғᴜʟʟʏ</b>
━━━━━━━━━━━━━━━━━━

🆔 <b>Usᴇʀ:</b> <code>{user_id}</code>

💬 <b>Mᴇssᴀɢᴇ:</b> {text}

━━━━━━━━━━━━━━━━━━""",
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "💬 Sᴇɴᴅ Aɴᴏᴛʜᴇʀ",
                            "callback_data": "/TalkUser"
                        },
                        {
                            "text": "🔙 Aᴅᴍɪɴ Pᴀɴᴇʟ",
                            "callback_data": "/admin"
                        }
                    ]
                ]
            }
        )

    except:

        bot.sendMessage(
            chat_id=u,
            text=f"""<b>❌ Mᴇssᴀɢᴇ Fᴀɪʟᴇᴅ</b>

━━━━━━━━━━━━━━━━━━

🆔 Usᴇʀ: <code>{user_id}</code>

<i>Tʜᴇ Usᴇʀ Mᴀʏ Hᴀᴠᴇ Bʟᴏᴄᴋᴇᴅ Tʜᴇ Bᴏᴛ Oʀ Tʜᴇ Iᴅ Mᴀʏ Bᴇ Iɴᴠᴀʟɪᴅ.</i>""",
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "🔄 Tʀʏ Aɢᴀɪɴ",
                            "callback_data": "/TalkUser"
                        },
                        {
                            "text": "🔙 Bᴀᴄᴋ",
                            "callback_data": "/admin"
                        }
                    ]
                ]
            }
        )
    pass


# ===/Top30Referral===
def cmd_Top30Referral():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss")

    if AllBotAdminss == None:
        push = []
    else:
        push = AllBotAdminss

    is_Admin = False

    for userid in push:
        if str(u) == str(userid):
            is_Admin = True


    Full = Bot.getData("FulBotUsrs")

    m = "<b>😍 Top 30 Users With Most Refers :</b>\n\n"

    if not Full:

        markup = InlineKeyboardMarkup()

        markup.row(
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="/AdminStats"
            )
        )

        _safe_edit(
            chat_id=u,
            message_id=call.message.message_id,
            text="<b>😳 No Top Users Available</b>",
            parse_mode="HTML",
            reply_markup=markup
        )

        raise ReturnCommand()


    FullUsrsRefC = []

    for i in Full:

        c = Bot.getData(str(i) + "RefCount")

        if c is None:
            c = 0

        FullUsrsRefC.append((i, c))


    # Sort highest referrals first
    FullUsrsRefC.sort(
        key=lambda x: x[1],
        reverse=True
    )


    # TOP 30 ONLY
    top_users = FullUsrsRefC[:30]


    # Rank numbers 1-30
    dec = [
        "🥇", "🥈", "🥉",
        "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
        "11", "12", "13", "14", "15",
        "16", "17", "18", "19", "20",
        "21", "22", "23", "24", "25",
        "26", "27", "28", "29", "30"
    ]


    n = 0

    for user_id, ref_count in top_users:

        if ref_count > 0 and Bot.getData("LdrbrdBanUsr") != user_id:

            UID = str(user_id)

            hide_wallet = f"{UID[0:2]}*****{UID[7:10]}"

            usr = "<b>" + UID[:3] + "****" + UID[-2:] + "</b>"

            if is_Admin == True:
                usr = f'<b><a href="tg://user?id={user_id}">{user_id}</a></b>'

            m += (
                f"{dec[n]} <b>Top {n+1}:</b>\n"
                f"   User ID: {hide_wallet}\n"
                f"   Verified Referrals: {ref_count}\n\n"
            )

            n += 1


    markup = InlineKeyboardMarkup()

    markup.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="/AdminStats"
        )
    )


    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=m,
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/TopBalance===
def cmd_TopBalance():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # TOP 30 BALANCE LEADERBOARD
    # 10 USERS PER PAGE - SINGLE COMMAND

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True
            break

    if not is_Admin:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    Full = Bot.getData("FulBotUsrs") or []

    if not Full:

        markup = InlineKeyboardMarkup()

        markup.row(
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="/AdminStats"
            )
        )

        _safe_edit(
            chat_id=u,
            message_id=call.message.message_id,
            text="<b>😳 No Top Users Available</b>",
            parse_mode="HTML",
            reply_markup=markup
        )

        raise ReturnCommand()


    # =========================
    # GET PAGE
    # =========================

    page = 1

    try:
        data = call.data or ""

        if " " in data:
            page = int(data.split(" ")[1])

    except:
        page = 1


    # =========================
    # GET BALANCE DATA
    # =========================

    FullUsrsBalC = []

    for user_id in Full:

        bal = libs.Resources.anotherRes(
            "Balance",
            user=user_id
        ).value()

        if bal is None:
            bal = 0

        try:
            bal = float(bal)
        except:
            bal = 0

        if bal > 0:
            FullUsrsBalC.append(
                (user_id, bal)
            )


    # Highest balance first
    FullUsrsBalC.sort(
        key=lambda x: x[1],
        reverse=True
    )


    # =========================
    # TOP 30 ONLY
    # =========================

    top_users = FullUsrsBalC[:30]


    # =========================
    # PAGE SYSTEM
    # =========================

    per_page = 10

    total_users = len(top_users)

    total_pages = (
        (total_users + per_page - 1)
        // per_page
    )

    if total_pages < 1:
        total_pages = 1

    if page < 1:
        page = 1

    if page > total_pages:
        page = total_pages


    start = (page - 1) * per_page
    end = start + per_page

    page_users = top_users[start:end]


    # =========================
    # MESSAGE
    # =========================

    m = (
        "<b>😍 Top Users With Most Balance :</b>\n"
        f"<i>Page {page}/{total_pages}</i>\n\n"
    )


    medals = [
        "🥇", "🥈", "🥉",
        "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
        "11", "12", "13", "14", "15",
        "16", "17", "18", "19", "20",
        "21", "22", "23", "24", "25",
        "26", "27", "28", "29", "30"
    ]


    for index, (user_id, bal) in enumerate(page_users):

        rank = start + index

        usr = (
            f'<b><a href="tg://user?id={user_id}">'
            f'{user_id}</a></b>'
        )

        m += (
            f"{medals[rank]} {usr} - ₹ {bal:g}\n"
        )


    # =========================
    # BUTTONS
    # =========================

    markup = InlineKeyboardMarkup()


    if page > 1:

        markup.row(
            InlineKeyboardButton(
                text="⬅️ Previous",
                callback_data=f"/TopBalance {page - 1}"
            ),
            InlineKeyboardButton(
                text=f"📄 {page}/{total_pages}",
                callback_data="noop"
            )
        )


    elif page == 1:

        markup.row(
            InlineKeyboardButton(
                text=f"📄 {page}/{total_pages}",
                callback_data="noop"
            )
        )


    if page < total_pages:

        markup.row(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"/TopBalance {page + 1}"
            ))


    markup.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="/AdminStats"
        )
    )


    # =========================
    # EDIT MESSAGE
    # =========================

    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=m,
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/TopReferral===
def cmd_TopReferral():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss")
    if AllBotAdminss==None:
        push = []
    else:
        push = AllBotAdminss

    is_Admin = False
    for userid in push:
        if str(u) == str(userid):
            is_Admin = True




    Full = Bot.getData("FulBotUsrs")

    m = "<b>😍Top Users With Most Refers :\n\n</b>"

    if not Full:  
        bot.sendMessage("<b>😳 No Top Users Available</b>")
        raise ReturnCommand()

    FullUsrsRefC = []

    for i in Full:
        c = Bot.getData(str(i) + "RefCount")
        if c is None:
            c = 0
        FullUsrsRefC.append((i, c))  

    FullUsrsRefC.sort(key=lambda x: x[1], reverse=True)

    s=Bot.getData("LdrbrdSize") or 5

    top_users = FullUsrsRefC[:int(s)]

    dec = ["🥇", "🥈", "🥉", "4⃣", "5⃣","6️⃣","7️⃣","8️⃣️","9️⃣","🔟"]

    n = 0
    for user_id, ref_count in top_users:

        if ref_count > 0 and Bot.getData("LdrbrdBanUsr") != user_id:
            UID = str(user_id)
            hide_wallet = f"{UID[0:2]}*****{UID[7:10]}"
            usr = "<b>"+UID[:3] +"****" +UID[-2:]+"</b>"

            if is_Admin==True:
                usr = f'<b><a href="tg://user?id={user_id}">{user_id}</a></b>'

            m += f"{dec[n]} <b>Top {n+1}:</b>\n   User ID: {hide_wallet}\n   Verified Referrals: {ref_count}\n\n"
            n += 1




    bot.sendMessage(f"<b>{m}</b>")
    ss=Bot.getData("LdrbrdTxt") or "0"
    if ss!="0":
        bot.sendMessage(f"<b>{ss}</b>")
    pass


# ===/TopWithdraw_Wallet===
def cmd_TopWithdraw_Wallet():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # coded by @Jenish_Dobariya1
    # params: page number (1,2,3...)

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            _safe_edit(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" in str(e).lower():
                try:
                    bot.answerCallbackQuery(call.id, "✅ Already up to date")
                except:
                    pass
            else:
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    def fmt(x):
        try:
            s = f"{float(x):.2f}".rstrip("0").rstrip(".")
            return s if s else "0"
        except:
            return str(x)


    PAGE_SIZE = 10

    try:
        page = int(str(params).strip())
    except:
        page = 1
    if page < 1:
        page = 1

    back_row = InlineKeyboardMarkup()
    back_row.add(InlineKeyboardButton(text="⬅️ Back", callback_data="/AdminStats"))

    Full = Bot.getData("FulBotUsrs") or []

    if not Full:
        show(message.message_id, "<b>😳 No Top Wallet Available</b>", back_row)
        raise ReturnCommand()

    # ---- group by wallet (same wallet on many accounts is added together) ----
    wallets = {}
    grand_total = 0.0

    for i in Full:
        try:
            c = libs.Resources.anotherRes("Withdraw", user=i).value()
        except:
            c = 0

        if c is None:
            c = 0
        c = float(c)

        if c <= 0:
            continue

        w = Bot.getData("UserWallet" + str(i))
        if not w:
            w = "~~~ (ID " + str(i) + ")"

        grand_total += c

        if w not in wallets:
            wallets[w] = {"total": 0.0, "users": 0, "top_user": i, "top_amt": 0.0}

        wallets[w]["total"] += c
        wallets[w]["users"] += 1

        if c > wallets[w]["top_amt"]:
            wallets[w]["top_amt"] = c
            wallets[w]["top_user"] = i

    ranked = sorted(wallets.items(), key=lambda x: x[1]["total"], reverse=True)

    if not ranked:
        show(message.message_id, "<b>😳 No Withdrawals Yet</b>", back_row)
        raise ReturnCommand()

    total_wallets = len(ranked)
    pages = (total_wallets + PAGE_SIZE - 1) // PAGE_SIZE

    if page > pages:
        page = pages

    start = (page - 1) * PAGE_SIZE
    chunk = ranked[start:start + PAGE_SIZE]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    m = "<b>😍 Top Wallets By Withdrawals</b>\n\n"
    m += f"💳 <b>Wallets:</b> {total_wallets}   💸 <b>Total Paid:</b> ₹{fmt(grand_total)}\n"
    m += "━━━━━━━━━━━━━━\n\n"

    rank = start
    for w, d in chunk:
        rank += 1
        badge = medals.get(rank, f"#{rank}")
        share = (d["total"] / grand_total * 100) if grand_total > 0 else 0

        m += f"<b>{badge}</b> <b>💳 Wallet Number :: <code>{w}</code></b>\n"
        m += f"💰 <b>Balance ₹{fmt(d['total'])}</b>  •  📊 {share:.1f}%\n"
        m += f"👤 <b>Open 👉🏻 <a href=\"tg://user?id={d['top_user']}\">{d['top_user']}</a></b>"

        if d["users"] > 1:
            m += f"  •  ⚠️ <b>{d['users']} accounts</b> use this wallet"

        m += "\n──────────────\n"

    m += f"\n<i>📄 Page {page}/{pages}</i>"

    markup = InlineKeyboardMarkup()

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data="/TopWithdraw_Wallet " + str(page - 1)))
    nav.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="/TopWithdraw_Wallet " + str(page)))
    if page < pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data="/TopWithdraw_Wallet " + str(page + 1)))
    markup.row(*nav)

    markup.add(InlineKeyboardButton(text="🔄 Refresh", callback_data="/TopWithdraw_Wallet " + str(page)))
    markup.add(InlineKeyboardButton(text="⬅️ Back", callback_data="/AdminStats"))

    show(message.message_id, m, markup)
    raise ReturnCommand()
    pass


# ===/TopWithdraws===
def cmd_TopWithdraws():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False

    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫You Are Not This Bot Admin</i></b>"
        )
        raise ReturnCommand()


    Full = Bot.getData("FulBotUsrs")

    m = "<b>😍 Top Users With Most Withdrawals :</b>\n\n"

    if not Full:

        markup = InlineKeyboardMarkup()

        markup.row(
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="/AdminStats"
            )
        )

        _safe_edit(
            chat_id=u,
            message_id=call.message.message_id,
            text="<b>😳 No Top Wallet Available</b>",
            parse_mode="HTML",
            reply_markup=markup
        )

        raise ReturnCommand()


    FullUsrsWithdrwC = []

    for i in Full:

        c = libs.Resources.anotherRes(
            'Withdraw',
            user=i
        ).value()

        if c is None:
            c = 0

        FullUsrsWithdrwC.append((i, c))


    FullUsrsWithdrwC.sort(
        key=lambda x: x[1],
        reverse=True
    )


    top_users = FullUsrsWithdrwC[:10]


    medals = [
        "🥇", "🥈", "🥉", "4⃣", "5⃣",
        "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"
    ]


    n = 0

    for user_id, Wbal in top_users:

        if Wbal > 0:

            usr = f'<b><a href="tg://user?id={user_id}">{user_id}</a></b>'

            refC = Bot.getData(
                str(user_id) + "RefCount"
            ) or 0

            m += f"""{medals[n]} <b>Top {n+1}:</b>
👤 User ID: {usr}
👥 Verified Refers: {refC}
💰 TOTAL Amount: ₹{Wbal}
──────────────

"""

            n += 1


    markup = InlineKeyboardMarkup()

    markup.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="/AdminStats"
        )
    )


    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=m,
        parse_mode="HTML",
        reply_markup=markup
    )
    pass


# ===/TransferOwnership===
def cmd_TransferOwnership():
    owner = Bot.getData("Owner")

    if str(u) != str(owner):
        bot.replyText(
            u,
            "<b>🚫 Only the bot owner can transfer ownership.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    bot.replyText(
        u,
        "<b>👤 Send the Telegram ID of the new owner.</b>\n\n",
        parse_mode="HTML"
    )

    Bot.handleNextCommand("/TransferOwnership_Save")
    pass


# ===/TransferOwnership_Save===
def cmd_TransferOwnership_Save():
    owner = Bot.getData("Owner")

    if str(u) != str(owner):
        raise ReturnCommand()

    new_owner = message.text.strip()

    if not new_owner.isdigit():
        bot.replyText(
            u,
            "<b>❌ Please send a valid Telegram User ID.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    # Cannot transfer to yourself
    if str(new_owner) == str(owner):
        bot.replyText(
            u,
            "<b>❌ You are already the owner of this bot.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    # User must have started the bot
    users = Bot.getData("FulBotUsrs") or []

    if str(new_owner) not in [str(x) for x in users]:
        bot.replyText(
            u,
            "<b>❌ This user hasn't started the bot yet.\n\nAsk them to start the bot first, then try again.</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    # Add to admin list if missing
    admins = Bot.getData("AllBotAdminss") or []

    if str(new_owner) not in [str(x) for x in admins]:
        admins.append(str(new_owner))
        Bot.saveData("AllBotAdminss", admins)

    old_owner = owner
    Bot.saveData("Owner", str(new_owner))

    bot.replyText(
        u,
        f"""<b>✅ Ownership transferred successfully.

👤 Old Owner:
<code>{old_owner}</code>

👑 New Owner:
<code>{new_owner}</code></b>""",
        parse_mode="HTML"
    )

    # Notify new owner (ignore errors)
    try:
        bot.sendMessage(
            chat_id=new_owner,
            text="<b>👑 Congratulations!\n\nYou are now the owner of this bot.</b>",
            parse_mode="HTML"
        )
    except:
        pass
    pass


# ===/UserKiSabDetails===
def cmd_UserKiSabDetails():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    bot.replyText( chat_id = message.chat.id,
        text = f"""<b>💡 Send User Telegram Id To View Details</b>""",
    parse_mode = "html")

    Bot.handleNextCommand("/UserKiSabDetails1")
    pass


# ===/UserKiSabDetails1===
def cmd_UserKiSabDetails1():
    # ==============================
    # USER DETAILS
    # ==============================

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, AllBotAdminss):
        bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>", parse_mode="HTML")
        raise ReturnCommand()


    # ==============================
    # GET USER ID
    # ==============================

    UID = ""
    if params != None and str(params) not in ["None", ""]:
        UID = str(params).strip()

    if not UID:
        UID = str(message.text).strip()

    if UID.startswith("/UserKiSabDetails1"):
        UID = UID.replace("/UserKiSabDetails1", "", 1).strip()

    if not UID:
        UID = str(Bot.getData("ViewingUser_" + str(u)) or "").strip()

    if not UID.isdigit():
        bot.replyText(
            u,
            "<b>❌ Invalid User ID</b>\n\n"
            "💡 Please send a numeric Telegram User ID.",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    UID = UID.strip()
    uid = UID
    # Save selected user permanently for buttons
    Bot.saveData("ViewingUser_" + str(u), UID)

    ref = Bot.getData(str(uid)+"Referral") or "None"
    sameDev = Bot.getData(str(uid)+"sameDev") or "no"
    is_invited = User.getData("is_invited")

    # ==============================
    # USER DATA
    # ==============================
    # ==============================
    # REFERRED BY
    # ==============================

    ref = Bot.getData(str(UID) + "Referral") or "None"

    if ref == "None":
        invited = "🤖 Auto Started"
    else:
        ref = str(ref)
        invited = f'<a href="tg://user?id={ref}">{ref}</a>'

    balance = libs.Resources.anotherRes(
        "Balance",
        user=UID
    ).value() or 0

    withdraw = libs.Resources.anotherRes(
        "Withdraw",
        user=UID
    ).value() or 0

    RefStrtC = Bot.getData(UID + "RefStrtC") or 0
    refC = Bot.getData(UID + "RefCount") or 0
    RefSameDevC = Bot.getData(UID + "RefSameDevC") or 0
    RefBonClimC = Bot.getData(UID + "RefBonClimC") or 0
    RefWalletLnkC = Bot.getData(UID + "RefWalletLnkC") or 0

    pending = int(RefStrtC) - int(refC)

    if pending < 0:
        pending = 0


    # ==============================
    # WALLET
    # ==============================

    wallet = Bot.getData("UserWallet" + UID) or "Not Linked"


    # ==============================
    # USER DETAILS TEXT
    # ==============================

    TXT = f"""<b>👤 Uѕᴇʀ Dᴇᴛᴀɪʟѕ

Usᴇʀ ID: <a href="tg://user?id={UID}">{UID}</a>
Bᴀʟᴀɴᴄᴇ: ₹{balance}
Tᴏᴛᴀʟ Wɪᴛʜᴅʀᴀᴡɴ: ₹{withdraw}
Tᴏᴛᴀʟ Rᴇғᴇʀs: {RefStrtC}
Vᴇʀɪғɪᴇᴅ Rᴇғᴇʀs: {refC}
Bʟᴏᴄᴋᴇᴅ Rᴇғᴇʀs (Sᴀᴍᴇ Dᴇᴠɪᴄᴇ): {RefSameDevC}
Pᴇɴᴅɪɴɢ Rᴇғᴇʀs: {RefStrtC - refC}
Rᴇғᴇʀʀᴀʟs Wɪᴛʜ Wᴀʟʟᴇᴛ Lɪɴᴋ: {RefWalletLnkC}
Rᴇғᴇʀʀᴀʟs Wɪᴛʜ Cʟᴀɪᴍᴇᴅ Bᴏɴᴜs: {RefBonClimC}
Wᴀʟʟᴇᴛ:<code> {wallet}</code>

Rᴇғᴇʀʀᴇᴅ Bʏ: {invited}"""


    # ==============================
    # BUTTONS
    # ==============================

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            text="👥 Rᴇғᴇʀʀᴀʟs",
            callback_data="/UserReferrals"
        ))

    markup.add(
        InlineKeyboardButton(
            text="🔙 Bᴀᴄᴋ",
            callback_data="/admin"
        )
    )


    # ==============================
    # SEND DETAILS
    # ==============================

    bot.replyText(
        u,
        TXT,
        reply_markup=markup,
        parse_mode="HTML"
    )
    pass


# ===/UserReferrals===
def cmd_UserReferrals():
    # ==============================
    # /UserReferrals
    # ==============================

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    if str(u) not in map(str, AllBotAdminss):
        bot.replyText(
            u,
            "<b>🚫 You Are Not This Bot Admin</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # GET UID + PAGE
    # ==============================

    UID = ""
    page = 1

    if params and str(params) not in ["None", ""]:
        parts = str(params).strip().split()

        if len(parts) >= 1:
            UID = parts[0]

        if len(parts) >= 2:
            try:
                page = int(parts[1])
            except:
                page = 1

    if not UID:
        UID = str(
            Bot.getData("ViewingUser_" + str(u)) or ""
        ).strip()

    if UID.startswith("/UserReferrals"):
        UID = UID.replace("/UserReferrals", "", 1).strip()

        parts = UID.split()

        if len(parts) >= 1:
            UID = parts[0]

        if len(parts) >= 2:
            try:
                page = int(parts[1])
            except:
                page = 1


    # ==============================
    # VALIDATE UID
    # ==============================

    if not UID.isdigit():
        bot.replyText(
            u,
            "<b>❌ Invalid User ID</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()

    Bot.saveData("ViewingUser_" + str(u), UID)

    if page < 1:
        page = 1


    # ==============================
    # FIND ALL REFERRED USERS
    # ==============================

    AllUsers = Bot.getData("FulBotUsrs") or []

    InvitedUsers = []

    for user_id in AllUsers:

        user_id = str(user_id)

        user_ref = Bot.getData(
            user_id + "Referral"
        ) or "None"

        if str(user_ref) == str(UID):

            if user_id not in InvitedUsers:
                InvitedUsers.append(user_id)


    # ==============================
    # PAGE SYSTEM
    # ==============================

    PER_PAGE = 10

    TotalUsers = len(InvitedUsers)

    TotalPages = (TotalUsers + PER_PAGE - 1) // PER_PAGE

    if TotalPages < 1:
        TotalPages = 1

    if page > TotalPages:
        page = TotalPages

    Start = (page - 1) * PER_PAGE
    End = Start + PER_PAGE

    PageUsers = InvitedUsers[Start:End]


    # ==============================
    # DISPLAY
    # ==============================

    TXT = f"""<b>👥 Uѕᴇʀ Rᴇғᴇʀʀᴀʟѕ

🆔 Rᴇғᴇʀʀᴇʀ ID: <code>{UID}</code>
📊 Tᴏᴛᴀʟ Iɴᴠɪᴛᴇᴅ: {TotalUsers}
📄 Pᴀɢᴇ: {page}/{TotalPages}</b>

"""

    if TotalUsers == 0:

        TXT += "<b>🤷‍♂️ Tʜɪs Uѕᴇʀ Hᴀs Nᴏᴛ Iɴᴠɪᴛᴇᴅ Aɴʏᴏɴᴇ.</b>"

    else:

        number = Start + 1

    for invited_id in PageUsers:

        try:
            chat_info = bot.getChat(invited_id)
            first_name = chat_info.first_name or "Unknown"
        except:
            first_name = "Unknown"

        TXT += (
            f'<b>{number}. '
            f'<a href="tg://user?id={invited_id}">{first_name}</a> '
            f'[<code>{invited_id}</code>]</b>\n'
        )

        number += 1

    # ==============================
    # BUTTONS
    # ==============================

    markup = InlineKeyboardMarkup()

    nav = []

    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Pʀᴇᴠ",
                callback_data="/UserReferrals " + UID + " " + str(page - 1)
            )
        )

    if page < TotalPages:
        nav.append(
            InlineKeyboardButton(
                text="Nᴇxᴛ ➡️",
                callback_data="/UserReferrals " + UID + " " + str(page + 1)
            )
        )

    if nav:
        markup.row(*nav)

    markup.add(
        InlineKeyboardButton(
            text="🔙 Bᴀᴄᴋ",
            callback_data="/admin"
        )
    )


    # ==============================
    # DISPLAY
    # ==============================

    try:

        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    except:

        bot.replyText(
            u,
            TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    raise ReturnCommand()
    pass


# ===/WalletOFF_Text===
def cmd_WalletOFF_Text():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    keyboard = ReplyKeyboardMarkup(True)
    keyboard.row("⛔ Cancel")

    bot.replyText(
        u,
        """<b>💡 Please Send Your Wallet Withdraw Mode Off Message

⚠️ Send '<code>Clear</code>' To Remove The Withdraw Mode Off Message</b>""",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    Bot.handleNextCommand("/WalletOFF_Text1")
    pass


# ===/WalletOFF_Text1===
def cmd_WalletOFF_Text1():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if message.text == "⛔ Cancel":
        Bot.runCommand("/PIRO_MainMenu", options="<b>⛔ Cancelled Successfully</b>")
        Bot.runCommand("/admin")
        raise ReturnCommand()

    if message.text.lower() == "clear":
        Bot.saveData("WalletOffText", "")

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>✅ Wallet OFF Text Cleared Successfully</b>"
        )
        raise ReturnCommand()

    Bot.saveData("WalletOffText", message.text)

    Bot.runCommand(
        "/PIRO_MainMenu",
        options="<b>✅ Wallet OFF Text Changed Successfully</b>"
    )
    pass


# ===/WdPostRetry===
def cmd_WdPostRetry():
    # Runs by itself (timer): posts a payout message that could not be sent the first time
    # (the channel allows only ~20 posts per minute). Retries quickly, up to 6 times, so no message is lost.
    # options = "!<attempt>!<text>"  (plain text also works)

    if options is None:
        raise ReturnCommand()

    PaymentCh = Bot.getData("Botpaychannel") or "not set"
    if PaymentCh == "not set":
        raise ReturnCommand()

    _raw = str(options)
    _n = 0
    _text = _raw
    if _raw.startswith("!") and _raw.find("!", 1) > 1:
        _j = _raw.find("!", 1)
        _num = _raw[1:_j]
        if _num.isdigit():
            _n = int(_num)
            _text = _raw[_j + 1:]

    _ok = False
    try:
        bot.replyText(PaymentCh, _text, parse_mode="HTML")
        _ok = True
    except:
        _ok = False

    if (not _ok) and _n < 10:
        try:
            Bot.runCommandAfter(5 + _n * 5, "/WdPostRetry", options="!" + str(_n + 1) + "!" + _text)
        except:
            pass
    pass


# ===/WithdrwOFF_Text===
def cmd_WithdrwOFF_Text():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    keyboard = ReplyKeyboardMarkup(True)
    keyboard.row("⛔ Cancel")

    # Admin ko hint de di ki Clear likhne se default text aa jayega
    bot.replyText(u, "<b>💬 Send The Text To Edit</b>\n\n💡 <i>Tip: Send <code>Clear</code> to reset to default message.</i>", parse_mode="html", reply_markup=keyboard)

    Bot.handleNextCommand("/WithdrwOFF_Text1")
    pass


# ===/WithdrwOFF_Text1===
def cmd_WithdrwOFF_Text1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    if message.text == "⛔ Cancel":
        Bot.runCommand("/PIRO_MainMenu", options="<b>⛔ Cancelled Succesfully</b>") 
        Bot.runCommand("/admin")
        raise ReturnCommand()

    # --- CLEAR / RESET LOGIC ---
    input_text = str(message.text).strip()

    if input_text.lower() == "clear":
        # Agar Admin 'Clear' likhe toh custom data remove kar do (Default vapas aa jayega)
        Bot.saveData("WOT", None)
        Bot.runCommand("/PIRO_MainMenu", options="<b>🔄 Reset to Default Successfully</b>")
    else:
        # Agar naya text likha toh save kar do
        Bot.saveData("WOT", message.text)
        Bot.runCommand("/PIRO_MainMenu", options="<b>Changed Successfully</b>")

    Bot.runCommand("/admin")
    pass


# ===/addgetwayonbbot===
def cmd_addgetwayonbbot():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if not isinstance(Admins, list):
        Admins = [Admins]

    if str(u) not in [str(x) for x in Admins]:
        bot.replyText(u, "<b>🚫 Access Denied\n\n🆔 Your Chat ID : " + str(u) + "</b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    # leaving any input screen -> cancel pending input
    User.saveData("gw_wait", "")

    GW = Bot.getData("AddedGateways") or []
    total = len(GW)
    on_count = 0
    for g in GW:
        if g.get("on", True):
            on_count += 1

    w_min = Bot.getData("MinWith") or 0
    w_max = Bot.getData("MaxWith") or 0
    m_ref = Bot.getData("SetMinRef") or 0
    tax_lines=[]
    for g in GW:
        if g.get("on",True):
            tax_lines.append(str(g.get("name"))+": ₹"+str(g.get("tax",0)))
    tax_text=", ".join(tax_lines) if tax_lines else "No gateway"

    text = f"""📢 <b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Yᴏᴜᴛ Gᴀᴛᴇᴡᴀʏs</b>

📊 <b>Tᴏᴛᴀʟ Gᴀᴛᴇᴡᴀʏs Aᴅᴅᴇᴅ:</b> {total}   
🟢 <b>Gᴀᴛᴇᴡᴀʏs Tᴜʀɴᴇᴅ ON:</b> {on_count} 
🔴 <b>Gᴀᴛᴇᴡᴀʏs Tᴜʀɴᴇᴅ OFF:</b> {total - on_count}
📉 <b>Wɪᴛʜᴅʀᴀᴡᴀʟ Lɪᴍɪᴛs:</b> ₹{w_min} - ₹{w_max}   
👥 <b>Mɪɴ Rᴇғᴇʀ Fᴏʀ Wɪᴛʜᴅʀᴀᴡᴀʟ:</b> {m_ref}
⚖️ <b>Tᴀx:</b> {tax_text}

🎥 <b>Hᴏᴡ Tᴏ Aᴅᴅ Gᴀᴛᴇᴡᴀʏs 👉🏻 <a href="https://t.me/MakerUpdateGuide/13">Click Here</a></b>"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🟢 Mᴀɴᴀɢᴇ Gᴀᴛᴇᴡᴀʏ's", callback_data="/gwManage"))
    markup.row(
        InlineKeyboardButton(text="➕ Aᴅᴅ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwAdd"),
        InlineKeyboardButton(text="⚖️ Tᴀx", callback_data="/SetTaxAmont")
    )
    markup.row(
        InlineKeyboardButton(text="📉 Wɪᴛʜᴅʀᴀᴡᴀʟ Lɪᴍɪᴛs", callback_data="/SetMinMax"),
        InlineKeyboardButton(text="👥 Mɪɴ Rᴇғᴇʀ", callback_data="/LimEdit ref")
    )
    same_w = Bot.getData("AllowSameWallet") or "OFF"
    markup.add(InlineKeyboardButton(text="🔁 Sᴀᴍᴇ Wᴀʟʟᴇᴛ Aʟʟᴏᴡᴇᴅ: " + ("🟢 ON" if same_w == "ON" else "🔴 OFF"), callback_data="/gwSameWallet"))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/admin AP"))

    show(message.message_id, text, markup)
    raise ReturnCommand()
    pass


# ===/addgetwayonbbot1===
def cmd_addgetwayonbbot1():
    global msg
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    # user already pressed Back / left this screen -> ignore
    if str(User.getData("gw_wait")) != "add":
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip()
    panel = User.getData("gw_panel")

    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    Gateways = Bot.getData("AddedGateways") or []
    seq = int(Bot.getData("AddedGatewaysSeq") or 0)

    for g in Gateways:
        if "id" not in g:
            seq += 1
            g["id"] = seq
            g["on"] = True
            g["tax"] = 0

    added = []
    bad = []

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue

        # support both "Name | URL" and plain URL (auto name from domain)
        if "|" in line:
            parts = line.split("|", 1)
            gname = parts[0].strip().replace("<", "").replace(">", "").replace("&", "and")
            gurl = parts[1].strip()
        else:
            gurl = line.strip()
            # auto name from domain
            try:
                dom = gurl.split("://",1)[1].split("/",1)[0].split("?",1)[0]
                gname = dom.replace("www.","").split(".")[0].capitalize() or "Gateway"
            except:
                gname = "Gateway"

        if not gname or not (gurl.startswith("http://") or gurl.startswith("https://")):
            bad.append(line)
            continue

        # accept {amount} case-insensitive and wallet placeholders: {number} {wallet} {upi} {account} {paytm}
        low = gurl.lower()
        has_amount = "{amount}" in low
        has_wallet = any(p in low for p in ["{number}", "{wallet}", "{upi}", "{vpa}", "{account}", "{paytm}"])
        if not has_amount or not has_wallet:
            bad.append(line)
            continue

        domain = gurl.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]

        # security: no spaces, sane length, and never an internal / private address
        _dl = domain.lower().split("@")[-1].split(":")[0]
        _blocked = (" " in gurl) or len(gurl) > 1500 or ("." not in _dl) or (_dl == "localhost")
        for _p in ["127.", "10.", "192.168.", "169.254.", "0.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31."]:
            if _dl.startswith(_p):
                _blocked = True
        if _blocked:
            bad.append(line)
            continue

        found = False
        for g in Gateways:
            if str(g.get("name")).lower() == gname.lower():
                g["url"] = gurl
                g["domain"] = domain
                found = True
                break

        if not found:
            seq += 1
            Gateways.append({"id": seq, "name": gname, "domain": domain, "url": gurl, "on": True, "tax": 0, "min": 0, "max": 0})

        added.append({"name": gname, "domain": domain})

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/addgetwayonbbot"))

    if not added:
        show(
            panel,
            "<b>⚠️ Iɴᴠᴀʟɪᴅ Fᴏʀᴍᴀᴛ!</b>\n\nSᴇɴᴅ Lɪᴋᴇ Tʜɪs:\n<code>Name | API_URL</code>\n\n<i>URL Mᴜsᴛ Sᴛᴀʀᴛ Wɪᴛʜ http:// or https:// Aɴᴅ Cᴏɴᴛᴀɪɴ {amount} and {number}</i>",
            back
        )
        Bot.handleNextCommand("/addgetwayonbbot1", cancel_at_command=True)
        raise ReturnCommand()

    Bot.saveData("AddedGateways", Gateways)
    Bot.saveData("AddedGatewaysSeq", seq)
    User.saveData("gw_wait", "")

    # admin log
    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> Gateway Added: " + ", ".join([a['name'] for a in added])
    )
    Bot.saveData("AdmAC", AdmAC)

    msg = "<b>✅ Gᴀᴛᴇᴡᴀʏ Aᴅᴅᴇᴅ Sᴜᴄᴄᴇsғᴜʟʟʏ</b>\n"
    for g in added:
        msg += f"\n🏦 <b>{g['name']}</b> — <code>{g['domain']}</code>"

    msg += "\n\n💡 Oᴘᴇɴ Mᴀɴᴀɢᴇ Tᴏ Sᴇᴛ Tᴀx / Lɪᴍɪᴛs / ON-OFF."

    if bad:
        msg += f"\n⚠️ <b>Sᴋɪᴘᴘᴇᴅ {len(bad)} Iɴᴠᴀʟɪᴅ Lɪɴᴇ(s)</b>"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🟢 Mᴀɴᴀɢᴇ Gᴀᴛᴇᴡᴀʏ's", callback_data="/gwManage"))
    markup.add(InlineKeyboardButton(text="➕ Aᴅᴅ Mᴏʀᴇ", callback_data="/gwAdd"))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/addgetwayonbbot"))

    show(panel, msg, markup)
    raise ReturnCommand()
    pass


# ===/admin===
def cmd_admin():
    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if AllBotAdminss == []:
        MAIN_ADMIN = u
        AllBotAdminss.append(MAIN_ADMIN)
        Bot.saveData("AllBotAdminss", AllBotAdminss)

        AllBotAdminss = Bot.getData("AllBotAdminss") or []
        Bot.saveData("Owner", u)

    if is_Admin != True:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>"
        )
        raise ReturnCommand()



    Owner = Bot.getData("Owner") or None

    # Owner always has all permissions
    is_Owner = str(u) == str(Owner)

    Permissions = Bot.getData("AdminPermissions") or {}


    DEFAULT_OFF_PERMISSIONS = [
        "permission",
        "reset_balance",
    ]


    def HasPermission(key):

        # Owner always has everything
        if is_Owner:
            return True

        # If permission was manually saved, use saved value
        if key in Permissions:
            return Permissions[key]

        # New permissions listed here are OFF by default
        if key in DEFAULT_OFF_PERMISSIONS:
            return False

        # All other permissions are ON by default
        return True

    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]
    time = now["time"][:5]

    year, month, day = date.split("-")
    hour, minute = time.split(":")

    hour = int(hour)

    ampm = "am"

    if hour >= 12:
        ampm = "pm"

    if hour > 12:
        hour -= 12

    if hour == 0:
        hour = 12

    MONTHS = {
        "01": "Jan",
        "02": "Feb",
        "03": "Mar",
        "04": "Apr",
        "05": "May",
        "06": "Jun",
        "07": "Jul",
        "08": "Aug",
        "09": "Sep",
        "10": "Oct",
        "11": "Nov",
        "12": "Dec"
    }

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


    # ==============================
    # DATA
    # ==============================

    AdmAC = Bot.getData("AdmAC") or []

    perRef = Bot.getData("PerRefer") or 0
    daiBon = Bot.getData("DailyBonus") or 0

    minWit = Bot.getData("MinWith") or 0
    maxWit = Bot.getData("MaxWith") or 0

    upi_minWit = Bot.getData("MinWith1") or 0
    upi_maxWit = Bot.getData("MaxWith1") or 0

    tax = Bot.getData("Tax") or 0

    PayOutChannel = Bot.getData("Botpaychannel") or "🚫 Not Set"

    MinRef = Bot.getData("MinRefToWithdrw") or 0
    SetMinRef = Bot.getData("SetMinRef") or 0

    Owner = Bot.getData("Owner") or None

    ownerid = f"tg://openmessage?user_id={Owner}"
    owner = f"<a href='{ownerid}'>{Owner}</a>"

    AddedGatewaysList = Bot.getData("AddedGateways") or []
    gw_total = len(AddedGatewaysList)
    gw_on = 0
    tax_lines = []

    for _g in AddedGatewaysList:
        if _g.get("on", True):
            gw_on += 1
            tax_lines.append(f"🧾 {_g.get('name')} Tax: ₹{_g.get('tax', 0)}")

    gateway_tax_text = "\n   ".join(tax_lines) if tax_lines else "🧾 Gateway Tax: No gateway ON"
    # ==============================
    # BOT MODE
    # ==============================

    if "BotMode" in str(params):

        P = params.split(" ")

        mode = "ON" if P[1].upper() == "ON" else "OFF"

        Bot.saveData("BotMode", mode)

        act = f"🤖 Bot Mode Turned {mode}"

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍<b> Action: </b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)


    BOT_MODE = Bot.getData("BotMode") or "ON"

    botSta = "🟢 On" if BOT_MODE == "ON" else "🔴 Off"

    botStatChngeBut = (
        "BotMode OFF"
        if BOT_MODE == "ON"
        else "BotMode ON"
    )


    # ==============================
    # NEW USER NOTIFICATION
    # ==============================

    NewUserNotification = Bot.getData(
        "NewUserNotification"
    ) or "ON"


    if "NewUserNotification" in str(params):

        P = params.split(" ")

        mode = "ON" if P[1].upper() == "ON" else "OFF"

        Bot.saveData(
            "NewUserNotification",
            mode
        )

        NewUserNotification = mode

        act = f"🔔 New User Notification Turned {mode}"

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍 <b>Action:</b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)


    NewUserNotification = Bot.getData(
        "NewUserNotification"
    ) or "ON"


    NewUserSta = (
        "🟢 On"
        if NewUserNotification == "ON"
        else "🔴 Off"
    )


    NewUserStatChngeBut = (
        "NewUserNotification OFF"
        if NewUserNotification == "ON"
        else "NewUserNotification ON"
    )


    # ==============================
    # WITHDRAW MODE
    # ==============================

    if "WithdrawMode" in str(params):

        P = params.split(" ")

        mode = "ON" if P[1].upper() == "ON" else "OFF"

        Bot.saveData("WithdrawMode", mode)

        act = f"💸 Withdraws Turned {mode}"

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍<b> Action: </b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)


    WITHD_MODE = Bot.getData("WithdrawMode") or "ON"

    witSta = (
        "🟢 On"
        if WITHD_MODE == "ON"
        else "🔴 Off"
    )

    witStatChngeBut = (
        "WithdrawMode OFF"
        if WITHD_MODE == "ON"
        else "WithdrawMode ON"
    )


    # ==============================
    # VERIFICATION SYSTEM
    # ==============================

    current_verif_mode = int(
        Bot.getData("VerifModeSystem") or 0
    )


    if "ToggleVerifMode" in str(params):

        current_verif_mode = (
            current_verif_mode + 1
        ) % 4

        Bot.saveData(
            "VerifModeSystem",
            current_verif_mode
        )

        contact_status = (
            "ON"
            if current_verif_mode in [1, 3]
            else "OFF"
        )

        captcha_status = (
            "ON"
            if current_verif_mode in [2, 3]
            else "OFF"
        )

        Bot.saveData(
            "VerifySystemStatus",
            contact_status
        )

        Bot.saveData(
            "CaptchaMode",
            captcha_status
        )

        mode_labels = {
            0: "ALL OFF ❌",
            1: "CONTACT ONLY 📱",
            2: "CAPTCHA ONLY 🔐",
            3: "BOTH ON 🛡"
        }

        act = (
            f"⚙ Verification Mode Changed To: "
            f"{mode_labels[current_verif_mode]}"
        )

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍<b> Action: </b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)


    verif_display_text = {
        0: "🔴 All Off",
        1: "📱 Contact On",
        2: "🔐 Captcha On",
        3: "🛡 Both On"
    }

    verif_status_label = verif_display_text.get(
        current_verif_mode,
        "🔴 All Off"
    )


    # ==============================
    # NEW USER MODE
    # ==============================

    NewU = Bot.getData("NewU") or "ON"

    NewSta = (
        "🟢 On"
        if NewU == "ON"
        else "🔴 Off"
    )

    NewStatChngeBut = (
        "NewU OFF"
        if NewU == "ON"
        else "NewU ON"
    )


    # ==============================
    # MENU
    # ==============================

    markup = InlineKeyboardMarkup()


    # MANAGE ADMIN PERMISSION
    if HasPermission("permission"):
        markup.add(
            InlineKeyboardButton(
                text="🧑‍💼 Mᴀɴᴀɢᴇ Aᴅᴍɪɴ Pᴇʀᴍɪssɪᴏɴ",
                callback_data="/permission"
            )
        )


    # TRANSFER + TAX
    buttons = []

    if HasPermission("transfer_owner"):
        buttons.append(
            InlineKeyboardButton(
                text="👑 Tʀᴀɴsғᴇʀ Oᴡɴᴇʀsʜɪᴘ",
                callback_data="/TransferOwnership"
            )
        )

    if HasPermission("tax"):
        buttons.append(
            InlineKeyboardButton(
                text="⚖️ Gᴀᴛᴇᴡᴀʏ Tᴀx",
                callback_data="/SetTaxAmont"
            )
        )

    if buttons:
        markup.add(*buttons)


    # VERIFICATION
    if HasPermission("verification"):
        markup.add(
            InlineKeyboardButton(
                text="🛡 Vᴇʀɪғɪᴄᴀᴛɪᴏɴ: " + verif_status_label,
                callback_data="/admin ToggleVerifMode"
            )
        )


    # ADMINS + BAN
    buttons = []

    if HasPermission("admins"):
        buttons.append(
            InlineKeyboardButton(
                text="👮 Mᴀɴᴀɢᴇ Aᴅᴍɪɴs",
                callback_data="/PIRO_Admins"
            )
        )

    if HasPermission("ban_unban"):
        buttons.append(
            InlineKeyboardButton(
                text="⛔ Mᴀɴᴀɢᴇ Bᴀɴ Usᴇʀs",
                callback_data="/BanUnban"
            )
        )

    if buttons:
        markup.add(*buttons)


    # PAYOUT CHANNEL
    if HasPermission("payout_channel"):
        markup.add(
            InlineKeyboardButton(
                text="💳 Sᴇᴛ Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ",
                callback_data="/SetBotPayChann"
            )
        )


    # WITHDRAW + BOT STATUS
    buttons = []

    if HasPermission("withdraw"):
        buttons.append(
            InlineKeyboardButton(
                text="💸 Wɪᴛʜᴅʀᴀᴡ: " + witSta,
                callback_data="/admin " + witStatChngeBut
            )
        )

    if HasPermission("bot_status"):
        buttons.append(
            InlineKeyboardButton(
                text="🤖 Bᴏᴛ Sᴛᴀᴛᴜs: " + botSta,
                callback_data="/admin " + botStatChngeBut
            )
        )

    if buttons:
        markup.add(*buttons)


    # ADD BALANCE
    if HasPermission("balance"):
        markup.add(
            InlineKeyboardButton(
                text="➕ Aᴅᴅ Bᴀʟᴀɴᴄᴇ",
                callback_data="/ChangeAnyUserBal"
            )
        )


    # VERIFY USER + MANAGE TEXT
    buttons = []

    if HasPermission("verify_user"):
        buttons.append(
            InlineKeyboardButton(
                text="🙇🏻‍♂️ Vᴇʀɪғʏ Usᴇʀ",
                callback_data="/manualVerifKro"
            )
        )

    if HasPermission("manage_text"):
        buttons.append(
            InlineKeyboardButton(
                text="🚀 Mᴀɴᴀɢᴇ Tᴇxᴛ",
                callback_data="/ManageText"
            )
        )

    if buttons:
        markup.add(*buttons)


    # CHANNELS
    if HasPermission("channels"):
        markup.add(
            InlineKeyboardButton(
                text="⚡ Mᴀɴᴀɢᴇ Cʜᴀɴɴᴇʟs",
                callback_data="/PIRO_Chanel_Pannel"
            )
        )


    # RESET + BROADCAST
    buttons = []

    if HasPermission("reset_balance"):
        buttons.append(
            InlineKeyboardButton(
                text="⚠️ Rᴇsᴇᴛ Bᴀʟᴀɴᴄᴇ",
                callback_data="/ResetAllBalance"
            )
        )

    if HasPermission("broadcast"):
        buttons.append(
            InlineKeyboardButton(
                text="🎙️ Bʀᴏᴀᴅᴄᴀsᴛ",
                callback_data="/BROADCAST"
            )
        )

    if buttons:
        markup.add(*buttons)


    # WITHDRAWAL LIMITS
    if HasPermission("withdraw_limits"):
        markup.add(
            InlineKeyboardButton(
                text=f"📉 Wɪᴛʜᴅʀᴀᴡᴀʟ Lɪᴍɪᴛs: ₹{minWit}-₹{maxWit} • 👥{SetMinRef}",
                callback_data="/SetMinMax"
            )
        )


    # REFER + BONUS
    buttons = []

    if HasPermission("refer"):
        buttons.append(
            InlineKeyboardButton(
                text=f"👥 Pᴇʀ Rᴇғᴇʀ ~ ₹{perRef}",
                callback_data="/SetPerReferAmont"
            )
        )

    if HasPermission("bonus"):
        buttons.append(
            InlineKeyboardButton(
                text=f"🎁 Bᴏɴᴜs ~ ₹{daiBon}",
                callback_data="/SetBonusAmont"
            )
        )

    if buttons:
        markup.add(*buttons)


    # STATISTICS
    if HasPermission("statistics"):
        markup.add(
            InlineKeyboardButton(
                text="📊 Sᴛᴀᴛɪsᴛɪᴄs",
                callback_data="/AdminStats"
            )
        )


    # TALK + USER DETAILS
    buttons = []

    if HasPermission("talk_user"):
        buttons.append(
            InlineKeyboardButton(
                text="💬 Tᴀʟᴋ Wɪᴛʜ Usᴇʀ",
                callback_data="/TalkUser"
            )
        )

    if HasPermission("user_details"):
        buttons.append(
            InlineKeyboardButton(
                text="ℹ️ Usᴇʀ Dᴇᴛᴀɪʟs",
                callback_data="/UserKiSabDetails"
            )
        )

    if buttons:
        markup.add(*buttons)


    # ADMIN ACTIONS
    if HasPermission("admin_actions"):
        markup.add(
            InlineKeyboardButton(
                text="📝 Rᴇᴄᴇɴᴛ Aᴅᴍɪɴ Aᴄᴛɪᴏɴs",
                callback_data="/PIRO_AdminAction"
            )
        )


    # LEADERBOARD + CLICK HERE
    buttons = []

    if HasPermission("leaderboard"):
        buttons.append(
            InlineKeyboardButton(
                text="🥇 Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ",
                callback_data="/PIRO_LeadSet"
            )
        )

    if HasPermission("click_here"):
        buttons.append(
            InlineKeyboardButton(
                text="🤖 Bᴏᴛ Oғғ Tᴇxᴛ",
                callback_data="/BotOffText"
            )
        )

    if buttons:
        markup.add(*buttons)


    # NEW USER NOTIFICATION
    if HasPermission("new_user_notification"):
        markup.add(
            InlineKeyboardButton(
                text="🔔 Nᴇᴡ Usᴇʀ Nᴏᴛɪғɪᴄᴀᴛɪᴏɴ: " + NewUserSta,
                callback_data="/admin " + NewUserStatChngeBut
            )
        )


    # GIFT + WITHDRAW OFF TEXT
    buttons = []

    if HasPermission("gift_codes"):
        buttons.append(
            InlineKeyboardButton(
                text="🎟 Gɪғᴛ Cᴏᴅᴇs",
                callback_data="/PIRO_RC_Pannel"
            )
        )

    if HasPermission("withdraw_off_text"):
        buttons.append(
            InlineKeyboardButton(
                text="📴 Wɪᴛʜᴅʀᴀᴡ Oғғ Tᴇxᴛ",
                callback_data="/WithdrwOFF_Text"
            )
        )

    if buttons:
        markup.add(*buttons)


    # GATEWAY
    if HasPermission("gateway"):
        markup.add(
            InlineKeyboardButton(
                text=f"🌐 Gᴀᴛᴇᴡᴀʏ Sᴇᴛᴜᴘ ({gw_on}/{gw_total} ON)",
                callback_data="/addgetwayonbbot"
            )
        )


    # ==============================
    # ADMIN TEXT
    # ==============================

    TXT = f"""<b>👋🏻 Hᴇʟʟᴏ </b> <code>{message.from_user.first_name}</code>

<b>👑 Mᴀɪɴ Oᴡɴᴇʀ: {owner}

   🤖 Bᴏᴛ Sᴛᴀᴛᴜs: {botSta}
   💸 Wɪᴛʜᴅʀᴀᴡᴀʟ Sᴛᴀᴛᴜs: {witSta}
   🛡️ Vᴇʀɪғɪᴄᴀᴛɪᴏɴ Mᴏᴅᴇ: {verif_status_label}
   📢 Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ: {PayOutChannel}
   🎫 Mɪɴɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ: ₹{minWit}
   🎟️ Mᴀxɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ: ₹{maxWit}
   👥 Pᴇʀ Rᴇғᴇʀ: ₹{perRef}
   🎯 Mɪɴ. Rᴇғᴇʀ Fᴏʀ Wɪᴛʜᴅʀᴀᴡ: {SetMinRef}
   🎁 Dᴀɪʟʏ Bᴏɴᴜs: ₹{daiBon}
   {gateway_tax_text}

<i>🧭 Sᴇʟᴇᴄᴛ A Sᴇᴛᴛɪɴɢ Bᴇʟᴏᴡ Tᴏ Mᴀɴᴀɢᴇ Yᴏᴜʀ Bᴏᴛ.</i>"""


    # ==============================
    # COMMAND HANDLING
    # ==============================

    if str(message.text) == "/admin":

        bot.replyText(
            u,
            TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

        raise ReturnCommand()


    if (
        params
        or params != None
        or options == "AP"
        or params == "AP"
    ):

        try:

            bot.editMessageText(
                chat_id=u,
                message_id=message.message_id,
                text=TXT,
                reply_markup=markup,
                parse_mode="HTML"
            )

        except:
            pass

        raise ReturnCommand()


    bot.replyText(
        u,
        TXT,
        reply_markup=markup,
        parse_mode="HTML"
    )
    pass


# ===/broadcast===
def cmd_broadcast():
    AllBotAdminss=Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.sendMessage("<b>🚫 You are not Admin</b>", parse_mode="HTML")
        raise ReturnCommand()
    for k in ["broadcast_data","broadcast_buttons","broadcast_button_rows","broadcast_await_buttons","broadcast_preview_mid","broadcast_confirming","active_broadcast_id","broadcast_status_mid","bc2_card_mid","bc2_rows","bc2_await_btn"]:
        User.deleteData(k)
    User.saveData("bc2_wait", True)
    TXT=(
        "<b>✍️ Write your message just like a normal Telegram chat\n\n"
        "You can use Telegram's built-in formatting:\n"
        "• <b>Bold</b> • <i>Italic</i> • <u>Underline</u> • <s>Strike</s> • <code>Mono</code>\n"
        "• <a href='https://google.com'>Clickable Links</a> • <tg-spoiler>Spoiler</tg-spoiler> •\n"
        "<blockquote>Quote</blockquote> • Plain Text\n\n"
        "No HTML or coding is required.\n"
        "The bot will send the message exactly as you format it. ✅</b>"
    )
    kb={"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "/BROADCAST"}]]}
    bot.editMessageText(TXT, message_id=message.message_id, reply_markup=kb)
    # normal wait: everything you send next (even /start or a keyboard button) is taken as the broadcast message; /cancel exits
    Bot.handleNextCommand("/BC2_msg")
    pass


# ===/broadcastResult===
def cmd_broadcastResult():
    # Coded by: @Owner_Here
    # Isko child bot ke broadcast result command me daalna hai

    try:
        # TBC engine se aaya hua raw background data parse karein
        data = bunchify(options.json)
        total = data["total"]
        success = data["total_success"]
        fail = data["total_errors"]

        # Is specific child bot ka username automatic nikalega
        bot_username = bot.getMe().username

        txt = f"""<b>📊 Broadcast Stats (@{bot_username}):

━━━━━━━━━━━━━━━━━━━━━━
👥 Total Attempt: <code>{total}</code>
✅ Total Success: <code>{success}</code>
❌ Total Failed: <code>{fail}</code></b>"""

        # System ke main admins ko dhoondh kar unhe automatic DM bhejega
        AllBotAdminss = Bot.getData("AllBotAdminss") or []
        if AllBotAdminss:
            for admin_id in AllBotAdminss:
                try:
                    bot.sendMessage(chat_id=int(admin_id), text=txt, parse_mode="html")
                except:
                    pass
        else:
            # Fallback: Agar admin list load na ho toh current chat me bhej dega
            bot.sendMessage(text=txt, parse_mode="html")

    except Exception as e:
        # Agar data sync me choti-moti delay aaye toh completion text bhejega
        bot_username = bot.getMe().username
        bot.sendMessage(f"<b>📊 Broadcast Completed in @{bot_username}</b>")
    pass


# ===/cleardb===
def cmd_cleardb():
    bot.sendMessage(
        str({
            "owner": Bot.getData("Owner"),
            "botid": Bot.getData("botid"),
            "token": Bot.getData("bot_token")
        })
    )
    pass


# ===/cooltimw===
def cmd_cooltimw():
    Bot.sendMessage("⏳ <b>Please enter the cooldown time (in minutes)</b>\n\n🕐 Example: <code>30</code>", "html")
    Bot.handleNextCommand("/setcool")
    pass


# ===/creating_NwRC===
def cmd_creating_NwRC():
    # 19'4'25   00'11'10  v:2'0'0 [√]
    # CODED BY: @Jenish_Dobariya1

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

    if not is_Admin:
        bot.replyText(
            u,
            "<b><i>🚫 You Are Not This Bot Admin</i></b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    bot.replyText(
        u,
        """<b>💸 Sᴇɴᴅ Dᴇᴛᴀɪʟs Iɴ Tʜɪs Fᴏʀᴍᴀᴛ:
    
<code>Pᴇʀ Uѕᴇʀ Aᴍᴏᴜɴᴛ-Mᴀx Uѕᴇʀѕ--Mɪɴ Rᴇғᴇʀ</code>

💡 E.xᴀᴍᴘʟᴇ:
<code>1-30--5</code></b>""",
        parse_mode="HTML"
    )

    Bot.handleNextCommand("/CreateBotRC")
    pass


# ===/creating_NwRC1===
def cmd_creating_NwRC1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Jenish_Dobariya1 

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    gift_name = options # पिछली स्टेप से कोड का नाम यहाँ आया
    max_users = message.text.strip()

    bot.replyText(u, f"<b>Okay! {max_users} Users Can Claim The Code: <code>{gift_name}</code></b>\n\n💸 Send the amount each user will receive upon redeeming.", parse_mode="html")

    # ये अगली कमांड (/CreateBotRC) को दोनों डेटा ट्रांसफर करेगा
    Bot.handleNextCommand("/CreateBotRC", options=f"{gift_name}_{max_users}")
    pass


# ===/creating_NwRC_Step2===
def cmd_creating_NwRC_Step2():
    # CODED BY: @Jenish_Dobariya1 

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

    if not is_Admin:
        bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()

    gift_name = message.text.strip().upper() # नाम को UPPERCASE में सेव करेगा ताकि यूज़र आसानी से टाइप कर सकें

    bot.replyText(u, f"<b>Code Name Set To: <code>{gift_name}</code></b>\n\n🔢 Enter the Maximum Number Of Users That Can Redeem This Code.", parse_mode="html")
    Bot.handleNextCommand("/creating_NwRC1", options=gift_name)
    pass


# ===/delete===
def cmd_delete():
    try:
        bot.deleteMessage(u,message.message_id)
    except:
        pass
    bot.sendMessage('<b>Payout Request Canceled ❌ </b>')
    pass


# ===/gwAdd===
def cmd_gwAdd():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    text = """➕ <b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Aᴅᴅ Nᴇᴡ Gᴀᴛᴇᴡᴀʏ</b>

Sᴇɴᴅ Iɴ Tʜɪs Fᴏʀᴍᴀᴛ:
<code>Name | API_URL</code>

<b>Exᴀᴍᴘʟᴇ:</b>
<code>VSV | https://vsv-gateway-solutions.co.in/Api/api.php?token=LLCABXWA&amp;paytm={number}&amp;amount={amount}&amp;comment={comment}</code>

<b>Aɴᴅ Mᴜʟᴛɪᴘʟᴇ Fᴏʀᴍᴀᴛ:</b>
<code>VSV | https://vsv-gateway-solutions.co.in/Api/api.php?token=LLCABXWA&amp;paytm={number}&amp;amount={amount}&amp;comment={comment}
Other Wallet | https://site.com/api.php?paytm={number}&amp;amount={amount}&amp;comment=Payout</code>

<b>Aᴜᴛᴏ Pʟᴀᴄᴇʜᴏʟᴅᴇʀs:
<code>{number}</code> / <code>{wallet}</code> → Usᴇʀ Wᴀʟʟᴇᴛ
<code>{amount}</code> → Aᴍᴏᴜɴᴛ Aғᴛᴇʀ Tᴀx
<code>{comment}</code> / <code>{username}</code> → Bᴏᴛ Usᴇʀɴᴀᴍᴇ

🎥 Hᴏᴡ Tᴏ Aᴅᴅ Gᴀᴛᴇᴡᴀʏs 👉🏻 <a href="https://t.me/MakerUpdateGuide/13">Click Here</a></b>"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/addgetwayonbbot"))

    User.saveData("gw_wait", "add")
    User.saveData("gw_panel", message.message_id)

    show(message.message_id, text, markup)

    Bot.handleNextCommand("/addgetwayonbbot1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/gwEdit===
def cmd_gwEdit():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()


    def mask_url(url):
        try:
            url = str(url)
            if "?" not in url:
                return url
            base, q = url.split("?", 1)
            out = []
            for pair in q.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    kl = k.lower()
                    hide = False
                    for w in ["token", "key", "secret", "pass", "auth", "sign", "guid", "mid"]:
                        if w in kl:
                            hide = True
                    if hide and "{" not in v:
                        v = (v[:2] + "****") if len(v) > 4 else "****"
                    out.append(k + "=" + v)
                else:
                    out.append(pair)
            return base + "?" + "&".join(out)
        except:
            return "hidden"
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    gid = str(params).strip()
    Gateways = Bot.getData("AddedGateways") or []
    gw = None
    for g in Gateways:
        if str(g.get("id")) == gid:
            gw = g
            break

    if gw is None:
        try:
            bot.answerCallbackQuery(call.id, "⚠️ Gᴀᴛᴇᴡᴀʏ Nᴏᴛ Fᴏᴜɴᴅ", show_alert=True)
        except:
            pass
        raise ReturnCommand()

    User.saveData("gw_wait", "edit")
    User.saveData("gw_id", gid)
    User.saveData("gw_panel", message.message_id)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwView " + gid))

    text = (
        "✏️ <b>Eᴅɪғ API Fᴏʀ " + str(gw.get("name")) + "</b>\n\nCᴜʀʀᴇɴᴛ:\n<code>"
        + mask_url(gw.get("url")).replace("&", "&amp;") + "</code>\n\nSᴇɴᴅ Nᴇᴡ API URL:\n"
        "<code>https://site.com/api.php?paytm={number}&amp;amount={amount}&amp;comment={comment}</code>"
    )

    show(message.message_id, text, markup)

    Bot.handleNextCommand("/gwEdit1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/gwEdit1===
def cmd_gwEdit1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    if str(User.getData("gw_wait")) != "edit":
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip()
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    gid = str(User.getData("gw_id") or "")
    panel = User.getData("gw_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwView " + gid))

    low = raw.lower()
    has_amount = "{amount}" in low
    has_wallet = any(p in low for p in ["{number}", "{wallet}", "{upi}", "{account}", "{paytm}"])
    if not (raw.startswith("http://") or raw.startswith("https://")) or not has_amount or not has_wallet:
        show(panel, "<b>❌ Sᴇɴᴅ A Vᴀʟɪᴅ URL</b> Sᴛᴀʀᴛɪɴɢ Wɪᴛʜ http:// or https:// & Cᴏɴᴛᴀɪɴɪɴɢ {amount} & {number}/{wallet}/{upi}", back)
        Bot.handleNextCommand("/gwEdit1", cancel_at_command=True)
        raise ReturnCommand()
    # decode html entity if pasted from panel
    raw = raw.replace("&amp;", "&")

    Gateways = Bot.getData("AddedGateways") or []
    name = ""
    for g in Gateways:
        if str(g.get("id")) == gid:
            g["url"] = raw
            g["domain"] = raw.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0]
            name = str(g.get("name"))
            break

    Bot.saveData("AddedGateways", Gateways)
    User.saveData("gw_wait", "")

    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> API of {name} updated"
    )
    Bot.saveData("AdmAC", AdmAC)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🏦 Oᴘᴇɴ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwView " + gid))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwManage"))

    show(panel, f"<b>✅ API Uᴘᴅᴀᴛᴇᴅ</b>\n\n🏦 {name}", markup)
    raise ReturnCommand()
    pass


# ===/gwLimit===
def cmd_gwLimit():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    gid = str(params).strip()
    Gateways = Bot.getData("AddedGateways") or []
    gw = None
    for g in Gateways:
        if str(g.get("id")) == gid:
            gw = g
            break

    if gw is None:
        try:
            bot.answerCallbackQuery(call.id, "⚠️ Gᴀᴛᴇᴡᴀʏ Nᴏᴛ Fᴏᴜɴᴅ", show_alert=True)
        except:
            pass
        raise ReturnCommand()

    gmin = gw.get("min", 0) or 0
    gmax = gw.get("max", 0) or 0
    cur = ("₹" + str(gmin) + " - ₹" + str(gmax)) if (gmin or gmax) else "Global limit"

    User.saveData("gw_wait", "limit")
    User.saveData("gw_id", gid)
    User.saveData("gw_panel", message.message_id)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwView " + gid))

    text = (
        "📉 <b>Sᴇᴛ Lɪᴍɪᴛ Fᴏʀ " + str(gw.get("name")) + "</b>\n\nCᴜʀʀᴇɴᴛ: <b>" + cur + "</b>\n\n"
        "Sᴇɴᴅ Iɴ Tʜɪs Fᴏʀᴍᴀᴛ:\n<code>Min-Max</code>\n\n"
        "<b>Exᴀᴍᴘʟᴇ:</b> <code>10-500</code>\n"
        "Sᴇɴᴅ <code>0</code> Tᴏ Usᴇ Gʟᴏʙᴀʟ Lɪᴍɪᴛs."
    )

    show(message.message_id, text, markup)

    Bot.handleNextCommand("/gwLimit1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/gwLimit1===
def cmd_gwLimit1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    if str(User.getData("gw_wait")) != "limit":
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip().replace(" ", "")
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    gid = str(User.getData("gw_id") or "")
    panel = User.getData("gw_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwView " + gid))

    try:
        if raw == "0":
            lo = 0
            hi = 0
        else:
            a, b = raw.split("-")
            lo = float(a)
            hi = float(b)
            if lo < 0 or hi <= 0 or lo > hi:
                raise Exception()
            if lo == int(lo):
                lo = int(lo)
            if hi == int(hi):
                hi = int(hi)
    except:
        show(panel, "<b>❌ Iɴᴠᴀʟɪᴅ Fᴏʀᴍᴀᴛ!</b>\n\nSᴇɴᴅ Lɪᴋᴇ <code>10-500</code> (Mɪɴ Mᴜsᴛ Bᴇ Bᴇʟᴏᴡ Mᴀx) or <code>0</code> Fᴏʀ Gʟᴏʙᴀʟ.", back)
        Bot.handleNextCommand("/gwLimit1", cancel_at_command=True)
        raise ReturnCommand()

    Gateways = Bot.getData("AddedGateways") or []
    name = ""
    for g in Gateways:
        if str(g.get("id")) == gid:
            g["min"] = lo
            g["max"] = hi
            name = str(g.get("name"))
            break

    Bot.saveData("AddedGateways", Gateways)
    User.saveData("gw_wait", "")

    res = "Global limits" if (lo == 0 and hi == 0) else "₹" + str(lo) + " - ₹" + str(hi)

    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> Limit of {name} set to {res}"
    )
    Bot.saveData("AdmAC", AdmAC)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🏦 Oᴘᴇɴ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwView " + gid))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwManage"))

    show(panel, f"<b>✅ Lɪᴍɪᴛ Uᴘᴅᴀᴛᴇᴅ Sᴜᴄᴄᴇsғᴜʟʟʏ</b>\n\n🏦 {name} → <b>{res}</b>", markup)
    raise ReturnCommand()
    pass


# ===/gwManage===
def cmd_gwManage():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    User.saveData("gw_wait", "")

    Gateways = Bot.getData("AddedGateways") or []
    seq = int(Bot.getData("AddedGatewaysSeq") or 0)
    changed = False
    for g in Gateways:
        if "id" not in g:
            seq += 1
            g["id"] = seq
            g["on"] = True
            g["tax"] = 0
            changed = True
    if changed:
        Bot.saveData("AddedGateways", Gateways)
        Bot.saveData("AddedGatewaysSeq", seq)

    markup = InlineKeyboardMarkup()

    if not Gateways:
        text = "⚠️ <b>Sᴛɪʟʟ Nᴏ Aɴʏ Gᴀᴛᴇᴡᴀʏs Aᴅᴅᴇᴅ</b>"
        markup.add(InlineKeyboardButton(text="➕ Aᴅᴅ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwAdd"))
    else:
        text = "🛠 <b>Hᴇʀᴇ Yᴏᴜ Cᴀɴ Mᴀɴᴀɢᴇ Yᴏᴜʀ Pᴀʀᴛɪᴄᴜʟʟᴀʀ Gᴀᴛᴇᴡᴀʏ Sᴇᴛᴛɪɴɢs</b>\n\n"
        n = 0
        for g in Gateways:
            n += 1
            icon = "🟢" if g.get("on", True) else "🔴"
            text += f"{n}. {icon} <b>{g.get('name')}</b> (<code>{g.get('domain')}</code>) — Tax ₹{g.get('tax', 0)}\n"
        text += "\n👇 <b>Cʜᴏᴏsᴇ Bᴇʟᴏᴡ Gᴀᴛᴇᴡᴀʏ Tᴏ Cʜᴀɴɢᴇ Sᴇᴛᴛɪɴɢs</b>"

        row = []
        for g in Gateways:
            icon = "🟢" if g.get("on", True) else "🔴"
            row.append(InlineKeyboardButton(text=icon + " " + str(g.get("name")), callback_data="/gwView " + str(g.get("id"))))
            if len(row) == 2:
                markup.row(*row)
                row = []
        if row:
            markup.row(*row)

    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/addgetwayonbbot"))

    show(message.message_id, text, markup)
    raise ReturnCommand()
    pass


# ===/gwRef===
def cmd_gwRef():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    gid = str(params).strip()
    Gateways = Bot.getData("AddedGateways") or []
    gw = None
    for g in Gateways:
        if str(g.get("id")) == gid:
            gw = g
            break

    if gw is None:
        try:
            bot.answerCallbackQuery(call.id, "⚠️ Gᴀᴛᴇᴡᴀʏ Nᴏᴛ Fᴏᴜɴᴅ", show_alert=True)
        except:
            pass
        raise ReturnCommand()

    glob = Bot.getData("SetMinRef") or 0
    cur = gw.get("ref", 0) or 0
    cur_txt = str(cur) if cur else "Global (" + str(glob) + ")"

    User.saveData("gw_wait", "ref")
    User.saveData("gw_id", gid)
    User.saveData("gw_panel", message.message_id)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Back", callback_data="/gwView " + gid))

    text = (
        "👥 <b>Mɪɴ Rᴇғᴇʀ Fᴏʀ " + str(gw.get("name")) + "</b>\n\nCᴜʀʀᴇɴᴛ: <b>" + cur_txt + "</b>\n\n"
        "Sᴇɴᴅ Hᴏᴡ Mᴀɴʏ Vᴇʀɪғɪᴇᴅ Rᴇғᴇʀs Aʀᴇ Nᴇᴇᴅᴇᴅ Tᴏ Usᴇ Tʜɪs Gᴀᴛᴇᴡᴀʏ.\n"
        "Sᴇɴᴅ <code>0</code> to Usᴇ Tʜᴇ Gʟᴏʙᴀʟ Mɪɴ Rᴇғᴇʀ (" + str(glob) + ")."
    )

    show(message.message_id, text, markup)

    Bot.handleNextCommand("/gwRef1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/gwRef1===
def cmd_gwRef1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    if str(User.getData("gw_wait")) != "ref":
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip()
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    gid = str(User.getData("gw_id") or "")
    panel = User.getData("gw_panel")

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Back", callback_data="/gwView " + gid))

    try:
        n = int(raw)
        if n < 0 or n > 1000:
            raise Exception()
    except:
        show(panel, "<b>❌ Iɴᴠᴀʟɪᴅ Nᴜᴍʙᴇʀ.</b>\n\nSᴇɴᴅ ᴀ Wʜᴏʟᴇ Nᴜᴍʙᴇʀ Lɪᴋᴇ <code>3</code> (or <code>0</code> Fᴏʀ Gʟᴏʙᴀʟ).", back)
        Bot.handleNextCommand("/gwRef1", cancel_at_command=True)
        raise ReturnCommand()

    Gateways = Bot.getData("AddedGateways") or []
    name = ""
    for g in Gateways:
        if str(g.get("id")) == gid:
            g["ref"] = n
            name = str(g.get("name"))
            break

    Bot.saveData("AddedGateways", Gateways)
    User.saveData("gw_wait", "")

    res = str(n) if n else "Global (" + str(Bot.getData("SetMinRef") or 0) + ")"

    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []
    AdmAC.append(
        f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> Min Refer of {name} set to {res}"
    )
    Bot.saveData("AdmAC", AdmAC)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🏦 Oᴘᴇɴ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwView " + gid))
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwManage"))

    show(panel, f"<b>✅ Mɪɴ Rᴇғᴇʀ Uᴘᴅᴀᴛᴇᴅ</b>\n\n🏦 {name} → <b>{res}</b>", markup)
    raise ReturnCommand()
    pass


# ===/gwSameWallet===
def cmd_gwSameWallet():
    # Toggle: allow the SAME wallet number on multiple accounts (ON/OFF). Default OFF = 1 wallet = 1 account.
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    Admins = Bot.getData("AllBotAdminss") or []
    if not isinstance(Admins, list):
        Admins = [Admins]
    if str(u) not in [str(x) for x in Admins]:
        bot.replyText(u, "<b>🚫 Access Denied</b>", parse_mode="HTML")
        raise ReturnCommand()

    cur = Bot.getData("AllowSameWallet") or "OFF"
    new = "OFF" if cur == "ON" else "ON"
    Bot.saveData("AllowSameWallet", new)

    try:
        bot.answerCallbackQuery(call.id, "Same Wallet Allowed: " + new, show_alert=False)
    except:
        pass

    Bot.runCommand("/addgetwayonbbot")
    pass


# ===/gwTax===
def cmd_gwTax():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("tax", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()
    # coded by @Jenish_Dobariya1
    # params: "<id>"  or  "<id> tax"  or  "all tax"   (2nd word = where Back should go)

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    P = str(params).strip().split()
    gid = str(P[0]) if P else ""
    src = P[1] if len(P) > 1 else "gw"

    Gateways = Bot.getData("AddedGateways") or []

    if gid == "all":
        title = "All Gateways"
        current = "—"
    else:
        gw = None
        for g in Gateways:
            if str(g.get("id")) == gid:
                gw = g
                break
        if gw is None:
            try:
                bot.answerCallbackQuery(call.id, "⚠️ Gᴀᴛᴇᴡᴀʏ Nᴏᴛ Fᴏᴜɴᴅ", show_alert=True)
            except:
                pass
            raise ReturnCommand()
        title = str(gw.get("name"))
        current = "₹" + str(gw.get("tax", 0))

    if src == "tax":
        back_cb = "/SetTaxAmont"
    elif gid == "all":
        back_cb = "/SetTaxAmont"
    else:
        back_cb = "/gwView " + gid

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data=back_cb))

    User.saveData("gw_wait", "tax")
    User.saveData("gw_id", gid)
    User.saveData("gw_src", src)
    User.saveData("gw_panel", message.message_id)

    show(
        message.message_id,
        f"💸 <b>Sᴇᴛ Tᴀx Fᴏʀ {title}\n\nCᴜʀʀᴇɴᴛ: <b>{current}</b>\n\nSᴇɴᴅ Nᴇᴡ Tᴀx Aᴍᴏᴜɴᴛ (₹).\nSᴇɴᴅ <code>0</code> Fᴏʀ Nᴏ Tᴀx.</b>",
        markup
    )

    Bot.handleNextCommand("/gwTax1", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/gwTax1===
def cmd_gwTax1():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("tax", True) is False:
        raise ReturnCommand()

    # coded by @Jenish_Dobariya1

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()

    if str(User.getData("gw_wait")) != "tax":
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    raw = str(message.text or "").strip()
    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    gid = str(User.getData("gw_id") or "")
    src = str(User.getData("gw_src") or "gw")
    panel = User.getData("gw_panel")

    if src == "tax" or gid == "all":
        back_cb = "/SetTaxAmont"
    else:
        back_cb = "/gwView " + gid

    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data=back_cb))

    try:
        tax = float(raw)
    except:
        show(panel, "<b>❌ Iɴᴠᴀʟɪᴅ Aᴍᴏᴜɴᴛ.</b>\n\nSᴇɴᴅ A Nᴜᴍʙᴇʀ Lɪᴋᴇ <code>2</code> Oʀ <code>1.5</code>", back)
        Bot.handleNextCommand("/gwTax1", cancel_at_command=True)
        raise ReturnCommand()

    if tax < 0 or tax > 1000:
        show(panel, "<b>❌ Tᴀx Mᴜsᴛ Bᴇ Bᴇᴛᴡᴇᴇɴ 0 & 1000.</b>", back)
        Bot.handleNextCommand("/gwTax1", cancel_at_command=True)
        raise ReturnCommand()

    if tax == int(tax):
        tax = int(tax)

    Gateways = Bot.getData("AddedGateways") or []
    name = ""

    if gid == "all":
        for g in Gateways:
            g["tax"] = tax
        name = "Aʟʟ Gᴀᴛᴇᴡᴀʏs"
    else:
        for g in Gateways:
            if str(g.get("id")) == gid:
                g["tax"] = tax
                name = str(g.get("name"))
                break

    Bot.saveData("AddedGateways", Gateways)
    User.saveData("gw_wait", "")

    now = libs.DateAndTime.now("Asia/Kolkata")
    AdmAC = Bot.getData("AdmAC") or []

    AdmAC.append(
        f"<b>📆 Tɪᴍᴇ:</b> {now['date']} {now['time'][:5]}\n"
        f"👥 <b>Bʏ {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
        f"🔍 <b>Aᴄᴛɪᴏɴ:</b> Tᴀx Oғ {name} Sᴇᴛ Tᴏ ₹{tax}"
    )

    Bot.saveData("AdmAC", AdmAC)

    markup = InlineKeyboardMarkup()

    if gid != "all":
        markup.add(InlineKeyboardButton(text="🏦 Oᴘᴇɴ Gᴀᴛᴇᴡᴀʏ", callback_data="/gwView " + gid))

    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data=back_cb))

    show(panel, f"<b>✅ Tᴀx Uᴘᴅᴀᴛᴇᴅ</b>\n\n🏦 {name} → <b>₹{tax}</b>", markup)

    raise ReturnCommand()
    pass


# ===/gwView===
def cmd_gwView():
    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("gateway", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()


    def mask_url(url):
        try:
            url = str(url)
            if "?" not in url:
                return url
            base, q = url.split("?", 1)
            out = []
            for pair in q.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    kl = k.lower()
                    hide = False
                    for w in ["token", "key", "secret", "pass", "auth", "sign", "guid", "mid"]:
                        if w in kl:
                            hide = True
                    if hide and "{" not in v:
                        v = (v[:2] + "****") if len(v) > 4 else "****"
                    out.append(k + "=" + v)
                else:
                    out.append(pair)
            return base + "?" + "&".join(out)
        except:
            return "hidden"
    # coded by @Jenish_Dobariya1
    # params: "<id>"  or  "<id> toggle"  or  "<id> delask"  or  "<id> deldo"

    Admins = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in Admins]:
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


    def log(act):
        now = libs.DateAndTime.now("Asia/Kolkata")
        AdmAC = Bot.getData("AdmAC") or []
        AdmAC.append(
            f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
            f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
            f"🔍 <b>Action:</b> {act}"
        )
        Bot.saveData("AdmAC", AdmAC)


    User.saveData("gw_wait", "")

    P = str(params).strip().split()
    if not P:
        Bot.runCommand("/gwManage")
        raise ReturnCommand()

    gid = str(P[0])
    action = P[1] if len(P) > 1 else ""

    Gateways = Bot.getData("AddedGateways") or []
    gw = None
    for g in Gateways:
        if str(g.get("id")) == gid:
            gw = g
            break

    if gw is None:
        try:
            bot.answerCallbackQuery(call.id, "⚠️ Gᴀᴛᴇᴡᴀʏ Nᴏᴛ Fᴏᴜɴᴅ", show_alert=True)
        except:
            pass
        raise ReturnCommand()

    mid = message.message_id
    markup = InlineKeyboardMarkup()

    # ---------- DELETE CONFIRM ----------
    if action == "delask":
        text = f"🗑 <b>Dᴇʟᴇᴛᴇ {gw.get('name')}?</b>\n\nTʜɪs Cᴀɴɴᴏᴛ Bᴇ Uɴᴅᴏɴᴇ."
        markup.row(
            InlineKeyboardButton(text="✅ Yᴇs, Dᴇʟᴇᴛᴇ", callback_data="/gwView " + gid + " deldo"),
            InlineKeyboardButton(text="❌ Nᴏ", callback_data="/gwView " + gid)
        )
        show(mid, text, markup)
        raise ReturnCommand()

    if action == "deldo":
        newlist = []
        for g in Gateways:
            if str(g.get("id")) != gid:
                newlist.append(g)
        Bot.saveData("AddedGateways", newlist)
        log("Gateway Deleted: " + str(gw.get("name")))
        try:
            bot.answerCallbackQuery(call.id, "🗑 Dᴇʟᴇᴛᴇᴅ", show_alert=False)
        except:
            pass
        Bot.runCommand("/gwManage")
        raise ReturnCommand()

    # ---------- TOGGLE ----------
    if action == "toggle":
        gw["on"] = not gw.get("on", True)
        Bot.saveData("AddedGateways", Gateways)
        log("Gateway " + str(gw.get("name")) + " turned " + ("ON" if gw["on"] else "OFF"))

    # ---------- VIEW ----------
    is_on = gw.get("on", True)
    status = "🟢 ON" if is_on else "🔴 OFF"
    tax = gw.get("tax", 0)

    gmin = gw.get("min", 0) or 0
    gmax = gw.get("max", 0) or 0
    if gmin or gmax:
        lim_txt = "₹" + str(gmin) + " - " + ("₹" + str(gmax) if gmax else "No Max")
    else:
        lim_txt = "Global (₹" + str(Bot.getData("MinWith") or 0) + " - ₹" + str(Bot.getData("MaxWith") or 0) + ")"

    masked_url = mask_url(gw.get("url")).replace("&", "&amp;")
    gref = gw.get("ref", 0) or 0
    ref_txt = str(gref) if gref else "Global (" + str(Bot.getData("SetMinRef") or 0) + ")"

    text = f"""🏦 <b>{gw.get('name')}</b>

🌐 <code>{gw.get('domain')}</code>

🔗 <code>{masked_url}</code>

📶 <b>Sᴛᴀᴛᴜs:</b> {status}
💸 <b>Tᴀx:</b> ₹{tax}
📉 <b>Lɪᴍɪᴛs:</b> {lim_txt}
👥 <b>Mɪɴɪᴍᴜᴍ Rᴇғᴇʀ:</b> {ref_txt}"""

    markup.add(InlineKeyboardButton(
        text=("🔴 Turn OFF" if is_on else "🟢 Turn ON"),
        callback_data="/gwView " + gid + " toggle"
    ))
    markup.row(
        InlineKeyboardButton(text="💸 Sᴇᴛ Tᴀx", callback_data="/gwTax " + gid),
        InlineKeyboardButton(text="📉 Sᴇᴛ Lɪᴍɪᴛ", callback_data="/gwLimit " + gid)
    )
    markup.add(InlineKeyboardButton(text="👥 Mɪɴ Rᴇғᴇʀ", callback_data="/gwRef " + gid))
    markup.row(
        InlineKeyboardButton(text="✏️ Eᴅɪᴛ API", callback_data="/gwEdit " + gid),
        InlineKeyboardButton(text="🗑 Dᴇʟᴇᴛᴇ", callback_data="/gwView " + gid + " delask")
    )
    markup.add(InlineKeyboardButton(text="🔙 Bᴀᴄᴋ", callback_data="/gwManage"))

    show(mid, text, markup)
    raise ReturnCommand()
    pass


# ===/handler_special_updates===
def cmd_handler_special_updates():
    if update_type == "my_chat_member":
        chat = message.chat
        new_status = message.new_chat_member.status

        #proceed if bot gt add in channel
        if chat.type == "channel" and new_status == "administrator":
            chat_id = chat.id
            chat_username = chat.username
            L="link..."

            AllBotAdminss = Bot.getData("AllBotAdminss") or []

            lnkFound=True
            try:
                L=bot.exportChatInviteLink(message.chat.id)
            except:
                lnkFound=False

            if lnkFound==False or str(L)=="None": 
                for A in AllBotAdminss:
                    try:
                        bot.replyText(A,f"<b>🚫 Can't Add Invite Link To  {message.chat.id} Because Admin Detected, But No Permission To Invite Users. 😊</b>",parse_mode="HTML",disable_web_page_preview=True)
                    except:
                        pass
                raise ReturnCommand()

            AMC = Bot.getData("AllMainCh") or []
            AMCL=Bot.getData("AllMainChlink") or []
            AMCU=Bot.getData("AllMainChUsernm") or []

            AMCTGID=Bot.getData("AllMainChTGID") or []

            try:
                try:
                    IND=AMCTGID.index(int(message.chat.id))
                except:
                    IND=AMCTGID.index(str(message.chat.id))
                AMCL[IND]=L
                Bot.saveData("AllMainChlink",AMCL) 
            except:
                raise ReturnCommand()



            for A in AllBotAdminss:
                try:
                    bot.replyText(A,f"<b>🎉 Bot Is Admin In {AMCU[IND]} 🎉\n\n🔗 Invite Link Successfully Added Automatically! 😊</b>",parse_mode="HTML",disable_web_page_preview=True)

                    bot.replyText(A,f"<b>🔗 Invite Link {L} Added To {AMCU[IND]} Because Admin With Invite Users Permissions Was Detected. 😊</b>",parse_mode="HTML",disable_web_page_preview=True)

                except:
                    pass




    if update_type == "chat_join_request":
        user_id = message.from_user.id

        Bot.runCommand("/JoinReqApprove")

    if update_type == "chat_member":
        Bot.runCommand("/ChMemberTrack")
    pass


# ===/info===
def cmd_info():
    bot.sendMessage(
        '<b>🚀 Created By <a href="https://t.me/Jenil_AutoMakerbot">AutoPay Maker [Jenil]</a></b>',
        parse_mode="HTML"
    )
    pass


# ===/manualVerifKro===
def cmd_manualVerifKro():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    bot.replyText(u,"<b>💡 Send User Telegram Id To Approve</b>""",parse_mode = "html")

    Bot.handleNextCommand("/manualVerifKro1")
    pass


# ===/manualVerifKro1===
def cmd_manualVerifKro1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    now = libs.DateAndTime.now("Asia/Kolkata")
    date = now["date"]  
    time = now["time"][:5] 
    year, month, day = date.split("-")
    hour, minute = time.split(":")
    hour = int(hour)
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
    if hour > 12:
        hour -= 12
    if hour == 0:
        hour = 12

    MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


    AdmAC=Bot.getData("AdmAC") or []



    USE=message.text
    try:
        bot.replyText(USE,f"""<b>You Are Verified By Admin

Now You Can Use The Bot

Press /start</b>""",parse_mode = "html")

        bot.replyText(USE,f"""<b>User Can Use Bot Now</b>""",parse_mode = "html")
        Bot.saveData(str(USE) +"ManualAproval","Y")
    except:
        bot.replyText(u,f"""<b>Invalid Value</b>""",parse_mode = "html")


    act=f"Verified to {message.text}"
    AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

    Bot.saveData("AdmAC",AdmAC)



    pass


# ===/onWebhook===
def cmd_onWebhook():
    # ================= VALIDATION =================
    if options is None:
        raise ReturnCommand()

    if options.json is None:
        raise ReturnCommand()

    # ================= DELETE VERIFY MESSAGE =================
    verify_msg_id = User.getData("verify_msg_id")

    if verify_msg_id:
        try:
            bot.deleteMessage(
                chat_id=u,
                message_id=verify_msg_id
            )
        except:
            pass

        User.deleteData("verify_msg_id")

    # ================= RESPONSE =================
    res = options.json

    # ================= FIELDS (real process.js contract) =================
    fp = str(res.get("fingerprint", "")).strip()
    botusername = str(res.get("botusername", "")).strip()
    status = str(res.get("status", "")).lower().strip()
    attempt = bool(res.get("attempt", False))
    message_txt = str(res.get("message", "")).strip()

    if fp == "":
        bot.sendMessage(
            "❌ Fingerprint Missing"
        )
        raise ReturnCommand()

    # Save identifying info now that we have it from the response
    User.saveData("fingerprint", fp)
    User.saveData("botusername", botusername)

    # ================= VERIFIED (brand-new device) =================
    if status == "success":

        User.saveData("verify", "ok")
        User.saveData("PhVerification", "Verified")

        bot.sendMessage(
            """
<b>🎉 Verification Successful!

✅ Welcome To Wallet Bot.</b>
""",
            parse_mode="HTML"
        )

        Bot.runCommand(
            "/PIRO_MainMenu"
        )

        raise ReturnCommand()


    # ================= ALREADY VERIFIED (same device, same user) =================
    elif attempt:

        User.saveData("verify", "ok")
        User.saveData("PhVerification", "Verified")

        Bot.runCommand(
            "/PIRO_MainMenu"
        )

        raise ReturnCommand()


    # ================= BLOCKED (same device, different user - clone attempt) =================
    elif status == "failed":

        Bot.saveData(str(u) + "sameDev", "yes")

        User.saveData("verify", "ok")
        User.saveData("PhVerification", "Verified")

        Bot.runCommand(
            "/PIRO_MainMenu",
            options="""
<b>⚠️ Same Device Detected

🎁 Referral Bonus Not Available.

🥳 But You Can Still Use The Bot.</b>
"""
        )

        raise ReturnCommand()


    # ================= UNKNOWN / ERROR =================
    else:

        User.saveData("verify", "pending")

        bot.sendMessage(
            """
<b>❌ Verification Failed

🔄 Please Try Again.</b>
""",
            parse_mode="HTML"
        )

        Bot.runCommand(
            "/PIRO_Verification"
        )

        raise ReturnCommand()

    pass


# ===/permission===
def cmd_permission():
    # ==============================
    # ADMIN PERMISSION PANEL
    # ==============================

    Owner = Bot.getData("Owner") or None

    if str(u) != str(Owner):
        bot.replyText(
            u,
            "<b>🚫 Only Bot Owner Can Manage Permissions</b>",
            parse_mode="HTML"
        )
        raise ReturnCommand()


    # ==============================
    # PERMISSIONS
    # ==============================

    Permissions = Bot.getData("AdminPermissions") or {}


    # ==============================
    # TOGGLE PERMISSION
    # ==============================

    if str(params) != "None" and str(params) != "":
        permission = str(params)

        current = Permissions.get(permission, True)
        Permissions[permission] = not current

        Bot.saveData("AdminPermissions", Permissions)


    # Reload
    Permissions = Bot.getData("AdminPermissions") or {}


    # ==============================
    # STATUS
    # ==============================

    DEFAULT_OFF_PERMISSIONS = [
        "permission",
        "reset_balance",
    ]

    def P(key):
        if key in Permissions:
            return "✅" if Permissions[key] else "❌"

        if key in DEFAULT_OFF_PERMISSIONS:
            return "❌"

        return "✅"

    # ==============================
    # MENU
    # ==============================

    markup = InlineKeyboardMarkup()


    markup.add(
        InlineKeyboardButton(
            text="🧑‍💼 Mᴀɴᴀɢᴇ Aᴅᴍɪɴ Pᴇʀᴍɪssɪᴏɴ " + P("permission"),
            callback_data="/permission permission"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="👑 Tʀᴀɴsғᴇʀ Oᴡɴᴇʀsʜɪᴘ " + P("transfer_owner"),
            callback_data="/permission transfer_owner"
        ),
        InlineKeyboardButton(
            text="⚖️ Tᴀx ~ " + str(Bot.getData("Tax") or 0) + "% " + P("tax"),
            callback_data="/permission tax"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🛡 Vᴇʀɪғɪᴄᴀᴛɪᴏɴ " + P("verification"),
            callback_data="/permission verification"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="👮 Mᴀɴᴀɢᴇ Aᴅᴍɪɴs " + P("admins"),
            callback_data="/permission admins"
        ),
        InlineKeyboardButton(
            text="⛔ Mᴀɴᴀɢᴇ Bᴀɴ Usᴇʀs " + P("ban_unban"),
            callback_data="/permission ban_unban"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="💳 Sᴇᴛ Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ " + P("payout_channel"),
            callback_data="/permission payout_channel"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="💸 Wɪᴛʜᴅʀᴀᴡ: " + P("withdraw"),
            callback_data="/permission withdraw"
        ),
        InlineKeyboardButton(
            text="🤖 Bᴏᴛ Sᴛᴀᴛᴜs: " + P("bot_status"),
            callback_data="/permission bot_status"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="➕ Aᴅᴅ Bᴀʟᴀɴᴄᴇ " + P("balance"),
            callback_data="/permission balance"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🙇🏻‍♂️ Vᴇʀɪғʏ Usᴇʀ " + P("verify_user"),
            callback_data="/permission verify_user"
        ),
        InlineKeyboardButton(
            text="🚀 Mᴀɴᴀɢᴇ Tᴇxᴛ " + P("manage_text"),
            callback_data="/permission manage_text"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="⚡ Mᴀɴᴀɢᴇ Cʜᴀɴɴᴇʟs " + P("channels"),
            callback_data="/permission channels"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="⚠️ Rᴇsᴇᴛ Bᴀʟᴀɴᴄᴇ " + P("reset_balance"),
            callback_data="/permission reset_balance"
        ),
        InlineKeyboardButton(
            text="🎙️ Bʀᴏᴀᴅᴄᴀsᴛ " + P("broadcast"),
            callback_data="/permission broadcast"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="📉 Wɪᴛʜᴅʀᴀᴡᴀʟ Lɪᴍɪᴛs " + P("withdraw_limits"),
            callback_data="/permission withdraw_limits"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="👥 Pᴇʀ Rᴇғᴇʀ ~ " + P("refer"),
            callback_data="/permission refer"
        ),
        InlineKeyboardButton(
            text="🎁 Bᴏɴᴜs ~ " + P("bonus"),
            callback_data="/permission bonus"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="📊 Sᴛᴀᴛɪsᴛɪᴄs " + P("statistics"),
            callback_data="/permission statistics"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="💬 Tᴀʟᴋ Wɪᴛʜ Usᴇʀ " + P("talk_user"),
            callback_data="/permission talk_user"
        ),
        InlineKeyboardButton(
            text="ℹ️ Usᴇʀ Dᴇᴛᴀɪʟs " + P("user_details"),
            callback_data="/permission user_details"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="📝 Rᴇᴄᴇɴᴛ Aᴅᴍɪɴ Aᴄᴛɪᴏɴs " + P("admin_actions"),
            callback_data="/permission admin_actions"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🥇 Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ " + P("leaderboard"),
            callback_data="/permission leaderboard"
        ),
        InlineKeyboardButton(
            text="📴 Bᴏᴛ Oғғ Tᴇxᴛ " + P("click_here"),
            callback_data="/permission click_here"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🔔 Nᴇᴡ Usᴇʀ Nᴏᴛɪғɪᴄᴀᴛɪᴏɴ " + P("new_user_notification"),
            callback_data="/permission new_user_notification"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🎟 Gɪғᴛ Cᴏᴅᴇs " + P("gift_codes"),
            callback_data="/permission gift_codes"
        ),
        InlineKeyboardButton(
            text="📴 Wɪᴛʜᴅʀᴀᴡ Oғғ Tᴇxᴛ " + P("withdraw_off_text"),
            callback_data="/permission withdraw_off_text"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🌐 Gᴀᴛᴇᴡᴀʏ Sᴇᴛᴜᴘ " + P("gateway"),
            callback_data="/permission gateway"
        )
    )


    markup.add(
        InlineKeyboardButton(
            text="🔙 Bᴀᴄᴋ",
            callback_data="/admin AP"
        )
    )


    # ==============================
    # TEXT
    # ==============================

    T = """<b>🧑‍💼 Aᴅᴍɪɴ Pᴇʀᴍɪssɪᴏɴ</b>

<b><i>Mᴀɴᴀɢᴇ Pᴇʀᴍɪssɪᴏɴs Fᴏʀ Aʟʟ Bᴏᴛ Aᴅᴍɪɴs</i>

<b>✅</b> Pᴇʀᴍɪssɪᴏɴ Gɪᴠᴇɴ
<b>❌</b> Pᴇʀᴍɪssɪᴏɴ Dɪsᴀʙʟᴇᴅ

<i>Tᴀᴘ Aɴʏ Bᴜᴛᴛᴏɴ Tᴏ Cʜᴀɴɢᴇ Aᴅᴍɪɴ Pᴇʀᴍɪssᴏɴ.</i></b>"""


    # ==============================
    # FULL EDIT MESSAGE
    # ==============================

    try:
        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=T,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except:
        bot.replyText(
            u,
            T,
            reply_markup=markup,
            parse_mode="HTML"
        )
    pass


# ===/savesocial===
def cmd_savesocial():
    global msg
    # Save Social Link (name + link) or Uncheck Channel (link only). Never crashes on a wrong link.

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        raise ReturnCommand()

    mode = str(User.getData("ch_wait"))
    if mode not in ["social", "uncheck"]:
        raise ReturnCommand()


    def esc(t):
        return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    def clean_url(raw):
        s = str(raw or "").strip().strip("<>").strip()
        if not s or " " in s or "\n" in s or len(s) > 500 or s.startswith("/"):
            return None
        low = s.lower()
        if s.startswith("@"):
            un = s[1:]
            if len(un) < 4 or len(un) > 32:
                return None
            for ch in un:
                if not (ch.isalnum() or ch == "_"):
                    return None
            return "https://t.me/" + un
        if low.startswith("t.me/") or low.startswith("telegram.me/"):
            s = "https://" + s
            low = s.lower()
        if low.startswith("tg://"):
            return s if len(s) > 8 else None
        if low.startswith("https://") or low.startswith("http://"):
            rest = s.split("://", 1)[1]
            host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            if "." not in host or host.startswith(".") or host.endswith(".") or len(host) < 4:
                return None
            for ch in host:
                if not (ch.isalnum() or ch in ".-:"):
                    return None
            return s
        return None


    def auto_name(url):
        try:
            low = url.lower()
            if low.startswith("tg://"):
                return "Join Channel"
            rest = url.split("://", 1)[1]
            host = rest.split("/", 1)[0]
            if "t.me" in host or "telegram.me" in host:
                path = rest.split("/", 1)[1] if "/" in rest else ""
                first = path.split("?")[0].split("/")[0]
                if first and not first.startswith("+") and first.lower() not in ["joinchat", "c", "addlist"]:
                    return "@" + first[:20]
                return "Join Channel"
            host = host.replace("www.", "")
            nm = host.split(".")[0].capitalize()
            return nm if nm else "Link"
        except:
            return "Join Channel"


    raw = str(message.text or "").strip()

    if raw.startswith("/"):
        raise ReturnCommand()

    try:
        bot.deleteMessage(u, message.message_id)
    except:
        pass

    panel = User.getData("ch_panel")
    back = InlineKeyboardMarkup()
    back.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    # premium (custom) Telegram emoji are not supported
    _premium = False
    try:
        for _e in (message.entities or []):
            _t = None
            try:
                _t = _e.type
            except:
                try:
                    _t = _e["type"]
                except:
                    _t = None
            if str(_t) == "custom_emoji":
                _premium = True
    except:
        pass

    if _premium:
        show(panel, "<b>❌ Invalid input.</b>\n\nUse plain text only, then send again.", back)
        Bot.handleNextCommand("/savesocial", cancel_at_command=True)
        raise ReturnCommand()

    added = []
    skipped = []
    failed = ""

    try:
        SocialLinks = Bot.getData("AllSocialLinks") or []

        for line in raw.split("\n"):
            try:
                line = line.strip()
                if not line:
                    continue

                name = None
                link = None

                if mode == "uncheck":
                    # only a link is needed
                    part = line.split("|", 1)[1] if "|" in line else line
                    link = clean_url(part)
                    if link is None:
                        skipped.append(line[:40] + " → invalid link")
                        continue
                    name = auto_name(link)
                else:
                    # social: button name AND link are both required
                    # format: Name - link   (the link is found first, everything before it is the name)
                    low_line = line.lower()
                    cut = -1
                    for mk_ in ["https://", "http://", "tg://", "t.me/", "telegram.me/"]:
                        p_ = low_line.find(mk_)
                        if p_ != -1 and (cut == -1 or p_ < cut):
                            cut = p_
                    if cut != -1:
                        name = line[:cut]
                        link = clean_url(line[cut:])
                    else:
                        parts_ = line.rsplit(None, 1)
                        name = parts_[0] if len(parts_) == 2 else ""
                        link = clean_url(parts_[-1])

                    name = name.strip().rstrip("-|:").strip()
                    if not name:
                        skipped.append(line[:40] + " → name missing (use Name - link)")
                        continue

                    if link is None:
                        skipped.append(line[:40] + " → invalid link")
                        continue

                    name = name.replace("<", "").replace(">", "").strip()[:30]
                    if not name or name.startswith("/"):
                        skipped.append(line[:40] + " → invalid name")
                        continue

                if len(SocialLinks) >= 30:
                    skipped.append(name + " → limit 30 reached")
                    continue

                found = False
                for s in SocialLinks:
                    if str(s.get("url")).lower() == link.lower():
                        s["name"] = name
                        s["type"] = mode
                        found = True
                        break

                if not found:
                    SocialLinks.append({"name": name, "url": link, "type": mode})

                added.append(name + " → " + link)
            except Exception as e1:
                skipped.append(str(line)[:40] + " → could not read")

        if added:
            Bot.saveData("AllSocialLinks", SocialLinks)

            now = libs.DateAndTime.now("Asia/Kolkata")
            AdmAC = Bot.getData("AdmAC") or []
            AdmAC.append(
                f"<b>📆 Time:</b> {now['date']} {now['time'][:5]}\n"
                f"👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n"
                f"🔍 <b>Action:</b> " + ("Uncheck channel" if mode == "uncheck" else "Social link") + " added: " + ", ".join([a.split(" → ")[0] for a in added])
            )
            Bot.saveData("AdmAC", AdmAC)
    except Exception as e:
        failed = str(e)[:100]

    # ---------- error: never crash, ask again ----------
    if failed:
        show(
            panel,
            "<b>⚠️ Something went wrong, nothing was saved.</b>\n\n<code>" + esc(failed) + "</code>\n\nPlease send again.",
            back
        )
        Bot.handleNextCommand("/savesocial", cancel_at_command=True)
        raise ReturnCommand()

    if not added:
        if mode == "uncheck":
            msg = "<b>❌ No valid link found!</b>\n\nSend only the link, like:\n<code>https://t.me/mychannel</code>"
        else:
            msg = "<b>❌ No valid link found!</b>\n\nSend both name and link:\n<code>Name - https://link.com</code>"
        if skipped:
            msg += "\n\n<b>Skipped:</b>\n" + "\n".join(["• " + esc(x) for x in skipped])
        show(panel, msg, back)
        Bot.handleNextCommand("/savesocial", cancel_at_command=True)
        raise ReturnCommand()

    User.saveData("ch_wait", "")

    msg = "<b>✅ Added " + str(len(added)) + (" Uncheck Channel(s)" if mode == "uncheck" else " Social Link(s)") + "</b>\n\n" + "\n".join(["• " + esc(a) for a in added])
    if skipped:
        msg += "\n\n<b>⚠️ Skipped:</b>\n" + "\n".join(["• " + esc(x) for x in skipped])

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="➕ Add More", callback_data=("/uncheck" if mode == "uncheck" else "/social")))
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(panel, msg, mk)
    raise ReturnCommand()
    pass


# ===/setcool===
def cmd_setcool():
    def save_cooltime(minutes):
        try:
            minutes = int(minutes)
            if minutes <= 0:
                minutes = 1   # minimum 1 minute to avoid 0 or negative
            Bot.setData("Cooltime", minutes)
            return f"✅ Cooltime updated to {minutes} minutes"
        except:
            return "❌ Invalid value. Please enter a number"

    pass


# ===/setwallet===
def cmd_setwallet():
    # coded by @Jenish_Dobariya1

    WalletStatus = Bot.getData("WalletWithdraw") or "ON"
    if WalletStatus == "OFF":
        bot.replyText(u, "<b>⛔ Wallet Withdraw Temporarily Disabled</b>", parse_mode="HTML")
        raise ReturnCommand()

    #Cancel Operation Flow
    if str(message.text) == "Cancel":
        Bot.runCommand("/PIRO_MainMenu")
        raise ReturnCommand()

    # Anti-Fraud Verification Block (1 Wallet = 1 Account Only) 
    all_users = Bot.getData("AllUsers") or []
    wallet_in_use = False
    input_wallet = str(message.text).strip()

    for user_id in ([] if (Bot.getData("AllowSameWallet") or "OFF") == "ON" else all_users):
        if str(user_id) == str(u):
            continue # Apne hi purane data ko verification me skip karega

        saved_wallet = Bot.getData("UserWallet" + str(user_id))
        if str(saved_wallet) == input_wallet:
            wallet_in_use = True
            break

    if wallet_in_use:
        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"<b>⛔ This Wallet Number Is Already Linked To Another User!</b>\n\n💡 Please enter a different one."
        )
        raise ReturnCommand()

    #  String Type Validation Check (Strict 10-Digit Rules)
    if input_wallet.isdigit() and len(input_wallet) == 10:
        Bot.saveData("UserWallet" + str(u), input_wallet)

        # Automatically map entry into registry
        if str(u) not in map(str, all_users):
            all_users.append(str(u))
            Bot.saveData("AllUsers", all_users)

        Bot.runCommand(
            "/PIRO_MainMenu",
            options=f"<b>✅ Your Wallet Number Has Been Successfully Updated To:\n\n👉 Wallet : </b><code>{input_wallet}</code>"
        )

        # Referral Metrics Tracking System
        upLnk = User.getData("upLnk") or "n"
        if upLnk == "n":
            T_usLnkWallet = Bot.getData('T_usLnkWallet') or 0
            Bot.saveData('T_usLnkWallet', int(T_usLnkWallet) + 1)
            User.saveData("upLnk", "y")

            refBy = Bot.getData(str(u) + "Referral") or "NONE"
            if refBy != "NONE":
                RefWalletLnkC = Bot.getData(str(refBy) + "RefWalletLnkC") or 0
                Bot.saveData(str(refBy) + "RefWalletLnkC", int(RefWalletLnkC) + 1)

    else:
        Bot.runCommand(
            "/PIRO_MainMenu",
            options="<b>⛔ Invalid Wallet Number!</b>\n\n💡 It must be a 10-digit number only."
        )
        raise ReturnCommand()

    pass


# ===/shareprofile===
def cmd_shareprofile():
    try:

        # ============================================================
        # 🛑 CHECK CONTACT VERIFICATION SYSTEM
        # ============================================================

        VerifySystemStatus = Bot.getData(
            "VerifySystemStatus"
        ) or "OFF"


        # If Contact Verification is OFF
        if VerifySystemStatus != "ON":

            Bot.runCommand(
                "/PIRO_MainMenu"
            )

            raise ReturnCommand()


        # ============================================================
        # 📱 INCOMING CONTACT
        # ============================================================

        contact = message.contact


        if contact:

            phone_number = contact.phone_number
            user_id = contact.user_id
            current_sender_id = message.from_user.id


            # ========================================================
            # 🔐 SECURITY CHECK
            # ========================================================

            if user_id and str(user_id) != str(current_sender_id):

                bot.sendMessage(
                    "❌ <b>Verification Failed!</b>\n\n"
                    "Aapko apna khud ka number share karna hoga.",
                    parse_mode="HTML"
                )

                Bot.handleNextCommand(
                    "/shareprofile"
                )

                raise ReturnCommand()


            # ========================================================
            # ✅ SAVE CONTACT VERIFICATION
            # ========================================================

            User.saveData(
                "user_phone",
                str(phone_number)
            )

            User.saveData(
                "is_verified",
                True
            )


            # ========================================================
            # 🔐 CAPTCHA CHECK
            # ========================================================

            Captcha = Bot.getData(
                "CaptchaMode"
            ) or "ON"


            if Captcha == "ON":

                verify = User.getData(
                    "verify"
                )


                if verify != "ok":

                    # 🧹 DELETE CONTACT MESSAGE
                    try:

                        current_msg_id = message.message_id

                        bot.deleteMessage(
                            chat_id=current_sender_id,
                            message_id=current_msg_id
                        )

                        bot.deleteMessage(
                            chat_id=current_sender_id,
                            message_id=current_msg_id - 1
                        )

                    except:

                        pass


                    # 🚀 GO TO MAIN MENU
                    # MainMenu will send user to CAPTCHA
                    Bot.runCommand(
                        "/PIRO_MainMenu"
                    )

                    raise ReturnCommand()


            # ========================================================
            # 🧹 DELETE CONTACT MESSAGES
            # ========================================================

            try:

                current_msg_id = message.message_id

                bot.deleteMessage(
                    chat_id=current_sender_id,
                    message_id=current_msg_id
                )

                bot.deleteMessage(
                    chat_id=current_sender_id,
                    message_id=current_msg_id - 1
                )

            except:

                pass


            # ========================================================
            # 🚀 ALL VERIFICATION PASSED
            # ========================================================

            Bot.runCommand(
                "/PIRO_MainMenu"
            )

            raise ReturnCommand()


        # ============================================================
        # 📱 INITIAL CONTACT PROMPT
        # ============================================================

        reply_markup = {
            "keyboard": [
                [
                    {
                        "text": "📱 Share Profile",
                        "request_contact": True
                    }
                ]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }


        bot.sendMessage(
            "<b>🔒 Please Verify Your Profile To Avoid Fake Accounts\n\n"
            "⚠️ Your Profile Is Not Shared With Anyone & Is Safe !!</b>",
            parse_mode="HTML",
            reply_markup=reply_markup
        )


        # ============================================================
        # 🔁 HANDLE NEXT CONTACT
        # ============================================================

        Bot.handleNextCommand(
            "/shareprofile"
        )

        raise ReturnCommand()


    except Exception as e:

        if type(e).__name__ != "ReturnCommand":

            pass
    pass


# ===/social===
def cmd_social():
    # Add Social Link - prompt (button name + link both required)

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b>🚫 You Are Not Authorized!</b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    User.saveData("ch_wait", "social")
    User.saveData("ch_panel", message.message_id)

    text = """➕ <b>Add Social Link</b>

Both <b>button name</b> and <b>link</b> are required.

Send in this format:
<code>Name - https://link.com</code>

<b>Examples:</b>
<code>YouTube - https://youtube.com/@mychannel
Telegram - https://t.me/mychannel
Instagram - https://instagram.com/myname</code>

<i>Multiple lines supported. Only valid links are saved.</i>"""

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(message.message_id, text, mk)

    Bot.handleNextCommand("/savesocial", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/start===
def cmd_start():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    if message.chat.type != "private": raise ReturnCommand()

    AllBotAdminss=Bot.getData("AllBotAdminss") or []
    if AllBotAdminss==[]:
        MAIN_ADMIN=u
        AllBotAdminss.append(MAIN_ADMIN)
        Bot.saveData("AllBotAdminss",AllBotAdminss)
        Bot.saveData("Owner",u)
        AllBotAdminss=Bot.getData("AllBotAdminss") or []

    BotMode=Bot.getData("BotMode") or "ON"
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    if Bot.getData("Bot7DayTimer")!="STARTED":
        Bot.saveData("Bot7DayTimer","STARTED")
        Bot.runCommandAfter(604800,"/AutoCheckBot")

    # 🆕 New user / Refer By / Total users / Bot users logic ab "@" command me hai

    BotAdmins=Bot.getData("AllBotAdminss") or [str(u)]
    joinSTAT="JOIND"

    AMC=Bot.getData("AllMainCh") or []
    AMCU=Bot.getData("AllMainChUsernm") or []
    AMCL=Bot.getData("AllMainChlink") or []
    AMCTGID=Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL=[]
    BINA=False
    IND=0

    for i in AMCTGID:
        try: UC=bot.getChatMember(i,u).status
        except: UC="error"

        ReqRec=Bot.getData(str(i)+"JoinRRq"+str(u)) or "N"

        if UC=="left" and ReqRec=="N":
            joinSTAT="NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None); libs.Resources.globalRes("ChShowS" + str(i).replace("-", "m")).add(1)

        DC=Bot.getData(str(i)+"isDef") or "Y"
        JR=Bot.getData(str(i)+"JoinReq") or "Y"

        if UC=="error" and JR=="Y":
            if DC=="Y":
                IND+=1
                continue

            for admin in BotAdmins:
                try:
                    bot.replyText(admin,f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                except: pass

            BINA=True

        IND+=1

    User.saveData("PIRO_NJCL",PIRO_NJCL)

    if BINA: raise ReturnCommand()

    Captcha=Bot.getData("CaptchaMode") or "ON"

    if joinSTAT=="JOIND":
        if Captcha=="OFF": User.saveData("PhVerification","verify")
        Bot.runCommand("/PIRO_MainMenu")
        raise ReturnCommand()

    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard=[]
        row=[]
        IND=0

        # channels + social/uncheck links, shown in the order set in the admin panel
        SocialLinks = Bot.getData("AllSocialLinks") or []
        LinkOrder = Bot.getData("AllLinkOrder") or []

        JM_TXT = ""
        ch_btn = {}
        for _k in range(len(AMCL)):
            if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or Bot.getData("JoinMode") == "ALL") and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

        so_btn = {}
        for s in SocialLinks:
            _u = str(s.get("url", ""))
            if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

        ordered = []
        for _t in LinkOrder:
            if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                ordered.append(_t)
        for _t in ch_btn:
            if _t not in ordered:
                ordered.append(_t)
        for _t in so_btn:
            if _t not in ordered:
                ordered.append(_t)

        row = []
        for _t in ordered:
            row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([{"text":"🔒 Claim","callback_data":"🟢 Joined","style":"success"}])

        usr=f'<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>'

        PIRO=bot.sendMessage(
            f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""",
            reply_markup={"inline_keyboard":keyboard}
        )

        User.saveData("PIRO_ltmg",PIRO.message_id)
    pass


# ===/uncheck===
def cmd_uncheck():
    # Add Uncheck Channel - prompt (only the link is needed)

    # permission guard (owner always allowed)
    _perms = Bot.getData("AdminPermissions") or {}
    if str(u) != str(Bot.getData("Owner")) and _perms.get("channels", True) is False:
        try:
            bot.answerCallbackQuery(call.id, "🚫 You don't have permission for this", show_alert=True)
        except:
            bot.replyText(u, "<b>🚫 You don't have permission for this</b>", parse_mode="HTML")
        raise ReturnCommand()

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    if str(u) not in [str(x) for x in AllBotAdminss]:
        bot.replyText(u, "<b>🚫 You Are Not Authorized!</b>", parse_mode="HTML")
        raise ReturnCommand()


    def show(mid, text, markup):
        try:
            bot.editMessageText(chat_id=u, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e).lower():
                try:
                    bot.replyText(u, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                except:
                    pass


    User.saveData("ch_wait", "uncheck")
    User.saveData("ch_panel", message.message_id)

    text = """<b>Add Uncheck Channel</b>

Only the <b>link</b> is needed (no name). Users are <b>not</b> checked for joining.

<b>Examples:</b>
<code>https://t.me/mychannel
@mychannel
https://t.me/+AbCdEf123</code>

<i>Multiple lines supported. Only valid links are saved.</i>"""

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton(text="🔙 Back", callback_data="/PIRO_Chanel_Pannel"))

    show(message.message_id, text, mk)

    Bot.handleNextCommand("/savesocial", cancel_at_command=True)
    raise ReturnCommand()
    pass


# ===/verifiedStats===
def cmd_verifiedStats():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    AllBotAdminss = Bot.getData("AllBotAdminss") or []

    is_Admin = False
    for userid in AllBotAdminss:
        if str(u) == str(userid):
            is_Admin = True

    if is_Admin != True:
        bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
        raise ReturnCommand()


    T_usLnkWallet=Bot.getData('T_usLnkWallet') or 0
    OWNER_ID = Bot.getData("Owner")

    owner = bot.getChatMember(OWNER_ID, OWNER_ID)
    owner_name = owner.user.first_name
    T_usClimBon=Bot.getData('T_usClimBon') or 0

    T_verifUsrs=Bot.getData('T_verifUsrs') or 0

    FulUsrs=Bot.getData("FulBotUsrs") or []

    # Verified = only successful verifications (same-device users excluded)
    _sameDev = 0
    for _uid in FulUsrs:
        if (Bot.getData(str(_uid) + "sameDev") or "no") == "yes":
            _sameDev += 1
    T_verifUsrs = max(0, int(T_verifUsrs) - _sameDev)


    markup = InlineKeyboardMarkup()

    markup.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="/RawStats"
        )
    )


    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=f"""<b>👍Total Users In Bot : {len(FulUsrs)} Users

👍Total Verified Users In Bot : {T_verifUsrs} Users

👍Total Users Claimed Bonus : {T_usClimBon} Users

👍Total Users Linked Wallet : {T_usLnkWallet} Users

📍 Powered By - <a href="tg://user?id={OWNER_ID}">{owner_name}</a></b>""",
        parse_mode="html",
        reply_markup=markup
    )
    pass


# ===@===
def cmd__():
    if message.update_type == "chat_join_request":
        user_id = u
        CHTGID = message.chat.id

        Bot.runCommand("/JoinReqApprove")


    # Ensure it's private chat only
    if message.chat.type != "private":
        raise ReturnCommand()


    # ============================================================
    # 🆕 NEW USER + REFER BY + TOTAL/BOT USERS (moved from /start)
    # Koi bhi kuch bhi bheje, pehli baar aane par sab yahin count hoga
    # ============================================================

    NU_params = params
    if NU_params == None:
        try:
            _txt = str(message.text or "").strip()
            if _txt.startswith("/start ") and len(_txt.split(" ", 1)[1].strip()) > 0:
                NU_params = _txt.split(" ", 1)[1].strip()
        except:
            NU_params = None

    NU_referalID = None
    if NU_params != None:
        NU_referalID = Bot.getData(str(NU_params) + "GetTgIDbyRefLetters")

    refer = NU_params

    if NU_params != None:
        A = NU_params
        if str(A[slice(3)]) == "ref":
            refer = NU_referalID

    if NU_params == None:
        refer = "@not_referred"

    # 🚫 Self-referral block: user apne hi link se aaye to "not referred" maano
    if str(refer) == str(u):
        refer = "@not_referred"

    is_new_user = False

    Neww = Bot.getData("UserID" + str(u))
    if Neww == None:
        is_new_user = True
        Bot.saveData("UserID" + str(u), "done")

        FulUsrs = Bot.getData("FulBotUsrs") or []
        FulUsrs.append(u)
        Bot.saveData("FulBotUsrs", FulUsrs)

        RefStrtC = Bot.getData(str(refer) + "RefStrtC") or 0
        Bot.saveData(str(refer) + "RefStrtC", RefStrtC + 1)

    already = User.getData("bot_user")

    if already == None:
        Bot.saveData(str(u) + "Referral", refer)

        User.saveData("bot_user", True)

        T_usrs = Bot.getData("total_users")
        if T_usrs == None:
            Bot.saveData("total_users", 1)
        else:
            Bot.saveData("total_users", int(T_usrs) + 1)


    def clean_html(text):
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


    # ============================================================
    # 👥 ALL ADMINS
    # ============================================================

    AllBotAdminss = Bot.getData("AllBotAdminss") or []


    # ============================================================
    # 🚫 CHECK BANNED
    # ============================================================

    PIRO_ban = Bot.getData(f"{u}ban") or False

    if PIRO_ban:
        bot.replyText(
            u,
            "<b>🚫 You're banned in this bot</b>"
        )
        raise ReturnCommand()


    # ============================================================
    # 🔔 NEW USER NOTIFICATION
    # ============================================================

    NewUserNotification = Bot.getData("NewUserNotification") or "ON"

    if NewUserNotification == "ON":
        NUN_key = "NUN_" + str(u)

        if is_new_user and not Bot.getData(NUN_key):
            Bot.saveData(NUN_key, "1")

            # refer upar (NEW USER block) me already nikal chuka hai
            if refer == None or str(refer) in ("None", "@not_referred"):
                invited_text = "🤖 Auto Started"
            else:
                refer = str(refer)
                ref_uname = ""
                try:
                    ref_chat = bot.getChat(refer)
                    if ref_chat.username:
                        ref_uname = "@" + str(ref_chat.username)
                except:
                    ref_uname = ""

                if ref_uname:
                    invited_text = f'{ref_uname} [<code>{refer}</code>]'
                else:
                    invited_text = f'<code>{refer}</code> (No Username)'

            NUN_name = "Trying to fetch"
            NUN_uname = ""

            try:
                NUN_name = str(message.from_user.first_name or "Trying to fetch")
            except:
                NUN_name = "Trying to fetch"

            try:
                if message.from_user.username:
                    NUN_uname = "@" + str(message.from_user.username)
            except:
                NUN_uname = ""

            safe_name = clean_html(NUN_name)

            notify_text = f"""
<b><b>🔍 New User Joined! 🎉</b>

🙇 <b>User:</b> {safe_name}
✅ <b>Username:</b> {NUN_uname if NUN_uname else "No Username"}
🆔 <b>User ID:</b> <code>{u}</code>
👥 <b>Referred By:</b> {invited_text}</b>
"""

            # ====================================================
            # 🔘 JSON INLINE KEYBOARD
            # ====================================================

            notify_keyboard = []

            notify_keyboard.append([
                {
                    "text": "👤 View New User",
                    "url": f"tg://user?id={u}"
                }
            ])

            if refer != None and refer != "@not_referred" and str(refer).isdigit():
                notify_keyboard.append([
                    {
                        "text": "👥 View Referral",
                        "url": f"tg://user?id={refer}"
                    }
                ])

            NUN_Admins = Bot.getData("AllBotAdminss") or []

            for admin in NUN_Admins:
                try:
                    bot.replyText(
                        admin,
                        notify_text,
                        parse_mode="HTML",
                        reply_markup={
                            "inline_keyboard": notify_keyboard
                        }
                    )
                except:
                    pass
    pass


# ===Balance===
def cmd_Balance():
    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]

    joinSTAT = "JOIND"

    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []

    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                except:
                    pass
            BINA = True

        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()

    if joinSTAT == "JOIND":
        Bot.runCommand("💴 Account1")
        raise ReturnCommand()


    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard = []
        row = []
        IND = 0

        # channels + social/uncheck links, in the order set in the admin panel
        SocialLinks = Bot.getData("AllSocialLinks") or []
        LinkOrder = Bot.getData("AllLinkOrder") or []
        JoinModeAll = (Bot.getData("JoinMode") == "ALL")

        JM_TXT = ""
        ch_btn = {}
        for _k in range(len(AMCL)):
            if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

        so_btn = {}
        for s in SocialLinks:
            _u = str(s.get("url", ""))
            if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

        ordered = []
        for _t in LinkOrder:
            if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                ordered.append(_t)
        for _t in ch_btn:
            if _t not in ordered:
                ordered.append(_t)
        for _t in so_btn:
            if _t not in ordered:
                ordered.append(_t)

        for _t in ordered:
            row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        PIRO=bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

        User.saveData("PIRO_ltmg",PIRO.message_id)

    pass


# ===Bonus===
def cmd_Bonus_2():
    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]

    joinSTAT = "JOIND"

    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []

    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                except:
                    pass
            BINA = True

        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()

    if joinSTAT == "JOIND":
        Bot.runCommand("🎁 Bonus1")
        raise ReturnCommand()


    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard = []
        row = []
        IND = 0

        # channels + social/uncheck links, in the order set in the admin panel
        SocialLinks = Bot.getData("AllSocialLinks") or []
        LinkOrder = Bot.getData("AllLinkOrder") or []
        JoinModeAll = (Bot.getData("JoinMode") == "ALL")

        JM_TXT = ""
        ch_btn = {}
        for _k in range(len(AMCL)):
            if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

        so_btn = {}
        for s in SocialLinks:
            _u = str(s.get("url", ""))
            if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

        ordered = []
        for _t in LinkOrder:
            if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                ordered.append(_t)
        for _t in ch_btn:
            if _t not in ordered:
                ordered.append(_t)
        for _t in so_btn:
            if _t not in ordered:
                ordered.append(_t)

        for _t in ordered:
            row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        PIRO=bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

        User.saveData("PIRO_ltmg",PIRO.message_id)

    pass


# ===Cancel===
def cmd_Cancel():
    Bot.runCommand(
        "/start"
    )
    pass


# ===Hii===
def cmd_Hii():
    response = HTTP.get("https://vsv-gateway-solutions.co.in/Api/api.php?token=ZNCZODLH&paytm=8153867035&amount=1&comment={comment}")
    bot.sendMessage("API Response:\n" + str(response.json()))
    pass


# ===Link Wallet===
def cmd_Link_Wallet():
    # coded by @Jenish_Dobariya1

    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]
    joinSTAT = "JOIND"

    # SYSTEM INTEGRITY CHECK
    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []
    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Reminder for Admin only.</b>", parse_mode="HTML")
                except:
                    pass
            BINA = True
        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()

    #FORCED CHANNEL JOIN LOGIC 
    if joinSTAT != "JOIND":
        if not AMCL:
            bot.sendMessage("📌 No channels found!")
        else:
            keyboard = []
            row = []
            IND = 0

            # channels + social/uncheck links, in the order set in the admin panel
            SocialLinks = Bot.getData("AllSocialLinks") or []
            LinkOrder = Bot.getData("AllLinkOrder") or []
            JoinModeAll = (Bot.getData("JoinMode") == "ALL")

            JM_TXT = ""
            ch_btn = {}
            for _k in range(len(AMCL)):
                if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                    ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

            so_btn = {}
            for s in SocialLinks:
                _u = str(s.get("url", ""))
                if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                    so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

            ordered = []
            for _t in LinkOrder:
                if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                    ordered.append(_t)
            for _t in ch_btn:
                if _t not in ordered:
                    ordered.append(_t)
            for _t in so_btn:
                if _t not in ordered:
                    ordered.append(_t)

            for _t in ordered:
                row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
                if len(row) == 2:
                    keyboard.append(row)
                    row = []

            if row:
                keyboard.append(row)

            keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])
            usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

            PIRO = bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !\n\n🛑 Must Join All Channels To Use Our Bot{JM_TXT}\n\n💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard}, parse_mode="HTML")
            User.saveData("PIRO_ltmg", PIRO.message_id)
            raise ReturnCommand()


    #  STRICT WITHDRAWAL OFF CHECK
    #  STRICT WITHDRAWAL OFF CHECK FOR LINK WALLET ONLY 
    W_Mode = Bot.getData("WalletWithdrawMode") or Bot.getData("WithdrawMode") or "ON"
    current_status = str(W_Mode).strip().upper()

    if current_status == "OFF" or current_status == "FALSE":
        # Link wallet ke liye fixed alert text jo aapne manga:
        WOT = Bot.getData("WOT") or "<b>⛔ Wallet Withdrawal off link your wallet after on</b>"
        bot.replyText(u, f"<b>{WOT}</b>", parse_mode="HTML")
        raise ReturnCommand()


    # MULTI-GWAY ACTIVE DETECTION SYSTEM
    # MULTI-GATEWAY ACTIVE DETECTION SYSTEM 
    active_gateways_list = []

    # Admin ke add kiye hue gateways (Name | API_URL) bhi show honge
    AddedGateways = Bot.getData("AddedGateways") or []
    added_names = []
    for g in AddedGateways:
        try:
            if g.get("on", True):
                _nm = str(g.get("name")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                _dom = str(g.get("domain") or "")
                _okd = ("." in _dom) and all([(_c.isalnum() or _c in ".-") for _c in _dom])
                if _okd:
                    added_names.append('<a href="https://' + _dom + '">https://' + _dom + '</a>'
    )
                else:
                    added_names.append(_nm)
        except:
            pass

    all_display = added_names + active_gateways_list

    if not all_display:
        gateway_display = "Admin Not Connected Any Gateway 🚨"
    else:
        gateway_display = "\n".join(all_display)


    #CHOOSE WALLET INPUT INTERFACE 
    keyboard = ReplyKeyboardMarkup(True)
    keyboard.row("Cancel")

    bot.replyText(
        u,
        text=f"""<b>👇🏻 Send Your Wallet Number To Link Wallet:</b>

💳 <b>Supported Gateways:</b>
<b>{gateway_display}</b>""",
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    Bot.handleNextCommand("/setwallet")
    pass


# ===Payout Method===
def cmd_Payout_Method():
    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]

    joinSTAT = "JOIND"
    Gatewaysett011 = Bot.getData("gatewaytype") or "Not Set"
    gatewaynow = Bot.getData("gatewaynow") or "RJ WALLET"



    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []

    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                except:
                    pass
            BINA = True

        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()

    if joinSTAT == "JOIND":
         pass 
    else:

        if not AMCL:
            bot.sendMessage("📌 No channels found!")
        else:
            keyboard = []
            row = []
            IND = 0

            # channels + social/uncheck links, in the order set in the admin panel
            SocialLinks = Bot.getData("AllSocialLinks") or []
            LinkOrder = Bot.getData("AllLinkOrder") or []
            JoinModeAll = (Bot.getData("JoinMode") == "ALL")

            JM_TXT = ""
            ch_btn = {}
            for _k in range(len(AMCL)):
                if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                    ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

            so_btn = {}
            for s in SocialLinks:
                _u = str(s.get("url", ""))
                if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                    so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

            ordered = []
            for _t in LinkOrder:
                if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                    ordered.append(_t)
            for _t in ch_btn:
                if _t not in ordered:
                    ordered.append(_t)
            for _t in so_btn:
                if _t not in ordered:
                    ordered.append(_t)

            for _t in ordered:
                row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
                if len(row) == 2:
                    keyboard.append(row)
                    row = []

            if row:
                keyboard.append(row)

            keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

            usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

            PIRO=bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

            User.saveData("PIRO_ltmg",PIRO.message_id)

            raise ReturnCommand() 


    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    # coded by @Jenish_Dobariya1

    # ----------- GET USER DATA -----------
    wallet = Bot.getData("UserWallet" + str(u)) or "Not Linked"

    WalletStatus = Bot.getData("WalletWithdraw") or "ON"

    wallet = Bot.getData("UserWallet" + str(u)) or "Not Linked"

    if WalletStatus == "OFF":
        bot.replyText(
            u,
            "<b>⛔ All Withdrawal Methods Are Currently Disabled</b>"
        )
        raise ReturnCommand()

    text = "<b>💳 Choose Desired Payment Method From Below 👇\n\n"

    buttons = []

    if WalletStatus == "ON":
        text += f"📱 Your Current Wallet - <code>{wallet}</code>\n"
        wallet_btn = {
            "text": "📱 Link Wallet",
            "callback_data": "Link Wallet"
        }

    text += "</b>"

    # Button Layout Fixed Here
    if WalletStatus == "ON":
        buttons.append([wallet_btn])

    keyboard = {"inline_keyboard": buttons}

    bot.replyText(
        u,
        text,
        parse_mode="html",
        reply_markup=keyboard
    )
    pass


# ===Refer Earn===
def cmd_Refer_Earn():
    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]

    joinSTAT = "JOIND"

    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []

    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                except:
                    pass
            BINA = True

        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()

    if joinSTAT == "JOIND":
        Bot.runCommand("👫Referral1")
        raise ReturnCommand()


    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard = []
        row = []
        IND = 0

        # channels + social/uncheck links, in the order set in the admin panel
        SocialLinks = Bot.getData("AllSocialLinks") or []
        LinkOrder = Bot.getData("AllLinkOrder") or []
        JoinModeAll = (Bot.getData("JoinMode") == "ALL")

        JM_TXT = ""
        ch_btn = {}
        for _k in range(len(AMCL)):
            if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

        so_btn = {}
        for s in SocialLinks:
            _u = str(s.get("url", ""))
            if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

        ordered = []
        for _t in LinkOrder:
            if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                ordered.append(_t)
        for _t in ch_btn:
            if _t not in ordered:
                ordered.append(_t)
        for _t in so_btn:
            if _t not in ordered:
                ordered.append(_t)

        for _t in ordered:
            row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        PIRO=bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

        User.saveData("PIRO_ltmg",PIRO.message_id)

    pass


# ===Withdraw===
def cmd_Withdraw():
    # coded by @Jenish_Dobariya1

    if message.chat.type != "private":
        raise ReturnCommand()

    BotMode = Bot.getData("BotMode")
    if BotMode == "OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    BotAdmins = Bot.getData("AllBotAdminss") or [str(u)]
    joinSTAT = "JOIND"

    AMC = Bot.getData("AllMainCh") or []
    AMCU = Bot.getData("AllMainChUsernm") or []
    AMCL = Bot.getData("AllMainChlink") or []
    AMCTGID = Bot.getData("AllMainChTGID") or []

    User.deleteData("PIRO_NJCL")
    PIRO_NJCL = []

    BINA = False
    IND = 0

    for i in AMCTGID:
        try:
            UC = bot.getChatMember(i, u)
            UC = UC.status
        except:
            UC = "error"

        ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or "N"

        if UC == "left" and ReqRec == "N":
            joinSTAT = "NOTJOIN"
            PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None)

        DC = Bot.getData(str(i) + "isDef") or "Y"
        JR = Bot.getData(str(i) + "JoinReq") or "Y"

        if UC == "error" and JR == "Y":
            if DC == "Y":
                continue
            for admin in BotAdmins:
                try:
                    bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️</b>", parse_mode="HTML")
                except:
                    pass
            BINA = True

        IND += 1

    User.saveData("PIRO_NJCL", PIRO_NJCL)

    if BINA:
        raise ReturnCommand()


    # ==================== ✅ IF JOINED BLOCK ====================
    if joinSTAT == "JOIND":

        # Dono variables ko fetch karein
        W_Mode = Bot.getData("WalletWithdrawMode") or Bot.getData("WithdrawMode") or "ON"
        W_With = Bot.getData("WalletWithdraw") or "ON"

        # Ultra-Strict Stripping & Formatter Engine
        check_mode = str(W_Mode).strip().upper()
        check_with = str(W_With).strip().upper()

        # Absolute Block Rule: Agar withdrawal status OFF hai toh dynamic custom text dikhayein
        WitdMode = Bot.getData("WithdrawMode") or "ON"
        if WitdMode == "OFF":
          WOT = Bot.getData("WOT") or "⛔ Withdrawal Is Off"
          bot.replyText(u, f"<b>{WOT}</b>", parse_mode="HTML")
          raise ReturnCommand()

        # User Live Profile Mapping Data
        wallet_data = Bot.getData("UserWallet" + str(u)) or "Not Linked"

        # Live Tracker Matrix Sync Engine
        if wallet_data != "Not Linked":
            HasSynced = User.getData("WalletStatSynced") or "N"
            if HasSynced == "N":
                T_usLnkWallet = Bot.getData('T_usLnkWallet') or 0
                Bot.saveData('T_usLnkWallet', int(T_usLnkWallet) + 1)

                inviter = User.getData("my_inviter_is", user=u)
                if inviter:
                    RefWalletLnkC = Bot.getData(str(inviter) + "RefWalletLnkC") or 0
                    Bot.saveData(str(inviter) + "RefWalletLnkC", int(RefWalletLnkC) + 1)
                User.saveData("WalletStatSynced", "Y")

        # Layout Generation Rendering Sequence
        Bot.runCommand("/PIRO_withdraw")
        raise ReturnCommand()


    # ==================== ❌ NOT JOINED BLOCK ====================
    if not AMCL:
        bot.sendMessage("📌 No channels found!")
    else:
        keyboard = []
        row = []
        IND = 0

        # channels + social/uncheck links, in the order set in the admin panel
        SocialLinks = Bot.getData("AllSocialLinks") or []
        LinkOrder = Bot.getData("AllLinkOrder") or []
        JoinModeAll = (Bot.getData("JoinMode") == "ALL")

        JM_TXT = ""
        ch_btn = {}
        for _k in range(len(AMCL)):
            if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or JoinModeAll) and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
                ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

        so_btn = {}
        for s in SocialLinks:
            _u = str(s.get("url", ""))
            if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
                so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

        ordered = []
        for _t in LinkOrder:
            if (_t in ch_btn or _t in so_btn) and _t not in ordered:
                ordered.append(_t)
        for _t in ch_btn:
            if _t not in ordered:
                ordered.append(_t)
        for _t in so_btn:
            if _t not in ordered:
                ordered.append(_t)

        for _t in ordered:
            row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

        usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

        PIRO = bot.sendMessage(
            f"""<b>👋 Hey {usr}\n\n🛑 Must Join All Channels To Use Bot{JM_TXT}\n\n💣 After Joining Click Claim</b>""",
            reply_markup={"inline_keyboard": keyboard},
            parse_mode="HTML"
        )

        User.saveData("PIRO_ltmg", PIRO.message_id)

    pass


# ===🎁 Bonus1===
def cmd___Bonus1():
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]
    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()


    markup = InlineKeyboardMarkup()

    markup.add(InlineKeyboardButton(text="🕕 Daily Bonus", callback_data="/Bonus"))

    markup.add(InlineKeyboardButton(
        text="🎁 Gift Code", callback_data="/RedeemBotRC"))

    bot.replyText(u, text="<b>✨ Choose One:</b>", reply_markup=markup)
    pass


# ===👫Referral1===
def cmd__Referral1():
    global msg
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()



    PER_REF=Bot.getData("PerRefer") or 0
    per_refer = PER_REF

    get_bot = Bot.info().username 
    RL=f"https://t.me/{get_bot}?start={u}"


    msg = f"""<b>💰 Per Refer Rs.{per_refer} Upi Cash
  
👤Your Refferal Link: {RL}
    
Share With Your Friend's & Family And Earn Refer Bonus Easily ✨🤑</b>"""

    markup = InlineKeyboardMarkup()

    markup.add(InlineKeyboardButton(text='✨My Invites',callback_data='/MyInvitez'),InlineKeyboardButton(text='🏆Leaderboard',callback_data='/TopReferral'))

    bot.replyText(u, msg, parse_mode="html", reply_markup=markup, disable_web_page_preview=True)
    pass


# ===💴 Account1===
def cmd___Account1():
    global msg
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()

    balance = libs.Resources.anotherRes('Balance',user=u).value()

    def Convert_To_TWO_DP(x):
        f = "{:.2f}".format(x)
        return f

    msg = f"""<b>💰 Balance: ₹{Convert_To_TWO_DP(balance)}
  
Use 'Withdraw' button to withdraw your balance to Wallet</b>"""

    bot.replyText(u,msg,parse_mode="html")
    pass


# ===📊 Statistics1===
def cmd___Statistics1():
    def _safe_edit(*a, **kw):
        # ignore Telegram's "message is not modified" (same button tapped twice)
        try:
            return bot.editMessageText(*a, **kw)
        except Exception as _e:
            if "not modified" in str(_e).lower():
                return None
            raise
    # 19'4'25   00'11'10  v:1'0'0 [√]
    # CODED BY: @Owner_Here 
    # [ DM TO BUY ANY BOTS & CODES ]

    BotMode=Bot.getData("BotMode")
    if BotMode=="OFF":
        bot.replyText(u, "<b>" + str(Bot.getData("BOT_OFF_TEXT") or "🙇‍♂️ Bot Is Currently Off") + "</b>", parse_mode="HTML")
        raise ReturnCommand()
    OWNER_ID = Bot.getData("Owner")

    owner = bot.getChatMember(OWNER_ID, OWNER_ID)
    owner_name = owner.user.first_name

    Tusrs=Bot.getData('total_users') or 0

    wit=libs.Resources.globalRes('BotWithdraws').value()

    FulUsrs=Bot.getData("FulBotUsrs") or []
    T_verifUsrs=Bot.getData('T_verifUsrs') or 0

    # Verified = only successful verifications (same-device users excluded)
    _sameDev = 0
    for _uid in FulUsrs:
        if (Bot.getData(str(_uid) + "sameDev") or "no") == "yes":
            _sameDev += 1
    T_verifUsrs = max(0, int(T_verifUsrs) - _sameDev)


    markup = InlineKeyboardMarkup()

    markup.row(
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data="/RawStats"
        )
    )


    _safe_edit(
        chat_id=u,
        message_id=call.message.message_id,
        text=f"""<b>👍Total Members In Bot: {len(FulUsrs)} Users

👍Total Verified Users In Bot : {T_verifUsrs} Users

👍Total Payouts In Bot : {wit} INR

📍 Powered By - <a href="tg://user?id={OWNER_ID}">{owner_name}</a>

</b>""",
        parse_mode="html",
        reply_markup=markup
    )
    pass


# ===📱 Link Wallet===
def cmd___Link_Wallet():
    Bot.runCommand("Link Wallet")
    pass


# ===🟢 Joined===
def cmd___Joined():
    global msg
    # 🟢 Joined
    # --- JOINING CHECK ---

    # delete the join post(s) as soon as Claim is clicked (also when everything is joined)
    PIRO_ltmg = User.getData("PIRO_ltmg")
    try:
        bot.deleteMessage(message.chat.id, PIRO_ltmg)
    except:
        pass

    # the post that was clicked (Claim button)
    _is_cb = False
    try:
        _cid = call.id
        _is_cb = True
    except:
        pass

    if _is_cb:
        try:
            bot.deleteMessage(message.chat.id, message.message_id)
        except:
            pass

    # the "you didn't join" message(s) under it - removed only when Claim is tapped
    for _njm in (User.getData("PIRO_njmsgs") or []):
        try:
            bot.deleteMessage(message.chat.id, _njm)
        except:
            pass
    User.saveData("PIRO_njmsgs", [])

    usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

    User.deleteData("PIRO_NJCL")

    BID = Bot.info().token.split(":")[0]
    BotAdmins = Bot.getData("AllBotAdminss") or []

    AMC = Bot.getData("AllMainCh") or []          # Channel Names
    AMCU = Bot.getData("AllMainChUsernm") or []   # Channel Usernames
    AMCL = Bot.getData("AllMainChlink") or []     # Channel Links
    AMCTGID = Bot.getData("AllMainChTGID") or []  # Channel IDs
    AMCO = Bot.getData("AllMainChOptional") or [] # Optional (N=must join, O=optional)

    SocialLinks = Bot.getData("AllSocialLinks") or []  # Social Media

    joinSTAT = "JOIND"
    NotJoindLst = ""
    PIRO_NJCL = []
    BINA = False
    IND = 0

    # Check each channel
    if AMCTGID:
        for i in AMCTGID:
            try:
                UC = bot.getChatMember(i, u)
                UC = UC.status
            except:
                UC = "error"

            ReqRec = Bot.getData(str(i) + "JoinRRq" + str(u)) or ""
            JR = Bot.getData(str(i) + "JoinReq") or "Y"

            # If not joined and channel is mandatory
            if UC == "left" and ReqRec != "Y" and JR == "Y" and (not AMCO or AMCO[IND] != "O"):
                Chlnk = f"""<a href="{AMCL[IND]}">{AMC[IND]}</a>"""
                NotJoindLst += f"""{Chlnk}\n"""
                PIRO_NJCL.append(str(i)); (Bot.saveData("shnj_" + str(i) + "_" + str(u), "1") if not Bot.getData("shnj_" + str(i) + "_" + str(u)) else None); libs.Resources.globalRes("ChClaim" + str(i).replace("-", "m")).add(1)
                joinSTAT = "NOTJOIN"

            # If bot not admin
            # same rule as /start: ignore check-errors on default channels / join-request channels
            if UC == "error" and JR == "Y" and (Bot.getData(str(i) + "isDef") or "Y") != "Y":
                for admin in BotAdmins:
                    try:
                        bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Don't worry, this message is only for admins as a reminder (not for users). 😊</b>")
                    except:
                        pass
                BINA = True

            IND += 1

    if BINA:
        raise ReturnCommand()

    # If joined all, run contact verification before main menu
    if joinSTAT == "JOIND":
        VerifySystemStatus = Bot.getData("VerifySystemStatus") or "OFF"
        if VerifySystemStatus == "ON":
            if not User.getData("is_verified"):
                Bot.runCommand("/shareprofile")
                raise ReturnCommand()
        Bot.runCommand("/PIRO_MainMenu")
        raise ReturnCommand()

    # Save data for reuse
    User.saveData("PIRO_NJCL", PIRO_NJCL)
    User.saveData("NJLST", NotJoindLst)

    # Prepare keyboard with only unjoined channels
    keyboard = []
    row = []
    IND = 0
    # channels + social/uncheck links, shown in the order set in the admin panel
    SocialLinks = Bot.getData("AllSocialLinks") or []
    LinkOrder = Bot.getData("AllLinkOrder") or []

    JM_TXT = ""
    ch_btn = {}
    for _k in range(len(AMCL)):
        if _k < len(AMCTGID) and (str(AMCTGID[_k]) in PIRO_NJCL or Bot.getData("JoinMode") == "ALL") and str(AMCL[_k]).startswith(("https://", "http://", "tg://")) and len(str(AMCL[_k])) > 8:
            ch_btn["c:" + str(AMCTGID[_k])] = {"text": "Join", "url": AMCL[_k], "style": "primary"}

    so_btn = {}
    for s in SocialLinks:
        _u = str(s.get("url", ""))
        if _u.startswith(("https://", "http://", "tg://")) and len(_u) > 8:
            so_btn["s:" + _u] = {"text": str(s.get("name")), "url": _u, "style": "primary"}

    ordered = []
    for _t in LinkOrder:
        if (_t in ch_btn or _t in so_btn) and _t not in ordered:
            ordered.append(_t)
    for _t in ch_btn:
        if _t not in ordered:
            ordered.append(_t)
    for _t in so_btn:
        if _t not in ordered:
            ordered.append(_t)

    row = []
    for _t in ordered:
        row.append(ch_btn[_t] if _t in ch_btn else so_btn[_t])
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)


    # Claim button
    keyboard.append([{"text": "🔒 Claim", "callback_data": "🟢 Joined","style":"success"}])

    # Send main join message
    msg = bot.sendMessage(
    f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join All Channels To Use Our Bot{JM_TXT}

💣 After Joining Click Claim</b>""",
        reply_markup={"inline_keyboard": keyboard},
        disable_web_page_preview=True
    )
    User.saveData("PIRO_ltmg", msg["message_id"]); Bot.runCommand("/NotJoinedMsg", options=",".join([str(_x) for _x in PIRO_NJCL]))

    # (the "not joined channels" list message is removed on purpose)
    pass


# ==========================================================================
# COMMAND REGISTRY  (command name -> function)
# ==========================================================================
COMMANDS.update({
    '/AddBalTxt': cmd_AddBalTxt,
    '/AddBalTxt1': cmd_AddBalTxt1,
    '/AdminStats': cmd_AdminStats,
    '/AutoCheckBot': cmd_AutoCheckBot,
    '/BC2_Hook': cmd_BC2_Hook,
    '/BC2_act': cmd_BC2_act,
    '/BC2_btn': cmd_BC2_btn,
    '/BC2_msg': cmd_BC2_msg,
    '/BC2_prog': cmd_BC2_prog,
    '/BROADCAST': cmd_BROADCAST,
    '/BanUnban': cmd_BanUnban,
    '/Bonus': cmd_Bonus,
    '/BotOffText': cmd_BotOffText,
    '/BotOffText1': cmd_BotOffText1,
    '/CHANNEL_BC': cmd_CHANNEL_BC,
    '/ChMemberTrack': cmd_ChMemberTrack,
    '/ChRules': cmd_ChRules,
    '/ChStats': cmd_ChStats,
    '/ChangeAnyUserBal': cmd_ChangeAnyUserBal,
    '/ChangeAnyUserBal2': cmd_ChangeAnyUserBal2,
    '/CreateBotRC': cmd_CreateBotRC,
    '/JoinPiro': cmd_JoinPiro,
    '/JoinPiro1': cmd_JoinPiro1,
    '/JoinReqApprove': cmd_JoinReqApprove,
    '/JoinReqExpire': cmd_JoinReqExpire,
    '/JoinReqSt': cmd_JoinReqSt,
    '/LimEdit': cmd_LimEdit,
    '/LimEdit1': cmd_LimEdit1,
    '/ManageText': cmd_ManageText,
    '/MyInvitez': cmd_MyInvitez,
    '/NotJoinedMsg': cmd_NotJoinedMsg,
    '/PIRO_AddAdmin': cmd_PIRO_AddAdmin,
    '/PIRO_AddAdmin1': cmd_PIRO_AddAdmin1,
    '/PIRO_AddCh': cmd_PIRO_AddCh,
    '/PIRO_AddCh1': cmd_PIRO_AddCh1,
    '/PIRO_AdminAction': cmd_PIRO_AdminAction,
    '/PIRO_Admins': cmd_PIRO_Admins,
    '/PIRO_BC_Cancel': cmd_PIRO_BC_Cancel,
    '/PIRO_BC_Confirm': cmd_PIRO_BC_Confirm,
    '/PIRO_BC_Process': cmd_PIRO_BC_Process,
    '/PIRO_BC_Schedule_Confirm': cmd_PIRO_BC_Schedule_Confirm,
    '/PIRO_BC_Schedule_Date': cmd_PIRO_BC_Schedule_Date,
    '/PIRO_BC_Schedule_Execute': cmd_PIRO_BC_Schedule_Execute,
    '/PIRO_BC_Schedule_Message': cmd_PIRO_BC_Schedule_Message,
    '/PIRO_BC_Schedule_Time': cmd_PIRO_BC_Schedule_Time,
    '/PIRO_Broadcast': cmd_PIRO_Broadcast,
    '/PIRO_ChLinks': cmd_PIRO_ChLinks,
    '/PIRO_Chanel_Pannel': cmd_PIRO_Chanel_Pannel,
    '/PIRO_Chanel_Pannel1': cmd_PIRO_Chanel_Pannel1,
    '/PIRO_LeadSet': cmd_PIRO_LeadSet,
    '/PIRO_LeadSet1': cmd_PIRO_LeadSet1,
    '/PIRO_MainMenu': cmd_PIRO_MainMenu,
    '/PIRO_RC_Pannel': cmd_PIRO_RC_Pannel,
    '/PIRO_ScheduleBroadcast': cmd_PIRO_ScheduleBroadcast,
    '/PIRO_Verification': cmd_PIRO_Verification,
    '/PIRO_Verification0': cmd_PIRO_Verification0,
    '/PIRO_ban': cmd_PIRO_ban,
    '/PIRO_ban1': cmd_PIRO_ban1,
    '/PIRO_broadcast1': cmd_PIRO_broadcast1,
    '/PIRO_broadcast2': cmd_PIRO_broadcast2,
    '/PIRO_broadcast3': cmd_PIRO_broadcast3,
    '/PIRO_unban': cmd_PIRO_unban,
    '/PIRO_unban1': cmd_PIRO_unban1,
    '/PIRO_withdraw': cmd_PIRO_withdraw,
    '/PIRO_withdraw1': cmd_PIRO_withdraw1,
    '/PIRO_withdrawCG': cmd_PIRO_withdrawCG,
    '/Perfom_Stats': cmd_Perfom_Stats,
    '/RC_Change': cmd_RC_Change,
    '/RC_ClaimedUsers': cmd_RC_ClaimedUsers,
    '/RC_Edit': cmd_RC_Edit,
    '/RC_Edit1': cmd_RC_Edit1,
    '/RC_SaveMinRef': cmd_RC_SaveMinRef,
    '/RC_SaveRename': cmd_RC_SaveRename,
    '/RC_claimedText': cmd_RC_claimedText,
    '/RC_claimedText1': cmd_RC_claimedText1,
    '/RawStats': cmd_RawStats,
    '/RedeemBotRC': cmd_RedeemBotRC,
    '/RedeemBotRC1': cmd_RedeemBotRC1,
    '/RedeemBotRC2': cmd_RedeemBotRC2,
    '/ResetAllBalance': cmd_ResetAllBalance,
    '/ResetAllBalanceConfirm': cmd_ResetAllBalanceConfirm,
    '/SCHEDULE_BROADCAST': cmd_SCHEDULE_BROADCAST,
    '/SCHEDULE_EXECUTE': cmd_SCHEDULE_EXECUTE,
    '/SCHEDULE_SET_MESSAGE': cmd_SCHEDULE_SET_MESSAGE,
    '/SCHEDULE_SET_TIME': cmd_SCHEDULE_SET_TIME,
    '/SetBonusAmont': cmd_SetBonusAmont,
    '/SetBonusAmont1': cmd_SetBonusAmont1,
    '/SetBotPayChann': cmd_SetBotPayChann,
    '/SetBotPayChann1': cmd_SetBotPayChann1,
    '/SetBotPayComm0': cmd_SetBotPayComm0,
    '/SetBotPayComm01': cmd_SetBotPayComm01,
    '/SetMinMax': cmd_SetMinMax,
    '/SetPerReferAmont': cmd_SetPerReferAmont,
    '/SetPerReferAmont1': cmd_SetPerReferAmont1,
    '/SetTaxAmont': cmd_SetTaxAmont,
    '/TalkUser': cmd_TalkUser,
    '/TalkUser2': cmd_TalkUser2,
    '/Top30Referral': cmd_Top30Referral,
    '/TopBalance': cmd_TopBalance,
    '/TopReferral': cmd_TopReferral,
    '/TopWithdraw_Wallet': cmd_TopWithdraw_Wallet,
    '/TopWithdraws': cmd_TopWithdraws,
    '/TransferOwnership': cmd_TransferOwnership,
    '/TransferOwnership_Save': cmd_TransferOwnership_Save,
    '/UserKiSabDetails': cmd_UserKiSabDetails,
    '/UserKiSabDetails1': cmd_UserKiSabDetails1,
    '/UserReferrals': cmd_UserReferrals,
    '/WalletOFF_Text': cmd_WalletOFF_Text,
    '/WalletOFF_Text1': cmd_WalletOFF_Text1,
    '/WdPostRetry': cmd_WdPostRetry,
    '/WithdrwOFF_Text': cmd_WithdrwOFF_Text,
    '/WithdrwOFF_Text1': cmd_WithdrwOFF_Text1,
    '/addgetwayonbbot': cmd_addgetwayonbbot,
    '/addgetwayonbbot1': cmd_addgetwayonbbot1,
    '/admin': cmd_admin,
    '/broadcast': cmd_broadcast,
    '/broadcastResult': cmd_broadcastResult,
    '/cleardb': cmd_cleardb,
    '/cooltimw': cmd_cooltimw,
    '/creating_NwRC': cmd_creating_NwRC,
    '/creating_NwRC1': cmd_creating_NwRC1,
    '/creating_NwRC_Step2': cmd_creating_NwRC_Step2,
    '/delete': cmd_delete,
    '/gwAdd': cmd_gwAdd,
    '/gwEdit': cmd_gwEdit,
    '/gwEdit1': cmd_gwEdit1,
    '/gwLimit': cmd_gwLimit,
    '/gwLimit1': cmd_gwLimit1,
    '/gwManage': cmd_gwManage,
    '/gwRef': cmd_gwRef,
    '/gwRef1': cmd_gwRef1,
    '/gwSameWallet': cmd_gwSameWallet,
    '/gwTax': cmd_gwTax,
    '/gwTax1': cmd_gwTax1,
    '/gwView': cmd_gwView,
    '/handler_special_updates': cmd_handler_special_updates,
    '/info': cmd_info,
    '/manualVerifKro': cmd_manualVerifKro,
    '/manualVerifKro1': cmd_manualVerifKro1,
    '/onWebhook': cmd_onWebhook,
    '/permission': cmd_permission,
    '/savesocial': cmd_savesocial,
    '/setcool': cmd_setcool,
    '/setwallet': cmd_setwallet,
    '/shareprofile': cmd_shareprofile,
    '/social': cmd_social,
    '/start': cmd_start,
    '/uncheck': cmd_uncheck,
    '/verifiedStats': cmd_verifiedStats,
    '@': cmd__,
    'Balance': cmd_Balance,
    'Bonus': cmd_Bonus_2,
    'Cancel': cmd_Cancel,
    'Hii': cmd_Hii,
    'Link Wallet': cmd_Link_Wallet,
    'Payout Method': cmd_Payout_Method,
    'Refer Earn': cmd_Refer_Earn,
    'Withdraw': cmd_Withdraw,
    '🎁 Bonus1': cmd___Bonus1,
    '👫Referral1': cmd__Referral1,
    '💴 Account1': cmd___Account1,
    '📊 Statistics1': cmd___Statistics1,
    '📱 Link Wallet': cmd___Link_Wallet,
    '🟢 Joined': cmd___Joined,
})


# ==========================================================================
# 3. DISPATCHER   (what the TBC platform did for you)
#    order:  @ (gate)  ->  command / handleNextCommand / *   ->  !  (on error)
# ==========================================================================
def _track_user(m):
    try:
        if m.chat.type == "private":
            uid = str(m.from_user.id)
            with db.lock:
                if uid not in db.d["users"]:
                    db.d["users"].append(uid)
                    db.touch()
    except Exception:
        pass


def _on_error(err_text, ctx):
    print("COMMAND ERROR:\n" + err_text, flush=True)
    if _find("!"):
        try:
            _invoke("!", **dict(ctx, options=err_text))
            return
        except Exception:
            traceback.print_exc()
    print(err_text)


def _handle(ctx, target, params_):
    """Gate '@' -> target command (or '*')  with error handling."""
    try:
        if _find("@") and _invoke("@", **ctx) == _RETURNED:
            return
        c = dict(ctx, params=params_)
        if _invoke(target, **c) == _NOT_FOUND:
            if _find("*"):
                _invoke("*", **c)
    except Exception:
        _on_error(traceback.format_exc(), ctx)


def on_message(m):
    uid = str(m.from_user.id)
    _track_user(m)
    _LAST_MSG[uid] = m
    try:
        m.update_type = "message"
    except Exception:
        pass
    text = m.text
    ctx = dict(u=uid, message=m, call=None, options=None, params=None, msg=text, update_type="message")
    is_cmd = bool(text) and text.startswith("/")
    pend = db.d["pending"].get(uid)
    if pend and is_cmd and pend.get("cancel"):
        with db.lock:
            db.d["pending"].pop(uid, None)
            db.touch()
        pend = None
    if pend:
        with db.lock:
            db.d["pending"].pop(uid, None)
            db.touch()
        ctx["options"] = pend.get("options")
        _handle(ctx, pend["cmd"], None)
        return
    if is_cmd:
        name, prm = _split_cmd(text)
        _handle(ctx, name, prm)
    elif text:
        _handle(ctx, text.strip(), None)       # reply-keyboard buttons = command names
    else:
        _handle(ctx, "*", None)                # media etc.


def on_callback(c):
    uid = str(c.from_user.id)
    m = c.message
    try:
        m.from_user = c.from_user
        m.update_type = "callback_query"
    except Exception:
        pass
    _LAST_MSG[uid] = m
    data = c.data or ""
    ctx = dict(u=uid, message=m, call=c, options=None, params=None, msg=data, update_type="callback_query")
    name, prm = _resolve(data)
    _handle(ctx, name, prm)


def _special(kind):
    def _h(obj):
        try:
            uid = str(obj.from_user.id)
            try:
                obj.update_type = kind
            except Exception:
                pass
            ctx = dict(u=uid, message=obj, call=None, options=None, params=None, msg=None, update_type=kind)
            if _invoke("/handler_special_updates", **ctx) == _NOT_FOUND and _find("*"):
                _invoke("*", **ctx)
        except Exception:
            _on_error(traceback.format_exc(), {})
    return _h


# ------------------------------------------------------- webhook server
def _start_webhook_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _go(self):
            try:
                p = urllib.parse.urlparse(self.path)
                parts = p.path.strip("/").split("/")
                if p.path in ("/", "/health", "/healthz"):          # Render health check
                    self.send_response(200); self.end_headers(); self.wfile.write(b"Bot is running"); return
                if len(parts) != 3 or parts[0] != "webhook":
                    self.send_response(404); self.end_headers(); return
                command = urllib.parse.unquote(parts[1])
                uid = urllib.parse.unquote(parts[2])
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "ignore") if n else ""
                try:
                    js = json.loads(raw) if raw else None
                except Exception:
                    js = None
                opt = Bunch(json=bunchify(js), text=raw, method=self.command,
                            query=Bunch({k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}),
                            headers=Bunch(dict(self.headers)))
                _run_for_user(uid, command, options=opt)
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            except Exception:
                traceback.print_exc()
                self.send_response(500); self.end_headers()

        do_GET = do_POST = do_PUT = _go

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print("webhook server on port", PORT)
    except Exception as e:
        print("webhook server not started:", e)


def _keep_alive():
    """Ping our own public url so a free Render web service does not go to sleep."""
    if not (KEEP_ALIVE and WEBHOOK_BASE_URL.startswith("http")):
        return

    def _loop():
        while True:
            time.sleep(600)
            try:
                requests.get(WEBHOOK_BASE_URL + "/health", timeout=20)
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


def main():
    if "PUT_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        sys.exit("Set BOT_TOKEN first:  BOT_TOKEN=123:ABC python %s" % os.path.basename(__file__))
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    telebot.logger.setLevel(logging.INFO)
    # TBC had set a webhook on this token -> getUpdates gives "409 Conflict" until it is removed.
    try:
        _api.delete_webhook(drop_pending_updates=False)
        me = _api.get_me()
        print("Logged in as @%s (id %s)" % (me.username, me.id))
    except Exception as e:
        sys.exit("BOT_TOKEN invalid / Telegram not reachable: %s" % e)
    _api.message_handler(func=lambda m: True,
                         content_types=["text", "photo", "video", "audio", "document", "animation",
                                        "sticker", "voice", "video_note", "contact", "location",
                                        "poll", "dice", "venue"])(on_message)
    _api.callback_query_handler(func=lambda c: True)(on_callback)
    _api.chat_member_handler(func=lambda c: True)(_special("chat_member"))
    _api.my_chat_member_handler(func=lambda c: True)(_special("my_chat_member"))
    _api.chat_join_request_handler(func=lambda c: True)(_special("chat_join_request"))
    _start_webhook_server()
    _keep_alive()
    print("%d commands loaded - bot started" % len(COMMANDS))
    _api.infinity_polling(allowed_updates=["message", "callback_query", "chat_member",
                                           "my_chat_member", "chat_join_request"],
                          skip_pending=False)


if __name__ == "__main__":
    main()
