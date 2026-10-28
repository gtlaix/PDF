import io
import random
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFilter
from pypdf import PdfReader
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.compressor import compress_pdf

pytestmark = pytest.mark.skipif(shutil.which("gs") is None, reason="Ghostscript not installed")

MB = 1_000_000


def _photo(seed: int, w: int = 3000, h: int = 2000) -> bytes:
    """A noisy, photo-like image that doesn't compress trivially."""
    rnd = random.Random(seed)
    img = Image.effect_noise((w // 4, h // 4), 60).convert("RGB").resize((w, h))
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rnd.randrange(w), rnd.randrange(h)
        draw.ellipse((x, y, x + rnd.randrange(50, 600), y + rnd.randrange(50, 600)),
                     fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    img = img.filter(ImageFilter.GaussianBlur(1))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def make_pdf(path, pages: int) -> None:
    c = canvas.Canvas(str(path), pagesize=landscape(A3))
    pw, ph = landscape(A3)
    for i in range(pages):
        c.drawImage(ImageReader(io.BytesIO(_photo(i))), 40, 80, pw - 80, ph - 160)
        c.setFont("Helvetica", 14)
        c.drawString(40, 40, f"Site photo sheet {i + 1} - Proposed rear extension")
        c.showPage()
    c.save()


@pytest.fixture(scope="module")
def big_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("in") / "Design and Access Statement.pdf"
    make_pdf(path, pages=12)
    return path


def test_already_small_is_untouched(big_pdf, tmp_path):
    res = compress_pdf(big_pdf, tmp_path, target_bytes=big_pdf.stat().st_size + 1)
    assert res.status == "unchanged"
    assert res.parts[0].read_bytes() == big_pdf.read_bytes()


def test_compresses_under_target_as_single_file(big_pdf, tmp_path):
    target = big_pdf.stat().st_size // 3
    res = compress_pdf(big_pdf, tmp_path, target_bytes=target)
    assert res.status == "compressed"
    assert len(res.parts) == 1
    assert res.part_sizes[0] <= target
    assert len(PdfReader(res.parts[0]).pages) == 12
    assert not list(tmp_path.glob(".work"))


def test_splits_when_quality_would_be_unreadable(big_pdf, tmp_path):
    # Find the size at the readable floor, then ask for a bit over half of it.
    from app.compressor import READABLE_LADDER, ghostscript_compress
    floor = ghostscript_compress(big_pdf, tmp_path / "floor.pdf", READABLE_LADDER[-1])
    (tmp_path / "floor.pdf").unlink()
    target = int(floor * 0.6)

    res = compress_pdf(big_pdf, tmp_path / "out", target_bytes=target)
    assert res.status == "split"
    assert 2 <= len(res.parts) <= 3
    assert all(s <= target for s in res.part_sizes)
    assert sum(len(PdfReader(p).pages) for p in res.parts) == 12
    assert res.parts[0].name == "Design and Access Statement - Part 1 of %d.pdf" % len(res.parts)


def test_impossible_target_reports_over_target(big_pdf, tmp_path):
    res = compress_pdf(big_pdf, tmp_path, target_bytes=5_000)
    assert res.status == "over_target"
    assert len(res.parts) == 1
    assert res.warnings
