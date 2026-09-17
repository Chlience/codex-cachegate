# CacheGate

CacheGate 的设计目的是提醒你留意长时间中断后继续会话的上下文开销。提示词缓存（KV 缓存）可能在长时间空闲后失效，重新处理长上下文可能增加耗时和输入成本。你可以据此决定继续当前会话，或手动 `/clear` 新建会话。[官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)

在 Codex CLI 提交用户消息前，检查同一会话距上次获准提交是否超过 30 分钟。默认仅提醒并继续发送，也可切换为拦截后由用户决定是否发送。

在 Codex 输入框直接提交以下内容即可查看帮助，无需任何前缀：

```text
cachegate help
```

超时提示只保留一行，例如：

> CacheGate：已超过 30 分钟，本次已拦截。切换模式等功能见 cachegate help。

API 输入价格表更新于 **2026-09-17**：[核验时间与对应价格](#api-输入价格快照)。

## 安装、更新和卸载

需要 Python 3.10+ 和支持 `UserPromptSubmit` command hook 的 Codex CLI。已在 Linux、Python 3.12.3、Codex CLI/app-server 0.154.0 验证。

首次安装，在终端执行：

```bash
codex plugin marketplace add Chlience/codex-cachegate --ref main
codex plugin add cachegate@cachegate
```

安装后新建 Codex 会话，在 `/hooks` 审阅并信任 CacheGate 即可使用，默认仅提醒，无需先配置。输入 `cachegate help` 查看切换模式等操作。更新时保留之前明确选择的模式。

更新已安装的 GitHub 版本：

```bash
codex plugin marketplace upgrade cachegate
codex plugin add cachegate@cachegate
```

更新由用户主动触发。第一条刷新跟踪 `main` 的 Git 市场快照，第二条安装该来源的插件。发布新版本时需更新插件 manifest 的 `version`。更新后新建会话检查效果，并按 Codex 提示重新审阅 hook。模式和计时状态保存在插件安装目录之外。

卸载插件：

```bash
codex plugin remove cachegate@cachegate
```

卸载交给 Codex 管理，CacheGate 的状态目录默认保留。需要停止跟踪该市场时，再执行 `codex plugin marketplace remove cachegate`。

仓库的 `.agents/plugins/marketplace.json` 指向 `plugins/cachegate`。Codex 管理 Git 快照和插件安装缓存；运行时只需要 Python 标准库，无需运行自定义安装脚本。

## 本地开发安装

仅在需要独立的本地副本时，在项目目录执行：

```bash
python3 scripts/install.py
```

脚本复制文件到 `~/plugins/cachegate`，向 `~/.agents/plugins/marketplace.json` 追加个人市场条目，再调用 `codex plugin add cachegate@<市场名>`。它保留已有内容；同名目录或条目已存在时停止，不覆盖。`--prepare-only` 仅准备插件和市场条目。该副本不随 GitHub 市场更新。

本地脚本会记录当前 Python 解释器和状态目录的绝对路径；GitHub 安装使用 `python3` 和运行时的状态目录配置。两种方式均通过 `${PLUGIN_ROOT}` 查找已安装脚本，不依赖 rtk，也不注册系统 shell 命令。

从原有个人市场安装迁移到 GitHub 源时，先用原市场名卸载旧插件，再按上面的 GitHub 流程安装，避免同一 hook 被加载两次。

## 帮助中的操作

下面的命令在 **Codex 输入框**单独提交，由 hook 本地处理。

| 输入 | 效果 |
| --- | --- |
| `cachegate help` | 展开帮助 |
| `cachegate remind` | 超过 30 分钟时提醒，然后继续发送 |
| `cachegate confirm` | 超过 30 分钟时拦截 |
| `cachegate status` | 查看当前模式 |
| `cachegate allow` | 允许原样重发本会话最近被拦截的消息一次，不会自动发送 |
| `cachegate cancel` | 撤销本会话的待确认记录和单次许可 |

模式在本机使用同一状态目录的会话间共享，下次提交生效。超时被拦截后，可打开帮助选择单次放行、修改模式，或手动 `/clear` 新建会话后重新输入请求。插件不自动执行 `/clear`。

`allow` 和 `cancel` 仅在超时拦截后需要。`allow` 授予一次重发许可；`cancel` 撤销该许可和待确认记录，不会撤回已经发送的消息。不想发送时直接不重发即可，无需执行取消命令。默认的仅提醒模式无需这两个命令。

命令区分大小写，只识别独立的 `cachegate` 或 `cachegate <单个命令>`；普通说明文字、多行请求和代码块中的提及按普通消息处理。单独的未知子命令会显示帮助入口。

## 计时和本地状态

- 阈值固定为严格大于 1,800 秒，恰好 30 分钟仍放行。比较范围是同一 `session_id` 的用户消息；首次见到的会话没有历史时间戳，直接放行。
- 计时从上次**获准提交**开始；被拦截、查看帮助、修改模式、许可和取消都不会刷新时间。许可后的重发获准时才更新时间。
- 单次许可绑定会话、消息文本和模型，使用一次后消耗。改发不同文本或模型需要重新确认。hook 不提供附件内容，因此这不是对附件或最终 API 请求体的完整绑定。
- 恢复已记录会话时沿用时间戳，安装前的历史不回溯。系统时钟回退时按所选模式提醒或拦截。同一运行中的 turn 收到不同用户消息仍分别检查。
- 本地保存模式、会话和 turn ID、时间戳、消息摘要及单次许可状态。正文仅在本次 hook 内用于识别命令和计算摘要，不保存原文，也不读取聊天记录。

默认状态位于 `${CODEX_HOME:-~/.codex}/cachegate/state.sqlite3`。GitHub 安装使用运行时配置；本地安装脚本可通过 `CACHEGATE_DATA_DIR` 指定并固定其他状态目录。旧版数据库的模式和时间戳会保留，首次运行时自动创建单次许可所需的表。

旧版终端 `config` 入口仍兼容；需操作与插件相同的数据目录。本地开发安装脚本不会覆盖已有副本，GitHub 版本则使用上面的 Codex 更新流程。

## 开销与边界

运行时仅使用 Python 标准库和本地 SQLite。每次提交启动一次短进程，不调用模型、不开启常驻服务，也不注册 MCP 工具或技能。

控制命令使用 `continue: false` 和 `stopReason` 本地显示结果；超时拦截使用 `decision: block`；仅提醒使用 `systemMessage`。不输出额外模型上下文。集成测试检查真实 HTTP 请求体，确认帮助、模式切换、放行、取消及提示文本未进入后续模型请求。

**30 分钟是检查阈值，无法保证 KV 缓存命中。** `/clear` 会开始新的会话上下文，不能恢复或延长过期缓存。参见 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) 和 [Codex /clear](https://developers.openai.com/codex/cli/slash-commands#clear-the-terminal-and-start-a-new-chat-with-clear)。

根据 [Codex hooks 的运行约定](https://developers.openai.com/codex/hooks)，hook 未信任、被禁用、进程无法启动或执行超时时，宿主可能跳过检查。插件捕获到的本地存储或输入错误会返回拦截结果，但无法控制宿主跳过 hook。其他 hook 或网络故障也可能阻止已获本插件批准的请求，因此时间戳是提交活动的近似值。任务内部的模型续发、工具循环和网络重试不在观察范围内。

## API 输入价格快照

**Update time（本项目核验时间）：2026-09-17 03:52:49 UTC（北京时间 11:52:49，UTC+8）。**

来源：[OpenAI API Pricing](https://developers.openai.com/api/docs/pricing)；计费分类参见 [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)。机器可读数据保存在 [data/openai-input-prices.json](data/openai-input-prices.json)。此时间表示本项目最后一次核对并保存价格的时间，不是官方调价生效时间。

单位统一为 **美元 / 100 万输入 tokens**，只记录输入费用。收录来源页的 Flagship models 和 Specialized models / Codex，覆盖 Standard 和 Fast；以下表格列出快照中的全部记录。Batch、Flex、地区附加费、工具费用、协议折扣和第三方服务商价格不在此表内。

<details>
<summary>Standard 输入价格（38 个模型）</summary>

| 模型 | 短上下文：普通输入 | 缓存命中 | 缓存写入 | 长上下文：普通输入 | 缓存命中 | 缓存写入 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-6-astra | 10.00 | 1.00 | 12.50 | 20.00 | 2.00 | 25.00 |
| gpt-5.6-sol | 4.00 | 0.40 | 5.00 | 8.00 | 0.80 | 10.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 2.50 | 4.00 | 0.40 | 5.00 |
| gpt-5.6-luna | 0.20 | 0.02 | 0.25 | 0.40 | 0.04 | 0.50 |
| gpt-5.5 (&lt;272K context length) | 5.00 | 0.50 | — | 10.00 | 1.00 | — |
| gpt-5.5-pro (&lt;272K context length) | 30.00 | — | — | 60.00 | — | — |
| gpt-5.4 (&lt;272K context length) | 2.50 | 0.25 | — | 5.00 | 0.50 | — |
| gpt-5.4-mini | 0.75 | 0.075 | — | — | — | — |
| gpt-5.4-nano | 0.20 | 0.02 | — | — | — | — |
| gpt-5.4-pro (&lt;272K context length) | 30.00 | — | — | 60.00 | — | — |
| gpt-5.2 | 1.75 | 0.175 | — | — | — | — |
| gpt-5.2-pro | 21.00 | — | — | — | — | — |
| gpt-5.1 | 1.25 | 0.125 | — | — | — | — |
| gpt-5 | 1.25 | 0.125 | — | — | — | — |
| gpt-5-mini | 0.25 | 0.025 | — | — | — | — |
| gpt-5-nano | 0.05 | 0.005 | — | — | — | — |
| gpt-5-pro | 15.00 | — | — | — | — | — |
| gpt-4.1 | 2.00 | 0.50 | — | — | — | — |
| gpt-4.1-mini | 0.40 | 0.10 | — | — | — | — |
| gpt-4.1-nano | 0.10 | 0.025 | — | — | — | — |
| gpt-4o | 2.50 | 1.25 | — | — | — | — |
| gpt-4o-2024-05-13 | 5.00 | — | — | — | — | — |
| gpt-4o-mini | 0.15 | 0.075 | — | — | — | — |
| o1 | 15.00 | 7.50 | — | — | — | — |
| o1-pro | 150.00 | — | — | — | — | — |
| o3-pro | 20.00 | — | — | — | — | — |
| o3 | 2.00 | 0.50 | — | — | — | — |
| o4-mini | 1.10 | 0.275 | — | — | — | — |
| o3-mini | 1.10 | 0.55 | — | — | — | — |
| gpt-4-turbo-2024-04-09 | 10.00 | — | — | — | — | — |
| gpt-4-0613 | 30.00 | — | — | — | — | — |
| gpt-3.5-turbo | 0.50 | — | — | — | — | — |
| gpt-3.5-turbo-0125 | 0.50 | — | — | — | — | — |
| gpt-3.5-turbo-1106 | 1.00 | — | — | — | — | — |
| gpt-3.5-turbo-instruct | 1.50 | — | — | — | — | — |
| davinci-002 | 2.00 | — | — | — | — | — |
| babbage-002 | 0.40 | — | — | — | — | — |
| gpt-5.3-codex | 1.75 | 0.175 | — | — | — | — |

</details>

<details>
<summary>Fast 输入价格（20 个模型）</summary>

| 模型 | 短上下文：普通输入 | 缓存命中 | 缓存写入 | 长上下文：普通输入 | 缓存命中 | 缓存写入 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| gpt-6-astra | 20.00 | 2.00 | 25.00 | 40.00 | 4.00 | 50.00 |
| gpt-5.6-sol | 8.00 | 0.80 | 10.00 | 16.00 | 1.60 | 20.00 |
| gpt-5.6-terra | 4.00 | 0.40 | 5.00 | 8.00 | 0.80 | 10.00 |
| gpt-5.6-luna | 0.40 | 0.04 | 0.50 | 0.80 | 0.08 | 1.00 |
| gpt-5.5 (&lt;272K context length) | 12.50 | 1.25 | — | — | — | — |
| gpt-5.4 (&lt;272K context length) | 5.00 | 0.50 | — | — | — | — |
| gpt-5.4-mini | 1.50 | 0.15 | — | — | — | — |
| gpt-5.2 | 3.50 | 0.35 | — | — | — | — |
| gpt-5.1 | 2.50 | 0.25 | — | — | — | — |
| gpt-5 | 2.50 | 0.25 | — | — | — | — |
| gpt-5-mini | 0.45 | 0.045 | — | — | — | — |
| gpt-4.1 | 3.50 | 0.875 | — | — | — | — |
| gpt-4.1-mini | 0.70 | 0.175 | — | — | — | — |
| gpt-4.1-nano | 0.20 | 0.05 | — | — | — | — |
| gpt-4o | 4.25 | 2.125 | — | — | — | — |
| gpt-4o-2024-05-13 | 8.75 | — | — | — | — | — |
| gpt-4o-mini | 0.25 | 0.125 | — | — | — | — |
| o3 | 3.50 | 0.875 | — | — | — | — |
| o4-mini | 2.00 | 0.50 | — | — | — | — |
| gpt-5.3-codex | 3.50 | 0.35 | — | — | — | — |

</details>

表中“—”对应 JSON 的 `null`，表示来源未列出对应单价，不能按零费用处理，也不能据此推断该档位可用。短、长上下文沿用来源页的分类，模型名称中的上下文限定条件也保留原文；本快照不自行推断分档阈值。Codex 专项表中未区分上下文档位的价格暂列在短上下文栏。

普通输入、缓存命中、缓存写入是不同的输入计费分类。同一 token 若按缓存写入价计费，不再叠加普通输入价；因此后续估算“未命中”开销时，需要区分普通输入和写入缓存的部分。GPT-5.6 Sol 的当前价格含促销，官方注明至少持续至 2026-11-21；GPT-6 Astra 的 Fast 不适用于欧盟数据驻留。

更新价格时，重新核对官方来源，同步修改 JSON、核验时间和本节表格。当前仅保存静态数据与展示价格，hook 尚未读取价格表或显示金额；不新增运行时网络请求，现有提醒和拦截行为保持不变。

## 验证

```bash
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy env CACHEGATE_CODEX_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

测试命令中的 `rtk proxy` 用于本工作区约定，运行插件不需要它。

集成测试使用独立临时 `CODEX_HOME`、测试状态目录和 loopback 模型服务，分别安装本地脚本生成的副本和仓库直接分发的插件包，验证默认仅提醒、直接输入命令、超时拦截、单次放行、取消、模式切换及请求内容。它不调用真实模型，仅在测试会话对本项目 hook 设置 `bypass_hook_trust`，不改变用户的信任配置。Codex 安装插件时可能访问自己的插件目录服务。临时目录保留日志和请求体供检查。

集成测试覆盖 app-server 的 hook 事件及模型请求边界；另在隔离的 Codex CLI 终端中检查了帮助展开和模式切换提示。尚未验证 Windows/macOS、桌面端或 IDE。

插件打包方式参见 [Build plugins](https://developers.openai.com/plugins/build/plugins)。
