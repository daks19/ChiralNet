import json
import sqlite3
from datetime import datetime
import paho.mqtt.client as mqtt

# ---- Config ----
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "chiralnet/telemetry"
DB_FILE = "chiralnet.db"

# ---- Database setup ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            ssid TEXT,
            rssi INTEGER,
            channel INTEGER,
            latency_ms REAL,
            packet_loss REAL,
            nearby_aps INTEGER,
            device_timestamp INTEGER,
            received_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def store_reading(data):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO telemetry
        (device_id, ssid, rssi, channel, latency_ms, packet_loss, nearby_aps, device_timestamp, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("device_id"),
        data.get("ssid"),
        data.get("rssi"),
        data.get("channel"),
        data.get("latency_ms"),
        data.get("packet_loss"),
        data.get("nearby_aps"),
        data.get("timestamp"),
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()

# ---- MQTT callbacks ----
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected to MQTT broker (reason code: {reason_code})")
    client.subscribe(MQTT_TOPIC)
    print(f"Subscribed to topic: {MQTT_TOPIC}")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        data = json.loads(payload)
        store_reading(data)
        print(f"Stored reading from {data.get('device_id')}: "
              f"RSSI={data.get('rssi')} dBm, "
              f"Latency={data.get('latency_ms')} ms, "
              f"Loss={data.get('packet_loss')}%, "
              f"APs={data.get('nearby_aps')}")
    except json.JSONDecodeError:
        print(f"Failed to parse message: {msg.payload}")
    except Exception as e:
        print(f"Error processing message: {e}")

# ---- Main ----
def main():
    init_db()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to broker at {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)

    client.loop_forever()

if __name__ == "__main__":
    main()