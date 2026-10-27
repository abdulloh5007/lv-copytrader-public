# Transparent SVG brand

`logo.svg` is the wordmark; `mark.svg` is the LV monogram. Both contain vector paths only,
without a background, raster image, external font or script. The mark was redrawn from the
existing LV artwork; the outlined wordmark uses Manrope ExtraBold (SIL Open Font License).

When inlining SVG, set `--brand-mark` (blue), `--brand-text` (defaults to currentColor), and
optionally `--brand-highlight` (defaults to the text color). External `<img>` images cannot
inherit page CSS variables; inline the SVG to recolor it. In React, `BrandMark` accepts
`markColor`, `textColor`, and `highlightColor`; omitted text color follows the current theme.
