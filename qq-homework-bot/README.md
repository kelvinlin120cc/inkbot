# QQ 作业收集机器人（方案 C：手动转发）

把手机 QQ 班级群里老师发的作业，转手一步推到**家庭信息看板（InkBoard）**和**企业微信群**。

用法只有三步：手机 QQ 长按老师消息 → 转发 → 选这个机器人。之后机器人自动回一条"已记录"，作业文本进看板「家庭作业」组件、图片直接发到企微手机上。

---

## 为什么是"转发给机器人"

| 路线 | 结论 |
|---|---|
| QQ 群消息全量接收 | **走不通**。要收群里所有消息必须由群主在群设置里把「机器人可获取的群聊消息范围」改成「获取群内全部消息」，个人拿不到 |
| 手机通知栏监听转发 | 可行，但**图片作业拿不到内容**（通知里只有"[图片]"三个字），而小学作业大量是拍卷子照片 |
| **转发给自建机器人（本方案）** | **推荐**。走的是单聊（C2C），不需要任何群权限、不需要企业认证、不需要群主配合；图片会以附件形式完整送达 |

本方案用 QQ 官方开放平台的 `C2C_MESSAGE_CREATE` 事件（公域 Intent `public_messages`），不破解客户端、不碰协议，无封号风险。

---

## 配置（首次约 10 分钟）

### 1. 创建 QQ 机器人

1. 打开 <https://q.qq.com>，扫码登录，完成实名认证（**个人主体即可**）
2. 进入「机器人」→ 创建机器人，记下 **AppID** 和 **AppSecret**（Secret 只显示一次）
3. 在机器人详情页找到「添加为好友」/ 体验入口，用你或孩子的 QQ 把它加为好友
4. 若平台提示机器人处于沙箱/未发布状态，把你的 QQ 号加进测试白名单后再试

> 平台页面会随版本调整，具体按钮名称以你看到的为准。

### 2. 填配置

复制 `config.example.json` 为 `config.json`，各字段含义：

```json
{
  "appid": "你的AppID",
  "secret": "你的AppSecret",
  "allowed_openids": [],
  "reply_enabled": true,
  "media_dir": "",
  "llm":     { "enabled": true, "base_url": "https://ark.cn-beijing.volces.com/api/v3",
               "api_key": "你的API Key", "model": "ark-code-latest",
               "timeout": 30, "vision": true, "output_spec": "" },
  "inkboard":{ "enabled": true, "host": "127.0.0.1", "port": 8765, "token": "" },
  "wecom":   { "enabled": true, "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." }
}
```

| 字段 | 说明 |
|---|---|
| `appid` / `secret` | 第 1 步拿到的机器人凭据 |
| `allowed_openids` | 先留空 `[]`；首次转发后到 `homework_bot.log` 找到打印的 openid 填回，锁定仅家人可用 |
| `reply_enabled` | 机器人是否回复「已记录」等提示 |
| `media_dir` | 作业图片存档目录，留空用默认 `data/media` |
| `llm` | 大模型格式化作业，详见下文「大模型解析」；`output_spec` 可追加自定义输出规范（留空用内置规范）；`vision=true` 识别纯图片作业，模型不支持图片会自动退化为纯文字 |
| `inkboard` | 看板地址，本机默认 `127.0.0.1:8765`；`token` 对应看板「设置」里的 webhook token（未设留空） |
| `wecom` | 是否把作业推到企业微信群；`webhook` 填群机器人地址 |

`config.json` 含密钥，**不要提交到公开仓库**（已被 `.gitignore` 忽略）。

### 3. 装依赖（已装可跳过）

```bat
python -m venv C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot
C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. 先做离线自测（不需要 appid）

```bat
C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot\Scripts\python.exe selftest.py --send-wecom
```

会验证消息解析、去重键、截止日期猜测，并真的往看板「家庭作业」推一条作业、往企微发一条文本。
自测产生的看板作业需到管理端手动删掉。

### 5. 启动

双击 `start.bat`。窗口里出现「机器人已上线，等待转发消息…」即成功。

---

## 开机自启（可选）

用 PowerShell（管理员）执行一次，之后每次登录自动后台运行（`$root` 换成 inkbot 仓库根目录）：

```powershell
$pyw = "C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot\Scripts\pythonw.exe"
$root = "D:\workbuddy\家庭日程管理\inkbot"
$a = New-ScheduledTaskAction -Execute $pyw -Argument "homework_bot.py" -WorkingDirectory "$root\qq-homework-bot"
$t = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "QQHomeworkBot" -Action $a -Trigger $t `
  -Description "QQ作业收集机器人" -Force
```

常用操作：`Get-ScheduledTask QQHomeworkBot`、`Stop-ScheduledTask -TaskName QQHomeworkBot`。

---

## 日常使用

1. 手机 QQ 打开班级群，长按老师的作业消息
2. 转发 → 选你创建的那个机器人 → 发送
3. 机器人秒回「已记录，文本 X 字，图片 Y 张，已推送到家庭看板和企微」
4. 家庭看板「家庭作业」组件出现作业；企微收到文字 + 作业原图

多条消息可以用**合并转发**一次发过来，程序会把聊天记录里的文本和图片全部拆出来。

---

## 大模型解析（智能整理作业）

机器人支持在转发后调用一个大模型，把老师口语化的作业消息**理解并结构化**再写入「家庭作业」组件：

- 自动拆分多条任务（如「抄写单词 + 背诵课文 + 做练习册」→ 3 条独立作业）
- 识别科目、布置老师、截止日期（按当天推算，已过期就填实际日期）
- 生成给孩子看的简洁总览，企微里一并推送
- 开启 `vision` 后，纯图片作业（黑板照 / 卷子）会把图发给支持视觉的模型识别

配置（`config.json` 的 `llm` 段）：

```json
"llm": {
  "enabled": true,
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "你的火山方舟 API Key",
  "model": "ark-code-latest",
  "timeout": 30,
  "vision": true
}
```

接口为 **OpenAI 兼容**格式，换 DeepSeek / OpenAI / 通义千问 等只需改 `base_url` / `model` / `api_key`。
**未填 `api_key` 或调用失败会自动回退到规则解析**（原有关键词猜日期 + 原文截断），不会丢失消息。

> 火山方舟的控制台里 `base_url` 可能是 `.../api/v3`，请以你控制台显示为准；
> 若所用模型不支持图片（如纯文本模型），`vision=true` 时会自动退化为纯文字重试，不影响使用。

---

## 目录结构

```
qq-homework-bot/
├── homework_bot.py     主程序
├── selftest.py         离线自测（无需 appid）
├── config.json         你的配置（含密钥，勿外传）
├── config.example.json 配置模板
├── start.bat           启动
├── homework_bot.log    运行日志
└── data/
    ├── homework_raw.jsonl  每条转发消息的原始存档
    ├── seen.json           去重记录（保留 30 天）
    └── media/              下载下来的作业图片
```

---

## 已处理的两个坑

1. **botpy 会丢字段**：官方 SDK 的 `C2CMessage` 只保留 `content`/`attachments`，
   丢掉了 `message_type`、`msg_elements`（合并转发的正文在这里）和 `message_scene.ext`
   （官方要求用来去重的 `msg_idx` 在这里）。所以代码里接管了
   `ConnectionState.parse_c2c_message_create`，直接拿原始 payload。
   注意补丁必须在 `ConnectionState` 实例化之前打——它在 `__init__` 里就用
   `inspect.getmembers` 把 `parse_*` 方法快照进 `self.parsers` 了。

2. **出站 HTTP 一律用 curl**：本机环境 `urllib` 发的请求体会被代理改写。
   访问本地看板必须显式 `--noproxy 127.0.0.1,localhost`，否则会被环境代理拦掉；
   访问企微和下载图片则走环境代理。

---

## 已知限制

- 需要**手动**转发，不是全自动（这是方案 C 的取舍，换来了图片可用 + 零风险）
- 图片超过 2MB 企微拒收，此时只发文字提示，原图仍在 `data/media/`
- 截止日期：开启大模型后由模型推算；未开启或模型不可用时回退到关键词猜（今天/明天/后天/周X/X月X日），猜不到就留空——宁可不填也不瞎填
- 若哪天 QQ 调整了消息结构导致解析不出内容，日志会打 `WARNING 这条消息既没有文本也没有附件`，此时看 `data/homework_raw.jsonl` 里的原始记录调整解析逻辑即可
