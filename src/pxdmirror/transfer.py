"""Transports: HTTPS (optionally k byte ranges), FTP, Globus batch. Each object downloads into its own private
work directory; nothing here publishes (store.publish does). Children are always reaped before returning."""
import json, os, re, shutil, socket, subprocess, time

from .config import EBI_PUBLIC
from .util import CapacityError, PxdError, log, mkdirs

SPLIT_MIN = 100e6        # transfer.split_min_mb

# Globus settings (process-wide; Mirror(config) calls configure_globus). Off until a destination is configured.
G = {"enabled": False, "cli": "globus", "source": EBI_PUBLIC, "dest": "", "kind": "gcp", "path_map": [],
     "gcp_config_dir": os.path.expanduser("~/.globusonline"), "check_config_paths": True, "shared_prefixes": []}


def configure_globus(**kw):
    unknown = set(kw) - set(G)
    if unknown:
        raise ValueError(f"unknown Globus settings {sorted(unknown)}")
    G.update(kw)


class Permanent(Exception):
    """This method cannot work for this file (403/404, DNS, bad URL): do not retry it."""


class Unresumable(Exception):
    """The partial cannot be resumed: restart from zero."""


def deadline(size):
    return 600 + max(size or 0, 0) / 300e3


def _curl_err(rc, err):
    if rc == 23 and re.search(r"quota|No space", err, re.I):
        raise CapacityError(err)
    if rc == 23 and re.search(r"Permission denied|Operation not permitted", err, re.I):
        raise Unresumable(err)          # e.g. another member's partial that we cannot append to: start afresh
    if rc in (33, 36) or "416" in err:
        raise Unresumable(err)
    if (rc == 22 and re.search(r"\b(403|404|410)\b", err)) or rc in (1, 3, 6):
        raise Permanent(err)
    raise RuntimeError(f"curl rc={rc}: {err}")


def curl_one(url, out, size, lock_fd):
    """One stream into `out` (resumed with -C -)."""
    limit = deadline(size)
    cmd = ["curl", "-sS", "-f", "-L", "-C", "-", "--connect-timeout", "30", "--max-time", str(int(limit)),
           "--speed-time", "120", "--speed-limit", "100000", "-o", out, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, pass_fds=(lock_fd,), timeout=limit + 30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"transfer exceeded {limit:.0f} s, killed")
    if r.returncode:
        _curl_err(r.returncode, (r.stderr or "").strip()[-300:])


def curl_split(url, workdir, out, size, k, lock_fd):
    """k byte ranges in workdir/ranges/ (boundaries saved in ranges/plan.json so a resume cuts identically),
    then assembled into `out`. All children are reaped in finally."""
    rd = mkdirs(os.path.join(workdir, "ranges"))
    plan_p = os.path.join(rd, "plan.json")
    plan = json.load(open(plan_p)) if os.path.exists(plan_p) else None
    if not plan or plan.get("size") != size:
        for f in os.listdir(rd):
            os.unlink(os.path.join(rd, f))
        step = -(-size // k)
        plan = {"size": size, "ranges": [[i * step, min(size, (i + 1) * step) - 1] for i in range(k)]}
        json.dump(plan, open(plan_p, "w"))
    procs, errs = [], []
    try:
        for i, (a, b) in enumerate(plan["ranges"]):
            piece = os.path.join(rd, f"s{i}")
            have = os.path.getsize(piece) if os.path.exists(piece) else 0
            if have > b - a + 1:
                os.unlink(piece); have = 0
            if have == b - a + 1:
                continue
            tmp = piece + ".tmp"
            cmd = ["curl", "-sS", "-f", "-L", "--connect-timeout", "30", "--speed-time", "120",
                   "--speed-limit", "50000", "-r", f"{a + have}-{b}", "-o", tmp, url]
            procs.append((piece, tmp, b - a + 1 - have, subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, pass_fds=(lock_fd,))))
        t_end = time.time() + deadline(size / k) + 30
        for piece, tmp, want, p in procs:
            try:
                _, err = p.communicate(timeout=max(1, t_end - time.time()))
            except subprocess.TimeoutExpired:
                p.kill(); _, err = p.communicate(); err = "range transfer timed out"
            got = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if p.returncode == 0 and got > want:
                raise Unresumable("server ignores byte ranges")
            if got:
                with open(piece, "ab") as o, open(tmp, "rb") as s:
                    shutil.copyfileobj(s, o, 1 << 20)
            if p.returncode:
                errs.append((p.returncode, (err or "").strip()[-200:]))
    finally:
        for _, tmp, _, p in procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if os.path.exists(tmp):
                os.unlink(tmp)
    if errs:
        _curl_err(*errs[0])
    try:
        with open(out, "wb") as o:
            for i in range(len(plan["ranges"])):
                with open(os.path.join(rd, f"s{i}"), "rb") as s:
                    shutil.copyfileobj(s, o, 1 << 20)
    except OSError as e:
        if e.errno in (122, 28):
            raise CapacityError(str(e))
        raise
    shutil.rmtree(rd)


# ------------------------------------------------------------------ Globus
def dest_path(local):
    """Destination path on the Globus collection for a local absolute path (globus.path_map)."""
    if not G["path_map"]:
        return local
    lp, cp = G["path_map"][0].rstrip("/"), G["path_map"][1].rstrip("/")
    if local == lp or local.startswith(lp + "/"):
        return cp + local[len(lp):] or "/"
    raise PxdError(f"{local} is outside globus.path_map {G['path_map'][0]}")


def _cli():
    return shutil.which(G["cli"])


def gcp_config_paths(cfgdir):
    """[(absolute path, sharing, writable)] from <cfgdir>/lta/config-paths (GCP's own format: path,sharing,rw).
    '~' means the home of the user running GCP: with the default config dir that is this user."""
    out = []
    with open(os.path.join(cfgdir, "lta", "config-paths")) as fh:
        for line in fh:
            parts = [p.strip() for p in line.strip().split(",")]
            if not parts or not parts[0] or parts[0].startswith("#"):
                continue
            p = os.path.expanduser(parts[0])
            rw = len(parts) >= 3 and parts[2] == "1"
            out.append((os.path.realpath(p).rstrip("/") or "/", parts[1:2] == ["1"], rw))
    return out


def globus_usable(root):
    """None if Globus can write into `root`, else the reason it cannot."""
    if not G["enabled"]:
        return "Globus is not enabled (globus.enabled)"
    if not G["dest"]:
        return "no destination collection configured (globus.dest_collection)"
    cli = _cli()
    if not cli:
        return f"no globus CLI ({G['cli']}; install globus-cli or set globus.cli)"
    rr = os.path.realpath(root)
    if G["kind"] == "gcp":
        cfgdir = os.path.expanduser(G["gcp_config_dir"])
        if G["check_config_paths"]:
            try:
                entries = gcp_config_paths(cfgdir)
            except OSError:
                return (f"no {cfgdir}/lta/config-paths: this user does not run Globus Connect Personal here "
                        "(set globus.check_config_paths = false if the endpoint belongs to someone who shares it with you)")
            match = [e for e in entries if rr == e[0] or rr.startswith(e[0].rstrip("/") + "/")]
            if not match:
                return f"{root} is not in the GCP config-paths {[e[0] for e in entries]} (add it, then restart GCP)"
            if not max(match, key=lambda e: len(e[0]))[2]:
                return f"{root} is read-only in the GCP config-paths (set the third field to 1, then restart GCP)"
        shared = any(rr == os.path.realpath(p).rstrip("/") or rr.startswith(os.path.realpath(p).rstrip("/") + "/")
                     for p in G["shared_prefixes"])
        if not shared:
            pat = r"gc\.py .*" + re.escape(os.path.basename(cfgdir.rstrip("/")))
            try:
                running = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", pat], capture_output=True).returncode == 0
            except OSError:
                return "pgrep not available to check for a local GCP process (set globus.shared_prefixes)"
            if not running:
                return (f"{root} is not under globus.shared_prefixes and GCP does not run on {socket.gethostname()} "
                        "for this user")
        try:
            r = subprocess.run([cli, "endpoint", "show", G["dest"], "-F", "unix", "--jmespath", "gcp_connected"],
                               capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"GCP {G['dest']} status query failed ({type(e).__name__})"
        if r.returncode or r.stdout.strip() != "True":
            return f"GCP {G['dest']} not connected or not visible to this Globus login ({(r.stderr or r.stdout).strip()[:160]})"
        return None
    # gcs: probe the mapped root on the collection
    try:
        target = dest_path(rr)
    except PxdError as e:
        return str(e)
    try:
        r = subprocess.run([cli, "ls", f"{G['dest']}:{target}", "-F", "unix"], capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"collection {G['dest']} probe failed ({type(e).__name__})"
    if r.returncode == 4 or "ConsentRequired" in (r.stderr or "") or "consent" in (r.stderr or "").lower():
        return (f"collection {G['dest']}: not logged in or a one-time data_access consent is missing; run "
                f"'globus ls {G['dest']}:{target}' and follow what the CLI prints (globus login / globus session consent)")
    if r.returncode:
        return f"collection {G['dest']}:{target} not accessible ({(r.stderr or r.stdout).strip()[:160]})"
    return None


def globus_status(task):
    """Task status; 'UNKNOWN (...)' when the query fails or times out (the task may still run: poll again later)."""
    try:
        r = subprocess.run([_cli() or G["cli"], "task", "show", task, "-F", "unix", "--jmespath", "status"],
                           capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"UNKNOWN ({type(e).__name__}: {str(e)[:80]})"
    return r.stdout.strip() if r.returncode == 0 else f"UNKNOWN ({(r.stderr or '').strip()[:80]})"


def globus_task_by_label(label):
    """The id of the newest task labelled @p label (truncated as submitted), or None."""
    try:
        r = subprocess.run([_cli() or G["cli"], "task", "list", "--filter-label", label[:120], "--exact", "--limit", "1",
                            "-F", "unix", "--jmespath", "DATA[0].task_id"], capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    t = r.stdout.strip() if r.returncode == 0 else ""
    return t if t and t != "None" else None


def _batch_quote(p):
    if '"' in p or "\n" in p:
        raise PxdError(f"path not usable in a Globus batch: {p!r}")
    return f'"{p}"'


def globus_submit(pairs, label):
    """pairs: [(source path on the source collection, absolute local destination)] -> task id. @p label must be
    unique: a submit that times out may still have created the task, which is then found by its label and adopted
    (no second transfer); without one it raises PxdError, so the caller can fall back."""
    batch = "".join(f"{_batch_quote(s)} {_batch_quote(dest_path(os.path.realpath(d)))}\n" for s, d in pairs)
    try:
        r = subprocess.run([_cli() or G["cli"], "transfer", G["source"], G["dest"], "--batch", "-", "--sync-level", "checksum",
                            "--label", label[:120], "--jmespath", "task_id", "-F", "unix"],
                           input=batch, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        task = globus_task_by_label(label)
        if task:
            log(f"globus submit {type(e).__name__}; adopting task {task} labelled {label[:120]!r}")
            return task
        raise PxdError(f"globus submit failed ({type(e).__name__}); no task labelled {label[:120]!r}")
    if r.returncode or not r.stdout.strip():
        raise PxdError(f"globus submit failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def globus_wait(task, hours):
    """'SUCCEEDED', 'FAILED', or 'ACTIVE' if it is still running after a cancel attempt (caller must not fall back)."""
    cli = _cli() or G["cli"]
    try:
        w = subprocess.run([cli, "task", "wait", task, "--timeout", str(int(hours * 3600)),
                            "--polling-interval", "120"], capture_output=True, text=True)
        wrc = w.returncode
    except OSError as e:
        log(f"globus task {task}: wait failed ({type(e).__name__})")
        wrc = 1
    st = globus_status(task)
    if st == "SUCCEEDED":
        return st
    if st in ("ACTIVE", "INACTIVE") or wrc:
        try:
            subprocess.run([cli, "task", "cancel", task], capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f"globus task {task}: cancel failed ({type(e).__name__})")
        st = globus_status(task)
        log(f"globus task {task}: cancel requested, status now {st}")
    return "FAILED" if st in ("FAILED", "CANCELED", "CANCELLED") else st
