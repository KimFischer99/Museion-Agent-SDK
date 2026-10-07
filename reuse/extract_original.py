"""Extract one known source helper for PRIVATE assessment, never for auto-publishing.
Usage: python reuse/extract_original.py /path/vendor-archive.zip /private/new-directory
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import sys
import zipfile

ARCHIVE_SHA = "088277b0a712fec0e2bcecb61c25e5450a277e08cd84579ec963ff2061bbde8c"
HELPER_SHA = "c87af221181a1559e3adcb0cdd601f5be5d4bd91eec2fe0597c09dc27558e741"
MEMBER = "muse-sdk/architecture/hooks/runtime/hatch_hook_runtime.sh"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    archive, destination = map(Path, sys.argv[1:])
    with archive.open("rb") as f:
        sha = hashlib.file_digest(f, "sha256").hexdigest()
    if sha != ARCHIVE_SHA:
        raise SystemExit("Archive hash differs; review the new source rather than silently accepting it")
    with zipfile.ZipFile(archive) as z:
        info = z.getinfo(MEMBER)
        if info.file_size > 65536:
            raise SystemExit("Unexpected member size")
        data = z.read(info)
    if hashlib.sha256(data).hexdigest() != HELPER_SHA:
        raise SystemExit("Helper hash mismatch")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    p = destination / "hatch_hook_runtime.sh"
    with p.open("xb") as f:
        f.write(data)
    os.chmod(p, 0o600)
    (destination / "DO_NOT_PUBLISH.txt").write_text(
        "PRIVATE source reference. No redistribution grant was identified in the supplied archive.\n"
        "Do not include this directory in a public package, repository, container, or release.\n"
        "Technical compatibility is not a license. See AUDIT_AND_REUSE.md.\n"
        + "Original SHA256: " + HELPER_SHA + "\n", encoding="utf-8")
    print("Extracted known helper for private review; redistribution remains unverified.")


if __name__ == "__main__": main()
