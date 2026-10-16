/**
 * TKBK Cloud Agents — client for the TKBK Agent / TKBK IDE extension.
 * fetch + SSE over fetch (ReadableStream), no dependencies; works in the VS Code extension host (Node ≥ 18)
 * and in browsers. Contract: docs/ide-client.md.
 *
 *   const cloud = new TkbkCloudAgents({ baseUrl: "https://agents.tkbk.online", apiKey: "cr_…" });
 *   const me = await cloud.me();                              // plan, hours left
 *   const { task } = await cloud.createTask({ repoUrl, prompt: "add tests for src/keys.ts" });
 *   for await (const ev of cloud.events(task.id)) render(ev);  // live progress
 *   const patch = await cloud.diff(task.id);                   // unified diff
 */

export type TaskStatus =
	| "awaiting_upload"
	| "queued"
	| "preparing"
	| "running"
	| "delivering"
	| "done"
	| "failed"
	| "cancelled"
	| "timeout"
export type Template = "fix_issue" | "add_tests" | "update_deps" | "pr_from_spec" | "custom"
export type Deliver = "patch" | "archive" | "pr"

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set(["done", "failed", "cancelled", "timeout"])

export interface CreateTaskRequest {
	/** https://github.com/<owner>/<repo>; omit with source: "upload" */
	repoUrl?: string
	source?: "repo" | "upload"
	branch?: string
	template?: Template
	/** the task for the agent (instruction) */
	prompt?: string
	issue?: number
	/** TKBK model id, e.g. claude-sonnet-5 (server default) or gpt-5.5 */
	model?: string
	/** GitHub token — only for private clone or push; never required for patch delivery */
	gitToken?: string
	deliver?: Deliver
	/** push the result to branch tkbk/<taskId> (default false; needs gitToken) */
	push?: boolean
	maxMinutes?: number
	maxIterations?: number
	clientRef?: string
}

export interface TaskResult {
	patch_url: string | null
	archive_url: string | null
	log_url: string | null
	pr_url: string | null
	branch: string | null
	pushed: boolean
	commit: string | null
	changed_files: string[]
	summary: string
	commands: number
}

export interface TaskMetrics {
	prompt_tokens: number
	completion_tokens: number
	cache_read_tokens: number
	cache_write_tokens: number
	cost_usd_reported: number
	iterations: number
}

export interface Task {
	id: string
	status: TaskStatus
	phase: string
	source: "repo" | "upload"
	template: Template
	repo: string | null
	base_branch: string | null
	work_branch: string | null
	model: string
	deliver: Deliver
	push: boolean
	plan: string
	created_at: string
	started_at: string | null
	finished_at: string | null
	max_minutes: number
	minutes_billed: number
	metrics: TaskMetrics
	error: string | null
	result: TaskResult | null
	events: number
}

export interface Me {
	ok: boolean
	key_id: string
	key_prefix: string
	plan: string
	cloud_agents: boolean
	hours_month: number
	minutes_used: number
	minutes_left: number
	concurrent_limit: number
	active_tasks: number
	until: string | null
	error: string | null
}

export interface AgentEvent {
	seq: number
	ts: string
	type: "status" | "agent" | "log" | "done"
	status?: TaskStatus
	phase?: string
	kind?: "action" | "observation" | "message" | "error"
	tool?: string
	text?: string
	thought?: string
	exit_code?: number
	message?: string
	error?: string | null
	minutes_billed?: number
	result?: TaskResult
}

export interface Artifact {
	name: "patch" | "archive" | "log"
	bytes: number
	media_type: string
	url: string
}

export class TkbkCloudError extends Error {
	constructor(
		message: string,
		public readonly status: number,
		public readonly code?: string,
	) {
		super(message)
		this.name = "TkbkCloudError"
	}
}

export interface ClientOptions {
	baseUrl: string
	/** the client's TKBK key cr_… (Pro / Teams plan) */
	apiKey: string
	fetch?: typeof fetch
	/** default 20 s for JSON calls; uploads and SSE are not limited by it */
	timeoutMs?: number
}

export class TkbkCloudAgents {
	private readonly base: string
	private readonly f: typeof fetch
	private readonly timeoutMs: number

	constructor(private readonly opts: ClientOptions) {
		this.base = opts.baseUrl.replace(/\/+$/, "")
		this.f = opts.fetch ?? fetch
		this.timeoutMs = opts.timeoutMs ?? 20_000
	}

	private headers(extra?: Record<string, string>): Record<string, string> {
		return { Authorization: `Bearer ${this.opts.apiKey}`, Accept: "application/json", ...(extra ?? {}) }
	}

	private async json<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
		const ctrl = new AbortController()
		const timer = setTimeout(() => ctrl.abort(), this.timeoutMs)
		signal?.addEventListener("abort", () => ctrl.abort(), { once: true })
		try {
			const res = await this.f(this.base + path, {
				method,
				headers: this.headers(body === undefined ? undefined : { "Content-Type": "application/json" }),
				body: body === undefined ? undefined : JSON.stringify(body),
				signal: ctrl.signal,
			})
			const text = await res.text()
			let data: any = {}
			try {
				data = text ? JSON.parse(text) : {}
			} catch {
				data = { ok: false, error: text.slice(0, 200) }
			}
			if (!res.ok || data.ok === false) {
				throw new TkbkCloudError(String(data.error ?? data.detail ?? `HTTP ${res.status}`), res.status)
			}
			return data as T
		} finally {
			clearTimeout(timer)
		}
	}

	/** Plan, hours left this month, concurrency limit. 403 → the key has no Pro/Teams plan. */
	me(): Promise<Me> {
		return this.json<Me>("GET", "/v1/me")
	}

	async createTask(req: CreateTaskRequest): Promise<{ task: Task; uploadUrl?: string }> {
		const r = await this.json<{ ok: true; task: Task; upload_url: string | null }>("POST", "/v1/tasks", req)
		return { task: r.task, uploadUrl: r.upload_url ?? undefined }
	}

	/** source: "upload" — send the working copy as tar.gz (max 200 MB by default). */
	async uploadWorkspace(taskId: string, tarGz: Uint8Array | Blob, signal?: AbortSignal): Promise<Task> {
		const res = await this.f(`${this.base}/v1/tasks/${encodeURIComponent(taskId)}/workspace`, {
			method: "PUT",
			headers: this.headers({ "Content-Type": "application/gzip" }),
			body: tarGz as any,
			signal,
		})
		const data = (await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }))) as any
		if (!res.ok || data.ok === false) throw new TkbkCloudError(String(data.error ?? `HTTP ${res.status}`), res.status)
		return data.task as Task
	}

	async getTask(taskId: string): Promise<{ task: Task; logTail: AgentEvent[] }> {
		const r = await this.json<{ ok: true; task: Task; log_tail: AgentEvent[] }>("GET", `/v1/tasks/${encodeURIComponent(taskId)}`)
		return { task: r.task, logTail: r.log_tail ?? [] }
	}

	async listTasks(limit = 50): Promise<Task[]> {
		const r = await this.json<{ ok: true; tasks: Task[] }>("GET", `/v1/tasks?limit=${limit}`)
		return r.tasks
	}

	async cancel(taskId: string): Promise<Task> {
		const r = await this.json<{ ok: true; task: Task }>("POST", `/v1/tasks/${encodeURIComponent(taskId)}/cancel`)
		return r.task
	}

	async result(taskId: string): Promise<{ status: TaskStatus; error: string | null; minutes_billed: number; metrics: TaskMetrics; result: TaskResult | null; artifacts: Artifact[] }> {
		return this.json("GET", `/v1/tasks/${encodeURIComponent(taskId)}/result`)
	}

	/** Unified diff (git apply / git am friendly). 404 until the task has produced changes. */
	async diff(taskId: string): Promise<string> {
		const res = await this.f(`${this.base}/v1/tasks/${encodeURIComponent(taskId)}/diff`, { headers: this.headers({ Accept: "text/x-patch" }) })
		if (!res.ok) throw new TkbkCloudError(`no patch (HTTP ${res.status})`, res.status)
		return res.text()
	}

	async artifacts(taskId: string): Promise<Artifact[]> {
		const r = await this.json<{ ok: true; artifacts: Artifact[] }>("GET", `/v1/tasks/${encodeURIComponent(taskId)}/artifacts`)
		return r.artifacts
	}

	/** Raw artifact bytes (patch, archive = tar.gz of the tree, log = JSONL of events). */
	async artifact(taskId: string, name: Artifact["name"]): Promise<Uint8Array> {
		const res = await this.f(`${this.base}/v1/tasks/${encodeURIComponent(taskId)}/artifacts/${name}`, { headers: this.headers() })
		if (!res.ok) throw new TkbkCloudError(`no artifact ${name} (HTTP ${res.status})`, res.status)
		return new Uint8Array(await res.arrayBuffer())
	}

	/**
	 * Live events (SSE over fetch). Replays stored events from `afterSeq`, then streams until the
	 * task ends (`done`) or `signal` aborts. Reconnects automatically on network errors.
	 */
	async *events(taskId: string, opts: { afterSeq?: number; signal?: AbortSignal } = {}): AsyncGenerator<AgentEvent> {
		let after = opts.afterSeq ?? 0
		let attempt = 0
		for (;;) {
			if (opts.signal?.aborted) return
			let res: Response
			try {
				res = await this.f(`${this.base}/v1/tasks/${encodeURIComponent(taskId)}/events?after=${after}`, {
					headers: this.headers({ Accept: "text/event-stream", "Last-Event-ID": String(after) }),
					signal: opts.signal,
				})
			} catch (e) {
				if (opts.signal?.aborted) return
				await sleep(Math.min(30_000, 1000 * 2 ** attempt++))
				continue
			}
			if (res.status === 404) throw new TkbkCloudError("task not found", 404)
			if (!res.ok || !res.body) {
				if (res.status === 401 || res.status === 403) throw new TkbkCloudError(`HTTP ${res.status}`, res.status)
				await sleep(Math.min(30_000, 1000 * 2 ** attempt++))
				continue
			}
			attempt = 0
			let finished = false
			for await (const ev of parseSse(res.body)) {
				if (ev.seq > after) after = ev.seq
				yield ev
				if (ev.type === "done") {
					finished = true
					break
				}
			}
			if (finished) return
			// stream ended without `done`: task still running, reconnect (server closes idle streams)
			await sleep(1000)
		}
	}

	/** Convenience: wait for a terminal status, with optional progress callback. */
	async waitFor(taskId: string, onEvent?: (ev: AgentEvent) => void, signal?: AbortSignal): Promise<Task> {
		for await (const ev of this.events(taskId, { signal })) onEvent?.(ev)
		return (await this.getTask(taskId)).task
	}
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/** Minimal SSE parser: `id:` / `event:` / `data:` lines, blank line = dispatch. */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
	const reader = body.getReader()
	const decoder = new TextDecoder()
	let buf = ""
	try {
		for (;;) {
			const { value, done } = await reader.read()
			if (done) break
			buf += decoder.decode(value, { stream: true })
			let idx: number
			while ((idx = buf.indexOf("\n\n")) >= 0) {
				const block = buf.slice(0, idx)
				buf = buf.slice(idx + 2)
				const data = block
					.split("\n")
					.filter((l) => l.startsWith("data:"))
					.map((l) => l.slice(5).trimStart())
					.join("\n")
				if (!data) continue
				try {
					yield JSON.parse(data) as AgentEvent
				} catch {
					/* skip malformed frame */
				}
			}
		}
	} finally {
		reader.releaseLock()
	}
}
