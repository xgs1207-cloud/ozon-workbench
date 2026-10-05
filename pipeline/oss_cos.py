"""对象存储适配器（腾讯云 COS）：把生成的图片传到 COS 并写出公网地址。

用户的选择：**图片存腾讯云 COS**。这里用**官方 SDK**（`cos-python-sdk-v5`）签名 ——
官方文档明确推荐 SDK 签名，自己实现 v5 签名算法容易"看起来对、实际被拒"，不值得冒险。

要点：

- **不猜域名**：公网前缀默认 `<bucket>.cos.<region>.myqcloud.com`，也可用 `COS_PUBLIC_BASE_URL`
  指向 CDN/自定义域名；写出的 `output/image-public-urls.json` 必须全是 https（Ozon 要能抓取）；
- **增量同步**：先 `head_object` 比对大小，相同就跳过（Content-MD5/ETag 由 SDK 处理）；
- **`--check` 是真正的验证**：用一个探针对象走 PUT → 匿名 GET（模拟 Ozon 抓取）→ DELETE，
  一次把"密钥对不对 / bucket 对不对 / 是否公有读"全验掉；不通过就说明 Ozon 也抓不到；
- 凭据只从环境变量读：`COS_SECRET_ID`、`COS_SECRET_KEY`（可选 `COS_TOKEN` 临时密钥）、
  `COS_BUCKET`、`COS_REGION`、`COS_KEY_PREFIX`、`COS_PUBLIC_BASE_URL`。

⚠️ 本模块的签名/上传路径**没有用真实密钥验证过**（我没有你的 COS 凭据）：
拿到凭据后先跑 `python -m pipeline.oss_cos --check`，再跑正式同步。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

SCHEMA_VERSION = "1.0.0"
URLS_FILE = "output/image-public-urls.json"
PLAN_FILE = "output/image-plan.json"
DEFAULT_KEY_PREFIX = "ozon-images"
DEFAULT_LAYOUT = "{product_id}/{slot}.png"
CHECK_KEY = "_ozon-workbench-check.txt"
CHECK_BODY = b"ozon-workbench connectivity check\n"
REQUIRED_ENV = ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION")

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class CosError(RuntimeError):
    """COS 配置或调用失败。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def content_type_for(path: Path | str) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def planned_slots(product_dir: Path | str) -> list[dict[str, Any]]:
    """读图片计划里的槽位（主图 + 详情图）。"""
    directory = Path(product_dir)
    plan = _read_json(directory / PLAN_FILE)
    rows: list[dict[str, Any]] = []
    for item in list(plan.get("main_images") or []) + list(plan.get("detail_images") or []):
        if not isinstance(item, Mapping):
            continue
        slot = str(item.get("slot") or "").strip()
        relative = str(item.get("output_path") or "").strip()
        if slot and relative:
            rows.append(
                {
                    "slot": slot,
                    "output_path": relative,
                    "role": "variant_main" if slot.startswith("main-") else "detail",
                }
            )
    return rows


def build_client(config: Mapping[str, Any]) -> Any:
    """按配置建官方 COS 客户端（延迟导入：没装 SDK 时给出清晰提示）。"""
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as error:  # pragma: no cover - 环境里已装
        raise CosError(
            "缺少 cos-python-sdk-v5：pip install cos-python-sdk-v5（官方 SDK，用它的签名）"
        ) from error
    kwargs: dict[str, Any] = {
        "Region": config["region"],
        "SecretId": config["secret_id"],
        "SecretKey": config["secret_key"],
        "Scheme": config.get("scheme", "https"),
    }
    if config.get("token"):
        kwargs["Token"] = config["token"]
    if config.get("timeout"):
        kwargs["Timeout"] = int(config["timeout"])
    return CosS3Client(CosConfig(**kwargs))


def config_from_env(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = env if env is not None else os.environ
    missing = [key for key in REQUIRED_ENV if not str(source.get(key) or "").strip()]
    if missing:
        raise CosError(
            "COS 配置不完整，缺少环境变量：" + ", ".join(missing)
            + "（示例：COS_SECRET_ID=AKIDxxx COS_SECRET_KEY=xxx COS_BUCKET=my-bucket-1250000000 "
            "COS_REGION=ap-hongkong COS_KEY_PREFIX=ozon-images）"
        )
    return {
        "secret_id": str(source["COS_SECRET_ID"]).strip(),
        "secret_key": str(source["COS_SECRET_KEY"]).strip(),
        "bucket": str(source["COS_BUCKET"]).strip(),
        "region": str(source["COS_REGION"]).strip(),
        "token": str(source.get("COS_TOKEN") or "").strip() or None,
        "key_prefix": str(source.get("COS_KEY_PREFIX") or DEFAULT_KEY_PREFIX).strip().strip("/"),
        "public_base_url": str(source.get("COS_PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        "scheme": str(source.get("COS_SCHEME") or "https").strip(),
        "timeout": str(source.get("COS_TIMEOUT") or "").strip() or None,
    }


class CosObjectStorage:
    """把商品图片上传到 COS，并写出 slot → https URL 映射。"""

    name = "tencent-cos"

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        region: str,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        layout: str = DEFAULT_LAYOUT,
        public_base_url: str | None = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if not bucket or not region:
            raise CosError("bucket 与 region 必填")
        self.client = client
        self.bucket = bucket
        self.region = region
        self.key_prefix = str(key_prefix or "").strip("/")
        self.layout = layout or DEFAULT_LAYOUT
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.dry_run = dry_run
        self.sleep = sleep
        self.max_attempts = max(1, int(max_attempts))

    # ---------------------------------------------------------------- 键与 URL

    def key_for(self, product_id: str, slot: str) -> str:
        relative = self.layout.format(product_id=product_id, slot=slot).lstrip("/")
        return f"{self.key_prefix}/{relative}" if self.key_prefix else relative

    def url_for(self, product_id: str, slot: str) -> str:
        key = self.key_for(product_id, slot)
        if self.public_base_url:
            return f"{self.public_base_url}/{quote(key)}"
        return f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{quote(key)}"

    # ---------------------------------------------------------------- 上传

    def _call(self, action: str, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return getattr(self.client, action)(**kwargs)
            except Exception as error:  # noqa: BLE001 - SDK 的异常类型很多，统一处理
                last = error
                message = str(error)
                retryable = any(token in message for token in ("-1", "429", "500", "502", "503", "504", "RequestTimeout", "timeout"))
                if not retryable or attempt >= self.max_attempts:
                    break
                self.sleep(1.0 * attempt)
        raise CosError(f"COS {action} 失败：{last}") from last

    def publish_slot(self, product_dir: Path, row: Mapping[str, Any]) -> dict[str, Any]:
        product_id = product_dir.name
        slot = str(row["slot"])
        source = product_dir / str(row["output_path"])
        key = self.key_for(product_id, slot)
        if not source.is_file():
            return {"slot": slot, "status": "missing", "reason": f"本地文件不存在：{row['output_path']}"}

        size = source.stat().st_size
        try:
            head = self._call("head_object", Bucket=self.bucket, Key=key)
            remote_size = int(head.get("Content-Length") or head.get("content-length") or -1)
        except CosError:
            remote_size = -1
        if remote_size == size:
            return {"slot": slot, "status": "unchanged", "key": key, "url": self.url_for(product_id, slot)}
        if self.dry_run:
            return {"slot": slot, "status": "would_upload", "key": key, "url": self.url_for(product_id, slot)}

        body = source.read_bytes()
        self._call(
            "put_object",
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type_for(source),
            CacheControl="public, max-age=2592000",
        )
        return {
            "slot": slot,
            "status": "uploaded",
            "key": key,
            "bytes": len(body),
            "url": self.url_for(product_id, slot),
        }

    def publish_product(
        self,
        product_dir: Path | str,
        *,
        slots: Sequence[str] | None = None,
        write_urls: bool = True,
    ) -> dict[str, Any]:
        directory = Path(product_dir)
        rows = planned_slots(directory)
        if not rows:
            raise CosError(f"缺少图片计划或计划里没有槽位：{directory / PLAN_FILE}")
        if slots:
            wanted = {str(item) for item in slots}
            rows = [row for row in rows if row["slot"] in wanted]

        results = [self.publish_slot(directory, row) for row in rows]
        ok = [item for item in results if item["status"] in {"uploaded", "unchanged"}]
        urls = {item["slot"]: item["url"] for item in ok if item.get("url")}

        written = None
        if write_urls and urls and not self.dry_run:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "product_id": directory.name,
                "storage": self.name,
                "bucket": self.bucket,
                "region": self.region,
                "key_prefix": self.key_prefix,
                "layout": self.layout,
                "published_at": now_iso(),
                "note": "腾讯云 COS（官方 SDK 签名）；上传载荷只接受 https 地址",
                "urls": urls,
                "skipped_slots": [item["slot"] for item in results if item["status"] not in {"uploaded", "unchanged"}],
            }
            path = directory / URLS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written = str(path)

        return {
            "product_id": directory.name,
            "storage": self.name,
            "bucket": self.bucket,
            "region": self.region,
            "dry_run": self.dry_run,
            "slots": len(rows),
            "uploaded": len([item for item in results if item["status"] == "uploaded"]),
            "unchanged": len([item for item in results if item["status"] == "unchanged"]),
            "missing": [item for item in results if item["status"] == "missing"],
            "urls": urls,
            "urls_file": written,
            "results": results,
            "https_ok": all(str(url).startswith("https://") for url in urls.values()) if urls else False,
        }

    # ---------------------------------------------------------------- 连通性自检

    def check(self, *, anonymous_get: bool = True, urlopen: Any | None = None) -> dict[str, Any]:
        """PUT 探针 → （模拟 Ozon）匿名 GET → DELETE，验证密钥/bucket/公有读。"""
        key = f"{self.key_prefix}/{CHECK_KEY}" if self.key_prefix else CHECK_KEY
        self._call(
            "put_object",
            Bucket=self.bucket,
            Key=key,
            Body=CHECK_BODY,
            ContentType="text/plain",
        )
        report: dict[str, Any] = {
            "put": "ok",
            "key": key,
            "bucket": self.bucket,
            "region": self.region,
            "url": f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{quote(key)}",
        }
        if anonymous_get:
            import urllib.error
            import urllib.request

            opener = urlopen or urllib.request.urlopen
            try:
                with opener(report["url"], timeout=15) as response:
                    body = response.read()
                report["anonymous_get"] = "ok" if body.strip() == CHECK_BODY.strip() else "content_mismatch"
            except urllib.error.HTTPError as error:
                report["anonymous_get"] = f"http_{error.code}"
                report["hint"] = (
                    "403 = 对象未公开读：把 bucket/前缀设为公有读，否则 Ozon 抓不到图片"
                    if error.code in (401, 403)
                    else f"HTTP {error.code}"
                )
            except Exception as error:  # noqa: BLE001
                report["anonymous_get"] = f"error: {error}"
        try:
            self._call("delete_object", Bucket=self.bucket, Key=key)
            report["delete"] = "ok"
        except CosError as error:
            report["delete"] = f"failed: {error}"
        report["ok"] = report.get("put") == "ok" and report.get("anonymous_get", "ok") == "ok"
        return report


# --------------------------------------------------------------------- CLI


def _storage_from_env(*, dry_run: bool = False) -> CosObjectStorage:
    config = config_from_env()
    return CosObjectStorage(
        build_client(config),
        bucket=config["bucket"],
        region=config["region"],
        key_prefix=config["key_prefix"],
        public_base_url=config["public_base_url"],
        dry_run=dry_run,
    )


def probe_public_url(
    url: str,
    *,
    urlopen: Any | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """匿名抓一个地址，判断"Ozon 能不能抓到"（**不需要任何密钥**）。

    用来在填密钥之前就把桶的公开读权限调好：403 就是没放开，别等传完图才发现。
    """
    import urllib.error
    import urllib.request

    opener = urlopen or urllib.request.urlopen
    report: dict[str, Any] = {"url": url, "anonymous_get": None, "ok": False}
    request = urllib.request.Request(url, headers={"User-Agent": "ozon-workbench/1.0 (public-read probe)"})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(64)
            report["anonymous_get"] = f"HTTP {getattr(response, 'status', 200)}"
            report["content_prefix"] = body[:8].hex()
            report["looks_like_image"] = body[:4] in (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")
            report["ok"] = True
    except urllib.error.HTTPError as error:
        report["anonymous_get"] = f"HTTP {error.code}"
        if error.code in (401, 403):
            report["hint"] = (
                "不是公有读，Ozon 抓不到。检查：① COS 桶「安全管理 → 阻止公共访问」是否关闭"
                "（它会压过公开读策略）；② 存储桶策略里「所有用户 → 读操作」的资源前缀是否覆盖该对象键；"
                "③ 或直接把桶访问权限设为「公有读私有写」。"
            )
        elif error.code == 404:
            report["hint"] = "密钥/权限没问题，但这个对象键不存在（先上传，或键前缀写错了）"
        else:
            report["hint"] = f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        report["anonymous_get"] = f"error: {error}"
        report["hint"] = "连不上：检查网络或域名拼写"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把商品图片上传到腾讯云 COS 并写出公网地址")
    parser.add_argument("--product-dir", default=None, help="商品目录（--check 时可不给）")
    parser.add_argument("--check", action="store_true", help="连通性自检：PUT + 匿名 GET + DELETE（需要密钥）")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="只做匿名可读性探测（**不需要密钥**）：给 --url 或 --key",
    )
    parser.add_argument("--url", default=None, help="--probe 用：完整 https 地址")
    parser.add_argument("--key", default=None, help="--probe 用：对象键（自动拼成 https 地址）")
    parser.add_argument("--bucket", default=None, help="--probe 用：桶名（默认取 COS_BUCKET）")
    parser.add_argument("--region", default=None, help="--probe 用：地域（默认取 COS_REGION）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slot", action="append", dest="slots")
    parser.add_argument("--key-prefix", default=None, help="覆盖 COS_KEY_PREFIX")
    parser.add_argument("--public-base-url", default=None, help="覆盖 COS_PUBLIC_BASE_URL（CDN 域名）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.probe:
        url = args.url
        if not url:
            if not args.key:
                parser.error("--probe 需要 --url 或 --key")
            bucket = args.bucket or os.environ.get("COS_BUCKET")
            region = args.region or os.environ.get("COS_REGION")
            if not (bucket and region):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "缺少桶名/地域：用 --bucket/--region，或设置 COS_BUCKET/COS_REGION。"
                                "注意：手动敲命令不会自动读 /etc/ozon-workbench.env（那是 systemd 专用的），"
                                "用 `bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --probe --key ...`"
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 1
            url = f"https://{bucket}.cos.{region}.myqcloud.com/{quote(args.key.lstrip('/'))}"
        report = probe_public_url(url)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"{url}\n匿名访问：{report['anonymous_get']}")
            if report.get("ok"):
                print(f"  ✅ 可以被 Ozon 抓到（前 4 字节 {report.get('content_prefix')}，图片={report.get('looks_like_image')}）")
            elif report.get("hint"):
                print(f"  ⚠️ {report['hint']}")
        return 0 if report.get("ok") else 1


    try:
        config = config_from_env()
        if args.key_prefix:
            config["key_prefix"] = str(args.key_prefix).strip("/")
        if args.public_base_url:
            config["public_base_url"] = str(args.public_base_url).rstrip("/")
        storage = CosObjectStorage(
            build_client(config),
            bucket=config["bucket"],
            region=config["region"],
            key_prefix=config["key_prefix"],
            public_base_url=config["public_base_url"],
            dry_run=args.dry_run,
        )
        if args.check:
            report = storage.check()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if not args.product_dir:
            parser.error("同步图片需要 --product-dir；只做连通性检查用 --check")
        summary = storage.publish_product(args.product_dir, slots=args.slots)
    except (CosError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    else:
        mode = "（dry-run）" if summary["dry_run"] else ""
        print(f"{summary['product_id']} → cos://{summary['bucket']}{mode}")
        print(f"  槽位 {summary['slots']}：新上传 {summary['uploaded']}，未变化 {summary['unchanged']}")
        for item in summary["missing"]:
            print(f"  ⚠️ {item['slot']}: {item['reason']}")
        if summary["urls_file"]:
            print(f"  已写出：{summary['urls_file']}")
        if summary["urls"] and not summary["https_ok"]:
            print("  ⚠️ 有非 https 地址：上传门禁只接受 https")
    return 0 if not summary["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
