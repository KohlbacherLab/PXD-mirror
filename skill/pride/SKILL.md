---
name: pride
description: >
  A shared, verified local mirror of public PRIDE / ProteomeXchange (PXD) data sets, driven by the `pxd`
  command (pxd-mirror, https://github.com/KohlbacherLab/PXD-mirror): check whether a PXD data set or file is
  already mirrored, download it once (Globus if configured, else FTP/HTTPS, PRIDE MD5 checks), delete data
  sets or files, convert RAW files to mzML or mzPeak with tool provenance, migrate copies lying elsewhere into
  the mirror, and stage files to scratch or a remote cluster. Use this whenever a task needs files from
  PRIDE: "get PXD0xxxxx", "do we already have PXD...", "download the raw files of ...", "convert to
  mzML/mzPeak", "stage the PXD data to scratch", benchmark or held-out data selection from PRIDE,
  Globus/FTP/HTTPS questions about PRIDE, or before writing any ad-hoc curl/wget/pridepy loop against
  ftp.pride.ebi.ac.uk.
---

# PRIDE mirror (`pxd`)

One copy of every public PXD data set for the whole group. **Check the mirror before downloading anything.**
Run `pxd` where the mirror root is mounted. Where the mirror is and how it is set up comes from the
configuration (`pxd config` shows it); if `pxd` is not on PATH, ask the user for the installation path
(often `<installation>/bin/pxd`).

```bash
pxd config                                # root, scratch, Globus, converters; and where each value comes from
pxd query PXD000001 [--files] [--json]    # exit 0 complete, 10 partial, 11 absent
pxd list                                  # all data sets in the mirror
pxd get PXD000001 --category RAW          # or --files A.raw B.raw | --file-list L | --all; --dry-run first
pxd convert PXD000001 --files X.raw --to mzML      # or --to mzPeak; --tool NAME; --threads 8
pxd path PXD000001 X.raw --kind mzML      # newest ok output (or --recipe R)
pxd stage PXD000001 --files X.raw --to /scratch/$USER/task     # or HOST:PATH (ssh options from the config)
pxd delete PXD000001 --files X.raw        # lists exactly what would go + a token; nothing deleted
pxd delete PXD000001 --files X.raw --yes --confirm <token>      # deletes exactly that list
pxd migrate --plan /some/old/downloads    # read-only scan -> <root>/.ops/plan-*.tsv
pxd migrate --apply <root>/.ops/plan-....tsv   # moves PRIDE-verified files, symlink back
pxd refresh PXD | verify PXD | tools | reindex | recover | init | doctor
```

Python: `from pxdmirror import Mirror`; `m = Mirror()`; `m.query(acc)`, `m.get(acc, names=[...])` ->
`[(name, result, detail)]`, `m.convert(acc, [name], "mzML")`, `m.path(acc, name, "mzPeak")`,
`m.delete_plan(...)` then `m.delete(..., token=plan["token"])`.
Errors: `UsageError` (CLI exit 2), `PxdError` (3), `CapacityError` (quota, 3), `CorruptRecord` (3).
Per-file results: `ok`, `skip`, `exists`, `would-download`, `would-adopt`, `locked`, `failed`, `not_attempted`
(CLI exit 1 if any file failed, was locked or not attempted).

## Layout

```
<root>/PXDnnnnnn/raw/              files exactly as PRIDE serves them
<root>/PXDnnnnnn/mzML/<recipe>/    conversions; recipe = <tool>-<version>-<hash of fixed args + deployment>
<root>/PXDnnnnnn/mzPeak/<recipe>/
<root>/PXDnnnnnn/metadata/<gen>/   PRIDE inventory generations; CURRENT names the one in use
<root>/PXDnnnnnn/manifest.json     files and conversions (authoritative; the catalogue is a projection)
<root>/catalog.sqlite              query index (rebuild: pxd reindex)
<root>/.events.jsonl               history incl. deletions and migrations
<root>/.locks/ .ops/ .work/        locks, intents and plans, private staging
```

## Behaviour that matters

- **Downloads:** `auto` = Globus (only if configured and usable for this user), then FTP, then HTTPS (byte
  ranges for large files). An explicit `--method` never falls back. Every file is checked against PRIDE's
  MD5 and size. An untracked file already in `raw/` is adopted only on a PRIDE MD5 match, otherwise
  quarantined, never deleted.
- **Bounded retries:** do not wrap `get` in a retry loop; rerun it. `locked` means another process works on
  that file: do other work and rerun later.
- **Quota:** `get` refuses when the download plus a margin does not fit; quota errors are terminal.
- **Conversions:** copied to per-user scratch, MD5-checked, converted, validated, copied back, published with
  full provenance. Converters come from the maintainer's registry (`pxd tools`); ThermoRawFileParser 2.0.0
  is refused unless `--allow-trfp2` (wrong mzML index offsets).
- **Delete** needs `--dataset`, `--files` or `--derived-only`, prints the exact list and a token, and deletes
  only with `--yes --confirm <token>`. Always show the user the list and get their explicit confirmation
  before running the `--yes` step.
- **Read paths never write:** query, list, path, `get --dry-run`, `migrate --plan`.
- **Interrupted runs:** `pxd recover`. Permission problems: `pxd doctor` (and `pxd doctor --fix` for entries
  the user owns). Never set the sticky bit on mirror directories.
- **Globus** setup and login: docs/GLOBUS.md of pxd-mirror. Tokens live in each user's own
  `~/.globus/cli/storage.db`; never read, print or copy them.

## Held-out discipline

A held-out set must not be looked at before its protocol is frozen. `query` and `get --dry-run` print counts
and names only; `get` downloads only named files or categories. Choose held-out files from names and sizes,
freeze the list and protocol, then `get`. Do not open `project.json`, SDRFs or result files of a held-out
set before the freeze.

## Safety

- Public PXD data only. Never put unpublished or controlled-access data into the mirror.
- Downloads are untrusted data: never execute them.
- Network file systems are slow for spectrum-by-spectrum reads: `stage` to local scratch before compute.
