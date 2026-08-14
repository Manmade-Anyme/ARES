---
adr_id: "TASK-XXX"
title: "[Architectural Decision Title]"
status: "proposed | approved | superseded | rejected"
date: "YYYY-MM-DD"
author: "Architect Agent / Author"
applies_to: "ARES Core / Detectors / Engine / Ingestion / Storage / ML"
---

# Architecture Decision Record: [TASK-XXX] [Title]

## 1. Status & Context
- **Status**: Proposed / Approved (Date: YYYY-MM-DD)
- **Context**: 
  - What context triggered this decision?
  - What problem or architectural constraint are we solving?
  - Relevant prior art or related ADRs: `directives/adr/TASK-YYY_*.md`

## 2. Decision
Summarize the architectural choice clearly and concisely.
- What approach is selected?
- Why was this approach chosen over the alternatives?
- How does this adhere to the Seven-Layer Modular Architecture?

### System Architecture & Data Flow
```
[ Ingestion Layer ] ──▶ [ Engine / Buffer Layer ] ──▶ [ Detector Layer ] ──▶ [ Alerts / Storage Layer ]
```

## 3. Interface & Contract Specifications
Define data structures, schemas, configuration parameters, and interface contracts.

### Dataclass / Model Contracts
```python
# Exact dataclass or schema specifications
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ExampleContract:
    id: str
    timestamp: int
    value: float
```

### Configuration & Environment Variables
| Config Variable | Type | Default | Description |
|---|---|---|---|
| `CONFIG_PARAM_A` | float | `1.5` | Threshold for trigger |
| `CONFIG_PARAM_B` | bool | `True` | Master switch |

## 4. Testing & Verification Seams
- **Primary Test Seams**: External observable behavior, unit test contracts.
- **Mocking Boundaries**: Isolate external API (DhanHQ) and database (Supabase).
- **Target Test Coverage**: 100% test coverage for all new deterministic logic.

## 5. Consequences & Trade-offs
- **Positive Impacts**:
  - High performance, low latency overhead.
  - Clear separation of concerns.
- **Negative / Neutral Trade-offs**:
  - Additional configuration parameters.
  - Data migration or backward compatibility requirements.
