# Home Trainer Bot 💪

Telegram-бот, який нагадує про вправи вдома. Протягом дня (за замовчуванням 10:00–17:00, будні) пропонує 6 вправ,
через 30 хвилин питає «Вийшло?», а ввечері показує підсумок: що зроблено, серія днів, пропуски.
Нічого не змушує робити, лише нагадує. Бот для одного користувача (тебе).

Канал: Cron Happens (copy > paste > run)

## 1. Токен і твій ID

1. У @BotFather створи бота й скопіюй токен (крок 1 серії).
2. Свій Telegram ID бот покаже сам: запусти його (див. нижче) і напиши йому `/id`.

## 2. Підготовка сервера (Ubuntu)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv git
```

## 3. Встановлення

```bash
git clone https://github.com/YOUR_NAME/home-trainer-bot.git
cd home-trainer-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

У `.env` встав `BOT_TOKEN`. `OWNER_ID` поки залиш порожнім.

## 4. Перший запуск

```bash
.venv/bin/python bot.py
```

Напиши боту `/id`, скопіюй число, зупини бота (Ctrl+C), впиши його в `OWNER_ID` у `.env` і запусти знову.
Далі `/start`, обери вправи, натисни «Готово». Спробувати одразу: `/demo`.

## 5. Робота 24/7 (systemd)

```bash
sudo cp home-trainer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now home-trainer
sudo systemctl status home-trainer
```

Логи: `journalctl -u home-trainer -f`
Перезапуск після зміни `.env`: `sudo systemctl restart home-trainer`

Якщо користувач на сервері не `ubuntu` (наприклад, `root`), поправ `User=` і шляхи в `home-trainer.service`.

## Команди бота

| Команда | Що робить |
|---|---|
| `/start`, `/exercises` | обрати вправи |
| `/today` | вправи на сьогодні |
| `/stats` | серія, пропуски, вправи за 7 днів |
| `/demo` | швидка демонстрація (не рахується в статистиці) |
| `/id` | показує твій Telegram ID |

## Налаштування (`.env`)

`START`, `END`, `SLOTS`, `WORKDAYS` (1 = понеділок, `1-5`), `WINDOW_MIN`, `RETRY_MIN`, `BOT_TZ`. Приклад у `.env.example`.

## Правила

- День вважається активним, якщо зроблена хоча б одна вправа.
- Серія: активні дні підряд; вихідні серію не ламають, день без жодної вправи ламає.
- Прострочені нагадування (бот був вимкнений) пропускаються, не приходять пачкою.

## Важливо

Не публікуй `.env` і токен. Якщо токен потрапив у публічний доступ, перевипусти його в @BotFather (`/revoke`).

Безкоштовні сервери Oracle можуть зупиняти майже простоюючі машини. Бот майже весь час «спить», тому слідкуй, щоб сервер був Running; на платному акаунті (Pay As You Go, лишаючись у Always Free ресурсах) цього не буває.
