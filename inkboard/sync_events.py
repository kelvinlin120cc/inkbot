#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
InkBoard 日程同步器 —— 把外部日历的日程推送到信息台。

零依赖（仅 Python 标准库）。支持三种数据源：

  --source file    读一个 JSON 文件（默认同目录 events.json）
  --source wecom   调用 @wecom/cli 拉取企业微信日历（需已安装并登录）
  --source demo    内置示例数据，用于验证链路是否连通

用法：
  python sync_events.py --dry-run                  只看会推什么，不真推
  python sync_events.py --source wecom             把企微「显示未来几天」范围内的日程同步到信息台
  python sync_events.py --source wecom --days 14   显式指定拉取未来 14 天（覆盖配置）
  python sync_events.py --file my.json             从指定文件同步
  python sync_events.py --demo                     生成一份 events.json 示例文件

说明：--source wecom 不指定 --days 时，会自动读取信息台 board.json 里
「家庭日程」组件的「显示未来几天」配置（1-14），使拉取范围与显示范围一致。

Windows 计划任务里每 10~30 分钟跑一次即可，日程变更后最迟 30 秒出现在墨水屏上。

计划任务（InkBoardSyncEvents）直接调用 python.exe 而非经过 .bat，避免每 10 分钟
弹出 cmd.exe 窗口；运行日志由本脚本自己追加到同目录 sync.log。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "events.json")
BOARD_PATH = os.path.join(HERE, "data", "board.json")

# 日志文件：原先由 sync.bat 负责追加（invoked / exit 两行 + 脚本输出 2>&1 重定向）。
# 计划任务改为直接调用 python.exe 后（避免 cmd.exe 弹窗），改由本脚本自己写，行为保持一致。
LOG_PATH = os.path.join(HERE, "sync.log")
LOG_MAX_BYTES = 1024 * 1024   # 超过 1MB 时裁剪，防止每 10 分钟追加导致日志无限增长
LOG_KEEP_LINES = 400          # 裁剪后保留的最后行数

# wecom-cli 的候选安装位置（按优先级探测：托管 node 全局 bin 优先）
NODE_DIRS = [
    r"C:\Users\Kelvinlin\.workbuddy\binaries\node\versions\22.22.2-2",
    r"C:\Users\Kelvinlin\.workbuddy\binaries\node\versions\22.22.2",
    r"C:\Program Files\nodejs",
]
WECOM_JS_TAIL = os.path.join("node_modules", "@wecom", "cli", "bin", "wecom.js")


def today(offset=0):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------
def from_file(path):
    if not os.path.exists(path):
        raise RuntimeError("找不到数据文件：%s（可先用 --demo 生成示例）" % path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("events") or []
    if not isinstance(data, list):
        raise RuntimeError("文件内容应是一个数组（或含 events 字段的对象）")
    return [dict(e, source=e.get("source") or "file") for e in data if isinstance(e, dict)]


def from_demo():
    return [
        {"title": "旭旭 Python 课", "date": today(0), "start": "19:00", "end": "20:30", "source": "demo"},
        {"title": "家长会", "date": today(1), "start": "09:30", "end": "", "source": "demo"},
        {"title": "全家出游", "date": today(3), "start": "", "end": "", "source": "demo"},
    ]


def find_wecom():
    """探测 @wecom/cli 的 node.exe 与 wecom.js 路径，找不到返回 (None, None)。

    优先用全局 bin（wecom-cli.cmd），这里直接定位其 node + wecom.js，避免依赖 PATH。
    """
    for base in NODE_DIRS:
        node = os.path.join(base, "node.exe")
        if not os.path.exists(node):
            continue
        js = os.path.join(base, WECOM_JS_TAIL)
        if os.path.exists(js):
            return node, js
    return None, None


def from_wecom(days, include_past=0):
    node, js = find_wecom()
    if not js:
        raise RuntimeError(
            "未找到 @wecom/cli（wecom.js）。\n"
            "  请先安装：npm i -g @wecom/cli，并按 wecom-unified skill 完成授权。"
        )
    begin = (datetime.datetime.now() - datetime.timedelta(days=include_past)).strftime("%Y-%m-%d 00:00:00")
    end = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime("%Y-%m-%d 23:59:59")
    payload = json.dumps({"begin_time": begin, "end_time": end}, ensure_ascii=False)
    # CREATE_NO_WINDOW：本进程由 pythonw.exe（无控制台）拉起，而 node.exe 是控制台程序。
    # 若不显式加此标志，Windows 每次都会为 node 新建一个控制台窗口 → 每 10 分钟闪一次黑窗。
    # capture_output 只重定向输出句柄，不能阻止窗口创建，必须用 creationflags。
    proc = subprocess.run(
        [node, js, "calendar", "schedules", "list", "--json", payload],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("wecom-cli 无输出，stderr=%s" % (proc.stderr or "")[:200])
    try:
        data = json.loads(out)
    except Exception:
        s, e = out.find("{"), out.rfind("}")
        if s == -1 or e == -1:
            raise RuntimeError("wecom-cli 输出非 JSON：%s" % out[:200])
        data = json.loads(out[s:e + 1])
    if isinstance(data, dict) and data.get("errcode"):
        # 850003 = 日程使用权限过期，需用户在企业微信重新授权机器人
        hint = ""
        if data.get("errcode") == 850003:
            hint = "\n  日程权限已过期，请在企业微信「工作台-智能机器人」中重新授权该机器人的「日程」权限。"
        raise RuntimeError("wecom-cli 返回错误：errcode=%s %s%s" % (data.get("errcode"), data.get("errmsg"), hint))

    events = []
    for s in (data.get("schedule_list") or []):
        bt = s.get("begin_time") or ""
        en = s.get("end_time") or ""
        date, start = bt[:10], bt[11:16]
        end = en[11:16]
        is_all_day = bool(s.get("is_all_day"))
        # 企微全天日程（is_all_day=true 或 00:00-23:59）—— 不显示具体时间点
        if is_all_day or (start == "00:00" and end in ("23:59", "00:00", "")):
            start, end = "", ""
        # 是否为「周期性日程的某次修改例外」：企微修改单个周期 occurrence 时会另建
        # 一条带 repeat_rule.exception 的日程来覆盖原日程在该日期的时间，schedule_list
        # 会同时返回「原日程」与「这条例外」，导致同一时段在显示端叠出两条 → 需去重。
        rr = s.get("repeat_rule") or {}
        is_exception = isinstance(rr.get("exception"), list) and len(rr.get("exception")) > 0
        events.append({
            "title": s.get("subject") or "(无标题)",
            "date": date, "start": start, "end": end,
            "source": "wecom", "_exc": is_exception,
        })

    # 去重：同一 (标题, 日期, 开始时间) 出现多条，基本就是「原周期日程 + 修改例外」。
    # 只保留一条 —— 例外(override)优先，其次结束时间更晚者（即被改长/改后的版本），
    # 再同级保留后出现者。这样编辑后的新时间会覆盖旧时间，不会两头都显示。
    seen = {}
    order = 0
    for e in events:
        k = (e["title"], e["date"], e["start"])
        prev = seen.get(k)
        win = (e["_exc"], e["end"], order) > (prev["_exc"], prev["end"], prev["_order"]) \
            if prev is not None else True
        if prev is None or win:
            e["_order"] = order
            seen[k] = e
        order += 1
    out = []
    for e in seen.values():
        e.pop("_exc", None)
        e.pop("_order", None)
        out.append(e)
    return out


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trim_log(path, max_bytes, keep_lines):
    """日志超过 max_bytes 时只保留最后 keep_lines 行。裁剪失败不影响主流程。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.write("[%s] --- 日志已裁剪，仅保留最后 %d 行 ---\n" % (_now(), keep_lines))
            f.writelines(lines[-keep_lines:])
    except Exception:
        pass


class _Tee(object):
    """把 stdout / stderr 同时写到终端与 sync.log（对齐原 .bat 的 >> sync.log 2>&1）。"""

    def __init__(self, stream, fobj):
        self.stream = stream
        self.fobj = fobj

    def write(self, data):
        for t in (self.stream, self.fobj):
            try:
                t.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for t in (self.stream, self.fobj):
            try:
                t.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def reconfigure(self, *args, **kwargs):
        pass


def _run_with_log():
    """包一层：写 invoked / exit 两行时间戳，并把 main() 的输出一并落盘。"""
    _trim_log(LOG_PATH, LOG_MAX_BYTES, LOG_KEEP_LINES)
    try:
        lf = open(LOG_PATH, "a", encoding="utf-8")
    except Exception:
        return main()

    lf.write("[%s] sync_events.py invoked\n" % _now())
    saved = (sys.stdout, sys.stderr)
    sys.stdout = _Tee(saved[0], lf)
    sys.stderr = _Tee(saved[1], lf)
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except Exception as e:
        print("[ERROR] 未捕获异常：%r" % (e,))
        code = 1
    finally:
        sys.stdout, sys.stderr = saved
        lf.write("[%s] sync_events.py exit=%s\n" % (_now(), code))
        lf.close()
    return code


# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------
def push(events, host, port, token, dry_run=False):
    if dry_run:
        print("[DRY-RUN] 将推送 %d 条日程到 http://%s:%d/api/push" % (len(events), host, port))
        for e in events:
            print("   - %s %s %s  %s" % (e.get("date", ""), e.get("start") or "全天",
                                         (e.get("end") and "-" + e["end"]) or "", e.get("title", "")))
        return {"ok": True, "dry_run": True}

    body = {"op": "events.set", "events": events}
    if token:
        body["token"] = token
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://%s:%d/api/push" % (host, port),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def write_demo_file(path):
    sample = [
        {"title": "旭旭 Python 课", "date": today(0), "start": "19:00", "end": "20:30"},
        {"title": "家长会", "date": today(1), "start": "09:30", "end": ""},
        {"title": "全家出游", "date": today(3), "start": "", "end": ""},
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    print("已生成示例文件：%s" % path)


def board_within_days():
    """读取信息台 board.json 里日程组件的「显示未来几天」配置，作为企微同步窗口。

    该值由管理端设定、范围 1-14；读不到或越界时兜底为 14，保证不漏远期日程。
    """
    try:
        with open(BOARD_PATH, encoding="utf-8") as f:
            st = json.load(f)
        for c in st.get("layout", []):
            if isinstance(c, dict) and c.get("id") == "schedule":
                v = (c.get("config") or {}).get("withinDays")
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = None
                if isinstance(v, int) and 1 <= v <= 14:
                    return v
    except Exception:
        pass
    return 14


def main():
    ap = argparse.ArgumentParser(description="把外部日程同步到 InkBoard 信息台")
    ap.add_argument("--source", default="file", choices=["file", "wecom", "demo"], help="数据源")
    ap.add_argument("--file", default=DEFAULT_FILE, help="source=file 时的文件路径")
    ap.add_argument("--days", type=int, default=None,
                     help="source=wecom 时拉取未来几天（缺省则读取信息台「显示未来几天」配置，范围 1-14）")
    ap.add_argument("--include-past", type=int, default=0, help="source=wecom 时额外往前回溯几天（默认 0，只看今天起）")
    ap.add_argument("--host", default="127.0.0.1", help="信息台地址")
    ap.add_argument("--port", type=int, default=8765, help="信息台端口")
    ap.add_argument("--token", default="", help="信息台 webhook token（未设置则留空）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要推送的内容，不真正推送")
    ap.add_argument("--demo", action="store_true", help="生成一份示例 events.json 后退出")
    args = ap.parse_args()

    if args.demo:
        write_demo_file(args.file)
        return 0

    try:
        if args.source == "file":
            events = from_file(args.file)
        elif args.source == "demo":
            events = from_demo()
        else:
            # 未显式指定 --days 时，按管理端「显示未来几天」配置同步，保证拉取范围与显示范围一致
            days = args.days if args.days is not None else board_within_days()
            if args.days is None:
                print("[INFO] 未指定 --days，按信息台「显示未来几天」配置拉取未来 %d 天" % days)
            events = from_wecom(days, args.include_past)
    except Exception as e:
        print("[ERROR] 读取数据源失败：%s" % e)
        return 1

    if not events:
        print("[WARN] 数据源没有返回任何日程，已跳过推送（不会清空已有日程）")
        return 0

    try:
        res = push(events, args.host, args.port, args.token, args.dry_run)
    except Exception as e:
        print("[ERROR] 推送失败：%s" % e)
        print("        请确认信息台服务已启动（start.bat），且端口 %d 可访问" % args.port)
        return 1

    if res.get("ok"):
        n = len((res.get("state") or {}).get("events", events))
        print("[OK] 已同步 %d 条日程，信息台现有 %d 条" % (len(events), n))
        return 0
    print("[ERROR] 服务端返回：%s" % res.get("error", res))
    return 1


if __name__ == "__main__":
    sys.exit(_run_with_log())
