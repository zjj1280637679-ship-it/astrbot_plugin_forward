import json
import os
import sys
import unittest
from PIL import Image

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ASTRBOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "..", ".."))

if CURRENT_DIR in sys.path:
    sys.path.remove(CURRENT_DIR)
sys.path.insert(0, CURRENT_DIR)

if ASTRBOT_DIR not in sys.path:
    sys.path.append(ASTRBOT_DIR)

from astrbot.core.star.updater import _PluginUpdater
from astrbot.api.all import AstrBotConfig


class TestOfficialAudit(unittest.TestCase):
    def test_01_plugin_structure_and_updater_audit(self):
        """测试 1: 官方 _PluginUpdater 规范校验"""
        valid, reasons = _PluginUpdater.inspect_plugin_directory(CURRENT_DIR)
        self.assertTrue(valid, f"插件规范检查失败: {reasons}")
        print("  [PASS] 1.1 官方 _PluginUpdater 规范校验通过")

    def test_02_conf_schema_validity(self):
        """测试 2: _conf_schema.json 校验与默认配置转换"""
        schema_path = os.path.join(CURRENT_DIR, "_conf_schema.json")
        self.assertTrue(os.path.exists(schema_path))
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        bot_cfg = AstrBotConfig()
        cfg = bot_cfg._config_schema_to_default_config(schema)
        self.assertIsInstance(cfg, dict)
        self.assertTrue(cfg.get("enabled", False))
        self.assertEqual(cfg.get("broadcast_scope"), "allowlist")
        print("  [PASS] 1.2 _conf_schema.json 解析与配置转换验证通过")

    def test_03_logo_specification(self):
        """测试 3: logo.png 尺寸与格式合规性"""
        logo_path = os.path.join(CURRENT_DIR, "logo.png")
        self.assertTrue(os.path.exists(logo_path), "缺少 logo.png")
        with Image.open(logo_path) as img:
            self.assertEqual(img.size, (256, 256), f"Logo 尺寸不合规: {img.size}")
            self.assertEqual(img.format, "PNG")
        print("  [PASS] 1.3 logo.png 符合官方 256x256 标准")

    def test_04_assets_completeness(self):
        """测试 4: 开源合规性必要文件完备性"""
        for req in ["README.md", "metadata.yaml", "LICENSE", "requirements.txt", "CHANGELOG.md"]:
            p = os.path.join(CURRENT_DIR, req)
            self.assertTrue(os.path.exists(p), f"缺少必要规范文件: {req}")
        print("  [PASS] 1.4 开源文件完备性校验通过")


if __name__ == "__main__":
    unittest.main()
