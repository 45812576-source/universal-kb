from fastapi import APIRouter, WebSocket
from whisperlivekit import TranscriptionEngine, AudioProcessor
import asyncio

router = APIRouter()
_engine = None


def _load_engine():
    """同步加载模型，在线程池中调用避免阻塞 event loop。"""
    return TranscriptionEngine(model_size="large-v3", lan="zh", pcm_input=True)


async def preload_engine():
    """服务启动时预热模型，避免首次使用时等待。"""
    global _engine
    loop = asyncio.get_event_loop()
    _engine = await loop.run_in_executor(None, _load_engine)


async def get_engine():
    global _engine
    if _engine is None:
        loop = asyncio.get_event_loop()
        _engine = await loop.run_in_executor(None, _load_engine)
    return _engine


@router.websocket("/asr")
async def asr_endpoint(websocket: WebSocket):
    await websocket.accept()
    engine = await get_engine()
    audio_processor = AudioProcessor(transcription_engine=engine)
    results_generator = await audio_processor.create_tasks()

    async def send_results():
        async for response in results_generator:
            await websocket.send_json(response.to_dict())
        await websocket.send_json({"type": "ready_to_stop"})

    asyncio.create_task(send_results())
    try:
        while True:
            data = await websocket.receive_bytes()
            await audio_processor.process_audio(data)
    except Exception:
        pass
