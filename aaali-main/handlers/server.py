import os
import asyncio
import random
import time
import json
import sqlite3  # 🛠️ 修正 1：补充遗漏的 sqlite3 模块导入，防止底层删机改库时报 NameError
import resend
import config
import glob
import db  # 导入本地账本
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_cms20190101 import models as cms_models
from datetime import datetime
from dateutil.relativedelta import relativedelta
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# 阿里云 SDK 依赖
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ecs20140526 import models as ecs_models

# 实例化当前模块的路由器
router = Router()

# 定义 FSM 状态机
class ServerManagement(StatesGroup):
    waiting_for_code = State()
    waiting_for_region = State()

# ================= 1. 底层 API 与发信函数 =================
def get_template_id(region_id: str) -> str:
    """
    ⚡️ 100% 精准锁定：从 bot_data.db 的 launch_templates 表实时抓取启动模板
    彻底斩断对 .env 假占位符的任何依赖！
    """
    # 1. 把真正的目标数据库 bot_data.db 放在第一优先级
    candidate_dbs = ['/srv/aali/bot_data.db']
    if hasattr(config, 'DB_PATH') and config.DB_PATH not in candidate_dbs:
        candidate_dbs.append(config.DB_PATH)
    candidate_dbs.extend(glob.glob('/srv/aali/*.db'))

    # 2. 遍历查找真实启动模板 ID
    for db_file in candidate_dbs:
        if not os.path.exists(db_file):
            continue
        try:
            conn = sqlite3.connect(db_file, timeout=3.0)
            cursor = conn.cursor()
            # 针对你刚才抓出来的数据结构：('cn-hongkong', '中国香港', 'lt-j6c...')
            cursor.execute("SELECT * FROM launch_templates")
            for row in cursor.fetchall():
                # 只要当前这一组数据中包含我们要开机的地域 (比如 cn-hongkong)
                if any(region_id.lower() == str(col).lower() for col in row):
                    for col in row:
                        if isinstance(col, str) and col.strip().startswith("lt-") and "xxxx" not in col:
                            conn.close()
                            return col.strip() # ⭐ 极速成功返回：lt-j6c2z7laetgycqgdtrcz
            conn.close()
        except Exception:
            continue

    # 3. 如果未配置，直接抛错阻断，绝对不再给阿里云发送假占位符 ID！
    raise ValueError(f"提示：未在系统数据库中找到 [{region_id}] 的启动模板 ID！请在面板的「启动模板管理」中重新添加。")

# 初始化 Resend
resend.api_key = config.RESEND_API_KEY  # 记得去 config.py 里把这个变量读出来

def send_email_sync(code: str) -> bool:
    """彻底抛弃 SMTP，使用新一代 Resend API 发信"""
    try:
        params = {
            "from": "onboarding@resend.dev",
            "to": [config.RECIPIENT],
            "subject": "MG 控制台 V2.0 - 极速 API 验证码",
            "text": f"【MG 控制台】\n\n您的开服验证码是：{code}\n请在 5 分钟内返回 TG 进行验证。",
        }
        email = resend.Emails.send(params)
        print(f"✅ API 发信成功: {email}")
        return True
    except Exception as e:
        print(f"❌ Resend API 发信失败: {e}")
        return False

def create_ecs_instance_sync(region_id: str, template_id: str) -> dict:
    """同步调用阿里云 API 创建实例并轮询获取 IP (后台线程执行)"""
    try:
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)

        run_request = ecs_models.RunInstancesRequest(
            region_id=region_id,
            launch_template_id=template_id,
            amount=1,
            password="@QS00008"
        )
        run_response = client.run_instances(run_request)
        instance_id = run_response.body.instance_id_sets.instance_id_set[0]
        
        describe_request = ecs_models.DescribeInstancesRequest(
            region_id=region_id,
            instance_ids=json.dumps([instance_id])
        )
        
        for _ in range(15):
            time.sleep(5)
            desc_resp = client.describe_instances(describe_request)
            instances = desc_resp.body.instances.instance
            if not instances:
                continue
                
            instance = instances[0]
            status = instance.status
            
            if status == "Running":
                public_ip = "无公网IP"
                if instance.public_ip_address and instance.public_ip_address.ip_address:
                    public_ip = instance.public_ip_address.ip_address[0]
                return {"success": True, "instance_id": instance_id, "ip": public_ip}
            elif status in ["Stopped", "Deleted"]:
                return {"success": False, "error": f"实例状态异常: {status}"}
                
        return {"success": False, "error": "轮询超时，机器可能还在创建中，请稍后去控制台查看。"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_real_instances_sync() -> list:
    """调用阿里云 API，获取所有真实的 ECS 服务器列表"""
    region_id = "cn-hongkong"
    try:
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.DescribeInstancesRequest(region_id=region_id, page_size=50)
        resp = client.describe_instances(req)
        
        instances = []
        if resp.body.instances and resp.body.instances.instance:
            for inst in resp.body.instances.instance:
                ip = "无公网IP"
                if inst.public_ip_address and inst.public_ip_address.ip_address:
                    ip = inst.public_ip_address.ip_address[0]
                instances.append({
                    "id": inst.instance_id,
                    "ip": ip,
                    "status": inst.status
                })
        return instances
    except Exception as e:
        print(f"获取实例列表失败: {e}")
        return []

# ================= 2. 动态折叠菜单 UI 构建器 =================

def get_region_main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🇭🇰 中国香港", callback_data="region_cn-hongkong"))
    builder.row(
        InlineKeyboardButton(text="🌏 亚洲地区", callback_data="menu_asia"),
        InlineKeyboardButton(text="🌍 欧美地区", callback_data="menu_eu_us")
    )
    builder.row(InlineKeyboardButton(text="🐪 中东及其他", callback_data="menu_others"))
    return builder.as_markup()

def get_region_asia_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇯🇵 日本(东京)", callback_data="region_ap-northeast-1"),
        InlineKeyboardButton(text="🇰🇷 韩国(首尔)", callback_data="region_ap-northeast-2")
    )
    builder.row(
        InlineKeyboardButton(text="🇸🇬 新加坡", callback_data="region_ap-southeast-1"),
        InlineKeyboardButton(text="🇲🇾 马来西亚(吉隆坡)", callback_data="region_ap-southeast-3")
    )
    builder.row(
        InlineKeyboardButton(text="🇮🇩 印尼(雅加达)", callback_data="region_ap-southeast-5"),
        InlineKeyboardButton(text="🇵🇭 菲律宾(马尼拉)", callback_data="region_ap-southeast-6")
    )
    builder.row(
        InlineKeyboardButton(text="🇹🇭 泰国(曼谷)", callback_data="region_ap-southeast-7"),
        InlineKeyboardButton(text="🇲🇾 马来西亚(柔佛)", callback_data="region_ap-southeast-8")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()

def get_region_eu_us_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇺🇸 美国(弗吉尼亚)", callback_data="region_us-east-1"),
        InlineKeyboardButton(text="🇺🇸 美国(硅谷)", callback_data="region_us-west-1")
    )
    builder.row(
        InlineKeyboardButton(text="🇲🇽 墨西哥", callback_data="region_na-south-1"),
        InlineKeyboardButton(text="🇬🇧 英国(伦敦)", callback_data="region_eu-west-1")
    )
    builder.row(
        InlineKeyboardButton(text="🇫🇷 法国(巴黎)", callback_data="region_eu-west-2"),
        InlineKeyboardButton(text="🇩🇪 德国(法兰克福)", callback_data="region_eu-central-1")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()

def get_region_others_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇦🇪 阿联酋(迪拜)", callback_data="region_me-east-1"),
        InlineKeyboardButton(text="🇸🇦 沙特(利雅得)", callback_data="region_me-central-1")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()

# ================= 3. 核心业务路由拦截 =================

@router.message(F.text == "💻 服务器管理")
async def cmd_server_management(message: types.Message, state: FSMContext):
    wait_msg = await message.answer("🔄 正在向阿里云获取最新服务器状态，请稍候...")
    instances = await asyncio.to_thread(get_real_instances_sync)
    
    running_count = sum(1 for i in instances if i['status'] == 'Running')
    stopped_count = sum(1 for i in instances if i['status'] in ['Stopped', 'Stopping'])
    pending_count = sum(1 for i in instances if i['status'] in ['Pending', 'Starting'])

    text = (
        "📊 **当前 ECS 服务器概览**\n\n"
        f"🟢 运行中: {running_count} 台\n"
        f"🔴 已停用: {stopped_count} 台\n"
        f"🔵 部署/开机中: {pending_count} 台\n"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ 新增服务器", callback_data="add_server"))
    
    for inst in instances:
        if inst['status'] == 'Running':
            status_emoji = "🟢"
        elif inst['status'] in ['Stopped', 'Stopping']:
            status_emoji = "🔴"
        else:
            status_emoji = "🔵"
            
        btn_text = f"{status_emoji} IP: {inst['ip']}"
        btn_data = f"manage_ecs_{inst['id']}" 
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=btn_data))
    
    await wait_msg.delete()
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# =====================================================================
# ================= 🚀 新增服务器 (邮箱验证 + 调用启动模板) =================
# =====================================================================

@router.callback_query(F.data == "add_server")
async def trigger_add_server(callback: types.CallbackQuery, state: FSMContext):
    """处理【新增服务器】点击事件"""
    if callback.from_user.id != config.ADMIN_ID: 
        return await callback.answer("权限不足！", show_alert=True)

    verify_code = f"{random.randint(0, 999999):06d}"
    await callback.message.answer("⏳ 正在向绑定邮箱发送验证码，请稍候...")
    send_success = await asyncio.to_thread(send_email_sync, verify_code)

    if send_success:
        await state.update_data(code=verify_code, timestamp=time.time())
        await state.set_state(ServerManagement.waiting_for_code)
        await callback.message.answer("✅ 验证码已发送至绑定邮箱！\n请直接在此回复 `6位数字验证码`。")
    else:
        await callback.message.answer("❌ 验证码发送失败，请检查 SMTP 或网络配置。")
    await callback.answer()

@router.message(ServerManagement.waiting_for_code)
async def verify_add_server_code(message: types.Message, state: FSMContext):
    """处理用户输入的验证码，并展示云端启动模板"""
    if message.from_user.id != config.ADMIN_ID: return
    
    user_input_code = message.text.strip()
    user_data = await state.get_data()
    
    if time.time() - user_data.get("timestamp", 0) > 300:
        await state.clear()
        return await message.answer("⚠️ 验证码已过期，请重新进入【💻 服务器管理】点击新增。")

    if user_input_code == user_data.get("code"):
        # 验证成功！清除状态机
        await state.clear()
        
        # 接入数据库模板逻辑
        import db
        templates = db.get_all_templates()
        
        # 🛠️ 修正 2：修复了原代码中 return await message 的致命错误，完善状态转入选择地域菜单
        if not templates:
            return await message.answer("⚠️ 当前没有任何配置好的云端启动模板，请先在控制台或数据库中添加后再试。")
            
        await state.set_state(ServerManagement.waiting_for_region)
        await message.answer(
            "✅ 验证码核对无误！\n请选择您要部署新ECS服务器的目标地域：", 
            reply_markup=get_region_main_menu()
        )
    else:
        await message.answer("❌ 验证码错误，请核对邮箱后重新输入 6 位数字：")

@router.callback_query(ServerManagement.waiting_for_region, F.data.startswith("menu_"))
async def navigate_menus(callback: types.CallbackQuery):
    """处理二级菜单的动态折叠与展开"""
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    
    target_menu = callback.data
    
    if target_menu == "menu_main":
        await callback.message.edit_reply_markup(reply_markup=get_region_main_menu())
    elif target_menu == "menu_asia":
        await callback.message.edit_reply_markup(reply_markup=get_region_asia_menu())
    elif target_menu == "menu_eu_us":
        await callback.message.edit_reply_markup(reply_markup=get_region_eu_us_menu())
    elif target_menu == "menu_others":
        await callback.message.edit_reply_markup(reply_markup=get_region_others_menu())
        
    await callback.answer()

@router.callback_query(ServerManagement.waiting_for_region, F.data.startswith("region_"))
async def execute_run_instances(callback: types.CallbackQuery, state: FSMContext):
    """接收最终的地区选择，调用阿里云 API 自动化开机"""
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    
    region_id = callback.data.replace("region_", "")
    template_id = get_template_id(region_id)
    
    if not template_id:
        return await callback.message.edit_text(f"⚠️ 暂未在系统或 `.env` 中配置 `{region_id}` 对应的启动模板，请配置后重试。")
        
    await state.clear()
    progress_msg = await callback.message.edit_text(f"🚀 已拦截指令。正在向阿里云 `{region_id}` 下发创建任务，请耐心等待 (约需20-40秒)...")
    
    result = await asyncio.to_thread(create_ecs_instance_sync, region_id, template_id)
    
    if result["success"]:
        inst_id = result['instance_id']
        real_ip = result.get('ip', '0.0.0.0')

        # --- ⭐ 核心补救：无论底层有没有存库，马上用 SQL 把真正的 IP 和实例强行写入所有数据库表！ ---
        try:
            import sqlite3, glob
            for db_file in glob.glob('/srv/aali/*.db'):
                conn = sqlite3.connect(db_file, timeout=2.0)
                for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    try:
                        # 1. 如果表里已经有这台机器，强行把 0.0.0.0 覆写为真正的 IP
                        cursor = conn.execute(f"UPDATE {t[0]} SET ip = ? WHERE instance_id = ?", (real_ip, inst_id))
                        # 2. 如果表里压根没存这台机器，直接顺手帮它插一条真实完整的新数据！
                        if cursor.rowcount == 0 and t[0] in ["servers", "ecs_instances", "ecs_business", "instances", "launch_templates"]:
                            try:
                                conn.execute(f"INSERT INTO {t[0]} (instance_id, ip, region_id) VALUES (?, ?, ?)", (inst_id, real_ip, region_id))
                            except Exception: pass
                        conn.commit()
                    except Exception: pass
                conn.close()
        except Exception as e: pass
        # ----------------------------------------------------------------------------------------

        # # 1. 提取实例 ID 并构造一键直达“节点配置第二步”的悬浮内联按钮
        node_config_btn = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⚙️ 节点配置 (选脚本)", callback_data=f"srv_sel:{inst_id}")]
        ])

        # # 2. 组装文案
        text = (
            f"🎉 **MG 控制台扩容成功！**\n\n"
            f"🌏 **地域**: `{region_id}`\n"
            f"🆔 **实例 ID**: `{inst_id}`\n"
            f"🌐 **公网 IP**: `{real_ip}`\n"
            f"✅ **状态**: 运行中\n\n"
            f"安全组与计费模式已按模板自动下发。 "
        )
        # # 3. 将悬浮按钮直接挂载到最终成功提示的消息底部！
        await progress_msg.edit_text(text, parse_mode="Markdown", reply_markup=node_config_btn)
    else:
        await progress_msg.edit_text(f"❌ **创建失败**\n\n原因: {result.get('error')}", parse_mode="Markdown")
        
    await callback.answer()


def get_single_instance_sync(instance_id: str) -> dict:
    """调用阿里云 API 获取单台机器的最新物理状态"""
    region_id = "cn-hongkong"
    try:
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.DescribeInstancesRequest(
            region_id=region_id, 
            instance_ids=json.dumps([instance_id])
        )
        resp = client.describe_instances(req)
        
        if resp.body.instances and resp.body.instances.instance:
            inst = resp.body.instances.instance[0]
            ip = inst.public_ip_address.ip_address[0] if inst.public_ip_address.ip_address else "无公网IP"
            creation_time = inst.creation_time.split('T')[0] if inst.creation_time else "未知"
            return {
                "id": inst.instance_id,
                "ip": ip,
                "status": inst.status,
                "region": region_id,
                "creation_time": creation_time
            }
    except Exception as e:
        print(f"查询单台实例失败: {e}")
    return None


def get_real_traffic_gb(instance_id: str, start_time_str: str) -> float:
    """调用阿里云 CMS 接口，拉取指定时间段内的出网总流量，并转换为 GB"""
    try:
        start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        start_ts = int(time.mktime(start_dt.timetuple()) * 1000)
        end_ts = int(time.time() * 1000)
        
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint='metrics.cn-hongkong.aliyuncs.com'
        )
        client = CmsClient(ali_config)
        
        req = cms_models.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard",
            metric_name="InternetOut",
            dimensions=json.dumps([{"instanceId": instance_id}]),
            start_time=str(start_ts),
            end_time=str(end_ts),
            period="3600"
        )
        resp = client.describe_metric_list(req)
        
        total_bytes = 0
        if resp.body.datapoints:
            datapoints = json.loads(resp.body.datapoints)
            for dp in datapoints:
                val = dp.get("Value", 0) or dp.get("Average", 0) 
                total_bytes += val
                
        total_gb = total_bytes / (1024 ** 3)
        return round(total_gb, 2)
    except Exception as e:
        print(f"获取实例 {instance_id} 流量失败: {e}")
        return 0.0

# ================= 核心：点击服务器 IP 展开详情面板 =================
@router.callback_query(F.data.startswith("manage_ecs_"))
async def process_manage_ecs(callback: types.CallbackQuery):
    instance_id = callback.data.replace("manage_ecs_", "")
    await callback.answer("🔄 正在加载服务器深度数据...")
    
    ali_data = await asyncio.to_thread(get_single_instance_sync, instance_id)
    biz_data = db.get_business_data(instance_id)
    
    if not ali_data:
        await callback.message.answer("❌ 无法从阿里云获取该实例的数据，可能已被释放。")
        return

    status_str = "🟢 运行中" if ali_data['status'] == 'Running' else "🔴 已关机"
    if ali_data['status'] in ['Starting', 'Pending']: status_str = "🔵 正在开机中..."
    if ali_data['status'] in ['Stopping']: status_str = "🔵 正在关机中..."

    start_time_str = biz_data.get('traffic_start_time')
    if not start_time_str:
        now = datetime.now()
        start_time_str = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        db.update_business_data(instance_id, "traffic_start_time", start_time_str)

    current_used_traffic = await asyncio.to_thread(get_real_traffic_gb, instance_id, start_time_str)
    
    text = (
        "📊 **ECS 实例详情**\n\n"
        f"🌍 地域: `{ali_data['region']}`\n"
        f"🆔 实例 ID: `{ali_data['id']}`\n"
        f"🌐 公网 IP: `{ali_data['ip']}`\n"
        f"✅ 状态: {status_str}\n"
        f"📶 本月出网流量: `{current_used_traffic} GB` / `{biz_data['traffic_limit_gb']} GB` (阈值95%防刷断网)\n"
        f"📅 服务器开机时间: `{ali_data['creation_time']}`\n"
        f"⏳ 服务器重置周期: 每月 `{biz_data['reset_day']}` 号重置\n"
        f"👤 客户业务到期: `{biz_data['expire_time']}`\n"
    )

    builder = InlineKeyboardBuilder()
    if ali_data['status'] == 'Running':
        builder.row(InlineKeyboardButton(text="🛑 关机", callback_data=f"power_stop_{instance_id}"))
    else:
        builder.row(InlineKeyboardButton(text="🟢 开机", callback_data=f"power_start_{instance_id}"))
        
    builder.row(
        InlineKeyboardButton(text="💰 续费选项", callback_data=f"renew_menu_{instance_id}"),
        InlineKeyboardButton(text="⚙️ 流量限制", callback_data=f"set_traffic_{instance_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔄 重装系统", callback_data=f"reinstall_os_{instance_id}"),
        InlineKeyboardButton(text="🚀 带宽设置", callback_data=f"set_bandwidth_{instance_id}")
    )
    builder.row(
        InlineKeyboardButton(text="⏳ 修改重置日", callback_data=f"set_resetday_{instance_id}"),
        InlineKeyboardButton(text="🗑️ 释放服务器", callback_data=f"release_ecs_{instance_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data="back_to_list"))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# =====================================================================
# ================= 2. 二级子菜单：全套商业化控制面板 =================
# =====================================================================

@router.callback_query(F.data.startswith("renew_menu_"))
async def process_renew_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("renew_menu_", "")
    await callback.answer()
    
    text = (
        f"⏳ **实例续费管理**\n\n"
        f"🆔 `{instance_id}`\n\n"
        f"💡 **计费规则：按自然月精准叠加**\n"
        f"例如客户 8 号到期，提前在 5 号续费，新到期日将自动累加至下个月 8 号，不吞天数。\n\n"
        f"• 💰 **全部续费**：阿里云物理续费 + 本地客户顺延 1 个月\n"
        f"• ☁️ **仅阿里云续费**：仅物理机续费（囤机器）\n"
        f"• 👤 **仅客户续费**：仅修改本地账本，顺延 1 个月"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 全部续费 (双端同步)", callback_data=f"action_renew_all_{instance_id}"))
    builder.row(
        InlineKeyboardButton(text="☁️ 仅阿里云", callback_data=f"action_renew_ali_{instance_id}"),
        InlineKeyboardButton(text="👤 仅客户", callback_data=f"action_renew_client_{instance_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_traffic_"))
async def process_set_traffic(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_traffic_", "")
    await callback.answer()
    biz_data = db.get_business_data(instance_id)
    limit = biz_data['traffic_limit_gb']
    
    text = (
        f"⚙️ **流量限额与风控熔断**\n\n"
        f"当前客户配额: `{limit} GB` / 月\n"
        f"到达 `{int(limit*0.8)} GB` 触发私聊预警\n"
        f"到达 `{int(limit*0.95)} GB` 触发物理断网（关机）\n\n"
        f"💡 *中途客户加钱买流量包？直接调高配额即可解封。*"
    )
    builder = InlineKeyboardBuilder()
    btn_500 = "🟢 500 GB" if limit == 500 else "⚪ 500 GB"
    btn_1000 = "🟢 1000 GB" if limit == 1000 else "⚪ 1000 GB"
    builder.row(
        InlineKeyboardButton(text=btn_500, callback_data=f"action_tfsize_{instance_id}_500"),
        InlineKeyboardButton(text=btn_1000, callback_data=f"action_tfsize_{instance_id}_1000")
    )
    builder.row(InlineKeyboardButton(text="✏️ 自定义配额 (输入数字)", callback_data=f"input_tflimit_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_resetday_"))
async def process_resetday_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_resetday_", "")
    await callback.answer()
    biz_data = db.get_business_data(instance_id)
    
    start_time_str = biz_data.get('traffic_start_time') or "跟随系统开机"
    
    text = (
        f"⏳ **账期重置与流量清零**\n\n"
        f"📅 当前账单锚点: 每月 `{biz_data['reset_day']}` 号\n"
        f"⏱️ 本期流量起点: `{start_time_str}`\n\n"
        f"💡 **客户中途流量用尽怎么处理？**\n"
        f"不要动锚点日，直接点击下方【重置当月流量】。系统会将统计起点更新为此刻，之前跑的流量瞬间清零，且下个月依然按锚点日正常循环！"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 立即重置当月流量 (清零)", callback_data=f"action_cleartf_{instance_id}"))
    builder.row(InlineKeyboardButton(text="✏️ 修改账单锚点日 (1-28)", callback_data=f"input_resetday_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("reinstall_os_"))
async def process_reinstall_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("reinstall_os_", "")
    await callback.answer()
    
    text = (
        f"🔄 **重装系统 (高危)**\n\n"
        f"⚠️ 警告：重装系统将**彻底抹除**该服务器系统盘上的所有数据，且不可逆！\n"
        f"👉 阿里云要求：执行重装前，**必须先将服务器关机**。"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚠️ 确认重装为 Debian 12", callback_data=f"action_reinstall_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 怂了，返回详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_bandwidth_"))
async def process_bandwidth_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_bandwidth_", "")
    await callback.answer()
    text = f"🚀 **公网带宽峰值调整**\n\n请选择需要调整的临时或永久带宽峰值："
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="30 Mbps", callback_data=f"action_bw_{instance_id}_30"),
        InlineKeyboardButton(text="50 Mbps", callback_data=f"action_bw_{instance_id}_50")
    )
    builder.row(
        InlineKeyboardButton(text="100 Mbps", callback_data=f"action_bw_{instance_id}_100"),
        InlineKeyboardButton(text="200 Mbps", callback_data=f"action_bw_{instance_id}_200")
    )
    builder.row(InlineKeyboardButton(text="✏️ 自定义带宽", callback_data=f"input_bw_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("release_ecs_"))
async def process_release_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("release_ecs_", "")
    await callback.answer()
    text = (
        f"🗑️ **释放服务器 (极其危险)**\n\n"
        f"🆔 实例 ID: `{instance_id}`\n\n"
        f"⚠️ **您正在执行不可逆的销毁操作！**\n"
        f"点击确认后，阿里云将立刻回收该物理机，IP 释放，所有数据彻底灰飞烟灭，且停止计费。"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔥 确认永久销毁该服务器", callback_data=f"action_release_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 怂了，返回详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# =====================================================================
# ================= 3. 危险动作执行区：重装与释放 =====================
# =====================================================================

@router.callback_query(F.data.startswith("action_reinstall_"))
async def execute_reinstall(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_reinstall_", "")
    await callback.message.edit_text("🔄 正在向阿里云下发重装指令，请稍候...\n⚠️ 注意：机器必须处于【已关机】状态才能成功！")
    
    def _do_reinstall():
        region_id = "cn-hongkong"
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.ReplaceSystemDiskRequest(
            region_id=region_id,
            instance_id=instance_id,
            password="@QS00008" 
        )
        return client.replace_system_disk(req)
        
    try:
        await asyncio.to_thread(_do_reinstall)
        await callback.message.edit_text(
            f"✅ **系统重装指令已下发！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🔑 默认密码: `@QS00008`\n"
            f"⏳ 阿里云大约需要 1-3 分钟完成重装，请稍后在控制台尝试重新开机。"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ **重装失败**\n\n原因可能是机器未关机：\n`{e}`")


@router.callback_query(F.data.startswith("action_release_"))
async def execute_release(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_release_", "")
    await callback.message.edit_text("🗑️ 正在执行强制销毁程序...\n1️⃣ 尝试转换计费方式为按量付费\n2️⃣ 尝试执行物理销毁")
    
    # 🛠️ 修正 3：将转按量付费与强制删机拆成独立的同步原子操作，使用 asyncio.sleep 代替阻塞的 time.sleep
    def _do_convert():
        region_id = "cn-hongkong"
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        try:
            req_convert = ecs_models.ModifyInstanceChargeTypeRequest(
                region_id=region_id,
                instance_ids=json.dumps([instance_id]),
                instance_charge_type="PostPaid"
            )
            client.modify_instance_charge_type(req_convert)
        except Exception as e:
            print(f"计费转换提示 (可忽略): {e}")

    def _do_delete():
        region_id = "cn-hongkong"
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req_delete = ecs_models.DeleteInstanceRequest(
            instance_id=instance_id,
            force=True
        )
        return client.delete_instance(req_delete)

    def _clean_local_db():
        conn = sqlite3.connect(db.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ecs_business WHERE instance_id = ?", (instance_id,))
        conn.commit()
        conn.close()

    try:
        # 第一步：异步放入线程池尝试转付费类型
        await asyncio.to_thread(_do_convert)
        # 第二步：纯异步休眠 2 秒等待阿里云后台数据生效，不阻塞线程池工作线程
        await asyncio.sleep(2)
        # 第三步：放入线程池真正执行物理销毁
        await asyncio.to_thread(_do_delete)
        # 第四步：清理数据库
        await asyncio.to_thread(_clean_local_db)

        await callback.message.edit_text(f"🔥 **服务器已被永久释放！**\n\n🆔 实例: `{instance_id}`\n本地业务数据已同步清理。")
    except Exception as e:
        await callback.message.edit_text(f"❌ **释放失败**\n\n可能存在未结清订单或 API 限制：\n`{e}`")

# =====================================================================
# ================= 4. FSM 状态机：等待与处理用户输入 =================
# =====================================================================

class ServerFSM(StatesGroup):
    wait_for_traffic = State()
    wait_for_reset_day = State()
    wait_for_bandwidth = State()

@router.callback_query(F.data.startswith("input_tflimit_"))
async def ask_traffic_limit(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_tflimit_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_traffic)
    
    await callback.message.answer(
        f"✏️ 请直接回复想要为实例 `{instance_id}` 设置的**当月流量限额 (GB)**：\n"
        f"*(请输入纯数字，例如 1500)*"
    )
    await callback.answer()

@router.message(ServerFSM.wait_for_traffic)
async def receive_traffic_limit(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ 格式错误！只能输入纯数字，请重新输入：")
        return
        
    new_limit = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    
    import db
    db.update_business_data(instance_id, "traffic_limit_gb", new_limit)
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 验证一下：返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await message.answer(f"✅ 成功！实例 `{instance_id}` 的流量配额已修改为 **{new_limit} GB**。", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("input_resetday_"))
async def ask_reset_day(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_resetday_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_reset_day)
    
    await callback.message.answer(
        f"📅 请回复您想设置的**每月重置日期 (1-28)**：\n"
        f"*(建议不要设置 29-31，因为 2 月没有这几天)*"
    )
    await callback.answer()

@router.message(ServerFSM.wait_for_reset_day)
async def receive_reset_day(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or not (1 <= int(message.text) <= 28):
        await message.answer("❌ 格式错误！请输入 1 到 28 之间的数字：")
        return
        
    new_day = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    
    import db
    db.update_business_data(instance_id, "reset_day", new_day)
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await message.answer(f"✅ 成功！实例的账单重置日已修改为 每月 **{new_day}** 号。", reply_markup=builder.as_markup())

# =====================================================================
# ================= 5. 执行动作：公网带宽动态调整 =====================
# =====================================================================

@router.callback_query(F.data.startswith("action_bw_"))
async def execute_set_bandwidth_fixed(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    instance_id = parts[2]
    bw_size = int(parts[3])
    
    await callback.message.edit_text(f"🚀 正在向阿里云提交申请，调整带宽至 **{bw_size} Mbps**...")
    
    def _do_modify_bw():
        region_id = "cn-hongkong"
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.ModifyInstanceNetworkSpecRequest(
            instance_id=instance_id,
            internet_max_bandwidth_out=bw_size 
        )
        return client.modify_instance_network_spec(req)
        
    try:
        await asyncio.to_thread(_do_modify_bw)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await callback.message.edit_text(
            f"✅ **带宽调整成功！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🚀 当前公网出网带宽峰值: **{bw_size} Mbps**\n\n"
            f"💡 *提示：配置已即时生效，业务未中断，无需重启服务器。*",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ **带宽调整失败**\n\n原因可能是账户欠费或购买额度受限：\n`{e}`")


@router.callback_query(F.data.startswith("input_bw_"))
async def ask_custom_bandwidth(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_bw_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_bandwidth)
    
    await callback.message.answer(
        f"🚀 请直接回复您想为实例 `{instance_id}` 设置的**自定义带宽峰值 (Mbps)**：\n"
        f"*(请输入 1 到 200 之间的纯数字)*"
    )
    await callback.answer()


@router.message(ServerFSM.wait_for_bandwidth)
async def receive_custom_bandwidth(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ 格式错误！请输入大于 0 的纯数字：")
        return
        
    bw_size = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    await state.clear()
    
    progress_msg = await message.answer(f"🚀 正在向阿里云提交申请，调整带宽至 **{bw_size} Mbps**...")
    
    def _do_modify_bw():
        region_id = "cn-hongkong"
        ali_config = open_api_models.Config(
            access_key_id=config.ALIYUN_ACCESS_KEY_ID,
            access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.ModifyInstanceNetworkSpecRequest(
            instance_id=instance_id,
            internet_max_bandwidth_out=bw_size
        )
        return client.modify_instance_network_spec(req)
        
    try:
        await asyncio.to_thread(_do_modify_bw)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await progress_msg.delete()
        await message.answer(
            f"✅ **自定义带宽调整成功！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🚀 当前公网出网带宽峰值: **{bw_size} Mbps**",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        await progress_msg.delete()
        await message.answer(f"❌ **带宽调整失败**\n\n原因：\n`{e}`")

# =====================================================================
# ================= 6. 执行动作：双端续费核心逻辑 =====================
# =====================================================================

def _extend_client_time(instance_id: str) -> str:
    """本地计算核心：为客户精准增加 1 个自然月"""
    import db
    biz_data = db.get_business_data(instance_id)
    current_expire = biz_data.get('expire_time', '')
    now = datetime.now()
    
    if not current_expire:
        base_date = now
    else:
        try:
            base_date = datetime.strptime(current_expire, "%Y-%m-%d")
            if base_date < now:
                base_date = now 
        except ValueError:
            base_date = now
            
    new_expire = base_date + relativedelta(months=1)
    new_expire_str = new_expire.strftime("%Y-%m-%d")
    
    db.update_business_data(instance_id, "expire_time", new_expire_str)
    db.update_business_data(instance_id, "traffic_start_time", now.strftime("%Y-%m-%d %H:%M:%S"))
    
    return new_expire_str

def _renew_aliyun_instance(instance_id: str):
    """向阿里云发起真实的物理机续费请求"""
    region_id = "cn-hongkong"
    ali_config = open_api_models.Config(
        access_key_id=config.ALIYUN_ACCESS_KEY_ID,
        access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET,
        endpoint=f'ecs.{region_id}.aliyuncs.com'
    )
    client = EcsClient(ali_config)
    req = ecs_models.RenewInstanceRequest(
        instance_id=instance_id,
        period=1 
    )
    return client.renew_instance(req)


@router.callback_query(F.data.startswith("action_renew_all_"))
async def execute_renew_all(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_all_", "")
    await callback.message.edit_text("🔄 正在向阿里云提交续费订单，并更新本地账单...")
    
    try:
        await asyncio.to_thread(_renew_aliyun_instance, instance_id)
        # 🛠️ 修正 4：把本地写数据库操作放入后台线程池执行，保障在高并发或数据库大锁时，主循环依然丝滑
        new_expire = await asyncio.to_thread(_extend_client_time, instance_id)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        await callback.message.edit_text(
            f"✅ **全部续费成功！**\n\n"
            f"💰 阿里云物理机已续费 1 个月。\n"
            f"👤 客户业务期已顺延，新到期日：`{new_expire}`\n"
            f"📶 本期流量统计已重新清零起算。",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        if "InvalidInstanceChargeType" in str(e):
            await callback.message.edit_text("❌ **阿里云续费失败：当前机器是按量付费，无法使用包月续费接口。**\n*(正式交付客户时请开通包年包月机器)*")
        else:
            await callback.message.edit_text(f"❌ **阿里云续费失败，本地账单未改变：**\n`{e}`")


@router.callback_query(F.data.startswith("action_renew_ali_"))
async def execute_renew_ali(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_ali_", "")
    await callback.message.edit_text("🔄 正在向阿里云提交续费订单...")
    
    try:
        await asyncio.to_thread(_renew_aliyun_instance, instance_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        await callback.message.edit_text(
            f"✅ **阿里云物理续费成功！**\n*(本地客户到期时间和流量保持不变)*",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        if "InvalidInstanceChargeType" in str(e):
            await callback.message.edit_text("❌ 失败：按量付费测试机不可包月续费。")
        else:
            await callback.message.edit_text(f"❌ 失败：\n`{e}`")


@router.callback_query(F.data.startswith("action_renew_client_"))
async def execute_renew_client(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_client_", "")
    
    # 🛠️ 修正 5：同步方法转异步调用，防止阻塞
    new_expire = await asyncio.to_thread(_extend_client_time, instance_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 验证一下：返回详情面板", callback_data=f"manage_ecs_{instance_id}"))
    
    await callback.message.edit_text(
        f"✅ **客户业务续费成功！**\n\n"
        f"👤 客户新到期日已顺延至：`{new_expire}`\n"
        f"📶 本期客户流量已自动清零。\n"
        f"*(阿里云底层未产生扣费动作)*",
        reply_markup=builder.as_markup()
    )
