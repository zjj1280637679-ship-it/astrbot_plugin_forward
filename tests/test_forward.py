"""把设计方案 §4 的不变式写成可执行断言。

本文件用最小 stub 替代 `astrbot.*`，使测试无需安装 AstrBot 即可运行。
覆盖：I1（记录先于发送）、I2/I3（指纹/锚点唯一）、I5（扇出上界）、
I6（冷却上界）、I7（深度上界）、I8（回执三值完备），以及定理 T1 / T2b / T4。
"""

import asyncio
import sys
import time
import types

import pytest

# ------------------------------------------------------------------ astrbot stub


def _install_stubs():
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    event_mod = types.ModuleType("astrbot.api.event")

    class _Filter:
        class EventMessageType:
            ALL = 1

        def __getattr__(self, name):
            def deco(*a, **k):
                def wrap(fn):
                    return fn

                return wrap

            return deco

    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = object

    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    event_mod.MessageChain = _MessageChain

    comp = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        def __init__(self, file=""):
            self.file = file

    class Json:
        def __init__(self, data=None):
            self.data = data

    class Reply:
        def __init__(self, chain=None, id=None):
            self.chain = chain
            self.id = id

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    for cls in (Plain, Image, Json, Reply, At):
        setattr(comp, cls.__name__, cls)

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star_mod.Star = Star
    star_mod.Context = object
    star_mod.register = register

    for name, mod in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.message_components", comp),
        ("astrbot.api.star", star_mod),
    ):
        sys.modules[name] = mod
    astrbot.api = api


_install_stubs()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import main as fwd  # noqa: E402
from astrbot.api.message_components import At, Image, Json, Plain, Reply  # noqa: E402


# ------------------------------------------------------------------ 测试替身


class FakeContext:
    def __init__(self, groups=None, fail_on=None):
        self.sent = []          # 投递记录（顺序敏感，用于 I1）
        self.groups = groups or []
        self.fail_on = set(fail_on or [])
        self.platform = types.SimpleNamespace(
            bot=types.SimpleNamespace(get_group_list=self._group_list)
        )

    async def _group_list(self):
        return [{"group_id": g} for g in self.groups]

    def get_platform_inst(self, _pid):
        return self.platform

    async def send_message(self, umo, chain):
        gid = umo.split(":")[-1]
        if gid in self.fail_on:
            raise RuntimeError("boom")
        self.sent.append(gid)
        return True


class FakeEvent:
    def __init__(self, chain, group="1", platform="default", admin=True):
        self._chain = chain
        self._group = group
        self._platform = platform
        self._admin = admin
        self.stopped = False

    def get_messages(self):
        return self._chain

    def get_group_id(self):
        return self._group

    def get_platform_id(self):
        return self._platform

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "bot"

    def get_sender_id(self):
        return "user"

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


def make_plugin(groups=("2", "3", "4"), fail_on=None, **cfg):
    base = {
        "enabled": True,
        "enable_llm_tool": True,
        "enable_command": True,
        "admin_only_broadcast": True,
        "broadcast_scope": "all",      # 扇出类用例走 all；allowlist 语义由专测覆盖
        "group_allowlist": [],
        "group_denylist": [],
        "max_fanout": 20,
        "max_hop": 1,
        "send_interval_ms": 0,
        "broadcast_cooldown_seconds": 0,
        "dedup_ttl_seconds": 600,
        "per_send_timeout_seconds": 5,
    }
    base.update(cfg)
    ctx = FakeContext(groups=list(groups), fail_on=fail_on)
    plugin = fwd.ForwardPlugin(ctx, base)
    plugin._state = {"records": []}
    plugin._state_path = __import__("pathlib").Path("/tmp/_fwd_test_state.json")
    return plugin, ctx


CARD = {
    "app": "com.tencent.miniapp_01",
    "prompt": "[QQ小程序]能量巨大",
    "meta": {"detail_1": {"title": "能量巨大", "qqdocurl": "https://b23.tv/abc123"}},
}


def quoted_event(**kw):
    inner = [Plain("看这个"), Json(data=CARD)]
    kw.setdefault("admin", True)   # 广播默认走管理员路径；G6 由专门用例覆盖
    return FakeEvent([Reply(chain=inner, id="99")], **kw)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ I2 / T1：去重


def test_T1_same_payload_only_once():
    """T1：同一载荷连发 5 次，只应成功 1 次，其余 duplicate_payload。"""
    plugin, ctx = make_plugin()
    results = []
    for _ in range(5):
        ev = quoted_event(group="1")
        results.append(run(plugin._do_forward(ev, "quoted", "all")))
    import json as _json

    parsed = [_json.loads(r) for r in results]
    ok = [p for p in parsed if p.get("ok")]
    dup = [p for p in parsed if p.get("error") == fwd.E_DUPLICATE]
    assert len(ok) == 1, f"应只成功 1 次，实际 {len(ok)}"
    assert len(dup) == 4, f"应有 4 次 duplicate，实际 {len(dup)}"
    assert ok[0]["sent"] == ["2", "3", "4"]


def test_I3_anchor_dedup_blocks_reworded_republish():
    """I3：改写措辞但保留同一链接，仍应被判重。"""
    plugin, _ = make_plugin()
    run(plugin._do_forward(quoted_event(group="1"), "quoted", "all"))
    # 改写：不同文本，但同一 b23.tv 链接
    ev2 = FakeEvent([Plain("换个说法再发一次 https://b23.tv/abc123")], group="1")
    out = run(plugin._do_forward(ev2, "current", "all"))
    import json as _json

    assert _json.loads(out)["error"] == fwd.E_DUPLICATE


# ------------------------------------------------------------------ I1：记录先于发送


def test_I1_record_written_before_first_send():
    """I1：占位必须先于任何一次投递（用一个会在发送时检查状态的 Context 验证）。"""
    plugin, ctx = make_plugin()
    seen = []

    orig_send = ctx.send_message

    async def spy(umo, chain):
        seen.append(len(plugin._state["records"]))
        return await orig_send(umo, chain)

    ctx.send_message = spy
    run(plugin._do_forward(quoted_event(group="1"), "quoted", "all"))
    assert seen, "应至少有一次投递"
    assert all(n >= 1 for n in seen), f"投递时记录尚未写入：{seen}"


# ------------------------------------------------------------------ I5 / G4：扇出上界


def test_I5_fanout_capped_and_truncated_flag():
    plugin, ctx = make_plugin(groups=[str(i) for i in range(2, 30)], max_fanout=5)
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert out["total"] == 5
    assert out["truncated"] is True
    assert len(ctx.sent) == 5


# ------------------------------------------------------------------ I6 / G3：冷却


def test_I6_cooldown_blocks_second_broadcast():
    plugin, _ = make_plugin(broadcast_cooldown_seconds=9999)
    import json as _json

    first = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert first["ok"] is True
    # 换一份不同内容，避开 G2，专门验证 G3
    ev2 = FakeEvent([Plain("完全不同的另一条消息")], group="1")
    second = _json.loads(run(plugin._do_forward(ev2, "current", "all")))
    assert second["error"] == fwd.E_RATE_LIMITED


def test_D1_cooldown_rejection_leaves_no_record():
    """D1：冷却失败时不得留下幽灵记录（否则白占 TTL、误杀后续合法转发）。"""
    plugin, _ = make_plugin(broadcast_cooldown_seconds=9999)
    run(plugin._do_forward(quoted_event(group="1"), "quoted", "all"))
    before = len(plugin._state["records"])
    ev2 = FakeEvent([Plain("另一条")], group="1")
    run(plugin._do_forward(ev2, "current", "all"))
    assert len(plugin._state["records"]) == before, "被拒请求不应新增记录"


# ------------------------------------------------------------------ I7 / G8：血缘深度


def test_T2b_same_anchor_second_hop_blocked():
    """T2b（真实场景）：同一锚点的第二跳被 G2 去重拦住 —— 转发链无法延续。

    这是最常见的情形：内容被转发到 2/3/4 后，群 2 里再有人（或 AI）想转同一条，
    锚点相同 → 命中 G2 duplicate_payload。**链在这里就断了。**
    """
    plugin, _ = make_plugin(max_hop=1)
    import json as _json

    r1 = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert r1["ok"] and r1["depth"] == 0

    ev2 = FakeEvent([Plain("转发 https://b23.tv/abc123")], group="2")
    r2 = _json.loads(run(plugin._do_forward(ev2, "current", "all")))
    assert r2["error"] == fwd.E_DUPLICATE, f"同锚点第二跳应被 G2 拦住：{r2}"


def test_G8_hop_limit_blocks_lineage_continuation():
    """G8：锚点不同（避开 G2），但来源群 ∈ 上一跳目标集 → 血缘识别 → 深度超限被拒。

    这是 G2 的**兜底**：若 AI 把内容改写到锚点消失，指纹与锚点都失效，
    仍由血缘深度（G8）保证终止性 —— 这是唯一不依赖内容指纹的硬保证。
    """
    plugin, _ = make_plugin(max_hop=0)
    import json as _json

    r1 = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert r1["ok"] and r1["depth"] == 0

    # 完全不同的一条消息（无锚点重合），但来自群 2 —— 上一跳的目标之一
    ev2 = FakeEvent([Plain("另一条完全不相干的消息")], group="2")
    r2 = _json.loads(run(plugin._do_forward(ev2, "current", "all")))
    assert r2["error"] == fwd.E_HOP_LIMIT, f"血缘续接应被 G8 拦住：{r2}"


# ------------------------------------------------------------------ I8 / T4：三值回执


def test_T4_failed_target_reported_not_silent():
    """T4：抛异常的目标必须出现在 failed[]，不得静默消失。"""
    plugin, _ = make_plugin(fail_on=["3"], broadcast_cooldown_seconds=0)
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert out["ok"] is True
    failed_groups = [f["group"] for f in out["failed"]]
    assert "3" in failed_groups, f"目标 3 未出现在 failed[]：{out}"
    assert out["state"] if "state" in out else True
    # 回执必须覆盖全部目标
    total = len(out["sent"]) + len(out["failed"]) + len(out["unknown"])
    assert total == out["total"], "回执未覆盖全部目标"


# ------------------------------------------------------------------ 守卫


def test_G6_broadcast_requires_admin():
    plugin, _ = make_plugin()
    import json as _json

    ev = quoted_event(group="1", admin=False)
    out = _json.loads(run(plugin._do_forward(ev, "quoted", "all")))
    assert out["error"] == fwd.E_PERMISSION_DENIED


def test_G7_rejects_non_aiocqhttp():
    plugin, _ = make_plugin()
    import json as _json

    ev = quoted_event(group="1", platform="telegram")
    ev._platform = "telegram"
    ev.get_platform_name = lambda: "telegram"
    out = _json.loads(run(plugin._do_forward(ev, "quoted", "all")))
    assert out["error"] == fwd.E_UNSUPPORTED_PLATFORM


def test_G1_no_reply_chain():
    plugin, _ = make_plugin()
    import json as _json

    ev = FakeEvent([Plain("没有引用任何消息")], group="1", admin=True)
    out = _json.loads(run(plugin._do_forward(ev, "quoted", "all")))
    assert out["error"] == fwd.E_NO_REPLY_CHAIN


def test_origin_excluded_from_targets():
    """来源群不得出现在目标集中（X 原则：不回源）。"""
    plugin, ctx = make_plugin(groups=["1", "2", "3"])
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert "1" not in out["sent"]
    assert set(out["sent"]) == {"2", "3"}


# ---------------------------------------------------------------- 会话寻址（实机回归）

def test_session_format_uses_correct_message_type():
    """回归：MessageType.GROUP_MESSAGE 的值是 "GroupMessage"，**不是 "group"**。

    实机踩过这个坑：拼成 "default:group:<gid>" 会让 `context.send_message`
    在 `MessageSession.from_str` 里抛 `ValueError`，两个目标群全部投递失败。
    而"mock 掉 send_message"的单测**永远发现不了**（因为 mock 接受任何 umo）。

    真实契约见 `astrbot/core/platform/message_type.py:5`。
    """
    s = fwd.build_session("default", "123456")
    s = s if isinstance(s, str) else str(s)
    assert s == "default:GroupMessage:123456", f"umo 格式错误：{s}"
    assert fwd._GROUP_TYPE_STR == "GroupMessage"


def test_targets_use_build_session_not_hardcoded_group():
    """确保投递路径调用 build_session（而不是某处又硬编码了 "group"）。"""
    plugin, ctx = make_plugin()
    seen = []
    orig = ctx.send_message

    async def spy(session, chain):
        seen.append(session if isinstance(session, str) else str(session))
        return await orig(session, chain)

    ctx.send_message = spy
    run(plugin._do_forward(quoted_event(group="1"), "quoted", "all"))
    assert seen, "应至少有一次投递"
    for s in seen:
        assert "GroupMessage" in s, f"投递用的 umo 不含 GroupMessage：{s}"
        assert ":group:" not in s, f"投递用了错误的小写 group：{s}"

def test_broadcast_scope_allowlist_only_targets_whitelist():
    """broadcast_scope=allowlist：只投白名单内的群。"""
    plugin, _ = make_plugin(
        groups=["1", "2", "3", "4"],
        broadcast_scope="allowlist",
        group_allowlist=["3", "4"],
    )
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert out["ok"] is True
    assert set(out["sent"]) == {"3", "4"}, f"应只投白名单：{out}"


def test_broadcast_scope_allowlist_empty_yields_no_target_with_hint():
    """保守默认：allowlist 模式下白名单为空 → 拒绝，且给出可操作提示。"""
    plugin, _ = make_plugin(groups=["1", "2", "3"], broadcast_scope="allowlist")
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert out["error"] == fwd.E_NO_TARGET
    assert "broadcast_scope" in out.get("hint", ""), f"应给出可操作提示：{out}"


def test_group_denylist_applies_in_both_scopes():
    """黑名单在 all 与 allowlist 两种 scope 下都生效。"""
    plugin, _ = make_plugin(groups=["1", "2", "3"], group_denylist=["2"])
    import json as _json

    out = _json.loads(run(plugin._do_forward(quoted_event(group="1"), "quoted", "all")))
    assert set(out["sent"]) == {"3"}, f"黑名单未生效：{out}"


def test_admin_only_broadcast_can_be_disabled():
    """admin_only_broadcast=False 时，非管理员也能广播。"""
    plugin, _ = make_plugin(admin_only_broadcast=False)
    import json as _json

    ev = quoted_event(group="1", admin=False)
    out = _json.loads(run(plugin._do_forward(ev, "quoted", "all")))
    assert out["ok"] is True, f"关闭管理员限制后应放行：{out}"


def test_enable_llm_tool_and_command_gate():
    """enable_llm_tool / enable_command 关闭时，对应入口回 forward_disabled。"""
    plugin, _ = make_plugin(enable_llm_tool=False, enable_command=False)
    import json as _json

    async def collect(agen):
        return [x async for x in agen]

    ev = quoted_event(group="1")
    # 工具入口
    tool_out = run(plugin.forward_message(ev, "quoted", "all"))
    assert _json.loads(tool_out)["error"] == fwd.E_DISABLED
    # 命令入口
    cmd_out = run(collect(plugin.cmd_forward(ev, "all")))
    assert _json.loads(cmd_out[0])["error"] == fwd.E_DISABLED
