"""Container entrypoint:  python -m driftscope.fleet

Env-configured so the same image runs the fleet service with no code baked in:
  DRIFTSCOPE_FLEET_STORE   inventory store dir           (default /data/fleet)
  DRIFTSCOPE_FLEET_POLICY  path to policy.json           (default: none -> empty policy)
  DRIFTSCOPE_FLEET_PORT    listen port                   (default 8080)
  DRIFTSCOPE_FLEET_TENANT  default tenant for the UI     (default "default")

See deploy/overlays/cloud/fleet.yaml for how these are wired in-cluster.
"""

from __future__ import annotations

import os
from pathlib import Path

from .api import serve
from .evaluate import FleetService
from .policy import Policy
from .store import Store


def main() -> None:
    store = os.environ.get("DRIFTSCOPE_FLEET_STORE", "/data/fleet")
    policy_path = os.environ.get("DRIFTSCOPE_FLEET_POLICY", "")
    port = int(os.environ.get("DRIFTSCOPE_FLEET_PORT", "8080"))
    tenant = os.environ.get("DRIFTSCOPE_FLEET_TENANT", "default")

    policy = Policy.load(policy_path) if policy_path and Path(policy_path).exists() else Policy()
    service = FleetService(Store(Path(store)), policy)
    print(f"driftscope-fleet: store={store} policy={policy_path or '(empty)'} "
          f"tenant={tenant} listening on :{port}", flush=True)
    serve(service, host="0.0.0.0", port=port, default_tenant=tenant)


if __name__ == "__main__":
    main()
