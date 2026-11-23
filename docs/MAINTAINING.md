# Maintaining the repository

## Publishing the repository or a release

1. Tests pass locally and in CI (`python3 -I tests/test_pxdmirror.py`, `python3 -I tests/test_multiuser.py`).
2. Nothing site-specific is tracked: no host or user names, private paths, endpoint or collection ids
   (except the public EMBL-EBI collection), keys or tokens. Site files live outside the repository (see
   [CONFIGURATION.md](CONFIGURATION.md)); `.gitignore` excludes `site/`.
3. The commit author and committer addresses are the ones you want published
   (`git log --format='%an <%ae> | %cn <%ce>'`); fix them before the first push, never by force-pushing a
   published branch.
4. When the GitHub repository is created, enable private vulnerability reporting, which
   [SECURITY.md](../SECURITY.md) and [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) rely on (it is off by default):

   ```bash
   gh api -X PUT repos/KohlbacherLab/PXD-mirror/private-vulnerability-reporting
   gh api repos/KohlbacherLab/PXD-mirror/private-vulnerability-reporting      # {"enabled": true}
   ```

5. Update `CHANGELOG.md`, the version in `src/pxdmirror/__init__.py` and `CITATION.cff`, then tag.

## Updating a shared installation

Copy the new version into the installation without touching `site/` (and any converter tree kept next to
it), keep directories `2755`, files `644` and `bin/pxd` `755`, then run `bin/pxd config` and `bin/pxd doctor`
as a member. After changing a converter deployment, run `bin/pxd tools` and update the pins in
`site/tools.json`.
