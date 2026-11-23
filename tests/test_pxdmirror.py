#!/usr/bin/env python3
"""Core tests of pxd-mirror. Offline by default (throwaway root, fake inventory); --online adds downloads of
small public PXD000001 files; --two-node HOSTA HOSTB runs the cross-node catalogue test (needs a --root on a
file system both hosts mount, and ssh between them).

  python3 -I tests/test_pxdmirror.py [--root DIR] [--online] [--two-node HOSTA HOSTB]
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolation   # noqa: E402,F401  (clean PXD_* environment, private XDG_CONFIG_HOME)
from pxdmirror import Mirror, PxdError, UsageError, CorruptRecord   # noqa: E402
from pxdmirror import inventory as inv_mod, convert as cv, util   # noqa: E402

PXD = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from pxdmirror.cli import run; run()", SRC]
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


def cli(root, *args):
    return subprocess.run([*PXD, "--root", root, *args], capture_output=True, text=True)


def tree(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        for f in fns:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def fake_inventory(m, acc, files):
    """files: {name: bytes} -> published inventory generation with PRIDE-style checksums."""
    raw = {"project": {"title": "test"}, "files": [
        {"fileName": n, "fileSizeBytes": len(b), "fileCategory": {"value": "RAW" if n.endswith(".raw") else "OTHER"},
         "publicFileLocations": [{"name": "FTP Protocol", "value": f"ftp://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/{acc}/{n}"}]}
        for n, b in files.items()],
        "checksum": "".join(f"{n}\t{hashlib.md5(b).hexdigest()}\t{len(b)}\n" for n, b in files.items())}
    util.mkdirs(m.ds(acc))
    return inv_mod.publish(m.ds(acc), acc, raw)


def offline(root):
    m = Mirror(root)
    m.init()
    ok("init: dirs + catalogue", all(os.path.isdir(os.path.join(root, d)) for d in (".locks", ".ops", ".work"))
       and os.path.exists(os.path.join(root, "catalog.sqlite")))
    if sys.platform.startswith("linux"):     # BSD/macOS directories ignore setgid (group is inherited anyway)
        ok("init: setgid group dir", os.stat(root).st_mode & 0o2070 == 0o2070, oct(os.stat(root).st_mode))

    # --- paths
    for bad in ("../x", "a/b", ".hidden", "", "x\0y"):
        ok(f"safe_name refuses {bad!r}", raises(lambda: util.safe_name(bad), UsageError))
    ok("safe_rel refuses absolute", raises(lambda: util.safe_rel("/etc/passwd"), UsageError))
    ok("safe_rel refuses ..", raises(lambda: util.safe_rel("raw/../../x"), UsageError))
    ok("bad accession refused", raises(lambda: m.ds("PXD12"), UsageError))
    os.makedirs(os.path.join(root, "PXD999999"), exist_ok=True)
    outside = tempfile.mkdtemp()
    os.symlink(outside, os.path.join(root, "PXD999999", "raw"))
    ok("symlink inside the mirror refused", raises(lambda: util.inside(root, os.path.join(root, "PXD999999", "raw", "x")), PxdError))
    os.unlink(os.path.join(root, "PXD999999", "raw"))
    os.rmdir(os.path.join(root, "PXD999999"))

    # --- locks: no truncation, no symlink following
    victim = os.path.join(outside, "victim.txt")
    open(victim, "w").write("precious")
    lp = os.path.join(root, ".locks", "PXD000002.lock")
    os.symlink(victim, lp)
    ok("lock refuses a symlinked lock file", raises(lambda: m._acc_lock("PXD000002", True).__enter__(), OSError))
    ok("lock did not truncate the symlink target", open(victim).read() == "precious")
    os.unlink(lp)
    lf = os.path.join(root, ".locks", "x.lock")
    open(lf, "w").write("keep")
    with util.flock(lf) as fd:
        ok("lock acquired", fd is not None)
        with util.flock(lf, wait=0.2) as fd2:
            ok("second exclusive lock refused (same file, other fd)", fd2 is None)
    ok("lock file not truncated", open(lf).read() == "keep")

    # --- inventory + publication
    acc = "PXD000003"
    A, B, C = b"A" * 1000, b"B" * 2000, b"C" * 300
    inv = fake_inventory(m, acc, {"a.raw": A, "b.raw": B, "c.mzML": C})
    ok("inventory generation published", inv_mod.load(m.ds(acc), acc)["gen"] == inv["gen"])
    st = util.mkdirs(m.work(acc, "t"))
    s1 = os.path.join(st, "a.raw"); open(s1, "wb").write(A)
    e = m._file_entry(inv, "a.raw", s1, hashlib.md5(A).hexdigest(), "test")
    m.publish(acc, s1, "raw/a.raw", {"kind": "file", "key": "a.raw", "entry": e})
    ok("publish: dest present, staging gone", os.path.exists(m.ds(acc, "raw", "a.raw")) and not os.path.exists(s1))
    ok("publish: no intent left", not [f for f in os.listdir(os.path.join(root, ".ops")) if f.startswith("pub-")])
    ok("publish: manifest records ok + pride-md5", m.manifest(acc)["files"]["a.raw"]["verification"] == "pride-md5")
    s2 = os.path.join(st, "a2"); open(s2, "wb").write(b"X" * 1000)
    e2 = dict(e, md5=hashlib.md5(b"X" * 1000).hexdigest())
    ok("publish: refuses to overwrite different content",
       raises(lambda: m.publish(acc, s2, "raw/a.raw", {"kind": "file", "key": "a.raw", "entry": e2}), PxdError))
    ok("publish: original intact", open(m.ds(acc, "raw", "a.raw"), "rb").read() == A)
    # intent recovery: simulate a crash after link, before unlink/manifest
    s3 = os.path.join(st, "b.raw"); open(s3, "wb").write(B)
    e3 = m._file_entry(inv, "b.raw", s3, hashlib.md5(B).hexdigest(), "test")
    util.mkdirs(m.ds(acc, "raw"))
    os.link(s3, m.ds(acc, "raw", "b.raw"))
    util.write_json(os.path.join(root, ".ops", "pub-crash.json"), {"op": "pub-crash", "kind": "publish", "accession": acc,
                    "rel": "raw/b.raw", "staged": s3, "md5": e3["md5"], "record": {"kind": "file", "key": "b.raw", "entry": e3}})
    rec = m.recover()
    ok("recover completes a half-done publication", any("completed" in r[1] for r in rec)
       and m.manifest(acc)["files"]["b.raw"]["status"] == "ok" and not os.path.exists(s3), rec)

    # --- query / dry-run / exit codes never write
    before = tree(root)
    q = m.query(acc)
    ok("query partial (2 of 3)", q["state"] == "partial" and q["files_ok"] == 2, q)
    r = cli(root, "query", acc)
    ok("cli query exit 10 for partial", r.returncode == 10, r.stdout + r.stderr)
    ok("cli query exit 11 for absent", cli(root, "query", "PXD000099").returncode == 11)
    ok("cli usage error exit 2", cli(root, "query", "PXD12").returncode == 2)
    ok("query/list did not write", tree(root) == before)
    dr = m.get(acc, ["a.raw", "c.mzML"], dry_run=True)
    ok("get --dry-run reports, no writes", dict((n, r) for n, r, _ in dr) == {"a.raw": "skip", "c.mzML": "would-download"}
       and tree(root) == before, dr)
    # adoption: an untracked copy with the PRIDE md5 is adopted; one with other bytes would be quarantined
    open(m.ds(acc, "raw", "c.mzML"), "wb").write(C)
    dr = m.get(acc, ["c.mzML"], dry_run=True)
    ok("dry run: would adopt matching untracked file", dr[0][1] == "would-adopt" and tree(root) != before)
    before = tree(root)
    ok("query complete after placing c (not yet recorded: still partial)", m.query(acc)["state"] == "partial")
    res = m.get(acc, ["c.mzML"], method="ftp")
    ok("get adopts a matching untracked file without download", res[0][1] == "skip" and "adopted" in res[0][2], res)
    ok("query complete", m.query(acc)["state"] == "complete")
    ok("cli query exit 0 for complete", cli(root, "query", acc).returncode == 0)

    # --- corrupt manifest: reported, never reset
    mp = m.ds(acc, "manifest.json")
    good = open(mp).read()
    open(mp, "w").write("{torn")
    ok("corrupt manifest: query raises CorruptRecord", raises(lambda: m.query(acc), CorruptRecord))
    ok("corrupt manifest: left as it was", open(mp).read() == "{torn")
    r = cli(root, "query", acc)
    ok("corrupt manifest: cli exit 3", r.returncode == 3, r.stderr)
    open(mp, "w").write(good)

    # --- conversion recipes: distinct recipes never share a path
    spec = {"argv": {"mzML": ["-in", "{in}", "-out", "{out}", "-x", "1"]}}
    i1 = {"version": "1.0", "deployment": {"lib": "aa"}}
    i2 = {"version": "1.0", "deployment": {"lib": "bb"}}
    spec2 = {"argv": {"mzML": ["-in", "{in}", "-out", "{out}", "-x", "2"]}}
    r1, r2, r3 = cv.recipe("t", i1, spec, "mzML"), cv.recipe("t", i2, spec, "mzML"), cv.recipe("t", i1, spec2, "mzML")
    ok("recipe differs by deployment and by arguments", len({r1, r2, r3}) == 3, (r1, r2, r3))
    tools = {"trfp": {"argv": {"mzML": []}, "refused_unless": "allow_trfp2", "default_for": {}}}
    ok("TRFP refused by default", raises(lambda: cv.pick(tools, "x.raw", "mzML", "trfp"), UsageError))
    ok("TRFP allowed with --allow-trfp2", cv.pick(tools, "x.raw", "mzML", "trfp", True)[0] == "trfp")

    # --- mzML validator
    good_mzml, bad_mzml = os.path.join(outside, "g.mzML"), os.path.join(outside, "b.mzML")
    write_mzml(good_mzml, corrupt=False)
    write_mzml(bad_mzml, corrupt=True)
    vg, vb = cv.validate("mzML", good_mzml), cv.validate("mzML", bad_mzml)
    ok("validator accepts a correct index", vg["ok"] and vg["indexed_spectra"] == 2, vg)
    ok("validator rejects shifted offsets", not vb["ok"] and vb["bad_offsets"], vb)
    import zipfile
    zp = os.path.join(outside, "x.mzpeak")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("spectra_data.parquet", b"PAR1xxxxPAR1")
        z.writestr("mzpeak_index.json", "{}")
    ok("mzPeak structural check", cv.validate("mzPeak", zp)["ok"])

    # --- delete: needs a selector, prints exact list, token bound
    ok("delete without selector refused", raises(lambda: m.delete_plan(acc), UsageError))
    ok("delete with empty --files refused", raises(lambda: m.delete_plan(acc, files=[]), UsageError))
    r = cli(root, "delete", acc, "--files", "b.raw")
    ok("cli delete without --yes lists, deletes nothing", r.returncode == 0 and "raw/b.raw" in r.stdout
       and "nothing deleted" in r.stdout and os.path.exists(m.ds(acc, "raw", "b.raw")), r.stdout)
    token = r.stdout.split("token ")[1].split()[0]
    ok("cli delete with a wrong token refused", cli(root, "delete", acc, "--files", "b.raw", "--yes", "--confirm", "nope").returncode == 2)
    r = cli(root, "delete", acc, "--files", "b.raw", "--yes", "--confirm", token)
    ok("cli delete with the token deletes exactly the listed file", r.returncode == 0 and not os.path.exists(m.ds(acc, "raw", "b.raw"))
       and os.path.exists(m.ds(acc, "raw", "a.raw")), r.stdout + r.stderr)
    ok("delete leaves a tombstone in manifest and event log", m.manifest(acc)["files"]["b.raw"]["status"] == "deleted"
       and '"delete"' in open(os.path.join(root, ".events.jsonl")).read())

    # --- migrate: plan, re-verify at apply, symlink back, never moves non-matching files
    src = tempfile.mkdtemp(dir=os.path.dirname(root))     # same file system as the mirror (else EXDEV, by design)
    d = os.path.join(src, "proj", acc)
    os.makedirs(d)
    open(os.path.join(d, "b.raw"), "wb").write(B)              # matches PRIDE -> move
    open(os.path.join(d, "local_result.mzML"), "wb").write(b"mine")   # not in PRIDE -> stays
    open(os.path.join(d, "a.raw"), "wb").write(A)              # already mirrored -> duplicate
    before_src = tree(src)
    plan = m.migrate_plan([src])
    ok("migrate plan wrote only the plan file", tree(src) == before_src)
    rows = [l.split("\t") for l in open(plan["plan"]) if not l.startswith(("#", "class"))]
    cls = {r[2]: r[0] for r in rows}
    ok("plan: move / duplicate / local file not listed", cls == {"b.raw": "move", "a.raw": "duplicate"}, cls)
    # a changed source after planning is left in place
    plan_txt = open(plan["plan"]).read()
    open(os.path.join(d, "b.raw"), "ab").write(b"")             # no change in content...
    os.utime(os.path.join(d, "b.raw"), ns=(1, 1))                # ...but identity (mtime) changed
    res = m.migrate_apply(plan["plan"])
    ok("apply refuses a source changed since the plan", res[0][2] == "left" and os.path.isfile(os.path.join(d, "b.raw"))
       and not os.path.islink(os.path.join(d, "b.raw")), res)
    plan = m.migrate_plan([src])
    res = m.migrate_apply(plan["plan"])
    ok("apply moves the matching file", res[0][2] == "moved" and open(m.ds(acc, "raw", "b.raw"), "rb").read() == B, res)
    ok("apply leaves a symlink at the old path", os.path.islink(os.path.join(d, "b.raw"))
       and os.path.realpath(os.path.join(d, "b.raw")) == os.path.realpath(m.ds(acc, "raw", "b.raw")))
    ok("apply never touched the local file or the duplicate", open(os.path.join(d, "local_result.mzML"), "rb").read() == b"mine"
       and os.path.isfile(os.path.join(d, "a.raw")) and not os.path.islink(os.path.join(d, "a.raw")))
    ok("migrated file recorded with its origin", m.manifest(acc)["files"]["b.raw"].get("migrated_from") == os.path.join(d, "b.raw"))

    # --- import-legacy: derived mzML of a pride_get data set, after its raw file was migrated
    lg = os.path.join(src, "legacy", acc)
    os.makedirs(os.path.join(lg, "mzML"))
    md5 = lambda b: hashlib.md5(b).hexdigest()
    good = b'<?xml version="1.0"?><mzML><run><spectrumList count="0"></spectrumList></run></mzML>'
    bad = b'<?xml version="1.0"?><indexedmzML><mzML><run><spectrumList count="1"><spectrum id="s1"/></spectrumList></run></mzML><indexListOffset>999999</indexListOffset></indexedmzML>'
    files = {"b.mzML": good, "b.profile.mzML": bad, "x.mzML": good, "c.mzML": good}
    for n, b in files.items():
        open(os.path.join(lg, "mzML", n), "wb").write(b)
    der = {f"mzML/{n}": {"source": "b.raw", "source_md5": md5(B), "converter": "ThermoRawFileParser", "converter_version": "2.0.0",
                         "args": "-f=2" if n != "b.profile.mzML" else "-f=0", "md5": md5(b), "host": "node1", "created": "2026-11-18T00:00:00Z"}
           for n, b in files.items()}
    der["mzML/x.mzML"]["md5"] = "0" * 32                     # recorded md5 does not match -> left
    der["mzML/c.mzML"]["source"] = "c.raw"                   # source not archived -> left
    json.dump({"derived": der, "files": {}}, open(os.path.join(lg, "manifest.json"), "w"))
    before_lg = tree(lg)
    res = {r[1]: r[2] for r in m.import_legacy(lg, plan_only=True)}
    ok("import-legacy --plan writes nothing", tree(lg) == before_lg and res["mzML/b.mzML"] == "would-import", res)
    res = {r[1]: r for r in m.import_legacy(lg)}
    ok("import-legacy imports a valid legacy mzML", res["mzML/b.mzML"][2] == "imported" and os.path.islink(os.path.join(lg, "mzML", "b.mzML")), res)
    ok("import-legacy keeps a bad-index mzML but flags it", res["mzML/b.profile.mzML"][2] == "imported-index-invalid", res)
    ok("import-legacy leaves a md5 mismatch and a missing source", res["mzML/x.mzML"][2] == "left" and res["mzML/c.mzML"][2] == "left"
       and not os.path.islink(os.path.join(lg, "mzML", "x.mzML")), res)
    convs = {c["output_path"]: c for c in m.manifest(acc)["conversions"].values()}
    rels = [r[3] for r in res.values() if r[2].startswith("imported")]
    ok("legacy recipes differ by args (centroid vs profile)", len({os.path.dirname(x) for x in rels}) == 2, rels)
    ok("legacy provenance recorded", all(convs[x]["tool"]["name"] == "ThermoRawFileParser" and convs[x]["host"] == "node1" for x in rels)
       and convs[res["mzML/b.profile.mzML"][3]]["status"] == "index_invalid")
    bad_rel = res["mzML/b.profile.mzML"][3]
    inv = [x[2] for x in m.delete_plan(acc, derived_only=True, status="index_invalid")["items"]]
    okp = [x[2] for x in m.delete_plan(acc, derived_only=True)["items"]]
    ok("delete --status index_invalid lists only the bad-index conversions", inv == [bad_rel] and bad_rel not in okp, (inv, okp))
    nm = os.path.join(src, "legacy-nomanifest", acc); os.makedirs(nm)
    ok("import-legacy skips a data set without a legacy manifest", m.import_legacy(nm)[0][2] == "left")
    ok("import-legacy is idempotent", {r[2] for r in m.import_legacy(lg) if r[1] in ("mzML/b.mzML", "mzML/b.profile.mzML")} <= {"exists", "left"})

    # --- reindex in place, catalogue matches manifests
    r = m.reindex()
    rows = m.cat.read("SELECT name, status FROM files WHERE accession=?", (acc,))
    ok("reindex rebuilds the catalogue from manifests", r["datasets"] >= 1 and {x["name"]: x["status"] for x in rows}
       == {n: f["status"] for n, f in m.manifest(acc)["files"].items()}, (r, rows))
    shutil.rmtree(src); shutil.rmtree(outside)


def globus_offline(root):
    """Globus paths with the CLI mocked (no network, no transfers): a status query that times out must not crash
    get, and a get after an interrupted one must adopt the earlier task's inbox instead of submitting again."""
    import types
    from pxdmirror import transfer as tx
    m = Mirror(root)
    m.init()
    acc = "PXD000777"
    files = {"g1.raw": b"G1" * 500, "g2.raw": b"G2" * 700}
    fake_inventory(m, acc, files)
    state = {"show": "ACTIVE", "submits": [], "on_wait": None}

    def run(cmd, **kw):
        if cmd[1:3] == ["task", "show"]:
            if state["show"] == "TIMEOUT":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, state["show"] + "\n", "")
        if cmd[1:3] == ["task", "wait"] and state["on_wait"]:
            state["on_wait"]()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def submit(pairs, label):
        state["submits"].append([d for _, d in pairs])
        return f"T{len(state['submits'])}"

    saved = (tx.subprocess, tx.globus_usable, tx.globus_submit)
    tx.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                          CompletedProcess=subprocess.CompletedProcess)
    tx.globus_usable, tx.globus_submit, m.margin = (lambda root: None), submit, 0
    ops = os.path.join(root, ".ops")
    intents = lambda: [json.load(open(os.path.join(ops, f))) for f in os.listdir(ops)]
    try:
        # bug 1: task show times out (after task wait was interrupted) -> unknown, resumable, not a crash
        state["show"] = "TIMEOUT"
        try:
            res, err = m.get(acc, ["g1.raw"], method="globus"), None
        except Exception as e:
            res, err = None, e
        ok("globus: a timed-out status query does not crash get", err is None and res and res[0][1] == "not_attempted", (res, repr(err)))
        its = intents()
        ok("globus: unknown task status keeps the intent (resumable)", len(its) == 1 and its[0]["task"] == "T1", its)
        try:
            rec, err = m.recover(), None
        except Exception as e:
            rec, err = None, e
        ok("globus: recover survives a timed-out status query", err is None and rec and "UNKNOWN" in rec[0][1], (rec, repr(err)))

        # bug 2: the earlier task's files arrive; recover + get must publish them, not submit again
        inbox = its[0]["inbox"]
        open(os.path.join(inbox, "g1.raw"), "wb").write(files["g1.raw"])
        state["show"] = "SUCCEEDED"
        m.recover()
        res = m.get(acc, ["g1.raw"], method="globus")
        raw = m.ds(acc, "raw", "g1.raw")
        ok("globus: get after an interrupted get adopts the earlier task (no new submit)",
           len(state["submits"]) == 1 and res[0][1:] == ("ok", "globus"), (state["submits"], res))
        ok("globus: adopted inbox published (link+unlink), intent and inbox removed",
           os.path.isfile(raw) and open(raw, "rb").read() == files["g1.raw"] and not intents() and not os.path.exists(inbox)
           and m.manifest(acc)["files"]["g1.raw"]["method"] == "globus", (intents(), os.path.exists(inbox)))

        # bug 2b: an earlier task still running is waited on, not resubmitted
        state["show"] = "ACTIVE"
        res = m.get(acc, ["g2.raw"], method="globus")
        ok("globus: task still active -> not_attempted, intent kept", res[0][1] == "not_attempted" and len(intents()) == 1, res)
        inbox2 = intents()[0]["inbox"]

        def arrive():
            open(os.path.join(inbox2, "g2.raw"), "wb").write(files["g2.raw"])
            state["show"] = "SUCCEEDED"
        state["on_wait"] = arrive
        res = m.get(acc, ["g2.raw"], method="globus")
        ok("globus: get waits on the still-running earlier task instead of submitting again",
           len(state["submits"]) == 2 and res[0][1:] == ("ok", "globus") and not intents()
           and os.path.isfile(m.ds(acc, "raw", "g2.raw")), (state["submits"], res, intents()))
        ev = [json.loads(l) for l in open(os.path.join(root, ".events.jsonl"))]
        ok("globus: publications logged as events", sum(e.get("action") == "publish" and e.get("accession") == acc for e in ev) == 2,
           [e.get("action") for e in ev])
    finally:
        tx.subprocess, tx.globus_usable, tx.globus_submit = saved


def globus_submit_timeout():
    """A submit that times out adopts the task its label names; without one it raises PxdError (FTP fallback)."""
    import types
    from pxdmirror import transfer as tx
    state = {"listed": "T9\n", "calls": []}

    def run(cmd, **kw):
        state["calls"].append(cmd[1:3])
        if cmd[1] == "transfer":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if cmd[1:3] == ["task", "list"]:
            return subprocess.CompletedProcess(cmd, 0, state["listed"], "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")
    saved = tx.subprocess
    tx.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired,
                                          CompletedProcess=subprocess.CompletedProcess)
    try:
        ok("globus submit timeout: the task named by its label is adopted",
           tx.globus_submit([("/s", "/d")], "pxdstore X 1 files op1") == "T9", state["calls"])
        state["listed"] = "None\n"
        ok("globus submit timeout without a task: PxdError, not a crash",
           raises(lambda: tx.globus_submit([("/s", "/d")], "pxdstore X 1 files op2"), PxdError))
    finally:
        tx.subprocess = saved


def write_mzml(path, corrupt):
    head = '<?xml version="1.0"?>\n<indexedmzML><mzML><run><spectrumList count="2">\n'
    sp = ['<spectrum index="0" id="scan=1"></spectrum>\n', '<spectrum index="1" id="scan=2"></spectrum>\n']
    body = head
    offs = []
    for s in sp:
        offs.append(len(body.encode()))
        body += s
    body += "</spectrumList></run></mzML>\n"
    if corrupt:
        offs = [o + 7 for o in offs]
    il = len(body.encode())
    body += '<indexList count="1"><index name="spectrum">\n' + "".join(
        f'<offset idRef="scan={i + 1}">{o}</offset>\n' for i, o in enumerate(offs)) + "</index></indexList>\n"
    body += f"<indexListOffset>{il}</indexListOffset>\n</indexedmzML>\n"
    open(path, "w").write(body)


def online(root):
    m = Mirror(root)
    m.init()
    acc = "PXD000001"
    r = cli(root, "get", acc, "--files", "erwinia_carotovora.fasta", "PXD000001_community_annotated.sdrf.tsv", "--method", "https")
    ok("online: https get of two files", r.returncode == 0 and r.stdout.count("\tok\t") == 2, r.stdout + r.stderr)
    r = cli(root, "get", acc, "--files", "erwinia_carotovora.fasta", "--method", "ftp")
    ok("online: second get skips", r.returncode == 0 and "\tskip\t" in r.stdout, r.stdout)
    r = cli(root, "get", acc, "--files", "PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz", "--method", "ftp")
    q = m.query(acc, files=True)
    ok("online: checksumless file recorded with a local baseline",
       q["files"].get("PRIDE_Exp_Complete_Ac_22134.pride.mztab.gz", {}).get("verification") == "local-baseline", (r.stdout, r.stderr))
    r = subprocess.run([*PXD, "--root", root, "get", acc, "--files", "F063721.dat", "--method", "https",
                        "--max-attempts", "2", "--backoff", "0.1"], capture_output=True, text=True,
                       env=dict(os.environ, PXD_PRIDE_HTTPS_BASE="https://127.0.0.1:9/"))
    ok("online: unreachable server -> failed, exit 1, explicit method does not fall back", r.returncode == 1 and "\tfailed\t" in r.stdout
       and "F063721.dat" not in m.manifest(acc)["files"], r.stdout + r.stderr)
    r = cli(root, "query", acc)
    ok("online: query partial exit 10", r.returncode == 10, r.stdout)
    r = cli(root, "stage", acc, "--files", "erwinia_carotovora.fasta", "--to", os.path.join(root, "..", "stage-test"))
    ok("online: stage copies with relative path", r.returncode == 0 and os.path.exists(os.path.join(root, "..", "stage-test", "raw", "erwinia_carotovora.fasta")), r.stdout + r.stderr)
    r = cli(root, "stage", acc, "--files", "erwinia_carotovora.fasta", "--to", os.path.join(root, "..", "stage-test"))
    ok("online: stage again skips", "skip" in r.stdout, r.stdout)


TWO = r'''
import os, sys, time, sqlite3
sys.path.insert(0, sys.argv[5])
from pxdmirror.catalog import Catalog
root, tag, n, die = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "die"
c = Catalog(root)
def w(con, i):
    con.execute("INSERT INTO events(time,user,host,action,accession,detail) VALUES(?,?,?,?,?,?)", ("t", "u", tag, "test", None, str(i)))
    if die and i == n // 2:
        os._exit(9)          # killed inside an open write transaction
for i in range(n):
    c.write(lambda con: w(con, i))
print("done", tag)
'''


def on(host, cmd, **kw):
    """Run cmd on host: directly when it is this node (node-to-node ssh to oneself needs a key we lack)."""
    me = __import__("socket").gethostname().split(".")[0]
    argv = ["bash", "-c", cmd] if host.split(".")[0] == me else ["ssh", "-o", "BatchMode=yes", host, cmd]
    return argv


def two_node(root, a, b):
    m = Mirror(root)
    m.init()
    prog = os.path.join(root, ".two_node.py")
    open(prog, "w").write(TWO)
    pa = subprocess.Popen(on(a, f"python3 -I {prog} {root} {a} 200 live {SRC}"), stdout=subprocess.PIPE, text=True)
    pb = subprocess.Popen(on(b, f"python3 -I {prog} {root} {b} 200 live {SRC}"), stdout=subprocess.PIPE, text=True)
    oa, ob = pa.communicate(timeout=900), pb.communicate(timeout=900)
    rows = m.cat.read("SELECT host, count(*) n FROM events WHERE action='test' GROUP BY host")
    ok("two nodes: 2 x 200 concurrent catalogue writes all committed", {r["host"]: r["n"] for r in rows} == {a: 200, b: 200}, (rows, oa, ob))
    pk = subprocess.run(on(a, f"python3 -I {prog} {root} {a}-k 40 die {SRC}"), capture_output=True, text=True)
    pr = subprocess.run(on(b, f"python3 -I {prog} {root} {b}-r 20 live {SRC}"), capture_output=True, text=True)
    rows = {r["host"]: r["n"] for r in m.cat.read("SELECT host, count(*) n FROM events WHERE action='test' GROUP BY host")}
    ok("two nodes: writer killed mid-transaction rolls back (20 committed, the 21st not), the other node recovers and writes",
       rows.get(f"{a}-k") == 20 and rows.get(f"{b}-r") == 20 and pr.returncode == 0, (rows, pk.returncode, pr.stderr[-300:]))
    ok("two nodes: integrity_check ok", m.cat.read("PRAGMA integrity_check")[0].get("integrity_check") == "ok")
    os.unlink(prog)


def test_offline_suite(tmp_path):
    """pytest entry point: the offline and mocked-Globus checks."""
    offline(str(tmp_path / "offline"))
    globus_offline(str(tmp_path / "globus"))
    globus_submit_timeout()
    assert failed == 0, f"{failed} checks failed (see the FAIL lines)"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--two-node", nargs=2)
    a = ap.parse_args()
    base = a.root or tempfile.mkdtemp(prefix="pxdtest-")
    offline(os.path.join(base, "offline"))
    globus_offline(os.path.join(base, "globus"))
    globus_submit_timeout()
    if a.online:
        online(os.path.join(base, "online"))
    if a.two_node:
        two_node(os.path.join(base, "twonode"), *a.two_node)
    print(f"passed={passed} failed={failed} (root {base})")
    sys.exit(1 if failed else 0)
