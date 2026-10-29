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

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time

from mcp.types import CallToolResult, ImageContent, TextContent

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

# 产物落到插件自己的数据目录，便于迁移与审计：
#   data/plugins/astrbot_plugin_hdc -> data/plugin_data/astrbot_plugin_hdc
# 这里只用 __file__ 推导，不依赖 AstrBot 的内部 API。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SHOT_DIR = os.path.normpath(
    os.path.join(_PLUGIN_DIR, "..", "..", "plugin_data", os.path.basename(_PLUGIN_DIR))
)

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

    async def _arun(self, *args: str, timeout: int | None = None) -> str:
        """异步版 _run：把阻塞的 subprocess 丢到线程池，别卡住事件循环。

        hdc 和设备之间走 USB / 无线调试（192.168.x.x:端口），本质是网络 I/O，
        在事件循环里同步跑会卡死整个 AstrBot，最长可达 timeout 秒。
        """
        return await asyncio.to_thread(self._run, *args, timeout=timeout)

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

    # 截图滚动保留份数：文件按 shot-1..shot-N 轮转，空间恒定不增长。
    RETAIN_SHOTS = 20
    SHOT_WIDTH = 720

    def _rotate_shot(self, path: str) -> str:
        """把新截图放进 shot-1，旧图依次后移，最老的一张丢弃。"""
        try:
            for i in range(self.RETAIN_SHOTS - 1, 0, -1):
                src = os.path.join(self.screenshot_dir, f"shot-{i}.jpeg")
                if os.path.exists(src):
                    os.replace(src, os.path.join(self.screenshot_dir, f"shot-{i + 1}.jpeg"))
            os.replace(path, os.path.join(self.screenshot_dir, "shot-1.jpeg"))
        except OSError as exc:
            logger.warning(f"[hdc] 轮转截图失败: {exc}")
        return os.path.join(self.screenshot_dir, "shot-1.jpeg")

    def _downscale(self, path: str) -> str:
        """缩到 SHOT_WIDTH 宽。图小一大半，界面文字仍然清晰。"""
        try:
            from PIL import Image
        except ImportError:
            return path
        try:
            with Image.open(path) as img:
                if img.width <= self.SHOT_WIDTH:
                    return path
                ratio = self.SHOT_WIDTH / img.width
                resized = img.resize(
                    (self.SHOT_WIDTH, max(1, int(img.height * ratio))),
                    Image.LANCZOS,
                )
                out = path.replace("_incoming", "_scaled")
                resized.convert("RGB").save(out, "JPEG", quality=80)
            return out
        except Exception as exc:
            logger.warning(f"[hdc] 缩放截图失败，改用原图: {exc}")
            return path

    def _capture(self) -> str:
        """截图 → 拉到电脑 → 缩放 → 放进滚动槽位，返回本地路径。

        同步实现；只能在 _arun 所在的线程池里跑（async 处理器用 to_thread 调）。
        """
        remote = "/data/local/tmp/astrbot_shot.jpeg"
        incoming = os.path.join(self.screenshot_dir, "_incoming.jpeg")
        self._run("shell", "snapshot_display", "-f", remote)
        self._run("file", "recv", remote, incoming)
        return self._rotate_shot(self._downscale(incoming))

    def _layout(self) -> list[dict]:
        """把当前界面读成 [{type, text, bounds, x, y}] 的扁平列表。

        同步实现，同样交给线程池执行。中转文件 _layout.json 是一次性产物，
        和截图一起放在插件数据目录里。
        """
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
            items = await asyncio.to_thread(self._layout)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)

        if keyword:
            items = [i for i in items if keyword in i["text"]]
        total = len(items)
        payload = {"ok": True, "count": total, "shown": min(total, 80), "nodes": items[:80]}
        # 节点太少通常意味着这是自绘界面（视频流、游戏、图片墙），文本树读不出
        # 内容。这时候要让模型知道该换视觉兜底，而不是对着空列表瞎猜。
        if total < 5:
            payload["hint"] = (
                "当前界面几乎没有可读文字，可能是自绘界面。"
                "需要理解画面内容时改用 hdc_see 看画面。"
            )
        return json.dumps(payload, ensure_ascii=False)

    @llm_tool(name="hdc_see")
    async def hdc_see_tool(
        self,
        event: AstrMessageEvent,
        question: str = "",
    ) -> CallToolResult:
        """截图并把画面直接交给模型看（视觉兜底）。

        什么时候用：hdc_ui_dump 返回的节点很少，或者界面是自绘的（视频流、
        游戏、图片墙），文本控件树读不出内容时，才用它看画面。

        注意：图片比读控件树贵得多（多消耗上千 token），能读文本就不要用它。

        Args:
            question(string): 你想从画面里确认什么，例如「这个视频在讲什么」。
        """
        if not self._allowed(event):
            return CallToolResult(
                content=[TextContent(type="text", text=self._deny())],
                isError=True,
            )
        try:
            path = await asyncio.to_thread(self._capture)
        except HdcError as exc:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False),
                    )
                ],
                isError=True,
            )

        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")

        note = question.strip() or "这是手机当前画面。"
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"{note}（截图已附在下一条消息里，按画面回答，别猜。）",
                ),
                ImageContent(type="image", data=b64, mimeType="image/jpeg"),
            ]
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
            await self._arun("shell", "uitest", "uiInput", "click", str(int(x)), str(int(y)))
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
            await self._arun(
                "shell", "uitest", "uiInput", "swipe",
                str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(speed)),
            )
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)

    @llm_tool(name="hdc_long_click")
    async def hdc_long_click_tool(self, event: AstrMessageEvent, x: int, y: int) -> str:
        """长按屏幕坐标（删除、多选、呼出菜单常用）。

        用 hdc_tap 点不开的东西，多半是要长按。

        Args:
            x(number): 横坐标，来自 hdc_ui_dump。
            y(number): 纵坐标。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            await self._arun("shell", "uitest", "uiInput", "longClick", str(int(x)), str(int(y)))
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "long_clicked": [int(x), int(y)]}, ensure_ascii=False)

    @llm_tool(name="hdc_double_tap")
    async def hdc_double_tap_tool(self, event: AstrMessageEvent, x: int, y: int) -> str:
        """双击屏幕坐标（放大图片、点赞、双击唤醒）。

        Args:
            x(number): 横坐标。
            y(number): 纵坐标。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            await self._arun("shell", "uitest", "uiInput", "doubleClick", str(int(x)), str(int(y)))
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "double_tapped": [int(x), int(y)]}, ensure_ascii=False)

    @llm_tool(name="hdc_swipe_loop")
    async def hdc_swipe_loop_tool(
        self,
        event: AstrMessageEvent,
        times: int = 10,
        direction: str = "up",
        delay_ms: int = 1500,
        screenshot_at_end: bool = True,
    ) -> str:
        """连续刷屏（刷短视频、翻长列表）——一次调用刷很多下，不用每屏都推理。

        刷抖音这类要「一直刷」的场景：连续调它几次，每次刷一批，
        中间用 hdc_see 看一眼画面，再决定要不要继续。

        Args:
            times(number): 刷几下，默认 10，最多 50。
            direction(string): up（看下一个）/ down（回上一个），默认 up。
            delay_ms(number): 每下之间间隔毫秒，默认 1500，最多 5000。
            screenshot_at_end(boolean): 刷完是否截一张图返回，默认 true。
        """
        if not self._allowed(event):
            return self._deny()
        count = max(1, min(50, int(times or 10)))
        gap = max(300, min(5000, int(delay_ms or 1500))) / 1000.0
        # dircFling 的方向码：0 左 1 右 2 上 3 下
        code = 2 if str(direction).lower() == "up" else 3
        try:
            for index in range(count):
                await self._arun("shell", "uitest", "uiInput", "dircFling", str(code))
                if index < count - 1:
                    await asyncio.sleep(gap)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)

        payload: dict = {"ok": True, "swiped": count, "direction": direction}
        if screenshot_at_end:
            try:
                payload["screenshot"] = await asyncio.to_thread(self._capture)
            except HdcError as exc:
                payload["screenshot_error"] = str(exc)
        return json.dumps(payload, ensure_ascii=False)

    @llm_tool(name="hdc_scroll")
    async def hdc_scroll_tool(
        self,
        event: AstrMessageEvent,
        direction: str = "down",
        times: int = 1,
    ) -> str:
        """按方向滚动屏幕（比手搓 swipe 坐标稳，适合刷列表）。

        direction 含义是「内容往哪边走」：down = 看下面的内容（列表上滑）。

        Args:
            direction(string): up / down / left / right，默认 down。
            times(number): 连滚几次，默认 1，最多 10。
        """
        if not self._allowed(event):
            return self._deny()
        # 0 左 1 右 2 上 3 下
        mapping = {"left": 0, "right": 1, "up": 2, "down": 3}
        code = mapping.get(str(direction).lower())
        if code is None:
            return json.dumps(
                {"ok": False, "reason": "direction 只能是 up/down/left/right"},
                ensure_ascii=False,
            )
        repeat = max(1, min(10, int(times or 1)))
        try:
            for _ in range(repeat):
                await self._arun("shell", "uitest", "uiInput", "dircFling", str(code))
                await asyncio.sleep(0.3)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps(
            {"ok": True, "scrolled": direction, "times": repeat}, ensure_ascii=False
        )

    @llm_tool(name="hdc_drag")
    async def hdc_drag_tool(
        self,
        event: AstrMessageEvent,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> str:
        """拖拽（拖动滑块、拖图标、调音量条）。和 swipe 的区别是拖拽更慢、会跟手。

        Args:
            x1(number): 起点横坐标。
            y1(number): 起点纵坐标。
            x2(number): 终点横坐标。
            y2(number): 终点纵坐标。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            await self._arun(
                "shell", "uitest", "uiInput", "drag",
                str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
            )
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "dragged": [int(x1), int(y1), int(x2), int(y2)]}, ensure_ascii=False)

    # --- 文件 ---

    def _pull_dir(self) -> str:
        path = os.path.join(self.screenshot_dir, "files")
        os.makedirs(path, exist_ok=True)
        return path

    @llm_tool(name="hdc_file_pull")
    async def hdc_file_pull_tool(self, event: AstrMessageEvent, remote_path: str) -> str:
        """把手机上的文件拉回电脑，返回本地路径。

        配合 hdc_ui_dump 找到的路径用；也可以拉相册里的原图。

        Args:
            remote_path(string): 手机上的完整路径，如 /storage/Media/DCIM/Camera/IMG_001.jpg。
        """
        if not self._allowed(event):
            return self._deny()
        remote = str(remote_path or "").strip()
        if not remote.startswith("/"):
            return json.dumps({"ok": False, "reason": "要给绝对路径"}, ensure_ascii=False)
        name = os.path.basename(remote) or f"pull_{int(time.time())}"
        local = os.path.join(self._pull_dir(), name)
        try:
            await self._arun("file", "recv", remote, local, timeout=120)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        if not os.path.isfile(local):
            return json.dumps({"ok": False, "reason": f"没拉到文件：{remote}"}, ensure_ascii=False)
        return json.dumps(
            {"ok": True, "local_path": local, "size": os.path.getsize(local)},
            ensure_ascii=False,
        )

    @llm_tool(name="hdc_file_push")
    async def hdc_file_push_tool(
        self,
        event: AstrMessageEvent,
        local_path: str,
        remote_path: str = "/data/local/tmp/",
    ) -> str:
        """把电脑上的文件推到手机。

        Args:
            local_path(string): 电脑上的文件路径。
            remote_path(string): 手机上的目标路径，默认 /data/local/tmp/。
        """
        if not self._allowed(event):
            return self._deny()
        local = str(local_path or "").strip()
        if not os.path.isfile(local):
            return json.dumps({"ok": False, "reason": f"本地没有这个文件：{local}"}, ensure_ascii=False)
        dest = remote_path if remote_path.endswith("/") else remote_path
        if remote_path.endswith("/"):
            dest = remote_path + os.path.basename(local)
        try:
            await self._arun("file", "send", local, dest, timeout=120)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "remote_path": dest}, ensure_ascii=False)

    @llm_tool(name="hdc_latest_photos")
    async def hdc_latest_photos_tool(
        self,
        event: AstrMessageEvent,
        count: int = 1,
        album_dir: str = "",
    ) -> str:
        """从手机某个目录取最新文件（需要 shell 用户有读权限）。

        注意：**相册目录在 HarmonyOS 上是读不到的**（/storage/media 与 /mnt/hmdfs
        都是 Permission denied），所以这个工具拿不到相册原图。要看相册内容，请走
        hdc_app_start(com.huawei.hmos.photos) 打开图库，再 hdc_screenshot + hdc_see。
        这个工具适合 /data/local/tmp 或应用自己导出的目录。

        Args:
            count(number): 取几个，默认 1，最多 5。
            album_dir(string): 目标目录，例如 /data/local/tmp。
        """
        if not self._allowed(event):
            return self._deny()
        target = str(album_dir or "").strip()
        if not target:
            return json.dumps(
                {
                    "ok": False,
                    "reason": (
                        "相册（/storage/media、/mnt/hmdfs）在 HarmonyOS 上对 shell 用户"
                        "是禁止访问的，取不到原图。要看相册请改用："
                        "hdc_app_start 打开 com.huawei.hmos.photos → hdc_screenshot → hdc_see。"
                        "如果要取的是 /data/local/tmp 之类的文件，请显式传 album_dir。"
                    ),
                },
                ensure_ascii=False,
            )
        take = max(1, min(5, int(count or 1)))
        try:
            listing = await self._arun("shell", "ls", "-t", target)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        names = [
            line.strip()
            for line in listing.splitlines()
            if line.strip() and not line.strip().endswith("/")
        ][:take]
        if not names:
            return json.dumps({"ok": False, "reason": f"{target} 里没有文件"}, ensure_ascii=False)

        saved = []
        for name in names:
            remote = f"{target.rstrip('/')}/{name}"
            local = os.path.join(self._pull_dir(), name)
            try:
                await self._arun("file", "recv", remote, local, timeout=120)
            except HdcError:
                continue
            if os.path.isfile(local):
                saved.append(local)
        return json.dumps(
            {"ok": bool(saved), "files": saved},
            ensure_ascii=False,
        )

    # --- 设备状态 ---

    @llm_tool(name="hdc_device_status")
    async def hdc_device_status_tool(self, event: AstrMessageEvent) -> str:
        """读手机当前状态：电量、充电、剩余存储、前台应用。

        想知道「手机是不是快没电了」「现在在用哪个 App」时用。
        """
        if not self._allowed(event):
            return self._deny()
        info: dict = {}
        try:
            battery = await self._arun("shell", "hidumper", "-s", "BatteryService", "-a", "-i")
            match = re.search(r"capacity:\s*(\d+)", battery)
            if match:
                info["battery_percent"] = int(match.group(1))
            info["charging"] = bool(re.search(r"chargingStatus:\s*[1-9]", battery))
        except HdcError:
            pass
        try:
            storage = await self._arun("shell", "df", "-h", "/data")
            info["storage"] = storage.splitlines()[-1].strip() if storage.strip() else ""
        except HdcError:
            pass
        try:
            top = await self._arun(
                "shell", "hidumper", "-s", "AbilityManagerService", "-a", "-e"
            )
            apps = re.findall(r"bundleName:\s*([\w.]+)", top)
            if apps:
                info["foreground_app"] = apps[0]
        except HdcError:
            pass
        return json.dumps({"ok": True, "status": info}, ensure_ascii=False)

    @llm_tool(name="hdc_app_list")
    async def hdc_app_list_tool(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """列出手机上装了哪些应用，可按关键词过滤。

        拿到 bundleName 后配合 hdc_app_start 使用。

        Args:
            keyword(string): 过滤词，比如「音乐」「淘宝」；留空列全部。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            raw = await self._arun("shell", "bm", "dump", "-a", timeout=60)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        bundles = [line.strip() for line in raw.splitlines() if line.strip()]
        needle = str(keyword or "").strip().lower()
        if needle:
            bundles = [b for b in bundles if needle in b.lower()]
        return json.dumps(
            {"ok": True, "count": len(bundles), "bundles": bundles[:80]},
            ensure_ascii=False,
        )


    # 输入法候选栏的坐标（1320 宽的屏，键盘弹出时）。
    # 候选栏是输入法自己的窗口，dumpLayout 读不到，只能按坐标点。
    CANDIDATE_X0 = 96
    CANDIDATE_STEP = 130
    CANDIDATE_Y = 1832

    @llm_tool(name="hdc_input_text")
    async def hdc_input_text_tool(
        self,
        event: AstrMessageEvent,
        text: str = "",
        pinyin: str = "",
        candidate: int = 0,
    ) -> str:
        """往当前获得焦点的输入框里输入文字。

        英文和数字直接写入。中文要先转成拼音（你自己会转），走输入法：
        插件逐字母敲拼音，再点候选栏里的第 candidate 个候选词。

        例子：想输入「测试」-> pinyin="ceshi"，candidate=0（第一个候选）。

        Args:
            text(string): 英文/数字内容。要输入中文时留空。
            pinyin(string): 中文对应的拼音字母，只含 a-z，不带声调。
            candidate(number): 取第几个候选词，从 0 开始。默认第一个。
        """
        if not self._allowed(event):
            return self._deny()

        # 鸿蒙的 uitest 提供两个输入命令：
        #   uiInput text <text>      在已聚焦的位置输入，支持中文
        #   uinput -K -t <text>      只支持 ASCII
        # 之前误判成「打不了中文」，是因为只看了 uitest 帮助的前半段，
        # 没翻到 text 那两行。
        raw = pinyin.strip() or text.strip()
        if not raw.strip():
            return json.dumps(
                {"ok": False, "reason": "给 text（要输入的内容）"}, ensure_ascii=False
            )
        try:
            if raw.isascii():
                await self._arun("shell", "uinput", "-K", "-t", raw)
            else:
                await self._arun("shell", "uitest", "uiInput", "text", raw)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "typed": raw[:40]}, ensure_ascii=False)

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
        resolved = await asyncio.to_thread(self._resolve_ability, bundle, ability)
        if not resolved:
            return json.dumps(
                {"ok": False, "reason": f"查不到 {bundle} 的入口 ability"},
                ensure_ascii=False,
            )
        try:
            await self._arun("shell", "aa", "start", "-a", resolved, "-b", bundle)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps(
            {"ok": True, "started": bundle, "ability": resolved}, ensure_ascii=False
        )

    @llm_tool(name="hdc_screenshot")
    async def hdc_screenshot_tool(self, event: AstrMessageEvent) -> str:
        """把手机当前画面截图保存下来（给人看或稍后用 send_message_to_user 发出去）。

        截图固定保留最近 20 张（shot-1 最新），磁盘占用恒定，不会越存越多。
        想让模型自己看懂画面，用 hdc_see 而不是这个。
        """
        if not self._allowed(event):
            return self._deny()
        try:
            local = await asyncio.to_thread(self._capture)
        except HdcError as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)
        return json.dumps({"ok": True, "path": local}, ensure_ascii=False)

    async def terminate(self):
        logger.info("[hdc] 插件已卸载")
