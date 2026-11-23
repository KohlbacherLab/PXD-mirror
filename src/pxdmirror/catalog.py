"""SQLite catalogue: a projection of the manifests, inventories and the event log (those are authoritative).

CephFS has POSIX locks but no cross-node shared memory, so: rollback journal (journal_mode=DELETE, no WAL),
busy_timeout, and every write transaction under an outer flock (catalog.lock) with its own timeout.
reindex rebuilds in place inside one transaction; the database file is never replaced.
"""
import json, os, sqlite3, urllib.parse

from . import util as U
from .util import PxdError, flock, own_mode

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets(accession TEXT PRIMARY KEY, title TEXT, n_files_pride INTEGER,
  bytes_pride INTEGER, inventory_gen TEXT, status TEXT, n_ok INTEGER, bytes_ok INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS files(accession TEXT, name TEXT, category TEXT, size INTEGER, md5_pride TEXT,
  md5 TEXT, verification TEXT, status TEXT, method TEXT, path TEXT, downloaded TEXT, verified TEXT,
  migrated_from TEXT, deleted TEXT, PRIMARY KEY(accession, name));
CREATE TABLE IF NOT EXISTS tools(name TEXT, version TEXT, path TEXT, sha256 TEXT, deployment TEXT,
  first_used TEXT, PRIMARY KEY(name, version, sha256));
CREATE TABLE IF NOT EXISTS conversions(id TEXT PRIMARY KEY, accession TEXT, source_name TEXT, source_md5 TEXT,
  format TEXT, recipe TEXT, output_path TEXT, tool_name TEXT, tool_version TEXT, tool_sha256 TEXT,
  argv TEXT, env TEXT, host TEXT, started TEXT, seconds REAL, output_size INTEGER, output_md5 TEXT,
  validator TEXT, validation TEXT, status TEXT, UNIQUE(accession, source_name, format, recipe));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, user TEXT, host TEXT,
  action TEXT, accession TEXT, detail TEXT);
"""


class Catalog:
    def __init__(self, root, lock_wait=300):
        self.path = os.path.join(root, "catalog.sqlite")
        self.lock = os.path.join(root, "catalog.lock")
        self.lock_wait = lock_wait

    def _connect(self, readonly=False):
        if readonly:
            if not os.path.isfile(self.path) or os.path.islink(self.path):
                return None
            con = sqlite3.connect(f"file:{urllib.parse.quote(self.path)}?mode=ro", uri=True, timeout=60)
        else:
            # Create the file ourselves with the mirror's file mode before SQLite does (SQLite would create it
            # 0644 & ~umask, i.e. not writable for the other members of the mirror's group). Rollback journals
            # copy the database file's mode.
            try:
                os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, U.FILE_MODE))
            except PermissionError:
                pass                            # exists and not writable for us: sqlite reports it below
            own_mode(self.path, U.FILE_MODE)
            con = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=60000")
        con.row_factory = sqlite3.Row
        return con

    def write(self, fn):
        """Run fn(con) in one IMMEDIATE transaction under the outer catalog lock. SQLite errors (e.g. a
        catalogue that is read-only for this user) become PxdError."""
        try:
            return self._write(fn)
        except sqlite3.Error as e:
            raise PxdError(f"catalogue {self.path}: {e} (the manifests are authoritative; fix the cause, then pxd reindex)")

    def _write(self, fn):
        with flock(self.lock, exclusive=True, wait=self.lock_wait) as fd:
            if fd is None:
                raise PxdError(f"catalog busy for {self.lock_wait} s ({self.lock})")
            con = self._connect()
            try:
                con.executescript(SCHEMA)
                con.execute("BEGIN IMMEDIATE")
                try:
                    out = fn(con)
                    con.execute("COMMIT")
                    return out
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.close()

    def read(self, sql, args=()):
        """Rows, or [] when there is no catalogue or it cannot be read now (callers fall back to the manifests)."""
        try:
            con = self._connect(readonly=True)
        except sqlite3.Error:
            return []
        if con is None:
            return []
        try:
            return [dict(r) for r in con.execute(sql, args)]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    # --------------------------------------------------------------- projection
    @staticmethod
    def put_dataset(con, acc, man, inv, status=None):
        """Replace the rows of one data set from its manifest and inventory snapshot."""
        con.execute("DELETE FROM files WHERE accession=?", (acc,))
        con.execute("DELETE FROM conversions WHERE accession=?", (acc,))
        n_ok = b_ok = 0
        for name, f in sorted(man.get("files", {}).items()):
            if f.get("status") == "ok":
                n_ok += 1
                b_ok += f.get("size") or 0
            con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                acc, name, f.get("category"), f.get("size"), f.get("md5_pride"), f.get("md5"),
                f.get("verification"), f.get("status"), f.get("method"), f.get("path"), f.get("downloaded"),
                f.get("verified"), f.get("migrated_from"), f.get("deleted")))
        for cid, c in sorted(man.get("conversions", {}).items()):
            con.execute("INSERT OR REPLACE INTO conversions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                cid, acc, c["source_name"], c.get("source_md5"), c["format"], c["recipe"], c.get("output_path"),
                c["tool"]["name"], c["tool"]["version"], c["tool"].get("sha256"), json.dumps(c.get("argv")),
                json.dumps(c.get("env")), c.get("host"), c.get("started"), c.get("seconds"), c.get("output_size"),
                c.get("output_md5"), c.get("validator"), json.dumps(c.get("validation")), c.get("status")))
            t = c["tool"]
            con.execute("INSERT OR IGNORE INTO tools VALUES(?,?,?,?,?,?)", (
                t["name"], t["version"], t.get("path"), t.get("sha256"), json.dumps(t.get("deployment")),
                c.get("started")))
        files = (inv or {}).get("files", {})
        st = status or man.get("status") or ("ok" if files and n_ok == len(files) else "partial" if n_ok else "empty")
        con.execute("INSERT OR REPLACE INTO datasets VALUES(?,?,?,?,?,?,?,?,?)", (
            acc, (inv or {}).get("title"), len(files) if files else None,
            sum(int(f.get("fileSizeBytes") or 0) for f in files.values()) if files else None,
            (inv or {}).get("gen"), st, n_ok, b_ok, man.get("updated")))

    @staticmethod
    def put_event(con, ev):
        con.execute("INSERT INTO events(time,user,host,action,accession,detail) VALUES(?,?,?,?,?,?)",
                    (ev["time"], ev["user"], ev["host"], ev["action"], ev.get("accession"),
                     json.dumps(ev.get("detail"))))
