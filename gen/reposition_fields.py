#!/usr/bin/env python3
"""
reposition_fields.py
--------------------
Reads an existing .kicad_sym file and normalises ALL symbols to the standard
field set and layout defined in config.json — without touching graphic primitives.

What it does:
  - Keeps ONLY the fields listed in config fields_order + Reference + Value
  - Any field not in that list is DISCARDED
  - If a field is missing from the symbol, it is added with value "~"
  - Reference  → y_max_of_graphic + reference.offset_y_above_graphic (x=0, visible)
  - Value      → y_max_of_graphic + value.offset_y_above_graphic     (x=0, visible)
  - All other fields → fixed hidden positions from config (fields_order list)
  - Adds do_not_autoplace to Reference and Value
  - Enforces pin_numbers / pin_names hide flags from config
  - Rebuilds the private info-table graphic to match fields_order exactly

Usage:
    python reposition_fields.py --input my_lib.kicad_sym --output my_lib_fixed.kicad_sym
    python reposition_fields.py --input my_lib.kicad_sym   # overwrites in-place
"""

import argparse
import copy
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Re-use parser/serializer from generate_symbols (or inline them here)
# ---------------------------------------------------------------------------

def tokenize(text):
    tokens = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('('); i += 1
        elif c == ')':
            tokens.append(')'); i += 1
        elif c == '"':
            j = i + 1
            result = []
            while j < len(text):
                if text[j] == '\\' and j + 1 < len(text):
                    result.append(text[j+1]); j += 2
                elif text[j] == '"':
                    break
                else:
                    result.append(text[j]); j += 1
            tokens.append('"' + ''.join(result) + '"')
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in ' \t\n\r()"':
                j += 1
            tokens.append(text[i:j]); i = j
    return tokens


def parse_sexpr(tokens, pos=0):
    if tokens[pos] == '(':
        pos += 1
        node = []
        while tokens[pos] != ')':
            child, pos = parse_sexpr(tokens, pos)
            node.append(child)
        return node, pos + 1
    else:
        return tokens[pos], pos + 1


def load_kicad_sym(path):
    text = Path(path).read_text(encoding='utf-8')
    tokens = tokenize(text)
    tree, _ = parse_sexpr(tokens, 0)
    return tree


SINGLE_LINE = {
    'pts','xy','at','start','end','mid',
    'stroke','fill','effects','font','justify','offset','size',
    'hide','do_not_autoplace',
}

def serialize(node, indent=0):
    if isinstance(node, str):
        return node
    if not node:
        return '()'
    head = node[0]
    if isinstance(head, str) and head in SINGLE_LINE:
        return '(' + ' '.join(serialize(c, 0) for c in node) + ')'
    pad = '\t' * (indent + 1)
    parts = [serialize(c, indent+1) for c in node]
    single = '(' + ' '.join(parts) + ')'
    if len(single) <= 100 and '\n' not in single:
        return single
    lines = ['(' + parts[0]]
    for p in parts[1:]:
        lines.append(pad + p)
    lines[-1] += ')'
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Bounding box
# ---------------------------------------------------------------------------

def _collect_xy(node):
    coords = []
    if isinstance(node, list):
        if node and node[0] in ('xy', 'start', 'end', 'mid', 'center') and len(node) >= 3:
            try:
                coords.append((float(node[1]), float(node[2])))
            except ValueError:
                pass
        for child in node:
            coords.extend(_collect_xy(child))
    return coords


def compute_y_max(symbol):
    """Max Y among non-private graphic primitives across all sub-symbols."""
    y_vals = []
    for node in symbol:
        if not isinstance(node, list) or not node or node[0] != 'symbol':
            continue
        for child in node[1:]:
            if not isinstance(child, list) or not child:
                continue
            prim = child[0]
            if prim not in ('polyline', 'arc', 'circle', 'rectangle', 'bezier'):
                continue
            if 'private' in child:
                continue
            for xy in _collect_xy(child):
                y_vals.append(xy[1])
    return max(y_vals) if y_vals else 1.016


# ---------------------------------------------------------------------------
# Info table builder (same as generate_symbols)
# ---------------------------------------------------------------------------

def build_info_table(fields_in_order, cfg):
    hc = cfg['hidden_fields']
    ic = cfg['info_table']
    if not ic.get('enabled', True):
        return []

    x0  = ic['line_start_x']
    x1  = hc['start_x']
    y_top = hc['start_y']
    step  = hc['step_y']
    lx    = ic['label_x']
    lfs   = str(ic['label_font_size'])
    n     = len(fields_in_order)
    y_bot = y_top + step * n

    nodes = []
    nodes.append([
        'polyline', 'private',
        ['pts', ['xy', str(x0),'0'], ['xy', str(x0), str(y_bot)], ['xy', str(x1), str(y_bot)]],
        ['stroke', ['width','0'], ['type','solid']],
        ['fill', ['type','none']],
    ])
    nodes.append([
        'polyline', 'private',
        ['pts', ['xy', str(x1), str(y_top)], ['xy', str(x0), str(y_top)],
         ['xy', str(x0),'0'], ['xy','0','0']],
        ['stroke', ['width','0'], ['type','solid']],
        ['fill', ['type','none']],
    ])
    for i in range(1, n):
        y = round(y_top + step * i, 6)
        nodes.append([
            'polyline', 'private',
            ['pts', ['xy', str(x1), str(y)], ['xy', str(x0), str(y)]],
            ['stroke', ['width','0'], ['type','solid']],
            ['fill', ['type','none']],
        ])
    for i, fname in enumerate(fields_in_order):
        y_row_top = y_top + step * i
        y_row_bot = y_top + step * (i+1)
        y_label = round((y_row_top + y_row_bot) / 2 + 0.508, 6)
        nodes.append([
            'text', 'private', f'"{fname}"',
            ['at', str(lx), str(y_label), '0'],
            ['effects', ['font', ['size', lfs, lfs]]],
        ])
    return nodes


# ---------------------------------------------------------------------------
# Core: normalise and reposition a single symbol
# ---------------------------------------------------------------------------

def build_property(name, value, x, y, font_size, visible, justify=None, do_not_autoplace=False, cfg=None):
    """Build a fresh property node from scratch."""
    s = str(font_size)
    effects = ['effects', ['font', ['size', s, s]]]
    if justify:
        effects.append(['justify', justify])
    if not visible:
        effects.append(['hide', 'yes'])
    node = ['property', f'"{name}"', f'"{value}"', ['at', str(x), str(y), '0'], effects]
    if do_not_autoplace and cfg and not cfg.get('allow_kicad_autoplace', False):
        node.append(['do_not_autoplace'])
    return node


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


def reposition_symbol(sym, cfg):
    """
    Normalise a symbol to the standard field set from config.fields_order.
    - Keeps only Reference, Value, and fields listed in fields_order
    - Fields missing from the symbol are added with value "~"
    - Fields NOT in the standard set are discarded
    - Repositions everything per config layout rules
    - Does NOT touch graphic primitives
    Returns a new symbol node.
    """
    sym = copy.deepcopy(sym)
    fields_order = cfg.get('fields_order', [
        'Footprint', 'Datasheet', 'Manufacturer', 'Package', 'MPN', 'Description'
    ])
    hc = cfg['hidden_fields']
    rc = cfg['reference']
    vc = cfg['value']

    y_max = compute_y_max(sym)

    # --- Collect existing property values (we only keep their VALUES, not nodes) ---
    existing_values = {}
    for node in sym:
        if isinstance(node, list) and node and node[0] == 'property':
            name = node[1].strip('"')
            existing_values[name] = node[2].strip('"')

    # --- Remove ALL existing property nodes from sym ---
    sym = [n for n in sym if not (isinstance(n, list) and n and n[0] == 'property')]

    # --- Build standard properties from scratch ---
    new_props = []

    # Reference — always has a real value
    y_ref = round(y_max + rc['offset_y_above_graphic'], 6)
    new_props.append(build_property(
        'Reference',
        existing_values.get('Reference', 'R?'),
        x=rc['x'], y=y_ref,
        font_size=rc['font_size'],
        visible=rc.get('visible', True),
        do_not_autoplace=rc.get('do_not_autoplace', True),
        cfg=cfg,
    ))

    # Value — always has a real value
    y_val = round(y_max + vc['offset_y_above_graphic'], 6)
    new_props.append(build_property(
        'Value',
        existing_values.get('Value', '~'),
        x=vc['x'], y=y_val,
        font_size=vc['font_size'],
        visible=vc.get('visible', True),
        do_not_autoplace=vc.get('do_not_autoplace', True),
        cfg=cfg,
    ))

    # Hidden fields — standard order, fill with ~ if missing
    x_h = hc['start_x']
    y_h = hc['start_y']
    step = hc['step_y']
    justify = hc.get('justify', 'left')
    fs_h = hc['font_size']

    for i, fname in enumerate(fields_order):
        y = round(y_h + step * i, 6)
        value = existing_values.get(fname, '~') or '~'
        new_props.append(build_property(
            fname, value,
            x=x_h, y=y,
            font_size=fs_h,
            visible=False,
            justify=justify,
            cfg=cfg,
        ))

    # --- Insert properties right after header flags ---
    insert_after = {'pin_numbers', 'pin_names', 'exclude_from_sim', 'in_bom', 'on_board', 'embedded_fonts'}
    header = []
    rest = []
    past_header = False
    for node in sym[2:]:  # skip 'symbol' keyword and name
        if not past_header and isinstance(node, list) and node and node[0] in insert_after:
            header.append(node)
        else:
            past_header = True
            rest.append(node)
    sym = [sym[0], sym[1]] + header + new_props + rest

    # --- Strip any leftover private elements from _1_1 sub-symbol ---
    for node in sym:
        if isinstance(node, list) and node and node[0] == 'symbol':
            sub = node[1].strip('"') if isinstance(node[1], str) else node[1]
            if sub.endswith('_1_1'):
                node[:] = [c for c in node if not (
                    isinstance(c, list) and c and c[0] in ('polyline', 'text')
                    and 'private' in c
                )]
                # Add rebuilt table
                # node.extend(build_info_table(fields_order, cfg))
                break

    # --- pin_numbers / pin_names hide ---
    pn_hide = 'yes' if cfg['pin_numbers'].get('hide', True) else 'no'
    pm_hide = 'yes' if cfg['pin_names'].get('hide', True) else 'no'
    for node in sym:
        if isinstance(node, list) and node:
            if node[0] == 'pin_numbers':
                node.clear(); node += ['pin_numbers', ['hide', pn_hide]]
            elif node[0] == 'pin_names':
                node.clear(); node += ['pin_names', ['hide', pm_hide]]

    apply_pin_fonts(sym, cfg)

    return sym


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Reposition fields in an existing .kicad_sym according to config.json'
    )
    parser.add_argument('--input',  required=True, help='Input .kicad_sym file')
    parser.add_argument('--output', default=None,  help='Output file (default: overwrite input)')
    parser.add_argument('--config', default='config.json')
    args = parser.parse_args()

    output_path = args.output or args.input

    for p in [args.input, args.config]:
        if not Path(p).exists():
            print(f"Error: file not found: {p}")
            sys.exit(1)

    cfg = load_config(args.config)
    lib = load_kicad_sym(args.input)

    updated = 0
    new_lib = []
    for node in lib:
        if isinstance(node, list) and node and node[0] == 'symbol':
            name = node[1].strip('"') if isinstance(node[1], str) else node[1]
            new_lib.append(reposition_symbol(node, cfg))
            print(f"  ✓ {name}")
            updated += 1
        else:
            new_lib.append(node)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(serialize(new_lib, 0))
        f.write('\n')

    print(f"\n{'='*60}")
    print(f"Repositioned {updated} symbols → {output_path}")


if __name__ == '__main__':
    main()
