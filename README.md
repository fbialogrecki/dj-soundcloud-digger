# 🎧 dj-digger

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![Built with Textual](https://img.shields.io/badge/TUI-Textual-ff69b4.svg)](https://textual.textualize.io/)
[![Version](https://img.shields.io/badge/version-1.1.0-orange.svg)](pyproject.toml)

> **A local-first playlist workflow for DJs.**
> Turn SoundCloud playlists, likes, and artist profiles into an auditionable,
> searchable library, then find the safest available path to download or buy
> each track.

`dj-digger` keeps the whole digging loop in one terminal application:
collect a playlist, preview and filter it, mark decisions, match music already on
disk, and follow verified store or download links. Your library and preferences
stay on your machine, and checkout always stays in your hands.

The current implemented behavior, architecture, interfaces, data model, and
security/privacy boundaries are maintained in
[PROJECT-SPECIFICATION.md](PROJECT-SPECIFICATION.md).

```bash
dj-digger https://soundcloud.com/someone/sets/that-playlist
```

---

## ⚡ The workflow

- **Collect complete playlists**: SoundCloud API v2 reads playlists, likes, profiles,
  and individual tracks without relying on the page's finite rendered list. A
  saved-HTML fallback covers private and unlisted sources.
- **Audition and narrow down**: Preview, seek, search, filter, and sort tracks in
  the Textual interface without leaving the playlist.
- **Remember every decision**: Local playlists keep `got it` / `skipped` status across
  playlists, refreshes identify new additions, and a library scan finds tracks
  already on disk.
- **Find the track, not just a link**: Store, smart-link, and download-gate URLs
  are classified and expanded into useful Bandcamp, Beatport, shop, or download
  destinations.
- **Acquire with guardrails**: Download artist-provided files, resolve supported
  gates, verify Bandcamp products, or prepare a Beatport playlist. Ambiguous
  matches and checkout remain manual.
- **Keep control locally**: Playlists, decisions, credentials, and browser profiles
  remain on your machine; there is no project-hosted account or backend.

## Additional capabilities

- **API-first collection**: A 300-track playlist can be read from SoundCloud in
  about three seconds using batch hydration. Following third-party purchase links
  takes as long as those providers take; a 484-track playlist is around a minute.
- **Store and gate classification**: Group links into **Bandcamp**, **Beatport**,
  **Traxsource**, **JunoDownload**, **record shops**, **download gates**, **smart
  links**, and **direct SoundCloud downloads**.
- **In-memory audio preview**: Stream, seek, prefetch upcoming tracks, and render
  block waveforms with a reactive audio meter, powered by `miniaudio`.
- **Multi-playlist local library**: Save, switch, refresh, and search playlists stored in
  `~/.local/share/dj-digger/digger.db`.
- **Cross-playlist track memory**: A decision made for a SoundCloud track applies
  when that track appears in another playlist.
- **🔓 Download Gate Automation**: Resolves follow-to-download gates (Hypeddit, ToneDen, GateRush, Droploud) through their supported download flows. SoundCloud, Instagram, YouTube and similar link steps are click-through markers: the app does not call their follow, like, repost, comment APIs or open external social links to simulate a click. A gate may receive the name or email configured in Settings when its manifest requires them. Spotify steps are reported like the other click-throughs: Hypeddit clears them through its own Spotify app, so no Spotify login is needed or used. CAPTCHA and unknown provider steps remain manual in the private Chromium profile. Gate automation remains subject to the provider's terms.
- **🔗 Link-Hub Expansion**: A purchase link that turns out to be a list of shops rather than a download—an ampsuite release page, a gate running in smart-link mode—is opened, and the Bandcamp and Beatport links behind it are added to the track directly instead of a `gate` badge.
- **🛒 Store Purchase Assistance**: An optional, user-triggered flow verifies Bandcamp additions and prepares Beatport tracks as an importable playlist. Login, playlist transfer, and checkout stay manual.
- **🆕 New Since Last Refresh**: Refreshing a playlist marks whatever it gained with `NEW` and sorts it to the top.
- **📄 Saved-HTML Fallback**: Fully supports saved HTML pages (`Ctrl+S`) for private or unlisted SoundCloud playlists.
- **⚙️ CLI & Non-Interactive Mode**: Export playlists directly to JSON or CSV for automated pipelines and scripts.

---

## 📦 Installation

### Recommended (via `uv` or `pipx`)

```bash
# Install with audio preview; store-cart support is included
uv tool install 'dj-digger[play]'
```

or with `pipx`:

```bash
pipx install 'dj-digger[play]'
```

### From Source (Development)

```bash
git clone https://github.com/fbialogrecki/dj-digger.git
cd dj-digger
uv venv
uv pip install -e '.[play,dev]'
```

Run the working tree without installing or releasing anything:

```bash
uv run --extra play dj-digger
```

The browser is drawn on standard error, so `2>file` would redirect the interface
itself and leave you looking at a blank terminal. To keep a log while it is up,
use `--log-file`:

```bash
uv run --extra play dj-digger --log-level DEBUG --log-file /tmp/dj-digger.log
```

To try it against a throwaway library instead of your real playlists, point the XDG
directories somewhere temporary for that one run:

```bash
XDG_DATA_HOME=/tmp/dj-dev XDG_CONFIG_HOME=/tmp/dj-dev XDG_CACHE_HOME=/tmp/dj-dev uv run --extra play dj-digger
```

> **Requires Python 3.12 or newer.**
>
> **Note on optional extras**:
> - `play`: Enables in-memory audio preview via `miniaudio`.
> Store-cart support is included. On the first `c` or `C`, the app asks before
> downloading its matching Chromium build automatically.

---

## 🔗 Supported Input Links

| Input Type | Supported URL Pattern | What You Get |
| --- | --- | --- |
| **Playlist / Set** | `soundcloud.com/user/sets/playlist-name` | Every track in the playlist |
| **User Likes** | `soundcloud.com/user/likes` | Every track liked by the user |
| **Artist Profile** | `soundcloud.com/user` | All tracks uploaded by the artist |
| **Single Track** | `soundcloud.com/user/track-name` | Single track metadata & purchase links |
| **Saved HTML** | `playlist.html` | Private / unlisted playlist saved locally |
| **Interactive Prompt**| *Run without arguments* | Prompts for a link or opens saved playlists |

---

## 🖥️ Interactive TUI Workflow

Launching `dj-digger` opens an interactive Textual browser divided into three key areas:
1. **Playlist Sidebar (`Ctrl+B`)**: Switch between saved playlists, add new links (`a`), or refresh existing playlists (`r`).
2. **Track Table**: Displays tracks with status badges (`·` untouched, `○` opened, `✓` got, `✗` skipped), position, artist/title, available store badges, genre, and duration.
3. **Player & Waveform Bar**: A four-row, 32-level waveform with a real-time VU level meter, over a one-row control strip: previous / play-pause / next buttons, track title, clock, a drag-to-set volume slider, and a close button.

```
▸ 0 all  1 soundcloud·18  2 bandcamp·12  3 gate·53      83/83 tracks · got 0 · skipped 4
```

### Keybinding Reference

Press `?` inside the TUI at any time to view the full grouped keybinding modal.

#### Track Navigation & Status Marks
| Key | Action |
| --- | --- |
| `Up` / `Down` | Navigate track rows |
| `o` or `Enter` | Open the best link (or active store filter) in your default web browser |
| `d` | Download the highlighted artist-provided or gate file to your download folder (set it with `s`) |
| `Shift+d` | Download all eligible tracks in the current view |
| `Ctrl+X` | Stop the running dig or download batch; finished files are kept, unfinished tracks stay new |
| `g` | Mark track as **Got** (`✓`) and move to next track |
| `k` | Mark track as **Skipped** (`✗`) and move to next track |
| `u` | Clear track status mark (`·`) |
| `x` | Remove track from current playlist (`Ctrl+Z` to undo) |
| `y` | Copy the path of the local file that matches this track (`▣` in the first column) |
| `b` / `Shift+b` | Search in Bandcamp / Search in Beatport for the highlighted track |

#### Audio Preview Controls
| Key | Action |
| --- | --- |
| `Space` | Play / Pause highlighted track |
| `[` / `]` | Seek backward / forward 10 seconds |
| `n` / `p` | Advance to Next / Previous track |
| `-` / `=` | Decrease / Increase playback volume (`m` to mute/unmute) |
| `Ctrl+W` | Stop playback and close the player bar |
| *Mouse Click* | Click anywhere on the waveform display to seek immediately, or use the buttons and volume slider under it |

#### Filtering, Stores & Library
| Key | Action |
| --- | --- |
| `/` | Live search/filter by artist, title, genre, tag or label (every word must match, any order) |
| `t` / `Shift+t` | Sort by title, time, genre, status or store (`t` cycles, `Shift+t` reverses); the header shows the arrow |
| `v` / `Shift+v` / `Ctrl+A` | Select a row / extend the selection to here / select everything shown; batch keys then act on the selection |
| `1` – `9` | Jump directly to store category filter |
| `0` | Reset store filter (show all tracks) |
| `h` | Toggle hiding handled tracks (`got` / `skipped`) |
| `Escape` | Clear the selection; then the search; then the store filter and hiding |
| `Shift+o` | Open all visible store links in browser (asks confirmation for >20 links) |
| `c` | Add the highlighted track to Bandcamp, or prepare it for a Beatport playlist |
| `Shift+c` | Review visible store tracks, add Bandcamp items, and prepare a Beatport playlist |
| `Shift+p` | Open every exact Beatport track page shown in your regular browser, to add to cart by hand (asks above 20) |
| `e` | Export visible rows to file |
| `a` | Add a new playlist from a SoundCloud URL |
| `r` | Refresh current playlist from SoundCloud (preserves local deletions) |
| `Shift+x` | Delete the highlighted playlist, after confirming |
| `Shift+u` | Reset every track in the playlist to untouched |
| `Ctrl+B` | Toggle Playlist Sidebar |
| `s` | Settings: profile, folders, browser, store session |
| `?` | Full keybinding help |
| `q` / `Ctrl+C` | Quit |

---

## 🏷️ Store & Gate Categories

Links are parsed and categorized using strict domain-boundary matching:

| Category | Description / Included Domains |
| --- | --- |
| `soundcloud` | Direct artist-provided download link enabled on SoundCloud |
| `bandcamp` | Official Bandcamp release page |
| `beatport` | Beatport purchase link |
| `traxsource` | Traxsource store link |
| `junodownload` | JunoDownload purchase page |
| `apple` | Apple Music / iTunes Store link |
| `shop` | Specialized record stores (*Boomkat, Hard Wax, Clone, Decks, Deejay, Red Eye, Juno, Phonica, Rush Hour, Bleep, Gumroad*) |
| `gate` | Follow-to-download gates (*Hypeddit, Gaterush, Droploud, Wump, Artist Union, Toneden, Pump Your Sound*) |
| `smartlink` | Landing pages (*lnk.to, ffm.to, fanlink, smarturl, orcd.co, DistroKid, Linktree*) |
| `streaming` | Pure streaming platforms (*Spotify, YouTube, Deezer, Tidal*) |
| `no-link` | No purchase or download link found |
| `others` | Unrecognized external web link |

### Bandcamp carts and Beatport playlists

Pressing `c` or `C` does the product checks and the cart clicks in a hidden
Chromium; nothing pops up while it works. A window appears only when there is
something for you: the completed Bandcamp cart, or items left to finish by
hand. That window is a separate browser carrying the same cookies, so the
hidden session is never disturbed. A desktop display is needed for those
moments only (WSL users need WSLg); without one the additions still stand and
the result screen says the window could not be shown. If the matching browser
build is absent, the app asks before downloading it in the background and then
resumes the cart preflight. The app uses a dedicated persistent browser profile,
separate from your everyday browser. A Bandcamp account is not required for its
cookie-backed cart; opening a manual Bandcamp session in Settings is optional.
Beatport login is never attempted there. The app never reads or fills your
password, chooses a payment method, or completes checkout.

- `c` resolves the highlighted track, then adds it to Bandcamp or prepares it for
  a Beatport playlist.
- `C` resolves all currently visible, unhandled tracks, then asks for one batch
  confirmation. The active Bandcamp or Beatport filter limits the target store.
- When both Bandcamp and Beatport filters are explicitly active, each track is
  handled for both destinations: Bandcamp is added to its cart and Beatport is
  retained for the transfer playlist.
- With no store filter, Bandcamp is tried first. Beatport is used only when the
  exact track is genuinely unavailable for individual purchase on Bandcamp—not
  when Bandcamp fails technically.

Batch mode resolves every candidate first and shows exact products, prices,
currencies, existing cart items, and skips before Bandcamp is changed. Bandcamp
rows with seller-approved flexible pricing show their minimum; highlight one and
press `E` to enter a higher value. Fixed-price rows cannot be edited. Bandcamp
pages are rechecked immediately before each click. Ambiguous titles, version
mismatches, changed prices or product IDs, CAPTCHA, and changed store UI stop the
affected Bandcamp operation instead of guessing. Two reusable work tabs bound
preflight; Bandcamp mutation is serial, and only its successful final cart tab
remains open for format selection and checkout.

If a Bandcamp link moved or points only to an artist/label page, the app uses the
site's visible autocomplete as a bounded fallback. It accepts only canonical,
exact track matches and inspects at most three returned album pages; it never
enters the CAPTCHA-protected full results page. A track sold only as part of a
full album is reported as album-only instead of silently adding the whole album.
After a click, verification uses the cart count, a visible removable row in the
opened side cart, and one reload check, each on its own clock. An uncertain
cart remains open for inspection and is never clicked again automatically; the
app saves a screenshot and a redacted copy of the page under
`cart-diagnostics` in its data folder (last ten kept) so a broken flow can be
reported with the page that broke it. After two unverified clicks in one
batch the app stops clicking: it opens the remaining products with Buy
expanded and the price filled, asks you to press Add to cart yourself, and
then checks the cart once. The result screen offers the same **Finish in
browser** for anything left uncertain.

Beatport login and cart mutation are not automated: `c`/`Shift+C` prepare a
playlist, and `Shift+P` opens the exact Beatport track pages in your everyday
browser, where you are already logged in, so adding to the cart is one click
each. The result screen creates a
new `Beatport playlist.txt` in the playlist's download folder, copies its contents,
creates a temporary Soundiiz import with Beatport already selected, and opens
the returned review page. Confirm the matches and finish the transfer there;
Soundiiz accepts up to 200 tracks per import. The saved file and clipboard remain
available as a fallback. Promo prefixes, uploader names, preview markers and
trailing label fields are removed before matching, including titles whose artist
separator has no surrounding spaces. Exact Beatport track URLs are preferred in that
file and replace stored release links so later imports can reuse them. Release links and blocked public pages
are looked up for the exact title/remix and fall back to cleaned `artist - title`, which
Soundiiz presents for review before writing the Beatport playlist. Already-exact
numeric track URLs bypass Chromium. Old `pro.beatport.com` links are automatically
rewritten and saved under `www.beatport.com`. In Beatport DJ, that playlist can then be
added to the default cart in one action.

Only canonical Bandcamp and Beatport HTTPS domains are inspected. Custom artist
domains remain outside this version. The feature depends on the stores' and
Soundiiz's public import interface; use it in line with their terms. Showing
the completed Bandcamp cart or opening its manual session needs a graphical
session; WSL users need WSLg or another working display.

For a timestamped diagnostic log that does not interfere with Textual, run:

```bash
uv run --extra play dj-digger --log-level DEBUG --log-file /tmp/dj-digger-cart.log
```

---

## 🔐 Authentication for Artist Downloads

Some artist-provided SoundCloud downloads require a logged-in account even when
the track is public. Run `dj-digger auth login`: an existing valid login or a
readable Firefox session is used first, otherwise dj-digger opens a dedicated
Playwright Chromium profile and waits up to five minutes for you to log in. Only
the verified `oauth_token` cookie is copied to dj-digger; passwords and other
cookies are not written to its credential file. If Chromium cannot start, the
command offers a hidden token-paste fallback:

```bash
dj-digger auth login
dj-digger auth login --token YOUR_SOUNDCLOUD_OAUTH_TOKEN
dj-digger auth status
dj-digger auth logout
```

The TUI opens the same choice automatically when a download needs SoundCloud.
Cancelling leaves the track untouched. A successful login updates credentials for subsequent API requests and retries
the waiting track once. Existing transfers keep their resources until they finish.

SoundCloud's private browser profile lives in
`~/.local/share/dj-digger/soundcloud-browser`; its verified API credential lives
in the owner-only `~/.config/dj-digger/auth.json` (the standard XDG environment
variables override both base directories).
If `SOUNDCLOUD_OAUTH_TOKEN` is set, it deliberately overrides that file; an
invalid value must be unset or updated before the CLI wizard can replace a login.

When Hypeddit or GateRush requires an email address, the TUI asks for a real
name and email before it submits the gate. It explains who receives those data,
rejects placeholders and malformed addresses, and retries only the downloads
that were waiting for the profile. Cancelling sends no retry request.

### Spotify steps on download gates

Hypeddit gates that show a Spotify step are handled like the other click-through
steps: the gate clears that step through Hypeddit's own Spotify app and server
session, so nothing dj-digger could do with your Spotify account would reach it.
Releases before 1.0 asked for a Spotify developer app and stored a login in
`~/.config/dj-digger/spotify.json`; that file is no longer read, and you can
delete it yourself.

When a gate ends up in the private Chromium profile (a refusal, a CAPTCHA, a
provider login), a hidden browser walks the gate's own step slides for you:
it ticks the follow and like links (closing the provider pages they open,
unread), presses the Connect of a Spotify step - whose login popup comes back
by itself once you have signed in to Spotify in that profile - fills the email
slide with the address from Settings (and the name, when the gate asks for
one), and presses Download. Only a step no program can do alone - a provider
asking you to sign in, a CAPTCHA, a missing email or name - opens a window, where the same driver keeps walking the steps before
and after the one that needs you. Sign in to Spotify there once and later
gates finish out of sight. Nothing outside the Hypeddit page is clicked.
Disable **gate social actions** in Settings to keep the program from
reporting or clicking any social step at all. Gates requiring those actions
will then remain manual.

---

## 🎧 In-Memory Streaming & Waveform Engine

- **Zero-Latency Playback**: Decodes audio chunks directly off the network socket in memory via `miniaudio`.
- **Pre-Fetching & Gapless Transitions**: While listening to a track, the next track's stream URL, waveform data, and initial MBs are buffered 20 seconds prior to track completion.
- **SoundCloud Waveform Rendering**: Renders SoundCloud's 1800-sample waveform data into a 4-row block ASCII visualizer with logarithmic loudness scaling.
- **Reactive Audio VU Metering**: Real-time RMS audio signal measurement rendered at 30 FPS.
- **Remote SSH / Headless Systems**: Automatically degrades to 4 FPS under `TEXTUAL_ANIMATIONS=none` for smooth operation over SSH connections.

---

## 🤖 Non-Interactive CLI & Automation

Add `--no-tui` to run `dj-digger` in batch mode for terminal pipelines, cron jobs, or export scripts:

```bash
# Export playlist links to CSV
dj-digger https://soundcloud.com/user/sets/playlist --no-tui -f csv -o playlist.csv

# Limit extraction to first 20 tracks and export to JSON
dj-digger https://soundcloud.com/user/likes -n 20 -f json -o likes.json

# Re-open a previously exported JSON playlist in the TUI browser
dj-digger open likes.json

# Open all Bandcamp links directly in browser from a saved playlist
dj-digger open likes.json --category bandcamp
```

---

## 🏗️ Project Architecture

```text
CLI / Textual controllers
    └── ApplicationServices (lazy composition)
        ├── Collection, download, purchase and account services
        │   └── SoundCloud, gate and store adapters
        ├── Library service → repositories → SQLite owner thread
        └── Playback service → in-memory audio engine
```

Controllers own presentation; services own completed effects and resource
lifecycles. The operation coordinator admits work and tracks cancellation until
workers finish. See [the architecture guide](docs/architecture.md) for ownership
and [the specification](PROJECT-SPECIFICATION.md) for behavioral contracts.

---

## 🧪 Testing & Quality Assurance

The codebase includes an extensive offline test suite covering unit tests, API serialization, player buffering, link parsing, cart safety, and TUI reactive widgets.

```bash
# Run the offline test suite (no network required)
uv run --frozen --extra dev pytest

# Run live integration tests (verifies SoundCloud API v2 contract stability)
uv run pytest -m live

# Open public store pages read-only; never logs in or changes a cart
uv run pytest -m shop_live
```

---

## 📄 License

Distributed under the **Apache License 2.0**. See [`LICENSE`](LICENSE) for details.

## Local music and club folders (1.1)

Open a folder with `ctrl+f`, or use the explorer below your playlists. Files are
loaded in pages of 250; `ctrl+n` moves to the next page. Opening a folder lists
names first, then reads audio tags in the background. It does not analyze audio.
Space or Enter previews a local file. FFmpeg/ffprobe must be installed and on
PATH; playback also needs the `play` extra. WAV, AIFF/AIF, FLAC/FLA, MP3, AAC,
and M4A/MP4 audio are supported, subject to successful decoder inspection.

| Key | Local library action |
| --- | --- |
| `ctrl+f` | Open a directory by path |
| `ctrl+n` | Next directory page (wrap to first) |
| `ctrl+p` | Pin the current directory |
| `ctrl+t` | Sidebar split: 50/50, 70/30, 30/70 |
| `ctrl+r` | Show playlists, explorer, or both; small terminals show one section |
| `ctrl+l` | Add selected local files to a named local playlist; same name appends |
| `j` | Estimate BPM and key for selected local files, or the visible page |
| `ctrl+k` | Edit manual BPM/key, double/halve tempo, or clear overrides |
| `ctrl+e` | Inspect and export local audio for club players |
| `ctrl+u` | Review/resume the most recent unfinished folder export |
| `i` | Import playlists created by a SoundCloud profile |

Install analysis with `pip install 'dj-digger[play,analyze]'` (or the equivalent
pipx/uv tool command). Analysis is optional and its libraries load in a separate
process only when requested. Results are estimates; ambiguous rhythm/key and
silence can return no value. Manual values take precedence, followed by current
analysis, then tags. Analysis never writes audio tags or rekordbox data.

Audio export defaults to **WAV, maximum 24-bit / 48 kHz, a NEW folder**. The
format applies to files requiring conversion. Compatible MP3/AAC and compatible
lossless files are kept rather than unnecessarily transcoded. All selected files
are copied, including unchanged files; original directories are preserved. With
no explicit selection, an open folder exports all matching files, including pages
not currently displayed. Recursive inclusion is an explicit checkbox.

The dialog shows manufacturer-documented profile compatibility and then the
compatibility of the actual planned set, including retained MP3/AAC parameters.
For example, 32 kHz MP3 can remain untouched but excludes CDJ-3000/3000X from the
actual compatible list. Unverified parameters are never shown as compatible.
This is **audio-file compatibility**, not hardware testing, a rekordbox export,
or approval of any USB filesystem. You can choose a mounted USB directory as
the destination; the app never formats a drive.

Replacement is optional and must be selected afresh for every operation. It
keeps a temporary original until the new file is verified and SQLite is updated,
then removes that original. Recovery preserves ambiguous files. Successful
replacement leaves no permanent backup. Symbolic/hard links and playing or
prefetched files cannot be replaced. Conversion never silently upsamples,
downmixes, normalizes, or converts a lossy file to improve its supposed quality.
Exceptions and omitted metadata are listed in the operation report.

Profile playlist import is separate from the existing profile-track dig. Public
playlists do not require login. Private playlists require a valid session for the
profile owner. This relies on SoundCloud's undocumented API: an incomplete or
missing-track response preserves the old playlist and is reported. No external
download gates are resolved by a mass import.

### Upgrade from the old package name

Close every running instance first. **Uninstall `dj-soundcloud-digger` before
installing `dj-digger`**: both distributions own the same module and CLI script.
Do not install them together. Choose the commands for the manager you used:

```sh
# pip, inside the same virtual environment
python -m pip uninstall dj-soundcloud-digger
python -m pip install 'dj-digger[play,analyze]'

# pipx
pipx uninstall dj-soundcloud-digger
pipx install 'dj-digger[play,analyze]'

# uv tools
uv tool uninstall dj-soundcloud-digger
uv tool install 'dj-digger[play,analyze]'
```

The `dj_digger` module, `dj-digger` command, configuration and data directories
keep their names. Schema 0/1 databases with the recognized 1.0 shape get a
verified SQLite backup including committed WAL data before migration to schema
2. Unknown databases are refused without changes. Downgrading requires consciously
restoring that backup while the app is closed; there is no automatic downgrade.
See [release procedure](docs/implementation/release-1.1.md) and
[deck rule sources](docs/implementation/deck-sources.md).
