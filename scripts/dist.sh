#!/usr/bin/env bash
# Collect the flash images of the last build into addon/firmware/<chip>/ and
# write the manifest the container flashes from.
#
# One directory per chip: the same add-on serves a C6 stick and a C5 one, and
# it picks the bundle that matches the chip it finds on the port.  Run this
# once per target; each run replaces only its own chip.
#
#   THBR_STAGE=2 scripts/build.sh && scripts/dist.sh
#
# Offsets and flash parameters come from the build's flash_args, so a partition
# layout change cannot drift away from what gets flashed.  Every image is
# written atomically and read back (the repository lives on NFS, where a
# half-written file can look complete to the next reader).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD="${THBR_BUILD_DIR:-/tmp/thbr_idf_build}"
OUT="$ROOT/addon/firmware"

python3 - "$ROOT" "$BUILD" "$OUT" <<'PY'
import hashlib, json, os, re, sys
root, build, out = sys.argv[1:4]
os.makedirs(out, exist_ok=True)

ver = open(os.path.join(root, "main/version.h")).read()
fw = re.search(r'FW_VERSION_STRING\s+"([^"]+)"', ver).group(1)
date = re.search(r'FW_BUILD_DATE\s+"([^"]+)"', ver).group(1)
cfg = open(os.path.join(build, "config/sdkconfig.json")).read() if os.path.exists(os.path.join(build, "config/sdkconfig.json")) else open(os.path.join(root, "sdkconfig")).read()
chip = re.search(r'IDF_TARGET"?[=:]\s*"([a-z0-9]+)"', cfg).group(1)

# The bundle for this chip, and nothing else, lives here.
out = os.path.join(out, chip)
os.makedirs(out, exist_ok=True)

lines = open(os.path.join(build, "flash_args")).read().split("\n")
flash_args = lines[0].strip()
images = []
for line in lines[1:]:
    if not line.strip():
        continue
    off, rel = line.split()
    images.append((off, rel))

def sha(b): return hashlib.sha256(b).hexdigest()

def atomic_copy(src, dst):
    data = open(src, "rb").read()
    want = sha(data)
    for attempt in range(3):
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, dst)
        dfd = os.open(os.path.dirname(dst), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        if sha(open(dst, "rb").read()) == want:
            return want, len(data)
        print(f"  read-back mismatch on {dst}, retry {attempt + 1}", file=sys.stderr)
    sys.exit(f"could not write {dst} intact after 3 attempts")

# Check the artefact against what the REPOSITORY says it should be, before
# anything is published.
#
# Checking the binary against the build's own config is not enough and would
# have missed the mistake this exists for: on 2026-08-25 a generated
# sdkconfig.ble had gone stale, the build was perfectly self-consistent, and it
# shipped a value a commit two days earlier had already corrected.  So the
# reference is the intent in the tree -- an explicit setting in any
# sdkconfig.defaults* overlay, else the Kconfig default -- and it is compared
# against both the effective config AND the bytes of the image.
def repo_intent(key):
    """What this tree says the value should be: overlay wins, else Kconfig."""
    for name in sorted(os.listdir(root)):
        if not name.startswith("sdkconfig.defaults"):
            continue
        m = re.search(r'^CONFIG_%s="([^"]*)"' % re.escape(key),
                      open(os.path.join(root, name)).read(), re.M)
        if m:
            return m.group(1), name
    kc = os.path.join(root, "main/Kconfig.projbuild")
    if os.path.exists(kc):
        m = re.search(r'config\s+%s\b.*?^\s*default\s+"([^"]*)"' % re.escape(key),
                      open(kc).read(), re.M | re.S)
        if m:
            return m.group(1), "Kconfig default"
    return None, None

def effective(key):
    j = os.path.join(build, "config/sdkconfig.json")
    if os.path.exists(j):
        import json as _json
        return _json.load(open(j)).get(key)
    m = re.search(r'^CONFIG_%s=(.*)$' % re.escape(key), cfg, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    return v.strip('"') if v.startswith('"') else (True if v == "y" else v)

app = None
for off, rel in images:
    if os.path.basename(rel).startswith("thbr"):
        app = os.path.join(build, rel)

# Only string values survive into the image recognisably -- but that is exactly
# the class of setting that points somewhere, and so the class that hurts when
# it points at the wrong place.
STRING_CHECKS = [("THBR_BLE_PROXY_URI", "BT_ENABLED"),
                 ("THBR_TAP_STICK_IPV4", "THBR_TRANSPORT_TAP"),
                 ("THBR_TAP_HOST_IPV4", "THBR_TRANSPORT_TAP"),
                 ("THBR_TAP_NETMASK", "THBR_TRANSPORT_TAP")]
for key, gate in STRING_CHECKS:
    if gate and not effective(gate):
        continue
    want, whence = repo_intent(key)
    have = effective(key)
    if want is None:
        print(f"  note: no repository intent found for {key}, not checked")
        continue
    if have != want:
        sys.exit(f"{key} is {have!r} in this build, but the tree says {want!r} "
                 f"({whence}).\n"
                 f"  The generated sdkconfig this was built from is stale.\n"
                 f"  Delete it and rebuild -- scripts/build.sh does that for you.\n"
                 f"  Changed it on purpose?  Then the tree does not know yet: put\n"
                 f"  it in an sdkconfig.defaults overlay, which is what this check\n"
                 f"  reads as intent, and menuconfig alone is not.")
    if app and want.encode() not in open(app, "rb").read():
        sys.exit(f"{os.path.basename(app)} does not contain {key}={want!r} -- "
                 f"the image and its config disagree.")
    print(f"  verified against the tree: {key}={want} ({whence})")

manifest = {"product": "THBR", "fw": fw, "build": date, "chip": chip,
            "flash_args": flash_args, "images": []}
for off, rel in images:
    name = os.path.basename(rel)
    digest, size = atomic_copy(os.path.join(build, rel), os.path.join(out, name))
    manifest["images"].append({"offset": off, "file": name, "size": size, "sha256": digest})
    print(f"  {off:>10}  {name:<22} {size:>8} B  {digest[:12]}")

tmp = os.path.join(out, "manifest.json.tmp")
with open(tmp, "w") as f:
    json.dump(manifest, f, indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(tmp, os.path.join(out, "manifest.json"))
print(f"firmware bundle: THBR {fw} ({date}, {chip}), {len(images)} images -> {out}")
PY
