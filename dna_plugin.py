"""二重螺旋（DNA）密函委托定时推送插件（主逻辑）。

每小时定时向白名单群推送《二重螺旋》游戏的密函委托信息（角色 / 武器 / 魔之楔）。
推送以「密函委托书」风格图片展示（通过 AstrBot T2I 服务渲染），T2I 不可用时
自动回退为文本推送。官方直连登录（dna_api / dna_login_server）凭据存于此模块
配置 dna_accounts，官方优先、失效自动回退第三方代理。

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
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from dna_api import DNAOfficialAPI, REQUIRED_MISSION_ROWS
from dna_login_server import DNALoginServer, DNA_LOGIN_SESSION_TTL

# ------------------------- 插件元信息 -------------------------
PLUGIN_NAME = "astrbot_plugin_dna_helper"
PLUGIN_VERSION = "2.6.0"
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
    # 图片重点标注开关（两个独立开关，可在 AstrBot 插件配置页分别控制）：
    # 开启后，「探险/无尽」用红色手绘圈、「调停」用蓝色手绘圈在图片中标注重点
    "enable_highlight_explore": True,
    "enable_highlight_mediation": True,
    "whitelist_targets": [],
    # 最近一次成功推送的密函内容指纹，用于内容去重（由插件自动维护）
    "last_pushed_signature": "",
    # ---- v2.7.0 官方直连登录 ----
    # 登录页 HTTP 服务绑定地址与端口（默认仅本机可访问，安全）
    "dna_login_bind_host": "127.0.0.1",
    "dna_login_port": 8899,
    # 登录页公开访问地址：留空则按 http://<bind>:<port> 生成登录链接提示；
    # 若需手机/群友访问，请填公网可访问地址（如 http://<公网IP>:8899）
    # 并自行确保该端口已在防火墙/安全组放行
    "dna_login_public_url": "",
    # 数据源策略：True=官方直连优先（有有效登录则直连官方，失效自动回退第三方代理）
    "dna_source_prefer_official": True,
    # 已绑定账号列表（含官方凭据；权限等同账号本人，仅供本插件拉取密函使用）
    "dna_accounts": [],
}

# ------------------------- 密函 API（第三方代理） -------------------------
MISSIONS_API_URL = "https://api.dna-builder.cn/graphql"
MISSIONS_API_QUERY = '{ missionsIngame(server: "cn") { missions } }'
MISSIONS_API_TIMEOUT = 15  # 秒
API_RETRY_TIMES = 1
API_RETRY_DELAY = 2.0  # 秒

MISSION_CATEGORIES = ("角色", "武器", "魔之楔")

# 账号状态
DNA_ACCOUNT_OK = "正常"
DNA_ACCOUNT_INVALID = "无效"

# 展示样式常量（模仿游戏「委托密圈」界面；等级不随内容变化，故不展示）
STATUS_LABEL = "当前开放"
BLOCK_SEPARATOR = "━" * 30

# ------------------------- 图片重点标注（手绘圈） -------------------------
# 名称匹配使用密函 API 返回的真实字符串（用户常写作"探索/无尽"，API 实为"探险/无尽"）。
HL_EXPLORE_NAMES = ("探险/无尽",)  # 红色手绘圈
HL_MEDIATION_NAMES = ("调停",)     # 蓝色手绘圈
HL_COLORS = {"explore": "#d63a2c", "mediation": "#3f6fb5"}

# 三种手绘圈样式（每次渲染随机取一种；viewBox 100x100，完整包住整行文字）
SCRIBBLE_STYLES: dict[str, dict[str, Any]] = {
    # A 左上开口单圈：连贯平滑，左上留口、末端微甩
    "A": {
        "rotate": -1.2,
        "paths": [
            "M 16 26 C 26 8, 62 6, 84 14 C 95 21, 94 52, 83 62 "
            "C 73 90, 38 93, 25 86 C 13 79, 9 62, 14 50 C 16 41, 21 33, 29 27"
        ],
    },
    # B 右上开口·粗细笔压：左半圈粗、右半圈细（收笔尖细）
    "B": {
        "rotate": -1.4,
        "paths": [
            "M 66 12 C 40 4, 22 14, 14 30 C 6 46, 10 66, 24 78",
            "M 24 78 C 38 88, 62 86, 78 74 C 88 64, 88 44, 80 30 "
            "C 76 22, 71 17, 69 15",
        ],
        "widths": [3.4, 1.6],
    },
    # C 单圈带勾：平滑近圆，左侧收笔弯一个小钩
    "C": {
        "rotate": -0.8,
        "paths": [
            "M 24 18 C 10 30, 8 58, 22 74 C 36 88, 64 86, 80 70 "
            "C 92 54, 90 30, 74 18 C 60 8, 34 8, 27 16 C 23 20, 20 26, 23 32"
        ],
    },
}
SCRIBBLE_DEFAULT_WIDTH = 2.4

# ------------------------- T2I 图片渲染服务 -------------------------
# AstrBot 官方文字生图服务（默认远程端点，实测有效路径为 /text2img/generate）：
# https://t2i.soulter.top/text2img/generate
# 用户可在配置页 t2i_api_url 填自己的服务（如自部署 astrbot-t2i-service 容器）覆盖。
T2I_URL = "https://t2i.soulter.top/text2img/generate"
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

# ================= 配置读写 =================
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

    if not isinstance(config.get("enable_highlight_explore"), bool):
        config["enable_highlight_explore"] = DEFAULT_CONFIG["enable_highlight_explore"]

    if not isinstance(config.get("enable_highlight_mediation"), bool):
        config["enable_highlight_mediation"] = DEFAULT_CONFIG["enable_highlight_mediation"]

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
        self._last_prefix: str | None = None
        # v2.7.0：官方直连登录服务
        self._login_server = DNALoginServer(self)
        # 账号凭据读取/写入互斥锁（与定时推送共用，避免并发写配置）
        self._account_lock = asyncio.Lock()

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
        """插件激活时调用：若启用了定时推送则启动定时任务；同时启动登录服务。"""
        if self.config.get("enable_scheduled_push", True):
            await self._start_scheduler()
        else:
            logger.info("定时推送已禁用，跳过定时任务启动")
        # v2.7.0：登录服务独立监听，不随定时推送开关关闭
        if not await self._login_server.start():
            logger.warning(
                "登录服务启动失败：请检查绑定地址/端口是否被占用（默认 127.0.0.1:8899）"
            )

    async def terminate(self) -> None:
        """插件被禁用/重载时调用：停止定时任务与登录服务，避免任务泄漏。"""
        await self._stop_scheduler()
        await self._login_server.stop()
        logger.info("插件已卸载，定时任务与登录服务已停止")

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
    def _category_rows(missions_data: list) -> list | None:
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
    def _extract_prefix(origin: str) -> str | None:
        """从 unified_msg_origin 提取平台前缀（冒号前一段）。"""
        try:
            prefix = (origin or "").split(":", 1)[0]
            return prefix or None
        except Exception:
            return None

    @staticmethod
    def _mask_mobile(mobile: str) -> str:
        """手机号脱敏：11 位显示前 3 后 4，其余返回未知。"""
        mobile = str(mobile or "")
        return (mobile[:3] + "****" + mobile[-4:]) if len(mobile) == 11 else "未知"

    def _detect_prefix(self) -> str | None:
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
            missions = await self._fetch_missions()
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
            missions = await self._fetch_missions()
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

    async def _fetch_missions(self) -> list | None:
        """按数据源策略拉取密函：官方直连优先，失效自动回退第三方代理。

        返回至少 3 行的任务列表（每行为玩法名列表），否则返回 None。
        """
        prefer_official = bool(self.config.get("dna_source_prefer_official", True))
        if prefer_official:
            missions = await self._fetch_missions_official()
            if missions is not None:
                return missions
            logger.warning("官方直连未取得密函，自动回退第三方代理")
        return await self._fetch_missions_from_third_party()

    async def _fetch_missions_official(self) -> list | None:
        """轮询所有已绑定账号调用官方 defaultRoleForTool，token 失效自动续期。

        任一账号成功即返回三行列表；全部失败返回 None。
        """
        accounts = list(self.config.get("dna_accounts") or [])
        if not accounts:
            return None
        api = DNAOfficialAPI()
        changed = False
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            if acc.get("status") == DNA_ACCOUNT_INVALID:
                continue
            token = str(acc.get("token") or "")
            dev_code = str(acc.get("dev_code") or "")
            if not token or not dev_code:
                continue
            rows = await api.fetch_missions(token, dev_code)
            if rows is not None:
                return rows
            # token 失效 → 尝试 refreshToken 续期一次
            refresh_token = str(acc.get("refresh_token") or "")
            renewed = None
            if refresh_token:
                renewed = await api.refresh(token, refresh_token, dev_code)
            if renewed:
                acc["token"] = renewed["token"]
                if renewed.get("d_num"):
                    acc["d_num"] = renewed["d_num"]
                acc["status"] = DNA_ACCOUNT_OK
                acc["notified_invalid"] = False  # 续期成功 → 清除失效提醒标记
                changed = True
                rows = await api.fetch_missions(renewed["token"], dev_code)
                if rows is not None:
                    return rows
                # 续期成功说明凭据有效；数据仍取不到属官方侧暂时异常，不判失效，本轮走第三方兜底
                logger.warning("官方直连凭据有效但密函数据暂不可用，本轮尝试其他账号/回退第三方")
                continue
            if acc.get("status") != DNA_ACCOUNT_INVALID:
                acc["status"] = DNA_ACCOUNT_INVALID
                changed = True
                # 首次判定失效 → 给绑定 QQ 发私聊提醒（去重：只提醒一次）
                await self._notify_credential_invalid(acc)
        if changed:
            self._save_config()
        return None

    async def _notify_credential_invalid(self, acc: dict) -> None:
        """账号凭证判定失效时，给绑定 QQ 发送私聊提醒（每个账号仅提醒一次，重新登录后重置）。

        只发私聊（FriendMessage），群聊来源（GroupMessage）一律跳过，避免在群里刷屏。
        """
        try:
            if acc.get("notified_invalid"):
                return
            raw = str(acc.get("user_id") or "")
            if not raw:
                return
            # 群聊来源不提醒
            if "GroupMessage" in raw:
                return
            # 已带会话类型（如 QQ_BOT:FriendMessage:QQ号）→ 直接作为私聊目标
            if ":" in raw:
                target = raw
            else:
                # 纯 QQ 号 → 用最近记录的平台前缀补全为私聊目标；拿不到前缀则跳过
                prefix = self._last_prefix or ""
                if not prefix:
                    logger.info("凭证失效提醒跳过：无平台前缀无法定位私聊目标")
                    return
                target = f"{prefix}:FriendMessage:{raw}"
            masked = self._mask_mobile(str(acc.get("mobile") or ""))
            notice = (
                "【密函凭证失效提醒】\n"
                f"你绑定的游戏账号（{masked}）的登录凭证已失效，"
                "官方直连已自动回退第三方代理，密函推送不受影响。\n"
                "如需恢复官方直连，请发送「dna_登录」获取链接重新登录。"
            )
            await self._send_message(target, notice)
            acc["notified_invalid"] = True
            logger.info(f"已提醒账号凭证失效: user_id={raw} target={target}")
        except Exception as e:
            logger.error(f"凭证失效提醒发送失败: {e}", exc_info=True)

    # ================= v2.7.0 账号管理 =================
    def bind_official_account(
        self,
        user_id: str,
        mobile: str,
        token: str,
        refresh_token: str,
        d_num: str,
        dev_code: str,
    ) -> bool:
        """登录成功后写入账号凭据（按 user_id 去重，同人重复登录直接覆盖）。"""
        if not user_id:
            return False
        try:
            accounts = list(self.config.get("dna_accounts") or [])
            entry = {
                "user_id": user_id,
                "mobile": mobile,
                "token": token,
                "refresh_token": refresh_token,
                "d_num": d_num,
                "dev_code": dev_code,
                "status": DNA_ACCOUNT_OK,
                "notified_invalid": False,  # 重新登录 → 清除失效提醒标记
                "bound_at": datetime.now(timezone(timedelta(hours=8))).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            replaced = False
            for i, acc in enumerate(accounts):
                if isinstance(acc, dict) and acc.get("user_id") == user_id:
                    accounts[i] = entry
                    replaced = True
                    break
            if not replaced:
                accounts.append(entry)
            self.config["dna_accounts"] = accounts
            self._save_config()
            logger.info(f"账号绑定成功: user_id={user_id} mobile={mobile[:3]}****{mobile[-4:]}")
            return True
        except Exception as e:
            logger.error(f"账号凭据保存失败: {e}", exc_info=True)
            return False

    def unbind_official_account(self, user_id: str) -> bool:
        """解绑指定账号（返回是否找到并删除）。"""
        try:
            accounts = list(self.config.get("dna_accounts") or [])
            kept = [
                acc
                for acc in accounts
                if not (isinstance(acc, dict) and acc.get("user_id") == user_id)
            ]
            if len(kept) == len(accounts):
                return False
            self.config["dna_accounts"] = kept
            self._save_config()
            return True
        except Exception as e:
            logger.error(f"账号解绑失败: {e}", exc_info=True)
            return False

    async def _fetch_missions_from_third_party(self) -> list | None:
        """请求第三方密函代理 API；失败时自动重试一次。返回至少 3 行的任务列表，否则返回 None。"""
        last_error: Exception | None = None
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
    def _format_missions_message(cls, missions_data: list) -> str | None:
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
    def _build_missions_html(
        cls,
        missions_data: list,
        beijing_time: str,
        hl_explore: bool = True,
        hl_mediation: bool = True,
        scribble_style: str = "A",
    ) -> str | None:
        """构造「密函委托书」风格 HTML 模板（供 T2I 服务渲染为图片）。

        视觉方向：做旧羊皮纸委托书质感——深色桌面背景上浮起一张米白信纸
        （横线纹理 + 做旧斑驳 + 火漆印章），信纸内部分三栏（角色 / 武器 /
        魔之楔）：墨色分类名 + 玩法清单（朱红圆点 + 玩法名 + 序号），
        底部落款更新时间。副标题与「当前开放」徽标按用户反馈精简，等级与
        持有数不随接口数据变化，故不展示。

        重点标注：开启对应开关时，「探险/无尽」加红色手绘圈、「调停」加
        蓝色手绘圈；圈样式为手绘椭圆（三种随机，由调用方传入 scribble_style）。
        """
        rows = cls._category_rows(missions_data)
        if rows is None:
            return None

        def _escape(text: Any) -> str:
            return html.escape(str(text), quote=True)

        def _scribble_svg(color: str, style_key: str) -> str:
            style = SCRIBBLE_STYLES.get(style_key) or SCRIBBLE_STYLES["A"]
            widths = style.get("widths") or [
                SCRIBBLE_DEFAULT_WIDTH
            ] * len(style["paths"])
            paths = "".join(
                f'<path d="{d}" stroke-width="{w}"/>'
                for d, w in zip(style["paths"], widths)
            )
            rot = style["rotate"]
            return (
                '<svg class="scribble" width="100%" height="100%" '
                'viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
                f'<g fill="none" stroke="{color}" stroke-linecap="round" '
                f'stroke-linejoin="round" opacity="0.95" '
                f'vector-effect="non-scaling-stroke" transform="rotate({rot} 50 50)">'
                f"{paths}</g></svg>"
            )

        cols = []
        for idx, category in enumerate(MISSION_CATEGORIES):
            modes_html = ""
            for num, v in enumerate(rows[idx], start=1):
                text = str(v)
                hl = None
                if hl_explore and text in HL_EXPLORE_NAMES:
                    hl = "explore"
                elif hl_mediation and text in HL_MEDIATION_NAMES:
                    hl = "mediation"
                svg = ""
                if hl is not None:
                    modes_html += f'<div class="mode hl-scribble">'
                    # 每个被标注条目独立随机一种手绘圈样式（传固定样式名则整图统一，便于测试）
                    style_key = (
                        scribble_style
                        if scribble_style
                        else random.choice(tuple(SCRIBBLE_STYLES.keys()))
                    )
                    svg = _scribble_svg(HL_COLORS[hl], style_key)
                else:
                    modes_html += '<div class="mode">'
                modes_html += (
                    f"{svg}"
                    f'<span class="dot"></span>'
                    f'<span class="mtext">{_escape(text)}</span>'
                    f'<span class="midx">{num:02d}</span>'
                    f"</div>"
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
            "color:#4a3a22;padding:6px 2px;border-bottom:1px dotted rgba(80,50,20,.24);position:relative}"
            ".mode:last-child{border-bottom:none}"
            ".mode.hl-scribble{border-bottom-color:transparent;background:none}"
            ".scribble{position:absolute;left:0;top:0;width:100%;height:100%;"
            "pointer-events:none;overflow:visible}"
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

    async def _render_missions_image(self, missions_data: list) -> bytes | None:
        """调用 T2I 服务将 HTML 模板渲染为 PNG 图片；任何失败返回 None（由调用方回退文本）。"""
        beijing_time = datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        # 重点标注开关：从配置读取（WebUI 可分别控制）；
        # 圈样式不固定（None）：每个被标注条目独立随机三种手绘圈样式之一
        hl_explore = bool(self.config.get("enable_highlight_explore", True))
        hl_mediation = bool(self.config.get("enable_highlight_mediation", True))
        page_html = self._build_missions_html(
            missions_data,
            beijing_time,
            hl_explore=hl_explore,
            hl_mediation=hl_mediation,
            scribble_style=None,
        )
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
    @staticmethod
    def _extract_user_id(event: AstrMessageEvent) -> str:
        """尽力提取发送者 ID；失败时退回消息来源字符串（仍可唯一定位）。"""
        try:
            sender = getattr(event, "message", None)
            uid = getattr(getattr(sender, "sender", None), "user_id", None)
            if uid:
                return str(uid)
        except Exception:
            pass
        return str(getattr(event, "unified_msg_origin", "") or "")

    # ================= v2.7.0 官方直连指令 =================
    @filter.command("dna_登录")
    async def cmd_dna_login(self, event: AstrMessageEvent):
        """生成官方直连登录链接（会话绑定当前发送者，10 分钟有效）。"""
        actor = {
            "user_id": self._extract_user_id(event),
            "origin": getattr(event, "unified_msg_origin", "") or "",
            "prefix": self._last_prefix or "",
        }
        try:
            auth = await self._login_server.create_session(actor)
        except Exception as e:
            logger.error(f"创建登录会话失败: {e}", exc_info=True)
            yield event.plain_result(
                "❌ 登录服务未运行，请检查插件配置 dna_login_bind_host / dna_login_port 是否被占用。"
            )
            return
        url = f"{self._login_server.public_base}{self._login_server.ROUTE_PREFIX}/{auth}"
        yield event.plain_result(
            "【官方直连登录】\n"
            f"用浏览器打开：\n{url}\n\n"
            "在页面输入「游戏账号绑定的手机号 + 短信验证码」完成登录。\n"
            "登录后官方凭据将绑定到当前账号，推送改为官方直连优先（失效自动回退代理）。\n"
            f"链接 {DNA_LOGIN_SESSION_TTL // 60} 分钟内有效；若无法访问，请在插件配置页填写 "
            "dna_login_public_url（公网地址）并放行端口。"
        )

    @filter.command("dna_账号")
    async def cmd_dna_accounts(self, event: AstrMessageEvent):
        """列出已绑定账号（手机号脱敏）。"""
        accounts = self.config.get("dna_accounts") or []
        if not accounts:
            yield event.plain_result("暂无绑定账号。发送「dna_登录」获取登录链接。")
            return
        lines = ["【已绑定账号】"]
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            masked = self._mask_mobile(str(acc.get("mobile") or ""))
            lines.append(
                f"· {masked}｜{acc.get('status') or '未知'}｜绑定于 {acc.get('bound_at') or '?'}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("dna_绑定状态")
    async def cmd_dna_bind_status(self, event: AstrMessageEvent):
        """查看当前账号的绑定状态与凭证状态（只看自己的）。"""
        my_id = self._extract_user_id(event)
        my_tail = my_id.rsplit(":", 1)[-1]
        accounts = self.config.get("dna_accounts") or []
        mine = None
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            uid_raw = str(acc.get("user_id") or "")
            if uid_raw.rsplit(":", 1)[-1] == my_tail:
                mine = acc
                break
        if mine is None:
            yield event.plain_result(
                "【我的绑定状态】\n"
                "❌ 未绑定任何游戏账号。\n"
                "发送「dna_登录」获取登录链接，用手机号+短信验证码登录后自动绑定到当前账号。"
            )
            return
        masked = self._mask_mobile(str(mine.get("mobile") or ""))
        status = str(mine.get("status") or "未知")
        notified = bool(mine.get("notified_invalid"))
        if status == DNA_ACCOUNT_OK:
            status_note = "凭证正常，官方直连可用"
        elif status == DNA_ACCOUNT_INVALID:
            status_note = (
                "凭证已失效" + ("（已发私聊提醒，请重新登录）" if notified else "（请重新登录恢复）")
            )
        else:
            status_note = f"状态：{status}"
        lines = [
            "【我的绑定状态】",
            f"✅ 已绑定：手机号 {masked}",
            f"· 凭证状态：{status_note}",
            f"· 绑定时间：{mine.get('bound_at') or '?'}",
            "· 数据源：官方直连优先（失效自动回退第三方代理）",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("dna_登出")
    async def cmd_dna_logout(self, event: AstrMessageEvent):
        """解绑当前账号的官方凭据。"""
        user_id = self._extract_user_id(event)
        if self.unbind_official_account(user_id):
            yield event.plain_result("✅ 已解绑当前账号的官方凭据，后续自动回退第三方代理。")
        else:
            yield event.plain_result("当前账号未绑定官方凭据。")

    @filter.command("dna_状态")
    async def cmd_status(self, event: AstrMessageEvent):
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
        login_running = self._login_server._runner is not None
        login_base = self._login_server.public_base
        accounts = self.config.get("dna_accounts") or []
        valid_accounts = sum(
            1 for a in accounts if isinstance(a, dict) and a.get("status") != DNA_ACCOUNT_INVALID
        )
        prefer_official = bool(self.config.get("dna_source_prefer_official", True))
        yield event.plain_result(
            f"【二重螺旋插件状态】\n"
            f"定时推送: {'✅ 已启用' if enabled else '❌ 已禁用'}\n"
            f"任务状态: {'✅ 运行中' if running else '❌ 未运行'}\n"
            f"内容轮询: {'✅ 检测中(每5分钟)' if polling else '未检测'}\n"
            f"推送目标: {count} 个\n"
            f"推送时间: 每小时 01:30（内容无变化自动轮询）\n"
            f"官方直连: {'✅ 运行中' if login_running else '❌ 未运行'}\n"
            f"数据源: {'官方优先+代理兜底' if prefer_official else '第三方代理'}\n"
            f"绑定账号: {valid_accounts}/{len(accounts)} 个有效\n"
            f"登录地址: {login_base}\n"
            f"已推送标记: {'有' if has_signature else '无（首次运行将直接推送）'}\n"
            f"版本: {PLUGIN_VERSION}"
        )

    @filter.command("dna_启用推送")
    async def cmd_enable_push(self, event: AstrMessageEvent):
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
    async def cmd_disable_push(self, event: AstrMessageEvent):
        """禁用定时推送（立即停止定时任务）。"""
        self.config["enable_scheduled_push"] = False
        saved = self._save_config()
        await self._stop_scheduler()
        if saved:
            yield event.plain_result("✅ 已禁用定时推送。")
        else:
            yield event.plain_result("❌ 已禁用定时推送，但配置保存失败，重启后可能恢复为启用。")

    @filter.command("dna_添加白名单")
    async def cmd_add_whitelist(self, event: AstrMessageEvent):
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
    async def cmd_remove_whitelist(self, event: AstrMessageEvent):
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
    async def cmd_show_whitelist(self, event: AstrMessageEvent):
        """列出当前推送白名单（显示群号，兼容纯群号与完整格式）。"""
        targets = self.config.get("whitelist_targets") or []
        if not targets:
            yield event.plain_result("白名单为空")
        else:
            lines = [f"{i + 1}. {self._display_target(t)}" for i, t in enumerate(targets)]
            yield event.plain_result("当前推送目标：\n" + "\n".join(lines))

    @filter.command("dna_测试推送群")
    async def cmd_test_push_group(self, event: AstrMessageEvent):
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
    async def cmd_test_fetch_info(self, event: AstrMessageEvent):
        """拉取一次当前密函信息，以游戏风格图片返回给当前群（不影响推送去重记录）。"""
        missions = await self._fetch_missions()
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

        # 测试信息同样遵循「图片推送开关」：关闭时直接返回纯文字
        png_bytes = None
        if self.config.get("enable_image_push", True):
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
    async def cmd_help(self, event: AstrMessageEvent):
        """显示插件可用指令。"""
        yield event.plain_result(
            "【二重螺旋助手命令】\n"
            "/dna_状态\n"
            "/dna_登录  ← 官方直连登录（手机号+验证码）\n"
            "/dna_账号  ← 查看已绑定账号\n"
            "/dna_登出  ← 解绑当前账号官方凭据\n"
            "/dna_添加白名单\n"
            "/dna_移除白名单 <内容>\n"
            "/dna_查看推送群\n"
            "/dna_测试推送群\n"
            "/dna_测试信息\n"
            "/dna_启用推送\n"
            "/dna_禁用推送\n"
            "/dna_帮助"
        )


def _fix_astrbot_plugin_registration(entry_module_path: str | None = None) -> None:
    """AstrBot 兼容：把本插件在 star_map 中的注册项补一个入口模块路径别名。

    AstrBot 的 star_manager 加载插件时用 ``path in star_map`` 判断插件是否通过
    ``__init_subclass__`` 注册（``star_map`` 的 key 是插件主类的 ``cls.__module__``）。
    单文件时代主类定义在入口模块（被加载为 ``data.plugins.astrbot_plugin_dna_helper.main``），
    key 恰好等于 ``path``，命中后才会按 ``get_handlers_by_module_name(metadata.module_path)``
    找到本插件的全部 handler 并用 ``functools.partial(star_cls)`` 完成实例绑定。

    拆分后主类定义在子模块（运行时 ``__module__`` = ``dna_plugin``），``star_map`` 的
    key 变成 ``dna_plugin``，与入口模块路径不匹配 → 落入 legacy 加载分支且不做 handler
    实例绑定 → 命令以未绑定函数被调用并抛出 ``TypeError: ... missing 'event'``。

    这里在入口 main.py 全部 handler 注册完成后调用，把 ``star_map["dna_plugin"]``
    条目同时挂到入口模块路径下，使 ``path in star_map`` 命中并走回正常绑定分支。
    非 AstrBot 环境（本地测试 / 编译）下 star_map 无本插件条目，静默跳过。
    """
    if not entry_module_path:
        return
    try:
        from astrbot.core.star.star import star_map as _star_map

        _md = _star_map.get(__name__)
        _pre_exist = entry_module_path in _star_map
        if _md is not None and not _pre_exist:
            _star_map[entry_module_path] = _md
        logger.warning(
            f"[dna-fix] entry={entry_module_path!r} my={__name__!r} "
            f"star_map_keys={list(_star_map)} md={getattr(_md, 'module_path', None)!r} "
            f"pre_exist={_pre_exist} registered={_md is not None}"
        )
    except Exception as _e:  # noqa: BLE001
        # 非 AstrBot 运行时（如本地 py_compile / 独立脚本导入）无需修复
        logger.warning(f"[dna-fix] EXC {type(_e).__name__}: {_e}")
