# NimbusFS — Release Process & Versioning (Phase 12)

Companion to `docs/ci-cd.md` (the pipeline) and `docs/deployment.md`
(environments). Same DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline —
see `docs/observability.md`'s opening section.

---

## 1. Versioning strategy (Phase 12 §27)

**Git commit SHA is the unit of release.** Not semantic versioning,
not a hand-maintained release-tag scheme. Every image this pipeline
builds is tagged `nimbusfs-api:<git-sha>` — see `docs/ci-cd.md` §7 for
why never `:latest`.

Why SHA over semver here: semver (`v1.4.2`) communicates something
about the SIZE/nature of a change to a human reading a changelog —
valuable for a published library with external consumers making their
own upgrade decisions. NimbusFS has none of that; it has one
deployment target this pipeline itself controls end to end. A SHA is
unambiguous (exactly one commit produced it, always), requires no
human judgment call at release time ("is this a minor or a patch?"),
and is what every other piece of traceability below is keyed on
already (`Settings.GIT_COMMIT`, since Phase 4). `Settings.APP_VERSION`
(currently a static `"0.1.0"`, human-maintained) remains a separate,
coarser marketing/API-version string — unaffected by this pipeline,
not conflated with the release identifier.

## 2. Release traceability (Phase 12 §28)

Every question the brief poses is answerable, and each is answered by
a mechanism that already existed BEFORE this phase (built in Phase 4)
or is added by this phase — nothing new had to be invented:

| Question | Answer mechanism |
|---|---|
| Which git commit is running? | `GET /api/v1/health` -> `data.server.git_commit` (`app/core/server_info.py`, Phase 4) |
| Which container is running? | Same response's `data.server.build_version`; the exact image is `<region>-docker.pkg.dev/<project>/nimbusfs/nimbusfs-api:<git_commit>` — tag equals the commit shown |
| Who deployed it? | The workflow run's "Record deployment" step summary (`deploy-staging.yml`/`deploy-production.yml`) records the triggering actor; GitHub's own Environment deployment history (repo -> Deployments) is the durable audit record — see §3 |
| When was it deployed? | Same step summary; GitHub Deployments UI timestamp |
| Which configuration was deployed? | `kubectl get configmap nimbusfs-config -n nimbusfs -o yaml` (ConfigMap is applied from the same `k8s/05-configmap.yaml` in the same commit); secret VALUES are never in scope for this question by design (Phase 10) |
| Which infrastructure version is active? | `terraform show`/`terraform state list` against the remote state bucket (`docs/infrastructure.md` §3) reflects the last-applied Terraform commit |

No new "release notes" system or dashboard was built — every answer
above already resolves through infrastructure that exists (health
endpoint, GitHub's own Deployments feature, Terraform state), which is
deliberately simpler than standing up a parallel release-tracking
service to answer questions the existing systems already answer.

## 3. Change audit (Phase 12 §29)

GitHub's native **Deployments** feature (visible under the repo's
"Environments" and "Deployments" tabs) is the audit trail: every run
of `deploy-staging.yml`/`deploy-production.yml` that references an
`environment:` creates one automatically, recording the environment,
the commit, the actor, the timestamp, and the outcome (success/
failure) — without any custom code. The `$GITHUB_STEP_SUMMARY` block
each workflow writes (see `docs/ci-cd.md`'s pipeline diagram) restates
the same facts in the run's own summary page for a reader who doesn't
know to look under "Deployments" separately. Neither ever logs a
credential value — the summary blocks explicitly enumerate SHA/
environment/actor/timestamp/result and nothing else; no secret VALUE
is ever an input to a workflow step in the first place (WIF-issued
access tokens are GitHub Actions' own masked-in-logs secret type, not
a repo-supplied one).

## 4. Build-once, deploy-many

The SAME image, built exactly once per commit to `main`
(`ci.yml`'s `build-and-push` job), is what `deploy-staging.yml` and,
later, `deploy-production.yml` deploy — production never triggers a
fresh `docker build`. This is what makes "staging validated this
exact artifact" a meaningful statement: there is no rebuild step
between staging approval and production deploy where a dependency
could resolve differently, a base-image tag could have moved, or a
build-time environment variable could differ. `deploy-production.yml`'s
`image_tag` input is validated implicitly — `scripts/ci-deploy.sh`
fails immediately (`docker`/`kubectl` referencing a nonexistent tag)
if someone supplies a SHA that was never actually built and pushed.

## 5. How a change actually ships, end to end

```
1. Branch from main:  git checkout -b feature/thing
2. Open a PR into main
3. ci.yml runs: lint/security/test/build/scan — all must pass
4. PR reviewed + approved (branch protection — GitHub repo setting,
   not a workflow file) + merged to main
5. ci.yml re-runs on the merge commit, pushes nimbusfs-api:<sha>
6. deploy-staging.yml deploys <sha> to staging automatically,
   verifies, rolls back automatically on smoke-test failure
7. A human confirms staging looks right (manual — dashboards from
   docs/monitoring.md, a manual smoke click-through, whatever the
   change warrants)
8. gh workflow run deploy-production.yml -f image_tag=<sha>
9. GitHub's "production" environment required reviewers approve
10. deploy-production.yml deploys the SAME <sha>, verifies, rolls
    back automatically on smoke-test failure
11. GET /api/v1/health on production confirms git_commit == <sha>
```

No step above requires anyone to SSH into a server, hand-edit a
Kubernetes manifest with real values, or run `docker push` from a
laptop — every credential-bearing action happens inside a GitHub
Actions runner using a short-lived WIF-issued token (`docs/ci-cd.md`
§4), answering Phase 12 §44's "can the system deploy without SSHing
into a server?" with yes, by construction.
