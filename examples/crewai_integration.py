"""
Perseus Vault + CrewAI Integration Example

Shows how to use Perseus Vault as persistent memory for a CrewAI crew.
Requires: pip install crewai perseus_vault
"""
import os
import json
import subprocess
from crewai import Agent, Task, Crew, Process

# Start Perseus Vault in the background
perseus_vault_process = subprocess.Popen(
    ["perseus-vault", "--db", "./crew_memory.db"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# Connect to Perseus Vault via MCP
# CrewAI supports MCP tools natively — just add the server config
mcp_config = {
    "perseus-vault": {
        "command": "perseus-vault",
        "args": ["--db", "./crew_memory.db"],
        "transport": "stdio",
    }
}

# Or use the Python client directly
from perseus_vault import PerseusVaultClient
client = PerseusVaultClient(db_path="./crew_memory.db")

# Store a memory
client.remember(
    content="The user prefers concise responses without emojis.",
    category="user-preference",
    metadata={"user_id": "alice", "importance": 0.9},
)

# Recall relevant memories for context
memories = client.recall("user preferences", limit=5)
context = "\n".join(m.content for m in memories)

# Create a CrewAI agent with Perseus Vault-backed context
agent = Agent(
    role="Assistant",
    goal="Help the user with their tasks",
    backstory=f"Previous context: {context}",
    llm="gpt-4o-mini",
)

task = Task(
    description="Respond to the user's query conversationally",
    expected_output="A helpful response",
    agent=agent,
)

crew = Crew(agents=[agent], tasks=[task], process=Process.sequential)
result = crew.kickoff(inputs={"query": "Hello! How can you help me today?"})

print(f"Agent response: {result}")

# Store this interaction for future recall
client.remember(
    content=f"User asked: Hello! Agent responded: {result}",
    category="conversation",
    metadata={"session_id": "demo-001"},
)

perseus_vault_process.terminate()
