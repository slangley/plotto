#!/usr/bin/env python3
"""
Convert plotto.txt to a machine-readable JSON file for story generation.

Parses the Plotto source text and produces a structured JSON with:
- A/B/C clauses
- Character symbols
- B-clause index
- All 1,462 conflicts with parsed internal links, descriptions, and sub-variants
"""

import json
import re
import sys
import os


def parse_link_ref(link_text):
    """Parse a single link reference like '347a -*' into structured data.

    Returns a dict with:
      - conflict_id: int
      - sub_ids: list of str (e.g. ['a', 'b'])
      - modifiers: list of str (e.g. ['ch A to B', '-*'])
    """
    link_text = link_text.strip()
    if not link_text:
        return None

    result = {
        "conflict_id": None,
        "sub_ids": [],
        "modifiers": [],
    }

    # Extract the conflict number
    m = re.match(r'^(\d+)', link_text)
    if not m:
        return None
    result["conflict_id"] = int(m.group(1))
    rest = link_text[m.end():]

    # Extract sub-ids like 'a', 'a, b, c'
    m = re.match(r'^([a-h](?:,\s*[a-h])*)', rest)
    if m:
        sub_ids_str = m.group(1)
        result["sub_ids"] = [s.strip() for s in sub_ids_str.split(',')]
        rest = rest[m.end():]

    # The remainder is modifiers (trim whitespace)
    rest = rest.strip()
    if rest:
        result["modifiers"] = [rest]

    return result


def parse_sequence(text):
    """Parse a sequence like '521; 1177' into a list of refs."""
    if ';' in text:
        parts = text.split(';')
        refs = [parse_link_ref(p.strip()) for p in parts]
        refs = [r for r in refs if r]
        if len(refs) > 1:
            return {"type": "sequence", "refs": refs}
        elif len(refs) == 1:
            return {"type": "single", "ref": refs[0]}
    ref = parse_link_ref(text.strip())
    if ref:
        return {"type": "single", "ref": ref}
    return None


def parse_link_group(group_text):
    """Parse a parenthesized link group like '(347a -*; 112 ch A to B)'.

    Handles:
      - Alternations separated by ' or ' (pick one)
      - Sequences separated by ';' (do all in order)
      - Single references
      - Mixed: '521; 1177 or 1178' = (sequence 521,1177) OR 1178
    """
    group_text = group_text.strip()

    # Check for alternation (or) first - splits at highest level
    if ' or ' in group_text:
        parts = group_text.split(' or ')
        alternatives = []
        for part in parts:
            parsed = parse_sequence(part.strip())
            if parsed:
                alternatives.append(parsed)
        if len(alternatives) > 1:
            return {"type": "alternation", "alternatives": alternatives}
        elif len(alternatives) == 1:
            return alternatives[0]
        return None

    # Check for sequence (;)
    return parse_sequence(group_text)


def parse_links_line(line):
    """Parse a line of link groups like '(112) (117) (148 ch A to B)'.

    Returns a list of parsed link groups.
    """
    links = []
    # Find all parenthesized groups
    for m in re.finditer(r'\(([^)]*)\)', line):
        content = m.group(1).strip()
        if not content:
            continue
        parsed = parse_link_group(content)
        if parsed:
            links.append(parsed)
    return links


def parse_inline_links(text):
    """Parse inline conflict references within description text.

    Returns the text with inline references identified, plus a list of
    inline link refs found.
    """
    inline_refs = []
    # Find parenthesized references where first char is a digit
    for m in re.finditer(r'\((\d[^)]*)\)', text):
        content = m.group(1).strip()
        parsed = parse_link_group(content)
        if parsed:
            inline_refs.append({
                "text": m.group(0),
                "parsed": parsed,
            })
    return inline_refs


def parse_description_sections(text):
    """Split a description by asterisk section markers.

    Returns a list of sections. Asterisks serve as dividers:
      * = end of section 1
      ** = end of section 2
      *** = end of section 3
    """
    # Normalize multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()

    # Split on *** first, then **, then *
    # We need to split carefully: *** before ** before *
    sections = []
    remaining = text

    # Find section breaks: ***, **, *
    # Use a regex that matches the markers as standalone (space-bounded or at edges)
    parts = re.split(r'\s*\*\*\*\s*', remaining, maxsplit=1)
    if len(parts) == 2:
        before_triple = parts[0]
        after_triple = parts[1]
        # Split before_triple on **
        sub_parts = re.split(r'\s*\*\*\s*', before_triple, maxsplit=1)
        if len(sub_parts) == 2:
            # Split first part on *
            sub_sub = re.split(r'\s*\*\s*', sub_parts[0], maxsplit=1)
            sections = sub_sub + [sub_parts[1]] + [after_triple]
        else:
            sub_sub = re.split(r'\s*\*\s*', sub_parts[0], maxsplit=1)
            sections = sub_sub + [after_triple]
    else:
        # No ***
        parts = re.split(r'\s*\*\*\s*', remaining, maxsplit=1)
        if len(parts) == 2:
            sub_parts = re.split(r'\s*\*\s*', parts[0], maxsplit=1)
            sections = sub_parts + [parts[1]]
        else:
            sub_parts = re.split(r'\s*\*\s*', parts[0], maxsplit=1)
            sections = sub_parts

    return [s.strip() for s in sections if s.strip()]


class PlottoParser:
    def __init__(self):
        self.a_clauses = []
        self.b_clauses = []
        self.c_clauses = []
        self.b_clause_index = []
        self.character_symbols = []
        self.conflicts = {}

        # Parser state
        self.current_group = ""
        self.current_subgroup = ""
        self.current_bclause_id = ""
        self.current_bclause_name = ""
        self.current_conflict_id = ""
        self.current_subid = ""
        self.current_pre_links = []
        self.current_text_lines = []
        self.in_conflict_section = False
        self.in_conflict = False
        self.page = 0

        # Section parsing state
        self.section = None  # 'a_clauses', 'b_clauses', 'c_clauses', 'index', 'symbols', 'symbols_note'
        self.format_lines = None
        self.format_paragraph = None
        self.format_next_line = None

        # HER/A/U disambiguation (carried from comment lines)
        self.her_info = None
        self.a_info = None
        self.u_info = None

    def finalize_sub_conflict(self, post_links):
        """Store the current sub-conflict variant."""
        if not self.current_conflict_id:
            return

        cid = self.current_conflict_id
        if cid not in self.conflicts:
            self.conflicts[cid] = {
                "id": int(cid),
                "group": self.current_group,
                "subgroup": self.current_subgroup,
                "b_clause_id": int(self.current_bclause_id) if self.current_bclause_id else None,
                "b_clause": self.current_bclause_name,
                "sub_conflicts": [],
            }

        # Build the description from accumulated text lines
        desc_text = ' '.join([line.strip() for line in self.current_text_lines])
        desc_text = re.sub(r'\s+', ' ', desc_text).strip()

        # Parse inline links within the description
        inline_links = parse_inline_links(desc_text)

        # Parse description sections (split by *)
        sections = parse_description_sections(desc_text)

        sub_conflict = {
            "sub_id": self.current_subid if self.current_subid else None,
            "pre_links": self.current_pre_links,
            "description": desc_text,
            "description_sections": sections,
            "inline_links": [il["parsed"] for il in inline_links],
            "post_links": post_links,
        }

        self.conflicts[cid]["sub_conflicts"].append(sub_conflict)
        self.current_text_lines = []
        self.in_conflict = False

    def parse_b_clause_index_line(self, line):
        """Parse a B-clause index line like:
        (1) Love and Courtship @{110}
        """
        line = line.strip()
        m = re.match(r'^\((\d+)\)\s+(.*)$', line)
        if not m:
            return
        clause_id = int(m.group(1))
        rest = m.group(2)

        # Parse references: "Category @{number}" separated by ; or ,
        entries = []
        # Split on ';' first for major separations
        for segment in re.split(r';\s*', rest):
            segment = segment.strip()
            if not segment:
                continue
            # A segment might be "Love and Courtship @{110}, @{200}"
            # or "Married Life @{369}, @{386}"
            # Extract category name (text before first @{})
            parts = re.split(r'@\{(\d+[a-h]?(?:\s*[-*]+)?)\}', segment)
            if len(parts) < 2:
                continue
            category = parts[0].strip().rstrip(',').strip()
            # Collect all conflict refs
            for i in range(1, len(parts), 2):
                ref_str = parts[i].strip()
                ref_id = int(re.match(r'(\d+)', ref_str).group(1))
                entries.append({
                    "category": category if category else entries[-1]["category"] if entries else "",
                    "conflict_id": ref_id,
                })
                # After a ref, the next text part might have a new category
                if i + 1 < len(parts):
                    next_text = parts[i + 1].strip().lstrip(',').strip()
                    if next_text:
                        category = next_text

        self.b_clause_index.append({
            "b_clause_id": clause_id,
            "entries": entries,
        })

    def parse_character_symbol_line(self, line):
        """Parse a character symbol line like 'A-2,    male friend of A'"""
        line = line.strip()
        m = re.match(r'^([A-Za-z][-A-Za-z0-9]*),\s+(.*)$', line)
        if m:
            self.character_symbols.append({
                "symbol": m.group(1),
                "description": m.group(2),
            })

    def process(self, filepath):
        with open(filepath, 'r') as f:
            lines = f.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].rstrip('\n')
            self.process_line(line)
            i += 1

        # Finalize any in-progress data
        return self.build_output()

    def process_line(self, line):
        stripped = line.strip()

        # Handle comment/directive lines
        if stripped.startswith('--'):
            self.handle_directive(stripped)
            return

        # Handle A/B/C clause sections
        if self.section == 'a_clauses' and self.format_lines == 'bodylist':
            if stripped:
                m = re.match(r'^(\d+)\.\s+(.+?)(?:,\s*)?$', stripped)
                if m:
                    self.a_clauses.append({
                        "id": int(m.group(1)),
                        "description": m.group(2).strip().rstrip(','),
                    })
            return

        if self.section == 'b_clauses' and self.format_lines == 'bodylist':
            if stripped:
                m = re.match(r'^\((\d+)\)\s+(.+?)(?:,)?$', stripped)
                if m:
                    self.b_clauses.append({
                        "id": int(m.group(1)),
                        "description": m.group(2).strip().rstrip(','),
                    })
            return

        if self.section == 'c_clauses' and self.format_lines == 'bodylist':
            if stripped:
                m = re.match(r'^\((\d+)\)\s+(.+?)(?:,)?$', stripped)
                if m:
                    self.c_clauses.append({
                        "id": int(m.group(1)),
                        "description": m.group(2).strip().rstrip(','),
                    })
            return

        if self.section == 'index' and self.format_lines == 'bodylist':
            if stripped:
                self.parse_b_clause_index_line(stripped)
            return

        if self.section == 'symbols' and self.format_lines == 'bodylist' and not self.format_paragraph:
            if stripped:
                self.parse_character_symbol_line(stripped)
            return

        # Conflict section parsing
        if self.in_conflict_section:
            self.process_conflict_line(stripped)

    def handle_directive(self, line):
        # Page tracking
        m = re.match(r'^-- page (\d+)', line)
        if m:
            self.page = int(m.group(1))
            if self.page == 18:
                self.in_conflict_section = True
                self.section = None
            if self.page == 190:
                self.in_conflict_section = False
            return

        # HER disambiguation
        m = re.match(r'^-- HER (.+)', line)
        if m:
            self.her_info = m.group(1).split()
            return

        # Section IDs
        m = re.match(r'^-- ID:(.+)', line)
        if m:
            section_id = m.group(1)
            if section_id == 'a-clauses':
                self.section = 'a_clauses'
            elif section_id == 'b-clauses':
                self.section = 'b_clauses'
            elif section_id == 'c-clauses':
                self.section = 'c_clauses'
            elif section_id == 'index-b-clause-conflicts':
                self.section = 'index'
            elif section_id == 'character-symbols':
                self.section = 'symbols'
            return

        # FORMAT_LINES
        m = re.match(r'^-- FORMAT_LINES:(.+)', line)
        if m:
            self.format_lines = m.group(1)
            return

        # FORMAT_BEGIN / FORMAT_END
        m = re.match(r'^-- FORMAT_BEGIN:(.+)', line)
        if m:
            self.format_paragraph = m.group(1)
            self.format_lines = None
            return

        m = re.match(r'^-- FORMAT_END', line)
        if m:
            self.format_paragraph = None
            return

        # FORMAT (single line)
        m = re.match(r'^-- FORMAT:(.+)', line)
        if m:
            self.format_lines = None
            self.format_paragraph = None
            self.format_next_line = m.group(1)
            return

        # Other directives we skip
        return

    def process_conflict_line(self, line):
        if not line:
            return

        # ConflictGroup
        m = re.match(r'^ConflictGroup\{(.+)\}$', line)
        if m:
            self.current_group = m.group(1)
            return

        # ConflictSubGroup
        m = re.match(r'^ConflictSubGroup\{(.*)\}$', line)
        if m:
            self.current_subgroup = m.group(1)
            return

        # B clause header
        m = re.match(r'^B\{(\d+)\}\s+(.+)$', line)
        if m:
            self.current_bclause_id = m.group(1)
            self.current_bclause_name = m.group(2)
            return

        # New conflict
        m = re.match(r'^Conflict\{(\d+)\}$', line)
        if m:
            self.current_conflict_id = m.group(1)
            return

        # PRE line (with optional sub-id)
        m = re.match(r'^(?:\(([a-m])\)\s+)?PRE:\s+(.*)$', line)
        if m:
            self.current_subid = m.group(1) or ""
            self.current_pre_links = parse_links_line(m.group(2))
            self.current_text_lines = []
            self.in_conflict = True
            return

        # POST line
        m = re.match(r'^POST:\s+(.*)$', line)
        if m:
            post_links = parse_links_line(m.group(1))
            self.finalize_sub_conflict(post_links)
            return

        # Text line within a conflict description
        if self.in_conflict:
            self.current_text_lines.append(line)

    def build_output(self):
        # Sort conflicts by ID
        sorted_conflicts = []
        for cid in sorted(self.conflicts.keys(), key=lambda x: int(x)):
            sorted_conflicts.append(self.conflicts[cid])

        return {
            "title": "Plotto: A New Method of Plot Suggestion for Writers of Creative Fiction",
            "author": "William Wallace Cook",
            "year": 1928,
            "a_clauses": self.a_clauses,
            "b_clauses": self.b_clauses,
            "c_clauses": self.c_clauses,
            "b_clause_index": self.b_clause_index,
            "character_symbols": self.character_symbols,
            "conflicts": sorted_conflicts,
        }


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(script_dir, '..', 'plotto.txt')
    dst = os.path.join(script_dir, '..', 'plotto.json')

    if not os.path.isfile(src):
        print(f'Error: Source file not found: {src}', file=sys.stderr)
        sys.exit(1)

    parser = PlottoParser()
    data = parser.process(src)

    # Summary stats
    n_conflicts = len(data["conflicts"])
    n_sub = sum(len(c["sub_conflicts"]) for c in data["conflicts"])
    print(f'Parsed {n_conflicts} conflicts ({n_sub} sub-variants)')
    print(f'  A clauses: {len(data["a_clauses"])}')
    print(f'  B clauses: {len(data["b_clauses"])}')
    print(f'  C clauses: {len(data["c_clauses"])}')
    print(f'  Character symbols: {len(data["character_symbols"])}')
    print(f'  B-clause index entries: {len(data["b_clause_index"])}')

    with open(dst, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f'Written to {dst}')


if __name__ == '__main__':
    main()
