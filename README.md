# LitBridge

**面向 AI agent 的本地文献工作流与文档规范化服务。**

LitBridge 保留检索结果、来源与原件，管理可恢复的获取任务，将文档转换成带原页/节点定位的 Markdown，再通过 CLI 或 MCP 提供阅读。所有网络来源通过开放 Provider 协议接入。本体不捆绑任何文献网站实现、网站目录、登录配方或来源密钥。

## 能力

- 能力声明与显式启用的 Provider 协议 1.0；多来源结果合并、DOI/来源身份、缓存、超时隔离和熔断。
- 持久任务队列、租约、失败分类、人工恢复和已有原件复用；获取、格式校验、规范化与阅读分别报告结果。
- PDF/XML 原件有界存储、SHA-256 校验；规范化 Markdown/结构化块及原页/节点定位。显式本地 HTML 导入也可规范化。
- 可选离线布局/OCR、按页检查点与取消恢复；可选云端公式识别和可配置第三方图片模型对照。云端默认关闭。
- CLI 与 16 个 MCP 工具共用服务层。通用浏览器工具仅执行 Provider 提供的受控目标，不提供网站规则。

## 安装

需要 Python 3.11+。从本仓库 Release 下载 wheel，或在源码目录安装：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[mcp]"
Copy-Item examples/litbridge.toml litbridge.local.toml
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml providers
```

未安装、启用 Provider 时，来源列表为空，这是本体默认行为。用于验证开放协议的中立本地目录示例：

```powershell
.venv/Scripts/python.exe -m pip install --no-deps -e examples/localcatalog
```

将自己拥有的元数据保存为 JSON 数组，例如 `[{"title":"Synthetic study","doi":"10.5555/example","year":2024}]`，在本地配置中填写：

```toml
enabled_plugins = ["localcatalog"]
[plugin_options.localcatalog]
path = "D:/YOUR_PATH/catalog.json"
```

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml search "Synthetic" --provider localcatalog
```

本地目录示例仅提供元数据，示例 DOI 不保证在线存在，不提供全文。其它来源由用户独立安装，并显式加入 enabled_plugins；本体不依赖其仓库或实现。

## 使用与 MCP

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml doctor
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml mcp
```

MCP 使用 stdio，可参照 [配置示例](examples/mcp.json)。工具包括 providers、search、resolve、access、retrieve、references、import_url、doctor、batch、job_create、job_run、job_status、job_history、human_run、normalize、read。先检查来源能力，再筛选并获取少量目标，保存原件后分别检查 normalization 与 read。详细步骤见 [阅读工作流](docs/READING_WORKFLOW.md)。

Release 中 `litbridge-core-plugin.zip` 是通用 Codex/MCP 客户端包装，内含本体服务，仍不含文献站点实现。解压后按包内说明安装依赖，提供本机配置。Python wheel 适合已有运行环境；source ZIP 适合开发。尚未发布到 PyPI，不应假定 `pip install litbridge` 会取得此版本。

## 规范化与模型

轻量解析无需大模型；复杂布局可选择独立离线运行环境。扫描页、双栏顺序、表格和公式都可能需要复核；ready/处理进度不表示质量准确率。未知打开密码的文档跳过，原件保留。

第三方图片模型通过明确的本地配置接入，凭证保留在进程环境或忽略文件，HTTPS 默认要求。外发仅在显式启用的所选公式裁剪上进行；模型一致率不等于准确率。详情见 [模型配置](docs/MODEL_SERVICES.md) 与 [规范化](docs/NORMALIZATION.md)。

## 安全与边界

文献、网页和模型输出始终视为不可信数据。Provider 是用户信任的 Python 包，当前不是进程沙箱；不会自动建立订阅权限、完成验证码或代替登录。API 权利、浏览器权利、原件成功和可读性需要各自验证。核心网络工具限制 HTTPS、目标域、大小和凭证跨域跳转。

不要提交密钥、机构会话、浏览器 profile、下载论文、全文、模型权重或本机报告。云端增强可能发送选定图片并产生费用，应先查看配置和限额。[安全说明](docs/SECURITY.md)详述这些边界。

## 开发

```powershell
.venv/Scripts/python.exe -m pip install -e ".[dev,mcp,browser]"
.venv/Scripts/python.exe -m pytest -m "not browser and not live" -q
.venv/Scripts/python.exe scripts/update_manifest.py
.venv/Scripts/python.exe scripts/check_manifest.py
.venv/Scripts/python.exe scripts/build_plugin.py
```

公开 API：[Provider 协议](docs/PROVIDER_PROTOCOL.md)。版本：[更新记录](CHANGELOG.md)。CI 在独立环境验证本体、官方 MCP SDK 和发布包，不访问用户机构会话。网络/浏览器验收不能由合成测试代替。

当前 0.2.0 是接口拆分版本，旧配置中来源专属字段应交由对应独立 Provider 配置；本体默认零来源。仓库现已公开，许可证尚未指定，公开可见不自动授予开源许可。
