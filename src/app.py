import atexit
import os
import time
from collections import deque
from threading import Lock
from typing import List, Tuple

import numpy as np
from flask import Flask, Response, jsonify, render_template

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cv2 = None


class ThermalReader:
    def __init__(self) -> None:
        # Initialize MLX90640 over I2C.
        self._sensor = None
        self._lock = Lock()
        self._init_sensor()
        self._frame = [0.0] * 768

    def _init_sensor(self) -> None:
        # Attempt to configure the MLX90640 sensor over I2C.
        try:
            import board  # type: ignore
            import busio  # type: ignore
            import adafruit_mlx90640  # type: ignore

            i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
            sensor = adafruit_mlx90640.MLX90640(i2c)
            refresh = os.getenv("MLX_REFRESH", "4")
            refresh_map = {
                "1": adafruit_mlx90640.RefreshRate.REFRESH_1_HZ,
                "2": adafruit_mlx90640.RefreshRate.REFRESH_2_HZ,
                "4": adafruit_mlx90640.RefreshRate.REFRESH_4_HZ,
                "8": adafruit_mlx90640.RefreshRate.REFRESH_8_HZ,
                "16": adafruit_mlx90640.RefreshRate.REFRESH_16_HZ,
                "32": adafruit_mlx90640.RefreshRate.REFRESH_32_HZ,
                "64": adafruit_mlx90640.RefreshRate.REFRESH_64_HZ,
            }
            sensor.refresh_rate = refresh_map.get(refresh, refresh_map["32"])
            if hasattr(sensor, "auto_refresh"):
                sensor.auto_refresh = False
            self._sensor = sensor
        except Exception:
            self._sensor = None

    def is_ready(self) -> bool:
        # Report whether the MLX90640 sensor is available.
        return self._sensor is not None

    def read_frame(self) -> Tuple[List[float], float, float]:
        # Return a 24x32 temperature frame with max/min values.
        if self._sensor is None:
            raise RuntimeError("MLX90640 sensor not available")
        with self._lock:
            retries = int(os.getenv("MLX_RETRIES", "3"))
            delay = float(os.getenv("MLX_RETRY_DELAY", "0.01"))
            last_error: Exception | None = None
            for _ in range(max(retries, 1)):
                try:
                    self._sensor.getFrame(self._frame)  # type: ignore[union-attr]
                    last_error = None
                    break
                except RuntimeError as exc:
                    last_error = exc
                    time.sleep(delay)
            if last_error is not None:
                raise RuntimeError("MLX90640 read failed") from last_error

        max_temp = max(self._frame)
        min_temp = min(self._frame)
        return list(self._frame), max_temp, min_temp


app = Flask(__name__, template_folder="../templates", static_folder="../static")
thermal_reader = ThermalReader()
temperature_history = deque(maxlen=int(os.getenv("TEMP_HISTORY_SIZE", "1000")))

if cv2 is not None:
    _camera_path = os.getenv(
        "CAMERA_PATH",
        "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._Webcam_Webcam-video-index0",
    )
    if os.path.exists(_camera_path):
        _camera_source: str | int = _camera_path
    else:
        _camera_source = int(os.getenv("CAMERA_INDEX", "0"))
    _capture = cv2.VideoCapture(_camera_source)
else:  # pragma: no cover - optional dependency
    _capture = None


@atexit.register
def _cleanup_camera() -> None:
    # Release the USB camera on exit.
    if _capture is not None:
        _capture.release()


def index() -> str:
    # Render the main dashboard screen.
    return render_template("index.html")


app.add_url_rule("/", view_func=index)


def generate_frames() -> Response:
    # Stream MJPEG frames from the USB camera.
    if cv2 is None or _capture is None:
        return Response("Camera backend not available", status=503)

    _capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    _capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    def stream():
        # Yield JPEG frames to the client as multipart data.
        if not _capture.isOpened():
            yield b""
            return

        target_fps = float(os.getenv("TARGET_FPS", "60"))
        frame_interval = 1.0 / max(target_fps, 1.0)
        last_frame = 0.0

        while True:
            now = time.time()
            if now - last_frame < frame_interval:
                time.sleep(frame_interval / 2)
                continue
            last_frame = now

            ok, frame = _capture.read()
            if not ok:
                time.sleep(0.05)
                continue

            success, buffer = cv2.imencode(".jpg", frame)
            if not success:
                continue
            jpg = buffer.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            )

    return Response(stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


app.add_url_rule("/stream", view_func=generate_frames)
app.add_url_rule("/video_usb", view_func=generate_frames)


def generate_thermal_frames() -> Response:
    # Stream MLX90640 frames as an MJPEG heatmap.
    if cv2 is None:
        return Response("Camera backend not available", status=503)
    if not thermal_reader.is_ready():
        return Response("MLX90640 not available", status=503)

    def stream():
        # Yield colorized thermal frames as multipart JPEG.
        while True:
            try:
                frame, _, _ = thermal_reader.read_frame()
            except RuntimeError:
                time.sleep(0.5)
                continue
            data = np.reshape(frame, (24, 32))
            normalized = cv2.normalize(data, None, 0, 255, cv2.NORM_MINMAX)
            resized = cv2.resize(
                normalized.astype(np.uint8), (480, 640), interpolation=cv2.INTER_CUBIC
            )
            colored = cv2.applyColorMap(resized, cv2.COLORMAP_INFERNO)
            success, buffer = cv2.imencode(".jpg", colored)
            if not success:
                continue
            jpg = buffer.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            )
            time.sleep(0.25)

    return Response(stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


app.add_url_rule("/video_thermal", view_func=generate_thermal_frames)


def thermal() -> Response:
    # Serve MLX90640 data as JSON for the frontend heatmap.
    if not thermal_reader.is_ready():
        return Response("MLX90640 not available", status=503)
    try:
        frame, max_temp, min_temp = thermal_reader.read_frame()
    except RuntimeError:
        return Response("MLX90640 read failed", status=503)
    return jsonify(
        {
            "width": 32,
            "height": 24,
            "temps": frame,
            "max": round(max_temp, 2),
            "min": round(min_temp, 2),
        }
    )


app.add_url_rule("/thermal", view_func=thermal)


def temperature() -> Response:
    # Return max/min temperature and record history.
    if not thermal_reader.is_ready():
        return jsonify({"error": "Thermal sensor not initialized"}), 500
    try:
        _, max_temp, min_temp = thermal_reader.read_frame()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    now = time.strftime("%H:%M:%S")
    temperature_history.append(
        {"time": now, "max": round(max_temp, 2), "min": round(min_temp, 2)}
    )
    return jsonify({"max": round(max_temp, 2), "min": round(min_temp, 2)})


app.add_url_rule("/temperature", view_func=temperature)


def temperature_history_api() -> Response:
    # Return FIFO temperature history.
    return jsonify(list(temperature_history))


app.add_url_rule("/temperature_history", view_func=temperature_history_api)


if __name__ == "__main__":
    # Run the Flask development server.
    debug_enabled = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_enabled, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
