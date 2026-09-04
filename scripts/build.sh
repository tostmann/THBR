#!/usr/bin/env bash
# THBR build wrapper for the native idf.py workflow.
#
# Replicates the old PlatformIO pre-build hook ordering: regenerate version.h
# + bump the build counter + git-snapshot the working tree, THEN build — so
# version.h is fresh when the compiler reads it.
#
#   scripts/build.sh              # build
#   scripts/build.sh flash monitor   # forwards extra args to idf.py
#   THBR_VARIANT=ble scripts/build.sh  # the build that ships (C6); c5 too
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
stale_check "$ROOT/sdkconfig.c5"  "$ROOT/sdkconfig.defaults" "$ROOT/sdkconfig.defaults.br" \
                                  "$ROOT/sdkconfig.defaults.ble" "$ROOT/sdkconfig.defaults.c5"
stale_check "$ROOT/sdkconfig.heap" "$ROOT/sdkconfig.defaults" "$ROOT/sdkconfig.defaults.br" \
                                  "$ROOT/sdkconfig.defaults.ble" "$ROOT/sdkconfig.defaults.heap"

# 1. Pre-build versioning (snapshot + bump + regenerate main/version.h).
# Second and further chips of the SAME release: THBR_NO_BUMP=1 holds the build
# number, so the bundles can carry one firmware version instead of one each.
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

# 1c. Variant selection.  THBR_VARIANT names WHICH PRODUCT BUILD this is, and
#     a product build is more than a chip: the images that ship carry the BLE
#     proxy, so the released C6 is `defaults + br + ble` and the released C5 is
#     that plus the c5 overlay, which sets the target and therefore goes last.
#
#     Getting this wrong is quiet and expensive.  The variant build lived
#     outside this script, so on 2026-08-26 a release build made here came out
#     at 1.31 MB against the 1.74 MB that was shipping -- the whole Bluetooth
#     stack missing, no error, nothing in the log.  It is in the script now so
#     that the bundle a release publishes is the bundle this file describes.
#
#       THBR_STAGE=2 THBR_VARIANT=ble scripts/build.sh                  # C6
#       THBR_STAGE=2 THBR_VARIANT=c5  THBR_NO_BUMP=1 scripts/build.sh   # C5
#
#     THBR_VARIANT=plain (the default) is the development build: no BLE, the
#     generated sdkconfig next to the project, whatever idf_env.sh points at.
VARIANT="${THBR_VARIANT:-plain}"
GENERATED="$ROOT/sdkconfig"
# The base defaults name the target too; start there so no build has to fall
# back on CMake's guess, which reads whichever generated config it finds first.
TARGET="$(sed -n 's/^CONFIG_IDF_TARGET="\(.*\)"/\1/p' "$ROOT/sdkconfig.defaults" | head -1)"
case "$VARIANT" in
    plain) OVERLAYS="" ;;
    ble)   OVERLAYS="ble"     ; GENERATED="$ROOT/sdkconfig.ble"
           export THBR_BUILD_DIR="${THBR_BUILD_DIR:-/root/thbr_idf_build_ble}" ;;
    c5)    OVERLAYS="ble c5"  ; GENERATED="$ROOT/sdkconfig.c5"
           export THBR_BUILD_DIR="${THBR_BUILD_DIR:-/root/thbr_idf_build_c5}" ;;
    # The shipping C6 build plus heap instrumentation, for the drift
    # investigation.  Its own generated config and build directory so it can
    # never be mistaken for, or overwrite, the image that ships.
    heap)  OVERLAYS="ble heap" ; GENERATED="$ROOT/sdkconfig.heap"
           export THBR_BUILD_DIR="${THBR_BUILD_DIR:-/root/thbr_idf_build_heap}" ;;
    *) echo "unknown THBR_VARIANT '$VARIANT' (plain, ble, c5, heap)" >&2; exit 1 ;;
esac

for o in $OVERLAYS; do
    f="$ROOT/sdkconfig.defaults.$o"
    [ -f "$f" ] || { echo "missing overlay: $f" >&2; exit 1; }
    export SDKCONFIG_DEFAULTS="$SDKCONFIG_DEFAULTS;$f"
    # The overlay naming the target is NOT enough on its own, and the failure
    # is quiet: CMake picks the target before it applies any defaults, so with
    # no IDF_TARGET in the environment it guesses -- from the OTHER build's
    # generated sdkconfig lying next to it -- and then cheerfully builds a C6
    # image into the C5's directory.  Seen on 2026-08-26.  So the target comes
    # out of the overlay and into the environment, and a build directory
    # without a generated config gets an explicit set-target.
    t="$(sed -n 's/^CONFIG_IDF_TARGET="\(.*\)"/\1/p' "$f" | head -1)"
    [ -n "$t" ] && TARGET="$t"
done
[ -n "$TARGET" ] || { echo "no CONFIG_IDF_TARGET anywhere in the defaults chain" >&2; exit 1; }
export IDF_TARGET="$TARGET"
[ "$VARIANT" = "plain" ] || STAMP="$ROOT/.thbr_stage_$VARIANT"

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$STAGE" ]; then
    echo "[build] stage -> $STAGE (regenerating $(basename "$GENERATED"))"
    rm -f "$GENERATED"
    echo "$STAGE" > "$STAMP"
fi

# 2. ESP-IDF 6.0 environment (IDF_PATH, toolchain PATH, `idf` function).
# shellcheck disable=SC1091
source "$HERE/idf_env.sh"

# idf_env.sh's `idf` builds the development config in its own directory; a
# product variant needs both its own directory and its own generated config.
if [ "$VARIANT" != "plain" ]; then
    idf() { "$IDF_PYTHON_ENV_PATH/bin/python" "$IDF_PATH/tools/idf.py" \
                -B "$THBR_BUILD_DIR" -D SDKCONFIG="$GENERATED" "$@"; }
fi

# 2b. A fresh variant build directory needs its target set once; set-target
#     regenerates the config from the defaults chain above.
if [ -n "$TARGET" ] && [ ! -f "$GENERATED" ]; then
    echo "[build] set-target $TARGET (fresh $(basename "$GENERATED"))"
    idf set-target "$TARGET"
fi

# 3. Build (or whatever idf.py action was passed).
if [ "$#" -eq 0 ]; then
    idf build
else
    idf "$@"
fi
