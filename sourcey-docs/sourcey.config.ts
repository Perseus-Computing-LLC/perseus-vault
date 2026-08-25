import { defineConfig, mcp } from "sourcey";

export default defineConfig({
  name: "Perseus Vault",
  description: "Generated MCP tool reference for Perseus Vault",
  navigation: {
    tabs: [
      {
        tab: "MCP tools",
        slug: "mcp-tools",
        source: mcp({ spec: "./mcp.json" })
      }
    ]
  }
});
