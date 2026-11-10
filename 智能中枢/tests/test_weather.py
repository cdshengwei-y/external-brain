import os
import unittest
from unittest.mock import patch

from agent_gateway import weather


FAKE_FORECAST = {
    "current": {
        "temperature_2m": 18.4,
        "apparent_temperature": 17.6,
        "relative_humidity_2m": 63,
        "weather_code": 1,
        "wind_speed_10m": 7.8,
    },
    "daily": {
        "temperature_2m_max": [21.2, 20.0],
        "temperature_2m_min": [12.1, 11.0],
        "precipitation_probability_max": [15, 80],
    },
}


class WeatherMatchTest(unittest.TestCase):
    def setUp(self):
        weather._weather_cache.clear()
        weather._coord_cache.clear()

    def test_plain_weather_question_matches(self):
        self.assertIsNotNone(weather.match_weather_question("今天天气怎么样"))
        self.assertIsNotNone(weather.match_weather_question("现在几度"))
        self.assertIsNotNone(weather.match_weather_question("要带伞吗"))

    def test_volume_question_does_not_match(self):
        """「音量多少度」必须让给设备插件，不能被天气抢走。"""
        self.assertIsNone(weather.match_weather_question("音量多少度"))
        self.assertIsNone(weather.match_weather_question("把声音调小一点"))

    def test_unrelated_text_does_not_match(self):
        self.assertIsNone(weather.match_weather_question("你叫什么名字"))
        self.assertIsNone(weather.match_weather_question("放一首稻香"))

    def test_city_is_taken_from_sentence(self):
        spec = weather.match_weather_question("北京天气怎么样")
        self.assertEqual(spec["city"], "北京")

    def test_city_falls_back_to_default(self):
        spec = weather.match_weather_question("外面天气怎么样")
        self.assertEqual(spec["city"], weather.default_city())

    def test_tomorrow_sets_day_offset(self):
        self.assertEqual(weather.match_weather_question("明天天气")["day_offset"], 1)
        self.assertEqual(weather.match_weather_question("今天天气")["day_offset"], 0)

    def test_city_strips_verbs(self):
        """2026-11-06：问句动词被吃进城市名。

        现场：「哦，你能查到锦江区的天气吗？」解析成 city='能查到锦江区'，
        地理编码必然查不到，最后回落到模型答「我看不到实时天气」。
        """
        self.assertEqual(
            weather.match_weather_question("哦，你能查到锦江区的天气吗？")["city"],
            "锦江区",
        )
        self.assertEqual(
            weather.match_weather_question("你能帮我查一下成都的天气")["city"], "成都"
        )

    def test_temperature_question_takes_city(self):
        """「北京今天几度」不含天气/气温/温度，原来直接按成都回答。"""
        self.assertEqual(weather.match_weather_question("北京今天几度")["city"], "北京")
        self.assertEqual(weather.match_weather_question("上海多少度")["city"], "上海")

    def test_default_city_reads_env(self):
        with patch.dict(os.environ, {"PET_CITY": "杭州"}):
            self.assertEqual(weather.default_city(), "杭州")
        with patch.dict(os.environ, {"PET_CITY": ""}):
            self.assertEqual(weather.default_city(), weather.DEFAULT_CITY)


class WeatherAnswerTest(unittest.TestCase):
    def setUp(self):
        weather._weather_cache.clear()
        weather._coord_cache.clear()

    def test_answer_reads_real_numbers(self):
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST):
            reply = weather.answer_weather("成都天气")
        self.assertIn("成都", reply)
        self.assertIn("18 度", reply)
        self.assertIn("12 到 21 度", reply)
        self.assertIn("降水概率 15%", reply)

    def test_district_falls_back_to_default_city(self):
        """区县级地名查不到时退回默认城市，而不是让整句掉进模型。

        现场：问「你能查到锦江区的天气吗」，open-meteo 的地理编码查不到
        「锦江区」，answer_weather 返回 None，模型只能答「看不到实时天气」。
        """
        real_lookup = weather.lookup_city

        def fake_lookup(name):
            return None if name == "锦江区" else real_lookup(name)

        with patch.object(weather, "lookup_city", side_effect=fake_lookup), patch.object(
            weather, "_get_json", return_value=FAKE_FORECAST
        ):
            reply = weather.answer_weather("哦，你能查到锦江区的天气吗？")
        self.assertIsNotNone(reply)
        self.assertIn(weather.default_city(), reply)

    def test_umbrella_tip_for_rainy_tomorrow(self):
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST):
            reply = weather.answer_weather("明天要带伞吗")
        self.assertIn("明天", reply)
        self.assertIn("概率 80%", reply)
        self.assertIn("带伞", reply)

    def test_no_umbrella_when_dry(self):
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST):
            reply = weather.answer_weather("今天要带伞吗")
        self.assertIn("不用带伞", reply)

    def test_rain_question_gets_short_answer(self):
        """问「会不会下雨」只答雨，不再念全量模板（2026-11-03 用户吐槽）。"""
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST):
            reply = weather.answer_weather("今天会不会下雨")
        self.assertIn("基本不会下雨", reply)
        self.assertNotIn("体感", reply)
        self.assertNotIn("外套", reply)

    def test_raining_now_answer(self):
        """当前 weather_code 在降水区间 → 直接说正在下雨。"""
        raining = {
            "current": dict(FAKE_FORECAST["current"], weather_code=61),
            "daily": FAKE_FORECAST["daily"],
        }
        with patch.object(weather, "_get_json", return_value=raining):
            reply = weather.answer_weather("成都会下雨吗")
        self.assertIn("正下着小雨", reply)

    def test_temperature_question_gets_short_answer(self):
        """问「几度」只答温度，不念降水。"""
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST):
            reply = weather.answer_weather("现在多少度")
        self.assertIn("18 度", reply)
        self.assertNotIn("降水", reply)

    def test_returns_none_when_network_fails(self):
        """查不到就返回 None，让上层退回模型，而不是硬编一句「看不到」。"""
        with patch.object(weather, "_get_json", return_value=None):
            self.assertIsNone(weather.answer_weather("成都天气"))

    def test_second_question_hits_cache(self):
        with patch.object(weather, "_get_json", return_value=FAKE_FORECAST) as fake:
            weather.answer_weather("成都天气")
            first = fake.call_count
            weather.answer_weather("成都天气")
            self.assertEqual(fake.call_count, first)


if __name__ == "__main__":
    unittest.main()
