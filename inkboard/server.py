#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
InkBoard · 墨水屏家庭信息台
零依赖本地 Web 服务（仅使用 Python 标准库）

路由:
  GET  /                 显示端（墨水屏常驻页面）
  GET  /admin            管理端（手机 / 电脑打开改内容）
  GET  /api/state        读取完整状态
  POST /api/update       动作分发 {op, payload} -> 返回最新 state
  POST /api/import       整体导入（备份恢复）

数据: data/board.json（原子写入，每次变更自动留一份备份快照）
"""

import argparse
import base64
import hmac
import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "board.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

CST = timezone(timedelta(hours=8))
LOCK = threading.Lock()
STATE = {}
LAST_SAVED = 0.0
SAVE_PENDING = False


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def new_id(prefix="i"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


def today_str(offset=0):
    return (datetime.now(CST).date() + timedelta(days=offset)).isoformat()


# --------------------------------------------------------------------------
# 组件注册表（服务端只保留「新建组件时的默认配置」，
# 渲染 / 字段元数据在前端，避免两处逻辑不同步）
# --------------------------------------------------------------------------
COMPONENT_DEFAULTS = {
    "clock":     {"showSeconds": False, "hour12": False, "size": 10},
    "date":      {"showWeek": True, "note": "", "size": 4.2},
    "todo":      {"maxItems": 5, "showDone": False, "title": "今天要处理"},
    "homework":  {"title": "家庭作业", "size": 2.6},
    "schedule":  {"maxItems": 4, "title": "家庭日程", "withinDays": 3, "showTime": True},
    "message":   {"maxItems": 3, "title": "留言板"},
    "countdown": {"label": "倒计时", "target": "", "size": 7},
    "text":      {"content": "在这里写一句话", "size": 2.4},
    "weather":   {},
    "course":    {"size": 1, "showTime": True},
    "timewx":    {"showSeconds": False, "showWeather": True, "showHumidity": True, "size": 13, "dateSize": 0.235, "weekSize": 0.235, "tempSize": 0.245, "descSize": 0.24, "humSize": 0.2},
}

# 每个组件的默认宽度档位：auto=按屏幕自动 / half=半幅 / third=三分之一 / full=整幅
COMPONENT_SPAN = {
    "clock": "auto", "date": "auto", "todo": "auto", "homework": "auto",
    "schedule": "auto", "message": "auto",
    "countdown": "auto", "text": "auto", "weather": "auto", "course": "auto", "timewx": "auto",
}

# 自由布局（layoutMode=free）的坐标基准分辨率：华为 MatePad Paper 原生像素。
# 横屏 1872×1404，竖屏 1404×1872。组件 geo 以该分辨率存储，显示端按实际屏幕等比缩放。
BOARD_GEO = {"land": (1872, 1404), "port": (1404, 1872)}
GEO_MIN_W, GEO_MIN_H = 200, 120     # 组件最小宽/高（基准像素），防止被拖成看不见


def clean_geo_one(orient, g):
    """收敛单个方向的几何 {x,y,w,h}：数值化 + 钳制在画布范围内。非法返回 None。"""
    if orient not in BOARD_GEO or not isinstance(g, dict):
        return None
    cw, ch = BOARD_GEO[orient]
    try:
        x = int(round(float(g.get("x", 0))))
        y = int(round(float(g.get("y", 0))))
        w = int(round(float(g.get("w", cw // 2))))
        h = int(round(float(g.get("h", 300))))
    except (TypeError, ValueError):
        return None
    w = max(GEO_MIN_W, min(w, cw))
    h = max(GEO_MIN_H, min(h, ch))
    # 先定宽高，再按「实际宽高」钳位置，保证 x+w<=cw、y+h<=ch（永不溢出画布）
    x = max(0, min(x, cw - w))
    y = max(0, min(y, ch - h))
    return {"x": x, "y": y, "w": w, "h": h}


def clean_geo(geo):
    """收敛组件完整几何 {land:{...}, port:{...}}；非字典或全非法时返回 {}。"""
    if not isinstance(geo, dict):
        return {}
    out = {}
    for orient in BOARD_GEO:
        g = clean_geo_one(orient, geo.get(orient))
        if g:
            out[orient] = g
    return out


# --------------------------------------------------------------------------
# 默认数据（首次启动预置示例，含 1 条逾期）
# --------------------------------------------------------------------------
def default_state():
    return {
        "version": 1,
        "settings": {
            "title": "家庭信息台",
            "reloadSec": 300,      # 整页全刷周期（秒）
            "fullFlash": True,     # 全刷前闪白，减轻墨水屏残影
            "invert": False,       # 黑白反色
            "boardW": 800,         # 目标分辨率（预览用）
            "boardH": 480,
            "fitMode": "auto",     # auto=铺满设备屏幕 / fixed=按目标分辨率预览
            "showFooter": True,    # 底部状态行（更新时间 + 访问地址）
            "webhookToken": "",    # 外部推送鉴权；留空=局域网内不鉴权
            "nightEnabled": False,   # 夜间自动息屏：显示端在时段内切全黑/暗时钟，并释放 wakeLock 让系统真正休眠
            "nightStart": "23:00",   # 夜间开始（HH:MM，24 小时制）
            "nightEnd": "07:00",     # 夜间结束（HH:MM，可小于 start = 跨午夜）
            "nightMode": "black",    # black=整页全黑（推荐，对睡眠零干扰）/ clock=只留暗时钟
            "layoutMode": "auto",    # auto=自动瀑布流排版 / free=管理端模拟器自由拖拽定位
            "removedComponents": [],  # 用户显式删除的自动注入组件（weather/course），重启后不再重生
            "weatherCity": "深圳",    # 天气组件城市（Open-Meteo 免费接口，无需 key / 无需注册）
            "weatherLat": 22.5431,    # 纬度（留空/0 时按城市名自动地理编码）
            "weatherLon": 114.0579,   # 经度
            "weatherDays": 2,         # 预报天数（今天 + 明天，固定 2 天）
            "fontDefaults": {},       # 各组件字号缺省：{组件id:{字号字段:值}}，由管理端「保存当前字号为缺省」写入；空字典=尚未保存过
        },
        "layout": [
            {"uid": new_id("c"), "id": "clock", "enabled": True,
             "config": {"showSeconds": False, "hour12": False, "size": 10}},
            {"uid": new_id("c"), "id": "date", "enabled": True,
             "config": {"showWeek": True, "note": "旭旭开学第 1 周"}},
            {"uid": new_id("c"), "id": "todo", "enabled": True,
             "config": {"maxItems": 5, "showDone": False, "title": "今天要处理"}},
            {"uid": new_id("c"), "id": "homework", "enabled": True,
             "config": {"maxItems": 5, "showDone": False, "title": "家庭作业"}},
            {"uid": new_id("c"), "id": "message", "enabled": True,
             "config": {"maxItems": 3, "title": "留言板"}},
            {"uid": new_id("c"), "id": "schedule", "enabled": True,
             "config": {"title": "家庭日程", "maxItems": 4, "withinDays": 3, "showTime": True}},
            {"uid": new_id("c"), "id": "weather", "enabled": True,
             "config": {}},
            {"uid": new_id("c"), "id": "course", "enabled": True,
             "config": {"size": 1}},
        ],
        "todos": [
            {"id": new_id("t"), "text": "交电费", "done": False,
             "due": today_str(0), "createdAt": now_iso()},
            {"id": new_id("t"), "text": "取快递（示例·已逾期 2 天）", "done": False,
             "due": today_str(-2), "createdAt": now_iso()},
            {"id": new_id("t"), "text": "预约牙科检查", "done": False,
             "due": today_str(3), "createdAt": now_iso()},
            {"id": new_id("t"), "text": "给旭旭报 Python 课（示例·已完成）", "done": True,
             "due": today_str(-1), "createdAt": now_iso()},
        ],
        "homework": [],
        "messages": [
            {"id": new_id("m"), "text": "晚上回来吃饭，不用等我先开饭",
             "author": "爸爸", "createdAt": now_iso()},
            {"id": new_id("m"), "text": "冰箱里的牛奶明天到期，记得喝",
             "author": "妈妈", "createdAt": now_iso()},
        ],
        "events": [
            {"id": new_id("e"), "title": "旭旭 Python 课", "date": today_str(0),
             "start": "19:00", "end": "20:30", "source": "示例"},
            {"id": new_id("e"), "title": "牙科复查", "date": today_str(1),
             "start": "09:30", "end": "", "source": "示例"},
        ],
        # 课程表：管理端上传的 Markdown 表格（首列=时间，后五列=周一~周五）
        # 显示端只渲染「今天」「明天」两天的课程
        "courses": (
            "**三（2）班课程表（第1–6节）**\n\n"
            "2026–2027 学年第一学期\n\n"
            "| 时间 | 周一 | 周二 | 周三 | 周四 | 周五 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 第1节 8:30-9:10 | 数学 | 科学 | 数学 | 语文 | 语文 |\n"
            "| 第2节 9:40-10:20 | 英语 | 语文 | 综合 | 【单】道法0/【双】心理 | 体育专项 |\n"
            "| 第3节 10:30-11:10 | 信息 | 体育 | 语文 | 英语 | 语文 |\n"
            "| 第4节 11:20-12:00 | 语文 | 数学 | 语文 | 数学 | 科学 |\n"
            "| 第5节 14:10-14:50 | 体育 | 道法 | 音乐 | 美术 | 形体 |\n"
            "| 第6节 15:05-15:45 | 班会（劳动） | 阅读 | 体育 | 体育专项1 | 【单】外教(小)/【双】道法1 |\n"
        ),
        "updatedAt": now_iso(),
    }


def normalize(state):
    """补齐缺失字段，保证向后兼容（老数据升级不丢）。"""
    base = default_state()
    if not isinstance(state, dict):
        return base
    for k, v in base.items():
        if k not in state or state[k] is None:
            state[k] = v
    for k, v in base["settings"].items():
        state["settings"].setdefault(k, v)
    if not isinstance(state.get("layout"), list):
        state["layout"] = base["layout"]
    # 老数据补齐 uid（同一类型可重复添加，靠 uid 唯一定位）
    for c in state["layout"]:
        if not isinstance(c, dict):
            continue
        if not c.get("uid"):
            c["uid"] = new_id("c")
        if not isinstance(c.get("config"), dict):
            c["config"] = dict(COMPONENT_DEFAULTS.get(c.get("id"), {}))
        if c.get("span") not in ("auto", "half", "third", "full"):
            c["span"] = COMPONENT_SPAN.get(c.get("id"), "auto")
        # 自由布局几何：老数据无此字段；有则收敛（非法几何清掉，不阻断加载）
        g = clean_geo(c.get("geo"))
        if g:
            c["geo"] = g
        elif "geo" in c:
            c.pop("geo", None)
        # 日程「显示未来几天」约束在 1-14（管理端输入已限制，这里兜底防越界）
        if c.get("id") == "schedule" and isinstance(c.get("config"), dict):
            wd = c["config"].get("withinDays")
            try:
                wd = int(wd)
            except (TypeError, ValueError):
                wd = None
            if not isinstance(wd, int) or wd < 1 or wd > 14:
                c["config"]["withinDays"] = 14
    for k in ("todos", "messages", "events", "homework"):
        if not isinstance(state.get(k), list):
            state[k] = []
    # 天气数据容器：缺失则补空结构（老数据升级兼容）
    if not isinstance(state.get("weather"), dict):
        state["weather"] = {}
    # 课程表文本：缺失则补空字符串（老数据升级兼容）
    if "courses" not in state or not isinstance(state.get("courses"), str):
        state["courses"] = ""
    # 老数据自动补齐 weather / course 组件；但尊重用户「显式删除」的意图：
    # 已删除的组件 id 记入 settings.removedComponents，重启后不再被重生。
    removed = state["settings"].get("removedComponents")
    if not isinstance(removed, list):
        removed = []
        state["settings"]["removedComponents"] = removed
    # 防御性清理：若某个组件「在布局里」+「被标记删除」（admin 加回过却没清标记，
    # 或外部直接编辑 board.json 造成的不一致），就把布局里那一份也清掉，保证两端一致。
    if removed:
        state["layout"] = [c for c in state.get("layout", [])
                           if isinstance(c, dict) and c.get("id") not in removed]
    existing_ids = {c.get("id") for c in state.get("layout", []) if isinstance(c, dict)}
    for cid in ("weather", "course"):
        if cid in existing_ids or cid in removed:
            continue
        state["layout"].append({
            "uid": new_id("c"), "id": cid, "enabled": True,
            "span": COMPONENT_SPAN.get(cid, "auto"), "config": {},
        })
    state["updatedAt"] = now_iso()
    return state


def _layout_index(state, p):
    """按 uid 优先定位布局项；无 uid 的老数据回退按组件类型。"""
    uid = p.get("uid")
    if uid:
        for i, c in enumerate(state["layout"]):
            if c.get("uid") == uid:
                return i
        return -1
    cid = p.get("id")
    for i, c in enumerate(state["layout"]):
        if c.get("id") == cid:
            return i
    return -1


def _find_layout_item(state, p):
    i = _layout_index(state, p)
    return state["layout"][i] if i >= 0 else None


def clean_event(e):
    """外部写入的日程做字段与长度收敛，避免脏数据把页面撑坏。"""
    if not isinstance(e, dict):
        return None
    title = (e.get("title") or e.get("text") or "").strip()
    if not title:
        return None
    return {
        "id": (e.get("id") or new_id("e")),
        "title": title[:80],
        "date": (e.get("date") or "").strip()[:10],
        "start": (e.get("start") or "").strip()[:5],
        "end": (e.get("end") or "").strip()[:5],
        "source": (e.get("source") or "").strip()[:20],
    }


def _course_markdown_valid(md):
    """校验课程表 Markdown 是否为「首列=时间、后五列=周一~周五」的表格。

    仅做结构性校验（表头含 时间 + 周一~周五、至少 1 行课程），不校验具体科目内容。"""
    if not isinstance(md, str) or not md.strip():
        return False
    rows = []
    for ln in md.splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not cells:
            continue
        # 跳过分隔行（形如 | --- | --- |）
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return False
    header = [h.replace(" ", "") for h in rows[0]]
    if "时间" not in header:
        return False
    for wd in ("周一", "周二", "周三", "周四", "周五"):
        if wd not in header:
            return False
    return True


# --------------------------------------------------------------------------
# 天气组件（Open-Meteo 免费接口，无需 key / 无需注册）
# --------------------------------------------------------------------------
WMO_MAP = {
    0:  ("晴",        "sun"),
    1:  ("晴间多云",   "sun-cloud"),
    2:  ("多云",       "cloud"),
    3:  ("阴",         "cloud"),
    45: ("雾",        "fog"),
    48: ("雾凇",       "fog"),
    51: ("毛毛雨",     "drizzle"),
    53: ("小雨",       "drizzle"),
    55: ("中雨",       "rain"),
    56: ("冻毛毛雨",    "drizzle"),
    57: ("冻雨",       "rain"),
    61: ("小雨",       "rain"),
    63: ("中雨",       "rain"),
    65: ("大雨",       "rain"),
    66: ("冻雨",       "rain"),
    67: ("强冻雨",     "rain"),
    71: ("小雪",       "snow"),
    73: ("中雪",       "snow"),
    75: ("大雪",       "snow"),
    77: ("雪粒",       "snow"),
    80: ("阵雨",       "rain"),
    81: ("强阵雨",      "rain"),
    82: ("暴雨",       "rain"),
    85: ("阵雪",       "snow"),
    86: ("强阵雪",      "snow"),
    95: ("雷阵雨",      "thunder"),
    96: ("雷阵雨伴冰雹", "thunder"),
    99: ("强雷阵雨伴冰雹","thunder"),
}
def wmo_info(code):
    try:
        return WMO_MAP.get(int(code), ("未知", "cloud"))
    except Exception:
        return ("未知", "cloud")


GEO_CACHE = {"city": None, "lat": None, "lon": None}


def _http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "InkBoard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def geocode_city(city):
    """Open-Meteo 地理编码 API：城市名 -> (lat, lon)；失败返回 None。"""
    try:
        u = "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh&format=json" % urllib.parse.quote(city)
        data = _http_get_json(u)
        res = data.get("results") or []
        if res:
            r = res[0]
            return float(r.get("latitude")), float(r.get("longitude"))
    except Exception as e:
        print("[WARN] 城市解析失败 %s: %s" % (city, e))
    return None


def fetch_weather():
    """拉取天气并就地更新 STATE['weather']；网络失败保留旧值、不崩。"""
    global GEO_CACHE
    settings = STATE.get("settings") or {}
    city = (settings.get("weatherCity") or "深圳").strip() or "深圳"
    lat = settings.get("weatherLat")
    lon = settings.get("weatherLon")
    # 经纬度缺失/为 0，或城市变更 -> 重新地理编码
    if (not lat or not lon) or GEO_CACHE.get("city") != city:
        g = geocode_city(city)
        if g:
            lat, lon = g
            GEO_CACHE = {"city": city, "lat": lat, "lon": lon}
            settings["weatherLat"] = lat
            settings["weatherLon"] = lon
            schedule_save(STATE)          # 写回经纬度，下次启动免解析
        elif not lat or not lon:
            return                        # 连经纬度都没有，无法拉取
    try:
        days = max(1, min(3, int(settings.get("weatherDays") or 3)))
        u = ("https://api.open-meteo.com/v1/forecast"
             "?latitude=%.4f&longitude=%.4f"
             "&current=temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m"
             "&daily=weather_code,temperature_2m_max,temperature_2m_min"
             "&wind_speed_unit=kmh&timezone=auto&forecast_days=%d") % (float(lat), float(lon), days)
        data = _http_get_json(u)
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        d_dates = daily.get("time") or []
        d_codes = daily.get("weather_code") or []
        d_max = daily.get("temperature_2m_max") or []
        d_min = daily.get("temperature_2m_min") or []
        fc = []
        for i in range(len(d_dates)):
            ci = d_codes[i] if i < len(d_codes) else None
            info = wmo_info(ci)
            fc.append({
                "date": d_dates[i],
                "code": ci,
                "text": info[0],
                "icon": info[1],
                "max": d_max[i] if i < len(d_max) else None,
                "min": d_min[i] if i < len(d_min) else None,
            })
        cur_info = wmo_info(cur.get("weather_code"))
        with LOCK:
            STATE["weather"] = {
                "city": city,
                "temp": cur.get("temperature_2m"),
                "code": cur.get("weather_code"),
                "text": cur_info[0],
                "icon": cur_info[1],
                "humidity": cur.get("relative_humidity_2m"),
                "wind": cur.get("wind_speed_10m"),
                "updated": now_iso(),
                "daily": fc,
                "ok": True,
            }
        print("[OK] 天气已更新：%s %s℃ %s" % (city, cur.get("temperature_2m"), cur_info[0]))
    except Exception as e:
        print("[WARN] 天气拉取失败：%s" % e)
        with LOCK:
            if not isinstance(STATE.get("weather"), dict) or not STATE["weather"].get("ok"):
                STATE["weather"] = {"ok": False, "error": str(e), "updated": now_iso()}


def weather_worker(interval=600):
    """后台定时拉取（默认 10 分钟），网络抖动不影响主服务。"""
    while True:
        time.sleep(interval)
        try:
            fetch_weather()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------
def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return normalize(json.load(f))
        except Exception as e:
            print("[WARN] 读取数据失败(%s)，已使用默认数据。旧文件保留为 board.corrupt.json" % e)
            try:
                shutil.copy(DATA_FILE, DATA_FILE.replace(".json", ".corrupt.json"))
            except Exception:
                pass
    return default_state()


def save_state(state, keep_backup=True):
    os.makedirs(DATA_DIR, exist_ok=True)
    if keep_backup and os.path.exists(DATA_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, "board-%s.json" % stamp)
        n = 1
        while os.path.exists(dst):          # 同一秒内的多次改动互不覆盖
            dst = os.path.join(BACKUP_DIR, "board-%s-%d.json" % (stamp, n))
            n += 1
        try:
            shutil.copy(DATA_FILE, dst)
            _trim_backups()
        except Exception:
            pass
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _trim_backups(keep=30):
    try:
        files = sorted(
            [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".json")]
        )
        for f in files[:-keep]:
            os.remove(f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 动作分发（统一入口，前端只调 /api/update）
# --------------------------------------------------------------------------
def apply_op(state, op, payload):
    p = payload or {}

    # ---- 待办 ----
    if op == "todo.add":
        text = (p.get("text") or "").strip()
        if not text:
            raise ValueError("待办内容不能为空")
        state["todos"].append({
            "id": new_id("t"), "text": text[:120], "done": False,
            "due": (p.get("due") or "").strip(), "createdAt": now_iso(),
        })
    elif op == "todo.toggle":
        for t in state["todos"]:
            if t["id"] == p.get("id"):
                t["done"] = not bool(t.get("done"))
                break
    elif op == "todo.update":
        for t in state["todos"]:
            if t["id"] == p.get("id"):
                if "text" in p:
                    t["text"] = str(p["text"])[:120]
                if "due" in p:
                    t["due"] = str(p["due"])
                break
    elif op == "todo.delete":
        state["todos"] = [t for t in state["todos"] if t["id"] != p.get("id")]
    elif op == "todo.clearDone":
        state["todos"] = [t for t in state["todos"] if not t.get("done")]

    # ---- 家庭作业（QQ 机器人转发 / 管理端录入，独立数据源，与待办互不混用）----
    elif op == "homework.add":
        text = (p.get("text") or "").strip()
        if not text:
            raise ValueError("作业内容不能为空")
        state["homework"].append({
            "id": new_id("h"),
            "text": text[:110],
            "done": False,
            "due": (p.get("due") or "").strip(),
            "teacher": (p.get("teacher") or "").strip()[:16],
            "subject": (p.get("subject") or "").strip()[:10],
            "source": (p.get("source") or "manual").strip()[:16],
            "createdAt": now_iso(),
        })
    elif op == "homework.toggle":
        for h in state["homework"]:
            if h["id"] == p.get("id"):
                h["done"] = not bool(h.get("done"))
                break
    elif op == "homework.update":
        for h in state["homework"]:
            if h["id"] == p.get("id"):
                if "text" in p:
                    h["text"] = str(p["text"])[:110]
                if "due" in p:
                    h["due"] = str(p["due"])
                if "teacher" in p:
                    h["teacher"] = str(p["teacher"])[:16]
                if "subject" in p:
                    h["subject"] = str(p["subject"])[:10]
                break
    elif op == "homework.delete":
        state["homework"] = [h for h in state["homework"] if h["id"] != p.get("id")]
    elif op == "homework.clearDone":
        state["homework"] = [h for h in state["homework"] if not h.get("done")]

    # ---- 留言 ----
    elif op == "msg.add":
        text = (p.get("text") or "").strip()
        if not text:
            raise ValueError("留言内容不能为空")
        state["messages"].insert(0, {
            "id": new_id("m"), "text": text[:200],
            "author": (p.get("author") or "").strip()[:16], "createdAt": now_iso(),
        })
    elif op == "msg.delete":
        state["messages"] = [m for m in state["messages"] if m["id"] != p.get("id")]

    # ---- 课程表（管理端上传的 Markdown）----
    elif op == "courses.set":
        md = p.get("markdown")
        if not isinstance(md, str):
            raise ValueError("课程表内容不合法")
        if len(md) > 20000:
            raise ValueError("课程表内容过长（上限 20000 字）")
        # 必须与「首列=时间、后五列=周一~周五」的表格结构匹配，否则拒绝写入
        if not _course_markdown_valid(md):
            raise ValueError("课程表格式不正确：需为「首列=时间、后五列=周一~周五」的 Markdown 表格")
        state["courses"] = md
    elif op == "courses.clear":
        state["courses"] = ""

    # ---- 日程（外部数据源同步用）----
    elif op == "events.set":
        raw = p.get("events")
        if not isinstance(raw, list):
            raise ValueError("events 格式不正确")
        cleaned = [c for c in (clean_event(e) for e in raw) if c]
        # 按来源整体替换：本次出现的 source（含「无来源」）先清旧，避免反复同步累积
        touched = set((c.get("source") or "") for c in cleaned)
        keep = [e for e in state["events"] if (e.get("source") or "") not in touched]
        # 同源内去重：同一 (标题,日期,开始) 只保留最后一条，避免企微「修改例外」在显示端叠出两条
        dedup = {}
        for c in cleaned:
            dedup[(c.get("title"), c.get("date"), c.get("start"))] = c
        cleaned = list(dedup.values())
        state["events"] = keep + cleaned
    elif op == "events.add":
        e = clean_event(p)
        if not e:
            raise ValueError("日程标题不能为空")
        state["events"].append(e)
    elif op == "events.delete":
        state["events"] = [e for e in state["events"] if e["id"] != p.get("id")]
    elif op == "events.clear":
        state["events"] = []

    # ---- 组件布局 ----
    elif op == "layout.set":
        layout = p.get("layout")
        if not isinstance(layout, list):
            raise ValueError("layout 格式不正确")
        cleaned = []
        for c in layout:
            if not isinstance(c, dict) or c.get("id") not in COMPONENT_DEFAULTS:
                continue        # 过滤未知组件，防止脏数据把显示端撑坏
            if not c.get("uid"):
                c["uid"] = new_id("c")
            if c.get("span") not in ("auto", "half", "third", "full"):
                c["span"] = COMPONENT_SPAN.get(c["id"], "auto")
            if not isinstance(c.get("config"), dict):
                c["config"] = dict(COMPONENT_DEFAULTS[c["id"]])
            cg = clean_geo(c.get("geo"))
            if cg:
                c["geo"] = cg
            elif "geo" in c:
                c.pop("geo", None)
            cleaned.append(c)
        if not cleaned:
            raise ValueError("至少保留一个组件")
        state["layout"] = cleaned
    elif op == "layout.add":
        cid = (p.get("id") or "").strip()
        if cid not in COMPONENT_DEFAULTS:
            raise ValueError("未知组件类型：%s" % cid)
        if len(state["layout"]) >= 12:
            raise ValueError("组件数量已达上限（12 个）")
        cfg = dict(COMPONENT_DEFAULTS[cid])
        if isinstance(p.get("config"), dict):
            cfg.update(p["config"])
        state["layout"].append({
            "uid": new_id("c"),
            "id": cid,
            "enabled": True,
            "span": COMPONENT_SPAN.get(cid, "auto"),
            "config": cfg,
        })
        # 若添加的是此前被显式删除的自动注入组件，清除删除标记（允许复活）
        rl = state["settings"].get("removedComponents")
        if isinstance(rl, list) and cid in rl:
            rl.remove(cid)
    elif op == "layout.remove":
        uid = p.get("uid")
        before = len(state["layout"])
        removed_id = None
        if uid:
            # 优先按 uid 精确删除；uid 提供了却没匹配到 = 组件已不存在或 uid 错误，应报错而非误删其它
            for c in state["layout"]:
                if c.get("uid") == uid:
                    removed_id = c.get("id")
                    break
            state["layout"] = [c for c in state["layout"] if c.get("uid") != uid]
        else:
            # 兼容老客户端（不带 uid，旧数据组件无 uid 字段）：按组件类型删第一个匹配项
            for c in state["layout"]:
                if c.get("id") == p.get("id"):
                    removed_id = c.get("id")
                    break
            state["layout"] = [c for c in state["layout"] if c.get("id") != p.get("id")]
        if len(state["layout"]) == before:
            raise ValueError("未找到要删除的组件")
        # 记录用户显式删除的自动注入类组件（weather/course），重启后不再被 normalize() 重生
        if removed_id in ("weather", "course"):
            rl = state["settings"].setdefault("removedComponents", [])
            if removed_id not in rl:
                rl.append(removed_id)
    elif op == "layout.toggle":
        c = _find_layout_item(state, p)
        if c is not None:
            c["enabled"] = not bool(c.get("enabled"))
    elif op == "layout.move":
        i = _layout_index(state, p)
        if i >= 0:
            j = i + (-1 if int(p.get("delta", 0)) < 0 else 1)
            if 0 <= j < len(state["layout"]):
                state["layout"][i], state["layout"][j] = state["layout"][j], state["layout"][i]
    elif op == "layout.config":
        c = _find_layout_item(state, p)
        if c is not None:
            cfg = c.get("config") or {}
            cfg.update(p.get("config") or {})
            c["config"] = cfg
            # 宽度档位单独存一层，不混进 config（config 只放组件业务字段）
            if p.get("span") in ("auto", "half", "third", "full"):
                c["span"] = p["span"]
    elif op == "layout.geo":
        # 管理端模拟器保存：items=[{uid 或 id, geo:{land,port}}, ...]
        items = p.get("items")
        if not isinstance(items, list):
            raise ValueError("items 格式不正确")
        n_upd = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            g = clean_geo(it.get("geo"))
            if not g:
                continue
            c = _find_layout_item(state, it)
            if c is not None:
                # 合并而非替换：本次只提交合法方向的几何，保留该组件其它方向已存值
                old = c.get("geo") if isinstance(c.get("geo"), dict) else {}
                merged = dict(old)
                merged.update(g)
                c["geo"] = clean_geo(merged)
                n_upd += 1
        # 保存几何的同时可选切换排版模式：auto / free
        if p.get("mode") in ("auto", "free"):
            state["settings"]["layoutMode"] = p["mode"]
        if n_upd == 0:
            raise ValueError("没有可保存的组件位置")

    # ---- 设置 / 全局 ----
    elif op == "settings.set":
        state["settings"].update(p.get("settings") or {})
    elif op == "reset.demo":
        fresh = default_state()
        fresh["settings"] = state.get("settings", fresh["settings"])
        state.update(fresh)
    elif op == "state.replace":
        incoming = p.get("state")
        if not isinstance(incoming, dict):
            raise ValueError("导入内容格式不正确")
        state.update(normalize(incoming))

    else:
        raise ValueError("未知操作: %s" % op)

    state["updatedAt"] = now_iso()
    return state


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 管理端鉴权（可选，用 --admin-user / --admin-pass 开启）
# --------------------------------------------------------------------------
# 开启后：/admin 页面与全部写操作（/api/update /api/import /api/push）需要登录。
# 显示端 / 与只读接口 /api/state /api/health 保持开放，墨水屏不受影响。
# 只上局域网时可以不开启；一旦经公网隧道暴露，务必开启。
AUTH_USER = ""
AUTH_PASS = ""
READONLY_TOKEN = ""


def auth_enabled():
    return bool(AUTH_USER and AUTH_PASS)


def readonly_enabled():
    return bool(READONLY_TOKEN)


def read_token_file(path):
    """从令牌文件读取只读令牌（单行，去首尾空白与 BOM）。缺失返回 ''。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read().replace("\ufeff", "").strip()
    except Exception as e:
        print("警告：读取只读令牌文件失败 %s: %s" % (path, e))
        return ""


def check_readonly(req_token):
    """req_token: 请求中提供的只读令牌字符串；通过返回 True。
    未开启只读令牌时一律放行（保持向后兼容的开放状态）。"""
    if not readonly_enabled():
        return True
    if not req_token:
        return False
    return hmac.compare_digest(req_token.encode("utf-8"), READONLY_TOKEN.encode("utf-8"))


def _readonly_denied_html():
    return ('<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>InkBoard · 需要访问令牌</title></head><body style="font-family:sans-serif;'
            'padding:24px;line-height:1.6"><h2>需要访问令牌</h2>'
            '<p>该信息页已开启只读令牌保护。请在地址后附加 '
            '<code>?rt=你的令牌</code> 后访问，例如：</p>'
            '<p><code>https://你的域名/?rt=XXXX</code></p>'
            '<p>墨水屏请直接配置带令牌的完整地址。</p></body></html>')


def read_creds_file(path):
    """从凭据文件读取 (user, pass)。格式：用户名: xxx / 密码: xxx。缺失返回 (None, None)。"""
    import re
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except Exception as e:
        print("警告：读取凭据文件失败 %s: %s" % (path, e))
        return (None, None)
    u = p = None
    m = re.search(r"用户名\s*[:：]\s*(.*)", text)
    if m:
        u = m.group(1).strip()
    m = re.search(r"密码\s*[:：]\s*(.*)", text)
    if m:
        p = m.group(1).strip()
    return (u, p)


def check_basic(headers):
    """校验 Authorization: Basic 头，通过返回 True。"""
    if not auth_enabled():
        return False
    raw = headers.get("Authorization") or ""
    if not raw.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(raw[6:]).decode("utf-8")
    except Exception:
        return False
    user, _sep, pwd = decoded.partition(":")
    return (hmac.compare_digest(user, AUTH_USER)
            and hmac.compare_digest(pwd, AUTH_PASS))


MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "InkBoard/1.0"

    # 外部推送（/api/push）只允许这些操作，避免被拿来改布局或删数据
    WEBHOOK_OPS = ("todo.add", "homework.add", "msg.add", "events.set", "events.add",
                   "events.delete", "events.clear")

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- helpers ---
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (str,)):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _file(self, relpath):
        path = os.path.normpath(os.path.join(WEB_DIR, relpath))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    def _file_inject(self, relpath, token):
        """同 _file，但可选地把只读令牌注入页面 <head>，供前端拉取 /api/state 时携带。"""
        path = os.path.normpath(os.path.join(WEB_DIR, relpath))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(path)[1].lower()
        if token and ext in (".html", ".htm"):
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            meta = '<meta name="readonly-token" content="%s">' % token
            if "</head>" in html:
                html = html.replace("</head>", meta + "\n</head>", 1)
            else:
                html = meta + "\n" + html
            self._send(200, html.encode("utf-8"), MIME.get(ext, "application/octet-stream"))
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    def _auth_gate(self):
        """需要鉴权时校验 Basic，未通过则写 401 并返回 False。"""
        if not auth_enabled() or check_basic(self.headers):
            return True
        self._send(401, "需要登录", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="InkBoard"'})
        return False

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # --- routes ---
    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        rt = (q.get("rt") or [None])[0] or self.headers.get("X-Readonly-Token") or ""
        if path == "/api/state":
            if not check_readonly(rt):
                self._json({"ok": False, "error": "需要只读令牌 ?rt="}, 401)
                return
            with LOCK:
                self._json({"ok": True, "state": STATE})
            return
        if path == "/api/health":
            self._json({"ok": True, "time": now_iso(), "version": 1})
            return
        if path in ("/", "/index.html", "/board"):
            if not check_readonly(rt):
                self._send(401, _readonly_denied_html(), "text/html; charset=utf-8")
                return
            self._file_inject("index.html", rt if readonly_enabled() else "")
            return
        if path in ("/admin", "/admin.html"):
            if not self._auth_gate():
                return
            self._file("admin.html")
            return
        if path.startswith("/"):
            self._file(path.lstrip("/") or "index.html")
            return
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def _apply_and_reply(self, op, payload):
        global STATE
        with LOCK:
            try:
                STATE = apply_op(STATE, op, payload)
                snapshot = json.loads(json.dumps(STATE))
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, 400)
                return
        schedule_save(snapshot)
        self._json({"ok": True, "state": snapshot})

    def _handle_push(self, body):
        """外部系统推送入口：只放行白名单操作，可配 token 鉴权。"""
        token = (STATE.get("settings") or {}).get("webhookToken") or ""
        authed = check_basic(self.headers)
        if token:
            got = body.get("token") or self.headers.get("X-InkBoard-Token") or ""
            if got != token and not authed:
                self._json({"ok": False, "error": "token 无效"}, 401)
                return
        elif auth_enabled() and not authed:
            self._json({"ok": False, "error": "需要登录"}, 401)
            return
        op = body.get("op")
        if op not in self.WEBHOOK_OPS:
            self._json({"ok": False, "error": "推送接口不允许该操作: %s" % op}, 403)
            return
        # 允许字段平铺（{op, text, due}）或标准包裹（{op, payload:{...}}）
        payload = body.get("payload")
        if not payload:
            payload = dict((k, v) for k, v in body.items() if k not in ("op", "token"))
        # events.add 兼容 {op, event:{...}} 包裹写法
        if op == "events.add" and isinstance(payload.get("event"), dict):
            payload = payload["event"]
        self._apply_and_reply(op, payload)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/push":
            self._handle_push(body)
            return
        if not self._auth_gate():
            return
        if path == "/api/update":
            self._apply_and_reply(body.get("op"), body.get("payload") or {})
            return

        if path == "/api/import":
            incoming = body.get("state")
            with LOCK:
                try:
                    STATE = apply_op(STATE, "state.replace", {"state": incoming})
                    snapshot = json.loads(json.dumps(STATE))
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, 400)
                    return
            do_save(snapshot)
            self._json({"ok": True, "state": snapshot})
            return

        self._json({"ok": False, "error": "未知接口"}, 404)


# --------------------------------------------------------------------------
# 延迟落盘（合并高频写入）
# --------------------------------------------------------------------------
def schedule_save(snapshot):
    global SAVE_PENDING, LAST_SAVED
    SAVE_PENDING = True
    LAST_SAVED = time.time()


def do_save(snapshot):
    global SAVE_PENDING
    try:
        save_state(snapshot)
        SAVE_PENDING = False
    except Exception as e:
        print("[ERROR] 保存失败: %s" % e)


def save_worker():
    global SAVE_PENDING
    while True:
        time.sleep(1.0)
        if SAVE_PENDING and (time.time() - LAST_SAVED) >= 0.8:
            with LOCK:
                snapshot = json.loads(json.dumps(STATE))
            do_save(snapshot)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global STATE
    ap = argparse.ArgumentParser(description="InkBoard 墨水屏家庭信息台")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（局域网可访问）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--admin-user",
                    default=os.environ.get("INKBOARD_ADMIN_USER", ""),
                    help="管理端 Basic 鉴权用户名（需与 --admin-pass 同时给出）")
    ap.add_argument("--admin-pass",
                    default=os.environ.get("INKBOARD_ADMIN_PASS", ""),
                    help="管理端 Basic 鉴权密码")
    ap.add_argument("--admin-pass-file", default="",
                    help="凭据文件路径（含 '用户名:' / '密码:' 行），改密后重启即生效")
    ap.add_argument("--readonly-token-file", default="",
                    help="只读令牌文件路径（单行）。设置后显示页 /api/state 需 ?rt=令牌 才能访问")
    args = ap.parse_args()

    global AUTH_USER, AUTH_PASS, READONLY_TOKEN
    AUTH_USER = (args.admin_user or "").strip()
    AUTH_PASS = args.admin_pass or ""
    if args.admin_pass_file:
        fu, fp = read_creds_file(args.admin_pass_file)
        if fu:
            AUTH_USER = fu
        if fp:
            AUTH_PASS = fp
    if bool(AUTH_USER) != bool(AUTH_PASS):
        print("警告：--admin-user 与 --admin-pass 必须同时设置，本次已忽略鉴权配置")
        AUTH_USER = AUTH_PASS = ""
    if args.readonly_token_file:
        READONLY_TOKEN = read_token_file(args.readonly_token_file)

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(WEB_DIR, exist_ok=True)
    STATE = load_state()
    if not os.path.exists(DATA_FILE):
        save_state(STATE, keep_backup=False)

    threading.Thread(target=save_worker, daemon=True).start()

    # 天气组件：启动即拉一次（让首屏有数据，最多等 8s 超时），随后后台每 10 分钟刷新
    try:
        fetch_weather()
    except Exception as e:
        print("[WARN] 初始天气拉取失败（后台会继续重试）：%s" % e)
    threading.Thread(target=weather_worker, kwargs={"interval": 600}, daemon=True).start()

    ip = lan_ip()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("")
    print("  InkBoard 墨水屏信息台已启动")
    print("  ------------------------------------------")
    print("  显示端(墨水屏打开) : http://%s:%d/" % (ip, args.port))
    print("  管理端(手机/电脑)   : http://%s:%d/admin" % (ip, args.port))
    print("  本机                : http://127.0.0.1:%d/" % args.port)
    print("  数据文件            : %s" % DATA_FILE)
    print("  管理端鉴权          : %s" % (
        "已开启（用户 %s）" % AUTH_USER if auth_enabled()
        else "未开启 —— 仅局域网可用，公网暴露前请开启"))
    print("  显示页只读令牌      : %s" % (
        "已开启（需 ?rt=令牌）" if readonly_enabled()
        else "未开启 —— 显示页与 /api/state 任何人可读取"))
    print("  ------------------------------------------")
    print("  按 Ctrl+C 停止")
    print("")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止，正在保存...")
        with LOCK:
            do_save(json.loads(json.dumps(STATE)))
        print("完成。")


if __name__ == "__main__":
    main()
