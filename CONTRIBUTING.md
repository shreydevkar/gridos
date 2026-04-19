# Contributing to GridOS

Thanks for your interest. GridOS is open-core and there are two ways in — which one fits depends on whether you want to touch the kernel itself or ship something on top of it.

## Two ways to contribute

**1. Core contributors** — Working on the kernel, provider adapters, collision engine, or SaaS layer. Higher bar, higher blast radius.

**2. Plugin / extension developers** — Shipping standalone formulas, agents, or models without patching core. Lower friction; this is where most third-party work belongs. See [`plugins/README.md`](./plugins/README.md) for the 60-second guide.

The full contribution overview lives in the README: [README.md#contributing](./README.md#contributing).

## Before you start

- Open an issue first for anything larger than a small bug fix or docs tweak. Alignment beats re-work.
- Use the [bug report](./.github/ISSUE_TEMPLATE/bug_report.md) and [feature request](./.github/ISSUE_TEMPLATE/feature_request.md) templates.
- Check the [roadmap](./README.md#roadmap) — some items are already claimed.

## Dev setup

```bash
git clone https://github.com/shreydevkar/gridos_kernel.git
cd gridos_kernel
pip install -r requirements.txt
cp .env.example .env   # add at least one provider key
uvicorn main:app --reload
```

Open http://localhost:8000 and confirm a prompt roundtrips.

## Tests

Both suites are offline — no network, no LLM calls:

```bash
python test_platform.py
python test_plugins.py
```

Run both green before opening a PR.

## PR checklist

- [ ] Scope is one thing. Don't bundle a refactor with a feature.
- [ ] New user-facing capability? Update README.md in the same PR.
- [ ] Added a formula / agent / model? Prefer a plugin under `plugins/` over patching core.
- [ ] Tests pass locally.
- [ ] No committed secrets, no `__pycache__/`, no `.env`.

## Style

- Python: keep it readable, match the surrounding file. No hard formatter requirement yet.
- Comments: explain *why*, not *what*. A well-named function beats a paragraph docstring.
- Commit messages: conventional-style prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `perf:`) are preferred — see `git log` for the house style.

## Reporting security issues

Don't file public issues for security vulnerabilities. Email the maintainer (see the GitHub profile) with details. We'll acknowledge within a few days.

## License

By contributing you agree that your contribution is licensed under the [MIT License](./LICENSE) the rest of the project uses.
