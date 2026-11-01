#!/bin/bash
# Обновление бота на сервере: tradebot-update (команда создаётся при первом запуске этого скрипта)
set -e
cd /opt/near-trade-bot
if [ -f .env ] && ! grep -q '^MASTER_KEY=..*' .env; then
  sed -i '/^MASTER_KEY=/d' .env
  echo "MASTER_KEY=$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')" >> .env
  echo "🔑 Создан MASTER_KEY в .env — им зашифрованы все кошельки."
  echo "   СОХРАНИТЕ его копию: grep MASTER_KEY /opt/near-trade-bot/.env"
fi
echo ">>> Обновляю библиотеки..."
sudo -u tradebot venv/bin/pip install -q -r requirements.txt
cp tradebot.service /etc/systemd/system/tradebot.service
systemctl daemon-reload
cat > /usr/local/bin/tradebot-update <<'UPD'
#!/bin/bash
set -e
cd /opt/near-trade-bot
sudo -u tradebot git fetch -q origin main
sudo -u tradebot git reset -q --hard origin/main
exec bash deploy.sh
UPD
chmod +x /usr/local/bin/tradebot-update
systemctl enable -q tradebot
echo ">>> Перезапускаю бота..."
systemctl restart tradebot
sleep 6
if systemctl is-active --quiet tradebot; then
  echo "✅ Бот работает. Версия: $(sudo -u tradebot git log -1 --format='%h — %s')"
else
  echo "❌ Бот не запустился:"
  journalctl -u tradebot -n 25 --no-pager
fi
