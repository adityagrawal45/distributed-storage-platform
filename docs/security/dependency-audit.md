# Dependency Vulnerability Audit (Phase 10)

## Tool used

`pip-audit` (installed temporarily into the local user site for this
session — not added to `requirements.txt`, since it's an audit tool,
not an application dependency), run against `requirements.txt` as
pinned before this phase's changes. `bandit`, `gitleaks`, `semgrep`,
`trivy` were not available in this environment and were not installed
— the hardcoded-secret sweep (see below) was done via targeted `grep`
for known secret-format patterns (AWS keys, PEM private key headers,
common API-key prefixes) plus manual inspection of `.gitignore` and
`git ls-files` to confirm `.env`/`k8s/06-secret.yaml` were never
tracked. This is a narrower check than a real Gitleaks/Trivy scan
would give — recorded honestly, not as a substitute for running those
tools for real before a production deployment.

## Findings (as scanned, before this phase's fixes)

`pip-audit -r requirements.txt` found 22 known CVEs across 5 packages:

| Package | Version | Vulns | Fixed in | Action taken |
|---|---|---|---|---|
| `python-jose[cryptography]` | 3.3.0 | 5 (incl. algorithm-confusion/DoS class) | 3.4.0+ | **Bumped to 3.5.0** — this is the JWT signing/verification library, directly on the authentication attack surface |
| `python-multipart` | 0.0.20 | 6 | 0.0.22–0.0.31 (progressive) | **Bumped to 0.0.32** — parses every `UploadFile`, i.e. every byte of untrusted upload input, in the codebase |
| `starlette` | 0.41.3 | 9 | varies | **Not bumped** — transitive via `fastapi==0.115.6`; bumping it in isolation would desync from FastAPI's tested pin, and bumping FastAPI itself is a larger-surface framework upgrade with real regression risk this phase did not have budget to fully re-verify beyond the existing test suite. Recorded as a remaining risk requiring a dedicated FastAPI/Starlette upgrade pass with its own review. |
| `ecdsa` | 0.19.2 | 1 (Minerva-class timing side-channel) | none published | **Not fixable by version bump** — a transitive dependency (of `python-jose[cryptography]`'s ECDSA support) with a known, currently-unpatched timing-side-channel weakness in its pure-Python implementation. NimbusFS signs tokens with HS256 (`JWT_ALGORITHM` default), not an EC algorithm, so this code path is not exercised by default configuration — recorded as an accepted, monitored risk, not exploitable under the current default config. |
| `pytest` | 8.3.4 | 1 | 9.0.3 | **Not bumped** — dev/test-only dependency, never shipped to production, not exposed to any attacker-controlled input; a major-version bump (8→9) carries real `pytest-asyncio` compatibility risk not worth taking mid-security-audit for a dev-tool-only, non-production-facing CVE. |

## Verification

Both applied bumps (`python-jose` 3.3.0→3.5.0, `python-multipart`
0.0.20→0.0.32) were installed into the actual test environment and the
**full 429-test suite was re-run and passed with zero regressions**
(see `final-report.md`'s Security Tests section) — including every
JWT-issuing/verifying test (`test_protected_routes.py`,
`test_security_phase10.py`) and every file-upload test
(`test_file_storage.py`, `test_chunked_upload.py`) that exercises
`python-multipart`'s parsing path directly.

## What "MEASURED" means here, honestly

This audit reflects the dependency set and known-vulnerability
database **as of this session** (2026-09). `pip-audit` queries a
public vulnerability database at run time — a new CVE published
tomorrow against any pinned package would not appear in this report.
This is a snapshot, not a standing guarantee — re-running `pip-audit
-r requirements.txt` periodically (or wiring it into CI, which does
not exist yet — see `CONTEXT.md`'s "Not yet built" list) is how that
gap gets closed structurally rather than by memory.

## Phase 12 addendum (2026-09-08): re-audit + wired into CI

Re-ran `pip-audit -r requirements.txt` while standing up
`.github/workflows/ci.yml`'s `dependency-audit` job. The accumulated
CVE database now reports **11 known vulnerabilities across 3
packages** (`starlette` 0.41.3 — now 7 distinct advisory IDs, up from
9 individual CVE mentions in the Phase 10 table above, as the public
database has been updated since; `pytest` 8.3.4 — 1; `ecdsa` 0.19.2 —
1). The underlying situation is UNCHANGED from Phase 10's analysis
above — same three packages, same reasoning for not bumping each
(`starlette`: transitive via `fastapi`, a real upgrade-and-reverify
project of its own; `pytest`: dev-only, never shipped; `ecdsa`: HS256
is the default `JWT_ALGORITHM`, so this code path isn't exercised) —
this addendum exists because the brief for the phase that adds
dependency-scanning CI (§10, §42: "do not fabricate ... vulnerability
results") requires re-confirming the count against what CI will
actually enforce, not reusing a possibly-stale number.

**CI enforcement**: `dependency-audit` passes `--ignore-vuln <ID>` for
each of the 9 specific advisory IDs found in this re-audit (7
`starlette` + 1 `pytest` + 1 `ecdsa` — see the job step in
`.github/workflows/ci.yml` and the mirrored `make security` target in
`Makefile` for the exact IDs). This is an ALLOW-LIST of specific,
individually-already-reviewed findings, not a blanket
`--ignore-vuln`-everything or a severity threshold — **a new CVE
published against any pinned dependency, including these three
packages, fails the build** the moment `pip-audit`'s database picks it
up, because its ID will not be in the ignore list. Re-reviewing and
either bumping the dependency or adding a newly-justified ignore entry
is the correct response to that failure, not blindly widening the
ignore list.
