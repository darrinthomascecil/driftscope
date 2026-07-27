"""Scanner tests against the synthetic stale estate. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from driftscope.models import (
    AZURE_API,
    DOCKER,
    GO,
    HELM,
    K8S_API,
    NPM,
    PYPI,
    TERRAFORM_MODULE,
    TERRAFORM_PROVIDER,
)
from driftscope.scanners import scan_tree
from driftscope.scanners.cicd import GH_ACTION
from driftscope.scanners.containers import parse_image_ref

FIXTURE = Path(__file__).parent.parent / "fixtures" / "stale-estate"


@pytest.fixture(scope="module")
def components():
    comps, files = scan_tree(FIXTURE)
    assert files > 10, "fixture tree should have been walked"
    return comps


def find(components, ecosystem, name):
    for c in components:
        if c.ecosystem == ecosystem and c.name.lower() == name.lower():
            return c
    return None


def of(components, ecosystem):
    return [c for c in components if c.ecosystem == ecosystem]


# ---------------------------------------------------------------- terraform


def test_provider_from_required_providers(components):
    c = find(components, TERRAFORM_PROVIDER, "hashicorp/azurerm")
    assert c is not None
    # The lock file is authoritative and wins the dedupe on exact version.
    locked = [x for x in of(components, TERRAFORM_PROVIDER)
              if x.name == "hashicorp/azurerm" and x.meta.get("locked")]
    assert locked, "lock file entry should be captured"
    assert locked[0].current == "3.75.0"


def test_constraint_floor_extracted(components):
    cs = [c for c in of(components, TERRAFORM_PROVIDER)
          if c.name == "hashicorp/azurerm" and not c.meta.get("locked")]
    assert cs and cs[0].current == "3.0"
    assert cs[0].raw == "~> 3.0"


def test_unresolvable_version_expression_not_guessed(components):
    c = find(components, TERRAFORM_PROVIDER, "hashicorp/kubernetes")
    assert c is not None
    assert c.current is None, "version = var.x must not be invented"
    assert "unresolved_expr" in c.meta


def test_terraform_core_version(components):
    c = find(components, TERRAFORM_PROVIDER, "hashicorp/terraform")
    assert c is not None and c.meta.get("core") is True
    assert c.current == "1.3"


def test_registry_module_vs_local(components):
    reg = find(components, TERRAFORM_MODULE, "Azure/naming/azurerm")
    assert reg is not None and reg.meta["registry"] is True
    assert reg.current == "0.3.0"
    local = find(components, TERRAFORM_MODULE, "../modules/local")
    assert local is not None and local.meta["registry"] is False


# --------------------------------------------------------------- azure iac


def test_bicep_api_versions(components):
    c = find(components, AZURE_API, "Microsoft.CognitiveServices/accounts")
    assert c is not None and c.current == "2023-05-01"
    assert c.meta["format"] == "bicep"
    assert c.meta["preview"] is False


def test_bicep_preview_flagged(components):
    c = find(components, AZURE_API, "Microsoft.App/containerApps")
    assert c is not None and c.meta["preview"] is True


def test_arm_json_api_version(components):
    c = find(components, AZURE_API, "Microsoft.Storage/storageAccounts")
    assert c is not None and c.current == "2021-04-01"
    assert c.meta["format"] == "arm"


def test_arm_schema_captured(components):
    c = find(components, AZURE_API, "ARM/deploymentTemplateSchema")
    assert c is not None and c.current == "2019-04-01"


# ------------------------------------------------------------------- k8s


def test_k8s_removed_api_found(components):
    c = find(components, K8S_API, "extensions/Ingress")
    assert c is not None and c.current == "v1beta1"


def test_k8s_stable_api_found(components):
    c = find(components, K8S_API, "apps/Deployment")
    assert c is not None and c.current == "v1"


def test_k8s_beta_hpa(components):
    c = find(components, K8S_API, "autoscaling/HorizontalPodAutoscaler")
    assert c is not None and c.current == "v2beta2"


def test_image_inside_manifest_found(components):
    c = find(components, DOCKER, "myregistry.example.com/api")
    assert c is not None and c.current == "1.4.0"


# ---------------------------------------------------------------- docker


def test_dockerfile_images(components):
    c = find(components, DOCKER, "python")
    assert c is not None and c.current == "3.8-slim"
    n = find(components, DOCKER, "node")
    assert n is not None and n.current == "14-alpine"


def test_multistage_self_reference_skipped(components):
    assert find(components, DOCKER, "builder") is None


def test_compose_images_and_floating_tag(components):
    assert find(components, DOCKER, "redis").current == "5"
    nginx = find(components, DOCKER, "nginx")
    assert nginx is not None and nginx.meta["floating"] is True


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("python:3.12-slim", ("python", "3.12-slim", None)),
        ("nginx", ("nginx", None, None)),
        ("registry.io:5000/team/app:1.2", ("registry.io:5000/team/app", "1.2", None)),
        ("img@sha256:abc", ("img", None, "sha256:abc")),
        ("ghcr.io/org/img:v2", ("ghcr.io/org/img", "v2", None)),
    ],
)
def test_parse_image_ref(ref, expected):
    assert parse_image_ref(ref) == expected


# ------------------------------------------------------------------ helm


def test_helm_self_and_dependencies(components):
    own = find(components, HELM, "my-platform")
    assert own is not None and own.meta["self"] is True
    redis = [c for c in of(components, HELM) if c.name == "redis"]
    assert redis and redis[0].current == "16.8.0"
    assert redis[0].meta["dependency"] is True


# --------------------------------------------------------------- lang deps


def test_requirements_parsed(components):
    assert find(components, PYPI, "requests").current == "2.20.0"
    assert find(components, PYPI, "Django").current == "2.2.28"
    az = find(components, PYPI, "azure-identity")
    assert az is not None and az.current == "1.5.0" and az.meta["pinned"] is False


def test_requirements_skips_non_registry_lines(components):
    names = {c.name.lower() for c in of(components, PYPI)}
    assert "thing" not in names
    assert not any(n.startswith(("-", ".")) for n in names)


def test_pyproject_parsed(components):
    assert find(components, PYPI, "pydantic").current == "1.10.2"
    assert find(components, PYPI, "urllib3") is not None
    pytest_dep = find(components, PYPI, "pytest")
    assert pytest_dep is not None and pytest_dep.meta["group"] == "test"


def test_package_json_parsed(components):
    assert find(components, NPM, "express").current == "4.16.0"
    assert find(components, NPM, "react").current == "16.8.6"
    jest = find(components, NPM, "jest")
    assert jest is not None and jest.meta["group"] == "devDependencies"


def test_node_engine_captured(components):
    c = find(components, NPM, "node")
    assert c is not None and c.meta["runtime"] is True and c.current == "16"


def test_gomod_direct_and_indirect(components):
    cobra = find(components, GO, "github.com/spf13/cobra")
    assert cobra is not None and cobra.current == "v1.4.0"
    assert cobra.meta["indirect"] is False
    mt = find(components, GO, "github.com/inconshreveable/mousetrap")
    assert mt is not None and mt.meta["indirect"] is True
    assert find(components, GO, "golang.org/toolchain").current == "1.19"


# ------------------------------------------------------------------- ci


def test_actions_and_runner(components):
    co = find(components, GH_ACTION, "actions/checkout")
    assert co is not None
    # The leading "v" is stripped so the major tag compares numerically rather
    # than being mistaken for a Kubernetes-style apiVersion.
    assert co.current == "2"
    assert co.raw == "actions/checkout@v2"
    runner = find(components, GH_ACTION, "runner/ubuntu-20.04")
    assert runner is not None and runner.meta["successor"] == "ubuntu-24.04"


def test_sha_pinned_action_has_no_version(components):
    shas = [c for c in of(components, GH_ACTION) if c.meta.get("sha_pinned")]
    assert shas, "sha-pinned action should be recorded"
    assert shas[0].current is None


def test_latest_runner_label_marked(components):
    r = find(components, GH_ACTION, "runner/ubuntu-latest")
    assert r is not None and r.meta["latest_label"] is True


# ------------------------------------------------------------- hygiene


def test_venv_directories_are_skipped(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\n")
    venv = tmp_path / "tutorials"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    (venv / "bin" / "activate").write_text("")
    (venv / "requirements.txt").write_text("django==1.0\n")
    comps, _ = scan_tree(tmp_path)
    names = {c.name.lower() for c in comps}
    assert "requests" in names
    assert "django" not in names, "virtualenv contents must not be scanned"


def test_ecosystem_filter(components):
    only, _ = scan_tree(FIXTURE, {PYPI})
    assert only and {c.ecosystem for c in only} == {PYPI}


# ------------------------------------------------- encoding (real-world bug)


@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-16-le", True),
        ("utf-16-be", True),
        ("utf-8-sig", True),
        ("utf-8", False),
    ],
)
def test_requirements_in_any_encoding(tmp_path, encoding, bom):
    """Windows `pip freeze > requirements.txt` writes UTF-16; it must still parse.

    Regression: forcing UTF-8 turned these into NUL-interleaved mojibake that
    matched nothing, so the file silently contributed zero components.
    """
    body = "requests==2.20.0\nDjango==2.2.28\n"
    target = tmp_path / "requirements.txt"
    target.write_bytes(body.encode(encoding))
    comps, _ = scan_tree(tmp_path)
    found = {c.name: c.current for c in comps if c.ecosystem == PYPI}
    assert found == {"requests": "2.20.0", "Django": "2.2.28"}, f"failed for {encoding}"


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_without_bom_is_detected(tmp_path, encoding):
    # "utf-16-le"/"utf-16-be" emit no BOM (unlike plain "utf-16"), so this
    # exercises the NUL-interleaving heuristic rather than the BOM table.
    payload = ("flask==1.0.2\n" * 4).encode(encoding)
    assert not payload.startswith((b"\xff\xfe", b"\xfe\xff"))
    (tmp_path / "requirements.txt").write_bytes(payload)
    comps, _ = scan_tree(tmp_path)
    assert any(c.name == "flask" for c in comps if c.ecosystem == PYPI)


# ------------------------------------------- multi-document YAML line numbers


def test_multidoc_yaml_line_numbers_are_file_absolute(tmp_path):
    """Regression: offsets were measured inside each split document and then
    applied to the whole file, so every document reported the first one's line."""
    (tmp_path / "multi.yaml").write_text(
        "# first\n"
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: a\n"
        "---\n"
        "# second\n"
        "apiVersion: policy/v1beta1\n"
        "kind: PodDisruptionBudget\n"
        "metadata:\n"
        "  name: b\n"
        "---\n"
        "apiVersion: batch/v1beta1\n"
        "kind: CronJob\n"
        "metadata:\n"
        "  name: c\n"
    )
    comps, _ = scan_tree(tmp_path)
    got = {c.name: c.line for c in comps if c.ecosystem == K8S_API}
    assert got["apps/Deployment"] == 2
    assert got["policy/PodDisruptionBudget"] == 8
    assert got["batch/CronJob"] == 13


def test_images_inside_later_documents_get_correct_lines(tmp_path):
    (tmp_path / "m.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  x: 1\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
        "  template:\n    spec:\n      containers:\n"
        "        - name: c\n          image: redis:5\n"
    )
    comps, _ = scan_tree(tmp_path)
    redis = next(c for c in comps if c.ecosystem == DOCKER and c.name == "redis")
    assert redis.line == 13
