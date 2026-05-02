# orchestrating-long-tasks

A [Claude Code](https://claude.ai/code) skill for orchestrating tasks that span multiple context windows or require consistent architecture decisions across multiple agent sessions.

## The Problem

Large tasks fail when context is lost between sessions. The next agent starts from scratch, makes inconsistent decisions, and repeats already-answered questions. Architecture choices made in session 1 are invisible to the agent in session 5.

## The Solution

**Docs-as-manual architecture:** structured domain documents let any AI agent resume work from any point without relying on conversation history.

> Documents are memory. Code is truth. Documents point to code — never copy implementation.

## When to Use

Use this skill when ANY of these apply:

- Task requires more than one context window (3+ work packages, 4+ hours)
- Architecture decisions must stay consistent across sessions
- Work may be handed off to a completely fresh agent mid-task
- Task spans 2+ independent domains (infrastructure, business logic, security, algorithms)

## Four-Phase Workflow

| Phase | Input | Output | Gate |
|-------|-------|--------|------|
| Discovery | User description | `_INDEX.md` discovery section | User approves findings |
| Documentation | Discovery record | Full L2 doc structure + work packages | User approves doc structure |
| Execution | L2 docs + codebase | Implementation + updated code pointers | Each WP documented before marked done |
| Review | Code + all L2 docs | `review-log.md` | All blockers resolved |

Each phase can run in an independent context window. Agents resume by loading documents, not conversation history.

## Installation

### Claude Code (Personal Skills)

```bash
# macOS / Linux
cp -r orchestrating-long-tasks ~/.claude/skills/

# Windows
xcopy /E /I orchestrating-long-tasks %USERPROFILE%\.claude\skills\orchestrating-long-tasks
```

### Plugin Installation

Follow the [agentskills.io specification](https://agentskills.io/specification) for plugin-based deployment.

## Files

| File | Purpose |
|------|---------|
| [`SKILL.md`](SKILL.md) | Main skill definition — four-phase workflow, quick reference, common mistakes |
| [`doc-architecture.md`](doc-architecture.md) | L1/L2 document templates with code pointer conventions |
| [`expert-roles.md`](expert-roles.md) | Multi-expert review protocol and severity levels |

## Two-Layer Document Structure

When the skill is active, it creates this structure under your project:

```
docs/tasks/{task-id}/
  _INDEX.md           ← L1: master doc (≤150 lines, always load first)
  tech-arch.md        ← L2: architecture, interfaces, infrastructure
  business-logic.md   ← L2: business rules, invariants, user journeys
  solution-detail.md  ← L2: file structure, components, configuration
  algorithm.md        ← L2: core algorithms (omit if not needed)
  security.md         ← L2: auth model, threat matrix, data classification
  review-log.md       ← L2: review findings and resolution checklist
```

**L1 is the recovery node.** Any agent can resume any task in under 2 minutes by reading `_INDEX.md` first.

## License

MIT
