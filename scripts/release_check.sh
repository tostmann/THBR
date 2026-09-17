#!/usr/bin/env bash
# Is anything that ships sitting here unreleased -- and is the build that would
# ship still reproducible?
#
# The mistake this exists for: a fix was committed AFTER the release commit,
# and no release followed.  The tag and the images went on pointing at a tree
# the fix was not in, the field ran without it, and nothing anywhere said so --
# the release itself had been perfectly self-consistent.  Nothing in the
# publish path can notice this, because at publish time there is nothing wrong
# yet; the damage is done by the commit that comes afterwards and stays.
#
# The second mistake, found 2026-09-08: a fix that never was a commit at all.
# The /diagnostics streaming fix lived as an untracked edit under
# managed_components/, which is gitignored, while the manifest named no version
# and the lock file was ignored too.  The firmware in the field had the fix;
# the repository did not, and neither did the upstream base it applied to.  A
# fresh checkout would have rebuilt the leak in silence.  So this script also
# asks whether the dependency pins still hold and whether anybody has edited a
# resolved component by hand.
#
# So this is a question to ask, not a gate to pass: run it after fixing
# something, and before starting a release.
#
#   scripts/release_check.sh          # exit 1 while something needs answering
#
# It compares against the last commit whose subject starts with "release:",
# which is what the release procedure writes.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# What reaches a user: the add-on image, the firmware and its build inputs,
# the documentation and the tools.  dependencies.lock is a build input like any
# other -- it is the only place the resolved commit of a git dependency is
# written down.  Not version.h and not the build counter -- those move on every
# build and would drown the answer.
SHIPPED=(addon main fhem tools scripts README.md LICENSE NOTICE repository.yaml
         CMakeLists.txt partitions.csv dependencies.lock sdkconfig.defaults
         sdkconfig.defaults.br sdkconfig.defaults.ble sdkconfig.defaults.c5)
NOISE='^(main/version\.h|build_number\.txt)$'

# Markers that must survive in the resolved tree.  Each is a canary for a fix
# that is ours and not upstream's: if the component is ever re-resolved from a
# source that lacks it, the build goes quiet and the bug comes back.  A marker
# is not a proof the fix is complete -- it is a proof it is still there.
MARKERS=("managed_components/espressif__esp_ot_br_server/src/esp_br_web.c:stream_diagnostic_set")

problems=0
note() { problems=$((problems + 1)); echo; echo "$@"; }

## 1. Are the git dependencies pinned?
#     A git dependency without `version:` resolves to whatever the branch tip
#     happens to be that day.  The lock file records what was actually taken --
#     but only if it is committed, which is what SHIPPED above is for.
MANIFEST="$ROOT/main/idf_component.yml"
if [ -f "$MANIFEST" ]; then
    unpinned="$(python3 - "$MANIFEST" <<'PY'
import re, sys
# Deliberately not yaml.safe_load: this must run on a bare checkout with no
# third-party modules installed.
text = open(sys.argv[1]).read().splitlines()
name, has_git, has_version, out = None, False, False, []
def flush():
    if name and has_git and not has_version:
        out.append(name)
for line in text + ["  end:"]:
    m = re.match(r"^  ([A-Za-z0-9_./-]+):\s*$", line)
    if m:
        flush()
        name, has_git, has_version = m.group(1), False, False
        continue
    if re.match(r"^    git:", line):
        has_git = True
    if re.match(r"^    version:", line):
        has_version = True
print("\n".join(out))
PY
)"
    if [ -n "$unpinned" ]; then
        note "git dependencies with no version — these resolve to a moving target:"
        echo "$unpinned" | sed 's/^/  /'
        echo "  Pin each to a commit hash, or a build here and a build elsewhere"
        echo "  are not the same firmware."
    fi

    ## 2. Does the lock agree with the manifest?
    #     They drift apart when somebody edits the manifest and does not build,
    #     which is exactly the state in which a release would ship the old pin.
    if [ -f "$ROOT/dependencies.lock" ]; then
        mismatch="$(python3 - "$MANIFEST" "$ROOT/dependencies.lock" <<'PY'
import re, sys
def pins(path, indent):
    """Component name -> its own version.

    The version must sit exactly one level below the component name.  A looser
    match walks into the nested `dependencies:` list a lock file carries and
    reports a dependency's range as if it were the component's pin.
    """
    cur, out = None, {}
    for line in open(path):
        m = re.match(r"^%s([A-Za-z0-9_./-]+):\s*$" % (" " * indent), line)
        if m:
            cur = m.group(1); continue
        m = re.match(r"^%sversion:\s*(\S+)\s*$" % (" " * (indent + 2)), line)
        if m and cur:
            out.setdefault(cur, m.group(1).strip('"\''))
    return out
man, lock = pins(sys.argv[1], 2), pins(sys.argv[2], 2)
for k, v in man.items():
    # Only compare what looks like a commit hash; version ranges like ^1.5.0
    # are resolved to a release number in the lock and will never match.
    if re.fullmatch(r"[0-9a-f]{40}", v) and lock.get(k) != v:
        print(f"{k}: manifest {v[:12]} vs lock {str(lock.get(k))[:12]}")
PY
)"
        if [ -n "$mismatch" ]; then
            note "manifest and dependencies.lock disagree about a pinned commit:"
            echo "$mismatch" | sed 's/^/  /'
            echo "  Build once so the lock is rewritten, then commit it."
        fi
    else
        note "dependencies.lock is missing — the resolved commits are unrecorded."
        echo "  Build once, then commit the lock."
    fi
fi

## 3. Is our fix still in the resolved tree, and has anybody edited it by hand?
#     managed_components/ is gitignored on purpose: it is generated.  That is
#     precisely why an edit made there is invisible and unrecoverable.
for entry in "${MARKERS[@]}"; do
    f="${entry%%:*}"; marker="${entry##*:}"
    if [ ! -f "$ROOT/$f" ]; then
        echo "note: $f not resolved yet — run a build to check the '$marker' marker"
        continue
    fi
    if ! grep -q "$marker" "$ROOT/$f"; then
        note "the resolved component lost a fix that is ours: '$marker' is gone from"
        echo "  $f"
        echo "  The dependency probably resolved to a source without it. Check the pin"
        echo "  in main/idf_component.yml before building anything that ships."
    fi
done

#     A file newer than the component hash beside it was written after the
#     manager resolved the component — i.e. by hand.  That edit exists nowhere
#     else: not in git, not in the lock, not in the fork.
for hashfile in managed_components/*/.component_hash; do
    [ -e "$hashfile" ] || continue
    comp="$(dirname "$hashfile")"
    edited="$(find "$comp" -type f -newer "$hashfile" -not -name '.component_hash' 2>/dev/null)"
    if [ -n "$edited" ]; then
        note "hand-edited files in a resolved component ($(basename "$comp")):"
        echo "$edited" | sed 's/^/  /'
        echo "  These are gitignored and generated: the next resolve overwrites them"
        echo "  without a word. Push the change to the pinned fork instead."
    fi
done

## 4. The original question: is anything that ships unreleased?
REL="$(git log -1 --format='%H %s' --grep='^release:' 2>/dev/null)"
if [ -z "$REL" ]; then
    echo "no release commit found — nothing to compare against"
    [ "$problems" -eq 0 ] && exit 0 || exit 1
fi
REL_SHA="${REL%% *}"
echo "last release commit: ${REL_SHA:0:7} ${REL#* }"

committed="$(git diff --name-only "$REL_SHA..HEAD" -- "${SHIPPED[@]}" 2>/dev/null \
             | grep -Ev "$NOISE" || true)"
dirty="$(git status --porcelain -- "${SHIPPED[@]}" 2>/dev/null | awk '{print $2}' \
         | grep -Ev "$NOISE" || true)"

if [ -z "$committed" ] && [ -z "$dirty" ]; then
    echo "everything that ships is in that release."
    [ "$problems" -eq 0 ] && exit 0
    echo
    echo "But see above: $problems thing(s) about this build's reproducibility."
    exit 1
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
