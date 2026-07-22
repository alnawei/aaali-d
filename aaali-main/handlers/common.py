#(基础指令与全局菜单)
from aiogram import Router, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import config

# 实例化当前模块的路由器
router = Router()

def get_main_keyboard():
    """生成底部的四大功能导航栏"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💻 服务器管理"), KeyboardButton(text="📊 流量与计费")],
            [KeyboardButton(text="⚙️ 节点配置"), KeyboardButton(text="🛠 系统设置")]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="请选择控制台功能..."
    )

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id != config.ADMIN_ID: return
    await message.answer("欢迎进入 MG 终极私有控制台 V2.0", reply_markup=get_main_keyboard())
