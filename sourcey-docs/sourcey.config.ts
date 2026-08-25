import { defineConfig, mcp } from "sourcey";

export default defineConfig({
  name: "Perseus Vault",
  description: "Generated MCP tool reference for Perseus Vault",
  theme: {
    css: ["./mobile.css"]
  },
  navigation: {
    tabs: [
      {
        tab: "MCP tools",
        slug: "mcp-tools",
        source: mcp({ spec: "./mcp.render.json" })
      }
    ]
  }
});
