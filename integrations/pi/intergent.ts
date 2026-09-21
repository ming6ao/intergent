/**
 * Intergent extension for pi.
 *
 * Exposes the Intergent local plane as **one** native pi tool, `ig`,
 * parameterized by an `action` enum — the same action surface as the CLI and
 * MCP server (see `intergent/surface.py`).  The tool is a thin forwarder: it
 * translates `{ action, ...flags }` into `intergent <action> ...`.
 *
 * It is cwd-native: once a session is bound to an Intergent unit worktree, all
 * built-in coding tools are rebound to that worktree and every `ig` call runs
 * there.
 *
 * Bootstrapping is tag-gated: a session only creates a unit when the user's
 * prompt mentions "intergent" (or when INTERGENT_AUTO_BOOTSTRAP=1).  Set
 * INTERGENT_AUTO_BOOTSTRAP=0 to disable entirely.
 *
 * Landing is human-gated: `ig` exposes `submit`, which first shows the review
 * packet and requires an interactive confirmation before it approves and lands.
 * It is a no-op without a UI.
 *
 * Install:
 *   1. `pip install -e /path/to/intergent` (so `intergent` is on PATH), or set
 *      `INTERGENT_BIN=/path/to/bin/intergent`.
 *   2. Copy/symlink this file into `~/.pi/agent/extensions/` (global) or
 *      `.pi/extensions/` (project), then `/reload`.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createCodingTools } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

/** Mirrors `intergent.surface.agent_actions()`; kept in sync by a test. */
export const IG_ACTIONS = [
	"start",
	"status",
	"declare",
	"commit",
	"verify",
	"review",
] as const;

/** Tool actions = the agent surface plus the human-gated `submit`. */
export const IG_TOOL_ACTIONS = [...IG_ACTIONS, "submit"] as const;

interface IgResult {
	text: string;
	json: unknown;
}

function parseJson(text: string): unknown {
	try {
		return JSON.parse(text);
	} catch {
		return undefined;
	}
}

const TAG = /\bintergent\b|\big\b/i;
const ACTION_RE = /^(intergent|ig)\b[:,]?\s*/i;

function toArgs(action: string, params: Record<string, unknown>): string[] {
	const args = [action];
	for (const [key, value] of Object.entries(params)) {
		if (key === "action" || value === undefined || value === null) continue;
		const flag = `--${key.replace(/_/g, "-")}`;
		if (typeof value === "boolean") {
			if (value) args.push(flag);
		} else if (Array.isArray(value)) {
			for (const item of value) args.push(flag, String(item));
		} else {
			args.push(flag, String(value));
		}
	}
	return args;
}

/** Subset of the `intergent review` packet the confirmation dialog needs. */
interface ReviewPacket {
	candidate?: { id?: number | string; status?: string; branch?: string; summary?: string | null };
	unit?: { name?: string; worktree?: string } | null;
	worktree?: string | null;
	open_command?: string | null;
	commits?: string[];
	files?: string[];
	risk_flags?: string[];
	verification?: { status?: string; fingerprint?: string } | null;
}

/** A staged (uncommitted) landing draft on the main branch. */
interface LandingDraft {
	main_branch?: string;
	main_worktree?: string;
	open_command?: string | null;
	files?: string[];
	stat?: string;
	message?: string;
	candidates?: Array<{ id: number | string; unit?: string; summary?: string | null }>;
}

/** Render a staged landing draft for the approve/commit dialog. */
function formatDraft(draft: LandingDraft): string {
	const units = (draft.candidates ?? []).map((c) => c.unit ?? String(c.id)).join(", ");
	const files = draft.files ?? [];
	const lines: Array<string | undefined> = [
		`Draft staged on ${draft.main_branch ?? "main"} — not committed yet`,
		`Units: ${units || "(unknown)"}`,
		draft.message ? `Commit subject: ${draft.message.split("\n")[0]}` : undefined,
		`Files (${files.length}): ${files.slice(0, 12).join(", ")}${files.length > 12 ? ", …" : ""}`,
		draft.stat,
		draft.open_command ? `Open main in VS Code: ${draft.open_command}` : undefined,
	];
	return lines.filter((line): line is string => Boolean(line)).join("\n");
}

/** Render a review packet as the short human summary shown before approval. */
function formatPacket(packet: ReviewPacket): string {
	const c: NonNullable<ReviewPacket["candidate"]> = packet.candidate ?? {};
	const lines: Array<string | undefined> = [
		`Candidate ${c.id ?? "?"} [${c.status ?? "?"}]${packet.unit?.name ? ` — unit ${packet.unit.name}` : ""}`,
		c.summary ? `Summary: ${c.summary}` : undefined,
		c.branch ? `Branch: ${c.branch}` : undefined,
		`Commits: ${packet.commits?.length ?? 0} · Files: ${packet.files?.length ?? 0}`,
		`Verification: ${packet.verification?.status ?? "none"}`,
		packet.risk_flags?.length ? `Risk: ${packet.risk_flags.join(", ")}` : undefined,
		packet.worktree ? `Worktree: ${packet.worktree}` : undefined,
		packet.open_command ? `Open: ${packet.open_command}` : undefined,
	];
	return lines.filter((line): line is string => Boolean(line)).join("\n");
}

export default function intergentExtension(pi: ExtensionAPI) {
	const bin = process.env.INTERGENT_BIN || "intergent";
	const bootstrapMode = process.env.INTERGENT_AUTO_BOOTSTRAP ?? "lazy";

	// The unit worktree this session is bound to. pi cannot change a running
	// session's cwd, so once bootstrap runs we (a) point every Intergent call at
	// the worktree and (b) rebind the built-in coding tools to it.
	let unitCwd: string | undefined;
	let bootstrapped = false;

	async function runIg(
		ctx: ExtensionContext,
		args: string[],
		signal?: AbortSignal,
	): Promise<IgResult> {
		const result = await pi.exec(bin, ["--json", ...args], {
			cwd: unitCwd ?? ctx.cwd,
			signal,
			timeout: 120_000,
		});
		const text = [result.stdout, result.stderr].filter((s) => s && s.trim()).join("\n").trim();
		if (result.code !== 0) {
			throw new Error(
				text ||
					`intergent exited with code ${result.code}. Is the CLI installed and is this a unit worktree?`,
			);
		}
		return { text: text || "ok", json: parseJson(result.stdout) };
	}

	/**
	 * Idempotently bootstrap the plane + a unit for the directory pi started in,
	 * then bind this session to the resulting worktree. Safe to call more than
	 * once: `intergent start` is a no-op once the directory is inside a unit.
	 */
	async function bootstrap(ctx: ExtensionContext, cwd?: string): Promise<void> {
		bootstrapped = true;
		if (bootstrapMode === "0") return;
		const result = await pi.exec(bin, ["--json", "start", "--agent", "pi"], {
			cwd: cwd ?? ctx.cwd,
			timeout: 120_000,
		});
		// Not a git repo, git missing, etc. — stay out of the way.
		if (result.code !== 0) return;
		const info = parseJson(result.stdout) as
			| { worktree?: string; unit?: string; initialized?: boolean; created?: boolean }
			| undefined;
		if (!info?.worktree) return;

		const changed = info.worktree !== unitCwd;
		unitCwd = info.worktree;
		if (info.worktree !== ctx.cwd) {
			// Route file edits and shell commands into the unit worktree.
			for (const tool of createCodingTools(info.worktree)) pi.registerTool(tool);
		}
		if (changed && ctx.hasUI && (info.initialized || info.created)) {
			ctx.ui.notify(`Intergent unit "${info.unit}" ready at ${info.worktree}`, "info");
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		if (bootstrapMode === "1") await bootstrap(ctx);
	});

	// Tag-gated bootstrap: the first prompt that mentions Intergent binds the
	// session to a unit worktree and injects the required lifecycle.
	pi.on("before_agent_start", async (event, ctx) => {
		if (bootstrapMode === "0" || bootstrapped) return;
		if (!TAG.test(event.prompt)) return;
		const task = event.prompt.replace(ACTION_RE, "").trim();
		await bootstrap(ctx);
		if (!unitCwd) return;
		return {
			systemPrompt:
				event.systemPrompt +
				"\n\n## Intergent task\n" +
				"This session is bound to an Intergent unit worktree. Work only in the " +
				"worktree; use the `ig` tool for the lifecycle: `declare` before editing " +
				"(scope every file/symbol), then `commit`, `verify`, and `review`. When " +
				"the work is done, call `ig` with action `submit`: it stages the change as an " +
				"uncommitted draft on local main and asks the human to review and approve the " +
				"commit. Never run `git merge` yourself. When the human explicitly asks you " +
				"to approve or land, call `ig` action `submit`; only the human's confirmation " +
				"commits the draft. When you report a candidate for review, " +
				"include the `open_command` from the review packet so the human can open " +
				"the unit worktree in VS Code. If `declare` returns `queued` or " +
				"`needs_decision`, stop and ask the human.",
		};
	});

	// A replaced session gets a fresh extension instance, but keep module state
	// honest anyway so a rebind never leaks across cwds.
	pi.on("session_shutdown", async () => {
		unitCwd = undefined;
		bootstrapped = false;
	});

	pi.registerTool({
		name: "ig",
		label: "Intergent",
		description:
			"Intergent unit lifecycle: start, status, declare, commit, verify, review, " +
			"submit. Call `declare` before editing, then `commit`, `verify`, `review`, " +
			"and finally `submit` (which drafts the wave onto local main and asks the human to approve the commit).",
		promptSnippet: "Drive the Intergent unit lifecycle (declare → commit → verify → submit)",
		promptGuidelines: [
			"Use `ig` with action `declare` before editing any file in an Intergent workspace; scope every file or symbol you touch.",
			"If `declare` returns `queued` or `needs_decision`, do not edit: stop and ask the human.",
			"When reporting a candidate for review or approval, include the review packet's `open_command` (e.g. `code -n <worktree>`) so the human can open the unit worktree in VS Code.",
			"When the human explicitly asks to approve or land, call `ig` with action `submit` (and the candidate id); never claim a merge happened unless the tool returns success.",
			"Finish with action `submit`; it drafts the change on local main and asks the human to approve before committing.",
		],
		parameters: Type.Object({
			action: StringEnum(IG_TOOL_ACTIONS),
			unit: Type.Optional(Type.String({ description: "unit (defaults to this worktree)" })),
			operation: Type.Optional(StringEnum(["add", "extend", "modify", "replace", "remove", "rename", "migrate"])),
			scope: Type.Optional(
				Type.Array(Type.String(), { description: 'scope specs like "file:docs/y.md"' }),
			),
			task: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
			candidate: Type.Optional(Type.String({ description: "candidate id or unit name" })),
			message: Type.Optional(Type.String({ description: "commit message (action=commit)" })),
			reason: Type.Optional(Type.String()),
			dry_run: Type.Optional(Type.Boolean({ description: "declare: conflict check only" })),
			renew: Type.Optional(Type.Boolean({ description: "declare: renew leases" })),
			release: Type.Optional(Type.Boolean({ description: "declare: release leases" })),
			decide: Type.Optional(StringEnum(["wait", "override", "redesign"])),
			sync: Type.Optional(Type.Boolean({ description: "commit: rebase onto base first" })),
			force: Type.Optional(Type.Boolean()),
			simulate: Type.Optional(Type.Boolean({ description: "status: plan waves" })),
			health: Type.Optional(Type.Boolean({ description: "status: check plane health" })),
			gc: Type.Optional(Type.Boolean({ description: "status: prune landed worktrees" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			no_checks: Type.Optional(Type.Boolean()),
			keep: Type.Optional(Type.Boolean({ description: "submit: keep the landed worktree (default: remove it)" })),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };

			if (action === "submit") {
				return submit(ctx, rest, signal);
			}
			const { text, json } = await runIg(ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	/** Candidates ordered so this session's own worktree wins, then ready ones. */
	async function resolveCandidate(
		ctx: ExtensionContext,
		candidate: string | undefined,
		signal?: AbortSignal,
	): Promise<string | undefined> {
		if (candidate) return candidate;
		const { json } = await runIg(ctx, ["status"], signal);
		const candidates =
			(
				json as {
					candidates?: Array<{
						id: number | string;
						status?: string;
						worktree?: string;
						unit_name?: string;
						summary?: string | null;
					}>;
				}
			)?.candidates ?? [];
		if (candidates.length === 0) return undefined;
		if (candidates.length === 1) return String(candidates[0]!.id);
		const here = unitCwd ?? ctx.cwd;
		const mine = candidates.filter((c) => c.worktree && here.startsWith(c.worktree));
		if (mine.length === 1) return String(mine[0]!.id);
		const ready = candidates.filter((c) => c.status === "verified" || c.status === "approved");
		if (ready.length === 1) return String(ready[0]!.id);
		if (!ctx.hasUI) return undefined;
		const labels = candidates.map(
			(c) => `${c.id}: ${c.summary ?? c.unit_name ?? "(candidate)"}`,
		);
		const choice = await ctx.ui.select("Which candidate?", labels);
		return choice ? choice.split(":", 1)[0]!.trim() : undefined;
	}

	/**
	 * Human-gated landing. Approves the candidate, stages the combined draft on
	 * main (uncommitted), asks the human to review it, and only commits on
	 * confirmation. The dialog — not the agent's tool call — is the approval.
	 */
	async function submit(
		ctx: ExtensionContext,
		params: Record<string, unknown>,
		signal?: AbortSignal,
	) {
		let candidate = params.candidate ? String(params.candidate) : undefined;
		try {
			candidate = await resolveCandidate(ctx, candidate, signal);
		} catch {
			/* status unavailable; keep whatever the caller passed */
		}
		if (!candidate) {
			throw new Error(
				"No candidate to submit. Run `ig` with action `commit` then `verify` first, " +
					"or pass the candidate id.",
			);
		}
		if (!ctx.hasUI) {
			throw new Error(
				"submit needs a human confirmation and this session has no UI; run " +
					"`intergent submit <candidate> --draft` to stage the draft on main, then " +
					"`intergent review --land --commit` to land it (or `--abort` to discard).",
			);
		}
		// Approve + stage the draft on main. Nothing is committed yet.
		const draftArgs = ["submit", candidate, "--draft"];
		if (params.reason) draftArgs.push("--reason", String(params.reason));
		if (params.no_checks) draftArgs.push("--no-checks");
		const drafted = await runIg(ctx, draftArgs, signal);
		const landed = (drafted.json as { landed?: Array<{ draft?: LandingDraft | null }> } | undefined)
			?.landed;
		const draft = landed?.find((entry) => entry.draft)?.draft;
		if (!draft) {
			// Nothing to stage (e.g. already contained in main).
			return { content: [{ type: "text" as const, text: drafted.text }], details: drafted.json ?? {} };
		}
		const ok = await ctx.ui.confirm("Intergent: approve and commit this draft?", formatDraft(draft));
		if (!ok) {
			const aborted = await runIg(ctx, ["review", "--land", "--abort"], signal);
			return {
				content: [
					{ type: "text" as const, text: "Draft discarded by the human.\n" + aborted.text },
				],
				details: { approved: false, draft },
			};
		}
		const commitArgs = ["review", "--land", "--commit"];
		if (params.keep) commitArgs.push("--keep");
		const { text, json } = await runIg(ctx, commitArgs, signal);
		return { content: [{ type: "text" as const, text }], details: json ?? {} };
	}

	function resultText(result: { content: Array<{ type: "text"; text: string }> }): string {
		return result.content[0]?.text ?? "done";
	}

	// Human-only commands: the human can trigger approval directly, without
	// asking the model, and the extension still confirms before any merge.
	pi.registerCommand("ig-approve", {
		description: "Intergent: review a candidate, then approve and land it",
		handler: async (args, ctx) => {
			const result = await submit(ctx, { candidate: args.trim() || undefined });
			ctx.ui.notify(resultText(result), "info");
		},
	});

	pi.registerCommand("ig-reject", {
		description: "Intergent: reject a candidate",
		handler: async (args, ctx) => {
			let candidate = args.trim() || undefined;
			try {
				candidate = await resolveCandidate(ctx, candidate);
			} catch {
				/* the undefined check below handles failures */
			}
			if (!candidate) {
				ctx.ui.notify("No candidate to reject.", "warning");
				return;
			}
			if (!ctx.hasUI) {
				throw new Error("reject needs a human confirmation, and this session has no UI");
			}
			const packet = await runIg(ctx, ["review", candidate]);
			const info = packet.json as ReviewPacket | undefined;
			const ok = await ctx.ui.confirm(
				"Intergent: reject?",
				info ? formatPacket(info) : `Reject candidate ${candidate}?`,
			);
			if (!ok) return;
			const { text } = await runIg(ctx, ["review", candidate, "--reject"]);
			ctx.ui.notify(text, "info");
		},
	});

	pi.registerCommand("ig-land", {
		description: "Intergent: land all approved candidates on local main",
		handler: async (_args, ctx) => {
			if (!ctx.hasUI) {
				throw new Error("land needs a human confirmation, and this session has no UI");
			}
			const ok = await ctx.ui.confirm(
				"Intergent: land approved candidates?",
				"Merge every approved candidate into local main in wave order?",
			);
			if (!ok) return;
			const { text } = await runIg(ctx, ["review", "--land", "--all"]);
			ctx.ui.notify(text, "info");
		},
	});
}
