import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { snapshot } from "mcp-parser";

const rawSpec = await snapshot({
  transport: {
    type: "stdio",
    command: "../target/debug/perseus-vault",
    args: [
      "serve",
      "--db",
      "/tmp/perseus-sourcey.db"
    ],
    timeout: 180_000
  }
});

const rawJson = `${JSON.stringify(rawSpec, null, 2)}\n`;
const rawSha256 = createHash("sha256").update(rawJson).digest("hex");
await writeFile("mcp.raw.json", rawJson);

const renderedSpec = structuredClone(rawSpec);
renderedSpec.description =
  "Persistent, encrypted, deterministic memory for AI agents. Local-first and MCP-native.";
renderedSpec.transport = {
  type: "stdio",
  command: "perseus-vault",
  args: ["serve", "--db", "~/.perseus-vault/data/perseus-vault.db"]
};
for (const tool of renderedSpec.tools ?? []) {
  tool.inputSchema ??= { type: "object", properties: {} };
  tool.inputSchema.type ??= "object";
  // Sourcey 3.6.5 cannot generate examples for conditional allOf branches.
  // Keep the canonical fields and required list; omit only those conditions
  // from the documentation snapshot so every live tool remains renderable.
  delete tool.inputSchema.allOf;
}

const rawNames = (rawSpec.tools ?? []).map(({ name }) => name);
const renderedNames = (renderedSpec.tools ?? []).map(({ name }) => name);
const unique = (names) => new Set(names).size === names.length;
if (!unique(rawNames) || !unique(renderedNames)) {
  throw new Error("MCP snapshots contain duplicate tool names");
}
if (
  rawNames.length !== renderedNames.length ||
  rawNames.some((name, index) => name !== renderedNames[index])
) {
  throw new Error("Raw and rendered MCP snapshots have different tool names");
}

const packageJson = JSON.parse(await readFile("package.json", "utf8"));
const metadata = {
  source_commit: execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: "..",
    encoding: "utf8"
  }).trim(),
  vault_version: rawSpec.server.version,
  feature_profile: "--no-default-features",
  generators: {
    sourcey: packageJson.dependencies.sourcey,
    mcp_parser: packageJson.dependencies["mcp-parser"]
  },
  tool_count: rawNames.length,
  raw_snapshot_sha256: rawSha256,
  raw_snapshot: "mcp.raw.json",
  rendered_snapshot: "mcp.render.json"
};

await writeFile("mcp.render.json", `${JSON.stringify(renderedSpec, null, 2)}\n`);
await writeFile("metadata.json", `${JSON.stringify(metadata, null, 2)}\n`);
