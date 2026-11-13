# ADR-0004: Vendored noVNC client

- Status: accepted
- Date: 2026-11-01
- Deciders: CloudCompile

## Context

The browser client needs a mature RFB implementation. Serving it from a
CDN would break offline/LAN use and adds an external dependency at
exactly the layer we most need to control (touch input, reconnect
behaviour).

## Decision

noVNC is vendored into `web/core` and `web/app` (MPL-2.0, notices in
`LICENSES/` and `THIRD_PARTY_NOTICES.md`), pinned and integrity-checked
in CI (`docs/VENDORED.sums`, issue #20). Upgrades are deliberate commits.

## Consequences

- The client works offline/LAN and cannot change underneath us.
- Security fixes in noVNC land only when we update the vendored copy —
  tracked via the integrity file and the release checklist.
- Our custom shell (`index.html`, `touch.js`) sits on stable noVNC
  internals; vendoring makes those internals reviewable.

## Revisit triggers

If the project ever ships a packaged web build (bundler, npm), vendoring
should be replaced by a lockfile-driven build step with the same
integrity guarantees.
