type ThermalResponse = {
  width: number;
  height: number;
  temps: number[];
  max: number;
  min: number;
};

const heatmap = document.getElementById("heatmap") as HTMLCanvasElement | null;
const statusText = document.getElementById("status-text");
const sensorBadge = document.getElementById("sensor-badge");
const tempMax = document.getElementById("temp-max");
const tempMin = document.getElementById("temp-min");
const cameraStream = document.getElementById("camera-stream") as HTMLImageElement | null;

const context = heatmap?.getContext("2d");
const offscreen = document.createElement("canvas");
const offscreenContext = offscreen.getContext("2d");
const targetFps = 6;
const baseInterval = 1000 / targetFps;
const maxBackoff = 5000;
const hiddenInterval = 2000;
let thermalDelay = baseInterval;
let thermalTimer = 0;
let thermalInFlight = false;
let streamRetryDelay = 500;

function updateStatus(message: string): void {
  // Update the header status text.
  if (statusText) {
    statusText.textContent = message;
  }
  if (sensorBadge) sensorBadge.textContent = "Live";
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  // Convert HSL values to RGB components.
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r = 0;
  let g = 0;
  let b = 0;

  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];

  return [
    Math.round((r + m) * 255),
    Math.round((g + m) * 255),
    Math.round((b + m) * 255),
  ];
}

function colorFor(value: number, min: number, max: number): [number, number, number] {
  // Map temperature values to a blue-to-red gradient.
  const range = Math.max(max - min, 0.1);
  const t = Math.min(Math.max((value - min) / range, 0), 1);
  const hue = (1 - t) * 220 + t * 10;
  return hslToRgb(hue, 0.85, 0.55);
}

function drawHeatmap(data: ThermalResponse): void {
  // Paint the heatmap canvas from the temperature array.
  if (!context || !heatmap || !offscreenContext) return;

  const { width, height, temps } = data;
  offscreen.width = width;
  offscreen.height = height;
  const image = offscreenContext.createImageData(width, height);
  for (let i = 0; i < temps.length; i += 1) {
    const [r, g, b] = colorFor(temps[i], data.min, data.max);
    const pixel = i * 4;
    image.data[pixel] = r;
    image.data[pixel + 1] = g;
    image.data[pixel + 2] = b;
    image.data[pixel + 3] = 255;
  }

  offscreenContext.putImageData(image, 0, 0);
  heatmap.width = height;
  heatmap.height = width;
  context.clearRect(0, 0, heatmap.width, heatmap.height);
  context.save();
  context.translate(heatmap.width, 0);
  context.scale(-1, 1);
  context.translate(0, heatmap.height);
  context.rotate(-Math.PI / 2);
  context.drawImage(offscreen, 0, 0);
  context.restore();
}

async function fetchThermal(): Promise<void> {
  // Pull the latest MLX90640 data and update UI.
  try {
    const response = await fetch("/thermal", { cache: "no-store" });
    if (!response.ok) {
      updateStatus("センサー接続エラー");
      return;
    }

    const data = (await response.json()) as ThermalResponse;
    drawHeatmap(data);
    if (tempMax) tempMax.textContent = data.max.toFixed(1);
    if (tempMin) tempMin.textContent = data.min.toFixed(1);
    updateStatus("監視中");
    thermalDelay = baseInterval;
  } catch (error) {
    updateStatus("センサー通信失敗");
    thermalDelay = Math.min(thermalDelay * 1.5, maxBackoff);
  }
}

function scheduleThermal(delay: number): void {
  // Schedule the next thermal poll with backoff.
  window.clearTimeout(thermalTimer);
  thermalTimer = window.setTimeout(tickThermal, delay);
}

updateStatus("接続中...");

async function tickThermal(): Promise<void> {
  // Perform one thermal poll cycle respecting backoff.
  if (document.hidden) {
    scheduleThermal(hiddenInterval);
    return;
  }
  if (thermalInFlight) {
    scheduleThermal(baseInterval);
    return;
  }
  thermalInFlight = true;
  try {
    await fetchThermal();
  } finally {
    thermalInFlight = false;
    scheduleThermal(thermalDelay);
  }
}

function startStream(): void {
  // Force-refresh the MJPEG stream URL.
  if (!cameraStream) return;
  const cacheBust = Date.now();
  cameraStream.src = `/stream?t=${cacheBust}`;
}

function scheduleStreamRetry(): void {
  // Reconnect the stream with exponential backoff.
  window.setTimeout(() => {
    streamRetryDelay = Math.min(streamRetryDelay * 1.5, maxBackoff);
    startStream();
  }, streamRetryDelay);
}

if (cameraStream) {
  cameraStream.addEventListener("error", () => {
    updateStatus("カメラ再接続中...");
    scheduleStreamRetry();
  });
  cameraStream.addEventListener("load", () => {
    streamRetryDelay = 500;
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (cameraStream) cameraStream.src = "";
  } else {
    startStream();
    scheduleThermal(baseInterval);
  }
});

startStream();
scheduleThermal(baseInterval);
