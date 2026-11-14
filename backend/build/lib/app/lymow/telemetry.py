# Derived from Lymow-One-MQTT (https://github.com/Mortimer452/Lymow-One-MQTT),
# Copyright (c) 2026 Aaron Gibbs, MIT License — see LICENSE-lymow-one-mqtt.
"""Telemetry snapshot: typed state fields derived from Lymow field tables.

Single source of truth for BOTH the simulated mower and the live MQTT
path: `StateSnapshot` is the typed model serialized to the frontend, and
`telemetry_from_wire()` derives it from the raw protobuf state dict.
Keeping one authoritative field table means the simulator and the real
mower emit identical schemas.
"""
from __future__ import annotations

from app.lymow import const
from app.lymow.state import derive_current_zone, is_task_active


def telemetry_from_wire(state: dict) -> dict:
    """Derive the telemetry field set from a merged protobuf state dict.

    Reads only fields verified in Lymow-One-MQTT (PbRobotInfo,
    PbCleanInfo, PbLocalizationInfo, PbRtkDiagnosticL1,
    PbNetDetailInfo, PbDeviceProfile). Missing sub-messages yield None
    rather than invented defaults.
    """
    t: dict = {}
    ri = state.get("robotInfo")
    if ri is not None:
        t["work_status"] = getattr(ri, "workStatus", None)
        t["robot_status"] = getattr(ri, "robotStatus", None)
        t["battery"] = getattr(ri, "battery", None)
        t["is_charging"] = bool(getattr(ri, "isCharging", False))
        t["is_recharging"] = bool(getattr(ri, "isRecharging", False))
        t["wifi_signal_quality"] = getattr(ri, "wifiSignalQuality", None)
        t["lte_signal_quality"] = getattr(ri, "lteSignalQuality", None)
        t["wifi_working"] = bool(getattr(ri, "wifiWorking", False))
        t["lte_working"] = bool(getattr(ri, "lteWorking", False))
    ci = state.get("cleanInfo")
    if ci is not None:
        t["clean_time_s"] = getattr(ci, "cleanTime", None)
        t["clean_area_m2"] = getattr(ci, "cleanArea", None)
        t["clean_percent"] = getattr(ci, "cleanPercent", None)
        t["remain_clean_time_s"] = getattr(ci, "remainCleanTime", None)
        t["map_area_m2"] = getattr(ci, "mapArea", None)
    li = state.get("localizationInfo")
    if li is not None:
        t["horizontal_accuracy_m"] = getattr(li, "horizontalAccuracy", None)
        t["vertical_accuracy_m"] = getattr(li, "verticalAccuracy", None)
        t["position_quality"] = getattr(li, "positionQuality", None)
        t["loc_node_status"] = getattr(li, "locNodeStatus", None)
        t["num_satellites"] = getattr(li, "numSatellites", None)
    l1 = state.get("rtkDiagnosticL1")
    if l1 is not None:
        t["rtk_status"] = getattr(l1, "rtkStatus", None)
        t["rtk_precision_m"] = getattr(l1, "precision", None)
        t["rtk_satellite_count"] = getattr(l1, "satelliteCount", None)
        t["rtk_base_station_status"] = getattr(l1, "baseStationStatus", None)
    nd = state.get("netDetailInfo")
    if nd is not None:
        t["current_net"] = getattr(nd, "currentNet", None)
        t["wifi_name"] = getattr(nd, "wifiName", None)
        t["wifi_ip"] = getattr(nd, "wifiIp", None)
        t["wifi_signal_dbm"] = getattr(nd, "wifiSignal", None)
        t["sim_signal_dbm"] = getattr(nd, "simSignal", None)
        t["sim_card_status"] = getattr(nd, "simCardStatus", None)
    di = state.get("deviceInfo")
    if di is not None:
        t["fw_version"] = getattr(di, "fwVersion", None)
        t["mcu_version"] = getattr(di, "mcuVersion", None)
        t["ip_address"] = getattr(di, "ipAddress", None)
        t["serial_number"] = getattr(di, "sn", None)
    if "errorCodes" in state:
        t["error_codes"] = list(state["errorCodes"])
    if "warningCodes" in state:
        t["warning_codes"] = list(state["warningCodes"])
    t["task_active"] = is_task_active(state)
    zone = derive_current_zone(state)
    if zone is not None:
        t["current_zone"] = zone
    return t


def describe_status(telemetry: dict) -> dict:
    """Human-readable status view (shared by simulator and live mower).

    Uses only label tables verified in Lymow-One-MQTT const.py; unknown
    enum values are labeled generically rather than guessed.
    """
    ws = telemetry.get("work_status")
    status: dict = {
        "work_status": ws,
        "work_status_label": (
            const.work_status_label(ws) if ws is not None else "Unknown"
        ),
        "task_active": bool(telemetry.get("task_active")),
    }
    battery = telemetry.get("battery")
    if battery is not None:
        status["battery"] = battery
    if telemetry.get("is_recharging"):
        status["phase"] = "recharging"
    elif telemetry.get("is_charging"):
        status["phase"] = "charging"
    elif status["task_active"]:
        status["phase"] = "task"
    else:
        status["phase"] = "idle"
    rs = telemetry.get("robot_status")
    if rs == const.WORK_STATUS_ERROR:
        codes = telemetry.get("error_codes") or []
        status["error"] = {
            "code": codes[0] if codes else None,
            "label": const.error_label(codes[0]) if codes else "Error",
            "all_codes": codes,
        }
    return status
