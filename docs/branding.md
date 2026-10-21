# DrainTracer brand guide

[README](../README.md) · [Contributing](../CONTRIBUTING.md) · [License](../LICENSE)

**DrainTracer**

**See what keeps growing.**

The identity pairs a continuous pipe with an escaping droplet inside a tracing
reticle. The pipe represents the monitored process tree; the droplet represents
resource growth; the reticle represents investigation. The symbol describes
observing a leak signal, without suggesting that DrainTracer can locate an
individual allocation.

## The asset kit

![DrainTracer wordmark on ink](assets/brand/wordmark-dark.png)

The editable SVGs are the source for the compact identity. Matching PNGs are
included for places that do not accept SVG. Each SVG has an accessible title and
description and contains no external images, scripts, or font requests.

- **Transparent mark:** [SVG](assets/brand/mark.svg) · [PNG](assets/brand/mark.png).
  Use on dark surfaces.
- **Dark avatar:** [SVG](assets/brand/mark-dark.svg) · [PNG](assets/brand/mark-dark.png).
- **Light avatar:** [SVG](assets/brand/mark-light.svg) · [PNG](assets/brand/mark-light.png).
- **Dark wordmark:** [SVG](assets/brand/wordmark-dark.svg) · [PNG](assets/brand/wordmark-dark.png).
- **Light wordmark:** [SVG](assets/brand/wordmark-light.svg) · [PNG](assets/brand/wordmark-light.png).
- **Social preview, 1280 × 640:** [SVG](assets/brand/social-preview.svg) ·
  [PNG](assets/brand/social-preview.png). The PNG is ready for a repository's
  social-preview image setting.
- **Illustrated README masthead, 2172 × 724:** [PNG](assets/hero.png).

The masthead gives the pipe a luminous, technical illustration treatment. The
compact SVG mark simplifies the same motif for avatars and smaller placements.
Use the compact mark wherever a detailed illustration would lose definition.

## Color

- **Ink — `#101820`:** primary dark background.
- **Trace teal — `#73DACA`:** pipe, tracing arcs, and the Tracer accent on ink.
- **Signal amber — `#E0AF68`:** escaping droplet and targeting ticks.
- **Warm white — `#EAF2F4`:** primary type on ink.
- **Paper — `#F5F8F8`:** light background.
- **Deep teal — `#196F69`:** pipe and wordmark accent on paper.
- **Deep amber — `#936011`:** droplet and ticks on paper.

The light assets deliberately deepen teal and amber for readable contrast.
Use both shape and color to convey meaning: the droplet and reticle should stay
recognizable in a single-color reproduction.

## Type and composition

Write the name as **DrainTracer**, with capital D and T and no space.
The SVG wordmarks use `Segoe UI, Arial, sans-serif`; no fonts are bundled.
They retain editable text, so a different system font can slightly change the
letterforms. Use the supplied PNG when exact rendering matters.

Keep the mark's proportions and the relationship between pipe, droplet, and
reticle. Leave at least 24 units of clear space around the 256-unit mark when
placing it beside other content. Use the transparent mark at 32 pixels or
larger and an avatar tile at 48 pixels or larger. At smaller sizes, use the
product name as text.

Keep the illustrated masthead intact, including its outer margins. Use the
separate wordmark or social card when a different aspect ratio is needed.
Avoid stretching, clipping the droplet, adding shadows to the compact mark,
or placing pale teal on a light background.

## Writing and product claims

Lead with what a user can observe: process trees, resource trends, sustained
growth, and potential leak signals. Use plain technical language. A signal
needs investigation; neither a clean session nor a reticle proves that an
application is leak free. Keep platform requirements explicit: Windows 10/11
and Python 3.10+.

## Sources, exports, and license

The illustrated masthead was created with the built-in image generation tool;
the exact generation brief is saved in [hero-prompt.txt](assets/brand/hero-prompt.txt).
The compact marks, wordmarks, and social card are editable SVG artwork. Their
PNG exports were rendered from those SVGs in Chromium with transparent
backgrounds preserved.

To refresh a PNG, render the corresponding SVG at its declared dimensions in
a browser or vector editor, inspect the result, and keep both formats together.
Check the light and dark variants, lettering, reticle gaps, and small-size
legibility before committing. Follow the repository's versioning rule for
asset changes.

The documentation and artwork are distributed under the repository's existing
[MIT license](../LICENSE). Include the applicable copyright and license notice
when redistributing them.

