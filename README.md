# inkbot

家庭自动化小工具集合，包含两个相互配合的本地服务：

| 子项目 | 作用 |
|---|---|
| **inkboard/** | 家庭信息看板（InkBoard）。HTTP 服务（默认端口 8765），墨水屏/平板展示日程、家庭作业、天气、课程表等；带管理端。`sync_events.py` 从企业微信日历拉取日程。 家庭成员使用企业微信日历的共享日历功能，编辑和共享家庭日程。 管理端可以灵活添加或删除时间天气、家庭日程、课程表、待办、留言组件，编辑组件的大小、位置等。 |
| **qq-homework-bot/** | QQ 作业收集机器人。手机 QQ 转发老师作业消息给机器人，自动解析（可选大模型整理）后写入 InkBoard「家庭作业」组件，并可转发到企业微信群。 |

两个服务通过本机 HTTP（`127.0.0.1:8765`）通信，均由 Windows 任务计划程序在登录时自启。
三个任务统一用 `pythonw.exe` 无窗口启动（不弹控制台）；程序检测到无控制台时会把 stdout/stderr 重定向到各自日志文件，便于后台排查。

- `InkBoardServer` → `pythonw.exe inkboard/server.py --port 8765`（日志 → `inkboard/server.log`）
- `InkBoardSyncEvents` → `pythonw.exe inkboard/sync_events.py --source wecom`（定时从企微同步日程，无窗口；日志由脚本自写到 `sync.log`）
- `QQHomeworkBot` → `pythonw.exe qq-homework-bot/homework_bot.py`（日志 → `qq-homework-bot/homework_bot.log`）

> 手动调试时仍可用各目录 `start.bat`（走 `python.exe` 带控制台，可直接看输出）。
> 注意：pythonw 下程序内部拉起的控制台子进程（node/curl）已加 `CREATE_NO_WINDOW`，否则仍会闪黑窗。


## 目录结构

```
inkbot/
├── inkboard/
│   ├── server.py          看板 HTTP 服务
│   ├── sync_events.py     企微日程同步（定时任务直接调用，自带日志）
│   ├── start.bat
│   ├── smoke_test.py      离线冒烟测试
│   └── web/               前端页面（显示端 + 管理端）
└── qq-homework-bot/
    ├── homework_bot.py    机器人主程序
    ├── selftest.py        离线自测（无需 appid）
    ├── config.example.json 配置模板（复制为 config.json 填写）
    ├── requirements.txt
    └── start.bat
```

各子项目的详细说明见其目录内的 `README.md`。

## 首次部署（换机器 / 重新克隆后）

1. 安装 Python 3.13，创建 qqbot 虚拟环境并装依赖：
   ```bat
   python -m venv C:\Users\<你>\.workbuddy\binaries\python\envs\qqbot
   C:\Users\<你>\.workbuddy\binaries\python\envs\qqbot\Scripts\python.exe -m pip install -r qq-homework-bot\requirements.txt
   ```
2. 复制配置模板并填写密钥（**config.json 不入库，需自行准备**）：
   - `qq-homework-bot/config.json`（QQ appid/secret、可选 LLM api_key、企微 webhook）
   - inkboard 的 `ADMIN_CREDENTIALS.txt`、`READONLY_TOKEN.txt`（管理密码 / 只读令牌）
3. 注册任务计划（登录自启），路径按实际位置调整，参考各 README。

> 密钥、运行数据（`data/`）、日志均**不入库**，见 `.gitignore`。

## 备份与同步（GitHub）

本仓库只备份**代码与配置模板**，不含任何密钥和个人数据。

日常提交：

```powershell
cd <仓库根>\inkbot
git add -A
git commit -m "update"
git push
```

换新机器克隆后，按上面「首次部署」补齐密钥和数据目录即可恢复运行。
