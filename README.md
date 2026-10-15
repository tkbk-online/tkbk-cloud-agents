# TKBK Cloud Agents

Облачные агенты для разработчиков на базе [OpenHands](https://github.com/OpenHands/OpenHands) (MIT): клиент ставит задачу из IDE (TKBK Agent), Telegram-бота или веб-панели, агент работает в изолированном контейнере на моделях через шлюз TKBK, результат — pull request или архив.

Статус: **готово к развёртыванию, на сервер не ставилось** (ждёт подтверждения владельца, см. [deploy/INSTALL.md](deploy/INSTALL.md) и [deploy/DECISIONS.md](deploy/DECISIONS.md)).

## Что внутри

| Путь | Что |
|---|---|
| `docker-compose.yml` | Стек для сервера TKBK: оркестратор `api` + egress-прокси `egress` |
| `orchestrator/` | Оркестратор (Python 3.12, FastAPI): REST с HMAC, очередь, сэндбокс на задачу, учёт минут; 66 тестов |
| `deploy/egress/` | squid с allow-list доменов (GitHub, npm, PyPI, api.tkbk.online) |
| `deploy/caddy/agents.tkbk.online.caddy` | Блок для Caddy — добавляется в `/opt/tkbk-shop/Caddyfile` **на месте** |
| `deploy/INSTALL.md` | Пошаговое развёртывание |
| `deploy/DECISIONS.md` | Решения, которые нужны от владельца |
| `integrations/` | Заглушки: кнопка «Запустить в облаке TKBK» для TKBK Agent, `/cloud` для бота, Python-клиент/CLI |
| `scripts/smoke_sandbox.py` | Живая проверка сэндбокса и egress в Docker без LLM-ключа |

## Архитектура

```
клиент (IDE / Telegram / панель)
   │  HMAC (как у бота: X-TKBK-Ts + X-TKBK-Sig), ключ cr_… в теле запроса
   ▼
Caddy (tkbk-caddy)  ──►  tkbk-agents-api :8090   (оркестратор, сеть tkbk_default + agents_ctrl)
                              │ docker.sock
                              │ на каждую задачу:
                              │   network create --internal tkbk-task-<id>
                              │   git clone (с токеном клиента, токен в сэндбокс не попадает)
                              │   docker run ghcr.io/openhands/agent-server:1.55.0-python
                              │       --cap-drop ALL --security-opt no-new-privileges
                              │       --memory 4g --cpus 2 --pids-limit 512
                              │       HTTPS_PROXY=http://tkbk-agents-egress:3128
                              │   POST /api/conversations {llm: {model, base_url, api_key=cr_…}}
                              │   poll /api/conversations/{id} → finished | error | stuck | timeout
                              │   commit → push → PR (или tar.gz) → минуты в SQLite
                              ▼
                    tkbk-sbx-<id>  ──►  tkbk-agents-egress (squid, allow-list)  ──►  api.tkbk.online
                    (агент + код)                                                   github.com, npm, PyPI
```

**LLM через TKBK.** Claude — Anthropic-формат: `anthropic/<model>` + `base_url=https://api.tkbk.online` (litellm сам дописывает `/v1/messages`, проверено по исходникам litellm). Остальные — OpenAI-формат: `openai/<model>` + `base_url=https://api.tkbk.online/openai/v1`. По умолчанию `claude-sonnet-5`; провайдер определяется по префиксу `claude`, можно задать явно.

**Ключ клиента** `cr_…` живёт только в памяти оркестратора на время задачи и в теле запроса к сэндбоксу (там agent-server шифрует его одноразовым `OH_SECRET_KEY` на эфемерном диске контейнера). В `.env` сервера, в SQLite и в логах ключа нет — в учёте хранятся `key_id = sha256(key)[:24]` и префикс `cr_abcd…`.

### Почему контейнер на задачу (а не на клиента и не общий)

Проверено по исходникам `software-agent-sdk` v1.55.0 (15.10.2026) — серверная часть OpenHands сегодня это `openhands-agent-server` (FastAPI REST + WS), а `OpenHands/OpenHands` v1.27.1 — это Agent Canvas, локальный UI-клиент:

* **Мультипользовательности в OSS нет**: один session-key на сервер, поле `user_id` — просто тег. Общий сервер на всех клиентов = любой клиент видит задачи и ключи остальных. Отпадает.
* **Контейнер на клиента** держит 4 ГБ RAM на каждого простаивающего клиента, оставляет ключ в живом процессе между задачами и всё равно не изолирует задачи одного клиента друг от друга.
* **Контейнер на задачу**: лимиты CPU/RAM/PID на задачу, сеть `--internal` на задачу, ключ умирает вместе с контейнером, нет простаивающей памяти, минуты считаются по факту жизни контейнера. Встроенный у OpenHands режим `OH_CONVERSATION_RUNTIME=docker` делает почти то же, но без `--network` (default bridge) — allow-list исходящих так не сделать, поэтому контейнеры запускает наш оркестратор.

Цена: холодный старт ~10–20 с (образ 2 ГБ уже на диске) и клон репозитория на каждую задачу.

## REST API оркестратора

Все эндпоинты кроме `/health` подписаны HMAC: `X-TKBK-Ts` (unix-секунды) и `X-TKBK-Sig = hex HMAC-SHA256(AGENTS_INTERNAL_SECRET, "{ts}\n{METHOD}\n{path?query}\n{body}")`, окно 300 с, 120 запросов/мин — ровно схема `app/ea_internal.py` бота. Ответы `{"ok": true, …}` / `{"ok": false, "error": "…"}`.

| Метод | Путь | Что |
|---|---|---|
| `POST` | `/v1/tasks` | Создать задачу: `api_key` (cr_…), `repo` (https://github.com/o/r), `template`, `instruction`, `issue`, `branch`, `model`, `provider`, `git_token`, `deliver` (`pr`\|`archive`), `max_minutes`, `max_iterations`, `callback_url`, `client_ref` |
| `GET` | `/v1/tasks/{id}` | Статус: `queued → preparing (fetching-inputs, cloning, starting-sandbox) → running → delivering → done \| failed \| cancelled \| timeout`, метрики токенов, минуты |
| `GET` | `/v1/tasks?key_id=&status=&limit=` | Список |
| `POST` | `/v1/tasks/{id}/cancel` | Отмена (в очереди — сразу, в работе — на ближайшей контрольной точке) |
| `GET` | `/v1/tasks/{id}/result` | Результат: `pr_url` или `archive_url`, ветка, коммит, изменённые файлы, резюме агента |
| `GET` | `/v1/tasks/{id}/archive` | tar.gz рабочего дерева (для `deliver=archive`) |
| `GET` | `/v1/usage?month=YYYY-MM` | Минуты/задачи/токены по каждому `key_id` за месяц — для биллинга |
| `GET` | `/v1/keys/{key_id}/usage` | То же по одному ключу |
| `GET` | `/v1/templates` | Шаблоны и лимиты |

Шаблоны задач: `fix_issue` (нужен `issue`, текст тянется из GitHub), `add_tests`, `update_deps`, `pr_from_spec` (ТЗ в `instruction`), `custom`. Промпт агенту содержит правила: не пушить, не трогать git-конфиг и секреты, коммитить в текущую ветку, закончить резюме. Пуш и PR делает оркестратор токеном клиента — агент токена не видит (защита от prompt-injection из репозитория).

`callback_url` — по завершении оркестратор делает POST с той же HMAC-подписью `{"event": "task.finished", "task": {...}}`.

**Учёт минут.** Минута агента = минута жизни сэндбокса, округление вверх, минимум 1. Хранится по `key_id`; `MONTHLY_MINUTES_CAP_PER_KEY` — страховочный потолок на ключ в месяц (402 при превышении); тарифные квоты — в панели.

## Лимиты и защита сэндбокса

* `--memory 4g --memory-swap 4g --cpus 2 --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges --ulimit core=0`, пользователь `openhands` (uid 10001), без публикации портов, без docker.sock.
* Сеть `--internal` на задачу: прямого выхода в интернет нет; `HTTP(S)_PROXY` → squid с allow-list (`deploy/egress/allowlist.txt`): `api.tkbk.online`, `*.github.com`, `*.githubusercontent.com`, `*.npmjs.org`, `*.npmjs.com`, `*.pypi.org`, `*.pythonhosted.org`. Только порты 80/443. Проверено скриптом `scripts/smoke_sandbox.py` (пройдены все 8 проверок, см. ниже).
* Тайм-аут задачи `max_minutes` (по умолчанию 30, потолок 120): агент ставится на паузу, контейнер убивается, что успело измениться — уезжает как **draft-PR с пометкой `[timeout]`**. `max_iterations` (100) и stuck-detection OpenHands — от зацикливания.
* Одновременно не больше `MAX_CONCURRENT_TASKS` (3) задач — это и про RAM, и про лимит docker-сетей (см. риски).
* При рестарте оркестратора активные и ждущие задачи помечаются `failed` (секреты были в памяти), осиротевшие контейнеры и сети убираются по метке `tkbk.cloud-agents`.

## Проверено локально

```
orchestrator: pytest — 66 passed (HMAC, модели, шаблоны, адаптер TKBK, сэндбокс, клиент agent-server,
              git/GitHub, API через TestClient, DockerRunner end-to-end с реальным git и фейковыми docker/agent-server)
ruff check   — чисто (3 стилистических замечания в тестах оставлены)
docker build tkbk/cloud-agents-api:0.1.0 — ок; docker compose config — ок
scripts/smoke_sandbox.py (реальные контейнеры agent-server 1.55.0 + squid):
  PASS sandbox /health == 200
  PASS direct Internet from sandbox network is blocked
  PASS proxy -> api.tkbk.online reachable (401 без ключа)
  PASS proxy -> example.com denied (403 от squid на CONNECT)
  PASS proxy -> pypi.org reachable
  PASS sandbox python via HTTPS_PROXY reaches api.tkbk.online
  PASS sandbox runs as uid 10001 with empty capability sets
  PASS HostConfig: memory/pids/cap-drop applied
```

Не проверено: полный прогон с реальным ключом `cr_…` и реальным репозиторием (нет ключа у сборочной сессии) — первый шаг после развёртывания, см. INSTALL.

## Тарифы (предложение)

Экономика: токены клиент оплачивает через шлюз со своего баланса (как сейчас), подписка — за часы агента и инфраструктуру. Себестоимость часа сэндбокса (2 vCPU / 4 ГБ) на VPS ≈ 10–20 ₽; маржа подписки идёт на сервер, поддержку и риск длинных задач.

| Тариф | Цена | Что входит |
|---|---|---|
| **Solo** | 9 900 ₽/мес | 20 часов агента, 1 задача одновременно, лимит задачи 30 мин, PR/архив, Telegram + IDE |
| **Pro** | 19 900 ₽/мес | 50 часов, 2 задачи одновременно, лимит задачи 60 мин, приоритет в очереди, callback-и в свои системы |
| **Teams** | 29 900 ₽/мес | 100 часов на команду с общим бюджетом, до 5 ключей, 3 задачи одновременно, лимит 120 мин, отчёт по ключам (`/v1/usage`) |
| Доп. часы | 490 ₽/час | Сверх пакета, списание с баланса |

Токены — отдельно, по обычным ценам шлюза (у Sonnet-класса задача на 30 минут обычно 0.5–3 $ объёма). Для Teams общий бюджет = сумма минут по всем `key_id` команды + их балансы на шлюзе.

## Риски

* **Безопасность сэндбокса.** Это обычный контейнер, не gVisor/Firecracker: ядро общее с хостом. Мы убираем capabilities, запрещаем привилегии, ограничиваем ресурсы и сеть, но 0-day в ядре/runc — риск. Смягчение на следующем шаге: `--runtime=runsc` (gVisor) на сервере, отдельный хост для сэндбоксов, AppArmor-профиль. Allow-list исходящих работает на уровне доменов через CONNECT — содержимое HTTPS прокси не видит (и не должен).
* **Код клиента — недоверенный ввод для агента.** Prompt-injection в README/issue может заставить агента делать странное внутри сэндбокса; наружу он может только через allow-list, токенов у него нет, пушит оркестратор. Но PR надо ревьюить — об этом стоит написать клиенту в карточке результата.
* **Стоимость длинных задач.** 120 минут Sonnet с большим контекстом — это десятки долларов объёма с баланса клиента. Защита: лимит минут, `max_iterations`, дневной лимит ключа на шлюзе, таск-бюджеты в панели (показывать прогноз перед запуском, `metrics` в статусе). В REST OpenHands нет `max_budget_per_run` — бюджет в долларах можно добавить в оркестратор по `metrics.accumulated_cost`, если шлюз отдаёт цены (для кастомных моделей litellm цену не знает → 0).
* **Лимит docker-сетей.** По умолчанию Docker выдаёт ~31 подсеть /16 + 16 /20 — одна на задачу; `MAX_CONCURRENT_TASKS` держать ниже, при росте — `default-address-pools` в `daemon.json`.
* **Диск.** Клон + node_modules на задачу; `TASKS_ROOT` чистится после задачи (кроме `keep_workspace`). Нужен мониторинг места.
* **Ключ в памяти.** Рестарт оркестратора = потеря очереди (задачи помечаются `failed`, клиент перезапускает). Осознанно: иначе ключи пришлось бы хранить.
* **GitHub-токен клиента** нужен для PR. В IDE берётся из VS Code GitHub-аутентификации, в боте — TODO (см. DECISIONS).

## Разработка

```bash
cd orchestrator
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tkbk_agents tests
python ../scripts/smoke_sandbox.py      # нужен Docker, тянет образ ~2 ГБ
```

Лицензия MIT. Модельные провайдеры в коде и документации не называются — только шлюз `api.tkbk.online`.
