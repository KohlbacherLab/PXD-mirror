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
- Second review round: `pxd recover` and Globus adoption act only on intents the caller wrote (owner from the
  file, intents written 640); converters are refused if anything they load (deployment, `env` directories,
  `ldd` libraries) can be changed by another user, re-checked before every run, with an optional
  `tree_sha256` pin and `recipe_deployment_sha256` for relinked deployments; conversion staging must be
  private and not replaceable through a world-writable parent; `paths.forbidden_roots` also covers
  conversion staging, migration sources and plan files and legacy imports; an archived file is replaced
  only once a verified copy exists, and a Globus delivery without a PRIDE checksum never replaces one;
  adopted Globus tasks publish only the requested files; `delete` handles Globus tasks still in flight;
  configuration layers `$PXD_SITE_CONFIG` < personal file < `--config`, `[stage.hosts]` merged per pattern,
  overridden values listed by `pxd config`; `pxd config` and `pxd doctor` warn when the installation or a
  directory above it can be changed by other users.
- Hardening from an adversarial review: recovery validates intents (paths inside the mirror, accession,
  MD5) and takes the data-set and per-intent locks; catalogue updates and reindex read the records inside
  their transaction; deletes check every item first; manifests and inventories with a wrong structure or
  unsafe names are reported as corrupt; staging refuses targets inside the mirror, validates hosts and
  protects rsync arguments; query and get compare with the current PRIDE checksums; clearer exit codes.
- Converters with compiled-in paths: documentation of the OpenMS case (its compiled-in `share/OpenMS` wins
  over the copy next to the executable and over `OPENMS_DATA_PATH`), a tracing check for installers, and
  `recipe_deployment` to keep a recipe name after the pins move to the files actually loaded.
- Documentation (configuration, Globus, permissions, operations, design with threat model and known
  limitations), Claude Code skill, CI.

### Notes
- The library was developed as `pxdstore`; the Globus task label prefix and the validator name keep that
  name for compatibility with existing mirrors.
