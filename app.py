from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ultralytics import YOLO


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MODEL_NAME = os.getenv("YOLO_MODEL", "yolo11s.pt")
CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.40"))
IMAGE_SIZE = int(os.getenv("YOLO_IMAGE_SIZE", "960"))
EMPTY_SECONDS = int(os.getenv("EMPTY_CONFIRM_SECONDS", "10"))
MIN_EMPTY_SAMPLES = int(os.getenv("MIN_EMPTY_SAMPLES", "3"))
FRAME_MIN_INTERVAL = float(os.getenv("FRAME_MIN_INTERVAL", "1.0"))
DEVICE_OFF_INTERVAL = float(os.getenv("DEVICE_OFF_INTERVAL", "1.0"))
DEVICE_LABELS = {"light": "灯光", "fan": "风扇", "ac": "空调", "board": "班班通"}

# 网页端是多人共用的（云上尤其如此），所以设备状态、无人计时、运行日志
# 必须按访问者隔离，否则一个人开摄像头会让别人的页面跟着关设备。
SESSION_COOKIE = "aied_sid"
SESSION_TTL = float(os.getenv("SESSION_TTL_SECONDS", "1800"))
# 公开部署时，不带 cookie 的请求（健康检查、爬虫、扫描器）也会各自建会话，
# 所以除了按闲置时长清理，还要有个硬上限兜底，防止字典无限增长。
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))

# 模型只有一份，加载一次即可；推理锁也全局共享，避免并发跑爆内存。
MODEL: YOLO | None = None
MODEL_LOCK = Lock()


class Runtime:
    """单个访问者的会话状态。"""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.state_lock = asyncio.Lock()
        self.empty_since: float | None = None
        self.empty_samples = 0
        self.last_control_frame_at = 0.0
        self.status = "unknown"
        self.shutdown_task: asyncio.Task | None = None
        self.devices = {"light": True, "fan": True, "ac": True, "board": True}
        self.events: list[dict] = []
        self.touched = time.time()
        self.log(f"模型 {MODEL_NAME} 已就绪，等待画面", "success")

    def log(self, message: str, level: str = "info") -> None:
        self.events.append({"time": time.time(), "message": message, "level": level})
        self.events = self.events[-100:]


SESSIONS: dict[str, Runtime] = {}


def drop_session(sid: str) -> None:
    rt = SESSIONS.pop(sid, None)
    if rt and rt.shutdown_task and not rt.shutdown_task.done():
        rt.shutdown_task.cancel()


def evict_sessions() -> None:
    """先清闲置超时的会话；仍然超量就按最久未访问淘汰一半。"""
    now = time.time()
    for sid, rt in list(SESSIONS.items()):
        if now - rt.touched > SESSION_TTL:
            drop_session(sid)
    if len(SESSIONS) >= MAX_SESSIONS:
        victims = sorted(SESSIONS.items(), key=lambda kv: kv[1].touched)
        for sid, _ in victims[: len(victims) // 2 + 1]:
            drop_session(sid)


def get_runtime(request: Request) -> Runtime:
    sid: str = request.state.sid
    rt = SESSIONS.get(sid)
    if rt is None:
        evict_sessions()
        rt = Runtime(sid)
        SESSIONS[sid] = rt
    rt.touched = time.time()
    return rt


@asynccontextmanager
async def lifespan(_: FastAPI):
    global MODEL
    MODEL = await asyncio.to_thread(YOLO, MODEL_NAME)
    yield
    for rt in SESSIONS.values():
        if rt.shutdown_task and not rt.shutdown_task.done():
            rt.shutdown_task.cancel()


app = FastAPI(title="教室 YOLO 人员检测与节能控制", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def attach_session(request: Request, call_next):
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid or not sid.isascii() or len(sid) > 64:
        sid = secrets.token_urlsafe(16)
    request.state.sid = sid
    response = await call_next(request)
    response.set_cookie(SESSION_COOKIE, sid, max_age=int(SESSION_TTL), samesite="lax")
    return response


class DeviceUpdate(BaseModel):
    device: str
    on: bool


def public_state(rt: Runtime, now: float | None = None) -> dict:
    now = now or time.time()
    elapsed = 0 if rt.empty_since is None else max(0, now - rt.empty_since)
    return {
        "occupancy": rt.status,
        "emptyElapsed": round(elapsed, 1),
        "emptyRequired": EMPTY_SECONDS,
        "emptySamples": rt.empty_samples,
        "minEmptySamples": MIN_EMPTY_SAMPLES,
        "devices": rt.devices.copy(),
        "shutdownPending": bool(rt.shutdown_task and not rt.shutdown_task.done()),
        "events": rt.events[-20:],
    }


async def cancel_shutdown(rt: Runtime) -> None:
    task = rt.shutdown_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        rt.log("检测到人员，已取消待执行的关闭程序", "warn")
    rt.shutdown_task = None


async def shutdown_sequence(rt: Runtime) -> None:
    try:
        for device in ("ac", "fan", "light", "board"):
            async with rt.state_lock:
                if rt.status != "empty":
                    return
                if rt.devices[device]:
                    rt.devices[device] = False
                    rt.log(f"{DEVICE_LABELS[device]} 已关闭（模拟设备适配器）", "success")
            await asyncio.sleep(DEVICE_OFF_INTERVAL)
        rt.log("无人节能关闭程序执行完成", "success")
    except asyncio.CancelledError:
        raise


async def apply_policy(rt: Runtime, has_person: bool, allow_control: bool) -> dict:
    now = time.time()
    # 普通上传只做识别展示，绝不改变实时教室状态或设备控制流程。
    if not allow_control:
        return public_state(rt, now)
    async with rt.state_lock:
        if has_person:
            rt.status = "occupied"
            rt.empty_since = None
            rt.empty_samples = 0
        elif now - rt.last_control_frame_at < FRAME_MIN_INTERVAL:
            return public_state(rt, now)
        else:
            rt.last_control_frame_at = now
            rt.status = "checking_empty"
            rt.empty_samples += 1
            if rt.empty_since is None:
                rt.empty_since = now
                rt.log("开始持续无人确认；设备保持原状态", "warn")

    if has_person:
        await cancel_shutdown(rt)
    elif allow_control:
        async with rt.state_lock:
            elapsed = now - (rt.empty_since or now)
            confirmed = elapsed >= EMPTY_SECONDS and rt.empty_samples >= MIN_EMPTY_SAMPLES
            if confirmed:
                rt.status = "empty"
                if any(rt.devices.values()) and not (
                    rt.shutdown_task and not rt.shutdown_task.done()
                ):
                    rt.log("持续无人条件满足，进入设备关闭程序", "success")
                    rt.shutdown_task = asyncio.create_task(shutdown_sequence(rt))
    return public_state(rt, now)


def run_detection(image: np.ndarray) -> tuple[list[dict], np.ndarray]:
    if MODEL is None:
        raise RuntimeError("模型尚未加载")
    with MODEL_LOCK:
        result = MODEL.predict(
            source=image,
            classes=[0],
            conf=CONFIDENCE,
            imgsz=IMAGE_SIZE,
            max_det=100,
            verbose=False,
        )[0]

    detections: list[dict] = []
    if result.boxes is not None:
        boxes = result.boxes.xyxy.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy()
        for box, score in zip(boxes, scores):
            x1, y1, x2, y2 = [float(v) for v in box]
            detections.append(
                {"class": "person", "score": float(score), "bbox": [x1, y1, x2 - x1, y2 - y1]}
            )
    return detections, result.plot(labels=True, conf=True)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request):
    return {
        "ok": MODEL is not None,
        "model": MODEL_NAME,
        "confidence": CONFIDENCE,
        "imageSize": IMAGE_SIZE,
        **public_state(get_runtime(request)),
    }


@app.get("/api/state")
async def state(request: Request):
    return public_state(get_runtime(request))


@app.post("/api/devices")
async def update_device(update: DeviceUpdate, request: Request):
    rt = get_runtime(request)
    if update.device not in rt.devices:
        raise HTTPException(400, "未知设备")
    async with rt.state_lock:
        rt.devices[update.device] = update.on
        rt.log(f"用户将 {DEVICE_LABELS[update.device]} 设置为{'开启' if update.on else '关闭'}")
    return public_state(rt)


@app.post("/api/detect")
async def detect(request: Request, file: UploadFile = File(...), allow_control: bool = Form(False)):
    rt = get_runtime(request)
    data = await file.read()
    if not data or len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "图片为空或超过20MB")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(400, "无法解析图片")
    try:
        detections, annotated = await asyncio.to_thread(run_detection, image)
    except Exception as exc:
        rt.log(f"推理失败：{exc}", "error")
        raise HTTPException(500, f"推理失败：{exc}") from exc

    has_person = bool(detections)
    policy = await apply_policy(rt, has_person, allow_control)
    ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])
    annotated_b64 = base64.b64encode(encoded).decode("ascii") if ok else None
    return {
        "hasPerson": has_person,
        "count": len(detections),
        "bestScore": max((d["score"] for d in detections), default=0),
        "detections": detections,
        "annotatedImage": f"data:image/jpeg;base64,{annotated_b64}" if annotated_b64 else None,
        "controlFrame": allow_control,
        "policy": policy,
    }
