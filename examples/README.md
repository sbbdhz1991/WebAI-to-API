# WebAI-to-API 客户端接入示例

按你的语言/场景选一个进去：

| 场景 | 文件 | 跑法 |
|---|---|---|
| Python 通用客户端（推荐） | `client.py` | `python examples/client.py --mode vision --file photo.jpg` |
| **视频上传 / 分析** | `video_demo.py` | `python examples/video_demo.py summary demo.mp4` |
| OpenAI SDK 直接接入 | `openai_sdk_example.py` | `pip install openai && python examples/openai_sdk_example.py` |
| curl 速查表 | `curl_cheatsheet.sh` | `./curl_cheatsheet.sh text` 或 `./curl_cheatsheet.sh vision photo.jpg` |
| Node.js (18+) | `node_client.js` | `node examples/node_client.js vision photo.jpg` |
| 简版单脚本上传 | `upload_mp4.py` | `python examples/upload_mp4.py demo.mp4 --mode multipart` |
| 哈希调试工具 | `find_hash.py` | 仅供协议反向工程时用 |

### 视频场景细分（`video_demo.py` 子命令）

| 子命令 | 用途 |
|---|---|
| `summary <video>` | 单视频概要总结 |
| `timeline <video>` | 输出 markdown 时间线表（带秒级时间戳） |
| `qa <video> "问题"` | 对视频问答 |
| `compare <v1> <v2> ...` | 多视频同时上传，做对比 |
| `compress-and-up <video>` | 大视频先用 ffmpeg 压缩到目标大小再上传 |
| `openai <video>` | 走 OpenAI 兼容端点（`/v1/chat/completions` multipart 变体） |

视频用 `gemini-3-pro` 模型最佳（`flash` 对视频细节理解偏弱）。

---

## 环境变量

所有 demo 共用：

```bash
export WEBAI_URL="http://localhost:6969"          # 服务地址
export GEMINI_API_KEY="your-key"                  # 服务端开启鉴权才需要
```

---

## 端点速查

| 端点 | 用途 | 上下文 | 推荐场景 |
|---|---|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容 | 客户端传 history | 主推。已有 OpenAI SDK 代码无缝迁移 |
| `POST /gemini` | 单次生成 | 无 | 服务端无状态调用，最稳 |
| `POST /gemini-chat` | 多轮对话 | 服务端保留 session | 简单聊天机器人场景 |
| `POST /translate` | 翻译 | 服务端保留 session | 浏览器扩展 |
| `GET /v1/models` | 列模型 | — | 健康检查 |
| `GET /v1/gems` | 列 Gem | — | 自定义 prompt 场景 |

---

## 文件上传三种方式（同效）

1. **multipart**（最直接）：`-F files=@photo.jpg`
2. **JSON + base64**（适合无法发 multipart 的客户端）：
   ```json
   {"files": [{"filename":"x.jpg","content_base64":"...","mime_type":"image/jpeg"}]}
   ```
3. **OpenAI 多模态**（适合 `/v1/chat/completions`，跟 OpenAI 完全一致）：
   ```json
   {"messages":[{"role":"user","content":[
     {"type":"text","text":"..."},
     {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
   ]}]}
   ```

支持的类型：图片（jpg/png/webp）、视频（mp4/mov/webm）、PDF、音频。**视频建议用 `gemini-3-pro` 模型**（`flash` 对视频识别弱）。

---

## 出现问题？

- **`503 Gemini cookies not found`** → 没填 cookie，看主 README 的 [Authentication](../README.md#authentication) + [File Uploads → Prerequisites](../README.md#prerequisites-full-browser-cookies)
- **文件上传 hang / Watchdog 超时** → `gemini_cookie_extra` 没配或过期，参见主 README 的故障排查表
- **`413 Payload Too Large`** → 文件超过 `max_upload_size_mb`，去 `config.conf` 调大或设 0
