# Changelog

Every published change is recorded here. Versions use `MAJOR.MINOR.PATCH`;
private and public releases share the same version, with separate Git histories.

## [Unreleased]

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
