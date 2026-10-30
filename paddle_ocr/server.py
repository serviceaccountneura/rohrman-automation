"""PaddleOCR text recognition as a small HTTP service.

TESTING BRANCH (staging-test-gemini-2.5pro-confidence-score).

One of three readers that vote on the handwritten GL lines and the invoice
number (see api/services/confidence_read.py). It reads small crops -- one
account, one amount -- that Gemini has boxed on the page. Recognition only:
the text-detection model needs more memory than this host can spare on a
page, and on a tight crop there is nothing to detect.

Its own container with a hard memory limit, so if it ever runs out of memory
it is this process that dies, not the API.

    POST /read   {"images": ["<base64 png>", ...]}
              -> {"results": [{"text": "#2430", "score": 0.964}, ...]}
    GET  /health -> {"status": "ok"}
"""
from __future__ import annotations

import base64
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import numpy as np  # noqa: E402
from paddleocr import TextRecognition  # noqa: E402
from PIL import Image  # noqa: E402

MODEL = os.environ.get("PADDLE_REC_MODEL", "PP-OCRv5_server_rec")
# oneDNN breaks PaddlePaddle 3.3 inference on CPU ("ConvertPirAttribute2Runtime
# Attribute not support"), so it stays off.
recognizer = TextRecognition(model_name=MODEL, enable_mkldnn=False)
MAX_IMAGES = 40


def read(images: list[str]) -> list[dict]:
    out = []
    for b64 in images[:MAX_IMAGES]:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            res = recognizer.predict(np.array(img)[:, :, ::-1].copy())[0]
            out.append({"text": str(res["rec_text"]), "score": round(float(res["rec_score"]), 4)})
        except Exception as e:  # noqa: BLE001 -- one bad crop must not lose the others
            out.append({"text": "", "score": 0.0, "error": str(e)[:200]})
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200 if self.path == "/health" else 404, {"status": "ok", "model": MODEL})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/read":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, {"results": read(list(body.get("images") or []))})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)[:300]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[PADDLE] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"[PADDLE] {MODEL} loaded, listening on :8100", flush=True)
    # One request at a time: the model is not shared across threads, and the
    # queue in front of it is one invoice at a time anyway.
    HTTPServer(("0.0.0.0", 8100), Handler).serve_forever()
