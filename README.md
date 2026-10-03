# astrbot_plugin_maaguard

AstrBot 插件：**MAA 日志分析守门员**。配合独立运行的 [OnebotMaaLogAnalyzer](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer) 使用，让 AstrBot 与 analyzer 共用同一个 QQ 身份时互不打架。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A53.4.21-blue)](https://github.com/AstrBotDevs/AstrBot)
[![平台](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-aiocqhttp%20(OneBot%20v11)-green)]()

## 解决什么问题

OnebotMaaLogAnalyzer 是一个独立运行的 OneBot v11 Bot（不依赖任何 Bot 框架）。实际部署时它和 AstrBot **连同一个 NapCat/LLOneBot，共用同一个 QQ 身份**。这带来一个结构性问题：

> 同一条消息（日志包上传、`/maa` 指令、引用分析结果的追问），analyzer 和 AstrBot **都能看到**。analyzer 正常处理的同时，AstrBot 也会把它当普通聊天接一句话——用户视角就是"Bot 回复了两次/自己跟自己打架"。

本插件的职责：

1. **该 analyzer 说话的场景，让 AstrBot 闭嘴**（静默拦截，零回复）
2. **门槛模式（默认）**：群友上传的任何 zip/文件，先由模型（AstrBot 已配置的 LLM）判断是不是想分析日志，是才把文件转发给 analyzer 分析
3. **群友操作不对时，主动指路**（@当事人 + 正确做法提醒）
4. **MaaNikke 客服**：回答 MaaNikke 使用问题——README 知识注入 + 复杂问题调用源码读取工具（AstrBot 的 LLM 工具循环）查证后回答

> 本插件**不分析日志**。所有分析行为都发生在独立运行的 OnebotMaaLogAnalyzer 进程里。

## MaaNikke 客服（v0.4.0 新增）

在监听群内，群友问到 MaaNikke（《胜利女神：NIKKE》每日任务自动化工具）的使用问题时，插件自动充当客服：

```text
群友提问："模拟室那个快速怎么开？" / "startnikke.ahk 找不到启动器"
        │
        ▼
 消息命中客服关键词（nikke / 妮姬 / 每日任务 / 脚本 …）
        │
        ▼
 AstrBot 发起 LLM 请求时，自动注入 MaaNikke README 知识（内置 knowledge/maanikke_readme.md）
        │
        ▼
 模型直接依据 README 回答（环境要求、游戏内设置、任务选项、常见问题…）
        │
        └─ 资料没写的复杂问题 ──► 模型自行调用源码工具（AstrBot 工具循环 / agent）：
                                  maanikke_source_search  在源码仓库里搜关键词
                                  maanikke_source_read    读取仓库内指定文件
                                         │
                                         ▼
                                  结合源码逻辑给出答案
```

特点：

- **不干扰正常聊天**：只有命中关键词的群消息才注入知识；其他话题照常由 AstrBot 的人格回答
- **不需要额外配置 API**：复用 AstrBot 已配置的对话模型与工具调用（function calling）能力
- **README 可更新**：替换插件目录下 `knowledge/maanikke_readme.md`，或配置 `cs_readme_path` 指向自己的文件（按 mtime 自动刷新缓存）
- **源码仓库需要先部署**：`git clone https://github.com/Shinarin/MaaNikke /opt/maanikke-src`（默认路径，可用 `cs_repo_path` 修改）；更新源码在该目录 `git pull`。仓库缺失时工具会返回提示，不影响 README 问答

工具只在 MaaNikke 相关问题上有用，模型一般不会滥用；不需要客服功能可把 `cs_enabled` 关掉。

## 门槛模式（v0.3.0 新增，默认开启）

不再只靠文件名前缀猜测意图，而是让模型结合上下文判断：

```text
群友上传 zip/文件
        │
        ▼
 本插件：调用 AstrBot 已配置的模型 ──判定──► 想分析日志？
        │                                 │
       是│                              否│
        ▼                                 ▼
 POST analyzer 本地 gate API          静默放行
 （analyzer 正常分析并回复群里）       （AstrBot 也不搭话）
```

- 判定的输入：文件名、文件大小、同一条消息里的附言（如"帮我看看为什么报错"）
- 判定用的模型就是 AstrBot 当前配置的对话模型，**无需额外配置 API**
- analyzer 侧需要开启 gate（见下），开启后 analyzer 不再按文件名前缀自动触发，只接受本插件转发来的任务
- 判定输出无法解析 / 模型不可用 / analyzer 拒绝时，自动回退旧的文件名前缀规则与提醒
- 文件名格式从此不再影响触发（`debug.zip` 只要模型判定有分析意图也会被分析），旧的"格式不对"提醒只在回退路径出现

### analyzer 侧需要增加的 gate 配置

analyzer 的 `config.json` 增加一段（开启后其自动触发只保留 `/maa` 指令与追问答疑，文件上传改由本插件转发）：

```json
"gate": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 8090,
  "token": "与本插件 gate_token 一致的随机字符串"
}
```

`gate.token` 与插件配置的 `gate_token` 保持一致即可；analyzer 未设置 token 时插件侧也留空。

## 与 OnebotMaaLogAnalyzer 的配合

### 部署架构

```text
                        ┌─► AstrBot (6199) ──► 加载本插件 maaguard
 群友消息 ──► NapCat ────┤      （模型判定上传意图 ──转发──► analyzer 本地 gate API :8090）
 (OneBot v11 实现)       │        （该闭嘴时闭嘴，异常时提醒）
                         └─► OnebotMaaLogAnalyzer (8080)
                                   （下载日志包 → 摘要 → AI 分析 → 发回群里）
```

两个程序各自与 NapCat 建立独立的 WebSocket 连接，互不感知；本插件是唯一知道"另一个 Bot 存在"的协调者。

### 职责划分

| 场景 | OnebotMaaLogAnalyzer | 本插件（AstrBot 侧） |
|---|---|---|
| 上传文件（模型判定想分析日志） | 接收转发、分析、回复结论 | 判定 + 转发 + 静默拦截 AstrBot |
| 上传文件（模型判定无关） | 不处理 | 静默拦截 AstrBot |
| `/maa` 系列指令 | 应答 | 静默拦截 AstrBot |
| 引用分析结果追问 | 基于日志继续答疑 | 静默拦截 AstrBot |
| 模型不可用时的格式不对文件 | 不处理（gate 模式不会自动触发） | 回退旧规则：命中前缀照样转发，疑似日志包 @当事人提醒 |
| 嘴上说要分析日志但没传文件 | 不处理 | 发送提醒 |
| 普通聊天 | 不处理 | 完全不干预 |

### 配置同步（重点）

analyzer 识别哪些群、哪些文件名，取决于它的 `config.json`；本插件要拦截同样的群、认同样的文件名。两种同步方式：

**方式一（推荐）：自动同步**。把插件配置里的 `analyzer_config_path` 填上 analyzer 的 `config.json` 路径（如 `/opt/maalog/config.json`）。之后监听群、文件名前缀以 analyzer 为准（多配置取并集），analyzer 里改群号/前缀**插件自动跟随**，每次消息实时读取，无需重载插件。读取失败自动回退本地配置。

**方式二：手动同步**。不填 `analyzer_config_path`，手动保持两边一致：

- analyzer 侧：`config.json` → `bot.configs.<配置名>.listen_groups` 与 `settings.file_prefix`
- 插件侧：`file_prefix`、`listen_groups`

两边改了一处忘了另一处，就会出现"该拦的没拦/不该提醒的乱提醒"——这也是推荐方式一的原因。

### 前置要求

- AstrBot ≥ 3.4.21（插件用到了事件优先级）
- AstrBot 已接入 aiocqhttp 平台（OneBot v11，如 NapCat / LLOneBot）
- OnebotMaaLogAnalyzer 已部署并连上**同一个**协议端，详见它的[文档](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer#readme)

## 安装

1. 把整个 `astrbot_plugin_maaguard` 目录拷贝到 AstrBot 的 `data/plugins/` 下：

   ```bash
   cp -r astrbot_plugin_maaguard /path/to/AstrBot/data/plugins/
   ```

2. 在 AstrBot WebUI「插件管理」中找到本插件，点击「重载插件」（或重启 AstrBot）

3. 在插件配置中填写（详见下表），保存后重载插件生效

## 配置说明

配置文件由 AstrBot 自动生成并管理，位于 `data/config/astrbot_plugin_maaguard_config.json`，也可以在 WebUI 插件配置弹窗中可视化编辑。

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `analyzer_config_path` | string | `""` | analyzer 的 `config.json` 路径。**填写后监听群与前缀以 analyzer 为准**，读取失败自动回退下方本地配置；留空则完全使用本地配置 |
| `gate_enabled` | bool | `true` | 门槛模式：文件上传先由模型判定意图再转发 analyzer。需要 analyzer 开启 gate 并正确填写下面两项 |
| `gate_analyzer_url` | string | `"http://127.0.0.1:8090"` | analyzer 本地 gate API 地址（仅监听 127.0.0.1） |
| `gate_token` | string | `""` | analyzer gate API 的鉴权 token，需与 analyzer `config.json` 里 `gate.token` 一致 |
| `file_prefix` | string | `"MaaXXX-logs"` | 日志包文件名前缀。配置了 `analyzer_config_path` 时仅作回退 |
| `listen_groups` | list | `[]` | 启用本插件的群号列表。配置了 `analyzer_config_path` 时仅作回退；两处都为空时插件不生效 |
| `command_prefix` | string | `"/maa"` | analyzer 的指令前缀，用于拦截 AstrBot 对 `/maa` 指令的应答 |
| `analysis_header_keywords` | list | `["日志分析结果"]` | analyzer 分析结果消息的特征词。引用消息含此特征且发送者是 Bot 自身时，判定为对分析结果的追问并拦截 |
| `cs_enabled` | bool | `true` | MaaNikke 客服：监听群内的使用问题注入 README 知识回答，并提供源码读取工具 |
| `cs_keywords` | list | 见配置文件 | 触发客服知识注入的关键词（子串匹配、不区分大小写）。留空 = 监听群所有消息都注入（费 token，不建议） |
| `cs_repo_path` | string | `"/opt/maanikke-src"` | MaaNikke 源码仓库本地路径，供工具搜索/读取；需先 `git clone https://github.com/Shinarin/MaaNikke` 到该路径 |
| `cs_readme_path` | string | `""` | 客服知识库文件路径；留空使用插件内置 `knowledge/maanikke_readme.md` |
| `intent_keywords` | list | 见配置文件 | 纯文本意图关键词（如"分析日志""看下日志"）。命中且未附带文件时发送提醒，可按群习惯增删 |

## 行为规则

插件只在生效的监听群内、且仅对 aiocqhttp 平台起作用。门槛模式（默认）下：

| 场景 | 行为 |
|---|---|
| 上传文件（模型判定想分析日志，analyzer 受理） | 静默拦截（analyzer 会分析并回复） |
| 上传文件（模型判定想分析日志，analyzer 拒绝，如超 100MB） | @当事人说明拒绝原因 + 拦截 |
| 上传文件（模型判定与日志无关） | 静默拦截 |
| 上传文件（模型判定无关，但文件名有日志信号，如 `xxxlog.zip`） | @当事人发轻提示（指导正确打包日志包）+ 拦截 |
| 上传文件（模型不可用，回退旧规则） | 命中前缀 → 照样转发 analyzer；疑似日志包 → @当事人发送提醒；无关文件 → 静默拦截 |
| 分析工具联系不上 | @当事人提示稍后重试 + 拦截 |
| `/maa` 开头的指令 | 静默拦截 |
| 引用 Bot 发出的分析结果消息追问 | 静默拦截（analyzer 负责答疑） |
| 纯文本命中意图关键词但没传文件 | 发送提醒 + 拦截 |
| 其他所有消息 | 完全不干预 |

在插件配置里把 `gate_enabled` 设为 `false` 可回到纯前缀匹配的旧行为（保留兼容）。

两个实现细节：

- **判定/提醒去重**：QQ 一次文件上传会产生"上传通知 + 文件消息"两个事件，同一 (群, 文件名) 在 120 秒内只判定/转发/提醒一次
- **决策日志**：所有拦截/判定/转发都有 `[maaguard]` 前缀的日志，排查问题时先看日志

## 典型使用流程

1. 群友上传日志压缩包（叫什么名字都行，最好带上说明） → 模型判定为想分析日志 → analyzer 分析并把结论发回群里，AstrBot 不出声
2. 群友上传与日志无关的文件 → 无人搭话，不打扰
3. 群友引用分析结果追问"这个报错怎么解决" → analyzer 继续回答，AstrBot 不插嘴
4. 群友发 `/maa 状态` → analyzer 应答
5. （回退路径）模型不可用时，群友上传命中前缀的日志包仍会照常分析；上传 `logs.7z` 这类疑似日志包会收到格式提醒

## 常见问题

**传了日志包，两边都没反应？**
先看 AstrBot 日志 `[maaguard]` 行：模型判定是不是 NO（文件名/附言没体现出分析意图时可能误判，可以让群友附言"帮忙分析日志"重试）；是不是转发失败（analyzer 的 gate 没开/token 不一致/服务没起）。再查 analyzer 日志（`journalctl -u maalog` 或其 `logs/` 目录）。

**analyzer 侧如何确认 gate 已生效？**
analyzer 启动日志里应有 `[门槛] 本地 API 已启动: http://127.0.0.1:8090`；`curl http://127.0.0.1:8090/health` 应返回 `{"ok": true}`。

**问 MaaNikke 的问题没被客服回答？**
检查三点：消息里是否含 `cs_keywords` 里的词（纯表情包/语音不触发）；AstrBot 日志里有没有 `[maaguard] 客服模式：已注入 MaaNikke README 知识`；知识库文件是否存在（`cs_readme_path` 或插件目录 `knowledge/maanikke_readme.md`）。源码工具查不到内容时，确认 `cs_repo_path` 已 clone 了 MaaNikke 仓库。

**提醒的内容和 analyzer 的前缀对不上？**
`analyzer_config_path` 未配置或读取失败（看 AstrBot 日志里的 `[maaguard]` 警告），此时用的是本地 `file_prefix`。

**analyzer 的报告文案改了，引用追问没被拦截？**
`analysis_header_keywords` 需要包含新报告标题里的特征词。

**可以给多个 analyzer 配置/多个群用吗？**
可以。`analyzer_config_path` 模式下自动取所有配置的并集；手动模式下群号全部写进 `listen_groups`。

**支持 QQ 以外的平台吗？**
不支持。OnebotMaaLogAnalyzer 本身只支持 OneBot v11，本插件也只注册在 aiocqhttp 平台。

## 更新日志

- **v0.4.0**：新增 MaaNikke 客服——命中关键词的群消息自动注入 README 知识（`knowledge/maanikke_readme.md`），并注册 `maanikke_source_search` / `maanikke_source_read` 两个 LLM 工具，复杂问题由模型调用工具查源码后回答
- **v0.3.1**：门槛判定为 NO 但文件名带日志信号（log/日志/maa/debug/error/crash 等）时，发轻提示指导正确打包日志包，避免误判后"死寂"
- **v0.3.0**：新增门槛模式（默认开启）——文件上传由模型判定意图后转发 analyzer 本地 gate API；模型异常/analyzer 拒绝自动回退旧规则；新增 `gate_enabled` / `gate_analyzer_url` / `gate_token` 配置
- **v0.2.0**：新增 `analyzer_config_path` 自动同步；提醒按 (群, 文件名) 去重；增加 `[maaguard]` 决策日志
- **v0.1.0**：初始版本（静默拦截 + 格式提醒）

## 相关项目

- [OnebotMaaLogAnalyzer](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer) — 干分析活的主角（AGPLv3）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 插件框架

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
