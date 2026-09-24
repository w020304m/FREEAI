"""Anthropic SDK 接入示例。

安装：pip install anthropic
运行：python examples/anthropic_example.py --key YOUR_MASTER_KEY

Claude Code 命令行接入（无需本脚本）：
    set ANTHROPIC_BASE_URL=http://127.0.0.1:8110
    set ANTHROPIC_AUTH_TOKEN=YOUR_MASTER_KEY
    set ANTHROPIC_MODEL=gpt-5        ← 必须覆盖，默认 claude-* 名不被网关识别
"""
import argparse
import os
from pathlib import Path

from anthropic import Anthropic


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
    ap.add_argument("--model", default="gpt-5", help="网关模型为 OpenAI 风格名，如 gpt-5 / claude-sonnet-4-6")
    args = ap.parse_args()

    base = args.base or f"http://127.0.0.1:{_env_value('PORT', '8110')}"
    key = args.key if args.key is not None else _env_value("API_MASTER_KEY", "1")
    client = Anthropic(base_url=base, api_key=key)

    # 1) 非流式
    print("[非流式]")
    msg = client.messages.create(
        model=args.model,
        max_tokens=1024,
        messages=[{"role": "user", "content": "say pong"}],
    )
    print("".join(b.text for b in msg.content if b.type == "text"))

    # 2) 流式
    print("\n[流式]")
    with client.messages.stream(
        model=args.model,
        max_tokens=1024,
        messages=[{"role": "user", "content": "用一句话介绍你自己"}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()


if __name__ == "__main__":
    main()