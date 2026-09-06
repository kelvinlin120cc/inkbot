# inkbot

墨水屏家庭信息看板，服务部署在家庭主机/电脑上（长开机），自动化获取、呈现日期天气、家庭日程、课程表和家庭作业等信息，墨水屏呈现哪些信息可配置，信息组件可扩展

含两个相互配合的本地服务：

| 子项目                  | 作用                                                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **inkboard/**        | 家庭信息看板主体应用。HTTP 服务（默认端口 8765），墨水屏/平板展示日程、家庭作业、天气、课程表等；带管理端。可以从企业微信日历拉取日程。管理端可灵活增删时间天气、家庭日程、课程表、待办、留言等组件，可视化编辑组件大小、位置等。 |
| **qq-homework-bot/** | QQ 作业收集机器人。手机 QQ 转发老师作业消息给机器人，自动解析（可选大模型整理）后写入 InkBoard「家庭作业」组件。                                                       |

两个服务通过本机 HTTP（`127.0.0.1:8765`）通信，均由 Windows 任务计划程序在登录时自启。

## 目录结构

```
inkbot/
├── inkboard/
│   ├── server.py          看板 HTTP 服务
│   ├── sync_events.py     企微日程同步（定时任务直接调用，自带日志；依赖 node + @wecom/cli）
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

***

## 内容从哪来：两种模式

看板上的「家庭日程」和「家庭作业」都各有**自动同步**和**手动**两种录入方式，按需选用（可混用）。

### 家庭日程

| 模式                   | 说明                                                                                                                                                                                          |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **① 企业微信日程自动同步**（推荐） | 给需要编辑管理家庭日程的成员开通企业微信，只通过看板查看的成员无需开通，用**企业微信的日程**作为家庭日程载体。若多人都要编辑/管理，创建一个**共享日程**（日历）并授予成员编辑权限（详见[附录 A](#附录-a企业微信创建共享家庭日程)）。`InkBoardSyncEvents` 任务每 10 分钟自动把企微日程拉到看板，在企微里新建/改日程后最迟 10 分钟上屏。 |
| **② 手动模式**           | 在管理端「日程」页直接新增/编辑/删除日程（标题、日期、起止时间）。适合不用企微、或临时补一条的情况。                                                                                                                                         |

> 两种模式写的都是同一批日程数据。自动同步会按周期用企微内容覆盖（`events.set`），手动加的日程若不同步来源，下次同步可能被企微结果替换；建议以企微为准、手动仅做临时补充。

### 家庭作业

| 模式                   | 说明                                                                                                                                                                                                                |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **① QQ 机器人转发模式**（推荐） | 学校老师在 **QQ 群**发作业时，直接**转发**或**复制粘贴**发给 QQ 机器人；机器人调用配置的大模型把口语化作业**格式化、拆条、识别科目/截止日**，推送到墨水屏「家庭作业」组件，同时发到企业微信群。若老师用**其它方式**（微信、短信、纸质、口头等）布置，同样把内容**复制后发给 QQ 机器人**即可，多条可用合并转发。机器人创建与参数配置见[附录 B](#附录-b创建-qq-机器人并配置参数)。 |
| **② 手动模式**           | 在管理端「作业」页手动新增作业；也可把作业原文粘贴进「AI 格式化」框，点「格式化」让大模型拆条后一键加入看板。                                                                                                                                                          |

> 作业在看板上按「教学日」归组，每天 08:00 为换日点（周五作业会保留到周一早上）。

***

## 首次部署（换机器 / 重新克隆后）

> 路径里的 `<用户>` 换成你的 Windows 用户名；`<仓库根>` 换成克隆后的 `inkbot` 目录绝对路径（如 `D:\workbuddy\家庭日程管理\inkbot`）。

### 1. 安装运行时

- **Python 3.13**（inkboard 用，纯标准库无需装包）。本机约定放在
  `C:\Users\<用户>\.workbuddy\binaries\python\versions\3.13.12\`（含 `python.exe` / `pythonw.exe`）。
- **Node.js**（企微日程同步需要）。本机约定放在
  `C:\Users\<用户>\.workbuddy\binaries\node\versions\22.22.2-2\`，并在其全局安装企微 CLI：
  ```bat
  "C:\Users\<用户>\.workbuddy\binaries\node\versions\22.22.2-2\npm.cmd" i -g @wecom/cli
  ```
  > 装完需按 @wecom/cli 的引导**登录并授权机器人「日程」权限**（企业微信「工作台 → 智能机器人」里对该机器人授权日程读取）。授权过期时同步日志会报 `errcode=850003`，重新授权即可。
  > 不用企微同步（纯手动日程）可跳过 Node 这一步。

### 2. 建 qqbot 虚拟环境并装依赖

```bat
"C:\Users\<用户>\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m venv C:\Users\<用户>\.workbuddy\binaries\python\envs\qqbot
C:\Users\<用户>\.workbuddy\binaries\python\envs\qqbot\Scripts\python.exe -m pip install -r "<仓库根>\qq-homework-bot\requirements.txt"
```

### 3. 准备配置（密钥/数据均不入库，需自行准备）

- 复制 `qq-homework-bot/config.example.json` 为 **`qq-homework-bot/config.json`** 并填写：
  - `appid` / `secret`：QQ 机器人凭据（见[附录 B](#附录-b创建-qq-机器人并配置参数)）
  - `llm.api_key` / `model` / `base_url`：大模型（OpenAI 兼容，如火山方舟）；不填则回退规则解析
  - `wecom.webhook`：企业微信群机器人 key（作业同步推送到企微群）
  - `allowed_openids`：先留空，首次转发后看 `homework_bot.log` 里打印的 openid 再填，锁定仅家人可用
- inkboard 默认本地开放、公网由 Cloudflare Access 在边缘鉴权，**不需要**本地账号密码文件。
  若你要重新启用服务端 Basic 鉴权 / 只读令牌，才需准备 `inkboard/ADMIN_CREDENTIALS.txt`、`inkboard/READONLY_TOKEN.txt` 并给 `InkBoardServer` 加对应启动参数（一般无需）。
- 数据目录 `data/`（看板内容、作业存档、图片、备份快照）不入库；首次启动会自动创建并写入示例数据。

### 4. 注册任务计划（登录自启 + 定时同步）

用 **PowerShell（管理员）** 执行一次（把路径替换为实际值）：

```powershell
$py   = "C:\Users\<用户>\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
$pyw  = "C:\Users\<用户>\.workbuddy\binaries\python\envs\qqbot\Scripts\pythonw.exe"
$root = "<仓库根>"   # 如 D:\workbuddy\家庭日程管理\inkbot

# 看板服务（登录自启，常驻）
$a1 = New-ScheduledTaskAction -Execute $py -Argument "server.py --port 8765" -WorkingDirectory "$root\inkboard"
Register-ScheduledTask -TaskName "InkBoardServer" -Action $a1 -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Description "InkBoard 看板服务" -Force

# QQ 作业机器人（登录自启，常驻；用 qqbot 虚拟环境的 pythonw）
$a2 = New-ScheduledTaskAction -Execute $pyw -Argument "homework_bot.py" -WorkingDirectory "$root\qq-homework-bot"
Register-ScheduledTask -TaskName "QQHomeworkBot" -Action $a2 -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Description "QQ 作业收集机器人" -Force

# 企微日程同步（登录后每 10 分钟一次）
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
$a3  = New-ScheduledTaskAction -Execute $py -Argument "sync_events.py --source wecom" -WorkingDirectory "$root\inkboard"
Register-ScheduledTask -TaskName "InkBoardSyncEvents" -Action $a3 -Trigger $trg -Description "企微日程同步到看板" -Force
```

### 5. 启动并自检

- 手动启动三个任务（或重启/重新登录），浏览器打开 `http://127.0.0.1:8765/`（显示端）和 `http://127.0.0.1:8765/admin`（管理端）。
- 看板自检：`python inkboard\smoke_test.py`（离线逻辑/前端静态检查）。
- 机器人自测：`qqbot` 环境的 python 跑 `python qq-homework-bot\selftest.py --send-wecom`（会真推一条作业到看板和企微，自测数据到管理端手动删除）。
- 日志：`inkboard\server.log`、`inkboard\sync.log`、`qq-homework-bot\homework_bot.log`。

> 密钥、运行数据（`data/`）、日志均**不入库**，见 `.gitignore`。

### 6. 墨水屏显示信息看板

服务在小主机/电脑上跑起来后，把信息看板摆上墨水屏：

1. **编辑内容与版式**：在家庭主机/电脑上打开任意浏览器，访问 `http://127.0.0.1:8765/admin`（管理端），添加/删除需要的组件（时间天气、家庭日程、课程表、家庭作业、待办、留言等），并可视化拖拽排布组件位置、调整大小。
2. **墨水屏显示**：在墨水屏平板上安装 **EinkBro 浏览器**（推荐，专为墨水屏优化、支持常亮/定时刷新），打开显示端地址 `http://x.x.x.x:8765/`，其中 `x.x.x.x` 是**同一局域网内**那台家庭主机/电脑的局域网 IP（在主机上用 `ipconfig` 查看，如 `192.168.x.x`；管理端首页也会打印局域网地址）。显示端常驻自动刷新，无需操作。

> 提示：手机/电脑管理端用 `/admin`，墨水屏显示端用 `/`（根路径）。两者同一份数据，管理端改完墨水屏最迟几十秒自动更新。若墨水屏打不开，检查它与主机是否在同一 WiFi、Windows 防火墙是否放行（首次启动会弹窗，允许「专用网络」）。

***

## 支持的操作系统

代码本身是跨平台 Python（路径全用 `__file__` + `os.path.join`，HTTP 走标准库，前端是纯 ES5 网页），
但本仓库在 **Windows 11** 上开发，开箱即用的部署脚本/自启方式是 Windows 的。各平台情况：

| 能力                                       | Windows 10/11 | macOS             | Linux                  |
| ---------------------------------------- | ------------- | ----------------- | ---------------------- |
| InkBoard 看板服务（server.py）                 | ✅             | ✅                 | ✅                      |
| QQ 作业机器人（homework\_bot.py）               | ✅             | ✅                 | ✅（需系统有 `curl`）         |
| 管理端 / 显示端网页                              | ✅             | ✅                 | ✅                      |
| 企微日程自动同步（sync\_events.py --source wecom） | ✅             | ✅                 | ✅（需 Node + @wecom/cli） |
| 日程/作业**手动模式**                            | ✅             | ✅                 | ✅                      |
| 无窗口后台运行                                  | `pythonw.exe` | `pythonw`/`nohup` | `nohup`/systemd        |
| 开机自启                                     | 任务计划程序        | launchd           | systemd                |

### 跨平台差异与处理

- **Python**：3.13（3.9+ 亦可）。inkboard 纯标准库、无需装包；QQ 机器人需 `pip install -r qq-homework-bot/requirements.txt`（`qq-botpy`）。
- **curl**：QQ 机器人所有出站 HTTP 都调用系统 `curl`。Windows 10+ 与 macOS 自带；**精简版 Linux 需先装**：`sudo apt install curl`（或 `yum install curl`）。
- **企微日程自动同步（跨平台）**：`sync_events.py` 通过 Node 跑 `@wecom/cli` 拉企微日历。node 与 wecom.js 的探测已做跨平台：先查 Windows 托管 node 的约定目录，再在 PATH 中找 `node`/`node.exe`，并通过 `npm root -g`、`require.resolve('@wecom/cli')` 及 node 安装前缀下的全局 `node_modules` 定位包。因此三个平台都只需：
  - 装 Node，全局安装 CLI：`npm i -g @wecom/cli`（确保 `node`、`npm` 在 PATH 中）；
  - 按 CLI 引导登录，并在企业微信「工作台 → 智能机器人」授权机器人「日程」权限；
  - 跑 `python3 sync_events.py --source wecom --dry-run` 能拉到日程即成功。
  - 不想装 Node：日程改用管理端**手动模式**即可完全跳过企微依赖，看板其余功能不受影响。
- **无窗口 / 自启**：`CREATE_NO_WINDOW`、`pythonw.exe`、`.bat`、任务计划程序都是 Windows 专用；代码已用 `getattr(subprocess, "CREATE_NO_WINDOW", 0)` 安全降级，非 Windows 下无副作用。macOS/Linux 用 `nohup python3 xxx.py &` 后台跑，自启用 launchd（macOS）或 systemd（Linux）。

### macOS / Linux 手动运行（开发/调试）

```bash
# 看板（终端前台，Ctrl-C 停止）
python3 inkboard/server.py --port 8765

# QQ 机器人（另开一个终端；先 cp config.example.json config.json 并填写）
python3 -m venv ~/.venv/qqbot && ~/.venv/qqbot/bin/pip install -r qq-homework-bot/requirements.txt
(cd qq-homework-bot && ~/.venv/qqbot/bin/python homework_bot.py)

# 后台常驻（nohup，日志重定向到文件）
nohup python3 inkboard/server.py --port 8765 >> inkboard/server.log 2>&1 &
```

### Linux systemd 自启示例

`/etc/systemd/system/inkboard.service`：

```ini
[Unit]
Description=InkBoard server
After=network-online.target

[Service]
WorkingDirectory=/opt/inkbot/inkboard
ExecStart=/usr/bin/python3 /opt/inkbot/inkboard/server.py --port 8765
Restart=always
User=你的用户名

[Install]
WantedBy=multi-user.target
```

QQ 机器人同理再建一个 service（`WorkingDirectory=/opt/inkbot/qq-homework-bot`，`ExecStart=.../python homework_bot.py`）。
然后 `sudo systemctl daemon-reload && sudo systemctl enable --now inkboard`。企微定时同步再加一个 `*.timer`（每 10 分钟）或用 cron：

```cron
*/10 * * * * cd /opt/inkbot/inkboard && /usr/bin/python3 sync_events.py --source wecom >> sync.log 2>&1
```

> macOS 用 launchd 的 `~/Library/LaunchAgents/*.plist`（`RunAtLoad=true` 常驻、`StartInterval=600` 定时）实现等价能力，这里不再展开。

***

## 备份与同步（GitHub）

本仓库只备份**代码与配置模板**，不含任何密钥和个人数据。

日常提交：

```powershell
cd <仓库根>\inkbot
git add -A
git commit -m "update"
git push
```

换新机器克隆后，按上面「首次部署」补齐运行时、密钥和数据目录即可恢复运行。

***

## 附录 A：企业微信创建共享家庭日程

目标：让多位家庭成员都能在**同一个日历**里新建/修改家庭日程，机器人再把它同步到墨水屏。

1. **开通企业微信**：管理员在企业微信后台（[work.weixin.qq.com](https://work.weixin.qq.com)）把需要管理日程的家人加入企业（可自建一个家庭「企业」），成员用手机企业微信登录。
2. **创建共享日历（日程）**：
   - 手机企业微信 → 底部「工作台」（或消息页）→ 打开「**日程**」；
   - 进入日历列表 / 侧边栏，选择「**新建日历**」（部分版本叫「添加日历 / 共享日历」）；
   - 名称填「家庭日程」之类；
   - **添加成员**：把家人加进来，并把权限设为「**可编辑 / 可管理**」（不是仅「可查看」），这样大家都能增删改；
   - 保存。成员在自己的日程里勾选该共享日历后，新建日程时选择它，所有人即可见、可改。
3. **授权机器人读取日程**（供 `@wecom/cli` 同步）：
   - 先完成首次部署第 1 步的 `npm i -g @wecom/cli` 并按 CLI 引导登录；
   - 企业微信「工作台 → **智能机器人**」里找到该机器人，确认已开通/授权「**日程**」权限（读取日历）；
   - 跑一次 `python sync_events.py --source wecom --dry-run` 能拉到日程即授权成功。
4. 之后任务每 10 分钟自动同步；同步范围跟随管理端「家庭日程」组件的「显示未来几天」（1–14 天）。

> 若日志出现 `errcode=850003 authorization expired`，表示机器人日程授权过期，回到第 3 步重新授权即可。
> 企业微信界面文案会随版本调整，按钮名称以你看到的为准。

***

## 附录 B：创建 QQ 机器人并配置参数

1. **创建机器人**：打开 QQ 开放平台 <https://q.qq.com>，扫码登录并完成**实名认证（个人主体即可）**。
2. 进入「机器人」→ **创建机器人**，记下 **AppID** 和 **AppSecret**（Secret 只显示一次，妥善保存）。
3. 用你或孩子的 QQ **把机器人加为好友**（机器人详情页有「添加为好友」/体验入口）；若提示沙箱/未发布，把测试 QQ 号加入**白名单**。
4. **回填配置**：把 AppID/AppSecret 填入 `qq-homework-bot/config.json`：
   | 配置项                                  | 说明                                                                                                           |
   | ------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
   | `appid` / `secret`                   | 第 2 步拿到的机器人凭据                                                                                                |
   | `allowed_openids`                    | 允许使用的用户 openid 列表；**先留空** **`[]`**，首次转发成功后到 `homework_bot.log` 找到打印的 `openid` 填回来，锁定仅家人可用                    |
   | `reply_enabled`                      | 机器人是否回复「已记录」等提示，默认 `true`                                                                                    |
   | `media_dir`                          | 作业图片存档目录，留空则用默认 `data/media`                                                                                 |
   | `llm.enabled`                        | 是否启用大模型格式化作业                                                                                                 |
   | `llm.base_url` / `model` / `api_key` | OpenAI 兼容接口。火山方舟示例 `base_url=https://ark.cn-beijing.volces.com/api/v3`，`model` 填接入点 ID；`api_key` 留空则自动回退规则解析 |
   | `llm.vision`                         | `true` 时纯图片作业（黑板/卷子照）发给支持视觉的模型识别；模型不支持图片会自动退化为纯文字重试                                                          |
   | `llm.output_spec`                    | 自定义输出规范（自由文本），会追加到系统提示，留空用内置规范                                                                               |
   | `inkboard.host` / `port` / `token`   | 看板地址，本机默认 `127.0.0.1:8765`；`token` 对应看板「设置」里的 webhook token（未设则留空）                                           |
   | `wecom.enabled` / `wecom.webhook`    | 是否把作业推送到企业微信群；`webhook` 填群机器人地址（企微群 → 群机器人 → 添加 → 复制 Webhook 地址）                                             |
5. **自测与上线**：
   - 离线自测（不需 appid）：`...\envs\qqbot\Scripts\python.exe selftest.py --send-wecom`；
   - 启动 `QQHomeworkBot` 任务（或 `start.bat`），日志出现「机器人已上线」即成功；
   - 手机 QQ 长按老师作业消息 → 转发 → 选该机器人 → 发送，机器人秒回「已记录」，作业上看板、图片和文字进企微群。

> 走的是单聊（C2C）转发，不需要群管理权限、不需要企业认证、不破解客户端；多条消息可**合并转发**一次发出。

