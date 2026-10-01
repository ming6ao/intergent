/**
 * Intergent unit lifecycle tool for pi.
 *
 * Registers a native `ig` tool that forwards to the bundled `intergent` CLI.
 * This is the unit lifecycle tool that campaign **workers** use
 * (`status → commit`). Workers are scoped to it by their agent `tools:`
 * allowlist, so they never see `campaign.ts`; the coordinator drives the
 * campaign with the `campaign` tool instead.  The tool shells out to the
 * bundled CLI, so no `intergent` install on `PATH` is needed.
 *
 * Ownership is decided at plan time: a worker edits only the directories its
 * DAG node owns, and `commit` refuses paths outside them (plan conformance).
 * There is no runtime declare/lease step.
 *
 * There is no automatic single-agent bootstrap: a session is only bound to a
 * unit when something explicitly creates one (the `campaign` tool's `spawn`,
 * or a human running `intergent start`).  `ig` resolves the unit from `ctx.cwd`,
 * so a worker launched inside its unit worktree needs no `--unit`.
 *
 * The tool is registered `defaultActive: false`, so a plain session never sees
 * it.  The `intergent` skill turns it on for the session; workers get it back
 * through their `tools:` allowlist (`pi --tools ig`).
 */

import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { runIg } from "./common.ts";

/** Mirrors `intergent.surface.ACTIONS`; kept in sync by a test. */
export const IG_ACTIONS = [
	"start",
	"status",
	"commit",
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
			"Intergent unit lifecycle for campaign workers: edit only the directories your " +
			"node owns, then `commit`. The coordinator owns the campaign and lands work with " +
			"`integrate`; there is no single-agent handoff and no declare step.",
		promptSnippet: "Drive the Intergent unit lifecycle (edit owned dirs → commit)",
		promptGuidelines: [
			"Edit only files inside the directories your DAG node owns; `commit` rejects paths outside them.",
			"Use `ig` action `status` with `short: true` to confirm you are inside your unit worktree.",
			"Finish with `ig` action `commit`. Never run `integrate` or `git merge` yourself.",
		],
		// Inert until the `intergent` skill activates it: a plain session must not
		// advertise (or inject guidelines for) the unit lifecycle. Workers get it
		// back through their `tools:` allowlist (`pi --tools ig`).
		defaultActive: false,
		parameters: Type.Object({
			action: StringEnum(IG_TOOL_ACTIONS),
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
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const { action, ...rest } = params as Record<string, unknown> & { action: string };
			const { text, json } = await runIg(pi, ctx, toArgs(action, rest), signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});
}
