# GitHub App token minting for agent runs

GitHub access for agents in this workspace goes through the **`manmade-agents`**
GitHub App. It is a least-privilege, per-task access model: every agent run that
needs GitHub mint a fresh, short-lived (1h) installation token, uses it, and
lets it expire. No long-lived token is stored or committed anywhere.

## Credentials (do not copy into runs or issues)

| Value | Where |
| --- | --- |
| App private key | `~/.multica/secrets/manmade-agents.pem` (mode 600, never commit/print) |
| `GITHUB_APP_ID` | `4645217` |
| `GITHUB_INSTALLATION_ID` | `154840801` |

The App is installed on the org's repos (ARES, Kairos, gamma-blaster, Aeolus,
...). Minted-token permissions (least privilege): `pull_requests: write`,
`contents: read`, `checks: read`, `statuses: read`, `issues: read`,
`metadata: read`.

## Using the mint helper

The helper lives at `scripts/mint-github-app-token.py` in this repo (a machine
copy also exists at `~/.multica/scripts/mint-github-app-token.py`, reachable
from any agent run on the daemon). Requires `python3` + the `cryptography`
package.

```bash
# full installation scope (all repos the App sees) — for read tasks
python3 scripts/mint-github-app-token.py --output env

# narrowed to ONE repo — use this for PR work targeting a single repo
source <(python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output env)
export GH_TOKEN=...            # either read from the token field / write to file

# scripted use — capture the token value directly
GH_TOKEN="$(python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output token)"
export GH_TOKEN

# JSON mode: token, expires_at, permissions, narrowed repos, and a mode-600
# temp file path (auto-removed on exit; pass --no-save to skip writing it)
python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output json
```

The three config values are overridable via env: `GITHUB_APP_ID`,
`GITHUB_INSTALLATION_ID`, `GITHUB_APP_KEY_PATH`. Defaults match the table above.

Then use the token for whichever operation (push, PR, review, merge), e.g. with
the GitHub CLI:

```bash
export GH_TOKEN="$(python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output token)"
gh pr review 12 --approve
gh pr merge 12 --merge --delete-branch
```

## Notes

- **One token per task, minted fresh** — never reuse a token across runs and
  never persist an installation token. Expiry is ~1h; the helper's JWT is valid
  10 minutes at mint time.
- **Repo narrowing** — pass `--repo OWNER/REPO` and the installation token is
  scoped to that repository only (`repository_ids` on the mint request).
- **contents is read-only** — with these least-privilege permissions the App
  cannot push commits, so a PR is authored by the run's push identity and the
  **approve/merge** step is what runs under the App-minted token.
- Never print or post the private key; never post a minted token value in an
  issue. Reference the key by path and keep token output in the run only.