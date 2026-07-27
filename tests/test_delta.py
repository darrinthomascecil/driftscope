"""GitHub delta note mining. No network."""

from __future__ import annotations

from driftscope.ghub.delta import (
    _breaking_from_releases,
    _is_section_heading,
)

# Trimmed from the real hashicorp/terraform-provider-azurerm release bodies that
# surfaced this bug during a live scan.
AZURERM_BODY = """DEPRECATIONS:

* `azurerm_kubernetes_cluster` - the `ebpf_data_plane` property has been deprecated and superseded by the `network_data_plane` property
* `azurerm_api_management` - the `policy` block has been deprecated is superseded by the `azurerm_api_management_policy` resource

FEATURES:

* New resource: `azurerm_nginx_configuration`

ENHANCEMENTS:

* dependencies: updating to `v0.20240610.1112550` of `github.com/hashicorp/go-azure-sdk`
"""


def test_section_headings_are_not_breaking_notes():
    """Regression: `DEPRECATIONS:` matched the breaking patterns and was quoted as
    a finding, inflating the breaking-change count with content-free entries."""
    notes = _breaking_from_releases([{"tag_name": "v3.108.0", "body": AZURERM_BODY}])
    assert notes, "the two real deprecation lines should still be captured"
    assert not any(n.endswith("DEPRECATIONS:") for n in notes)
    assert all("ebpf_data_plane" in n or "api_management_policy" in n for n in notes), notes
    assert len(notes) == 2


def test_real_prose_is_still_captured():
    body = "* `azurerm_thing` - the `old` property has been removed in favour of `new`\n"
    notes = _breaking_from_releases([{"tag_name": "v4.0.0", "body": body}])
    assert len(notes) == 1
    assert notes[0].startswith("v4.0.0: ")


def test_heading_detection():
    for heading in (
        "DEPRECATIONS:",
        "BREAKING CHANGES:",
        "BUG FIXES:",
        "Notes for upgraders:",
    ):
        assert _is_section_heading(heading), heading
    for prose in (
        "`azurerm_x` - the `y` property has been deprecated and superseded by `z`",
        "Minimum required version of Terraform is now 1.3:",
        "removed support for the legacy authentication flow",
    ):
        assert not _is_section_heading(prose), prose


def test_notes_are_capped_and_deduplicated():
    line = "* `azurerm_x` - the `y` property has been removed\n"
    releases = [{"tag_name": f"v3.{i}.0", "body": line * 5} for i in range(20)]
    notes = _breaking_from_releases(releases)
    assert len(notes) <= 25
    assert len(notes) == len(set(notes)), "duplicates within a release must collapse"
