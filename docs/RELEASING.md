# Releasing

This document describes how a roadmap milestone becomes a tagged release.
The roadmap itself lives in [ROADMAP.md](../ROADMAP.md) and on the
[project board](https://github.com/users/CloudCompile/projects/4).

## Versioning

The project follows [Semantic Versioning](https://semver.org/):

| Change | Version |
|---|---|
| Backwards-compatible bug fixes and hardening | **Patch** (`2.0.1`) |
| New features, backwards-compatible API additions | **Minor** (`2.1.0`) |
| Breaking API, configuration, state-schema or deployment changes | **Major** (`3.0.0`) |

Pre-1.0 conventions do not apply: the project is already on 2.x. A release
candidate, if needed, uses `v2.1.0-rc.1`; the release workflow accepts only
the final `vX.Y.Z` tag.

## Source of truth

- **Milestone** — the release scope.
- **Epic issue** (`EPIC: vX.Y.Z — …`) — goal, exit criteria and a linked
  checklist of every child issue.
- **`CHANGELOG.md`** — user-facing notes, in Keep a Changelog format.
- **ROADMAP.md** — the long-lived plan. Update it when scope moves between
  releases.
- **Project board** — execution status, area, priority and size.

Planned versions have a **draft GitHub release** created in advance. The
draft is a placeholder for the final notes and is published by the tag
workflow. Do not publish a draft by hand unless the workflow is unavailable.

## Release gates

All of these must be true before tagging:

- [ ] The milestone has no open `priority:p0` or `priority:p1` issues.
- [ ] Remaining `priority:p2`/`p3` work has been moved to a later milestone
      or to Icebox, with a comment explaining the move.
- [ ] CI is green on `main`.
- [ ] The changelog has a `## [X.Y.Z] — YYYY-MM-DD` section.
- [ ] Upgrade from the previous release and rollback have been tested on a
      clean host.
- [ ] `docs/API.md`, `README.md`, deployment and security docs match reality.
- [ ] New environment variables, state files and permissions are documented.
- [ ] Known limitations and security notes are current.

## Release procedure

1. **Freeze scope.** Move unfinished P2/P3 issues out of the milestone.
2. **Update the changelog.** Add a dated section under `## [Unreleased]`:

   ```markdown
   ## [2.1.0] — 2026-12-20

   ### Added
   - ...

   ### Fixed
   - ...
   ```

   Use `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed` and `Security`.
   Write for an operator, not for a diff reader.
3. **Verify the tree.** Run the same checks as CI locally:

   ```sh
   python3 -m py_compile control.py
   sh -n session.sh start.sh webapps.sh tunnel-url.sh
   # plus the test suite once v2.0.1 lands
   ```

4. **Open the release PR.** Title it `release: vX.Y.Z`. Review the changelog,
   version references and docs. Squash-merge to `main`.
5. **Tag the merge commit.**

   ```sh
   git checkout main && git pull
   git tag -a v2.1.0 -m "v2.1.0 — Architecture & Reliability"
   git push origin v2.1.0
   ```

6. **Let the release workflow publish.** `.github/workflows/release.yml`
   validates the tag and changelog, runs the syntax checks, extracts the
   changelog section and publishes or updates the existing draft release.
7. **Verify the published release.** Check the notes, assets, tag target and
   that the install instructions work from a clean checkout.
8. **Close the milestone** and the epic issue if they are complete.
9. **Announce** in the release notes and update the support matrix if the
   supported OS/Python/browser set changed.

## Hotfixes

A hotfix is a patch release from `main`:

1. Fix on `main` with a regression test.
2. Add a `### Fixed` entry to a new patch section.
3. Tag `vX.Y.Z+1` and run the normal workflow.
4. If `main` contains unreleased features, cherry-pick the fix to a
   `release/x.y` branch and tag from there instead. Document the branch in
   the release notes.

## Rollback

If a release is bad, do not delete tags or rewrite history:

1. Mark the GitHub release as **pre-release** and add a warning at the top.
2. Publish a follow-up patch with the fix, or advise the previous tag in the
   release notes.
3. Update `SECURITY.md` and the deployment docs if the regression is
   security-relevant.
4. Open an issue in the next patch milestone with the root cause and a test.

## Manual fallback

If Actions is unavailable:

```sh
VERSION=2.1.0
gh release view "v$VERSION" >/dev/null 2>&1 \
  && gh release edit "v$VERSION" --draft=false --notes-file release-notes.md \
  || gh release create "v$VERSION" --verify-tag --title "v$VERSION" --notes-file release-notes.md
```

Generate `release-notes.md` from the changelog section (and add the roadmap
link). Do not publish a release from a dirty or untested tree.
