"""Pixel helpers on rendered pages."""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw


def px(img: Image.Image, rel: list[float], grow: int = 0) -> tuple[int, int, int, int]:
    w, h = img.size
    return (
        round(rel[0] * w) - grow,
        round(rel[1] * h) - grow,
        round(rel[2] * w) + grow,
        round(rel[3] * h) + grow,
    )


def extrema(img: Image.Image) -> tuple[int, int]:
    """(darkest, brightest) of a grayscale image."""
    lo, hi = img.convert("L").getextrema()
    return int(lo), int(hi)  # type: ignore[arg-type]


def ink(img: Image.Image, rel: list[float]) -> bool:
    """Is there dark content inside the box?"""
    lo, _ = extrema(img.crop(px(img, rel)))
    return bool(lo < 120)


def all_black(img: Image.Image, rel: list[float], margin: int = 1) -> bool:
    box = px(img, rel, -margin)
    _, hi = extrema(img.crop(box))
    return bool(hi < 40)


def changed_outside(before: Image.Image, after: Image.Image, rels: list[list[float]], grow: int = 3) -> bool:
    """Do the two renders differ anywhere except inside (slightly grown) boxes?"""
    a, b = before.convert("L").copy(), after.convert("L").copy()
    for rel in rels:
        for img in (a, b):
            ImageDraw.Draw(img).rectangle(px(img, rel, grow), fill=128)
    return ImageChops.difference(a, b).getbbox() is not None
