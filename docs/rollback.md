# NimbusFS — Rollback Strategy (Phase 12)

Companion to `docs/ci-cd.md` and `docs/deployment.md`. Same
DESIGNED/IMPLEMENTED/TESTED/MEASURED discipline — see
`docs/observability.md`'s opening section. **Nothing in this document
has been exercised against a real cluster or a real Postgres instance
this session or any prior one** — see the honesty note at the end.

---

## 1. Application / Kubernetes rollback

```
Version N (running, healthy)
    |
    v
Version N+1 deployed (rolling update, k8s/07-deployment.yaml)
    |
    v
Smoke test fails  ------------------------->  Smoke test passes
    |                                              |
    v                                              v
scripts/ci-rollback.sh                    N+1 stays, becomes "N"
    |                                     for the next release
    v
kubectl rollout undo deployment/<name> -n nimbusfs
(for nimbusfs-api AND all 4 worker Deployments together — see
 scripts/ci-rollback.sh's docstring for why together, not just the API)
    |
    v
Version N running again — verified via scripts/k8s-smoke-test.sh
```

`kubectl rollout undo` reverts to the **previous ReplicaSet
revision**, not to "whatever image tag you specify" — Kubernetes
already has the old ReplicaSet's Pod template recorded (unless
`revisionHistoryLimit: 5` — `k8s/07-deployment.yaml` — has aged it
out), so this is near-instant: no rebuild, no re-pull if the old
image layers are already cached on the node. This is why
`docs/ci-cd.md`'s pipeline auto-rollback step calls
`scripts/ci-rollback.sh` (which wraps `rollout undo`) rather than
re-running `scripts/ci-deploy.sh` with an old `IMAGE_TAG` — the latter
would repeat the exact rollout mechanics that may have just failed,
slower and no safer.

**Two rollback triggers**:
1. **Automatic** — `deploy-staging.yml`/`deploy-production.yml`'s own
   post-deploy smoke test fails -> immediate, unattended rollback,
   within the same workflow run (Phase 12 §32: "detect, stop rollout,
   keep healthy version serving, investigate, rollback if necessary" —
   here "investigate" happens AFTER traffic is already safely back on
   the old version, not before).
2. **Manual** — `.github/workflows/rollback.yml`, for the case a
   deploy passed every automated check and only later (a
   `docs/alerting.md` alert, a human-reported incident) turns out to
   need reverting.

**What automated rollback does NOT do**: retry the failed deploy in a
loop. `scripts/ci-deploy.sh`/`scripts/ci-rollback.sh` each run once
per workflow invocation; if `ci-rollback.sh` itself fails to complete
cleanly, it exits non-zero and prints "this needs a human, now" rather
than looping — Phase 12 §32's explicit "do not blindly retry a broken
deployment indefinitely."

## 2. Database rollback is NOT "run the down-migration" — why

A tempting-but-wrong mental model: "rolling back the app should roll
back the database too." This is wrong for a specific, important
reason:

**The moment a migration's forward step has run against production
data, an Alembic `downgrade` is not the inverse operation it looks
like — it is a SEPARATE forward operation that can itself destroy
data.** Concretely: an `expand` step (per `docs/deployment.md` §4)
that adds a nullable column and backfills it; if new-version Pods have
already written real user data into that column before a rollback is
triggered, running the migration's `downgrade()` (which typically
`DROP COLUMN`s it) **deletes that data** — data a rollback is supposed
to be a SAFE reaction to a problem, not a second, self-inflicted one.

The correct model, consistent with the expand/contract discipline in
`docs/deployment.md` §4:

- **A rollback reverts the APPLICATION to version N.** The database
  schema stays exactly as the (already-run) migration for version N+1
  left it — this is *why* expand/contract requires every migration to
  be additive-and-backward-compatible: version N's code must be able
  to run correctly against the schema version N+1 already migrated to,
  precisely so that a rollback never needs a matching schema rollback
  to be safe.
- **A schema "contract" step (dropping the now-unused old column) only
  ever runs in a LATER, separate migration**, after enough time/
  confidence has passed that no rollback to a version needing the old
  shape is still plausible — never bundled into the same deploy as the
  expand step.
- **If a migration genuinely cannot be made backward-compatible**
  (rare, and a signal to reconsider the migration's design first): the
  correct sequence is deploy the schema change alone first (with
  BOTH app versions still compatible with it, which is the actual
  requirement expand/contract exists to satisfy), confirm it's stable,
  THEN deploy the app version that depends on it — never combine an
  incompatible schema change with the app rollout that needs it in one
  step, specifically because that removes the option to roll the app
  back independently of the schema.

**This project has never actually needed to exercise this discipline
for real** — every Alembic migration since Phase 1 has been
additive-only so far (new tables, a new nullable column) by
happenstance of what each phase needed, not because expand/contract
has been deliberately tested end to end. Recorded here as the
governing PRINCIPLE for the day a genuinely breaking schema change is
needed, not as a claim it has already been battle-tested.

## 3. When automated rollback itself fails

`scripts/ci-rollback.sh` checks the outcome of `rollout undo` for
every Deployment and `kubectl rollout status`'s subsequent wait; if
either fails for any Deployment, it exits non-zero with an explicit
"this needs a human, now" message rather than pretending success. The
correct human response at that point:

1. `kubectl get pods -n nimbusfs -o wide` — is anything actually
   unhealthy, or did the wait just time out on a slow-but-fine rollout?
2. `kubectl rollout history deployment/<name> -n nimbusfs` — confirm
   there IS a previous revision to go back to (a fresh cluster/
   Deployment with `revisionHistoryLimit`-aged-out history has none).
3. `kubectl rollout undo deployment/<name> -n nimbusfs --to-revision=<N>`
   — target a SPECIFIC known-good revision rather than "the immediately
   previous one," if more than one bad revision shipped in succession.
4. If the Deployment object itself is in a bad state Kubernetes can't
   reconcile out of: `kubectl apply -f k8s/07-deployment.yaml` (with
   the last-known-good image tag substituted) re-establishes the
   desired spec from scratch — the same mechanism `ci-deploy.sh` uses
   for a forward deploy, applied to a known-good tag instead.

## 4. Honesty statement

**No rollback in this document — automatic or manual — has been
executed against a real cluster in this session or any prior one.**
No real GKE cluster has ever been reachable from any session that
worked on this repository (see `docs/deployment.md` §6). `kubectl
rollout undo`'s behavior described above is Kubernetes' own documented
mechanism, not something this project has independently verified by
breaking a real deployment and watching it recover. Doing so — for the
first time — is `scripts/k8s-smoke-test.sh --full`'s explicit purpose
once a real cluster exists.
