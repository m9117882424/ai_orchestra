# AI Orchestra — виртуальный отдел разработки и аналитики

AI Orchestra — самостоятельный AI-отдел, который принимает задачи владельца, декомпозирует их между профильными ролями, реализует изменения в изолированных Git worktree, проводит QA и независимое review и оставляет проверяемый audit trail.

**AI Orchestra не является частью Trading Platform.** Trading Platform, Arvento, Wialon, Fuel Monitor, BI и другие системы — отдельные продукты, которые отдел может разрабатывать.

> Статус: pilot 0.3 hardening. `git push`, merge, production deploy, доступ к product secrets, запись во внешние production-системы и финансовое исполнение технически не входят в разрешенный контур отдела.

## Ключевые свойства

- OpenCode Web как рабочее место AI-руководителя и специалистов;
- кабинет руководителя на FastAPI;
- PostgreSQL для задач, согласований, бюджетов и audit trail;
- обязательные QA и independent review;
- одна задача — одна ветка/worktree — один агент-редактор;
- inference-only Model Gateway между OpenCode и Model Router;
- Model Router между отделом и внешними AI API;
- shared AITunnel или отдельные API OpenAI / Anthropic / Google;
- добавление новых AI-провайдеров без изменения prompts и ролей;
- provider secrets и router admin key недоступны OpenCode/агентам;
- control-plane PostgreSQL физически отделен Docker-сетью от OpenCode;
- pinned runtime versions, resource limits и localhost-only web ports;
- backup без `.env`, `.env.providers` и OpenCode credential store.

Подробные границы описаны в [`docs/ARCHITECTURE_V1.md`](docs/ARCHITECTURE_V1.md).

## Архитектура

```text
                         Владелец
                     /              \
                    v                v
          Кабинет руководителя    OpenCode Web
                   |                  |
                   v                  v
          control-plane DB      department-lead
          (private network)          |
                                  specialists
                                      |
                                      v
                                 worktrees/repos
                                      |
                                      v
                                Model Gateway
                           (inference endpoints only)
                                      |
                                      v
                                Model Router
                              /      |       \
                             /       |        \
                       AITunnel   direct APIs  future providers
                                 /    |    \
                              OpenAI Anthropic Google
```

OpenCode не получает:

- `AITUNNEL_API_KEY`;
- `OPENAI_API_KEY`;
- `ANTHROPIC_API_KEY`;
- `GOOGLE_GENERATIVE_AI_API_KEY`;
- `MODEL_ROUTER_MASTER_KEY`;
- пароль PostgreSQL control-plane;
- пароль кабинета руководителя;
- GitHub write token;
- broker/exchange/product production secrets.

OpenCode получает только отдельный `MODEL_ROUTER_CLIENT_KEY`. Gateway разрешает этому ключу только inference endpoints и сам подставляет router admin credential на закрытом backend-сегменте.

## Роли

### Общая разработка

| Роль | Задача |
| --- | --- |
| `department-lead` | декомпозиция, делегирование, контроль результата |
| `business-analyst` | требования и критерии приемки |
| `architect` | архитектура, API, БД, безопасность, rollback |
| `developer` | реализация |
| `qa-engineer` | независимые тесты |
| `code-reviewer` | независимое review diff |
| `devops-engineer` | Docker, CI/CD, runbook |
| `data-analyst` | данные, SQL, качество и метрики |

### Профильные специалисты для разработки Trading Platform

| Роль | Задача |
| --- | --- |
| `quant-researcher` | гипотезы, backtest, bias, costs, tail-risk |
| `market-data-engineer` | ingestion и качество market data |
| `portfolio-analyst` | P&L, экспозиции, концентрация |
| `risk-officer` | независимое risk veto |
| `execution-engineer` | mocks/sandbox adapters |
| `trade-reviewer` | независимое review торгового проекта |

Эти роли — компетенции **отдела разработки**. Они не делают AI Orchestra торговым терминалом. Риск-параметры, позиции, ордера и execution runtime принадлежат отдельной Trading Platform.

## Model Router

Агенты обращаются только к логическим моделям:

- `orchestra-lead`;
- `orchestra-architect`;
- `orchestra-coder`;
- `orchestra-analyst`;
- `orchestra-qa`;
- `orchestra-reviewer`;
- `orchestra-risk`;
- `orchestra-quant`;
- `orchestra-fast`.

Конкретная модель за alias зависит от режима и может меняться без изменения prompts и agent workflow.

### `shared`

Все aliases идут через AITunnel. Provider credentials находятся только в `.env.providers` и загружаются только в контейнер Model Router.

### `separate`

Aliases распределяются по прямым API:

- Anthropic — lead/review/risk;
- OpenAI — architecture/coding/QA/quant;
- Google — analytics/fast tasks.

Baseline direct profile v1:

- Claude Sonnet 5;
- GPT-5.6 Sol;
- Gemini 3.5 Flash / Gemini 3.5 Flash-Lite.

Gemini 3.7 Flash и Gemini 3.8 Flash рассматриваются как следующие upgrade-кандидаты, но не включаются в baseline v1 до отдельной проверки полного agent workflow со stable LiteLLM. Новая модель не попадает в рабочий отдел только потому, что она новее.

### Добавление нового AI-провайдера

OpenCode менять не нужно.

1. Добавьте API key в `.env.providers`.
2. Добавьте deployment в `config/model-router.separate.yaml`.
3. Направьте нужный `orchestra-*` alias на новый deployment.
4. Выполните `make separate` или отдельный тестовый switch в ветке.

Switch script сначала поднимает новую пару Router/Gateway, ждет health и выполняет реальные low-cost provider smoke tests. Только после успешной проверки пересоздается OpenCode. При ошибке выполняется rollback на предыдущую маршрутизацию.

Model Router построен на LiteLLM Proxy, поэтому архитектура не ограничена тремя провайдерами.

## Runtime baseline

Для воспроизводимости версии закреплены:

```text
OpenCode  1.18.27
LiteLLM   1.98.0 stable
```

`latest` в production build не используется. Обновление версии — отдельное изменение с build + smoke + review.

## Секреты

После `make init` есть два файла:

```text
.env
.env.providers
```

`.env` содержит operational credentials:

- пароли Web UI;
- пароль control-plane DB;
- `MODEL_ROUTER_MASTER_KEY`;
- `MODEL_ROUTER_CLIENT_KEY`;
- версии runtime;
- Git identity.

`.env.providers` содержит только provider API keys.

Оба файла имеют права `0600` и игнорируются Git. `make preflight` откажется продолжать, если provider credentials обнаружены в обычном `.env`.

**Не используйте `/connect` OpenCode для production provider credentials.**

## Docker isolation

- `control-db` — только PostgreSQL + control-plane, internal network;
- `model-net` — только OpenCode + Model Gateway;
- `router-backend` — только Model Gateway + Model Router, internal network;
- `provider-egress` — Model Router для исходящих AI API;
- OpenCode не находится в сети control-plane DB или router admin service;
- Docker socket хоста не монтируется;
- все web/diagnostic ports привязаны только к `127.0.0.1`;
- контейнеры имеют CPU/RAM limits, log rotation и `no-new-privileges`.

## Требования

Минимум для пилота:

- Ubuntu 22.04/24.04;
- 4 vCPU;
- 8 GiB RAM;
- Docker Engine + Compose v2;
- Nginx/TLS или VPN для внешнего доступа.

На основном сервере Orchestra имеет отдельные лимиты и не должен вытеснять действующие production-сервисы.

## Быстрый запуск

```bash
cd /opt
git clone https://github.com/m9117882424/ai_orchestra.git
cd ai_orchestra
make init
```

Для первого запуска используем `shared`. Откройте только provider secret file:

```bash
nano .env.providers
```

и заполните:

```dotenv
AITUNNEL_API_KEY=...
```

Не публикуйте ключ в терминальных логах, Git или чатах.

Дальше:

```bash
make shared
make preflight
make build
make up
make status
make smoke
```

`make smoke` проверяет:

1. inference-only Model Gateway и отказ неправильному client key;
2. реальные model routes через Model Router;
3. OpenCode Web;
4. control-plane;
5. PostgreSQL.

## Переход на прямые API

Заполните в `.env.providers`:

```dotenv
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_GENERATIVE_AI_API_KEY=...
```

Затем:

```bash
make separate
make smoke
```

`make separate` выполняет реальный запрос минимум к одному route каждого прямого провайдера. При неуспехе прежний режим восстанавливается.

Возврат:

```bash
make shared
```

## Интерфейсы

| Сервис | Host bind |
| --- | --- |
| OpenCode | `127.0.0.1:4096` |
| Manager | `127.0.0.1:8088` |
| Model Gateway diagnostics | `127.0.0.1:18089` |
| Model Router | не публикуется |
| Control PostgreSQL | не публикуется |

Для внешнего браузерного доступа используйте Nginx HTTPS/VPN. PostgreSQL, Router и Gateway наружу публиковать не нужно.

## Кабинет руководителя

Кабинет показывает:

- задачи и статусы;
- approvals;
- budget records;
- usage records;
- audit trail;
- fail-closed Capability Guard.

Capability Guard относится только к полномочиям AI-отдела:

```text
production deploy       DENY
external write          DENY
financial execution     DENY
secret access           DENY
```

Approval — запись решения владельца. Он **не изменяет Capability Guard автоматически** и не является командой выполнения.

## Workflow задачи

```text
backlog
   ↓
planned
   ↓
in_progress
   ├──→ waiting_approval
   ↓
qa
   ├──→ in_progress
   ├──→ failed
   ↓
done
```

Для нетривиальной задачи стандартная цепочка:

```text
Owner
 → department-lead
 → business-analyst + architect
 → developer/devops
 → qa-engineer
 → code-reviewer
 → result
```

## Рабочие репозитории

Проекты клонируются на host в `/opt/ai_orchestra/repos/`. Не передавайте GitHub write token OpenCode-контейнеру.

```bash
cd /opt/ai_orchestra/repos
git clone https://github.com/m9117882424/arvento-kpp-report.git
```

Worktree задачи:

```bash
cd /opt/ai_orchestra
./scripts/worktree-create.sh arvento-kpp-report fix-mileage origin/main
```

Удаление после проверки:

```bash
./scripts/worktree-remove.sh arvento-kpp-report fix-mileage --yes
```

Скрипт не удалит worktree с незакоммиченными файлами.

## Проверки разработки

```bash
python3 -m pip install -r control_plane/requirements-dev.txt
make test
make validate
```

CI выполняет:

- JSON/YAML/Python/JavaScript syntax;
- shell syntax + ShellCheck;
- Docker Compose validation;
- static security boundary checks;
- control-plane tests;
- сборку всех контейнеров.

## Резервное копирование

```bash
make backup
```

Backup включает:

- dump control-plane PostgreSQL;
- код и несекретную конфигурацию, включая Model Router и Model Gateway;
- OpenCode state;
- Git bundles проектов;
- `SHA256SUMS`.

Не включаются `.env`, `.env.providers` и `data/opencode/auth.json`. Provider secrets храните отдельно в password/secret manager; backup рекомендуется копировать в шифрованное внешнее хранилище.

## Логи и диагностика

```bash
make status
make logs
make manager-logs
make router-logs
```

Проверки вручную:

```bash
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:18089/health
curl -u "manager:ПАРОЛЬ" http://127.0.0.1:8088/api/summary
curl -u "opencode:ПАРОЛЬ" http://127.0.0.1:4096/global/health
```

## Обновление

```bash
make backup
git pull --ff-only
make init
make preflight
make build
make up
make smoke
```

## Безопасная эксплуатация

- не публиковать `4096`, `8088`, `18089` или PostgreSQL напрямую в интернет;
- не монтировать Docker socket хоста;
- не хранить product secrets в Orchestra;
- не давать OpenCode GitHub write token;
- не выполнять production deploy из agent runtime;
- не считать prompt permission полноценной security boundary — критичные ограничения обеспечиваются Docker/network/secret separation;
- изменения `main` проходят через ветку/PR и CI;
- AI Orchestra и Trading Platform всегда остаются отдельными проектами.
