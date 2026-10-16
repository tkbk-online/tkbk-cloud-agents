# Контракт для TKBK IDE / TKBK Agent — облачные задачи

Сервер: `https://agents.tkbk.online` (оркестратор TKBK Cloud Agents). Клиент: [`client/tkbkCloudAgents.ts`](../client/tkbkCloudAgents.ts) (fetch + SSE, без зависимостей).

## Авторизация

`Authorization: Bearer cr_…` — ключ клиента TKBK. Оркестратор спрашивает сайт `POST https://tkbk.online/api/license/check` (ответ кэшируется 10 минут): облачные задачи доступны планам **Pro / Teams / Teams+** (feature `cloud_agents`). Free → `403 {"ok":false,"error":"plan free does not include cloud agents"}` — показать кнопку «Купить Pro» (`tkbk.online/buy?plan=pro#ide-pro`).

Токены модели агент тратит **с этого же ключа** через шлюз `api.tkbk.online` (Claude — Anthropic-формат, остальные — OpenAI-формат); отдельно ничего не биллится. Квота плана — часы агента в месяц: Pro 20, Teams 100, Teams+ 300 (из `license.ts` сайта); минута = минута жизни сэндбокса, вверх до целой. Ключ сервер не хранит и не логирует.

Ответы: `{"ok": true, …}` или `{"ok": false, "error": "…"}` со статусом 4xx/5xx. Лимит 60 запросов в минуту на ключ.

## Эндпоинты

| Метод | Путь | Что |
|---|---|---|
| `GET` | `/v1/me` | План, часы, `minutes_used/left`, `concurrent_limit`, `active_tasks` |
| `POST` | `/v1/tasks` | Создать задачу → `201 {task, upload_url}` |
| `PUT` | `/v1/tasks/{id}/workspace` | Для `source: "upload"`: тело — tar.gz рабочей копии (≤ 200 МБ), `Content-Type: application/gzip` |
| `GET` | `/v1/tasks/{id}` | `{task, log_tail}` — статус, фаза, минуты, метрики, последние 20 событий |
| `GET` | `/v1/tasks/{id}/events` | **SSE**: события задачи; `?after=N` или `Last-Event-ID` для возобновления; закрывается после `done` |
| `GET` | `/v1/tasks/{id}/diff` | `text/x-patch` — unified diff (`git apply`); 404, пока патча нет |
| `GET` | `/v1/tasks/{id}/artifacts` | Список: `patch`, `archive` (tar.gz дерева, если `deliver: "archive"`), `log` (JSONL событий) |
| `GET` | `/v1/tasks/{id}/artifacts/{name}` | Байты артефакта |
| `GET` | `/v1/tasks/{id}/result` | Итог: статус, ошибка, минуты, метрики, `result`, `artifacts` |
| `POST` | `/v1/tasks/{id}/cancel` | Отмена (в очереди — сразу, в работе — на ближайшей контрольной точке) |
| `GET` | `/v1/tasks?limit=` | Свои задачи (новые первыми) |
| `GET` | `/v1/templates` | Шаблоны и лимиты сервера |

Клиент видит только задачи своего ключа (чужой id → 404).

### POST /v1/tasks — тело

```json
{
  "repoUrl": "https://github.com/me/app",      // или "source": "upload" без repoUrl
  "branch": "main",                            // необязательно (default branch репозитория)
  "template": "custom",                        // fix_issue | add_tests | update_deps | pr_from_spec | custom
  "prompt": "добавь тесты для src/keys.ts",    // инструкция агенту (для fix_issue — поле issue)
  "issue": 42,
  "model": "claude-sonnet-5",                  // по умолчанию сервера; gpt-5.5, deepseek-… тоже через шлюз
  "gitToken": "ghp_…",                         // только для приватного клона или push; агент токен не видит
  "deliver": "patch",                          // patch (по умолчанию) | archive | pr
  "push": false,                               // true = ветка tkbk/<taskId> в репозиторий клиента (нужен gitToken)
  "maxMinutes": 60,                            // по умолчанию 60, потолок 120
  "clientRef": "ide:session-id"
}
```

Snake_case имена (`repo`, `instruction`, `git_token`, `max_minutes`, `client_ref`) принимаются тоже.

Ошибки: `400` неверные поля, `402` исчерпаны часы плана, `403` план без облачных агентов, `413` архив больше лимита, `429` лимит запросов.

### Статусы

`awaiting_upload → queued → preparing (fetching-inputs | cloning | importing-workspace | starting-sandbox) → running (agent) → delivering → done | failed | cancelled | timeout`

`timeout` — лимит минут: агент остановлен, что успело измениться — всё равно оформлено как patch (а при `deliver: "pr"` — draft-PR с пометкой `[timeout]`).

Очередь: `concurrent_limit` задач на ключ одновременно (Pro 3, Teams 5, Teams+ 10), остальные ждут в `queued`.

### SSE-события

```
id: 7
event: agent
data: {"seq":7,"ts":"2026-10-16T10:00:03+00:00","type":"agent","kind":"action","tool":"terminal","text":"pytest -q","thought":"Run the suite first"}
```

| `type` | Поля | Когда |
|---|---|---|
| `status` | `status`, `phase` | смена статуса/фазы |
| `agent` | `kind` (`action` \| `observation` \| `message` \| `error`), `tool`, `text` (≤ 2000 символов), `thought`, `exit_code` | каждое действие агента и его результат |
| `log` | `level`, `message` | сообщения оркестратора (ошибки) |
| `done` | `status`, `error`, `minutes_billed`, `result` | конец; поток закрывается |

### Результат (`task.result`)

```json
{
  "patch_url": "https://agents.tkbk.online/v1/tasks/t_…/diff",
  "archive_url": null,
  "log_url": "https://agents.tkbk.online/v1/tasks/t_…/artifacts/log",
  "pr_url": null,
  "branch": "tkbk/9f3c…", "pushed": false, "commit": "abc123…",
  "changed_files": ["src/keys.test.ts"],
  "summary": "Резюме агента: что сделано, как проверено, что осталось",
  "commands": 14
}
```

Патч считается от состояния репозитория на момент клона (или от загруженной рабочей копии как есть, включая незакоммиченные файлы). Применить в IDE: `git apply --3way <patch>`; при `push: true` — `git fetch origin tkbk/<id>`.

## Типовой сценарий в расширении

1. `me()` при включении функции → показать план и остаток часов; при `cloud_agents: false` — кнопка «Купить Pro».
2. Кнопка «Запустить в облаке»: repoUrl из `origin` (GitHub) → `createTask({repoUrl, prompt})`; если origin не GitHub или есть незакоммиченные правки — `source: "upload"` + `uploadWorkspace(tar.gz без node_modules/.git больших каталогов)`.
3. `for await (const ev of events(id))` → панель прогресса (действия агента, фаза, минуты); кнопка «Отменить» → `cancel(id)`.
4. По `done`: `diff(id)` → предложить применить патч / открыть `pr_url` / показать `summary`.

## Локальная проверка без сервера

`docker compose --profile stub up -d` с `LICENSE_MODE=stub` (см. README) даёт тот же API на `http://localhost:8090` с фейковой моделью: задача создаёт файл `TKBK_CLOUD_AGENT.md`; любой ключ вида `cr_…` принимается как Pro.
