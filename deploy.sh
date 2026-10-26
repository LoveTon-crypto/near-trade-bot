#!/bin/bash
# Обновление бота на сервере: tradebot-update (команда создаётся при первом запуске этого скрипта)
set -e
cd /opt/near-trade-bot
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
  echo "   Он заблокирован: отправьте ему /unlock в Telegram."
else
  echo "❌ Бот не запустился:"
  journalctl -u tradebot -n 25 --no-pager
fi
