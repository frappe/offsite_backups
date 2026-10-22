"""Decode checked-in OneDrive source payloads."""

from __future__ import annotations

import base64
import hashlib
import pathlib

EXPECTED = {
    "onedrive_settings.py": "9e07aa13fc054424919ab5bceae13331d9fef0601ea61070be8cc801067d8cdf",
    "onedrive_graph.py": "54e355f2fe68344c516a1de14a50059bb4cd176cb82a2ebf3ae8ab39f00cc25f",
    "test_onedrive_settings.py": "490050e03f8a6e45ea6e560935deb365cd1bbb14077ebb0f26eb9b1955eac5cc",
}
DESTINATION = pathlib.Path("offsite_backups/offsite_backups/doctype/onedrive_settings")


def main() -> None:
    """Write the decoded Python modules when every payload hash matches."""
    for name, digest in EXPECTED.items():
        parts = sorted(pathlib.Path("payload", name).glob("[0-9][0-9]"))
        if not parts:
            raise SystemExit(f"missing payload for {name}")
        encoded = "".join(part.read_text(encoding="ascii").strip() for part in parts)
        raw = base64.b64decode(encoded, validate=True)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise SystemExit(f"{name} sha256 {actual}")
        (DESTINATION / name).write_bytes(raw)


if __name__ == "__main__":
    main()
