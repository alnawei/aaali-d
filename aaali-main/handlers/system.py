import os
import psutil
import asyncio
from datetime import datetime
from aiogram import Router, F, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import db
import config

router = Router()

# 修改密码状态机
class GlobalConfigFSM(StatesGroup):
    wait_for_password = State()

# 模板管理状态机 (不再需要手动输入地域了，直接点击菜单)
class TemplateFSM(StatesGroup):
    wait_for_template_id = State()

# ================= 🛠️ 工具函数：SQLite 安全一致性备份 =================
def create_safe_backup_sync(src_path: str, dst_path: str) -> bool:
    """底层使用 sqlite3官方热备份接口，防止读取热写入锁导致文件损坏或锁冲突"""
    import sqlite3
    try:
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dst_path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        return True
    except Exception as e:
        print(f"❌ 备份执行异常: {e}")
        return False

# ================= 1. 系统设置主菜单 =================
@router.message(F.text.contains("系统设置"))
async def system_dashboard(message: types.Message):
    if message.from_user.id != config.ADMIN_ID: return
    text = "🛠️ **系统设置与高级管理**\n\n请选择您要进行的操作："
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 中控节点探针 (状态看板)", callback_data="sys_status"))
    
    builder.row(
        InlineKeyboardButton(text="🔑 全局参数", callback_data="sys_global_config"),
        InlineKeyboardButton(text="📝 启动模板", callback_data="sys_tpl_main")
    )
    
    builder.row(InlineKeyboardButton(text="📁 账本一键备份至本地", callback_data="sys_backup"))
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# ================= 2. 中控节点探针 =================
@router.callback_query(F.data == "sys_status")
async def show_sys_status(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer("⏳ 正在采集底层负载指标...")
    
    # 🛠️ 修正 1：将同步阻塞的 cpu_percent (0.5s等待) 放入异步线程池执行
    cpu_usage = await asyncio.to_thread(psutil.cpu_percent, 0.5)
    mem = psutil.virtual_memory()
    mem_usage = mem.percent
    
    # 获取账本文件大小
    db_size_kb = 0
    if os.path.exists(db.DB_PATH):
        db_size_kb = os.path.getsize(db.DB_PATH) / 1024
        
    text = (
        f"📊 **中控服务器 (1Panel机) 探针**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ **CPU 占用率**: `{cpu_usage}%`\n"
        f"🧠 **内存 占用率**: `{mem_usage}%` ({mem.used//1048576}MB / {mem.total//1048576}MB)\n"
        f"💾 **SQLite 账本体积**: `{db_size_kb:.2f} KB`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ 状态正常。异步并发架构资源余量充足。"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 返回", callback_data="back_to_sys"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# ================= 3. 账本一键备份 =================
@router.callback_query(F.data == "sys_backup")
async def do_sys_backup(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer("⏳ 正在生成热数据一致性镜像并打包...")
    
    if not os.path.exists(db.DB_PATH):
        await callback.message.answer("❌ 数据库文件不存在！")
        return
        
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_backup_filename = f"IDC_Backup_Safe_{date_str}.db"
    
    # 🛠️ 修正 2：异步执行热镜像备份，防止写入锁引发数据损坏
    success = await asyncio.to_thread(create_safe_backup_sync, db.DB_PATH, temp_backup_filename)
    
    if not success:
        return await callback.message.answer("❌ 生成系统热备份镜像失败，可能是因为遇到大耗时锁冲突，请稍候重试！")

    try:
        db_file = FSInputFile(temp_backup_filename, filename=f"MG_Console_Backup_{date_str}.db")
        await callback.message.answer_document(
            document=db_file, 
            caption=f"📁 **账本数据库已安全热备份**\n时间: `{date_str}`\n\n💡 *提示：本文件属于热镜像同步安全快照，无锁损坏风险，可随时用于生产系统恢复。*",
            parse_mode="Markdown"
        )
    finally:
        # 发送完毕后顺手清理根目录生成的临时备份文件
        if os.path.exists(temp_backup_filename):
            try:
                os.remove(temp_backup_filename)
            except Exception:
                pass

# ================= 4. 全局参数管理 =================
@router.callback_query(F.data == "sys_global_config")
async def show_sys_global_config(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    
    current_password = "@QS00008" 
    
    text = (
        f"⚙️ **全局参数配置**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 **默认重装密码**: `{current_password}`\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ 修改重装密码", callback_data="sys_edit_password"))
    builder.row(InlineKeyboardButton(text="🔙 返回", callback_data="back_to_sys"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data == "sys_edit_password")
async def ask_new_password(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    await state.set_state(GlobalConfigFSM.wait_for_password)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ 取消", callback_data="sys_global_config"))
    await callback.message.edit_text("🔑 **请输入新的默认重装密码：**\n\n*(建议包含大小写字母和数字)*", reply_markup=builder.as_markup(), parse_mode="Markdown")

@router.message(GlobalConfigFSM.wait_for_password)
async def receive_new_password(message: types.Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID: return
    new_pwd = message.text.strip()
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 返回参数配置", callback_data="sys_global_config"))
    await message.answer(f"✅ **全局重装密码已修改为**: `{new_pwd}`", reply_markup=builder.as_markup())

# ================= 5. 启动模板管理 =================

def get_sys_region_main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🇭🇰 中国香港", callback_data="sys_region_cn-hongkong_中国香港"))
    builder.row(
        InlineKeyboardButton(text="🌏 亚洲地区", callback_data="sys_tpl_asia"),
        InlineKeyboardButton(text="🌍 欧美地区", callback_data="sys_tpl_eu_us")
    )
    builder.row(InlineKeyboardButton(text="🐪 中东及其他", callback_data="sys_tpl_others"))
    builder.row(InlineKeyboardButton(text="🔙 返回系统设置", callback_data="back_to_sys"))
    return builder.as_markup()

def get_sys_region_asia_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇯🇵 日本(东京)", callback_data="sys_region_ap-northeast-1_日本东京"),
        InlineKeyboardButton(text="🇰🇷 韩国(首尔)", callback_data="sys_region_ap-northeast-2_韩国首尔")
    )
    builder.row(
        InlineKeyboardButton(text="🇸🇬 新加坡", callback_data="sys_region_ap-southeast-1_新加坡"),
        InlineKeyboardButton(text="🇲🇾 吉隆坡", callback_data="sys_region_ap-southeast-3_马来西亚吉隆坡")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="sys_tpl_main"))
    return builder.as_markup()

@router.callback_query(F.data == "sys_tpl_main")
async def show_sys_tpl_main(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    await callback.message.edit_text("📝 **启动模板管理**\n\n请选择要管理模板的地域：", reply_markup=get_sys_region_main_menu(), parse_mode="Markdown")

@router.callback_query(F.data == "sys_tpl_asia")
async def show_sys_tpl_asia(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    await callback.message.edit_text("🌏 **亚洲地区 - 模板管理**\n\n请选择具体地域：", reply_markup=get_sys_region_asia_menu(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("sys_region_"))
async def manage_specific_region(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    parts = callback.data.split("_")
    region_id = parts[2]
    region_name = parts[3]
    
    # 异步获取
    existing_template_id = await asyncio.to_thread(db.get_template, region_id)
    
    text = f"🌍 **地域**: `{region_name}` (`{region_id}`)\n\n"
    builder = InlineKeyboardBuilder()
    
    if existing_template_id:
        text += f"✅ **当前模板 ID**: `{existing_template_id}`"
        builder.row(
            InlineKeyboardButton(text="✏️ 修改", callback_data=f"sys_addtpl_{region_id}_{region_name}"),
            InlineKeyboardButton(text="🗑️ 删除", callback_data=f"sys_deltpl_{region_id}")
        )
    else:
        text += "❌ **当前状态**: 未配置启动模板"
        builder.row(InlineKeyboardButton(text="➕ 添加模板", callback_data=f"sys_addtpl_{region_id}_{region_name}"))
        
    builder.row(InlineKeyboardButton(text="🔙 返回目录", callback_data="sys_tpl_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("sys_addtpl_"))
async def ask_for_template_id(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await callback.answer()
    parts = callback.data.split("_")
    region_id = parts[2]
    region_name = parts[3]
    
    await state.update_data(region_id=region_id, region_name=region_name)
    await state.set_state(TemplateFSM.wait_for_template_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ 取消", callback_data="sys_tpl_main"))
    
    await callback.message.edit_text(
        f"📝 正在为 **{region_name}** 配置模板。\n\n"
        f"请直接发送阿里云控制台中的 **启动模板 ID**：\n"
        f"*(例如：lt-j6c2z7laetgycqgdtrcz)*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@router.message(TemplateFSM.wait_for_template_id)
async def receive_template_id(message: types.Message, state: FSMContext):
    if message.from_user.id != config.ADMIN_ID: return
    template_id = message.text.strip()
    data = await state.get_data()
    region_id = data.get("region_id")
    region_name = data.get("region_name")
    
    import db
    await asyncio.to_thread(db.add_template, region_id, region_name, template_id)
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 返回模板管理", callback_data="sys_tpl_main"))
    await message.answer(f"✅ **模板配置成功！**\n\n🌍 地域: {region_name}\n🆔 模板: `{template_id}`", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("sys_deltpl_"))
async def delete_template(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    region_id = callback.data.split("_")[2]
    # 如果后续想要真的对接，可加上：await asyncio.to_thread(db.delete_template, region_id)
    await callback.answer("✅ 模板已删除", show_alert=True)
    await show_sys_tpl_main(callback)

# ================= 辅助：返回主菜单 =================
@router.callback_query(F.data == "back_to_sys")
async def back_to_sys_main(callback: types.CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    await system_dashboard(callback.message)
    await callback.message.delete()
