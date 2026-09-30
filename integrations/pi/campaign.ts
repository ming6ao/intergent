/**
 * Intergent **campaign** extension for pi — the coordinator tool.
 *
 * One top-level coordinator session turns a design document into landed work by
 * spawning a planner, one-shot workers, and a read-only verifier, while
 * `intergent` remains the deterministic isolation/integration engine.  The DAG
 * in `.intergent/<branch-key>.dag.json` is the only schedule; there are no
 * phases in the scheduler (docs/orchestration.md).
 *
 * One tool, `campaign`, mirrors the shape of `ig`:
 *
 *   start <design>   feature branch + no-unit plane + planner -> dag.json
 *   status           merge `ig status --json` with live child state
 *   ready            nodes whose every dependency is done
 *   spawn <node>     create the unit, launch a one-shot worker, tee its log
 *   verify <node>    verifier on the latest prepared candidate; record a verdict
 *   integrate <node> land the verified candidate onto the feature branch
 *   report           `ig report` plus the coordinator's narrative
 *
 * Workers are child processes of the coordinator and are **not detached**: an
 * orchestrator crash kills them, and on resume any `running` node is reset to
 * `pending` (its worktree reset before re-spawn).  Only the verifier is given
 * the GPU broker (`tools/gpu.sh`); workers never touch the GPU.
 *
 * Install alongside `intergent.ts`:
 *   cp integrations/pi/campaign.ts ~/.pi/agent/extensions/campaign.ts
 *   cp -r integrations/pi/agents ~/.pi/agent/campaign-agents
 * Launch the coordinator with INTERGENT_AUTO_BOOTSTRAP=0.
 */

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const CAMPAIGN_ACTIONS = [
	"start",
	"status",
	"ready",
	"spawn",
	"verify",
	"integrate",
	"report",
] as const;

const AGENT_DIR_CANDIDATES = [
	() => path.join(getAgentDirSafe(), "campaign-agents"),
	() => path.join(__dirnameSafe(), "agents"),
];

function __dirnameSafe(): string {
	// ESM has no __dirname; fall back to the extension's own directory if known.
	const url = import.meta?.url;
	if (url) return path.dirname(new URL(url).pathname);
	return process.cwd();
}

function getAgentDirSafe(): string {
	return process.env.PI_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
}

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

interface IgResult {
	text: string;
	json: any;
}

interface SubagentResult {
	exitCode: number;
	output: string;
	stderr: string;
}

function parseJson(text: string): any {
	try {
		return JSON.parse(text);
	} catch {
		return undefined;
	}
}

function branchKey(branch: string): string {
	return (branch || "main").trim().replace(/\//g, "--") || "main";
}

function stateDir(cwd: string): string {
	return path.join(cwd, ".intergent");
}

function dagPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.dag.json`);
}

function statePath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.state.json`);
}

function logPath(cwd: string, branch: string, node: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.worker_${node}.log`);
}

function eventsPath(cwd: string, branch: string): string {
	return path.join(stateDir(cwd), `${branchKey(branch)}.events.jsonl`);
}

/**
 * Append one audit line to the campaign event log. The DAG can be hand-edited,
 * so plan evolution (added edges, split nodes, retries) is recorded here next to
 * commits and fingerprints (docs/orchestration.md §9).
 */
function logEvent(cwd: string, branch: string, kind: string, data: unknown): void {
	const file = eventsPath(cwd, branch);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.appendFileSync(file, JSON.stringify({ kind, data }) + "\n", "utf8");
}

function readJson<T>(file: string, fallback: T): T {
	try {
		return JSON.parse(fs.readFileSync(file, "utf8")) as T;
	} catch {
		return fallback;
	}
}

function writeJson(file: string, data: unknown): void {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n", "utf8");
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

// ---------------------------------------------------------------------------
// Process helpers
// ---------------------------------------------------------------------------

function getPiInvocation(args: string[]): { command: string; args: string[] } {
	const currentScript = process.argv[1];
	const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
	if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
		return { command: process.execPath, args: [currentScript, ...args] };
	}
	const execName = path.basename(process.execPath).toLowerCase();
	if (!/^(node|bun)(\.exe)?$/.test(execName)) {
		return { command: process.execPath, args };
	}
	return { command: "pi", args };
}

function findAgentFile(name: string): string | undefined {
	for (const candidate of AGENT_DIR_CANDIDATES) {
		const file = path.join(candidate(), `${name}.md`);
		if (fs.existsSync(file)) return file;
	}
	return undefined;
}

/** Spawn a one-shot `pi` subagent, tee its raw output to *log*, return the final text. */
async function runSubagent(
	ctx: ExtensionContext,
	options: {
		agent: string;
		task: string;
		cwd: string;
		log?: string;
		signal?: AbortSignal;
	},
): Promise<SubagentResult> {
	const agentFile = findAgentFile(options.agent);
	const args = ["--mode", "json", "-p", "--no-session"];
	let promptPath: string | undefined;
	if (agentFile) {
		// Strip the YAML frontmatter before appending the system prompt.
		const raw = fs.readFileSync(agentFile, "utf8");
		const stripped = raw.replace(/^---\n[\s\S]*?\n---\n/, "");
		promptPath = path.join(
			os.tmpdir(),
			`ig-campaign-${options.agent}-${process.pid}-${Date.now()}.md`,
		);
		fs.writeFileSync(promptPath, stripped, "utf8");
		args.push("--append-system-prompt", promptPath);
	}
	args.push(options.task);

	const cleanup = () => {
		if (promptPath) {
			try {
				fs.unlinkSync(promptPath);
			} catch {
				/* ignore */
			}
		}
	};

	const invocation = getPiInvocation(args);
	return new Promise<SubagentResult>((resolve) => {
		const proc = spawn(invocation.command, invocation.args, {
			cwd: options.cwd,
			shell: false,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let buffer = "";
		let output = "";
		let stderr = "";
		let stream: fs.WriteStream | undefined;
		if (options.log) {
			fs.mkdirSync(path.dirname(options.log), { recursive: true });
			stream = fs.createWriteStream(options.log, { flags: "a" });
		}

		const processLine = (line: string) => {
			if (!line.trim()) return;
			stream?.write(line + "\n");
			let event: any;
			try {
				event = JSON.parse(line);
			} catch {
				return;
			}
			if (event.type === "message_end" && event.message?.role === "assistant") {
				for (const part of event.message.content ?? []) {
					if (part.type === "text") output = part.text;
				}
			}
		};

		proc.stdout.on("data", (data) => {
			buffer += data.toString();
			const lines = buffer.split("\n");
			buffer = lines.pop() ?? "";
			for (const line of lines) processLine(line);
		});
		proc.stderr.on("data", (data) => {
			stderr += data.toString();
			stream?.write(data.toString());
		});
		proc.on("close", (code) => {
			if (buffer.trim()) processLine(buffer);
			stream?.end();
			cleanup();
			resolve({ exitCode: code ?? 0, output, stderr });
		});
		proc.on("error", (err) => {
			stream?.end();
			cleanup();
			resolve({ exitCode: 1, output, stderr: String(err) });
		});

		if (options.signal) {
			const kill = () => {
				proc.kill("SIGTERM");
				setTimeout(() => {
					if (!proc.killed) proc.kill("SIGKILL");
				}, 5000);
			};
			if (options.signal.aborted) kill();
			else options.signal.addEventListener("abort", kill, { once: true });
		}
	});
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function campaignExtension(pi: ExtensionAPI) {
	const bin = process.env.INTERGENT_BIN || "intergent";

	async function runIg(
		ctx: ExtensionContext,
		args: string[],
		signal?: AbortSignal,
	): Promise<IgResult> {
		const result = await pi.exec(bin, ["--json", ...args], {
			cwd: ctx.cwd,
			signal,
			timeout: 600_000,
		});
		const text = [result.stdout, result.stderr].filter((s) => s?.trim()).join("\n").trim();
		if (result.code !== 0) {
			throw new Error(text || `intergent exited with code ${result.code}`);
		}
		return { text: text || "ok", json: parseJson(result.stdout) };
	}

	async function featureBranch(ctx: ExtensionContext): Promise<string> {
		const { json } = await runIg(ctx, ["status"]);
		const branch = json?.feature_branch ?? json?.main_branch;
		if (!branch) throw new Error("campaign: no feature branch; run `campaign start` first");
		return String(branch);
	}

	function load(ctx: ExtensionContext, branch: string): { dag: Dag; state: CampaignState } {
		const dag = readJson<Dag>(dagPath(ctx.cwd, branch), { nodes: [] });
		const state = readJson<CampaignState>(statePath(ctx.cwd, branch), { nodes: {} });
		return { dag, state };
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

	pi.registerTool({
		name: "campaign",
		label: "Intergent campaign",
		description:
			"Coordinate a design into landed work: start (feature branch + planner), " +
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
			feature_branch: Type.Optional(Type.String({ description: "start: feature branch" })),
			base: Type.Optional(Type.String({ description: "start: base branch" })),
			node: Type.Optional(Type.String({ description: "node id for spawn/verify/integrate" })),
			narrative: Type.Optional(Type.String({ description: "report: what-changed/risks text" })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const action = params.action as (typeof CAMPAIGN_ACTIONS)[number];
			switch (action) {
				case "start":
					return startCampaign(ctx, runIg, runSubagent, params, signal);
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
					return spawnNode(ctx, runIg, runSubagent, params, signal);
				case "verify":
					return verifyNode(ctx, runIg, runSubagent, params, signal);
				case "integrate":
					return integrateNode(ctx, runIg, params, signal);
				case "report": {
					const branch = await featureBranch(ctx);
					const { dag } = load(ctx, branch);
					const args = ["report"];
					if (params.narrative) args.push("--narrative", String(params.narrative));
					if (dag.design) args.push("--design", dag.design);
					const { json, text } = await runIg(ctx, args, signal);
					return { content: [{ type: "text" as const, text }], details: json ?? {} };
				}
				default:
					throw new Error(`campaign: unknown action '${action}'`);
			}
		},
	});
}

async function startCampaign(
	ctx: ExtensionContext,
	runIg: (ctx: ExtensionContext, args: string[], signal?: AbortSignal) => Promise<IgResult>,
	runSubagent: (
		ctx: ExtensionContext,
		options: { agent: string; task: string; cwd: string; log?: string; signal?: AbortSignal },
	) => Promise<SubagentResult>,
	params: any,
	signal?: AbortSignal,
): Promise<any> {
	const design = String(params.design ?? "DESIGN.md");
	const campaign = String(params.campaign ?? path.basename(ctx.cwd));
	const requestedBranch = params.feature_branch ? String(params.feature_branch) : undefined;
	const base = String(params.base ?? "main");

	// Resume picks the branch from an existing dag.json; `replan` ignores it.
	const probeBranch = requestedBranch ?? `feat/${campaign}`;
	const probeFile = path.join(stateDir(ctx.cwd), `${branchKey(probeBranch)}.dag.json`);
	const existing = fs.existsSync(probeFile)
		? parseJson(fs.readFileSync(probeFile, "utf8"))
		: undefined;
	const branch = String(existing?.feature_branch ?? probeBranch);

	// 1. Feature branch + a plane with no coordinator unit.
	await runIg(ctx, ["start", "--no-unit", "--main", branch, "--base", base], signal);

	const dagFile = path.join(stateDir(ctx.cwd), `${branchKey(branch)}.dag.json`);
	const stateFile = path.join(stateDir(ctx.cwd), `${branchKey(branch)}.state.json`);

	// 2a. Resume: rebuild node status from git/state.db, which always win over
	// state.json. A node left `running` by a crash is reset to `pending`.
	if (existing?.nodes?.length && !params.replan) {
		const state: any = readJson(stateFile, { nodes: {} });
		const status = (await runIg(ctx, ["status"], signal)).json;
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
			const candidate = candidates.find(
				(c: any) => c.unit_name === (entry.unit ?? node),
			);
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
			content: [{ type: "text" as const, text: summarise(existing, state) }],
			details: { dag: existing, state, resumed: true },
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
	const planner = await runSubagent(ctx, {
		agent: "planner",
		task,
		cwd: ctx.cwd,
		log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_planner.log`),
		signal,
	});
	if (planner.exitCode !== 0) {
		return {
			content: [{ type: "text" as const, text: `planner failed:\n${planner.stderr}` }],
			isError: true,
		};
	}
	const dag = parseJson(fs.existsSync(dagFile) ? fs.readFileSync(dagFile, "utf8") : "{}");
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
	return { content: [{ type: "text" as const, text: summarise(dag, state) }], details: { dag, state } };
}

async function spawnNode(
	ctx: ExtensionContext,
	runIg: (ctx: ExtensionContext, args: string[], signal?: AbortSignal) => Promise<IgResult>,
	runSubagent: (
		ctx: ExtensionContext,
		options: { agent: string; task: string; cwd: string; log?: string; signal?: AbortSignal },
	) => Promise<SubagentResult>,
	params: any,
	signal?: AbortSignal,
): Promise<any> {
	const node = String(params.node ?? "");
	if (!node) throw new Error("spawn requires --node <id>");
	const { json } = await runIg(ctx, ["status"]);
	const branch = String(json?.feature_branch ?? "main");
	const stateFile = path.join(stateDir(ctx.cwd), `${branchKey(branch)}.state.json`);
	const dag: any = readJson(path.join(stateDir(ctx.cwd), `${branchKey(branch)}.dag.json`), { nodes: [] });
	const state: any = readJson(stateFile, { nodes: {} });
	const spec = (dag.nodes ?? []).find((n: any) => n.id === node);
	if (!spec) throw new Error(`spawn: unknown node '${node}'`);

	const maxAttempts = Number(dag.max_attempts ?? 3);
	const attempts = Number(state.nodes[node]?.attempts ?? 0);
	if (attempts >= maxAttempts) {
		state.nodes[node] = { ...(state.nodes[node] ?? {}), status: "failed" };
		writeJson(stateFile, state);
		throw new Error(`spawn: node '${node}' exceeded max_attempts=${maxAttempts}`);
	}

	// Every dependency must already be integrated (done), not merely verified.
	const ready = new Set(readyNodes(dag, state));
	if (!ready.has(node)) throw new Error(`spawn: node '${node}' is not ready`);

	// A re-spawn uses a fresh unit name so the previous attempt cannot collide;
	// the old unit is released and pruned first.
	const attempt = attempts + 1;
	const unitName = attempt === 1 ? node : `${node}-a${attempt}`;
	const previous = state.nodes[node]?.unit;
	if (previous) {
		try {
			await runIg(ctx, ["declare", "--unit", previous, "--release"], signal);
			await runIg(ctx, ["status", "--gc"], signal);
		} catch {
			/* the unit may already be gone */
		}
	}

	const created = await runIg(
		ctx,
		["start", "--name", unitName, "--base", branch, "--kind", "worker"],
		signal,
	);
	const worktree = created.json?.worktree;
	if (!worktree) throw new Error(`spawn: could not create a unit for '${node}'`);
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
		`You MUST use the ig lifecycle: declare every owned scope, edit only your worktree, ` +
		`then commit. Never use the GPU and never touch another node.` +
		previousEvidence;
	const result = await runSubagent(ctx, {
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
	runIg: (ctx: ExtensionContext, args: string[], signal?: AbortSignal) => Promise<IgResult>,
	runSubagent: (
		ctx: ExtensionContext,
		options: { agent: string; task: string; cwd: string; log?: string; signal?: AbortSignal },
	) => Promise<SubagentResult>,
	params: any,
	signal?: AbortSignal,
): Promise<any> {
	const node = String(params.node ?? "");
	if (!node) throw new Error("verify requires --node <id>");
	const branch = await (async () => {
		const { json } = await runIg(ctx, ["status"]);
		return String(json?.feature_branch ?? "main");
	})();
	const dag: any = readJson(path.join(stateDir(ctx.cwd), `${branchKey(branch)}.dag.json`), { nodes: [] });
	const stateFile = path.join(stateDir(ctx.cwd), `${branchKey(branch)}.state.json`);
	const state: any = readJson(stateFile, { nodes: {} });
	const spec = (dag.nodes ?? []).find((n: any) => n.id === node);
	if (!spec) throw new Error(`verify: unknown node '${node}'`);
	const unit = String(state.nodes[node]?.unit ?? node);

	// Read-only verifier: T0 CPU, then tools/gpu.sh for GPU acceptance.
	const task =
		`Independently verify DAG node "${node}". Do not edit any file. Run the acceptance ` +
		`commands: ${(spec.acceptance ?? []).join(" ; ")}. If and only if the node's gpu tier is ` +
		`not "none" (it is "${spec.gpu ?? "none"}"), wrap GPU commands with ` +
		`\`tools/gpu.sh --tier <tier> -- <command>\`. Report a single line starting with ` +
		`"VERDICT: PASS" or "VERDICT: FAIL", then the evidence.`;
	const result = await runSubagent(ctx, {
		agent: "verifier",
		task,
		cwd: ctx.cwd,
		log: path.join(stateDir(ctx.cwd), `${branchKey(branch)}.worker_verify_${node}.log`),
		signal,
	});

	// Record the verdict in intergent (§6.4) so re-integration can reuse it.
	const acceptanceArgs: string[] = [];
	for (const cmd of spec.acceptance ?? []) acceptanceArgs.push("--acceptance", cmd);
	const recorded = await runIg(
		ctx,
		["integrate", "--node", unit, "--check-only", "--gpu", spec.gpu ?? "none", ...acceptanceArgs],
		signal,
	);
	const passed =
		result.exitCode === 0 && /VERDICT:\s*PASS/i.test(result.output) &&
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
		content: [{ type: "text", text: `${node}: ${passed ? "PASS" : "FAIL"}\n${result.output}` }],
		details: { node, passed, recorded: recorded.json },
		isError: !passed,
	};
}

async function integrateNode(
	ctx: ExtensionContext,
	runIg: (ctx: ExtensionContext, args: string[], signal?: AbortSignal) => Promise<IgResult>,
	params: any,
	signal?: AbortSignal,
): Promise<any> {
	const node = String(params.node ?? "");
	const { json } = await runIg(ctx, ["status"]);
	const branch = String(json?.feature_branch ?? "main");
	const stateFile = path.join(stateDir(ctx.cwd), `${branchKey(branch)}.state.json`);
	const state: any = readJson(stateFile, { nodes: {} });
	const unit = node ? String(state.nodes[node]?.unit ?? node) : "";
	const args = unit
		? ["integrate", "--node", unit, "--cleanup", "none"]
		: ["integrate", "--cleanup", "none"];
	const { json: integrated, text } = await runIg(ctx, args, signal);
	const results = integrated?.results ?? [];
	const failed = results.some((r: any) => r.status === "failed");
	if (results.length) {
		const landedUnits: string[] = results
			.filter((r: any) => r.status === "landed")
			.map((r: any) => r.unit);
		// A sweep (no --node) lands any straggler; mark matching nodes done.
		for (const id of Object.keys(state.nodes)) {
			if (landedUnits.includes(state.nodes[id].unit ?? id)) {
				state.nodes[id].status = "done";
			}
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
			"Remove unit worktrees for this campaign?",
		);
		if (ok) await runIg(ctx, ["integrate", "--cleanup", "worktrees"], signal);
	}
	return { content: [{ type: "text", text }], details: integrated ?? {}, isError: failed };
}

export { CAMPAIGN_ACTIONS, branchKey, readyNodes, summarise };
