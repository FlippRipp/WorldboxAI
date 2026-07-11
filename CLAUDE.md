# Project instructions for Claude

## Git workflow

- Always push completed, tested work to `main`. Do not leave finished changes
  sitting only on a feature branch and do not wait to be asked.
- If work was developed on a feature branch, rebase it onto `origin/main` and
  fast-forward `main` (`git push origin <branch>:main`). Run the test suite
  after the rebase, before pushing.
- Running the test suite mutates fixtures under `test_data/` (`save1.wbx`,
  snapshot zips). Restore them with `git checkout -- test_data` instead of
  committing them.

## LLM prompt context

- Do not add token/character caps when assembling LLM input context anywhere
  (character rosters, scene excerpts, instruction blocks). Context windows are
  large, and silent truncation causes subtle bugs — e.g. characters missing
  from image prompts. The only acceptable limits are hard external API
  constraints (e.g. Novita rejects image prompts over 1024 characters).

## Testing

- Run tests with `python -m pytest` from the repo root. The full suite is fast
  (~10s) — run all of it before pushing.
