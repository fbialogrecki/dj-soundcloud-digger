# Release and package-name migration

Distribution: `dj-digger` 1.1.0. Python module: `dj_digger`.
Command and XDG directories remain `dj-digger`.

## Publication prerequisites

- Complete the offline suite, Ruff and the specification-map check.
- Run real-FFmpeg multimedia tests and analysis invariance tests.
- Confirm clean installation and pip/pipx/uv migration on supported platforms.
- Record a live owner-private SoundCloud playlist test. Fixture coverage does
  not establish whether the undocumented endpoint currently lists every private playlist.
- Evaluate analysis against a licensed human-labelled music corpus. Synthetic
  invariants are not an accuracy benchmark. No device-tested badge without hardware evidence.
- A 404 from the public PyPI JSON endpoint is only absence of a published project,
  not a reservation or a guarantee that PyPI will accept the name.

## GitHub and Trusted Publisher

Rename the existing repository to `fbialogrecki/dj-digger`; keep history, issues,
releases and tags. Update the local remote. Do not create another repository
under `dj-soundcloud-digger`, which would break the old-name redirects.

On PyPI, configure a pending Trusted Publisher for the **new** `dj-digger`
project, with owner `fbialogrecki`, repository `dj-digger`, workflow
`publish.yml`, environment `pypi`. Update the old project's publisher to the
same renamed repository (the legacy branch retains the publishing workflow).
This requires the authenticated PyPI owner; GitHub administrator access alone
cannot change PyPI publisher settings. Do not paste credentials into issues/logs.

Build artifacts separately. Publish the new 1.1.0 package, then verify installation
from the registry before publishing the old-name informational 1.0.1. Never
publish both distributions from one mixed `dist/` directory.

## Legacy informational release

Branch `release/legacy-name-1.0.1` starts at `v1.0.0`, not at the new feature
branch. It keeps the old distribution and implementation and only updates the
version, migration notice and read-only refusal to open schema >1 on downgrade. It must not depend on `dj-digger`: both packages
own `dj_digger` and the `dj-digger` entry point, so pip installation ordering
cannot make a metapackage migration safe.

Users close the app, uninstall the old distribution with their original manager,
then install the new one with the desired extras. See README for pip, pipx and uv
commands. Uninstallation must not delete the XDG data/config directories.
Do not downgrade schema 2 automatically; a rollback requires the user to restore
the pre-migration SQLite backup while all app processes are stopped.
