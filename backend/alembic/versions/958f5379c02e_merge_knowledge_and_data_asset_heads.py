"""merge knowledge and data asset heads

Revision ID: 958f5379c02e
Revises: a1b2_cloud_doc, a1b2c3d4e5f9, a1b2c3d4e5f6, a1b2c3d4e5f7, d5e6f7a8b9c0, j1k2l3m4n5o6
Create Date: 2026-04-03
"""
from typing import Sequence, Union


revision: str = "958f5379c02e"
down_revision: Union[str, Sequence[str], None] = (
    "a1b2_cloud_doc",
    "a1b2c3d4e5f9",
    "a1b2c3d4e5f6",
    "a1b2c3d4e5f7",
    "d5e6f7a8b9c0",
    "j1k2l3m4n5o6",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
