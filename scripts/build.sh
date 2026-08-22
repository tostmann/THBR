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
if [ -f "$ROOT/sdkconfig" ]; then
    if [ "$ROOT/sdkconfig.defaults" -nt "$ROOT/sdkconfig" ] || \
       [ "$ROOT/partitions.csv" -nt "$ROOT/sdkconfig" ]; then
        echo "[build] sdkconfig.defaults/partitions.csv changed → regenerating sdkconfig"
        rm -f "$ROOT/sdkconfig"
    fi
fi

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
