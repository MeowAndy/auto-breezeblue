#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BreezeBlue 每日积分自动签到 —— 核心库 + 命令行(多账号版)
==========================================================

原理(从 breezeblue.ai 前端代码逆向):
  1. 登录: POST /api/auth/email/continue  {email, password, locale, next_path}
     -> 服务端校验 Firebase 凭据,通过 Set-Cookie 下发 HttpOnly 会话
     (新账号注册需要 Cloudflare Turnstile;已有账号登录不需要;
      Google OAuth 注册的账号没有密码,只能用 Cookie 方式)
  2. 签到: POST /api/credits/daily-claim
     服务端自动按账号档位发放(discord 400 / standard 100),无需参数
  3. 余额: GET  /api/credits/summary   含 daily_claim_* 状态字段
  4. 身份: GET  /api/me
  5. 每日重置: UTC 00:00(北京时间 08:00)

登录态两种来源:
  A. 从浏览器 DevTools 复制 Cookie 请求头,原样回放(推荐,无需密码)
  B. 邮箱 + 密码,脚本自己完成登录换会话(密码只保存在本机 config.json)

命令行:
  python bb_checkin.py run             # 签到所有启用的账号(供计划任务调用)
  python bb_checkin.py watch           # 常驻守护模式:按各账号积分重置时间自动领取
                                       #   (无 systemd/cron 的环境如 Docker 的兜底方案)
  python bb_checkin.py list            # 列出账号
  python bb_checkin.py add "cookie..." # 添加账号(粘贴 Cookie 串)
  python bb_checkin.py test            # 测试所有账号登录态,不签到
  python bb_checkin.py status          # 查看配置与最近结果
  python bb_checkin.py install 08:05   # 注册每日计划任务(Windows: schtasks;
                                       #   Linux: systemd 用户/系统定时器 ->
                                       #   crontab -> watch 守护进程,自动逐级回退)
  python bb_checkin.py remove          # 移除计划任务/守护进程
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
BASE = "https://breezeblue.ai"
API = BASE + "/api"

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_PATH = LOG_DIR / "checkin.log"

TASK_NAME_DAILY = "BreezeBlueDailyCheckin"
TASK_NAME_LOGON = "BreezeBlueCheckinLogon"
TASK_ARGS = "run"

IS_WINDOWS = sys.platform == "win32"

# Linux 计划任务单元名(用户级 systemd)
SYSTEMD_SERVICE = "breezeblue-checkin.service"
SYSTEMD_TIMER = "breezeblue-checkin.timer"
CRON_MARKER = "# BB-CHECKIN-JOB"
WATCH_PID_FILE = SCRIPT_DIR / "watch.pid"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 可重入锁: load_config(迁移) -> save_config、update_account -> save_config
# 存在同线程嵌套获取,必须用 RLock,否则自死锁
_CONFIG_LOCK = threading.RLock()


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    """追加写日志文件并同步到 stdout。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("gbk", "replace").decode("gbk"))


def _brief(info, limit: int = 800) -> str:
    if isinstance(info, (dict, list)):
        info = json.dumps(info, ensure_ascii=False)
    return str(info)[:limit]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# 配置(v2: 多账号)
# ----------------------------------------------------------------------------
def new_account(**kw) -> dict:
    acct = {
        "id": uuid.uuid4().hex[:8],
        "name": "",            # 显示名(留空则自动用站点昵称/邮箱)
        "email": "",           # 可选,密码重登用
        "password": "",        # 可选
        "cookie": "",          # 会话 Cookie 串
        "enabled": True,
        "reward_tier": "",     # 一般留空,服务端自动定档
        "added_at": _now(),
        "last_result": None,
    }
    acct.update(kw)
    return acct


def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("警告: config.json 解析失败,已忽略并重建")

    # v1(单账号) -> v2(账号列表)自动迁移,保留老 Cookie
    if cfg.get("version") != 2:
        legacy_cookie = (cfg.get("cookie") or "").strip()
        legacy_email = (cfg.get("email") or "").strip()
        legacy_pwd = cfg.get("password") or ""
        legacy_result = cfg.get("last_result")
        accounts = cfg.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
        if legacy_cookie or (legacy_email and legacy_pwd):
            if not any(
                a.get("cookie") == legacy_cookie and a.get("email") == legacy_email
                for a in accounts
            ):
                accounts.insert(
                    0,
                    new_account(
                        cookie=legacy_cookie,
                        email=legacy_email,
                        password=legacy_pwd,
                        last_result=legacy_result,
                    ),
                )
                if legacy_result:
                    log("已迁移旧版单账号配置到多账号格式")
        cfg = {"version": 2, "accounts": accounts}
        save_config(cfg)
    cfg.setdefault("accounts", [])
    return cfg


def save_config(cfg: dict) -> None:
    with _CONFIG_LOCK:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def get_accounts() -> list[dict]:
    return load_config().get("accounts", [])


def find_account(acct_id: str) -> dict | None:
    for a in get_accounts():
        if a.get("id") == acct_id:
            return a
    return None


def update_account(acct_id: str, **kwargs) -> dict | None:
    with _CONFIG_LOCK:
        cfg = load_config()
        for a in cfg["accounts"]:
            if a.get("id") == acct_id:
                a.update(kwargs)
                save_config(cfg)
                return a
    return None


def remove_account(acct_id: str) -> bool:
    with _CONFIG_LOCK:
        cfg = load_config()
        before = len(cfg["accounts"])
        cfg["accounts"] = [a for a in cfg["accounts"] if a.get("id") != acct_id]
        if len(cfg["accounts"]) != before:
            save_config(cfg)
            return True
    return False


def add_account(cookie: str = "", email: str = "", password: str = "",
                name: str = "", enabled: bool = True) -> dict:
    acct = new_account(cookie=cookie, email=email, password=password,
                       name=name, enabled=enabled)
    with _CONFIG_LOCK:
        cfg = load_config()
        cfg["accounts"].append(acct)
        save_config(cfg)
    return acct


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, _, domain = email.partition("@")
    if len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "***" + name[-1]
    return f"{masked}@{domain}"


def display_name(acct: dict) -> str:
    return acct.get("name") or _mask_email(acct.get("email", "")) or acct.get("id", "??")


# ----------------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------------
def _headers(cookie: str | None = None, extra: dict | None = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE,
        "Referer": BASE + "/app/home",
        # 模拟网页客户端的请求 id: cfr_<uuid>
        "x-breeze-client-request-id": f"cfr_{uuid.uuid4().hex}",
    }
    if cookie:
        h["Cookie"] = cookie
    if extra:
        h.update(extra)
    return h


def api(
    method: str,
    path: str,
    cookie: str | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict | str]:
    """调用 BreezeBlue API,返回 (http_status, 解析后的JSON或原始文本)。"""
    extra = {}
    data = None
    if json_body is not None:
        extra["Content-Type"] = "application/json"
        data = json.dumps(json_body)
    resp = requests.request(
        method, API + path, headers=_headers(cookie, extra), data=data, timeout=timeout,
    )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text


def extract_cookie(text: str) -> str:
    """
    从用户粘贴的内容里尽量智能地提取 Cookie 头。
    支持: 纯 cookie 串 / "cookie: xxx" 头片段 / copy as cURL 全文。
    """
    text = text.strip()
    if not text:
        return ""
    m = re.search(r"['\"]?cookie['\"]?\s*:\s*['\"]?([^\r\n'\"]+)", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", text)
    if m:
        return m.group(1).strip()
    if "=" in text:
        return text.replace("\n", " ").strip()
    raise ValueError("无法识别的 Cookie 内容,请粘贴完整的 Cookie 请求头")


def cookies_from_response(resp: requests.Response) -> str:
    """把登录响应的 Set-Cookie 汇总成可回放的 Cookie 头。"""
    return "; ".join(f"{c.name}={c.value}" for c in resp.cookies if c.value is not None)


# ----------------------------------------------------------------------------
# 业务动作
# ----------------------------------------------------------------------------
def check_session(cookie: str) -> tuple[bool, dict | str]:
    """GET /api/me 验证登录态。返回 (是否有效, 用户信息或错误)。"""
    status, body = api("GET", "/me", cookie=cookie, timeout=15)
    return status == 200, body


def login_password(email: str, password: str) -> tuple[bool, str, dict | str]:
    """
    邮箱密码登录,返回 (成功?, Cookie头, 用户信息或错误)。
    对应网页端登录表单的提交;用 requests.Session 正确收集 Set-Cookie。

    ⚠️ 必须用 json= 传 body(自动带 Content-Type: application/json)。
    之前用 data=json.dumps(...) 且不设 Content-Type,服务端把 body 当
    非 JSON 解析,对任何账号都返回 400 VALIDATION_ERROR
    ({"loc":["body"],"type":"model_attributes_type","msg":"Invalid value."})。

    网页端还有 signup_verification 字段:首次提交不带,服务端若返回
    403 AUTH_SIGNUP_VERIFICATION_REQUIRED 则弹 Turnstile 人机验证后带
    token 重交。作为兜底:若遇到 VALIDATION_ERROR,补上占位 token 重试一次
    (实测服务端当前对 token 值并不做严格校验)。
    """
    s = requests.Session()
    s.headers.update(_headers())
    payload = {
        "email": email.strip(),
        "password": password,
        "locale": "zh",
        "next_path": "/app/home",
    }
    try:
        resp = s.post(API + "/auth/email/continue", json=payload, timeout=30)
        if resp.status_code == 400 and "VALIDATION_ERROR" in str(
            getattr(resp, "text", "")
        ):
            # 兜底:疑似要求人机验证字段,补占位 token 重试一次
            resp = s.post(
                API + "/auth/email/continue",
                json={**payload, "signup_verification": "auto"},
                timeout=30,
            )
    except requests.RequestException as e:
        return False, "", f"网络错误: {e}"
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if resp.status_code == 200 and isinstance(body, dict):
        cookie = cookies_from_response(resp)
        if cookie:
            return True, cookie, body
        return False, "", body
    return False, "", body


def claim_daily(cookie: str, reward_tier: str | None = None) -> tuple[str, dict | str]:
    """
    执行每日签到。
    返回 (结果类别, 原始响应):
      claimed / already / expired / error
    """
    body = {"reward_tier": reward_tier} if reward_tier else None
    status, resp = api("POST", "/credits/daily-claim", cookie=cookie, json_body=body)

    # 服务端 200 返回示例:
    #   成功: {"claimed": true,  "credits_issued": 400.0, "balance": 2051.3, ...}
    #   重复: {"claimed": false, "already_claimed": true, "credits_issued": 0.0, ...}
    if isinstance(resp, dict):
        if resp.get("already_claimed") is True or resp.get("claimed_today") is True:
            return "already", resp
        if resp.get("claimed") is True:
            return "claimed", resp

    if status == 200 and isinstance(resp, dict) and resp.get("ok") is not False:
        return "claimed", resp

    if isinstance(resp, dict):
        code = str(resp.get("code", "")).upper()
        detail = str(resp.get("detail", "") or resp.get("error", ""))
        if status == 401 or code in ("AUTH_REQUIRED", "UNAUTHORIZED"):
            return "expired", resp
        if "ALREADY" in code or "already" in detail.lower():
            return "already", resp
    return "error", resp


def credits_summary(cookie: str) -> tuple[bool, dict | str]:
    status, body = api("GET", "/credits/summary", cookie=cookie, timeout=15)
    return status == 200, body


# ----------------------------------------------------------------------------
# 单账号签到流程
# ----------------------------------------------------------------------------
def run_checkin_account(acct: dict) -> dict:
    """
    签到单个账号:
      1. 用账号 Cookie 调 daily-claim;无 Cookie 则用邮箱密码现登
      2. 401 时若配置了密码则自动重登重试一次
      3. 结果写回账号 last_result 并打日志
    """
    aid, name = acct.get("id"), display_name(acct)
    email = (acct.get("email") or "").strip()
    password = acct.get("password") or ""
    reward_tier = (acct.get("reward_tier") or "").strip() or None
    cookie = (acct.get("cookie") or "").strip()

    result = {"ok": False, "kind": "error", "message": "", "detail": ""}
    tag = f"[{name}] "

    if not acct.get("enabled", True):
        result.update(kind="disabled", message="账号已停用,跳过")
        return result

    if not cookie and not (email and password):
        result.update(
            kind="unconfigured",
            message="未配置登录:请补充 Cookie 或邮箱密码",
        )
        log(tag + "签到失败: " + result["message"])
        update_account(aid, last_result={**result, "at": _now()})
        return result

    if not cookie:
        log(tag + "本地无 Cookie,使用邮箱密码登录…")
        ok, cookie, info = login_password(email, password)
        if not ok:
            result.update(kind="login_failed", message="邮箱密码登录失败",
                          detail=_brief(info))
            log(tag + f"签到失败: {result['message']} -> {result['detail']}")
            update_account(aid, last_result={**result, "at": _now()})
            return result
        update_account(aid, cookie=cookie)
        log(tag + "密码登录成功,会话已保存")

    kind, resp = claim_daily(cookie, reward_tier)

    # 会话失效: 尝试密码重登后重试一次
    if kind == "expired" and email and password:
        log(tag + "会话已过期,尝试用邮箱密码重新登录…")
        ok, new_cookie, info = login_password(email, password)
        if ok:
            cookie = new_cookie
            update_account(aid, cookie=cookie)
            log(tag + "重新登录成功,重试签到…")
            kind, resp = claim_daily(cookie, reward_tier)
        else:
            result.update(kind="expired", message="会话已过期,且密码重登失败;请重新抓取 Cookie",
                          detail=_brief(info))
            log(tag + f"签到失败: {result['message']} ({result['detail']})")
            update_account(aid, cookie="", last_result={**result, "at": _now()})
            return result

    if kind == "expired":
        result.update(kind="expired", message="会话已过期且未配置邮箱密码;请重新抓取 Cookie")
        log(tag + "签到失败: " + result["message"])
        update_account(aid, cookie="", last_result={**result, "at": _now()})
        return result

    if kind in ("claimed", "already"):
        claim = resp if isinstance(resp, dict) else {}
        issued, balance = claim.get("credits_issued"), claim.get("balance")
        reset_at = str(claim.get("daily_claim_next_reset_at") or "")
        if kind == "claimed":
            msg = "签到成功 🎉"
            if isinstance(issued, (int, float)):
                msg += f" +{issued:g} 积分"
            if isinstance(balance, (int, float)):
                msg += f",余额 {balance:g}"
        else:
            msg = "今天已签到,无需重复"
            if reset_at:
                msg += f"(下次重置 {reset_at})"
        result.update(ok=True, kind=kind, message=msg,
                      detail=json.dumps({"claim": claim}, ensure_ascii=False)[:4000])
        log(tag + f"签到完成: {result['message']}")
    else:
        result.update(kind="error", message="签到接口返回异常", detail=_brief(resp))
        log(tag + f"签到失败: {result['message']} -> {result['detail']}")

    update_account(aid, last_result={**result, "at": _now()})
    return result


def run_checkin_all() -> dict:
    """签到所有启用的账号,返回汇总。"""
    accounts = [a for a in get_accounts() if a.get("enabled", True)]
    if not accounts:
        log("没有启用的账号,跳过签到")
        return {"ok": False, "kind": "unconfigured",
                "message": "没有启用的账号", "results": []}

    results = []
    for acct in accounts:
        results.append({"id": acct.get("id"), "name": display_name(acct),
                        **run_checkin_account(acct)})
    ok_count = sum(1 for r in results if r.get("ok"))
    summary = {
        "ok": ok_count == len(results) and bool(results),
        "kind": "summary",
        "message": f"共 {len(results)} 个账号: 成功/已签 {ok_count},失败 {len(results) - ok_count}",
        "results": results,
    }
    log("批量签到 -> " + summary["message"])
    return summary


# ----------------------------------------------------------------------------
# 计划任务(Windows: schtasks / Linux: systemd 用户定时器,回退 crontab)
# ----------------------------------------------------------------------------
def _task_command() -> str:
    return f'"{sys.executable}" "{SCRIPT_DIR / "bb_checkin.py"}" {TASK_ARGS}'


def _norm_time(time_hhmm: str) -> str | None:
    """校验并规范化 HH:MM,非法返回 None。"""
    if not re.fullmatch(r"\d{1,2}:\d{2}", time_hhmm or ""):
        return None
    hh, mm = time_hhmm.split(":")
    return f"{int(hh):02d}:{int(mm):02d}"


def _run_quiet(args: list[str], input_text: str | None = None) -> tuple[int, str]:
    """静默执行命令,可靠解码输出(bytes 模式,输入显式 UTF-8 编码)。"""
    stdin_bytes = input_text.encode("utf-8") if input_text is not None else None
    try:
        r = subprocess.run(args, capture_output=True, input=stdin_bytes)
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"

    def dec(b: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    return r.returncode, (dec(r.stderr) + "\n" + dec(r.stdout)).strip()


# ---------------- Windows ----------------
def _schtasks(args: list[str]) -> tuple[int, str]:
    """
    执行 schtasks 并可靠解码输出。
    中文 Windows 下 schtasks 输出为 GBK,不能用 text=True(UTF-8 解码会崩)。
    """
    return _run_quiet(["schtasks", *args])


def _install_windows(time_hhmm: str) -> tuple[bool, str]:
    tr = _task_command()
    msgs = []

    code, out = _schtasks([
        "/Create", "/F",
        "/TN", TASK_NAME_DAILY,
        "/SC", "DAILY",
        "/ST", time_hhmm,
        "/TR", tr,
    ])
    daily_ok = code == 0
    msgs.append("每日任务 " + time_hhmm + ": " + ("成功" if daily_ok else out[:200]))

    # 开机补签: 到点时电脑关机错过的话,开机后自动补一次(已签过会直接跳过)
    code2, out2 = _schtasks([
        "/Create", "/F",
        "/TN", TASK_NAME_LOGON,
        "/SC", "ONLOGON",
        "/TR", tr,
    ])
    logon_ok = code2 == 0
    msgs.append("开机补签: " + ("成功" if logon_ok else "失败(不影响每日任务) " + out2[:150]))

    return daily_ok, "; ".join(msgs)


def _remove_windows() -> tuple[bool, str]:
    msgs = []
    for name in (TASK_NAME_DAILY, TASK_NAME_LOGON):
        code, _out = _schtasks(["/Delete", "/F", "/TN", name])
        msgs.append(f"{name}: {'已移除' if code == 0 else '不存在或移除失败'}")
    return True, "; ".join(msgs)


def _status_windows() -> dict:
    out = {}
    for key, name in (("daily", TASK_NAME_DAILY), ("logon", TASK_NAME_LOGON)):
        code, text = _schtasks(["/Query", "/TN", name, "/FO", "LIST", "/V"])
        if code != 0:
            out[key] = {"installed": False}
            continue
        info = {"installed": True}
        m = re.search(r"(?:Next Run Time|下次运行时间)\s*:\s*(.*)", text)
        if m:
            info["next_run"] = m.group(1).strip()
        m = re.search(r"(?:Status|状态)\s*:\s*(.*)", text)
        if m:
            info["status"] = m.group(1).strip()
        out[key] = info
    return out


# ---------------- Linux ----------------
def _systemd_available() -> bool:
    """
    systemd 用户会话是否真的可用。
    `is-system-running` 退出码 1 可能是 degraded(可用)也可能是
    "Failed to connect to bus"(不可用),必须看输出文本区分。
    """
    if not shutil.which("systemctl"):
        return False
    code, out = _run_quiet(["systemctl", "--user", "is-system-running"])
    text = out.lower()
    if "failed to connect" in text or "not booted" in text or text.strip() == "offline":
        return False
    return code in (0, 1)  # running / degraded / starting 都可用


def _install_linux(time_hhmm: str) -> tuple[bool, str]:
    """systemd 用户定时器(错过自动补跑);systemd 不可用或失败时回退 crontab。"""
    hh, mm = time_hhmm.split(":")
    py = sys.executable
    script = SCRIPT_DIR / "bb_checkin.py"
    tz_note = "(按服务器本地时间;若时区为 UTC,建议设 00:05 对应北京时间 08:05)"
    systemd_err = ""

    # ---- 尝试 systemd 用户定时器 ----
    if _systemd_available():
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        service = f"""\
[Unit]
Description=BreezeBlue daily credits check-in

[Service]
Type=oneshot
WorkingDirectory={SCRIPT_DIR}
ExecStart={py} {script} run
"""
        timer = f"""\
[Unit]
Description=Run BreezeBlue check-in daily at {time_hhmm}

[Timer]
OnCalendar=*-*-* {hh}:{mm}:00
# 错过(如关机/重启中)则在下次可用时自动补跑一次
Persistent=true
Unit={SYSTEMD_SERVICE}

[Install]
WantedBy=timers.target
"""
        try:
            (unit_dir / SYSTEMD_SERVICE).write_text(service, encoding="utf-8")
            (unit_dir / SYSTEMD_TIMER).write_text(timer, encoding="utf-8")
        except OSError as e:
            systemd_err = f"写入单元文件失败: {e}"
        else:
            _run_quiet(["systemctl", "--user", "daemon-reload"])
            code, out = _run_quiet([
                "systemctl", "--user", "enable", "--now", SYSTEMD_TIMER,
            ])
            if code == 0:
                msg = (f"systemd 定时器已启用 ✅ 每日 {time_hhmm}; {tz_note}; "
                       "Persistent=true 错过自动补跑")
                return True, msg
            systemd_err = f"enable --now 失败: {out[:150]}"

    # ---- 回退: crontab ----
    if shutil.which("crontab"):
        ok, msg = _install_crontab(hh, mm, py, script)
        if ok:
            return True, msg
        crontab_err = msg
    else:
        crontab_err = "crontab 未安装"

    # ---- 回退: system 级 systemd 定时器(root 或免密 sudo) ----
    ok, msg = _install_system_timer(time_hhmm, py, script)
    if ok:
        return True, msg
    systemd_sys_err = msg

    # ---- 最终兜底: watch 常驻守护进程(任何环境可跑,Docker 亦适用) ----
    ok, msg = _spawn_watch()
    if ok:
        return True, (f"watch 守护进程已启动 ✅(按各账号积分重置时间自动领取); "
                      f"此前: {crontab_err}; system 级 systemd: {systemd_sys_err}")
    return False, (f"所有定时方案均失败: crontab({crontab_err}); "
                   f"system 级 systemd({systemd_sys_err}); watch({msg})。"
                   "若是 Docker 容器,可手动运行: python3 bb_checkin.py watch")


def _install_crontab(hh: str, mm: str, py: str, script: Path) -> tuple[bool, str]:
    cmd = f"cd {SCRIPT_DIR} && {py} {script} run >> {LOG_DIR / 'cron.log'} 2>&1 {CRON_MARKER}"
    code, out = _run_quiet(["crontab", "-l"])
    lines = out.splitlines() if code == 0 else []
    lines = [ln for ln in lines if CRON_MARKER not in ln]
    lines.append(f"{int(mm)} {int(hh)} * * * {cmd}")
    lines.append(f"@reboot {cmd}")
    code2, out2 = _run_quiet(["crontab", "-"], input_text="\n".join(lines) + "\n")
    if code2 == 0:
        return True, f"crontab 已注册 ✅ 每日 {hh}:{mm} + @reboot 补签"
    return False, f"写入失败 {out2[:150]}"


def _install_system_timer(time_hhmm: str, py: str, script: Path) -> tuple[bool, str]:
    """system 级 systemd 定时器,需要 root 或免密 sudo。"""
    import getpass
    euid = getattr(os, "geteuid", lambda: 1)()
    sudo: list[str] = []
    if euid != 0:
        code, _out = _run_quiet(["sudo", "-n", "true"])
        if code != 0:
            return False, "需要 root 或免密 sudo"
        sudo = ["sudo"]

    hh, mm = time_hhmm.split(":")
    user = getpass.getuser()
    service = f"""\
[Unit]
Description=BreezeBlue daily credits check-in

[Service]
Type=oneshot
User={user}
WorkingDirectory={SCRIPT_DIR}
ExecStart={py} {script} run
"""
    timer = f"""\
[Unit]
Description=Run BreezeBlue check-in daily at {time_hhmm}

[Timer]
OnCalendar=*-*-* {hh}:{mm}:00
Persistent=true
Unit={SYSTEMD_SERVICE}

[Install]
WantedBy=multi-user.target
"""
    rcode, rout = _run_quiet(
        [*sudo, "tee", f"/etc/systemd/system/{SYSTEMD_SERVICE}"],
        input_text=service,
    )
    if rcode != 0:
        return False, rout[:150]
    rcode, rout = _run_quiet(
        [*sudo, "tee", f"/etc/systemd/system/{SYSTEMD_TIMER}"],
        input_text=timer,
    )
    if rcode != 0:
        return False, rout[:150]
    _run_quiet([*sudo, "systemctl", "daemon-reload"])
    code, out = _run_quiet([*sudo, "systemctl", "enable", "--now", SYSTEMD_TIMER])
    if code != 0:
        return False, out[:150]
    return True, (f"system 级 systemd 定时器已启用 ✅ 每日 {time_hhmm}; {tz_note()}; "
                  "Persistent=true 错过自动补跑")


def tz_note() -> str:
    return "(按服务器本地时间;若时区为 UTC,建议设 00:05 对应北京时间 08:05)"


def _remove_linux() -> tuple[bool, str]:
    msgs = []
    # systemd 用户级(watch 进程由 remove_tasks 统一停止)
    _run_quiet(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER])
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    removed_unit = False
    for f in (SYSTEMD_TIMER, SYSTEMD_SERVICE):
        p = unit_dir / f
        if p.exists():
            p.unlink(missing_ok=True)
            removed_unit = True
    if removed_unit:
        _run_quiet(["systemctl", "--user", "daemon-reload"])
        msgs.append("systemd 用户定时器: 已移除")
    # systemd 系统级
    code, _t = _run_quiet(["systemctl", "is-enabled", SYSTEMD_TIMER])
    if code == 0:
        _run_quiet(["systemctl", "disable", "--now", SYSTEMD_TIMER])
        for f in (SYSTEMD_TIMER, SYSTEMD_SERVICE):
            _run_quiet(["rm", "-f", f"/etc/systemd/system/{f}"])
        _run_quiet(["systemctl", "daemon-reload"])
        msgs.append("systemd 系统定时器: 已移除")
    # cron
    if shutil.which("crontab"):
        code, out = _run_quiet(["crontab", "-l"])
        if code == 0 and any(CRON_MARKER in ln for ln in out.splitlines()):
            kept = [ln for ln in out.splitlines() if CRON_MARKER not in ln]
            _run_quiet(["crontab", "-"], input_text=("\n".join(kept) + "\n") if kept else "")
            msgs.append("crontab 任务: 已移除")
    if len(msgs) == 1:
        msgs.append("未发现其他已注册的任务")
    return True, "; ".join(msgs)


def _status_linux() -> dict:
    out = {"daily": {"installed": False}, "logon": {"installed": False}}
    if shutil.which("systemctl"):
        code, _t = _run_quiet(["systemctl", "--user", "is-enabled", SYSTEMD_TIMER])
        if code == 0:
            out["daily"] = {"installed": True, "status": "systemd 用户定时器已启用"}
            _c, text = _run_quiet([
                "systemctl", "--user", "list-timers", SYSTEMD_TIMER,
                "--no-pager",
            ])
            # list-timers NEXT 列形如: Mon 2026-11-09 08:05:00 CST
            m = re.search(r"^(\w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?: \S+)?)", text, re.M)
            if m:
                out["daily"]["next_run"] = m.group(1)
            out["logon"] = {"installed": True, "status": "Persistent=true,错过自动补跑"}
            return out
        code, text = _run_quiet(["systemctl", "is-enabled", SYSTEMD_TIMER])
        if code == 0:
            out["daily"] = {"installed": True, "status": "system 级 systemd 定时器已启用"}
            out["logon"] = {"installed": True, "status": "Persistent=true,错过自动补跑"}
            return out
    if shutil.which("crontab"):
        code, text = _run_quiet(["crontab", "-l"])
        if code == 0 and any(CRON_MARKER in ln for ln in text.splitlines()):
            out["daily"] = {"installed": True, "status": "crontab 已注册(每日 + @reboot)"}
            out["logon"] = {"installed": True, "status": "@reboot 补签"}
            return out
    # watch 守护进程
    pid = _watch_pid()
    if pid:
        out["daily"] = {"installed": True,
                        "status": f"watch 守护进程运行中(PID {pid}),按重置时间自动领取"}
        out["logon"] = {"installed": True, "status": "常驻进程,无需补签"}
    return out


# ---------------- watch 常驻守护进程(终极兜底,Docker 亦适用) ----------------
def _watch_pid() -> int | None:
    """读取 PID 文件并确认进程仍存活;无效返回 None。"""
    try:
        pid = int(WATCH_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if IS_WINDOWS:
        _code, out = _run_quiet(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        alive = str(pid) in out
    else:
        code, _out = _run_quiet(["kill", "-0", str(pid)])
        alive = code == 0
    return pid if alive else None


def _spawn_watch() -> tuple[bool, str]:
    pid = _watch_pid()
    if pid:
        return True, f"watch 守护进程已在运行(PID {pid})"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "watch.log"
    with open(log_path, "ab") as lf:
        p = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "bb_checkin.py"), "watch"],
            stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
            cwd=str(SCRIPT_DIR),
            start_new_session=not IS_WINDOWS,
        )
    time.sleep(1.2)
    if p.poll() is None:
        return True, f"watch 守护进程已启动(PID {p.pid}),日志: {log_path}"
    return False, f"启动失败,详见 {log_path}"


def _stop_watch() -> str:
    pid = _watch_pid()
    if not pid:
        return "watch 守护进程: 未在运行"
    try:
        if IS_WINDOWS:
            _run_quiet(["taskkill", "/PID", str(pid), "/F"])
        else:
            _run_quiet(["kill", str(pid)])
        WATCH_PID_FILE.unlink(missing_ok=True)
        return f"watch 守护进程: 已停止(原 PID {pid})"
    except OSError as e:
        return f"watch 守护进程: 停止失败 {e}"


def _parse_iso_utc(s: str) -> float | None:
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _watch_next_sleep() -> float:
    """按各账号 daily_claim_next_reset_at 计算下次唤醒秒数(60s~6h,兜底 1h)。"""
    now = time.time()
    candidates: list[float] = []
    for acct in get_accounts():
        if not acct.get("enabled", True):
            continue
        cookie = (acct.get("cookie") or "").strip()
        if not cookie:
            continue
        try:
            ok, s = credits_summary(cookie)
        except Exception:  # noqa: BLE001
            continue
        if ok and isinstance(s, dict):
            ts = _parse_iso_utc(str(s.get("daily_claim_next_reset_at") or ""))
            if ts and ts > now:
                candidates.append(ts + 90)  # 重置后 90 秒缓冲
    if not candidates:
        return 3600.0
    return max(60.0, min(min(candidates) - now, 21600.0))


def cmd_watch() -> int:
    """常驻模式:循环为所有启用账号签到,醒来时间对齐最近的积分重置点。"""
    pid = _watch_pid()
    if pid:
        print(f"已有 watch 守护进程在运行(PID {pid}),如需重启先执行: python bb_checkin.py remove")
        return 1
    WATCH_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log("watch 守护进程启动(按积分重置时间自动领取)")
    try:
        while True:
            try:
                for acct in [a for a in get_accounts() if a.get("enabled", True)]:
                    run_checkin_account(acct)
            except Exception as e:  # noqa: BLE001
                log(f"watch 轮次异常: {e!r}")
            sleep_s = _watch_next_sleep()
            log(f"watch 下次检查: {int(sleep_s)}s 后")
            deadline = time.time() + sleep_s
            while time.time() < deadline:
                time.sleep(min(600.0, max(0.0, deadline - time.time())))
    finally:
        try:
            WATCH_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------- 对外接口 ----------------
def install_tasks(time_hhmm: str = "08:05") -> tuple[bool, str]:
    """注册每日自动签到任务(Windows 计划任务 / Linux systemd+crontab+watch)。"""
    t = _norm_time(time_hhmm)
    if not t:
        return False, "时间格式应为 HH:MM,例如 08:05"
    if IS_WINDOWS:
        ok, msg = _install_windows(t)
    else:
        ok, msg = _install_linux(t)
    log("计划任务注册 -> " + msg)
    return ok, msg


def remove_tasks() -> tuple[bool, str]:
    if IS_WINDOWS:
        ok, msg = _remove_windows()
    else:
        ok, msg = _remove_linux()
    stop_msg = _stop_watch()
    if "未在运行" not in stop_msg:
        msg += "; " + stop_msg
    log("计划任务移除 -> " + msg)
    return ok, msg


def task_status() -> dict:
    if IS_WINDOWS:
        return _status_windows()
    return _status_linux()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_run() -> int:
    result = run_checkin_all()
    return 0 if result.get("ok") else 1


def cmd_list() -> int:
    accounts = get_accounts()
    if not accounts:
        print("(暂无账号,请在面板里添加)")
        return 0
    for a in accounts:
        print(f"- [{a.get('id')}] {display_name(a)}  "
              f"cookie={'有' if a.get('cookie') else '无'} "
              f"pwd={'有' if a.get('password') else '无'} "
              f"enabled={a.get('enabled', True)}")
    return 0


def cmd_add(cookie_text: str, name: str = "") -> int:
    try:
        cookie = extract_cookie(cookie_text)
    except ValueError as e:
        print(str(e))
        return 1
    ok, info = check_session(cookie)
    if not ok:
        print("Cookie 无效或已过期:", _brief(info, 200))
        return 1
    acct = add_account(cookie=cookie, name=name)
    me = info if isinstance(info, dict) else {}
    acct_name = name or me.get("display_name") or me.get("email") or acct["id"]
    update_account(acct["id"], name=acct_name)
    print(f"已添加账号 {acct_name} (id={acct['id']})")
    return 0


def cmd_test() -> int:
    accounts = get_accounts()
    if not accounts:
        print("未配置任何账号")
        return 1
    bad = 0
    for a in accounts:
        cookie = (a.get("cookie") or "").strip()
        name = display_name(a)
        if not cookie:
            print(f"[{name}] 无 Cookie")
            bad += 1
            continue
        ok, info = check_session(cookie)
        if not ok:
            print(f"[{name}] 登录态已失效:", _brief(info, 150))
            bad += 1
            continue
        ok2, summary = credits_summary(cookie)
        s = summary if isinstance(summary, dict) else {}
        print(f"[{name}] 有效 · 余额 {s.get('balance')} · "
              f"今日已签 {s.get('daily_claim_claimed_today')} · "
              f"档位 {s.get('daily_claim_reward_tier')}")
    return 0 if bad == 0 else 1


def cmd_status() -> int:
    print("配置文件:", CONFIG_PATH)
    print("账号列表:")
    for a in get_accounts():
        lr = a.get("last_result") or {}
        print(f"  - {display_name(a)}: 上次 -> {lr.get('message') or '无'} ({lr.get('at') or '-'})")
    print("计划任务:", json.dumps(task_status(), ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        return cmd_run()
    if cmd == "list":
        return cmd_list()
    if cmd == "add":
        if len(argv) < 3:
            print('用法: python bb_checkin.py add "cookie串" [显示名]')
            return 1
        return cmd_add(argv[2], argv[3] if len(argv) > 3 else "")
    if cmd == "test":
        return cmd_test()
    if cmd == "status":
        return cmd_status()
    if cmd == "install":
        t = argv[2] if len(argv) > 2 else "08:05"
        ok, msg = install_tasks(t)
        print(msg)
        return 0 if ok else 1
    if cmd == "remove":
        ok, msg = remove_tasks()
        print(msg)
        return 0
    if cmd == "watch":
        return cmd_watch()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
