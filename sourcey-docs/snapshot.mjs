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

await writeFile("mcp.json", `${JSON.stringify(spec, null, 2)}\n`);
