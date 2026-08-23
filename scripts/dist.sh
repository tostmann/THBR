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
