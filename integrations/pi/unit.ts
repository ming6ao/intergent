/**
 * Sliceme unit lifecycle tool for pi.
 *
 * Registers a native `sliceme-unit` tool that forwards to the bundled `sliceme`
 * CLI.  This is the unit lifecycle tool that campaign **workers** use
 * (`status → commit`). Workers are scoped to it by their agent `tools:`
 * allowlist, so they never see the `sliceme` coordinator tool
 * (`coordinator.ts`).  The tool shells out to the bundled CLI, so no
 * `sliceme` install on `PATH` is needed.
 *
 * Ownership is decided at plan time: a worker edits only the directories its
 * DAG node owns, and `commit` refuses paths outside them (plan conformance).
 * There is no runtime declare/lease step.
 *
 * There is no automatic single-agent bootstrap: a session is only bound to a
 * unit when something explicitly creates one (the `sliceme` coordinator tool's
 * `spawn`, or a human running `sliceme start`).  The tool resolves the unit from
 * `ctx.cwd`, so a worker launched inside its unit worktree needs no `--unit`.
 *
 * The tool is registered `defaultActive: false`, so a plain session never sees
 * it.  The `sliceme` skill turns it on for the session; workers get it back
 * through their `tools:` allowlist (`pi --tools sliceme-unit`).
 */

import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { runSliceme } from "./common.ts";

/** Mirrors `sliceme.surface.ACTIONS`; kept in sync by a test. */
export const SLICEME_ACTIONS = [
	"start",
	"status",
	"commit",
	"integrate",
	"report",
	"exec",
] as const;

/** The `sliceme` tool exposes exactly the agent surface. */
export const SLICEME_TOOL_ACTIONS = [...SLICEME_ACTIONS] as const;

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

export default function unitExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "sliceme-unit",
		label: "Sliceme unit",
		description:
			"Sliceme unit lifecycle for campaign workers: edit only the directories your " +
			"node owns, then `commit`. The coordinator owns the campaign and lands work with " +
			"`integrate`; there is no single-agent handoff and no declare step.",
		promptSnippet: "Drive the Sliceme unit lifecycle (edit owned dirs → commit)",
		promptGuidelines: [
			"Edit only files inside the directories your DAG node owns; `commit` rejects paths outside them.",
			"Use `sliceme-unit` action `status` with `short: true` to confirm you are inside your unit worktree.",
			"Finish with `sliceme-unit` action `commit`. Never run `integrate` or `git merge` yourself.",
		],
		// Inert until the `sliceme` skill activates it: a plain session must not
		// advertise (or inject guidelines for) the unit lifecycle. Workers get it
		// back through their `tools:` allowlist (`pi --tools sliceme-unit`).
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(SLICEME_TOOL_ACTIONS),
			unit: Type.Optional(Type.String({ description: "unit (defaults to this worktree)" })),
			task: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
			message: Type.Optional(Type.String({ description: "commit message (action=commit)" })),
			no_unit: Type.Optional(
				Type.Boolean({ description: "start: initialise the plane without a unit for cwd" }),
			),
			main: Type.Optional(
				Type.String({
					description: "start: integration branch to adopt (default: current; must exist)",
				}),
			),
			base: Type.Optional(Type.String({ description: "start: base branch/ref" })),
			node: Type.Optional(
				Type.String({ description: "integrate: only the candidate for this node/unit id" }),
			),
			cleanup: Type.Optional(
				StringEnum(["none", "worktrees", "all"] as const, {
					description: "integrate: post-merge cleanup (default none)",
				}),
			),
			acceptance: Type.Optional(
				Type.Array(Type.String(), { description: "integrate: node acceptance command" }),
			),
			gpu: Type.Optional(
				StringEnum(["none", "T1", "T2"] as const, { description: "integrate: verifier GPU tier" }),
			),
			check_only: Type.Optional(
				Type.Boolean({ description: "integrate: record the verdict without merging" }),
			),
			narrative: Type.Optional(
				Type.String({ description: "report: what-changed/risks narrative" }),
			),
			design: Type.Optional(Type.String({ description: "report: design document reference" })),
			simulate: Type.Optional(Type.Boolean({ description: "status: plan waves" })),
			health: Type.Optional(Type.Boolean({ description: "status: check plane health" })),
			gc: Type.Optional(Type.Boolean({ description: "status: prune landed worktrees" })),
			short: Type.Optional(Type.Boolean({ description: "status: print only the unit name" })),
			no_checks: Type.Optional(Type.Boolean({ description: "skip verification" })),
			submit: Type.Optional(Type.Boolean({ description: "exec: enqueue a check job" })),
			validate: Type.Optional(
				Type.Boolean({ description: "exec: resolve and validate the sandbox gate" }),
			),
			gpu_required: Type.Optional(
				Type.Boolean({ description: "exec: with validate, require a GPU runner" }),
			),
			run: Type.Optional(Type.Boolean({ description: "exec: drain the queue" })),
			open: Type.Optional(
				Type.Boolean({ description: "exec: create the single worktree for --wave" }),
			),
			record: Type.Optional(
				Type.Boolean({ description: "exec: record a wave (conformance + per-node commits)" }),
			),
			wait: Type.Optional(Type.Boolean({ description: "exec: wait for a job" })),
			cancel: Type.Optional(Type.Boolean({ description: "exec: cancel a queued job" })),
			job: Type.Optional(Type.String({ description: "exec: job id" })),
			source: Type.Optional(
				Type.String({ description: "exec: fingerprint source, e.g. node:w1 or wave:0" }),
			),
			commit: Type.Optional(Type.String({ description: "exec: commit/ref to run checks at" })),
			command: Type.Optional(
				Type.Array(Type.String(), { description: "exec: check command (repeatable)" }),
			),
			sandbox: Type.Optional(
				StringEnum(["none", "bwrap", "unshare"] as const, {
					description: "exec: sandbox mode",
				}),
			),
			priority: Type.Optional(Type.Number({ description: "exec: higher runs first" })),
			timeout: Type.Optional(Type.Number({ description: "exec: per-command timeout seconds" })),
			wave: Type.Optional(Type.Number({ description: "exec: campaign wave" })),
			requester: Type.Optional(Type.String({ description: "exec: verifier id" })),
			limit: Type.Optional(Type.Number({ description: "exec: max jobs to drain" })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };
			const { text, json } = await runSliceme(pi, ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});
}
