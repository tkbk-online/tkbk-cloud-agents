# TKBK Cloud Agents

Фоновые облачные агенты для TKBK IDE / TKBK Agent (как Background Agents у Cursor) на базе [OpenHands](https://github.com/OpenHands/software-agent-sdk) (MIT). Клиент ставит задачу из IDE, Telegram или панели, агент работает в изолированном контейнере на моделях через шлюз `api.tkbk.online` с ключа клиента, результат — patch + сводка + лог команд (по желанию — push в ветку `tkbk/<taskId>` или PR).

Статус: **готово к развёртыванию на отдельный VPS, на сервер не ставилось** (решения владельца — [deploy/DECISIONS.md](deploy/DECISIONS.md)).

## Что внутри

| Путь | Что |
|---|---|
| `docker-compose.yml` | Стек для отдельного VPS: Caddy (TLS) + оркестратор + squid-egress (+ `llm-stub` по профилю) |
| `deploy/bootstrap.sh` | Поднять VPS за 15 минут: docker, ufw, клон, `.env`, `compose up` |
| `orchestrator/` | Оркестратор (Python 3.12, FastAPI): Bearer `cr_…` через лицензии сайта, HMAC для бота/сайта, очередь, сэндбокс на задачу, SSE, patch, учёт минут; 94 теста |
| `client/tkbkCloudAgents.ts` | TS-клиент для расширения (fetch + SSE, без зависимостей) |
| `docs/ide-client.md` | Контракт API для IDE |
| `deploy/egress/` | squid с allow-list доменов (GitHub, npm, PyPI, api.tkbk.online) |
| `deploy/caddy/` | `Caddyfile` для VPS; `shared-caddy-block.caddy` + `deploy/compose.shared-caddy.yml` — вариант на хост с уже стоящим Caddy |
| `deploy/llm-stub/` | Фейковая OpenAI-совместимая модель для локальных прогонов без ключа |
| `integrations/` | Заглушки для бота (`/cloud`), Python-клиент/CLI (HMAC-сторона) |
| `scripts/smoke_sandbox.py` | Живая проверка сэндбокса и egress в Docker без LLM-ключа |

## Как поднять за 15 минут (отдельный VPS)

Основной сервер TKBK (2 vCPU / 3.8 ГБ) не подходит — сэндбоксы едят по 2 vCPU / 4 ГБ. Нужен свой VPS с Ubuntu 24.04.

```bash
# 1. DNS: A agents.tkbk.online -> IP нового VPS (до запуска, Caddy выпустит сертификат сам)
# 2. на VPS, под root:
curl -fsSL https://raw.githubusercontent.com/tkbk-online/tkbk-cloud-agents/main/deploy/bootstrap.sh | sudo bash
# 3. проверка
curl -s https://agents.tkbk.online/health
cd /opt/tkbk-agents && python3 scripts/smoke_sandbox.py      # 8 проверок сэндбокса и egress, без ключа
# 4. первая задача с реальным ключом Pro/Teams (IDE-сторона):
curl -s -X POST https://agents.tkbk.online/v1/tasks -H "Authorization: Bearer cr_…" -H "Content-Type: application/json" \
  -d '{"repoUrl":"https://github.com/tkbk-online/tkbk-mcp","prompt":"добавь тесты для парсера конфигурации","maxMinutes":30}'
curl -s -N https://agents.tkbk.online/v1/tasks/<id>/events -H "Authorization: Bearer cr_…"   # живой лог
curl -s https://agents.tkbk.online/v1/tasks/<id>/diff   -H "Authorization: Bearer cr_…"   # патч
```

`bootstrap.sh` идемпотентен: ставит docker-ce + compose, открывает в ufw только 22/80/443, кладёт репозиторий в `/opt/tkbk-agents`, создаёт `.env` со свежим `AGENTS_INTERNAL_SECRET`, тянет образ сэндбокса (2 ГБ), собирает оркестратор и запускает стек. Переменные: `AGENTS_DOMAIN`, `SANDBOX_IMAGE`, `WITH_STUB=1` (локальная фейковая модель).

Руками после скрипта: в `.env` при желании `LICENSE_PUBLIC_KEY` (публичный ключ Ed25519 из админки сайта — тогда ответы `/api/license/check` проверяются по подписи), `AGENTS_INTERNAL_SECRET` — в бот/сайт.

### Рекомендация по VPS (10 параллельных задач)

| Нагрузка | vCPU | RAM | Диск | Примечание |
|---|---|---|---|---|
| 3 задачи (старт) | 8 | 16 ГБ | 100 ГБ NVMe | `MAX_CONCURRENT_TASKS=3` |
| 5–6 задач | 12 | 32 ГБ | 160 ГБ NVMe | |
| **10 задач** | **16–24** | **48 ГБ** | **250 ГБ NVMe** | сэндбокс 2 vCPU / 4 ГБ, агенты редко грузят CPU одновременно → переподписка ×1.5 допустима; `MAX_CONCURRENT_TASKS=10` |

Диск: образ 2 ГБ + клон и `node_modules`/`.venv` на задачу (1–3 ГБ) + патчи/архивы 7 дней. Сеть: одна `/24` на задачу — `bootstrap.sh` расширяет address-pools Docker.

## Архитектура

```
TKBK IDE / TKBK Agent                Telegram-бот, сайт, панель
   │ Authorization: Bearer cr_…          │ HMAC (X-TKBK-Ts + X-TKBK-Sig, как у бота)
   ▼                                     ▼
Caddy (TLS)  ──►  tkbk-agents-api :8090  ──►  POST tkbk.online/api/license/check  (план, часы; кэш 10 мин)
                      │ docker.sock
                      │ на каждую задачу:
                      │   network create --internal tkbk-task-<id>
                      │   git clone (токен клиента, в сэндбокс не попадает) | tar.gz рабочей копии → baseline-коммит
                      │   docker run ghcr.io/openhands/agent-server:1.55.0-python
                      │       --cap-drop ALL --security-opt no-new-privileges --memory 4g --cpus 2 --pids-limit 512
                      │       --mount volume=tasks,subpath=<id>/workspace  HTTPS_PROXY=http://tkbk-agents-egress:3128
                      │   POST /api/conversations {llm: {model, base_url=api.tkbk.online, api_key=cr_…}}
                      │   poll состояния + события → SSE клиенту; finished | error | stuck | timeout | cancel
                      │   commit → patch (+ archive | push tkbk/<id> | PR) → минуты в SQLite
                      ▼
            tkbk-sbx-<id>  ──►  tkbk-agents-egress (squid allow-list)  ──►  api.tkbk.online, github.com, npm, PyPI
```

**Решение: контейнер на задачу** (не на клиента, не общий). Проверено по исходникам `software-agent-sdk` v1.55.0: серверная часть OpenHands — `openhands-agent-server`, мультипользовательности в OSS нет (один session-key, `user_id` — тег), а встроенный docker-режим не даёт задать сеть → allow-list исходящих невозможен. Одна задача = один контейнер agent-server; внутри OpenHands волен дробить работу (суб-агенты, task tracker), наружу это один сэндбокс с одними лимитами.

**LLM через TKBK.** Claude: `anthropic/<model>` + `base_url=https://api.tkbk.online` (litellm дописывает `/v1/messages`). Остальные: `openai/<model>` + `https://api.tkbk.online/openai/v1`. Токены — с ключа клиента, отдельно не биллим. Ключ только в памяти оркестратора на время задачи и в теле запроса к сэндбоксу; в SQLite и логах — `key_id = sha256(key)[:24]` и префикс.

**Лицензии и квоты.** `POST /api/license/check` сайта → `plan`, `features`, `limits.cloudAgentHours` (Pro 20, Teams 100, Teams+ 300 — из `src/lib/license.ts` сайта). Задача принимается, если план содержит `cloud_agents`, и `использовано + max_minutes ≤ часы × 60` за календарный месяц (очередь и активные задачи резервируют `max_minutes`). Параллельно на ключ: Pro 3, Teams 5, Teams+ 10 (`CONCURRENT_PER_PLAN`). Ответ сайта проверяется по Ed25519-подписи, если задан `LICENSE_PUBLIC_KEY`. `LICENSE_MODE=stub` — только локально.

## API (кратко; полный контракт — [docs/ide-client.md](docs/ide-client.md))

| Метод | Путь | Что |
|---|---|---|
| `GET` | `/v1/me` | план, часы, остаток минут, лимит параллельных задач (Bearer) |
| `POST` | `/v1/tasks` | `{repoUrl | source:"upload", branch, template, prompt, issue, model, gitToken, deliver, push, maxMinutes}` → `taskId` |
| `PUT` | `/v1/tasks/{id}/workspace` | tar.gz рабочей копии (≤ 200 МБ) для `source: "upload"` |
| `GET` | `/v1/tasks/{id}` | статус, фаза, прогресс, минуты, метрики, хвост лога |
| `GET` | `/v1/tasks/{id}/events` | SSE: `status` / `agent` (action, observation, message, error) / `log` / `done`; `Last-Event-ID` |
| `GET` | `/v1/tasks/{id}/diff` | unified diff (`git apply`) |
| `GET` | `/v1/tasks/{id}/artifacts[/{patch\|archive\|log}]` | артефакты |
| `GET` | `/v1/tasks/{id}/result` | итог: patch/archive/pr, ветка, файлы, сводка агента, число команд |
| `POST` | `/v1/tasks/{id}/cancel` | отмена |
| `GET` | `/v1/usage`, `/v1/keys/{key_id}/usage` | минуты/задачи/токены по ключу за месяц (биллинг, HMAC видит всех) |

Шаблоны: `fix_issue` (текст issue тянется из GitHub), `add_tests`, `update_deps`, `pr_from_spec`, `custom`. Промпт агенту содержит правила: не пушить, не трогать git-конфиг и секреты, коммитить в текущую ветку, закончить резюме. Push/PR делает оркестратор токеном клиента — агент токена не видит.

## Лимиты, очередь, уборка

* Таймаут задачи `maxMinutes` (по умолчанию 60, потолок 120): агент на паузу, контейнер убит, изменения всё равно оформлены как patch (при `deliver: pr` — draft с `[timeout]`). `max_iterations` 100 + stuck-detection OpenHands.
* Глобально `MAX_CONCURRENT_TASKS` (10 на рекомендованном VPS), на ключ — по плану. Остальные ждут в `queued` (FIFO).
* Сэндбокс: `--cap-drop ALL`, `no-new-privileges`, 4g/2 CPU/512 pids, без swap, uid 10001, без портов и docker.sock, сеть `--internal` + squid allow-list (80/443). Проверено `scripts/smoke_sandbox.py` (8/8).
* Уборка: контейнер, сеть и workspace удаляются по концу задачи (`keep_workspace` для отладки); при старте оркестратора — осиротевшие контейнеры/сети по метке `tkbk.cloud-agents`; патчи/архивы/логи старше `RESULTS_TTL_DAYS` (7) — джанитор раз в 10 минут. Рестарт оркестратора = ожидающие/активные задачи `failed` (ключи были в памяти).

## Проверено локально (16.10.2026)

```
pytest  — 94 passed (HMAC и Bearer-авторизация, лицензии и Ed25519, квоты часов, per-client параллелизм,
          upload tar.gz с защитой от traversal/symlink, SSE replay/resume, DockerRunner end-to-end
          с реальным git и фейковыми docker/agent-server/GitHub/сайтом, таймаут, отмена)
ruff    — чисто;  tsc --strict client/tkbkCloudAgents.ts — чисто
docker compose build — ок (api 0.2.0, llm-stub); compose config — ок
scripts/smoke_sandbox.py — 8/8 PASS (реальные контейнеры agent-server 1.55.0 + squid)
Локальный прогон задачи через compose (profile stub, LICENSE_MODE=stub, реального ключа TKBK_API_KEY
в окружении НЕ было → модель — llm-stub): repoUrl=github.com/tkbk-online/tkbk-mcp → клон → сэндбокс →
агент выполнил команду → patch с TKBK_CLOUD_AGENT.md, SSE-поток, /v1/me показал списанные минуты.
```

Что дал живой прогон: agent-server сам стартует диалог при `initial_message` (`/run` → 409, учтено); workspace после `chown` сэндбоксу требует `git -c safe.directory=*` у оркестратора (учтено). Прогон с реальным ключом и реальной моделью — за владельцем (INSTALL §5).

## Риски

* **Сэндбокс — контейнер, не VM**: ядро общее с хостом. Capabilities сняты, ресурсы и сеть ограничены, но 0-day в ядре/runc — риск. Следующий шаг: gVisor (`--runtime=runsc`), отдельный хост (его и предлагаем), AppArmor-профиль.
* **Код клиента — недоверенный ввод**: prompt-injection из README/issue может заставить агента делать странное внутри сэндбокса; наружу — только allow-list, токенов у агента нет. Патч и PR надо ревьюить — показывать это в IDE.
* **Стоимость длинных задач**: токены списываются с баланса клиента; защита — лимит минут, итераций, дневной лимит ключа на шлюзе; в `metrics` есть токены и `cost_usd_reported` (для кастомных моделей litellm цены не знает → 0). Прогноз стоимости перед запуском — TODO панели.
* **Общий бюджет Teams**: `/api/license/check` не отдаёт `sub` владельца команды, поэтому часы сейчас считаются на каждый ключ отдельно (`limits.cloudAgentHours` на ключ). Для общего пула сайту нужно добавить `teamSub` в payload — см. DECISIONS.
* **Лицензии на проде**: `/api/license/check` на tkbk.online сейчас отвечает 404 (коммит с лицензиями на main, не задеплоен) — без деплоя сайта оркестратор отвечает 403 всем. `LICENSE_MODE=stub` — только для локальной отладки.

## Разработка

```bash
cd orchestrator && uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check tkbk_agents tests
# локальный стек без ключа и без TLS (API на 127.0.0.1:8090, любой cr_… = Pro):
cp .env.example .env && sed -i 's/^AGENTS_INTERNAL_SECRET=.*/AGENTS_INTERNAL_SECRET='$(openssl rand -hex 32)'/' .env
docker compose -f docker-compose.yml -f deploy/compose.local.yml --profile stub up -d --build api egress llm-stub
```

Лицензия MIT. Поставщики моделей в коде и документации не называются — только шлюз `api.tkbk.online`.
