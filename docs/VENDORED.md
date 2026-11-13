# Vendored code provenance (#20)

Recorded: 2026-11-13. Regenerate with:
find web/vendor web/core -type f | sort | xargs sha256sum > docs/VENDORED.sums

## noVNC — `web/core/`, `web/app/`, `web/include/`

- **Upstream:** https://github.com/novnc/noVNC (Apache-2.0, bundled per
  THIRD_PARTY_NOTICES.md)
- **Customised:** this copy is modified for the mobile shell (touch layer
  integration, reconnect behaviour). Do not hot-swap with an upstream
  release without diffing `web/touch.js` and the reconnect paths in
  `web/core/rfb.js`.
- **Provenance:** upstream noVNC v1.x tree, customized. Exact upstream
  commit lineage predates the vendoring snapshot and is not recorded here;
  updates replace the tree wholesale and must re-record `VENDORED.sums`.

## pako — `web/vendor/pako/`

- **Upstream:** https://github.com/nodeca/pako (MIT + zlib license)
- **Version:** 2.x line (see its own README.md); imports trimmed to the
  deflate/inflate paths noVNC uses.

## Integrity

`VENDORED.sums` holds the SHA-256 of every vendored file at the last
vendor refresh. CI can re-verify with:

    sha256sum -c docs/VENDORED.sums

A change to a vendored file without a matching entry is either a local
customisation (must be documented above) or tampering.
