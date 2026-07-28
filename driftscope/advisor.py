"""Severity scoring and upgrade-safety advice.

Severity answers "how bad is this", advice answers "how do I move without an
outage". Both are rule-based and inspectable on purpose -- a customer-facing
staleness report has to be arguable, so every score traces to a stated rule
rather than a model's opinion.

Scoring inputs, in rough order of weight:
  * removed/deprecated upstream          -> critical (it will break, not might)
  * major versions behind                 -> high
  * age of the pinned release             -> escalates medium -> high
  * unpinned/floating version             -> medium regardless of number
  * minor/patch only                      -> low
"""

from __future__ import annotations

from .models import (
    AZURE_API,
    DOTNET_SDK,
    DOTNET_TFM,
    JVM_TOOLCHAIN,
    RUBY_RUNTIME,
    MAVEN,
    NUGET,
    AZURE_NODE_IMAGE,
    AZURE_SKU,
    DOCKER,
    GO,
    HELM,
    K8S_API,
    K8S_CLUSTER,
    NPM,
    PYPI,
    SEV_CRITICAL,
    SEV_CURRENT,
    SEV_HIGH,
    SEV_LOW,
    SEV_MEDIUM,
    SEV_UNKNOWN,
    TERRAFORM_MODULE,
    TERRAFORM_PROVIDER,
    Advice,
    Component,
    GitHubDelta,
    Lag,
    Resolution,
)
from .scanners.cicd import GH_ACTION

# Days-behind thresholds
STALE_DAYS_HIGH = 730  # two years
STALE_DAYS_MEDIUM = 365

# AKS node images ship weekly and carry OS security patches, so they need their
# own much tighter scale. Microsoft's own guidance is to patch node images
# monthly; these thresholds are multiples of that cadence.
NODE_IMAGE_LOW_DAYS = 30
NODE_IMAGE_MEDIUM_DAYS = 60
NODE_IMAGE_HIGH_DAYS = 120
NODE_IMAGE_CRITICAL_DAYS = 240

# Runtime/toolchain support-expiry thresholds. A runtime going out of support is a
# hard date with a long lead time, so these are generous compared to node images.
RUNTIME_EOL_SOON_DAYS = 180
RUNTIME_EOL_WATCH_DAYS = 365


def score(component: Component, res: Resolution, lag: Lag) -> str:
    if res.error and not res.latest_stable:
        return SEV_UNKNOWN
    if res.deprecated:
        return SEV_CRITICAL

    # Floating/unpinned is a finding in its own right and has no "latest" to
    # compare against, so it must be scored before the resolvability check.
    if component.meta.get("floating") or component.meta.get("untagged"):
        return SEV_MEDIUM

    # AKS node images are scored on age alone, on a much tighter scale than
    # everything else: Microsoft ships them roughly weekly and they carry OS
    # security patches, so "a few months old" means unpatched kernel/OS CVEs.
    # Generic year-scale thresholds would rate a dangerously stale node pool LOW.
    if component.ecosystem == AZURE_NODE_IMAGE:
        age = lag.current_age_days
        if age is None:
            return SEV_UNKNOWN
        if age >= NODE_IMAGE_CRITICAL_DAYS:
            return SEV_CRITICAL
        if age >= NODE_IMAGE_HIGH_DAYS:
            return SEV_HIGH
        if age >= NODE_IMAGE_MEDIUM_DAYS:
            return SEV_MEDIUM
        if age >= NODE_IMAGE_LOW_DAYS:
            return SEV_LOW
        return SEV_CURRENT

    # Runtimes and toolchains are judged on support status, not version distance.
    # Java 17 is eight "majors" behind 25 and is a supported LTS -- rating that
    # CRITICAL would train people to ignore the report.
    if component.ecosystem in (JVM_TOOLCHAIN, DOTNET_TFM, DOTNET_SDK,
                               RUBY_RUNTIME):
        days_left = res.extra.get("support_days_left")
        is_lts = res.extra.get("lts")
        if res.deprecated:
            return SEV_CRITICAL  # already out of support
        if days_left is not None:
            if days_left < 0:
                return SEV_CRITICAL
            if days_left <= RUNTIME_EOL_SOON_DAYS:
                return SEV_HIGH
            if days_left <= RUNTIME_EOL_WATCH_DAYS:
                return SEV_MEDIUM
            return SEV_LOW if lag.behind_major else SEV_CURRENT
        if is_lts is False:
            return SEV_HIGH  # non-LTS: six-month window, no time to plan
        if is_lts is True:
            # Supported with no announced end date -- the healthiest state a
            # runtime can be in. A null `days_left` is ambiguous on its own: it
            # means both "no data" and "supported indefinitely". Without this
            # branch a fully maintained line was rated MEDIUM purely for being one
            # major behind, which is the same mistake as rating Java 17 CRITICAL
            # that this whole block exists to avoid. Covers Ruby's normal-
            # maintenance branches and .NET Framework 4.7/4.8.
            return SEV_LOW if lag.behind_major else SEV_CURRENT
        # No support data at all: fall back to distance, but never above MEDIUM,
        # because "N majors behind" is a poor proxy for runtime risk.
        return SEV_MEDIUM if lag.behind_major else SEV_CURRENT

    # Ordering note: the node image branch is deliberately above these checks.
    # Node image version strings are opaque and never comparable, and AKS may
    # report no upgrade target, so both guards below would swallow the finding.
    if not res.latest_stable:
        return SEV_UNKNOWN
    if component.current is None:
        return SEV_UNKNOWN
    if not lag.comparable:
        return SEV_UNKNOWN

    # K8s APIs are binary: removed, deprecated, or fine.
    if component.ecosystem == K8S_API:
        note = (res.note or "").lower()
        if "removed in" in note:
            return SEV_CRITICAL
        if "deprecated" in note:
            return SEV_HIGH
        return SEV_CURRENT

    if lag.releases_behind == 0 and lag.behind_major == 0 and lag.behind_minor == 0 and lag.behind_patch == 0:
        return SEV_CURRENT

    sev = SEV_LOW
    if lag.behind_major >= 2:
        sev = SEV_CRITICAL
    elif lag.behind_major == 1:
        sev = SEV_HIGH
    elif lag.behind_minor >= 5:
        sev = SEV_MEDIUM
    elif lag.behind_minor >= 1:
        sev = SEV_LOW

    days = lag.days_behind or 0
    if days >= STALE_DAYS_HIGH and sev in (SEV_LOW, SEV_MEDIUM):
        sev = SEV_HIGH
    elif days >= STALE_DAYS_MEDIUM and sev == SEV_LOW:
        sev = SEV_MEDIUM

    # A preview API version in production IaC is its own risk class.
    if component.ecosystem == AZURE_API and component.meta.get("preview"):
        sev = SEV_HIGH if sev in (SEV_LOW, SEV_MEDIUM, SEV_CURRENT) else sev

    return sev


def _intermediate_majors(res: Resolution, current: str | None, target: str | None) -> list[str]:
    """Highest release on each intervening major line, for staged upgrades."""
    from .version import parse

    if not current or not target:
        return []
    cv, tv = parse(current), parse(target)
    if not cv.valid or not tv.valid or tv.major - cv.major < 2:
        return []
    best: dict[int, object] = {}
    for r in res.releases:
        pv = parse(r.version)
        if not pv.valid or pv.is_prerelease:
            continue
        if cv.major < pv.major < tv.major:
            cur = best.get(pv.major)
            if cur is None or pv.sort_key() > cur.sort_key():
                best[pv.major] = pv
    return [best[k].raw for k in sorted(best)]


# Maven groups that ship many artifacts on one version line, where a partial
# upgrade silently mixes versions. Not exhaustive; membership only adds a caution.
_MULTIMODULE_GROUPS = (
    "com.fasterxml.jackson",
    "org.springframework",
    "io.netty",
    "org.junit",
    "org.apache.logging.log4j",
    "io.micrometer",
    "org.hibernate",
    "com.google.cloud",
    "software.amazon.awssdk",
    "io.grpc",
    "org.jetbrains.kotlin",
    "io.quarkus",
)


def _is_multimodule(coordinate: str) -> bool:
    group = coordinate.split(":", 1)[0]
    return any(group == g or group.startswith(g + ".") for g in _MULTIMODULE_GROUPS)


def advise(
    component: Component,
    res: Resolution,
    lag: Lag,
    severity: str,
    delta: GitHubDelta | None = None,
) -> Advice:
    eco = component.ecosystem
    target = res.latest_stable
    a = Advice(risk=severity, target_version=target)
    steps: list[str] = []
    cautions: list[str] = []

    # ---- strategy by distance
    if severity == SEV_CURRENT:
        a.strategy = "No action: already at the latest stable version."
        return a
    if severity == SEV_UNKNOWN:
        if component.meta.get("unresolved_expr"):
            a.strategy = (
                f"Cannot assess: version is the expression "
                f"`{component.meta['unresolved_expr']}`, not a literal. Resolve the "
                "variable's default (or the lock file) to get a real answer."
            )
        elif component.current is None and component.meta.get("sha_pinned"):
            a.strategy = (
                "Cannot assess: pinned to a commit SHA. Map the SHA to a tag "
                "(`gh api repos/OWNER/REPO/commits/SHA`) to measure lag."
            )
        elif component.current is None:
            a.strategy = "Cannot assess: no concrete version declared at this site."
        else:
            a.strategy = "Cannot assess: " + (res.error or res.note or "version not comparable")
        if lag.reason and component.current:
            cautions.append(lag.reason)
        if res.error:
            cautions.append(res.error)
        a.cautions = cautions
        return a

    inter = _intermediate_majors(res, component.current, target)
    a.intermediate_versions = inter

    # Calendar versions have no majors; the vocabulary has to change with them.
    from .version import CALVER, parse as _vparse

    unit = "api-version" if _vparse(component.current or "").kind == CALVER else "major"

    if lag.behind_major >= 2 and inter:
        a.strategy = (
            f"Staged upgrade. Do not jump {component.current} -> {target} in one "
            f"change; step through {', '.join(inter)} so each {unit}'s breaking "
            "changes are isolated and revertible."
        )
    elif lag.behind_major >= 1:
        a.strategy = (
            f"Single major upgrade {component.current} -> {target}. Expect breaking "
            "changes; read the migration guide before editing anything."
        )
    elif lag.behind_minor >= 1:
        a.strategy = (
            f"Minor upgrade {component.current} -> {target}. Additive by convention; "
            "verify with tests rather than a full migration review."
        )
    else:
        a.strategy = f"Patch upgrade {component.current} -> {target}. Low risk; batch with other patches."

    # ---- ecosystem-specific mechanics
    if eco == TERRAFORM_PROVIDER:
        if component.meta.get("core"):
            steps += [
                f"Bump required_version, then run `terraform version` on the new binary",
                "Run `terraform plan` on every workspace: state upgrades are one-way",
            ]
            cautions.append("State written by a newer Terraform cannot be read by an older one")
        else:
            steps += [
                f"Set version = \"~> {target}\" in required_providers",
                "Run `terraform init -upgrade` to refresh .terraform.lock.hcl",
                "Run `terraform plan` and read every diff: provider majors change "
                "resource schemas, so a plan may show replacements you did not intend",
            ]
            if lag.behind_major >= 1:
                cautions.append(
                    "Provider major bumps can force resource replacement. Check the plan "
                    "for `must be replaced` before applying to production"
                )
            cautions.append("Commit the updated lock file so CI resolves identically")
    elif eco == TERRAFORM_MODULE:
        steps += [
            f"Bump the module version to {target}",
            "Run `terraform init -upgrade` then `plan`; module majors often rename inputs",
        ]
    elif eco == AZURE_API:
        steps += [
            f"Change the api-version on `{component.name}` to {target}",
            "Run `az deployment group what-if` (or `--confirm-with-what-if`) against a "
            "non-production resource group and diff the result",
        ]
        cautions.append(
            "Newer api-versions can add required properties or change defaults; "
            "what-if is the only reliable way to see it before it applies"
        )
        if component.meta.get("preview"):
            cautions.append(
                "Currently on a preview api-version: preview APIs can be withdrawn "
                "without notice and are not covered by the Azure SLA"
            )
    elif eco == DOCKER:
        if component.meta.get("floating"):
            a.strategy = (
                "Pin this image. A floating tag means the running artifact changes "
                "without a commit, so builds are not reproducible and a base-image "
                "regression is untraceable."
            )
            steps += [
                f"Replace `{component.raw}` with an explicit version tag",
                "Better: pin by digest (`image@sha256:...`) and update it deliberately",
            ]
        else:
            steps += [
                f"Update the FROM tag to {target}",
                "Rebuild and run the image's own test suite; base images change libc, "
                "OpenSSL and CA bundles between majors",
            ]
            if component.meta.get("digest"):
                steps.append("Update the pinned digest alongside the tag")
    elif eco == HELM:
        steps += [
            f"Bump the dependency to {target} and run `helm dependency update`",
            "Run `helm diff upgrade` (helm-diff plugin) before applying",
        ]
        cautions.append("Chart majors frequently rename values keys; your values.yaml may silently stop applying")
    elif eco == K8S_API:
        steps += [
            f"Rewrite the manifest to `{(res.note or '').split('migrate to ')[-1] or target}`",
            "Run `kubectl apply --dry-run=server` to confirm the new version is served",
        ]
        cautions.append(
            "If the cluster has already removed this version, `kubectl get` will not "
            "return existing objects; migrate before upgrading the cluster"
        )
    elif eco == K8S_CLUSTER:
        steps += [
            f"Upgrade the control plane to {target}, then node pools, one at a time",
            "Check API deprecations for the target version first (`kubectl-convert`, "
            "or the deprecation guide)",
        ]
        cautions.append("AKS allows at most one minor version skip per upgrade")
    elif eco == NUGET:
        central = component.meta.get("central")
        steps += [
            (
                "Bump the version in Directory.Packages.props (this project uses "
                "Central Package Management)"
                if central
                else f"Set Version=\"{target}\" on the PackageReference"
            ),
            "Run `dotnet restore` then `dotnet build -warnaserror:NU1605` to catch "
            "downgrade conflicts introduced by transitive pins",
            "Run the test suite; for a major, read the package's migration notes first",
        ]
        cautions.append(
            "`dotnet restore` reports known vulnerabilities as NU1901-NU1904 "
            "warnings - check the build log, it may already be flagging this package"
        )
        if lag.behind_major >= 1:
            cautions.append(
                "A major NuGet bump can change the target framework floor; if the "
                "new version requires a newer TFM the project will not compile"
            )
    elif eco == MAVEN:
        if component.meta.get("parent"):
            a.strategy = (
                f"Upgrade the parent POM to {target}. This moves every managed "
                "dependency version at once, so treat it as a fan-out change rather "
                "than a single bump."
            )
            steps.append(f"Set the <parent> version to {target}")
        elif component.meta.get("unresolved_expr"):
            steps.append(
                f"The version is `{component.meta['unresolved_expr']}`; update the "
                "property (or the parent that defines it), not the dependency block"
            )
        else:
            steps.append(f"Set the <version> to {target}")
        steps += [
            "Run `mvn versions:display-dependency-updates` to see the full picture, "
            "then `mvn -U dependency:tree` to check for conflicting transitive pulls",
            "Run `mvn verify`; Maven resolves nearest-wins, so a bump can silently "
            "change which version of a shared transitive dependency you get",
        ]
        # Membership of a multi-module family is a better trigger than distance:
        # Jackson four minors behind is exactly the case where a partial upgrade
        # mixes versions across jackson-core/databind/annotations.
        if _is_multimodule(component.name) or lag.behind_minor >= 5 or lag.behind_major >= 1:
            cautions.append(
                "Check for a matching BOM: upgrading one artifact of a multi-module "
                "project (Jackson, Spring, Netty) without the rest mixes versions"
            )
    elif eco in (PYPI, NPM, GO):
        mgr = {PYPI: "pip", NPM: "npm", GO: "go"}[eco]
        steps += [
            f"Update the constraint to {target} and refresh the lock file ({mgr})",
            "Run the test suite; for a major, read the changelog for removed APIs first",
        ]
        if eco == NPM and component.meta.get("runtime"):
            cautions.append("Runtime bumps affect every dependency's native modules; rebuild from clean")
    elif eco == GH_ACTION:
        if component.meta.get("runner"):
            steps += [
                f"Change `runs-on` to {target}",
                "Expect preinstalled tool versions to differ; pin tool versions explicitly",
            ]
            cautions.append("Retired runner labels fail the whole workflow, not a single step")
        else:
            steps += [
                f"Update `uses: {component.name}@v{target}`",
                "Consider pinning to a commit SHA instead of a tag for supply-chain safety",
            ]
    elif eco == AZURE_NODE_IMAGE:
        pool = component.meta.get("pool")
        cluster = component.meta.get("cluster")
        scope = f" -n {pool} --cluster-name {cluster}" if pool and cluster else ""
        age = lag.current_age_days
        a.strategy = (
            f"Upgrade the node image. This pool's image is "
            f"{age} days old" + (" — it is missing every OS security patch released since."
            if age and age >= NODE_IMAGE_MEDIUM_DAYS else ".")
        )
        steps += [
            f"`az aks nodepool upgrade{scope} --node-image-only`",
            "Node image upgrades cordon and drain nodes: confirm PodDisruptionBudgets "
            "allow it, and that the pool has surge capacity (`--max-surge`)",
            "Enable automatic node image upgrades so this does not recur: "
            "`az aks nodepool update --auto-upgrade-channel NodeImage`",
        ]
        cautions.append(
            "This replaces nodes. Stateful workloads without correct PDBs and "
            "readiness probes can lose availability during the roll"
        )
        if age and age >= NODE_IMAGE_HIGH_DAYS:
            cautions.append(
                "At this age the kernel and container runtime are also behind; "
                "review the AgentBaker changelog for runtime behaviour changes"
            )
    elif eco == AZURE_SKU:
        steps += [
            f"Plan a move to {target}",
            "Check quota and regional availability for the target before changing anything",
        ]
        cautions.append("SKU changes on a live deployment can require recreate, not update")

    # ---- fold in GitHub delta signals
    if delta:
        if delta.breaking_notes:
            cautions.append(
                f"{len(delta.breaking_notes)} breaking-change note(s) found in release "
                "notes between these versions - read them before upgrading"
            )
        if delta.counts_available and (delta.open_bug_count or 0) >= 20:
            cautions.append(
                f"{delta.open_bug_count} open bug-labelled issues were filed in this "
                "version window; consider targeting one release below the newest"
            )
        elif delta.counts_available and delta.open_bug_count:
            cautions.append(
                f"{delta.open_bug_count} open bug-labelled issue(s) filed in the window"
            )

    # res.note is rendered on its own line by the reporters, so appending it here
    # too produced the same sentence twice in every finding.
    steps.append("Change one component per commit so a regression bisects cleanly")
    a.steps = steps
    a.cautions = cautions
    return a
