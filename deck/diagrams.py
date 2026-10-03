"""Native, editable drawing blocks for the Team Osprey deck (python-pptx; no pictures anywhere).

Every diagram is built from autoshapes, connectors, freeform lines and text frames, so each box, arrow and label
stays editable in PowerPoint. Coordinates are inches from the slide's top-left corner; colours are hex strings.

- Palette: ``C`` (team colours only).
- Primitives: ``write_text``, ``box``, ``label``, ``pill``, ``badge``, ``connect``, ``arrow``, ``polyline``.
- Diagrams: ``functional_flow`` (Outcome 01: components C1-C10 with checkpoints HC1 and HC2) and ``architecture``
  (Outcome 02: layers, stores, external services and the Gemini trust boundary).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

DASH = MSO_LINE_DASH_STYLE.DASH


class C:
    """Team Osprey palette: deep-water ink and teal, with osprey-eye saffron reserved for human checkpoints."""

    INK = "0C2D36"         # titles, dark slides
    TEAL = "0E7C7B"        # primary accent, component outlines
    TEAL_DARK = "0A5E5D"   # accent text on white
    SEAFOAM = "86D4CA"     # accent on dark backgrounds
    TEAL_TINT = "E3F2F0"   # component fill
    SURFACE = "F1F5F7"     # neutral card fill
    LINE = "C8D3D9"        # hairlines and borders
    SLATE = "43535F"       # body text
    MUTED = "66757F"       # captions
    ARROW = "4F5F6B"       # connectors
    SAFFRON = "F2B705"     # human checkpoints (HC1, HC2)
    SAFFRON_TINT = "FFF4CC"
    WHITE = "FFFFFF"
    MONO = "Consolas"      # tag strings and code; every other run uses the theme font

    # tier and risk-class colours: (solid, tint, text on tint)
    CLASS = {
        "Critical": ("9B1C1C", "FBE4E4", "861818"),
        "High": ("B4501A", "FCEBDF", "8A3A0E"),
        "Medium": ("9A7400", "FFF4D1", "6B5000"),
        "Low": ("2F7D53", "E2F3E9", "1E5C3B"),
        "None identified": ("5F6F7A", "EDF1F4", "3F4D57"),
    }
    GATE = {  # verification gate kinds on the AI-layer slide: (fill, text)
        "blocking": ("FBE4E4", "861818"),
        "repair": ("FFF4D1", "6B5000"),
        "local": ("E3F2F0", "0A5E5D"),
    }


def rgb(hex_colour: str) -> RGBColor:
    return RGBColor.from_string(hex_colour)


def class_colours(name: str) -> tuple[str, str, str]:
    """(solid, tint, text-on-tint) for a tier or risk class; neutral for anything else."""
    return C.CLASS.get(name, C.CLASS["None identified"])


# --------------------------------------------------------------------------- text

Run = tuple[str, dict[str, Any]]
Paragraph = str | dict[str, Any]
"""A paragraph is a plain string, or a dict: {"runs": [str | (text, style)], "align", "bullet", "indent",
"space_before", "space_after", "line_spacing", "size", "color", "bold", "italic", "font"}. Run styles take the five
keys size, color, bold, italic and font."""


def run(text: str, **style: Any) -> Run:
    return (text, style)


def para(*runs: str | Run, **opts: Any) -> dict[str, Any]:
    return {"runs": list(runs), **opts}


def write_text(tf: Any, paragraphs: Paragraph | Sequence[Paragraph], *, size: float = 12, color: str = C.SLATE,
               bold: bool = False, italic: bool = False, font: str | None = None, align: Any = PP_ALIGN.LEFT,
               anchor: Any = MSO_ANCHOR.TOP, margins: tuple[float, float, float, float] = (0.06, 0.04, 0.06, 0.04),
               space_after: float = 0, line_spacing: float | None = None, wrap: bool = True) -> None:
    """Replace the text of a text frame. Sizes are points; every run gets an explicit size and colour."""
    if isinstance(paragraphs, (str, dict)):
        paragraphs = [paragraphs]
    tf.clear()
    tf.word_wrap = wrap
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = anchor
    left, top, right, bottom = margins
    tf.margin_left, tf.margin_top = Inches(left), Inches(top)
    tf.margin_right, tf.margin_bottom = Inches(right), Inches(bottom)
    for index, spec in enumerate(paragraphs):
        if isinstance(spec, str):
            spec = {"runs": [spec]}
        p = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        p.alignment = spec.get("align", align)
        if spec.get("space_before"):
            p.space_before = Pt(spec["space_before"])
        after = spec.get("space_after", space_after)
        if after:
            p.space_after = Pt(after)
        spacing = spec.get("line_spacing", line_spacing)
        if spacing:
            p.line_spacing = spacing
        base = {
            "size": spec.get("size", size), "color": spec.get("color", color), "bold": spec.get("bold", bold),
            "italic": spec.get("italic", italic), "font": spec.get("font", font),
        }
        for item in spec.get("runs", []):
            text, style = (item, {}) if isinstance(item, str) else item
            _add_run(p, text, {**base, **style})
        p._p.get_or_add_endParaRPr().set("sz", str(int(round(base["size"] * 100))))
        if spec.get("bullet"):
            char = spec["bullet"] if isinstance(spec["bullet"], str) else "•"
            _bullet(p, char, color=spec.get("bullet_color", C.TEAL), indent=spec.get("indent", 0.16))


def _add_run(p: Any, text: str, style: dict[str, Any]) -> None:
    r = p.add_run()
    r.text = text
    font = r.font
    font.size = Pt(style["size"])
    font.bold = bool(style["bold"])
    font.italic = bool(style["italic"])
    font.color.rgb = rgb(style["color"])
    if style.get("font"):
        font.name = style["font"]


def _insert_before_successor(parent: Any, element: Any, successors: Sequence[str]) -> None:
    successor = next((parent.find(qn(tag)) for tag in successors if parent.find(qn(tag)) is not None), None)
    if successor is not None:
        successor.addprevious(element)
    else:
        parent.append(element)


def _bullet(p: Any, char: str, *, color: str, indent: float) -> None:
    """A real paragraph bullet with a hanging indent, never a bullet character typed into the text."""
    ppr = p._p.get_or_add_pPr()
    ppr.set("marL", str(int(Inches(indent))))
    ppr.set("indent", str(-int(Inches(indent))))
    for tag in ("a:buClr", "a:buSzPct", "a:buFont", "a:buChar", "a:buNone", "a:buAutoNum"):
        for old in ppr.findall(qn(tag)):
            ppr.remove(old)
    clr = ppr.makeelement(qn("a:buClr"), {})
    clr.append(clr.makeelement(qn("a:srgbClr"), {"val": color}))
    for element in (clr, ppr.makeelement(qn("a:buSzPct"), {"val": "100000"}),
                    ppr.makeelement(qn("a:buFont"), {"typeface": "Arial"}),
                    ppr.makeelement(qn("a:buChar"), {"char": char})):
        _insert_before_successor(ppr, element, ("a:tabLst", "a:defRPr", "a:extLst"))


# --------------------------------------------------------------------------- shapes


def _flat(shape: Any) -> Any:
    """No theme shadow: an empty effect list overrides the shape style's effect reference."""
    shape.shadow.inherit = False
    return shape


def box(slide: Any, x: float, y: float, w: float, h: float, text: Paragraph | Sequence[Paragraph] | None = None, *,
        shape: Any = MSO_SHAPE.ROUNDED_RECTANGLE, fill: str | None = C.WHITE, line: str | None = C.LINE,
        line_w: float = 0.75, dash: Any = None, radius: float = 0.08, name: str | None = None,
        **text_kw: Any) -> Any:
    """An autoshape with optional text. ``radius`` is the corner radius in inches (rounded rectangles only)."""
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        shp.name = name
    if fill is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(fill)
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = rgb(line)
        shp.line.width = Pt(line_w)
        if dash is not None:
            shp.line.dash_style = dash
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        shp.adjustments[0] = min(0.5, radius / max(min(w, h), 0.01))
    _flat(shp)
    text_kw.setdefault("anchor", MSO_ANCHOR.MIDDLE)
    write_text(shp.text_frame, text if text is not None else [], **text_kw)
    return shp


def label(slide: Any, x: float, y: float, w: float, h: float, text: Paragraph | Sequence[Paragraph], *,
          name: str | None = None, **text_kw: Any) -> Any:
    """A text box (no fill, no line, no inset unless asked)."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    text_kw.setdefault("margins", (0, 0, 0, 0))
    write_text(tb.text_frame, text, **text_kw)
    return tb


def pill(slide: Any, x: float, y: float, w: float, h: float, text: str, *, fill: str = C.TEAL_TINT,
         color: str = C.TEAL_DARK, line: str | None = None, size: float = 10, bold: bool = True,
         name: str | None = None) -> Any:
    return box(slide, x, y, w, h, para(text), fill=fill, line=line, radius=h / 2, size=size, bold=bold, color=color,
               align=PP_ALIGN.CENTER, margins=(0.04, 0.0, 0.04, 0.0), name=name)


def badge(slide: Any, cx: float, cy: float, d: float, text: str, *, fill: str = C.TEAL, color: str = C.WHITE,
          size: float = 11, line: str | None = None, name: str | None = None) -> Any:
    """A numbered circle centred on (cx, cy)."""
    return box(slide, cx - d / 2, cy - d / 2, d, d, para(text), shape=MSO_SHAPE.OVAL, fill=fill, line=line,
               size=size, bold=True, color=color, align=PP_ALIGN.CENTER, margins=(0, 0, 0, 0), name=name)


# --------------------------------------------------------------------------- lines and arrows

TOP, LEFT, BOTTOM, RIGHT = 0, 1, 2, 3  # connection sites of rectangles, ovals, diamonds and most flowchart shapes


def _style_line(shape: Any, *, color: str, width: float, dash: Any, head: str | None, tail: str | None) -> None:
    shape.line.color.rgb = rgb(color)
    shape.line.width = Pt(width)
    if dash is not None:
        shape.line.dash_style = dash
    ln = shape.line._get_or_add_ln()
    for tag in ("a:headEnd", "a:tailEnd"):
        for old in ln.findall(qn(tag)):
            ln.remove(old)
    for tag, kind in (("a:headEnd", head), ("a:tailEnd", tail)):
        if kind:
            _insert_before_successor(ln, ln.makeelement(qn(tag), {"type": kind, "w": "med", "len": "med"}),
                                     ("a:extLst",))
    _flat(shape)


def connect(slide: Any, a: Any, b: Any, a_site: int = RIGHT, b_site: int = LEFT, *, color: str = C.ARROW,
            width: float = 1.25, dash: Any = None, head: str | None = None, tail: str | None = "triangle",
            name: str | None = None) -> Any:
    """A straight connector glued to two shapes, so it follows them when they are moved in PowerPoint."""
    cxn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, 0, 0, 0, 0)
    cxn.begin_connect(a, a_site)
    cxn.end_connect(b, b_site)
    if name:
        cxn.name = name
    _style_line(cxn, color=color, width=width, dash=dash, head=head, tail=tail)
    return cxn


def arrow(slide: Any, x1: float, y1: float, x2: float, y2: float, *, color: str = C.ARROW, width: float = 1.25,
          dash: Any = None, head: str | None = None, tail: str | None = "triangle", name: str | None = None) -> Any:
    """A straight connector between two points (inches)."""
    cxn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    if name:
        cxn.name = name
    _style_line(cxn, color=color, width=width, dash=dash, head=head, tail=tail)
    return cxn


def polyline(slide: Any, points: Sequence[tuple[float, float]], *, color: str = C.ARROW, width: float = 1.25,
             dash: Any = None, head: str | None = None, tail: str | None = "triangle",
             name: str | None = None) -> Any:
    """An open freeform path through the points (inches), with optional arrowheads. Needs a bend in both axes."""
    emu = [(int(Inches(x)), int(Inches(y))) for x, y in points]
    builder = slide.shapes.build_freeform(emu[0][0], emu[0][1], scale=1.0)
    builder.add_line_segments(emu[1:], close=False)
    shp = builder.convert_to_shape()
    if name:
        shp.name = name
    shp.fill.background()
    _style_line(shp, color=color, width=width, dash=dash, head=head, tail=tail)
    return shp


# --------------------------------------------------------------------------- Outcome 01: functional design

FLOW_COMPONENTS: dict[str, tuple[str, str]] = {
    "C1": ("Intake & validate", "header aliases, cream-cell map, skip V-000"),
    "C2": ("Criticality", "rubric on columns B–K, floors, sensitivity"),
    "C3": ("Depth planner", "families, caps, budgets, stop rules"),
    "C4": ("Discovery", "seeds, DNS, sitemaps, SEC, ATS, Wayback"),
    "C5": ("Compliant capture", "ToS → robots → rate limit → GET"),
    "C6": ("Extraction", "dated text, sentences, lexicon passages"),
    "C7": ("Signal analysis", "rules ∥ Gemini claims → V1–V9 → tags"),
    "C8": ("AI-usage verdict", "rules a–f, ICD 203 wording"),
    "C9": ("Risk engine", "E·K·TP·TG → ARP, gate, cap; actions"),
    "C10": ("Compose & export", "L–V cells, Evidence & Coverage Log"),
}
CHECKPOINTS: dict[str, str] = {
    "HC1": "analyst confirms the tier; an override needs a reason",
    "HC2": "analyst accepts or rejects each cited excerpt, with a reason",
}
"""The two human checkpoints. There is no release sign-off: the export is gated by code (``RELEASE_GATE``)."""
RELEASE_GATE = "LLM audit gate"
RELEASE_ORDER: tuple[str, ...] = ("Freeze evidence pack", "Render L–V cells", RELEASE_GATE, "Export workbook",
                                  "Verify by replay")
"""C10's release order. The gate is code: export_assessment refuses a run whose LLM audit log has problems, and the
export's integrity check refuses any change outside the student cells."""


def _component(slide: Any, x: float, y: float, w: float, h: float, code: str) -> Any:
    name, detail = FLOW_COMPONENTS[code]
    return box(slide, x, y, w, h, [
        para(run(code, size=10, bold=True, color=C.TEAL_DARK)),
        para(run(name, size=12, bold=True, color=C.INK)),
        para(run(detail, size=10, color=C.SLATE), space_before=2),
    ], fill=C.TEAL_TINT, line=C.TEAL, line_w=1.0, margins=(0.08, 0.05, 0.06, 0.05), name=f"{code} {name}")


def _checkpoint(slide: Any, x: float, y: float, d: float, code: str) -> Any:
    return box(slide, x, y, d, d, para(run(code, size=11, bold=True, color=C.INK)), shape=MSO_SHAPE.DIAMOND,
               fill=C.SAFFRON, line=None, align=PP_ALIGN.CENTER, margins=(0, 0, 0, 0), name=f"{code} checkpoint")


def _document(slide: Any, x: float, y: float, w: float, h: float, title: str, detail: str, name: str) -> Any:
    return box(slide, x, y, w, h, [para(run(title, size=11, bold=True, color=C.INK)),
                                   para(run(detail, size=10, color=C.SLATE))],
               shape=MSO_SHAPE.FLOWCHART_DOCUMENT, fill=C.SURFACE, line=C.ARROW, line_w=1.0,
               margins=(0.08, 0.04, 0.06, 0.14), name=name)


def functional_flow(slide: Any, *, top: float = 1.78, left: float = 0.55, width: float = 12.23) -> dict[str, Any]:
    """Outcome 01: C1-C10 as a two-row flow with HC1 and HC2, the evidence store, Gemini behind the payload guard,
    the stop-rule loop and the release order. Returns the main shapes by key (component codes, HC1, HC2, upload,
    output, store, gemini, gate)."""
    shapes: dict[str, Any] = {}
    doc_w, comp_w, diamond, row_h = 1.25, 1.6, 0.85, 1.05
    gap = (width - doc_w - 5 * comp_w - diamond) / 6
    row_a, row_b = top, top + 2.47
    band_top = row_a + row_h

    def xs(kinds: Sequence[str]) -> list[float]:
        out, x = [], left
        for kind in kinds:
            out.append(x)
            x += {"doc": doc_w, "comp": comp_w, "hc": diamond}[kind] + gap
        return out

    # row A, left to right: upload -> C1 -> C2 -> HC1 -> C3 -> C4 -> C5
    ax = xs(["doc", "comp", "comp", "hc", "comp", "comp", "comp"])
    shapes["upload"] = _document(slide, ax[0], row_a, doc_w, row_h, "Vendor workbook", ".xlsx upload, profiles B–K",
                                 "Input workbook")
    for code, x in zip(("C1", "C2"), ax[1:3]):
        shapes[code] = _component(slide, x, row_a, comp_w, row_h, code)
    shapes["HC1"] = _checkpoint(slide, ax[3], row_a + (row_h - diamond) / 2, diamond, "HC1")
    for code, x in zip(("C3", "C4", "C5"), ax[4:7]):
        shapes[code] = _component(slide, x, row_a, comp_w, row_h, code)
    # row B, drawn left to right but read right to left: C6 -> C7 -> HC2 -> C8 -> C9 -> C10 -> output
    bx = xs(["doc", "comp", "comp", "comp", "hc", "comp", "comp"])
    shapes["output"] = _document(slide, bx[0], row_b, doc_w, row_h, "Completed workbook",
                                 "L–V + Evidence & Coverage Log", "Output workbook")
    for code, x in zip(("C10", "C9", "C8"), bx[1:4]):
        shapes[code] = _component(slide, x, row_b, comp_w, row_h, code)
    shapes["HC2"] = _checkpoint(slide, bx[4], row_b + (row_h - diamond) / 2, diamond, "HC2")
    for code, x in zip(("C7", "C6"), bx[5:7]):
        shapes[code] = _component(slide, x, row_b, comp_w, row_h, code)

    for a, b in (("upload", "C1"), ("C1", "C2"), ("C2", "HC1"), ("HC1", "C3"), ("C3", "C4"), ("C4", "C5")):
        connect(slide, shapes[a], shapes[b], RIGHT, LEFT, name=f"flow {a} to {b}")
    for a, b in (("C6", "C7"), ("C7", "HC2"), ("HC2", "C8"), ("C8", "C9"), ("C9", "C10"), ("C10", "output")):
        connect(slide, shapes[a], shapes[b], LEFT, RIGHT, name=f"flow {a} to {b}")

    # middle band: evidence store between C5 and C6
    store_w = 1.45
    shapes["store"] = box(slide, ax[6] + (comp_w - store_w) / 2, band_top + 0.27, store_w, 0.95, [
        para(run("Evidence store", size=11, bold=True, color=C.INK)),
        para(run("raw bytes + SHA-256", size=10, color=C.SLATE)),
        para(run("UTC time, headers", size=10, color=C.SLATE)),
    ], shape=MSO_SHAPE.CAN, fill=C.SURFACE, line=C.ARROW, line_w=1.0, align=PP_ALIGN.CENTER,
        margins=(0.04, 0.2, 0.04, 0.03), name="Evidence store")
    connect(slide, shapes["C5"], shapes["store"], BOTTOM, TOP, name="capture to store")
    connect(slide, shapes["store"], shapes["C6"], BOTTOM, TOP, name="store to extraction")

    # Gemini between C4 and C7 (proposes only; dashed = outside the trust boundary)
    gem_w = 1.85
    shapes["gemini"] = box(slide, ax[5], band_top + 0.3, gem_w, 0.88, [
        para(run("Gemini", size=11, bold=True, color=C.INK)),
        para(run("expand · triage · extract", size=10, color=C.SLATE)),
        para(run("public text, payload guard", size=10, color=C.SLATE)),
    ], fill=C.WHITE, line=C.TEAL, line_w=1.25, dash=DASH, align=PP_ALIGN.CENTER,
        margins=(0.04, 0.03, 0.04, 0.03), name="Gemini (AI proposes)")
    c4_mid = ax[5] + comp_w / 2
    arrow(slide, c4_mid, band_top, c4_mid, band_top + 0.3, color=C.TEAL, dash=DASH, head="triangle",
          name="C4 and Gemini")
    arrow(slide, c4_mid, band_top + 1.18, c4_mid, row_b, color=C.TEAL, dash=DASH, head="triangle",
          name="Gemini and C7")

    # stop-rule loop: from C7 back to C4, through the gap to the left of both
    loop_x = ax[5] - gap / 2
    polyline(slide, [(ax[5], row_b + 0.22), (loop_x, row_b + 0.22), (loop_x, band_top - 0.22),
                     (ax[5], band_top - 0.22)], color=C.TEAL_DARK, width=1.5, name="stop-rule loop")
    label(slide, loop_x - 1.6, band_top + 0.5, 1.5, 0.66, [
        para(run("Stop-rule loop", size=10, bold=True, color=C.TEAL_DARK)),
        para(run("until U/S/D answered, saturated or out of budget", size=10, color=C.SLATE)),
    ], align=PP_ALIGN.RIGHT, name="stop-rule loop label")

    # checkpoint captions
    label(slide, ax[3] - 0.5, band_top + 0.06, diamond + 1.0, 0.5,
          para(run("HC1 ", size=10, bold=True, color=C.INK), run(CHECKPOINTS["HC1"], size=10)),
          align=PP_ALIGN.CENTER, name="HC1 caption")
    label(slide, bx[4] - 0.62, row_b + row_h + 0.07, diamond + 1.24, 0.5,
          para(run("HC2 ", size=10, bold=True, color=C.INK), run(CHECKPOINTS["HC2"], size=10)),
          align=PP_ALIGN.CENTER, name="HC2 caption")

    # C10 release order: freeze -> render -> LLM audit gate (code) -> export with integrity check -> verify
    strip_y = row_b + row_h + 0.66
    step_w, step_gap = 1.42, 0.22
    label(slide, left, strip_y - 0.3, len(RELEASE_ORDER) * (step_w + step_gap) - step_gap, 0.26, para(
        run("C10 release order  ", size=10, bold=True, color=C.TEAL_DARK),
        run("(evidence is frozen before cells are rendered; a failed LLM audit stops the export)", size=10)),
        name="release order label")
    previous = None
    for index, step in enumerate(RELEASE_ORDER):
        gate = step == RELEASE_GATE
        shp = box(slide, left + index * (step_w + step_gap), strip_y, step_w, 0.44,
                  para(run(step, size=10, bold=gate, color=C.TEAL_DARK if gate else C.INK)),
                  fill=C.TEAL_TINT if gate else C.SURFACE, line=C.TEAL if gate else C.LINE, radius=0.22,
                  align=PP_ALIGN.CENTER, margins=(0.04, 0, 0.04, 0), name=f"release {index + 1}: {step}")
        if gate:
            shapes["gate"] = shp
        if previous is not None:
            connect(slide, previous, shp, RIGHT, LEFT, width=1.0, name=f"release step {index}")
        previous = shp

    # legend
    legend = (
        (MSO_SHAPE.ROUNDED_RECTANGLE, C.TEAL_TINT, C.TEAL, None, "component C1–C10"),
        (MSO_SHAPE.DIAMOND, C.SAFFRON, None, None, "human checkpoint"),
        (MSO_SHAPE.ROUNDED_RECTANGLE, C.WHITE, C.TEAL, DASH, "AI service (proposes only)"),
        (MSO_SHAPE.CAN, C.SURFACE, C.ARROW, None, "content-addressed store"),
    )
    lx, ly = left + width - 2.75, row_b + row_h + 0.3
    for index, (kind, fill, line, dash, text) in enumerate(legend):
        y0 = ly + index * 0.31
        box(slide, lx, y0 + 0.03, 0.32, 0.24, None, shape=kind, fill=fill, line=line, dash=dash, line_w=1.0,
            radius=0.05, name=f"legend symbol {index + 1}")
        label(slide, lx + 0.42, y0, 2.3, 0.3, para(run(text, size=10, color=C.SLATE)), anchor=MSO_ANCHOR.MIDDLE,
              name=f"legend text {index + 1}")
    return shapes


# --------------------------------------------------------------------------- Outcome 02: architecture

EXTERNAL_SERVICES: tuple[tuple[str, str], ...] = (
    ("Vendor & provider websites", "sitemaps · WP-REST · PDFs"),
    ("SEC EDGAR", "submissions · full-text search"),
    ("DNS over HTTPS", "dns.google and Cloudflare agree"),
    ("ATS APIs", "Workday · Oracle ORC · Workable"),
    ("Wayback Machine", "availability · CDX · read-only"),
)
ENGINE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Net policy", ("Live | Replay fetcher", "ToS register (tou.toml)", "robots.txt via Protego",
                    "per-host rate limit", "honest UA, no evasion")),
    ("Collect", ("dns · site · wordpress", "sec · jobs (three ATS)", "wayback · providers · seeds",
                 "manual capture import", "Coverage Log status")),
    ("Analyse", ("extract: trafilatura, pypdf", "rules: lexicon, guards, tags", "ai: Gemini | Cached | Null",
                 "verify: gates V1–V9", "cluster: corroboration")),
    ("Decide", ("verdict: rules a–f", "risk: ARP, gate, cap", "actions: playbook, Q1–Q15",
                "compose: cells L–V", "review: HC records applied")),
)
STORES: tuple[tuple[str, str], ...] = (
    ("config/ prompts/ seeds/", "versioned, hashed TOML"),
    ("evidence/", "SHA-256 blobs · text · shots"),
    ("review/", "HC decisions with reasons"),
    ("runs/", "manifests · logs · hashes"),
)
OUTPUTS: tuple[tuple[str, str], ...] = (
    ("Completed workbook", "cream cells L–V + six appended sheets"),
    ("Run record", "manifest, config and prompt hashes"),
    ("Deck", "python-pptx, numbers from the frozen run"),
)


def architecture(slide: Any, *, top: float = 1.78) -> dict[str, Any]:
    """Outcome 02: external services on the left; inside the dashed trust boundary, interfaces -> pipeline ->
    engine (net policy, collect, analyse, decide) -> stores -> outputs; Gemini reached only through the guard.
    Returns the main shapes by key."""
    shapes: dict[str, Any] = {}
    ext_x, ext_w = 0.55, 2.5
    bound_x, bound_w = 3.3, 9.48
    inner_x, inner_w = bound_x + 0.15, bound_w - 0.3
    bound_bottom = top + 4.5
    pipe_y, engine_y, engine_h = top + 0.75, top + 1.62, 1.45
    path_y = (pipe_y + 0.5 + engine_y) / 2  # the gap between the pipeline and the engine row

    # external services
    label(slide, ext_x, top - 0.02, ext_w, 0.3, para(run("External services · GET only", size=11, bold=True,
                                                         color=C.INK)), name="external header")
    label(slide, ext_x, top + 0.3, ext_w, 0.62, para(
        run("Manual lane: ", size=10, bold=True, color=C.INK),
        run("fiserv.com and linkedin.com: analyst capture only (not performed this run); "
            "never crawled or sent to Gemini", size=10)),
        name="manual capture lane")
    gem_y = path_y - 0.31
    shapes["gemini"] = box(slide, ext_x, gem_y, ext_w, 0.62, [
        para(run("Gemini API", size=10, bold=True, color=C.INK)),
        para(run("structured JSON · seed 1234 · no tools", size=10)),
    ], fill=C.SAFFRON_TINT, line=C.SAFFRON, line_w=1.5, margins=(0.08, 0.02, 0.05, 0.02),
        name="external: Gemini API")
    group_y = top + 1.8
    group = box(slide, ext_x, group_y, ext_w, bound_bottom - group_y, None, fill=C.SURFACE, line=None, radius=0.1,
                name="public web services")
    shapes["web"] = group
    y = group_y + 0.1
    for name, detail in EXTERNAL_SERVICES:
        shapes[name] = box(slide, ext_x + 0.08, y, ext_w - 0.16, 0.46, [
            para(run(name, size=10, bold=True, color=C.INK)), para(run(detail, size=10)),
        ], fill=C.WHITE, line=C.LINE, margins=(0.07, 0.02, 0.05, 0.02), name=f"external: {name}")
        y += 0.53

    # trust boundary
    shapes["boundary"] = box(slide, bound_x, top - 0.05, bound_w, bound_bottom - top + 0.05, None, fill=None,
                             line=C.TEAL, line_w=1.5, dash=DASH, radius=0.14, name="Trust boundary")
    label(slide, bound_x, bound_bottom + 0.07, bound_w, 0.45, para(
        run("Trust boundary: ", size=10, bold=True, color=C.TEAL_DARK),
        run("profile fields, tiers, verdicts and findings stay inside. Only public passages that pass the payload "
            "guard cross to Gemini, and the audit log records each call without its text.", size=10)),
        name="trust boundary caption")

    # interfaces, each calling the one pipeline entry point
    third = (inner_w - 2 * 0.2) / 3
    for index, (name, detail) in enumerate((("CLI", "typer: assess · export · verify"),
                                            ("Streamlit app", "Assess · Evidence · Risk · Export"),
                                            ("Notebook", "Colab or local Jupyter"))):
        x = inner_x + index * (third + 0.2)
        shapes[name] = box(slide, x, top + 0.05, third, 0.48, [
            para(run(name, size=11, bold=True, color=C.INK), run("  " + detail, size=10)),
        ], fill=C.WHITE, line=C.TEAL, line_w=1.0, margins=(0.08, 0.02, 0.06, 0.02), name=f"interface: {name}")
        arrow(slide, x + third / 2, top + 0.53, x + third / 2, pipe_y, width=1.0, name=f"{name} calls the pipeline")
    shapes["pipeline"] = box(slide, inner_x, pipe_y, inner_w, 0.5, [
        para(run("run_assessment(xlsx, mode)", size=11, bold=True, color=C.INK, font=C.MONO),
             run("  one entry point → AssessmentResult", size=10)),
    ], fill=C.TEAL_TINT, line=C.TEAL, line_w=1.0, margins=(0.1, 0.02, 0.06, 0.02), name="pipeline")
    shapes["mode"] = box(slide, inner_x + inner_w - 3.02, pipe_y + 0.08, 2.92, 0.34, [
        para(run("replay", size=10, bold=True, color=C.TEAL_DARK), run(" (default, offline)", size=10),
             run(" | live_rules | live_ai", size=10, color=C.MUTED)),
    ], fill=C.WHITE, line=C.TEAL, line_w=0.75, radius=0.17, align=PP_ALIGN.CENTER,
        margins=(0.06, 0, 0.06, 0), name="mode switch")

    # engine columns
    col_gap = 0.3
    col_w = (inner_w - 3 * col_gap) / 4
    col_x = [inner_x + index * (col_w + col_gap) for index in range(4)]
    for x, (name, lines) in zip(col_x, ENGINE):
        shapes[name] = box(slide, x, engine_y, col_w, engine_h, [
            para(run(name, size=11, bold=True, color=C.TEAL_DARK), space_after=2),
            *[para(run(text, size=10), bullet=True, indent=0.12) for text in lines],
        ], fill=C.WHITE, line=C.TEAL, line_w=1.0, anchor=MSO_ANCHOR.TOP, margins=(0.09, 0.06, 0.06, 0.04),
            name=f"engine: {name}")
    for (a, _), (b, _) in zip(ENGINE, ENGINE[1:]):
        connect(slide, shapes[a], shapes[b], RIGHT, LEFT, name=f"engine {a} to {b}")
    arrow(slide, ext_x + ext_w, engine_y + 0.75, col_x[0], engine_y + 0.75, color=C.TEAL_DARK, width=1.5,
          head="triangle", name="GET requests across the boundary")

    # Gemini path: Analyse -> payload guard -> Gemini, through the gap above the engine row
    analyse_mid = col_x[2] + col_w / 2
    polyline(slide, [(analyse_mid, engine_y), (analyse_mid, path_y), (ext_x + ext_w, path_y)],
             color=C.SAFFRON, width=1.75, head="triangle", name="guarded Gemini call")
    shapes["guard"] = pill(slide, inner_x + 0.25, path_y - 0.15, 2.3, 0.3, "Payload guard · public text only",
                           fill=C.SAFFRON, color=C.INK, size=10, name="Payload guard")

    # stores, aligned with the engine columns
    store_y = engine_y + engine_h + 0.16
    for x, (name, detail) in zip(col_x, STORES):
        shapes[name] = box(slide, x, store_y, col_w, 0.62, [
            para(run(name, size=10, bold=True, color=C.INK, font=C.MONO)),
            para(run(detail, size=10)),
        ], shape=MSO_SHAPE.CAN, fill=C.SURFACE, line=C.ARROW, line_w=0.75, margins=(0.05, 0.13, 0.05, 0.0),
            align=PP_ALIGN.CENTER, name=f"store: {name}")
    connect(slide, shapes["Collect"], shapes["evidence/"], BOTTOM, TOP, width=1.0, name="collect writes evidence")
    arrow(slide, col_x[1] + col_w, store_y + 0.31, col_x[2] + 0.3, engine_y + engine_h, width=1.0,
          name="analyse reads evidence")
    connect(slide, shapes["review/"], shapes["Analyse"], TOP, BOTTOM, width=1.0, name="reviews applied")
    connect(slide, shapes["Decide"], shapes["runs/"], BOTTOM, TOP, width=1.0, name="decide writes run record")

    # outputs
    out_y = store_y + 0.72
    out_w = (inner_w - 2 * 0.2) / 3
    for index, (name, detail) in enumerate(OUTPUTS):
        shapes[name] = box(slide, inner_x + index * (out_w + 0.2), out_y, out_w, 0.5, [
            para(run(name, size=10, bold=True, color=C.INK), run("  " + detail, size=10)),
        ], shape=MSO_SHAPE.FLOWCHART_DOCUMENT, fill=C.WHITE, line=C.ARROW, line_w=0.75,
            margins=(0.08, 0.02, 0.05, 0.08), name=f"output: {name}")
    return shapes
