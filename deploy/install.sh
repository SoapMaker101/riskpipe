#!/usr/bin/env bash
# Установка riskpipe на Linux-сервер (Ubuntu 22.04+/Debian 12+): venv, зависимости, .env, тесты, systemd-таймер.
# Запуск из корня репозитория:  sudo bash deploy/install.sh [пользователь]   (по умолчанию — текущий)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${1:-${SUDO_USER:-$(whoami)}}"

echo "riskpipe: каталог $APP_DIR, пользователь $RUN_USER"

if ! command -v python3 >/dev/null; then
  apt-get update && apt-get install -y python3 python3-venv python3-pip
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Создан $APP_DIR/.env — впишите CHECKO_API_KEY (и при желании MOS_ROOT, ANTHROPIC_API_KEY)."
fi
mkdir -p "$APP_DIR/input/private" && chown -R "$RUN_USER" "$APP_DIR/input"

echo "Тесты (офлайн):"
(cd "$APP_DIR" && sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/python" -m pytest eval -q)

# systemd: ежедневный прогон (время — в deploy/riskpipe.timer, часовой пояс сервера)
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" "$APP_DIR/deploy/riskpipe.service" \
  > /etc/systemd/system/riskpipe.service
cp "$APP_DIR/deploy/riskpipe.timer" /etc/systemd/system/riskpipe.timer
systemctl daemon-reload
systemctl enable --now riskpipe.timer
systemctl list-timers riskpipe.timer --no-pager

cat <<MSG

Готово. Дальше:
  1) положите входной список заказчиков в $APP_DIR/input/private/customers.csv
     (формат — input/example.csv; путь задаётся в deploy/riskpipe.service);
  2) проверьте ручной прогон:  sudo systemctl start riskpipe.service && journalctl -u riskpipe -f
  3) карточки и отчёт — в $APP_DIR/out/run-<дата>/ (cards.json, report.md, deep/).
MSG
