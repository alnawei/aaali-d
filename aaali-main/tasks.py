import asyncio
import db
import config
from aiogram import Bot
from datetime import datetime
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_tea_openapi import models as open_api_models
# 引入我们刚才在 server.py 写的查流量黑科技
from handlers.server import get_real_traffic_gb

# 设置一个简单的内存缓存，防止 80% 预警信息每半小时就给你发一次“轰炸”你
alerted_80_instances = set()

async def traffic_monitor_job(bot: Bot, admin_id: int):
    """后台静默巡查任务：计算流量并执行风控"""
    # 1. 从本地账本拉取所有登记在册的机器
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT instance_id, traffic_limit_gb, traffic_start_time FROM ecs_business")
    rows = cursor.fetchall()
    conn.close()

    # 初始化阿里云 ECS 客户端（用于发送关机指令）
    region_id = "cn-hongkong"
    ali_config = open_api_models.Config(
        access_key_id=config.ALIYUN_ACCESS_KEY_ID,
        access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
        endpoint=f'ecs.{region_id}.aliyuncs.com'
    )
    ecs_client = EcsClient(ali_config)

    for row in rows:
        instance_id = row[0]
        limit_gb = row[1]
        start_time_str = row[2]

        if not start_time_str:
            continue  # 如果这台机器连计费起点都没设置，直接跳过

        # 2. 【核心优化】先看机器是不是开着的，关机状态就不需要查流量了，节省 API 次数
        try:
            req_status = ecs_models.DescribeInstancesRequest(region_id=region_id, instance_ids=f'["{instance_id}"]')
            # 🛠️ 修正 1：将其放入线程池执行，防止同步的网络 HTTP 请求卡死整个 Telegram 机器人主循环
            resp_status = await asyncio.to_thread(ecs_client.describe_instances, req_status)
            if not resp_status.body.instances.instance:
                continue
            status = resp_status.body.instances.instance[0].status
            if status != "Running":
                continue # 只要不是 Running，就放过它
        except Exception as e:
            print(f"⚠️ [流量监控] 查询实例状态异常 ({instance_id}): {e}")
            continue

        # 3. 去查真实的流量 (加入 0.5 秒睡眠，防止并发请求太多被阿里云 API 封禁)
        await asyncio.sleep(0.5) 
        
        # 🛠️ 修正 2：为整个查询和计算逻辑加上 try...except，绝对防止单台机器查流量报错导致后续机器直接中断检查！
        try:
            current_traffic = await asyncio.to_thread(get_real_traffic_gb, instance_id, start_time_str)
            
            # 4. 计算消耗比例，执行风控判断
            usage_percent = current_traffic / limit_gb

            # 🛑 【最高警报】：触发 95% 熔断关机
            if usage_percent >= 0.95:
                try:
                    # 下发强制关机指令！
                    req_stop = ecs_models.StopInstanceRequest(instance_id=instance_id)
                    await asyncio.to_thread(ecs_client.stop_instance, req_stop)
                    
                    # 给老板（你）的 Telegram 发送拔管通知
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"🚨 **【系统熔断断网警报】**\n\n"
                             f"🆔 实例: `{instance_id}`\n"
                             f"📶 当前流量: `{current_traffic} GB` / `{limit_gb} GB`\n"
                             f"⚠️ 消耗已达 **{usage_percent*100:.1f}%**\n"
                             f"🛑 系统已自动执行**物理关机**，防止产生天价流量账单！"
                    )
                    # 既然已经关机了，把它的预警记录清掉，等客户交钱重置后再重新监控
                    alerted_80_instances.discard(instance_id)
                except Exception as e:
                    await bot.send_message(admin_id, f"❌ **警告：对实例 {instance_id} 执行熔断关机失败**，请立刻手动处理！\n报错: {e}")

            # 📢 【中级警报】：触发 80% 预警
            elif usage_percent >= 0.80 and usage_percent < 0.95:
                # 只有还没被警告过的，才发消息，防骚扰
                if instance_id not in alerted_80_instances:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"⚠️ **【流量消耗极速预警】**\n\n"
                             f"🆔 实例: `{instance_id}`\n"
                             f"📶 当前流量: `{current_traffic} GB` / `{limit_gb} GB`\n"
                             f"📢 消耗已达 **{usage_percent*100:.1f}%**，即将面临熔断，请注意客户续费动态。"
                    )
                    alerted_80_instances.add(instance_id) # 标记为“已警告”
            
            # 🟢 【绿灯恢复】：如果有人调高了流量包或重置了流量，比例降下来了
            elif usage_percent < 0.80:
                alerted_80_instances.discard(instance_id) # 解除它的警告标记
                
        except Exception as e:
            # 单台查询失败仅打印日志或提示，用 continue 保证后续其它机器正常被巡查
            print(f"⚠️ [流量监控] 检查实例 {instance_id} 时发生异常: {e}")
            continue

async def daily_billing_check_job(bot: Bot, admin_id: int):
    """每日后台巡查任务：主动推送即将到期的机器，提醒老板催费"""
    import sqlite3
    import db
    
    conn = sqlite3.connect(db.DB_PATH)
    cursor = conn.cursor()
    # 只需要拿实例 ID 和 到期时间
    cursor.execute("SELECT instance_id, expire_time FROM ecs_business")
    rows = cursor.fetchall()
    conn.close()

    now = datetime.now()
    alerts = []

    for row in rows:
        instance_id = row[0]
        expire_time_str = row[1]
        
        if not expire_time_str:
            continue

        try:
            expire_date = datetime.strptime(expire_time_str, "%Y-%m-%d")
            days_left = (expire_date - now).days

            short_id = instance_id[-6:]

            # 💡 精准推送逻辑：只有在剩余3天、1天 和 今天到期时，才发通知。
            # 这样既能起到提醒作用，又不会每天无脑轰炸你。
            if days_left in [3, 1]:
                alerts.append(f"• `...{short_id}` 剩余 **{days_left}** 天 (到期: `{expire_time_str}`)")
            elif days_left == 0:
                alerts.append(f"• `...{short_id}` 🚨 **今日到期！请立即处理！**")
            elif days_left == -1:
                alerts.append(f"• `...{short_id}` ❌ **已过期 1 天，请确认是否拔管释放**")
        except ValueError:
            pass

    # 如果今天有需要催费的机器，就给老板发微信（TG）
    if alerts:
        msg = "🔔 **【每日催费与到期预警】**\n\n老板，以下机器即将到期，请及时联系客户续费：\n\n" + "\n".join(alerts)
        # 🛠️ 修正 3：包裹异常并增加降级策略，防范由于字符原因导致的 Markdown 语法崩溃发不出消息
        try:
            await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"⚠️ Markdown 解析异常，降级为普通文本发送: {e}")
            try:
                await bot.send_message(chat_id=admin_id, text=msg, parse_mode=None)
            except Exception as e2:
                print(f"❌ 催费通知发送彻底失败: {e2}")
