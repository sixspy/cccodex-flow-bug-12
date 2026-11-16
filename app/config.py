import os
from pathlib import Path

class Config:
    SECRET = "cc-flow-lab-dev"
    LAB_NAME = "编排流抽取台"
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    @staticmethod
    def database() -> str:
        p = os.environ.get("DATABASE_PATH") or str(Path(__file__).resolve().parent.parent / "instance" / "flow_lab.db")
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        return p
