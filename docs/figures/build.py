"""Generate the Excalidraw scenes used by the README.

The scene files are committed next to their PNGs. Numbers come from
docs/figures/measured.json, which is written from the run records after the
benchmark and tune runs. The generator refuses to build a figure before that
file exists, so a placeholder cannot reach the README.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIGURES = ROOT / "docs" / "figures"
MEASURED = FIGURES / "measured.json"

WHITE = "#ffffff"
INK = "#1e1e1e"
MUTED = "#5c5f66"
PALETTE: dict[str, tuple[str, str]] = {
    "input": ("#1971c2", "#d0ebff"),
    "hip": ("#2f9e44", "#d3f9d8"),
    "runtime": ("#9c36b5", "#f3d9fa"),
    "analysis": ("#f08c00", "#fff3bf"),
    "risk": ("#e03131", "#ffe3e3"),
    "model": ("#0c8599", "#c5f6fa"),
    "neutral": ("#495057", "#f1f3f5"),
}
TITLE_SIZE = 32
BODY_SIZE = 22
NOTE_SIZE = 18
SCENE_W = 1520
SCENE_H = 940
MARGIN = 40
LINE_HEIGHT = 1.25


class Scene:
    """Small builder that keeps every required Excalidraw field in one place."""

    def __init__(self) -> None:
        self.elements: list[dict[str, Any]] = []
        self._updated = int(time.time() * 1000)

    def _seed(self) -> int:
        return random.randint(1, 2**31 - 1)

    def _base(
        self, element_id: str, kind: str, x: float, y: float, w: float, h: float
    ) -> dict[str, Any]:
        return {
            "id": element_id,
            "type": kind,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "angle": 0,
            "strokeColor": INK,
            "backgroundColor": "transparent",
            "fillStyle": "hachure",
            "strokeWidth": 2,
            "strokeStyle": "solid",
            "roughness": 2,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": None,
            "seed": self._seed(),
            "version": 1,
            "versionNonce": self._seed(),
            "isDeleted": False,
            "boundElements": [],
            "updated": self._updated,
            "link": None,
            "locked": False,
        }

    def text(
        self,
        element_id: str,
        x: float,
        y: float,
        text: str,
        size: int = BODY_SIZE,
        *,
        colour: str = INK,
        align: str = "left",
        width: float | None = None,
    ) -> None:
        lines = text.split("\n")
        estimated = max(len(line) for line in lines) * size * 0.55
        box_width = width if width is not None else max(60.0, estimated)
        box_height = len(lines) * size * LINE_HEIGHT
        element = self._base(element_id, "text", x, y, box_width, box_height)
        element.update(
            {
                "strokeColor": colour,
                "fontSize": size,
                "fontFamily": 1,
                "text": text,
                "textAlign": align,
                "verticalAlign": "top",
                "baseline": size * 0.9,
                "containerId": None,
                "originalText": text,
                "lineHeight": LINE_HEIGHT,
                "autoResize": True,
            }
        )
        self.elements.append(element)

    def box(
        self,
        element_id: str,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str,
        role: str,
        *,
        size: int = BODY_SIZE,
    ) -> None:
        stroke, fill = PALETTE[role]
        shape = self._base(element_id, "rectangle", x, y, w, h)
        shape.update(
            {
                "strokeColor": stroke,
                "backgroundColor": fill,
                "roundness": {"type": 3},
                "boundElements": [{"type": "text", "id": f"{element_id}-label"}],
            }
        )
        self.elements.append(shape)
        text = self._base(f"{element_id}-label", "text", x, y, w, h)
        text.update(
            {
                "strokeColor": INK,
                "fontSize": size,
                "fontFamily": 1,
                "text": label,
                "textAlign": "center",
                "verticalAlign": "middle",
                "baseline": size * 0.9,
                "containerId": element_id,
                "originalText": label,
                "lineHeight": LINE_HEIGHT,
                "autoResize": True,
            }
        )
        self.elements.append(text)

    def arrow(
        self,
        element_id: str,
        x: float,
        y: float,
        dx: float,
        dy: float,
        label: str,
        role: str = "neutral",
        *,
        label_offset: float = -26.0,
    ) -> None:
        stroke, _ = PALETTE[role]
        element = self._base(element_id, "arrow", x, y, abs(dx), abs(dy))
        element.update(
            {
                "strokeColor": stroke,
                "points": [[0, 0], [dx, dy]],
                "lastCommittedPoint": None,
                "startBinding": None,
                "endBinding": None,
                "startArrowhead": None,
                "endArrowhead": "arrow",
            }
        )
        self.elements.append(element)
        middle_x = x + dx / 2
        middle_y = y + dy / 2 + label_offset
        label_width = max(40.0, len(label) * NOTE_SIZE * 0.55)
        self.text(
            f"{element_id}-label",
            middle_x - label_width / 2,
            middle_y,
            label,
            NOTE_SIZE,
            colour=MUTED,
            align="center",
            width=label_width,
        )

    def tick(self, element_id: str, x: float, y: float, height: float = 18.0) -> None:
        element = self._base(element_id, "line", x, y, 1, height)
        element.update(
            {
                "strokeColor": MUTED,
                "points": [[0, 0], [0, height]],
                "startBinding": None,
                "endBinding": None,
                "startArrowhead": None,
                "endArrowhead": None,
            }
        )
        self.elements.append(element)

    def save(self, name: str) -> Path:
        scene = {
            "type": "excalidraw",
            "version": 2,
            "source": "blinding",
            "elements": self.elements,
            "appState": {"viewBackgroundColor": WHITE, "gridSize": None},
            "files": {},
        }
        path = FIGURES / f"{name}.excalidraw"
        path.write_text(json.dumps(scene, indent=2) + "\n", encoding="utf-8")
        return path


def load_measured() -> dict[str, Any]:
    if not MEASURED.is_file():
        raise SystemExit(
            "docs/figures/measured.json is missing; run the benchmark and tune "
            "commands and write the numbers from results/ first"
        )
    raw: Any = json.loads(MEASURED.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("measured.json must hold an object")
    return {str(key): value for key, value in raw.items()}


def architecture(measured: dict[str, Any]) -> Scene:
    scene = Scene()
    scene.text(
        "title",
        MARGIN,
        MARGIN,
        "One pinned GGUF is served, measured, tuned, and trained against",
        TITLE_SIZE,
    )
    gpu = str(measured.get("gpu_name", "RX 7800 XT"))
    scene.box("client", 60, 140, 260, 80, "HTTP client", "input")
    scene.box("gateway", 480, 140, 300, 90, "gateway\nauth, rate limit", "input")
    scene.box("server", 480, 320, 300, 110, "llama-server\nCPU or HIP", "runtime")
    scene.box("gguf", 480, 520, 300, 90, "pinned GGUF\nsha256 verified", "model")
    scene.box("bench", 60, 300, 260, 90, "bench\nTTFT, ITL, tok/s", "analysis")
    scene.box("tune", 60, 460, 260, 90, "tune\nllama-bench sweep", "analysis")
    scene.box("train", 60, 620, 260, 90, "train\nllama-finetune", "model")
    scene.box("kernel", 900, 620, 300, 90, "kernel\nHIP copy and GEMM", "hip")
    scene.box("finetuned", 900, 460, 300, 90, "finetuned GGUF", "model")
    scene.box("device", 900, 780, 300, 80, gpu, "hip")
    scene.box("results", 480, 680, 300, 80, "results\nrun.json + report.md", "neutral")
    scene.arrow("a-client", 320, 180, 150, 0, "requests", "input")
    scene.arrow("a-gateway", 480, 200, 0, 110, "proxies", "input")
    scene.arrow("a-load", 630, 430, 0, 85, "loads", "model")
    scene.arrow("a-bench", 320, 340, 150, 30, "requests", "analysis")
    scene.arrow("a-tune", 320, 500, 150, -80, "restarts", "analysis")
    scene.arrow("a-train", 320, 660, 150, 20, "reads", "model")
    scene.arrow("a-finetuned", 780, 505, 110, 0, "writes", "model")
    scene.arrow("a-kernel", 1110, 710, 110, 60, "runs on", "hip")
    scene.text(
        "note",
        60,
        800,
        "Every command writes a run record. A number enters the README only when "
        "that record is valid.",
        NOTE_SIZE,
        colour=MUTED,
        width=760,
    )
    return scene


def methodology(measured: dict[str, Any]) -> Scene:
    cpu = measured["cpu"]
    gpu = measured["gpu"]
    scene = Scene()
    scene.text(
        "title", MARGIN, MARGIN, "The client measures TTFT and every inter-token gap", TITLE_SIZE
    )
    scene.box("send", 80, 200, 220, 80, "request sent", "input")
    scene.box("first", 420, 200, 240, 80, "first content token", "model")
    scene.box("rest", 760, 200, 240, 80, "token 2..N", "analysis")
    scene.box("done", 1100, 200, 220, 80, "stream ends", "neutral")
    scene.arrow("timeline", 90, 360, 1280, 0, "", "neutral", label_offset=10)
    scene.tick("t0", 100, 350)
    scene.tick("t1", 500, 350)
    scene.tick("t2", 840, 350)
    scene.tick("t3", 1200, 350)
    scene.text("f-ttft", 220, 420, "TTFT = t_first - t_send", BODY_SIZE)
    scene.text("f-itl", 640, 420, "ITL = t_i - t_{i-1}", BODY_SIZE)
    scene.text("f-rate", 980, 420, "tok/s = N / (t_last - t_first)", BODY_SIZE)
    scene.box("cpu", 80, 560, 620, 140, "", "input")
    scene.text(
        "cpu-text",
        100,
        580,
        f"CPU  {cpu['model']}\nTTFT {cpu['ttft_ms']:.0f} ms   ITL {cpu['itl_ms']:.0f} ms   "
        f"{cpu['tok_s']:.1f} tok/s",
        BODY_SIZE,
        width=580,
    )
    scene.box("gpu", 760, 560, 620, 140, "", "hip")
    scene.text(
        "gpu-text",
        780,
        580,
        f"GPU  {gpu['model']}\nTTFT {gpu['ttft_ms']:.0f} ms   ITL {gpu['itl_ms']:.0f} ms   "
        f"{gpu['tok_s']:.1f} tok/s",
        BODY_SIZE,
        width=580,
    )
    scene.box(
        "record",
        80,
        760,
        1300,
        80,
        "run.json carries the host, device, model hash, config, and validity",
        "neutral",
    )
    return scene


def tuning(measured: dict[str, Any]) -> Scene:
    rows = measured["tune"]
    recommendation = measured["recommend"]
    scene = Scene()
    scene.text(
        "title",
        MARGIN,
        MARGIN,
        "The sweep recommends the measured knee, not the loudest number",
        TITLE_SIZE,
    )
    scene.box("grid", 60, 160, 260, 100, "grid\nquant x threads x ngl", "neutral")
    scene.text("table", 60, 300, "candidate          pp tok/s   tg tok/s", NOTE_SIZE, colour=MUTED)
    max_tg = max(float(row["tg"]) for row in rows)
    y = 340
    for index, row in enumerate(rows):
        label = f"{row['quant']} t{row['threads']} ngl{row['ngl']}"
        scene.text(f"row-{index}", 60, y, label, NOTE_SIZE, width=290)
        scene.text(f"pp-{index}", 360, y, f"{row['pp']:.0f}", NOTE_SIZE, width=80)
        scene.text(f"tg-{index}", 460, y, f"{row['tg']:.0f}", NOTE_SIZE, width=80)
        width = 240.0 * float(row["tg"]) / max_tg if max_tg > 0 else 0.0
        scene.tick(f"bar-{index}", 560, y + 2, height=14)
        if width > 0:
            scene.arrow(f"barline-{index}", 560, y + 10, width, 0, "", "analysis", label_offset=0)
        y += 46
    scene.arrow("a-grid", 320, 210, 70, 30, "measure", "neutral")
    scene.box(
        "interactive", 760, 520, 320, 120, f"interactive\n{recommendation['interactive']}", "hip"
    )
    scene.box("batch", 1120, 520, 320, 120, f"batch\n{recommendation['batch']}", "analysis")
    scene.arrow("a-recommend", 760, 640, 0, 60, "pareto", "neutral")
    scene.box(
        "evidence", 760, 760, 680, 80, "llama-bench means and stddev, three repetitions", "neutral"
    )
    scene.text(
        "note",
        60,
        800,
        "A candidate wins only when the mean gap is larger than the spread. "
        "Otherwise both are named.",
        NOTE_SIZE,
        colour=MUTED,
        width=660,
    )
    return scene


def main() -> int:
    measured = load_measured()
    FIGURES.mkdir(parents=True, exist_ok=True)
    for name, scene in (
        ("architecture", architecture(measured)),
        ("bench-methodology", methodology(measured)),
        ("tuning-frontier", tuning(measured)),
    ):
        path = scene.save(name)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
