# NimbusFS — Development Workflow (Phase 12)

Companion to `docs/ci-cd.md` (what CI actually runs) and
`docs/release-process.md` (how a merged change ships). This is the
"what do I actually type" doc.

---

## 1. Branching strategy (Phase 12 §6)

**Trunk-based, not GitFlow**: `feature/*` branches, opened as a Pull
Request into `main`, merged once CI passes and the PR is approved.
`main` is always deployable — it is what `deploy-staging.yml`
auto-deploys on every successful merge.

```
feature/add-thing  --PR-->  main  --auto-->  staging  --manual-->  production
```

No `develop` branch, no `release/*` branches. GitFlow's extra branches
exist to support maintaining multiple release lines simultaneously
(e.g. patching an old major version while developing the next) —
NimbusFS has exactly one thing running in production at a time and
one image built per commit (`docs/release-process.md` §4), so that
complexity would have no problem to solve here. Recommended GitHub
repo settings (not enforceable from a workflow file — configured in
Settings -> Branches): require `main` to only accept PRs (no direct
pushes), require the `ci.yml` checks to pass, require at least one
approval.

## 2. Day-to-day loop

```bash
git checkout -b feature/my-change
# ... edit code ...
make lint            # ruff check . — same as CI's blocking lint job
make test             # pytest -q — same 449+ tests CI runs
make security          # bandit + pip-audit — same as CI's blocking security jobs
git add -A && git commit -m "..."
git push -u origin feature/my-change
# open a PR — ci.yml runs the full gate automatically
```

`make ci` runs lint+typecheck+test+security together, in the same
order CI does, so a failure locally is a failure in CI too (see
`Makefile`'s own comment for the one thing it does NOT reproduce
locally: the Trivy container scan, which needs a registry/Trivy
install most developers won't have).

## 3. What CI checks that `make ci` doesn't

- `ruff format` — informational only, see `docs/ci-cd.md` §6; running
  `ruff format .` locally is safe and welcome, just not required.
- `gitleaks` — not distributed as a simple `pip install`; install
  separately (https://github.com/gitleaks/gitleaks#installing) if you
  want to run it locally before pushing. CI always runs it regardless.
- The Trivy container scan — needs a built image and Trivy installed;
  `make build` builds the image, `trivy image nimbusfs-api:local` (if
  Trivy is installed) reproduces the scan.

## 4. Getting a change to production

See `docs/release-process.md` §5 for the full, exact sequence
(branch -> PR -> merge -> auto-staging -> manual approval ->
production). This document stops at "how do I write and submit the
change" — that one covers what happens to it after merge.

## 5. Database model changes

```bash
# after editing a SQLAlchemy model in app/models/
alembic revision --autogenerate -m "describe the change"
# READ the generated migration file — autogenerate is a starting
# point, not something to commit unread (see docs/deployment.md §4's
# expand/contract discipline: does this migration stay compatible
# with the OLD app version that will still be running mid-rollout?)
git add alembic/versions/xxxx_describe_the_change.py app/models/...
```

Running the migration against a real database is a deliberate, manual
step (`alembic upgrade head`) — not automated by any CI/CD workflow in
this repo. See `docs/deployment.md` §4 for why.

## 6. Infrastructure changes

```bash
cd terraform
# edit a .tf file
terraform fmt          # matches what terraform.yml's plan job checks
git add -A && git commit -m "..." && git push
# open a PR — terraform.yml posts a plan as a PR comment automatically
```

Applying is never automatic — see `docs/infrastructure.md` §5.

## 7. Local end-to-end (development environment)

Unchanged from before this phase — `docker-compose.yml` (Postgres +
Redis + the app, `--reload` enabled) remains the local development
loop; this phase adds nothing to it and does not require Docker to be
running for `make lint`/`make test`/`make security` (all pure-Python,
no containers involved).
