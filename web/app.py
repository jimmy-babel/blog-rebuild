from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator

WEB_ROOT = Path(__file__).resolve().parent
load_dotenv(WEB_ROOT / ".env")

from database import TaskStore, now_iso
from website_pipeline import BLOG_ROOT, PipelineError, run_pipeline


DB = TaskStore(WEB_ROOT / "data" / "tasks.db")
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT_TASKS", "2")))
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)
RUNNING: dict[str, asyncio.Task] = {}


class TaskCreate(BaseModel):
    url: str
    image_mode: Literal["copy", "rebuild"] = "copy"
    caption_mode: Literal["ai", "local"] = "ai"
    brand: Literal["on", "off"] | None = None
    brand_reference: Literal["on", "off"] | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("https://mp.weixin.qq.com/s/"):
            raise ValueError("仅支持 https://mp.weixin.qq.com/s/... 格式的文章链接")
        return value

    @model_validator(mode="after")
    def normalize_brand_aliases(self) -> "TaskCreate":
        if self.brand is not None and self.brand_reference is not None and self.brand != self.brand_reference:
            raise ValueError("brand 与 brand_reference 的值必须一致")
        self.brand = self.brand or self.brand_reference or "on"
        return self


def public_task(task: dict | None) -> dict | None:
    if not task:
        return None
    result = dict(task)
    result["brand"] = result.get("brand_reference") or "on"
    if result.get("html_file"):
        result["preview_url"] = f"/results/{result['id']}/{Path(result['html_file']).name}"
    return result


async def execute_task(task_id: str) -> None:
    async with SEMAPHORE:
        task = DB.get(task_id)
        if not task:
            return
        DB.update(task_id, status="processing", progress="开始处理", started_at=now_iso())

        def progress(message: str) -> None:
            DB.update(task_id, progress=message)

        try:
            result = await asyncio.to_thread(
                run_pipeline, task_id, task["url"], task["image_mode"], task["caption_mode"], progress, task["brand_reference"]
            )
            DB.update(
                task_id, status="completed", title=result["title"], progress="处理完成",
                result_dir=result["result_dir"], html_file=result["html_file"], finished_at=now_iso(),
            )
        except Exception as exc:
            DB.update(task_id, status="failed", progress="处理失败", error=str(exc), finished_at=now_iso())


def schedule(task_id: str) -> None:
    task = asyncio.create_task(execute_task(task_id))
    RUNNING[task_id] = task

    def done(_: asyncio.Task) -> None:
        RUNNING.pop(task_id, None)

    task.add_done_callback(done)


@asynccontextmanager
async def lifespan(_: FastAPI):
    DB.recover_on_start()
    queued, _ = DB.list(status="queued", page=1, page_size=100)
    for task in queued:
        schedule(task["id"])
    yield
    for task in RUNNING.values():
        task.cancel()


app = FastAPI(title="Blog Article Studio", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB_ROOT / "static"), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict:
    n8n = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/wechat-article-extractor")
    return {
        "ok": True, "database": str(DB.path), "n8n_webhook": n8n,
        "api_key_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "text_model": os.getenv("TEXT_MODEL", "gpt-5.6-luna"),
        "image_model": os.getenv("IMAGE_MODEL", "gpt-image-2"),
        "max_concurrent_tasks": MAX_CONCURRENT,
    }


@app.post("/api/tasks", status_code=202)
async def create_task(request: TaskCreate) -> dict:
    task_id = uuid.uuid4().hex
    task = DB.create(task_id, request.url, request.image_mode, request.caption_mode, request.brand)
    schedule(task_id)
    return public_task(task)


@app.get("/api/tasks")
async def list_tasks(
    q: str = Query(""), status: Literal["", "queued", "processing", "completed", "failed"] = "",
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
) -> dict:
    tasks, total = DB.list(q, status, page, page_size)
    return {"items": [public_task(task) for task in tasks], "total": total, "page": page, "page_size": page_size}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = DB.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return public_task(task)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> dict:
    task = DB.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] == "processing":
        raise HTTPException(409, "处理中任务不能删除")
    running = RUNNING.pop(task_id, None)
    if running:
        running.cancel()
    deleted = DB.delete(task_id)
    work_dir = BLOG_ROOT / ".recreate-work" / task_id
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    if task.get("result_dir"):
        result_dir = Path(task["result_dir"]).resolve()
        result_root = (BLOG_ROOT / "result").resolve()
        if result_dir != result_root and result_root in result_dir.parents and result_dir.exists():
            shutil.rmtree(result_dir, ignore_errors=True)
    return {"deleted": bool(deleted), "id": task_id}


@app.get("/results/{task_id}/{file_path:path}")
async def result_file(task_id: str, file_path: str) -> FileResponse:
    task = DB.get(task_id)
    if not task or not task.get("result_dir"):
        raise HTTPException(404, "结果不存在")
    root = Path(task["result_dir"]).resolve()
    target = (root / file_path).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(403, "非法路径")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)
