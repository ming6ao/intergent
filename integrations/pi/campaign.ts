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
 * Both Intergent tools are registered `defaultActive: false`: a plain session
 * never advertises them.  The `intergent` skill activates them for the session
 * only when invoked as `/skill:intergent <design.md>` with an existing design
 * document.
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
	wave?: number;
}

interface WaveState {
	index: number;
	members: string[];
	status: "pending" | "running" | "done";
	integrated: string[];
	cleanup_done: boolean;
}

interface CampaignState {
	campaign?: string;
	feature_branch?: string;
	base?: string;
	wave_size?: number;
	current_wave?: number;
	dag_fingerprint?: string;
	waves?: WaveState[];
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

/** Structural identity of the DAG inputs that determine wave packing. */
function dagFingerprint(dag: Dag): string {
	return JSON.stringify(
		(dag.nodes ?? []).map((n) => ({
			id: n.id,
			owns: n.owns ?? [],
			depends_on: n.depends_on ?? [],
		})),
	);
}

function firstNonDoneWave(state: CampaignState): number {
	const next = (state.waves ?? []).find((w) => w.status !== "done");
	return next ? next.index : (state.waves ?? []).length;
}

function currentWave(state: CampaignState): WaveState | undefined {
	const index = state.current_wave ?? firstNonDoneWave(state);
	return (state.waves ?? []).find((w) => w.index === index);
}

/** Nodes in the current wave whose dependencies are all integrated. */
function readyWaveNodes(dag: Dag, state: CampaignState): string[] {
	const wave = currentWave(state);
	if (!wave) return [];
	const ready = new Set(readyNodes(dag, state));
	return wave.members.filter((id) => ready.has(id));
}

/** Rebuild wave state from the engine's projection, preserving cleanup flags. */
function reconcileWaves(state: CampaignState, dagWaves: any[]): void {
	// Key prior cleanup flags by membership, not index, so adding a depends_on
	// edge (which can reindex waves) never skips or repeats a wave's cleanup.
	const prior = new Map<string, WaveState>(
		(state.waves ?? []).map((w) => [[...w.members].sort().join("|"), w]),
	);
	state.waves = (dagWaves ?? []).map((dw: any) => {
		const members: string[] = (dw.members ?? []).map((m: any) => String(m));
		const prev = prior.get([...members].sort().join("|"));
		const integrated = members.filter((id) => state.nodes[id]?.status === "done");
		const running = members.some((id) => state.nodes[id]?.status === "running");
		const status: WaveState["status"] =
			members.length > 0 && integrated.length === members.length
				? "done"
				: running
					? "running"
					: "pending";
		return {
			index: Number(dw.wave),
			members,
			status,
			integrated,
			cleanup_done: prev?.cleanup_done ?? false,
		};
	});
	const byNode = new Map<string, number>();
	for (const wave of state.waves) {
		for (const id of wave.members) byNode.set(id, wave.index);
	}
	for (const id of Object.keys(state.nodes)) {
		if (byNode.has(id)) state.nodes[id].wave = byNode.get(id);
	}
	state.current_wave = firstNonDoneWave(state);
}

/** Mark any wave whose members are all integrated as done. */
function advanceWaves(state: CampaignState): WaveState[] {
	const completed: WaveState[] = [];
	for (const wave of state.waves ?? []) {
		wave.integrated = wave.members.filter((id) => state.nodes[id]?.status === "done");
		const allDone =
			wave.members.length > 0 && wave.integrated.length === wave.members.length;
		if (allDone && wave.status !== "done") {
			wave.status = "done";
			completed.push(wave);
		}
	}
	state.current_wave = firstNonDoneWave(state);
	return completed;
}

function summarise(dag: Dag, state: CampaignState): string {
	const lines = [
		`campaign: ${dag.campaign ?? "(unnamed)"}`,
		`feature:  ${dag.feature_branch ?? "(unset)"}  base: ${dag.base ?? "(unset)"}`,
		`design:   ${dag.design ?? "(unspecified)"}`,
		`nodes:    ${nodeIds(dag).length}  wave size: ${
			state.wave_size ?? dag.concurrency ?? "?"
		}`,
	];
	const waveOf = new Map<string, number>();
	for (const wave of state.waves ?? []) {
		for (const id of wave.members) waveOf.set(id, wave.index);
	}
	for (const id of nodeIds(dag)) {
		const node = (dag.nodes ?? []).find((n) => n.id === id);
		const wave =
			waveOf.get(id) ?? state.nodes[id]?.wave ?? "?";
		lines.push(
			`  w${wave} ${id} [${node?.phase ?? "-"}] ${nodeStatus(state, id)}` +
				(node?.label ? ` — ${node.label}` : ""),
		);
	}
	for (const wave of state.waves ?? []) {
		lines.push(`wave ${wave.index} [${wave.status}]: ${wave.members.join(", ")}`);
	}
	return lines.join("\n");
}

export default function campaignExtension(pi: ExtensionAPI) {
	// Intergent is opt-in. The `campaign`/`ig` tools are registered inactive, so
	// a plain session neither lists them nor appends their prompt guidelines.
	// `/skill:intergent <design.md>` is the only thing that turns them on for
	// the session. A design document is required: without one the input is
	// dropped with a warning rather than starting a campaign.
	const SKILL_INVOCATION = /^\/skill:intergent(?:\s+([\s\S]*))?$/;

	function hasDesignDocument(args: string, cwd: string): boolean {
		for (const raw of args.split(/\s+/)) {
			if (!raw || raw.startsWith("-")) continue;
			const candidate = raw.replace(/^['"]|['"]$/g, "");
			if (fs.existsSync(path.resolve(cwd, candidate))) return true;
		}
		return false;
	}

	pi.on("input", (event, ctx) => {
		const match = SKILL_INVOCATION.exec(event.text.trim());
		if (!match) return;
		if (!hasDesignDocument(match[1] ?? "", ctx.cwd)) {
			if (ctx.hasUI) {
				ctx.ui.notify(
					"intergent: pass a design document, e.g. /skill:intergent docs/design.md",
					"warning",
				);
			}
			return { action: "handled" as const };
		}
		const active = new Set(pi.getActiveTools());
		active.add("campaign");
		active.add("ig");
		pi.setActiveTools([...active]);
	});

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

	/**
	 * Ensure `state.waves` matches the current DAG.  Waves are the engine's
	 * deterministic projection (`ig status` -> `dag_waves`); a coordinator-added
	 * `depends_on` edge changes the DAG fingerprint and triggers a replan.
	 * Replanning preserves each node's done/pending status and the per-wave
	 * cleanup flag, so a resume never re-runs finished work.
	 */
	async function ensureWaves(
		ctx: ExtensionContext,
		branch: string,
		dag: Dag,
		state: CampaignState,
	): Promise<void> {
		const fingerprint = dagFingerprint(dag);
		if (state.waves?.length && state.dag_fingerprint === fingerprint) return;
		const { json } = await ig(ctx, ["status"]);
		if (json?.dag_waves_error) throw new Error(`campaign: ${json.dag_waves_error}`);
		reconcileWaves(state, json?.dag_waves ?? []);
		state.dag_fingerprint = fingerprint;
		state.wave_size = Number(dag.concurrency ?? 3);
		writeJson(statePath(ctx.cwd, branch), state);
		logEvent(ctx.cwd, branch, "wave.replanned", {
			waves: (state.waves ?? []).map((w) => w.members),
		});
	}

	function widget(ctx: ExtensionContext, dag: Dag, state: CampaignState): void {
		if (!ctx.hasUI) return;
		const marker = (status: string) =>
			status === "running" ? "●" : status === "done" ? "✓" : status === "failed" ? "✗" : "·";
		const lines: string[] = [];
		for (const wave of state.waves ?? []) {
			const members = wave.members.map((id) => {
				const node = (dag.nodes ?? []).find((n) => n.id === id);
				const status = nodeStatus(state, id);
				return `${marker(status)} ${id}${node?.label ? ` ${node.label}` : ""}`.trim();
			});
			lines.push(`─ wave ${wave.index} [${wave.status}]  ${members.join("   ")}`);
		}
		if (!lines.length) {
			lines.push(
				...nodeIds(dag).map((id) => {
					const status = nodeStatus(state, id);
					return `${marker(status)} ${id} [${status}]`;
				}),
			);
		}
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
			await ensureWaves(ctx, branch, existing, state);
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
			`Rules: the DAG is the only authored schedule; waves are derived from owns + ` +
			`depends_on with concurrency (default 3) as the per-wave cap, and a strict ` +
			`scope overlap puts the later node in a later wave; keep same-wave owns disjoint; ` +
			`"phase" is a display label only; ` +
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
		await ensureWaves(ctx, branch, dag, state);
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
		await ensureWaves(ctx, branch, dag, state);

		const maxAttempts = Number(dag.max_attempts ?? 3);
		const attempts = Number(state.nodes[node]?.attempts ?? 0);
		if (attempts >= maxAttempts) {
			state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "failed" };
			writeJson(stateFile, state);
			throw new Error(`spawn: node '${node}' exceeded max_attempts=${maxAttempts}`);
		}

		// A node may only start in the current wave, and only once every
		// dependency is integrated (done), not merely verified.
		const wave = currentWave(state);
		if (!wave || !wave.members.includes(node)) {
			throw new Error(
				`spawn: node '${node}' is scheduled in wave ${state.nodes[node]?.wave ?? "?"}; ` +
					`current wave is ${wave?.index ?? "(none)"}`,
			);
		}
		if (!new Set(readyWaveNodes(dag, state)).has(node)) {
			throw new Error(`spawn: node '${node}' is not ready in wave ${wave.index}`);
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
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		await ensureWaves(ctx, branch, dag, state);
		const unit = node ? String(state.nodes[node]?.unit ?? node) : "";
		const args = unit
			? ["integrate", "--node", unit, "--cleanup", "none"]
			: ["integrate", "--cleanup", "none"];
		const { json: integrated, text } = await ig(ctx, args, signal);
		const results = integrated?.results ?? [];
		const failed = results.some((r: any) => r.status === "failed");
		let completed: WaveState[] = [];
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
			completed = advanceWaves(state);
			writeJson(stateFile, state);
			logEvent(ctx.cwd, branch, failed ? "node.integrate_failed" : "node.integrated", {
				node: node || "(sweep)",
				unit,
				results,
				completed_waves: completed.map((w) => w.index),
			});
		}

		// Cleanup is destructive and grouped by wave: when the last member of a
		// wave lands, ask once for that whole wave (§7).
		if (!failed && ctx.hasUI) {
			for (const wave of completed) {
				if (wave.cleanup_done) continue;
				const ok = await ctx.ui.confirm(
					`Wave ${wave.index} integrated onto ${branch}.`,
					`Remove the landed worktrees, branches, and logs for wave ${wave.index} ` +
						`(${wave.members.join(", ")}) now?`,
				);
				if (!ok) continue;
				await ig(ctx, ["integrate", "--cleanup", "worktrees"], signal);
				for (const id of wave.members) {
					try {
						fs.rmSync(logPath(ctx.cwd, branch, id));
					} catch {
						/* the log may not exist */
					}
				}
				wave.cleanup_done = true;
				writeJson(stateFile, state);
				logEvent(ctx.cwd, branch, "wave.cleaned", {
					wave: wave.index,
					members: wave.members,
				});
			}
		}
		return { content: [{ type: "text" as const, text }], details: integrated ?? {}, isError: failed };
	}

	pi.registerTool({
		name: "campaign",
		label: "Intergent campaign",
		description:
			"Coordinate a design into landed work: start (adopt current branch + planner), " +
			"status, ready, spawn (one-shot worker), verify (read-only verifier), " +
			"integrate (land a verified node), report. The dag.json plan is the only schedule; " +
			"waves are a projection of it.",
		promptSnippet: "Drive an Intergent campaign (start → spawn → verify → integrate)",
		promptGuidelines: [
			"The DAG in dag.json is the only authored schedule; waves are its deterministic " +
				"projection (owns + depends_on, capped by concurrency).",
			"Spawn nodes only from the current wave; a later wave starts after the previous " +
				"wave is fully integrated (its units fork from the updated feature branch).",
			"Spawn every ready node in the current wave together (issue the spawn calls in " +
				"one turn so they run in parallel); never exceed the wave cap.",
			"A node is ready only once every dependency is integrated (done), never merely verified.",
			"If a worker exits on a queued lease, add a depends_on edge in dag.json; the next " +
				"status/ready/spawn replans the waves to serialize it.",
			"Only the verifier may use the GPU (tools/gpu.sh); workers never touch it.",
			"Integrate each node after its verifier passes; cleanup is offered once per wave.",
		],
		// Inert until the `intergent` skill activates it, so a plain session never
		// advertises the campaign workflow or injects its guidelines.
		defaultActive: false,
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
					await ensureWaves(ctx, branch, dag, state);
					widget(ctx, dag, state);
					return {
						content: [{ type: "text" as const, text: summarise(dag, state) }],
						details: { dag, state },
					};
				}
				case "ready": {
					const branch = await featureBranch(ctx);
					const { dag, state } = load(ctx, branch);
					await ensureWaves(ctx, branch, dag, state);
					const wave = currentWave(state);
					const ready = readyWaveNodes(dag, state);
					const label = wave ? `wave ${wave.index}` : "(no open wave)";
					return {
						content: [
							{
								type: "text" as const,
								text: ready.length ? `${label}: ${ready.join(", ")}` : `(${label}: none ready)`,
							},
						],
						details: { ready, wave: wave?.index, waves: state.waves },
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
