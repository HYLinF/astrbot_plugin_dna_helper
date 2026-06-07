import aiohttp
import asyncio
import json
import os
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, "config.json")

DEFAULT_CONFIG = {
    "enable_scheduled_push": True,
    "whitelist_targets": [],   # 改成存完整 unified_msg_origin，更稳
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(saved)
                # 兼容旧版只存群号的情况
                if isinstance(config.get("whitelist_groups"), list):
                    config["whitelist_targets"] = config.get("whitelist_targets", []) + config["whitelist_groups"]
                    config.pop("whitelist_groups", None)
                return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False


@register("astrbot_plugin_dna_helper", "HYLinF", "二重螺旋插件", "1.0.1", "https://github.com/HYLinF/astrbot_plugin_dna_helper")
class DnaHelperPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = load_config()
        self.scheduled_task = None
        self.running = True

    async def on_load(self):
        logger.info("二重螺旋插件已加载 (NapCat 适配版)")
        if self.config.get("enable_scheduled_push"):
            await self._start_scheduler()
            logger.info("定时推送已启动（每小时整点 UTC）")
        else:
            logger.info("定时推送未启用")

    async def on_unload(self):
        self.running = False
        if self.scheduled_task:
            self.scheduled_task.cancel()
            try:
                await self.scheduled_task
            except asyncio.CancelledError:
                pass
        logger.info("插件已卸载")

    async def _start_scheduler(self):
        if self.scheduled_task:
            self.scheduled_task.cancel()
        self.scheduled_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.utcnow()
                next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                wait_seconds = (next_hour - now).total_seconds()
                await asyncio.sleep(wait_seconds)
                await self._push_missions_to_whitelist()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时循环出错: {e}")
                await asyncio.sleep(60)

    # ================= 核心发送方法（NapCat 友好） =================
    async def _send_message(self, unified_origin: str, message: str):
        """发送消息（支持 NapCat / aiocqhttp）"""
        try:
            chain = MessageChain().message(message)
            await self.context.send_message(unified_origin, chain)
            logger.info(f"成功发送消息: {unified_origin}")
            return True
        except Exception as e:
            logger.error(f"发送消息失败 {unified_origin}: {e}")
            return False

    async def _push_missions_to_whitelist(self):
        if not self.config.get("enable_scheduled_push"):
            return
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            return

        missions = await self._fetch_missions_from_api()
        if missions is None:
            logger.warning("获取密函信息失败")
            return
        message = self._format_missions_message(missions)
        if not message:
            return

        for target in targets:
            await self._send_message(target, message)
            await asyncio.sleep(0.5)

    async def _fetch_missions_from_api(self):
        url = "https://api.dna-builder.cn/graphql"
        query = "{ missionsIngame(server: \"cn\") { missions } }"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={'query': query}, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'errors' in data:
                            logger.error(f"GraphQL 错误: {data['errors']}")
                            return None
                        missions_data = data.get('data', {}).get('missionsIngame', {}).get('missions')
                        if not isinstance(missions_data, list) or len(missions_data) < 3:
                            logger.error(f"无效的 missions 数据: {missions_data}")
                            return None
                        return missions_data
                    else:
                        logger.error(f"API 状态码: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"API 请求异常: {e}")
            return None

    def _format_missions_message(self, missions_data):
        try:
            if not isinstance(missions_data, list) or len(missions_data) < 3:
                return None
            categories = ['角色', '武器', '魔之楔']
            beijing_time = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
            lines = [f"【密函委托更新】{time_str}"]
            for idx, cat in enumerate(categories):
                row = missions_data[idx]
                if not isinstance(row, list):
                    return None
                values = [str(v) for v in row]
                lines.append(f"{cat}：{'   '.join(values)}")
                if idx < 2:
                    lines.append("-" * 30)
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"格式化消息时出错: {e}")
            return None

    # ================= 白名单管理（推荐使用 /sid 获取完整 UMO） =================
    @filter.command("dna_添加白名单")
    async def add_whitelist(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        target = None

        if len(parts) < 2:
            # 不带参数 → 添加当前群
            target = event.unified_msg_origin
            logger.info(f"尝试添加当前会话: {target}")
        else:
            raw = parts[1].strip()
            if raw.isdigit():
                # 只输入群号时，尝试构造常见 NapCat 格式
                platform = getattr(self.context, 'platform_name', None) or "aiocqhttp"
                target = f"{platform}:GroupMessage:{raw}"
            else:
                target = raw  # 允许直接输入完整 UMO

        if not target:
            yield event.plain_result("添加失败，请检查输入或使用 /sid 查看当前 UMO")
            return

        current = self.config.setdefault("whitelist_targets", [])
        if target not in current:
            current.append(target)
            if save_config(self.config):
                yield event.plain_result(f"✅ 已添加：{target}")
            else:
                yield event.plain_result("保存配置失败")
        else:
            yield event.plain_result("该目标已在白名单中。")

    @filter.command("dna_移除白名单")
    async def remove_whitelist(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("请提供要移除的群号或完整 UMO")
            return

        raw = parts[1].strip()
        current = self.config.get("whitelist_targets", [])
        new_list = [t for t in current if raw not in t and t != raw]
        removed = len(current) - len(new_list)

        if removed > 0:
            self.config["whitelist_targets"] = new_list
            if save_config(self.config):
                yield event.plain_result(f"✅ 成功移除 {removed} 个目标")
            else:
                yield event.plain_result("保存失败")
        else:
            yield event.plain_result("未找到匹配的目标")

    @filter.command("dna_查看推送群")
    async def show_whitelist(self, event: AstrMessageEvent):
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            yield event.plain_result("白名单为空。")
        else:
            text = "当前推送目标：\n" + "\n".join(targets)
            yield event.plain_result(text)

    # ================= 其他命令（保持不变） =================
    @filter.command("dna_推送测试")
    async def test_push(self, event: AstrMessageEvent):
        missions = await self._fetch_missions_from_api()
        if missions is None:
            yield event.plain_result("获取密函信息失败，请检查日志")
            return
        message = self._format_missions_message(missions)
        if message:
            yield event.plain_result(message)
        else:
            yield event.plain_result("数据格式错误")

    @filter.command("dna_推送所有群")
    async def push_to_all_groups(self, event: AstrMessageEvent):
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            yield event.plain_result("白名单为空，请先添加目标。")
            return

        missions = await self._fetch_missions_from_api()
        if missions is None:
            yield event.plain_result("获取密函信息失败")
            return
        message = self._format_missions_message(missions)
        if not message:
            yield event.plain_result("密函数据无效")
            return

        success = 0
        for target in targets:
            if await self._send_message(target, message):
                success += 1
            await asyncio.sleep(0.5)
        yield event.plain_result(f"推送完成：成功 {success}/{len(targets)} 个目标。")

    @filter.command("dna_测试推送群")
    async def test_push_group(self, event: AstrMessageEvent):
        targets = self.config.get("whitelist_targets", [])
        if not targets:
            yield event.plain_result("白名单为空。")
            return
        success = 0
        for target in targets:
            if await self._send_message(target, "【二重螺旋助手】这是一条测试消息，您的群已成功接收。"):
                success += 1
            await asyncio.sleep(0.5)
        yield event.plain_result(f"测试完成：成功 {success}/{len(targets)} 个目标。")

    # 启用/禁用/重载/帮助 等命令保持不变（省略以节省篇幅，你可以从上一版复制）
    @filter.command("dna_启用推送")
    async def enable_push(self, event: AstrMessageEvent):
        self.config["enable_scheduled_push"] = True
        if save_config(self.config):
            await self._start_scheduler()
            yield event.plain_result("定时推送已启用")
        else:
            yield event.plain_result("保存配置失败")

    @filter.command("dna_禁用推送")
    async def disable_push(self, event: AstrMessageEvent):
        self.config["enable_scheduled_push"] = False
        if save_config(self.config):
            self.running = False
            if self.scheduled_task:
                self.scheduled_task.cancel()
                self.scheduled_task = None
            yield event.plain_result("定时推送已禁用")
        else:
            yield event.plain_result("保存配置失败")

    @filter.command("dna_重载")
    async def reload_config(self, event: AstrMessageEvent):
        self.config = load_config()
        self.running = True
        if self.scheduled_task:
            self.scheduled_task.cancel()
            try:
                await self.scheduled_task
            except asyncio.CancelledError:
                pass
            self.scheduled_task = None
        if self.config.get("enable_scheduled_push"):
            await self._start_scheduler()
            yield event.plain_result("配置已重载，定时推送已启用")
        else:
            yield event.plain_result("配置已重载，定时推送已禁用")

    @filter.command("dna_帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        help_text = """【二重螺旋助手命令】
/dna_添加白名单          - 添加当前群（推荐）
/dna_添加白名单 <群号>   - 添加指定群号
/dna_移除白名单 <群号或UMO> - 移除
/dna_查看推送群         - 查看白名单
/dna_推送测试           - 测试获取数据
/dna_推送所有群         - 立即推送
/dna_测试推送群         - 发送测试消息
/dna_启用推送 /dna_禁用推送
/dna_重载 /dna_帮助
"""
        yield event.plain_result(help_text)

    @filter.command("dna")
    async def dna_default(self, event: AstrMessageEvent):
        yield event.plain_result("二重螺旋插件已加载。发送 /dna_帮助 查看命令。")
