#!/usr/bin/env bash
# Make a THBR border router adopt an existing Thread network.
#
# This is the production cut-over: the new border router takes over the running
# network's credentials, so every already-commissioned device stays joined.
# Matter fabrics live in the controller, not in the border router, and Thread
# devices only care that network key, PAN ID and channel are unchanged.
#
#   adopt_dataset.sh <br-ip> <active-dataset-tlv-hex>
#   adopt_dataset.sh <br-ip> --from <old-br-ip>
#
# The active dataset can only be written while Thread is stopped (the REST API
# answers 409 / OT_ERROR_INVALID_STATE otherwise), hence stop / set / start.
# Measured downtime on the bench: 6 s to re-attach, mesh and devices intact.
set -euo pipefail

BR="${1:?usage: adopt_dataset.sh <br-ip> <dataset-hex|--from <old-ip>>}"
shift

if [ "${1:-}" = "--from" ]; then
    OLD="${2:?--from needs the old border routers address}"
    DS=$(curl -fsS --max-time 10 -H "Accept: text/plain" "http://$OLD/node/dataset/active")
    printf '[adopt] pulled dataset from %s (%s hex chars)\n' "$OLD" "${#DS}"
else
    DS="${1:?dataset hex missing}"
fi

if [ $(( ${#DS} % 2 )) -ne 0 ]; then
    printf '[adopt] dataset has odd length - truncated?\n' >&2
    exit 1
fi

say()   { printf '[adopt] %s\n' "$1"; }
state() { curl -fsS --max-time 8 "http://$BR/node/state" | tr -d '"'; }

say "current state: $(state)"
say "KEEP THIS - the dataset currently on the router, needed for rollback:"
curl -fsS --max-time 8 -H "Accept: text/plain" "http://$BR/node/dataset/active"
printf '\n'

START=$(date +%s)

# Once Thread is stopped, it MUST be started again on every path out of this
# script.  Twice on 2026-08-27 the dataset write ran past its timeout, set -e
# took the script down before the enable, and the stick sat there with Thread
# disabled -- a border router that is not merely unmigrated but off, which is
# the worst outcome this script has.  A trap is cheap insurance.
started=0
restart_thread() {
    [ "$started" = 1 ] && return
    curl -fsS --max-time 20 -X PUT -H "Content-Type: application/json" \
         --data '"enable"' "http://$BR/node/state" >/dev/null 2>&1 \
        && say "Thread started again" \
        || say "WARNING could not start Thread -- do it by hand: PUT \"enable\" to /node/state"
}
trap restart_thread EXIT

say "stopping Thread"
curl -fsS --max-time 10 -X PUT -H "Content-Type: application/json" \
     --data '"disable"' "http://$BR/node/state" >/dev/null
sleep 2

# 15 s was not enough: the write goes over a serial backbone and was measured
# taking longer, which is how the two incidents above started.
say "writing the dataset"
curl -fsS --max-time 90 -X PUT -H "Content-Type: text/plain" \
     --data "$DS" "http://$BR/node/dataset/active" >/dev/null

say "starting Thread"
curl -fsS --max-time 20 -X PUT -H "Content-Type: application/json" \
     --data '"enable"' "http://$BR/node/state" >/dev/null
started=1

for _ in $(seq 1 60); do
    s=$(state || true)
    if [ "$s" = "router" ] || [ "$s" = "leader" ] || [ "$s" = "child" ]; then
        say "attached as $s after $(( $(date +%s) - START ))s"
        exit 0
    fi
    sleep 2
done

say "did not attach within 120s - check /node/state"
exit 1
