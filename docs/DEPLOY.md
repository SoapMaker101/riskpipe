# Развёртывание riskpipe на сервере

## 1. Сервер

- Linux (Ubuntu 22.04+ / Debian 12+), 1–2 vCPU, 2 ГБ RAM, 10 ГБ диска. Python 3.10+.
- **Сеть.** ФНС «Прозрачный бизнес», ГИР БО, Checko API и sudact.ru работают с любого IP. КАД (kad.arbitr.ru),
  Федресурс и ЕИС (zakupki.gov.ru) с зарубежных IP отвечают отказом — для будущих коннекторов к ним нужен
  сервер с российским IP. Проверка: `curl -sI https://kad.arbitr.ru | head -1` (451 — закрыто).
- Часовой пояс — тот, в котором удобно запускать утренний прогон: `sudo timedatectl set-timezone Europe/Moscow`.

## 2. Установка

```bash
sudo apt-get install -y git python3 python3-venv
git clone https://github.com/SoapMaker101/riskpipe.git /opt/riskpipe
cd /opt/riskpipe
sudo bash deploy/install.sh <пользователь>
```

Скрипт создаёт `.venv`, ставит зависимости, копирует `.env.example` → `.env` (права 600), прогоняет офлайн-тесты
и включает systemd-таймер `riskpipe.timer` (06:00 по времени сервера).

## 3. Ключи и вход

```bash
nano /opt/riskpipe/.env          # CHECKO_API_KEY=...  (+ при желании MOS_ROOT, ANTHROPIC_API_KEY)
```

Входной список — `/opt/riskpipe/input/private/customers.csv` (формат как в `input/example.csv`;
папка `input/private/` не попадает в git). Его можно перезаписывать каждое утро до 06:00 из вашей системы
(выгрузка закупок, CRM).

## 4. Проверка

```bash
sudo systemctl start riskpipe.service       # ручной прогон
journalctl -u riskpipe -f                   # журнал
ls /opt/riskpipe/out/run-$(date +%F)/       # cards.json, report.md, deep/, run.log
```

Повторный запуск того же дня продолжает с чекпойнта; факты, которые ещё свежи, не перезапрашиваются.

## 5. Без systemd (cron)

```cron
0 6 * * * cd /opt/riskpipe && .venv/bin/python -m pipeline.run --input input/private/customers.csv --run-id run-$(date +\%F) >> out/cron.log 2>&1
```

## 6. LLM на сервере (необязательно)

- **Claude Code CLI**: установите (`npm install -g @anthropic-ai/claude-code`) и авторизуйтесь под пользователем
  сервиса (`claude` → вход). Конвейер найдёт `claude` в PATH сам. Убедитесь, что PATH сервиса его видит
  (при необходимости добавьте `Environment=PATH=...` в `riskpipe.service`).
- **Anthropic API** (только для классификатора СМИ): `ANTHROPIC_API_KEY` в `.env` и `.venv/bin/pip install anthropic`.
- Если Anthropic недоступен из сети сервера, запускайте LLM-шаги отдельно на другой машине по той же базе
  или отключите их: `--no-deep` (СМИ без LLM просто пропускаются).

Бюджеты: `scoring/rules.yaml` → `deep.run_budget_tokens` (на прогон), `deep.budget_tokens` (на компанию),
`deep.max_companies`.

## 7. Обновление

```bash
cd /opt/riskpipe && git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest eval -q
```

База (`data/riskpipe.db`) и кэш (`data/cache/`) переживают обновления; при смене схемы пересоздайте базу —
факты соберутся заново.

## 8. Объёмы и время

- 150 заказчиков: сбор ФНС с паузами ≈ 40–60 мин (без капчи), Checko API — 3 запроса на компанию
  (бесплатного тарифа хватает примерно на 33 компании в день; дальше — платный тариф или очередь на следующий день).
- Волна фронтира — до 3 соседей на красную/жёлтую карточку, ≈ 15 с на узел.
- Глубокий разбор — ~20–35 тыс. токенов на компанию, в пределах бюджета прогона.
