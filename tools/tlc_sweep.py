#!/usr/bin/env python3
"""
Sweep all PlusCal model parameter combinations through TLC.

Model-specific knowledge (constants, skip rules, invariants, participants,
channel abbreviations) comes from a `*.explorer.json` config file —
see pcal_config.py and PLAN.md for the schema.

For each combination, writes a temporary .cfg file, runs TLC, and captures
the trace (the final value of the trace variable in the terminal state).

Outputs:
  - Console summary: PASS/FAIL per combo
  - distrib/traces/ directory with one .puml per passing combo
  - distrib/traces/_aliases.json deduplication map

Usage:
  python tlc_sweep.py                       # auto-detect *.explorer.json
  python tlc_sweep.py --model mesi_coherence
  python tlc_sweep.py --model my_model
"""
import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pcal_config import PcalConfig, find_config

SCRIPT_DIR = Path(__file__).resolve().parent
TLA2TOOLS = str(SCRIPT_DIR / "tla2tools.jar")


def _find_java() -> str:
    """Locate a Java executable portably.

    Search order:
      1. JAVA_HOME environment variable  (works on all platforms)
      2. shutil.which("java")            (finds java on PATH)
    """
    # 1. JAVA_HOME
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / ("java.exe" if sys.platform == "win32" else "java")
        if candidate.is_file():
            return str(candidate)

    # 2. PATH lookup
    on_path = shutil.which("java")
    if on_path:
        return on_path

    sys.exit(
        "ERROR: Java not found.\n"
        "  Set JAVA_HOME, add java to PATH, or install a JDK.\n"
        "  TLC requires Java 11+."
    )


JAVA = _find_java()

# Suppress console-window flash on Windows (prevents black-screen flicker).
_NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# Fast-start JVM flags: skip C2 JIT, small heap — saves ~1-2s per JVM launch.
_JVM_FAST = ["-XX:TieredStopAtLevel=1", "-Xms32m", "-Xmx256m"]

# ── Active config — set by load_model_config() at startup ──────────────
# These module-level names are kept for backward compatibility with
# tlc_server.py and build_explorer.py which import them.
CONFIG: PcalConfig | None = None      # set by load_model_config()
MODULE: str = ""                       # set by load_model_config()


def load_model_config(model: str | None = None) -> PcalConfig:
    """Load a model config and set module-level globals."""
    global CONFIG, MODULE
    CONFIG = find_config(SCRIPT_DIR, module=model)
    MODULE = CONFIG.module
    return CONFIG


def _hoist_process_defines(text):
    """Move process-local define {} blocks to global scope.

    The .pcal golden source keeps Ca_*, Ha_*, Rca_* operator families
    inside process-local define {} blocks for readability.  pcal.trans
    (all known versions) does NOT support process-local defines, so we
    hoist them to just before the --fair algorithm line.

    Algorithm-scope define {} blocks (between ``variables`` and the first
    ``process``) ARE supported by pcal.trans and must be left alone.

    Convention: process-local define blocks are indented 4 spaces:
        define {
        ...
        }
    The content inside is indented 4 extra spaces (8 total) and gets
    un-indented by 4 when hoisted.
    """
    lines = text.split("\n")
    hoisted = []       # collected operator lines to insert at global scope
    out_lines = []     # lines with define blocks removed
    in_process = False # track whether we've entered a process block
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Track entry into a process block
        if re.match(r'\s*process\s*\(', lines[i]):
            in_process = True

        # Only hoist "    define {" if we're inside a process block.
        # Algorithm-scope defines (before any process) are left intact.
        if lines[i].rstrip() == "    define {" and in_process:
            # Collect everything until the matching "    }"
            i += 1
            block = []
            while i < len(lines) and lines[i].rstrip() != "    }":
                # Un-indent by 4 spaces
                if lines[i].startswith("    "):
                    block.append(lines[i][4:])
                else:
                    block.append(lines[i])
                i += 1
            i += 1  # skip the closing "    }"
            hoisted.extend(block)
            hoisted.append("")  # blank separator between blocks
        else:
            out_lines.append(lines[i])
            i += 1

    if not hoisted:
        return text  # nothing to hoist

    # Insert hoisted operators just before the --fair algorithm line
    result = []
    for line in out_lines:
        if "--fair algorithm" in line or "--algorithm" in line:
            result.extend(hoisted)
        result.append(line)

    return "\n".join(result)


def _wrap_pcal_for_trans(text):
    """Prepare PlusCal source for pcal.trans translation.

    Two source styles are supported:

    1. **Bare PlusCal** (e.g. my_model.pcal) -- the algorithm block
       is NOT wrapped in ``(* ... *)``.  We wrap it and add
       ``\\* BEGIN/END TRANSLATION`` markers.

    2. **Full .tla-style** (e.g. mesi_coherence.pcal) -- the algorithm
       block is already wrapped in ``(* ... *)``, a closing ``*)``
       exists, and ``\\* BEGIN/END TRANSLATION`` markers are present.
       No wrapping is needed; we only hoist process-local defines.
    """
    text = _hoist_process_defines(text)

    # Detect whether the file is already wrapped
    already_wrapped = False
    for line in text.split("\n"):
        stripped = line.strip()
        # Check for "(* --fair algorithm" or "(* --algorithm"
        if re.match(r'\(\*\s*--(?:fair\s+)?algorithm\b', stripped):
            already_wrapped = True
            break

    if already_wrapped:
        # File already has (* ... *) wrapping and translation markers
        return text

    # --- Bare PlusCal: wrap in (* ... *) and add translation markers ---
    lines = text.split("\n")
    out = []
    in_algo = False
    depth = 0

    for line in lines:
        if not in_algo and ("--fair algorithm" in line or "--algorithm" in line):
            out.append("(* " + line)
            in_algo = True
            depth = line.count("{") - line.count("}")
            continue

        if in_algo:
            depth += line.count("{") - line.count("}")
            out.append(line)
            if depth <= 0:
                out.append("*)")
                out.append("")
                out.append("\\* BEGIN TRANSLATION")
                out.append("\\* END TRANSLATION")
                in_algo = False
            continue

        out.append(line)

    return "\n".join(out)


def translate_pcal(pcal_path=None, tla_path=None):
    """Regenerate .tla from .pcal golden source via pcal.trans.

    Steps:
      1. Read .pcal (bare PlusCal, no comment wrappers)
      2. Re-wrap PlusCal block in (* ... *) + translation markers
      3. Write to .tla  (pcal.trans requires .tla extension)
      4. Run pcal.trans -nocfg  (fills in TLA+ translation in-place)
      5. Prepend DO-NOT-EDIT banner
    """
    if pcal_path is None:
        pcal_path = CONFIG.pcal_path
    if tla_path is None:
        tla_path = CONFIG.tla_path
    pcal_path = Path(pcal_path)
    tla_path  = Path(tla_path)
    if not pcal_path.exists():
        print(f"  WARNING: {pcal_path} not found -- skipping pcal translation")
        return
    # Read golden source and re-wrap for pcal.trans
    raw = pcal_path.read_text(encoding="utf-8")
    wrapped = _wrap_pcal_for_trans(raw)
    tla_path.parent.mkdir(parents=True, exist_ok=True)
    tla_path.write_text(wrapped, encoding="utf-8")
    # Run pcal.trans
    cmd = [JAVA, *_JVM_FAST, "-cp", TLA2TOOLS, "pcal.trans", "-nocfg", str(tla_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **_NOWIN)
    combined = result.stdout + result.stderr
    if result.returncode != 0 or "Unrecoverable error" in combined:
        sys.exit(f"pcal.trans failed:\n{combined}")
    # Inject DO-NOT-EDIT banner at top of generated .tla
    tla_text = tla_path.read_text(encoding="utf-8")
    banner = (
        "\\* ====================================================================\n"
        f"\\* DO NOT EDIT -- this file is generated from {CONFIG.pcal}\n"
        "\\* Regenerate: python tlc_sweep.py  (or: python tlc_server.py)\n"
        "\\* ====================================================================\n"
    )
    tla_path.write_text(banner + tla_text, encoding="utf-8")
    print(f"  pcal.trans -> {tla_path} (OK)")


# ── Channel-style generation (data-driven, no hardcoded names) ─────────
# Palette of 16 highly distinguishable Material-Design colours.
# Each entry is (stroke, label-fill) — label colour is a darker variant.
_PALETTE = [
    ("#e53935", "#c62828"),   # red
    ("#1e88e5", "#1565c0"),   # blue
    ("#43a047", "#2e7d32"),   # green
    ("#00acc1", "#00838f"),   # cyan
    ("#8e24aa", "#6a1b9a"),   # purple
    ("#6d4c41", "#4e342e"),   # brown
    ("#d81b60", "#ad1457"),   # pink
    ("#f4511e", "#d84315"),   # deep-orange
    ("#ffb300", "#ff8f00"),   # amber
    ("#5e35b1", "#4527a0"),   # deep-purple
    ("#00897b", "#00695c"),   # teal
    ("#78909c", "#546e7a"),   # blue-grey
    ("#26a69a", "#00897b"),   # teal-accent
    ("#7cb342", "#558b2f"),   # light-green
    ("#ec407a", "#c2185b"),   # pink-accent
    ("#ab47bc", "#7b1fa2"),   # purple-accent
]


def _humanize_channel(key):
    """Derive a short human-readable label from a channel key.

    Examples:
        agent_bus_a2f_req -> Agent Bus Req
        pcie_req        -> PCIe Req
        mem             -> Memory
        local           -> Local

    Uses CONFIG.abbreviations for model-specific overrides.
    """
    abbr = CONFIG.abbreviations if CONFIG else {}
    # Strip the a2f/f2a direction marker, then split on underscores
    clean = key.replace("_a2f_", "_").replace("_f2a_", "_")
    parts = clean.split("_")
    result = []
    for p in parts:
        up = p.upper()
        if up in abbr:
            result.append(abbr[up])
        else:
            result.append(p.capitalize())
    return " ".join(result)


def _channel_styles(channels):
    """Build a channelStyles map from the global channel list.

    Parameters
    ----------
    channels : list[str]
        Ordered list of channel keys.  The position in this list
        determines the colour assignment, so the same channel always
        receives the same colour regardless of which trace it appears in.

    Uses explicit colours from CONFIG.channel_colors when available,
    falling back to the auto-generated palette.

    Returns a dict:  { channel_key: { stroke, label, name } }
    """
    config_colors = CONFIG.channel_colors if CONFIG else {}
    styles = {}
    for i, key in enumerate(channels):
        if config_colors and key in config_colors:
            cc = config_colors[key]
            stroke = cc.get("stroke", _PALETTE[i % len(_PALETTE)][0])
            label = cc.get("label", _PALETTE[i % len(_PALETTE)][1])
        else:
            stroke, label = _PALETTE[i % len(_PALETTE)]
        styles[key] = {
            "stroke": stroke,
            "label":  label,
            "name":   _humanize_channel(key),
        }
    return styles


def _extract_error_summary(tlc_output: str) -> str:
    """Extract a short error description from TLC output for the diagram banner.

    Looks for common TLC error patterns:
      - "Temporal properties were violated."
      - "Invariant ... is violated."
      - "Deadlock reached."
      - "State N: Stuttering"
    Returns a one-line summary.
    """
    if "Temporal properties were violated" in tlc_output:
        m = re.search(r'State (\d+): Stuttering', tlc_output)
        if m:
            return f"Temporal property violated (stuttering at state {m.group(1)})"
        return "Temporal property violated"
    m = re.search(r'Invariant\s+(\S+)\s+is violated', tlc_output)
    if m:
        return f"Invariant {m.group(1)} violated"
    if "Deadlock reached" in tlc_output:
        return "Deadlock reached"
    m = re.search(r'Error:\s*(.+)', tlc_output)
    if m:
        return m.group(1).strip()[:120]
    return "TLC model checking failed"


def write_cfg(combo, cfg_path):
    """Write a TLC .cfg file for one parameter combo.

    `combo` can be a tuple (positional, matching CONFIG.constant_names)
    or a dict {constant_name: value}.
    """
    CONFIG.write_cfg(combo, cfg_path)


# ── PlantUML generation (primary output format) ─────────────────────────

def _sanitize_alias(name):
    """Sanitize a participant name into a valid PlantUML alias."""
    return re.sub(r'[^a-zA-Z0-9_]', '_', name)


def _esc_puml(s):
    """Escape PlantUML special characters in a label."""
    return s.replace('\\', '\\\\')


def trace_data_to_puml(data, *, error_info=None):
    """Convert a trace data dict to PlantUML text.

    This is the Python equivalent of plantUmlGenerator.ts —
    PlantUML is the primary output format for embedding in specifications.

    Parameters
    ----------
    data : dict
        Keys: parameters, participants, trace, steps, channelStyles.
    error_info : str or None
        If set, the diagram is styled as an error case with a faint red
        background and the given string shown as the error summary.

    Returns
    -------
    str
        PlantUML sequence diagram text.
    """
    lines = []
    lines.append('@startuml')
    lines.append('')

    # Skin settings for clean look
    if error_info:
        lines.append('skinparam backgroundColor #FFF0F0')
    else:
        lines.append('skinparam backgroundColor transparent')
    lines.append('skinparam sequenceMessageAlign center')
    lines.append('skinparam responseMessageBelowArrow true')
    lines.append('skinparam sequenceGroupBorderThickness 1')
    lines.append('skinparam sequenceBoxBorderColor #999999')
    lines.append('skinparam defaultFontName "Segoe UI", Arial, sans-serif')
    lines.append('skinparam defaultFontSize 12')
    lines.append('skinparam sequenceParticipantBorderColor #666666')
    lines.append('skinparam sequenceParticipantBackgroundColor #F5F5F5')
    lines.append('skinparam sequenceLifeLineBorderColor #BBBBBB')
    lines.append('skinparam sequenceDividerBorderColor #CCCCCC')
    lines.append('')

    lines.append('autonumber')
    lines.append('')

    # Error banner (before participants so it appears at the top)
    if error_info:
        # Escape PlantUML creole markup in the error text
        safe = _esc_puml(error_info)
        lines.append(f'note across #FFCCCC')
        lines.append(f'  <b><color:red>\u26a0 TLC ERROR — Counterexample Trace</color></b>')
        lines.append(f'  {safe}')
        lines.append(f'end note')
        lines.append('')

    # Parameter header
    params = data.get("parameters", {})
    if params:
        header_text = "  ".join(f"{k}={v}" for k, v in params.items())
        lines.append(f'header {header_text}')
        lines.append('')

    # Declare ALL participants
    for p in data.get("participants", []):
        lines.append(f'participant "{p}" as {_sanitize_alias(p)}')
    lines.append('')

    # Channel styles lookup
    styles = data.get("channelStyles", {})
    default_style = {"stroke": "#616161", "label": "#424242", "name": "?"}

    # Faint fill colours for concurrent-chain group boxes
    group_colors = ['#FFF0F0', '#F0F9FF', '#F0FFF4', '#FFF8F0', '#F8F0FF', '#F0FFFC']

    def _style_of(msg):
        ch = msg.get("ch") or msg.get("channel")
        if ch and ch in styles:
            return styles[ch]
        return default_style

    def _arrow(msg):
        s = _style_of(msg)
        color = s["stroke"].lstrip('#')
        line = msg.get("line") or msg.get("style")
        if line == "dashed":
            return f'-[#{color},dashed]>'
        elif line == "dotted":
            return f'-[#{color},dotted]>'
        return f'-[#{color}]>'

    def _label(msg):
        s = _style_of(msg)
        ch = msg.get("ch") or msg.get("channel")
        color = s["label"].lstrip('#')
        txt = _esc_puml(msg["msg"])
        if ch:
            # Channel on second line in smaller font (matches vscode-tlaplus style)
            # Close <color> before \n — PlantUML creole breaks mid-tag across newlines
            return (f'<color:#{color}><b>{txt}</b></color>'
                    f'\\n<color:#{color}><size:9>{_esc_puml(ch)}</size></color>')
        return f'<color:#{color}><b>{txt}</b></color>'

    def _render_msg(msg):
        src = _sanitize_alias(msg["src"])
        dst = _sanitize_alias(msg["dst"])
        return f'{src} {_arrow(msg)} {dst} : {_label(msg)}'

    def _render_concurrent(chains):
        if not chains:
            return
        if len(chains) == 1:
            for msg in chains[0]:
                lines.append(_render_msg(msg))
            return
        lines.append('')
        lines.append('par Concurrent Chains')
        for i, chain in enumerate(chains):
            border = group_colors[i % len(group_colors)]
            lines.append(f'  group {border} Chain {i + 1}')
            for msg in chain:
                lines.append(f'    {_sanitize_alias(msg["src"])} {_arrow(msg)} {_sanitize_alias(msg["dst"])} : {_label(msg)}')
            lines.append('  end')
        lines.append('end')
        lines.append('')

    # Render steps (structured) or flat trace (legacy)
    steps = data.get("steps")
    if steps:
        for step in steps:
            if isinstance(step, dict) and "concurrent" in step:
                _render_concurrent(step["concurrent"])
            else:
                # Sequential step: list of messages
                for msg in step:
                    lines.append(_render_msg(msg))
    else:
        for msg in data.get("trace", []):
            lines.append(_render_msg(msg))

    lines.append('')
    lines.append('@enduml')
    return '\n'.join(lines)


def parse_trace(output):
    """Extract the trace sequence from the last state's TLC output.

    Handles records with or without `ch` field (e.g. MESI has no ch).
    Uses CONFIG.trace_variable for the variable name (default: "trace").
    """
    trace_var = CONFIG.trace_variable if CONFIG else "trace"
    # Collapse whitespace — TLC wraps long lines in dump output, which can
    # split delimiters like ">>" across lines and break naive regexes.
    flat = re.sub(r'\s+', ' ', output)
    pattern = r'/\\ ' + re.escape(trace_var) + r' = <<(.*?)>>'
    matches = re.findall(pattern, flat)
    if not matches:
        return None
    raw = matches[-1].strip()
    if not raw:
        return []
    entries = []
    for rec_m in re.finditer(r'\[([^\]]+)\]', raw):
        rec = rec_m.group(1)
        dst = re.search(r'dst\s*\|->\s*"([^"]+)"', rec)
        msg = re.search(r'msg\s*\|->\s*"([^"]+)"', rec)
        src = re.search(r'src\s*\|->\s*"([^"]+)"', rec)
        if not (dst and msg and src):
            continue
        entry = {"msg": msg.group(1), "src": src.group(1), "dst": dst.group(1)}
        ch = re.search(r'ch\s*\|->\s*"([^"]+)"', rec)
        if ch:
            entry["ch"] = ch.group(1)
        entries.append(entry)
    return entries

def parse_all_terminal_traces(dump_text):
    """Extract ALL distinct terminal traces from a TLC dump file.

    Returns a list of traces (each trace is a list of {msg,src,dst[,ch]} dicts).
    Multiple terminal states arise when TLC explores different interleavings
    of concurrent processes — each interleaving produces a different message
    ordering in the trace variable.

    Uses CONFIG.done_variable to detect terminal states.
    """
    done_var = CONFIG.done_variable if CONFIG else "done"
    done_marker = f"{done_var} = TRUE"
    blocks = re.split(r'\nState \d+:\n', dump_text)
    seen = set()
    traces = []

    for block in blocks:
        if done_marker not in block:
            continue
        trace = parse_trace(block)
        if not trace:
            continue
        # Deduplicate by message sequence
        sig = tuple(
            (m["msg"], m["src"], m["dst"], m.get("ch", ""))
            for m in trace
        )
        if sig not in seen:
            seen.add(sig)
            traces.append(trace)

    return traces


def _msg_sig(m):
    """Hashable signature for a trace message."""
    return (m["msg"], m["src"], m["dst"], m.get("ch", ""))


def _order_of_subset(traces, sig_indices, subset):
    """Return the relative order of *subset* indices within each trace.

    Returns a list of tuples (one per trace).  Each tuple lists the subset
    indices in the order they appear in that trace.  If all tuples are
    identical, the subset's internal order is *stable* across traces.
    """
    orders = []
    for trace in traces:
        # Build pos-of-sig for this trace
        sig_to_pos = {}
        for pos, m in enumerate(trace):
            sig_to_pos[_msg_sig(m)] = pos
        # Sort subset indices by their position in this trace
        ranked = sorted(subset, key=lambda i: sig_to_pos.get(sig_indices[i], i))
        orders.append(tuple(ranked))
    return orders


def _split_variant(traces, sig_indices, indices):
    """Recursively split *indices* into causal chains.

    For each proper non-empty subset S of *indices* (up to complement
    symmetry), check whether S and its complement S̄ both have stable
    internal order across all *traces*.  If so, S and S̄ are independent
    causal chains — recurse into each half in case they contain further
    variant sub-sequences.

    Returns a list of chains, where each chain is a list of canonical-
    trace indices in their stable order.
    """
    if len(indices) <= 1:
        return [list(indices)]

    # Check if the whole set is already stable (single chain)
    orders = _order_of_subset(traces, sig_indices, indices)
    if len(set(orders)) == 1:
        return [list(orders[0])]

    idx_list = list(indices)
    k = len(idx_list)

    # Enumerate subsets of size 1…k//2  (complement covers the other half)
    combinations = itertools.combinations
    best_split = None
    for size in range(1, k // 2 + 1):
        for combo in combinations(range(k), size):
            s_set = frozenset(idx_list[c] for c in combo)
            s_bar = frozenset(indices) - s_set
            if not s_bar:
                continue
            # Avoid checking symmetric complements when size == k//2
            if size == k - size and min(s_set) > min(s_bar):
                continue

            s_orders = _order_of_subset(traces, sig_indices, s_set)
            if len(set(s_orders)) != 1:
                continue
            sb_orders = _order_of_subset(traces, sig_indices, s_bar)
            if len(set(sb_orders)) != 1:
                continue

            # Valid split found — prefer the most balanced one
            best_split = (s_set, s_bar, s_orders[0], sb_orders[0])
            # For size < k//2, a split of 1-vs-rest might exist but a
            # more balanced one is better for rendering — keep searching
            # within this size level.  But any valid split is correct,
            # so we can stop at the first balanced size that works.
            break
        if best_split is not None:
            break

    if best_split is None:
        # No valid 2-way split — each message is its own chain
        # Sort by canonical order for stable rendering
        return [[i] for i in sorted(indices)]

    s_set, s_bar, s_order, sb_order = best_split
    # Recurse into each half (they might contain further variant ranges)
    left  = _split_variant(traces, sig_indices, s_set)
    right = _split_variant(traces, sig_indices, s_bar)
    return left + right


def compute_steps(all_traces):
    """Derive sequential steps and concurrent regions from trace variants.

    Algorithm:
      1. **Segment** the canonical trace into *fixed* positions (same
         message at that position in every trace) and *variant* ranges.
      2. Each fixed position becomes a sequential step ``[msg]``.
      3. Each variant range is split into causal chains by finding subsets
         whose internal order is stable across all traces.

    Returns:
      steps: list of ``[msg]`` (sequential) and
             ``{"concurrent": [[chain_a], [chain_b], …]}`` (concurrent).
      canonical_trace: the first trace (reference order).
    """
    if not all_traces:
        return [], []

    canonical = all_traces[0]
    n = len(canonical)

    if len(all_traces) == 1:
        return [[m] for m in canonical], canonical

    # Build sig → canonical index mapping
    sig_indices = [_msg_sig(m) for m in canonical]

    # ── Phase 1: identify fixed vs variant positions ──────────────
    # A position p is *fixed* iff every trace has the same sig at p.
    fixed = [True] * n
    for trace in all_traces[1:]:
        for p in range(n):
            if fixed[p] and _msg_sig(trace[p]) != sig_indices[p]:
                fixed[p] = False

    # ── Phase 2: segment into fixed / variant ranges ─────────────
    steps = []
    i = 0
    while i < n:
        if fixed[i]:
            steps.append([canonical[i]])
            i += 1
        else:
            # Collect maximal contiguous variant range
            j = i
            while j < n and not fixed[j]:
                j += 1
            variant_indices = frozenset(range(i, j))
            chains = _split_variant(all_traces, sig_indices, variant_indices)
            # Convert index-chains to message-chains
            msg_chains = [[canonical[idx] for idx in chain] for chain in chains]
            if len(msg_chains) == 1:
                # Single chain — emit as sequential steps
                for m in msg_chains[0]:
                    steps.append([m])
            else:
                steps.append({"concurrent": msg_chains})
            i = j

    return steps, canonical


def parse_trace_from_dump(dump_text):
    """Extract trace from a TLC dump file (backwards compat wrapper)."""
    traces = parse_all_terminal_traces(dump_text)
    return traces[0] if traces else None


def run_single_combo(config: PcalConfig, combo_dict: dict, model_dir: Path) -> dict | None:
    """Run TLC for a single parameter combo and return trace data (or None on failure).

    This is the entry point used by build.py's cmd_sweep to run one combo
    at a time.  It writes the .cfg, runs TLC, and returns the result dict
    (with parameters, participants, trace, steps, channelStyles) on success,
    or None on failure/timeout.
    """
    cfg_path = model_dir / "tmp" / f"{config.module}_sweep.cfg"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    combo_tuple = tuple(combo_dict[k] for k in config.constant_names)
    write_cfg(combo_tuple, cfg_path)

    try:
        success, all_traces, output = run_tlc(cfg_path)
    except subprocess.TimeoutExpired:
        return None

    if not success:
        # Try to extract the counterexample trace from TLC output
        error_trace = parse_trace(output)
        if error_trace:
            error_summary = _extract_error_summary(output)
            steps_err, canonical_err = compute_steps([error_trace])
            global_channels = config.resolve_channels()
            global_styles = _channel_styles(global_channels) if global_channels else {}
            return {
                "parameters": combo_dict,
                "participants": config.participants,
                "trace": canonical_err,
                "steps": steps_err,
                "channelStyles": global_styles,
                "error": True,
                "error_info": error_summary,
            }
        return None

    if not all_traces:
        # Model passed but no trace extracted (deadlock-free, no liveness)
        return {"parameters": combo_dict, "participants": config.participants,
                "trace": [], "steps": [], "channelStyles": {}}

    steps, canonical_trace = compute_steps(all_traces)
    global_channels = config.resolve_channels()
    global_styles = _channel_styles(global_channels) if global_channels else {}

    return {
        "parameters": combo_dict,
        "participants": config.participants,
        "trace": canonical_trace,
        "steps": steps,
        "channelStyles": global_styles,
    }


def _kill_proc_tree(pid):
    """Kill a process and all its children (Windows process-tree kill).

    On Windows, subprocess.kill() only terminates the immediate process,
    leaving JVM worker threads alive.  taskkill /T /F kills the whole tree.
    On Unix, os.killpg() handles process groups.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5, **_NOWIN)
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def run_tlc(cfg_path):
    """Run TLC and return (success: bool, all_traces: list[list], output: str)."""
    tmp = Path("tmp")
    tmp.mkdir(exist_ok=True)
    dump_file = tmp / "tlc_dump.dump"      # TLC appends .dump
    try:
        dump_file.unlink(missing_ok=True)
    except PermissionError:
        pass  # stale lock from dying JVM — will be overwritten
    # Resolve cfg to absolute since we run TLC with cwd=tmp/
    cfg_abs = str(Path(cfg_path).resolve())
    cmd = [
        JAVA, *_JVM_FAST, "-XX:+UseParallelGC",
        "-cp", TLA2TOOLS,
        "tlc2.TLC", MODULE,
        "-config", cfg_abs,
        "-deadlock",
        "-workers", "1",
        "-dump", "tlc_dump",
    ]
    # Use Popen so we can kill the full process tree on timeout.
    # subprocess.run on Windows only kills the immediate process, leaving
    # JVM worker threads alive — the root cause of stale TLC processes.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(tmp), **_NOWIN)
    try:
        stdout, stderr = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc.pid)
        proc.kill()
        proc.wait()
        raise
    combined = stdout + stderr
    success = "Model checking completed. No error has been found." in combined

    # Parse ALL terminal traces from dump file
    all_traces = []
    if success and dump_file.exists():
        try:
            dump_text = dump_file.read_text(encoding="utf-8", errors="replace")
            all_traces = parse_all_terminal_traces(dump_text)
        except PermissionError:
            pass  # Windows: JVM may still hold the file briefly
    if not all_traces:
        # Fallback: parse single trace from TLC error trace output
        single = parse_trace(combined)
        if single:
            all_traces = [single]

    try:
        dump_file.unlink(missing_ok=True)
    except PermissionError:
        pass  # Windows: JVM may still hold the file briefly
    return success, all_traces, combined

def main():
    parser = argparse.ArgumentParser(description="Sweep PlusCal model combos through TLC")
    parser.add_argument("--model", "-m", default=None,
                        help="Model name (e.g. mesi_coherence). "
                             "Auto-detected from *.explorer.json if omitted.")
    args = parser.parse_args()

    os.chdir(Path(__file__).parent)

    # Load config and set module-level globals
    cfg = load_model_config(args.model)

    print(f"Model:  {cfg.title}  ({cfg.module})")
    print(f"Config: {cfg._path}")

    # Resolve global channel palette (consistent colours across all traces)
    global_channels = cfg.resolve_channels()
    global_styles = _channel_styles(global_channels) if global_channels else {}
    if global_channels:
        print(f"Channels: {len(global_channels)} ({', '.join(global_channels[:5])}{'…' if len(global_channels) > 5 else ''})")

    # Regenerate .tla from PlusCal golden source
    print("Translating PlusCal -> TLA+ ...")
    translate_pcal()

    traces_dir = Path("distrib") / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    combos = cfg.all_combos()
    total = len(combos)
    passed = 0
    failed = 0
    skipped = 0
    errors = []

    cfg_path = Path(f"tmp/{cfg.module}_sweep.cfg")

    # Collect traces for deduplication: tag -> trace (list of msg dicts)
    all_results = {}  # tag -> trace list

    for i, combo in enumerate(combos, 1):
        tag = cfg.combo_tag(combo)
        combo_d = cfg.combo_dict(combo)

        if cfg.is_excluded(combo):
            print(f"[{i:3d}/{total}] SKIP  {tag}")
            skipped += 1
            continue

        write_cfg(combo, cfg_path)

        try:
            success, all_traces, output = run_tlc(cfg_path)
        except subprocess.TimeoutExpired:
            print(f"[{i:3d}/{total}] TIMEOUT {tag}")
            errors.append((tag, "timeout"))
            failed += 1
            continue

        if success:
            if all_traces:
                steps, canonical_trace = compute_steps(all_traces)
                n_interleave = len(all_traces)
                n_regions = sum(1 for s in steps if isinstance(s, dict))
                n_visual = sum(
                    max(len(c) for c in s["concurrent"]) if isinstance(s, dict)
                    else 1
                    for s in steps
                )
                detail = f"{len(canonical_trace)} msgs, {n_visual} steps"
                if n_regions > 0:
                    detail += f", {n_regions} concurrent, {n_interleave} interleavings"
                print(f"[{i:3d}/{total}] PASS  {tag}  ({detail})")
                all_results[tag] = {
                    "parameters": combo_d,
                    "participants": cfg.participants,
                    "trace": canonical_trace,
                    "steps": steps,
                    "channelStyles": global_styles,
                }
            else:
                print(f"[{i:3d}/{total}] PASS  {tag}  (no trace extracted)")
            passed += 1
        else:
            failed += 1
            # Try to extract counterexample trace from TLC error output
            error_trace = parse_trace(output)
            if error_trace:
                error_summary = _extract_error_summary(output)
                steps_err, canonical_err = compute_steps([error_trace])
                print(f"[{i:3d}/{total}] FAIL  {tag}  (counterexample: {len(error_trace)} msgs — {error_summary})")
                all_results[tag] = {
                    "parameters": combo_d,
                    "participants": cfg.participants,
                    "trace": canonical_err,
                    "steps": steps_err,
                    "channelStyles": global_styles,
                    "error": True,
                    "error_info": error_summary,
                }
            else:
                print(f"[{i:3d}/{total}] FAIL  {tag}")
            errors.append((tag, output[-500:] if output else "no output"))
            # Save error output
            err_file = traces_dir / f"{tag}.error.txt"
            err_file.write_text(output, encoding="utf-8")

    # ── Deduplication: collapse identical step sequences into aliases ────
    print(f"\nDeduplicating traces ...")
    # Build canonical map: steps_signature -> first tag seen
    canonical = {}  # signature -> canonical_tag
    aliases = {}    # tag -> canonical_tag (for ALL tags, including canonical -> itself)
    for tag, data in all_results.items():
        sig = json.dumps(data["steps"], sort_keys=True, separators=(",", ":"))
        if sig not in canonical:
            canonical[sig] = tag
        aliases[tag] = canonical[sig]

    # Count unique vs total
    unique_tags = set(canonical.values())
    n_passing = sum(1 for t in all_results if not all_results[t].get("error"))
    n_failing = sum(1 for t in all_results if all_results[t].get("error"))
    print(f"   {n_passing} passing combos -> {len([t for t in unique_tags if not all_results[t].get('error')])} unique traces"
          + (f", {n_failing} failing with counterexamples" if n_failing else ""))

    # Write canonical traces as .puml (PlantUML) — the primary output format.
    # PlantUML text can be embedded in specifications, rendered by any
    # PlantUML toolchain, or displayed in the web explorer.
    for tag in unique_tags:
        data = all_results[tag]
        error_info = data.get("error_info") if data.get("error") else None
        puml_text = trace_data_to_puml(data, error_info=error_info)
        puml_file = traces_dir / f"{tag}.puml"
        puml_file.write_text(puml_text, encoding="utf-8")

    # Write alias map (still JSON — it's metadata, not a diagram artifact)
    alias_file = traces_dir / "_aliases.json"
    alias_file.write_text(json.dumps(aliases, indent=2, sort_keys=True), encoding="utf-8")
    print(f"   Alias map: {alias_file}")

    print(f"\n{'='*60}")
    print(f"Total: {total}  Passed: {passed}  Failed: {failed}  Skipped: {skipped}")
    if errors:
        print(f"\nFailed combos:")
        for tag, msg in errors:
            print(f"  {tag}: {msg[:200]}")

    # Clean up temp cfg
    cfg_path.unlink(missing_ok=True)

    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
