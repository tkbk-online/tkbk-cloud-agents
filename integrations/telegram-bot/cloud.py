"""/cloud <repo> <задача> — stub for the TKBK Telegram bot (aiogram 3, H:\\TKBK\\Бот магазин tkbk).

STATUS: integration stub with TODOs. Not wired into the bot yet.

How it fits the bot:
  * The bot already signs requests to the site with HMAC (app/ea_internal.py scheme). The
    orchestrator uses the same scheme with AGENTS_INTERNAL_SECRET — see integrations/client.
  * The client's cr_ key is NOT stored by the orchestrator; the bot must obtain it per request.
    The bot knows which key belongs to a Telegram account (tg_links from the site's key delivery),
    so it can resolve `user.id -> cr_…` through the site's internal API.

Install (when ready):
  1. copy this file to app/handlers/cloud.py, add `from .handlers.cloud import router as cloud_router`
     and `dp.include_router(cloud_router)` in main.py;
  2. env: AGENTS_URL=https://agents.tkbk.online, AGENTS_INTERNAL_SECRET=<same as the orchestrator>;
  3. implement the two TODO helpers below.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

try:  # aiogram is the bot's framework; keep the stub importable without it for tests/tools
    from aiogram import Router
    from aiogram.filters import Command
    from aiogram.types import Message
except ImportError:  # pragma: no cover
    Router = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from tkbk_agents_client import AgentsClient  # noqa: E402

REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/?$")
USAGE = (
    "Использование: /cloud <ссылка на GitHub-репозиторий> <задача>\n"
    "Пример: /cloud https://github.com/me/app добавь тесты для модуля auth\n"
    "Шаблоны: /cloud <repo> issue 42  — исправить issue;  /cloud <repo> deps — обновить зависимости."
)


def parse_cloud_args(text: str) -> tuple[str, str, dict] | None:
    """Returns (repo, template, extra fields) or None if the text does not parse."""
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not REPO_RE.match(parts[1]):
        return None
    repo, rest = parts[1].rstrip("/"), parts[2].strip()
    m = re.match(r"^issue\s+#?(\d+)\s*(.*)$", rest, re.I)
    if m:
        return repo, "fix_issue", {"issue": int(m.group(1)), "instruction": m.group(2)}
    if rest.lower() in ("deps", "dependencies", "зависимости"):
        return repo, "update_deps", {"instruction": ""}
    if rest.lower().startswith(("tests", "тест")):
        return repo, "add_tests", {"instruction": rest}
    return repo, "custom", {"instruction": rest}


async def resolve_client_key(tg_user_id: int) -> str | None:
    """TODO: return the cr_ key linked to this Telegram account (site internal API: tg_links),
    or None when the account has no key. Never log the key."""
    raise NotImplementedError


async def resolve_github_token(tg_user_id: int) -> str | None:
    """TODO: GitHub token the client registered for cloud agents (needed for private repos, push, PR).
    Without a token the task falls back to deliver=archive."""
    return None


def format_status(task: dict) -> str:
    st = task.get("status")
    lines = [f"Задача {task.get('id')} — {st}" + (f" ({task.get('phase')})" if task.get("phase") else "")]
    if task.get("minutes_billed"):
        lines.append(f"Минут агента: {task['minutes_billed']}")
    res = task.get("result") or {}
    if res.get("pr_url"):
        lines.append(f"PR: {res['pr_url']}")
    if res.get("archive_url"):
        lines.append(f"Архив: {res['archive_url']}")
    if task.get("error"):
        lines.append(f"Ошибка: {task['error']}")
    if res.get("summary"):
        lines.append("\n" + res["summary"][:1500])
    return "\n".join(lines)


if Router is not None:
    router = Router(name="cloud")

    def _client() -> AgentsClient:
        return AgentsClient(os.environ.get("AGENTS_URL", "https://agents.tkbk.online"),
                            os.environ["AGENTS_INTERNAL_SECRET"])

    @router.message(Command("cloud"))
    async def cmd_cloud(message: Message) -> None:
        parsed = parse_cloud_args(message.text or "")
        if parsed is None:
            await message.answer(USAGE)
            return
        repo, template, extra = parsed
        key = await resolve_client_key(message.from_user.id)  # type: ignore[union-attr]
        if not key:
            await message.answer("У этого аккаунта нет ключа TKBK. Купить: https://tkbk.online/buy")
            return
        token = await resolve_github_token(message.from_user.id)  # type: ignore[union-attr]
        body = {"api_key": key, "repo": repo, "template": template, **extra,
                "deliver": "pr" if token else "archive", "client_ref": f"tg:{message.from_user.id}"}  # type: ignore[union-attr]
        if token:
            body["git_token"] = token
        # TODO: callback_url -> a bot endpoint (HMAC-signed by the orchestrator) instead of polling.
        out = await asyncio.to_thread(_client().create_task, **body)
        if not out.get("ok"):
            await message.answer(f"Не удалось запустить: {out.get('error', 'ошибка')}")
            return
        task = out["task"]
        await message.answer(f"Запустил облачного агента: {task['id']} (лимит {task['max_minutes']} мин).\n"
                             f"Статус: /cloud_status {task['id']}")

    @router.message(Command("cloud_status"))
    async def cmd_cloud_status(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Использование: /cloud_status <id задачи>")
            return
        out = await asyncio.to_thread(_client().task, parts[1])
        await message.answer(format_status(out.get("task") or {}) if out.get("ok") else f"Ошибка: {out.get('error')}")
