# Research & Documentation Workflow Specification

**Document Version:** 1.0.0  
**Target Project:** ARES (Adaptive Reversal & Entry Signal)  
**Applicability:** All Multica Agents, Autonomous Coding Workflows, and Engineering Contributors

---

## 1. Executive Summary

This specification standardizes the end-to-end lifecycle for taking technical ideas from initial conversation or hypothesis to production code and synchronized documentation. It establishes clear protocols for:
1. **Technical Spikes & Architecture Research**
2. **PRD Generation & Alignment**
3. **Tracer-Bullet Vertical Slice Backlog Decomposition**
4. **Implementation & Test-Driven Development (TDD)**
5. **Continuous Documentation & Obsidian Knowledge Synchronization**

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ 1. Tech Spike   │ ──▶ │ 2. Architecture │ ──▶ │ 3. PRD Authoring│
│ & Exploration   │     │ Decision (ADR)  │     │ (`to-prd`)      │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ 6. Doc & Vault  │ ◀── │ 5. Code & TDD   │ ◀── │ 4. Vertical     │
│ Sync (`release`)│     │ Implementation  │     │ Slice Issues    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

---

## 2. End-to-End Workflow Phases

### Phase 1: Technical Spikes & Architectural Exploration
- **Trigger**: New algorithmic concepts, unfamiliar external APIs, performance bottlenecks, or trade-off decisions.
- **Responsible Agents/Skills**: `research-creativity-agent`, `problem-understanding-agent`.
- **Workflow**:
  1. Define research hypotheses and performance metrics.
  2. Implement lightweight prototypes or benchmarks under `execution/` or scratch environment.
  3. Evaluate trade-offs (complexity, latency, reliability, maintenance).
  4. Document findings using [`directives/templates/RESEARCH_SPIKE_TEMPLATE.md`](file:///directives/templates/RESEARCH_SPIKE_TEMPLATE.md).
- **Exit Criteria**: A completed spike document with a clear go/no-go recommendation and benchmark data.

### Phase 2: Architecture Decision Records (ADRs)
- **Trigger**: System design changes, schema migrations, new detector logic, or interface modifications.
- **Responsible Agents/Skills**: `architect-agent`, `grill-with-docs`.
- **Workflow**:
  1. Author a formal ADR using [`directives/templates/ARCHITECTURE_ADR_TEMPLATE.md`](file:///directives/templates/ARCHITECTURE_ADR_TEMPLATE.md).
  2. Map changes against the Seven-Layer Modular Architecture.
  3. Detail exact dataclass models, Pydantic configuration fields, and external API mocking seams.
  4. Register the ADR in [`directives/adr/INDEX.md`](file:///directives/adr/INDEX.md).
- **Exit Criteria**: Peer/human approval of the ADR before writing production code.

### Phase 3: PRD Authoring & Spec Synthesis
- **Trigger**: Feature initiatives spanning multiple functional capabilities.
- **Responsible Agents/Skills**: `to-prd`, `pm-agent`.
- **Workflow**:
  1. Synthesize conversational findings and ADR architectural contracts into a PRD.
  2. Apply [`directives/templates/PRD_TEMPLATE.md`](file:///directives/templates/PRD_TEMPLATE.md).
  3. Include an exhaustive numbered list of user stories (`As a <role>, I want <feature>, so that <benefit>`).
  4. Explicitly document testing decisions, quality seams, and out-of-scope items.
- **Exit Criteria**: A finalized PRD attached to the project roadmap and ready for backlog decomposition.

### Phase 4: Vertical Slice Issue Decomposition (Tracer Bullets)
- **Trigger**: An approved PRD or complex task ready for implementation.
- **Responsible Agents/Skills**: `to-issues`, `pm-agent`, `multica-working-on-issues`.
- **Workflow**:
  1. Decompose the PRD into narrow, end-to-end **tracer bullet** vertical slices rather than horizontal architectural layers.
  2. Ensure each slice cuts through all necessary layers (Model/Schema -> Business Logic -> Persistence/Alerts -> Unit Tests).
  3. Classify slices as `AFK` (autonomous execution) or `HITL` (human interaction/approval).
  4. Author issue descriptions using [`directives/templates/VERTICAL_SLICE_ISSUE_TEMPLATE.md`](file:///directives/templates/VERTICAL_SLICE_ISSUE_TEMPLATE.md).
  5. Publish issues to the Multica issue tracker using `multica issue create`.
- **Exit Criteria**: Dependency-ordered backlog tickets with unambiguous acceptance criteria and verification commands.

### Phase 5: Implementation & Quality Assurance
- **Trigger**: An assigned vertical slice issue.
- **Responsible Agents/Skills**: `code-generator-agent`, `qa-agent`, `diagnose`.
- **Workflow**:
  1. Branch out: `git checkout -b feature/[TASK_ID]-[description]`.
  2. Apply TDD: Write failing unit tests in `tests/test_*.py` first.
  3. Implement minimal clean code to pass tests following zero-hardcoding and stateless design principles.
  4. Run full test suite regression: `pytest`.
- **Exit Criteria**: 100% unit test coverage for new code and all regression tests passing.

### Phase 6: Code Release & Continuous Documentation Synchronization
- **Trigger**: Completed implementation ready for PR submission.
- **Responsible Agents/Skills**: `documentation-agent`, `obsidian-vault`, `obsidian-markdown`.
- **Workflow**:
  1. Complete the release checklist using [`directives/templates/RELEASE_DOC_SYNC_TEMPLATE.md`](file:///directives/templates/RELEASE_DOC_SYNC_TEMPLATE.md).
  2. Update in-repository docs:
     - `CHANGELOG.md`: Human-readable summary under `[Unreleased]` or version tag.
     - `README.md`: Update architecture or setup steps.
     - `directives/api-docs/`: Sync domain model interfaces.
     - `.env.example`: Add any new config variables.
  3. Synchronize Obsidian Knowledge Vault (`~/Documents/Obsidian/Projects/Ares/`):
     - Update component notes (`Architecture.md`, `Engine.md`, `Detectors.md`, `Configuration.md`, etc.).
     - Write task execution log: `TASK-XXX-log.md`.
  4. Commit changes: `feat([TASK_ID]): [Detailed Description]`.
  5. Push and raise Pull Request:
     ```bash
     gh pr create --title "feat([TASK_ID]): [Detailed Description]" -F temp_pr_body.md
     ```
  6. Update issue status: `multica issue status [ID] in_review`.
- **Exit Criteria**: PR raised with full test and doc-sync evidence; issue transitioned to `in_review`.

---

## 3. Skill & Agent Capability Matrix

| Capability / Workflow Step | Primary Skills | Secondary Skills | Output Deliverable |
|---|---|---|---|
| **Technical Spikes** | `research-creativity-agent` | `problem-understanding-agent` | `directives/templates/RESEARCH_SPIKE_TEMPLATE.md` |
| **Architectural Decisions** | `architect-agent` | `grill-with-docs` | `directives/adr/TASK-XXX_*.md` |
| **PRD Generation** | `to-prd` | `pm-agent` | Standard PRD Document |
| **Backlog Decomposition** | `to-issues` | `multica-working-on-issues` | Multica Vertical Slice Issues |
| **Implementation & TDD** | `code-generator-agent` | `diagnose` | Clean Code + Unit Tests |
| **QA Verification** | `qa-agent` | `qa` | Test Execution & Coverage Report |
| **Doc & Obsidian Sync** | `documentation-agent` | `obsidian-vault`, `obsidian-markdown` | `CHANGELOG.md`, `README.md`, Vault Notes |

---

## 4. Standard Templates Reference

All workflow templates are located in [`directives/templates/`](file:///directives/templates/):

1. **[`RESEARCH_SPIKE_TEMPLATE.md`](file:///directives/templates/RESEARCH_SPIKE_TEMPLATE.md)**: Technical spikes, benchmark evaluations, and feasibility studies.
2. **[`ARCHITECTURE_ADR_TEMPLATE.md`](file:///directives/templates/ARCHITECTURE_ADR_TEMPLATE.md)**: Architecture Decision Records (ADRs) with system layer mapping.
3. **[`PRD_TEMPLATE.md`](file:///directives/templates/PRD_TEMPLATE.md)**: Standard Product Requirements Documents connecting user stories to technical boundaries.
4. **[`VERTICAL_SLICE_ISSUE_TEMPLATE.md`](file:///directives/templates/VERTICAL_SLICE_ISSUE_TEMPLATE.md)**: Tracer-bullet backlog tickets for autonomous and human-guided execution.
5. **[`RELEASE_DOC_SYNC_TEMPLATE.md`](file:///directives/templates/RELEASE_DOC_SYNC_TEMPLATE.md)**: Release synchronization checklist between code, repository docs, and Obsidian vault.

---

## 5. Verification & Compliance Checklist

Before declaring any feature or workflow milestone complete, verify:
- [x] Documentation templates created and validated.
- [x] Standard PRD format and vertical-slice decomposition pattern established.
- [x] Bidirectional synchronization rules defined for Repository Docs and Obsidian Vault.
- [x] Git PR gate rules strictly observed (no direct commits to `main`, PR created via `gh pr create`).
