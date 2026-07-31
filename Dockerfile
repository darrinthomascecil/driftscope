# driftscope in a container.
#
# The tool is pure standard library, so the Python side of this image is trivial:
# there is nothing to resolve, pin or audit. What the image exists to supply is
# the *external* surface driftscope shells out to --
#
#   git   clones the scan target (`--depth 1`, hooks disabled)
#   gh    is the only way driftscope obtains a GitHub token (see below)
#
#   docker build -t driftscope .        # 414MB
#   docker build --target test .        # runs the suite, ships nothing
#
# Sizes are measured on linux/arm64, not estimated. Most of the 414MB is
# python:3.14-slim itself; driftscope's own wheel is under 300KB, which is what
# having no dependencies buys.
#
# ---------------------------------------------------------------------------
# Why `gh` is installed rather than an env var being passed
#
# driftscope reads its GitHub token exclusively via `gh auth token`
# (driftscope/http.py:gh_token) -- there is no GITHUB_TOKEN fallback in the code.
# Fortunately `gh auth token` itself resolves GH_TOKEN/GITHUB_TOKEN from the
# environment, so shipping the ~12MB binary makes `-e GITHUB_TOKEN=...` work with
# no change to driftscope. That indirection is worth knowing about: without a
# token the GitHub API allows 60 requests/hour, and driftscope's response to
# exhausting it is `counts_available=False` -- degraded findings, not an error.
# An unauthenticated container therefore produces a *quieter* report, not a
# failing one, which is the more dangerous outcome.
#
# Azure DevOps needs nothing extra: `source.ado_credential()` reads a token from
# the environment, so this image scans ADO without azure-cli in it. See the usage
# notes at the foot of this file.
# ---------------------------------------------------------------------------
#
# The base is pinned to a minor version rather than a digest, deliberately: a
# digest freezes out Debian security updates until someone remembers to bump it,
# and this image's whole job is telling people they are out of date.

ARG PYTHON_VERSION=3.14
ARG GH_VERSION=2.96.0


# --------------------------------------------------------------- build the wheel
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /src
COPY pyproject.toml README.md ./
COPY driftscope ./driftscope

# `dependencies = []` is a deliberate property of this project, so there is no
# dependency layer to cache. `--no-deps` at install time below is an assertion:
# if it ever starts failing, something added a runtime dependency and the claim
# in pyproject.toml has quietly stopped being true.
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist


# ------------------------------------------------------------- fetch the gh CLI
FROM python:${PYTHON_VERSION}-slim AS ghcli
ARG GH_VERSION
# TARGETARCH is supplied by BuildKit and is already Go-style (amd64/arm64), which
# matches gh's release asset naming exactly -- so an arm64 Mac and an amd64 CI
# runner both build from this one line.
ARG TARGETARCH

# Checksums from the release's own gh_${GH_VERSION}_checksums.txt. Pinning the
# version alone is not enough: a release asset can be replaced, and this binary
# is handed a GitHub token at runtime. Verifying content means a tampered or
# truncated download fails the build instead of shipping.
ARG GH_SHA256_amd64=83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60
ARG GH_SHA256_arm64=06f86ec7103d41993b76cd78072f43595c34aaa56506d971d9860e67140bf909

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH}" in \
      amd64) sha="${GH_SHA256_amd64}" ;; \
      arm64) sha="${GH_SHA256_arm64}" ;; \
      *) echo "no pinned gh checksum for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    echo "${sha}  /tmp/gh.tgz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh" /usr/local/bin/gh; \
    gh --version


# ------------------------------------------------------------------ runtime base
FROM python:${PYTHON_VERSION}-slim AS runtime

# git is not optional -- a remote target is cloned, not downloaded. ca-certificates
# is what lets urllib verify OSV, the package registries and the forge APIs; TLS
# failures there would surface as unreachable registries, which driftscope reports
# as `unknown` rather than as current.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcli /usr/local/bin/gh /usr/local/bin/gh

# An ordinary install into site-packages, which puts the console script on PATH
# by itself. (An earlier revision installed to a side directory and wired it up
# with PYTHONPATH plus a symlink into the wheel's bin/. That worked, but it hand-
# rolled what pip already does and depended on the wheel's internal layout, for
# no benefit -- it costs ~15MB of pip metadata to just do it properly.)
# COPY (not a BuildKit `RUN --mount`) so the image builds on the classic builder too --
# e.g. `az acr build`, which the bootstrap uses so no local Docker/BuildKit is required. The
# wheel is <300KB; removing it in the same layer keeps it out of the final image.
COPY --from=builder /dist /tmp/dist
RUN pip install --no-cache-dir --no-deps /tmp/dist/*.whl && rm -rf /tmp/dist

# Scanning is a read-only operation on untrusted repositories: nothing here needs
# root, and `git clone` runs with hooks disabled precisely because the target is
# not trusted. Dropping privileges is the cheap second layer.
RUN useradd --create-home --uid 1000 drift

# A repo bind-mounted from the host is owned by the host's uid, which git rejects
# as "dubious ownership". Scoped to the documented mount point rather than the
# `*` wildcard -- the exemption should cover the one path we tell people to use,
# not every path in the filesystem. Mounting a repo somewhere else needs its own
# entry.
RUN git config --system --add safe.directory /work

# The HTTP cache is the difference between a 4-minute scan and a 20-second one,
# so it is worth persisting: `-v driftscope-cache:/cache`. Without that flag this
# is just a directory in the container's writable layer and still works.
#
# Deliberately not a `VOLUME`: declaring one would make every `docker run` that
# forgets the flag leave an anonymous volume behind to be garbage-collected by
# hand, and it would silently discard anything written here in later layers.
ENV DRIFTSCOPE_CACHE=/cache
RUN mkdir -p /cache && chown drift:drift /cache

USER drift
# Reports are written relative to the working directory, so `-v "$PWD:/work"` puts
# them straight onto the host.
WORKDIR /work

ENTRYPOINT ["driftscope"]
CMD ["--help"]


# ------------------------------------------------------------------------- tests
# Kept in the same file so the suite runs against the image's interpreter and its
# real git, not the host's. Nothing from this stage reaches the shipped image.
FROM runtime AS test
USER root
RUN pip install --no-cache-dir pytest
COPY tests /src/tests
COPY tools /src/tools
COPY fixtures /src/fixtures
COPY pyproject.toml /src/
WORKDIR /src
# Network-marked tests hit live registries; a build must not depend on the
# internet being reachable or on a rate limit not being hit.
RUN python -m pytest -q -m "not network"


# ------------------------------------------------------------- az-enabled cloud
# Opt-in variant for running in Azure: `docker build --target cloud -t
# driftscope:cloud`. The default slim image (414MB) is unchanged and stays the
# last stage below.
#
# Why az is here when the base deliberately omits it: three paths shell to the az
# CLI, not to an env token -- the --live-azure estate scan (live/azure.py gates on
# `az account show`), ACR image drift (`az acr login --expose-token`), and the ARM
# api-version resolver. Bundling az lets all three authenticate through a managed
# identity with no stored secret, via the entrypoint's `az login --identity`.
#
# az is installed from Microsoft's signed apt repo, pinned by the repo GPG key --
# NOT the `curl https://aka.ms/... | bash` pipe the old `azure` target ran. The
# `bookworm` suite is deliberate: trixie is not published on the az repo yet, and
# the azure-cli deb bundles its own Python, so the bookworm package runs on this
# trixie base unchanged.
FROM runtime AS cloud
USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg; \
    chmod go+r /etc/apt/keyrings/microsoft.gpg; \
    arch="$(dpkg --print-architecture)"; \
    printf 'Types: deb\nURIs: https://packages.microsoft.com/repos/azure-cli/\nSuites: bookworm\nComponents: main\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/microsoft.gpg\n' "$arch" \
      > /etc/apt/sources.list.d/azure-cli.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends azure-cli; \
    apt-get purge -y gnupg; apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    az version

COPY docker/cloud-entrypoint.sh /usr/local/bin/cloud-entrypoint.sh
RUN chmod 0755 /usr/local/bin/cloud-entrypoint.sh

USER drift
# az writes its token cache under $HOME/.azure; drift owns /home/drift, so a
# managed-identity login has somewhere to land without extra chown.
ENTRYPOINT ["cloud-entrypoint.sh"]
CMD ["--help"]


# ----------------------------------------------------------------- default stage
# This must stay the LAST stage in the file. `docker build` with no `--target`
# builds the final stage, so ending on anything else silently hands out the wrong
# image to anyone who typed the documented command -- a difference nothing in the
# output announces. (This bit once, when `azure` was last.)
FROM runtime AS default

# ---------------------------------------------------------------------------
# Scanning Azure DevOps
#
# No extra image is needed. `source.ado_credential()` checks the environment
# before shelling out to `az`, and each variable carries its own auth scheme --
# a PAT must be sent as Basic, an AAD token as Bearer, and sending one as the
# other returns 401, which driftscope converts into a report with fewer findings
# rather than an error.
#
#   # Personal Access Token -> Basic
#   docker run --rm -e AZURE_DEVOPS_EXT_PAT -v "$PWD:/work" \
#     driftscope scan https://dev.azure.com/org/proj/_git/repo --html drift.html
#
#   # Azure Pipelines job that opts into System.AccessToken -> Bearer
#   docker run --rm -e SYSTEM_ACCESSTOKEN -v "$PWD:/work" driftscope scan ...
#
#   # a host `az login`, with no azure-cli in the image at all
#   docker run --rm -v "$PWD:/work" \
#     -e AZURE_DEVOPS_TOKEN="$(az account get-access-token \
#         --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv)" \
#     driftscope scan https://dev.azure.com/org/proj/_git/repo --html drift.html
#
# An earlier revision of this file shipped an `azure` target that installed
# azure-cli via `curl https://aka.ms/InstallAzureCLIDeb | bash`. That piped an
# unpinned, unverified script from a redirector into a root shell at build time,
# for 790MB, to obtain a token the environment can now supply directly. It is
# gone rather than fixed.
# ---------------------------------------------------------------------------
