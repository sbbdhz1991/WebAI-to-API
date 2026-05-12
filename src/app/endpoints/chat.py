# src/app/endpoints/chat.py
import json
import time
from typing import Any, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, UploadFile

from app.logger import logger
from app.services.gemini_client import get_gemini_client, GeminiClientNotInitializedError
from app.services.session_manager import get_translate_session_manager
from app.utils.files import (
    FileEntry,
    check_content_length,
    enforce_total_size,
    get_max_upload_size,
    materialize_files,
    parse_gemini_call,
    resolve_json_files,
    resolve_openai_content_parts,
)

router = APIRouter()

@router.get("/v1/gems")
async def list_gems():
    try:
        gemini_client = get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        gems = await gemini_client.fetch_gems()
        return {
            "gems": [
                {
                    "id": gem.id,
                    "name": gem.name,
                    "description": gem.description,
                    "predefined": gem.predefined,
                }
                for gem in gems
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching gems: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching gems: {str(e)}")


@router.post("/translate")
async def translate_chat(request: Request):
    try:
        get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    session_manager = get_translate_session_manager()
    if not session_manager:
        raise HTTPException(status_code=503, detail="Session manager is not initialized.")

    call = await parse_gemini_call(request)
    try:
        async with materialize_files(call.files) as files:
            response = await session_manager.get_response(
                call.model, call.message, files, call.gem
            )
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Error in /translate endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error during translation: {str(e)}")


def _build_tools_prompt(tools: list) -> str:
    """Convert OpenAI tool definitions to a system prompt for Gemini."""
    declarations = []
    for t in tools:
        if t.get("type") == "function" and "function" in t:
            declarations.append(t["function"])
    if not declarations:
        return ""
    lines = [
        "You have access to the following tools. When you want to call a tool, respond with "
        "ONLY a JSON object in this exact format, with no other text before or after:\n"
        '{"tool_call": {"name": "<tool_name>", "arguments": {<arguments>}}}\n',
        "Available tools:",
    ]
    for fn in declarations:
        lines.append(f"- {fn['name']}: {fn.get('description', '')}")
        if fn.get("parameters"):
            lines.append(f"  Parameters: {json.dumps(fn['parameters'])}")
    return "\n".join(lines)


def _parse_tool_call(text: str) -> Optional[dict]:
    """Extract a tool_call JSON object from model response text."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == '{':
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, dict) and "tool_call" in obj:
                    return obj["tool_call"]
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def convert_to_openai_format(response_text: str, model: str, stream: bool = False, tool_call: Optional[dict] = None):
    ts = int(time.time())
    choice_key = "delta" if stream else "message"

    if tool_call:
        args = tool_call.get("arguments", {})
        content = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{ts}",
                "type": "function",
                "function": {
                    "name": tool_call.get("name", ""),
                    "arguments": json.dumps(args) if isinstance(args, dict) else args,
                },
            }],
        }
        return {
            "id": f"chatcmpl-{ts}",
            "object": "chat.completion.chunk" if stream else "chat.completion",
            "created": ts,
            "model": model,
            "choices": [{
                "index": 0,
                choice_key: content,
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return {
        "id": f"chatcmpl-{ts}",
        "object": "chat.completion.chunk" if stream else "chat.completion",
        "created": ts,
        "model": model,
        "choices": [{
            "index": 0,
            choice_key: {
                "role": "assistant",
                "content": response_text,
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@router.get("/v1/models")
async def list_models():
    from gemini_webapi.constants import Model
    ts = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model.model_name,
                "object": "model",
                "created": ts,
                "owned_by": "google",
            }
            for model in Model
            if model != Model.UNSPECIFIED
        ],
    }


async def _parse_openai_chat_request(
    request: Request,
    max_bytes: int = 0,
) -> Tuple[dict, Optional[List[FileEntry]]]:
    """Parse /v1/chat/completions from either JSON or multipart.

    Multipart contract: `payload` (or `messages`) holds a JSON string
    matching the regular OpenAIChatRequest body; one or more `files`
    uploads are appended to whatever files the JSON body already
    declares.
    """
    ct = (request.headers.get("content-type") or "").lower()
    if ct.startswith("multipart/form-data"):
        form = await request.form()
        raw_payload = form.get("payload") or form.get("messages")
        if isinstance(raw_payload, str):
            try:
                body = json.loads(raw_payload)
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=400, detail=f"invalid JSON in 'payload' field: {e}"
                )
            if isinstance(body, list):
                # `messages` field used directly as the messages array
                body = {"messages": body}
        else:
            body = {}
        # form scalar overrides (handy for curl examples)
        for k in ("model", "gem"):
            v = form.get(k)
            if isinstance(v, str) and v:
                body[k] = v
        if "stream" in form:
            sv = form.get("stream")
            if isinstance(sv, str):
                body["stream"] = sv.lower() in ("1", "true", "yes")
        uploaded: List[FileEntry] = []
        from app.utils.files import FileBlob
        for _key, value in form.multi_items():
            if isinstance(value, UploadFile):
                try:
                    data = await value.read()
                finally:
                    await value.close()
                uploaded.append(FileBlob(data, value.filename or None))
                enforce_total_size(uploaded, max_bytes)
        return body, (uploaded or None)

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    extra_files = resolve_json_files(body.get("files"))
    enforce_total_size(extra_files, max_bytes)
    return body, extra_files


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        gemini_client = get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    max_bytes = get_max_upload_size()
    check_content_length(request, max_bytes)
    body, extra_files = await _parse_openai_chat_request(request, max_bytes)

    messages: List[dict] = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided.")

    model: Optional[str] = body.get("model")
    stream: bool = bool(body.get("stream"))
    tools: Optional[List[dict]] = body.get("tools")
    gem: Optional[Any] = body.get("gem")

    # Pull multimodal parts (image_url / file) out of message content arrays.
    try:
        messages, multimodal_files = await resolve_openai_content_parts(messages, max_bytes)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving multimodal content: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"invalid multimodal content: {e}")

    all_files: List[FileEntry] = []
    if extra_files:
        all_files.extend(extra_files)
    all_files.extend(multimodal_files)
    enforce_total_size(all_files, max_bytes)
    files_for_call = all_files or None

    conversation_parts: List[str] = []

    tools_prompt = _build_tools_prompt(tools) if tools else ""

    system_msg_index = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            system_msg_index = i
            break

    if tools_prompt:
        if system_msg_index != -1:
            orig_content = messages[system_msg_index].get("content") or ""
            messages[system_msg_index]["content"] = f"{orig_content}\n\n{tools_prompt}".strip()
        else:
            conversation_parts.append(tools_prompt)

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""

        if role == "system":
            conversation_parts.append(f"System: {content}")
        elif role == "user":
            conversation_parts.append(f"User: {content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    conversation_parts.append(
                        f"Assistant called tool {fn.get('name')}: {fn.get('arguments', '')}"
                    )
            elif content:
                conversation_parts.append(f"Assistant: {content}")
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            conversation_parts.append(f"Tool result [{tool_call_id}]: {content}")

    if not conversation_parts:
        raise HTTPException(status_code=400, detail="No valid messages found.")

    final_prompt = "\n\n".join(conversation_parts)

    if not model:
        raise HTTPException(status_code=400, detail="Model not specified in the request.")

    try:
        async with materialize_files(files_for_call) as files:
            logger.debug(
                f"/v1/chat/completions calling generate_content with "
                f"{len(files) if files else 0} file(s): "
                f"{[str(p) for p in (files or [])]}"
            )
            response = await gemini_client.generate_content(
                message=final_prompt, model=model, files=files, gem=gem
            )
        logger.debug(f"Gemini raw response: {response.text!r}")
        tool_call = _parse_tool_call(response.text) if tools else None
        logger.debug(f"Parsed tool_call: {tool_call}")

        openai_response = convert_to_openai_format(response.text, model, stream, tool_call)

        if stream:
            from fastapi.responses import StreamingResponse
            async def sse_stream():
                yield f"data: {json.dumps(openai_response)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(sse_stream(), media_type="text/event-stream")

        return openai_response
    except Exception as e:
        logger.error(f"Error in /v1/chat/completions endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing chat completion: {str(e)}")
