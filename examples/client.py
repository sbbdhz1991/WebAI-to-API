"""WebAI-to-API 客户端 SDK（参考实现）。

提供 ``WebAIClient``（同步）和 ``AsyncWebAIClient`` 两个开箱即用的封装，
覆盖所有上传方式与所有端点。生产接入直接复制本文件 + 改环境变量即可。

依赖：
    pip install httpx

示例：
    from client import WebAIClient

    cli = WebAIClient(base_url="http://localhost:6969", api_key="...")

    # 1) 纯文本（OpenAI 兼容）
    reply = cli.chat("用一句话介绍 Python")
    print(reply)

    # 2) 单条带图片，OpenAI 多模态
    reply = cli.chat(
        "描述这张图",
        files=["photo.jpg"],
        model="gemini-3-pro",
    )

    # 3) 走 /gemini multipart 端点（兜底，最直接）
    reply = cli.gemini_generate("视频内容", files=["clip.mp4"])

    # 4) 持久会话，多轮对话
    reply1 = cli.gemini_chat("我刚刚说什么了？")  # 会维持上下文
    reply2 = cli.gemini_chat("再说一遍")

异步用法见 ``AsyncWebAIClient``。
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, Iterator, List, Optional, Union

import httpx

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

FileInput = Union[str, Path, "FileBlob"]


class FileBlob:
    """直接以 bytes 形式提供文件，无需落磁盘。"""

    def __init__(self, data: bytes, filename: str, mime_type: Optional[str] = None) -> None:
        self.data = data
        self.filename = filename
        self.mime_type = mime_type or (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )


# ---------------------------------------------------------------------------
# 同步客户端
# ---------------------------------------------------------------------------


class WebAIClient:
    """同步客户端。线程安全（httpx.Client 是）。"""

    def __init__(
        self,
        base_url: str = "http://localhost:6969",
        api_key: Optional[str] = None,
        timeout: float = 600.0,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or None
        self._client = httpx.Client(timeout=timeout, verify=verify_ssl)

    # ---- 上下文管理 ----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WebAIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部工具 ------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _prepare_file_tuples(
        files: Optional[Iterable[FileInput]],
    ) -> List[tuple]:
        """转成 httpx 的 files= 参数格式 [(field_name, (filename, content, mime))]."""
        if not files:
            return []
        out: List[tuple] = []
        for f in files:
            if isinstance(f, FileBlob):
                out.append(("files", (f.filename, f.data, f.mime_type)))
            elif isinstance(f, (str, Path)):
                p = Path(f)
                if not p.is_file():
                    raise FileNotFoundError(f"file not found: {p}")
                mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                out.append(("files", (p.name, p.read_bytes(), mime)))
            else:
                raise TypeError(f"unsupported file input: {type(f).__name__}")
        return out

    @staticmethod
    def _files_to_base64_list(files: Optional[Iterable[FileInput]]) -> List[dict]:
        """转成 JSON 端点的 files= 数组，每项 {filename, mime_type, content_base64}."""
        if not files:
            return []
        out: List[dict] = []
        for f in files:
            if isinstance(f, FileBlob):
                data, name, mime = f.data, f.filename, f.mime_type
            elif isinstance(f, (str, Path)):
                p = Path(f)
                if not p.is_file():
                    raise FileNotFoundError(f"file not found: {p}")
                data = p.read_bytes()
                name = p.name
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            else:
                raise TypeError(f"unsupported file input: {type(f).__name__}")
            out.append(
                {
                    "filename": name,
                    "mime_type": mime,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            )
        return out

    # ---- 公共端点 ------------------------------------------------------------

    def gemini_generate(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
        gem: Optional[str] = None,
    ) -> str:
        """``POST /gemini`` — 单次无状态生成。multipart 形式，最直接。"""
        url = f"{self.base_url}/gemini"
        data = {"message": message, "model": model}
        if gem:
            data["gem"] = gem
        files_tuples = self._prepare_file_tuples(files)
        r = self._client.post(
            url, data=data, files=files_tuples or None, headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["response"]

    def gemini_chat(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
        gem: Optional[str] = None,
    ) -> str:
        """``POST /gemini-chat`` — 维持会话上下文（服务端管理 session）。"""
        url = f"{self.base_url}/gemini-chat"
        data = {"message": message, "model": model}
        if gem:
            data["gem"] = gem
        files_tuples = self._prepare_file_tuples(files)
        r = self._client.post(
            url, data=data, files=files_tuples or None, headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["response"]

    def translate(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
    ) -> str:
        """``POST /translate`` — Translate-It 扩展专用，行为同 gemini_chat。"""
        url = f"{self.base_url}/translate"
        data = {"message": message, "model": model}
        files_tuples = self._prepare_file_tuples(files)
        r = self._client.post(
            url, data=data, files=files_tuples or None, headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["response"]

    def gemini_generate_json(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
        gem: Optional[str] = None,
    ) -> str:
        """``POST /gemini``（JSON + base64）— 当客户端不方便发 multipart 时使用。"""
        url = f"{self.base_url}/gemini"
        payload: dict = {"message": message, "model": model}
        if gem:
            payload["gem"] = gem
        if files:
            payload["files"] = self._files_to_base64_list(files)
        r = self._client.post(
            url,
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **self._auth_headers()},
        )
        r.raise_for_status()
        return r.json()["response"]

    def chat(
        self,
        prompt: Union[str, List[dict]],
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-pro",
        system: Optional[str] = None,
        history: Optional[List[dict]] = None,
        tools: Optional[List[dict]] = None,
        stream: bool = False,
        gem: Optional[str] = None,
    ) -> Union[str, Iterator[str]]:
        """``POST /v1/chat/completions`` — OpenAI 兼容，最通用入口。

        - ``prompt`` 可以是 str（自动包成单条 user message）或 messages 数组
        - ``files`` 会被嵌入到 user message 的 content 数组（OpenAI 多模态格式）
        - ``stream=True`` 返回 chunk 迭代器（SSE 解析后纯文本片段）
        """
        url = f"{self.base_url}/v1/chat/completions"
        messages = self._build_messages(prompt, files, system, history)
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if gem:
            payload["gem"] = gem
        if stream:
            payload["stream"] = True
            return self._stream_chat(url, payload)
        r = self._client.post(url, json=payload, headers=self._auth_headers())
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"].get("content") or ""

    def _stream_chat(self, url: str, payload: dict) -> Iterator[str]:
        with self._client.stream(
            "POST", url, json=payload, headers=self._auth_headers()
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0].get("delta") or obj["choices"][0].get("message") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    def list_models(self) -> List[dict]:
        """``GET /v1/models``."""
        r = self._client.get(f"{self.base_url}/v1/models", headers=self._auth_headers())
        r.raise_for_status()
        return r.json()["data"]

    def list_gems(self) -> List[dict]:
        """``GET /v1/gems`` — 列出账号下的 Gem。"""
        r = self._client.get(f"{self.base_url}/v1/gems", headers=self._auth_headers())
        r.raise_for_status()
        return r.json()["gems"]

    # ---- messages 构造工具（OpenAI 多模态） -------------------------------

    @staticmethod
    def _build_messages(
        prompt: Union[str, List[dict]],
        files: Optional[Iterable[FileInput]],
        system: Optional[str],
        history: Optional[List[dict]],
    ) -> List[dict]:
        if isinstance(prompt, list):
            # 用户自己拼好的 messages，直接用
            return prompt

        messages: List[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)

        if files:
            content_parts: List[dict] = [{"type": "text", "text": prompt}]
            for f in files:
                if isinstance(f, FileBlob):
                    data, name, mime = f.data, f.filename, f.mime_type
                elif isinstance(f, (str, Path)):
                    p = Path(f)
                    data = p.read_bytes()
                    name = p.name
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                else:
                    raise TypeError(f"unsupported file input: {type(f).__name__}")
                b64 = base64.b64encode(data).decode("ascii")
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    }
                )
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages


# ---------------------------------------------------------------------------
# 异步客户端
# ---------------------------------------------------------------------------


class AsyncWebAIClient:
    """异步客户端。API 与同步版完全对称，把方法 ``await`` 一下即可。"""

    def __init__(
        self,
        base_url: str = "http://localhost:6969",
        api_key: Optional[str] = None,
        timeout: float = 600.0,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "") or None
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify_ssl)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncWebAIClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    # ---- 复用同步类的纯函数辅助 ----------------------------------------------

    _prepare_file_tuples = staticmethod(WebAIClient._prepare_file_tuples)  # type: ignore[assignment]
    _files_to_base64_list = staticmethod(WebAIClient._files_to_base64_list)  # type: ignore[assignment]
    _build_messages = staticmethod(WebAIClient._build_messages)  # type: ignore[assignment]

    # ---- 端点（异步版） ------------------------------------------------------

    async def gemini_generate(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
        gem: Optional[str] = None,
    ) -> str:
        url = f"{self.base_url}/gemini"
        data = {"message": message, "model": model}
        if gem:
            data["gem"] = gem
        files_tuples = self._prepare_file_tuples(files)
        r = await self._client.post(
            url, data=data, files=files_tuples or None, headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["response"]

    async def gemini_chat(
        self,
        message: str,
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-flash",
        gem: Optional[str] = None,
    ) -> str:
        url = f"{self.base_url}/gemini-chat"
        data = {"message": message, "model": model}
        if gem:
            data["gem"] = gem
        files_tuples = self._prepare_file_tuples(files)
        r = await self._client.post(
            url, data=data, files=files_tuples or None, headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["response"]

    async def chat(
        self,
        prompt: Union[str, List[dict]],
        files: Optional[Iterable[FileInput]] = None,
        model: str = "gemini-3-pro",
        system: Optional[str] = None,
        history: Optional[List[dict]] = None,
        tools: Optional[List[dict]] = None,
        stream: bool = False,
        gem: Optional[str] = None,
    ) -> Union[str, AsyncIterator[str]]:
        url = f"{self.base_url}/v1/chat/completions"
        messages = self._build_messages(prompt, files, system, history)
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if gem:
            payload["gem"] = gem
        if stream:
            payload["stream"] = True
            return self._stream_chat(url, payload)
        r = await self._client.post(url, json=payload, headers=self._auth_headers())
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"].get("content") or ""

    async def _stream_chat(self, url: str, payload: dict) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST", url, json=payload, headers=self._auth_headers()
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0].get("delta") or obj["choices"][0].get("message") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    async def list_models(self) -> List[dict]:
        r = await self._client.get(
            f"{self.base_url}/v1/models", headers=self._auth_headers()
        )
        r.raise_for_status()
        return r.json()["data"]


# ---------------------------------------------------------------------------
# 命令行 demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="WebAI-to-API 客户端示例。")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("WEBAI_URL", "http://localhost:6969"),
    )
    parser.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    parser.add_argument(
        "--mode",
        choices=("text", "vision", "video", "stream", "session"),
        default="text",
    )
    parser.add_argument("--prompt", default="用一句话介绍你自己")
    parser.add_argument("--file", help="附件路径（vision/video 模式必填）")
    parser.add_argument("--model", default="gemini-3-pro")
    args = parser.parse_args()

    cli = WebAIClient(base_url=args.base_url, api_key=args.api_key)

    if args.mode == "text":
        print(cli.chat(args.prompt, model=args.model))

    elif args.mode == "vision":
        if not args.file:
            parser.error("--file is required for vision mode")
        print(cli.chat(args.prompt, files=[args.file], model=args.model))

    elif args.mode == "video":
        if not args.file:
            parser.error("--file is required for video mode")
        # 走 /gemini 端点，对视频更稳
        print(cli.gemini_generate(args.prompt, files=[args.file], model=args.model))

    elif args.mode == "stream":
        for chunk in cli.chat(args.prompt, model=args.model, stream=True):
            print(chunk, end="", flush=True)
        print()

    elif args.mode == "session":
        # 多轮对话示例：第二轮要求复述第一轮内容
        r1 = cli.gemini_chat("请记住一个秘密词：紫色独角兽")
        print("[turn 1]", r1)
        r2 = cli.gemini_chat("我刚刚让你记什么词？")
        print("[turn 2]", r2)


if __name__ == "__main__":
    _demo()
