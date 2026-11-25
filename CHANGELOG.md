# Changelog

Every published change is recorded here. Versions use `MAJOR.MINOR.PATCH`;
private and public releases share the same version, with separate Git histories.

## [Unreleased]

### Trading performance

- Add a shareable read-only calendar with weekly and monthly P&L, day details,
  three intensity levels and optional weekly and monthly Telegram documents.
- Add an opt-in daily profit threshold that blocks copied entries until the next
  Tashkent day and requests closure of verified strategy exposure at the threshold.
  Allow scheduling its first day and keep the threshold separate from the operator's trading switch.
- Add optional dynamic gold sizing from the remaining daily profit goal, verified
  channel protection, broker lot increments, margin and a minimum 1:1.5 risk/reward ratio.
- After hitting the daily goal, continue classifying channel posts and track protected
  simulated gold positions in a separate journal using broker bid and ask history.
- Include estimated simulation P&L in private performance reports and test TP,
  SL, and the absence of broker writes after the daily goal with the fake broker.
- Preserve the effective performance and daily-target settings in owner snapshots.
- Run daily performance, signal closure and the read-only MT5 quote RPC tests in CI.
- Include entry fees and commissions when reporting the realized result of a
  position closed by the app.

### Gold-channel signal corrections

- Distinguish a request to close at break-even from a request to move SL or a
  retrospective comment, and verify a close target across a deleted repost.
- Instruct the channel reviewer to retain evidence distinguishing break-even
  closes, stop moves and commentary, together with source and reply links.
- A deleted gold-channel post no longer disables trading globally: the deleted
  post stays blocked, existing positions stay open, and the owner is notified.
- Distinguish a verified fill at a changed price from an unknown broker result;
  keep later legs unsent after a large deviation and report confirmed partial entries.
- Recognize compact add-on posts such as `75dalivka 78 Sl` and use the verified
  channel stop for that add-on instead of replacing it with the fallback stop.
- Let the existing AI classification inspect recent channel context and identify
  an earlier signal's targets when a new entry refers to the same trade idea;
  verify the source and prices independently before sending broker orders.
- Resolve explicitly referenced earlier channel take-profits for a later gold
  entry, and revalidate the source and prices before each order leg.
- Recheck fresh edited posts that were previously unrecognized with the AI and
  alert the operator when the edit may be a signal; this review never places an order.
- Treat an abbreviated entry zone and its provably equivalent full-price edit
  as the same signal, retaining later SL and TP updates without disarming.
- Reclassify a verified corrected entry after a prior revision was refused for
  changing direction or zone; never replay a signal with a recorded trade.
- Preserve existing positions on a material edit and report the change without
  globally disabling trading. Test the reported edit and direction-change races.

### Small-account gold signals

- Add an opt-in small-account mode for one 0.01-lot gold entry, the channel's
  first target or a fallback 1R target, and a seven-dollar fallback stop.
- Limit linked add-ons to one 0.01-lot position per signal, independently
  protected from its own fill, while preserving the existing two-order mode.
- Refuse broker writes when an explicitly configured account binding cannot
  be matched to the connected MT5 account.
- Verify fills, channel-level updates, repeated add-ons and existing trailing
  behavior with the fake broker.

### Independent signal workers

- Classify each new entry once per worker instead of asking the same model
  three times, leaving the other workers free for unrelated messages.
- Keep broker-write serialization, signal-chain guards and refusal on an
  uncertain or unavailable gold-signal classifier.

### Telegram delivery diagnostics

- Record bounded, content-free transport and active-channel difference timing
  alongside handler and ingest timing to investigate delayed channel updates.

### Explicit gold re-entry

- Recognize an explicit zone, direction and re-entry as a new signal, while
  keeping ambiguous re-entry and management messages out of automatic entry.

### Owner AI connectivity

- Document that the owner Codex service must be rebound to the current app
  network after app recreation and verified with an authenticated inference.

### Linked gold add-ons

- Take the add-on’s first target from the linked original channel signal, even if
  its orders never opened; otherwise use the add-on’s own 1R target.

- Recognize an explicit price-and-dalivka reply only when the source policy
  opts in; resolve the abbreviated price against the fresh broker quote and use the
  configured quote tolerance and bounded waiting window.
- Open one independent 0.10-lot position with its own $15 price-distance stop
  from its actual fill, a usable channel TP or a 1R fallback. Original positions
  may be open, closed or absent; the linked source must verify direction.
- Isolate add-ons from the original signal's updates and closures, and persist
  a one-shot claim before sending to prevent replay. Keep source, price, age,
  broker, margin and arm checks.
- Exercise BUY/SELL, duplicate posts, closed or missing originals, independent
  management, disarmed trading and lost broker responses in simulation.

### Gold signal level fallback

- Keep a verified gold entry eligible when channel TP or SL values are incomplete
  or invalid, using the configured protective stop and 1R/runner targets where
  needed, while reporting ignored levels.
- Use the first two valid channel targets for two positions, retain a single
  valid first target, and apply later matching updates without duplicate entry.
- Preserve price, freshness, direction, position, broker and reached-target
  checks, with simulated-broker coverage for partial levels and edits.

### Private recovery snapshot

- Preserve the active channel brief and the owner's configured Codex executable
  and login material in the private recovery archive; omit transient caches.
- Start the saved Codex service on restore when its CPU architecture matches,
  and refuse an automatic start with an incompatible executable.

### Channel stop prices

- Recognize explicit channel stop prices in gold entries and edits, apply them
  to matching positions, and keep a stronger break-even or trailing stop.
- Convert gold pip-based stops at 0.10 price units per pip from each actual
  fill, and use the configured fallback only when no channel stop exists.
- Recalculate an automatic 1R target after the channel changes the stop; do
  not replace an explicit channel target or loosen a protected stop.
- Accept the channel's observed distant full-price gold target while retaining
  strict direction, ordering and abbreviated-price checks.
- Cover channel edits, per-position fills, simulated slippage and 1R updates
  with broker-simulation tests; no live test orders are sent.

### Gold signals without targets

- Keep automatic trading enabled when an ambiguous channel instruction or an
  unverifiable channel update needs review, while reporting the issue.
- For verified gold entries without targets, put the first position at 1R and
  leave the second without a take-profit for break-even and profit trailing.

### Uzbek channel re-entry announcement

- Recognize the observed stop-hit/future-re-entry announcement as information,
  leaving trading armed for the next separately verified signal.

### Automatic Telegram reactions

- Add an opt-in Telegram setting to react to new active-channel posts with a
  random available free reaction, using the connected Telegram user session.
- Run reactions independently of signal processing, respect existing manual
  reactions and Telegram flood waits, and deduplicate across edits and restarts.

### Gold profit trailing

- Add an opt-in settings control for tracked classic two-order signals across
  instruments: after a verified TP1 and confirmed break-even stop, remove TP2
  and tighten the stop in configurable price steps without loosening it.
- Default the global control to off, explain instrument price units and the
  effect of disabling it, and retain previously enrolled owner gold positions
  when moving from the legacy source-specific setting.
- Check enrolled positions every five seconds and use the last confirmed stop
  as the restart-safe trailing anchor; leave old trades and other sources alone.
- Rebuild a waiting gold entry when its targets change during pre-order checks;
  never retry after a broker order was attempted.

### Missing original signal

- Treat a fresh first-observed edit as a potential new entry when no prior
  message, trade, broker position or pending order exists; still require the
  configured AI, source, time, price, stop and broker checks.
- Check the complete signal trade history to prevent re-entry after a close.

### Signal delivery timing

- Record Telegram arrival age and ingestion duration separately to diagnose
  late signals without recording message contents or changing trading rules.

### Missed-signal status

- Send one localized skipped-signal notification with the channel post and reason
  when a recognized entry is not opened; retain separate error notifications.
- Safely incorporate target-only edits to an unchanged entry zone while the
  original signal is being evaluated or waiting, without replaying old signals.

### Uzbek channel signals

- Recognize the channel's explicit `selldaman` and `buydaman` entry-zone
  formats before AI review, retaining source, quote, risk and direction checks.

### Immediate gold market signals

- Recognize unambiguous buy/sell-now instructions as market entries without a
  printed zone, keeping the confirmed sizing, protective stop, AI votes and
  fresh-signal checks; attach later matching targets to the same positions.

### Gold channel update safety

- Skip a superseded signal update without disabling entries only when the
  tracked signal and broker both confirm there are no positions or pending
  orders; retain the protective stop for exposure or unavailable checks.

### Dependency security

- Updated pyasn1 to 0.6.5 to address four ASN.1 decoding vulnerabilities.

### Telegram notification widgets

- Added a private quick menu with styled read-only buttons inside the widget;
  validate and answer every tap, update the same card and fall back to one
  editable text message when widgets are unavailable.

- Removed the duplicate visible caption and matched widget colours and rounded
  surfaces to the website's light and dark themes.
- Added owner-only, read-only account, positions, pending orders and recent
  channel commands; report broker failures instead of false empty lists.

- Display private-chat trading events, warnings, errors and command replies as
  compact Telegram Bot API 10.4 HTML widgets without duplicate captions.
- Fall back to rich and classic messages when widgets are unavailable or too
  long, while retaining grouped entries and existing notification permissions.

### Gold channel entry window

- Support an explicitly configured, time-limited wait for gold to return to an
  entry zone, with a bounded price tolerance and no automatic order retry.
- Recheck source, edits, later channel messages, fresh quote and armed state;
  discard waits on restart and refuse entry after the first target is reached.
- Added simulated-broker tests for waiting, price tolerance, cancellation,
  signal changes and the two protected trade legs.

### Documentation interface

- Restyled scrollbars and replaced the system language selector with an accessible
  custom menu; matched previous and next navigation cards across screen sizes.
- Kept previous and next cards compact instead of stretching across the page.

### Source-specific gold trading

- Added an opt-in, source-bound gold strategy with a fixed stop in price units,
  explicit two-leg sizing and three independent pre-entry classifications.
- Attach subsequently edited TP levels to the original tickets and handle a
  reply requesting break-even without opening or reversing positions.
- Require a fresh quote, the current unedited signal, matching direction and
  entry zone; reject stale, ambiguous, corrected or replayed signals.
- Verify each broker fill and its stop, never reprice or retry an uncertain
  order, and disable entries on partial, unverified or unrecorded fills.
- Added simulated-broker coverage for fixed stops, late targets, break-even,
  contradictory edits, provider disagreement and broker failures.

### Research-only channel onboarding

- Added a research-only source mode that blocks AI trading writes until the
  operator defines and verifies the channel's execution policy.
- Added a non-trading parser for abbreviated Uzbek gold price zones, targets and
  management messages; ambiguous or contradictory prices fail closed. Recent
  posts may be interpreted against a broker quote and saved for inspection,
  explicitly marking that quote freshness was not verified.
- Kept the research-only source identifier in private runtime settings.

### Channel correction and stop-loss protection

- Stop automatic entries and notify the operator if the source edits a signal's
  direction or entry zone; verify the latest revision and direction again before
  each broker order. Never automatically reverse an existing position.
- Restore the stop-loss watchdog for channel positions and limit it to adding
  protection to its assigned unprotected ticket, even while entries are disabled.
- Added regression tests for edited signals, changes between order legs, and
  ticket-scoped watchdog protection.

### Codex failure diagnostics

- Report a sanitized upstream Codex failure and distinguish authentication, rate
  limits and service outages while preventing tool calls from being replayed.

### Codex provider

- Monitor the optional Codex service on its local port in Docker Compose.

- Added an opt-in Codex app-server provider for GPT-6.1 Terra with medium reasoning,
  isolated per-run conversations, guarded dynamic tools and no automatic replay
  after a tool result. Added Codex selection to the settings in three languages.
- Verified authentication and a mock-only tool call against a local Codex service;
  production activation still requires the separately configured service and a
  reviewed deployment.

### Refresh channel research

- Refresh the bounded channel playbook when the recent message sample changes;
  retain the last usable profile if a refresh fails, and never append to it.
- Feed the matching active profile to classifier and trading runs as untrusted
  context; do not mix profiles across Telegram sources.

### Channel signal research

- Added a source-specific parser for abbreviated gold entry zones and up to 12
  targets, with fail-closed checks for contradictory levels and cautious matching
  of threaded or uniquely matched recent unthreaded follow-ups.
- Implemented two 0.20-lot teaser entries and subsequent attachment of channel SL,
  TP2 and TP4 to the existing positions, with break-even for the second position
  after the first closes. Added protections against duplicate entries, looser SL
  and direct model trade writes for this source. Three isolated, tool-free AI
  sessions classify entries in parallel and a two-vote agreement is required
  for automatic fast entry. Follow-up protection is applied independently of AI.
  Activation remains an explicit per-source configuration choice.

### Minimal film player

- Simplified the film opening and replaced the pause card with centered playback
  controls and a bottom control bar, removing the on-screen pause prompt.

### Cinematic film controls

- Redesigned the film opening and pause controls with custom icons, clear voice
  and sound settings, responsive layout and reduced-motion support.

### Three film voices in three languages

- Added Ivan, Pavel and Aleksandr recordings in Russian, English and Uzbek, with
  synchronized word highlighting and voice selection before and during playback.

### Russian promo-film narration

- Replaced the Russian film narration with ElevenLabs voice Ivan and synchronized
  word highlighting using the new audio timing data.

### Shared documentation theme and attached LV-1 speech

- The documentation now builds from the same theme tokens as the application, including
  light/dark neutral surfaces, typography, borders and accessible feedback colors.
- Anchored LV-1's speech bubble beside its head, with mirrored placement and facing on
  the right side, a smaller monospace voice, and springy appearance and shrinking exit.
- Kept the bubble attached during movement and respected reduced-motion preferences.

### Waitlist documentation and companion styling

- Linked the official documentation from the waitlist footer in the visitor's chosen language.
- Restyled LV-1's speech bubble to match the site's rounded controls and light/dark theme,
  retaining distinct TP and SL text colors.

### Visible copy feedback and simpler Windows install

- Copy buttons now show an animated checkmark and localized confirmation in the hero
  and all guides, with clear manual-copy feedback if clipboard access is denied.
- Reset copy feedback on OS changes and retain only the latest confirmation timer.
- Shortened the official Windows curl command while preserving complete-script parsing,
  unchanged execution policy and optional Python setup through the hosted installer.

### Simpler, rounded documentation interface

- Reduced the installation hero to its title, OS tabs, installation command and guide/demo links.
- Added a persistent macOS beta badge and a short localized note about pending Mac MT5
  validation and the unavailable local Apple Silicon runtime.
- Rounded controls, terminals, navigation and search to match the product style; added
  a sliding OS selection pill, subtle tab bounce and command transition with reduced-motion support.

### Desktop compatibility clarification

- Clarified that SSH mode connects to an existing Linux x64 computer/VPS supplied by
  the user; hosting is arranged separately from installing the CLI.
- Marked automatic local Apple Silicon execution as unimplemented/unvalidated and
  separated CLI portability checks from real MT5/Python/RPC integration validation.
- Explained the Linux-specific host-library dependencies of the existing ARM/FEX
  deployment and the remaining Mac validation work in all three documentation languages.
- Retained runtime architecture guards; changed no broker connection or running service.

### Official documentation and download site

- Added a standalone, pre-rendered docs.lvenc.site site with a minimal installation hero,
  automatic desktop OS selection, real locally hosted Windows/Apple/Linux SVG logos,
  copyable shell/PowerShell commands, mobile guidance and dark/light themes.
- Added eight guides in English, Russian and Uzbek, sidebar/table-of-contents navigation,
  keyboard search, accessible mobile navigation and code-copy controls.
- Added an official HTTPS preview distribution manifest with immutable SHA-256 archives,
  validated public-source exports and download integrity checks; GitHub release tags remain unchanged.
- Added isolated static deployment and DNS for the documentation subdomain, with no trading restart.
- Verified desktop OS detection, mobile layouts from 320 px, exact clipboard copying,
  multilingual search, HTTPS CLI installation, immutable caching and WCAG A/AA checks.

### CLI diagnostics, safe updates and support inbox

- New CLI installs select a free loopback port from 19471 instead of the common 8000;
  explicit ports fail on collision, SSH installs probe the server, existing ports remain.
- Added component-aware health/watch, structured doctor output, error groups with next
  steps, severity/text/time log filters and a bounded private CLI failure journal.
- Added public update checks and explicit stopped-runtime apply/rollback with durable
  recovery state, previous source/CLI/image retention and unchanged credentials/volumes.
- Added local-first minimised support reports and explicit HTTPS submission, a separate
  encrypted support receiver with expiry, rate/body limits, challenge/replay protection
  and a durable Telegram/SMTP owner notification outbox. No broker volume is mounted.
- Added user/agent command contracts, time budgets and support retention instructions;
  consolidated stale product/deployment notes into current guides and the runbook.
- Added regressions for port collisions, health readiness, redaction, upload consent,
  stopped updates/rollback, inbox encryption, abuse limits and independent alert retries.

### Cross-platform installation and LVCT console

- Added a dependency-free `lvct` package and coloured terminal menu with setup, doctor,
  dependency installation, start/status/logs, local/SSH panel access and password commands.
- Added curl/shell and PowerShell installers that resolve public revisions to commit IDs,
  reject unsafe archives, preserve existing configuration and install per-user launchers.
- Isolated new installations using their own protected environment file, Docker context,
  image names and owner-labelled volumes. Start does not recreate running services.
- Added live dashboard password/username rotation using a salted PBKDF2 hash in the data
  volume; old cookies and WebSocket access expire without restarting the trading process.
  Container liveness no longer depends on the original bootstrap password. Expensive checks
  run in bounded workers outside the trading event loop, with pre-verification throttling.
- Retained password overrides and session/rate-limit state in owner-only snapshot archives,
  so restoring a private clone does not reactivate the old bootstrap password.
- Documented Linux amd64 and Docker Desktop paths, SSH runtimes for ARM clients, remaining
  desktop broker validation, and the separate existing Linux ARM/FEX deployment.
- Added CLI/offline installer tests and a Linux/Windows/macOS CI matrix, plus backend auth
  regressions, CLI version consistency and explicit public-export allowlisting.

### Corporate support email

- Configured inbound `support@lvenc.site` forwarding to the owner's existing inbox through
  Forward Email, using an encrypted DNS rule rather than a public recipient address.
- Added mail MX/SPF records and verified authoritative/public DNS and website health.
- Enabled outgoing support mail through Oracle Email Delivery with domain DKIM signing,
  merged SPF and TLS-authenticated SMTP; preserved incoming forwarding and DMARC.
- Configured a 90-email-unit daily quota in the sending region to remain below the monthly
  free allocation, and retained SMTP credentials outside Git. Verified SMTP authentication
  and acceptance of one owner-approved test message.

### LV-1 arm movement and cash launchers

- Rebuilt the greeting and money-throwing motions with smoothly articulated arms whose
  upper and lower sections retain their lengths.
- Added a frequent two-handed cash-launcher animation, with bills flying to both sides;
  the companion moves clear of the screen edge first and respects reduced-motion settings.
- Deferred the companion until after page load and load its activity artwork later, while
  retaining cached artwork and the companion's state across page navigation.
- Checked arm geometry, page-load timing, animations and navigation in Chromium and WebKit.

### LV-1 sleep and shake animation

- Replaced the sleep emoji with softly floating `Z` letters.
- Rapid back-and-forth dragging makes LV-1 dizzy: its eyes spiral, it sits down with
  rotating stars above its head, then recovers. Ordinary drags do not trigger the effect.
- Kept the action silent, preserved its state across public-page navigation, and added a
  still seated pose for reduced motion. Verified mouse, touch, WebKit and gesture detection.

### Signal-scene hold interaction

- Prevented browser text selection and touch callouts in the signal scenes while retaining
  normal scrolling, and clear existing selection when 4× hold begins.
- Stop 4× exactly when the simulated trade reaches its result, including within a frame;
  subsequent result and Telegram-notification animations run at their regular speed. A new
  replay can be accelerated again, but holding after the result cannot restart acceleration.
- Validated clock boundaries with unit tests and exercised both scenes, selection, replay
  and post-trade timing in Chromium; checked selection behavior in WebKit.

### Persistent, quieter LV-1 companion

- The companion now uses a single instance across waitlist and demo navigation, preserving
  its position, current activity and animation rather than greeting again on each page.
- Limited speech bubbles to a greeting and TP/SL outcomes. Sleep now displays gently
  rising, swaying sleep marks without a bubble, with reduced-motion support.
- Added content-addressed pet artwork with long-lived browser caching and retained older
  versions for open tabs. The website deployer validates hashes before publishing.
- Verified navigation, TP/SL, quiet actions, sleeping animation and cache reuse in Chromium,
  plus continuity in WebKit; deployment safety checks pass.

### Demo layout, companion visibility and story previews

- Made LV-1 appear as soon as its small standing image loads, added an atlas fallback and
  SVG compatibility, and raised its mobile position above the browser's bottom controls.
- Removed the demo header's waitlist button, centered its title, and moved the three
  integration icons out of their card while removing their extra descriptions.
- Replaced the old waitlist thumbnail with localized captures of the first story scene.
  Added localized 1200×630 Open Graph images and metadata for story link previews.
- Validated mobile layouts in three languages, WebKit with a delayed atlas and failed pose,
  and social preview metadata/images with automated tests.

### Story sound and faster signal playback

- Removed the chapter navigation dots from all story language routes; scrolling and keyboard
  navigation remain available.
- Added the film-inspired procedural music and signal effects to the story, activated by a
  user gesture with a localized sound control and a remembered mute preference.
- Increased held playback to 4× in both signal scenes and show a small `4×` opposite the
  chart's channel-opened label while holding. Release or movement returns to normal speed.
- Verified actual Web Audio output, mute persistence, touch playback speed, all three
  locales, reduced motion and the frontend build/tests in Chromium.

### Independent public-site deployment

- Added a Caddy-served public frontend and a guarded deployment script so updates to
  waitlist, demo and story can be installed without restarting the trading containers.
- Preserved API proxying, security headers and CSP; retained previous hashed bundles for
  open tabs. The deployer validates Caddy, saves a rollback configuration, rejects symlinks
  and environment files, and restores the old configuration on switch failure.
- Validated routing and browser behavior against an isolated Caddy instance, plus four
  deployment safety tests.

### Public-page companion and story finale

- Moved the interactive LV-1 companion to the waitlist and demo pages only; its controls,
  animations and EN/RU/UZ labels follow the selected website language and clean up on exit.
- Holding the right side of either signal scene now plays its timeline and market simulation
  at twice the speed, with a subtle edge shade. Release, movement, navigation or loss of
  focus returns playback to normal without a speed label.
- Reworked the final story scene: the logo rises, the title is typed, and the dashed route
  link, film button and Telegram/LV/MetaTrader icons appear in sequence. Removed the extra
  final-scene captions and retained full reduced-motion and screen-reader support.
- Validation: frontend typecheck/build and tests; Chromium checks on mobile and desktop for
  all three languages, touch/mouse hold and release, navigation cleanup and reduced motion.

### Private CI configuration isolation

- CI removes owner `.env` files from its disposable checkout before running tools/tests.
  Private restore configuration no longer changes test Host validation or supplies owner
  credentials to the test process. Production configuration and checks remain unchanged.
- Found by inspecting the first private Actions run; the public run passed all checks.

### Publication verification policy

- Agent instructions now require version/export checks before publication and inspection of
  GitHub Actions/check statuses for the exact published commits in both repositories.
- Publication reports must distinguish pushed source, CI results and deployment verification;
  failed, pending, skipped or unavailable checks must be reported explicitly.

### Pre-publication validation

- Current source passed 671 backend tests in a network-isolated container, 10 frontend
  tests, frontend typecheck/production build and 13 publishing/snapshot tests.
- The allowlisted public export passed credential scanning and version consistency checks.

### Story language follows the main website

- Removed the standalone story language switcher. Story pages read the main site's saved
  EN/RU/UZ preference before rendering and select the matching translated page.
- Story navigation no longer overwrites the main language preference. Locale redirects
  retain chapter hashes and internal-navigation welcome suppression; open tabs also follow
  language changes made on the main site. Without a saved preference, the current URL works.
- Browser checks cover all nine stored-language/URL combinations, chapter preservation,
  no visible picker, no welcome replay on redirects, cross-tab changes and direct URL fallback.

### Uzbek interface and translated GitHub link

- Added a complete 774-key Uzbek Latin UI dictionary, Uzbek browser/URL detection,
  locale persistence and an explicit EN/RU/UZ language picker. Public pages, login,
  privacy and dashboard strings now share the three-language interface. Added visible
  EN/RU/UZ links to each standalone story and fixed narrow translated header overflow.
- Localized the shared GitHub link and accessible name in all three languages, including
  immediate updates on locale change and language persistence from standalone stories.
- Removed opacity changes from the floating link: scroll now moves it fully below the
  viewport and back without fading. Decorative star particle fades are independent.
- Kept the existing EN/RU Telegram notification preference when saving the Uzbek UI;
  this website translation does not change trading or notification language support.
- Validation: typecheck/build, 10 frontend tests including dictionary/placeholder parity,
  all five public page types in all three languages, instant translated GitHub labels,
  preference persistence and fully opaque scroll transitions. Header fits 320–390px screens.

### Star effect tuning

- Retuned the GitHub link slide from 1000ms to 520ms with smooth easing.
- Split the welcome shower into three waves of six stars, 650ms apart; reduced flight
  height to 42–76px and sideways spread to 16–48px around the button.
- Browser checks verify six stars per wave, bounded trajectories, scroll timing, no internal
  navigation replay, reload replay, cleanup and reduced-motion behavior.

### GitHub star welcome effect

- Slowed the floating GitHub link slide to one second with gentler easing and a 14px
  direction threshold. Added a single short gold-star shower and subtle welcome glow.
- Internal navigation, SPA route changes and history returns suppress replay; reloads and
  fresh visits allow it again. Decorative particles clean up automatically and respect
  reduced motion, background tabs, scrolling and focused fields.
- Added optional native vibration on supported devices after real user activation and when
  activating the GitHub link; no synthetic activation or unsupported-browser workaround.
- Restricted DOM observation to route mounts so live market text does not trigger redraws.
- Verified build/typecheck, intro particle cleanup, SPA and standalone-story navigation,
  reload/direct-entry replay, scroll transitions and reduced-motion behavior in Chromium.
  Native haptic calls were checked with a mocked device API and real user activation;
  physical vibration remains dependent on browser/device support.

### Transparent vector brand

- Replaced background-backed raster logos in the React app with transparent inline SVG.
  The LV monogram and outlined wordmark have separate configurable mark/text/highlight
  colors; text automatically follows the theme. Added standalone SVG downloads and usage.
- Removed demo integration icon borders, rounded wrappers and brand backgrounds; reused
  the native Telegram/MetaTrader marks without extra containers around their artwork.
- Verified typecheck/build, light/dark mobile rendering, transparent SVG paths and independent
  CSS color changes. Deployed a frontend-only overlay after checking non-root asset access.

### Public navigation and controls

- Removed the waitlist owner sign-in button and email-only hint; kept the header sticky.
  Email inputs use 16px text to avoid iOS focus zoom, and the data policy opens a new tab.
- Added one shared fixed GitHub star link to waitlist, demo and all three story languages.
  It slides out on downward scroll, returns on upward scroll, clears focused form controls,
  respects safe-area insets and reduced-motion preferences, and remains keyboard accessible.
- Reused the story's Telegram and MetaTrader artwork in the demo integration strip.
- Verified types/build, nine frontend tests and mobile browser checks for sticky navigation,
  scroll direction, form focus, links and loaded artwork; checked all three story languages.
  Deployed a frontend-only image overlay while retaining existing hashed assets. Verified
  non-root file access, restored the prior armed state and kept the MT5 container running.

### Security audit — 2026-10-27

- Private APIs now fail closed with missing credentials. Unique signed sessions are bound
  to current credentials; logout revocation and per-IP/global login budgets survive restart.
  Active WebSocket streams stop sending private data after session expiry or revocation.
- Enforced matching Origin scheme, allowed Host values, bounded API request bodies, Telegram
  image download/time/pixel limits, and additional cloud-token redaction/public export scans.
- Updated the host Node runtime to 24.22.1; rebuilt Caddy 2.11.7 with Go 1.27.3,
  x/net 0.60.0 and chi 5.4.0. Hardened proxy headers, timeouts, service permissions and SSH
  connection throttling; closed unnecessary IPv6 service exposure.
- Rebuilt the backend on patched Alpine/Python with no root privileges, read-only root,
  dropped capabilities, bounded processes/concurrency/logs and no runtime package installer.
- Restricted live MT5 RPC access to the backend. The portable Compose deployment separates
  its RPC network and passes only MT5/RPC variables, avoiding backend and Telegram secrets.
  Added sidecar token placeholder rejection, stalled-client timeout, privilege restrictions
  and current Pillow; applied 40 available Wine-image package updates.
- Prepared pinned GitHub Actions dependency/security checks, Dependabot updates and a private
  reporting policy. Repository-side security settings require authenticated GitHub access.
- Validation: 666 isolated backend tests, 38 focused sidecar tests, frontend types/build and
  nine tests, 13 publishing/snapshot tests; npm/pip audits and the deployed backend image
  scan report zero known vulnerabilities. Verified live logout replay denial, proxy-IP
  sanitization, Host/Origin/body rejection and RPC isolation without any test broker orders.
- Wine/GUI image findings are tracked separately in the security audit; vendor-unfixed
  findings must not be represented as a clean scan of the entire installation.

### Deployment

- Applied the rich-notification backend to the running application; the previous container
  still used the legacy OPENED template despite updated source. Rebuilt the runtime image,
  retained current public assets, prepared rollback and a consistent database backup,
  paused new entries for replacement and restored the prior armed state after readiness.
- Verified the deployed notifier hash, MT5/Telegram connection, remaining open position,
  145 isolated notification/trading regression tests and a real sendRichMessage test card.
  MT5 was not restarted and no test orders were executed.

### Added

- Imported the product-film source, three-language narration, interactive story, LV-1
  artwork, and asset provenance. Added official website, film and story links to README.
- Applied the approved logo, mini mark and app icon across public/private headers, login,
  demo, browser/touch icons and documentation; original artwork remains available.
- Connected `lvenc.site` with automatic HTTPS and a canonical `www` redirect. Preserved
  unrelated DNS records and existing authentication, without restarting trading.

### Improved

- Simplified the waitlist to one centered demo link, an email signup form and a story
  preview below it. Removed excess copy and card chrome; plain Explore links, rounded
  signup controls and thin dividers follow the minimal interface style.
- Clarified self-hosting on the user's VPS and launch notification signup. Added clear
  privacy/consent/error/success feedback, accessible hidden email labels, correct heading
  order, mobile layout and a skip link in English and Russian.
- Unauthenticated home visitors reach the waitlist; operator dashboard/login stay protected.
- Replaced oversized public logo/icon downloads with small content-hashed WebP derivatives;
  enabled immutable caching for hashed assets while HTML remains refreshable.
- Externalized promo inline scripts for the same-origin CSP. Optimized only the interactive
  story in the performance pass: compressed local fonts, deferred classic scripts, cached
  closed-candle OHLC, reduced ticker layout reads and bypassed redundant background canvas
  compositing. Film playback logic and private trading logic were not changed.

### Validation

- Pre-publication checks passed: 12 publishing/snapshot tests, 60 isolated public signup/demo
  API tests, frontend typecheck/build and nine frontend tests. No broker orders were executed.

- Verified frontend types/build and nine frontend tests; public UI in both themes on mobile
  and desktop; signup validation without creating subscribers; Axe accessibility checks;
  and all nine story chapters in English, Russian and Uzbek.
- Original/optimized market values and canvas pixels match across 50 deterministic frames.
- Live Lighthouse Mobile: waitlist 99 (owner's previous PageSpeed report: 81), demo 99
  (previously 72), story 93 (previously 48); all three scored 100 for accessibility,
  best practices and SEO. Film and private trading were excluded from the performance pass.
- Earlier brand-integration checks verified the imported film startup and Uzbek audio.

## [0.1.0] - 2026-10-26

First public MVP release of LV Copytrader.

### Added

- Telegram signal ingestion, AI interpretation, MetaTrader 5 execution, and a web dashboard.
- Market and pending orders, stop-loss and take-profit management, multi-target entries,
  ladder cover/runner handling, break-even policies, history, and recovery after reconnect.
- Native Telegram RichMessage notifications, grouped entries, and distinct TP/SL/error states.
- Live TP/SL monitoring with confirmed remaining-position protection in the TP card.
- Dual-repository publishing: private snapshot first, independently scanned public export second.
- Owner snapshot/restore tooling, agent instructions, and a version consistency check.

### Improved

- Two positions belonging to one signal run produce a single entry card with individual levels.
- Failed break-even moves remain errors; notifications show the actual unchanged stop.
- Public installation documentation and credential-free example configuration.

### Release notes

- `0.1.0` starts the public version series. Earlier private implementation labels, including
  `1.1.0`, predate this release scheme and are not public release versions.
- The private repository can contain the owner's environment, encrypted settings database,
  Telegram session, media, and broker configuration. These are excluded from public exports.
