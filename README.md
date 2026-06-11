# codex会话同步工具

这是一个 Windows 桌面工具，用于浏览、修复、导入、导出和同步本地 Codex 会话记录，并提供“修复丢失插件技能”的辅助功能。

本仓库包含完整源码、资源文件、依赖清单、PyInstaller 打包配置，以及一个已打包好的 Windows exe。别人拿到这个目录后，可以直接看源码、运行源码，也可以按自己的环境重新构建 exe。

## 文件说明

- `session_repair_tool.py`：主程序源码，Python/Tkinter 桌面应用入口。
- `assets/`：程序图标和背景图资源，源码运行和 exe 打包都需要保留。
- `requirements.txt`：运行和打包所需 Python 依赖。
- `CodexSessionRepair.spec`：PyInstaller 打包配置。
- `codex会话同步工具.exe`：当前已打包好的 Windows 可执行文件，保留给不想自行打包的用户直接运行。
- `.gitignore`：Git 忽略规则。

## 功能

- 浏览本机 `%USERPROFILE%\.codex\sessions` 会话目录。
- 修复单个会话文件或会话目录索引。
- 导入会话并自动修复。
- 导入时如果会话 ID 重复，可以选择“替换”或“增量”。
- 导出全部会话或选中的会话。
- 修复丢失插件技能。
- 在界面日志面板显示操作结果。

## 环境要求

- Windows 10/11。
- Python 3.11 或更高版本。
- 能正常执行 `python` 和 `pip` 命令。

## 运行源码

如果只想直接使用，也可以双击根目录的：

```text
codex会话同步工具.exe
```

如果要从源码运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe session_repair_tool.py
```

## 构建 exe

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm CodexSessionRepair.spec
```

构建完成后，新的 exe 会生成在：

```text
dist\codex会话同步工具.exe
```

## 插件技能修复功能

软件界面中有按钮：

```text
修复丢失插件技能
```

该功能会检查当前 Windows 用户目录下的 Codex 配置和插件缓存：

- `%USERPROFILE%\.codex\config.toml`
- `%USERPROFILE%\.codex\plugins\cache\openai-curated`
- `%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\plugins\openai-primary-runtime`

它使用当前登录用户的用户目录，不绑定固定用户名，例如不依赖 `LYNX`。

也可以用命令行执行同一功能：

```powershell
python session_repair_tool.py --repair-plugins
```

执行后会在源码目录生成：

```text
plugin_skill_repair_report.md
```

这是运行报告，不是源码必需文件。

## 注意事项

- 运行“修复丢失插件技能”后，建议重启 Codex，让插件和技能列表重新加载。
- 该功能只启用本地缓存中实际存在的插件，不会把 marketplace 中所有可安装插件都强行启用。
- 插件技能能显示，不代表外部平台账号、CLI、网络和授权都已经完成。例如 Vercel、Netlify、Canva 等仍可能需要各自登录或授权。
- `build/`、`dist/`、`__pycache__/`、`.venv/` 是运行或构建过程产生的临时目录，不建议提交到 GitHub。
- 本仓库按源码为主整理，同时保留根目录 exe 方便直接试用；如果正式发布，建议后续把 exe 同步放到 GitHub Releases。

## 已验证

当前版本已验证：

- `python -m py_compile session_repair_tool.py` 通过。
- `python session_repair_tool.py --repair-plugins` 可执行。
- `python -m PyInstaller --clean --noconfirm CodexSessionRepair.spec` 可成功构建 exe。
- 构建输出位置为 `dist\codex会话同步工具.exe`。

## 说明

本软件免费使用。发现收费倒卖请自行甄别和举报。
