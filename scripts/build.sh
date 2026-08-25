#!/usr/bin/env bash
# THBR build wrapper for the native idf.py workflow.
#
# Replicates the old PlatformIO pre-build hook ordering: regenerate version.h
# + bump the build counter + git-snapshot the working tree, THEN build — so
# version.h is fresh when the compiler reads it.
#
#   scripts/build.sh              # build
#   scripts/build.sh flash monitor   # forwards extra args to idf.py
#
# Requires scripts/idf_env.sh (host-specific, gitignored) to set up the
# ESP-IDF 6.0 environment and define the `idf` shell function.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# 0. idf.py does NOT re-read sdkconfig.defaults once sdkconfig exists, and a
#    bare edit to sdkconfig.defaults does not trigger a CMake reconfigure.  So
#    if sdkconfig.defaults (or partitions.csv) is newer than the generated
#    sdkconfig, drop sdkconfig to force a fresh regeneration from defaults.
#    (sdkconfig lives in the PROJECT dir, not the build dir, under idf.py.)
#    This applies to EVERY generated config, not just the plain one: the
#    variants (sdkconfig.ble, sdkconfig.c5) are generated the same way and go
#    stale the same way.  On 2026-08-25 that cost a release -- a fix committed
#    two days earlier lived in sdkconfig.defaults.ble, while the build reused a
#    stale sdkconfig.ble and shipped the old value.  Only the plain config was
#    guarded, so nothing said a word.
stale_check() {                 # <generated> <defaults...>
    local gen="$1"; shift
    [ -f "$gen" ] || return 0
    local d
    for d in "$@" "$ROOT/partitions.csv"; do
        if [ -f "$d" ] && [ "$d" -nt "$gen" ]; then
            echo "[build] $(basename "$d") is newer than $(basename "$gen") → regenerating"
            rm -f "$gen"
            return 0
        fi
    done
}
stale_check "$ROOT/sdkconfig"     "$ROOT/sdkconfig.defaults"
stale_check "$ROOT/sdkconfig.ble" "$ROOT/sdkconfig.defaults" "$ROOT/sdkconfig.defaults.br" "$ROOT/sdkconfig.defaults.ble"
stale_check "$ROOT/sdkconfig.c5"  "$ROOT/sdkconfig.defaults" "$ROOT/sdkconfig.defaults.c5"

# 1. Pre-build versioning (snapshot + bump + regenerate main/version.h).
python3 "$HERE/version_bump.py"

# 1b. Stage selection.  THBR_STAGE=2 layers sdkconfig.defaults.br on top of
#     sdkconfig.defaults (border router on).  Changing stage invalidates the
#     generated sdkconfig, so drop it.
STAGE="${THBR_STAGE:-1}"
STAMP="$ROOT/.thbr_stage"
if [ "$STAGE" = "2" ]; then
    export SDKCONFIG_DEFAULTS="$ROOT/sdkconfig.defaults;$ROOT/sdkconfig.defaults.br"
else
    export SDKCONFIG_DEFAULTS="$ROOT/sdkconfig.defaults"
fi
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$STAGE" ]; then
    echo "[build] stage -> $STAGE (regenerating sdkconfig)"
    rm -f "$ROOT/sdkconfig"
    echo "$STAGE" > "$STAMP"
fi

# 2. ESP-IDF 6.0 environment (IDF_PATH, toolchain PATH, `idf` function).
# shellcheck disable=SC1091
source "$HERE/idf_env.sh"

# 3. Build (or whatever idf.py action was passed).
if [ "$#" -eq 0 ]; then
    idf build
else
    idf "$@"
fi
