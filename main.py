"""通过 hdc 操控 HarmonyOS 设备的 AstrBot 插件。

设计要点：这里不依赖图像识别，而是用 `uitest dumpLayout` 把当前界面读成一棵
**文本控件树**（控件类型 + 文字 + 坐标），模型据此决定点击哪里。这样纯文本模型
也能操作手机；截图只落盘给人看，不作为模型输入。

一个已知的坑：`aa start` 即使 ability 名字不存在也会返回成功，所以启动前必须
从 `bm dump -n <bundle>` 里核对 ability 名，不能盲信调用方传进来的名字。

安全边界：
- 只允许配置里的管理员 QQ 触发（其余人调用直接拒绝）
- 支付/银行类应用在黑名单里，不允许被启动
- 每次工具调用都有超时，避免设备卡住时把对话拖死
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star

# --- 默认配置（可在插件配置里覆盖） ---
# hdc 的查找顺序：插件配置 -> PATH -> DevEco Studio 的常见安装位置。
# 这里刻意不写死某一台机器的路径，换电脑只要 hdc 在 PATH 里就能直接用。
_HDC_CANDIDATES = (
    r"D:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe",
    r"C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe",
    r"C:\Program Files\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe",
    "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
)


def _default_hdc() -> str:
    found = shutil.which("hdc") or shutil.which("hdc_std")
    if found:
        return found
    for candidate in _HDC_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return "hdc"


DEFAULT_HDC = _default_hdc()
DEFAULT_TARGET = ""            # 留空 = 自动取 hdc list targets 的第一台
DEFAULT_ADMIN_IDS = []         # 允许使用的 QQ 号；为空时只允许 AstrBot 管理员
DEFAULT_TIMEOUT = 25
DEFAULT_SHOT_DIR = os.path.join(tempfile.gettempdir(), "astrbot-hdc-shots")

# 这些应用（bundle 名包含关键字）不允许被启动。
PAYMENT_BUNDLES = (
    "com.tencent.mm",          # 微信（内含支付）
    "com.eg.android.AlipayGphone",  # 支付宝
    "com.unionpay",
    "com.icbc",
    "com.chinamworld",
)


class HdcError(RuntimeError):
    """hdc 调用失败。"""


class HdcPhone(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}
        self.hdc = str(cfg.get("hdc_path") or DEFAULT_HDC)
        self.target = str(cfg.get("target") or DEFAULT_TARGET).strip()
        self.admin_ids = [str(x).strip() for x in (cfg.get("admin_ids") or DEFAULT_ADMIN_IDS)]
        self.timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
        self.screenshot_dir = str(cfg.get("screenshot_dir") or DEFAULT_SHOT_DIR)
        os.makedirs(self.screenshot_dir, exist_ok=True)

    # ---------------- 基础设施 ----------------

    def _run(self, *args: str, timeout: int | None = None) -> str:
        cmd = [self.hdc]
        target = self._resolve_target()
        if target:
            cmd += ["-t", target]
        cmd += list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise HdcError(f"hdc 超时（{timeout or self.timeout}s）：{' '.join(args)}") from exc
        except FileNotFoundError as exc:
            raise HdcError(f"找不到 hdc 可执行文件：{self.hdc}") from exc
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise HdcError(f"hdc 退出码 {proc.returncode}: {err or out}")
        return out

    def _resolve_target(self) -> str:
        if self.target:
            return self.target
        try:
            proc = subprocess.run(
                [self.hdc, "list", "targets"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return ""
        lines = [x.strip() for x in (proc.stdout or "").splitlines() if x.strip()]
        lines = [x for x in lines if x.lower() != "[empty]"]
        return lines[0] if lines else ""

    def _allowed(self, event: AstrMessageEvent) -> bool:
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if sender and sender in self.admin_ids:
            return True
        # 没配 admin_ids 时退回 AstrBot 自己的管理员列表
        if not self.admin_ids:
            try:
                admins = [str(x) for x in (self.context.get_config().get("admins_id") or [])]
            except Exception:
                admins = []
            return sender in admins
        return False

    @staticmethod
    def _deny() -> str:
        return json.dumps(
            {"ok": False, "reason": "只有管理员可以让这台设备执行操作"},
            ensure_ascii=False,
        )

    def _layout(self) -> list[dict]:
        """把当前界面读成 [{type, text, bounds, x, y}] 的扁平列表。"""
        remote = "/data/local/tmp/astrbot_layout.json"
        self._run("shell", "uitest", "dumpLayout", "-p", remote)
        local = os.path.join(self.screenshot_dir, "_layout.json")
        self._run("file", "recv", remote, local)
        with open(local, "r", encoding="utf-8", errors="replace") as fh:
            root = json.load(fh)

        out: list[dict] = []

        def walk(node):
            if not isinstance(node, dict):
                return
            attrs = node.get("attributes") or {}
            text = str(attrs.get("text") or "").strip()
            kind = str(attrs.get("type") or "").strip()
            bounds = str(attrs.get("bounds") or "").strip()
            match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
            if text and match and kind not in {"__Common__"}:
                x1, y1, x2, y2 = (int(g) for g in match.groups())
                out.append(
                    {
                        "type": kind,
                        "text": text[:60],
                        "x": (x1 + x2) // 2,
                        "y": (y1 + y2) // 2,
                    }
                )
            for child in node.get("children") or []:
                walk(child)

        walk(root)
        return out

    # ---------------- 工具 ----------------

    @llm_tool(name="hdc_ui_dump")
    async def hdc_ui_dump_tool(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """读取鸿蒙手机当前界面上的控件（文字 + 中心坐标），用来决定该点哪里。

        这是操作手机前的**第一步**：先看屏上有什么，再决定点击坐标。

        Args:
            keyword(string): 可选。只返回文字里包含该关键字的控件；留空返回全部。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            items = self._layout()
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)

        if keyword:
            items = [i for i in items if keyword in i["text"]]
        total = len(items)
        items = items[:80]
        return json.dumps(
            {"ok": True, "count": total, "shown": len(items), "nodes": items},
            ensure_ascii=False,
        )

    @llm_tool(name="hdc_tap")
    async def hdc_tap_tool(
        self,
        event: AstrMessageEvent,
        x: int,
        y: int,
    ) -> str:
        """点击屏幕上的一个坐标点（坐标来自 hdc_ui_dump 给出的 x / y）。

        Args:
            x(number): 横坐标。
            y(number): 纵坐标。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            self._run("shell", "uitest", "uiInput", "click", str(int(x)), str(int(y)))
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "tapped": [int(x), int(y)]}, ensure_ascii=False)

    @llm_tool(name="hdc_swipe")
    async def hdc_swipe_tool(
        self,
        event: AstrMessageEvent,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        speed: int = 800,
    ) -> str:
        """在屏幕上滑动（滚动列表、翻页）。

        Args:
            x1(number): 起点横坐标。
            y1(number): 起点纵坐标。
            x2(number): 终点横坐标。
            y2(number): 终点纵坐标。
            speed(number): 滑动速度，越大越快，默认 800。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            self._run(
                "shell", "uitest", "uiInput", "swipe",
                str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(speed)),
            )
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)

    @llm_tool(name="hdc_input_text")
    async def hdc_input_text_tool(self, event: AstrMessageEvent, text: str) -> str:
        """往当前获得焦点的输入框里输入文字（需要先点击输入框让它获得焦点）。

        Args:
            text(string): 要输入的文字。
        """
        if not self._allowed(event):
            return self._deny()
        if not text.strip():
            return json.dumps({"ok": False, "reason": "输入内容为空"}, ensure_ascii=False)
        try:
            self._run("shell", "uitest", "uiInput", "inputText", text)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "length": len(text)}, ensure_ascii=False)

    def _resolve_ability(self, bundle: str, preferred: str = "") -> str:
        """查这个 bundle 的真正入口 ability。

        关键：鸿蒙里 ability 的名字有两种截然不同的写法，而 `aa start -a` 必须
        收到清单里的原名，不能自己拼包名前缀：
          - 设置：com.huawei.hmos.settings.MainAbility（名字本身带包名前缀）
          - 抖音：MainAbility（名字就是裸的）
        所以这里只负责「挑一个像入口的」，原样返回，不做任何前缀处理。

        关于调用方传进来的 preferred：`aa start` 即使 ability 名字不存在也会返回
        成功，所以不能盲信。模型经常自作主张塞一个 EntryAbility 进来，这里必须
        先拿清单核对，名字对不上就丢掉，回退到自动探测。
        """
        try:
            dump = self._run("shell", "bm", "dump", "-n", bundle)
        except HdcError:
            # 查不到清单时只能信调用方给的，总比什么都不做强。
            return preferred
        names = re.findall(r'"name":\s*"([^"]+)"', dump)
        if preferred and preferred in names:
            return preferred
        candidates = [n for n in names if n.endswith("Ability")]
        if not candidates:
            return ""
        # 入口优先：MainAbility / EntryAbility，其次名字里带 Entry 的。
        for name in candidates:
            if name.endswith("MainAbility") or name.endswith("EntryAbility"):
                return name
        for name in candidates:
            if "Entry" in name:
                return name
        return candidates[0]

    @llm_tool(name="hdc_app_start")
    async def hdc_app_start_tool(
        self,
        event: AstrMessageEvent,
        bundle: str,
        ability: str = "",
    ) -> str:
        """按 bundle 名启动一个应用。

        只需要给包名，插件会自动查出该应用的入口 ability。
        支付类、银行类应用在黑名单里，不允许启动。

        Args:
            bundle(string): 应用包名，例如 com.huawei.hmos.settings。
            ability(string): 一般留空；只有在自动查不到入口时才需要手动指定。
        """
        if not self._allowed(event):
            return self._deny()
        if any(bundle.startswith(p) for p in PAYMENT_BUNDLES):
            return json.dumps(
                {"ok": False, "reason": f"{bundle} 属于支付/银行类应用，插件拒绝启动"},
                ensure_ascii=False,
            )
        resolved = self._resolve_ability(bundle, ability)
        if not resolved:
            return json.dumps(
                {"ok": False, "reason": f"查不到 {bundle} 的入口 ability"},
                ensure_ascii=False,
            )
        try:
            self._run("shell", "aa", "start", "-a", resolved, "-b", bundle)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps(
            {"ok": True, "started": bundle, "ability": resolved}, ensure_ascii=False
        )

    @llm_tool(name="hdc_screenshot")
    async def hdc_screenshot_tool(self, event: AstrMessageEvent) -> str:
        """把手机当前画面截图保存到电脑上（只给人看，模型看不到图片）。

        适合在完成一串操作后留证据，或者让人确认结果。
        """
        if not self._allowed(event):
            return self._deny()
        remote = "/data/local/tmp/astrbot_shot.jpeg"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        local = os.path.join(self.screenshot_dir, f"shot-{stamp}.jpeg")
        try:
            self._run("shell", "snapshot_display", "-f", remote)
            self._run("file", "recv", remote, local)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "path": local}, ensure_ascii=False)

    async def terminate(self):
        logger.info("[hdc] 插件已卸载")
