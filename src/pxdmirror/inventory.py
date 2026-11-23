"""PRIDE inventory snapshots: project.json, files.json, checksum.tsv fetched once, validated, written to
<PXD>/metadata/<gen>/ and published by atomically replacing <PXD>/metadata/CURRENT. An operation loads one
generation and uses it throughout (file list and checksums never come from different generations)."""
import hashlib, json, os, re, time, urllib.error, urllib.request

from .util import CorruptRecord, PxdError, UsageError, atomic_write, check_acc, fsync_dir, log, mkdirs, safe_name, write_json

from . import __version__

API = "https://www.ebi.ac.uk/pride/ws/archive/v3"        # pride.api, pride.ftp_base, pride.https_base
FTP_PREFIX = "ftp://ftp.pride.ebi.ac.uk/"
HTTPS_PREFIX = "https://ftp.pride.ebi.ac.uk/"
UA = f"pxd-mirror/{__version__} (stdlib urllib)"


def configure(api, ftp_base, https_base):
    """Set the PRIDE endpoints (process-wide; Mirror(config) calls this)."""
    global API, FTP_PREFIX, HTTPS_PREFIX
    API = api.rstrip("/")
    FTP_PREFIX = ftp_base if ftp_base.endswith("/") else ftp_base + "/"
    HTTPS_PREFIX = https_base if https_base.endswith("/") else https_base + "/"


def http_get(url, attempts=4, timeout=120, accept="application/json"):
    err = None
    for a in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                raise UsageError(f"{url}: HTTP {e.code} (not a public PRIDE data set?)")
            err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = e
        if a < attempts:
            time.sleep(min(120, 10 * 2 ** (a - 1)))
    raise PxdError(f"{url}: giving up after {attempts} attempts: {err}")


def parse_checksums(text):
    out = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) >= 3 and re.fullmatch(r"[0-9a-f]{32}", p[1]):
            out[p[0]] = (p[1], int(p[2]) if p[2].strip().isdigit() else None)
    return out


def urls_for(frec):
    ftp = asp = None
    for loc in frec.get("publicFileLocations", []):
        if loc.get("name") == "FTP Protocol":
            ftp = loc.get("value")
        elif loc.get("name") == "Aspera Protocol":
            asp = loc.get("value")
    out = {}
    if ftp and ftp.startswith(FTP_PREFIX):
        out["ftp"] = ftp
        out["https"] = HTTPS_PREFIX + ftp[len(FTP_PREFIX):]
        out["globus"] = "/" + ftp[len(FTP_PREFIX):]
    if asp:
        out["aspera"] = asp
    return out


def fetch(acc):
    """Fetch and validate one inventory in memory (no writes). Duplicate basenames are refused."""
    check_acc(acc)
    proj = json.loads(http_get(f"{API}/projects/{acc}"))
    files = json.loads(http_get(f"{API}/projects/{acc}/files/all"))
    cks = http_get(f"{API}/files/checksum/{acc}", accept="*/*").decode(errors="replace")
    names = [f.get("fileName") for f in files]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise PxdError(f"{acc}: duplicate file names in the PRIDE inventory: {sorted(dup)[:5]}")
    for n in names:
        safe_name(n)
    return {"project": proj, "files": files, "checksum": cks}


def snapshot(raw, acc):
    """The in-memory view of one generation."""
    files = {f["fileName"]: f for f in raw["files"]}
    return {"accession": acc, "gen": raw.get("gen"), "title": (raw.get("project") or {}).get("title"),
            "files": files, "checksums": parse_checksums(raw["checksum"])}


def publish(ds_dir, acc, raw):
    """Write a new generation and point CURRENT at it (atomic)."""
    blob = json.dumps(raw["files"], sort_keys=True).encode() + raw["checksum"].encode()
    gen = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + hashlib.sha256(blob).hexdigest()[:8]
    d = mkdirs(os.path.join(ds_dir, "metadata", gen))
    write_json(os.path.join(d, "project.json"), raw["project"])
    write_json(os.path.join(d, "files.json"), raw["files"])
    atomic_write(os.path.join(d, "checksum.tsv"), raw["checksum"])
    fsync_dir(d)
    atomic_write(os.path.join(ds_dir, "metadata", "CURRENT"), gen + "\n")
    raw = dict(raw, gen=gen)
    return snapshot(raw, acc)


def load(ds_dir, acc):
    """The current generation, or None if the data set has none. CorruptRecord if it is unreadable."""
    try:
        gen = open(os.path.join(ds_dir, "metadata", "CURRENT")).read().strip()
    except FileNotFoundError:
        return None
    safe_name(gen)
    d = os.path.join(ds_dir, "metadata", gen)
    try:
        files = json.load(open(os.path.join(d, "files.json")))
        cks = open(os.path.join(d, "checksum.tsv")).read()
        proj = json.load(open(os.path.join(d, "project.json")))
    except (OSError, ValueError) as e:
        raise CorruptRecord(f"inventory {d} unreadable: {e}")
    try:
        for f in files:
            safe_name(f["fileName"])
    except (TypeError, KeyError, UsageError) as e:
        raise CorruptRecord(f"inventory {d} has an invalid file entry: {e}")
    return snapshot({"project": proj, "files": files, "checksum": cks, "gen": gen}, acc)
