"""astrbot_plugin_maaguard

MAA 日志分析"守门"插件，配合独立运行的 OnebotMaaLogAnalyzer（并存部署）使用：

- 会触发 analyzer 的消息（日志压缩包上传、/maa 指令、引用分析结果的追问）
  一律静默拦截 AstrBot，避免同一身份双重回复；
- 门槛模式（默认开启）：群内的 zip/文件上传先交给模型判定是否想分析日志，
  判定为是才把文件转发给 analyzer 的本地 gate API；判定为否静默放行；
  模型不可用或 analyzer 拒绝时自动回退旧规则（文件名前缀匹配 + 格式提醒）；
- QQ 一次文件上传会产生 notice 与 file 消息两个事件，处理按 (群, 文件名) 去重；
- 可选：配置 analyzer_config_path 后，监听群与文件名前缀直接以 analyzer
  的 config.json 为准（读取失败自动回退本地配置）。
"""

import asyncio
import json
import time

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

MATCH = "match"
NEAR_MISS = "near_miss"
IGNORE = "ignore"

# 同一 (群, 文件名) 的去重窗口： notice 与 file 消息会先后到达
REMINDER_DEDUP_SECONDS = 120

# 除 .zip 外常见的压缩包后缀，命中即视为"可能是日志包但格式不对"
ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2")

REMINDER = (
    "看起来你想分析 MAA 日志，但这样触发不了。正确做法：\n"
    "1. 直接上传 Maa 自动生成的日志压缩包，文件名形如 {example}，不要改名；\n"
    "2. 必须是 .zip（不要解压后分开发，也不要再压成 7z/rar）；\n"
    "3. 上传后无需 @ 或发指令，我会自动分析并把结论发回群里；\n"
    "4. 若格式正确仍无反应，可能是文件过大超限，或本群未开通监听，请联系管理员。"
)

# 门槛模式下的提醒：analyzer 明确拒绝 / 联系不上时发送
GATE_REJECT_REMINDER = (
    "看起来你想分析 MAA 日志，但分析工具处理不了这个文件：{reason}。"
    "也可以联系管理员看看。"
)
GATE_DOWN_REMINDER = (
    "看起来你想分析 MAA 日志，但分析工具暂时联系不上（{detail}），"
    "请稍后重试或联系管理员。"
)

# 上传意图判定提示词：只要求输出 YES / NO，便于严格解析
INTENT_JUDGE_PROMPT = """你在一个 QQ 群里，群里的机器人提供「MaaNikke 自动化脚本日志分析」服务：群友上传日志压缩包，机器人下载、提取报错并用 AI 分析原因。

现在有人上传了一个文件，请判断上传者是否希望机器人分析日志、帮忙排查问题。

文件信息：
- 文件名：{file_name}
- 大小：{size}{caption}

判断为 YES 的典型信号：文件名含 log / 日志 / debug / maa / error / crash 等，或附言里请求帮忙看日志、分析报错、任务为什么失败、帮我看看等。
判断为 NO 的典型情况：与日志排查明显无关的分享（安装包、资源包、图片包、文档、存档等），且附言没有求助分析的意思。
难以判断时按 NO 处理。

只回答 YES 或 NO，不要输出任何其他内容。"""


def _human_size(value) -> str:
    """把字节数格式化为可读大小；未知返回「未知」。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "未知"
    if n <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def _normalize_prefixes(prefixes) -> tuple[str, ...]:
    """把前缀参数统一成非空元组（接受单个字符串或可迭代对象）。"""
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    return tuple(p for p in prefixes if p)


def classify_upload(filename: str, prefixes) -> str:
    """判断文件名与 analyzer 触发条件的关系：match / near_miss / ignore。

    prefixes 支持多个前缀（analyzer 多配置各自的前缀）。
    """
    prefix_tuple = _normalize_prefixes(prefixes)
    name = (filename or "").strip()
    if not name:
        return IGNORE
    lower = name.lower()
    if prefix_tuple and any(name.startswith(p) for p in prefix_tuple) \
            and lower.endswith(".zip"):
        return MATCH
    looks_like_log = (
        "log" in lower
        or "maa" in lower
        or lower.endswith((".log", ".txt"))
        or any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)
    )
    return NEAR_MISS if looks_like_log else IGNORE


class MaaGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._reminder_sent: dict[tuple, float] = {}

    def _cfg(self, key: str, default):
        if not self.config:
            return default
        value = self.config.get(key, default)
        return default if value is None else value

    # ── 本地配置（回退用） ──────────────────────────────

    def _local_prefix(self) -> str:
        return str(self._cfg("file_prefix", "MaaXXX-logs")).strip() or "MaaXXX-logs"

    def _local_listen_groups(self) -> set[str]:
        groups = self._cfg("listen_groups", []) or []
        return {str(g).strip() for g in groups if str(g).strip()}

    def _command_prefix(self) -> str:
        cmd = str(self._cfg("command_prefix", "/maa")).strip() or "/maa"
        return cmd if cmd.startswith("/") else "/" + cmd

    def _analysis_keywords(self) -> list[str]:
        keywords = self._cfg("analysis_header_keywords", []) or []
        return [str(k) for k in keywords if str(k).strip()]

    def _intent_keywords(self) -> list[str]:
        keywords = self._cfg("intent_keywords", []) or []
        return [str(k) for k in keywords if str(k).strip()]

    # ── 门槛模式 ────────────────────────────────────────

    def _gate_enabled(self) -> bool:
        return bool(self._cfg("gate_enabled", True))

    def _gate_analyzer_url(self) -> str:
        url = str(self._cfg("gate_analyzer_url",
                            "http://127.0.0.1:8090") or "").strip()
        return url or "http://127.0.0.1:8090"

    def _gate_token(self) -> str:
        return str(self._cfg("gate_token", "") or "").strip()

    # ── analyzer 配置同步（可选） ────────────────────────

    def _analyzer_derived(self) -> tuple[set[str], set[str]]:
        """读取 analyzer 的 config.json，返回 (文件名前缀集合, 监听群集合)。

        未配置路径或读取失败时返回空集，调用方据此回退本地配置。
        """
        path = str(self._cfg("analyzer_config_path", "") or "").strip()
        if not path:
            return set(), set()
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            configs = (cfg.get("bot") or {}).get("configs") or {}
            prefixes: set[str] = set()
            groups: set[str] = set()
            for conf in configs.values():
                if not isinstance(conf, dict):
                    continue
                prefix = str((conf.get("settings") or {}).get("file_prefix") or "").strip()
                if prefix:
                    prefixes.add(prefix)
                for g in (conf.get("listen_groups") or []):
                    g = str(g).strip()
                    if g:
                        groups.add(g)
            if not prefixes and not groups:
                logger.warning(f"[maaguard] {path} 中未找到可用配置，回退本地配置")
            return prefixes, groups
        except Exception as exc:
            logger.warning(f"[maaguard] 读取 analyzer 配置失败，回退本地配置: {exc}")
            return set(), set()

    def _prefixes(self) -> set[str]:
        analyzer_prefixes, _ = self._analyzer_derived()
        return analyzer_prefixes if analyzer_prefixes else {self._local_prefix()}

    def _listen_groups(self) -> set[str]:
        _, analyzer_groups = self._analyzer_derived()
        return analyzer_groups if analyzer_groups else self._local_listen_groups()

    # ── 判定与提醒 ──────────────────────────────────────

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        """只在生效的监听群内处理；未配置任何群时插件整体不生效。"""
        groups = self._listen_groups()
        if not groups:
            return False
        group_id = event.get_group_id()
        return group_id is not None and str(group_id) in groups

    def _reminder_recently_sent(self, key: tuple) -> bool:
        """同一 key 在 REMINDER_DEDUP_SECONDS 内只提醒一次。"""
        now = time.monotonic()
        expired = [k for k, ts in self._reminder_sent.items()
                   if now - ts > REMINDER_DEDUP_SECONDS]
        for k in expired:
            self._reminder_sent.pop(k, None)
        if key in self._reminder_sent:
            return True
        self._reminder_sent[key] = now
        return False

    def _at_chain(self, event: AstrMessageEvent, text: str) -> list:
        return [Comp.At(qq=event.get_sender_id()), Comp.Plain(text)]

    def _reminder_chain(self, event: AstrMessageEvent) -> list:
        example = f"{sorted(self._prefixes())[0]}-20260928-120000.zip"
        return self._at_chain(event, REMINDER.format(example=example))

    # ── 门槛模式：模型判定 + 转发 analyzer ───────────────

    @staticmethod
    def _extract_uploads(event: AstrMessageEvent, raw: dict) -> list:
        """统一抽取群文件上传元数据（notice 与 file 消息两个入口）。"""
        uploads: list[dict] = []
        if raw.get("post_type") == "notice":
            # group_upload 之外的 notice（如 poke）不处理
            if raw.get("notice_type") != "group_upload":
                return uploads
            f = raw.get("file") if isinstance(raw.get("file"), dict) else {}
            uploads.append({
                "name": str(f.get("name") or f.get("file_name") or ""),
                "size": f.get("size") or f.get("file_size") or 0,
                "file_id": str(f.get("id") or f.get("file_id") or ""),
                "url": str(f.get("url") or ""),
            })
            return uploads

        chain = event.message_obj.message or []
        files = [c for c in chain if isinstance(c, Comp.File)]
        if not files:
            return uploads
        # AstrBot 转换后 Comp.File 只剩 name/url，file_id 与大小从原始消息段补
        raw_files = [
            m.get("data") or {}
            for m in (raw.get("message") or [])
            if isinstance(m, dict) and m.get("type") == "file"
        ]
        for i, c in enumerate(files):
            data = raw_files[i] if i < len(raw_files) else {}
            uploads.append({
                "name": str(getattr(c, "name", "") or ""),
                "size": data.get("file_size") or data.get("size") or 0,
                "file_id": str(data.get("file_id") or ""),
                "url": str(getattr(c, "url", "") or data.get("url") or ""),
            })
        return uploads

    async def _judge_upload_intent(self, event: AstrMessageEvent,
                                   meta: dict,
                                   message_str: str) -> bool | None:
        """调用 AstrBot 配置的模型判定上传意图。返回 True/False，失败为 None。"""
        try:
            provider = await self.context.get_using_provider_async()
        except Exception as exc:
            logger.warning(f"[maaguard] 获取 LLM provider 失败: {exc}")
            return None
        if provider is None:
            logger.warning("[maaguard] 未配置对话模型，门槛判定不可用")
            return None

        caption = f"\n- 附言：{message_str}" if message_str else ""
        prompt = INTENT_JUDGE_PROMPT.format(
            file_name=meta.get("name") or "未知",
            size=_human_size(meta.get("size")),
            caption=caption,
        )
        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt),
                                          timeout=60)
            text = (getattr(resp, "completion_text", "") or "").strip().upper()
        except Exception as exc:
            logger.warning(f"[maaguard] 门槛判定调用失败: {exc}")
            return None

        if text.startswith("YES"):
            return True
        if text.startswith("NO"):
            return False
        logger.warning(f"[maaguard] 门槛判定输出无法解析: {text[:50]!r}")
        return None

    async def _forward_to_analyzer(self, meta: dict, group_id: str,
                                   event: AstrMessageEvent) -> tuple[str, str]:
        """把判定为日志意图的上传转发给 analyzer 的本地 gate API。

        返回 (status, reason)：ok / duplicate / rejected / error。
        """
        url = self._gate_analyzer_url().rstrip("/") + "/analyze"
        payload = {
            "group_id": group_id,
            "file_name": str(meta.get("name") or ""),
            "file_id": str(meta.get("file_id") or ""),
            "size": int(meta.get("size") or 0),
            "uploader": str(event.get_sender_id() or ""),
        }
        headers = {}
        token = self._gate_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, json=payload, headers=headers) as resp:
                    data: dict = {}
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        pass
                    status = str((data or {}).get("status") or "")
                    reason = str((data or {}).get("reason") or "")
                    if resp.status in (200, 202) and status in ("ok", "duplicate"):
                        return status, reason
                    if status == "rejected":
                        return "rejected", reason or "分析工具拒绝了该文件"
                    return "error", f"HTTP {resp.status}"
        except Exception as exc:
            return "error", str(exc)[:120]

    async def _handle_gate_uploads(self, event: AstrMessageEvent,
                                   uploads: list, group_id: str,
                                   message_str: str):
        """门槛模式主流程：模型判定 → 转发 analyzer / 静默 / 回退旧规则。"""
        for meta in uploads:
            name = str(meta.get("name") or "")
            if not name:
                continue
            # notice 与 file 双入口去重：第二个入口直接静默
            if self._reminder_recently_sent(("gate", group_id, name)):
                continue

            decision = await self._judge_upload_intent(event, meta, message_str)
            if decision is None:
                # 模型不可用：回退旧规则。match 必须转发——门槛模式下
                # analyzer 不会按前缀自动触发，不转就丢了这个包
                result = classify_upload(name, self._prefixes())
                if result == MATCH:
                    decision = True
                elif result == NEAR_MISS:
                    if not self._reminder_recently_sent(("file", group_id, name)):
                        logger.info(f"[maaguard] 模型不可用，旧规则提醒: {name}")
                        yield event.chain_result(self._reminder_chain(event))
                    continue
                else:
                    continue  # 与日志无关：静默

            if not decision:
                logger.info(f"[maaguard] 门槛判定 NO: {name}，静默")
                continue

            status, reason = await self._forward_to_analyzer(
                meta, group_id, event)
            if status in ("ok", "duplicate"):
                logger.info(f"[maaguard] 已转发 analyzer: {name} ({status})")
            elif status == "rejected":
                logger.info(f"[maaguard] analyzer 拒绝 {name}: {reason}")
                yield event.chain_result(self._at_chain(
                    event, GATE_REJECT_REMINDER.format(reason=reason)))
            else:
                logger.warning(f"[maaguard] analyzer 不可达: {reason}")
                yield event.chain_result(self._at_chain(
                    event, GATE_DOWN_REMINDER.format(detail=reason)))

    def _is_analysis_quote(self, event: AstrMessageEvent, replies: list) -> bool:
        """被引消息是否为本 Bot 发出的分析结果（analyzer 会接管这类追问）。"""
        self_id = str(getattr(event.message_obj, "self_id", "") or "")
        keywords = self._analysis_keywords()
        if not self_id or not keywords:
            return False
        for r in replies:
            quoted_sender = str(
                getattr(r, "sender_id", "") or getattr(r, "qq", "") or ""
            )
            quoted_text = str(
                getattr(r, "message_str", "") or getattr(r, "text", "") or ""
            )
            if quoted_sender == self_id and any(k in quoted_text for k in keywords):
                return True
        return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def guard_group_message(self, event: AstrMessageEvent):
        if not self._in_scope(event):
            return

        raw = event.message_obj.raw_message or {}
        chain = event.message_obj.message or []
        message_str = (event.message_str or "").strip()
        group_id = str(event.get_group_id())
        prefixes = self._prefixes()

        # 0) 门槛模式：文件上传先由模型判定意图，再决定是否转发 analyzer
        if self._gate_enabled():
            uploads = self._extract_uploads(event, raw)
            if uploads:
                async for result in self._handle_gate_uploads(
                    event, uploads, group_id, message_str
                ):
                    yield result
                # 无论判定结果如何，含文件的事件都不再由 AstrBot 回复
                event.stop_event()
                return

        # 1) 通知类事件：群文件上传（analyzer 只关心 group_upload）
        if raw.get("post_type") == "notice":
            if raw.get("notice_type") == "group_upload":
                file_info = raw.get("file") or {}
                filename = str(file_info.get("name") or "")
                result = classify_upload(filename, prefixes)
                if result == NEAR_MISS and not self._reminder_recently_sent(
                    ("file", group_id, filename)
                ):
                    logger.info(f"[maaguard] 疑似日志包(通知): {filename}，发送提醒")
                    yield event.chain_result(self._reminder_chain(event))
                # match 交给 analyzer；ignore 也无须 AstrBot 搭话，统一静默
                event.stop_event()
            return

        # 2) /maa 系列指令归 analyzer 处理
        if message_str.startswith(self._command_prefix()):
            logger.info(f"[maaguard] 拦截 {self._command_prefix()} 指令，交给 analyzer")
            event.stop_event()
            return

        # 3) 消息内的文件段
        files = [c for c in chain if isinstance(c, Comp.File)]
        if files:
            results = [
                classify_upload(str(getattr(f, "name", "") or ""), prefixes)
                for f in files
            ]
            if MATCH in results:
                logger.info("[maaguard] 命中日志包(file 消息)，静默拦截交给 analyzer")
                event.stop_event()
                return
            if NEAR_MISS in results:
                filename = str(getattr(files[results.index(NEAR_MISS)], "name", "") or "")
                if not self._reminder_recently_sent(("file", group_id, filename)):
                    logger.info(f"[maaguard] 疑似日志包(file 消息): {filename}，发送提醒")
                    yield event.chain_result(self._reminder_chain(event))
                event.stop_event()
            # 与日志无关的普通文件：不拦截
            return

        # 4) 引用 analyzer 分析结果继续追问
        replies = [c for c in chain if isinstance(c, Comp.Reply)]
        if replies:
            if self._is_analysis_quote(event, replies):
                logger.info("[maaguard] 拦截对分析结果的引用追问，交给 analyzer")
                event.stop_event()
            return

        # 5) 纯文本表达了分析日志的意图，但没传文件
        if message_str and not message_str.startswith("/"):
            if any(k in message_str for k in self._intent_keywords()):
                key = ("text", group_id, str(event.get_sender_id()), message_str[:64])
                if not self._reminder_recently_sent(key):
                    logger.info(f"[maaguard] 文本意图: {message_str[:30]}...，发送提醒")
                    yield event.chain_result(self._reminder_chain(event))
                event.stop_event()
