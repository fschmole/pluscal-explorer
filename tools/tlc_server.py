#!/usr/bin/env python3
"""TLC Server — local HTTP API for PlusCal model exploration.

Provides REST endpoints for the browser to fetch TLC-verified traces
on demand, enabling live parameter changes without rebuilding.

Model-specific knowledge comes from `*.explorer.json` config files
(see pcal_config.py and PLAN.md for the schema).

Endpoints:
    GET  /api/health        → {"status":"ok", "tlc_version":"...", "model":"..."}
    GET  /api/params        → {"constants":{name:[vals],...}, "skip":[...], "participants":[...]}
    POST /api/trace         → body: {name:value,...}  resp: {parameters, participants, trace, steps, channelStyles, puml_text, puml_svg, ...}
    POST /api/trace-custom  → body: {pcal_source, ...constants...}  resp: same + custom:true
    POST /api/stategraph    → body: {pcal_source}  resp: {dot:"...", elapsed_ms}

Security:
    - Binds to 127.0.0.1 only (not 0.0.0.0)
    - CORS headers for localhost origins

Usage:
    python tlc_server.py
    python tlc_server.py --port 18080
    python tlc_server.py --model mesi_coherence
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Import reusable functions from tlc_sweep.py ───────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tlc_sweep  # noqa: E402

DEFAULT_PORT = 18080
BIND_HOST = "127.0.0.1"
PLANTUML_JAR = Path(__file__).resolve().parent / "plantuml.jar"

# Reuse JVM flags and platform guards from tlc_sweep (single source of truth)
_JVM_FAST = tlc_sweep._JVM_FAST
_NOWIN    = tlc_sweep._NOWIN

# Cache pcal.trans output keyed by SHA-256 of source text.
# Avoids re-translating identical source across repeated Run TLC clicks.
_pcal_cache: dict[str, str] = {}   # {source_hash: translated_tla_text}


# ═══════════════════════════════════════════════════════════════════════
# TLC version detection
# ═══════════════════════════════════════════════════════════════════════

def detect_tlc_version() -> str:
    """Try to get TLC version string."""
    try:
        result = subprocess.run(
            [tlc_sweep.JAVA, "-cp", tlc_sweep.TLA2TOOLS, "tlc2.TLC", "-h"],
            capture_output=True, text=True, timeout=10, **_NOWIN,
        )
        combined = result.stdout + result.stderr
        for line in combined.splitlines():
            if "Version" in line or "version" in line:
                return line.strip()
        return "unknown"
    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════
# PlantUML SVG rendering
# ═══════════════════════════════════════════════════════════════════════

def _clean_svg(content: str) -> str:
    """Strip XML/DOCTYPE for safe innerHTML embedding."""
    import re as _re
    content = _re.sub(r"<\?xml[^?]*\?>\s*", "", content)
    content = _re.sub(r"<!DOCTYPE[^>]*>\s*", "", content)
    return content.strip()


def _render_puml_svg(puml_text: str) -> str:
    """Render PlantUML text to SVG via plantuml.jar -pipe.

    Returns cleaned SVG string, or empty string on failure.
    """
    if not PLANTUML_JAR.exists():
        return ""
    try:
        result = subprocess.run(
            [tlc_sweep.JAVA, "-jar", str(PLANTUML_JAR),
             "-tsvg", "-pipe", "-nometadata"],
            input=puml_text, capture_output=True, text=True,
            timeout=30, **_NOWIN,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _clean_svg(result.stdout)
        return ""
    except Exception:
        return ""


def _build_trace_result(combo_d: dict, all_traces, elapsed_ms: int) -> dict:
    """Build a complete response dict with PlantUML text and SVG.

    Parameters
    ----------
    combo_d : dict
        {constant_name: value} for the parameter combo.
    all_traces : list[list[dict]]
        All terminal traces from TLC (for concurrent-step detection).
    elapsed_ms : int
        TLC execution time.

    Returns
    -------
    dict
        Complete response with trace, steps, channelStyles,
        puml_text, and puml_svg.
    """
    cfg = tlc_sweep.CONFIG
    steps, canonical_trace = tlc_sweep.compute_steps(all_traces)
    channels = cfg.resolve_channels()
    channel_styles = tlc_sweep._channel_styles(channels) if channels else {}

    data = {
        "parameters":    combo_d,
        "participants":  cfg.participants,
        "trace":         canonical_trace,
        "steps":         steps,
        "channelStyles": channel_styles,
    }
    puml_text = tlc_sweep.trace_data_to_puml(data)
    puml_svg  = _render_puml_svg(puml_text)

    data["puml_text"]   = puml_text
    data["puml_svg"]    = puml_svg
    data["elapsed_ms"]  = elapsed_ms
    return data


# ═══════════════════════════════════════════════════════════════════════
# Trace cache — LRU keyed by parameter tuple
# ═══════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=128)
def get_trace_cached(combo_key: tuple[tuple[str, str], ...]):
    """Run TLC for one combo and return a complete result dict or raise.

    combo_key is a tuple of (name, value) pairs (hashable for LRU).
    The result includes trace, steps, channelStyles, puml_text, and puml_svg.
    """
    combo_d = dict(combo_key)
    cfg_path = Path("tmp/_tlc_server_temp.cfg")
    try:
        tlc_sweep.write_cfg(combo_d, cfg_path)
        t0 = time.monotonic()
        success, all_traces, output = tlc_sweep.run_tlc(cfg_path)
        elapsed = round((time.monotonic() - t0) * 1000)

        if not success:
            lines = output.strip().splitlines()
            err_context = "\n".join(lines[-15:]) if len(lines) > 15 else output
            raise RuntimeError(f"TLC verification failed:\n{err_context}")

        if not all_traces:
            raise RuntimeError("TLC passed but no trace could be extracted from dump")

        result = _build_trace_result(combo_d, all_traces, elapsed)
        result["cached"] = True
        return result
    finally:
        cfg_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# HTTP Request Handler
# ═══════════════════════════════════════════════════════════════════════

class TLCHandler(BaseHTTPRequestHandler):
    """Handle API requests for PlusCal model exploration."""

    server_version = "TLCServer/1.0"
    tlc_version = "unknown"

    # Suppress default request logging (we do our own)
    def log_message(self, fmt, *args):
        pass

    def _cors_headers(self):
        """Set CORS headers for localhost origins."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, code: int, data: dict):
        """Send a JSON response."""
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Client disconnected (e.g. browser AbortController timeout)
            pass

    def _read_json_body(self) -> dict:
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    # ── OPTIONS (CORS preflight) ──────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── GET routes ────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/api/health":
            self._handle_health()
        elif self.path == "/api/params":
            self._handle_params()
        else:
            self._json_response(404, {"error": "not found"})

    # ── POST routes ───────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/api/trace":
            self._handle_trace()
        elif self.path == "/api/trace-custom":
            self._handle_trace_custom()
        elif self.path == "/api/stategraph":
            self._handle_stategraph()
        else:
            self._json_response(404, {"error": "not found"})

    # ── /api/health ───────────────────────────────────────────────────
    def _handle_health(self):
        self._json_response(200, {
            "status": "ok",
            "tlc_version": self.tlc_version,
            "model": tlc_sweep.MODULE,
            "java": tlc_sweep.JAVA,
        })
        print(f"  GET /api/health → 200")

    # ── /api/params ───────────────────────────────────────────────────
    def _handle_params(self):
        cfg = tlc_sweep.CONFIG
        invalid_list = [dict(zip(cfg.constant_names, combo))
                        for combo in sorted(cfg.expanded_invalid_set())]
        skip_list = [dict(zip(cfg.constant_names, combo))
                     for combo in sorted(cfg.expanded_skip_set())]
        self._json_response(200, {
            "constants": cfg.constants,
            "participants": cfg.participants,
            "invalid": invalid_list,
            "skip": skip_list,
            "title": cfg.title,
        })
        print(f"  GET /api/params → 200  ({len(invalid_list)} invalid, {len(skip_list)} skip combos)")

    # ── /api/trace ────────────────────────────────────────────────────
    def _handle_trace(self):
        try:
            body = self._read_json_body()
        except Exception as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        cfg = tlc_sweep.CONFIG
        # Extract constant values from request body using config constant names
        combo_d: dict[str, str] = {}
        missing = []
        for name in cfg.constant_names:
            val = body.get(name, "")
            if not val and val != 0 and val is not False:
                missing.append(name)
            else:
                combo_d[name] = val

        if missing:
            self._json_response(400, {
                "error": f"missing required fields: {', '.join(missing)}",
                "expected": cfg.constant_names,
            })
            return

        # Coerce string values from JSON to native types (int/bool/str)
        combo_d = cfg.coerce_combo_dict(combo_d)
        combo_tuple = tuple(combo_d[k] for k in cfg.constant_names)
        tag = ".".join(str(v) for v in combo_tuple)

        # Check if this is an invalid or skipped combo
        if cfg.is_invalid(combo_tuple):
            self._json_response(422, {
                "error": f"invalid combo: {tag}",
                "reason": "This parameter combination is not physically possible"
            })
            print(f"  POST /api/trace {tag} → 422 (invalid)")
            return
        # NOTE: skipped combos are *not* blocked here — the server should
        # happily generate traces for them on-demand (they are only excluded
        # from batch sweeps, not invalid).

        # Make combo hashable for LRU cache
        combo_key = tuple(sorted(combo_d.items()))

        try:
            result = get_trace_cached(combo_key)
            self._json_response(200, result)
            n = len(result.get("trace", []))
            ms = result.get("elapsed_ms", 0)
            svg = "svg" if result.get("puml_svg") else "puml"
            print(f"  POST /api/trace {tag} → 200  ({n} msgs, {ms}ms, {svg})")

        except RuntimeError as e:
            self._json_response(500, {
                "error": str(e),
                "parameters": combo_d,
            })
            print(f"  POST /api/trace {tag} → 500  (TLC error)")

        except Exception as e:
            self._json_response(500, {"error": f"unexpected error: {e}"})
            print(f"  POST /api/trace {tag} → 500  ({e})")

    # ── /api/trace-custom ─────────────────────────────────
    def _handle_trace_custom(self):
        try:
            body = self._read_json_body()
        except Exception as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        cfg = tlc_sweep.CONFIG
        tla_source = body.get("pcal_source", "") or body.get("tla_source", "")

        if not tla_source:
            self._json_response(400, {"error": "missing required field: pcal_source"})
            return

        # Extract constant values from request body
        combo_d: dict[str, str] = {}
        missing = []
        for name in cfg.constant_names:
            val = body.get(name, "")
            if not val and val != 0 and val is not False:
                missing.append(name)
            else:
                combo_d[name] = val

        if missing:
            self._json_response(400, {
                "error": f"missing required fields: {', '.join(missing)}",
                "expected": cfg.constant_names,
            })
            return

        # Coerce string values from JSON to native types (int/bool/str)
        combo_d = cfg.coerce_combo_dict(combo_d)
        tag = "custom:" + ".".join(str(combo_d[k]) for k in cfg.constant_names)

        # Work in a temp directory so we don't disturb the main model files
        tmpdir = None
        tla2tools_abs = tlc_sweep.TLA2TOOLS

        try:
            tmpdir = tempfile.mkdtemp(prefix="tlc_custom_")
            tmp = Path(tmpdir)

            wrapped = tlc_sweep._wrap_pcal_for_trans(tla_source)
            tla_file = tmp / f"{cfg.module}.tla"

            # Check translation cache — skip pcal.trans JVM if source unchanged
            src_hash = hashlib.sha256(tla_source.encode()).hexdigest()
            cached_tla = _pcal_cache.get(src_hash)

            if cached_tla:
                tla_file.write_text(cached_tla, encoding="utf-8")
            else:
                tla_file.write_text(wrapped, encoding="utf-8")

                pcal_cmd = [
                    tlc_sweep.JAVA, *_JVM_FAST,
                    "-cp", tla2tools_abs,
                    "pcal.trans", "-nocfg", str(tla_file),
                ]
                pcal_result = subprocess.run(
                    pcal_cmd, capture_output=True, text=True, timeout=30, **_NOWIN,
                )
                pcal_combined = pcal_result.stdout + pcal_result.stderr
                if pcal_result.returncode != 0:
                    self._json_response(422, {
                        "error": "PlusCal translation failed",
                        "details": pcal_combined.strip(),
                        "stage": "pcal.trans",
                    })
                    print(f"  POST /api/trace-custom {tag} → 422 (pcal error)")
                    return

                if "Unrecoverable error" in pcal_combined:
                    self._json_response(422, {
                        "error": "PlusCal translation error",
                        "details": pcal_combined.strip(),
                        "stage": "pcal.trans",
                    })
                    print(f"  POST /api/trace-custom {tag} → 422 (pcal unrecoverable)")
                    return

                _pcal_cache[src_hash] = tla_file.read_text(encoding="utf-8")

            # Write .cfg file using config
            cfg_file = tmp / f"{cfg.module}.cfg"
            cfg.write_cfg(combo_d, cfg_file)

            # Run TLC on the translated file
            dump_stem = tmp / "tlc_dump"
            dump_file = tmp / "tlc_dump.dump"
            tlc_cmd = [
                tlc_sweep.JAVA, *_JVM_FAST, "-XX:+UseParallelGC",
                "-cp", tla2tools_abs,
                "tlc2.TLC", cfg.module,
                "-config", str(cfg_file),
                "-deadlock",
                "-workers", "auto",
                "-dump", str(dump_stem),
            ]
            t0 = time.monotonic()
            tlc_result = subprocess.run(
                tlc_cmd, capture_output=True, text=True, timeout=120,
                cwd=tmpdir, **_NOWIN,
            )
            elapsed = round((time.monotonic() - t0) * 1000)
            tlc_combined = tlc_result.stdout + tlc_result.stderr
            success = "Model checking completed. No error has been found." in tlc_combined

            if not success:
                lines = tlc_combined.strip().splitlines()
                err_context = "\n".join(lines[-25:]) if len(lines) > 25 else tlc_combined
                self._json_response(422, {
                    "error": "TLC model checking failed",
                    "details": err_context.strip(),
                    "stage": "tlc2.TLC",
                    "elapsed_ms": elapsed,
                })
                print(f"  POST /api/trace-custom {tag} → 422 (TLC error, {elapsed}ms)")
                return

            # Parse trace from dump
            trace = None
            if dump_file.exists():
                dump_text = dump_file.read_text(encoding="utf-8", errors="replace")
                trace = tlc_sweep.parse_trace_from_dump(dump_text)
            if trace is None:
                trace = tlc_sweep.parse_trace(tlc_combined)

            if trace is None:
                self._json_response(500, {
                    "error": "TLC passed but no trace could be extracted",
                    "stage": "parse",
                    "elapsed_ms": elapsed,
                })
                print(f"  POST /api/trace-custom {tag} → 500 (no trace, {elapsed}ms)")
                return

            result = _build_trace_result(combo_d, [trace], elapsed)
            result["custom"] = True
            self._json_response(200, result)
            svg = "svg" if result.get("puml_svg") else "puml"
            print(f"  POST /api/trace-custom {tag} → 200  ({len(trace)} msgs, {elapsed}ms, {svg})")

        except subprocess.TimeoutExpired:
            self._json_response(504, {
                "error": "TLC timed out (120s limit)",
                "stage": "timeout",
            })
            print(f"  POST /api/trace-custom {tag} → 504 (timeout)")

        except Exception as e:
            self._json_response(500, {"error": f"unexpected error: {e}"})
            print(f"  POST /api/trace-custom {tag} → 500  ({e})")

        finally:
            # Clean up temp directory
            if tmpdir:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    # ── /api/stategraph ──────────────────────────────────────────────
    def _handle_stategraph(self):
        """Run TLC with -dump dot to produce a Graphviz DOT state graph."""
        try:
            body = self._read_json_body()
        except Exception as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        tla_source = body.get("pcal_source", "") or body.get("tla_source", "")
        if not tla_source:
            self._json_response(400, {"error": "missing required field: pcal_source"})
            return

        cfg = tlc_sweep.CONFIG
        tmpdir = None
        tla2tools_abs = tlc_sweep.TLA2TOOLS

        try:
            tmpdir = tempfile.mkdtemp(prefix="tlc_dotgraph_")
            tmp = Path(tmpdir)

            # Translate PlusCal → TLA+
            wrapped = tlc_sweep._wrap_pcal_for_trans(tla_source)
            tla_file = tmp / f"{cfg.module}.tla"

            src_hash = hashlib.sha256(tla_source.encode()).hexdigest()
            cached_tla = _pcal_cache.get(src_hash)

            if cached_tla:
                tla_file.write_text(cached_tla, encoding="utf-8")
            else:
                tla_file.write_text(wrapped, encoding="utf-8")
                pcal_cmd = [
                    tlc_sweep.JAVA, *_JVM_FAST,
                    "-cp", tla2tools_abs,
                    "pcal.trans", "-nocfg", str(tla_file),
                ]
                pcal_result = subprocess.run(
                    pcal_cmd, capture_output=True, text=True, timeout=30, **_NOWIN,
                )
                if pcal_result.returncode != 0 or "Unrecoverable error" in (pcal_result.stdout + pcal_result.stderr):
                    self._json_response(422, {
                        "error": "PlusCal translation failed",
                        "details": (pcal_result.stdout + pcal_result.stderr).strip(),
                        "stage": "pcal.trans",
                    })
                    print(f"  POST /api/stategraph → 422 (pcal error)")
                    return
                _pcal_cache[src_hash] = tla_file.read_text(encoding="utf-8")

            # Write a minimal .cfg for state-graph exploration using first combo
            cfg_file = tmp / f"{cfg.module}.cfg"
            cfg.write_cfg(cfg.first_combo(), cfg_file)

            # Run TLC with -dump dot
            dot_stem = tmp / "states"
            dot_file = tmp / "states.dot"
            tlc_cmd = [
                tlc_sweep.JAVA, *_JVM_FAST, "-XX:+UseParallelGC",
                "-cp", tla2tools_abs,
                "tlc2.TLC", cfg.module,
                "-config", str(cfg_file),
                "-deadlock",
                "-workers", "1",
                "-dump", "dot,actionlabels", str(dot_stem.resolve()),
            ]
            t0 = time.monotonic()
            tlc_result = subprocess.run(
                tlc_cmd, capture_output=True, text=True, timeout=120,
                cwd=tmpdir, **_NOWIN,
            )
            elapsed = round((time.monotonic() - t0) * 1000)

            if dot_file.exists():
                dot_text = dot_file.read_text(encoding="utf-8", errors="replace")
                self._json_response(200, {
                    "dot": dot_text,
                    "elapsed_ms": elapsed,
                })
                print(f"  POST /api/stategraph → 200 ({len(dot_text)} chars, {elapsed}ms)")
            else:
                # TLC may not have produced a .dot file — return what we can
                tlc_combined = tlc_result.stdout + tlc_result.stderr
                self._json_response(422, {
                    "error": "TLC did not produce a DOT state graph",
                    "details": tlc_combined.strip()[-2000:],
                    "stage": "tlc2.TLC",
                    "elapsed_ms": elapsed,
                })
                print(f"  POST /api/stategraph → 422 (no .dot, {elapsed}ms)")

        except subprocess.TimeoutExpired:
            self._json_response(504, {"error": "TLC timed out (120s limit)", "stage": "timeout"})
            print(f"  POST /api/stategraph → 504 (timeout)")

        except Exception as e:
            self._json_response(500, {"error": f"unexpected error: {e}"})
            print(f"  POST /api/stategraph → 500 ({e})")

        finally:
            if tmpdir:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="TLC Server for PlusCal model exploration",
        epilog="Examples:\n"
               "  python tlc_server.py models/mesi_coherence/mesi_coherence.explorer.json\n"
               "  python tlc_server.py --model mesi_coherence\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("config", nargs="?", default=None,
                   help="Path to *.explorer.json config file")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    p.add_argument("--host", default=BIND_HOST, help=f"Bind host (default: {BIND_HOST})")
    p.add_argument("--model", "-m", default=None,
                   help="Model name (e.g. mesi_coherence). "
                        "Auto-detected from *.explorer.json if omitted.")
    return p.parse_args()


def _resolve_config(args) -> tuple:
    """Resolve config path and model directory from CLI args.

    Accepts either a positional path to *.explorer.json, or --model name
    (searched relative to the models/ sibling directory of tools/).
    Returns (PcalConfig, model_dir).
    """
    from pcal_config import load_config, find_config
    script_dir = Path(__file__).resolve().parent

    if args.config:
        # Positional path to .explorer.json
        config_path = Path(args.config).resolve()
        if config_path.is_dir():
            cfg = find_config(config_path)
            return cfg, config_path
        if not config_path.exists():
            sys.exit(f"ERROR: config file not found: {config_path}")
        cfg = load_config(config_path)
        return cfg, config_path.parent

    if args.model:
        # --model name or path
        model_arg = Path(args.model)
        if model_arg.suffix == ".json" and model_arg.exists():
            cfg = load_config(model_arg.resolve())
            return cfg, model_arg.resolve().parent
        # Try models/<name>/ relative to repo root
        models_dir = script_dir.parent / "models" / args.model
        if models_dir.is_dir():
            cfg = find_config(models_dir)
            return cfg, models_dir
        # Try as a module name in SCRIPT_DIR (legacy)
        cfg = find_config(script_dir, module=args.model)
        return cfg, script_dir

    # No arg — try models/ subdirectories, then tools/ itself
    models_root = script_dir.parent / "models"
    if models_root.is_dir():
        explorer_jsons = sorted(models_root.glob("*/*.explorer.json"))
        if len(explorer_jsons) == 1:
            cfg = load_config(explorer_jsons[0])
            return cfg, explorer_jsons[0].parent
        if len(explorer_jsons) > 1:
            names = [p.parent.name for p in explorer_jsons]
            sys.exit(f"ERROR: multiple models found: {', '.join(names)}\n"
                     f"Specify one with: python tlc_server.py <path-to-config>")
    cfg = find_config(script_dir)
    return cfg, script_dir


def main():
    args = parse_args()

    # Resolve config and model directory
    cfg, model_dir = _resolve_config(args)

    # Point tlc_sweep at the model directory (like build.py does)
    tlc_sweep.CONFIG = cfg
    tlc_sweep.MODULE = cfg.module
    tlc_sweep.SCRIPT_DIR = model_dir
    tlc_sweep.SKIP = cfg.expanded_excluded_set()

    # Work in the model directory so TLC can find .tla files
    os.chdir(str(model_dir))

    print("=" * 60)
    print(f"PlusCal Explorer — TLC Server")
    print(f"Model: {cfg.title}  ({cfg.module})")
    print("=" * 60)

    # Detect TLC
    print(f"\nJava:  {tlc_sweep.JAVA}")
    print(f"TLC:   {tlc_sweep.TLA2TOOLS}")

    tlc_ver = detect_tlc_version()
    TLCHandler.tlc_version = tlc_ver
    print(f"TLC version: {tlc_ver}")

    # Regenerate .tla from PlusCal golden source
    print(f"\nTranslating PlusCal -> TLA+ ...")
    tlc_sweep.translate_pcal()

    # Warm up: verify TLC works with a quick combo
    warmup = cfg.first_combo()
    warmup_tag = ".".join(str(v) for v in warmup.values())
    print(f"\nWarming up (testing TLC with {warmup_tag}) …")
    try:
        combo_key = tuple(sorted(warmup.items()))
        result = get_trace_cached(combo_key)
        n = len(result.get("trace", []))
        ms = result.get("elapsed_ms", 0)
        svg_ok = "yes" if result.get("puml_svg") else "no"
        print(f"  OK — {n} messages in {ms}ms (SVG: {svg_ok})")
    except Exception as e:
        print(f"  WARNING: warm-up failed: {e}")
        print(f"  Server will start anyway, but TLC may not work.")

    # Start server
    server = HTTPServer((args.host, args.port), TLCHandler)
    const_names = ", ".join(cfg.constant_names)
    print(f"\nListening on http://{args.host}:{args.port}")
    print(f"  GET  /api/health")
    print(f"  GET  /api/params")
    print(f"  POST /api/trace         {{{const_names}}}")
    print(f"  POST /api/trace-custom  {{pcal_source, {const_names}}}")
    print(f"  POST /api/stategraph    {{pcal_source}}")
    print(f"\nPress Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")
        server.server_close()


if __name__ == "__main__":
    main()
