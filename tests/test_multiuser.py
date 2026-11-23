"""Configuration, permission and multi-user tests of pxd-mirror (offline; no network, no real mirror).

  python3 -I tests/test_multiuser.py [--root DIR]        (or: pytest tests/)
"""
import argparse, hashlib, json, os, shlex, shutil, stat, subprocess, sys, tempfile, types, zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolation   # noqa: E402,F401
from pxdmirror import Config, Mirror, PxdError, UsageError   # noqa: E402
from pxdmirror import config as cfg_mod, convert as cv, inventory as inv_mod, store as st_mod, transfer as tx, util   # noqa: E402

PXD = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from pxdmirror.cli import run; run()", SRC]
LINUX = sys.platform.startswith("linux")
passed = failed = 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {name}")
    else:
        failed += 1
        print(f"FAIL {name} {extra}")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as e:
        print(f"   (raised {type(e).__name__}: {e})")
        return False
    return False


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def dir_ok(p, want=0o2770):
    """Directory mode check; BSD/macOS may drop setgid on directories (the group is inherited there anyway)."""
    m = mode(p)
    return m == want if LINUX else (m & 0o777) == (want & 0o777)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def cfg_for(root, extra="", env=None):
    """A Config read from a temporary file (the user's own file is never read in tests)."""
    d = tempfile.mkdtemp(prefix="pxdcfg-")
    p = write(os.path.join(d, "config.toml"), f'[paths]\nroot = "{root}"\nscratch = "{d}/scratch-{{user}}"\n' + extra)
    return Config(config_file=p, env=env or {}, read_user_file=False)


def fake_inventory(m, acc, files):
    raw = {"project": {"title": "test"}, "files": [
        {"fileName": n, "fileSizeBytes": len(b), "fileCategory": {"value": "RAW" if n.endswith(".raw") else "OTHER"},
         "publicFileLocations": [{"name": "FTP Protocol", "value": f"ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/{acc}/{n}"}]}
        for n, b in files.items()],
        "checksum": "".join(f"{n}\t{hashlib.md5(b).hexdigest()}\t{len(b)}\n" for n, b in files.items())}
    util.mkdirs(m.ds(acc))
    return inv_mod.publish(m.ds(acc), acc, raw)


def publish_file(m, inv, acc, name, data):
    st = util.mkdirs(m.work(acc, "t"))
    s = os.path.join(st, name)
    with open(s, "wb") as fh:
        fh.write(data)
    e = m._file_entry(inv, name, s, hashlib.md5(data).hexdigest(), "test")
    return m.publish(acc, s, f"raw/{name}", {"kind": "file", "key": name, "entry": e})


# ------------------------------------------------------------------ configuration
def config_tests(base):
    xdg = os.path.join(base, "xdg")
    write(os.path.join(xdg, "pxd", "config.toml"), '[paths]\nroot = "/from/user"\n[transfer]\nsplit = 2\n')
    site = write(os.path.join(base, "site", "config.toml"),
                 '[paths]\nroot = "/from/site"\nscratch = "/tmp/s-{user}"\n[limits]\nquota_margin_gb = 7\n'
                 '[stage]\nssh_options = ["-o", "BatchMode=yes"]\n'
                 '[stage.hosts."*.cluster.example"]\nssh_options = ["-i", "~/.ssh/id_x", "-o", "IdentitiesOnly=yes"]\n')
    write(os.path.join(base, "site", "tools.json"), "{}")
    env = {"XDG_CONFIG_HOME": xdg}
    old = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = xdg
    try:
        c = Config(env=env)
        ok("config: user file read", c.get("paths", "root") == "/from/user" and c.source("paths", "root").startswith("file:"), c.rows()[:3])
        c = Config(env=dict(env, PXD_CONFIG=site))
        ok("config: $PXD_CONFIG overrides the user file", c.get("paths", "root") == "/from/site" and c.get("transfer", "split") == 2)
        ok("config: {user} expanded", c.get("paths", "scratch") == f"/tmp/s-{cfg_mod.user()}", c.get("paths", "scratch"))
        ok("config: tools.json next to the config file", c.get("paths", "tools") == os.path.join(base, "site", "tools.json"))
        c = Config(env=dict(env, PXD_CONFIG=site, PXD_ROOT="/from/env", PXD_LIMITS_QUOTA_MARGIN_GB="3"))
        ok("config: env overrides files (alias and PXD_SECTION_KEY)", c.get("paths", "root") == "/from/env"
           and c.get("limits", "quota_margin_gb") == 3.0 and c.source("paths", "root") == "env:PXD_ROOT")
        c = Config(cli={("paths", "root"): "/from/cli"}, env=dict(env, PXD_CONFIG=site, PXD_ROOT="/from/env"))
        ok("config: command line wins", c.get("paths", "root") == "/from/cli" and c.source("paths", "root") == "cli")
        ssh, rsync = c.stage_options("node7.cluster.example")
        ok("config: per-host ssh options", ssh[:2] == ["-i", os.path.expanduser("~/.ssh/id_x")] and "IdentitiesOnly=yes" in ssh, ssh)
        ok("config: other hosts get the default ssh options", c.stage_options("elsewhere.org")[0] == ["-o", "BatchMode=yes"])
        bad = write(os.path.join(base, "bad.toml"), '[paths]\nroot = 5\n')
        ok("config: a wrong type is a usage error", raises(lambda: Config(config_file=bad, env={}, read_user_file=False), UsageError))
        unk = write(os.path.join(base, "unk.toml"), '[paths]\nrooot = "/x"\n[nonsense]\na = 1\n')
        c = Config(config_file=unk, env={}, read_user_file=False)
        ok("config: unknown keys and sections warn", len(c.warnings) == 2, c.warnings)
        ok("config: missing --config file is a usage error",
           raises(lambda: Config(config_file="/nonexistent/x.toml", env={}, read_user_file=False), UsageError))
        g = write(os.path.join(base, "g.toml"), '[globus]\nenabled = true\n')
        ok("config: Globus enabled without destination warns",
           any("dest_collection" in w for w in Config(config_file=g, env={}, read_user_file=False).warnings))
        c = Config(env={}, read_user_file=False)
        ok("config: generic defaults (no root, Globus off, shared modes)", c.get("paths", "root") == "" and not c.get("globus", "enabled")
           and c.modes() == (0o2770, 0o660) and c.umask() == 0o007)
        ok("config: no root -> Mirror refuses with a usage error", raises(lambda: Mirror(config=c), UsageError))
    finally:
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old
    r = subprocess.run([*PXD, "config", "--json"], capture_output=True, text=True)
    ok("cli: config works without a root", r.returncode == 0 and json.loads(r.stdout)["values"]["paths.root"]["value"] == "", r.stderr)
    r = subprocess.run([*PXD, "list"], capture_output=True, text=True)
    ok("cli: no root -> exit 2 with a hint", r.returncode == 2 and "paths.root" in r.stderr, r.stderr)
    r = subprocess.run([*PXD, "--config", bad, "config"], capture_output=True, text=True)
    ok("cli: bad config -> exit 2", r.returncode == 2, r.stderr)


# ------------------------------------------------------------------ modes
def mode_tests(base):
    for um in (0o022, 0o077):
        os.umask(um)
        root = os.path.join(base, f"modes-{um:03o}")
        m = Mirror(config=cfg_for(root))
        ok(f"umask {um:03o}: Mirror applies permissions.umask 007", util.current_umask() == 0o007)
        os.umask(um)                                   # a library caller may change it again; modes stay explicit
        m.init()
        acc = "PXD000101"
        inv = fake_inventory(m, acc, {"a.raw": b"A" * 100})
        dest = publish_file(m, inv, acc, "a.raw", b"A" * 100)
        files = [os.path.join(root, "catalog.sqlite"), os.path.join(root, ".events.jsonl"), m.ds(acc, "manifest.json"), dest,
                 os.path.join(root, "catalog.lock"), os.path.join(root, ".locks", acc, "manifest.lock")]
        bad = {p: oct(mode(p)) for p in files if mode(p) != 0o660}
        ok(f"umask {um:03o}: catalogue, events, manifest, published file, locks are 660", not bad, bad)
        dirs = [root, os.path.join(root, ".locks"), os.path.join(root, ".ops"), os.path.join(root, ".work"), m.ds(acc),
                m.ds(acc, "raw"), os.path.join(root, ".locks", acc)]
        bad = {p: oct(mode(p)) for p in dirs if not dir_ok(p)}
        ok(f"umask {um:03o}: directories are 2770", not bad, bad)
        # a fresh catalogue created by a write (not by init) is group-writable too (H2)
        os.unlink(os.path.join(root, "catalog.sqlite"))
        m.reindex()
        ok(f"umask {um:03o}: a catalogue re-created by reindex is 660", mode(os.path.join(root, "catalog.sqlite")) == 0o660,
           oct(mode(os.path.join(root, "catalog.sqlite"))))
        # stage to a local directory: a normal copy (umask decides), never the 0600 of mkstemp (M1)
        to = os.path.join(base, f"stage-{um:03o}")
        res = m.stage(acc, ["a.raw"], to=to)
        p = os.path.join(to, "raw", "a.raw")
        ok(f"umask {um:03o}: staged copy is 0666 & ~(caller's umask), not mkstemp's 0600", res[0][1] == "copied"
           and mode(p) == 0o666 & ~um, (res, oct(mode(p))))
    os.umask(0o022)
    root = os.path.join(base, "private")
    m = Mirror(config=cfg_for(root, "[permissions]\nshared = false\n"))
    m.init()
    inv = fake_inventory(m, "PXD000102", {"p.raw": b"P" * 10})
    dest = publish_file(m, inv, "PXD000102", "p.raw", b"P" * 10)
    ok("private mirror: files 600, directories 700", mode(dest) == 0o600 and mode(root) == 0o700
       and mode(os.path.join(root, "catalog.sqlite")) == 0o600, (oct(mode(dest)), oct(mode(root))))
    util.set_modes(0o2770, 0o660)
    os.umask(0o022)


# ------------------------------------------------------------------ zip staging
def unzip_tests(base):
    root = os.path.join(base, "zip")
    m = Mirror(config=cfg_for(root))
    m.init()
    acc = "PXD000103"
    zp = os.path.join(base, "x.d.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("x.d/analysis.tdf", b"T" * 50)
    data = open(zp, "rb").read()
    inv = fake_inventory(m, acc, {"x.d.zip": data})
    publish_file(m, inv, acc, "x.d.zip", data)
    to = os.path.join(base, "zipstage")
    res = m.stage(acc, ["x.d.zip"], to=to, unzip=True)
    fin = os.path.join(to, "raw", "x.d")
    ok("stage --unzip: tree is 0777 & ~umask, not the 0700 of mkdtemp (M2)",
       any(r[1] == "unzipped" for r in res) and mode(fin) & 0o777 == 0o770, (res, oct(mode(fin)) if os.path.exists(fin) else None))


# ------------------------------------------------------------------ other users' files and intents
def foreign_tests(base):
    root = os.path.join(base, "foreign")
    m = Mirror(config=cfg_for(root))
    m.init()
    m.margin = 0
    acc = "PXD000104"
    files = {"f1.raw": b"F1" * 300, "f2.raw": b"F2" * 300}
    inv = fake_inventory(m, acc, files)
    me = os.getuid()
    real_getuid = os.getuid

    # publish(allow_copy=True) of a staged file owned by someone else: copied, verified, original removed (H1c)
    inbox = util.mkdirs(m.work(acc, "globus", "globus-x"))
    staged = os.path.join(inbox, "f1.raw")
    open(staged, "wb").write(files["f1.raw"])
    e = m._file_entry(inv, "f1.raw", staged, hashlib.md5(files["f1.raw"]).hexdigest(), "globus")
    st_mod.os.getuid = lambda: me + 1
    try:
        dest = m.publish(acc, staged, "raw/f1.raw", {"kind": "file", "key": "f1.raw", "entry": e}, allow_copy=True)
    finally:
        st_mod.os.getuid = real_getuid
    ok("foreign staged file: published from a private copy, original removed, copy dir cleaned",
       open(dest, "rb").read() == files["f1.raw"] and not os.path.exists(staged)
       and not os.listdir(m.work(acc, "adopt")) and os.stat(dest).st_ino != 0)

    # migrate classification of another user's file (H3)
    gid = os.stat(root).st_gid
    S = types.SimpleNamespace
    src_dir = tempfile.mkdtemp(dir=base)
    p = os.path.join(src_dir, "x.raw")
    open(p, "w").close()
    ok("migrate: other user's 640 file is not movable", "not group read-write" in (m._not_movable(p, S(st_uid=me + 1, st_mode=0o100640, st_gid=gid)) or ""))
    ok("migrate: other user's 660 file of another group is not movable",
       "another group" in (m._not_movable(p, S(st_uid=me + 1, st_mode=0o100660, st_gid=gid + 1)) or ""))
    ok("migrate: other user's 660 file of the mirror's group is movable", m._not_movable(p, S(st_uid=me + 1, st_mode=0o100660, st_gid=gid)) is None)
    os.chmod(src_dir, 0o1777)
    ok("migrate: other user's file in a sticky directory is not movable",
       "sticky" in (m._not_movable(p, S(st_uid=me + 1, st_mode=0o100660, st_gid=gid)) or ""))
    ok("migrate: own file in a sticky directory is movable", m._not_movable(p, S(st_uid=me, st_mode=0o100600, st_gid=gid)) is None)
    os.chmod(src_dir, 0o700)

    # another user's Globus intent: not adopted, its files are not blocked (H1b)
    state = {"submits": []}

    def run(cmd, **kw):
        if cmd[1:3] == ["task", "show"]:
            return subprocess.CompletedProcess(cmd, 0, "SUCCEEDED\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def submit(pairs, label):
        state["submits"].append([d for _, d in pairs])
        for _, d in pairs:
            open(d, "wb").write(files[os.path.basename(d)])
        return f"T{len(state['submits'])}"

    foreign_inbox = util.mkdirs(m.work(acc, "globus", "globus-foreign"))
    ip = os.path.join(root, ".ops", "globus-foreign.json")
    util.write_json(ip, {"op": "globus-foreign", "kind": "globus", "task": "TF", "accession": acc, "inbox": foreign_inbox,
                         "files": ["f2.raw"], "uid": me, "user": "someone", "dest_collection": ""}, 0o640)
    real_read_own = st_mod.read_own_json

    def foreign(path, what="record"):                # the file owner decides (a uid field in the JSON is ignored)
        obj, owner, problem = real_read_own(path, what)
        return (obj, me + 1, "written by someone") if path.endswith("globus-foreign.json") else (obj, owner, problem)
    st_mod.read_own_json = foreign
    saved = (tx.subprocess, tx.globus_usable, tx.globus_submit)
    tx.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess)
    tx.globus_usable, tx.globus_submit = (lambda root: None), submit
    try:
        res = m.get(acc, ["f2.raw"], method="globus")
    finally:
        tx.subprocess, tx.globus_usable, tx.globus_submit = saved
    ok("globus: another user's intent is not adopted; the file is fetched by our own task",
       res[0][1:] == ("ok", "globus") and len(state["submits"]) == 1 and os.path.exists(ip), (res, state))
    its = [json.load(open(os.path.join(root, ".ops", f))) for f in os.listdir(os.path.join(root, ".ops"))]
    ok("globus: the foreign intent is kept for its owner; ours records user, uid and destination",
       [i["op"] for i in its] == ["globus-foreign"], its)
    rec = m.recover()
    st_mod.read_own_json = real_read_own
    ok("recover: reports another user's Globus intent without querying it",
       any("only its owner" in r[1] and "TF" in r[1] for r in rec), rec)

    # an intent that vanishes while recover runs (finished by another user's get) is skipped (M6)
    orig = st_mod.read_json
    orig_own = st_mod.read_own_json
    st_mod.read_json = lambda path, what="record": None if path.endswith(".json") and "/.ops/" in path else orig(path, what)
    st_mod.read_own_json = lambda path, what="record": (None, None, None) if "/.ops/" in path else orig_own(path, what)
    try:
        rec, err = m.recover(), None
    except Exception as e:
        rec, err = None, e
    finally:
        st_mod.read_json = orig
        st_mod.read_own_json = orig_own
    ok("recover: a vanished intent is skipped, not a crash", err is None, repr(err))

    # lock files that are not writable for us still lock (M5)
    lp = os.path.join(root, ".locks", "ro.lock")
    open(lp, "w").close()
    os.chmod(lp, 0o440)
    if os.geteuid() != 0:
        with util.flock(lp) as fd:
            ok("lock: a read-only lock file still locks", fd is not None)
    os.chmod(lp, 0o660)


# ------------------------------------------------------------------ remote stage quoting, forbidden roots
def stage_remote_tests(base):
    root = os.path.join(base, "remote")
    m = Mirror(config=cfg_for(root, '[stage]\nssh_options = ["-o", "BatchMode=yes"]\n'
                                    '[stage.hosts."login*.example"]\nssh_options = ["-i", "/k/my key", "-o", "IdentitiesOnly=yes"]\n'))
    m.init()
    acc = "PXD000105"
    inv = fake_inventory(m, acc, {"r.raw": b"R" * 10})
    publish_file(m, inv, acc, "r.raw", b"R" * 10)
    calls = []
    weird = os.path.join(base, "it's $(touch pwned) `touch pwned2` here")

    def run(cmd, **kw):
        """rsync: copy locally to the path after 'host:'; ssh: run the remote script with the local bash."""
        calls.append(cmd)
        if cmd[0] == "rsync":
            dst = cmd[-1].split(":", 1)[1]
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(cmd[-2], "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(["bash", "-c", cmd[-1]], capture_output=True, text=True, cwd=base)
    saved = st_mod.subprocess
    st_mod.subprocess = types.SimpleNamespace(run=run, CompletedProcess=subprocess.CompletedProcess, SubprocessError=subprocess.SubprocessError)
    try:
        res = m.stage(acc, ["r.raw"], to=f"login2.example:{weird}")
    finally:
        st_mod.subprocess = saved
    rs, sh = calls[0], calls[1]
    ok("remote stage: rsync -e carries the host's ssh options, quoted", rs[rs.index("-e") + 1] == "ssh -i '/k/my key' -o IdentitiesOnly=yes" and "-s" in rs, rs)
    ok("remote stage: ssh gets the per-host options, host after --", sh[:7] == ["ssh", "-i", "/k/my key", "-o", "IdentitiesOnly=yes", "--", "login2.example"], sh)
    ok("remote stage: a target with quotes, $() and backticks is used literally (no injection)",
       res[0][1] == "copied" and open(os.path.join(weird, "raw", "r.raw"), "rb").read() == b"R" * 10
       and not os.path.exists(os.path.join(base, "pwned")) and not os.path.exists(os.path.join(base, "pwned2")), (res, sh[-1]))
    ok("remote stage: temporary directory removed", not [x for x in os.listdir(weird) if x.startswith(".pxd-stage")], os.listdir(weird))

    forb = os.path.join(base, "forbidden")
    os.makedirs(forb, exist_ok=True)
    c = cfg_for(os.path.join(forb, "mirror"), f'forbidden_roots = ["{forb}"]\n')
    ok("forbidden root: Mirror refuses", raises(lambda: Mirror(config=c), UsageError))
    m2 = Mirror(config=cfg_for(root, f'forbidden_roots = ["{forb}"]\n'))
    ok("forbidden root: stage refuses a target below it", raises(lambda: m2.stage(acc, ["r.raw"], to=os.path.join(forb, "x")), UsageError))


# ------------------------------------------------------------------ converters
FAKE_CONV = """#!/bin/sh
# fake converter: --version prints a version; otherwise <in> -o <out> writes a small unindexed mzML
if [ "$1" = "--version" ]; then echo "fakeconv 1.2.3"; exit 0; fi
printf '<?xml version="1.0"?><mzML><run><spectrumList count="0"></spectrumList></run></mzML>' > "$3"
"""


def convert_tests(base):
    tdir = os.path.join(base, "tools")
    os.makedirs(tdir, exist_ok=True)
    exe = write(os.path.join(tdir, "fakeconv"), FAKE_CONV)
    os.chmod(exe, 0o755)
    sha = hashlib.sha256(open(exe, "rb").read()).hexdigest()
    reg = {"fakeconv": {"path": "${FAKE_HOME}/fakeconv", "env": {"FAKE_HOME": tdir}, "version_args": ["--version"],
                        "version_regex": r"fakeconv (\S+)", "argv": {"mzML": ["{in}", "-o", "{out}"]},
                        "default_for": {"mzML": [".raw"]}, "sha256": sha}}
    tj = write(os.path.join(tdir, "tools.json"), json.dumps(reg))
    os.chmod(tdir, 0o755)                     # converters live where only their maintainer can write
    os.chmod(tj, 0o644)
    root = os.path.join(base, "conv")
    c = cfg_for(root, f'tools = "{tj}"\n')
    m = Mirror(config=c)
    m.init()
    acc = "PXD000106"
    inv = fake_inventory(m, acc, {"c.raw": b"C" * 64})
    publish_file(m, inv, acc, "c.raw", b"C" * 64)
    cv._ID.clear()
    res = m.convert(acc, ["c.raw"], "mzML")
    ok("convert: ${VAR} in the registry, configured scratch, validated, published", res[0][1] == "ok", res)
    out = os.path.join(root, acc, res[0][2]) if res[0][1] == "ok" else None
    ok("convert: output is 660", out and mode(out) == 0o660, oct(mode(out)) if out else None)
    scratch = c.get("paths", "scratch")
    ok("convert: per-user scratch created private (700)", os.path.isdir(scratch) and mode(scratch) & 0o777 == 0o700, scratch)
    conv = list(m.manifest(acc)["conversions"].values())[0]
    ok("convert: provenance has version and sha256", conv["tool"]["version"] == "1.2.3" and conv["tool"]["sha256"] == sha, conv["tool"])
    ok("convert: provenance records the tree hash", len(conv["tool"].get("tree_sha256") or "") == 64, conv["tool"])
    rec1 = conv["recipe"]

    # the binary is swapped after the first conversion in the same process: the next one re-hashes and refuses
    spec = cv.load_tools(tj)["fakeconv"]
    cv.identify("fakeconv", spec)
    write(exe, FAKE_CONV.replace("1.2.3", "6.6.6"))
    os.chmod(exe, 0o755)
    ok("tools: a pinned executable swapped within one process is refused before it runs",
       raises(lambda: cv.identify("fakeconv", spec), PxdError))
    inv2 = fake_inventory(m, "PXD000116", {"e.raw": b"E" * 64})
    publish_file(m, inv2, "PXD000116", "e.raw", b"E" * 64)
    res = m.convert("PXD000116", ["e.raw"], "mzML")
    ok("convert: ... and convert reports it as failed", res[0][1] == "failed" and "sha256" in res[0][2], res)
    write(exe, FAKE_CONV)
    os.chmod(exe, 0o755)

    # entries another member could change: a group-writable deployment directory, a symlink to a writable file
    os.chmod(tdir, 0o775)
    cv._ID.clear()
    ok("tools: a group-writable deployment directory refuses the tool",
       raises(lambda: cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"]), PxdError))
    os.chmod(tdir, 0o755)
    shared = os.path.join(base, "shared-lib")
    os.makedirs(shared, exist_ok=True)
    os.chmod(shared, 0o777)
    lib = write(os.path.join(shared, "libx.so"), "x")
    os.symlink(lib, os.path.join(tdir, "libx.so"))
    cv._ID.clear()
    ok("tools: a symlink to a file in a writable directory outside the deployment refuses the tool",
       raises(lambda: cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"]), PxdError))
    os.unlink(os.path.join(tdir, "libx.so"))
    os.chmod(shared, 0o755)

    # tree pin, recipe compatibility declaration
    good = cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"])
    reg["fakeconv"]["tree_sha256"] = "1" * 64
    write(tj, json.dumps(reg))
    cv._ID.clear()
    ok("tools: a tree_sha256 pin that does not match refuses the tool",
       raises(lambda: cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"]), PxdError))
    reg["fakeconv"]["tree_sha256"] = good["tree_sha256"]
    dep = write(os.path.join(tdir, "dep.dll"), "v1")
    os.chmod(dep, 0o644)
    reg["fakeconv"]["deployment_files"] = ["dep.dll"]
    write(tj, json.dumps(reg))
    cv._ID.clear()
    ok("tools: a tree_sha256 pin catches a changed file the registry does not list",
       raises(lambda: cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"]), PxdError))
    del reg["fakeconv"]["tree_sha256"]
    write(tj, json.dumps(reg))
    cv._ID.clear()
    sp = cv.load_tools(tj)["fakeconv"]
    i1 = cv.identify("fakeconv", sp)
    r1 = cv.recipe("fakeconv", i1, sp, "mzML")
    old_sha = i1["deployment"]["dep.dll"]
    write(dep, "v2 relinked")
    cv._ID.clear()
    i2 = cv.identify("fakeconv", sp)
    ok("recipe: a changed deployment file changes the recipe", cv.recipe("fakeconv", i2, sp, "mzML") != r1)
    sp["recipe_deployment_sha256"] = {"dep.dll": old_sha}
    ok("recipe: recipe_deployment_sha256 keeps the earlier name; provenance keeps the actual hash",
       cv.recipe("fakeconv", i2, sp, "mzML") == r1 and i2["deployment"]["dep.dll"] != old_sha)
    os.unlink(dep)
    reg["fakeconv"].pop("deployment_files")
    reg["fakeconv"]["sha256"] = "0" * 64
    write(tj, json.dumps(reg))
    cv._ID.clear()
    ok("tools: a pinned sha256 that does not match refuses the tool",
       raises(lambda: cv.identify("fakeconv", cv.load_tools(tj)["fakeconv"]), PxdError))
    reg["fakeconv"]["sha256"] = sha
    write(tj, json.dumps(reg))
    cv._ID.clear()

    # conversion staging another user could rename away, or under a forbidden root
    ww = os.path.join(base, "worldwritable")
    os.makedirs(ww, exist_ok=True)
    os.chmod(ww, 0o777)
    res = m.convert("PXD000116", ["e.raw"], "mzML", stage=os.path.join(ww, "me", "pxd"), tools_json=tj)
    ok("convert: staging below a world-writable directory without the sticky bit is refused",
       res[0][1] == "failed" and "replace" in res[0][2], res)
    os.chmod(ww, 0o755)
    cf = Mirror(config=cfg_for(root, f'tools = "{tj}"\nforbidden_roots = ["{ww}"]\n'))
    res = cf.convert("PXD000116", ["e.raw"], "mzML", stage=os.path.join(ww, "st"))
    ok("convert: staging under a forbidden root is refused", res[0][1] == "failed" and "forbidden" in res[0][2], res)
    ok("convert: ... and nothing was created there", not os.path.exists(os.path.join(ww, "st")))
    cv._ID.clear()
    del rec1


# ------------------------------------------------------------------ doctor
def doctor_tests(base):
    root = os.path.join(base, "doctor")
    m = Mirror(config=cfg_for(root))
    m.init()
    acc = "PXD000107"
    inv = fake_inventory(m, acc, {"d.raw": b"D" * 10})
    dest = publish_file(m, inv, acc, "d.raw", b"D" * 10)
    before = {p: os.lstat(p).st_mode for p in (dest, m.ds(acc))}
    r = m.doctor()
    ok("doctor: a fresh mirror has no problems", not r["problems"], r["problems"])
    g = subprocess.run(["getfacl", "-p", root], capture_output=True, text=True) if shutil.which("getfacl") else None
    if g and "default:group::rwx" in g.stdout:      # a file system with ACLs: init set them, doctor must see them
        ok("doctor: the default ACL set by init is recognised", not [c for c in r["checks"] if c[0] == "acl"], r["checks"])
    os.chmod(dest, 0o644)
    os.chmod(m.ds(acc), 0o1755)
    r = m.doctor()
    kinds = set(r["problems"])
    ok("doctor: reports file mode, directory mode, sticky bit, access for others",
       {"file mode", "directory mode", "sticky bit", "access for others"} <= kinds, kinds)
    ok("doctor: read-only without --fix", mode(dest) == 0o644)
    r = m.doctor(fix=True)
    ok("doctor --fix: repairs own entries", r["fixed"] >= 2 and mode(dest) == 0o660 and dir_ok(m.ds(acc)), (r, oct(mode(dest))))
    ok("doctor: clean after --fix", not m.doctor()["problems"])
    ip = os.path.join(root, ".ops", "pub-x.json")
    util.write_json(ip, {"op": "pub-x", "kind": "publish"}, 0o640)
    ok("doctor: an intent written 640 is not a problem", not m.doctor()["problems"], m.doctor()["problems"])
    os.chmod(ip, 0o660)
    r = m.doctor(fix=True)
    ok("doctor --fix: a group-writable intent is reported, never made trusted by --fix",
       mode(ip) == 0o660 and any(k.startswith("intent mode") for k in r["problems"]), (oct(mode(ip)), r["problems"]))
    os.unlink(ip)
    del before


# ------------------------------------------------------------------ transport and Globus helpers
def transport_tests(base):
    ok("curl rc 23 permission denied -> restart from zero", raises(lambda: tx._curl_err(23, "Failure writing output: Permission denied"), tx.Unresumable))
    ok("curl rc 23 quota -> capacity", raises(lambda: tx._curl_err(23, "Disk quota exceeded"), util.CapacityError))
    saved = dict(tx.G)
    try:
        tx.configure_globus(enabled=False)
        ok("globus: off by default", "not enabled" in tx.globus_usable(base))
        tx.configure_globus(enabled=True, dest="")
        ok("globus: no destination -> skipped", "destination" in tx.globus_usable(base))
        cfgdir = os.path.join(base, "gcp")
        ro = os.path.join(base, "ro")
        os.makedirs(ro, exist_ok=True)
        write(os.path.join(cfgdir, "lta", "config-paths"), f"~/,0,1\n{ro},0,0\n")
        tx.configure_globus(enabled=True, dest="00000000-0000-0000-0000-000000000000", cli="sh", kind="gcp",
                            gcp_config_dir=cfgdir, check_config_paths=True, shared_prefixes=[base])
        why = tx.globus_usable(os.path.join(ro))
        ok("globus: a read-only config-paths entry is refused", why and "read-only" in why, why)
        entries = tx.gcp_config_paths(cfgdir)
        ok("globus: ~ in config-paths means the GCP user's home", entries[0][0] == os.path.realpath(os.path.expanduser("~")), entries)
        tx.configure_globus(path_map=["/local/mirror", "/collection/pxd"])
        ok("globus: path_map maps local paths to collection paths", tx.dest_path("/local/mirror/.work/x") == "/collection/pxd/.work/x")
        ok("globus: paths outside path_map are refused", raises(lambda: tx.dest_path("/elsewhere/x"), PxdError))
    finally:
        tx.G.clear()
        tx.G.update(saved)

# ------------------------------------------------------------------ regressions found by the adversarial review
def review_tests(base):
    root = os.path.join(base, "review")
    m = Mirror(config=cfg_for(root))
    m.init()
    m.margin = 0
    acc = "PXD000108"
    files = {"a.raw": b"A" * 50, "b.raw": b"B" * 60, "c.raw": b"C" * 70}
    inv = fake_inventory(m, acc, files)

    # migrating from a directory that happens to be called "adopt" must not delete the source directory
    src = os.path.join(tempfile.mkdtemp(dir=os.path.dirname(root)), "adopt", acc)
    os.makedirs(src)
    open(os.path.join(src, "a.raw"), "wb").write(files["a.raw"])
    open(os.path.join(src, "notes.txt"), "w").write("keep me")
    res = m.migrate_apply(m.migrate_plan([os.path.dirname(os.path.dirname(src))])["plan"])
    ok("migrate from .../adopt/PXD...: moved, the other files in the source directory survive",
       res and res[0][2] == "moved" and open(os.path.join(src, "notes.txt")).read() == "keep me"
       and os.path.islink(os.path.join(src, "a.raw")), res)

    # a plan file never overwrites anything
    ok("migrate --plan --out refuses an existing file",
       raises(lambda: m.migrate_plan([src], out=os.path.join(src, "notes.txt")), UsageError)
       and open(os.path.join(src, "notes.txt")).read() == "keep me")

    # recover refuses intents whose paths point outside the mirror
    victim = os.path.join(base, "victim.txt")
    open(victim, "w").write("precious")
    bad = {"op": "pub-evil", "kind": "publish", "accession": acc, "rel": "raw/b.raw", "staged": victim,
           "md5": hashlib.md5(b"precious").hexdigest(), "backlink": None,
           "record": {"kind": "file", "key": "b.raw", "entry": {"md5": hashlib.md5(b"precious").hexdigest()}}}
    util.write_json(os.path.join(root, ".ops", "pub-evil.json"), bad, 0o640)
    util.write_json(os.path.join(root, ".ops", "pub-evil2.json"), dict(bad, op="pub-evil2", accession="../../x"), 0o640)
    util.write_json(os.path.join(root, ".ops", "pub-evil3.json"), {"kind": "publish"}, 0o640)
    rec = dict(m.recover())
    ok("recover: intents with outside paths, a bad accession or missing keys need attention, nothing touched",
       all("NEEDS ATTENTION" in rec.get(k, "") for k in ("pub-evil.json", "pub-evil2.json", "pub-evil3.json"))
       and open(victim).read() == "precious" and not os.path.exists(m.ds(acc, "raw", "b.raw")), rec)
    r = subprocess.run([*PXD, "--root", root, "recover"], capture_output=True, text=True)
    ok("cli: recover exits 1 when something needs attention", r.returncode == 1, r.stdout + r.stderr)
    for f in ("pub-evil.json", "pub-evil2.json", "pub-evil3.json"):
        os.unlink(os.path.join(root, ".ops", f))

    # review: a group member plants a publish intent whose staged/backlink/migrated_from all name a file in another
    # member's home; recover must never create that symlink (the intent is not the caller's own)
    bait = publish_file(m, inv, acc, "b.raw", files["b.raw"])
    home_target = os.path.join(base, "victim-home", ".bash_profile")
    os.makedirs(os.path.dirname(home_target), exist_ok=True)
    md5b = hashlib.md5(files["b.raw"]).hexdigest()
    plant = {"op": "pub-plant", "kind": "publish", "accession": acc, "rel": "raw/b.raw", "staged": home_target,
             "backlink": home_target, "md5": md5b,
             "record": {"kind": "file", "key": "b.raw", "entry": {"migrated_from": home_target, "md5": md5b}}}
    ip = os.path.join(root, ".ops", "pub-plant.json")
    util.write_json(ip, plant)                         # 660: writable for the group, as a planted file would be
    rec = dict(m.recover())
    ok("recover: a group-writable publish intent is not acted on", "NEEDS ATTENTION" in rec.get("pub-plant", "")
       and not os.path.lexists(home_target), rec)
    os.chmod(ip, 0o640)
    real_read_own = st_mod.read_own_json
    st_mod.read_own_json = lambda path, what="record": (real_read_own(path, what)[0], os.getuid() + 1, "written by someone")
    try:
        rec = dict(m.recover())
    finally:
        st_mod.read_own_json = real_read_own
    ok("recover: another member's publish intent is only reported; no symlink at its paths",
       "only its owner" in rec.get("pub-plant", "") and not os.path.lexists(home_target) and os.path.exists(ip), rec)
    os.unlink(ip)
    ok("recover: the bait file stays where it was", os.path.isfile(bait))

    # delete checks every item before removing any
    publish_file(m, inv, acc, "b.raw", files["b.raw"])
    publish_file(m, inv, acc, "c.raw", files["c.raw"])
    plan = m.delete_plan(acc, files=["b.raw", "c.raw"])
    os.unlink(m.ds(acc, "raw", "c.raw"))
    os.symlink(victim, m.ds(acc, "raw", "c.raw"))
    ok("delete: a bad item stops the delete before anything is removed",
       raises(lambda: m.delete(acc, files=["b.raw", "c.raw"], token=m.delete_plan(acc, files=["b.raw", "c.raw"])["token"]), PxdError)
       and os.path.isfile(m.ds(acc, "raw", "b.raw")) and open(victim).read() == "precious", plan)
    os.unlink(os.path.join(root, acc, "raw", "c.raw"))

    # manifests with a wrong structure are corrupt, not empty; manifest paths are validated
    mp = m.ds(acc, "manifest.json")
    good = open(mp).read()
    for txt in ("{}", "null", "[]"):
        open(mp, "w").write(txt)
        ok(f"manifest {txt}: CorruptRecord, never treated as empty", raises(lambda: m.manifest(acc), st_mod.CorruptRecord) and open(mp).read() == txt)
    man = json.loads(good)
    man["conversions"]["evil"] = {"source_name": "a.raw", "format": "mzML", "recipe": "r", "status": "ok",
                                  "output_path": "../../../etc/passwd", "started": "z", "tool": {"name": "t", "version": "1"}}
    open(mp, "w").write(json.dumps(man))
    ok("path: a manifest path that escapes the mirror is refused", raises(lambda: m.path(acc, "a.raw", "mzML"), UsageError))
    open(mp, "w").write(good)

    # query compares with PRIDE's md5 of the current inventory generation
    ok("query: complete before PRIDE changes a checksum", m.query(acc)["state"] in ("complete", "partial"))
    raw = {"project": {"title": "t"}, "files": [dict(f) for f in inv["files"].values()],
           "checksum": "".join(f"{n}\t{hashlib.md5(b + b'x').hexdigest()}\t{len(b)}\n" for n, b in files.items())}
    inv_mod.publish(m.ds(acc), acc, raw)
    q = m.query(acc)
    ok("query: a file whose PRIDE checksum changed counts as missing", "a.raw" in q["missing"] and q["state"] == "partial", q)
    os.unlink(os.path.join(root, acc, "raw", "b.raw"))
    ok("query: a recorded file that vanished counts as missing", "b.raw" in m.query(acc)["missing"])
    ok("delete token depends on the accession", m.delete_plan(acc, derived_only=True)["token"]
       != Mirror(config=cfg_for(root)).delete_plan("PXD000109", derived_only=True)["token"])

    # an on-disk inventory with an unsafe file name is corrupt
    inv2 = fake_inventory(m, "PXD000109", {"x.raw": b"x"})
    fj = os.path.join(m.ds("PXD000109"), "metadata", inv2["gen"], "files.json")
    data = json.load(open(fj))
    data[0]["fileName"] = "../../outside"
    open(fj, "w").write(json.dumps(data))
    ok("inventory: an unsafe file name on disk is a CorruptRecord", raises(lambda: inv_mod.load(m.ds("PXD000109"), "PXD000109"), st_mod.CorruptRecord))

    # --method globus on files already present: skip, not failed
    saved = tx.globus_usable
    tx.globus_usable = lambda root: None
    inv3 = fake_inventory(m, "PXD000110", {"g.raw": b"G" * 9})
    publish_file(m, inv3, "PXD000110", "g.raw", b"G" * 9)
    try:
        res = m.get("PXD000110", ["g.raw"], method="globus")
    finally:
        tx.globus_usable = saved
    ok("get --method globus: a file already mirrored is skip, not failed", res[0][1] == "skip", res)

    # local stage into the mirror itself is refused
    ok("stage: a target inside the mirror is refused", raises(lambda: m.stage(acc, ["b.raw"], to=os.path.join(root, "x")), UsageError))

    # rsync -e quoting and remote host validation
    ok("rsync -e: plain, spaces and apostrophes quoted the way rsync parses them",
       st_mod.rsync_command(["ssh", "-i", "/k/my key", "-i", "/k/O'Brien"]) == "ssh -i '/k/my key' -i \"/k/O'Brien\"")
    ok("stage: a host starting with - is refused", raises(lambda: m.stage(acc, ["b.raw"], to="-oProxyCommand=x:/tmp"), UsageError))

    # recover isolates non-object intents; a re-downloaded deleted data set is no longer marked deleted
    util.write_json(os.path.join(root, ".ops", "00-junk.json"), "not an object")
    os.mkdir(os.path.join(root, ".ops", "01-dir.json"))
    rec = dict(m.recover())
    ok("recover: a non-object intent and a directory named *.json need attention, the scan goes on",
       "NEEDS ATTENTION" in rec.get("00-junk.json", "") and "NEEDS ATTENTION" in rec.get("01-dir.json", ""), rec)
    os.unlink(os.path.join(root, ".ops", "00-junk.json"))
    os.rmdir(os.path.join(root, ".ops", "01-dir.json"))
    acc4 = "PXD000112"
    inv4 = fake_inventory(m, acc4, {"d.raw": b"D" * 5})
    publish_file(m, inv4, acc4, "d.raw", b"D" * 5)
    m.delete(acc4, dataset=True, token=m.delete_plan(acc4, dataset=True)["token"])
    ok("delete --dataset marks the data set deleted", m.query(acc4)["deleted"])
    publish_file(m, inv4, acc4, "d.raw", b"D" * 5)
    q = m.query(acc4)
    ok("a data set downloaded again after deletion is complete and not deleted", q["state"] == "complete" and not q["deleted"], q)

    # placeholders inside arguments; identity cache keyed by path and pin
    spec = {"path": "/bin/tool", "argv": {"mzML": ["-i={in}", "-b={out}", "-t", "{threads}"]}}
    ok("convert argv: placeholders inside an argument are substituted",
       cv.argv(spec, "mzML", "/s/in.raw", "/s/out.mzML", 4) == ["/bin/tool", "-i=/s/in.raw", "-b=/s/out.mzML", "-t", "4"])


def config_type_tests(base):
    for txt, what in (('[globus]\nenabled = "yes"\n', "string for a boolean"), ('[transfer]\nsplit = "3"\n', "string for an integer"),
                      ('[paths]\nforbidden_roots = [42]\n', "non-string list element"), ('[limits]\nlock_wait_s = -1\n', "negative number")):
        p = write(os.path.join(base, "t.toml"), txt)
        ok(f"config: {what} in a file is a usage error", raises(lambda: Config(config_file=p, env={}, read_user_file=False), UsageError))
    ok("config: nan from the environment is a usage error", raises(lambda: Config(env={"PXD_LIMITS_LOCK_WAIT_S": "nan"}, read_user_file=False), UsageError))
    c = Config(env={"PXD_ROOT": "/short", "PXD_PATHS_ROOT": "/long"}, read_user_file=False)
    ok("config: PXD_PATHS_ROOT wins over the short alias", c.get("paths", "root") == "/long")
    c = Config(env={"PXD_GLOBUS_SHARED_PREFIXES": '["/shared/My Group"]'}, read_user_file=False)
    ok("config: env lists accept a JSON array (elements with spaces)", c.get("globus", "shared_prefixes") == ["/shared/My Group"])
    d1, d2 = os.path.join(base, "u"), os.path.join(base, "s")
    write(os.path.join(d1, "pxd", "config.toml"), "[site]\nname = 'u'\n")
    write(os.path.join(d1, "pxd", "tools.json"), "{}")
    write(os.path.join(d2, "config.toml"), "[site]\nname = 's'\n")
    write(os.path.join(d2, "tools.json"), "{}")
    c = Config(env={"XDG_CONFIG_HOME": d1, "PXD_CONFIG": os.path.join(d2, "config.toml")})
    ok("config: the higher-precedence file's tools.json wins", c.get("paths", "tools") == os.path.join(d2, "tools.json"), c.get("paths", "tools"))


# ------------------------------------------------------------------ review round 4 regressions
def inventory_with(m, acc, files, cks):
    """An inventory generation of @p files whose checksum list covers only @p cks ({name: md5})."""
    raw = {"project": {"title": "test"}, "files": [
        {"fileName": n, "fileSizeBytes": len(b), "fileCategory": {"value": "RAW"},
         "publicFileLocations": [{"name": "FTP Protocol", "value": f"ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/{acc}/{n}"}]}
        for n, b in files.items()],
        "checksum": "".join(f"{n}\t{h}\t{len(files[n])}\n" for n, h in cks.items())}
    util.mkdirs(m.ds(acc))
    return inv_mod.publish(m.ds(acc), acc, raw)


def own_intent(m, acc, oid, names, contents):
    inbox = util.mkdirs(m.work(acc, "globus", oid))
    for n in names:
        if n in contents:
            open(os.path.join(inbox, n), "wb").write(contents[n])
    it = {"op": oid, "kind": "globus", "task": "T-" + oid, "accession": acc, "inbox": inbox, "files": list(names),
          "created": util.now(), "user": util.user(), "dest_collection": tx.G["dest"]}
    ip = os.path.join(m.root, ".ops", oid + ".json")
    util.write_json(ip, it, 0o640)
    return ip, it


def review4_tests(base):
    root = os.path.join(base, "review4")
    m = Mirror(config=cfg_for(root))
    m.init()
    m.margin = 0
    md5 = lambda b: hashlib.md5(b).hexdigest()

    # Globus: a file without a PRIDE checksum never replaces an archived copy; a failed task's delivery is not used
    acc = "PXD000120"
    good, short = b"N" * 500, b"N" * 100
    files = {"n.raw": good, "k.raw": b"K" * 300}
    inv = inventory_with(m, acc, files, {"k.raw": md5(b"K" * 300)})
    publish_file(m, inv, acc, "n.raw", good)                  # local baseline (no PRIDE md5)
    ip, it = own_intent(m, acc, "globus-r4a", ["n.raw"], {"n.raw": short})
    done, _ = m._globus_collect(acc, inv, ip, it, "FAILED")
    ok("globus: a failed task's delivery of a checksumless file is not published; the archived copy stays",
       not done and open(m.ds(acc, "raw", "n.raw"), "rb").read() == good
       and m.manifest(acc)["files"]["n.raw"]["md5"] == md5(good), done)
    ip, it = own_intent(m, acc, "globus-r4b", ["n.raw"], {"n.raw": b"M" * 500})
    done, _ = m._globus_collect(acc, inv, ip, it, "SUCCEEDED")
    ok("globus: a checksumless delivery that differs from the archived copy does not replace it",
       not done and open(m.ds(acc, "raw", "n.raw"), "rb").read() == good, done)

    # Globus: only the requested names are published; the rest stays in the inbox with the intent
    acc2 = "PXD000121"
    f2 = {"a.raw": b"A" * 200, "b.raw": b"B" * 200}
    inv2 = inventory_with(m, acc2, f2, {n: md5(b) for n, b in f2.items()})
    ip, it = own_intent(m, acc2, "globus-r4c", ["a.raw", "b.raw"], f2)
    done, _ = m._globus_collect(acc2, inv2, ip, it, "SUCCEEDED", ["a.raw"])
    ok("globus: an adopted task publishes only what this get asked for", done == {"a.raw"}
       and not os.path.exists(m.ds(acc2, "raw", "b.raw")) and os.path.exists(os.path.join(it["inbox"], "b.raw"))
       and os.path.exists(ip), done)

    # delete: an in-flight Globus task is part of the plan; its deliveries never bring the data set back
    plan = m.delete_plan(acc2, dataset=True)
    ok("delete: the plan lists the Globus task still carrying files of the data set",
       [g["op"] for g in plan["globus"]] == ["globus-r4c"], plan)
    m.delete(acc2, dataset=True, token=plan["token"])
    ok("delete: the delivered but unpublished file is removed from the inbox",
       not os.path.exists(os.path.join(it["inbox"], "b.raw")))
    open(os.path.join(it["inbox"], "b.raw"), "wb").write(f2["b.raw"])        # the task delivers it later
    done, _ = m._globus_collect(acc2, inv2, ip, it, "SUCCEEDED", ["a.raw", "b.raw"])
    man = m.manifest(acc2)
    ok("delete: a later delivery is discarded and the data set stays deleted",
       not done and man.get("status") == "deleted" and not os.path.exists(m.ds(acc2, "raw", "b.raw"))
       and not os.path.exists(os.path.join(it["inbox"], "b.raw")), (done, man.get("status")))
    ok("delete: a token computed without the Globus task does not match", plan["token"] != m.delete_plan(acc, dataset=True)["token"])

    # get of a file whose PRIDE md5 changed: the archived copy stays until a verified replacement exists
    acc3 = "PXD000122"
    old, new = b"O" * 300, b"P" * 300
    inv3 = inventory_with(m, acc3, {"s.raw": old}, {"s.raw": md5(old)})
    publish_file(m, inv3, acc3, "s.raw", old)
    inv3 = inventory_with(m, acc3, {"s.raw": new}, {"s.raw": md5(new)})
    saved = tx.curl_one

    def broken(url, out, size, lk):
        raise tx.Permanent("network down")
    tx.curl_one = broken
    try:
        r = m._fetch_one(acc3, inv3, "s.raw", "ftp", False, 1, 1, 1, 0.0)
    finally:
        tx.curl_one = saved
    ok("get: a failed replacement leaves the archived copy and its record in place",
       r[0] == "failed" and open(m.ds(acc3, "raw", "s.raw"), "rb").read() == old
       and m.manifest(acc3)["files"]["s.raw"]["status"] == "ok", r)

    def fetch(url, out, size, lk):
        open(out, "wb").write(new)
    tx.curl_one = fetch
    try:
        r = m._fetch_one(acc3, inv3, "s.raw", "ftp", False, 1, 1, 1, 0.0)
    finally:
        tx.curl_one = saved
    e = m.manifest(acc3)["files"]["s.raw"]
    ok("get: a verified replacement is published and the old copy quarantined",
       r[0] == "ok" and open(m.ds(acc3, "raw", "s.raw"), "rb").read() == new and e["md5"] == md5(new)
       and e["status"] == "ok" and os.listdir(m.work(acc3, "quarantine")), (r, e))

    # forbidden roots: migrate sources, plan files, hand-edited plans, legacy imports
    fb = os.path.join(base, "forbidden-area")
    os.makedirs(os.path.join(fb, acc3), exist_ok=True)
    victim = os.path.join(fb, acc3, "s.raw")
    open(victim, "wb").write(new)
    mf = Mirror(config=cfg_for(root, f'forbidden_roots = ["{fb}"]\n'))
    ok("migrate --plan: a source under a forbidden root is refused", raises(lambda: mf.migrate_plan([fb]), UsageError))
    ok("migrate --plan --out: a plan file under a forbidden root is refused",
       raises(lambda: mf.migrate_plan([os.path.join(base, "nothing-here")], out=os.path.join(fb, "plan.tsv")), UsageError)
       and not os.path.exists(os.path.join(fb, "plan.tsv")))
    stv = os.stat(victim)
    plan_path = os.path.join(base, "edited-plan.tsv")
    write(plan_path, "class\taccession\tname\tsrc\tdev\tino\tsize\tmtime_ns\tmd5\tdest\n"
          f"move\t{acc3}\ts.raw\t{victim}\t{stv.st_dev}\t{stv.st_ino}\t{stv.st_size}\t{stv.st_mtime_ns}\t{md5(new)}\traw/s2.raw\n")
    res = mf.migrate_apply(plan_path)
    ok("migrate --apply: a hand-edited row with a source under a forbidden root is left alone",
       res and res[0][2] == "left" and os.path.isfile(victim) and not os.path.islink(victim), res)
    ok("import-legacy: a directory under a forbidden root is refused",
       raises(lambda: mf.import_legacy(os.path.join(fb, acc3)), UsageError))

    # configuration layers: site < personal < --config; [stage.hosts] merged pattern by pattern
    d = os.path.join(base, "layers")
    site = write(os.path.join(d, "site", "config.toml"), '[paths]\nroot = "/site"\n[globus]\nenabled = true\n'
                 'dest_collection = "site-uuid"\n[stage.hosts."*hpc*"]\nssh_options = ["-i", "~/.ssh/site_key"]\n'
                 '[stage.hosts."*other*"]\nssh_options = ["-i", "~/.ssh/other"]\n')
    write(os.path.join(d, "xdg", "pxd", "config.toml"), '[globus]\ndest_collection = "my-uuid"\n'
          '[stage.hosts."*hpc*"]\nssh_options = ["-i", "~/.ssh/my_key"]\n')
    c = Config(env={"PXD_SITE_CONFIG": site, "XDG_CONFIG_HOME": os.path.join(d, "xdg")})
    ok("config: the personal file overrides the site file", c.get("globus", "dest_collection") == "my-uuid"
       and c.get("paths", "root") == "/site" and c.get("globus", "enabled") is True, c.rows())
    ok("config: [stage.hosts] merges pattern by pattern; the personal entry wins",
       c.stage_options("login.hpc.example")[0] == ["-i", os.path.expanduser("~/.ssh/my_key")]
       and c.stage_options("x.other.example")[0] == ["-i", os.path.expanduser("~/.ssh/other")], c.get("stage", "hosts"))
    ok("config: overridden file values are listed", any(k == "globus.dest_collection" for k, _, _ in c.shadowed), c.shadowed)
    over = write(os.path.join(d, "over.toml"), '[globus]\ndest_collection = "explicit"\n')
    c = Config(config_file=over, env={"PXD_SITE_CONFIG": site, "XDG_CONFIG_HOME": os.path.join(d, "xdg")})
    ok("config: --config overrides the personal file", c.get("globus", "dest_collection") == "explicit")
    ok("config: a missing $PXD_SITE_CONFIG is a usage error",
       raises(lambda: Config(env={"PXD_SITE_CONFIG": "/nonexistent/site.toml"}, read_user_file=False), UsageError))


def run_all(base):
    os.makedirs(base, exist_ok=True)
    config_tests(os.path.join(base, "config"))
    mode_tests(base)
    unzip_tests(base)
    foreign_tests(base)
    stage_remote_tests(base)
    convert_tests(base)
    doctor_tests(base)
    transport_tests(base)
    review_tests(base)
    review4_tests(base)
    config_type_tests(os.path.join(base, "config"))


def test_multiuser_suite(tmp_path):
    """pytest entry point."""
    run_all(str(tmp_path))
    assert failed == 0, f"{failed} checks failed (see the FAIL lines)"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    a = ap.parse_args()
    base = a.root or tempfile.mkdtemp(prefix="pxdtest-mu-")
    run_all(base)
    print(f"passed={passed} failed={failed} (root {base})")
    sys.exit(1 if failed else 0)
