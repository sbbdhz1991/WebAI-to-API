"""用官方 ``openai`` SDK 直接接入 WebAI-to-API。

由于 ``/v1/chat/completions`` 完全 OpenAI 兼容，把 ``base_url`` 指过来就行。

依赖：
    pip install openai

运行：
    python examples/openai_sdk_example.py
"""
from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

from openai import OpenAI

BASE_URL = os.environ.get("WEBAI_URL", "http://localhost:6969") + "/v1"
API_KEY = os.environ.get("GEMINI_API_KEY", "dummy-if-auth-disabled")

# WebAI-to-API 鉴权开启时用 GEMINI_API_KEY，关闭时 api_key 随便填一个
# 非空字符串即可（OpenAI SDK 必填）。
client = OpenAI(base_url=BASE_URL, api_key=API_KEY)


# ---------------------------------------------------------------------------
# 1. 最普通的文本对话
# ---------------------------------------------------------------------------

def demo_text() -> None:
    resp = client.chat.completions.create(
        model="gemini-3-pro",
        messages=[
            {"role": "system", "content": "你是个简洁的助手，回复控制在 30 字内。"},
            {"role": "user", "content": "解释什么是 Python 装饰器"},
        ],
    )
    print("[文本]", resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# 2. 多轮上下文
# ---------------------------------------------------------------------------

def demo_multiturn() -> None:
    history = [
        {"role": "user", "content": "我叫张三"},
        {"role": "assistant", "content": "好的，张三你好。"},
    ]
    history.append({"role": "user", "content": "我刚才告诉你我叫什么？"})
    resp = client.chat.completions.create(
        model="gemini-3-flash",
        messages=history,
    )
    print("[多轮]", resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# 3. 视觉理解（OpenAI 多模态格式：image_url）
# ---------------------------------------------------------------------------

def demo_vision(image_path: str = "frame_001.jpg") -> None:
    p = Path(image_path)
    if not p.is_file():
        print(f"[视觉] 跳过：找不到 {p}")
        return
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    data_url = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    resp = client.chat.completions.create(
        model="gemini-3-pro",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "用 50 字以内描述这张图。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    print("[视觉]", resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# 4. 流式输出
# ---------------------------------------------------------------------------

def demo_stream() -> None:
    stream = client.chat.completions.create(
        model="gemini-3-pro",
        messages=[{"role": "user", "content": "写一首关于秋天的 4 行短诗"}],
        stream=True,
    )
    print("[流式] ", end="", flush=True)
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            print(delta.content, end="", flush=True)
    print()


# ---------------------------------------------------------------------------
# 5. Tool calling（OpenAI 函数调用风格）
# ---------------------------------------------------------------------------

def demo_tools() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询某城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名"},
                    },
                    "required": ["city"],
                },
            },
        }
    ]
    resp = client.chat.completions.create(
        model="gemini-3-pro",
        messages=[{"role": "user", "content": "帮我查下东京的天气"}],
        tools=tools,
    )
    msg = resp.choices[0].message
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        print(f"[工具] 模型请求调用 {tc.function.name}({tc.function.arguments})")
    else:
        print("[工具] 模型未触发工具：", msg.content)


if __name__ == "__main__":
    demo_text()
    demo_multiturn()
    demo_vision()  # 没图片会自动跳过
    demo_stream()
    demo_tools()
