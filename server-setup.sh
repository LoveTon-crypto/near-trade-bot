#!/bin/bash
# Первичная защита НОВОГО сервера (Ubuntu 22.04/24.04). Запускать от root один раз.
set -e
echo ">>> Обновления системы и автообновления безопасности"
apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q ufw fail2ban unattended-upgrades git python3-venv
dpkg-reconfigure -f noninteractive unattended-upgrades

echo ">>> Firewall: входящие закрыты, кроме SSH"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo ">>> fail2ban защищает SSH от перебора паролей"
systemctl enable --now fail2ban

echo ">>> Отдельный системный пользователь без входа и без sudo"
id tradebot &>/dev/null || useradd --system --create-home --home-dir /opt/near-trade-bot --shell /usr/sbin/nologin tradebot
chmod 700 /opt/near-trade-bot

echo ">>> Swap выключаем, чтобы ключ из памяти не попал на диск"
swapoff -a || true
sed -i.bak '/ swap / s/^/#/' /etc/fstab

echo
echo "✅ Готово. Дальше — шаги из README (SSH-ключ, отключение входа по паролю, клонирование репо)."
