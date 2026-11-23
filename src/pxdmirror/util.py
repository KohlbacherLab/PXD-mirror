"""Shared helpers: time, hashing, safe names and paths, locks, atomic writes, fsync, quota.

Every path inside the mirror is built from validated components (safe_name) and checked to have no
symlinked component below the root (inside). Lock files are opened O_NOFOLLOW and never truncated.
"""
import datetime, errno, fcntl, hashlib, json, os, re, socket, stat, time
from contextlib import contextmanager

ACC_RE = re.compile(r"^PXD\d{6}$")
DIR_MODE, FILE_MODE = 0o2770, 0o660          # group-shared defaults; Mirror(config) calls set_modes()


def set_modes(dir_mode, file_mode):
    """Modes for every directory and file pxd creates (process-wide)."""
    global DIR_MODE, FILE_MODE
    DIR_MODE, FILE_MODE = dir_mode, file_mode


class PxdError(Exception):
    """Operational error (CLI exit 3)."""


class UsageError(PxdError):
    """Bad request (CLI exit 2)."""


class CapacityError(PxdError):
    """Quota or disk full: terminal, never retried as a transport error."""


class CorruptRecord(PxdError):
    """An authoritative record (manifest, inventory, plan) cannot be read; reported, never reset."""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opid(prefix="op"):
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}-{socket.gethostname().split('.')[0]}-{os.getpid()}-{time.time_ns() % 1000000:06d}"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=__import__("sys").stderr, flush=True)


def user():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return str(os.getuid())


def current_umask():
    u = os.umask(0)
    os.umask(u)
    return u


def md5sum(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256sum(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_acc(acc):
    if not isinstance(acc, str) or not ACC_RE.match(acc):
        raise UsageError(f"not a PXD accession: {acc!r}")
    return acc


def safe_name(name):
    """One path component from PRIDE, a manifest or the user: no separators, no '..', no leading dot."""
    if (not isinstance(name, str) or not name or len(name) > 255 or "/" in name or "\\" in name
            or "\0" in name or name.startswith(".") or name in ("..",)):
        raise UsageError(f"unsafe file name {name!r}")
    return name


def safe_rel(rel):
    """A manifest-relative path (e.g. 'mzML/<recipe>/<x>.mzML'): every component safe, not absolute."""
    if not isinstance(rel, str) or rel.startswith("/") or not rel:
        raise UsageError(f"unsafe relative path {rel!r}")
    for part in rel.split("/"):
        safe_name(part)
    return rel


def inside(root, path):
    """Raise unless `path` is below `root` and no existing component between them is a symlink."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    if not (path == root or path.startswith(root + os.sep)):
        raise PxdError(f"{path} is outside the mirror {root}")
    cur = root
    for part in os.path.relpath(path, root).split(os.sep):
        if part in ("", "."):
            continue
        cur = os.path.join(cur, part)
        try:
            if stat.S_ISLNK(os.lstat(cur).st_mode):
                raise PxdError(f"symlink inside the mirror: {cur}")
        except FileNotFoundError:
            break
    return path


def own_mode(fd_or_path, mode):
    """Set `mode` on a file or directory we own (umask and ACL masks may have narrowed it)."""
    try:
        st = os.fstat(fd_or_path) if isinstance(fd_or_path, int) else os.stat(fd_or_path)
        if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) != mode:
            (os.fchmod if isinstance(fd_or_path, int) else os.chmod)(fd_or_path, mode)
    except OSError:
        pass


def _uname(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, TypeError, OverflowError):
        return f"uid {uid}"


def replaceable(path, trusted=(), ancestors_only=False):
    """[(path, reason)] for every component of realpath(@p path), from the object itself up to '/', through which
    a user outside @p trusted (always root and the caller) could change or replace it: an entry owned by somebody
    else, a file writable for group or other, a directory writable for group or other (above the object a
    directory with the sticky bit is fine: there only owners rename their own entries). Group write granted by
    an ACL shows in the group bits (the ACL mask), so it is covered."""
    trusted = {0, os.getuid()} | set(trusted)
    out = []
    p = os.path.realpath(path)
    first = True
    while True:
        try:
            st = os.lstat(p)
        except OSError as e:
            out.append((p, f"cannot be checked ({e.strerror})"))
            break
        if not (first and ancestors_only):
            isdir = stat.S_ISDIR(st.st_mode)
            if st.st_uid not in trusted:
                out.append((p, f"owned by {_uname(st.st_uid)}"))
            elif st.st_mode & 0o022 and not (isdir and not first and st.st_mode & stat.S_ISVTX):
                out.append((p, f"{'directory' if isdir else 'file'} writable for {'group' if st.st_mode & 0o020 else 'others'}"
                               + ("" if first or not isdir else " without the sticky bit")))
        first = False
        if p == "/":
            break
        p = os.path.dirname(p)
    return out


def private_dir(path):
    """Create (mode 0700) or accept a per-user directory and return its real path, refusing one that somebody
    else could tamper with: it must be ours, not writable for group or other, and no directory above it may let
    another user rename it away (a world-writable parent without the sticky bit, such as some /scratch)."""
    mkdirs(path, 0o700)
    real = os.path.realpath(path)
    st = os.lstat(real)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PxdError(f"{path} is not a directory of yours")
    if st.st_mode & 0o077:
        os.chmod(real, stat.S_IMODE(st.st_mode) & ~0o077)        # ours: make it private
    bad = replaceable(real, ancestors_only=True)
    if bad:
        raise PxdError(f"{path}: another user could replace it ({bad[0][0]}: {bad[0][1]}); use a directory below one "
                       "that only you or root can write, or one with the sticky bit (such as /tmp)")
    return real


def mkdirs(path, mode=None):
    """mkdir -p; every directory created here gets `mode` (default DIR_MODE, e.g. 2770: setgid keeps the group)."""
    mode = DIR_MODE if mode is None else mode
    if os.path.isdir(path):
        return path
    parent = os.path.dirname(path)
    if parent and parent != path:
        mkdirs(parent, mode)
    try:
        os.mkdir(path, mode)
        try:
            os.chmod(path, mode)       # umask may have stripped bits; only directories created here
        except PermissionError:
            pass
    except FileExistsError:
        pass
    return path


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def capacity_guard(e):
    """Turn quota/disk-full errors into CapacityError (terminal)."""
    if isinstance(e, OSError) and e.errno in (errno.EDQUOT, errno.ENOSPC):
        raise CapacityError(f"no space or quota exhausted: {e}") from e
    raise e


def atomic_write(path, data, mode=None):
    """Write to an exclusive temp in the same directory, fsync, rename over, fsync the directory.
    @p mode: file mode (default FILE_MODE)."""
    mode = FILE_MODE if mode is None else mode
    d = os.path.dirname(path)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp.{socket.gethostname()}.{os.getpid()}.{time.time_ns()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        own_mode(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        fsync_dir(d)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        capacity_guard(e)


def write_json(path, obj, mode=None):
    atomic_write(path, json.dumps(obj, indent=1, sort_keys=True) + "\n", mode)


def read_own_json(path, what="record"):
    """(object, owner uid, problem) of a JSON record read through one O_NOFOLLOW descriptor, so the owner and
    the content belong to the same file. problem is None only if the file is owned by the caller and nobody
    else may write it (not group- or other-writable): the content is then the caller's own."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None, None, None
    except OSError as e:
        raise CorruptRecord(f"unreadable {what} {path}: {e}")
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CorruptRecord(f"{what} {path} is not a regular file")
        try:
            obj = json.loads(fh.read().decode())
        except ValueError as e:
            raise CorruptRecord(f"unreadable {what} {path}: {e}")
    problem = None
    if st.st_uid != os.getuid():
        problem = f"written by {_uname(st.st_uid)}"
    elif st.st_mode & 0o022:
        problem = "writable for other users (not written by this version of pxd, or changed since)"
    return obj, st.st_uid, problem


def read_json(path, what="record"):
    """None if missing; CorruptRecord if unreadable (never reset here: read paths do not write)."""
    try:
        with open(path) as fh:
            obj = json.load(fh)
        if obj is None:
            raise ValueError("null record")
        return obj
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise CorruptRecord(f"unreadable {what} {path}: {e}")


def append_line(path, line):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    own_mode(fd, FILE_MODE)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode())
        os.fsync(fd)
    except OSError as e:
        capacity_guard(e)
    finally:
        os.close(fd)


@contextmanager
def flock(path, exclusive=True, wait=0.0):
    """flock on a lock file opened O_NOFOLLOW without truncation. wait=0: try once, yield None if busy;
    wait>0: retry up to `wait` seconds, then yield None. The fd is yielded so children can inherit it."""
    mkdirs(os.path.dirname(path))
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    except PermissionError:
        # another member's lock file that is not group-writable: flock works on a read-only descriptor too
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    own_mode(fd, FILE_MODE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PxdError(f"lock {path} is not a regular file")
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        t_end, got = time.time() + wait, False
        while True:
            try:
                fcntl.flock(fd, mode)
                got = True
                break
            except BlockingIOError:
                if time.time() >= t_end:
                    break
                time.sleep(min(1.0, max(0.05, t_end - time.time())))
        if not got:
            yield None
            return
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def quota_free(path):
    """Free bytes in the CephFS quota realm containing `path` (nearest ancestor with a quota), else disk free."""
    p = os.path.abspath(path)
    while True:
        try:
            q = int(os.getxattr(p, "ceph.quota.max_bytes") or 0)
            if q > 0:
                return q - int(os.getxattr(p, "ceph.dir.rbytes")), p
        except (OSError, ValueError, AttributeError):
            pass
        if p == "/":
            break
        p = os.path.dirname(p)
    import shutil
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free, "disk"
