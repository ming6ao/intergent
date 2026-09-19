/**
 * Intergent extension for pi.
 *
 * pi has no built-in MCP, so this extension exposes the Intergent local plane
 * as native pi tools backed by the `intergent` CLI. It is cwd-native: run pi
 * inside your Intergent unit worktree and the unit is resolved automatically.
 *
 * Install:
 *   1. `pip install -e /path/to/intergent` (so `intergent` is on PATH), or set
 *      `INTERGENT_BIN=/path/to/bin/intergent`.
 *   2. Copy/symlink this file into `~/.pi/agent/extensions/` (global) or
 *      `.pi/extensions/` (project), then `/reload`.
 *
 * Landing (`approve`/`land`) is deliberately NOT exposed: it stays a human action.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const OPERATIONS = [
	"add",
	"extend",
	"modify",
	"replace",
	"remove",
	"rename",
	"migrate",
] as const;

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

export default function intergentExtension(pi: ExtensionAPI) {
	const bin = process.env.INTERGENT_BIN || "intergent";

	async function runIg(
		ctx: ExtensionContext,
		args: string[],
		signal?: AbortSignal,
	): Promise<IgResult> {
		const result = await pi.exec(bin, ["--json", ...args], {
			cwd: ctx.cwd,
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

	const current = async (ctx: ExtensionContext) => {
		const { json, text } = await runIg(ctx, ["workspace", "current"]);
		return { content: [{ type: "text" as const, text }], details: json ?? {} };
	};

	pi.registerTool({
		name: "ig_current",
		label: "Intergent: current unit",
		description: "Show the Intergent unit (worktree + branch) for the current directory.",
		promptSnippet: "Show the Intergent unit for this worktree",
		parameters: Type.Object({}),
		async execute(_id, _params, signal, _onUpdate, ctx) {
			return current(ctx);
		},
	});

	pi.registerTool({
		name: "ig_declare",
		label: "Intergent: declare intent",
		description:
			"Declare the scopes and operation this session will edit, acquiring scope leases. " +
			"Must be called before editing. Returns granted, queued, or needs_decision.",
		promptSnippet: "Declare scopes and acquire Intergent leases before editing",
		promptGuidelines: [
			"Use ig_declare before editing any file in an Intergent workspace; if it returns queued or needs_decision, do not edit and follow up with the human.",
		],
		parameters: Type.Object({
			operation: StringEnum(OPERATIONS),
			scopes: Type.Array(Type.String(), {
				description: 'Scope specs like "symbol:src/x.py#Cls.m" or "file:docs/y.md"',
			}),
			task: Type.Optional(Type.String()),
			summary: Type.Optional(Type.String()),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["declare", "--operation", params.operation];
			for (const scope of params.scopes) args.push("--scope", scope);
			if (params.task) args.push("--task", params.task);
			if (params.summary) args.push("--summary", params.summary);
			const { text, json } = await runIg(ctx, args, signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_check",
		label: "Intergent: check conflicts",
		description: "Dry-run Intergent conflict detection for scopes without taking leases.",
		parameters: Type.Object({
			operation: StringEnum(OPERATIONS),
			scopes: Type.Array(Type.String()),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["check", "--operation", params.operation];
			for (const scope of params.scopes) args.push("--scope", scope);
			const { text, json } = await runIg(ctx, args, signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_heartbeat",
		label: "Intergent: heartbeat",
		description: "Renew the current unit's scope leases.",
		parameters: Type.Object({}),
		async execute(_id, _params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["heartbeat"], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_release",
		label: "Intergent: release leases",
		description: "Release the current unit's leases early (e.g. abandoning the task).",
		parameters: Type.Object({}),
		async execute(_id, _params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["release"], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_commit",
		label: "Intergent: commit",
		description: "Commit all changes in the current unit worktree.",
		parameters: Type.Object({ message: Type.String() }),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["commit", "-m", params.message], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_finish",
		label: "Intergent: finish candidate",
		description: "Register the current branch as a ready candidate for review.",
		promptSnippet: "Register the finished work as an Intergent candidate",
		parameters: Type.Object({ summary: Type.Optional(Type.String()) }),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["finish"];
			if (params.summary) args.push("--summary", params.summary);
			const { text, json } = await runIg(ctx, args, signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_verify",
		label: "Intergent: verify",
		description: "Run trusted checks pinned to a fingerprint for a candidate.",
		parameters: Type.Object({ candidate: Type.String() }),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["verify", params.candidate], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_review",
		label: "Intergent: review packet",
		description: "Build the review packet for a candidate for the human to approve.",
		parameters: Type.Object({ candidate: Type.String() }),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["review", params.candidate], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_status",
		label: "Intergent: status",
		description: "Show Intergent units, candidates, lease queue, and wave plan.",
		parameters: Type.Object({}),
		async execute(_id, _params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["status"], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});

	pi.registerTool({
		name: "ig_simulate",
		label: "Intergent: simulate",
		description: "Plan waves over all candidates and verify the combined tree.",
		parameters: Type.Object({}),
		async execute(_id, _params, signal, _onUpdate, ctx) {
			const { text, json } = await runIg(ctx, ["simulate"], signal);
			return { content: [{ type: "text" as const, text }], details: json ?? {} };
		},
	});
}
