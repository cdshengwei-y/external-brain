# ESP-SparkBot 桌宠硬件基准

## 工程对应关系

当前桌宠实物对应嘉立创工程：

- 工程：`/Users/jh-05/Desktop/ProPrj_ESP-SparkBot_2026-10-13.epro`；
- 项目：`随身设备`；
- 主板：`ESP_SparkBot_MainBoard_V1.2`；
- 主板原理图：`sch_esp_sparkbot_v1.2`；
- 主板 PCB：`PCB_ESP_SparkBot_V1.2`。

该工程中还存在子板和履带底盘两个 Board，不代表当前桌宠一定安装了履带底盘。当前上电验证的是主板及其连接的桌宠组合板。

## 实测芯片信息

| 项目 | 工程标注 | 实物读取结果 | 结论 |
|---|---|---|---|
| 主控 | U1：`ESP32-S3-WROOM-1-N8` | ESP32-S3，revision 0 | 芯片型号一致 |
| Flash | N8 标注通常表示 8MB | `32MB` | 以实测容量为准，工程物料标注需要保留差异记录 |
| PSRAM | 工程值未明确体现 | `约 8MB` | 已确认可用 |
| USB | U1 IO19/IO20 接 `D-/D+` | `/dev/cu.usbmodem13101` | 原生 USB JTAG/串口正常 |

## ESP32-S3 与音频编解码器 U2（ES8311）

| ESP32 GPIO | U1 引脚 | U2 引脚 | 信号 | 用途 |
|---:|---:|---:|---|---|
| GPIO45 | 26 | 2 | `MCLK` | 音频主时钟 |
| GPIO39 | 32 | 6 | `SCLK/DMIC_SCL` | 音频位时钟 |
| GPIO41 | 34 | 8 | `LRCK` | 音频左右声道时钟 |
| GPIO40 | 33 | 7 | `ASDOUT` | ES8311 输出到 ESP32 的音频数据 |
| GPIO42 | 35 | 9 | `DSDIN` | ESP32 输出到 ES8311 的音频数据 |
| GPIO4 | 4 | 19 | `CDATA` | ES8311 控制数据 |
| GPIO5 | 5 | 1 | `CCLK` | ES8311 控制时钟 |

## 麦克风、功放与显示

- U11：板上模拟麦克风，型号标注为 `B4013AM422-42`；
- U3/U12：麦克风板间连接器，对应 `MICP`、`MICN`、`ADCVREF` 以及子板侧的 `MICP_SUB`、`MICN_SUB`、`ADCVREF_SUB`；
- U5：`NS4150B` 功放；
- GPIO46 → U5 pin 1 `CTRL`，用于功放控制；
- U4：1.54 英寸 LCD；
- GPIO43、GPIO44、GPIO47 与 LCD 相关信号相连，不能作为普通测试 GPIO 随意拉高或拉低。

## 主板与底部电源/扩展板连接

主板 U8 与对应连接器 U9 的关键网络：

| U8 主板 | 网络 | U9 对应网络 | 说明 |
|---:|---|---|---|
| 1 | `ESP_3V3` | `VDD_3V3` | 3.3V |
| 2 | `GPIO4` | `IMU_SDA` | 复用网络，不能单独当普通 GPIO |
| 3 | `GPIO5` | `IMU_SCL` | 复用网络，不能单独当普通 GPIO |
| 4 | `GPIO48` | `SUB_GPIO48` | 子板信号 |
| 5 | `GPIO14` | `VBAT_ADC` | 电池电压检测 |
| 7 | `D+` | `USB_D+` | USB 数据正 |
| 8 | `D-` | `USB_D-` | USB 数据负 |
| 9 | `VOUT` | `VBUS` | 电源路径，禁止当普通信号测试 |
| 10-12 | `TP_GPIO1-3` | `TOUCH_BTN1-3` | 触摸/按键输入 |
| 13 | `GPIO38` | `SUB_GPIO38` | 子板信号 |
| 14 | `GPIO0` | `SUB_GPIO0` | 启动相关信号，禁止随意拉低 |

## 当前开发限制

1. 第一版安全固件已经验证 USB、ESP32-S3、PSRAM 和 Wi-Fi 扫描；
2. 音频测试可以使用上面的真实引脚表，但仍应先做 ES8311 初始化和短时采样，不要直接接入完整语音链路；
3. GPIO4/5 同时连接 ES8311 控制总线和板间扩展，GPIO0 参与启动，GPIO43/44/47 与 LCD 相关，不能作为随意的 LED/按键测试脚；
4. 不修改嘉立创原始工程，不根据 N8 字样覆盖实测 Flash 结论；
5. 下一步应先验证 ES8311 的 I2C/控制通信和 I2S 采样，再验证 NS4150B 播放。
