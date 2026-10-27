# Changelog

Every published change is recorded here. Versions use `MAJOR.MINOR.PATCH`;
private and public releases share the same version, with separate Git histories.

## [Unreleased]

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
