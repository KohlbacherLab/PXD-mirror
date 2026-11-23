"""Converters (registry: tools.json, maintainer controlled; paths.tools) and output validators.

Provenance per conversion: tool name, version string (from the tool), sha256 of the executable(s) and of the
deployment files listed in tools.json (e.g. the RawFileReader assembly), argv, relevant environment, host,
staged-input MD5, output MD5, validator name/version and result. recipe = <tool>-<version>-<argshash8>
(paths and thread counts are not part of the hash), so two recipes never share an output path."""
import hashlib, json, os, re, shutil, stat, subprocess, sys, zipfile

from .util import PxdError, UsageError, log, replaceable, sha256sum

TOOLS_JSON = ""          # paths.tools (Mirror(config) sets it); empty: no converters configured
VALIDATOR = "pxdstore-validate 1"
EXT = {"mzML": ".mzML", "mzPeak": ".mzpeak"}
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(v, env):
    """~ and ${VAR} (from the environment, then from the tool's own env block) in registry strings."""
    if not isinstance(v, str):
        return v
    v = _VAR.sub(lambda m: env.get(m.group(1), os.environ.get(m.group(1), m.group(0))), v)
    return os.path.expanduser(v)


def load_tools(path=None):
    """The converter registry: {name: spec} with ~ and ${VAR} expanded in path, env and deployment_files."""
    path = path or TOOLS_JSON
    if not path:
        raise UsageError("no converter registry configured (paths.tools; see examples/tools.json)")
    try:
        with open(path) as fh:
            tools = json.load(fh)
            owner = os.fstat(fh.fileno()).st_uid
    except FileNotFoundError:
        raise UsageError(f"converter registry {path} not found (paths.tools)")
    except ValueError as e:
        raise PxdError(f"converter registry {path} unreadable: {e}")
    for name, spec in tools.items():
        if name.startswith("_"):
            continue
        env = {}
        for k, v in (spec.get("env") or {}).items():
            env[k] = _expand(v, env)
        spec["env"] = env
        spec["path"] = _expand(spec["path"], env)
        spec["deployment_files"] = [_expand(x, env) for x in spec.get("deployment_files", [])]
        spec["_registry_uid"] = owner          # the maintainer: trusted like root and the caller (see trust_scan)
    return {n: s for n, s in tools.items() if not n.startswith("_")}


_ID = {}
_WARNED = set()


def deploy_root(spec):
    exe = spec["path"]
    return os.path.dirname(os.path.dirname(exe)) if spec.get("deploy_root") == "prefix" else os.path.dirname(exe)


def _dep_path(spec, rel):
    return rel if rel.startswith("/") else os.path.join(deploy_root(spec), rel)


def trees(spec):
    """Directories whose whole content a tool may load: its deployment root and every absolute directory named in
    its env block (DOTNET_ROOT, LD_LIBRARY_PATH entries, ...), symlinks resolved, nested ones dropped."""
    cand = [deploy_root(spec)]
    for v in (spec.get("env") or {}).values():
        cand += [x for x in str(v).split(os.pathsep) if x.startswith("/") and os.path.isdir(x)]
    real = sorted({os.path.realpath(t) for t in cand})
    return [t for t in real if not any(t != u and t.startswith(u.rstrip("/") + "/") for u in real)]


def _in(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _ldd(paths, env):
    """Shared libraries the dynamic loader resolves for @p paths (Linux ldd; [] elsewhere or for scripts)."""
    if not sys.platform.startswith("linux") or not shutil.which("ldd"):
        return []
    out = set()
    for p in paths:
        try:
            r = subprocess.run(["ldd", p], capture_output=True, text=True, env=env, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode:
            continue                                    # not a dynamic ELF (a script, a .dll)
        for line in r.stdout.splitlines():
            m = re.search(r"(?:=>\s*)?(/\S+)\s+\(0x", line)
            if m:
                out.add(os.path.realpath(m.group(1)))
    return sorted(out)


def trust_scan(name, spec, extras=None):
    """(errors, warnings, entries, extras): what a user other than root, the registry's owner and the caller could
    change in what the tool runs. Every entry below its trees() must be owned by one of them and must not be
    writable for group or other; so must every library or symlink target outside the trees (and its directory).
    Directories above those (e.g. a group-writable parent) are reported as warnings: they let a member rename
    the whole deployment away, which only a sticky bit or root-only parents prevent. @p extras: libraries
    found by ldd before (else determined here). entries: [(path, lstat)] for the fingerprint."""
    trusted = {spec.get("_registry_uid", os.getuid())}
    errors, warnings, entries, outside = [], set(), [], set()
    roots = trees(spec)

    def bad(p, st):
        if st.st_uid not in trusted | {0, os.getuid()}:
            return "owned by another user"
        if st.st_mode & 0o022:
            return f"writable for {'group' if st.st_mode & 0o020 else 'others'}"
        return None
    for t in roots:
        for p, why in replaceable(t, trusted, ancestors_only=True):
            warnings.add(f"{p}: {why}")
        for dp, dns, fns in os.walk(t):
            for n in [None] + dns + fns:
                p = dp if n is None else os.path.join(dp, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode) and n is not None:
                    continue                            # visited as dp
                if stat.S_ISLNK(st.st_mode):
                    tgt = os.path.realpath(p)
                    if not _in(tgt, roots):
                        outside.add(tgt)
                    continue
                why = bad(p, st)
                if why:
                    errors.append(f"{p} {why}")
                entries.append((p, st))
    for p in [spec["path"]] + [_dep_path(spec, r) for r in spec.get("deployment_files", [])]:
        rp = os.path.realpath(p)
        if os.path.exists(rp) and not _in(rp, roots):
            outside.add(rp)
    if extras is None:
        env = dict(os.environ, **(spec.get("env") or {}))
        extras = _ldd([spec["path"]] + [_dep_path(spec, r) for r in spec.get("deployment_files", [])], env) if not errors else []
    outside |= {x for x in extras if not _in(x, roots)}
    for x in sorted(outside):
        for p, why in replaceable(x, trusted):
            (errors.append(f"{p} {why}") if p in (x, os.path.dirname(x)) else warnings.add(f"{p}: {why}"))
        try:
            entries.append((x, os.stat(x)))
        except OSError:
            pass
    return errors, sorted(warnings), entries, extras


def _fingerprint(entries):
    return hashlib.sha256(json.dumps([(p, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode,
                                       st.st_uid) for p, st in sorted(entries, key=lambda e: e[0])]).encode()).hexdigest()


def tree_hash(entries):
    """sha256 over (path, sha256) of every regular file a tool may load (its trees and outside libraries)."""
    h = hashlib.sha256()
    for p, st in sorted(entries, key=lambda e: e[0]):
        if stat.S_ISREG(st.st_mode):
            h.update(f"{p}\t{sha256sum(p)}\n".encode())
    return h.hexdigest()


def identify(name, spec):
    """Version string and hashes of one deployment. Refuses a tool that another user could change (trust_scan) or
    whose pins do not match: "sha256" (executable), "deployment_sha256" ({file: sha256}), "tree_sha256" (all
    files the tool may load, see tree_hash). Called before every conversion: the result is cached per process
    only while nothing in the deployment changed (inode, size, times, mode, owner of every entry)."""
    exe = spec["path"]
    key = (name, exe, spec.get("sha256"), json.dumps(spec.get("deployment_sha256"), sort_keys=True), spec.get("tree_sha256"))
    cached = _ID.get(key)
    errors, warnings, entries, extras = trust_scan(name, spec, cached["extras"] if cached else None)
    if errors:
        raise PxdError(f"tool {name}: {len(errors)} entries another user could change, e.g. {'; '.join(errors[:3])}; "
                       "refused (install converters where only maintainers can write: docs/PERMISSIONS.md)")
    for w in warnings:
        if (name, w) not in _WARNED:
            _WARNED.add((name, w))
            log(f"warning: tool {name}: {w}: another user could replace the whole deployment (docs/PERMISSIONS.md)")
    fp = _fingerprint(entries)
    if cached and cached["fp"] == fp:
        return cached["ident"]
    if not os.access(exe, os.X_OK):
        raise PxdError(f"tool {name}: {exe} not executable here")
    digest = sha256sum(exe)
    if spec.get("sha256") and spec["sha256"] != digest:
        raise PxdError(f"tool {name}: {exe} sha256 {digest[:16]}... does not match the registry ({spec['sha256'][:16]}...); refused")
    dep = {}
    for rel in spec.get("deployment_files", []):
        p = _dep_path(spec, rel)
        dep[rel] = sha256sum(p) if os.path.exists(p) else "missing"
        pin = (spec.get("deployment_sha256") or {}).get(rel)
        if pin and pin != dep[rel]:
            raise PxdError(f"tool {name}: deployment file {rel} does not match its pinned sha256; refused")
    th = tree_hash(entries)
    if spec.get("tree_sha256") and spec["tree_sha256"] != th:
        raise PxdError(f"tool {name}: the files it may load changed (tree sha256 {th[:16]}... != pinned "
                       f"{spec['tree_sha256'][:16]}...); refused")
    env = dict(os.environ, **spec.get("env", {}))
    r = subprocess.run([exe] + spec["version_args"], capture_output=True, text=True, env=env, timeout=300)
    m = re.search(spec["version_regex"], (r.stdout or "") + (r.stderr or ""))
    if not m:
        raise PxdError(f"tool {name}: no version in the output of {spec['version_args']}")
    ident = {"name": name, "version": " ".join(g.strip() for g in m.groups() if g) or m.group(0), "path": exe,
             "sha256": digest, "deployment": dep, "tree_sha256": th, "env": spec.get("env", {})}
    if fp != _fingerprint(trust_scan(name, spec, extras)[2]):
        raise PxdError(f"tool {name}: its deployment changed while it was being identified; try again")
    _ID[key] = {"fp": fp, "extras": extras, "ident": ident}
    return ident


def recipe(name, ident, spec, fmt):
    """<tool>-<version>-<hash of format, fixed arguments and deployment files>. A maintainer who relinks or moves a
    deployment without changing what it computes may declare that in "recipe_deployment_sha256" ({file: the
    earlier sha256}): the recipe name then stays, while the provenance records the actual hashes. After moving
    the pins to other files (e.g. to the copies the tool actually loads), "recipe_deployment" ({file: sha256},
    the complete earlier set) replaces the deployment files in the hash altogether."""
    fixed = [a for a in spec["argv"][fmt] if a not in ("{in}", "{out}", "{threads}")]
    same = spec.get("recipe_deployment_sha256") or {}
    dep = dict(spec["recipe_deployment"]) if spec.get("recipe_deployment") else \
        {k: same.get(k, v) for k, v in ident["deployment"].items()}
    h = hashlib.sha256(json.dumps([fmt, fixed, sorted(dep.items())]).encode()).hexdigest()[:8]
    v = re.sub(r"[^A-Za-z0-9.]+", "-", ident["version"])[:40].strip("-")
    return f"{name}-{v}-{h}"


def pick(tools, src_name, fmt, tool=None, allow_trfp2=False):
    """Choose a converter for (input, format); refuse TRFP 2.0.0 unless allowed."""
    low = src_name.lower()
    cands = [tool] if tool else [n for n, s in tools.items() if s.get("default_for", {}).get(fmt)
                                 and any(low.endswith(x) for x in s["default_for"][fmt])]
    if not cands or cands[0] not in tools:
        raise UsageError(f"no converter for {src_name} -> {fmt} (tools: {', '.join(tools)})")
    name = cands[0]
    spec = tools[name]
    if fmt not in spec["argv"]:
        raise UsageError(f"{name} cannot write {fmt}")
    if spec.get("refused_unless") == "allow_trfp2" and not allow_trfp2:
        raise UsageError(f"{name} is refused by default ({spec.get('why', 'known defect')}); pass --allow-trfp2")
    return name, spec


def argv(spec, fmt, src, out, threads):
    def sub(a):                     # placeholders also inside an argument, e.g. -i={in}
        return a.replace("{in}", src).replace("{out}", out).replace("{threads}", str(threads))
    return [spec["path"]] + [sub(a) for a in spec["argv"][fmt]]


# ------------------------------------------------------------------ validators
def validate_mzml(path):
    """Indexed mzML: every <offset idRef=...> must land on <spectrum/<chromatogram with that id; the spectrum
    index count must equal spectrumList count. Unindexed mzML passes with indexed=False (flagged)."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 4096))
        tail = fh.read()
        m = re.search(rb"<indexListOffset>\s*(\d+)\s*</indexListOffset>", tail)
        fh.seek(0)
        head = fh.read(1 << 20)
        cm = re.search(rb'<spectrumList[^>]*\bcount="(\d+)"', head)
        if not m:
            return {"ok": True, "indexed": False, "spectra": int(cm.group(1)) if cm else None}
        off = int(m.group(1))
        if off >= size:
            return {"ok": False, "why": f"indexListOffset {off} beyond file size {size}"}
        fh.seek(off)
        idx = fh.read(size - off)
        if not idx.lstrip().startswith(b"<indexList"):
            return {"ok": False, "why": "indexListOffset does not point at <indexList>"}
        n = {"spectrum": 0, "chromatogram": 0}
        bad = []
        for blk in re.finditer(rb'<index name="(\w+)">(.*?)</index>', idx, re.S):
            kind = blk.group(1).decode()
            for e in re.finditer(rb'<offset idRef="([^"]*)">\s*(\d+)\s*</offset>', blk.group(2)):
                n[kind] = n.get(kind, 0) + 1
                fh.seek(int(e.group(2)))
                chunk = fh.read(2048)
                if not (chunk.startswith(b"<" + kind.encode()) and (b'id="' + e.group(1) + b'"') in chunk):
                    bad.append(e.group(1).decode(errors="replace"))
                    if len(bad) >= 5:
                        break
    spectra = int(cm.group(1)) if cm else None
    ok = not bad and (spectra is None or spectra == n["spectrum"])
    return {"ok": ok, "indexed": True, "spectra": spectra, "indexed_spectra": n["spectrum"],
            "indexed_chromatograms": n["chromatogram"], "bad_offsets": bad,
            **({} if ok else {"why": f"{len(bad)} offsets off target" if bad else "spectrum count != index count"})}


def validate_mzpeak(path):
    """Structural: a zip whose CRCs check, with at least one non-empty Parquet member (no pyarrow here)."""
    try:
        with zipfile.ZipFile(path) as z:
            badm = z.testzip()
            members = {i.filename: i.file_size for i in z.infolist()}
    except zipfile.BadZipFile as e:
        return {"ok": False, "why": f"not a zip: {e}"}
    pq = {k: v for k, v in members.items() if k.endswith(".parquet")}
    ok = badm is None and any(v > 0 for v in pq.values())
    return {"ok": ok, "members": len(members), "parquet": sorted(pq), **({} if ok else {
        "why": f"CRC error in {badm}" if badm else "no non-empty Parquet member"})}


def validate(fmt, path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"ok": False, "why": "missing or empty output"}
    return validate_mzml(path) if fmt == "mzML" else validate_mzpeak(path)
