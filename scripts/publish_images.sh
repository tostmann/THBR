#!/usr/bin/env bash
# Build the add-on images and publish them on Docker Hub.
#
# Two architectures, each built natively.  Emulating one of them is slow enough
# to be worth a second machine: point THBR_REMOTE_HOST at an ssh host of the
# other architecture that can reach this same directory (a shared mount, or set
# THBR_REMOTE_PATH), and the architecture this machine is not gets built there.
# Without it, only the local architecture is published and the multi-arch tag
# is left alone.
#
# The tag is the add-on version from addon/config.yaml — the Supervisor pulls
# exactly that tag, so the two must not drift apart.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

NAMESPACE="${THBR_NAMESPACE:-tostmann}"
VERSION="$(sed -n 's/^version: *"\(.*\)"/\1/p' "$ROOT/addon/config.yaml")"
[ -n "$VERSION" ] || { echo "no version in addon/config.yaml" >&2; exit 1; }

# The bundled firmware must be one version, not one per chip.
#
# dist.sh replaces only the chip it was just run for -- deliberately, because
# it runs on whichever board is attached.  Nothing downstream ever compared the
# results, so 2026.8.34 shipped 0.1.42 for the C6 and 0.1.40 for the C5 while
# the CHANGELOG said "firmware 0.1.42" and the docs promised a fix that half
# the users did not get.  Same shape as the stale sdkconfig this release added
# guards for: the artefact quietly disagreed with what the release claimed.
#
# Drift can be legitimate (a chip whose build is deliberately held back), so
# this is a stop with an override, not a prohibition: set THBR_ALLOW_FW_DRIFT=1
# and say so in the CHANGELOG, where the user who is missing the fix can read
# it.
python3 - "$ROOT" "$VERSION" "${THBR_ALLOW_FW_DRIFT:-}" <<'PYCHECK'
import glob, json, os, re, sys
root, version, allow = sys.argv[1], sys.argv[2], sys.argv[3]

found = {}
for path in sorted(glob.glob(os.path.join(root, "addon/firmware/*/manifest.json"))):
    m = json.load(open(path))
    found[m.get("chip", os.path.basename(os.path.dirname(path)))] = m.get("fw")
if not found:
    sys.exit("no firmware bundles under addon/firmware -- run scripts/dist.sh first")
listing = ", ".join(f"{c} {v}" for c, v in sorted(found.items()))

def stop(msg):
    if allow:
        print(f"WARNING: {msg} — publishing anyway, say so in the CHANGELOG")
    else:
        sys.exit(f"{msg}\n"
                 f"  Build the chip that is behind and re-run scripts/dist.sh for\n"
                 f"  it, or set THBR_ALLOW_FW_DRIFT=1 and name the exception in\n"
                 f"  addon/CHANGELOG.md so the affected users learn of it.")

# One chip built and the other forgotten reads as agreement, because there is
# nothing to disagree with.  Say what is in the box either way.
if len(found) < 2:
    print(f"note: only one firmware bundle present ({listing}) — "
          f"the add-on offers aarch64 and amd64 hosts both chips")

if len(set(found.values())) > 1:
    stop(f"firmware bundles disagree: {listing}")

# And the release must not claim a firmware the box does not contain.  The
# CHANGELOG is the claim a user reads; the bundles are what they get.  If the
# top section names no firmware, this says so and checks nothing -- a missing
# reference is not a reason to block a release.
changelog = os.path.join(root, "addon/CHANGELOG.md")
if os.path.exists(changelog):
    text = open(changelog).read()
    heads = list(re.finditer(r"^## +(\S+)", text, re.M))
    # The section for THIS version, not merely the first one: a tree that keeps
    # an "Unreleased" heading on top would otherwise be read there, find no
    # firmware line, and quietly check nothing at the one moment that matters.
    hit = next((i for i, h in enumerate(heads) if h.group(1) == version), None)
    if hit is None and heads:
        hit = 0
        print(f"note: no CHANGELOG section for {version}; reading the top one "
              f"({heads[0].group(1)}) instead")
    if heads:
        top = heads[hit]
        section = (text[top.end():heads[hit + 1].start()]
                   if hit + 1 < len(heads) else text[top.end():])
        claim = re.search(r"Firmware(?:\s+unchanged\s+at)?\s+(\d+\.\d+\.\d+)", section)
        if not claim:
            print("note: the CHANGELOG's top section names no firmware version, "
                  "so the bundles were not checked against it")
        elif claim.group(1) not in set(found.values()):
            stop(f"the CHANGELOG promises firmware {claim.group(1)}, "
                 f"the bundles carry {listing}")
        elif len(set(found.values())) > 1:
            print(f"the CHANGELOG names {claim.group(1)}, which one bundle "
                  f"carries and another does not: {listing}")
        else:
            print(f"firmware bundles agree with the CHANGELOG: {listing}")
    else:
        print("note: no release sections in addon/CHANGELOG.md")
else:
    print("note: no addon/CHANGELOG.md, nothing to check the bundles against")

# The compose file is a pin AND shipped documentation, and it was left behind
# once already.  A version there that is not the one being published sends
# every plain-Docker user to the previous image.
compose = os.path.join(root, "addon/compose.yaml")
if os.path.exists(compose):
    pin = re.search(r"image:\s*\S*/thbr:(\S+)", open(compose).read())
    if pin and pin.group(1) not in ("latest", version):
        sys.exit(f"addon/compose.yaml still pins {pin.group(1)}, publishing "
                 f"{version}.\n  One line, and it is shipped documentation.")
PYCHECK

base_for() { echo "ghcr.io/home-assistant/$1-base:latest"; }
case "$(uname -m)" in
    aarch64|arm64) LOCAL_ARCH=aarch64; REMOTE_ARCH=amd64 ;;
    x86_64)        LOCAL_ARCH=amd64;   REMOTE_ARCH=aarch64 ;;
    *) echo "unsupported build host: $(uname -m)" >&2; exit 1 ;;
esac

build_and_push() {   # <arch> [ssh-host]
    local arch="$1" host="${2:-}" path="${THBR_REMOTE_PATH:-$ROOT}"
    local img="$NAMESPACE/thbr-$arch"
    local cmd=(docker build --build-arg "BUILD_FROM=$(base_for "$arch")"
               --build-arg "THBR_VERSION=$VERSION"
               -t "$img:$VERSION" -t "$img:latest"
               -f "$path/addon/Dockerfile" "$path/addon")
    echo ">>> $arch${host:+ on $host}"
    if [ -n "$host" ]; then
        ssh "$host" "${cmd[*]} && docker push $img:$VERSION && docker push $img:latest"
    else
        "${cmd[@]}" && docker push "$img:$VERSION" && docker push "$img:latest"
    fi
}

build_and_push "$LOCAL_ARCH"
if [ -n "${THBR_REMOTE_HOST:-}" ]; then
    build_and_push "$REMOTE_ARCH" "$THBR_REMOTE_HOST"
    # One name for both architectures, for everyone not using the add-on.
    for tag in "$VERSION" latest; do
        docker manifest rm "$NAMESPACE/thbr:$tag" >/dev/null 2>&1 || true
        docker manifest create "$NAMESPACE/thbr:$tag" \
            "$NAMESPACE/thbr-aarch64:$tag" "$NAMESPACE/thbr-amd64:$tag" >/dev/null
        docker manifest push "$NAMESPACE/thbr:$tag" >/dev/null
        echo "pushed $NAMESPACE/thbr:$tag"
    done
else
    echo "THBR_REMOTE_HOST unset — only $LOCAL_ARCH published, multi-arch tag untouched" >&2
fi
