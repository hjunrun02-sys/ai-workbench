#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 工作台 —— 本地可编辑服务（飞书表格风格，纯标准库，零外部依赖）

为什么需要它：
  纯静态 HTML（双击打开）在浏览器沙箱里写不了本地文件。要做到
  “编辑并确认改到本地文件”，必须有一个本地后端。本服务只绑定
  127.0.0.1（不暴露到外网），负责：
    - GET  /                  返回飞书表格风格前端 workbench_app.html
    - GET  /api/data          返回 6 类数据（定时任务/记忆/Skill/每日亮点/对话记忆）+ Git 仓库 + 计数 + 可筛选维度
    - GET  /api/processes     列出本机运行进程（名称/PID/用户/CPU/内存/状态，受保护进程标记 protected）
    - POST /api/update        修改某条记录单字段
    - POST /api/update_multi  一次修改多个字段（含定时任务多字段编辑）
    - POST /api/update_category  修改某条记录的分类
    - POST /api/add           新增每日亮点
    - POST /api/add_cm        新增对话记忆
    - POST /api/add_automation  新建定时任务
    - POST /api/delete        删除（亮点/定时任务/记忆小节/对话记忆）
    - GET  /api/skill_content 读取某 Skill 的 SKILL.md 全文
    - GET  /api/git_repos     扫描并列出本地 Git 仓库（路径/分支/状态/远程）
    - POST /api/git_action    Git 操作（fetch/pull/checkout/clone/push，远程操作自动注入令牌）
    - GET  /api/tokens        读取 GitHub/Gitee 令牌脱敏摘要（不回显明文）
    - POST /api/tokens        保存/删除平台令牌（写入本地 .git/config，不进仓库）
    - GET  /api/backups       列出所有 .bak 备份
    - POST /api/restore       一键还原某个 .bak
    - POST /api/kill_process  结束指定 PID 的进程（服务端二次复核受保护进程，禁止误杀系统/自身）
    - POST /api/shutdown      关闭本地服务

数据来源（用户选择「直接读写真实 WorkBuddy」，故核心资产直连活数据）：
    定时任务  -> ~/.workbuddy/workbuddy.db（真实 WorkBuddy 自动化库，可读写）
    记忆      -> ~/.workbuddy/MEMORY.md（全局长期记忆，可读写，每次写前 .bak 备份）
    Skill     -> ~/.workbuddy/skills/（真实用户级 Skill，查看真实内容；编辑写覆盖层 data/skill_override.json）
工作台自身数据（无真实对应物的概念，留在本地 ./data，不上云）：
    每日亮点  -> data/highlights.md
    对话记忆  -> data/conversation_memory.md
    分类      -> data/categories.json（统一分类索引）
    Git 仓库  -> 本地真实 .git 目录（只读扫描 + 本地 git 子进程操作，远程操作注入令牌）
不依赖任何第三方运行时；写真实文件/库前均自动 .bak 备份，写坏可还原。
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
import subprocess
import csv
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 数据目录（人人可装：默认本地 ./data，可覆盖） ----------------
DATA_DIR = os.environ.get("AI_WORKBENCH_DATA") or os.path.join(HERE, "data")
# 工作台自身本地数据（无真实 WorkBuddy 对应物的概念：亮点/对话记忆/分类索引/技能覆盖层）
HIGHLIGHTS_PATH = os.path.join(DATA_DIR, "highlights.md")
OVERRIDE_PATH = os.path.join(DATA_DIR, "skill_override.json")
CATEGORIES_PATH = os.path.join(DATA_DIR, "categories.json")
CONVERSATION_PATH = os.path.join(DATA_DIR, "conversation_memory.md")
GIT_ROOTS_FILE = os.path.join(DATA_DIR, "git_roots.txt")
# 真实 WorkBuddy 数据（用户选择「直接读写真实 WorkBuddy」：显示与管理均指向活数据）
WB_HOME = os.path.expanduser("~/.workbuddy")
MEMORY_PATH = os.path.join(WB_HOME, "MEMORY.md")      # 全局长期记忆（真实，可读写）
SKILLS_DIR = os.path.join(WB_HOME, "skills")          # 用户级 Skill（真实，查看真实内容）
DB = os.path.join(WB_HOME, "workbuddy.db")            # 定时任务（真实，可读写）
HTML_PATH = os.path.join(HERE, "workbench_app.html")
PORT = 8765

VALID_TYPES = ["决策", "学习", "洞察", "任务", "其他"]
DEFAULT_CATEGORIES = ["自媒体", "电商", "AI技术", "运营", "其他"]
WEEK_MAP = {"MO": "一", "TU": "二", "WE": "三", "TH": "四", "FR": "五", "SA": "六", "SU": "日"}
GIT_DEPTH = 4  # 仓库扫描最大递归深度，避免扫全盘过慢


# ---------------- 备份 ----------------
def backup(path):
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")


# ---------------- 分类（统一索引） ----------------
def load_categories():
    if os.path.exists(CATEGORIES_PATH):
        try:
            return json.load(open(CATEGORIES_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_categories(d):
    backup(CATEGORIES_PATH)
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(d, open(CATEGORIES_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def category_of(section, rid):
    return load_categories().get(section, {}).get(rid, "")


def update_category(section, rid, category):
    d = load_categories()
    d.setdefault(section, {})
    if category:
        d[section][rid] = category
    else:
        d[section].pop(rid, None)
    save_categories(d)
    return CATEGORIES_PATH


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


def get_skill_content(path):
    """读取 SKILL.md 全文，供前端「查看具体内容」展示。"""
    if not os.path.isfile(path):
        return None
    # 防止越权读取仓库外文件
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(SKILLS_DIR)]) != os.path.abspath(SKILLS_DIR):
        return None
    return open(path, encoding="utf-8").read()


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
    ensure_git_roots()
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
    # 仅当使用「自带示例库」(data/automations.db) 且为空时才注入示例；
    # 真实 WorkBuddy 库即使为空也不注入示例，避免污染活数据。
    if DB == os.path.join(DATA_DIR, "automations.db"):
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


def ensure_git_roots():
    if not os.path.exists(GIT_ROOTS_FILE):
        defaults = [
            os.path.join(os.path.expanduser("~"), "repos"),
            os.path.join(os.path.expanduser("~"), "projects"),
            os.path.dirname(HERE),
        ]
        with open(GIT_ROOTS_FILE, "w", encoding="utf-8") as f:
            f.write("# 每行一个本地仓库根目录（扫描其下的 .git 仓库）\n")
            for d in defaults:
                f.write(d.replace("\\", "/") + "\n")


def load_git_roots():
    if not os.path.exists(GIT_ROOTS_FILE):
        return []
    out = []
    for ln in open(GIT_ROOTS_FILE, encoding="utf-8").read().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        out.append(ln)
    return out


def run_git(cmd):
    """调本地 git 子进程，返回 {ok,out,err,code}。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25, shell=False)
        return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr, "code": r.returncode}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e), "code": -1}


# ---------------- 采集（带写回 id 与分类） ----------------
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
                "说明全文": r["prompt"],
                "分类": category_of("automations", r["id"]),
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
        rows.append({"id": heading, "主题": heading, "内容": body,
                     "来源": os.path.relpath(MEMORY_PATH, DATA_DIR),
                     "分类": category_of("memory", heading)})
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
            rows.append({"id": path, "名称": name, "功能说明": cn, "位置": pos,
                         "路径": path, "分类": category_of("skills", name)})
        except Exception as e:
            print(f"[warn] 解析 skill 失败 {p}: {e}")
    return rows


def gather_highlights():
    if not os.path.exists(HIGHLIGHTS_PATH):
        return []
    rows = []
    pat = re.compile(r"^\s*-\s*\[id=(\S+)\]\s*\[(\S+)\]\s*(?:\[分类=([^\]]*)\]\s*)?(.*)$")
    for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().splitlines():
        m = pat.match(ln)
        if not m:
            continue
        hid, htype, hcat, content = m.group(1), m.group(2), m.group(3) or "", m.group(4).strip()
        if htype not in VALID_TYPES:
            htype = "其他"
        dm = re.match(r"HL-(\d{4})(\d{2})(\d{2})", hid)
        date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else ""
        rows.append({"id": hid, "亮点ID": hid, "日期": date, "类型": htype,
                     "分类": hcat, "内容": content[:1500]})
    return rows


def gather_conversation_memory():
    if not os.path.exists(CONVERSATION_PATH):
        return []
    rows = []
    pat = re.compile(r"^\s*-\s*\[id=(\S+)\]\s*\[(\S+)\]\s*(?:\[分类=([^\]]*)\]\s*)?(.*)$")
    for ln in open(CONVERSATION_PATH, encoding="utf-8").read().splitlines():
        m = pat.match(ln)
        if not m:
            continue
        cid, ctype, ccat, content = m.group(1), m.group(2), m.group(3) or "", m.group(4).strip()
        if ctype not in VALID_TYPES:
            ctype = "其他"
        dm = re.match(r"CM-(\d{4})(\d{2})(\d{2})", cid)
        date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else ""
        rows.append({"id": cid, "ID": cid, "日期": date, "类型": ctype,
                     "分类": ccat, "内容": content[:1500]})
    return rows


def gather_git_repos():
    """扫描 git_roots 下所有 .git 仓库（限制深度），返回只读信息。"""
    repos = []
    for root in load_git_roots():
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth > GIT_DEPTH:
                dirnames[:] = []
                continue
            if ".git" in dirnames:
                info = describe_git(dirpath)
                if info:
                    repos.append(info)
                dirnames[:] = []  # 仓库内部不再向下扫
    return repos


def describe_git(repo):
    try:
        branch = run_git(["git", "-C", repo, "branch", "--show-current"]).get("out", "").strip()
        st = run_git(["git", "-C", repo, "status", "--porcelain"])
        changes = len([l for l in st.get("out", "").splitlines() if l.strip()])
        remotes = parse_remotes(run_git(["git", "-C", repo, "remote", "-v"]).get("out", ""))
        return {"id": repo, "路径": repo, "分支": branch or "(分离头指针)",
                "状态": "有改动" if changes else "干净", "改动数": changes, "远程": remotes}
    except Exception:
        return None


def parse_remotes(text):
    out = []
    for ln in text.splitlines():
        m = re.match(r"^(\S+)\s+(\S+)\s+\((\S+)\)$", ln.strip())
        if m:
            out.append({"name": m.group(1), "url": m.group(2), "type": m.group(3)})
    return out


def collect():
    return {
        "automations": gather_automations(),
        "memory": gather_memory(),
        "skills": gather_skills(),
        "highlights": gather_highlights(),
        "conversation_memory": gather_conversation_memory(),
        "git_repos": gather_git_repos(),
    }


def compute_filters(data):
    return {
        "automations": [{"field": "状态", "label": "状态",
                         "values": sorted({it.get("状态", "") for it in data["automations"] if it.get("状态")})},
                        {"field": "分类", "label": "分类", "values": DEFAULT_CATEGORIES}],
        "memory": [{"field": "分类", "label": "分类", "values": DEFAULT_CATEGORIES}],
        "skills": [{"field": "位置", "label": "位置",
                    "values": sorted({it.get("位置", "") for it in data["skills"] if it.get("位置")})},
                   {"field": "分类", "label": "分类", "values": DEFAULT_CATEGORIES}],
        "highlights": [{"field": "类型", "label": "类型",
                        "values": [t for t in VALID_TYPES if t in {it.get("类型", "") for it in data["highlights"]}]},
                       {"field": "分类", "label": "分类", "values": DEFAULT_CATEGORIES},
                       {"field": "__date_range__", "label": "日期范围", "values": ["近7天", "近30天"]}],
        "conversation_memory": [{"field": "类型", "label": "类型",
                                 "values": [t for t in VALID_TYPES if t in {it.get("类型", "") for it in data["conversation_memory"]}]},
                                {"field": "分类", "label": "分类", "values": DEFAULT_CATEGORIES}],
        "git_repos": [{"field": "状态", "label": "状态", "values": ["有改动", "干净"]}],
    }


# ---------------- 进程管理 ----------------
# 受保护进程（不在工作台内提供「结束」按钮，避免误杀系统/自身导致故障）
PROTECTED_NAMES = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "smss.exe", "winlogon.exe", "explorer.exe", "dwm.exe",
    "fontdrvhost.exe", "audiodg.exe", "registry", "memory compression",
    "searchui.exe", "shellexperiencehost.exe", "runtimebroker.exe",
}
PROTECTED_PIDS = set()


def _protect_self():
    """把工作台自身进程及其父进程标记为受保护，禁止在工作台里结束自己。"""
    PROTECTED_PIDS.add(os.getpid())
    try:
        ppid = os.getppid()
        if ppid:
            PROTECTED_PIDS.add(ppid)
    except Exception:
        pass


def _parse_mem_kb(s):
    s = (s or "").strip().replace("K", "").replace("M", "").replace(",", "").replace(" ", "")
    try:
        return int(s)
    except Exception:
        return 0


def _human_mem(kb):
    if not kb:
        return "0"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.0f} MB"
    return f"{kb} KB"


def _split_csv(line):
    try:
        return next(csv.reader(io.StringIO(line)))
    except Exception:
        return [line]


def gather_processes():
    """跨平台列出进程（零依赖，调用系统命令）。返回列表，含 protected 标记。"""
    rows = []
    is_win = (os.name == "nt")
    try:
        if is_win:
            out = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=25
            ).stdout or ""
            for ln in out.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                p = _split_csv(ln)
                # 列序：Image Name,PID,Session Name,Session Number,Mem Usage
                if len(p) < 5:
                    continue
                name, pid, mem = p[0], p[1], p[4]
                rows.append(_mk_proc(pid, name, "", "", mem, ""))
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid,user,%cpu,%mem,comm"],
                capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=25
            ).stdout or ""
            for ln in out.splitlines()[1:]:
                ln = ln.strip()
                if not ln:
                    continue
                parts = ln.split(None, 4)
                if len(parts) < 5:
                    continue
                pid, user, cpu, mem, name = parts[0], parts[1], parts[2], parts[3], parts[4]
                rows.append(_mk_proc(pid, name, user, cpu + "%", mem + "%", "运行"))
    except Exception:
        return rows
    return rows


def _mk_proc(pid, name, user, cpu, mem, status):
    try:
        pid_i = int(pid)
    except Exception:
        pid_i = -1
    nm = (name or "").lower().strip()
    protected = False
    reason = ""
    if pid_i in PROTECTED_PIDS:
        protected = True
        reason = "工作台自身进程"
    elif nm in PROTECTED_NAMES:
        protected = True
        reason = "系统关键进程"
    mem_raw = (mem or "").strip()
    if mem_raw.endswith("%"):
        mem_human = mem_raw          # POSIX 的 ps 给的是百分比
    else:
        mem_human = _human_mem(_parse_mem_kb(mem_raw))
    return {
        "pid": pid_i, "name": name, "user": user, "cpu": cpu,
        "mem": mem, "mem_human": mem_human,
        "status": status, "protected": protected, "reason": reason,
    }


def kill_process(pid):
    """结束指定进程。服务端二次复核：受保护进程（系统关键/自身）一律拒绝。"""
    try:
        pid = int(pid)
    except Exception:
        return {"ok": False, "err": "pid 无效"}
    if pid in PROTECTED_PIDS:
        return {"ok": False, "err": "不能结束工作台自身进程"}
    # 复核受保护名单（防止前端绕过）
    for p in gather_processes():
        if p["pid"] == pid:
            if p["protected"]:
                return {"ok": False, "err": f"受保护进程（{p.get('reason', '')}），不能结束"}
            break
    is_win = (os.name == "nt")
    try:
        if is_win:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=15)
        else:
            r = subprocess.run(["kill", "-9", str(pid)],
                               capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=15)
        ok = r.returncode == 0
        return {"ok": ok, "out": (r.stdout or "").strip(), "err": (r.stderr or "").strip()}
    except Exception as e:
        return {"ok": False, "err": str(e)}


# ---------------- 写回（真实改写本地文件 / 数据库） ----------------
def update_automation(aid, field, value):
    """单字段更新（兼容旧调用：名称/状态）。"""
    if field == "名称":
        col = "name"
    elif field == "状态":
        col = "status"
        value = "ACTIVE" if value == "运行中" else "PAUSED"
    else:
        raise ValueError(f"定时任务不支持单字段修改：{field}（请用多字段编辑）")
    backup(DB)
    con = sqlite3.connect(DB)
    con.execute(f"UPDATE automations SET {col}=?, updated_at=? WHERE id=?",
                (value, int(datetime.datetime.now().timestamp() * 1000), aid))
    con.commit()
    con.close()
    return DB


def update_automation_fields(aid, fields):
    """多字段更新（R1：名称/状态/频率/说明 一起改）。"""
    sets, params = [], []
    now = int(datetime.datetime.now().timestamp() * 1000)
    if "名称" in fields:
        sets.append("name=?"); params.append(fields["名称"])
    if "状态" in fields:
        sets.append("status=?"); params.append("ACTIVE" if fields["状态"] == "运行中" else "PAUSED")
    if "说明" in fields:
        sets.append("prompt=?"); params.append(fields["说明"])
    ftype = fields.get("频率类型")
    if ftype:
        if ftype == "一次性":
            scheduled_at = fields.get("执行时间")
            sets.append("schedule_type=?"); params.append("once")
            sets.append("rrule=?"); params.append("")
            sets.append("scheduled_at=?"); params.append(scheduled_at)
            nxt = compute_next_run("", "once", scheduled_at)
        else:
            hh = int(fields.get("小时", 9)); mm = int(fields.get("分钟", 0))
            byday = fields.get("星期", "MO"); md = int(fields.get("每月几号", 1))
            rrule = build_rrule(ftype, hh, mm, byday, md)
            sets.append("schedule_type=?"); params.append("recurring")
            sets.append("rrule=?"); params.append(rrule)
            sets.append("scheduled_at=?"); params.append(None)
            nxt = compute_next_run(rrule, "recurring", None)
        sets.append("next_run_at=?"); params.append(nxt)
    if not sets:
        raise ValueError("没有可更新的字段")
    sets.append("updated_at=?"); params.append(now)
    params.append(aid)
    backup(DB)
    con = sqlite3.connect(DB)
    con.execute(f"UPDATE automations SET {','.join(sets)} WHERE id=?", params)
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
    pat = re.compile(r"^(\s*-\s*\[id=" + re.escape(hid) + r"\]\s*\[)(\S+)(\](?:\s*\[分类=[^\]]*\])?)(\s*)(.*)$")
    for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().split("\n"):
        m = pat.match(ln)
        if m:
            if field == "类型":
                ln = m.group(1) + value + m.group(3) + m.group(4) + m.group(5)
            elif field == "分类":
                ln = m.group(1) + m.group(2) + (f"] [分类={value}]" if value else "]") + m.group(4) + m.group(5)
            elif field == "内容":
                ln = m.group(1) + m.group(2) + m.group(3) + m.group(4) + value
            changed = True
        out.append(ln)
    if changed:
        open(HIGHLIGHTS_PATH, "w", encoding="utf-8").write("\n".join(out))
    return HIGHLIGHTS_PATH, changed


def update_conversation_line(cid, field, value):
    backup(CONVERSATION_PATH)
    out, changed = [], False
    pat = re.compile(r"^(\s*-\s*\[id=" + re.escape(cid) + r"\]\s*\[)(\S+)(\](?:\s*\[分类=[^\]]*\])?)(\s*)(.*)$")
    for ln in open(CONVERSATION_PATH, encoding="utf-8").read().split("\n"):
        m = pat.match(ln)
        if m:
            if field == "类型":
                ln = m.group(1) + value + m.group(3) + m.group(4) + m.group(5)
            elif field == "分类":
                ln = m.group(1) + m.group(2) + (f"] [分类={value}]" if value else "]") + m.group(4) + m.group(5)
            elif field == "内容":
                ln = m.group(1) + m.group(2) + m.group(3) + m.group(4) + value
            changed = True
        out.append(ln)
    if changed:
        open(CONVERSATION_PATH, "w", encoding="utf-8").write("\n".join(out))
    return CONVERSATION_PATH, changed


def delete_highlight_line(hid):
    backup(HIGHLIGHTS_PATH)
    pat = re.compile(r"^\s*-\s*\[id=" + re.escape(hid) + r"\]")
    out = [ln for ln in open(HIGHLIGHTS_PATH, encoding="utf-8").read().split("\n") if not pat.match(ln)]
    open(HIGHLIGHTS_PATH, "w", encoding="utf-8").write("\n".join(out))
    return HIGHLIGHTS_PATH


def delete_conversation_line(cid):
    backup(CONVERSATION_PATH)
    pat = re.compile(r"^\s*-\s*\[id=" + re.escape(cid) + r"\]")
    out = [ln for ln in open(CONVERSATION_PATH, encoding="utf-8").read().split("\n") if not pat.match(ln)]
    open(CONVERSATION_PATH, "w", encoding="utf-8").write("\n".join(out))
    return CONVERSATION_PATH


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
            last = update_automation_fields(rid, values)  # 多字段一次处理
            break
        elif sec == "memory":
            if field != "内容":
                continue
            last = update_memory(rid, value)
        elif sec == "skills":
            if field != "功能说明":
                continue
            last = update_skill(rid, value)
        elif sec == "highlights":
            if field not in ("类型", "内容", "分类"):
                continue
            last, _ = update_highlight_line(rid, field, value)
        elif sec == "conversation_memory":
            if field not in ("类型", "内容", "分类"):
                continue
            last, _ = update_conversation_line(rid, field, value)
    if last is None:
        raise ValueError("没有可更新的字段")
    return last


def add_highlight(htype, content, category=""):
    if htype not in VALID_TYPES:
        htype = "其他"
    now = datetime.datetime.now()
    hid = "HL-" + now.strftime("%Y%m%d-%H%M-") + "%03d" % (now.microsecond // 1000)
    line = f"- [id={hid}] [{htype}]" + (f" [分类={category}]" if category else "") + f" {content}"
    backup(HIGHLIGHTS_PATH)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HIGHLIGHTS_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return HIGHLIGHTS_PATH, hid


def add_conversation_memory(ctype, content, category=""):
    if ctype not in VALID_TYPES:
        ctype = "其他"
    now = datetime.datetime.now()
    cid = "CM-" + now.strftime("%Y%m%d-%H%M-") + "%03d" % (now.microsecond // 1000)
    line = f"- [id={cid}] [{ctype}]" + (f" [分类={category}]" if category else "") + f" {content}"
    backup(CONVERSATION_PATH)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONVERSATION_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return CONVERSATION_PATH, cid


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


# ---------------- 平台令牌管理（直接管理 GitHub/Gitee） ----------------
def parse_git_tokens():
    """从本地 .git/config 的 insteadOf 读回已配置的 GitHub/Gitee 令牌（仅判断是否已配置，不回显明文）。"""
    out = {"github": None, "gitee": None}
    res = run_git(["git", "-C", HERE, "config", "--get-regexp", r"^url\."])
    if not res["ok"]:
        return out
    for ln in res["out"].splitlines():
        if ".insteadof" not in ln.lower():
            continue
        m = re.match(r"^url\.(https://[^/]+/)\.insteadof\s+(\S+)$", ln, re.IGNORECASE)
        if not m:
            continue
        base, target = m.group(1), m.group(2)
        mm = re.match(r"https://([^@]+)@(.+)$", base)
        if not mm:
            continue
        userinfo, host = mm.group(1), mm.group(2)
        if "github.com" in target:
            out["github"] = {"token": userinfo, "username": ""}
        elif "gitee.com" in target:
            if ":" in userinfo:
                u, t = userinfo.split(":", 1)
                out["gitee"] = {"token": t, "username": u}
            else:
                out["gitee"] = {"token": userinfo, "username": ""}
    return out


def git_token_summary():
    """返回给前端的脱敏摘要（绝不回显明文令牌）。"""
    toks = parse_git_tokens()
    summary = {}
    for plat in ("github", "gitee"):
        t = toks.get(plat)
        if t and t.get("token"):
            tok = t["token"]
            summary[plat] = {
                "configured": True,
                "username": t.get("username", ""),
                "token_masked": ("****" + tok[-4:]) if len(tok) >= 4 else "****",
            }
        else:
            summary[plat] = {"configured": False, "username": "", "token_masked": ""}
    return summary


def write_git_token(platform, username, token):
    """把平台令牌写入本地 .git/config（insteadOf 形式，不进仓库、不被推送）。"""
    platform = (platform or "").lower()
    if platform not in ("github", "gitee"):
        raise ValueError("不支持的平台：" + str(platform))
    token = (token or "").strip()
    if not token:
        raise ValueError("令牌不能为空")
    if platform == "gitee" and not (username or "").strip():
        raise ValueError("Gitee 需要填写用户名（gitee 登录名）")
    rem = "https://github.com/" if platform == "github" else "https://gitee.com/"
    # 清掉该平台已有的 insteadOf（可能有多条/旧格式），避免重复
    res = run_git(["git", "-C", HERE, "config", "--get-regexp", r"^url\."])
    for ln in (res["out"] or "").splitlines():
        if rem in ln and ".insteadof" in ln.lower():
            key = ln.split()[0]
            run_git(["git", "-C", HERE, "config", "--unset", key])
    if platform == "github":
        base = f"https://{token}@github.com/"
    else:
        base = f"https://{(username or '').strip()}:{token}@gitee.com/"
    r = run_git(["git", "-C", HERE, "config", f"url.{base}.insteadOf", rem])
    if not r["ok"]:
        raise ValueError("写入令牌到 .git/config 失败：" + r["err"])
    return True


def delete_git_token(platform):
    """删除某平台在 .git/config 里的 insteadOf 令牌配置。"""
    platform = (platform or "").lower()
    rem = "https://github.com/" if platform == "github" else "https://gitee.com/"
    res = run_git(["git", "-C", HERE, "config", "--get-regexp", r"^url\."])
    removed = 0
    for ln in (res["out"] or "").splitlines():
        if rem in ln and ".insteadof" in ln.lower():
            key = ln.split()[0]
            run_git(["git", "-C", HERE, "config", "--unset", key])
            removed += 1
    return removed


def git_inject_flags():
    """生成本次 git 命令的令牌注入参数（-c url...insteadOf），让远程操作在沙箱非交互可用，且不污染任何 git 配置。"""
    toks = parse_git_tokens()
    flags = []
    gh = toks.get("github")
    if gh and gh.get("token"):
        flags += ["-c", f"url.https://{gh['token']}@github.com/.insteadOf=https://github.com/"]
    ge = toks.get("gitee")
    if ge and ge.get("token"):
        flags += ["-c", f"url.https://{ge['username']}:{ge['token']}@gitee.com/.insteadOf=https://gitee.com/"]
    return flags


# ---------------- Git 操作 ----------------
def safe_clone_dest(dest):
    dest = os.path.abspath(os.path.expanduser(dest))
    for root in load_git_roots():
        root = os.path.abspath(os.path.expanduser(root))
        try:
            if os.path.commonpath([dest, root]) == root:
                return True
        except Exception:
            continue
    return False


def git_action(body):
    action = body.get("action")
    path = body.get("path")
    inject = git_inject_flags()
    if action == "fetch":
        res = run_git(["git"] + inject + ["-C", path, "fetch", "--all"])
    elif action == "pull":
        res = run_git(["git"] + inject + ["-C", path, "pull"])
    elif action == "checkout":
        res = run_git(["git", "-C", path, "checkout", body.get("branch", "")])
    elif action == "clone":
        url = body.get("url", "")
        dest = os.path.expanduser(body.get("dest", ""))
        if not url or not dest:
            raise ValueError("clone 需要 url 与 dest")
        if not safe_clone_dest(dest):
            raise ValueError("目标路径不在允许的仓库根内（见 data/git_roots.txt）")
        res = run_git(["git"] + inject + ["clone", url, dest])
    elif action == "push":
        branch = body.get("branch") or run_git(
            ["git", "-C", path, "branch", "--show-current"]).get("out", "").strip()
        if not branch:
            raise ValueError("无法确定当前分支（可能处于分离头指针），无法推送")
        remotes = body.get("remotes") or []
        if not remotes:
            info = describe_git(path)
            remotes = [r["name"] for r in info.get("远程", [])] if info else []
        remotes = list(dict.fromkeys(remotes))  # 去重（git remote -v 会列 fetch/push 两行同名）
        if not remotes:
            raise ValueError("该仓库没有配置任何远程，无法推送")
        agg, ok_all = [], True
        for rm in remotes:
            cmd = ["git"] + inject + ["-C", path, "push", rm, branch]
            r = run_git(cmd)
            ok_all = ok_all and r["ok"]
            agg.append(f"[{rm}] {'成功' if r['ok'] else '失败'} (code {r['code']})\n{r['out']}{r['err']}".strip())
        res = {"ok": ok_all, "out": "\n".join(agg), "err": "", "code": 0 if ok_all else 1}
    else:
        raise ValueError("未知操作：" + str(action))
    return res


# ---------------- 备份管理（R11 误删恢复） ----------------
def list_backups():
    out = []
    for dirpath, _, filenames in os.walk(DATA_DIR):
        for fn in filenames:
            if fn.endswith(".bak"):
                p = os.path.join(dirpath, fn)
                st = os.stat(p)
                out.append({
                    "file": os.path.relpath(p, DATA_DIR),
                    "original": os.path.relpath(p[:-4], DATA_DIR),
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size": st.st_size,
                })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def restore_backup(file):
    """把 .bak 还原回原文件（file 必须在 DATA_DIR 内且以 .bak 结尾）。"""
    p = os.path.abspath(os.path.join(DATA_DIR, file))
    if not p.startswith(os.path.abspath(DATA_DIR)):
        raise ValueError("非法路径")
    if not p.endswith(".bak") or not os.path.exists(p):
        raise ValueError("找不到该备份文件")
    original = p[:-4]
    backup(original)  # 先备份当前原文件，避免覆盖丢失
    shutil.copy2(p, original)
    return original


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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            if os.path.exists(HTML_PATH):
                self._send(200, html=open(HTML_PATH, encoding="utf-8").read())
            else:
                self._send(404, {"error": "找不到 workbench_app.html"})
        elif parsed.path == "/api/data":
            data = collect()
            self._send(200, {"ok": True, "data": data,
                             "counts": {k: len(v) for k, v in data.items()},
                             "filters": compute_filters(data)})
        elif parsed.path == "/api/skill_content":
            q = urllib.parse.parse_qs(parsed.query)
            path = q.get("path", [""])[0]
            content = get_skill_content(path)
            if content is None:
                self._send(404, {"ok": False, "error": "找不到该 Skill 文件"})
            else:
                self._send(200, {"ok": True, "content": content, "path": path})
        elif parsed.path == "/api/git_repos":
            self._send(200, {"ok": True, "repos": gather_git_repos()})
        elif parsed.path == "/api/backups":
            self._send(200, {"ok": True, "backups": list_backups()})
        elif parsed.path == "/api/tokens":
            self._send(200, {"ok": True, "tokens": git_token_summary()})
        elif parsed.path == "/api/processes":
            self._send(200, {"ok": True, "processes": gather_processes()})
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
                elif sec == "conversation_memory":
                    if field not in ("类型", "内容"):
                        raise ValueError("对话记忆仅支持修改类型/内容")
                    path, _ = update_conversation_line(rid, field, value)
                else:
                    raise ValueError("未知数据区")
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/update_multi":
                sec, rid, values = body["section"], body["id"], body.get("values", {})
                path = apply_updates(sec, rid, values)
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/update_category":
                sec, rid, category = body["section"], body["id"], body.get("category", "")
                path = update_category(sec, rid, category)
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/add":
                htype, content = body.get("type", "其他"), body.get("content", "").strip()
                category = body.get("category", "")
                if not content:
                    raise ValueError("内容不能为空")
                path, hid = add_highlight(htype, content, category)
                self._send(200, {"ok": True, "path": path, "id": hid})
            elif self.path == "/api/add_cm":
                ctype, content = body.get("type", "其他"), body.get("content", "").strip()
                category = body.get("category", "")
                if not content:
                    raise ValueError("内容不能为空")
                path, cid = add_conversation_memory(ctype, content, category)
                self._send(200, {"ok": True, "path": path, "id": cid})
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
                elif sec == "conversation_memory":
                    path = delete_conversation_line(rid)
                elif sec == "automations":
                    path = delete_automation(rid)
                elif sec == "memory":
                    path = delete_memory(rid)
                elif sec == "skills":
                    raise ValueError("Skill 不支持删除（避免误删文件），如需移除请到 data/skills 手动处理")
                else:
                    raise ValueError("未知数据区")
                self._send(200, {"ok": True, "path": path})
            elif self.path == "/api/git_action":
                res = git_action(body)
                self._send(200, {"ok": res["ok"], "out": res["out"], "err": res["err"], "code": res["code"]})
            elif self.path == "/api/restore":
                original = restore_backup(body.get("file", ""))
                self._send(200, {"ok": True, "path": original})
            elif self.path == "/api/tokens":
                plat = body.get("platform", "")
                if body.get("op") == "delete":
                    n = delete_git_token(plat)
                    self._send(200, {"ok": True, "removed": n})
                else:
                    write_git_token(plat, body.get("username", ""), body.get("token", ""))
                    self._send(200, {"ok": True})
            elif self.path == "/api/kill_process":
                res = kill_process(body.get("pid"))
                self._send(200, res)
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
    _protect_self()  # 标记自身进程受保护，禁止在工作台内被结束
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
