#!/usr/bin/env bash
#
# network-stress-test.sh -- chaos/stress test for the AKS + Istio + Cloudflare network path.
#
# Actively triggers 7 networking failure modes N times and logs timings/codes so you can
# spot anomalies (variance, fail-open races, slow recovery, stuck pods). Companion to
# docs/network-stress-test.md, which records a run's findings + the prevention plan.
#
# WARNING: this is DESTRUCTIVE by design -- it force-deletes cloudflared / CoreDNS pods and
# scales oauth2-proxy to 0 (brief app/SSO/DNS outages, each self-healing in seconds). Run it
# against dev/staging, never blindly against prod.
#
# Usage:  ./network-stress-test.sh [iterations]      # default 5
# Env overrides: HOST, KUBE_CONTEXT, RG, AKS, APP_NS, GW_NS
#
set +e

ITERS="${1:-5}"
HOST="${HOST:-driftscope.justmakeitgo.net}"
KUBE_CONTEXT="${KUBE_CONTEXT:-driftscope-aks}"
RG="${RG:-driftscope-cloud}"
AKS="${AKS:-driftscope-aks}"
APP_NS="${APP_NS:-driftscope}"
GW="aks-istio-ingressgateway-internal.aks-istio-ingress.svc.cluster.local:80"

kubectl config use-context "$KUBE_CONTEXT" >/dev/null 2>&1

# out-of-mesh tester pod (netshoot: curl/dig/ss/nc)
kubectl -n default get pod nettest >/dev/null 2>&1 || \
  kubectl -n default run nettest --image=nicolaka/netshoot --restart=Never --command -- sleep 10800 >/dev/null 2>&1
kubectl -n default wait --for=condition=Ready pod/nettest --timeout=120s >/dev/null 2>&1

# poll the external site from inside the pod (hairpin through Cloudflare); prints "secs firstbadcode"
poll_ext(){
  kubectl -n default exec nettest -- sh -c "
    s=\$(date +%s); bad=''
    for i in \$(seq 1 60); do
      c=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://$HOST/ 2>/dev/null)
      if [ \"\$c\" = 302 ] || [ \"\$c\" = 200 ]; then echo \"\$(( \$(date +%s)-s )) \${bad:-none}\"; exit; fi
      bad=\$c; sleep 1
    done; echo 'TIMEOUT '\$bad"
}
# poll DNS from the pod; validates a real IP is returned; prints "secs firstbad"
poll_dns(){
  kubectl -n default exec nettest -- sh -c "
    s=\$(date +%s); bad=''
    for i in \$(seq 1 60); do
      r=\$(dig +short +time=2 +tries=1 github.com 2>/dev/null | grep -E '^[0-9]+\\.[0-9]' | head -1)
      if [ -n \"\$r\" ]; then echo \"\$(( \$(date +%s)-s )) \${bad:-none}\"; exit; fi
      bad=refused; sleep 1
    done; echo 'TIMEOUT '\$bad"
}

echo "network-stress-test: $ITERS iterations against $HOST (context $KUBE_CONTEXT)"
for N in $(seq 1 "$ITERS"); do
  echo "===== ITERATION $N ($(TZ=UTC date +%H:%M:%S)Z) ====="

  # T4 -- tunnel outage: kill both cloudflared, measure external recovery
  kubectl -n "$APP_NS" delete pod -l app.kubernetes.io/name=cloudflared --grace-period=0 --force >/dev/null 2>&1
  R=$(poll_ext); echo "T4 tunnel: outage=${R%% *}s firstbad=${R##* }"
  kubectl -n "$APP_NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=cloudflared --timeout=90s >/dev/null 2>&1

  # T5 -- DNS blast radius: kill both CoreDNS, measure resolution recovery
  kubectl -n kube-system delete pod -l k8s-app=kube-dns --grace-period=0 --force >/dev/null 2>&1
  R=$(poll_dns); echo "T5 dns:    outage=${R%% *}s firstbad=${R##* }"
  kubectl -n kube-system wait --for=condition=Ready pod -l k8s-app=kube-dns --timeout=90s >/dev/null 2>&1

  # T3 -- ext_authz fail mode: oauth2-proxy down must be 403 (200 = fail-open bypass); then recovery
  kubectl -n "$APP_NS" scale deployment oauth2-proxy --replicas=0 >/dev/null 2>&1
  kubectl -n "$APP_NS" wait --for=delete pod -l app=oauth2-proxy --timeout=40s >/dev/null 2>&1
  DOWN=$(kubectl -n default exec nettest -- curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Host: $HOST" "http://$GW/" 2>/dev/null)
  kubectl -n "$APP_NS" scale deployment oauth2-proxy --replicas=2 >/dev/null 2>&1
  REC=$(kubectl -n default exec nettest -- sh -c "
    s=\$(date +%s)
    for i in \$(seq 1 60); do
      c=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: $HOST' http://$GW/ 2>/dev/null)
      if [ \"\$c\" = 302 ]; then echo \$(( \$(date +%s)-s )); exit; fi; sleep 1
    done; echo TIMEOUT")
  ANOM=""; [ "$DOWN" = 200 ] && ANOM="  <<< ANOMALY: FAIL-OPEN (200 with oauth2-proxy down)"
  echo "T3 sso:    down_code=$DOWN (want 403) recovery=${REC}s${ANOM}"

  # T2 -- egress control: IMDS should stay reachable while networkPolicy=none (the finding)
  IMDS=$(kubectl -n default exec nettest -- curl -s --max-time 5 -H "Metadata:true" \
    "http://169.254.169.254/metadata/instance/compute/name?api-version=2021-02-01&format=text" 2>/dev/null)
  echo "T2 imds:   reachable=$([ -n "$IMDS" ] && echo YES || echo no)"

  # T1 -- outbound connection ceiling (fd-limited proxy for SNAT); expect ~1024 from one pod
  EST=$(kubectl -n default exec nettest -- sh -c '
    for i in $(seq 1 1600); do (timeout 8 nc -w 8 1.1.1.1 443 >/dev/null 2>&1 &) done 2>/dev/null
    sleep 4; ss -tan state established 2>/dev/null | grep ":443" | grep -c "1.1.1.1"')
  echo "T1 snat:   max_concurrent_conns=$EST (2048=SNAT ceiling, ~1024=pod fd-limit)"
  sleep 6

  # T6 -- false HA: how many distinct nodes host the 2-replica components (1 = co-located)
  NODES=$(kubectl get pods -A -o wide --no-headers 2>/dev/null \
    | grep -E 'cloudflared|oauth2-proxy|istiod|ingressgateway|coredns-c' | awk '{print $8}' | sort -u | wc -l | tr -d ' ')
  echo "T6 nodes:  distinct_nodes_hosting_HA_components=$NODES (1 = false HA)"

  # health after the iteration -- anything stuck?
  STUCK=$(kubectl get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed"{print $2":"$4}' | tr '\n' ' ')
  echo "health:    not_running=[$STUCK]"
  echo ""
  sleep 5
done

kubectl -n "$APP_NS" scale deployment oauth2-proxy --replicas=2 >/dev/null 2>&1   # ensure restored
echo "===== DONE ($ITERS iterations) ====="
echo "cleanup: kubectl -n default delete pod nettest"
