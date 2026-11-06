# G5 — Telegram-уведомления AI Orchestra

Issue: [#54](https://github.com/m9117882424/ai_orchestra/issues/54). Control Plane task: `71e3071e-c343-4f53-93cc-94bd97aad8ed`.

## Назначение и границы

Отдельный **opt-in** процесс читает только allowlist действий из `public.audit_events`
**основного** Control Plane. Он не заменяет уже работающего бота корпоративного
такси и не читает production-БД такси. Пилотный Control Plane не является вторым
источником Telegram-уведомлений. Пароль административного кабинета, токены
исполнителей и права записи в PostgreSQL уведомителю не выдаются.

Получатель — согласованный владельцем Telegram chat ID, бот — отдельный
`@max_it_orchestra_bot`. Сам токен не хранится в Git и не входит в Docker env.
Секреты читаются исключительно из root-only файлов, смонтированных в контейнер
по одному в режиме read-only.

Отправляются только служебные типы событий: создание/изменение задачи,
запрос/получение согласования, запуск в очереди, завершение/отмена/ошибка запуска,
ошибка задания исполнителя. Потоковые события `repository.sync_*`, heartbeat,
артефакты, audit `details`, названия задач, имена сотрудников, контактные
данные, URL с токенами **не передаются**. В сообщении присутствуют лишь
заранее заданный текст события, короткий UUID, время Europe/Istanbul и ссылка
на кабинет.

При самом первом включении курсор устанавливается по времени PostgreSQL:
исторические события не рассылаются. Последующие сообщения повторно не
отправляются благодаря SQLite state volume. Запрос с 120-секундным перекрытием
защищает от небольших задержек фиксации транзакций; во время простоя сохраняется
старый курсор, чтобы догнать новые события. Обычный лимит — 10 отправок в
минуту. При отказе Telegram курсор не продвигается, поэтому отправка будет
повторена. В узком окне между успешным HTTP и записью SQLite возможно одно
повторное сообщение (семантика *at-least-once*).

## Условия включения

1. CI для PR зелёный; независимое ревью кода, безопасности и лимитов принято.
2. Проверен изолированный стенд **без подключения к рабочему Telegram-чату**.
3. Для основного PostgreSQL создан отдельный логин, которому разрешены только
   `CONNECT`, `USAGE ON SCHEMA public`, `SELECT ON public.audit_events`.
   Его пароль сохраняется в отдельном DSN-файле. Не использовать владельца
   БД `ai_orchestra` или административный Basic кабинета.
4. Проверено, что бот и chat ID указаны в корректных root-only файлах,
   Telegram доступен из выделенной egress-сети, а state volume сохраняется
   после рестарта контейнера.
5. Только после этого выставляется явный флаг `ORCHESTRA_TELEGRAM_SEND_ENABLED=yes`.

Пример принципа разграничения БД (выполнить администратору с собственным
случайным секретом, не копируя пароль в Git или историю команд):

```sql
CREATE ROLE orchestra_notify_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '<SECRET_FROM_VAULT>';
GRANT CONNECT ON DATABASE ai_orchestra TO orchestra_notify_reader;
GRANT USAGE ON SCHEMA public TO orchestra_notify_reader;
GRANT SELECT ON TABLE public.audit_events TO orchestra_notify_reader;
ALTER ROLE orchestra_notify_reader SET default_transaction_read_only = on;
```

DSN-файл с правами `0600` должен содержать URL PostgreSQL с логином
`orchestra_notify_reader` и адресом `postgres:5432`. Его расположение передаётся
в `ORCHESTRA_TELEGRAM_DB_DSN_FILE`. Два других файла передаются через
`ORCHESTRA_TELEGRAM_TOKEN_FILE` и `ORCHESTRA_TELEGRAM_CHAT_ID_FILE`.

Проверка без отправки (из директории репозитория):

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.telegram-notifier.yml \
  --profile telegram-notifications run --rm telegram-notifier \
  python -m notifications.telegram_notifier --check
```

Включение после ревью и явной активации `ORCHESTRA_TELEGRAM_SEND_ENABLED=yes`:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.telegram-notifier.yml \
  --profile telegram-notifications up -d telegram-notifier
```

Откат канала **без остановки Control Plane и такси**:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.telegram-notifier.yml \
  --profile telegram-notifications stop telegram-notifier
```

Проверка логики (без доступа к БД и Telegram):

```bash
python3 -m unittest discover -v -s notifications/tests
```

## Ограничения и следующие этапы

- Пока это отдельный безопасный шлюз, а не двусторонний чат или интерфейс управления.
- `qa` и `in_progress` в Control Plane — записанные статусы задач, а не
  индикатор работающего процесса. Уведомления основаны на **новых audit events**.
- Для большой истории audit (>100 тысяч строк) перед постоянно включённой
  доставкой стоит добавить индекс по `(created_at, id)` отдельной миграцией.
- Внешние алерты инфраструктуры Grafana/Alertmanager требуют следующей
  независимой интеграции с тем же ботом, с дедупликацией и явной классификацией.
- Развёртывание overlay не является частью стандартной сборки и не запускается
  без явного решения владельца.
