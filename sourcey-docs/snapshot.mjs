import { writeFile } from "node:fs/promises";
import { snapshot } from "mcp-parser";

const spec = await snapshot({
  transport: {
    type: "stdio",
    command: "cargo",
    args: [
      "run",
      "--quiet",
      "--manifest-path",
      "../Cargo.toml",
      "--no-default-features",
      "--",
      "serve",
      "--db",
      "/tmp/perseus-sourcey.db"
    ],
    timeout: 180_000
  }
});

await writeFile("mcp.json", `${JSON.stringify(spec, null, 2)}\n`);
