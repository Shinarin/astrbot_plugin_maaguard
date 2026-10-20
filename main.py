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
import os
import re
import time
from difflib import SequenceMatcher

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_README_PATH = os.path.join(PLUGIN_DIR, "knowledge", "maanikke_readme.md")
DEFAULT_ISSUES_PATH = os.path.join(PLUGIN_DIR, "knowledge", "maanikke_issues.md")

ISSUES_HEADER = """# MaaNikke 问题与解决档案（群聊沉淀）

> 由 astrbot_plugin_maaguard 自动维护：监听群内确认的 MaaNikke 问题与解决方法会自动追加到这里；
> 也可以手动补充/修改，保存后按 mtime 自动生效。最新条目在文件末尾。
"""

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
# 门槛判定为 NO、但文件名像日志包时的轻提示（指导正确打包，语气比旧提醒轻）
GATE_HINT = (
    "如果这是想分析 MAA 日志：把 MaaNikke 目录下的 debug 文件夹整个压成 1 个 "
    "zip 发上来即可——不用改名，也别用软件自带导出（会拆成多个包）；"
    "如果是 agent（Python 侧）的问题，改压 logs 文件夹。"
    "最好带一句「帮忙分析日志」。"
)

# ── MaaNikke 客服 ─────────────────────────────────────

# 命中这些关键词的群消息，才会注入 README 知识。
# 默认空列表 = 监听群内所有消息都注入（专用群推荐；关键词筛选反而漏掉
# "为什么卡住""点不动"这类描述不准的提问）。
DEFAULT_CS_KEYWORDS: list = []

CS_SYSTEM_PROMPT = """

【MaaNikke 客服模式】群友可能在询问 MaaNikke（《胜利女神：NIKKE》每日任务自动化工具）的使用问题。请优先根据下面的 README 资料和《问题与解决档案》准确回答——档案里是群友真实遇到过的问题和确认有效的解决方法，新问题优先参考其中的既有结论；资料都没覆盖的复杂问题（节点行为、选项含义、源码逻辑、报错原因），请调用工具 maanikke_source_search（在源码仓库中搜索关键词）和 maanikke_source_read（读取仓库内指定文件）查证后再回答。回答要求：像群里的客服，口语化、先给结论再给步骤，不要整段照搬资料；如果群友的问题其实与 MaaNikke 无关，正常聊天即可，不要硬套资料。

【无日志不猜】如果群友在问"这个怎么回事""为什么失败/卡住/报错"这类运行异常，但既没有上传日志包，消息里也没有足够的信息判断原因：不要编造原因或给笼统答案，请他把日志压缩包发到群里——把 MaaNikke 目录下的 debug 文件夹压成一个 zip 直接上传即可（不用改名），发上来后会自动分析并把结论发回群里；如果是 agent（Python 侧）的问题，则推荐打包 logs 文件夹。也可以先告诉他日志位置（logs/ 目录下的 log-*.log、maafw.log 和 vision/ 截图）。如果群友附了截图或具体描述，可以先基于可见信息做初步解答，但准确分析仍建议发日志包。

【日志分析工具】如果群友要求分析一个已经存在的日志文件（刚才上传过的、或群文件里的，比如"帮我分析刚才那个 debug.zip""分析群文件里最新的日志包"），调用工具 maa_analyze_log（file_name 传文件名，不确定就留空让它自动选最新的日志包），不要让他重复上传已有的文件。调用成功后告诉群友"分析已开始，结果稍候发群里"。

【MaaNikke README 资料】
{knowledge}"""

# 从群聊问答中提取 FAQ 条目的判定提示词：要求严格，宁缺毋滥
FAQ_EXTRACT_PROMPT = """你在整理 MaaNikke（《胜利女神：NIKKE》每日任务自动化工具）的用户问题档案。下面是 QQ 群里的一段问答：

【群友提问】
{question}

【客服回答】
{answer}

判定这段问答是否值得入库，必须同时满足：
1. 群友遇到的是 MaaNikke 使用中的具体问题、bug 或异常；
2. 回答给出了明确的解决方法、结论或可操作的排查步骤。

以下情况一律不入库：闲聊、与 MaaNikke 无关的话题、回答只是在追问信息（如"请发一下日志"）、没有形成结论的讨论、README 里已有的基础问题。

值得入库时，严格按以下格式输出（不要输出其他任何内容）：
### <一句话问题标题>
- 解决：<解决方法，一两句话写清关键操作>

不值得入库时，只输出：SKIP"""

# 源码搜索时跳过的目录与二进制后缀
_SKIP_DIRS = {".git", "runtimes", "libs", "node_modules", "__pycache__", ".vs", ".idea"}
_SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".exe", ".dll",
    ".so", ".dylib", ".zip", ".7z", ".rar", ".onnx", ".bin", ".pyc", ".pdb",
    ".msi", ".woff", ".woff2", ".ttf", ".mp4", ".mkv",
}
_MAX_FILE_BYTES = 512 * 1024
_SEARCH_RESULT_MAX_CHARS = 6000

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


def looks_log_related(filename: str) -> bool:
    """文件名是否有较强日志相关信号（用于门槛 NO 时的轻提示判断）。

    比 classify_upload 的 near_miss 更严格：仅仅"是个压缩包"不算，
    必须名字里带 log/日志/maa/debug/error/crash 或日志类后缀。
    """
    lower = (filename or "").strip().lower()
    if not lower:
        return False
    return (
        "log" in lower
        or "日志" in lower
        or "maa" in lower
        or "debug" in lower
        or "error" in lower
        or "crash" in lower
        or lower.endswith((".log", ".txt"))
    )


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

    def _skip_file_keywords(self) -> list[str]:
        keywords = self._cfg("skip_file_keywords", []) or []
        return [str(k).lower() for k in keywords if str(k).strip()]

    def _skip_uploader_ids(self) -> set[str]:
        ids = self._cfg("skip_uploader_ids", []) or []
        return {str(i).strip() for i in ids if str(i).strip()}

    def _should_skip_upload(self, meta: dict, event: AstrMessageEvent) -> bool:
        """免分析名单：命中即完全静默——不调模型判定、不转发 analyzer、不发提醒。"""
        name = str(meta.get("name") or "").lower()
        if name and any(k in name for k in self._skip_file_keywords()):
            return True
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        if sender and sender in self._skip_uploader_ids():
            return True
        return False

    # ── MaaNikke 客服 ────────────────────────────────────

    def _cs_enabled(self) -> bool:
        return bool(self._cfg("cs_enabled", True))

    def _cs_keywords(self) -> list[str]:
        keywords = self._cfg("cs_keywords", DEFAULT_CS_KEYWORDS) or []
        return [str(k).lower() for k in keywords if str(k).strip()]

    def _cs_repo_path(self) -> str:
        path = str(self._cfg("cs_repo_path", "/opt/maanikke-src") or "").strip()
        return path or "/opt/maanikke-src"

    def _cs_readme_path(self) -> str:
        return str(self._cfg("cs_readme_path", "") or "").strip() \
            or DEFAULT_README_PATH

    def _cs_issues_path(self) -> str:
        return str(self._cfg("cs_issues_path", "") or "").strip() \
            or DEFAULT_ISSUES_PATH

    def _cs_faq_enabled(self) -> bool:
        return bool(self._cfg("cs_faq_enabled", True))

    def _cs_faq_max_entries(self) -> int:
        try:
            return max(1, int(self._cfg("cs_faq_max_entries", 200) or 200))
        except (TypeError, ValueError):
            return 200

    def _load_cs_knowledge(self) -> str:
        """读取 README 知识库 + 群聊沉淀的问题档案（按 mtime 缓存）。"""
        readme_path = self._cs_readme_path()
        issues_path = self._cs_issues_path()
        try:
            readme_mtime = os.path.getmtime(readme_path)
        except OSError:
            logger.warning(f"[maaguard] 客服知识库不存在: {readme_path}")
            return ""
        try:
            issues_mtime = os.path.getmtime(issues_path)
        except OSError:
            issues_mtime = None
        cached = getattr(self, "_cs_knowledge_cache", None)
        if cached and cached[0] == readme_path and cached[1] == readme_mtime \
                and cached[2] == issues_path and cached[3] == issues_mtime:
            return cached[4]
        try:
            with open(readme_path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as exc:
            logger.warning(f"[maaguard] 客服知识库读取失败: {exc}")
            return ""
        if issues_mtime is not None:
            try:
                with open(issues_path, encoding="utf-8") as f:
                    issues = f.read().strip()
                # 去掉文件头说明，只留条目
                body = issues.split("\n", 3)
                issues_body = body[3] if len(body) >= 4 else ""
                if issues_body.strip():
                    text += ("\n\n【群友遇到的问题与解决档案（持续积累，越靠后越新）】\n"
                             + issues_body.strip())
            except OSError as exc:
                logger.warning(f"[maaguard] 问题档案读取失败: {exc}")
        self._cs_knowledge_cache = (readme_path, readme_mtime,
                                    issues_path, issues_mtime, text)
        return text

    def _cs_keyword_hit(self, text: str) -> bool:
        keywords = self._cs_keywords()
        if not keywords:  # 空列表 = 不筛选，监听群内消息一律注入
            return True
        lower = (text or "").lower()
        return any(k in lower for k in keywords)

    @staticmethod
    def _latest_user_text(req) -> str:
        prompt = str(getattr(req, "prompt", "") or "").strip()
        if prompt:
            return prompt
        contexts = getattr(req, "contexts", None) or []
        for msg in reversed(contexts):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return ""

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
            "url": str(meta.get("url") or ""),
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
            # 免分析名单（文件名关键词 / 上传者 QQ）：确定性跳过，连模型判定都不调用
            if self._should_skip_upload(meta, event):
                logger.info(f"[maaguard] 免分析名单命中: {name}，静默跳过")
                continue
            # notice 与 file 双入口去重：第二个入口若补充了下载 url 则补传 analyzer，其余静默
            has_seen = self._reminder_recently_sent(("gate", group_id, name))
            if has_seen:
                if meta.get("url"):
                    await self._forward_to_analyzer(meta, group_id, event)
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
                # 模型说 NO，但文件名有较强日志信号：可能是误判或打包方式
                # 不对，发轻提示指导正确打包；其余文件依旧静默
                if looks_log_related(name):
                    logger.info(f"[maaguard] 门槛判定 NO 但疑似日志包: {name}，发轻提示")
                    yield event.chain_result(self._at_chain(event, GATE_HINT))
                else:
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

    # ── MaaNikke 客服：知识注入 + 源码工具 ─────────────

    @filter.on_llm_request()
    async def cs_inject_knowledge(self, event: AstrMessageEvent, req) -> None:
        """监听群内的 MaaNikke 相关问题，在 LLM 请求中注入 README 知识。"""
        if not self._cs_enabled() or event is None or req is None:
            return
        try:
            group_id = event.get_group_id()
        except Exception:
            return
        groups = self._listen_groups()
        if not groups or group_id is None or str(group_id) not in groups:
            return
        text = self._latest_user_text(req)
        if not text or not self._cs_keyword_hit(text):
            return
        knowledge = self._load_cs_knowledge()
        if not knowledge:
            return
        req.system_prompt = (getattr(req, "system_prompt", "") or "") \
            + CS_SYSTEM_PROMPT.format(knowledge=knowledge)
        logger.info("[maaguard] 客服模式：已注入 MaaNikke README 知识")

    # ── 群聊沉淀：问题与解决档案 ─────────────────────────

    @staticmethod
    def _parse_faq_extraction(text: str) -> str:
        """解析提取模型的输出：合法条目返回文本，SKIP/不合规返回空串。"""
        text = (text or "").strip()
        if not text or text.upper().startswith("SKIP"):
            return ""
        if not text.startswith("### "):
            return ""
        keep = [l for l in text.splitlines()
                if l.startswith("### ") or l.lstrip().startswith("- ")]
        if len(keep) < 2:
            return ""
        return "\n".join(keep).strip()

    @staticmethod
    def _split_issues(content: str) -> list:
        """把档案文件内容拆成条目列表（每个条目以 ### 标题 开始）。"""
        entries, cur = [], []
        for line in (content or "").splitlines():
            if line.startswith("### "):
                if cur:
                    entries.append("\n".join(cur))
                cur = [line]
            elif cur:
                cur.append(line)
        if cur:
            entries.append("\n".join(cur))
        return entries

    def _append_issue(self, entry: str) -> bool:
        """去重后追加条目；返回是否真正追加（False = 已有相似条目）。"""
        path = self._cs_issues_path()
        header = ISSUES_HEADER
        entries: list = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    entries = self._split_issues(f.read())
            except OSError as exc:
                logger.warning(f"[maaguard] 问题档案读取失败: {exc}")
        compact = "".join(entry.split())
        for old in entries:
            if SequenceMatcher(None, compact, "".join(old.split())).ratio() > 0.75:
                return False
        title = entry.splitlines()[0].lstrip("# ").strip()
        today = time.strftime("%Y-%m-%d")
        entries.append(f"### {today} {title}\n" + "\n".join(entry.splitlines()[1:]))
        max_entries = self._cs_faq_max_entries()
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(entries) + "\n")
        except OSError as exc:
            logger.warning(f"[maaguard] 问题档案写入失败: {exc}")
            return False
        return True

    async def _record_faq(self, question: str, answer: str) -> None:
        """调用模型判定这段问答是否值得入库，是则追加到档案。"""
        try:
            provider = await self.context.get_using_provider_async()
            if provider is None:
                return
            prompt = FAQ_EXTRACT_PROMPT.format(
                question=question[:2000], answer=answer[:3000])
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt),
                                          timeout=60)
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[maaguard] FAQ 提取失败: {exc}")
            return
        entry = self._parse_faq_extraction(text)
        if not entry:
            return
        if self._append_issue(entry):
            logger.info(f"[maaguard] FAQ 档案新增: {entry.splitlines()[0]}")
        else:
            logger.info("[maaguard] FAQ 档案已有相似条目，跳过")

    @filter.on_llm_response()
    async def cs_record_faq(self, event: AstrMessageEvent, response) -> None:
        """监听群内机器人的回复，把确认有效的问题与解决异步沉淀进档案。"""
        if not self._cs_enabled() or not self._cs_faq_enabled() \
                or event is None or response is None:
            return
        try:
            group_id = event.get_group_id()
        except Exception:
            return
        groups = self._listen_groups()
        if not groups or group_id is None or str(group_id) not in groups:
            return
        question = (getattr(event, "message_str", "") or "").strip()
        answer = (getattr(response, "completion_text", "") or "").strip()
        if len(question) < 6 or not answer:
            return
        asyncio.create_task(self._record_faq(question, answer))

    # ── 源码工具（供 AstrBot 的 LLM 工具循环/agent 调用） ──

    def _repo_root(self) -> str:
        return os.path.realpath(self._cs_repo_path())

    def _safe_repo_file(self, path: str):
        """把用户给的 path 解析到仓库内；越界返回 None。"""
        root = self._repo_root()
        candidate = os.path.realpath(os.path.join(root, path))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def _repo_search(self, query: str, max_results: int = 10) -> str:
        """在仓库内做大小写不敏感的子串搜索，返回 path:line: 文本片段。"""
        root = self._repo_root()
        if not os.path.isdir(root):
            return (f"源码仓库未部署（期望路径 {root}）。"
                    "请联系管理员执行：git clone https://github.com/Shinarin/MaaNikke "
                    + root)
        query = (query or "").strip()
        if not query:
            return "query 不能为空。"
        max_results = max(1, min(int(max_results or 10), 30))
        needle = query.lower()

        matches: list[str] = []
        per_file = 0
        last_file = ""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in sorted(filenames):
                if len(matches) >= max_results:
                    break
                ext = os.path.splitext(fn)[1].lower()
                if ext in _SKIP_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                    with open(full, encoding="utf-8", errors="replace") as f:
                        rel = os.path.relpath(full, root)
                        per_file = 0 if rel != last_file else per_file
                        for lineno, line in enumerate(f, 1):
                            if needle in line.lower():
                                matches.append(
                                    f"{rel}:{lineno}: {line.strip()[:200]}"
                                )
                                last_file = rel
                                per_file += 1
                                if per_file >= 3 or len(matches) >= max_results:
                                    break
                except OSError:
                    continue
        if not matches:
            return f"源码仓库中没有找到包含 {query!r} 的内容。"
        out = "\n".join(matches)
        if len(out) > _SEARCH_RESULT_MAX_CHARS:
            out = out[:_SEARCH_RESULT_MAX_CHARS] + "\n…（结果已截断）"
        return out

    def _repo_read(self, path: str, max_chars: int = 8000) -> str:
        """读取仓库内指定文件（相对路径）。"""
        target = self._safe_repo_file(path)
        if target is None:
            return "path 必须是仓库内的相对路径，不允许越出仓库目录。"
        if not os.path.isfile(target):
            return f"仓库内不存在文件：{path}（可用 maanikke_source_search 先查找）"
        ext = os.path.splitext(target)[1].lower()
        if ext in _SKIP_EXTS:
            return f"{path} 是二进制文件，无法以文本读取。"
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
                text = f.read(int(max_chars or 8000) + 1)
        except OSError as exc:
            return f"读取失败：{exc}"
        truncated = len(text) > int(max_chars or 8000)
        if truncated:
            text = text[: int(max_chars or 8000)]
        header = f"===== {path} =====\n"
        return header + text + ("\n…（文件已截断）" if truncated else "")

    @filter.llm_tool(name="maanikke_source_search")
    async def cs_tool_search(self, event: AstrMessageEvent,
                             query: str, max_results: int = 10) -> str:
        """在 MaaNikke 源码仓库中搜索关键词，返回 文件:行号: 内容 片段。
        仅用于回答 MaaNikke 使用/配置/源码相关的问题。"""
        return self._repo_search(query, max_results)

    @filter.llm_tool(name="maanikke_source_read")
    async def cs_tool_read(self, event: AstrMessageEvent,
                           path: str, max_chars: int = 8000) -> str:
        """读取 MaaNikke 源码仓库内的文件内容。path 为仓库内相对路径
        （如 resource/base/pipeline/xxx.json）。仅用于 MaaNikke 相关问题。"""
        return self._repo_read(path, max_chars)

    # ── 日志分析工具（自然语言直接调用 analyzer） ──────────

    @staticmethod
    def _pick_group_file(candidates: list, file_name: str) -> dict | None:
        """从群文件列表中挑选要分析的文件：空名称优先取带强日志信号
        （log/日志/debug/error/crash）的最新文件，其次最新的压缩包；
        有名称则子串匹配取最新命中。candidates 需已按 modify_time 倒序。"""
        if not candidates:
            return None
        name = (file_name or "").strip().lower()
        if not name:
            strong = ("log", "日志", "debug", "error", "crash")
            for item in candidates:
                fn = item.get("file_name", "").lower()
                if any(k in fn for k in strong):
                    return item
            for item in candidates:
                if item.get("file_name", "").lower().endswith(".zip"):
                    return item
            return candidates[0]
        for item in candidates:
            if name in item.get("file_name", "").lower():
                return item
        return None

    def _analyze_enabled(self) -> bool:
        return bool(self._cfg("analyze_tool_enabled", True))

    async def _gate_get_files(self, group_id: str,
                              keyword: str = "") -> tuple[list, str]:
        """调用 analyzer gate API 列群文件。返回 (files, error)。"""
        url = self._gate_analyzer_url().rstrip("/") + "/files"
        params = {"group_id": group_id}
        if keyword:
            params["keyword"] = keyword
        headers = {}
        token = self._gate_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, params=params,
                                    headers=headers) as resp:
                    data = {}
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        pass
                    if resp.status == 200 and isinstance(data, dict):
                        return list(data.get("files") or []), ""
                    reason = str((data or {}).get("reason") or f"HTTP {resp.status}")
                    return [], reason
        except Exception as exc:
            return [], str(exc)[:120]

    async def _gate_submit(self, group_id: str, meta: dict,
                           sender: str) -> tuple[str, str]:
        """调用 analyzer gate API 提交分析（force 跳过去重窗口）。"""
        url = self._gate_analyzer_url().rstrip("/") + "/analyze"
        payload = {
            "group_id": group_id,
            "file_name": str(meta.get("file_name") or ""),
            "file_id": str(meta.get("file_id") or ""),
            "size": int(meta.get("size") or 0),
            "uploader": sender,
            "force": True,
        }
        headers = {}
        token = self._gate_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, json=payload,
                                     headers=headers) as resp:
                    data = {}
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

    @filter.llm_tool(name="maa_analyze_log")
    async def maa_analyze_log(self, event: AstrMessageEvent,
                              file_name: str = "",
                              reason: str = "") -> str:
        """分析群里的 Maa 日志压缩包。file_name 为群文件名（可省略，省略时
        自动选群内最新的日志包）；分析结果会直接发到群里。仅用于监听群内
        的日志分析请求。"""
        if not self._analyze_enabled():
            return "日志分析工具未启用。"
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        groups = self._listen_groups()
        if group_id is None:
            return "请先在群里上传日志包，或在群里直接说你要分析哪个文件。"
        if not groups or str(group_id) not in groups:
            return "该群未开通日志分析，请联系管理员。"

        group_id = str(group_id)
        sender = str(event.get_sender_id() or "")

        # 有文件名时先按关键词查一次，没命中再拉全量列表
        files, err = await self._gate_get_files(group_id, file_name or "")
        if err and err != "该群未在监听配置中":
            return f"分析工具暂时联系不上（{err}），请稍后重试。"
        if err == "该群未在监听配置中":
            return "该群未开通日志分析，请联系管理员。"

        meta = self._pick_group_file(files, file_name)
        if meta is None:
            hint = (f"群文件里没有找到名为 {file_name!r} 的文件。"
                    "可以让群友直接上传日志 zip，会自动分析。")
            return hint

        status, reason = await self._gate_submit(group_id, meta, sender)
        if status == "ok":
            return (f"已开始分析 {meta['file_name']}，"
                    "分析结果稍后会直接发到群里，请稍等。")
        if status == "duplicate":
            return (f"{meta['file_name']} 已在分析队列或刚分析过，"
                    "结果会直接发到群里。")
        if status == "rejected":
            return f"分析工具处理不了 {meta['file_name']}：{reason}"
        return f"分析工具暂时联系不上（{reason}），请稍后重试。"

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
