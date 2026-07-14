# Mimir Integration Examples

## Quickstart
`quickstart.py` — 60-second demo: remember, recall, forget, vault export.

## CrewAI Integration
`crewai_integration.py` — Use Mimir as persistent memory for CrewAI crews.
Stores conversation history and user preferences, recalls context across crew kickoffs.

## Google ADK Integration
`adk_integration.py` — Implements `BaseMemoryService` for Google Agent Development Kit.
Local-first, encrypted, zero cloud dependencies — complementing ADK's InMemory and Vertex AI backends.

## Running

```bash
# Install the Perseus Vault binary:
curl -sSf https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/install.sh | sh
# Then install the example dependencies as needed:
pip install crewai google-adk
python examples/quickstart.py
```
