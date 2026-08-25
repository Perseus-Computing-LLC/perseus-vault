import { writeFile } from "node:fs/promises";
import { snapshot } from "mcp-parser";

const spec = await snapshot({
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

spec.description =
  "Persistent, encrypted, deterministic memory for AI agents. Local-first and MCP-native.";
for (const tool of spec.tools ?? []) {
  tool.inputSchema ??= { type: "object", properties: {} };
  tool.inputSchema.type ??= "object";
}

await writeFile("mcp.json", `${JSON.stringify(spec, null, 2)}\n`);
