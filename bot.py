"""
VOLGA EXCHANGE — бот-«дверь» в приложение обменника, чат заявок и админ-панель курсов.

Клиент:
  /start               — баннер с рыбкой + кнопка «🔴 Открыть обменник»
  отправил заявку      — получает номер заявки, потом уведомления о статусе

Команда (группа заявок):
  /bindchat            — написать В ГРУППЕ: бот запомнит её как чат заявок
  кнопки под заявкой   — 🟡 Взять · ✅ Выполнено · ❌ Отмена
  /orders              — открытые заявки

Админ (в личке с ботом):
  /admin               — панель: курсы, наценки, ник оператора (всё кнопками)
  /myid                — узнать свой Telegram ID
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip("-").isdigit()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")   # адрес бота в Railway (для живых курсов)
PORT = int(os.getenv("PORT", "8080"))
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "7"))))

BASE = Path(__file__).parent
BANNER = BASE / "assets" / "banner.jpg"
DATA_FILE = BASE / "data.json"

# Что можно менять в админке: ключ → (название, единица, значение по умолчанию)
RATE_FIELDS = {
    "crypto":     ("Крипта → наличные", "%", 3),
    "rub_cash":   ("Наличные ₽ → наличные", "%", 3),
    "rub_card":   ("₽ переводом → наличные", "%", 3),
    "swap":       ("Крипта → крипта", "%", 2),
    "usdt_buy":   ("Клиент покупает 1 USDT за", "₽", 91),
    "usdt_sell":  ("Клиент продаёт 1 USDT за", "₽", 83),
    "rub_market": ("Мой рыночный курс ₽/USDT (0 = с биржи)", "₽", 0),
    "min_cash":   ("Минимум, если едет курьер", "₽", 30000),
    "min_online": ("Минимум для переводов, банкомата и крипты", "₽", 5000),
}
# Ступени по сумме (в рублях): [от суммы, надбавка %]. Плюс — курс хуже, минус — лучше.
DEFAULT_TIERS = [[0, 4], [10000, 1], [30000, 0]]   # при наценке 3%: 7% / 4% / 3%
RATE_ORDER = list(RATE_FIELDS)  # порядок важен: так курсы передаются в приложение


# ─────────────────────────── хранилище ───────────────────────────
def load_data() -> dict:
    try:
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    rates = d.setdefault("rates", {})
    for k, (_, _, default) in RATE_FIELDS.items():
        env = os.getenv("RATE_" + k.upper())
        rates.setdefault(k, float(env) if env else default)
    d.setdefault("tiers", DEFAULT_TIERS)
    d.setdefault("manager", os.getenv("MANAGER_USERNAME", "volga_manager").lstrip("@"))
    d.setdefault("orders_chat", int(os.getenv("ORDERS_CHAT_ID", "0") or 0))
    d.setdefault("next_id", 1001)
    d.setdefault("orders", {})
    return d


DATA = load_data()


def save() -> None:
    if len(DATA["orders"]) > 500:
        for k in sorted(DATA["orders"], key=int)[:-500]:
            DATA["orders"].pop(k)
    DATA_FILE.write_text(json.dumps(DATA, ensure_ascii=False), encoding="utf-8")


def num(v: float) -> str:
    return f"{v:g}"


# ─────────────────────────── оформление ──────────────────────────
LINE = "━━━━━━━━━━━━━━"
WAVE = "〰〰〰 🌊 〰〰〰"

WELCOME = (
    "🎏 <b>VOLGA EXCHANGE</b>  ·  両替\n"
    f"{LINE}\n\n"
    "Как карп поднимается против течения —\n"
    "так и ваши деньги плывут туда, куда нужно.\n\n"
    "▫️ крипта: USDT, BTC и другие\n"
    "▫️ рубли наличными (Москва, Петербург, Самара) или переводом\n"
    "▫️ выдача: 🇹🇭 Таиланд · 🇻🇳 Вьетнам · 🇬🇪 Грузия · 🇹🇷 Турция · 🇦🇪 Дубай\n"
    "▫️ рубли ⇄ USDT и крипта → крипта с AML-проверкой\n\n"
    f"{WAVE}\n"
    "Нажмите <b>🔴 Открыть обменник</b> внизу экрана 👇"
)

STATUS = {"new": ("🔴", "Новая"), "work": ("🟡", "В работе"), "done": ("✅", "Выполнена"), "cancel": ("❌", "Отменена")}
CLIENT_MSG = {
    "work": "🟡 Заявка <b>№{id}</b> взята в работу. Оператор {op} скоро напишет вам.",
    "done": "✅ Заявка <b>№{id}</b> выполнена. Спасибо, что выбрали VOLGA EXCHANGE 🎏",
    "cancel": "❌ Заявка <b>№{id}</b> отменена. Если это ошибка — напишите оператору @{mgr}.",
}


def webapp_url() -> str:
    sep = "&" if "?" in WEBAPP_URL else "?"
    s = ",".join(num(DATA["rates"][k]) for k in RATE_ORDER)
    t = ",".join(f"{num(a)}:{num(b)}" for a, b in DATA["tiers"])
    url = f"{WEBAPP_URL}{sep}s={s}&t={t}&m={DATA['manager']}"
    if PUBLIC_URL:
        url += f"&api={PUBLIC_URL}"
    return url


def app_kb() -> ReplyKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔴 Открыть обменник", web_app=WebAppInfo(url=webapp_url()))]],
        resize_keyboard=True, is_persistent=True)


def manager_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать оператору", url=f"https://t.me/{DATA['manager']}")]])


def order_card(o: dict, with_profit: bool = True) -> str:
    icon, name = STATUS[o["status"]]
    who = f"@{o['username']}" if o.get("username") else escape(o["name"])
    s = (f"{icon} <b>ЗАЯВКА №{o['id']}</b> · {name}\n{LINE}\n"
         f"🕰 {o['time']}\n👤 {who} (<code>{o['client_id']}</code>)\n\n{escape(o['text'])}")
    if with_profit and o.get("profit"):
        s += f"\n\n💰 <b>Заработок {escape(o['profit'])}</b>"
        if o.get("market") == "примерный":
            s += "\n<i>(рынок не загрузился, сумма примерная)</i>"
    if o.get("history"):
        s += "\n\n" + "\n".join(f"<i>{escape(h)}</i>" for h in o["history"])
    return s


def order_kb(o: dict) -> InlineKeyboardMarkup | None:
    rows = []
    if o["status"] == "new":
        rows.append([InlineKeyboardButton(text="🟡 Взять", callback_data=f"st:{o['id']}:work"),
                     InlineKeyboardButton(text="❌ Отмена", callback_data=f"st:{o['id']}:cancel")])
    elif o["status"] == "work":
        rows.append([InlineKeyboardButton(text="✅ Выполнено", callback_data=f"st:{o['id']}:done"),
                     InlineKeyboardButton(text="❌ Отмена", callback_data=f"st:{o['id']}:cancel")])
    if o.get("username"):
        rows.append([InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{o['username']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


router = Router()


class Edit(StatesGroup):
    value = State()


# ─────────────────────────── клиент ──────────────────────────────
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    kb = app_kb() or manager_kb()
    if BANNER.exists():
        await m.answer_photo(FSInputFile(BANNER), caption=WELCOME, reply_markup=kb)
    else:
        await m.answer(WELCOME, reply_markup=kb)
    if m.from_user.id in ADMIN_IDS:
        warn = []
        if not WEBAPP_URL:
            warn.append("не задан WEBAPP_URL — кнопки приложения не будет")
        if not DATA["orders_chat"]:
            warn.append("не привязан чат заявок — добавьте бота в группу и напишите там /bindchat")
        if warn:
            await m.answer("⚠️ Для админа:\n• " + "\n• ".join(warn))
        await m.answer("Панель управления: /admin")


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>\nID этого чата: <code>{m.chat.id}</code>")


@router.message(F.web_app_data)
async def from_webapp(m: Message, bot: Bot):
    try:
        payload = json.loads(m.web_app_data.data)
        text = str(payload["text"])[:3000]
    except Exception:
        return await m.answer("Не получилось прочитать заявку, попробуйте ещё раз 🙏")
    u = m.from_user
    oid = DATA["next_id"]
    DATA["next_id"] += 1
    o = {"id": oid, "status": "new", "text": text, "profit": str(payload.get("profit", ""))[:200],
         "market": payload.get("market"), "client_id": u.id, "username": u.username, "name": u.full_name,
         "time": datetime.now(TZ).strftime("%d.%m %H:%M"), "history": []}

    await m.answer(
        f"⛩ <b>受付</b> · <b>ЗАЯВКА №{oid} ПРИНЯТА</b>\n{LINE}\n\n{escape(text)}\n\n"
        f"Оператор свяжется с вами в ближайшее время 🎏\n{WAVE}",
        reply_markup=manager_kb())

    targets = [DATA["orders_chat"]] if DATA["orders_chat"] else list(ADMIN_IDS)
    for chat in targets:
        try:
            msg = await bot.send_message(chat, order_card(o), reply_markup=order_kb(o))
            o.setdefault("msgs", []).append([chat, msg.message_id])
        except Exception as e:
            logging.warning("Не смог отправить заявку в %s: %s", chat, e)
    if not o.get("msgs"):
        logging.error("Заявка №%s никуда не доставлена — проверьте ORDERS_CHAT_ID / ADMIN_IDS", oid)
    DATA["orders"][str(oid)] = o
    save()


# ─────────────────────────── команда ─────────────────────────────
@router.callback_query(F.data.startswith("st:"))
async def cb_status(c: CallbackQuery, bot: Bot):
    _, oid, new = c.data.split(":")
    o = DATA["orders"].get(oid)
    if not o:
        return await c.answer("Заявка не найдена (старая или бот обновлялся)", show_alert=True)
    if new not in {"new": {"work", "cancel"}, "work": {"done", "cancel"}}.get(o["status"], set()):
        return await c.answer("Статус уже изменён", show_alert=True)

    op = f"@{c.from_user.username}" if c.from_user.username else c.from_user.full_name
    o["status"] = new
    o.setdefault("history", []).append(f"{datetime.now(TZ).strftime('%H:%M')} {STATUS[new][1].lower()} — {op}")
    if new == "work":
        o["operator"] = op
    save()

    for chat, mid in o.get("msgs", []):
        try:
            await bot.edit_message_text(order_card(o), chat_id=chat, message_id=mid, reply_markup=order_kb(o))
        except Exception as e:
            logging.warning("Не смог обновить карточку: %s", e)
    try:
        await bot.send_message(o["client_id"], CLIENT_MSG[new].format(id=oid, op=escape(op), mgr=DATA["manager"]),
                               reply_markup=manager_kb())
    except Exception as e:
        logging.warning("Клиент %s не получил статус: %s", o["client_id"], e)
    await c.answer(f"{STATUS[new][0]} {STATUS[new][1]}")


@router.message(Command("bindchat"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_bindchat(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return await m.answer("Привязать чат может только админ бота (его ID в ADMIN_IDS).")
    DATA["orders_chat"] = m.chat.id
    save()
    await m.answer(f"⛩ Готово. Все заявки теперь будут приходить сюда.\nID чата: <code>{m.chat.id}</code>\n"
                   "Впишите этот ID в переменную ORDERS_CHAT_ID в Railway, чтобы привязка не слетела.")


def orders_text() -> str:
    active = [o for o in DATA["orders"].values() if o["status"] in ("new", "work")]
    if not active:
        return "Открытых заявок нет 🌊"
    lines = [f"{STATUS[o['status']][0]} №{o['id']} · {o['time']} · {escape(o.get('operator', '—'))} · 💰 {escape(o.get('profit') or '—')}"
             for o in active]
    return "<b>Открытые заявки</b>\n" + "\n".join(lines)


@router.message(Command("orders"))
async def cmd_orders(m: Message):
    if m.from_user.id not in ADMIN_IDS and m.chat.id != DATA["orders_chat"]:
        return
    await m.answer(orders_text())


# ─────────────────────────── админ-панель ────────────────────────
def money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def tiers_text() -> str:
    rows = []
    for a, b in sorted(DATA["tiers"]):
        sign = "+" if b > 0 else ""
        mood = "хуже" if b > 0 else "лучше" if b < 0 else "базовый"
        rows.append(f"от {money(a)} ₽: <b>{sign}{num(b)}%</b> ({mood})")
    return "\n".join(rows)


def admin_text() -> str:
    r = DATA["rates"]
    market = f"{num(r['rub_market'])} ₽" if r["rub_market"] > 0 else "с биржи (авто)"
    chat = DATA["orders_chat"] or "не привязан ⚠️"
    return (
        f"⛩ <b>管理</b> · <b>ПАНЕЛЬ ОПЕРАТОРА</b>\n{LINE}\n\n"
        "<b>Наценка от рыночного курса</b>\n"
        f"🪙 Крипта → наличные: <b>{num(r['crypto'])}%</b>\n"
        f"💵 Наличные ₽ → наличные: <b>{num(r['rub_cash'])}%</b>\n"
        f"💳 ₽ переводом → наличные: <b>{num(r['rub_card'])}%</b>\n"
        f"🔁 Крипта → крипта: <b>{num(r['swap'])}%</b>\n\n"
        "<b>Рубли ⇄ USDT</b>\n"
        f"Покупка (клиент платит): <b>{num(r['usdt_buy'])} ₽</b>\n"
        f"Продажа (клиент получает): <b>{num(r['usdt_sell'])} ₽</b>\n"
        f"Мой рыночный курс: <b>{market}</b>\n\n"
        "<b>Минимальная сумма</b>\n"
        f"🚚 С курьером: <b>{money(r['min_cash'])} ₽</b>\n"
        f"💻 Переводы, банкомат, крипта: <b>{money(r['min_online'])} ₽</b>\n\n"
        "<b>Ступени по сумме</b> (к наценке)\n"
        f"{tiers_text()}\n\n"
        f"Ник оператора: @{DATA['manager']}\n"
        f"Чат заявок: <code>{chat}</code>\n\n"
        "<i>Нажмите кнопку, чтобы изменить значение.</i>"
    )


def admin_kb() -> InlineKeyboardMarkup:
    r = DATA["rates"]
    b = lambda t, k: InlineKeyboardButton(text=t, callback_data=f"ed:{k}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(f"🪙 Крипта {num(r['crypto'])}%", "crypto"), b(f"🔁 Крипта→крипта {num(r['swap'])}%", "swap")],
        [b(f"💵 ₽ нал {num(r['rub_cash'])}%", "rub_cash"), b(f"💳 ₽ перевод {num(r['rub_card'])}%", "rub_card")],
        [b(f"🟢 Покупка {num(r['usdt_buy'])} ₽", "usdt_buy"), b(f"🔴 Продажа {num(r['usdt_sell'])} ₽", "usdt_sell")],
        [b("📈 Мой рыночный курс ₽", "rub_market")],
        [b(f"🚚 Мин. курьер {money(r['min_cash'])}", "min_cash"), b(f"💻 Мин. онлайн {money(r['min_online'])}", "min_online")],
        [b("📶 Ступени по сумме", "tiers")],
        [b("👤 Ник оператора", "manager"), InlineKeyboardButton(text="📋 Открытые заявки", callback_data="adm:orders")],
    ])


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await m.answer(admin_text(), reply_markup=admin_kb())


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer()
    key = c.data[3:]
    await state.set_state(Edit.value)
    await state.update_data(key=key)
    if key == "tiers":
        cur = "\n".join(f"{num(a)} {num(b)}" for a, b in sorted(DATA["tiers"]))
        prompt = ("<b>Ступени по сумме</b>\nКаждая строка: <i>от суммы в рублях</i> и <i>надбавка в %</i>.\n"
                  "Плюс — курс для клиента хуже, минус — лучше, 0 — обычная наценка.\n\n"
                  f"Сейчас:\n<code>{cur}</code>\n\nПришлите новый список в том же виде.")
    elif key == "manager":
        prompt = f"Сейчас: @{DATA['manager']}\nНапишите новый ник оператора (например <code>@volga_master</code>):"
    else:
        title, unit, _ = RATE_FIELDS[key]
        prompt = f"<b>{title}</b>\nСейчас: <b>{num(DATA['rates'][key])} {unit}</b>\n\nНапишите новое значение числом:"
    await c.message.answer(prompt + "\n\n<i>Отмена — /admin</i>")
    await c.answer()


@router.callback_query(F.data == "adm:orders")
async def cb_adm_orders(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer()
    await c.message.answer(orders_text())
    await c.answer()


@router.message(Edit.value, F.chat.type == "private")
async def edit_value(m: Message, state: FSMContext):
    key = (await state.get_data()).get("key")
    txt = (m.text or "").strip()
    if txt.startswith("/"):
        await state.clear()
        return await cmd_admin(m, state)
    if key == "tiers":
        tiers = []
        try:
            for line in txt.replace(";", "\n").splitlines():
                if not line.strip():
                    continue
                a, b = line.replace(":", " ").replace(",", ".").split()[:2]
                a, b = float(a.replace(" ", "")), float(b.replace("%", ""))
                assert a >= 0 and -20 < b < 50
                tiers.append([a, b])
            assert tiers
        except Exception:
            return await m.answer("Не понял список. Пример:\n<code>0 4\n10000 1\n30000 0</code>")
        DATA["tiers"] = sorted(tiers)
    elif key == "manager":
        nick = txt.lstrip("@").split()[0] if txt else ""
        if not nick.replace("_", "").isalnum():
            return await m.answer("Ник может содержать только буквы, цифры и _. Попробуйте ещё раз:")
        DATA["manager"] = nick
    else:
        try:
            val = float(txt.replace(",", ".").replace(" ", ""))
        except ValueError:
            return await m.answer("Нужно число, например <code>3</code> или <code>91.5</code>. Попробуйте ещё раз:")
        unit = RATE_FIELDS[key][1]
        limit = 10_000_000 if key.startswith("min") else 10000
        if (unit == "%" and not 0 <= val < 50) or (unit == "₽" and not 0 <= val < limit):
            return await m.answer("Похоже на опечатку. Проверьте значение и напишите ещё раз:")
        DATA["rates"][key] = val
    save()
    await state.clear()
    note = "" if PUBLIC_URL else "\n<i>Клиенты увидят новый курс после /start.</i>"
    await m.answer("✅ Сохранено" + note, reply_markup=app_kb())
    await m.answer(admin_text(), reply_markup=admin_kb())


@router.message(F.chat.type == "private")
async def fallback(m: Message):
    await m.answer("Нажмите /start, чтобы открыть обменник 🎏")


# ─────────────────────────── курсы для приложения ────────────────
async def rates_handler(request: web.Request) -> web.Response:
    return web.json_response({"rates": {**DATA["rates"], "tiers": DATA["tiers"]}, "manager": DATA["manager"]},
                             headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


async def start_web() -> None:
    app = web.Application()
    app.router.add_get("/rates", rates_handler)
    app.router.add_get("/", lambda r: web.Response(text="Volga Exchange bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Курсы доступны на порту %s: /rates", PORT)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN — возьмите токен у @BotFather")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await start_web()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
