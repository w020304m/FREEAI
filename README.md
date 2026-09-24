# FREEAI

> **把免费 AI 聚合站的站内 API 反代并转换为 OpenAI / Anthropic 兼容格式的本地网关。**

[中文](#freeai) | 免责声明参见 [第零章](#零免责声明请务必先读)

---

## 零、免责声明（请务必先读）

> **⚠️ 本项目仅供个人学习、技术研究与本地实验参考使用。**
>
> 1. **禁止商用。** 不得将本项目（含其代码、衍生版本、以及通过它获得的一切输出）用于任何商业用途、盈利性服务、对外提供的公开服务、或任何形式的流量/内容变现。禁止将其部署为面向公众的 API 服务。
> 2. **仅供学习。** 本项目的唯一目的是学习 HTTP 反向代理、格式协议转换（OpenAI / Anthropic 兼容层）、浏览器会话与 Cloudflare 人机验证机制等**技术原理**。它不提供任何 AI 能力，所有模型能力均来自第三方站点。
> 3. **请勿滥用。** 请勿用于批量注册、批量调用、内容工厂、爬虫抓取、规避第三方站点的使用限制或任何违反第三方服务条款的行为。**请控制调用频率，尊重上游站点的免费额度。**
> 4. **风险自负。** 使用本项目可能违反上游站点的服务条款（ToS），可能导致你的 IP 或账号被封禁。**一切后果由使用者自行承担**，作者不承担任何责任。
> 5. **无担保。** 本项目按"原样"提供，不附带任何明示或暗示的担保。上游接口随时可能变更，本项目随时可能失效。
> 6. **不存储、不传播。** 本项目不含任何模型权重、不代理任何付费内容、不分发任何受版权保护的素材。所有请求均直连上游，作者不收集任何用户数据。
>
> **📧 侵权或异议请联系：`w020304m@gmail.com`**
>
> 若本项目（或其中任何部分）侵犯了你的权益、违反了你的服务条款，或你不希望它存在——**请发送邮件说明，我会在收到后立即删除仓库**，无需任何法律程序。请在邮件中注明具体诉求，我会尽快处理。

---

## 一、这是什么

FREEAI 是一个**本地运行的网关服务**：把 `aifreeforever.com` 的站内 API 转换为标准的 **OpenAI / Anthropic 接口格式**，让任何兼容这两种协议的客户端（OpenAI SDK、Anthropic SDK、各类 Agent 框架、Claude Code、Cherry Studio、NextChat 等）可以直接接入使用。

> ⚠️ 再次强调：本项目**不提供**任何 AI 能力，它只是一个协议转换层。上游站点的可用性、额度、政策变化都会直接影响本项目。**请仅用于学习。**

**已实现的功能：**

| 能力 | 端点 | 状态 |
|---|---|---|
| 文本对话（流式 / 非流式） | `POST /v1/chat/completions` | ✅ |
| 文本对话（Anthropic 格式） | `POST /v1/messages` | ✅ |
| 模型列表（含能力标注） | `GET /v1/models` | ✅ |
| 文生图 | `POST /v1/images/generations` | ✅ |
| 图生图 / 图像修改 | `POST /v1/images/edits` | ✅ |
| 文件上传（图像理解） | `POST /v1/files` | ✅ |
| 工具调用（tools） | 上述对话端点 | ⚠️ **提示词注入实现，见第四章** |
| 视频生成 | `POST /v1/video` | ❌ 上游无此 API，明确返回 501 |
| 微调（fine-tuning） | `POST /v1/fine_tuning/jobs` | ❌ 上游无此 API，明确返回 501 |
| 视觉（图像输入） | 对话端点传 `image_url` | ⚠️ 可用但受限，见第五章 |

---

## 二、核心问题：tools 是通过「提示词注入」模拟的

> **这是本项目最重要的技术局限，请务必理解后再使用。**

### 2.1 问题本质

**上游 API 根本不支持工具调用（function calling）。** 它的聊天请求体里没有 `tools` 字段，没有 `tool_choice`，模型也不会返回结构化的 `tool_calls`。上游本质上只是一个"你问我答"的纯文本补全接口。

为了让我们导出的 OpenAI / Anthropic 接口**看起来**支持 tools，本项目采取了一个**模拟方案**：

### 2.2 实现方式（`app/formats.py`）

```
客户端发送 tools 参数
        ↓
① 网关把工具定义（JSON Schema）渲染成一段文本
② 注入到 system prompt 里，并约定输出协议：
     "当需要调用工具时，独占一行输出：<<TOOL_CALL>>{"name":"...","arguments":{...}}"
        ↓
③ 上游模型（并不知道什么是 function calling）被要求按这个格式输出
        ↓
④ 网关用正则从纯文本回复里"抠"出 <<TOOL_CALL>> 标记
⑤ 解析 JSON，还原成原生的 tool_calls（OpenAI）或 tool_use（Anthropic）结构
```

对应代码：

- `formats.tools_prompt()` —— 把工具 schema 渲染进 system prompt
- `formats.TOOL_MARKER = "<<TOOL_CALL>>"` —— 约定的文本标记
- `formats.parse_tool_call()` —— 从模型输出的**纯文本**里解析调用
- `chat.py` 的多处调用点 —— 在完整响应与流式分片中还原成原生结构

### 2.3 因此存在的问题（务必知悉）

| # | 问题 | 说明 |
|---|---|---|
| 1 | **不是原生能力** | 模型并未真正"理解"工具调用协议，只是在模仿文本格式。可靠性完全取决于模型是否听话。 |
| 2 | **可能不遵守格式** | 模型可能拒绝输出标记、输出变体格式、把标记写在句子中间、或用 markdown 代码块包裹，导致解析失败。 |
| 3 | **参数可能幻觉** | 模型可能编造 schema 里不存在的参数名，或给出类型错误的参数值。**网关不会做严格的 JSON Schema 校验**。 |
| 4 | **多轮工具调用不可靠** | 原生 function calling 有专门的对齐训练；纯提示词模拟在多轮（调用→结果→再调用）时容易丢失上下文或格式崩坏。 |
| 5 | **流式解析是启发式的** | 流式输出时标记可能被切分到多个 chunk，网关需要缓冲拼接后判断，存在边缘情况。 |
| 6 | **与真实回答混淆** | 如果模型在正常回答里恰好写出 `<<TOOL_CALL>>` 字样，会被误判为工具调用。 |
| 7 | **token 浪费** | 工具 schema 会被完整塞进 system prompt，占用上下文。工具多时尤其明显。 |
| 8 | **不支持的字段被忽略** | `parallel_tool_calls`、`strict` 等原生参数**没有实际效果**，网关只是接受而不报错。 |
| 9 | **上游看不到 tool 结果的结构** | 回传的 `tool` 角色消息会被降级成文本描述塞进对话历史，上游模型无法区分"工具结果"与"用户发言"。 |

> **结论：本项目导出的 tools 能力适合做 Demo、学习与轻量实验。**
> **如果你要构建生产级 Agent，请使用官方 API 的原生 function calling。** 不要把关键业务链路建立在这个模拟层上。

---

## 三、快速开始

### 3.1 环境要求

- **Windows**（实测环境；核心依赖 CDP 附加到本机 Edge 窗口，Linux/macOS 需自行适配）
- **Python 3.11+**
- **Microsoft Edge 或 Chrome**（必须安装，且需要能打开图形界面）

### 3.2 安装

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 3.3 配置

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，**至少**设置：

```ini
API_MASTER_KEY=你的访问密钥          # 客户端调用本网关时用的 key
PORT=8110
AUTH_MODE=strict                     # 默认严格模式
CDP_PORT=9230                        # CDP 调试端口
```

### 3.4 启动

```powershell
# 方式一：双击
start.bat

# 方式二：命令行
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8110
```

启动后：

- **管理面板**：<http://127.0.0.1:8110/ui/>（或 `/panel`）
  在面板的「连接与密钥」页填入 `.env` 里的 `API_MASTER_KEY`
- **健康检查**：<http://127.0.0.1:8110/health>
- **接口文档**：面板的「接口参考」页有全部端点与调用示例

### 3.5 关于人机验证：通常**不需要**手动操作

服务启动时会自动打开一个浏览器窗口并访问上游站点。

> **多数情况下你什么都不用做** —— 只要浏览器里之前留有有效的 `cf_clearance` cookie，
> 网关就能直接工作。

**什么时候才需要手动过验证？**

只有当浏览器窗口里**确实显示了 Cloudflare 验证页面**（"Just a moment…" / "安全验证"）时，
才需要在窗口内手动完成一次验证。判断方式：

```powershell
curl http://127.0.0.1:8110/health
```

- `"session_ready": true` → **已可用，无需任何操作**
- `"session_ready": false` → 看 `hint` 字段的说明：
  - 若窗口显示验证页 → 手动过一次即可
  - 若窗口是正常页面 → 通常是上游限流或临时异常，**稍后重试即可，不必反复点验证**

> 这个窗口**需要保持开启**（可以最小化，双击 `minimize_window.bat`）。
> 关闭窗口 → 网关返回 503。

> 补充：**生图/改图**接口额外需要 Turnstile token，网关会自动在窗口内采集
> （首次请求较慢），这同样**不需要你手动点击**。

---

## 四、接口用法

### 4.1 OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8110/v1", api_key="你的API_MASTER_KEY")

r = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

### 4.2 Anthropic SDK

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8110", api_key="你的API_MASTER_KEY")

r = client.messages.create(
    model="gpt-5", max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(r.content[0].text)
```

### 4.3 文生图

```bash
curl -X POST http://127.0.0.1:8110/v1/images/generations \
  -H "Authorization: Bearer 你的API_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"a serene japanese garden","n":1,"size":"1024x1024"}'
```

### 4.4 图像修改（图生图）

```bash
curl -X POST http://127.0.0.1:8110/v1/images/edits \
  -H "Authorization: Bearer 你的API_MASTER_KEY" \
  -F image=@input.png \
  -F model=gpt-image-2 \
  -F prompt="change the red square to blue"
```

> 改图需要上游的人机验证 token，首次请求较慢（约 20–40 秒），之后 110 秒内会复用缓存。

### 4.5 工具调用（存在前述问题）

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

r = client.chat.completions.create(
    model="gpt-5",
    messages=[{"role": "user", "content": "北京天气怎么样？"}],
    tools=tools,
)
# 注意：这是模拟实现，可能失败或不准确 —— 见第二章
print(r.choices[0].message.tool_calls)
```

更多示例见 `examples/` 目录。

---

## 五、已知问题与限制

### 5.1 架构层面的根本限制

| # | 限制 | 说明 |
|---|---|---|
| 1 | **必须挂着一个浏览器窗口** | Cloudflare 的 `cf_clearance` 与浏览器指纹（IP + UA + TLS 指纹）强绑定，纯 HTTP 直连（含 curl_cffi 等指纹伪装）一律 403。唯一稳定通道是"已过验证的真实浏览器窗口内的页面级 fetch"。**服务无法在纯服务器环境（无 GUI）下运行。** |
| 2 | **单点、无法水平扩展** | 一个窗口 = 一个会话 = 一个出口 IP。做不到负载均衡或多实例并发。 |
| 3 | **后台标签页会被节流** | 浏览器会限制后台标签的 JS 执行。网关在请求时会短暂把站点标签页切到前台再切回，可能造成窗口闪烁。 |
| 4 | **会话会过期** | `cf_clearance` 有效期从数十分钟到数小时不等，过期后需要重新在窗口内手动过验证。 |
| 5 | **依赖上游改版** | 所有端点路径都是从上游前端 JS 逆向得到的（见 `app/config.py` 的 `MODEL_ENDPOINTS`）。**上游一旦改版，本项目立刻失效。** |

### 5.2 功能层面的限制

| # | 限制 | 说明 |
|---|---|---|
| 6 | **图片生成有人机验证** | 上游 v2 生图接口强制 Turnstile token，且**必须通过真实鼠标交互采集**（合成点击无效）。本项目通过 CDP 派发可信鼠标事件实现，但这是对抗性的、脆弱的。 |
| 7 | **每日额度限制** | 上游按 IP 限制每日免费额度。额度用完返回 **429**，需等次日重置（北京时间约 15:00）。 |
| 8 | **模型可用性不稳** | 模型列表里 45 个模型中，真正可用的聊天模型通常只有 5 个左右；部分模型上游直接返回 500（实测 `kimi-k2-6`、`gpt-5-mini` 间歇性 500）。 |
| 9 | **不支持视频生成与微调** | 上游没有这两个能力，网关明确返回 501 而不是假装成功。 |
| 10 | **图像编辑仅 4 个模型** | `gpt-image-2`、`seedream-4`、`qwen-image`、`grok-imagine`。 |
| 11 | **不支持 thinking / 推理模式** | 上游请求体没有 reasoning/thinking 相关字段，无法透传。 |
| 12 | **文件上传是本地代理** | 文件存在本地 `data/`，仅用于图像理解场景；上游并不真正"存储"你的文件。 |
| 13 | **网关不截断对话历史** | 网关会把客户端传来的全部历史塞进上游 `conversationHistory`。上游前端自身只取最近 20 条（`slice(-20)`），服务端是否同样截断**未经确认**。**长对话存在超出上游处理能力、被静默丢弃或报错的风险**，建议客户端自行控制历史长度。 |
| 14 | **Token 用量不准确** | 上游不返回 usage，网关返回的 `usage` 字段是占位值（全 0），**不要用于计费统计**。 |
| 15 | **视觉能力受限** | 图像输入需要特定的 base64 对象格式，传 data-URI 字符串会导致模型胡编内容。 |

### 5.3 合规与稳定性风险

| # | 风险 | 说明 |
|---|---|---|
| 16 | **违反上游 ToS** | 反向代理第三方站点 API 通常违反其服务条款。**存在被上游封禁 IP / 账号的风险。** |
| 17 | **不可用于生产** | 上述任意一条都足以让本项目不适合生产环境。**请勿用于任何实际业务。** |
| 18 | **本项目随时可能停止维护或删除** | 见第零章免责声明。 |

---

## 六、技术架构

```
┌──────────────────────────────────────────────────────────────┐
│  客户端（OpenAI SDK / Anthropic SDK / Agent / 面板）           │
└───────────────────────────┬──────────────────────────────────┘
                            │ OpenAI / Anthropic 协议
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  FREEAI 网关（FastAPI, main.py）                              │
│  ├── 鉴权（API_MASTER_KEY）                                   │
│  ├── 协议转换（app/formats.py）                               │
│  │     ├── OpenAI ⇄ 站内格式                                  │
│  │     ├── Anthropic ⇄ 站内格式                               │
│  │     └── tools 提示词注入与解析（模拟层）                     │
│  ├── 业务逻辑（app/chat.py, app/images.py, app/files.py）      │
│  └── 模型映射（app/models.py, app/config.py）                 │
└───────────────────────────┬──────────────────────────────────┘
                            │ CDP（Chrome DevTools Protocol）
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  干净的 Edge 窗口（app/cdp_bridge.py）                          │
│  └── 页面内 fetch() → 携带真实的 cf_clearance cookie           │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS
                            ▼
                  aifreeforever.com 站内 API
```

**为什么必须用 CDP 桥？** 实测结论：

| 通道 | 结果 |
|---|---|
| httpx / requests 直连 | ❌ 403（无 cookie） |
| curl_cffi（TLS 指纹伪装） | ❌ 403（`cf_clearance` 绑定签发时的浏览器指纹） |
| Playwright / Selenium 自动化窗口 | ❌ 被识破（CDP 指纹），手动过验证也会回弹 |
| **用户手动过验证的干净 Edge 窗口 + 页面内 fetch** | ✅ **唯一稳定通道** |

### 目录结构

```
FREEAI/
├── main.py                  # FastAPI 入口与全部路由
├── start.bat                # Windows 启动脚本
├── requirements.txt
├── .env.example             # 配置模板
├── app/
│   ├── cdp_bridge.py        # CDP 桥：附加浏览器、页面内 fetch、Turnstile 采集
│   ├── chat.py              # 对话逻辑（流式/非流式、历史、视觉）
│   ├── images.py            # 文生图 / 图生图 / 合规检查
│   ├── formats.py           # OpenAI / Anthropic / 站内 三向格式转换（含 tools 模拟层）
│   ├── models.py            # 模型列表与能力标注
│   ├── files.py             # 文件上传
│   ├── turnstile.py         # Turnstile token 获取与缓存
│   ├── session.py           # Playwright 会话后端（备选）
│   └── config.py            # 配置与模型端点映射
├── web/index.html           # 管理面板（单文件，无构建）
└── examples/                # 各语言/SDK 调用示例
```

---

## 七、配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_MASTER_KEY` | `1` | 访问密钥。客户端用 `Authorization: Bearer <key>` |
| `AUTH_MODE` | `strict` | `strict` 严格比对；`lenient` 任意 key 放行 |
| `PORT` | `8110` | 监听端口 |
| `CDP_PORT` | `9230` | CDP 调试端口（0 = 关闭 CDP 桥） |
| `BROWSER_EXECUTABLE` | 自动探测 | 浏览器可执行文件路径。**留空即自动探测** Edge/Chrome 的常见安装位置；若你的浏览器装在非标准位置，请填写完整路径 |
| `CHAT_PAGE` | 站内聊天页 | 用于建立会话的页面 |
| `OUTBOUND_PROXY` | 空 | 出口代理，用于改善 IP 信誉 |
| `TURNSTILE_SITEKEY` | 内置 | 上游 Turnstile sitekey |
| `TURNSTILE_SOLVER_URL` / `_KEY` | 空 | 外部验证码求解服务（可选，留空则浏览器内采集） |
| `USE_DIRECT_HTTP` | `False` | 直连 HTTP 加速通道（实测无效，保持关闭） |
| `UPSTREAM_TIMEOUT` | `240` | 上游请求超时（秒） |
| `MODELS_CACHE_TTL` | `3600` | 模型列表缓存（秒） |

完整配置见 `.env.example`。

---

## 八、验证是否可用

启动后，按以下顺序确认链路是否正常：

**1. 检查健康状态**

浏览器打开 <http://127.0.0.1:8110/health>，或命令行：

```powershell
curl http://127.0.0.1:8110/health
```

期望看到：

```json
{"status":"ok","session_ready":true,"browser_alive":true,"models_cached":true,"hint":"运行正常，可直接调用接口。"}
```

若 `session_ready` 为 `false`，请阅读响应中的 `hint` 字段 —— 它会说明是窗口被关闭、
还是上游暂不可达。**只有窗口确实显示 Cloudflare 验证页时才需要手动过一次**。

**2. 检查模型列表**

```powershell
curl -H "Authorization: Bearer 你的API_MASTER_KEY" http://127.0.0.1:8110/v1/models
```

**3. 试一次对话**（把 `API_KEY` 换成你自己的）

```powershell
curl -X POST http://127.0.0.1:8110/v1/chat/completions ^
  -H "Authorization: Bearer 你的API_MASTER_KEY" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"gpt-5\",\"messages\":[{\"role\":\"user\",\"content\":\"说 pong\"}]}"
```

正常应返回 `pong`。也可以直接使用管理面板 <http://127.0.0.1:8110/ui/> 的聊天页做可视化验证。

> 提示：可用模型以 `/v1/models` 返回的 `available` 字段为准。上游不同模型的可用性会随时变化，
> 部分模型可能间歇性返回 500 —— 换一个模型重试即可。

---

## 九、常见问题

**Q：启动后 `/health` 显示 `session_ready: false`？**
A：先看响应里的 `hint` 字段。**这不一定意味着需要人机验证**：
- 若 `browser_alive: false` → 浏览器窗口被关闭了，重新运行 `start.bat`
- 若浏览器窗口显示 Cloudflare 验证页 → 在窗口内手动过一次
- 若浏览器窗口是正常页面 → 通常是上游限流或临时异常，**稍等重试即可**

**Q：请求返回 503？**
A：浏览器会话失效或窗口被关闭。检查窗口是否还在，重新过验证。

**Q：请求返回 401 / 403？**
A：`API_MASTER_KEY` 不对。在面板「连接与密钥」页填入 `.env` 中的值。

**Q：图片生成返回 429？**
A：当日免费额度用完，等次日重置。**这不是 bug，请勿绕过。**

**Q：改图一直 403 `missing-input-response`？**
A：Turnstile token 采集失败。确认窗口在前台且页面正常，或配置 `TURNSTILE_SOLVER_URL`。

**Q：工具调用不生效 / 解析失败？**
A：见第二章 —— 这是提示词注入模拟方案的固有缺陷。换个更"听话"的模型可能改善，但无法根治。

**Q：启动后没弹出浏览器窗口？**
A：程序会自动探测 Edge / Chrome 的常见安装路径。若你的浏览器装在非标准位置（或只装了其他 Chromium 浏览器），请在 `.env` 中设置 `BROWSER_EXECUTABLE` 为浏览器的完整路径，例如：

```ini
BROWSER_EXECUTABLE=C:\Program Files\Google\Chrome\Application\chrome.exe
```

**Q：能部署到服务器 / Docker 吗？**
A：**不能**（除非服务器有图形界面并能手动过验证）。本项目依赖真实浏览器窗口，见 5.1。

**Q：能商用吗？**
A：**不能。** 见第零章免责声明。

---

## 十、许可与声明

本项目仅供**学习、研究与个人实验**使用。

- ❌ **禁止任何形式的商业使用**
- ❌ **禁止部署为公开服务**
- ❌ **禁止用于批量调用、内容生成农场、爬虫等滥用场景**
- ❌ **禁止移除本 README 中的免责声明后二次分发**

本项目与 `aifreeforever.com` 及其中出现的任何模型提供商**没有任何关联**，未被其授权、认可或赞助。所有商标与模型名称归各自所有者所有。

**本项目按"原样"提供，不提供任何担保。使用风险由使用者自行承担。**

---

## 📧 联系与删除请求

如有任何问题、侵权异议，或**希望删除本仓库**，请联系：

**w020304m@gmail.com**

我会在收到邮件后尽快处理删除请求。

---

<div align="center">

**⚠️ 再次提醒：仅供学习参考 · 禁止商用 · 请勿滥用 ⚠️**

</div>
