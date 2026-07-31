# Audit logging

driftscope clones and reads customers' **private** repositories, so it must be able to answer
*who scanned which repo, when, under which app, and was it allowed* — for incident response and
for compliance (SOC 2). The server emits **one structured audit event per scan request**, for
every outcome, allow **and** deny (an audit trail is mostly about the denials).

The CLI is single-user and local, so there is nothing to audit there — this is a server concern.

## Where it goes

**Stdout, one JSON line per event.** The platform's log pipeline (Azure Monitor / Loki / …)
captures, retains, and makes it immutable. The audited service deliberately does **not** own its
own tamper-proof store: separating *who did the thing* from *who keeps the record* is the whole
point of an audit trail. `driftscope.audit.set_sink` swaps the destination (tests, or a future
forwarder to a dedicated audit store).

## The event

```json
{
  "ts": "2026-07-31T16:00:00.123456+00:00",
  "audit": true,
  "event": "scan",
  "request_id": "b3c1…",           // X-Request-Id / traceparent if supplied, else generated
  "caller": "3f9a…-oid",           // Entra object id (opaque); "anonymous" if unauthenticated;
                                    //   "authz-disabled" when token validation is off
  "authenticated": true,
  "roles": ["tenant.acme"],
  "tenant": "…-tid",
  "repo": "https://github.com/acme-corp/app",   // userinfo-stripped
  "requested_identity": "acme-github",           // the name the caller asked for (if any)
  "identity": "acme-github",                     // the app that actually ran (allowed scans)
  "forge": "github",
  "outcome": "allowed",             // allowed | denied (400/401/403) | error (422/500)
  "status": 200,
  "findings": 72                    // component count on success; "error" holds the reason otherwise
}
```

`None` fields are omitted. A denied request has an `error` and no `identity`/`findings`.

## What it never logs

Bearer **tokens**, **secrets** (Key Vault refs / client secrets / signing keys), or repository
**contents**. The caller is an opaque **subject** (`oid`), not an email; the repo URL is stripped
of any `user@` userinfo. A regression test (`tests/test_audit.py`) asserts none of those strings
appear in the output.

## Shipping & querying

- **AKS + Azure Monitor:** container stdout is already collected into a Log Analytics workspace.
  Query with KQL:
  ```kusto
  ContainerLogV2
  | where LogMessage has '"audit":true'
  | extend e = parse_json(LogMessage)
  | where e.outcome == "denied"
  | project e.ts, e.caller, e.repo, e.identity, e.status, e.error
  ```
- **Alerting:** a burst of `outcome == "denied"` (401/403) is a probe or a misconfigured tenant —
  a good first audit-driven alert (pairs with the parked "denied-login spike" backlog item).
- **Retention:** set by the log workspace, not the app — pick a retention that meets your
  compliance obligation for access records over private code.

## Scope boundary

This records the **access decision and outcome** — the audit half. It is not access *control*
(that's `docs/caller-authz.md`) and not scan-result storage (there is none yet; results are
synchronous). When scan history/DB arrives, its reads should emit their own audit events.
