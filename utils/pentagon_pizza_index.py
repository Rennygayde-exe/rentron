from __future__ import annotations

import base64
import io
import json
import logging
import math
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Dict, List, Optional

HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "pentagon_pizza_history.json"
SUPER_BUSY_THRESHOLD = 0.82
FORTRAN_MODULE_NAME = "_pentagon_pizza_index"

FORTRAN_SOURCE = r"""
      subroutine compute_pizza_index(busy, capacity, index)
          implicit none
          integer, intent(in) :: busy
          integer, intent(in) :: capacity
          real*8, intent(out) :: index
          real*8 :: ratio
          if (capacity .le. 0) then
              index = 0.0d0
              return
          endif
          if (busy .lt. 0) then
              ratio = 0.0d0
          else
              ratio = dble(busy) / dble(capacity)
          endif
          if (ratio .gt. 3.0d0) then
              ratio = 3.0d0
          endif
          index = 1.0d0 - dexp(-2.1d0 * ratio)
      end subroutine compute_pizza_index
"""

_fortran_module = None


def _compile_fortran_module() -> Optional[Any]:
    """
    Try to compile the tiny Fortran helper with f2py. Falls back to Python math if
    compilation fails (e.g., no gfortran available).
    """
    global _fortran_module
    if _fortran_module is not None:
        return _fortran_module

    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="pizza_index_f2py_"))
        source_file = tmp_dir / f"{FORTRAN_MODULE_NAME}.f90"
        source_file.write_text(FORTRAN_SOURCE, encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "numpy.f2py",
            "-c",
            str(source_file),
            "-m",
            FORTRAN_MODULE_NAME,
        ]
        subprocess.run(cmd, cwd=tmp_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        so_files = list(tmp_dir.glob(f"{FORTRAN_MODULE_NAME}*.so")) + list(tmp_dir.glob(f"{FORTRAN_MODULE_NAME}*.pyd"))
        if not so_files:
            return None
        spec = importlib_util.spec_from_file_location(FORTRAN_MODULE_NAME, so_files[0])
        if spec and spec.loader:
            module = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _fortran_module = module
            return module
    except Exception as exc:
        logging.debug("Fortran pizza index compile failed: %s", exc)
    return None


def _python_index(busy_count: int, capacity: int) -> float:
    busy = max(0, int(busy_count))
    cap = max(1, int(capacity))
    ratio = busy / cap
    ratio = min(ratio, 3.0)
    return 1.0 - math.exp(-2.1 * ratio)


def compute_pizza_index(busy_count: int, capacity: int = 12) -> float:
    module = _compile_fortran_module()
    if module and hasattr(module, "compute_pizza_index"):
        try:
            return float(module.compute_pizza_index(int(busy_count), int(capacity)))
        except Exception as exc:
            logging.debug("Fortran pizza index call failed, using Python fallback: %s", exc)
    return _python_index(busy_count, capacity)


def _ensure_history_dir() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_history() -> List[Dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logging.warning("Failed to read pizza history: %s", exc)
        return []


def record_observation(busy_count: int, capacity: int = 12, source: Optional[str] = None) -> Dict[str, Any]:
    index = compute_pizza_index(busy_count, capacity)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "busy_count": int(busy_count),
        "capacity": int(capacity),
        "index": index,
        "source": source or "manual",
    }
    history = load_history()
    history.append(entry)
    # Keep the file from growing unbounded.
    if len(history) > 512:
        history = history[-512:]
    _ensure_history_dir()
    with HISTORY_PATH.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2)
    return entry


def summarize(history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    history = history if history is not None else load_history()
    if not history:
        return {
            "latest": None,
            "average_index": 0.0,
            "max_index": 0.0,
            "super_busy": False,
        }
    latest = history[-1]
    avg_index = sum(entry.get("index", 0.0) for entry in history) / len(history)
    max_index = max(entry.get("index", 0.0) for entry in history)
    super_busy = bool(latest.get("index", 0.0) >= SUPER_BUSY_THRESHOLD)
    return {
        "latest": latest,
        "average_index": avg_index,
        "max_index": max_index,
        "super_busy": super_busy,
    }


def _parse_timestamp(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(timezone.utc)


def render_history_plot(history: Optional[List[Dict[str, Any]]] = None) -> str:
    history = history if history is not None else load_history()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(9, 4))
    if not history:
        ax1.text(0.5, 0.5, "No Pentagon pizza data yet", ha="center", va="center", fontsize=12)
        ax1.axis("off")
    else:
        times = [_parse_timestamp(entry.get("timestamp", "")) for entry in history]
        busy_counts = [entry.get("busy_count", 0) for entry in history]
        indexes = [entry.get("index", 0.0) for entry in history]

        color_busy = "#ed6a5a"
        color_index = "#68c6ff"
        ax1.plot(times, busy_counts, color=color_busy, linewidth=2.2, label="Busy pizza spots")
        ax1.set_ylabel("Busy locations", color=color_busy)
        ax1.tick_params(axis="y", labelcolor=color_busy)

        ax2 = ax1.twinx()
        ax2.plot(times, indexes, color=color_index, linewidth=2.4, linestyle="--", label="Pizza index")
        ax2.set_ylabel("Pentagon pizza index", color=color_index)
        ax2.tick_params(axis="y", labelcolor=color_index)
        ax2.axhline(SUPER_BUSY_THRESHOLD, color="#ffbf00", linewidth=1.5, linestyle=":", label="Super busy threshold")

        ax1.set_xlabel("Timestamp (UTC)")
        fig.autofmt_xdate(rotation=20)
        lines, labels = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines + lines2, labels + labels2, loc="upper left", framealpha=0.85)
        ax1.grid(alpha=0.2)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150)
    plt.close(fig)
    buffer.seek(0)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return encoded

