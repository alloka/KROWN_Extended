#!/usr/bin/env python3
"""
generate_reverse_datalog.py

Prototype compiler from forward Datalog program D to reverse Datalog program D'.

Input:
  - A generated forward Datalog file D

Output:
  - A reverse Datalog file D' that:
      * declares RDF triples as input
      * uses generic external helper functors
      * reconstructs source predicates from RDF triples where possible

Supported fragment:
  - Source declarations like: .decl Student_lt0(id:symbol, name:symbol)
  - Subject rules of the form: Subject...(...) :- Source(...)
  - Predicate rules with constant IRIs
  - Object rules:
      * plain literals: "x"
      * typed literals: "x"^^<datatype>
      * simple IRI templates
  - triple(...) rules

Important notes:
  - This script does NOT execute the reverse program.
  - It generates D' only.
  - It assumes helper functors exist in the Soufflé environment, e.g.:
      @removePrefix, @removeSuffix, @beforeFirst, @afterFirst,
      @decodeIRI, @stripLiteralQuotes, @stripTypedLiteral
  - It treats auxiliary trailing variables in Subject/Predicate/Object predicates
    as alignment variables, not as automatically recoverable source columns.
"""

import re
import sys
import argparse
import json
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Union, Set


# =============================================================================
# Template AST
# =============================================================================

@dataclass
class Const:
    value: str


@dataclass
class Var:
    name: str
    encoded: bool = False  # True when this came from @toIRI(...)


TemplatePart = Union[Const, Var]


def force_decode_vars(parts: List[TemplatePart]) -> List[TemplatePart]:
    """
    Return a copy of template parts where all variables are marked encoded=True.

    This is used for IRI-template parsing: values embedded in IRIs may be
    percent-encoded even when the forward rule does not explicitly wrap columns
    in @toIRI(...).
    """
    out: List[TemplatePart] = []
    for p in parts:
        if isinstance(p, Var):
            out.append(Var(name=p.name, encoded=True))
        else:
            out.append(Const(value=p.value))
    return out


# =============================================================================
# Forward program structures
# =============================================================================

@dataclass
class SourceDecl:
    name: str
    columns: List[str]


@dataclass
class SubjectRule:
    pred_name: str
    source_name: str
    source_args: List[str]
    term_expr: str
    aux_args: List[str]
    parts: List[TemplatePart] = field(default_factory=list)


@dataclass
class PredicateRule:
    pred_name: str
    source_name: str
    source_args: List[str]
    term_expr: str
    aux_args: List[str]
    constant_iri: Optional[str] = None
    parts: List[TemplatePart] = field(default_factory=list)


@dataclass
class ObjectRule:
    pred_name: str
    source_name: str
    source_args: List[str]
    term_expr: str
    aux_args: List[str]
    object_kind: str = "unknown"   # plain_literal | typed_literal | iri_template | constant_iri | unknown
    datatype: Optional[str] = None
    parts: List[TemplatePart] = field(default_factory=list)
    constant_iri: Optional[str] = None


@dataclass
class GraphRule:
    pred_name: str
    source_name: str
    source_args: List[str]
    term_expr: str
    aux_args: List[str]
    graph_kind: str = "unknown"   # iri_template | constant_iri | unknown
    parts: List[TemplatePart] = field(default_factory=list)
    constant_iri: Optional[str] = None


@dataclass
class TriplePattern:
    head_kind: str
    subject_pred: Optional[str]
    object_subject_pred: Optional[str]
    predicate_pred: Optional[str]
    object_pred: Optional[str]
    graph_pred: Optional[str]
    predicate_constant: Optional[str]
    object_constant: Optional[str]
    head_graph_term: Optional[str]
    raw_rule: str


# =============================================================================
# Utility parsing helpers
# =============================================================================

def split_top_level_args(s: str) -> List[str]:
    """
    Split a comma-separated argument list while respecting nested parentheses
    and quoted strings.
    """
    parts = []
    buf = []
    depth = 0
    in_string = False
    escaped = False

    for ch in s:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            buf.append(ch)
        elif ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)

    if buf:
        parts.append(''.join(buf).strip())

    return parts


def parse_atom(atom: str) -> Tuple[str, List[str]]:
    """
    Parse an atom like:
      Predicate01_lt0(x, y)
    into:
      ("Predicate01_lt0", ["x", "y"])
    """
    m = re.match(r'^([A-Za-z0-9_]+)\((.*)\)$', atom.strip())
    if not m:
        raise ValueError(f"Cannot parse atom: {atom}")
    pred = m.group(1)
    args = split_top_level_args(m.group(2))
    return pred, args


def parse_rule(line: str) -> Optional[Tuple[str, str]]:
    """
    Parse a Datalog rule line:
      head :- body.
    Returns (head, body) without trailing period.
    """
    if ':-' not in line:
        return None
    line = line.strip()
    if not line.endswith('.'):
        return None
    line = line[:-1]
    head, body = line.split(':-', 1)
    return head.strip(), body.strip()


def is_string_literal(expr: str) -> bool:
    return len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"'


def unquote(expr: str) -> str:
    if is_string_literal(expr):
        inner = expr[1:-1]

        # Souffle string literals commonly escape quotes and backslashes.
        # Decode the simple escape forms used in generated templates.
        out = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == '\\' and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt == '"':
                    out.append('"')
                    i += 2
                    continue
                if nxt == '\\':
                    out.append('\\')
                    i += 2
                    continue
            out.append(ch)
            i += 1

        return ''.join(out)
    return expr


def is_identifier(expr: str) -> bool:
    return re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', expr) is not None


def escape_souffle_string(s: str) -> str:
    """
    Escape a Python string for inclusion as a Soufflé string literal.
    """
    return s.replace('\\', '\\\\').replace('"', '\\"')


def unique_preserve_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def constant_term_variants(term: str) -> List[str]:
    """
    Generate equivalent lexical variants for constant matching.

    For IRI constants like <http://x>, accept common encodings observed
    in generated triple facts: <...>, "...", and "<...>".
    """
    term = term.strip()

    # If term is already a quoted symbol constant, keep it as-is.
    if term.startswith('"') and term.endswith('"'):
        return [term]

    if term.startswith('<') and term.endswith('>'):
        inner = term[1:-1]
        variants = [
            f'"{escape_souffle_string(term)}"',
            f'"{escape_souffle_string(inner)}"',
            f'"\\"{escape_souffle_string(inner)}\\""',
            f'"\\"{escape_souffle_string(term)}\\""',
        ]
        return unique_preserve_order(variants)

    # Fallback: treat as plain symbol text and quote it.
    return [f'"{escape_souffle_string(term)}"']


def strip_outer_quotes(term: str) -> str:
    t = term.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return unquote(t)
    return t


def is_rdf_type_predicate(term: str) -> bool:
    """
    Return True when a constant term denotes rdf:type.
    """
    t = strip_outer_quotes(term).strip()
    if t.startswith('<') and t.endswith('>'):
        t = t[1:-1]
    return t == 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'


# =============================================================================
# cat(...) flattening and template extraction
# =============================================================================

def flatten_expr(expr: str) -> List[TemplatePart]:
    """
    Flatten nested cat(...) and @toIRI(...) structures into a linear sequence
    of Const / Var parts.

    Examples:
      cat("<", cat("http://x/", id))
      -> [Const("<"), Const("http://x/"), Var("id", False)]

      @toIRI(name)
      -> [Var("name", True)]
    """
    expr = expr.strip()

    # cat(a,b)
    m = re.match(r'^cat\((.*)\)$', expr)
    if m:
        args = split_top_level_args(m.group(1))
        if len(args) == 2:
            return flatten_expr(args[0]) + flatten_expr(args[1])

    # @toIRI(x)
    m = re.match(r'^@toIRI\((.*)\)$', expr)
    if m:
        inner = m.group(1).strip()
        if is_identifier(inner):
            return [Var(inner, encoded=True)]

    # string constant
    if is_string_literal(expr):
        return [Const(unquote(expr))]

    # identifier
    if is_identifier(expr):
        return [Var(expr, encoded=False)]

    # fallback opaque piece
    return [Const(expr)]


def merge_adjacent_consts(parts: List[TemplatePart]) -> List[TemplatePart]:
    merged: List[TemplatePart] = []
    for p in parts:
        if isinstance(p, Const) and merged and isinstance(merged[-1], Const):
            merged[-1] = Const(merged[-1].value + p.value)
        else:
            merged.append(p)
    return merged


def source_columns_used_in_parts(parts: List[TemplatePart]) -> List[str]:
    seen = []
    for p in parts:
        if isinstance(p, Var) and p.name not in seen:
            seen.append(p.name)
    return seen


def parser_can_recover(parts: List[TemplatePart]) -> bool:
    """
    Return True if template parsing can safely recover at least one variable.

    Parsing is considered unsupported when variables are adjacent without
    separators (ambiguous split).
    """
    used = source_columns_used_in_parts(parts)
    if not used:
        return False
    return not has_adjacent_vars(parts)


def allow_ref_positional_fallback(col_name: str) -> bool:
    """
    Guard positional ref-object fallback for obviously derived/lexical targets.

    We only want positional fallback to fill unresolved join-like attributes,
    not semantic fields such as graph URIs or names.
    """
    n = col_name.lower()
    if n.endswith("uri"):
        return False
    if "graph" in n:
        return False
    if n in {"name", "ename", "dname", "fname", "lname", "description"}:
        return False
    return True


def extract_constant_iri(term_expr: str) -> Optional[str]:
    """
    If a term expression is a fully constant IRI template like:
      cat("<",cat("http://example.com/x",">"))
    return:
      <http://example.com/x>
    otherwise None.
    """
    parts = merge_adjacent_consts(flatten_expr(term_expr))
    text = ''.join(p.value if isinstance(p, Const) else '{VAR}' for p in parts)
    if '{VAR}' in text:
        return None
    if text.startswith('<') and text.endswith('>'):
        return text
    return None


def classify_object(parts: List[TemplatePart]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Classify object template.

    Returns:
      (kind, datatype, constant_iri)
    """
    parts = merge_adjacent_consts(parts)
    text = ''.join(p.value if isinstance(p, Const) else '{VAR}' for p in parts)

    # Constant IRI
    if '{VAR}' not in text and text.startswith('<') and text.endswith('>'):
        return "constant_iri", None, text

    # Typed literal: "{VAR}"^^<datatype>
    m = re.match(r'^"\{VAR\}"\^\^<([^>]+)>$', text)
    if m:
        return "typed_literal", m.group(1), None

    # Plain literal: "{VAR}"
    if text == '"{VAR}"':
        return "plain_literal", None, None

    # IRI template with variables
    if text.startswith('<') and text.endswith('>'):
        return "iri_template", None, None

    return "unknown", None, None


# =============================================================================
# Forward D parser
# =============================================================================

def extract_decl(line: str) -> Optional[SourceDecl]:
    """
    Extract source declarations of the form:
      .decl Student_lt0(id:symbol, name:symbol)

    Excludes:
      - triple / quadruple
      - Subject... / Predicate... / Object...
      - eval_...
    """
    m = re.match(r'^\.decl\s+([A-Za-z0-9_]+)\((.*)\)\s*$', line.strip())
    if not m:
        return None

    pred = m.group(1)
    args = split_top_level_args(m.group(2))

    if pred in ("triple", "quadruple"):
        return None

    if pred.startswith("Subject") or pred.startswith("Predicate") or pred.startswith("Object"):
        return None

    if pred.startswith("eval_"):
        return None

    # heuristic: source relations end in _ltN
    if not re.match(r'^[A-Za-z0-9_]+_lt\d+$', pred):
        return None

    cols = []
    for a in args:
        if ':' in a:
            cols.append(a.split(':', 1)[0].strip())
        else:
            cols.append(a.strip())

    return SourceDecl(pred, cols)


def parse_datalog(text: str):
    """
    Parse the forward Datalog program D and extract:
      - source declarations
      - subject rules
      - predicate rules
      - object rules
      - graph rules
      - triple production patterns
    """
    source_decls: Dict[str, SourceDecl] = {}
    subject_rules: Dict[str, SubjectRule] = {}
    predicate_rules: Dict[str, PredicateRule] = {}
    object_rules: Dict[str, ObjectRule] = {}
    graph_rules: Dict[str, GraphRule] = {}
    triple_patterns: List[TriplePattern] = []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("//")]

    # pass 1: declarations
    for line in lines:
        d = extract_decl(line)
        if d:
            source_decls[d.name] = d

    # pass 2: rules
    for line in lines:
        pr = parse_rule(line)
        if not pr:
            continue

        head, body = pr
        head_pred, head_args = parse_atom(head)
        body_atoms = split_top_level_args(body)

        # Subject / Predicate / Object rules have a source atom as last body atom
        if body_atoms:
            try:
                last_pred, last_args = parse_atom(body_atoms[-1])
            except Exception:
                last_pred, last_args = None, None

            if last_pred in source_decls:
                if head_pred.startswith("Subject"):
                    subject_rules[head_pred] = SubjectRule(
                        pred_name=head_pred,
                        source_name=last_pred,
                        source_args=last_args,
                        term_expr=head_args[0],
                        aux_args=head_args[1:],
                        parts=merge_adjacent_consts(flatten_expr(head_args[0])),
                    )
                    continue

                if head_pred.startswith("Predicate"):
                    predicate_rules[head_pred] = PredicateRule(
                        pred_name=head_pred,
                        source_name=last_pred,
                        source_args=last_args,
                        term_expr=head_args[0],
                        aux_args=head_args[1:],
                        constant_iri=extract_constant_iri(head_args[0]),
                        parts=merge_adjacent_consts(flatten_expr(head_args[0])),
                    )
                    continue

                if head_pred.startswith("Object"):
                    parts = merge_adjacent_consts(flatten_expr(head_args[0]))
                    kind, datatype, constant_iri = classify_object(parts)
                    object_rules[head_pred] = ObjectRule(
                        pred_name=head_pred,
                        source_name=last_pred,
                        source_args=last_args,
                        term_expr=head_args[0],
                        aux_args=head_args[1:],
                        object_kind=kind,
                        datatype=datatype,
                        parts=parts,
                        constant_iri=constant_iri,
                    )
                    continue

                if head_pred.startswith("Graph"):
                    parts = merge_adjacent_consts(flatten_expr(head_args[0]))
                    kind, _, constant_iri = classify_object(parts)
                    graph_rules[head_pred] = GraphRule(
                        pred_name=head_pred,
                        source_name=last_pred,
                        source_args=last_args,
                        term_expr=head_args[0],
                        aux_args=head_args[1:],
                        graph_kind=kind,
                        parts=parts,
                        constant_iri=constant_iri,
                    )
                    continue

        # triple/quadruple rules
        if head_pred in ("triple", "quadruple"):
            subj_pred = None
            obj_subj_pred = None
            pred_pred = None
            obj_pred = None
            graph_pred = None
            pred_const = None
            obj_const = None

            head_s = head_args[0] if len(head_args) > 0 else None
            head_p = head_args[1] if len(head_args) > 1 else None
            head_o = head_args[2] if len(head_args) > 2 else None
            head_g = head_args[3] if head_pred == "quadruple" and len(head_args) > 3 else None

            for atom in body_atoms:
                atom = atom.strip()
                try:
                    bp, bargs = parse_atom(atom)
                except Exception:
                    continue

                if bp.startswith("Subject"):
                    # Prefer role-aware mapping against triple head variables.
                    if bargs and head_s is not None and bargs[0] == head_s:
                        subj_pred = bp
                    elif bargs and head_o is not None and bargs[0] == head_o:
                        obj_subj_pred = bp
                    elif subj_pred is None:
                        subj_pred = bp
                elif bp.startswith("Predicate"):
                    if bargs and head_p is not None and bargs[0] == head_p:
                        pred_pred = bp
                    elif pred_pred is None:
                        pred_pred = bp
                elif bp.startswith("Object"):
                    if bargs and head_o is not None and bargs[0] == head_o:
                        obj_pred = bp
                    elif obj_pred is None:
                        obj_pred = bp
                elif bp.startswith("Graph"):
                    if bargs and head_g is not None and bargs[0] == head_g:
                        graph_pred = bp
                    elif graph_pred is None:
                        graph_pred = bp

            if len(head_args) >= 3:
                if is_string_literal(head_args[1]) or (head_args[1].startswith("<") and head_args[1].endswith(">")):
                    pred_const = head_args[1]
                if is_string_literal(head_args[2]) or (head_args[2].startswith("<") and head_args[2].endswith(">")):
                    obj_const = head_args[2]

            triple_patterns.append(
                TriplePattern(
                    head_kind=head_pred,
                    subject_pred=subj_pred,
                    object_subject_pred=obj_subj_pred,
                    predicate_pred=pred_pred,
                    object_pred=obj_pred,
                    graph_pred=graph_pred,
                    predicate_constant=pred_const,
                    object_constant=obj_const,
                    head_graph_term=head_g,
                    raw_rule=line,
                )
            )

    return source_decls, subject_rules, predicate_rules, object_rules, graph_rules, triple_patterns


# =============================================================================
# Reverse generation helpers
# =============================================================================

def find_source_for_triple_pattern(
    tp: TriplePattern,
    subject_rules: Dict[str, SubjectRule],
    predicate_rules: Dict[str, PredicateRule],
    object_rules: Dict[str, ObjectRule],
    graph_rules: Optional[Dict[str, GraphRule]] = None,
) -> Optional[str]:
    """
    Infer the source predicate associated with a triple production rule.
    """
    srcs = set()

    if tp.subject_pred and tp.subject_pred in subject_rules:
        srcs.add(subject_rules[tp.subject_pred].source_name)
    if tp.predicate_pred and tp.predicate_pred in predicate_rules:
        srcs.add(predicate_rules[tp.predicate_pred].source_name)
    if tp.object_pred and tp.object_pred in object_rules:
        srcs.add(object_rules[tp.object_pred].source_name)
    if tp.graph_pred and graph_rules is not None and tp.graph_pred in graph_rules:
        srcs.add(graph_rules[tp.graph_pred].source_name)

    if len(srcs) == 1:
        return next(iter(srcs))
    return None


def has_adjacent_vars(parts: List[TemplatePart]) -> bool:
    for i in range(len(parts) - 1):
        if isinstance(parts[i], Var) and isinstance(parts[i + 1], Var):
            return True
    return False


def build_template_parse_rule(parse_name: str, input_var: str, parts: List[TemplatePart]) -> List[str]:
    """
    Build a parser predicate for a flattened template:
      const0 var1 const1 var2 const2 ... varN constN

    using helper functors:
      @removePrefix
      @beforeFirst
      @afterFirst
      @decodeIRI

    Returns a list of Datalog lines.
    """
    lines = []
    parts = merge_adjacent_consts(parts)
    used_cols = source_columns_used_in_parts(parts)

    if not used_cols:
        lines.append(f'// {parse_name}: no recoverable variables in template')
        lines.append('')
        return lines

    lines.append(f'.decl {parse_name}({input_var}:symbol, {", ".join(c + ":symbol" for c in used_cols)})')

    if has_adjacent_vars(parts):
        lines.append(f'// Unsupported ambiguous template in {parse_name}: adjacent variables without separator')
        lines.append('')
        return lines

    # Convert into sequence of either Const or Var
    body_terms = []
    if input_var == 's':
        body_terms.append('InputSubject(s)')
    elif input_var == 'p':
        body_terms.append('InputPredicate(p)')
    elif input_var == 'o':
        body_terms.append('InputObject(o)')
    elif input_var == 'g':
        body_terms.append('InputGraph(g)')

    current = input_var
    temp_idx = 0
    i = 0

    # Optional leading constant
    if i < len(parts) and isinstance(parts[i], Const):
        lead = parts[i].value
        t = f't{temp_idx}'
        temp_idx += 1
        body_terms.append(f'{t} = @removePrefix({current}, "{escape_souffle_string(lead)}")')
        current = t
        i += 1

    # Parse each variable up to next constant
    while i < len(parts):
        if not isinstance(parts[i], Var):
            lines.append(f'// Unsupported parse state in {parse_name}')
            lines.append('')
            return lines

        var = parts[i]
        next_const = parts[i + 1].value if i + 1 < len(parts) and isinstance(parts[i + 1], Const) else None

        # Generic fallback for templates that encode two adjacent variables with
        # an empty separator, e.g. var1 + "" + var2. We recover both by taking
        # the combined segment and splitting once on a space.
        if next_const == "" and i + 2 < len(parts) and isinstance(parts[i + 2], Var):
            var2 = parts[i + 2]
            trailing_const = parts[i + 3].value if i + 3 < len(parts) and isinstance(parts[i + 3], Const) else None

            pair_segment = f'raw_pair_{var.name}_{var2.name}'
            if trailing_const is None:
                body_terms.append(f'{pair_segment} = {current}')
                i_advance = 3
            else:
                body_terms.append(f'{pair_segment} = @beforeFirst({current}, "{escape_souffle_string(trailing_const)}")')
                if i + 4 < len(parts):
                    t_after = f't{temp_idx}'
                    temp_idx += 1
                    body_terms.append(f'{t_after} = @afterFirst({current}, "{escape_souffle_string(trailing_const)}")')
                    current = t_after
                i_advance = 4

            raw1 = f'raw_{var.name}'
            raw2 = f'raw_{var2.name}'
            body_terms.append(f'{raw1} = @beforeFirst({pair_segment}, " ")')
            body_terms.append(f'{raw2} = @afterFirst({pair_segment}, " ")')

            if var.encoded:
                body_terms.append(f'{var.name} = @decodeIRI({raw1})')
            else:
                body_terms.append(f'{var.name} = {raw1}')

            if var2.encoded:
                body_terms.append(f'{var2.name} = @decodeIRI({raw2})')
            else:
                body_terms.append(f'{var2.name} = {raw2}')

            i += i_advance
            continue

        raw_name = f'raw_{var.name}'

        if next_const is None:
            body_terms.append(f'{raw_name} = {current}')
            i += 1
        else:
            body_terms.append(f'{raw_name} = @beforeFirst({current}, "{escape_souffle_string(next_const)}")')
            if i + 2 < len(parts):
                t_after = f't{temp_idx}'
                temp_idx += 1
                body_terms.append(f'{t_after} = @afterFirst({current}, "{escape_souffle_string(next_const)}")')
                current = t_after
            i += 2

        if var.encoded:
            body_terms.append(f'{var.name} = @decodeIRI({raw_name})')
        else:
            body_terms.append(f'{var.name} = {raw_name}')

    lines.append(f'{parse_name}({input_var}, {", ".join(used_cols)}) :- {", ".join(body_terms)}.')
    lines.append('')
    return lines


def build_subject_parse_rule(source: SourceDecl, subj: SubjectRule) -> List[str]:
    """
    Build Parse_Subject... helper predicate.
    """
    parse_name = f'Parse_{subj.pred_name}'
    lines = [f'// Subject parser for {subj.pred_name}', f'// Source: {source.name}', f'// Template: {subj.term_expr}']

    # Subject templates are IRI-based; decode extracted variables.
    lines.extend(build_template_parse_rule(parse_name, 's', force_decode_vars(subj.parts)))
    return lines


def build_predicate_parse_rule(source: SourceDecl, pred: PredicateRule) -> List[str]:
    """
    Build Parse_Predicate... helper predicate.
    """
    lines = [f'// Predicate parser for {pred.pred_name}', f'// Source: {source.name}', f'// Template: {pred.term_expr}']

    if pred.constant_iri is not None:
        used_cols = source_columns_used_in_parts(pred.parts)
        if used_cols:
            lines.append(f'.decl Parse_{pred.pred_name}(p:symbol, {", ".join(c + ":symbol" for c in used_cols)})')
        else:
            lines.append(f'.decl Parse_{pred.pred_name}(p:symbol)')
        lines.append(f'// No recoverable variables: constant predicate IRI {pred.constant_iri}')
        lines.append('')
        return lines

    # Predicate templates are IRI-based; decode extracted variables.
    lines.extend(build_template_parse_rule(f'Parse_{pred.pred_name}', 'p', force_decode_vars(pred.parts)))
    return lines


def build_object_parse_rule(source: SourceDecl, obj: ObjectRule) -> List[str]:
    """
    Build Parse_Object... helper predicate.
    """
    lines = [f'// Object parser for {obj.pred_name}', f'// Source: {source.name}', f'// Template: {obj.term_expr}']
    used_cols = source_columns_used_in_parts(obj.parts)

    if obj.object_kind == "constant_iri":
        if used_cols:
            lines.append(f'.decl Parse_{obj.pred_name}(o:symbol, {", ".join(c + ":symbol" for c in used_cols)})')
        else:
            lines.append(f'.decl Parse_{obj.pred_name}(o:symbol)')
        lines.append(f'// No recoverable variables: constant object IRI {obj.constant_iri}')
    else:
        # Generic path: parse any non-constant object template the same way as
        # subject/IRI templates, as long as separators make it unambiguous.
        parts = force_decode_vars(obj.parts) if obj.object_kind == "iri_template" else obj.parts
        lines.extend(build_template_parse_rule(f'Parse_{obj.pred_name}', 'o', parts))
        return lines

    lines.append('')
    return lines


def build_graph_parse_rule(source: SourceDecl, graph: GraphRule) -> List[str]:
    """
    Build Parse_Graph... helper predicate for graph terms.
    """
    lines = [f'// Graph parser for {graph.pred_name}', f'// Source: {source.name}', f'// Template: {graph.term_expr}']
    used_cols = source_columns_used_in_parts(graph.parts)

    if graph.graph_kind == "constant_iri":
        if used_cols:
            lines.append(f'.decl Parse_{graph.pred_name}(g:symbol, {", ".join(c + ":symbol" for c in used_cols)})')
        else:
            lines.append(f'.decl Parse_{graph.pred_name}(g:symbol)')
        lines.append(f'// No recoverable variables: constant graph IRI {graph.constant_iri}')
    else:
        parts = force_decode_vars(graph.parts) if graph.graph_kind == "iri_template" else graph.parts
        lines.extend(build_template_parse_rule(f'Parse_{graph.pred_name}', 'g', parts))
        return lines

    lines.append('')
    return lines


def classify_term_recoverability(parts: List[TemplatePart]) -> str:
    """Classify a template as full/partial/unsupported recoverability."""
    cols = source_columns_used_in_parts(parts)
    if not cols:
        return "full"
    if has_adjacent_vars(parts):
        return "unsupported"
    if parser_can_recover(parts):
        return "full"
    return "partial"


def compute_support_report(
    source_decls: Dict[str, SourceDecl],
    subject_rules: Dict[str, SubjectRule],
    predicate_rules: Dict[str, PredicateRule],
    object_rules: Dict[str, ObjectRule],
    graph_rules: Dict[str, GraphRule],
    triple_patterns: List[TriplePattern],
) -> Dict[str, object]:
    """Compute full/partial/unsupported status for each triple pattern."""
    summary = {
        "triple_patterns_total": len(triple_patterns),
        "full": 0,
        "partial": 0,
        "unsupported": 0,
    }
    details: List[Dict[str, object]] = []

    for idx, tp in enumerate(triple_patterns, start=1):
        src_name = find_source_for_triple_pattern(tp, subject_rules, predicate_rules, object_rules, graph_rules)
        if not src_name:
            summary["unsupported"] += 1
            details.append({
                "index": idx,
                "status": "unsupported",
                "source": None,
                "head_kind": tp.head_kind,
                "reason": "cannot infer unique source for triple pattern",
                "raw_rule": tp.raw_rule,
            })
            continue

        subj = subject_rules.get(tp.subject_pred) if tp.subject_pred else None
        pred = predicate_rules.get(tp.predicate_pred) if tp.predicate_pred else None
        obj = object_rules.get(tp.object_pred) if tp.object_pred else None

        if not subj:
            summary["unsupported"] += 1
            details.append({
                "index": idx,
                "status": "unsupported",
                "source": src_name,
                "head_kind": tp.head_kind,
                "reason": "missing subject mapping in triple body",
                "raw_rule": tp.raw_rule,
            })
            continue

        subj_status = classify_term_recoverability(subj.parts)
        pred_status = "full"
        obj_status = "full"
        reasons: List[str] = []

        if pred and pred.constant_iri is None:
            pred_status = classify_term_recoverability(pred.parts)
        if obj and obj.object_kind != "constant_iri":
            obj_status = classify_term_recoverability(obj.parts)

        states = [subj_status, pred_status, obj_status]
        if any(s == "unsupported" for s in states):
            status = "unsupported"
        elif any(s == "partial" for s in states):
            status = "partial"
        else:
            status = "full"

        if subj_status != "full":
            reasons.append(f"subject={subj_status}")
        if pred_status != "full":
            reasons.append(f"predicate={pred_status}")
        if obj_status != "full":
            reasons.append(f"object={obj_status}")

        summary[status] += 1
        details.append({
            "index": idx,
            "status": status,
            "source": src_name,
            "head_kind": tp.head_kind,
            "reason": ", ".join(reasons) if reasons else "all mapped terms recoverable",
            "raw_rule": tp.raw_rule,
        })

    by_source: Dict[str, Dict[str, int]] = {}
    for row in details:
        src = row["source"] or "<unknown>"
        by_source.setdefault(src, {"full": 0, "partial": 0, "unsupported": 0})
        by_source[src][row["status"]] += 1

    return {
        "summary": summary,
        "by_source": by_source,
        "details": details,
    }


# =============================================================================
# Reverse D' generation
# =============================================================================

def build_reverse_program(
    source_decls: Dict[str, SourceDecl],
    subject_rules: Dict[str, SubjectRule],
    predicate_rules: Dict[str, PredicateRule],
    object_rules: Dict[str, ObjectRule],
    graph_rules: Dict[str, GraphRule],
    triple_patterns: List[TriplePattern],
    provenance_enabled: bool = False,
    minimal_mode: bool = True,
    recovery_mode: str = "best-effort",
) -> str:
    lines: List[str] = []
    support_report = compute_support_report(
        source_decls,
        subject_rules,
        predicate_rules,
        object_rules,
        graph_rules,
        triple_patterns,
    )

    lines.append('// ==========================================')
    lines.append("// Reverse Datalog program D' generated from D")
    lines.append('// ==========================================')
    lines.append('')
    lines.append(f'// recovery_mode={recovery_mode}')
    lines.append('// Support Report')
    lines.append(
        f'// triple_patterns={support_report["summary"]["triple_patterns_total"]} '
        f'full={support_report["summary"]["full"]} '
        f'partial={support_report["summary"]["partial"]} '
        f'unsupported={support_report["summary"]["unsupported"]}'
    )
    for src_name in sorted(support_report["by_source"].keys()):
        row = support_report["by_source"][src_name]
        lines.append(
            f'// source={src_name} full={row["full"]} partial={row["partial"]} unsupported={row["unsupported"]}'
        )
    lines.append('')
    lines.append('// RDF input')
    lines.append('.decl triple(s:symbol, p:symbol, o:symbol)')
    lines.append('.input triple(filename="triple.csv", delimiter="\\t")')
    lines.append('.decl quadruple(s:symbol, p:symbol, o:symbol, g:symbol)')
    lines.append('.input quadruple(filename="quadruple.csv", delimiter="\\t")')
    lines.append('triple(s, p, o) :- quadruple(s, p, o, _).')
    lines.append('')

    lines.append('// Grounding domains derived from input triples')
    lines.append('.decl InputSubject(s:symbol)')
    lines.append('.decl InputPredicate(p:symbol)')
    lines.append('.decl InputObject(o:symbol)')
    lines.append('.decl InputGraph(g:symbol)')
    lines.append('InputSubject(s) :- triple(s, _, _).')
    lines.append('InputPredicate(p) :- triple(_, p, _).')
    lines.append('InputObject(o) :- triple(_, _, o).')
    lines.append('InputGraph(g) :- quadruple(_, _, _, g).')
    lines.append('')

    lines.append('// External functor declarations')
    lines.append('.functor removePrefix(x:symbol, p:symbol):symbol')
    lines.append('.functor removeSuffix(x:symbol, s:symbol):symbol')
    lines.append('.functor beforeFirst(x:symbol, d:symbol):symbol')
    lines.append('.functor afterFirst(x:symbol, d:symbol):symbol')
    lines.append('.functor decodeIRI(x:symbol):symbol')
    lines.append('.functor stripLiteralQuotes(x:symbol):symbol')
    lines.append('.functor stripTypedLiteral(x:symbol, dt:symbol):symbol')
    lines.append('')

    lines.append('// -----------------------------------------------------------------')
    lines.append('// External helper functors expected by this generated reverse code:')
    lines.append('//   @removePrefix(x, p)')
    lines.append('//   @removeSuffix(x, s)')
    lines.append('//   @beforeFirst(x, d)')
    lines.append('//   @afterFirst(x, d)')
    lines.append('//   @decodeIRI(x)')
    lines.append('//   @stripLiteralQuotes(x)')
    lines.append('//   @stripTypedLiteral(x, dt)')
    lines.append('// -----------------------------------------------------------------')
    lines.append('')

    if provenance_enabled:
        lines.append('// Optional provenance output')
        lines.append('.decl Prov_Evidence(source_rel:symbol, s:symbol, p:symbol, o:symbol)')
        lines.append('.decl Prov_Evidence_Quad(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol)')
        lines.append('.decl Prov_Column(source_rel:symbol, s:symbol, p:symbol, o:symbol, col:symbol, val:symbol, pos:number)')
        lines.append('.decl Prov_Mapping(source_rel:symbol, output_kind:symbol, source_rule:symbol, s:symbol, p:symbol, o:symbol)')
        lines.append('')
        lines.append('// User-facing provenance views (internal IDs hidden)')
        lines.append('.decl ExplainTriple(source_rel:symbol, s:symbol, p:symbol, o:symbol)')
        lines.append('.decl ExplainQuad(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol)')
        lines.append('.decl ExplainContributor(source_rel:symbol, s:symbol, p:symbol, o:symbol, col:symbol, val:symbol, pos:number)')
        lines.append('.decl ExplainRule(source_rel:symbol, output_kind:symbol, source_rule:symbol, s:symbol, p:symbol, o:symbol)')
        lines.append('')

    # Emit parsers
    for subj in subject_rules.values():
        src = source_decls.get(subj.source_name)
        if src:
            lines.extend(build_subject_parse_rule(src, subj))

    for pred in predicate_rules.values():
        src = source_decls.get(pred.source_name)
        if src:
            lines.extend(build_predicate_parse_rule(src, pred))

    for obj in object_rules.values():
        src = source_decls.get(obj.source_name)
        if src:
            lines.extend(build_object_parse_rule(src, obj))

    for graph in graph_rules.values():
        src = source_decls.get(graph.source_name)
        if src:
            lines.extend(build_graph_parse_rule(src, graph))

    # Derive optional rdf:type guards per subject predicate to disambiguate
    # subject templates that may parse the same IRI shape.
    subject_type_guards: Dict[str, List[str]] = {}
    if not minimal_mode:
        for tp in triple_patterns:
            if not tp.subject_pred or not tp.predicate_constant or not tp.object_constant:
                continue
            if not is_rdf_type_predicate(tp.predicate_constant):
                continue
            subject_type_guards.setdefault(tp.subject_pred, [])
            if tp.object_constant not in subject_type_guards[tp.subject_pred]:
                subject_type_guards[tp.subject_pred].append(tp.object_constant)

    # Columns recoverable from non-reference predicate/object evidence across
    # all triple patterns per source. Ref-join fallback must not overwrite
    # these, regardless of rule order.
    recoverable_non_ref_any_by_source: Dict[str, Set[str]] = {}
    recoverable_non_rdf_type_by_source: Dict[str, Set[str]] = {}
    for tp in triple_patterns:
        src_name = find_source_for_triple_pattern(tp, subject_rules, predicate_rules, object_rules, graph_rules)
        if not src_name:
            continue
        src_cols = recoverable_non_ref_any_by_source.setdefault(src_name, set())
        src_cols_non_type = recoverable_non_rdf_type_by_source.setdefault(src_name, set())

        pred_iri = None
        pred = predicate_rules.get(tp.predicate_pred) if tp.predicate_pred else None
        if tp.predicate_constant is not None:
            pred_iri = strip_outer_quotes(tp.predicate_constant)
        elif pred and pred.constant_iri:
            pred_iri = strip_outer_quotes(pred.constant_iri)
        is_type_rule = bool(pred_iri and is_rdf_type_predicate(pred_iri))

        if pred and pred.constant_iri is None and parser_can_recover(pred.parts):
            cols = source_columns_used_in_parts(pred.parts)
            src_cols.update(cols)
            if not is_type_rule:
                src_cols_non_type.update(cols)

        obj = object_rules.get(tp.object_pred) if tp.object_pred else None
        if obj and obj.object_kind != "constant_iri" and parser_can_recover(obj.parts):
            cols = source_columns_used_in_parts(obj.parts)
            src_cols.update(cols)
            if not is_type_rule:
                src_cols_non_type.update(cols)

    # Emit evidence rules
    evidence_count_by_source: Dict[str, int] = {}
    evidence_by_source: Dict[str, List[int]] = {}
    complete_evidence_by_source: Dict[str, List[int]] = {}
    col_evidence_cols_by_source: Dict[str, Set[str]] = {}
    tuple_evidence_by_source: Dict[str, List[Tuple[str, List[str]]]] = {}
    ref_optional_cols_by_source: Dict[str, Set[str]] = {}
    recovered_cols_before_by_source: Dict[str, Set[str]] = {}
    declared_col_evidence: Set[Tuple[str, str]] = set()
    handled_sources = set()

    for tp in triple_patterns:
        src_name = find_source_for_triple_pattern(tp, subject_rules, predicate_rules, object_rules, graph_rules)
        if not src_name:
            continue

        handled_sources.add(src_name)
        source = source_decls[src_name]
        subj = subject_rules.get(tp.subject_pred) if tp.subject_pred else None
        obj_subj = subject_rules.get(tp.object_subject_pred) if tp.object_subject_pred else None
        pred = predicate_rules.get(tp.predicate_pred) if tp.predicate_pred else None
        obj = object_rules.get(tp.object_pred) if tp.object_pred else None

        pred_iri_current = None
        if tp.predicate_constant is not None:
            pred_iri_current = strip_outer_quotes(tp.predicate_constant)
        elif pred and pred.constant_iri:
            pred_iri_current = strip_outer_quotes(pred.constant_iri)
        current_is_type_rule = bool(pred_iri_current and is_rdf_type_predicate(pred_iri_current))

        if not subj:
            continue

        evidence_count_by_source[src_name] = evidence_count_by_source.get(src_name, 0) + 1
        ev_idx = evidence_count_by_source[src_name]
        ev_name = f'Evidence_{src_name}_{ev_idx}'

        lines.append(f'// Evidence rule derived from forward triple rule:')
        lines.append(f'// {tp.raw_rule}')
        rule_text = tp.raw_rule.replace('"', '\\"') if provenance_enabled else ""
        lines.append(f'.decl {ev_name}({", ".join(c + ":symbol" for c in source.columns)})')

        pred_parse_name = ""
        pred_parse_vars: List[str] = []
        obj_parse_name = ""
        obj_parse_vars: List[str] = []
        graph_parse_name = ""
        graph_parse_vars: List[str] = []
        ref_parse_name = ""
        ref_parse_vars: List[str] = []
        subj_parse_vars: List[str] = []

        # Match fixed terms using lexical variants to tolerate equivalent
        # encodings across generators (e.g., <IRI> vs "IRI").
        triple_s = 's'
        triple_p_terms = ['p']
        triple_o_terms = ['o']

        if tp.predicate_constant is not None:
            triple_p_terms = constant_term_variants(tp.predicate_constant)
        elif pred and pred.constant_iri:
            triple_p_terms = constant_term_variants(pred.constant_iri)

        if tp.object_constant is not None:
            triple_o_terms = constant_term_variants(tp.object_constant)
        elif obj and obj.object_kind == "constant_iri" and obj.constant_iri:
            triple_o_terms = constant_term_variants(obj.constant_iri)

        if minimal_mode:
            triple_p_terms = triple_p_terms[:1]
            triple_o_terms = triple_o_terms[:1]

        col_var_for_source: Dict[str, str] = {}
        # Columns directly constrained by this triple pattern. We only emit
        # ColEvidence for these columns to avoid leaking unrelated subject
        # components across sources with overlapping IRI shapes.
        direct_col_var_for_source: Dict[str, str] = {}
        # Columns recovered in this rule without reference-object fallback.
        non_ref_direct_cols: Set[str] = set()

        # Subject parse recovers columns encoded in subject term when parsing
        # is structurally unambiguous.
        subj_cols = source_columns_used_in_parts(subj.parts)
        subj_var_for_source: Dict[str, str] = {}
        subj_recoverable_cols: Set[str] = set()
        if subj_cols and parser_can_recover(subj.parts):
            subj_recoverable_cols = set(subj_cols)
            for c in subj_cols:
                sv = f'subj_{c}'
                subj_var_for_source[c] = sv
                subj_parse_vars.append(sv)
            # Subject parse is added per emitted rule with only needed vars;
            # unused subject components are replaced by '_' to reduce noise.

        # Predicate parse recovers columns when predicate is templated.
        pred_cols = []
        if pred and pred.constant_iri is None and parser_can_recover(pred.parts):
            pred_cols = source_columns_used_in_parts(pred.parts)
            if pred_cols:
                pred_parse_name = f'Parse_{pred.pred_name}'
                pred_parse_vars = list(pred_cols)
                for c in pred_cols:
                    col_var_for_source[c] = c
                    direct_col_var_for_source[c] = c
                    non_ref_direct_cols.add(c)

        # Object parse recovers columns actually encoded in object term
        obj_cols = []
        if obj:
            obj_cols = source_columns_used_in_parts(obj.parts)
            if obj_cols and obj.object_kind != "constant_iri" and parser_can_recover(obj.parts):
                obj_parse_name = f'Parse_{obj.pred_name}'
                obj_parse_vars = list(obj_cols)
                for c in obj_cols:
                    col_var_for_source[c] = c
                    direct_col_var_for_source[c] = c
                    non_ref_direct_cols.add(c)

        # Graph parse recovers columns actually encoded in the graph term.
        graph_rule = graph_rules.get(tp.graph_pred) if tp.graph_pred else None
        if graph_rule:
            graph_cols = source_columns_used_in_parts(graph_rule.parts)
            if graph_cols and parser_can_recover(graph_rule.parts):
                graph_parse_name = f'Parse_{graph_rule.pred_name}'
                graph_parse_vars = list(graph_cols)
                for c in graph_cols:
                    col_var_for_source[c] = c
                    direct_col_var_for_source[c] = c
                    non_ref_direct_cols.add(c)

        # If object is an IRI produced by another Subject map (RefObjectMap-like
        # triple), parse that object IRI and use it to fill missing source cols.
        if obj_subj and parser_can_recover(obj_subj.parts):
            ref_cols = source_columns_used_in_parts(obj_subj.parts)
            if ref_cols:
                ref_var_by_col: Dict[str, str] = {}
                ref_vars: List[str] = []
                for rc in ref_cols:
                    rv = f'ref_{obj_subj.pred_name}_{rc}'
                    ref_var_by_col[rc] = rv
                    ref_vars.append(rv)
                ref_parse_name = f'Parse_{obj_subj.pred_name}'
                ref_parse_vars = list(ref_vars)

                missing_now = [c for c in source.columns if c not in col_var_for_source]
                recovered_before = recovered_cols_before_by_source.get(src_name, set())
                non_ref_any = recoverable_non_ref_any_by_source.get(src_name, set())
                missing_now = [c for c in missing_now if c not in recovered_before and c not in non_ref_any]
                # Do not let RefObjectMap-derived values preempt columns that
                # are directly recoverable from the source subject template.
                missing_now = [c for c in missing_now if c not in subj_recoverable_cols]
                inferred: Dict[str, str] = {}

                # Direct name match first.
                for mc in missing_now:
                    if mc in ref_var_by_col:
                        inferred[mc] = ref_var_by_col[mc]

                # Common RefObjectMap case: one missing FK in child and one key
                # extracted from referenced parent subject.
                unmapped = [c for c in missing_now if c not in inferred]
                used_ref = set(inferred.values())
                free_ref = [rv for rv in ref_vars if rv not in used_ref]
                fallback_targets = [c for c in unmapped if allow_ref_positional_fallback(c)]
                if len(free_ref) == 1 and len(fallback_targets) == 1:
                    inferred[fallback_targets[0]] = free_ref[0]

                for c, v in inferred.items():
                    col_var_for_source[c] = v
                    direct_col_var_for_source[c] = v
                    ref_optional_cols_by_source.setdefault(src_name, set()).add(c)

            recovered_cols_before_by_source.setdefault(src_name, set()).update(non_ref_direct_cols)

        # Use subject-derived values only for columns that remain unresolved by
        # predicate/object/reference evidence. This avoids enforcing equality
        # between subject IRI-normalized values and raw literal column values.
        for c, sv in subj_var_for_source.items():
            if c not in col_var_for_source:
                col_var_for_source[c] = sv

        # If nothing besides subject parsing constrains columns in this rule,
        # allow subject-derived evidence as a fallback.
        if not direct_col_var_for_source:
            for c in subj_cols:
                if c in col_var_for_source:
                    direct_col_var_for_source[c] = col_var_for_source[c]

        def build_parse_atoms_with_needed(needed_vars: Set[str]) -> List[str]:
            atoms: List[str] = []
            if subj_parse_vars:
                subj_args = [v if v in needed_vars else '_' for v in subj_parse_vars]
                atoms.append(f'Parse_{subj.pred_name}(s, {", ".join(subj_args)})')
            if pred_parse_name:
                pred_args = [v if v in needed_vars else '_' for v in pred_parse_vars]
                atoms.append(f'{pred_parse_name}(p, {", ".join(pred_args)})')
            if obj_parse_name:
                obj_args = [v if v in needed_vars else '_' for v in obj_parse_vars]
                atoms.append(f'{obj_parse_name}(o, {", ".join(obj_args)})')
            if graph_parse_name:
                graph_args = [v if v in needed_vars else '_' for v in graph_parse_vars]
                atoms.append(f'{graph_parse_name}(g, {", ".join(graph_args)})')
            if ref_parse_name:
                ref_args = [v if v in needed_vars else '_' for v in ref_parse_vars]
                atoms.append(f'{ref_parse_name}(o, {", ".join(ref_args)})')
            return atoms

        provenance_cols: List[Tuple[str, str, int]] = []
        if provenance_enabled:
            src_pos = {c: i for i, c in enumerate(source.columns)}
            for c, v in subj_var_for_source.items():
                provenance_cols.append((c, v, src_pos.get(c, -1)))
            for c in pred_cols:
                provenance_cols.append((c, c, src_pos.get(c, -1)))
            for c in obj_cols:
                provenance_cols.append((c, c, src_pos.get(c, -1)))

        needed_for_head: Set[str] = set(v for v in col_var_for_source.values() if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v))
        body_atoms: List[str] = build_parse_atoms_with_needed(needed_for_head)

        head_args = []
        is_complete = True
        for c in source.columns:
            if c in col_var_for_source:
                head_args.append(col_var_for_source[c])
            else:
                is_complete = False
                head_args.append('""')

        # Require direct per-triple evidence for completeness. Subject-only
        # carried values can be useful for partials, but should not mark a row
        # as fully reconstructed.
        if not all(c in direct_col_var_for_source for c in source.columns):
            is_complete = False

        guard_variants: List[Tuple[str, str]] = []
        if (not minimal_mode) and subj and subj.pred_name in subject_type_guards:
            for guard_obj in subject_type_guards[subj.pred_name]:
                for gp in constant_term_variants('<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>'):
                    for go in constant_term_variants(guard_obj):
                        guard_variants.append((gp, go))
            # keep deterministic unique guard pairs
            seen_guards = set()
            uniq_guards: List[Tuple[str, str]] = []
            for g in guard_variants:
                if g in seen_guards:
                    continue
                seen_guards.add(g)
                uniq_guards.append(g)
            guard_variants = uniq_guards

        tuple_cols = sorted(direct_col_var_for_source.keys())
        tuple_rel_name = ""
        if (not minimal_mode) and len(tuple_cols) >= 2:
            tuple_rel_name = f'ColTuple_{src_name}_{ev_idx}'
            tuple_sig = ", ".join(c + ":symbol" for c in tuple_cols)
            lines.append(f'.decl {tuple_rel_name}(s:symbol, {tuple_sig})')
            tuple_evidence_by_source.setdefault(src_name, []).append((tuple_rel_name, tuple_cols))

        graph_term_expr = tp.head_graph_term if tp.head_kind == 'quadruple' and tp.head_graph_term is not None else 'g'

        for triple_p in triple_p_terms:
            for triple_o in triple_o_terms:
                if tp.head_kind == 'quadruple':
                    base_parse_atoms = [f'quadruple({triple_s}, {triple_p}, {triple_o}, {graph_term_expr})']
                else:
                    base_parse_atoms = [f'triple({triple_s}, {triple_p}, {triple_o})']

                if not minimal_mode:
                    # Column evidence keyed by RDF subject s. This allows fallback
                    # reconstruction by joining recoverable columns on s.
                    for c in sorted(direct_col_var_for_source.keys()):
                        # Prefer non-rdf:type evidence for a column when available
                        # to avoid same-subject Cartesian products from broad type
                        # triples in dense mappings.
                        if current_is_type_rule and c in recoverable_non_rdf_type_by_source.get(src_name, set()):
                            continue

                        key = (src_name, c)
                        rel_name = f'ColEvidence_{src_name}_{c}'
                        if key not in declared_col_evidence:
                            lines.append(f'.decl {rel_name}(s:symbol, {c}:symbol)')
                            declared_col_evidence.add(key)
                        col_evidence_cols_by_source.setdefault(src_name, set()).add(c)
                        cvar = direct_col_var_for_source[c]
                        c_needed = {cvar} if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', cvar) else set()
                        col_parse_atoms = base_parse_atoms + build_parse_atoms_with_needed(c_needed)

                        if guard_variants:
                            for gp, go in guard_variants:
                                atoms = col_parse_atoms + [f'triple({triple_s}, {gp}, {go})']
                                lines.append(f'{rel_name}(s, {cvar}) :- {", ".join(atoms)}.')
                        else:
                            lines.append(f'{rel_name}(s, {cvar}) :- {", ".join(col_parse_atoms)}.')

                    if tuple_rel_name:
                        tuple_args = ", ".join(direct_col_var_for_source[c] for c in tuple_cols)
                        t_needed = set(v for v in (direct_col_var_for_source[c] for c in tuple_cols) if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v))
                        tuple_parse_atoms = base_parse_atoms + build_parse_atoms_with_needed(t_needed)
                        if guard_variants:
                            for gp, go in guard_variants:
                                atoms = tuple_parse_atoms + [f'triple({triple_s}, {gp}, {go})']
                                lines.append(f'{tuple_rel_name}(s, {tuple_args}) :- {", ".join(atoms)}.')
                        else:
                            lines.append(f'{tuple_rel_name}(s, {tuple_args}) :- {", ".join(tuple_parse_atoms)}.')

                if tp.head_kind == 'quadruple':
                    base_atoms = [f'quadruple({triple_s}, {triple_p}, {triple_o}, {graph_term_expr})'] + body_atoms
                    prov_atoms = [f'quadruple({triple_s}, {triple_p}, {triple_o}, {graph_term_expr})'] + build_parse_atoms_with_needed(set())
                else:
                    base_atoms = [f'triple({triple_s}, {triple_p}, {triple_o})'] + body_atoms
                    prov_atoms = [f'triple({triple_s}, {triple_p}, {triple_o})'] + build_parse_atoms_with_needed(set())
                if guard_variants:
                    for gp, go in guard_variants:
                        atoms = base_atoms + [f'triple({triple_s}, {gp}, {go})']
                        p_atoms = prov_atoms + [f'triple({triple_s}, {gp}, {go})']
                        lines.append(f'{ev_name}({", ".join(head_args)}) :- {", ".join(atoms)}.')
                        if provenance_enabled:
                            lines.append(
                                f'Prov_Evidence("{src_name}", {triple_s}, {triple_p}, {triple_o}) :- {", ".join(p_atoms)}.'
                            )
                            lines.append(
                                f'Prov_Evidence_Quad("{src_name}", {triple_s}, {triple_p}, {triple_o}, gq) :- {", ".join(p_atoms)}, quadruple({triple_s}, {triple_p}, {triple_o}, gq).'
                            )
                            lines.append(
                                f'Prov_Mapping("{src_name}", "{tp.head_kind}", "{rule_text}", {triple_s}, {triple_p}, {triple_o}) :- {", ".join(p_atoms)}.'
                            )
                            for col, val_var, pos in provenance_cols:
                                v_needed = {val_var} if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', val_var) else set()
                                v_atoms = [f'triple({triple_s}, {triple_p}, {triple_o})'] + build_parse_atoms_with_needed(v_needed) + [f'triple({triple_s}, {gp}, {go})']
                                lines.append(
                                    f'Prov_Column("{src_name}", {triple_s}, {triple_p}, {triple_o}, "{col}", {val_var}, {pos}) :- {", ".join(v_atoms)}.'
                                )
                else:
                    lines.append(f'{ev_name}({", ".join(head_args)}) :- {", ".join(base_atoms)}.')
                    if provenance_enabled:
                        lines.append(
                            f'Prov_Evidence("{src_name}", {triple_s}, {triple_p}, {triple_o}) :- {", ".join(prov_atoms)}.'
                        )
                        lines.append(
                            f'Prov_Evidence_Quad("{src_name}", {triple_s}, {triple_p}, {triple_o}, gq) :- {", ".join(prov_atoms)}, quadruple({triple_s}, {triple_p}, {triple_o}, gq).'
                        )
                        lines.append(
                            f'Prov_Mapping("{src_name}", "{tp.head_kind}", "{rule_text}", {triple_s}, {triple_p}, {triple_o}) :- {", ".join(prov_atoms)}.'
                        )
                        for col, val_var, pos in provenance_cols:
                            v_needed = {val_var} if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', val_var) else set()
                            v_atoms = [f'triple({triple_s}, {triple_p}, {triple_o})'] + build_parse_atoms_with_needed(v_needed)
                            lines.append(
                                f'Prov_Column("{src_name}", {triple_s}, {triple_p}, {triple_o}, "{col}", {val_var}, {pos}) :- {", ".join(v_atoms)}.'
                            )
        lines.append('')

        # Keep all evidence rules for potential fallback reconstruction.
        # Unrecoverable columns are represented by empty-string placeholders
        # in the generated evidence.
        evidence_by_source.setdefault(src_name, []).append(ev_idx)
        if is_complete:
            complete_evidence_by_source.setdefault(src_name, []).append(ev_idx)

    if not minimal_mode:
        # For columns never directly evidenced in a source, backfill from subject
        # parsing but only for subjects already anchored by at least one direct
        # ColEvidence column from the same source. This restores key columns like
        # IDs in mixed mappings without reintroducing broad cross-source leakage.
        for src_name in sorted(handled_sources):
            source = source_decls[src_name]
            direct_cols = col_evidence_cols_by_source.get(src_name, set())
            missing_cols = [c for c in source.columns if c not in direct_cols]
            if not missing_cols:
                continue
            if not direct_cols:
                continue

            anchor_col = sorted(direct_cols)[0]
            anchor_rel = f'ColEvidence_{src_name}_{anchor_col}'

            for subj in subject_rules.values():
                if subj.source_name != src_name:
                    continue
                if not parser_can_recover(subj.parts):
                    continue

                subj_cols = source_columns_used_in_parts(subj.parts)
                if not subj_cols:
                    continue

                parse_args = ", ".join(subj_cols)
                for c in missing_cols:
                    if c not in subj_cols:
                        continue
                    key = (src_name, c)
                    rel_name = f'ColEvidence_{src_name}_{c}'
                    if key not in declared_col_evidence:
                        lines.append(f'.decl {rel_name}(s:symbol, {c}:symbol)')
                        declared_col_evidence.add(key)
                    col_evidence_cols_by_source.setdefault(src_name, set()).add(c)
                    lines.append(
                        f'{rel_name}(s, {c}) :- Parse_{subj.pred_name}(s, {parse_args}), {anchor_rel}(s, _).'
                    )
            lines.append('')

    # Emit compact reconstructed source relations (minimal default behavior).
    for src_name in sorted(handled_sources):
        source = source_decls[src_name]
        lines.append(f'.decl Recovered_{src_name}({", ".join(c + ":symbol" for c in source.columns)})')
        if recovery_mode == "strict":
            selected_indices = complete_evidence_by_source.get(src_name, [])
        else:
            selected_indices = evidence_by_source.get(src_name, [])
        for idx in selected_indices:
            ev_name = f'Evidence_{src_name}_{idx}'
            lines.append(f'Recovered_{src_name}({", ".join(source.columns)}) :- {ev_name}({", ".join(source.columns)}).')
        lines.append(f'.output Recovered_{src_name}')
        lines.append('')

    if provenance_enabled:
        lines.append('ExplainTriple(source_rel, s, p, o) :- Prov_Evidence(source_rel, s, p, o).')
        lines.append('ExplainQuad(source_rel, s, p, o, g) :- Prov_Evidence_Quad(source_rel, s, p, o, g).')
        lines.append('ExplainContributor(source_rel, s, p, o, col, val, pos) :- Prov_Column(source_rel, s, p, o, col, val, pos).')
        lines.append('ExplainRule(source_rel, output_kind, source_rule, s, p, o) :- Prov_Mapping(source_rel, output_kind, source_rule, s, p, o).')
        lines.append('')

        lines.append('.output ExplainTriple')
        lines.append('.output ExplainQuad')
        lines.append('.output ExplainContributor')
        lines.append('.output ExplainRule')
        lines.append('')

    return '\n'.join(lines)


def build_forward_provenance_program(
    forward_text: str,
    source_decls: Dict[str, SourceDecl],
    subject_rules: Dict[str, SubjectRule],
    predicate_rules: Dict[str, PredicateRule],
    object_rules: Dict[str, ObjectRule],
    triple_patterns: List[TriplePattern],
    provenance_enabled: bool = True,
    input_dir: Optional[str] = None,
    target_triples_file: Optional[str] = None,
) -> str:
    """
    Build a forward provenance program:
      sources (input) -> triples/quads + provenance metadata.

    When target_triples_file is given, a TargetTriple relation is added and
    every Prov* rule is guarded by TargetTriple(s, p, o), limiting provenance
    materialization to only the triples listed in that file.  The forward
    triple/quadruple rules themselves are unaffected.
    """
    cleaned_lines: List[str] = []

    def pick_input_filename(rel_name: str) -> str:
        m_rel = re.match(r'^([A-Za-z0-9_]+)_lt(\d+)$', rel_name)
        lt_candidate = f'lt{m_rel.group(2)}.facts' if m_rel else None
        rel_candidate = f'{rel_name}.facts'

        if input_dir:
            if lt_candidate and os.path.exists(os.path.join(input_dir, lt_candidate)):
                return lt_candidate
            if os.path.exists(os.path.join(input_dir, rel_candidate)):
                return rel_candidate

        return lt_candidate if lt_candidate else rel_candidate

    for ln in forward_text.splitlines():
        m = re.match(r'^\s*\.input\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*$', ln)
        if m:
            # Some test cases contain malformed split input names like
            # '.input DEPT lt1'; normalize to '.input DEPT_lt1'.
            rel = f'{m.group(1)}_{m.group(2)}'
            cleaned_lines.append(f'.input {rel}(filename="{pick_input_filename(rel)}")')
            continue

        m2 = re.match(r'^\s*\.input\s+([A-Za-z0-9_]+)\s*$', ln)
        if m2:
            rel = m2.group(1)
            cleaned_lines.append(f'.input {rel}(filename="{pick_input_filename(rel)}")')
            continue
        else:
            cleaned_lines.append(ln)
    cleaned_forward_text = "\n".join(cleaned_lines)

    lines: List[str] = [cleaned_forward_text.rstrip(), "", "// ==========================================", "// Forward Provenance Additions", "// ==========================================", ""]

    if not provenance_enabled:
        return "\n".join(lines)

    lines.append('.decl ProvTriple(source_rel:symbol, s:symbol, p:symbol, o:symbol)')
    lines.append('.decl ProvQuad(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol)')
    lines.append('.decl ProvContributor(source_rel:symbol, s:symbol, p:symbol, o:symbol, col:symbol, val:symbol, pos:number)')
    lines.append('.decl ProvQuadContributor(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol, col:symbol, val:symbol, pos:number)')
    lines.append('')
    lines.append('.decl ExplainTriple(source_rel:symbol, s:symbol, p:symbol, o:symbol)')
    lines.append('.decl ExplainQuad(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol)')
    lines.append('.decl ExplainContributor(source_rel:symbol, s:symbol, p:symbol, o:symbol, col:symbol, val:symbol, pos:number)')
    lines.append('.decl ExplainQuadContributor(source_rel:symbol, s:symbol, p:symbol, o:symbol, g:symbol, col:symbol, val:symbol, pos:number)')
    lines.append('.decl ExplainAnyValue(source_rel:symbol, s:symbol, p:symbol, o:symbol, val:symbol)')
    lines.append('.decl ExplainAllInput(source_rel:symbol, col:symbol, val:symbol)')
    lines.append('.decl ExplainAnyInput(source_rel:symbol, col:symbol, val:symbol)')
    lines.append('')

    # Selective provenance: optional TargetTriple guard
    target_guard: str = ''
    if target_triples_file:
        target_filename = os.path.basename(target_triples_file)
        lines.append('// Selective provenance: only triples listed in TargetTriple receive Prov* facts')
        lines.append('.decl TargetTriple(s:symbol, p:symbol, o:symbol)')
        lines.append(f'.input TargetTriple(filename="{target_filename}", delimiter="\\t")')
        lines.append('')
        target_guard = ', TargetTriple(s, p, o)'

    for tp in triple_patterns:
        src_name = find_source_for_triple_pattern(tp, subject_rules, predicate_rules, object_rules)
        if not src_name:
            continue

        pr = parse_rule(tp.raw_rule)
        if not pr:
            continue
        head, body = pr
        try:
            head_pred, head_args = parse_atom(head)
        except Exception:
            continue
        body_atoms = split_top_level_args(body)

        if head_pred not in ("triple", "quadruple"):
            continue
        if len(head_args) < 3:
            continue

        atoms = list(body_atoms)
        # Bind output tuple variables (s,p,o[,g]) from the original head terms.
        bindings: List[str] = []
        out_terms = [('s', head_args[0]), ('p', head_args[1]), ('o', head_args[2])]
        if head_pred == "quadruple" and len(head_args) >= 4:
            out_terms.append(('g', head_args[3]))

        for out_var, term in out_terms:
            if is_identifier(term):
                if term != out_var:
                    bindings.append(f'{out_var} = {term}')
            else:
                bindings.append(f'{out_var} = {term}')

        atoms.extend(bindings)
        if not atoms:
            continue

        src_decl = source_decls.get(src_name)
        if not src_decl:
            continue

        lines.append(f'ProvTriple("{src_name}", s, p, o) :- {", ".join(atoms)}{target_guard}.')
        if head_pred == "quadruple":
            lines.append(f'ProvQuad("{src_name}", s, p, o, g) :- {", ".join(atoms)}{target_guard}.')

        contributors: List[Tuple[str, str, int]] = []
        seen_contrib: Set[Tuple[str, str, int]] = set()

        def collect_contributors(rule_source_args: List[str], parts: List[TemplatePart]):
            col_to_arg = {c: a for c, a in zip(src_decl.columns, rule_source_args)}
            part_cols = source_columns_used_in_parts(parts)
            if part_cols:
                for pos, col in enumerate(part_cols):
                    val = col_to_arg.get(col, col)
                    if not is_identifier(val):
                        continue
                    key = (col, val, pos)
                    if key in seen_contrib:
                        continue
                    seen_contrib.add(key)
                    contributors.append(key)
            else:
                # Fallback: constant term maps still originate from source rows.
                # Emit available source-argument bindings to maximize retrievable
                # input evidence when template variables are absent.
                for pos, (col, val) in enumerate(zip(src_decl.columns, rule_source_args)):
                    if not is_identifier(val):
                        continue
                    key = (col, val, pos)
                    if key in seen_contrib:
                        continue
                    seen_contrib.add(key)
                    contributors.append(key)

        subj = subject_rules.get(tp.subject_pred) if tp.subject_pred else None
        pred = predicate_rules.get(tp.predicate_pred) if tp.predicate_pred else None
        obj = object_rules.get(tp.object_pred) if tp.object_pred else None

        if subj:
            collect_contributors(subj.source_args, subj.parts)
        if pred and pred.constant_iri is None:
            collect_contributors(pred.source_args, pred.parts)
        if obj and obj.object_kind != "constant_iri":
            collect_contributors(obj.source_args, obj.parts)

        for col, val_var, pos in contributors:
            lines.append(
                f'ProvContributor("{src_name}", s, p, o, "{col}", {val_var}, {pos}) :- {", ".join(atoms)}{target_guard}.'
            )

        if head_pred == "quadruple" and len(head_args) >= 4:
            g_term = head_args[3]
            quad_contributors: List[Tuple[str, str, int]] = []
            seen_qc: Set[Tuple[str, str, int]] = set()

            def add_quad_contrib(col: str, val_var: str, pos: int):
                if not is_identifier(val_var):
                    return
                key = (col, val_var, pos)
                if key in seen_qc:
                    return
                seen_qc.add(key)
                quad_contributors.append(key)

            if is_identifier(g_term):
                # Direct source-variable graph map.
                src_atom_args: Optional[List[str]] = None
                for a in body_atoms:
                    try:
                        bp, bargs = parse_atom(a)
                    except Exception:
                        continue
                    if bp == src_name:
                        src_atom_args = bargs
                        break
                if src_atom_args:
                    for idx, v in enumerate(src_atom_args):
                        if v == g_term and idx < len(src_decl.columns):
                            add_quad_contrib(src_decl.columns[idx], g_term, idx)

                # Graph term produced by another mapped term in body.
                for a in body_atoms:
                    try:
                        bp, bargs = parse_atom(a)
                    except Exception:
                        continue
                    if not bargs or bargs[0] != g_term:
                        continue
                    if bp in subject_rules:
                        sr = subject_rules[bp]
                        col_to_arg = {c: av for c, av in zip(src_decl.columns, sr.source_args)}
                        part_cols = source_columns_used_in_parts(sr.parts)
                        if part_cols:
                            for pos, col in enumerate(part_cols):
                                add_quad_contrib(col, col_to_arg.get(col, col), pos)
                        else:
                            for pos, (col, val) in enumerate(zip(src_decl.columns, sr.source_args)):
                                add_quad_contrib(col, val, pos)
                    elif bp in predicate_rules:
                        prd = predicate_rules[bp]
                        if prd.constant_iri is None:
                            col_to_arg = {c: av for c, av in zip(src_decl.columns, prd.source_args)}
                            part_cols = source_columns_used_in_parts(prd.parts)
                            if part_cols:
                                for pos, col in enumerate(part_cols):
                                    add_quad_contrib(col, col_to_arg.get(col, col), pos)
                            else:
                                for pos, (col, val) in enumerate(zip(src_decl.columns, prd.source_args)):
                                    add_quad_contrib(col, val, pos)
                    elif bp in object_rules:
                        orule = object_rules[bp]
                        if orule.object_kind != "constant_iri":
                            col_to_arg = {c: av for c, av in zip(src_decl.columns, orule.source_args)}
                            part_cols = source_columns_used_in_parts(orule.parts)
                            if part_cols:
                                for pos, col in enumerate(part_cols):
                                    add_quad_contrib(col, col_to_arg.get(col, col), pos)
                            else:
                                for pos, (col, val) in enumerate(zip(src_decl.columns, orule.source_args)):
                                    add_quad_contrib(col, val, pos)

            for col, val_var, pos in quad_contributors:
                lines.append(
                    f'ProvQuadContributor("{src_name}", s, p, o, g, "{col}", {val_var}, {pos}) :- {", ".join(atoms)}{target_guard}.'
                )

        lines.append('')

    lines.append('ExplainTriple(source_rel, s, p, o) :- ProvTriple(source_rel, s, p, o).')
    lines.append('ExplainQuad(source_rel, s, p, o, g) :- ProvQuad(source_rel, s, p, o, g).')
    lines.append('ExplainContributor(source_rel, s, p, o, col, val, pos) :- ProvContributor(source_rel, s, p, o, col, val, pos).')
    lines.append('ExplainQuadContributor(source_rel, s, p, o, g, col, val, pos) :- ProvQuadContributor(source_rel, s, p, o, g, col, val, pos).')
    lines.append('ExplainAnyValue(source_rel, s, p, o, val) :- ProvContributor(source_rel, s, p, o, _, val, _).')
    lines.append('ExplainAnyValue(source_rel, s, p, o, val) :- ProvQuadContributor(source_rel, s, p, o, _, _, val, _).')
    lines.append('ExplainAllInput(source_rel, col, val) :- ProvContributor(source_rel, _, _, _, col, val, _).')
    lines.append('ExplainAllInput(source_rel, col, val) :- ProvQuadContributor(source_rel, _, _, _, _, col, val, _).')
    lines.append('ExplainAnyInput(source_rel, col, val) :- ExplainAllInput(source_rel, col, val).')
    lines.append('')
    lines.append('.output ExplainTriple')
    lines.append('.output ExplainQuad')
    lines.append('.output ExplainContributor')
    lines.append('.output ExplainQuadContributor')
    lines.append('.output ExplainAnyValue')
    lines.append('.output ExplainAllInput')
    lines.append('.output ExplainAnyInput')

    return "\n".join(lines)


# =============================================================================
# Main CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate forward-provenance or reverse Datalog programs from a forward mapping program."
    )
    parser.add_argument('input_D_file', help='Path to forward Datalog input file')
    parser.add_argument(
        'output_D_prime_file',
        nargs='?',
        default=None,
        help='Optional path to write the primary generated output for the selected mode'
    )
    parser.add_argument(
        '--with-provenance',
        action='store_true',
        help='Emit provenance relations for the selected mode'
    )
    parser.add_argument(
        '--mode',
        choices=['forward', 'reverse'],
        default='forward',
        help='Generation mode: forward (source-input provenance) or reverse (triple-input reconstruction)'
    )
    parser.add_argument(
        '--recovery-mode',
        choices=['best-effort', 'strict'],
        default='best-effort',
        help='Deprecated in forward provenance mode (accepted for backward CLI compatibility)'
    )
    parser.add_argument(
        '--support-report',
        help='Optional path to write JSON support report (full/partial/unsupported)'
    )
    parser.add_argument(
        '--forward-output',
        help='Optional extra path to write the forward/provenance program when using --mode forward'
    )
    parser.add_argument(
        '--reverse-output',
        help='Optional extra path to write the reverse program alongside the forward/provenance output'
    )
    parser.add_argument(
        '--target-triples-file',
        help='Optional path to a tab-separated file (s, p, o) used to limit provenance materialization to selected triples (--mode forward --with-provenance only)'
    )
    args = parser.parse_args()

    if args.mode == 'reverse' and args.forward_output:
        parser.error('--forward-output is only supported with --mode forward')

    if args.target_triples_file and (args.mode != 'forward' or not args.with_provenance):
        parser.error('--target-triples-file requires --mode forward and --with-provenance')

    if not args.output_D_prime_file and not args.forward_output and not args.reverse_output:
        parser.error('at least one output path must be provided')

    in_file = args.input_D_file
    out_file = args.output_D_prime_file

    with open(in_file, 'r', encoding='utf-8') as f:
        text = f.read()

    source_decls, subject_rules, predicate_rules, object_rules, graph_rules, triple_patterns = parse_datalog(text)
    support_report = compute_support_report(
        source_decls,
        subject_rules,
        predicate_rules,
        object_rules,
        graph_rules,
        triple_patterns,
    )

    forward_program = None
    reverse_program = None

    if args.mode == 'forward' or args.forward_output:
        forward_program = build_forward_provenance_program(
            text,
            source_decls,
            subject_rules,
            predicate_rules,
            object_rules,
            triple_patterns,
            provenance_enabled=args.with_provenance,
            input_dir=os.path.dirname(os.path.abspath(in_file)),
            target_triples_file=args.target_triples_file if hasattr(args, 'target_triples_file') else None,
        )

    if args.mode == 'reverse' or args.reverse_output:
        reverse_program = build_reverse_program(
            source_decls,
            subject_rules,
            predicate_rules,
            object_rules,
            graph_rules,
            triple_patterns,
            provenance_enabled=args.with_provenance,
            minimal_mode=True,
            recovery_mode=args.recovery_mode,
        )

    if args.mode == 'forward':
        primary_program = forward_program
        primary_path = out_file or args.forward_output
        if not primary_path and args.reverse_output:
            primary_path = args.reverse_output
    else:
        primary_program = reverse_program
        primary_path = out_file

    if primary_path and primary_program is not None:
        with open(primary_path, 'w', encoding='utf-8') as f:
            f.write(primary_program)

    if args.forward_output and forward_program is not None:
        with open(args.forward_output, 'w', encoding='utf-8') as f:
            f.write(forward_program)

    if args.reverse_output and reverse_program is not None:
        with open(args.reverse_output, 'w', encoding='utf-8') as f:
            f.write(reverse_program)

    if args.support_report:
        with open(args.support_report, 'w', encoding='utf-8') as f:
            json.dump(support_report, f, indent=2)

    if args.mode == 'forward':
        if args.with_provenance:
            print(f"Wrote forward provenance program to {primary_path}")
        else:
            print(f"Wrote forward program to {primary_path}")
    else:
        if args.with_provenance:
            print(f"Wrote reverse program with provenance to {primary_path}")
        else:
            print(f"Wrote reverse program to {primary_path}")

    if args.forward_output:
        print(f"Wrote forward program to {args.forward_output}")
    if args.reverse_output:
        print(f"Wrote reverse program to {args.reverse_output}")
    if args.support_report:
        print(f"Wrote support report to {args.support_report}")


if __name__ == "__main__":
    main()