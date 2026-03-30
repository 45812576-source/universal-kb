"""data assets phase 1: folders, table fields, sync jobs, skill bindings

Revision ID: j1k2l3m4n5o6
Revises: m0d3l_a5s1gn_001
Create Date: 2026-03-30

Changes:
1. CREATE TABLE data_folders — 持久化目录树
2. CREATE TABLE table_fields — 字段元信息
3. CREATE TABLE table_sync_jobs — 同步任务记录
4. CREATE TABLE skill_table_bindings — Skill 到视图绑定
5. ALTER TABLE business_tables — 新增 folder_id, source_type, source_ref 等 11 个字段
6. ALTER TABLE table_views — 新增 view_purpose, visibility_scope 等 5 个字段
7. 创建默认目录
8. 数据迁移：从 validation_rules 提取飞书来源/folder_id/last_synced_at
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import JSON

revision = 'j1k2l3m4n5o6'
down_revision = 'm0d3l_a5s1gn_001'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. data_folders ──────────────────────────────────────────────────────
    op.create_table(
        'data_folders',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('data_folders.id'), nullable=True),
        sa.Column('workspace_scope', sa.String(20), server_default='company'),
        sa.Column('department_id', sa.Integer(), sa.ForeignKey('departments.id'), nullable=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.Column('is_archived', sa.Boolean(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')),
        sa.UniqueConstraint('parent_id', 'name', 'workspace_scope', 'owner_id', name='uq_folder_name_scope'),
    )

    # ── 2. table_fields ──────────────────────────────────────────────────────
    op.create_table(
        'table_fields',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('table_id', sa.Integer(), sa.ForeignKey('business_tables.id', ondelete='CASCADE'), nullable=False),
        sa.Column('field_name', sa.String(200), nullable=False),
        sa.Column('display_name', sa.String(200), nullable=True),
        sa.Column('physical_column_name', sa.String(200), nullable=True),
        sa.Column('field_type', sa.String(30), server_default='text'),
        sa.Column('source_field_type', sa.String(50), nullable=True),
        sa.Column('is_nullable', sa.Boolean(), server_default='1'),
        sa.Column('is_system', sa.Boolean(), server_default='0'),
        sa.Column('is_hidden_by_default', sa.Boolean(), server_default='0'),
        sa.Column('is_filterable', sa.Boolean(), server_default='1'),
        sa.Column('is_groupable', sa.Boolean(), server_default='0'),
        sa.Column('is_sortable', sa.Boolean(), server_default='1'),
        sa.Column('enum_values', JSON, nullable=True),
        sa.Column('enum_source', sa.String(20), nullable=True),
        sa.Column('sample_values', JSON, nullable=True),
        sa.Column('distinct_count_cache', sa.Integer(), nullable=True),
        sa.Column('null_ratio', sa.Float(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_table_fields_table_id', 'table_fields', ['table_id'])

    # ── 3. table_sync_jobs ───────────────────────────────────────────────────
    op.create_table(
        'table_sync_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('table_id', sa.Integer(), sa.ForeignKey('business_tables.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_type', sa.String(20), nullable=True),
        sa.Column('job_type', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), server_default='queued'),
        sa.Column('error_type', sa.String(30), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('triggered_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('trigger_source', sa.String(20), server_default='manual'),
        sa.Column('result_summary', JSON, nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('stats', JSON, nullable=True),
    )
    op.create_index('ix_sync_jobs_table_id', 'table_sync_jobs', ['table_id'])

    # ── 4. skill_table_bindings ──────────────────────────────────────────────
    op.create_table(
        'skill_table_bindings',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('skill_id', sa.Integer(), sa.ForeignKey('skills.id', ondelete='CASCADE'), nullable=False),
        sa.Column('table_id', sa.Integer(), sa.ForeignKey('business_tables.id', ondelete='CASCADE'), nullable=False),
        sa.Column('view_id', sa.Integer(), sa.ForeignKey('table_views.id', ondelete='SET NULL'), nullable=True),
        sa.Column('binding_type', sa.String(20), server_default='runtime_read'),
        sa.Column('alias', sa.String(100), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_skill_bindings_skill', 'skill_table_bindings', ['skill_id'])
    op.create_index('ix_skill_bindings_table', 'skill_table_bindings', ['table_id'])

    # ── 5. business_tables 扩展字段 ──────────────────────────────────────────
    op.add_column('business_tables', sa.Column('folder_id', sa.Integer(), sa.ForeignKey('data_folders.id'), nullable=True))
    op.add_column('business_tables', sa.Column('source_type', sa.String(20), nullable=True, server_default='blank'))
    op.add_column('business_tables', sa.Column('source_ref', JSON, nullable=True))
    op.add_column('business_tables', sa.Column('sync_status', sa.String(20), nullable=True, server_default='idle'))
    op.add_column('business_tables', sa.Column('sync_error', sa.Text(), nullable=True))
    op.add_column('business_tables', sa.Column('last_synced_at', sa.DateTime(), nullable=True))
    op.add_column('business_tables', sa.Column('last_sync_job_id', sa.Integer(), nullable=True))
    op.add_column('business_tables', sa.Column('field_profile_status', sa.String(20), nullable=True, server_default='pending'))
    op.add_column('business_tables', sa.Column('field_profile_error', sa.Text(), nullable=True))
    op.add_column('business_tables', sa.Column('record_count_cache', sa.Integer(), nullable=True))
    op.add_column('business_tables', sa.Column('is_archived', sa.Boolean(), nullable=True, server_default='0'))

    # ── 6. table_views 扩展字段 ──────────────────────────────────────────────
    op.add_column('table_views', sa.Column('view_purpose', sa.String(30), nullable=True))
    op.add_column('table_views', sa.Column('visibility_scope', sa.String(20), nullable=True, server_default='table_inherit'))
    op.add_column('table_views', sa.Column('is_default', sa.Boolean(), nullable=True, server_default='0'))
    op.add_column('table_views', sa.Column('is_system', sa.Boolean(), nullable=True, server_default='0'))
    op.add_column('table_views', sa.Column('last_used_at', sa.DateTime(), nullable=True))

    # ── 7. 创建默认目录 ─────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO data_folders (name, parent_id, workspace_scope, sort_order)
        VALUES
            ('全部数据', NULL, 'company', 0),
            ('飞书同步', NULL, 'company', 10),
            ('我的数据', NULL, 'company', 20)
    """)

    # ── 8. 数据迁移：从 validation_rules 提取飞书来源信息 ──────────────────
    # 标记有 bitable_app_token 的表为 lark_bitable 来源
    op.execute("""
        UPDATE business_tables
        SET source_type = 'lark_bitable',
            source_ref = JSON_OBJECT(
                'app_token', JSON_UNQUOTE(JSON_EXTRACT(validation_rules, '$.bitable_app_token')),
                'table_id', JSON_UNQUOTE(JSON_EXTRACT(validation_rules, '$.bitable_table_id'))
            ),
            sync_status = 'success'
        WHERE JSON_EXTRACT(validation_rules, '$.bitable_app_token') IS NOT NULL
          AND JSON_UNQUOTE(JSON_EXTRACT(validation_rules, '$.bitable_app_token')) != ''
    """)

    # 从 validation_rules.last_synced_at (unix timestamp) 迁移到 last_synced_at (datetime)
    op.execute("""
        UPDATE business_tables
        SET last_synced_at = FROM_UNIXTIME(JSON_EXTRACT(validation_rules, '$.last_synced_at'))
        WHERE JSON_EXTRACT(validation_rules, '$.last_synced_at') IS NOT NULL
          AND JSON_EXTRACT(validation_rules, '$.last_synced_at') > 0
    """)

    # 从 validation_rules.folder_id 迁移到 folder_id 列
    op.execute("""
        UPDATE business_tables
        SET folder_id = JSON_EXTRACT(validation_rules, '$.folder_id')
        WHERE JSON_EXTRACT(validation_rules, '$.folder_id') IS NOT NULL
          AND JSON_EXTRACT(validation_rules, '$.folder_id') > 0
    """)

    # 标记用户创建的空白表
    op.execute("""
        UPDATE business_tables
        SET source_type = 'blank'
        WHERE source_type IS NULL OR source_type = 'blank'
    """)

    # 飞书表移入"飞书同步"目录
    op.execute("""
        UPDATE business_tables bt
        SET bt.folder_id = (SELECT id FROM data_folders WHERE name = '飞书同步' LIMIT 1)
        WHERE bt.source_type = 'lark_bitable' AND (bt.folder_id IS NULL OR bt.folder_id = 0)
    """)


def downgrade():
    # ── table_views 扩展字段 ──
    op.drop_column('table_views', 'last_used_at')
    op.drop_column('table_views', 'is_system')
    op.drop_column('table_views', 'is_default')
    op.drop_column('table_views', 'visibility_scope')
    op.drop_column('table_views', 'view_purpose')

    # ── business_tables 扩展字段 ──
    op.drop_column('business_tables', 'is_archived')
    op.drop_column('business_tables', 'record_count_cache')
    op.drop_column('business_tables', 'field_profile_error')
    op.drop_column('business_tables', 'field_profile_status')
    op.drop_column('business_tables', 'last_sync_job_id')
    op.drop_column('business_tables', 'last_synced_at')
    op.drop_column('business_tables', 'sync_error')
    op.drop_column('business_tables', 'sync_status')
    op.drop_column('business_tables', 'source_ref')
    op.drop_column('business_tables', 'source_type')
    op.drop_column('business_tables', 'folder_id')

    # ── 新表 ──
    op.drop_table('skill_table_bindings')
    op.drop_table('table_sync_jobs')
    op.drop_table('table_fields')
    op.drop_table('data_folders')
