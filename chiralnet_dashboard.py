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
import paho.mqtt.publish as mqtt_publish
from datetime import datetime, timedelta
import matplotlib.colors as mcolors

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

node_modes = {}
critical_counts = {}
CRITICAL_THRESHOLD = 3
AUTO_LOG = []


def send_command(device_id, mode):
    mqtt_publish.single(f"chiralnet/commands/{device_id}", mode, hostname="localhost")
    node_modes[device_id] = mode


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
    
    all_latest = [dict(r) for r in rows]
    active_latest = []
    now = datetime.now()
    for r in all_latest:
        try:
            # Handle potential missing microseconds in isoformat
            dt = datetime.fromisoformat(r["received_at"].split('.')[0])
            if now - dt < timedelta(minutes=2):
                active_latest.append(r)
        except Exception:
            pass
    return active_latest


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

    def fallback_position(index, total):
        if total <= 1:
            return fw / 2, fh / 2
        if total == 2:
            points = [(fw * 0.33, fh * 0.5), (fw * 0.67, fh * 0.5)]
            return points[index]

        angle = (2 * np.pi * index) / total
        radius = min(fw, fh) * 0.28
        x = fw / 2 + np.cos(angle) * radius
        y = fh / 2 + np.sin(angle) * radius
        return float(np.clip(x, 0, fw)), float(np.clip(y, 0, fh))

    xs, ys, rssis, labels = [], [], [], []
    active_ids = [device_id for device_id in latest if latest[device_id].get("rssi") is not None]
    active_ids.sort()
    missing_positions = []
    for index, device_id in enumerate(active_ids):
        pos = node_positions.get(device_id)
        if pos is None:
            missing_positions.append(device_id)
            x, y = fallback_position(index, len(active_ids))
            label = device_id
        else:
            x, y = pos["x"], pos["y"]
            label = pos.get("label", device_id)

        xs.append(x)
        ys.append(y)
        rssis.append(latest[device_id]["rssi"])
        labels.append(label)

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(8, 8 * fh / fw), facecolor="#04080f")
    ax.set_facecolor("#04080f")

    if len(xs) > 0:
        grid_x, grid_y = np.mgrid[0:fw:200j, 0:fh:200j]
        grid_z = np.full_like(grid_x, -100.0)
        
        # Smooth radial decay blending
        for ix in range(200):
            for iy in range(200):
                px, py = grid_x[ix, iy], grid_y[ix, iy]
                max_sig = -100
                for x, y, rssi in zip(xs, ys, rssis):
                    dist = np.sqrt((px - x)**2 + (py - y)**2)
                    sig = rssi - (dist * 2.5) # Signal decay factor
                    if sig > max_sig: max_sig = sig
                grid_z[ix, iy] = max_sig

        colors = ["#04080f", "#004466", "#00c8ff", "#00ff99", "#ffbb00", "#ff3d6b"]
        cmap = mcolors.LinearSegmentedColormap.from_list("cyber", colors)

        im = ax.imshow(
            grid_z.T, extent=(0, fw, 0, fh), origin="lower",
            cmap=cmap, vmin=-90, vmax=-30, alpha=0.9, aspect="auto"
        )
        
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("RSSI (dBm)", color="#5a7399", fontsize=9)
        cbar.ax.yaxis.set_tick_params(color="#5a7399", labelcolor="#5a7399")
        cbar.outline.set_edgecolor("#334155")
    else:
        ax.text(fw/2, fh/2, "WAITING FOR ACTIVE NODES", ha="center", va="center", 
                color="#5a7399", fontsize=12, fontweight="bold", alpha=0.5)

    if missing_positions:
        ax.text(
            0.5, 0.98,
            f"{len(missing_positions)} node(s) using provisional positions",
            transform=ax.transAxes, ha="center", va="top",
            color="#5a7399", fontsize=8, alpha=0.75,
        )

    for x, y, rssi, label in zip(xs, ys, rssis, labels):
        ax.scatter(x, y, c="#00c8ff", s=400, alpha=0.15, edgecolors="none", zorder=4)
        ax.scatter(x, y, c="#e2eaf8", s=35, zorder=5, edgecolors="#00c8ff", linewidths=2)
        ax.annotate(
            f"{label}\n{rssi} dBm", (x, y),
            textcoords="offset points", xytext=(12, 12),
            fontsize=8, fontweight="bold", color="#e2eaf8", zorder=6,
            bbox=dict(boxstyle="round,pad=0.4", fc="#081224", ec="#00c8ff", alpha=0.8, lw=1)
        )

    ax.set_xlim(0, fw)
    ax.set_ylim(0, fh)
    ax.set_xlabel("Meters", color="#5a7399", fontsize=9)
    ax.set_ylabel("Meters", color="#5a7399", fontsize=9)
    ax.tick_params(colors="#5a7399", labelsize=8)
    for spine in ax.spines.values(): spine.set_color("#334155")
    ax.grid(color="#00c8ff", alpha=0.1, linestyle="--")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#04080f")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/set-mode/<device_id>", methods=["POST"])
def api_set_mode(device_id):
    from flask import request
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode")
    if mode not in ("monitor", "repeater"):
        return jsonify({"error": "mode must be 'monitor' or 'repeater'"}), 400
    try:
        send_command(device_id, mode)
        critical_counts[device_id] = 0
        return jsonify({"status": "sent", "device_id": device_id, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/swarm", methods=["POST"])
def api_swarm():
    try:
        mqtt_publish.single("chiralnet/commands/all", "swarm", hostname="localhost")
        AUTO_LOG.append("SYSTEM: Swarm Provisioning Master Mode ACTIVATED (5m)")
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        
        current_mode = node_modes.get(device_id, "monitor")
        if diag["severity"] == "critical":
            critical_counts[device_id] = critical_counts.get(device_id, 0) + 1
        else:
            critical_counts[device_id] = 0

        if critical_counts.get(device_id, 0) >= CRITICAL_THRESHOLD and current_mode != "repeater":
            send_command(device_id, "repeater")
            AUTO_LOG.append(f"{device_id}: auto-switched to REPEATER (critical x{CRITICAL_THRESHOLD})")
            current_mode = "repeater"
        elif diag["severity"] == "good" and current_mode == "repeater":
            send_command(device_id, "monitor")
            AUTO_LOG.append(f"{device_id}: auto-reverted to MONITOR (healthy again)")
            current_mode = "monitor"

        diag["mode"] = current_mode
        diag["critical_streak"] = critical_counts.get(device_id, 0)
        results.append(diag)
    return jsonify(results)


@app.route("/api/history/<device_id>")
def api_history(device_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT rssi, latency_ms, packet_loss, received_at
        FROM telemetry
        WHERE device_id = ?
        ORDER BY id DESC LIMIT 50
    """, (device_id,)).fetchall()
    conn.close()
    return jsonify(list(reversed([dict(r) for r in rows])))


@app.route("/api/auto-log")
def api_auto_log():
    return jsonify(AUTO_LOG[-10:])


if __name__ == "__main__":
    init_db_if_missing()
    print(f"Reading from: {DB_FILE}")
    print(f"Node config:  {CONFIG_FILE}")
    print("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)