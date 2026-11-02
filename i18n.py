"""English for the bot: every user-facing text is translated on the way out (see I18nBot in bot.py).

Keys are the exact Russian fragments from the code (f-string pieces split at variables and line breaks);
tests/test_i18n.py checks that every user-facing Cyrillic fragment of the code has a translation here.
"""
from __future__ import annotations

import re

EN: dict[str, str] = {
    # units, statuses, menu
    "в NEAR": "in NEAR", "в %": "in %", "в $": "in $", "в MC $": "in MC $",
    "⏳ активен": "⏳ active", "⚙️ исполняется": "⚙️ executing", "✅ исполнен": "✅ filled",
    "❌ не исполнен": "❌ not filled", "🚫 отменён": "🚫 cancelled",
    "💼 Кошелёк": "💼 Wallet", "📊 Позиции": "📊 Positions", "⚙️ Настройки": "⚙️ Settings", "↗️ Вывод": "↗️ Withdraw",
    "📋 Ордера": "📋 Orders", "🤝 Рефералы": "🤝 Referrals",
    "Главное меню": "Main menu", "Кошельки": "Wallets", "Позиции": "Positions", "Лимит-ордера": "Limit orders",
    "Вывод средств": "Withdraw funds", "Рефералы": "Referrals", "Настройки": "Settings",

    # start / wallets
    "<b>NEAR Trade</b> — покупка и продажа токенов NEAR прямо в Telegram.":
        "<b>NEAR Trade</b> — buy and sell NEAR tokens right in Telegram.",
    "Для начала создайте новый кошелёк или импортируйте свой по сид-фразе.":
        "To start, create a new wallet or import yours with a seed phrase.",
    "👛 Кошелёк:": "👛 Wallet:", "Кошелёк: <code>": "Wallet: <code>", "Баланс: ": "Balance: ",
    "Отправьте адрес токена или ссылку nearly / justhoot / nearblocks, чтобы торговать.":
        "Send a token address or a nearly / justhoot / nearblocks link to trade.",
    "Сначала создайте или импортируйте кошелёк: 💼 Кошелёк": "Create or import a wallet first: 💼 Wallet",
    "➕ Создать": "➕ Create", "📥 Импорт": "📥 Import",
    "👛 <b>Кошельки</b>": "👛 <b>Wallets</b>", "👛 <b>Кошельки</b> (": "👛 <b>Wallets</b> (",
    "Кошельков пока нет. Создайте новый или импортируйте свой по сид-фразе.":
        "No wallets yet. Create a new one or import yours with a seed phrase.",
    "✅ — основной, с него идут сделки.": "✅ — main wallet, trades go from it.",
    "📭 <b>Адрес</b>": "📭 <b>Address</b>", " · ✅ основной": " · ✅ main",
    "📋 Скопировать адрес": "📋 Copy address", "💰 Пополнить": "💰 Deposit", "Вывод ↗️": "Withdraw ↗️",
    "Изменить имя ✍️": "Rename ✍️", "⬅️ Назад": "⬅️ Back", "Удалить 🗑": "Delete 🗑",
    "✅ Основной": "✅ Main", "⭐ Сделать основным": "⭐ Make main", "Сид-фраза 🌱": "Seed phrase 🌱",
    "Приватный ключ 🔑": "Private key 🔑",
    "🌱 <b>Сид-фраза</b> (12 слов):": "🌱 <b>Seed phrase</b> (12 words):",
    "🔑 <b>Приватный ключ</b>:": "🔑 <b>Private key</b>:",
    "Запишите и храните офлайн. Никому не отправляйте — с ней можно забрать все средства.":
        "Write it down and keep it offline. Never send it to anyone — it gives full access to your funds.",
    "<i>Сообщение удалится через ": "<i>This message will be deleted in ", " мин.</i>": " min.</i>",
    "этот кошелёк уже добавлен": "this wallet is already added", "Кошелёк ": "Wallet ",
    "✅ <b>Кошелёк создан.</b> Сохраните сид-фразу прямо сейчас:":
        "✅ <b>Wallet created.</b> Save your seed phrase right now:",
    "Пополните адрес NEAR, чтобы кошелёк активировался в сети. Сид-фраза подходит для Meteor, MyNearWallet и других NEAR-кошельков.":
        "Send NEAR to this address to activate the wallet on-chain. The seed phrase works in Meteor, MyNearWallet and other NEAR wallets.",
    "Ключ подходит к этим аккаунтам. Какой добавить?": "This key controls these accounts. Which one to add?",
    "✅ <b>Кошелёк импортирован.</b>": "✅ <b>Wallet imported.</b>",
    "Отправьте сид-фразу (12 или 24 слова) или приватный ключ <code>ed25519:…</code>.":
        "Send your seed phrase (12 or 24 words) or private key <code>ed25519:…</code>.",
    "Сообщение я сразу удалю из чата.": "I'll delete the message from the chat right away.",
    ". Импорт отменён, попробуйте ещё раз: 💼 Кошелёк → 📥 Импорт": ". Import cancelled, try again: 💼 Wallet → 📥 Import",
    "❌ Ключ не подходит к ": "❌ The key doesn't match ", "ключ без полного доступа": "key has no full access",
    "Максимум ": "Maximum ", " кошельков": " wallets", " кошельков.": " wallets.",
    "Импорт устарел, начните заново.": "This import has expired, start again.",
    "Кошелёк не найден": "Wallet not found", "Кошелёк не найден.": "Wallet not found.",
    "Теперь сделки идут с этого кошелька": "Trades now go from this wallet",
    "💰 <b>Пополнение</b>": "💰 <b>Deposit</b>", "Отправьте NEAR на адрес:": "Send NEAR to this address:",
    "С биржи (Binance, OKX, Bybit…) выбирайте сеть <b>NEAR</b>. Новый кошелёк появится в сети после первого пополнения — для этого хватит 0.1 NEAR.":
        "When withdrawing from an exchange (Binance, OKX, Bybit…) choose the <b>NEAR</b> network. A new wallet appears on-chain after the first deposit — 0.1 NEAR is enough.",
    "Новое имя для «": "New name for «", "» (до 24 символов):": "» (up to 24 characters):",
    "Пустое имя, отменено.": "Empty name, cancelled.",
    "⚠️ Показать ": "⚠️ Show the ", " кошелька «": " of wallet «", "сид-фразу": "seed phrase", "приватный ключ": "private key",
    "Кто её видит — может забрать все средства. Убедитесь, что рядом никого нет и экран не записывается.":
        "Anyone who sees it can take all the funds. Make sure nobody is around and the screen isn't being recorded.",
    "👁 Показать": "👁 Show", "Отмена": "Cancel", "Отменено": "Cancelled",
    "🗑 Удалить кошелёк «": "🗑 Delete wallet «", "» из бота?": "» from the bot?", "На нём <b>": "It holds <b>",
    "Средства останутся в сети, но доступ к ним будет только по сид-фразе или ключу. Сохранили их?":
        "The funds stay on-chain, but you'll only be able to access them with the seed phrase or key. Did you save it?",
    "Да, удалить": "Yes, delete", "Удалён": "Deleted",
    "🙈 Похоже на сид-фразу или ключ — я удалил сообщение. Чтобы добавить кошелёк: 💼 Кошелёк → 📥 Импорт.":
        "🙈 That looks like a seed phrase or key — I deleted the message. To add a wallet: 💼 Wallet → 📥 Import.",

    # withdraw
    "↗️ <b>Вывод</b> с <code>": "↗️ <b>Withdraw</b> from <code>", "↗️ <b>Вывод</b> с «": "↗️ <b>Withdraw</b> from «",
    "Что выводим?": "What do you want to withdraw?", "↗️ Вывод ": "↗️ Withdraw ",
    "➕ Другой токен (адрес контракта)": "➕ Another token (contract address)",
    "Отправьте адрес получателя (<code>имя.near</code> или 64 символа):":
        "Send the recipient address (<code>name.near</code> or 64 characters):",
    "Отправьте адрес контракта токена (например, batman-4.nearlytrade.near):":
        "Send the token contract address (e.g. batman-4.nearlytrade.near):",
    "Токен не найден.": "Token not found.", "Токен не найден": "Token not found",
    "❌ Это не адрес токена. Вывод отменён.": "❌ That's not a token address. Withdrawal cancelled.",
    "❌ Это не токен NEAR (нет ft_metadata). Вывод отменён.": "❌ That's not a NEAR token (no ft_metadata). Withdrawal cancelled.",
    "❌ Неверный адрес. Вывод отменён.": "❌ Invalid address. Withdrawal cancelled.",
    "❌ Аккаунта ": "❌ Account ", " не существует. Вывод отменён.": " doesn't exist. Withdrawal cancelled.",
    "❌ Это тот же кошелёк.": "❌ That's the same wallet.",
    "Сколько $": "How many $", " отправить? Доступно: <b>": " to send? Available: <b>",
    "Сколько NEAR отправить? Доступно: <b>": "How much NEAR to send? Available: <b>",
    "Число, процент (<code>50%</code>) или <code>all</code> — всё.": "A number, a percent (<code>50%</code>) or <code>all</code>.",
    "Число, например <code>1.5</code>, процент (<code>50%</code>) или <code>all</code> — всё.":
        "A number like <code>1.5</code>, a percent (<code>50%</code>) or <code>all</code>.",
    "❌ Неверная сумма. Доступно ": "❌ Invalid amount. Available ",
    "❌ Нужно хотя бы 0.01 NEAR на газ, а на кошельке ": "❌ You need at least 0.01 NEAR for gas, the wallet has ",
    "❌ Неверная сумма или не хватает на газ. Доступно ": "❌ Invalid amount or not enough for gas. Available ",
    "Вывести <b>": "Withdraw <b>", "с <code>": "from <code>", "на <code>": "to <code>",
    "Проверьте адрес: перевод в блокчейне нельзя отменить.": "Double-check the address: blockchain transfers can't be undone.",
    "✅ Отправить": "✅ Send", "Вывод отменён.": "Withdrawal cancelled.", "⏳ Отправляю…": "⏳ Sending…",
    "✅ Отправлено ": "✅ Sent ", " на <code>": " to <code>", "❌ Ошибка: ": "❌ Error: ",

    # token card / trading
    "% за 24ч": "% 24h", "💲 Цена: <b>": "💲 Price: <b>", "🔀 Пул: ": "🔀 Pool: ", "📊 Объём 24ч: ": "📊 Volume 24h: ", " · сделок ": " · trades ",
    "⚠️ Пула с NEAR пока нет ни на Ref, ни на Ref DCL — купить токен пока нельзя.":
        "⚠️ No NEAR pool on Ref or Ref DCL yet — the token can't be bought yet.",
    "👛 У вас: <b>": "👛 You hold: <b>", "📥 Вложено: <b>": "📥 Invested: <b>", "💰 Текущая стоимость: <b>": "💰 Current value: <b>",
    " Ⓝ · ⚙️ слиппедж ": " Ⓝ · ⚙️ slippage ", "📋 Лимит-ордер": "📋 Limit order", "🔄 Обновить": "🔄 Refresh",
    "⏳ Сеть NEAR сейчас перегружена запросами, карточка не загрузилась. Нажмите «Обновить» через пару секунд.":
        "⏳ The NEAR network is busy right now and the card didn't load. Tap «Refresh» in a couple of seconds.",
    "Не удалось прочитать токен: ": "Couldn't read the token: ", "Обновлено": "Updated",
    "Больше лимита ": "Above the limit of ", " NEAR на одну покупку.": " NEAR per buy.", " NEAR на одну покупку": " NEAR per buy",
    "Недостаточно NEAR: баланс ": "Not enough NEAR: balance ", ", резерв на газ ": ", gas reserve ",
    "Сделка по этому токену уже выполняется.": "A trade for this token is already in progress.",
    "⏳ Покупаю на ": "⏳ Buying for ", "✅ Куплено <b>": "✅ Bought <b>", "</b> за ": "</b> for ",
    "❌ Нет пула с NEAR ни на Ref, ни на Ref DCL для этого токена.": "❌ This token has no NEAR pool on Ref or Ref DCL.",
    "❌ Покупка не прошла: ": "❌ Buy failed: ", "Нечего продавать.": "Nothing to sell.", "⏳ Продаю ": "⏳ Selling ",
    "✅ Продано ": "✅ Sold ", "❌ Продажа не прошла: ": "❌ Sell failed: ",
    "Сколько NEAR потратить? Например: 1.5": "How much NEAR to spend? E.g. 1.5", "Какой % продать? Например: 30": "What % to sell? E.g. 30",
    "Неверное число, отменено.": "Invalid number, cancelled.", "Максимум 100%.": "Maximum 100%.",
    "Отправьте адрес токена (например, batman-4.nearlytrade.near) или ссылку на него.":
        "Send a token address (e.g. batman-4.nearlytrade.near) or a link to it.",

    # positions / settings
    "Открытых позиций на ": "No open positions on ", " нет.": ".", "📊 <b>Позиции</b> · ": "📊 <b>Positions</b> · ",
    "</b> · ⚠️ сеть перегружена, обновите позже": "</b> · ⚠️ network busy, refresh later",
    "⚙️ <b>Настройки</b>": "⚙️ <b>Settings</b>", "Слиппедж:": "Slippage:", "✏️ Пресеты покупки: ": "✏️ Buy presets: ",
    "Отправьте 4 суммы в NEAR через пробел, например: 0.5 1 2 5": "Send 4 NEAR amounts separated by spaces, e.g.: 0.5 1 2 5",
    "Нужно ровно 4 положительных числа.": "Exactly 4 positive numbers are needed.", "✅ Пресеты сохранены.": "✅ Presets saved.",
    "Сохранено": "Saved", "⚠️ Внутренняя ошибка, попробуйте ещё раз.": "⚠️ Internal error, please try again.",

    # limit orders
    "⚪ Лимит на просадке (−%)": "⚪ Buy the dip (−%)", "⚪ Тейк-профит (+%)": "⚪ Take profit (+%)",
    "🚀 Покупка на пробое (+%)": "🚀 Breakout buy (+%)", "🛑 Стоп-лосс (−%)": "🛑 Stop loss (−%)",
    "Лимит на просадке (−%)": "Buy the dip (−%)", "Тейк-профит (+%)": "Take profit (+%)",
    "Покупка на пробое (+%)": "Breakout buy (+%)", "Стоп-лосс (−%)": "Stop loss (−%)",
    "📈 Покупка": "📈 Buy", "📉 Продажа": "📉 Sell", "Стоп-лосс": "Stop loss", "Тейк-профит": "Take profit",
    "📋 <b>Лимит-ордер</b> · <b>$": "📋 <b>Limit order</b> · <b>$", "💲 Сейчас: ": "💲 Now: ", "🧬 Тип: <b>": "🧬 Type: <b>",
    "🎯 Сработает, когда цена ": "🎯 Fires when the price is ", " цели (": " the target (", "% от текущей)": "% from now)",
    "💰 Лимитная цена: ≈": "💰 Limit price: ≈", "📈 Лимитная MC: ≈": "📈 Limit MC: ≈",
    "⚠️ Цена уже на цели — ордер исполнится сразу после создания.": "⚠️ The price is already at the target — the order will fill right after creation.",
    "🎯 Цена не задана — нажмите «Цена»": "🎯 No price set — tap «Price»",
    "Ⓝ Сумма: <b>": "Ⓝ Amount: <b>", "📊 Продать: <b>": "📊 Sell: <b>",
    "%</b> баланса токена в момент срабатывания": "%</b> of the token balance when it fires",
    "🏷 Название: ": "🏷 Name: ", "не задано": "not set", "нет кошелька": "no wallet", "👛 <b>Баланс</b> ": "👛 <b>Balance</b> ",
    "💰 Цена: ": "💰 Price: ", "Ⓝ Сумма ": "Ⓝ Amount ", "📊 Процент ": "📊 Percent ",
    "✅ Создать ордер": "✅ Create order", "💬 Введите цену": "💬 Enter price", "🚫 Отмена": "🚫 Cancel",
    "💬 Введи ": "💬 Enter ", ", при которой ": " at which to ", " токен, или выбери кнопку:": " the token, or pick a button:",
    "купить": "buy", "продать": "sell",
    "изменение цены в % от текущей (например, ": "the price change in % from now (e.g. ",
    "цену 1 токена в NEAR (например, 0.0005)": "the price of 1 token in NEAR (e.g. 0.0005)",
    "цену 1 токена в $ (например, 0.00012)": "the price of 1 token in $ (e.g. 0.00012)",
    "капитализацию в $ (например, 50000 или 50k)": "the market cap in $ (e.g. 50000 or 50k)",
    "Сколько NEAR потратить на покупку? Например: 1.5": "How much NEAR to spend on the buy? E.g. 1.5",
    "Название ордера (до 24 символов), например: «докупка на дне»": "Order name (up to 24 characters), e.g. «buy the bottom»",
    "Не понял число, попробуйте ещё раз (или 🚫 Отмена).": "Couldn't read the number, try again (or 🚫 Cancel).",
    "Падение не может быть 100% и больше.": "A drop can't be 100% or more.", "Неверное число.": "Invalid number.",
    "⚠️ Пула с NEAR для этого токена нет — лимит-ордер поставить нельзя.": "⚠️ This token has no NEAR pool — a limit order can't be placed.",
    "⏳ Сеть NEAR перегружена, попробуйте через пару секунд.": "⏳ The NEAR network is busy, try again in a couple of seconds.",
    "Черновик устарел — откройте токен и нажмите «Лимит-ордер» снова": "This draft has expired — open the token and tap «Limit order» again",
    "Черновик ордера устарел — откройте токен и нажмите «Лимит-ордер» снова.": "This order draft has expired — open the token and tap «Limit order» again.",
    "Сначала задайте цену": "Set the price first", "Выберите кошелёк": "Choose a wallet", " активных ордеров": " active orders",
    "Не удалось получить цену: ": "Couldn't get the price: ", "Цель получилась ≤ 0 — задайте другую цену": "The target is ≤ 0 — set another price",
    "Ордер создан": "Order created", "✅ <b>Ордер создан.</b> Бот проверяет цену каждые ": "✅ <b>Order created.</b> The bot checks the price every ",
    " сек и пришлёт уведомление, когда он исполнится.": " sec and will notify you when it fills.",
    "📋 Мои ордера": "📋 My orders", "❌ Отменить": "❌ Cancel", "❌ Отменить ордер": "❌ Cancel order", "⬅️ К ордерам": "⬅️ Back to orders",
    "Ордер отменён": "Order cancelled", "Ордер уже не активен": "The order is no longer active",
    "на ": "for ", "% баланса": "% of balance", "🎯 когда ": "🎯 when ", "цена": "price",
    "📋 <b>Лимит-ордера</b>": "📋 <b>Limit orders</b>", "📋 <b>Активные лимит-ордера</b> (": "📋 <b>Active limit orders</b> (",
    "Активных ордеров нет. Откройте токен (пришлите контракт) и нажмите «📋 Лимит-ордер».":
        "No active orders. Open a token (send its contract) and tap «📋 Limit order».",
    "<b>Последние:</b>": "<b>Recent:</b>", " — открыть / отменить": " — open / cancel",
    "✅ <b>Лимит-ордер #": "✅ <b>Limit order #", " исполнен</b>": " filled</b>", "Куплено <b>": "Bought <b>",
    " Ⓝ по цене ≈": " Ⓝ at ≈", "Продано ": "Sold ", "❌ Ордер #": "❌ Order #",
    ") не исполнен: кошелёк удалён из бота.": ") not filled: the wallet was removed from the bot.",
    " сработал, но на кошельке ": " fired, but the wallet has ", " Ⓝ — не хватает на покупку ": " Ⓝ — not enough to buy for ",
    " Ⓝ + газ. Ордер закрыт.": " Ⓝ + gas. Order closed.", " сработал, но $": " fired, but there's no $",
    " на кошельке нет. Ордер закрыт.": " in the wallet. Order closed.", ") не исполнен: ": ") not filled: ",
    ") не исполнен из-за внутренней ошибки. Проверьте баланс.": ") not filled due to an internal error. Check your balance.",
    "кошелёк удалён": "wallet removed", "не хватило NEAR: баланс ": "not enough NEAR: balance ", "нечего продавать": "nothing to sell",
    "внутренняя ошибка: ": "internal error: ", "не хватает NEAR на комиссию": "not enough NEAR for the fee",

    # referrals / admin
    "🤝 <b>Реферальная программа</b>": "🤝 <b>Referral program</b>",
    "Приглашай друзей и получай часть комиссии бота с каждой их сделки — пожизненно, по 3 уровням:":
        "Invite friends and earn part of the bot fee from every trade they make — for life, across 3 levels:",
    "📦 <b>Твой доход</b>": "📦 <b>Your earnings</b>", " ур. (": " lvl (", "Всего: <b>": "Total: <b>",
    " · рефералов: 👤 ": " · referrals: 👤 ", "Доступно для вывода: <b>": "Available to withdraw: <b>",
    "🔗 <b>Твоя ссылка</b>": "🔗 <b>Your link</b>", "📤 Поделиться ссылкой": "📤 Share link", "💸 Вывод (от ": "💸 Withdraw (from ",
    "Минимум для вывода ": "Minimum withdrawal is ", " NEAR, сейчас ": " NEAR, you have ",
    "Сначала создайте кошелёк — доход придёт на него": "Create a wallet first — earnings are paid to it",
    "Сначала создайте кошелёк.": "Create a wallet first.", "💸 Вывести <b>": "💸 Withdraw <b>",
    " Ⓝ</b> реферального дохода на кошелёк <code>": " Ⓝ</b> of referral earnings to wallet <code>", "✅ Вывести": "✅ Withdraw",
    "⏳ Выплаты временно недоступны, попробуйте позже.": "⏳ Payouts are temporarily unavailable, try again later.",
    "Недостаточно для вывода: ": "Not enough to withdraw: ",
    "❌ Вывод не прошёл, средства остались на реферальном балансе. Попробуйте позже.":
        "❌ Withdrawal failed, the funds are still on your referral balance. Try again later.",
    "✅ Выведено ": "✅ Withdrawn ", " Ⓝ на <code>": " Ⓝ to <code>",
    "🎉 По твоей ссылке пришёл новый пользователь! Ты будешь получать часть комиссии с его сделок.":
        "🎉 A new user joined with your link! You'll earn part of the fee from their trades.",
    "⚠️ Запрошен вывод реферального дохода, но ключ кошелька выплат ": "⚠️ A referral withdrawal was requested, but the payout wallet key for ",
    " не задан. На сервере: admin.py payout-key": " is not set. On the server: admin.py payout-key",
    "❌ не задан (на сервере: admin.py payout-key)": "❌ not set (on the server: admin.py payout-key)",
    "✅ задан и подходит": "✅ set and valid", "❌ не подходит к ": "❌ doesn't match ", "🛠 <b>Админ</b>": "🛠 <b>Admin</b>",
    "Комиссия: ": "Fee: ", "</code> · рефералам ": "</code> · referrals ", "Баланс ": "Balance ", "Ключ выплат: ": "Payout key: ",
    "Собрано комиссий: <b>": "Fees collected: <b>", " Ⓝ</b> · ждут отправки ": " Ⓝ</b> · pending ", " · не дошли ": " · failed ",
    "Начислено рефералам: ": "Credited to referrers: ", " Ⓝ · выплачено ": " Ⓝ · paid ", " · <b>к выплате ": " · <b>owed ",
    "💾 Бэкап базы · пользователей: ": "💾 Database backup · users: ", " · кошельков: ": " · wallets: ",
    "Восстановление: распаковать в файл базы и запустить бота с тем же MASTER_KEY.":
        "Restore: unpack into the database file and start the bot with the same MASTER_KEY.",
    "Чистый доход: <b>": "Net income: <b>", "Пользователей: ": "Users: ", " · пришли по рефке: ": " · via referral: ",
    " · сделок с комиссией: ": " · trades with fee: ",
    "⚠️ На кошельке меньше, чем должны рефералам — пополните или не выводите.":
        "⚠️ The wallet holds less than what's owed to referrers — top it up or hold off withdrawing.",

    # errors from ref.py / near.py / wallets.py
    "котировка 0 — в пуле нет ликвидности": "quote is 0 — the pool has no liquidity",
    "свап не дал токенов (возможно, слиппедж). NEAR возвращён.": "the swap returned no tokens (probably slippage). NEAR refunded.",
    "токен просит необычно большой storage-депозит (": "the token asks for an unusually large storage deposit (",
    " NEAR), отменено": " NEAR), cancelled", "неверная длина ключа": "invalid key length",
    "не дождались результата транзакции, проверьте по хэшу": "no transaction result yet, check it by hash",
    "все RPC недоступны: ": "all RPC nodes are unavailable: ",
    "ключ не FullAccess — свапы с депозитом им подписать нельзя": "the key isn't FullAccess — it can't sign swaps with a deposit",
    "публичная часть ключа не совпадает с секретной": "the public part of the key doesn't match the secret",
    "транзакция отклонена: ": "transaction rejected: ",
    "сид-фраза с ошибкой: проверьте слова и их порядок": "invalid seed phrase: check the words and their order",
    "это не сид-фраза и не приватный ключ ed25519:…": "this is neither a seed phrase nor an ed25519:… private key",

    "Активных ордеров нет. Нажмите «➕ Новый ордер» или откройте токен и нажмите «📋 Лимит-ордер».":
        "No active orders. Tap «➕ New order» or open a token and tap «📋 Limit order».",
    "➕ Новый ордер": "➕ New order", "🛑 Остановить все": "🛑 Stop all",
    "💬 Отправьте адрес токена (или ссылку nearly / justhoot / nearblocks), на который хотите поставить лимит-ордер:":
        "💬 Send the token address (or a nearly / justhoot / nearblocks link) you want to place a limit order on:",
    "🛑 Отменить все активные ордера (": "🛑 Cancel all active orders (", ")?": ")?",
    "Да, отменить все": "Yes, cancel all", "Отменено: ": "Cancelled: ",
    "❌ Это не адрес токена. Пришлите адрес контракта, например batman-4.nearlytrade.near":
        "❌ That's not a token address. Send a contract address, e.g. batman-4.nearlytrade.near",
    "❌ Это не токен NEAR или сеть перегружена. Попробуйте ещё раз.": "❌ That's not a NEAR token or the network is busy. Try again.",

    # PnL share
    "📤 Поделиться PnL": "📤 Share PnL", "👁 Показать суммы": "👁 Show amounts", "🙈 Скрыть суммы": "🙈 Hide amounts", "🙈 Скрыть": "🙈 Hide", "🎨 Рисую карточку…": "🎨 Drawing the card…",
    "Нет открытой позиции по этому токену": "No open position in this token",
    "⏳ Не удалось получить цену, попробуйте через пару секунд.": "⏳ Couldn't get the price, try again in a couple of seconds.",
    "🤝 Делись с друзьями и получай до ": "🤝 Share with friends and earn up to ", "% комиссий!": "% of fees!",
    "Чтобы купить токен, создайте кошелёк — это займёт секунду:": "To buy this token, create a wallet — it takes a second:",
    " в NDbot 🚀 Купить в один клик:": " on NDbot 🚀 Buy it in one tap:",

    # own PnL pictures
    "🖼 Мои PnL-картинки": "🖼 My PnL pictures", "🖼 <b>Мои PnL-картинки</b>": "🖼 <b>My PnL pictures</b>",
    "Загрузите свои картинки — они будут на ваших PnL-карточках (видны только в ваших карточках).":
        "Upload your own pictures for your PnL cards (they appear only on your cards).",
    "Лучше всего — PNG с прозрачным фоном, отправленный файлом; обычные фото тоже подойдут.":
        "A PNG with a transparent background sent as a file looks best; regular photos work too.",
    "📈 Для профита: <b>": "📈 For profit: <b>", "📉 Для минуса: <b>": "📉 For loss: <b>",
    "➕ Для профита": "➕ For profit", "➕ Для минуса": "➕ For loss",
    "✅ Только мои картинки": "✅ Only my pictures", "🔀 Мои + стандартные": "🔀 Mine + standard",
    "🗑 Удалить мои картинки": "🗑 Delete my pictures", " картинок — удалите старые": " pictures — delete old ones",
    " картинок — удалите старые.": " pictures — delete old ones.", "Пришлите картинку ": "Send a picture ",
    "для профита 📈": "for profit 📈", "для минуса 📉": "for loss 📉",
    " — фото или файлом (PNG с прозрачным фоном выглядит лучше всего).": " — as a photo or a file (a transparent PNG looks best).",
    "Удалено": "Deleted", "❌ Нужна картинка (PNG, JPG или WEBP) до 8 МБ.": "❌ Send an image (PNG, JPG or WEBP) up to 8 MB.",
    "❌ Не получилось открыть картинку. Попробуйте другой файл.": "❌ Couldn't open the picture. Try another file.",
    "✅ Картинка добавлена.": "✅ Picture added.", "слишком большое изображение": "image is too large",

    # copytrade
    "🔁 Копитрейд": "🔁 Copytrade", "Копитрейд": "Copytrade", "🔁 <b>Копитрейд</b>": "🔁 <b>Copytrade</b>",
    "🔁 <b>Копитрейд</b> · ": "🔁 <b>Copytrade</b> · ",
    "Бот повторяет сделки выбранных кошельков: покупает, когда они покупают, и продаёт ту же долю, когда продают.":
        "The bot mirrors the trades of wallets you pick: it buys when they buy and sells the same share when they sell.",
    "Пока нет кошельков. Добавьте кошелёк трейдера, за которым хотите повторять сделки.":
        "No wallets yet. Add a trader's wallet whose trades you want to copy.",
    "   покупка: ": "   buy: ", ", макс. ": ", max ", " Ⓝ · продажи: ": " Ⓝ · sells: ", "да": "yes", "нет": "no",
    "% от сделки": "% of the trade", "➕ Добавить кошелёк": "➕ Add wallet", "Статус: ": "Status: ",
    "🟢 активен": "🟢 active", "⏸ на паузе": "⏸ paused", "Покупка: ": "Buy: ",
    " Ⓝ</b> на каждую сделку": " Ⓝ</b> per trade", "%</b> от суммы сделки лидера": "%</b> of the leader's trade size",
    "Макс. за сделку: <b>": "Max per trade: <b>", " Ⓝ</b> · пропускать сделки лидера меньше ": " Ⓝ</b> · skip leader trades below ",
    "Продажи: ": "Sells: ", "повторять ту же долю": "copy the same share", "не повторять": "don't copy",
    "Кошелёк: основной": "Wallet: main", "основной": "main", "Повторено сделок: ": "Trades copied: ", " · не удалось: ": " · failed: ",
    "⏸ Пауза": "⏸ Pause", "▶️ Включить": "▶️ Resume", "Включено": "Resumed", "На паузе": "Paused",
    "💰 Фикс. сумма (🔀)": "💰 Fixed amount (🔀)", "📊 % от сделки (🔀)": "📊 % of trade (🔀)", "Сумма ": "Amount ",
    "Процент ": "Percent ", "Макс. ": "Max ", "Мин. лидера ": "Leader min ", "Продажи: ✅": "Sells: ✅", "Продажи: ❌": "Sells: ❌",
    "🏷 Имя (✏️)": "🏷 Name (✏️)", "⬅️ К списку": "⬅️ Back to list", "🗑 Удалить": "🗑 Delete",
    "🗑 Удалить копитрейд «": "🗑 Delete copytrade «", "Не найдено": "Not found",
    "Отправьте адрес кошелька трейдера (<code>имя.near</code> или 64 символа):":
        "Send the trader's wallet address (<code>name.near</code> or 64 characters):",
    "❌ Неверный адрес кошелька.": "❌ Invalid wallet address.", "❌ Это ваш собственный кошелёк в боте.": "❌ That's your own wallet in the bot.",
    " не существует.": " doesn't exist.", "Этот кошелёк уже добавлен.": "This wallet is already added.",
    "✅ Кошелёк добавлен — копитрейд запущен. Настройте сумму ниже.": "✅ Wallet added — copytrade is on. Set the amount below.",
    "Сколько? Для фикс. суммы — NEAR (например 0.5), для режима % — процент от сделки лидера (например 50).":
        "How much? For a fixed amount — NEAR (e.g. 0.5); in % mode — percent of the leader's trade (e.g. 50).",
    "Максимум NEAR на одну скопированную покупку (например 5):": "Maximum NEAR per copied buy (e.g. 5):",
    "Пропускать сделки лидера меньше скольких NEAR? (например 0.5)": "Skip leader trades below how many NEAR? (e.g. 0.5)",
    "Имя для этого кошелька (до 24 символов):": "Name for this wallet (up to 24 characters):",
    "Слишком большой процент (максимум 1000%).": "Percent is too large (max 1000%).", "📊 Токен": "📊 Token",
    "Лидер купил <b>$": "Leader bought <b>$", "Лидер продал ": "Leader sold ", "❌ Не удалось повторить ": "❌ Couldn't copy the ",
    "покупку": "buy", "продажу": "sell", " Ⓝ, но на кошельке ": " Ⓝ, but the wallet has ",
    " Ⓝ — не хватает на ": " Ⓝ — not enough for ", " Ⓝ + газ.": " Ⓝ + gas.",
    "сделка лидера меньше минимума": "leader trade below minimum", "сумма меньше 0.01 NEAR": "amount below 0.01 NEAR",
    "мало NEAR": "not enough NEAR", "продажи выключены": "sells are off", "токена нет на кошельке": "no token in the wallet",

    # deployer sniper
    'Сколько NEAR тратить на каждый снайп? (например 0.5)': 'How much NEAR to spend on each snipe? (e.g. 0.5)',
    'Тейк-профит: на сколько % вырасти, чтобы продать (например 100). 0 — выключить.': 'Take-profit: how many % up to sell (e.g. 100). 0 — off.',
    'Стоп-лосс: на сколько % упасть, чтобы продать всё (например 50). 0 — выключить.': 'Stop-loss: how many % down to sell everything (e.g. 50). 0 — off.',
    'Имя для этого дева (до 24 символов):': 'Name for this dev (up to 24 characters):',
    '🎯 Снайпер': '🎯 Sniper',
    '🎯 <b>Снайпер</b> · ': '🎯 <b>Sniper</b> · ',
    'деплой': 'deploy',
    'TP выкл': 'TP off',
    'SL выкл': 'SL off',
    '🎯 <b>Снайпер деплоера</b>': '🎯 <b>Deployer sniper</b>',
    'Добавьте кошельки девов. Как только такой кошелёк задеплоит токен на nearly.trade или JustHoot, бот сразу купит на заданную сумму и поставит тейк-профит и стоп-лосс.': 'Add dev wallets. As soon as such a wallet deploys a token on nearly.trade or JustHoot, the bot instantly buys for the set amount and places a take-profit and a stop-loss.',
    '">деплой</a>': '">deploy</a>',
    '⚠️ Поток новых токенов переподключается — снайпы могут задержаться.': '⚠️ The new-token stream is reconnecting — snipes may be delayed.',
    'Пока нет девов. Нажмите «➕ Добавить дева».': 'No devs yet. Tap «➕ Add dev».',
    'Срабатывать: ': 'Trigger: ',
    'Снайпер деплоера': 'Deployer sniper',
    '⏸ Снайпер этого дева поставлен на паузу (режим «только следующий деплой»).': '⏸ The sniper for this dev is paused (mode «next deploy only»).',
    '➕ Добавить дева': '➕ Add dev',
    'выкл': 'off',
    'покупать сразу': 'buy instantly',
    'на каждый деплой': 'on every deploy',
    'только на следующий деплой': 'on the next deploy only',
    'Снайпов: ': 'Snipes: ',
    'Последние:': 'Latest:',
    'Отправьте адрес кошелька дева (<code>имя.near</code> или 64 символа):': "Send the dev's wallet address (<code>name.near</code> or 64 characters):",
    'пул не появился': 'no pool appeared',
    '%</b> → продать всё': '%</b> → sell all',
    'ждать ': 'wait ',
    ' сек (анти-снайп налог)': ' sec (anti-snipe tax)',
    'Этот дев уже добавлен.': 'This dev is already added.',
    '✅ Дев добавлен — снайпер запущен. Проверьте сумму и TP/SL ниже.': '✅ Dev added — the sniper is on. Check the amount and TP/SL below.',
    'Снайп TP +': 'Snipe TP +',
    'Снайп SL −': 'Snipe SL −',
    '% → продать всё': '% → sell all',
    'Продать на TP ': 'Sell at TP ',
    'JustHoot: ждать ⏳': 'JustHoot: wait ⏳',
    'JustHoot: сразу ⚡': 'JustHoot: instant ⚡',
    '🔁 Каждый деплой': '🔁 Every deploy',
    '1️⃣ Только следующий': '1️⃣ Next one only',
    ' девов': ' devs',
    'Минимум 0.01 NEAR.': 'Minimum 0.01 NEAR.',
    'Дев задеплоил <code>': 'Dev deployed <code>',
    '), но на кошельке ': '), but the wallet has ',
    '⏳ Жду ': '⏳ Waiting ',
    ' сек — окончание анти-снайп налога JustHoot, потом куплю.': ' sec for the JustHoot anti-snipe tax to end, then buying.',
    '❌ Дев задеплоил <code>': '❌ Dev deployed <code>',
    '), но пул для покупки за NEAR не появился за ': '), but no NEAR pool to buy from appeared within ',
    ' сек.': ' sec.',
    '📈 TP выкл': '📈 TP off',
    '🛑 SL выкл': '🛑 SL off',
    '🚫 Позиция $': '🚫 Position $',
    ' закрыта — остальные ордера на продажу (': ' closed — other sell orders (',
    ') отменены.': ') cancelled.',
    '❌ Не удалось купить <code>': "❌ Couldn't buy <code>",
    'Стоп-лосс должен быть меньше 100%.': 'Stop-loss must be below 100%.',
    '🗑 Удалить снайпер «': '🗑 Delete sniper «',
    ' через ': ' via ',

    # language switch
    "🌐 Язык: Русский": "🌐 Language: English", "✅ Язык: русский": "✅ Language: English",
}

# служебные строки разбора ввода — не переводятся
NOT_UI = {"к", "м", "kк", "всё", "все", "все RPC", "большой storage"}

_RX = re.compile("|".join(re.escape(k) for k in sorted(EN, key=len, reverse=True)))
_REV = {v: k for k, v in EN.items()}


def to_en(text: str | None) -> str | None:
    if not text:
        return text
    return _RX.sub(lambda m: EN[m.group(0)], text)


def from_en(text: str) -> str:
    """Подпись кнопки меню на английском → русская (по ней бот ищет обработчик)."""
    return _REV.get(text, text)
