"""Gera assets/icon.ico (um globo branco sobre fundo azul-violeta). Requer Pillow.

Uso: python tools/make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZE = 1024
BACKGROUND = (88, 101, 242, 255)
WHITE = (255, 255, 255, 255)


def draw() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((32, 32, SIZE - 32, SIZE - 32), radius=220, fill=BACKGROUND)
    center, radius, stroke = SIZE // 2, 290, 58
    box = (center - radius, center - radius, center + radius, center + radius)
    pen.ellipse(box, outline=WHITE, width=stroke)                                   # contorno do globo
    pen.ellipse((center - 135, center - radius, center + 135, center + radius), outline=WHITE, width=stroke)  # meridiano
    pen.rectangle((center - radius, center - stroke // 2, center + radius, center + stroke // 2), fill=WHITE)  # equador
    return image


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.join(root, "assets", "icon.ico")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    base = draw().resize((256, 256), Image.LANCZOS)
    base.save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(target)


if __name__ == "__main__":
    main()
