#!/usr/bin/env python3
"""Unified CLI for the PlusCal Explorer build pipeline.

Subcommands:
    sweep   — Run TLC parameter sweep over all constant combinations
    build   — Build a self-contained HTML explorer from sweep traces
    deploy  — Deploy the built explorer to a configured target
    all     — sweep → build → deploy (full pipeline)

Usage:
    python build.py sweep   models/mesi_coherence/mesi_coherence.explorer.json
    python build.py build   models/mesi_coherence/mesi_coherence.explorer.json
    python build.py deploy  models/mesi_coherence/mesi_coherence.explorer.json
    python build.py all     models/mesi_coherence/mesi_coherence.explorer.json

The explorer.json config controls everything: constants, skip rules,
participants, channels, colors, branding, and deploy targets.
See PLAN.md for the full schema.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Add tools/ to path so sibling modules can be imported
sys.path.insert(0, str(SCRIPT_DIR))

from pcal_config import load_config, PcalConfig  # noqa: E402


# ═════════════════════════════════════════════════════════════════════
# Sweep
# ═════════════════════════════════════════════════════════════════════

def cmd_sweep(config: PcalConfig, config_path: Path, args: argparse.Namespace) -> bool:
    """Run TLC parameter sweep (delegates to tlc_sweep.py)."""
    import tlc_sweep

    model_dir = config_path.parent
    print(f"[sweep] Model: {config.module} ({model_dir})")

    # Set module-level config in tlc_sweep for backward compat
    tlc_sweep.CONFIG = config
    tlc_sweep.MODULE = config.module
    tlc_sweep.SCRIPT_DIR = model_dir

    combos = config.all_combos()
    skipped = config.expanded_excluded_set()
    active = [c for c in combos if c not in skipped]
    print(f"[sweep] {len(combos)} total combos, {len(skipped)} skipped, {len(active)} to run")

    # Ensure distrib/puml dir exists
    traces_dir = model_dir / "distrib" / "puml"
    traces_dir.mkdir(parents=True, exist_ok=True)

    # Clean stale traces from previous sweeps (different constant schemes)
    for old in traces_dir.glob("*.puml"):
        old.unlink()

    # Ensure tmp dir exists for TLA+ translation
    tmp_dir = model_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Translate PlusCal -> TLA+
    saved_cwd = os.getcwd()
    os.chdir(str(model_dir))
    try:
        print("[sweep] Translating PlusCal -> TLA+ ...")
        tlc_sweep.translate_pcal()
    finally:
        os.chdir(saved_cwd)

    # Run the sweep (cwd must be model_dir for run_tlc to find tmp/)
    os.chdir(str(model_dir))
    success_count = 0
    fail_count = 0
    all_results = {}

    try:
        for combo in active:
            tag = config.combo_tag(combo)
            combo_d = config.combo_dict(combo)
            print(f"  [{tag}] ", end="", flush=True)

            try:
                result = tlc_sweep.run_single_combo(config, combo_d, model_dir)
                if result is not None:
                    success_count += 1
                    all_results[tag] = result
                    n_msgs = len(result.get("trace", []))
                    print(f"PASS  ({n_msgs} msgs)")
                else:
                    fail_count += 1
                    print("FAIL")
            except Exception as e:
                fail_count += 1
                print(f"ERROR: {e}")
    finally:
        os.chdir(saved_cwd)

    # Deduplication and output (same as tlc_sweep.main)
    import json as _json
    print(f"\n[sweep] Deduplicating traces ...")
    canonical = {}
    aliases = {}
    for tag, data in all_results.items():
        sig = _json.dumps(data["steps"], sort_keys=True, separators=(",", ":"))
        if sig not in canonical:
            canonical[sig] = tag
        aliases[tag] = canonical[sig]

    unique_tags = set(canonical.values())
    print(f"[sweep] {len(all_results)} passing -> {len(unique_tags)} unique traces")

    for tag in unique_tags:
        data = all_results[tag]
        puml_text = tlc_sweep.trace_data_to_puml(data)
        (traces_dir / f"{tag}.puml").write_text(puml_text, encoding="utf-8")

    (traces_dir / "_aliases.json").write_text(
        _json.dumps(aliases, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\n[sweep] Done: {success_count} pass, {fail_count} fail")
    return fail_count == 0


# ═════════════════════════════════════════════════════════════════════
# Build
# ═════════════════════════════════════════════════════════════════════

def cmd_build(config: PcalConfig, config_path: Path, args: argparse.Namespace) -> bool:
    """Build self-contained HTML explorer (delegates to build_explorer.py)."""
    import build_explorer

    model_dir = config_path.parent
    print(f"[build] Model: {config.module} ({model_dir})")

    distrib_dir = model_dir / "distrib"
    traces_dir = distrib_dir / "puml"

    if not traces_dir.is_dir() or not any(traces_dir.glob("*.puml")):
        print("[build] WARNING: No traces found. Dropdowns will show invalid/skipped status only.")
        traces_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Set paths for build_explorer
        build_explorer.SCRIPT_DIR = model_dir
        build_explorer.DISTRIB_DIR = distrib_dir
        build_explorer.TRACES_DIR = traces_dir
        build_explorer.OUTPUT_HTML = distrib_dir / "index.html"

        build_explorer.main_build(config)
        print(f"[build] Output: {distrib_dir / 'index.html'}")
        return True
    except Exception as e:
        print(f"[build] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


# ═════════════════════════════════════════════════════════════════════
# Deploy
# ═════════════════════════════════════════════════════════════════════

def cmd_deploy(config: PcalConfig, config_path: Path, args: argparse.Namespace) -> bool:
    """Deploy the built explorer to a configured target."""
    model_dir = config_path.parent
    distrib_dir = model_dir / "distrib"

    if not config.deploy:
        print("[deploy] No 'deploy' section in config — nothing to do.")
        return True

    deploy_cfg = config.deploy
    target = deploy_cfg.get("target", "local")
    dry_run = args.dry_run if hasattr(args, "dry_run") else False
    archive = args.archive if hasattr(args, "archive") else False

    print(f"[deploy] Target: {target}" + (" (dry-run)" if dry_run else ""))

    if target == "local":
        return _deploy_local(config, distrib_dir, deploy_cfg, dry_run)
    elif target == "webdav":
        return _deploy_webdav(config, distrib_dir, deploy_cfg, dry_run, archive)
    else:
        print(f"[deploy] ERROR: Unknown target '{target}'")
        return False


def _deploy_local(config: PcalConfig, distrib_dir: Path, deploy_cfg: dict,
                  dry_run: bool) -> bool:
    """Deploy to a local directory."""
    local_path = Path(deploy_cfg.get("localPath", "./deploy_output"))
    if not local_path.is_absolute():
        local_path = distrib_dir.parent / local_path

    manifest = deploy_cfg.get("fileManifest", [])
    subdirs = deploy_cfg.get("subdirs", [])

    if dry_run:
        print(f"  Would copy to: {local_path}")
        for f in manifest:
            src = distrib_dir / f
            print(f"  {'EXISTS' if src.exists() else 'MISSING'}: {f}")
        for d in subdirs:
            src = distrib_dir / d
            count = len(list(src.rglob("*"))) if src.is_dir() else 0
            print(f"  DIR {d}/ ({count} files)")
        return True

    # Liveness check: try to create/access the target directory
    try:
        local_path.mkdir(parents=True, exist_ok=True)
        # Verify we can write by testing parent is writable
        _test_file = local_path / ".deploy_test"
        _test_file.write_text("ok", encoding="utf-8")
        _test_file.unlink()
    except PermissionError:
        print(f"[deploy] ERROR: Permission denied: {local_path}")
        print("  Check directory permissions and try again.")
        return False
    except OSError as e:
        print(f"[deploy] ERROR: Cannot access target: {local_path}")
        print(f"  {e}")
        return False

    # Copy manifest files
    copied = 0
    for f in manifest:
        src = distrib_dir / f
        if src.exists():
            shutil.copy2(src, local_path / f)
            copied += 1

    # Copy subdirectories
    for d in subdirs:
        src = distrib_dir / d
        dst = local_path / d
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            copied += len(list(dst.rglob("*")))

    print(f"  Deployed {copied} items to {local_path}")
    return True


def _unc_to_https(webdav_path: str) -> str | None:
    """Convert a Windows WebDAV UNC path to an HTTPS URL.

    //host@SSL/DavWWWRoot/path  →  https://host/path
    \\\\host@SSL\\DavWWWRoot\\path  →  https://host/path
    """
    # Normalise separators
    p = webdav_path.replace("\\", "/").strip("/")
    # Expected: host@SSL/DavWWWRoot/rest...
    parts = p.split("/")
    if len(parts) < 2:
        return None
    host_part = parts[0]           # e.g. "docs.intel.com@SSL"
    host = host_part.split("@")[0] # strip @SSL
    # Skip "DavWWWRoot" if present
    rest_parts = parts[1:]
    if rest_parts and rest_parts[0].lower() == "davwwwroot":
        rest_parts = rest_parts[1:]
    return f"https://{host}/{'/'.join(rest_parts)}"


def _curl_put(base_url: str, local_file: Path, remote_name: str) -> bool:
    """Upload a single file via curl HTTP PUT with negotiate auth."""
    url = f"{base_url}/{remote_name}"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--negotiate", "-u", ":", "-T", str(local_file), url],
        capture_output=True, text=True, timeout=120,
    )
    code = result.stdout.strip()
    return code in ("200", "201", "204")


def _curl_mkcol(base_url: str, dirname: str) -> bool:
    """Create a directory on WebDAV via MKCOL.  Returns True on success or 405 (already exists)."""
    url = f"{base_url}/{dirname}/"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--negotiate", "-u", ":", "-X", "MKCOL", url],
        capture_output=True, text=True, timeout=30,
    )
    code = result.stdout.strip()
    return code in ("200", "201", "204", "301", "405")


def _curl_delete(base_url: str, name: str) -> bool:
    """Delete a file or directory on WebDAV."""
    url = f"{base_url}/{name}"
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--negotiate", "-u", ":", "-X", "DELETE", url],
        capture_output=True, text=True, timeout=60,
    )
    code = result.stdout.strip()
    return code in ("200", "204", "404")


def _deploy_webdav(config: PcalConfig, distrib_dir: Path, deploy_cfg: dict,
                   dry_run: bool, archive: bool) -> bool:
    """Deploy to a WebDAV share via HTTP (curl) — avoids Windows WebClient locking."""
    webdav_path = deploy_cfg.get("webdavPath", "")
    if not webdav_path:
        print("[deploy] ERROR: No webdavPath configured")
        return False

    base_url = _unc_to_https(webdav_path)
    if not base_url:
        print(f"[deploy] ERROR: Cannot parse webdavPath: {webdav_path}")
        return False

    # Liveness check — test with a curl PUT + DELETE
    if not dry_run:
        try:
            test_result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--negotiate", "-u", ":", "--max-time", "10",
                 "-T", "-", f"{base_url}/.deploy_test"],
                input="ok", capture_output=True, text=True, timeout=15,
            )
            code = test_result.stdout.strip()
            if code not in ("200", "201", "204"):
                print(f"[deploy] ERROR: Write test failed (HTTP {code}): {base_url}")
                print(f"  Are you on VPN?")
                return False
            # Clean up test file
            _curl_delete(base_url, ".deploy_test")
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[deploy] ERROR: curl not available or target unreachable: {e}")
            return False

        print(f"  Target verified: {base_url}")

    manifest = deploy_cfg.get("fileManifest", [])
    subdirs = deploy_cfg.get("subdirs", [])

    if dry_run:
        print(f"  Would deploy to: {base_url}")
        for f in manifest:
            print(f"    {f}")
        for d in subdirs:
            print(f"    {d}/")
        return True

    # Archive existing files (via COPY on server, or skip if too complex)
    if archive:
        archive_prefix = deploy_cfg.get("archivePrefix", "v1")
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"{archive_prefix}_{timestamp}"
        print(f"  Archive: {base_url}/{archive_name}")
        _curl_mkcol(base_url, archive_name)
        for f in manifest:
            # Server-side COPY: source → archive
            src_url = f"{base_url}/{f}"
            dst_url = f"{base_url}/{archive_name}/{f}"
            subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "--negotiate", "-u", ":",
                 "-X", "COPY", "-H", f"Destination: {dst_url}", src_url],
                capture_output=True, text=True, timeout=30,
            )
        for d in subdirs:
            # Directory COPY
            src_url = f"{base_url}/{d}/"
            dst_url = f"{base_url}/{archive_name}/{d}/"
            subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "--negotiate", "-u", ":",
                 "-X", "COPY", "-H", f"Destination: {dst_url}",
                 "-H", "Depth: infinity", src_url],
                capture_output=True, text=True, timeout=60,
            )

    # Deploy new files
    copied = 0
    failed = []
    for f in manifest:
        src = distrib_dir / f
        if src.exists():
            if _curl_put(base_url, src, f):
                copied += 1
                print(f"    {f}")
            else:
                failed.append(f)
                print(f"    {f}  [FAILED]")

    for d in subdirs:
        src_dir = distrib_dir / d
        if not src_dir.is_dir():
            continue
        # Delete existing remote directory (clears stale SharePoint permissions),
        # then recreate fresh before uploading.
        _curl_delete(base_url, f"{d}/")
        if not _curl_mkcol(base_url, d):
            print(f"    {d}/  [MKCOL FAILED — skipping]")
            continue
        n = 0
        for src_file in sorted(src_dir.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(distrib_dir)
            remote_path = rel.as_posix()
            # Ensure parent dirs exist
            parent = rel.parent.as_posix()
            if parent != d:
                _curl_mkcol(base_url, parent)
            if _curl_put(base_url, src_file, remote_path):
                n += 1
            else:
                failed.append(remote_path)
        copied += n
        print(f"    {d}/ ({n} files)")

    if failed:
        print(f"\n[deploy] WARNING: {len(failed)} files failed to upload:")
        for f in failed[:10]:
            print(f"    {f}")
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")

    print(f"  Deployed {copied} items to {base_url}")
    return len(failed) == 0


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="PlusCal Explorer build pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # sweep
    p_sweep = sub.add_parser("sweep", help="Run TLC parameter sweep")
    p_sweep.add_argument("config", help="Path to *.explorer.json")

    # build
    p_build = sub.add_parser("build", help="Build HTML explorer")
    p_build.add_argument("config", help="Path to *.explorer.json")

    # deploy
    p_deploy = sub.add_parser("deploy", help="Deploy built explorer")
    p_deploy.add_argument("config", help="Path to *.explorer.json")
    p_deploy.add_argument("--dry-run", action="store_true", help="Show what would be deployed")
    p_deploy.add_argument("--archive", action="store_true", help="Archive existing before deploy")

    # all
    p_all = sub.add_parser("all", help="sweep → build → deploy")
    p_all.add_argument("config", help="Path to *.explorer.json")
    p_all.add_argument("--dry-run", action="store_true", help="Dry-run deploy step")
    p_all.add_argument("--archive", action="store_true", help="Archive existing before deploy")

    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    print(f"Loaded config: {config.module} ({config_path})")

    commands = {
        "sweep": cmd_sweep,
        "build": cmd_build,
        "deploy": cmd_deploy,
    }

    if args.command == "all":
        for step in ["sweep", "build", "deploy"]:
            print(f"\n{'═' * 60}")
            print(f"  {step.upper()}")
            print(f"{'═' * 60}")
            ok = commands[step](config, config_path, args)
            if not ok and step != "deploy":
                print(f"\n[all] {step} failed — stopping.")
                sys.exit(1)
    else:
        ok = commands[args.command](config, config_path, args)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
