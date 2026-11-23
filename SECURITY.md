# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: **Security > Report a vulnerability** on
<https://github.com/KohlbacherLab/PXD-mirror> (private vulnerability reporting). Do not open a public issue.
We aim to acknowledge reports within a week and will credit reporters unless they prefer otherwise.

## Scope

pxd-mirror handles untrusted input and shared directories. Relevant problems include:

- **Untrusted downloads and metadata:** file names, inventories or zip archives from PRIDE (or a server
  pretending to be PRIDE) that make pxd write outside the mirror, follow symlinks, overwrite files, execute
  content, or exhaust disk space.
- **Shared directories:** ways for one member of a mirror's group to make pxd, when run by another member,
  read or write outside the mirror, run a different converter binary, or lose or corrupt data.
- **Remote staging:** injection through target paths or file names into the commands run over ssh.
- **Credentials:** any situation in which pxd reads, prints, logs or copies Globus tokens, ssh keys or other
  secrets.

Out of scope: the security of PRIDE, Globus, ssh or the converters themselves; an attacker who already
controls the account running pxd or the directory pxd is installed in; and deliberate attacks by a member of
the mirror's own group, who can delete the mirror anyway (see "Threat model and known limitations" in
[docs/DESIGN.md](docs/DESIGN.md)). Robustness against such a member's mistakes is in scope.

## Supported versions

Security fixes go into the latest release.
