# Установка Free Spy

Бот развертывается в облаке Render (бесплатно) или запускается локально.

---

## Автоматический деплой (Render)

Быстрый запуск с автоматическим созданием базы PostgreSQL.

1. Сгенерируйте ключ шифрования для сообщений, запустив локально:
   ```bash
   python generate_key.py
   ```
   Скопируйте полученную строку.
2. Нажмите кнопку деплоя:
   [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Claxy-mod/Free-spy)
3. Заполните переменные:
   * `BOT_TOKEN` — токен от @BotFather.
   * `USER_ID` — ваш числовой ID в Telegram.
   * `ENCRYPTION_KEY` — ключ шифрования, полученный на Шаге 1.
4. Дождитесь окончания сборки и настройте [UptimeRobot](#настройка-пинга-uptimerobot) для предотвращения засыпания.

> [!NOTE]
> На бесплатном тарифе база PostgreSQL на Render удаляется через 90 дней. Для постоянной работы лучше использовать ручной деплой с базой от Supabase.

---

## Ручной деплой (Render + Supabase)

Запуск бота в режиме 24/7 с постоянной бесплатной базой данных.

### Шаг 1. База данных Supabase
1. Создайте проект на [Supabase](https://supabase.com/).
2. В панели управления нажмите **Connect** (справа вверху).
3. Во вкладке **Connection String** -> **URI** выберите метод **Transaction Pooler** и скопируйте строку.
4. Замените `[YOUR-PASSWORD]` на ваш пароль СУБД. Специальные символы в пароле нужно закодировать (например, `,` заменить на `%2C`, а `/` на `%2F`).

### Шаг 2. Ключ шифрования
Сгенерируйте ключ шифрования для сообщений, запустив:
```bash
python generate_key.py
```
Скопируйте полученную строку.

### Шаг 3. Деплой на Render
1. Сделайте форк репозитория на GitHub.
2. Создайте новый веб-сервис на [Render](https://render.com/) и подключите форкнутый репозиторий.
3. Настройки:
   * **Language**: `Python`
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `python main.py`
   * **Instance Type**: `Free`
4. В разделе **Advanced** добавьте Environment Variables:
   * `BOT_TOKEN` — токен бота.
   * `USER_ID` — ваш ID в Telegram.
   * `DATABASE_URL` — строка подключения к Supabase (из Шага 1).
   * `ENCRYPTION_KEY` — ключ шифрования (из Шага 2).
   * `TIMEZONE_NAME` — часовой пояс (например, `Europe/Moscow`).
5. Нажмите **Create Web Service**.

---

## Настройка пинга (UptimeRobot)

Бесплатный веб-сервис Render засыпает через 15 минут простоя. Чтобы бот не отключался:
1. Скопируйте URL вашего веб-сервиса (выглядит как `https://your-app.onrender.com`).
2. Создайте на [UptimeRobot](https://uptimerobot.com/) бесплатный монитор типа **HTTPS**.
3. Укажите скопированный URL и интервал 5 минут.

---

## Привязка бота в Telegram

1. В Telegram перейдите в **Изменить профиль -> Автоматизация чатов -> Добавить бота**.
2. Введите юзернейм вашего бота.
3. Если бот уже был привязан, переподключите его (отключите и привяжите заново) для обновления соединения.

---

## Локальный запуск

1. Клонируйте репозиторий и настройте venv:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. Создайте конфиг из шаблона:
   ```bash
   cp config.ini.example config.ini
   ```
3. Заполните параметры в `config.ini` и запустите бота:
   ```bash
   python main.py
   ```
