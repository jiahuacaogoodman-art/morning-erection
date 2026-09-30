## What and why

<!-- One paragraph. Link the issue / ADR. -->

## Guarantees

- [ ] Does not weaken any of the rules in CONTRIBUTING.md (send-once, approval binding, origin scope, verification, read-only replay, stdlib kernel, …), or an ADR explains the change.
- [ ] Protocol / schema changes are additive, and `spec/` was regenerated (`uv run python scripts/gen_openapi.py`).
- [ ] Storage changes come with a numbered migration.

## Tested with

- [ ] `uv run pytest -q --ignore=tests/ui`
- [ ] `uv run ruff check` and `uv run python scripts/gen_openapi.py --check`
- [ ] `uv run pytest -q tests/ui` (sidecar changes)
- [ ] PostgreSQL gate (storage / lease / executor changes)

## Checklist

- [ ] Commits are signed off (`git commit -s`, DCO).
- [ ] `CHANGELOG.md` updated under *Unreleased* (user-visible changes).
- [ ] No credentials, cookies, keys or personal data in code, tests, fixtures or screenshots.
