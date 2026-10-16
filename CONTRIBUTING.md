# Contributing to Boom

Thanks for helping make Boom better. This guide covers the essentials: setting up your environment, our code style, testing expectations, and the pull-request process.

## Development Setup

**Option 1 — Devbox (recommended, reproducible):**

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
devbox shell        # Nix-powered: Python 3.12, Node 22, tesseract
./start.sh all
```

**Option 2 — Manual:**

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
pip install httpx "pydantic>=2" pyyaml   # must match pyproject.toml dependencies
./start.sh all
```

**Option 3 — Docker:**

```bash
docker compose up --build
```

Verify your setup with a quick smoke test:

```bash
python3 -m pytest evals/ -q   # or: for f in evals/test_*.py; do python3 "$f"; done
```

## Code Style

- **Python:** stdlib + `httpx`/`pydantic` only. New third-party dependencies need maintainer discussion first.
- **TypeScript:** strict mode; no `any` without justification.
- **Comments:** complex logic in Vietnamese is fine, API docs in English.
- **No decorative code:** every module must sit on a real execution path. Unwired code gets removed, not parked.

## Testing

- Every new module ships with tests in `evals/test_*.py`.
- Tests must pass before push: `python3 evals/test_yourmodule.py`.
- Mock external calls — no live API calls in tests.
- Test files assert real behavior; mark skips explicitly when an optional dependency is missing.

## Pull Requests

1. **One feature per PR.** Keep diffs focused and reviewable.
2. **Include tests** for all new behavior.
3. **Update docs** if you add anything user-facing.
4. **CI must pass:** Python tests, web build, and nix check.

### Before you open a PR

- [ ] Tests pass locally (`python3 evals/test_yourmodule.py`)
- [ ] Docs updated (if user-facing)
- [ ] No new dependencies without discussion
- [ ] Commit messages describe *what changed and why*

### After review

Maintainers may request changes — that's normal and not a rejection. Push follow-up commits to the same branch; once approved, we'll squash-merge.

## Reporting Issues

Open an issue with:

- What you expected vs. what happened
- Steps to reproduce (minimal, please)
- Environment: OS, Python/Node versions, relevant config (redact secrets)
- Logs or tracebacks, if any

Security issues: **do not open a public issue** — contact the maintainers privately.

## License

By contributing, you agree your work is licensed under the [MIT License](LICENSE).
