"""Fast acknowledgement for browser-triggered collection, without model/Ozon writes."""
from pathlib import Path
import threading
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from collector.ingest import CaptureValidationError
from collector.jobs import CaptureJobs


class CaptureRequest(BaseModel):
    request_id: str = Field(min_length=16, max_length=80)
    capture: dict
    allow_new_version: bool = False


def create_router(products_root: Path, db_path: Path):
    router = APIRouter(prefix="/api/collector/jobs")
    instances = {}
    instance_lock = threading.Lock()

    def current_jobs():
        root = Path(products_root() if callable(products_root) else products_root)
        database = Path(db_path() if callable(db_path) else db_path)
        key = (str(root.resolve()), str(database.resolve()))
        with instance_lock:
            if key not in instances:
                instances[key] = CaptureJobs(root, database)
            return instances[key]

    @router.on_event("startup")
    def resume_unstarted_captures():
        current_jobs().initialize()

    @router.post("", status_code=202)
    def submit(request: CaptureRequest, x_factory_device_id: str = Header(default="")):
        try:
            return current_jobs().enqueue(request.request_id, x_factory_device_id, request.capture, request.allow_new_version)
        except CaptureValidationError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/{request_id}")
    def read(request_id: str, x_factory_device_id: str = Header(default="")):
        try:
            return current_jobs().read(request_id, x_factory_device_id)
        except KeyError as error:
            raise HTTPException(404, "采集任务不存在") from error
        except CaptureValidationError as error:
            raise HTTPException(422, str(error)) from error

    return router
