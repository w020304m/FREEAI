"""OpenAI SDK 接入示例。

安装：pip install openai
运行：python examples/openai_example.py --key YOUR_MASTER_KEY
"""
import argparse
import os
from pathlib import Path

from openai import OpenAI


def _env_value(key: str, default: str = None) -> str:
    """从项目根 .env 读取配置（自动带上 PORT / API_MASTER_KEY）。"""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or default
    except Exception:
        pass
    return os.environ.get(key, default)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="默认读 .env 的 PORT")
    ap.add_argument("--key", default=None, help="默认自动读 .env 的 API_MASTER_KEY")
    ap.add_argument("--model", default="gpt-5")
    args = ap.parse_args()

    base = args.base or f"http://127.0.0.1:{_env_value('PORT', '8110')}/v1"
    key = args.key if args.key is not None else _env_value("API_MASTER_KEY", "1")
    client = OpenAI(base_url=base, api_key=key)

    # 1) 模型列表
    models = client.models.list().data
    chat = [m.id for m in models if "chat" in (m.model_extra.get("capabilities") or [])
            and m.model_extra.get("available")]
    print(f"可用聊天模型: {chat}")

    # 2) 流式对话
    print("\n[流式]")
    stream = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": "用一句话介绍你自己"}],
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
    print()

    # 3) 非流式
    print("\n[非流式]")
    resp = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": "say pong"}],
    )
    print(resp.choices[0].message.content)

    # 4) 图像生成
    print("\n[图像]")
    img = client.images.generate(
        model="gpt-image-2", prompt="a tiny red apple icon", size="512x512",
    )
    print(img.data[0].url)


if __name__ == "__main__":
    main()