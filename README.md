# ChiralNet Dashboard (Step 7)

Reads from `chiralnet.db` (the same SQLite file `chiralnet_backend.py` writes to)
and shows a live metrics table + an RSSI heatmap over a floor plan.

## Setup (Windows PowerShell)

```powershell
cd S:\Projects\ChiralNet
python -m pip install -r requirements.txt
```

## 1. Configure your node positions

Open `nodes_config.json` and edit:
- `floor_width` / `floor_height` — real-world dimensions of your space, in meters
- `nodes` — one entry per physical ESP32, keyed by its `device_id`
  (this is the MAC address with colons stripped — you already see it printed
  in Serial Monitor / the JSON payload, e.g. `B4BFE90E08E4`).
  `x`/`y` are that node's position in meters on your floor plan.

You currently have 1 real device (`B4BFE90E08E4`). The other two entries in
the config are placeholders for nodes 2 and 3 — update them once those are
flashed and deployed (Step 8).

**Note:** the heatmap can still render if a node is active but not yet listed
in `nodes_config.json`; the dashboard will place that node provisionally so you
still get a visible map. For the most accurate layout, add the real device IDs
and positions for every physical node.

## 2. Try it with fake data first (optional, no hardware needed)

```powershell
python seed_test_data.py
```
This drops a few fake readings into `chiralnet.db` so you can see the
dashboard fully working before your ESP32/MQTT/backend chain (Steps 5-6)
is finished.

## 3. Run the dashboard

```powershell
python chiralnet_dashboard.py
```

Open **http://localhost:5000** in a browser. The table and heatmap both
auto-refresh every 5 seconds.

## 4. Use with real data

Once `chiralnet_backend.py` (Step 6) is running and receiving real MQTT
messages, just leave it running in one terminal and run
`chiralnet_dashboard.py` in another — both read/write the same
`chiralnet.db`, so real readings will appear automatically.
