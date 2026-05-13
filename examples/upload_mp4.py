"""Demo: upload an mp4 to the WebAI-to-API server.

Three variants — all hit the same `/gemini` endpoint, pick whichever
fits your client style.

Usage:
    python examples/upload_mp4.py path/to/video.mp4
    python examples/upload_mp4.py path/to/video.mp4 --mode json
    python examples/upload_mp4.py path/to/video.mp4 --mode openai

Requirements:
    pip install httpx
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx

DEFAULT_URL = os.environ.get("WEBAI_URL", "http://43.130.151.184:6969")
DEFAULT_KEY = os.environ.get("GEMINI_API_KEY", "Yw1EYOJWVH2MIyiXs2AaHiaTQ29mmnYYfkP5hjQ2")  # optional
DEFAULT_PROMPT = "请概述这段视频的内容，并列出关键时间点。"
DEFAULT_MODEL = "gemini-3-pro"  # video works best on pro


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {DEFAULT_KEY}"} if DEFAULT_KEY else {}


def upload_multipart(video: Path, prompt: str, model: str) -> dict:
    """Variant 1 — multipart/form-data on /gemini (recommended)."""
    url = f"{DEFAULT_URL}/gemini"
    with httpx.Client(timeout=600.0) as client:
        with video.open("rb") as fp:
            files = {"files": (video.name, fp, "video/mp4")}
            data = {"message": prompt, "model": model}
            r = client.post(url, data=data, files=files, headers=auth_headers())
    r.raise_for_status()
    return r.json()


def upload_json_base64(video: Path, prompt: str, model: str) -> dict:
    """Variant 2 — JSON body with base64-embedded file."""
    url = f"{DEFAULT_URL}/gemini"
    payload = {
        "message": prompt,
        "model": model,
        "files": [
            {
                "filename": video.name,
                "mime_type": "video/mp4",
                "content_base64": base64.b64encode(video.read_bytes()).decode("ascii"),
            }
        ],
    }
    headers = {"Content-Type": "application/json", **auth_headers()}
    with httpx.Client(timeout=600.0) as client:
        r = client.post(url, content=json.dumps(payload), headers=headers)
    r.raise_for_status()
    return r.json()


def upload_openai_multimodal(video: Path, prompt: str, model: str) -> dict:
    """Variant 3 — OpenAI-compatible /v1/chat/completions, multipart variant.

    We embed the OpenAI-style messages JSON in the `payload` form field and
    attach the video as a regular file upload.
    """
    url = f"{DEFAULT_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=600.0) as client:
        with video.open("rb") as fp:
            files = {"files": (video.name, fp, "video/mp4")}
            data = {"payload": json.dumps(payload)}
            r = client.post(url, data=data, files=files, headers=auth_headers())
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload an mp4 to WebAI-to-API and print the response.")
    ap.add_argument("video", type=Path, help="Path to the .mp4 file")
    ap.add_argument(
        "--mode",
        choices=("multipart", "json", "openai"),
        default="multipart",
        help="Upload variant to use (default: multipart)",
    )
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    if not args.video.is_file():
        print(f"error: {args.video} is not a file", file=sys.stderr)
        return 2

    size_mb = args.video.stat().st_size / (1024 * 1024)
    print(f"[+] uploading {args.video.name} ({size_mb:.1f} MB) via {args.mode} ...")

    runner = {
        "multipart": upload_multipart,
        "json": upload_json_base64,
        "openai": upload_openai_multimodal,
    }[args.mode]

    try:
        result = runner(args.video, args.prompt, args.model)
    except httpx.HTTPStatusError as e:
        print(f"[!] HTTP {e.response.status_code}: {e.response.text}", file=sys.stderr)
        return 1

    if args.mode == "openai":
        text = result["choices"][0]["message"]["content"]
    else:
        text = result.get("response", json.dumps(result, ensure_ascii=False, indent=2))
    print("\n--- Gemini response ---")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
