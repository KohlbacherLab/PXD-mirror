# Configuration

pxd reads TOML files (Python's built-in `tomllib`), environment variables and command-line options. Nothing is
required except the mirror root. `pxd config` prints every effective value and where it came from;
`pxd config --json` does the same for scripts.

## Precedence

For every key, the first of these that sets it wins:

1. command line: `--root`, `--tools`, and per command `--stage`, `--split`, `--max-attempts`, ...
2. environment: `PXD_<SECTION>_<KEY>` (for example `PXD_GLOBUS_DEST_COLLECTION`, `PXD_LIMITS_QUOTA_MARGIN_GB`),
   plus the short forms `PXD_ROOT`, `PXD_SCRATCH`, `PXD_TOOLS`
3. `--config FILE`, else `$PXD_CONFIG` (typically a site file maintained by whoever runs the mirror)
4. `$XDG_CONFIG_HOME/pxd/config.toml`, default `~/.config/pxd/config.toml` (a personal file)
5. built-in defaults

Files are merged key by key: a site file can set the root and Globus, and a personal file can add a
per-host ssh key for staging.

`bin/pxd` (the wrapper for running from a checkout or a shared installation) sets `PXD_CONFIG` to
`<installation>/site/config.toml` if that file exists and `PXD_CONFIG` is not set. That is how one shared
installation gives every user the same settings without a personal file.

List values in environment variables are separated by spaces or commas (`PXD_TRANSFER_METHODS="ftp https"`),
or given as a JSON array when an element contains spaces (`PXD_GLOBUS_SHARED_PREFIXES='["/shared/My Group"]'`);
booleans accept `1/0`, `true/false`, `yes/no`, `on/off`. When both a short alias and the long form are set
(`PXD_ROOT`, `PXD_PATHS_ROOT`), the long form wins. `[stage.hosts]` can only be set in a file.

Path values expand `~`, `{user}` (login name), `{home}` and `{tmp}` (the system temporary directory, usually
`$TMPDIR` or `/tmp`). A relative `paths.tools` is relative to the file that sets it. An unknown key or section
prints a warning; a value of the wrong type (in a file, `"yes"` is not a boolean and `"3"` not a number), a
negative number or a list element that is not a string stops pxd with exit code 2.

## Keys

A fully commented file is in [`examples/config.toml`](../examples/config.toml).

### `[site]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `"PRIDE mirror"` | heading of the `README.md` that `pxd init` writes into the root |

### `[paths]`

| Key | Default | Env | Meaning |
|---|---|---|---|
| `root` | none (required) | `PXD_ROOT` | the mirror root |
| `scratch` | `{tmp}/pxd-{user}` | `PXD_SCRATCH` | per-user staging for `pxd convert` (created 0700). Use a node-local disk. `convert --stage DIR` overrides it |
| `tools` | `tools.json` next to the config file, else none | `PXD_TOOLS` | converter registry ([`examples/tools.json`](../examples/tools.json)) |
| `forbidden_roots` | `[]` | | directories under which pxd refuses to work (as root) or to stage into, e.g. a backup area |

The layout below the root (`PXDnnnnnn/`, `.locks/`, `.ops/`, `.work/`, `catalog.sqlite`, `.events.jsonl`) is
fixed and cannot be configured.

### `[permissions]`

| Key | Default | Meaning |
|---|---|---|
| `shared` | `true` | `true`: directories 2770 (setgid), files 660, for a group of users. `false`: directories 700, files 600 |
| `group` | `""` | the group the root should have; `pxd init` applies it to the directories it creates, `pxd doctor` reports a mismatch |
| `default_acl` | `true` | `pxd init` sets a default ACL `group::rwx, other::---` on the root and its internal directories where `setfacl` works (only when `shared`) |
| `umask` | `""` | octal umask for the pxd process; empty means `007` when shared, `077` when private |

See [PERMISSIONS.md](PERMISSIONS.md).

### `[limits]`

| Key | Default | Meaning |
|---|---|---|
| `quota_margin_gb` | `50` | `get` refuses unless the download (twice for range assembly) plus this margin fits. On CephFS the quota of the realm containing the root counts, elsewhere the free disk space |
| `lock_wait_s` | `600` | how long to wait for a data-set lock held by another process |

### `[transfer]`

| Key | Default | Meaning |
|---|---|---|
| `methods` | `["globus", "ftp", "https"]` | methods `--method auto` may use. Globus, if listed and set up, always goes first (one batch for all files); FTP and HTTPS follow in the given order |
| `split` | `4` | HTTPS byte ranges per large file (`--split`) |
| `split_min_mb` | `100` | files at least this large are fetched in ranges |
| `max_attempts` | `4` | attempts per method and file (`--max-attempts`) |
| `max_total` | `8` | attempts per file over all methods (`--max-total`) |
| `backoff_s` | `30` | first retry delay, doubling with jitter, capped at 600 s (`--backoff`) |
| `stop_after` | `3` | after this many consecutive failed files the rest are `not_attempted` (`--stop-after`) |

### `[pride]`

| Key | Default |
|---|---|
| `api` | `https://www.ebi.ac.uk/pride/ws/archive/v3` |
| `ftp_base` | `ftp://ftp.pride.ebi.ac.uk/` |
| `https_base` | `https://ftp.pride.ebi.ac.uk/` |

Change these only for a PRIDE mirror or a test server.

### `[globus]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | try Globus first under `--method auto` |
| `cli` | `globus` | the globus CLI, by name on `PATH` or absolute path |
| `source_collection` | `47772002-3e5b-4fd3-b97c-18cee38d6df2` | EMBL-EBI Public Data |
| `dest_collection` | `""` | UUID of the endpoint or collection that writes into the root |
| `kind` | `gcp` | `gcp` (Globus Connect Personal) or `gcs` (Globus Connect Server collection) |
| `path_map` | `[]` | `["/local/prefix", "/collection/prefix"]`; empty means the collection sees local paths unchanged (GCP) |
| `gcp_config_dir` | `~/.globusonline` | GCP configuration directory (GCP's `-dir`) |
| `check_config_paths` | `true` | gcp: the root must be a writable entry of `<gcp_config_dir>/lta/config-paths` |
| `shared_prefixes` | `[]` | roots under these prefixes are on a file system that the GCP host also mounts; elsewhere GCP must run on the node running pxd |
| `wait_hours` | `72` | how long `get` waits for a task (`--globus-hours`) |

How the checks work, and how to get a Globus login: [GLOBUS.md](GLOBUS.md).

### `[stage]`

| Key | Default | Meaning |
|---|---|---|
| `ssh_options` | `["-o", "BatchMode=yes"]` | ssh options for `pxd stage ... --to HOST:PATH` |
| `rsync_options` | `["-rt", "--partial", "--mkpath"]` | rsync options for remote staging (`--mkpath` needs rsync 3.2.3 or newer); pxd always adds `-s` (protect arguments) |
| `hosts` | `{}` | per-host-pattern overrides, see below |

```toml
[stage.hosts."login*.cluster.example.org"]          # shell-style pattern on HOST; the last match wins
ssh_options = ["-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519_cluster", "-o", "IdentitiesOnly=yes"]
```

Host aliases and keys in `~/.ssh/config` work as usual, so per-host options are often unnecessary.

### Environment variables of the predecessor

`PXD_TOOLS_JSON`, `PXD_PRIDE_API`, `PXD_GLOBUS` (CLI path), `PXD_GLOBUS_GCP` (destination) and
`PXD_TEST_HTTPS_PREFIX` still work as aliases of `paths.tools`, `pride.api`, `globus.cli`,
`globus.dest_collection` and `pride.https_base`.

## Examples

### 1. Single user on a laptop

```toml
# ~/.config/pxd/config.toml
[paths]
root = "~/data/pxd"

[permissions]
shared = false              # 700/600, umask 077
```

```bash
pxd init && pxd get PXD000001 --category RAW
```

### 2. Shared group mirror on a cluster file system

One maintainer installs the package in a directory only maintainers can write and puts the site file in
`<installation>/site/config.toml` (or anywhere, with `PXD_CONFIG` set in the group's shell profile):

```toml
[site]
name = "PRIDE mirror of the proteomics group"

[paths]
root = "/shared/proteomics/pxd"
scratch = "/scratch/{user}/pxd"              # node-local disk for conversions
tools = "tools.json"                         # next to this file
forbidden_roots = ["/shared/backup"]

[permissions]
shared = true
group = "proteomics"
default_acl = true
```

The maintainer runs `pxd init` once (with the group's members able to write the parent directory or not, as
the site prefers), then `pxd doctor`. Every member can then `get`, `convert`, `delete` and `migrate`. See
[PERMISSIONS.md](PERMISSIONS.md) for why there is no sticky bit.

### 3. No Globus

The default. To make it explicit, or to prefer HTTPS over FTP:

```toml
[globus]
enabled = false

[transfer]
methods = ["https", "ftp"]
```

### 4. Globus Connect Personal (GCP)

One person runs GCP on a node that sees the root; with a shared file system, any node will do.

```toml
[globus]
enabled = true
kind = "gcp"
dest_collection = "<UUID printed by `globus endpoint local-id`>"
gcp_config_dir = "~/.globusonline"
check_config_paths = true
shared_prefixes = ["/shared/"]               # the root is on a file system the GCP node also mounts
```

Only the user who runs GCP can use it: a personal endpoint writes as its owner and only its owner's Globus
login can submit to it. For everybody else pxd falls back to FTP/HTTPS. Put such a `[globus]` section in the
GCP owner's personal file, or in the site file (the checks then skip Globus for other users with a reason).

### 5. Globus Connect Server (GCS) collection

If the institution runs GCS v5 with a mapped collection that covers the root:

```toml
[globus]
enabled = true
kind = "gcs"
dest_collection = "<collection UUID>"
path_map = ["/shared/proteomics/pxd", "/proteomics/pxd"]     # local root -> path on the collection
```

Every member with an identity mapped on that collection can use it, after a one-time consent
(see [GLOBUS.md](GLOBUS.md)). Files are written by the account the collection maps the user to.

### 6. Read-only consumers

Users who only look things up (`query`, `list`, `path`, `get --dry-run`) need read access to the root and no
configuration beyond the root. Read commands never write. A group with read-only access needs `r-x` on the
directories and `r--` on the files; use `permissions.group` for the writers and grant readers via an
additional ACL entry on the root (`setfacl -m g:readers:rx -d -m g:readers:rx <root>`).

```bash
PXD_ROOT=/shared/proteomics/pxd pxd query PXD000001
```

### 7. Staging to a remote cluster over ssh

```toml
[stage]
ssh_options = ["-o", "BatchMode=yes"]

[stage.hosts."login*.hpc.example.org"]
ssh_options = ["-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519_hpc", "-o", "IdentitiesOnly=yes"]
```

```bash
pxd stage PXD000001 --files F063721.dat --to login1.hpc.example.org:/work/me/pxd
```

The file is copied to a temporary name on the target, checked there with `md5sum`, and moved into place
with `mv -n` (never overwriting a different file). Target paths are quoted, so names with spaces or quotes
are safe. `HOST:~/dir` means `dir` in the remote home. Local stage targets may not lie inside the mirror or
below a forbidden root (symlinks are resolved for this check).

## Python API

```python
from pxdmirror import Config, Mirror
m = Mirror()                                   # Config() from files and environment
m = Mirror(root="/tmp/test-mirror")            # override the root
m = Mirror(config=Config(config_file="site.toml", cli={("paths", "root"): "/x"}))
```

`Mirror(...)` applies the file modes, umask, PRIDE endpoints, Globus and converter settings process-wide.
