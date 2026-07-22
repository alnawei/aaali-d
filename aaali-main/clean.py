import sys
import db  # 直接引入我们的底层数据库引擎

def clean_ghost_instances():
    """安全清空本地服务器业务账单数据 (带有防呆二次确认)"""
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("⚠️  **警告：高危运维操作** ⚠️")
    print("你正在尝试清空本地数据库 `ecs_business` 表中的所有服务器与计费数据！")
    print("此操作不可逆！(仅建议在测试期或确认阿里云实例已清零时使用)")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # 1. 强制交互式二次确认 (防错敲键盘)
    confirm = input("👉 如果确定要彻底清空，请大写输入 [ YES ] 并回车：").strip()
    if confirm != "YES":
        print("🛑 操作已自动中止，未删除任何数据。")
        sys.exit(0)

    print("\n⏳ 正在挂载安全连接并清理数据...")
    
    # 2. 复用 db.py 内置的防锁连接获取方式 (自带 timeout=5.0)
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        
        # 统计准备删除的行数
        cursor.execute("SELECT COUNT(*) FROM ecs_business")
        count = cursor.fetchone()[0]
        
        # 执行物理清理
        cursor.execute("DELETE FROM ecs_business")
        
        # 3. 执行 SQLite 特有的 VACUUM 碎片整理，彻底收回磁盘空间
        cursor.execute("VACUUM")
        
        conn.commit()
        print(f"✅ 幽灵数据清理成功！共释放了 {count} 条历史服务器账单记录。")
    except Exception as e:
        print(f"❌ 清理过程中遇到数据库异常: {e}")
        conn.rollback()
    finally:
        conn.close()
        print("🔒 数据库连接已安全关闭。")

if __name__ == "__main__":
    clean_ghost_instances()
