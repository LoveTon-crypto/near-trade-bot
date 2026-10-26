"""Приватный трейд-бот NEAR (этап 1): кошелёк, купить/продать через Ref, позиции.

Бот отвечает только владельцу (OWNER_ID) и только в личке. Ключ на диске
зашифрован; после каждого запуска бот заблокирован до /unlock.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

load_dotenv()

import keystore
from db import DB
from near import Rpc, Signer, RpcError, TxFailed, YOCTO, fmt_near, to_yocto
from ref import Ref, NoPool, WNEAR

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
RPC_URLS = [u.strip() for u in os.getenv(
    "RPC_URLS", "https://rpc.mainnet.fastnear.com,https://free.rpc.fastnear.com,https://rpc.mainnet.near.org").split(",") if u.strip()]
WITHDRAW_ADDRESS = os.getenv("WITHDRAW_ADDRESS", "").strip().lower()
MAX_BUY = to_yocto(os.getenv("MAX_BUY_NEAR", "20"))
GAS_RESERVE = to_yocto(os.getenv("GAS_RESERVE_NEAR", "0.1"))
AUTO_LOCK_MIN = int(os.getenv("AUTO_LOCK_MIN", "0"))

ADDR_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9_\-]+\.)+(?:near|tg)|[0-9a-f]{64})(?![\w.-])")
URL_RES = [re.compile(r"nearly\.trade/([a-z0-9_\-.]+)"), re.compile(r"justhoot\.fun/p/([a-z0-9_\-.]+)"),
           re.compile(r"nearblocks\.io/(?:token|address)/([a-z0-9_\-.]+)")]

db = DB(os.getenv("DB_PATH", "trade.db"))
rpc = Rpc(RPC_URLS)
ref = Ref(rpc)


class State:
    signer: Signer | None = None
    failed: int = 0
    lockout_until: float = 0
    last_activity: float = time.time()
    busy: set = set()


S = State()
MENU = ReplyKeyboardMarkup([["💼 Кошелёк", "📊 Позиции"], ["⚙️ Настройки", "🔒 Заблокировать"]], resize_keyboard=True)


def slippage_bps() -> int:
    return int(db.get("slippage_bps", 500))


def presets() -> list[str]:
    return db.get("buy_presets", ["0.5", "1", "2", "5"])


def nb_tx(h): return f'<a href="https://nearblocks.io/txns/{h}">{h[:8]}…</a>'
def esc(s): return html.escape(str(s))


def fmt_tok(raw: int, dec: int) -> str:
    v = Decimal(raw) / (Decimal(10) ** dec) if dec else Decimal(raw)
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- доступ
async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Все апдейты не от владельца или не из лички молча отбрасываются."""
    u = update.effective_user
    chat = update.effective_chat
    if not u or u.id != OWNER_ID or (chat and chat.type != ChatType.PRIVATE):
        raise ApplicationHandlerStop
    S.last_activity = time.time()


def need_unlock(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not S.signer:
            msg = "🔒 Бот заблокирован. Отправьте /unlock и пароль."
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            else:
                await update.effective_message.reply_text(msg)
            return
        return await fn(update, ctx)
    return wrap


# ---------------------------------------------------------------- старт / разблокировка
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store = keystore.load()
    if not store:
        await update.message.reply_text("Кошелёк не создан. На сервере выполните:\n"
                                        "<code>venv/bin/python keystore.py new</code>", parse_mode=ParseMode.HTML)
        return
    status = "🔓 разблокирован" if S.signer else "🔒 заблокирован — /unlock"
    await update.message.reply_text(
        f"<b>NEAR Trade</b> · {status}\nКошелёк: <code>{esc(store['account_id'])}</code>\n\n"
        "Отправьте адрес токена или ссылку nearly / justhoot / nearblocks, чтобы торговать.",
        parse_mode=ParseMode.HTML, reply_markup=MENU)


async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if S.signer:
        await update.message.reply_text("Уже разблокирован.")
        return
    if time.time() < S.lockout_until:
        await update.message.reply_text(f"Слишком много неверных попыток. Подождите {int(S.lockout_until - time.time()) // 60 + 1} мин.")
        return
    ctx.user_data["await"] = ("password",)
    await update.message.reply_text("Отправьте пароль следующим сообщением. Я сразу удалю его из чата.")


async def try_unlock(update: Update, password: str):
    try:
        await update.message.delete()
    except Exception:
        pass
    store = keystore.load()
    try:
        kp = await asyncio.to_thread(keystore.decrypt, store, password)
    except ValueError as e:
        S.failed += 1
        if S.failed >= 5:
            S.lockout_until, S.failed = time.time() + 15 * 60, 0
        await update.effective_chat.send_message(f"❌ {e}.")
        return
    S.failed = 0
    signer = Signer(rpc, store["account_id"], kp)
    try:
        await signer.check_key()
    except RpcError as e:
        await update.effective_chat.send_message(
            f"⚠️ Пароль верный, но ключ не подходит к аккаунту (или аккаунт ещё не пополнен): {esc(str(e)[:200])}",
            parse_mode=ParseMode.HTML)
        return
    S.signer = signer
    bal = await ref.native_balance(signer.account_id)
    await update.effective_chat.send_message(f"🔓 Разблокирован. Баланс: <b>{fmt_near(bal)} NEAR</b>",
                                             parse_mode=ParseMode.HTML, reply_markup=MENU)


async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    S.signer = None
    ctx.user_data.pop("await", None)
    await update.effective_message.reply_text("🔒 Заблокирован. Ключ удалён из памяти бота.")


async def auto_lock(ctx: ContextTypes.DEFAULT_TYPE):
    if AUTO_LOCK_MIN and S.signer and time.time() - S.last_activity > AUTO_LOCK_MIN * 60 and not S.busy:
        S.signer = None
        await ctx.bot.send_message(OWNER_ID, "🔒 Автоблокировка по неактивности.")


# ---------------------------------------------------------------- кошелёк
@need_unlock
async def show_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = S.signer.account_id
    bal = await ref.native_balance(acc)
    w = WITHDRAW_ADDRESS or "не задан (WITHDRAW_ADDRESS в .env)"
    await update.effective_message.reply_text(
        f"💼 <b>Кошелёк</b>\n<code>{esc(acc)}</code>\n\nБаланс: <b>{fmt_near(bal)} NEAR</b>\n"
        f"Резерв на газ: {fmt_near(GAS_RESERVE)} NEAR\nЛимит на одну покупку: {fmt_near(MAX_BUY)} NEAR\n\n"
        f"Вывод только на: <code>{esc(w)}</code>\nКоманда: /withdraw 5 или /withdraw all",
        parse_mode=ParseMode.HTML)


@need_unlock
async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not WITHDRAW_ADDRESS:
        await update.message.reply_text("Адрес для вывода не задан. Добавьте WITHDRAW_ADDRESS в .env на сервере.")
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /withdraw 5 или /withdraw all")
        return
    bal = await ref.native_balance(S.signer.account_id)
    if ctx.args[0].lower() == "all":
        amt = bal - GAS_RESERVE // 2
    else:
        try:
            amt = to_yocto(ctx.args[0].replace(",", "."))
        except InvalidOperation:
            await update.message.reply_text("Неверная сумма.")
            return
    if amt <= 0 or amt > bal - 10 ** 21:
        await update.message.reply_text(f"Недостаточно средств. Баланс {fmt_near(bal)} NEAR.")
        return
    ctx.user_data["withdraw"] = amt
    await update.message.reply_text(
        f"Вывести <b>{fmt_near(amt)} NEAR</b> на <code>{esc(WITHDRAW_ADDRESS)}</code>?", parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("✅ Подтвердить", callback_data="wd:ok"), B("Отмена", callback_data="wd:no")]]))


@need_unlock
async def on_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    amt = ctx.user_data.pop("withdraw", None)
    await q.answer()
    if q.data != "wd:ok" or not amt:
        await q.edit_message_text("Отменено.")
        return
    await q.edit_message_text("⏳ Отправляю…")
    try:
        res = await ref.withdraw_near(S.signer, WITHDRAW_ADDRESS, amt)
        await q.edit_message_text(f"✅ Отправлено {fmt_near(amt)} NEAR · {nb_tx(res['_hash'])}", parse_mode=ParseMode.HTML)
    except (TxFailed, RpcError) as e:
        await q.edit_message_text(f"❌ Ошибка: {esc(str(e)[:300])}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- карточка токена
async def token_card(token: str) -> tuple[str, KB]:
    meta = await ref.ft_metadata(token)
    sym, dec = str(meta.get("symbol") or "?")[:20], int(meta.get("decimals") or 0)
    tid = db.token_id(token, sym, dec)
    lines = [f"<b>${esc(sym)}</b> · {esc(str(meta.get('name') or '')[:40])}", f"<code>{esc(token)}</code>", ""]
    try:
        pool_id, wn = await ref.best_pool(token)
        p = await rpc.view("v2.ref-finance.near", "get_pool", {"pool_id": pool_id})
        toks, amts = p["token_account_ids"], [int(a) for a in p["amounts"]]
        tr = amts[1 - toks.index(WNEAR)]
        price = (wn / YOCTO) / (tr / 10 ** dec) if tr else 0
        lines.append(f"💲 Цена: <b>{price:.10g} NEAR</b>")
        lines.append(f"💧 Ликвидность: {fmt_near(wn * 2, 1)} NEAR · пул Ref #{pool_id}")
    except NoPool:
        lines.append("⚠️ Пула с NEAR на Ref нет. Если токен ещё на мемпаде — покупка там будет на следующем этапе.")
    if S.signer:
        bal = await ref.ft_balance(token, S.signer.account_id)
        if bal:
            line = f"👛 У вас: <b>{fmt_tok(bal, dec)} {esc(sym)}</b>"
            try:
                pid, _ = await ref.best_pool(token)
                val = await ref.quote(pid, token, bal, WNEAR)
                spent, got = db.totals(token)
                line += f" ≈ {fmt_near(val)} NEAR"
                if spent:
                    pnl = got + val - spent
                    line += f"\n📈 PnL: <b>{'+' if pnl >= 0 else '−'}{fmt_near(abs(pnl))} NEAR</b> ({pnl / spent * 100:+.1f}%)"
            except NoPool:
                pass
            lines.append(line)
    lines.append(f"\n⚙️ Слиппедж: {slippage_bps() / 100:g}%")
    lines.append(f'<a href="https://nearblocks.io/token/{esc(token)}">Nearblocks</a> · '
                 f'<a href="https://dexscreener.com/near/{esc(token)}">DexScreener</a>')
    pr = presets()
    kb = KB([
        [B(f"🟢 {a} NEAR", callback_data=f"b:{a}:{tid}") for a in pr[:4]],
        [B("🟢 ✏️ X NEAR", callback_data=f"bx:{tid}"), B("🔄 Обновить", callback_data=f"c:{tid}")],
        [B("🔴 25%", callback_data=f"s:25:{tid}"), B("🔴 50%", callback_data=f"s:50:{tid}"),
         B("🔴 100%", callback_data=f"s:100:{tid}"), B("🔴 ✏️ X%", callback_data=f"sx:{tid}")],
    ])
    return "\n".join(lines), kb


def find_token(text: str) -> str | None:
    t = text.strip().lower()
    for r in URL_RES:
        m = r.search(t)
        if m:
            return m.group(1)
    m = ADDR_RE.search(t)
    return m.group(1) if m else None


async def send_card(chat, token: str, edit_msg=None):
    try:
        text, kb = await token_card(token)
    except RpcError as e:
        text, kb = f"Не удалось прочитать токен: {esc(str(e)[:200])}", None
    if edit_msg:
        try:
            await edit_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
    else:
        await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


# ---------------------------------------------------------------- торговля
async def do_buy(chat, token: str, amount: int):
    if amount > MAX_BUY:
        await chat.send_message(f"Больше лимита {fmt_near(MAX_BUY)} NEAR (MAX_BUY_NEAR в .env).")
        return
    bal = await ref.native_balance(S.signer.account_id)
    if amount > bal - GAS_RESERVE:
        await chat.send_message(f"Недостаточно NEAR: баланс {fmt_near(bal)}, резерв на газ {fmt_near(GAS_RESERVE)}.")
        return
    key = ("buy", token)
    if key in S.busy:
        await chat.send_message("Сделка по этому токену уже выполняется.")
        return
    S.busy.add(key)
    msg = await chat.send_message(f"⏳ Покупаю на {fmt_near(amount)} NEAR…")
    try:
        r = await ref.buy(S.signer, token, amount, slippage_bps())
        db.add_trade(token, "buy", r["got"], amount, r["hash"])
        meta = await ref.ft_metadata(token)
        await msg.edit_text(f"✅ Куплено <b>{fmt_tok(r['got'], int(meta.get('decimals') or 0))} "
                            f"{esc(meta.get('symbol') or '')}</b> за {fmt_near(amount)} NEAR · {nb_tx(r['hash'])}",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await send_card(chat, token)
    except NoPool:
        await msg.edit_text("❌ Нет пула с NEAR на Ref для этого токена.")
    except (TxFailed, RpcError) as e:
        h = f" · {nb_tx(e.tx_hash)}" if isinstance(e, TxFailed) and e.tx_hash else ""
        await msg.edit_text(f"❌ Покупка не прошла: {esc(str(e)[:300])}{h}", parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    finally:
        S.busy.discard(key)


async def do_sell(chat, token: str, pct: Decimal):
    bal = await ref.ft_balance(token, S.signer.account_id)
    amount = bal if pct >= 100 else int(Decimal(bal) * pct / 100)
    if amount <= 0:
        await chat.send_message("Нечего продавать.")
        return
    key = ("sell", token)
    if key in S.busy:
        await chat.send_message("Сделка по этому токену уже выполняется.")
        return
    S.busy.add(key)
    msg = await chat.send_message(f"⏳ Продаю {pct:g}%…")
    try:
        r = await ref.sell(S.signer, token, amount, slippage_bps())
        db.add_trade(token, "sell", amount, r["got"], r["hash"])
        await msg.edit_text(f"✅ Продано {pct:g}% → <b>{fmt_near(r['got'])} NEAR</b> · {nb_tx(r['hash'])}",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await send_card(chat, token)
    except NoPool:
        await msg.edit_text("❌ Нет пула с NEAR на Ref для этого токена.")
    except (TxFailed, RpcError) as e:
        h = f" · {nb_tx(e.tx_hash)}" if isinstance(e, TxFailed) and e.tx_hash else ""
        await msg.edit_text(f"❌ Продажа не прошла: {esc(str(e)[:300])}{h}", parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    finally:
        S.busy.discard(key)


def _token_from_cb(tid: str) -> str | None:
    row = db.token_by_id(int(tid)) if tid.isdigit() else None
    return row[0] if row else None


async def on_trade_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    token = _token_from_cb(parts[-1])
    if not token:
        await q.answer("Токен не найден", show_alert=True)
        return
    if parts[0] == "c":
        await q.answer("Обновлено")
        await send_card(q.message.chat, token, edit_msg=q.message)
        return
    if not S.signer:
        await q.answer("🔒 Бот заблокирован. /unlock", show_alert=True)
        return
    await q.answer()
    chat = q.message.chat
    if parts[0] == "b":
        await do_buy(chat, token, to_yocto(parts[1]))
    elif parts[0] == "s":
        await do_sell(chat, token, Decimal(parts[1]))
    elif parts[0] == "bx":
        ctx.user_data["await"] = ("buy", token)
        await chat.send_message("Сколько NEAR потратить? Например: 1.5")
    elif parts[0] == "sx":
        ctx.user_data["await"] = ("sell", token)
        await chat.send_message("Какой % продать? Например: 30")


# ---------------------------------------------------------------- позиции
@need_unlock
async def show_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    acc = S.signer.account_id
    rows, kb = [], []
    for token in db.traded_tokens()[:15]:
        bal = await ref.ft_balance(token, acc)
        if not bal:
            continue
        tid = db.token_id(token)
        _, sym, dec = db.token_by_id(tid)
        spent, got = db.totals(token)
        try:
            pid, _ = await ref.best_pool(token)
            val = await ref.quote(pid, token, bal, WNEAR)
        except (NoPool, RpcError):
            val = 0
        pnl = got + val - spent
        pct = f" ({pnl / spent * 100:+.1f}%)" if spent else ""
        rows.append(f"<b>${esc(sym)}</b> · {fmt_tok(bal, dec)} ≈ {fmt_near(val)} NEAR\n"
                    f"   PnL {'+' if pnl >= 0 else '−'}{fmt_near(abs(pnl))} NEAR{pct}")
        kb.append([B(f"${sym}", callback_data=f"c:{tid}")])
    if not rows:
        await update.effective_message.reply_text("Открытых позиций нет.")
        return
    await update.effective_message.reply_text("📊 <b>Позиции</b>\n\n" + "\n\n".join(rows), parse_mode=ParseMode.HTML,
                                              reply_markup=KB(kb))


# ---------------------------------------------------------------- настройки
def settings_kb():
    cur = slippage_bps()
    row = [B(("✅ " if cur == v else "") + f"{v / 100:g}%", callback_data=f"sl:{v}") for v in (100, 300, 500, 1000, 2000, 3000)]
    return KB([row[:3], row[3:], [B("✏️ Пресеты покупки: " + " / ".join(presets()), callback_data="pr")]])


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⚙️ <b>Настройки</b>\nСлиппедж:", parse_mode=ParseMode.HTML,
                                              reply_markup=settings_kb())


async def on_settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data.startswith("sl:"):
        db.set("slippage_bps", int(q.data[3:]))
        await q.answer("Сохранено")
        await q.edit_message_reply_markup(settings_kb())
    elif q.data == "pr":
        await q.answer()
        ctx.user_data["await"] = ("presets",)
        await q.message.chat.send_message("Отправьте 4 суммы в NEAR через пробел, например: 0.5 1 2 5")


# ---------------------------------------------------------------- текст
def parse_num(s: str) -> Decimal | None:
    try:
        v = Decimal(s.strip().replace(",", ".").rstrip("%"))
        return v if v > 0 else None
    except InvalidOperation:
        return None


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    pending = ctx.user_data.pop("await", None)
    if pending:
        kind = pending[0]
        if kind == "password":
            if text in ("💼 Кошелёк", "📊 Позиции", "⚙️ Настройки", "🔒 Заблокировать"):
                await update.message.reply_text("Разблокировка отменена.")
                return
            await try_unlock(update, text)
            return
        if kind == "presets":
            vals = [parse_num(x) for x in text.split()]
            if len(vals) != 4 or not all(vals):
                await update.message.reply_text("Нужно ровно 4 положительных числа.")
                return
            db.set("buy_presets", [f"{v:g}" for v in vals])
            await update.message.reply_text("✅ Пресеты сохранены.", reply_markup=settings_kb())
            return
        if not S.signer:
            await update.message.reply_text("🔒 Бот заблокирован. /unlock")
            return
        v = parse_num(text)
        if not v:
            await update.message.reply_text("Неверное число, отменено.")
            return
        if kind == "buy":
            await do_buy(update.effective_chat, pending[1], to_yocto(v))
        elif kind == "sell":
            if v > 100:
                await update.message.reply_text("Максимум 100%.")
                return
            await do_sell(update.effective_chat, pending[1], v)
        return
    menu = {"💼 Кошелёк": show_wallet, "📊 Позиции": show_positions, "⚙️ Настройки": show_settings,
            "🔒 Заблокировать": cmd_lock}
    if text in menu:
        await menu[text](update, ctx)
        return
    token = find_token(text)
    if token:
        await send_card(update.effective_chat, token)
    else:
        await update.message.reply_text("Отправьте адрес токена (например, blackdragon.tkn.near) или ссылку на него.")


# ---------------------------------------------------------------- запуск
async def post_init(app: Application):
    try:
        await ref.refresh_index()
    except Exception as e:
        log.warning("Ref index load failed (will retry on demand): %s", e)
    await app.bot.set_my_commands([("start", "Главное меню"), ("unlock", "Разблокировать"), ("lock", "Заблокировать"),
                                   ("withdraw", "Вывести NEAR")])
    try:
        await app.bot.send_message(OWNER_ID, "🤖 Бот запущен и заблокирован. Отправьте /unlock.")
    except Exception:
        pass


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    # в лог пишем только тип и текст ошибки, без содержимого сообщений
    log.error("handler error: %s: %s", type(ctx.error).__name__, str(ctx.error)[:300])
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("⚠️ Внутренняя ошибка, попробуйте ещё раз.")
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CallbackQueryHandler(on_withdraw, pattern=r"^wd:"))
    app.add_handler(CallbackQueryHandler(on_trade_cb, pattern=r"^(b|bx|s|sx|c):"))
    app.add_handler(CallbackQueryHandler(on_settings_cb, pattern=r"^(sl:|pr$)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    if AUTO_LOCK_MIN:
        app.job_queue.run_repeating(auto_lock, interval=60, first=60)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
