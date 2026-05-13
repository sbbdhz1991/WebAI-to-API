"""视频上传与分析 demo 集合。

覆盖典型视频场景：
  - 单视频内容总结
  - 关键时间点抽取
  - 多视频对比
  - 视频问答
  - 长视频处理（先压缩再上传）
  - 通过 OpenAI 兼容端点上传

依赖：
    pip install httpx
    # 长视频压缩可选：
    # apt install ffmpeg / brew install ffmpeg

环境变量：
    WEBAI_URL          默认 http://localhost:6969
    GEMINI_API_KEY     服务端鉴权开启时必填

运行：
    python examples/video_demo.py summary         demo.mp4
    python examples/video_demo.py timeline        demo.mp4
    python examples/video_demo.py qa              demo.mp4 "片中主角穿什么颜色衣服？"
    python examples/video_demo.py compare         a.mp4 b.mp4
    python examples/video_demo.py compress-and-up large.mp4
    python examples/video_demo.py openai          demo.mp4
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

import httpx

BASE_URL = os.environ.get("WEBAI_URL", "http://localhost:6969").rstrip("/")
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 视频识别建议用 pro。flash 对视频内容理解较弱、容易遗漏细节。
DEFAULT_MODEL = "gemini-3-pro"

# 单次请求总大小默认 100 MB。超过这个值要么 ① 改 config 提升上限，要么 ② 用 compress-and-up 子命令先压缩。
SOFT_SIZE_WARN_MB = 80


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def _check_file(p: Path) -> None:
    if not p.is_file():
        sys.exit(f"找不到文件: {p}")
    size_mb = p.stat().st_size / (1024 * 1024)
    if size_mb > SOFT_SIZE_WARN_MB:
        print(
            f"[warn] {p.name} 大小 {size_mb:.1f} MB，接近/超过默认 max_upload_size_mb=100。"
            f" 如服务返回 413，请去 config.conf 调大 [Server] max_upload_size_mb，"
            "或先用 compress-and-up 子命令压缩。",
            file=sys.stderr,
        )


def _upload_via_gemini(
    video_path: Path,
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 900.0,
) -> str:
    """走 /gemini multipart 端点（最稳）。"""
    _check_file(video_path)
    url = f"{BASE_URL}/gemini"
    print(f"[upload] {video_path.name} ({video_path.stat().st_size / 1e6:.1f} MB) → {url}")
    with httpx.Client(timeout=timeout) as client:
        with video_path.open("rb") as fp:
            r = client.post(
                url,
                data={"message": prompt, "model": model},
                files={"files": (video_path.name, fp, "video/mp4")},
                headers=auth_headers(),
            )
    r.raise_for_status()
    return r.json()["response"]


def _upload_via_openai(
    video_path: Path,
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 900.0,
) -> str:
    """走 /v1/chat/completions 的 multipart 变体（payload 用 JSON 字符串放在表单里）。"""
    _check_file(video_path)
    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    print(f"[upload] {video_path.name} → {url}")
    with httpx.Client(timeout=timeout) as client:
        with video_path.open("rb") as fp:
            r = client.post(
                url,
                data={"payload": json.dumps(payload)},
                files={"files": (video_path.name, fp, "video/mp4")},
                headers=auth_headers(),
            )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_summary(args: argparse.Namespace) -> None:
    """单视频内容总结。"""
    out = _upload_via_gemini(
        Path(args.video),
        prompt="请简洁概括这段视频的内容，包含核心剧情/主题、主要画面元素以及整体风格。控制在 200 字内。",
        model=args.model,
    )
    print("\n--- 视频概述 ---\n" + out)


def cmd_timeline(args: argparse.Namespace) -> None:
    """关键时间点列表。模型会按秒级精度列出场景切换/重要事件。"""
    out = _upload_via_gemini(
        Path(args.video),
        prompt=(
            "请逐段分析这段视频，并按 markdown 表格输出关键时间点和对应内容。\n\n"
            "格式：\n"
            "| 时间点 | 画面/事件描述 |\n"
            "| --- | --- |\n"
            "| 00:01 | ... |\n\n"
            "时间点精确到秒，覆盖所有镜头切换或情节转折。"
        ),
        model=args.model,
    )
    print("\n--- 时间线 ---\n" + out)


def cmd_qa(args: argparse.Namespace) -> None:
    """对视频提问。"""
    if not args.question:
        sys.exit("qa 子命令需要 --question 参数")
    out = _upload_via_gemini(
        Path(args.video),
        prompt=f"基于视频内容回答：{args.question}。如果视频中没有相关信息，请明确说明。",
        model=args.model,
    )
    print("\n--- 回答 ---\n" + out)


def cmd_compare(args: argparse.Namespace) -> None:
    """多视频同时上传，做对比。"""
    paths = [Path(p) for p in args.videos]
    for p in paths:
        _check_file(p)
    if len(paths) < 2:
        sys.exit("compare 至少需要 2 个视频")

    url = f"{BASE_URL}/gemini"
    files_payload = []
    opened = []
    try:
        for p in paths:
            fp = p.open("rb")
            opened.append(fp)
            files_payload.append(("files", (p.name, fp, "video/mp4")))
        print(f"[upload] {len(paths)} 个视频 → {url}")
        with httpx.Client(timeout=900.0) as client:
            r = client.post(
                url,
                data={
                    "message": (
                        "请对比这些视频的内容、风格和叙事重点，"
                        "按"
                        "**核心差异 / 共同主题 / 各自亮点**"
                        "三个维度分析。"
                    ),
                    "model": args.model,
                },
                files=files_payload,
                headers=auth_headers(),
            )
    finally:
        for fp in opened:
            fp.close()
    r.raise_for_status()
    print("\n--- 对比 ---\n" + r.json()["response"])


def cmd_compress_and_up(args: argparse.Namespace) -> None:
    """先用 ffmpeg 压缩到可接受大小再上传。

    适合：原视频太大（> max_upload_size_mb）或带宽差。
    丢失的清晰度对 Gemini 理解通常无伤大雅。
    """
    if shutil.which("ffmpeg") is None:
        sys.exit("未找到 ffmpeg。compress-and-up 子命令需要先安装 ffmpeg。")

    src = Path(args.video)
    _check_file(src)

    target_mb = args.target_mb
    # 简单粗暴的两段策略：先按 CRF + 720p 试一下，超大再降 480p
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / (src.stem + "-compressed.mp4")
        for crf, scale in [(28, "720:-2"), (32, "480:-2"), (36, "360:-2")]:
            print(f"[ffmpeg] crf={crf} scale={scale} → {out.name}")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y", "-loglevel", "error",
                    "-i", str(src),
                    "-c:v", "libx264", "-crf", str(crf),
                    "-vf", f"scale={scale}",
                    "-c:a", "aac", "-b:a", "96k",
                    str(out),
                ],
                check=True,
            )
            size_mb = out.stat().st_size / (1024 * 1024)
            print(f"[ffmpeg] 压缩后 {size_mb:.1f} MB")
            if size_mb <= target_mb:
                break

        out_text = _upload_via_gemini(
            out,
            prompt="请概述视频内容并列出关键时间点。",
            model=args.model,
        )
    print("\n--- 概述 ---\n" + out_text)


def cmd_openai(args: argparse.Namespace) -> None:
    """走 OpenAI 兼容端点的视频上传（multipart 变体）。"""
    out = _upload_via_openai(
        Path(args.video),
        prompt="请详细描述视频内容。",
        model=args.model,
    )
    print("\n--- OpenAI 端点返回 ---\n" + out)


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="WebAI-to-API 视频上传与分析 demo 集合",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("summary", help="单视频内容总结")
    p1.add_argument("video")
    p1.add_argument("--model", default=DEFAULT_MODEL)
    p1.set_defaults(func=cmd_summary)

    p2 = sub.add_parser("timeline", help="关键时间点表")
    p2.add_argument("video")
    p2.add_argument("--model", default=DEFAULT_MODEL)
    p2.set_defaults(func=cmd_timeline)

    p3 = sub.add_parser("qa", help="对视频问答")
    p3.add_argument("video")
    p3.add_argument("question", nargs="?")
    p3.add_argument("--question", dest="question_kw", default=None)
    p3.add_argument("--model", default=DEFAULT_MODEL)
    p3.set_defaults(func=lambda a: cmd_qa(_resolve_qa(a)))

    p4 = sub.add_parser("compare", help="多视频对比")
    p4.add_argument("videos", nargs="+")
    p4.add_argument("--model", default=DEFAULT_MODEL)
    p4.set_defaults(func=cmd_compare)

    p5 = sub.add_parser("compress-and-up", help="ffmpeg 压缩后再上传")
    p5.add_argument("video")
    p5.add_argument("--target-mb", type=int, default=50, help="期望压缩后大小（MB）")
    p5.add_argument("--model", default=DEFAULT_MODEL)
    p5.set_defaults(func=cmd_compress_and_up)

    p6 = sub.add_parser("openai", help="走 /v1/chat/completions multipart 变体")
    p6.add_argument("video")
    p6.add_argument("--model", default=DEFAULT_MODEL)
    p6.set_defaults(func=cmd_openai)

    args = ap.parse_args()
    try:
        args.func(args)
    except httpx.HTTPStatusError as e:
        print(f"[HTTP {e.response.status_code}] {e.response.text}", file=sys.stderr)
        sys.exit(1)


def _resolve_qa(a: argparse.Namespace) -> argparse.Namespace:
    # 兼容 `qa video.mp4 "问题"` 和 `qa video.mp4 --question "..."`
    if not a.question and a.question_kw:
        a.question = a.question_kw
    return a


if __name__ == "__main__":
    main()
