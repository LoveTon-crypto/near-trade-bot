"""Трейд-бот NEAR: кошельки (создание, импорт, перевод), купить/продать через Ref, позиции.

Работает для всех, только в личке. У каждого пользователя свои кошельки;
ключи на диске зашифрованы MASTER_KEY из .env (см. wallets.py).
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import (CopyTextButton, InlineKeyboardButton as B, InlineKeyboardMarkup as KB, ReplyKeyboardMarkup,
                      Update)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

load_dotenv()

from db import DB
from near import KeyPair, Rpc, Signer, RpcError, TxFailed, YOCTO, fmt_near, to_yocto
from ref import Ref, NoPool, WNEAR
import wallets
from wallets import Vault, WalletError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
RPC_URLS = [u.strip() for u in os.getenv(
    "RPC_URLS", "https://rpc.mainnet.fastnear.com,https://free.rpc.fastnear.com,https://rpc.mainnet.near.org").split(",") if u.strip()]
MAX_BUY = to_yocto(os.getenv("MAX_BUY_NEAR", "100"))
GAS_RESERVE = to_yocto(os.getenv("GAS_RESERVE_NEAR", "0.1"))
MAX_WALLETS = int(os.getenv("MAX_WALLETS", "10"))
SECRET_TTL = int(os.getenv("SECRET_MESSAGE_TTL", "120"))  # сек, через сколько удалять сообщения с ключами

ADDR_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9_\-]+\.)+(?:near|tg)|[0-9a-f]{64})(?![\w.-])")
URL_RES = [re.compile(r"nearly\.trade/([a-z0-9_\-.]+)"), re.compile(r"justhoot\.fun/p/([a-z0-9_\-.]+)"),
           re.compile(r"nearblocks\.io/(?:token|address)/([a-z0-9_\-.]+)")]

db = DB(os.getenv("DB_PATH", "trade.db"))
rpc = Rpc(RPC_URLS)
ref = Ref(rpc)
vault = Vault(wallets.load_master_key())

SIGNERS: dict[int, Signer] = {}  # wallet id → Signer (один nonce-замок на кошелёк)
BUSY: set = set()
MENU = ReplyKeyboardMarkup([["💼 Кошелёк", "📊 Позиции"], ["⚙️ Настройки"]], resize_keyboard=True)
_price = {"usd": 0.0, "ts": 0.0}


def slippage_bps(uid: int) -> int:
    return int(db.get(f"u{uid}:slippage_bps", 500))


def presets(uid: int) -> list[str]:
    return db.get(f"u{uid}:buy_presets", ["0.5", "1", "2", "5"])


def signer_for(uid: int, wid: int) -> Signer | None:
    row = db.wallet(uid, wid)
    if not row:
        return None
    s = SIGNERS.get(wid)
    if not s:
        kp, _ = vault.open(uid, row[2], row[3])
        s = SIGNERS[wid] = Signer(rpc, row[2], kp)
    return s


def active_signer(uid: int) -> Signer | None:
    wid = db.active_wallet(uid)
    return signer_for(uid, wid) if wid else None


async def near_usd() -> float:
    if time.time() - _price["ts"] > 60:
        try:
            r = await rpc.http.get("https://api.coingecko.com/api/v3/simple/price",
                                   params={"ids": "near", "vs_currencies": "usd"})
            _price.update(usd=float(r.json()["near"]["usd"]), ts=time.time())
        except Exception:
            _price["ts"] = time.time() - 30  # повторить через 30 сек
    return _price["usd"]


def nb_tx(h): return f'<a href="https://nearblocks.io/txns/{h}">{h[:8]}…</a>'
def esc(s): return html.escape(str(s))


def fmt_tok(raw: int, dec: int) -> str:
    v = Decimal(raw) / (Decimal(10) ** dec) if dec else Decimal(raw)
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")



def short(acc: str) -> str:
    return acc if len(acc) <= 20 else f"{acc[:6]}…{acc[-6:]}"


async def balance_line(acc: str) -> str:
    bal = await ref.native_balance(acc)
    usd = await near_usd()
    return f"<b>{fmt_near(bal)} Ⓝ</b>" + (f" (${bal / YOCTO * usd:,.2f})" if usd else "")


# ---------------------------------------------------------------- доступ
async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Только личные чаты. Каждый пользователь видит только свои кошельки."""
    chat = update.effective_chat
    if not update.effective_user or (chat and chat.type != ChatType.PRIVATE):
        raise ApplicationHandlerStop
    db.ensure_user(update.effective_user.id)


async def need_wallet(update: Update) -> Signer | None:
    s = active_signer(update.effective_user.id)
    if not s:
        msg = "Сначала создайте или импортируйте кошелёк: 💼 Кошелёк"
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.effective_message.reply_text(msg, reply_markup=MENU)
    return s


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.user_data.pop("await", None)
    s = active_signer(uid)
    if not s:
        await update.message.reply_text(
            "<b>NEAR Trade</b> — покупка и продажа токенов NEAR прямо в Telegram.\n\n"
            "Для начала создайте новый кошелёк или импортируйте свой по сид-фразе.",
            parse_mode=ParseMode.HTML, reply_markup=MENU)
        await update.message.reply_text("👛 Кошелёк:", reply_markup=KB([[B("➕ Создать", callback_data="w:new"),
                                                                          B("📥 Импорт", callback_data="w:imp")]]))
        return
    await update.message.reply_text(
        f"<b>NEAR Trade</b>\nКошелёк: <code>{esc(s.account_id)}</code>\nБаланс: {await balance_line(s.account_id)}\n\n"
        "Отправьте адрес токена или ссылку nearly / justhoot / nearblocks, чтобы торговать.",
        parse_mode=ParseMode.HTML, reply_markup=MENU, disable_web_page_preview=True)


# ---------------------------------------------------------------- кошельки: список
async def wallets_view(uid: int) -> tuple[str, KB]:
    rows = db.wallets(uid)
    act = db.active_wallet(uid)
    kb = []
    for wid, name, acc, _ in rows:
        kb.append([B(("✅ " if wid == act else "") + f"{name} · {short(acc)}", callback_data=f"w:v:{wid}")])
    kb.append([B("➕ Создать", callback_data="w:new"), B("📥 Импорт", callback_data="w:imp")])
    if not rows:
        return "👛 <b>Кошельки</b>\n\nКошельков пока нет. Создайте новый или импортируйте свой по сид-фразе.", KB(kb)
    return f"👛 <b>Кошельки</b> ({len(rows)}/{MAX_WALLETS})\n✅ — основной, с него идут сделки.", KB(kb)


async def show_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = await wallets_view(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def wallet_detail(uid: int, wid: int) -> tuple[str, KB] | None:
    row = db.wallet(uid, wid)
    if not row:
        return None
    _, name, acc, _, has_phrase = row
    is_active = db.active_wallet(uid) == wid
    text = (f"…{esc(acc[-7:])} 👛 <b>{esc(name)}</b>{' · ✅ основной' if is_active else ''}\n\n"
            f"📭 <b>Адрес</b>\n<code>{esc(acc)}</code>\n └ {await balance_line(acc)}")
    kb = KB([
        [B("📋 Скопировать адрес", copy_text=CopyTextButton(acc))],
        [B("💰 Пополнить", callback_data=f"w:top:{wid}")],
        [B("✅ Основной" if is_active else "⭐ Сделать основным", callback_data=f"w:act:{wid}"),
         B("Перевод ↗️", callback_data=f"w:tr:{wid}")],
        [B("Изменить имя ✍️", callback_data=f"w:ren:{wid}"),
         B("Сид-фраза 🌱" if has_phrase else "Приватный ключ 🔑", callback_data=f"w:seed:{wid}")],
        [B("⬅️ Назад", callback_data="w:list"), B("Удалить 🗑", callback_data=f"w:del:{wid}")],
    ])
    return text, kb


async def edit_or_send(q, text, kb):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await q.message.chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)


def delete_later(ctx: ContextTypes.DEFAULT_TYPE, msg, seconds: int = SECRET_TTL):
    async def _del(_):
        try:
            await msg.delete()
        except Exception:
            pass
    ctx.job_queue.run_once(_del, seconds)


async def send_secret(ctx, chat, title: str, kp: KeyPair, phrase: str | None):
    parts = [title, ""]
    if phrase:
        parts.append(f"🌱 <b>Сид-фраза</b> (12 слов):\n<tg-spoiler><code>{esc(phrase)}</code></tg-spoiler>\n")
    parts.append(f"🔑 <b>Приватный ключ</b>:\n<tg-spoiler><code>{esc(kp.secret_string())}</code></tg-spoiler>\n")
    parts.append("Запишите и храните офлайн. Никому не отправляйте — с ней можно забрать все средства.\n"
                 f"<i>Сообщение удалится через {SECRET_TTL // 60} мин.</i>")
    msg = await chat.send_message("\n".join(parts), parse_mode=ParseMode.HTML, protect_content=True)
    delete_later(ctx, msg)


# ---------------------------------------------------------------- кошельки: создание и импорт
async def save_wallet(uid: int, kp: KeyPair, phrase: str | None, account: str | None = None) -> int:
    account = account or kp.implicit_account
    if db.has_account(uid, account):
        raise WalletError("этот кошелёк уже добавлен")
    n = len(db.wallets(uid)) + 1
    wid = db.add_wallet(uid, f"Кошелёк {n}", account, vault.seal(uid, account, kp, phrase), bool(phrase))
    return wid


async def create_wallet(q, ctx):
    uid = q.from_user.id
    if len(db.wallets(uid)) >= MAX_WALLETS:
        await q.answer(f"Максимум {MAX_WALLETS} кошельков", show_alert=True)
        return
    await q.answer()
    phrase = wallets.new_phrase()
    kp = wallets.keypair_from_phrase(phrase)
    wid = await save_wallet(uid, kp, phrase)
    await send_secret(ctx, q.message.chat, "✅ <b>Кошелёк создан.</b> Сохраните сид-фразу прямо сейчас:", kp, phrase)
    text, kb = await wallet_detail(uid, wid)
    await q.message.chat.send_message(
        text + "\n\nПополните адрес NEAR, чтобы кошелёк активировался в сети. Сид-фраза подходит для Meteor, "
               "MyNearWallet и других NEAR-кошельков.",
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def accounts_for_key(public_key: str) -> list[str]:
    """Именованные аккаунты (имя.near), к которым привязан ключ — по индексу FastNear."""
    try:
        r = await rpc.http.get(f"https://api.fastnear.com/v0/public_key/{public_key}")
        ids = r.json().get("account_ids") or []
        return [a for a in ids if isinstance(a, str) and wallets.ACCOUNT_RE.match(a)][:5]
    except Exception:
        return []


async def import_secret(update: Update, ctx, text: str):
    uid = update.effective_user.id
    try:
        await update.message.delete()  # сид-фразе не место в истории чата
    except Exception:
        pass
    chat = update.effective_chat
    try:
        kp, phrase = wallets.parse_secret(text)
    except WalletError as e:
        await chat.send_message(f"❌ {e}. Импорт отменён, попробуйте ещё раз: 💼 Кошелёк → 📥 Импорт")
        return
    named = [a for a in await accounts_for_key(kp.public_key) if not db.has_account(uid, a)]
    if named:
        ctx.user_data["import_pick"] = (kp, phrase, named)
        kb = [[B(a, callback_data=f"w:pick:{i}")] for i, a in enumerate(named)]
        kb.append([B(f"{short(kp.implicit_account)} (implicit)", callback_data="w:pick:-1")])
        await chat.send_message("Ключ подходит к этим аккаунтам. Какой добавить?", reply_markup=KB(kb))
        return
    await finish_import(uid, ctx, chat, kp, phrase, None)


async def finish_import(uid, ctx, chat, kp: KeyPair, phrase, account: str | None):
    if account:
        try:
            ak = await rpc.access_key(account, kp.public_key)
            if ak.get("permission") != "FullAccess":
                raise RpcError("ключ без полного доступа")
        except RpcError as e:
            await chat.send_message(f"❌ Ключ не подходит к {esc(account)}: {esc(str(e)[:150])}", parse_mode=ParseMode.HTML)
            return
    if len(db.wallets(uid)) >= MAX_WALLETS:
        await chat.send_message(f"Максимум {MAX_WALLETS} кошельков.")
        return
    try:
        wid = await save_wallet(uid, kp, phrase, account)
    except WalletError as e:
        await chat.send_message(f"❌ {e}.")
        return
    text, kb = await wallet_detail(uid, wid)
    await chat.send_message("✅ <b>Кошелёк импортирован.</b>\n\n" + text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- кошельки: кнопки
async def on_wallet_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    act = parts[1]
    wid = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else None
    chat = q.message.chat

    if act == "list":
        await q.answer()
        text, kb = await wallets_view(uid)
        await edit_or_send(q, text, kb)
        return
    if act == "new":
        await create_wallet(q, ctx)
        return
    if act == "imp":
        if len(db.wallets(uid)) >= MAX_WALLETS:
            await q.answer(f"Максимум {MAX_WALLETS} кошельков", show_alert=True)
            return
        await q.answer()
        ctx.user_data["await"] = ("import",)
        await chat.send_message("Отправьте сид-фразу (12 или 24 слова) или приватный ключ <code>ed25519:…</code>.\n"
                                "Сообщение я сразу удалю из чата.", parse_mode=ParseMode.HTML)
        return
    if act == "pick":
        pending = ctx.user_data.pop("import_pick", None)
        await q.answer()
        if not pending:
            await edit_or_send(q, "Импорт устарел, начните заново.", None)
            return
        kp, phrase, named = pending
        account = named[wid] if wid is not None and 0 <= wid < len(named) else None
        await q.edit_message_reply_markup(None)
        await finish_import(uid, ctx, chat, kp, phrase, account)
        return

    row = db.wallet(uid, wid) if wid else None
    if not row:
        await q.answer("Кошелёк не найден", show_alert=True)
        return
    _, name, acc, _, has_phrase = row

    if act == "v":
        await q.answer()
        await edit_or_send(q, *await wallet_detail(uid, wid))
    elif act == "act":
        db.set_active(uid, wid)
        await q.answer("Теперь сделки идут с этого кошелька")
        await edit_or_send(q, *await wallet_detail(uid, wid))
    elif act == "top":
        await q.answer()
        await chat.send_message(
            f"💰 <b>Пополнение</b>\n\nОтправьте NEAR на адрес:\n<code>{esc(acc)}</code>\n\n"
            "С биржи (Binance, OKX, Bybit…) выбирайте сеть <b>NEAR</b>. Новый кошелёк появится в сети после "
            "первого пополнения — для этого хватит 0.1 NEAR.",
            parse_mode=ParseMode.HTML, reply_markup=KB([[B("📋 Скопировать адрес", copy_text=CopyTextButton(acc))]]))
    elif act == "ren":
        await q.answer()
        ctx.user_data["await"] = ("rename", wid)
        await chat.send_message(f"Новое имя для «{esc(name)}» (до 24 символов):", parse_mode=ParseMode.HTML)
    elif act == "seed":
        await q.answer()
        what = "сид-фразу" if has_phrase else "приватный ключ"
        await chat.send_message(
            f"⚠️ Показать {what} кошелька «{esc(name)}»?\n\nКто её видит — может забрать все средства. "
            "Убедитесь, что рядом никого нет и экран не записывается.", parse_mode=ParseMode.HTML,
            reply_markup=KB([[B("👁 Показать", callback_data=f"w:seedok:{wid}"), B("Отмена", callback_data="w:x:0")]]))
    elif act == "seedok":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass
        kp, phrase = vault.open(uid, acc, row[3])
        await send_secret(ctx, chat, f"👛 <b>{esc(name)}</b> · <code>{esc(short(acc))}</code>", kp, phrase)
    elif act == "del":
        await q.answer()
        bal = await ref.native_balance(acc)
        warn = f"\n\nНа нём <b>{fmt_near(bal)} NEAR</b>." if bal else ""
        await chat.send_message(
            f"🗑 Удалить кошелёк «{esc(name)}» из бота?{warn}\n\nСредства останутся в сети, но доступ к ним будет только "
            "по сид-фразе или ключу. Сохранили их?", parse_mode=ParseMode.HTML,
            reply_markup=KB([[B("Да, удалить", callback_data=f"w:delok:{wid}"), B("Отмена", callback_data="w:x:0")]]))
    elif act == "delok":
        db.delete_wallet(uid, wid)
        SIGNERS.pop(wid, None)
        await q.answer("Удалён")
        text, kb = await wallets_view(uid)
        await edit_or_send(q, text, kb)
    elif act == "tr":
        await q.answer()
        ctx.user_data["await"] = ("tr_to", wid)
        await chat.send_message(f"↗️ Перевод NEAR с «{esc(name)}».\nОтправьте адрес получателя (имя.near или 64 символа):",
                                parse_mode=ParseMode.HTML)


async def on_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Отменено")
    try:
        await q.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------- перевод
async def transfer_to(update: Update, ctx, wid: int, text: str):
    to = text.strip().lower()
    if not wallets.ACCOUNT_RE.match(to):
        await update.message.reply_text("❌ Неверный адрес. Перевод отменён.")
        return
    if len(to) != 64 and not await rpc.account(to):
        await update.message.reply_text(f"❌ Аккаунта {to} не существует. Перевод отменён.")
        return
    s = signer_for(update.effective_user.id, wid)
    if not s:
        return
    if to == s.account_id:
        await update.message.reply_text("❌ Это тот же кошелёк.")
        return
    bal = await ref.native_balance(s.account_id)
    ctx.user_data["await"] = ("tr_amt", wid, to)
    await update.message.reply_text(f"Сколько NEAR отправить? Доступно: <b>{fmt_near(bal)}</b>\n"
                                    "Число, например <code>1.5</code>, или <code>all</code> — всё.",
                                    parse_mode=ParseMode.HTML)


async def transfer_amount(update: Update, ctx, wid: int, to: str, text: str):
    s = signer_for(update.effective_user.id, wid)
    if not s:
        return
    bal = await ref.native_balance(s.account_id)
    fee = YOCTO // 100  # 0.01 NEAR на газ
    if text.strip().lower() in ("all", "max", "всё", "все"):
        amt = bal - fee
    else:
        v = parse_num(text)
        amt = to_yocto(v) if v else 0
    if amt <= 0 or amt > bal - fee:
        await update.message.reply_text(f"❌ Неверная сумма или не хватает на газ. Доступно {fmt_near(bal)} NEAR.")
        return
    ctx.user_data["transfer"] = (wid, to, amt)
    await update.message.reply_text(
        f"Отправить <b>{fmt_near(amt)} NEAR</b>\nс <code>{esc(short(s.account_id))}</code>\nна <code>{esc(to)}</code>?",
        parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("✅ Отправить", callback_data="tr:ok"), B("Отмена", callback_data="tr:no")]]))


async def on_transfer_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pending = ctx.user_data.pop("transfer", None)
    await q.answer()
    if q.data != "tr:ok" or not pending:
        await q.edit_message_text("Перевод отменён.")
        return
    wid, to, amt = pending
    s = signer_for(q.from_user.id, wid)
    if not s:
        await q.edit_message_text("Кошелёк не найден.")
        return
    await q.edit_message_text("⏳ Отправляю…")
    try:
        res = await ref.withdraw_near(s, to, amt)
        await q.edit_message_text(f"✅ Отправлено {fmt_near(amt)} NEAR на <code>{esc(to)}</code> · {nb_tx(res['_hash'])}",
                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except (TxFailed, RpcError) as e:
        await q.edit_message_text(f"❌ Ошибка: {esc(str(e)[:300])}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- карточка токена
async def token_card(uid: int, token: str) -> tuple[str, KB]:
    meta = await ref.ft_metadata(token)
    sym, dec = str(meta.get("symbol") or "?")[:20], int(meta.get("decimals") or 0)
    tid = db.token_id(token, sym, dec)
    lines = [f"<b>${esc(sym)}</b> · {esc(str(meta.get('name') or '')[:40])}", f"<code>{esc(token)}</code>", ""]
    route = None
    try:
        route = await ref.route(token)
        probe = YOCTO // 10  # цена по котировке 0.1 NEAR (с учётом комиссии пула)
        out = await ref.quote_route(route, WNEAR, probe, token)
        price = (probe / YOCTO) / (out / 10 ** dec) if out else 0
        lines.append(f"💲 Цена: <b>≈{price:.6g} NEAR</b>")
        lines.append(f"💧 Ликвидность: {fmt_near(route.wnear * 2, 1)} NEAR · {esc(route.label)}")
    except NoPool:
        lines.append("⚠️ Пула с NEAR нет ни на Ref, ни на Ref DCL. Токены JustHoot до миграции пока не поддерживаются.")
    s = active_signer(uid)
    if s:
        bal = await ref.ft_balance(token, s.account_id)
        if bal:
            line = f"👛 У вас: <b>{fmt_tok(bal, dec)} {esc(sym)}</b>"
            try:
                if not route:
                    raise NoPool(token)
                val = await ref.quote_route(route, token, bal, WNEAR)
                spent, got = db.totals(s.account_id, token)
                line += f" ≈ {fmt_near(val)} NEAR"
                if spent:
                    pnl = got + val - spent
                    line += f"\n📈 PnL: <b>{'+' if pnl >= 0 else '−'}{fmt_near(abs(pnl))} NEAR</b> ({pnl / spent * 100:+.1f}%)"
            except (NoPool, RpcError):
                pass
            lines.append(line)
        lines.append(f"\n👛 {esc(short(s.account_id))} · баланс {fmt_near(await ref.native_balance(s.account_id))} NEAR")
    lines.append(f"⚙️ Слиппедж: {slippage_bps(uid) / 100:g}%")
    lines.append(f'<a href="https://nearblocks.io/token/{esc(token)}">Nearblocks</a> · '
                 f'<a href="https://dexscreener.com/near/{esc(token)}">DexScreener</a>')
    pr = presets(uid)
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


async def send_card(uid: int, chat, token: str, edit_msg=None):
    try:
        text, kb = await token_card(uid, token)
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
async def do_buy(uid: int, s: Signer, chat, token: str, amount: int):
    if amount > MAX_BUY:
        await chat.send_message(f"Больше лимита {fmt_near(MAX_BUY)} NEAR на одну покупку.")
        return
    bal = await ref.native_balance(s.account_id)
    if amount > bal - GAS_RESERVE:
        await chat.send_message(f"Недостаточно NEAR: баланс {fmt_near(bal)}, резерв на газ {fmt_near(GAS_RESERVE)}.")
        return
    key = (s.account_id, token)
    if key in BUSY:
        await chat.send_message("Сделка по этому токену уже выполняется.")
        return
    BUSY.add(key)
    msg = await chat.send_message(f"⏳ Покупаю на {fmt_near(amount)} NEAR…")
    try:
        r = await ref.buy(s, token, amount, slippage_bps(uid))
        db.add_trade(s.account_id, token, "buy", r["got"], amount, r["hash"])
        meta = await ref.ft_metadata(token)
        await msg.edit_text(f"✅ Куплено <b>{fmt_tok(r['got'], int(meta.get('decimals') or 0))} "
                            f"{esc(meta.get('symbol') or '')}</b> за {fmt_near(amount)} NEAR · {nb_tx(r['hash'])}",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await send_card(uid, chat, token)
    except NoPool:
        await msg.edit_text("❌ Нет пула с NEAR ни на Ref, ни на Ref DCL для этого токена.")
    except (TxFailed, RpcError) as e:
        h = f" · {nb_tx(e.tx_hash)}" if isinstance(e, TxFailed) and e.tx_hash else ""
        await msg.edit_text(f"❌ Покупка не прошла: {esc(str(e)[:300])}{h}", parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    finally:
        BUSY.discard(key)


async def do_sell(uid: int, s: Signer, chat, token: str, pct: Decimal):
    bal = await ref.ft_balance(token, s.account_id)
    amount = bal if pct >= 100 else int(Decimal(bal) * pct / 100)
    if amount <= 0:
        await chat.send_message("Нечего продавать.")
        return
    key = (s.account_id, token)
    if key in BUSY:
        await chat.send_message("Сделка по этому токену уже выполняется.")
        return
    BUSY.add(key)
    msg = await chat.send_message(f"⏳ Продаю {pct:g}%…")
    try:
        r = await ref.sell(s, token, amount, slippage_bps(uid))
        db.add_trade(s.account_id, token, "sell", amount, r["got"], r["hash"])
        await msg.edit_text(f"✅ Продано {pct:g}% → <b>{fmt_near(r['got'])} NEAR</b> · {nb_tx(r['hash'])}",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        await send_card(uid, chat, token)
    except NoPool:
        await msg.edit_text("❌ Нет пула с NEAR ни на Ref, ни на Ref DCL для этого токена.")
    except (TxFailed, RpcError) as e:
        h = f" · {nb_tx(e.tx_hash)}" if isinstance(e, TxFailed) and e.tx_hash else ""
        await msg.edit_text(f"❌ Продажа не прошла: {esc(str(e)[:300])}{h}", parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    finally:
        BUSY.discard(key)


def _token_from_cb(tid: str) -> str | None:
    row = db.token_by_id(int(tid)) if tid.isdigit() else None
    return row[0] if row else None


async def on_trade_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    token = _token_from_cb(parts[-1])
    if not token:
        await q.answer("Токен не найден", show_alert=True)
        return
    if parts[0] == "c":
        await q.answer("Обновлено")
        await send_card(uid, q.message.chat, token, edit_msg=q.message)
        return
    s = await need_wallet(update)
    if not s:
        return
    await q.answer()
    chat = q.message.chat
    if parts[0] == "b":
        await do_buy(uid, s, chat, token, to_yocto(parts[1]))
    elif parts[0] == "s":
        await do_sell(uid, s, chat, token, Decimal(parts[1]))
    elif parts[0] == "bx":
        ctx.user_data["await"] = ("buy", token)
        await chat.send_message("Сколько NEAR потратить? Например: 1.5")
    elif parts[0] == "sx":
        ctx.user_data["await"] = ("sell", token)
        await chat.send_message("Какой % продать? Например: 30")


# ---------------------------------------------------------------- позиции
async def show_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await need_wallet(update)
    if not s:
        return
    acc = s.account_id
    rows, kb = [], []
    for token in db.traded_tokens(acc)[:15]:
        bal = await ref.ft_balance(token, acc)
        if not bal:
            continue
        tid = db.token_id(token)
        _, sym, dec = db.token_by_id(tid)
        spent, got = db.totals(acc, token)
        try:
            val = await ref.quote_route(await ref.route(token), token, bal, WNEAR)
        except (NoPool, RpcError):
            val = 0
        pnl = got + val - spent
        pct = f" ({pnl / spent * 100:+.1f}%)" if spent else ""
        rows.append(f"<b>${esc(sym)}</b> · {fmt_tok(bal, dec)} ≈ {fmt_near(val)} NEAR\n"
                    f"   PnL {'+' if pnl >= 0 else '−'}{fmt_near(abs(pnl))} NEAR{pct}")
        kb.append([B(f"${sym}", callback_data=f"c:{tid}")])
    if not rows:
        await update.effective_message.reply_text(f"Открытых позиций на {short(acc)} нет.")
        return
    await update.effective_message.reply_text(f"📊 <b>Позиции</b> · {esc(short(acc))}\n\n" + "\n\n".join(rows),
                                              parse_mode=ParseMode.HTML, reply_markup=KB(kb))


# ---------------------------------------------------------------- настройки
def settings_kb(uid: int):
    cur = slippage_bps(uid)
    row = [B(("✅ " if cur == v else "") + f"{v / 100:g}%", callback_data=f"sl:{v}") for v in (100, 300, 500, 1000, 2000, 3000)]
    return KB([row[:3], row[3:], [B("✏️ Пресеты покупки: " + " / ".join(presets(uid)), callback_data="pr")]])


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⚙️ <b>Настройки</b>\nСлиппедж:", parse_mode=ParseMode.HTML,
                                              reply_markup=settings_kb(update.effective_user.id))


async def on_settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if q.data.startswith("sl:"):
        db.set(f"u{uid}:slippage_bps", int(q.data[3:]))
        await q.answer("Сохранено")
        await q.edit_message_reply_markup(settings_kb(uid))
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


MENU_BUTTONS = {"💼 Кошелёк": show_wallet, "📊 Позиции": show_positions, "⚙️ Настройки": show_settings}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""
    if text in MENU_BUTTONS:  # кнопка меню отменяет любой ввод
        ctx.user_data.pop("await", None)
        await MENU_BUTTONS[text](update, ctx)
        return
    pending = ctx.user_data.pop("await", None)
    if pending:
        kind = pending[0]
        if kind == "import":
            await import_secret(update, ctx, text)
        elif kind == "rename":
            name = " ".join(text.split())[:24]
            if not name:
                await update.message.reply_text("Пустое имя, отменено.")
                return
            db.rename_wallet(uid, pending[1], name)
            detail = await wallet_detail(uid, pending[1])
            if detail:
                await update.message.reply_text(detail[0], parse_mode=ParseMode.HTML, reply_markup=detail[1])
        elif kind == "tr_to":
            await transfer_to(update, ctx, pending[1], text)
        elif kind == "tr_amt":
            await transfer_amount(update, ctx, pending[1], pending[2], text)
        elif kind == "presets":
            vals = [parse_num(x) for x in text.split()]
            if len(vals) != 4 or not all(vals):
                await update.message.reply_text("Нужно ровно 4 положительных числа.")
                return
            db.set(f"u{uid}:buy_presets", [f"{v:g}" for v in vals])
            await update.message.reply_text("✅ Пресеты сохранены.", reply_markup=settings_kb(uid))
        elif kind in ("buy", "sell"):
            s = await need_wallet(update)
            if not s:
                return
            v = parse_num(text)
            if not v:
                await update.message.reply_text("Неверное число, отменено.")
                return
            if kind == "buy":
                await do_buy(uid, s, update.effective_chat, pending[1], to_yocto(v))
            elif v > 100:
                await update.message.reply_text("Максимум 100%.")
            else:
                await do_sell(uid, s, update.effective_chat, pending[1], v)
        return
    if wallets.is_phrase(text) or text.strip().startswith("ed25519:"):  # секрет прислали без «Импорт» — удаляем
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.effective_chat.send_message("🙈 Похоже на сид-фразу или ключ — я удалил сообщение. Чтобы добавить кошелёк: "
                                                 "💼 Кошелёк → 📥 Импорт.")
        return
    token = find_token(text)
    if token:
        await send_card(uid, update.effective_chat, token)
    else:
        await update.message.reply_text("Отправьте адрес токена (например, batman-4.nearlytrade.near) или ссылку на него.",
                                        reply_markup=MENU)


# ---------------------------------------------------------------- запуск
async def post_init(app: Application):
    try:
        await ref.refresh_index()
    except Exception as e:
        log.warning("Ref index load failed (will retry on demand): %s", e)
    await app.bot.set_my_commands([("start", "Главное меню"), ("wallet", "Кошельки"), ("positions", "Позиции"),
                                   ("settings", "Настройки")])


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
    app.add_handler(CommandHandler("wallet", show_wallet))
    app.add_handler(CommandHandler("positions", show_positions))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CallbackQueryHandler(on_cancel_cb, pattern=r"^w:x:"))
    app.add_handler(CallbackQueryHandler(on_wallet_cb, pattern=r"^w:"))
    app.add_handler(CallbackQueryHandler(on_transfer_cb, pattern=r"^tr:"))
    app.add_handler(CallbackQueryHandler(on_trade_cb, pattern=r"^(b|bx|s|sx|c):"))
    app.add_handler(CallbackQueryHandler(on_settings_cb, pattern=r"^(sl:|pr$)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
