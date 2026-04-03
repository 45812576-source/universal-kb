"""阿里云 OSS 文件存储服务：上传、签名URL、删除。"""
from __future__ import annotations

import logging
import os
import uuid

import oss2

from app.config import settings
from app.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

_bucket: oss2.Bucket | None = None


def _get_bucket() -> oss2.Bucket:
    """懒初始化 OSS Bucket 实例。"""
    global _bucket
    if _bucket is not None:
        return _bucket

    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    _bucket = oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET)
    return _bucket


def generate_oss_key(ext: str, prefix: str = "knowledge") -> str:
    """生成 OSS 对象路径：{prefix}/{year}/{month}/{uuid}.{ext}"""
    now = utcnow()
    clean_ext = ext.lstrip(".")
    return f"{prefix}/{now.year}/{now.month:02d}/{uuid.uuid4()}.{clean_ext}"


def upload_file(local_path: str, oss_key: str) -> str:
    """上传本地文件到 OSS，返回 oss_key。"""
    bucket = _get_bucket()
    bucket.put_object_from_file(oss_key, local_path)
    logger.info(f"Uploaded to OSS: {oss_key}")
    return oss_key


def upload_bytes(data: bytes, oss_key: str, content_type: str = None) -> str:
    """上传字节流到 OSS。"""
    bucket = _get_bucket()
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    bucket.put_object(oss_key, data, headers=headers)
    logger.info(f"Uploaded bytes to OSS: {oss_key}")
    return oss_key


def generate_signed_url(oss_key: str, expires: int = 3600, inline: bool = False) -> str:
    """生成 OSS 签名下载 URL（默认1小时有效）。inline=True 时浏览器内联预览而非下载。"""
    bucket = _get_bucket()
    params = {}
    if inline:
        params["response-content-disposition"] = "inline"
    return bucket.sign_url("GET", oss_key, expires, params=params if params else None)


def generate_signed_upload_url(oss_key: str, expires: int = 600) -> str:
    """生成 OSS 签名上传 URL（前端直传场景，默认10分钟有效）。"""
    bucket = _get_bucket()
    return bucket.sign_url("PUT", oss_key, expires)


def download_file(oss_key: str, local_path: str) -> str:
    """下载 OSS 对象到本地路径，返回 local_path。"""
    bucket = _get_bucket()
    bucket.get_object_to_file(oss_key, local_path)
    logger.info(f"Downloaded from OSS: {oss_key} -> {local_path}")
    return local_path


def delete_file(oss_key: str) -> None:
    """删除 OSS 对象。"""
    bucket = _get_bucket()
    bucket.delete_object(oss_key)
    logger.info(f"Deleted from OSS: {oss_key}")


def file_exists(oss_key: str) -> bool:
    """检查 OSS 对象是否存在。"""
    bucket = _get_bucket()
    return bucket.object_exists(oss_key)


def get_file_size(oss_key: str) -> int:
    """获取 OSS 对象大小（bytes）。"""
    bucket = _get_bucket()
    meta = bucket.head_object(oss_key)
    return meta.content_length
