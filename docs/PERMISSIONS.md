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
| ~~sticky bit~~ | **never** `+t` | see below |

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

Install the code and the converter binaries where **only maintainers can write** (directories `2755` or
`755`, files `644`, executables `755`), not in a group-writable directory: any member could otherwise
replace a converter or the package that everybody else then runs. Pin converter binaries with `sha256` in
`tools.json`; pxd refuses a binary whose hash changed. The parent directory of the installation matters too:
if it is group-writable without the sticky bit, a member can rename the installation directory away and put
another one in its place, so choose a parent that only maintainers can write.

The mirror data, on the other hand, must be group-writable. Keep the two apart.
