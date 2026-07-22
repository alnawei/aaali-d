import os
import logging
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# ==========================================
# 日志配置 (全局统一 Logging)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MG_Bot")

# ==========================================
# 读取并严格校验基础配置 (Fail-Fast 机制)
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("❌ 致命错误: 未在 .env 文件中检测到 BOT_TOKEN！程序拒绝启动。")
    raise ValueError("Missing BOT_TOKEN in environment variables.")

# 安全读取 ADMIN_ID
_admin_id = os.getenv("ADMIN_ID", "0")
ADMIN_ID = int(_admin_id) if _admin_id.strip().lstrip('-').isdigit() else 0
if ADMIN_ID == 0:
    logger.warning("⚠️ 警告: ADMIN_ID 未配置或无效，机器人将拒绝任何用户的指令！")

# ==========================================
# 邮件服务配置
# ==========================================
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RECIPIENT = os.getenv("RECIPIENT", "")

# ==========================================
# 阿里云 ECS 核心凭证 (必填校验)
# ==========================================
ALIYUN_ACCESS_KEY_ID = os.getenv("ALIYUN_ACCESS_KEY_ID")
ALIYUN_ACCESS_KEY_SECRET = os.getenv("ALIYUN_ACCESS_KEY_SECRET")

if not ALIYUN_ACCESS_KEY_ID or not ALIYUN_ACCESS_KEY_SECRET:
    logger.critical("❌ 致命错误: 阿里云 AccessKey 凭证缺失！请在 .env 中填入完整 API 密钥。")
    raise ValueError("Missing Aliyun AccessKey credentials.")

# ==========================================
# 统一收口管理文件与数据库路径
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))
