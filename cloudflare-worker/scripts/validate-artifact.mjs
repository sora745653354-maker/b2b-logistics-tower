import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const workerPath = resolve(root, "dist/server/index.js");
const source = await readFile(workerPath, "utf8");
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const workerModule = await import(moduleUrl);
assert.equal(typeof workerModule.default?.fetch, "function", `${pathToFileURL(workerPath)} must export default.fetch`);
assert.ok(!source.includes("__INITIAL_SNAPSHOT_GZIP_BASE64__"), "Worker must not embed an initial order snapshot");
console.log("Artifact is valid ESM, exports default.fetch, and contains no initial snapshot.");
