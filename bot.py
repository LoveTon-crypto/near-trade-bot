"""Трейд-бот NEAR: кошельки (создание, импорт, перевод), купить/продать через Ref, позиции.

Работает для всех, только в личке. У каждого пользователя свои кошельки;
ключи на диске зашифрованы MASTER_KEY из .env (см. wallets.py).
"""
from __future__ import annotations

import asyncio
import contextvars
import html
import inspect
import io
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import (CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardMarkup,
                      Update)
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB
from telegram.constants import ParseMode, ChatType
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ExtBot, MessageHandler, TypeHandler, filters)

load_dotenv()

from db import DB
import market
import i18n
import pnl_card
from near import KeyPair, Rpc, Signer, RpcError, TxFailed, YOCTO, fmt_near, to_yocto
from ref import Ref, NoPool, WNEAR
import wallets
from wallets import Vault, WalletError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
RPC_URLS = [u.strip() for u in os.getenv(
    "RPC_URLS", "https://free.rpc.fastnear.com,https://rpc.intea.rs,https://near.drpc.org,https://1rpc.io/near,"
                "https://near.lava.build,https://rpc.shitzuapes.xyz").split(",") if u.strip()]
FASTNEAR_API_KEY = os.getenv("FASTNEAR_API_KEY", "").strip()
MAX_BUY = to_yocto(os.getenv("MAX_BUY_NEAR", "100"))
GAS_RESERVE = to_yocto(os.getenv("GAS_RESERVE_NEAR", "0.1"))
MAX_WALLETS = int(os.getenv("MAX_WALLETS", "10"))
SECRET_TTL = int(os.getenv("SECRET_MESSAGE_TTL", "120"))
# Комиссия бота и рефералка: FEE_BPS от каждой сделки уходит на FEE_ACCOUNT, из неё REF_SHARES (% по уровням)
# начисляются пригласившим. Реферальный доход выплачивается с FEE_ACCOUNT (ключ задаётся через admin.py).
FEE_BPS = int(os.getenv("FEE_BPS", "100"))
FEE_ACCOUNT = os.getenv("FEE_ACCOUNT", "buddhadefi-hot.tg").strip().lower()
REF_SHARES = [int(x) for x in os.getenv("REF_SHARES", "30,10,5").split(",") if x.strip()]
REF_MIN_WITHDRAW = to_yocto(os.getenv("REF_MIN_WITHDRAW_NEAR", "0.1"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}  # сек, через сколько удалять сообщения с ключами

ADDR_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9_\-]+\.)+(?:near|tg)|[0-9a-f]{64})(?![\w.-])")
URL_RES = [re.compile(r"nearly\.trade/([a-z0-9_\-.]+)"), re.compile(r"justhoot\.fun/p/([a-z0-9_\-.]+)"),
           re.compile(r"nearblocks\.io/(?:token|address)/([a-z0-9_\-.]+)")]

db = DB(os.getenv("DB_PATH", "trade.db"))
rpc = Rpc(RPC_URLS, FASTNEAR_API_KEY)
ref = Ref(rpc)
vault = Vault(wallets.load_master_key())
scanner = market.Scanner(rpc)

# ---------------------------------------------------------------- язык
# Русский — для ru/uk/be в Telegram, остальным английский; переключается в ⚙️ Настройках.
# Тексты пишутся по-русски, а I18nBot переводит всё исходящее (сообщения, подписи, кнопки, всплывашки) по i18n.EN.
CUR_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default="ru")


def lang_of(uid: int) -> str:
    return db.get(f"u{uid}:lang", "ru")


def _tr_markup(m):
    if isinstance(m, InlineKeyboardMarkup):
        rows = []
        for row in m.inline_keyboard:
            r = []
            for b in row:
                d = b.to_dict()
                d["text"] = i18n.to_en(b.text)
                r.append(InlineKeyboardButton.de_json(d, None))
            rows.append(r)
        return InlineKeyboardMarkup(rows)
    if isinstance(m, ReplyKeyboardMarkup):
        return ReplyKeyboardMarkup([[i18n.to_en(b.text) for b in row] for row in m.keyboard],
                                   resize_keyboard=m.resize_keyboard)
    return m


class I18nBot(ExtBot):
    """ExtBot, который переводит исходящие тексты на английский для пользователей с языком en."""

    async def _i18n(self, name: str, args, kwargs):
        meth = getattr(super(), name)
        ba = inspect.signature(meth).bind_partial(*args, **kwargs)
        a = ba.arguments
        chat = a.get("chat_id")
        lang = lang_of(chat) if isinstance(chat, int) and chat > 0 else CUR_LANG.get()
        if lang == "en":
            for k in ("text", "caption"):
                if isinstance(a.get(k), str):
                    a[k] = i18n.to_en(a[k])
            if a.get("reply_markup") is not None:
                a["reply_markup"] = _tr_markup(a["reply_markup"])
            m = a.get("media")
            if isinstance(m, InputMediaPhoto) and m.caption:
                a["media"] = InputMediaPhoto(m.media, caption=i18n.to_en(m.caption), parse_mode=m.parse_mode)
        return await meth(*ba.args, **ba.kwargs)

    async def send_message(self, *args, **kwargs):
        return await self._i18n("send_message", args, kwargs)

    async def send_photo(self, *args, **kwargs):
        return await self._i18n("send_photo", args, kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return await self._i18n("edit_message_text", args, kwargs)

    async def edit_message_caption(self, *args, **kwargs):
        return await self._i18n("edit_message_caption", args, kwargs)

    async def edit_message_media(self, *args, **kwargs):
        return await self._i18n("edit_message_media", args, kwargs)

    async def edit_message_reply_markup(self, *args, **kwargs):
        return await self._i18n("edit_message_reply_markup", args, kwargs)

    async def answer_callback_query(self, *args, **kwargs):
        return await self._i18n("answer_callback_query", args, kwargs)

SIGNERS: dict[int, Signer] = {}  # wallet id → Signer (один nonce-замок на кошелёк)
BUSY: set = set()
MENU = ReplyKeyboardMarkup([["💼 Кошелёк", "📊 Позиции"], ["📋 Ордера", "↗️ Вывод"], ["🤝 Рефералы", "⚙️ Настройки"]],
                           resize_keyboard=True)
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
    u = update.effective_user
    db.ensure_user(u.id)
    lang = db.get(f"u{u.id}:lang")
    if lang is None:
        lang = "ru" if (u.language_code or "").split("-")[0] in ("ru", "uk", "be") else "en"
        db.set(f"u{u.id}:lang", lang)
    CUR_LANG.set(lang)


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
    arg = ctx.args[0] if ctx.args else ""
    open_tid = None
    if "_t" in arg:
        arg, _, t = arg.partition("_t")
        open_tid = int(t) if t.isdigit() else None
    if arg.startswith("r") and db.is_fresh(uid):
        ref_uid = db.user_by_code(arg[1:])
        if ref_uid and db.set_referrer(uid, ref_uid):
            try:
                await ctx.bot.send_message(ref_uid, "🎉 По твоей ссылке пришёл новый пользователь! "
                                                    "Ты будешь получать часть комиссии с его сделок.")
            except Exception:
                pass
    if open_tid and db.token_by_id(open_tid):
        token = db.token_by_id(open_tid)[0]
        if not active_signer(uid):
            await update.message.reply_text("Чтобы купить токен, создайте кошелёк — это займёт секунду:", reply_markup=MENU)
            await update.message.reply_text("👛 Кошелёк:", reply_markup=KB([[B("➕ Создать", callback_data="w:new"),
                                                                              B("📥 Импорт", callback_data="w:imp")]]))
        await send_card(uid, update.effective_chat, token)
        return
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
         B("Вывод ↗️", callback_data=f"w:tr:{wid}")],
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
        await chat.send_message(f"↗️ <b>Вывод</b> с «{esc(name)}»\nЧто выводим?", parse_mode=ParseMode.HTML,
                                reply_markup=await assets_kb(wid, acc))


async def on_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Отменено")
    try:
        await q.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------- вывод (NEAR и токены)
async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await need_wallet(update)
    if not s:
        return
    wid = db.active_wallet(update.effective_user.id)
    await update.message.reply_text(f"↗️ <b>Вывод</b> с <code>{esc(short(s.account_id))}</code>\nЧто выводим?",
                                    parse_mode=ParseMode.HTML, reply_markup=await assets_kb(wid, s.account_id))


async def assets_kb(wid: int, acc: str) -> KB:
    """NEAR + токены с ненулевым балансом из истории сделок кошелька + «другой токен»."""
    rows = [[B(f"Ⓝ NEAR · {fmt_near(await ref.native_balance(acc))}", callback_data=f"wa:{wid}:n")]]
    for token in db.traded_tokens(acc)[:12]:
        try:
            bal = await ref.ft_balance(token, acc)
        except RpcError:
            continue
        if bal > 0:
            tid = db.token_id(token)
            _, sym, dec = db.token_by_id(tid)
            rows.append([B(f"${sym} · {fmt_tok(bal, dec or 0)}", callback_data=f"wa:{wid}:{tid}")])
    rows.append([B("➕ Другой токен (адрес контракта)", callback_data=f"wa:{wid}:x")])
    rows.append([B("Отмена", callback_data="w:x:0")])
    return KB(rows)


async def on_asset_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, wid, what = q.data.split(":")
    wid = int(wid)
    await q.answer()
    if not db.wallet(q.from_user.id, wid):
        return
    if what == "x":
        ctx.user_data["await"] = ("tr_token", wid)
        await q.edit_message_text("Отправьте адрес контракта токена (например, batman-4.nearlytrade.near):")
        return
    token = None if what == "n" else _token_from_cb(what)
    if what != "n" and not token:
        await q.edit_message_text("Токен не найден.")
        return
    await ask_recipient(q.message.chat, ctx, wid, token, edit=q)


async def ask_recipient(chat, ctx, wid: int, token: str | None, edit=None):
    label = "NEAR"
    if token:
        meta = await ref.ft_metadata(token)
        label = f"${esc(str(meta.get('symbol') or token)[:20])}"
    ctx.user_data["await"] = ("tr_to", wid, token)
    text = f"↗️ Вывод {label}\nОтправьте адрес получателя (<code>имя.near</code> или 64 символа):"
    if edit:
        await edit.edit_message_text(text, parse_mode=ParseMode.HTML)
    else:
        await chat.send_message(text, parse_mode=ParseMode.HTML)


async def transfer_token_addr(update: Update, ctx, wid: int, text: str):
    token = find_token(text)
    if not token:
        await update.message.reply_text("❌ Это не адрес токена. Вывод отменён.")
        return
    try:
        meta = await ref.ft_metadata(token)
    except RpcError:
        meta = None
    if not meta or meta.get("decimals") is None:
        await update.message.reply_text("❌ Это не токен NEAR (нет ft_metadata). Вывод отменён.")
        return
    db.token_id(token, str(meta.get("symbol") or "?")[:20], int(meta.get("decimals") or 0))
    await ask_recipient(update.effective_chat, ctx, wid, token)


async def transfer_to(update: Update, ctx, wid: int, token: str | None, text: str):
    to = text.strip().lower()
    if not wallets.ACCOUNT_RE.match(to):
        await update.message.reply_text("❌ Неверный адрес. Вывод отменён.")
        return
    if len(to) != 64 and not await rpc.account(to):
        await update.message.reply_text(f"❌ Аккаунта {to} не существует. Вывод отменён.")
        return
    s = signer_for(update.effective_user.id, wid)
    if not s:
        return
    if to == s.account_id:
        await update.message.reply_text("❌ Это тот же кошелёк.")
        return
    ctx.user_data["await"] = ("tr_amt", wid, to, token)
    if token:
        tid = db.token_id(token)
        _, sym, dec = db.token_by_id(tid)
        bal = await ref.ft_balance(token, s.account_id)
        await update.message.reply_text(f"Сколько ${esc(sym)} отправить? Доступно: <b>{fmt_tok(bal, dec or 0)}</b>\n"
                                        "Число, процент (<code>50%</code>) или <code>all</code> — всё.",
                                        parse_mode=ParseMode.HTML)
    else:
        bal = await ref.native_balance(s.account_id)
        await update.message.reply_text(f"Сколько NEAR отправить? Доступно: <b>{fmt_near(bal)}</b>\n"
                                        "Число, например <code>1.5</code>, процент (<code>50%</code>) или <code>all</code> — всё.",
                                        parse_mode=ParseMode.HTML)


def _parse_amount(text: str, bal: int, dec: int) -> int:
    t = text.strip().lower().replace(",", ".")
    if t in ("all", "max", "всё", "все", "100%"):
        return bal
    if t.endswith("%"):
        v = parse_num(t)
        return int(Decimal(bal) * min(v, Decimal(100)) / 100) if v else 0
    v = parse_num(t)
    return int(v * (Decimal(10) ** dec)) if v else 0


async def transfer_amount(update: Update, ctx, wid: int, to: str, token: str | None, text: str):
    s = signer_for(update.effective_user.id, wid)
    if not s:
        return
    nat = await ref.native_balance(s.account_id)
    fee = YOCTO // 100  # 0.01 NEAR на газ
    if token:
        tid = db.token_id(token)
        _, sym, dec = db.token_by_id(tid)
        dec = dec or 0
        bal = await ref.ft_balance(token, s.account_id)
        amt = _parse_amount(text, bal, dec)
        if amt <= 0 or amt > bal:
            await update.message.reply_text(f"❌ Неверная сумма. Доступно {fmt_tok(bal, dec)} ${esc(sym)}.")
            return
        if nat < fee:
            await update.message.reply_text(f"❌ Нужно хотя бы 0.01 NEAR на газ, а на кошельке {fmt_near(nat)}.")
            return
        what = f"{fmt_tok(amt, dec)} ${esc(sym)}"
    else:
        amt = _parse_amount(text, nat, 24)
        if text.strip().lower() in ("all", "max", "всё", "все", "100%"):
            amt = nat - fee
        if amt <= 0 or amt > nat - fee:
            await update.message.reply_text(f"❌ Неверная сумма или не хватает на газ. Доступно {fmt_near(nat)} NEAR.")
            return
        what = f"{fmt_near(amt)} NEAR"
    ctx.user_data["transfer"] = (wid, to, amt, token)
    await update.message.reply_text(
        f"Вывести <b>{what}</b>\nс <code>{esc(short(s.account_id))}</code>\nна <code>{esc(to)}</code>?\n\n"
        "Проверьте адрес: перевод в блокчейне нельзя отменить.",
        parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("✅ Отправить", callback_data="tr:ok"), B("Отмена", callback_data="tr:no")]]))


async def on_transfer_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pending = ctx.user_data.pop("transfer", None)
    await q.answer()
    if q.data != "tr:ok" or not pending:
        await q.edit_message_text("Вывод отменён.")
        return
    wid, to, amt, token = pending
    s = signer_for(q.from_user.id, wid)
    if not s:
        await q.edit_message_text("Кошелёк не найден.")
        return
    await q.edit_message_text("⏳ Отправляю…")
    try:
        if token:
            tid = db.token_id(token)
            _, sym, dec = db.token_by_id(tid)
            res = await ref.transfer_ft(s, token, to, amt)
            what = f"{fmt_tok(amt, dec or 0)} ${esc(sym)}"
        else:
            res = await ref.withdraw_near(s, to, amt)
            what = f"{fmt_near(amt)} NEAR"
        await q.edit_message_text(f"✅ Отправлено {what} на <code>{esc(to)}</code> · {nb_tx(res['_hash'])}",
                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except (TxFailed, RpcError) as e:
        await q.edit_message_text(f"❌ Ошибка: {esc(str(e)[:300])}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- карточка токена
def k_usd(x: float) -> str:
    x = float(x or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.2f}{suf}"
    return f"${x:,.2f}"


def near_px(x: float) -> str:
    """Цена в NEAR с «маленькими нулями», как в сканерах: 0.0₄1314."""
    return market.price_fmt(float(f"{x:.6g}"))[1:] if x else "0"  # .6g: 0.0014 не превращается в 0.001399


async def token_card(uid: int, token: str) -> tuple[str, KB, bytes | None]:
    meta = await ref.ft_metadata(token)
    sym, dec = str(meta.get("symbol") or "?")[:20], int(meta.get("decimals") or 0)
    tid = db.token_id(token, sym, dec)

    async def _route():
        try:
            return await ref.route(token)
        except NoPool:
            return None

    async def _mkt():
        try:
            return await scanner.card(token, with_chart=True, with_holders=False)
        except Exception as e:
            log.info("market %s: %s", token, e)
            return None

    route, c, usd_near = await asyncio.gather(_route(), _mkt(), near_usd())
    name = (c.name if c and c.name != "?" else meta.get("name")) or ""
    ch = (c.chg.get("h24") if c else None)
    head = f"<b>${esc(sym)}</b> · {esc(str(name)[:32])}"
    if ch is not None:
        head += f" | {'🟢' if ch >= 0 else '🔴'} {ch:+.2f}% за 24ч"
    lines = [head, f"<code>{esc(token)}</code>"]
    pad = "nearly" if token.endswith(".nearlytrade.near") else ("justhoot" if "justhoot" in token else "")
    links = [f'<a href="https://dexscreener.com/near/{esc(token)}">DS</a>',
             f'<a href="https://nearblocks.io/token/{esc(token)}">Nearblocks</a>']
    if pad == "nearly":
        links.append(f'<a href="https://nearly.trade/{esc(token)}">Nearly</a>')
    elif pad == "justhoot":
        links.append(f'<a href="https://justhoot.fun/p/{esc(token)}">JustHoot</a>')
    lines.append("└ " + " | ".join(links))
    lines.append("")

    price_usd = c.price if c else 0.0
    if price_usd:
        in_near = f" ({near_px(price_usd / usd_near)} Ⓝ)" if usd_near else ""
        lines.append(f"💲 Цена: <b>{market.price_fmt(price_usd)}</b>{in_near}")
        lp = c.liq or (route.wnear * 2 / YOCTO * usd_near if route and usd_near else 0)
        lines.append(f"📈 MC: <b>{k_usd(c.fdv)}</b> · 💧 LP: <b>{k_usd(lp)}</b>")
        if c.vol24 or c.txns24:
            lines.append(f"📊 Объём 24ч: {k_usd(c.vol24)} · сделок {c.txns24}")
        extra = []
        if c.ath_mcap and c.ath_mcap > c.fdv * 1.01:
            extra.append(f"🏆 ATH MC: {k_usd(c.ath_mcap)}")
        if c.created_ms:
            extra.append(f"⏱ {market.age_fmt(c.created_ms)}")
        if extra:
            lines.append(" · ".join(extra))
    elif route:
        probe = YOCTO // 10  # цена по котировке 0.1 NEAR (с учётом комиссии пула)
        out = await ref.quote_route(route, WNEAR, probe, token)
        if out:
            p_near = (probe / YOCTO) / (out / 10 ** dec)
            lines.append(f"💲 Цена: <b>{near_px(p_near)} Ⓝ</b>" + (f" ({market.price_fmt(p_near * usd_near)})" if usd_near else ""))
        lines.append(f"💧 LP: <b>{fmt_near(route.wnear * 2, 1)} Ⓝ</b>"
                     + (f" ({k_usd(route.wnear * 2 / YOCTO * usd_near)})" if usd_near else ""))
    if route:
        lines.append(f"🔀 Пул: {esc(route.label)}")
    else:
        lines.append("⚠️ Пула с NEAR нет ни на Ref, ни на Ref DCL. Токены JustHoot до миграции пока не поддерживаются.")

    s = active_signer(uid)
    has_pos = False
    if s:
        bal = await ref.ft_balance(token, s.account_id)
        if bal:
            has_pos = db.totals(s.account_id, token)[0] > 0
            lines.append("")
            lines.append(f"👛 У вас: <b>{fmt_tok(bal, dec)} {esc(sym)}</b>")
            try:
                if not route:
                    raise NoPool(token)
                val = await ref.quote_route(route, token, bal, WNEAR)
                spent, got = db.totals(s.account_id, token)
                u = lambda y: f" (${y / YOCTO * usd_near:,.2f})" if usd_near else ""
                if spent:
                    lines.append(f"📥 Вложено: <b>{fmt_near(spent)} Ⓝ</b>{u(spent)}")
                lines.append(f"💰 Текущая стоимость: <b>{fmt_near(val)} Ⓝ</b>{u(val)}")
                if spent:
                    pnl = got + val - spent
                    lines.append(f"{'📈' if pnl >= 0 else '📉'} PnL: <b>{'+' if pnl >= 0 else '−'}{fmt_near(abs(pnl))} Ⓝ</b> "
                                 f"({pnl / spent * 100:+.1f}%)")
            except (NoPool, RpcError):
                pass
        lines.append("")
        lines.append(f"👛 {esc(short(s.account_id))} · {fmt_near(await ref.native_balance(s.account_id))} Ⓝ · "
                     f"⚙️ слиппедж {slippage_bps(uid) / 100:g}%")
    if c and c.links:
        lines.append("")
        lines.append(" • ".join(f'<a href="{esc(u)}">{esc(lb)}</a>' for lb, u in c.links[:4]))

    png = None
    if c and len(c.candles) >= 3:
        if not c.symbol or c.symbol == "?":
            c.symbol = sym
        try:
            png = await asyncio.to_thread(market.render_chart, c)
        except Exception as e:
            log.info("chart %s: %s", token, e)
    pr = presets(uid)
    kb = KB([
        [B(f"🟢 {a} Ⓝ", callback_data=f"b:{a}:{tid}") for a in pr[:4]],
        [B("🟢 ✏️ X Ⓝ", callback_data=f"bx:{tid}"), B("🔄 Обновить", callback_data=f"c:{tid}")],
        [B("🔴 25%", callback_data=f"s:25:{tid}"), B("🔴 50%", callback_data=f"s:50:{tid}"),
         B("🔴 100%", callback_data=f"s:100:{tid}"), B("🔴 ✏️ X%", callback_data=f"sx:{tid}")],
        [B("📋 Лимит-ордер", callback_data=f"lo:new:{tid}")]
        + ([B("📤 Поделиться PnL", callback_data=f"pn:{tid}")] if has_pos else []),
    ])
    return "\n".join(lines), kb, png


def find_token(text: str) -> str | None:
    t = text.strip().lower()
    for r in URL_RES:
        m = r.search(t)
        if m:
            return m.group(1)
    m = ADDR_RE.search(t)
    return m.group(1) if m else None


async def send_card(uid: int, chat, token: str, edit_msg=None):
    png = None
    try:
        text, kb, png = await token_card(uid, token)
    except RpcError as e:
        busy = "429" in str(e) or "Rate limit" in str(e) or "все RPC" in str(e)
        text = ("⏳ Сеть NEAR сейчас перегружена запросами, карточка не загрузилась. Нажмите «Обновить» через пару секунд."
                if busy else f"Не удалось прочитать токен: {esc(str(e)[:200])}")
        tid = db.token_id(token)
        kb = KB([[B("🔄 Обновить", callback_data=f"c:{tid}")]])
    if png and len(html.unescape(re.sub(r"<[^>]+>", "", text))) > 1000:
        png = None  # подпись к фото ограничена 1024 символами — тогда отправляем без графика
    if edit_msg:
        try:
            if edit_msg.photo and png:
                await edit_msg.edit_media(InputMediaPhoto(png, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
            elif edit_msg.photo:
                await edit_msg.edit_caption(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await edit_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e):
                log.info("card edit: %s", e)
        return
    if png:
        await chat.send_photo(png, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


# ---------------------------------------------------------------- торговля
def fee_of(amount: int, account: str) -> int:
    if not FEE_BPS or not FEE_ACCOUNT or account == FEE_ACCOUNT:
        return 0
    return amount * FEE_BPS // 10_000


def wallet_id_of(uid: int, account: str) -> int | None:
    r = db.c.execute("SELECT id FROM wallets WHERE user_id=? AND account=?", (uid, account)).fetchone()
    return r[0] if r else None


async def swap_buy(uid: int, s: Signer, token: str, amount: int, slip: int) -> dict:
    """Покупка на amount NEAR: свапается amount − комиссия, комиссия уходит после успешного свапа."""
    fee = fee_of(amount, s.account_id)
    r = await ref.buy(s, token, amount - fee, slip)
    if fee:
        schedule_fee(uid, s, "buy", fee, r["hash"])
    return r


async def swap_sell(uid: int, s: Signer, token: str, amount: int, slip: int) -> dict:
    """Продажа: комиссия берётся с полученных NEAR; в r["got"] — сумма уже за вычетом комиссии."""
    r = await ref.sell(s, token, amount, slip)
    fee = fee_of(r["got"], s.account_id)
    if fee:
        r["got"] -= fee
        schedule_fee(uid, s, "sell", fee, r["hash"])
    return r


def schedule_fee(uid: int, s: Signer, side: str, fee: int, swap_tx: str):
    fid = db.add_fee(uid, wallet_id_of(uid, s.account_id), side, fee, swap_tx)
    asyncio.create_task(pay_fee(fid, uid, s, fee))


def ref_shares(uid: int, fee: int) -> list[tuple[int, int, int]]:
    return [(up, lvl, fee * pct // 100) for lvl, (up, pct) in enumerate(zip(db.upline(uid, len(REF_SHARES)), REF_SHARES), 1)]


async def pay_fee(fid: int, uid: int, s: Signer, fee: int, tries: int = 0) -> bool:
    try:
        res = await ref.withdraw_near(s, FEE_ACCOUNT, fee)
    except (TxFailed, RpcError) as e:
        db.fee_failed(fid, str(e), give_up=tries >= 19)
        log.info("fee %s not sent yet: %s", fid, str(e)[:120])
        return False
    db.fee_paid(fid, res["_hash"], ref_shares(uid, fee))
    return True


async def retry_fees(ctx: ContextTypes.DEFAULT_TYPE):
    for fid, uid, wid, amount, tries in db.pending_fees():
        s = signer_for(uid, wid) if wid else None
        if not s:
            db.fee_failed(fid, "кошелёк удалён", give_up=True)
            continue
        if await ref.native_balance(s.account_id) < int(amount) + YOCTO // 1000:
            db.fee_failed(fid, "не хватает NEAR на комиссию", give_up=tries >= 19)
            continue
        await pay_fee(fid, uid, s, int(amount), tries)


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
        r = await swap_buy(uid, s, token, amount, slippage_bps(uid))
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
        r = await swap_sell(uid, s, token, amount, slippage_bps(uid))
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


# ---------------------------------------------------------------- лимит-ордера (как в DTrade)
# Бот сам следит за ценой (раз в ORDER_POLL_SEC) и, когда условие выполнено, делает рыночный свап.
#   Покупка · базовый  — купить, когда цена упала до цели (≤)
#   Покупка · пробой   — купить, когда цена выросла до цели (≥)
#   Продажа · базовый  — тейк-профит: продать, когда цена выросла до цели (≥)
#   Продажа · стоп-лосс — продать, когда цена упала до цели (≤)
ORDER_POLL = int(os.getenv("ORDER_POLL_SEC", "10"))
MAX_ORDERS = int(os.getenv("MAX_ORDERS", "20"))
UNIT_LABEL = {"near": "в NEAR", "pct": "в %", "usd": "в $", "mc": "в MC $"}
SLIP_STEPS = (100, 300, 500, 1000, 2000, 3000)
PCT_STEPS = (25, 50, 75, 100)


def order_cond(side: str, mode: str) -> str:
    return {("buy", "basic"): "le", ("buy", "stop"): "ge", ("sell", "basic"): "ge", ("sell", "stop"): "le"}[(side, mode)]


def mode_label(side: str, mode: str) -> str:
    if mode == "basic":
        return "⚪ Лимит на просадке (−%)" if side == "buy" else "⚪ Тейк-профит (+%)"
    return "🚀 Покупка на пробое (+%)" if side == "buy" else "🛑 Стоп-лосс (−%)"


def fmt_unit(unit: str, v: float) -> str:
    if unit == "pct":
        return f"{v:+g}%"
    if unit == "near":
        return f"{near_px(v)} Ⓝ"
    if unit == "usd":
        return market.price_fmt(float(f"{v:.6g}"))
    return k_usd(v)


async def token_state(token: str) -> dict:
    """Цена токена сейчас: в NEAR (из пула), в $ и капитализация."""
    meta = await ref.ft_metadata(token)
    dec = int(meta.get("decimals") or 0)
    route = await ref.route(token)
    px, usd, supply = await asyncio.gather(ref.spot_price(route, token, dec), near_usd(), ref.total_supply(token))
    sup = supply / 10 ** dec if dec else float(supply)
    return {"dec": dec, "sym": str(meta.get("symbol") or "?")[:20], "route": route, "near": px,
            "usd": px * usd, "mc": px * usd * sup, "near_usd": usd, "supply": sup}


def in_unit(st: dict, unit: str) -> float:
    return st["near"] if unit == "near" else st["usd"] if unit == "usd" else st["mc"]


def target_to(st: dict, unit: str, value: float, to: str) -> float:
    """Пересчёт цели из одной единицы в другую по текущим NEAR/$ и supply."""
    if unit == "pct":
        near = st["near"] * (1 + value / 100)
    elif unit == "near":
        near = value
    elif unit == "usd":
        near = value / st["near_usd"] if st["near_usd"] else 0
    else:
        near = value / st["near_usd"] / st["supply"] if st["near_usd"] and st["supply"] else 0
    return near if to == "near" else near * st["near_usd"] if to == "usd" else near * st["near_usd"] * st["supply"]


def new_draft(uid: int, tid: int) -> dict:
    return {"tid": tid, "side": "buy", "mode": "basic", "wid": db.active_wallet(uid), "unit": "pct", "value": None,
            "amount": presets(uid)[0], "pct": 100, "slip": slippage_bps(uid), "name": ""}


async def draft_view(uid: int, d: dict) -> tuple[str, KB]:
    token = _token_from_cb(str(d["tid"]))
    st = await token_state(token)
    side, mode = d["side"], d["mode"]
    w = db.wallet(uid, d["wid"]) if d["wid"] else None
    acc = w[2] if w else None
    lines = [f"📋 <b>Лимит-ордер</b> · <b>${esc(st['sym'])}</b>", f"<code>{esc(token)}</code>",
             f"💲 Сейчас: {near_px(st['near'])} Ⓝ ({market.price_fmt(st['usd'])}) · MC {k_usd(st['mc'])}", ""]
    kind = ("📈 Покупка" if side == "buy" else "📉 Продажа") + " · " + mode_label(side, mode)[2:]
    lines.append(f"🧬 Тип: <b>{kind}</b>")
    cond = order_cond(side, mode)
    if d["value"] is not None:
        t_near = target_to(st, d["unit"], d["value"], "near")
        sign = "≤" if cond == "le" else "≥"
        diff = (t_near / st["near"] - 1) * 100 if st["near"] else 0
        lines.append(f"🎯 Сработает, когда цена {sign} цели ({diff:+.1f}% от текущей)")
        lines.append(f"💰 Лимитная цена: ≈{near_px(t_near)} Ⓝ ({market.price_fmt(target_to(st, 'near', t_near, 'usd'))})")
        lines.append(f"📈 Лимитная MC: ≈{k_usd(target_to(st, 'near', t_near, 'mc'))}")
        if (cond == "le" and st["near"] <= t_near) or (cond == "ge" and st["near"] >= t_near):
            lines.append("⚠️ Цена уже на цели — ордер исполнится сразу после создания.")
    else:
        lines.append("🎯 Цена не задана — нажмите «Цена»")
    lines.append(f"🍌 Slippage: {d['slip'] / 100:g}%")
    if side == "buy":
        lines.append(f"Ⓝ Сумма: <b>{esc(d['amount'])} Ⓝ</b>"
                     + (f" ≈${float(d['amount']) * st['near_usd']:,.2f}" if st["near_usd"] else ""))
    else:
        lines.append(f"📊 Продать: <b>{d['pct']:g}%</b> баланса токена в момент срабатывания")
    if d["name"]:
        lines.append(f"🏷 {esc(d['name'])}")
    if acc:
        nat, tb = await asyncio.gather(ref.native_balance(acc), ref.ft_balance(token, acc))
        lines += ["", f"👛 <b>Баланс</b> {esc(short(acc))}",
                  f"└ {fmt_near(nat)} Ⓝ" + (f" (${nat / YOCTO * st['near_usd']:,.2f})" if st["near_usd"] else ""),
                  f"   └ ${esc(st['sym'])} {fmt_tok(tb, st['dec'])}"]
    kb = [[B("🔄 Обновить", callback_data="lo:r")],
          [B(f"🏷 Название: {d['name'][:16] or 'не задано'} (✏️)", callback_data="lo:name")],
          [B(("📈 Покупка" if side == "buy" else "📉 Продажа") + " (🔀)", callback_data="lo:side")],
          [B(f"👛 {short(acc) if acc else 'нет кошелька'} (🔀)", callback_data="lo:wal")],
          [B("💰 Цена: " + (fmt_unit(d["unit"], d["value"]) if d["value"] is not None else "—") + " (✏️)",
             callback_data="lo:price")],
          [B(f"Ⓝ Сумма {d['amount']} (✏️)", callback_data="lo:amt") if side == "buy"
           else B(f"📊 Процент {d['pct']:g}% (🔀)", callback_data="lo:pct"),
           B(f"🍌 Slippage {d['slip'] / 100:g}% (🔀)", callback_data="lo:slip")],
          [B(mode_label(side, mode) + " (🔀)", callback_data="lo:mode")],
          [B("✅ Создать ордер", callback_data="lo:ok") if d["value"] is not None
           else B("💬 Введите цену", callback_data="lo:price"), B("🚫 Отмена", callback_data="lo:x")]]
    return "\n".join(lines), KB(kb)


def price_prompt(d: dict) -> tuple[str, KB]:
    up = (d["side"] == "sell") == (d["mode"] == "basic")  # тейк-профит и пробой — цель выше текущей
    steps = (10, 25, 50, 75, 150, 200) if up else (-10, -20, -25, -50, -75, -90)
    unit = d.get("pu", "pct")
    act = "купить" if d["side"] == "buy" else "продать"
    hint = {"pct": "изменение цены в % от текущей (например, " + ("50" if up else "-30") + ")",
            "near": "цену 1 токена в NEAR (например, 0.0005)", "usd": "цену 1 токена в $ (например, 0.00012)",
            "mc": "капитализацию в $ (например, 50000 или 50k)"}[unit]
    text = f"💬 Введи {hint}, при которой {act} токен, или выбери кнопку:"
    rows = [[B(f"{'+' if s > 0 else '−'} {abs(s)}%", callback_data=f"lo:pp:{s}") for s in steps[:3]],
            [B(f"{'+' if s > 0 else '−'} {abs(s)}%", callback_data=f"lo:pp:{s}") for s in steps[3:]],
            [B(("✅ " if unit == u else "☑️ ") + UNIT_LABEL[u], callback_data=f"lo:pu:{u}") for u in ("near", "pct")],
            [B(("✅ " if unit == u else "☑️ ") + UNIT_LABEL[u], callback_data=f"lo:pu:{u}") for u in ("usd", "mc")],
            [B("🚫 Отмена", callback_data="lo:pc")]]
    return text, KB(rows)


def parse_value(text: str, unit: str) -> float | None:
    t = text.strip().lower().replace(",", ".").replace("$", "").replace(" ", "").rstrip("%")
    mult = 1
    if unit in ("mc", "usd") and t[-1:] in ("k", "m", "к", "м"):
        mult = 1e3 if t[-1] in "kк" else 1e6
        t = t[:-1]
    try:
        v = float(Decimal(t)) * mult
    except (InvalidOperation, ValueError):
        return None
    if unit != "pct" and v <= 0:
        return None
    return v


async def send_draft(uid: int, chat, ctx, edit_q=None):
    d = ctx.user_data.get("lo_draft")
    if not d:
        return
    try:
        text, kb = await draft_view(uid, d)
    except NoPool:
        text, kb = "⚠️ Пула с NEAR для этого токена нет — лимит-ордер поставить нельзя.", None
    except RpcError:
        text, kb = "⏳ Сеть NEAR перегружена, попробуйте через пару секунд.", KB([[B("🔄 Обновить", callback_data="lo:r")]])
    if edit_q:
        try:
            await edit_q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception as e:
            if "not modified" in str(e):
                return
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_order_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    act = parts[1]
    chat = q.message.chat

    # --- список ордеров и отмена
    if act == "l":
        await q.answer()
        text, kb = orders_view(uid)
        await edit_or_send(q, text, kb)
        return
    if act == "add":
        if not await need_wallet(update):
            return
        await q.answer()
        ctx.user_data["await"] = ("lo_token",)
        await chat.send_message("💬 Отправьте адрес токена (или ссылку nearly / justhoot / nearblocks), на который хотите "
                                "поставить лимит-ордер:", reply_markup=KB([[B("🚫 Отмена", callback_data="lo:pc")]]))
        return
    if act == "stop":
        n = len(db.user_orders(uid))
        await q.answer()
        await chat.send_message(f"🛑 Отменить все активные ордера ({n})?", reply_markup=KB(
            [[B("Да, отменить все", callback_data="lo:stopok"), B("Отмена", callback_data="w:x:0")]]))
        return
    if act == "stopok":
        n = db.cancel_all_orders(uid)
        await q.answer(f"Отменено: {n}")
        text, kb = orders_view(uid)
        await edit_or_send(q, text, kb)
        return
    if act == "o":
        await q.answer()
        o = db.order(uid, int(parts[2]))
        if not o:
            return
        await edit_or_send(q, order_text(o), KB(
            ([[B("❌ Отменить ордер", callback_data=f"lo:c:{o['id']}")]] if o["status"] == "active" else [])
            + [[B("⬅️ К ордерам", callback_data="lo:l")]]))
        return
    if act == "c":
        ok = db.cancel_order(uid, int(parts[2]))
        await q.answer("Ордер отменён" if ok else "Ордер уже не активен")
        text, kb = orders_view(uid)
        await edit_or_send(q, text, kb)
        return

    # --- редактор
    if act == "new":
        tid = int(parts[2])
        if not db.token_by_id(tid):
            await q.answer("Токен не найден", show_alert=True)
            return
        if not await need_wallet(update):
            return
        await q.answer()
        ctx.user_data["lo_draft"] = new_draft(uid, tid)
        await send_draft(uid, chat, ctx)
        return
    d = ctx.user_data.get("lo_draft")
    if not d:
        await q.answer("Черновик устарел — откройте токен и нажмите «Лимит-ордер» снова", show_alert=True)
        return
    if act == "r":
        await q.answer("Обновлено")
    elif act == "side":
        d["side"] = "sell" if d["side"] == "buy" else "buy"
        d["mode"] = "basic"  # по умолчанию: покупка на просадке (−%), продажа в плюс (+%)
        d["value"] = None  # направление цели меняется — цену задаём заново
        await q.answer()
    elif act == "mode":
        d["mode"] = "stop" if d["mode"] == "basic" else "basic"
        d["value"] = None
        await q.answer()
    elif act == "wal":
        ws = [w[0] for w in db.wallets(uid)]
        if ws:
            d["wid"] = ws[(ws.index(d["wid"]) + 1) % len(ws)] if d["wid"] in ws else ws[0]
        await q.answer()
    elif act == "slip":
        cur = d["slip"]
        d["slip"] = next((s for s in SLIP_STEPS if s > cur), SLIP_STEPS[0])
        await q.answer()
    elif act == "pct":
        d["pct"] = next((p for p in PCT_STEPS if p > d["pct"]), PCT_STEPS[0])
        await q.answer()
    elif act == "amt":
        await q.answer()
        ctx.user_data["await"] = ("lo_amt",)
        await chat.send_message("Сколько NEAR потратить на покупку? Например: 1.5")
        return
    elif act == "name":
        await q.answer()
        ctx.user_data["await"] = ("lo_name",)
        await chat.send_message("Название ордера (до 24 символов), например: «докупка на дне»")
        return
    elif act == "price":
        await q.answer()
        d.setdefault("pu", "pct")
        ctx.user_data["await"] = ("lo_price",)
        text, kb = price_prompt(d)
        await chat.send_message(text, reply_markup=kb)
        return
    elif act == "pu":
        d["pu"] = parts[2]
        ctx.user_data["await"] = ("lo_price",)
        await q.answer(UNIT_LABEL[parts[2]])
        text, kb = price_prompt(d)
        await q.edit_message_text(text, reply_markup=kb)
        return
    elif act == "pp":
        d["unit"], d["value"] = "pct", float(parts[2])
        ctx.user_data.pop("await", None)
        await q.answer()
    elif act == "pc":
        ctx.user_data.pop("await", None)
        await q.answer("Отменено")
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    elif act == "x":
        ctx.user_data.pop("lo_draft", None)
        ctx.user_data.pop("await", None)
        await q.answer("Отменено")
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    elif act == "ok":
        await create_order(q, ctx, uid, d)
        return
    await send_draft(uid, chat, ctx, edit_q=q)


async def create_order(q, ctx, uid: int, d: dict):
    if d["value"] is None:
        await q.answer("Сначала задайте цену", show_alert=True)
        return
    if not d["wid"] or not db.wallet(uid, d["wid"]):
        await q.answer("Выберите кошелёк", show_alert=True)
        return
    if len(db.user_orders(uid)) >= MAX_ORDERS:
        await q.answer(f"Максимум {MAX_ORDERS} активных ордеров", show_alert=True)
        return
    token = _token_from_cb(str(d["tid"]))
    try:
        st = await token_state(token)
    except (NoPool, RpcError) as e:
        await q.answer(f"Не удалось получить цену: {str(e)[:80]}", show_alert=True)
        return
    unit, target = d["unit"], d["value"]
    if unit == "pct":  # проценты фиксируем в цену NEAR в момент создания
        unit, target = "near", target_to(st, "pct", d["value"], "near")
    if target <= 0:
        await q.answer("Цель получилась ≤ 0 — задайте другую цену", show_alert=True)
        return
    amount = to_yocto(d["amount"]) if d["side"] == "buy" else None
    if amount and amount > MAX_BUY:
        await q.answer(f"Больше лимита {fmt_near(MAX_BUY)} NEAR на одну покупку", show_alert=True)
        return
    oid = db.add_order(uid, d["wid"], token, d["side"], order_cond(d["side"], d["mode"]), unit, target, amount,
                       d["pct"] if d["side"] == "sell" else None, d["slip"], d["name"][:24])
    ctx.user_data.pop("lo_draft", None)
    await q.answer("Ордер создан")
    o = db.order(uid, oid)
    await q.edit_message_text("✅ <b>Ордер создан.</b> Бот проверяет цену каждые "
                              f"{ORDER_POLL} сек и пришлёт уведомление, когда он исполнится.\n\n" + order_text(o),
                              parse_mode=ParseMode.HTML,
                              reply_markup=KB([[B("📋 Мои ордера", callback_data="lo:l"),
                                                B("❌ Отменить", callback_data=f"lo:c:{oid}")]]))


STATUS_LABEL = {"active": "⏳ активен", "executing": "⚙️ исполняется", "done": "✅ исполнен",
                "failed": "❌ не исполнен", "cancelled": "🚫 отменён"}


def order_text(o: dict) -> str:
    row = db.c.execute("SELECT symbol, decimals FROM tokens WHERE address=?", (o["token"],)).fetchone()
    sym = row[0] if row and row[0] else o["token"]
    side = "📈 Покупка" if o["side"] == "buy" else "📉 Продажа"
    mode = "basic" if order_cond(o["side"], "basic") == o["cond"] else "stop"
    sign = "≤" if o["cond"] == "le" else "≥"
    what = (f"на {fmt_near(int(o['amount']))} Ⓝ" if o["side"] == "buy" else f"{o['pct']:g}% баланса")
    lines = [f"<b>#{o['id']}</b> {side} · {mode_label(o['side'], mode)[2:]} · <b>${esc(sym)}</b>"
             + (f" · 🏷 {esc(o['name'])}" if o["name"] else ""),
             f"🎯 когда {'цена' if o['unit'] != 'mc' else 'MC'} {sign} {fmt_unit(o['unit'], o['target'])} · {what}",
             f"🍌 slippage {o['slippage_bps'] / 100:g}% · {STATUS_LABEL.get(o['status'], o['status'])}"]
    if o.get("note"):
        lines.append(f"ℹ️ {esc(o['note'])}")
    if o.get("tx"):
        lines.append(f"🔗 {nb_tx(o['tx'])}")
    return "\n".join(lines)


def orders_view(uid: int) -> tuple[str, KB]:
    act = db.user_orders(uid)
    if not act:
        recent = db.user_orders(uid, active_only=False, limit=5)
        text = "📋 <b>Лимит-ордера</b>\n\nАктивных ордеров нет. Нажмите «➕ Новый ордер» или откройте токен и нажмите «📋 Лимит-ордер»."
        if recent:
            text += "\n\n<b>Последние:</b>\n\n" + "\n\n".join(order_text(o) for o in recent)
        return text, KB([[B("➕ Новый ордер", callback_data="lo:add")]])
    text = f"📋 <b>Активные лимит-ордера</b> ({len(act)}/{MAX_ORDERS})\n\n" + "\n\n".join(order_text(o) for o in act)
    if len(text) > 3800:
        text = text[:3800] + "…"
    kb = [[B("➕ Новый ордер", callback_data="lo:add")]]
    kb += [[B(f"#{o['id']} — открыть / отменить", callback_data=f"lo:o:{o['id']}")] for o in act[:12]]
    kb.append([B("🛑 Остановить все", callback_data="lo:stop")])
    return text, KB(kb)


async def show_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = orders_view(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                              disable_web_page_preview=True)


async def order_token_input(update: Update, ctx, text: str):
    uid = update.effective_user.id
    token = find_token(text)
    if not token:
        ctx.user_data["await"] = ("lo_token",)
        await update.message.reply_text("❌ Это не адрес токена. Пришлите адрес контракта, например batman-4.nearlytrade.near")
        return
    try:
        meta = await ref.ft_metadata(token)
        await ref.route(token)
    except NoPool:
        await update.message.reply_text("⚠️ Пула с NEAR для этого токена нет — лимит-ордер поставить нельзя.")
        return
    except RpcError:
        await update.message.reply_text("❌ Это не токен NEAR или сеть перегружена. Попробуйте ещё раз.")
        return
    tid = db.token_id(token, str(meta.get("symbol") or "?")[:20], int(meta.get("decimals") or 0))
    ctx.user_data["lo_draft"] = new_draft(uid, tid)
    await send_draft(uid, update.effective_chat, ctx)


async def order_input(update: Update, ctx, kind: str, text: str):
    uid = update.effective_user.id
    d = ctx.user_data.get("lo_draft")
    if not d:
        await update.message.reply_text("Черновик ордера устарел — откройте токен и нажмите «Лимит-ордер» снова.")
        return
    if kind == "lo_amt":
        v = parse_num(text)
        if not v:
            await update.message.reply_text("Неверное число.")
        else:
            d["amount"] = f"{v:g}"
    elif kind == "lo_name":
        d["name"] = " ".join(text.split())[:24]
    elif kind == "lo_price":
        unit = d.get("pu", "pct")
        v = parse_value(text, unit)
        if v is None:
            ctx.user_data["await"] = ("lo_price",)
            await update.message.reply_text("Не понял число, попробуйте ещё раз (или 🚫 Отмена).")
            return
        if unit == "pct":
            up = (d["side"] == "sell") == (d["mode"] == "basic")
            v = abs(v) if up else -abs(v)
            if v <= -100:
                await update.message.reply_text("Падение не может быть 100% и больше.")
                return
        d["unit"], d["value"] = unit, v
    await send_draft(uid, update.effective_chat, ctx)


# ---------------------------------------------------------------- исполнение ордеров
async def check_orders(ctx: ContextTypes.DEFAULT_TYPE):
    orders = db.active_orders()
    if not orders:
        return
    states: dict[str, dict | None] = {}
    for token in {o["token"] for o in orders}:
        try:
            states[token] = await token_state(token)
        except Exception as e:  # пул пропал / сеть занята — проверим на следующем круге
            log.info("orders: price %s: %s", token, str(e)[:120])
            states[token] = None
    for o in orders:
        st = states.get(o["token"])
        if not st or not st["near"]:
            continue
        cur = in_unit(st, o["unit"])
        hit = cur <= o["target"] if o["cond"] == "le" else cur >= o["target"]
        if hit and db.claim_order(o["id"]):
            asyncio.create_task(execute_order(ctx.bot, o, st))


async def execute_order(tg_bot, o: dict, st: dict):
    uid, token = o["user_id"], o["token"]
    sym, dec = st["sym"], st["dec"]
    tid = db.token_id(token, sym, dec)
    open_kb = KB([[B(f"📊 ${sym}", callback_data=f"c:{tid}"), B("📋 Ордера", callback_data="lo:l")]])

    async def tell(text):
        try:
            await tg_bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=open_kb,
                                      disable_web_page_preview=True)
        except Exception as e:
            log.info("order notify %s: %s", uid, e)

    s = signer_for(uid, o["wallet_id"])
    if not s:
        db.finish_order(o["id"], "failed", note="кошелёк удалён")
        await tell(f"❌ Ордер #{o['id']} (${esc(sym)}) не исполнен: кошелёк удалён из бота.")
        return
    key = (s.account_id, token)
    while key in BUSY:
        await asyncio.sleep(1)
    BUSY.add(key)
    try:
        if o["side"] == "buy":
            amount = int(o["amount"])
            bal = await ref.native_balance(s.account_id)
            if amount > bal - GAS_RESERVE:
                db.finish_order(o["id"], "failed", note=f"не хватило NEAR: баланс {fmt_near(bal)}")
                await tell(f"❌ Ордер #{o['id']} сработал, но на кошельке {fmt_near(bal)} Ⓝ — "
                           f"не хватает на покупку {fmt_near(amount)} Ⓝ + газ. Ордер закрыт.")
                return
            r = await swap_buy(uid, s, token, amount, o["slippage_bps"])
            db.add_trade(s.account_id, token, "buy", r["got"], amount, r["hash"])
            db.finish_order(o["id"], "done", tx=r["hash"])
            await tell(f"✅ <b>Лимит-ордер #{o['id']} исполнен</b>\nКуплено <b>{fmt_tok(r['got'], dec)} ${esc(sym)}</b> "
                       f"за {fmt_near(amount)} Ⓝ по цене ≈{near_px(st['near'])} Ⓝ · MC {k_usd(st['mc'])} · {nb_tx(r['hash'])}")
        else:
            bal = await ref.ft_balance(token, s.account_id)
            amount = bal if o["pct"] >= 100 else int(Decimal(bal) * Decimal(str(o["pct"])) / 100)
            if amount <= 0:
                db.finish_order(o["id"], "failed", note="нечего продавать")
                await tell(f"❌ Ордер #{o['id']} сработал, но ${esc(sym)} на кошельке нет. Ордер закрыт.")
                return
            r = await swap_sell(uid, s, token, amount, o["slippage_bps"])
            db.add_trade(s.account_id, token, "sell", amount, r["got"], r["hash"])
            db.finish_order(o["id"], "done", tx=r["hash"])
            what = "Стоп-лосс" if o["cond"] == "le" else "Тейк-профит"
            await tell(f"✅ <b>{what} #{o['id']} исполнен</b>\nПродано {fmt_tok(amount, dec)} ${esc(sym)} → "
                       f"<b>{fmt_near(r['got'])} Ⓝ</b> · MC {k_usd(st['mc'])} · {nb_tx(r['hash'])}")
    except (TxFailed, RpcError, NoPool) as e:
        h = e.tx_hash if isinstance(e, TxFailed) else None
        db.finish_order(o["id"], "failed", tx=h, note=str(e)[:200])
        await tell(f"❌ Ордер #{o['id']} (${esc(sym)}) не исполнен: {esc(str(e)[:250])}"
                   + (f" · {nb_tx(h)}" if h else ""))
    except Exception as e:
        log.exception("order %s", o["id"])
        db.finish_order(o["id"], "failed", note=f"внутренняя ошибка: {type(e).__name__}")
        await tell(f"❌ Ордер #{o['id']} (${esc(sym)}) не исполнен из-за внутренней ошибки. Проверьте баланс.")
    finally:
        BUSY.discard(key)


# ---------------------------------------------------------------- рефералы
def payout_signer() -> Signer | None:
    """Кошелёк выплат реферального дохода = FEE_ACCOUNT. Ключ задаётся на сервере: admin.py payout-key."""
    cfg = db.get("payout_wallet")
    if not cfg or cfg.get("account") != FEE_ACCOUNT:
        return None
    s = SIGNERS.get(-1)
    if not s:
        kp, _ = vault.open(0, cfg["account"], bytes.fromhex(cfg["blob"]))
        s = SIGNERS[-1] = Signer(rpc, cfg["account"], kp)
    return s


async def ref_view(uid: int, bot_username: str) -> tuple[str, KB]:
    code = db.ref_code(uid)
    link = f"https://t.me/{bot_username}?start=r{code}"
    earned = db.ref_earned(uid)
    counts = db.referral_counts(uid)
    bal = db.ref_balance(uid)
    usd = await near_usd()
    u = lambda y: f" (${y / YOCTO * usd:,.2f})" if usd else ""
    lines = ["🤝 <b>Реферальная программа</b>",
             "Приглашай друзей и получай часть комиссии бота с каждой их сделки — пожизненно, по 3 уровням:",
             "", "📦 <b>Твой доход</b>"]
    for lvl, pct in enumerate(REF_SHARES, 1):
        lines.append(f"{lvl} ур. ({pct}%): <b>{fmt_near(earned.get(lvl, 0))} Ⓝ</b> · 👤 {counts[lvl - 1] if lvl <= 3 else 0}")
    lines += ["", f"Всего: <b>{fmt_near(sum(earned.values()))} Ⓝ</b>{u(sum(earned.values()))} · рефералов: 👤 {sum(counts)}",
              f"Доступно для вывода: <b>{fmt_near(bal)} Ⓝ</b>{u(bal)}",
              "", "🔗 <b>Твоя ссылка</b>", f"<code>{link}</code>"]
    share = "https://t.me/share/url?url=" + link.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F").replace("=", "%3D")
    kb = KB([[B("📤 Поделиться ссылкой", url=share)],
             [B(f"💸 Вывод (от {fmt_near(REF_MIN_WITHDRAW)} Ⓝ)", callback_data="rf:w")],
             [B("🔄 Обновить", callback_data="rf:r")]])
    return "\n".join(lines), kb


async def show_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = await ref_view(update.effective_user.id, ctx.bot.username)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                              disable_web_page_preview=True)


async def on_ref_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if q.data == "rf:r":
        await q.answer("Обновлено")
        await edit_or_send(q, *await ref_view(uid, ctx.bot.username))
        return
    if q.data == "rf:w":
        bal = db.ref_balance(uid)
        if bal < REF_MIN_WITHDRAW:
            await q.answer(f"Минимум для вывода {fmt_near(REF_MIN_WITHDRAW)} NEAR, сейчас {fmt_near(bal)}", show_alert=True)
            return
        s = active_signer(uid)
        if not s:
            await q.answer("Сначала создайте кошелёк — доход придёт на него", show_alert=True)
            return
        await q.answer()
        await q.message.chat.send_message(
            f"💸 Вывести <b>{fmt_near(bal)} Ⓝ</b> реферального дохода на кошелёк <code>{esc(s.account_id)}</code>?",
            parse_mode=ParseMode.HTML,
            reply_markup=KB([[B("✅ Вывести", callback_data="rf:ok"), B("Отмена", callback_data="w:x:0")]]))
        return
    if q.data == "rf:ok":
        await q.answer()
        await ref_withdraw(q, uid)


REF_LOCK = asyncio.Lock()


async def ref_withdraw(q, uid: int):
    s = active_signer(uid)
    payer = payout_signer()
    if not s:
        await q.edit_message_text("Сначала создайте кошелёк.")
        return
    if not payer:
        await q.edit_message_text("⏳ Выплаты временно недоступны, попробуйте позже.")
        for a in ADMIN_IDS:
            try:
                await q.get_bot().send_message(a, "⚠️ Запрошен вывод реферального дохода, но ключ кошелька выплат "
                                                  f"{FEE_ACCOUNT} не задан. На сервере: admin.py payout-key")
            except Exception:
                pass
        return
    async with REF_LOCK:  # один вывод за раз: баланс не уйдёт дважды
        bal = db.ref_balance(uid)
        if bal < REF_MIN_WITHDRAW:
            await q.edit_message_text(f"Недостаточно для вывода: {fmt_near(bal)} Ⓝ.")
            return
        if await ref.native_balance(payer.account_id) < bal + YOCTO // 100:
            await q.edit_message_text("⏳ Выплаты временно недоступны, попробуйте позже.")
            return
        pid = db.add_payout(uid, bal, s.account_id)
    await q.edit_message_text("⏳ Отправляю…")
    try:
        res = await ref.withdraw_near(payer, s.account_id, bal)
    except (TxFailed, RpcError) as e:
        db.finish_payout(pid, "failed", getattr(e, "tx_hash", None), str(e)[:200])
        await q.edit_message_text("❌ Вывод не прошёл, средства остались на реферальном балансе. Попробуйте позже.")
        return
    db.finish_payout(pid, "done", res["_hash"])
    await q.edit_message_text(f"✅ Выведено {fmt_near(bal)} Ⓝ на <code>{esc(s.account_id)}</code> · {nb_tx(res['_hash'])}",
                              parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def payout_key_status(payer: Signer | None) -> str:
    if not payer:
        return "❌ не задан (на сервере: admin.py payout-key)"
    try:
        await payer.check_key()
        return "✅ задан и подходит"
    except RpcError as e:
        return f"❌ не подходит к {esc(FEE_ACCOUNT)}: {esc(str(e)[:80])}"


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    st = db.fee_stats()
    payer = payout_signer()
    pbal = await ref.native_balance(FEE_ACCOUNT)
    owed = st["ref_accrued"] - st["ref_paid"]
    lines = ["🛠 <b>Админ</b>",
             f"Комиссия: {FEE_BPS / 100:g}% → <code>{esc(FEE_ACCOUNT)}</code> · рефералам {'/'.join(map(str, REF_SHARES))}%",
             f"Баланс {esc(FEE_ACCOUNT)}: <b>{fmt_near(pbal)} Ⓝ</b>",
             f"Ключ выплат: {await payout_key_status(payer)}", "",
             f"Собрано комиссий: <b>{fmt_near(st['paid'])} Ⓝ</b> · ждут отправки {fmt_near(st['pending'])} · "
             f"не дошли {fmt_near(st['failed'])}",
             f"Начислено рефералам: {fmt_near(st['ref_accrued'])} Ⓝ · выплачено {fmt_near(st['ref_paid'])} · "
             f"<b>к выплате {fmt_near(owed)} Ⓝ</b>",
             f"Чистый доход: <b>{fmt_near(st['paid'] - st['ref_accrued'])} Ⓝ</b>", "",
             f"Пользователей: {st['users']} · пришли по рефке: {st['with_ref']} · сделок с комиссией: {st['trades']}"]
    if payer and pbal < owed:
        lines.append("⚠️ На кошельке меньше, чем должны рефералам — пополните или не выводите.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- поделиться PnL
def hold_fmt(sec: float) -> str:
    sec = int(max(sec, 0))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m {sec % 60}s"


def buy_link(bot_username: str, uid: int, tid: int) -> str:
    """Ссылка на быструю покупку токена с рефералкой: t.me/<bot>?start=r<code>_t<tid>."""
    return f"https://t.me/{bot_username}?start=r{db.ref_code(uid)}_t{tid}"


def share_url(link: str, text: str) -> str:
    from urllib.parse import quote
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


async def on_pnl_share(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """pn:<tid> — новая карточка; pn:<tid>:h / pn:<tid>:s — перерисовать со скрытыми / открытыми суммами."""
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    token = _token_from_cb(parts[1])
    hide = len(parts) > 2 and parts[2] == "h"
    s = active_signer(uid)
    if not token or not s:
        await q.answer("Токен не найден", show_alert=True)
        return
    pos = db.position(s.account_id, token)
    if not pos["open"] or not pos["spent"]:
        await q.answer("Нет открытой позиции по этому токену", show_alert=True)
        return
    await q.answer("🎨 Рисую карточку…")
    tid = db.token_id(token)
    _, sym, dec = db.token_by_id(tid)
    try:
        bal = await ref.ft_balance(token, s.account_id)
        val = await ref.quote_route(await ref.route(token), token, bal, WNEAR) if bal else 0
    except (NoPool, RpcError):
        await q.message.chat.send_message("⏳ Не удалось получить цену, попробуйте через пару секунд.")
        return
    spent, got = pos["spent"], pos["got"]
    worth = got + val
    pct = (worth / spent - 1) * 100
    hold = hold_fmt(time.time() - pos["since"]) if pos["since"] else ""
    code = db.ref_code(uid)
    link = buy_link(ctx.bot.username, uid, tid)
    png = await asyncio.to_thread(pnl_card.render, sym or "?", pct, spent / YOCTO, worth / YOCTO, hold, code,
                                  "NDBOT", hide, None, user_pics(uid), db.get(f"u{uid}:pics_only", False))
    caption = (f"🤝 Делись с друзьями и получай до {REF_SHARES[0] if REF_SHARES else 0}% комиссий!\n"
               f"└ 🔗 {esc(link)}")
    share_text = (f"${sym} {pct:+.1f}% on NDbot 🚀 Buy it in one tap:" if lang_of(uid) == "en"
                  else f"${sym} {pct:+.1f}% в NDbot 🚀 Купить в один клик:")
    kb = KB([[B("📤 Поделиться PnL", url=share_url(link, share_text))],
             [B("👁 Показать суммы", callback_data=f"pn:{tid}:s") if hide
              else B("🙈 Скрыть суммы", callback_data=f"pn:{tid}:h")],
             [B("🙈 Скрыть", callback_data="w:x:0")]])
    if len(parts) > 2 and q.message.photo:
        await q.message.edit_media(InputMediaPhoto(png, caption=caption, parse_mode=ParseMode.HTML), reply_markup=kb)
    else:
        await q.message.chat.send_photo(png, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- свои картинки для PnL (у каждого пользователя свои)
USER_PICS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_pnl")
MAX_USER_PICS = int(os.getenv("MAX_USER_PICS", "5"))
MAX_PIC_BYTES = 8 * 1024 * 1024


def user_pic_dir(uid: int, win: bool) -> str:
    return os.path.join(USER_PICS_DIR, str(int(uid)), "win" if win else "loss")


def user_pics(uid: int) -> dict:
    out = {}
    for win in (True, False):
        d = user_pic_dir(uid, win)
        out[win] = sorted(os.path.join(d, f) for f in os.listdir(d)) if os.path.isdir(d) else []
    return out


def pics_view(uid: int) -> tuple[str, KB]:
    p = user_pics(uid)
    only = db.get(f"u{uid}:pics_only", False)
    text = ("🖼 <b>Мои PnL-картинки</b>\n\nЗагрузите свои картинки — они будут на ваших PnL-карточках "
            "(видны только в ваших карточках).\nЛучше всего — PNG с прозрачным фоном, отправленный файлом; "
            "обычные фото тоже подойдут.\n\n"
            f"📈 Для профита: <b>{len(p[True])}/{MAX_USER_PICS}</b>\n📉 Для минуса: <b>{len(p[False])}/{MAX_USER_PICS}</b>")
    kb = KB([[B("➕ Для профита", callback_data="pi:add:w"), B("➕ Для минуса", callback_data="pi:add:l")],
             [B("✅ Только мои картинки" if only else "🔀 Мои + стандартные", callback_data="pi:mode")],
             [B("🗑 Удалить мои картинки", callback_data="pi:del")]])
    return text, kb


async def on_pics_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    act = parts[1]
    if act == "add":
        win = parts[2] == "w"
        if len(user_pics(uid)[win]) >= MAX_USER_PICS:
            await q.answer(f"Максимум {MAX_USER_PICS} картинок — удалите старые", show_alert=True)
            return
        await q.answer()
        ctx.user_data["await"] = ("pnl_pic", win)
        await q.message.chat.send_message(
            "Пришлите картинку " + ("для профита 📈" if win else "для минуса 📉")
            + " — фото или файлом (PNG с прозрачным фоном выглядит лучше всего).",
            reply_markup=KB([[B("🚫 Отмена", callback_data="lo:pc")]]))
        return
    if act == "mode":
        db.set(f"u{uid}:pics_only", not db.get(f"u{uid}:pics_only", False))
        await q.answer("Сохранено")
    elif act == "del":
        import shutil
        shutil.rmtree(os.path.join(USER_PICS_DIR, str(int(uid))), ignore_errors=True)
        await q.answer("Удалено")
    else:
        await q.answer()
    await edit_or_send(q, *pics_view(uid))


def _save_pic(raw: bytes, path: str):
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.width * img.height > 40_000_000:
        raise ValueError("слишком большое изображение")
    img = img.convert("RGBA") if (img.mode in ("RGBA", "LA", "P")) else img.convert("RGB")
    img.thumbnail((900, 900))
    img.save(path, format="PNG")


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pending = ctx.user_data.get("await")
    if not pending or pending[0] != "pnl_pic":
        return
    msg = update.message
    if msg.document:
        if not (msg.document.mime_type or "").startswith("image/") or (msg.document.file_size or 0) > MAX_PIC_BYTES:
            await msg.reply_text("❌ Нужна картинка (PNG, JPG или WEBP) до 8 МБ.")
            return
        f = await msg.document.get_file()
    elif msg.photo:
        f = await msg.photo[-1].get_file()
    else:
        return
    ctx.user_data.pop("await", None)
    win = pending[1]
    d = user_pic_dir(uid, win)
    os.makedirs(d, exist_ok=True)
    if len(os.listdir(d)) >= MAX_USER_PICS:
        await msg.reply_text(f"Максимум {MAX_USER_PICS} картинок — удалите старые.")
        return
    raw = bytes(await f.download_as_bytearray())
    path = os.path.join(d, f"{int(time.time() * 1000)}.png")
    try:
        await asyncio.to_thread(_save_pic, raw, path)
    except Exception:
        await msg.reply_text("❌ Не получилось открыть картинку. Попробуйте другой файл.")
        return
    text, kb = pics_view(uid)
    await msg.reply_text("✅ Картинка добавлена.\n\n" + text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- позиции
async def show_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await need_wallet(update)
    if not s:
        return
    acc = s.account_id
    rows, kb = [], []
    for token in db.traded_tokens(acc)[:15]:
        try:
            bal = await ref.ft_balance(token, acc)
        except RpcError:
            rows.append(f"<b>{esc(token)}</b> · ⚠️ сеть перегружена, обновите позже")
            continue
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
    return KB([row[:3], row[3:], [B("✏️ Пресеты покупки: " + " / ".join(presets(uid)), callback_data="pr")],
               [B("🖼 Мои PnL-картинки", callback_data="pi:v")],
               [B("🌐 Язык: Русский", callback_data="lang")]])


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
    elif q.data == "lang":
        new = "en" if lang_of(uid) == "ru" else "ru"
        db.set(f"u{uid}:lang", new)
        CUR_LANG.set(new)
        await q.answer()
        await q.edit_message_reply_markup(settings_kb(uid))
        await q.message.chat.send_message("✅ Язык: русский", reply_markup=MENU)  # обновить кнопки меню
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


MENU_BUTTONS = {"💼 Кошелёк": show_wallet, "📊 Позиции": show_positions, "⚙️ Настройки": show_settings,
                "↗️ Вывод": cmd_withdraw, "📋 Ордера": show_orders, "🤝 Рефералы": show_ref}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = i18n.from_en(update.message.text or "")
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
        elif kind == "lo_token":
            await order_token_input(update, ctx, text)
        elif kind in ("lo_amt", "lo_name", "lo_price"):
            await order_input(update, ctx, kind, text)
        elif kind == "tr_token":
            await transfer_token_addr(update, ctx, pending[1], text)
        elif kind == "tr_to":
            await transfer_to(update, ctx, pending[1], pending[2], text)
        elif kind == "tr_amt":
            await transfer_amount(update, ctx, pending[1], pending[2], pending[3], text)
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
    cmds = [("start", "Главное меню"), ("wallet", "Кошельки"), ("positions", "Позиции"), ("orders", "Лимит-ордера"),
            ("withdraw", "Вывод средств"), ("ref", "Рефералы"), ("settings", "Настройки")]
    await app.bot.set_my_commands([(c, i18n.to_en(d)) for c, d in cmds])
    for lc in ("ru", "uk", "be"):
        await app.bot.set_my_commands(cmds, language_code=lc)
    db.reset_stuck_orders()


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    # в лог пишем только тип и текст ошибки, без содержимого сообщений
    log.error("handler error: %s: %s", type(ctx.error).__name__, str(ctx.error)[:300])
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("⚠️ Внутренняя ошибка, попробуйте ещё раз.")
        except Exception:
            pass


def main():
    app = (Application.builder().bot(I18nBot(BOT_TOKEN)).post_init(post_init).concurrent_updates(True).build())
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("wallet", show_wallet))
    app.add_handler(CommandHandler("positions", show_positions))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("orders", show_orders))
    app.add_handler(CommandHandler("ref", show_ref))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_ref_cb, pattern=r"^rf:"))
    app.add_handler(CallbackQueryHandler(on_pnl_share, pattern=r"^pn:"))
    app.add_handler(CallbackQueryHandler(on_order_cb, pattern=r"^lo:"))
    app.add_handler(CallbackQueryHandler(on_cancel_cb, pattern=r"^w:x:"))
    app.add_handler(CallbackQueryHandler(on_wallet_cb, pattern=r"^w:"))
    app.add_handler(CallbackQueryHandler(on_transfer_cb, pattern=r"^tr:"))
    app.add_handler(CallbackQueryHandler(on_asset_cb, pattern=r"^wa:"))
    app.add_handler(CallbackQueryHandler(on_trade_cb, pattern=r"^(b|bx|s|sx|c):"))
    app.add_handler(CallbackQueryHandler(on_settings_cb, pattern=r"^(sl:|pr$|lang$)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_media))
    app.add_handler(CallbackQueryHandler(on_pics_cb, pattern=r"^pi:"))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_orders, interval=ORDER_POLL, first=20)
    app.job_queue.run_repeating(retry_fees, interval=120, first=60)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
