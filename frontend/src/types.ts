// Mirrors backend app/models/api.py — the single shared contract.

export interface Telemetry {
  work_status?: number;
  robot_status?: number;
  battery?: number;
  is_charging?: boolean;
  is_recharging?: boolean;
  wifi_signal_quality?: number;
  lte_signal_quality?: number;
  wifi_working?: boolean;
  lte_working?: boolean;
  clean_time_s?: number;
  clean_area_m2?: number;
  clean_percent?: number;
  remain_clean_time_s?: number;
  map_area_m2?: number;
  horizontal_accuracy_m?: number;
  vertical_accuracy_m?: number;
  position_quality?: number;
  loc_node_status?: number;
  num_satellites?: number;
  rtk_status?: number;
  rtk_precision_m?: number;
  rtk_satellite_count?: number;
  rtk_base_station_status?: number;
  current_net?: number;
  wifi_name?: string;
  wifi_ip?: string;
  wifi_signal_dbm?: number;
  sim_signal_dbm?: number;
  fw_version?: string;
  mcu_version?: string;
  ip_address?: string;
  serial_number?: string;
  error_codes?: number[];
  warning_codes?: number[];
  task_active?: boolean;
  current_zone?: string;
}

export interface StateSnapshot {
  device_id: string;
  online: boolean;
  simulator: boolean;
  stale: boolean;
  map_ready: boolean;
  activity: string | null;
  status: {
    work_status?: number;
    work_status_label?: string;
    task_active?: boolean;
    phase?: string;
    battery?: number;
    error?: { code: number | null; label: string; all_codes: number[] };
  };
  telemetry: Telemetry;
  allowed_actions: Record<"start_mowing" | "pause" | "dock", number | null>;
  task_zones: { hash_id: string; name: string; mow_order: number }[];
  pose: { x: number; y: number; theta: number } | null;
  last_update_epoch: number | null;
}

export interface MapDocument {
  available: boolean;
  zones: {
    hash_id: string;
    name: string;
    polygon: [number, number][];
    is_enabled: boolean;
    mow_order: number;
    in_current_task: boolean;
  }[];
  channels: {
    hash_id: string;
    zone1: string;
    zone2: string;
    is_docking_channel: boolean;
    polygon: [number, number][];
  }[];
  dock: { x: number; y: number; theta: number } | null;
  pose: { x: number; y: number; theta: number } | null;
  mower_latlon: { lat: number; lon: number } | null;
  enu_base: { lat: number; lon: number; altitude: number } | null;
  current_zone: string | null;
  frame?: string;
  unsupported: { feature: string; reason: string }[];
}

export interface HistoryTask {
  id: number;
  started_at: number;
  ended_at: number | null;
  end_type: number | null;
  duration_s: number | null;
  area_m2: number | null;
  used_battery: number | null;
  zones?: string[];
  simulator: boolean;
}

export interface CommandResult {
  action: string;
  outcome: string;
  confirmed: boolean;
}
