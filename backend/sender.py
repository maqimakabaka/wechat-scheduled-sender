# -*- coding: utf-8 -*-
"""发送引擎：基于 UIA 语义控件驱动微信客户端（不使用鼠标坐标 / 不依赖窗口位置）。

核心机制：
1. 热激活微信 Qt accessibility gate（Weixin.dll 里的 active byte），让 mmui:: 控件树
   实体化。微信重启/升级/重登后该字节会重置为 0，因此每次运行前都必须重写并校验。
2. 通过 AutomationId 定位控件（session_list / chat_input_field / 发送按钮），
   用 InvokePattern / ValuePattern 语义调用 —— 窗口移动、缩放完全不影响。
3. 打开会话**只用显示名**，不用 wxid：微信搜索框不认 wxid，输入它只会命中
   「搜索网络结果」并打开搜一搜面板。wxid 仅用于发送后的数据库回读校验。
4. 发送后可选做数据库回读校验（真实落库才算成功），并截图存档。
"""
from __future__ import annotations

import ctypes
import difflib
import os
import re
import time
from typing import Any, Dict, List, Optional

import uiautomation as auto

try:
    import win32gui
    import win32con
except Exception:  # pragma: no cover
    win32gui = None

WECHAT_MAIN_CLASS = "Qt51514QWindowIcon"
# 注意：类名里的数字是 **Qt 版本**（Qt51514 = Qt 5.15.14，Qt624 = Qt 6.24），
# 微信换 Qt 版本时这个类名就会变（实测 4.1.15.13 是 Qt51514QWindowIcon）。
# 所以窗口匹配不能认死一个类名，改为「类名含 QWindowIcon」+ 标题含微信 + 尺寸够大。
WECHAT_CLASS_HINT = "QWindowIcon"
WECHAT_TITLE_HINTS = ("微信", "Weixin", "WeChat")
# 主窗口最小边长。窗口被拖小是常态（实测见过 296x388），门槛定高了会「找不到微信」。
MIN_WINDOW_SIDE = 200
PW_RENDERFULLCONTENT = 0x00000002


# ---------------------------------------------------------------------------
# 名称归一化：微信搜索框对长名字 / emoji / 特殊符号很不友好
# ---------------------------------------------------------------------------
# 去掉空白、零宽字符、emoji、以及各种中英文装饰符号。
# 群名里常见的 【】& 1️⃣ 之类会让「整名搜索」搜不到，
# 所以改成「用归一化后的短关键词检索，再用模糊匹配认行」。
_STRIP_RE = re.compile(
    r"[\s\u200b-\u200f\u2028-\u202f\u20d0-\u20ff\u2600-\u27bf\ufe0e\ufe0f"
    r"\U0001F000-\U0001FAFF"
    r"【】\[\]（）()<>《》「」『』·、，,。.：:；;！!？?…\-—_=+*#@/\\|~^&%$'\"`^]+"
)


def normalize_name(s: Optional[str]) -> str:
    """把名称压成「只剩实义字符」的形式，用于搜索与比较。"""
    return _STRIP_RE.sub("", s or "")


def search_keys(name: str, max_len: int = 6) -> List[str]:
    """给一个显示名，产出若干个检索词（按可用性排序）。

    优先用归一化后的前几个字（区分度通常最高）；不行再试尾部和原文。
    """
    norm = normalize_name(name)
    out: List[str] = []
    cands = []
    if norm:
        cands.append(norm[:max_len])
        if len(norm) > max_len:
            cands.append(norm[-max_len:])
        cands.append(norm)
    # 注意：不要把「带符号的原文」作为候选。
    # 输入 `上海财税-【` 这类残缺符号串时，微信只会给「搜索网络结果」，
    # 点进去就是搜一搜面板（实测）。
    for k in cands:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
def init_com() -> None:
    """在当前线程初始化 COM。

    uiautomation 依赖 COM；在后台线程里创建控件对象前必须调用，
    否则会拿不到控件树（表现为「gate 未生效」）。可重复调用。
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(0, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        pass


def find_wechat_window(restore: bool = True) -> Optional[int]:
    """找到微信主窗口句柄。

    注意最小化的情况：窗口最小化时 GetWindowRect 会返回图标尺寸（实测 160x28），
    如果按「尺寸必须 > 300」直接筛掉，就会得出「没找到微信」的错误结论 ——
    而恢复逻辑在查找之后才执行，永远没机会跑。
    所以这里遇到尺寸过小的候选时，先判断是否最小化，是就恢复再测一次。
    """
    if win32gui is None:
        return None
    found: List[tuple] = []

    def _size(hwnd):
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return r - l, b - t

    def _cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            # 兼容不同 Qt 版本的类名（Qt51514QWindowIcon / Qt624QWindowIcon …）
            if WECHAT_CLASS_HINT not in cls or "QWindowTool" in cls:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not any(x in title for x in WECHAT_TITLE_HINTS):
                return True

            w, h = _size(hwnd)
            if (w < 300 or h < 300) and restore and win32gui.IsIconic(hwnd):
                # 最小化 → 先恢复，再重新量尺寸
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.35)
                except Exception:
                    pass
                w, h = _size(hwnd)

            # 门槛不能定太高：窗口被拖小是常态（实测见过 296x388），
            # 只要是个能用的窗口就收进来，最后再挑最大的那个。
            if w < MIN_WINDOW_SIDE or h < MIN_WINDOW_SIDE:
                return True
            found.append((hwnd, w * h))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return max(found, key=lambda x: x[1])[0] if found else None


def find_independent_chat_windows(main_hwnd: int, usernames: set[str]) -> dict[str, int]:
    """仅识别微信主进程内、带原生会话 ID 的独立聊天窗口。"""
    init_com()
    try:
        process_id = auto.ControlFromHandle(main_hwnd).ProcessId
        windows = auto.GetRootControl().GetChildren()
    except Exception:
        return {}
    found = {}
    prefix = "ChatSingleWindow"
    for window in windows:
        try:
            if window.ProcessId != process_id or window.ClassName != "mmui::ChatSingleWindow":
                continue
            aid = window.AutomationId or ""
            username = aid.removeprefix(prefix)
            if aid.startswith(prefix) and username in usernames and window.NativeWindowHandle:
                found[username] = window.NativeWindowHandle
        except Exception:
            continue
    return found


def ensure_window_visible(hwnd: int) -> None:
    if win32gui is None:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.8)


# 本程序完整验证过的微信版本。微信升级会改 Weixin.dll，
# accessibility gate（控件树开关）的位置随之变化，新版本可能扫不准。
VERIFIED_WECHAT_VERSIONS = ("4.1.15.13", "4.1.15.53")


_VERSION_DIR_RE = re.compile(r"^\d+\.\d+(\.\d+)*$")


def detect_wechat_version() -> Optional[str]:
    """推断正在运行的微信版本（形如 ``4.2.0.11``）。

    微信 4.x 有两种安装结构，两种都要认：
      * ``<安装目录>\\<版本>\\Weixin.exe``     —— exe 就在版本目录里
      * ``<安装目录>\\Weixin.exe`` + ``<安装目录>\\<版本>\\Weixin.dll``
        —— exe 是启动器，真正带版本的是子目录里的 Weixin.dll
        （本机就是这种：``C:\\soft\\Weixin\\Weixin.exe`` 与 ``4.2.0.11\\``）
    """
    try:
        import psutil
    except Exception:
        return None

    exe = None
    try:
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if (p.info.get("name") or "").lower().startswith("weixin"):
                    e = p.info.get("exe")
                    if e:
                        exe = e
                        break
            except Exception:
                continue
    except Exception:
        return None
    if not exe:
        return None

    base = os.path.dirname(exe)
    # 情况 A：exe 自身就在版本目录里
    if _VERSION_DIR_RE.match(os.path.basename(base)):
        return os.path.basename(base)

    # 情况 B：启动器旁边按版本命名的子目录，取 Weixin.dll 最新的那个
    try:
        cands = []
        for n in os.listdir(base):
            full = os.path.join(base, n)
            if not (os.path.isdir(full) and _VERSION_DIR_RE.match(n)):
                continue
            dll = os.path.join(full, "Weixin.dll")
            cands.append((os.path.getmtime(dll) if os.path.exists(dll) else 0, n))
        if cands:
            cands.sort(reverse=True)
            return cands[0][1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# UIA gate 热激活
# ---------------------------------------------------------------------------
def wake_accessibility(hwnd: int, retries: int = 3) -> bool:
    """热激活 accessibility gate 并校验控件树是否实体化。

    微信重启/升级/重登后 gate byte 会重置为 0，控件树退回空壳；
    这里做多次重试，每次重新写入并校验。
    """
    for i in range(retries):
        try:
            from wechatauto import accessibility as _acc
            try:
                _acc.wake([hwnd], set_flag=True, verify=True)
            except Exception:
                pass
        except Exception:
            pass

        if _uia_ready(hwnd, timeout=4.0):
            return True
        # 窗口可能被最小化/遮挡，恢复后再试
        try:
            ensure_window_visible(hwnd)
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _count_mmui(root, max_depth: int = 4) -> int:
    """统计树里出现多少个 mmui:: 控件（gate 生效的直接证据）。"""
    n = 0
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for ch in children:
            try:
                if (ch.ClassName or "").startswith("mmui::"):
                    n += 1
            except Exception:
                pass
            stack.append((ch, depth + 1))
    return n


def _uia_ready(hwnd: int, timeout: float = 5.0, need: int = 3) -> bool:
    """检查控件树是否已实体化（gate 是否生效）。

    判定用两个信号，命中任一即可：

    1. 能按 AutomationId 找到主容器 ``MainView``。它是各页面（聊天/通讯录/
       朋友圈）共同的根容器，gate 生效就必然存在 —— 最可靠。
    2. 树里出现 >= need 个 ``mmui::`` 控件。

    注意第 2 条必须数到第 6 层：``mmui::MainView`` 本身就在第 4 层
    （窗口 → Pane → QWidget → QStackedWidget → MainView），
    只数 4 层会得到很少的计数，从而把「已就绪」误判成「gate 未生效」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is not None:
                if root.GroupControl(searchDepth=30, AutomationId="MainView").Exists(0.4):
                    return True
                if _count_mmui(root, 6) >= need:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def capture_window(hwnd: int, out_path: str) -> bool:
    """PrintWindow 全内容渲染截图（不依赖窗口是否在前台/被遮挡）。"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        from PIL import Image
        import win32ui
    except Exception:
        return False
    if win32gui is None:
        return False

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return False

    hwnd_dc = mfc_dc = save_dc = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)

        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)

        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits,
                               "raw", "BGRX", 0, 1)
        img.save(out_path, "PNG")
        return True
    except Exception:
        return False
    finally:
        try:
            if save_dc:
                save_dc.DeleteDC()
            if mfc_dc:
                mfc_dc.DeleteDC()
            if hwnd_dc:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 发送引擎
# ---------------------------------------------------------------------------
class SendEngine:
    def _uia_alive(self) -> bool:
        """控件树现在还在不在（廉价检查）。"""
        try:
            root = auto.ControlFromHandle(self.hwnd)
            if root is None:
                return False
            if _count_mmui(root, 4) >= 1:
                return True
            return root.GroupControl(searchDepth=30,
                                     AutomationId="MainView").Exists(0.25)
        except Exception:
            return False

    def ensure_alive(self) -> bool:
        """确保控件树可用；不在就重新唤醒 gate（自愈）。

        实测（微信 4.2.0.11）：界面状态变化之后（按 Esc 关浮层、打开搜一搜
        面板等），微信会重建 MMUI 渲染子窗口，控件树随之消失。
        这不是"版本不支持"，因为**重新写一次 gate 字节就能恢复**，
        不必重启微信 —— 所以不能只在启动时唤醒一次。
        """
        if self._uia_alive():
            return True
        for _ in range(3):
            try:
                wake_accessibility(self.hwnd, retries=1)
                ensure_window_visible(self.hwnd)
            except Exception:
                pass
            time.sleep(1.2)
            if self._uia_alive():
                return True
        return False

    def __init__(self, hwnd: Optional[int] = None, should_stop=None,
                 independent_username: Optional[str] = None):
        # 必须先在当前线程初始化 COM，否则后台线程拿不到控件树
        init_com()
        self.hwnd = hwnd or find_wechat_window()
        if not self.hwnd:
            raise RuntimeError("未找到微信主窗口，请先启动并登录微信")
        self.root = None
        # 最近一次 open_chat 失败的原因（供日志展示，便于定位）
        self.last_open_error: Optional[str] = None
        # 会话列表找不到目标时的具体原因（翻了几屏、扫了几项）
        self.last_session_error: Optional[str] = None
        # 取消回调：一次发送最长可能几十秒，不能只在「两条之间」判停止，
        # 必须能在发送过程内部被叫停（否则点了停止还会继续发下一条）。
        self.should_stop = should_stop or (lambda: False)
        if independent_username is None:
            self._ensure_uia()
        else:
            self.root = auto.ControlFromHandle(self.hwnd)
            if self.root is None or not self._is_independent_chat(independent_username) \
                    or self._input_field() is None:
                raise RuntimeError("无法核对独立聊天窗口或其输入框")

    def _is_independent_chat(self, username: str) -> bool:
        return (self.root.ClassName == "mmui::ChatSingleWindow"
                and self.root.AutomationId == "ChatSingleWindow" + username)

    def cancelled(self) -> bool:
        try:
            return bool(self.should_stop())
        except Exception:
            return False

    def _ensure_uia(self) -> None:
        """确保控件树可用。

        实测：gate 的唤醒**首次经常失败、隔几秒重试才成功**
        （用户日志里就是 16:13:57 失败、16:14:57 成功）。
        所以这里分多轮尝试，每轮之间给微信一点时间，
        而不是一次失败就把整个任务判死。
        """
        ensure_window_visible(self.hwnd)
        for attempt in range(1, 5):
            if wake_accessibility(self.hwnd, retries=1):
                self.root = auto.ControlFromHandle(self.hwnd)
                if self.root is not None:
                    return
            if attempt < 4:
                time.sleep(2.5)
        self.root = None

        ver = detect_wechat_version()
        hint = ""
        if ver and ver not in VERIFIED_WECHAT_VERSIONS:
            hint = ("\n检测到微信版本 %s；本程序只在 %s 上完整验证过。"
                    "微信升级会改变 accessibility gate 的位置，"
                    "可能导致控件树无法实体化。"
                    "建议关闭微信自动更新并退回已验证版本。"
                    % (ver, " / ".join(VERIFIED_WECHAT_VERSIONS)))
        elif ver:
            hint = "\n当前微信版本 %s（已验证版本）。" % ver
        raise RuntimeError(
            "微信控件树未能实体化（accessibility gate 未生效）。"
            "请确认微信已登录；若刚重启过微信，重试本程序即可。" + hint
        )

    # -- 控件查找 ----------------------------------------------------------
    def _search_box(self, min_width: int = 24):
        """取搜索输入框；不可用时返回 None（不抛异常）。

        微信 4.2.x 起搜索框控件可能换了类名，所以**不认死一个类名**，
        按可靠性依次尝试多种定位方式。

        另外搜索框会「缩起来」：窗口变窄、或拖动分隔条把会话列表压窄时，
        会折叠成只剩一个放大镜图标。这种控件宽度接近 0，
        SendKeys 打不进字却会让调用方以为「已经输入了关键词」，所以要拒绝。
        """
        cands = []
        try:
            cands.append(self.root.EditControl(
                searchDepth=30, ClassName="mmui::XValidatorTextEdit"))
            # 4.2.x 备选：按 AutomationId 找
            for aid in ("search_input", "search_field", "search_edit"):
                cands.append(self.root.EditControl(searchDepth=30, AutomationId=aid))
            # 再备选：搜索容器里的任意 EditControl
            sf = self.root.GroupControl(searchDepth=30, ClassName="mmui::XSearchField")
            if sf.Exists(0.2):
                cands.append(sf.EditControl(searchDepth=6))
        except Exception:
            return None

        for ed in cands:
            try:
                if ed is None or not ed.Exists(0.25):
                    continue
                r = ed.BoundingRectangle
                if (r.right - r.left) < min_width or (r.bottom - r.top) < 10:
                    continue
                return ed
            except Exception:
                continue
        return None

    def _ensure_search_box(self, timeout: float = 6.0):
        """尽最大努力拿到可用的搜索框，失败返回 None。

        依次尝试：
          1. 直接找（搜索框正常展开时命中）
          2. Ctrl+F —— 微信的「搜索」快捷键，会把搜索框唤出并聚焦
          3. 点 ``mmui::XSearchField`` 容器（把折叠状态展开）
          4. 点容器里的放大镜图标按钮
        每一步之后都重新检查一次，全部失败才放弃。
        """
        deadline = time.time() + timeout

        def ok():
            box = self._search_box()
            return box if box is not None and box.Exists(0.2) else None

        box = ok()
        if box is not None:
            return box

        attempts = (
            ("Ctrl+F 唤出搜索", lambda: auto.SendKeys("{Ctrl}f", waitTime=0.15)),
            ("点击搜索框容器", lambda: self._click_search_field()),
            ("点击放大镜图标", lambda: self._click_search_icon()),
        )
        for name, act in attempts:
            if time.time() >= deadline:
                break
            try:
                act()
            except Exception:
                pass
            time.sleep(0.9)
            box = ok()
            if box is not None:
                return box

        # 最后再等一等（有些布局要等动画结束才把 EditControl 渲染出来）
        while time.time() < deadline:
            box = ok()
            if box is not None:
                return box
            time.sleep(0.3)
        return None

    def _search_field_container(self):
        c = self.root.GroupControl(searchDepth=30, ClassName="mmui::XSearchField")
        return c if c.Exists(0.3) else None

    def _click_search_field(self) -> None:
        c = self._search_field_container()
        if c is not None:
            try:
                c.Click()
            except Exception:
                pass

    def _click_search_icon(self) -> None:
        c = self._search_field_container()
        if c is None:
            return
        try:
            for ch in c.GetChildren():
                if ch.ControlTypeName == "ButtonControl":
                    ch.Click()
                    return
        except Exception:
            pass

    def _search_list(self):
        lst = self.root.ListControl(searchDepth=30, AutomationId="search_list")
        return lst if lst.Exists(0.3) else None

    def _session_list(self):
        lst = self.root.ListControl(searchDepth=30, AutomationId="session_list")
        return lst if lst.Exists(0.3) else None

    def _input_field(self):
        ed = self.root.EditControl(searchDepth=30, ClassName="mmui::ChatInputField")
        return ed if ed.Exists(0.3) else None

    def current_chat_name(self) -> Optional[str]:
        """输入框的 Name 会反映当前会话名，用作发送前校验。"""
        try:
            f = self._input_field()
            if f is not None:
                return (f.Name or "").strip() or None
        except Exception:
            pass
        return None

    # 搜索结果里属于「网络搜索」的入口行。点中它们会打开搜一搜面板
    # （返回公众号文章），而不是打开会话。必须跳过。
    NETWORK_ROW_HINTS = ("搜索网络结果", "搜一搜", "网络结果", "搜索文章", "查找")

    def _panel_present(self) -> bool:
        """当前是否盖着搜索浮层 / 搜一搜面板。

        只在真的有时才按 Esc —— 无脑按 Esc 会把可用的搜索框一起收掉，
        还会让微信重建渲染子窗口、把控件树弄没（实测）。
        """
        try:
            lst = self.root.ListControl(searchDepth=30, AutomationId="search_list")
            if lst.Exists(0.2):
                return True
            for cls in ("mmui::SearchContentPopover", "mmui::SearchContentView"):
                c = self.root.GroupControl(searchDepth=30, ClassName=cls)
                if c.Exists(0.2):
                    return True
        except Exception:
            pass
        return False

    def _escape_panels(self, times: int = 3) -> None:
        """按 Esc 关掉已打开的浮层/搜一搜面板。没有浮层时什么也不做。"""
        if not self._panel_present():
            return
        for _ in range(times):
            try:
                auto.SendKeys("{Esc}", waitTime=0.2)
            except Exception:
                pass
            time.sleep(0.45)
            if not self._panel_present():
                return

    def _dismiss_popover(self) -> None:
        """确保搜索浮层是干净的。

        **搜索框已经可用时不要按 Esc** —— Esc 会把搜索框收起来，
        导致后面 `_ensure_search_box()` 拿不到控件、直接判定失败
        （实测就是这样：开局搜索框可用，一轮 open_chat 后变不可用）。
        """
        if self._search_box() is not None:
            return
        self._escape_panels(2)
        if self._search_box() is None:
            # 可能停在通讯录/朋友圈等页面，切回「微信」标签页
            try:
                tab = self.root.ButtonControl(searchDepth=30, Name="微信")
                if tab.Exists(0.4):
                    tab.Click()
                    time.sleep(1.0)
            except Exception:
                pass
        return

    # -- 打开会话 ----------------------------------------------------------
    def _search_row_split(self, lst):
        """把搜索结果分成「聊天行」与「是否存在网络搜索入口」。

        返回 (list[(name, control)], 是否有网络入口)。
        """
        chats, has_network = [], False
        try:
            items = lst.GetChildren()
        except Exception:
            return chats, has_network
        for it in items:
            try:
                nm = (it.Name or "").strip()
            except Exception:
                continue
            if not nm:
                continue
            if any(h in nm for h in self.NETWORK_ROW_HINTS):
                has_network = True
                continue
            # 会话行会带换行分段（名称\n时间/预览），取首行做名称比较
            chats.append((nm.split("\n")[0].strip(), it))
        return chats, has_network

    def _already_in_chat(self, kw: str, display: Optional[str]) -> bool:
        """当前是否已经就在目标会话里。

        为什么要单独判：**对「已经是当前会话」的列表项再点一次，微信不会
        切回去**（实测点完校验反而失败）。连续给同一个群发多条时就会踩到，
        所以先认一下，已经在就直接算打开成功。
        """
        cur = self.current_chat_name()
        if not cur:
            return False
        names = [n for n in (display, kw) if n]
        cn = normalize_name(cur)
        for n in names:
            if n in cur or cur in n:
                return True
            nn = normalize_name(n)
            if nn and cn and (nn == cn or (len(nn) >= 4 and (nn in cn or cn in nn))):
                return True
        return False

    def open_chat(self, target: str, display: Optional[str] = None,
                  timeout: float = 12.0, exact: bool = False) -> bool:
        """打开会话。

        **只用显示名搜索，绝不用 wxid**：微信搜索框不认 wxid，
        输入 wxid 只会命中「搜索网络结果」，点进去就是搜一搜面板
        （公众号文章列表），而不是会话 —— 这正是实际踩到的 bug。

        也不再用「回车进入」作为兜底：在搜索框里按回车同样会触发网络搜索。
        """
        # 控件树可能在上一次操作后消失了（微信会重建渲染子窗口），先自愈
        self.ensure_alive()

        kw = (display or target or "").strip()
        if not kw:
            return False

        # 只有 wxid / 群 id、没有显示名时，无法在会话列表里按名字找
        if not exact and (kw.endswith("@chatroom") or kw.startswith(("wxid_", "gh_"))):
            self.last_open_error = "只给了 id（%s），没有可用显示名" % kw
            return False

        if self.cancelled():
            self.last_open_error = "已取消"
            return False

        # 已经就在这个会话里就别再点了（再点反而切不过去，见 _already_in_chat）
        already_open = self.current_chat_name() == kw if exact else self._already_in_chat(kw, display)
        if already_open:
            self.last_open_error = None
            return True

        # 1) 会话列表里直接命中（唯一可靠路径，见下方说明）
        clicked = self._click_session_item(kw, exact=exact)
        if clicked:
            if self._verify_chat(kw, display, timeout, exact=exact):
                return True
            self.last_open_error = "点中了「%s」，但会话没有切换过去" % kw
            return False

        # 2) 不再走搜索浮层。
        #    微信 4.2.0.11 实测：点搜索结果行打开的是「搜一搜 / 视频号」面板
        #    （一屏公众号文章），不是会话切换；Invoke、选中、回车同样
        #    都切不过去。硬点只会把界面搞乱、还留下一个无关面板。
        #    所以打开会话**只走会话列表** —— 它包含全部会话（按最近排序），
        #    配合 PageDown 翻页足以覆盖目标。
        self.last_open_error = getattr(self, "last_session_error", None) or (
            "会话列表里没找到「%s」。请先在微信里手动打开一次该群，"
            "或把它置顶，让它出现在列表靠前的位置。" % kw)
        return False

    def _pick_best_row(self, chats, display: str, key: str):
        """从搜索结果里挑出与目标最匹配的一行；不确定时返回 None。

        **宁可失败也不点错** —— 点错群的代价是把消息发到别人的群里。
        所以：归一化后完全相同 → 直接选；否则取相似度最高且明显领先的一行；
        若有两行分数接近（典型的「一堆相似群名」），放弃。
        """
        dn = normalize_name(display)
        scored = []
        for nm, it in chats:
            rn = normalize_name(nm)
            if not rn:
                continue
            if dn and rn == dn:
                score = 1.0
            elif dn and (dn in rn or rn in dn):
                score = 0.92
            else:
                score = difflib.SequenceMatcher(None, dn or key, rn).ratio()
            scored.append((score, nm, it))

        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        if best[0] < 0.60:
            return None
        if len(scored) > 1 and (best[0] - scored[1][0]) < 0.06:
            return None
        return best[2]

    def _scroll_session_list(self, lst) -> bool:
        """把会话列表往下翻一屏（返回是否翻动了）。

        实测（微信 4.2.0.11）：
          * 会话列表**不提供 ScrollPattern**（`GetScrollPattern()` 返回 None）；
          * 滚轮**不可靠** —— 它依赖鼠标当前位置，第一次之后就失效
            （实测连滚 14 次列表纹丝不动）；
          * **列表获焦后按 PageDown 可以稳定翻页** —— 这是唯一稳定的方式。
        所以这里用键盘翻页，不依赖坐标、也不依赖滚轮。
        """
        try:
            before = [c.Name for c in lst.GetChildren()]
        except Exception:
            before = None
        try:
            lst.SetFocus()
            time.sleep(0.2)
            auto.SendKeys("{PageDown}", waitTime=0.1)
        except Exception:
            return False
        time.sleep(0.7)
        try:
            after = [c.Name for c in lst.GetChildren()]
            if before is not None and after == before:
                return False      # 没翻动，说明到底了
        except Exception:
            pass
        return True

    def _click_session_item(self, target: str, max_scroll: int = 30,
                            exact: bool = False) -> bool:
        """在会话列表里找目标并点开。

        会话列表只渲染可见的 9 行（虚拟滚动），目标基本都在可见区之外，
        所以必须翻页找。翻页用键盘 PageDown（见 `_scroll_session_list`）。
        max_scroll 给得比较宽，因为翻到底会自动停下。
        """
        lst = self._session_list()
        if lst is None:
            self.last_session_error = "会话列表不可用（控件树可能没实体化）"
            return False
        want = normalize_name(target)
        scanned = 0

        # 先把列表回到顶部再往下找。
        # 打开会话会让它跳到列表最前，所以每次查找都必须从头开始，
        # 否则会从上次翻到的位置继续，把靠前的目标漏掉。
        try:
            lst.SetFocus()
            time.sleep(0.15)
            auto.SendKeys("{Home}", waitTime=0.1)
            time.sleep(0.5)
        except Exception:
            pass

        for _round in range(max_scroll + 1):
            if self.cancelled():
                self.last_session_error = "已取消"
                return False
            try:
                items = lst.GetChildren()
            except Exception:
                self.last_session_error = "读取会话列表失败"
                return False
            scanned += len(items)
            for it in items:
                try:
                    aid = it.AutomationId or ""
                    name = (it.Name or "").split("\n")[0].strip()
                    hit = name == target or (not exact and aid == "session_item_%s" % target)
                    if not hit and not exact and want and len(want) >= 4:
                        hit = normalize_name(name) == want
                    if not hit:
                        continue
                    # 注意：会话项的 InvokePattern **返回成功但不切换会话**
                    # （微信 4.2.0.11 实测，与「发送」按钮的 InvokePattern
                    # 同样无效），所以主通道用 Click，Invoke 只作兜底。
                    try:
                        it.Click()
                    except Exception:
                        try:
                            it.GetInvokePattern().Invoke()
                        except Exception:
                            pass
                    time.sleep(1.2)
                    return True
                except Exception:
                    continue
            if not self._scroll_session_list(lst):
                break
            time.sleep(0.6)
        self.last_session_error = (
            "会话列表翻了 %d 屏、共扫过 %d 项，没找到「%s」"
            % (max_scroll, scanned, target))
        return False

    def _verify_chat(self, kw: str, display: Optional[str], timeout: float = 12.0,
                     exact: bool = False) -> bool:
        """校验当前会话是否为目标（防止重名发错）。

        搜索关键词往往是「去掉符号后的短片段」（例如用「代博会浦飞出海」去找
        群名「【代博会】浦飞出海&会计工厂1️⃣」），所以除了原文比较，
        还要把两边都归一化后再比一次，否则命中了也认不出来。
        """
        names = [n for n in (kw, display) if n]
        norms = [normalize_name(n) for n in names]
        norms = [d for d in norms if d]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.current_chat_name()
            if exact:
                if cur == kw:
                    return True
                time.sleep(0.4)
                continue
            if cur:
                for n in names:
                    if n in cur or cur in n:
                        return True
                cn = normalize_name(cur)
                if cn:
                    for d in norms:
                        if d == cn:
                            return True
                        # 片段匹配要求至少 4 个字，避免过短关键词误判
                        if len(d) >= 4 and (d in cn or cn in d):
                            return True
            time.sleep(0.4)
        return False

    # -- 发送 --------------------------------------------------------------
    def _send_button(self):
        """定位发送按钮。

        注意：该按钮（mmui::XOutlineButton, Name='发送'）只在输入框有内容时
        才会被渲染出来，因此必须在写入文本之后再查找。
        """
        iv = self.root.GroupControl(searchDepth=30, ClassName="mmui::ChatInputView")
        if not iv.Exists(0.3):
            return None
        btn = iv.ButtonControl(searchDepth=6, Name="发送")
        if btn.Exists(0.4):
            return btn
        # 退化：按类名找
        btn = iv.ButtonControl(searchDepth=6, ClassName="mmui::XOutlineButton")
        return btn if btn.Exists(0.3) else None

    def send_text(self, text: str) -> bool:
        """写入输入框并触发发送。返回 True 表示已触发（不代表已落库）。"""
        field = self._input_field()
        if field is None:
            raise RuntimeError("未找到输入框 chat_input_field")
        try:
            field.Click()
        except Exception:
            pass
        time.sleep(0.2)

        # 清空
        try:
            field.GetValuePattern().SetValue("")
        except Exception:
            try:
                field.SendKeys("{Ctrl}a", waitTime=0.05)
                field.SendKeys("{Delete}", waitTime=0.05)
            except Exception:
                pass

        # 写入（ValuePattern 避开剪贴板与输入法）
        try:
            field.GetValuePattern().SetValue(text)
        except Exception:
            try:
                field.SendKeys(text, waitTime=0.02)
            except Exception:
                return False

        # 等待按钮渲染出来
        time.sleep(0.6)
        btn = self._send_button()

        # 实测结论：该按钮的 InvokePattern 无效（输入框不会清空），
        # 必须用 Click（控件级点击，仍由 UIA 定位，不依赖屏幕坐标）或回车。
        if btn is not None:
            try:
                btn.Click()
                time.sleep(0.5)
                if self.input_is_empty():
                    return True
            except Exception:
                pass

        # 退化：回车发送
        try:
            field.SetFocus()
            field.SendKeys("{Enter}", waitTime=0.1)
            time.sleep(0.5)
            return True
        except Exception:
            return False

    def clear_input(self) -> None:
        """清空输入框。

        发送前必须清：上一轮如果中途失败，输入框可能残留文字或素材预览，
        不清就会和这一条混在一起发出去（实测「纯图片」被上一轮的文件残留
        干扰，判成功失败）。
        """
        try:
            f = self._input_field()
            if f is None:
                return
            f.SetFocus()
            time.sleep(0.2)
            f.SendKeys("{Ctrl}a", waitTime=0.05)
            f.SendKeys("{Delete}", waitTime=0.05)
            time.sleep(0.3)
        except Exception:
            pass

    def verify_media_in_db(self, username: Optional[str], kind: str = "image",
                           timeout: float = 10.0) -> bool:
        """回读数据库，确认最近一条是图片/文件。

        纯素材发送没有文字可回读，只能按类型判断：发送后最近一条消息
        如果是「图片」或「文件/链接/卡片」，就认为发出去了。
        """
        if not username:
            return False
        try:
            import contacts as C
            db = C.get_db(None)
        except Exception:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msgs = db.get_messages(username, limit=3)
            except Exception:
                msgs = []
            for m in msgs or []:
                t = (m.get("type") or "")
                if kind == "image" and "图片" in t:
                    return True
                if kind != "image" and ("文件" in t or "卡片" in t or "链接" in t):
                    return True
            if self.cancelled():
                return False
            time.sleep(1.0)
        return False

    def input_is_empty(self) -> bool:
        """检查输入框是否已清空（发送成功的强信号）。"""
        try:
            f = self._input_field()
            if f is None:
                return False
            vp = f.GetValuePattern()
            # uiautomation 的 ValuePattern 用 Value 属性
            try:
                val = vp.Value
            except Exception:
                val = ""
            return not (val or "").strip()
        except Exception:
            return False

    def verify_in_db(self, username: Optional[str], text: str,
                     timeout: float = 8.0) -> bool:
        """数据库回读校验：确认消息真的落库（即真的发出去了）。"""
        if not username:
            return False
        probe = (text or "")[:16]
        if not probe:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                from wechatauto import WeChatDB
                db = WeChatDB()
                for m in db.get_messages(username, limit=6):
                    if probe in (m.get("content") or ""):
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def send_media(self, path: str, kind: Optional[str] = None) -> bool:
        """把图片或文件发到**当前已打开**的会话。

        机制：放进剪贴板 -> 输入框 Ctrl+V -> 点「发送」。
        为什么不用「发送文件」按钮：那会弹系统文件对话框，要多层 UIA 操作；
        而剪贴板粘贴是纯语义路径，实测图片/文件都能发出去并落库
        （图片落库 type=图片，文件落库 type=文件/链接/卡片）。

        注意：微信输入区工具栏**没有「发送图片」入口**，图片只能粘贴或拖拽，
        拖拽需要精确坐标（本项目的红线），所以剪贴板是唯一可行路径。
        """
        if self.cancelled():
            return False
        try:
            import clipboard as CB
        except Exception:
            return False

        if not path or not os.path.isfile(path):
            return False

        if kind is None:
            try:
                import media as MD
                kind = MD.classify(path)
            except Exception:
                kind = "image" if path.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")) else "file"

        field = self._input_field()
        if field is None:
            return False

        # 1) 放进剪贴板
        ok = CB.set_image(path) if kind == "image" else CB.set_files([path])
        if not ok:
            return False

        # 2) 聚焦输入框 + 粘贴
        try:
            field.SetFocus()
            time.sleep(0.3)
            field.SendKeys("{Ctrl}v", waitTime=0.1)
        except Exception:
            CB.clear()
            return False

        # 3) 等微信渲染预览。发送按钮只在输入框有内容时才渲染，
        #    所以「按钮出现」就是粘贴成功的直接证据。
        deadline = time.time() + 8.0
        btn = None
        while time.time() < deadline:
            if self.cancelled():
                CB.clear()
                return False
            btn = self._send_button()
            if btn is not None:
                break
            time.sleep(0.5)
        if btn is None:
            CB.clear()
            return False

        # 4) 点发送
        try:
            btn.Click()
        except Exception:
            try:
                btn.GetInvokePattern().Invoke()
            except Exception:
                CB.clear()
                return False
        time.sleep(1.5)
        # 清掉剪贴板，避免下一条误粘贴同一张图
        CB.clear()
        return True

    def send_remark_text(self, username: str, display: str, text: str,
                         independent: bool, reserve) -> Dict[str, Any]:
        """备注发送只点击一次。发送前复核会话，保留已有草稿。"""
        def is_target() -> bool:
            if independent:
                return self._is_independent_chat(username)
            return self.root.ClassName == "mmui::MainWindow" and \
                self.current_chat_name() == display

        if not independent:
            if self.root.ClassName != "mmui::MainWindow":
                return {"error": "无法核对微信主窗口"}
            if not self.open_chat(display, exact=True):
                return {"error": self.last_open_error or "无法准确打开联系人会话"}
        if self.cancelled() or not is_target():
            return {"error": "取消或无法核对会话身份"}
        field = self._input_field()
        if field is None:
            return {"error": "找不到会话输入框"}
        value = field.GetValuePattern()
        if value.Value:
            return {"error": "会话中已有草稿，已跳过"}

        attempted = False
        try:
            value.SetValue(text)
            time.sleep(0.6)
            if self._send_button() is None or value.Value.replace("\r\n", "\n") != text \
                    or self.cancelled() or not is_target():
                return {"error": "发送前核对失败，已跳过"}
            if not reserve():
                return {"error": "该联系人已有备注发送尝试记录"}
            button = self._send_button()
            if button is None or value.Value.replace("\r\n", "\n") != text \
                    or self.cancelled() or not is_target():
                return {"error": "发送前会话已变更或任务已停止"}
            attempted = True
            button.Click()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not self.cancelled():
                if not is_target():
                    break
                if self.input_is_empty():
                    return {"ok": True}
                time.sleep(0.4)
            return {"error": "已尝试发送，结果待确认；不会自动重试"}
        finally:
            if not attempted:
                try:
                    if is_target() and value.Value.replace("\r\n", "\n") == text:
                        value.SetValue("")
                except Exception:
                    pass

    def send_to(self, target: str, text: str, display: Optional[str] = None,
                username: Optional[str] = None,
                shot_path: Optional[str] = None,
                verify: bool = True,
                media_path: Optional[str] = None,
                media_kind: Optional[str] = None,
                media_first: bool = True,
                verify_timeout: float = 8.0,
                clear_before: bool = True) -> Dict[str, Any]:
        """打开会话 -> 校验目标 -> 发送（可选先/后发素材）-> 回读校验 -> 截图。

        判定成功的条件：打开成功 + 已触发发送 + (输入框已清空 或 回读命中)

        media_path 非空时会额外发一条图片/文件：
          * media_first=True  —— 先发素材再发文字（更像"配图说明"）
          * media_first=False —— 先发文字再发素材
          * text 为空时只发素材。
        """
        res: Dict[str, Any] = {"target": display or target, "ok": False}
        try:
            # 取消检查要放在最前面：一条发送最长可能几十秒，
            # 点了停止之后绝不能还继续往下发。
            if self.cancelled():
                res["error"] = "已取消"
                res["cancelled"] = True
                return res

            if not self.open_chat(target, display=display):
                if self.cancelled():
                    res["error"] = "已取消"
                    res["cancelled"] = True
                    return res
                res["error"] = "打开会话失败：%s" % (
                    self.last_open_error or "未能校验目标（可能重名或名称变更）")
                return res

            if self.cancelled():
                res["error"] = "已取消"
                res["cancelled"] = True
                return res

            has_text = bool((text or "").strip())
            has_media = bool(media_path)

            # 发送前先清空输入框，避免上一轮的残留混进来。
            # 清完要等一下并确认真的空了 —— 微信清空是异步的，
            # 立刻粘贴会和残留混在一起（纯素材发送就踩过这个坑）。
            # clear_before=False 时跳过（少数情况下用户想保留草稿）。
            if clear_before:
                self.clear_input()
                for _ in range(6):
                    if self.input_is_empty():
                        break
                    time.sleep(0.4)

            def _do_media() -> bool:
                if not has_media:
                    return True
                if self.cancelled():
                    return False
                return self.send_media(media_path, media_kind)

            def _do_text() -> bool:
                if not has_text:
                    return True
                if self.cancelled():
                    return False
                return self.send_text(text)

            if media_first:
                media_ok = _do_media()
                if has_media:
                    res["media"] = media_ok
                if has_media and not media_ok:
                    res["error"] = "发送素材失败（%s）" % os.path.basename(media_path or "")
                    return res
                if has_text:
                    time.sleep(1.0)      # 素材和文字之间留点间隔，避免挤在一起
                text_ok = _do_text()
            else:
                text_ok = _do_text()
                if has_text:
                    time.sleep(1.0)
                media_ok = _do_media()
                if has_media:
                    res["media"] = media_ok
                if has_media and not media_ok:
                    res["error"] = "发送素材失败（%s）" % os.path.basename(media_path or "")
                    return res

            if not text_ok:
                res["error"] = "写入输入框或触发发送失败"
                return res

            # 等输入框被清空（最多 verify_timeout 秒轮询）。
            # 微信发图片/文件后清空输入区需要一点时间，实测约 3~5 秒；
            # 固定 sleep 太短会把成功判成失败（「纯图片」就踩过这个坑）。
            cleared = False
            deadline = time.time() + float(verify_timeout)
            while time.time() < deadline:
                if self.input_is_empty():
                    cleared = True
                    break
                if self.cancelled():
                    break
                time.sleep(0.6)
            res["cleared"] = cleared
            res["btn_gone"] = self._send_button() is None

            verified = False
            # 回读校验也要能被取消叫停（它最长等 verify_timeout）
            if verify and username and not self.cancelled() and (text or "").strip():
                verified = self.verify_in_db(username, text or "")
            res["verified"] = verified

            # 纯素材发送没有文字可回读，按「最近一条是不是图片/文件」判断。
            # 这是纯素材唯一可靠的落地证据（输入框清空也可能是残留被清掉）。
            media_verified = False
            if has_media and not has_text and username and not self.cancelled():
                media_verified = self.verify_media_in_db(
                    username, media_kind or "image",
                    timeout=float(verify_timeout))
            res["media_verified"] = media_verified

            if shot_path:
                res["shot"] = shot_path if capture_window(self.hwnd, shot_path) else None

            # 输入框已清空 = 微信把内容发走了（文字/图片/文件都适用）
            if cleared or verified or media_verified:
                res["ok"] = True
            elif has_text:
                res["error"] = "已触发发送，但输入框仍有内容且数据库未回读（可能发送失败）"
            else:
                res["error"] = "素材已触发发送，但输入区仍有内容（可能没发出去）"
            return res
        except Exception as exc:
            res["error"] = str(exc)
            return res
