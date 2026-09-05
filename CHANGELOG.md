# Changelog

## Unreleased

### 1.1.0 — dj-digger

- Local file explorer, paginated folder views, pinned directories and local playlists.
- Local FFmpeg playback, independent waveform caching and BPM/key analysis with manual overrides.
- Manufacturer-based deck profiles, verified complete-folder audio export and journaled replacement/recovery.
- Public/owner-private SoundCloud profile playlist import with stable provider identity and partial-response protection.
- Schema 2 migration with consistent SQLite backups and a CLI instance lock.
- Distribution/repository rename to dj-digger; module, CLI and data directories remain unchanged.

## 1.0.0

### Added

- The footer labels `b` and `shift+b` as searches in Bandcamp and Beatport,
  shows `c` beside `shift+c`, and displays shifted letter keys in lowercase.
- Download now uses `d`, `Shift+D` is labelled Download all, and adding a
  playlist moves to `a`.
- The interface and documentation now call saved collections playlists rather
  than crates. Internal data names stay unchanged for compatibility.
- Ctrl+C quits the playlist browser like `q`, from the search box too. Textual
  only showed a hint to press Ctrl+Q.
- `ctrl+x` stops whatever long job is running: a dig, a download batch, a bulk
  open, a scan or a store cart. What was already collected or downloaded is
  kept, and nothing partial is saved.
- The status bar shows the running job with a spinner, progress counts and
  `^X stop`. A refresh no longer blanks the table while it digs, and a scan
  that hits folders it cannot read says so in the error banner.
- Multi-select: `v` toggles a row, `Shift+V` extends to the cursor, `Ctrl+A`
  selects everything shown; batch download, open, cart, export, marks and
  removal then act on the selection.
- The `f`/`Shift+F` store-cycling keys are gone; the number keys, `0`, and a
  click on the legend's `…` cover it, and the footer has the room back.
- Sorting with `t` (title, time, genre, status, store, and any enabled BPM,
  key or year column) and `Shift+T` to reverse; the header shows the arrow.
- The search box matches every word in any order against artist, title,
  genre, tags and label. Escape in the box keeps the filter; on the list it
  clears the selection, then the search, then the store filters. `f`/`Shift+F`
  step from the store toggled last instead of jumping to the first.
- Tracks carry BPM, key, release year and label when SoundCloud has them;
  Settings can show them as columns, and JSON/CSV exports include them (the
  four CSV columns are appended after the original five).
- A theme choice in Settings, remembered between runs. Every colour the
  interface paints now comes from the theme (badges, marks, waveform, volume
  bar, banner, help) instead of the terminal's cyan, green and yellow.
- `Shift+U` (reset statuses) asks before it wipes the marks.
- `Shift+P` opens every exact Beatport track page shown in your regular,
  logged-in browser so each can be added to the cart with one click; the `c`
  and `Shift+C` labels now say that Beatport goes to a playlist, not a cart.
- When a Bandcamp click cannot be verified, the app saves a screenshot and a
  redacted page copy under `cart-diagnostics` in its data folder (last ten
  kept). After two unverified clicks in a batch it stops clicking, opens the
  remaining products with Buy expanded, and asks you to finish them; the
  result screen offers **Finish in browser** for anything left uncertain.
- A gate handed to the private browser is now finished out of sight: a
  hidden Chromium walks the gate's own step slides the way a fan does (the
  follow and like links, whose provider pages are closed unread; the
  Connect of a Spotify, Deezer, Apple Music or Threads step, whose login
  popup comes back by itself when the profile is signed in there; the email
  slide, filled with the address from Settings) and presses its Download.
  Only a step no program can do alone - a provider asking you to sign in, a
  CAPTCHA, an email the profile lacks - opens a window, with every such gate
  of the batch in it, and there the same driver keeps walking the steps
  before and after the one that needs you. A page without those controls is
  only watched.
- Two opt-in test suites for the Bandcamp cart: `bandcamp_dom` replays
  recorded store pages in a real headless Chromium with no network, and
  `shop_mutate` performs one add/verify/remove on a name-your-price track.

### Fixed

- Legacy `pro.beatport.com` links are canonicalized to `www.beatport.com` and
  persisted when preparing a playlist, instead of targeting a retired host.
- Soundiiz imports now derive artist/title from promo-style SoundCloud titles
  instead of sending uploader names and `PREMIERE`/label noise, including compact
  separators, preview markers, and a third dash-separated label. Featured
  performers and remixers are sent as additional catalog artists. Exact
  Beatport track results replace stored release links for reuse by later imports.
- Beatport playlist handoff now creates a real temporary Soundiiz import with
  Beatport preselected instead of opening Soundiiz's marketing/tutorial page.
- Bandcamp batches verify `already_in_cart` against the global cart and finish
  on that cart page; a seller-specific side cart can no longer suppress an
  addition or leave Chromium showing the last product with an incomplete cart.
- The footer now measures the application width on its first composition, so
  clicking or focusing the table no longer makes additional shortcuts appear.
- Footer actions are grouped as `o`/`Shift+O` and `b`/`Shift+B`; Beatport and
  unmark are visible. Settings moves to `s`, and skip moves to `k`.
- The status bar scrolls sideways like the footer instead of clipping the
  store legend (a plain mouse wheel over it scrolls it, no Shift needed), and the track counts (`tracks · got · skipped · opened`) are
  gone from it; only the running job and the view's state remain on the right.
- Bandcamp cart verification now recognises the side cart as Bandcamp renders
  it (a row with the product link and an `x` delete control) and counts its
  rows, instead of looking for a "remove" link and a menubar badge that artist
  pages do not have. The first diagnostics dump from a real batch showed both
  additions sitting in the cart unrecognised. The manual-finish step also no
  longer crashes the batch on its progress label.
- Recorded Bandcamp pages (`tests/fixtures/bandcamp/`) now back the
  `bandcamp_dom` suite, and the test run keeps its data under a temporary
  directory instead of writing diagnostics into the real data folder.
- A Hypeddit gate that refuses the first unlock is retried once the way its own
  skip buttons do (`is_skippable=1`); pages offering alternative steps get the
  cheapest one (direct download, then click-through, then email). Gate flows
  are limited to two per host instead of one at a time overall.
- A Hypeddit gate that refuses the unlock, or one whose social steps are
  disabled in Settings, now opens in the private browser instead of failing;
  the failure message keeps the HTTP reason. A batch hands at most eight such
  gates to the browser per run.
- A Bandcamp batch whose clicks all verified no longer shows "Cart automation
  failed" when the final cart window cannot be shown; the additions are kept
  and the window problem is one warning row. The cause was the hidden work
  context being relaunched as a visible one on the same profile, which raced
  Chromium's profile lock. The work stays hidden; the finished cart and the
  manual-finish pages open in a separate visible browser carrying the same
  cookies, and a still-locked profile is retried before it is reported.
- Bandcamp click verification runs its count, side-cart and reload checks on
  separate clocks inside a 45-second budget and says which stage gave up,
  instead of one 30-second timeout shorter than the checks inside it.
- Quitting no longer waits on a background thread that cannot be stopped: the
  browser closes within three seconds and the process ends with the reason in
  the log, and a second Ctrl+C during shutdown exits at once.
- Opening a link or a store search no longer freezes the interface while the
  browser is being reached; on WSL that handoff could take twenty seconds.
- The README key tables now match the playlist browser: no phantom keys,
  every bound key listed, shifted keys shown as `Shift+…` in the footer and
  help, and the bulk-open confirmation names the right key.
- The browser gate driver looked for step buttons the desktop gate does not
  show - its steps are slides revealed by the sidebar's Download - so a gate
  handed to the browser had to be clicked through by hand, including the
  steps around a Spotify login. The driver now follows the slides.
- A download saved by a provider popup was never credited to the tab that
  opened it, and a popup was never recognised as one, because Playwright's
  `opener` is a method and the code read it as an attribute.
- A browser batch of gates no longer gives up on a still-open tab when one of
  the other rows was refused up front, and a batch that finds the private
  profile busy reports those refusals alongside the lock error instead of
  dropping them.
- A SoundCloud track URL with query parameters other than `in=` is no longer
  rewritten with a doubled `??`.
- A gate whose email slide also asks for a name no longer stalls the hidden
  browser on "Please enter your name.": the name from Settings is filled in
  with the address, and a profile still carrying the placeholder name sends
  the row to the window with that reason.
- The hidden browser no longer hands a gate to a window merely because
  Hypeddit sent the first visit after a download to its hot-or-not poll: the
  tab is pointed at the gate again and walked out of sight, so the window
  with its flashing provider popups only appears for a login or a CAPTCHA.
- In a hidden batch every gate after the first waited five minutes for a
  file that never came: Hypeddit refuses a download while the
  `filedownloading` cookie of the previous one is still set, and the next
  tab pressed its Download seconds later. The cookie is cleared before each
  Download now.

### Changed

- Textual is now pinned to the 8.x line; the playlist browser depends on its
  binding semantics and a few private hooks, so a major upgrade needs review.
- Track statuses are mirrored in memory after the first read, the playlist
  sidebar lists headers and reads a playlist's tracks only when it is opened,
  and single-row changes (a mark, an opened link, the playing marker, download
  progress) repaint that row instead of rebuilding the table. A large library
  starts, refreshes and searches without stutter.
- The local scan writes in batches, follows symlinked folders as before, and
  reports folders it could not enter instead of skipping them silently.
- The managed Chromium lifecycle (profile, display check, launch errors,
  installer) lives in one module, `browser_session.py`, shared by the cart,
  the gate fallback and the SoundCloud login instead of two copies.
- `cart.py` is split: its errors and dataclasses, store URL rules, product
  matching, page parsing and Beatport playlist helpers each live in their own
  module, re-exported from `cart` so nothing importing it changes.
- A repo-wide cleanup removed about a thousand lines without changing what
  the app does: dead helpers and never-read fields (`db.get_track_status`,
  `library.all_crates`, sync locator helpers and an unread session state in
  `cart.py`, `Palette.panel`, the always-`None` `direct_url`), duplicated
  code folded into one place (the redirect walker, the Chrome request
  headers, the browser profile directory, the Bandcamp Buy-dialog sequence,
  the cart "cancelled" and preflight-failure results, the store URL parser,
  the crate builders in the tests) and the longest functions split
  (`_preflight`, `_execute_store`, `run_batch`, the Hypeddit inspection and
  browser batch, `download_track`, the CLI login).
- A single Hypeddit browser download is now a batch of one: its five-minute
  limit is one deadline, counted once the automatic steps are done, instead
  of one per popup plus one for the download, and only tabs its own page
  opened are watched. A hidden pass always has that limit; a window without
  one stays as long as a tab stays open.
- The browser batch toast reads "Finishing N Hypeddit gates in the hidden
  browser; a window opens only for a step that needs you" instead of asking
  you to complete the tabs and close Chromium.
- The local file cache keeps only the path, modification time and normalised
  stem; the unread size, artist and title columns are dropped, so an
  existing cache is rebuilt by the next scan.
- The redirect and browser-download refusals read "Redirected to an unsafe
  address" and "That link returned a web page rather than an audio file".
- A SoundCloud login that completes while a download still holds the old
  client no longer queues a hidden client swap: it says "Signed in to
  SoundCloud, but a download is still running on the old login: let it finish
  or stop it with ctrl+x, then press w again" and leaves the retry to you.
- The dig reports only through its job: when another job starts during a
  dig, the status line and spinner follow the newer job.
- The "more than 20 tabs, press again" confirmation is one key: pressing
  `Shift+O`, then `Shift+P`, then `Shift+O` again asks again instead of
  opening.
- Escape on the confirm, manual-cart and gate-profile dialogs answers "no"
  through one shared action; the footer keys are unchanged.

### Removed

- The Spotify integration (`dj-digger auth spotify`, the PKCE login, and the
  library writes for Hypeddit `sp` steps). Hypeddit clears its Spotify step
  through its own OAuth app and server session, so a user's own Spotify login
  could never satisfy the gate; the step is now reported like the other
  click-throughs. A saved `spotify.json` is no longer read and may be deleted.
- The unused synchronous cart stack in `cart.py` (about a thousand lines with
  no caller since the asynchronous session took over) and its tests.
- The unreachable Beatport cart code (cart page, add-to-cart control, cart
  membership check, login). Beatport items have been playlist entries since
  0.15.0; the code that would have clicked a Beatport cart never ran.

## 0.15.0

### Added

- Beatport results can now be saved as a non-overwriting plain-text playlist,
  copied for a Soundiiz plain-text import, and handed to Beatport's official
  Soundiiz transfer page. Exact track URLs are preferred; release-page and
  blocked lookups fall back to artist/title matching.
- Explicitly selecting both Bandcamp and Beatport now sends each eligible track
  to both workflows instead of treating Beatport only as Bandcamp's fallback.
- Store-cart batches now have live progress, an editable review screen, grouped
  results, safe retry for failures known to precede a click, and Settings actions
  for checking, opening, or explicitly resetting the dedicated store session.
- Cart diagnostics now record lifecycle phases, redacted navigation/status,
  per-track outcomes, browser HTTP/console signals, and aggregate counts in
  `--log-file` without recording URL queries, credentials, or raw console text.
- Bandcamp moved-link recovery now uses its visible autocomplete, accepts only an
  exact canonical track, and inspects at most three returned album pages without
  entering the CAPTCHA-protected full search page.

### Fixed

- The purchase-review table now shrinks in short terminals instead of pushing
  its Continue button off-screen. A visible instruction and `Enter` shortcut
  make the intentional review pause distinct from a stalled batch.
- Natural decoder EOF no longer escapes through miniaudio's CFFI callback as
  `RuntimeError: generator raised StopIteration`. Playback events are tagged by
  generation, so a late callback after seek or stop cannot advance the wrong row.
- Bandcamp product discovery now tolerates its current split between public DOM,
  `TralbumData`, and structured metadata. It also dismisses the necessary-cookie
  footer, accepts an exact unique trailing title across artist aliases, and does
  not require an account login for a cookie-backed cart.
- Bandcamp storefront homepages and undeclared name-your-price values no longer
  masquerade as global structure failures and stop the remaining queue. Current
  DOM identity/price now wins over stale `?action=download` metadata, safe plain
  HTTP store links are upgraded after domain validation, and common `//`, quoted
  premiere, and `feat.` title forms can be matched exactly.
- Beatport's production verification no longer loops in managed Chromium or
  aborts an unrelated Bandcamp batch when its server rejects Playwright.
- Bandcamp cart verification now checks the post-click item count, opens the real
  side-cart control instead of the SVG sprite, inspects its current rows, and
  performs one final reload check. A still-unverified page stays open instead of
  disappearing, and no second add click is issued.
- Bandcamp cart membership now requires a visible removable row with the exact
  canonical track, preventing hidden page data from marking the rest of a batch
  as already added. Flexible prices default to the verified minimum, remain
  editable with `E`, and fail before mutation if Bandcamp withdraws its price
  input.
- Bandcamp storefront recovery now reaches the global autocomplete instead of
  reopening the same label homepage. Beatport release rows can use their track
  slug to preserve an exact mix or remix before the target page is revalidated.

### Changed

- Whole-list opening now follows the existing Shift convention: `o` opens the
  highlighted track and `Shift+O` opens all visible tracks. The local-file marker
  is a monochrome `▣` at the start of the first marker column, and the collapsed
  error banner occupies the full first row above the TUI.
- Store work uses one persistent Chromium profile: preflight and Bandcamp
  mutation stay headless with two bounded pages, then the same profile is
  relaunched visibly only for the final Bandcamp cart.
- Beatport public metadata is preflighted only to improve playlist accuracy.
  Managed login and cart mutation are no longer attempted; Settings now exposes
  only the Bandcamp store session. Exact numeric Beatport track URLs go straight
  to the transfer playlist without starting Chromium.
- Product-checking progress now says that nothing is added until review, and an
  album-only Bandcamp track is reported explicitly rather than substituting a
  full album purchase.

## 0.14.0

### Added

- The player bar now carries a one-row control strip under the waveform:
  previous, play/pause and next buttons, the track title, the clock, a volume
  slider you can click or drag, and a close button. `ctrl+w` closes it from the
  keyboard. Play/pause there acts on the track that is playing rather than on
  the row the cursor sits on.
- `--log-file PATH` writes the log to a file, with timestamps, instead of to the
  terminal - and the crate browser no longer silences the log when it is given.
  Textual draws the interface on standard error, so a log line under the browser
  landed in the middle of the track list and redirecting the shell's stderr to
  catch it took the interface with it. There was no way to keep a log of a TUI
  session before this.

### Fixed

- Any exception out of a player operation - not just the ones the player has
  names for - is now shown as a player message instead of escaping through
  Textual's message pump and taking the TUI down.
- A TUI crash is written to `--log-file` with its traceback before the screen
  is torn down, and the log captures native (segfault-level) crashes too via
  `faulthandler`. Previously a crash could erase its own report along with the
  alternate screen, leaving the log ending mid-sentence.
- A backend that refuses to start or stop the audio device no longer crashes the
  app. Pressing play twice in quick succession was enough: miniaudio raises its
  own numbered error out of `device.start`, and nothing on the interface side
  caught it, so it came out through Textual's message pump. It is now reported
  the same way a missing audio device already was, and the device is rebuilt on
  the next attempt.
- Starting a track no longer opens its stream from the interface thread. The
  connect waits up to thirty seconds for a slow CDN, and the whole TUI was
  frozen behind it; it now happens on the worker that already fetches the rest
  of what the track needs.

### Changed

- The error banner opens collapsed to one summary line with the error count.
  Click it to read the messages, click it again to fold them away. It used to
  arrive with every message on screen, taking half the terminal over the list
  you were reading. Its close control is now a three-column `✕` rather than a
  yellow block, the bar stops at 88 columns instead of spanning the terminal,
  and the message list has a thin scrollbar in the banner's own colours.
- The transport buttons are bold text glyphs (`◀◀`, `▶`/`❚❚`, `▶▶`, `✕`) on
  three-row translucent chips - no emoji, which every terminal renders in its
  own colour and size, and no theme-coloured background. A terminal cell cannot
  be scaled, so chip size and weight are what make them read as controls; the
  volume slider's speaker went the same way (`♪` / `Ø`).
- The playhead pulse is two columns in shades of cyan instead of a twelve-column
  band reaching bold white, which read as the tail of the waveform flickering
  thirty times a second.
- The player bar is a row shorter overall and its waveform is twice as tall:
  the title, the clock and the play/pause glyph moved down into the control
  strip, and the four rows that frees go to the picture.
- The crate sidebar heading is centred, with a blank row between it and the
  first crate name.

## 0.13.3

### Changed

- Large internal cleanup after a full audit: dead code removed, duplicated
  logic consolidated, hand-rolled helpers replaced with standard-library
  equivalents, the seven largest functions split, and undocumented constants
  annotated. No user-facing behaviour changed, with the one exception below.
- `hypd.it` gate links now share the same Chromium fallback route as
  `hypeddit.com` ones when automatic resolution needs a browser step.

### Fixed

- Bandcamp and Beatport batches now keep one Chromium session from manual login
  through preflight, confirmation and cart addition. All candidate tabs are
  created up front and reused, so the browser no longer closes between phases or
  replaces the whole batch in one short-lived tab.

### Removed

- The one-time import of pre-0.9 data files (`state.json`, `crates/*.json`)
  no longer runs, and a crates table written by 0.8 or earlier is dropped and
  recreated in the new shape. Upgrading from 0.8 or earlier directly to this
  version therefore starts with an empty library; go through any 0.9–0.13
  release first if you need those crates and statuses carried over.

## 0.13.2

### Added

- The track context menu can copy a matched file from another library folder,
  such as `Music`, into the current playlist's download folder. The copy runs
  off the UI thread, preserves the source, avoids overwrites and is finalized
  atomically.

### Fixed

- The local-file cache now removes deleted paths and validates every cached
  match before exposing it, so missing files no longer retain a folder badge or
  a copyable dead path.
- Automatic `got` statuses now remember the exact backing file. Deleting that
  file clears only the status that depended on it, while a manually assigned
  `got` remains an independent ownership decision.
- Legacy stale cache matches from earlier releases are reconciled during the
  next scan, including playlists removed and imported again.

## 0.13.1

### Fixed

- Batch downloads no longer wait for every Hypeddit preflight before starting.
  Each worker checks its own link and immediately continues with that track,
  while all queued rows show progress as soon as the batch begins.
- A single left click now only selects a track. Opening remains available via
  `o`, `Enter` or a double click.
- Confident local-file matches automatically mark tracks as `got`, including
  stale `opened` or `skipped` statuses, and `w`/`W` avoid downloading verified
  files that are already on disk.
- Downloads are stored below the configured root in a safely named playlist
  folder instead of placing every file directly in the root Downloads folder.

## 0.13.0

### Changed

- Hypeddit browser fallbacks now share one private Chromium context and open
  every required gate in its own tab. The batch remains open for manual work,
  can be stopped with `Ctrl+X`, and only marks tracks as got after a validated
  atomic file save.
- Spotify ART and PLAY gate actions now use the post-February-2026
  `PUT /me/library` API with validated Spotify URIs. Three-part PLAY values are
  supported, missing scopes require reauthorization, and each URI gets one
  non-retried mutating request.

### Fixed

- Hypeddit gate pages no longer mistake audio URLs from Hot-or-Not
  recommendations, advertisements, widgets or SoundCloud previews for the
  current track. Active gates obtain their file only from `/gate/download/ul`.
- Dormant CAPTCHA assets no longer turn smartlinks into challenges. Visible
  challenges and explicit flow responses remain typed manual CAPTCHA outcomes
  and are never bypassed.
- Hypeddit hubs are expanded from purchase fields, extra links and descriptions,
  including persisted crates immediately before download. Pure wrappers are
  replaced with purchase stores, nested gates or `no-link` as appropriate.
- Gate failures retain authentication, CAPTCHA, manual, protocol, rejection and
  download types through the batch summary. All errors are shown, and bracketed
  Textual notifications render literally instead of raising `MarkupError`.

### Security

- SoundCloud, Instagram, YouTube and other click-through gate steps are recorded
  only in `skip_gate_steps[]`; no external social link is opened and no
  click-through provider follow, like, repost, comment or CAPTCHA bypass is
  attempted. Spotify ART/PLAY remains the explicit OAuth-backed exception above.
- SSRF and redirect checks, download size limits, HTML sniffing, owner-only
  temporary files, atomic finalization, telemetry best-effort limits and the ban
  on retrying mutating Hypeddit or Spotify requests remain in force.

## 0.12.1

### Fixed

- Restored the legacy `shop` extra as an empty compatibility alias. Playwright
  remains installed as a core dependency, while existing `pipx` and `uv tool`
  update commands using `dj-soundcloud-digger[shop]` no longer emit a warning.

## 0.12.0

### Added

- **Current Hypeddit desktop-gate support.** One parser now distinguishes gates,
  smartlinks, hybrid pages, nested gates, direct files, expired pages and
  challenges. SoundCloud and other link-only steps are completed without
  mutating provider APIs; Spotify artist and public-playlist steps use their
  documented OAuth endpoints.
- **Safe Chromium fallback.** CAPTCHA, provider OAuth and unknown future steps
  can continue in the existing private Playwright profile. Interactive and
  batch downloads share one profile lock and never copy API tokens into pages.
- **Guided SoundCloud login.** CLI and TUI flows can open the private Chromium
  profile, verify its OAuth cookie and retry only downloads waiting for login.
- **Read-only Hypeddit contract suite.** An opt-in live test classifies all 60
  reported pages using GET requests only, with no profile, OAuth or download
  submission.
- **Track context menu.** Right-clicking a row opens its Open, Copy path, Got,
  Skip, Clear and Remove actions instead of opening the link immediately.

### Security

- Gate and download URLs reject credentials, lookalike hosts, unsafe schemes
  and private or link-local literal addresses. Every page and file redirect is
  checked before the next request and capped at five hops.
- Downloads use owner-only temporary files, enforce the byte limit while
  streaming, sniff HTML masquerading as audio, serialize final-name selection
  and atomically replace only fully validated files.
- CSRF values, OAuth state, email, comments, query strings and signed download
  URLs are excluded from diagnostics. Hypeddit telemetry is best-effort and
  mutating POSTs are never retried.

### Fixed

- Hypeddit no longer calls obsolete mobile endpoints or performs SoundCloud
  follow, like, repost or comment actions that the current desktop gate does not
  verify. Declared link steps are represented exactly in `skip_gate_steps[]`.
- Hybrid smartlinks retain their downloadable gate while exposing shop links;
  wrappers such as `link/ky9i8z` become the nested gate plus Beatport and
  Bandcamp destinations.
- Batch downloads serialize gate resolution per provider, stream up to four
  validated files concurrently, keep failed tracks new and summarize failures
  as authentication, CAPTCHA, manual, protocol or rejection errors.
- Email and comment data are submitted only when the manifest requires them;
  lifetime-fan options default to opt-out.

## 0.10.0

### Added

- **Spotify-backed Hypeddit gates.** `dj-digger auth spotify login` uses
  Authorization Code with PKCE, stores refresh credentials privately and can
  complete the artist action declared by a gate through Spotify's current
  library endpoint. Status and logout commands are included.
- **Chromium installation on demand.** Store-cart support is installed by
  default. When Playwright's matching browser build is absent, the TUI asks
  before downloading it in the background and resumes the original preflight.

### Security

- Spotify login binds its temporary callback to `127.0.0.1`, validates OAuth
  state, requests only `user-follow-modify`, refreshes tokens without a client
  secret and writes credentials atomically with owner-only permissions.
- Hypeddit no longer submits the reserved placeholder email or claims unknown
  Spotify actions. Disabling gate social actions prevents Spotify mutations.

### Fixed

- The TUI error banner keeps bracketed messages literal, has a visible close
  button and is no longer corrupted by application logs writing behind it.
- Single and batch downloads stay at `0%` while a link or gate is being
  resolved instead of reporting fictional progress.
- SoundCloud artist downloads without a concrete URL use the authenticated
  download endpoint and distinguish missing, rejected and failed credentials.
- Hypeddit step and download failures now produce actionable errors. Smart-link
  pages such as `l87679` keep their Beatport destination without retaining a
  false gate link.
- Store carts distinguish a missing Chromium build from missing Linux system
  libraries, stop the installer process tree on cancellation and recognise an
  existing Bandcamp session after its login redirect.

## 0.9.1

### Fixed

- Download progress, completion and failure now repaint only the affected track
  rows, so the list can be freely scrolled during single and batch downloads.
- Necessary table rebuilds preserve the selected track and the top visible
  track, including back-to-back completions when handled tracks are hidden.
- Throttled batch progress repaints every track waiting for an update instead of
  letting a busy download starve the other progress indicators.

## 0.9.0

### Security

- **Store links are verified before cart automation.** The optional Bandcamp and
  Beatport flow accepts only canonical HTTPS hosts, rejects credentials and
  custom ports, rechecks redirects, and stops on ambiguous products, changed
  prices or changed product IDs. Login and checkout remain manual.
- **A link could run a command under WSL.** Handing a URL to the Windows browser
  went through `powershell.exe -Command Start-Process <url>`, and everything
  after `-Command` is parsed by PowerShell as code rather than taken as an
  argument — `shell=False` does not help when the interpreter *is* PowerShell.
  A `purchase_url` is set by whoever uploaded the track, and one containing `;`
  or `$(...)` is a perfectly valid URL. The address now travels in an
  environment variable, which PowerShell reads and never re-parses.
- **A dig no longer reaches into your own network.** Link hubs and gates are
  fetched from addresses that come out of a track's purchase link, with no check
  on where they point, so one aimed at `127.0.0.1`, at a box on your LAN or at a
  cloud metadata service made your machine issue those requests. Loopback,
  link-local, private and reserved addresses are refused before anything is
  sent. Opening such a link by hand still works — that is your decision to make.
- **A download can no longer fill the disk.** The write loop ran until the server
  stopped sending; `Content-Length` was read but only ever fed the progress bar.
  There is a 2 GB ceiling now, applied to the declared length as well.
- **A lookalike host no longer receives our client_id.** The check was
  `"soundcloud.com" in host`, which is also true of
  `evil-soundcloud.com.attacker.example`.
- Scheme checks that used `startswith("http")` — which accepts `httpfoo://` —
  now parse the URL.

### Breaking

- **Crates and track statuses live in SQLite only.** They used to be written to
  `crates/<slug>.json` *and* the database, with the file treated as the real copy
  and the table as a fallback that had room for five of a record's fields — so a
  crate that fell back to it silently lost its import date, its `partial` flag
  and its `NEW` marks. There is one copy now. Existing `state.json` and
  `crates/*.json` are imported once, on first start after the upgrade, and then
  left on disk untouched; nothing reads or writes them again. Downgrading to 0.8
  after that point loses anything changed in between.

### Added

- **Exact-track Bandcamp and Beatport carts.** `c` preflights one track and `C`
  shows a batch confirmation with prices and per-item outcomes. A dedicated
  persistent Chromium profile keeps the cart session without exposing or
  reusing the user's normal browser profile. Install the optional `shop` extra.
- **A switch for what gates do with your account.** Every version up to 0.8 sent
  `is_repost` and `is_subscribe` to Hypeddit and a comment to GateRush, hard-coded
  and visible in no interface. It is a checkbox on the Settings screen now — the
  same screen a first run opens on — and it is on by default, so nothing changes
  unless you turn it off.
- **A download folder setting.** It was `~/Downloads`, written into the download
  code in two places.
- **Tests run on every push and pull request**, across Ubuntu, macOS and Windows.
  They only ran on a release or a tag before, which is the point at which it is
  too late for them to tell you anything.
- **A weekly job hits the real api-v2**, so a change on SoundCloud's side shows
  up as a red build rather than as a bug report.
- Ruff is a declared dev dependency, configured, and enforced in CI.
- `py.typed`, so the annotations reach anyone importing `dj_digger`.

### Fixed

- **Deleting a crate did nothing** unless it happened to still have a JSON file:
  the whole operation sat inside `if the file exists`, so a crate whose row
  outlived its file could not be removed at all. The confirmation appeared, the
  crate stayed, and it came back on every reload.
- **Link hubs that mention "download" anywhere are no longer mistaken for gates.**
  The check matched the word across the entire page, so a shop with it in a
  footer, a FAQ or an analytics script was left as a `gate` and never expanded.
  It now reads the text of the thing you would press, in eight languages — a
  German or Spanish gate used to be invisible to it. On a 484-track playlist this
  turned up 46 shop links that were previously never followed.
- **A batch download no longer shares one session between its four threads.** Gate
  flows are multi-step and held together by their own cookies, so four of them in
  one cookie jar overwrote each other's state. `dig` already got this right; the
  download path never had the fix.
- **A dead host costs seconds, not minutes.** Third-party pages were fetched with
  the retry budget meant for api-v2 — five connect retries against a 20 second
  timeout — and a playlist names the same dead smart-link domain over and over. A
  host that stops answering is now skipped for the rest of the dig. The dig this
  was measured on went from minutes to about a minute.
- **The log is ours again.** `logging.basicConfig` configured the root logger, so
  urllib3's retry warnings came out with our own output: dozens of
  `Retrying (Retry(total=1...))` lines before a single result. `--log-level DEBUG`
  still shows everything.
- SQLite connections are no longer leaked. A fresh `Database` was built on every
  call — three times inside `list_crates` alone — each opening its own connection,
  re-running every `CREATE TABLE`, and closing nothing.
- A gate that answers with its own web page is no longer saved as an `.mp3` that
  no player can open.
- A track called `Aux` or `Con` no longer fails to save on Windows.
- Scanning browser cookies no longer pretends to support Chromium. The query read
  a column that is always empty there, because the value is encrypted behind the
  system keyring — `dj-digger auth` says so now instead of reporting nothing found.

### Interface

- The help screen no longer wraps its own descriptions back to column 0, leaving
  words hanging underneath as if they were key names.
- The sidebar folds itself away below 110 columns, where it was costing the track
  title, the genre and the time column.
- The footer drops its least important bindings rather than cutting the last one
  mid-word. `q Quit` is visible for the first time.
- Settings scrolls, and its Save button is reachable on an 80×24 terminal. It was
  off the bottom of the screen — on the one screen a first run opens on.
- Store badges are elided rather than clipped, so `gate(hypeddit)` no longer
  arrives as `gate(hypedd`.
- The store counts in the status bar follow the search instead of staying at the
  crate's totals.
- The search box is one line rather than three, and says how to leave it.
- The terminal title says `dj-digger`, not `DiggerApp`.

## 0.8.0

### Breaking

- **`--browser` is gone.** Deprecated in 0.6.0, carried through 0.7.0 with a
  warning, removed here. Passing it is now an argument error rather than a
  warning, so a script still using it fails loudly instead of quietly opening
  the wrong browser. The browser is a setting: press `S` in the crate browser
  and pick from what this machine actually has. `--no-tui --category` reads the
  same setting, so batch opening and the interactive path no longer disagree
  about which browser you meant.

## 0.7.0

### Added

- **Settings open on the first launch.** A fresh install had a profile nobody
  had ever seen: gates were handed the placeholder name and address, and the
  library scan walked whatever `~/Music` happened to contain. With no config
  file on disk the crate browser now opens Settings first, and the scan waits
  for the answer. Scan folders are editable there too, which was the one
  setting the screen could not reach.
- **A refresh says what it brought in.** Tracks that were not in the crate
  before are marked `NEW` and sorted to the top; the playlist's own order is
  kept inside each half. A refresh that turns up nothing leaves the previous
  batch marked, so pressing `r` twice does not lose it.
- **Link hubs are opened and replaced by the shops behind them.** Plenty of
  purchase links hand over no file at all - ampsuite release pages, and gates
  running in smart-link mode - they are just a list of streaming services and
  shops. Those pages are now read during a dig, the Bandcamp and Beatport links
  behind them (including the ones wrapped in the hub's own redirect) are added
  to the track, and the hub link itself is dropped so the track is not badged
  as a gate that gates nothing. A page that does offer a download is left
  exactly as it was.

## 0.6.0

### Breaking

- **Python 3.12 or newer is required.** 3.9 reached end of life in October 2025
  and 3.10 does so in October 2026. The test suite now runs against 3.12, 3.13
  and 3.14 in CI, so the claim is checked rather than asserted.
- **The YAML export format is gone**, along with the `[yaml]` extra and the
  `pyyaml` dependency. Use `-f json` or `-f csv`. Reading a `.yaml` summary
  written by an earlier version reports what happened instead of failing to
  parse.
- **`--browser` is deprecated and will be removed in 0.7.** It still works this
  release and prints a warning. The browser is a setting now: press `S` in the
  crate browser and pick from what the machine actually has.

### Security

- **Links are checked before they are opened.** A `purchase_url` is whatever the
  artist typed into SoundCloud, and a summary file is whatever was on disk;
  neither was validated, so `file://`, `javascript:`, `data:` and `\\host\share`
  reached the browser layer verbatim. Only `http` and `https` with a host are
  opened now, checked in `store_for_url`, in `categorise`, in `load_summary`,
  and again at the point of opening.
- **The OAuth token is never world-readable.** `auth.json` was created with the
  umask default and narrowed with a `chmod` afterwards, leaving a window in
  which any other account could read it. It is now created at 0600 and moved
  into place atomically, in a directory tightened to 0700.
- **A browser named in the config file is no longer executed unchecked.**
  `webbrowser.get()` accepts a command line as well as a browser name, so a
  stored preference is matched against the machine's own list first.
- **The default gate identity is unroutable.** The old default email was a real
  address at a real provider, submitted automatically to third-party download
  gates - so every unconfigured install was signing a stranger up for artist
  mailing lists. It is now a reserved `.invalid` address, the old one is dropped
  on load, and the resolvers warn before submitting a placeholder.
- **CI runs the tests before publishing** and pins its third-party actions to
  commits rather than movable tags.

### Fixed

- **Clearing a status no longer comes back.** The legacy JSON import ran on
  every `Database()` construction, and the crate library builds one per call, so
  a status you had just cleared was re-read from the state.json mirror and put
  back. It now runs once per database per process.
- **`dj-digger --version` reports the real version.** It said 0.4.20 while the
  package was 0.5.1; the number is now read from the installed distribution.
- **The crate browser lets go of everything on the way out.** `DiggerApp`
  defined `on_unmount` twice and Python kept the second, so the 30fps ticker
  went on running after the app closed.
- **Discovery failure is reported.** Three invented `client_id` fallbacks were
  tried and cached when discovery failed - one of them 28 characters long and
  silently skipped - which poisoned the cache for every later run.

### Added

- **The local library scanner runs.** Shipped in 0.5.0 but wired only to a
  package that never executed, it now walks `scan_directories` in the background
  on startup, marks matched tracks with `📁`, and copies the path with `y`.
  A track is only marked as *got* automatically when artist and title both
  match and it is still unmarked - a filename never overrules a decision you
  made by hand.
- **Browser detection**, including WSL: links can be handed to the Windows
  browser through `wslview`, `explorer.exe` or PowerShell.

### Internal

- `dj_digger/ui/`, 633 lines of a second Textual app that nothing imported and
  that could never have run, is gone.
- `tui.py` was 2034 lines in one module and one 114-method class. It is now a
  package of thirteen files whose largest is 318 lines, with a test asserting
  that no two parts define the same method.
- Annotations use `X | None`, `list[...]` and `typing.Self` throughout; the 29
  `from __future__ import annotations` imports are gone.
