#!/usr/bin/env bash
# WebAI-to-API curl 速查表（所有端点 + 所有上传方式）。
#
# 直接执行：
#   ./curl_cheatsheet.sh                # 全部跑一遍（涉及上传的需要本地有 photo.jpg 等文件）
#   ./curl_cheatsheet.sh text           # 只跑文本对话
#   ./curl_cheatsheet.sh vision         # 只跑图像识别
#
# 环境变量：
#   WEBAI_URL       服务地址（默认 http://localhost:6969）
#   GEMINI_API_KEY  鉴权开关；服务端没开则可留空

set -u

BASE="${WEBAI_URL:-http://localhost:6969}"
API_KEY="${GEMINI_API_KEY:-}"

if [ -n "$API_KEY" ]; then
  AUTH=(-H "Authorization: Bearer $API_KEY")
else
  AUTH=()
fi

echo "===== Target: $BASE ====="
echo

# ---------------------------------------------------------------------------
# 1. 健康检查（列模型 / 列 gem）
# ---------------------------------------------------------------------------
section_health() {
  echo "----- GET /v1/models -----"
  curl -sS "$BASE/v1/models" "${AUTH[@]}" | head -c 500; echo; echo

  echo "----- GET /v1/gems -----"
  curl -sS "$BASE/v1/gems" "${AUTH[@]}" | head -c 500; echo; echo
}

# ---------------------------------------------------------------------------
# 2. 纯文本（OpenAI 兼容）
# ---------------------------------------------------------------------------
section_text() {
  echo "----- POST /v1/chat/completions（纯文本） -----"
  curl -sS "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d '{
      "model": "gemini-3-pro",
      "messages": [
        {"role": "system", "content": "你是个简洁的助手"},
        {"role": "user", "content": "用一句话介绍 Python"}
      ]
    }'
  echo; echo
}

# ---------------------------------------------------------------------------
# 3. 流式输出
# ---------------------------------------------------------------------------
section_stream() {
  echo "----- POST /v1/chat/completions（stream=true，SSE 流式） -----"
  curl -sN "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d '{
      "model": "gemini-3-pro",
      "stream": true,
      "messages": [{"role": "user", "content": "写一首关于代码的 4 行短诗"}]
    }'
  echo; echo
}

# ---------------------------------------------------------------------------
# 4. 视觉（OpenAI 多模态 + data: URL）
# ---------------------------------------------------------------------------
section_vision() {
  local img="${1:-photo.jpg}"
  if [ ! -f "$img" ]; then echo "[skip] vision: 找不到 $img"; return; fi

  echo "----- POST /v1/chat/completions（多模态 image_url） -----"
  local b64
  b64=$(base64 -w 0 "$img" 2>/dev/null || base64 "$img" | tr -d '\n')
  curl -sS "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d "{
      \"model\": \"gemini-3-pro\",
      \"messages\": [{
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"text\", \"text\": \"用 30 字描述这张图\"},
          {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/jpeg;base64,$b64\"}}
        ]
      }]
    }"
  echo; echo
}

# ---------------------------------------------------------------------------
# 5. /gemini multipart 上传（最直接的附件方式）
# ---------------------------------------------------------------------------
section_multipart() {
  local img="${1:-photo.jpg}"
  if [ ! -f "$img" ]; then echo "[skip] multipart: 找不到 $img"; return; fi

  echo "----- POST /gemini（multipart 单图） -----"
  curl -sS "$BASE/gemini" \
    -F message="描述图片内容" \
    -F model=gemini-3-pro \
    -F "files=@$img" \
    "${AUTH[@]}"
  echo; echo

  echo "----- POST /gemini（multipart 多文件） -----"
  curl -sS "$BASE/gemini" \
    -F message="对比这些文件" \
    -F model=gemini-3-pro \
    -F "files=@$img" \
    -F "files=@$img" \
    "${AUTH[@]}"
  echo; echo
}

# ---------------------------------------------------------------------------
# 6. /gemini-chat 会话对话（服务端保留上下文）
# ---------------------------------------------------------------------------
section_session() {
  echo "----- POST /gemini-chat（第 1 轮） -----"
  curl -sS "$BASE/gemini-chat" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d '{"message": "记住这个词：紫色独角兽", "model": "gemini-3-flash"}'
  echo; echo

  echo "----- POST /gemini-chat（第 2 轮，应能记住） -----"
  curl -sS "$BASE/gemini-chat" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d '{"message": "我刚刚让你记的词是什么？", "model": "gemini-3-flash"}'
  echo; echo
}

# ---------------------------------------------------------------------------
# 7. JSON + base64（适合不方便发 multipart 的客户端）
# ---------------------------------------------------------------------------
section_json_base64() {
  local img="${1:-photo.jpg}"
  if [ ! -f "$img" ]; then echo "[skip] json+base64: 找不到 $img"; return; fi

  local b64
  b64=$(base64 -w 0 "$img" 2>/dev/null || base64 "$img" | tr -d '\n')

  echo "----- POST /gemini（JSON + base64 文件） -----"
  curl -sS "$BASE/gemini" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    -d "{
      \"message\": \"描述图片\",
      \"model\": \"gemini-3-pro\",
      \"files\": [
        {\"filename\": \"$(basename "$img")\", \"mime_type\": \"image/jpeg\", \"content_base64\": \"$b64\"}
      ]
    }"
  echo; echo
}

# ---------------------------------------------------------------------------
# 8. /v1/chat/completions multipart 变体
#    （payload 用 JSON 字符串放在表单里，files 单独传）
# ---------------------------------------------------------------------------
section_chat_multipart() {
  local img="${1:-photo.jpg}"
  if [ ! -f "$img" ]; then echo "[skip] chat multipart: 找不到 $img"; return; fi

  echo "----- POST /v1/chat/completions（multipart 变体） -----"
  curl -sS "$BASE/v1/chat/completions" \
    -F 'payload={"model":"gemini-3-pro","messages":[{"role":"user","content":"描述图片"}]}' \
    -F "files=@$img" \
    "${AUTH[@]}"
  echo; echo
}

# ---------------------------------------------------------------------------
# 9. 视频
# ---------------------------------------------------------------------------
section_video() {
  local video="${1:-demo.mp4}"
  if [ ! -f "$video" ]; then echo "[skip] video: 找不到 $video"; return; fi

  echo "----- POST /gemini（视频） -----"
  curl -sS "$BASE/gemini" \
    -F message="逐段描述视频内容，列出关键时间点" \
    -F model=gemini-3-pro \
    -F "files=@$video" \
    "${AUTH[@]}"
  echo; echo
}

# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
case "${1:-all}" in
  health)         section_health ;;
  text)           section_text ;;
  stream)         section_stream ;;
  vision)         section_vision "${2:-photo.jpg}" ;;
  multipart)      section_multipart "${2:-photo.jpg}" ;;
  session)        section_session ;;
  json-base64)    section_json_base64 "${2:-photo.jpg}" ;;
  chat-multipart) section_chat_multipart "${2:-photo.jpg}" ;;
  video)          section_video "${2:-demo.mp4}" ;;
  all|*)
    section_health
    section_text
    section_stream
    section_vision     "${2:-photo.jpg}"
    section_multipart  "${2:-photo.jpg}"
    section_session
    section_json_base64 "${2:-photo.jpg}"
    section_chat_multipart "${2:-photo.jpg}"
    section_video      "${3:-demo.mp4}"
    ;;
esac
