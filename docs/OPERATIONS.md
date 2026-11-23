# Operations

## Daily use

```bash
pxd query PXD000001                      # exit 0 complete, 10 partial, 11 absent
pxd get PXD000001 --category RAW --dry-run
pxd get PXD000001 --category RAW
pxd path PXD000001 F063721.dat           # where the file is
pxd stage PXD000001 --files F063721.dat --to /scratch/$USER/task
```

Check the mirror before downloading anything. Do not wrap `pxd get` in a retry loop: it retries within
bounds by itself (`[transfer]`), and a second run continues where the first stopped.

## After a crash or an interrupted run: `pxd recover`

Every publication writes an intent into `<root>/.ops/` before it links the file into place, and removes it
at the end. `pxd recover` goes through leftover intents:

- a publication whose file is in place with the right MD5 is completed (manifest, catalogue, event);
- one whose staged copy is intact is published again;
- anything else is reported as `NEEDS ATTENTION` and left alone;
- Globus tasks are queried and reported, not published: their files are published by the next `get` of
  the same user. Tasks of other users are listed with their owner.

Intents are validated first (accession, paths inside the mirror, MD5); an intent that another process is
working on is skipped. Any member may run `recover`, but it acts only on intents the caller wrote: an intent
names paths (the staging file, the symlink left at a migrated file's old place) that pxd trusts only because
its owner wrote them, and `.ops` is writable for the whole group. Intents are therefore written `640` (group
read, owner write), and recover reports other members' intents with their owner instead of acting on them;
an own intent that is writable for others is not acted on either (`NEEDS ATTENTION`). It exits 1 if
something needs attention.

## The catalogue: `pxd reindex`

`catalog.sqlite` is a projection of the per-data-set `manifest.json` files, the inventory generations and
`.events.jsonl`. If it is missing, damaged or out of date, rebuild it in place:

```bash
pxd reindex           # exit 3 if a record could not be read (it is reported, never reset)
```

`pxd list` falls back to scanning the manifests when the catalogue cannot be read.

## Bringing existing copies into the mirror: `pxd migrate`

```bash
pxd migrate --plan /shared/projects/old-downloads    # read-only scan; writes only <root>/.ops/plan-*.tsv
less <root>/.ops/plan-....tsv                         # class per file: move, duplicate, md5-mismatch, ...
pxd migrate --apply <root>/.ops/plan-....tsv          # moves the "move" rows, leaves a symlink behind
```

Only files whose name, size and MD5 match PRIDE's checksum list for the accession in their path are moved
(by hard link and unlink, so source and mirror must be on the same file system). Local results, second
copies, changed files, hard-linked files and other users' files that are not group read-write stay where
they are. `--no-symlink` skips the symlink at the old location.

`pxd import-legacy [--plan] DIR...` imports converted mzML recorded in a legacy per-data-set archive
(`DIR/manifest.json` with a `derived` section) after their raw files were migrated; each file is checked
against its recorded MD5 and its mzML index is validated.

## Deleting: two steps with a token

```bash
pxd delete PXD000001 --files F063721.dat            # prints the exact list and a confirmation token
pxd delete PXD000001 --files F063721.dat --yes --confirm <token printed by the first step>
pxd delete PXD000001 --dataset                      # every file and conversion of the data set
pxd delete PXD000001 --derived-only                 # conversions only
pxd delete PXD000001 --derived-only --status index_invalid
```

The token is a hash of the list; if the list changed in between (someone added a file), the token no longer
matches and nothing is deleted. Deletion removes files only: the manifest keeps a tombstone (who, when),
the inventory and `.events.jsonl` keep the history. Nothing is moved to a trash: deleted means gone, so
review the list.

## Verifying: `pxd verify PXD`

Re-hashes every recorded file of a data set; mismatches are marked `mismatch` in the manifest (exit 1).

## Converters: `pxd tools`, `pxd convert`

```bash
pxd tools                                            # version, path, sha256 and tree_sha256 of each converter
pxd convert PXD000001 --files RUN01.raw --to mzML     # or --to mzPeak; --tool NAME; --threads N
pxd path PXD000001 RUN01.raw --kind mzML
```

The source is copied to `paths.scratch` and its MD5 checked, the converter runs there, the output is
validated (mzML index offsets, mzPeak zip structure), copied back and published under
`<PXD>/<format>/<recipe>/`. The catalogue records tool version, executable and deployment hashes, argv,
environment, host, timing and validation result. A new tool version or different arguments give a new
recipe, never an overwrite. A converter whose files another user could change is refused (see
"Converter registry" in [CONFIGURATION.md](CONFIGURATION.md)), and so is a staging directory that another
user could replace (`paths.scratch`).

## Permissions: `pxd doctor`

See [PERMISSIONS.md](PERMISSIONS.md). Run `pxd doctor` after setting up a mirror and when a member reports
"Permission denied".

## Held-out discipline

Benchmark and machine-learning work often needs data sets that are not looked at before an evaluation
protocol is frozen. pxd supports this:

- `pxd query` and `pxd get --dry-run` print counts, names and sizes only;
- `pxd get` downloads only the files or categories you name.

Choose held-out files from names and sizes, freeze the list and the protocol, then `get`. Do not open
`project.json`, SDRF files or result files of a held-out data set before the freeze.

## Quotas and capacity

`get` refuses to start when the download (twice that with HTTPS range assembly) plus
`limits.quota_margin_gb` does not fit (CephFS quota realm of the root, otherwise free disk space). Disk-full
and quota errors stop the run immediately and are never retried; already published files stay.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`query`: complete) |
| 1 | some files failed, were locked or not attempted (`doctor`: problems found) |
| 2 | usage or configuration error |
| 3 | operational error, including quota, unreadable records and a busy catalogue |
| 10 / 11 | `query`: partial / absent; `path`: 11 if not found |
