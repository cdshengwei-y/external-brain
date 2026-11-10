"""天气快答：查结构化接口，不走模型、也不走"联网搜索"。

为什么不接通用搜索：实测这台机器的网络里，DuckDuckGo 直连 10 秒无响应，
Bing 和百度都是 302 反爬跳转。真正的通用搜索要么上付费 API（SerpAPI /
Brave / Tavily，按次计费），要么抓搜索结果页再解析 HTML —— 页面一改版就
断，还得处理验证码。天气不一样：它有免费的结构化接口，0.2 秒返回，
比"让模型转述搜索结果"更准，也不会把它听成别的句子。

数据源 Open-Meteo，无需 API Key。城市名走它的 geocoding，支持中文
（「成都」→ 30.57, 104.07）。坐标和结果都缓存，连着问两次不会打两次网络。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 默认城市，可用 PET_CITY 覆盖。桌宠在成都，问「天气」没说城市就按这个报。
DEFAULT_CITY = "成都"


def default_city() -> str:
    """每次现读环境变量：plugin.env 是启动时注入的，但测试里会临时改。"""
    return (os.environ.get("PET_CITY") or "").strip() or DEFAULT_CITY

# 天气半小时才会变一次，缓存 10 分钟够用，也挡住了连问两次的场景。
CACHE_SECONDS = 600
HTTP_TIMEOUT = 4.0

# 常见城市的坐标直接写死：geocoding 一次要 1.4 秒，而这十几个城市占了绝大多数
# 问句。命中就省掉一次网络往返，没命中的走 geocoding 兜底。
_CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "成都": (30.5728, 104.0668),
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "重庆": (29.5630, 106.5516),
    "西安": (34.3416, 108.9398),
    "武汉": (30.5928, 114.3055),
    "南京": (32.0603, 118.7969),
    "天津": (39.3434, 117.3616),
    "苏州": (31.2989, 120.5853),
    "郑州": (34.7466, 113.6254),
    "长沙": (28.2282, 112.9388),
    "青岛": (36.0671, 120.3826),
    "济南": (36.6512, 117.1201),
    "合肥": (31.8206, 117.2272),
    "福州": (26.0745, 119.2965),
}

# WMO weather code → 能念出来的中文。分档粗一点，TTS 念长句反而难懂。
_WMO = {
    0: "晴",
    1: "晴间少云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "有雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "雷阵雨伴冰雹",
}

# 句子里出现这些词才可能是天气问句。宁可少认，也不能把「音量多少度」当成天气。
_WEATHER_WORDS = (
    "天气",
    "气温",
    "温度",
    "下雨",
    "带伞",
    "雨伞",
    "淋雨",
    "有雨",
    "冷不冷",
    "热不热",
    "穿什么",
    "穿多少",
    "几度",
    "多少度",
)
# 这些问句已经由设备插件接管（音量和声音），天气这边必须先让开。
_NOT_WEATHER_WORDS = ("音量", "声音", "大点声", "小点声", "大声", "小声", "喷泉")

_TIME_WORDS = {
    "今天": 0,
    "今日": 0,
    "现在": 0,
    "当前": 0,
    "明天": 1,
    "明日": 1,
    "明儿": 1,
}

# 「成都天气」「北京的天气」「杭州气温」——取天气词前面那截当城市名。
# 2026-11-06 补「几度 / 多少度」：原来「北京今天几度」不含天气/气温/温度，
# 城市整段抓不到，直接按默认城市成都回答了。
_CITY_PATTERN = re.compile(
    r"([\u4e00-\u9fa5]{2,6}?)(?:的)?(?:今天|明天|现在|今日|明日)?"
    r"(?:天气|气温|温度|几度|多少度|几摄氏度)"
)
_CITY_STOPWORDS = (
    "今天",
    "明天",
    "现在",
    "今日",
    "明日",
    "明儿",
    "今儿",
    "外面",
    "这边",
    "那边",
    "这里",
    "那里",
    "当地",
    "我这",
    "你那",
    "什么",
    "怎么样",
    "多少",
)

# 问句里的动词会被城市正则一起吃进去（「能查到锦江区」），按前缀逐层剥掉。
# 长的排前面，免得「查一下」先被「查」削成「一下成都」。
_CITY_PREFIX_NOISE = (
    "能不能", "能否", "你可以", "你能", "能帮我", "帮我", "给我", "替我",
    "想知道", "我想", "我要", "查一下", "查查", "查询", "查到", "查",
    "看一下", "看看", "告诉我", "问一下", "问问", "知道", "一下", "可以", "能",
)
# 单字介词/动词前缀，剥完还剩两字以上才认（「在成都」→「成都」，「成都」不动）。
# 「我」也在这里：「你能帮我查一下成都的天气」残留「我查一下成都」。
_CITY_LEADING_CHARS = "在到去问查看我"

_coord_cache: Dict[str, Optional[Tuple[float, float, str]]] = {}
_weather_cache: Dict[str, Tuple[float, str]] = {}


def _get_json(url: str, params: Dict[str, object]) -> Optional[dict]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": "sparkbot-hub/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def lookup_city(city: str) -> Optional[Tuple[float, float, str]]:
    """城市名 → (纬度, 经度, 规范名)。查不到返回 None。"""
    name = str(city or "").strip()
    if not name:
        return None
    if name in _coord_cache:
        return _coord_cache[name]
    preset = _CITY_COORDS.get(name)
    if preset is not None:
        resolved = (preset[0], preset[1], name)
        _coord_cache[name] = resolved
        return resolved
    payload = _get_json(
        GEOCODE_URL,
        {"name": name, "count": 1, "language": "zh", "format": "json"},
    )
    results = (payload or {}).get("results") or []
    if not results:
        _coord_cache[name] = None
        return None
    item = results[0]
    resolved = (
        float(item["latitude"]),
        float(item["longitude"]),
        str(item.get("name") or name),
    )
    _coord_cache[name] = resolved
    return resolved


def _weather_code_text(code: object) -> str:
    try:
        return _WMO.get(int(code), "天气不明")
    except (TypeError, ValueError):
        return "天气不明"


def _comfort(sensible: float) -> str:
    if sensible <= 5:
        return "挺冷的，多穿点"
    if sensible <= 12:
        return "偏冷，穿厚一点"
    if sensible <= 18:
        return "有点凉，外套够了"
    if sensible <= 26:
        return "挺舒服"
    if sensible <= 32:
        return "偏热，少穿点"
    return "很热，注意别中暑"


# 问句侧重点：用户问「会不会下雨」就别念全量模板（实测被吐槽）。
# 问雨只答雨，问温度只答温度，泛问「天气怎么样」才给全量。
_RAIN_WORDS = ("下雨", "带伞", "雨伞", "淋雨", "有雨", "降水")
_TEMP_WORDS = ("几度", "多少度", "气温", "温度", "冷不冷", "热不热", "穿什么", "穿多少")


def _reply_focus(text: str) -> str:
    normalized = "".join(str(text or "").split())
    if any(word in normalized for word in _RAIN_WORDS):
        return "rain"
    if any(word in normalized for word in _TEMP_WORDS):
        return "temperature"
    return "general"


# WMO 代码落在这些区间 = 现在真的在降水（毛毛雨/雨/冻雨/阵雨/雷雨）。
_RAINING_CODES = tuple(
    list(range(51, 68)) + [80, 81, 82, 95, 96, 99]
)


def _rain_reply(
    name: str,
    prefix: str,
    code: Optional[int],
    code_text: str,
    rain: Optional[int],
) -> str:
    if code is not None and code in _RAINING_CODES:
        return f"{name}现在正下着{code_text}，出门带把伞。"
    if rain is None:
        return f"{name}{prefix}降水概率查不到，看天色是{code_text}。"
    if rain >= 70:
        return f"{name}{prefix}会下雨，概率 {rain}%，记得带伞。"
    if rain >= 40:
        return f"{name}{prefix}可能下雨，概率 {rain}%，带把伞稳妥。"
    return f"{name}{prefix}基本不会下雨，概率才 {rain}%，不用带伞。"


def _temperature_reply(
    name: str,
    day_offset: int,
    data: Dict[str, object],
) -> Optional[str]:
    temperature = data.get("temperature")
    sensible = data.get("sensible")
    prefix = "今天" if day_offset == 0 else "明天"
    if day_offset == 0 and isinstance(temperature, float):
        text = f"{name}现在 {int(round(temperature))} 度"
        if isinstance(sensible, float) and abs(sensible - temperature) >= 2.0:
            text += f"，体感 {int(round(sensible))} 度"
        if isinstance(sensible, float):
            text += f"，{_comfort(sensible)}"
        return text + "。"
    high = data.get("high")
    low = data.get("low")
    if isinstance(high, int) and isinstance(low, int):
        return f"{name}{prefix} {low} 到 {high} 度。"
    return None


def _extract_forecast(payload: dict, day_offset: int) -> Optional[Dict[str, object]]:
    """API 返回 → 本次回复要用的字段。缺关键字段返回 None。"""
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    index = min(day_offset, max(len(highs) - 1, 0)) if highs else 0

    try:
        temperature = float(current.get("temperature_2m"))
        sensible = float(current.get("apparent_temperature"))
        humidity = int(float(current.get("relative_humidity_2m")))
    except (TypeError, ValueError):
        return None
    raw_code = current.get("weather_code")
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None

    high = None
    low = None
    rain = None
    try:
        if highs:
            high = int(round(float(highs[index])))
        if lows:
            low = int(round(float(lows[index])))
        if rains and rains[index] is not None:
            rain = int(round(float(rains[index])))
    except (IndexError, TypeError, ValueError):
        pass

    return {
        "temperature": temperature,
        "sensible": sensible,
        "humidity": humidity,
        "code": code,
        "code_text": _weather_code_text(code),
        "high": high,
        "low": low,
        "rain": rain,
    }


def fetch_weather(
    city: str = DEFAULT_CITY,
    day_offset: int = 0,
    umbrella: bool = False,
    focus: str = "general",
    force: bool = False,
) -> Optional[str]:
    """返回一句能直接念的话；查不到返回 None（调用方退回模型）。

    focus：rain=问雨只答雨，temperature=问温度只答温度，general=全量模板。
    缓存的是**数据**而不是成句 —— 同一份数据三种问法各答各的，不用打三次网。
    """
    resolved = lookup_city(city)
    if resolved is None:
        return None
    latitude, longitude, name = resolved
    cache_key = f"{name}:{day_offset}"
    cached = _weather_cache.get(cache_key)
    if cached and not force and time.time() - cached[0] < CACHE_SECONDS:
        data = cached[1]
    else:
        payload = _get_json(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max",
                "timezone": "Asia/Shanghai",
                "forecast_days": max(2, day_offset + 1),
            },
        )
        if not payload:
            return None
        data = _extract_forecast(payload, day_offset)
        if data is None:
            return None
        _weather_cache[cache_key] = (time.time(), data)

    prefix = "今天" if day_offset == 0 else "明天"
    rain = data.get("rain")
    code = data.get("code")
    if not isinstance(code, int):
        code = None

    if focus == "rain":
        return _rain_reply(name, prefix, code, str(data.get("code_text")), rain)
    if focus == "temperature":
        reply = _temperature_reply(name, day_offset, data)
        if reply:
            return reply
        # 温度字段凑不齐就退回全量，总得说点什么

    parts = [f"{name}{prefix}{data.get('code_text')}"]
    if day_offset == 0 and isinstance(data.get("temperature"), float):
        parts.append(
            f"现在 {int(round(data['temperature']))} 度，"
            f"体感 {int(round(data['sensible']))} 度"  # type: ignore[arg-type]
        )
    if isinstance(data.get("high"), int) and isinstance(data.get("low"), int):
        parts.append(f"{data['low']} 到 {data['high']} 度")
    if isinstance(rain, int):
        parts.append(f"降水概率 {rain}%")
    if isinstance(data.get("humidity"), int) and data["humidity"] >= 85:
        parts.append("挺潮的")
    if umbrella and isinstance(rain, int):
        parts.append("建议带把伞" if rain >= 40 else "不用带伞")
    if day_offset == 0 and isinstance(data.get("sensible"), float):
        parts.append(_comfort(data["sensible"]))  # type: ignore[arg-type]
    return "，".join(parts) + "。"


def match_weather_question(text: str) -> Optional[Dict[str, object]]:
    """判断是不是天气问句，是就返回 {'city':…, 'day_offset':…}。

    只认「天气/气温/几度/下雨/带伞」这类明确说法；音量、声音相关的问句让开，
    它们归设备插件管。
    """
    normalized = "".join(str(text or "").split())
    if not normalized:
        return None
    if any(word in normalized for word in _NOT_WEATHER_WORDS):
        return None
    if not any(word in normalized for word in _WEATHER_WORDS):
        return None

    day_offset = 0
    for word, offset in _TIME_WORDS.items():
        if word in normalized:
            day_offset = offset
            break

    city = default_city()
    match = _CITY_PATTERN.search(normalized)
    if match:
        candidate = _strip_city_noise(match.group(1))
        if len(candidate) >= 2 and not any(stop in candidate for stop in _CITY_STOPWORDS):
            city = candidate
    return {"city": city, "day_offset": day_offset}


def _strip_city_noise(candidate: str) -> str:
    """剥掉问句动词，只留地名。

    2026-11-06 实测：「哦，你能查到锦江区的天气吗？」被解析成
    ``city='能查到锦江区'``，拿这个去查地理编码必然失败，最后回落到模型，
    用户听到的是「我这边看不到实时天气数据」—— 明明本地就能答。
    正则只能按「天气前面那截汉字」切，动词混进来是常态，这里逐层剥掉。
    """
    text = candidate
    while True:
        for prefix in _CITY_PREFIX_NOISE:
            if text.startswith(prefix) and len(text) - len(prefix) >= 2:
                text = text[len(prefix):]
                break
        else:
            stripped = text.lstrip(_CITY_LEADING_CHARS)
            if stripped != text and len(stripped) >= 2:
                text = stripped
                continue
            return text


def answer_weather(text: str) -> Optional[str]:
    """一句话入口：不是天气问句返回 None。

    按问什么答什么：「会不会下雨」只答雨，「几度」只答温度，
    泛问「天气怎么样」才给全量模板。
    """
    spec = match_weather_question(text)
    if spec is None:
        return None
    normalized = "".join(str(text or "").split())
    city = str(spec.get("city") or default_city())
    day_offset = int(spec.get("day_offset") or 0)
    umbrella = "带伞" in normalized or "下雨" in normalized
    focus = _reply_focus(normalized)
    reply = fetch_weather(
        city=city, day_offset=day_offset, umbrella=umbrella, focus=focus
    )
    # 「锦江区」「双流县」这类区县级地名，open-meteo 的地理编码基本查不到
    # （实测「锦江区」→ None，「北京」正常）。查不到就退回默认城市再问一次：
    # 桌宠在成都，问锦江区答成都的天气仍然有用；直接返回 None 会让整句掉进
    # 模型，用户听到的是「我这边看不到实时天气数据」。
    if reply is None and city != default_city() and city.endswith(
        ("区", "县", "镇", "乡", "街道")
    ):
        reply = fetch_weather(
            city=default_city(), day_offset=day_offset, umbrella=umbrella, focus=focus
        )
    return reply


def start_warmer(interval_seconds: float = 240.0) -> None:
    """后台定时刷新默认城市今明两天的天气。

    这个接口一次要 1.1 秒（TLS 握手 + 海外网络），可用户问的是「现在几度」，
    让他等一秒没道理。预先刷好，问句就变成 0ms —— 和问时间一样的体感。
    只在 run.py 里启动，测试进程不会去打网络。
    """
    city = default_city()

    def loop() -> None:
        while True:
            for offset in (0, 1):
                try:
                    fetch_weather(city, day_offset=offset, force=True)
                except Exception:
                    pass
            time.sleep(max(60.0, interval_seconds))

    threading.Thread(target=loop, name="weather-warmer", daemon=True).start()
