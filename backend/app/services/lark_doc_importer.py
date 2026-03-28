"""飞书文档导入 & 定时同步服务。

支持将飞书 docx / wiki / sheet / file 链接一键导入为知识库云文档，
并可配置定时同步以跟踪飞书端的文档更新。
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry

logger = logging.getLogger(__name__)

# 飞书链接正则：匹配 /docx/xxx, /wiki/xxx, /sheets/xxx, /file/xxx 等
_LARK_URL_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/(?P<type>docx|wiki|sheets|file|base|doc)"
    r"/(?P<token>[A-Za-z0-9_-]+)"
)

# 文档类型 → 导出 file_extension 映射
_EXPORT_EXT_MAP = {
    "docx": "docx",
    "doc": "docx",
    "sheet": "xlsx",
    "sheets": "xlsx",
    "file": None,   # 云空间文件直接下载，不走导出
}


class LarkDocImporter:

    def parse_lark_url(self, url: str) -> tuple[str, str]:
        """解析飞书链接 → (token, type)。

        Returns:
            (token, doc_type) 如 ("AbcDef123", "docx")
        Raises:
            ValueError: 链接格式不支持
        """
        m = _LARK_URL_RE.search(url)
        if not m:
            raise ValueError(f"无法解析飞书链接: {url}")
        return m.group("token"), m.group("type")

    async def import_doc(
        self,
        db: Session,
        user,
        url: str,
        title: Optional[str] = None,
        folder_id: Optional[int] = None,
        category: str = "experience",
        sync_interval: int = 0,
    ) -> KnowledgeEntry:
        """完整导入一个飞书文档到知识库。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.services.knowledge_namer import auto_name
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        from app.services.knowledge_service import submit_knowledge
        from app.services.review_policy import review_policy
        from app.utils.file_parser import extract_text

        # 1. 解析链接
        token, doc_type = self.parse_lark_url(url)

        # 2. wiki → 解析为实际文档 token
        wiki_title = None
        if doc_type == "wiki":
            node = await lark_client.get_wiki_node(token)
            token = node["obj_token"]
            doc_type = node["obj_type"]  # 通常是 "docx"
            wiki_title = node.get("title", "")

        # 3. 确定导出格式
        file_ext_str = _EXPORT_EXT_MAP.get(doc_type, "docx")
        if file_ext_str is None:
            file_ext_str = "docx"  # fallback

        # 4. 创建导出任务 → 轮询 → 下载
        ticket = await lark_client.create_export_task(token, doc_type, file_ext_str)
        file_bytes = await lark_client.poll_and_download_export(ticket)

        # 5. 写临时文件 → 提取文本
        ext = f".{file_ext_str}"
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, f"lark_import{ext}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)
            content = extract_text(tmp_path)

            # 6. 上传到 OSS
            oss_key = generate_oss_key(ext)
            oss_upload(tmp_path, oss_key)
        finally:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass

        file_size = len(file_bytes)
        import mimetypes
        file_type = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"

        # 7. 敏感词检测
        sensitive_flags = review_policy.detect_sensitive(content)
        strategic_flags = review_policy.detect_strategic(content)
        capture_mode = "upload" if (sensitive_flags or strategic_flags) else "upload_ai_clean"

        # 8. 创建 KnowledgeEntry
        entry = KnowledgeEntry(
            title=title or wiki_title or f"飞书文档导入 {token[:8]}",
            content=content,
            category=category,
            created_by=user.id,
            department_id=user.department_id,
            folder_id=folder_id,
            source_type="lark_doc",
            source_file=url,
            capture_mode=capture_mode,
            oss_key=oss_key,
            file_type=file_type,
            file_ext=ext,
            file_size=file_size,
            # 飞书同步字段
            lark_doc_token=token,
            lark_doc_type=doc_type,
            lark_doc_url=url,
            lark_sync_interval=sync_interval,
            lark_last_synced_at=int(time.time()),
        )
        db.add(entry)
        db.flush()

        # 9. AI 命名/摘要
        try:
            naming_result = await auto_name(content, url, file_type)
            entry.ai_title = naming_result["title"]
            entry.ai_summary = naming_result["summary"]
            entry.ai_tags = naming_result["tags"]
            entry.quality_score = naming_result["quality_score"]
            if not title:
                entry.title = naming_result["title"]
            if naming_result["tags"].get("industry"):
                entry.industry_tags = naming_result["tags"]["industry"]
            if naming_result["tags"].get("platform"):
                entry.platform_tags = naming_result["tags"]["platform"]
            if naming_result["tags"].get("topic"):
                entry.topic_tags = naming_result["tags"]["topic"]
        except Exception as e:
            logger.warning(f"AI naming failed for lark doc: {e}")

        # 10. AI 分类
        try:
            cls_result = await classify(content, db)
            if cls_result:
                apply_classification_to_entry(entry, cls_result)
        except Exception as e:
            logger.warning(f"Auto-classification failed for lark doc: {e}")

        # 11. 审核流程
        entry = submit_knowledge(db, entry)

        return entry

    async def sync_doc(self, db: Session, entry: KnowledgeEntry) -> dict:
        """增量同步：导出飞书文档最新版，对比内容，有变化则更新。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.utils.file_parser import extract_text
        from sqlalchemy.orm.attributes import flag_modified

        token = entry.lark_doc_token
        doc_type = entry.lark_doc_type or "docx"

        if not token:
            return {"updated": False, "error": "missing lark_doc_token"}

        # wiki 类型需重新解析（token 可能是 obj_token 已经解析过了）
        file_ext_str = _EXPORT_EXT_MAP.get(doc_type, "docx") or "docx"

        try:
            ticket = await lark_client.create_export_task(token, doc_type, file_ext_str)
            file_bytes = await lark_client.poll_and_download_export(ticket)
        except Exception as e:
            logger.warning(f"Lark doc export failed for entry {entry.id}: {e}")
            return {"updated": False, "error": str(e)}

        # 提取文本
        ext = f".{file_ext_str}"
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, f"lark_sync{ext}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)
            new_content = extract_text(tmp_path)

            content_changed = new_content.strip() != (entry.content or "").strip()

            if content_changed:
                # 更新 OSS 文件
                oss_key = generate_oss_key(ext)
                oss_upload(tmp_path, oss_key)

                # 删除旧 OSS（如果有）
                if entry.oss_key:
                    try:
                        from app.services.oss_service import delete_file
                        delete_file(entry.oss_key)
                    except Exception:
                        pass

                entry.content = new_content
                entry.oss_key = oss_key
                entry.file_size = len(file_bytes)

                # 重新向量化
                try:
                    from app.services import vector_service
                    # 删除旧 chunks
                    if entry.milvus_ids:
                        try:
                            col = vector_service.get_collection()
                            col.delete(f"knowledge_id == {entry.id}")
                        except Exception:
                            pass
                    # 重新入库（复用 submit_knowledge 后的自动入库逻辑）
                    vector_service.index_knowledge(entry)
                except Exception as e:
                    logger.warning(f"Re-vectorize failed for entry {entry.id}: {e}")
        finally:
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass

        entry.lark_last_synced_at = int(time.time())
        db.commit()

        return {
            "updated": True,
            "content_changed": content_changed,
            "entry_id": entry.id,
        }


lark_doc_importer = LarkDocImporter()
