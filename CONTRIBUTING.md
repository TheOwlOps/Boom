# Contributing to Boom

## Development Setup

```bash
git clone https://github.com/TheOwlOps/Boom.git
cd Boom
devbox shell  # or: pip install httpx pydantic
```

## Code Style

- Python: stdlib + httpx/pydantic only (no new deps without discussion)
- TypeScript: strict mode, no `any` without justification
- Comments in Vietnamese for complex logic, English for API docs
- No decorative code: every module must be on a real execution path

## Testing

- Every new module needs tests in `evals/test_*.py`
- Tests must PASS before push: `python3 evals/test_yourmodule.py`
- Mock external calls (no real API calls in tests)

## Pull Requests

1. One feature per PR
2. Include tests
3. Update docs if adding user-facing features
4. CI must pass (Python tests + web build + nix check)
