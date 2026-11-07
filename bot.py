"""
Telegram-бот: калькулятор межгорода (aiogram 3).
Запуск:
    pip install aiogram aiohttp
    export BOT_TOKEN=...   (токен от @BotFather)
    python bot.py
"""
import asyncio
import json
import math
import os
import time
from copy import deepcopy

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

CUSTOM = "Свой тариф"  # тариф, цену которого вписывает сам пользователь (по умолчанию выключен)
TARIFFS = ["Стандарт", "Комфорт", "Комфорт+", "Минивэн", CUSTOM]

# Значения по умолчанию (₽/км)
DEFAULTS = {
    "normal": {"Стандарт": 27, "Комфорт": 30, "Комфорт+": 35, "Минивэн": 50, CUSTOM: 40},
    "high": {"Стандарт": 70, "Комфорт": 80, "Комфорт+": 90, "Минивэн": 110, CUSTOM: 100},
    "urgent_on": False,
    "urgent_pct": 20,
    "mode": "normal",  # normal | double
    "order": "normal_high",  # порядок тарифов в двойной тарификации (см. ORDERS)
    "fee": 0,  # сумма подачи, ₽ (0 — не показывать и не учитывать)
    "fee_on": True,  # подачу можно отключить, не стирая сумму
    "enabled": {t: (t != CUSTOM) for t in TARIFFS},  # какие тарифы показывать в расчёте
}

# Порядок тарифов в режиме двойной тарификации: тип тарифа для 1-го и 2-го отрезка
ORDERS = ["normal_high", "high_normal", "normal_normal"]
ORDER_KINDS = {"normal_high": ("normal", "high"),
               "high_normal": ("high", "normal"),
               "normal_normal": ("normal", "normal")}
ORDER_TITLE = {"normal_high": "обычный → повышенный",
               "high_normal": "повышенный → обычный",
               "normal_normal": "обычный → обычный"}
KIND_NAME = {"normal": "обычный", "high": "повышенный"}


def leg_kinds(s):
    return ORDER_KINDS.get(s.get("order", "normal_high"), ORDER_KINDS["normal_high"])


def uses_high(s):
    return s["mode"] == "double" and "high" in leg_kinds(s)

# True  — платные дороги прибавляются до наценки за срочность (20% берётся и с них)
# False — срочность считается только с поездки, платные дороги прибавляются после
TOLL_BEFORE_URGENT = False
# Сумма подачи: False — прибавляется после наценки за срочность (как платные дороги),
# True — срочность считается и с неё
FEE_BEFORE_URGENT = False

USER_SETTINGS: dict[int, dict] = {}

# Файл настроек. На Bothost данные в /app/data переживают обновления бота.
DATA_DIR = os.getenv("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print("Не удалось прочитать настройки:", e)
        return
    for uid, st in raw.items():
        base = deepcopy(DEFAULTS)
        for k in ("normal", "high"):
            base[k].update(st.get(k, {}))
        base["enabled"].update(st.get("enabled", {}))
        for k in ("urgent_on", "urgent_pct", "mode", "fee", "fee_on"):
            if k in st:
                base[k] = st[k]
        if st.get("order") in ORDERS:
            base["order"] = st["order"]
        elif st.get("reverse"):  # старый формат настроек
            base["order"] = "high_normal"
        USER_SETTINGS[int(uid)] = base


def save_settings():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in USER_SETTINGS.items()}, f, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print("Не удалось сохранить настройки:", e)


# ---------- платный доступ: пробный период + разовый донат ----------
# Всё настраивается переменными окружения на хостинге:
#   PAID_MODE=1       — включить платный режим (по умолчанию выключен, бот бесплатный)
#   ADMIN_ID=123456   — ваш Telegram ID (узнать: команда /id), сюда приходят заявки об оплате
#   DONATE_INFO=...   — текст с реквизитами и суммой доната, его увидят пользователи
#   TRIAL_DAYS=3      — длительность пробного периода
# Если на хостинге нет переменных окружения, те же параметры можно положить в файл
# config.json в папке data: {"admin_id": 123456, "paid_mode": 1, "donate_info": "...", "trial_days": 3}
def _read_config():
    try:
        with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = _read_config()
PAID_MODE = str(os.getenv("PAID_MODE") or CFG.get("paid_mode") or "0").strip().lower() in (
    "1", "true", "yes", "on")
ADMIN_ID = int(os.getenv("ADMIN_ID") or CFG.get("admin_id") or 0)
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS") or CFG.get("trial_days") or 3)
DONATE_INFO = str(os.getenv("DONATE_INFO") or CFG.get("donate_info") or "").strip()
SEEN_FILE = os.path.join(DATA_DIR, "seen_users.json")
SEEN: dict[int, dict] = {}  # все, кто хоть раз открыл бота (для уведомлений о новых)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
USERS: dict[int, dict] = {}
CLAIM_COOLDOWN = 600  # секунд между заявками «Я оплатил» от одного человека


def load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print("Не удалось прочитать пользователей:", e)
        return
    for uid, u in raw.items():
        USERS[int(uid)] = u


def save_users():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in USERS.items()}, f, ensure_ascii=False)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        print("Не удалось сохранить пользователей:", e)


def new_user_record():
    return {"first_seen": time.time(), "paid": False, "until": None}


def register_user(user):
    u = USERS.get(user.id)
    changed = False
    if u is None:
        u = USERS[user.id] = new_user_record()
        changed = True
    name, uname = user.full_name or "", user.username or ""
    if u.get("name") != name or u.get("username") != uname:
        u["name"], u["username"] = name, uname
        changed = True
    if changed:
        save_users()
    return u


def paid_active(u):
    return bool(u.get("paid")) and (u.get("until") is None or u["until"] > time.time())


def trial_left_days(u):
    left = TRIAL_DAYS * 86400 - (time.time() - u["first_seen"])
    return math.ceil(left / 86400) if left > 0 else 0


def has_access(user):
    if not PAID_MODE or user.id == ADMIN_ID:
        return True
    u = register_user(user)
    return paid_active(u) or trial_left_days(u) > 0


def state_text(u):
    if paid_active(u):
        if u.get("until") is None:
            return "оплачено (навсегда)"
        return "оплачено до " + time.strftime("%d.%m.%Y", time.localtime(u["until"]))
    days = trial_left_days(u)
    return f"пробный период, осталось дней: {days}" if days else "пробный период закончился"


def access_status(user):
    if not PAID_MODE or user.id == ADMIN_ID:
        return ""
    u = register_user(user)
    if paid_active(u):
        return "Доступ открыт."
    days = trial_left_days(u)
    return f"Пробный период: осталось дней — {days}." if days else "Пробный период закончился."


def paywall_text():
    txt = ("Пробный период закончился. Чтобы продолжить пользоваться ботом, "
           "поддержите проект донатом — после этого доступ открывается насовсем.")
    if DONATE_INFO:
        txt += "\n\n" + DONATE_INFO
    txt += ("\n\nПосле перевода нажмите «Я оплатил»: администратор проверит поступление "
            "и откроет доступ.")
    return txt


def paywall_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я оплатил", callback_data="paid_claim")]])


def user_label(user):
    uname = f" (@{user.username})" if user.username else ""
    return f"{user.full_name}{uname}"


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print("Не удалось прочитать список пользователей:", e)
        return
    for uid, u in raw.items():
        SEEN[int(uid)] = u


def save_seen():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in SEEN.items()}, f, ensure_ascii=False)
        os.replace(tmp, SEEN_FILE)
    except Exception as e:
        print("Не удалось сохранить список пользователей:", e)


async def note_new_user(bot, user):
    """Сообщает администратору, когда новый человек впервые открыл бота."""
    if user.id in SEEN:
        if SEEN[user.id].get("blocked"):  # человек снова пишет боту — значит, разблокировал
            SEEN[user.id]["blocked"] = False
            save_seen()
        return
    if user.id == ADMIN_ID:
        return
    SEEN[user.id] = {"name": user.full_name or "", "username": user.username or "",
                     "first_seen": time.time()}
    save_seen()
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID, f"🆕 Новый пользователь: {user_label(user)}\n"
                          f"ID: {user.id}\nВсего пользователей: {len(SEEN)}")
        except Exception as e:
            print("Не удалось отправить уведомление администратору:", e)


class Form(StatesGroup):
    point_a = State()
    point_c = State()
    point_b = State()
    toll = State()
    edit_value = State()
    broadcast = State()


# ---------- геокодинг и маршруты ----------
HEADERS = {"User-Agent": "mezhgorod-calc-bot/1.0"}


async def geocode(session: aiohttp.ClientSession, query: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1,
              "addressdetails": 1, "accept-language": "ru"}
    async with session.get(url, params=params, headers=HEADERS) as r:
        data = await r.json()
    if not data:
        return None
    addr = data[0].get("address", {})
    place = data[0]["display_name"].split(",")[0]
    region = addr.get("state") or addr.get("region") or addr.get("country", "")
    label = f"{place} ({region})" if region else place
    return float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"], label


async def get_legs(session, points):
    """Возвращает список расстояний по отрезкам (км) или None."""
    coords = ";".join(f"{p[1]},{p[0]}" for p in points)
    url = f"https://router.project-osrm.org/route/v1/driving/{coords}"
    async with session.get(url, params={"overview": "false"}, headers=HEADERS) as r:
        if r.status != 200:
            return None
        data = await r.json()
    if data.get("code") != "Ok":
        return None
    return [leg["distance"] / 1000 for leg in data["routes"][0]["legs"]]


# ---------- расчёт ----------
def round100(x):
    return int(math.floor(x / 100 + 0.5) * 100)


def _r(x):
    return int(math.floor(x + 0.5))


def fee_amount(s):
    """Сумма подачи, которая учитывается в расчёте (0, если выключена или равна нулю)."""
    fee = s.get("fee", 0) or 0
    return _r(fee) if s.get("fee_on", True) and fee > 0 else 0


def breakdown(s, tariff, legs, toll):
    """Возвращает (части, добавки, сумма, итог_до_сотни).
    части — список (сумма, подпись): отрезки маршрута с типом тарифа;
    добавки — список (сумма, подпись): подача и платные дороги."""
    k = 1 + s["urgent_pct"] / 100 if s["urgent_on"] else 1
    if s["mode"] == "double":
        first, second = leg_kinds(s)
        parts = [(_r(legs[0] * s[first][tariff] * k), KIND_NAME[first]),
                 (_r(legs[1] * s[second][tariff] * k), KIND_NAME[second])]
    else:
        parts = [(_r(legs[0] * s["normal"][tariff] * k), "расчёт")]
    extras = []
    fee = fee_amount(s)
    if fee:
        extras.append((_r(fee * k) if FEE_BEFORE_URGENT else fee, "подача"))
    toll_part = _r(toll * k) if TOLL_BEFORE_URGENT else _r(toll)
    if toll_part:
        extras.append((toll_part, "платные дороги"))
    exact = sum(a for a, _ in parts) + sum(a for a, _ in extras)
    return parts, extras, exact, round100(exact)


def price(s, tariff, legs, toll):
    return breakdown(s, tariff, legs, toll)[3]


def format_tariff(s, tariff, legs, toll):
    parts, extras, exact, total = breakdown(s, tariff, legs, toll)
    terms = [f"{money(a)} ({label})" for a, label in parts + extras]
    out = [f"<b>{tariff}</b>"]
    if len(terms) > 1 or exact != total:
        eq = " + ".join(terms)
        out.append(f"{eq} = {money(exact)} ₽" if len(terms) > 1 else f"{money(exact)} ₽ ({parts[0][1]})")
    out.append(f"Итог: <b>{money(total)} ₽</b>")
    return "\n".join(out)


def money(x):
    return f"{x:,}".replace(",", " ")


# ---------- интерфейс ----------
def settings_of(uid):
    return USER_SETTINGS.setdefault(uid, deepcopy(DEFAULTS))


MENU_CALC = "🧮 Рассчитать поездку"
MENU_MODE = "🔀 Режим"
MENU_SETTINGS = "⚙️ Настройки"
MENU_HELP = "❓ Инструкция"

INSTRUCTION = (
    "📖 <b>Как пользоваться</b>\n\n"
    "1️⃣ Нажмите «🧮 Рассчитать поездку» и отправьте точки: откуда, "
    "(промежуточную — если включена двойная тарификация), куда.\n"
    "2️⃣ Укажите сумму платных дорог (0, если их нет).\n"
    "3️⃣ Получите цену по каждому тарифу: расчёт + подача + платные дороги = итог, "
    "округлённый до сотни.\n\n"
    "🔀 <b>Режим</b>\n"
    "• Обычный расчёт — весь путь по одному тарифу.\n"
    "• Двойная тарификация — маршрут через промежуточную точку. Порядок тарифов "
    "переключается кнопкой: обычный → повышенный, повышенный → обычный или "
    "обычный → обычный.\n\n"
    "⚙️ <b>Настройки</b>\n"
    "• Тарифы — цены за км, «Свой тариф» и сумма подачи; любой пункт можно отключить.\n"
    "• Срочная поездка — надбавка в % (по умолчанию 20). Изменить процент: "
    "команда /urgent 25.\n\n"
    "💡 Пишите «город» или «город, улица». Если нашёлся не тот город, добавьте регион. "
    "Расчёт справочный."
)


def reply_kb():
    """Кнопки, закреплённые внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_CALC)],
                  [KeyboardButton(text=MENU_MODE), KeyboardButton(text=MENU_SETTINGS)],
                  [KeyboardButton(text=MENU_HELP)]],
        resize_keyboard=True, is_persistent=True)


def mode_text(s):
    if s["mode"] == "double":
        return ("🔀 <b>Режим: двойная тарификация</b>\n"
                f"Порядок тарифов: {ORDER_TITLE[s.get('order', 'normal_high')]}\n\n"
                "Маршрут считается через промежуточную точку по выбранному порядку тарифов.")
    return "🔀 <b>Режим: обычный расчёт</b>\n\nВесь путь считается по одному тарифу."


def mode_kb(s):
    normal = s["mode"] == "normal"
    rows = [[InlineKeyboardButton(text=("✅ " if normal else "") + "Обычный расчёт",
                                  callback_data="mode_set:normal"),
             InlineKeyboardButton(text=("" if normal else "✅ ") + "Двойная тарификация",
                                  callback_data="mode_set:double")]]
    if not normal:
        rows.append([InlineKeyboardButton(
            text=f"Порядок: {ORDER_TITLE[s.get('order', 'normal_high')]}",
            callback_data="order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(s):
    urgent = f"вкл ({s['urgent_pct']}%)" if s["urgent_on"] else f"выкл ({s['urgent_pct']}%)"
    return f"⚙️ <b>Настройки</b>\n\nСрочная поездка: {urgent}"


def settings_kb(s):
    urgent_txt = f"Срочная: {'вкл' if s['urgent_on'] else 'выкл'} ({s['urgent_pct']}%)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton(text=urgent_txt, callback_data="urgent")],
    ])


async def safe_edit(c, text, kb):
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass  # например, текст не изменился


def tariffs_text(s):
    title = "Обычный тариф" if s["mode"] == "double" else "Тарифы"
    txt = f"<b>{title} (₽/км)</b>\n"
    mark = lambda t: "✅" if s["enabled"].get(t, True) else "⬜"
    txt += "\n".join(f"{mark(t)} {t}: {s['normal'][t]}" for t in TARIFFS)
    if uses_high(s):
        txt += "\n\n<b>Повышенный тариф (₽/км)</b>\n"
        txt += "\n".join(f"{mark(t)} {t}: {s['high'][t]}" for t in TARIFFS)
    fee = s.get("fee", 0) or 0
    fee_mark = "✅" if s.get("fee_on", True) else "⬜"
    txt += f"\n\n{fee_mark} Сумма подачи: " + (f"{money(_r(fee))} ₽" if fee > 0 else "не задана")
    txt += "\n\n✅ — показывается в расчёте, ⬜ — скрыт"
    return txt


def tariffs_kb(s):
    rows = [[InlineKeyboardButton(text=f"{t}: {s['normal'][t]}",
                                  callback_data=f"edit:normal:{t}"),
             InlineKeyboardButton(text="✅ вкл" if s["enabled"].get(t, True) else "⬜ выкл",
                                  callback_data=f"toggle:{t}")] for t in TARIFFS]
    if uses_high(s):
        rows += [[InlineKeyboardButton(text=f"Повышенный · {t}: {s['high'][t]}",
                                       callback_data=f"edit:high:{t}")] for t in TARIFFS]
    rows.append([
        InlineKeyboardButton(text=f"Подача: {money(_r(s.get('fee', 0) or 0))} ₽",
                             callback_data="edit_fee"),
        InlineKeyboardButton(text="✅ вкл" if s.get("fee_on", True) else "⬜ выкл",
                             callback_data="fee_toggle")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await note_new_user(m.bot, m.from_user)
    text = ("Калькулятор межгорода.\nКнопки внизу: расчёт поездки, режим, настройки "
            "и инструкция.")
    status = access_status(m.from_user)
    if status:
        text += "\n\n" + status
    await m.answer(text, reply_markup=reply_kb())


async def begin_calc(bot, user, answer, state):
    await note_new_user(bot, user)
    if not has_access(user):
        await answer(paywall_text(), reply_markup=paywall_kb())
        return
    await state.set_state(Form.point_a)
    await answer("Точка A — откуда? (город или адрес)", reply_markup=reply_kb())


@dp.message(F.text == MENU_CALC)
async def menu_calc(m: Message, state: FSMContext):
    await state.clear()
    await begin_calc(m.bot, m.from_user, m.answer, state)


@dp.message(F.text == MENU_MODE)
async def menu_mode(m: Message, state: FSMContext):
    await state.clear()
    s = settings_of(m.from_user.id)
    await m.answer(mode_text(s), reply_markup=mode_kb(s), parse_mode="HTML")


@dp.message(F.text == MENU_SETTINGS)
async def menu_settings(m: Message, state: FSMContext):
    await state.clear()
    s = settings_of(m.from_user.id)
    await m.answer(settings_text(s), reply_markup=settings_kb(s), parse_mode="HTML")


@dp.message(F.text == MENU_HELP)
async def menu_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(INSTRUCTION, parse_mode="HTML")


@dp.message(Command("admin"))
async def admin_help(m: Message):
    if not is_admin(m):
        return
    await m.answer("Команды администратора:\n"
                   "/users — статистика пользователей\n"
                   "/broadcast — рассылка сообщения всем пользователям\n"
                   "/grant ID [дней] — открыть доступ\n"
                   "/revoke ID — закрыть доступ\n"
                   "/id — показать свой Telegram ID")


# ---------- рассылка всем пользователям ----------
PENDING_BROADCAST: dict[int, tuple[int, int]] = {}  # админ -> (чат, id сообщения)
BROADCAST_PAUSE = 0.05  # пауза между отправками (лимит Telegram ~30 сообщений в секунду)


def broadcast_recipients():
    """Все, кто открывал бота (кроме администратора и заблокировавших бота)."""
    ids = set(SEEN) | set(USER_SETTINGS) | set(USERS)
    ids.discard(ADMIN_ID)
    return sorted(i for i in ids if not SEEN.get(i, {}).get("blocked"))


async def send_copy(bot, uid, from_chat, message_id):
    for attempt in range(2):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=message_id)
            return "ok"
        except Exception as e:
            name = type(e).__name__
            if name == "TelegramRetryAfter" and attempt == 0:
                await asyncio.sleep(getattr(e, "retry_after", 1) + 1)
                continue
            text = str(e).lower()
            if (name == "TelegramForbiddenError" or "blocked" in text
                    or "deactivated" in text or "chat not found" in text):
                return "blocked"
            return "error"
    return "error"


@dp.message(Command("cancel"))
async def cancel_input(m: Message, state: FSMContext):
    if await state.get_state() is None and m.from_user.id not in PENDING_BROADCAST:
        await m.answer("Нечего отменять.")
        return
    await state.clear()
    PENDING_BROADCAST.pop(m.from_user.id, None)
    await m.answer("Отменено.")


@dp.message(Command("broadcast"))
async def broadcast_start(m: Message, state: FSMContext):
    if not is_admin(m):
        return
    await state.set_state(Form.broadcast)
    await m.answer("Отправьте сообщение для рассылки: текст, можно с фото или видео. "
                   "Для отмены: /cancel")


@dp.message(Form.broadcast)
async def broadcast_preview(m: Message, state: FSMContext):
    if not is_admin(m):
        await state.clear()
        return
    PENDING_BROADCAST[m.from_user.id] = (m.chat.id, m.message_id)
    await state.clear()
    n = len(broadcast_recipients())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Отправить ({n})", callback_data="bc_send"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")]])
    await m.answer(f"Получателей: {n}. Отправить это сообщение всем?", reply_markup=kb)


@dp.callback_query(F.data == "bc_cancel")
async def broadcast_cancel(c: CallbackQuery):
    if not ADMIN_ID or c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True)
        return
    PENDING_BROADCAST.pop(c.from_user.id, None)
    await c.message.edit_text("Рассылка отменена.")
    await c.answer()


@dp.callback_query(F.data == "bc_send")
async def broadcast_send(c: CallbackQuery):
    if not ADMIN_ID or c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True)
        return
    pending = PENDING_BROADCAST.pop(c.from_user.id, None)  # повторное нажатие ничего не сделает
    if not pending:
        await c.answer("Нет сообщения для рассылки", show_alert=True)
        return
    await c.answer()
    recipients = broadcast_recipients()
    await c.message.edit_text(f"Отправляю… получателей: {len(recipients)}")
    ok = blocked = errors = 0
    for uid in recipients:
        result = await send_copy(c.bot, uid, *pending)
        if result == "ok":
            ok += 1
        elif result == "blocked":
            blocked += 1
            rec = SEEN.setdefault(uid, {"name": "", "username": "", "first_seen": time.time()})
            rec["blocked"] = True
        else:
            errors += 1
        await asyncio.sleep(BROADCAST_PAUSE)
    if blocked:
        save_seen()
    await c.message.edit_text(f"✅ Рассылка завершена\nДоставлено: {ok}\n"
                              f"Заблокировали бота: {blocked}\nОшибок: {errors}")


@dp.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


def is_admin(m: Message):
    return bool(ADMIN_ID) and m.from_user.id == ADMIN_ID


@dp.message(Command("grant"))
async def grant(m: Message):
    if not is_admin(m):
        return
    parts = (m.text or "").split()
    try:
        uid = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else None
        assert days is None or days > 0
    except Exception:
        await m.answer("Формат: /grant ID [дней]. Без числа дней доступ открывается навсегда.")
        return
    u = USERS.setdefault(uid, new_user_record())
    u["paid"] = True
    u["until"] = None if days is None else time.time() + days * 86400
    save_users()
    await m.answer(f"Пользователь {uid}: {state_text(u)}")
    try:
        await m.bot.send_message(uid, "Вам открыт доступ к боту. Спасибо!")
    except Exception:
        pass


@dp.message(Command("revoke"))
async def revoke(m: Message):
    if not is_admin(m):
        return
    parts = (m.text or "").split()
    try:
        uid = int(parts[1])
    except Exception:
        await m.answer("Формат: /revoke ID")
        return
    u = USERS.get(uid)
    if not u:
        await m.answer("Такого пользователя нет.")
        return
    u["paid"], u["until"] = False, None
    save_users()
    await m.answer(f"Пользователь {uid}: {state_text(u)}")


@dp.message(Command("users"))
async def users_list(m: Message):
    if not is_admin(m):
        return
    if not USERS:
        await m.answer("Пока никого нет. Список ведётся, когда включён PAID_MODE.")
        return
    paid = sum(paid_active(u) for u in USERS.values())
    trial = sum((not paid_active(u)) and trial_left_days(u) > 0 for u in USERS.values())
    expired = len(USERS) - paid - trial
    lines = [f"Всего: {len(USERS)} · оплатили: {paid} · на пробном: {trial} · закончился пробный: {expired}", ""]
    recent = sorted(USERS.items(), key=lambda kv: kv[1]["first_seen"], reverse=True)[:30]
    for uid, u in recent:
        uname = f" @{u['username']}" if u.get("username") else ""
        lines.append(f"{uid} {u.get('name', '')}{uname} — {state_text(u)}")
    await m.answer("\n".join(lines))


@dp.callback_query(F.data == "paid_claim")
async def paid_claim(c: CallbackQuery):
    if has_access(c.from_user):
        await c.answer("Доступ уже открыт", show_alert=True)
        return
    if not ADMIN_ID:
        await c.answer("Приём оплаты не настроен, напишите администратору", show_alert=True)
        return
    u = register_user(c.from_user)
    now = time.time()
    if now - u.get("last_claim", 0) < CLAIM_COOLDOWN:
        await c.answer("Заявка уже отправлена, дождитесь подтверждения", show_alert=True)
        return
    u["last_claim"] = now
    save_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_ok:{c.from_user.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_no:{c.from_user.id}"),
    ]])
    await c.bot.send_message(
        ADMIN_ID, f"Заявка об оплате\n{user_label(c.from_user)}\nID: {c.from_user.id}",
        reply_markup=kb)
    await c.message.answer("Заявка отправлена. Когда оплата будет подтверждена, я напишу вам.")
    await c.answer()


@dp.callback_query(F.data.startswith("adm_"))
async def admin_decision(c: CallbackQuery):
    if not ADMIN_ID or c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True)
        return
    action, uid_s = c.data.split(":", 1)
    uid = int(uid_s)
    u = USERS.setdefault(uid, new_user_record())
    if action == "adm_ok":
        u["paid"], u["until"] = True, None
        save_users()
        note, user_msg = "✅ Подтверждено", "Оплата подтверждена, доступ к боту открыт насовсем. Спасибо за поддержку!"
    else:
        u["last_claim"] = 0
        save_users()
        note, user_msg = "❌ Отклонено", ("Оплата не найдена. Если вы переводили деньги, "
                                           "напишите администратору.")
    try:
        await c.bot.send_message(uid, user_msg)
    except Exception:
        pass
    await c.message.edit_text((c.message.text or "") + "\n\n" + note)
    await c.answer()


@dp.callback_query(F.data == "back")
async def back(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    await safe_edit(c, settings_text(s), settings_kb(s))
    await c.answer()


@dp.callback_query(F.data == "mode")
async def toggle_mode(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    s["mode"] = "double" if s["mode"] == "normal" else "normal"
    save_settings()
    await safe_edit(c, mode_text(s), mode_kb(s))
    await c.answer()


@dp.callback_query(F.data.startswith("mode_set:"))
async def set_mode(c: CallbackQuery):
    mode = c.data.split(":", 1)[1]
    if mode in ("normal", "double"):
        s = settings_of(c.from_user.id)
        s["mode"] = mode
        save_settings()
        await safe_edit(c, mode_text(s), mode_kb(s))
    await c.answer()


@dp.callback_query(F.data == "order")
async def cycle_order(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    cur = s.get("order", "normal_high")
    s["order"] = ORDERS[(ORDERS.index(cur) + 1) % len(ORDERS)]
    save_settings()
    await safe_edit(c, mode_text(s), mode_kb(s))
    await c.answer()


@dp.callback_query(F.data == "fee_toggle")
async def toggle_fee(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    s["fee_on"] = not s.get("fee_on", True)
    save_settings()
    await c.message.edit_text(tariffs_text(s) + "\n\nНажмите на тариф, чтобы изменить цену.",
                              reply_markup=tariffs_kb(s), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "edit_fee")
async def edit_fee(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_value)
    await state.update_data(group="fee", tariff="fee")
    await c.message.answer("Сумма подачи, ₽ (0 — не показывать и не учитывать):")
    await c.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_tariff(c: CallbackQuery):
    t = c.data.split(":", 1)[1]
    s = settings_of(c.from_user.id)
    if t not in TARIFFS:
        await c.answer()
        return
    if s["enabled"].get(t, True) and sum(s["enabled"].get(x, True) for x in TARIFFS) == 1:
        await c.answer("Должен остаться хотя бы один тариф", show_alert=True)
        return
    s["enabled"][t] = not s["enabled"].get(t, True)
    save_settings()
    await c.message.edit_text(tariffs_text(s) + "\n\nНажмите на тариф, чтобы изменить цену.",
                              reply_markup=tariffs_kb(s), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "urgent")
async def toggle_urgent(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    s["urgent_on"] = not s["urgent_on"]
    save_settings()
    await safe_edit(c, settings_text(s), settings_kb(s))
    await c.answer()
    if s["urgent_on"]:
        await c.message.answer(f"Надбавка за срочность — {s['urgent_pct']}%. "
                               "Изменить: /urgent 25")


@dp.message(Command("urgent"))
async def set_urgent(m: Message):
    parts = (m.text or "").split()
    try:
        pct = float(parts[1].replace(",", "."))
        assert 0 <= pct <= 500
    except Exception:
        await m.answer("Формат: /urgent 20")
        return
    s = settings_of(m.from_user.id)
    s["urgent_pct"] = int(pct) if pct.is_integer() else pct
    save_settings()
    await m.answer(f"Срочность: {s['urgent_pct']}%")


@dp.callback_query(F.data == "tariffs")
async def show_tariffs(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    await c.message.edit_text(tariffs_text(s) + "\n\nНажмите на тариф, чтобы изменить.",
                              reply_markup=tariffs_kb(s), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def edit_tariff(c: CallbackQuery, state: FSMContext):
    _, group, tariff = c.data.split(":", 2)
    await state.set_state(Form.edit_value)
    await state.update_data(group=group, tariff=tariff)
    await c.message.answer(f"Новая цена для «{tariff}» (₽/км):")
    await c.answer()


@dp.message(Form.edit_value)
async def save_tariff(m: Message, state: FSMContext):
    d = await state.get_data()
    is_fee = d.get("group") == "fee"
    try:
        val = float((m.text or "").replace(",", ".").replace(" ", ""))
        assert val >= 0 if is_fee else val > 0
    except Exception:
        await m.answer("Введите число, например 0 или 300." if is_fee
                       else "Введите положительное число.")
        return
    s = settings_of(m.from_user.id)
    num = int(val) if val.is_integer() else val
    if is_fee:
        s["fee"] = num
    else:
        s[d["group"]][d["tariff"]] = num
    save_settings()
    await state.clear()
    await m.answer(tariffs_text(s), reply_markup=tariffs_kb(s), parse_mode="HTML")


# ---------- расчёт поездки ----------
@dp.callback_query(F.data == "calc")
async def calc_start(c: CallbackQuery, state: FSMContext):
    await begin_calc(c.bot, c.from_user, c.message.answer, state)
    await c.answer()


@dp.message(Form.point_a)
async def got_a(m: Message, state: FSMContext):
    await state.update_data(a=m.text)
    s = settings_of(m.from_user.id)
    if s["mode"] == "double":
        await state.set_state(Form.point_c)
        order = s.get("order", "normal_high")
        if order == "normal_normal":
            q = "Промежуточная точка — через какую точку поедем? (весь маршрут по обычному тарифу)"
        elif order == "high_normal":
            q = "Промежуточная точка — где заканчивается повышенный тариф и начинается обычный?"
        else:
            q = "Промежуточная точка — где заканчивается обычный тариф и начинается повышенный?"
        await m.answer(q)
    else:
        await state.set_state(Form.point_b)
        await m.answer("Точка B — куда?")


@dp.message(Form.point_c)
async def got_c(m: Message, state: FSMContext):
    await state.update_data(c=m.text)
    await state.set_state(Form.point_b)
    await m.answer("Точка B — куда?")


@dp.message(Form.point_b)
async def got_b(m: Message, state: FSMContext):
    await state.update_data(b=m.text)
    await state.set_state(Form.toll)
    await m.answer("Сумма за платные дороги, ₽ (0 — если нет):")


@dp.message(Form.toll)
async def got_toll(m: Message, state: FSMContext):
    try:
        toll = float((m.text or "").replace(",", ".").replace(" ", ""))
        assert toll >= 0
    except Exception:
        await m.answer("Введите число, например 0 или 450.")
        return
    data = await state.get_data()
    await state.clear()
    s = settings_of(m.from_user.id)
    wait = await m.answer("Считаю маршрут…")

    skip = {"нет", "-", "—", "no", "0"}
    use_c = s["mode"] == "double" and data.get("c", "").strip().lower() not in skip
    # без промежуточной точки весь путь считается по обычному тарифу
    cs = s if (s["mode"] != "double" or use_c) else {**s, "mode": "normal"}
    names = [data["a"]] + ([data["c"]] if use_c else []) + [data["b"]]
    points = []
    async with aiohttp.ClientSession() as session:
        for i, name in enumerate(names):
            if i:
                await asyncio.sleep(1)  # лимит Nominatim: 1 запрос/сек
            p = await geocode(session, name)
            if not p:
                await wait.edit_text(f"Не нашёл точку «{name}». Уточните адрес: /start")
                return
            points.append(p)
        legs = await get_legs(session, points)
    if not legs:
        found = " → ".join(p[3] for p in points)
        await wait.edit_text("Не удалось построить маршрут.\nНашёл точки: " + found +
                             "\nУточните адреса (город, улица) и повторите: /start")
        return

    lines = [f"<b>{' → '.join(n.strip() for n in names)}</b>",
             "Найдено: " + " → ".join(p[3] for p in points)]
    if cs["mode"] == "double":
        lab = tuple(KIND_NAME[k] for k in leg_kinds(cs))
        lines.append(f"Расстояние: {sum(legs):.1f} км "
                     f"({legs[0]:.1f} {lab[0]} + {legs[1]:.1f} {lab[1]})")
    else:
        lines.append(f"Расстояние: {legs[0]:.1f} км")
    lines.append("")
    blocks = [format_tariff(cs, t, legs, toll) for t in TARIFFS if cs["enabled"].get(t, True)]
    lines.append("\n\n".join(blocks))
    extra = []
    if fee_amount(s):
        extra.append(f"подача: {money(fee_amount(s))} ₽")
    if toll:
        extra.append(f"платные дороги: {money(round(toll))} ₽")
    if s["urgent_on"]:
        extra.append(f"срочная +{s['urgent_pct']}%")
    if extra:
        lines.append("\nУчтено: " + ", ".join(extra))
    lines.append("\n<i>Расчёт справочный. Итог округлён до сотни.</i>")
    await wait.edit_text("\n".join(lines), parse_mode="HTML")


async def main():
    load_settings()
    load_users()
    load_seen()
    if PAID_MODE and not ADMIN_ID:
        print("ВНИМАНИЕ: PAID_MODE включён, но ADMIN_ID не задан — оплаты некому подтверждать.")
    bot = Bot(os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
