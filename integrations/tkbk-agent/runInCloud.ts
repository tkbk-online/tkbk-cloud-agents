// VS Code command "tkbk.runInCloud" — «Запустить в облаке TKBK». Stub with TODOs.
// Target path in tkbk-online/tkbk-agent: apps/vscode/src/hosts/vscode/commands/runInCloud.ts
//
// Flow: origin URL of the workspace repo -> GitHub token via VS Code auth -> POST /v1/tasks
//       -> status bar progress -> notification with the PR link.

import * as vscode from "vscode"
import { CloudAgentsClient, type CreateTaskRequest, type TaskTemplate } from "../../../services/tkbk/cloudClient"

const TEMPLATES: Array<vscode.QuickPickItem & { id: TaskTemplate }> = [
	{ id: "custom", label: "Своя задача", description: "Произвольная инструкция" },
	{ id: "fix_issue", label: "Исправить issue", description: "Номер issue + PR с фиксом" },
	{ id: "add_tests", label: "Добавить тесты", description: "Покрыть тестами, поведение не менять" },
	{ id: "update_deps", label: "Обновить зависимости", description: "Минорные/патчи, прогнать тесты" },
	{ id: "pr_from_spec", label: "PR по ТЗ", description: "Реализовать фичу по описанию" },
]

async function originRepoUrl(): Promise<string | undefined> {
	// TODO: use the built-in git extension API (vscode.git) instead of spawning git.
	const git = vscode.extensions.getExtension<any>("vscode.git")?.exports?.getAPI?.(1)
	const repo = git?.repositories?.[0]
	const url: string | undefined = repo?.state?.remotes?.find((r: any) => r.name === "origin")?.fetchUrl
	if (!url) return undefined
	const m = url.match(/github\.com[:/]([^/]+)\/([^/]+?)(?:\.git)?$/)
	return m ? `https://github.com/${m[1]}/${m[2]}` : undefined
}

export function registerRunInCloud(context: vscode.ExtensionContext, getTkbkApiKey: () => Promise<string | undefined>) {
	const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50)
	context.subscriptions.push(status)

	context.subscriptions.push(
		vscode.commands.registerCommand("tkbk.runInCloud", async (prefillInstruction?: string) => {
			const apiKey = await getTkbkApiKey()
			if (!apiKey) {
				void vscode.window.showWarningMessage("Сначала добавьте ключ TKBK (cr_…) в настройках провайдера.")
				return
			}
			const repo = await originRepoUrl()
			if (!repo) {
				void vscode.window.showWarningMessage("Облачный агент работает с репозиториями на GitHub (origin не найден).")
				return
			}
			const tpl = await vscode.window.showQuickPick(TEMPLATES, { title: "TKBK Cloud: что сделать?" })
			if (!tpl) return
			const req: CreateTaskRequest = { api_key: apiKey, repo, template: tpl.id, client_ref: "ide", deliver: "archive" }
			if (tpl.id === "fix_issue") {
				const n = await vscode.window.showInputBox({ prompt: "Номер issue", validateInput: (v) => (/^\d+$/.test(v) ? undefined : "число") })
				if (!n) return
				req.issue = Number(n)
			} else if (tpl.id !== "update_deps") {
				const text = await vscode.window.showInputBox({ prompt: "Задача для агента", value: prefillInstruction ?? "" })
				if (!text) return
				req.instruction = text
			}
			// GitHub token through VS Code's own GitHub auth -> PR delivery. Without it: archive.
			const session = await vscode.authentication.getSession("github", ["repo"], { createIfNone: false })
			if (session) {
				req.git_token = session.accessToken
				req.deliver = "pr"
			}

			const cfg = vscode.workspace.getConfiguration("tkbk.cloud")
			// TODO(owner): replace the shared secret with per-key auth (see README.md).
			const client = new CloudAgentsClient(cfg.get<string>("url", "https://agents.tkbk.online"), cfg.get<string>("secret", ""))
			const created = await client.createTask(req)
			if (!created.ok || !created.task) {
				void vscode.window.showErrorMessage(`TKBK Cloud: ${created.error ?? "не удалось создать задачу"}`)
				return
			}
			const id = created.task.id
			status.text = `$(cloud) TKBK: ${id} queued`
			status.show()
			try {
				const final = await client.waitFor(id, (t) => {
					status.text = `$(cloud) TKBK: ${t.status}${t.phase ? ` · ${t.phase}` : ""} · ${t.minutes_billed || 0}/${t.max_minutes} мин`
				})
				const link = final.result?.pr_url ?? final.result?.archive_url
				const msg = `TKBK Cloud: задача ${final.status}${final.error ? ` — ${final.error}` : ""}`
				const action = link ? await vscode.window.showInformationMessage(msg, "Открыть результат") : void vscode.window.showInformationMessage(msg)
				if (action && link) void vscode.env.openExternal(vscode.Uri.parse(link))
			} finally {
				status.hide()
			}
		}),
	)
}
