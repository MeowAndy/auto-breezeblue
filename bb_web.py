#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BreezeBlue 每日积分签到 —— 本地管理面板(多账号版)
====================================================
纯标准库实现(http.server),仅监听 127.0.0.1,不依赖 Flask。

  python bb_web.py   # 启动面板并自动打开浏览器(端口被占用时直接打开已运行的实例)

页面功能:
  - 多账号管理: 添加(Cookie / 邮箱密码)、编辑、启停、删除
  - 单账号 / 全部一键签到,余额与签到状态一览
  - 注册/移除每日自动签到计划任务
  - 查看运行日志
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bb_checkin as core

HOST = "127.0.0.1"
PORT = 8765

# 每账号会话探测缓存(key = 账号id+cookie指纹,避免刷新页面就打 API)
_probe_lock = threading.Lock()
_probe_cache: dict = {}
CACHE_TTL = 60.0

# 签到互斥锁(防止按钮连点并发)
_checkin_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 状态采集
# ----------------------------------------------------------------------------
def _cache_key(acct: dict) -> str:
    cookie = acct.get("cookie", "")
    return acct.get("id", "?") + "|" + hashlib.md5(cookie.encode()).hexdigest()[:10]


def _probe_account(acct: dict) -> dict:
    """查询单账号登录态 + 积分概况(带缓存)。"""
    key = _cache_key(acct)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit and (time.time() - hit["at"]) < CACHE_TTL:
            return hit["info"]

    cookie = (acct.get("cookie") or "").strip()
    info: dict = {"valid": None, "me": None, "summary": None, "error": "", "checked_at": ""}
    if cookie:
        try:
            ok, me = core.check_session(cookie)
            info["valid"] = ok
            if ok and isinstance(me, dict):
                info["me"] = {
                    "email": me.get("email", ""),
                    "display_name": me.get("display_name", ""),
                    "avatar_url": me.get("avatar_url", ""),
                }
                # 老账号没有显示名时自动回填站点昵称
                if not acct.get("name") and me.get("display_name"):
                    core.update_account(acct.get("id"), name=me["display_name"])
                    info["me"]["display_name"] = me["display_name"]
                ok2, summary = core.credits_summary(cookie)
                if ok2 and isinstance(summary, dict):
                    info["summary"] = summary
                else:
                    info["error"] = "余额查询失败: " + core._brief(summary, 200)
            elif not ok:
                info["error"] = core._brief(me, 200)
        except Exception as e:  # noqa: BLE001
            info["valid"] = False
            info["error"] = f"网络错误: {e!r}"
    info["checked_at"] = time.strftime("%H:%M:%S")

    with _probe_lock:
        _probe_cache[key] = {"at": time.time(), "info": info}
    return info


def probe_all(accounts: list[dict]) -> dict:
    """并发探测所有账号,返回 {账号id: 探测结果}。"""
    out: dict = {}
    if not accounts:
        return out
    with ThreadPoolExecutor(max_workers=8) as pool:
        for acct, info in zip(accounts, pool.map(_probe_account, accounts)):
            out[acct.get("id", "?")] = info
    return out


def build_state(force: bool = False) -> dict:
    if force:
        with _probe_lock:
            _probe_cache.clear()

    accounts = core.get_accounts()
    probes = probe_all(accounts)

    items = []
    total_balance = 0.0
    claimed_today = 0
    valid_count = 0
    for a in accounts:
        p = probes.get(a.get("id", "?"), {})
        s = p.get("summary") or {}
        balance = s.get("balance")
        if isinstance(balance, (int, float)):
            total_balance += balance
        if p.get("valid"):
            valid_count += 1
        if s.get("daily_claim_claimed_today"):
            claimed_today += 1
        items.append({
            "id": a.get("id"),
            "name": core.display_name(a),
            "email_masked": core._mask_email(a.get("email", "")),
            "enabled": a.get("enabled", True),
            "cookie_set": bool(a.get("cookie")),
            "pwd_set": bool(a.get("password")),
            "reward_tier_cfg": a.get("reward_tier", ""),
            "last_result": a.get("last_result"),
            "probe": p,
        })

    return {
        "accounts": items,
        "totals": {
            "count": len(items),
            "valid": valid_count,
            "claimed_today": claimed_today,
            "total_balance": total_balance,
        },
        "task": core.task_status(),
    }


def read_log_tail(lines: int = 150) -> list[str]:
    if not core.LOG_PATH.exists():
        return []
    try:
        content = core.LOG_PATH.read_text(encoding="utf-8", errors="replace")
        return content.splitlines()[-lines:]
    except OSError:
        return []


def _invalidate_cache():
    with _probe_lock:
        _probe_cache.clear()


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BBPanel/2.0"

    # ---- 工具 ----
    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, *args):  # 静默访问日志
        pass

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            force = "refresh=1" in (self.path or "")
            try:
                self._json({"ok": True, "state": build_state(force=force)})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": repr(e)}, 500)
            return
        if path == "/api/logs":
            self._json({"ok": True, "lines": read_log_tail()})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        data = self._body()

        if path == "/api/account":
            return self._account_add(data)
        if path == "/api/account/update":
            return self._account_update(data)
        if path == "/api/account/delete":
            acct = core.find_account(str(data.get("id", "")))
            if not acct:
                self._json({"ok": False, "message": "账号不存在"})
                return
            core.remove_account(str(data.get("id")))
            core.log(f"面板: 已删除账号 [{core.display_name(acct)}]")
            _invalidate_cache()
            self._json({"ok": True, "message": f"已删除账号 [{core.display_name(acct)}]"})
            return
        if path == "/api/account/checkin":
            acct = core.find_account(str(data.get("id", "")))
            if not acct:
                self._json({"ok": False, "message": "账号不存在"})
                return
            if not _checkin_lock.acquire(blocking=False):
                self._json({"ok": False, "message": "已有签到正在进行,请稍候"})
                return
            try:
                result = core.run_checkin_account(acct)
                _invalidate_cache()
                self._json({"ok": bool(result.get("ok")), **result})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "message": f"执行异常: {e!r}"}, 500)
            finally:
                _checkin_lock.release()
            return
        if path == "/api/checkin/all":
            if not _checkin_lock.acquire(blocking=False):
                self._json({"ok": False, "message": "已有签到正在进行,请稍候"})
                return
            try:
                result = core.run_checkin_all()
                _invalidate_cache()
                self._json({"ok": bool(result.get("ok")),
                            "message": result.get("message", ""),
                            "results": result.get("results", [])})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "message": f"执行异常: {e!r}"}, 500)
            finally:
                _checkin_lock.release()
            return
        if path == "/api/task":
            ok, msg = core.install_tasks(str(data.get("time", "08:05")).strip())
            self._json({"ok": ok, "message": msg})
            return
        if path == "/api/task/remove":
            ok, msg = core.remove_tasks()
            self._json({"ok": ok, "message": msg})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    # ---- 业务: 添加账号 ----
    def _account_add(self, data: dict):
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))
        cookie_text = str(data.get("cookie", ""))

        try:
            cookie = core.extract_cookie(cookie_text) if cookie_text.strip() else ""
        except ValueError as e:
            self._json({"ok": False, "message": str(e)})
            return

        if not cookie and not (email and password):
            self._json({"ok": False, "message": "请填写 Cookie,或邮箱+密码"})
            return

        # Cookie 方式: 先验证
        me: dict = {}
        if cookie:
            ok, info = core.check_session(cookie)
            if not ok:
                self._json({"ok": False, "message": "Cookie 无效或已过期,请重新抓取",
                            "detail": core._brief(info, 400)})
                return
            if isinstance(info, dict):
                me = info
        else:
            # 密码方式: 现登拿会话
            ok, cookie, info = core.login_password(email, password)
            if not ok:
                msg = "登录失败"
                if isinstance(info, dict):
                    code = str(info.get("code", "")).upper()
                    if "VERIFICATION" in code or info.get("site_key"):
                        msg = "该流程需要人机验证(仅限新注册账号);已有账号请用 Cookie 方式"
                self._json({"ok": False, "message": msg, "detail": core._brief(info, 400)})
                return
            ok2, info2 = core.check_session(cookie)
            if ok2 and isinstance(info2, dict):
                me = info2

        if not name and me:
            name = me.get("display_name") or core._mask_email(me.get("email", ""))

        acct = core.add_account(cookie=cookie, email=email, password=password, name=name)
        core.log(f"面板: 添加账号 [{core.display_name(acct)}]")
        _invalidate_cache()
        who = name or (me.get("email") if me else "") or acct["id"]
        self._json({"ok": True, "message": f"账号 [{who}] 添加成功 ✅", "id": acct["id"]})

    # ---- 业务: 修改账号 ----
    def _account_update(self, data: dict):
        acct = core.find_account(str(data.get("id", "")))
        if not acct:
            self._json({"ok": False, "message": "账号不存在"})
            return

        updates: dict = {}
        msgs = []

        if "name" in data:
            updates["name"] = str(data.get("name", "")).strip()
        if "enabled" in data:
            updates["enabled"] = bool(data.get("enabled"))

        cookie_text = str(data.get("cookie", ""))
        if cookie_text.strip():
            try:
                cookie = core.extract_cookie(cookie_text)
            except ValueError as e:
                self._json({"ok": False, "message": str(e)})
                return
            ok, info = core.check_session(cookie)
            if not ok:
                self._json({"ok": False, "message": "新 Cookie 无效或已过期",
                            "detail": core._brief(info, 400)})
                return
            updates["cookie"] = cookie
            msgs.append("Cookie 已更新")

        email = str(data.get("email", "")).strip()
        if email:
            updates["email"] = email
        password = str(data.get("password", ""))
        if password:
            updates["password"] = password
            msgs.append("密码已更新")
        if data.get("clear_password"):
            updates["password"] = ""
            updates["email"] = ""
            msgs.append("已清除邮箱密码")

        if not updates:
            self._json({"ok": False, "message": "没有需要修改的内容"})
            return

        core.update_account(acct["id"], **updates)
        core.log(f"面板: 修改账号 [{core.display_name(acct)}] -> "
                 + (", ".join(msgs) if msgs else ",".join(updates.keys())))
        _invalidate_cache()
        self._json({"ok": True,
                    "message": f"账号 [{core.display_name(acct)}] 已保存 ✅ "
                               + ("; ".join(msgs) if msgs else "")})


# ----------------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BreezeBlue 积分签到管家</title>
<style>
:root{
  --bg:#070d1a; --card:#101a30; --card2:#0c1526; --line:#1f2d4d; --line2:#2a3c66;
  --txt:#eaf1ff; --sub:#8ba0c9; --dim:#5a6d94;
  --acc:#4d9fff; --acc2:#38d0ff; --ok:#34d399; --bad:#fb7185; --warn:#fbbf24;
  --grad:linear-gradient(135deg,#2f7cf6,#38d0ff);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:#2a3c66 #0a1120}
body{
  background:
    radial-gradient(1100px 520px at 85% -8%, #143a7540, transparent),
    radial-gradient(900px 500px at -8% 108%, #0b443c40, transparent),
    var(--bg);
  color:var(--txt);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  min-height:100vh;
}
.wrap{max-width:1060px;margin:0 auto;padding:26px 20px 60px}

/* ---------- 顶栏 ---------- */
.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:20px}
.brand{display:flex;align-items:center;gap:13px}
.logo{width:44px;height:44px;border-radius:13px;background:var(--grad);
  display:flex;align-items:center;justify-content:center;font-size:23px;
  box-shadow:0 4px 20px #2f7cf655}
h1{font-size:19px;font-weight:700;letter-spacing:.3px}
.sub{color:var(--sub);font-size:12px;margin-top:2px}
.top-actions{display:flex;gap:10px;flex-wrap:wrap}

/* ---------- 按钮 ---------- */
button{appearance:none;border:1px solid var(--line2);background:linear-gradient(180deg,#182747,#121e38);
  color:var(--txt);padding:9px 16px;border-radius:11px;font-size:13.5px;cursor:pointer;
  transition:transform .15s,border-color .15s,box-shadow .15s;font-family:inherit}
button:hover{border-color:var(--acc);transform:translateY(-1px)}
button:disabled{opacity:.45;cursor:not-allowed;transform:none}
button.primary{background:var(--grad);border:none;font-weight:700;box-shadow:0 4px 16px #2f7cf644}
button.ghost{background:transparent}
button.sm{padding:6px 12px;font-size:12.5px;border-radius:9px}
button.danger{background:linear-gradient(180deg,#431b26,#331520);border-color:#7f2d3f}
button.danger:hover{border-color:var(--bad)}
.iconbtn{padding:6px 9px;font-size:13px;line-height:1}

/* ---------- 总览 ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
.stat{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);
  border-radius:15px;padding:14px 18px}
.stat .n{font-size:23px;font-weight:800;background:var(--grad);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.stat.plain .n{background:none;color:var(--txt)}
.stat .t{color:var(--sub);font-size:12px;margin-top:3px}

/* ---------- 账号卡片 ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.acct{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);
  border-radius:17px;padding:18px;display:flex;flex-direction:column;gap:12px;
  transition:border-color .2s,transform .2s;position:relative;overflow:hidden}
.acct:hover{border-color:var(--line2);transform:translateY(-2px)}
.acct.off{opacity:.55}
.acct::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--grad);opacity:.75}
.acct-head{display:flex;align-items:center;gap:12px}
.avatar{width:44px;height:44px;border-radius:50%;flex:none;background:var(--grad);
  display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;
  color:#fff;overflow:hidden;border:2px solid #ffffff22}
.avatar img{width:100%;height:100%;object-fit:cover}
.acct-name{font-size:15.5px;font-weight:700;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.acct-mail{color:var(--sub);font-size:12px;margin-top:2px;word-break:break-all}
.badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;letter-spacing:.4px}
.badge.discord{background:#5865f226;color:#9ba3ff;border:1px solid #5865f255}
.badge.standard{background:#34d39922;color:#5fe8b7;border:1px solid #34d39944}
.badge.off{background:#fb718522;color:#ff9db0;border:1px solid #fb718544}
.balance-row{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.balance{font-size:30px;font-weight:800;letter-spacing:.5px}
.balance-unit{color:var(--sub);font-size:12px}
.claim-chip{font-size:11.5px;color:#7cc0ff;background:#12233f;border:1px solid #24406e;
  padding:3px 9px;border-radius:999px}
.kv{display:flex;justify-content:space-between;gap:10px;font-size:12.8px;padding:4px 0}
.kv .k{color:var(--sub);flex:none}
.kv .v{text-align:right;word-break:break-all}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
  font-size:12px;border:1px solid var(--line);background:#0d1526}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.dot.g{background:var(--ok);box-shadow:0 0 7px var(--ok)}
.dot.r{background:var(--bad);box-shadow:0 0 7px var(--bad)}
.dot.y{background:var(--warn);box-shadow:0 0 7px var(--warn)}
.dot.b{background:var(--acc);box-shadow:0 0 7px var(--acc)}
.acct-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:4px}
.err{color:var(--bad);font-size:11.5px;word-break:break-all}

/* ---------- 空状态 ---------- */
.empty{grid-column:1/-1;text-align:center;padding:52px 20px;border:1.5px dashed var(--line2);
  border-radius:17px;color:var(--sub)}
.empty .big{font-size:40px;margin-bottom:10px}
.empty p{margin-bottom:16px;line-height:1.8}

/* ---------- 区块卡 ---------- */
.section{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);
  border-radius:17px;padding:20px;margin-top:18px}
.section h2{font-size:14.5px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.timebox{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.timebox input{width:110px;text-align:center}
.hint{font-size:12px;color:var(--sub);line-height:1.9;margin-top:12px;
  background:#0b1322;border:1px dashed var(--line);border-radius:11px;padding:10px 13px}
.hint b{color:var(--txt)}
.hint code{color:#7cc0ff;background:#132038;padding:1px 6px;border-radius:5px;font-size:11.5px}

input,textarea{background:#0b1322;border:1px solid var(--line);color:var(--txt);
  border-radius:11px;padding:10px 13px;font-size:13.5px;width:100%;outline:none;font-family:inherit}
input:focus,textarea:focus{border-color:var(--acc)}
textarea{resize:vertical;min-height:88px;font-family:Consolas,monospace;font-size:12.5px}
label{display:block;font-size:12.5px;color:var(--sub);margin:12px 0 6px}
.checkline{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--sub);margin-top:12px}
.checkline input{width:auto}

/* ---------- 弹窗 ---------- */
.mask{position:fixed;inset:0;background:#04070fd9;backdrop-filter:blur(3px);
  display:none;align-items:flex-start;justify-content:center;z-index:50;padding:40px 16px;overflow:auto}
.mask.show{display:flex}
.modal{background:linear-gradient(180deg,#131f3a,#0e1830);border:1px solid var(--line2);
  border-radius:18px;max-width:560px;width:100%;padding:24px}
.modal h3{font-size:16px;margin-bottom:6px}
.modal .msub{color:var(--sub);font-size:12.5px;line-height:1.7}
.tabs{display:flex;gap:8px;margin:16px 0 4px}
.tab{flex:1;text-align:center;padding:9px;border-radius:11px;border:1px solid var(--line);
  color:var(--sub);cursor:pointer;font-size:13px;transition:.15s}
.tab.on{background:#16233f;color:var(--txt);border-color:var(--acc);font-weight:600}
.pane{display:none}.pane.on{display:block}
.modal-btns{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}

pre#log{background:#080e1c;border:1px solid var(--line);border-radius:11px;padding:13px;
  font-size:11.8px;line-height:1.75;color:#9fb6dd;max-height:250px;overflow:auto;
  font-family:Consolas,monospace;white-space:pre-wrap;word-break:break-all}

#toast{position:fixed;top:18px;left:50%;transform:translateX(-50%) translateY(-90px);
  background:#16233f;border:1px solid var(--acc);color:var(--txt);padding:11px 22px;
  border-radius:12px;font-size:13.5px;transition:.3s;z-index:99;max-width:92vw;
  box-shadow:0 8px 30px #0008}
#toast.show{transform:translateX(-50%) translateY(0)}
#toast.bad{border-color:var(--bad)}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #ffffff55;border-top-color:#fff;
  border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div id="toast"></div>
<div class="wrap">

  <div class="topbar">
    <div class="brand">
      <div class="logo">🌊</div>
      <div>
        <h1>BreezeBlue 积分签到管家</h1>
        <div class="sub">多账号 · 本地运行 · 仅监听 127.0.0.1 · 凭据只存本机 config.json</div>
      </div>
    </div>
    <div class="top-actions">
      <button class="primary" id="btn-all" onclick="checkinAll()">🚀 全部签到</button>
      <button onclick="openAdd()">＋ 添加账号</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat plain"><div class="n" id="n-accounts">–</div><div class="t">账号总数</div></div>
    <div class="stat plain"><div class="n" id="n-claimed">–</div><div class="t">今日已签</div></div>
    <div class="stat"><div class="n" id="n-balance">–</div><div class="t">总积分余额</div></div>
    <div class="stat plain"><div class="n" id="n-task">–</div><div class="t">每日任务</div></div>
  </div>

  <div class="grid" id="accounts"></div>

  <div class="section">
    <h2>⏰ 每日自动签到</h2>
    <div class="timebox">
      <span style="font-size:13.5px;color:var(--sub)">每天</span>
      <input id="in-time" type="time" value="08:05">
      <button class="primary sm" id="btn-task" onclick="installTask()">✅ 开启</button>
      <button class="danger sm" onclick="removeTask()">✖ 关闭</button>
      <span id="task-detail" style="font-size:12.5px;color:var(--sub)"></span>
    </div>
    <div class="hint">
      积分每天 <b>UTC 00:00(北京时间 08:00)</b> 重置,建议设在 08:05 之后。
      注册 Windows 计划任务到点后台自动为<b>所有启用的账号</b>签到;
      另注册开机补签任务,到点关机错过时开机自动补签(已签过会跳过)。
      命令行等价:<code>python bb_checkin.py install 08:05</code>
    </div>
  </div>

  <div class="section">
    <h2>📜 运行日志 <span style="font-weight:400;color:var(--sub);font-size:11.5px">logs/checkin.log</span></h2>
    <pre id="log">加载中…</pre>
    <div style="margin-top:10px"><button class="sm" onclick="loadLogs()">刷新日志</button></div>
  </div>
</div>

<!-- 添加/编辑账号弹窗 -->
<div class="mask" id="mask">
  <div class="modal">
    <h3 id="m-title">添加账号</h3>
    <div class="msub" id="m-sub">支持两种登录方式;Cookie 方式适用于所有账号(包括 Google 登录)。</div>

    <div class="tabs" id="m-tabs">
      <div class="tab on" id="tab-cookie" onclick="switchTab('cookie')">抓取 Cookie(推荐)</div>
      <div class="tab" id="tab-pwd" onclick="switchTab('pwd')">邮箱密码</div>
    </div>

    <label for="m-name">显示名(可选,留空自动取站点昵称)</label>
    <input id="m-name" placeholder="例如: 大号 / 小号">

    <div class="pane on" id="pane-cookie">
      <label for="m-cookie">粘贴浏览器里的 <b style="color:var(--txt)">Cookie 请求头</b>(整行)</label>
      <textarea id="m-cookie" placeholder="breeze_blue_session=xxxx; breeze_locale=zh"></textarea>
      <div class="hint">
        <b>抓取步骤:</b>浏览器登录 <code>breezeblue.ai</code> → <code>F12</code> →
        <b>Network/网络</b> → <code>F5</code> 刷新 → 点任意一条请求 →
        <b>Request Headers/请求标头</b> 里复制 <code>Cookie:</code> 后面的全部内容。
        也支持直接粘贴 <b>Copy as cURL</b> 全文,自动提取。
        <b id="hint-edit" style="display:none">留空表示不修改 Cookie。</b>
      </div>
    </div>

    <div class="pane" id="pane-pwd">
      <label for="m-email">邮箱</label>
      <input id="m-email" type="email" placeholder="name@example.com">
      <label for="m-pwd">密码</label>
      <input id="m-pwd" type="password" placeholder="BreezeBlue 密码(留空=不修改)">
      <div class="hint">密码只存本机,用于 Cookie 过期时自动重登。
        ⚠️ Google 登录的账号没有密码,请用 Cookie 方式。<b id="hint-edit-pwd" style="display:none">编辑模式:密码留空表示不修改。</b></div>
      <div class="checkline" id="clear-pwd-line" style="display:none">
        <input type="checkbox" id="m-clearpwd"> 清除已保存的邮箱密码
      </div>
    </div>

    <div class="modal-btns">
      <button class="ghost" onclick="closeModal()">取消</button>
      <button class="primary" id="m-save" onclick="saveModal()">保存</button>
    </div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let ACCOUNTS=[], EDIT_ID=null;

function toast(msg,bad=false,ms=4200){
  const t=$('toast');t.textContent=msg;t.className=bad?'bad show':'show';
  clearTimeout(t._h);t._h=setTimeout(()=>t.className=bad?'bad':'',ms);
}
function busy(btn,on,text){
  if(on){btn._txt=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class="spin"></span>'+text;}
  else{btn.disabled=false;if(btn._txt)btn.innerHTML=btn._txt;}
}
async function post(url,data){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{})});
  return r.json();
}
function fmtTime(iso){
  try{return new Date(iso).toLocaleString('zh-CN',{hour12:false});}catch(e){return iso||'';}
}
function fmtNum(n){return (typeof n==='number')? n.toLocaleString('zh-CN') : '–';}

/* ================= 渲染 ================= */
function claimStatus(p){
  const s=p.summary;
  if(p.valid===false) return {cls:'r',txt:'Cookie 失效'};
  if(p.valid!==true) return {cls:'y',txt:'未配置 Cookie'};
  if(s.daily_claim_claimed_today) return {cls:'g',txt:'今日已签 ✓'};
  if(s.daily_claim_available) return {cls:'b',txt:'可签到'};
  return {cls:'y',txt:'暂不可签'};
}
function render(){
  const box=$('accounts');
  if(!ACCOUNTS.length){
    box.innerHTML='<div class="empty"><div class="big">🏄</div>'+
      '<p>还没有账号,先添加一个吧<br>添加后即可一键签到 / 每日自动签到</p>'+
      '<button class="primary" onclick="openAdd()">＋ 添加第一个账号</button></div>';
  }else{
    box.innerHTML=ACCOUNTS.map(a=>{
      const p=a.probe||{}, s=p.summary||{}, me=p.me||{};
      const st=claimStatus(p);
      const avatar = me.avatar_url
        ? '<img src="'+esc(me.avatar_url)+'" referrerpolicy="no-referrer" alt="">'
        : esc((a.name||'?')[0].toUpperCase());
      const tier = s.daily_claim_reward_tier==='discord'
        ? '<span class="badge discord">DISCORD · '+fmtNum(s.daily_claim_discord_amount)+'/天</span>'
        : (s.daily_claim_reward_tier==='standard'
          ? '<span class="badge standard">STANDARD · '+fmtNum(s.daily_claim_standard_amount)+'/天</span>' : '');
      const stpill='<span class="pill"><span class="dot '+st.cls+'"></span>'+st.txt+'</span>';
      const reset = s.daily_claim_next_reset_at ? '下次重置 '+fmtTime(s.daily_claim_next_reset_at) : '';
      const lr=a.last_result;
      const last = lr ? '<div class="kv"><span class="k">上次结果</span><span class="v">'+
        (lr.ok?'<span style="color:var(--ok)">'+esc(lr.message)+'</span>'
              :'<span style="color:var(--bad)">'+esc(lr.message)+'</span>')+
        '<br><span style="color:var(--dim);font-size:11px">'+esc(lr.at||'')+'</span></span></div>' : '';
      const err = p.error ? '<div class="err">'+esc(p.error)+'</div>' : '';
      const disableBtn = a.enabled? '停用':'启用';
      return '<div class="acct'+(a.enabled?'':' off')+'" data-id="'+esc(a.id)+'">'+
        '<div class="acct-head">'+
          '<div class="avatar">'+avatar+'</div>'+
          '<div style="min-width:0;flex:1">'+
            '<div class="acct-name">'+esc(a.name)+(a.enabled?'':'<span class="badge off">已停用</span>')+'</div>'+
            '<div class="acct-mail">'+esc(me.email||a.email_masked||'')+'</div>'+
          '</div>'+
        '</div>'+
        '<div class="balance-row">'+
          '<span class="balance">'+fmtNum(s.balance)+'</span><span class="balance-unit">积分</span>'+
          (s.daily_claim_amount?'<span class="claim-chip">每日 +'+fmtNum(s.daily_claim_amount)+'</span>':'')+
          tier+
        '</div>'+
        '<div class="kv"><span class="k">签到状态</span><span class="v">'+stpill+'</span></div>'+
        (reset?'<div class="kv"><span class="k">重置时间</span><span class="v">'+esc(reset)+'</span></div>':'')+
        '<div class="kv"><span class="k">登录方式</span><span class="v">'+
          (a.cookie_set?'Cookie':'')+(a.cookie_set&&a.pwd_set?' + ':'')+(a.pwd_set?'密码':'')+
          ((!a.cookie_set&&!a.pwd_set)?'未配置':'')+'</span></div>'+
        last+err+
        '<div class="acct-actions">'+
          '<button class="primary sm" onclick="checkinOne(\''+esc(a.id)+'\',this)">🚀 签到</button>'+
          '<button class="sm" onclick="refreshOne()">🔄 刷新</button>'+
          '<button class="sm" onclick="openEdit(\''+esc(a.id)+'\')">✏️ 编辑</button>'+
          '<button class="sm" onclick="toggleOne(\''+esc(a.id)+'\','+(!a.enabled)+')">'+disableBtn+'</button>'+
          '<button class="danger sm" onclick="delOne(\''+esc(a.id)+'\')">🗑</button>'+
        '</div>'+
      '</div>';
    }).join('');
  }
  // 总览
  fetch('/api/state').then(r=>r.json()).then(()=>{}); // noop keepalive
}
function renderStats(st){
  $('n-accounts').textContent=st.totals.count;
  $('n-claimed').textContent=st.totals.valid? (st.totals.claimed_today+' / '+st.totals.valid) : st.totals.claimed_today;
  $('n-balance').textContent=fmtNum(st.totals.total_balance);
  const t=st.task||{};
  $('n-task').textContent=t.daily&&t.daily.installed?'已开启':'未开启';
  $('task-detail').textContent=t.daily&&t.daily.installed?
    ('下次运行: '+(t.daily.next_run||'—')+'  ·  开机补签: '+(t.logon&&t.logon.installed?'已开启':'未开启')):'';
}
async function loadState(force){
  try{
    const r=await fetch('/api/state'+(force?'?refresh=1':''));
    const j=await r.json();
    if(!j.ok){toast('读取状态失败: '+(j.error||''),true);return;}
    const st=j.state;
    ACCOUNTS=st.accounts;
    render();
    renderStats(st);
  }catch(e){toast('连接面板失败: '+e,true);}
}

/* ================= 签到 ================= */
async function checkinOne(id,btn){
  busy(btn,true,'签到中…');
  try{
    const j=await post('/api/account/checkin',{id});
    toast(j.message||String(j),!j.ok,6000);
    await loadState(true);loadLogs();
  }catch(e){toast('请求失败: '+e,true);}
  finally{busy(btn,false);}
}
async function checkinAll(){
  const b=$('btn-all');busy(b,true,'全部签到中…');
  try{
    const j=await post('/api/checkin/all',{});
    const bad=(j.results||[]).filter(r=>!r.ok);
    toast(j.message||String(j), !j.ok, 7000);
    if(bad.length)console.warn('failed accounts:',bad);
    await loadState(true);loadLogs();
  }catch(e){toast('请求失败: '+e,true);}
  finally{busy(b,false);}
}
function refreshOne(){loadState(true);}
async function toggleOne(id,enable){
  const j=await post('/api/account/update',{id,enabled:enable});
  toast(j.message,!j.ok);loadState();
}
async function delOne(id){
  const a=ACCOUNTS.find(x=>x.id===id);
  if(!confirm('确定删除账号 ['+(a?a.name:id)+'] ?\n只删除本机记录,不影响网站账号。'))return;
  const j=await post('/api/account/delete',{id});
  toast(j.message,!j.ok);loadState();
}

/* ================= 弹窗 ================= */
let ModalTab='cookie';
function switchTab(k){
  ModalTab=k;
  $('tab-cookie').classList.toggle('on',k==='cookie');
  $('tab-pwd').classList.toggle('on',k==='pwd');
  $('pane-cookie').classList.toggle('on',k==='cookie');
  $('pane-pwd').classList.toggle('on',k==='pwd');
}
function openAdd(){
  EDIT_ID=null;
  $('m-title').textContent='添加账号';
  $('m-sub').textContent='支持两种登录方式;Cookie 方式适用于所有账号(包括 Google 登录)。';
  $('m-tabs').style.display='flex';
  $('m-name').value='';$('m-cookie').value='';$('m-email').value='';$('m-pwd').value='';
  $('clear-pwd-line').style.display='none';
  $('hint-edit').style.display='none';$('hint-edit-pwd').style.display='none';
  switchTab('cookie');$('mask').classList.add('show');
}
function openEdit(id){
  const a=ACCOUNTS.find(x=>x.id===id);if(!a)return;
  EDIT_ID=id;
  $('m-title').textContent='编辑账号 · '+a.name;
  $('m-sub').textContent='留空的字段表示保持不变;可更换 Cookie、改显示名或启停。';
  $('m-tabs').style.display='none';   // 编辑时两种方式都可改,不再分 tab
  $('pane-cookie').classList.add('on');$('pane-pwd').classList.add('on');
  $('m-name').value=a.name;$('m-cookie').value='';
  $('m-email').value='';$('m-pwd').value='';
  $('clear-pwd-line').style.display=a.pwd_set?'flex':'none';
  $('m-clearpwd').checked=false;
  $('hint-edit').style.display='inline';$('hint-edit-pwd').style.display='inline';
  $('mask').classList.add('show');
}
function closeModal(){$('mask').classList.remove('show');}
$('mask').addEventListener('click',e=>{if(e.target===$('mask'))closeModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

async function saveModal(){
  const b=$('m-save');busy(b,true,'保存中…');
  try{
    let j;
    if(EDIT_ID){
      j=await post('/api/account/update',{
        id:EDIT_ID,
        name:$('m-name').value,
        cookie:$('m-cookie').value,
        email:$('m-email').value,
        password:$('m-pwd').value,
        clear_password:$('m-clearpwd').checked
      });
    }else{
      j=await post('/api/account',{
        name:$('m-name').value,
        cookie:$('m-cookie').value,
        email:$('m-email').value,
        password:$('m-pwd').value
      });
    }
    toast(j.message+(j.detail?' · '+String(j.detail).slice(0,120):''),!j.ok,6500);
    if(j.ok){closeModal();await loadState(true);}
  }catch(e){toast('请求失败: '+e,true);}
  finally{busy(b,false);}
}

/* ================= 计划任务 / 日志 ================= */
async function installTask(){
  const b=$('btn-task');busy(b,true,'注册中…');
  try{
    const j=await post('/api/task',{time:$('in-time').value||'08:05'});
    toast(j.message||String(j),!j.ok,9000);
    await loadState();
  }catch(e){toast('请求失败: '+e,true);}
  finally{busy(b,false);}
}
async function removeTask(){
  const j=await post('/api/task/remove',{});
  toast(j.message||'已处理',!j.ok);loadState();
}
async function loadLogs(){
  try{
    const r=await fetch('/api/logs');const j=await r.json();
    $('log').textContent=(j.lines&&j.lines.length)?j.lines.join('\n'):'(暂无日志)';
    const pre=$('log');pre.scrollTop=pre.scrollHeight;
  }catch(e){}
}

loadState();loadLogs();
setInterval(()=>loadState(),60000);
setInterval(()=>loadLogs(),60000);
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def main() -> int:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    url = f"http://{HOST}:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 已被占用,直接打开已运行的面板…")
        webbrowser.open(url)
        return 0

    core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    core.log(f"管理面板启动: {url}")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    print(f"BreezeBlue 签到面板运行中: {url}  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
