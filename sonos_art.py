#!/usr/bin/env python3
"""Display Dining Room Sonos artwork on a 64x64 RGB matrix."""

import logging
import json
import os
import subprocess
import threading
import time
from io import BytesIO
from queue import Empty, Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from soco import SoCo
from rgbmatrix import RGBMatrix, RGBMatrixOptions


ROOM_IP = os.environ.get("SONOS_ROOM_IP", "192.168.68.70")
# This is the local render/event-loop tick; speaker health checks use the
# separate FALLBACK_POLL_SECONDS interval below.
POLL_SECONDS = float(os.environ.get("SONOS_EVENT_TICK_SECONDS", "0.1"))
BRIGHTNESS = int(os.environ.get("MATRIX_BRIGHTNESS", "40"))
PAUSE_FADE_SECONDS = float(os.environ.get("SONOS_PAUSE_FADE_SECONDS", "120"))
FALLBACK_POLL_SECONDS = float(os.environ.get("SONOS_FALLBACK_POLL_SECONDS", "30"))
BANNER_SECONDS = float(os.environ.get("SONOS_BANNER_SECONDS", "5"))
HTTP_PORT = int(os.environ.get("SONOS_ART_HTTP_PORT", "8787"))
MQTT_HOST = os.environ.get("SONOS_MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.environ.get("SONOS_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("SONOS_MQTT_USER")
MQTT_PASSWORD = os.environ.get("SONOS_MQTT_PASSWORD")
MQTT_STATE_TOPIC = "sonos-art/state"
MQTT_STATUS_TOPIC = "sonos-art/status"
MQTT_DISCOVERY_PREFIX = os.environ.get("SONOS_MQTT_DISCOVERY_PREFIX", "homeassistant")
CONFIG_PATH = os.environ.get("SONOS_ART_CONFIG", "/opt/sonos-art/config.json")
DEFAULT_CONFIG = {
    "enabled": True,
    "brightness": 40,
    "banner_seconds": 5,
    "banner_speed": 18,
    "pause_fade_seconds": 120,
    "keep_paused_art": True,
}
config_lock = threading.Lock()


def load_config():
    values = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            values.update(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return values


runtime_config = load_config()


def get_config(name):
    with config_lock:
        return runtime_config[name]


def set_config(name, value):
    with config_lock:
        runtime_config[name] = value
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(runtime_config, handle, indent=2)

health_lock = threading.Lock()
health = {
    "status": "starting",
    "version": "0.1.0",
    "room_ip": ROOM_IP,
    "transport_state": "UNKNOWN",
    "volume": None,
    "title": None,
    "artist": None,
    "album": None,
    "event_subscriptions": False,
    "last_success": None,
    "last_error": None,
    "uptime_seconds": 0,
    "speaker_name": None,
    "source_uri": None,
    "is_line_in": False,
    "cpu_temperature_c": None,
    "throttled": None,
    "last_artwork_success": None,
    "artwork_cache_hits": 0,
    "last_event": None,
}
started_at = time.time()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/config"):
            self.send_error(404)
            return
        with health_lock:
            payload = dict(health)
            payload["uptime_seconds"] = int(time.time() - started_at)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def update_health(**values):
    with health_lock:
        health.update(values)


def systemd_notify(message):
    try:
        subprocess.run(
            ["systemd-notify", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def cpu_temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as handle:
            return round(int(handle.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def throttled_state():
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, check=False
        )
        return result.stdout.strip().split("=", 1)[-1]
    except OSError:
        return None


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("health API listening on port %s", HTTP_PORT)
    return server


class MqttPublisher:
    def __init__(self):
        self.client = None
        self.connected = threading.Event()
        if mqtt is None:
            logging.warning("paho-mqtt is not installed; MQTT disabled")
            return
        self.client = mqtt.Client(client_id="sonos-art")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD or "")
        self.client.will_set(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()
        if not self.connected.wait(timeout=10):
            raise RuntimeError("MQTT broker connection timed out")
        self.client.subscribe("sonos-art/config/+/set", qos=1)
        self.client.publish(MQTT_STATUS_TOPIC, "online", qos=1, retain=True)
        self.publish_discovery()
        self.publish_config_state()
        logging.info("MQTT discovery published to %s", MQTT_HOST)

    def _on_connect(self, _client, _userdata, _flags, rc, *_args):
        if rc == 0:
            self.connected.set()
        else:
            logging.error("MQTT broker rejected connection with code %s", rc)

    def _on_message(self, _client, _userdata, message):
        name = message.topic.removeprefix("sonos-art/config/").removesuffix("/set")
        raw = message.payload.decode("utf-8").strip()
        try:
            if name in ("enabled", "keep_paused_art"):
                value = raw.lower() in ("on", "true", "1")
            elif name in ("brightness", "banner_seconds", "banner_speed", "pause_fade_seconds"):
                value = float(raw)
                if name == "brightness":
                    value = int(max(1, min(100, value)))
                elif name == "banner_seconds":
                    value = max(1, min(30, value))
                elif name == "banner_speed":
                    value = max(5, min(60, value))
                else:
                    value = max(0, min(600, value))
            else:
                return
            set_config(name, value)
            self.publish_config_state()
            logging.info("configuration changed: %s=%s", name, value)
        except (TypeError, ValueError, OSError):
            logging.exception("invalid MQTT configuration value for %s", name)

    def publish_discovery(self):
        device = {
            "identifiers": ["sonos_art"],
            "name": "Sonos Art",
            "manufacturer": "Custom",
            "model": "Raspberry Pi RGB Matrix Sonos Display",
            "sw_version": "0.1.0",
            "configuration_url": "http://sonos-art.local:8787/",
        }
        sensors = {
            "health": ("Health", "status", None),
            "transport": ("Transport State", "transport_state", None),
            "volume": ("Volume", "volume", "%"),
            "track": ("Track", "title", None),
            "artist": ("Artist", "artist", None),
            "album": ("Album", "album", None),
            "events": ("Event Subscriptions", "event_subscriptions", None),
            "uptime": ("Uptime", "uptime_seconds", "s"),
        }
        for key, (name, field, unit) in sensors.items():
            payload = {
                "name": name,
                "unique_id": f"sonos_art_{key}",
                "state_topic": MQTT_STATE_TOPIC,
                "value_template": "{{ value_json.%s }}" % field,
                "availability_topic": MQTT_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/sonos_art/{key}/config"
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

        numbers = {
            "brightness": ("Brightness", 1, 100, 1, "%"),
            "banner_seconds": ("Banner Duration", 1, 30, 1, "s"),
            "banner_speed": ("Banner Scroll Speed", 5, 60, 1, "px/s"),
            "pause_fade_seconds": ("Pause Fade Duration", 0, 600, 5, "s"),
        }
        for key, (name, minimum, maximum, step, unit) in numbers.items():
            payload = {
                "name": name,
                "unique_id": f"sonos_art_{key}",
                "command_topic": f"sonos-art/config/{key}/set",
                "state_topic": f"sonos-art/config/{key}/state",
                "availability_topic": MQTT_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "min": minimum,
                "max": maximum,
                "step": step,
                "unit_of_measurement": unit,
                "device": device,
            }
            topic = f"{MQTT_DISCOVERY_PREFIX}/number/sonos_art/{key}/config"
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

        payload = {
            "name": "Keep Artwork While Paused",
            "unique_id": "sonos_art_keep_paused_art",
            "command_topic": "sonos-art/config/keep_paused_art/set",
            "state_topic": "sonos-art/config/keep_paused_art/state",
            "payload_on": "true",
            "payload_off": "false",
            "state_on": "true",
            "state_off": "false",
            "availability_topic": MQTT_STATUS_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        self.client.publish(
            f"{MQTT_DISCOVERY_PREFIX}/switch/sonos_art/keep_paused_art/config",
            json.dumps(payload), qos=1, retain=True
        )
        payload["name"] = "Display Enabled"
        payload["unique_id"] = "sonos_art_enabled"
        payload["command_topic"] = "sonos-art/config/enabled/set"
        payload["state_topic"] = "sonos-art/config/enabled/state"
        self.client.publish(
            f"{MQTT_DISCOVERY_PREFIX}/switch/sonos_art/enabled/config",
            json.dumps(payload), qos=1, retain=True
        )

    def publish_state(self):
        if self.client is None:
            return
        with health_lock:
            payload = dict(health)
            payload["uptime_seconds"] = int(time.time() - started_at)
        self.client.publish(MQTT_STATE_TOPIC, json.dumps(payload), qos=0, retain=True)

    def publish_config_state(self):
        if self.client is None:
            return
        with config_lock:
            values = dict(runtime_config)
        for name, value in values.items():
            self.client.publish(f"sonos-art/config/{name}/state", str(value).lower(), qos=1, retain=True)

    def close(self):
        if self.client is not None:
            self.client.publish(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()


def make_matrix():
    options = RGBMatrixOptions()
    options.rows = 64
    options.cols = 64
    options.chain_length = 1
    options.parallel = 1
    options.hardware_mapping = "adafruit-hat-pwm"
    options.gpio_slowdown = 2
    options.brightness = 100
    return RGBMatrix(options=options)


def artwork_image(payload):
    image = Image.open(BytesIO(payload)).convert("RGB")
    return ImageOps.fit(image, (64, 64), Image.Resampling.LANCZOS)


def banner_font():
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, 8)
        except OSError:
            pass
    return ImageFont.load_default()


def show_image(matrix, image, brightness=1.0, banner=None, banner_started=None, banner_kind="track"):
    brightness_factor = max(0.0, brightness * get_config("brightness") / 100.0)
    frame = ImageEnhance.Brightness(image).enhance(brightness_factor)
    if banner and brightness > 0:
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 52, 63, 63), fill=(0, 0, 0))
        font = banner_font()
        if banner_kind == "volume":
            try:
                volume = max(0, min(100, int(banner)))
            except (TypeError, ValueError):
                volume = 0
            draw.rectangle((2, 55, 43, 59), outline=(255, 255, 255))
            fill_width = int(40 * volume / 100)
            if fill_width > 0:
                draw.rectangle((3, 56, 2 + fill_width, 58), fill=(80, 220, 255))
            draw.text((47, 54), str(volume), font=font, fill=(255, 255, 255))
        else:
            text = banner[:160]
            box = draw.textbbox((0, 0), text, font=font)
            text_width = box[2] - box[0]
            if text_width <= 60 or banner_started is None:
                x = max(2, (64 - text_width) // 2)
            else:
                elapsed = max(0.0, time.monotonic() - banner_started)
                travel = text_width + 64
                x = 64 - int((elapsed * get_config("banner_speed")) % travel)
            draw.text((x, 54), text, font=font, fill=(255, 255, 255))
    matrix.SetImage(frame.rotate(180))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    speaker = SoCo(ROOM_IP)
    matrix = make_matrix()
    matrix.Clear()
    health_server = start_health_server()
    mqtt_publisher = None
    try:
        mqtt_publisher = MqttPublisher()
    except Exception:
        logging.exception("MQTT connection failed; continuing without MQTT")
    event_queue = Queue()
    subscriptions = []
    try:
        subscriptions.append(speaker.avTransport.subscribe(
            auto_renew=True, event_queue=event_queue
        ))
        subscriptions.append(speaker.renderingControl.subscribe(
            auto_renew=True, event_queue=event_queue
        ))
        update_health(status="ok", event_subscriptions=True, last_error=None)
        if mqtt_publisher:
            mqtt_publisher.publish_state()
        logging.info("subscribed to Sonos transport and volume events")
    except Exception:
        update_health(status="degraded", event_subscriptions=False)
        logging.exception("event subscription failed; using fallback polling")

    last_art_url = None
    last_payload = None
    base_image = None
    last_state = None
    last_volume = None
    paused_at = None
    banner_until = 0.0
    banner_text = None
    banner_kind = "track"
    banner_started = None
    next_poll = 0.0
    was_enabled = True

    try:
        while True:
            enabled = bool(get_config("enabled"))
            if not enabled:
                if was_enabled:
                    matrix.Clear()
                    logging.info("display disabled")
                was_enabled = False
                time.sleep(POLL_SECONDS)
                continue
            if not was_enabled:
                next_poll = 0.0
                logging.info("display enabled")
            was_enabled = True

            now = time.monotonic()
            event_received = False
            try:
                event_queue.get(timeout=min(POLL_SECONDS, max(0.0, next_poll - now)))
                event_received = True
            except Empty:
                pass

            now = time.monotonic()
            should_poll = event_received or now >= next_poll
            if should_poll:
                next_poll = now + FALLBACK_POLL_SECONDS
            try:
                if should_poll:
                    transport = speaker.get_current_transport_info()
                    state = transport.get("current_transport_state", "STOPPED")
                    track = speaker.get_current_track_info()
                    art_url = track.get("album_art")
                    volume = speaker.volume
                    update_health(
                        status="ok" if subscriptions else "degraded",
                        transport_state=state,
                        volume=volume,
                        title=track.get("title"),
                        artist=track.get("artist"),
                        album=track.get("album"),
                        last_success=int(time.time()),
                        last_error=None,
                    )
                    if mqtt_publisher:
                        mqtt_publisher.publish_state()

                    # STOPPED/NO_MEDIA means the display should be blank.
                    if state in ("STOPPED", "NO_MEDIA") or not art_url:
                        update_health(title=None, artist=None, album=None)
                        if last_state not in ("STOPPED", "NO_MEDIA"):
                            matrix.Clear()
                            logging.info("cleared display (%s)", state)
                        last_art_url = None
                        last_payload = None
                        base_image = None
                        paused_at = None
                        banner_text = None
                    elif art_url != last_art_url:
                        response = requests.get(art_url, timeout=10)
                        response.raise_for_status()
                        last_payload = response.content
                        base_image = artwork_image(last_payload)
                        banner_text = track.get("title", "Unknown")
                        banner_kind = "track"
                        banner_started = time.monotonic()
                        banner_until = time.monotonic() + get_config("banner_seconds")
                        show_image(matrix, base_image, banner=banner_text, banner_started=banner_started, banner_kind=banner_kind)
                        last_art_url = art_url
                        paused_at = time.monotonic() if state == "PAUSED_PLAYBACK" else None
                        last_volume = volume
                        logging.info("displaying %s — %s (%s)", track.get("artist", ""), track.get("title", ""), state)
                    elif state == "PLAYING":
                        if paused_at is not None and base_image is not None:
                            show_image(matrix, base_image, banner=banner_text if time.monotonic() < banner_until else None, banner_started=banner_started, banner_kind=banner_kind)
                        paused_at = None
                    elif state == "PAUSED_PLAYBACK" and base_image is not None:
                        if paused_at is None:
                            paused_at = time.monotonic()

                    if last_volume is not None and volume != last_volume and base_image is not None:
                        banner_text = str(volume)
                        banner_kind = "volume"
                        banner_started = time.monotonic()
                        banner_until = time.monotonic() + get_config("banner_seconds")
                    last_volume = volume

                    last_state = state

                # Fade locally between event/fallback-poll cycles.
                if last_state == "PAUSED_PLAYBACK" and not get_config("keep_paused_art"):
                    matrix.Clear()
                elif last_state == "PAUSED_PLAYBACK" and base_image is not None and paused_at is not None:
                    elapsed = time.monotonic() - paused_at
                    fade_seconds = get_config("pause_fade_seconds")
                    remaining = 0.0 if fade_seconds == 0 else max(0.0, 1.0 - elapsed / fade_seconds)
                    if remaining > 0:
                        show_image(matrix, base_image, remaining, banner_text if time.monotonic() < banner_until else None, banner_started, banner_kind)
                    else:
                        matrix.Clear()
                elif last_state == "PLAYING" and base_image is not None:
                    show_image(
                        matrix,
                        base_image,
                        banner=banner_text if time.monotonic() < banner_until else None,
                        banner_started=banner_started,
                        banner_kind=banner_kind,
                    )
            except Exception:
                update_health(status="degraded", last_error="poll/display cycle failed")
                if mqtt_publisher:
                    mqtt_publisher.publish_state()
                logging.exception("poll/display cycle failed")
    finally:
        for subscription in subscriptions:
            subscription.unsubscribe(strict=False)
        if mqtt_publisher:
            mqtt_publisher.close()
        health_server.shutdown()


if __name__ == "__main__":
    main()
