"""Erzeugt das Tray-Symbol für NightmareCatcher (lila Kreis mit weißem N)."""
from __future__ import annotations


def make_icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(83, 74, 183, 255))
    d.line([(20, 46), (20, 18)], fill=(255, 255, 255, 255), width=7)
    d.line([(20, 18), (44, 46)], fill=(255, 255, 255, 255), width=7)
    d.line([(44, 46), (44, 18)], fill=(255, 255, 255, 255), width=7)
    return img
