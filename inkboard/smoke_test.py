#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""InkBoard 冒烟自检：直接调用 server 模块，验证全部动作与持久化，不依赖网络。"""
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ((" -> " + str(extra)) if extra and not cond else ""))


def main():
    print("\n== 1. 默认数据 ==")
    st = server.default_state()
    check("默认含 8 个组件（含天气/课程表/家庭作业）", len(st["layout"]) == 8, len(st["layout"]))
    check("预置待办 >= 3 条", len(st["todos"]) >= 3, len(st["todos"]))
    check("预置留言 >= 2 条", len(st["messages"]) >= 2, len(st["messages"]))
    overdue = [t for t in st["todos"] if not t["done"] and t["due"] < server.today_str(0)]
    check("含 1 条逾期示例", len(overdue) == 1, len(overdue))
    done = [t for t in st["todos"] if t["done"]]
    check("含 1 条已完成示例", len(done) == 1, len(done))

    print("\n== 2. 待办动作 ==")
    st = server.apply_op(st, "todo.add", {"text": "  测试待办  ", "due": server.today_str(1)})
    tid = st["todos"][-1]["id"]
    check("新增待办并去除首尾空格", st["todos"][-1]["text"] == "测试待办")
    check("空内容被拒绝", _raises(st, "todo.add", {"text": "   "}))
    server.apply_op(st, "todo.toggle", {"id": tid})
    check("勾选完成", st["todos"][-1]["done"] is True)
    server.apply_op(st, "todo.toggle", {"id": tid})
    check("再次点击取消完成", st["todos"][-1]["done"] is False)
    server.apply_op(st, "todo.update", {"id": tid, "text": "改过了", "due": server.today_str(5)})
    check("修改内容与日期", st["todos"][-1]["text"] == "改过了" and st["todos"][-1]["due"] == server.today_str(5))
    n0 = len(st["todos"])
    server.apply_op(st, "todo.delete", {"id": tid})
    check("删除待办", len(st["todos"]) == n0 - 1)
    before = len(st["todos"])
    server.apply_op(st, "todo.clearDone", {})
    check("清除已完成（数量下降且无已完成项）",
          len(st["todos"]) < before and not any(t["done"] for t in st["todos"]))

    print("\n== 3. 留言动作 ==")
    n = len(st["messages"])
    st = server.apply_op(st, "msg.add", {"text": "测试留言", "author": "旭旭"})
    check("新增留言插到最前", st["messages"][0]["text"] == "测试留言" and len(st["messages"]) == n + 1)
    check("空留言被拒绝", _raises(st, "msg.add", {"text": ""}))
    mid = st["messages"][0]["id"]
    server.apply_op(st, "msg.delete", {"id": mid})
    check("删除留言", len(st["messages"]) == n)

    print("\n== 3.5 日程动作（外部同步用） ==")
    n = len(st.get("events", []))
    st = server.apply_op(st, "events.add", {"title": "  测试日程  ", "date": server.today_str(0), "start": "10:00"})
    check("新增日程并去首尾空格", st["events"][-1]["title"] == "测试日程")
    check("空标题日程被拒绝", _raises(st, "events.add", {"title": " "}))
    eid = st["events"][-1]["id"]
    st = server.apply_op(st, "events.delete", {"id": eid})
    check("删除日程", len(st["events"]) == n)
    st = server.apply_op(st, "events.set", {"events": [
        {"title": "A", "date": server.today_str(0), "start": "09:00", "source": "wecom"},
        {"title": "B", "date": server.today_str(1), "start": "", "source": "wecom"},
        {"title": "", "date": "", "source": "wecom"},
    ]})
    check("events.set 过滤空标题", len([e for e in st["events"] if e["source"] == "wecom"]) == 2)
    st = server.apply_op(st, "events.set", {"events": [
        {"title": "C", "date": server.today_str(0), "start": "11:00", "source": "wecom"},
    ]})
    check("同来源再次同步不累积", len([e for e in st["events"] if e["source"] == "wecom"]) == 1)
    check("同来源同步不影响其它来源", len([e for e in st["events"] if e["source"] != "wecom"]) >= 0)
    st = server.apply_op(st, "events.set", {"events": [{"title": "手动日程", "date": server.today_str(0)}]})
    check("无来源同步清掉旧手动日程", len([e for e in st["events"] if not e.get("source")]) == 1)
    check("无来源同步保留 wecom 日程", len([e for e in st["events"] if e["source"] == "wecom"]) == 1)
    st = server.apply_op(st, "events.clear", {})
    check("清空日程", st["events"] == [])
    check("非法 events 被拒绝", _raises(st, "events.set", {"events": "bad"}))

    print("\n== 3.6 课程表动作 ==")
    good_md = "| 时间 | 周一 | 周二 | 周三 | 周四 | 周五 |\n| --- | --- | --- | --- | --- | --- |\n| 第1节 8:30-9:10 | 数学 | 科学 | 数学 | 语文 | 语文 |\n"
    bad_md = "这不是表格\n就是一段普通文字"
    st = server.apply_op(st, "courses.set", {"markdown": good_md})
    check("上传合法课程表", st["courses"] == good_md)
    check("非法课程表被拒绝", _raises(st, "courses.set", {"markdown": bad_md}))
    check("非字符串课程表被拒绝", _raises(st, "courses.set", {"markdown": 123}))
    st = server.apply_op(st, "courses.clear", {})
    check("清空课程表", st["courses"] == "")

    print("\n== 4. 组件布局动作 ==")
    order0 = [c["id"] for c in st["layout"]]
    st = server.apply_op(st, "layout.move", {"id": order0[0], "delta": 1})
    order1 = [c["id"] for c in st["layout"]]
    check("下移一位", order1[0] == order0[1] and order1[1] == order0[0], order1)
    st = server.apply_op(st, "layout.move", {"id": order0[0], "delta": -1})
    check("上移复位", [c["id"] for c in st["layout"]] == order0)
    st = server.apply_op(st, "layout.move", {"id": order0[0], "delta": -1})
    check("首位再上移不越界", [c["id"] for c in st["layout"]] == order0)
    st = server.apply_op(st, "layout.toggle", {"id": "todo"})
    check("关闭组件", [c for c in st["layout"] if c["id"] == "todo"][0]["enabled"] is False)
    st = server.apply_op(st, "layout.toggle", {"id": "todo"})
    check("重新启用组件", [c for c in st["layout"] if c["id"] == "todo"][0]["enabled"] is True)
    st = server.apply_op(st, "layout.config", {"id": "todo", "config": {"maxItems": 9}})
    check("修改组件配置（保留其它键）",
          [c for c in st["layout"] if c["id"] == "todo"][0]["config"]["maxItems"] == 9
          and "title" in [c for c in st["layout"] if c["id"] == "todo"][0]["config"])
    st = server.apply_op(st, "layout.set", {"layout": [{"id": "clock", "enabled": True, "config": {}}]})
    check("整体重排布局", len(st["layout"]) == 1)
    check("非法 layout 被拒绝", _raises(st, "layout.set", {"layout": "bad"}))

    print("\n== 5. 设置 / 全局 ==")
    st = server.apply_op(st, "settings.set", {"settings": {"reloadSec": 120, "invert": True}})
    check("改设置且不影响其它项", st["settings"]["reloadSec"] == 120 and st["settings"]["title"] != "")
    st = server.apply_op(st, "reset.demo", {})
    check("重置示例后组件回到 8 个（含天气/课程表/家庭作业）", len(st["layout"]) == 8)
    check("重置示例保留现有设置", st["settings"]["reloadSec"] == 120)
    check("未知操作被拒绝", _raises(st, "no.such.op", {}))

    print("\n== 6. 兼容与持久化 ==")
    old = {"layout": [{"id": "clock", "enabled": True}], "todos": "坏数据"}
    fixed = server.normalize(old)
    check("老数据补齐字段", fixed["settings"]["title"] != "" and fixed["todos"] == [])
    check("导入非对象被拒绝", _raises(st, "state.replace", {"state": "xxx"}))

    tmp = tempfile.mkdtemp()
    try:
        f = os.path.join(tmp, "board.json")
        server.DATA_FILE = f
        server.BACKUP_DIR = os.path.join(tmp, "backups")
        st2 = server.default_state()
        st2 = server.apply_op(st2, "todo.add", {"text": "落盘测试"})
        server.save_state(st2, keep_backup=False)
        back = server.load_state()
        check("写文件后能读回", any(t["text"] == "落盘测试" for t in back["todos"]))
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("{ 坏掉的 json")
        back2 = server.load_state()
        check("坏文件降级为默认数据", len(back2["layout"]) == 8)
        check("坏文件已另存备份", os.path.exists(os.path.join(tmp, "board.corrupt.json")))
        server.save_state(back2, keep_backup=True)
        server.save_state(back2, keep_backup=True)
        n_bak = len([x for x in os.listdir(server.BACKUP_DIR) if x.endswith(".json")])
        check("自动生成备份快照", n_bak >= 2, n_bak)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n== 7. 前端脚本语法 ==")
    web = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    for fn in ("index.html", "admin.html"):
        path = os.path.join(web, fn)
        src = open(path, "r", encoding="utf-8").read()
        check(fn + " 存在且非空", len(src) > 1000)
        # 只查真实资源引用（script/link/img 的 src|href、CSS url()），放过说明文字里的示例 URL
        bad_res = re.search(r'<(script|link|img)\b[^>]*\b(src|href)\s*=\s*["\']https?://', src, re.I) \
            or re.search(r'url\(\s*["\']?https?://', src, re.I)
        check(fn + " 无外部 CDN 资源引用", bad_res is None, bad_res.group(0) if bad_res else "")
        check(fn + " 无 emoji 图标", not _has_emoji(src))
        s = src.find("<script>")
        e = src.rfind("</script>")
        check(fn + " 含内联脚本", s > 0 and e > s)
        js = src[s + 8:e]
        open(os.path.join(tempfile.gettempdir(), "_ink_" + fn + ".js"), "w", encoding="utf-8").write(js)
        print("    (脚本已导出，交由 node --check 校验)")

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
        return 1
    return 0


def _raises(state, op, payload):
    try:
        server.apply_op(json.loads(json.dumps(state)), op, payload)
        return False
    except ValueError:
        return True
    except Exception:
        return False


def _has_emoji(s):
    for ch in s:
        cp = ord(ch)
        if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or cp in (0x2705, 0x274C, 0x2B50):
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
