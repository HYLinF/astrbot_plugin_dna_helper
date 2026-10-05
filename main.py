"""二重螺旋（DNA）密函委托定时推送插件。

每小时定时向白名单群推送《二重螺旋》游戏的密函委托信息（角色 / 武器 / 魔之楔）。
推送以「游戏委托密圈风格」图片展示：通过服务器上的 AstrBot T2I 服务
（HTML -> 图片渲染，端口 8999）将固定模板填入实时玩法名渲染成图；
T2I 不可用时自动回退为文本推送，保证功能不丢。

内容去重：若本次拉取的内容与上次成功推送的内容一致，则不推送，
改为每 5 分钟轮询一次，直到出现新内容再推送，并恢复每小时推送。

依据 AstrBot 官方插件开发指南编写：
https://docs.astrbot.app/en/dev/star/plugin.html
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# ------------------------- 插件元信息 -------------------------
PLUGIN_NAME = "astrbot_plugin_dna_helper"
PLUGIN_VERSION = "2.4.2"
PLUGIN_REPO = "https://github.com/HYLinF/astrbot_plugin_dna_helper"
PLUGIN_DESCRIPTION = "二重螺旋（DNA）密函委托定时推送插件"

# ------------------------- 路径与配置 -------------------------
# 插件目录即配置目录（容器内: /AstrBot/data/plugins/astrbot_plugin_dna_helper）
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "enable_scheduled_push": True,
    # 图片推送开关：关闭时直接推送纯文字（T2I 不可用/渲染失败也会自动回退纯文字）
    "enable_image_push": True,
    # 文字生图 API 地址：留空使用 AstrBot 官方 astrbot-t2i-service 默认地址
    "t2i_api_url": "",
    "whitelist_targets": [],
    # 最近一次成功推送的密函内容指纹，用于内容去重（由插件自动维护）
    "last_pushed_signature": "",
}

# ------------------------- 密函 API -------------------------
MISSIONS_API_URL = "https://api.dna-builder.cn/graphql"
MISSIONS_API_QUERY = '{ missionsIngame(server: "cn") { missions } }'
MISSIONS_API_TIMEOUT = 15  # 秒
API_RETRY_TIMES = 1
API_RETRY_DELAY = 2.0  # 秒

MISSION_CATEGORIES = ("角色", "武器", "魔之楔")
REQUIRED_MISSION_ROWS = 3

# 展示样式常量（模仿游戏「委托密圈」界面；等级不随内容变化，故不展示）
STATUS_LABEL = "当前开放"
BLOCK_SEPARATOR = "━" * 30

# ------------------------- T2I 图片渲染服务 -------------------------
# AstrBot 官方 astrbot-t2i-service 容器，将 HTML 模板渲染为图片。
# 插件容器经 Docker bridge 网关（172.17.0.1）访问宿主机映射的 8999 端口。
T2I_URL = "http://172.17.0.1:8999/text2img/generate"
T2I_TIMEOUT = 30  # 秒
# T2I 服务固定视口 1280x720（Playwright 默认），viewport_width 参数被忽略；
# clip 只截中间内容区域（body 宽 430 居中），左右各留 30px 背景余量，
# 使输出宽度 ≈ 640px 而非全视口 1280px，四周留白更美观。
T2I_CLIP = {"x": 395, "y": 0, "width": 490, "height": 720}
T2I_SCALE_LEVEL = "high"  # device_scale_factor_level: normal=1.0 / high=1.3 / ultra=1.8
T2I_HTML_FONT = '"WenQuanYi Zen Hei","Noto Sans CJK SC",sans-serif'

# ------------------------- 定时任务 -------------------------
# 正常推送：每小时 :01:30 触发
PUSH_JOB_ID = "dna_mission_push"
PUSH_CRON_MINUTE = 1
PUSH_CRON_SECOND = 30
PUSH_MISFIRE_GRACE_SECONDS = 180
PUSH_TARGET_INTERVAL_SECONDS = 0.8
TEST_TARGET_INTERVAL_SECONDS = 0.5

# 内容无变化时的轮询：每 5 分钟一次，直到出现新内容
POLL_JOB_ID = "dna_mission_poll"
POLL_INTERVAL_MINUTES = 5


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化配置，保证返回的配置字段类型正确。"""
    config = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return config
    config.update(raw)

    if not isinstance(config["enable_scheduled_push"], bool):
        config["enable_scheduled_push"] = DEFAULT_CONFIG["enable_scheduled_push"]

    if not isinstance(config["enable_image_push"], bool):
        config["enable_image_push"] = DEFAULT_CONFIG["enable_image_push"]

    if not isinstance(config.get("t2i_api_url"), str):
        config["t2i_api_url"] = ""

    targets = config["whitelist_targets"]
    if not isinstance(targets, list):
        targets = []
    # 统一转为字符串并去重（保持顺序）
    seen = set()
    normalized: list[str] = []
    for item in targets:
        text = str(item)
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    config["whitelist_targets"] = normalized
    return config


def load_config() -> dict[str, Any]:
    """从插件目录加载配置；文件缺失或损坏时回退到默认配置。"""
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return _normalize_config(saved)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"读取配置失败，已使用默认配置: {e}")
        return dict(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> bool:
    """原子化保存配置（先写临时文件再替换），避免写入中断导致配置损坏。"""
    try:
        tmp_file = CONFIG_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, CONFIG_FILE)
        return True
    except OSError as e:
        logger.error(f"保存配置失败: {e}")
        return False


@register(PLUGIN_NAME, "HYLinF", PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO)
class DnaHelperPlugin(Star):
    """二重螺旋密函委托定时推送插件。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # config: AstrBot 官方可视化配置（插件目录 _conf_schema.json → data/config/<插件名>_config.json）。
        # 传 None 时（本地测试/旧环境）回退到插件目录 config.json，保证不丢失功能。
        self._framework_config = config
        if config is not None:
            merged = _normalize_config(dict(config))
            # 一次性迁移：把插件目录 config.json 中的历史数据搬进框架配置；
            # 迁移成功后重置旧配置文件，避免旧数据在重启后反复回灌框架配置。
            legacy = load_config()
            migrated = False
            if not merged.get("whitelist_targets") and legacy.get("whitelist_targets"):
                merged["whitelist_targets"] = legacy["whitelist_targets"]
                migrated = True
            if not merged.get("last_pushed_signature") and legacy.get("last_pushed_signature"):
                merged["last_pushed_signature"] = legacy["last_pushed_signature"]
                migrated = True
            self.config = merged
            if migrated:
                self._save_config()  # 迁移结果立即写回框架配置文件
                try:
                    save_config(dict(DEFAULT_CONFIG))
                except Exception as e:
                    logger.warning(f"重置旧配置文件失败（不影响运行）: {e}")
        else:
            self.config = load_config()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        # 防止定时任务与轮询任务同时推送，造成重复发送
        self._push_lock = asyncio.Lock()
        # 最近一次收到的消息前缀（如 "QQ_BOT"），用于为纯群号白名单条目补全前缀
        self._last_prefix: Optional[str] = None

    def _save_config(self) -> bool:
        """保存配置：优先写入框架可视化配置（AstrBotConfig.save_config），
        无框架配置时回退到插件目录 config.json（原子写）。"""
        if self._framework_config is not None:
            try:
                self._framework_config.clear()
                self._framework_config.update(self.config)
                self._framework_config.save_config()
                return True
            except Exception as e:
                logger.error(f"保存框架配置失败，已回退本地文件: {e}")
                return save_config(self.config)
        return save_config(self.config)

    # ================= 生命周期 =================
    async def initialize(self) -> None:
        """插件激活时调用：若启用了定时推送则启动定时任务。"""
        if self.config.get("enable_scheduled_push", True):
            await self._start_scheduler()
        else:
            logger.info("定时推送已禁用，跳过定时任务启动")

    async def terminate(self) -> None:
        """插件被禁用/重载时调用：停止定时任务，避免任务泄漏。"""
        await self._stop_scheduler()
        logger.info("插件已卸载，定时任务已停止")

    # ================= 定时任务 =================
    async def _start_scheduler(self) -> bool:
        """启动定时任务；已在运行时直接返回 True（幂等）。"""
        if self.scheduler.running:
            return True
        try:
            self.scheduler.add_job(
                self._push_missions_to_whitelist,
                CronTrigger(
                    minute=PUSH_CRON_MINUTE,
                    second=PUSH_CRON_SECOND,
                ),
                id=PUSH_JOB_ID,
                replace_existing=True,
                misfire_grace_time=PUSH_MISFIRE_GRACE_SECONDS,
                max_instances=1,  # 上一轮未结束时不允许并发执行
                coalesce=True,    # 错过的多次触发合并为一次
            )
            self.scheduler.start()
            logger.info(
                f"定时任务已启动（每小时 {PUSH_CRON_MINUTE:02d}:{PUSH_CRON_SECOND:02d} 执行）"
            )
            return True
        except Exception as e:
            logger.error(f"启动定时任务失败: {e}", exc_info=True)
            return False

    async def _stop_scheduler(self) -> None:
        """停止定时任务。

        APScheduler 3.11+ 的 ``shutdown(wait=False)`` 是延迟生效的（要等事件循环
        真正处理完才会停止），因此这里等待其完成，保证「禁用 -> 启用」切换时
        状态一致，不会出现启用了却因旧状态未清理而实际未运行的问题。
        """
        if not self.scheduler.running:
            return
        self.scheduler.shutdown(wait=False)
        for _ in range(20):  # 最多等待约 1 秒
            if not self.scheduler.running:
                break
            await asyncio.sleep(0.05)

    # ================= 数据校验 =================
    @staticmethod
    def _category_rows(missions_data: list) -> Optional[list]:
        """校验密函数据并切分为三个分类行；任何格式异常返回 None（fail fast）。

        统一入口：签名 / 文本 / HTML 三个方法共用，保证「同样数据 → 同样判断」，
        避免某一路径对异常数据静默产生错乱输出。
        """
        if (
            not isinstance(missions_data, list)
            or len(missions_data) < REQUIRED_MISSION_ROWS
        ):
            return None
        rows = []
        for idx, category in enumerate(MISSION_CATEGORIES):
            row = missions_data[idx]
            if not isinstance(row, (list, tuple)) or not row:
                logger.warning(f"密函数据第 {idx + 1} 行（{category}）格式异常，本次跳过")
                return None
            rows.append(row)
        return rows

    # ================= 智能目标解析（纯群号白名单适配） =================
    @staticmethod
    def _extract_prefix(origin: str) -> Optional[str]:
        """从 unified_msg_origin 提取平台前缀（冒号前一段）。"""
        try:
            prefix = (origin or "").split(":", 1)[0]
            return prefix or None
        except Exception:
            return None

    def _detect_prefix(self) -> Optional[str]:
        """推断当前机器人的平台前缀：优先最近消息缓存，其次从白名单已有完整条目提取。"""
        if self._last_prefix:
            return self._last_prefix
        for target in self.config.get("whitelist_targets") or []:
            if ":GroupMessage:" in str(target):
                prefix = self._extract_prefix(str(target))
                if prefix:
                    return prefix
        return None

    def _resolve_target(self, target: str) -> str:
        """把白名单条目解析为可发送的完整消息源 ID。

        - 含 ':' 视为完整格式，原样返回（兼容旧配置）；
        - 纯群号（如 123456789）自动补全为 '<前缀>:GroupMessage:<群号>'；
        - 无法推断前缀时原样返回并记录日志（不崩溃，避免漏推完整条目）。
        """
        target = str(target).strip()
        if ":" in target or not target:
            return target
        prefix = self._detect_prefix()
        if prefix:
            return f"{prefix}:GroupMessage:{target}"
        logger.warning(f"白名单条目 '{target}' 为纯群号且无法推断平台前缀，已原样发送")
        return target

    @staticmethod
    def _targets_equal(a: str, b: str) -> bool:
        """判断两条白名单条目是否指向同一目标（完整格式与纯群号互通）。"""
        a, b = str(a).strip(), str(b).strip()
        if a == b:
            return True
        a_group = a.rsplit("GroupMessage:", 1)[-1] if "GroupMessage:" in a else a
        b_group = b.rsplit("GroupMessage:", 1)[-1] if "GroupMessage:" in b else b
        return a_group == b_group

    def _display_target(self, target: str) -> str:
        """白名单条目的友好展示：完整格式只显示群号。"""
        resolved = self._resolve_target(target)
        if "GroupMessage:" in resolved:
            return resolved.rsplit("GroupMessage:", 1)[-1]
        return resolved

    # ================= 推送核心 =================
    async def _push_missions_to_whitelist(self) -> None:
        """定时任务主体（每小时 01:30）：拉取密函数据。

        若内容与上次成功推送的一致则跳过推送，并转为每 5 分钟轮询；
        直到出现新内容才推送，并移除轮询任务，恢复正常每小时推送。
        """
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            return

        async with self._push_lock:
            logger.info(f"开始定时推送 → {len(targets)} 个目标")
            missions = await self._fetch_missions_from_api()
            if missions is None:
                logger.warning("获取密函数据失败，转为每 5 分钟轮询重试")
                await self._ensure_polling()
                return

            signature = self._missions_signature(missions)
            if not signature:
                logger.warning("轮询：密函数据格式异常，继续等待")
                return
            last = self.config.get("last_pushed_signature") or ""
            if last and signature == last:
                logger.info("密函内容与上次推送一致，跳过推送，转为每 5 分钟轮询")
                await self._ensure_polling()
                return

            message_text = self._format_missions_message(missions)
            if not message_text:
                logger.warning("密函数据格式异常，本次推送跳过")
                return

            success = await self._send_to_targets(missions)
            if success > 0:
                # 只有确实推送成功才记录指纹，避免发送失败后永远不再重试
                self.config["last_pushed_signature"] = signature
                self._save_config()
                await self._stop_polling()
            logger.info(f"定时推送完成：成功 {success}/{len(targets)}")

    async def _poll_missions(self) -> None:
        """轮询任务主体（每 5 分钟）：内容出现变化时推送并恢复每小时推送。"""
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            await self._stop_polling()
            return

        async with self._push_lock:
            missions = await self._fetch_missions_from_api()
            if missions is None:
                logger.warning("轮询：获取密函数据失败，稍后重试")
                return

            signature = self._missions_signature(missions)
            if not signature:
                logger.warning("轮询：密函数据格式异常，继续等待")
                return
            last = self.config.get("last_pushed_signature") or ""
            if last and signature == last:
                logger.info("轮询：密函内容仍与上次一致，继续等待")
                return

            message_text = self._format_missions_message(missions)
            if not message_text:
                logger.warning("轮询：密函数据格式异常，继续等待")
                return

            success = await self._send_to_targets(missions)
            if success > 0:
                self.config["last_pushed_signature"] = signature
                self._save_config()
                await self._stop_polling()
                logger.info("轮询发现新内容并推送成功，恢复正常每小时 01:30 推送")
            else:
                logger.error("轮询：推送失败，稍后重试")

    async def _send_to_targets(self, missions_data: list) -> int:
        """向全部白名单目标推送（优先游戏风格图片，渲染失败回退文本），返回成功数量。"""
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            return 0

        # 图片推送开关：关闭时跳过渲染，直接推送纯文字
        png_bytes = None
        if self.config.get("enable_image_push", True):
            png_bytes = await self._render_missions_image(missions_data)
            if png_bytes is None:
                logger.warning("T2I 图片渲染不可用，本次推送回退为文本消息")
        else:
            logger.info("图片推送已关闭，本次推送使用纯文字")

        success = 0
        for target in targets:
            resolved = self._resolve_target(target)
            if not resolved:
                logger.warning(f"跳过无效推送目标: {target}")
                continue
            if resolved != target:
                logger.info(f"白名单 '{target}' 已自动补全为 '{resolved}'")
            ok = False
            if png_bytes is not None:
                ok = await self._send_image_message(resolved, png_bytes)
                if not ok:
                    logger.warning(f"{resolved} 图片推送失败，回退为文本消息")
                    message_text = self._format_missions_message(missions_data)
                    if message_text:
                        ok = await self._send_message(resolved, message_text)
            else:
                message_text = self._format_missions_message(missions_data)
                if message_text:
                    ok = await self._send_message(resolved, message_text)
            if ok:
                success += 1
            await asyncio.sleep(PUSH_TARGET_INTERVAL_SECONDS)
        return success

    async def _send_image_message(self, unified_origin: str, png_bytes: bytes) -> bool:
        """以 base64 图片形式向单个目标推送（OneBot v11 经 base64:// 直传，无跨容器文件问题）。"""
        try:
            b64 = base64.b64encode(png_bytes).decode("ascii")
            chain = MessageChain().base64_image(b64)
            await self.context.send_message(unified_origin, chain)
            logger.info(f"图片推送成功: {unified_origin}")
            return True
        except Exception as e:
            logger.error(f"图片推送失败 {unified_origin}: {e}")
            return False

    async def _ensure_polling(self) -> None:
        """确保轮询任务存在（幂等）：已存在则保持原节奏，不重置 5 分钟计时。"""
        if not self.scheduler.running:
            return
        try:
            if self.scheduler.get_job(POLL_JOB_ID) is not None:
                return  # 已在轮询中，跳过重建
            self.scheduler.add_job(
                self._poll_missions,
                IntervalTrigger(minutes=POLL_INTERVAL_MINUTES),
                id=POLL_JOB_ID,
                replace_existing=True,
                misfire_grace_time=PUSH_MISFIRE_GRACE_SECONDS,
                max_instances=1,
                coalesce=True,
            )
            logger.info(f"已开启轮询：每 {POLL_INTERVAL_MINUTES} 分钟检测一次内容变化")
        except Exception as e:
            logger.error(f"开启轮询失败: {e}", exc_info=True)

    async def _stop_polling(self) -> None:
        """移除轮询任务（不存在时静默忽略）。"""
        try:
            self.scheduler.remove_job(POLL_JOB_ID)
            logger.info("内容检测轮询已停止，恢复正常每小时推送")
        except Exception:
            pass  # 轮询任务不存在（JobLookupError），无需处理

    @classmethod
    def _missions_signature(cls, missions_data: list) -> str:
        """生成密函内容指纹（不含时间戳，只含实际推送的正文），用于内容去重。"""
        rows = cls._category_rows(missions_data)
        if rows is None:
            return ""
        lines = []
        for idx, category in enumerate(MISSION_CATEGORIES):
            lines.append(f"{category}：{' '.join(str(v) for v in rows[idx])}")
        return "\n".join(lines)

    async def _fetch_missions_from_api(self) -> Optional[list]:
        """请求密函 API；失败时自动重试一次。返回至少 3 行的任务列表，否则返回 None。"""
        last_error: Optional[Exception] = None
        for attempt in range(API_RETRY_TIMES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        MISSIONS_API_URL,
                        json={"query": MISSIONS_API_QUERY},
                        timeout=MISSIONS_API_TIMEOUT,
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(f"密函 API 返回非 200 状态码: {resp.status}")
                        else:
                            data = await resp.json()
                            if not isinstance(data, dict):
                                logger.warning("密函 API 返回结构异常（非对象）")
                            else:
                                missions = (
                                    (data.get("data") or {})
                                    .get("missionsIngame") or {}
                                ).get("missions")
                                if (
                                    isinstance(missions, list)
                                    and len(missions) >= REQUIRED_MISSION_ROWS
                                ):
                                    return missions
                                logger.warning("密函 API 返回的任务数量不足 3 行")
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ValueError,
                AttributeError,
                TypeError,
            ) as e:
                last_error = e
                logger.warning(f"密函 API 请求失败（第 {attempt + 1} 次）: {e}")
            if attempt < API_RETRY_TIMES:
                await asyncio.sleep(API_RETRY_DELAY)
        if last_error is not None:
            logger.error(f"密函 API 请求最终失败: {last_error}")
        return None

    @classmethod
    def _format_missions_message(cls, missions_data: list) -> Optional[str]:
        """将密函数据格式化为游戏「委托密圈」风格的推送文本。

        版式模仿游戏界面：每个分类一个区块（分类名 + 状态 + 玩法列表逐行）。
        """
        rows = cls._category_rows(missions_data)
        if rows is None:
            return None

        beijing_time = datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines = [f"【密函委托更新】{beijing_time}", ""]
        for idx, category in enumerate(MISSION_CATEGORIES):
            lines.append(f"{category}  {STATUS_LABEL}")
            lines.extend(str(v) for v in rows[idx])
            if idx < len(MISSION_CATEGORIES) - 1:
                lines.append(BLOCK_SEPARATOR)
        return "\n".join(lines)

    @classmethod
    def _build_missions_html(cls, missions_data: list, beijing_time: str) -> Optional[str]:
        """构造「密函委托书」风格 HTML 模板（供 T2I 服务渲染为图片）。

        视觉方向：做旧羊皮纸委托书质感——深色桌面背景上浮起一张米白信纸
        （横线纹理 + 做旧斑驳 + 火漆印章），信纸内部分三栏（角色 / 武器 /
        魔之楔）：墨色分类名 + 玩法清单（朱红圆点 + 玩法名 + 序号），
        底部落款更新时间。副标题与「当前开放」徽标按用户反馈精简，等级与
        持有数不随接口数据变化，故不展示。
        """
        rows = cls._category_rows(missions_data)
        if rows is None:
            return None

        def _escape(text: Any) -> str:
            return html.escape(str(text), quote=True)

        cols = []
        for idx, category in enumerate(MISSION_CATEGORIES):
            modes_html = "".join(
                f'<div class="mode">'
                f'<span class="dot"></span>'
                f'<span class="mtext">{_escape(v)}</span>'
                f'<span class="midx">{num:02d}</span>'
                f"</div>"
                for num, v in enumerate(rows[idx], start=1)
            )
            cols.append(
                '<div class="col">'
                f'<div class="col-head"><span class="cat">{_escape(category)}</span></div>'
                '<div class="col-line"></div>'
                f'<div class="modes">{modes_html}</div>'
                "</div>"
            )

        return (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f'<meta name="viewport" content="width={T2I_CLIP["width"]}">'
            "<style>"
            "*{box-sizing:border-box}"
            "html,body{margin:0;padding:0}"
            "body{margin:0 auto;padding:34px 0 22px;width:430px;"
            f"font-family:{T2I_HTML_FONT};color:#33240f;"
            "background:linear-gradient(180deg,rgba(70,48,26,.50) 0%,rgba(70,48,26,.22) 150px,transparent 260px) no-repeat,#1c130a}"
            ".page{position:relative;border-radius:4px;padding:26px 34px 8px;"
            "background:radial-gradient(120% 42% at 86% 0%,rgba(150,110,60,.10),transparent 55%),"
            "radial-gradient(105% 65% at 6% 100%,rgba(120,80,40,.13),transparent 58%),"
            "repeating-linear-gradient(0deg,rgba(120,90,50,.05) 0 1px,transparent 1px 30px),"
            "linear-gradient(180deg,#f6eedb 0%,#efe3c9 100%);"
            "box-shadow:inset 0 0 0 1px rgba(120,90,50,.18)}"
            ".wax{position:absolute;top:-16px;right:30px;width:54px;height:54px;border-radius:50%;"
            "background:radial-gradient(circle at 34% 28%,#c95a40,#8f2f1e 66%);"
            "box-shadow:0 8px 20px rgba(0,0,0,.42),inset 0 0 10px rgba(255,255,255,.22);"
            "display:flex;align-items:center;justify-content:center;"
            "color:#f6ead2;font-size:20px;font-weight:700;letter-spacing:1px}"
            ".hero{text-align:center;margin:0 0 14px}"
            ".title{font-size:32px;font-weight:700;letter-spacing:15px;margin:0;color:#33240f;"
            "text-shadow:0 1px 0 rgba(255,255,255,.5)}"
            ".hero-line{width:58%;height:1px;margin:9px auto 0;position:relative;"
            "background:linear-gradient(90deg,transparent,rgba(80,50,20,.4),transparent)}"
            ".hero-line::after{content:'';position:absolute;left:50%;top:-2px;"
            "transform:translateX(-50%);width:7px;height:6px;background:#a33b22;"
            "clip-path:polygon(0 0,100% 0,50% 100%)}"
            ".board{display:flex;justify-content:center;align-items:stretch;gap:0}"
            ".col{flex:0 0 auto;padding:0 14px;border-right:1px solid rgba(80,50,20,.14)}"
            ".col:first-child{padding-left:0}"
            ".col:last-child{padding-right:0;border-right:none}"
            ".col-head{display:flex;justify-content:center;margin-bottom:5px}"
            ".cat{font-size:19px;font-weight:700;color:#33240f;letter-spacing:5px}"
            ".col-line{height:2px;border-radius:2px;margin-bottom:10px;"
            "background:linear-gradient(90deg,#a33b22 0%,rgba(80,50,20,.30) 72%,transparent)}"
            ".modes{width:100%}"
            ".mode{display:flex;align-items:center;justify-content:center;gap:10px;font-size:15.5px;"
            "color:#4a3a22;padding:6px 2px;border-bottom:1px dotted rgba(80,50,20,.24)}"
            ".mode:last-child{border-bottom:none}"
            ".dot{width:5px;height:5px;border-radius:50%;background:#a33b22;flex:0 0 auto;opacity:.85}"
            ".mtext{text-align:left}"
            ".midx{font-size:10px;color:#a33b22;letter-spacing:1px;opacity:.75;"
            "font-variant-numeric:tabular-nums}"
            ".footer{text-align:center;color:#96774c;font-size:11px;letter-spacing:3px;margin-top:10px;"
            "font-variant-numeric:tabular-nums}"
            "</style></head><body>"
            '<div class="page">'
            '<div class="wax">密</div>'
            '<div class="hero">'
            '<div class="title">密 函 委 托</div>'
            '<div class="hero-line"></div>'
            "</div>"
            f'<div class="board">{"".join(cols)}</div>'
            f'<div class="footer">更新于 {_escape(beijing_time)}</div>'
            "</div>"
            "</body></html>"
        )

    def _t2i_url(self) -> str:
        """文字生图 API 地址：优先使用配置值，留空回退 AstrBot 官方默认。"""
        custom = (self.config.get("t2i_api_url") or "").strip()
        return custom or T2I_URL

    async def _render_missions_image(self, missions_data: list) -> Optional[bytes]:
        """调用 T2I 服务将 HTML 模板渲染为 PNG 图片；任何失败返回 None（由调用方回退文本）。"""
        beijing_time = datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        page_html = self._build_missions_html(missions_data, beijing_time)
        if not page_html:
            return None
        payload = {
            "html": page_html,
            "options": {
                "type": "png",
                "full_page": True,
                "viewport_width": 1280,
                "device_scale_factor_level": T2I_SCALE_LEVEL,
                "clip": T2I_CLIP,
            },
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._t2i_url(), json=payload, timeout=T2I_TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"T2I 渲染失败: HTTP {resp.status}")
                        return None
                    data = await resp.read()
                    if not data.startswith(b"\x89PNG"):
                        logger.warning("T2I 返回内容不是 PNG 图片")
                        return None
                    return self._trim_bottom_blank(data)
        except Exception as e:
            logger.warning(f"T2I 渲染请求失败: {e}")
            return None

    @staticmethod
    def _trim_bottom_blank(png_bytes: bytes) -> bytes:
        """裁掉渲染图底部多余的纯背景空白（T2I 全页截图有时多出一段）。

        从图片底部向上扫描，找第一行存在"前景内容"（亮度 > 80）的位置，
        在其下方保留约 48 像素（按超高 DPI 换算约 12px CSS）后裁剪；
        本就没有明显空白或 PIL 不可用/异常时原样返回，绝不破坏原图。
        """
        try:
            from PIL import Image
            import io as _io

            img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
            width, height = img.size
            if height < 64:
                return png_bytes
            pixels = img.load()
            cutoff = None
            # 从图片底部向上扫描到顶部：T2I 渲染的页面高度可能远超内容
            # （视口默认高度），因此必须扫全高，找到最后一行"有内容"的位置。
            for y in range(height - 1, 0, -1):
                bright = 0
                for x in range(0, width, 3):  # 抽样列，足够判定且快
                    r, g, b = pixels[x, y]
                    lum = (r * 299 + g * 587 + b * 114) // 1000
                    if lum > bright:
                        bright = lum
                if bright > 80:
                    cutoff = y
                    break
            if cutoff is None:
                return png_bytes
            # 底部保留一段深色背景（40px 像素 ≈ 30px CSS，带暖光层次），
            # 信纸下缘缩进、深色背景自然露出；不足时原样返回。
            bottom = min(height, cutoff + 40)
            if bottom >= height - 8:
                return png_bytes
            buf = _io.BytesIO()
            img.crop((0, 0, width, bottom)).save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return png_bytes

    async def _send_message(self, unified_origin: str, message: str) -> bool:
        """向单个目标推送消息；单次失败不影响其他目标。"""
        try:
            chain = MessageChain().message(message)
            await self.context.send_message(unified_origin, chain)
            logger.info(f"推送成功: {unified_origin}")
            return True
        except Exception as e:
            logger.error(f"推送失败 {unified_origin}: {e}")
            return False

    # ================= 消息事件 =================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _learn_prefix(self, event: AstrMessageEvent):
        """记录最近一条消息的平台前缀，供纯群号白名单条目补全使用。"""
        prefix = self._extract_prefix(getattr(event, "unified_msg_origin", "") or "")
        if prefix:
            self._last_prefix = prefix

    # ================= 指令 =================
    @filter.command("dna_状态")
    async def status(self, event: AstrMessageEvent):
        """查看插件与定时任务状态。"""
        enabled = self.config.get("enable_scheduled_push", True)
        count = len(self.config.get("whitelist_targets") or [])
        running = getattr(self.scheduler, "running", False)
        polling = False
        try:
            polling = self.scheduler.get_job(POLL_JOB_ID) is not None
        except Exception:
            polling = False
        has_signature = bool(self.config.get("last_pushed_signature"))
        yield event.plain_result(
            f"【二重螺旋插件状态】\n"
            f"定时推送: {'✅ 已启用' if enabled else '❌ 已禁用'}\n"
            f"任务状态: {'✅ 运行中' if running else '❌ 未运行'}\n"
            f"内容轮询: {'✅ 检测中(每5分钟)' if polling else '未检测'}\n"
            f"推送目标: {count} 个\n"
            f"推送时间: 每小时 01:30（内容无变化自动轮询）\n"
            f"已推送标记: {'有' if has_signature else '无（首次运行将直接推送）'}\n"
            f"版本: {PLUGIN_VERSION}"
        )

    @filter.command("dna_启用推送")
    async def enable_push(self, event: AstrMessageEvent):
        """启用定时推送（立即生效）。"""
        self.config["enable_scheduled_push"] = True
        saved = self._save_config()
        started = await self._start_scheduler()
        if saved and started:
            yield event.plain_result("✅ 已启用定时推送。")
        elif not saved:
            yield event.plain_result("❌ 启用失败：配置保存失败，重启后可能恢复为禁用。")
        else:
            yield event.plain_result("❌ 启用失败：定时任务启动异常，请查看日志。")

    @filter.command("dna_禁用推送")
    async def disable_push(self, event: AstrMessageEvent):
        """禁用定时推送（立即停止定时任务）。"""
        self.config["enable_scheduled_push"] = False
        saved = self._save_config()
        await self._stop_scheduler()
        if saved:
            yield event.plain_result("❌ 已禁用定时推送。")
        else:
            yield event.plain_result("❌ 已禁用定时推送，但配置保存失败，重启后可能恢复为启用。")

    @filter.command("dna_添加白名单")
    async def add_whitelist(self, event: AstrMessageEvent):
        """将当前群或指定目标加入推送白名单。"""
        parts = event.message_str.strip().split(maxsplit=1)
        target = event.unified_msg_origin if len(parts) < 2 else parts[1].strip()
        current = self.config.setdefault("whitelist_targets", [])
        if target not in current:
            current.append(target)
            self._save_config()
            yield event.plain_result(f"✅ 已添加: {target}")
        else:
            yield event.plain_result("已在白名单中")

    @filter.command("dna_移除白名单")
    async def remove_whitelist(self, event: AstrMessageEvent):
        """从白名单移除指定目标。"""
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请提供要移除的内容")
            return
        raw = parts[1].strip()
        current = self.config.get("whitelist_targets") or []
        new_list = [t for t in current if not self._targets_equal(t, raw)]
        if len(new_list) < len(current):
            self.config["whitelist_targets"] = new_list
            self._save_config()
            yield event.plain_result(f"✅ 已移除: {raw}")
        else:
            yield event.plain_result(f"❌ 未找到: {raw}")

    @filter.command("dna_查看推送群")
    async def show_whitelist(self, event: AstrMessageEvent):
        """列出当前推送白名单（显示群号，兼容纯群号与完整格式）。"""
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            yield event.plain_result("白名单为空")
        else:
            lines = [f"{i + 1}. {self._display_target(t)}" for i, t in enumerate(targets)]
            yield event.plain_result("当前推送目标：\n" + "\n".join(lines))

    @filter.command("dna_测试推送群")
    async def test_push_group(self, event: AstrMessageEvent):
        """向所有白名单目标发送一条测试消息。"""
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            yield event.plain_result("白名单为空")
            return
        success = 0
        for target in targets:
            resolved = self._resolve_target(target)
            if not resolved:
                continue
            if await self._send_message(
                resolved, "【二重螺旋助手】这是一条测试消息，您的群已成功接收。"
            ):
                success += 1
            await asyncio.sleep(TEST_TARGET_INTERVAL_SECONDS)
        yield event.plain_result(f"测试完成：成功 {success}/{len(targets)} 个目标。")

    @filter.command("dna_测试信息")
    async def test_fetch_info(self, event: AstrMessageEvent):
        """拉取一次当前密函信息，以游戏风格图片返回给当前群（不影响推送去重记录）。"""
        missions = await self._fetch_missions_from_api()
        if missions is None:
            yield event.plain_result("❌ 获取密函信息失败，请稍后重试。")
            return
        # 附注当前内容与上次推送的关系，方便判断是否需要推送
        signature = self._missions_signature(missions)
        last = self.config.get("last_pushed_signature") or ""
        if last and signature == last:
            note = "（当前内容与上次推送一致）"
        elif last:
            note = "（当前内容与上次推送不同）"
        else:
            note = "（暂无上次推送记录）"

        png_bytes = await self._render_missions_image(missions)
        if png_bytes is not None:
            try:
                b64 = base64.b64encode(png_bytes).decode("ascii")
                chain = MessageChain().base64_image(b64).message(note)
                await self.context.send_message(event.unified_msg_origin, chain)
                yield event.plain_result(f"已发送当前密函信息图片。{note}")
                return
            except Exception as e:
                logger.error(f"测试信息图片发送失败，回退文本: {e}")

        message_text = self._format_missions_message(missions)
        if not message_text:
            yield event.plain_result("❌ 密函数据格式异常，无法生成信息。")
            return
        yield event.plain_result(message_text + "\n\n" + note)

    @filter.command("dna_帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        """显示插件可用指令。"""
        yield event.plain_result(
            "【二重螺旋助手命令】\n"
            "/dna_状态\n"
            "/dna_添加白名单\n"
            "/dna_移除白名单 <内容>\n"
            "/dna_查看推送群\n"
            "/dna_测试推送群\n"
            "/dna_测试信息\n"
            "/dna_启用推送\n"
            "/dna_禁用推送\n"
            "/dna_帮助"
        )
