"""Stationary-stamping print planner: text or a bitmap to carriage stops and record words.

Shared by the host tool (tools/stamp_print.py, later) and the emulator suite
(tests/validate_stamp.py). The nozzle mapping is a parameter object so the
hypothesis from brief 8 (eight nozzles per mask bit, cleared bit fires, bit 0
of word A nearest the trailing edge) can be replaced by the measured map from
the mask-mapping sheet without touching the planner or the renderer.

Geometry (measured 2026-10-25): carriage 9600 counts/inch nominal (16 counts
per 600 dpi column), feed 1800 counts/inch nominal (3 counts per 600 dpi
row). Positive carriage counts run toward the printer's left; the sheet
spans about 29000 to 110000 counts; paper advances toward the leading edge,
marks printed later land nearer the trailing edge.
"""
from dataclasses import dataclass, field

ALL = 0xffffffff
COUNTS_PER_COLUMN = 16          # carriage counts per 600 dpi column
COUNTS_PER_ROW = 3              # feed counts per 600 dpi row

# 5x7 glyphs, column-major, bit 0 = top row. Enough for "HELLO WORLD".
FONT = {
    'H': [0x7f, 0x08, 0x08, 0x08, 0x7f], 'E': [0x7f, 0x49, 0x49, 0x49, 0x41],
    'L': [0x7f, 0x40, 0x40, 0x40, 0x40], 'O': [0x3e, 0x41, 0x41, 0x41, 0x3e],
    'W': [0x7f, 0x20, 0x18, 0x20, 0x7f], 'R': [0x7f, 0x09, 0x19, 0x29, 0x46],
    'D': [0x7f, 0x41, 0x41, 0x22, 0x1c], ' ': [0, 0, 0, 0, 0],
    'A': [0x7e, 0x09, 0x09, 0x09, 0x7e], 'I': [0x00, 0x41, 0x7f, 0x41, 0x00],
    'N': [0x7f, 0x04, 0x08, 0x10, 0x7f], 'T': [0x01, 0x01, 0x7f, 0x01, 0x01],
    'S': [0x46, 0x49, 0x49, 0x49, 0x31], 'C': [0x3e, 0x41, 0x41, 0x41, 0x22],
    'P': [0x7f, 0x09, 0x09, 0x09, 0x06], 'M': [0x7f, 0x02, 0x04, 0x02, 0x7f],
    'U': [0x3f, 0x40, 0x40, 0x40, 0x3f], 'G': [0x3e, 0x41, 0x49, 0x49, 0x3a],
    '.': [0x00, 0x60, 0x60, 0x00, 0x00], '!': [0x00, 0x00, 0x5f, 0x00, 0x00],
}


@dataclass
class NozzleMap:
    """How the two record words select black nozzle groups. Defaults are the map measured on
    2026-10-26 (app/evidence/head_fire/nozzle_map.json)."""
    nozzles_per_bit: int = 32                # one mask bit = this many adjacent nozzles (measured 2026-10-26)
    bits_word_a: int = 10                    # groups addressed by word A (0xc0c291e0); word B does nothing
    bits_word_b: int = 0
    cleared_fires: bool = True               # cleared bit = group fires (measured)
    bit0_at_trailing_edge: bool = False      # group 0 is at the leading-edge end (measured)
    pitch_rows: int = 1                      # 600 dpi rows per nozzle

    @property
    def groups(self): return self.bits_word_a + self.bits_word_b

    @property
    def group_rows(self): return self.nozzles_per_bit * self.pitch_rows   # paper rows per group

    def words(self, group_bits):
        """Record words for a set of group indexes that should fire."""
        a = b = 0
        for g in group_bits:
            if g < self.bits_word_a: a |= 1 << g
            else: b |= 1 << (g - self.bits_word_a)
        if self.cleared_fires:
            a, b = ALL & ~a, ALL & ~b
        return a, b

    def groups_from_words(self, word_a, word_b):
        """Inverse: which groups fire for these words (for the renderer)."""
        a, b = (ALL & ~word_a, ALL & ~word_b) if self.cleared_fires else (word_a, word_b)
        return [g for g in range(self.bits_word_a) if a >> g & 1] + \
               [self.bits_word_a + g for g in range(self.bits_word_b) if b >> g & 1]


@dataclass
class Stop:
    x: int                      # carriage target, counts
    word_a: int
    word_b: int
    columns: list = field(default_factory=list)   # pixel columns this stop prints (for reports)


def text_bitmap(text, scale=1, gap=1):
    """Bitmap as a list of columns; each column is a list of row booleans (top first)."""
    cols = []
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[' '])
        for c in glyph:
            cols += [[bool(c >> r & 1) for r in range(7) for _ in range(scale)]] * scale
        cols += [[False] * (7 * scale)] * gap * scale
    return cols


def plan(columns, nmap, x0, pixel_groups=1, column_counts=None, row_group0=0, mirror=True):
    """Stops for one pass. Each pixel row maps to `pixel_groups` consecutive nozzle
    groups (vertical pixel size = pixel_groups * nmap.group_rows rows); each bitmap
    column becomes one stop `column_counts` counts apart (default: one pixel wide =
    pixel_groups * group_rows columns, i.e. square pixels)."""
    if column_counts is None:
        column_counts = pixel_groups * nmap.group_rows * COUNTS_PER_COLUMN
    # Reading orientation: the sheet is read with its leading edge at the top, so the text's
    # top row is the group nearest the leading edge and its left column is at the highest
    # carriage count (counts rise toward the printer's left). `mirror` places column 0 at
    # x0 and later columns at lower counts; entries are returned sorted by x as the job needs.
    stops = []
    for i, col in enumerate(columns):
        groups = []
        for r, on in enumerate(col):
            if on:
                base = row_group0 + r * pixel_groups
                groups += [base + k for k in range(pixel_groups)]
        if not groups: continue
        if max(groups) >= nmap.groups: raise ValueError('bitmap taller than the head')
        top_first = not nmap.bit0_at_trailing_edge
        groups = groups if top_first else [nmap.groups - 1 - g for g in groups]
        a, b = nmap.words(groups)
        x = x0 - i * column_counts if mirror else x0 + i * column_counts
        stops.append(Stop(x, a, b, [i]))
    return sorted(stops, key=lambda st: st.x)


class VirtualPaper:
    """Dots painted from fires: (carriage x, feed y, words) -> 600 dpi dot grid.
    Rows run along the paper axis in feed counts (3 per row); columns across
    in carriage counts (16 per column). One fire of a group paints
    `nozzles_per_bit` rows at the stop's column."""
    def __init__(self, nmap):
        self.nmap = nmap; self.dots = set()      # (column, row) at 600 dpi

    def fire(self, x_counts, y_counts, word_a, word_b):
        col = x_counts // COUNTS_PER_COLUMN
        for g in self.nmap.groups_from_words(word_a, word_b):
            r0 = y_counts // COUNTS_PER_ROW + g * self.nmap.group_rows
            for r in range(r0, r0 + self.nmap.group_rows):
                self.dots.add((col, r))

    def bitmap(self, col0, row0, width, height):
        return [[(col0 + c, row0 + r) in self.dots for c in range(width)] for r in range(height)]

    def ascii(self, col0, row0, width, height, step_c=1, step_r=1):
        return '\n'.join(''.join('#' if (col0 + c, row0 + r) in self.dots else '.' for c in range(0, width, step_c))
                         for r in range(0, height, step_r))
