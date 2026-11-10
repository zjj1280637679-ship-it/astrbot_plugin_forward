# 更新日志 (CHANGELOG)

## [1.0.0] - 2026-11-10

### 🚀 架构重构与官方生产级发布
- **全平台标准化规范**：
  - 符合 AstrBot 官方插件规范，支持 `_PluginUpdater` 零告警自检。
  - 补充官方规格 256x256 RGBA 高清图标 `logo.png`。
  - 增加标准化 `LICENSE` (MIT) 与 `requirements.txt`（零第三方依赖）。
- **核心转发引擎增强**：
  - 严谨修复 allowlist 空白状态下的 `hint` 友好引导提示。
  - 修复 `test_forward.py` 中协程类型断言与工具入口规范。
  - 20/20 个全生命周期单测用例与实机不变式断言 100% 通过。
- **开源发布与生态同步**：
  - 同步至 GitHub 开源仓库 `https://github.com/zjj1280637679-ship-it/astrbot_plugin_forward`。

---

## [0.3.1] - 2026-10-14
- 完成 nova 12 Pro 真机实机实测与 B 站分享卡片保真度验收。
- 引入 G2 锚点去重、G8 血缘深度、G3 冷却、G4 扇出上界等多重安全守卫。
