"""蔗糖的菜单 —— 把「我会干什么」做成一张随时能递出去的卡片。

三条投递路径：
    1. 指令触发    —— 谁发 #菜单 就要一张
    2. AI 工具     —— 对方直接问「你会干啥」时由模型自己判断该递卡
    3. 事件自动    —— 加好友、机器人进群时主动递一张

渲染：Playwright 截图，失败/超时自动回退纯文本，绝不因为画不出图而哑掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import os
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

try:  # 消息链位置随版本变动过，兜一层
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    from astrbot.core.message.message import MessageChain  # type: ignore

_TAG = "[SugarMenu]"

# 正则过滤器不受 wake_prefix 制约，但它在插件导入时就固化了，
# 读不到运行时配置。所以这里给一个宽表兜底，handler 内部再用
# 用户配置的 commands 做精确校验——常见触发词改配置能实时生效，
# 表外的全新词需要重载插件。
_TRIGGER_RE = r"(?i)^[#/!！]?\s*(?:菜单|菜单卡|功能|功能表|功能列表|帮助|指令|指令表|用法|说明|自我介绍|help|menu|commands?)\s*$"

_THEMES: dict[str, dict[str, str]] = {
    "light": {
        "page_bg": "#eef1f5",
        "card_bg": "#ffffff",
        "card_shadow": "0 10px 40px rgba(24,32,48,.10)",
        "title": "#16181d",
        "sub": "#7a8290",
        "name": "#1f2430",
        "desc": "#6b7280",
        "accent": "#2f6feb",
        "divider": "#e8ecf1",
        "item_bg": "#f8fafc",
        "item_border": "#eef1f6",
        "tip": "#8a94a6",
        "footer": "#9aa3af",
    },
    "dark": {
        "page_bg": "#14161b",
        "card_bg": "#1e2128",
        "card_shadow": "0 10px 40px rgba(0,0,0,.45)",
        "title": "#f2f4f8",
        "sub": "#959dac",
        "name": "#eef1f6",
        "desc": "#98a1b0",
        "accent": "#7aa7ff",
        "divider": "#2b303a",
        "item_bg": "#252a33",
        "item_border": "#2f3540",
        "tip": "#8992a1",
        "footer": "#78808e",
    },
    "sugar": {
        "page_bg": "linear-gradient(155deg,#fdf3f9 0%,#f4e9ff 48%,#eef2ff 100%)",
        "card_bg": "rgba(255,255,255,.88)",
        "card_shadow": "0 14px 44px rgba(163,110,196,.16)",
        "title": "#3b2b46",
        "sub": "#8d7c97",
        "name": "#48335a",
        "desc": "#8a7c93",
        "accent": "#c26ed6",
        "divider": "rgba(194,110,214,.16)",
        "item_bg": "rgba(255,255,255,.64)",
        "item_border": "rgba(194,110,214,.13)",
        "tip": "#a08cab",
        "footer": "#b09bbb",
    },
}

_CSS = """
*{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}
html,body{background:__page_bg__}
body{
  padding:26px;
  font-family:"PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC",
              "Source Han Sans SC",system-ui,-apple-system,"Segoe UI",sans-serif;
  color:__name__;
}
.card{
  background:__card_bg__;
  border-radius:22px;
  box-shadow:__card_shadow__;
  padding:26px 26px 20px;
  max-width:__card_max__;
  margin:0 auto;
  backdrop-filter:blur(6px);
}
.head{display:flex;align-items:center;gap:14px}
.avatar{
  width:52px;height:52px;border-radius:50%;object-fit:cover;flex:0 0 auto;
  border:2px solid __item_border__;
}
.head-text{min-width:0;flex:1}
h1{
  font-size:__title_size__px;font-weight:700;color:__title__;
  letter-spacing:.2px;line-height:1.28;word-break:break-word;
}
.sub{
  margin-top:5px;font-size:__sub_size__px;color:__sub__;
  line-height:1.5;word-break:break-word;
}
.divider{
  height:1px;background:__divider__;margin:18px 0 16px;border-radius:1px;
}
.items{display:flex;flex-direction:column;gap:9px}
.item{
  display:flex;align-items:flex-start;gap:12px;
  background:__item_bg__;border:1px solid __item_border__;
  border-radius:13px;padding:11px 14px;
}
.bar{
  width:3px;border-radius:2px;background:__accent__;flex:0 0 auto;
  align-self:stretch;min-height:16px;opacity:.85;
}
.item-text{min-width:0;flex:1}
.name{
  font-size:__font_size__px;font-weight:600;color:__name__;
  line-height:1.45;word-break:break-word;
}
.desc{
  font-size:__desc_size__px;color:__desc__;line-height:1.55;
  margin-top:2px;word-break:break-word;
}
.tip{
  margin-top:15px;font-size:__desc_size__px;color:__tip__;
  line-height:1.6;padding:10px 14px;border-radius:11px;
  background:__item_bg__;border:1px dashed __item_border__;
  word-break:break-word;
}
.footer{
  margin-top:16px;text-align:right;font-size:__desc_size__px;
  color:__footer__;letter-spacing:.4px;
}
"""


def _esc(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=True)


def _get_value(raw: Any, key: str) -> Any:
    """OneBot 原始事件可能是 dict、对象或带 __getitem__ 的包装，统一取值。"""
    if raw is None:
        return None
    try:
        if isinstance(raw, dict):
            return raw.get(key)
        if hasattr(raw, key):
            return getattr(raw, key)
        return raw[key]
    except Exception:
        return None


class _FriendAdded(filter.CustomFilter):
    """OneBot 好友已添加通知：post_type=notice, notice_type=friend_add。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        return (
            _get_value(raw, "post_type") == "notice"
            and _get_value(raw, "notice_type") == "friend_add"
        )


class _BotJoinedGroup(filter.CustomFilter):
    """机器人自己进群：post_type=notice, notice_type=group_increase, user_id == self_id。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        if (
            _get_value(raw, "post_type") != "notice"
            or _get_value(raw, "notice_type") != "group_increase"
        ):
            return False
        sub = str(_get_value(raw, "sub_type") or "")
        if sub and sub not in ("invite", "approve"):
            return False
        user_id = str(_get_value(raw, "user_id") or "")
        self_id = str(_get_value(raw, "self_id") or "")
        return bool(user_id and self_id and user_id == self_id)


class SugarMenuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._pw = None
        self._browser = None
        self._render_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, str]] = {}
        self._auto_seen: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}
        self._cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        os.makedirs(self._cache_dir, exist_ok=True)

    # ──────────────────────────── 配置 ────────────────────────────

    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        g = self.config.get(group)
        if not isinstance(g, dict):
            return default
        v = g.get(key, default)
        return default if v is None else v

    def _int_cfg(self, group: str, key: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
        try:
            return max(lo, min(hi, int(self._cfg(group, key, default))))
        except (TypeError, ValueError):
            return default

    def _items(self) -> list[tuple[str, str]]:
        raw = self._cfg("menu", "sections", []) or []
        if not isinstance(raw, list):
            raw = [raw]
        out: list[tuple[str, str]] = []
        for line in raw:
            s = str(line or "").strip()
            if not s:
                continue
            if "|" in s:
                name, desc = s.split("|", 1)
                out.append((name.strip(), desc.strip()))
            else:
                out.append((s, ""))
        return out

    def _theme(self) -> dict[str, str]:
        name = str(self._cfg("render", "theme", "sugar") or "sugar").strip().lower()
        return _THEMES.get(name, _THEMES["sugar"])

    def _commands(self) -> list[str]:
        raw = self._cfg("trigger", "commands", []) or []
        if not isinstance(raw, list):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x or "").strip()]

    # ──────────────────────────── 文本版 ────────────────────────────

    def _text_version(self) -> str:
        title = str(self._cfg("menu", "title", "") or "").strip() or "我会这些"
        sub = str(self._cfg("menu", "subtitle", "") or "").strip()
        tip = str(self._cfg("menu", "tip", "") or "").strip()
        footer = str(self._cfg("menu", "footer", "") or "").strip()

        lines: list[str] = [f"【{title}】"]
        if sub:
            lines.append(sub)
        lines.append("")
        for i, (name, desc) in enumerate(self._items(), 1):
            lines.append(f"{i}. {name}" + (f"\n   {desc}" if desc else ""))
        if tip:
            lines.append("")
            lines.append(f"· {tip}")
        if footer:
            lines.append("")
            lines.append(footer)
        return "\n".join(lines)

    # ──────────────────────────── HTML ────────────────────────────

    def _build_html(self) -> str:
        t = self._theme()
        width = self._int_cfg("render", "width", 720, 320, 2000)
        font_size = self._int_cfg("render", "font_size", 15, 10, 40)
        title = str(self._cfg("menu", "title", "") or "").strip() or "我会这些"
        sub = str(self._cfg("menu", "subtitle", "") or "").strip()
        tip = str(self._cfg("menu", "tip", "") or "").strip()
        footer = str(self._cfg("menu", "footer", "") or "").strip()
        avatar = str(self._cfg("menu", "avatar", "") or "").strip()

        css = _CSS
        for k, v in t.items():
            css = css.replace(f"__{k}__", v)
        css = (
            css.replace("__card_max__", f"{width - 52}px")
            .replace("__title_size__", str(int(font_size * 1.55)))
            .replace("__sub_size__", str(font_size))
            .replace("__font_size__", str(font_size))
            .replace("__desc_size__", str(max(10, font_size - 2)))
        )

        head = ""
        if title or sub:
            avatar_html = (
                f'<img class="avatar" src="{_esc(avatar)}" alt="">' if avatar else ""
            )
            sub_html = f'<div class="sub">{_esc(sub)}</div>' if sub else ""
            head = (
                f'<div class="head">{avatar_html}<div class="head-text">'
                f"<h1>{_esc(title)}</h1>{sub_html}</div></div>"
            )

        items_html = ""
        rows = self._items()
        if rows:
            chunks = []
            for name, desc in rows:
                desc_html = f'<div class="desc">{_esc(desc)}</div>' if desc else ""
                chunks.append(
                    f'<div class="item"><div class="bar"></div>'
                    f'<div class="item-text"><div class="name">{_esc(name)}</div>'
                    f"{desc_html}</div></div>"
                )
            items_html = f'<div class="divider"></div><div class="items">{"".join(chunks)}</div>'

        tip_html = f'<div class="tip">{_esc(tip)}</div>' if tip else ""
        footer_html = f'<div class="footer">{_esc(footer)}</div>' if footer else ""

        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<style>{css}</style></head><body>"
            f'<div class="card">{head}{items_html}{tip_html}{footer_html}</div>'
            "</body></html>"
        )

    # ──────────────────────────── 渲染 ────────────────────────────

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        async with self._render_lock:
            if self._browser is not None:
                return self._browser
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--disable-gpu"]
            )
        return self._browser

    async def _render_png(self) -> str | None:
        html_str = self._build_html()
        key = hashlib.md5(html_str.encode("utf-8")).hexdigest()[:16]
        now = time.time()
        cache_sec = self._int_cfg("render", "cache_seconds", 300)
        hit = self._cache.get(key)
        if hit and cache_sec > 0 and (now - hit[0]) < cache_sec and os.path.exists(hit[1]):
            return hit[1]

        width = self._int_cfg("render", "width", 720, 320, 2000)
        browser = await self._ensure_browser()
        page = await browser.new_page(
            viewport={"width": width, "height": 600}, device_scale_factor=2
        )
        out = os.path.join(self._cache_dir, f"menu_{key}.png")
        try:
            await page.set_content(html_str, wait_until="load")
            try:
                await page.evaluate(
                    "() => Promise.all(Array.from(document.images).map(i => "
                    "i.complete ? null : new Promise(r => { i.onload = i.onerror = r })))"
                )
            except Exception:
                pass
            await page.wait_for_timeout(120)
            await page.screenshot(path=out, full_page=True)
        finally:
            try:
                await page.close()
            except Exception:
                pass

        for old_key, (_, old_path) in list(self._cache.items()):
            if old_key != key and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        self._cache = {key: (now, out)}
        return out

    async def _render_with_timeout(self) -> str | None:
        timeout = self._int_cfg("render", "timeout", 30, 3, 300)
        return await asyncio.wait_for(self._render_png(), timeout=timeout)

    # ──────────────────────────── 投递 ────────────────────────────

    async def _deliver(self, event: AstrMessageEvent, *, use_image: bool) -> str:
        if use_image:
            try:
                path = await self._render_with_timeout()
                if path:
                    await event.send(MessageChain(chain=[Image.fromFileSystem(path)]))
                    return "image"
            except asyncio.TimeoutError:
                logger.warning(f"{_TAG} 渲染超时，回退纯文本")
            except Exception as e:
                logger.warning(f"{_TAG} 渲染失败，回退纯文本: {e}")
            if not self._cfg("render", "fallback_text", True):
                await event.send(MessageChain(chain=[Plain("菜单生成失败，请联系管理员检查图片渲染环境。")]))
                return "error"
        await event.send(MessageChain(chain=[Plain(self._text_version())]))
        return "text"

    def _dedup_ok(self, key: str) -> bool:
        hours = self._int_cfg("auto", "dedup_hours", 24)
        if hours <= 0:
            return True
        now = time.time()
        window = hours * 3600
        last = self._auto_seen.get(key)
        if last and (now - last) < window:
            return False
        self._auto_seen[key] = now
        for k, ts in list(self._auto_seen.items()):
            if now - ts > max(window, 86400) * 2:
                self._auto_seen.pop(k, None)
        return True

    def _cooldown_ok(self, scope: str) -> bool:
        sec = self._int_cfg("trigger", "cooldown", 10, 0, 86400)
        if sec <= 0:
            return True
        now = time.time()
        last = self._cooldown.get(scope)
        if last and (now - last) < sec:
            return False
        self._cooldown[scope] = now
        return True

    def _allow_scope(self, event: AstrMessageEvent, group: str) -> bool:
        try:
            in_group = bool(event.get_group_id())
        except Exception:
            in_group = False
        if in_group:
            return bool(self._cfg(group, "allow_group", True))
        return bool(self._cfg(group, "allow_private", True))

    # ──────────────────────────── 指令触发 ────────────────────────────

    @staticmethod
    def _norm_cmd(s: str) -> str:
        """统一指令写法：去掉前导 # / ! ！ 和空白，转小写。

        AstrBot 在命中 wake_prefix 时会把前缀剥掉，所以 `/菜单` 到这里只剩 `菜单`；
        而 `#菜单` 不受 wake_prefix 影响，原样带着井号。两种都要能对上配置。
        """
        return str(s or "").strip().lstrip("#/!！").strip().lower()

    async def _handle_menu_request(self, event: AstrMessageEvent, text: str) -> None:
        if not self._cfg("trigger", "enable", True):
            return
        allowed = self._commands()
        norm = self._norm_cmd(text)
        if allowed:
            pool = {self._norm_cmd(c) for c in allowed}
            if norm not in pool:
                logger.info(f"{_TAG} 触发词 {text!r} 不在白名单 {pool} 内，忽略")
                return
        if not self._allow_scope(event, "trigger"):
            return
        if self._cfg("trigger", "admin_only", False) and not event.is_admin():
            await event.send(MessageChain(chain=[Plain("这个指令只有管理员能用。")]))
            event.stop_event()
            return
        scope = f"{event.get_group_id() or event.get_sender_id()}"
        if not self._cooldown_ok(scope):
            logger.info(f"{_TAG} 会话 {scope} 冷却中，跳过")
            return

        logger.info(f"{_TAG} 响应菜单请求：{text!r}")
        use_image = bool(self._cfg("render", "enable", True))
        await self._deliver(event, use_image=use_image)
        event.stop_event()

    @filter.command("菜单", alias={"帮助", "功能", "help", "menu"}, priority=1)
    async def on_menu_slash(self, event: AstrMessageEvent):
        """走 wake_prefix 的标准命令，`/菜单` 由这里响应。"""
        await self._handle_menu_request(event, (event.message_str or "").strip())

    @filter.regex(_TRIGGER_RE, priority=2)
    async def on_menu_command(self, event: AstrMessageEvent):
        """不带唤醒前缀的写法，`#菜单` / `菜单` 由这里响应。"""
        await self._handle_menu_request(event, (event.message_str or "").strip())

    # ──────────────────────────── AI 工具 ────────────────────────────

    @filter.llm_tool(name="show_sugar_menu")
    async def show_sugar_menu(self, event: AstrMessageEvent) -> str:
        """当用户询问你会什么、能做什么、有什么功能、怎么用、能帮什么忙，或者让你介绍自己时，调用此工具。
        它会把一张整理好的功能菜单卡片直接发给用户，卡片上列着当前可用的全部指令和用途。
        适合的场景：「你会干啥」「你有什么功能」「怎么用你」「介绍一下自己」「你能帮我做什么」。
        不适合：用户在问某个具体功能怎么用时，先回答问题本身，不要为了展示而弹卡。
        用户在连续追问细节时，不要重复调用本工具。
        """
        if not self._cfg("ai", "enable", True):
            return "菜单工具当前已被管理员关闭，请直接用文字回答用户，或告诉对方发送菜单指令。"
        if not self._allow_scope(event, "ai"):
            return "当前会话不允许调用菜单工具，请直接用文字简洁回答用户。"

        use_image = bool(self._cfg("render", "enable", True))
        try:
            mode = await self._deliver(event, use_image=use_image)
        except Exception as e:
            logger.warning(f"{_TAG} AI 工具投递失败: {e}")
            return "菜单发送失败，请直接用文字回答用户。"

        if mode == "image":
            return "功能菜单卡片已发送给用户。不要再复述卡片内容，可以简短说一句「上面都写着，挑一个试试」之类的话。"
        return "功能菜单（纯文字版）已发送给用户。不要再完整复述，简短补一句引导即可。"

    # ──────────────────────────── 事件自动投递 ────────────────────────────

    @filter.custom_filter(_FriendAdded)
    async def on_friend_added(self, event: AstrMessageEvent):
        if not self._cfg("auto", "friend_enable", True):
            return
        user_id = ""
        try:
            user_id = str(event.get_sender_id() or "")
        except Exception:
            pass
        if not self._dedup_ok(f"friend:{user_id}"):
            return
        delay = self._int_cfg("auto", "friend_delay", 2, 0, 120)
        if delay:
            await asyncio.sleep(delay)
        try:
            await self._deliver(
                event, use_image=bool(self._cfg("auto", "auto_use_image", True))
            )
            logger.info(f"{_TAG} 已向新好友 {user_id} 发送菜单")
        except Exception as e:
            logger.warning(f"{_TAG} 新好友菜单发送失败: {e}")
        event.stop_event()

    @filter.custom_filter(_BotJoinedGroup)
    async def on_bot_joined_group(self, event: AstrMessageEvent):
        if not self._cfg("auto", "group_enable", True):
            return
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            pass
        if not self._dedup_ok(f"group:{group_id}"):
            return
        delay = self._int_cfg("auto", "group_delay", 4, 0, 300)
        if delay:
            await asyncio.sleep(delay)
        try:
            await self._deliver(
                event, use_image=bool(self._cfg("auto", "auto_use_image", True))
            )
            logger.info(f"{_TAG} 已在新群 {group_id} 发送菜单")
        except Exception as e:
            logger.warning(f"{_TAG} 进群菜单发送失败: {e}")
        event.stop_event()

    # ──────────────────────────── 生命周期 ────────────────────────────

    def _apply_ai_hint(self) -> None:
        """把配置里的 ai_hint 追加进工具的 description。

        装饰器里的 docstring 在插件导入时就固化了，读不到运行时配置；
        这里在加载后补一层，改配置 + 重载插件即可调模型的判断依据。
        """
        extra = str(self._cfg("ai", "ai_hint", "") or "").strip()
        want_name = str(self._cfg("ai", "tool_name", "") or "").strip()
        if not extra and not want_name:
            return
        try:
            mgr = self.context.provider_manager.llm_tools
            tool = mgr.get_func("show_sugar_menu")
            if tool is None:
                return
            if want_name and want_name != "show_sugar_menu":
                try:
                    tool.name = want_name
                    logger.info(f"{_TAG} 工具名已改为 {want_name}")
                except Exception as e:
                    logger.warning(f"{_TAG} 工具改名失败（保持原名）: {e}")
            if extra and "show_sugar_menu" not in extra:
                base = getattr(tool, "description", "") or ""
                if extra not in base:
                    tool.description = f"{base}\n\n补充判断依据（管理员配置）：{extra}"
                    logger.info(f"{_TAG} 已把 ai_hint 合并进工具描述")
        except Exception as e:
            logger.warning(f"{_TAG} 注入 ai_hint / 工具名失败（不影响使用）: {e}")

    async def initialize(self):
        self._apply_ai_hint()
        logger.info(f"{_TAG} 已加载，菜单条目 {len(self._items())} 项")

    async def terminate(self):
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
