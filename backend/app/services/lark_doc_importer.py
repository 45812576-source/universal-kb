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
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry

logger = logging.getLogger(__name__)

# ── 飞书链接正则 ─────────────────────────────────────────────────────────
# 覆盖所有已知飞书路径类型，含 wiki 套壳和 share 前缀
_LARK_URL_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/(?:wiki/|share/)?"
    r"(?P<type>docx|doc|sheets|sheet|file|base|bitable|slides|mindnotes|mindnote|board|minutes|survey)"
    r"/(?!folder/)(?P<token>[A-Za-z0-9_-]+)"
    # ↑ 负向前瞻排除 /drive/folder/xxx（文件夹不是文件）
    # ↑ 去掉 drive（/drive/xxx 是文件夹或容器，不是具体文档）
)

# 飞书问卷分享链接：/share/base/form/TOKEN
_LARK_SHARE_FORM_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/share/base/form/(?P<token>[A-Za-z0-9_-]+)"
)

# 单独匹配纯 wiki 链接（/wiki/TOKEN，不含二级类型路径）
_LARK_WIKI_RE = re.compile(
    r"https?://[^/]*(?:feishu\.cn|larksuite\.com)"
    r"/wiki/(?P<token>[A-Za-z0-9_-]+)(?:\?|$)"
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
    "mindnotes": "mindnote",
    "mindnote": "mindnote",
    "board": "board",
    "minutes": "minutes",
    "survey": "survey",
    "form": "survey",  # 问卷分享链接
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


def _allow_user_token_fallback() -> bool:
    """是否允许从组织应用 token 回退到用户 OAuth token。默认关闭。"""
    from app.config import settings

    mode = (getattr(settings, "LARK_IMPORT_AUTH_MODE", "app_only") or "app_only").lower()
    oauth_enabled = bool(getattr(settings, "LARK_OAUTH_ENABLED", False))
    return oauth_enabled and mode in {"user_fallback", "user", "oauth_fallback"}


async def get_valid_user_token(db: Session, user) -> str | None:
    """获取用户的有效 lark user_access_token，过期则自动刷新。

    Returns: 有效的 access_token 或 None（未授权/刷新失败）。
    """
    if not user.lark_access_token:
        return None

    import datetime
    from app.services.lark_client import lark_client

    # 未过期，直接返回
    if user.lark_token_expires_at and datetime.datetime.utcnow() < user.lark_token_expires_at:
        return user.lark_access_token

    # 过期了，尝试 refresh
    if not user.lark_refresh_token:
        return None

    try:
        token_data = await lark_client.refresh_user_token(user.lark_refresh_token)
        user.lark_access_token = token_data.get("access_token", "")
        user.lark_refresh_token = token_data.get("refresh_token", user.lark_refresh_token)
        expires_in = token_data.get("expires_in", 7200)
        user.lark_token_expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
        db.commit()
        logger.info(f"用户 {user.id} 飞书 OAuth token 自动刷新成功")
        return user.lark_access_token
    except Exception as e:
        logger.warning(f"用户 {user.id} 飞书 OAuth token 刷新失败: {e}")
        return None


class LarkDocImporter:

    def parse_lark_url(self, url: str) -> tuple[str, str, dict]:
        """解析飞书链接 → (token, api_type, extra_params)。

        Returns:
            (token, api_type, extra_params) 如 ("AbcDef123", "docx", {})
            bitable 类型时 extra_params 含 {"table_id": "tblXXX"}（从 URL ?table= 解析）
        Raises:
            ValueError: 链接格式不支持
        """
        # 问卷分享链接（优先匹配，避免被主正则的 /base/ 吃掉）
        m = _LARK_SHARE_FORM_RE.search(url)
        if m:
            return m.group("token"), "survey", {}

        m = _LARK_URL_RE.search(url)
        if m:
            token = m.group("token")
            url_type = m.group("type")
            api_type = _normalize_type(url_type)
            extra: dict = {}
            if api_type == "bitable":
                tm = re.search(r"[?&]table=([A-Za-z0-9]+)", url)
                if tm:
                    extra["table_id"] = tm.group(1)
            return token, api_type, extra

        # 纯 wiki 链接
        m = _LARK_WIKI_RE.search(url)
        if m:
            return m.group("token"), "wiki", {}

        # 飞书文件夹链接 — 不支持导入，给明确提示
        if re.search(r"feishu\.cn/drive/folder/", url) or re.search(r"larksuite\.com/drive/folder/", url):
            raise ValueError("飞书文件夹不支持直接导入，请导入文件夹内的具体文档链接")

        raise ValueError(
            f"无法解析飞书链接: {url}\n"
            "支持的格式：飞书文档/表格/知识库/云空间文件/问卷等链接，"
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
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> KnowledgeEntry:
        """完整导入一个飞书文档到知识库。根据类型自动分发到三种策略。

        默认用 tenant_access_token（组织统一飞书应用）。仅在显式开启 user_fallback 时，
        才会遇权限错误后切换 user_access_token 重试。
        on_phase: 可选回调，用于报告当前阶段（如 "parse_url", "resolve_wiki", "exporting" 等）。
        """
        from app.services.lark_client import lark_client, LarkPermissionError
        from app.services.lark_errors import LarkAppDocumentPermissionError

        _report = on_phase or (lambda _: None)
        allow_user_fallback = _allow_user_token_fallback()

        # 1. 解析链接
        _report("parse_url")
        token, api_type, extra_params = self.parse_lark_url(url)

        # 默认不使用 user token
        effective_token: str | None = None

        # 2. wiki → 解析为实际文档 token
        wiki_title = None
        if api_type == "wiki":
            _report("resolve_wiki")
            try:
                node = await lark_client.get_wiki_node(token)
            except (PermissionError, LarkPermissionError) as e:
                if not allow_user_fallback:
                    raise LarkAppDocumentPermissionError(str(e)) from e
                effective_token = await self._get_user_token_or_raise(db, user)
                node = await lark_client.get_wiki_node(token, access_token=effective_token)
            token = node["obj_token"]
            api_type = node["obj_type"]
            wiki_title = node.get("title", "")

        # 3. 根据 api_type 分发到三种策略（带 fallback）
        try:
            return await self._dispatch_strategy(
                db, user, url, token, api_type, title, wiki_title,
                folder_id, category, effective_token,
                extra_params=extra_params, on_phase=_report,
            )
        except (PermissionError, LarkPermissionError) as e:
            if effective_token:
                raise  # 已经用了 user token 还失败，直接抛
            if not allow_user_fallback:
                raise LarkAppDocumentPermissionError(str(e)) from e
            effective_token = await self._get_user_token_or_raise(db, user)
            return await self._dispatch_strategy(
                db, user, url, token, api_type, title, wiki_title,
                folder_id, category, effective_token,
                extra_params=extra_params, on_phase=_report,
            )

    async def _get_user_token_or_raise(self, db: Session, user) -> str:
        """获取用户的有效 user_access_token，无则抛出提示性错误。"""
        token = await get_valid_user_token(db, user)
        if not token:
            raise PermissionError(
                "当前环境未启用个人飞书授权 fallback。请优先将该文档授权给 Le Desk 飞书应用。"
            )
        return token

    async def _dispatch_strategy(
        self, db, user, url, token, api_type, title, wiki_title,
        folder_id, category, access_token, *, extra_params=None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> KnowledgeEntry:
        """根据 api_type 分发到三种策略。"""
        if api_type in _EXPORTABLE_TYPES:
            return await self._strategy_export(
                db, user, url, token, api_type, title, wiki_title,
                folder_id, category, access_token=access_token,
                extra_params=extra_params, on_phase=on_phase,
            )
        elif api_type in _DIRECT_DOWNLOAD_TYPES:
            return await self._strategy_download(
                db, user, url, token, api_type, title, wiki_title,
                folder_id, category, access_token=access_token,
            )
        else:
            return await self._strategy_link_reference(
                db, user, url, token, api_type, title, wiki_title,
                folder_id, category, access_token=access_token,
            )

    # ── 策略A：可导出类型 ────────────────────────────────────────────────

    async def _strategy_export(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category,
        *, access_token: str | None = None, extra_params: dict | None = None,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> KnowledgeEntry:
        """doc/docx/sheet/bitable → export_tasks → 下载 → 提取文本 → 入库。
        bitable 导出失败时 fallback 到 records 拉取。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.services.knowledge_namer import auto_name
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        from app.services.knowledge_service import submit_knowledge
        from app.services.review_policy import review_policy
        from app.utils.file_parser import extract_text, extract_html

        _report = on_phase or (lambda _: None)
        file_ext_str = _EXPORT_EXT_MAP[api_type]

        # 创建导出任务 → 轮询 → 下载
        _report("exporting")
        try:
            ticket = await lark_client.create_export_task(token, api_type, file_ext_str, access_token=access_token)
            file_bytes = await lark_client.poll_and_download_export(ticket, doc_token=token, access_token=access_token)
        except Exception as export_error:
            if api_type == "bitable":
                logger.warning(f"Bitable export 失败，尝试 records fallback: {export_error}")
                return await self._bitable_records_fallback(
                    db, user, url, token, api_type, title, wiki_title,
                    folder_id, category, extra_params, access_token,
                    export_error=export_error,
                )
            raise

        # 写临时文件 → 提取文本
        _report("downloading")
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
        _report("generating_doc")
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

        # 后台 job 队列
        self._enqueue_jobs(db, entry)

        # 审核流程
        entry = submit_knowledge(db, entry)
        return entry

    # ── Bitable fallback：导出失败时改用 records API ──────────────────────

    async def _bitable_records_fallback(
        self, db, user, url, token, api_type, title, wiki_title,
        folder_id, category, extra_params, access_token, *, export_error,
    ) -> KnowledgeEntry:
        """bitable 导出失败时 fallback 到 records 拉取。
        缺 table_id 时自动调用 tables list API 获取。
        """
        from app.services.bitable_reader import bitable_reader
        from app.services.knowledge_service import submit_knowledge

        app_token = token  # parse_lark_url 对 bitable 返回的 token 就是 app_token
        table_id = (extra_params or {}).get("table_id")

        effective_token = access_token or await bitable_reader.get_token()

        # 缺 table_id 时自动列表
        if not table_id:
            try:
                tables = await bitable_reader.fetch_table_list(effective_token, app_token)
            except Exception as e:
                raise RuntimeError(
                    f"多维表格导出失败且无法获取数据表列表: export={export_error}, list={e}"
                )
            if not tables:
                raise RuntimeError(f"多维表格导出失败且该表格内无数据表: {export_error}")
            table_id = tables[0]["table_id"]
            if len(tables) > 1:
                table_names = "、".join(t["name"] for t in tables[:5])
                logger.info(
                    f"Bitable fallback: 多维表格含 {len(tables)} 个数据表（{table_names}），"
                    f"自动选择第一个: {tables[0]['name']} ({table_id})"
                )

        fields = await bitable_reader.fetch_fields(effective_token, app_token, table_id)
        records, stats = await bitable_reader.fetch_records_adaptive(effective_token, app_token, table_id)

        if not records:
            raise RuntimeError(f"多维表格导出和记录拉取均失败: export={export_error}, records={stats.get('errors')}")

        content_html = bitable_reader.records_to_html_table(fields, records)
        content = bitable_reader.records_to_text(fields, records)

        entry = self._build_entry(
            db, user, url, token, api_type, title, wiki_title, folder_id, category,
            content=content, content_html=content_html,
            capture_mode="upload_ai_clean",
            oss_key=None, file_type=None, file_ext=None, file_size=0,
            doc_render_mode="lark_bitable_import",
        )
        db.add(entry)
        db.flush()

        if content:
            await self._ai_enrich(entry, content, url, None, title, db)
        self._enqueue_jobs(db, entry)

        entry = submit_knowledge(db, entry)
        return entry

    # ── 策略B：文件直接下载 ──────────────────────────────────────────────

    async def _strategy_download(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category,
        *, access_token: str | None = None,
    ) -> KnowledgeEntry:
        """file 类型 → drive/v1/files/:token/download → 入库。"""
        from app.services.lark_client import lark_client
        from app.services.oss_service import generate_oss_key, upload_file as oss_upload
        from app.services.knowledge_namer import auto_name
        from app.services.knowledge_classifier import classify, apply_classification_to_entry
        from app.services.knowledge_service import submit_knowledge
        from app.services.review_policy import review_policy
        from app.utils.file_parser import extract_text, extract_html

        file_bytes, filename = await lark_client.download_file(token, access_token=access_token)

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

        self._enqueue_jobs(db, entry)

        entry = submit_knowledge(db, entry)
        return entry

    # ── 策略C：不可导出类型 → 获取元数据 + 嵌入链接 ────────────────────

    async def _strategy_link_reference(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category,
        *, access_token: str | None = None,
    ) -> KnowledgeEntry:
        """mindnote/slides/board/minutes/survey 等 → 通过元数据 API 获取信息，生成知识条目。"""
        from app.services.lark_client import lark_client
        from app.services.knowledge_service import submit_knowledge

        type_name = _TYPE_DISPLAY_NAME.get(api_type, api_type)

        # 尝试通过飞书 Drive API 获取文档元数据
        meta = {}
        try:
            meta = await lark_client.get_doc_meta(token, api_type, access_token=access_token)
        except Exception as e:
            logger.warning(f"获取飞书文档元数据失败: {e}")

        meta_title = meta.get("title", "") or wiki_title or ""
        create_time = meta.get("create_time", "")
        modify_time = meta.get("latest_modify_time", "")

        # 格式化时间
        def _fmt_ts(ts):
            if not ts:
                return ""
            try:
                import datetime
                return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return str(ts)

        create_str = _fmt_ts(create_time)
        modify_str = _fmt_ts(modify_time)

        # 生成有信息量的内容文本
        content_lines = [f"飞书{type_name}：{meta_title or '未命名'}"]
        if create_str:
            content_lines.append(f"创建时间：{create_str}")
        if modify_str:
            content_lines.append(f"最近更新：{modify_str}")
        content_lines.append(f"文档类型：{type_name}（该类型暂不支持全文导出）")
        content_lines.append(f"原始链接：{url}")
        content = "\n".join(content_lines)

        # 生成嵌入式链接卡片 HTML
        card_parts = []
        card_parts.append(f'<div class="lark-link-card" style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:8px 0;background:#fafafa;">')
        card_parts.append(f'  <h3 style="margin:0 0 8px 0;font-size:16px;">📄 {meta_title or "未命名"}</h3>')
        card_parts.append(f'  <p style="margin:4px 0;color:#666;font-size:13px;">类型：飞书{type_name}</p>')
        if create_str:
            card_parts.append(f'  <p style="margin:4px 0;color:#666;font-size:13px;">创建：{create_str}</p>')
        if modify_str:
            card_parts.append(f'  <p style="margin:4px 0;color:#666;font-size:13px;">更新：{modify_str}</p>')
        card_parts.append(f'  <p style="margin:8px 0 0 0;">')
        card_parts.append(f'    <a href="{url}" target="_blank" style="color:#3370ff;text-decoration:none;">在飞书中打开 →</a>')
        card_parts.append(f'  </p>')
        card_parts.append(f'</div>')
        card_parts.append(f'<p style="color:#999;font-size:12px;margin-top:8px;">该文档类型暂不支持全文内容导出，如需查看完整内容请点击上方链接。</p>')
        content_html = "\n".join(card_parts)

        # 优先用元数据标题
        effective_title = title or meta_title or wiki_title

        entry = self._build_entry(
            db, user, url, token, api_type, effective_title, wiki_title, folder_id, category,
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

        # AI 命名（用元数据内容做输入）
        if content:
            await self._ai_enrich(entry, content, url, None, title, db)

        # 策略C 也入 job 队列（understand/governance_classify）
        self._enqueue_jobs(db, entry)

        entry = submit_knowledge(db, entry)
        return entry

    # ── 公共工具方法 ─────────────────────────────────────────────────────

    def _enqueue_jobs(self, db, entry: KnowledgeEntry):
        """为飞书导入的 entry 创建后台处理 job（understand/ai_notes/governance_classify）。"""
        try:
            from app.models.knowledge_job import KnowledgeJob
            if entry.content:
                db.add(KnowledgeJob(knowledge_id=entry.id, job_type="understand", trigger_source="lark_import"))
                db.add(KnowledgeJob(knowledge_id=entry.id, job_type="ai_notes", trigger_source="lark_import"))
                entry.ai_notes_status = "pending"
            db.add(KnowledgeJob(knowledge_id=entry.id, job_type="governance_classify", trigger_source="lark_import"))
        except Exception as e:
            logger.warning(f"Failed to enqueue jobs for lark entry {entry.id}: {e}")

    def _build_entry(
        self, db, user, url, token, api_type, title, wiki_title, folder_id, category,
        *, content, content_html, capture_mode, oss_key, file_type, file_ext, file_size,
        doc_render_mode,
    ) -> KnowledgeEntry:
        """构建 KnowledgeEntry 对象。"""
        import datetime

        # 有 content_html 才标记 ready，否则 failed
        render_status = "ready" if content_html else "failed"
        render_error = None if content_html else "导入后未能生成可渲染内容"

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
            doc_render_status=render_status,
            doc_render_mode=doc_render_mode if content_html else None,
            doc_render_error=render_error,
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
            file_bytes = await lark_client.poll_and_download_export(ticket, doc_token=token)
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
