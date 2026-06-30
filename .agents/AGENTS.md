# ARES Workspace Rules

Always adhere to the Global Development Pipeline workflows when performing any work in this workspace.

## 1. Development Pipeline Modes

Choose the appropriate mode based on scope:
*   **Code-First Mode** (< 1 day, quick features/fixes):
    1. Write failing unit tests first (TDD).
    2. Assist with minimal implementation to pass tests.
    3. Run Debug & QA steps to check coverage (target: 100%).
*   **Design-First Mode** (complex changes, > 1 day, architectural decisions):
    1. PM phase: Write a directive (`directives/TASK-###_[description].md`). **Wait for human approval.**
    2. Architect phase: Write an ADR (`directives/adr/TASK-###_[description].md`) detailing design and API contracts. **Wait for human approval.**
    3. Code Generator phase: Implement from the ADR.
    4. Run Debug & QA validation.
*   **Research/Brainstorm Mode** (exploring unknowns, stress-testing plans):
    1. Ask clarifying questions one at a time.
    2. Map out decision trees and document insights.

## 2. Git Branching & PR Gate

Every task must follow this workflow:
1.  **Branch Out**: Before any code is changed, check out `main` and pull latest. Create a feature branch: `git checkout -b feature/[TASK_ID]-[description]`.
2.  **Verify & Sync**: After QA approval, automatically:
    - Update `CHANGELOG.md`.
    - Update documentation files.
    - Sync relevant notes to the Obsidian Vault (`~/Documents/Obsidian`).
3.  **Commit & Push**: Commit with `feat([TASK_ID]): [Detailed Description]` and push the branch.
4.  **Raise PR**: Create a Pull Request using `gh pr create`. Print the PR URL and halt execution. Do not merge the PR yourself.
5.  **Merge Cleanup**: Once the human merges the PR:
    - Switch to `main` and pull.
    - Delete the local branch (`git branch -d feature/[TASK_ID]-[description]`). Do not delete the remote branch.
    - Sync final status logs to the Obsidian Vault (`~/Documents/Obsidian`).
