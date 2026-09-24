"""生图/改图使用示例：模型识别 + 文生图 + 图生图（multipart 上传）。

运行：python examples/image_example.py
"""
import io
import struct
import time
import zlib
from pathlib import Path

import httpx


def _env_value(key: str, default: str = None) -> str:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or default
    except Exception:
        pass
    return default


def make_png(color: tuple, size: int = 64) -> bytes:
    """生成纯色 PNG（演示用；实际用你自己的图片文件）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    px = bytes(color)
    raw = b"".join(b"\x00" + px * size for _ in range(size))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    headers = {}
    key = _env_value("API_MASTER_KEY")
    if key and key not in ("", "1"):
        headers["Authorization"] = f"Bearer {key}"
    c = httpx.Client(base_url="http://127.0.0.1:8110", headers=headers, timeout=300, trust_env=False)

    # 1. 识别模型类别：capabilities 是唯一判别依据
    print("【1】识别模型类别 GET /v1/models")
    data = c.get("/v1/models", timeout=60).json().get("data", [])
    chat = [m["id"] for m in data if "chat" in (m.get("capabilities") or [])]
    gen = [m["id"] for m in data if "image" in (m.get("capabilities") or [])]
    edit = [m["id"] for m in data if "image_edit" in (m.get("capabilities") or [])]
    print(f"  聊天(capabilities=chat): {len(chat)} 个 → {chat[:5]}")
    print(f"  生图(capabilities=image): {len(gen)} 个 → {gen[:5]}")
    print(f"  改图(capabilities=image_edit): {len(edit)} 个 → {edit}")

    # 2. 文生图
    print("\n【2】文生图 POST /v1/images/generations")
    t0 = time.time()
    r = c.post("/v1/images/generations", json={
        "model": "gpt-image-2",          # 生图模型 id（见上一步 image 类）
        "prompt": "a small blue circle on white background",
        "size": "512x512",               # 支持 1024x1024 / 1792x1024 / 1024x1792 等
    })
    if r.status_code == 200:
        print(f"  ✅ {round(time.time()-t0,1)}s {r.json()['data'][0]['url'][:90]}")
    else:
        print(f"  {r.status_code} {r.text[:200]}（403=每IP配额收紧；429=每日限额）")

    # 3. 图生图/改图：multipart 上传你的图片文件
    print("\n【3】改图 POST /v1/images/edits（multipart）")
    img = make_png((255, 0, 0))  # 实际替换为 open("your.png","rb")
    t0 = time.time()
    r = c.post("/v1/images/edits",
               files=[("image", ("input.png", io.BytesIO(img), "image/png"))],
               data={"prompt": "turn the shape blue",
                     "model": "gpt-image-2",   # 必须是 capabilities 含 image_edit 的模型
                     "size": "512x512"})
    # 最多 3 张：files=[("image", ...), ("image", ...), ("image", ...)]
    if r.status_code == 200:
        print(f"  ✅ {round(time.time()-t0,1)}s {r.json()['data'][0]['url'][:90]}")
    else:
        print(f"  {r.status_code} {r.text[:260]}")
        print("  （说明：改图正式通道需 Turnstile token。两种方案见 README「图像通道说明」）")
    c.close()


if __name__ == "__main__":
    main()