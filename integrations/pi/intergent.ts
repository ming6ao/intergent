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
 * The agent's last step is `handoff`: it verifies the work and stages an
 * uncommitted draft on local main.  The human owns the boundary — `/ig-approve`
 * commits the draft and cleans up, `/ig-reject` restores main.  The agent never
 * approves or lands.
 *
 * Install:
 *   1. `pip install -e /path/to/intergent` (so `intergent` is on PATH), or set
 *      `INTERGENT_BIN=/path/to/bin/intergent`.
 *   2. Copy/symlink this file into `~/.pi/agent/extensions/` (global) or
 *      `.pi/extensions/` (project), then `/reload`.
 */

import { existsSync } from "node:fs";
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
	"handoff",
] as const;

/** The `ig` tool exposes exactly the agent surface. */
export const IG_TOOL_ACTIONS = [...IG_ACTIONS] as const;

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

/**
 * True when the session is bound to a worktree that no longer exists, e.g.
 * because it landed and was cleaned up. A session that was never bound
 * (``worktree`` undefined) is *not* stale — bootstrapping stays tag-gated.
 */
export function bindingIsStale(
	worktree: string | undefined,
	exists: (path: string) => boolean,
): boolean {
	return worktree !== undefined && !exists(worktree);
}

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

/** The uncommitted handoff draft staged on the main branch. */
interface HandoffDraft {
	main_branch?: string;
	main_worktree?: string;
	open_command?: string | null;
	files?: string[];
	stat?: string;
	message?: string;
	candidates?: Array<{ id: number | string; unit?: string; summary?: string | null }>;
}

/** Render a staged handoff for the approve dialog. */
function formatDraft(draft: HandoffDraft): string {
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
		// If the worktree this session was bound to has been removed (it landed
		// and was cleaned up), drop the stale binding and bootstrap a fresh unit
		// so the CLI has a valid cwd again. We only re-bootstrap an existing
		// binding; the initial bootstrap stays tag-gated in `before_agent_start`.
		if (bindingIsStale(unitCwd, existsSync)) {
			unitCwd = undefined;
			bootstrapped = false;
			await bootstrap(ctx);
		}
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
				"(scope every file/symbol), then `commit`, then `handoff`. `handoff` " +
				"verifies your work, trial-merges it, and stages an uncommitted draft on " +
				"local main. After that you are done: report the draft (including its " +
				"`open_command`) and stop. A human approves or rejects the handoff with " +
				"`/ig-approve` / `/ig-reject`; never run `git merge`, never approve or land " +
				"yourself. If `declare` returns `queued` or `needs_decision`, stop and ask " +
				"the human." +
				(task ? `\n\nTask: ${task}` : ""),
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
			"Intergent unit lifecycle: start, status, declare, commit, handoff. " +
			"Call `declare` before editing, then `commit`, then `handoff` to verify " +
			"your work and stage an uncommitted draft on local main for human approval.",
		promptSnippet: "Drive the Intergent unit lifecycle (declare → commit → handoff)",
		promptGuidelines: [
			"Use `ig` with action `declare` before editing any file in an Intergent workspace; scope every file or symbol you touch.",
			"If `declare` returns `queued` or `needs_decision`, do not edit: stop and ask the human.",
			"Finish with `ig` action `handoff`: it verifies your work and stages an uncommitted draft on local main. Then report the draft's `open_command` and stop.",
			"Never approve or land a handoff yourself; the human runs `/ig-approve` or `/ig-reject`.",
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
			message: Type.Optional(Type.String({ description: "commit message (action=commit)" })),
			reason: Type.Optional(Type.String()),
			dry_run: Type.Optional(Type.Boolean({ description: "declare: conflict check only" })),
			renew: Type.Optional(Type.Boolean({ description: "declare: renew leases" })),
			release: Type.Optional(Type.Boolean({ description: "declare: release leases" })),
			decide: Type.Optional(StringEnum(["wait", "override", "redesign"])),
			sync: Type.Optional(Type.Boolean({ description: "commit: rebase onto base first" })),
			simulate: Type.Optional(Type.Boolean({ description: "status: plan waves" })),
			health: Type.Optional(Type.Boolean({ description: "status: check plane health" })),
			gc: Type.Optional(Type.Boolean({ description: "status: prune landed worktrees" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			no_checks: Type.Optional(Type.Boolean({ description: "handoff: skip verification" })),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };
			const { text, json } = await runIg(ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	/** The uncommitted draft staged on main, if any. */
	async function pendingDraft(
		ctx: ExtensionContext,
		signal?: AbortSignal,
	): Promise<HandoffDraft | undefined> {
		const { json } = await runIg(ctx, ["review"], signal);
		return (json as { draft?: HandoffDraft | null } | undefined)?.draft ?? undefined;
	}

	// Human-only commands: the human owns the handoff boundary. The extension
	// confirms before committing (approve) or discarding (reject).
	pi.registerCommand("ig-approve", {
		description: "Intergent: approve the pending handoff (commit + clean up)",
		handler: async (_args, ctx) => {
			const draft = await pendingDraft(ctx);
			if (!draft) {
				ctx.ui.notify("No handoff is pending. Ask the agent to run `ig handoff`.", "warning");
				return;
			}
			if (!ctx.hasUI) throw new Error("approve needs a human confirmation");
			const ok = await ctx.ui.confirm("Intergent: approve this handoff?", formatDraft(draft));
			if (!ok) return;
			const { text } = await runIg(ctx, ["review", "--approve"]);
			ctx.ui.notify(text, "info");
		},
	});

	pi.registerCommand("ig-reject", {
		description: "Intergent: reject the pending handoff (restore main)",
		handler: async (_args, ctx) => {
			if (!ctx.hasUI) throw new Error("reject needs a human confirmation");
			const ok = await ctx.ui.confirm(
				"Intergent: reject the pending handoff?",
				"Discard the staged draft and restore main?",
			);
			if (!ok) return;
			const { text } = await runIg(ctx, ["review", "--reject"]);
			ctx.ui.notify(text, "info");
		},
	});
}
