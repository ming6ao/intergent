/**
 * Intergent unit lifecycle tool for pi.
 *
 * Registers a native `ig` tool that forwards to the bundled `intergent` CLI.
 * This is the tool campaign **workers** use (`declare → commit`) and that the
 * coordinator uses for `start`/`status`; the campaign orchestration itself
 * lives in `campaign.ts`.
 *
 * There is no automatic single-agent bootstrap: a session is only bound to a
 * unit when something explicitly creates one (the `campaign` tool's `spawn`,
 * or a human running `intergent start`).  `ig` resolves the unit from `ctx.cwd`,
 * so a worker launched inside its unit worktree needs no `--unit`.
 */

import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { runIg } from "./common.ts";

/** Mirrors `intergent.surface.agent_actions()`; kept in sync by a test. */
export const IG_ACTIONS = [
	"start",
	"status",
	"declare",
	"commit",
	"handoff",
	"integrate",
	"report",
] as const;

/** The `ig` tool exposes exactly the agent surface. */
export const IG_TOOL_ACTIONS = [...IG_ACTIONS] as const;

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
	pi.registerTool({
		name: "ig",
		label: "Intergent",
		description:
			"Intergent unit lifecycle. Campaign workers call `declare` before editing and " +
			"`commit` when done; the coordinator calls `start`/`status` and lands work with " +
			"`integrate`. There is no single-agent handoff: the campaign coordinator owns " +
			"verification and integration.",
		promptSnippet: "Drive the Intergent unit lifecycle (declare → commit)",
		promptGuidelines: [
			"Use `ig` action `declare` before editing any file; scope every file or symbol you touch.",
			"If `declare` returns `queued`, do not edit: report the blocker and stop.",
			"Finish with `ig` action `commit`. Never run `handoff`, `integrate`, `review`, or `git merge` yourself.",
		],
		parameters: Type.Object({
			action: StringEnum(IG_TOOL_ACTIONS),
			unit: Type.Optional(Type.String({ description: "unit (defaults to this worktree)" })),
			operation: Type.Optional(
				StringEnum(["add", "extend", "modify", "replace", "remove", "rename", "migrate"]),
			),
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
			no_unit: Type.Optional(
				Type.Boolean({ description: "start: initialise the plane without a unit for cwd" }),
			),
			main: Type.Optional(
				Type.String({ description: "start: main/integration branch (default: current)" }),
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
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };
			const { text, json } = await runIg(pi, ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});
}
