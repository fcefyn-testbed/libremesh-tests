#!/usr/bin/env python3
"""
Resolve LG_IMAGE path from firmware catalog for images on the lab TFTP server.

Usage:
  resolve_firmware_from_catalog.py <device> <place> [catalog_key]

  device      - Device type from labnet (e.g. linksys_e8450)
  place       - Full labgrid place (e.g. labgrid-fcefyn-belkin_rt3200_1)
  catalog_key - Optional. Key from catalog (e.g. lime-2024.1). Default: use device default.

Output: prints the absolute path to stdout, or exits non-zero with error on stderr.
"""
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: resolve_firmware_from_catalog.py <device> <place> [catalog_key]", file=sys.stderr)
        return 1

    device = sys.argv[1]
    place = sys.argv[2]
    catalog_key = sys.argv[3] if len(sys.argv) > 3 else None

    # Extract TFTP folder from place: labgrid-fcefyn-belkin_rt3200_1 -> belkin_rt3200_1
    if "-" in place:
        parts = place.split("-", 2)
        if len(parts) >= 3:
            tftp_folder = parts[2]
        else:
            tftp_folder = place
    else:
        tftp_folder = place

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
    images = dev_config.get("images", {})
    default_key = dev_config.get("default", "default")

    key = catalog_key if catalog_key else default_key
    if key not in images:
        print(f"ERROR: Catalog key '{key}' not found for device '{device}'", file=sys.stderr)
        return 5

    filename = images[key]["filename"]
    path = Path("/srv/tftp") / tftp_folder / filename
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
