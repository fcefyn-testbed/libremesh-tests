#!/usr/bin/env python3
"""
Resolve LG_IMAGE path from firmware catalog for images on the lab TFTP server.

Outputs path to file in /srv/tftp/firmwares/<device_folder>/{openwrt,libremesh}/.
Labgrid's stage() creates the symlink in the place folder when booting.

Usage:
  resolve_firmware_from_catalog.py <device> [catalog_key]

  device      - Device type from labnet (e.g. linksys_e8450)
  catalog_key - Optional. Key from catalog (e.g. openwrt-24.10.5). Default: use device default.

Output: prints the absolute path to stdout, or exits non-zero with error on stderr.
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: resolve_firmware_from_catalog.py <device> [catalog_key]",
            file=sys.stderr,
        )
        return 1

    device = sys.argv[1]
    catalog_key = sys.argv[2] if len(sys.argv) > 2 else None

    repo_root = Path(__file__).parent.parent
    catalog_path = repo_root / "configs" / "firmware-catalog.yaml"
    if not catalog_path.exists():
        print(f"ERROR: Catalog not found: {catalog_path}", file=sys.stderr)
        return 3

    with open(catalog_path) as f:
        catalog = yaml.safe_load(f)

    devices = catalog.get("devices", {})
    if device not in devices:
        print(f"ERROR: Device '{device}' not in catalog", file=sys.stderr)
        return 4

    dev_config = devices[device]
    firmwares_folder = dev_config.get("firmwares_folder", device)
    images = dev_config.get("images", {})
    default_key = dev_config.get("default", "default")

    key = catalog_key if catalog_key else default_key
    if key not in images:
        print(
            f"ERROR: Catalog key '{key}' not found for device '{device}'",
            file=sys.stderr,
        )
        return 5

    filename = images[key]["filename"]
    path = Path("/srv/tftp") / "firmwares" / firmwares_folder / filename
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
