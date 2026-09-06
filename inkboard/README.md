# InkBoard · 墨水屏家庭信息台

一台给安卓墨水屏 / 相框 / 平板常驻显示的信息台服务。零依赖、纯本地局域网运行，手机和电脑可以随时改内容，墨水屏自动跟着更新。

## 快速开始

**启动**（双击 `start.bat`，或命令行）：

```bash
python server.py --port 8765
```

启动后会打印局域网地址：

```
显示端(墨水屏打开) : http://192.168.3.142:8765/
管理端(手机/电脑)   : http://192.168.3.142:8765/admin
```

| 页面 | 用途 | 打开设备 |
|---|---|---|
| `/` | 显示端，常驻不操作，自动刷新 | 墨水屏 |
| `/admin` | 管理端，改组件 / 待办 / 留言 / 日程 | 手机、平板、电脑 |

### 管理端响应式断点

管理端一套代码适配三档屏幕，由 CSS 断点自动切换（尺寸/字号全部走 `:root` 令牌，改一处即全局生效）：

| 屏幕 | 断点 | 布局 | 基准字号 | 控件高度 |
|---|---|---|---|---|
| 手机（< 720px） | 默认 | 底部 Tab、单列堆叠 | 16px | 按钮 44px / 输入框 46px |
| 平板（720–1099px） | `min-width:720px` | 底部 Tab、单列、内容 900px | 16px | 同上（保持触摸友好） |
| PC / 平板横屏（≥ 1100px） | `min-width:1100px` | **左侧导航** + 卡片双列栅格 | **14px** | 按钮 / 输入框 34px |

PC 下的具体表现：

- 底部 Tab 变**左侧 212px 固定侧边栏**（图标 + 文字横排），`body` 留出 `padding-left` 占位
- 内容区加宽到 **1180px** 并居中（1920 屏左右各留约 500px，不再是一根 760px 窄条）
- 待办 / 日程 / 留言页「新增表单 + 列表」**左右并排**；设置页 4 张卡排成 **2×2**
- 组件页保持单列（顺序语义优先），但**组件列表内部双列**，高度从约 1459px 压到约 916px
- 字号 / 控件高度收紧到桌面常规值，并补了 hover 反馈（触摸端不加，避免粘滞高亮）

> 触摸端输入框字号固定 **16px**：iOS Safari 对 <16px 的输入框会在聚焦时整页放大，这条不能动；PC 断点无此问题，已收到 13.5px。

首次打开就有示例数据（含 1 条逾期待办），可在管理端「设置 → 重置为示例数据」随时恢复。

## 六个内置组件

| 组件 | 幅面 | 可调项 |
|---|---|---|
| 时间 | 半幅 | 字号、显示秒、12/24 小时制 |
| 日期 | 半幅 | 字号、显示星期、副标题（如「开学第 2 周」） |
| 待办 | 整幅 | 标题、最多显示条数、是否显示已完成 |
| 留言 | 整幅 | 标题、最多显示条数 |
| 日程 | 整幅 | 标题、最多显示条数、**显示未来几天（1-14 天，管理端可调）**、是否显示时间；每条显示「时间 + 内容 + 日期(MM-DD) + 星期」 |
| 天气 | 半幅（默认）/可调 | 城市（设置页填城市名即可，经纬度自动解析）；显示温度 / 状况 / 3 日预报 |

**天气组件**：完全免费、无需 API key——数据来自 [Open-Meteo](https://open-meteo.com/) 免费接口，由服务端后台每 10 分钟自动拉取。城市在管理端「设置 → 天气组件」填写（如 `深圳` / `北京` / `上海`），经纬度通过 Open-Meteo 地理编码自动解析并缓存到 `data/board.json`；想精确到区县可手动改 `weatherLat/weatherLon`。显示端只读 `state.weather`，墨水屏本身不联网、不受 CORS 限制。

**待办排序规则**：未完成优先 → 逾期最前 → 有截止日的按日期 → 无期限的最后 → 已完成垫底。昨天没做完的自动顺延到今天，不会凭空消失。

## 扩展新组件

组件是注册表驱动的，加一个新组件**不用动任何已有代码**，只需在 `web/index.html` 的 `COMPONENTS` 里加一段：

```js
weather: {
  name: '天气',
  span: 6,                                    // 6 = 半幅，12 = 整幅（独占一行）
  defaults: { city: '深圳', size: 5 },        // 配置字段与默认值
  render: function(cfg, ctx) {                // ctx 里有 todos / messages / settings
    return '<div class="c-text" style="--fsize:calc(var(--u) * ' + cfg.size + ')">'
         + cfg.city + '</div>';
  },
  tick: function(cfg, ctx) { /* 可选：局部刷新，不改 DOM 结构 */ }
}
```

然后在 `web/admin.html` 的 `CNAME` / `CORDER` / `CFIELDS` / `CDEFAULTS` / `SPAN` 里补上对应条目，管理端就会自动生成配置表单并出现在「添加组件」下拉里。

内置的扩展示范组件：**倒计时**（距某日还有多少天）、**自定义文字**（任意一句话）。天气组件见上方，已是正式内置组件，下面是其注册机制的示意写法。

## 墨水屏调优

| 项 | 默认 | 说明 |
|---|---|---|
| 整页全刷周期 | 300 秒 | 到点 `location.reload()`，彻底清除残影 |
| 全刷前闪白 | 开 | 先白屏 420ms 再重载，减轻残影 |
| 数据检查间隔 | 30 秒 | 只有内容真的变了才重绘，无变化不动 DOM |
| 秒级局部刷新 | 1 秒 | 仅时钟用，分钟/秒没变不写 DOM |
| 黑白反色 | 关 | 深色底白字 |

其它：数字统一等宽（`tabular-nums`）避免宽度跳动、无动画无过渡、图标全内联 SVG、字体系统栈、支持 Wake Lock 保持常亮、跨天 0 点自动重排。

**时钟字号自适应**：时间组件是半幅（占 50% 宽），竖屏（如 MatePad Paper 1404px 宽）下若字号配置过大，时钟会溢出到右侧日期区。显示端内置 `fitClock()` 自动检测——当「时:分」文字超过容器宽度时自动按比例缩小字号，任何分辨率都不会溢出；固定分辨率预览（`fitMode: fixed`）下不做缩放，按预览比例走。竖屏建议字号 9–12（默认 10），带「显示秒」时建议 ≤8。

**尺寸预览**：管理端「设置 → 尺寸预览」可切到「按目标分辨率预览」，内置 800×480 / 1200×825 / 1440×1080 / 1600×1200 预设，方便在电脑上先调好版式再上屏。墨水屏实际使用时选「自适应铺满」。

## 夜间自动息屏（护眠）

墨水屏本身常亮不耗电、无背光，但 MatePad Paper 这类有背光的设备深夜常亮会晃眼。本功能让**显示端在设定时段内自动转全黑 / 暗时钟，并主动释放 Wake Lock**，把「真息屏」交给系统完成——避免网页一直持锁导致设备整夜亮着。

**原理**：网页层无法调用系统级息屏 API，只能靠「页面切黑 + 释放 wakelock」让系统按自身「休眠时间」阈值真正熄屏。配合 MatePad Paper 系统设置（设置 → 显示 → 休眠时间，建议设为 1–2 分钟）即可实现「到点自动黑屏、不刺眼」。

**配置**（管理端「设置 → 夜间自动息屏」）：

| 项 | 默认 | 说明 |
|---|---|---|
| 开关 | 关 | 总开关 |
| 开始 / 结束 | 23:00 / 07:00 | 24 小时制 HH:MM；结束可小于开始（跨午夜），如 23:00→07:00 表示「每晚 23 点到次日早 7 点」 |
| 夜间模式 | `black`（整页全黑） | `black`=整页全黑，墨水屏无反光、对睡眠零干扰（推荐）；`clock`=只保留一个压暗到 0.45 透明度的时钟，方便夜间瞄一眼时间又不刺眼 |

**行为细节**：

- 进入夜间窗口瞬间整页切黑（或只留暗时钟），并调用 `releaseWakeLock()` 释放保持常亮的锁，系统随即按休眠时间熄屏
- 离开时段（如早上 7 点）自动恢复正常显示并重新 `keepAwake()`
- 跨午夜正确判定：`inNightWindow("23:00","07:00")` 覆盖当天 23:00 至次日 07:00 整段，不在窗口内的其它时间（如白天 08:00、傍晚 20:00）均正常显示
- 夜间时段内禁止整页全刷（避免无谓闪屏 / 反白），只做轻量时间表更
- 显示端每 30 秒检查一次时间窗口，不会因机器整夜锁屏而漏判

> 想「定时亮屏」？系统级定时开关机（MatePad Paper 设置 → 辅助功能 → 定时开关机）或 EinkBro 的「定时刷新」配合上面的夜间窗口即可：夜里黑屏、白天到时自动恢复显示。

## 外部日程接入（webhook + 同步脚本）

信息台提供 `/api/push` 通用接入点，任何系统都能把待办 / 留言 / 日程推进来，配合一个 `sync_events.py` 脚本，可把企业微信日历一键同步进来：

```bash
# 生成示例 events.json（先跑一次）
python sync_events.py --demo

# 从 events.json 文件同步（dry-run 只预览不推送）
python sync_events.py --dry-run
python sync_events.py

# 从企业微信日历拉取日程并推送（需 @wecom/cli 已安装并授权）
# 不指定 --days 时，脚本自动读取信息台「家庭日程」组件的「显示未来几天」配置
#（范围 1-14，管理端可改），拉取范围与显示范围始终保持一致，不会漏远期日程。
python sync_events.py --source wecom

# 想手动指定拉取天数也可以（与显示范围解耦，1-14）
python sync_events.py --source wecom --days 14

# 回溯历史日程（额外往前 3 天，默认 0）
python sync_events.py --source wecom --include-past 3
```

> 🔗 **同步窗口自动跟随显示窗口**：同步不写死 `--days`，而是读取信息台 board.json 里「家庭日程」组件的 `withinDays`（1-14）。你在管理端把「显示未来几天」调到几，企微同步就拉几天——显示与拉取永远对齐，杜绝「企微有、信息台看不到」的落差。

推送接口（token 为空则免鉴权，可在管理端「设置」里配置）：

```
POST /api/push
{"op":"events.set", "events":[{"title":"家长会","date":"2026-09-03","start":"09:30","end":""}]}
{"op":"todo.add",  "text":"交电费", "due":"2026-09-02"}
{"op":"msg.add",   "text":"今晚加班", "author":"爸爸"}
```

白名单操作：`todo.add` / `msg.add` / `events.set` / `events.add` / `events.delete` / `events.clear`。配置了 token 后须带 `{"token":"..."}` 或请求头 `X-InkBoard-Token`。

> ✅ 企微日程链路已全线贯通：`@wecom/cli 1.2.0` 已安装到托管 node 全局（`versions\22.22.2-2`）且已授权，机器人「日程」权限已由用户重新授权生效。真实家庭日历日程已成功同步到信息台。

**自动同步已挂载**：Windows 计划任务 `InkBoardSyncEvents` 每 10 分钟直接用 `pythonw.exe sync_events.py --source wecom` 无窗口运行（不再经过 sync.bat），按管理端「显示未来几天」配置把企微对应天数内的日程同步到信息台。日志在 `inkboard/sync.log`。在企微里新建日程后，最迟 10 分钟自动出现在墨水屏上。

## 数据与备份

- 数据文件：`data/board.json`，UTF-8，可直接编辑
- 每次改动自动留一份快照到 `data/backups/`，最多保留 30 份
- 管理端首屏有「导出 JSON / 导入恢复」，建议定期导出
- 前端全部 ES5 + XMLHttpRequest，兼容老旧安卓浏览器

## API

```
GET  /api/state            读取完整状态
POST /api/update           动作分发 {"op":"todo.add","payload":{"text":"...","due":"2026-09-02"}}
POST /api/import           整体导入 {"state":{...}}
GET  /api/health           健康检查
```

可用 op：`todo.add` `todo.toggle` `todo.update` `todo.delete` `todo.clearDone` `msg.add` `msg.delete` `events.set` `events.add` `events.delete` `events.clear` `layout.set` `layout.toggle` `layout.move` `layout.config` `settings.set` `reset.demo` `state.replace`

## 自检

```bash
python smoke_test.py      # 51 项逻辑与前端静态检查，不依赖网络
```

## 常见问题

**手机连不上？** 手机和电脑要在同一个 WiFi；Windows 防火墙首次会弹窗，允许「专用网络」访问即可。

**想开机自启？** 把 `start.bat` 的快捷方式放进 `shell:startup` 文件夹。

**地址会变？** 在路由器里给电脑绑定静态 IP，或改用电脑名访问 `http://<电脑名>:8765/`。

**想在外面用手机也能改？** 已通过 cloudflared 隧道暴露到公网域名 `https://board.goodinsight.online/admin`（任意网络可开，HTTPS）。**服务端 Basic 鉴权已于 2026-09-02 移除**，公网访问的鉴权改由 **Cloudflare Access（Zero Trust）** 在边缘层接管（**已于 2026-09-02 配置生效**：未登录访问返回 302 跳转 Cloudflare 登录页，登录后正常）。墨水屏显示端与手机管理端首次打开都需先经 Cloudflare Access 登录一次。隧道配置在 `C:\Users\Kelvinlin\.cloudflared\config.yml`（ingress 规则）；改完重启「Cloudflared-wsl-web」计划任务生效。

**显示页也要远程？** 显示端 `/` 与只读接口 `/api/state` 的**只读令牌**保护已于 2026-09-02 移除（原 `inkboard/READONLY_TOKEN.txt` 不再被读取，计划任务 `InkBoardServer` 也已去掉 `--readonly-token-file` / `--admin-pass-file` 两个参数）。当前显示端与管理端统一由 **Cloudflare Access** 在边缘层保护（**已生效**：公网访问需先登录，本地 `http://192.168.3.142:8765/` 不受 Access 影响、仍直接开放）。墨水屏请直接打开 `https://board.goodinsight.online/`，首次会跳 Cloudflare 登录页，登录后正常拉取数据。

**手机浏览器手输地址跳到搜索？** 手输进的是地址栏 omnibox，常被当成搜索词（尤其输进了桌面搜索组件、串了空格、或漏写 `http://` 前缀）。最稳的办法是扫二维码（`qr_admin.png` / `qr_admin_lan.png` / `qr_display.png` / `qr_display_lan.png`）直开，或改用上面的好记域名——它同样可以扫码，且不用记 IP:端口。
