# CacheGate 开发文档

[返回项目主页](../README.md)

## 项目结构

| 路径 | 用途 |
| --- | --- |
| [plugins/cachegate](../plugins/cachegate) | 通过 GitHub 市场分发的插件包 |
| [.agents/plugins/marketplace.json](../.agents/plugins/marketplace.json) | 指向插件目录的市场条目 |
| [scripts/install.py](../scripts/install.py) | 准备独立的本地开发副本 |
| [tests](../tests) | 状态逻辑、hook 进程、安装和 Codex 集成测试 |
| [data/openai-input-prices.json](../data/openai-input-prices.json) | 尚未接入 hook 的静态输入价格快照 |

Codex 管理 Git 快照和插件安装缓存。GitHub 安装不需要运行自定义安装脚本。

## 本地开发安装

在仓库根目录执行：

```bash
python3 scripts/install.py
```

脚本将插件复制到 `~/plugins/cachegate`，向 `~/.agents/plugins/marketplace.json` 追加个人市场条目，再调用 `codex plugin add cachegate@<市场名>`。同名目录或条目已存在时停止，保留已有内容。该副本不会跟随 GitHub 市场更新。

仅准备插件和市场条目：

```bash
python3 scripts/install.py --prepare-only
```

本地安装会固定当前 Python 解释器和状态目录的绝对路径；可在安装前设置 `CACHEGATE_DATA_DIR` 指定状态目录。GitHub 安装使用 `python3` 和运行时的状态目录配置。两种方式均通过 `${PLUGIN_ROOT}` 查找脚本，不依赖 rtk，也不注册系统 shell 命令。

安装后新建会话，在 `/hooks` 审阅并信任 CacheGate。避免同时安装本地副本和 GitHub 版本，迁移步骤见[更新与卸载](../README.md#更新与卸载)。

## 实现约定

运行时使用 Python 标准库和本地 SQLite，每次用户提交启动一次短进程，不开启常驻服务，也不注册 MCP 工具或技能。

| 场景 | Hook 输出 |
| --- | --- |
| 本地控制命令 | `continue: false` 和 `stopReason` |
| 超时拦截 | `decision: block` 和 `reason` |
| 仅提醒 | `systemMessage` |

提示不作为额外模型上下文输出。正文只在当前进程中用于识别命令，不再计算用于许可匹配的消息摘要，持久化字段见[行为与数据](../README.md#行为与数据)。

同一运行中的 turn 收到多条用户消息时，仍逐条检查，即使文本和 turn ID 相同。捕获到的本地存储或输入错误会返回拦截结果；宿主跳过 hook 的情况见[行为与数据](../README.md#行为与数据)。

单次许可只绑定会话，下一条普通消息获准提交后消耗；控制命令不消耗许可。数据库事务将许可检查、计时更新和许可消耗作为一次操作完成。

旧版数据库的模式和时间戳会保留，首次运行时自动创建单次许可所需的表。保留旧表的 `digest` 列，新记录写入空字符串；旧版未消耗的许可按新的“本会话下一条消息”规则生效，无需迁移或清空数据库。旧版终端 `config` 入口仍兼容，使用时需指向与插件相同的状态目录。

## 验证

在仓库根目录执行：

```bash
rtk proxy python3 -m unittest discover -s tests -v
rtk proxy env CACHEGATE_CODEX_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

第一条运行默认测试集；第二条启用 Codex 集成测试。测试命令沿用维护者工作区的 `rtk proxy`，集成测试本身也通过 rtk 启动 Codex app-server，因此需要安装 rtk 和 Codex CLI。插件运行无需 rtk。

集成测试使用独立临时 `CODEX_HOME`、状态目录和 loopback 模型服务，分别安装本地脚本生成的副本及仓库直接分发的插件包，覆盖：

- 默认仅提醒、超时拦截和模式切换。
- 直接输入控制命令、连续拦截后放行较早或修改后的消息，以及取消许可。
- 帮助、模式切换、放行、取消及提示文本不进入后续模型 HTTP 请求。

测试不调用真实模型，仅在测试会话对本项目 hook 设置 `bypass_hook_trust`，不改变用户信任配置。Codex 安装插件时可能访问自身的插件目录服务，临时目录保留日志和请求体供检查。

已验证 Linux、Python 3.12.3 和 Codex CLI/app-server 0.154.0，并在隔离的 CLI 终端检查过帮助展开和模式切换。Windows/macOS、桌面端和 IDE 尚未验证。

## 更新价格与发布

价格快照的单位、核验时间和来源保存在 JSON 中，展示表格位于[主页价格快照](../README.md#api-输入价格快照)。更新时重新核对官方来源，检查 JSON 与主页的价格、缺失值和核验时间一致。价格的缺失值不等于零。

发布插件包变更时，更新 [plugin.json](../plugins/cachegate/.codex-plugin/plugin.json) 的 `version`，并完成相关验证。用户通过主页的市场升级及插件安装命令获取新版，模式和计时状态保存在插件目录之外。

插件打包约定参见 [Build plugins](https://developers.openai.com/plugins/build/plugins)。
