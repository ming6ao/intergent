/**
 * Intergent **campaign** extension for pi — the coordinator tool.
 *
 * One top-level coordinator session turns a design document into landed work by
 * spawning a planner, one-shot workers, and a read-only verifier, while
 * `intergent` remains the deterministic isolation/integration engine.  The DAG
 * in `.intergent/<branch-key>.dag.json` is the only schedule; there are no
 * phases in the scheduler (docs/orchestration.md).
 *
 * One tool, `campaign`, wraps the CLI's orchestration verbs:
 *
 *   start <design>   adopt current branch + no-unit plane + planner -> dag.json
 *   status           merge `intergent status --json` with live child state
 *   ready            nodes whose every dependency is done
 *   spawn <node>     create the unit, launch a one-shot worker, tee its log
 *   verify <node>    verifier on the latest prepared candidate; record a verdict
 *   integrate <node> land the verified candidate onto the feature branch
 *   report           `intergent report` plus the coordinator's narrative
 *
 * Workers are scoped to the `ig` unit tool (`intergent.ts`) and run inside
 * their unit worktree; `runSubagent` enforces the agent `tools:` allowlist, so
 * a worker never sees this `campaign` tool.
 *
 * Workers are child processes of the coordinator and are **not detached**: an
 * orchestrator crash kills them, and on resume any `running` node is reset to
 * `pending`.  Only the verifier is given the GPU broker (`tools/gpu.sh`).
 *
 * Install as part of the `intergent` pi package (`pi install ./` or
 * `pi install npm:intergent`); shared helpers live in `./common.ts`.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	branchKey,
	dagPath,
	logEvent,
	logPath,
	readJson,
	runIg,
	runSubagent,
	stateDir,
	statePath,
	writeJson,
} from "./common.ts";

export const CAMPAIGN_ACTIONS = [
	"start",
	"status",
	"ready",
	"spawn",
	"verify",
	"integrate",
	"report",
] as const;

interface CampaignNode {
	id: string;
	label?: string;
	phase?: string;
	goal?: string;
	owns?: string[];
	depends_on?: string[];
	acceptance?: string[];
	gpu?: "none" | "T1" | "T2";
}

interface Dag {
	campaign?: string;
	feature_branch?: string;
	base?: string;
	design?: string;
	concurrency?: number;
	max_attempts?: number;
	nodes?: CampaignNode[];
}

interface NodeState {
	status: "pending" | "running" | "done" | "failed" | "stopped";
	attempts?: number;
	verdict?: string;
	lastError?: string;
	unit?: string;
	branch?: string;
	worktree?: string;
}

interface CampaignState {
	campaign?: string;
	feature_branch?: string;
	base?: string;
	nodes: Record<string, NodeState>;
}

function nodeIds(dag: Dag): string[] {
	return (dag.nodes ?? []).map((n) => n.id);
}

function nodeStatus(state: CampaignState, id: string): string {
	return state.nodes?.[id]?.status ?? "pending";
}

function readyNodes(dag: Dag, state: CampaignState): string[] {
	const done = new Set(nodeIds(dag).filter((id) => nodeStatus(state, id) === "done"));
	return nodeIds(dag).filter((id) => {
		if (nodeStatus(state, id) === "done" || nodeStatus(state, id) === "running") return false;
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		return (node?.depends_on ?? []).every((dep) => done.has(dep));
	});
}

function summarise(dag: Dag, state: CampaignState): string {
	const lines = [
		`campaign: ${dag.campaign ?? "(unnamed)"}`,
		`feature:  ${dag.feature_branch ?? "(unset)"}  base: ${dag.base ?? "(unset)"}`,
		`design:   ${dag.design ?? "(unspecified)"}`,
		`nodes:    ${nodeIds(dag).length}  concurrency: ${dag.concurrency ?? "?"}`,
	];
	for (const id of nodeIds(dag)) {
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		lines.push(
			`  ${id} [${node?.phase ?? "-"}] ${nodeStatus(state, id)}` +
				(node?.label ? ` — ${node.label}` : ""),
		);
	}
	return lines.join("\n");
}

export default function campaignExtension(pi: ExtensionAPI) {
	const ig = (ctx: ExtensionContext, args: string[], signal?: AbortSignal) =>
		runIg(pi, ctx, args, signal);

	async function featureBranch(ctx: ExtensionContext): Promise<string> {
		const { json } = await ig(ctx, ["status"]);
		const branch = json?.feature_branch ?? json?.main_branch;
		if (!branch) throw new Error("campaign: no feature branch; run `campaign start` first");
		return String(branch);
	}

	/** The branch currently checked out in the coordinator's checkout. */
	async function currentBranch(ctx: ExtensionContext): Promise<string> {
		const result = await pi.exec("git", ["symbolic-ref", "--quiet", "--short", "HEAD"], {
			cwd: ctx.cwd,
		});
		const branch = result.stdout?.trim();
		if (result.code !== 0 || !branch) {
			throw new Error(
				"campaign: not on a branch (detached HEAD); check out the campaign branch first",
			);
		}
		return branch;
	}

	/**
	 * The repository default branch, mirroring `integrate.found_default_branch`:
	 * origin/HEAD, then init.defaultBranch, then an existing main/master.  The
	 * checked-out branch is deliberately not a fallback, because `start` adopts
	 * it as the feature branch.
	 */
	async function defaultBranch(ctx: ExtensionContext, feature: string): Promise<string> {
		const origin = await pi.exec(
			"git",
			["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
			{ cwd: ctx.cwd },
		);
		if (origin.code === 0 && origin.stdout?.trim()) {
			return origin.stdout.trim().replace(/^origin\//, "");
		}
		const configured = await pi.exec("git", ["config", "--get", "init.defaultBranch"], {
			cwd: ctx.cwd,
		});
		const name = configured.stdout?.trim();
		if (configured.code === 0 && name && name !== feature) return name;
		for (const candidate of ["main", "master"]) {
			if (candidate === feature) continue;
			const exists = await pi.exec(
				"git",
				["rev-parse", "--verify", "--quiet", `refs/heads/${candidate}`],
				{ cwd: ctx.cwd },
			);
			if (exists.code === 0) return candidate;
		}
		return "main";
	}

	function load(ctx: ExtensionContext, branch: string): { dag: Dag; state: CampaignState } {
		return {
			dag: readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] }),
			state: readJson<CampaignState>(statePath(ctx.cwd, branch), { nodes: {} }),
		};
	}

	function widget(ctx: ExtensionContext, dag: Dag, state: CampaignState): void {
		if (!ctx.hasUI) return;
		const lines = nodeIds(dag).map((id) => {
			const node = (dag.nodes ?? []).find((n) => n.id === id);
			const status = nodeStatus(state, id);
			const marker =
				status === "running" ? "●" : status === "done" ? "✓" : status === "failed" ? "✗" : "·";
			return `${marker} ${id} ${node?.label ?? ""} [${status}]`.trim();
		});
		ctx.ui.setWidget("campaign", lines.length ? lines : ["campaign: no plan"]);
	}

	// ------------------------------------------------------------------
	// Actions
	// ------------------------------------------------------------------

	async function startCampaign(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const design = String(params.design ?? "DESIGN.md");
		const campaign = String(params.campaign ?? path.basename(ctx.cwd));

		// `start` adopts the branch that is already checked out; it never
		// creates a feature branch.  The branch key selects the plane files.
		const branch = await currentBranch(ctx);
		const defaultBr = await defaultBranch(ctx, branch);
		const existing = fs.existsSync(dagPath(ctx.cwd, branch))
			? readJson<Dag>(dagPath(ctx.cwd, branch), {})
			: undefined;
		const base = params.base ? String(params.base) : String(existing?.base ?? branch);
		const notice =
			`campaign: using current branch '${branch}' as the feature branch ` +
			`(no branch created).`;
		if (ctx.hasUI) ctx.ui.notify(notice, "info");

		// `integrate` refuses to land on the recorded default branch, so a
		// campaign can never be delivered from there.  Fail before the plane is
		// written.
		if (branch === defaultBr) {
			return {
				content: [
					{
						type: "text" as const,
						text:
							`campaign: '${branch}' is the repository default branch. ` +
							`Create or check out a feature branch first; intergent adopts the ` +
							`current branch and never creates one.`,
					},
				],
				isError: true,
			};
		}

		// 1. Plane with no coordinator unit; the engine adopts the current branch
		// as the integration/feature branch (and re-points an existing plane).
		await ig(ctx, ["start", "--no-unit", "--main", branch], signal);

		const dagFile = dagPath(ctx.cwd, branch);
		const stateFile = statePath(ctx.cwd, branch);

		// 2a. Resume: rebuild node status from git/state.db, which always win
		// over state.json. A node left `running` by a crash is reset.
		if (existing?.nodes?.length && !params.replan) {
			const state: any = readJson(stateFile, { nodes: {} });
			const status = (await ig(ctx, ["status"], signal)).json;
			const units = new Map<string, any>((status?.units ?? []).map((u: any) => [u.name, u]));
			const candidates = status?.candidates ?? [];
			for (const node of nodeIds(existing)) {
				const entry = state.nodes[node] ?? { status: "pending", attempts: 0 };
				const unit = units.get(node);
				if (unit) {
					entry.unit = node;
					entry.branch = unit.branch;
					entry.worktree = unit.worktree;
				}
				const candidate = candidates.find((c: any) => c.unit_name === (entry.unit ?? node));
				if (candidate?.status === "landed") {
					entry.status = "done";
				} else if (entry.status === "running") {
					entry.status = "pending";
					entry.attempts = (entry.attempts ?? 0) + 1;
				}
				state.nodes[node] = entry;
			}
			state.campaign = existing.campaign ?? campaign;
			state.feature_branch = branch;
			state.base = existing.base ?? base;
			writeJson(stateFile, state);
			logEvent(ctx.cwd, branch, "campaign.resumed", {
				running_reset: nodeIds(existing).filter((id) => state.nodes[id]?.status === "pending"),
			});
			return {
				content: [{ type: "text" as const, text: `${notice}\n\n${summarise(existing, state)}` }],
				details: { dag: existing, state, resumed: true, feature_branch: branch },
			};
		}

		// 2b. Planner writes dag.json (plane state, never committed).
		const task =
			`Read the design at ${design}. Produce a machine-readable execution DAG as the file ` +
			`${dagFile}. Use ONLY the Write tool for that file. The JSON shape is: ` +
			`{"campaign","feature_branch","base","design","concurrency","max_attempts","nodes":[` +
			`{"id","label","phase","goal","owns","depends_on","acceptance","gpu"}]}. ` +
			`Rules: the DAG is the only schedule (no phases); "phase" is a display label only; ` +
			`route shared build files (BUILD, Cargo.toml, lockfiles) to an explicit aggregation ` +
			`node every touched component depends_on; each node lists its acceptance commands; ` +
			`gpu is "none","T1","T2" and only the verifier may use it. Feature branch: ${branch}. ` +
			`Base: ${base}. Design: ${design}.`;
		const planner = await runSubagent({
			agent: "planner",
			task,
			cwd: ctx.cwd,
			log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_planner.log`),
			signal,
		});
		if (planner.exitCode !== 0 || !fs.existsSync(dagFile)) {
			return {
				content: [
					{ type: "text" as const, text: `planner failed:\n${planner.stderr || planner.output}` },
				],
				isError: true,
			};
		}
		const dag = readJson<Dag>(dagFile, { nodes: [] });
		if (!dag?.nodes?.length) {
			return {
				content: [{ type: "text" as const, text: `planner did not write a DAG at ${dagFile}` }],
				isError: true,
			};
		}
		const state: any = { campaign, feature_branch: branch, base, nodes: {} };
		for (const node of dag.nodes) state.nodes[node.id] = { status: "pending", attempts: 0 };
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, params.replan ? "dag.replanned" : "dag.created", {
			nodes: nodeIds(dag),
		});
		return {
			content: [{ type: "text" as const, text: `${notice}\n\n${summarise(dag, state)}` }],
			details: { dag, state, feature_branch: branch },
		};
	}

	async function spawnNode(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const node = String(params.node ?? "");
		if (!node) throw new Error("spawn requires --node <id>");
		const { json } = await ig(ctx, ["status"]);
		const branch = String(json?.feature_branch ?? "main");
		const stateFile = statePath(ctx.cwd, branch);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state: any = readJson(stateFile, { nodes: {} });
		const spec = (dag.nodes ?? []).find((n) => n.id === node);
		if (!spec) throw new Error(`spawn: unknown node '${node}'`);

		const maxAttempts = Number(dag.max_attempts ?? 3);
		const attempts = Number(state.nodes[node]?.attempts ?? 0);
		if (attempts >= maxAttempts) {
			state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "failed" };
			writeJson(stateFile, state);
			throw new Error(`spawn: node '${node}' exceeded max_attempts=${maxAttempts}`);
		}

		// Every dependency must already be integrated (done), not merely verified.
		if (!new Set(readyNodes(dag, state)).has(node)) {
			throw new Error(`spawn: node '${node}' is not ready`);
		}

		// A re-spawn uses a fresh unit name so the previous attempt cannot
		// collide; the old unit is released and pruned first.
		const attempt = attempts + 1;
		const unitName = attempt === 1 ? node : `${node}-a${attempt}`;
		const previous = state.nodes[node]?.unit;
		if (previous) {
			try {
				await ig(ctx, ["declare", "--unit", previous, "--release"]);
				await ig(ctx, ["status", "--gc"]);
			} catch {
				/* the unit may already be gone */
			}
		}

		const created = await ig(
			ctx,
			["start", "--name", unitName, "--base", branch, "--kind", "worker"],
			signal,
		);
		const worktree = created.json?.worktree;
		if (!worktree) throw new Error(`spawn: could not create a unit for '${node}'`);
		if (path.resolve(String(worktree)) === path.resolve(ctx.cwd)) {
			throw new Error("spawn: worker worktree must differ from the coordinator checkout");
		}
		const unit = String(created.json?.unit ?? unitName);

		state.nodes[node] = {
			...(state.nodes[node] ?? {}),
			status: "running",
			unit,
			worktree,
			branch: created.json?.branch,
		};
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, "node.spawn", { node, unit, attempt });

		const previousEvidence = state.nodes[node]?.lastError
			? `\nA previous attempt failed with this verifier evidence:\n${state.nodes[node].lastError}`
			: "";
		const task =
			`You are a one-shot worker for DAG node "${node}" (${spec.label ?? ""}). ` +
			`Goal: ${spec.goal ?? ""}. You own: ${(spec.owns ?? []).join(", ")}. ` +
			`Run these acceptance commands before committing: ${(spec.acceptance ?? []).join(" ; ")}. ` +
			`You MUST use the \`ig\` tool: declare every owned scope, edit only your worktree, ` +
			`then commit. Never use the GPU and never touch another node.` +
			previousEvidence;
		const result = await runSubagent({
			agent: "worker",
			task,
			cwd: String(worktree),
			log: logPath(ctx.cwd, branch, node),
			signal,
		});
		state.nodes[node].status = result.exitCode === 0 ? "pending" : "failed";
		state.nodes[node].attempts = attempt;
		writeJson(stateFile, state);
		return {
			content: [{ type: "text" as const, text: `worker ${node} exited ${result.exitCode}` }],
			details: { node, unit, exitCode: result.exitCode, output: result.output },
			isError: result.exitCode !== 0,
		};
	}

	async function verifyNode(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const node = String(params.node ?? "");
		if (!node) throw new Error("verify requires --node <id>");
		const branch = await featureBranch(ctx);
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const stateFile = statePath(ctx.cwd, branch);
		const state: any = readJson(stateFile, { nodes: {} });
		const spec = (dag.nodes ?? []).find((n) => n.id === node);
		if (!spec) throw new Error(`verify: unknown node '${node}'`);
		const unit = String(state.nodes[node]?.unit ?? node);

		// Read-only verifier: T0 CPU, then tools/gpu.sh for GPU acceptance.
		const task =
			`Independently verify DAG node "${node}". Do not edit any file. Run the acceptance ` +
			`commands: ${(spec.acceptance ?? []).join(" ; ")}. If and only if the node's gpu tier is ` +
			`not "none" (it is "${spec.gpu ?? "none"}"), wrap GPU commands with ` +
			`\`tools/gpu.sh --tier <tier> -- <command>\`. Report a single line starting with ` +
			`"VERDICT: PASS" or "VERDICT: FAIL", then the evidence.`;
		const result = await runSubagent({
			agent: "verifier",
			task,
			cwd: ctx.cwd,
			log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_verify_${node}.log`),
			signal,
		});

		// Record the verdict in intergent (§6.4) so re-integration can reuse it.
		const acceptanceArgs: string[] = [];
		for (const cmd of spec.acceptance ?? []) acceptanceArgs.push("--acceptance", cmd);
		const recorded = await ig(
			ctx,
			["integrate", "--node", unit, "--check-only", "--gpu", spec.gpu ?? "none", ...acceptanceArgs],
			signal,
		);
		const passed =
			result.exitCode === 0 &&
			/VERDICT:\s*PASS/i.test(result.output) &&
			(recorded.json?.results ?? []).every((r: any) => r.status === "passed");
		state.nodes[node] = {
			...(state.nodes[node] ?? {}),
			status: passed ? "pending" : "failed",
			verdict: passed ? "pass" : "fail",
			...(passed ? {} : { lastError: result.output }),
		};
		writeJson(stateFile, state);
		logEvent(ctx.cwd, branch, "node.verdict", { node, passed, gpu: spec.gpu ?? "none" });
		return {
			content: [{ type: "text" as const, text: `${node}: ${passed ? "PASS" : "FAIL"}\n${result.output}` }],
			details: { node, passed, recorded: recorded.json },
			isError: !passed,
		};
	}

	async function integrateNode(
		ctx: ExtensionContext,
		params: any,
		signal?: AbortSignal,
	): Promise<any> {
		const node = String(params.node ?? "");
		const branch = await featureBranch(ctx);
		const stateFile = statePath(ctx.cwd, branch);
		const state: any = readJson(stateFile, { nodes: {} });
		const unit = node ? String(state.nodes[node]?.unit ?? node) : "";
		const args = unit
			? ["integrate", "--node", unit, "--cleanup", "none"]
			: ["integrate", "--cleanup", "none"];
		const { json: integrated, text } = await ig(ctx, args, signal);
		const results = integrated?.results ?? [];
		const failed = results.some((r: any) => r.status === "failed");
		if (results.length) {
			const landedUnits: string[] = results
				.filter((r: any) => r.status === "landed")
				.map((r: any) => r.unit);
			// A sweep (no --node) lands any straggler; mark matching nodes done.
			for (const id of Object.keys(state.nodes)) {
				if (landedUnits.includes(state.nodes[id].unit ?? id)) state.nodes[id].status = "done";
			}
			if (node) {
				state.nodes[node] = {
					...(state.nodes[node] ?? {}),
					status: failed ? "failed" : "done",
				};
			}
			writeJson(stateFile, state);
			logEvent(ctx.cwd, branch, failed ? "node.integrate_failed" : "node.integrated", {
				node: node || "(sweep)",
				unit,
				results,
			});
		}

		// Cleanup is destructive, so the agent asks before choosing it (§7).
		if (!failed && ctx.hasUI) {
			const ok = await ctx.ui.confirm(
				`Integrate succeeded onto ${branch}.`,
				"Remove the landed unit worktrees and unit branches now?",
			);
			if (ok) await ig(ctx, ["integrate", "--cleanup", "worktrees"]);
		}
		return { content: [{ type: "text" as const, text }], details: integrated ?? {}, isError: failed };
	}

	pi.registerTool({
		name: "campaign",
		label: "Intergent campaign",
		description:
			"Coordinate a design into landed work: start (adopt current branch + planner), " +
			"status, ready, spawn (one-shot worker), verify (read-only verifier), " +
			"integrate (land a verified node), report. The dag.json plan is the only schedule.",
		promptSnippet: "Drive an Intergent campaign (start → spawn → verify → integrate)",
		promptGuidelines: [
			"The DAG in dag.json is the only schedule; there are no phases in the scheduler.",
			"Spawn a node only once every dependency is integrated (done), never merely verified.",
			"Only the verifier may use the GPU (tools/gpu.sh); workers never touch it.",
			"Integrate a node immediately after its verifier passes, before spawning dependents.",
		],
		parameters: Type.Object({
			action: StringEnum(CAMPAIGN_ACTIONS),
			design: Type.Optional(Type.String({ description: "start: design document path" })),
			campaign: Type.Optional(Type.String({ description: "start: campaign name" })),
			base: Type.Optional(
				Type.String({ description: "start: base branch/ref (default: feature branch)" }),
			),
			replan: Type.Optional(Type.Boolean({ description: "start: re-run the planner" })),
			node: Type.Optional(Type.String({ description: "node id for spawn/verify/integrate" })),
			narrative: Type.Optional(Type.String({ description: "report: what-changed/risks text" })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			switch (params.action as (typeof CAMPAIGN_ACTIONS)[number]) {
				case "start":
					return startCampaign(ctx, params, signal);
				case "status": {
					const branch = await featureBranch(ctx);
					const { dag, state } = load(ctx, branch);
					widget(ctx, dag, state);
					return {
						content: [{ type: "text" as const, text: summarise(dag, state) }],
						details: { dag, state },
					};
				}
				case "ready": {
					const branch = await featureBranch(ctx);
					const { dag, state } = load(ctx, branch);
					const ready = readyNodes(dag, state);
					return {
						content: [
							{ type: "text" as const, text: ready.length ? ready.join(", ") : "(none ready)" },
						],
						details: { ready },
					};
				}
				case "spawn":
					return spawnNode(ctx, params, signal);
				case "verify":
					return verifyNode(ctx, params, signal);
				case "integrate":
					return integrateNode(ctx, params, signal);
				case "report": {
					const branch = await featureBranch(ctx);
					const { dag } = load(ctx, branch);
					const args = ["report"];
					if (params.narrative) args.push("--narrative", String(params.narrative));
					if (dag.design) args.push("--design", dag.design);
					const { json, text } = await ig(ctx, args, signal);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
				}
				default:
					throw new Error(`campaign: unknown action '${params.action}'`);
			}
		},
	});
}

export { nodeIds, nodeStatus, readyNodes, summarise };
