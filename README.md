# pxd-mirror

[![CI](https://github.com/KohlbacherLab/PXD-mirror/actions/workflows/ci.yml/badge.svg)](https://github.com/KohlbacherLab/PXD-mirror/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**One verified copy of public PRIDE / ProteomeXchange (PXD) data sets for a whole group.**

`pxd` keeps a local mirror of public proteomics data sets from the
[PRIDE Archive](https://www.ebi.ac.uk/pride/) on a shared file system. Before anyone downloads a data set,
`pxd query` tells whether it is already there. `pxd get` downloads each file once, checks it against PRIDE's
MD5 checksums and publishes it without ever overwriting anything. Conversions to mzML or mzPeak are stored next
to the raw files with full tool provenance. A small SQLite catalogue answers "what do we have?" in
milliseconds and can always be rebuilt from the plain-file records.

It is a single Python package with no dependencies beyond the standard library (Python 3.11 or newer) and
the `curl` command.

## Features

- **Query before download.** `pxd query PXD000001` answers with exit code 0 (complete), 10 (partial) or 11 (absent).
- **Verified downloads.** Globus (optional) first, then FTP, then HTTPS with parallel byte ranges; bounded
  retries; every file checked against PRIDE's MD5 and size; quota checked before starting.
- **Safe publication.** Private staging, an intent record, a no-overwrite hard link, then manifest,
  catalogue and event log; `pxd recover` finishes or reports whatever a crash interrupted.
- **Multi-user by design.** setgid directories, default ACLs, explicit modes and umask; other members' files,
  locks and interrupted runs are handled; `pxd doctor` checks and repairs permission drift.
- **Conversions with provenance.** RAW to mzML/mzPeak through a registry of converters; tool version,
  executable and library hashes, arguments, environment, host and validation results are recorded; output
  validation catches broken mzML indexes.
- **Two-step deletion** with a confirmation token bound to the exact list; tombstones and history kept.
- **Migration** of existing copies elsewhere on the file system: only files that match PRIDE's checksum list
  are moved, a symlink is left behind.
- **Staging** to local scratch or, over ssh/rsync, to a remote cluster, verified at the target.
- **Configurable** for laptops, group servers and clusters: TOML file, environment variables, flags.

## Requirements

- Python 3.11 or newer (only the standard library)
- `curl`; for remote staging `ssh`, `rsync` (3.2.3 or newer) and `md5sum` on the target
- optional: the [Globus CLI](https://docs.globus.org/cli/) and a Globus endpoint or collection
- optional: converters such as OpenMS FileConverter or mzpeak-convert for `pxd convert`
- Linux or macOS; a POSIX file system (CephFS, NFS, ZFS, ext4, APFS, ...); ACL support is used where present

## Installation

```bash
pipx install git+https://github.com/KohlbacherLab/PXD-mirror      # isolated install of the pxd command
# or
git clone https://github.com/KohlbacherLab/PXD-mirror && cd PXD-mirror && pip install .
# or run straight from a checkout, without installing:
git clone https://github.com/KohlbacherLab/PXD-mirror && PXD-mirror/bin/pxd --version
```

`bin/pxd` runs the package with `python3 -I` from its own checkout. For a group installation, unpack a release
into a directory only maintainers can write, put the site configuration in `<installation>/site/config.toml`
(picked up automatically by `bin/pxd`) and let users call `<installation>/bin/pxd`. See
[docs/PERMISSIONS.md](docs/PERMISSIONS.md#installing-the-tool-for-a-group).

## Quick start

```bash
mkdir -p ~/.config/pxd
cat > ~/.config/pxd/config.toml <<'EOF'
[paths]
root = "~/data/pxd"
[permissions]
shared = false
EOF

pxd config                                   # effective settings and their sources
pxd init                                     # create the mirror root
pxd query PXD000001                          # absent (exit 11)
pxd get PXD000001 --files F063721.dat --dry-run
pxd get PXD000001 --files F063721.dat
pxd query PXD000001 --files
pxd path PXD000001 F063721.dat
```

## Command reference

| Command | What it does |
|---|---|
| `pxd query PXD [--files] [--json]` | is it mirrored? exit 0 complete, 10 partial, 11 absent |
| `pxd list [--json]` | data sets in the mirror |
| `pxd get PXD (--files F.. \| --file-list L \| --category RAW \| --all) [--method auto\|globus\|ftp\|https] [--dry-run]` | download and verify |
| `pxd refresh PXD` | fetch a new PRIDE inventory generation (file list and checksums) |
| `pxd path PXD NAME [--kind raw\|mzML\|mzPeak] [--recipe R]` | path of a file or of its newest conversion |
| `pxd stage PXD (--files F.. \| --all) --to DIR\|HOST:PATH [--unzip]` | verified copy for compute |
| `pxd convert PXD --files F.. --to mzML\|mzPeak [--tool T] [--threads N] [--stage DIR]` | convert with provenance |
| `pxd tools` | registered converters with version and hashes |
| `pxd delete PXD (--dataset \| --files F.. \| --derived-only) [--yes --confirm TOKEN]` | two-step deletion |
| `pxd migrate --plan SRC.. \| --apply PLAN [--no-symlink]` | move existing PRIDE-verified copies into the mirror |
| `pxd import-legacy [--plan] DIR..` | import converted mzML from a legacy per-data-set archive |
| `pxd verify PXD` | re-hash a data set |
| `pxd recover` | finish or report interrupted publications and Globus tasks |
| `pxd reindex` | rebuild the catalogue from the manifests |
| `pxd init` | create a mirror root (setgid, ACLs, catalogue, README) |
| `pxd config [--json]` | effective configuration and the source of every value |
| `pxd doctor [--fix] [--json]` | check group, modes, sticky bits and ACLs; repair entries you own |

Global options: `--root DIR`, `--config FILE`, `--tools FILE`, `--version`.

Per-file results: `ok`, `skip`, `exists`, `would-download`, `would-adopt`, `locked`, `failed`,
`not_attempted`, `copied`, `conflict`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`query`: complete) |
| 1 | some files failed, were locked or not attempted (`doctor`: problems found) |
| 2 | usage or configuration error |
| 3 | operational error (quota, unreadable record, busy catalogue, I/O) |
| 10, 11 | `query`: partial, absent (`path`: 11 if not found) |

### Examples

```bash
pxd get PXD000001 --category RAW,PEAK                # by PRIDE file category
pxd get PXD000001 --all --method https --split 8     # HTTPS only, 8 ranges per large file
pxd convert PXD000001 --files RUN01.raw --to mzPeak
pxd stage PXD000001 --all --to cluster:/work/me/pxd  # rsync over ssh, verified with md5sum there
pxd delete PXD000001 --dataset                       # prints the list and a token; deletes nothing
pxd delete PXD000001 --dataset --yes --confirm <token>
pxd migrate --plan /shared/old-downloads
```

Python:

```python
from pxdmirror import Mirror
m = Mirror()                              # settings from the configuration
m.query("PXD000001")["state"]             # 'complete' | 'partial' | 'absent'
m.get("PXD000001", names=["F063721.dat"]) # [(name, result, detail)]
m.path("PXD000001", "F063721.dat")
```

## Mirror layout

```
<root>/
  PXDnnnnnn/raw/                 files exactly as PRIDE serves them
  PXDnnnnnn/mzML/<recipe>/       conversions; recipe = <tool>-<version>-<hash of arguments and deployment>
  PXDnnnnnn/mzPeak/<recipe>/
  PXDnnnnnn/metadata/<gen>/      PRIDE inventory generations; CURRENT names the one in use
  PXDnnnnnn/manifest.json        files and conversions (authoritative)
  catalog.sqlite                 query index (rebuild with pxd reindex)
  .events.jsonl                  history: downloads, conversions, deletions, migrations
  .locks/ .ops/ .work/           locks, intents and plans, private staging
```

Design and rationale: [docs/DESIGN.md](docs/DESIGN.md).

## Configuration

pxd needs one setting, the mirror root. Everything else has generic defaults (Globus off, private scratch in
the system temporary directory, group-shared modes). Precedence: command line > `PXD_*` environment >
`--config FILE` / `$PXD_CONFIG` > `~/.config/pxd/config.toml` > defaults.

```toml
[paths]
root = "/shared/proteomics/pxd"
scratch = "/scratch/{user}/pxd"
[permissions]
group = "proteomics"
```

All keys, environment variables and worked examples (single user, shared group mirror, no Globus, Globus
Connect Personal, Globus Connect Server, read-only consumers, remote staging):
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). A commented template: [examples/config.toml](examples/config.toml);
a converter registry template: [examples/tools.json](examples/tools.json).

## Multi-user mirrors

A shared mirror uses one POSIX group, setgid directories (`2770`), a default ACL (group `rwx`, other none)
and files `660`. **Do not set the sticky bit** on mirror directories: it would stop members from deleting or
replacing each other's files. `pxd doctor` checks the invariants, `pxd doctor --fix` repairs entries you own.
Details: [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## Globus

Globus is optional and off by default. How to install the CLI, log in (and where the token is stored),
set up Globus Connect Personal or use a Globus Connect Server collection: [docs/GLOBUS.md](docs/GLOBUS.md).

## Safety

- **Public data only.** Every accession must resolve in the public PRIDE API, and `migrate` moves only files
  that match PRIDE's checksum list. Do not put unpublished or controlled-access data into a mirror.
- **Downloads are untrusted.** pxd never executes downloaded content. File names with `/`, `..`, a leading
  `.` or NUL are refused; symlinks inside the mirror are refused; zip members that escape the target or are
  symlinks are refused; zip bombs are refused by size and ratio limits.
- **No overwrites.** Publication uses hard links that fail if the target exists; a different existing file
  is reported as a conflict and kept.
- **Read paths never write.** `query`, `list`, `path`, `get --dry-run` and `migrate --plan` (except its plan
  file) leave the mirror untouched; unreadable records are reported, never reset.
- **Credentials.** pxd never reads or stores Globus tokens or ssh keys; it calls the `globus`, `ssh` and
  `rsync` commands, which use each user's own credentials.

Operations (recover, reindex, migrate, delete, held-out discipline): [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Claude Code skill

[`skill/pride/SKILL.md`](skill/pride/SKILL.md) is a skill for [Claude Code](https://claude.com/claude-code)
that teaches the agent to use `pxd` instead of ad-hoc downloads. Copy the directory to `~/.claude/skills/pride/`.

## Tests

```bash
python3 -I tests/test_pxdmirror.py                 # core: paths, locks, publication, recovery, delete, migrate, validators
python3 -I tests/test_multiuser.py                 # configuration, modes, other users' files, remote staging, doctor
python3 -m pytest tests                            # the same through pytest
python3 -I tests/test_pxdmirror.py --online        # also downloads two small public PXD000001 files into a temporary root
python3 -I tests/test_pxdmirror.py --root DIR --two-node HOSTA HOSTB   # concurrent catalogue writes from two hosts
```

The offline tests need no network and run in a temporary directory. For `--two-node`, the checkout and
`DIR` must be on a file system both hosts mount, and `ssh HOSTB` must work without a password.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Security issues: [SECURITY.md](SECURITY.md).

## Citation

If you use pxd-mirror in published work, please cite it as described in [CITATION.cff](CITATION.cff).
Please also cite the PRIDE database for the data you use
(<https://www.ebi.ac.uk/pride/markdownpage/citationpage>).

## Licence

MIT, see [LICENSE](LICENSE). Data downloaded with pxd is subject to the terms of the PRIDE Archive and of the
individual data sets.
