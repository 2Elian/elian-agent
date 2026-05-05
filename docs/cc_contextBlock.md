❯ TextBlock是什么呀，有什么作用呢

● TextBlock 是 LLM 对话中最基础的信息载体。

  定义

  # models.py
  @dataclass
  class TextBlock:
      type: Literal["text"] = "text"
      text: str = ""

  就这么简单 — 两个字段：type 永远是 "text"，text 是实际内容。

  为什么需要它？

  LLM API 不接收纯字符串。每条消息的 content 必须是 ContentBlock 的列表：

  # ❌ 错误 — API 不接受纯字符串
  message = {"role": "user", "content": "Fix the bug in auth.py"}

  # ✅ 正确 — content 必须是 ContentBlock 列表
  message = {"role": "user", "content": [
      {"type": "text", "text": "Fix the bug in auth.py"}
  ]}

  TextBlock 就是这个 {"type": "text", "text": "..."} 的 Python 对象表示。

  ContentBlock 家族

  一条消息可以混合多种块类型：

  ContentBlock 联合类型:
  ├── TextBlock        {"type": "text", "text": "..."}
  ├── ToolUseBlock     {"type": "tool_use", "id": "t1", "name": "Read", "input": {...}}
  ├── ToolResultBlock  {"type": "tool_result", "tool_use_id": "t1", "content": "..."}
  └── ImageBlock       {"type": "image", "source": {...}}

  实际例子

  当 LLM 返回 "我找到了 bug" 并且调用 Read 工具时：

  AssistantMessage(
      content=[
          TextBlock(text="I found the bug in auth.py"),      # ← 文本部分
          ToolUseBlock(id="t1", name="Read", input={          # ← 工具调用部分
              "file_path": "/src/auth.py"
          }),
      ]
  )

  在 API 层面这会变成：

  {
    "role": "assistant",
    "content": [
      {"type": "text", "text": "I found the bug in auth.py"},
      {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/src/auth.py"}}
    ]
  }

  在整个系统中的流转

  engine.py 构建消息:
    UserMessage(content=[TextBlock(text="Fix bug")])
            ↓
  normalization.py 规范化:
    提取 TextBlock.text → API dict {"role": "user", "content": "Fix bug"}
            ↓
  providers.py 发送 API 请求:
    body = {"messages": [{"role": "user", "content": "Fix bug"}]}
            ↓
  providers.py 解析 API 响应:
    收到 {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
            ↓
  engine.py 构建:
    AssistantMessage(content=[TextBlock(text="ok")])
            ↓
  token_estimation.py 估算 token:
    estimate_block(TextBlock(text="ok")) → 1 token  (3 chars / 4 ≈ 1)

  为什么不用纯字符串？

  因为 ToolUse 和 Text 需要共存。LLM 在一次回复中同时说话和调用工具：

  用户: "查一下 auth.py 里的 bug"
          ↓
  LLM 回复:
    content=[
        TextBlock("我来看一下 auth.py..."),    ← 说话
        ToolUseBlock("Read", "auth.py"),      ← 做事
        TextBlock("找到了，第 42 行有问题。"),  ← 继续说
    ]

  这就是 ContentBlock 联合类型存在的意义 — 一条消息里可以交错排列文本和工具调用。TextBlock
  就是其中最基础、最常用的那个。
