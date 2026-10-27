# LV Copytrader Public

This is the public source edition. Read README.md and CHANGELOG.md before working.

- Never add .env, API keys, PATs, Telegram session strings/files, databases, account credentials,
  runtime-state, private deployment backups, or personal trade/channel data.
- Use .env.example with placeholders only. The owner maintains a separate private edition;
  never import its Git history, branches, tags, or snapshots here.
- Record every behavior change/fix/improvement in CHANGELOG.md under Unreleased.
- Keep VERSION, backend/app/__init__.py, backend/pyproject.toml, frontend/package.json and
  frontend/package-lock.json consistent when preparing a new release.
- Run the appropriate tests before committing; never use real broker orders as a test.
- Review changed CI workflows and run relevant typecheck/build and tests before pushing.
  After pushing, inspect Actions/check statuses for the exact new commit. Report failed, pending,
  skipped or unavailable checks explicitly; a successful push alone does not mean CI or
  deployment succeeded. Fix failures without disabling checks or rewriting published releases.
- Public contributors push branches/PRs in this public repository only. Owner publication starts
  from the private source repository using its AGENTS.md and scripts/publish.py, private first.
