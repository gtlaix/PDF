# Planning PDF Compressor

A small web app that runs on your own computer. Drag planning application PDFs (drawings,
site photos, Design & Access statements, scans) into it and it shrinks each one to fit
under a size limit, 10 MB by default, ready to upload to a planning portal.

- **Highest quality that fits:** it tries image resolutions from 300 DPI downwards and keeps
  the best one that gets the file under the limit. Text and vector drawings stay sharp and
  selectable; only embedded images are resampled.
- **Splits rather than ruins:** if a file won't fit even at 100 DPI (the lowest it considers
  comfortably readable), it is split into **at most 3 parts**
  (`Name - Part 1 of 3.pdf`, …), each under the limit.
- **Last resort:** if 3 parts at 100 DPI still aren't enough, it goes lower (down to 50 DPI)
  and flags the result so you can check it. Files that can't be brought under the limit at
  all are flagged too.
- **Batch:** drop many files at once, then download them individually or all as one ZIP.
- **Before/after report:** shows the original size, the new size, the saving and the quality
  used for each file, and can be downloaded as CSV.
- **Private:** files never leave your computer. Results are deleted from a temp folder after 6 hours.

Sizes are in decimal megabytes (10 MB = 10,000,000 bytes), matching how upload portals
count, so a "10 MB" result will pass a 10 MB limit.

## Running it

### Option A: Docker (easiest, works on Windows, Mac and Linux)

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), then in this folder run:

```bash
docker compose up -d --build
```

Open <http://localhost:8000>. It keeps running in the background and restarts with Docker.
To stop it:

```bash
docker compose down
```

### Option B: Python

1. Install **Python 3.10+** and **Ghostscript**:
   - Windows: Ghostscript from <https://ghostscript.com/releases/gsdnld.html> (64-bit installer).
   - Mac: `brew install ghostscript`
   - Linux: `sudo apt install ghostscript`
2. In this folder:

   ```bash
   python -m venv .venv
   ```

   ```bash
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   ```

   ```bash
   pip install -r requirements.txt
   ```

   ```bash
   python -m app
   ```

   Your browser opens at <http://localhost:8000>.

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```

## How it works

| File | What it does |
| --- | --- |
| `app/compressor.py` | The compression engine. Runs Ghostscript at different quality levels, searches for the best one that fits, and splits the file with pypdf if needed. |
| `app/main.py` | FastAPI server: upload, download and ZIP endpoints. |
| `app/static/index.html` | The drag-and-drop page. |

The quality levels (DPI and JPEG quality) and the 100 DPI readability floor are defined at
the top of `app/compressor.py` if you want to tune them.

**Limits:** large vector CAD drawings and long text-heavy documents contain few images, so
they shrink less. If one of those won't fit in 3 parts, the app says so and you'll need to
split it by hand or raise the limit.
