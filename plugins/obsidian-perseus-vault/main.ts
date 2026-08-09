import {
  App,
  Plugin,
  PluginSettingTab,
  Setting,
  Notice,
  TFile,
  normalizePath,
  Modal,
} from "obsidian";
import { execSync, spawn } from "child_process";
import * as path from "path";
import * as fs from "fs";

// ─── Interfaces ────────────────────────────────────────────────────────

interface PerseusVaultPluginSettings {
  /** Path to the perseus_vault binary */
  perseusVaultBinaryPath: string;
  /** Path to the Perseus Vault SQLite database */
  perseusVaultDbPath: string;
  /** Sync folder name within the vault (relative) */
  syncFolder: string;
  /** Auto-sync interval in minutes (0 = disabled) */
  autoSyncIntervalMinutes: number;
  /** Show sync status in the status bar */
  showStatusBar: boolean;
}

const DEFAULT_SETTINGS: PerseusVaultPluginSettings = {
  perseusVaultBinaryPath: "perseus-vault",
  perseusVaultDbPath: "",
  syncFolder: "perseus-vault",
  autoSyncIntervalMinutes: 0,
  showStatusBar: true,
};

// ─── Plugin ────────────────────────────────────────────────────────────

export default class PerseusVaultSyncPlugin extends Plugin {
  settings: PerseusVaultPluginSettings;
  statusBar: HTMLElement | null = null;
  syncInterval: number | null = null;

  async onload() {
    await this.loadSettings();

    // Ensure sync folder exists
    const syncDir = this.getSyncFolderPath();
    if (!fs.existsSync(syncDir)) {
      fs.mkdirSync(syncDir, { recursive: true });
    }

    // Commands
    this.addCommand({
      id: "perseus_vault-sync-now",
      name: "Sync now (pull from Perseus Vault)",
      callback: () => this.pullFromVault(),
    });

    this.addCommand({
      id: "perseus_vault-export-vault",
      name: "Export vault to Perseus Vault sync folder",
      callback: () => this.exportVault(),
    });

    this.addCommand({
      id: "perseus_vault-push-note",
      name: "Push current note to Perseus Vault",
      editorCheckCallback: (checking, editor, view) => {
        if (!checking && view?.file) {
          this.pushNoteToVault(view.file);
        }
        return true;
      },
    });

    // Settings tab
    this.addSettingTab(new PerseusVaultSettingTab(this.app, this));

    // Status bar
    if (this.settings.showStatusBar) {
      this.statusBar = this.addStatusBarItem();
      this.statusBar.setText("Perseus Vault: ready");
    }

    // File watcher: auto-push on save
    this.registerEvent(
      this.app.vault.on("modify", (file) => {
        if (this.isInSyncFolder(file)) {
          this.pushNoteToVault(file);
        }
      })
    );

    this.registerEvent(
      this.app.vault.on("create", (file) => {
        if (this.isInSyncFolder(file)) {
          this.pushNoteToVault(file);
        }
      })
    );

    // Auto-sync timer
    if (this.settings.autoSyncIntervalMinutes > 0) {
      this.startAutoSync();
    }
  }

  onunload() {
    if (this.syncInterval) {
      window.clearInterval(this.syncInterval);
    }
  }

  // ─── Sync Operations ───────────────────────────────────────────────

  /** Pull entities from Perseus Vault database into the sync folder. */
  async pullFromVault() {
    this.setStatus("Syncing...");
    try {
      const binary = this.settings.perseusVaultBinaryPath;
      const dbPath = this.getDbPathArg();
      const vaultDir = this.getSyncFolderPath();

      // Run perseus_vault_vault_export via MCP stdio
      const request = JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "obsidian-perseus-vault", version: "0.1.0" },
        },
      });

      const exportCall = JSON.stringify({
        jsonrpc: "2.0",
        id: 2,
        method: "tools/call",
        params: {
          name: "perseus_vault_vault_export",
          arguments: { vault_dir: vaultDir },
        },
      });

      const input = request + "\n" + exportCall + "\n";
      const cmd = `${binary} serve ${dbPath}`;

      const result = execSync(cmd, {
        input,
        timeout: 30000,
        encoding: "utf-8",
        env: { ...process.env },
      });

      // Parse second line (the export response)
      const lines = result.trim().split("\n");
      if (lines.length >= 2) {
        const response = JSON.parse(lines[1]);
        if (response.result) {
          const content = JSON.parse(response.result.content[0].text);
          new Notice(
            `Perseus Vault: pulled ${content.exported || "?"} entities to ${this.settings.syncFolder}`
          );
          this.setStatus(`Synced (${content.exported || "?"} entities)`);
        } else if (response.error) {
          new Notice(`Perseus Vault sync error: ${response.error.message}`);
          this.setStatus("Error");
        }
      }
    } catch (e: any) {
      new Notice(`Perseus Vault sync failed: ${e.message}`);
      this.setStatus("Error");
    }
  }

  /** Run perseus_vault_vault_export to export all entities to the vault sync folder. */
  async exportVault() {
    this.setStatus("Exporting...");
    try {
      const binary = this.settings.perseusVaultBinaryPath;
      const dbPath = this.getDbPathArg();
      const vaultDir = this.getSyncFolderPath();

      const cmd = `${binary} serve ${dbPath}`;
      const request =
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {
            protocolVersion: "2024-11-05",
            capabilities: {},
            clientInfo: { name: "obsidian-perseus-vault", version: "0.1.0" },
          },
        }) +
        "\n" +
        JSON.stringify({
          jsonrpc: "2.0",
          id: 2,
          method: "tools/call",
          params: {
            name: "perseus_vault_vault_export",
            arguments: { vault_dir: vaultDir },
          },
        }) +
        "\n";

      const result = execSync(cmd, {
        input: request,
        timeout: 30000,
        encoding: "utf-8",
      });

      const lines = result.trim().split("\n");
      if (lines.length >= 2) {
        const response = JSON.parse(lines[1]);
        if (response.result) {
          const content = JSON.parse(response.result.content[0].text);
          new Notice(`Perseus Vault: exported ${content.exported || "?"} entities`);
          this.setStatus(`Exported ${content.exported || "?"}`);
        } else if (response.error) {
          new Notice(`Perseus Vault export error: ${response.error.message}`);
        }
      }
    } catch (e: any) {
      new Notice(`Perseus Vault export failed: ${e.message}`);
    }
  }

  /** Push a single Obsidian note to Perseus Vault via perseus_vault_remember. */
  async pushNoteToVault(file: TFile) {
    try {
      const binary = this.settings.perseusVaultBinaryPath;
      const dbPath = this.getDbPathArg();
      const content = await this.app.vault.read(file);

      // Extract YAML frontmatter for metadata
      const { category, key, tags } = this.parseFrontmatter(content);

      const request =
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {
            protocolVersion: "2024-11-05",
            capabilities: {},
            clientInfo: { name: "obsidian-perseus-vault", version: "0.1.0" },
          },
        }) +
        "\n" +
        JSON.stringify({
          jsonrpc: "2.0",
          id: 2,
          method: "tools/call",
          params: {
            name: "perseus_vault_remember",
            arguments: {
              id: "", // let Perseus Vault generate
              category: category || "obsidian",
              key: key || file.basename,
              body_json: JSON.stringify({ content, source: "obsidian" }),
              type: "insight",
              status: "active",
              tags: tags || [],
              topic_path: file.parent?.path?.replace(/^\//, "") || "",
            },
          },
        }) +
        "\n";

      const result = execSync(`${binary} serve ${dbPath}`, {
        input: request,
        timeout: 10000,
        encoding: "utf-8",
      });

      this.setStatus(`Pushed: ${file.basename}`);
    } catch (e: any) {
      // Silently skip push errors to avoid noise on every save
      console.debug("Perseus Vault push error:", e.message);
    }
  }

  // ─── Helpers ────────────────────────────────────────────────────────

  private getSyncFolderPath(): string {
    const vaultRoot = (this.app.vault.adapter as any).getBasePath?.() || "";
    return normalizePath(path.join(vaultRoot, this.settings.syncFolder));
  }

  private getDbPathArg(): string {
    return this.settings.perseusVaultDbPath
      ? `--db "${this.settings.perseusVaultDbPath}"`
      : "";
  }

  private isInSyncFolder(file: TFile): boolean {
    return file.path.startsWith(this.settings.syncFolder + "/");
  }

  private setStatus(text: string) {
    if (this.statusBar) {
      this.statusBar.setText(`Perseus Vault: ${text}`);
    }
  }

  private parseFrontmatter(content: string): {
    category?: string;
    key?: string;
    tags?: string[];
  } {
    const match = content.match(/^---\n([\s\S]*?)\n---/);
    if (!match) return {};
    const fm: Record<string, any> = {};
    for (const line of match[1].split("\n")) {
      const colon = line.indexOf(":");
      if (colon > 0) {
        const key = line.slice(0, colon).trim();
        let value: any = line.slice(colon + 1).trim();
        if (value.startsWith("[") && value.endsWith("]")) {
          value = value
            .slice(1, -1)
            .split(",")
            .map((s: string) => s.trim().replace(/^"|"$/g, ""));
        }
        fm[key] = value;
      }
    }
    return {
      category: fm.category,
      key: fm.key || fm.id,
      tags: Array.isArray(fm.tags) ? fm.tags : fm.tags ? [fm.tags] : undefined,
    };
  }

  private startAutoSync() {
    this.syncInterval = window.setInterval(() => {
      this.pullFromVault();
    }, this.settings.autoSyncIntervalMinutes * 60 * 1000);
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }
}

// ─── Settings Tab ──────────────────────────────────────────────────────

class PerseusVaultSettingTab extends PluginSettingTab {
  plugin: PerseusVaultSyncPlugin;

  constructor(app: App, plugin: PerseusVaultSyncPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();

    containerEl.createEl("h2", { text: "Perseus Vault Sync Settings" });

    new Setting(containerEl)
      .setName("Perseus Vault binary path")
      .setDesc("Path to the perseus_vault executable")
      .addText((text) =>
        text
          .setPlaceholder("perseus-vault")
          .setValue(this.plugin.settings.perseusVaultBinaryPath)
          .onChange(async (value) => {
            this.plugin.settings.perseusVaultBinaryPath = value;
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Perseus Vault database path")
      .setDesc("Path to the SQLite database (leave empty for default ~/.perseus-vault/data/perseus-vault.db)")
      .addText((text) =>
        text
          .setPlaceholder("")
          .setValue(this.plugin.settings.perseusVaultDbPath)
          .onChange(async (value) => {
            this.plugin.settings.perseusVaultDbPath = value;
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Sync folder")
      .setDesc("Vault folder to sync with Perseus Vault (created if missing)")
      .addText((text) =>
        text
          .setPlaceholder("perseus-vault")
          .setValue(this.plugin.settings.syncFolder)
          .onChange(async (value) => {
            this.plugin.settings.syncFolder = value;
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl)
      .setName("Auto-sync interval (minutes)")
      .setDesc("0 = disabled. Automatically pulls from Perseus Vault on this interval.")
      .addText((text) =>
        text
          .setPlaceholder("0")
          .setValue(String(this.plugin.settings.autoSyncIntervalMinutes))
          .onChange(async (value) => {
            const v = parseInt(value) || 0;
            this.plugin.settings.autoSyncIntervalMinutes = v;
            await this.plugin.saveSettings();
            if (v > 0) {
              this.plugin.startAutoSync();
            } else if (this.plugin.syncInterval) {
              window.clearInterval(this.plugin.syncInterval);
              this.plugin.syncInterval = null;
            }
          })
      );

    new Setting(containerEl)
      .setName("Show status bar")
      .setDesc("Display sync status in the Obsidian status bar")
      .addToggle((toggle) =>
        toggle
          .setValue(this.plugin.settings.showStatusBar)
          .onChange(async (value) => {
            this.plugin.settings.showStatusBar = value;
            await this.plugin.saveSettings();
          })
      );
  }
}
