# Contributing

Thank you for helping. Bug reports, documentation fixes and pull requests are welcome.

## Reporting a problem

Open an issue with the command you ran, its output (`pxd ...` prints the reason for every failed file), the
output of `pxd config` and `pxd --version`, the operating system and the file system of the mirror root.
Remove host names, user names and paths you do not want to publish, and any credentials (for example a
token inside a URL or an environment variable). For security problems see
[SECURITY.md](SECURITY.md) instead.

## Development

```bash
git clone https://github.com/KohlbacherLab/PXD-mirror && cd PXD-mirror
bin/pxd --version                     # runs from the checkout
python3 -I tests/test_pxdmirror.py
python3 -I tests/test_multiuser.py
```

Guidelines:

- Python 3.11+, standard library only. New runtime dependencies need a very good reason.
- Keep the invariants in [docs/DESIGN.md](docs/DESIGN.md): every write goes through `Mirror.publish`, nothing
  is overwritten, read paths never write, unreadable records are reported and never reset, every path is
  validated.
- Add an offline test for every fix (temporary roots only, no network; mock the `globus` CLI and `ssh` as
  the existing tests do). Online tests stay opt-in.
- No site-specific values in code, tests or documentation: host names, user names, private paths, endpoint
  ids and keys belong in a site configuration file, not in the repository.
- Document new configuration keys in `src/pxdmirror/config.py` (the schema), `examples/config.toml` and
  `docs/CONFIGURATION.md`, and add a line to `CHANGELOG.md`.

## Pull requests

Keep them focused, describe the problem and how you tested the change, and make sure CI passes. By
contributing you agree that your contribution is licensed under the MIT licence of this project.
