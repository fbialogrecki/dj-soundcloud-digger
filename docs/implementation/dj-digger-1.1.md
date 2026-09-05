# dj-digger 1.1 implementation ledger

Approved scope: local media identity and v2 SQLite migration; lazy file explorer,
local playlists and playback; bounded BPM/key analysis; device compatibility
profiles and complete-folder copy or journaled replacement; own SoundCloud
profile playlist import including authorized private playlists; GitHub/PyPI rename.

Defaults: WAV integer PCM, max 24 bit/48 kHz; copy all selected audio to a new
folder; no upsampling/downmix/normalization; replacement has temporary recovery
originals only. No USB formatting, rekordbox libraries, or tag writes by analysis.
Old PyPI package gets an informational 1.0.1 release, NOT a metapackage.

## Progress
- [x] Baseline: 838 offline tests passed; public API profile/pagination checked live
- [ ] Private owner API completeness and PyPI publisher access (no authenticated session)
- [x] Identity, v2 migration, single-instance guard, media repository
- [x] Local explorer/playlists/playback (integration and duplicate-occurrence tests pass)
- [x] Media inspection, deck rules, verified copy/replacement/recovery (failure-boundary tests pass)
- [x] Bounded analysis and manual overrides (invariant tests; corpus evaluation still needed)
- [x] Profile playlist import (fixture tests cover identity/session/partial responses)
- [x] Documentation, multimedia tests, Linux pip/pipx/uv migration checks
- [x] Cross-platform CI on implemented code (77983e0); final test-only stabilization rerun
- [x] GitHub repository and remote renamed to fbialogrecki/dj-digger
- [x] Separate informational 1.0.1 worktree and artifacts based on v1.0.0
- [ ] PyPI publication (new first, legacy second); owner authentication/publisher setup missing

## Validation / outstanding external evidence
Tests must use temporary data, never the user's library or credentials. Hardware
compatibility is manufacturer-documented until physically tested. Actual audio
analysis accuracy requires a licensed, human-labelled evaluation corpus.

## Validation recorded during implementation

- Original main baseline: 838 offline tests passed.
- Feature suite: 869 passed, 81 live tests deselected; subsequent focused UI
  synchronization checks: 7 passed.
- Legacy informational branch: 788 passed, 81 live tests deselected, including
  read-only downgrade refusal.
- Real FFmpeg: mixed MP3/WAV/FLAC, canonical 24-bit RIFF, copy hashes,
  every replacement commit boundary, changed sources, links, exclusive rename,
  same-filesystem rename identity, partial copy resume and metadata overrides.
- Analysis: spawned silence/abstention, anti-phase channel and block-boundary invariance.
- Isolated bare-wheel startup/import works without play/analyze extras.
- Actual pip, pipx and uv-tool old-uninstall/new-install smoke checks passed on Linux,
  with temporary data sentinels preserved. No user's installed app or library was modified.
- Public SoundCloud owner resolution and a paginated playlist response were checked live.
- CI run [33970627345](https://github.com/fbialogrecki/dj-digger/actions/runs/33970627345)
  passed all nine OS/Python combinations, isolated builds/installs and pip/pipx/uv
  old-name migration on Linux, macOS and Windows (Python 3.14).
- Windows CI exposed writable-handle fsync and explicit mmap cleanup requirements;
  both fixed. A UI test now dispatches its button event before waiting for workers.
- No private-session or physical-deck test has been performed. Human-labelled
  corpus requested; `scripts/evaluate_analysis.py` is ready to evaluate it.
