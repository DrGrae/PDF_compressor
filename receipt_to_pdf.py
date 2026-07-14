#!/usr/bin/env python3
"""
receipt_to_pdf.py  (path-resolution update)

What changed (to avoid "Path not found: scans"):
- New --base-dir option: resolve inputs relative to this folder if not found.
- Fallback: also try resolving inputs relative to the script's directory.
- Helpful diagnostics: --dry-run (list what would be processed) and --verbose.

You can now do:
    python receipt_to_pdf.py scans --out-dir out
    # If you're running the script from elsewhere:
    python receipt_to_pdf.py scans --base-dir "/path/to/expense test" --out-dir out

    # See what would be picked up without writing PDFs:
    python receipt_to_pdf.py scans --dry-run --verbose

Other features remain the same: directories/files/quoted globs accepted, recursive discovery,
HEIC support (pillow-heif), PDF compression under a target (default 1 MB).
"""

import argparse
import io
import os
import sys
from typing import List, Tuple, Iterable

from PIL import Image, ImageOps, ImageColor

# Optional HEIC/HEIF support
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()  # registers HEIF/HEIC with Pillow
except Exception:
    pillow_heif = None  # if missing, non-HEIC formats still work

# ReportLab for assembling PDFs
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


SUPPORTED_EXTS_DEFAULT = [
    "jpg", "jpeg", "png", "heic", "heif", "webp",
    "tif", "tiff", "bmp", "gif", "pbm", "pgm", "ppm"
]


def parse_bg_color(color_str: str) -> Tuple[int, int, int]:
    """Parse a CSS-like color string into an RGB tuple."""
    try:
        rgb = ImageColor.getrgb(color_str)
        if len(rgb) == 4:  # ignore alpha if provided
            rgb = rgb[:3]
        return tuple(int(c) for c in rgb)  # type: ignore
    except Exception:
        raise argparse.ArgumentTypeError(f"Invalid color value: '{color_str}'")


def open_image_normalized(path: str, bg_rgb=(255, 255, 255), grayscale=False) -> Image.Image:
    """
    Open an image from disk, apply EXIF orientation, flatten alpha onto bg,
    convert to RGB (or L if grayscale).
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)

    # Handle transparency by compositing on background
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        base = Image.new("RGBA", img.size, bg_rgb + (255,))
        base.paste(img, (0, 0), img.convert("RGBA"))
        img = base.convert("RGB")
    else:
        img = img.convert("L" if grayscale else "RGB")

    return img


def resize_to_long_edge(img: Image.Image, max_long_edge: int) -> Image.Image:
    """Resize image so that its longest side equals max_long_edge, preserving aspect ratio."""
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return img
    scale = max_long_edge / float(long_edge)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return img.resize(new_size, Image.LANCZOS)


def to_jpeg_bytes(
    img: Image.Image,
    quality: int,
    dpi: int,
    optimize: bool = True,
    progressive: bool = True,
    subsampling: int = 2,
) -> bytes:
    """Encode PIL image to JPEG bytes with the given params."""
    buf = io.BytesIO()
    save_kwargs = {
        "format": "JPEG",
        "quality": quality,
        "dpi": (dpi, dpi),
        "optimize": optimize,
        "progressive": progressive,
        "subsampling": subsampling,  # 4:2:0
    }
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def images_to_pdf_bytes(
    jpeg_blobs: List[Tuple[bytes, Tuple[int, int]]],
    pdf_dpi: int,
) -> bytes:
    """
    Assemble one or more JPEGs into a PDF. Each page size matches its image
    dimensions at the given PDF DPI.
    """
    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf)  # default page size will be overridden per page

    for i, (jpeg_data, (w_px, h_px)) in enumerate(jpeg_blobs):
        w_pt = w_px * 72.0 / pdf_dpi
        h_pt = h_px * 72.0 / pdf_dpi
        c.setPageSize((w_pt, h_pt))
        reader = ImageReader(io.BytesIO(jpeg_data))
        c.drawImage(reader, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=False, mask='auto')
        c.showPage()

    c.save()
    return pdf_buf.getvalue()


def try_compress(
    pil_images: List[Image.Image],
    max_size_bytes: int,
    initial_quality: int,
    min_quality: int,
    initial_long_edge: int,
    min_long_edge: int,
    quality_step: int,
    pdf_dpi: int,
) -> Tuple[bytes, int, int, int]:
    """
    Iteratively attempt compression by reducing JPEG quality and then downscaling.
    Returns (pdf_bytes, final_quality, final_long_edge, iterations).
    Raises RuntimeError if constraints cannot be met.
    """
    quality = initial_quality
    max_edge = initial_long_edge
    iterations = 0

    while True:
        iterations += 1
        # Prepare JPEGs for current settings
        jpeg_blobs = []
        for img in pil_images:
            resized = resize_to_long_edge(img, max_edge)
            jpeg_bytes = to_jpeg_bytes(resized, quality=quality, dpi=pdf_dpi)
            jpeg_blobs.append((jpeg_bytes, resized.size))

        pdf_bytes = images_to_pdf_bytes(jpeg_blobs, pdf_dpi=pdf_dpi)
        size = len(pdf_bytes)

        if size <= max_size_bytes:
            return pdf_bytes, quality, max_edge, iterations

        # Reduce quality first, then scale
        if quality - quality_step >= min_quality:
            quality -= quality_step
        else:
            # Quality floor reached; reduce size of images
            if int(max_edge * 0.9) >= min_long_edge:
                max_edge = int(max_edge * 0.9)  # scale down by 10%
                quality = min(initial_quality, max(quality, min_quality))
            else:
                # Last resort: try grayscale conversion if not already grayscale
                if any(img.mode != "L" for img in pil_images):
                    pil_images = [img.convert("L") for img in pil_images]
                    quality = min(initial_quality, max(quality, min_quality))
                else:
                    raise RuntimeError(
                        "Could not compress below the desired size with given limits. "
                        "Try increasing --max-size, lowering --min-quality, or lowering --min-long-edge."
                    )


def resolve_candidate(path: str, base_dir: str, script_dir: str) -> str:
    """
    Try multiple locations for a path:
    - As given (expanded ~)
    - Joined with base_dir
    - Joined with script_dir
    Returns the first existing path, else the original string.
    """
    p = os.path.expanduser(path)

    # 1) As given
    if os.path.exists(p):
        return p

    # 2) Relative to base dir
    if base_dir:
        cand = os.path.join(base_dir, p)
        if os.path.exists(cand):
            return cand

    # 3) Relative to script dir
    cand = os.path.join(script_dir, p)
    if os.path.exists(cand):
        return cand

    return p  # not found; caller may handle globs or warn


def collect_inputs(
    paths: Iterable[str],
    recursive: bool,
    exts: Iterable[str],
    base_dir: str,
    verbose: bool = False,
) -> List[str]:
    """
    Collect input image files from files, directories, and quoted glob patterns.
    Avoids shell globbing errors by allowing raw directory paths and base-dir resolution.
    """
    import glob

    files: List[str] = []
    exts_low = {e.lower().lstrip(".") for e in exts}

    script_dir = os.path.dirname(os.path.abspath(__file__))

    for raw in paths:
        if not raw:
            continue

        # Resolve path through multiple bases
        p = resolve_candidate(raw, base_dir=base_dir, script_dir=script_dir)

        if verbose and raw != p:
            print(f"[VERBOSE] Resolved '{raw}' -> '{p}'")

        if os.path.isdir(p):
            if recursive:
                for root, _dirs, names in os.walk(p):
                    for name in names:
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        if ext in exts_low:
                            files.append(os.path.join(root, name))
            else:
                for name in os.listdir(p):
                    fpath = os.path.join(p, name)
                    if os.path.isfile(fpath):
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        if ext in exts_low:
                            files.append(fpath)

        elif os.path.isfile(p):
            files.append(p)

        else:
            # If it's a QUOTED glob, expand here.
            if any(ch in p for ch in "*?[]"):
                matches = [m for m in glob.glob(p) if os.path.isfile(m)]
                files.extend(matches)
                if not matches:
                    print(f"[WARN] Pattern matched no files (did you mean to quote it in zsh?): {raw}", file=sys.stderr)
            else:
                print(f"[WARN] Path not found after resolution: {raw}", file=sys.stderr)
                if verbose:
                    print(f"[VERBOSE] Tried: '{raw}', '{os.path.expanduser(raw)}', "
                          f"'{os.path.join(base_dir, raw) if base_dir else '(no base)'}', "
                          f"'{os.path.join(script_dir, raw)}'", file=sys.stderr)

    # Deduplicate and sort for stability
    files = sorted(dict.fromkeys(files))
    return files


def process(
    inputs: List[str],
    output: str,
    out_dir: str,
    combine: bool,
    max_size: int,
    initial_quality: int,
    min_quality: int,
    initial_long_edge: int,
    min_long_edge: int,
    quality_step: int,
    bg_rgb: Tuple[int, int, int],
    grayscale: bool,
    pdf_dpi: int,
    recursive: bool,
    exts: List[str],
    base_dir: str,
    dry_run: bool,
    verbose: bool,
) -> None:
    # Resolve output directory
    out_dir = out_dir or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    pil_paths = collect_inputs(inputs, recursive=recursive, exts=exts, base_dir=base_dir, verbose=verbose)

    if not pil_paths:
        print("[ERROR] No images found.", file=sys.stderr)
        print(f"  CWD: {os.getcwd()}", file=sys.stderr)
        if base_dir:
            print(f"  BASE: {base_dir}", file=sys.stderr)
        print("Tips:", file=sys.stderr)
        print("  - Pass a directory (e.g., scans) OR a full path", file=sys.stderr)
        print("  - Use --base-dir if you're running the script from a different location", file=sys.stderr)
        print("  - Or quote your glob in zsh: 'scans/*.jpg'", file=sys.stderr)
        print("  - Or adjust --ext to include your file types", file=sys.stderr)
        sys.exit(2)

    if dry_run or verbose:
        print(f"[INFO] Found {len(pil_paths)} image(s).")
        for p in pil_paths:
            print(" -", p)
        if dry_run:
            print("[INFO] Dry-run enabled; not writing PDFs.")
            return

    if combine:
        if not output:
            output = "receipts_combined.pdf"
        out_path = os.path.join(out_dir, output)

        print(f"[INFO] Combining {len(pil_paths)} images into: {out_path}")
        pil_images = [open_image_normalized(p, bg_rgb=bg_rgb, grayscale=grayscale) for p in pil_paths]

        try:
            pdf_bytes, final_q, final_edge, iters = try_compress(
                pil_images,
                max_size_bytes=max_size,
                initial_quality=initial_quality,
                min_quality=min_quality,
                initial_long_edge=initial_long_edge,
                min_long_edge=min_long_edge,
                quality_step=quality_step,
                pdf_dpi=pdf_dpi,
            )
        except RuntimeError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(2)

        with open(out_path, "wb") as f:
            f.write(pdf_bytes)

        size_kb = os.path.getsize(out_path) / 1024.0
        print(f"[OK] Wrote {out_path} ({size_kb:.1f} KB) "
              f"after {iters} iteration(s), quality={final_q}, long_edge={final_edge}px, pdf_dpi={pdf_dpi}")

    else:
        # One output PDF per input image
        for idx, p in enumerate(pil_paths, start=1):
            base = os.path.splitext(os.path.basename(p))[0]
            out_name = output if output else f"{base}.pdf"
            out_path = os.path.join(out_dir, out_name)

            print(f"[INFO] ({idx}/{len(pil_paths)}) Converting '{p}' -> '{out_path}'")
            pil_img = open_image_normalized(p, bg_rgb=bg_rgb, grayscale=grayscale)

            try:
                pdf_bytes, final_q, final_edge, iters = try_compress(
                    [pil_img],
                    max_size_bytes=max_size,
                    initial_quality=initial_quality,
                    min_quality=min_quality,
                    initial_long_edge=initial_long_edge,
                    min_long_edge=min_long_edge,
                    quality_step=quality_step,
                    pdf_dpi=pdf_dpi,
                )
            except RuntimeError as e:
                print(f"[ERROR] {e}", file=sys.stderr)
                sys.exit(2)

            with open(out_path, "wb") as f:
                f.write(pdf_bytes)

            size_kb = os.path.getsize(out_path) / 1024.0
            print(f"[OK] Wrote {out_path} ({size_kb:.1f} KB) "
                  f"after {iters} iteration(s), quality={final_q}, long_edge={final_edge}px, pdf_dpi={pdf_dpi}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert images (files/dirs/globs) to compressed PDF(s) under a target size (default 1 MB)."
    )
    parser.add_argument("inputs", nargs="+", help="Input image file(s), directory/ies, or QUOTED glob(s)")
    parser.add_argument("--output", "-o", default="", help="Output PDF filename (used when --combine or for single image)")
    parser.add_argument("--out-dir", default="", help="Directory to write output PDF(s)")
    parser.add_argument("--combine", action="store_true", help="Combine all inputs into one multi-page PDF")

    parser.add_argument("--max-size", type=int, default=1_000_000, help="Max PDF size in bytes (default: 1,000,000)")
    parser.add_argument("--initial-quality", type=int, default=85, help="Starting JPEG quality (default: 85)")
    parser.add_argument("--min-quality", type=int, default=35, help="Lowest JPEG quality allowed (default: 35)")
    parser.add_argument("--quality-step", type=int, default=5, help="Quality decrement per iteration (default: 5)")
    parser.add_argument("--initial-long-edge", type=int, default=2000, help="Start scaling so longest edge ≤ this (px) (default: 2000)")
    parser.add_argument("--min-long-edge", type=int, default=800, help="Do not scale longest edge below this (px) (default: 800)")
    parser.add_argument("--bg", type=parse_bg_color, default="#FFFFFF", help="Background for transparent images (default: white)")
    parser.add_argument("--grayscale", action="store_true", help="Force grayscale (smaller files; good for text receipts)")
    parser.add_argument("--pdf-dpi", type=int, default=150, help="DPI used to map image pixels to PDF points (default: 150)")

    # Discovery options
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not search subfolders of directories")
    parser.set_defaults(recursive=True)
    parser.add_argument(
        "--ext",
        default=",".join(SUPPORTED_EXTS_DEFAULT),
        help="Comma-separated list of file extensions to include (default covers common image types)"
    )

    # New resolution / diagnostics
    parser.add_argument("--base-dir", default="", help="Resolve relative inputs against this directory if not found")
    parser.add_argument("--dry-run", action="store_true", help="List found images and exit without writing PDFs")
    parser.add_argument("--verbose", action="store_true", help="Print extra diagnostics")

    args = parser.parse_args()

    # Validate ranges
    if not (10 <= args.min_quality <= args.initial_quality <= 100):
        parser.error("--min-quality must be ≤ --initial-quality and in [10,100]")

    if args.min_long_edge < 200 or args.initial_long_edge < args.min_long_edge:
        parser.error("--min-long-edge must be at least 200 and ≤ --initial-long-edge")

    # Normalize extensions list
    ext_list = [e.strip().lstrip(".").lower() for e in args.ext.split(",") if e.strip()]
    if not ext_list:
        parser.error("--ext produced an empty list.")

    # Process
    process(
        inputs=args.inputs,
        output=args.output,
        out_dir=args.out_dir,
        combine=args.combine,
        max_size=args.max_size,
        initial_quality=args.initial_quality,
        min_quality=args.min_quality,
        initial_long_edge=args.initial_long_edge,
        min_long_edge=args.min_long_edge,
        quality_step=args.quality_step,
        bg_rgb=args.bg,
        grayscale=args.grayscale,
        pdf_dpi=args.pdf_dpi,
        recursive=args.recursive,
        exts=ext_list,
        base_dir=os.path.expanduser(args.base_dir) if args.base_dir else "",
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
