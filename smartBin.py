from machine import Pin, time_pulse_us, I2C
import time
import dht
from imu import MPU6050

import network
import json
from umqtt.simple import MQTTClient

# --- Constants & Configuration ---
BIN_HEIGHT_CM = 27         
FIRE_TEMP = 35            
SOUND_SPEED = 340
TRIG_PULSE_DURATION_US = 10
THRESHOLD = 0.7
SAFETY_CHECK_INTERVAL = 10

WIFI_SSID = ""
WIFI_PASSWORD = ""

MQTT_BROKER = "test.mosquitto.org"
MQTT_CLIENT_ID = "smartbin01"
MQTT_TOPIC = b"smartbin/bin01/telemetry"

BIN_ID = "bin01"

open_count = 0
last_reed_state = 1

# --- Pin Setup ---
try:
    buzzer = Pin(18, Pin.OUT)
    trig_pin = Pin(12, Pin.OUT)
    echo_pin = Pin(13, Pin.IN)
    reed = Pin(10, Pin.IN, Pin.PULL_UP)
    dht_sensor = dht.DHT11(Pin(16))

    # --- IMU (MPU6050) Setup ---
    i2c = I2C(0, sda=Pin(8), scl=Pin(9), freq=400000)
    imu = MPU6050(i2c)

except Exception as e:
    print(f"CRITICAL ERROR: Failed to initialize hardware. {e}")

def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    print("Connecting WiFi...")
    while not wlan.isconnected():
        time.sleep(1)

    print("WiFi connected:", wlan.ifconfig())


def connect_mqtt():
    client = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER)
    client.connect()
    print("MQTT connected")
    return client


def publish_data(client, data):
    try:
        msg = json.dumps(data)
        client.publish(MQTT_TOPIC, msg)
        print("MQTT sent:", msg)
    except Exception as e:
        print("MQTT error:", e)


# ----------------- SENSORS -----------------

def get_distance():
    try:
        trig_pin.value(0)
        time.sleep_us(5)

        trig_pin.value(1)
        time.sleep_us(TRIG_PULSE_DURATION_US)
        trig_pin.value(0)

        duration = time_pulse_us(echo_pin, 1, 30000)

        if duration < 0:
            return None

        return (SOUND_SPEED * duration) / 20000

    except:
        return None

def get_fill_level(distance):
    if distance is None:
        return None, "Unknown"

    fill_percent = 100 * (BIN_HEIGHT_CM - distance) / BIN_HEIGHT_CM
    fill_percent = max(0, min(100, fill_percent))
    fill_percent = round(fill_percent, 1)

    if fill_percent < 40:
        level = "Level 1 - Normal"
    elif fill_percent < 70:
        level = "Level 2 - Medium"
    elif fill_percent < 90:
        level = "Level 3 - Almost Full"
    else:
        level = "Level 4 - Urgent Collection"

    return fill_percent, level

def get_reed_state():
    try:
        return reed.value()
    except:
        return None

def check_lid_opening(current_reed_state):
    global open_count, last_reed_state

    lid_open = current_reed_state == 1
    opening_detected = False

    if last_reed_state == 0 and current_reed_state == 1:
        open_count += 1
        opening_detected = True
        print("Lid opening detected!")

    last_reed_state = current_reed_state

    return lid_open, open_count, opening_detected

def get_dht_readings():
    try:
        dht_sensor.measure()
        return dht_sensor.temperature(), dht_sensor.humidity()
    except:
        return None, None


def get_imu():
    """Returns accel + gyro data + direction."""
    try:
        ax = round(imu.accel.x, 2)
        ay = round(imu.accel.y, 2)
        az = round(imu.accel.z, 2)

        gx = round(imu.gyro.x, 2)
        gy = round(imu.gyro.y, 2)
        gz = round(imu.gyro.z, 2)

        return ax, ay, az, gx, gy, gz, get_direction(ax, ay, az)

    except:
        return None, None, None, None, None, None, None


def get_direction(ax, ay, az):
    if az > THRESHOLD:
        return "Face Up"
    elif az < -THRESHOLD:
        return "Face Down"
    elif ax > THRESHOLD:
        return "Right"
    elif ax < -THRESHOLD:
        return "Left"
    elif ay > THRESHOLD:
        return "Forward"
    elif ay < -THRESHOLD:
        return "Backward"
    else:
        return "Unknown"

def is_overturned(direction):
    return direction != "Face Up"

def get_alert_status(temp, direction):
    fire_alert = temp is not None and temp >= FIRE_TEMP
    overturned = is_overturned(direction) if direction is not None else False

    if fire_alert and overturned:
        return "FIRE_AND_OVERTURNED"
    elif fire_alert:
        return "FIRE_ALERT"
    elif overturned:
        return "OVERTURNED"
    else:
        return "NORMAL"


def manage_buzzer(status):
    if status in ["FIRE_ALERT", "OVERTURNED", "FIRE_AND_OVERTURNED"]:
        buzzer.on()
    else:
        buzzer.off()

# ----------------- MAIN LOOP -----------------

def main():
    print("Starting SmartBin IoT...")
    

    connect_wifi()
    client = connect_mqtt()
    last_safety_check = time.time() - SAFETY_CHECK_INTERVAL

    while True:
        
        current_time = time.time()

        reed_state = get_reed_state()

        if reed_state is None:
            print("Reed Switch: Failed")
            time.sleep(1)
            continue

        lid_open, total_openings, opening_detected = check_lid_opening(reed_state)

        # 1. Ultrasonidos: SOLO mide cuando se abre la tapa
        if opening_detected:
            distance = get_distance()
            fill_percent, fill_level = get_fill_level(distance)

            print("\n--- Lid opening measurement ---")
            print(f"Lid open:     {lid_open}")
            print(f"Open count:   {total_openings}")
            print(f"Distance:     {distance:.2f} cm" if distance is not None else "Distance:     Failed")
            print(f"Fill:         {fill_percent}%")
            print(f"Fill level:   {fill_level}")

            data = {
                "bin_id": BIN_ID,
                "event": "lid_opening",
                "open_count": total_openings,
                "distance_cm": distance,
                "fill_percent": fill_percent,
                "fill_level": fill_level,
                "timestamp": time.time()
            }

            publish_data(client, data)

        # 2. Temperatura y vuelco: cada 10 segundos
        if current_time - last_safety_check >= SAFETY_CHECK_INTERVAL:
            temp, hum = get_dht_readings()
            ax, ay, az, gx, gy, gz, direction = get_imu()

            status = get_alert_status(temp, direction)
            manage_buzzer(status)

            print("\n--- Safety measurement ---")

            if temp is not None:
                print(f"Temperature:  {temp:.1f} C")
                print(f"Humidity:     {hum:.1f} %")
            else:
                print("DHT11:        Failed")

            if direction is not None:
                print(f"Orientation:  {direction}")
                print(f"Overturned:   {is_overturned(direction)}")
            else:
                print("IMU:          Failed")

            print(f"Status:       {status}")

            last_safety_check = current_time

            data = {
                "bin_id": BIN_ID,
                "event": "safety_measurement",
                "temperature": temp,
                "humidity": hum,
                "orientation": direction,
                "overturned": is_overturned(direction) if direction is not None else None,
                "status": status,
                "timestamp": time.time()
            }

            publish_data(client, data)

        time.sleep(0.5)
try:
    main()
except KeyboardInterrupt:
    print("\nStopped by user.")
    buzzer.off()

