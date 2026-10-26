# NEAR Trade Bot (приватный, этап 1)

Личный трейд-бот для NEAR: кошелёк, покупка/продажа токенов через Ref Finance,
позиции с PnL, слиппедж, вывод на ваш адрес. Отвечает **только вам** и **только в личке**.

## Как устроена безопасность

| Что | Как |
|---|---|
| Приватный ключ | на диске только в зашифрованном виде (scrypt + AES-256-GCM), файл `wallet.enc.json` с правами 600 |
| Пароль | **нигде не хранится**. После каждого запуска бот заблокирован, пока вы не отправите `/unlock` и пароль. Сообщение с паролем бот сразу удаляет |
| Доступ к боту | все сообщения не от `OWNER_ID` и не из лички игнорируются |
| Подбор пароля | 5 неверных попыток → блокировка на 15 минут |
| Вывод средств | только на один адрес `WITHDRAW_ADDRESS` из `.env`, с подтверждением кнопкой |
| Лимиты | максимум NEAR на одну покупку (`MAX_BUY_NEAR`), резерв на газ |
| Подпись транзакций | своим кодом (`near.py`), без сторонних NEAR-библиотек; пул и котировка читаются прямо из контракта Ref |
| Процесс | отдельный системный пользователь `tradebot` без входа и sudo, изоляция systemd, без swap и core-дампов |
| Сервер | отдельный VPS, firewall (открыт только SSH), fail2ban, автообновления, вход только по SSH-ключу |

**Правило:** держите в боте только торговый банк. Основные средства — на вашем обычном кошельке.

---

## Установка на новый сервер

Подойдёт любой VPS с Ubuntu 22.04/24.04 и 1 ГБ RAM (например, ещё один у InterServer или Hetzner CX22). **Не ставьте на сервер с другими ботами.**

### 1. SSH-ключ вместо пароля (Termius)
1. В Termius: **Keychain → Generate Key** (ed25519).
2. Нажмите **Export to host** и выберите новый сервер.
3. Проверьте, что Termius заходит без пароля.
4. Только после этого отключите вход по паролю:
   ```bash
   sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   systemctl restart ssh
   ```

### 2. Защита сервера
Загрузите файл `server-setup.sh` на сервер (в Termius: **SFTP** → перетащите файл в `/root`) и выполните:
```bash
bash /root/server-setup.sh
```

### 3. Код из приватного репозитория
Создайте на GitHub **приватный** репозиторий `near-trade-bot` и загрузите туда файлы.

Чтобы сервер мог его скачивать, дайте ему ключ только на чтение:
```bash
sudo -u tradebot mkdir -p -m 700 /opt/near-trade-bot/.ssh
sudo -u tradebot ssh-keygen -t ed25519 -f /opt/near-trade-bot/.ssh/id_ed25519 -N ""
cat /opt/near-trade-bot/.ssh/id_ed25519.pub
```
Этот ключ добавьте на GitHub: **репозиторий → Settings → Deploy keys → Add** (галочку *write* НЕ ставить).

Затем:
```bash
cd /opt/near-trade-bot
sudo -u tradebot git init -q && sudo -u tradebot git remote add origin git@github.com:LoveTon-crypto/near-trade-bot.git
sudo -u tradebot git fetch -q origin main   # на вопрос про github.com ответьте yes
sudo -u tradebot git reset -q --hard origin/main
sudo -u tradebot python3 -m venv venv
```

### 4. Настройки
```bash
sudo -u tradebot cp .env.example .env
sudo -u tradebot nano .env      # BOT_TOKEN, OWNER_ID, WITHDRAW_ADDRESS, лимиты
chmod 600 .env
```
Токен создайте у @BotFather для **нового** бота. Свой Telegram ID можно узнать у @userinfobot.

### 5. Кошелёк
```bash
bash deploy.sh                                        # ставит библиотеки и запускает бота
sudo -u tradebot venv/bin/python keystore.py new      # новый торговый кошелёк
# или: sudo -u tradebot venv/bin/python keystore.py import   — свой аккаунт (ключ вводится скрыто)
sudo -u tradebot venv/bin/python keystore.py export   # бэкап ключа: запишите офлайн, затем clear
```
Пополните показанный адрес NEAR. Затем в Telegram: `/start` → `/unlock` → пароль.

### Обновление
```bash
tradebot-update
```

---

## Как пользоваться
- Отправьте адрес токена (`blackdragon.tkn.near`) или ссылку nearly / justhoot / nearblocks — откроется карточка с кнопками **Купить** (пресеты и своя сумма) и **Продать** (25 / 50 / 100 % или свой процент).
- **💼 Кошелёк** — баланс; `/withdraw 5` или `/withdraw all` — вывод на ваш адрес.
- **📊 Позиции** — токены, их стоимость в NEAR и PnL.
- **⚙️ Настройки** — слиппедж, пресеты покупки.
- **🔒 Заблокировать** (`/lock`) — убрать ключ из памяти.

## Сначала проверка малыми суммами
Код транзакций проверен тестами офлайн, но в сети ещё не запускался.
Первые сделки делайте на 0.1–0.5 NEAR и проверяйте каждую на nearblocks.

## Дальше (следующие этапы)
- покупка на мемпадах nearly.trade и justhoot до миграции;
- снайпер по деплою и новым пулам;
- копи-трейдинг и лимитные ордера.
