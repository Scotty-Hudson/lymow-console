"""Tests for state_matrix.py — the lawn-mower decision table.

Ported from Lymow-One-MQTT's test_state_matrix.py (MIT, (c) 2026
Aaron Gibbs) and extended with `allowed_actions` coverage for the web
command service. The matrix is the single source of truth for:
  (work_status, robot_status, is_recharging) -> (activity, actions)
"""
from __future__ import annotations

import pytest

from app.lymow import state_matrix
from app.lymow.const import (
    USER_CTRL_CLEAN,
    USER_CTRL_DOCK,
    USER_CTRL_PAUSE,
    USER_CTRL_PAUSE_DOCK,
    USER_CTRL_RECHARGE_DOCK,
    USER_CTRL_RESUME,
    USER_CTRL_RESUME_DOCK,
    WORK_STATUS_CHARGING,
    WORK_STATUS_CHARGING_FULL,
    WORK_STATUS_DOCKING,
    WORK_STATUS_EMERGENCY_STOP,
    WORK_STATUS_ERROR,
    WORK_STATUS_ESCAPING,
    WORK_STATUS_MOWING,
    WORK_STATUS_NONE,
    WORK_STATUS_PAUSE,
    WORK_STATUS_PAUSE_DOCKING,
    WORK_STATUS_RESUME,
    WORK_STATUS_WAITING,
    WORK_STATUS_ZONE_PARTITION,
)


class TestPriorityOrder:
    """Physical state (robot_status) overrides task intent (work_status)
    for the states where they can disagree."""

    def test_rs_error_overrides_ws_mowing(self):
        """Mid-mow blade jam: rs=ERROR but ws=MOWING (firmware lag)."""
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_ERROR,
            is_recharging=False,
        )
        assert row.activity == state_matrix.ACTIVITY_ERROR
        assert row.start_mowing is None
        assert row.pause == USER_CTRL_PAUSE, "Pause doubles as Clear-Error"

    def test_rs_pause_overrides_ws_mowing(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_PAUSE,
            is_recharging=False,
        )
        assert row.activity == state_matrix.ACTIVITY_PAUSED
        assert row.start_mowing == USER_CTRL_RESUME, "Start must Resume, not fresh-start"
        assert row.pause is None, "Pause hidden (already paused)"
        assert row.dock == USER_CTRL_RECHARGE_DOCK

    def test_rs_charging_overrides_ws_docking(self):
        """Mower has arrived at dock: rs=CHARGING but ws may still say DOCKING."""
        row = state_matrix.lookup(
            work_status=WORK_STATUS_DOCKING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=True,
        )
        assert row.activity == state_matrix.ACTIVITY_DOCKED
        assert row.start_mowing == USER_CTRL_RESUME, "Saved task -> Resume"

    def test_rs_emergency_stop_blocks_all_buttons(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_EMERGENCY_STOP,
            is_recharging=False,
        )
        assert row.activity == state_matrix.ACTIVITY_ERROR
        assert row.start_mowing is None
        assert row.pause is None
        assert row.dock is None


class TestChargingForkOnIsRecharging:
    """is_recharging forks Start between 'resume saved task' and 'fresh
    mow' — the PR #2 bug class. Matrix exposes both rows side-by-side."""

    def test_charging_with_saved_task_resumes(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=True,
        )
        assert row.start_mowing == USER_CTRL_RESUME

    def test_charging_idle_starts_fresh(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING,
            robot_status=WORK_STATUS_CHARGING,
            is_recharging=False,
        )
        assert row.start_mowing == USER_CTRL_CLEAN, "No saved task -> fresh CLEAN"

    def test_charging_full_with_saved_task_resumes(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING_FULL,
            robot_status=WORK_STATUS_CHARGING_FULL,
            is_recharging=True,
        )
        assert row.start_mowing == USER_CTRL_RESUME

    def test_charging_full_idle_starts_fresh(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_CHARGING_FULL,
            robot_status=WORK_STATUS_CHARGING_FULL,
            is_recharging=False,
        )
        assert row.start_mowing == USER_CTRL_CLEAN


class TestActiveTaskStates:
    @pytest.mark.parametrize("ws", [
        WORK_STATUS_MOWING,
        WORK_STATUS_RESUME,
        WORK_STATUS_ZONE_PARTITION,
        WORK_STATUS_ESCAPING,
    ])
    def test_mowing_like_states_share_buttons(self, ws):
        row = state_matrix.lookup(
            work_status=ws, robot_status=ws, is_recharging=False
        )
        assert row.activity == state_matrix.ACTIVITY_MOWING
        assert row.pause == USER_CTRL_PAUSE
        assert row.dock == USER_CTRL_RECHARGE_DOCK, "keep-progress dock variant"


class TestDockingStates:
    def test_docking_pause_is_pause_dock(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_DOCKING,
            robot_status=WORK_STATUS_DOCKING,
            is_recharging=False,
        )
        assert row.activity == state_matrix.ACTIVITY_RETURNING
        assert row.pause == USER_CTRL_PAUSE_DOCK

    def test_pause_docking_resumes_dock(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_PAUSE_DOCKING,
            robot_status=WORK_STATUS_PAUSE_DOCKING,
            is_recharging=False,
        )
        assert row.start_mowing == USER_CTRL_RESUME_DOCK


class TestWaitingAwayFromDock:
    """GitHub #28 regression guard: away-from-dock idle needs plain
    DOCK(2); firmware silently ignores RECHARGE_DOCK(33) there."""

    def test_rs_waiting_gets_plain_dock(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_WAITING,
            robot_status=WORK_STATUS_WAITING,
            is_recharging=False,
        )
        assert row.dock == USER_CTRL_DOCK
        assert row.start_mowing == USER_CTRL_CLEAN


class TestIdleStates:
    """Idle with the physical status not saying WAITING (e.g. transient
    firmware combo): fresh CLEAN, activity docked. rs=WAITING itself is
    covered by TestWaitingAwayFromDock (physical-pause semantics)."""

    @pytest.mark.parametrize(
        ("ws", "rs"),
        [
            (WORK_STATUS_WAITING, WORK_STATUS_NONE),
            (WORK_STATUS_NONE, WORK_STATUS_NONE),
        ],
    )
    def test_idle_starts_fresh(self, ws, rs):
        row = state_matrix.lookup(
            work_status=ws, robot_status=rs, is_recharging=False
        )
        assert row.activity == state_matrix.ACTIVITY_DOCKED
        assert row.start_mowing == USER_CTRL_CLEAN


class TestDefaultRow:
    def test_unhandled_combo_gets_no_buttons(self):
        row = state_matrix.lookup(
            work_status=11,  # UPDATING — deliberately unhandled
            robot_status=11,
            is_recharging=False,
        )
        assert row is state_matrix.DEFAULT_ROW
        assert row.activity is None
        assert state_matrix.allowed_actions(row) == {
            "start_mowing": None, "pause": None, "dock": None,
        }

    def test_allowed_actions_shape(self):
        row = state_matrix.lookup(
            work_status=WORK_STATUS_MOWING,
            robot_status=WORK_STATUS_MOWING,
            is_recharging=False,
        )
        assert state_matrix.allowed_actions(row) == {
            "start_mowing": None,
            "pause": USER_CTRL_PAUSE,
            "dock": USER_CTRL_RECHARGE_DOCK,
        }
