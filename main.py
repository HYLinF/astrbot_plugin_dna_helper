import aiohttp
import asyncio
import json
import os
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ================= 数据持久化（符合规范） =================
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data",
    "plugins",
    "astrbot_plugin_dna_helper",
)
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DEFAULT_CONFIG = {
    "enable_scheduled_push": True,
    "whitelist_targets": [],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(saved)
                return config
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return False


@register(
    "astrbot_plugin_dna_helper",
    "HYLinF",
    "二重螺旋插件",
    "1.4.8",
    "https://github.com/HYLinF/astrbot_plugin_dna_helper",
)
class DnaHelperPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = load_config()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def on_load(self):
        logger.info("=== 二重螺旋插件 v1.4.8 已加载 ===")
        if self.config.get("enable_scheduled_push"):
            asyncio.create_task(self._delayed_start())

    async def initialize(self):
        """AstrBot 推荐的初始化钩子（重要）"""
        await asyncio.sleep(8)  # 更长的延迟，确保平台适配器已就绪
        await self._start_scheduler()

    async def _delayed_start(self):
        await asyncio.sleep(8)
        await self._start_scheduler()

    async def _start_scheduler(self):
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)

            self.scheduler.add_job(
                self._push_missions_to_whitelist,
                CronTrigger(minute=1, second=30),
                id="dna_mission_push",
                replace_existing=True,
                misfire_grace_time=180,
            )
            self.scheduler.start()
            logger.info("✅ APScheduler 定时任务**成功启动**！（每小时 01:30 执行）")
        except Exception as e:
            logger.error(f"❌ 启动定时任务失败: {e}", exc_info=True)

    async def on_unload(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("插件已卸载")

    # ================= 推送核心 =================
    async def _send_message(self, unified_origin: str, message: str):
        try:
            chain = MessageChain().message(message)
            await self.context.send_message(unified_origin, chain)
            logger.info(f"✅ 推送成功: {unified_origin}")
            return True
        except Exception as e:
            logger.error(f"❌ 推送失败 {unified_origin}: {e}")
            return False

    async def _push_missions_to_whitelist(self):
        try:
            targets = self.config.get("whitelist_targets", [])
            if not targets:
                return

            logger.info(f"开始定时推送 → {len(targets)} 个目标")
            missions = await self._fetch_missions_from_api()
            if not missions:
                logger.warning("获取密函数据失败")
                return

            message_text = self._format_missions_message(missions)
            if not message_text:
                return

            for target in targets:
                await self._send_message(target, message_text)
                await asyncio.sleep(0.8)

            logger.info("定时推送完成")
        except Exception as e:
            logger.error(f"定时推送异常: {e}", exc_info=True)

    async def _fetch_missions_from_api(self):
        url = "https://api.dna-builder.cn/graphql"
        query = '{ missionsIngame(server: "cn") { missions } }'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={"query": query}, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        missions_data = (
                            data.get("data", {})
                            .get("missionsIngame", {})
                            .get("missions")
                        )
                        if isinstance(missions_data, list) and len(missions_data) >= 3:
                            return missions_data
        except Exception as e:
            logger.error(f"API 请求失败: {e}")
        return None

    def _format_missions_message(self, missions_data):
        try:
            categories = ["角色", "武器", "魔之楔"]
            beijing_time = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
            lines = [f"【密函委托更新】{time_str}"]
            for idx, cat in enumerate(categories):
                row = missions_data[idx]
                values = [str(v) for v in row]
                lines.append(f"{cat}：{' '.join(values)}")
                if idx < 2:
                    lines.append("-" * 30)
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"格式化失败: {e}")
            return None

    # ================= 命令 =================
    @filter.command("dna_状态")
    async def status(self, event: AstrMessageEvent):
        enabled = self.config.get("enable_scheduled_push")
        count = len(self.config.get("whitelist_targets", []))
        running = getattr(self.scheduler, "running", False)
        yield event.plain_result(f"""【二重螺旋插件状态】
定时推送: {"✅ 已启用" if enabled else "❌ 已禁用"}
任务状态: {"✅ 运行中" if running else "❌ 未运行"}
推送目标: {count} 个
推送时间: 每小时 01:30
版本: 1.4.8""")

    @filter.command("dna_添加白名单")
    async def add_whitelist(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        target = event.unified_msg_origin if len(parts) < 2 else parts[1].strip()
        current = self.config.setdefault("whitelist_targets", [])
        if target not in current:
            current.append(target)
            save_config(self.config)
            yield event.plain_result(f"✅ 已添加: {target}")
        else:
            yield event.plain_result("已在白名单中")

    @filter.command("dna_移除白名单")
    async def remove_whitelist(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请提供要移除的内容")
            return
        raw = parts[1].strip()
        current = self.config.get("whitelist_targets", [])
        new_list = [t for t in current if t != raw]
        if len(new_list) < len(current):
            self.config["whitelist_targets"] = new_list
            save_config(self.config)
            yield event.plain_result(f"✅ 已移除: {raw}")
        else:
            yield event.plain_result(f"❌ 未找到: {raw}")

    @filter.command("dna_查看推送群")
    async def show_whitelist(self, event: AstrMessageEvent):
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            yield event.plain_result("白名单为空")
        else:
            yield event.plain_result("当前推送目标：\n" + "\n".join(targets))

    @filter.command("dna_测试推送群")
    async def test_push_group(self, event: AstrMessageEvent):
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            yield event.plain_result("白名单为空")
            return
        success = 0
        for t in targets:
            if await self._send_message(
                t, "【二重螺旋助手】这是一条测试消息，您的群已成功接收。"
            ):
                success += 1
            await asyncio.sleep(0.5)
        yield event.plain_result(f"测试完成：成功 {success}/{len(targets)} 个目标。")

    @filter.command("dna_帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result("""【二重螺旋助手命令】
/dna_状态
/dna_添加白名单
/dna_移除白名单 <内容>
/dna_查看推送群
/dna_测试推送群
/dna_启用推送
/dna_禁用推送
/dna_帮助""")
