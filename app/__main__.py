"""Run with `python -m app`, then open http://localhost:8000 (opened automatically)."""

import os
import threading
import webbrowser

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)
