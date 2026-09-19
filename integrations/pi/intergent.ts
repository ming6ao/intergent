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
				"the work is done, call `ig` with action `submit`: it asks the human to " +
				"approve and, if they confirm, lands the change on local main. Never run " +
				"`git merge` yourself. If `declare` returns `queued` or " +
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
			"and finally `submit` (which asks the human to approve and lands on main).",
		promptSnippet: "Drive the Intergent unit lifecycle (declare → commit → verify → submit)",
		promptGuidelines: [
			"Use `ig` with action `declare` before editing any file in an Intergent workspace; scope every file or symbol you touch.",
			"If `declare` returns `queued` or `needs_decision`, do not edit: stop and ask the human.",
			"Finish with action `submit`; it asks the human for approval and lands only if they confirm.",
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
			cleanup: Type.Optional(Type.Boolean({ description: "submit: remove the landed worktree" })),
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

	/**
	 * Human-gated approval + landing. Shows the review packet, asks the human to
	 * confirm, and only then runs `intergent submit` (approve + land).
	 */
	async function submit(
		ctx: ExtensionContext,
		params: Record<string, unknown>,
		signal?: AbortSignal,
	) {
		const candidate = params.candidate ? String(params.candidate) : undefined;
		let summary = "Submit the current candidate?";
		if (candidate) {
			try {
				const packet = await runIg(ctx, ["review", candidate], signal);
				summary = "Approve and land this candidate?\n\n" + packet.text.slice(0, 2000);
			} catch {
				/* fall through to a generic confirmation */
			}
		}
		if (!ctx.hasUI) {
			throw new Error(
				"submit needs a human confirmation and this session has no UI; " +
					"run `intergent review --approve <candidate>` then `intergent review --land <candidate>` in a terminal.",
			);
		}
		const ok = await ctx.ui.confirm("Intergent: approve and land?", summary);
		if (!ok) {
			return {
				content: [{ type: "text" as const, text: "Landing declined by the human." }],
				details: { approved: false },
			};
		}
		const args = ["submit"];
		if (candidate) args.push(candidate);
		if (params.reason) args.push("--reason", String(params.reason));
		if (params.no_checks) args.push("--no-checks");
		if (params.cleanup) args.push("--cleanup");
		const { text, json } = await runIg(ctx, args, signal);
		return { content: [{ type: "text" as const, text }], details: json ?? {} };
	}
}
