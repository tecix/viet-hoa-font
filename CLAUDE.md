# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Basic usage
python viet_hoa.py INPUT_FONT [-o OUTPUT] [--donor DONOR_FONT] [--check] [-v]

# Check Vietnamese coverage only (no output written)
python viet_hoa.py font.ttf --check

# Add Vietnamese support with a donor font for missing diacritics
python viet_hoa.py font.ttf -o font-VN.ttf --donor NotoSans.ttf

# Run AI validation loop (requires API key in env or .env file)
python viet_hoa.py font.ttf -o font-VN.ttf --ai claude
```

Environment variables for AI features: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`.

## Architecture

Everything lives in a single file: `viet_hoa.py` (~1,230 lines). The code is organized into logical sections top-to-bottom:

**Vietnamese character set** — `VIETNAMESE_CODEPOINTS` (line ~54): the 134 Unicode codepoints that define complete Vietnamese coverage.

**Font metrics and positioning helpers**: functions for reading GPOS anchor data (`collect_anchors()`), computing font metrics (`font_x_height()`, `font_cap_height()`), `topmost_right_x()` for finding the actual right stem (used to anchor horns on calligraphic / swash fonts where bbox right runs past the letter body), and the heuristic diacritic placement algorithm (`heuristic_offset()`). Notable behaviors of `heuristic_offset()`: tone marks on a precomposed circumflex base (Ấ/Ế/Ố) shift to the upper-right of the circumflex peak instead of centering; horns on Ơ/Ư share a single target y so they line up; horn x is anchored at `topmost_right_x` of the base.

**Core composition logic** (`compose_components()`, lines ~400–527): takes a precomposed Vietnamese character, decomposes it via NFD, finds or creates the base glyph and combining marks, then builds a composite glyph using either TrueType (`add_composite_glyph_truetype()`) or CFF (`add_composite_glyph_cff()`) depending on the font's outline format.

**Donor font import** (`import_glyph_from_donor()`, line ~528): copies a missing diacritic glyph from a secondary "donor" font when the target font lacks it entirely.

**Đ/đ synthesis** (`synthesize_dstroke()`): builds Đ (U+0110) and đ (U+0111) by overlaying a horizontal crossbar contour onto the font's existing D/d outline. Used as the default path when no donor is provided, since Đ/đ are not Unicode-decomposable. Stroke thickness is inferred from the font's hyphen-minus glyph for style consistency.

**Combining-mark derivation** (`derive_mark_from_existing()`): before falling back to geometric synthesis, this tries to reuse an existing spacing-equivalent glyph from the source font itself — e.g. spacing acute U+00B4 → combining acute U+0301, right single quote U+2019 → horn U+031B, period U+002E → dot below U+0323. The candidate map (`_MARK_SOURCE_CODEPOINTS`) lists best-fit-first sources per mark. The shape is the designer's own work, so style matches naturally. Below-marks get a vertical shift to push the source bbox below the baseline; for above-marks the composition step's heuristic offset handles re-positioning per base, so source coordinates can be left as-is.

**Combining-mark synthesis** (`synthesize_mark()`): last-resort fallback when no donor is available AND the font has no spacing equivalent for the mark. Draws the mark from polygon primitives scaled to the font's x-height and stroke thickness. Quality is "functional, not pretty" — geometric shapes won't match calligraphic / display fonts aesthetically. `_draw_mark_outline()` holds the per-codepoint shape definitions for U+0300/0301/0302/0303/0304/0306/0308/0309/031B/0323. In practice, only U+0309 (hook above) routinely lands in this tier since most fonts ship with the spacing equivalents the derive tier needs.

**Ơ/ơ/Ư/ư synthesis** (`synthesize_horned_base()`): triggered only when the horn mark itself had to be drawn from polygon primitives (i.e. neither the font, donor, nor any spacing-equivalent like U+2019 provided a horn). In that case heuristic placement of a free-floating geometric horn looks bad, so we draw a comma-flick horn directly onto the font's existing O/o/U/u outline instead. When the horn is real (donor / derived from apostrophe), `compose_components()` handles Ơ/ư via heuristic_offset using the natural horn shape.

**Font rename** (`rename_font()`): updates OS/2 and name table entries to distinguish the output font.

**Diacritic-stack-root recomposition** (`recompose_stack_roots()`): when a combining mark was newly added (not native to the source font), the font's native precomposed Â/Ê/Ô/Ă (and lowercase) may have been drawn with a different mark glyph than the one we just installed — so the bare letter looks inconsistent with the Vietnamese stacks built on top (e.g. Ô vs Ố = Ô + acute). This re-composes the listed targets in `_DIACRITIC_STACK_ROOTS` using the same mark glyph used everywhere downstream.

**Main orchestration** (`vietnamize()`): pre-pass acquires each missing combining mark in priority order — donor → derive from existing spacing glyph → synthesise from primitives. Then synthesises Đ/đ via `synthesize_dstroke()`, and (only when horn was geometric) Ơ/ơ/Ư/ư via `synthesize_horned_base()`. Then `recompose_stack_roots()` overwrites native bases whose mark we replaced. Finally calls `compose_components()` for each remaining precomposed codepoint, iterating until no progress is made.

**AI validation loop** (`run_validation_loop()`, lines ~780–1132): renders glyphs to PNG via Pillow, sends them to an AI model (Claude, Gemini, or OpenRouter), parses structured JSON feedback, and applies fine-grained positioning adjustments (`_apply_adjustment()`). Supports `--ai claude|gemini|openrouter`.

**CLI entry point** (`main()`, line ~1150): argparse setup.

## Key design notes

- The tool operates on `fontTools` TTFont objects in-memory and saves once at the end; no intermediate files.
- Composite glyphs reference existing component glyphs by name — the tool never synthesizes new outlines from scratch.
- GPOS anchor data (when present) is preferred over heuristic positioning; `calibrate_mark_gap()` auto-tunes the fallback heuristic against any anchors that do exist.
- CFF fonts (`.otf`) require different composite handling than TrueType (`.ttf`) because CFF doesn't support composite glyphs natively — the tool inlines component contours instead.
- The AI loop is purely optional; the core tool works without any API keys.
