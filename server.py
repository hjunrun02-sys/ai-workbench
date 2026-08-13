#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 工作台 —— 本地可编辑服务（飞书表格风格，纯标准库，零外部依赖）

为什么需要它：
  纯静态 HTML（双击打开）在浏览器沙箱里写不了本地文件。要做到
  “编辑并确认改到本地文件”，必须有一个本地后端。本服务只绑定
  127.0.0.1（不暴露到外网），负责：
    - GET  /              返回飞书表格风格前端 workbench_app.html
    - GET  /api/data      返回 4 类数据（含写回用的 id）
    - POST /api/update    修改某条记录，真实改写本地文件 / 数据库
    - POST /api/update_multi  一次修改多个字段
    - POST /api/add       新增每日亮点
    - POST /api/add_automation  新建定时任务
    - POST /api/delete    删除（亮点 / 定时任务 / 记忆小节）
    - POST /api/shutdown  关闭本地服务

数据全部落在 DATA_DIR（默认本程序同级的 ./data，可用环境变量
AI_WORKBENCH_DATA 覆盖），不上云、不依赖任何第三方运行时。

写回目标（即“本地真源”）：
    定时任务  -> data/automations.db（SQLite，自带建表 + 示例数据）
    记忆      -> data/memory/MEMORY.md 的 ## 小节
    Skill     -> data/skill_override.json（覆盖功能说明）
    每日亮点  -> data/highlights.md（类型/内容/增删）

每次写前自动生成 .bak 备份，写坏可恢复。
"""

import os
import re
import json
import glob
import shutil
import sqlite3
import datetime
import webbrowser
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 数据目录（人人可装：默认本地 ./data，可覆盖） ----------------
DATA_DIR = os.environ.get("AI_WORKBENCH_DATA") or os.path.join(HERE, "data")
HIGHLIGHTS_PATH = os.path.join(DATA_DIR, "highlights.md")
MEMORY_PATH = os.path.join(DATA_DIR, "memory", "MEMORY.md")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
OVERRIDE_PATH = os.path.join(DATA_DIR, "skill_override.json")
DB = os.path.join(DATA_DIR, "automations.db")
HTML_PATH = os.path.join(HERE, "workbench_app.html")
PORT = 8765

VALID_TYPES = ["决策", "学习", "洞察", "任务", "其他"]
WEEK_MAP = {"MO": "一", "TU": "二", "WE": "三", "TH": "四", "FR": "五", "SA": "六", "SU": "日"}


# ---------------- 备份 ----------------
def backup(path):
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")


# ---------------- 自包含的展示/解析原语（不再依赖 sync.py） ----------------
def short_desc(prompt):
    if not prompt:
        return ""
    p = re.sub(r"\s+", " ", prompt.strip())
    return p[:60] + ("…" if len(p) > 60 else "")


def rrule_to_cn(rrule, schedule_type, scheduled_at):
    if (schedule_type or "").lower() == "once":
        if scheduled_at:
            return "一次性 " + scheduled_at[:16].replace("T", " ")
        return "一次性"
    if not rrule:
        return "（未知）"
    parts = {}
    for p in rrule.split(";"):
        if "=" in p:
            k, v = p.split("=", 1)
            parts[k] = v
    hh = parts.get("BYHOUR", "?")
    mm = parts.get("BYMINUTE", "00")
    try:
        t = f"{int(hh):02d}:{int(mm):02d}"
    except (ValueError, TypeError):
        t = f"{hh}:{mm}"
    freq = parts.get("FREQ")
    if freq == "DAILY":
        return f"每天 {t}"
    if freq == "WEEKLY":
        return f"每周{WEEK_MAP.get(parts.get('BYDAY', ''), '?')} {t}"
    if freq == "MONTHLY":
        return f"每月{parts.get('BYMONTHDAY', '?')}日 {t}"
    return rrule


def fmt_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def parse_sections(path):
    """解析 Markdown 的 ## / ### 小节，返回 [(heading, body), ...]。"""
    if not os.path.exists(path):
        return []
    out, cur_h, buf = [], None, []
    for ln in open(path, encoding="utf-8").read().split("\n"):
        m = re.match(r"^(#{2,3})\s+(.*)$", ln)
        if m:
            if cur_h is not None:
                out.append((cur_h, "\n".join(buf).strip()))
            cur_h, buf = m.group(2).strip(), []
        elif cur_h is not None:
            buf.append(ln)
    if cur_h is not None:
        out.append((cur_h, "\n".join(buf).strip()))
    return out


def parse_skill(path, position):
    """解析单个 SKILL.md：从 frontmatter 取 title/name 与 summary。"""
    txt = open(path, encoding="utf-8").read()
    name = os.path.basename(os.path.dirname(path))
    desc = "（无说明）"
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", txt, re.S)
    if fm:
        for line in fm.group(1).split("\n"):
            kv = re.match(r"^(title|name|summary)\s*:\s*(.*)$", line)
            if kv:
                val = kv.group(2).strip().strip('"').strip("'")
                if kv.group(1) in ("title", "name"):
                    name = val
                elif kv.group(1) == "summary":
                    desc = val
    if desc == "（无说明）":
        for ln in txt.split("\n"):
            s = ln.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                desc = s[:80]
                break
    return name, desc, position, path


# ---------------- 数据库：建表 + 示例数据 ----------------
def build_rrule(ftype, hour, minute, byday="MO", bymonthday=1):
    hh, mm = int(hour), int(minute)
    if ftype == "每日":
        return f"FREQ=DAILY;BYHOUR={hh};BYMINUTE={mm}"
    if ftype == "每周":
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={hh};BYMINUTE={mm}"
    if ftype == "每月":
        return f"FREQ=MONTHLY;BYMONTHDAY={int(bymonthday)};BYHOUR={hh};BYMINUTE={mm}"
    return ""  # 一次性（用 scheduled_at）


def compute_next_run(rrule, schedule_type, scheduled_at):
    """尽力算出下一次运行时间（毫秒），失败返回 None（交给运行时补算）。"""
    try:
        now = datetime.datetime.now()
        if (schedule_type or "").lower() == "once":
            if scheduled_at:
                dt = datetime.datetime.strptime(scheduled_at[:16], "%Y-%m-%dT%H:%M")
                return int(dt.timestamp() * 1000)
            return None
        parts = {}
        for p in (rrule or "").split(";"):
            if "=" in p:
                k, v = p.split("=", 1)
                parts[k] = v
        hh = int(parts.get("BYHOUR", 9))
        mm = int(parts.get("BYMINUTE", 0))
        freq = parts.get("FREQ")
        if freq == "DAILY":
            cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= now:
                cand += datetime.timedelta(days=1)
            return int(cand.timestamp() * 1000)
        if freq == "WEEKLY":
            wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}.get(
                parts.get("BYDAY"), now.weekday())
            days_ahead = (wd - now.weekday()) % 7
            base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if days_ahead == 0 and base <= now:
                days_ahead = 7
            cand = (now + datetime.timedelta(days=days_ahead)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            return int(cand.timestamp() * 1000)
        if freq == "MONTHLY":
            dom = int(parts.get("BYMONTHDAY", 1))
            y, m = now.year, now.month
            try:
                cand = now.replace(day=dom, hour=hh, minute=mm, second=0, microsecond=0)
            except ValueError:
                cand = now.replace(day=28, hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= now:
                if m < 12:
                    cand = now.replace(year=y, month=m + 1, day=1) - datetime.timedelta(days=1)
                    cand = cand.replace(day=dom, hour=hh, minute=mm, second=0, microsecond=0)
                else:
                    cand = now.replace(year=y + 1, month=1, day=dom, hour=hh, minute=mm,
                                       second=0, microsecond=0)
            return int(cand.timestamp() * 1000)
    except Exception as e:
        print(f"[warn] 计算 next_run_at 失败: {e}")
    return None


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
    os.makedirs(SKILLS_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY, name TEXT, prompt TEXT, status TEXT,
            schedule_type TEXT, rrule TEXT, scheduled_at TEXT, cwds TEXT,
            skills_json TEXT, connector_ids_json TEXT, owner_user_id TEXT,
            owner_status TEXT, permission_mode TEXT, model_id TEXT,
            created_at INTEGER, updated_at INTEGER, deleted_at INTEGER,
            next_run_at INTEGER)"""
    )
    if con.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 0:
        seed_automations(con)
    con.commit()
    con.close()
    return DB


def seed_automations(con):
    now = int(datetime.datetime.now().timestamp() * 1000)
    samples = [
        ("每日 AI 早报", "每个工作日上午自动整理 AI 领域重要进展，生成简报。",
         "每日", "已暂停", 9, 0, "MO", 1, None),
        ("每周复盘", "每周日晚自动汇总本周亮点与待办，生成周报。",
         "每周", "已暂停", 20, 0, "SU", 1, None),
        ("每月账单核对", "每月 1 日检查本地财务记录并提醒核对。",
         "每月", "已暂停", 8, 0, "MO", 1, None),
    ]
    for name, prompt, ftype, status, hh, mm, wd, md, sa in samples:
        st = "ACTIVE" if status == "运行中" else "PAUSED"
        stype = "once" if ftype == "一次性" else "recurring"
        rrule = "" if stype == "once" else build_rrule(ftype, hh, mm, wd, md)
        aid = "automation-seed-" + str(now) + "-" + re.sub(r"\W", "", name)
        nxt = compute_next_run(rrule, stype, sa)
        con.execute(
            "INSERT OR IGNORE INTO automations "
            "(id,name,prompt,status,schedule_type,rrule,scheduled_at,cwds,skills_json,"
            "connector_ids_json,owner_user_id,owner_status,permission_mode,model_id,"
            "created_at,updated_at,deleted_at,next_run_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (aid, name, prompt, st, stype, rrule, sa,
             json.dumps(["."], ensure_ascii=False), "[]", "[]",
             None, "legacy_unassigned", "fullAccess", "auto", now, now, nxt),
        )


# ---------------- 采集（带写回 id） ----------------
def gather_automations():
    rows = []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            "SELECT id,name,prompt,status,schedule_type,rrule,scheduled_at,next_run_at "
            "FROM automations WHERE deleted_at IS NULL OR deleted_at=0"
        )
        for r in cur.fetchall():
            rows.append({
                "id": r["id"],
                "名称": r["name"],
                "说明": short_desc(r["prompt"]),
                "频率": rrule_to_cn(r["rrule"], r["schedule_type"], r["scheduled_at"]),
                "状态": "运行中" if (r["status"] or "").upper() == "ACTIVE" else "已暂停",
                "下次运行": fmt_ts(r["next_run_at"]),
            })
        con.close()
    except Exception as e:
        print(f"[warn] 读取 automations 失败: {e}")
    return rows


def gather_memory():
    rows = []
    for heading, body in parse_sections(MEMORY_PATH):
        if not body:
            continue
        rows.append({"id": heading, "主题": heading, "内容": body[:1500],
                     "来源": os.path.relpath(MEMORY_PATH, DATA_DIR)})
    return rows


def load_override():
    if os.path.exists(OVERRIDE_PATH):
        try:
            return json.load(open(OVERRIDE_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def gather_skills():
    override = load_override()
    rows = []
    for p in sorted(glob.glob(os.path.join(SKILLS_DIR, "*", "SKILL.md"))):
        try:
            name, desc, pos, path = parse_skill(p, "本地")
            cn = override.get(name) or desc or "（无说明）"
            rows.append({"id": path, "名称": name, "功能说明": cn, "位置": pos, "路径": path})
        except Exception as e:
            print(f"[warn] 解析 skill 失败 {p}: {e}")
    return rows


def gather_highlights():
    if not os.path.exists(HIGHLIGHTS_PATH):
        return []
    rows = []
    for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().splitlines():
        m = re.match(r"^\s*-\s*\[id=(\S+)\]\s*\[(\S+)\]\s*(.*)$", ln)
        if not m:
            continue
        hid, htype, content = m.group(1), m.group(2), m.group(3).strip()
        if htype not in VALID_TYPES:
            htype = "其他"
        dm = re.match(r"HL-(\d{4})(\d{2})(\d{2})", hid)
        date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else ""
        rows.append({"id": hid, "亮点ID": hid, "日期": date, "类型": htype, "内容": content[:1500]})
    return rows


def collect():
    return {
        "automations": gather_automations(),
        "memory": gather_memory(),
        "skills": gather_skills(),
        "highlights": gather_highlights(),
    }


def compute_filters(data):
    return {
        "automations": [{"field": "状态", "label": "状态",
                         "values": sorted({it.get("状态", "") for it in data["automations"] if it.get("状态")})}],
        "skills": [{"field": "位置", "label": "位置",
                    "values": sorted({it.get("位置", "") for it in data["skills"] if it.get("位置")})}],
        "highlights": [{"field": "类型", "label": "类型",
                        "values": [t for t in VALID_TYPES if t in {it.get("类型", "") for it in data["highlights"]}]},
                       {"field": "__date_range__", "label": "日期范围", "values": ["近7天", "近30天"]}],
        "memory": [],
    }


# ---------------- 写回（真实改写本地文件 / 数据库） ----------------
def update_automation(aid, field, value):
    if field == "名称":
        col = "name"
    elif field == "状态":
        col = "status"
        value = "ACTIVE" if value == "运行中" else "PAUSED"
    else:
        raise ValueError(f"定时任务不支持修改字段：{field}")
    backup(DB)
    con = sqlite3.connect(DB)
    con.execute(f"UPDATE automations SET {col}=?, updated_at=? WHERE id=?",
                (value, int(datetime.datetime.now().timestamp() * 1000), aid))
    con.commit()
    con.close()
    return DB


def rewrite_md_section(path, heading, new_body):
    backup(path)
    lines = open(path, encoding="utf-8").read().split("\n")
    out, i, n, replaced = [], 0, len(lines), False
    while i < n:
        ln = lines[i]
        m = re.match(r"^(#{2,3})\s+(.*)$", ln)
        if m and m.group(2).strip() == heading:
            out.append(ln)
            j = i + 1
            while j < n and not re.match(r"^#{1,3}\s", lines[j]):
                j += 1
            for bl in new_body.split("\n"):
                out.append(bl)
            i = j
            replaced = True
            continue
        out.append(ln)
        i += 1
    if not replaced:
        out.append("")
        out.append("## " + heading)
        for bl in new_body.split("\n"):
            out.append(bl)
    open(path, "w", encoding="utf-8").write("\n".join(out))
    return path


def update_memory(topic, content):
    return rewrite_md_section(MEMORY_PATH, topic, content)


def update_skill(path, value):
    name = os.path.basename(os.path.dirname(path))
    override = load_override()
    override[name] = value
    backup(OVERRIDE_PATH)
    os.makedirs(os.path.dirname(OVERRIDE_PATH), exist_ok=True)
    json.dump(override, open(OVERRIDE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return OVERRIDE_PATH


def update_highlight_line(hid, field, value):
    backup(HIGHLIGHTS_PATH)
    out, changed = [], False
    pat = re.compile(r"^(\s*-\s*\[id=" + re.escape(hid) + r"\]\s*\[)(\S+)(\]\s*)(.*)$")
    for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().split("\n"):
        m = pat.match(ln)
        if m:
            if field == "类型":
                ln = m.group(1) + value + m.group(3) + m.group(4)
            elif field == "内容":
                ln = m.group(1) + m.group(2) + m.group(3) + value
            changed = True
        out.append(ln)
    if changed:
        open(HIGHLIGHTS_PATH, "w", encoding="utf-8").write("\n".join(out))
    return HIGHLIGHTS_PATH, changed


def delete_highlight_line(hid):
    backup(HIGHLIGHTS_PATH)
    pat = re.compile(r"^\s*-\s*\[id=" + re.escape(hid) + r"\]")
    out = [ln for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().split("\n") if not pat.match(ln)]
    open(HIGHLIGHTS_PATH, "w", encoding="utf-8").write("\n".join(out))
    return HIGHLIGHTS_PATH


def delete_automation(aid):
    """软删除：置 deleted_at，使其从工作台视图隐藏（可经 .bak + 数据库恢复）。"""
    backup(DB)
    ts = int(datetime.datetime.now().timestamp())
    con = sqlite3.connect(DB)
    con.execute("UPDATE automations SET deleted_at=?, updated_at=? WHERE id=?",
                (ts, ts, aid))
    con.commit()
    con.close()
    return DB


def delete_memory(topic):
    """删除 MEMORY.md 中对应的 ## 小节。"""
    backup(MEMORY_PATH)
    lines = open(MEMORY_PATH, encoding="utf-8").read().split("\n")
    out, i, n, removed = [], 0, len(lines), False
    while i < n:
        ln = lines[i]
        m = re.match(r"^(#{2,3})\s+(.*)$", ln)
        if m and m.group(2).strip() == topic:
            j = i + 1
            while j < n and not re.match(r"^#{1,3}\s", lines[j]):
                j += 1
            i = j
            removed = True
            continue
        out.append(ln)
        i += 1
    if not removed:
        raise ValueError("未找到对应的记忆小节：" + topic)
    open(MEMORY_PATH, "w", encoding="utf-8").write("\n".join(out))
    return MEMORY_PATH


def apply_updates(sec, rid, values):
    """按字段批量写回（一次修改多个字段）。返回最后写回的文件路径。"""
    last = None
    for field, value in values.items():
        if sec == "automations":
            last = update_automation(rid, field, value)
        elif sec == "memory":
            if field != "内容":
                continue
            last = update_memory(rid, value)
        elif sec == "skills":
            if field != "功能说明":
                continue
            last = update_skill(rid, value)
        elif sec == "highlights":
            if field not in ("类型", "内容"):
                continue
            last, _ = update_highlight_line(rid, field, value)
    if last is None:
        raise ValueError("没有可更新的字段")
    return last


def add_highlight(htype, content):
    if htype not in VALID_TYPES:
        htype = "其他"
    now = datetime.datetime.now()
    hid = "HL-" + now.strftime("%Y%m%d-%H%M-") + "%03d" % (now.microsecond // 1000)
    line = f"- [id={hid}] [{htype}] {content}"
    backup(HIGHLIGHTS_PATH)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HIGHLIGHTS_PATH, "a", encoding="utf-8") as f:
        f.write(("" if os.path.exists(HIGHLIGHTS_PATH) and os.path.getsize(HIGHLIGHTS_PATH) > 0 else "") + line + "\n")
    return HIGHLIGHTS_PATH, hid


def add_automation(name, prompt, ftype="每日", status="运行中", hour=9, minute=0,
                   byday="MO", bymonthday=1, scheduled_at=None):
    """在本地 automations.db 新建一条定时任务。返回 (db_path, new_id)。"""
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name:
        raise ValueError("定时任务名称不能为空")
    if not prompt:
        raise ValueError("定时任务说明（prompt）不能为空")
    st = "ACTIVE" if status == "运行中" else "PAUSED"
    schedule_type = "once" if ftype == "一次性" else "recurring"
    rrule = "" if schedule_type == "once" else build_rrule(ftype, hour, minute, byday, bymonthday)
    sa = scheduled_at if schedule_type == "once" else None
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    aid = "automation-" + str(now_ms)
    next_run = compute_next_run(rrule, schedule_type, sa)
    backup(DB)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO automations "
        "(id,name,prompt,status,schedule_type,rrule,scheduled_at,cwds,skills_json,"
        "connector_ids_json,owner_user_id,owner_status,permission_mode,model_id,"
        "created_at,updated_at,deleted_at,next_run_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
        (aid, name, prompt, st, schedule_type, rrule, sa,
         json.dumps(["."], ensure_ascii=False), "[]", "[]",
         None, "legacy_unassigned", "fullAccess", "auto", now_ms, now_ms, next_run),
    )
    con.commit()
    con.close()
    return DB, aid


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, html=None):
        self.send_response(code)
        if html is not None:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            if os.path.exists(HTML_PATH):
                self._send(200, html=open(HTML_PATH, encoding="utf-8").read())
            else:
                self._send(404, {"error": "找不到 workbench_app.html"})
        elif self.path == "/api/data":
            data = collect()
            self._send(200, {"ok": True, "data": data,
                             "counts": {k: len(v) for k, v in data.items()},
                             "filters": compute_filters(data)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/api/update":
                sec, rid, field, value = body["section"], body["id"], body["field"], body.get("value", "")
                if sec == "automations":
                    path = update_automation(rid, field, value)
                elif sec == "memory":
                    if field != "内容":
                        raise ValueError("记忆仅支持修改内容")
                    path = update_memory(rid, value)
                elif sec == "skills":
                    if field != "功能说明":
                        raise ValueError("Skill 仅支持修改功能说明")
                    path = update_skill(rid, value)
                elif sec == "highlights":
                    if field not in ("类型", "内容"):
                        raise ValueError("每日亮点仅支持修改类型/内容")
                    path, _ = update_highlight_line(rid, field, value)
                else:
                    raise ValueError("未知数据区")
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/update_multi":
                sec, rid, values = body["section"], body["id"], body.get("values", {})
                path = apply_updates(sec, rid, values)
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/add":
                htype, content = body.get("type", "其他"), body.get("content", "").strip()
                if not content:
                    raise ValueError("内容不能为空")
                path, hid = add_highlight(htype, content)
                self._send(200, {"ok": True, "path": path, "id": hid})
            elif self.path == "/api/add_automation":
                b = body
                path, aid = add_automation(
                    b.get("name", ""), b.get("prompt", ""), b.get("ftype", "每日"),
                    b.get("status", "运行中"), int(b.get("hour", 9)), int(b.get("minute", 0)),
                    b.get("byday", "MO"), int(b.get("bymonthday", 1)), b.get("scheduled_at"))
                self._send(200, {"ok": True, "path": path, "id": aid})
            elif self.path == "/api/delete":
                sec, rid = body["section"], body["id"]
                if sec == "highlights":
                    path = delete_highlight_line(rid)
                elif sec == "automations":
                    path = delete_automation(rid)
                elif sec == "memory":
                    path = delete_memory(rid)
                elif sec == "skills":
                    raise ValueError("Skill 不支持删除（避免误删文件），如需移除请到 data/skills 手动处理")
                else:
                    raise ValueError("未知数据区")
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/shutdown":
                self._send(200, {"ok": True})
                os._exit(0)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})


def find_free_port(start, host="127.0.0.1", max_tries=50):
    """从 start 起找第一个未被占用的端口，避免端口冲突导致起不来。"""
    for p in range(start, start + max_tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            return p
        except OSError:
            pass
        finally:
            s.close()
    return None


def main():
    init_db()  # 首次运行自动建库 + 写入示例数据
    port = find_free_port(PORT)
    if port is None:
        print(f"在 {PORT}~{PORT+49} 区间找不到可用端口，请检查是否有程序占满端口后重试。")
        return
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    try:
        with open(os.path.join(HERE, "workbench_addr.txt"), "w", encoding="utf-8") as f:
            f.write(url)
    except Exception:
        pass
    print(f"AI 工作台已启动：{url}  （仅本机可访问，Ctrl+C 退出）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
