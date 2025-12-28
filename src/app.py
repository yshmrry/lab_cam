import atexit
import logging
import os
import time
from collections import deque
from threading import Event, Lock, Thread
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
        self._init_lock = Lock()
        self._lock = Lock()
        self._frame = [0.0] * 768
        self._last_error: str | None = None

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
            self._last_error = None
        except Exception:
            self._sensor = None
            self._last_error = "MLX90640 init failed"
            logger.exception("MLX90640 init failed")

    def _ensure_sensor(self) -> None:
        # Lazily initialize the sensor on first use.
        if self._sensor is not None:
            return
        with self._init_lock:
            if self._sensor is None:
                self._init_sensor()

    def is_ready(self) -> bool:
        # Report whether the MLX90640 sensor is available.
        self._ensure_sensor()
        return self._sensor is not None

    def read_frame(self) -> Tuple[List[float], float, float]:
        # Return a 24x32 temperature frame with max/min values.
        self._ensure_sensor()
        if self._sensor is None:
            self._last_error = "MLX90640 sensor not available"
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
                self._last_error = "MLX90640 read failed"
                raise RuntimeError("MLX90640 read failed") from last_error

        max_temp = max(self._frame)
        min_temp = min(self._frame)
        return list(self._frame), max_temp, min_temp

    def last_error(self) -> str | None:
        # Return the most recent sensor error message.
        return self._last_error


app = Flask(__name__, template_folder="../templates", static_folder="../static")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("thermal_app")
thermal_reader = ThermalReader()
temperature_history = deque(maxlen=int(os.getenv("TEMP_HISTORY_SIZE", "1000")))

_capture = None
_capture_lock = Lock()
_camera_thread: Thread | None = None
_camera_stop = Event()
_camera_frame: np.ndarray | None = None
_camera_frame_time = 0.0
_camera_lock = Lock()
_camera_last_error: str | None = None

_thermal_thread: Thread | None = None
_thermal_stop = Event()
_thermal_frame: List[float] | None = None
_thermal_stats: Tuple[float, float] | None = None
_thermal_frame_time = 0.0
_thermal_lock = Lock()


def _get_capture():
    # Lazily initialize the USB camera on first use.
    global _capture
    if cv2 is None:
        return None
    if _capture is not None:
        return _capture
    with _capture_lock:
        if _capture is not None:
            return _capture
        camera_path = os.getenv(
            "CAMERA_PATH",
            "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._Webcam_Webcam-video-index0",
        )
        if os.path.exists(camera_path):
            camera_source: str | int = camera_path
        else:
            camera_source = int(os.getenv("CAMERA_INDEX", "0"))
        capture = cv2.VideoCapture(camera_source)
        camera_width = int(os.getenv("CAMERA_WIDTH", "640"))
        camera_height = int(os.getenv("CAMERA_HEIGHT", "480"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, camera_width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, camera_height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            global _camera_last_error
            _camera_last_error = "Camera open failed"
            return None
        _capture = capture
        return _capture


def _reset_capture() -> None:
    # Reset the USB camera capture handle.
    global _capture
    with _capture_lock:
        if _capture is not None:
            _capture.release()
            _capture = None
    global _camera_last_error
    _camera_last_error = "Camera reset after read failure"


def _ensure_camera_thread() -> None:
    # Ensure the camera reader thread is running.
    global _camera_thread
    if cv2 is None:
        return
    if _camera_thread and _camera_thread.is_alive():
        return
    _camera_stop.clear()
    _camera_thread = Thread(target=_camera_worker, daemon=True)
    _camera_thread.start()


def _camera_worker() -> None:
    # Continuously read camera frames into a shared buffer.
    failures = 0
    target_fps = float(os.getenv("CAMERA_READ_FPS", "20"))
    frame_interval = 1.0 / max(target_fps, 1.0)
    last_read = 0.0
    last_log = 0.0

    while not _camera_stop.is_set():
        capture = _get_capture()
        if capture is None:
            time.sleep(0.5)
            continue

        now = time.time()
        if now - last_read < frame_interval:
            time.sleep(frame_interval / 2)
            continue
        last_read = now

        ok, frame = capture.read()
        if not ok:
            failures += 1
            now = time.time()
            if now - last_log > 5:
                logger.warning("Camera read failed (%d)", failures)
                last_log = now
            if failures >= 5:
                _reset_capture()
                failures = 0
            time.sleep(0.1)
            continue

        failures = 0
        with _camera_lock:
            global _camera_frame, _camera_frame_time
            _camera_frame = frame
            _camera_frame_time = now


def _get_latest_camera_frame() -> np.ndarray | None:
    # Fetch the most recent camera frame from the buffer.
    with _camera_lock:
        if _camera_frame is None:
            return None
        return _camera_frame.copy()


def _ensure_thermal_thread() -> None:
    # Ensure the thermal reader thread is running.
    global _thermal_thread
    if _thermal_thread and _thermal_thread.is_alive():
        return
    _thermal_stop.clear()
    _thermal_thread = Thread(target=_thermal_worker, daemon=True)
    _thermal_thread.start()


def _thermal_worker() -> None:
    # Continuously read thermal frames into a shared buffer.
    interval = float(os.getenv("THERMAL_READ_INTERVAL", "0.25"))
    last_log = 0.0
    while not _thermal_stop.is_set():
        if not thermal_reader.is_ready():
            time.sleep(0.5)
            continue
        try:
            frame, max_temp, min_temp = thermal_reader.read_frame()
        except RuntimeError:
            now = time.time()
            if now - last_log > 5:
                logger.warning("MLX90640 read failed")
                last_log = now
            time.sleep(interval)
            continue
        with _thermal_lock:
            global _thermal_frame, _thermal_stats, _thermal_frame_time
            _thermal_frame = frame
            _thermal_stats = (max_temp, min_temp)
            _thermal_frame_time = time.time()
        time.sleep(interval)


def _get_latest_thermal() -> Tuple[List[float], float, float] | None:
    # Fetch the most recent thermal frame and stats.
    with _thermal_lock:
        if _thermal_frame is None or _thermal_stats is None:
            return None
        return list(_thermal_frame), _thermal_stats[0], _thermal_stats[1]


@atexit.register
def _cleanup_camera() -> None:
    # Release the USB camera on exit.
    _camera_stop.set()
    _thermal_stop.set()
    global _capture
    if _capture is not None:
        _capture.release()
        _capture = None


def index() -> str:
    # Render the main dashboard screen.
    return render_template("index.html")


app.add_url_rule("/", view_func=index)


def generate_frames() -> Response:
    # Stream MJPEG frames from the USB camera.
    _ensure_camera_thread()
    if cv2 is None:
        return Response("Camera backend not available", status=503)

    def stream():
        # Yield JPEG frames to the client as multipart data.
        target_fps = float(os.getenv("TARGET_FPS", "15"))
        frame_interval = 1.0 / max(target_fps, 1.0)
        last_frame = 0.0
        jpeg_quality = int(os.getenv("JPEG_QUALITY", "70"))
        max_width = int(os.getenv("STREAM_WIDTH", "640"))
        max_height = int(os.getenv("STREAM_HEIGHT", "480"))
        max_age = float(os.getenv("STREAM_FRAME_MAX_AGE", "2.0"))
        last_sent = 0.0

        while True:
            now = time.time()
            if now - last_frame < frame_interval:
                time.sleep(frame_interval / 2)
                continue
            last_frame = now

            frame = _get_latest_camera_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            with _camera_lock:
                frame_time = _camera_frame_time
            if now - frame_time > max_age:
                time.sleep(0.1)
                continue
            if frame_time <= last_sent:
                time.sleep(0.01)
                continue
            last_sent = frame_time

            if frame.shape[1] != max_width or frame.shape[0] != max_height:
                frame = cv2.resize(frame, (max_width, max_height))
            success, buffer = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
            )
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
    _ensure_thermal_thread()
    if not thermal_reader.is_ready():
        return Response("MLX90640 not available", status=503)

    def stream():
        # Yield colorized thermal frames as multipart JPEG.
        max_age = float(os.getenv("THERMAL_MAX_AGE", "2.0"))
        while True:
            cached = _get_latest_thermal()
            if cached is None:
                time.sleep(0.2)
                continue
            frame, _, _ = cached
            if time.time() - _thermal_frame_time > max_age:
                time.sleep(0.2)
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
    _ensure_thermal_thread()
    if not thermal_reader.is_ready():
        return Response("MLX90640 not available", status=503)
    cached = _get_latest_thermal()
    if cached is None:
        return Response("MLX90640 read failed", status=503)
    frame, max_temp, min_temp = cached
    max_age = float(os.getenv("THERMAL_MAX_AGE", "2.0"))
    if time.time() - _thermal_frame_time > max_age:
        return Response("MLX90640 data stale", status=503)
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
    _ensure_thermal_thread()
    if not thermal_reader.is_ready():
        return jsonify({"error": "Thermal sensor not initialized"}), 500
    cached = _get_latest_thermal()
    if cached is None:
        return jsonify({"error": "MLX90640 read failed"}), 500
    _, max_temp, min_temp = cached
    max_age = float(os.getenv("THERMAL_MAX_AGE", "2.0"))
    if time.time() - _thermal_frame_time > max_age:
        return jsonify({"error": "MLX90640 data stale"}), 500
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


def healthz() -> Response:
    # Return a lightweight health status for UI checks.
    _ensure_camera_thread()
    _ensure_thermal_thread()
    camera_ready = _get_latest_camera_frame() is not None
    thermal_ready = _get_latest_thermal() is not None
    return jsonify(
        {
            "camera": "ok" if camera_ready else "unavailable",
            "thermal": "ok" if thermal_ready else "unavailable",
            "camera_error": _camera_last_error,
            "thermal_error": thermal_reader.last_error(),
        }
    )


app.add_url_rule("/healthz", view_func=healthz)


if __name__ == "__main__":
    # Run the Flask development server.
    debug_enabled = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_enabled, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
