"""
ChiralNet Dashboard
--------------------
Reads telemetry written by chiralnet_backend.py (chiralnet.db) and serves:
  - a live metrics table (latest reading per node)
  - an RSSI heatmap plotted over your floor plan (node positions from nodes_config.json)

Run:
    python chiralnet_dashboard.py
Then open:
    http://localhost:5000
"""

import io
import json
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless rendering, no GUI backend needed
import matplotlib.pyplot as plt
import numpy as np
from flask import Flask, jsonify, render_template, send_file
from scipy.interpolate import griddata

from chiralnet_diagnostics import diagnose

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "chiralnet.db"
CONFIG_FILE = BASE_DIR / "nodes_config.json"

app = Flask(__name__)


# ---------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------
def load_node_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def init_db_if_missing():
    """Create the telemetry table if chiralnet.db / the table doesn't exist yet
    (matches the schema chiralnet_backend.py writes to)."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
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


def get_latest_readings():
    """One row per device_id: its most recent reading."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT t.*
        FROM telemetry t
        INNER JOIN (
            SELECT device_id, MAX(id) AS max_id
            FROM telemetry
            GROUP BY device_id
        ) latest
        ON t.device_id = latest.device_id AND t.id = latest.max_id
        ORDER BY t.received_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rolling_averages(window=10):
    """Average latency/packet_loss over each device's last `window` readings.
    Needed because pingCount=1 on the firmware makes any single reading's
    latency/packet_loss noisy (packet_loss in particular becomes binary
    0%/100% per cycle) - averaging recent readings smooths that back out
    without slowing down the firmware's publish rate."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    device_ids = [r["device_id"] for r in conn.execute(
        "SELECT DISTINCT device_id FROM telemetry"
    ).fetchall()]

    result = {}
    for device_id in device_ids:
        rows = conn.execute("""
            SELECT latency_ms, packet_loss FROM telemetry
            WHERE device_id = ? AND latency_ms IS NOT NULL
            ORDER BY id DESC LIMIT ?
        """, (device_id, window)).fetchall()
        if rows:
            latencies = [r["latency_ms"] for r in rows]
            losses = [r["packet_loss"] for r in rows]
            result[device_id] = {
                "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
                "avg_packet_loss": round(sum(losses) / len(losses), 1),
                "sample_count": len(rows),
            }
    conn.close()
    return result


def get_rssi_history(device_id, window=10):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("""
        SELECT rssi FROM telemetry
        WHERE device_id = ? AND rssi IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (device_id, window)).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------
# Heatmap rendering
# ---------------------------------------------------------------------
def render_heatmap_png():
    config = load_node_config()
    node_positions = config["nodes"]
    fw, fh = config["floor_width"], config["floor_height"]

    latest = {r["device_id"]: r for r in get_latest_readings()}

    xs, ys, rssis, labels = [], [], [], []
    for device_id, pos in node_positions.items():
        if device_id in latest:
            xs.append(pos["x"])
            ys.append(pos["y"])
            rssis.append(latest[device_id]["rssi"])
            labels.append(pos.get("label", device_id))

    fig, ax = plt.subplots(figsize=(8, 8 * fh / fw))

    if len(xs) >= 3:
        # interpolate RSSI across the floor plan
        grid_x, grid_y = np.mgrid[0:fw:200j, 0:fh:200j]
        grid_z = griddata((xs, ys), rssis, (grid_x, grid_y), method="linear")
        # fill any NaNs outside the convex hull of node points using nearest
        nearest = griddata((xs, ys), rssis, (grid_x, grid_y), method="nearest")
        grid_z = np.where(np.isnan(grid_z), nearest, grid_z)

        im = ax.imshow(
            grid_z.T, extent=(0, fw, 0, fh), origin="lower",
            cmap="RdYlGn", vmin=-90, vmax=-30, alpha=0.85, aspect="auto"
        )
        fig.colorbar(im, ax=ax, label="RSSI (dBm)")
    elif len(xs) > 0:
        ax.text(
            fw / 2, fh / 2,
            "Add 3+ nodes to nodes_config.json\nfor a full interpolated heatmap.\n"
            "Showing raw node readings only for now.",
            ha="center", va="center", fontsize=10, color="gray"
        )

    # plot node markers + labels on top regardless
    for x, y, rssi, label in zip(xs, ys, rssis, labels):
        ax.scatter(x, y, c="black", s=60, zorder=5, edgecolors="white")
        ax.annotate(
            f"{label}\n{rssi} dBm", (x, y),
            textcoords="offset points", xytext=(8, 8),
            fontsize=9, fontweight="bold", zorder=6
        )

    ax.set_xlim(0, fw)
    ax.set_ylim(0, fh)
    ax.set_xlabel("meters")
    ax.set_ylabel("meters")
    ax.set_title(config.get("floor_label", "ChiralNet RSSI Heatmap"))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/latest")
def api_latest():
    latest = get_latest_readings()
    rolling = get_rolling_averages(window=10)
    for row in latest:
        avg = rolling.get(row["device_id"])
        if avg:
            row["avg_latency_ms"] = avg["avg_latency_ms"]
            row["avg_packet_loss"] = avg["avg_packet_loss"]
            row["avg_sample_count"] = avg["sample_count"]
    return jsonify(latest)


@app.route("/api/heatmap.png")
def api_heatmap():
    buf = render_heatmap_png()
    return send_file(buf, mimetype="image/png")


@app.route("/api/diagnosis")
def api_diagnosis():
    latest = get_latest_readings()
    rolling = get_rolling_averages(window=10)
    results = []
    for row in latest:
        device_id = row["device_id"]
        avg = rolling.get(device_id, {})
        history = get_rssi_history(device_id, window=10)
        diag = diagnose(
            rssi=row.get("rssi"),
            avg_latency_ms=avg.get("avg_latency_ms"),
            avg_packet_loss=avg.get("avg_packet_loss"),
            nearby_aps=row.get("nearby_aps"),
            rssi_history=history,
        )
        diag["device_id"] = device_id
        diag["ssid"] = row.get("ssid")
        results.append(diag)
    return jsonify(results)


if __name__ == "__main__":
    init_db_if_missing()
    print(f"Reading from: {DB_FILE}")
    print(f"Node config:  {CONFIG_FILE}")
    print("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)