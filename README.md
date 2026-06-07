# 二重螺旋插件 - 密函委托定时推送

此项目是完全人机交互的产物（AI编写）
每小时给白名单群推送密函委托信息或使用指令手动触发
AstrBot 插件，用于定时推送《二重螺旋》游戏的密函委托信息。

## 功能
- 每小时自动推送角色、武器、魔之楔委托
- 白名单群管理
- 支持 NapCat / aiocqhttp

## 命令
- /dna_添加白名单          - 添加当前群（推荐）
- /dna_添加白名单 <群号>   - 添加指定群号
- /dna_移除白名单 <群号或UMO> - 移除
- /dna_查看推送群         - 查看白名单
- /dna_推送测试           - 测试获取数据
- /dna_推送所有群         - 立即推送
- /dna_测试推送群         - 发送测试消息
- /dna_启用推送
- /dna_禁用推送
- /dna_重载
- /dna_帮助               -输出此列表

## 安装方法
在 AstrBot 中执行：/plugin install https://github.com/HYLinF/astrbot_plugin_dna_helper
