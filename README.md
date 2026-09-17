# CacheGate

在 Codex CLI 提交用户消息前，检查同一会话距上次获准提交是否超过 30 分钟。可选择仅提醒，或拦截后由用户决定是否发送。

在 Codex 输入框直接提交以下内容即可查看帮助，无需任何前缀：

```text
cachegate help
```

超时提示只保留一行，例如：

> CacheGate：已超过 30 分钟，本次已拦截。切换模式等功能见 cachegate help。

## 安装

需要 Python 3.10+ 和支持 `UserPromptSubmit` command hook 的 Codex CLI。已在 Linux、Python 3.12.3、Codex CLI/app-server 0.154.0 验证。

在项目目录执行一次安装：

```bash
python3 scripts/install.py
```

安装程序复制文件到 `~/plugins/cachegate`，向 `~/.agents/plugins/marketplace.json` 追加个人市场条目，再调用 `codex plugin add cachegate@<市场名>`。它保留市场原有内容和名称；同名目录或条目已存在时停止，不覆盖。`--prepare-only` 仅准备插件和市场条目。

安装后新建 Codex 会话，在 `/hooks` 审阅并信任 CacheGate，再输入 `cachegate help` 选择模式。未配置模式时，普通消息会被拦截并提示打开帮助。

安装程序记录当前 Python 解释器和状态目录的绝对路径；脚本位置通过 `${PLUGIN_ROOT}` 解析到已安装的插件。移动解释器或状态目录后需重新配置。插件及安装器均不依赖 rtk，也不会注册系统 shell 命令。

## 帮助中的操作

下面的命令在 **Codex 输入框**单独提交，由 hook 本地处理。

| 输入 | 效果 |
| --- | --- |
| `cachegate help` | 展开帮助 |
| `cachegate remind` | 超过 30 分钟时提醒，然后继续发送 |
| `cachegate confirm` | 超过 30 分钟时拦截 |
| `cachegate status` | 查看当前模式 |
| `cachegate allow` | 允许本会话最近被拦截的消息，随后原样重发 |
| `cachegate cancel` | 撤销本会话的待确认记录和单次许可 |

模式在本机使用同一状态目录的会话间共享，下次提交生效。超时被拦截后，可打开帮助选择单次放行、修改模式，或手动 `/clear` 新建会话后重新输入请求。插件不自动执行 `/clear`。

命令区分大小写，只识别独立的 `cachegate` 或 `cachegate <单个命令>`；普通说明文字、多行请求和代码块中的提及按普通消息处理。单独的未知子命令会显示帮助入口。

## 计时和本地状态

- 阈值固定为严格大于 1,800 秒，恰好 30 分钟仍放行。比较范围是同一 `session_id` 的用户消息；首次见到的会话没有历史时间戳，配置模式后放行。
- 计时从上次**获准提交**开始；被拦截、查看帮助、修改模式、许可和取消都不会刷新时间。许可后的重发获准时才更新时间。
- 单次许可绑定会话、消息文本和模型，使用一次后消耗。改发不同文本或模型需要重新确认。hook 不提供附件内容，因此这不是对附件或最终 API 请求体的完整绑定。
- 恢复已记录会话时沿用时间戳，安装前的历史不回溯。系统时钟回退时按所选模式提醒或拦截。同一运行中的 turn 收到不同用户消息仍分别检查。
- 本地保存模式、会话和 turn ID、时间戳、消息摘要及单次许可状态。正文仅在本次 hook 内用于识别命令和计算摘要，不保存原文，也不读取聊天记录。

默认状态位于 `${CODEX_HOME:-~/.codex}/cachegate/state.sqlite3`，安装时可通过 `CACHEGATE_DATA_DIR` 指定其他目录。安装器把选定目录明确写入 hook 命令，避免子进程环境差异。旧版数据库的模式和时间戳会保留，首次运行时自动创建单次许可所需的表。

旧版终端 `config` 入口仍兼容；需操作与插件相同的数据目录。安装器不覆盖已有插件，数据库兼容不代表已安装版本会自动更新。更新安装后需新建会话并重新审阅变更后的 hook。

## 开销与边界

运行时仅使用 Python 标准库和本地 SQLite。每次提交启动一次短进程，不调用模型、不开启常驻服务，也不注册 MCP 工具或技能。

控制命令使用 `continue: false` 和 `stopReason` 本地显示结果；超时拦截使用 `decision: block`；仅提醒使用 `systemMessage`。不输出额外模型上下文。集成测试检查真实 HTTP 请求体，确认帮助、模式切换、放行、取消及提示文本未进入后续模型请求。

**30 分钟是检查阈值，无法保证 KV 缓存命中。** `/clear` 会开始新的会话上下文，不能恢复或延长过期缓存。参见 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) 和 [Codex /clear](https://developers.openai.com/codex/cli/slash-commands#clear-the-terminal-and-start-a-new-chat-with-clear)。

根据 [Codex hooks 的运行约定](https://developers.openai.com/codex/hooks)，hook 未信任、被禁用、进程无法启动或执行超时时，宿主可能跳过检查。插件捕获到的本地存储或输入错误会返回拦截结果，但无法控制宿主跳过 hook。其他 hook 或网络故障也可能阻止已获本插件批准的请求，因此时间戳是提交活动的近似值。任务内部的模型续发、工具循环和网络重试不在观察范围内。

## 验证

```bash
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy env CACHEGATE_CODEX_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

测试命令中的 `rtk proxy` 用于本工作区约定，运行插件不需要它。

集成测试使用独立临时 `CODEX_HOME`、测试状态目录和 loopback 模型服务，验证直接输入命令、首次配置、超时拦截、单次放行、取消、模式切换及请求内容。它不调用真实模型，仅在测试会话对本项目 hook 设置 `bypass_hook_trust`，不改变用户的信任配置。Codex 安装插件时可能访问自己的插件目录服务。临时目录保留日志和请求体供检查。

集成测试覆盖 app-server 的 hook 事件及模型请求边界；另在隔离的 Codex CLI 终端中检查了帮助展开和模式切换提示。尚未验证 Windows/macOS、桌面端或 IDE。

插件打包方式参见 [Build plugins](https://developers.openai.com/plugins/build/plugins)。
