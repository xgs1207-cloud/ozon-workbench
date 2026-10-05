"""部署套件自检：文件齐全、店铺模板合法、脚本不夹带密钥（全离线）。"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pipeline import stores as store_registry  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

REQUIRED_FILES = (
    "README.md",
    "install.sh",
    "ozon-workbench-api.service",
    "ozon-workbench.env.example",
    "nginx-ozon-images.conf",
    "shops.example.json",
)

#: 看起来像真密钥的形态（模板里不该出现）
SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9]{8,}|AKID[A-Za-z0-9]{10,}|[0-9a-f]{32})")


class DeployKitTests(unittest.TestCase):
    def test_required_files_exist(self):
        for name in REQUIRED_FILES:
            self.assertTrue((DEPLOY / name).is_file(), name)

    def test_shops_template_is_valid_and_disabled(self):
        payload = json.loads((DEPLOY / "shops.example.json").read_text(encoding="utf-8"))
        self.assertEqual(store_registry.validate_registry(payload), [])
        self.assertTrue(payload["shops"])
        for shop in payload["shops"]:
            self.assertFalse(shop["enabled"], "模板里的店铺默认必须是关闭的")
            self.assertTrue(shop["client_id_env"].startswith("OZON_"))
            self.assertTrue(shop["api_key_env"].startswith("OZON_"))

    def test_env_template_has_no_real_secrets(self):
        text = (DEPLOY / "ozon-workbench.env.example").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.startswith("OZON_IMAGE_ROOT"):
                continue
            self.assertIsNone(
                SECRET_PATTERN.search(value), f"模板里疑似有真密钥：{key}={value}"
            )

    def test_systemd_unit_points_at_venv_and_env_file(self):
        text = (DEPLOY / "ozon-workbench-api.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-/etc/ozon-workbench.env", text)
        self.assertIn(".venv/bin/uvicorn", text)
        self.assertIn("WorkingDirectory=/opt/ozon-workbench", text)
        self.assertIn("ReadWritePaths=", text)

    def test_read_write_paths_are_optional(self):
        """目录不存在不能让 unit 崩：ReadWritePaths 必须带 "-" 前缀（踩过 226/NAMESPACE）。"""
        text = (DEPLOY / "ozon-workbench-api.service").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ReadWritePaths="):
                self.assertTrue(
                    stripped.startswith("ReadWritePaths=-"),
                    f"ReadWritePaths 缺少 '-' 前缀（目录不存在时服务会崩溃重启）：{stripped}",
                )

    def test_install_creates_config_and_image_dirs(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn('mkdir -p "$APP_DIR" "$IMAGE_ROOT" "$APP_DIR/config"', text)

    def test_nginx_config_forces_https_and_blocks_scripts(self):
        text = (DEPLOY / "nginx-ozon-images.conf").read_text(encoding="utf-8")
        self.assertIn("return 301 https://", text)
        self.assertIn("ssl_certificate", text)
        self.assertIn("root /var/www/ozon-images;", text)
        self.assertIn("deny all;", text)
        self.assertIn("autoindex off;", text)

    def test_install_script_is_idempotent_and_copies_template(self):
        text = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("if [ ! -x \"$APP_DIR/.venv/bin/python\" ]", text)  # venv 只在缺失时创建
        self.assertIn("deploy/shops.example.json", text)
        self.assertIn("contracts/fetch_contracts.sh", text)
        self.assertIn("install -m 600 -o root -g root", text)

    def test_linux_contract_fetcher_lists_the_same_files_as_powershell(self):
        ps1 = (ROOT / "contracts" / "fetch_contracts.ps1").read_text(encoding="utf-8")
        sh = (ROOT / "contracts" / "fetch_contracts.sh").read_text(encoding="utf-8")
        ps_files = set(re.findall(r'"(templates/[^"]+\.json)"', ps1))
        sh_files = set(re.findall(r'"(templates/[^"]+\.json)"', sh))
        self.assertTrue(ps_files)
        self.assertEqual(ps_files, sh_files, "两个脚本的契约清单必须一致")
        self.assertIn("gh-proxy.com", sh)
        self.assertIn("jsdelivr", sh)

    def test_deploy_readme_covers_the_https_requirement(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        self.assertIn("https", text)
        self.assertIn("oss_local", text)
        self.assertIn("certbot", text)
        self.assertIn("shops.example.json", text)


class ShellSyntaxTests(unittest.TestCase):
    """有 bash 就做语法检查（部署脚本不该带着语法错误上服务器）。"""

    BASH_CANDIDATES = (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    )

    @classmethod
    def setUpClass(cls):
        import shutil as _shutil

        found = next((path for path in cls.BASH_CANDIDATES if pathlib.Path(path).is_file()), None)
        if not found:
            found = _shutil.which("bash")
        cls.bash = found

    def check(self, relative: str) -> None:
        if not self.bash:
            self.skipTest("本机没有 bash")
        import subprocess

        result = subprocess.run(
            [self.bash, "-n", str(ROOT / relative)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"{relative} 语法错误：{result.stderr}")

    def test_install_script_syntax(self):
        self.check("deploy/install.sh")

    def test_contract_fetcher_syntax(self):
        self.check("contracts/fetch_contracts.sh")


class DeploymentGotchaTests(unittest.TestCase):
    """两个只有真部署才会暴露的坑，锁成测试防止回归。"""

    def test_shell_scripts_are_lf_in_git_blobs(self):
        """Windows 上 git archive 会把脚本打成 CRLF → Linux 上 `set -o pipefail` 报错。"""
        import shutil
        import subprocess

        git = shutil.which("git")
        if not git:
            self.skipTest("本机没有 git")
        for path in ("contracts/fetch_contracts.sh", "deploy/install.sh"):
            result = subprocess.run(
                [git, "-C", str(ROOT), "show", f"HEAD:{path}"],
                capture_output=True,
            )
            if result.returncode != 0:
                self.skipTest("还不是 git 仓库或文件未提交")
            self.assertNotIn(b"\r", result.stdout, f"{path} 的 git 内容里有 CR（应为 LF）")

    def test_gitattributes_forces_lf_for_scripts(self):
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", text)
        self.assertIn("* text=auto eol=lf", text)

    def test_requirements_include_self_check_dependency(self):
        """服务器自检要跑 API 测试，必须有 httpx（否则 TestClient 导入失败）。"""
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("httpx", text)
        self.assertIn("fastapi", text)


class LicenseTests(unittest.TestCase):
    """许可文件必须在位且是官方原文（不是我自己缩写的摘要）。"""

    def setUp(self):
        self.text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    def test_license_is_polyform_noncommercial_and_has_notice(self):
        self.assertIn("PolyForm Noncommercial License 1.0.0", self.text)
        self.assertIn("Required Notice: Copyright", self.text)
        self.assertIn("Licensor: xgs1207-cloud", self.text)

    def test_official_sections_are_intact(self):
        for section in (
            "## Acceptance",
            "## Copyright License",
            "## Distribution License",
            "## Notices",
            "## Noncommercial Purposes",
            "## No Liability",
            "## Definitions",
        ):
            self.assertIn(section, self.text, section)

    def test_commercial_use_warning_present(self):
        self.assertIn("商业用途", self.text)
        self.assertIn("上游", self.text)

    def test_readme_points_at_license(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[LICENSE](LICENSE)", readme)
        self.assertIn("PolyForm Noncommercial", readme)


class ObjectStorageCliDocsTests(unittest.TestCase):
    def test_readme_documents_local_storage_option(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("oss_local", readme)
        self.assertIn("对象存储", readme)


if __name__ == "__main__":
    unittest.main()
