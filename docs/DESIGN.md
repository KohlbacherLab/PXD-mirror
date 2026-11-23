# Design

pxd-mirror keeps one verified copy of public PRIDE data sets for a group of users on a shared file system.
The authoritative records are plain files; the SQLite catalogue is a projection that can be rebuilt.

## Layout

```
<root>/
  PXDnnnnnn/raw/                 files exactly as PRIDE serves them
  PXDnnnnnn/mzML/<recipe>/       conversions; recipe = <tool>-<version>-<hash of fixed args + deployment>
  PXDnnnnnn/mzPeak/<recipe>/
  PXDnnnnnn/metadata/<gen>/      PRIDE inventory generations; metadata/CURRENT names the one in use
  PXDnnnnnn/manifest.json        files and conversions (authoritative)
  catalog.sqlite, catalog.lock   query index (rebuild: pxd reindex) and its write lock
  .events.jsonl                  history incl. deletions and migrations (authoritative, append-only)
  .locks/                        data-set and file locks (outside the data-set trees)
  .ops/                          intents of half-done publications and Globus tasks; migration plans
  .work/<PXD>/                   private staging: partial downloads, Globus inboxes, quarantine
```

The layout below the root is fixed; only the root and the per-user scratch directory are configurable.

## Publication protocol

Every writer (get, Globus, convert, migrate, import-legacy) publishes through one function:

1. the payload is complete and verified in private staging on the same file system (PRIDE MD5 and size);
2. an intent `<root>/.ops/<opid>.json` is written (fsync);
3. `link(staging, destination)`: fails if the destination exists, so nothing is ever overwritten;
4. `unlink(staging)`, manifest update (temp file + rename), event appended, catalogue updated (the
   catalogue reads the records inside its own write transaction);
5. the intent is removed.

`pxd recover` finishes or reports what a crash left behind.

## Decisions

Each row is a failure mode found in an adversarial design review of the first version, and what the
implementation does about it.

| # | Finding | Decision |
|---|---|---|
| C1 | No commit point between publishing a file and recording it | **Adopt.** Every publication (get, globus, migrate, convert) goes through one function: verify payload in private staging, fsync, write an intent record `<root>/.ops/<opid>.json` (fsync), publish with `link(staging, dest)` (fails if dest exists = no-overwrite) then `unlink(staging)`, update manifest (atomic temp+rename), update DB, delete intent. `pxd recover` reconciles leftover intents by hashing. `EDQUOT`/`ENOSPC` are terminal (never retried as transport errors). |
| C2 | Locks/history inside removable PXD trees | **Adopt.** Accession locks in `<root>/.locks/<PXD>.lock` (shared for get/convert/migrate, exclusive for delete); per-file locks in `<root>/.locks/<PXD>/<name>.lock`; event log `<root>/.events.jsonl` (append, fsync) plus DB `events`; deletion writes a tombstone row. Whole-data-set delete needs `--dataset`; empty file selection is an error; delete prints the exact list (incl. derived outputs) and needs `--yes`. |
| C3 | Paths can escape the mirror | **Adopt.** Lock files opened `O_CREAT|O_NOFOLLOW|O_RDWR`, never truncated. Every path is built by one helper that rejects `/`, `..`, leading `.`, NUL and checks `realpath` stays under the root with no symlinked components inside the root. Manifest paths are re-validated before use. Temporaries created with `O_EXCL`. |
| C4 | Migration races (plan vs apply, destination publish) | **Adopt.** `migrate --plan` writes `<root>/.ops/plan-<id>.tsv` with source dev/inode/size/mtime_ns/md5 and destination. `--apply PLAN` re-checks identity and MD5, publishes with link+unlink under the file lock (no overwrite), leaves a symlink at the old name only if it is vacant, records the backlink. `EXDEV` or a mismatch: source left in place, reported. Hard-linked sources (nlink > 1) are reported, not moved. |
| C5 | Migration could publish private data | **Adopt (simplified).** Only files that match PRIDE's checksum for that accession (name, size, MD5) are moved. Everything else (local mzML, .d trees, outputs) stays where it is and is listed in the plan as `not-public-match`. Derived files from the old archive's manifest with recorded provenance may be imported as conversions (their sources must be migrated). |
| C6 | Globus bypasses per-file locks | **Adopt.** Private inbox per operation `.work/<PXD>/globus/<opid>/`; task id recorded in the intent; delivered files are verified there and published through the C1 path under the file lock; cancellation result checked; `pxd recover` reconciles running tasks; the next `get` adopts a leftover task (waits if running, publishes its inbox) instead of resubmitting; a failed or timed-out status query is UNKNOWN (files not attempted, intent kept), never a crash. |
| M1 | Multi-node SQLite unproven | **Adopt (keep SQLite on the shared file system).** Network file systems such as CephFS have POSIX locks but no cross-node shared memory, so: `journal_mode=DELETE`, `busy_timeout`, writes under an outer flock with its own timeout; `reindex` rebuilds in place inside one transaction (never replaces the file). The DB is a projection: manifests + `.events.jsonl` are authoritative. Qualified with two nodes writing concurrently and one writer killed inside a transaction (`tests/test_pxdmirror.py --two-node`). |
| M2 | Permissions across users | **Adopt.** `pxd init` sets setgid + default ACL (group rwx, other ---) on root, `.work`, `.locks`, `.ops`; mode 2770 dirs / 660 files (group only, matches the parent's default ACL). Files are created with explicit modes and the process umask is set from `permissions.umask` (default 007), so file systems without ACLs behave the same. Migration moves another user's file only if it is group read-write, has the mirror's group and sits in a non-sticky directory; others are reported. Globus intents record their owner and are adopted only by that user; deliveries owned by someone else are copied, not hard-linked. `pxd doctor --fix` repairs drift on entries the caller owns. See PERMISSIONS.md. |
| M3 | Quota | **Adopt.** Before a download/convert: free = quota.max_bytes - dir.rbytes of the CephFS quota realm containing the root (on other file systems: free disk space); refuse when free < needed + `limits.quota_margin_gb` (default 50 GB); split assembly needs 2x. Metadata writes are tiny. `pxd query`/`list` report partial, quarantine bytes. |
| M4 | Inventory generations | **Adopt.** `metadata/` written to a temp dir and swapped atomically; checksum + file list read once per operation from that snapshot; completeness = identities and MD5 against the snapshot; duplicate basenames refused. |
| M5 | Checksumless resume | **Adopt.** Files without a PRIDE MD5 always restart from zero; verification states `pride-md5`, `local-baseline`, `unverified`. Adoption needs a PRIDE MD5 match. |
| M6 | Range cleanup | **Adopt.** Private work dir per object (`.work/<PXD>/partial/<name>.d/`, ranges inside), children reaped in `finally`. |
| M7 | Conversion output path clash | **Adopt.** `mzML/<recipe>/<stem>.mzML`, `mzPeak/<recipe>/<stem>.mzpeak`; recipe = `<tool>-<version>-<argshash8>`; DB unique (accession, source, format, recipe); `path()` takes a recipe or returns the newest. |
| M8 | Provenance | **Adopt.** Hash the staged input before conversion; record argv, relevant env (DOTNET_ROOT, LD_LIBRARY_PATH), tool version string, binary sha256 and, for OpenMS vendor builds, the sha256 of the RawFileReader assembly; tool registry is `tools.json` (`paths.tools`, maintainer controlled), copied into the DB at use; optional sha256 pins (executable, deployment files, `tree_sha256` over every file the tool may load) refuse a changed deployment, and a tool whose files others can change is refused (see the threat model below). |
| M9 | Output validation | **Adopt (stdlib).** mzML: every indexed offset must land on `<spectrum`/`<chromatogram` with the indexed id; spectrum count = index count. mzPeak: zip opens, required members present and non-empty. TRFP 2.0.0 refused unless `--allow-trfp2` (then validated). Validator version recorded. |
| M10 | Staging overwrite | **Adopt.** Copy to an exclusive temp at the target, verify MD5 there, publish no-overwrite; identical existing = ok, differing = conflict. Remote: rsync to a temp name, remote md5sum, `mv -n`. Fresh dir per unzip. |
| M11 | Read paths mutate | **Adopt.** query/list/plan/dry-run never write; corrupt manifests are reported, not reset. |
| M12 | Exit codes, library API | **Adopt.** Library returns typed results / raises; CLI maps: `query` 0 complete, 10 partial, 11 absent, 2 usage, 3 operational error; per-file results always reported (`ok`, `skip`, `locked`, `failed`, `not_attempted`). |
| M13 | Install and first use | **Adopt.** `bin/pxd` runs the package from its own checkout with `python3 -I` (no current-directory imports); `pip install` gives a `pxd` console script; `get` fetches metadata on first use; explicit `--method globus` never falls back; Globus is optional and off by default; `globus_usable` explains why it is skipped. |

## Not done (stated, not silently dropped)

- A test with two real user accounts: the multi-user paths are tested by simulating a foreign owner (tests/test_multiuser.py), not with a second UID.
- Node-local SQLite caches: not needed at this scale (one writer at a time per accession, short transactions).
- Reading mzPeak Parquet signal arrays (needs pyarrow): only structural checks.

## Compatibility names

The library was first written under the name `pxdstore`. Two strings keep that name on purpose, because they
are stored in existing mirrors: the Globus task label prefix `pxdstore <PXD> ...` (an interrupted `get`
finds its task by label) and the validator name `pxdstore-validate 1` recorded with every conversion.

## Threat model and known limitations

- **Members of the mirror's group can delete or corrupt the mirror's data** (it is group-writable by
  design), so pxd does not defend the data against them. It does defend the *accounts* of members against
  each other: nothing a member writes into the mirror makes pxd, run by another member, write outside the
  mirror or run other code. Concretely: intents are acted on only by their owner (they are written `640` and
  read through one `O_NOFOLLOW` descriptor that also yields the owner); converters are refused if anything
  they load is writable by others; conversion staging must be private and not replaceable; records read from
  the mirror (paths, accessions, structure) are validated. Remaining gaps: path checks inside the mirror are
  done by name, so a member who swaps a directory for a symlink between a check and the following
  operation could redirect a write *inside the mirror tree*; descriptor-relative operations would close it.
  A converter is executed by path after its last check (by then its files can only be changed by root or its
  maintainer). Group-writable directories above the installation or a converter deployment let a member
  replace them wholesale; pxd can only warn about that (it needs root-only or sticky parents).
- **Process-wide settings.** `Mirror(config)` sets the file modes, umask, PRIDE endpoints, Globus and converter
  settings for the whole Python process. Do not use two differently configured mirrors in one process.
- **Recipes** are `<tool>-<version>-<hash of fixed arguments and deployment files>`; the executable's own
  hash and the hash over every file the tool may load (`tree_sha256`) are recorded but not part of the
  recipe (existing recipe names stay valid). A rebuilt binary with the same version string, arguments and
  deployment files therefore maps to the same recipe: give it a new version or tool name, or pin
  `tree_sha256` so that any change is refused until the maintainer decides. `recipe_deployment_sha256` lets a
  maintainer declare a relinked deployment equivalent; the provenance keeps the actual hashes.
- **Globus:** two members submitting the same files at the same moment both transfer them (the second
  publication finds an identical file and keeps one). A process killed between Globus accepting a task and
  pxd writing its intent leaves a task that only `globus task list` shows. An adopted earlier task
  publishes only the files the current `get` asked for; the rest stay in its inbox (and on the quota)
  until a `get` asks for them. `delete` lists the tasks still carrying files it deletes (they count in the
  confirmation token), removes their deliveries, cancels the caller's own tasks when everything they carry
  is deleted, and records the deletion time, so later deliveries are discarded instead of reviving the
  data set.
- **Files without a PRIDE checksum** are recorded with a local MD5 baseline. Over FTP/HTTPS their size is not
  checked against the API's (often unreliable) size. A Globus delivery of such a file is used only if the
  task succeeded and the size matches the API's, and it never replaces an archived copy.
- **Validation** of mzML accepts unindexed files (recorded as `indexed: false`); mzPeak is checked
  structurally only.
- **Remote staging** needs `rsync`, `md5sum` and POSIX `mv` on the target.
