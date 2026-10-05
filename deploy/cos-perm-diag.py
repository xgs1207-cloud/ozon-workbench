"""COS 权限矩阵诊断：用已配置的凭据逐个操作，告诉你"到底缺什么权限"。

为什么需要：腾讯云 COS 的 403 有好几种完全不同的原因，报错都是 ``AccessDenied``：
- 密钥所属账号没有该桶的策略（**所有操作都拒**）；
- 策略的资源前缀没覆盖你要写的路径（**根路径能写、前缀不能写**，或反之）；
- 「阻止公共访问」开着导致匿名读失败（**PUT 能成功但匿名 GET 403**）。

这三种要改的地方完全不同，所以先用这个脚本把范围缩到一个。

用法（服务器上，让它读 env 文件）：

    cd /opt/ozon-workbench
    bash deploy/with-env.sh .venv/bin/python deploy/cos-perm-diag.py

也可以在别的机器上用环境变量跑（凭据只从环境读，脚本里不存密钥、不打印密钥）。
"""

from __future__ import annotations

import os
import sys

PROBE_ROOT = "_ozon-workbench-diag-root.txt"
PROBE_PREFIX = "ozon-images/_ozon-workbench-diag-prefix.txt"


def _client():
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:  # pragma: no cover - 环境里已装
        print("缺少 cos-python-sdk-v5：pip install cos-python-sdk-v5")
        raise SystemExit(2)
    missing = [key for key in ("COS_BUCKET", "COS_REGION", "COS_SECRET_ID", "COS_SECRET_KEY") if not os.environ.get(key)]
    if missing:
        print("缺少环境变量：" + ", ".join(missing))
        print("提示：手动敲命令不会自动读 /etc/ozon-workbench.env，用 `bash deploy/with-env.sh ...` 包一层。")
        raise SystemExit(3)
    return (
        CosS3Client(
            CosConfig(
                Region=os.environ["COS_REGION"],
                SecretId=os.environ["COS_SECRET_ID"],
                SecretKey=os.environ["COS_SECRET_KEY"],
                Scheme=os.environ.get("COS_SCHEME", "https"),
            )
        ),
        os.environ["COS_BUCKET"],
    )


def main() -> int:
    try:
        from qcloud_cos.cos_exception import CosServiceError
    except ImportError:  # pragma: no cover
        print("缺少 cos-python-sdk-v5")
        return 2

    client, bucket = _client()
    secret_id = os.environ["COS_SECRET_ID"]
    print(f"桶：{bucket}｜地域：{os.environ['COS_REGION']}｜SecretId：{secret_id[:4]}***（{len(secret_id)} 字符）")
    print()

    results: dict[str, str] = {}

    def run(label: str, fn) -> str:
        try:
            fn()
            results[label] = "ok"
            print(f"  ✅ {label}")
        except CosServiceError as error:
            results[label] = f"HTTP {error.get_status_code()} {error.get_error_code()}"
            print(f"  ❌ {label} → {results[label]}")
        except Exception as error:  # noqa: BLE001
            results[label] = f"{type(error).__name__}"
            print(f"  ❓ {label} → {type(error).__name__}: {str(error)[:70]}")
        return results[label]

    print("== 权限矩阵 ==")
    run("HEAD 桶", lambda: client.head_bucket(Bucket=bucket))
    run("读桶（列出对象）", lambda: client.list_objects(Bucket=bucket, MaxKeys=5))
    run(f"PUT 对象 @ 桶根 {PROBE_ROOT}", lambda: client.put_object(Bucket=bucket, Key=PROBE_ROOT, Body=b"diag"))
    run(f"PUT 对象 @ 前缀 {PROBE_PREFIX}", lambda: client.put_object(Bucket=bucket, Key=PROBE_PREFIX, Body=b"diag"))
    run("GET 对象 @ 桶根", lambda: client.get_object(Bucket=bucket, Key=PROBE_ROOT))
    run("DELETE 探测对象（桶根）", lambda: client.delete_object(Bucket=bucket, Key=PROBE_ROOT))
    run("DELETE 探测对象（前缀）", lambda: client.delete_object(Bucket=bucket, Key=PROBE_PREFIX))

    root_put = results.get(f"PUT 对象 @ 桶根 {PROBE_ROOT}", "")
    prefix_put = results.get(f"PUT 对象 @ 前缀 {PROBE_PREFIX}", "")
    head = results.get("HEAD 桶", "")

    print()
    print("== 结论与下一步 ==")
    if head != "ok" and root_put != "ok" and prefix_put != "ok":
        print("  ⛔ 这个账号在该桶上**没有任何权限**。要改的是「授权」，不是路径：")
        print("     · COS 控制台 → 桶 → 权限管理 → 存储桶策略 → 添加：用户=该密钥所属账号，")
        print(f"       资源={bucket}/ozon-images/*，操作勾 PutObject/PostObject/InitiateMultipartUpload/")
        print("       UploadPart/CompleteMultipartUpload/AbortMultipartUpload/HeadObject/GetObject；")
        print("     · 或用 CAM 给该子用户关联预置策略 QcloudCOSDataWriteAccess / QcloudCOSDataFullControl；")
        print("     · 或直接用已有写权限的账号建密钥（此前看到 ozon-cos-uploader 有 PutObject 权限）。")
    elif root_put == "ok" and prefix_put != "ok":
        print("  ⚠️ 根路径可写、ozon-images/ 前缀被拒 → 策略**资源前缀**没覆盖该路径。")
        print(f"     把策略资源改成 {bucket}/ozon-images/*（或把 COS_KEY_PREFIX 改成策略允许的前缀）。")
    elif prefix_put == "ok":
        print("  ✅ 写权限没问题。若上传后匿名 GET 仍 403，那是「公开读」的问题：")
        print("     · 安全管理 → 阻止公共访问 → 关闭；访问权限 → 公有读私有写（或策略里让所有用户可读该前缀）。")
    else:
        print(f"  ❓ 未覆盖的组合：HEAD={head}，根 PUT={root_put}，前缀 PUT={prefix_put}")
    return 0 if prefix_put == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
