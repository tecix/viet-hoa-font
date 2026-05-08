#!/usr/bin/env python3
"""
viet_hoa.py — Việt hoá an OTF/TTF font.

Adds Vietnamese character support to a font that already contains the
basic Latin alphabet plus standard combining diacritics. It does this by:

  1. Detecting which Vietnamese precomposed codepoints are missing.
  2. For each missing codepoint, decomposing it into a base + combining
     marks via Unicode NFD.
  3. Synthesising a precomposed glyph by stacking the base glyph and the
     combining mark glyphs the font already has, positioning the marks
     using the font's anchor / mark-positioning data when available, or
     falling back to heuristic placement based on glyph bounding boxes.
  4. Adding the new glyph to the font's cmap so it is reachable by its
     codepoint.

This is a pragmatic "best effort" tool: results are good when the source
font already contains decent combining accent glyphs (U+0300..U+031B).
For fonts that lack those, supply a donor font with --donor.

Usage:
    python viet_hoa.py input.otf -o output.otf
    python viet_hoa.py input.otf -o output.otf --donor NotoSans.ttf
    python viet_hoa.py input.otf --check        # just report coverage
"""

from __future__ import annotations

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import argparse
import base64
import datetime
import io
import json as _json
import os
import sys
import unicodedata
from pathlib import Path

from fontTools.ttLib import TTFont, newTable
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.misc.transform import Offset

# ---------------------------------------------------------------------------
# Vietnamese codepoint set
# ---------------------------------------------------------------------------
# Vietnamese uses the basic Latin alphabet plus seven additional letters
# (Ă, Â, Đ, Ê, Ô, Ơ, Ư) and five tone marks (grave, acute, hook above,
# tilde, dot below) which combine with the six vowels A, Ă, Â, E, Ê, I,
# O, Ô, Ơ, U, Ư, Y. The full set lives mostly in the Latin Extended
# Additional block.

VIETNAMESE_CODEPOINTS = [
    # Base letters with single diacritics
    0x00C0, 0x00C1, 0x00C2, 0x00C3,                      # À Á Â Ã
    0x00C8, 0x00C9, 0x00CA,                              # È É Ê
    0x00CC, 0x00CD,                                      # Ì Í
    0x00D2, 0x00D3, 0x00D4, 0x00D5,                      # Ò Ó Ô Õ
    0x00D9, 0x00DA, 0x00DD,                              # Ù Ú Ý
    0x00E0, 0x00E1, 0x00E2, 0x00E3,                      # à á â ã
    0x00E8, 0x00E9, 0x00EA,                              # è é ê
    0x00EC, 0x00ED,                                      # ì í
    0x00F2, 0x00F3, 0x00F4, 0x00F5,                      # ò ó ô õ
    0x00F9, 0x00FA, 0x00FD,                              # ù ú ý
    # Đ / đ
    0x0102, 0x0103,                                      # Ă ă
    0x0110, 0x0111,                                      # Đ đ
    0x0128, 0x0129,                                      # Ĩ ĩ
    0x0168, 0x0169,                                      # Ũ ũ
    0x01A0, 0x01A1,                                      # Ơ ơ
    0x01AF, 0x01B0,                                      # Ư ư
    # Latin Extended Additional — the bulk of Vietnamese precomposed
    # Range U+1EA0..U+1EF9 covers all remaining Vietnamese letters.
    *range(0x1EA0, 0x1EFA),
]

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def cmap_of(font: TTFont) -> dict[int, str]:
    """Return the best Unicode cmap as a {codepoint: glyph_name} dict."""
    return font.getBestCmap()


def reverse_cmap(font: TTFont) -> dict[str, int]:
    return {g: cp for cp, g in cmap_of(font).items()}


def font_is_cff(font: TTFont) -> bool:
    return "CFF " in font or "CFF2" in font


def missing_codepoints(font: TTFont, wanted: list[int]) -> list[int]:
    cm = cmap_of(font)
    return [cp for cp in wanted if cp not in cm]


# ---------------------------------------------------------------------------
# Anchor lookup (GPOS MarkBasePos)
# ---------------------------------------------------------------------------

def collect_anchors(font: TTFont) -> tuple[dict, dict]:
    """
    Walk the GPOS table and return:
      base_anchors: {glyph_name: {anchor_class: (x, y)}}
      mark_anchors: {glyph_name: (anchor_class, (x, y))}
    Returns ({}, {}) if GPOS is missing or has no MarkBase data.
    """
    base_anchors: dict[str, dict[str, tuple[float, float]]] = {}
    mark_anchors: dict[str, tuple[str, tuple[float, float]]] = {}
    if "GPOS" not in font:
        return base_anchors, mark_anchors

    gpos = font["GPOS"].table
    if not gpos.LookupList:
        return base_anchors, mark_anchors

    for lookup in gpos.LookupList.Lookup:
        # LookupType 4 = MarkToBase
        if lookup.LookupType != 4:
            continue
        for sub in lookup.SubTable:
            mark_coverage = sub.MarkCoverage.glyphs
            base_coverage = sub.BaseCoverage.glyphs
            mark_array = sub.MarkArray.MarkRecord
            base_array = sub.BaseArray.BaseRecord
            for mname, mrec in zip(mark_coverage, mark_array):
                cls = f"class{mrec.Class}"
                a = mrec.MarkAnchor
                mark_anchors[mname] = (cls, (a.XCoordinate, a.YCoordinate))
            for bname, brec in zip(base_coverage, base_array):
                d = base_anchors.setdefault(bname, {})
                for i, anchor in enumerate(brec.BaseAnchor):
                    if anchor is None:
                        continue
                    d[f"class{i}"] = (anchor.XCoordinate, anchor.YCoordinate)
    return base_anchors, mark_anchors


# ---------------------------------------------------------------------------
# Glyph composition
# ---------------------------------------------------------------------------

def glyph_bbox(font: TTFont, glyph_name: str) -> tuple[float, float, float, float] | None:
    """Return (xMin, yMin, xMax, yMax) for a glyph, or None if empty."""
    glyph_set = font.getGlyphSet()
    if glyph_name not in glyph_set:
        return None
    from fontTools.pens.boundsPen import BoundsPen
    pen = BoundsPen(glyph_set)
    try:
        glyph_set[glyph_name].draw(pen)
    except Exception:
        return None
    return pen.bounds  # may be None for empty glyphs


def glyph_advance(font: TTFont, glyph_name: str) -> int:
    hmtx = font["hmtx"]
    if glyph_name in hmtx.metrics:
        return hmtx.metrics[glyph_name][0]
    return 0


def topmost_right_x(
    font: TTFont, glyph_name: str, y_tol_frac: float = 0.12,
) -> float | None:
    """X-coordinate of the rightmost on-curve point in the glyph's top region.

    Useful for placing side-marks (horn) on calligraphic / swash fonts
    where the bbox right edge extends well past the actual right stem.
    The rightmost point in the top y_tol_frac slab of the outline is a
    much better proxy for "where the right stem ends at the top".
    """
    glyph_set = font.getGlyphSet()
    if glyph_name not in glyph_set:
        return None
    points: list[tuple[float, float]] = []

    class _Pen:
        def moveTo(self, pt): points.append(pt)
        def lineTo(self, pt): points.append(pt)
        def curveTo(self, *pts): points.extend(p for p in pts if p is not None)
        def qCurveTo(self, *pts): points.extend(p for p in pts if p is not None)
        def closePath(self): pass
        def endPath(self): pass

    try:
        glyph_set[glyph_name].draw(_Pen())
    except Exception:
        return None
    if not points:
        return None
    max_y = max(p[1] for p in points)
    min_y = min(p[1] for p in points)
    h = max_y - min_y
    if h <= 0:
        return None
    tol = h * y_tol_frac
    top_pts = [p for p in points if p[1] >= max_y - tol]
    if not top_pts:
        return None
    return max(p[0] for p in top_pts)


def font_x_height(font: TTFont) -> float:
    """Best-effort x-height. OS/2.sxHeight if present, else bbox of 'x'."""
    if "OS/2" in font and getattr(font["OS/2"], "sxHeight", 0):
        return float(font["OS/2"].sxHeight)
    bb = glyph_bbox(font, "x")
    if bb:
        return bb[3]  # yMax of x
    return 500.0


def font_cap_height(font: TTFont) -> float:
    if "OS/2" in font and getattr(font["OS/2"], "sCapHeight", 0):
        return float(font["OS/2"].sCapHeight)
    bb = glyph_bbox(font, "H")
    if bb:
        return bb[3]
    bb = glyph_bbox(font, "O")
    if bb:
        return bb[3]
    return 700.0


def measure_stroke_thickness(font: TTFont) -> float:
    """Estimate the font's horizontal stroke weight.

    The hyphen-minus glyph is essentially a thin horizontal bar — its
    bbox height is a very good proxy for the font's horizontal stroke
    thickness, and it's present in every font. Falls back to macron /
    underscore, then to a fraction of x-height for fonts missing those.
    """
    cmap = cmap_of(font)
    for cp in (0x002D, 0x00AF, 0x005F):  # -, ¯, _
        if cp not in cmap:
            continue
        bb = glyph_bbox(font, cmap[cp])
        if bb and bb[3] > bb[1]:
            return bb[3] - bb[1]
    return font_x_height(font) * 0.10


# Combining marks that sit above the base, vs below.
ABOVE_MARKS = {0x0300, 0x0301, 0x0302, 0x0303, 0x0306, 0x0309, 0x030A, 0x030B}
BELOW_MARKS = {0x0323, 0x0324, 0x0325, 0x0326, 0x0327, 0x0328}
SIDE_MARKS = {0x031B}  # horn — sits to upper-right of base


def calibrate_mark_gap(font: TTFont) -> float:
    """
    Measure the natural gap between a lowercase letter's top and a
    combining above-mark's bottom by inspecting the font's existing
    composite glyphs. Falls back to 60 units (a reasonable default).

    Vietnamese fonts almost always ship with 'á' (U+00E1) as a composite
    of 'a' + acutecomb. The y-offset of acutecomb in that composite tells
    us exactly how the designer wants the mark stacked on a flat lowercase.
    """
    glyf = font.get("glyf")
    cmap = cmap_of(font)
    candidates = [(0x00E1, 0x0301), (0x00E0, 0x0300), (0x00E3, 0x0303),
                  (0x00F3, 0x0301), (0x00F2, 0x0300)]
    for letter_cp, mark_cp in candidates:
        if letter_cp not in cmap or mark_cp not in cmap:
            continue
        if not glyf:
            return 60.0
        composite_name = cmap[letter_cp]
        mark_name = cmap[mark_cp]
        if composite_name not in glyf:
            continue
        g = glyf[composite_name]
        if not g.isComposite():
            continue
        # Find the base and mark components
        base_name = None
        mark_dy = 0.0
        for c in g.components:
            if c.glyphName == mark_name:
                mark_dy = c.y
            else:
                base_name = c.glyphName
        if base_name is None:
            continue
        bb = glyph_bbox(font, base_name)
        mb = glyph_bbox(font, mark_name)
        if not bb or not mb:
            continue
        # Mark's actual rendered bottom = mb[1] + mark_dy
        # Gap above base = (mb[1] + mark_dy) - bb[3]
        gap = (mb[1] + mark_dy) - bb[3]
        return gap
    return 60.0


def heuristic_offset(
    font: TTFont,
    base: str,
    mark: str,
    mark_codepoint: int | None,
    cap_height: float,
    x_height: float,
    mark_gap: float,
    effective_top: float | None,
    reverse_cm: dict[str, int],
    bare_base: str | None = None,
) -> tuple[float, float]:
    """Fallback positioning when no anchor data is available.

    - Horizontal: center the mark over the base. For below-marks, center
      over `bare_base` if provided (so dots-below on horn-letters like ợ
      sit under the o body, not under the o-plus-horn extent).
    - Vertical: place the mark's bottom `mark_gap` above the effective
      top of the stack. For taller bases (capitals) the mark lifts. For
      stacked marks the second mark sits next to the first.
    - Below-marks: place below baseline, similarly stacked.
    - Horn: anchored to the base's upper-right corner.
    """
    bb = glyph_bbox(font, base)
    mb = glyph_bbox(font, mark)
    if not bb or not mb:
        return (0.0, 0.0)

    if mark_codepoint is None:
        mark_codepoint = reverse_cm.get(mark)

    is_above = mark_codepoint in ABOVE_MARKS if mark_codepoint else mb[1] > 0
    is_side = mark_codepoint in SIDE_MARKS if mark_codepoint else False
    is_below = mark_codepoint in BELOW_MARKS if mark_codepoint else mb[3] <= 0

    base_top = bb[3] if effective_top is None else effective_top
    base_bottom = bb[1]

    # Horizontal centering. For below-marks, optionally center over the
    # bare base (excluding side-marks like horn).
    if is_below and bare_base is not None:
        bare_bb = glyph_bbox(font, bare_base)
        if bare_bb is not None:
            bb_for_centering = bare_bb
        else:
            bb_for_centering = bb
    else:
        bb_for_centering = bb
    bx_center = (bb_for_centering[0] + bb_for_centering[2]) / 2
    mx_center = (mb[0] + mb[2]) / 2
    dx = bx_center - mx_center
    dy = 0.0

    if is_above:
        # Place mark so its bottom is `mark_gap` above the effective top.
        # When stacking onto another above-mark (effective_top > base bbox
        # top), place tightly: same baseline-ish as the previous mark,
        # then shift horizontally to one side. This matches Vietnamese
        # tone-on-circumflex/breve typography.
        #
        # Special case: when the base is a precomposed circumflex letter
        # (Â/Ê/Ô/â/ê/ô), Vietnamese typography places the tone mark to
        # the upper-right of the circumflex peak rather than centered
        # above. Shift the mark right even though we're not in the
        # "stacked on a mark" branch (the precomposed base hides the
        # stack from effective_top).
        base_cp = reverse_cm.get(base)
        base_has_circumflex = (
            base_cp is not None
            and "̂" in unicodedata.normalize("NFD", chr(base_cp))
        )
        if effective_top is not None and effective_top > bb[3] + 5:
            target_bottom = effective_top - (mb[3] - mb[1]) * 0.85
            dy = target_bottom - mb[1]
            dx += (mb[2] - mb[0]) * 0.5
        elif base_has_circumflex and mark_codepoint != 0x0302:
            dy = bb[3] + mark_gap - mb[1]
            dx += (mb[2] - mb[0]) * 0.5
        else:
            dy = bb[3] + mark_gap - mb[1]

    elif is_side:
        # Horn placement:
        #   - X anchor uses `topmost_right_x()` rather than bb[2] — for
        #     calligraphic / swash fonts the bbox right extends past the
        #     actual right stem, so we use the rightmost on-curve point
        #     in the glyph's top region as a stem proxy.
        #   - Y anchor is normalised to cap-height (or x-height for the
        #     lowercase pair) rather than the base's bbox top: round
        #     letters like O have optical overshoot above cap-height,
        #     so using bb[3] would put ơ's horn higher than ư's. Using
        #     the metric height makes ư and ơ horns line up exactly.
        #   - Overlap: 0 for O/o (horn touches the bowl edge without
        #     overlapping into it); a small inset for U/u so the horn
        #     sits cleanly on the right stem.
        base_cp = reverse_cm.get(base)
        is_u_base = base_cp in (0x0055, 0x0075)
        is_o_base = base_cp in (0x004F, 0x006F)
        is_upper = base_cp in (0x004F, 0x0055)

        horn_w = mb[2] - mb[0]
        horn_h = mb[3] - mb[1]

        right_anchor = topmost_right_x(font, base)
        if right_anchor is None:
            right_anchor = bb[2]

        if is_u_base:
            overlap = horn_w * 0.10
        elif is_o_base:
            overlap = 0.0
        else:
            overlap = horn_w * 0.15
        dx = right_anchor - mb[0] - overlap

        # Same target horn-top y for U/u and O/o, computed from the
        # font's metric height (cap-height or x-height) instead of the
        # base bbox top so optical overshoot doesn't unalign them.
        if is_u_base or is_o_base:
            letter_h = cap_height if is_upper else x_height
        else:
            letter_h = base_top - base_bottom
        target_horn_top = letter_h * 0.89 + horn_h * 0.525
        dy = target_horn_top - mb[3]

    elif is_below:
        below_gap = mark_gap * 0.4
        dy = base_bottom - below_gap - mb[3]

    return (dx, dy)


def compose_components(
    font: TTFont,
    target_cp: int,
    cmap: dict[int, str],
    base_anchors: dict,
    mark_anchors: dict,
    cap_height: float,
    x_height: float,
    mark_gap: float,
    reverse_cm: dict[str, int],
) -> list[tuple[str, float, float]] | None:
    """
    Decompose target_cp via NFD, then map each component to a glyph name
    and compute a (dx, dy) offset for it. Returns None if any required
    component glyph is missing from the font.

    Strategy: prefer the longest precomposed prefix of the NFD decomposition
    that already exists as a glyph in the font. For NFD(ấ) = a + circumflex
    + acute, if `acircumflex` (U+00E2) already exists we use it as the base
    and only stack `acute` on top. This lets us reuse anchor data attached
    to acircumflex itself (which says exactly where a tone mark should
    attach above the circumflex peak) rather than guessing a stack offset.

    Tracks the running effective top/bottom of the stack so subsequent
    marks position relative to the last mark, not the bare base.
    """
    decomp = unicodedata.normalize("NFD", chr(target_cp))
    if len(decomp) < 2:
        return None  # not decomposable

    # Save the original NFD root (e.g. 'o' for ợ) before any prefix
    # mutation. Used as `bare_base` for below-mark centering so dots
    # under horn-letters sit under the letter body, not the horn.
    nfd_root_cp = ord(decomp[0])

    # Vietnamese-specific: when NFD gives O + horn + tone (e.g. for ớ),
    # collapse the first two into Ơ (precomposed) if available, since the
    # horn placement is most correct on the precomposed Ơ.
    if (
        len(decomp) >= 3
        and ord(decomp[1]) == 0x031B
        and target_cp not in (0x01A0, 0x01A1, 0x01AF, 0x01B0)
    ):
        precomposed = unicodedata.normalize("NFC", decomp[0] + decomp[1])
        if len(precomposed) == 1 and ord(precomposed) in cmap:
            decomp = precomposed + decomp[2:]

    # Find the longest precomposed prefix that's already in the font.
    # For ấ = a + circumflex + acute, this picks acircumflex if present
    # (so we only need to stack acute, using acircumflex's own anchors).
    base_glyph = None
    remaining_marks: list[str] = []
    for prefix_len in range(len(decomp), 0, -1):
        prefix = unicodedata.normalize("NFC", decomp[:prefix_len])
        if len(prefix) == 1 and ord(prefix) in cmap:
            base_glyph = cmap[ord(prefix)]
            remaining_marks = list(decomp[prefix_len:])
            break

    if base_glyph is None:
        return None
    if not remaining_marks:
        # Precomposed form already exists; no work to do.
        return None

    bare_base = cmap.get(nfd_root_cp)

    components: list[tuple[str, float, float]] = [(base_glyph, 0.0, 0.0)]

    base_bb = glyph_bbox(font, base_glyph)
    if base_bb is None:
        return None
    eff_top = base_bb[3]
    eff_bottom = base_bb[1]

    current_base = base_glyph
    for ch in remaining_marks:
        cp = ord(ch)
        if cp not in cmap:
            return None
        mark_glyph = cmap[cp]

        # Try anchor-based placement first. `current_base` may be a
        # stacked precomposed form like `acircumflex` whose anchors
        # already encode the correct attachment point for the next mark.
        dx = dy = None
        if current_base in base_anchors and mark_glyph in mark_anchors:
            mclass, (mx, my) = mark_anchors[mark_glyph]
            if mclass in base_anchors[current_base]:
                bx, by = base_anchors[current_base][mclass]
                dx = bx - mx
                dy = by - my

        if dx is None:
            dx, dy = heuristic_offset(
                font, current_base, mark_glyph, cp,
                cap_height, x_height, mark_gap,
                eff_top if (cp in ABOVE_MARKS or cp in SIDE_MARKS) else None,
                reverse_cm,
                bare_base=bare_base,
            )

        components.append((mark_glyph, dx, dy))

        # Update the effective top/bottom of the stack.
        mb = glyph_bbox(font, mark_glyph)
        if mb is not None:
            mark_top = mb[3] + dy
            mark_bottom = mb[1] + dy
            if mark_top > eff_top:
                eff_top = mark_top
            if mark_bottom < eff_bottom:
                eff_bottom = mark_bottom

    return components


def add_composite_glyph_truetype(
    font: TTFont,
    glyph_name: str,
    components: list[tuple[str, float, float]],
    advance_width: int,
) -> None:
    """Add a TrueType composite glyph referencing existing components."""
    glyf = font["glyf"]
    pen = TTGlyphPen(font.getGlyphSet())
    for comp_name, dx, dy in components:
        pen.addComponent(comp_name, (1, 0, 0, 1, int(round(dx)), int(round(dy))))
    glyph = pen.glyph()
    glyf[glyph_name] = glyph

    # hmtx: advance width copied from the base, lsb auto.
    font["hmtx"].metrics[glyph_name] = (advance_width, 0)


def add_composite_glyph_cff(
    font: TTFont,
    glyph_name: str,
    components: list[tuple[str, float, float]],
    advance_width: int,
) -> None:
    """
    For CFF fonts there is no native composite glyph mechanism, so we
    flatten: draw each component's outlines, transformed, into a new
    Type 2 CharString.
    """
    glyph_set = font.getGlyphSet()
    pen = T2CharStringPen(advance_width, glyph_set)
    for comp_name, dx, dy in components:
        if comp_name not in glyph_set:
            continue
        tpen = TransformPen(pen, Offset(dx, dy))
        try:
            glyph_set[comp_name].draw(tpen)
        except Exception:
            continue
    charstring = pen.getCharString()

    cff_table = font["CFF "] if "CFF " in font else font["CFF2"]
    top_dict = cff_table.cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    # charstrings need a reference to the Private dict for width encoding
    charstring.private = top_dict.Private
    if glyph_name in char_strings.charStrings:
        char_strings[glyph_name] = charstring
    else:
        # New glyph: __setitem__ requires pre-existing registration, so we
        # insert directly into the internal name→index dict and index list.
        char_strings.charStrings[glyph_name] = len(char_strings.charStrings)
        char_strings.charStringsIndex.append(charstring)

    # Register glyph in CFF charset / private dict ordering.
    if hasattr(top_dict, "charset") and glyph_name not in top_dict.charset:
        top_dict.charset.append(glyph_name)

    font["hmtx"].metrics[glyph_name] = (advance_width, 0)


def register_glyph_order(font: TTFont, glyph_name: str) -> None:
    order = font.getGlyphOrder()
    if glyph_name not in order:
        order.append(glyph_name)
        font.setGlyphOrder(order)


def add_to_cmap(font: TTFont, codepoint: int, glyph_name: str) -> None:
    """Add (codepoint -> glyph_name) to all Unicode cmap subtables."""
    cmap_table = font["cmap"]
    for sub in cmap_table.tables:
        if sub.isUnicode():
            sub.cmap[codepoint] = glyph_name


# ---------------------------------------------------------------------------
# Donor font support
# ---------------------------------------------------------------------------

def import_glyph_from_donor(
    target: TTFont,
    donor: TTFont,
    codepoint: int,
) -> str | None:
    """
    Copy a glyph for `codepoint` from `donor` into `target`. Returns the
    glyph name in target on success, or None if the donor doesn't have it.

    This handles the simple case: outline + advance width. It does not
    transplant kerning, OT features, or color tables.
    """
    donor_cmap = cmap_of(donor)
    if codepoint not in donor_cmap:
        return None
    donor_name = donor_cmap[codepoint]

    # Generate a unique name in target.
    new_name = donor_name
    target_order = target.getGlyphOrder()
    suffix = 0
    while new_name in target_order:
        suffix += 1
        new_name = f"{donor_name}.imp{suffix}"

    donor_glyph_set = donor.getGlyphSet()
    advance = donor.getGlyphSet()[donor_name].width

    if font_is_cff(target):
        pen = T2CharStringPen(advance, donor_glyph_set)
        donor_glyph_set[donor_name].draw(pen)
        cs = pen.getCharString()
        cff_table = target["CFF "] if "CFF " in target else target["CFF2"]
        top_dict = cff_table.cff.topDictIndex[0]
        cs.private = top_dict.Private
        char_strings = top_dict.CharStrings
        if new_name in char_strings.charStrings:
            char_strings[new_name] = cs
        else:
            char_strings.charStrings[new_name] = len(char_strings.charStrings)
            char_strings.charStringsIndex.append(cs)
        if hasattr(top_dict, "charset") and new_name not in top_dict.charset:
            top_dict.charset.append(new_name)
    else:
        # Need TrueType outlines on both sides; if donor is CFF we'd have
        # to convert which is out of scope, so we draw via a TT pen.
        if "glyf" not in target:
            return None
        pen = TTGlyphPen(donor_glyph_set)
        donor_glyph_set[donor_name].draw(pen)
        target["glyf"][new_name] = pen.glyph()

    target["hmtx"].metrics[new_name] = (advance, 0)
    register_glyph_order(target, new_name)
    add_to_cmap(target, codepoint, new_name)
    return new_name


# ---------------------------------------------------------------------------
# Đ / đ synthesis
# ---------------------------------------------------------------------------

def synthesize_dstroke(
    font: TTFont,
    target_cp: int,
    is_cff: bool,
) -> str | None:
    """Build Đ (U+0110) or đ (U+0111) by overlaying a horizontal crossbar
    onto the font's existing D/d outline.

    Đ is not Unicode-decomposable (it's a base letter with a stroke,
    not base + combining mark), so it can't go through compose_components.
    Instead we copy D/d's outline into a new glyph and add a small filled
    rectangle:
      - Đ: crossbar through the middle of the bowl, sticking out a touch
        past the left stem.
      - đ: short crossbar high on the ascender, crossing the right-side
        stem with a small overhang on either side.

    Stroke thickness is inferred from the font's own hyphen-minus glyph
    so the bar matches existing horizontal strokes. Returns the new
    glyph name on success, or None if the source D/d isn't present.
    """
    if target_cp == 0x0110:
        source_cp, base_name = 0x0044, "Dcroat"
        is_upper = True
    elif target_cp == 0x0111:
        source_cp, base_name = 0x0064, "dcroat"
        is_upper = False
    else:
        return None

    cmap = cmap_of(font)
    if source_cp not in cmap:
        return None
    source_glyph = cmap[source_cp]
    bb = glyph_bbox(font, source_glyph)
    if bb is None:
        return None

    bx0, _, bx1, by1 = bb
    bw = bx1 - bx0
    stem = measure_stroke_thickness(font)

    if is_upper:
        # Crossbar through the middle of the bowl. Slightly above visual
        # centre because optical centring sits a touch above geometric.
        cap_h = font_cap_height(font)
        y_center = cap_h * 0.52
        x_left = bx0 - stem * 0.4
        x_right = bx0 + bw * 0.55
    else:
        # Short crossbar high on the ascender, biased to cross the right
        # stem of d. The stem is approximately at the right edge of the
        # bbox; we extend ~2.5 stem widths to the left of it.
        x_h = font_x_height(font)
        y_center = x_h + (by1 - x_h) * 0.62
        x_right = bx1 + stem * 0.3
        x_left = x_right - stem * 2.5

    y_bottom = y_center - stem / 2
    y_top = y_center + stem / 2

    # Pick a unique glyph name (avoid clobbering an unrelated existing entry).
    order = font.getGlyphOrder()
    glyph_name = base_name
    suffix = 0
    while glyph_name in order:
        suffix += 1
        glyph_name = f"{base_name}.syn{suffix}"

    advance = glyph_advance(font, source_glyph)
    glyph_set = font.getGlyphSet()

    def _draw_rect(pen) -> None:
        # Wind the rectangle so it ADDS fill rather than punching a hole.
        # TrueType convention: outer contours are CW. CFF/PostScript: CCW.
        # Match the prevailing convention for the font's outline format.
        pen.moveTo((x_left, y_bottom))
        if is_cff:
            pen.lineTo((x_right, y_bottom))
            pen.lineTo((x_right, y_top))
            pen.lineTo((x_left, y_top))
        else:
            pen.lineTo((x_left, y_top))
            pen.lineTo((x_right, y_top))
            pen.lineTo((x_right, y_bottom))
        pen.closePath()

    if is_cff:
        pen = T2CharStringPen(advance, glyph_set)
        glyph_set[source_glyph].draw(pen)
        _draw_rect(pen)
        cs = pen.getCharString()
        cff_table = font["CFF "] if "CFF " in font else font["CFF2"]
        top_dict = cff_table.cff.topDictIndex[0]
        cs.private = top_dict.Private
        char_strings = top_dict.CharStrings
        if glyph_name in char_strings.charStrings:
            char_strings[glyph_name] = cs
        else:
            char_strings.charStrings[glyph_name] = len(char_strings.charStrings)
            char_strings.charStringsIndex.append(cs)
        if hasattr(top_dict, "charset") and glyph_name not in top_dict.charset:
            top_dict.charset.append(glyph_name)
    else:
        if "glyf" not in font:
            return None
        pen = TTGlyphPen(glyph_set)
        glyph_set[source_glyph].draw(pen)
        _draw_rect(pen)
        font["glyf"][glyph_name] = pen.glyph()

    font["hmtx"].metrics[glyph_name] = (advance, 0)
    register_glyph_order(font, glyph_name)
    add_to_cmap(font, target_cp, glyph_name)
    return glyph_name


# ---------------------------------------------------------------------------
# Combining-mark synthesis (when the font lacks them and no donor is given)
# ---------------------------------------------------------------------------

# Adobe Glyph List names for the synthesised marks, used as the preferred
# glyph name when not already taken in the font.
_MARK_GLYPH_NAMES = {
    0x0300: "gravecomb",
    0x0301: "acutecomb",
    0x0302: "circumflexcomb",
    0x0303: "tildecomb",
    0x0304: "macroncomb",
    0x0306: "brevecomb",
    0x0308: "dieresiscomb",
    0x0309: "hookabovecomb",
    0x031B: "horncomb",
    0x0323: "dotbelowcomb",
}


def _add_polygon(pen, points: list[tuple[float, float]], ccw_outer: bool) -> None:
    """Add a single closed polygonal contour with the right winding.

    `points` is given in CCW order. For TrueType (CW outer) we reverse it
    so the contour fills (rather than punches) under the non-zero winding
    rule the same way as existing glyphs in the font.
    """
    if not ccw_outer:
        points = list(reversed(points))
    pen.moveTo(points[0])
    for p in points[1:]:
        pen.lineTo(p)
    pen.closePath()


def _add_rect(pen, x0: float, y0: float, x1: float, y1: float,
              ccw_outer: bool) -> None:
    _add_polygon(pen, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], ccw_outer)


def _add_slanted_bar(
    pen, cx: float, cy: float, length: float, thickness: float,
    angle_deg: float, ccw_outer: bool,
) -> None:
    """A rectangle of `length` × `thickness` rotated by angle_deg around (cx, cy).

    angle_deg is measured from the positive x-axis: 0 = horizontal,
    90 = vertical, 60 = leaning up-right (acute), 120 = leaning up-left (grave).
    """
    import math
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    # Half-length along the bar's long axis, half-thickness perpendicular to it.
    hx, hy = cos_a * length / 2, sin_a * length / 2
    px, py = -sin_a * thickness / 2, cos_a * thickness / 2
    pts = [
        (cx - hx - px, cy - hy - py),
        (cx + hx - px, cy + hy - py),
        (cx + hx + px, cy + hy + py),
        (cx - hx + px, cy - hy + py),
    ]
    _add_polygon(pen, pts, ccw_outer)


def _draw_mark_outline(pen, codepoint: int, xh: float, cap_h: float,
                       stem: float, ccw: bool) -> None:
    """Draw the outline for a synthesised combining mark into `pen`.

    Geometry is parameterised by the font's x-height and stroke thickness
    so the mark's weight and proportions roughly match existing glyphs.
    The mark's vertical position in glyph space is chosen so its bbox
    sits naturally above x-height (or below baseline for dot-below) — the
    composition step (heuristic_offset) re-positions the mark per base
    via a dy offset, so absolute placement here only needs to be sane.
    """
    rest_y = xh + stem * 0.6  # bottom of above-marks
    cx = 0.0  # marks are centered on x = 0; advance width is 0

    if codepoint == 0x0301:  # acute
        length = xh * 0.45
        cy = rest_y + length * 0.45
        _add_slanted_bar(pen, cx, cy, length, stem * 0.95, 65, ccw)

    elif codepoint == 0x0300:  # grave
        length = xh * 0.45
        cy = rest_y + length * 0.45
        _add_slanted_bar(pen, cx, cy, length, stem * 0.95, 115, ccw)

    elif codepoint == 0x0304:  # macron
        w = xh * 0.55
        t = stem * 0.9
        _add_rect(pen, cx - w / 2, rest_y, cx + w / 2, rest_y + t, ccw)

    elif codepoint == 0x0302:  # circumflex
        w = xh * 0.55
        leg_len = w * 0.62
        leg_t = stem * 0.85
        # Two legs meeting near the top center, forming ^.
        # Left leg leans up-right at ~55°, right leg up-left at ~125°.
        # Place each leg's center so the legs meet at (cx, rest_y + apex_h).
        apex_h = leg_len * 0.85  # vertical reach of the ^
        # Centroid of each leg:
        import math
        a_left = math.radians(55)
        a_right = math.radians(125)
        # Tip of left leg should land at apex (cx, rest_y + apex_h);
        # base of left leg at (cx - w/2, rest_y).
        left_tip = (cx - leg_len * math.cos(a_left) / 2,
                    rest_y + leg_len * math.sin(a_left) / 2)
        # Adjust: place center so base is at left edge of mark.
        lx_center = cx - w / 2 + leg_len * math.cos(a_left) / 2
        ly_center = rest_y + leg_len * math.sin(a_left) / 2
        rx_center = cx + w / 2 + leg_len * math.cos(a_right) / 2
        ry_center = rest_y + leg_len * math.sin(a_right) / 2
        _add_slanted_bar(pen, lx_center, ly_center, leg_len, leg_t, 55, ccw)
        _add_slanted_bar(pen, rx_center, ry_center, leg_len, leg_t, 125, ccw)

    elif codepoint == 0x0306:  # breve  ⌣ (cup, opens up)
        w = xh * 0.58
        h = xh * 0.30
        t = stem * 0.85
        # Bottom horizontal bar
        _add_rect(pen, cx - w / 2, rest_y, cx + w / 2, rest_y + t, ccw)
        # Two short verticals at the ends, going up by h
        _add_rect(pen, cx - w / 2, rest_y + t,
                  cx - w / 2 + t, rest_y + h, ccw)
        _add_rect(pen, cx + w / 2 - t, rest_y + t,
                  cx + w / 2, rest_y + h, ccw)

    elif codepoint == 0x0303:  # tilde  ~ (sine wave)
        # Approximate with two angled bars forming an S-ish shape.
        # Left half rises (positive slope); right half falls (negative slope).
        # The mid-point connects them at the centre of the wave.
        w = xh * 0.65
        amp = xh * 0.10  # vertical amplitude (peak-to-trough / 2)
        t = stem * 0.85
        cy = rest_y + amp + t / 2
        # Left bar: from (-w/2, cy - amp) to (0, cy + amp), tilted up-right.
        seg_len = ((w / 2) ** 2 + (2 * amp) ** 2) ** 0.5
        import math
        # Left segment goes from (-w/2, cy - amp) to (0, cy + amp): up-right.
        a_left_deg = math.degrees(math.atan2(2 * amp, w / 2))
        # Right segment goes from (0, cy + amp) to (w/2, cy - amp): down-right.
        a_right_deg = -a_left_deg
        _add_slanted_bar(pen, -w / 4, cy, seg_len, t, a_left_deg, ccw)
        _add_slanted_bar(pen, w / 4, cy, seg_len, t, a_right_deg, ccw)

    elif codepoint == 0x0308:  # dieresis (two dots)
        d = stem * 1.25
        gap = stem * 1.4
        for sign in (-1, 1):
            x_c = cx + sign * gap
            _add_rect(pen, x_c - d / 2, rest_y,
                      x_c + d / 2, rest_y + d, ccw)

    elif codepoint == 0x0309:  # hook above ◌̉
        # Resembles a small ?. Vertical stroke at top, hook curling left
        # near the bottom of the mark. Approximated with rectangles.
        h = xh * 0.45
        t = stem * 0.85
        # Vertical stem (upper part)
        stem_top = rest_y + h
        stem_bot = rest_y + h * 0.45
        _add_rect(pen, cx - t / 2, stem_bot,
                  cx + t / 2, stem_top, ccw)
        # Hook curl: a horizontal segment going left, then down — two bars.
        hook_y = stem_bot
        hook_h = stem_bot - rest_y
        # Top of hook (horizontal piece going left from stem)
        _add_rect(pen, cx - t * 2.0, hook_y,
                  cx + t / 2, hook_y + t, ccw)
        # Down stroke at the left end of the hook
        _add_rect(pen, cx - t * 2.0, rest_y,
                  cx - t * 2.0 + t, hook_y + t, ccw)

    elif codepoint == 0x0323:  # dot below
        d = stem * 1.2
        # Place below baseline: bbox from -d*1.6 .. -d*0.4, so mb[3] < 0.
        y_top = -stem * 0.4
        y_bot = y_top - d
        _add_rect(pen, cx - d / 2, y_bot, cx + d / 2, y_top, ccw)

    elif codepoint == 0x031B:  # horn (side mark, upper right)
        # Small comma/hook shape. heuristic_offset positions it so its
        # bbox right edge sits flush with the base's right side.
        # Draw a triangle-ish polygon for the hook.
        hook_h = (cap_h - xh) * 0.6 + xh * 0.25
        w = stem * 1.4
        t = stem * 0.85
        y0 = cap_h * 0.55
        y1 = y0 + hook_h
        # Polygon (CCW): bottom-left, bottom-right, top-right, top-left curl.
        pts = [
            (cx, y0),
            (cx + w, y0),
            (cx + w, y1),
            (cx + w * 0.55, y1 + t * 0.5),
            (cx + w * 0.15, y1 - t * 0.4),
            (cx, y1 - t * 1.2),
        ]
        _add_polygon(pen, pts, ccw)


# Spacing/standalone glyphs whose outlines can stand in for a missing
# combining mark. The first available codepoint wins; reuse is preferred
# over geometric synthesis because the font designer drew these shapes
# to match the rest of the font's hand. Order each list from "best fit"
# to "acceptable fallback".
_MARK_SOURCE_CODEPOINTS: dict[int, list[int]] = {
    0x0301: [0x00B4, 0x0027],          # acute       ← ´  '
    0x0300: [0x0060],                  # grave       ← `
    0x0302: [0x02C6, 0x005E],          # circumflex  ← ˆ  ^
    0x0303: [0x02DC, 0x007E],          # tilde       ← ˜  ~
    0x0304: [0x00AF],                  # macron      ← ¯
    0x0306: [0x02D8],                  # breve       ← ˘
    0x0308: [0x00A8],                  # dieresis    ← ¨
    0x031B: [0x2019, 0x02BC, 0x0027],  # horn        ← ’  ʼ  '
    0x0323: [0x002E, 0x00B7],          # dot below   ← .  ·
    # 0x0309 (hook above) intentionally absent: no spacing equivalent in
    # standard fonts is close enough to a Vietnamese hook above.
}


def derive_mark_from_existing(
    font: TTFont, mark_cp: int, is_cff: bool,
) -> str | None:
    """Build a combining mark by copying a similar spacing glyph's outline.

    For example, the spacing acute (U+00B4 ´) in the font has already been
    drawn by the designer to match the font's stroke weight and style;
    reusing its outline as the combining acute (U+0301) gives a much
    better visual match than a geometric polygon. The composition step
    re-positions the mark via a per-base dy offset, so absolute placement
    of the source glyph matters only for below-marks (where we lower the
    glyph so its bbox sits below the baseline).
    """
    if mark_cp not in _MARK_SOURCE_CODEPOINTS:
        return None
    cmap = cmap_of(font)
    if mark_cp in cmap:
        return cmap[mark_cp]

    source_cp = next(
        (cp for cp in _MARK_SOURCE_CODEPOINTS[mark_cp] if cp in cmap), None,
    )
    if source_cp is None:
        return None

    source_name = cmap[source_cp]
    bb = glyph_bbox(font, source_name)
    if bb is None or bb[2] <= bb[0] or bb[3] <= bb[1]:
        return None

    # Below marks: shift outline downward so its bbox top sits below the
    # baseline. heuristic_offset's BELOW branch positions by `mb[3]`, so
    # an unshifted period (bbox y in [0, ~100]) would be misclassified
    # by the bbox-based branch detector and placed wrong.
    dy = 0.0
    if mark_cp == 0x0323:
        stem = measure_stroke_thickness(font)
        dy = -bb[3] - stem * 0.4

    glyph_set = font.getGlyphSet()
    advance = 0  # combining marks have zero advance width

    if is_cff:
        pen = T2CharStringPen(advance, glyph_set)
    else:
        if "glyf" not in font:
            return None
        pen = TTGlyphPen(glyph_set)

    if dy != 0.0:
        glyph_set[source_name].draw(TransformPen(pen, Offset(0, dy)))
    else:
        glyph_set[source_name].draw(pen)

    base_name = _MARK_GLYPH_NAMES.get(mark_cp, f"uni{mark_cp:04X}")
    order = font.getGlyphOrder()
    glyph_name = base_name
    suffix = 0
    while glyph_name in order:
        suffix += 1
        glyph_name = f"{base_name}.der{suffix}"

    if is_cff:
        cs = pen.getCharString()
        cff_table = font["CFF "] if "CFF " in font else font["CFF2"]
        top_dict = cff_table.cff.topDictIndex[0]
        cs.private = top_dict.Private
        char_strings = top_dict.CharStrings
        if glyph_name in char_strings.charStrings:
            char_strings[glyph_name] = cs
        else:
            char_strings.charStrings[glyph_name] = len(char_strings.charStrings)
            char_strings.charStringsIndex.append(cs)
        if hasattr(top_dict, "charset") and glyph_name not in top_dict.charset:
            top_dict.charset.append(glyph_name)
    else:
        font["glyf"][glyph_name] = pen.glyph()

    font["hmtx"].metrics[glyph_name] = (advance, 0)
    register_glyph_order(font, glyph_name)
    add_to_cmap(font, mark_cp, glyph_name)
    return glyph_name


def synthesize_mark(font: TTFont, codepoint: int, is_cff: bool) -> str | None:
    """Build a missing combining mark glyph from primitives.

    Used when the target font lacks a mark required for Vietnamese
    composition (e.g. breve, dot below, horn) and no donor is provided.
    The mark's geometry scales to the font's x-height and stroke thickness
    so weight roughly matches; on script/display fonts the result is
    geometric and won't truly match the design — it's a fallback to make
    the font functional rather than an aesthetic match.

    Combining marks have zero advance width and live at fixed positions in
    glyph space. The composition step shifts them per-base via a dy offset.
    """
    if codepoint not in _MARK_GLYPH_NAMES:
        return None

    cmap = cmap_of(font)
    if codepoint in cmap:
        return cmap[codepoint]

    xh = font_x_height(font)
    cap_h = font_cap_height(font)
    stem = measure_stroke_thickness(font)

    glyph_set = font.getGlyphSet()
    advance = 0  # combining marks: zero advance width

    # CCW outer convention for CFF/PostScript, CW for TrueType.
    ccw = bool(is_cff)

    if is_cff:
        pen = T2CharStringPen(advance, glyph_set)
    else:
        if "glyf" not in font:
            return None
        pen = TTGlyphPen(glyph_set)

    _draw_mark_outline(pen, codepoint, xh, cap_h, stem, ccw)

    base_name = _MARK_GLYPH_NAMES[codepoint]
    order = font.getGlyphOrder()
    glyph_name = base_name
    suffix = 0
    while glyph_name in order:
        suffix += 1
        glyph_name = f"{base_name}.syn{suffix}"

    if is_cff:
        cs = pen.getCharString()
        cff_table = font["CFF "] if "CFF " in font else font["CFF2"]
        top_dict = cff_table.cff.topDictIndex[0]
        cs.private = top_dict.Private
        char_strings = top_dict.CharStrings
        if glyph_name in char_strings.charStrings:
            char_strings[glyph_name] = cs
        else:
            char_strings.charStrings[glyph_name] = len(char_strings.charStrings)
            char_strings.charStringsIndex.append(cs)
        if hasattr(top_dict, "charset") and glyph_name not in top_dict.charset:
            top_dict.charset.append(glyph_name)
    else:
        font["glyf"][glyph_name] = pen.glyph()

    font["hmtx"].metrics[glyph_name] = (advance, 0)
    register_glyph_order(font, glyph_name)
    add_to_cmap(font, codepoint, glyph_name)
    return glyph_name


def synthesize_horned_base(
    font: TTFont, target_cp: int, is_cff: bool,
) -> str | None:
    """Build Ơ/ơ/Ư/ư by overlaying a horn shape onto O/o/U/u.

    These four letters aren't Unicode-decomposable into base + combining
    horn at NFC level (they ARE under NFD: U+01A0 → O + U+031B), so we
    let `compose_components` handle the NFD path normally — provided the
    horn mark glyph exists. This function exists for the case where neither
    the precomposed nor the horn mark are available: we draw a horn
    directly onto the base.
    """
    if target_cp == 0x01A0:
        source_cp = 0x004F  # O
        base_name = "Ohorn"
    elif target_cp == 0x01A1:
        source_cp = 0x006F  # o
        base_name = "ohorn"
    elif target_cp == 0x01AF:
        source_cp = 0x0055  # U
        base_name = "Uhorn"
    elif target_cp == 0x01B0:
        source_cp = 0x0075  # u
        base_name = "uhorn"
    else:
        return None

    cmap = cmap_of(font)
    if source_cp not in cmap:
        return None
    source_glyph = cmap[source_cp]
    bb = glyph_bbox(font, source_glyph)
    if bb is None:
        return None

    is_upper = target_cp in (0x01A0, 0x01AF)
    bx0, by0, bx1, by1 = bb
    stem = measure_stroke_thickness(font)
    cap_h = font_cap_height(font)
    xh = font_x_height(font)

    # Horn dimensions: small comma-like flick at the upper-right shoulder.
    horn_h = (by1 - (cap_h * 0.5 if is_upper else xh * 0.5)) * 0.55
    horn_h = max(horn_h, stem * 2.5)
    horn_w = stem * 1.35
    t = stem * 0.85

    # Anchor: top-right of the base, with a slight overlap into the bowl
    # so the horn looks attached rather than floating.
    overlap = horn_w * 0.20
    x0 = bx1 - overlap
    y_top = by1 + horn_h * 0.18  # horn rises slightly above the base top
    y_bot = y_top - horn_h

    glyph_set = font.getGlyphSet()
    advance = glyph_advance(font, source_glyph)

    if is_cff:
        pen = T2CharStringPen(advance, glyph_set)
        ccw = True
    else:
        if "glyf" not in font:
            return None
        pen = TTGlyphPen(glyph_set)
        ccw = False

    glyph_set[source_glyph].draw(pen)

    # Horn polygon (CCW): left edge of horn going down from bowl, curling
    # slightly outward at the top to suggest a comma flick.
    pts = [
        (x0, y_bot),
        (x0 + t, y_bot),
        (x0 + horn_w, y_top - t * 1.0),
        (x0 + horn_w * 0.65, y_top + t * 0.4),
        (x0 + horn_w * 0.20, y_top - t * 0.2),
        (x0, y_top - t * 1.4),
    ]
    _add_polygon(pen, pts, ccw)

    order = font.getGlyphOrder()
    glyph_name = base_name
    suffix = 0
    while glyph_name in order:
        suffix += 1
        glyph_name = f"{base_name}.syn{suffix}"

    if is_cff:
        cs = pen.getCharString()
        cff_table = font["CFF "] if "CFF " in font else font["CFF2"]
        top_dict = cff_table.cff.topDictIndex[0]
        cs.private = top_dict.Private
        char_strings = top_dict.CharStrings
        if glyph_name in char_strings.charStrings:
            char_strings[glyph_name] = cs
        else:
            char_strings.charStrings[glyph_name] = len(char_strings.charStrings)
            char_strings.charStringsIndex.append(cs)
        if hasattr(top_dict, "charset") and glyph_name not in top_dict.charset:
            top_dict.charset.append(glyph_name)
    else:
        font["glyf"][glyph_name] = pen.glyph()

    font["hmtx"].metrics[glyph_name] = (advance, 0)
    register_glyph_order(font, glyph_name)
    add_to_cmap(font, target_cp, glyph_name)
    return glyph_name


# ---------------------------------------------------------------------------
# Diacritic-stack-root re-composition
# ---------------------------------------------------------------------------

# Native precomposed letters whose Vietnamese tone-stacked descendants
# (e.g. Ấ, ế, Ố, Ằ) are built on top of them. Each entry: (target,
# bare_base, mark). When the mark was newly added (not native to the
# source font), the font's native precomposed target may have been drawn
# with a different mark glyph than the one we now use for descendants —
# overwriting the target with a fresh composition keeps Ô consistent
# with Ố (= Ô + acute) and Ă consistent with Ằ (= Ă + grave), etc.
_DIACRITIC_STACK_ROOTS: list[tuple[int, int, int]] = [
    (0x00C2, 0x0041, 0x0302),  # Â = A + circumflex
    (0x00E2, 0x0061, 0x0302),  # â
    (0x00CA, 0x0045, 0x0302),  # Ê
    (0x00EA, 0x0065, 0x0302),  # ê
    (0x00D4, 0x004F, 0x0302),  # Ô
    (0x00F4, 0x006F, 0x0302),  # ô
    (0x0102, 0x0041, 0x0306),  # Ă = A + breve
    (0x0103, 0x0061, 0x0306),  # ă
]


def recompose_stack_roots(
    font: TTFont,
    newly_added_marks: set[int],
    base_anchors: dict,
    mark_anchors: dict,
    cap_height: float,
    x_height: float,
    mark_gap: float,
    is_cff: bool,
    composition_map: dict[str, list[tuple[str, float, float]]],
) -> list[int]:
    """Overwrite native Â/â/Ê/ê/Ô/ô/Ă/ă with our own composition when the
    relevant mark was newly added. See _DIACRITIC_STACK_ROOTS for why.

    Returns the list of recomposed codepoints.
    """
    recomposed: list[int] = []
    cmap = cmap_of(font)
    reverse_cm = reverse_cmap(font)
    for target_cp, base_cp, mark_cp in _DIACRITIC_STACK_ROOTS:
        if mark_cp not in newly_added_marks:
            continue
        if base_cp not in cmap or mark_cp not in cmap:
            continue
        base_glyph = cmap[base_cp]
        mark_glyph = cmap[mark_cp]

        dx = dy = None
        if base_glyph in base_anchors and mark_glyph in mark_anchors:
            mclass, (mx, my) = mark_anchors[mark_glyph]
            if mclass in base_anchors[base_glyph]:
                bx, by = base_anchors[base_glyph][mclass]
                dx, dy = bx - mx, by - my
        if dx is None:
            dx, dy = heuristic_offset(
                font, base_glyph, mark_glyph, mark_cp,
                cap_height, x_height, mark_gap, None, reverse_cm,
            )

        components = [(base_glyph, 0.0, 0.0), (mark_glyph, dx, dy)]
        adv = glyph_advance(font, base_glyph)
        glyph_name = f"uni{target_cp:04X}"
        if is_cff:
            add_composite_glyph_cff(font, glyph_name, components, adv)
        else:
            add_composite_glyph_truetype(font, glyph_name, components, adv)
        register_glyph_order(font, glyph_name)
        add_to_cmap(font, target_cp, glyph_name)
        composition_map[glyph_name] = components
        recomposed.append(target_cp)
    return recomposed


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def rename_font(font: TTFont, new_family: str) -> None:
    """Update the font's name table so it doesn't conflict with the source font."""
    name_table = font["name"]
    # Derive a PostScript-safe name (no spaces/special chars).
    ps_name = "".join(c for c in new_family if c.isalnum() or c in "-_")

    for record in name_table.names:
        if record.nameID == 1:   # Family name
            record.string = new_family
        elif record.nameID == 3: # Unique identifier
            try:
                current = record.toUnicode()
            except Exception:
                current = ""
            record.string = f"{ps_name}:{current}".encode(record.getEncoding())
        elif record.nameID == 4: # Full name
            try:
                subfam = name_table.getName(2, record.platformID,
                                            record.platEncID, record.langID)
                subfam_str = subfam.toUnicode() if subfam else "Regular"
            except Exception:
                subfam_str = "Regular"
            full = f"{new_family} {subfam_str}" if subfam_str != "Regular" else new_family
            record.string = full
        elif record.nameID == 6: # PostScript name
            try:
                subfam = name_table.getName(2, record.platformID,
                                            record.platEncID, record.langID)
                subfam_str = subfam.toUnicode() if subfam else "Regular"
            except Exception:
                subfam_str = "Regular"
            # PostScript names use hyphen-separated style
            ps = ps_name if subfam_str == "Regular" else f"{ps_name}-{subfam_str}"
            record.string = ps


def vietnamize(
    input_path: Path,
    output_path: Path,
    donor_path: Path | None = None,
    font_name: str | None = None,
    validate: bool = False,
    validate_provider: str = "claude",
    api_key: str | None = None,
    validate_model: str | None = None,
    max_iterations: int = 3,
    log_path: Path | None = None,
    verbose: bool = False,
) -> dict:
    font = TTFont(str(input_path))
    cmap = cmap_of(font)

    missing = missing_codepoints(font, VIETNAMESE_CODEPOINTS)
    if verbose:
        print(f"Missing Vietnamese codepoints at start: {len(missing)}", file=sys.stderr)

    base_anchors, mark_anchors = collect_anchors(font)
    if verbose:
        print(
            f"Found {len(base_anchors)} bases and {len(mark_anchors)} marks "
            "with anchor data",
            file=sys.stderr,
        )

    donor = TTFont(str(donor_path)) if donor_path else None

    composed: list[int] = []
    imported: list[int] = []
    failed: list[int] = []
    composition_map: dict[str, list[tuple[str, float, float]]] = {}

    is_cff = font_is_cff(font)

    cap_height = font_cap_height(font)
    x_height = font_x_height(font)
    mark_gap = calibrate_mark_gap(font)
    if verbose:
        print(
            f"cap_height={cap_height}, x_height={x_height}, "
            f"mark_gap={mark_gap}",
            file=sys.stderr,
        )

    # Pre-pass: ensure the combining marks Vietnamese composition relies on
    # are present in the font. Order of preference per missing mark:
    #   1. donor font (if provided) — outlines match a real designer's hand
    #   2. derive from an existing similar glyph in the source font itself
    #      (e.g. spacing acute U+00B4 → combining acute U+0301, right
    #      single quote U+2019 → horn U+031B). The shape was drawn by the
    #      font's designer, so it stays in style.
    #   3. synthesise from primitives — geometric shapes scaled to the
    #      font's stroke weight and x-height. Quality is "functional, not
    #      pretty" — script/display fonts will look mismatched, but the
    #      result is enough to compose every Vietnamese precomposed glyph.
    # We deliberately do NOT import precomposed bases (Ơ, Ư, Ă, Â, Ê, Ô)
    # from the donor: those are full letter outlines whose metrics belong
    # to the donor's design. We compose them from the target's own base
    # + the imported / derived / synthesised mark for visual consistency.
    mark_prerequisites = [
        0x0300, 0x0301, 0x0302, 0x0303, 0x0306, 0x0309, 0x031B, 0x0323,
    ]
    synthesized_marks: set[int] = set()
    newly_added_marks: set[int] = set()  # marks NOT native to the source font
    for cp in mark_prerequisites:
        if cp in cmap_of(font):
            continue
        if donor is not None and import_glyph_from_donor(font, donor, cp):
            imported.append(cp)
            newly_added_marks.add(cp)
            continue
        if derive_mark_from_existing(font, cp, is_cff):
            composed.append(cp)
            newly_added_marks.add(cp)
            continue
        if synthesize_mark(font, cp, is_cff):
            composed.append(cp)
            synthesized_marks.add(cp)
            newly_added_marks.add(cp)

    # Đ/đ are base letters with a stroke, not Unicode-decomposable.
    # Prefer donor when one is provided (consistent with the rest of the
    # donor's contributions). Otherwise synthesize by drawing a crossbar
    # onto the font's own D/d outline so the result stays style-matched.
    for cp in (0x0110, 0x0111):
        if cp in cmap_of(font):
            continue
        if donor is not None and import_glyph_from_donor(font, donor, cp):
            imported.append(cp)
            continue
        if synthesize_dstroke(font, cp, is_cff):
            composed.append(cp)

    # Ơ/ơ/Ư/ư: only fall back to direct horn-on-base synthesis when the
    # horn mark itself had to be drawn from primitives. When the horn is
    # natural (font / donor / derived from a real apostrophe), composing
    # via the regular pipeline + heuristic_offset produces a designer-
    # drawn horn shape attached at a sensible position — and that's
    # better than overwriting with a geometric polygon.
    if 0x031B in synthesized_marks or 0x031B not in cmap_of(font):
        for cp in (0x01A0, 0x01A1, 0x01AF, 0x01B0):
            if cp in cmap_of(font):
                continue
            if synthesize_horned_base(font, cp, is_cff):
                composed.append(cp)

    # When a mark was newly added (derived/synthesised), the font's
    # native Â/Ê/Ô/Ă (and lowercase) may use a different glyph for that
    # mark than the one we just installed — making Vietnamese stacks like
    # Ố (= Ô + acute) look inconsistent with the bare Ô. Re-compose the
    # native targets so they share our mark glyph.
    composed.extend(
        recompose_stack_roots(
            font, newly_added_marks, base_anchors, mark_anchors,
            cap_height, x_height, mark_gap, is_cff, composition_map,
        )
    )

    # We may need to compose codepoints in passes — Ớ depends on Ơ — so
    # iterate until no progress is made. Order matters: simple two-piece
    # composites (Ơ = O + horn) must complete before three-piece ones
    # (Ớ = Ơ + acute) can find their base.
    remaining = [cp for cp in missing if cp not in cmap_of(font)]
    # Sort so shorter NFD decompositions go first.
    remaining.sort(key=lambda cp: len(unicodedata.normalize("NFD", chr(cp))))
    progress = True
    while progress and remaining:
        progress = False
        next_round: list[int] = []
        reverse_cm = reverse_cmap(font)
        for cp in remaining:
            cmap = cmap_of(font)  # refresh after each addition
            comps = compose_components(
                font, cp, cmap, base_anchors, mark_anchors,
                cap_height, x_height, mark_gap, reverse_cm,
            )
            if comps is None:
                next_round.append(cp)
                continue

            base_name = comps[0][0]
            adv = glyph_advance(font, base_name)
            glyph_name = f"uni{cp:04X}"

            if is_cff:
                add_composite_glyph_cff(font, glyph_name, comps, adv)
            else:
                add_composite_glyph_truetype(font, glyph_name, comps, adv)
            register_glyph_order(font, glyph_name)
            add_to_cmap(font, cp, glyph_name)
            composition_map[glyph_name] = comps
            composed.append(cp)
            progress = True
        remaining = next_round

    # Anything left after composition: try the donor.
    if remaining and donor is not None:
        for cp in remaining:
            new_name = import_glyph_from_donor(font, donor, cp)
            if new_name:
                imported.append(cp)
            else:
                failed.append(cp)
    else:
        failed.extend(remaining)

    # OS/2 Unicode range bit 29 covers Latin Extended Additional, which is
    # where most Vietnamese precomposed lives. Set it so apps recognize
    # the font as supporting Vietnamese.
    if "OS/2" in font and composed:
        os2 = font["OS/2"]
        os2.ulUnicodeRange2 |= (1 << (57 - 32))  # bit 57 = Latin Ext Add
        # Also bit for Vietnamese codepage (1258 → bit 8 in ulCodePageRange1)
        if hasattr(os2, "ulCodePageRange1"):
            os2.ulCodePageRange1 |= (1 << 8)

    validation_quality = None
    if validate and composed:
        effective_log = log_path or output_path.with_name(
            output_path.stem + "_viet_hoa_validate.jsonl"
        )
        effective_model = validate_model or _PROVIDER_DEFAULT_MODEL.get(
            validate_provider, "claude-opus-4-7"
        )
        font, validation_quality = run_validation_loop(
            font, composition_map, composed,
            input_path, output_path,
            validate_provider, api_key, effective_model, max_iterations,
            effective_log, is_cff, verbose,
        )

    if font_name:
        rename_font(font, font_name)

    font.save(str(output_path))

    return {
        "composed": composed,
        "imported": imported,
        "failed": failed,
        "total_target": len(VIETNAMESE_CODEPOINTS),
        "validation_quality": validation_quality,
    }


# ---------------------------------------------------------------------------
# AI validation (optional — requires pillow + at least one provider SDK)
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ImageDraw, ImageFont as _PILFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    import anthropic as _anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

try:
    from google import genai as _genai  # pip install google-genai
    from google.genai import types as _genai_types
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

try:
    import openai as _openai              # pip install openai  (used for OpenRouter)
    _OPENAI_OK = True
except ImportError:
    _OPENAI_OK = False

_PROVIDER_ENV = {
    "claude":      "ANTHROPIC_API_KEY",
    "gemini":      "GEMINI_API_KEY",
    "openrouter":  "OPENROUTER_API_KEY",
}
_PROVIDER_DEFAULT_MODEL = {
    "claude":      "claude-opus-4-7",
    "gemini":      "gemini-2.0-flash",
    "openrouter":  "google/gemini-2.0-flash-001",
}
_PROVIDER_SDK = {
    "claude":      ("anthropic",            _ANTHROPIC_OK),
    "gemini":      ("google-genai",          _GEMINI_OK),
    "openrouter":  ("openai",               _OPENAI_OK),
}


_VALIDATE_PROMPT = """\
You are a Vietnamese typography expert. The image shows Vietnamese characters \
rendered from a font, each labelled with its Unicode codepoint.

Identify positioning problems such as:
• Diacritical marks overlapping each other (e.g. circumflex colliding with tone mark)
• Excessive gap between the base letter and its mark (mark floats too high)
• Mark touching or clashing with the base letter
• Mark not horizontally centred over its base
• Dot-below not properly positioned

Return ONLY valid JSON (no markdown fences):
{
  "overall_quality": "good" | "fair" | "poor",
  "issues": [
    {
      "char": "<character>",
      "codepoint": "U+XXXX",
      "issue": "<concise description>",
      "severity": "low" | "medium" | "high",
      "mark_index": <1-based index of the problematic mark>,
      "adjustment": {
        "direction": "up" | "down" | "left" | "right",
        "magnitude": "small" | "medium" | "large"
      }
    }
  ],
  "notes": "<general observations>"
}

If there are no issues, return an empty issues list.\
"""

_MAGNITUDE = {"small": 20, "medium": 50, "large": 100}


def _require_validate_deps(provider: str) -> None:
    missing = []
    if not _PIL_OK:
        missing.append("pillow")
    sdk_pkg, sdk_ok = _PROVIDER_SDK.get(provider, ("unknown", False))
    if not sdk_ok:
        missing.append(sdk_pkg)
    if missing:
        raise SystemExit(
            f"error: --validate --provider {provider} requires: "
            f"pip install {' '.join(missing)}"
        )


def _render_chars(font_path: Path, codepoints: list[int], cell: int = 80) -> bytes:
    """Return a PNG (bytes) with all codepoints rendered in a grid."""
    cols = 8
    rows = (len(codepoints) + cols - 1) // cols
    pad = 14
    label_h = 16
    cw, ch = cell + pad, cell + pad + label_h
    img = Image.new("RGB", (cols * cw, rows * ch), "white")
    draw = ImageDraw.Draw(img)
    try:
        pfont = _PILFont.truetype(str(font_path), cell)
    except Exception as exc:
        raise RuntimeError(f"Cannot load font for rendering: {exc}") from exc
    lfont = _PILFont.load_default()
    for i, cp in enumerate(codepoints):
        col, row = i % cols, i // cols
        x, y = col * cw + pad // 2, row * ch + pad // 2
        try:
            draw.text((x, y), chr(cp), fill="black", font=pfont)
        except Exception:
            draw.text((x, y), "?", fill="red", font=pfont)
        draw.text((x, y + cell + 2), f"U+{cp:04X}", fill="#888", font=lfont)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _parse_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from a model response."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return {"overall_quality": "unknown", "issues": [],
                "notes": "Failed to parse model response as JSON.", "raw": raw}


def _call_claude(image_bytes: bytes, api_key: str | None, model: str) -> dict:
    client = _anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": _VALIDATE_PROMPT},
            ],
        }],
    )
    return _parse_response(msg.content[0].text)


def _call_gemini(image_bytes: bytes, api_key: str | None, model: str) -> dict:
    client = _genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[
            _genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            _VALIDATE_PROMPT,
        ],
    )
    return _parse_response(response.text)


def _call_openrouter(image_bytes: bytes, api_key: str | None, model: str) -> dict:
    client = _openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = client.chat.completions.create(
        model=model,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": _VALIDATE_PROMPT},
            ],
        }],
    )
    return _parse_response(resp.choices[0].message.content or "")


def _call_provider(provider: str, image_bytes: bytes,
                   api_key: str | None, model: str) -> dict:
    if provider == "claude":
        return _call_claude(image_bytes, api_key, model)
    if provider == "gemini":
        return _call_gemini(image_bytes, api_key, model)
    if provider == "openrouter":
        return _call_openrouter(image_bytes, api_key, model)
    raise ValueError(f"Unknown provider: {provider!r}")


def _shift_tt_component(font: TTFont, glyph_name: str,
                         mark_idx: int, ddx: int, ddy: int) -> bool:
    """Move component mark_idx (1-based) in a TrueType composite glyph."""
    glyf = font.get("glyf")
    if not glyf or glyph_name not in glyf:
        return False
    g = glyf[glyph_name]
    if not g.isComposite():
        return False
    idx = min(mark_idx, len(g.components) - 1)
    c = g.components[idx]
    c.x = int(round(getattr(c, "x", 0) + ddx))
    c.y = int(round(getattr(c, "y", 0) + ddy))
    return True


def _shift_cff_component(
    font: TTFont,
    composition_map: dict[str, list[tuple[str, float, float]]],
    glyph_name: str,
    mark_idx: int,
    ddx: float,
    ddy: float,
) -> bool:
    """Update composition_map and redraw the flattened CFF outline."""
    if glyph_name not in composition_map:
        return False
    comps = composition_map[glyph_name]
    idx = min(mark_idx, len(comps) - 1)
    name, dx, dy = comps[idx]
    comps[idx] = (name, dx + ddx, dy + ddy)
    adv = glyph_advance(font, comps[0][0])
    add_composite_glyph_cff(font, glyph_name, comps, adv)
    return True


def _apply_adjustment(
    font: TTFont,
    composition_map: dict[str, list[tuple[str, float, float]]],
    cp: int, mark_idx: int, ddx: float, ddy: float, is_cff: bool,
) -> bool:
    glyph_name = f"uni{cp:04X}"
    if is_cff:
        return _shift_cff_component(font, composition_map, glyph_name,
                                    mark_idx, ddx, ddy)
    return _shift_tt_component(font, glyph_name, mark_idx, int(ddx), int(ddy))


def _append_log(log_path: Path, entry: dict) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")


def run_validation_loop(
    font: TTFont,
    composition_map: dict[str, list[tuple[str, float, float]]],
    composed_cps: list[int],
    input_path: Path,
    output_path: Path,
    provider: str,
    api_key: str | None,
    model: str,
    max_iterations: int,
    log_path: Path,
    is_cff: bool,
    verbose: bool,
) -> tuple[TTFont, str]:
    """
    Render composed glyphs → ask AI to validate → apply adjustments.
    Repeats up to max_iterations times. Appends JSONL entries to log_path.
    Returns (font, final_quality_string).
    """
    if not composed_cps:
        return font, "n/a"

    _append_log(log_path, {
        "type": "run_start",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input_font": str(input_path),
        "output_font": str(output_path),
        "provider": provider,
        "model": model,
        "composed_count": len(composed_cps),
        "max_iterations": max_iterations,
    })

    tmp = output_path.with_suffix(".vtmp" + output_path.suffix)
    final_quality = "unknown"

    try:
        for iteration in range(1, max_iterations + 1):
            if verbose:
                print(f"[validate] iteration {iteration}/{max_iterations}", file=sys.stderr)

            font.save(str(tmp))
            img_bytes = _render_chars(tmp, composed_cps)
            result = _call_provider(provider, img_bytes, api_key, model)

            quality = result.get("overall_quality", "unknown")
            issues: list[dict] = result.get("issues") or []
            final_quality = quality

            if verbose:
                print(f"[validate] quality={quality} issues={len(issues)}", file=sys.stderr)

            adjustments: list[dict] = []
            for iss in issues:
                cp_str = iss.get("codepoint", "")
                try:
                    cp = int(cp_str.replace("U+", ""), 16)
                except ValueError:
                    continue

                adj = iss.get("adjustment") or {}
                direction = adj.get("direction", "")
                mag = float(_MAGNITUDE.get(adj.get("magnitude", "small"), 20))
                mark_idx = int(iss.get("mark_index", 1))

                ddx = mag if direction == "right" else (-mag if direction == "left" else 0.0)
                ddy = mag if direction == "up" else (-mag if direction == "down" else 0.0)
                if ddx == 0 and ddy == 0:
                    continue

                ok = _apply_adjustment(font, composition_map, cp,
                                       mark_idx, ddx, ddy, is_cff)
                if ok:
                    adjustments.append({
                        "codepoint": cp_str, "char": iss.get("char", ""),
                        "mark_index": mark_idx, "ddx": ddx, "ddy": ddy,
                    })
                    if verbose:
                        print(
                            f"  → {iss.get('char','')} "
                            f"mark{mark_idx} ddx={ddx} ddy={ddy}",
                            file=sys.stderr,
                        )

            _append_log(log_path, {
                "type": "iteration",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "iteration": iteration,
                "quality": quality,
                "issues": issues,
                "adjustments": adjustments,
                "notes": result.get("notes", ""),
            })

            if quality == "good" or not adjustments:
                break

    finally:
        if tmp.exists():
            tmp.unlink()

    return font, final_quality


# ---------------------------------------------------------------------------
# Sample image generation (optional — requires pillow)
# ---------------------------------------------------------------------------

_PANGRAMS = [
    "Đầu cuối trường, mở quặng phễng.",
    "Trăm năm trong cõi người ta, chữ tài chữ phận khéo là ghét nhau",
    "Do bạch kim rất quý nên sẽ dùng để lắp vô xương",
]


def generate_sample_image(
    font_path: Path,
    output_path: Path,
    *,
    grid_font_size: int = 48,
    text_font_size: int = 40,
) -> None:
    COLS = 16
    cell = grid_font_size
    pad = 10
    label_h = 14
    margin = 32
    section_gap = 20

    cw = cell + pad
    ch = cell + pad + label_h
    n_rows = (len(VIETNAMESE_CODEPOINTS) + COLS - 1) // COLS

    try:
        char_font = _PILFont.truetype(str(font_path), grid_font_size)
        para_font = _PILFont.truetype(str(font_path), text_font_size)
    except Exception as exc:
        raise RuntimeError(f"Cannot load font for rendering: {exc}") from exc
    label_font = _PILFont.load_default()

    _scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pangram_widths = [
        _scratch.textbbox((0, 0), p, font=para_font)[2] for p in _PANGRAMS
    ]

    content_w = max(COLS * cw, max(pangram_widths, default=0))
    img_w = content_w + 2 * margin

    section_h = 14 + section_gap // 2
    title_h = text_font_size + section_gap
    grid_h = n_rows * ch + section_gap
    para_line_h = text_font_size + section_gap // 2
    img_h = (margin + title_h + section_h + len(_PANGRAMS) * para_line_h
             + section_gap // 2 + section_h + grid_h + margin)

    img = Image.new("RGB", (img_w, img_h), "white")
    draw = ImageDraw.Draw(img)

    try:
        _tf = TTFont(str(font_path))
        rec = _tf["name"].getName(4, 3, 1, 0x0409) or _tf["name"].getName(1, 3, 1, 0x0409)
        font_title = rec.toUnicode() if rec else font_path.stem
        _tf.close()
    except Exception:
        font_title = font_path.stem

    y = margin
    draw.text((margin, y), font_title, fill="#111", font=para_font)
    y += title_h

    draw.text((margin, y), "Pangrams", fill="#888", font=label_font)
    y += section_h

    for pangram in _PANGRAMS:
        draw.text((margin, y), pangram, fill="#111", font=para_font)
        y += para_line_h

    y += section_gap // 2

    draw.text((margin, y), "Vietnamese Characters", fill="#888", font=label_font)
    y += section_h

    for i, cp in enumerate(VIETNAMESE_CODEPOINTS):
        col, row = i % COLS, i // COLS
        x = margin + col * cw + pad // 2
        gy = y + row * ch + pad // 2
        try:
            draw.text((x, gy), chr(cp), fill="#111", font=char_font)
        except Exception:
            draw.text((x, gy), "?", fill="#c00", font=char_font)
        draw.text((x, gy + cell + 2), f"U+{cp:04X}", fill="#bbb", font=label_font)

    img.save(str(output_path))


def check_coverage(input_path: Path) -> None:
    font = TTFont(str(input_path))
    missing = missing_codepoints(font, VIETNAMESE_CODEPOINTS)
    have = len(VIETNAMESE_CODEPOINTS) - len(missing)
    print(f"Vietnamese coverage: {have}/{len(VIETNAMESE_CODEPOINTS)}")
    if missing:
        print("Missing:")
        for cp in missing:
            try:
                name = unicodedata.name(chr(cp))
            except ValueError:
                name = "<unnamed>"
            print(f"  U+{cp:04X}  {chr(cp)}  {name}")


def main() -> int:
    p = argparse.ArgumentParser(description="Việt hoá an OTF/TTF font.")
    p.add_argument("input", type=Path, help="Input .otf or .ttf file")
    p.add_argument("-o", "--output", type=Path, help="Output font path")
    p.add_argument("--donor", type=Path, help="Optional donor font for missing glyphs")
    p.add_argument("--check", action="store_true",
                   help="Only report Vietnamese coverage; don't write output")
    p.add_argument("--font-name", metavar="NAME",
                   help="Override the font family name in the output (avoids conflict with source)")
    p.add_argument("--validate", action="store_true",
                   help="Use an AI model to validate composed glyphs and adjust positioning")
    p.add_argument("--provider", metavar="NAME", default="claude",
                   choices=["claude", "gemini", "openrouter"],
                   help="AI provider for validation: claude, gemini, openrouter (default: claude)")
    p.add_argument("--api-key", metavar="KEY",
                   help="API key for the chosen provider "
                        "(default: $ANTHROPIC_API_KEY / $GEMINI_API_KEY / $OPENROUTER_API_KEY)")
    p.add_argument("--validate-model", metavar="MODEL", default=None,
                   help="Model to use for validation "
                        "(defaults: claude-opus-4-7 / gemini-2.0-flash / "
                        "google/gemini-2.0-flash-001)")
    p.add_argument("--validate-iterations", metavar="N", type=int, default=3,
                   help="Max validate→adjust iterations (default: 3)")
    p.add_argument("--log", metavar="PATH", type=Path,
                   help="JSONL log path for validation runs (default: <output>_viet_hoa_validate.jsonl)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if not args.input.exists():
        print(f"error: input {args.input} not found", file=sys.stderr)
        return 1

    if args.check:
        check_coverage(args.input)
        return 0

    if not args.output:
        args.output = args.input.with_name(args.input.stem + "-VN" + args.input.suffix)

    if args.donor and not args.donor.exists():
        print(f"error: donor {args.donor} not found", file=sys.stderr)
        return 1

    if args.validate:
        _require_validate_deps(args.provider)

    env_var = _PROVIDER_ENV.get(args.provider, "ANTHROPIC_API_KEY")
    api_key = args.api_key or os.environ.get(env_var)
    if args.validate and not api_key:
        print(
            f"error: --validate --provider {args.provider} requires an API key "
            f"via --api-key or ${env_var}",
            file=sys.stderr,
        )
        return 1

    result = vietnamize(
        args.input, args.output, args.donor, args.font_name,
        validate=args.validate,
        validate_provider=args.provider,
        api_key=api_key,
        validate_model=args.validate_model,
        max_iterations=args.validate_iterations,
        log_path=args.log,
        verbose=args.verbose,
    )

    print(f"Composed: {len(result['composed'])} glyph(s)")
    print(f"Imported from donor: {len(result['imported'])} glyph(s)")
    print(f"Failed: {len(result['failed'])} glyph(s)")
    if result["failed"]:
        print("Failed codepoints:",
              ", ".join(f"U+{cp:04X}" for cp in result["failed"]))
    if result["validation_quality"] is not None:
        print(f"Validation quality: {result['validation_quality']}")
    print(f"Wrote {args.output}")
    if _PIL_OK:
        sample_path = args.output.with_suffix(".png")
        generate_sample_image(args.output, sample_path)
        print(f"Sample image: {sample_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
