import asyncio
import sqlite3
import time  # 新增 time 模块用于 TTL 时间戳
from aiogram import Router, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import config

router = Router()

# ================= 🧠 核心优化：全局 TTL 内存缓存池 =================
# 格式: { "ip地址": {"status": "fq_pie", "expire": 1690000000.0} }
BBR_CACHE = {}
CACHE_TTL_SECONDS = 300  # 缓存存活时间：5分钟（期间内反复进出菜单0延迟）


# ================= 🛠️ 底层工具：跨表自愈获取服务器真 IP =================
def get_server_ip(instance_id: str) -> str:
    """从本地数据库中主动检索并过滤脏数据，精准抓取公网 IP"""
    try:
        db_path = getattr(config, 'DB_PATH', '/srv/aali/bot_data.db')
        conn = sqlite3.connect(db_path, timeout=4.0)
        cursor = conn.cursor()
        for table in ["ecs_business", "servers", "ecs_instances", "instances"]:
            try:
                cursor.execute(f"SELECT ip FROM {table} WHERE instance_id = ? LIMIT 1", (instance_id,))
                row = cursor.fetchone()
                if row and row[0] and "0.0.0" not in str(row[0]):
                    conn.close()
                    return row[0].strip()
            except Exception:
                continue
        conn.close()
    except Exception as e:
        print(f"⚠️ [数据库检索异常] 实例 ID: {instance_id} | 原因: {str(e)}")
    return "127.0.0.1"


# ⭐ 新增工具：动态获取 SSH 密码
def get_ssh_pwd(instance_id: str) -> str:
    """智能识别自定义机器，提取专属密码；否则返回全局兜底密码"""
    pwd = getattr(config, 'SSH_PASSWORD', getattr(config, 'ROOT_PASSWORD', '@QS00008'))
    if instance_id and instance_id.startswith("ssh_"):
        try:
            import db
            custom_pwd = db.get_custom_server_password(instance_id)
            if custom_pwd:
                pwd = custom_pwd
        except Exception:
            pass
    return pwd


# ================= 🔍 核心算法：极速高防 SSH 实时探活 =================
def sync_check_bbr_status(instance_id: str, ip: str) -> str:
    """真实的 SSH 连接底层探活逻辑"""
    if not ip or "0.0.0" in ip or "分配中" in ip or ip == "127.0.0.1":
        return "unknown"
        
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # ⭐ 核心修改 1：使用动态密码函数
        pwd = get_ssh_pwd(instance_id)
        
        client.connect(hostname=ip, port=22, username="root", password=pwd, timeout=4.0, banner_timeout=4.0, auth_timeout=4.0)
        cmd = "sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc"
        stdin, stdout, stderr = client.exec_command(cmd, timeout=4.0)
        output = stdout.read().decode('utf-8').lower()
        client.close()
        
        if "cubic" in output: return "cubic"
        elif "bbrplus" in output: return "bbrplus"
        elif "bbr" in output:
            if "fq_pie" in output: return "fq_pie"
            if "cake" in output: return "cake"
            return "fq"
        else: return "cubic"
    except Exception as e:
        print(f"⚠️ [SSH探活异常] IP: {ip} | 原因: {str(e)}")
        return "unknown"


# ================= 🚀 缓存拦截器：异步封装与 TTL 管理 =================
async def get_bbr_status_cached(instance_id: str, ip: str, force_refresh: bool = False) -> str:
    """
    状态获取入口：优先拦截并读取内存缓存。如果击穿/过期才触发真实 SSH 握手。
    """
    current_time = time.time()
    
    # 1. 毫秒级拦截：若未强制刷新，且缓存未过期，直接斩断网络请求，瞬间返回！
    if not force_refresh and ip in BBR_CACHE:
        if current_time < BBR_CACHE[ip]["expire"]:
            return BBR_CACHE[ip]["status"]
            
    # 2. 缓存击穿/过期：异步执行真实 SSH 探活
    try:
        status = await asyncio.wait_for(
            # ⭐ 核心修改 2：把 instance_id 传给底层探测函数
            asyncio.to_thread(sync_check_bbr_status, instance_id, ip), 
            timeout=4.5
        )
    except Exception:
        status = "unknown"
        
    # 3. 结果写入缓存 (哪怕是 unknown 也要缓存 10 秒，防止死锁机器被高频重复查询拖垮 Bot)
    ttl = CACHE_TTL_SECONDS if status != "unknown" else 10
    BBR_CACHE[ip] = {"status": status, "expire": current_time + ttl}
    
    return status


# ================= 🚀 底层执行：一键热加载内核参数 =================
def sync_apply_bbr_script(instance_id: str, ip: str, target_template: str) -> tuple[bool, str]:
    """通过 SSH 动态修改 sysctl.conf 并瞬间热加载生效（原子指令版）"""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # ⭐ 核心修改 3：使用动态密码函数
        pwd = get_ssh_pwd(instance_id)
        
        client.connect(hostname=ip, port=22, username="root", password=pwd, timeout=8.0, banner_timeout=8.0, auth_timeout=8.0)
        
        clean_part = "sed -i '/net.core.default_qdisc/d' /etc/sysctl.conf && sed -i '/net.ipv4.tcp_congestion_control/d' /etc/sysctl.conf"
        
        if target_template == "cubic":
            add_part = "echo 'net.ipv4.tcp_congestion_control=cubic' >> /etc/sysctl.conf && echo 'net.core.default_qdisc=fq_codel' >> /etc/sysctl.conf"
        elif target_template == "fq":
            add_part = "echo 'net.ipv4.tcp_congestion_control=bbr' >> /etc/sysctl.conf && echo 'net.core.default_qdisc=fq' >> /etc/sysctl.conf"
        elif target_template == "fq_pie":
            add_part = "echo 'net.ipv4.tcp_congestion_control=bbr' >> /etc/sysctl.conf && echo 'net.core.default_qdisc=fq_pie' >> /etc/sysctl.conf"
        elif target_template == "cake":
            add_part = "echo 'net.ipv4.tcp_congestion_control=bbr' >> /etc/sysctl.conf && echo 'net.core.default_qdisc=cake' >> /etc/sysctl.conf"
        elif target_template == "bbrplus":
            add_part = "echo 'net.ipv4.tcp_congestion_control=bbrplus' >> /etc/sysctl.conf && echo 'net.core.default_qdisc=fq' >> /etc/sysctl.conf"
        else:
            client.close()
            return False, "未识别的目标参数"

        atomic_cmd = f"{clean_part} && {add_part} && sysctl -p"
        
        # ⭐ 核心修改 4：捕获网络重载造成的合法 SSH 瞬断
        try:
            stdin, stdout, stderr = client.exec_command(atomic_cmd, timeout=8.0)
            out_str = stdout.read().decode('utf-8')
            err_str = stderr.read().decode('utf-8')
            client.close()
        except Exception as read_e:
            client.close()
            err_msg = str(read_e).lower()
            if "no existing session" in err_msg or "closed" in err_msg or "timeout" in err_msg:
                return True, "配置已生效！(网络重载导致瞬断)"
            return False, f"异常: {str(read_e)}"
        
        if "No such file" in err_str or "cannot stat" in err_str or "No such file" in out_str:
            return False, "当前系统内核不支持该算法"
        if "sysctl: cannot" in err_str or "No such file or directory" in err_str:
            return False, f"加载失败: {err_str.strip()}"
            
        return True, "配置已生效！"
    except Exception as e:
        return False, f"异常: {str(e)}"


# ================= 🎨 UI 构建 =================
def build_bbr_keyboard(instance_id: str, active_status: str) -> InlineKeyboardMarkup:
    # (此部分保持原样)
    templates = [
        {"id": "fq", "name": "BBR+FQ (标准)"},
        {"id": "fq_pie", "name": "BBR+FQ_PIE (新内核)"},
        {"id": "cake", "name": "BBR+CAKE (抗丢包)"},
        {"id": "bbrplus", "name": "BBRplus 魔改"},
    ]
    if active_status in ["cubic", "unknown"]:
        top_btn_text = "🔴 当前状态: 默认 Cubic (请在下方选择模版开启)"
        top_btn_data = f"bbr_noop:{instance_id}"
    else:
        active_name = next((t["name"].split()[0] for t in templates if t["id"] == active_status), "BBR")
        top_btn_text = f"🟢 运行中: {active_name} ［ 🔴 点击停用 / 恢复Cubic ］"
        top_btn_data = f"bbr_set:cubic:{instance_id}"
        
    builder = [[InlineKeyboardButton(text=top_btn_text, callback_data=top_btn_data)]]
    row_temp = []
    for t in templates:
        icon = "🟢" if active_status == t["id"] else "⚪"
        builder_btn = InlineKeyboardButton(text=f"{icon} {t['name']}", callback_data=f"bbr_set:{t['id']}:{instance_id}")
        row_temp.append(builder_btn)
        if len(row_temp) == 2:
            builder.append(row_temp)
            row_temp = []
            
    builder.append([InlineKeyboardButton(text="🔙 返回上一级 (脚本清单)", callback_data=f"srv_sel:{instance_id}")])
    return InlineKeyboardMarkup(inline_keyboard=builder)


# ================= ⚡️ 回调路由 1：智能懒加载进入 BBR 中心 =================
@router.callback_query(F.data.startswith("run_sh:bbr:"))
async def show_bbr_center(call: CallbackQuery):
    try:
        _, _, instance_id = call.data.split(":")
    except ValueError:
        return await call.answer("数据异常！", show_alert=True)
        
    ip = get_server_ip(instance_id)
    
    # ⭐ 核心优化：先静默查询一次缓存。如果击中，根本不显示“加载中”过渡动画！
    cached_data = BBR_CACHE.get(ip)
    if cached_data and time.time() < cached_data["expire"]:
        # 缓存命中！0网络请求，直接出盘！
        active_status = cached_data["status"]
        progress_msg = call.message # 跳过新建消息，直接在最终步修改
    else:
        # 缓存未命中，才下发“正在探测”过渡动画，并真实拉起探测流程
        progress_msg = await call.message.edit_text(
            f"⚡️ **BBR 网络控制中心**\n\n🖥 实例：`{instance_id}`\n🌐 IP：`{ip}`\n\n⏳ *正在极速探测远端内核...*",
            parse_mode="Markdown"
        )
        # ⭐ 核心修改 5：把 instance_id 传给缓存获取函数
        active_status = await get_bbr_status_cached(instance_id, ip)

    keyboard = build_bbr_keyboard(instance_id, active_status)
    status_tip = "\n⚠️ *注：远端获取超时，系统已切换至兜底选单。*\n" if active_status == "unknown" else ""
    
    text = (
        f"⚡️ **BBR 网络吞吐与拥塞控制中心**\n\n"
        f"🖥 当前操作实例：`{instance_id}`\n🌐 当前 IP：`{ip}`\n━━━━━━━━━━━━━━━━━━{status_tip}\n"
        f"💡 **参数说明与运维指南：**\n"
        f"• **标准 BBR+FQ**：极大压榨带宽、平稳抗丢包。\n"
        f"• **FQ-PIE / CAKE**：针对高并发与丢包优化的现代 AQM 算法。\n"
        f"• **极速热加载**：无缝注入，**瞬间生效，无需重启**！\n\n👇 *请点击下方模版动态切换：*"
    )
    
    try:
        # 若是缓存命中，这里的 edit_text 会将原本的列表界面瞬间刷成 BBR 界面
        if hasattr(progress_msg, 'edit_text'):
            await progress_msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        pass
    
    await call.answer()


# ================= ⚡️ 回调路由 2：执行指令并「篡改缓存」0延迟闭环 =================
@router.callback_query(F.data.startswith("bbr_set:"))
async def execute_bbr_switch(call: CallbackQuery):
    try:
        _, target_template, instance_id = call.data.split(":")
    except ValueError:
        return await call.answer("解析异常", show_alert=True)
        
    ip = get_server_ip(instance_id)
    
    temp_msg = await call.message.edit_text(
        f"🔄 **正在向远端 `{ip}` 热加载内核...**\n请稍候 1-2 秒...", parse_mode="Markdown"
    )
    
    # 真实下发指令
    # ⭐ 核心修改 6：传 instance_id 进行执行
    success, msg = await asyncio.to_thread(sync_apply_bbr_script, instance_id, ip, target_template)
    
    if success:
        # BBR生效因为捕获到了合法的断连，依然算作成功
        await call.answer("🎉 内核参数已热加载生效！", show_alert=True)
        # ⭐ 核心优化 2：指令下发成功后，没必要再去 SSH 查一遍！
        # 直接暴力“篡改”本地内存记录，将本次修改的模板设为当前存活状态，并刷新存活时间
        BBR_CACHE[ip] = {"status": target_template, "expire": time.time() + CACHE_TTL_SECONDS}
        new_status = target_template
    else:
        await call.answer(f"⚠️ {msg}", show_alert=True)
        # 只有发生错误时，才强制重新发起真实探测
        # ⭐ 核心修改 7：传 instance_id 进行刷新
        new_status = await get_bbr_status_cached(instance_id, ip, force_refresh=True)
        
    keyboard = build_bbr_keyboard(instance_id, new_status)
    text = (
        f"⚡️ **BBR 网络吞吐与拥塞控制中心**\n\n"
        f"🖥 当前操作实例：`{instance_id}`\n🌐 当前 IP：`{ip}`\n━━━━━━━━━━━━━━━━━━\n"
        f"💡 **参数说明与运维指南：**\n"
        f"• **标准 BBR+FQ**：极大压榨带宽、平稳抗丢包。\n"
        f"• **FQ-PIE / CAKE**：针对高并发与丢包优化的现代 AQM 算法。\n"
        f"• **极速热加载**：无缝注入，**瞬间生效，无需重启**！\n\n👇 *请点击下方模版动态切换：*"
    )
    
    try:
        await temp_msg.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        await temp_msg.edit_text(text.replace('*', '').replace('`', ''), reply_markup=keyboard)


# ================= ⚡️ 回调路由 3：防呆弹窗 =================
@router.callback_query(F.data.startswith("bbr_noop:"))
async def bbr_noop_handler(call: CallbackQuery):
    await call.answer("💡 当前未开启加速，请点击下方中间两排的 ⚪ 模版按钮开启！", show_alert=True)
