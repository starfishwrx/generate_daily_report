from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    release_dir = Path(sys.argv[1]).resolve()
    version = sys.argv[2] if len(sys.argv) > 2 else "dev"
    executables = {}
    for path in sorted(release_dir.glob("*.exe")):
        executables[path.name] = {"sha256": sha256(path), "size_bytes": path.stat().st_size}
    manifest = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signed": False,
        "executables": executables,
    }
    (release_dir / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
