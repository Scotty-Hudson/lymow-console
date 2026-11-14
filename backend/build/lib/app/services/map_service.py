"""MapService — read-only map view for the Leaflet frontend.

Read-only by design: the console never writes map geometry. Zone editing
stays in the official Lymow app (the community protocol's edit paths are
unverified and geometry mistakes are unrecoverable without re-mapping).
"""
from __future__ import annotations

from typing import Any

from app.lymow.state import enu_to_lla, zone_at_pose
from app.services.state_service import MowerStateService

# Features that the community protocol reverse-engineering could NOT
# deliver a schema for (documented in docs/PROTOCOL.md):
UNSUPPORTED = [
    {
        "feature": "no_go_zones",
        "reason": "PbNoGoZone is an empty schema placeholder in the "
                  "extracted protocol; geometry cannot be decoded.",
    },
    {
        "feature": "mowing_path",
        "reason": "PbPath is an empty schema placeholder; the mower's "
                  "coverage path is not published on the wire.",
    },
]


class MapService:
    def __init__(self, state: MowerStateService) -> None:
        self.state = state

    def get_map(self) -> dict[str, Any]:
        catalog = self.state.state.get("zone_catalog")
        if catalog is None or not catalog.zones:
            return {
                "available": False,
                "zones": [],
                "channels": [],
                "dock": None,
                "pose": None,
                "mower_latlon": None,
                "enu_base": None,
                "current_zone": None,
                "unsupported": UNSUPPORTED,
            }
        snap = self.state.snapshot()
        task_hashes = {z["hash_id"] for z in snap["task_zones"]}
        zones = [
            {
                "hash_id": z.hash_id,
                "name": z.name,
                "polygon": [list(p) for p in z.polygon_points],
                "is_enabled": z.is_enabled,
                "mow_order": z.mow_order,
                "in_current_task": z.hash_id in task_hashes,
            }
            for z in catalog.zones
        ]
        channels = [
            {
                "hash_id": c.hash_id,
                "zone1": c.zone1,
                "zone2": c.zone2,
                "is_docking_channel": c.is_docking_channel,
                "polygon": [list(p) for p in c.polygon_points],
            }
            for c in catalog.channels
        ]
        cs = self.state.state.get("chargingStationLoc")
        dock = {"x": cs.x, "y": cs.y, "theta": cs.theta} if cs is not None else None
        pose = snap["pose"]
        mower_latlon = None
        ebp = catalog.enu_base_point
        raw_pose = self.state.state.get("pose")
        if ebp is not None and raw_pose is not None:
            ll = enu_to_lla(ebp, raw_pose)
            if ll is not None:
                mower_latlon = {"lat": ll[0], "lon": ll[1]}
        current = zone_at_pose(self.state.state)
        return {
            "available": True,
            "zones": zones,
            "channels": channels,
            "dock": dock,
            "pose": pose,
            "mower_latlon": mower_latlon,
            "enu_base": (
                {"lat": ebp.latitude, "lon": ebp.longitude, "altitude": ebp.altitude}
                if ebp is not None else None
            ),
            "current_zone": current.hash_id if current is not None else None,
            "frame": "local ENU meters, origin at surveyed RTK base point",
            "unsupported": UNSUPPORTED,
        }
