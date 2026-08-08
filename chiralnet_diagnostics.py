"""
ChiralNet Diagnostic Engine
----------------------------
Rule-based root-cause analysis. Takes a node's current + recent metrics and
outputs:
  - diagnosis   (human-readable root cause)
  - severity    ("good" | "warning" | "critical")
  - confidence  (0-100, how strongly the evidence matches this rule)
  - reasoning   (list of short evidence bullets)
  - action      (concrete suggested fix)

Rules are checked in priority order - first match wins. This mirrors how a
human would triage: rule out "is the signal just too weak" before looking
at congestion/ISP-side explanations, since a weak signal makes every other
metric unreliable anyway.
"""

import statistics

# ---- Thresholds (tune these based on your real deployment data) ----
RSSI_WEAK = -75          # below this: treat as a coverage problem
RSSI_VERY_WEAK = -85     # below this: high-confidence dead zone
LATENCY_HIGH = 80        # ms, above this: "high" latency
PACKET_LOSS_HIGH = 10    # %, above this: "high" loss
NEARBY_APS_MANY = 15     # above this: congested RF environment
NEARBY_APS_FEW = 5       # at/below this: sparse RF environment (rules out congestion)
RSSI_FLUCTUATION_STDDEV = 8  # dBm, above this: unstable/moving signal


def _clamp(value, lo=0, hi=99):
    return int(round(max(lo, min(hi, value))))


def diagnose(rssi, avg_latency_ms, avg_packet_loss, nearby_aps, rssi_history=None):
    """
    rssi              : latest RSSI reading (dBm, int)
    avg_latency_ms     : rolling average latency (ms)
    avg_packet_loss    : rolling average packet loss (%)
    nearby_aps         : latest nearby AP count
    rssi_history       : optional list of recent RSSI values, for fluctuation detection

    Returns a dict: {diagnosis, severity, confidence, reasoning, action}
    """
    reasoning = [
        f"RSSI: {rssi} dBm",
        f"Avg Latency: {avg_latency_ms} ms" if avg_latency_ms is not None else "Avg Latency: N/A",
        f"Avg Packet Loss: {avg_packet_loss}%" if avg_packet_loss is not None else "Avg Packet Loss: N/A",
        f"Nearby APs: {nearby_aps}",
    ]

    # ---- Rule 1: Weak signal / dead zone ----
    if rssi is not None and rssi < RSSI_WEAK:
        if rssi < RSSI_VERY_WEAK:
            confidence = _clamp(85 + (RSSI_VERY_WEAK - rssi))
        else:
            confidence = _clamp(60 + (RSSI_WEAK - rssi) * 2)
        return {
            "diagnosis": "Weak Signal / Dead Zone",
            "severity": "critical" if rssi < RSSI_VERY_WEAK else "warning",
            "confidence": confidence,
            "reasoning": reasoning,
            "action": (
                "Move this node closer to the router, remove obstructions "
                "(walls/metal) between them, or deploy an additional "
                "Wi-Fi repeater / ChiralNet relay node near this location "
                "to extend coverage into this area."
            ),
        }

    # ---- Rule 2: Router or interference issue ----
    if avg_packet_loss is not None and avg_packet_loss > PACKET_LOSS_HIGH:
        confidence = _clamp(55 + avg_packet_loss)
        return {
            "diagnosis": "Router / Interference Issue",
            "severity": "warning",
            "confidence": confidence,
            "reasoning": reasoning,
            "action": (
                "Restart the router, check for physical interference "
                "sources near it (microwaves, Bluetooth devices, cordless "
                "phones), and confirm no cable is loose."
            ),
        }

    # ---- Rule 3: Channel congestion ----
    if (avg_latency_ms is not None and avg_latency_ms > LATENCY_HIGH
            and nearby_aps is not None and nearby_aps > NEARBY_APS_MANY):
        confidence = _clamp(50 + (nearby_aps - NEARBY_APS_MANY) * 2 + (avg_latency_ms - LATENCY_HIGH) / 5)
        return {
            "diagnosis": "Channel Congestion",
            "severity": "warning",
            "confidence": confidence,
            "reasoning": reasoning,
            "action": (
                "Switch your router to a less crowded 2.4GHz channel "
                "(1, 6, or 11), or move devices to the 5GHz band if "
                "available. Many overlapping networks nearby are likely "
                "competing for airtime."
            ),
        }

    # ---- Rule 4: Possible ISP issue ----
    # Widened from "nearby_aps <= NEARBY_APS_FEW" to "<= NEARBY_APS_MANY" so
    # the moderate-congestion range (6-15 APs) doesn't fall through both
    # this rule and Rule 3 and get misreported as "Healthy". Confidence is
    # scaled down for the ambiguous middle range since congestion hasn't
    # been fully ruled out there the way it has at <= NEARBY_APS_FEW.
    if (avg_latency_ms is not None and avg_latency_ms > LATENCY_HIGH
            and nearby_aps is not None and nearby_aps <= NEARBY_APS_MANY):
        if nearby_aps <= NEARBY_APS_FEW:
            confidence = _clamp(45 + (avg_latency_ms - LATENCY_HIGH) / 3)
            diagnosis = "Possible ISP Issue"
            action = (
                "Local Wi-Fi looks healthy (strong signal, few competing "
                "networks) but latency is still high - run a wired speed "
                "test to isolate Wi-Fi from your internet connection, and "
                "contact your ISP if the issue persists."
            )
        else:
            confidence = _clamp(30 + (avg_latency_ms - LATENCY_HIGH) / 4)
            diagnosis = "Elevated Latency, Cause Unclear"
            action = (
                "Latency is high but the RF environment isn't clearly "
                "congested or clear - could be moderate airtime "
                "contention or an upstream ISP issue. Try switching "
                "channels first (cheap to test), then a wired speed test "
                "if that doesn't help."
            )
        return {
            "diagnosis": diagnosis,
            "severity": "warning",
            "confidence": confidence,
            "reasoning": reasoning,
            "action": action,
        }

    # ---- Rule 5: Intermittent interference / movement ----
    if rssi_history and len(rssi_history) >= 5:
        stddev = statistics.pstdev(rssi_history)
        if stddev > RSSI_FLUCTUATION_STDDEV:
            confidence = _clamp(40 + (stddev - RSSI_FLUCTUATION_STDDEV) * 3)
            reasoning.append(f"RSSI fluctuation (std dev): {stddev:.1f} dBm")
            return {
                "diagnosis": "Intermittent Interference / Movement",
                "severity": "warning",
                "confidence": confidence,
                "reasoning": reasoning,
                "action": (
                    "Signal strength is swinging significantly over time. "
                    "Check for moving obstructions (people, doors, "
                    "furniture) or intermittent interference sources near "
                    "this node's path to the router."
                ),
            }

    # ---- No issues detected ----
    return {
        "diagnosis": "Healthy Connection",
        "severity": "good",
        "confidence": 90,
        "reasoning": reasoning,
        "action": "No action needed - this node's connection looks healthy.",
    }