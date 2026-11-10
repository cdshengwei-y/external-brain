"""把桌宠的大脑从前置的 sw-bot 换成直连的 OpenAI 兼容接口。

为什么要有这个模块
------------------
``swbot.py`` 那条链路是「设备 → 中枢 → bot.vx123.xin → 中转平台 → 模型」，
2026-11-06 实测同一条提示词要 23.8s / 96.0s，还撞过一次
``429 prefill_cap_refused: the cell is at capacity``。瓶颈全在中间的转发环节。

``192.168.0.245:8888`` 上跑的 sglang（deepseek-v4.1-flash）实测整段中位 1.95s、
意图 JSON 5/5 合法，于是这里做一层薄适配：对外长得和 ``SwBotClient`` 一样
（``send(text, bot_id=None, raw=False)``），这样 ``SwBotAgent`` /
``SwBotIntentClassifier`` 一行都不用改就能直接复用。

配置（都从环境变量读，缺哪项就不启用）
--------------------------------------
主用：
    HUB_CHAT_BASE_URL=http://192.168.0.245:8888/v1
    HUB_CHAT_MODEL=deepseek-v4.1-flash
    HUB_CHAT_API_KEY=sk-local          # 本地服务不校验，但有些客户端要求非空
备用（可选，主用失败/超时自动顶上）：
    HUB_CHAT_FALLBACK_BASE_URL=https://sub2api.vx123.xin/v1
    HUB_CHAT_FALLBACK_MODEL=gpt-6-luna
    HUB_CHAT_FALLBACK_API_KEY=sk-...
可调：
    HUB_CHAT_TIMEOUT=20                # 单次请求秒数，超时即换下一个端点
    HUB_CHAT_MAX_TOKENS=150            # 语音回复不需要长文，顺手压住时长
    HUB_CHAT_SYSTEM_PROMPT=...         # 人设；不设就用下面的默认，设成空串则不加
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from agent_gateway.pet_agent import ChatModelError, OpenAICompatibleChatModel
from agent_gateway.swbot import DEFAULT_USER_AGENT, DEFAULT_VOICE_PREFIX

# 桌宠是「一句话应声」的场景，默认人设只做三件事：表明身份、说人话、别长篇大论。
DEFAULT_SYSTEM_PROMPT = (
    "你是桌面上的语音助手，别人用说话的方式跟你交流，你的回答会被念出来。"
    "所以只用口语，一两句话说清楚，不要用 markdown、不要列条、不要念表情符号。"
    "不确定的事情直接说不确定，不要编。"
    # 2026-11-06 现场：用户问「说说你会什么」它答「会查天气新闻」，随后被问
    # 「查信用卡」又说「我这边查不了」，还补一句「也查不到叫陈的人」——
    # 它根本没查过，纯属顺着话头编。能力边界写死在这儿，别让模型自己发挥。
    "你真正能做的只有：聊天、回答常识问题、报时间和天气、放歌和调音量、"
    "开关音乐喷泉、录音豆开关和查它的状态。"
    "查信用卡、查账户、查联系人、搜实时新闻、控制别的家电、下单买东西，"
    "这些你都做不到，直接说做不到，绝对不要说「我帮你查一下」，"
    "也不要编造「查不到」这种好像已经查过的说法。"
)


def default_system_prompt() -> str:
    """默认人设 + 当前身份。

    身份不能写死在常量里：用户随时会改名（「以后叫你小灰」），
    而客户端是启动时装配一次的，写死就会用到下次重启为止都是旧名字。
    所以每次请求现取一遍 ``pet_identity.identity_line()``。
    """
    from agent_gateway.pet_identity import identity_line

    line = identity_line()
    return f"{line}\n{DEFAULT_SYSTEM_PROMPT}" if line else DEFAULT_SYSTEM_PROMPT


class ChatLLMError(RuntimeError):
    """所有端点都没给出可用回复。"""


class ChatLLMClient:
    """OpenAI 兼容接口的 ``SwBotClient`` 替身（鸭子类型，只实现被用到的部分）。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        *,
        timeout_seconds: float = 20.0,
        max_tokens: Optional[int] = 300,
        system_prompt: Optional[Any] = None,
        voice_prefix: str = DEFAULT_VOICE_PREFIX,
        user_agent: str = DEFAULT_USER_AGENT,
        label: str = "chat",
        history_turns: int = 6,
        history_ttl_seconds: float = 300.0,
        history_path: Optional[Path] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.label = label
        # 可以是字符串，也可以是零参函数 —— 身份会变，得每次现取。
        self.system_prompt = system_prompt if system_prompt is not None else default_system_prompt
        self.voice_prefix = voice_prefix
        # SwBotAgent 会读这个属性做启动打印，直连模式下没有 Bot 的概念
        self.bot_hint = model
        # 会话记忆：设备端没有上下文，每句话到这儿都是全新的。
        # 2026-11-06 现场：说完「叫麦德」，下一句问「我叫什么名字」，
        # 它答「你还没给我起名字呢」—— 刚答应就忘。
        self.history_turns = max(0, int(history_turns))
        self.history_ttl_seconds = float(history_ttl_seconds)
        self._histories: Dict[str, List[Dict[str, Any]]] = {}
        self._history_seen: Dict[str, float] = {}
        self._history_wall: Dict[str, float] = {}
        self._history_lock = threading.Lock()
        # 历史落盘：默认只在内存（重启就丢）。给了路径就每次记完写一份，
        # 启动时按墙钟时间恢复剩余 TTL —— 上周说好的「重启不丢名字，也不丢对话」。
        self.history_path = Path(history_path) if history_path else None
        self._history_loaded = False
        if self.history_path is not None:
            self._load_history()
        self._model = OpenAICompatibleChatModel(
            self.base_url,
            model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            user_agent=user_agent,
        )

    # ---------------- SwBotClient 兼容层 ----------------

    def history_text(self, conversation_id: str, max_chars: int = 700) -> str:
        """把最近几轮压成一段纯文本，供意图分类器拼进提示词。

        为什么不直接塞 messages：分类器要求「只输出一行 JSON」，一旦带上
        role=assistant 的历史，模型很容易顺着聊天而不再输出 JSON。
        """
        if not conversation_id:
            return ""
        lines = []
        for message in self._history_for(conversation_id):
            role = "用户" if message.get("role") == "user" else "你"
            lines.append(f"{role}：{message.get('content', '')}")
        text = "\n".join(lines)
        return text[-max_chars:] if len(text) > max_chars else text

    def remember(self, conversation_id: str, user_text: str, reply: str) -> None:
        """给不走 send() 的调用方（意图分类器）补记一轮。"""
        if conversation_id and self.history_turns > 0 and reply:
            self._remember(conversation_id, user_text, reply)

    def _history_for(self, conversation_id: str) -> List[Dict[str, Any]]:
        """取出该会话的历史消息；闲置超过 TTL 就整段丢掉。

        设备端一次对话窗口只有 45 秒，隔很久再喊醒是全新的一轮，
        把半小时前的「我叫麦德」带进来只会让模型答得莫名其妙。
        """
        now = time.monotonic()
        with self._history_lock:
            last = self._history_seen.get(conversation_id)
            if last is not None and now - last > self.history_ttl_seconds:
                self._histories.pop(conversation_id, None)
                self._history_seen.pop(conversation_id, None)
                return []
            return list(self._histories.get(conversation_id, ()))

    def _remember(self, conversation_id: str, user_text: str, reply: str) -> None:
        limit = self.history_turns * 2
        with self._history_lock:
            bucket = self._histories.setdefault(conversation_id, [])
            bucket.append({"role": "user", "content": user_text})
            # 长回复（讲故事那种）截断保存：记忆要的是「说过什么」，
            # 不是逐字复述，留着全文只会白烧 prefill。
            bucket.append({"role": "assistant", "content": reply[:240]})
            if len(bucket) > limit:
                del bucket[: len(bucket) - limit]
            self._history_seen[conversation_id] = time.monotonic()
            self._history_wall[conversation_id] = time.time()
            self._save_history_locked()

    def _save_history_locked(self) -> None:
        """把全部会话历史原子写盘。调用方必须已持有 ``_history_lock``。"""
        if self.history_path is None:
            return
        payload = {
            cid: {
                "wall": self._history_wall.get(cid, time.time()),
                "messages": messages,
            }
            for cid, messages in self._histories.items()
        }
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.history_path.parent),
                delete=False, suffix=".tmp",
            )
            try:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.history_path)
        except OSError as error:
            # 落盘失败不拦说话：记忆退化为「重启会丢」，别为它打断对话。
            print(f"[CHAT] 历史落盘失败（不影响使用）：{error}", flush=True)

    def _load_history(self) -> None:
        """启动时恢复历史；过期的（按墙钟时间算）直接丢弃。"""
        assert self.history_path is not None
        self._history_loaded = True
        try:
            raw = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # 没有文件 / 文件被写坏：当全新开始，不要因此起不来。
        if not isinstance(raw, dict):
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        with self._history_lock:
            for cid, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                messages = entry.get("messages")
                wall = entry.get("wall")
                if not isinstance(messages, list) or not messages:
                    continue
                if not isinstance(wall, (int, float)):
                    continue
                age = now_wall - float(wall)
                if age < 0 or age > self.history_ttl_seconds:
                    continue  # 重启花掉的时间也计入 TTL，隔夜的对话不复活。
                self._histories[str(cid)] = [
                    m for m in messages
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                ]
                # 把墙钟年龄换算回 monotonic 基准，TTL 从原对话的最后一句话起算。
                self._history_seen[str(cid)] = now_mono - age
                self._history_wall[str(cid)] = float(wall)

    def _prepare(
        self,
        text: str,
        *,
        raw: bool,
        conversation_id: Optional[str],
    ) -> tuple[List[Dict[str, Any]], bool, str]:
        """拼出要发给模型的消息，并说明这次要不要记历史。"""
        original = str(text or "")
        message = original
        if not raw and self.voice_prefix:
            message = f"{self.voice_prefix}\n{message}"
        use_history = bool(conversation_id) and not raw and self.history_turns > 0
        history = self._history_for(conversation_id or "") if use_history else []

        messages: List[Dict[str, Any]] = []
        if not raw:
            prompt = self.system_prompt() if callable(self.system_prompt) else self.system_prompt
            if prompt:
                messages.append({"role": "system", "content": str(prompt)})
        messages.extend(history)
        messages.append({"role": "user", "content": message})
        return messages, use_history, original

    def complete_only(
        self,
        text: str,
        *,
        raw: bool = False,
        conversation_id: Optional[str] = None,
    ) -> str:
        """只跑模型、**不动历史**。

        并发兜底（``FallbackChatClient.send``）两个端点同时发同一条消息，
        如果两边都调 ``send``，记忆会被写两遍、两份内容还不一样。
        所以并发路径用这个入口，等谁赢了再对赢家 ``remember`` 一次。
        """
        messages, _, _ = self._prepare(text, raw=raw, conversation_id=conversation_id)
        result = self._model.complete(messages)
        return str(result.get("content") or "").strip()

    def send(
        self,
        text: str,
        bot_id: Optional[str] = None,
        *,
        raw: bool = False,
        conversation_id: Optional[str] = None,
    ) -> str:
        """发一句话，返回模型回复文本。

        ``raw=True`` 时不加「口语回答」前缀、也不带会话历史 —— 意图路由要的是
        严格 JSON，历史里的闲聊会把模型带偏（与 swbot.py 的语义保持一致）。
        ``conversation_id`` 给了才维护上下文；历史里存的是**原始文本**，
        不带 ``voice_prefix``，否则每轮都重复一遍前缀白烧 token。
        """
        messages, use_history, original = self._prepare(
            text, raw=raw, conversation_id=conversation_id
        )
        result = self._model.complete(messages)
        reply = str(result.get("content") or "").strip()
        if use_history and reply:
            self._remember(conversation_id or "", original, reply)
        return reply

    def close(self) -> None:
        return None


class FallbackChatClient:
    """按顺序试多个端点，第一个给出非空回复的胜出。

    本地模型一挂（机器重启、进程没起来）直接退回公网中转，
    比让设备干等 20 秒再报错强得多。

    2026-11-06 实测：本地 sglang 卡死时 ``/v1/models`` 还能回但 chat 一直不吐字，
    一次请求要 **20 秒超时 + 4.7 秒备用** = 24.7 秒，设备端听起来就是「死机」。
    所以失败过的端点进**冷却期**：45 秒内的后续请求直接跳过它，
    首句从 24.7s 降到 4.7s；冷却到期才重新给它一次机会。

    2026-11-06 下午再实测（245 的 sglang 恢复后，n=10 同一句）：
    本地 p50 **2.05s**、p90 **12.12s**、最坏 **32.90s**；备用 p50 3.28s、
    p90 3.90s、最坏 4.08s。中位数赢 1.2 秒，但十次里有三次掉到 3 秒以上 ——
    只在「报错」时切备用根本救不了这种**不报错只是慢**的长尾，
    用户听到的是「说到一半它没反应了」。
    所以改成**并发兜底**：主端点超过 ``hedge_after_seconds`` 还没回，
    就把同一条消息同时发给备端点，谁先给出非空回复用谁，另一个丢弃。
    最坏情况从「12s 超时 + 3.3s」压到「hedge 点 + 一个备用延迟」。
    """

    def __init__(
        self,
        clients: List["ChatLLMClient"],
        *,
        cooldown_seconds: float = 45.0,
        hedge_after_seconds: float = 3.0,
        total_budget_seconds: float = 8.0,
        hedge_cooldown_seconds: float = 20.0,
    ) -> None:
        if not clients:
            raise ValueError("至少要有一个端点")
        self.clients = clients
        self.base_url = " → ".join(client.base_url for client in clients)
        self.bot_hint = clients[0].model
        self.voice_prefix = clients[0].voice_prefix
        self.cooldown_seconds = float(cooldown_seconds)
        self.hedge_after_seconds = float(hedge_after_seconds)
        # 一整句回复的总预算。两个端点都卡住时，实测会一路等到
        # 「主 12s 超时 + 备用 12s 超时」= 24~32 秒，设备端就是一段死寂。
        # 桌宠场景超过 8 秒这句话就没意义了，宁可早点认输换一句人话。
        self.total_budget_seconds = float(total_budget_seconds)
        self.hedge_cooldown_seconds = float(hedge_cooldown_seconds)
        self._down_until: Dict[int, float] = {}
        # 连续失败次数：网络烂的端点不能每 45 秒就复活一次，否则每次都要
        # 陪它白等一个 hedge 周期。失败越多冷却越久，成功一次立刻归零。
        # 2026-11-06 实测 245 那台机器：TCP 握手 0.5~7.5 秒、四次里三次超时，
        # 属于分钟级抖动，45 秒的固定冷却根本盖不住。
        self._fail_streak: Dict[int, int] = {}
        # 每次调用最多同时打 len(clients) 个端点；复用线程池，避免每句都新建。
        self._executor = ThreadPoolExecutor(
            max_workers=max(2, len(clients)), thread_name_prefix="chat-hedge"
        )

    def _usable(self) -> List[int]:
        """当前可用的端点下标；全在冷却就清空冷却（不能让桌宠彻底哑掉）。"""
        now = time.monotonic()
        alive = [i for i in range(len(self.clients)) if self._down_until.get(i, 0.0) <= now]
        if not alive:
            self._down_until.clear()
            return list(range(len(self.clients)))
        return alive

    def _mark_down(self, index: int, error: BaseException) -> None:
        client = self.clients[index]
        streak = self._fail_streak.get(index, 0) + 1
        self._fail_streak[index] = streak
        # 45s → 90s → 180s → 300s 封顶：网络抖的端点越失败越靠边站。
        cooldown = min(self.cooldown_seconds * (2 ** (streak - 1)), 300.0)
        self._down_until[index] = time.monotonic() + cooldown
        print(
            f"[CHAT] {client.label} 失败：{error}"
            f"（连续 {streak} 次，{cooldown:.0f}s 内不再尝试）",
            flush=True,
        )

    def send(
        self,
        text: str,
        bot_id: Optional[str] = None,
        *,
        raw: bool = False,
        conversation_id: Optional[str] = None,
    ) -> str:
        usable = self._usable()
        record = bool(conversation_id) and not raw
        if len(usable) == 1 or self.hedge_after_seconds <= 0:
            return self._send_sequential(usable, text, raw, conversation_id, record)
        return self._send_hedged(usable, text, raw, conversation_id, record)

    def _soft_down(self, index: int) -> None:
        """hedge 一触发就说明它已经慢到没法用了，先靠边站一小会儿。

        2026-11-06 现场：主端点要 12 秒才超时，可它 1.5 秒就叫不来人。
        这 12 秒里每个新请求都还会再打它一次、再白等一个 hedge 周期，
        实测连续几句全卡在 6~8 秒。所以 hedge 命中即短期靠边，不等超时。
        不动 ``_fail_streak``：偶发一次的慢不该让它越退越远。
        """
        until = time.monotonic() + self.hedge_cooldown_seconds
        if self._down_until.get(index, 0.0) < until:
            self._down_until[index] = until

    def _discard(self, pending: Dict[Future, int]) -> None:
        """胜者已定，把输掉的请求挂个回调：它们迟到超时也要记进冷却。

        不挂这个回调的话，hedged 分支永远是「备用先回 → 直接返回」，
        主端点的 12 秒超时异常没人接 —— 于是它**永远进不了冷却期**，
        下一句照样先白等 3 秒才拉备用。245 掉线那一轮就是这么一句一句耗的。
        """
        for future, index in list(pending.items()):

            def _settle(fut: Future, idx: int = index) -> None:
                error = fut.exception()
                if error is not None:
                    self._mark_down(idx, error)

            future.add_done_callback(_settle)

    def _accept(
        self, index: int, reply: str, conversation_id: Optional[str], text: str, record: bool
    ) -> str:
        client = self.clients[index]
        self._down_until.pop(index, None)
        self._fail_streak.pop(index, None)
        if record:
            # 记忆永远挂在 clients[0] 上（history_text 也只读那一份）。
            # 写赢家就会分裂成两套上下文：切一次备用，前面聊过的事全丢。
            recorder = getattr(self.clients[0], "remember", None)
            if callable(recorder):
                recorder(conversation_id or "", text, reply)
        if index > 0:
            print(
                f"[CHAT] 已切到备用 {client.label}（{client.base_url}）",
                flush=True,
            )
        return reply

    def _send_sequential(
        self,
        usable: List[int],
        text: str,
        raw: bool,
        conversation_id: Optional[str],
        record: bool,
    ) -> str:
        errors: List[str] = []
        for index in usable:
            client = self.clients[index]
            try:
                reply = client.complete_only(
                    text, raw=raw, conversation_id=conversation_id
                )
            except (ChatModelError, OSError, ValueError) as error:
                errors.append(f"{client.label}: {error}")
                self._mark_down(index, error)
                continue
            if reply and reply.strip():
                return self._accept(index, reply, conversation_id, text, record)
            errors.append(f"{client.label}: 空回复")
        raise ChatLLMError("；".join(errors) or "所有端点都没有回复")

    def _send_hedged(
        self,
        usable: List[int],
        text: str,
        raw: bool,
        conversation_id: Optional[str],
        record: bool,
    ) -> str:
        """主端点打头，超时即加发备端点，先给出非空回复的胜出。

        线程池里输掉的那个请求不会被取消（HTTP 已经在路上），
        它的结果直接被丢掉 —— 多花一次 token，换掉十几秒的干等，值。
        但它的**失败**要留个尾巴：见 ``_discard``，否则掉线的主端点
        永远进不了冷却，每句都要先白等一个 hedge 周期。
        """
        errors: List[str] = []
        queue = list(usable)
        pending: Dict[Future, int] = {}

        def launch(index: int) -> None:
            pending[
                self._executor.submit(
                    self.clients[index].complete_only,
                    text,
                    raw=raw,
                    conversation_id=conversation_id,
                )
            ] = index

        launch(queue.pop(0))
        hedge_at = time.monotonic() + self.hedge_after_seconds
        deadline = time.monotonic() + self.total_budget_seconds
        while pending or queue:
            now = time.monotonic()
            if pending and now >= deadline:
                # 到预算了还没人要给出回复：认输，别再拖成 24 秒的死寂。
                self._discard(pending)
                raise ChatLLMError(
                    "；".join(errors + ["等待超时"]) or "等待超时"
                )
            if not pending:
                # 主端点已经失败/空回复，剩下的一次性全发，别再等 hedge 点了。
                while queue:
                    launch(queue.pop(0))
                hedge_at = now + 30.0
            timeout = max(0.0, min(hedge_at, deadline) - time.monotonic())
            done, _ = wait(list(pending), timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                # 到点还没结果：把还没发出去的端点都放出去，之后纯等先回者。
                # 同时让这个端点先靠边 —— 它已经慢到不值得每句都白等一次。
                for index in list(pending.values()):
                    self._soft_down(index)
                while queue:
                    launch(queue.pop(0))
                hedge_at = time.monotonic() + 30.0
                continue
            for future in done:
                index = pending.pop(future)
                try:
                    reply = future.result()
                except (ChatModelError, OSError, ValueError) as error:
                    errors.append(f"{self.clients[index].label}: {error}")
                    self._mark_down(index, error)
                    continue
                if reply and reply.strip():
                    self._discard(pending)
                    return self._accept(index, reply, conversation_id, text, record)
                errors.append(f"{self.clients[index].label}: 空回复")
        raise ChatLLMError("；".join(errors) or "所有端点都没有回复")

    def history_text(self, conversation_id: str, max_chars: int = 700) -> str:
        # 历史永远挂在第一个端点上：切备用时不该让记忆分裂成两份。
        getter = getattr(self.clients[0], "history_text", None)
        return getter(conversation_id, max_chars) if callable(getter) else ""

    def remember(self, conversation_id: str, user_text: str, reply: str) -> None:
        recorder = getattr(self.clients[0], "remember", None)
        if callable(recorder):
            recorder(conversation_id, user_text, reply)

    def close(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 - 关闭路径保持安静
            pass
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭路径保持安静
                pass


def _read_client(
    environ: Mapping[str, str], prefix: str, label: str
) -> Optional[ChatLLMClient]:
    base_url = str(environ.get(f"{prefix}BASE_URL") or "").strip()
    model = str(environ.get(f"{prefix}MODEL") or "").strip()
    if not base_url or not model:
        return None
    api_key = str(environ.get(f"{prefix}API_KEY") or "").strip() or None
    try:
        timeout = float(environ.get("HUB_CHAT_TIMEOUT") or 20.0)
    except ValueError:
        timeout = 20.0
    try:
        max_tokens = int(environ.get("HUB_CHAT_MAX_TOKENS") or 300)
    except ValueError:
        max_tokens = 300
    # 环境变量里显式给了人设就用它（空串 = 不要人设）；没配就走默认人设 + 身份。
    system_prompt: Optional[Any] = environ.get("HUB_CHAT_SYSTEM_PROMPT")
    if system_prompt is not None:
        system_prompt = str(system_prompt)
    # 历史落盘路径：只有主模型（prefix == "HUB_CHAT_"）挂载，
    # 记忆统一记在这一份上（FallbackChatClient 也是只读 clients[0] 的历史）。
    history_path = None
    if prefix == "HUB_CHAT_":
        history_path = Path(
            str(environ.get("HUB_CHAT_HISTORY_PATH") or ".data/pet_chat_history.json")
        )
    return ChatLLMClient(
        base_url,
        model,
        api_key,
        timeout_seconds=timeout,
        max_tokens=max_tokens or None,
        system_prompt=system_prompt,
        label=label,
        history_path=history_path,
    )


def build_chat_client_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Any]:
    """按 HUB_CHAT_* 装配客户端；没配主用就返回 None（调用方继续用别的后端）。"""
    source = dict(os.environ if environ is None else environ)
    primary = _read_client(source, "HUB_CHAT_", "主模型")
    if primary is None:
        return None
    fallback = _read_client(source, "HUB_CHAT_FALLBACK_", "备用模型")
    if fallback is None:
        return primary
    try:
        hedge_after = float(source.get("HUB_CHAT_HEDGE_AFTER") or 3.0)
    except ValueError:
        hedge_after = 3.0
    try:
        budget = float(source.get("HUB_CHAT_BUDGET") or 8.0)
    except ValueError:
        budget = 8.0
    return FallbackChatClient(
        [primary, fallback],
        hedge_after_seconds=hedge_after,
        total_budget_seconds=budget,
    )
