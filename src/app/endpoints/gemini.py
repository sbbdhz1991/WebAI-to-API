# src/app/endpoints/gemini.py
from fastapi import APIRouter, HTTPException, Request

from app.logger import logger
from app.services.gemini_client import get_gemini_client, GeminiClientNotInitializedError
from app.services.session_manager import get_gemini_chat_manager
from app.utils.files import materialize_files, parse_gemini_call

router = APIRouter()


@router.post("/gemini")
async def gemini_generate(request: Request):
    try:
        gemini_client = get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    call = await parse_gemini_call(request)
    try:
        async with materialize_files(call.files) as files:
            response = await gemini_client.generate_content(
                call.message, call.model, files=files, gem=call.gem
            )
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Error in /gemini endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generating content: {str(e)}")


@router.post("/gemini-chat")
async def gemini_chat(request: Request):
    try:
        get_gemini_client()
    except GeminiClientNotInitializedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    session_manager = get_gemini_chat_manager()
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
        logger.error(f"Error in /gemini-chat endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error in chat: {str(e)}")
