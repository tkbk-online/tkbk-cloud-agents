# Интеграция с TKBK Agent (форк Cline) — что брать

Актуальный контракт для расширения: [`docs/ide-client.md`](../../docs/ide-client.md) и клиент [`client/tkbkCloudAgents.ts`](../../client/tkbkCloudAgents.ts) (fetch + SSE, без зависимостей, `Authorization: Bearer cr_…` — тот же ключ, что в настройках провайдера TKBK; HMAC-секрет расширению НЕ нужен).

Файлы в этой папке — ранняя заглушка кнопки «Запустить в облаке TKBK» (`runInCloud.ts`, `package.contributes.json`) и устаревший `cloudClient.ts` с HMAC; при переносе в `tkbk-online/tkbk-agent` заменить `cloudClient.ts` на `client/tkbkCloudAgents.ts` и убрать настройку `tkbk.cloud.secret`.

Сценарий в расширении (кратко): `me()` → план/часы; кнопка → `createTask({repoUrl, prompt})` или `source: "upload"` + `uploadWorkspace(tar.gz)` при незакоммиченных правках / не-GitHub origin; `for await (ev of events(id))` → панель прогресса; по `done` → `diff(id)` → `git apply --3way`.
