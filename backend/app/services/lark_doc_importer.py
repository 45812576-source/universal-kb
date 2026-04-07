"""飞书文档导入 & 定时同步服务。

三策略分发架构：
- 策略A（导出）：doc, docx, sheet, bitable → export_tasks API → 下载 → 提取文本 → 入库
- 策略B（直接下载）：file → drive/v1/files/:token/download → 入库
- 策略C（链接引用）：mindnote, slides, 及所有未知类型 → 只保存 URL 和元数据
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

# ── 飞书链接正则 ─────────────────────────────────────────────────────────
# 覆盖所有已知飞书路径类型，含 wiki 套壳
_LARK_URL_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/(?:wiki/)?(?P<type>docx|doc|sheets|sheet|file|base|bitable|slides|mindnote|board|minutes|survey|drive)"
    r"/(?P<token>[A-Za-z0-9_-]+)"
)

# 单独匹配纯 wiki 链接（/wiki/TOKEN，不含二级类型路径）
_LARK_WIKI_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/wiki/(?P<token>[A-Za-z0-9_-]+)$"
)

# ── URL 路径名 → API 类型名 ──────────────────────────────────────────────
_URL_PATH_TO_API_TYPE = {
    "docx": "docx",
    "doc": "doc",
    "sheets": "sheet",
    "sheet": "sheet",
    "base": "bitable",
    "bitable": "bitable",
    "file": "file",
    "slides": "slides",
    "mindnote": "mindnote",
    "drive": "file",
    "board": "board",
    "minutes": "minutes",
    "survey": "survey",
}

# ── 策略分类 ─────────────────────────────────────────────────────────────
_EXPORTABLE_TYPES = {"doc", "docx", "sheet", "bitable"}
_DIRECT_DOWNLOAD_TYPES = {"file"}
# 其余全部走策略C（链接引用）

# ── 文档类型 → 导出 file_extension 映射（仅策略A 使用）────────────────────
_EXPORT_EXT_MAP = {
    "doc": "docx",
    "docx": "docx",
    "sheet": "xlsx",
    "bitable": "xlsx",
}

# ── 渲染模式映射 ─────────────────────────────────────────────────────────
_LARK_RENDER_MODE_MAP = {
    "docx": "lark_doc_import",
    "doc": "lark_doc_import",
    "sheet": "lark_sheet_import",
    "bitable": "lark_sheet_import",
    "file": "lark_file_import",
}
_LINK_REF_RENDER_MODE = "lark_link_reference"

# ── 不可导出类型的中文名 ────────────────────────────────────────────────
_TYPE_DISPLAY_NAME = {
    "mindnote": "思维笔记",
    "slides": "演示文稿",
    "board": "画板",
    "minutes": "妙记（会议纪要）",
    "survey": "问卷",
}


def _normalize_type(url_type: str) -> str:
    """URL 路径名 → API 类型名。"""
    return _URL_PATH_TO_API_TYPE.get(url_type, url_type)


class LarkDocImporter:

    def parse_lark_url(self, url: str) -> tuple[str, str]:
        """解析飞书链接 → (token, api_type)。

        Returns:
            (token, api_type) 如 ("AbcDef123", "docx")
        Raises:
            ValueError: 链接格式不支持
        """
        m = _LARK_URL_RE.search(url)
        if m:
            token = m.group("token")
            url_type = m.group("type")
            return token, _normalize_type(url_type)

        # 纯 wiki 链接
        m = _LARK_WIKI_RE.search(url)
        if m:
            return m.group("token"), "wiki"

        raise ValueError(
            f"无法解析飞书链接: {url}\n"
            "支持的格式：飞书文档/表格/知识库/云空间文件等链接，"
            "如 https://xxx.feishu.cn/docx/TOKEN 或 https://xxx.feishu.cn/wiki/TOKEN"
        )

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
        """完整导入一个飞书文档到知识库。根据类型自动分发到三种策略。"""
        from app.services.lark_client import lark_client

        # 1. 解析链接
        token, api_type = self.parse_lark_url(url)

        # 2. wiki → 解析为实际文档 token
        wiki_title = None
        if api_type == "wiki":
            node = await lark_client.get_wiki_node(token)
            token = node["obj_token"]
            api_type = node["obj_type"]
            wiki_title = node.get("title", "")

        # 3. 根据 api_type 分发到三种策略
        if api_type in _EXPORTABLE_TYPES:
            return await self._strategy_export(
                db, user, url, token, api_type, title, wiki_title, folder_id, category
            )
        elif api_type in _DIRECT_DOWNLOAD_TYPES:
            return await self._strategy_download(
                db, user, url, token, api_type, title, wiki_title, folder_id, category
            )
        else:
            return await self._strategy_link_reference(
                db, user, url, token, api_type, title, wiki_title, folder_id, category
            )

    # ── 策略A：可导出类型 ────────────────────────────────────────────────

    async def _strategy_export(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category
    ) -> KnowledgeEntry:
        """doc/docx/sheet/bitable → export_tasks → 下载 → 提取文本 → 入库。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.services.knowledge_namer import auto_name
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        from app.services.knowledge_service import submit_knowledge
        from app.services.review_policy import review_policy
        from app.utils.file_parser import extract_text, extract_html

        file_ext_str = _EXPORT_EXT_MAP[api_type]

        # 创建导出任务 → 轮询 → 下载
        ticket = await lark_client.create_export_task(token, api_type, file_ext_str)
        file_bytes = await lark_client.poll_and_download_export(ticket)

        # 写临时文件 → 提取文本
        ext = f".{file_ext_str}"
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, f"lark_import{ext}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)
            content = extract_text(tmp_path)

            # 生成 content_html
            content_html = None
            doc_render_mode = _LARK_RENDER_MODE_MAP.get(api_type, "lark_doc_import")
            try:
                content_html = extract_html(tmp_path)
            except Exception as e:
                logger.warning(f"extract_html failed for lark doc: {e}")
            if not content_html and content:
                content_html = "\n".join(f"<p>{line or '<br>'}</p>" for line in content.split("\n"))

            # 上传到 OSS
            oss_key = generate_oss_key(ext)
            oss_upload(tmp_path, oss_key)
        finally:
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass

        file_size = len(file_bytes)
        import mimetypes
        file_type = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"

        # 敏感词检测
        sensitive_flags = review_policy.detect_sensitive(content)
        strategic_flags = review_policy.detect_strategic(content)
        capture_mode = "upload" if (sensitive_flags or strategic_flags) else "upload_ai_clean"

        # 创建 KnowledgeEntry
        entry = self._build_entry(
            db, user, url, token, api_type, title, wiki_title, folder_id, category,
            content=content,
            content_html=content_html,
            capture_mode=capture_mode,
            oss_key=oss_key,
            file_type=file_type,
            file_ext=ext,
            file_size=file_size,
            doc_render_mode=doc_render_mode,
        )
        db.add(entry)
        db.flush()

        # AI 命名/摘要
        await self._ai_enrich(entry, content, url, file_type, title, db)

        # AI 分类
        try:
            cls_result = await classify(content, db)
            if cls_result:
                apply_classification_to_entry(entry, cls_result)
        except Exception as e:
            logger.warning(f"Auto-classification failed for lark doc: {e}")

        # 审核流程
        entry = submit_knowledge(db, entry)
        return entry

    # ── 策略B：文件直接下载 ──────────────────────────────────────────────

    async def _strategy_download(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category
    ) -> KnowledgeEntry:
        """file 类型 → drive/v1/files/:token/download → 入库。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.services.knowledge_namer import auto_name
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        from app.services.knowledge_service import submit_knowledge
        from app.services.review_policy import review_policy
        from app.utils.file_parser import extract_text, extract_html

        file_bytes, filename = await lark_client.download_file(token)

        # 推断扩展名
        ext = os.path.splitext(filename)[1] if filename else ""
        if not ext:
            ext = ".bin"

        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, f"lark_download{ext}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)

            # 尝试提取文本（可能失败，如二进制文件）
            content = ""
            try:
                content = extract_text(tmp_path)
            except Exception as e:
                logger.info(f"无法从飞书文件提取文本（可能为二进制文件）: {e}")

            content_html = None
            if content:
                try:
                    content_html = extract_html(tmp_path)
                except Exception:
                    pass
                if not content_html:
                    content_html = "\n".join(f"<p>{line or '<br>'}</p>" for line in content.split("\n"))
            if not content_html:
                content_html = (
                    "<p>该飞书文件已导入为工作台副本，但暂未解析出结构化正文。</p>"
                    "<p>你仍可在此补充编辑，或下载原文件查看。</p>"
                )

            oss_key = generate_oss_key(ext)
            oss_upload(tmp_path, oss_key)
        finally:
            try:
                os.unlink(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass

        file_size = len(file_bytes)
        import mimetypes
        file_type = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"

        sensitive_flags = review_policy.detect_sensitive(content) if content else []
        strategic_flags = review_policy.detect_strategic(content) if content else []
        capture_mode = "upload" if (sensitive_flags or strategic_flags) else "upload_ai_clean"

        entry = self._build_entry(
            db, user, url, token, api_type, title, wiki_title, folder_id, category,
            content=content,
            content_html=content_html,
            capture_mode=capture_mode,
            oss_key=oss_key,
            file_type=file_type,
            file_ext=ext,
            file_size=file_size,
            doc_render_mode="lark_file_import",
        )
        db.add(entry)
        db.flush()

        if content:
            await self._ai_enrich(entry, content, url, file_type, title, db)
            try:
                cls_result = await classify(content, db)
                if cls_result:
                    apply_classification_to_entry(entry, cls_result)
            except Exception as e:
                logger.warning(f"Auto-classification failed for lark file: {e}")

        entry = submit_knowledge(db, entry)
        return entry

    # ── 策略C：不可导出类型 → 链接引用 ──────────────────────────────────

    async def _strategy_link_reference(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category
    ) -> KnowledgeEntry:
        """mindnote/slides/board/minutes 等 → 只保存 URL 和元数据。"""
        from app.services.knowledge_service import submit_knowledge

        type_name = _TYPE_DISPLAY_NAME.get(api_type, api_type)
        content = (
            f"此条目为飞书{type_name}的链接引用，该类型暂不支持内容导出。\n"
            f"请点击原始链接查看完整内容：{url}"
        )
        content_html = (
            f"<p>此条目为飞书<strong>{type_name}</strong>的链接引用，该类型暂不支持内容导出。</p>"
            f'<p>请点击原始链接查看完整内容：<a href="{url}" target="_blank">{url}</a></p>'
        )

        entry = self._build_entry(
            db, user, url, token, api_type, title, wiki_title, folder_id, category,
            content=content,
            content_html=content_html,
            capture_mode="upload",
            oss_key=None,
            file_type=None,
            file_ext=None,
            file_size=0,
            doc_render_mode=_LINK_REF_RENDER_MODE,
        )
        db.add(entry)
        db.flush()

        entry = submit_knowledge(db, entry)
        return entry

    # ── 公共工具方法 ─────────────────────────────────────────────────────

    def _build_entry(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category,
        *, content, content_html, capture_mode, oss_key, file_type, file_ext, file_size,
        doc_render_mode,
    ) -> KnowledgeEntry:
        """构建 KnowledgeEntry 对象。"""
        import datetime

        return KnowledgeEntry(
            title=title or wiki_title or f"飞书文档导入 {token[:8]}",
            content=content,
            content_html=content_html,
            category=category,
            created_by=user.id,
            department_id=user.department_id,
            folder_id=folder_id,
            source_type="lark_doc",
            source_file=url,
            capture_mode=capture_mode,
            oss_key=oss_key,
            file_type=file_type,
            file_ext=file_ext,
            file_size=file_size,
            lark_doc_token=token,
            lark_doc_type=api_type,
            lark_doc_url=url,
            lark_sync_interval=0,
            lark_last_synced_at=int(time.time()),
            external_edit_mode="detached_copy",
            doc_render_status="ready",
            doc_render_mode=doc_render_mode,
            last_rendered_at=datetime.datetime.utcnow(),
            source_uri=url,
            sync_status="ok",
        )

    async def _ai_enrich(self, entry, content, url, file_type, title, db):
        """AI 命名/摘要。"""
        from app.services.knowledge_namer import auto_name
        try:
            naming_result = await auto_name(content, url, file_type, db=db)
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

    async def sync_doc(self, db: Session, entry: KnowledgeEntry) -> dict:
        """增量同步：导出飞书文档最新版，对比内容，有变化则更新。

        仅对策略A（可导出类型）有效；策略B/C 类型跳过同步。
        """
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.utils.file_parser import extract_text, extract_html

        token = entry.lark_doc_token
        doc_type = entry.lark_doc_type or "docx"

        if not token:
            return {"updated": False, "error": "missing lark_doc_token"}

        # 策略C 类型不支持同步
        if doc_type not in _EXPORTABLE_TYPES:
            return {"updated": False, "error": f"类型 {doc_type} 不支持内容同步，仅保存链接引用"}

        file_ext_str = _EXPORT_EXT_MAP.get(doc_type, "docx")

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
        content_changed = False
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)
            new_content = extract_text(tmp_path)

            content_changed = new_content.strip() != (entry.content or "").strip()

            if content_changed:
                # 更新 OSS 文件
                oss_key = generate_oss_key(ext)
                oss_upload(tmp_path, oss_key)

                # 删除旧 OSS
                if entry.oss_key:
                    try:
                        from app.services.oss_service import delete_file
                        delete_file(entry.oss_key)
                    except Exception:
                        pass

                entry.content = new_content
                entry.oss_key = oss_key
                entry.file_size = len(file_bytes)

                # 更新 content_html
                try:
                    new_html = extract_html(tmp_path)
                    if not new_html and new_content:
                        new_html = "\n".join(f"<p>{line or '<br>'}</p>" for line in new_content.split("\n"))
                    if new_html:
                        entry.content_html = new_html
                        entry.doc_render_status = "ready"
                        entry.doc_render_mode = _LARK_RENDER_MODE_MAP.get(doc_type, "lark_doc_import")
                except Exception as e:
                    logger.warning(f"extract_html failed during sync for entry {entry.id}: {e}")

                # 重新向量化
                try:
                    from app.services import vector_service
                    if entry.milvus_ids:
                        try:
                            col = vector_service.get_collection()
                            col.delete(f"knowledge_id == {entry.id}")
                        except Exception:
                            pass
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
            "doc_render_status": entry.doc_render_status,
            "doc_render_mode": entry.doc_render_mode,
        }


lark_doc_importer = LarkDocImporter()
