# AI Orchestra v1 — архитектурные границы

## 1. Назначение

AI Orchestra — самостоятельный виртуальный отдел разработки и аналитики. Он не является частью Trading Platform или любого другого продукта, который разрабатывает.

Продуктовые бизнес-правила, production credentials и runtime конкретного продукта находятся только в репозитории и инфраструктуре этого продукта.

## 2. Поток моделей

```text
OpenCode / агенты
        |
        | только MODEL_ROUTER_CLIENT_KEY
        v
Inference Model Gateway
        | разрешены только inference endpoints
        | router master key недоступен агенту
        v
Model Router (LiteLLM)
        |
        +--> shared: AITunnel
        |
        +--> separate: OpenAI
        |              Anthropic
        |              Google
        |
        +--> future: xAI / DeepSeek / Mistral / OpenRouter / other
```

OpenCode никогда не получает provider API keys или `MODEL_ROUTER_MASTER_KEY`.

Добавление нового провайдера требует:

1. добавить secret только в `.env.providers`;
2. добавить deployment/route в `config/model-router.separate.yaml`;
3. при необходимости перенаправить логический alias;
4. выполнить switch с build/provider smoke;
5. только после успешного smoke переключить OpenCode.

При неуспехе switch script восстанавливает предыдущий route.

## 3. Логические модели

Агенты используют стабильные aliases:

- `orchestra-lead`;
- `orchestra-architect`;
- `orchestra-coder`;
- `orchestra-analyst`;
- `orchestra-qa`;
- `orchestra-reviewer`;
- `orchestra-risk`;
- `orchestra-quant`;
- `orchestra-fast`.

Конкретные модели за aliases могут меняться без изменения prompts и ролей.

## 4. Секреты

- `.env` — operational/control credentials, router admin credential и отдельный inference client credential;
- `.env.providers` — только реальные ключи AI-провайдеров; файл получает только `model-router`;
- `.env.repositories` — host-bound read-only Git profiles; файл получает только
  `repo-manager`;
- OpenCode не получает пароли control-plane/PostgreSQL, provider keys или router admin key;
- GitHub write token не передается агентскому контейнеру;
- `/connect` в OpenCode не используется для production credentials;
- broker/exchange/product secrets запрещены в Orchestra.

## 5. Docker isolation

```text
control-db (internal)
  postgres <-> control-plane / execution-worker / repo-manager / workspace-manager

model-net (internal)
  opencode <-> model-gateway

router-backend (internal)
  model-gateway <-> model-router

provider-egress
  model-router -> AI provider APIs

repository-egress
  repo-manager -> разрешённые HTTPS Git remotes

repository-mirrors volume
  repo-manager (rw) -> workspace-manager (ro)

task-workspaces volume
  workspace-manager (rw) -> opencode (rw) / execution-worker (ro)
```

Legacy operator worktrees монтируются отдельно в
`/workspace/worktrees/manual`; bind mount на общий `/workspace/worktrees`
запрещён, потому что он может скрыть managed named volume внутри OpenCode.
Managed volume принадлежит UID/GID `10001:10001` с режимом `0700`. OpenCode
получает `DAC_OVERRIDE` и `FOWNER`, чтобы его UID 0 мог изменять содержимое и file
mode workspace. Workspace Manager запускается root только в fail-closed launcher,
который переходит на UID/GID 10001 и перед exec оставляет в effective, permitted
и ambient наборах только эти две filesystem capabilities. Временные
`SETUID`/`SETGID` приложению не наследуются. Это позволяет инспектировать и
очищать root-owned пути OpenCode; rootfs и mirror mount остаются read-only,
egress отсутствует. Execution Worker видит volume только read-only без
capabilities.

Дополнительные правила:

- PostgreSQL control-plane недоступен агентскому контейнеру;
- admin endpoint Model Router недоступен агентскому контейнеру;
- Git credentials и mirror недоступны OpenCode и Execution Worker;
- Workspace Manager не имеет network egress, а task workspace создаёт только из
  локального read-only mirror;
- Docker socket хоста не монтируется;
- web ports публикуются только на `127.0.0.1`;
- CPU/RAM limits и log rotation заданы в Compose;
- prompt permissions рассматриваются как дополнительный слой, а не как единственная security boundary.

## 6. Fail-closed control plane

Capability Guard относится только к полномочиям AI-отдела:

- production deploy — запрещен;
- external write — запрещен;
- financial execution — запрещено;
- secret access — запрещен.

Approval является журналом решения владельца, а не технической командой разблокировки.

## 7. Trading Platform

Trading Platform — отдельный проект. Допустимо иметь в Orchestra профильных специалистов (`quant-researcher`, `market-data-engineer`, `risk-officer`, `execution-engineer`) для разработки этого продукта.

Но в Orchestra запрещено хранить:

- торговые API keys;
- лимиты риска конкретного счета;
- параметры стратегий;
- cash reserve / leverage / stop / position sizing rules;
- состояние позиций и ордеров;
- endpoint реального исполнения.

## 8. Runtime baseline v1

- OpenCode: `1.18.27`;
- LiteLLM Proxy: `1.98.0` stable;
- direct roles: Claude Sonnet 5, GPT-5.6 Sol, Gemini 3.5 Flash / Gemini 3.5 Flash-Lite.

Новые runtime/model versions проходят отдельный build + compatibility/provider smoke. Gemini 3.7 Flash и Gemini 3.8 Flash рассматриваются как следующие upgrade-кандидаты после подтверждения полного agent-workflow compatibility со stable router runtime.

## 9. Проверяемые инварианты

`make preflight` и CI должны подтверждать:

- provider keys отсутствуют в OpenCode environment;
- `MODEL_ROUTER_MASTER_KEY` отсутствует в OpenCode environment;
- OpenCode использует только `MODEL_ROUTER_CLIENT_KEY`;
- runtime OpenCode/Router config соответствует выбранному `KEY_MODE`;
- Docker network membership соответствует этой схеме;
- task execution до inference связан с exact repository/base/workspace identity
  и проходит повторную read-only filesystem verification;
- workspace с изменениями или неоднозначным evidence не удаляется автоматически;
- в core-моделях Orchestra нет продуктовых trading risk parameters;
- runtime версии закреплены, а не используют `latest`.
