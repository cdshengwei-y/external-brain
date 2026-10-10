#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "config.h"

namespace {

WiFiClient wifi_client;
PubSubClient mqtt_client(wifi_client);
String topic_root = String("hub/v1/devices/") + HUB_DEVICE_ID;
String topic_commands = topic_root + "/commands";
String topic_manifest = topic_root + "/manifest";
String topic_availability = topic_root + "/availability";
String topic_state = topic_root + "/state";
String topic_events = topic_root + "/events";
String topic_command_acks = topic_root + "/command-acks";

unsigned long next_wifi_attempt = 0;
unsigned long next_mqtt_attempt = 0;
String indicator_color = "off";
String indicator_mode = "solid";

void publish_json(const String &topic, JsonDocument &document, bool retain = false)
{
    String payload;
    serializeJson(document, payload);
    mqtt_client.publish(topic.c_str(), payload.c_str(), retain);
}

void publish_availability(const char *status)
{
    StaticJsonDocument<96> document;
    document["status"] = status;
    publish_json(topic_availability, document, true);
}

void publish_manifest()
{
    StaticJsonDocument<256> document;
    document["device_id"] = HUB_DEVICE_ID;
    document["device_type"] = "recording_beam";
    document["protocol_version"] = "1.0";
    document["firmware_version"] = "replace-me";
    JsonArray capabilities = document["capabilities"].to<JsonArray>();
    capabilities.add("button.press");
    capabilities.add("indicator.set");
    publish_json(topic_manifest, document, true);
}

void publish_state()
{
    StaticJsonDocument<160> document;
    JsonObject indicator = document["indicator"].to<JsonObject>();
    indicator["color"] = indicator_color;
    indicator["mode"] = indicator_mode;
    publish_json(topic_state, document, true);
}

void publish_ack(const char *command_id, const char *state, const char *error = nullptr)
{
    StaticJsonDocument<192> document;
    document["command_id"] = command_id;
    document["state"] = state;
    if (error != nullptr) {
        document["error"] = error;
    }
    publish_json(topic_command_acks, document);
}

void apply_indicator(const char *color, const char *mode)
{
    // TODO: 按录音豆真实 LED 电路实现颜色和模式控制。
    indicator_color = color;
    indicator_mode = mode;
}

void handle_command(const byte *payload, unsigned int length)
{
    StaticJsonDocument<512> document;
    DeserializationError error = deserializeJson(document, payload, length);
    if (error) {
        return;
    }

    const char *command_id = document["command_id"] | "";
    const char *type = document["type"] | "command";
    if (command_id[0] == '\0') {
        return;
    }
    if (strcmp(type, "cancel") == 0) {
        publish_ack(command_id, "cancelled");
        return;
    }
    const char *capability = document["capability"] | "";
    if (strcmp(capability, "indicator.set") != 0) {
        publish_ack(command_id, "failed", "unsupported_capability");
        return;
    }

    const char *color = document["params"]["color"] | "off";
    const char *mode = document["params"]["mode"] | "solid";
    publish_ack(command_id, "running");
    apply_indicator(color, mode);
    publish_state();
    publish_ack(command_id, "succeeded");
}

void on_mqtt_message(char *topic, byte *payload, unsigned int length)
{
    if (String(topic) == topic_commands) {
        handle_command(payload, length);
    }
}

void connect_wifi_if_needed()
{
    if (WiFi.status() == WL_CONNECTED) {
        return;
    }
    if (millis() < next_wifi_attempt) {
        return;
    }
    next_wifi_attempt = millis() + 5000;
    WiFi.mode(WIFI_STA);
    WiFi.begin(HUB_WIFI_SSID, HUB_WIFI_PASSWORD);
}

void connect_mqtt_if_needed()
{
    if (WiFi.status() != WL_CONNECTED || mqtt_client.connected()) {
        return;
    }
    if (millis() < next_mqtt_attempt) {
        return;
    }
    next_mqtt_attempt = millis() + 5000;
    String client_id = String(HUB_DEVICE_ID) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    if (!mqtt_client.connect(client_id.c_str(), topic_availability.c_str(), 1, true, "{\"status\":\"offline\"}")) {
        return;
    }
    mqtt_client.subscribe(topic_commands.c_str(), 1);
    publish_availability("online");
    publish_manifest();
    publish_state();
}

void emit_button_event()
{
    StaticJsonDocument<160> document;
    document["event_type"] = "button.press";
    document["button"] = "main";
    document["pressed_at_ms"] = millis();
    publish_json(topic_events, document);
}

}  // namespace

void setup()
{
    Serial.begin(115200);
    pinMode(HUB_STATUS_LED_PIN, OUTPUT);
    pinMode(HUB_BUTTON_PIN, INPUT_PULLUP);
    mqtt_client.setServer(HUB_MQTT_HOST, HUB_MQTT_PORT);
    mqtt_client.setCallback(on_mqtt_message);
}

void loop()
{
    connect_wifi_if_needed();
    connect_mqtt_if_needed();
    if (mqtt_client.connected()) {
        mqtt_client.loop();
    }

    static int last_button_state = HIGH;
    int button_state = digitalRead(HUB_BUTTON_PIN);
    if (last_button_state == HIGH && button_state == LOW && mqtt_client.connected()) {
        emit_button_event();
    }
    last_button_state = button_state;
    delay(10);
}
