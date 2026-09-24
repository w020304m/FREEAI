"""Agent 工具调用示例：证明本网关可作为 Agent 的 LLM 后端。

上游无原生 tools 字段，但实测模型能严格按 prompt 协议输出工具调用 JSON —
本示例演示完整 Agent 循环：模型请求工具 → 框架执行 → 回填结果 → 模型出最终答案。

运行：python examples/agent_tools_example.py
"""
import json
import time
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


# ---- 模拟工具（实际 Agent 里换成真实函数/API 调用）----
TOOLS = {
    "get_weather": lambda location: f"{location}今天晴，26℃",
    "search_web": lambda query: f"搜索「{query}」：相关结果前三条……",
}

SYSTEM_PROMPT = """你可以调用以下工具解决用户问题：
- get_weather(location: string): 查询某地天气
- search_web(query: string): 搜索网络信息

当需要调用工具时，只输出一行 JSON（不要输出其他任何文字）：
{"tool_call": {"name": "工具名", "arguments": {"参数名": "值"}}}
当信息已足够时，直接给出最终回答。"""


def main() -> None:
    headers = {}
    key = _env_value("API_MASTER_KEY")
    if key and key not in ("", "1"):
        headers["Authorization"] = f"Bearer {key}"
    c = httpx.Client(base_url="http://127.0.0.1:8110", headers=headers, timeout=300, trust_env=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "北京今天天气怎么样？适合户外跑步吗？"},
    ]

    # ---- Agent 循环（最多 5 轮）----
    for turn in range(5):
        t0 = time.time()
        r = c.post("/v1/chat/completions", json={
            "model": "gpt-5", "messages": messages, "stream": False,
        })
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "") \
            if r.status_code == 200 else f"[HTTP {r.status_code}]"
        print(f"\n[轮次 {turn + 1}] {round(time.time() - t0, 1)}s")
        print(f"模型: {content[:300]}")

        # 尝试解析工具调用
        tool_called = False
        for line in content.strip().splitlines():
            line = line.strip().lstrip("```json").lstrip("```").rstrip("```")
            try:
                obj = json.loads(line)
            except Exception:
                continue
            tc = obj.get("tool_call") or obj.get("tool_calls")
            if isinstance(tc, list):
                tc = tc[0] if tc else None
            if isinstance(tc, dict) and tc.get("name") in TOOLS:
                name, args = tc["name"], tc.get("arguments", {})
                print(f">>> 执行工具 {name}({args})")
                result = TOOLS[name](**args)
                print(f"<<< 工具结果: {result}")
                messages.append({"role": "assistant", "content": line})
                messages.append({"role": "user", "content": f"[工具 {name} 返回结果]\n{result}"})
                tool_called = True
                break
        if not tool_called:
            print("\n✅ Agent 循环结束（模型给出最终答案，无需再调工具）")
            break
    c.close()


if __name__ == "__main__":
    main()