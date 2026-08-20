---
task_id: "TASK-XXX"
date: "YYYY-MM-DD"
status: "draft | completed | rejected"
author: "Agent/Author Name"
objective: "Brief 1-line objective of this technical spike"
---

# Technical Spike & Research: [TASK-XXX] [Spike Title]

## 1. Context & Problem Statement
Describe the unknown, architectural uncertainty, or research question being investigated.
- What problem are we solving?
- Why is a spike needed before writing code or an ADR?
- What are the core assumptions to validate?

## 2. Exploration Angles & Alternatives
List the options or technologies evaluated during this spike.

### Option A: [Name]
- **Mechanism**: How it works.
- **Dependencies**: Required libraries or external services.
- **Integration Seam**: Where it connects into the existing codebase.

### Option B: [Name]
- **Mechanism**: How it works.
- **Dependencies**: Required libraries or external services.
- **Integration Seam**: Where it connects into the existing codebase.

## 3. Empirical Findings & Benchmarks
Include hard data, reproducible benchmarks, and prototype outcomes.

### Benchmark / Test Setup
```bash
# Command used to run prototype or benchmark
python -m execution.spike_prototype --samples 1000
```

### Results Table
| Metric | Option A | Option B | Baseline / Target | Notes |
|---|---|---|---|---|
| Latency (p95) | X ms | Y ms | < Z ms | |
| Memory Footprint | X MB | Y MB | < Z MB | |
| Accuracy / Precision | X% | Y% | > Z% | |
| Error Rate | X% | Y% | 0.0% | |

### Key Observations
1. Observation 1 with empirical evidence.
2. Observation 2 with empirical evidence.

## 4. Trade-Off Analysis
| Decision Dimension | Option A | Option B | Winner |
|---|---|---|---|
| Implementation Effort | Low / Med / High | Low / Med / High | Option X |
| Operational Complexity | Low / Med / High | Low / Med / High | Option X |
| Architectural Alignment | High / Med / Low | High / Med / Low | Option X |
| Maintenance Overhead | Low / Med / High | Low / Med / High | Option X |

## 5. Recommendation & Downstream Path
- **Recommended Direction**: Clear, unambiguous recommendation of the path forward.
- **Rejected Alternatives**: Explicitly stated why alternatives were discarded.
- **Next Stage**:
  - [ ] Author Architecture Decision Record (`directives/adr/TASK-XXX_[slug].md`)
  - [ ] Generate PRD (`to-prd`)
  - [ ] Create Vertical Slice tickets (`to-issues`)
