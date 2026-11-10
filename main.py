"""astrbot_plugin_forward —— AI 转发 / 跨群广播。

设计依据：
- `astrbot_plugin_forward-设计方案.md` v0.5（两轮实机实测收敛）
- `astrbot_plugin_forward-工具面板设计.md`（面向自然语言的工具面）

核心立场：
- **插件不解析自然语言**。插件只提供语义无歧义、枚举清晰、名字可解析的工具面，
  "模糊理解"交给 LLM —— 这是正确的分工。
- **转发永不自动发生**；价值在编排层（去重 / 血缘 / 冷却 / 扇出 / 三值回执）。
"""

import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, register

# 会话寻址：优先用 MessageSession 对象，彻底绕开字符串解析。
# 注意 MessageType.GROUP_MESSAGE 的值是 "GroupMessage"（不是 "group"）——
# 拼错会让 context.send_message 抛 ValueError（实机踩过）。
try:
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType
except Exception:  # pragma: no cover - 老版本兜底
    MessageSession = None
    MessageType = None

_GROUP_TYPE_STR = (
    MessageType.GROUP_MESSAGE.value if MessageType is not None else "GroupMessage"
)


def build_session(platform_id: str, group_id: str):
    """构造目标群会话。优先对象形式；不可用时退回正确的字符串格式。"""
    if MessageSession is not None and MessageType is not None:
        return MessageSession(platform_id, MessageType.GROUP_MESSAGE, str(group_id))
    return f"{platform_id}:{_GROUP_TYPE_STR}:{group_id}"


# ---------------------------------------------------------------- 稳定错误码

E_DISABLED = "forward_disabled"
E_EMPTY = "empty_payload"
E_NO_REPLY_CHAIN = "no_reply_chain"
E_DUPLICATE = "duplicate_payload"
E_RATE_LIMITED = "rate_limited"
E_SENDER_FLOODING = "sender_flooding"
E_CONTENT_BLOCKED = "content_blocked"
E_NO_TARGET = "no_target"
E_PERMISSION_DENIED = "permission_denied"
E_UNSUPPORTED_PLATFORM = "unsupported_platform"
E_HOP_LIMIT = "hop_limit_exceeded"
E_GROUP_LIST_FAILED = "group_list_failed"
E_BAD_ARG = "bad_argument"
E_AMBIGUOUS_GROUP = "ambiguous_group"
E_GROUP_NOT_FOUND = "group_not_found"

# 错误码 → 人话（LLM 直接转述即可，不必自己组织措辞）
_MESSAGES = {
    E_DISABLED: "转发功能已关闭。",
    E_EMPTY: "这条消息里没有可转发的内容。",
    E_NO_REPLY_CHAIN: "没有取到被引用消息的内容，请确认引用的是哪一条。",
    E_NO_TARGET: "没有可投递的目标群。",
    E_PERMISSION_DENIED: "广播到全部群需要管理员权限。",
    E_UNSUPPORTED_PLATFORM: "当前平台不支持转发。",
    E_GROUP_LIST_FAILED: "取群列表失败，请稍后再试。",
    E_HOP_LIMIT: "这条内容已在转发链上传播过，为避免循环不再继续转发。",
    E_BAD_ARG: "参数不正确。",
}

# 锚点提取：URL 与 B 站 BV 号（用于抗"改写后重发"）
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

_SCOPE_ALIASES = {
    "all": {"all", "全部", "所有", "全体", "所有群", "全区", "广播"},
    "only": {"only", "include", "白名单", "只发", "仅", "仅发"},
    "except": {"except", "exclude", "黑名单", "排除", "除了", "除了这些"},
    "here": {"here", "本群", "当前", "当前群"},
}


def _data_dir() -> Path:
    """插件数据目录：<AstrBot>/data/plugin_data/<plugin_name>/。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "plugins" and parent.parent.name == "data":
            d = parent.parent / "plugin_data" / here.parent.name
            d.mkdir(parents=True, exist_ok=True)
            return d
    d = here.parent / "_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _canon(chain) -> str:
    """把消息链规范化成可哈希的字符串（用于指纹）。图片只哈希引用标识。"""
    parts = []
    for c in chain or []:
        t = type(c).__name__
        if t == "Plain":
            parts.append("T:" + (getattr(c, "text", "") or ""))
        elif t == "Image":
            f = getattr(c, "file", None) or getattr(c, "url", None) or ""
            parts.append("I:" + str(f)[:200])
        elif t == "Json":
            d = getattr(c, "data", None)
            try:
                parts.append("J:" + json.dumps(d, ensure_ascii=False, sort_keys=True))
            except Exception:
                parts.append("J:" + str(d)[:400])
        elif t == "Forward":
            parts.append("F:" + str(getattr(c, "id", "")))
        elif t == "At":
            parts.append("A:" + str(getattr(c, "qq", "")))
        else:
            parts.append(t + ":")
    return "\n".join(parts)


def _anchors(chain) -> set:
    """抽取锚点集合：URL ∪ BV 号 ∪ 卡片内链接。"""
    out = set()
    for c in chain or []:
        t = type(c).__name__
        texts = []
        if t == "Plain":
            texts.append(getattr(c, "text", "") or "")
        elif t == "Json":
            d = getattr(c, "data", None)
            try:
                texts.append(json.dumps(d, ensure_ascii=False))
            except Exception:
                texts.append(str(d))
        for s in texts:
            out.update(_URL_RE.findall(s))
            out.update(_BV_RE.findall(s))
    return out


def _plain_text(chain) -> str:
    parts = []
    for c in chain or []:
        t = type(c).__name__
        if t == "Plain":
            parts.append(getattr(c, "text", "") or "")
        elif t == "Json":
            try:
                parts.append(json.dumps(getattr(c, "data", None), ensure_ascii=False))
            except Exception:
                pass
    return "\n".join(parts)


def _fingerprint(chain) -> str:
    return hashlib.sha256(_canon(chain).encode("utf-8")).hexdigest()[:16]


def parse_scope(raw) -> str | None:
    """把 scope 参数（含中文别名）归一成 all/only/except/here。"""
    t = str(raw or "").strip().lower()
    for name, aliases in _SCOPE_ALIASES.items():
        if t in aliases:
            return name
    return None


def validate_group_ids(tokens: list, all_groups: dict, reachable: set) -> tuple:
    """校验工具面板传入的群号。

    **只接受纯数字群号** —— 群名 → 群号的映射由 LLM 查 `list_forward_targets`
    得到的实时表自行完成，插件不做模糊匹配（职责分离：插件给事实，LLM 做理解）。

    Args:
        tokens: 传入的原始片段。
        all_groups: {群号: 群名}，机器人当前所在的所有群。
        reachable: 配置层允许投递的群号集合。

    Returns:
        (ids, error)；error 为 None 表示全部通过。
    """
    ids, non_digit, unknown, blocked = [], [], [], []
    for tok in tokens:
        tok = str(tok).strip()
        if not tok:
            continue
        if not tok.isdigit():
            non_digit.append(tok)
            continue
        if tok not in all_groups:
            unknown.append(tok)
            continue
        if tok not in reachable:
            blocked.append(tok)
            continue
        ids.append(tok)

    if non_digit:
        return [], (
            E_BAD_ARG,
            "group_ids 只接受群号（纯数字），收到「%s」。"
            "群名请先用 list_forward_targets 查表换成群号。" % "、".join(non_digit),
        )
    if unknown:
        return [], (E_GROUP_NOT_FOUND, "机器人不在群 %s 里。" % "、".join(unknown))
    if blocked:
        return [], (
            E_GROUP_NOT_FOUND,
            "群 %s 被配置层白/黑名单排除，不可投递。" % "、".join(blocked),
        )
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out, None


@register(
    "astrbot_plugin_forward",
    "羊膜大人",
    "AI 转发：把被引用的消息（文字/图片/分享卡片）转发到指定群或全部群；"
    "支持 全体/白名单/黑名单/本群 四种策略，带去重、血缘深度、频次与内容拦截。",
    "0.3.1",
)
class ForwardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.cfg = config if config is not None else {}
        self._lock = asyncio.Lock()          # D3：查重 + 冷却 + 占位 同锁
        self._last_broadcast = 0.0
        self._state_path = _data_dir() / "forward_state.json"
        self._state = self._load_state()

    # ------------------------------------------------------------ 配置读取

    def _get(self, key, default=None):
        try:
            v = self.cfg.get(key, default)
        except Exception:
            v = default
        return default if v is None else v

    @property
    def _enabled(self):
        return bool(self._get("enabled", True))

    @property
    def _cooldown(self):
        return max(0, int(self._get("broadcast_cooldown_seconds", 60)))

    @property
    def _ttl(self):
        return max(30, int(self._get("dedup_ttl_seconds", 600)))

    @property
    def _max_fanout(self):
        return max(1, int(self._get("max_fanout", 20)))

    @property
    def _max_hop(self):
        return max(0, int(self._get("max_hop", 1)))

    @property
    def _interval(self):
        return max(0.0, int(self._get("send_interval_ms", 800)) / 1000.0)

    @property
    def _timeout(self):
        return max(1, int(self._get("per_send_timeout_seconds", 15)))

    @property
    def _sender_window(self):
        return max(0, int(self._get("sender_window_seconds", 60)))

    @property
    def _sender_max(self):
        return max(1, int(self._get("sender_max_in_window", 3)))

    # ------------------------------------------------------------ 状态持久化

    def _load_state(self):
        try:
            with open(self._state_path, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("records", [])
            st.setdefault("sender_hits", {})
            return st
        except Exception:
            return {"records": [], "sender_hits": {}}

    def _save_state(self):
        try:
            tmp = self._state_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._state, f, ensure_ascii=False)
            tmp.replace(self._state_path)
        except Exception:
            logger.exception("[forward] 状态写入失败")

    def _live_records(self, now):
        recs = [
            r
            for r in self._state.get("records", [])
            if now < float(r.get("t0", 0)) + float(r.get("ttl", self._ttl))
        ]
        self._state["records"] = recs
        return recs

    def _find_duplicate(self, records, fp, anchors):
        for r in records:
            if r.get("fp") == fp:
                return r
            if anchors and set(r.get("anchors") or []) & anchors:
                return r
        return None

    def _hop_depth(self, records, anchors, origin):
        """G8：血缘深度 = 1 + max{存活记录 depth | 锚点相交 或 来源群 ∈ 该记录目标集}。"""
        best = -1
        for r in records:
            hit = bool(anchors and set(r.get("anchors") or []) & anchors)
            if not hit and origin and origin in (r.get("targets") or []):
                hit = True
            if hit:
                best = max(best, int(r.get("depth", 0)))
        return best + 1

    def _sender_hits(self, sender, now):
        """单用户滑动窗口计数（防刷）。"""
        win = self._sender_window
        if win <= 0:
            return []
        hits = self._state.setdefault("sender_hits", {})
        arr = [t for t in hits.get(str(sender), []) if now - float(t) < win]
        hits[str(sender)] = arr
        return arr

    # ------------------------------------------------------------ 入站：S 守卫

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _self_echo_guard(self, event: AstrMessageEvent):
        """S 守卫：机器人自身广播的回声，在 TTL 内直接阻断本轮 LLM。

        注：NapCat 的 `reportSelfMessage` 默认 false，正常情况下不会触发；
        此处为纵深防御，不把配置当作安全前提。
        """
        try:
            if event.get_self_id() != event.get_sender_id():
                return
            chain = event.get_messages()
            fp = _fingerprint(chain)
            anchors = _anchors(chain)
            async with self._lock:
                rec = self._find_duplicate(self._live_records(time.time()), fp, anchors)
            if rec is not None:
                logger.info("[forward] S 守卫阻断自回声 fp=%s", fp)
                event.stop_event()
        except Exception:
            logger.exception("[forward] S 守卫异常")

    # ------------------------------------------------------------ 工具面

    @filter.llm_tool(name="forward_message")
    async def forward_message(
        self, event: AstrMessageEvent, source: str = "quoted", scope: str = "all",
        group_ids: str = ""
    ) -> str:
        """把消息转发到其他群。仅当用户明确要求「转发 / 分享 / 广播到别处」时才调用。

        **source 怎么选（最要紧）**：用户通常是「先引用一条消息，再说'帮我转发到 X 群'」，
        这时要转的是**被引用的那条**，必须用 source="quoted"（默认值）。
        只有用户明确要发「我这句话本身」、且消息里没有引用时，才用 source="current"。
        **千万不要把用户那句指令本身（如"帮我发到山根王国"）当内容转发出去。**

        group_ids 只接受群号。若用户说的是群名（如"哲学群"），
        必须先用 list_forward_targets 查到对应群号，再填进来；不要直接填群名，也不要编造群号。
        不确定用户指哪个群时，先问清楚。
        不要转发广告、刷屏或违法内容。同一内容在去重窗口内只会成功一次，
        重复调用返回 duplicate_payload 属正常；被频次或内容规则拦下时也在回执里说明原因。
        本工具只返回执行结果给你（不会直接发消息到群里），请据此用自己的话回复用户。

        Args:
            source(string): 转发哪条消息。默认 "quoted"=被引用的那条（用户先引用、再让你转发时用这个）；"current"=当前这条消息本身（仅在用户说"把这条/我这句话发出去"且消息里没有引用时用）。
            scope(string): 目标策略，四选一。"all"=所有可投群；"only"=只发 group_ids 里列的群；"except"=发所有可投群但排除 group_ids 里列的群；"here"=只发本群。
            group_ids(string): 群号，多个用逗号分隔，例如 "1017786877,1073672548"。scope 为 "only" 或 "except" 时必填。只填数字群号，不要填群名。

        Returns:
            执行结果的 JSON 字符串（含 ok / message 等字段），供你组织回复，不会直接发给用户。
        """
        if not bool(self._get("enable_llm_tool", True)):
            return self._reject(E_DISABLED)
        return await self._do_forward(event, source, scope, group_ids)

    @filter.llm_tool(name="list_forward_targets")
    async def list_forward_targets(self, event: AstrMessageEvent) -> str:
        """实时读取「群名 ↔ 群号」对照表。

        当用户用群名（如"哲学群"）而不是群号指定目标时，**先调用本工具查表**，
        再把查到的群号填进 forward_message 的 group_ids。
        available=false 的群当前不可投递（reason 说明原因）。
        本工具只把表返回给你（不会直接发消息到群里），请据此用自己的话回复用户。

        Returns:
            群表 JSON 字符串（groups 数组含 id / name / available / reason）。
        """
        try:
            all_groups, reachable, blocked = await self._snapshot(event)
        except Exception:
            logger.exception("[forward] list_forward_targets 取群失败")
            return self._reject(E_GROUP_LIST_FAILED)
        origin = event.get_group_id() or ""
        items = sorted(
            (
                {
                    "id": gid,
                    "name": name,
                    "available": gid in set(reachable),
                    "reason": blocked.get(gid, ""),
                }
                for gid, name in all_groups.items()
            ),
            key=lambda x: x["id"],
        )
        available_count = len([g for g in reachable if g != origin])
        return json.dumps(
            {
                "ok": True,
                "count": len(items),
                "available_count": available_count,
                "current_group_id": origin,
                "groups": items,
                "message": "机器人共在 %d 个群，其中 %d 个当前可投递。"
                % (len(items), available_count),
            },
            ensure_ascii=False,
        )

    @filter.command("转发")
    async def cmd_forward(self, event: AstrMessageEvent, scope: str = "here",
                          group_ids: str = ""):
        """转发被引用的消息。/转发 <all|only|except|here> [群号,逗号分隔]"""
        # 命令是用户直接发起的，回执**应该**可见（与 LLM 工具相反）
        if not bool(self._get("enable_command", True)):
            yield event.plain_result(self._reject(E_DISABLED))
            return
        yield event.plain_result(
            await self._do_forward(event, "quoted", scope, group_ids)
        )

    # ------------------------------------------------------------ 目标解析

    async def _snapshot(self, event):
        """**实时**拉取群表。

        Returns:
            (all_groups, reachable, blocked_reason)
            - all_groups: {群号: 群名}，机器人当前所在的所有群
            - reachable:  **列表**，配置层允许投递的群号，保持 get_group_list 的返回顺序
                          （顺序确定 → 投递顺序与回执可复现）
            - blocked_reason: {群号: 原因}，不可投递的群及其原因（供 AI 解释）
        """
        platform_id = event.get_platform_id()
        bot = getattr(self.context.get_platform_inst(platform_id), "bot", None)
        if bot is None:
            return {}, [], {}
        groups = await bot.get_group_list()
        all_groups = {
            str(g.get("group_id")): str(g.get("group_name") or g.get("group_id"))
            for g in groups
        }
        cfg_scope = str(self._get("broadcast_scope", "allowlist")).strip().lower()
        allow = {str(x) for x in (self._get("group_allowlist", []) or [])}
        deny = {str(x) for x in (self._get("group_denylist", []) or [])}
        reachable, blocked = [], {}
        for gid in all_groups:          # dict 保序 → 与 get_group_list 同序
            if gid in deny:
                blocked[gid] = "在黑名单里"
            elif cfg_scope == "allowlist" and gid not in allow:
                blocked[gid] = "不在白名单里"
            else:
                reachable.append(gid)
        return all_groups, reachable, blocked

    async def _resolve_targets(self, event, scope, group_ids_raw, origin):
        """按 scope + group_ids 算出请求级目标集合。

        Returns:
            (ids, names, error) —— names 为 {群号: 群名}，供回执说人话用。
        """
        all_groups, reachable, _ = await self._snapshot(event)
        names = all_groups

        if scope == "here":
            return ([origin] if origin else []), names, None

        if scope == "all":
            return [g for g in reachable if g != origin], names, None

        tokens = [
            t for t in re.split(r"[,，\s]+", str(group_ids_raw or "")) if t.strip()
        ]
        if not tokens:
            return [], names, (E_BAD_ARG, "scope=%s 时必须填写 group_ids" % scope)

        ids, err = validate_group_ids(tokens, all_groups, set(reachable))
        if err:
            return [], names, err

        if scope == "only":
            return [i for i in ids if i != origin], names, None
        # except：从可投群里排除填写的
        exclude = set(ids)
        return [g for g in reachable if g not in exclude and g != origin], names, None

    # ------------------------------------------------------------ 内容检查

    def _content_check(self, chain):
        """L2 规则层检查。返回 None 或 (错误码, 人话原因)。

        ⚠️ 这是**规则层拦截，不是语义审核**：能挡明显广告刷屏，挡不住措辞委婉的违规内容。
        """
        text = _plain_text(chain)
        kws = [str(k) for k in (self._get("content_deny_keywords", []) or []) if str(k)]
        for k in kws:
            if k and k in text:
                return E_CONTENT_BLOCKED, "内容命中了拦截关键词「%s」。" % k
        n_url = len(_URL_RE.findall(text))
        max_url = int(self._get("content_max_urls", 3))
        if max_url >= 0 and n_url > max_url:
            return E_CONTENT_BLOCKED, "内容含 %d 个链接，超过上限 %d，疑似广告。" % (
                n_url, max_url)
        n_at = sum(1 for c in chain if type(c).__name__ == "At")
        max_at = int(self._get("content_max_ats", 5))
        if max_at >= 0 and n_at > max_at:
            return E_CONTENT_BLOCKED, "内容 @ 了 %d 人，超过上限 %d。" % (n_at, max_at)
        max_len = int(self._get("content_max_text_len", 2000))
        if max_len >= 0 and len(text) > max_len:
            return E_CONTENT_BLOCKED, "内容过长（%d 字），超过上限 %d。" % (
                len(text), max_len)
        return None

    # ------------------------------------------------------------ 核心

    async def _do_forward(self, event, source, scope_raw, group_ids_raw=""):
        if event.get_platform_name() != "aiocqhttp":
            return self._reject(E_UNSUPPORTED_PLATFORM)
        if not self._enabled:
            return self._reject(E_DISABLED)

        scope = parse_scope(scope_raw)
        if scope is None:
            return self._reject(
                E_BAD_ARG,
                detail="scope 只接受 all / only / except / here，收到「%s」" % scope_raw,
            )

        # G6 权限
        if scope == "all" and bool(self._get("admin_only_broadcast", True)) \
                and not event.is_admin():
            return self._reject(E_PERMISSION_DENIED)

        # G1 载荷
        chain = self._extract(event, source)
        if chain is None:
            return self._reject(E_NO_REPLY_CHAIN)
        if not chain:
            return self._reject(E_EMPTY)

        # 防「把转发指令本身当内容发出去」（实机事故的护栏）
        if (
            not self._is_quoted_source(source)
            and self._looks_like_instruction(event, chain)
        ):
            return self._reject(
                E_BAD_ARG,
                detail=(
                    "当前消息看起来只是转发指令（只有一句「%s」），"
                    "而消息里引用了别的内容。请改用 source=\"quoted\" 转发被引用的那条。"
                    % _plain_text(chain).strip()[:30]
                ),
            )

        # L2 内容规则
        hit = self._content_check(chain)
        if hit:
            return self._reject(hit[0], detail=hit[1])

        origin = event.get_group_id() or ""
        platform_id = event.get_platform_id()
        sender = str(event.get_sender_id())
        now = time.time()
        fp = _fingerprint(chain)
        anchors = _anchors(chain)

        # 目标集 T(o)
        try:
            targets, names, err = await self._resolve_targets(
                event, scope, group_ids_raw, origin
            )
        except Exception:
            logger.exception("[forward] 解析目标失败")
            return self._reject(E_GROUP_LIST_FAILED)
        if err:
            return self._reject(err[0], detail=err[1])
        if not targets:
            why = ("请求级筛选后为空"
                   if scope in ("only", "except")
                   else "配置层白名单为空或无其他群")
            hint = "若需广播到所有群，请在插件配置中将 broadcast_scope 设为 all 或在 group_whitelist 中填入目标群" if scope == "all" else ""
            return self._reject(E_NO_TARGET, detail="（%s）" % why, hint=hint)
        truncated = False
        if len(targets) > self._max_fanout:
            targets = targets[: self._max_fanout]
            truncated = True

        # ★ 原子段：G8 深度 → G2 查重 → G3 冷却 → 单用户频次 → 占位 IN_FLIGHT
        async with self._lock:
            records = self._live_records(now)
            depth = self._hop_depth(records, anchors, origin)
            if depth > self._max_hop:
                return self._reject(E_HOP_LIMIT, depth=depth)

            dup = self._find_duplicate(records, fp, anchors)
            if dup is not None:
                return self._reject(
                    E_DUPLICATE, epoch=dup.get("epoch"), state=dup.get("state")
                )

            left = self._cooldown - (now - self._last_broadcast)
            if left > 0:
                return self._reject(E_RATE_LIMITED, retry_after=int(left) + 1)

            hits = self._sender_hits(sender, now)
            if len(hits) >= self._sender_max:
                wait = int(self._sender_window - (now - float(hits[0]))) + 1
                return self._reject(
                    E_SENDER_FLOODING,
                    detail="你在 %d 秒内已请求 %d 次，上限 %d 次。"
                           % (self._sender_window, len(hits), self._sender_max),
                    retry_after=max(1, wait),
                )

            record = {
                "fp": fp,
                "anchors": sorted(anchors)[:20],
                "epoch": uuid.uuid4().hex[:8],
                "targets": list(targets),
                "scope": scope,
                "t0": now,
                "ttl": self._ttl,
                "depth": depth,
                "state": "IN_FLIGHT",
            }
            self._state.setdefault("records", []).append(record)
            self._state.setdefault("sender_hits", {}).setdefault(sender, []).append(now)
            self._last_broadcast = now
            self._save_state()

        # 锁外执行外部副作用（D4）
        sent, failed, unknown = [], [], []
        for g in targets:
            session = build_session(platform_id, g)
            try:
                ok = await asyncio.wait_for(
                    self.context.send_message(session, self._chain_of(chain)),
                    timeout=self._timeout,
                )
                (sent if ok else unknown).append(g)
            except asyncio.TimeoutError:
                unknown.append(g)
            except Exception as e:  # noqa: BLE001
                failed.append({"group": g, "code": type(e).__name__, "msg": str(e)[:120]})
            if self._interval:
                await asyncio.sleep(self._interval)

        record["state"] = "COMPLETED" if not (failed or unknown) else "PARTIAL"
        self._save_state()

        return self._ok_receipt(
            record, sent, failed, unknown, truncated, scope, names
        )

    # ------------------------------------------------------------ 辅助

    @staticmethod
    def _chain_of(chain):
        from astrbot.api.event import MessageChain

        return MessageChain(chain=list(chain))

    @staticmethod
    def _is_quoted_source(source) -> bool:
        """空值/未填 一律按 quoted 处理（quoted 是安全默认：转被引用的那条）。"""
        return str(source or "").strip().lower() in ("", "quoted", "引用", "reply")

    def _extract(self, event: AstrMessageEvent, source: str):
        msgs = event.get_messages()
        if self._is_quoted_source(source):
            for c in msgs:
                if isinstance(c, Reply):
                    return list(getattr(c, "chain", None) or []) or None
            return None
        return [c for c in msgs if not isinstance(c, (Reply, At))]

    def _reply_chain(self, event):
        for c in event.get_messages():
            if isinstance(c, Reply):
                return list(getattr(c, "chain", None) or []) or None
        return None

    def _looks_like_instruction(self, event, chain) -> bool:
        """判定「当前消息其实只是转发指令」。

        实机事故（2026-10-14）：用户引用 B 站卡片 + 发"@羊魔 帮我发到山根王国"，
        AI 传了 source="current"，结果**把指令文本本身转发出去**，卡片没发。

        条件（三者同时成立才判定，尽量少误伤）：
          1. 消息里引用了别的内容（有非空的 Reply.chain）
          2. 当前链里没有任何富媒体（图片/卡片/合并转发/文件/视频/语音）
          3. 当前链的文字很短（像一句指令，而不是一段要转发的内容）
        """
        if not self._reply_chain(event):
            return False
        rich = {
            "Image", "Json", "Forward", "Record", "Video", "File", "Nodes", "Node",
        }
        if any(type(c).__name__ in rich for c in chain or []):
            return False
        return len(_plain_text(chain).strip()) <= 60

    def _ok_receipt(self, record, sent, failed, unknown, truncated, scope, names):
        names = names or {}
        shown = "、".join(names.get(g, g) for g in sent[:5])
        if not sent and (failed or unknown):
            human = "转发失败：没有群收到这条消息。"
        else:
            human = "已转发到 %d 个群" % len(sent)
            if shown:
                human += "：%s" % shown
            if failed or unknown:
                human += "（%d 个未成功）" % (len(failed) + len(unknown))
            human += "。"
        return json.dumps(
            {
                "ok": True,
                "epoch": record["epoch"],
                "scope": scope,
                "depth": record["depth"],
                "total": len(record["targets"]),
                "sent": sent,
                "failed": failed,
                "unknown": unknown,
                "truncated": truncated,
                "message": human,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _reject(code: str, detail: str = "", **extra) -> str:
        text = _MESSAGES.get(code, "操作未执行。")
        if detail:
            text = text.rstrip("。") + "：" + detail
        payload = {"ok": False, "error": code, "message": text}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)
