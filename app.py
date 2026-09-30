from __future__ import annotations

import asyncio
import base64
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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


class Runtime:
    def __init__(self) -> None:
        self.model: YOLO | None = None
        self.model_lock = Lock()
        self.state_lock = asyncio.Lock()
        self.empty_since: float | None = None
        self.empty_samples = 0
        self.last_control_frame_at = 0.0
        self.status = "unknown"
        self.shutdown_task: asyncio.Task | None = None
        self.devices = {"light": True, "fan": True, "ac": True, "board": True}
        self.events: list[dict] = []

    def log(self, message: str, level: str = "info") -> None:
        self.events.append({"time": time.time(), "message": message, "level": level})
        self.events = self.events[-100:]


runtime = Runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime.log(f"正在加载模型 {MODEL_NAME}")
    runtime.model = await asyncio.to_thread(YOLO, MODEL_NAME)
    runtime.log(f"模型 {MODEL_NAME} 加载完成", "success")
    yield
    if runtime.shutdown_task and not runtime.shutdown_task.done():
        runtime.shutdown_task.cancel()


app = FastAPI(title="教室 YOLO 人员检测与节能控制", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DeviceUpdate(BaseModel):
    device: str
    on: bool


def public_state(now: float | None = None) -> dict:
    now = now or time.time()
    elapsed = 0 if runtime.empty_since is None else max(0, now - runtime.empty_since)
    return {
        "occupancy": runtime.status,
        "emptyElapsed": round(elapsed, 1),
        "emptyRequired": EMPTY_SECONDS,
        "emptySamples": runtime.empty_samples,
        "minEmptySamples": MIN_EMPTY_SAMPLES,
        "devices": runtime.devices.copy(),
        "shutdownPending": bool(runtime.shutdown_task and not runtime.shutdown_task.done()),
        "events": runtime.events[-20:],
    }


async def cancel_shutdown() -> None:
    task = runtime.shutdown_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        runtime.log("检测到人员，已取消待执行的关闭程序", "warn")
    runtime.shutdown_task = None


async def shutdown_sequence() -> None:
    try:
        for device in ("ac", "fan", "light", "board"):
            async with runtime.state_lock:
                if runtime.status != "empty":
                    return
                if runtime.devices[device]:
                    runtime.devices[device] = False
                    runtime.log(f"{DEVICE_LABELS[device]} 已关闭（模拟设备适配器）", "success")
            await asyncio.sleep(DEVICE_OFF_INTERVAL)
        runtime.log("无人节能关闭程序执行完成", "success")
    except asyncio.CancelledError:
        raise


async def apply_policy(has_person: bool, allow_control: bool) -> dict:
    now = time.time()
    # 普通上传只做识别展示，绝不改变实时教室状态或设备控制流程。
    if not allow_control:
        return public_state(now)
    async with runtime.state_lock:
        if has_person:
            runtime.status = "occupied"
            runtime.empty_since = None
            runtime.empty_samples = 0
        elif now - runtime.last_control_frame_at < FRAME_MIN_INTERVAL:
            return public_state(now)
        else:
            runtime.last_control_frame_at = now
            runtime.status = "checking_empty"
            runtime.empty_samples += 1
            if runtime.empty_since is None:
                runtime.empty_since = now
                runtime.log("开始持续无人确认；设备保持原状态", "warn")

    if has_person:
        await cancel_shutdown()
    elif allow_control:
        async with runtime.state_lock:
            elapsed = now - (runtime.empty_since or now)
            confirmed = elapsed >= EMPTY_SECONDS and runtime.empty_samples >= MIN_EMPTY_SAMPLES
            if confirmed:
                runtime.status = "empty"
                if any(runtime.devices.values()) and not (
                    runtime.shutdown_task and not runtime.shutdown_task.done()
                ):
                    runtime.log("持续无人条件满足，进入设备关闭程序", "success")
                    runtime.shutdown_task = asyncio.create_task(shutdown_sequence())
    return public_state(now)


def run_detection(image: np.ndarray) -> tuple[list[dict], np.ndarray]:
    if runtime.model is None:
        raise RuntimeError("模型尚未加载")
    with runtime.model_lock:
        result = runtime.model.predict(
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
async def health():
    return {
        "ok": runtime.model is not None,
        "model": MODEL_NAME,
        "confidence": CONFIDENCE,
        "imageSize": IMAGE_SIZE,
        **public_state(),
    }


@app.get("/api/state")
async def state():
    return public_state()


@app.post("/api/devices")
async def update_device(update: DeviceUpdate):
    if update.device not in runtime.devices:
        raise HTTPException(400, "未知设备")
    async with runtime.state_lock:
        runtime.devices[update.device] = update.on
        runtime.log(f"用户将 {DEVICE_LABELS[update.device]} 设置为{'开启' if update.on else '关闭'}")
    return public_state()


@app.post("/api/detect")
async def detect(file: UploadFile = File(...), allow_control: bool = Form(False)):
    data = await file.read()
    if not data or len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "图片为空或超过20MB")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(400, "无法解析图片")
    try:
        detections, annotated = await asyncio.to_thread(run_detection, image)
    except Exception as exc:
        runtime.log(f"推理失败：{exc}", "error")
        raise HTTPException(500, f"推理失败：{exc}") from exc

    has_person = bool(detections)
    policy = await apply_policy(has_person, allow_control)
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
