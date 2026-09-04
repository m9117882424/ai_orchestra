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
- OpenCode не получает пароли control-plane/PostgreSQL, provider keys или router admin key;
- GitHub write token не передается агентскому контейнеру;
- `/connect` в OpenCode не используется для production credentials;
- broker/exchange/product secrets запрещены в Orchestra.

## 5. Docker isolation

```text
control-db (internal)
  postgres <-> control-plane

model-net
  opencode <-> model-gateway

router-backend (internal)
  model-gateway <-> model-router

provider-egress
  model-router -> AI provider APIs
```

Дополнительные правила:

- PostgreSQL control-plane недоступен агентскому контейнеру;
- admin endpoint Model Router недоступен агентскому контейнеру;
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
- direct roles: Claude Sonnet 5, GPT-5.6 Sol, Gemini 3.7 Flash / Gemini 3.5 Flash-Lite.

Новые runtime/model versions проходят отдельный build + compatibility/provider smoke. Gemini 3.8 Flash рассматривается как следующий upgrade после подтверждения совместимости stable router runtime.

## 9. Проверяемые инварианты

`make preflight` и CI должны подтверждать:

- provider keys отсутствуют в OpenCode environment;
- `MODEL_ROUTER_MASTER_KEY` отсутствует в OpenCode environment;
- OpenCode использует только `MODEL_ROUTER_CLIENT_KEY`;
- runtime OpenCode/Router config соответствует выбранному `KEY_MODE`;
- Docker network membership соответствует этой схеме;
- в core-моделях Orchestra нет продуктовых trading risk parameters;
- runtime версии закреплены, а не используют `latest`.
