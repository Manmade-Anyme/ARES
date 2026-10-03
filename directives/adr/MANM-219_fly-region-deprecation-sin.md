---
adr_id: "MANM-219"
title: "Fly.io Primary Region Migration from Deprecated bom (Mumbai) to sin (Singapore)"
status: "approved"
date: "2026-10-03"
author: "Software Architect Agent"
applies_to: "ARES Infrastructure / Fly.io Deployment / Telemetry"
pr: "https://github.com/Manmade-Anyme/ARES/pull/124"
---

# Architecture Decision Record: MANM-219 Fly.io Primary Region Migration to sin

## 1. Status & Context
- **Task ID**: MANM-219
- **Date**: 2026-10-03
- **Status**: Approved (Implemented via PR #124)
- **Problem Statement**:
  - Following the merge of PR #122 (`c3f666d` / `TASK-210`), the automated GitHub Actions CI deployment workflow (`.github/workflows/deploy.yml` run [#37109548020](https://github.com/Manmade-Anyme/ARES/actions/runs/37109548020)) failed during the Fly deployment step (`flyctl deploy --local-only`).
  - Fly.io rejected provisioning the new `jev` process group machine:
    ```
    Process groups have changed. This will:
     * create 1 "jev" machine and 1 standby machine for it
    > Launching new machine
    No machines in group jev, launching a new machine
    ✖ Failed: error creating a new machine: failed to launch VM: Region bom is deprecated and cannot have new resources provisioned. Please consider using an alternate region such as sin
    Error: error creating a new machine: failed to launch VM: Region bom is deprecated and cannot have new resources provisioned. Please consider using an alternate region such as sin
    ```
  - Fly.io deprecated the Mumbai (`bom`) region on 2026-08-17 (as evaluated in `TASK-202 BOM Deprecation Migration Assessment 2026-08-02.md`). While existing VMs may continue operating until auto-eviction, Fly strictly prohibits provisioning any new VM instances, standby VMs, or process groups in `bom`.
  - Because ARES introduced an independent `jev` forward-predictor consumer process group in `TASK-210`, deploying to Fly requires provisioning a new VM in a non-deprecated region.

---

## 2. Decision & Architectural Rationale

We migrate the primary Fly deployment region for `ares-xzy-gq` from `bom` (Mumbai) to `sin` (Singapore).

### Core Decisions:
1. **Primary Region Configuration**:
   - Update `primary_region = 'sin'` in `fly.toml`.
   - Update `DEPLOYMENT.md` to document Singapore (`sin`) as the active deployment region.
2. **Topology & Machine Allocation**:
   - Retain dual-process VM topology defined in `TASK-210`:
     - `app`: `shared-cpu-1x`, 768 MB memory, `policy = 'never'`.
     - `jev`: `shared-cpu-1x`, 256 MB memory, `policy = 'always'`.
   - Both process groups and their standby failover instances will provision into `sin`.
3. **Latency Profile & Risk Assessment**:
   - As established in the `TASK-202` assessment, `dhanhq.co` endpoints reside on AWS `ap-south-1` (Mumbai).
   - Network RTT between Mumbai and Singapore over cloud backbone connections (Azure/AWS) is measured at ~53–63 ms (budgeted at ~55–65 ms).
   - ARES operates on an asynchronous 60-second polling cycle (`config_profiles.py`) and a 2-second tick interval for trailing stop-loss checks. An added latency of ~60 ms represents ~0.1% of the 60s cycle and ~3% of the 2s tick interval.
   - Therefore, ARES is not latency-critical to sub-10ms precision, and the Singapore latency delta is well within acceptable operational boundaries for Nifty options trading.
4. **Supabase and Discord Interconnects**:
   - Supabase project queries (`ares_signals`, `ml_collection`, `trade_analytics`, `llm_predictions`) and Discord webhook dispatches run over HTTPS egress. Singapore maintains Tier-1 routing to Supabase and Discord edge networks.

---

## 3. System Topology & Data Flow

```
                      ┌───────────────────────────────────────────────┐
                      │             Fly.io Region: sin                │
                      │                                               │
                      │   ┌─────────────────┐   ┌─────────────────┐   │
                      │   │   Process: app  │   │   Process: jev  │   │
                      │   │  (768MB, 1 vCPU)│   │  (256MB, 1 vCPU)│   │
                      │   └────────┬────────┘   └────────┬────────┘   │
                      └────────────┼─────────────────────┼────────────┘
                                   │                     │
                     HTTPS / WS    │                     │ HTTPS / WS
                     ~55-65ms RTT  │                     │ ~55-65ms RTT
                                   ▼                     ▼
               ┌───────────────────────┐             ┌───────────────────────┐
               │    DhanHQ Gateway     │             │    Supabase DB &      │
               │ (AWS ap-south-1, BOM) │             │    Discord Webhooks   │
               └───────────────────────┘             └───────────────────────┘
```

---

## 4. Alternatives Considered & Rejected

| Option | Rationale for Rejection |
|---|---|
| **Retain `bom` region** | Impossible. Fly.io API hard-rejects machine provisioning (`failed to launch VM: Region bom is deprecated`). |
| **Self-Hosted VPS (Hostinger Mumbai)** | Analyzed in `TASK-202`. Viable for whole-fleet consolidation, but introduces severe operational burdens (manual patching, OS maintenance, single point of failure, no automated CI/CD push deploy) for a single urgent unblock. |
| **Vercel Serverless** | Analyzed in `TASK-202`. Hard hobby/pro limits (max duration 300s, cron at 1/day, no persistent WebSocket) make it incompatible with ARES's 6h 15m continuous trading loop. |
| **Other Fly Regions (`maa`, `del`)** | Fly has consolidated its Indian presence; Chennai (`maa`) is not an active alternative for new apps. Singapore (`sin`) is Fly's officially recommended and robust regional hub in the error directive. |

---

## 5. Component Boundaries & File Manifest

| File | Component | Action |
|---|---|---|
| `fly.toml` | Infrastructure | Set `primary_region = 'sin'` |
| `DEPLOYMENT.md` | Documentation | Update primary region references to Singapore (`sin`) |
| `CHANGELOG.md` | Release tracking | Record region migration under `[Unreleased]` |
| `docs/Ares Build Log.md` | System Build Log | Record deployment fix and date |
| `directives/adr/MANM-219_fly-region-deprecation-sin.md` | Architecture | Record ADR specifications |
| `directives/adr/INDEX.md` | Architecture Index | Register MANM-219 |

---

## 6. Verification Checklist & Definition of Done

- [x] `fly.toml` updated with `primary_region = 'sin'`.
- [x] Documentation and build logs synchronized with changes.
- [x] Pre-merge CI checks pass on GitHub Actions (`deploy/test`).
- [x] ADR committed and registered in `INDEX.md`.
- [x] Pull Request #124 verified clean, mergeable, and ready for human merge.
- [ ] Post-merge Fly deploy on `main` provisions `app` and `jev` machines into `sin` without errors.
