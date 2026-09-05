#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线自测：不需要 appid/secret，验证「解析 -> 去重 -> 推送」整条后半段链路。

用法：
    python selftest.py                    只跑纯逻辑 + 家庭看板推送
    python selftest.py --send-wecom       额外发一条文本到企微群（会真的收到）
    python selftest.py --send-image X.jpg 额外发一张图片到企微群
    python selftest.py --all              全跑

    python selftest.py --test-llm                真实调用大模型解析一条示例作业（需先在 config.json 填 llm.api_key）
    python selftest.py --test-llm --push         解析后顺便推一条到家庭作业组件（便于看渲染效果）
"""

import datetime
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import homework_bot as H

RESULTS = []


def check(name, cond, extra=""):
    ok = bool(cond)
    RESULTS.append(ok)
    tail = (" | " + str(extra)) if extra else ""
    print(("[PASS] " if ok else "[FAIL] ") + name + tail)
    return ok


def t_dedup():
    print("\n--- 去重键 ---")
    raw = {"id": "MSG1", "message_scene": {"source": "default",
           "ext": ["msg_idx=REFIDX_abc123==", "auth_token=zzz"]}}
    check("从 message_scene.ext 取 msg_idx", H.dedup_key(raw) == "idx:REFIDX_abc123==", H.dedup_key(raw))
    raw2 = {"id": "MSG2", "message_scene": {}}
    check("无 ext 时回退到消息 id", H.dedup_key(raw2) == "id:MSG2", H.dedup_key(raw2))


def t_parse():
    print("\n--- 消息解析 ---")
    r = {"id": "1", "content": "今天作业：抄写第3课生字", "message_type": 0}
    txt, atts = H.parse_message(r)
    check("纯文本", txt == "今天作业：抄写第3课生字" and atts == [], txt)

    r = {"id": "2", "content": " ", "message_type": 102, "msg_elements": [
        {"content": "=== 消息 1 ===\n[消息内容] 语文：背诵《春晓》\n\n=== 消息 2 ===\n[消息内容] 数学：练习册P12"}]}
    txt, atts = H.parse_message(r)
    check("转发的聊天记录(102)", "背诵《春晓》" in txt and "练习册P12" in txt, txt.replace("\n", " / ")[:60])

    r = {"id": "3", "content": "", "message_type": 0, "attachments": [
        {"content_type": "image/jpeg", "url": "https://x/y.jpg", "filename": "a.jpg"}]}
    txt, atts = H.parse_message(r)
    check("纯图片附件", txt == "" and len(atts) == 1 and atts[0]["content_type"] == "image/jpeg")

    r = {"id": "4", "content": "", "message_type": 103, "msg_elements": [
        {"content": "数学卷子", "msg_elements": [
            {"content": "第 2 题", "attachments": [{"content_type": "image/png", "url": "u"}]}]}]}
    txt, atts = H.parse_message(r)
    check("嵌套 elements 递归取附件", "数学卷子" in txt and len(atts) == 1, "%s / atts=%d" % (txt, len(atts)))


def t_due():
    print("\n--- 截止日期猜测 ---")
    today = datetime.date.today()
    tmr = (today + datetime.timedelta(days=1)).isoformat()
    check("明天", H.guess_due("明天交") == tmr, H.guess_due("明天交"))
    check("后天", H.guess_due("后天早上交") == (today + datetime.timedelta(days=2)).isoformat())
    check("今天", H.guess_due("今天完成") == today.isoformat())
    wd = H.guess_due("下周三之前交")
    check("周X 落在未来", bool(wd) and wd > today.isoformat(), wd)
    check("无时间词不瞎猜", H.guess_due("抄写生字三遍") == "", repr(H.guess_due("抄写生字三遍")))


def t_short():
    print("\n--- 文本截断 ---")
    s = H.short_text("作业" * 200, 100)
    check("截断到 100 字并以省略号结尾", len(s) == 100 and s.endswith("…"), len(s))
    check("压缩空白", H.short_text("语文\n  作业  ") == "语文 作业")


def t_llm(args):
    print("\n--- 大模型解析（真实调用）---")
    H.load_config(require_creds=False)
    llm = H.CFG.get("llm") or {}
    if not llm.get("enabled"):
        check("llm.enabled 已开启", False, "config.json 的 llm.enabled 需为 true")
        return
    if not (llm.get("api_key") or "").strip():
        check("llm.api_key 已填写", False,
              "请在 config.json 的 llm.api_key 填入火山方舟 Key 后再测")
        print("  （base_url=%s model=%s）" % (llm.get("base_url"), llm.get("model")))
        return
    check("llm 配置就绪", True, "base=%s model=%s vision=%s" %
          (llm.get("base_url"), llm.get("model"), llm.get("vision")))

    sample = ("【语文】王老师：本周五前完成：1、抄写第3课生字每个3遍；"
              "2、背诵《春晓》并默写；3、读课外书30分钟。另外下周一交手抄报。")
    print("  示例输入：%s" % sample[:50] + "…")
    parsed = H.llm_analyze(sample, None)
    if not parsed:
        check("大模型返回结构化结果", False, "调用失败或被回退，看上方日志")
        return
    check("大模型返回结构化结果", True,
          "is_homework=%s subject=%s items=%d" %
          (parsed.get("is_homework"), parsed.get("subject"), len(parsed.get("items") or [])))
    print("  模型原始结构：%s" % json.dumps(parsed, ensure_ascii=False)[:300])
    entries = H.build_homework_from_llm(parsed)
    check("拆成多条作业条目", len(entries) >= 1, "entries=%d" % len(entries))
    for i, e in enumerate(entries[:6]):
        print("    [%d] %s | 截止 %s | 老师 %s | 科目 %s" %
              (i + 1, e["text"][:30], e.get("due") or "-", e.get("teacher") or "-", e.get("subject") or "-"))

    if args.get("push"):
        print("  推送到家庭作业组件…")
        for e in entries:
            ok, info = H.push_inkboard(e["text"], e.get("due", ""),
                                      teacher=e.get("teacher", ""), subject=e.get("subject", ""))
            check("推送条目 %s" % e["text"][:16], ok, info)


def t_live(args):
    print("\n--- 真实链路 ---")
    H.load_config(require_creds=False)
    H.ensure_dirs()
    print("media 目录: %s" % (H.CFG.get("media_dir") or H.MEDIA_DIR))

    ok, info = H.push_inkboard("[自测] QQ作业机器人链路测试，可删除", "")
    check("推送待办到家庭看板 :%s" % (H.CFG.get("inkboard", {}).get("port", 8765)), ok, info)

    if args.get("send_wecom"):
        ok, info = H.wecom_send_text("[自测] QQ作业机器人链路正常，这条可忽略。")
        check("企业微信文本推送", ok, info)
    if args.get("send_image"):
        p = args["send_image"]
        if os.path.exists(p):
            ok, info = H.wecom_send_image(p)
            check("企业微信图片推送 %s" % os.path.basename(p), ok, info)
        else:
            check("企业微信图片推送（文件不存在）", False, p)


def main():
    args = {"send_wecom": "--send-wecom" in sys.argv or "--all" in sys.argv,
            "send_image": "", "push": "--push" in sys.argv}
    if "--send-image" in sys.argv:
        i = sys.argv.index("--send-image")
        if i + 1 < len(sys.argv):
            args["send_image"] = sys.argv[i + 1]

    # 只跑大模型测试时跳过推送/企微等无关用例
    if "--test-llm" in sys.argv:
        t_llm(args)
        total, passed = len(RESULTS), sum(1 for x in RESULTS if x)
        print("\n==== 大模型自测结果：%d/%d 通过 ====" % (passed, total))
        return 0 if passed == total else 1

    t_dedup()
    t_parse()
    t_due()
    t_short()
    t_live(args)

    total, passed = len(RESULTS), sum(1 for x in RESULTS if x)
    print("\n==== 自测结果：%d/%d 通过 ====" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
