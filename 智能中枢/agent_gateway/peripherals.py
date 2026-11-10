"""外设插件族：接真实硬件的意图插件都住这里。

与普通插件的区别：只声明「意图 → 外设命令」的映射（``actions`` 表），
执行（HTTP 调用、幂等、超时、错误话术）统一交给 :class:`ActionDispatcher`。
以后接新外设 = 照着 :class:`RecordingBeanPlugin` 写一个几十行的子类，
在 ``portable.py`` 的插件表里注册一行，Bot 那边不用改任何东西 ——
能力清单（catalog）会自动注入 Bot 的意图判定提示词。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .actions import ActionDispatcher, ActionSpec
from .intent import IntentResult, Plugin, PluginContext


class PeripheralPlugin(Plugin):
    """外设插件基类：声明 actions，执行交给分发器，回复用真实数据。"""

    actions: Mapping[str, ActionSpec] = {}
    # 注册时由 portable.py 注入；为 None 时一律答「还没配置」
    dispatcher: Optional[ActionDispatcher] = None

    def __init__(self, dispatcher: Optional[ActionDispatcher] = None):
        if dispatcher is not None:
            self.dispatcher = dispatcher

    def handle(self, intent: IntentResult, ctx: PluginContext) -> IntentResult:
        spec = self.actions.get(intent.intent)
        if spec is None:
            return IntentResult(
                intent=intent.intent,
                reply="这个操作我还没学会。",
                slots=intent.slots,
                source="peripheral",
            )
        outcome = self.dispatcher.execute(
            intent.intent, spec, intent.slots, ctx.request_id
        ) if self.dispatcher is not None else None
        if outcome is None or not outcome.ok:
            reply = outcome.user_reply if outcome is not None else (
                "外设控制还没配置好，缺命令接口的密钥。"
            )
            return IntentResult(
                intent=intent.intent,
                reply=reply,
                slots=intent.slots,
                source="peripheral",
            )
        if callable(spec.ok_reply):
            reply = spec.ok_reply(outcome.result)
        else:
            reply = str(spec.ok_reply or intent.reply or "好了。")
        return IntentResult(
            intent=intent.intent,
            reply=reply,
            slots=intent.slots,
            source="peripheral",
        )


def _stop_reply(result: Dict[str, Any]) -> str:
    """停止成功：设备给了刚完成的文件名就播出来，没给就简单确认。"""
    name = str(result.get("last_completed_file") or "").strip()
    if name:
        return f"好，录音停了，这次存成了 {name}。"
    return "好，录音停了。"


def _status_reply(result: Dict[str, Any]) -> str:
    """状态查询：真实数据照播；字段缺失就说没读全，不编数字。"""
    if not result:
        return "录音豆的状态我没读全，稍后再问一次吧。"
    recording = result.get("recording")
    parts: List[str] = []
    if recording is True:
        current = str(result.get("current_file") or "").strip()
        parts.append("录音豆正在录音" + (f"，当前文件 {current}" if current else ""))
    elif recording is False:
        parts.append("录音豆现在空闲，没在录音")
    free_mb = result.get("sd_free_mb")
    if isinstance(free_mb, (int, float)):
        parts.append(f"存储还剩 {int(free_mb)} MB")
    if not parts:
        return "录音豆的状态我没读全，稍后再问一次吧。"
    return "，".join(parts) + "。"


class RecordingBeanPlugin(PeripheralPlugin):
    """录音豆（bean_ye_1）：第一个接入统一分发器的外设。

    命令走 RK3588 命令 API → MQTT → 录音豆固件（AgentOS 协议，
    见录音豆仓库《交接文档_录音豆-AgentOS-MQTT.md》）。params 协议上
    必须为空，所以三个意图都不声明 slots、也不透传 Bot 给的 slots。
    """

    intents = ("record_start", "record_stop", "record_status")
    description = "控制录音豆开始/停止录音或查询状态"
    actions = {
        "record_start": ActionSpec(
            capability="record.start", ok_reply="开始录音了。"
        ),
        "record_stop": ActionSpec(
            capability="record.stop", ok_reply=_stop_reply
        ),
        "record_status": ActionSpec(
            capability="status.read", ok_reply=_status_reply, read_only=True
        ),
    }

    def catalog_lines(self) -> List[str]:
        # 覆盖默认实现：给 Bot 的判定提示词更明确的指令
        return [
            "- record_start：让录音豆开始录音；确认只说一句「开始录音了」",
            "- record_stop：让录音豆停止录音；确认只说一句",
            "- record_status：查录音豆的状态（在不在录音、存储还剩多少）",
        ]
