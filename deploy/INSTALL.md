# Развёртывание — отдельный VPS (Ubuntu 24.04)

Коротко: `deploy/bootstrap.sh` делает всё за 15 минут (README, раздел «Как поднять»). Здесь — что он делает, ручной путь и эксплуатация. На сервер TKBK (194.87.49.168, 2 vCPU / 3.8 ГБ) не ставить: сэндбоксы по 2 vCPU / 4 ГБ.

## 0. До запуска

* VPS: см. таблицу в README (10 задач → 16–24 vCPU / 48 ГБ / 250 ГБ NVMe). Ubuntu 24.04, root по SSH.
* DNS: `A agents.tkbk.online → IP VPS` (Caddy выпустит сертификат Let's Encrypt при первом запросе).
* Сайт tkbk.online должен отдавать `POST /api/license/check` (коммит с лицензиями на main сайта, на 16.10 не задеплоен — без него оркестратор отвечает 403 всем ключам).

## 1. Автоматически

```bash
curl -fsSL https://raw.githubusercontent.com/tkbk-online/tkbk-cloud-agents/main/deploy/bootstrap.sh | sudo bash
```

Шаги скрипта: docker-ce + compose-plugin, `daemon.json` (address-pools под сети задач, лимит логов), ufw 22/80/443, клон в `/opt/tkbk-agents`, `.env` со свежим секретом и доменом, `docker pull` образа сэндбокса, `docker compose build && up -d`, печать того, что осталось владельцу.

## 2. Вручную (то же самое по шагам)

```bash
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin ufw git python3
ufw default deny incoming && ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
git clone https://github.com/tkbk-online/tkbk-cloud-agents.git /opt/tkbk-agents && cd /opt/tkbk-agents
cp .env.example .env && nano .env     # AGENTS_INTERNAL_SECRET (openssl rand -hex 32), PUBLIC_BASE_URL, AGENTS_DOMAIN
docker pull ghcr.io/openhands/agent-server:1.55.0-python
docker compose build && docker compose up -d
docker compose ps && curl -s https://agents.tkbk.online/health
```

Ожидается `{"ok":true,"version":"0.2.0","active":0,"secret_configured":true,"license_mode":"site"}`.

## 3. Проверки

```bash
python3 scripts/smoke_sandbox.py                        # 8 проверок сэндбокса и egress (без ключа)
curl -s https://agents.tkbk.online/v1/me -H "Authorization: Bearer cr_…"   # план и часы по ключу Pro/Teams
```

## 4. Первая задача с реальным ключом

```bash
curl -s -X POST https://agents.tkbk.online/v1/tasks -H "Authorization: Bearer cr_…" -H "Content-Type: application/json" \
  -d '{"repoUrl":"https://github.com/tkbk-online/tkbk-mcp","prompt":"добавь тесты для парсера конфигурации","maxMinutes":30}'
curl -s -N https://agents.tkbk.online/v1/tasks/<id>/events -H "Authorization: Bearer cr_…"
curl -s https://agents.tkbk.online/v1/tasks/<id>/diff -H "Authorization: Bearer cr_…"
```

Смотреть: `docker ps` (контейнер `tkbk-sbx-t_…`, сеть `tkbk-task-t_…`), `docker exec tkbk-agents-egress tail -f /var/log/squid/access.log` (только allow-list-хосты), `docker compose logs -f api`.

## 5. Эксплуатация

* `.env` → `LICENSE_PUBLIC_KEY` (публичный Ed25519-ключ из админки сайта): ответы лицензий проверяются по подписи. `AGENTS_INTERNAL_SECRET` — в бот/сайт для HMAC-стороны.
* Данные: `/opt/tkbk-agents/data/agents.db` (SQLite: задачи, минуты; ключей нет), `data/results/*.patch|*.tgz`, `data/events/*.jsonl` — чистятся джанитором через `RESULTS_TTL_DAYS` (7). Workspaces — volume `tkbk-agents_tasks`, удаляются по концу задачи.
* Обновление: `git pull && docker compose build && docker compose up -d` при `active: 0` в `/health` (рестарт api = активные задачи `failed`). Версия сэндбокса — `SANDBOX_IMAGE` в `.env`, не `latest`.
* Allow-list: `deploy/egress/allowlist.txt` → `docker compose restart egress` между задачами.
* Вариант на хост с уже стоящим Caddy: `deploy/compose.shared-caddy.yml` + блок `deploy/caddy/shared-caddy-block.caddy` (дописывать в Caddyfile на месте, не заменять файл).

## 6. Откат

```bash
cd /opt/tkbk-agents && docker compose --profile stub down -v
docker ps -aq --filter label=tkbk.cloud-agents | xargs -r docker rm -f
docker network ls -q --filter label=tkbk.cloud-agents | xargs -r docker network rm
```
