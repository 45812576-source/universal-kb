"""Chart generation builtin tool.

Input params:
{
  "title": "月度销售趋势",
  "chart_type": "bar",           # bar | line | pie | bar_line
  "labels": ["1月", "2月", "3月"],
  "datasets": [
    {"name": "销售额", "values": [100, 200, 150]},
    {"name": "目标", "values": [120, 180, 160]}
  ],
  "x_label": "月份",              # optional
  "y_label": "万元",              # optional
}

Output: {"file_id": "xxx.png", "download_url": "/api/files/xxx.png", "filename": "月度销售趋势.png"}
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

_UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./uploads")
_GENERATED_DIR = Path(_UPLOAD_DIR) / "generated"

# Chinese font fallback
def _setup_chinese_font():
    for font_name in ["SimHei", "Arial Unicode MS", "PingFang SC", "Heiti SC"]:
        try:
            fm.findfont(fm.FontProperties(family=font_name), fallback_to_default=False)
            plt.rcParams["font.family"] = font_name
            plt.rcParams["axes.unicode_minus"] = False
            return
        except Exception:
            continue
    # fallback — DejaVu Sans (no Chinese, but won't crash)
    plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False


def execute(params: dict) -> dict:
    """Generate a chart PNG and return file_id + download_url."""
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    _setup_chinese_font()

    title = params.get("title", "图表")
    chart_type = params.get("chart_type", "bar")
    labels = params.get("labels", [])
    datasets = params.get("datasets", [])
    x_label = params.get("x_label", "")
    y_label = params.get("y_label", "")

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#FAFAFA")
    ax.set_facecolor("#FAFAFA")

    colors = ["#4472C4", "#ED7D31", "#A9D18E", "#FF0000", "#5B9BD5", "#70AD47"]

    if chart_type == "pie":
        _draw_pie(ax, labels, datasets, colors, title)
    elif chart_type == "line":
        _draw_line(ax, labels, datasets, colors, title, x_label, y_label)
    elif chart_type == "bar_line":
        fig, ax = plt.subplots(figsize=(10, 6))
        fig.patch.set_facecolor("#FAFAFA")
        _draw_bar_line(fig, ax, labels, datasets, colors, title, x_label, y_label)
    else:
        _draw_bar(ax, labels, datasets, colors, title, x_label, y_label)

    if chart_type not in ("pie", "bar_line"):
        if x_label:
            ax.set_xlabel(x_label, fontsize=11)
        if y_label:
            ax.set_ylabel(y_label, fontsize=11)
        if len(datasets) > 1:
            ax.legend(loc="best")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    plt.tight_layout()

    file_id = f"{uuid.uuid4().hex}.png"
    file_path = _GENERATED_DIR / file_id
    fig.savefig(str(file_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return {
        "file_id": file_id,
        "download_url": f"/api/files/{file_id}",
        "filename": f"{title}.png",
        "message": f"图表已生成：{title}",
    }


def _draw_bar(ax, labels, datasets, colors, title, x_label, y_label):
    n = len(datasets)
    x = range(len(labels))
    width = 0.8 / max(n, 1)
    offsets = [i * width - (n - 1) * width / 2 for i in range(n)]

    for i, ds in enumerate(datasets):
        bars = ax.bar(
            [xi + offsets[i] for xi in x],
            ds.get("values", []),
            width=width * 0.9,
            label=ds.get("name", f"系列{i+1}"),
            color=colors[i % len(colors)],
            alpha=0.85,
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)


def _draw_line(ax, labels, datasets, colors, title, x_label, y_label):
    x = range(len(labels))
    for i, ds in enumerate(datasets):
        ax.plot(
            list(x),
            ds.get("values", []),
            marker="o",
            label=ds.get("name", f"系列{i+1}"),
            color=colors[i % len(colors)],
            linewidth=2,
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)


def _draw_pie(ax, labels, datasets, colors, title):
    values = datasets[0].get("values", []) if datasets else []
    ax.pie(
        values,
        labels=labels,
        colors=colors[:len(labels)],
        autopct="%1.1f%%",
        startangle=140,
        pctdistance=0.85,
    )
    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)


def _draw_bar_line(fig, ax, labels, datasets, colors, title, x_label, y_label):
    """First dataset as bars on left Y, remaining as lines on right Y."""
    ax.set_facecolor("#FAFAFA")
    x = range(len(labels))

    if datasets:
        bars = ax.bar(
            list(x),
            datasets[0].get("values", []),
            color=colors[0],
            alpha=0.75,
            label=datasets[0].get("name", "柱状"),
        )
        ax.set_ylabel(datasets[0].get("name", y_label or ""), fontsize=11, color=colors[0])

    if len(datasets) > 1:
        ax2 = ax.twinx()
        ax2.set_facecolor("#FAFAFA")
        for i, ds in enumerate(datasets[1:], 1):
            ax2.plot(
                list(x),
                ds.get("values", []),
                marker="o",
                color=colors[i % len(colors)],
                linewidth=2,
                label=ds.get("name", f"折线{i}"),
            )
        ax2.set_ylabel(datasets[1].get("name", "") if len(datasets) > 1 else "", fontsize=11)
        ax2.spines["top"].set_visible(False)

        # Combine legends
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    elif datasets:
        ax.legend(loc="upper left")

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=10)
    if x_label:
        ax.set_xlabel(x_label, fontsize=11)
    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)
    ax.spines["top"].set_visible(False)
