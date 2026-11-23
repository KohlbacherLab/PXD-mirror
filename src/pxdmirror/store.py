"""Mirror: a shared mirror of public PRIDE data sets (layout and rules: docs/DESIGN.md).

Authoritative records: <PXD>/manifest.json (files, conversions), <PXD>/metadata/<gen>/ + CURRENT (inventory),
<root>/.events.jsonl (history incl. deletions). catalog.sqlite is a projection, rebuilt by reindex.
Every publication goes through Mirror.publish (intent in .ops -> link no-overwrite -> unlink staging ->
manifest -> catalogue -> event -> intent removed); recover() finishes or reports leftover intents.
"""
import concurrent.futures, errno, glob, grp, hashlib, json, os, pwd, random, re, shlex, shutil, socket, stat, subprocess, tempfile, time, zipfile

from . import convert as cv, inventory as inv_mod, transfer as tx, util as U
from .catalog import Catalog
from .config import Config
from .util import (ACC_RE, own_mode, current_umask, CapacityError, CorruptRecord, PxdError, UsageError, append_line, atomic_write, capacity_guard,
                   check_acc, flock, fsync_dir, inside, log, md5sum, mkdirs, now, opid, private_dir, quota_free, read_json,
                   read_own_json, replaceable, safe_name, safe_rel, user, write_json)

README = """# {name}

Public PXD data sets, one copy for everyone. Do not edit by hand: use the `pxd` tool of pxd-mirror
(https://github.com/KohlbacherLab/PXD-mirror; `pxd --help`, `pxd config`).

  PXDnnnnnn/raw/            files exactly as PRIDE serves them
  PXDnnnnnn/mzML/<recipe>/  converted mzML (recipe = tool-version-argshash)
  PXDnnnnnn/mzPeak/<recipe>/
  PXDnnnnnn/metadata/       PRIDE inventory generations; CURRENT names the one in use
  PXDnnnnnn/manifest.json   per-file and per-conversion records (authoritative)
  catalog.sqlite            query index (rebuildable: pxd reindex)
  .events.jsonl             history (downloads, conversions, deletions, migrations)
  .locks .ops .work         tool internals
"""


def _stem(name):
    for x in (".d.zip", ".raw", ".mzml.gz", ".mzml", ".mzxml", ".wiff", ".d", ".zip"):
        if name.lower().endswith(x):
            return name[: -len(x)]
    return os.path.splitext(name)[0]


def _under(path, prefixes):
    rp = os.path.realpath(path)
    for p in prefixes:
        q = os.path.realpath(p).rstrip("/") or "/"
        if rp == q or rp.startswith(q.rstrip("/") + "/"):
            return p
    return None


class Mirror:
    def __init__(self, root=None, config=None, lock_wait=None):
        """@p config: a Config (default: Config() from the files and environment); @p root overrides paths.root.
        Applies the configured file modes, umask, PRIDE endpoints, Globus and converter settings process-wide."""
        self.cfg = cfg = config if config is not None else Config(cli={("paths", "root"): root})
        root = root or cfg.get("paths", "root")
        if not root:
            raise UsageError("no mirror root configured: set paths.root in the config file, $PXD_ROOT or --root "
                             "(see pxd config and docs/CONFIGURATION.md)")
        from .config import expand
        self.root = os.path.abspath(expand(root))
        bad = _under(self.root, cfg.get("paths", "forbidden_roots"))
        if bad:
            raise UsageError(f"{self.root} is under the forbidden root {bad} (paths.forbidden_roots)")
        U.set_modes(*cfg.modes())
        os.umask(cfg.umask())
        inv_mod.configure(cfg.get("pride", "api"), cfg.get("pride", "ftp_base"), cfg.get("pride", "https_base"))
        tx.SPLIT_MIN = cfg.get("transfer", "split_min_mb") * 1e6
        tx.configure_globus(enabled=cfg.get("globus", "enabled"),
                            cli=cfg.get("globus", "cli"), source=cfg.get("globus", "source_collection"),
                            dest=cfg.get("globus", "dest_collection"), kind=cfg.get("globus", "kind"),
                            path_map=cfg.get("globus", "path_map"), gcp_config_dir=cfg.get("globus", "gcp_config_dir"),
                            check_config_paths=cfg.get("globus", "check_config_paths"),
                            shared_prefixes=cfg.get("globus", "shared_prefixes"))
        cv.TOOLS_JSON = cfg.get("paths", "tools")
        self.margin = cfg.get("limits", "quota_margin_gb") * 1e9
        self.lock_wait = cfg.get("limits", "lock_wait_s") if lock_wait is None else lock_wait
        self.cat = Catalog(self.root)

    # ---------------------------------------------------------------- paths
    def ds(self, acc, *parts):
        check_acc(acc)
        return inside(self.root, os.path.join(self.root, acc, *[safe_name(p) for p in parts]))

    def work(self, acc, *parts):
        return inside(self.root, os.path.join(self.root, ".work", check_acc(acc), *parts))

    def _acc_lock(self, acc, exclusive):
        return flock(os.path.join(self.root, ".locks", check_acc(acc) + ".lock"), exclusive, self.lock_wait)

    def _file_lock(self, acc, key, wait=0.0):
        return flock(os.path.join(self.root, ".locks", acc, safe_name(key) + ".lock"), True, wait)

    def _refuse_forbidden(self, path, what):
        """UsageError if @p path (symlinks resolved) is under one of paths.forbidden_roots."""
        bad = _under(path, self.cfg.get("paths", "forbidden_roots"))
        if bad:
            raise UsageError(f"{what} {path} is under the forbidden root {bad} (paths.forbidden_roots)")

    def _need_root(self):
        if not os.path.isdir(os.path.join(self.root, ".ops")):
            raise PxdError(f"{self.root} is not an initialised mirror (pxd init)")

    # ---------------------------------------------------------------- init
    def init(self):
        group = self.cfg.get("permissions", "group")
        for d in ("", ".locks", ".ops", ".work"):
            p = mkdirs(os.path.join(self.root, d))
            if group and os.stat(p).st_uid == os.getuid():
                try:
                    os.chown(p, -1, grp.getgrnam(group).gr_gid)
                except (KeyError, OSError) as e:
                    raise PxdError(f"cannot give {p} the group {group}: {e}")
            own_mode(p, U.DIR_MODE)
        if self.cfg.get("permissions", "shared") and self.cfg.get("permissions", "default_acl") and shutil.which("setfacl"):
            for d in ("", ".locks", ".ops", ".work"):
                r = subprocess.run(["setfacl", "-m", "g::rwx,o::---", "-d", "-m", "g::rwx,o::---",
                                    os.path.join(self.root, d)], capture_output=True, text=True)
                if r.returncode:
                    log(f"setfacl on {os.path.join(self.root, d)} failed ({r.stderr.strip()[:100]}); "
                        "relying on explicit modes and the umask")
        p = os.path.join(self.root, "README.md")
        if not os.path.exists(p):
            atomic_write(p, README.replace("{name}", self.cfg.get("site", "name")))
        self.cat.write(lambda con: None)
        return {"root": self.root, "mode": oct(os.stat(self.root).st_mode & 0o7777)}

    # ---------------------------------------------------------------- records
    def manifest(self, acc):
        m = read_json(self.ds(acc, "manifest.json"), "manifest")
        if m is None:
            return {"schema": 2, "accession": acc, "files": {}, "conversions": {}}
        if not isinstance(m, dict) or not isinstance(m.get("files"), dict) or not isinstance(m.get("conversions", {}), dict):
            raise CorruptRecord(f"manifest {self.ds(acc, 'manifest.json')} has an unexpected structure")
        m.setdefault("conversions", {})
        return m

    def _update_manifest(self, acc, fn):
        with flock(os.path.join(self.root, ".locks", acc, "manifest.lock"), True, 300) as fd:
            if fd is None:
                raise PxdError(f"{acc}: manifest busy")
            mkdirs(self.ds(acc))
            m = self.manifest(acc)          # CorruptRecord propagates: never overwrite an unreadable record
            out = fn(m)
            m["updated"] = now()
            write_json(self.ds(acc, "manifest.json"), m)
            return out

    def _event(self, action, acc, detail):
        ev = {"time": now(), "user": user(), "host": socket.gethostname().split(".")[0], "action": action,
              "accession": acc, "detail": detail}
        append_line(os.path.join(self.root, ".events.jsonl"), json.dumps(ev, sort_keys=True))
        return ev

    def _sync(self, acc, ev=None):
        """Project one data set (and an event) into the catalogue. The records are read inside the catalogue
        transaction, so a slower writer can never commit an older snapshot over a newer one."""
        def fn(con):
            try:
                inv = inv_mod.load(self.ds(acc), acc)
                man = self.manifest(acc)
            except CorruptRecord as e:
                log(f"catalogue not updated for {acc}: {e}")
                return
            Catalog.put_dataset(con, acc, man, inv)
            if ev:
                Catalog.put_event(con, ev)
        self.cat.write(fn)

    def inventory(self, acc, fetch=True):
        """Current inventory generation; fetched and published on first use."""
        cur = inv_mod.load(self.ds(acc), acc)
        if cur or not fetch:
            return cur
        raw = inv_mod.fetch(acc)
        mkdirs(self.ds(acc))
        snap = inv_mod.publish(self.ds(acc), acc, raw)
        self._sync(acc, self._event("inventory", acc, {"gen": snap["gen"], "files": len(snap["files"])}))
        return snap

    def refresh(self, acc):
        with self._acc_lock(acc, False) as fd:
            if fd is None:
                raise PxdError(f"{acc}: busy")
            raw = inv_mod.fetch(acc)
            mkdirs(self.ds(acc))
            snap = inv_mod.publish(self.ds(acc), acc, raw)
            self._sync(acc, self._event("inventory", acc, {"gen": snap["gen"], "files": len(snap["files"])}))
            return snap

    # ---------------------------------------------------------------- publication (C1)
    def _own_copy(self, acc, staged, want):
        """Copy another user's staged file (e.g. a Globus delivery written 640 by the endpoint owner) into a
        private work file of ours and verify it. The original stays until the copy is published."""
        cw = mkdirs(self.work(acc, "adopt", opid("cp")))
        cpy = os.path.join(cw, os.path.basename(staged))
        try:
            with open(staged, "rb") as src, open(cpy, "xb") as dst:
                shutil.copyfileobj(src, dst, 8 << 20)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError as e:
            shutil.rmtree(cw, ignore_errors=True)
            capacity_guard(e)
        if want and md5sum(cpy) != want:
            shutil.rmtree(cw, ignore_errors=True)
            raise PxdError(f"copy of {staged} does not match {want}; original left in place")
        return cpy

    @staticmethod
    def _intent_mode():
        """Intents are readable for the group (recover reports them) but writable only by their owner: an intent
        is acted on only by the user who wrote it (read_own_json)."""
        return U.FILE_MODE & ~0o022

    def _intent_lock_path(self, oid):
        return os.path.join(self.root, ".locks", "ops", safe_name(oid) + ".lock")

    def _intent_lock(self, oid):
        """Held by whoever is working on intent @p oid (a live publish or a recover), so no two do."""
        return flock(self._intent_lock_path(oid), True, 0)

    def _drop_intent(self, ipath, oid):
        os.unlink(ipath)
        try:
            os.unlink(self._intent_lock_path(oid))     # whoever opens it later finds the intent gone
        except OSError:
            pass

    def publish(self, acc, staged, rel, record, backlink=None, allow_copy=False):
        """Publish `staged` (verified by the caller, same file system) at <acc>/<rel> without overwriting.
        record = {"kind": "file"|"conversion", "key": name|id, "entry": {...}} goes into the manifest.
        @p allow_copy: a staged file owned by another user that we may not hard-link (protected_hardlinks) is
        first copied to a private file of ours (used for Globus deliveries); the original is removed afterwards."""
        check_acc(acc)
        safe_rel(rel)
        dest = inside(self.root, os.path.join(self.root, acc, rel))
        mkdirs(os.path.dirname(dest))
        original = None
        if allow_copy and os.stat(staged).st_uid != os.getuid():
            original, staged = staged, self._own_copy(acc, staged, record["entry"].get("md5") or record["entry"].get("output_md5"))
        fd = os.open(staged, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        oid = opid("pub")
        ipath = os.path.join(self.root, ".ops", oid + ".json")
        with self._intent_lock(oid):
            write_json(ipath, {"op": oid, "kind": "publish", "accession": acc, "rel": rel, "staged": staged,
                               "md5": record["entry"].get("md5") or record["entry"].get("output_md5"),
                               "record": record, "backlink": backlink, "created": now(), "host": socket.gethostname(),
                               "user": user()}, self._intent_mode())
            try:
                os.link(staged, dest)
            except FileExistsError:
                want = record["entry"].get("md5") or record["entry"].get("output_md5")
                if not (os.path.isfile(dest) and not os.path.islink(dest) and md5sum(dest) == want):
                    self._drop_intent(ipath, oid)
                    raise PxdError(f"{acc}/{rel} exists with different content; not overwriting (staged copy kept at {staged})")
            except OSError as e:
                self._drop_intent(ipath, oid)
                if e.errno == errno.EXDEV:
                    raise PxdError(f"{staged} and {dest} are in different quota realms/file systems (EXDEV); left in place")
                capacity_guard(e)
            try:
                if os.stat(dest).st_uid == os.getuid():
                    os.chmod(dest, U.FILE_MODE)
                    os.chown(dest, -1, os.stat(self.root).st_gid)
            except OSError:
                pass
            fsync_dir(os.path.dirname(dest))
            work = os.path.join(self.root, ".work") + os.sep
            if os.path.exists(staged) and (os.path.samefile(staged, dest) or os.path.abspath(staged).startswith(work)):
                os.unlink(staged)                     # same inode, or an identical copy in our own staging area
                fsync_dir(os.path.dirname(staged))
            if backlink and not os.path.lexists(backlink) and not os.path.lexists(staged):
                os.symlink(dest, backlink)
            self._apply_record(acc, record)
            self._drop_intent(ipath, oid)
        if original is not None:
            try:
                os.unlink(original)
            except OSError as e:
                log(f"could not remove {original} after publishing a copy ({e}); remove it later")
            if os.path.dirname(os.path.dirname(staged)) == self.work(acc, "adopt"):
                shutil.rmtree(os.path.dirname(staged), ignore_errors=True)
        return dest

    def _apply_record(self, acc, record):
        def put(m):
            (m["files"] if record["kind"] == "file" else m["conversions"])[record["key"]] = record["entry"]
            if record["kind"] == "file" and m.get("status") == "deleted":
                del m["status"]                      # the data set is back
        self._update_manifest(acc, put)
        self._sync(acc, self._event({"file": "publish", "conversion": "convert"}[record["kind"]], acc,
                                    {"key": record["key"], "method": record["entry"].get("method")}))

    def recover(self):
        """Finish or report leftover intents (publications, Globus tasks). Intents are validated first; an intent
        another process works on is skipped."""
        out = []
        for ip in sorted(glob.glob(os.path.join(self.root, ".ops", "*.json"))):
            try:
                it, owner, problem = read_own_json(ip, "intent")
            except (CorruptRecord, OSError) as e:
                out.append((os.path.basename(ip), f"NEEDS ATTENTION: {e}"))
                continue
            if it is None:
                continue                                   # finished meanwhile by another process
            if not isinstance(it, dict):
                out.append((os.path.basename(ip), "NEEDS ATTENTION: intent is not a JSON object"))
                continue
            op = str(it.get("op") or os.path.basename(ip))
            if owner != os.getuid():
                # only the owner acts on an intent: its paths (staging file, symlink back) are trusted only because
                # the owner wrote them; .ops is writable for the whole group
                what = f"globus task {it.get('task')}" if it.get("kind") == "globus" else f"{it.get('kind')} intent"
                out.append((op, f"{what} of {_uname(owner)}: only its owner can finish it (they run pxd recover)"))
                continue
            if problem:
                out.append((op, f"NEEDS ATTENTION: intent {problem}; not acted on (check its content, then chmod 640 "
                                "it to have recover finish it, or remove it)"))
                continue
            try:
                out.append(self._recover_one(ip, it))
            except CapacityError:
                raise
            except (PxdError, OSError, KeyError, TypeError, ValueError) as e:
                out.append((os.path.basename(ip), f"NEEDS ATTENTION: {type(e).__name__}: {str(e)[:200]}"))
        return out

    def _recover_one(self, ip, it):
        op = str(it.get("op") or os.path.basename(ip))
        if it.get("kind") == "globus":
            try:
                left = len(os.listdir(it["inbox"])) if os.path.isdir(it["inbox"]) else 0
            except OSError:
                left = "?"
            st = tx.globus_status(it["task"])
            return (op, f"globus task {st}; {left} files in {it['inbox']} (verified and published by the next get)")
        if it.get("kind") != "publish":
            raise PxdError(f"unknown intent kind {it.get('kind')!r}")
        acc = check_acc(it["accession"])
        rel = safe_rel(it["rel"])
        dest = inside(self.root, os.path.join(self.root, acc, rel))
        rec = it["record"]
        staged, backlink = it["staged"], it.get("backlink")
        entry = rec["entry"]
        origin = entry.get("migrated_from") or entry.get("imported_from")
        in_work = os.path.abspath(staged).startswith(os.path.join(self.root, ".work") + os.sep)
        if not (in_work or staged == origin) or (backlink and backlink != staged):
            raise PxdError(f"intent paths do not belong to this mirror (staged {staged}, backlink {backlink})")
        if not re.fullmatch(r"[0-9a-f]{32}", str(it.get("md5"))):
            raise PxdError("intent without a valid md5")
        with self._acc_lock(acc, False) as al, self._intent_lock(safe_name(op)) as lk:
            if al is None:
                return (op, f"{acc} is busy (a delete holds it); skipped, run recover again later")
            if lk is None:
                return (op, "in progress in another process; skipped")
            if not os.path.exists(ip):
                return (op, "finished meanwhile")
            regular = lambda p: os.path.isfile(p) and not os.path.islink(p)
            if regular(dest) and md5sum(dest) == it["md5"]:
                if regular(staged) and (os.path.samefile(staged, dest) or in_work):
                    os.unlink(staged)
                if backlink and not os.path.lexists(backlink):
                    os.symlink(dest, backlink)
                self._apply_record(acc, rec)
                self._drop_intent(ip, op)
                return (op, f"completed {acc}/{rel}")
            if not os.path.lexists(dest) and regular(staged) and md5sum(staged) == it["md5"]:
                self.publish(acc, staged, rel, rec, backlink)          # writes its own intent first
                self._drop_intent(ip, op)
                return (op, f"republished {acc}/{rel}")
            return (op, f"NEEDS ATTENTION: {acc}/{rel} (dest and staged copy do not match {it['md5']})")

    # ---------------------------------------------------------------- query / list
    def query(self, acc, files=False):
        """Read-only. state: complete | partial | absent (against the current inventory generation)."""
        check_acc(acc)
        if not os.path.isdir(os.path.join(self.root, acc)):
            return {"accession": acc, "state": "absent", "path": None}
        inv = inv_mod.load(self.ds(acc), acc)
        man = self.manifest(acc)
        okf = {n: f for n, f in man["files"].items() if f.get("status") == "ok"}
        present = lambda n: os.path.isfile(os.path.join(self.root, acc, "raw", n))
        want = (inv or {}).get("files", {})
        cks = (inv or {}).get("checksums", {})
        cur = lambda n: (cks.get(n) or (None,))[0] or okf[n].get("md5_pride")   # PRIDE's md5 in the current generation
        good = [n for n in want if n in okf and (not cur(n) or okf[n].get("md5") == cur(n)) and present(n)]
        state = "absent" if not okf else "complete" if want and len(good) == len(want) else "partial"
        if man.get("status") == "deleted" and not okf:
            state = "absent"
        r = {"accession": acc, "state": state, "path": os.path.join(self.root, acc), "inventory_gen": (inv or {}).get("gen"),
             "files_ok": len(okf), "files_pride": len(want), "bytes_ok": sum(f.get("size") or 0 for f in okf.values()),
             "bytes_pride": sum(int(f.get("fileSizeBytes") or 0) for f in want.values()),
             "missing": sorted(set(want) - set(good))[:20] if want else [],
             "conversions": [{"id": k, "source": c["source_name"], "format": c["format"], "recipe": c["recipe"],
                              "path": c.get("output_path"), "status": c.get("status")} for k, c in sorted(man["conversions"].items())],
             "deleted": man.get("status") == "deleted"}
        if files:
            r["files"] = {n: {k: f.get(k) for k in ("status", "size", "verification", "method", "path")} for n, f in sorted(man["files"].items())}
        return r

    def list(self):
        rows = self.cat.read("SELECT accession, status, n_ok, n_files_pride, bytes_ok, updated FROM datasets ORDER BY accession")
        if rows:
            return rows
        return [{"accession": a, **{k: v for k, v in self.query(a).items() if k in ("state", "files_ok", "files_pride", "bytes_ok")}}
                for a in sorted(os.listdir(self.root)) if ACC_RE.match(a)]

    def path(self, acc, name, kind="raw", recipe=None):
        """Path of a raw file or of a conversion output (newest ok one unless a recipe is given)."""
        man = self.manifest(acc)
        if kind == "raw":
            f = man["files"].get(name)
            return self.ds(acc, "raw", name) if f and f.get("status") == "ok" else None
        cs = sorted((c for c in man["conversions"].values() if c["source_name"] == name and c["format"] == kind
                     and c.get("status") == "ok" and (recipe is None or c["recipe"] == recipe)), key=lambda c: c["started"])
        return inside(self.root, os.path.join(self.root, acc, safe_rel(cs[-1]["output_path"]))) if cs else None

    # ---------------------------------------------------------------- get
    def select(self, inv, names=None, category=None, all_=False):
        files = inv["files"]
        if all_:
            return sorted(files)
        sel = list(dict.fromkeys(names or []))
        if category:
            cats = set(category.upper().split(","))
            sel += [n for n, f in sorted(files.items()) if f.get("fileCategory", {}).get("value") in cats]
        if not sel:
            raise UsageError("give --files, --file-list, --category or --all")
        unknown = [n for n in sel if n not in files]
        if unknown:
            raise UsageError(f"not in the PRIDE inventory: {unknown[:5]}")
        return list(dict.fromkeys(sel))

    def _t(self, v, key, sec="transfer"):
        return self.cfg.get(sec, key) if v is None else v

    def get(self, acc, names=None, category=None, all_=False, method="auto", dry_run=False, split=None,
            max_attempts=None, max_total=None, backoff=None, stop_after=None, globus_hours=None):
        """Returns [(name, result, detail)], result in ok|skip|would-download|would-adopt|locked|failed|not_attempted.
        Unset numeric arguments come from the [transfer] / [globus] configuration."""
        split, max_attempts, max_total = self._t(split, "split"), self._t(max_attempts, "max_attempts"), self._t(max_total, "max_total")
        backoff, stop_after = self._t(backoff, "backoff_s"), self._t(stop_after, "stop_after")
        globus_hours = self._t(globus_hours, "wait_hours", "globus")
        check_acc(acc)
        self._need_root()
        if method not in ("auto", "globus", "https", "ftp"):
            raise UsageError(f"unknown method {method}")
        if dry_run:
            inv = inv_mod.load(self.ds(acc), acc) or inv_mod.snapshot(inv_mod.fetch(acc), acc)
            return [(n, *self._fetch_one(acc, inv, n, method, True)) for n in self.select(inv, names, category, all_)]
        with self._acc_lock(acc, False) as fd:
            if fd is None:
                raise PxdError(f"{acc}: busy (a delete holds it)")
            inv = self.inventory(acc)
            sel = self.select(inv, names, category, all_)
            man = self.manifest(acc)
            def stale(n):                               # recorded against an older PRIDE checksum
                want = (inv["checksums"].get(n) or (None,))[0]
                return bool(want) and man["files"][n].get("md5_pride") != want
            todo = [n for n in sel if man["files"].get(n, {}).get("status") != "ok" or not os.path.exists(self.ds(acc, "raw", n))
                    or stale(n)]
            need = sum(inv["checksums"].get(n, (0, int(inv["files"][n].get("fileSizeBytes") or 0)))[1] or 0 for n in todo)
            free, realm = quota_free(self.root)
            if todo and free < need * (2 if method in ("auto", "https") and split > 1 else 1) + self.margin:
                raise CapacityError(f"{realm}: {free / 1e9:.0f} GB free, need {need / 1e9:.0f} GB (x2 for range assembly) + {self.margin / 1e9:.0f} GB margin")
            res, done, blocked = {}, set(), set()
            if (method == "globus" or (method == "auto" and "globus" in self.cfg.get("transfer", "methods"))) and todo:
                done, blocked = self._globus(acc, inv, todo, strict=method == "globus", hours=globus_hours)
                for n in done:
                    res[n] = ("ok", "globus")
            streak = 0
            for n in sel:
                if n in res:
                    continue
                if method == "globus" and n not in todo:
                    res[n] = ("skip", "recorded")
                    continue
                if n in blocked or method == "globus":
                    res[n] = ("not_attempted" if n in blocked else "failed", "globus task still active" if n in blocked else "globus did not deliver it")
                    continue
                if streak >= stop_after:
                    res[n] = ("not_attempted", "circuit breaker")
                    continue
                try:
                    r = self._fetch_one(acc, inv, n, method, False, split, max_attempts, max_total, backoff)
                except CapacityError as e:
                    res[n] = ("failed", str(e))
                    for m in sel:
                        res.setdefault(m, ("not_attempted", "capacity"))
                    raise CapacityError(str(e)) from e
                res[n] = r
                streak = streak + 1 if r[0] == "failed" else 0
            return [(n, *res[n]) for n in sel]

    def _quarantine(self, acc, path, why):
        q = mkdirs(self.work(acc, "quarantine"))
        dst = os.path.join(q, f"{os.path.basename(path)}.{time.time_ns()}.{socket.gethostname()}.{os.getpid()}")
        os.rename(path, dst)
        self._event("quarantine", acc, {"file": os.path.basename(path), "why": why, "to": dst})
        log(f"quarantined {os.path.basename(path)} ({why}) -> {dst}")
        return dst

    def _file_entry(self, inv, name, path, md5, method, attempts=0, url=None, seconds=None, verification=None, extra=None):
        frec = inv["files"][name]
        want = inv["checksums"].get(name, (None, None))[0]
        st = os.stat(path)
        return {"path": f"raw/{name}", "category": frec.get("fileCategory", {}).get("value"), "size": st.st_size,
                "api_size": int(frec.get("fileSizeBytes") or -1), "mtime_ns": st.st_mtime_ns, "md5": md5,
                "md5_pride": want, "verification": verification or ("pride-md5" if want else "local-baseline"),
                "status": "ok", "method": method, "attempts": attempts, "url": url, "downloaded": now(),
                "seconds": seconds, "verified": now(), "inventory_gen": inv["gen"], **(extra or {})}

    def _fetch_one(self, acc, inv, name, method, dry_run, split=4, max_attempts=4, max_total=8, backoff=30.0):
        frec = inv["files"][name]
        want_md5, want_size = inv["checksums"].get(name, (None, None))
        api_size = int(frec.get("fileSizeBytes") or -1)
        final = self.ds(acc, "raw", name)
        man = read_json(self.ds(acc, "manifest.json"), "manifest") if os.path.isdir(os.path.join(self.root, acc)) else None
        entry = ((man or {}).get("files") or {}).get(name, {})
        if dry_run:
            if os.path.exists(final):
                st = os.stat(final)
                if (entry.get("status") == "ok" and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns
                        and (not want_md5 or entry.get("md5_pride") == want_md5)):
                    return ("skip", "recorded")
                return ("would-adopt", "PRIDE md5 matches") if want_md5 and md5sum(final) == want_md5 else ("failed", "existing file does not match PRIDE (would be quarantined)")
            return ("would-download", f"{(want_size or api_size) / 1e6:.1f} MB")
        with self._file_lock(acc, name) as lk:
            if lk is None:
                return ("locked", "another process holds this file")
            replace = None                                  # why the archived copy goes, once a verified one is here
            if os.path.exists(final):
                st = os.stat(final)
                if (entry.get("status") == "ok" and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns
                        and entry.get("md5_pride") == want_md5):
                    return ("skip", "recorded")
                local = md5sum(final)
                if want_md5 and local == want_md5:
                    e = self._file_entry(inv, name, final, local, entry.get("method", "adopted"))
                    self._update_manifest(acc, lambda m: m["files"].__setitem__(name, e))
                    self._sync(acc, self._event("adopt", acc, {"file": name}))
                    return ("skip", "adopted (PRIDE md5)")
                if not want_md5 and entry.get("status") == "ok" and entry.get("md5") == local:
                    return ("skip", "local baseline")
                replace = "does not match PRIDE md5" if want_md5 else "no PRIDE md5 and no recorded baseline"
            urls = inv_mod.urls_for(frec)
            auto = [x for x in self.cfg.get("transfer", "methods") if x != "globus"]
            order = {"auto": auto, "globus": [], "https": ["https"], "ftp": ["ftp"]}[method]
            wd = self.work(acc, "partial", name + ".d")
            if not want_md5 and os.path.exists(wd):
                shutil.rmtree(wd)                          # M5: checksumless objects never resume
            mkdirs(wd)
            part = os.path.join(wd, "data")
            total, last, bad_md5 = 0, "no usable URL", None
            for m in order:
                if m not in urls:
                    continue
                for attempt in range(1, max_attempts + 1):
                    if total >= max_total:
                        break
                    total += 1
                    t0 = time.time()
                    try:
                        if not want_md5 and os.path.exists(part):
                            os.unlink(part)
                        if m == "https" and split > 1 and want_size and want_size >= tx.SPLIT_MIN and not os.path.exists(part):
                            tx.curl_split(urls[m], wd, part, want_size, split, lk)
                        else:
                            tx.curl_one(urls[m], part, want_size or api_size, lk)
                        got = os.path.getsize(part)
                        if want_size is not None and got != want_size:
                            raise tx.Unresumable(f"size {got} != PRIDE {want_size}")
                        local = md5sum(part)
                        if want_md5 and local != want_md5:
                            if local == bad_md5:
                                self._quarantine(acc, part, "md5 mismatch twice")
                                raise tx.Permanent(f"md5 {local} != PRIDE {want_md5} twice")
                            bad_md5 = local
                            raise tx.Unresumable(f"md5 {local} != PRIDE {want_md5}")
                        e = self._file_entry(inv, name, part, local, m, total, urls[m], round(time.time() - t0, 1))
                        if replace and os.path.lexists(final):
                            self._replace_out(acc, name, final, replace)
                        self.publish(acc, part, f"raw/{name}", {"kind": "file", "key": name, "entry": e})
                        shutil.rmtree(wd, ignore_errors=True)
                        log(f"ok {name} via {m} ({got / 1e6:.1f} MB)")
                        return ("ok", m)
                    except CapacityError:
                        raise
                    except tx.Permanent as e:
                        last = f"{m}: {e}"
                        break
                    except tx.Unresumable as e:
                        last = f"{m}: {e}"
                        shutil.rmtree(wd, ignore_errors=True)
                        mkdirs(wd)
                    except OSError as e:
                        if e.errno in (errno.EDQUOT, errno.ENOSPC):
                            raise CapacityError(str(e))
                        last = f"{m}: {e}"
                    except Exception as e:
                        last = f"{m}: {e}"
                    if attempt < max_attempts and total < max_total:
                        w = min(600, backoff * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
                        log(f"{name}: attempt {total} failed ({last[:160]}); retry in {w:.0f}s")
                        time.sleep(w)
            self._event("get-failed", acc, {"file": name, "error": last[:300], "attempts": total})
            return ("failed", f"{total} attempts: {last[:200]}")

    def _globus(self, acc, inv, names, strict, hours):
        """One Globus batch into a private inbox; delivered files are verified and published under the file lock.
        Earlier tasks of this accession (intent left in .ops by an interrupted get) are adopted first: waited on if
        still running, their inbox published, so their files are not transferred again.
        Returns (published names, names blocked by a task that is still active or whose status is unknown)."""
        why = tx.globus_usable(self.root)
        if why:
            if strict:
                raise UsageError(f"--method globus: {why}")
            log(f"globus skipped: {why}; using FTP/HTTPS")
            return set(), set()
        done, blocked = set(), set()
        for ip in sorted(glob.glob(os.path.join(self.root, ".ops", "globus-*.json"))):
            try:
                it, owner, problem = read_own_json(ip, "intent")
            except CorruptRecord as e:
                log(f"{os.path.basename(ip)} unreadable, ignored here (pxd recover reports it): {e}")
                continue
            if it is None:
                continue                                   # finished by a concurrent get
            if (not isinstance(it, dict) or it.get("kind") != "globus" or it.get("accession") != acc
                    or not isinstance(it.get("files"), list) or not all(isinstance(n, str) for n in it["files"])
                    or not set(it["files"]) & set(names)
                    or not isinstance(it.get("task"), str) or not isinstance(it.get("inbox"), str)):
                continue
            try:
                inside(os.path.join(self.root, ".work", acc, "globus"), it["inbox"])
                [safe_name(n) for n in it["files"]]
            except (PxdError, TypeError) as e:
                log(f"{os.path.basename(ip)}: invalid Globus intent ignored ({e})")
                continue
            if owner != os.getuid() or problem or it.get("dest_collection", tx.G["dest"]) != tx.G["dest"]:
                log(f"globus task {it['task']} ({os.path.basename(ip)}) belongs to {_uname(owner)}, is writable for others "
                    f"or uses another destination; not adopted: its {len(set(it['files']) & set(names))} files are fetched anew")
                continue
            if all(self._deleted_since(acc, n, it.get("created")) for n in set(it["files"]) & set(names)):
                log(f"globus task {it['task']}: its files were deleted after it was submitted; not waited for")
                continue                                   # its deliveries are discarded when they are collected
            status = tx.globus_status(it["task"])
            if status not in ("SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"):
                status = tx.globus_wait(it["task"], hours)
            log(f"globus task {it['task']} of an earlier get: {status}; publishing {it['inbox']}")
            d, b = self._globus_collect(acc, inv, ip, it, "FAILED" if status in ("CANCELED", "CANCELLED") else status, names)
            done |= d
            blocked |= b
        names = [n for n in names if n not in done and n not in blocked]
        oid = opid("globus")
        pairs = [(inv_mod.urls_for(inv["files"][n])["globus"], n) for n in names
                 if "globus" in inv_mod.urls_for(inv["files"][n])]
        if not pairs:
            return done, blocked
        inbox = mkdirs(self.work(acc, "globus", oid))
        pairs = [(s, os.path.join(inbox, n)) for s, n in pairs]
        try:
            task = tx.globus_submit(pairs, f"pxdstore {acc} {len(pairs)} files {oid}")
        except PxdError as e:
            if strict:
                raise
            log(f"{e}; using FTP/HTTPS")
            return done, blocked
        ipath = os.path.join(self.root, ".ops", oid + ".json")
        it = {"op": oid, "kind": "globus", "task": task, "accession": acc, "inbox": inbox,
              "files": [os.path.basename(d) for _, d in pairs], "created": now(), "user": user(), "uid": os.getuid(),
              "dest_collection": tx.G["dest"]}
        write_json(ipath, it, self._intent_mode())
        log(f"globus task {task}: {len(pairs)} files -> {inbox}")
        d, b = self._globus_collect(acc, inv, ipath, it, tx.globus_wait(task, hours))
        return done | d, blocked | b

    def _globus_collect(self, acc, inv, ipath, it, status, names=None):
        """Verify and publish what the task `it` delivered into its inbox; only the files in @p names (default: all of
        the task) are published now, the others stay in the inbox for the get that asks for them. The intent is
        removed once the task is over and its inbox is empty. A delivery is published only if it matches PRIDE's
        MD5, or, for a file without a PRIDE checksum, if the task SUCCEEDED and the size matches PRIDE's and no
        other copy is archived. Deliveries of files deleted after the task was submitted are discarded.
        Returns (published names, names blocked because the task may still run)."""
        if status not in ("SUCCEEDED", "FAILED"):
            log(f"globus task {it['task']} still {status}: its files are not retried now (pxd recover, then get again)")
            return set(), set(it["files"])
        inbox, done = it["inbox"], set()
        sel = set(it["files"]) if names is None else set(it["files"]) & set(names)
        for n in it["files"]:
            d = os.path.join(inbox, n)
            if n not in sel or not os.path.isfile(d) or n not in inv["files"]:
                continue
            with self._file_lock(acc, n, wait=600) as lk:
                if lk is None or not os.path.isfile(d):
                    continue
                if self._deleted_since(acc, n, it.get("created")):
                    try:
                        os.unlink(d)
                        log(f"{n}: deleted from the mirror after Globus task {it['task']} was submitted; delivery discarded")
                    except OSError as e:
                        log(f"{n}: deleted after Globus task {it['task']} was submitted; could not discard {d} ({e})")
                    continue
                want_md5, want_size = inv["checksums"].get(n, (None, None))
                api_size = int(inv["files"][n].get("fileSizeBytes") or -1)
                size = os.path.getsize(d)
                if want_md5:
                    local = md5sum(d)
                    if local != want_md5 or (want_size is not None and size != want_size):
                        self._quarantine(acc, d, "globus delivery does not match PRIDE")
                        continue
                elif status != "SUCCEEDED" or (api_size >= 0 and size != api_size):
                    self._quarantine(acc, d, f"no PRIDE checksum and the Globus task {status.lower()}"
                                     if status != "SUCCEEDED" else f"no PRIDE checksum and size {size} != PRIDE {api_size}")
                    continue
                else:
                    local = md5sum(d)
                final = self.ds(acc, "raw", n)
                fmd5 = md5sum(final) if os.path.isfile(final) and not os.path.islink(final) else None
                if fmd5 == local:
                    rec = self.manifest(acc)["files"].get(n, {})
                    if rec.get("status") != "ok" or rec.get("md5") != local:
                        e = self._file_entry(inv, n, final, local, rec.get("method", "adopted"))
                        self._update_manifest(acc, lambda m: m["files"].__setitem__(n, e))
                        self._sync(acc, self._event("adopt", acc, {"file": n}))
                    os.unlink(d)
                    done.add(n)
                    continue
                if os.path.lexists(final):
                    if not want_md5:                # nothing tells which copy is right: keep the archived one
                        self._quarantine(acc, d, "no PRIDE checksum and a different copy is already archived")
                        continue
                    self._replace_out(acc, n, final, "replaced by a Globus delivery matching the current PRIDE md5")
                e = self._file_entry(inv, n, d, local, "globus", 1, f"{tx.G['source']}:{inv_mod.urls_for(inv['files'][n])['globus']}")
                self.publish(acc, d, f"raw/{n}", {"kind": "file", "key": n, "entry": e}, allow_copy=True)
                done.add(n)
        try:
            os.rmdir(inbox)                                # only if empty
        except FileNotFoundError:
            pass
        except OSError:
            left = len(set(it["files"]) - sel)
            log(f"globus task {it['task']}: files left in {inbox}" + (f" ({left} not requested by this get)" if left else "")
                + "; intent kept for the next get")
            return done, set()
        try:
            os.unlink(ipath)
        except FileNotFoundError:
            pass
        return done, set()

    def _deleted_since(self, acc, name, created):
        """True if @p name (or the whole data set) was deleted at or after the time @p created (ISO, UTC)."""
        try:
            m = self.manifest(acc)
        except CorruptRecord:
            return False
        t = [m.get("deleted")] if m.get("status") == "deleted" else []
        f = m["files"].get(name) or {}
        if f.get("status") == "deleted":
            t.append(f.get("deleted"))
        return any(isinstance(x, str) and (not isinstance(created, str) or x >= created) for x in t)

    def _replace_out(self, acc, name, final, why):
        """Move the archived copy of @p name aside (quarantine) right before its verified replacement is
        published, and mark the manifest entry accordingly, so a crash in between leaves a truthful record."""
        q = self._quarantine(acc, final, why)
        def mark(m):
            if name in m["files"]:
                m["files"][name].update(status="quarantined", quarantined=now(), quarantine_path=q)
        self._update_manifest(acc, mark)

    # ---------------------------------------------------------------- delete (C2)
    def delete_plan(self, acc, files=None, dataset=False, derived_only=False, status="ok"):
        """@p status selects the conversions by catalogue status (e.g. index_invalid for legacy mzML)."""
        if sum(bool(x) for x in (files, dataset, derived_only and not files)) != 1 and not (derived_only and files):
            raise UsageError("delete needs exactly one of --dataset, --files ... (optionally with --derived-only), --derived-only")
        man = self.manifest(acc)
        names = set(files or [])
        unknown = [n for n in names if n not in man["files"]]
        if unknown:
            raise UsageError(f"not in the mirror: {unknown[:5]}")
        items = []
        if not derived_only:
            for n, f in sorted(man["files"].items()):
                if (dataset or n in names) and f.get("status") in ("ok", "mismatch") and os.path.lexists(os.path.join(self.root, acc, f["path"])):
                    items.append(("file", n, f["path"]))
        for cid, c in sorted(man["conversions"].items()):
            if ((dataset or not names or c["source_name"] in names) and c.get("status") == status and c.get("output_path")
                    and os.path.lexists(os.path.join(self.root, acc, safe_rel(c["output_path"])))):
                items.append(("conversion", cid, c["output_path"]))
        globus = []                                   # Globus tasks that may still deliver what is deleted
        if not derived_only:
            for ip, it, owner, problem in self._globus_intents(acc):
                hit = sorted(set(it["files"]) if dataset else set(it["files"]) & names)
                if hit:
                    globus.append({"op": it.get("op") or os.path.basename(ip)[:-5], "task": it["task"], "owner": owner,
                                   "files": hit, "inbox": it["inbox"], "intent": ip, "own": owner == os.getuid() and not problem})
        token = hashlib.sha256("\n".join([acc] + [f"{k}\t{key}\t{p}" for k, key, p in items]
                                          + [f"globus\t{g['op']}\t{','.join(g['files'])}" for g in globus]).encode()).hexdigest()[:12]
        return {"accession": acc, "items": items, "globus": globus, "token": token, "dataset": dataset}

    def _globus_intents(self, acc):
        """[(path, intent, owner uid, problem)] of the valid Globus intents of @p acc (see read_own_json)."""
        out = []
        for ip in sorted(glob.glob(os.path.join(self.root, ".ops", "globus-*.json"))):
            try:
                it, owner, problem = read_own_json(ip, "intent")
            except CorruptRecord:
                continue
            if (not isinstance(it, dict) or it.get("kind") != "globus" or it.get("accession") != acc
                    or not isinstance(it.get("files"), list) or not all(isinstance(n, str) for n in it["files"])
                    or not isinstance(it.get("task"), str) or not isinstance(it.get("inbox"), str)):
                continue
            try:
                inside(os.path.join(self.root, ".work", acc, "globus"), it["inbox"])
                [safe_name(n) for n in it["files"]]
            except (PxdError, TypeError):
                continue
            out.append((ip, it, owner, problem))
        return out

    def delete(self, acc, files=None, dataset=False, derived_only=False, token=None, status="ok"):
        """Deletes exactly the items of delete_plan(...) whose token the caller confirms."""
        with self._acc_lock(acc, True) as fd:
            if fd is None:
                raise PxdError(f"{acc}: in use by another get/convert/migrate; try later")
            plan = self.delete_plan(acc, files, dataset, derived_only, status)
            if token != plan["token"]:
                raise UsageError(f"confirmation token {token!r} does not match the current list ({plan['token']}); review it again")
            gone, paths = [], []
            for kind, key, rel in plan["items"]:              # check everything before deleting anything
                p = inside(self.root, os.path.join(self.root, acc, safe_rel(rel)))
                if os.path.islink(p) or not os.path.isfile(p):
                    raise PxdError(f"{p} is not a regular file; nothing deleted")
                paths.append(p)
            for (kind, key, rel), p in zip(plan["items"], paths):
                os.unlink(p)
                gone.append((kind, key))
                d = os.path.dirname(p)
                while d != self.ds(acc) and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)

            t = now()

            def mark(m):
                for kind, key in gone:
                    (m["files"] if kind == "file" else m["conversions"])[key].update(status="deleted", deleted=t, deleted_by=user())
                for g in plan["globus"]:               # files still in flight: a later delivery must not bring them back
                    for n in g["files"]:
                        if n in m["files"] and m["files"][n].get("status") != "deleted":
                            m["files"][n].update(status="deleted", deleted=t, deleted_by=user())
                if dataset:
                    m.update(status="deleted", deleted=t, deleted_by=user())
            self._update_manifest(acc, mark)
            self._sync(acc, self._event("delete", acc, {"items": [list(g) for g in gone], "dataset": dataset,
                                                        "globus": [g["op"] for g in plan["globus"]]}))
            for g in plan["globus"]:
                self._discard_globus(g)
            return gone

    def _discard_globus(self, g):
        """After a delete: remove what a Globus task delivered of the deleted files (and cancel the task if it is
        ours and everything it carries is deleted). Deliveries arriving later are discarded by the next get of its
        owner (_deleted_since)."""
        left = []
        for n in g["files"]:
            d = os.path.join(g["inbox"], n)
            try:
                if os.path.lexists(d):
                    os.unlink(d)
            except OSError as e:
                left.append(f"{n} ({e.strerror})")
        if left:
            log(f"globus task {g['task']} of {_uname(g['owner'])}: could not discard {', '.join(left[:5])} in {g['inbox']}")
        if not g["own"]:
            log(f"globus task {g['task']} belongs to {_uname(g['owner'])}; its later deliveries of deleted files are "
                "discarded by their next get")
            return
        try:
            it, _, problem = read_own_json(g["intent"], "intent")
        except CorruptRecord:
            return
        if not it or problem or not set(it["files"]) <= set(g["files"]):
            return                                         # the task also carries files that are not deleted
        st = tx.globus_status(g["task"])
        if st not in ("SUCCEEDED", "FAILED", "CANCELED", "CANCELLED") and tx._cli():
            st = tx.globus_cancel(g["task"])
        if st in ("SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"):
            try:
                if not os.listdir(g["inbox"]):
                    os.rmdir(g["inbox"])
                os.unlink(g["intent"])
            except OSError:
                pass

    # ---------------------------------------------------------------- convert
    def convert(self, acc, names, fmt, tool=None, threads=8, stage=None, allow_trfp2=False, tools_json=None):
        if fmt not in cv.EXT:
            raise UsageError(f"--to must be mzML or mzPeak, not {fmt}")
        tools = cv.load_tools(tools_json)
        out = []
        with self._acc_lock(acc, False) as fd:
            if fd is None:
                raise PxdError(f"{acc}: busy")
            for name in names:
                try:
                    out.append((name, *self._convert_one(acc, name, fmt, tools, tool, threads, stage, allow_trfp2)))
                except CapacityError:
                    raise
                except (PxdError, OSError, subprocess.SubprocessError) as e:
                    out.append((name, "failed", str(e)[:300]))
        return out

    def _convert_one(self, acc, name, fmt, tools, tool, threads, stage, allow_trfp2):
        man = self.manifest(acc)
        src = man["files"].get(name)
        if not src or src.get("status") != "ok":
            raise UsageError(f"{name} is not an archived file of {acc} (pxd get first)")
        tname, spec = cv.pick(tools, name, fmt, tool, allow_trfp2)
        ident = cv.identify(tname, spec)
        rec = cv.recipe(tname, ident, spec, fmt)
        stem = _stem(name)
        rel = f"{fmt}/{rec}/{stem}{cv.EXT[fmt]}"
        for cid, c in man["conversions"].items():
            if (c["source_name"], c["format"], c["recipe"], c.get("status")) == (name, fmt, rec, "ok") and os.path.exists(os.path.join(self.root, acc, rel)):
                return ("exists", rel)
        with self._file_lock(acc, f"conv-{rec}-{stem}") as lk:
            if lk is None:
                return ("locked", rel)
            base = os.path.abspath(os.path.expanduser(stage or self.cfg.get("paths", "scratch")))
            self._refuse_forbidden(base, "conversion staging")
            try:
                base = private_dir(base)                 # ours, 0700, and nobody can rename it away
            except OSError as e:
                raise PxdError(f"conversion staging {base} not usable ({e}); set paths.scratch or --stage")
            sd = tempfile.mkdtemp(prefix=f"{acc}-{stem}-", dir=base)
            try:
                staged = os.path.join(sd, name)
                shutil.copyfile(self.ds(acc, "raw", name), staged)
                if md5sum(staged) != src["md5"]:
                    raise PxdError(f"staged copy of {name} does not match its recorded md5")
                inp = staged
                if name.lower().endswith(".zip"):
                    ud = os.path.join(sd, "unzip")
                    safe_unzip(staged, ud)
                    ds_ = [os.path.join(ud, d) for d in os.listdir(ud)]
                    if len(ds_) != 1:
                        raise PxdError(f"{name}: expected one member directory, found {len(ds_)}")
                    inp = ds_[0]
                outp = os.path.join(sd, "out" + cv.EXT[fmt])
                argv = cv.argv(spec, fmt, inp, outp, threads)
                env = dict(os.environ, **spec.get("env", {}))
                if cv.identify(tname, spec) != ident:          # re-checked right before it runs
                    raise PxdError(f"tool {tname} changed during staging; not run")
                t0, started = time.time(), now()
                r = subprocess.run(argv, capture_output=True, text=True, env=env)
                secs = round(time.time() - t0, 1)
                val = cv.validate(fmt, outp) if r.returncode == 0 else {"ok": False, "why": f"exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}"}
                cid = f"{acc}:{name}:{fmt}:{rec}"
                entry = {"source_name": name, "source_md5": src["md5"], "format": fmt, "recipe": rec, "output_path": rel,
                         "tool": {**{k: ident[k] for k in ("name", "version", "path", "sha256", "deployment", "tree_sha256")},
                                  **{k: spec[k] for k in ("recipe_deployment_sha256", "recipe_deployment")
                                     if spec.get(k)}},
                         "argv": [a.replace(sd, "<stage>") for a in argv], "env": spec.get("env", {}),
                         "host": socket.gethostname(), "started": started, "seconds": secs,
                         "validator": cv.VALIDATOR, "validation": val, "status": "ok" if val["ok"] else "failed"}
                if not val["ok"]:
                    self._update_manifest(acc, lambda m: m["conversions"].__setitem__(cid, entry))
                    self._sync(acc, self._event("convert-failed", acc, {"id": cid, "why": val.get("why")}))
                    return ("failed", val.get("why"))
                cw = mkdirs(self.work(acc, "conv", opid("conv")))
                cpy = os.path.join(cw, os.path.basename(rel))
                try:
                    shutil.copyfile(outp, cpy)
                except OSError as e:
                    capacity_guard(e)
                entry.update(output_size=os.path.getsize(cpy), output_md5=md5sum(cpy))
                self.publish(acc, cpy, rel, {"kind": "conversion", "key": cid, "entry": entry})
                shutil.rmtree(cw, ignore_errors=True)
                return ("ok", rel)
            finally:
                shutil.rmtree(sd, ignore_errors=True)

    # ---------------------------------------------------------------- migrate (C4, C5)
    def migrate_plan(self, srcs, out=None, workers=8):
        """Read-only scan (writes only the plan file). Only files whose name, size and MD5 match PRIDE's checksum
        for the accession in their path are candidates; everything else stays where it is."""
        self._need_root()
        for s in srcs:
            self._refuse_forbidden(s, "migration source")
        if out:
            self._refuse_forbidden(os.path.dirname(os.path.abspath(out)) or "/", "plan file")
        cand, other = {}, {}
        for s in srcs:
            for dp, dns, fns in os.walk(s):
                dns[:] = [d for d in dns if not d.startswith(".")]
                acc = next((p for p in reversed(dp.split(os.sep)) if ACC_RE.match(p)), None)
                for fn in fns:
                    p = os.path.join(dp, fn)
                    if acc:
                        cand.setdefault(acc, []).append(p)
                    else:
                        other[dp] = other.get(dp, 0) + 1
        rows, invs = [], {}
        for acc, paths in sorted(cand.items()):
            try:
                inv = inv_mod.load(self.ds(acc), acc) or inv_mod.snapshot(inv_mod.fetch(acc), acc)
            except (PxdError, CorruptRecord) as e:
                rows += [("no-inventory", acc, os.path.basename(p), p, 0, 0, 0, 0, "", str(e)[:80]) for p in paths]
                continue
            invs[acc] = inv
            todo = []
            for p in paths:
                n = os.path.basename(p)
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue                                            # symlinks, FIFOs, devices: never
                if n not in inv["files"]:
                    continue                                            # local file: not listed, never moved
                want = inv["checksums"].get(n)
                if not os.access(p, os.R_OK):
                    rows.append(("not-readable", acc, n, p, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, "", ""))
                elif not want:
                    rows.append(("no-pride-md5", acc, n, p, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, "", ""))
                elif want[1] is not None and st.st_size != want[1]:
                    rows.append(("size-mismatch", acc, n, p, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, "", ""))
                else:
                    todo.append((p, n, st, want[0]))
            def h(t):
                try:
                    return md5sum(t[0])
                except OSError:
                    return None
            with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                for (p, n, st, want), md5 in zip(todo, ex.map(h, todo)):
                    dest = os.path.join(self.root, acc, "raw", n)
                    if md5 is None:
                        cls = "not-readable"
                    elif md5 != want:
                        cls = "md5-mismatch"
                    elif os.path.exists(dest):
                        cls = "duplicate" if md5sum(dest) == md5 else "conflict"
                    elif st.st_nlink > 1:
                        cls = "hardlinked"
                    elif self._not_movable(p, st):
                        cls = "not-movable"
                    else:
                        cls = "move"
                    rows.append((cls, acc, n, p, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, md5, f"raw/{n}"))
        # within the plan, a second copy of the same accession/name is a duplicate of the first
        seen = set()
        for i, r in enumerate(rows):
            if r[0] == "move":
                if (r[1], r[2]) in seen:
                    rows[i] = ("duplicate",) + r[1:]
                seen.add((r[1], r[2]))
        pid = opid("plan")
        if out:
            out = os.path.abspath(out)
            if os.path.lexists(out):
                raise UsageError(f"{out} exists; give a new file name for the plan")
            if out.startswith(self.root + os.sep) and not out.startswith(os.path.join(self.root, ".ops") + os.sep):
                raise UsageError(f"{out}: a plan inside the mirror goes into {self.root}/.ops")
        out = out or os.path.join(self.root, ".ops", pid + ".tsv")
        hdr = "class\taccession\tname\tsrc\tdev\tino\tsize\tmtime_ns\tmd5\tdest\n"
        atomic_write(out, f"# {pid} {now()} {user()} sources: {' '.join(srcs)}\n" + hdr +
                     "".join("\t".join(map(str, r)) + "\n" for r in rows))
        summary = {}
        for r in rows:
            c = summary.setdefault(r[0], [0, 0])
            c[0] += 1
            c[1] += int(r[6] or 0)
        return {"plan": out, "summary": summary, "files_without_accession_dir": sum(other.values())}

    def _not_movable(self, p, st):
        """Why a source file cannot be moved into the mirror by this user, or None. Another user's file moves
        only if it is group read-write, already has the mirror's group and its directory is writable and not
        sticky (else the hard link fails, the mirror would get a file nobody but its owner can fix, or the
        unlink after the link fails)."""
        parent = os.path.dirname(p)
        if not os.access(parent, os.W_OK):
            return "source directory not writable"
        if st.st_uid != os.getuid():
            if st.st_mode & 0o002:
                return f"owned by {_uname(st.st_uid)} and writable for everybody"
            if not os.access(p, os.R_OK | os.W_OK):
                return f"owned by {_uname(st.st_uid)} and not readable and writable for you"
            if st.st_mode & 0o060 != 0o060:
                return f"owned by {_uname(st.st_uid)} and not group read-write"
            if st.st_gid != os.stat(self.root).st_gid:
                return f"owned by {_uname(st.st_uid)} with another group"
            if os.stat(parent).st_mode & stat.S_ISVTX:
                return f"owned by {_uname(st.st_uid)} in a sticky directory"
        return None

    def migrate_apply(self, plan_path, symlink_back=True):
        rows = []
        with open(plan_path) as fh:
            for line in fh:
                if line.startswith("#") or line.startswith("class\t"):
                    continue
                c = line.rstrip("\n").split("\t")
                if c[0] == "move":
                    rows.append(c)
        out = []
        for cls, acc, n, src, dev, ino, size, mtime_ns, md5, rel in rows:
            try:
                out.append((acc, n, *self._migrate_one(acc, n, src, int(dev), int(ino), int(size), int(mtime_ns), md5, rel, symlink_back)))
            except CapacityError:
                raise
            except (PxdError, OSError) as e:
                out.append((acc, n, "failed", str(e)[:200]))
        self._event("migrate", None, {"plan": plan_path, "results": {r: sum(1 for x in out if x[2] == r) for r in {x[2] for x in out}}})
        return out

    def _migrate_one(self, acc, n, src, dev, ino, size, mtime_ns, md5, rel, symlink_back):
        safe_name(n)
        with self._acc_lock(acc, False) as fd:
            if fd is None:
                return ("locked", "accession busy")
            inv = self.inventory(acc)
            if inv["checksums"].get(n, (None,))[0] != md5:
                return ("left", "PRIDE checksum changed since the plan")
            with self._file_lock(acc, n, wait=300) as lk:
                if lk is None:
                    return ("locked", "file busy")
                if _under(src, self.cfg.get("paths", "forbidden_roots")):
                    return ("left", "under a forbidden root (paths.forbidden_roots)")
                st = os.lstat(src)
                if (not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (dev, ino, size, mtime_ns)):
                    return ("left", "source changed since the plan")
                if st.st_nlink > 1:
                    return ("left", "source has other hard links")
                why = self._not_movable(src, st)
                if why:
                    return ("left", f"not movable: {why}")
                if md5sum(src) != md5:
                    return ("left", "source md5 changed since the plan")
                dest = os.path.join(self.root, acc, rel)
                if os.path.exists(dest):
                    return ("left", "destination exists (duplicate)" if md5sum(dest) == md5 else "destination exists with other content")
                if st.st_uid == os.getuid():
                    os.chmod(src, U.FILE_MODE)
                    try:
                        os.chown(src, -1, os.stat(self.root).st_gid)
                    except PermissionError:
                        pass                    # we are not in the mirror's group: the file keeps its group
                e = self._file_entry(inv, n, src, md5, "migrated", extra={"migrated_from": src, "downloaded": None})
                self.publish(acc, src, rel, {"kind": "file", "key": n, "entry": e}, backlink=src if symlink_back else None)
                return ("moved", f"{acc}/{rel}")

    # ---------------------------------------------------------------- legacy conversions (pride_get "derived")
    def import_legacy(self, legacy_dir, plan_only=False, symlink_back=True):
        """Import the derived mzML of a legacy pride_get data set (manifest.json "derived"), after its raw files were
        migrated. Each output is checked against its recorded md5, its source must be archived here with the recorded
        source md5, the mzML index is validated (TRFP 2.0.0 is known to write wrong offsets) and the result is kept in
        the record: status ok, or index_invalid (file kept, flagged). Published like everything else (link+unlink, no
        overwrite, symlink back). Legacy provenance (converter, version, args, host, time, note) is copied as recorded;
        binary hashes were never recorded and stay unknown."""
        acc = os.path.basename(os.path.normpath(legacy_dir))
        safe_name(acc)
        self._refuse_forbidden(legacy_dir, "legacy directory")
        old = read_json(os.path.join(legacy_dir, "manifest.json"), "legacy manifest")
        out = []
        if not old:
            return [(acc, "manifest.json", "left", "no legacy manifest")]
        for key, d in sorted((old.get("derived") or {}).items()):
            n = os.path.basename(key)
            try:
                safe_name(n)
                if not n.lower().endswith(".mzml"):
                    out.append((acc, key, "left", "not an mzML")); continue
                src_old = os.path.join(legacy_dir, key)
                if _under(src_old, [legacy_dir]) is None or _under(src_old, self.cfg.get("paths", "forbidden_roots")):
                    out.append((acc, key, "left", "path outside the legacy directory or under a forbidden root")); continue
                man = self.manifest(acc)
                srcrec = man["files"].get(d.get("source"))
                if not srcrec or srcrec.get("status") != "ok" or srcrec.get("md5") != d.get("source_md5"):
                    out.append((acc, key, "left", f"source {d.get('source')} not archived here with md5 {d.get('source_md5')}")); continue
                args = d.get("args") or ""
                v = re.sub(r"[^A-Za-z0-9.]+", "-", str(d.get("converter_version") or "unknown"))[:40].strip("-")
                tname = re.sub(r"[^A-Za-z0-9]+", "", str(d.get("converter") or "unknown")).lower()
                h = hashlib.sha256(json.dumps(["mzML", args.split(), "legacy"]).encode()).hexdigest()[:8]
                rec = f"legacy-{tname}-{v}-{h}"
                rel = f"mzML/{rec}/{n}"
                cid = f"{acc}:{d['source']}:mzML:{rec}:{n}"
                if cid in man["conversions"] and os.path.exists(os.path.join(self.root, acc, rel)):
                    out.append((acc, key, "exists", rel)); continue
                if plan_only:
                    out.append((acc, key, "would-import", rel)); continue
                with self._acc_lock(acc, False) as fd:
                    if fd is None:
                        out.append((acc, key, "locked", "accession busy")); continue
                    with self._file_lock(acc, f"legacy-{rec}-{n}", wait=300) as lk:
                        if lk is None:
                            out.append((acc, key, "locked", "file busy")); continue
                        st = os.lstat(src_old)
                        if not stat.S_ISREG(st.st_mode):
                            out.append((acc, key, "left", "not a regular file (already imported or a symlink)")); continue
                        if st.st_nlink > 1:
                            out.append((acc, key, "left", "has other hard links")); continue
                        why = self._not_movable(src_old, st)
                        if why:
                            out.append((acc, key, "left", f"not movable: {why}")); continue
                        md5 = md5sum(src_old)
                        if d.get("md5") and md5 != d["md5"]:
                            out.append((acc, key, "left", f"md5 {md5} != recorded {d['md5']}")); continue
                        val = cv.validate("mzML", src_old)
                        if st.st_uid == os.getuid():
                            os.chmod(src_old, U.FILE_MODE)
                            try:
                                os.chown(src_old, -1, os.stat(self.root).st_gid)
                            except PermissionError:
                                pass
                        entry = {"source_name": d["source"], "source_md5": d["source_md5"], "format": "mzML", "recipe": rec,
                                 "output_path": rel, "output_size": st.st_size, "output_md5": md5,
                                 "tool": {"name": d.get("converter"), "version": d.get("converter_version"), "path": None,
                                          "sha256": None, "deployment": "legacy pride_get record (binary not recorded)"},
                                 "argv": args.split(), "env": {}, "host": d.get("host"), "started": d.get("created"),
                                 "seconds": None, "note": d.get("note", ""), "imported_from": src_old, "imported": now(),
                                 "validator": cv.VALIDATOR, "validation": val,
                                 "status": "ok" if val.get("ok") else "index_invalid"}
                        self.publish(acc, src_old, rel, {"kind": "conversion", "key": cid, "entry": entry},
                                     backlink=src_old if symlink_back else None)
                        out.append((acc, key, "imported" if val.get("ok") else "imported-index-invalid", rel))
            except CapacityError:
                raise
            except (PxdError, OSError) as e:
                out.append((acc, key, "failed", str(e)[:200]))
        if not plan_only:
            self._event("import-legacy", acc, {"dir": legacy_dir, "results": {r: sum(1 for x in out if x[2] == r) for r in {x[2] for x in out}}})
        return out

    # ---------------------------------------------------------------- verify / stage / reindex
    def verify(self, acc):
        out = []
        with self._acc_lock(acc, False) as fd:
            if fd is None:
                raise PxdError(f"{acc}: busy")
            man = self.manifest(acc)
            for n, f in sorted(man["files"].items()):
                if f.get("status") != "ok":
                    continue
                with self._file_lock(acc, n, wait=300) as lk:
                    if lk is None:
                        out.append((n, "locked"))
                        continue
                    p = inside(self.root, os.path.join(self.root, acc, safe_rel(f["path"])))
                    ok = os.path.isfile(p) and md5sum(p) == f["md5"]
                    out.append((n, "ok" if ok else "mismatch"))
                    if not ok:
                        self._update_manifest(acc, lambda m: m["files"][n].update(status="mismatch", verified=now()))
                    else:
                        st = os.stat(p)
                        self._update_manifest(acc, lambda m: m["files"][n].update(verified=now(), size=st.st_size, mtime_ns=st.st_mtime_ns))
            self._sync(acc, self._event("verify", acc, {"mismatch": [n for n, r in out if r != "ok"]}))
        return out

    def stage(self, acc, names=None, all_=False, to=None, unzip=False):
        """Copy archived files (raw/<name> or conversion outputs, relative paths kept) to `to`; the copy is verified
        at the target and published without overwriting. Remote targets (host:path) via rsync + remote md5sum."""
        man = self.manifest(acc)
        items = {n: f for n, f in man["files"].items() if f.get("status") == "ok"}
        items.update({c["output_path"]: {"path": c["output_path"], "md5": c["output_md5"]} for c in man["conversions"].values()
                      if c.get("status") == "ok"})
        sel = sorted(items) if all_ else list(dict.fromkeys(names or []))
        missing = [n for n in sel if n not in items]
        if not sel or missing:
            raise UsageError(f"nothing to stage or not archived: {missing[:5]}")
        out = []
        if re.match(r"^[^/]+:", to):
            host, rpath = to.split(":", 1)
            if not re.fullmatch(r"[A-Za-z0-9_.%+-]+(@[A-Za-z0-9_.%-]+)?", host) or host.startswith("-"):
                raise UsageError(f"stage: unusable host {host!r}")
            if rpath in ("~", "~/"):
                rpath = "."
            elif rpath.startswith("~/"):
                rpath = rpath[2:]                         # relative to the remote home: ssh and rsync start there
            elif rpath.startswith("~"):
                raise UsageError("stage HOST:~user/... is not supported; give an absolute path or one relative to your home")
            if not rpath:
                raise UsageError("stage HOST:PATH needs a path")
            ssh, rsync = self.cfg.stage_options(host)
            ssh = ["ssh"] + ssh
            tmp = f"{rpath.rstrip('/') or '/'}/.pxd-stage-{opid('st')}"
            q = shlex.quote
            for n in sel:
                rel = safe_rel(items[n]["path"])
                src = inside(self.root, os.path.join(self.root, acc, rel))
                md5 = items[n]["md5"]
                if not re.fullmatch(r"[0-9a-f]{32}", md5 or ""):
                    out.append((n, "failed", "no recorded md5")); continue
                if os.path.islink(src) or not os.path.isfile(src):
                    out.append((n, "failed", "not a regular file in the mirror")); continue
                # -s (--protect-args): the remote path is not split or expanded by the remote shell
                r = subprocess.run(["rsync", *rsync, "-s", "-e", rsync_command(ssh), "--", src,
                                    f"{host}:{tmp}/{rel}"], capture_output=True, text=True)
                if r.returncode:
                    out.append((n, "failed", r.stderr[-200:])); continue
                t, d = q(f"{tmp}/{rel}"), q(f"{rpath}/{rel}")
                tdirs = []
                x = os.path.dirname(f"{tmp}/{rel}")
                while len(x) >= len(tmp):
                    tdirs.append(q(x))
                    x = os.path.dirname(x)
                clean = f"rmdir -- {' '.join(tdirs)} 2>/dev/null || true"
                chk = (f"set -e; s=$(md5sum -- {t} | cut -c1-32); [ \"$s\" = {md5} ] || exit 7; "
                       f"mkdir -p -- \"$(dirname -- {d})\"; if [ -e {d} ] || [ -L {d} ]; then "
                       f"[ -f {d} ] && [ \"$(md5sum -- {d} | cut -c1-32)\" = {md5} ] && rm -f -- {t} && {{ {clean}; exit 0; }}; exit 8; fi; "
                       f"mv -n -- {t} {d}; [ ! -e {t} ] && [ -f {d} ] && [ ! -L {d} ] || exit 8; {clean}")
                r = subprocess.run(ssh + ["--", host, chk], capture_output=True, text=True)
                out.append((n, {0: "copied", 7: "failed: md5 at target", 8: "conflict: target differs"}.get(r.returncode, f"failed rc={r.returncode}"), f"{to}/{rel}"))
            return out
        to = os.path.abspath(os.path.expanduser(to))
        forbidden = self.cfg.get("paths", "forbidden_roots") + [self.root]

        def check_target(p):                       # realpath: symlinked components count where they lead
            bad = _under(p, forbidden)
            if bad:
                raise UsageError(f"{p} is under {bad} ({'the mirror itself' if bad == self.root else 'paths.forbidden_roots'})")
        check_target(to)
        um = current_umask()
        for n in sel:
            rel = safe_rel(items[n]["path"])
            src = inside(self.root, os.path.join(self.root, acc, rel))
            if os.path.islink(src) or not os.path.isfile(src):
                out.append((n, "failed", "not a regular file in the mirror")); continue
            dst = os.path.join(to, rel)
            check_target(os.path.dirname(dst))                 # before creating anything (existing symlinks resolved)
            mkdirs(os.path.dirname(dst), 0o777 & ~um)          # a normal copy: the process umask decides
            check_target(os.path.dirname(dst))
            if os.path.exists(dst):
                out.append((n, "skip" if md5sum(dst) == items[n]["md5"] else "conflict", dst))
                continue
            fd, tmp = tempfile.mkstemp(prefix=".pxd-stage-", dir=os.path.dirname(dst))
            os.fchmod(fd, 0o666 & ~um)                        # mkstemp creates 0600; the copy keeps that mode
            os.close(fd)
            try:
                shutil.copyfile(src, tmp)
            except OSError as e:
                os.unlink(tmp)
                capacity_guard(e)
            if md5sum(tmp) != items[n]["md5"]:
                out.append((n, "failed", f"copy does not match the recorded md5 (left at {tmp})"))
                continue
            try:
                os.link(tmp, dst)
                os.unlink(tmp)
                out.append((n, "copied", dst))
            except FileExistsError:
                out.append((n, "conflict", f"{dst} appeared meanwhile; copy left at {tmp}"))
            if unzip and dst.lower().endswith(".zip"):
                ud = tempfile.mkdtemp(prefix=".pxd-unzip-", dir=os.path.dirname(dst))
                os.chmod(ud, 0o777 & ~um)                     # mkdtemp creates 0700
                safe_unzip(dst, ud)
                fin = dst[:-4]
                if os.path.exists(fin):
                    shutil.rmtree(ud, ignore_errors=True)
                    out.append((n, "unzip-skip", fin))
                else:
                    os.rename(ud, fin)
                    out.append((n, "unzipped", fin))
        return out

    # ---------------------------------------------------------------- doctor (permissions)
    def doctor(self, fix=False, limit=20):
        """Check the shared-use invariants of the mirror: one group, directories setgid with the configured mode
        and a default ACL, files with the configured mode, no sticky bit, no access for others. With @p fix,
        repair what the caller owns (chgrp, chmod, setfacl); entries of other users are only reported.
        Read-only without @p fix. Returns {"checks": [...], "problems": {kind: {"count", "examples"}}, "fixed"}."""
        root_st = os.stat(self.root)
        gid = root_st.st_gid
        want_dir, want_file = U.DIR_MODE, U.FILE_MODE
        shared = self.cfg.get("permissions", "shared")
        checks, problems, fixed = [], {}, 0
        me = os.getuid()

        def problem(kind, path, owner):
            p = problems.setdefault(kind, {"count": 0, "examples": [], "owners": {}})
            p["count"] += 1
            p["owners"][_uname(owner)] = p["owners"].get(_uname(owner), 0) + 1
            if len(p["examples"]) < limit:
                p["examples"].append(path)

        try:
            gname = grp.getgrgid(gid).gr_name
        except KeyError:
            gname = str(gid)
        want_group = self.cfg.get("permissions", "group")
        checks.append(("root", self.root, f"owner {_uname(root_st.st_uid)} group {gname} mode {oct(stat.S_IMODE(root_st.st_mode))}"))
        if want_group and want_group != gname:
            checks.append(("group", "MISMATCH", f"configured permissions.group {want_group}, root has {gname}"))
        if shared:
            member = gid in os.getgroups() or gid == os.getgid()
            checks.append(("membership", "ok" if member else "PROBLEM",
                           f"you are {'' if member else 'NOT '}in the group {gname}"))
        checks.append(("umask", oct(current_umask()), "process umask (from permissions.umask)"))
        for f in ("catalog.sqlite", ".events.jsonl"):
            p = os.path.join(self.root, f)
            if os.path.exists(p):
                checks.append((f, "ok" if os.access(p, os.R_OK | os.W_OK) else "PROBLEM", "writable for you" if os.access(p, os.W_OK) else "NOT writable for you"))
        acl = shared and self.cfg.get("permissions", "default_acl") and shutil.which("getfacl")
        no_acl = set()
        acl_spec = ["-m", "g::rwx,o::---", "-d", "-m", "g::rwx,o::---"]

        def root_has_acl():
            r = subprocess.run(["getfacl", "-p", self.root], capture_output=True, text=True)
            return "default:group::rwx" in r.stdout
        if acl and fix and root_st.st_uid == me and not root_has_acl() and shutil.which("setfacl"):
            if subprocess.run(["setfacl", *acl_spec, self.root], capture_output=True).returncode == 0:
                fixed += 1
        if acl:
            r = subprocess.run(["getfacl", "-p", "-R", self.root], capture_output=True, text=True)
            if r.returncode and "Operation not supported" in r.stderr:
                checks.append(("acl", "unsupported", "this file system has no POSIX ACLs; modes and umask only"))
                acl = False
            else:
                cur, has = None, False
                for line in r.stdout.splitlines() + ["# file: "]:
                    if line.startswith("# file: "):
                        if cur is not None and not has:
                            no_acl.add(cur)
                        cur, has = line[8:], False
                    elif line.startswith("default:group::rwx"):
                        has = True
                if self.root in no_acl:
                    checks.append(("acl", "PROBLEM", "no default ACL on the root: run pxd doctor --fix as its owner, "
                                   "or set permissions.default_acl = false on a file system without ACLs"))
                    acl = False
        ops = os.path.join(self.root, ".ops")
        for dp, dns, fns in os.walk(self.root):
            for name in [None] + dns + fns:
                path = dp if name is None else os.path.join(dp, name)
                if name is not None and name in dns:
                    if os.path.islink(path):
                        problem("symlink inside the mirror", path, os.lstat(path).st_uid)
                    continue                              # real directories are visited as dp
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    problem("symlink inside the mirror", path, st.st_uid)
                    continue
                mode = stat.S_IMODE(st.st_mode)
                isdir = stat.S_ISDIR(st.st_mode)
                want = want_dir if isdir else want_file
                if not isdir and dp == ops and name.endswith(".json"):
                    want = self._intent_mode()          # intents: writable only by their owner (recover)
                    if mode != want:                    # never "repaired": its content may have been changed
                        problem("intent mode (check its content, then chmod 640 it, or remove it)", path, st.st_uid)
                        continue
                bad = []
                if st.st_gid != gid:
                    bad.append("group")
                if mode & stat.S_ISVTX:
                    bad.append("sticky bit")
                if isdir:
                    if (mode & 0o7777) & want != want:
                        bad.append("directory mode")
                else:
                    if mode != want:
                        bad.append("file mode")
                if mode & 0o007 and shared:
                    bad.append("access for others")
                if isdir and acl and path in no_acl:
                    bad.append("default ACL")
                if not bad:
                    continue
                if fix and st.st_uid == me:
                    try:
                        try:
                            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if isdir else 0))
                        except PermissionError:       # our own file without read permission
                            fd = None
                        if fd is None:
                            if not os.path.samestat(os.lstat(path), st):
                                raise OSError(errno.ESTALE, "changed while checking")
                            if "group" in bad:
                                os.chown(path, -1, gid, follow_symlinks=False)
                            os.chmod(path, want)
                        else:
                            try:                      # by descriptor: a swapped-in symlink is not followed
                                if not os.path.samestat(os.fstat(fd), st):
                                    raise OSError(errno.ESTALE, "changed while checking")
                                if "group" in bad:
                                    os.fchown(fd, -1, gid)
                                os.fchmod(fd, want)
                            finally:
                                os.close(fd)
                        if isdir and "default ACL" in bad:
                            if subprocess.run(["setfacl", *acl_spec, path], capture_output=True).returncode:
                                raise OSError(errno.EPERM, "setfacl failed")
                        fixed += 1
                        continue
                    except OSError as e:
                        bad.append(f"fix failed: {e.strerror}")
                for b in bad:
                    problem(b, path, st.st_uid)
        return {"root": self.root, "checks": checks, "problems": problems, "fixed": fixed}

    def reindex(self):
        """Rebuild the catalogue in place from manifests, inventories and the event log. The records are read
        inside the catalogue's write transaction (no concurrent publication is lost). Unreadable records are
        reported (and left out), never reset."""
        result = {}

        def rebuild(con):
            problems, data = [], []
            for acc in sorted(os.listdir(self.root)):
                if not ACC_RE.match(acc):
                    continue
                try:
                    data.append((acc, self.manifest(acc), inv_mod.load(self.ds(acc), acc)))
                except (CorruptRecord, PxdError) as e:
                    problems.append(str(e))
            evs = []
            ep = os.path.join(self.root, ".events.jsonl")
            if os.path.exists(ep):
                with open(ep) as fh:
                    for i, line in enumerate(fh):
                        try:
                            evs.append(json.loads(line))
                        except ValueError:
                            problems.append(f".events.jsonl line {i + 1} unreadable")
            for t in ("files", "conversions", "datasets", "events", "tools"):
                con.execute(f"DELETE FROM {t}")
            for acc, man, inv in data:
                Catalog.put_dataset(con, acc, man, inv)
            for ev in evs:
                Catalog.put_event(con, ev)
            result.update(datasets=len(data), events=len(evs), problems=problems)
        self.cat.write(rebuild)
        return result


def rsync_command(argv):
    """argv as one rsync -e string. rsync splits it on spaces and honours single and double quotes, but not
    backslashes, so shlex.join does not fit."""
    out = []
    for a in argv:
        if a and not re.search(r"[\s'\"\\]", a):
            out.append(a)
        elif "'" not in a:
            out.append(f"'{a}'")
        elif '"' not in a:
            out.append(f'"{a}"')
        else:
            raise UsageError(f"ssh option {a!r} contains both quote characters; not usable with rsync -e")
    return " ".join(out)


def _uname(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, TypeError, OverflowError):
        return f"uid {uid}"


def safe_unzip(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        base = os.path.realpath(dest)
        os.makedirs(base, exist_ok=True)
        total = sum(m.file_size for m in z.infolist())
        free = shutil.disk_usage(base).free
        if len(z.infolist()) > 2_000_000 or total > 0.9 * free or total > 200 * max(1, os.path.getsize(zpath)):
            raise PxdError(f"refusing {zpath}: {len(z.infolist())} members, {total / 1e9:.1f} GB unpacked")
        for m in z.infolist():
            t = os.path.realpath(os.path.join(dest, m.filename))
            if not (t == base or t.startswith(base + os.sep)) or (m.external_attr >> 16) & 0o170000 == 0o120000:
                raise PxdError(f"unsafe member in {zpath}: {m.filename!r}")
        z.extractall(dest)
