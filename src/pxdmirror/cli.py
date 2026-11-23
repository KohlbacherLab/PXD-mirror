"""pxd - a verified local mirror of public PRIDE / ProteomeXchange (PXD) data sets.

  query PXD [--files] [--json]        mirrored? exit 0 complete, 10 partial, 11 absent
  list [--json]                       data sets in the mirror
  get PXD (--files F..|--file-list L|--category RAW|--all) [--method auto|globus|ftp|https] [--dry-run]
  refresh PXD                         fetch a new PRIDE inventory generation
  delete PXD (--dataset | --files F.. [--derived-only] | --derived-only) [--status S] [--yes --confirm TOKEN]
  convert PXD --files F.. --to mzML|mzPeak [--tool NAME] [--threads N] [--stage DIR] [--allow-trfp2]
  path PXD NAME [--kind raw|mzML|mzPeak] [--recipe R]
  tools                               converters of the registry, with version and hashes
  migrate --plan SRC.. [--out F] | --apply PLAN [--no-symlink]
  import-legacy [--plan] DIR..        derived mzML of a legacy per-data-set archive
  stage PXD (--files F..|--all) --to DIR|HOST:PATH [--unzip]
  verify PXD | reindex | recover | init
  config [--json]                     effective configuration and where each value comes from
  doctor [--fix] [--json]             check (and repair what you own) group, modes, ACLs

Configuration: --config FILE or $PXD_CONFIG, ~/.config/pxd/config.toml, PXD_* variables (docs/CONFIGURATION.md).
Exit codes: 0 ok; 1 some files failed, were locked or not attempted (doctor: problems found); 2 usage or
configuration error; 3 operational error (including quota); query: 0 complete, 10 partial, 11 absent.
"""
import argparse, json, os, sqlite3, sys

from . import __version__
from . import convert as cv
from .config import Config
from .store import Mirror
from .util import CapacityError, PxdError, UsageError, replaceable


def names_of(a):
    n = list(a.files or [])
    if getattr(a, "file_list", None):
        with open(a.file_list) as fh:
            n += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    return n


def show(rows):
    bad = 0
    for r in rows:
        print("\t".join(map(str, r)))
        bad += str(r[1]).startswith(("failed", "locked", "not_attempted", "mismatch", "conflict"))
    return 1 if bad else 0


def parser():
    ap = argparse.ArgumentParser(prog="pxd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"pxd-mirror {__version__}")
    ap.add_argument("--root", help="mirror root (paths.root)")
    ap.add_argument("--config", help="configuration file (default $PXD_CONFIG)")
    ap.add_argument("--tools", help="converter registry (paths.tools)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("query"); s.add_argument("acc"); s.add_argument("--files", action="store_true"); s.add_argument("--json", action="store_true")
    s = sp.add_parser("list"); s.add_argument("--json", action="store_true")
    s = sp.add_parser("get"); s.add_argument("acc"); s.add_argument("--files", nargs="+"); s.add_argument("--file-list")
    s.add_argument("--category"); s.add_argument("--all", action="store_true"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--method", default="auto", choices=["auto", "globus", "ftp", "https"]); s.add_argument("--split", type=int)
    s.add_argument("--max-attempts", type=int); s.add_argument("--max-total", type=int)
    s.add_argument("--backoff", type=float); s.add_argument("--stop-after", type=int)
    s.add_argument("--globus-hours", type=float)
    s = sp.add_parser("refresh"); s.add_argument("acc")
    s = sp.add_parser("delete"); s.add_argument("acc"); s.add_argument("--files", nargs="+"); s.add_argument("--dataset", action="store_true")
    s.add_argument("--derived-only", action="store_true"); s.add_argument("--yes", action="store_true"); s.add_argument("--confirm")
    s.add_argument("--status", default="ok", help="conversion status to delete (default ok; index_invalid = legacy mzML)")
    s = sp.add_parser("convert"); s.add_argument("acc"); s.add_argument("--files", nargs="+"); s.add_argument("--file-list")
    s.add_argument("--to", required=True); s.add_argument("--tool"); s.add_argument("--threads", type=int, default=8)
    s.add_argument("--stage", help="staging directory (default paths.scratch)"); s.add_argument("--allow-trfp2", action="store_true")
    s = sp.add_parser("path"); s.add_argument("acc"); s.add_argument("name"); s.add_argument("--kind", default="raw"); s.add_argument("--recipe")
    sp.add_parser("tools")
    s = sp.add_parser("migrate"); s.add_argument("--plan", nargs="+", metavar="SRC"); s.add_argument("--out")
    s.add_argument("--apply", metavar="PLAN"); s.add_argument("--no-symlink", action="store_true")
    s = sp.add_parser("import-legacy", help="import the derived mzML of legacy per-data-set archives (after migrating their raw files)")
    s.add_argument("dirs", nargs="+", metavar="LEGACY_PXD_DIR"); s.add_argument("--plan", action="store_true"); s.add_argument("--no-symlink", action="store_true")
    s = sp.add_parser("stage"); s.add_argument("acc"); s.add_argument("--files", nargs="+"); s.add_argument("--file-list")
    s.add_argument("--all", action="store_true"); s.add_argument("--to", required=True); s.add_argument("--unzip", action="store_true")
    s = sp.add_parser("verify"); s.add_argument("acc")
    for c in ("reindex", "recover", "init"):
        sp.add_parser(c)
    s = sp.add_parser("config"); s.add_argument("--json", action="store_true")
    s = sp.add_parser("doctor"); s.add_argument("--fix", action="store_true", help="repair the entries you own")
    s.add_argument("--json", action="store_true")
    return ap


INSTALL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # <install>/src/pxdmirror/cli.py


def install_exposure():
    """[(path, reason)]: the package directory and every directory above it that a user other than root, the
    package's owner and the caller could rename or change (see util.replaceable)."""
    pkg = os.path.dirname(os.path.abspath(__file__))
    try:
        owner = os.stat(pkg).st_uid
    except OSError:
        return []
    out = replaceable(pkg, {owner})
    for f in sorted(os.listdir(pkg)):
        st = os.lstat(os.path.join(pkg, f))
        if st.st_uid not in (0, owner, os.getuid()) or st.st_mode & 0o022:
            out.append((os.path.join(pkg, f), "writable for group or other, or owned by another user"))
    return out


def show_config(cfg, as_json):
    creds = {
        "globus-cli login (per user)": os.path.expanduser("~/.globus/cli/storage.db"),
        "GCP config-paths (globus.gcp_config_dir)": os.path.join(cfg.get("globus", "gcp_config_dir"), "lta", "config-paths"),
    }
    if as_json:
        print(json.dumps({"version": __version__, "files": cfg.files, "warnings": cfg.warnings,
                          "shadowed": [{"key": k, "from": a, "by": b} for k, a, b in cfg.shadowed],
                          "installation": install_exposure(),
                          "values": {k: {"value": v, "source": src} for k, v, src in cfg.rows()},
                          "credential_files": {k: {"path": p, "exists": os.path.exists(p)} for k, p in creds.items()}},
                         indent=1))
        return
    print(f"pxd-mirror {__version__}; configuration files read (lowest precedence first): {', '.join(cfg.files) or 'none'}")
    for k, v, src in cfg.rows():
        print(f"  {k:<30} {json.dumps(v):<48} {src}")
    print("credential files (paths only; pxd never reads them):")
    for k, p in creds.items():
        print(f"  {k}: {p} ({'present' if os.path.exists(p) else 'absent'})")
    if not cfg.get("paths", "root"):
        print("warning: paths.root is not set; commands other than config and tools need it")
    for k, lo, hi in cfg.shadowed:
        print(f"note: {k} from {lo[5:]} is overridden by {hi}")
    for w in cfg.warnings:
        print(f"warning: {w}")
    for p, why in install_exposure():
        print(f"warning: installation {INSTALL}: {p} {why}: another user could replace the pxd you run (docs/PERMISSIONS.md)")


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        cfg = Config(cli={("paths", "root"): a.root, ("paths", "tools"): a.tools}, config_file=a.config)
        if a.cmd == "config":
            show_config(cfg, a.json)
            return 0
        for w in cfg.warnings:
            print(f"pxd: warning: {w}", file=sys.stderr)
        if a.cmd == "tools":
            for n, spec in cv.load_tools(cfg.get("paths", "tools")).items():
                try:
                    i = cv.identify(n, spec)
                    print(f"{n}\t{i['version']}\t{i['path']}\tsha256 {i['sha256'][:16]}\ttree_sha256 {i['tree_sha256']}"
                          f"\t{'REFUSED by default' if spec.get('refused_unless') else ''}")
                except PxdError as e:
                    print(f"{n}\tunavailable here: {e}")
            return 0
        m = Mirror(config=cfg)
        if a.cmd == "query":
            r = m.query(a.acc, a.files)
            if a.json:
                print(json.dumps(r, indent=1))
            else:
                print(f"{r['accession']}: {r['state']}" + (f", {r['files_ok']}/{r['files_pride']} files, {r['bytes_ok'] / 1e9:.2f} GB, "
                      f"{len(r['conversions'])} conversions, {r['path']}" if r["path"] else ""))
                for c in r.get("conversions", []):
                    print(f"  {c['format']}\t{c['status']}\t{c['path']}")
            return {"complete": 0, "partial": 10, "absent": 11}[r["state"]]
        if a.cmd == "list":
            rows = m.list()
            print(json.dumps(rows, indent=1) if a.json else "\n".join("\t".join(map(str, r.values())) for r in rows))
            return 0
        if a.cmd == "get":
            return show(m.get(a.acc, names_of(a), a.category, a.all, a.method, a.dry_run, a.split, a.max_attempts,
                              a.max_total, a.backoff, a.stop_after, a.globus_hours))
        if a.cmd == "refresh":
            r = m.refresh(a.acc)
            print(f"{a.acc}: generation {r['gen']}, {len(r['files'])} files")
            return 0
        if a.cmd == "delete":
            plan = m.delete_plan(a.acc, a.files, a.dataset, a.derived_only, a.status)
            for kind, key, rel in plan["items"]:
                print(f"{kind}\t{key}\t{m.root}/{a.acc}/{rel}")
            for g in plan["globus"]:
                print(f"globus-task\t{g['task']}\t{len(g['files'])} files in flight (deliveries discarded"
                      f"{', task cancelled' if g['own'] else ''})")
            print(f"{len(plan['items']) + len(plan['globus'])} items; confirmation token {plan['token']}")
            if not a.yes:
                print(f"nothing deleted; to delete exactly these: add --yes --confirm {plan['token']}")
                return 0
            gone = m.delete(a.acc, a.files, a.dataset, a.derived_only, a.confirm, a.status)
            print(f"deleted {len(gone)} items")
            return 0
        if a.cmd == "convert":
            return show(m.convert(a.acc, names_of(a), a.to, a.tool, a.threads, a.stage, a.allow_trfp2))
        if a.cmd == "path":
            p = m.path(a.acc, a.name, a.kind, a.recipe)
            print(p or "")
            return 0 if p else 11
        if a.cmd == "migrate":
            if bool(a.plan) == bool(a.apply):
                raise UsageError("migrate needs exactly one of --plan SRC.. or --apply PLAN")
            if a.plan:
                r = m.migrate_plan(a.plan, a.out)
                print(f"plan: {r['plan']}")
                for c, (n, b) in sorted(r["summary"].items()):
                    print(f"  {c:<14} {n:>7} files {b / 1e9:>10.2f} GB")
                print(f"  ({r['files_without_accession_dir']} files outside PXD-named folders were not considered)")
                return 0
            out = m.migrate_apply(a.apply, not a.no_symlink)
            for r in out:
                print("\t".join(map(str, r)))
            return 1 if any(r[2] in ("failed", "locked") for r in out) else 0
        if a.cmd == "import-legacy":
            out = [r for d in a.dirs for r in m.import_legacy(d, a.plan, not a.no_symlink)]
            for r in out:
                print("\t".join(map(str, r)))
            return 1 if any(r[2] in ("failed", "locked") for r in out) else 0
        if a.cmd == "stage":
            return show(m.stage(a.acc, names_of(a), a.all, a.to, a.unzip))
        if a.cmd == "verify":
            return show(m.verify(a.acc))
        if a.cmd == "reindex":
            r = m.reindex()
            print(json.dumps(r, indent=1))
            return 3 if r["problems"] else 0
        if a.cmd == "recover":
            out = m.recover()
            for o in out:
                print("\t".join(o))
            return 1 if any("NEEDS ATTENTION" in o[1] for o in out) else 0
        if a.cmd == "init":
            print(json.dumps(m.init()))
            return 0
        if a.cmd == "doctor":
            r = m.doctor(fix=a.fix)
            exp = install_exposure()       # a warning: members cannot fix it (needs root-only or sticky parents)
            r["checks"].append(("installation", "WARNING" if exp else "ok",
                                f"{exp[0][0]} {exp[0][1]} (+{len(exp) - 1} more): another user could replace pxd"
                                if exp else f"{INSTALL}: only root and its owner can change it"))
            if a.json:
                print(json.dumps(r, indent=1))
            else:
                for c in r["checks"]:
                    print("  ".join(map(str, c)))
                for kind, p in sorted(r["problems"].items()):
                    owners = ", ".join(f"{u}: {n}" for u, n in sorted(p["owners"].items()))
                    print(f"PROBLEM {kind}: {p['count']} entries (owners {owners}); e.g. {p['examples'][:3]}")
                if a.fix:
                    print(f"fixed {r['fixed']} entries you own; other users fix theirs with pxd doctor --fix")
                print("no problems" if not r["problems"] else "" if a.fix else "pxd doctor --fix repairs the entries you own")
            return 1 if r["problems"] or any(c[1] in ("PROBLEM", "MISMATCH") for c in r["checks"]) else 0
    except UsageError as e:
        print(f"pxd: {e}", file=sys.stderr)
        return 2
    except CapacityError as e:
        print(f"pxd: capacity: {e}", file=sys.stderr)
        return 3
    except (PxdError, OSError, sqlite3.Error) as e:
        print(f"pxd: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("pxd: interrupted (pxd recover finishes or reports half-done work)", file=sys.stderr)
        return 130


def run():
    sys.exit(main())


if __name__ == "__main__":
    run()
