# CacheGate

在 Codex 把用户消息发送给模型之前，检查同一会话的消息提交间隔。超过 30 分钟时，可以仅提醒，或等待用户明确允许后发送。

| 模式 | 超过 30 分钟时的行为 |
| --- | --- |
| `remind` | 在客户端显示提醒，然后继续发送 |
| `confirm` | 显示原生确认表单；明确选择发送才放行，拒绝、取消或无法获得回答时阻止本次提交 |

安装并信任 hook 后，首次提交消息会先询问模式。没有预先替用户选择模式；取消设置会阻止这次提交。阈值固定为严格大于 1,800 秒，恰好 30 分钟仍允许发送。

## 安装

需要 Python 3.10+ 和支持 MCP hook、原生 MCP 表单的 Codex 客户端。已在 Linux、Python 3.12.3、Codex CLI/app-server 0.154.0 验证。运行时只用 Python 标准库，无需 API key、SDK 或常驻网络服务。

在本项目目录执行：

```bash
rtk proxy python3 scripts/install.py
```

`rtk proxy` 仅用于本工作区的命令约定；插件本身不依赖 rtk，其他环境可直接运行 Python。

安装程序将文件复制到 `~/plugins/cachegate`，在 `~/.agents/plugins/marketplace.json` 中追加个人市场条目，再调用 `codex plugin add cachegate@<市场名>`。它保留市场已有内容和名称；已有同名目录或条目时会停止，不自动覆盖或更新。使用 `--prepare-only` 可只准备文件和市场条目。

安装后：

1. 在 Codex 中新建会话，执行 `/hooks`，审阅并信任 CacheGate 的 `UserPromptSubmit` hook。
2. 用 `/mcp` 确认 `cachegate` 服务已连接。
3. 提交消息，在出现的表单中选择 `remind` 或 `confirm`。首次配置发生在发送前，不需要先调用模型。

这是当前 Codex 的 hook 信任要求；安装插件不会自动信任 hook。不要跳过审阅步骤。客户端或组织策略不允许 MCP 表单时，可先通过下方命令设置 `remind`；`confirm` 无法获得明确同意时会返回阻止结果。

当前安装流程针对本机个人市场。`.mcp.json` 是安装模板，安装程序会填写 Python、脚本和数据目录的绝对路径。请使用安装程序；直接把源码目录加入市场不能保证在 Codex 0.154.0 下正确启动 MCP。生成后的插件目录绑定本机路径，移动或跨机器复制后需要重新生成配置。无需为默认个人市场执行 `codex plugin marketplace add`。

## 修改模式

安装后的任意时间可以在终端修改；下次提交即生效：

```bash
rtk proxy python3 ~/plugins/cachegate/scripts/cachegate.py config --mode remind
rtk proxy python3 ~/plugins/cachegate/scripts/cachegate.py config --mode confirm
rtk proxy python3 ~/plugins/cachegate/scripts/cachegate.py config
```

也可以要求 Codex“调用 CacheGate 的 configure 工具修改模式”，再在表单中选择。该请求本身仍受现有模式约束；已经被拦截时，终端配置命令可以直接修改设置。

默认状态文件位于 `${CODEX_HOME:-~/.codex}/cachegate/state.sqlite3`。安装时设置 `CACHEGATE_DATA_DIR` 可覆盖目录；安装程序会把选定路径传给 MCP 子进程。在另一个终端手动修改配置时，需使用相同环境变量，或指定 `--data-dir /绝对路径`（放在 `config` 之前）。模式在本机该状态目录内共享，时间戳按会话隔离。

## 计时与取消

- 记录的是 CacheGate 上次**获准提交**的时间。hook 位于实际模型请求之前，不能证明网络请求已成功发送。
- 首次见到的会话没有历史时间戳，设置模式后放行。安装前的历史不回溯；恢复已记录的会话沿用原时间戳。
- 取消超时确认不会更新计时，重新提交仍会询问。允许发送后，以用户作出决定的时间更新计时。
- 只比较同一 `session_id` 的用户消息。任务内部的模型续发、工具循环、重试和缓存读写不在此 hook 的观察范围内。
- 系统时钟回退时，`remind` 提醒后发送，`confirm` 要求确认。同一会话并发提交发生状态冲突时，要求重新提交，避免覆盖新记录。
- 插件不会自动执行 `/clear`。用户可取消当前发送，再手动 `/clear` 并重新输入请求；这会新建会话，后续请求不再携带当前对话上下文。
- 本地只保存模式、会话 ID、最近获准提交时间和 turn ID；hook 不接收正文，插件不读取聊天记录、不保存提示词，也不发起网络请求。

## 能力边界

**30 分钟是检查阈值，无法保证 KV 缓存命中。** OpenAI 的缓存保留时间依模型和配置而定，前缀匹配及请求路由也会影响复用。`/clear` 的作用是开始新的上下文，不能恢复或延长已过期的缓存。参见 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) 和 [Codex /clear](https://developers.openai.com/codex/cli/slash-commands#clear-the-terminal-and-start-a-new-chat-with-clear)。

**这是用户提交前的辅助检查，不是网络层强制防火墙。** 按 [Codex hooks 的运行约定](https://developers.openai.com/codex/hooks)，hook 未信任、被禁用、MCP 未连接、进程崩溃或宿主执行超时时，Codex 可能跳过 hook 并继续提交。插件捕获到的本地存储/输入错误会返回阻止结果，但无法控制宿主跳过 hook 的行为。配置 `confirm` 不代表这些故障条件下仍能强制阻断请求。

本实现通过 MCP 表单让用户决定，不调用模型进行间隔判断。提醒使用 `systemMessage`，不向模型注入动态提醒。其他 hook 仍可能阻止已获本插件批准的提交；因此计时是提交活动的近似值。

## 验证

```bash
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy env CACHEGATE_CODEX_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

第二条需要本机 `codex`。集成测试使用独立临时 `CODEX_HOME`、明确传给 MCP 的测试数据目录和 loopback 模型服务，验证首次配置、拒绝时无模型请求、重试确认、允许后发送和仅提醒。测试不会调用真实模型；Codex 安装插件时可能访问自己的插件目录服务。它仅在测试会话中对本项目已审阅的 hook 设置 `bypass_hook_trust`，不会改变用户的 hook 信任配置。

集成测试验证 app-server 协议和模型请求边界；尚未人工验证桌面端或 IDE 表单的视觉显示，也未验证 Windows/macOS。失败时保留临时目录中的日志用于排查。

插件打包方式参见 [Build plugins](https://developers.openai.com/plugins/build/plugins)。
