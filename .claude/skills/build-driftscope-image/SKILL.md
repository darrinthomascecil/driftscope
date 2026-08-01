---
name: build-driftscope-image
description: Build or push the driftscope container image correctly. Invoke whenever wiring or running an image build — CI/CD (az acr build), scripts/bootstrap/image.sh, or local docker. Encodes the TARGETARCH build-arg rule that has broken the build repeatedly.
---

# Building the driftscope image

The Dockerfile's `gh` CLI stage declares `ARG TARGETARCH` and does `case "${TARGETARCH}"` under
`set -eux`. So an **unset `TARGETARCH` hard-fails the build**:

```
/bin/sh: 1: TARGETARCH: parameter not set
... returned a non-zero code: 2
```

`TARGETARCH` is auto-populated **only by BuildKit**. Every other builder leaves it empty.

## The rule (check this FIRST when an image build fails)

| Builder | TARGETARCH | Action |
|---|---|---|
| `docker buildx` / `docker/build-push-action@v6` (CI `release.yml`) | automatic | nothing |
| `az acr build` (deploy pipeline, ACR Tasks) | **NOT set** | pass `--build-arg TARGETARCH=amd64` |
| classic `docker build` (BuildKit off) | **NOT set** | pass `--build-arg TARGETARCH=amd64` |

`amd64` is the AKS node arch we deploy to. The Dockerfile also defaults `ARG TARGETARCH=amd64` as a
backstop, but still pass the build-arg explicitly in non-BuildKit paths — intent stays visible and a
deliberate arm64 build still works.

## Known-good invocations

```bash
# ACR server-side build (what .github/workflows/deploy.yml uses to feed the AKS pull):
az acr build --registry <acr> --image driftscope:<tag> \
  --platform linux/amd64 --build-arg TARGETARCH=amd64 --file Dockerfile .

# Local / classic docker (BuildKit disabled):
DOCKER_BUILDKIT=0 docker build --build-arg TARGETARCH=amd64 -t driftscope .

# BuildKit (TARGETARCH is automatic — do NOT need the build-arg):
docker buildx build --platform linux/amd64 -t driftscope .
```

## Where it bites
- `.github/workflows/deploy.yml` — `az acr build`
- `scripts/bootstrap/image.sh` — if it builds via `az acr build` or the classic builder

If a driftscope image build ever errors on `TARGETARCH`, the fix is always: add
`--build-arg TARGETARCH=amd64` to that build path.
