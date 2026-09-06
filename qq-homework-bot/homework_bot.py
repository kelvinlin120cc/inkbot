#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QQ 作业收集机器人（方案 C：手动转发）

用法：
    在手机 QQ 里长按老师发的作业消息 -> 转发 -> 选这个机器人 -> 作业自动进家庭信息看板 + 企业微信。

为什么这么做：
    QQ 群里要收「全量消息」必须由群主在群设置里授权，个人拿不到。
    但「单聊」不需要任何群权限：用户主动给机器人发消息就触发 C2C_MESSAGE_CREATE，
    用的是公域 Intent（public_messages），个人开发者即可，也不需要企业认证。

两个必须知道的实现细节：
    1. botpy 的 C2CMessage 只保留了 content/attachments 等少数字段，
       丢掉了 message_type / msg_elements / message_scene。
       而「合并转发的聊天记录」内容在 msg_elements 里、
       官方要求用来去重的 msg_idx 在 message_scene.ext 里。
       所以这里 monkey-patch 掉 parse_c2c_message_create，直接拿原始 payload。
    2. 本环境 urllib 发出的请求体会被代理改写（历史坑），
       所以所有出站 HTTP 一律用 curl；本地地址显式 --noproxy，外网走环境代理。
"""

import asyncio
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import botpy
import botpy.connection as _botconn
from botpy.flags import Intents

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
EXAMPLE_FILE = os.path.join(HERE, "config.example.json")
DATA_DIR = os.path.join(HERE, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
RAW_FILE = os.path.join(DATA_DIR, "homework_raw.jsonl")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
LOG_FILE = os.path.join(HERE, "homework_bot.log")

# pythonw.exe（计划任务无窗口启动）下没有控制台，sys.stdout/stderr 为 None，
# 此时裸 print、第三方库输出、未捕获异常会丢失甚至报错。重定向到业务日志文件；
# log() 通过判断 stdout 是否指向本文件来避免双写。
if sys.stdout is None or sys.stderr is None:
    try:
        _bot_log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _bot_log
        if sys.stderr is None:
            sys.stderr = _bot_log
    except Exception:
        pass

# 去重记录保留天数（官方可能对同一 msg_id 重复推送，必须去重）
SEEN_KEEP_DAYS = 30

# 企微 text 消息内容上限（字节，UTF-8）
WECOM_TEXT_LIMIT = 1800

# 允许触发的 QQ 用户 openid；留空 = 允许所有人（首次调试时便于拿到自己的 openid）
ALLOWED_OPENIDS = set()

CFG = {}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _stdout_is_logfile():
    """pythonw 重定向后 sys.stdout 指向 homework_bot.log；此时 print 即写文件。"""
    name = getattr(sys.stdout, "name", None)
    return bool(name) and os.path.abspath(name) == os.path.abspath(LOG_FILE)


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _stdout_is_logfile():
        return  # 输出已重定向到本日志文件，无需再 open 写第二遍
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def ensure_dirs():
    for d in (DATA_DIR, MEDIA_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def load_config(require_creds=True):
    """require_creds=False 时允许缺 appid/secret（离线自测用）。"""
    global CFG, ALLOWED_OPENIDS
    path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else EXAMPLE_FILE
    if not os.path.exists(path):
        raise SystemExit("缺少 config.json，也找不到 config.example.json。")
    with open(path, "r", encoding="utf-8-sig") as f:
        CFG = json.load(f)
    CFG.setdefault("inkboard", {})
    CFG.setdefault("wecom", {})
    CFG.setdefault("reply_enabled", True)
    CFG.setdefault("media_dir", MEDIA_DIR)
    CFG.setdefault("llm", {})
    CFG["llm"].setdefault("enabled", False)
    CFG["llm"].setdefault("base_url", "https://ark.cn-beijing.volces.com/api/v3")
    CFG["llm"].setdefault("api_key", "")
    CFG["llm"].setdefault("model", "ark-code-latest")
    CFG["llm"].setdefault("timeout", 30)
    CFG["llm"].setdefault("max_retries", 1)
    CFG["llm"].setdefault("vision", False)
    CFG["llm"].setdefault("fallback_on_error", True)
    # 用户自定义大模型输出规范（自由文本，追加到系统提示末尾、优先级最高；留空=仅用内置规范）
    CFG["llm"].setdefault("output_spec", "")
    CFG.setdefault("api", {})
    CFG["api"].setdefault("enabled", True)
    CFG["api"].setdefault("port", 8766)
    ALLOWED_OPENIDS = {str(x).strip() for x in (CFG.get("allowed_openids") or []) if str(x).strip()}
    if require_creds and (not CFG.get("appid") or not CFG.get("secret")):
        raise SystemExit("config.json 里的 appid / secret 还没填。")
    return CFG


# --------------------------------------------------------------------------
# 出站 HTTP：一律走 curl
# --------------------------------------------------------------------------
# pythonw（无控制台）下调控制台程序 curl 时，需 CREATE_NO_WINDOW，否则每次请求闪黑窗。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def curl_run(args, timeout=30):
    """执行 curl，返回 (stdout, stderr, returncode)。"""
    try:
        proc = subprocess.run(
            ["curl", "-s", "-S"] + args,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return (proc.stdout or ""), (proc.stderr or ""), proc.returncode
    except Exception as e:
        return "", "curl 调用异常: %s" % e, -1


def post_json(url, payload, noproxy="", timeout=20, extra_headers=None):
    """POST JSON。数据经 stdin 管道传入（curl --data-binary @-），
    不写临时文件，从而绕开中文路径问题与安全删除守卫（原实现把临时文件
    写到用户主目录并在 finally 删除，长期运行累积删除次数触发守卫
    强制 SystemExit 杀掉整个进程）。
    noproxy 非空时对指定 host 绕过代理（本地服务必须）。
    extra_headers: 形如 ["-H", "X: y"] 的额外请求头（大模型鉴权用）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    args = ["-X", "POST", url, "-H", "Content-Type: application/json"]
    if extra_headers:
        args += list(extra_headers)
    if noproxy:
        args += ["--noproxy", noproxy]
    args += ["--data-binary", "@-"]
    try:
        proc = subprocess.run(
            ["curl", "-s", "-S"] + args,
            input=raw,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode, out, err
    except Exception as e:
        return -1, "", "curl 调用异常: %s" % e


def download_file(url, dest, timeout=60):
    """下载附件（图片等），返回是否成功。走环境代理访问外网。"""
    args = ["-L", "-o", dest, url]
    out, err, rc = curl_run(args, timeout=timeout)
    ok = rc == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0
    if not ok:
        log("下载失败 rc=%s url=%s err=%s" % (rc, str(url)[:80], (err or "")[:120]))
    return ok


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------
def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=SEEN_KEEP_DAYS)).timestamp()
    pruned = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > cutoff}
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False)
    except Exception as e:
        log("WARNING 去重状态保存失败: %s" % e)


def dedup_key(raw):
    """官方要求用 message_scene.ext 里的 msg_idx 去重（同一 msg_id 可能重复推送）。"""
    ext = ((raw.get("message_scene") or {}).get("ext")) or []
    for item in ext:
        if isinstance(item, str) and item.startswith("msg_idx="):
            return "idx:" + item.split("=", 1)[1]
    return "id:" + str(raw.get("id") or "")


# --------------------------------------------------------------------------
# 消息解析
# --------------------------------------------------------------------------
def collect_elements(elements, texts, atts, depth=0):
    """递归收集 msg_elements 里的文本与附件（转发聊天记录 / 引用消息 用）。"""
    if depth > 3:
        return
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        c = (el.get("content") or "").strip()
        if c:
            texts.append(c)
        for a in el.get("attachments") or []:
            if isinstance(a, dict):
                atts.append(a)
        if el.get("msg_elements"):
            collect_elements(el["msg_elements"], texts, atts, depth + 1)


def parse_message(raw):
    """把 C2C 原始 payload 拆成 (文本, 附件列表)。"""
    texts, atts = [], []
    content = (raw.get("content") or "").strip()
    if content:
        texts.append(content)

    collect_elements(raw.get("msg_elements"), texts, atts)
    for a in raw.get("attachments") or []:
        if isinstance(a, dict):
            atts.append(a)

    # 去重（转发聊天记录时 content 可能与 element 内容重复）
    uniq, seen = [], set()
    for t in texts:
        k = t.strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    return "\n".join(uniq).strip(), atts


WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def guess_due(text):
    """从文本里猜截止日期，猜不到返回空串（避免瞎填误导孩子）。"""
    if not text:
        return ""
    today = datetime.date.today()
    t = text[:200]

    if re.search(r"明天|明日", t):
        return (today + datetime.timedelta(days=1)).isoformat()
    if re.search(r"后天", t):
        return (today + datetime.timedelta(days=2)).isoformat()
    if re.search(r"今天|今日", t):
        return today.isoformat()

    m = re.search(r"(?:周|星期)([一二三四五六日天])", t)
    if m:
        target = WEEKDAY_CN.get(m.group(1))
        if target is not None:
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return (today + datetime.timedelta(days=delta)).isoformat()

    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", t)
    if m:
        try:
            d = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
            # 作业场景：日期在今年已过去就当作「逾期」（如 9月2日作业在9月3日转发），
            # 不再滚到下一年，避免出现 2027 这种离谱的截止日。
            return d.isoformat()
        except ValueError:
            return ""
    return ""


def short_text(s, n=100):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------
# 大模型解析（OpenAI 兼容接口：火山方舟 / DeepSeek / OpenAI / 通义 等）
# --------------------------------------------------------------------------
LLM_SYSTEM_PROMPT = """你是一个家庭作业整理助手，负责把家长转发来的老师作业消息，整理成结构化、便于孩子查看的作业清单。

# 输入
- 可能是纯文字，也可能是一张或多张图片（黑板照、卷子、练习册照片）。
- 今天是 {today}（{weekday}）。

# 任务
从中提取作业信息，并仅输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字）。字段：
- is_homework: 布尔。true=这是作业/任务；false=闲聊或非作业内容。
- subject: 字符串，整条消息的主科目（如 语文/数学/英语/科学），仅作兜底；单条任务有自己的科目时以单条为准。无法识别填 ""。
- teacher: 字符串，布置作业的老师姓名，没有填 ""。
- due: 字符串，整体兜底截止日期 YYYY-MM-DD（按今天推算；若已过期就填实际日期，不要加一年）；没有截止日填 ""。
- type: 字符串，作业类型（书面/背诵/默写/阅读/实践/其他），没有填 ""。
- items: 数组，每条独立任务，元素 {{"text": 字符串(简洁、给孩子看的一句话任务), "subject": 字符串(该条科目, 如 数学, 优先填最具体的科目; 没有填 ""), "due": 字符串(可空, 该条单独截止日 YYYY-MM-DD)}}。至少 1 条。
- summary: 字符串，给孩子/家长看的简洁总览，如 "英语：抄写 Unit3 单词 + 背诵课文，周五前交"。

# 要求
- 多条任务务必拆成多个 items，不要合并成一段。
- 每条 items 尽量标上自己的 subject（如一份消息既有语文又有数学，要分别标注），便于按科目归类展示；标不出时留 ""，由整体 subject 兜底。
- text 不要带项目符号前缀，保持简短自然。
- 若图片中文字看不清，is_homework 仍按文字/上下文尽力判断，看不清的部分在 text 中注明。
"""


def _with_output_spec(system_prompt):
    """把 config.llm.output_spec（用户自定义输出规范）追加到系统提示末尾，优先级最高。"""
    spec = ((CFG.get("llm") or {}).get("output_spec") or "").strip()
    if spec:
        return system_prompt.rstrip() + "\n\n# 用户自定义输出规范（必须优先遵循）\n" + spec + "\n"
    return system_prompt


def _build_llm_messages(text, image_paths):
    """按 OpenAI 多模态消息格式组装（text + 可选 base64 图片）。"""
    today = datetime.date.today()
    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
    system = _with_output_spec(
        LLM_SYSTEM_PROMPT.format(today=today.isoformat(), weekday="周" + weekday_cn))
    content = []
    if text:
        content.append({"type": "text", "text": "作业消息原文：\n" + text})
    for p in (image_paths or [])[:4]:
        try:
            if os.path.getsize(p) > 6 * 1024 * 1024:
                continue
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            ext = os.path.splitext(p)[1].lower().lstrip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                    "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/%s;base64,%s" % (mime, b64)}})
        except Exception as e:
            log("WARNING 读图失败，跳过视觉: %s" % e)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _extract_json(text_out):
    """从模型输出里尽量抠出 JSON 对象。"""
    if not text_out:
        return None
    t = text_out.strip()
    # 去 ```json ... ``` 围栏
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def llm_analyze(text, image_paths=None):
    """调用大模型把作业消息解析为结构化 dict；任何失败返回 None（调用方回退）。

    返回示例：
        {"is_homework": true, "subject": "英语", "teacher": "王老师",
         "due": "2026-09-05", "type": "书面",
         "items": [{"text": "...", "due": ""}], "summary": "..."}
    """
    llm = CFG.get("llm") or {}
    if not llm.get("enabled"):
        return None
    key = (llm.get("api_key") or "").strip()
    if not key:
        log("LLM 未配置 api_key，跳过大模型解析（回退规则解析）")
        return None
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        return None
    model = llm.get("model") or "ark-code-latest"
    timeout = int(llm.get("timeout") or 30)
    vision = bool(llm.get("vision")) and bool(image_paths)

    messages = _build_llm_messages(text, image_paths if vision else None)
    if not messages[-1]["content"]:
        # 既没文字也没图片，无需调用
        return None

    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    url = base + "/chat/completions"
    headers = ["-H", "Authorization: Bearer %s" % key]

    rc, out, err = post_json(url, body, extra_headers=headers, timeout=timeout)
    # 带图调用失败 → 退化为纯文字重试一次（避免模型不支持多模态时整条丢失）
    if rc != 0 and vision:
        log("LLM 多模态调用失败（rc=%s），改用纯文字重试" % rc)
        messages = _build_llm_messages(text, None)
        rc, out, err = post_json(url, body, extra_headers=headers, timeout=timeout)

    if rc != 0:
        log("LLM 调用失败 rc=%s err=%s" % (rc, (err or "")[:160]))
        return None
    try:
        resp = json.loads(out)
    except Exception as e:
        log("LLM 响应解析失败: %s" % e)
        return None
    if isinstance(resp, dict) and resp.get("error"):
        em = resp.get("error") or {}
        log("LLM 接口错误（如鉴权失效）: %s" % str(em.get("message") or em.get("code") or "unknown")[:160])
        return None
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not content:
        log("LLM 返回为空（无 content）")
        return None
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        log("LLM 未返回可用 JSON")
        return None
    log("LLM 解析完成 is_homework=%s subject=%s items=%d" % (
        parsed.get("is_homework"), parsed.get("subject"), len(parsed.get("items") or [])))
    return parsed


def build_homework_from_llm(parsed):
    """把 LLM 结构化结果转成待推送的作业条目列表（每条含 text/due/teacher/subject）。

    每条 item 优先取自己的 subject，缺失时回退到整条消息的顶层 subject；
    这样一份跨科目消息（既含语文又含数学）能被正确拆到不同科目分组下。
    """
    subject = (parsed.get("subject") or "").strip()[:10]
    teacher = (parsed.get("teacher") or "").strip()[:16]
    top_due = (parsed.get("due") or "").strip()
    items = parsed.get("items") or []
    entries = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = (it.get("text") or "").strip()
        if not t:
            continue
        d = (it.get("due") or top_due or "").strip()
        subj = (it.get("subject") or subject or "").strip()[:10]
        entries.append({
            "text": t[:110], "due": d,
            "teacher": teacher, "subject": subj,
        })
    if not entries:
        summary = (parsed.get("summary") or "").strip()
        if summary:
            entries.append({"text": summary[:110], "due": top_due,
                            "teacher": teacher, "subject": subject})
    return entries


# --------------------------------------------------------------------------
# 本地 HTTP 格式化 API（供信息板管理端调用，复用上面的 LLM 解析能力）
# --------------------------------------------------------------------------
FORMAT_SYSTEM_PROMPT = """你是一个家庭作业整理助手。用户会给你一段「作业原文 + 格式化要求」，请严格按照用户要求把作业整理成结构化清单，仅输出一个 JSON 对象（不要 markdown 代码块、不要任何解释文字）。字段：
- items: 数组，每条独立任务，元素 {{"text": 字符串(按用户要求的格式、给孩子看的一句话任务), "subject": 字符串(科目, 如 数学, 没有填 ""), "due": 字符串(可空, 该条截止日 YYYY-MM-DD), "teacher": 字符串(布置老师, 没有填 "")}}。至少 1 条。
今天是 {today}（{weekday}）。截止日按今天推算；若已过期就填实际日期，不要加一年。
多条任务务必拆成多个 items，不要合并成一段。text 不要带项目符号前缀。
"""


def _build_format_messages(text):
    today = datetime.date.today()
    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
    system = _with_output_spec(
        FORMAT_SYSTEM_PROMPT.format(today=today.isoformat(), weekday="周" + weekday_cn))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def llm_format(text):
    """调用大模型做「自由格式作业整理」。返回 (parsed_dict_or_None, error_msg_or_None)。

    error_msg 仅在失败时非空，用于让调用方（管理端）给出准确的回退原因，
    而不是把「鉴权失败」误报成「未启用」。
    """
    llm = CFG.get("llm") or {}
    if not llm.get("enabled"):
        return None, "大模型未启用（config llm.enabled=false）"
    key = (llm.get("api_key") or "").strip()
    if not key:
        return None, "大模型未配置 api_key"
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        return None, "大模型未配置 base_url"
    model = llm.get("model") or "ark-code-latest"
    timeout = int(llm.get("timeout") or 30)
    body = {
        "model": model,
        "messages": _build_format_messages(text),
        "temperature": 0.2,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    url = base + "/chat/completions"
    headers = ["-H", "Authorization: Bearer %s" % key]
    rc, out, err = post_json(url, body, extra_headers=headers, timeout=timeout)
    if rc != 0:
        return None, "大模型调用失败（网络/HTTP rc=%s）" % rc
    try:
        resp = json.loads(out)
    except Exception as e:
        return None, "大模型响应非 JSON：%s" % str(e)[:120]
    # 火山方舟等会在 HTTP 200(rc=0) 时返回 {"error":{...}}（如 401 鉴权失败），必须识别，
    # 否则会被当成「成功但 content 为空」而静默回退、还误报「未启用」。
    if isinstance(resp, dict) and resp.get("error"):
        em = resp.get("error") or {}
        return None, "大模型鉴权/接口错误：%s" % str(em.get("message") or em.get("code") or "unknown")[:140]
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not content:
        return None, "大模型返回为空（无 content）"
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return None, "大模型返回无法解析为 JSON 对象"
    return parsed, None


def _fallback_format(text):
    """LLM 不可用时的规则回退：按换行拆分，整体猜一个截止日。"""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return []
    due = guess_due(text)
    entries = []
    for l in lines[:30]:
        entries.append({"text": l[:110], "due": due, "teacher": "", "subject": ""})
    return entries


def format_homework(text):
    """入口：返回 (entries, llm_used, llm_error)。

    entries 形如 [{text, due, teacher, subject}]；llm_used 是否真用了大模型；
    llm_error 为失败时的人类可读原因（成功时为 None），供管理端展示准确回退说明。
    """
    parsed, err = llm_format(text)
    if parsed:
        entries = build_homework_from_llm(parsed)
        if entries:
            return entries, True, None
    return _fallback_format(text), False, (err or "规则回退")


def run_format_api():
    """在独立线程里起一个极简 HTTP 服务，供信息板管理端调用大模型格式化作业。仅本地 127.0.0.1。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    api = CFG.get("api") or {}
    port = int(api.get("port") or 8766)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_GET(self):
            if self.path.startswith("/api/health"):
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/format"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as e:
                self._send(400, {"ok": False, "error": "bad json: %s" % e})
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": "text 不能为空"})
                return
            try:
                entries, used, llm_err = format_homework(text)
            except Exception as e:
                log("FORMAT API 异常: %s" % e)
                self._send(500, {"ok": False, "error": str(e)})
                return
            self._send(200, {"ok": True, "llm_used": used, "llm_error": llm_err,
                            "count": len(entries), "entries": entries})

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        log("格式化 API 已启动：http://127.0.0.1:%d/api/format" % port)
        srv.serve_forever()
    except Exception as e:
        log("格式化 API 启动失败（端口 %d 可能被占用）：%s" % (port, e))


# --------------------------------------------------------------------------
# 落地：InkBoard + 企业微信
# --------------------------------------------------------------------------
def push_inkboard(text, due="", teacher="", subject=""):
    ib = CFG.get("inkboard") or {}
    if not ib.get("enabled"):
        return False, "未启用"
    host = ib.get("host") or "127.0.0.1"
    port = int(ib.get("port") or 8765)
    url = "http://%s:%d/api/push" % (host, port)
    payload = {"op": "homework.add", "text": text[:110], "source": "qq"}
    if due:
        payload["due"] = due
    if teacher:
        payload["teacher"] = teacher[:16]
    if subject:
        payload["subject"] = subject[:10]
    token = (ib.get("token") or "").strip()
    if token:
        payload["token"] = token
    # 本地地址必须绕代理，否则会被环境里的 HTTP_PROXY 拦掉
    rc, out, err = post_json(url, payload, noproxy="%s,localhost" % host, timeout=15)
    if rc == 0 and '"ok":true' in out.replace(" ", ""):
        return True, "ok"
    return False, "rc=%s out=%s err=%s" % (rc, (out or "")[:150], (err or "")[:120])


def wecom_send_text(content):
    wc = CFG.get("wecom") or {}
    if not wc.get("enabled"):
        return False, "未启用"
    webhook = (wc.get("webhook") or "").strip()
    if not webhook:
        return False, "缺 webhook"
    b = content.encode("utf-8")
    if len(b) > WECOM_TEXT_LIMIT:
        content = b[:WECOM_TEXT_LIMIT].decode("utf-8", errors="ignore") + "…（内容过长已截断）"
    payload = {"msgtype": "text", "text": {"content": content}}
    rc, out, err = post_json(webhook, payload, timeout=20)
    if rc == 0 and '"errcode":0' in out.replace(" ", ""):
        return True, "ok"
    return False, "rc=%s out=%s err=%s" % (rc, (out or "")[:150], (err or "")[:120])


def wecom_send_image(path):
    """企微机器人图片消息：base64 + md5 直发 webhook（不走 upload_media，那个会 40004）。"""
    wc = CFG.get("wecom") or {}
    if not wc.get("enabled"):
        return False, "未启用"
    webhook = (wc.get("webhook") or "").strip()
    if not webhook:
        return False, "缺 webhook"
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > 2 * 1024 * 1024:
            return False, "图片超过 2MB（%d 字节），企微拒收" % len(data)
        b64 = base64.b64encode(data).decode("ascii")
        md5 = hashlib.md5(data).hexdigest()
    except Exception as e:
        return False, "读取图片失败: %s" % e
    payload = {"msgtype": "image", "image": {"base64": b64, "md5": md5}}
    rc, out, err = post_json(webhook, payload, timeout=40)
    if rc == 0 and '"errcode":0' in out.replace(" ", ""):
        return True, "ok"
    return False, "rc=%s out=%s" % (rc, (out or "")[:150])


def append_raw(record):
    try:
        with open(RAW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log("WARNING 原始记录写入失败: %s" % e)


# --------------------------------------------------------------------------
# 消息处理
# --------------------------------------------------------------------------
def process(raw):
    """处理一条 C2C 消息（在线程池里跑，不会阻塞 websocket 事件循环）。"""
    openid = str(((raw.get("author") or {}).get("user_openid")) or "")
    username = str(((raw.get("author") or {}).get("username")) or "")
    if ALLOWED_OPENIDS and openid not in ALLOWED_OPENIDS:
        log("忽略非白名单消息 openid=%s user=%s" % (openid[:12], username))
        return

    key = dedup_key(raw)
    seen = load_seen()
    if key in seen:
        log("重复消息已跳过 key=%s" % key[:40])
        return
    seen[key] = datetime.datetime.now().timestamp()
    save_seen(seen)

    text, atts = parse_message(raw)
    ts = (raw.get("timestamp") or "")[:19].replace("T", " ")

    record = {
        "ts": ts, "openid": openid, "username": username,
        "message_type": raw.get("message_type"),
        "text": text, "attachments": [], "key": key,
    }

    # 图片/文件附件：下载到本地（url 带时效，必须立刻存下来）
    saved = []
    for i, a in enumerate(atts):
        url = a.get("url")
        if not url:
            continue
        ct = (a.get("content_type") or "").lower()
        is_img = ct.startswith("image/") or (a.get("filename") or "").lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp"))
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "gif" in ct:
            ext = ".gif"
        elif "webp" in ct:
            ext = ".webp"
        elif not is_img:
            ext = ".bin"
        name = "%s_%d%s" % (re.sub(r"\W+", "", ts) or "msg", i, ext)
        dest = os.path.join(CFG.get("media_dir") or MEDIA_DIR, name)
        if download_file(url, dest):
            saved.append({"path": dest, "content_type": ct, "filename": a.get("filename"), "is_image": is_img})
            record["attachments"].append({"path": dest, "content_type": ct, "is_image": is_img})
        else:
            record["attachments"].append({"url": str(url)[:200], "content_type": ct, "saved": False})

    append_raw(record)
    log("收到作业消息 user=%s 文本%d字 附件%d个" % (username, len(text), len(saved)))

    images = [s for s in saved if s.get("is_image")]
    image_paths = [s["path"] for s in images]

    # ---- 大模型解析（理解 + 结构化 + 格式化）----
    hw_entries = None
    llm_summary = ""
    used_llm = False
    is_hw = True  # 默认按作业处理；大模型明确判断为非作业时置 False
    if CFG.get("llm", {}).get("enabled") and (text or image_paths):
        parsed = llm_analyze(text, image_paths)
        if parsed:
            used_llm = True
            llm_summary = (parsed.get("summary") or "").strip()
            if parsed.get("is_homework") is False:
                is_hw = False
            else:
                hw_entries = build_homework_from_llm(parsed)
                if not hw_entries:
                    hw_entries = None  # 解析为空，触发规则回退

    # ---- 回退：未启用 / 调用失败 / 解析为空 → 规则解析 ----
    if hw_entries is None and is_hw:
        due = guess_due(text)
        if text:
            hw_entries = [{"text": short_text(text, 100), "due": due,
                          "teacher": username, "subject": ""}]
        elif images:
            hw_entries = [{"text": "[图片作业] 已推送到手机，共 %d 张" % len(images),
                          "due": due, "teacher": username, "subject": ""}]
    if hw_entries is None:
        hw_entries = []

    # ---- 家庭信息看板：逐条写入「家庭作业」组件 ----
    for e in hw_entries:
        ok, info = push_inkboard(e["text"], e.get("due", ""),
                                 teacher=e.get("teacher", ""), subject=e.get("subject", ""))
        log("InkBoard 作业 %s %s" % ("OK" if ok else "FAIL", "" if ok else info))

    # ---- 企业微信：文字 + 图片 ----
    lines = ["【班级作业】" + (" · AI 整理" if used_llm else "")]
    if ts:
        lines.append("转发时间：%s" % ts)
    if username:
        lines.append("来源：%s" % username)
    if llm_summary:
        lines.append("")
        lines.append(llm_summary)
    if text:
        lines.append("")
        lines.append(text)
    if not is_hw:
        lines.append("")
        lines.append("（AI 判断非作业内容，未写入家庭作业看板）")
    ok, info = wecom_send_text("\n".join(lines))
    log("企微文本 %s %s" % ("OK" if ok else "FAIL", "" if ok else info))

    for s in images:
        ok, info = wecom_send_image(s["path"])
        log("企微图片 %s %s %s" % ("OK" if ok else "FAIL", os.path.basename(s["path"]), "" if ok else info))

    if not text and not saved:
        log("WARNING 这条消息既没有文本也没有附件（message_type=%s），可能被平台结构改了" % raw.get("message_type"))
        return {"openid": openid, "msg_id": raw.get("id"),
                "ack": "这条没解析出文本或附件（可能是特殊消息类型），请手动补录。"}

    bits = ["已记录"]
    if used_llm:
        bits.append("AI 已整理")
    if text:
        bits.append("文本 %d 字" % len(text))
    if images:
        bits.append("图片 %d 张" % len(images))
    if not is_hw:
        return {"openid": openid, "msg_id": raw.get("id"),
                "ack": "✅ 已收到（AI 判断非作业内容，已发企微提醒，未写入作业看板）。"}
    return {"openid": openid, "msg_id": raw.get("id"),
            "ack": "✅ " + "，".join(bits) + "，已推送到家庭看板和企微。"}


# --------------------------------------------------------------------------
# botpy 接线
# --------------------------------------------------------------------------
RAW_QUEUE = None
REPLY_QUEUE = None
API_REF = {"api": None}

_orig_parse_c2c = _botconn.ConnectionState.parse_c2c_message_create


def _patched_parse_c2c(self, payload):
    """接管 C2C 事件，把原始 payload 投入队列（botpy 的对象会丢字段）。"""
    try:
        d = payload.get("d") or {}
        if RAW_QUEUE is not None:
            RAW_QUEUE.put_nowait(d)
        if API_REF.get("api") is None:
            API_REF["api"] = getattr(self, "api", None)
    except Exception as e:
        log("WARNING 事件入队失败: %s" % e)
    return _orig_parse_c2c(self, payload)


# 注意：ConnectionState 在 __init__ 里用 inspect.getmembers 把 parse_* 方法快照进
# self.parsers，所以补丁必须在实例创建之前打（这里处于模块导入期，client.start() 才实例化）。
_botconn.ConnectionState.parse_c2c_message_create = _patched_parse_c2c


class HomeworkClient(botpy.Client):
    async def on_ready(self):
        API_REF["api"] = self.api
        log("机器人已上线，等待转发消息…")


async def worker():
    loop = asyncio.get_event_loop()
    while True:
        raw = await RAW_QUEUE.get()
        try:
            ack = await loop.run_in_executor(None, process, raw)
            # 回执投递必须回到事件循环线程（asyncio.Queue 非线程安全）
            if ack and REPLY_QUEUE is not None and ack.get("openid") and ack.get("msg_id"):
                await REPLY_QUEUE.put((ack["openid"], ack["msg_id"], ack["ack"]))
        except Exception as e:
            log("ERROR 处理消息异常: %s: %s" % (type(e).__name__, e))


async def reply_worker():
    """异步回执：告诉转发的人「已收到」，避免用户不确定有没有成功。"""
    if not CFG.get("reply_enabled"):
        return
    while True:
        item = await REPLY_QUEUE.get()
        openid, msg_id, content = item
        api = API_REF.get("api")
        if not api:
            continue
        try:
            await api.post_c2c_message(
                openid=openid, msg_type=0, content=content,
                msg_id=msg_id, msg_seq=1,
            )
        except Exception as e:
            log("WARNING 回执发送失败: %s: %s" % (type(e).__name__, e))


def main():
    global RAW_QUEUE, REPLY_QUEUE
    cfg = load_config()
    ensure_dirs()
    log("=" * 60)
    log("QQ 作业收集机器人启动（media=%s）" % (cfg.get("media_dir") or MEDIA_DIR))

    RAW_QUEUE = asyncio.Queue()
    REPLY_QUEUE = asyncio.Queue()

    # 启动本地格式化 API（管理端调用大模型用），与 QQ 长连互不干扰
    if (CFG.get("api") or {}).get("enabled", True):
        threading.Thread(target=run_format_api, daemon=True).start()

    intents = Intents.none()
    intents.public_messages = True

    client = HomeworkClient(intents=intents, bot_log=False)

    async def runner():
        asyncio.create_task(worker())
        asyncio.create_task(reply_worker())
        async with client:
            await client.start(appid=cfg["appid"], secret=cfg["secret"])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:
        log("已手动停止")
    except Exception as e:
        log("FATAL %s: %s" % (type(e).__name__, e))
        raise


if __name__ == "__main__":
    main()
