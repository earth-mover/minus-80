"""Building blocks for the SVG figures that draw an ingest: its source, its mapping and its store.

Why plain SVG text
------------------
Every ingest commits figures of what it read and what it wrote, drawn from
real values. Each figure is a handful of boxes, trees, arrows and array
cubes, so a plotting or graph-layout library would add a dependency and take
away control over where each label lands. These functions write SVG
elements as strings, and the figures they make diff cleanly in git.

How a figure is built
---------------------
A :class:`Canvas` collects elements. Each block (:func:`panel`,
:func:`tree`, :func:`link`, :func:`cube`, :func:`table`, :func:`legend`,
:func:`heading`) takes the canvas and a position, adds its elements, and
returns the y coordinate of its bottom edge. A figure is laid out with a
running y: the next block starts below the bottom the last one returned.
The canvas tracks the lowest edge any block reached and sizes the SVG to
it, so nothing is clipped however the content grows.

Data stays out of this module. An ingest's ``diagrams.py`` reads its source
listing and its store, and passes the values in as strings and numbers.

The style
---------
One light theme with an explicit white background, so a figure reads the
same in a dark-mode viewer. Status is a band color plus a one-word tag, never
color alone (:data:`STATUS`). Labels are short: a header or a field holds a
name or a value, never a sentence.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

FONT_SANS = "Inter, system-ui, sans-serif"
FONT_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

INK = "#0f172a"
MUTED = "#64748b"
RULE = "#cbd5e1"
BACKGROUND = "#ffffff"
PANEL_FILL = "#ffffff"
FACE_FILL = "#f1f5f9"


@dataclass(frozen=True)
class Status:
    """A row's status: the band behind it and the ink of its tag."""

    fill: str
    ink: str


#: The three states a figure marks. Each is shown with its one-word tag as well as its color.
STATUS = {
    "changed": Status("#fef3c7", "#b45309"),
    "added": Status("#dcfce7", "#15803d"),
    "removed": Status("#fee2e2", "#b91c1c"),
}

#: Accent colors for the kind of thing a block shows. These are the first three slots of a
#: categorical palette whose slots stay distinct under color-vision deficiency.
ACCENTS = {
    "source": "#2a78d6",
    "store": "#1baf7a",
    "highlight": "#eb6834",
}

#: Average glyph widths as a fraction of the font size, for laying out without measuring.
#: The sans figure is wide for Inter so that estimates err toward more room.
MONO_WIDTH = 0.61
SANS_WIDTH = 0.56


def text_width(s: str, size: float, *, mono: bool = False, bold: bool = False) -> float:
    """An estimate of how wide ``s`` renders, in pixels.

    SVG has no text measurement without a renderer, so layout uses an average
    glyph width. Monospace text is exact up to the font's advance; sans text is
    an overestimate.
    """
    width = len(s) * size * (MONO_WIDTH if mono else SANS_WIDTH)
    return width * 1.05 if bold else width


def fit(s: str, width: float, size: float, *, mono: bool = False) -> str:
    """``s``, cut at the end with an ellipsis if it would render wider than ``width``."""
    if text_width(s, size, mono=mono) <= width:
        return s
    keep = max(1, int(width / (size * (MONO_WIDTH if mono else SANS_WIDTH))) - 1)
    return s[:keep] + "…"


def text(
    x: float,
    y: float,
    s: str,
    *,
    size: float = 12,
    weight: str = "normal",
    mono: bool = False,
    color: str = INK,
    anchor: str = "start",
) -> str:
    """One ``<text>`` element with its baseline at ``y``. ``s`` is escaped, so any string is safe.

    Whitespace is preserved, so runs of spaces can align columns and indent keys.
    """
    family = FONT_MONO if mono else FONT_SANS
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}" xml:space="preserve">{escape(s)}</text>'
    )


def dims(shape: Sequence[int | str]) -> str:
    """A shape as ``4 × 271 × 1000 × 1000``."""
    return " × ".join(str(n) for n in shape)


def span(low: int | float, high: int | float) -> str:
    """``low–high``, or one number when they are equal."""
    return str(low) if low == high else f"{low}–{high}"


def size_text(n: int) -> str:
    """A byte count in decimal units, as ``591.3 GB``."""
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def count_text(n: int) -> str:
    """A count with thousands separators, as ``2,036,714``."""
    return f"{n:,}"


@dataclass
class Canvas:
    """The figure being drawn: its width, its elements, and the lowest edge any block reached."""

    width: float
    margin: float = 24
    parts: list[str] = field(default_factory=list)
    bottom: float = 0.0

    def add(self, element: str, bottom: float) -> float:
        """Add one element whose lowest edge is ``bottom``, and return ``bottom``."""
        self.parts.append(element)
        self.bottom = max(self.bottom, bottom)
        return bottom

    def text(self, x: float, y: float, s: str, **style) -> float:
        """Add :func:`text` at baseline ``y`` and return the bottom of its descenders."""
        return self.add(text(x, y, s, **style), y + style.get("size", 12) * 0.3)

    @property
    def height(self) -> int:
        return math.ceil(self.bottom + self.margin)

    def svg(self) -> str:
        """The whole figure, sized to its content, on a white background."""
        width = math.ceil(self.width)
        return "\n".join(
            [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{self.height}" '
                f'viewBox="0 0 {width} {self.height}">',
                f'<rect x="0" y="0" width="{width}" height="{self.height}" fill="{BACKGROUND}"/>',
                *self.parts,
                "</svg>",
                "",
            ]
        )

    def write(self, path: str | Path) -> int:
        """Write the SVG to ``path`` and return its size in bytes."""
        data = self.svg().encode()
        Path(path).write_bytes(data)
        return len(data)


def heading(canvas: Canvas, title: str, subtitle: str | None = None, *, y: float | None = None) -> float:
    """A figure's title, with an optional muted line under it, at the top left."""
    y = canvas.margin if y is None else y
    bottom = canvas.text(canvas.margin, y + 20, title, size=20, weight="bold")
    if subtitle:
        bottom = canvas.text(canvas.margin, y + 42, subtitle, size=13, color=MUTED, mono=True)
    return bottom


@dataclass(frozen=True)
class Row:
    """One field of a panel: a key and its value, optionally marked with a status and a one-word tag."""

    key: str
    value: str = ""
    status: str | None = None
    tag: str | None = None


ROW_HEIGHT = 20
PANEL_TITLE = 13
PANEL_ROW = 11.5


def panel(
    canvas: Canvas,
    x: float,
    y: float,
    width: float,
    title: str,
    rows: Sequence[Row],
    *,
    accent: str = ACCENTS["store"],
    source: str | None = None,
    key_width: float | None = None,
) -> float:
    """A box with an accent bar, a bold monospace title and one line per row.

    ``source`` names the file or array the panel's values come from, shown
    under the title. A row with a ``status`` gets that status's band across
    the panel and its ``tag`` at the right, so the status reads without color.
    Values too wide for the panel are cut with an ellipsis; keep them short.
    """
    keys = [row.key for row in rows]
    if key_width is None:
        key_width = max((text_width(k, PANEL_ROW, mono=True) for k in keys), default=0) + 12
    top_rows = y + 30 + (16 if source else 0)
    height = top_rows - y + len(rows) * ROW_HEIGHT + 6
    parts = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="6" '
        f'fill="{PANEL_FILL}" stroke="{RULE}"/>',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="4" height="{height:.1f}" rx="2" fill="{accent}"/>',
        text(x + 14, y + 20, fit(title, width - 24, PANEL_TITLE, mono=True), size=PANEL_TITLE, weight="bold", mono=True),
    ]
    if source:
        parts.append(text(x + 14, y + 37, fit(source, width - 24, 11), size=11, color=MUTED))
    for i, row in enumerate(rows):
        top = top_rows + i * ROW_HEIGHT
        baseline = top + 14
        tag_room = 0.0
        if row.status:
            status = STATUS[row.status]
            parts.append(
                f'<rect x="{x + 5:.1f}" y="{top:.1f}" width="{width - 6:.1f}" height="{ROW_HEIGHT}" '
                f'fill="{status.fill}"/>'
            )
            if row.tag:
                parts.append(
                    text(x + width - 10, baseline, row.tag, size=10.5, weight="bold", color=status.ink, anchor="end")
                )
                tag_room = text_width(row.tag, 10.5, bold=True) + 12
        parts.append(text(x + 14, baseline, row.key, size=PANEL_ROW, color=MUTED, mono=True))
        room = width - 14 - key_width - 10 - tag_room
        parts.append(text(x + 14 + key_width, baseline, fit(row.value, room, PANEL_ROW, mono=True), size=PANEL_ROW, mono=True))
    return canvas.add("\n".join(parts), y + height)


def panel_height(rows: int, *, source: bool = False) -> float:
    """The height :func:`panel` gives a panel with ``rows`` rows, for laying out before drawing."""
    return 30 + (16 if source else 0) + rows * ROW_HEIGHT + 6


@dataclass(frozen=True)
class TreeNode:
    """One node of a hierarchy: its name, the y of its line, its depth, and what it holds.

    ``count`` is how many nodes of this kind there are, drawn as ``×30`` after
    the name; ``subtitle`` is a short muted line under it.
    """

    name: str
    y: float
    depth: int = 0
    count: int | None = None
    subtitle: str | None = None
    status: str | None = None


TREE_INDENT = 22


def tree(canvas: Canvas, x: float, nodes: Sequence[TreeNode], *, leader_to: float | None = None) -> float:
    """A hierarchy: a dot, a name and a subtitle per node, with elbows from each node to its parent.

    Each node's y is given, so a tree can line up with panels drawn beside it.
    With ``leader_to``, a dashed leader runs from each node's name to that x,
    where its panel starts. A node's parent is the nearest node above it one
    level up.
    """
    parts = []
    bottom = 0.0
    for i, node in enumerate(nodes):
        cx = x + node.depth * TREE_INDENT
        parent = next((p for p in reversed(nodes[:i]) if p.depth == node.depth - 1), None)
        if parent is not None:
            px = x + parent.depth * TREE_INDENT
            parts.append(
                f'<path d="M {px:.1f} {parent.y + 5:.1f} V {node.y:.1f} H {cx - 5:.1f}" '
                f'fill="none" stroke="{RULE}" stroke-width="1.5"/>'
            )
        color = STATUS[node.status].ink if node.status else INK
        parts.append(f'<circle cx="{cx:.1f}" cy="{node.y:.1f}" r="4.5" fill="{color}"/>')
        name_x = cx + 11
        parts.append(text(name_x, node.y + 4.5, node.name, size=13, weight="bold", mono=True, color=color))
        end = name_x + text_width(node.name, 13, mono=True, bold=True)
        if node.count is not None:
            label = f"×{node.count:,}"
            parts.append(text(end + 6, node.y + 4.5, label, size=12, color=MUTED, mono=True))
            end += 6 + text_width(label, 12, mono=True)
        node_bottom = node.y + 9
        if node.subtitle:
            parts.append(text(name_x, node.y + 21, node.subtitle, size=11, color=MUTED))
            node_bottom = node.y + 25
        if leader_to is not None and leader_to > end + 16:
            parts.append(
                f'<line x1="{end + 8:.1f}" y1="{node.y:.1f}" x2="{leader_to - 2:.1f}" y2="{node.y:.1f}" '
                f'stroke="{RULE}" stroke-width="1.2" stroke-dasharray="3 4"/>'
            )
        bottom = max(bottom, node_bottom)
    return canvas.add("\n".join(parts), bottom)


def link(
    canvas: Canvas,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    label: str | None = None,
    color: str = MUTED,
    vertical: bool = False,
    dashed: bool = False,
    stroke: float = 1.6,
) -> float:
    """A curved arrow from ``start`` to ``end``, with an optional label on a white chip at its middle.

    The curve leaves and arrives horizontally, or vertically with
    ``vertical``, so it meets the side of a box square on.
    """
    (x1, y1), (x2, y2) = start, end
    if vertical:
        dy = (y2 - y1) / 2
        path = f"M {x1:.1f} {y1:.1f} C {x1:.1f} {y1 + dy:.1f} {x2:.1f} {y2 - dy:.1f} {x2:.1f} {y2:.1f}"
        sign = 1 if y2 >= y1 else -1
        head = [(x2, y2), (x2 - 4.5, y2 - 8 * sign), (x2 + 4.5, y2 - 8 * sign)]
    else:
        dx = (x2 - x1) / 2
        path = f"M {x1:.1f} {y1:.1f} C {x1 + dx:.1f} {y1:.1f} {x2 - dx:.1f} {y2:.1f} {x2:.1f} {y2:.1f}"
        sign = 1 if x2 >= x1 else -1
        head = [(x2, y2), (x2 - 8 * sign, y2 - 4.5), (x2 - 8 * sign, y2 + 4.5)]
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    parts = [
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{stroke}"{dash}/>',
        '<polygon points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in head) + f'" fill="{color}"/>',
    ]
    bottom = max(y1, y2) + 5
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        w = text_width(label, 11) + 12
        parts.append(
            f'<rect x="{mx - w / 2:.1f}" y="{my - 9:.1f}" width="{w:.1f}" height="18" rx="9" '
            f'fill="{BACKGROUND}" stroke="{RULE}"/>'
        )
        parts.append(text(mx, my + 4, label, size=11, color=MUTED, anchor="middle"))
        bottom = max(bottom, my + 9)
    return canvas.add("\n".join(parts), bottom)


@dataclass(frozen=True)
class CubeGeometry:
    """Where a cube's faces sit. The front face starts at ``(x, y + depth)``; the top and side recede up and right."""

    x: float
    y: float
    width: float
    height: float
    depth: float

    @property
    def face_top(self) -> float:
        return self.y + self.depth

    @property
    def face_bottom(self) -> float:
        return self.y + self.depth + self.height

    @property
    def right(self) -> float:
        return self.x + self.width + self.depth

    def face(self, fx: float, fy: float) -> tuple[float, float]:
        """The point at fractions ``fx`` across and ``fy`` down the front face."""
        return self.x + fx * self.width, self.face_top + fy * self.height

    def middle_left(self) -> tuple[float, float]:
        return self.face(0, 0.5)

    def middle_right(self) -> tuple[float, float]:
        return self.right, self.face_top + self.height / 2 - self.depth / 2


def grid_lines(extent: int, chunk: int) -> list[float]:
    """The chunk boundaries inside an axis of ``extent`` pixels, as fractions of it."""
    return [k * chunk / extent for k in range(1, math.ceil(extent / chunk))]


Window = tuple[float, float, float, float]


def cube(
    canvas: Canvas,
    geometry: CubeGeometry,
    title: str,
    lines: Sequence[str],
    *,
    accent: str = ACCENTS["store"],
    grid: tuple[Sequence[float], Sequence[float]] | None = None,
    window: Window | None = None,
    outlines: Sequence[Window] = (),
    depth_lines: Sequence[float] = (),
    source: str | None = None,
    label_width: float | None = None,
) -> float:
    """An array drawn as a box, with its name and its shape, chunks and codec as lines below it.

    ``grid`` is the chunk boundaries on the front face, as fractions across
    (x) and down (y); :func:`grid_lines` computes them. ``window`` highlights
    one region of the face, as ``(x0, y0, x1, y1)`` fractions, and
    ``outlines`` draws others faintly. ``depth_lines`` divides the top and
    side faces at fractions of the depth axis, for the boundaries of shards
    that run along it. ``source`` names the array the cube comes from, under
    its title.
    """
    g = geometry
    x, y, w, h, d = g.x, g.y, g.width, g.height, g.depth
    parts = [
        f'<polygon points="{x:.1f},{y + d:.1f} {x + d:.1f},{y:.1f} {x + w + d:.1f},{y:.1f} {x + w:.1f},{y + d:.1f}" '
        f'fill="{BACKGROUND}" stroke="{accent}" stroke-width="1.4"/>',
        f'<polygon points="{x + w:.1f},{y + d:.1f} {x + w + d:.1f},{y:.1f} {x + w + d:.1f},{y + h:.1f} '
        f'{x + w:.1f},{y + d + h:.1f}" fill="{RULE}" fill-opacity="0.45" stroke="{accent}" stroke-width="1.4"/>',
        f'<rect x="{x:.1f}" y="{y + d:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{FACE_FILL}" '
        f'stroke="{accent}" stroke-width="1.4"/>',
    ]
    if grid:
        xs, ys = grid
        for fx in xs:
            parts.append(
                f'<line x1="{x + fx * w:.1f}" y1="{y + d:.1f}" x2="{x + fx * w:.1f}" y2="{y + d + h:.1f}" '
                f'stroke="{RULE}" stroke-width="1"/>'
            )
        for fy in ys:
            parts.append(
                f'<line x1="{x:.1f}" y1="{y + d + fy * h:.1f}" x2="{x + w:.1f}" y2="{y + d + fy * h:.1f}" '
                f'stroke="{RULE}" stroke-width="1"/>'
            )
    for t in depth_lines:
        parts.append(
            f'<polyline points="{x + t * d:.1f},{y + d - t * d:.1f} {x + w + t * d:.1f},{y + d - t * d:.1f} '
            f'{x + w + t * d:.1f},{y + d + h - t * d:.1f}" fill="none" stroke="{accent}" stroke-width="1"/>'
        )
    for x0, y0, x1, y1 in outlines:
        parts.append(
            f'<rect x="{x + x0 * w:.1f}" y="{y + d + y0 * h:.1f}" width="{(x1 - x0) * w:.1f}" '
            f'height="{(y1 - y0) * h:.1f}" fill="none" stroke="{MUTED}" stroke-width="1" stroke-dasharray="2 2"/>'
        )
    if window:
        x0, y0, x1, y1 = window
        parts.append(
            f'<rect x="{x + x0 * w:.1f}" y="{y + d + y0 * h:.1f}" width="{(x1 - x0) * w:.1f}" '
            f'height="{(y1 - y0) * h:.1f}" fill="{ACCENTS["highlight"]}" fill-opacity="0.3" '
            f'stroke="{ACCENTS["highlight"]}" stroke-width="2"/>'
        )
    room = label_width if label_width is not None else w + d
    baseline = y + d + h + 22
    parts.append(text(x, baseline, fit(title, room, 13, mono=True), size=13, weight="bold", mono=True))
    if source:
        baseline += 16
        parts.append(text(x, baseline, fit(source, room, 11), size=11, color=MUTED))
    for line in lines:
        baseline += 17
        parts.append(text(x, baseline, fit(line, room, 11.5, mono=True), size=11.5, mono=True))
    return canvas.add("\n".join(parts), baseline + 4)


def legend(canvas: Canvas, x: float, y: float, entries: Sequence[tuple[str, str]]) -> float:
    """A row of keys. Each entry is ``(swatch, label)``.

    A swatch is a :data:`STATUS` name (a band with the label as its tag), a
    color (a bar of that color), or ``"arrow"`` (an arrow, for a link).
    """
    parts = []
    cursor = x
    for swatch, label in entries:
        if swatch in STATUS:
            status = STATUS[swatch]
            w = text_width(label, 10.5, bold=True) + 16
            parts.append(f'<rect x="{cursor:.1f}" y="{y:.1f}" width="{w:.1f}" height="18" rx="3" fill="{status.fill}"/>')
            parts.append(text(cursor + w / 2, y + 13, label, size=10.5, weight="bold", color=status.ink, anchor="middle"))
            cursor += w + 18
            continue
        if swatch == "arrow":
            parts.append(f'<line x1="{cursor:.1f}" y1="{y + 9:.1f}" x2="{cursor + 22:.1f}" y2="{y + 9:.1f}" stroke="{MUTED}" stroke-width="1.6"/>')
            parts.append(f'<polygon points="{cursor + 28:.1f},{y + 9:.1f} {cursor + 20:.1f},{y + 4.5:.1f} {cursor + 20:.1f},{y + 13.5:.1f}" fill="{MUTED}"/>')
            cursor += 34
        else:
            parts.append(f'<rect x="{cursor:.1f}" y="{y + 2:.1f}" width="14" height="14" rx="3" fill="{swatch}"/>')
            cursor += 20
        parts.append(text(cursor, y + 13, label, size=11.5, color=INK))
        cursor += text_width(label, 11.5) + 22
    return canvas.add("\n".join(parts), y + 18)


@dataclass(frozen=True)
class Cell:
    """One table cell: its lines of text, and optionally a status band behind it."""

    lines: Sequence[str] = ()
    status: str | None = None


TABLE_HEADER = 28
CELL_LINE = 17


def table(
    canvas: Canvas,
    x: float,
    y: float,
    widths: Sequence[float],
    header: Sequence[str],
    rows: Sequence[Sequence[Cell]],
    *,
    row_height: float | None = None,
    draw: Callable[[int, int, float, float], str] | None = None,
) -> float:
    """A grid of cells under a bold header row, with rules between rows.

    A cell with a ``status`` gets that status's band; the legend or a tag
    elsewhere names it. ``draw``, if given, is called with each cell's row,
    column and top-left corner and returns extra SVG for that cell, such as
    a small glyph beside its text.
    """
    lines = max((len(cell.lines) for row in rows for cell in row), default=1)
    row_height = row_height or 10 + lines * CELL_LINE
    total = sum(widths)
    parts = [
        f'<line x1="{x:.1f}" y1="{y + TABLE_HEADER:.1f}" x2="{x + total:.1f}" y2="{y + TABLE_HEADER:.1f}" '
        f'stroke="{INK}" stroke-width="1"/>'
    ]
    cx = x
    for title, width in zip(header, widths):
        parts.append(text(cx + 8, y + 19, fit(title, width - 12, 12, mono=True), size=12, weight="bold", mono=True))
        cx += width
    for i, row in enumerate(rows):
        top = y + TABLE_HEADER + i * row_height
        cx = x
        for j, (cell, width) in enumerate(zip(row, widths)):
            if cell.status:
                parts.append(
                    f'<rect x="{cx + 1:.1f}" y="{top + 1:.1f}" width="{width - 2:.1f}" height="{row_height - 2:.1f}" '
                    f'fill="{STATUS[cell.status].fill}"/>'
                )
            for n, line in enumerate(cell.lines):
                parts.append(text(cx + 8, top + 18 + n * CELL_LINE, line, size=11.5, mono=True))
            if draw:
                parts.append(draw(i, j, cx, top))
            cx += width
        parts.append(
            f'<line x1="{x:.1f}" y1="{top + row_height:.1f}" x2="{x + total:.1f}" y2="{top + row_height:.1f}" '
            f'stroke="{RULE}" stroke-width="1"/>'
        )
    return canvas.add("\n".join(parts), y + TABLE_HEADER + len(rows) * row_height)
