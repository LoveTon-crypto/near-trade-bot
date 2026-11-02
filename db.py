"""SQLite: users, wallets, per-user settings, trades, short token ids for buttons."""
from __future__ import annotations

import json
import sqlite3
import time


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT UNIQUE,
                                           symbol TEXT, decimals INTEGER);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, token TEXT,
                                           side TEXT, token_amt TEXT, near_amt TEXT, tx TEXT);
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, active_wallet INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                                            name TEXT, account TEXT NOT NULL, secret BLOB NOT NULL,
                                            has_phrase INTEGER, created INTEGER,
                                            UNIQUE(user_id, account));
        """)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            wallet_id INTEGER NOT NULL, token TEXT NOT NULL, side TEXT NOT NULL, cond TEXT NOT NULL,
            unit TEXT NOT NULL, target REAL NOT NULL, amount TEXT, pct REAL, slippage_bps INTEGER NOT NULL,
            name TEXT, status TEXT NOT NULL DEFAULT 'active', created INTEGER, done_ts INTEGER,
            tx TEXT, note TEXT);
        CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
        """)
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS fees (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER,
            wallet_id INTEGER, side TEXT, amount TEXT NOT NULL, swap_tx TEXT, tx TEXT,
            status TEXT NOT NULL DEFAULT 'pending', tries INTEGER DEFAULT 0, note TEXT);
        CREATE TABLE IF NOT EXISTS ref_earn (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER,
            from_user INTEGER, level INTEGER, amount TEXT NOT NULL, fee_id INTEGER);
        CREATE TABLE IF NOT EXISTS ref_payouts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER,
            amount TEXT NOT NULL, to_account TEXT, tx TEXT, status TEXT NOT NULL DEFAULT 'pending', note TEXT);
        CREATE INDEX IF NOT EXISTS fees_status ON fees(status);
        CREATE INDEX IF NOT EXISTS ref_earn_user ON ref_earn(user_id);
        """)
        ucols = {r[1] for r in self.c.execute("PRAGMA table_info(users)")}
        if "ref_code" not in ucols:
            self.c.execute("ALTER TABLE users ADD COLUMN ref_code TEXT")
            self.c.execute("ALTER TABLE users ADD COLUMN referrer INTEGER")
            self.c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_ref_code ON users(ref_code)")
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(trades)")}
        if "account" not in cols:  # trades from the single-wallet version
            self.c.execute("ALTER TABLE trades ADD COLUMN account TEXT")
        self.c.commit()

    # settings (global or per user: key "u{id}:name")
    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v)))
        self.c.commit()

    # users & wallets
    def ensure_user(self, uid: int):
        self.c.execute("INSERT OR IGNORE INTO users (id, created) VALUES (?,?)", (uid, int(time.time())))
        self.c.commit()

    def wallets(self, uid: int) -> list[tuple]:
        """[(id, name, account, has_phrase)] oldest first."""
        return self.c.execute("SELECT id, name, account, has_phrase FROM wallets WHERE user_id=? ORDER BY id",
                              (uid,)).fetchall()

    def wallet(self, uid: int, wid: int):
        """(id, name, account, secret, has_phrase) — only if it belongs to this user."""
        return self.c.execute("SELECT id, name, account, secret, has_phrase FROM wallets WHERE id=? AND user_id=?",
                              (wid, uid)).fetchone()

    def add_wallet(self, uid: int, name: str, account: str, secret: bytes, has_phrase: bool) -> int:
        cur = self.c.execute("INSERT INTO wallets (user_id, name, account, secret, has_phrase, created) "
                             "VALUES (?,?,?,?,?,?)", (uid, name, account, secret, int(has_phrase), int(time.time())))
        wid = cur.lastrowid
        if not self.active_wallet(uid):
            self.set_active(uid, wid)
        self.c.commit()
        return wid

    def rename_wallet(self, uid: int, wid: int, name: str):
        self.c.execute("UPDATE wallets SET name=? WHERE id=? AND user_id=?", (name, wid, uid))
        self.c.commit()

    def delete_wallet(self, uid: int, wid: int):
        self.c.execute("DELETE FROM wallets WHERE id=? AND user_id=?", (wid, uid))
        if self.active_wallet(uid) == wid:
            rest = self.wallets(uid)
            self.c.execute("UPDATE users SET active_wallet=? WHERE id=?", (rest[0][0] if rest else None, uid))
        self.c.commit()

    def active_wallet(self, uid: int) -> int | None:
        r = self.c.execute("SELECT active_wallet FROM users WHERE id=?", (uid,)).fetchone()
        return r[0] if r else None

    def set_active(self, uid: int, wid: int):
        self.c.execute("UPDATE users SET active_wallet=? WHERE id=?", (wid, uid))
        self.c.commit()

    def has_account(self, uid: int, account: str) -> bool:
        return bool(self.c.execute("SELECT 1 FROM wallets WHERE user_id=? AND account=?", (uid, account)).fetchone())

    # tokens
    def token_id(self, address: str, symbol: str = "", decimals: int = 0) -> int:
        self.c.execute("INSERT OR IGNORE INTO tokens (address, symbol, decimals) VALUES (?,?,?)",
                       (address, symbol, decimals))
        if symbol:
            self.c.execute("UPDATE tokens SET symbol=?, decimals=? WHERE address=?", (symbol, decimals, address))
        self.c.commit()
        return self.c.execute("SELECT id FROM tokens WHERE address=?", (address,)).fetchone()[0]

    def token_by_id(self, tid: int):
        return self.c.execute("SELECT address, symbol, decimals FROM tokens WHERE id=?", (tid,)).fetchone()

    # trades (per wallet account)
    def add_trade(self, account, token, side, token_amt: int, near_amt: int, tx: str):
        self.c.execute("INSERT INTO trades (ts, account, token, side, token_amt, near_amt, tx) VALUES (?,?,?,?,?,?,?)",
                       (int(time.time()), account, token, side, str(token_amt), str(near_amt), tx))
        self.c.commit()

    def traded_tokens(self, account) -> list[str]:
        return [r[0] for r in self.c.execute(
            "SELECT token FROM trades WHERE account=? GROUP BY token ORDER BY MAX(ts) DESC", (account,))]

    def position(self, account, token) -> dict:
        """Текущая позиция по токену: считается с момента, когда токен был куплен после полного выхода.
        Если всё продано (осталось < 1% от максимума), следующая покупка начинает новую позицию с нуля."""
        spent = got = held = peak = 0
        since = None
        for ts, side, tok, near in self.c.execute(
                "SELECT ts, side, token_amt, near_amt FROM trades WHERE account=? AND token=? ORDER BY ts, id",
                (account, token)):
            tok, near = int(tok), int(near)
            if side == "buy":
                if held <= 0:
                    spent = got = held = peak = 0
                    since = ts
                spent += near
                held += tok
                peak = max(peak, held)
            else:
                got += near
                held -= tok
                if held <= peak // 100:
                    held = 0
        return {"spent": spent, "got": got, "since": since, "open": held > 0}

    def totals(self, account, token) -> tuple[int, int]:
        """(NEAR вложено, NEAR получено) по текущей позиции; после полного выхода — (0, 0)."""
        p = self.position(account, token)
        return (p["spent"], p["got"]) if p["open"] else (0, 0)

    # limit orders
    #   side: buy|sell · cond: le (цена ≤ цели) | ge (цена ≥ цели)
    #   unit: near (цена токена в NEAR) | usd (цена в $) | mc (капитализация в $) · target — в этой единице
    #   amount: NEAR (yocto) для покупки · pct: % баланса токена для продажи
    def add_order(self, uid, wid, token, side, cond, unit, target, amount, pct, slippage_bps, name) -> int:
        cur = self.c.execute(
            "INSERT INTO orders (user_id, wallet_id, token, side, cond, unit, target, amount, pct, slippage_bps, name,"
            " status, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (uid, wid, token, side, cond, unit, float(target), str(amount) if amount else None, pct, slippage_bps,
             name, int(time.time())))
        self.c.commit()
        return cur.lastrowid

    _ORDER_COLS = "id, user_id, wallet_id, token, side, cond, unit, target, amount, pct, slippage_bps, name, status, created, tx, note"

    def _order_row(self, r):
        return dict(zip([c.strip() for c in self._ORDER_COLS.split(",")], r)) if r else None

    def order(self, uid, oid):
        return self._order_row(self.c.execute(f"SELECT {self._ORDER_COLS} FROM orders WHERE id=? AND user_id=?",
                                              (oid, uid)).fetchone())

    def user_orders(self, uid, active_only=True, limit=20):
        q = f"SELECT {self._ORDER_COLS} FROM orders WHERE user_id=?" + (" AND status='active'" if active_only else "")
        return [self._order_row(r) for r in self.c.execute(q + " ORDER BY id DESC LIMIT ?", (uid, limit))]

    def active_orders(self):
        return [self._order_row(r) for r in self.c.execute(
            f"SELECT {self._ORDER_COLS} FROM orders WHERE status='active' ORDER BY id")]

    def claim_order(self, oid) -> bool:
        """active → executing атомарно: один ордер не исполнится дважды."""
        cur = self.c.execute("UPDATE orders SET status='executing' WHERE id=? AND status='active'", (oid,))
        self.c.commit()
        return cur.rowcount == 1

    def finish_order(self, oid, status, tx=None, note=None):
        self.c.execute("UPDATE orders SET status=?, done_ts=?, tx=?, note=? WHERE id=?",
                       (status, int(time.time()), tx, note, oid))
        self.c.commit()

    def cancel_order(self, uid, oid) -> bool:
        cur = self.c.execute("UPDATE orders SET status='cancelled', done_ts=? WHERE id=? AND user_id=? AND status='active'",
                             (int(time.time()), oid, uid))
        self.c.commit()
        return cur.rowcount == 1

    def cancel_all_orders(self, uid) -> int:
        cur = self.c.execute("UPDATE orders SET status='cancelled', done_ts=? WHERE user_id=? AND status='active'",
                             (int(time.time()), uid))
        self.c.commit()
        return cur.rowcount

    def reset_stuck_orders(self):
        """После перезапуска: ордера, которые были в процессе исполнения, помечаем — проверить вручную."""
        self.c.execute("UPDATE orders SET status='failed', note='бот перезапустился во время исполнения — проверьте "
                       "баланс' WHERE status='executing'")
        self.c.commit()

    # ------------------------------------------------------------ рефералы
    def ref_code(self, uid: int) -> str:
        r = self.c.execute("SELECT ref_code FROM users WHERE id=?", (uid,)).fetchone()
        if r and r[0]:
            return r[0]
        import secrets
        while True:
            code = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
            try:
                self.c.execute("UPDATE users SET ref_code=? WHERE id=?", (code, uid))
                self.c.commit()
                return code
            except sqlite3.IntegrityError:
                continue

    def user_by_code(self, code: str) -> int | None:
        r = self.c.execute("SELECT id FROM users WHERE ref_code=?", (code,)).fetchone()
        return r[0] if r else None

    def referrer(self, uid: int) -> int | None:
        r = self.c.execute("SELECT referrer FROM users WHERE id=?", (uid,)).fetchone()
        return r[0] if r else None

    def is_fresh(self, uid: int) -> bool:
        """Новичок: ещё нет пригласившего, кошельков и сделок — только такого можно закрепить за рефералом."""
        if self.referrer(uid) or self.wallets(uid):
            return False
        return not self.c.execute("SELECT 1 FROM fees WHERE user_id=? LIMIT 1", (uid,)).fetchone()

    def set_referrer(self, uid: int, ref_uid: int) -> bool:
        if uid == ref_uid:
            return False
        up, seen = ref_uid, set()
        while up and up not in seen:  # без циклов: пригласивший не может быть в моей же цепочке
            if up == uid:
                return False
            seen.add(up)
            up = self.referrer(up)
        self.c.execute("UPDATE users SET referrer=? WHERE id=? AND referrer IS NULL", (ref_uid, uid))
        self.c.commit()
        return True

    def upline(self, uid: int, levels: int = 3) -> list[int]:
        out, cur = [], uid
        for _ in range(levels):
            cur = self.referrer(cur)
            if not cur or cur in out or cur == uid:
                break
            out.append(cur)
        return out

    def referral_counts(self, uid: int) -> list[int]:
        l1 = [r[0] for r in self.c.execute("SELECT id FROM users WHERE referrer=?", (uid,))]
        l2 = [r[0] for r in self.c.execute(f"SELECT id FROM users WHERE referrer IN ({','.join('?' * len(l1))})", l1)] if l1 else []
        l3 = self.c.execute(f"SELECT COUNT(*) FROM users WHERE referrer IN ({','.join('?' * len(l2))})", l2).fetchone()[0] if l2 else 0
        return [len(l1), len(l2), l3]

    def ref_earned(self, uid: int) -> dict[int, int]:
        out = {1: 0, 2: 0, 3: 0}
        for lvl, amt in self.c.execute("SELECT level, amount FROM ref_earn WHERE user_id=?", (uid,)):
            out[lvl] = out.get(lvl, 0) + int(amt)
        return out

    def ref_paid(self, uid: int) -> int:
        """Выплачено + в процессе (pending не даём вывести повторно)."""
        return sum(int(a) for (a,) in self.c.execute(
            "SELECT amount FROM ref_payouts WHERE user_id=? AND status IN ('done','pending')", (uid,)))

    def ref_balance(self, uid: int) -> int:
        return sum(self.ref_earned(uid).values()) - self.ref_paid(uid)

    def add_payout(self, uid, amount: int, to_account: str) -> int:
        cur = self.c.execute("INSERT INTO ref_payouts (ts, user_id, amount, to_account, status) VALUES (?,?,?,?, 'pending')",
                             (int(time.time()), uid, str(amount), to_account))
        self.c.commit()
        return cur.lastrowid

    def finish_payout(self, pid, status, tx=None, note=None):
        self.c.execute("UPDATE ref_payouts SET status=?, tx=?, note=? WHERE id=?", (status, tx, note, pid))
        self.c.commit()

    # ------------------------------------------------------------ комиссия бота
    def add_fee(self, uid, wid, side, amount: int, swap_tx) -> int:
        cur = self.c.execute("INSERT INTO fees (ts, user_id, wallet_id, side, amount, swap_tx, status) VALUES (?,?,?,?,?,?,'pending')",
                             (int(time.time()), uid, wid, side, str(amount), swap_tx))
        self.c.commit()
        return cur.lastrowid

    def fee_paid(self, fee_id, tx, shares: list[tuple[int, int, int]]):
        """Комиссия дошла до админского кошелька → начисляем рефералам их доли. shares: [(user_id, level, amount)]."""
        row = self.c.execute("SELECT user_id, status FROM fees WHERE id=?", (fee_id,)).fetchone()
        if not row or row[1] == "paid":
            return
        self.c.execute("UPDATE fees SET status='paid', tx=? WHERE id=?", (tx, fee_id))
        for earner, lvl, amt in shares:
            if amt > 0:
                self.c.execute("INSERT INTO ref_earn (ts, user_id, from_user, level, amount, fee_id) VALUES (?,?,?,?,?,?)",
                               (int(time.time()), earner, row[0], lvl, str(amt), fee_id))
        self.c.commit()

    def fee_failed(self, fee_id, note, give_up=False):
        self.c.execute("UPDATE fees SET tries=tries+1, note=?, status=? WHERE id=?",
                       (note[:200], "failed" if give_up else "pending", fee_id))
        self.c.commit()

    def pending_fees(self, max_tries=20):
        return self.c.execute("SELECT id, user_id, wallet_id, amount, tries FROM fees WHERE status='pending' AND tries<?",
                              (max_tries,)).fetchall()

    def fee_stats(self) -> dict:
        def tot(q, *a):
            return sum(int(x) for (x,) in self.c.execute(q, a))
        return {"paid": tot("SELECT amount FROM fees WHERE status='paid'"),
                "pending": tot("SELECT amount FROM fees WHERE status='pending'"),
                "failed": tot("SELECT amount FROM fees WHERE status='failed'"),
                "ref_accrued": tot("SELECT amount FROM ref_earn"),
                "ref_paid": tot("SELECT amount FROM ref_payouts WHERE status='done'"),
                "users": self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "with_ref": self.c.execute("SELECT COUNT(*) FROM users WHERE referrer IS NOT NULL").fetchone()[0],
                "trades": self.c.execute("SELECT COUNT(*) FROM fees").fetchone()[0]}
