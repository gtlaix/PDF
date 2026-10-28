"""PDF compression to a target size using Ghostscript, with splitting as a fallback.

Strategy
--------
1. If the file is already under the target, it is returned untouched.
2. Otherwise we search the "readable" quality ladder (300 -> 100 DPI) for the
   highest quality that fits the target in a single file.
3. If even the lowest readable quality is too big, we split the document into
   at most ``max_parts`` parts, again choosing the highest quality at which
   every part fits.
4. Only if that still fails do we drop below the readability floor (down to
   50 DPI), still allowing up to ``max_parts`` parts. The result is flagged
   with a warning.
5. If nothing fits, the smallest result we produced is returned with a warning.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter


@dataclass(frozen=True)
class Quality:
    dpi: int
    qfactor: float  # JPEG quantisation: lower = better quality, bigger file
    label: str


# Ordered best -> worst. Everything down to READABLE_FLOOR is considered
# comfortably readable for planning drawings and reports.
READABLE_LADDER = [
    Quality(300, 0.40, "High (300 DPI)"),
    Quality(250, 0.50, "High (250 DPI)"),
    Quality(200, 0.60, "Good (200 DPI)"),
    Quality(150, 0.76, "Good (150 DPI)"),
    Quality(125, 0.90, "Medium (125 DPI)"),
    Quality(100, 1.00, "Medium (100 DPI)"),
]
LAST_RESORT_LADDER = [
    Quality(85, 1.30, "Low (85 DPI)"),
    Quality(72, 1.50, "Low (72 DPI)"),
    Quality(60, 1.80, "Very low (60 DPI)"),
    Quality(50, 2.20, "Very low (50 DPI)"),
]

# Parts are packed to this fraction of the target, leaving headroom for the
# shared resources (fonts etc.) that get duplicated into every part.
PACKING_HEADROOM = 0.92


class GhostscriptMissing(RuntimeError):
    pass


@dataclass
class Result:
    status: str  # "unchanged" | "compressed" | "split" | "over_target"
    original_size: int
    parts: list[Path] = field(default_factory=list)
    part_sizes: list[int] = field(default_factory=list)
    quality: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(self.part_sizes)


def find_ghostscript() -> str:
    for name in ("gs", "gswin64c", "gswin32c"):
        path = shutil.which(name)
        if path:
            return path
    raise GhostscriptMissing(
        "Ghostscript is not installed or not on PATH. See the README for install steps."
    )


def ghostscript_compress(src: Path, dst: Path, q: Quality) -> int:
    """Re-write ``src`` to ``dst`` at quality ``q``. Returns the output size in bytes."""
    gs = find_ghostscript()
    mono_dpi = max(q.dpi * 2, 300)  # 1-bit scans compress well; keep them crisp
    image_dict = (
        f"<< /QFactor {q.qfactor} /Blend 1 /HSamples [2 1 1 2] /VSamples [2 1 1 2] >>"
    )
    cmd = [
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dPassThroughJPEGImages=false",
        "-dPassThroughJPXImages=false",
        "-dDownsampleColorImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={q.dpi}",
        "-dColorImageDownsampleThreshold=1.0",
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dDownsampleGrayImages=true",
        "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={q.dpi}",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dAutoFilterGrayImages=false",
        "-dGrayImageFilter=/DCTEncode",
        "-dDownsampleMonoImages=true",
        "-dMonoImageDownsampleType=/Subsample",
        f"-dMonoImageResolution={mono_dpi}",
        "-dMonoImageDownsampleThreshold=1.0",
        f"-sOutputFile={dst}",
        "-c",
        f"<< /ColorACSImageDict {image_dict} /GrayACSImageDict {image_dict} "
        f"/ColorImageDict {image_dict} /GrayImageDict {image_dict} >> setdistillerparams",
        "-f",
        str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(
            f"Ghostscript failed: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return dst.stat().st_size


def _page_sizes(reader: PdfReader) -> list[int]:
    """Approximate the byte cost of each page by writing it on its own."""
    sizes = []
    for page in reader.pages:
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        sizes.append(buf.tell())
    return sizes


def _partition(sizes: list[int], capacity: float, max_parts: int) -> list[range] | None:
    """Greedily pack consecutive pages into at most ``max_parts`` chunks."""
    chunks: list[range] = []
    start, used = 0, 0
    for i, s in enumerate(sizes):
        if s > capacity:
            return None  # a single page can't fit
        if used + s > capacity and i > start:
            chunks.append(range(start, i))
            start, used = i, 0
        used += s
    chunks.append(range(start, len(sizes)))
    return chunks if len(chunks) <= max_parts else None


def _write_parts(src: Path, chunks: list[range], out_dir: Path, stem: str) -> list[Path]:
    reader = PdfReader(str(src))
    paths = []
    n = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        w = PdfWriter()
        for p in chunk:
            w.add_page(reader.pages[p])
        w.compress_identical_objects()
        path = out_dir / f"{stem} - Part {idx} of {n}.pdf"
        with open(path, "wb") as fh:
            w.write(fh)
        paths.append(path)
    return paths


def _try_split(
    compressed: Path, target: int, max_parts: int, out_dir: Path, stem: str
) -> list[Path] | None:
    size = compressed.stat().st_size
    if math.ceil(size / (target * PACKING_HEADROOM)) > max_parts:
        return None  # clearly can't fit; skip the expensive per-page pass
    reader = PdfReader(str(compressed))
    if len(reader.pages) < 2:
        return None
    sizes = _page_sizes(reader)
    # Per-page sizes include duplicated fonts, so scale them to the real total.
    scale = size / max(sum(sizes), 1)
    scaled = [s * scale for s in sizes]
    for headroom in (PACKING_HEADROOM, 0.80, 0.65):
        chunks = _partition(scaled, target * headroom, max_parts)
        if chunks is None:
            continue
        if len(chunks) == 1:
            chunks = _partition(scaled, size / 2 + 1, max_parts) or chunks
        parts = _write_parts(compressed, chunks, out_dir, stem)
        if all(p.stat().st_size <= target for p in parts):
            return parts
        for p in parts:
            p.unlink(missing_ok=True)
    return None


def compress_pdf(
    src: Path,
    out_dir: Path,
    target_bytes: int,
    max_parts: int = 3,
    output_stem: str | None = None,
) -> Result:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem or src.stem
    original_size = src.stat().st_size
    result = Result(status="compressed", original_size=original_size)

    if original_size <= target_bytes:
        dst = out_dir / f"{stem}.pdf"
        shutil.copyfile(src, dst)
        result.status = "unchanged"
        result.parts = [dst]
        result.part_sizes = [original_size]
        result.quality = "Original (already under target)"
        return result

    work = out_dir / ".work"
    work.mkdir(exist_ok=True)
    cache: dict[int, Path] = {}

    def run(q: Quality) -> Path:
        if q.dpi not in cache:
            dst = work / f"{q.dpi}.pdf"
            ghostscript_compress(src, dst, q)
            cache[q.dpi] = dst
        return cache[q.dpi]

    def size_at(q: Quality) -> int:
        return run(q).stat().st_size

    def finish_single(q: Quality, path: Path) -> Result:
        dst = out_dir / f"{stem}.pdf"
        shutil.move(path, dst)
        result.parts = [dst]
        result.part_sizes = [dst.stat().st_size]
        result.quality = q.label
        return result

    try:
        # Step 2: highest readable quality that fits as one file. Size falls as
        # DPI falls, so binary search over the ladder.
        ladder = READABLE_LADDER
        if size_at(ladder[-1]) <= target_bytes:
            lo, hi = 0, len(ladder) - 1  # hi is known to fit
            while lo < hi:
                mid = (lo + hi) // 2
                if size_at(ladder[mid]) <= target_bytes:
                    hi = mid
                else:
                    lo = mid + 1
            return finish_single(ladder[hi], cache[ladder[hi].dpi])

        # Step 3 & 4: split, best quality first, readable before last resort.
        for q in READABLE_LADDER + LAST_RESORT_LADDER:
            below_floor = q in LAST_RESORT_LADDER
            path = run(q)
            if below_floor and path.stat().st_size <= target_bytes:
                result.warnings.append(
                    "Compressed below 100 DPI to fit; check fine drawing detail is still legible."
                )
                return finish_single(q, path)
            parts = _try_split(path, target_bytes, max_parts, out_dir, stem)
            if parts:
                result.status = "split"
                result.parts = parts
                result.part_sizes = [p.stat().st_size for p in parts]
                result.quality = q.label
                if below_floor:
                    result.warnings.append(
                        f"Needed {len(parts)} parts and below-100 DPI quality to fit; "
                        "check fine drawing detail is still legible."
                    )
                return result

        # Step 5: nothing fits. Return the smallest single file we made.
        best_dpi = min(cache, key=lambda d: cache[d].stat().st_size)
        best_q = next(q for q in READABLE_LADDER + LAST_RESORT_LADDER if q.dpi == best_dpi)
        result.status = "over_target"
        if cache[best_dpi].stat().st_size >= original_size:
            dst = out_dir / f"{stem}.pdf"
            shutil.copyfile(src, dst)
            result.parts, result.part_sizes = [dst], [original_size]
            result.quality = "Original (could not be reduced)"
        else:
            finish_single(best_q, cache[best_dpi])
        result.warnings.append(
            f"Could not get under the target, even split into {max_parts} parts. "
            "This file is likely large vector drawings or many pages; "
            "consider splitting it by hand or raising the target size."
        )
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
