import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")

# Load .env so ASR_BACKEND is available at engine-load time
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
except ImportError:
    pass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from whisperlivekit import TranscriptionEngine, AudioProcessor
import whisperlivekit.core
import asyncio
import logging

logger = logging.getLogger("asr")

router = APIRouter()
_engine = None
_engine_lock = asyncio.Lock()
_engine_ready = asyncio.Event()

_ENGINE_KWARGS = dict(
    model_size="large-v3",
    lan="zh",
    pcm_input=True,
    backend_policy="localagreement",
    buffer_trimming="segment",
    buffer_trimming_sec=60,
    no_vac=True,
)


_ZH_PROMPT = "以下是普通话的句子，使用简体中文。"


def _load_engine():
    """同步加载模型，在线程池中调用避免阻塞 event loop。"""
    # 重置单例，确保每次创建新引擎
    whisperlivekit.core.TranscriptionEngine._instance = None
    whisperlivekit.core.TranscriptionEngine._initialized = False
    kwargs = {**_ENGINE_KWARGS, "backend": os.environ.get("ASR_BACKEND", "faster-whisper")}
    engine = TranscriptionEngine(**kwargs)
    # 包装 transcribe，自动注入简体中文引导 prompt
    _orig_transcribe = engine.asr.transcribe

    def _transcribe_zh(audio, init_prompt=""):
        if not init_prompt:
            init_prompt = _ZH_PROMPT
        elif not init_prompt.startswith(_ZH_PROMPT):
            init_prompt = _ZH_PROMPT + init_prompt
        return _orig_transcribe(audio, init_prompt=init_prompt)

    engine.asr.transcribe = _transcribe_zh
    return engine


async def preload_engine():
    """服务启动时预热模型，避免首次使用时等待。"""
    global _engine
    async with _engine_lock:
        if _engine is not None:
            return
        loop = asyncio.get_event_loop()
        _engine = await loop.run_in_executor(None, _load_engine)
    _engine_ready.set()


async def get_engine():
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        loop = asyncio.get_event_loop()
        _engine = await loop.run_in_executor(None, _load_engine)
        _engine_ready.set()
    return _engine


@router.websocket("/asr")
async def asr_endpoint(websocket: WebSocket, token: str = None):
    # 握手阶段校验 JWT token（通过 query param 传入）
    from app.services.auth_service import decode_token
    from app.database import SessionLocal
    from app.models.user import User as _User

    if not token:
        await websocket.close(code=4401, reason="Missing token")
        return

    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4401, reason="Invalid or expired token")
        return

    db = SessionLocal()
    try:
        user = db.get(_User, int(payload.get("sub", 0)))
        if not user or not user.is_active:
            await websocket.close(code=4403, reason="User not found or inactive")
            return
    finally:
        db.close()

    await websocket.accept()

    # 等引擎加载完毕，最多等 120 秒
    try:
        await asyncio.wait_for(_engine_ready.wait(), timeout=120)
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "error": "模型加载超时，请稍后重试"})
        await websocket.close()
        return

    engine = await get_engine()
    audio_processor = AudioProcessor(transcription_engine=engine)
    results_generator = await audio_processor.create_tasks()

    async def send_results():
        try:
            async for response in results_generator:
                try:
                    d = response.to_dict()
                    logger.info("send_json: %s", d)
                    await websocket.send_json(d)
                except Exception:
                    break
            try:
                await websocket.send_json({"type": "ready_to_stop"})
            except Exception:
                pass
        except Exception:
            pass

    _total_bytes = 0
    send_task = asyncio.create_task(send_results())
    try:
        while True:
            data = await websocket.receive_bytes()
            _total_bytes += len(data)
            if _total_bytes % 32000 < len(data):
                logger.info("received audio total=%d bytes", _total_bytes)
            await audio_processor.process_audio(data)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        send_task.cancel()
        try:
            await send_task
        except (asyncio.CancelledError, Exception):
            pass
