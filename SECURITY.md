# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security > Report a vulnerability** on
<https://github.com/KohlbacherLab/PXD-mirror/security> (private vulnerability reporting). Only the
maintainers see such a report. Do not open a public issue with details.

If that button is missing, open an issue titled "Security contact request" that contains **no details**;
a maintainer will reply with a private channel. We aim to acknowledge reports within a week and will
credit reporters unless they prefer otherwise.

## Scope

pxd-mirror handles untrusted input and shared directories. Relevant problems include:

- **Untrusted downloads and metadata:** file names, inventories or zip archives from PRIDE (or a server
  pretending to be PRIDE) that make pxd write outside the mirror, follow symlinks, overwrite files, execute
  content, or exhaust disk space.
- **Shared directories:** ways for one member of a mirror's group to make pxd, when run by another member,
  read or write outside the mirror, create files or symlinks outside it, or run code other than the
  registered converters.
- **Remote staging:** injection through target paths or file names into the commands run over ssh.
- **Credentials:** any situation in which pxd reads, prints, logs or copies Globus tokens, ssh keys or other
  secrets.

Out of scope: the security of PRIDE, Globus, ssh or the converters themselves; an attacker who already
controls the account running pxd, the directory pxd is installed in or a directory above it (pxd warns
about such directories); and a member of the mirror's group damaging the mirror's *data*, which is
group-writable by design (see "Threat model and known limitations" in [docs/DESIGN.md](docs/DESIGN.md)).
Attacks by such a member on *other members' accounts* through pxd are in scope, and so is robustness
against members' mistakes.

## Supported versions

Security fixes go into the latest release.
