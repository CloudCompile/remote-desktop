#!/usr/bin/env python3
"""Check that local markdown links in README.md and docs/ resolve (#13)."""
import os
import re
import sys
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINK = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")


def md_files():
    out = [os.path.join(BASE, "README.md")]
    docs = os.path.join(BASE, "docs")
    out += [os.path.join(docs, f) for f in sorted(os.listdir(docs)) if f.endswith(".md")]
    return out


def main():
    bad = []
    for path in md_files():
        text = open(path).read()
        for target in LINK.findall(text):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            full = os.path.normpath(os.path.join(os.path.dirname(path), urllib.parse.unquote(target)))
            if not os.path.exists(full):
                bad.append(f"{os.path.relpath(path, BASE)} -> {target}")
    for b in bad:
        print(f"broken link: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
