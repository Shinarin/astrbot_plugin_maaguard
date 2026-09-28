# astrbot_plugin_maaguard

AstrBot 插件：**MAA 日志分析守门员**。配合独立运行的 [OnebotMaaLogAnalyzer](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer) 使用，让 AstrBot 与 analyzer 共用同一个 QQ 身份时互不打架。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A53.4.21-blue)](https://github.com/AstrBotDevs/AstrBot)
[![平台](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-aiocqhttp%20(OneBot%20v11)-green)]()

## 解决什么问题

OnebotMaaLogAnalyzer 是一个独立运行的 OneBot v11 Bot（不依赖任何 Bot 框架）。实际部署时它和 AstrBot **连同一个 NapCat/LLOneBot，共用同一个 QQ 身份**。这带来一个结构性问题：

> 同一条消息（日志包上传、`/maa` 指令、引用分析结果的追问），analyzer 和 AstrBot **都能看到**。analyzer 正常处理的同时，AstrBot 也会把它当普通聊天接一句话——用户视角就是"Bot 回复了两次/自己跟自己打架"。

本插件的职责：

1. **该 analyzer 说话的场景，让 AstrBot 闭嘴**（静默拦截，零回复）
2. **群友操作不对时，主动指路**（@当事人 + 正确做法提醒）

> 本插件**不分析日志**。所有分析行为都发生在独立运行的 OnebotMaaLogAnalyzer 进程里。

## 与 OnebotMaaLogAnalyzer 的配合

### 部署架构

```text
                        ┌─► AstrBot (6199) ──► 加载本插件 maaguard
 群友消息 ──► NapCat ────┤         （该闭嘴时闭嘴，格式不对时提醒）
 (OneBot v11 实现)       │
                        └─► OnebotMaaLogAnalyzer (8080)
                                  （下载日志包 → 摘要 → AI 分析 → 发回群里）
```

两个程序各自与 NapCat 建立独立的 WebSocket 连接，互不感知；本插件是唯一知道"另一个 Bot 存在"的协调者。

### 职责划分

| 场景 | OnebotMaaLogAnalyzer | 本插件（AstrBot 侧） |
|---|---|---|
| 上传 `MaaXXX-logs*.zip` | 下载、分析、回复结论 | 静默拦截 AstrBot |
| `/maa` 系列指令 | 应答 | 静默拦截 AstrBot |
| 引用分析结果追问 | 基于日志继续答疑 | 静默拦截 AstrBot |
| 文件格式不对（改名/7z/散文件） | 不处理 | @当事人发送正确做法提醒 |
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
| `file_prefix` | string | `"MaaXXX-logs"` | 日志包文件名前缀。配置了 `analyzer_config_path` 时仅作回退 |
| `listen_groups` | list | `[]` | 启用本插件的群号列表。配置了 `analyzer_config_path` 时仅作回退；两处都为空时插件不生效 |
| `command_prefix` | string | `"/maa"` | analyzer 的指令前缀，用于拦截 AstrBot 对 `/maa` 指令的应答 |
| `analysis_header_keywords` | list | `["日志分析结果"]` | analyzer 分析结果消息的特征词。引用消息含此特征且发送者是 Bot 自身时，判定为对分析结果的追问并拦截 |
| `intent_keywords` | list | 见配置文件 | 纯文本意图关键词（如"分析日志""看下日志"）。命中且未附带文件时发送提醒，可按群习惯增删 |

## 行为规则

插件只在生效的监听群内、且仅对 aiocqhttp 平台起作用：

| 场景 | 行为 |
|---|---|
| 群文件上传 notice（文件名 = 前缀 + `.zip`） | 静默拦截（analyzer 会处理） |
| 群文件上传 notice（文件名与日志无关） | 静默拦截（AstrBot 无需搭话） |
| 消息内 file 段（文件名 = 前缀 + `.zip`） | 静默拦截 |
| 消息内 file 段（疑似日志包但格式不对） | @当事人发送提醒 + 拦截 |
| `/maa` 开头的指令 | 静默拦截 |
| 引用 Bot 发出的分析结果消息追问 | 静默拦截（analyzer 负责答疑） |
| 纯文本命中意图关键词但没传文件 | 发送提醒 + 拦截 |
| 其他所有消息 | 完全不干预 |

两个实现细节：

- **提醒去重**：QQ 一次文件上传会产生"上传通知 + 文件消息"两个事件，同一 (群, 文件名) 在 120 秒内只提醒一次
- **决策日志**：所有拦截/提醒都有 `[maaguard]` 前缀的日志，排查问题时先看日志

## 典型使用流程

1. 群友上传 `MaaXXX-logs-20260928-120000.zip` → 群里只有 analyzer 的分析结论，AstrBot 不出声
2. 群友上传 `debug.zip` / `logs.7z` → 本插件 @他并给出正确做法（示例名按 analyzer 的前缀生成）
3. 群友引用分析结果追问"这个报错怎么解决" → analyzer 继续回答，AstrBot 不插嘴
4. 群友发 `/maa 状态` → analyzer 应答

## 常见问题

**传了正确的日志包，两边都没反应？**
analyzer 没部署/没连上，或被本插件拦截后 analyzer 侧出错。查 analyzer 日志（`journalctl -u maalog` 或其 `logs/` 目录）。

**提醒的内容和 analyzer 的前缀对不上？**
`analyzer_config_path` 未配置或读取失败（看 AstrBot 日志里的 `[maaguard]` 警告），此时用的是本地 `file_prefix`。

**analyzer 的报告文案改了，引用追问没被拦截？**
`analysis_header_keywords` 需要包含新报告标题里的特征词。

**可以给多个 analyzer 配置/多个群用吗？**
可以。`analyzer_config_path` 模式下自动取所有配置的并集；手动模式下群号全部写进 `listen_groups`。

**支持 QQ 以外的平台吗？**
不支持。OnebotMaaLogAnalyzer 本身只支持 OneBot v11，本插件也只注册在 aiocqhttp 平台。

## 更新日志

- **v0.2.0**：新增 `analyzer_config_path` 自动同步；提醒按 (群, 文件名) 去重；增加 `[maaguard]` 决策日志
- **v0.1.0**：初始版本（静默拦截 + 格式提醒）

## 相关项目

- [OnebotMaaLogAnalyzer](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer) — 干分析活的主角（AGPLv3）
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 插件框架

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
