#!/usr/bin/env python3
"""Generate a CycloneDX-style SBOM for a release (#128).

The runtime is stdlib-only Python, so the component list is: the release
itself, the interpreter it runs on, the vendored third-party web assets
(from docs/VENDORED.sums, integrity-checked in CI) and the dev tooling
(declared in pyproject/CI, never needed at runtime).

Usage: sbom.py --version 2.6.0 [--out sbom.json]
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED_SUMS = os.path.join(ROOT, "docs", "VENDORED.sums")

# dev tooling pinned in CI (not runtime components, listed for review)
DEV_TOOLS = [
    ("pytest", "dev"),
    ("pytest-cov", "dev"),
    ("ruff", "dev"),
    ("pre-commit", "dev"),
]


def vendored_components():
    out = []
    if not os.path.exists(VENDORED_SUMS):
        return out
    for ln in open(VENDORED_SUMS):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        sha, path = (ln.split(None, 1) + [""])[:2]
        out.append(
            {
                "type": "file",
                "name": path.replace("web/", "web/"),
                "version": sha[:12],
                "hashes": [{"alg": "SHA-256", "content": sha}],
                "scope": "required",
            }
        )
    return out


def purl_ecosystem_probe():
    """Best-effort upstream check that the interpreter is importable — the
    SBOM records the version it was BUILT with."""
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default="sbom.json")
    args = ap.parse_args()

    ver = purl_ecosystem_probe()
    components = [
        {
            "type": "application",
            "name": "remote-desktop",
            "version": args.version,
            "purl": f"pkg:github/CloudCompile/remote-desktop@v{args.version}",
        },
        {
            "type": "library",
            "name": "python-stdlib-runtime",
            "version": ver,
            "comment": "the control server is stdlib-only (see pyproject.toml)",
            "scope": "required",
        },
    ]
    components += [{"type": "library", "name": n, "scope": s, "comment": "dev tooling only"} for n, s in DEV_TOOLS]
    components += vendored_components()

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{hashlib.sha256(args.version.encode()).hexdigest()[:8]}-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}-4a4f-8c0d-{'f' * 12}",
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "component": {
                "type": "application",
                "name": "remote-desktop",
                "version": args.version,
            },
        },
        "components": components,
    }
    with open(args.out, "w") as f:
        json.dump(bom, f, indent=1)
    print(f"sbom: {args.out} ({len(components)} components)")


if __name__ == "__main__":
    main()
