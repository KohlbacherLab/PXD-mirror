# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-23

First public release.

### Added
- `pxd` command and `pxdmirror` library: query, list, get (Globus, FTP, HTTPS with byte ranges), refresh,
  path, stage (local and over ssh/rsync), convert (mzML, mzPeak) with provenance and output validation,
  delete with confirmation token, migrate (plan/apply), import-legacy, verify, recover, reindex, init.
- Configuration system: TOML files (`--config`, `$PXD_CONFIG`, `~/.config/pxd/config.toml`), `PXD_*`
  environment variables, command-line overrides; `pxd config` shows every value and its source.
- `pxd doctor [--fix]`: checks and repairs group, modes, sticky bits and default ACLs of a shared mirror.
- Multi-user handling: explicit modes and umask, catalogue created group-writable, Globus intents owned by
  their submitter, other users' Globus deliveries copied rather than hard-linked, migration of other users'
  files only when group read-write and in the mirror's group, read-only lock files, staged copies honour the
  caller's umask.
- Globus Connect Server collections as destination (`globus.kind = "gcs"`, `globus.path_map`), honouring
  the read-write flag and `~` of GCP config-paths.
- Optional sha256 pins for converter binaries and deployment files; `${VAR}` and `~` in the registry.
- `paths.forbidden_roots`; per-host ssh and rsync options for remote staging; shell-safe quoting of remote
  paths.
- Hardening from an adversarial review: recovery validates intents (paths inside the mirror, accession,
  MD5) and takes the data-set and per-intent locks; catalogue updates and reindex read the records inside
  their transaction; deletes check every item first; manifests and inventories with a wrong structure or
  unsafe names are reported as corrupt; staging refuses targets inside the mirror, validates hosts and
  protects rsync arguments; query and get compare with the current PRIDE checksums; clearer exit codes.
- Documentation (configuration, Globus, permissions, operations, design with threat model and known
  limitations), Claude Code skill, CI.

### Notes
- The library was developed as `pxdstore`; the Globus task label prefix and the validator name keep that
  name for compatibility with existing mirrors.
