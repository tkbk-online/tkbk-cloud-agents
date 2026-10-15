// Signed client for the TKBK Cloud Agents API — stub for the TKBK Agent VS Code extension.
// Target path in tkbk-online/tkbk-agent: apps/vscode/src/services/tkbk/cloudClient.ts
//
// TODO(owner): the HMAC secret must NOT ship inside the extension. Until the auth decision is made
// (see README.md in this folder), `secret` is injected from a VS Code setting for local testing only.

import { createHmac } from "node:crypto"

export type TaskTemplate = "fix_issue" | "add_tests" | "update_deps" | "pr_from_spec" | "custom"
export type TaskStatus = "queued" | "preparing" | "running" | "delivering" | "done" | "failed" | "cancelled" | "timeout"

export interface CreateTaskRequest {
	api_key: string // cr_… from the TKBK provider settings
	repo: string // https://github.com/<owner>/<repo>
	branch?: string
	template?: TaskTemplate
	instruction?: string
	issue?: number
	model?: string // e.g. claude-sonnet-5 (default on the server)
	git_token?: string // from vscode.authentication.getSession("github", ["repo"])
	deliver?: "pr" | "archive"
	max_minutes?: number
	client_ref?: string
}

export interface TaskView {
	id: string
	status: TaskStatus
	phase: string
	minutes_billed: number
	max_minutes: number
	error?: string | null
	result?: { pr_url?: string | null; archive_url?: string | null; summary?: string; changed_files?: string[] } | null
}

export interface ApiEnvelope<T> {
	ok: boolean
	error?: string
	task?: T
}

export const TERMINAL: ReadonlySet<TaskStatus> = new Set(["done", "failed", "cancelled", "timeout"])

export class CloudAgentsClient {
	constructor(
		private readonly baseUrl: string,
		private readonly secret: string,
		private readonly fetchImpl: typeof fetch = fetch,
	) {}

	/** Same scheme as the TKBK bot: ts\nMETHOD\npath?query\nbody, HMAC-SHA256 hex. */
	sign(method: string, pathWithQuery: string, body: string, ts = Math.floor(Date.now() / 1000)): Record<string, string> {
		const msg = `${ts}\n${method.toUpperCase()}\n${pathWithQuery}\n${body}`
		const sig = createHmac("sha256", this.secret).update(msg).digest("hex")
		return { "X-TKBK-Ts": String(ts), "X-TKBK-Sig": sig }
	}

	private async call<T>(method: string, pathWithQuery: string, body?: unknown): Promise<ApiEnvelope<T>> {
		const raw = body === undefined ? "" : JSON.stringify(body)
		const headers: Record<string, string> = { Accept: "application/json", ...this.sign(method, pathWithQuery, raw) }
		if (body !== undefined) headers["Content-Type"] = "application/json"
		const res = await this.fetchImpl(this.baseUrl.replace(/\/$/, "") + pathWithQuery, {
			method,
			headers,
			body: body === undefined ? undefined : raw,
		})
		const json = (await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }))) as ApiEnvelope<T>
		return json
	}

	createTask(req: CreateTaskRequest): Promise<ApiEnvelope<TaskView>> {
		return this.call<TaskView>("POST", "/v1/tasks", req)
	}

	getTask(id: string): Promise<ApiEnvelope<TaskView>> {
		return this.call<TaskView>("GET", `/v1/tasks/${encodeURIComponent(id)}`)
	}

	cancel(id: string): Promise<ApiEnvelope<TaskView>> {
		return this.call<TaskView>("POST", `/v1/tasks/${encodeURIComponent(id)}/cancel`)
	}

	/** Polls until the task reaches a terminal status. */
	async waitFor(id: string, onUpdate?: (t: TaskView) => void, pollMs = 10_000, signal?: AbortSignal): Promise<TaskView> {
		for (;;) {
			const r = await this.getTask(id)
			if (!r.ok || !r.task) throw new Error(r.error ?? "status failed")
			onUpdate?.(r.task)
			if (TERMINAL.has(r.task.status)) return r.task
			if (signal?.aborted) {
				await this.cancel(id)
				throw new Error("cancelled")
			}
			await new Promise((res) => setTimeout(res, pollMs))
		}
	}
}
