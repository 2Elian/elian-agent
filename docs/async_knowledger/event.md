# asyncio.Event

Event 就是一个“开关”
Event 内部有一个私有变量 self._value，它的值要么是 False（关），要么是 True（开）。
四个方法就是对这个开关的读、写、等待操作。

1. is_set() —— 看一眼开关
python
def is_set(self):
    return self._value
作用：立刻告诉你开关现在是开（True）还是关（False），不会让你等待。

什么时候用：你想检查一下状态，但不想被阻塞。

2. set() —— 打开开关
python
def set(self):
    if not self._value:          # 如果现在是关着的
        self._value = True       # 打开
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(True)  # 通知所有正在等待的人
作用：把开关变成 True。

副作用：同时会通知所有正在执行 wait() 并已挂起的协程：“开关已经开了，你们可以继续了”。

如果开关本来就是开的，调用 set() 什么也不做。

3. clear() —— 关闭开关
python
def clear(self):
    self._value = False
作用：把开关变回 False。

效果：之后再执行 wait() 的协程就会进入等待状态，直到下一次 set() 被调用。

4. wait() —— 等到开关开启
python
async def wait(self):
    if self._value:            # 开关已经开了，直接通过
        return True

    fut = self._get_loop().create_future()
    self._waiters.append(fut)  # 我挂个号，说“我在等”
    try:
        await fut              # 停在这里，直到被 set() 通知
        return True
    finally:
        self._waiters.remove(fut)  # 不管怎样，最后都把自己的挂号取消
作用：

如果开关是开的（_value == True），立刻返回 True，不等待。

如果开关是关的，就把当前协程停在这里（挂起），直到另一个协程调用了 set() 打开开关，然后 wait() 返回 True。

关键点：await fut 这行就是“停车点”，协程的执行就暂停在这一行，不会往下走，也不会消耗 CPU。当 set() 里调用了 fut.set_result(True) 时，这行会自动醒来，然后函数返回 True。

它们之间的协作关系（最简流程）
text
初始时开关是 False

协程A调用 wait()
   ↓ 因为 False，创建一个 Future，挂起协程A（停住不动）

协程B调用 set()
   ↓ 把开关变成 True
   ↓ 通知协程A的 Future：“可以醒了”
   ↓ 协程A从 await 处醒来，wait() 返回 True，继续执行

现在开关是 True
协程C调用 wait()
   ↓ 看到开关是 True，立刻返回 True，不停

协程D调用 clear()
   ↓ 又把开关拨回 False
   ↓ 之后再有协程调用 wait() 又会停住


❯ engine 里面的self._abort是什么？详细解释其作用

● # engine.py, QueryEngine.__init__
  self._abort = cfg.abort_controller or asyncio.Event()

  self._abort 是一个 asyncio.Event — Python 内置的线程安全信号量。两个状态：

  asyncio.Event.clear()     → 未触发 (红灯 🔴) — 正常运行
  asyncio.Event.set()       → 已触发 (绿灯 🟢) — 要求停止
  asyncio.Event.is_set()    → 检查当前状态

  在引擎中的 4 个位置

  位置 1：每次新请求时重置

  # engine.py, submit_message() 开头
  async def submit_message(self, prompt):
      self._abort.clear()            # 🔴 重置为 "未触发" — 允许运行

  位置 2：循环中检查 — "我该停了吗？"

  # engine.py, 主查询循环
  while turn_count < max_turns and not self._abort.is_set():
      #                            ^^^^^^^^^^^^^^^^^^^^^^^^
      #  每次迭代都检查：有人让我停吗？
      turn_count += 1
      ...

  位置 3：外部调用停止

  # engine.py
  def interrupt(self):
      self._abort.set()              # 🟢 触发 — 告诉循环停止

  当用户点击 "Stop" 按钮、API 错误需要中止、或外部系统需要取消时，调用 engine.interrupt()。

  位置 4：Agent 子进程中也检查

  # agents.py, run_agent()
  for turn in range(ctx.max_turns):
      if ctx.abort.is_set():         # 子 Agent 也有自己的 abort
          break

  完整工作流

  时间线:
    0s  用户输入 "Fix the bug"
    0s  engine._abort.clear()          ← 🔴 重置

    1s  while not _abort.is_set():     ← 检查，未触发，继续
    1s  API 调用开始...

    3s  用户点击 "Stop" 按钮
    3s  engine.interrupt()             ← 调用
    3s  self._abort.set()              ← 🟢 触发!

    4s  API 响应返回（太慢了）
    4s  while not _abort.is_set():     ← 检查，已触发！
    4s  退出循环，返回部分结果

  为什么需要两套 abort？

  引擎内部有一个，Agent 子进程各有一个：

  # Engine:
  self._abort = asyncio.Event()       # 控制主引擎
  # 每个 Agent:
  ctx.abort = asyncio.Event()          # 控制子 Agent

  因为你需要独立控制它们：

  用户点击 "Stop"
    → 主引擎 _abort.set()     ← 停止主循环
    → 所有子 Agent ctx.abort.set()  ← 同时停止所有子 Agent

  某个 Agent 超时
    → ctx.abort.set()          ← 只停止这个 Agent
    → 主引擎继续运行           ← 其他 Agent 不受影响

  对比：和 TypeScript 源版完全一致

  // TypeScript 版 (QueryEngine.ts)
  private abortController: AbortController

  interrupt(): void {
      this.abortController.abort()    // 同样的模式
  }

  # Python 版
  self._abort = asyncio.Event()

  def interrupt(self):
      self._abort.set()               # 完全等价

  区别只是 TypeScript 用 AbortController（Web 标准），Python 用 asyncio.Event（asyncio 标准）— 效果完全一样。