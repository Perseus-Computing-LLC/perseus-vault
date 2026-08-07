# Context Engineering and AI Memory Systems Research Memo

_Date: August 6, 2026_

## Executive summary

Recent work on context engineering and AI memory systems points to a clear architectural pattern:

- **Context engineering** is becoming a discipline of compiling structured, task-scoped, machine-legible working context instead of stuffing more prose into prompts.
- **Memory systems** are moving beyond vector retrieval toward governed memory state with ingestion, revision, forgetting, temporal validity, provenance, and evidence-preserving episodic storage.
- **Action systems** increasingly need explicit previews, stage-aware execution traces, uncertainty signaling, and durable receipts.

This direction aligns strongly with the existing decomposition across **Perseus**, **Perseus Vault**, and **Ledger**:

- **Perseus** as a context compiler
- **Vault** as an evolving memory runtime
- **Ledger** as a governed action and audit substrate

The research does not suggest a pivot away from the current architecture. It suggests making those boundaries more explicit and measurable.

## Sources reviewed

### Official / primary protocol and platform sources
1. [Model Context Protocol specification](https://modelcontextprotocol.io/specification/latest): standardized protocol for tools, resources, and structured interaction between AI applications and
   external systems.
2. [Atlassian Rovo MCP Server announcement](https://www.atlassian.com/blog/announcements/atlassian-rovo-mcp-ga) and [Rovo MCP overview](https://www.atlassian.com/platform/rovo-mcp),
   reviewed as the primary Atlassian context-engineering/Rovo materials.

### Research sources on agent memory
3. [Is Agent Memory a Database? Rethinking Data Foundations for Long-Term AI Agent Memory](https://arxiv.org/abs/2605.26252)
4. [Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management for Large Language Model Agents (AgeMem)](https://arxiv.org/abs/2601.01885)
5. [MemMachine: A Ground-Truth-Preserving Memory System for Personalized AI Agents](https://arxiv.org/abs/2604.04853)
6. [RaMem: Contextual Reinstatement for Long-term Agentic Memory](https://arxiv.org/abs/2606.22844)
7. [MEMENTO: Teaching LLMs to Manage Their Own Context](https://arxiv.org/abs/2604.09852)

## Major findings

### 1. Context should be compiled, not merely assembled
The strongest trend is a move from prompt accumulation to **structured context packaging**. Systems increasingly benefit when context is:

- task-scoped
- machine-legible
- provenance-aware
- token-efficient
- explicit about intent, constraints, and action boundaries

This reinforces the case for Perseus to evolve from a context assembler into a **context compiler with optimization passes**.

### 2. Compaction is a first-class capability
Memento is especially important because it frames context management as controlled compaction, not just retrieval. Compact intermediate artifacts can preserve what future reasoning needs while materially
reducing context cost.

This suggests a product pattern of distinct artifact tiers:

- full context
- task projection
- compact memento projection
- execution preview

### 3. Memory is a state machine, not just a search index
The most important conceptual contribution from the memory literature is the argument that long-term agent memory should be modeled through state-level operators:

- ingestion
- revision
- forgetting
- retrieval

This maps cleanly onto Vault’s strengths in supersession, history, decay, and temporal lookup, but suggests making those semantics more first-class in product language and MCP/API design.

### 4. Episodic evidence should be preserved longer
MemMachine and RaMem both emphasize preserving raw or lightly processed episodes so later retrieval and synthesis can stay anchored to the underlying truth conditions.

This strengthens the case for:

- durable episode storage
- better excerpt-level provenance
- temporal observation metadata
- derivation links from summaries back to original evidence

### 5. Retrieval should be validity-aware, not just semantically similar
RaMem’s framing is especially useful here. The best memory systems do not merely retrieve similar content. They retrieve memories that are valid for the current question, time frame, entity scope,
and workflow.

Useful retrieval factors include:

- freshness
- scope match
- provenance and trust class
- supersession state
- temporal validity
- recall conditions

### 6. Short-term and long-term memory should be jointly managed
AgeMem argues for integrating short-term and long-term memory decisions into one control policy. In practice, that means a stronger loop between context assembly and durable memory management.

This suggests making the promotion and relegation path across Perseus and Vault much more explicit and measurable.

### 7. Protocolized interfaces matter
The MCP specification matters because it provides a stable, typed interface for tools, resources, and structured interaction. That supports the move away from brittle bespoke integrations and toward
predictable agent behavior.

## Implications by repo

## Perseus

### What the research suggests
Perseus should own the compilation of the active working set for a task.

### Recommended emphasis
- first-class structured context artifacts
- compact derivative context layers
- uncertainty and missing-data surfacing
- validity-aware context ranking and pruning
- benchmark harnesses comparing context strategies

### Product direction
Perseus should become a **context compiler with optimization passes**, not just a context renderer.

## Vault

### What the research suggests
Vault should explicitly model memory as evolving governed state.

### Recommended emphasis
- state-oriented memory lifecycle surfaces
- better episodic evidence anchoring
- validity-aware retrieval and projection
- clearer separation between live references, recalled memory, and derived task projections
- correctness metrics like contradiction rate, stale recall rate, and provenance completeness

### Product direction
Vault should become a **governed evolving memory runtime**, not just a memory database.

## Ledger

### What the research suggests
Ledger should serve as the collaboration-safe action substrate where plans, approvals, execution, and uncertainty become durable receipts.

### Recommended emphasis
- stage-aware action traces
- source-context hashes and policy hashes
- explicit uncertainty/risk fields
- durable human intercept receipts
- replayable execution explanations

### Product direction
Ledger should become a **collaborative execution control plane**.

## Recommended next-step concepts

### Highest-priority design patterns to adopt
1. **Task-scoped structured context packets** in Perseus
2. **Task-scoped projections** in Vault that separate live truth, recalled memory, and derived inferences
3. **State-level memory lifecycle framing** in Vault APIs and docs
4. **Stage-aware execution receipts** in Ledger
5. **Compaction artifacts** that preserve future-use signal at lower token cost
6. **Validity-aware ranking** over simple similarity-heavy recall
7. **Evidence-preserving summary derivation** rather than premature compression of raw episodes

## Research-backed principles

- Context should be structured enough for machine use, not just human readability.
- Memory correctness depends on revision and forgetting, not retrieval alone.
- Temporal and scope validity matter as much as semantic similarity.
- Raw evidence should remain recoverable.
- Compact derivative artifacts are valuable products in their own right.
- Trust improves when uncertainty and action boundaries are explicit.

## Bottom line

The research supports a coherent architecture:

- **Perseus** compiles and optimizes working context
- **Vault** governs evolving memory state
- **Ledger** governs inspectable action execution

The next generation of improvements should make compaction, validity, provenance, and stage-awareness first-class across those boundaries.

## Memory benchmarks and evaluation methods

A third research pass focused on how long-term memory systems are actually evaluated suggests that memory quality cannot be captured by one retrieval score alone.

### Key benchmark themes

Recent benchmarks increasingly measure different aspects of memory quality:

- **Memory organization and structure** rather than raw fact recall alone
- **Temporal validity** and whether an agent respects updates and contradictions over time
- **Prospective memory**, meaning whether the agent remembers to do something later
- **Memory utilization in tool use**, not just whether the memory was stored or retrieved
- **Multi-session dependency handling**, where one task depends on what was learned in earlier sessions
- **Context collapse resilience**, especially across long interaction horizons

### Important benchmark families

- [StructMemEval](https://arxiv.org/abs/2602.11243) evaluates whether an agent can organize long-term memory, not just retrieve a fact.
- [MemGround](https://arxiv.org/html/2604.14158v1) focuses on long-term memory under rich interactive scenarios and tests temporal association and reasoning over accumulated evidence.
- [PM-Bench](https://arxiv.org/abs/2607.12385) focuses on prospective memory and delayed obligations.
- [MemoryAgentBench](https://arxiv.org/abs/2507.05257) evaluates accurate retrieval, test-time learning, long-range understanding, and selective forgetting in incremental multi-turn settings.
- [MemoryArena](https://arxiv.org/abs/2602.16313) benchmarks interdependent multi-session agentic tasks where memory must guide later action.
- [Mem2ActBench](https://arxiv.org/abs/2601.19935) tests whether long-term memory actually grounds tool-parameter selection in interrupted task histories.

### Evaluation implications for Perseus, Vault, and Ledger

#### For Perseus
Perseus should be evaluated not only on recall utility, but on:
- token cost per successful task
- robustness of compact context artifacts
- sensitivity to evidence position in history
- whether structured projections reduce context collapse

#### For Vault
Vault should be evaluated not only on retrieval accuracy, but on:
- stale-recall rate
- contradiction handling
- temporal-validity adherence
- scope-validity adherence
- memory mutation correctness after updates or supersession
- evidence recoverability from derived summaries

#### For Ledger
Ledger should be evaluated not only on whether an action ran, but on:
- whether the action used the right memory/context basis
- whether the action could be reconstructed from receipts
- whether approval or intercept points captured the relevant uncertainty
- whether delayed or prospective obligations were successfully carried forward

### Recommended evaluation categories

A mature internal evaluation suite should measure at least these categories:

1. **Retrieval accuracy**
2. **Temporal validity**
3. **Contextual scope validity**
4. **Memory update / mutation correctness**
5. **Prospective memory**
6. **Tool grounding from memory**
7. **Context compaction quality**
8. **Evidence-preserving synthesis**
9. **Action reconstruction fidelity**

### Bottom line

The benchmark literature reinforces that memory systems should be judged as end-to-end reasoning and action substrates, not just search layers. The best next step is not one more retrieval metric. It is
a small internal evaluation harness that spans retrieval, validity, compaction, mutation, and action grounding.

## Follow-up implementation issues

- Perseus: [#923](https://github.com/Perseus-Computing-LLC/perseus/issues/923) and [#924](https://github.com/Perseus-Computing-LLC/perseus/issues/924)
- Perseus Vault: [#859](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/859) and [#860](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/860)
- Ledger: [#219](https://github.com/Perseus-Computing-LLC/ledger/issues/219) and [#220](https://github.com/Perseus-Computing-LLC/ledger/issues/220)
