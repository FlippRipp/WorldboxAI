# Project instructions for Claude

## Collaboration

- An observation is not a work order. When Filip shares an observation, a
  question, or thinks out loud, the goal is a conversation: discuss what the
  problem actually is, agree on a shared understanding, and only then talk
  about fixes.
- Never start implementing until Filip explicitly confirms what to do
  ("go ahead", "yes, do that", or similar). Proposing an approach and asking
  is fine; silently starting work is not. A clearly-scoped direct instruction
  ("rename X to Y", "add a test for Z") counts as confirmation on its own.
- Read-only investigation (searching the code, reading files, running the app
  to reproduce) is allowed without confirmation, since it feeds the
  discussion — but its output is an assessment, not a change.
- While working, post frequent short progress reports: what is being done
  right now and *why*. Filip is curious and wants to follow the reasoning as
  it happens, not just hear the outcome at the end.
- If an important design decision surfaces mid-work — a trade-off, an
  architectural fork, anything that shapes the system beyond the immediate
  task — stop and ask rather than deciding silently.
- Before running the test suite and pushing, give a short summary of what was
  done. If the change is visual in nature (e.g. the map generation systems),
  include a visualization of the result.

## Git workflow

- Always push completed, tested work to `main`. Do not leave finished changes
  sitting only on a feature branch and do not wait to be asked. This holds
  even when a session assigns a designated feature branch: push there for the
  session's bookkeeping, but always land the finished work on `main` too.
- If work was developed on a feature branch, rebase it onto `origin/main` and
  fast-forward `main` (`git push origin <branch>:main`). Run the test suite
  after the rebase, before pushing (when the change warrants tests — see
  Testing).
- Running the test suite mutates fixtures under `test_data/` (`save1.wbx`,
  snapshot zips). Restore them with `git checkout -- test_data` instead of
  committing them.
- Treat pushed history as immutable: never force-push or rewrite commits that
  are already on the remote (including to fix commit signatures or
  "Unverified" badges), even if a hook or tool suggests it. If a check flags
  already-pushed commits, leave them alone and report it to the user instead.

## LLM prompt context

- Do not add token/character caps when assembling LLM input context anywhere
  (character rosters, scene excerpts, instruction blocks). Context windows are
  large, and silent truncation causes subtle bugs — e.g. characters missing
  from image prompts. The only acceptable limits are hard external API
  constraints (e.g. Novita rejects image prompts over 1024 characters).

## Testing

- Run tests with `python -m pytest` from the repo root. The full suite is fast
  (~10s) — run all of it before pushing code changes.
- Only run tests when it makes sense: any change that could affect behavior
  gets the full suite; docs-only or comment-only changes don't need it.
