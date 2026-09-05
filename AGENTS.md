# AGENTS.md

Repository-wide routing and guardrails for `dj-digger`, a Python 3.12+
terminal crate-digging CLI/TUI.

## Source of truth

- Product behavior, architecture boundaries, public interfaces, data models,
  privacy, and security requirements live in `PROJECT-SPECIFICATION.md`.
- Never read `PROJECT-SPECIFICATION.md` end to end unless the user explicitly
  requires an exhaustive audit and repository instructions allow it.
- Start by running `python3 scripts/spec_section_map.py --print-map`.
- Read only the mapped sections relevant to the current task. For a known symbol,
  field, endpoint, environment variable, class, or collection, use `rg`.
- `PROJECT-SPECIFICATION.md` records shipped behavior only. If behavior is not
  described and cannot be confirmed in code, do not assume it exists.
- Update the affected specification sections in the same change whenever
  modifying product behavior, interfaces, data models, security/privacy behavior,
  or component ownership.
- After editing the specification, run `python3 scripts/spec_section_map.py`,
  followed by `python3 scripts/spec_section_map.py --check`.
- Plans, target designs, TODOs, open questions, and acceptance checklists belong
  in `specs/` or `docs/`, not in `PROJECT-SPECIFICATION.md`.
- Keep `AGENTS.md` concise. Put repository-wide rules here and detailed
  operational procedures in dedicated documentation.

## Repository map

- `dj_digger/cli.py`, `services/`, `models.py`, `crate_models.py`, `links.py`: entry, collection
  orchestration, shared objects, classification, and exports.
- `dj_digger/soundcloud.py`, `html_fallback.py`: SoundCloud API v2 and saved-page
  inputs.
- `dj_digger/tui/`, `player.py`: Textual crate browser and optional in-memory
  audio preview.
- `dj_digger/db.py`, `schema.py`, `state.py`, `library.py`, `scanner.py`, `config.py`: SQLite
  state, crates, local-file matching, and preferences.
- `dj_digger/auth.py`, `spotify.py`, `gates/`, `stores/`, `http.py`, `browser.py`: external
  authentication, gate/store integrations, browser handoff, and cart safety.
- `tests/`: offline suite plus explicitly marked live contract checks.
- `.github/workflows/`: CI, scheduled live monitoring, and PyPI publishing.

## Engineering rules

- Prefer the standard library and small, direct diffs; do not add dependencies
  for behavior already covered by the platform or existing packages.
- Fix root causes. When a core signature or data structure changes, update every
  caller and the corresponding specification contract.
- Long-running network or disk work in `dj_digger/tui/` must run in Textual
  workers/background threads and return UI mutations to the UI thread.
- Offline tests must never reach live endpoints, real XDG application state, or
  the user's music/download folders. Use fakes and `tests/fixtures/`.
- Treat artist links, summary files, redirects, provider responses, and filenames
  as untrusted. Preserve URL scheme/domain-boundary checks, SSRF guards, private
  credential writes, download limits, atomic file replacement, and cart
  revalidation.
- Keep SoundCloud, Spotify, store, and managed-browser credentials out of logs,
  fixtures, documentation, and commits.
- If an intentional shortcut is accepted, document it locally as
  `# ponytail: <reason>`.

## Prohibited operations

- Do not publish, deploy, create a release, run production migrations, or invoke
  live/write-capable third-party flows unless the user explicitly requests it.
- Do not delete or rewrite user databases, config, credentials, managed browser
  profiles, downloads, or music files.
- Do not run live pytest markers as part of the default verification command.
- Do not weaken URL, credential, file-download, gate-consent, or cart-mutation
  boundaries to make a test pass.

## Verification

Minimum checks for a normal code change:

```bash
python3 scripts/spec_section_map.py --check
uv run --extra dev ruff check .
uv run --extra dev pytest
```

Run focused tests while iterating. The default pytest configuration excludes
`live`, `shop_live`, and `hypeddit_live`; run those markers only when explicitly
needed to validate an external contract. Preserve unrelated worktree changes.
