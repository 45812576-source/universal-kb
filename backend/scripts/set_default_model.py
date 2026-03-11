"""将 model_configs 默认模型切换为 kimi-k2.5"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models.skill import ModelConfig


def main():
    db = SessionLocal()
    try:
        # 先清除所有 is_default
        db.query(ModelConfig).update({"is_default": False})

        # 找已有的 kimi/moonshot 配置
        mc = db.query(ModelConfig).filter(ModelConfig.provider == "moonshot").first()
        if mc:
            mc.name = "Kimi-K2"
            mc.model_id = "kimi-k2.5"
            mc.api_base = "https://api.moonshot.cn/v1"
            mc.api_key_env = "KIMI_API_KEY"
            mc.max_tokens = 4096
            mc.temperature = "0.7"
            mc.is_default = True
            print(f"Updated existing Moonshot config (id={mc.id}) and set as default.")
        else:
            mc = ModelConfig(
                name="Kimi-K2",
                provider="moonshot",
                model_id="kimi-k2.5",
                api_base="https://api.moonshot.cn/v1",
                api_key_env="KIMI_API_KEY",
                max_tokens=4096,
                temperature="0.7",
                is_default=True,
            )
            db.add(mc)
            print("Inserted new Kimi-K2 config and set as default.")

        db.commit()
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
