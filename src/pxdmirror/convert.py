"""Converters (registry: tools.json, maintainer controlled; paths.tools) and output validators.

Provenance per conversion: tool name, version string (from the tool), sha256 of the executable(s) and of the
deployment files listed in tools.json (e.g. the RawFileReader assembly), argv, relevant environment, host,
staged-input MD5, output MD5, validator name/version and result. recipe = <tool>-<version>-<argshash8>
(paths and thread counts are not part of the hash), so two recipes never share an output path."""
import hashlib, json, os, re, subprocess, zipfile

from .util import PxdError, UsageError, sha256sum

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
    return {n: s for n, s in tools.items() if not n.startswith("_")}


_ID = {}


def identify(name, spec):
    """Version string and hashes of one deployment (cached per process). A "sha256" pinned in the registry
    (executable) or "deployment_sha256" ({file: sha256}) must match, else the tool is refused."""
    exe = spec["path"]
    key = (name, exe, spec.get("sha256"), json.dumps(spec.get("deployment_sha256"), sort_keys=True))
    if key in _ID:
        return _ID[key]
    if not os.access(exe, os.X_OK):
        raise PxdError(f"tool {name}: {exe} not executable here")
    digest = sha256sum(exe)
    if spec.get("sha256") and spec["sha256"] != digest:
        raise PxdError(f"tool {name}: {exe} sha256 {digest[:16]}... does not match the registry ({spec['sha256'][:16]}...); refused")
    dep = {}
    for rel in spec.get("deployment_files", []):
        p = rel if rel.startswith("/") else os.path.join(os.path.dirname(os.path.dirname(exe)) if spec.get("deploy_root") == "prefix" else os.path.dirname(exe), rel)
        dep[rel] = sha256sum(p) if os.path.exists(p) else "missing"
        pin = (spec.get("deployment_sha256") or {}).get(rel)
        if pin and pin != dep[rel]:
            raise PxdError(f"tool {name}: deployment file {rel} does not match its pinned sha256; refused")
    env = dict(os.environ, **spec.get("env", {}))
    r = subprocess.run([exe] + spec["version_args"], capture_output=True, text=True, env=env, timeout=300)
    m = re.search(spec["version_regex"], (r.stdout or "") + (r.stderr or ""))
    if not m:
        raise PxdError(f"tool {name}: no version in the output of {spec['version_args']}")
    _ID[key] = {"name": name, "version": " ".join(g.strip() for g in m.groups() if g) or m.group(0), "path": exe,
                 "sha256": digest, "deployment": dep, "env": spec.get("env", {})}
    return _ID[key]


def recipe(name, ident, spec, fmt):
    fixed = [a for a in spec["argv"][fmt] if a not in ("{in}", "{out}", "{threads}")]
    h = hashlib.sha256(json.dumps([fmt, fixed, sorted(ident["deployment"].items())]).encode()).hexdigest()[:8]
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
