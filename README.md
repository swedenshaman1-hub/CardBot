# CardBot — бот метафорических карт дня

Хранение данных — Supabase (как в проекте SMMAgents), деплой — Railway (единая площадка для всех ботов).

## Настройка Supabase

В Supabase-проекте (можно использовать тот же, что у SMMAgents, или отдельный) выполни в SQL Editor:

```sql
create table cards (
  id bigint primary key,
  name text not null,
  meaning text not null,
  image_url text not null
);

create table spreads (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  card_ids jsonb not null,
  channel_message_id bigint
);

create table settings (
  key text primary key,
  value text not null
);
```

Создай публичный Storage-бакет `card-images` (Storage → New bucket → Public bucket).

## Установка

```text
cd CardBot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполни `.env`:

- `BOT_TOKEN` — токен бота от @BotFather
- `ADMIN_ID` — твой Telegram user id (узнать у @userinfobot)
- `CHANNEL_ID` — username канала вида `@my_channel` (бот должен быть админом канала)
- `SUPABASE_URL`, `SUPABASE_KEY` — из настроек проекта Supabase (Project Settings → API)

## Загрузка рубашки карты

Отправь боту в личные сообщения фото обратной стороны карты (рубашку) с подписью:

```text
back
```

Бот загрузит изображение в Supabase Storage и будет использовать его для коллажей.
(Альтернатива: задай переменную `CARD_BACK_URL` с прямой ссылкой на изображение.)

## Загрузка карт в базу

Отправь боту в личные сообщения фото лицевой стороны карты с подписью:

```text
12: Зеркало: Карта говорит о честности с собой...
```

Формат подписи: `ID: Название: Расшифровка`. ID — это номер карты в твоей колоде, его и используешь в `/newspread`. Фото загружается в Supabase Storage, запись — в таблицу `cards`.

## Публикация расклада дня

В личке боту:

```text
/newspread 5 12 3 18 7 22
```

Бот соберёт коллаж из этих 6 карт (пронумерует 1–6 по порядку, как ты указал) и опубликует в канал. Расклад сохраняется в таблице `spreads`.

## Как работает выбор карты пользователем

Любой человек пишет боту в личные сообщения цифру от 1 до 6 — бот отвечает:
1. Фото лицевой стороны карты с названием и расшифровкой
2. Голосовым сообщением с текстом расшифровки (gTTS, русский)

## Локальный запуск

```bash
python bot.py
```

## Деплой на Railway

Репозиторий уже содержит `Procfile` и `railway.json` (по аналогии с SMMAgents):

1. Создай новый проект в Railway, подключи репозиторий CardBot.
2. В Variables добавь те же ключи, что в `.env` (`BOT_TOKEN`, `ADMIN_ID`, `CHANNEL_ID`, `SUPABASE_URL`, `SUPABASE_KEY`).
3. Railway автоматически использует `worker: python bot.py` из Procfile.

Данные (карты, расклады, изображения) хранятся в Supabase, поэтому при редеплое на Railway ничего не теряется — Volume не нужен.
