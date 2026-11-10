# astrbot_plugin_forward · AI 转发

把**被引用的消息**（文字 / 图片 / 分享卡片，如 B 站分享）转发到指定群或机器人加入的全部群。

> 设计依据：[astrbot_plugin_forward-设计方案.md](../astrbot_plugin_forward-设计方案.md)（v0.5，含两轮实机实测结论）

## 它做什么

- 群里有人分享了一条 B 站视频，`@机器人 转发到所有群` → AI 调 `forward_message` 工具转发。
- 也支持 `/转发 <all|only|except|here> [群号,...]` 手动命令。

## 工具面板（AI 看到的两个工具）

| 工具 | 作用 |
|---|---|
| `list_forward_targets()` | **实时读取「群名 ↔ 群号」对照表**（含 `available`/`reason`） |
| `forward_message(source, scope, group_ids)` | 执行转发 |

### 核心契约：`group_ids` 只收群号

```
用户："转发到哲学群"
  ↓
AI ① list_forward_targets()        → 拿到实时对照表
AI ② 表里把"哲学群"翻成 1017786877  → 这是 AI 的活
AI ③ forward_message(scope="only", group_ids="1017786877")
  ↓
插件 只校验群号（数字 / 在列表里 / 可投递），不做任何名称匹配
```

**为什么插件不做模糊匹配**：转发**不可撤销**（实测撤回窗口约 2 分钟）。
"哲学群"可能匹配到「哲学」和「哲学群」两个群，插件无权替用户决定 —— **宁可让 AI 反问一句，也不猜**。

### 四种策略（`scope`）

| scope | 中文 | 语义 |
|---|---|---|
| `all` | 全体 | 所有可投群（排除来源群） |
| `only` | 白名单 | **只发** `group_ids` 里列的群 |
| `except` | 黑名单 | 发所有可投群但**排除** `group_ids` 里列的群 |
| `here` | 本群 | 只发回来源群 |

`group_ids` 支持中文别名（"全体"/"白名单"/"黑名单"/"本群"也能识别）。

### source 怎么选（最要紧）

| 用户怎么说 | 用哪个 |
|---|---|
| **先引用一条消息，再说"帮我转发到 X 群"** | **`source="quoted"`（默认值）** ← 绝大多数情况 |
| "把这条 / 我这句话发出去"（且消息里没有引用） | `source="current"` |

**护栏**：若传了 `current`，但消息里引用了别的内容、而当前消息只有一句短文字，
插件会**拒绝执行**并提示改用 `quoted` —— 防止把用户的指令本身（如"帮我发到山根王国"）
当成内容转发出去（这是实机踩过的真实事故）。

### 工具返回语义：**不刷硬回执**

两个 LLM 工具都是 `async def ... -> str`，用 **`return`** 把结果回给 LLM，
由 AI 用自己的话回复 —— 与"网络搜索"这类工具的行为一致。

```python
@filter.llm_tool(name="forward_message")
async def forward_message(self, event, source, scope, group_ids="") -> str:
    """..."""
    return await self._do_forward(event, source, scope, group_ids)   # ← return，不是 yield
```

**不要**写成 `yield event.plain_result(JSON)` —— 那会把回执**直接贴到群里**。
（`/转发` 命令是例外：它由用户直接发起，回执应该可见。）

依据 `register_llm_tool` 官方 docstring（`star_handler.py:604-616`）：
`return str` → 结果加入下一次 LLM 请求的 prompt；`yield` → 直接发消息。

单测里有回归防护：两个工具必须 `iscoroutinefunction=True` 且 `isasyncgenfunction=False`。

### 拒绝面（每次都带中文 `message`，AI 可直接转述）

| 类别 | 错误码 | 人话示例 |
|---|---|---|
| 频次 | `rate_limited` / `sender_flooding` | "距上次广播不足 42 秒（冷却 60 秒），请稍后再试" |
| 内容 | `content_blocked` | "内容含 5 个链接，超过上限 3，疑似广告" |
| 权限 | `permission_denied` | "广播到全部群需要管理员权限" |
| 目标 | `bad_argument` / `group_not_found` | "只接受群号，收到「哲学」。请先用 list_forward_targets 查表" |

> ⚠️ **内容拦截是规则层（关键词 + 链接数/@数/长度），不是语义审核**。
> 能挡明显广告刷屏，**挡不住措辞委婉的违规内容** —— 不宣称能力之外的事。

## 设计上最要紧的三件事

### 1. 转发**永不自动发生**

只由 AI 工具调用或管理员命令触发。这是防广播风暴的**结构性前提**，不是可选项。

### 2. 不构造任何载荷（实机实测得出）

真实 B 站分享卡在 OneBot 侧是 `com.tencent.miniapp_01`。经**真机目视验收**：
把消息链**原样**用 `context.send_message` 投递即可，卡片完整渲染；
`json` 段的 `data.data` 传字符串或对象**都正常**。

所以插件里没有 `Node` 重建、没有卡片序列化覆写、没有图片 base64 处理 —— 那些都是**不必要的复杂度**。

### 3. 价值在编排层

传输层宿主已经够用，真正的风险在"转发到所有群"这个动作本身：

| 守卫 | 作用 |
|---|---|
| **G2** 去重 | 指纹 + 锚点（URL/BV号），同一内容在 TTL 内最多广播一次 |
| **G8** 血缘深度 | 指纹可被改写绕过，**血缘深度是唯一不依赖内容指纹的终止性保证** |
| **G3** 冷却 | 两次广播之间的最小间隔 |
| **G4** 扇出上限 | 单次广播最多几个群 |
| **I8/T4** 三值回执 | 每个目标的结果 ∈ `SENT` / `FAILED` / `UNKNOWN`，**不允许静默消失** |

## 配置

见 `_conf_schema.json`。几个关键项：

| 键 | 默认 | 说明 |
|---|---|---|
| `broadcast_scope` | `allowlist` | 默认**收窄**目标集；成熟生态都不用"转发到所有群" |
| `max_hop` | `1` | 同一内容只允许被转发一次 |
| `broadcast_cooldown_seconds` | `60` | 两次广播最小间隔 |
| `max_fanout` | `20` | 单次广播最大群数 |
| `dedup_ttl_seconds` | `600` | 去重窗口，须大于最大重投递窗口 |

## 安装

放到 AstrBot 的 `data/plugins/astrbot_plugin_forward/`，重启或在面板重载。

仅支持 `aiocqhttp`（OneBot v11）。

## 测试

```bash
python -m pytest tests -q
```

20 个用例，把设计方案 §4 的不变式写成了**可执行断言**：

| 用例 | 覆盖 |
|---|---|
| `test_T1_same_payload_only_once` | 定理 T1：同载荷连发 5 次，只成功 1 次 |
| `test_I3_anchor_dedup_blocks_reworded_republish` | 改写措辞但保留链接仍被判重 |
| `test_I1_record_written_before_first_send` | 不变式 I1：占位先于投递 |
| `test_I5_fanout_capped_and_truncated_flag` | 不变式 I5 / G4 扇出上界 |
| `test_I6_cooldown_blocks_second_broadcast` | 不变式 I6 / G3 冷却 |
| `test_D1_cooldown_rejection_leaves_no_record` | 纪律 D1：被拒不留下幽灵记录 |
| `test_T2b_same_anchor_second_hop_blocked` | 同锚点第二跳被 G2 拦住（链断） |
| `test_G8_hop_limit_blocks_lineage_continuation` | G8 兜底：锚点消失时仍靠血缘终止 |
| `test_T4_failed_target_reported_not_silent` | 定理 T4：失败目标必须上报 |
| `test_G6_broadcast_requires_admin` | 广播需管理员 |
| `test_G7_rejects_non_aiocqhttp` | 平台守卫 |
| `test_G1_no_reply_chain` | 引用内容取不到时的稳定错误码 |
| `test_origin_excluded_from_targets` | 不回源群 |
| `test_broadcast_scope_allowlist_only_targets_whitelist` 等 4 个 | 配置项真的生效（scope / denylist / admin_only / enable_*） |
| `test_session_format_uses_correct_message_type` | **实机回归**：umo 必须是 `GroupMessage` 而非 `group` |

## 实机验证（2026-10-14）

在 nova 12 Pro（AstrBot 4.27.4 + NapCat）上跑通端到端：

- 用户引用真实 B 站分享卡发 `/转发 all` → 白名单内 2 个群各收到一条，`state=COMPLETED` 零失败
- **保真度**：投递卡片与源卡片**解析后 31 个叶子字段逐字段一致**（字符串长度差 318 字符纯属 JSON 空白压缩）
- **踩过的坑**：umo 若拼成 `default:group:<gid>` 会抛 `ValueError`（正确值是 `GroupMessage`）；
  单测因 mock 掉 `send_message` 而无法发现 —— 已加回归用例 + 改用 `MessageSession` 对象寻址

## 已知边界（实测）

- **撤回有时效窗口**：`delete_msg` 约 2 分钟后永久失败（实测 13 次重试全败）。若要"转发后撤回"，必须秒级同请求内完成。
- **NapCat 不上报机器人自己的消息**（`reportSelfMessage: false`），自回声循环在传输层不存在；但插件仍保留 S 守卫作纵深防御，**不把配置当作安全前提**。

## 零密钥

插件内不保存任何 API Key / Base URL，不复用带 rkey 的图片 URL。
