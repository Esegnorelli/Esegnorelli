"""Turn text into SVG glyph outlines, using only the standard library.

The profile images are served as <img>, where webfonts are unreliable, so
every letter is drawn as a path read straight from a TrueType file. Each
glyph is defined once in <defs> and reused with <use>.
"""

import struct


class Font:
    def __init__(self, path: str, key: str):
        self.key = key
        self.data = data = open(path, "rb").read()
        count = struct.unpack(">H", data[4:6])[0]
        self.tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            self.tables[tag.decode("latin-1")] = offset

        head = self.tables["head"]
        self.upm = struct.unpack(">H", data[head + 18: head + 20])[0]
        long_loca = struct.unpack(">h", data[head + 50: head + 52])[0] == 1

        maxp = self.tables["maxp"]
        glyphs = struct.unpack(">H", data[maxp + 4: maxp + 6])[0]

        hhea = self.tables["hhea"]
        self.ascent, self.descent = struct.unpack(">hh", data[hhea + 4: hhea + 8])
        metrics = struct.unpack(">H", data[hhea + 34: hhea + 36])[0]
        hmtx = self.tables["hmtx"]
        self.advances = [
            struct.unpack(">H", data[hmtx + 4 * i: hmtx + 4 * i + 2])[0] for i in range(metrics)
        ]

        loca = self.tables["loca"]
        if long_loca:
            self.loca = struct.unpack(f">{glyphs + 1}I", data[loca: loca + 4 * (glyphs + 1)])
        else:
            short = struct.unpack(f">{glyphs + 1}H", data[loca: loca + 2 * (glyphs + 1)])
            self.loca = [value * 2 for value in short]
        self.glyf = self.tables["glyf"]
        self.cmap = self._read_cmap()
        self._contours = {}
        self._paths = {}

    # -- character map -----------------------------------------------------

    def _read_cmap(self) -> dict:
        data, base = self.data, self.tables["cmap"]
        count = struct.unpack(">H", data[base + 2: base + 4])[0]
        subtables = {}
        for i in range(count):
            platform, encoding, offset = struct.unpack(">HHI", data[base + 4 + 8 * i: base + 12 + 8 * i])
            subtables[(platform, encoding)] = base + offset
        for key in [(3, 10), (0, 4), (3, 1), (0, 3)]:
            if key not in subtables:
                continue
            offset = subtables[key]
            fmt = struct.unpack(">H", data[offset: offset + 2])[0]
            if fmt == 12:
                return self._cmap12(offset)
            if fmt == 4:
                return self._cmap4(offset)
        raise ValueError("font has no usable cmap")

    def _cmap4(self, offset: int) -> dict:
        data = self.data
        seg2 = struct.unpack(">H", data[offset + 6: offset + 8])[0]
        seg = seg2 // 2
        ends = struct.unpack(f">{seg}H", data[offset + 14: offset + 14 + seg2])
        at = offset + 16 + seg2
        starts = struct.unpack(f">{seg}H", data[at: at + seg2])
        at += seg2
        deltas = struct.unpack(f">{seg}h", data[at: at + seg2])
        at += seg2
        ranges = struct.unpack(f">{seg}H", data[at: at + seg2])
        mapping = {}
        for i in range(seg):
            for code in range(starts[i], ends[i] + 1):
                if code == 0xFFFF:
                    continue
                if ranges[i] == 0:
                    glyph = (code + deltas[i]) & 0xFFFF
                else:
                    addr = at + 2 * i + ranges[i] + 2 * (code - starts[i])
                    glyph = struct.unpack(">H", data[addr: addr + 2])[0]
                    if glyph:
                        glyph = (glyph + deltas[i]) & 0xFFFF
                if glyph:
                    mapping[code] = glyph
        return mapping

    def _cmap12(self, offset: int) -> dict:
        data = self.data
        groups = struct.unpack(">I", data[offset + 12: offset + 16])[0]
        mapping = {}
        for i in range(groups):
            start, end, glyph = struct.unpack(">III", data[offset + 16 + 12 * i: offset + 28 + 12 * i])
            for code in range(start, end + 1):
                mapping[code] = glyph + code - start
        return mapping

    # -- outlines ----------------------------------------------------------

    def glyph(self, char: str) -> int:
        return self.cmap.get(ord(char)) or self.cmap.get(ord("?"), 0)

    def advance(self, glyph: int) -> int:
        return self.advances[min(glyph, len(self.advances) - 1)]

    def contours(self, glyph: int) -> list:
        if glyph in self._contours:
            return self._contours[glyph]
        start, end = self.loca[glyph], self.loca[glyph + 1]
        result = []
        if end > start:
            at = self.glyf + start
            count = struct.unpack(">h", self.data[at: at + 2])[0]
            result = self._simple(at + 10, count) if count >= 0 else self._composite(at + 10)
        self._contours[glyph] = result
        return result

    def _simple(self, at: int, count: int) -> list:
        data = self.data
        ends = struct.unpack(f">{count}H", data[at: at + 2 * count])
        at += 2 * count
        at += 2 + struct.unpack(">H", data[at: at + 2])[0]
        points = ends[-1] + 1 if count else 0
        flags = []
        while len(flags) < points:
            flag = data[at]
            at += 1
            flags.append(flag)
            if flag & 8:
                flags.extend([flag] * data[at])
                at += 1
        flags = flags[:points]

        def coords(short_bit, same_bit):
            nonlocal at
            values, value = [], 0
            for flag in flags:
                if flag & short_bit:
                    step = data[at]
                    at += 1
                    value += step if flag & same_bit else -step
                elif not flag & same_bit:
                    value += struct.unpack(">h", data[at: at + 2])[0]
                    at += 2
                values.append(value)
            return values

        xs = coords(2, 16)
        ys = coords(4, 32)
        result, first = [], 0
        for last in ends:
            result.append([(xs[i], ys[i], bool(flags[i] & 1)) for i in range(first, last + 1)])
            first = last + 1
        return result

    def _composite(self, at: int) -> list:
        data, result = self.data, []
        while True:
            flags, child = struct.unpack(">HH", data[at: at + 4])
            at += 4
            if flags & 1:
                arg1, arg2 = struct.unpack(">hh", data[at: at + 4])
                at += 4
            else:
                arg1, arg2 = struct.unpack(">bb", data[at: at + 2])
                at += 2
            a, b, c, d = 1.0, 0.0, 0.0, 1.0
            if flags & 8:
                a = d = struct.unpack(">h", data[at: at + 2])[0] / 16384
                at += 2
            elif flags & 0x40:
                a, d = (v / 16384 for v in struct.unpack(">hh", data[at: at + 4]))
                at += 4
            elif flags & 0x80:
                a, b, c, d = (v / 16384 for v in struct.unpack(">hhhh", data[at: at + 8]))
                at += 8
            dx, dy = (arg1, arg2) if flags & 2 else (0, 0)
            for contour in self.contours(child):
                result.append([(a * x + c * y + dx, b * x + d * y + dy, on) for x, y, on in contour])
            if not flags & 0x20:
                return result

    def path(self, glyph: int) -> str:
        if glyph not in self._paths:
            self._paths[glyph] = "".join(_contour_path(c) for c in self.contours(glyph) if c)
        return self._paths[glyph]

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        if not text:
            return 0.0
        units = sum(self.advance(self.glyph(ch)) for ch in text)
        return units * size / self.upm + tracking * size * (len(text) - 1)


def _num(value: float) -> str:
    value = round(value, 1)
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _contour_path(points: list) -> str:
    first_on = next((i for i, p in enumerate(points) if p[2]), None)
    if first_on is None:
        start = ((points[-1][0] + points[0][0]) / 2, (points[-1][1] + points[0][1]) / 2)
        sequence = points
    else:
        start = points[first_on][:2]
        sequence = points[first_on + 1:] + points[:first_on]
    out = [f"M{_num(start[0])} {_num(start[1])}"]
    pending = None
    for x, y, on in sequence:
        if on:
            if pending:
                out.append(f"Q{_num(pending[0])} {_num(pending[1])} {_num(x)} {_num(y)}")
                pending = None
            else:
                out.append(f"L{_num(x)} {_num(y)}")
        else:
            if pending:
                mx, my = (pending[0] + x) / 2, (pending[1] + y) / 2
                out.append(f"Q{_num(pending[0])} {_num(pending[1])} {_num(mx)} {_num(my)}")
            pending = (x, y)
    if pending:
        out.append(f"Q{_num(pending[0])} {_num(pending[1])} {_num(start[0])} {_num(start[1])}")
    return "".join(out) + "Z"


class Glyphs:
    """Collects the glyphs a document uses and writes them once into <defs>."""

    def __init__(self):
        self._used = {}

    def ref(self, font: Font, glyph: int) -> str:
        key = f"{font.key}{glyph}"
        if key not in self._used:
            self._used[key] = font.path(glyph)
        return key

    def defs(self) -> str:
        return "".join(f'<path id="{k}" d="{d}"/>' for k, d in self._used.items() if d)

    def text(self, font: Font, text: str, size: float, x: float, y: float,
             anchor: str = "start", tracking: float = 0.0, cls: str = "") -> str:
        """Return a <g> that draws `text` with its baseline at (x, y)."""
        width = font.width(text, size, tracking)
        if anchor == "middle":
            x -= width / 2
        elif anchor == "end":
            x -= width
        scale = size / font.upm
        step = tracking * font.upm
        cursor, uses = 0.0, []
        for ch in text:
            glyph = font.glyph(ch)
            if font.path(glyph):
                uses.append(f'<use href="#{self.ref(font, glyph)}" x="{_num(cursor)}"/>')
            cursor += font.advance(glyph) + step
        klass = f' class="{cls}"' if cls else ""
        return (f'<g{klass} transform="translate({x:.1f} {y:.1f}) scale({scale:.5f} {-scale:.5f})">'
                + "".join(uses) + "</g>")
