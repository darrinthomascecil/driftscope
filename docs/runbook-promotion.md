# Runbook: promoting a version dev → staging → prod

Phase 0.6d ships a **build-once / promote-the-digest** ladder across three isolated clusters. This is
the day-2 operational guide. Design: `docs/design-multi-env-0.6d.md`.

## The model in one line

An image is **built + signed exactly once** (when a change merges to `main`, into the **shared** ACR).
Promotion never rebuilds — it moves the **same digest** up the ladder by pointing each env's overlay
`newTag` at it. Because the ACR is shared, every cluster's kubelet can already pull that digest.

```
merge → main        →  dev builds vX.Y.Z, overlays/dev/newTag = vX.Y.Z            (env: dev)
git tag vX.Y.Z      →  overlays/staging/newTag = vX.Y.Z@sha256:…  (no rebuild)    (env: staging)
GitHub release vX.Y.Z → overlays/prod/newTag   = vX.Y.Z@sha256:…  (no rebuild)    (env: production)
```

Each hop is a `[skip ci]` commit to `main` by `.github/workflows/deploy.yml`; that env's Argo CD
(auto-sync + selfHeal) reconciles it. You never run `kubectl`.

## Promote to staging

1. Confirm dev is healthy on the version you want to promote — check `overlays/dev/kustomization.yaml`
   `newTag` (e.g. `v0.1.7`) and dev's Argo `Application` is **Synced/Healthy**.
2. Tag that version and push the tag:
   ```sh
   git tag v0.1.7 && git push origin v0.1.7
   ```
3. `deploy` runs on the tag: it resolves `v0.1.7`'s digest from the shared ACR (**no rebuild**) and
   commits `overlays/staging/newTag = v0.1.7@sha256:…`. Staging's Argo rolls it out.
   - The tag also triggers `release.yml` (public GHCR publish) — that is separate and expected.
   - If the job errors *"v0.1.7 has no image in the shared ACR"*, dev never built that version — merge
     it to main first so dev builds `v0.1.7`, then tag.

## Promote to prod

1. Soak on staging. When satisfied, publish a **GitHub Release** for the same tag `v0.1.7`.
2. `deploy` runs on `release: published`: it sets `overlays/prod/newTag = v0.1.7@sha256:…` (same
   digest). Prod's Argo rolls it out.
3. If `production` has **required reviewers** (recommended), the job pauses for approval first.

## Verify a promotion ran the identical artifact

```sh
# per env, after Argo syncs:
kubectl --context <env> -n driftscope get deploy driftscope \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
# staging and prod must show the SAME @sha256:… digest; ACR shows ONE build for the version.
```

## Standing up / tearing down an environment

```sh
make shared-up                       # ONCE: shared ACR + DNS zone + state backend
make up   ENV=dev                    # dev end-to-end (default ENV=dev)
make up   ENV=staging
make up   ENV=prod
make down ENV=dev                    # tear down ONE env (its DNS records, NOT the shared zone)
```

`make up` runs `kv-recover` before `infra`: Key Vault purge protection reserves a destroyed vault's
name for 7 days, so a same-name rebuild would otherwise fail — `az keyvault recover` handles it
automatically. The shared DNS zone (and its registrar NS delegation) is never touched by `make down`.

## Gotchas

- **Never** stack commits or rebuild to promote — always tag/release so the same digest moves.
- Keep `istio_revision` equal on staging and prod before promoting a mesh change.
- The pointer-bump commits (`chore(deploy): <env> -> …`) are `[skip ci]` and pushed with
  `GITHUB_TOKEN`, so they never re-trigger the pipeline (no deploy loop). A single `concurrency` group
  serializes them, so a dev merge and a staging promotion can't race a non-fast-forward push.
