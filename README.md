# codex会话同步工具

一个用于浏览、修复、导入、导出和同步本地 Codex 会话记录的 Windows 桌面工具。

## 功能

- 浏览本机 `.codex/sessions` 会话目录。
- 修复单个会话文件或会话目录索引。
- 导入会话并自动修复。
- 导入时如果会话 ID 重复，会弹窗选择“替换”或“增量”。
- “替换”会使用导入会话覆盖本机同 ID 会话。
- “增量”会保留原会话，并给导入会话生成新 ID，列表中会出现两个内容相同但 ID 不同的会话。
- 导出全部会话或选中的会话。
- 提供日志面板显示操作结果。

## 环境

- Windows
- Python 3.11 或更高版本
- Pillow
- PyInstaller

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 运行源码

```powershell
python session_repair_tool.py
```

## 打包 exe

```powershell
pyinstaller --clean --noconfirm CodexSessionRepair.spec
```

打包完成后，程序位于：

```text
dist\codex会话同步工具.exe
```

## 资源文件

程序依赖 `assets` 目录中的背景图和图标文件：

- `assets/halo_background.png`
- `assets/CodexSessionRepair.ico`
- `assets/CodexSessionRepair-icon.png`

## 说明

本软件纯免费，发现收费立即举报。
