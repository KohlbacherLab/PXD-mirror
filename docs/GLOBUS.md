# Globus (optional fast path)

With `--method auto`, `pxd get` first submits one Globus batch transfer for the files it needs, then fetches
whatever is still missing over FTP and then HTTPS. Globus is optional and **off by default**. When it is not
usable, pxd logs `globus skipped: <reason>; using FTP/HTTPS` and carries on. `--method globus` uses Globus
only and stops with exit code 2 and the reason if it is not usable.

- **Source:** the public collection *EMBL-EBI Public Data*, `47772002-3e5b-4fd3-b97c-18cee38d6df2`
  (`globus.source_collection`). It is a Globus Connect Server v5 guest collection that any Globus user can
  read without an extra consent.
- **Destination:** a Globus endpoint or collection that can write into the mirror root
  (`globus.dest_collection`): either a Globus Connect Personal (GCP) endpoint that you run yourself, or your
  institution's Globus Connect Server (GCS) collection.

pxd drives the `globus` command-line client as a subprocess. It never reads, copies or stores Globus tokens
itself: authentication is whatever `globus login` stored for the user who runs `pxd`.

## 1. Install the Globus CLI (every user who runs `pxd get`)

```bash
pipx install globus-cli          # recommended by Globus; update with: pipx upgrade globus-cli
# or, inside a virtual environment:  pip install globus-cli
globus version
```

Globus advises against `pip install --user` and `sudo pip install`. If `globus` is not on your `PATH`, set
`globus.cli` to its absolute path (or `PXD_GLOBUS_CLI=/path/to/globus`).

## 2. Log in (this creates your token)

On a computer with a browser:

```bash
globus login
```

On a remote machine (an ssh session on a cluster node):

```bash
globus login --no-local-server
```

The CLI prints a URL. Open it in any browser, sign in with your institutional identity, allow the access the
Globus CLI asks for, copy the authorization code shown at the end, and paste it into the terminal. The CLI
chooses this flow by itself when it detects a remote session.

Check the login:

```bash
globus whoami                        # your identity
globus whoami --linked-identities
globus session show                  # identities in this session and when they authenticated
```

### Where the token is stored

The Globus CLI keeps your refresh and access tokens in an SQLite file in your home directory:

| Operating system | Token file |
|---|---|
| Linux, macOS | `~/.globus/cli/storage.db` |
| Windows | `%LOCALAPPDATA%\globus\cli\storage.db` |

(This location comes from the globus-cli source, `globus_cli/login_manager/storage.py`, version 3.x; the
Globus documentation does not state it.)

- The file is created readable by you only (mode 600). Tighten its directory too: `chmod 700 ~/.globus`.
- Tokens are **per user**. Every person logs in themselves. Never share the file, never copy it into the
  mirror, into a site configuration or into a repository.
- If your home directory is on a shared file system, all nodes see the same login.
- `GLOBUS_PROFILE=<name>` keeps separate logins in the same file (one per profile).
- `pxd config` shows whether the file exists, never its content.

Log out (removes the local tokens and revokes them at Globus):

```bash
globus logout
```

If a token was lost locally (for example a deleted home directory), revoke it at
<https://auth.globus.org/consents>.

Unattended use (a service account): the CLI also logs in as a registered Globus confidential client when
`GLOBUS_CLI_CLIENT_ID` and `GLOBUS_CLI_CLIENT_SECRET` are set. Keep the secret out of repositories and out of
files other people can read.

## 3a. Destination option A: Globus Connect Personal (GCP) on Linux

Run **one** GCP process on a node that sees the mirror root. If the root is on a shared file system, any node
that mounts it will do; list that file system in `globus.shared_prefixes`.

```bash
mkdir -p ~/opt && cd ~/opt
wget https://downloads.globus.org/globus-connect-personal/linux/stable/globusconnectpersonal-latest.tgz
tar xzf globusconnectpersonal-latest.tgz
cd globusconnectpersonal-*/
```

Register the endpoint, in one of two ways:

- interactively: `./globusconnectpersonal -setup` (text prompts and a login URL when there is no display), or
- with a setup key from the CLI:

  ```bash
  globus gcp create mapped "my-pxd-mirror"     # prints the new endpoint id and a setup key
  ./globusconnectpersonal -setup --setup-key <KEY>
  ```

  (The Globus web app offers the same under Collections, Globus Connect Personal.) Treat the setup key as a
  secret: do not keep it in a file, or delete that file once the setup has worked.

Expose the mirror: edit `~/.globusonline/lta/config-paths`. It is a CSV file without a header, one line per
path, `<path>,<sharing 0|1>,<read-write 0|1>`. Keep sharing at 0 and make the mirror read-write:

```
~/,0,1
/shared/proteomics/pxd/,0,1
```

`~` means the home of the user running GCP. File-system permissions still apply: GCP writes as the user who
runs it, so that user must be a member of the mirror's group. pxd reads this file (`globus.gcp_config_dir`,
`globus.check_config_paths`) and refuses a root that is missing or read-only there. Starting GCP with
`-restrict-paths` overrides config-paths; pxd does not see that option.

Start GCP in the background (survives logging out, no tmux or screen needed):

```bash
nohup setsid ./globusconnectpersonal -start > ~/.globusonline/gcp.log 2>&1 < /dev/null &
./globusconnectpersonal -status
```

After editing config-paths, restart GCP, but only while no transfer is running
(`globus task list --filter-status ACTIVE`):

```bash
./globusconnectpersonal -stop
nohup setsid ./globusconnectpersonal -start > ~/.globusonline/gcp.log 2>&1 < /dev/null &
```

A systemd user unit is an alternative (`ExecStart=%h/opt/globusconnectpersonal-X.Y.Z/globusconnectpersonal -start`,
then `systemctl --user enable --now globusconnectpersonal`; `sudo loginctl enable-linger $USER` keeps it running
after logout).

The endpoint id for `globus.dest_collection`: `globus endpoint local-id` on that node, or the output of
`globus gcp create mapped`. Check it with
`globus endpoint show <id> -F json --jmespath gcp_connected` (prints `true`).

GCP keeps its configuration and a private host certificate in `~/.globusonline/`; keep that directory private.
With `-dir <path>` (before `-setup` and on every start) GCP uses another configuration directory; set
`globus.gcp_config_dir` to the same path. To update GCP: stop it, unpack the new version, start it again. To
remove it: `-stop`, then `rm -r ~/.globusonline`.

**Multi-user note.** A GCP endpoint is personal. It writes files as its owner (mode 640 regardless of the
directory's default ACL) and only its owner's Globus login can submit transfers to it or see their status.
pxd therefore:

- uses it only for the user whose `gcp_config_dir` lists the root (others fall back to FTP/HTTPS);
- records the owner in each Globus intent and lets only that user adopt an interrupted task; another user's
  `get` fetches the same files over FTP/HTTPS instead of waiting;
- copies (instead of hard-linking) a delivered file that belongs to someone else before publishing it.

If several people should use Globus, use a GCS collection (option B).

## 3b. Destination option B: a Globus Connect Server (GCS) collection

If your site runs GCS v5 with a mapped collection covering the mirror's file system, ask the administrators
for the collection id and for the collection path that corresponds to the mirror root, then set:

```toml
[globus]
enabled = true
kind = "gcs"
dest_collection = "<collection UUID>"
path_map = ["/local/path/of/the/root", "/collection/path/of/the/root"]
```

The first access needs a one-time *data_access* consent. pxd probes the collection with
`globus ls <collection>:<path>` and, if consent is missing, tells you to run the command the CLI prints, of
the form

```bash
globus session consent 'urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/scopes/<COLLECTION_ID>/data_access]'
```

(or `globus login --gcs <ENDPOINT_ID>:<COLLECTION_ID>`). Test with `globus ls <COLLECTION_ID>:<collection path>`.

## 4. What pxd checks and runs

`globus_usable` (in `src/pxdmirror/transfer.py`) returns a reason, and Globus is skipped, unless all of the
following hold:

| Check | gcp | gcs |
|---|---|---|
| `globus.enabled` is true and `"globus"` is in `transfer.methods` | yes | yes |
| `globus.dest_collection` is set | yes | yes |
| the CLI (`globus.cli`) is found | yes | yes |
| the root is a read-write entry of `<gcp_config_dir>/lta/config-paths` (if `check_config_paths`) | yes | |
| the root is under `shared_prefixes`, or a GCP process of this user runs on this node | yes | |
| `globus endpoint show <dest> --jmespath gcp_connected` prints `True` | yes | |
| `globus ls <dest>:<mapped root>` succeeds | | yes |

Commands used: `globus transfer <source> <dest> --batch - --sync-level checksum --label "pxdstore <PXD> <n> files <op>"`,
`globus task show`, `globus task wait`, `globus task list --filter-label` (to find a task whose submission
timed out) and `globus task cancel`. Each task's files arrive in a private inbox
`<root>/.work/<PXD>/globus/<op>/`; they are checked against PRIDE's MD5 and size and then published like any
other download. A task left by an interrupted `get` is recorded in `<root>/.ops/globus-*.json`; the next `get`
of the same files by the same user waits for it or publishes what it delivered; `pxd recover` reports its
status.

## 5. Test

```bash
pxd config | grep globus
pxd --root /tmp/pxd-test init
pxd --root /tmp/pxd-test get PXD000001 --files F063721.dat --method globus
```

For GCP the test root must be in config-paths too (or use a directory below an existing entry).
