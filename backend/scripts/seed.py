"""种子数据：组织架构 + 超管账号 + 默认模型配置"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.user import Department, User, Role
from app.models.skill import ModelConfig
from passlib.hash import bcrypt


def seed():
    db = SessionLocal()

    # 清理已有数据（幂等）
    if db.query(User).filter(User.username == "admin").first():
        print("Seed data already exists, skipping.")
        db.close()
        return

    # 组织架构
    corp = Department(name="公司经营发展中心", category="后台", business_unit="公司经营发展中心")
    db.add(corp)
    db.flush()

    cid_bu = Department(name="国内电商广告事业部", category="前台", business_unit="国内电商广告事业部")
    dic_bu = Department(name="AI云浏览器事业部", category="前台", business_unit="AI云浏览器事业部")
    db.add_all([cid_bu, dic_bu])
    db.flush()

    depts = [
        # 后台职能
        Department(name="总裁办", category="后台", business_unit="公司经营发展中心", parent_id=corp.id),
        Department(name="财务部", category="后台", business_unit="公司经营发展中心", parent_id=corp.id),
        Department(name="HR&行政部", category="后台", business_unit="公司经营发展中心", parent_id=corp.id),
        Department(name="法务部", category="后台", business_unit="公司经营发展中心", parent_id=corp.id),
        # 前台业务 CID
        Department(name="CID商业化", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id),
        Department(name="电商投流运营部", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id),
        Department(name="商城项目部", category="前台", business_unit="国内电商广告事业部", parent_id=cid_bu.id),
        # 前台业务 DIC
        Department(name="DIC商业化", category="前台", business_unit="AI云浏览器事业部", parent_id=dic_bu.id),
        # 中台产研 CID
        Department(name="CID产研", category="中台", business_unit="国内电商广告事业部", parent_id=cid_bu.id),
        # 中台产研 DIC
        Department(name="DIC产研", category="中台", business_unit="AI云浏览器事业部", parent_id=dic_bu.id),
    ]
    db.add_all(depts)
    db.flush()

    # 超级管理员
    admin = User(
        username="admin",
        password_hash=bcrypt.hash("admin123"),
        display_name="超级管理员",
        role=Role.SUPER_ADMIN,
    )
    db.add(admin)

    # 默认模型配置
    default_model = ModelConfig(
        name="DeepSeek-V3",
        provider="deepseek",
        model_id="deepseek-chat",
        api_base="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        max_tokens=4096,
        temperature="0.7",
        is_default=True,
    )
    db.add(default_model)

    db.commit()
    db.close()
    print("Seed complete: org structure, admin user, default model config.")


if __name__ == "__main__":
    seed()
