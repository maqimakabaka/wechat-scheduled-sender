# -*- coding: utf-8 -*-
"""本地 HTTP 服务（标准库实现，零额外依赖）。

由 Electron 主进程拉起，也可单独运行（浏览器模式）。

接口：
  GET  /                     前端页面（浏览器模式）
  GET  /api/paths            各用户目录位置（都在安装目录下）
  GET  /api/accounts         本机所有微信账号
  GET  /api/contacts?account= 该账号的群聊 + 好友
  GET  /api/messages         当前消息池
  POST /api/messages         保存消息池
  POST /api/plan             生成当日计划（预览）
  POST /api/start            开始执行
  POST /api/stop             停止执行
  GET  /api/status           运行状态与日志
  GET  /api/wechat           微信窗口检测
  GET  /shots/<file>         截图

鉴权：Electron 模式会传入 --token，所有 /api/* 需带 X-Token 头；
      浏览器模式（无 token）不强制，方便本地调试。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

# 兼容 embeddable / 隔离模式的 Python：
# 内置的 python._pth 会接管 sys.path，**不会**自动把脚本所在目录加进去，
# 于是下面 `import userdata` 这类同级模块就找不到了
# （实测报 ModuleNotFoundError: No module named 'userdata'）。
# 显式补一次，对普通 Python 无副作用（它本就已经在 sys.path 里）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 必须在导入 sender / contacts 之前完成路径重定向
import userdata

userdata.configure(os.environ.get("WCS_DATA_ROOT"))

import autorun as autorun_mod  # noqa: E402
import backup as backup_mod  # noqa: E402
import campaign as campaign_mod  # noqa: E402
import contacts as contacts_mod  # noqa: E402
import doctor as doctor_mod  # noqa: E402
import failures as failures_mod  # noqa: E402
import guard as guard_mod  # noqa: E402
import housekeeping as house_mod  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import leads as leads_mod  # noqa: E402
import media as media_mod  # noqa: E402
import planner  # noqa: E402
import remark_send  # noqa: E402
import report as report_mod  # noqa: E402
import scheduler  # noqa: E402
import settings as settings_mod  # noqa: E402
import targets as targets_mod  # noqa: E402
import update as update_mod  # noqa: E402
import wxdir  # noqa: E402
from sender import SendEngine, find_independent_chat_windows, find_wechat_window  # noqa: E402

ROOT = userdata.data_root()
DATA_DIR = userdata.sub(userdata.DIR_DATA)
LOG_DIR = userdata.sub(userdata.DIR_LOGS)
SHOT_DIR = userdata.sub(userdata.DIR_SHOTS)
CACHE_DIR = userdata.sub(userdata.DIR_CACHE)
FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.txt")
CAMPAIGN_FILE = os.path.join(DATA_DIR, "campaign.txt")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

AUTH_TOKEN = ""   # 由 main() 注入

# 当前程序版本，由 main() 注入（Electron 传 app.getVersion()）。
# 浏览器模式/直接跑 server.py 时留空，检查更新会提示「未知当前版本」。
APP_VERSION = ""


def read_mode() -> str:
    """读取用户选择的解析模式（line / separator）。"""
    m = settings_mod.get(DATA_DIR, "message_mode")
    if m in (planner.MODE_LINE, planner.MODE_SEPARATOR):
        return m
    return planner.MODE_LINE


def read_settings() -> dict:
    """全部设置（已与默认值合并）。"""
    return settings_mod.load(DATA_DIR)


def schedule_config() -> Dict[str, Any]:
    """调度相关设置的投影，给前端与计划生成用。"""
    s = settings_mod.load(DATA_DIR)
    return {
        "start_at": s["start_at"],
        "hook_delay_minutes": float(s["hook_delay_minutes"]),
        "weekly_cap": int(s["weekly_cap"]),
        "daily_cap": int(s["daily_cap"]),
        "run_cap": int(s["run_cap"]),
        "max_fail_streak": int(s["max_fail_streak"]),
        "interval_min": float(s["interval_min"]),
        "interval_max": float(s["interval_max"]),
        "cooldown_every": int(s["cooldown_every"]),
        "cooldown_seconds": int(s["cooldown_seconds"]),
        "retry_times": int(s["retry_times"]),
        "retry_delay_seconds": int(s["retry_delay_seconds"]),
        "verify_timeout": int(s["verify_timeout"]),
        "clear_input_before": bool(s["clear_input_before"]),
        "auto_enabled": bool(s["auto_enabled"]),
        "auto_check_seconds": int(s["auto_check_seconds"]),
        # auto_grace_minutes 必须在这里透出：due() 从这份投影里读它，
        # 漏掉的话会回落到 0（= 过点就不补跑），程序晚开一分钟就永远不发。
        "auto_grace_minutes": int(s["auto_grace_minutes"]),
        "inbox_auto_poll": bool(s["inbox_auto_poll"]),
        "inbox_poll_seconds": int(s["inbox_poll_seconds"]),
        # 清理与关键词也必须在这里透出：_housekeep() 从这份投影里读保留天数，
        # 漏掉会回落到 0（= 不清理），自动清理就永远不生效 ——
        # 实测就是这么发生的，是「健康自检」把它揪出来的。
        "log_keep_days": int(s["log_keep_days"]),
        "shot_keep_days": int(s["shot_keep_days"]),
        "hook_days": list(s["hook_days"]),
        "run_days": list(s["run_days"]),
        "enforce_window": bool(s["enforce_window"]),
        "send_window_start": s["send_window_start"],
        "send_window_end": s["send_window_end"],
    }


def write_settings(**kw) -> Dict[str, Any]:
    """保存设置，返回 ``{ok, saved, errors}``（校验失败的不写入）。"""
    return settings_mod.save(DATA_DIR, kw)


class Runtime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.logs: list = []
        self.progress = {"round": 0, "total_rounds": 0, "sent": 0, "total": 0, "failed": 0}
        self.plan = None
        self.started_at = None

    def log(self, level: str, msg: str) -> None:
        item = {"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
        with self.lock:
            self.logs.append(item)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
        try:
            with open(os.path.join(LOG_DIR, "run-%s.log" % time.strftime("%Y%m%d")),
                      "a", encoding="utf-8") as f:
                f.write("[%s] %-5s %s\n" % (item["time"], level, msg))
        except Exception:
            pass


RT = Runtime()


def _launch_send_thread(run, *args) -> bool:
    def worker():
        try:
            run(*args)
        finally:
            RT.running = False

    with RT.lock:
        if RT.thread is not None and RT.thread.is_alive():
            return False
        RT.logs = []
        RT.progress.update({"round": 0, "total_rounds": 0, "sent": 0, "total": 0, "failed": 0})
        RT.running = True
        RT.started_at = time.time()
        RT.thread = threading.Thread(target=worker, daemon=True)
        try:
            RT.thread.start()
        except Exception:
            RT.running = False
            raise
        return True


def _remark_preview(account: str, keywords: list[str]) -> tuple[str, list[dict]]:
    if not isinstance(account, str) or not account:
        raise ValueError("请选择微信账号")
    accounts = contacts_mod.list_local_accounts()
    selected = next((a for a in accounts if a.get("account") == account), None)
    if selected is None:
        raise ValueError("所选微信账号不可用，请重新加载联系人")
    friends = contacts_mod.list_friends(account)
    matches = remark_send.matches(friends, keywords)
    account_id = selected.get("wxid") or account
    if not matches:
        return account_id, matches

    names: dict[str, set[str]] = {}
    for contact in [*friends, *contacts_mod.list_groups(account)]:
        for value in (contact.get("name"), contact.get("nick_name"), contact.get("remark")):
            if value:
                names.setdefault(value.strip(), set()).add(contact["username"])

    history = remark_send.attempted(DATA_DIR, account_id)
    active = selected.get("is_current") and not selected.get("current_uncertain")
    ambiguous = {m["username"] for m in matches if len(accounts) > 1
                 or names.get(m["name"]) != {m["username"]}}
    main = find_wechat_window(restore=False) if ambiguous and active else None
    windows = find_independent_chat_windows(main, ambiguous) if main else {}
    for match in matches:
        username = match["username"]
        independent = username in ambiguous
        reason = ""
        if username in history:
            reason = "已有备注发送尝试记录"
        elif not active:
            reason = "无法确认所选账号已登录"
        elif independent and username not in windows:
            reason = "请在微信中单独打开该联系人的聊天窗口，或改用保留关键词的唯一备注"
        match.update(eligible=not reason, reason=reason, independent=independent)
    return account_id, matches


def _run_remark_send(account: str, selected_account: str, main: int,
                     targets: list[dict], text: str) -> None:
    RT.progress.update({"round": 1, "total_rounds": 1, "total": len(targets)})
    RT.log("INFO", "开始备注关键词发送，目标 %d 位" % len(targets))
    cfg = schedule_config()
    failed_in_row = 0
    main_engine = None
    for target in targets:
        if not RT.running:
            break
        if cfg["enforce_window"] and not scheduler.in_window(
                datetime.datetime.now(), cfg["send_window_start"], cfg["send_window_end"]):
            RT.log("WARN", "已超出发送时段，停止备注关键词发送")
            break
        accounts = contacts_mod.list_local_accounts()
        if find_wechat_window(restore=False) != main or not any(
                a.get("account") == selected_account and a.get("is_current")
                and not a.get("current_uncertain") for a in accounts):
            RT.log("WARN", "微信窗口或登录账号已变化，停止备注关键词发送")
            break
        username, name = target["username"], target["name"]
        independent = target["independent"]
        try:
            if independent:
                window = find_independent_chat_windows(main, {username}).get(username)
                if not window:
                    raise RuntimeError("已无法核对独立聊天窗口的身份")
                engine = SendEngine(hwnd=window, should_stop=lambda: not RT.running,
                                    independent_username=username)
            else:
                if main_engine is None:
                    main_engine = SendEngine(hwnd=main, should_stop=lambda: not RT.running)
                engine = main_engine
            result = engine.send_remark_text(
                username, name, text, independent,
                reserve=lambda: remark_send.reserve(DATA_DIR, account, username),
            )
        except Exception as exc:
            result = {"error": str(exc)}
        if result.get("ok"):
            RT.progress["sent"] += 1
            failed_in_row = 0
            RT.log("OK", "备注关键词已发送 → %s（%s）" % (name, username))
        else:
            RT.progress["failed"] += 1
            failed_in_row += 1
            RT.log("WARN", "%s（%s）：%s" % (name, username, result.get("error")))
            if cfg["max_fail_streak"] and failed_in_row >= cfg["max_fail_streak"]:
                RT.log("WARN", "连续失败达到限制，停止备注关键词发送")
                break
        time.sleep(1.2)
    RT.log("INFO", "备注关键词发送结束。成功 %d / 失败 %d"
           % (RT.progress["sent"], RT.progress["failed"]))


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in str(name))
    return out[:40] or "target"


def _send_one(engine, display: str, wxid, text: str, shot_dir: str,
              pair: dict, kind: str = "main",
              kind_label: str = "", cfg: Optional[Dict[str, Any]] = None) -> str:
    """发一条（含重试）并记账，同时写投放日志。

    返回 ``"ok"`` / ``"fail"`` / ``"cancel"`` —— 调用方靠它判断
    是否触发「连续失败中止」或停止。用字符串而不是布尔值，
    是因为「失败」和「被取消」要区别对待：前者计数、后者立即停。
    """
    cfg = cfg or schedule_config()
    attempts = max(1, int(cfg.get("retry_times") or 0) + 1)
    retry_delay = float(cfg.get("retry_delay_seconds") or 3)

    # 截图存档可关（设置里「发送后自动截图存档」）。
    # 关掉后 shot_path 传 None，send_to 就完全跳过截图 —— 既不写盘也不抓画面。
    shot_path = None
    if bool(cfg.get("shot_enabled", True)):
        shot_path = os.path.join(
            shot_dir, time.strftime("%Y%m%d"),
            "r%02d_%s_%s.png" % (pair.get("index") or 1, time.strftime("%H%M%S"),
                                 _safe(display)),
        )

    res: Dict[str, Any] = {}
    ok = False

    # 有素材但没有文字时明确说出来。
    # 这类情况多半是「计划是消息池还空着时生成的」，事后补了文字却没重建计划，
    # 表现就是「设置了先发文字，结果只发了素材」，而且日志里看不出任何异常。
    if (pair.get("media_path") or "") and not (text or "").strip():
        RT.log("WARN", "%s：本条没有文字，只发素材"
                       "（若你本意是发文字，请重新生成计划预览）" % display)

    for attempt in range(attempts):
        if not RT.running:
            return "cancel"
        res = engine.send_to(wxid or display, text, display=display,
                             username=wxid, shot_path=shot_path,
                             media_path=pair.get("media_path") or None,
                             media_first=bool(pair.get("media_first", True)),
                             verify_timeout=float(cfg.get("verify_timeout") or 8),
                             clear_before=bool(cfg.get("clear_input_before", True)))
        if res.get("cancelled"):
            return "cancel"
        ok = bool(res.get("ok"))
        if ok:
            break
        if attempt < attempts - 1:
            RT.log("WARN", "发送失败，%.0f 秒后重试（第 %d/%d 次）-> %s：%s"
                   % (retry_delay, attempt + 1, attempts - 1, display,
                      res.get("error")))
            slept = 0.0
            while slept < retry_delay and RT.running:
                time.sleep(min(1.0, retry_delay - slept))
                slept += 1.0
            if not RT.running:
                return "cancel"

    # 投放日志：周报要靠它算「单群 7 条主文案 / 2-3 条钩子」的达成率
    try:
        report_mod.log_delivery(DATA_DIR, display, kind, ok)
    except Exception:
        pass

    if ok:
        RT.progress["sent"] += 1
        t = "已校验" if res.get("verified") or res.get("media_verified") else "未回读"
        extra = ""
        mp = pair.get("media_path")
        if mp:
            extra = " [素材 %s]" % os.path.basename(mp)
        if attempts > 1 and res.get("_retried"):
            extra += " [重试成功]"
        head = ("%s " % kind_label) if kind_label else ""
        RT.log("OK", "%s已发送 -> %s [%s]%s | %s"
               % (head, display, t, extra,
                  (text or "(仅素材)")[:24].replace("\n", " ")))
        # 发成功就按内容销账：这条以前失败过的话，待办自动消失
        try:
            failures_mod.resolve(DATA_DIR, display, kind, text,
                                  pair.get("media_path") or "")
        except Exception:
            pass
        return "ok"

    RT.progress["failed"] += 1
    RT.log("ERROR", "发送失败（已试 %d 次）-> %s : %s"
           % (attempts, display, res.get("error")))
    # 落盘登记，避免「程序一关就不知道哪些没发出去」
    try:
        failures_mod.add(DATA_DIR, display, kind, text,
                         media_path=pair.get("media_path") or "",
                         username=wxid or "", error=res.get("error") or "")
    except Exception:
        pass
    return "fail"


def _run_events(engine, plan: dict) -> List[str]:
    """按「绝对时间事件表」执行（内容编排模式）。

    事件已按时间排好序，逐条等到点再发。这样群 1 的钩子可以插在
    群 5 的主文案之前 —— 而不是把所有钩子堆到全部主文案之后。

    返回：**主文案发送成功**的目标列表（供每日/每周上限记账）。
    """
    events = plan.get("events") or []
    RT.progress.update({"total": len(events), "sent": 0, "failed": 0,
                        "total_rounds": 1, "round": 1})
    RT.log("INFO", "===== 内容编排模式：%d 个事件，起始 %s ====="
           % (len(events), plan.get("start_at")))

    cfg = schedule_config()
    # 连续失败中止：一条失败可能是偶发，连着失败通常是微信窗口出问题
    # 或账号被限制，继续发只是白费功夫还可能加重风控
    fail_streak_limit = int(cfg.get("max_fail_streak") or 0)
    # 冷却：一口气连发几十条最像机器人
    cooldown_every = int(cfg.get("cooldown_every") or 0)
    cooldown_seconds = float(cfg.get("cooldown_seconds") or 0)
    fail_streak = 0
    sent_streak = 0
    sent_main: List[str] = []

    for i, ev in enumerate(events):
        if not RT.running:
            RT.log("WARN", "收到停止指令，终止执行")
            break
        # 等到事件时间点；等待期间可被停止打断
        # 注意：这里必须用 datetime.datetime 而不是 datetime ——
        # `import datetime` 拿到的是**模块**，模块上并没有 fromisoformat/now，
        # 直接 datetime.fromisoformat() 会抛 AttributeError；
        # 早期版本被 except 吞掉后 at 变成 None，整段等待被跳过，
        # 结果是所有事件连着发（实测钩子比计划早 29 秒落库）。
        try:
            at = datetime.datetime.fromisoformat(ev["at"])
        except Exception:
            at = None
        if at is not None:
            # 只报一次等待，不然每 2 秒一条会把日志刷爆
            announced = False
            while RT.running:
                gap = (at - datetime.datetime.now()).total_seconds()
                if gap <= 0:
                    break
                if not announced and gap > 5:
                    announced = True
                    RT.log("INFO", "等待 %.0f 秒后发送 %s -> %s"
                           % (gap, {"main": "主文案", "hook": "钩子"}.get(
                               ev.get("kind") or "", ev.get("kind")),
                              ev.get("target")))
                time.sleep(min(2.0, max(0.2, gap)))
        if not RT.running:
            break

        pair = {
            "index": 1,
            "target": ev.get("target"),
            "media_path": ev.get("media_path") or "",
            # 跟随事件自带的顺序设置（原来硬编码 True，界面选择被忽略）
            "media_first": bool(ev.get("media_first", True)),
        }
        wxid = (plan.get("username_of") or {}).get(ev.get("target") or "")
        kind = ev.get("kind") or "main"
        label = {"main": "[主文案]", "hook": "[钩子]"}.get(kind, "")
        status = _send_one(engine, ev.get("target") or "", wxid,
                           ev.get("text") or "", SHOT_DIR, pair,
                           kind=kind, kind_label=label, cfg=cfg)
        RT.progress["round"] = 1
        if status == "cancel":
            RT.log("WARN", "已停止：中断于 %s" % ev.get("target"))
            break

        if status == "ok":
            fail_streak = 0
            sent_streak += 1
            if kind == "main" and ev.get("target"):
                sent_main.append(ev["target"])
            # 冷却：每发满 N 条强制休息，打断「连续不断发送」的机器特征
            if cooldown_every > 0 and sent_streak >= cooldown_every:
                sent_streak = 0
                if i + 1 < len(events):
                    RT.log("INFO", "已连发 %d 条，休息 %.0f 秒"
                           % (cooldown_every, cooldown_seconds))
                    slept = 0.0
                    while slept < cooldown_seconds and RT.running:
                        time.sleep(min(2.0, cooldown_seconds - slept))
                        slept += 2.0
        else:
            fail_streak += 1
            if fail_streak_limit > 0 and fail_streak >= fail_streak_limit:
                RT.log("ERROR", "连续失败 %d 条，已自动中止（可在设置里调整"
                                "「连续失败即中止」）" % fail_streak)
                break

        if i + 1 >= len(events):
            break
        time.sleep(1.2)
    return sent_main


def _housekeep(reason: str = "") -> Dict[str, Any]:
    """按保留天数回收截图与日志。

    只删自己产生的东西（shots/ 下的日期目录、delivery_log.jsonl），
    并把清单写进运行日志 —— 默默删掉的话，用户某天发现截图没了
    会以为是 bug。
    """
    sc = schedule_config()
    try:
        r = house_mod.run_all(DATA_DIR, SHOT_DIR,
                              int(sc.get("log_keep_days") or 0),
                              int(sc.get("shot_keep_days") or 0))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    files = r.get("total_files") or 0
    dropped = r.get("logs", {}).get("dropped") or 0
    if files or dropped:
        mb = (r.get("total_bytes") or 0) / 1048576.0
        RT.log("INFO", "清理%s：回收截图 %d 个文件（%.2f MB），"
                       "清理日志 %d 条" % (("（%s）" % reason) if reason else "",
                                        files, mb, dropped))
    return r


class InboxPoller:
    """自动轮询私聊：定时读库把新线索登记进线索表。

    为什么单独一个线程而不是塞进 AutoRunner：
    两者的触发条件完全不同（一个按「每天几点」、一个按「每隔几秒」），
    塞一起会让「该不该跑」的判断变成一团条件。
    """

    def __init__(self, data_dir: str, log) -> None:
        self.data_dir = data_dir
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_poll = 0.0
        self.last_added = 0
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="inbox-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            try:
                cfg = schedule_config()
                if cfg.get("inbox_auto_poll"):
                    st = settings_mod.load(self.data_dir)
                    r = inbox_mod.poll_new(
                        data_dir=self.data_dir,
                        need_keywords=st.get("keywords_need"),
                        hook_keywords=st.get("keywords_hook"),
                    )
                    if r.get("error"):
                        self.last_error = r["error"]
                    else:
                        items = r.get("items") or []
                        added = leads_mod.add_items(items, self.data_dir) \
                            if items else 0
                        self.last_poll = time.time()
                        self.last_added = added
                        self.last_error = ""
                        if added:
                            self._log("INFO", "自动轮询：新增 %d 条线索" % added)
            except Exception as exc:
                self.last_error = str(exc)
                self._log("ERROR", "自动轮询异常：%s" % exc)
            try:
                wait_s = float(schedule_config().get("inbox_poll_seconds") or 300)
            except Exception:
                wait_s = 300.0
            self._stop.wait(max(20.0, min(3600.0, wait_s)))

    def status(self) -> Dict[str, Any]:
        cfg = schedule_config()
        return {
            "thread_running": self.running,
            "enabled": bool(cfg.get("inbox_auto_poll")),
            "interval_seconds": cfg.get("inbox_poll_seconds"),
            "last_poll": self.last_poll,
            "last_added": self.last_added,
            "last_error": self.last_error,
        }


def _retry_run() -> bool:
    """补发：把失败的待办当成一份计划跑一遍。

    复用内容编排那套事件执行器 —— 补发本质上就是「按时间表发一批消息」，
    没必要另写一条发送路径（两条路径迟早会跑偏）。
    发成功的条目会在 _send_one 里自动销账，仍失败的继续留在待办。
    """
    if RT.running:
        return False
    events = failures_mod.build_plan_items(DATA_DIR)
    if not events:
        RT.log("INFO", "没有待补发的失败记录")
        return False
    if not find_wechat_window():
        RT.log("WARN", "补发：未找到微信窗口（可能未登录）")
        return False

    pend = failures_mod.pending(DATA_DIR)
    by_name = {x.get("target"): x.get("username") or "" for x in pend}
    plan = {
        "mode": "campaign",
        "events": events,
        "targets": sorted({e["target"] for e in events}),
        "target_count": len({e["target"] for e in events}),
        "total_messages": len(events),
        "main_count": sum(1 for e in events if e["kind"] == "main"),
        "hook_count": sum(1 for e in events if e["kind"] == "hook"),
        "username_of": by_name,
        "retry": True,
        "start_at": "补发",
    }
    RT.plan = plan
    if not _launch_send_thread(_run_plan, plan):
        return False
    RT.log("INFO", "开始补发 %d 条失败记录" % len(events))
    return True


def _auto_run(task: dict) -> bool:
    """自动运行的执行入口：建计划 + 起线程跑。返回是否真的触发了执行。

    由 AutoRunner 调用。守护只负责判断「该不该跑」，建计划和发送在这里 ——
    这样 autorun 模块不依赖 server 的任何内部实现。
    """
    if RT.running:
        return False
    entries = [t for t in (task.get("targets") or []) if t.get("name")]
    if not entries:
        RT.log("WARN", "自动运行：目标名单为空，跳过")
        return False
    targets = [t["name"] for t in entries]

    if not find_wechat_window():
        RT.log("WARN", "自动运行：未找到微信窗口（可能未登录），本轮跳过")
        return False

    cfg = schedule_config()
    camp = campaign_mod.load_campaign(CAMPAIGN_FILE)
    try:
        plan = planner.build_campaign_plan(
            targets=targets, campaign_obj=camp,
            start_at=cfg["start_at"],
            interval_min=cfg["interval_min"], interval_max=cfg["interval_max"],
            hook_delay_min=cfg["hook_delay_minutes"], hook_days=cfg["hook_days"],
            weekly_cap=cfg["weekly_cap"], daily_cap=cfg["daily_cap"],
            run_cap=cfg["run_cap"],
            enforce_window=cfg["enforce_window"],
            window_start=cfg["send_window_start"],
            window_end=cfg["send_window_end"],
            data_dir=DATA_DIR,
        )
    except Exception as exc:
        RT.log("ERROR", "自动运行建计划失败：%s" % exc)
        return False

    # 按名字绑 wxid，执行器靠它精确发送
    plan["username_of"] = {t["name"]: t.get("username") for t in entries}
    plan["auto"] = True

    if not plan.get("events"):
        why = plan.get("blocked_reason") or (
            "所有目标都达到上限" if plan.get("skipped_by_cap") else "没有可发的事件")
        RT.log("WARN", "自动运行：本轮没有要发的内容（%s）" % why)
        return False

    RT.plan = plan
    if not _launch_send_thread(_run_plan, plan):
        return False
    RT.log("INFO", "自动运行开始：%d 个事件，覆盖 %d 个目标"
           % (plan["total_messages"], plan["target_count"]))
    return True


AUTORUN = autorun_mod.AutoRunner(
    DATA_DIR,
    cfg_loader=schedule_config,
    run_fn=_auto_run,
    log=lambda lvl, msg: RT.log(lvl, msg),
    is_busy=lambda: RT.running,
)

INBOX_POLLER = InboxPoller(DATA_DIR, log=lambda lvl, msg: RT.log(lvl, msg))


def _run_plan(plan: dict) -> None:
    try:
        # 把「是否已请求停止」透给引擎：一次发送最长几十秒，
        # 只在两条之间判停止会导致「点了停止还在发」（实测停了还在跑 42 秒）。
        engine = SendEngine(should_stop=lambda: not RT.running)
        RT.log("INFO", "已连接微信窗口 hwnd=%s" % engine.hwnd)
    except Exception as exc:
        RT.log("ERROR", "初始化发送引擎失败: %s" % exc)
        RT.running = False
        return

    # 内容编排模式：按绝对时间事件表跑
    if plan.get("mode") == "campaign" and plan.get("events"):
        sent_main: List[str] = []
        try:
            sent_main = _run_events(engine, plan)
        except Exception as exc:
            RT.log("ERROR", "执行异常: %s" % exc)
        RT.log("INFO", "任务结束。成功 %d / 失败 %d" %
               (RT.progress["sent"], RT.progress["failed"]))
        # 记账供每日/每周上限统计。
        # 只记**主文案真的发成功**的群：按计划里所有目标一律记账会让
        # 失败的群也消耗额度，次数一多就莫名其妙被上限挡住。
        try:
            for t in sent_main:
                scheduler.record_sent(t, DATA_DIR, kind="main")
        except Exception:
            pass
        # 跑完顺手回收一次（截图是这次跑出来的，趁热清最省事）
        try:
            _housekeep("任务结束")
        except Exception:
            pass
        RT.running = False
        return

    rounds = plan.get("assignments") or []
    cfg = schedule_config()
    fail_streak_limit = int(cfg.get("max_fail_streak") or 0)
    fail_streak = 0
    total = sum(len(r["pairs"]) for r in rounds)
    RT.progress.update({"total": total, "sent": 0, "failed": 0,
                        "total_rounds": len(rounds), "round": 0})

    for idx, rnd in enumerate(rounds):
        if not RT.running:
            RT.log("WARN", "收到停止指令，终止执行")
            break
        RT.progress["round"] = rnd["index"]
        RT.log("INFO", "===== 第 %d 轮开始（%d 条）=====" % (rnd["index"], len(rnd["pairs"])))

        for pair in rnd["pairs"]:
            if not RT.running:
                break

            # 相邻两次发送之间的随机等待（种子=日期，当天可复现）
            wait_s = float(pair.get("delay_seconds") or 0)
            if wait_s > 0:
                nxt = pair.get("target")
                RT.log("INFO", "等待 %.1f 分钟后发送 -> %s" % (wait_s / 60.0, nxt))
                slept = 0.0
                while slept < wait_s and RT.running:
                    time.sleep(min(2.0, wait_s - slept))
                    slept += 2.0
                if not RT.running:
                    break

            display = pair.get("target")
            wxid = pair.get("username")
            text = pair.get("text")
            pair = dict(pair, index=rnd["index"])
            status = _send_one(engine, display, wxid, text, SHOT_DIR, pair)
            if status == "cancel":
                RT.log("WARN", "已停止：中断于 %s" % display)
                break
            # 老模式也要遵守「连续失败中止」，否则一个打不开的群会一路失败到底
            if status == "fail":
                fail_streak += 1
                if fail_streak_limit > 0 and fail_streak >= fail_streak_limit:
                    RT.log("ERROR", "连续失败 %d 条，已自动中止" % fail_streak)
                    break
            else:
                fail_streak = 0
            time.sleep(1.2)

    RT.log("INFO", "任务结束。成功 %d / 失败 %d" %
           (RT.progress["sent"], RT.progress["failed"]))
    RT.running = False


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatSender/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Electron 以 file:// 加载界面，需要允许跨源访问本服务
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _authed(self) -> bool:
        """校验 token；未配置 token（浏览器模式）时放行。"""
        if not AUTH_TOKEN:
            return True
        got = self.headers.get("X-Token") or ""
        if not got:
            q = parse_qs(urlparse(self.path).query)
            got = (q.get("token") or [""])[0]
        return got == AUTH_TOKEN

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain; charset=utf-8")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p.startswith("/api/") and not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if p in ("/", "/index.html"):
                return self._file(os.path.join(FRONTEND_DIR, "index.html"),
                                  "text/html; charset=utf-8")
            # 前端静态资源（设置页拆出去后需要：settings.html / common.css / common.js）
            # 只放行 frontend/ 下这几种扩展名，且拒绝任何带 .. 的路径 ——
            # 简单白名单比「按路径拼接再信任」安全得多。
            if p.startswith("/") and not p.startswith("/api/") \
                    and not p.startswith("/shots/"):
                rel = p.lstrip("/")
                if rel and ".." not in rel and "/" not in rel:
                    name, ext = os.path.splitext(rel)
                    ctype = {".html": "text/html; charset=utf-8",
                             ".css": "text/css; charset=utf-8",
                             ".js": "application/javascript; charset=utf-8"}.get(ext.lower())
                    if ctype:
                        full = os.path.join(FRONTEND_DIR, rel)
                        if os.path.isfile(full):
                            return self._file(full, ctype)
            if p == "/api/paths":
                return self._json(userdata.describe())
            if p == "/api/accounts":
                return self._json({"accounts": contacts_mod.list_local_accounts()})
            if p == "/api/contacts":
                acc = (q.get("account") or [None])[0]
                return self._json(contacts_mod.get_contacts(acc))
            if p == "/api/messages":
                return self._json(planner.load_messages_detail(MESSAGES_FILE, read_mode()))
            if p == "/api/status":
                with RT.lock:
                    return self._json({"running": RT.running, "progress": RT.progress,
                                       "logs": RT.logs[-120:]})
            if p == "/api/wechat":
                h = find_wechat_window()
                return self._json({"found": bool(h), "hwnd": h})
            if p == "/api/campaign":
                # 内容编排预览：各天引导语、正文、钩子 + 当前调度配置 + 本周计数
                detail = campaign_mod.load_campaign_detail(CAMPAIGN_FILE)
                detail.update(schedule_config())
                detail["week"] = scheduler.week_key()
                detail["state"] = scheduler.load_state(DATA_DIR).get(
                    "weekly", {}).get(scheduler.week_key(), {})
                return self._json({"ok": True, **detail})
            if p == "/api/schedule":
                # 读取当前调度配置
                return self._json({"ok": True, **schedule_config()})
            if p == "/api/settings":
                # 全部设置：当前值 + 表单描述（分组）
                return self._json({"ok": True, **settings_mod.describe(DATA_DIR)})
            if p == "/api/auto":
                # 自动运行状态：该不该跑、为什么、上次结果、已保存的目标
                return self._json({"ok": True, **AUTORUN.status()})
            if p == "/api/doctor":
                # 健康自检：一次把依赖链上的关键点全查一遍
                sc = schedule_config()
                acc = (q.get("account") or [None])[0]
                return self._json({"ok": True, **doctor_mod.run(
                    DATA_DIR, SHOT_DIR, acc,
                    int(sc.get("shot_keep_days") or 0))})
            if p == "/api/failures":
                # 查看待补发的失败记录（GET 读接口，必须放在 do_GET）
                return self._json({"ok": True,
                                   "items": failures_mod.pending(DATA_DIR),
                                   "stats": failures_mod.stats(DATA_DIR)})
            if p == "/api/backup":
                # 已有备份列表
                return self._json({"ok": True,
                                   "backups": backup_mod.list_backups(DATA_DIR),
                                   "files": list(backup_mod.BACKUP_FILES)})
            if p == "/api/app":
                # 程序自身信息：版本号、发布页、备用下载渠道（百度网盘）。
                st = settings_mod.load(DATA_DIR)
                return self._json({
                    "ok": True,
                    "version": APP_VERSION or "未知",
                    "repo": update_mod.REPO,
                    "releases_url": update_mod.RELEASES_PAGE,
                    "pan_url": str(st.get("pan_url") or ""),
                    "pan_code": str(st.get("pan_code") or ""),
                    "shot_enabled": bool(st.get("shot_enabled", True)),
                })
            if p == "/api/wxdir":
                # 微信数据目录诊断。?manual=xxx 可以先试一个候选路径再决定要不要保存。
                manual = (q.get("manual") or [""])[0]
                if not manual:
                    manual = str(settings_mod.get(DATA_DIR, "wechat_db_dir") or "")
                return self._json({"ok": True, **wxdir.diagnose(manual)})
            if p == "/api/update/check":
                # 查 GitHub 最新正式版并与当前版本比较。
                # 允许 ?refresh=1 绕过 5 分钟缓存（用户手点「再查一次」时用）。
                refresh = (q.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                if not APP_VERSION:
                    return self._json({
                        "ok": False,
                        "error": "拿不到当前版本号（未以桌面版启动），无法比较",
                        "url": update_mod.RELEASES_PAGE,
                    })
                return self._json(update_mod.check(APP_VERSION, use_cache=not refresh))
            if p == "/api/update/download/status":
                return self._json({"ok": True, **update_mod.download_status()})
            if p == "/api/notify/pending":
                # 待弹通知的高意向线索（前端轮询这个）
                st = settings_mod.load(DATA_DIR)
                enabled = bool(st.get("lead_notify"))
                items = leads_mod.pending_notify(DATA_DIR, 5) if enabled else []
                return self._json({
                    "ok": True, "enabled": enabled,
                    "interval": int(st.get("lead_notify_seconds") or 60),
                    "items": items, "count": len(items),
                })
            if p == "/api/targets":
                # 目标投放状态（暂停/降频），以及报告给出的砍群建议
                return self._json({
                    "ok": True,
                    "targets": targets_mod.describe(DATA_DIR),
                    "modes": [{"key": m, "cn": targets_mod.MODE_CN[m]}
                              for m in targets_mod.MODES],
                    "cut_candidates": report_mod.weekly_report(
                        DATA_DIR).get("cut_candidates") or [],
                })
            if p == "/api/report":
                # 周报：投放量 + 线索量 + 6 项 KPI + 砍群建议
                return self._json({"ok": True, **report_mod.weekly_report(DATA_DIR)})
            if p == "/api/inbox":
                # 接收状态：各会话游标 + 自动轮询状态
                st = inbox_mod.load_state(DATA_DIR)
                chats = st.get("chats") or {}
                return self._json({
                    "ok": True,
                    "chats": len(chats),
                    "detail": {v.get("name") or k: v.get("cursor")
                               for k, v in chats.items()},
                    "poller": INBOX_POLLER.status(),
                })
            if p == "/api/leads":
                # 线索表：列表 + 统计（可按状态/意图过滤）
                status = (q.get("status") or [None])[0]
                intent = (q.get("intent") or [None])[0]
                limit = int((q.get("limit") or ["200"])[0] or 200)
                return self._json({
                    "ok": True,
                    "leads": leads_mod.list_leads(DATA_DIR, status=status,
                                                  intent=intent, limit=limit),
                    "stats": leads_mod.stats(DATA_DIR),
                    "statuses": [{"key": k, "cn": leads_mod.STATUS_CN[k]}
                                 for k in leads_mod.STATUSES],
                })
            if p.startswith("/shots/"):
                rel = os.path.normpath(p[len("/shots/"):]).replace("..", "")
                return self._file(os.path.join(SHOT_DIR, rel), "image/png")
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            return self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_json()
        try:
            if p.startswith("/api/") and not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if p == "/api/messages":
                # 优先按原始文本保存（保留多行消息的换行）；
                # 兼容旧的 {messages: [...]} 调用方式。
                os.makedirs(DATA_DIR, exist_ok=True)
                mode = body.get("mode")
                if mode in (planner.MODE_LINE, planner.MODE_SEPARATOR):
                    write_settings(message_mode=mode)
                else:
                    mode = read_mode()
                if "text" in body:
                    raw = str(body.get("text") or "")
                else:
                    msgs_in = body.get("messages") or []
                    # 用 --- 连接，避免把多行消息压成一行
                    raw = ("\n%s\n" % planner.MESSAGE_SEPARATOR).join(
                        str(m) for m in msgs_in
                    )
                    if raw:
                        raw += "\n"
                if raw and not raw.endswith("\n"):
                    raw += "\n"
                with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
                    f.write(raw)
                detail = planner.load_messages_detail(MESSAGES_FILE, mode)
                return self._json({"ok": True, "count": detail["count"],
                                   "mode": detail["mode"]})

            if p == "/api/plan":
                msgs = planner.load_messages(MESSAGES_FILE, read_mode())
                targets = body.get("targets") or []
                # 合规校验：方案风控 1 要求群内不得出现违规开票暗示。
                # 在生成计划时就拦，别等发出去才发现。
                # 编排模式还要带上引导语与钩子（它们也是要发出去的内容）。
                check_items = [("消息池", m) for m in msgs]
                if body.get("campaign"):
                    camp_chk = campaign_mod.load_campaign(
                        body.get("campaign_file") or CAMPAIGN_FILE)
                    for k in campaign_mod.WEEKDAY_KEYS:
                        g = camp_chk.guides.get(k) or ""
                        if g:
                            check_items.append(
                                ("引导语(%s)" % campaign_mod.weekday_cn(k), g))
                    for i, h in enumerate(camp_chk.hooks or [], 1):
                        check_items.append(("钩子%d" % i, h))
                guard_hits = guard_mod.check_many(check_items)
                if not guard_hits["ok"]:
                    return self._json({
                        "ok": False, "error": "内容命中违规措辞，已拦下",
                        "guard": guard_hits}, 400)
                # 素材：按界面配置收集，再按目标轮转分配
                media_spec = body.get("media") or {}
                media_items = []
                media_by_target: Dict[str, List[str]] = {}
                if media_spec and targets:
                    try:
                        media_items = media_mod.collect(media_spec)
                    except Exception:
                        media_items = []
                    if media_items:
                        mode = (media_spec.get("assign_mode") or "rotate").lower()
                        seed = None
                        if mode == "shuffle":
                            seed = int(time.strftime("%Y%m%d"))
                        assign = media_mod.assign(media_items, targets, mode=mode, seed=seed)
                        media_by_target = {
                            k: [x.path for x in v] for k, v in assign.items()
                        }
                # ── 内容编排模式：绝对时间 + 双波次剧本 ──
                # body.campaign = true 时走这条；否则仍走原来的「轮次 + 相对间隔」
                if body.get("campaign"):
                    camp_path = body.get("campaign_file") or CAMPAIGN_FILE
                    camp = campaign_mod.load_campaign(camp_path)
                    cfg = schedule_config()
                    # body 里的值优先，没给就用配置里的
                    start_at = body.get("start_at") or cfg["start_at"]
                    hook_delay = float(body.get("hook_delay_min")
                                       or cfg["hook_delay_minutes"])
                    hook_days = body.get("hook_days") or cfg["hook_days"]
                    weekly_cap = int(body.get("weekly_cap")
                                     if body.get("weekly_cap") is not None
                                     else cfg["weekly_cap"])
                    daily_cap = int(body.get("daily_cap")
                                    if body.get("daily_cap") is not None
                                    else cfg["daily_cap"])
                    run_cap = int(body.get("run_cap")
                                  if body.get("run_cap") is not None
                                  else cfg["run_cap"])
                    run_days = body.get("run_days") or cfg.get("run_days") or []
                    plan = planner.build_campaign_plan(
                        targets=targets,
                        campaign_obj=camp,
                        start_at=start_at,
                        interval_min=float(body.get("interval_min")
                                           or cfg["interval_min"]),
                        interval_max=float(body.get("interval_max")
                                           or cfg["interval_max"]),
                        hook_delay_min=hook_delay,
                        hook_days=hook_days,
                        media_by_target=media_by_target,
                        weekly_cap=weekly_cap,
                        daily_cap=daily_cap,
                        run_cap=run_cap,
                        enforce_window=bool(cfg["enforce_window"]),
                        window_start=cfg["send_window_start"],
                        window_end=cfg["send_window_end"],
                        data_dir=DATA_DIR,
                        # 「素材与文字顺序」必须传下去；否则编排模式下界面选了
                        # 「先发文字」也会被忽略（planner 里原来硬编码 True）。
                        media_first=bool(media_spec.get("media_first", True)),
                    )
                    # 「只在指定星期执行」：今天不在名单里就产出空计划并说明原因
                    plan["run_days"] = run_days
                    if run_days and plan.get("weekday") not in run_days:
                        plan["events"] = []
                        plan["total_messages"] = 0
                        plan["main_count"] = 0
                        plan["hook_count"] = 0
                        plan["blocked_reason"] = (
                            "今天是%s，不在允许投放的星期（%s）里"
                            % (plan.get("weekday_cn"),
                               "、".join(run_days)))
                    # 目标全被「暂停/降频」过滤掉时也要说清原因，
                    # 否则用户看到「计划为空」只会以为程序坏了
                    if not plan.get("events") and plan.get("skipped_by_mode"):
                        plan["blocked_reason"] = (
                            "目标都被暂停或降频跳过了："
                            + "；".join("%s（%s）" % (k, v) for k, v
                                       in list(plan["skipped_by_mode"].items())[:4]))
                    plan["media"] = media_mod.describe(media_items) if media_items else None
                    plan["campaign_file"] = camp_path
                    plan["guard"] = guard_hits
                    RT.plan = plan
                    return self._json({"ok": True, "plan": plan})

                plan = planner.build_plan(
                    messages=msgs, targets=targets,
                    rounds=int(body.get("rounds") or planner.DEFAULT_ROUNDS),
                    per_round=int(body.get("per_round") or planner.DEFAULT_PER_ROUND),
                    interval_min=float(body.get("interval_min") or planner.DEFAULT_INTERVAL_MIN),
                    interval_max=float(body.get("interval_max") or planner.DEFAULT_INTERVAL_MAX),
                    media_by_target=media_by_target,
                    media_first=bool(media_spec.get("media_first", True)),
                )
                plan["media"] = media_mod.describe(media_items) if media_items else None
                plan["guard"] = guard_hits
                RT.plan = plan
                return self._json({"ok": True, "plan": plan})

            if p == "/api/guard":
                # 单独校验一段文本（界面里"改完先查一下"用）
                body = body or {}
                text = body.get("text") or ""
                if not text:
                    return self._json({"ok": False, "error": "缺少 text"}, 400)
                return self._json({"ok": True, **guard_mod.check(text)})

            if p == "/api/settings":
                # 保存全部设置（逐项校验，非法值不写入）
                body = body or {}
                r = settings_mod.save(DATA_DIR, body)
                if not r.get("ok"):
                    return self._json({"ok": False, "error": "设置有非法值",
                                       "errors": r.get("errors"),
                                       "settings": r.get("settings")}, 400)
                # 改了微信目录 / 截图开关要立刻生效：
                # 目录换了必须清缓存，否则还在用旧目录的 WeChatDB 实例。
                if "wechat_db_dir" in (body or {}):
                    try:
                        wxdir.clear_cache()
                    except Exception:
                        pass
                    contacts_mod.clear_cache()
                return self._json({"ok": True, "saved": r.get("saved"),
                                   **settings_mod.describe(DATA_DIR)})

            if p == "/api/settings/reset":
                # 恢复默认值；body.keys 为空则全部恢复
                body = body or {}
                keys = body.get("keys")
                settings_mod.reset(DATA_DIR, keys)
                return self._json({"ok": True, **settings_mod.describe(DATA_DIR)})

            if p == "/api/auto/targets":
                # 保存自动任务的目标名单（含 wxid，执行时要精确发送）
                body = body or {}
                entries = []
                for t in (body.get("targets") or []):
                    if isinstance(t, dict) and t.get("name"):
                        entries.append({"name": t["name"],
                                        "username": t.get("username") or ""})
                    elif isinstance(t, str) and t:
                        entries.append({"name": t, "username": ""})
                autorun_mod.save_task(DATA_DIR, targets=entries)
                return self._json({"ok": True, "saved": len(entries),
                                   **AUTORUN.status()})

            if p == "/api/auto/run-now":
                # 立刻按自动任务的配置跑一轮（用于验证，不等时间点）
                task = autorun_mod.load_task(DATA_DIR)
                if not (task.get("targets") or []):
                    return self._json({"ok": False,
                                       "error": "自动任务还没有目标，请先保存名单"}, 400)
                if RT.running:
                    return self._json({"ok": False, "error": "已有任务在执行"}, 409)
                fired = _auto_run(task)
                if not fired:
                    return self._json({"ok": False,
                                       "error": "未能启动（详见运行日志）"}, 500)
                return self._json({"ok": True, "started": True})

            if p == "/api/auto/reset-day":
                # 清掉「今天已跑过」的标记，允许今天再跑一次
                autorun_mod.save_task(DATA_DIR, last_run_day="", last_result="")
                return self._json({"ok": True, **AUTORUN.status()})

            if p == "/api/cleanup":
                # 手动清理一次（界面上的「立即清理」按钮）
                return self._json({"ok": True, **_housekeep("手动")})

            if p == "/api/backup/export":
                # 立即备份一次，返回包路径与清单
                return self._json(backup_mod.export(DATA_DIR))

            if p == "/api/backup/import":
                # 从备份恢复（导入前会自动快照当前状态）
                body = body or {}
                path = str(body.get("path") or "")
                if not path:
                    return self._json({"ok": False, "error": "缺少备份文件路径"}, 400)
                if RT.running:
                    return self._json({"ok": False,
                                       "error": "任务执行中，请先停止再恢复"}, 409)
                r = backup_mod.import_(DATA_DIR, path)
                return self._json(r, 200 if r.get("ok") else 400)

            if p == "/api/targets/state":
                # 设置目标投放状态：active / reduced / paused
                body = body or {}
                names = body.get("targets") or []
                if isinstance(names, str):
                    names = [names]
                mode = str(body.get("mode") or "")
                if mode not in targets_mod.MODES:
                    return self._json({"ok": False,
                                       "error": "mode 必须是 active/reduced/paused"}, 400)
                n = targets_mod.set_mode(DATA_DIR, names, mode,
                                         str(body.get("reason") or ""))
                return self._json({"ok": True, "changed": n,
                                   "targets": targets_mod.describe(DATA_DIR)})

            if p == "/api/targets/apply-cuts":
                # 把报告的砍群建议一键落成状态（默认降频，不直接暂停）
                body = body or {}
                mode = str(body.get("mode") or targets_mod.MODE_REDUCED)
                if mode not in targets_mod.MODES:
                    return self._json({"ok": False, "error": "mode 非法"}, 400)
                cands = report_mod.weekly_report(DATA_DIR).get("cut_candidates") or []
                n = targets_mod.apply_cut_candidates(DATA_DIR, cands, mode)
                return self._json({"ok": True, "applied": n,
                                   "targets": targets_mod.describe(DATA_DIR)})

            if p == "/api/update/download":
                # 下载新版本安装包到 temp/update/。
                # 注意：**不接受前端传 URL**，只接受资产名，服务端自己去 Release 里取，
                # 避免这个接口被当成 SSRF 跳板。
                body = body or {}
                name = str(body.get("asset") or "")
                return self._json(update_mod.start_download(DATA_DIR, name))

            if p == "/api/notify/ack":
                # 标记已通知，避免同一线索反复弹窗
                body = body or {}
                ids = body.get("ids") or []
                if isinstance(ids, str):
                    ids = [ids]
                n = leads_mod.mark_notified(DATA_DIR, ids)
                return self._json({"ok": True, "acked": n})

            if p == "/api/failures/retry":
                # 一键补发
                if RT.running:
                    return self._json({"ok": False, "error": "已有任务在执行"}, 409)
                fired = _retry_run()
                if not fired:
                    return self._json({"ok": False,
                                       "error": "没有待补发的记录，或未找到微信窗口"}, 400)
                return self._json({"ok": True, "started": True})

            if p == "/api/failures/clear":
                # 清理失败记录（默认只删已解决的）
                body = body or {}
                n = failures_mod.clear(DATA_DIR,
                                       only_resolved=bool(body.get("only_resolved", True)))
                return self._json({"ok": True, "cleared": n,
                                   "stats": failures_mod.stats(DATA_DIR)})

            if p == "/api/media":
                # 预览素材：只收集不发送，让用户确认选对了
                spec = body.get("media") or body or {}
                items = media_mod.collect(spec)
                return self._json({"ok": True, "media": media_mod.describe(items)})

            if p == "/api/schedule":
                body = body or {}
                # 只挑出调度相关字段，校验与写入交给 settings 模块
                keys = ("start_at", "hook_delay_minutes", "weekly_cap", "daily_cap",
                        "run_cap", "max_fail_streak", "interval_min", "interval_max",
                        "cooldown_every", "cooldown_seconds", "retry_times",
                        "retry_delay_seconds", "verify_timeout", "clear_input_before",
                        "auto_enabled", "auto_check_seconds", "inbox_auto_poll",
                        "inbox_poll_seconds", "hook_days", "run_days",
                        "enforce_window", "send_window_start", "send_window_end",
                        "log_keep_days", "shot_keep_days",
                        "keywords_need", "keywords_hook")
                patch = {k: body[k] for k in keys if k in body}
                r = write_settings(**patch) if patch else {"ok": True, "errors": {}}
                if not r.get("ok"):
                    return self._json({"ok": False, "error": "设置有非法值",
                                       "errors": r.get("errors")}, 400)
                return self._json({"ok": True, **schedule_config()})

            if p == "/api/schedule/reset":
                # 清空本周投放计数（比如手动重跑当天）
                state = scheduler.load_state(DATA_DIR)
                wk = scheduler.week_key()
                (state.get("weekly") or {}).pop(wk, None)
                scheduler.save_state(DATA_DIR, state)
                return self._json({"ok": True, "week": wk})

            if p == "/api/inbox/poll":
                # 轮询私聊新消息（读库，不注入进程）
                body = body or {}
                st = settings_mod.load(DATA_DIR)
                r = inbox_mod.poll_new(
                    account=body.get("account"),
                    data_dir=DATA_DIR,
                    include_history=bool(body.get("include_history")),
                    need_keywords=st.get("keywords_need"),
                    hook_keywords=st.get("keywords_hook"),
                )
                if r.get("error"):
                    return self._json({"ok": False, "error": r["error"]}, 500)
                items = r.get("items") or []
                # 命中的线索登记进线索表
                try:
                    r["added"] = leads_mod.add_items(items, DATA_DIR)
                except Exception as exc:
                    r["leads_error"] = str(exc)
                    r["added"] = 0
                r.pop("state", None)
                return self._json({"ok": True, **r})

            if p == "/api/inbox/reset":
                n = inbox_mod.reset_cursors(DATA_DIR)
                return self._json({"ok": True, "cleared": n})

            if p == "/api/leads/update":
                body = body or {}
                lid = str(body.get("id") or "")
                if not lid:
                    return self._json({"ok": False, "error": "缺少 id"}, 400)
                patch = {k: body[k] for k in ("status", "note", "source_group", "name")
                         if k in body}
                if not leads_mod.update_lead(DATA_DIR, lid, **patch):
                    return self._json({"ok": False,
                                       "error": "线索不存在或状态非法"}, 400)
                return self._json({"ok": True,
                                   "lead": leads_mod.get_lead(DATA_DIR, lid)})

            if p == "/api/leads/export":
                body = body or {}
                # 默认导到安装目录下的 data/，用户可指定其它路径
                out = body.get("path") or os.path.join(DATA_DIR, "leads.csv")
                r = leads_mod.export_csv(DATA_DIR, out,
                                         status=body.get("status"))
                return self._json(r, 200 if r.get("ok") else 500)

            if p in ("/api/remark/preview", "/api/remark/start"):
                if not isinstance(body, dict):
                    return self._json({"ok": False, "error": "无效的请求内容"}, 400)
                try:
                    keywords = remark_send.parse_keywords(body.get("keywords"))
                    account, matches = _remark_preview(body.get("account"), keywords)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                if p == "/api/remark/preview":
                    return self._json({"ok": True, "matches": matches})

                usernames = body.get("usernames")
                if not isinstance(usernames, list) or not usernames or any(
                        not isinstance(username, str) or not username for username in usernames):
                    return self._json({"ok": False, "error": "请选择至少一位符合条件的联系人"}, 400)
                selected_ids = set(usernames)
                selected = [m for m in matches if m["username"] in selected_ids and m["eligible"]]
                if len(selected) != len(selected_ids):
                    return self._json({"ok": False, "error": "联系人条件已变化，请重新预览"}, 409)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"ok": False, "error": "请填写要发送的文字"}, 400)
                text = text.replace("\r\n", "\n").strip()
                guard = guard_mod.check(text)
                if not guard["ok"]:
                    return self._json({"ok": False, "error": guard["reason"]}, 400)
                cap = schedule_config()["run_cap"]
                if cap and len(selected) > cap:
                    return self._json({"ok": False, "error": "所选人数超过单次运行上限（%d）" % cap}, 400)
                main = find_wechat_window(restore=False)
                if not main:
                    return self._json({"ok": False, "error": "未找到微信窗口，请先打开已登录的微信"}, 400)
                if not _launch_send_thread(_run_remark_send, account, body["account"], main,
                                           selected, text):
                    return self._json({"ok": False, "error": "任务已在运行中"}, 409)
                return self._json({"ok": True, "queued": len(selected)})

            if p == "/api/start":
                if RT.running:
                    return self._json({"ok": False, "error": "任务已在运行中"}, 409)
                plan = body.get("plan") or RT.plan
                if not plan or not (plan.get("assignments") or plan.get("events")):
                    return self._json({"ok": False, "error": "计划为空"}, 400)
                if not find_wechat_window():
                    return self._json({"ok": False, "error": "未找到微信窗口，请先登录微信"}, 400)
                if not _launch_send_thread(_run_plan, plan):
                    return self._json({"ok": False, "error": "任务已在运行中"}, 409)
                return self._json({"ok": True})

            if p == "/api/stop":
                RT.running = False
                RT.log("WARN", "已请求停止")
                return self._json({"ok": True})

            return self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            return self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)


def main(host="127.0.0.1", port=8791, token="", quiet=False, version=""):
    """启动服务。port=0 表示由系统分配空闲端口（Electron 用这种模式）。"""
    global AUTH_TOKEN, APP_VERSION
    AUTH_TOKEN = token or ""
    APP_VERSION = (version or "").strip().lstrip("vV")

    srv = ThreadingHTTPServer((host, port), Handler)
    actual_port = srv.server_address[1]

    # 拉起自动运行守护。它每轮自己读设置里的 auto_enabled，
    # 所以这里无条件启动 —— 用户中途打开开关无需重启程序。
    try:
        AUTORUN.start()
        if schedule_config().get("auto_enabled"):
            print("  自动运行: 已启用，到点自动执行", flush=True)
    except Exception as exc:
        print("  自动运行守护启动失败: %s" % exc, flush=True)

    # 启动时按保留天数回收一次（长期常驻程序，磁盘不能只涨不落）
    try:
        _housekeep("启动")
    except Exception:
        pass

    # 自动轮询私聊（同样自己读开关，用户中途打开无需重启）
    try:
        INBOX_POLLER.start()
    except Exception as exc:
        print("  自动轮询启动失败: %s" % exc, flush=True)

    # Electron 主进程靠这一行拿到端口
    print("PORT=%d" % actual_port, flush=True)
    if not quiet:
        print("  浏览器打开:  http://%s:%d" % (host, actual_port), flush=True)
        print("  用户文件目录: %s" % userdata.data_root(), flush=True)
        print("  关闭此窗口即停止服务", flush=True)
        print("-" * 64, flush=True)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="微信定时群发控制台 - 本地服务")
    ap.add_argument("port", nargs="?", type=int, default=8791,
                    help="端口，0 表示自动分配（默认 8791）")
    ap.add_argument("--token", default="", help="API 鉴权 token（留空则不校验）")
    ap.add_argument("--data-root", default="", help="用户文件根目录（默认安装目录）")
    ap.add_argument("--quiet", action="store_true", help="只输出 PORT=")
    ap.add_argument("--version", default="",
                    help="当前程序版本（Electron 传 app.getVersion()，用于检查更新）")
    args = ap.parse_args()

    if args.data_root:
        os.environ["WCS_DATA_ROOT"] = args.data_root
        userdata.configure(args.data_root)

    main(port=args.port, token=args.token, quiet=args.quiet,
         version=args.version)
