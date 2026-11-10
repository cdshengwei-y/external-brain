"""语音门控与点歌查询抽取的单元测试。

两件事都来自实测翻车：

1. 房间里的闲谈（「两台一米八三，各两吨重量」）被 VAD 当成指令，
   中枢回了「嗯，我听着呢」——听起来像它在插话；
2. 「我要听单字一个枫，枫叶的枫，周杰伦的枫」整句被拿去搜索，
   搜出《枫 - 全网找歌君》，把翻唱当原唱放。
"""

from __future__ import annotations

import unittest

from agent_gateway.portable import (
    PortableRequestService,
    _artist_matches,
    _song_query,
    _split_song_query,
    voice_gate_allows,
)


class SongQueryTest(unittest.TestCase):
    def test_single_char_title_with_artist(self) -> None:
        self.assertEqual(
            _song_query("单字一个枫，枫叶的枫，周杰伦的枫"), "枫 周杰伦"
        )

    def test_quotes_from_asr_are_stripped(self) -> None:
        self.assertEqual(
            _song_query("单字一个“枫”，枫叶的“枫”，周杰伦的“枫”。"), "枫 周杰伦"
        )

    def test_plain_title(self) -> None:
        self.assertEqual(_song_query("稻香"), "稻香")
        self.assertEqual(_song_query("《晴天》"), "晴天")

    def test_artist_prefix(self) -> None:
        self.assertEqual(_song_query("周杰伦的稻香"), "稻香 周杰伦")

    def test_music_action_accepts_bare_song_name_description(self) -> None:
        action = PortableRequestService._music_action(
            "单字一个“枫”，枫叶的“枫”，周杰伦的“枫”。", False
        )
        self.assertEqual(action, ("music_play", "枫 周杰伦"))

    def test_music_action_keeps_trigger_forms(self) -> None:
        self.assertEqual(
            PortableRequestService._music_action("我要听单字一个枫，枫叶的枫，周杰伦的枫", False),
            ("music_play", "枫 周杰伦"),
        )
        self.assertEqual(
            PortableRequestService._music_action("来一首七里香", False),
            ("music_play", "七里香"),
        )
        self.assertEqual(
            PortableRequestService._music_action("我想听周杰伦的稻香", False),
            ("music_play", "稻香 周杰伦"),
        )

    def test_negative_sentence_is_not_a_song_request(self) -> None:
        self.assertEqual(PortableRequestService._music_action("不要听了", False)[0], "music_stop")

    def test_split_query(self) -> None:
        self.assertEqual(_split_song_query("枫 周杰伦"), ("枫", "周杰伦"))
        self.assertEqual(_split_song_query("稻香"), ("稻香", ""))

    def test_artist_alias(self) -> None:
        self.assertTrue(_artist_matches("Jay", "周杰伦"))
        self.assertTrue(_artist_matches("周杰伦.、街道办GDC", "周杰伦"))
        self.assertFalse(_artist_matches("全网找歌君", "周杰伦"))


class VoiceGateTest(unittest.TestCase):
    def test_ambient_talk_is_dropped(self) -> None:
        for text in (
            "两台一米八三，各两吨重量。",
            "我觉得这个可能，他都。",
            "是吧？对啊。",
            "好的，好的，好的。",
        ):
            self.assertFalse(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_intent_word_always_passes(self) -> None:
        for text in ("我要听枫", "音量调到2", "打开喷泉", "退下吧", "暂停", "几点了"):
            self.assertTrue(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_conversation_window_allows_chat(self) -> None:
        # 20 秒前刚交互过：允许闲聊
        self.assertTrue(voice_gate_allows("你觉得呢", 80.0, 45.0, 100.0))
        # 90 秒前：窗口已关，没意图词就丢
        self.assertFalse(voice_gate_allows("你觉得呢", 10.0, 45.0, 100.0))

    def test_complete_question_passes_without_intent_word(self) -> None:
        """2026-11-06：语音链路修好后，准确识别出的问句被门控误杀。

        设备上传 2.31 秒录音，ASR 给出「成都今天的温度怎么样？」—— 一个字都
        没错，但句子不含「天气」「几度」这类意图词，门控当成环境闲聊丢掉，
        用户看到的是「说了没反应」。
        """
        for text in (
            "成都今天的温度怎么样？",
            "这附近有什么好吃的",
            "你为什么不动了",
            "明天会不会下雨",
            "现在几点了",  # 有意图词，也走问句分支
        ):
            self.assertTrue(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_short_filler_with_question_mark_still_dropped(self) -> None:
        # 带问号的语气词不是提问：字数不够就不认。
        for text in ("是吧？", "对吧？", "是吧？对啊。", "好吗？"):
            self.assertFalse(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_short_question_word_passes(self) -> None:
        """2026-11-06：四字问句被 6 字门槛拦掉。

        现场日志：「维斯。」唤醒成功，紧接着说「为啥不讲？」被 [GATE] 丢掉。
        疑问词已经足够是提问信号，不该再拿长度否决。
        """
        for text in ("为啥不讲？", "你去吗", "几点啊", "能听见吗", "吃了吗"):
            self.assertTrue(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_imperative_without_intent_word_passes(self) -> None:
        """祈使句既没有疑问词也没有问号，只能靠意图词兜。

        现场：唤醒后说「给我讲个笑话」被 [GATE] 丢掉，用户体感是「只听懂
        唤醒和结束」。
        """
        for text in ("给我讲个笑话。", "讲个故事", "说个绕口令", "陪我聊会儿"):
            self.assertTrue(voice_gate_allows(text, 0.0, 45.0, 100.0), text)

    def test_sentence_fragments_still_dropped(self) -> None:
        # 修正前的实测残片：长度够但既无意图词也无疑问词，仍然丢弃。
        for text in (
            "然后，然后拍一下，然后就。",
            "这，这是大概是一天。",
            "两台一米八三，各两吨重量。",
        ):
            self.assertFalse(voice_gate_allows(text, 0.0, 45.0, 100.0), text)


class ExitPhraseTest(unittest.TestCase):
    """「退下吧」必须能退下去，否则屏幕永远亮着。

    2026-11-06：用户说「行，那你退下吧」，ASR 给出「行的，那你对去吧。」——
    意图词没命中，中枢当闲聊回了一句「好，那我先忙去了」，没下发
    session_action=sleep。设备只在收到 sleep 时才熄屏，于是「退下了但屏还亮着」。
    """

    def test_confused_transcripts_still_exit(self) -> None:
        for text in (
            "行的，那你对去吧。",  # 实测原句，首字退→对、下→去
            "退下吧",
            "跪下吧",
            "推下吧",
            "退下把",
            "你可以休息了",
            "进入待机",
            "你在待命",
        ):
            self.assertTrue(PortableRequestService._is_exit_phrase(text), text)

    def test_screen_off_phrases_exit(self) -> None:
        """「你先关闭吧」是让桌宠熄屏，不是关某个外设。

        2026-11-06 实测：这句被模型判成 fountain_stop，回了
        「音乐喷泉已经关掉了」，而喷泉根本没打开过。
        """
        for text in ("你先关闭吧。", "你先关掉吧。", "关屏", "熄屏吧", "屏幕关掉"):
            self.assertTrue(PortableRequestService._is_exit_phrase(text), text)

    def test_other_targets_are_not_screen_off(self) -> None:
        # 句子里点了具体对象，就不该当熄屏，让给对应插件。
        for text in ("关闭喷泉", "把音乐关掉", "关闭音量", "灯关掉", "关掉音乐喷泉"):
            self.assertFalse(PortableRequestService._is_exit_phrase(text), text)

    def test_normal_speech_is_not_exit(self) -> None:
        for text in (
            "这附近有什么好吃的",
            "我退后一步看",
            "对不起，我错了",
            "对，下面我们开始",
            "今天天气不错",
            "我们讨论一下这个方案",
        ):
            self.assertFalse(PortableRequestService._is_exit_phrase(text), text)


class WakeChainCommandTest(unittest.TestCase):
    """剥掉唤醒词后剩下的残片，不能都当成指令。

    2026-11-06 现场故障：ASR 把「贾维斯」写成「夏威斯家。」，剥词只剩一个
    「家」，被当成连锁指令重走管线，走到半路没落地 → 请求永远停在
    transcribing → 设备无限轮询同一条请求，用户说「待命之后叫不醒」。
    """

    def test_single_char_remainder_is_not_a_command(self) -> None:
        for text in ("夏威斯家。", "贾维斯家。", "查维斯家"):
            remainder = PortableRequestService._strip_wake_phrase(text)
            self.assertFalse(
                PortableRequestService._is_chain_command(remainder),
                f"{text!r} 剥成 {remainder!r} 不该算指令",
            )

    def test_pure_wake_has_empty_remainder(self) -> None:
        for text in ("贾维斯", "夏威斯", "贾维斯。"):
            remainder = PortableRequestService._strip_wake_phrase(text)
            self.assertFalse(
                PortableRequestService._is_chain_command(remainder), repr(text)
            )

    def test_real_chain_command_still_works(self) -> None:
        for text, expected in (
            ("贾维斯今天几号", "今天几号"),
            ("贾维斯，放首歌", "放首歌"),
            ("贾维斯，成都天气怎么样", "成都天气怎么样"),
        ):
            remainder = PortableRequestService._strip_wake_phrase(text)
            self.assertEqual(remainder, expected)
            self.assertTrue(
                PortableRequestService._is_chain_command(remainder), repr(text)
            )


class ExitPhraseTest(unittest.TestCase):
    """「X 下吧」不等于收口指令 —— 误判一次桌宠就当场熄屏。

    2026-11-06 第四轮实测：用户说「你配一下吧。」，中枢判成收口并下发熄屏。
    原来的规则是「≤5 字 + 以 下吧 结尾」，于是「等一下吧」「你放下吧」
    「你配一下吧」全都会让桌宠睡过去 —— 「一下」是最普通的口语，不是退下。
    """

    def test_real_exit_phrases(self) -> None:
        for text in ("你退下吧。", "退下吧", "行的，那你对去吧。", "你跪下吧", "可以休息了"):
            self.assertTrue(
                PortableRequestService._is_exit_phrase(text), repr(text)
            )

    def test_ordinary_yixiaba_is_not_exit(self) -> None:
        for text in ("等一下吧", "你配一下吧。", "你放下吧", "你先坐一下吧"):
            self.assertFalse(
                PortableRequestService._is_exit_phrase(text), repr(text)
            )


if __name__ == "__main__":
    unittest.main()
