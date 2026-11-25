# AI 内容工作台

在 Windows 本机整理选题、生成初稿、编辑、审阅和记录发布。图文支持微信公众号、今日头条和小红书，结构化拍摄脚本支持微信视频号、快手和抖音。工作台用 React、FastAPI 和 SQLite，生成任务由独立 Worker 执行。

LM Studio 在本机生成正文和脚本。作品在工作台编辑和保存，由你在对应平台人工发布。信息雷达首次安装时关闭定时监测，可按需手动抓取公开来源。选题附带来源链接时，整理资料也可能请求公开来源。

## Windows 快速开始

准备 Python 3.13、Node.js 24（含 npm）。把项目放在支持 SQLite 文件锁的本机磁盘上。Windows 未启用长路径时建议选择层级较浅的目录，给素材文件留出路径长度。在项目根目录打开 PowerShell，安装锁定依赖并构建前端。

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r backend/requirements.lock.txt
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
```

首次启动前用本机终端设置至少 12 个字符的登录密码。输入时终端隐藏密码。

```powershell
& ./.venv/Scripts/python.exe scripts/workbench.py init
& ./.venv/Scripts/python.exe scripts/workbench.py start
```

打开 [本机工作台](http://127.0.0.1:18765) 登录。程序监听回环地址，默认数据目录为 `runtime/`。在原窗口按 Ctrl+C，或从另一个 PowerShell 窗口运行下列命令，以正常停止 Web 和 Worker。

```powershell
& ./.venv/Scripts/python.exe scripts/workbench.py stop
```

端口占用时可在 `start` 命令后加 `--port 18766`。需要另起一份空数据库时，在 `init`、`start` 和 `stop` 都加同一个 `--runtime runtime-demo`。重设密码用 `init --reset-password`，它会让此数据库的现有会话失效。

## 生成第一篇稿件

1. 单独安装 LM Studio，下载并加载一款可在本机运行的语言模型，在 LM Studio 开启本地 API 服务。
2. 在工作台「设置 → 生成与资料库」检查连接。默认地址是 `http://127.0.0.1:1234/v1`，生成使用 LM Studio 当前加载的模型。
3. 在选题中心新建一个选题，填入你有权使用的资料并选择目标平台。批准选题后点「开始创作」。
4. 在内容工坊修改并保存工作稿，随后检查并定稿。审阅完成后可复制文案，手动发布并回填记录。

工作台自带的文风示例供演示。资料库默认路径为运行目录下的 `vault/`。要同步到你自己的 Obsidian 库，把现有目录通过 `start --vault "D:\我的资料库"` 显式传给启动命令。同步写入该库的「工作台」子目录。

## 验证与排障

- 启动后可打开 [健康检查](http://127.0.0.1:18765/api/ready) 查看 Web 启动状态。登录后在设置页查看 Worker 状态。进程日志位于 `runtime/logs/`。
- 运行后端测试可用 `& ./.venv/Scripts/python.exe -m pytest backend/tests`。运行前端构建检查可用 `npm.cmd --prefix frontend run build`。
- 本地生成依赖正在运行且已加载模型的 LM Studio。项目依赖安装和前端构建无需模型。

## 许可

本项目按 [MIT 许可证](LICENSE) 授权。

Python 和 Node.js 依赖适用各自的许可证。需使用 TrendRadar 时请参照[其上游项目及许可证](https://github.com/sansan0/TrendRadar)，本仓库提供适配代码，另行安装 TrendRadar。模型也由使用者单独安装并遵守相应条款。
