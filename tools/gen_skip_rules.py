#!/usr/bin/env python3
"""Generate optimal skip rules from the dedup alias map.

Reads _aliases.json (produced by tlc_sweep.py) and the explorer.json
config, then computes a minimal set of partial-match skip rules that
eliminate all non-canonical (duplicate) combos without skipping any
canonical one.

Algorithm: greedy set-cover over the space of all possible partial-match
rules.  Each rule is a dict of {constant: value} for some subset of
constants.  A rule matches a combo if all specified keys match.  We
enumerate candidate rules, score them by how many uncovered non-canonical
combos they hit (without hitting any canonical), and greedily pick the
best until all non-canonical combos are covered.
"""
import json
import itertools
import sys
from pathlib import Path


def main():
    # ── Load data ────────────────────────────────────────────────────
    aliases = json.loads(Path("distrib/traces/_aliases.json").read_text())
    cfg_files = list(Path(".").glob("*.explorer.json"))
    if not cfg_files:
        sys.exit("ERROR: No *.explorer.json found in current directory")
    cfg = json.loads(cfg_files[0].read_text())
    const_names = list(cfg["constants"].keys())
    const_values = cfg["constants"]  # {name: [values]}

    def parse_tag(tag):
        return dict(zip(const_names, tag.split(".")))

    # Identify canonical vs non-canonical
    groups = {}
    for tag, canon in aliases.items():
        groups.setdefault(canon, []).append(tag)

    canonical_combos = set()
    non_canonical_combos = set()
    for canon, members in groups.items():
        canonical_combos.add(canon)
        for tag in members:
            if tag != canon:
                non_canonical_combos.add(tag)

    print(f"Canonical: {len(canonical_combos)}")
    print(f"Non-canonical (to skip): {len(non_canonical_combos)}")

    # Also account for existing skipped combos (already not in aliases)
    existing_skip = cfg.get("skip", [])
    print(f"Existing skip rules: {len(existing_skip)}")

    # ── Generate candidate rules ─────────────────────────────────────
    # A rule is a partial assignment: a dict mapping some subset of
    # constant names to specific values.  We consider all possible
    # partial assignments of sizes 1..len(const_names).
    # For efficiency, we only go up to size 4 (empirically sufficient).

    def rule_matches(rule, tag):
        """Check if a partial-match rule matches a tag string."""
        d = parse_tag(tag)
        return all(d[k] == v for k, v in rule.items())

    print("\nEnumerating candidate rules ...")
    candidates = []  # (rule_dict, covered_non_canonical_set)
    max_rule_size = min(5, len(const_names))

    for size in range(1, max_rule_size + 1):
        for keys in itertools.combinations(const_names, size):
            # All possible value assignments for these keys
            values_lists = [const_values[k] for k in keys]
            for vals in itertools.product(*values_lists):
                rule = dict(zip(keys, vals))
                # Check: rule must NOT match any canonical combo
                hits_canonical = any(rule_matches(rule, c) for c in canonical_combos)
                if hits_canonical:
                    continue
                # Count non-canonical hits
                covered = frozenset(
                    nc for nc in non_canonical_combos if rule_matches(rule, nc)
                )
                if covered:
                    candidates.append((rule, covered))

    print(f"  {len(candidates)} valid candidate rules")

    # ── Greedy set-cover ─────────────────────────────────────────────
    remaining = set(non_canonical_combos)
    chosen_rules = []

    while remaining:
        # Pick the candidate that covers the most remaining combos
        best_rule = None
        best_covered = frozenset()
        for rule, covered in candidates:
            hits = covered & remaining
            if len(hits) > len(best_covered):
                best_rule = rule
                best_covered = hits
        if not best_covered:
            print(f"\n  WARNING: {len(remaining)} combos not coverable by any rule!")
            print(f"  Uncovered: {sorted(remaining)[:10]}...")
            break
        chosen_rules.append(best_rule)
        remaining -= best_covered
        print(f"  Rule {len(chosen_rules):2d}: {best_rule}  covers {len(best_covered)} ({len(remaining)} remaining)")

    print(f"\n{'='*60}")
    print(f"Optimal skip rules: {len(chosen_rules)} rules cover {len(non_canonical_combos)} non-canonical combos")

    # ── Merge with existing rules ────────────────────────────────────
    # Keep existing rules that aren't subsumed by new ones
    final_rules = list(existing_skip)
    for rule in chosen_rules:
        # Check if already subsumed by an existing rule
        already_covered = any(
            all(rule.get(k) == v for k, v in existing.items())
            for existing in final_rules
        )
        if not already_covered:
            final_rules.append(rule)

    print(f"Final skip rules (including existing): {len(final_rules)}")
    print(f"\nJSON skip array:")
    print(json.dumps(final_rules, indent=2))

    # ── Verify ───────────────────────────────────────────────────────
    # Make sure no canonical combo is skipped and all non-canonical are
    all_combos = list(itertools.product(*const_values.values()))
    skip_count = 0
    canonical_skipped = []
    non_canonical_missed = []

    for combo in all_combos:
        tag = ".".join(combo)
        combo_d = dict(zip(const_names, combo))
        is_skipped = any(
            all(combo_d.get(k) == v for k, v in rule.items())
            for rule in final_rules
        )
        if is_skipped:
            skip_count += 1
            if tag in canonical_combos:
                canonical_skipped.append(tag)
        else:
            if tag in non_canonical_combos:
                non_canonical_missed.append(tag)

    print(f"\nVerification:")
    print(f"  Total combos: {len(all_combos)}")
    print(f"  Would skip: {skip_count}")
    print(f"  Would run: {len(all_combos) - skip_count}")
    print(f"  Canonical accidentally skipped: {len(canonical_skipped)}")
    if canonical_skipped:
        for t in canonical_skipped:
            print(f"    !! {t}")
    print(f"  Non-canonical missed: {len(non_canonical_missed)}")
    if non_canonical_missed:
        for t in non_canonical_missed[:10]:
            print(f"    !! {t}")

    # Write the output to a temp file for easy copy
    output = {"skip": final_rules}
    Path("tmp/generated_skip_rules.json").write_text(
        json.dumps(output, indent=2), encoding="utf-8"
    )
    print(f"\nRules written to tmp/generated_skip_rules.json")


if __name__ == "__main__":
    main()
