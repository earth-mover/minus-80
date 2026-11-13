import xml.etree.ElementTree as ET

from minus80 import diagrams
from minus80.diagrams import Canvas, CubeGeometry, Row, TreeNode


def parse(canvas: Canvas) -> ET.Element:
    return ET.fromstring(canvas.svg())


def texts(canvas: Canvas) -> list[str]:
    return [element.text for element in parse(canvas).iter("{http://www.w3.org/2000/svg}text")]


def test_text_escapes_every_string():
    element = diagrams.text(0, 0, '<b> & "x"')
    assert "<b>" not in element
    assert ET.fromstring(element).text == '<b> & "x"'


def test_panel_escapes_titles_keys_and_values():
    canvas = Canvas(400)
    diagrams.panel(canvas, 10, 10, 380, "a<b", [Row("k&", "<v>")], source="x>y")
    assert {"a<b", "k&", "<v>", "x>y"} <= set(texts(canvas))


def test_canvas_height_covers_the_lowest_block():
    canvas = Canvas(400)
    y = diagrams.heading(canvas, "Title", "subtitle")
    for i in range(12):
        y = diagrams.panel(canvas, 10, y + 10, 380, f"panel {i}", [Row("key", "value")] * 3)
    root = parse(canvas)
    assert float(root.get("height")) >= y
    assert canvas.height >= y + canvas.margin


def test_panel_returns_the_height_it_promised():
    canvas = Canvas(400)
    bottom = diagrams.panel(canvas, 0, 100, 300, "t", [Row("a", "1"), Row("b", "2")], source="from")
    assert bottom == 100 + diagrams.panel_height(2, source=True)


def test_svg_has_a_white_background():
    first = parse(Canvas(100))[0]
    assert first.tag.endswith("rect") and first.get("fill") == "#ffffff"


def test_status_row_has_its_band_and_tag():
    canvas = Canvas(400)
    diagrams.panel(canvas, 0, 0, 380, "t", [Row("round 3", "DAPI_3", status="changed", tag="unlisted")])
    svg = canvas.svg()
    assert diagrams.STATUS["changed"].fill in svg
    assert "unlisted" in texts(canvas)


def test_long_values_are_cut_to_the_panel():
    canvas = Canvas(400)
    diagrams.panel(canvas, 0, 0, 200, "t", [Row("k", "x" * 200)])
    value = [t for t in texts(canvas) if t and t.startswith("x")][0]
    assert value.endswith("…") and len(value) < 200


def test_cube_emits_its_labels_and_grid():
    canvas = Canvas(600)
    geometry = CubeGeometry(20, 20, 200, 160, 30)
    bottom = diagrams.cube(
        canvas,
        geometry,
        "C/02/1/0",
        ["shape 4 × 271 × 12425 × 13187", "chunks 1 × 10 × 1000 × 1000", "blosc lz4"],
        grid=(diagrams.grid_lines(13187, 1000), diagrams.grid_lines(12425, 1000)),
        window=(0.1, 0.1, 0.2, 0.2),
        source="AssayPlate.zarr.zip",
    )
    labels = texts(canvas)
    assert {"C/02/1/0", "shape 4 × 271 × 12425 × 13187", "chunks 1 × 10 × 1000 × 1000", "blosc lz4"} <= set(labels)
    assert "AssayPlate.zarr.zip" in labels
    lines = list(parse(canvas).iter("{http://www.w3.org/2000/svg}line"))
    assert len(lines) == 13 + 12
    assert bottom > geometry.face_bottom


def test_grid_lines_fall_on_chunk_boundaries():
    assert diagrams.grid_lines(2500, 1000) == [0.4, 0.8]
    assert diagrams.grid_lines(1000, 1000) == []


def test_tree_draws_an_elbow_from_each_child_to_its_parent():
    canvas = Canvas(400)
    diagrams.tree(
        canvas,
        10,
        [TreeNode("/", 20), TreeNode("B", 60, 1, count=6), TreeNode("B/02", 100, 2), TreeNode("C", 140, 1)],
        leader_to=300,
    )
    root = parse(canvas)
    ns = "{http://www.w3.org/2000/svg}"
    assert len(list(root.iter(f"{ns}path"))) == 3
    assert len(list(root.iter(f"{ns}circle"))) == 4
    assert "×6" in texts(canvas)


def test_link_label_sits_on_the_curve():
    canvas = Canvas(400)
    diagrams.link(canvas, (0, 0), (200, 100), label="copied")
    assert "copied" in texts(canvas)
    assert canvas.bottom >= 100


def test_formatting():
    assert diagrams.dims((4, 271, 1000)) == "4 × 271 × 1000"
    assert diagrams.span(500, 501) == "500–501"
    assert diagrams.span(5, 5) == "5"
    assert diagrams.size_text(591_288_782_548) == "591.3 GB"
    assert diagrams.count_text(2036714) == "2,036,714"


def test_legend_marks_status_with_a_word():
    canvas = Canvas(600)
    diagrams.legend(canvas, 0, 0, [("changed", "unlisted"), (diagrams.ACCENTS["source"], "source"), ("arrow", "copied")])
    assert {"unlisted", "source", "copied"} <= set(texts(canvas))


def test_table_bands_status_cells_and_calls_draw():
    canvas = Canvas(600)
    seen = []

    def draw(i, j, x, y):
        seen.append((i, j))
        return ""

    bottom = diagrams.table(
        canvas, 0, 0, [100, 200], ["field", "level 1"],
        [[diagrams.Cell(["FOV_1"]), diagrams.Cell(["y 0–500", "x 0–501"], status="changed")]], draw=draw,
    )
    assert {"field", "level 1", "FOV_1", "y 0–500", "x 0–501"} <= set(texts(canvas))
    assert diagrams.STATUS["changed"].fill in canvas.svg()
    assert seen == [(0, 0), (0, 1)]
    assert bottom == diagrams.TABLE_HEADER + 10 + 2 * diagrams.CELL_LINE


def test_cube_divides_its_depth_into_shards():
    canvas = Canvas(400)
    diagrams.cube(canvas, CubeGeometry(0, 0, 100, 100, 30), "a", [], depth_lines=[0.25, 0.5])
    assert len(list(parse(canvas).iter("{http://www.w3.org/2000/svg}polyline"))) == 2
