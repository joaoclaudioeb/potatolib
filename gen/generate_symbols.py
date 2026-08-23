#!/usr/bin/env python3
"""
generate_symbols.py
-------------------
Generates a KiCad 9 .kicad_sym library from:
  - A _templates.kicad_sym file (one symbol per graphic variant)
  - One or more CSV files with component data
  - A config.json controlling field layout

Usage:
    python generate_symbols.py [options]

Options:
    --templates   Path to _templates.kicad_sym     (default: ./_templates.kicad_sym)
    --csv         One or more CSV files            (default: all *.csv in current dir)
    --output      Output .kicad_sym file           (default: ./output_library.kicad_sym)
    --config      Path to config.json              (default: ./config.json)

CSV columns:
    Required : MPN, Value
    Optional : Footprint, Datasheet, Manufacturer, Package, Description, Symbol
               (any extra column becomes a hidden field automatically)
    Notes    : If Symbol is empty or missing, symbol is generated fields-only (no graphic).
"""

import argparse
import copy
import csv
import glob
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal S-expression parser
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list:
    tokens = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            j = i + 1
            result = []
            while j < len(text):
                if text[j] == '\\' and j + 1 < len(text):
                    result.append(text[j + 1])
                    j += 2
                elif text[j] == '"':
                    break
                else:
                    result.append(text[j])
                    j += 1
            tokens.append('"' + ''.join(result) + '"')
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in ' \t\n\r()"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_sexpr(tokens: list, pos: int = 0):
    if tokens[pos] == '(':
        pos += 1
        node = []
        while tokens[pos] != ')':
            child, pos = parse_sexpr(tokens, pos)
            node.append(child)
        pos += 1
        return node, pos
    else:
        return tokens[pos], pos + 1


def load_kicad_sym(path: str) -> list:
    text = Path(path).read_text(encoding='utf-8')
    tokens = tokenize(text)
    tree, _ = parse_sexpr(tokens, 0)
    return tree


# ---------------------------------------------------------------------------
# S-expression serializer
# ---------------------------------------------------------------------------

SINGLE_LINE = {
    'pts', 'xy', 'at', 'start', 'end', 'mid',
    'stroke', 'fill', 'effects', 'font', 'justify', 'offset', 'size',
    'hide', 'do_not_autoplace',
}

def serialize(node, indent: int = 0) -> str:
    if isinstance(node, str):
        return node
    if not node:
        return '()'
    head = node[0]
    if isinstance(head, str) and head in SINGLE_LINE:
        return '(' + ' '.join(serialize(c, 0) for c in node) + ')'
    pad = '\t' * (indent + 1)
    parts = [serialize(c, indent + 1) for c in node]
    single = '(' + ' '.join(parts) + ')'
    if len(single) <= 100 and '\n' not in single:
        return single
    lines = ['(' + parts[0]]
    for p in parts[1:]:
        lines.append(pad + p)
    lines[-1] += ')'
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def get_symbol_names(lib_tree: list) -> list:
    return [
        node[1].strip('"')
        for node in lib_tree
        if isinstance(node, list) and node and node[0] == 'symbol'
    ]


def extract_template(lib_tree: list, name: str) -> list | None:
    for node in lib_tree:
        if isinstance(node, list) and node and node[0] == 'symbol':
            if node[1].strip('"') == name:
                return node
    return None


def get_existing_property_names(symbol: list) -> list:
    return [
        node[1].strip('"')
        for node in symbol
        if isinstance(node, list) and node and node[0] == 'property'
    ]


# ---------------------------------------------------------------------------
# Field builders (driven by config)
# ---------------------------------------------------------------------------

def _font_node(size: float) -> list:
    s = str(size)
    return ['font', ['size', s, s]]


def _effects_visible(cfg_field: dict) -> list:
    return ['effects', _font_node(cfg_field['font_size'])]


def _effects_hidden(cfg_hidden: dict) -> list:
    justify = cfg_hidden.get('justify', 'left')
    size = cfg_hidden['font_size']
    s = str(size)
    return ['effects', ['font', ['size', s, s]], ['justify', justify], ['hide', 'yes']]


def _at(x: float, y: float, rot: float = 0) -> list:
    return ['at', str(x), str(y), str(int(rot))]


def make_reference_property(cfg: dict, y_max: float, ref_default: str = 'R?') -> list:
    rc = cfg['reference']
    y = round(y_max + rc['offset_y_above_graphic'], 6)
    node = ['property', '"Reference"', f'"{ref_default}"',
            _at(rc['x'], y),
            _effects_visible(rc)]
    if rc.get('do_not_autoplace', True):
        node.append(['do_not_autoplace'])
    return node


def make_value_property(value: str, cfg: dict, y_max: float) -> list:
    vc = cfg['value']
    y = round(y_max + vc['offset_y_above_graphic'], 6)
    node = ['property', f'"{value}"', f'"{value}"',
            _at(vc['x'], y),
            _effects_visible(vc)]
    # KiCad stores Value as property name AND value both equal to the value string
    # but the property "key" should literally be "Value"
    node[1] = '"Value"'
    node[2] = f'"{value}"'
    if vc.get('do_not_autoplace', True):
        node.append(['do_not_autoplace'])
    return node


def make_hidden_property(name: str, value: str, x: float, y: float, cfg: dict) -> list:
    hc = cfg['hidden_fields']
    justify = hc.get('justify', 'left')
    size = hc['font_size']
    s = str(size)
    return [
        'property', f'"{name}"', f'"{value}"',
        _at(x, y),
        ['effects', ['font', ['size', s, s]], ['justify', justify], ['hide', 'yes']],
    ]


# ---------------------------------------------------------------------------
# Graphic bounding box (for y_max calculation)
# ---------------------------------------------------------------------------

def _xy_values(node: list) -> list:
    """Recursively collect all (x, y) coordinate pairs from a node."""
    coords = []
    if isinstance(node, list):
        if node and node[0] in ('xy', 'at', 'start', 'end', 'mid', 'center') and len(node) >= 3:
            try:
                coords.append((float(node[1]), float(node[2])))
            except ValueError:
                pass
        for child in node:
            coords.extend(_xy_values(child))
    return coords


def compute_y_max(symbol: list) -> float:
    """
    Find the maximum Y coordinate among all graphic primitives
    (polylines, arcs, circles, rectangles) — excluding pins and private elements.
    Returns 1.016 as a safe fallback (top of resistor zigzag).
    """
    y_vals = []
    for node in symbol:
        if not isinstance(node, list):
            continue
        if node and node[0] == 'symbol':
            # sub-symbol: scan its children for graphic primitives
            for child in node[1:]:
                if not isinstance(child, list):
                    continue
                prim = child[0] if child else None
                if prim in ('polyline', 'arc', 'circle', 'rectangle', 'bezier'):
                    # skip private (info table) elements
                    is_private = any(
                        c == 'private' or (isinstance(c, str) and c == 'private')
                        for c in child
                    )
                    if not is_private:
                        for xy in _xy_values(child):
                            y_vals.append(xy[1])
    return max(y_vals) if y_vals else 1.016


# ---------------------------------------------------------------------------
# Symbol generation
# ---------------------------------------------------------------------------

# Fields that get special visible treatment
VISIBLE_FIELDS = {'Reference', 'Value'}

# Fields we never add as extra hidden (already handled explicitly)
SKIP_FIELDS = {'MPN', 'Value', 'Symbol', 'Reference'}


def build_info_table(fields_in_order: list, cfg: dict) -> list:
    """
    Rebuild the private graphical info table (polylines + text labels)
    to match the actual fields present, using config layout.
    """
    hc = cfg['hidden_fields']
    ic = cfg['info_table']
    if not ic.get('enabled', True):
        return []

    x0 = ic['line_start_x']
    x1 = hc['start_x']
    y_top = hc['start_y']
    step = hc['step_y']        # negative = downward
    lx = ic['label_x']
    lfs = str(ic['label_font_size'])

    n = len(fields_in_order)
    y_bottom = y_top + step * n   # e.g. 6.35 + (-2.54)*6 = -8.89

    nodes = []

    # Outer box: right side vertical + bottom
    nodes.append([
        'polyline', 'private',
        ['pts', ['xy', str(x0), '0'], ['xy', str(x0), str(y_bottom)], ['xy', str(x1), str(y_bottom)]],
        ['stroke', ['width', '0'], ['type', 'solid']],
        ['fill', ['type', 'none']],
    ])
    # Top + left vertical back to origin
    nodes.append([
        'polyline', 'private',
        ['pts', ['xy', str(x1), str(y_top)], ['xy', str(x0), str(y_top)],
         ['xy', str(x0), '0'], ['xy', '0', '0']],
        ['stroke', ['width', '0'], ['type', 'solid']],
        ['fill', ['type', 'none']],
    ])
    # Horizontal dividers between rows
    for i in range(1, n):
        y = round(y_top + step * i, 6)
        nodes.append([
            'polyline', 'private',
            ['pts', ['xy', str(x1), str(y)], ['xy', str(x0), str(y)]],
            ['stroke', ['width', '0'], ['type', 'solid']],
            ['fill', ['type', 'none']],
        ])
    # Text labels (centred vertically in each row)
    for i, fname in enumerate(fields_in_order):
        y_row_top = y_top + step * i
        y_row_bot = y_top + step * (i + 1)
        y_label = round((y_row_top + y_row_bot) / 2 + 0.508, 6)
        nodes.append([
            'text', 'private', f'"{fname}"',
            ['at', str(lx), str(y_label), '0'],
            ['effects', ['font', ['size', lfs, lfs]]],
        ])

    return nodes


def rename_subsymbols(symbol: list, new_name: str) -> None:
    old_name = symbol[1].strip('"')
    for node in symbol:
        if isinstance(node, list) and node and node[0] == 'symbol':
            sub = node[1].strip('"') if isinstance(node[1], str) else node[1]
            if sub.startswith(old_name + '_'):
                suffix = sub[len(old_name):]
                node[1] = f'"{new_name}{suffix}"'


def rename_subsymbols_explicit(symbol: list, old_name: str, new_name: str) -> None:
    """Rename sub-symbols using explicit old_name — safe after parent name already changed."""
    for node in symbol:
        if isinstance(node, list) and node and node[0] == 'symbol':
            sub = node[1].strip('"') if isinstance(node[1], str) else node[1]
            if sub.startswith(old_name + '_'):
                suffix = sub[len(old_name):]
                node[1] = f'"{new_name}{suffix}"'


def apply_pin_fonts(sym, cfg):
    """Set pin name/number font sizes from config. No-op if 'pin_text' absent."""
    pc = cfg.get('pin_text')
    if not pc:
        return
    targets = {}
    if 'name_font_size' in pc:
        targets['name'] = str(pc['name_font_size'])
    if 'number_font_size' in pc:
        targets['number'] = str(pc['number_font_size'])
    if not targets:
        return

    def walk(node):
        if not isinstance(node, list):
            return
        if node and node[0] == 'pin':
            for child in node:
                if not (isinstance(child, list) and child and child[0] in targets):
                    continue
                s = targets[child[0]]
                for eff in child:
                    if not (isinstance(eff, list) and eff and eff[0] == 'effects'):
                        continue
                    for fnt in eff:
                        if not (isinstance(fnt, list) and fnt and fnt[0] == 'font'):
                            continue
                        for sz in fnt:
                            if isinstance(sz, list) and sz and sz[0] == 'size' and len(sz) >= 3:
                                sz[1] = s
                                sz[2] = s
        for child in node:
            walk(child)

    walk(sym)



def generate_symbol(template: list | None, row: dict, output_name: str, cfg: dict) -> list:
    """
    Build a complete symbol node.
    If template is None → fields-only symbol (no graphic sub-symbols).
    """
    fields_order = cfg.get('fields_order', [
        'Footprint', 'Datasheet', 'Manufacturer', 'Package', 'MPN', 'Description'
    ])

    if template is not None:
        sym = copy.deepcopy(template)
        template_name = sym[1].strip('"')   # capture before anything changes
        sym[1] = f'"{output_name}"'
        y_max = compute_y_max(sym)

        # Remove ALL existing property nodes (we rebuild them cleanly)
        sym = [n for n in sym if not (isinstance(n, list) and n and n[0] == 'property')]

        # Rename sub-symbols AFTER list comprehension (new list object)
        rename_subsymbols_explicit(sym, template_name, output_name)

        # Rebuild info table in _1_1 sub-symbol to match actual fields
        for node in sym:
            if isinstance(node, list) and node and node[0] == 'symbol':
                sub = node[1].strip('"') if isinstance(node[1], str) else node[1]
                if sub.endswith('_1_1'):
                    # Remove old private polylines and texts
                    node[:] = [c for c in node if not (
                        isinstance(c, list) and c and c[0] in ('polyline', 'text')
                        and 'private' in c
                    )]
                    # Add rebuilt table
                    # node.extend(build_info_table(fields_order, cfg))
                    break
    else:
        # Fields-only: minimal symbol skeleton
        y_max = cfg['reference']['offset_y_above_graphic']  # safe default
        sym = [
            'symbol', f'"{output_name}"',
            ['pin_numbers', ['hide', 'yes']],
            ['pin_names', ['hide', 'yes']],
            ['exclude_from_sim', 'no'],
            ['in_bom', 'yes'],
            ['on_board', 'yes'],
        ]

    # --- Inject pin_numbers / pin_names hide flags on template symbols ---
    if template is not None:
        pn_hide = 'yes' if cfg['pin_numbers'].get('hide', True) else 'no'
        pm_hide = 'yes' if cfg['pin_names'].get('hide', True) else 'no'
        for node in sym:
            if isinstance(node, list) and node:
                if node[0] == 'pin_numbers':
                    node.clear(); node += ['pin_numbers', ['hide', pn_hide]]
                elif node[0] == 'pin_names':
                    node.clear(); node += ['pin_names', ['hide', pm_hide]]

    apply_pin_fonts(sym, cfg)

    # --- Build property list ---
    hc = cfg['hidden_fields']
    x_hidden = hc['start_x']
    y_start = hc['start_y']
    step = hc['step_y']

    properties = []

    # Reference — use the default from the template if available, else 'R?'
    ref_default = 'R?'
    if template is not None:
        for n in template:
            if isinstance(n, list) and n and n[0] == 'property' and n[1].strip('"') == 'Reference':
                ref_default = n[2].strip('"')
                break
    properties.append(make_reference_property(cfg, y_max, ref_default))

    # Value
    properties.append(make_value_property(row.get('Value', output_name), cfg, y_max))

    # Hidden fields — in config-defined order, then any extra CSV columns
    hidden_fields_seen = []
    for i, fname in enumerate(fields_order):
        val = row.get(fname, '')
        y = round(y_start + step * i, 6)
        properties.append(make_hidden_property(fname, val, x_hidden, y, cfg))
        hidden_fields_seen.append(fname)

    # Any extra CSV columns not in fields_order and not in skip list
    extra_i = len(fields_order)
    for fname, val in row.items():
        if fname in SKIP_FIELDS or fname in hidden_fields_seen or fname == 'Symbol':
            continue
        if not fname or not val:
            continue
        y = round(y_start + step * extra_i, 6)
        properties.append(make_hidden_property(fname, val, x_hidden, y, cfg))
        extra_i += 1

    # Insert properties right after the header flags
    # Find insertion point: after 'on_board' or similar flags
    insert_at = 1  # right after 'symbol' keyword and name
    new_sym = [sym[0], sym[1]]
    flags_done = False
    rest = []
    for node in sym[2:]:
        if isinstance(node, list) and node and node[0] in (
            'pin_numbers', 'pin_names', 'exclude_from_sim', 'in_bom', 'on_board', 'embedded_fonts'
        ):
            new_sym.append(node)
            flags_done = True
        else:
            rest.append(node)
    new_sym.extend(properties)
    new_sym.extend(rest)
    return new_sym


# ---------------------------------------------------------------------------
# Library builder
# ---------------------------------------------------------------------------

def build_library(templates_path: str, csv_files: list, output_path: str, config_path: str):
    cfg = load_config(config_path)

    print(f"Loading templates from : {templates_path}")
    lib_tree = load_kicad_sym(templates_path)
    available = get_symbol_names(lib_tree)
    print(f"Available templates    : {available}")

    output_symbols = []
    errors = []
    total = 0

    for csv_path in csv_files:
        print(f"\nProcessing: {csv_path}")
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):
                row = {k.strip(): v.strip() for k, v in row.items() if k}

                mpn = row.get('MPN', '').strip()
                sym_name = row.get('Symbol', '').strip()

                if not mpn:
                    errors.append(f"{csv_path}:{i} — missing MPN, skipped")
                    continue

                if sym_name:
                    if sym_name not in available:
                        errors.append(
                            f"{csv_path}:{i} MPN={mpn} — template '{sym_name}' not found "
                            f"(available: {available}), skipping graphic"
                        )
                        template = None
                    else:
                        template = extract_template(lib_tree, sym_name)
                else:
                    template = None  # fields-only

                symbol = generate_symbol(template, row, mpn, cfg)
                output_symbols.append(symbol)
                total += 1
                tag = f"({sym_name})" if sym_name else "(fields-only)"
                print(f"  + {mpn} {tag}")

    header = [
        ['version', '20241209'],
        ['generator', '"generate_symbols.py"'],
        ['generator_version', '"2.0"'],
    ]
    output_tree = ['kicad_symbol_lib'] + header + output_symbols

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(serialize(output_tree, 0))
        f.write('\n')

    print(f"\n{'='*60}")
    print(f"Generated {total} symbols → {output_path}")
    if errors:
        print(f"\nWarnings ({len(errors)}):")
        for e in errors:
            print(f"  ⚠  {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate KiCad 9 symbol library from templates + CSV.'
    )
    parser.add_argument('--templates', default='_templates.kicad_sym')
    parser.add_argument('--csv', nargs='+', default=None)
    parser.add_argument('--output', default='output_library.kicad_sym')
    parser.add_argument('--config', default='config.json')
    args = parser.parse_args()

    csv_files = args.csv or sorted(glob.glob('*.csv'))
    if not csv_files:
        print("Error: no CSV files found.")
        sys.exit(1)

    for p in [args.templates, args.config] + csv_files:
        if not Path(p).exists():
            print(f"Error: file not found: {p}")
            sys.exit(1)

    build_library(args.templates, csv_files, args.output, args.config)


if __name__ == '__main__':
    main()
