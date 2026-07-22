"""Second-opinion PR review with Gemini 3.5 Flash.

Reads a unified diff on stdin, prints a markdown review on stdout. Called by
.github/workflows/gemini-review.yml, which posts the output as a PR comment.

Kept to a single generate_content call, not an agentic loop: Codex already
does whole-diff review, so the value here is a second model's opinion on the
same diff, not a deeper repo crawl. Swap to the Antigravity SDK if you later
want file-level context.

Self-check (no API key needed):  python gemini_review.py --selftest
"""
import os
import sys

MAX_DIFF_CHARS = 120_000  # Flash 3.5's context is far larger; this caps cost/latency.

PROMPT = """You are a senior reviewer giving a SECOND opinion on a pull request \
for ARES, an intraday options-trading bot (Python). Another AI reviewer already \
covers style and general bugs, so do not duplicate that — focus on what a second \
set of eyes catches:

- Correctness of trading/money math: position sizing, P&L, stop-loss/target logic, \
rounding, off-by-one on price levels.
- Credential and secret handling: anything that could log, leak, or hardcode a key, \
token, or webhook.
- State/concurrency bugs: the bot holds in-memory session state (VWAP, buffers) that \
resets on restart; flag anything that corrupts or wrongly assumes it.
- Silent failure: exceptions swallowed, a branch that no-ops when it should alert.

Be concise. Report only real issues, most serious first, each with the file and a \
one-line fix. If the diff is clean, say so in one line — do not invent nitpicks.

Here is the diff:

```diff
{diff}
```
"""


def build_prompt(diff: str) -> str:
    """Truncate the diff (keeping the head) and embed it in the review prompt."""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncated — review the rest manually]"
    return PROMPT.format(diff=diff)


def _selftest() -> None:
    assert "```diff" in build_prompt("small"), "prompt must fence the diff"
    big = build_prompt("x" * (MAX_DIFF_CHARS + 500))
    assert "truncated" in big, "oversized diff must be truncated"
    assert len(big) < MAX_DIFF_CHARS + len(PROMPT) + 200, "truncation must bound length"
    print("selftest ok")


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        return

    diff = sys.stdin.read().strip()
    if not diff:
        print("🔷 **Gemini review** — no diff to review.")
        return

    try:
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        resp = client.models.generate_content(
            model=os.environ.get("MODEL", "gemini-3.5-flash"),
            contents=build_prompt(diff),
        )
        body = (resp.text or "").strip() or "_(empty response)_"
    except Exception as e:  # never crash the job — post the failure instead
        body = f"_Gemini review failed: `{type(e).__name__}: {e}`_"

    print(f"🔷 **Gemini review** ({os.environ.get('MODEL', 'gemini-3.5-flash')})\n\n{body}")


if __name__ == "__main__":
    main()
