#!/usr/bin/env python3
"""
Generate PlantUML activity and state diagrams from PlusCal AST.tla files.

Reads the AST.tla file produced by `pcal.trans -writeAST` from stdin
and outputs PlantUML diagram(s) to stdout or to files in --output-dir.

Usage:
  cat AST.tla | python ast_to_puml.py                      # both diagrams to stdout
  cat AST.tla | python ast_to_puml.py --activity            # activity only
  cat AST.tla | python ast_to_puml.py --state               # state only
  cat AST.tla | python ast_to_puml.py --output-dir ./out    # write .puml files
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
#  Section 1 — TLA+ Record Literal Parser
# ═══════════════════════════════════════════════════════════════════════

# Token kinds
_LBRACKET = "LBRACKET"   # [
_RBRACKET = "RBRACKET"   # ]
_LANGLE   = "LANGLE"     # <<
_RANGLE   = "RANGLE"     # >>
_MAPSTO   = "MAPSTO"     # |->
_COMMA    = "COMMA"      # ,
_STRING   = "STRING"     # "..."
_NAME     = "NAME"       # identifier
_NUMBER   = "NUMBER"     # digits
_OP       = "OP"         # anything else (operators preserved as-is)

_TOKEN_RE = re.compile(r"""
    (?P<LANGLE>  <<)        |
    (?P<RANGLE>  >>)        |
    (?P<MAPSTO>  \|->)      |
    (?P<STRING>  "(?:[^"\\]|\\.)*")  |
    (?P<LBRACKET> \[)       |
    (?P<RBRACKET> \])       |
    (?P<COMMA>   ,)         |
    (?P<NUMBER>  \d+)       |
    (?P<NAME>    [A-Za-z_][A-Za-z_0-9]*)  |
    (?P<WS>      \s+)       |
    (?P<OP>      [^\s\[\],"<>|]+|[<>|])
""", re.VERBOSE)


@dataclass
class Token:
    kind: str
    value: str


def tokenize(text: str) -> list[Token]:
    """Tokenize the TLA+ record literal subset used in AST.tla."""
    tokens: list[Token] = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "WS":
            continue
        value = m.group()
        if kind == _STRING:
            value = value[1:-1]  # strip quotes
            value = value.replace('\\"', '"')  # unescape \" → "
        tokens.append(Token(kind, value))
    return tokens


def parse_value(tokens: list[Token], pos: int) -> tuple[Any, int]:
    """Recursive-descent parser. Returns (python_value, next_pos)."""
    if pos >= len(tokens):
        raise ValueError("Unexpected end of input")

    tok = tokens[pos]

    # Record: [ field, field, ... ]
    if tok.kind == _LBRACKET:
        return _parse_record(tokens, pos)

    # Sequence: << val, val, ... >>
    if tok.kind == _LANGLE:
        return _parse_sequence(tokens, pos)

    # String literal
    if tok.kind == _STRING:
        return tok.value, pos + 1

    # Number literal
    if tok.kind == _NUMBER:
        return tok.value, pos + 1

    # Name (identifier) — appears in expression tokens like variable names
    if tok.kind == _NAME:
        return tok.value, pos + 1

    # Operator — preserve as string
    if tok.kind == _OP:
        return tok.value, pos + 1

    raise ValueError(f"Unexpected token {tok!r} at position {pos}")


def _parse_record(tokens: list[Token], pos: int) -> tuple[dict, int]:
    """Parse [ name |-> value, name |-> value, ... ]"""
    assert tokens[pos].kind == _LBRACKET
    pos += 1  # skip [
    rec: dict[str, Any] = {}
    while pos < len(tokens) and tokens[pos].kind != _RBRACKET:
        # Skip commas between fields
        if tokens[pos].kind == _COMMA:
            pos += 1
            continue
        # field:  name |-> value
        if tokens[pos].kind != _NAME:
            raise ValueError(f"Expected field name, got {tokens[pos]!r} at {pos}")
        field_name = tokens[pos].value
        pos += 1
        if pos >= len(tokens) or tokens[pos].kind != _MAPSTO:
            raise ValueError(f"Expected |-> after '{field_name}' at {pos}")
        pos += 1  # skip |->
        value, pos = parse_value(tokens, pos)
        rec[field_name] = value
    if pos < len(tokens) and tokens[pos].kind == _RBRACKET:
        pos += 1  # skip ]
    return rec, pos


def _parse_sequence(tokens: list[Token], pos: int) -> tuple[list, int]:
    """Parse << val, val, ... >>"""
    assert tokens[pos].kind == _LANGLE
    pos += 1  # skip <<
    items: list[Any] = []
    while pos < len(tokens) and tokens[pos].kind != _RANGLE:
        if tokens[pos].kind == _COMMA:
            pos += 1
            continue
        val, pos = parse_value(tokens, pos)
        items.append(val)
    if pos < len(tokens) and tokens[pos].kind == _RANGLE:
        pos += 1  # skip >>
    return items, pos


def parse_ast_tla(text: str) -> dict:
    """Parse an AST.tla file into a Python dict.

    Locates `ast ==` in the text, tokenizes the RHS, and parses it as
    a TLA+ record literal.
    """
    # Find the `ast ==` definition
    match = re.search(r'\bast\s*==\s*', text)
    if not match:
        raise ValueError("Could not find 'ast ==' in input")
    rhs = text[match.end():]
    # Strip module footer (====...)
    rhs = re.sub(r'={4,}.*', '', rhs, flags=re.DOTALL)
    tokens = tokenize(rhs)
    if not tokens:
        raise ValueError("No tokens found after 'ast =='")
    result, _ = parse_value(tokens, 0)
    if not isinstance(result, dict):
        raise ValueError(f"Expected record at top level, got {type(result).__name__}")
    return result


# ═══════════════════════════════════════════════════════════════════════
#  Section 2 — Control-Flow Graph (CFG) Builder
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CFGEdge:
    source: str
    target: str
    guard: str | None = None
    kind: str = "sequential"  # sequential | branch | loop | goto

    @property
    def edge_id(self) -> str:
        return f"{self.source}->{self.target}"


@dataclass
class CFGNode:
    label: str
    stmts: list[dict] = field(default_factory=list)
    outgoing: list[CFGEdge] = field(default_factory=list)


@dataclass
class ProcessCFG:
    name: str
    variables: list[dict] = field(default_factory=list)
    nodes: dict[str, CFGNode] = field(default_factory=dict)
    entry: str = ""


def _reassemble_quotes(tokens: list[str]) -> list[str]:
    """Merge standalone quote chars back into quoted strings: \" M \" → \"M\"."""
    result: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == '"':
            # Collect tokens until closing quote
            j = i + 1
            parts: list[str] = []
            while j < len(tokens) and tokens[j] != '"':
                parts.append(tokens[j])
                j += 1
            if j < len(tokens):  # found closing quote
                result.append('"' + ''.join(parts) + '"')
                i = j + 1
            else:
                result.append(tokens[i])
                i += 1
        else:
            result.append(tokens[i])
            i += 1
    return result


def _smart_join(tokens: list[str]) -> str:
    """Join tokens with context-aware spacing."""
    if not tokens:
        return ""
    parts = [tokens[0]]
    for t in tokens[1:]:
        # No space before closing delimiters / punctuation
        if t in (')', ']', '}', ',', '.'):
            parts.append(t)
        # No space after opening delimiters
        elif parts[-1] in ('(', '[', '{'):
            parts.append(t)
        # No space after/before dot (record field access)
        elif parts[-1] == '.':
            parts.append(t)
        else:
            parts.append(' ')
            parts.append(t)
    return ''.join(parts)


def _expr_to_str(expr) -> str:
    """Convert an expression (string or sequence of tokens) to a readable string."""
    if isinstance(expr, str):
        return expr
    if isinstance(expr, list):
        tokens = [str(e) for e in expr]
        tokens = _reassemble_quotes(tokens)
        return _smart_join(tokens)
    return str(expr)


def _stmt_summary(stmt: dict) -> str:
    """One-line human-readable summary of a statement AST node."""
    t = stmt.get("type", "")
    if t == "assignment":
        parts = []
        for a in stmt.get("ass", []):
            lhs = a.get("lhs", {})
            var = lhs.get("var", "?") if isinstance(lhs, dict) else str(lhs)
            sub = lhs.get("sub", []) if isinstance(lhs, dict) else []
            if sub:
                var += f"[{_expr_to_str(sub)}]"
            rhs = _expr_to_str(a.get("rhs", "?"))
            parts.append(f"{var} := {rhs}")
        return "; ".join(parts)
    if t == "if":
        guard = _expr_to_str(stmt.get('test', '?'))
        then_stmts = [s for s in stmt.get('then', []) if isinstance(s, dict)]
        else_stmts = [s for s in stmt.get('else', []) if isinstance(s, dict)]
        result = f"IF ({guard})"
        then_summaries = [_stmt_summary(s) for s in then_stmts[:3]]
        then_summaries = [s for s in then_summaries if s]
        if then_summaries:
            brief = "; ".join(then_summaries[:2])
            if len(then_summaries) > 2:
                brief += "; ..."
            result += f" {{ {brief} }}"
        else_summaries = [_stmt_summary(s) for s in else_stmts[:3]]
        else_summaries = [s for s in else_summaries if s]
        if else_summaries:
            brief = "; ".join(else_summaries[:2])
            if len(else_summaries) > 2:
                brief += "; ..."
            result += f" ELSE {{ {brief} }}"
        return result
    if t == "while":
        return f"while ({_expr_to_str(stmt.get('test', '?'))})"
    if t == "either":
        return "either { ... }"
    if t == "with":
        return f"with ({_expr_to_str(stmt.get('var', '?'))})"
    if t == "await" or t == "when":
        return f"await ({_expr_to_str(stmt.get('exp', '?'))})"
    if t == "print":
        return f"print({_expr_to_str(stmt.get('exp', '?'))})"
    if t == "assert":
        return f"assert({_expr_to_str(stmt.get('exp', '?'))})"
    if t == "skip":
        return "skip"
    if t == "goto":
        return f"goto {stmt.get('to', '?')}"
    if t == "call":
        return f"call {stmt.get('to', '?')}()"
    if t == "return":
        return "return"
    if t == "callReturn":
        return f"call {stmt.get('to', '?')}(); return"
    if t == "callGoto":
        return f"call {stmt.get('to', '?')}(); goto {stmt.get('after', '?')}"
    return t or "???"


def _collect_labels_from_body(body: list[dict]) -> list[str]:
    """Collect all label names from a list of labeled statements."""
    labels = []
    for ls in body:
        if isinstance(ls, dict) and "label" in ls:
            labels.append(ls["label"])
            # Also collect labels nested in stmts
            labels.extend(_collect_labels_from_stmts(ls.get("stmts", [])))
    return labels


def _collect_labels_from_stmts(stmts: list) -> list[str]:
    """Recursively collect labels from nested statement lists."""
    labels = []
    for s in stmts:
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")
        if t == "while":
            for lab in s.get("labDo", []):
                if isinstance(lab, dict) and "label" in lab:
                    labels.append(lab["label"])
                    labels.extend(_collect_labels_from_stmts(lab.get("stmts", [])))
        if t in ("if", "labelIf"):
            for lab in s.get("then", []):
                if isinstance(lab, dict) and "label" in lab:
                    labels.append(lab["label"])
                    labels.extend(_collect_labels_from_stmts(lab.get("stmts", [])))
            for lab in s.get("else", []):
                if isinstance(lab, dict) and "label" in lab:
                    labels.append(lab["label"])
                    labels.extend(_collect_labels_from_stmts(lab.get("stmts", [])))
        if t in ("either", "labelEither"):
            for clause in s.get("ors", []):
                if isinstance(clause, list):
                    for lab in clause:
                        if isinstance(lab, dict) and "label" in lab:
                            labels.append(lab["label"])
                            labels.extend(_collect_labels_from_stmts(lab.get("stmts", [])))
    return labels


def _build_process_cfg(name: str, body: list[dict], variables: list[dict] | None = None) -> ProcessCFG:
    """Build a ProcessCFG from a body (list of labeled statements)."""
    cfg = ProcessCFG(name=name, variables=variables or [])
    if not body:
        return cfg

    # First pass: create all CFGNodes from labeled statements
    all_labels: list[str] = []
    _create_nodes_from_body(cfg, body, all_labels)

    if all_labels:
        cfg.entry = all_labels[0]

    # Second pass: add edges
    _add_edges_from_body(cfg, body, next_label=None)

    return cfg


def _create_nodes_from_body(cfg: ProcessCFG, body: list[dict], all_labels: list[str]):
    """Recursively create CFGNodes for every labeled block."""
    for ls in body:
        if not isinstance(ls, dict) or "label" not in ls:
            continue
        label = ls["label"]
        all_labels.append(label)
        node = CFGNode(label=label)
        cfg.nodes[label] = node
        # Collect non-label statements for this node
        for s in ls.get("stmts", []):
            if isinstance(s, dict):
                node.stmts.append(s)
                _create_nodes_from_nested(cfg, s, all_labels)


def _create_nodes_from_nested(cfg: ProcessCFG, stmt: dict, all_labels: list[str]):
    """Recursively create nodes from labels inside while/if/either."""
    t = stmt.get("type", "")
    if t == "while":
        for lab in stmt.get("labDo", []):
            if isinstance(lab, dict) and "label" in lab:
                all_labels.append(lab["label"])
                node = CFGNode(label=lab["label"])
                cfg.nodes[lab["label"]] = node
                for s in lab.get("stmts", []):
                    if isinstance(s, dict):
                        node.stmts.append(s)
                        _create_nodes_from_nested(cfg, s, all_labels)
    for branch_key in ("then", "else"):
        for lab in stmt.get(branch_key, []):
            if isinstance(lab, dict) and "label" in lab:
                all_labels.append(lab["label"])
                node = CFGNode(label=lab["label"])
                cfg.nodes[lab["label"]] = node
                for s in lab.get("stmts", []):
                    if isinstance(s, dict):
                        node.stmts.append(s)
                        _create_nodes_from_nested(cfg, s, all_labels)
    if t in ("either", "labelEither"):
        for clause in stmt.get("ors", []):
            if isinstance(clause, list):
                for lab in clause:
                    if isinstance(lab, dict) and "label" in lab:
                        all_labels.append(lab["label"])
                        node = CFGNode(label=lab["label"])
                        cfg.nodes[lab["label"]] = node
                        for s in lab.get("stmts", []):
                            if isinstance(s, dict):
                                node.stmts.append(s)
                                _create_nodes_from_nested(cfg, s, all_labels)


def _add_edges_from_body(cfg: ProcessCFG, body: list[dict], next_label: str | None):
    """Add edges between labeled nodes based on statement semantics."""
    labels_in_body = [ls["label"] for ls in body if isinstance(ls, dict) and "label" in ls]

    for i, ls in enumerate(body):
        if not isinstance(ls, dict) or "label" not in ls:
            continue
        label = ls["label"]
        # The fall-through target is the next label in this body, or next_label
        fall_through = labels_in_body[i + 1] if i + 1 < len(labels_in_body) else next_label
        _add_edges_for_stmts(cfg, label, ls.get("stmts", []), fall_through)


def _find_terminal_goto(stmts: list[dict]) -> str | None:
    """If the statement list ends with a goto, return its target."""
    for s in reversed(stmts):
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")
        if t == "goto":
            return s.get("to")
        # Assignments, asserts, etc. are not terminal control flow
        if t in ("assignment", "assert", "print", "skip", "await", "when", "with"):
            continue
        break
    return None


def _collect_gotos_from_stmts(stmts: list) -> set[str]:
    """Recursively collect all goto targets from a list of (unlabeled) statements."""
    targets: set[str] = set()
    for s in stmts:
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")
        if t == "goto":
            target = s.get("to", "")
            if target:
                targets.add(target)
        elif t == "if":
            targets.update(_collect_gotos_from_stmts(s.get("then", [])))
            targets.update(_collect_gotos_from_stmts(s.get("else", [])))
        elif t == "either":
            for clause in s.get("ors", []):
                if isinstance(clause, list):
                    targets.update(_collect_gotos_from_stmts(clause))
    return targets


def _all_paths_end_in_goto_or_return(stmts: list) -> bool:
    """Check whether ALL control-flow paths through *stmts* end with goto/return.

    Returns True only when every reachable path terminates, meaning no
    fall-through to the next label is possible.
    """
    if not stmts:
        return False
    # Walk backwards to find the last control-flow-relevant statement
    for s in reversed(stmts):
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")
        if t == "goto" or t == "return" or t == "callReturn":
            return True
        if t == "if":
            return (_all_paths_end_in_goto_or_return(s.get("then", []))
                    and _all_paths_end_in_goto_or_return(s.get("else", [])))
        if t == "either":
            return all(
                _all_paths_end_in_goto_or_return(clause)
                for clause in s.get("ors", [])
                if isinstance(clause, list)
            )
        # Non-control-flow statements — keep scanning backwards
        if t in ("assignment", "assert", "print", "skip", "await", "when", "with"):
            continue
        break
    return False


def _add_edges_for_stmts(cfg: ProcessCFG, label: str, stmts: list[dict], fall_through: str | None):
    """Analyze statements in a labeled block and add outgoing edges."""
    node = cfg.nodes.get(label)
    if not node:
        return

    has_goto = False
    has_return = False

    for s in stmts:
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")

        if t == "goto":
            target = s.get("to", "")
            if target:
                node.outgoing.append(CFGEdge(source=label, target=target, kind="goto"))
                has_goto = True

        elif t == "return" or t == "callReturn":
            has_return = True

        elif t == "call" or t == "callGoto":
            # call: note the call but control flows to next label
            pass

        elif t == "while":
            guard = _expr_to_str(s.get("test", "?"))
            # labDo: labels inside the while loop body
            lab_do = s.get("labDo", [])
            unlab_do = s.get("unlabDo", [])

            if lab_do:
                # Labeled while body — guard-true edge to first labeled block
                first_lab = None
                for ld in lab_do:
                    if isinstance(ld, dict) and "label" in ld:
                        first_lab = ld["label"]
                        break
                if first_lab:
                    node.outgoing.append(CFGEdge(source=label, target=first_lab,
                                                 guard=guard, kind="loop"))
                # Add edges within the labDo body (back to loop head)
                _add_edges_from_labeled_list(cfg, lab_do, next_label=label)
            elif unlab_do:
                # Unlabeled while body — stays in same label, no new edges
                pass

            # guard-false edge: falls through to next label
            if fall_through:
                node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                             guard=f"¬({guard})", kind="branch"))
            continue  # while handled

        elif t == "if":
            # Unlabeled if — check branches for gotos and create guarded edges
            guard = _expr_to_str(s.get("test", "?"))
            then_stmts = s.get("then", []) if isinstance(s.get("then"), list) else []
            else_stmts = s.get("else", []) if isinstance(s.get("else"), list) else []
            then_gotos = _collect_gotos_from_stmts(then_stmts)
            else_gotos = _collect_gotos_from_stmts(else_stmts)

            if then_gotos or else_gotos:
                # Create guarded branch edges (like labelIf)
                if then_gotos:
                    for target in sorted(then_gotos):
                        node.outgoing.append(CFGEdge(source=label, target=target,
                                                     guard=guard, kind="branch"))
                elif fall_through:
                    node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                                 guard=guard, kind="branch"))

                neg_guard = f"¬({guard})"
                if else_gotos:
                    for target in sorted(else_gotos):
                        node.outgoing.append(CFGEdge(source=label, target=target,
                                                     guard=neg_guard, kind="branch"))
                elif fall_through:
                    node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                                 guard=neg_guard, kind="branch"))

                has_goto = True  # both branches handled via branch edges
                continue

        elif t == "labelIf":
            guard = _expr_to_str(s.get("test", "?"))
            then_labs = [x for x in s.get("then", []) if isinstance(x, dict) and "label" in x]
            else_labs = [x for x in s.get("else", []) if isinstance(x, dict) and "label" in x]

            if then_labs:
                node.outgoing.append(CFGEdge(source=label, target=then_labs[0]["label"],
                                             guard=guard, kind="branch"))
                _add_edges_from_labeled_list(cfg, then_labs, next_label=fall_through)
            elif fall_through:
                node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                             guard=guard, kind="branch"))

            if else_labs:
                node.outgoing.append(CFGEdge(source=label, target=else_labs[0]["label"],
                                             guard=f"¬({guard})", kind="branch"))
                _add_edges_from_labeled_list(cfg, else_labs, next_label=fall_through)
            elif fall_through:
                node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                             guard=f"¬({guard})", kind="branch"))
            has_goto = True  # suppress default fall-through
            continue

        elif t in ("either", "labelEither"):
            clauses = s.get("ors", [])
            for ci, clause in enumerate(clauses):
                if not isinstance(clause, list):
                    continue
                clause_labs = [x for x in clause if isinstance(x, dict) and "label" in x]
                if clause_labs:
                    node.outgoing.append(CFGEdge(source=label, target=clause_labs[0]["label"],
                                                 guard=f"branch {ci+1}", kind="branch"))
                    _add_edges_from_labeled_list(cfg, clause_labs, next_label=fall_through)
                elif fall_through:
                    node.outgoing.append(CFGEdge(source=label, target=fall_through,
                                                 guard=f"branch {ci+1}", kind="branch"))
            has_goto = True  # suppress default fall-through
            continue

    # Default fall-through if no explicit goto/return
    if not has_goto and not has_return and fall_through:
        node.outgoing.append(CFGEdge(source=label, target=fall_through, kind="sequential"))


def _add_edges_from_labeled_list(cfg: ProcessCFG, labeled_list: list[dict],
                                  next_label: str | None):
    """Add edges for a list of labeled statement blocks (e.g. labDo, then)."""
    labs = [x for x in labeled_list if isinstance(x, dict) and "label" in x]
    for i, ls in enumerate(labs):
        succ = labs[i + 1]["label"] if i + 1 < len(labs) else next_label
        _add_edges_for_stmts(cfg, ls["label"], ls.get("stmts", []), succ)


def build_cfg(ast: dict) -> list[ProcessCFG]:
    """Build one ProcessCFG per process from a parsed AST.

    For uniprocess algorithms, returns a single-element list.
    For multiprocess algorithms, returns one ProcessCFG per process.
    """
    ast_type = ast.get("type", "")
    cfgs: list[ProcessCFG] = []

    if ast_type == "uniprocess":
        body = ast.get("body", [])
        decls = ast.get("decls", [])
        cfg = _build_process_cfg(ast.get("name", "Main"), body, decls)
        cfgs.append(cfg)
    elif ast_type == "multiprocess":
        for proc in ast.get("procs", []):
            name = proc.get("name", "?")
            body = proc.get("body", [])
            decls = proc.get("decls", [])
            cfg = _build_process_cfg(name, body, decls)
            cfgs.append(cfg)
    else:
        raise ValueError(f"Unknown AST type: {ast_type!r}")

    # Also handle procedures (prcds) — build sub-CFGs
    for prcd in ast.get("prcds", []):
        name = f"procedure {prcd.get('name', '?')}"
        body = prcd.get("body", [])
        decls = prcd.get("decls", [])
        cfg = _build_process_cfg(name, body, decls)
        cfgs.append(cfg)

    return cfgs


# ═══════════════════════════════════════════════════════════════════════
#  Section 3 — Activity Diagram Generator (New Syntax)
# ═══════════════════════════════════════════════════════════════════════

def _filter_stmts(stmts: list[dict], filter_vars: set[str]) -> list[dict]:
    """Filter out assignments to excluded variables (e.g. instrumentation)."""
    result = []
    for s in stmts:
        if not isinstance(s, dict):
            continue
        t = s.get("type", "")
        if t == "assignment":
            ass = s.get("ass", [])
            filtered = [a for a in ass
                        if not (isinstance(a, dict)
                                and isinstance(a.get("lhs"), dict)
                                and a["lhs"].get("var") in filter_vars)]
            if not filtered:
                continue  # all assignments were to filtered vars
            result.append({**s, "ass": filtered})
        elif t == "if":
            then_s = s.get("then", [])
            else_s = s.get("else", [])
            result.append({**s,
                           "then": _filter_stmts(then_s, filter_vars) if isinstance(then_s, list) else then_s,
                           "else": _filter_stmts(else_s, filter_vars) if isinstance(else_s, list) else else_s})
        else:
            result.append(s)
    return result


def _esc(text: str) -> str:
    """Escape text for PlantUML activity diagrams."""
    return text.replace("<", "~<").replace(">", "~>").replace("|", "~|")


def cfg_to_activity_puml(
    cfgs: list[ProcessCFG],
    ast: dict,
    *,
    highlight_edges: set[str] | None = None,
    filter_vars: set[str] | None = None,
) -> str:
    """Generate a PlantUML activity diagram (new syntax) from process CFGs."""
    lines: list[str] = []
    lines.append("@startuml")
    title = ast.get("name", "PlusCal Model")
    lines.append(f'title {_esc(title)} — Activity Diagram')
    lines.append("")

    multiprocess = len(cfgs) > 1 or ast.get("type") == "multiprocess"

    # Global variable declarations as a note
    global_decls = ast.get("decls", [])
    if global_decls:
        lines.append("floating note left")
        lines.append("  **Variables**")
        for d in global_decls:
            if isinstance(d, dict):
                var = d.get("var", "?")
                if filter_vars and var in filter_vars:
                    continue
                op = d.get("eqOrIn", "=")
                val = _expr_to_str(d.get("val", ""))
                lines.append(f"  {_esc(var)} {_esc(op)} {_esc(val)}")
        lines.append("end note")
        lines.append("")

    for cfg in cfgs:
        if multiprocess:
            lines.append(f"|{_esc(cfg.name)}|")

        if not cfg.nodes:
            lines.append(":empty;")
            continue

        lines.append("start")

        # Walk the CFG in label order, emitting activity constructs
        visited: set[str] = set()
        _emit_activity_node(cfg, cfg.entry, lines, visited, highlight_edges,
                            indent=0, filter_vars=filter_vars)

        lines.append("stop")
        lines.append("")

    lines.append("@enduml")
    return "\n".join(lines)


def _emit_activity_node(cfg: ProcessCFG, label: str, lines: list[str],
                         visited: set[str], highlight_edges: set[str] | None,
                         indent: int, filter_vars: set[str] | None = None):
    """Recursively emit PlantUML activity syntax for a CFG node."""
    if label in visited or label not in cfg.nodes:
        # Back-edge or unknown label — emit a reference
        lines.append(f":{_esc(label)};")
        return

    visited.add(label)
    node = cfg.nodes[label]

    # Emit the label as an activity
    display_stmts = node.stmts
    if filter_vars:
        display_stmts = _filter_stmts(node.stmts, filter_vars)
    stmt_lines = [_stmt_summary(s) for s in display_stmts
                  if s.get("type") not in ("while", "labelIf", "labelEither", "either")]
    stmt_lines = [s for s in stmt_lines if s]  # remove empty summaries
    if stmt_lines:
        detail = "\\n".join(_esc(s) for s in stmt_lines[:6])
        lines.append(f':{_esc(label)}\\n{detail};')
    else:
        lines.append(f":{_esc(label)};")

    # Analyze outgoing edges
    edges = node.outgoing
    if not edges:
        return  # terminal node

    # Classify the edges
    loop_edges = [e for e in edges if e.kind == "loop"]
    branch_edges = [e for e in edges if e.kind == "branch"]
    goto_edges = [e for e in edges if e.kind == "goto"]
    seq_edges = [e for e in edges if e.kind == "sequential"]

    # While loop pattern: one loop edge + one branch edge (guard false)
    if loop_edges and len(loop_edges) == 1:
        loop_e = loop_edges[0]
        exit_edges = [e for e in edges if e != loop_e]
        guard = loop_e.guard or "?"
        lines.append(f"while ({_esc(guard)}) is (yes)")
        _emit_activity_node(cfg, loop_e.target, lines, visited, highlight_edges, indent + 1, filter_vars)
        lines.append("endwhile (no)")
        # Continue with the exit target
        for ex in exit_edges:
            _emit_activity_node(cfg, ex.target, lines, visited, highlight_edges, indent, filter_vars)
        return

    # If/else pattern: exactly 2 branch edges
    if len(branch_edges) == 2 and not loop_edges and not seq_edges and not goto_edges:
        e1, e2 = branch_edges
        guard = e1.guard or "?"
        lines.append(f"if ({_esc(guard)}) then (yes)")
        _emit_activity_node(cfg, e1.target, lines, visited, highlight_edges, indent + 1, filter_vars)
        lines.append("else (no)")
        _emit_activity_node(cfg, e2.target, lines, visited, highlight_edges, indent + 1, filter_vars)
        lines.append("endif")
        return

    # Either/or pattern: 3+ branch edges
    if len(branch_edges) >= 3 and not loop_edges and not seq_edges:
        lines.append("fork")
        for i, e in enumerate(branch_edges):
            if i > 0:
                lines.append("fork again")
            _emit_activity_node(cfg, e.target, lines, visited, highlight_edges, indent + 1, filter_vars)
        lines.append("end fork")
        return

    # Goto: single explicit goto
    if len(goto_edges) == 1 and not branch_edges and not seq_edges and not loop_edges:
        e = goto_edges[0]
        _emit_activity_node(cfg, e.target, lines, visited, highlight_edges, indent, filter_vars)
        return

    # Sequential: single fall-through
    if len(seq_edges) == 1 and not branch_edges and not goto_edges and not loop_edges:
        _emit_activity_node(cfg, seq_edges[0].target, lines, visited, highlight_edges, indent, filter_vars)
        return

    # Mixed / fallback: emit edges as a fork
    if len(edges) > 1:
        lines.append("fork")
        for i, e in enumerate(edges):
            if i > 0:
                lines.append("fork again")
            _emit_activity_node(cfg, e.target, lines, visited, highlight_edges, indent + 1, filter_vars)
        lines.append("end fork")
    elif edges:
        _emit_activity_node(cfg, edges[0].target, lines, visited, highlight_edges, indent, filter_vars)


# ═══════════════════════════════════════════════════════════════════════
#  Section 4 — State Diagram Generator
# ═══════════════════════════════════════════════════════════════════════

def cfg_to_state_puml(cfgs: list[ProcessCFG], ast: dict) -> str:
    """Generate a PlantUML state diagram from process CFGs."""
    lines: list[str] = []
    lines.append("@startuml")
    title = ast.get("name", "PlusCal Model")
    lines.append(f'title {title} — State Diagram')
    lines.append("hide empty description")
    lines.append("")

    multiprocess = len(cfgs) > 1 or ast.get("type") == "multiprocess"

    for cfg in cfgs:
        indent = ""
        safe_name = re.sub(r'\W+', '_', cfg.name)
        if multiprocess:
            lines.append(f'state "{cfg.name}" as {safe_name} {{')
            indent = "  "

        # Declare all states
        for label in cfg.nodes:
            safe_label = f"{safe_name}_{label}"
            lines.append(f'{indent}state "{label}" as {safe_label}')

        lines.append("")

        # Entry edge
        if cfg.entry and cfg.entry in cfg.nodes:
            safe_entry = f"{safe_name}_{cfg.entry}"
            lines.append(f"{indent}[*] --> {safe_entry}")

        # Transition edges
        terminal_labels = set(cfg.nodes.keys())
        for label, node in cfg.nodes.items():
            safe_src = f"{safe_name}_{label}"
            for edge in node.outgoing:
                safe_tgt = f"{safe_name}_{edge.target}"
                if edge.guard:
                    lines.append(f"{indent}{safe_src} --> {safe_tgt} : {edge.guard}")
                else:
                    lines.append(f"{indent}{safe_src} --> {safe_tgt}")
                terminal_labels.discard(label)

        # Terminal states
        for label in terminal_labels:
            if label in cfg.nodes:
                safe_label = f"{safe_name}_{label}"
                lines.append(f"{indent}{safe_label} --> [*]")

        if multiprocess:
            lines.append("}")
        lines.append("")

    lines.append("@enduml")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
#  Section 5 — CLI Interface
# ═══════════════════════════════════════════════════════════════════════

def write_output(text: str, kind: str, args):
    """Write diagram text to file or stdout."""
    if args.output_dir:
        name = args.name or "diagram"
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}_{kind}.puml"
        path.write_text(text, encoding="utf-8")
        print(f"  Wrote {path}", file=sys.stderr)
    else:
        print(f"--- {kind} ---")
        print(text)
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate PlantUML diagrams from PlusCal AST.tla (read from stdin)."
    )
    parser.add_argument("--activity", action="store_true",
                        help="Generate activity diagram only")
    parser.add_argument("--state", action="store_true",
                        help="Generate state diagram only")
    parser.add_argument("--output-dir", metavar="DIR",
                        help="Write .puml files to DIR (default: stdout)")
    parser.add_argument("--name", metavar="NAME",
                        help="Base filename for output (default: from AST module name)")
    parser.add_argument("--filter-vars", metavar="VARS", default="trace",
                        help="Comma-separated variables to exclude from diagrams (default: trace)")
    args = parser.parse_args()

    ast_text = sys.stdin.read()
    if not ast_text.strip():
        sys.exit("ERROR: No input on stdin. Pipe an AST.tla file.")

    ast = parse_ast_tla(ast_text)
    cfgs = build_cfg(ast)

    # Default name from AST
    if not args.name:
        args.name = ast.get("name", "diagram")

    filter_vars = set(v.strip() for v in args.filter_vars.split(",") if v.strip()) if args.filter_vars else set()

    do_both = not args.activity and not args.state

    if args.activity or do_both:
        activity = cfg_to_activity_puml(cfgs, ast, filter_vars=filter_vars)
        write_output(activity, "activity", args)

    if args.state or do_both:
        state = cfg_to_state_puml(cfgs, ast)
        write_output(state, "state", args)


if __name__ == "__main__":
    main()
