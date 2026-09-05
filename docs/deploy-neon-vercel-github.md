# Бесплатный serverless-деплой: Neon + Vercel + GitHub Actions

Этот вариант запускает **только Telegram-бота**. Каталог `web/` и `run_web.py`
не разворачиваются и не должны добавляться как Vercel-проект.

## Что уже подготовлено

- `api/index.py` — Vercel Function по адресу `/api`; она принимает Telegram webhook.
- `jobs/scan_once.py` — единичный скан, который завершается после обработки очереди.
- `.github/workflows/scan.yml` — скан раз в 12 часов.
- `.github/workflows/configure-webhook.yml` — ручная регистрация Telegram webhook.
- Neon URL вида `postgresql://...` автоматически переводится в драйвер
  `postgresql+asyncpg://...`.

## 1. Neon

1. Создайте бесплатный проект Neon и скопируйте **pooled connection string**.
2. Не меняйте её: стандартный URL `postgresql://...` подходит.
3. Первая serverless-функция сама создаст пустую схему БД.

Текущая SQLite БД сохранена в локальном архиве. Чтобы перенести историю,
черновики и публикации в новый пустой Neon проект, до включения расписания
выполните на своём ПК:

```powershell
$env:DATABASE_URL = 'postgresql://<Neon pooled connection string>'
& 'C:\Python314\python.exe' -m jobs.migrate_sqlite_to_neon --sqlite-path .\airdrop_bot.db --upload-local-images
```

Скрипт откажется работать, если Neon уже содержит таблицы, поэтому случайно
перезаписать БД нельзя. Флаг `--upload-local-images` отправит старые карточки в
ваш личный Telegram-чат и заменит локальные пути на Telegram `file_id`; это нужно
для публикации старых черновиков из Vercel. После успешной миграции добавьте тот
же `DATABASE_URL` в Vercel и GitHub Secrets.

## 2. GitHub Secrets

В репозитории откройте **Settings → Secrets and variables → Actions** и создайте:

`BOT_TOKEN`, `ADMIN_USER_ID`, `PUBLISH_CHANNEL_ID`, `DATABASE_URL`,
`GROQ_API_KEY`, `GEMINI_API_KEY`, `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ACCOUNT_ID`, `WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET`.

`WEBHOOK_URL` заполняется после пункта 3 как
`https://<ваш-проект>.vercel.app/api`. `TELEGRAM_WEBHOOK_SECRET` — случайная
строка длиной не менее 32 символов. Секреты не копируются в `.env` и не коммитятся.

## 3. Vercel

1. Импортируйте GitHub-репозиторий в личный Vercel Hobby account.
2. Root Directory: `airdrop_bot_Claude`, если репозиторий содержит текущую
   внешнюю папку `Crypto_aggregator`; иначе корень репозитория.
3. В Environment Variables добавьте: `BOT_TOKEN`, `ADMIN_USER_ID`,
   `PUBLISH_CHANNEL_ID`, `DATABASE_URL`, `TELEGRAM_WEBHOOK_SECRET`,
   `GROQ_API_KEY`, `GEMINI_API_KEY`, `CLOUDFLARE_API_TOKEN`,
   `CLOUDFLARE_ACCOUNT_ID`.
4. Deploy. Проверка: откройте `https://<ваш-проект>.vercel.app/api` — должен
   вернуться JSON `{"ok": true}`.
5. Добавьте этот URL в GitHub secret `WEBHOOK_URL`.

## 4. Включение Telegram

В GitHub Actions вручную запустите workflow **Configure Telegram webhook**.
После успешного выполнения напишите боту `/status` или нажмите кнопку у черновика.
Polling на ПК после этого запускать нельзя: у бота может быть только один способ
получения updates.

## Расписание и лимиты

По умолчанию Actions запускает скан в `00:17` и `12:17` UTC. GitHub не обещает
секундную точность запуска scheduled workflows. Чтобы переключить на 6 часов,
в `.github/workflows/scan.yml` замените cron на `17 */6 * * *` и следите за
месячным лимитом 2000 минут для закрытого бесплатного репозитория.

Во время скана изображение отправляется в личный Telegram-чат. Бот сохраняет
выданный Telegram `file_id`, поэтому при нажатии Approve Vercel повторно использует
его без локального диска.
