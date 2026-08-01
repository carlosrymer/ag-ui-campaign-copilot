/**
 * Protocol conformance check.
 *
 * The recordings are produced by the Python backend (`ag-ui-protocol` on PyPI).
 * This script validates every recorded event against the TypeScript zod schemas
 * shipped in `@ag-ui/core` -- i.e. a genuine cross-language check that what the
 * Python side emits is what the JS side expects.
 *
 * Run: npm run validate
 */
import { EventSchemas } from "@ag-ui/core";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const recDir = path.resolve(here, "../../recordings");

const scenarios = JSON.parse(
  fs.readFileSync(path.join(recDir, "index.json"), "utf8"),
).recordings;

let total = 0;
let ok = 0;
const failures = new Map();
const byType = new Map();

for (const s of scenarios) {
  const doc = JSON.parse(fs.readFileSync(path.join(recDir, s.file), "utf8"));
  for (const { event } of doc.events) {
    total++;
    byType.set(event.type, (byType.get(event.type) ?? 0) + 1);
    const res = EventSchemas.safeParse(event);
    if (res.success) {
      ok++;
    } else {
      const key = `${event.type}: ${res.error.issues
        .map((i) => `${i.path.join(".") || "<root>"} ${i.message}`)
        .join("; ")}`;
      failures.set(key, (failures.get(key) ?? 0) + 1);
    }
  }
}

console.log(`AG-UI conformance: ${ok}/${total} recorded events validate against @ag-ui/core EventSchemas`);
console.log("\nevents by type:");
for (const [t, n] of [...byType.entries()].sort()) {
  console.log(`  ${String(n).padStart(4)}  ${t}`);
}

if (failures.size) {
  console.log("\nfailures:");
  for (const [k, n] of failures) console.log(`  x${n}  ${k}`);
  process.exit(1);
}
console.log("\nall recorded events conform.");
