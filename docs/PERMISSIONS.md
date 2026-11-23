# Permissions for a shared mirror

A shared mirror is used by several accounts that belong to one POSIX group. Every member must be able to add
data sets, publish conversions, delete files and data sets, and clean up what an interrupted run of
*another* member left behind. pxd relies on three mechanisms, and deliberately not on a fourth.

## The model

| Mechanism | Setting | Why |
|---|---|---|
| **setgid directories** | mode `2770` (`drwxrws---`) | new files and directories inherit the directory's group instead of the creator's primary group |
| **default ACL** | `default:group::rwx`, `default:other::---` on the root, `.locks`, `.ops`, `.work` | new entries are group-writable even when a member's umask would remove the group's write bit |
| **explicit modes and umask** | files `660`, directories `2770`, process umask `007` | the same result on file systems without ACL support (many ZFS, NFS and local setups) |
| ~~sticky bit~~ | **never** `+t` on mirror directories | see below (above an *installation* it is right: see "Installing the tool for a group") |

`pxd init` sets all of this up for the root and its internal directories (`permissions.group` names the
group, `permissions.default_acl` the ACL). Every directory pxd creates gets `2770`, every file `660`
(`permissions.shared = false` gives `700`/`600` for a private mirror).

The default ACL overrides the umask only in one direction: the effective permissions are the requested
create mode intersected with the ACL. A program that asks for `0600` (for example Python's `tempfile`) still
gets a private file. pxd therefore never publishes a temporary file without setting its mode first, and sets
its own umask (`permissions.umask`, default `007`), which child processes such as `curl` inherit.

## Why not the sticky bit

"Sticky" sounds like "keeps the group", but on Linux the sticky bit on a directory (`chmod +t`, as on `/tmp`)
means the opposite of what a shared mirror needs: **only the owner of a file may delete or rename it**.
With `+t` on mirror directories:

- `pxd delete` fails for every file another member downloaded;
- quarantining a corrupt file, replacing another member's manifest (atomic rename), and cleaning another
  member's partial downloads all fail;
- with `fs.protected_regular=2` (a common kernel default) even opening another member's lock file or the
  shared event log with `O_CREAT` fails.

What keeps the group is the **setgid** bit (`chmod g+s`), which pxd uses. `pxd doctor` reports sticky
directories, and `pxd doctor --fix` removes the bit from directories you own.

## Group membership and umask

- Every user of a shared mirror must be a member of its group (`id -Gn`). `pxd doctor` says if you are not.
- pxd sets its own umask, so a member's shell umask (`022`, `077`) does not matter for files pxd creates.
  Files you copy into the mirror by hand are your responsibility: do not do that, use `pxd migrate`.
- Members need write access to the root directory only through the group; "other" gets nothing.

## Other users' files

pxd treats files owned by another member carefully:

- **Hard links.** Linux with `fs.protected_hardlinks=1` lets you hard-link another user's file only if you
  can read and write it. pxd publishes by hard link (no-overwrite), so:
  - Globus deliveries written by another member's endpoint (mode 640) are copied to a private file first,
    verified, and that copy is published.
  - `pxd migrate` moves another member's file only if it is group read-write, already has the mirror's group
    and is not in a sticky directory; otherwise it is listed as `not-movable` and stays where it is. The
    owner can migrate it, or make it group read-write first.
- **chmod/chown** work only on your own files. pxd changes modes only on entries it created or that you own
  and ignores the harmless failures on others' entries.
- **Lock files** that are not group-writable still work (pxd falls back to a read-only descriptor for
  `flock`).
- **Globus tasks** can be followed only by the user who submitted them. pxd records the owner in each Globus
  intent; other users do not wait for it and fetch the files over FTP/HTTPS.

## Checking and repairing drift

```bash
pxd doctor            # read-only: group, modes, sticky bits, access for others, default ACLs, catalogue access
pxd doctor --fix      # repairs the entries you own: chgrp to the root's group, chmod 2770/660, default ACL
pxd doctor --json
```

`pxd doctor --fix` never touches entries of other users; it lists them with their owners, who run
`pxd doctor --fix` themselves. Typical causes of drift: files copied in by hand, a Globus endpoint writing 640,
an old tool version, or a member whose primary group differs from the mirror's on a file system without
setgid support.

Manual equivalent for a mirror whose files all belong to you (on a multi-user mirror, prefer `pxd doctor --fix`,
which touches only your own entries):

```bash
chgrp -R proteomics /shared/proteomics/pxd
find /shared/proteomics/pxd -type d -exec chmod 2770 {} +
find /shared/proteomics/pxd -type f -exec chmod 660 {} +
setfacl -R -m g::rwx,o::--- -d -m g::rwx,o::--- /shared/proteomics/pxd    # where ACLs are supported
```

## Installing the tool for a group

Whatever every member runs must be writable only by its maintainers; the mirror data, on the other hand,
must be group-writable. Keep the two apart.

- **The package** (`bin/pxd`, `src/`, the site file): directories `2755` or `755`, files `644`, `bin/pxd`
  `755`, no ACL granting the group write access.
- **The converters, all of them:** not only the executable but everything it loads: its deployment prefix
  (OpenMS `bin/` and `lib/`), every shared library the dynamic loader resolves (a conda environment named in
  an `RPATH` counts), and runtimes named in `env` (`DOTNET_ROOT`). Copy them into a maintainer-only tree and
  make the binaries look only there (`LD_LIBRARY_PATH`, or relink an absolute `RPATH` that points elsewhere).
  pxd checks this before every conversion and refuses a tool whose files another user could change; the
  `sha256`, `deployment_sha256` and `tree_sha256` pins in `tools.json` then catch a change by the maintainers
  themselves (see "Converter registry" in [CONFIGURATION.md](CONFIGURATION.md)).
- **Every directory above them:** if one of them is group-writable without the sticky bit, a member can
  rename the installation (or a whole parent) away and put another one in its place, and everybody then
  runs that. Choose parents that only root or the maintainers can write, or ask the administrators to set
  the sticky bit (`chmod +t`) on group-writable parents. Here the sticky bit is right: it stops members
  from renaming entries they do not own. `pxd doctor` and `pxd config` warn about such parents of the
  installation, and `pxd convert` about such parents of a converter.

### Compiled-in paths

Copying a converter is not enough when it has absolute paths compiled in, because they still point at the
original location. If that location (or a directory above it) is group-writable, a member can put their own
files there, and every member's conversion then loads them, although the copied tree, its pins and
`tree_sha256` are unchanged.

OpenMS: `libOpenMS.so` contains the install prefix's `share/OpenMS` (and `share/doc`) and the build tree's
`share/OpenMS`, and tries them before `<prefix>/share/OpenMS` next to the executable. `OPENMS_DATA_PATH` is
only the last fallback, so it cannot redirect a copy. Either build OpenMS with the final install prefix, or
overwrite those strings in the copied `libOpenMS.so` with paths **of the same length** that nobody can
create, such as `/proc/disabled-...` padded to length, so that OpenMS falls back to the copy's own
`share/OpenMS`. Then pin the files OpenMS actually loads (`share/OpenMS/openms_thermo_bridge/managed/`
`ThermoFisher.CommonCore.RawFileReader.dll` and `ThermoWrapperManaged.dll`) in `deployment_files`. Keep the
recipe name with `recipe_deployment` when the computation is unchanged. Remove the original tree, or make
it and its parents writable only by the maintainers, so it cannot be used to plant files either way. Check
the result by tracing a real conversion ([MAINTAINING.md](MAINTAINING.md)).

### Per-user staging

`pxd convert` copies the input into `paths.scratch` (default `{tmp}/pxd-{user}`) and runs the converter
there. pxd creates the directory `0700` and refuses one that is not the caller's, is writable for others,
or sits below a directory through which another user could rename it away: a world-writable `/scratch`
without the sticky bit lets anybody replace `/scratch/<you>` with a directory of their own and swap the
converter's input or output. `/tmp` (mode `1777`) is fine. Ask the administrators for `chmod +t` on a
world-writable scratch file system, and use `/tmp` or `convert --stage DIR` until then.

### Intents

Every write first records an intent in `.ops/`, so that `pxd recover` can finish or report it after a
crash. Intents are written `640` and acted on only by their owner: `.ops` is writable for the whole group,
and an intent names paths (a staging file, the symlink back at a migrated file's old place) that must not be
chosen by somebody else. `pxd doctor` reports an intent that is not `640` and `--fix` leaves it alone:
check its content (the paths it names), then `chmod 640` it so that its owner's `pxd recover` finishes it, or
remove it.
