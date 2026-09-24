"""文件上传（OpenAI Files API 兼容的"基本文件上传"）。

- POST /v1/files        multipart 上传（purpose 可指定；默认 fine-tune 语义占位）
- GET  /v1/files         列表
- GET  /v1/files/{id}    元信息
- GET  /v1/files/{id}/content  下载内容
- DELETE /v1/files/{id}  删除

存储：本地 ./data/uploads/（进程内；生产可换成对象存储）。
注意：上游 aifreeforever 没有真正的文件存储/微调服务，这里提供的是本地代理存储，
方便 Agent 把图片/文档先上传，再通过 /v1/images/edits 或聊天多模态使用。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import UploadFile

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
_META_FILE = Path(__file__).resolve().parent.parent / "data" / "files_meta.json"
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25MB 上限，防止内存 DoS


class FileStore:
    def __init__(self) -> None:
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if _META_FILE.exists():
                self._meta = json.loads(_META_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("files meta 读取失败: %s", e)

    def _save(self) -> None:
        try:
            _META_FILE.parent.mkdir(parents=True, exist_ok=True)
            _META_FILE.write_text(json.dumps(self._meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("files meta 保存失败: %s", e)

    async def save(self, file: UploadFile, purpose: str = "assistants") -> Dict[str, Any]:
        data = await file.read()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"文件超过大小限制（最大 {MAX_FILE_BYTES // (1024 * 1024)}MB）")
        fid = f"file-{uuid.uuid4().hex}"
        fname = file.filename or "unnamed"
        path = _UPLOAD_DIR / fid
        path.write_bytes(data)
        entry = {
            "id": fid,
            "object": "file",
            "bytes": len(data),
            "created_at": int(time.time()),
            "filename": fname,
            "purpose": purpose,
            "content_type": file.content_type or "application/octet-stream",
        }
        self._meta[fid] = entry
        self._save()
        return entry

    def list(self) -> List[Dict[str, Any]]:
        return list(self._meta.values())

    def get(self, fid: str) -> Optional[Dict[str, Any]]:
        return self._meta.get(fid)

    def path(self, fid: str) -> Optional[Path]:
        if fid not in self._meta:
            return None
        p = _UPLOAD_DIR / fid
        return p if p.exists() else None

    def delete(self, fid: str) -> bool:
        if fid not in self._meta:
            return False
        p = _UPLOAD_DIR / fid
        if p.exists():
            p.unlink()
        del self._meta[fid]
        self._save()
        return True


file_store = FileStore()
