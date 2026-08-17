# ARES Workspace Rules

Always adhere to the Global Development Pipeline workflows when performing any work in this workspace. Make sure to use ai-grep and caveman skill for you internal communication.

## 1. Development Pipeline Modes

Every task must involve the Project Manager, Documentation, QA, and PR Reviewer agents. The Software Architect agent can be skipped only if there are no architectural changes.

Choose the appropriate mode based on scope:
*   **Code-First Mode** (< 1 day, quick features/fixes):
    1. PM phase: Define the task and create a directive if needed.
    2. Write failing unit tests first (TDD).
    3. Assist with minimal implementation to pass tests.
    4. Documentation phase: Update all documentation properly.
    5. Run Debug & QA steps to check coverage (target: 100%).
    6. PR Reviewer phase: Review the PR.
*   **Design-First Mode** (complex changes, > 1 day, architectural decisions):
    1. PM phase: Write a directive (`directives/TASK-###_[description].md`). **Wait for human approval.**
    2. Architect phase: Write an ADR (`directives/adr/TASK-###_[description].md`) detailing design and API contracts. **Wait for human approval.** *(Skip if no architectural changes)*
    3. Code Generator phase: Implement from the ADR.
    4. Documentation phase: Update all documentation properly.
    5. Run Debug & QA validation.
    6. PR Reviewer phase: Review the PR.
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
4.  **Raise PR**: Create a Pull Request. To avoid shell substitution/expansion issues (e.g., with backticks or quotes in descriptions), write the PR description to a temporary markdown file (e.g., `temp_pr_body.md`) and run the PR creation command as:
    ```bash
    gh pr create --title "feat([TASK_ID]): [Detailed Description]" -F temp_pr_body.md
    ```
    Ensure the PR description includes:
    - **Problem**: What issue or request was addressed.
    - **Solution/Implementation**: A summary of what changes were made.
    - **Testing & Verification**: The test coverage report, verification steps, or deployment validations.
    Delete the temporary file after creation. If terminal/environment permissions restrict the command, provide the direct PR creation link. Print the PR URL and halt execution. Do not merge the PR yourself.
5.  **Merge Cleanup**: Once the human merges the PR:
    - Switch to `main` and pull.
    - Delete the local branch (`git branch -d feature/[TASK_ID]-[description]`). Do not delete the remote branch.
    - Sync final status logs to the Obsidian Vault (`~/Documents/Obsidian`).
