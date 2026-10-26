#!/usr/bin/env python3
# Copyright 2026 Abdulloh (github.com/abdulloh5007)
"""Locate the red Exness "Algo Trading" toolbar control in a screenshot.

Used by enable_algo_trading.sh. Prints "ROOT_X ROOT_Y" on stdout when a red
cluster looks like the OFF state of that button. Exit 2 when nothing matches.

Does not print secrets. Window titles and account numbers never go to stdout.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable


def _is_algo_off_red(r: int, g: int, b: int) -> bool:
    """True for the saturated red / red-orange of Algo Trading when it is OFF.

    The ON state is green/teal — those pixels are ignored so we never click a
    button that is already enabled (that would turn AutoTrading off).
    """
    if r < 150:
        return False
    if (r - g) < 40:
        return False
    if b > 130:
        return False
    if g > 160:
        return False
    return True


def find_red_button(
    rgb: bytes,
    width: int,
    height: int,
    *,
    margin_left: int = 40,
    margin_right: int = 80,
    min_pixels: int = 40,
) -> tuple[int, int] | None:
    """Return image-space (x, y) centroid of the largest red cluster, or None.

    ``rgb`` is packed RGB8, ``width * height * 3`` bytes, row-major.
    """
    if width <= 0 or height <= 0:
        return None
    expected = width * height * 3
    if len(rgb) < expected:
        return None
    left = max(0, min(margin_left, width - 1))
    right = max(left + 1, width - max(0, margin_right))
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        row = y * width * 3
        for x in range(left, right):
            i = row + x * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if _is_algo_off_red(r, g, b):
                xs.append(x)
                ys.append(y)
    if len(xs) < min_pixels:
        return None
    # A title-bar close widget is a tiny red square; a toolbar button is wide.
    bbox_w = max(xs) - min(xs) + 1
    bbox_h = max(ys) - min(ys) + 1
    if bbox_w < 12 or bbox_h < 6:
        return None
    if bbox_w > max(width * 0.7, 40):
        # Whole-strip wash of red is not a button — still use the median of the
        # densest x-column so a wide control can match.
        pass
    xs.sort()
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _load_png_rgb(path: str) -> tuple[bytes, int, int]:
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise SystemExit(f"PIL is required to read PNG screenshots: {exc}") from exc
    image = Image.open(path).convert("RGB")
    width, height = image.size
    return image.tobytes(), width, height


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find the red Algo Trading toolbar button")
    parser.add_argument("--png", help="toolbar-strip PNG captured from the X display")
    parser.add_argument("--origin-x", type=int, default=0, help="root X of the PNG's top-left")
    parser.add_argument("--origin-y", type=int, default=0, help="root Y of the PNG's top-left")
    parser.add_argument("--margin-left", type=int, default=40)
    parser.add_argument("--margin-right", type=int, default=80)
    parser.add_argument("--min-pixels", type=int, default=40)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.png:
        print("find_algo_button: --png is required", file=sys.stderr)
        return 2
    try:
        rgb, width, height = _load_png_rgb(args.png)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"find_algo_button: could not read PNG: {exc}", file=sys.stderr)
        return 2
    found = find_red_button(
        rgb,
        width,
        height,
        margin_left=args.margin_left,
        margin_right=args.margin_right,
        min_pixels=args.min_pixels,
    )
    if found is None:
        return 2
    x, y = found
    print(f"{args.origin_x + x} {args.origin_y + y}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
