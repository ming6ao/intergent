#!/usr/bin/env node
/**
 * Type-check the pi extensions against the pi runtime the developer runs.
 *
 * The pi runtime ships its own pinned dependency tree (including an
 * `npm-shrinkwrap.json`), so installing `@earendil-works/pi-coding-agent` from
 * npm only to read its `.d.ts` files drags in a transitive copy of
 * `brace-expansion` that npm cannot override (a dependency shrinkwrap beats
 * `overrides`).  Instead, resolve the three module specifiers from the pi
 * installation already present on this machine (or a local `node_modules` copy
 * when one exists) and point `tsc` at them with `paths`.
 *
 * Resolution order for the pi package:
 *   1. `$SLICEME_PI_PACKAGE`
 *   2. `./node_modules/@earendil-works/pi-coding-agent`
 *   3. the package behind the `pi` executable on `PATH`
 *
 * Exit codes: 0 clean, 1 type errors, 3 pi runtime or typescript not found
 * (callers may treat 3 as "skipped").
 */
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import {
	existsSync,
	readFileSync,
	realpathSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const generated = path.join(root, ".tsconfig.typecheck.json");
const SKIP = 3;

function isPiPackage(dir) {
	const manifest = path.join(dir, "package.json");
	if (!existsSync(manifest)) return false;
	try {
		return JSON.parse(readFileSync(manifest, "utf8")).name ===
			"@earendil-works/pi-coding-agent";
	} catch {
		return false;
	}
}

function packageBehindExecutable(name) {
	let resolved;
	try {
		const which = process.platform === "win32" ? "where" : "which";
		resolved = execFileSync(which, [name], { encoding: "utf8" })
			.split(/\r?\n/)
			.map((line) => line.trim())
			.find(Boolean);
	} catch {
		return undefined;
	}
	if (!resolved) return undefined;
	try {
		resolved = realpathSync(resolved);
	} catch {
		/* keep the unresolved path */
	}
	let dir = path.dirname(resolved);
	while (true) {
		if (isPiPackage(dir)) return dir;
		const parent = path.dirname(dir);
		if (parent === dir) return undefined;
		dir = parent;
	}
}

function findPiPackage() {
	const candidates = [];
	if (process.env.SLICEME_PI_PACKAGE) candidates.push(process.env.SLICEME_PI_PACKAGE);
	candidates.push(path.join(root, "node_modules", "@earendil-works", "pi-coding-agent"));
	const fromPath = packageBehindExecutable("pi");
	if (fromPath) candidates.push(fromPath);
	return candidates.find((dir) => dir && isPiPackage(dir));
}

function skip(message) {
	console.error(`typecheck: ${message}`);
	console.error("  Install the pi runtime, or set SLICEME_PI_PACKAGE to its package directory.");
	process.exit(SKIP);
}

const piDir = findPiPackage();
if (!piDir) skip("could not locate @earendil-works/pi-coding-agent.");

const piAiDir = path.join(piDir, "node_modules", "@earendil-works", "pi-ai");
const typeboxDir = path.join(piDir, "node_modules", "typebox");
for (const [name, dir] of [
	["@earendil-works/pi-ai", piAiDir],
	["typebox", typeboxDir],
]) {
	if (!existsSync(dir)) skip(`could not locate ${name} under ${piDir}/node_modules`);
}

let tsc;
try {
	tsc = require.resolve("typescript/bin/tsc");
} catch {
	skip("typescript is not installed; run `npm install`.");
}

const config = {
	extends: "./tsconfig.json",
	compilerOptions: {
		baseUrl: ".",
		paths: {
			"@earendil-works/pi-coding-agent": [piDir],
			"@earendil-works/pi-ai": [piAiDir],
			typebox: [typeboxDir],
		},
	},
};
writeFileSync(generated, JSON.stringify(config, null, 2) + "\n", "utf8");
try {
	execFileSync(process.execPath, [tsc, "--project", generated, "--noEmit"], {
		stdio: "inherit",
	});
	console.log(`typecheck: OK (types from ${piDir})`);
} catch (error) {
	process.exitCode = typeof error.status === "number" ? error.status : 1;
} finally {
	rmSync(generated, { force: true });
}
