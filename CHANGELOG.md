# Changelog

Every published change is recorded here. Versions use `MAJOR.MINOR.PATCH`;
private and public releases share the same version, with separate Git histories.

## [Unreleased]

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
