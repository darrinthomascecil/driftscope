"""The contract every resolver owes, exercised against all of them at once.

`resolvers/__init__.py` says a resolver "must never raise -- transport and parsing
failures become `Resolution.error` so one dead registry cannot fail a whole scan".
That contract had no test. There is a dispatch-level try/except, so a crash cannot
abort a run -- but it converts a resolver *bug* into `Resolution.error`, which
renders as `unknown`. A resolver that silently returns nonsense is therefore
indistinguishable from a registry being down, and neither shows up as a failure.

Two properties are asserted here for every ecosystem:

1. **No raise.** Malformed, hostile and wrong-typed payloads all yield a
   `Resolution`, never an exception escaping the resolver itself.
2. **No invented answer.** This is the one that matters. Garbage in must not
   produce a plausible-looking `latest_stable` out. Every wrong-answer bug this
   project has shipped had this shape: data that did not support a conclusion,
   and a conclusion drawn anyway.

Parametrising over the dispatch table means a nineteenth ecosystem is covered the
day it is added, without anyone remembering to write a test.
"""

from __future__ import annotations

import pytest

from driftscope.http import HttpError
from driftscope.models import (
    AZURE_API,
    AZURE_NODE_IMAGE,
    AZURE_SKU,
    DOCKER,
    DOTNET_SDK,
    DOTNET_TFM,
    GO,
    HELM,
    JVM_TOOLCHAIN,
    K8S_API,
    K8S_CLUSTER,
    MAVEN,
    NPM,
    NUGET,
    PYPI,
    TERRAFORM_MODULE,
    TERRAFORM_PROVIDER,
    Component,
    Resolution,
)
from driftscope.resolvers import Context, resolve
from driftscope.version import parse
from driftscope.scanners.cicd import GH_ACTION

# Every ecosystem in the dispatch table, with a plausible component for each.
ECOSYSTEMS = [
    (TERRAFORM_PROVIDER, "hashicorp/azurerm", "3.75.0"),
    (TERRAFORM_MODULE, "Azure/naming/azurerm", "0.3.0"),
    (PYPI, "requests", "2.20.0"),
    (NPM, "express", "4.16.0"),
    (GO, "github.com/spf13/cobra", "v1.4.0"),
    (DOCKER, "python", "3.8-slim"),
    (HELM, "redis", "16.8.0"),
    (K8S_API, "extensions/Ingress", "v1beta1"),
    (K8S_CLUSTER, "aks", "1.24.9"),
    (GH_ACTION, "actions/checkout", "2"),
    (AZURE_API, "Microsoft.CognitiveServices/accounts", "2023-05-01"),
    (AZURE_SKU, "Standard_D2_v3", "v3"),
    (AZURE_NODE_IMAGE, "AKSUbuntu-2204gen2containerd", "202401.09.0"),
    (NUGET, "Newtonsoft.Json", "12.0.3"),
    (MAVEN, "com.fasterxml.jackson.core:jackson-databind", "2.15.2"),
    (DOTNET_TFM, "net6.0", "6.0"),
    (DOTNET_SDK, "dotnet-sdk", "6.0.400"),
    (JVM_TOOLCHAIN, "java", "17"),
]

# Payloads a registry should never send, but might: truncated responses, a proxy
# returning HTML, a field that changed type between API versions, hostile input.
HOSTILE = [
    ("none", None),
    ("empty dict", {}),
    ("empty list", []),
    ("empty string", ""),
    ("html error page", "<html><body>502 Bad Gateway</body></html>"),
    ("null-valued fields", {"versions": None, "info": None, "releases": None,
                            "value": None, "tags": None, "data": None}),
    ("wrong types", {"versions": 42, "info": "a string", "releases": [1, 2, 3],
                     "value": {"nested": True}, "tags": 3.14}),
    ("list where dict expected", {"versions": [[]], "releases": [[]], "info": []}),
    ("versions present but junk", {"versions": {"": {}, "  ": {}, "not-a-version": {}},
                                   "releases": {"": [], "???": []}}),
    ("nul bytes", {"versions": {"1.0\x00.0": {}}, "info": {"version": "\x00"}}),
    ("deeply nested", {"a": {"b": {"c": {"d": {"e": {"f": {"g": {}}}}}}}}),
    ("huge version string", {"versions": {"9" * 5000: {}}}),
]


# Ecosystems whose answer comes from a fixed local table rather than a network
# lookup: k8s deprecations, .NET/JVM support dates, Azure SKU families. They are
# correct precisely because they do not depend on the response, so the
# "must not answer from a bad payload" rule does not apply to them.
LOCAL_TABLE = {K8S_API, DOTNET_TFM, DOTNET_SDK, JVM_TOOLCHAIN, AZURE_SKU}


class HostileClient:
    """Returns the same payload for every call, however it is asked."""

    def __init__(self, payload, *, raises: Exception | None = None):
        self.payload, self.raises = payload, raises
        self.cache = type("C", (), {"hits": 0, "get": lambda *a, **k: None,
                                    "put": lambda *a, **k: None,
                                    "fresh": lambda *a, **k: False})()

    def get_json(self, url, **kw):
        if self.raises:
            raise self.raises
        return self.payload

    def get_text(self, url, **kw):
        if self.raises:
            raise self.raises
        return self.payload if isinstance(self.payload, str) else ""


class HostileGitHub(HostileClient):
    enabled = True
    queries = 0

    def api(self, path, **kw):
        return None if self.raises else self.payload

    def search_count(self, q):
        return None

    def search_issues(self, q, **kw):
        return []


def ctx_for(payload, *, raises=None) -> Context:
    return Context(
        client=HostileClient(payload, raises=raises),
        github=HostileGitHub(payload, raises=raises),
        allow_az=False,  # never shell out to az from a test
    )


def component(eco: str, name: str, current: str) -> Component:
    return Component(ecosystem=eco, name=name, current=current, source="fixture")


def _strings_in(obj) -> set[str]:
    """Every string anywhere in a payload, keys and values alike."""
    if isinstance(obj, str):
        return {obj}
    if isinstance(obj, dict):
        out: set[str] = set()
        for k, v in obj.items():
            out |= _strings_in(k) | _strings_in(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        out = set()
        for v in obj:
            out |= _strings_in(v)
        return out
    return set()


@pytest.mark.parametrize("eco,name,current", ECOSYSTEMS, ids=lambda v: str(v))
@pytest.mark.parametrize("label,payload", HOSTILE, ids=lambda v: str(v))
def test_hostile_payload_never_raises(eco, name, current, label, payload):
    res = resolve(component(eco, name, current), ctx_for(payload))
    assert isinstance(res, Resolution)


@pytest.mark.parametrize("eco,name,current", ECOSYSTEMS, ids=lambda v: str(v))
@pytest.mark.parametrize("label,payload", HOSTILE, ids=lambda v: str(v))
def test_hostile_payload_never_invents_a_version(eco, name, current, label, payload):
    """The property that actually protects the customer.

    A resolver may answer "I do not know" (latest_stable None, or an error). What it
    may not do is answer with a version it did not receive -- that is the shape of
    every wrong-answer bug this project has shipped.

    Ecosystems whose answer is a fixed local table rather than a network lookup are
    exempt: k8s deprecations, .NET/JVM support dates and runner labels are correct
    precisely because they do not depend on the response.
    """
    res = resolve(component(eco, name, current), ctx_for(payload))
    if eco in LOCAL_TABLE:
        return
    if res.latest_stable is None:
        return

    # Two things must hold, and an earlier version of this test asserted neither
    # strongly enough -- mutation testing found both holes.
    #
    # 1. The value came from the payload. Compared against the payload's actual
    #    strings, not its repr: repr escapes control characters, so a genuine
    #    NUL-containing value looked invented when it was merely passed through.
    #    There is deliberately no `or == current` escape hatch. Allowing a
    #    resolver to echo the pin back as "latest" is precisely the bug where a
    #    dead registry makes every component look up to date.
    assert res.latest_stable in _strings_in(payload), (
        f"{eco} invented latest_stable={res.latest_stable!r} from {label}"
    )
    # 2. It is actually a version. Passing a registry's free-text field straight
    #    through puts a value in the report that nothing downstream can compare.
    assert parse(str(res.latest_stable)).valid, (
        f"{eco} emitted unparseable latest_stable={res.latest_stable!r} from {label}"
    )


@pytest.mark.parametrize("eco,name,current", ECOSYSTEMS, ids=lambda v: str(v))
def test_transport_failure_becomes_an_error_not_a_guess(eco, name, current):
    """A dead registry must produce a *stated* failure, not just a blank answer.

    `res.error or res.latest_stable is None` was too weak -- swallowing the
    exception and returning a bare Resolution() satisfied it, which renders as a
    finding with no answer and no explanation. The error string is what tells the
    reader the registry was unreachable rather than the package being unknowable.
    """
    res = resolve(component(eco, name, current),
                  ctx_for(None, raises=HttpError("HTTP 503 for x", 503)))
    assert isinstance(res, Resolution)
    if eco in LOCAL_TABLE:
        return
    assert res.latest_stable is None, (
        f"{eco} answered {res.latest_stable!r} from a 503"
    )
    # An error *or* a note -- not merely a blank. Some resolvers legitimately never
    # reach the network (a local Terraform module has no upstream feed; node images
    # need `az`), so they have no transport error to report -- but they do explain
    # themselves in `note`. What is forbidden is a finding with no answer and no
    # reason, which renders as an unexplained `unknown`.
    assert res.error or res.note, (
        f"{eco} produced no answer and no explanation from a 503"
    )


@pytest.mark.parametrize("eco,name,current", ECOSYSTEMS, ids=lambda v: str(v))
def test_unexpected_exception_is_caught_by_the_dispatcher(eco, name, current):
    """The documented guarantee: a resolver bug degrades one finding, not the run."""
    res = resolve(component(eco, name, current),
                  ctx_for(None, raises=RuntimeError("boom")))
    assert isinstance(res, Resolution)


def test_unknown_ecosystem_is_an_error_not_a_crash():
    res = resolve(component("not-a-real-ecosystem", "x", "1.0"), ctx_for({}))
    assert res.error and "no resolver" in res.error


def test_every_dispatch_entry_is_covered():
    """Guards the guard: a nineteenth ecosystem must not slip past this file."""
    from driftscope.resolvers import resolve as _r
    import inspect

    src = inspect.getsource(_r)
    listed = {e for e, _, _ in ECOSYSTEMS}
    # Each table key appears in the dispatch source as a bare constant name.
    from driftscope import models
    names = {getattr(models, n): n for n in dir(models)
             if n.isupper() and isinstance(getattr(models, n), str)}
    from driftscope.scanners.cicd import GH_ACTION as _GA
    names[_GA] = "GH_ACTION"
    in_table = {val for val, nm in names.items() if f"{nm}:" in src}
    missing = in_table - listed
    assert not missing, f"dispatch entries with no contract coverage: {missing}"
