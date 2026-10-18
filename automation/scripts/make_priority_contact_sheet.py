from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((width, height), Image.Resampling.LANCZOS)
    return copy


def build_contact_sheet(
    plots_root: Path, output: Path, countries: list[str], cutoff: str,
) -> None:
    canvas_width = 1600
    margin = 40
    gutter = 32
    header_height = 100
    row_height = 430
    label_height = 38
    cell_width = (canvas_width - margin * 2 - gutter) // 2
    image_height = row_height - label_height - 22
    canvas = Image.new("RGB", (canvas_width, header_height + row_height * len(countries)), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (margin, 22), "Priority country production diagnostics",
        fill="#111827", font=_font(30, bold=True),
    )
    draw.text(
        (margin, 61), f"Monthly volume and recent daily coverage through {cutoff}",
        fill="#4b5563", font=_font(18),
    )

    for index, country in enumerate(countries):
        top = header_height + index * row_height
        if index:
            draw.line((margin, top, canvas_width - margin, top), fill="#e5e7eb", width=2)
        for column, (suffix, label) in enumerate((
            ("monthly_volume_2026.png", "monthly volume"),
            ("recent_daily_2026.png", "recent daily coverage"),
        )):
            left = margin + column * (cell_width + gutter)
            draw.text(
                (left, top + 14), f"{country} — {label}",
                fill="#111827", font=_font(18, bold=True),
            )
            path = plots_root / country / f"{country}_{suffix}"
            with Image.open(path) as source:
                image = _fit(source.convert("RGB"), cell_width, image_height)
            x = left + (cell_width - image.width) // 2
            y = top + label_height + (image_height - image.height) // 2
            canvas.paste(image, (x, y))

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plots-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--countries", nargs="+", required=True)
    args = parser.parse_args()
    build_contact_sheet(
        Path(args.plots_root), Path(args.output), args.countries, args.cutoff,
    )


if __name__ == "__main__":
    main()
