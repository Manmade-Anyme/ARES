# TASK-264 Trading Methodology Documentation: Options Greeks & Dynamic ATR Trailing
**Date:** 2026-10-17  
**Status:** ready  
**Assigned Agent:** [@Documentation Agent](mention://agent/08c6ab82-42ca-4c05-b058-ee016cf04e7d)  
**Related Issue:** [MANM-264](mention://issue/01a145c6-7a15-7928-b756-073ddec4a602)  
**Related PR:** [PR #136](https://github.com/Manmade-Anyme/ARES/pull/136) (Merged)  

---

## Goal
Create comprehensive, publication-grade Trading Methodology documentation in the Obsidian vault (`~/Documents/Obsidian/Trading Methodology/`) covering Greeks-based option pricing/brackets, strike selection, dual-layer stop-loss architecture, and dynamic ATR trailing stop roadmap from PR #136, linking them bidirectionally with changelogs, ADR-176, and vault indices.

---

## Inputs
1. **Pull Request & Implementation Details:**
   - GitHub PR #136: `feat(MANM-176): implement Greeks-based option brackets and paper execution` (Merged).
   - Core codebase implementation: `options_math.py`, `execution/order_manager.py`, `models.py`, `config_profiles.py`.
2. **Architectural & Research Artifacts:**
   - ADR-176: `ARES/docs/MANM-176-options-entry-exit-sl-greeks-limit-orders.md` and `~/Documents/Obsidian/Projects/Ares/ADR-176_options-entry-exit-sl-greeks-limit-orders.md`.
   - Obsidian Research Synthesis: `~/Documents/Obsidian/Projects/Ares/MANM-176-Obsidian-Notes-Synthesis-and-Plan.md`.
   - Historical Research: `Projects/Ares/TASK-011 Option Calculator & Greeks Audit POC.md`, `Notes/Daily Market Observation/2026-10-13 - Exhaustion Stop Loss Replay - Three Iterations.md`, `Projects/Ares/TASK-185_SL-Target-Optimization.md`.
3. **Changelogs & Project Tracking:**
   - `ARES/CHANGELOG.md` (Unreleased MANM-176 entries).
   - `~/Documents/Obsidian/Projects/Ares/Ares Build Log.md`.
   - `~/Documents/Obsidian/Projects/Ares/MANM-176-log.md`.
   - `~/Documents/Obsidian/Vault Index.md`.

---

## Tools / Scripts to Use
- Obsidian Flavored Markdown (wikilinks `[[...]]`, callouts, mathematical formulas `$$...$$`, frontmatter tags).
- Repository documentation sync (`ARES/docs/` and `~/Documents/Obsidian/`).

---

## Expected Output
1. **New Trading Methodology Folder & Notes in Obsidian:**
   - Path: `~/Documents/Obsidian/Trading Methodology/ARES Options & Greeks Methodology/`
   - Files to create:
     1. `ARES Options Methodology Index.md`: Master index and conceptual overview of ARES options execution methodology.
     2. `Options Greeks Calculation & Bracket Execution.md`:
        - Strike Selection (ATM preference $||\Delta| - 0.50| \to 0$, current expiry retention vs liquidity cliff).
        - 2nd-order Taylor expansion Greeks-based pricing ($\Delta P_{\text{gain}} = \Delta \cdot \text{Index} + \frac{1}{2}\Gamma (\text{Index})^2$).
        - Dual-layer stop-loss architecture (Layer 1 synthetic underlying spot monitor, Layer 2 emergency exchange OCO stop).
        - Upfront limit order execution, tick rounding (0.05), and nonnegative capital/lot sizing.
     3. `Dynamic ATR Trailing Stop & High-Water Mark Invariant.md`:
        - 5-Minute ATR Trailing Stop specification (`atr_period=5`, `hhv_period=10`, `multiplier=2.5`).
        - High-Water Mark (HWM) Ratchet Invariant: $\text{SL}_t = \max(\text{SL}_{t-1}, \text{Cost}, \text{ATR}_t)$.
        - Guaranteed post-T1 break-even floor and underlying spot evaluation domain.
2. **Bidirectional Obsidian Linking & Index Updates:**
   - Update `~/Documents/Obsidian/Vault Index.md` under `## Trading Methodology` to link the new ARES Options Methodology index.
   - Update `~/Documents/Obsidian/Projects/Ares/ARES.md` and `~/Documents/Obsidian/Projects/Ares/Ares Build Log.md`.
   - Create or update `~/Documents/Obsidian/Projects/Ares/MANM-176-log.md` with explicit links to PR #136 and the new Trading Methodology notes.
3. **Repository Documentation Mirror:**
   - Sync the core methodology guide into `ARES/docs/trading_methodology_options_greeks_atr.md`.

---

## Acceptance Criteria
- [ ] New notes created in `~/Documents/Obsidian/Trading Methodology/ARES Options & Greeks Methodology/` with valid Obsidian frontmatter, LaTeX math, and wikilinks.
- [ ] Greeks calculations, strike selection rationale, dual-layer SL, and ATR trailing invariants accurately described without mathematical ambiguity.
- [ ] Bidirectional wikilinks exist between new methodology notes, ADR-176, `MANM-176-log.md`, `Ares Build Log.md`, and `Vault Index.md`.
- [ ] Changelogs (`CHANGELOG.md` and `Ares Build Log.md`) explicitly cross-link to PR #136 and the new methodology notes.
- [ ] All wikilinks resolve cleanly with zero broken references.

---

## Edge Cases & Boundaries
- Documentation only — no code modifications to trading logic.
- Respect existing Obsidian vault structure and frontmatter conventions.
- Ensure all file paths and wikilinks match case-sensitive names in the vault.
