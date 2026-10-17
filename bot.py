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
        for k in ("urgent_on", "urgent_pct", "mode"):
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


def price(s, tariff, legs, toll):
    if s["mode"] == "double":
        base = legs[0] * s["normal"][tariff] + legs[1] * s["high"][tariff]
    else:
        base = legs[0] * s["normal"][tariff]
    k = 1 + s["urgent_pct"] / 100 if s["urgent_on"] else 1
    total = (base + toll) * k if TOLL_BEFORE_URGENT else base * k + toll
    return round100(total)


def money(x):
    return f"{x:,}".replace(",", " ")


# ---------- интерфейс ----------
def settings_of(uid):
    return USER_SETTINGS.setdefault(uid, deepcopy(DEFAULTS))


def main_kb(s):
    mode_txt = "Обычный расчёт" if s["mode"] == "normal" else "Двойная тарификация"
    urgent_txt = f"Срочная: {'вкл' if s['urgent_on'] else 'выкл'} ({s['urgent_pct']}%)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Режим: {mode_txt}", callback_data="mode")],
        [InlineKeyboardButton(text="Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton(text=urgent_txt, callback_data="urgent")],
        [InlineKeyboardButton(text="🧮 Рассчитать поездку", callback_data="calc")],
    ])


def tariffs_text(s):
    title = "Обычная часть маршрута" if s["mode"] == "double" else "Тарифы"
    txt = f"<b>{title} (₽/км)</b>\n"
    txt += "\n".join(f"{t}: {s['normal'][t]}" for t in TARIFFS)
    if s["mode"] == "double":
        txt += "\n\n<b>Повышенный тариф, второй отрезок (₽/км)</b>\n"
        txt += "\n".join(f"{t}: {s['high'][t]}" for t in TARIFFS)
    return txt


def tariffs_kb(s):
    rows = [[InlineKeyboardButton(text=f"{t}: {s['normal'][t]}",
                                  callback_data=f"edit:normal:{t}")] for t in TARIFFS]
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
    await m.answer("Калькулятор межгорода.\nНастройте параметры или сразу жмите «Рассчитать».",
                   reply_markup=main_kb(s))


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
        lines.append(f"Расстояние: {sum(legs):.1f} км "
                     f"({legs[0]:.1f} + {legs[1]:.1f})")
    else:
        lines.append(f"Расстояние: {legs[0]:.1f} км")
    lines.append("")
    for t in TARIFFS:
        lines.append(f"• {t}: <b>{money(price(cs, t, legs, toll))}</b> ₽")
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
    bot = Bot(os.environ["BOT_TOKEN"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
