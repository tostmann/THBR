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
