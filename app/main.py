"""Local web app: drop PDFs in, get them back under a target size."""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .compressor import GhostscriptMissing, compress_pdf, find_ghostscript

STATIC_DIR = Path(__file__).parent / "static"
JOBS_DIR = Path(tempfile.gettempdir()) / "pdf-compressor-jobs"
JOB_TTL_SECONDS = 6 * 60 * 60
MB = 1_000_000  # decimal megabytes, matching how upload portals usually count

app = FastAPI(title="Planning PDF Compressor")


def _cleanup_old_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - JOB_TTL_SECONDS
    for d in JOBS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "document.pdf").stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
    return stem[:150] or "document"


def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "Unknown job")
    d = JOBS_DIR / job_id
    if not d.is_dir():
        raise HTTPException(404, "This result has expired; please compress the file again.")
    return d


@app.get("/api/health")
def health():
    try:
        find_ghostscript()
        return {"ok": True}
    except GhostscriptMissing as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/compress")
def compress(file: UploadFile = File(...), target_mb: float = Form(10.0)):
    if not (0.5 <= target_mb <= 500):
        raise HTTPException(400, "Target size must be between 0.5 and 500 MB.")
    _cleanup_old_jobs()

    job_id = uuid.uuid4().hex
    job = JOBS_DIR / job_id
    out = job / "out"
    out.mkdir(parents=True)
    src = job / "input.pdf"
    with open(src, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    with open(src, "rb") as fh:
        if fh.read(1024).find(b"%PDF") == -1:
            shutil.rmtree(job, ignore_errors=True)
            raise HTTPException(400, "That file doesn't look like a PDF.")

    try:
        result = compress_pdf(
            src, out, target_bytes=int(target_mb * MB), output_stem=_safe_stem(file.filename)
        )
    except GhostscriptMissing as e:
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(500, str(e))
    except Exception as e:  # corrupt / encrypted PDFs etc.
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(422, f"Couldn't process this PDF: {e}")
    finally:
        src.unlink(missing_ok=True)

    return {
        "id": job_id,
        "name": file.filename,
        "status": result.status,
        "original_size": result.original_size,
        "total_size": result.total_size,
        "target_size": int(target_mb * MB),
        "quality": result.quality,
        "warnings": result.warnings,
        "parts": [
            {"name": p.name, "size": s, "url": f"/api/download/{job_id}/{p.name}"}
            for p, s in zip(result.parts, result.part_sizes)
        ],
    }


@app.get("/api/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    out = _job_dir(job_id) / "out"
    path = (out / filename).resolve()
    if path.parent != out.resolve() or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)


class ZipRequest(BaseModel):
    ids: list[str]


@app.post("/api/zip")
def download_zip(req: ZipRequest):
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for job_id in req.ids:
            for path in sorted((_job_dir(job_id) / "out").glob("*.pdf")):
                name, n = path.name, 2
                while name in used:
                    name = f"{path.stem} ({n}).pdf"
                    n += 1
                used.add(name)
                zf.write(path, name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="compressed-pdfs.zip"'},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
