# Развёртывание на сервере TKBK (194.87.49.168, Ubuntu 24.04, Docker 29)

Ничего из этого ещё не выполнялось на сервере — только локальная проверка. Делать после подтверждения владельца.

## 0. Что нужно заранее

* DNS: `A agents.tkbk.online → 194.87.49.168`.
* Секрет: `openssl rand -hex 32` → `AGENTS_INTERNAL_SECRET` (его же — в бот/сайт/панель, кто будет звать API).
* На сервере свободно ≥ 12 ГБ RAM под 2–3 сэндбокса по 4 ГБ (у `tkbk-shop` лимит 512 МБ, Caddy мало) и ≥ 10 ГБ диска (образ agent-server 2 ГБ + клоны).
* Образы: `ghcr.io/openhands/agent-server:1.55.0-python` (amd64), `ubuntu/squid:6.6-24.04_edge`, `docker:29-cli` (для сборки), `python:3.12-slim-bookworm`.

## 1. Файлы

```bash
sudo mkdir -p /opt/tkbk-agents /var/lib/tkbk-agents/tasks
cd /opt/tkbk-agents
git clone https://github.com/tkbk-online/tkbk-cloud-agents.git .
cp .env.example .env && nano .env        # AGENTS_INTERNAL_SECRET, PUBLIC_BASE_URL=https://agents.tkbk.online
```

`TASKS_ROOT=/var/lib/tkbk-agents/tasks` должен быть **одним и тем же абсолютным путём** на хосте и внутри контейнера `api` (compose так и монтирует) — сэндбоксы bind-mount-ят подкаталоги этого пути через docker.sock хоста.

## 2. Сеть Caddy и сборка

```bash
docker network inspect tkbk_default >/dev/null       # сеть проекта tkbk (Caddy + shop) должна существовать
docker pull ghcr.io/openhands/agent-server:1.55.0-python
docker compose build
docker compose up -d
docker compose ps && curl -s http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' tkbk-agents-api | cut -d' ' -f1):8090/health
```

Ожидается `{"ok":true,"version":"0.1.0","active":0,"secret_configured":true}`.

## 3. Caddy — только правка на месте

`/opt/tkbk-shop/Caddyfile` — bind-mount, **файл не заменять** (заменённый файл отвязывается от контейнера; см. память проекта). Дописать блок и перечитать:

```bash
cat /opt/tkbk-agents/deploy/caddy/agents.tkbk.online.caddy | sudo tee -a /opt/tkbk-shop/Caddyfile >/dev/null
docker exec tkbk-caddy caddy validate --config /etc/caddy/Caddyfile
docker exec tkbk-caddy caddy reload   --config /etc/caddy/Caddyfile
sha256sum /opt/tkbk-shop/Caddyfile; docker exec tkbk-caddy sha256sum /etc/caddy/Caddyfile   # должны совпасть
curl -s https://agents.tkbk.online/health
```

## 4. Смоук без LLM-ключа

```bash
cd /opt/tkbk-agents && python3 scripts/smoke_sandbox.py
```

Должны пройти все 8 проверок (health сэндбокса, нет прямого интернета, прокси пускает api.tkbk.online/PyPI и режет example.com, uid 10001 без capabilities, лимиты в HostConfig).

## 5. Первая реальная задача (нужен ключ cr_… и GitHub-токен владельца)

```bash
export AGENTS_URL=https://agents.tkbk.online AGENTS_INTERNAL_SECRET=<секрет>
python3 integrations/client/tkbk_agents_client.py create --key cr_... \
   --repo https://github.com/tkbk-online/tkbk-api --template add_tests \
   --instruction "добавь тесты для модуля ключей" --git-token ghp_... --max-minutes 15 --wait
```

Что смотреть: `docker ps` (контейнер `tkbk-sbx-t_…`, сеть `tkbk-task-t_…`), `docker exec tkbk-agents-egress tail -f /var/log/squid/access.log` (только allow-list-хосты), `docker logs -f tkbk-agents-api`. Результат — PR в репозитории, `GET /v1/usage` — минуты по ключу.

## 6. Эксплуатация

* Логи: `docker logs tkbk-agents-api`, `docker exec tkbk-agents-egress tail /var/log/squid/access.log`.
* Данные: `/opt/tkbk-agents/data/agents.db` (SQLite, учёт минут; ключей там нет), `/opt/tkbk-agents/data/results/*.tgz` (архивы — чистить cron-ом старше 7 дней).
* Обновление версии agent-server: сменить `SANDBOX_IMAGE` в `.env`, `docker pull`, `docker compose restart api`. Тег `latest-python` не использовать — ломает воспроизводимость.
* Allow-list: `deploy/egress/allowlist.txt`, затем `docker compose restart egress` (идущие задачи на секунды теряют прокси — делать между задачами).
* Рестарт `api` = активные задачи `failed` (ключи были в памяти) — делать при `active: 0` в `/health`.

## 7. Откат

```bash
cd /opt/tkbk-agents && docker compose down
docker ps -aq --filter label=tkbk.cloud-agents | xargs -r docker rm -f
docker network ls -q --filter label=tkbk.cloud-agents | xargs -r docker network rm
# убрать блок agents.tkbk.online из /opt/tkbk-shop/Caddyfile (на месте, редактором) и caddy reload
```
