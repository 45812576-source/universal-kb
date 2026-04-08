import datetime
import enum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Role(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    DEPT_ADMIN = "dept_admin"
    EMPLOYEE = "employee"


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    category = Column(String(50))  # 后台/前台/中台
    business_unit = Column(String(100))  # 事业部/中心
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    parent = relationship("Department", remote_side=[id], back_populates="children")
    children = relationship("Department", back_populates="parent")
    users = relationship("User", back_populates="department", foreign_keys="User.department_id")

    def get_all_descendant_ids(self, db) -> set[int]:
        """递归获取当前部门及所有子部门的ID集合"""
        result = {self.id}
        children = db.query(Department).filter(Department.parent_id == self.id).all()
        for child in children:
            result |= child.get_all_descendant_ids(db)
        return result


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(100), nullable=False)
    role = Column(Enum(Role), default=Role.EMPLOYEE)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    managed_department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    lark_user_id = Column(String(100), nullable=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    report_to_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    avatar_url = Column(String(500), nullable=True)
    feature_flags = Column(JSON, default=dict, server_default="{}")
    lark_access_token = Column(Text, nullable=True)
    lark_refresh_token = Column(Text, nullable=True)
    lark_token_expires_at = Column(DateTime, nullable=True)

    department = relationship("Department", back_populates="users", foreign_keys=[department_id])
    managed_department = relationship("Department", foreign_keys=[managed_department_id])
    position = relationship("Position", back_populates="users", foreign_keys=[position_id])
    report_to = relationship("User", remote_side=[id], foreign_keys=[report_to_id])

    def get_managed_department_ids(self, db) -> set[int]:
        """获取此用户管辖的所有部门ID（含子部门递归）。
        super_admin 返回空集（由调用方特判），无管辖部门返回空集。
        """
        if not self.managed_department_id:
            return set()
        managed = db.query(Department).get(self.managed_department_id)
        if not managed:
            return set()
        return managed.get_all_descendant_ids(db)


# ── 系统用户 ──────────────────────────────────────────────────────────────────

SYSTEM_USERNAME = "_system"


def get_system_user_id(db) -> int:
    """返回系统用户 ID，不存在则自动创建。"""
    sys_user = db.query(User).filter(User.username == SYSTEM_USERNAME).first()
    if sys_user:
        return sys_user.id
    sys_user = User(
        username=SYSTEM_USERNAME,
        password_hash="!locked",
        display_name="系统",
        role=Role.EMPLOYEE,
        is_active=False,
    )
    db.add(sys_user)
    db.flush()
    return sys_user.id
