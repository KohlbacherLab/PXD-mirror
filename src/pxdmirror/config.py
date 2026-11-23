"""Configuration: TOML files, environment variables and command-line overrides.

Precedence, per key (highest first):

  1. command line (``--root``, ``--scratch``, ``--tools``, ...)
  2. environment: ``PXD_<SECTION>_<KEY>`` (e.g. ``PXD_GLOBUS_DEST_COLLECTION``) and the short aliases
     ``PXD_ROOT``, ``PXD_SCRATCH``, ``PXD_TOOLS``
  3. ``--config FILE`` or ``$PXD_CONFIG`` (a site or project file)
  4. ``$XDG_CONFIG_HOME/pxd/config.toml`` (default ``~/.config/pxd/config.toml``)
  5. built-in defaults (generic and safe: no root, Globus off)

Strings expand ``~``, ``{user}``, ``{home}`` and ``{tmp}`` (the system temporary directory) in path keys.
Unknown keys produce a warning; a value of the wrong type is a usage error.
"""
import fnmatch, getpass, json, math, os, tempfile

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    tomllib = None

from .util import UsageError

EBI_PUBLIC = "47772002-3e5b-4fd3-b97c-18cee38d6df2"        # Globus collection "EMBL-EBI Public Data"

# section -> key -> (default, type, is_path, help)
SCHEMA = {
    "site": {
        "name": ("PRIDE mirror", str, False, "name shown in the mirror README and in pxd config"),
    },
    "paths": {
        "root": ("", str, True, "mirror root directory (required)"),
        "scratch": ("{tmp}/pxd-{user}", str, True, "per-user staging for conversions"),
        "tools": ("", str, True, "converter registry (tools.json); empty: tools.json next to the config file"),
        "forbidden_roots": ([], list, True, "directories under which pxd refuses to work or stage"),
    },
    "permissions": {
        "shared": (True, bool, False, "true: group-shared mirror (dirs 2770, files 660); false: private (700/600)"),
        "group": ("", str, False, "expected group of the mirror root (pxd doctor warns on a mismatch)"),
        "default_acl": (True, bool, False, "pxd init sets a default ACL (group rwx, other none) if setfacl exists"),
        "umask": ("", str, False, "octal umask for the pxd process; empty: 007 when shared, 077 when private"),
    },
    "limits": {
        "quota_margin_gb": (50.0, float, False, "free space kept in reserve when downloading"),
        "lock_wait_s": (600.0, float, False, "how long to wait for a busy data-set lock"),
    },
    "transfer": {
        "methods": (["globus", "ftp", "https"], list, False, "order tried by --method auto"),
        "split": (4, int, False, "HTTPS byte ranges per large file"),
        "split_min_mb": (100.0, float, False, "files at least this large are fetched in ranges"),
        "max_attempts": (4, int, False, "attempts per method and file"),
        "max_total": (8, int, False, "attempts per file over all methods"),
        "backoff_s": (30.0, float, False, "first retry delay (doubles, jittered, capped at 600 s)"),
        "stop_after": (3, int, False, "consecutive failed files before the rest are not attempted"),
    },
    "pride": {
        "api": ("https://www.ebi.ac.uk/pride/ws/archive/v3", str, False, "PRIDE Archive REST API"),
        "ftp_base": ("ftp://ftp.pride.ebi.ac.uk/", str, False, "prefix of PRIDE FTP locations"),
        "https_base": ("https://ftp.pride.ebi.ac.uk/", str, False, "HTTPS equivalent of ftp_base"),
    },
    "globus": {
        "enabled": (False, bool, False, "try Globus first (needs dest_collection)"),
        "cli": ("globus", str, True, "globus CLI (name on PATH or absolute path)"),
        "source_collection": (EBI_PUBLIC, str, False, "source collection (EMBL-EBI Public Data)"),
        "dest_collection": ("", str, False, "destination endpoint/collection UUID that can write into the root"),
        "kind": ("gcp", str, False, "gcp (Globus Connect Personal) or gcs (Globus Connect Server collection)"),
        "path_map": ([], list, False, "[local prefix, collection prefix]; empty: identity (typical for GCP)"),
        "gcp_config_dir": ("~/.globusonline", str, True, "GCP configuration directory (-dir); gcp only"),
        "check_config_paths": (True, bool, False, "gcp only: require the root in <gcp_config_dir>/lta/config-paths"),
        "shared_prefixes": ([], list, True, "roots under these prefixes are on a file system the GCP host sees"),
        "wait_hours": (72.0, float, False, "how long get waits for a Globus task"),
    },
    "stage": {
        "ssh_options": (["-o", "BatchMode=yes"], list, False, "ssh options for HOST:PATH targets"),
        "rsync_options": (["-rt", "--partial", "--mkpath"], list, False, "rsync options for HOST:PATH targets"),
        "hosts": ({}, dict, False, "per-host-pattern overrides: [stage.hosts.\"pattern\"] ssh_options = [...]"),
    },
}

ENV_ALIASES = {
    "PXD_ROOT": ("paths", "root"),
    "PXD_SCRATCH": ("paths", "scratch"),
    "PXD_TOOLS": ("paths", "tools"),
    # deprecated names of the predecessor tool
    "PXD_TOOLS_JSON": ("paths", "tools"),
    "PXD_PRIDE_API": ("pride", "api"),
    "PXD_GLOBUS": ("globus", "cli"),
    "PXD_GLOBUS_GCP": ("globus", "dest_collection"),
    "PXD_TEST_HTTPS_PREFIX": ("pride", "https_base"),
}
DEPRECATED = {"PXD_TOOLS_JSON", "PXD_PRIDE_API", "PXD_GLOBUS", "PXD_GLOBUS_GCP", "PXD_TEST_HTTPS_PREFIX"}


def user():
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid())


def expand(s):
    if not isinstance(s, str):
        return s
    s = s.replace("{user}", user()).replace("{home}", os.path.expanduser("~"))
    if "{tmp}" in s:                       # gettempdir() probes the file system: only when needed
        s = s.replace("{tmp}", tempfile.gettempdir())
    return os.path.expanduser(s)


def user_config_path(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pxd", "config.toml")


def _coerce(sec, key, val, origin):
    """Check (and for environment strings: convert) one value. TOML values must already have the right type."""
    default, typ, _, _ = SCHEMA[sec][key]
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if origin.startswith("env") and isinstance(val, str):
        if typ is bool:
            if val.lower() in ("1", "true", "yes", "on"):
                return True
            if val.lower() in ("0", "false", "no", "off", ""):
                return False
        elif typ in (int, float):
            try:
                val = typ(val)
            except ValueError:
                pass
        elif typ is list:                              # a JSON array, or whitespace/comma-separated words
            if val.strip().startswith("["):
                try:
                    val = json.loads(val)
                except ValueError:
                    raise UsageError(f"{origin}: not a JSON array: {val!r}")
            else:
                val = [x for x in val.replace(",", " ").split()]
    if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
        raise UsageError(f"{origin}: {sec}.{key} must be {typ.__name__}, not {type(val).__name__} ({val!r})")
    if typ is float and not math.isfinite(val):
        raise UsageError(f"{origin}: {sec}.{key} must be a finite number")
    if typ in (int, float) and val < 0:
        raise UsageError(f"{origin}: {sec}.{key} must not be negative")
    if typ is list and not all(isinstance(x, str) for x in val):
        raise UsageError(f"{origin}: {sec}.{key} must be a list of strings")
    return val


def read_toml(path):
    if tomllib is None:
        raise UsageError(f"reading {path} needs Python >= 3.11 (tomllib)")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"{path}: invalid TOML: {e}")


class Config:
    """Effective configuration with the source of every value (``cfg.get("paths", "root")``)."""

    def __init__(self, cli=None, config_file=None, env=None, read_user_file=True):
        env = os.environ if env is None else env
        self.values, self.sources, self.warnings, self.files = {}, {}, [], []
        for sec, keys in SCHEMA.items():
            for key, (default, *_rest) in keys.items():
                self._set(sec, key, default, "default")
        layers = []
        if read_user_file:
            up = user_config_path(env)
            if os.path.isfile(up):
                layers.append(up)
        cf = config_file or env.get("PXD_CONFIG")
        if cf:
            cf = os.path.abspath(os.path.expanduser(cf))
            if not os.path.isfile(cf):
                raise UsageError(f"config file {cf} not found")
            if cf not in layers:
                layers.append(cf)
        for path in layers:                               # lowest first
            self.files.append(path)
            self._load_file(path)
        def order(name):                                 # deprecated aliases < short aliases < PXD_SECTION_KEY
            return (0 if name in DEPRECATED else 1 if name in ENV_ALIASES else 2, name)
        for name, val in sorted(env.items(), key=lambda kv: order(kv[0])):
            if not name.startswith("PXD_") or name == "PXD_CONFIG":
                continue
            if name in ENV_ALIASES:
                sec, key = ENV_ALIASES[name]
            else:
                rest = name[4:].lower()
                sec = next((s for s in SCHEMA if rest.startswith(s + "_")), None)
                key = rest[len(sec) + 1:] if sec else None
                if not sec or key not in SCHEMA[sec]:
                    continue
            if SCHEMA[sec][key][1] is dict:
                self.warnings.append(f"env {name}: {sec}.{key} can only be set in a configuration file")
                continue
            self._set(sec, key, _coerce(sec, key, val, f"env {name}"), f"env:{name}")
        for (sec, key), val in (cli or {}).items():
            if val is not None:
                self._set(sec, key, _coerce(sec, key, val, "command line"), "cli")
        self._check()

    def _set(self, sec, key, val, source):
        self.values[(sec, key)] = val
        self.sources[(sec, key)] = source

    def _load_file(self, path):
        data = read_toml(path)
        base = os.path.dirname(path)
        for sec, tbl in data.items():
            if sec not in SCHEMA or not isinstance(tbl, dict):
                self.warnings.append(f"{path}: unknown section [{sec}]")
                continue
            for key, val in tbl.items():
                if key not in SCHEMA[sec]:
                    self.warnings.append(f"{path}: unknown key {sec}.{key}")
                    continue
                val = _coerce(sec, key, val, path)
                if sec == "stage" and key == "hosts":
                    for pat, opts in val.items():
                        if not isinstance(opts, dict) or set(opts) - {"ssh_options", "rsync_options"}:
                            raise UsageError(f"{path}: stage.hosts.{pat!r} takes ssh_options / rsync_options lists")
                        for k, v in opts.items():
                            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                                raise UsageError(f"{path}: stage.hosts.{pat!r}.{k} must be a list of strings")
                if sec == "paths" and key == "tools" and val and not os.path.isabs(os.path.expanduser(val)) \
                        and not val.startswith("{"):
                    val = os.path.join(base, val)                  # relative to the config file
                self._set(sec, key, val, f"file:{path}")
        tj = os.path.join(base, "tools.json")
        src = self.sources[("paths", "tools")]
        if os.path.isfile(tj) and (src == "default" or src.endswith("(tools.json next to it)")):
            self._set("paths", "tools", tj, f"file:{path} (tools.json next to it)")

    def _check(self):
        if self.get("globus", "kind") not in ("gcp", "gcs"):
            raise UsageError("globus.kind must be gcp or gcs")
        pm = self.get("globus", "path_map")
        if pm and (len(pm) != 2 or not all(isinstance(x, str) and x.startswith("/") for x in pm)):
            raise UsageError("globus.path_map must be [\"/local/prefix\", \"/collection/prefix\"]")
        if self.get("globus", "enabled") and not self.get("globus", "dest_collection"):
            self.warnings.append("globus.enabled is true but globus.dest_collection is empty: Globus is skipped")
        bad = [m for m in self.get("transfer", "methods") if m not in ("globus", "ftp", "https")]
        if bad:
            raise UsageError(f"transfer.methods: unknown {bad}")
        um = self.get("permissions", "umask")
        if um:
            try:
                if not 0 <= int(um, 8) <= 0o777:
                    raise ValueError
            except ValueError:
                raise UsageError(f"permissions.umask must be octal like 007, not {um!r}")

    # ---------------------------------------------------------------- access
    def get(self, sec, key):
        val = self.values[(sec, key)]
        if SCHEMA[sec][key][2]:
            val = [expand(x) for x in val] if isinstance(val, list) else expand(val)
        return val

    def source(self, sec, key):
        return self.sources[(sec, key)]

    def umask(self):
        um = self.get("permissions", "umask")
        return int(um, 8) if um else (0o007 if self.get("permissions", "shared") else 0o077)

    def modes(self):
        """(directory mode, file mode)."""
        return (0o2770, 0o660) if self.get("permissions", "shared") else (0o700, 0o600)

    def stage_options(self, host):
        """ssh and rsync options for a remote stage target, the last matching host pattern winning."""
        ssh, rsync = list(self.get("stage", "ssh_options")), list(self.get("stage", "rsync_options"))
        for pat, opts in self.get("stage", "hosts").items():
            if fnmatch.fnmatch(host, pat):
                ssh = [expand(x) for x in opts.get("ssh_options", ssh)]
                rsync = list(opts.get("rsync_options", rsync))
        return ssh, rsync

    def rows(self):
        """[(section.key, value, source)] for pxd config."""
        return [(f"{s}.{k}", self.get(s, k), self.sources[(s, k)]) for s in SCHEMA for k in SCHEMA[s]]
