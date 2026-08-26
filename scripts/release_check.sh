#!/usr/bin/env bash
# Is anything that ships sitting here unreleased?
#
# The mistake this exists for: a fix was committed AFTER the release commit,
# and no release followed.  The tag and the images went on pointing at a tree
# the fix was not in, the field ran without it, and nothing anywhere said so --
# the release itself had been perfectly self-consistent.  Nothing in the
# publish path can notice this, because at publish time there is nothing wrong
# yet; the damage is done by the commit that comes afterwards and stays.
#
# So this is a question to ask, not a gate to pass: run it after fixing
# something, and before starting a release.
#
#   scripts/release_check.sh          # exit 1 while shipped files are unreleased
#
# It compares against the last commit whose subject starts with "release:",
# which is what the release procedure writes.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# What reaches a user: the add-on image, the firmware and its build inputs,
# the documentation and the tools.  Not version.h and not the build counter --
# those move on every build and would drown the answer.
SHIPPED=(addon main fhem tools scripts README.md LICENSE NOTICE repository.yaml
         CMakeLists.txt partitions.csv sdkconfig.defaults sdkconfig.defaults.br
         sdkconfig.defaults.ble sdkconfig.defaults.c5)
NOISE='^(main/version\.h|build_number\.txt)$'

REL="$(git log -1 --format='%H %s' --grep='^release:' 2>/dev/null)"
if [ -z "$REL" ]; then
    echo "no release commit found — nothing to compare against"
    exit 0
fi
REL_SHA="${REL%% *}"
echo "last release commit: ${REL_SHA:0:7} ${REL#* }"

committed="$(git diff --name-only "$REL_SHA..HEAD" -- "${SHIPPED[@]}" 2>/dev/null \
             | grep -Ev "$NOISE" || true)"
dirty="$(git status --porcelain -- "${SHIPPED[@]}" 2>/dev/null | awk '{print $2}' \
         | grep -Ev "$NOISE" || true)"

if [ -z "$committed" ] && [ -z "$dirty" ]; then
    echo "everything that ships is in that release."
    exit 0
fi
if [ -n "$committed" ]; then
    echo
    echo "committed since then, and not released:"
    echo "$committed" | sed 's/^/  /'
fi
if [ -n "$dirty" ]; then
    echo
    echo "not committed at all:"
    echo "$dirty" | sed 's/^/  /'
fi
echo
echo "These reach users only through a release.  Either they belong in the next"
echo "one -- raise the version, write the CHANGELOG entry, publish -- or they are"
echo "work in progress and this is just the reminder that they are."
exit 1
