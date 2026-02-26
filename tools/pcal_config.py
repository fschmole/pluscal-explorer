"""Load and validate a pcal-explorer.json config file.

The config file describes everything needed to sweep a PlusCal model
through TLC: constants, skip rules, invariants, properties, and
visualization hints (participants, channel abbreviations).

All model-specific knowledge lives in the JSON, not in Python.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════════
# Config dataclass (plain dict wrapper with validation)
# ═══════════════════════════════════════════════════════════════════════

class PcalConfig:
    """Typed accessor for pcal-explorer.json fields."""

    def __init__(self, raw: dict[str, Any], config_path: Path | None = None):
        self._raw = raw
        self._path = config_path

        # ── Required fields ──────────────────────────────────────────
        self.module: str = raw["module"]
        self.pcal: str = raw.get("pcal", f"{self.module}.pcal")
        self.title: str = raw.get("title", self.module)

        # ── Constants (dict of name -> list of values) ───────────────
        self.constants: dict[str, list[str]] = raw.get("constants", {})
        for k, v in self.constants.items():
            if not isinstance(v, list) or not v:
                raise ValueError(f"constants.{k} must be a non-empty list, got {v!r}")

        # ── Skip rules ──────────────────────────────────────────────
        self.invalid_rules: list[dict[str, str]] = raw.get("invalid", [])
        self.skip_rules: list[dict[str, str]] = raw.get("skip", [])

        # ── TLC properties ──────────────────────────────────────────
        self.invariants: list[str] = raw.get("invariants", [])
        self.properties: list[str] = raw.get("properties", [])

        # ── Trace extraction ────────────────────────────────────────
        self.trace_variable: str = raw.get("traceVariable", "trace")
        self.done_variable: str = raw.get("doneVariable", "done")

        # ── Visualization hints ─────────────────────────────────────
        self.participants: list[str] = raw.get("participants", [])
        self.abbreviations: dict[str, str] = raw.get("abbreviations", {})
        # ── Channel palette (optional — extracted from pcal if omitted)
        self._channels: list[str] | None = raw.get("channels")
        # ── Explicit channel color map (optional) ───────────────────
        self.channel_colors: dict[str, dict[str, str]] | None = raw.get("channelColors")
        # ── PlantUML server URL (optional) ──────────────────────────
        self.plantuml_server: str | None = raw.get("plantUmlServer") or None
        # ── TLC server URL (optional) ───────────────────────────────
        self.tlc_server: str | None = raw.get("tlcServer") or None
        # ── Branding (optional — omitted for open-source models) ────
        self.branding: dict[str, Any] | None = raw.get("branding")
        # ── Deploy config (optional) ────────────────────────────────
        self.deploy: dict[str, Any] | None = raw.get("deploy")
        # ── Warm-up combo (optional — first if omitted) ─────────────
        self.warmup: dict[str, str] | None = raw.get("warmup")

    # ── Derived helpers ──────────────────────────────────────────────

    @property
    def pcal_path(self) -> str:
        return self.pcal

    @property
    def tla_path(self) -> str:
        return f"tmp/{self.module}.tla"

    @property
    def constant_names(self) -> list[str]:
        """Ordered list of constant names (dict preserves insertion order)."""
        return list(self.constants.keys())

    def resolve_channels(self, base_dir: Path | None = None) -> list[str]:
        """Return the global channel list.

        If "channels" was set in the JSON config, return it directly.
        Otherwise, extract channel names from the PlusCal source file
        (best-effort: finds <<>>-initialized variables that also appear
        as string literals, which corresponds to Log() ch arguments).

        Parameters
        ----------
        base_dir : Path, optional
            Directory containing the .pcal file.  Defaults to the
            directory that held the config JSON, or cwd.
        """
        if self._channels is not None:
            return list(self._channels)

        if base_dir is None:
            base_dir = self._path.parent if self._path else Path(".")
        pcal = base_dir / self.pcal
        if pcal.exists():
            return self.extract_channels_from_pcal(pcal)
        return []

    @staticmethod
    def extract_channels_from_pcal(pcal_path: Path) -> list[str]:
        """Extract traced channel names from a PlusCal source file.

        Strategy:
          1. Collect all variable names initialized with ``= <<>>``.
          2. Collect all quoted string literals in the file.
          3. The intersection gives variables referenced as strings,
             which in practice are the ``ch`` arguments of ``Log()`` calls.
        """
        text = pcal_path.read_text(encoding="utf-8")
        seq_vars = set(re.findall(r"(\w+)\s*=\s*<<\s*>>", text))
        quoted = set(re.findall(r'"(\w+)"', text))
        return sorted(seq_vars & quoted)

    def all_combos(self) -> list[tuple[str, ...]]:
        """Cartesian product of all constant values."""
        if not self.constants:
            return [()]
        return list(itertools.product(*self.constants.values()))

    def combo_dict(self, combo: tuple[str, ...]) -> dict[str, str]:
        """Map a combo tuple back to {constant_name: value}."""
        return dict(zip(self.constant_names, combo))

    def coerce_value(self, name: str, val: Any) -> Any:
        """Coerce a string value back to its native type based on the config.

        Looks at the declared constant values to infer the expected type
        (int, bool, or str) and converts accordingly.  If *val* is already
        the right type it is returned unchanged.
        """
        declared = self.constants.get(name, [])
        if not declared:
            return val
        sample = declared[0]
        if isinstance(sample, bool):
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return bool(val)
        if isinstance(sample, int):
            if isinstance(val, int) and not isinstance(val, bool):
                return val
            return int(val)
        return str(val)

    def coerce_combo_dict(self, combo_d: dict[str, Any]) -> dict[str, Any]:
        """Coerce all values in a combo dict to their native types."""
        return {k: self.coerce_value(k, v) for k, v in combo_d.items()}

    def coerce_combo_tuple(self, combo: tuple) -> tuple:
        """Coerce a combo tuple's values to their native types."""
        return tuple(
            self.coerce_value(name, val)
            for name, val in zip(self.constant_names, combo)
        )

    def combo_tag(self, combo: tuple[str, ...]) -> str:
        """Dot-separated tag string for a combo tuple."""
        return ".".join(str(v) for v in combo)

    def is_invalid(self, combo: tuple[str, ...]) -> bool:
        """Check whether a combo matches any invalid rule.

        An invalid rule marks an impossible/illegal parameter combination.
        """
        combo_d = self.combo_dict(combo)
        for rule in self.invalid_rules:
            if all(combo_d.get(k) == v for k, v in rule.items()):
                return True
        return False

    def is_skipped(self, combo: tuple[str, ...]) -> bool:
        """Check whether a combo matches any skip rule.

        A skip rule is a partial assignment: unmentioned constants are
        wildcards.  A combo matches if every mentioned constant in the
        rule matches the combo's value for that constant.
        """
        combo_d = self.combo_dict(combo)
        for rule in self.skip_rules:
            if all(combo_d.get(k) == v for k, v in rule.items()):
                return True
        return False

    def is_excluded(self, combo: tuple[str, ...]) -> bool:
        """Check whether a combo is either invalid or skipped."""
        return self.is_invalid(combo) or self.is_skipped(combo)

    def expanded_invalid_set(self) -> set[tuple[str, ...]]:
        """Expand invalid rules into a full set of invalid combo tuples."""
        return {combo for combo in self.all_combos() if self.is_invalid(combo)}

    def expanded_skip_set(self) -> set[tuple[str, ...]]:
        """Expand skip rules into a full set of skipped combo tuples."""
        skipped = set()
        for combo in self.all_combos():
            if self.is_skipped(combo):
                skipped.add(combo)
        return skipped

    def expanded_excluded_set(self) -> set[tuple[str, ...]]:
        """Expand both invalid and skip rules into a full set of excluded combo tuples."""
        return {combo for combo in self.all_combos() if self.is_excluded(combo)}

    def write_cfg(self, combo: tuple[str, ...] | dict[str, str], cfg_path):
        """Write a TLC .cfg file for one parameter combo."""
        if isinstance(combo, tuple):
            combo = self.combo_dict(combo)
        cfg_path = Path(cfg_path)

        lines = [
            "\\* Auto-generated by tlc_sweep.py",
            "SPECIFICATION Spec",
            "",
            "CONSTANTS",
        ]
        for name in self.constant_names:
            val = combo[name]
            # Format value for TLA+ cfg: ints bare, bools as TRUE/FALSE, strings quoted
            if isinstance(val, bool):
                tla_val = "TRUE" if val else "FALSE"
            elif isinstance(val, int):
                tla_val = str(val)
            else:
                tla_val = f'"{val}"'
            lines.append(f'    {name:<12s} = {tla_val}')

        for inv in self.invariants:
            lines.append(f"\nINVARIANT {inv}")
        for prop in self.properties:
            lines.append(f"\nPROPERTY {prop}")

        lines.append("")
        cfg_path.write_text("\n".join(lines), encoding="utf-8")

    def first_combo(self) -> dict[str, str]:
        """Return the warm-up combo (explicit or first of each constant)."""
        if self.warmup:
            return dict(self.warmup)
        return {k: v[0] for k, v in self.constants.items()}


# ═══════════════════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════════════════

def load_config(path: str | Path) -> PcalConfig:
    """Load and validate a pcal-explorer.json file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "module" not in raw:
        raise ValueError(f"Config file {path} missing required field: module")
    return PcalConfig(raw, config_path=path)


def find_config(model_dir: str | Path, module: str | None = None) -> PcalConfig:
    """Find and load a pcal-explorer.json in a directory.

    Search order:
      1. {module}.explorer.json  (if module given)
      2. *.explorer.json         (first match)
      3. pcal-explorer.json      (legacy name)
    """
    d = Path(model_dir)
    if module:
        p = d / f"{module}.explorer.json"
        if p.exists():
            return load_config(p)

    # Try any .explorer.json
    matches = sorted(d.glob("*.explorer.json"))
    if matches:
        return load_config(matches[0])

    # Legacy fallback
    p = d / "pcal-explorer.json"
    if p.exists():
        return load_config(p)

    raise FileNotFoundError(
        f"No explorer config found in {d}.\n"
        f"Create {d / '<module>.explorer.json'} — see PLAN.md for schema."
    )
