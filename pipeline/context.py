"""步骤上下文与门禁异常（runner 与 handlers 共用，避免循环导入）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class PipelineGateError(RuntimeError):
    """某一步的硬门禁不通过：会把商品标成 NEEDS_ATTENTION，而不是重试。"""

    def __init__(self, step: str, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason
        self.details = dict(details or {})


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class StepContext:
    product_dir: Path
    step: str
    dry_run: bool = True
    app_mode: str = "development"
    status: Mapping[str, Any] = field(default_factory=dict)
    provider: Any | None = None
    uploader: Any | None = None
    image_generator: Any | None = None
    ozon_client: Any | None = None

    def path(self, relative: str) -> Path:
        return self.product_dir / relative

    def read_json(self, relative: str) -> dict[str, Any]:
        return read_json(self.path(relative))

    def write_json(self, relative: str, payload: Any) -> Path:
        return write_json(self.path(relative), payload)

    def require_file(self, relative: str) -> Path:
        path = self.path(relative)
        if not path.is_file():
            raise PipelineGateError(self.step, f"缺少必需文件：{relative}")
        return path

    def require_json(self, relative: str) -> dict[str, Any]:
        payload = self.read_json(relative)
        if not payload:
            raise PipelineGateError(self.step, f"文件缺失或无法解析：{relative}")
        return payload

    def require_provider(self) -> Any:
        if self.provider is None:
            raise PipelineGateError(
                self.step,
                "未配置模型层：用 run_product(provider=load_provider()) 或设置 MODEL_PROVIDER",
            )
        return self.provider
