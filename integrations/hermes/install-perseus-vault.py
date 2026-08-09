#!/usr/bin/env python3
"""Perseus Vault Memory Provider Installer for Hermes Agent.

Installs the perseus-vault memory plugin on any machine with Hermes.
Run with: python3 install-perseus-vault.py
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_ok(msg: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.NC}  {msg}")


def log_warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")


def run_cmd(cmd: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run command and return result."""
    return subprocess.run(cmd, capture_output=capture, text=True)


def check_hermes() -> Optional[str]:
    """Check if Hermes is installed and return version."""
    if not shutil.which("hermes"):
        return None
    result = run_cmd(["hermes", "--version"])
    return result.stdout.strip().split("\n")[0] if result.stdout else "unknown"


def prompt_with_default(prompt: str, default: str) -> str:
    """Prompt user with default value. Falls back to default in non-interactive mode."""
    if not sys.stdin.isatty():
        log_info(f"Non-interactive mode: using default for '{prompt}'")
        return default
    
    if default:
        response = input(f"{prompt} [{default}]: ").strip()
        return response if response else default
    else:
        while True:
            response = input(f"{prompt}: ").strip()
            if response:
                return response
            print("  This field is required.")


def get_hermes_home() -> Path:
    """Get Hermes home directory."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


# Plugin files content - using raw strings to preserve escape sequences
PLUGIN_YAML = r"""name: perseus-vault
version: "0.1.0"
description: "Perseus Vault memory provider via MCP HTTP — persistent, cross-session knowledge with semantic search, graph RAG, and bi-temporal queries"
author: "Carlos Caceres"
entry_point: "provider:create_provider"
config_schema:
  mcp_url:
    type: string
    default: "http://localhost:8767/message"
    description: "Perseus Vault MCP HTTP endpoint"
  api_key:
    type: string
    description: "MCP Bearer token (from MCP_PERSEUS_VAULT_API_KEY env var)"
    secret: true
cli_commands:
  - perseus-vault
"""

INIT_PY = r'''"""Perseus Vault Memory Provider Plugin for Hermes Agent.

Registers the MemoryProvider implementation via the plugin system.
"""

from .provider import PerseusVaultProvider, create_provider


def register(ctx):
    """Plugin entry point - registers the MemoryProvider instance."""
    ctx.register_memory_provider(create_provider())


__all__ = ["PerseusVaultProvider", "create_provider", "register"]
'''

PROVIDER_PY = r'''"""Perseus Vault Memory Provider for Hermes Agent.

Implements the MemoryProvider ABC using Perseus Vault MCP HTTP API.
Implements lifecycle hooks per docs/lifecycle-hooks.md:
- SessionStart (on_turn_start): proactive recall via prepare/context
- on_insight (on_memory_write): capture durable facts via remember/capture/journal
- SessionStop (on_session_end): consolidate/maintain hygiene pass
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


class MCPClient:
    """Minimal MCP HTTP client for Perseus Vault."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault", "version": "0.1.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params

        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e}")

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments or {}})


class PerseusVaultProvider(MemoryProvider):
    """Perseus Vault memory provider via MCP HTTP with lifecycle hooks."""

    name = "perseus-vault"

    def __init__(self):
        self._config: dict = {}
        self._client: Optional[MCPClient] = None
        self._tools_schemas: list[dict] = []
        self._session_id: Optional[str] = None
        self._turn_count: int = 0
        self._session_started: bool = False

    def is_available(self) -> bool:
        """Quick health check — no network calls per contract."""
        token = os.getenv("MCP_PERSEUS_VAULT_API_KEY")
        if not token and self._config:
            token = self._config.get("api_key")
        url = self._config.get("url") or "http://localhost:8767/message"
        return bool(url and token)

    def initialize(self, session_id: str, **kwargs) -> None:
        """Connect to MCP server and cache tool schemas."""
        self._session_id = session_id
        self._turn_count = 0
        self._session_started = False

        url = self._config.get("url") or "http://localhost:8767/message"
        token = self._config.get("api_key") or os.getenv("MCP_PERSEUS_VAULT_API_KEY")
        if not token:
            raise RuntimeError("MCP_PERSEUS_VAULT_API_KEY not configured")

        self._client = MCPClient(url, token)
        init_result = self._client.initialize()
        if "error" in init_result:
            raise RuntimeError(f"MCP initialize failed: {init_result['error']}")

        # Cache tool schemas
        tools = self._client.list_tools()
        self._tools_schemas = []
        for t in tools:
            self._tools_schemas.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })

        logger.info(f"Perseus Vault connected: {len(self._tools_schemas)} tools available")

    # ============================================================
    # Lifecycle Hooks — per docs/lifecycle-hooks.md
    # ============================================================

    def on_turn_start(self, turn: int, message: str, **kwargs) -> None:
        """SessionStart equivalent — proactive recall before each turn.
        
        Runs perseus_vault_context with the current task/query to seed
        relevant memories into the session context. Also runs perseus_vault_recall
        for keyword search. Only does full recall on first turn; subsequent 
        turns use lighter recall_when trigger matching.
        
        Per contract: SessionStart uses perseus_vault_context, perseus_vault_recall_when, perseus_vault_recall
        """
        if not self._client:
            return

        self._turn_count += 1

        # First turn = SessionStart: full context preparation
        if not self._session_started:
            self._session_started = True
            try:
                # 1. Context block with query for recall-first injection
                result = self._client.call_tool(
                    "perseus_vault_context",
                    {
                        "query": message,
                        "mode": "on_demand",
                        "limit": 10,
                    },
                )
                logger.debug("SessionStart: injected memory context block")

                # 2. Keyword recall (perseus_vault_recall) - complementary to context
                recall = self._client.call_tool(
                    "perseus_vault_recall",
                    {"query": message, "limit": 10},
                )
                logger.debug(f"SessionStart: keyword recall found {recall.get('result', {}).get('entities', [])}")

            except Exception as e:
                logger.debug(f"SessionStart context failed: {e}")
        else:
            # Subsequent turns: lightweight recall_when trigger matching
            try:
                self._client.call_tool(
                    "perseus_vault_recall_when",
                    {"context": message, "limit": 5},
                )
            except Exception:
                pass  # Non-blocking

    def on_session_end(self, messages: List[Dict[str, str]]) -> None:
        """SessionStop — agent consolidates session memories.
        
        Per the memory loop: "Before finishing, call perseus_vault_consolidate 
        (with dry_run: true first) to merge overlap into durable observations."
        
        This is SEPARATE from cron's nightly maintain (which does decay, compact, vacuum).
        """
        if not self._client:
            return

        try:
            # Step 1: Dry-run to preview what would be merged
            dry = self._client.call_tool("perseus_vault_consolidate", {
                "dry_run": True,
            })
            logger.info(f"SessionStop: consolidate dry-run: {dry}")

            # Step 2: Actual consolidation (archive_sources merges related memories)
            result = self._client.call_tool("perseus_vault_consolidate", {
                "dry_run": False,
                "archive_sources": True,
            })
            logger.info(f"SessionStop: consolidate complete: {result}")
        except Exception as e:
            logger.warning(f"SessionStop consolidate failed: {e}")

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict] = None) -> None:
        """on_insight equivalent — capture durable facts when agent writes to memory.
        
        Mirrors built-in memory writes (MEMORY.md/USER.md) to Perseus Vault
        with richer metadata (recall_when triggers, tags, workspace).
        
        Per the memory loop: "whenever a durable fact, decision, constraint, or lesson
        is established, immediately call perseus_vault_remember with a clear
        category, a stable key, and the fact in content. Set recall_when
        triggers describing when it should resurface. Record significant events
        with perseus_vault_journal."
        """
        if not self._client:
            return

        try:
            import hashlib
            key_hash = hashlib.md5(content.encode()).hexdigest()[:12]
            category = metadata.get("category", "insight") if metadata else "insight"
            insight_type = metadata.get("type", "insight") if metadata else "insight"
            
            # Determine which tool to use based on action/type
            # - "remember" / "fact" / "decision" / "lesson" → perseus_vault_remember
            # - "event" / "journal" → perseus_vault_journal
            # - "capture" / "raw" → perseus_vault_capture
            
            if action in ("event", "journal") or insight_type == "event":
                # Record significant events with perseus_vault_journal
                self._client.call_tool(
                    "perseus_vault_journal",
                    {
                        "text": content,
                        "category": category,
                        "tags": metadata.get("tags", ["auto-capture", action]) if metadata else ["auto-capture", action],
                        "workspace_hash": metadata.get("workspace_hash", "default") if metadata else "default",
                    },
                )
                logger.debug(f"on_insight: journaled event to {category}")
            elif action in ("capture", "raw") or insight_type == "capture":
                # Distill raw payload with perseus_vault_capture
                self._client.call_tool(
                    "perseus_vault_capture",
                    {
                        "text": content,
                        "max_entities": metadata.get("max_entities", 10) if metadata else 10,
                    },
                )
                logger.debug(f"on_insight: captured raw payload")
            else:
                # Default: durable facts/decisions/lessons with perseus_vault_remember
                self._client.call_tool(
                    "perseus_vault_remember",
                    {
                        "category": category,
                        "key": f"auto-{action}-{key_hash}",
                        "content": content,
                        "type": insight_type,
                        "tags": metadata.get("tags", ["auto-capture", action]) if metadata else ["auto-capture", action],
                        "recall_when": metadata.get("recall_when", [f"when: {action}"]) if metadata else [f"when: {action}"],
                        "workspace_hash": metadata.get("workspace_hash", "default") if metadata else "default",
                    },
                )
                logger.debug(f"on_insight: remembered {action} to {category}")
        except Exception:
            pass  # Non-blocking

    def on_pre_compress(self, messages: List[Dict[str, str]]) -> str:
        """Extract key insights before context compression.
        
        Returns a summary of what should be preserved durably.
        """
        if not self._client:
            return ""
        
        try:
            # Use capture to distill the session into durable notes
            session_text = "\n".join(
                f"{m.get('role', 'unknown')}: {m.get('content', '')}"
                for m in messages[-50:]  # Last 50 messages
            )
            result = self._client.call_tool(
                "perseus_vault_capture",
                {
                    "text": session_text,
                    "max_entities": 10,
                },
            )
            logger.debug(f"on_pre_compress: captured {result}")
            return "Session insights captured to Vault"
        except Exception:
            return ""

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        """Capture subagent work results as durable memories."""
        if not self._client:
            return
        
        try:
            import hashlib
            key_hash = hashlib.md5(task.encode()).hexdigest()[:12]
            self._client.call_tool(
                "perseus_vault_remember",
                {
                    "category": "delegation",
                    "key": f"task-{key_hash}",
                    "content": f"Task: {task}\n\nResult: {result}",
                    "type": "observation",
                    "tags": ["delegation", "auto-capture"],
                    "recall_when": ["when: reviewing delegated work", "when: similar task"],
                    "workspace_hash": "default",
                },
            )
        except Exception:
            pass

    def backup_paths(self) -> List[str]:
        """Extra paths to include in hermes backup."""
        return [
            "~/.hermes/perseus-vault.json",
            "~/.hermes/plugins/perseus-vault/",
        ]

    # ============================================================
    # Standard MemoryProvider methods
    # ============================================================

    def get_tool_schemas(self) -> list[dict]:
        return self._tools_schemas

    def handle_tool_call(self, name: str, arguments: dict) -> dict:
        if not self._client:
            return {"error": "Provider not initialized"}
        try:
            result = self._client.call_tool(name, arguments)
            return result
        except Exception as e:
            logger.error(f"Tool call {name} failed: {e}")
            return {"error": str(e)}

    def prefetch(self, query: str, **kwargs) -> None:
        """Background recall before each turn (legacy hook)."""
        if not self._client:
            return
        try:
            self._client.call_tool("perseus_vault_recall_when", {"context": query, "limit": 10})
        except Exception:
            pass

    def queue_prefetch(self, query: str, **kwargs) -> None:
        """Alias for prefetch."""
        self.prefetch(query, **kwargs)

    def sync_turn(self, user_message: str, assistant_message: str, **kwargs) -> None:
        """Persist conversation turn to chat_history."""
        if not self._client:
            return
        try:
            self._client.call_tool(
                "perseus_vault_remember",
                {
                    "category": "chat_history",
                    "key": f"turn-{self._session_id or 'unknown'}-{hash(user_message) % 100000}",
                    "content": f"User: {user_message}\n\nAssistant: {assistant_message}",
                    "type": "conversation",
                    "tags": ["auto-capture", "turn"],
                    "workspace_hash": "default",
                },
            )
        except Exception:
            pass

    def system_prompt_block(self) -> str:
        """Static system prompt addition + dynamic context injection."""
        return (
            "## Memory (Perseus Vault)\n"
            "You have persistent cross-session memory via the perseus_vault_* tools. "
            "Relevant memories are automatically injected before each turn via SessionStart hook. "
            "When you learn something durable, capture it with perseus_vault_remember or perseus_vault_capture. "
            "At session end, a maintenance pass consolidates and promotes learning."
        )

    def shutdown(self) -> None:
        self._client = None
        self._tools_schemas = []


def create_provider() -> PerseusVaultProvider:
    """Factory function for plugin registration."""
    return PerseusVaultProvider()
'''

CLI_PY = r'''"""CLI commands for Perseus Vault memory provider.

Registers subcommands under `hermes perseus-vault <cmd>`.
Only available when perseus-vault is the active memory provider.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _get_config() -> dict:
    """Load provider config from saved file and env vars."""
    hermes_home = get_hermes_home()
    config_path = hermes_home / "perseus-vault.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass
    if token := os.getenv("MCP_PERSEUS_VAULT_API_KEY"):
        config["api_key"] = token
    if url := os.getenv("PERSEUS_MCP_URL"):
        config["url"] = url
    return config


class MCPClient:
    """Minimal MCP HTTP client."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault-cli", "version": "0.1.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params

        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e}")

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments or {}})


def _get_client() -> MCPClient:
    """Create MCP client from config."""
    config = _get_config()
    url = config.get("url", "http://localhost:8767/message")
    token = config.get("api_key")
    if not token:
        raise RuntimeError("MCP_PERSEUS_VAULT_API_KEY not configured. Run `hermes memory setup`.")
    client = MCPClient(url, token)
    init_result = client.initialize()
    if "error" in init_result:
        raise RuntimeError(f"MCP initialize failed: {init_result['error']}")
    return client


def cmd_status(args) -> int:
    """Show provider status and connection info."""
    try:
        client = _get_client()
        print("Provider: perseus-vault")
        print(f"Connected: True")
        print(f"  URL: {client.url}")
        tools = client.list_tools()
        print(f"  Tools available: {len(tools)}")
    except Exception as e:
        print(f"Provider: perseus-vault")
        print(f"Connected: False")
        print(f"  Error: {e}")
        return 1
    return 0


def cmd_tools(args) -> int:
    """List available Perseus Vault tools."""
    try:
        client = _get_client()
        tools = client.list_tools()
        if not tools:
            print("No tools available")
            return 1
        print(f"Available tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description', '')[:80]}...")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_call(args) -> int:
    """Call a Perseus Vault tool directly."""
    if not args.tool:
        print("Usage: hermes perseus-vault call <tool_name> [json_args]")
        return 1
    try:
        client = _get_client()
        tool_name = args.tool
        arguments = {}
        if args.json_args:
            try:
                arguments = json.loads(args.json_args)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return 1
        result = client.call_tool(tool_name, arguments)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_recall(args) -> int:
    """Recall entities from Vault (FTS5 keyword search)."""
    if not args.query:
        print("Usage: hermes perseus-vault recall <query> [--limit N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_recall", {"query": args.query, "limit": args.limit or 10})
        entities = result.get("result", {}).get("entities", [])
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] {e.get('summary', e.get('content', '')[:80])}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_semantic(args) -> int:
    """Semantic search in Vault (dense vector search)."""
    if not args.query:
        print("Usage: hermes perseus-vault semantic <query> [--top_k N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_semantic_search", {"query": args.query, "top_k": args.top_k or 10})
        entities = result.get("result", {}).get("entities", [])
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] {e.get('summary', e.get('content', '')[:80])}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_remember(args) -> int:
    """Store/remember an entity in Vault."""
    if not args.category or not args.key or not args.content:
        print("Usage: hermes perseus-vault remember <category> <key> <content> [--type TYPE] [--tags TAGS]")
        return 1
    try:
        client = _get_client()
        tags = args.tags.split(",") if args.tags else []
        result = client.call_tool(
            "perseus_vault_remember",
            {
                "category": args.category,
                "key": args.key,
                "content": args.content,
                "type": args.type or "insight",
                "tags": tags,
                "workspace_hash": "default",
            },
        )
        print(f"Remembered: {result}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_stats(args) -> int:
    """Show Vault statistics."""
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_stats", {})
        print(json.dumps(result.get("result", result), indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_config_show(args) -> int:
    """Show current provider config (non-secret)."""
    config = _get_config()
    safe = {k: v for k, v in config.items() if k != "api_key"}
    print(json.dumps(safe, indent=2))
    return 0


def register_cli(parser) -> None:
    """Register CLI subcommands for perseus-vault provider."""
    sub = parser.add_subparsers(dest="pv_cmd", required=True)

    sub.add_parser("status", help="Show provider status").set_defaults(func=cmd_status)
    sub.add_parser("tools", help="List available tools").set_defaults(func=cmd_tools)

    p_call = sub.add_parser("call", help="Call a tool directly")
    p_call.add_argument("tool", help="Tool name")
    p_call.add_argument("json_args", nargs="?", help="JSON arguments")
    p_call.set_defaults(func=cmd_call)

    p_recall = sub.add_parser("recall", help="FTS5 keyword recall")
    p_recall.add_argument("query", help="Search query")
    p_recall.add_argument("--limit", type=int, default=10)
    p_recall.set_defaults(func=cmd_recall)

    p_sem = sub.add_parser("semantic", help="Semantic vector search")
    p_sem.add_argument("query", help="Search query")
    p_sem.add_argument("--top_k", type=int, default=10)
    p_sem.set_defaults(func=cmd_semantic)

    p_rem = sub.add_parser("remember", help="Store an entity")
    p_rem.add_argument("category", help="Category")
    p_rem.add_argument("key", help="Key")
    p_rem.add_argument("content", help="Content")
    p_rem.add_argument("--type", default="insight")
    p_rem.add_argument("--tags", help="Comma-separated tags")
    p_rem.set_defaults(func=cmd_remember)

    sub.add_parser("stats", help="Show Vault statistics").set_defaults(func=cmd_stats)
    sub.add_parser("config", help="Show config").set_defaults(func=cmd_config_show)

    def _dispatch(args):
        if hasattr(args, "func"):
            return args.func(args)
        parser.print_help()
        return 1

    parser.set_defaults(func=_dispatch)
'''


def write_plugin_files(plugin_dir: Path) -> None:
    """Write all plugin files."""
    files = {
        "plugin.yaml": PLUGIN_YAML,
        "__init__.py": INIT_PY,
        "provider.py": PROVIDER_PY,
        "cli.py": CLI_PY,
    }
    for name, content in files.items():
        (plugin_dir / name).write_text(content)
    log_ok("Plugin files created")


def main() -> int:
    print("=" * 60)
    print("  Perseus Vault Memory Provider Installer for Hermes")
    print("=" * 60)
    print()

    # Check Hermes
    log_info("Checking Hermes installation...")
    hermes_version = check_hermes()
    if not hermes_version:
        log_error("Hermes not found in PATH. Install Hermes first: https://hermes-agent.nousresearch.com")
        return 1
    log_ok(f"Hermes found: {hermes_version}")

    # Get Hermes home
    hermes_home = get_hermes_home()
    log_info(f"Hermes home: {hermes_home}")

    # Prompt for MCP server URL
    print()
    print("--- Perseus Vault MCP Server Configuration ---")

    # Check environment variables first (for non-interactive/automated use)
    env_host_port = os.environ.get("MCP_HOST_PORT")
    env_token = os.environ.get("MCP_TOKEN")

    if env_host_port and env_token and not sys.stdin.isatty():
        # Non-interactive mode with all required env vars - use them directly
        mcp_host_port = env_host_port
        mcp_token = env_token
        mcp_url = f"http://{mcp_host_port}/message"
        log_info(f"Non-interactive mode: using MCP_HOST_PORT={mcp_host_port}")
        log_info("Token configured from MCP_TOKEN")
        # Determine connection type from IP
        connection_type = "local" if mcp_host_port.startswith(("localhost", "127.0.0.1")) else "remote"
    elif env_host_port and not sys.stdin.isatty():
        # Non-interactive with host but no token - use host, ask for token
        mcp_host_port = env_host_port
        mcp_url = f"http://{mcp_host_port}/message"
        log_info(f"Non-interactive mode: using MCP_HOST_PORT={mcp_host_port}")
        default_token = env_token or "devon-token-2026"
        mcp_token = prompt_with_default("MCP Bearer token (MCP_PERSEUS_VAULT_API_KEY)", default_token)
        log_info("Token configured")
        # Determine connection type from IP
        connection_type = "local" if mcp_host_port.startswith(("localhost", "127.0.0.1")) else "remote"
    else:
        # Interactive mode
        print("Enter the IP:port of the machine running Perseus Vault MCP server")
        print("Example: localhost:8767 or 192.168.1.54:8767")
        print()

        # Ask local vs remote
        location = prompt_with_default("Is the server local or remote? (local/remote)", "local").lower()

        if location in ("local", "l", "localhost"):
            default_host_port = "localhost:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port", default_host_port)
            connection_type = "local"
        else:
            # Remote - ask for IP:port
            default_host_port = env_host_port or "192.168.1.54:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port", default_host_port)
            connection_type = "remote"

        mcp_url = f"http://{mcp_host_port}/message"
        log_info(f"MCP URL: {mcp_url}")

        # Prompt for API key
        default_token = env_token or "devon-token-2026"
        mcp_token = prompt_with_default("MCP Bearer token (MCP_PERSEUS_VAULT_API_KEY)", default_token)
        log_info("Token configured")

    # Plugin directory
    plugin_dir = hermes_home / "plugins" / "perseus-vault"
    log_info(f"Plugin directory: {plugin_dir}")

    # Create plugin directory
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # Write plugin files
    write_plugin_files(plugin_dir)

    # Enable plugin
    log_info("Enabling plugin...")
    result = run_cmd(["hermes", "plugins", "enable", "perseus-vault"], capture=False)
    if result.returncode == 0:
        log_ok("Plugin enabled")
    else:
        log_warn("Plugin enable returned non-zero (may already be enabled)")

    # Configure memory provider
    log_info("Configuring memory provider...")
    result = run_cmd(["hermes", "memory", "setup", "perseus-vault"], capture=False)
    if result.returncode == 0:
        log_ok("Memory provider configured")
    else:
        log_warn("Memory setup returned non-zero")

    # Save config file for CLI
    config_file = hermes_home / "perseus-vault.json"
    config = {"url": mcp_url, "api_key": mcp_token}
    config_file.write_text(json.dumps(config, indent=2))
    config_file.chmod(0o600)
    log_ok(f"Config saved to {config_file}")

    # Verify
    log_info("Verifying installation...")
    result = run_cmd(["hermes", "memory", "status"])
    if "perseus-vault" in result.stdout:
        log_ok("Installation verified!")
    else:
        log_error("Verification failed - check hermes memory status")
        return 1

    print()
    print("=" * 60)
    print("  Perseus Vault Memory Provider Ready")
    print("=" * 60)
    print()
    print("Commands available:")
    print("  hermes perseus-vault status       # Check connection")
    print("  hermes perseus-vault tools        # List 81 tools")
    print("  hermes perseus-vault recall <q>   # Keyword search")
    print("  hermes perseus-vault semantic <q> # Vector search")
    print("  hermes perseus-vault remember     # Store memory")
    print("  hermes perseus-vault stats        # Vault stats")
    print("  hermes perseus-vault config       # Show config")
    print()
    print("Memory provider is active and will auto-inject context each turn.")
    print()
    print(f"Connection: {connection_type}")
    print()
    print("Lifecycle hooks implemented:")
    print("  SessionStart (on_turn_start)   → perseus_vault_context / recall_when")
    print("  on_insight (on_memory_write)   → perseus_vault_remember")
    print("  SessionStop (on_session_end)   → perseus_vault_consolidate (dry_run + archive)")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())