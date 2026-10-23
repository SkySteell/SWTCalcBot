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
                           InlineKeyboardMarkup, Message)

TARIFFS = ["Стандарт", "Комфорт", "Комфорт+", "Минивэн"]

# Значения по умолчанию (₽/км)
DEFAULTS = {
    "normal": {"Стандарт": 27, "Комфорт": 30, "Комфорт+": 35, "Минивэн": 50},
    "high": {"Стандарт": 70, "Комфорт": 80, "Комфорт+": 90, "Минивэн": 110},
    "urgent_on": False,
    "urgent_pct": 20,
    "mode": "normal",  # normal | double
    "reverse": False,  # двойная тарификация: False = обычный → повышенный, True = наоборот
    "enabled": {t: True for t in TARIFFS},  # какие тарифы показывать в расчёте
}

# True  — платные дороги прибавляются до наценки за срочность (20% берётся и с них)
# False — срочность считается только с поездки, платные дороги прибавляются после
TOLL_BEFORE_URGENT = False

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
        for k in ("urgent_on", "urgent_pct", "mode", "reverse"):
            if k in st:
                base[k] = st[k]
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
PAID_MODE = os.getenv("PAID_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3") or 3)
DONATE_INFO = os.getenv("DONATE_INFO", "").strip()
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


class Form(StatesGroup):
    point_a = State()
    point_c = State()
    point_b = State()
    toll = State()
    edit_value = State()


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


def breakdown(s, tariff, legs, toll):
    """Возвращает (части, платные_дороги, сумма, итог_до_сотни).
    части — список (сумма, подпись): отрезки маршрута с типом тарифа."""
    k = 1 + s["urgent_pct"] / 100 if s["urgent_on"] else 1
    if s["mode"] == "double":
        first, second = ("high", "normal") if s.get("reverse") else ("normal", "high")
        names = {"normal": "обычный", "high": "повышенный"}
        parts = [(_r(legs[0] * s[first][tariff] * k), names[first]),
                 (_r(legs[1] * s[second][tariff] * k), names[second])]
    else:
        parts = [(_r(legs[0] * s["normal"][tariff] * k), "расчёт")]
    toll_part = _r(toll * k) if TOLL_BEFORE_URGENT else _r(toll)
    exact = sum(a for a, _ in parts) + toll_part
    return parts, toll_part, exact, round100(exact)


def price(s, tariff, legs, toll):
    return breakdown(s, tariff, legs, toll)[3]


def format_tariff(s, tariff, legs, toll):
    parts, toll_part, exact, total = breakdown(s, tariff, legs, toll)
    terms = [f"{money(a)} ({label})" for a, label in parts]
    if toll_part:
        terms.append(f"{money(toll_part)} (платные дороги)")
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


def main_kb(s):
    mode_txt = "Обычный расчёт" if s["mode"] == "normal" else "Двойная тарификация"
    urgent_txt = f"Срочная: {'вкл' if s['urgent_on'] else 'выкл'} ({s['urgent_pct']}%)"
    rows = [[InlineKeyboardButton(text=f"Режим: {mode_txt}", callback_data="mode")]]
    if s["mode"] == "double":
        dir_txt = ("повышенный → обычный" if s.get("reverse")
                   else "обычный → повышенный")
        rows.append([InlineKeyboardButton(text=f"Порядок: {dir_txt}",
                                          callback_data="reverse")])
    rows += [
        [InlineKeyboardButton(text="Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton(text=urgent_txt, callback_data="urgent")],
        [InlineKeyboardButton(text="🧮 Рассчитать поездку", callback_data="calc")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_text(s):
    title = "Обычная часть маршрута" if s["mode"] == "double" else "Тарифы"
    txt = f"<b>{title} (₽/км)</b>\n"
    mark = lambda t: "✅" if s["enabled"].get(t, True) else "⬜"
    txt += "\n".join(f"{mark(t)} {t}: {s['normal'][t]}" for t in TARIFFS)
    if s["mode"] == "double":
        txt += "\n\n<b>Повышенный тариф (₽/км)</b>\n"
        txt += "\n".join(f"{mark(t)} {t}: {s['high'][t]}" for t in TARIFFS)
    txt += "\n\n✅ — показывается в расчёте, ⬜ — скрыт"
    return txt


def tariffs_kb(s):
    rows = [[InlineKeyboardButton(text=f"{t}: {s['normal'][t]}",
                                  callback_data=f"edit:normal:{t}"),
             InlineKeyboardButton(text="✅ вкл" if s["enabled"].get(t, True) else "⬜ выкл",
                                  callback_data=f"toggle:{t}")] for t in TARIFFS]
    if s["mode"] == "double":
        rows += [[InlineKeyboardButton(text=f"Повышенный · {t}: {s['high'][t]}",
                                       callback_data=f"edit:high:{t}")] for t in TARIFFS]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    s = settings_of(m.from_user.id)
    text = "Калькулятор межгорода.\nНастройте параметры или сразу жмите «Рассчитать»."
    status = access_status(m.from_user)
    if status:
        text += "\n\n" + status
    await m.answer(text, reply_markup=main_kb(s))


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
    await c.message.edit_text("Калькулятор межгорода.", reply_markup=main_kb(s))
    await c.answer()


@dp.callback_query(F.data == "mode")
async def toggle_mode(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    s["mode"] = "double" if s["mode"] == "normal" else "normal"
    save_settings()
    await c.message.edit_reply_markup(reply_markup=main_kb(s))
    await c.answer()


@dp.callback_query(F.data == "reverse")
async def toggle_reverse(c: CallbackQuery):
    s = settings_of(c.from_user.id)
    s["reverse"] = not s.get("reverse", False)
    save_settings()
    await c.message.edit_reply_markup(reply_markup=main_kb(s))
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
    await c.message.edit_reply_markup(reply_markup=main_kb(s))
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
    await m.answer(f"Срочность: {s['urgent_pct']}%", reply_markup=main_kb(s))


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
    try:
        val = float((m.text or "").replace(",", "."))
        assert val > 0
    except Exception:
        await m.answer("Введите положительное число.")
        return
    d = await state.get_data()
    s = settings_of(m.from_user.id)
    s[d["group"]][d["tariff"]] = int(val) if val.is_integer() else val
    save_settings()
    await state.clear()
    await m.answer(tariffs_text(s), reply_markup=tariffs_kb(s), parse_mode="HTML")


# ---------- расчёт поездки ----------
@dp.callback_query(F.data == "calc")
async def calc_start(c: CallbackQuery, state: FSMContext):
    if not has_access(c.from_user):
        await c.message.answer(paywall_text(), reply_markup=paywall_kb())
        await c.answer()
        return
    await state.set_state(Form.point_a)
    await c.message.answer("Точка A — откуда? (город или адрес)")
    await c.answer()


@dp.message(Form.point_a)
async def got_a(m: Message, state: FSMContext):
    await state.update_data(a=m.text)
    s = settings_of(m.from_user.id)
    if s["mode"] == "double":
        await state.set_state(Form.point_c)
        await m.answer("Промежуточная точка — где заканчивается обычный тариф?")
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
        lab = ("повышенный", "обычный") if cs.get("reverse") else ("обычный", "повышенный")
        lines.append(f"Расстояние: {sum(legs):.1f} км "
                     f"({legs[0]:.1f} {lab[0]} + {legs[1]:.1f} {lab[1]})")
    else:
        lines.append(f"Расстояние: {legs[0]:.1f} км")
    lines.append("")
    blocks = [format_tariff(cs, t, legs, toll) for t in TARIFFS if cs["enabled"].get(t, True)]
    lines.append("\n\n".join(blocks))
    extra = []
    if toll:
        extra.append(f"платные дороги: {money(round(toll))} ₽")
    if s["urgent_on"]:
        extra.append(f"срочная +{s['urgent_pct']}%")
    if extra:
        lines.append("\nУчтено: " + ", ".join(extra))
    lines.append("\n<i>Расчёт справочный. Итог округлён до сотни.</i>")
    await wait.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=main_kb(s))


async def main():
    load_settings()
    load_users()
    if PAID_MODE and not ADMIN_ID:
        print("ВНИМАНИЕ: PAID_MODE включён, но ADMIN_ID не задан — оплаты некому подтверждать.")
    bot = Bot(os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
