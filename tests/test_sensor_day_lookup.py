"""Tests for dynamic day sensors using absolute date lookup."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from custom_components.ovo_energy_au.models import PlanConfig
from custom_components.ovo_energy_au.sensor import (
    AU_TIMEZONE,
    OVODailyHistorySensor,
    OVODaySensor,
)


@pytest.fixture
def mock_coordinator() -> MagicMock:
    """Return a mocked coordinator with sample daily entries."""
    coord = MagicMock()
    coord.account_id = "12345"
    coord.plan_config = PlanConfig()
    # March 20 is "today", so Day 1 = March 19, Day 2 = March 18
    coord.data = {
        "all_daily_entries": [
            {
                "date": "2026-03-19",
                "day_name": "Thursday",
                "day": 19,
                "month": 3,
                "year": 2026,
                "solar_consumption": 10.0,
                "grid_consumption": 5.0,
                "grid_charge": 1.5,
                "return_to_grid": 1.0,
                "grid_rates_kwh": {"OTHER": 5.0},
            },
            {
                "date": "2026-03-17",
                "day_name": "Tuesday",
                "day": 17,
                "month": 3,
                "year": 2026,
                "solar_consumption": 8.0,
                "grid_consumption": 4.0,
                "grid_charge": 1.2,
                "return_to_grid": 0.5,
                "grid_rates_kwh": {"OTHER": 4.0},
            },
        ]
    }
    return coord


def _patch_sensor_now():
    """Patch sensor.datetime.now to return a known AU datetime."""
    fixed_now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=AU_TIMEZONE)
    mock_datetime = MagicMock(
        now=MagicMock(return_value=fixed_now),
        fromisoformat=datetime.fromisoformat,
        strptime=datetime.strptime,
    )
    return patch("custom_components.ovo_energy_au.sensor.datetime", mock_datetime)


class TestOVODaySensor:
    """Tests for absolute date mapping in OVODaySensor."""

    def test_day_sensor_returns_value_for_target_date(self, mock_coordinator):
        """Day 1 should return the March 19 grid consumption."""
        sensor = OVODaySensor(
            mock_coordinator,
            "day_1_grid_consumption",
            "Day 1 Grid Consumption",
            "kWh",
            None,
            None,
            "mdi:test",
            0,
            "grid_consumption",
        )
        with _patch_sensor_now():
            assert sensor.native_value == 5.0

    def test_day_sensor_returns_none_when_target_date_missing(self, mock_coordinator):
        """Day 2 targets March 18, which is missing from the data."""
        sensor = OVODaySensor(
            mock_coordinator,
            "day_2_grid_consumption",
            "Day 2 Grid Consumption",
            "kWh",
            None,
            None,
            "mdi:test",
            1,
            "grid_consumption",
        )
        with _patch_sensor_now():
            assert sensor.native_value is None

    def test_day_sensor_name_uses_actual_date(self, mock_coordinator):
        """The friendly name should reflect the matched calendar date."""
        sensor = OVODaySensor(
            mock_coordinator,
            "day_1_grid_consumption",
            "Day 1 Grid Consumption",
            "kWh",
            None,
            None,
            "mdi:test",
            0,
            "grid_consumption",
        )
        with _patch_sensor_now():
            assert "19 Mar" in sensor.name


class TestOVODailyHistorySensor:
    """Tests for absolute date mapping in OVODailyHistorySensor."""

    def test_history_sensor_returns_value_for_target_date(self, mock_coordinator):
        """Day 1 history total should be March 19 solar + grid."""
        sensor = OVODailyHistorySensor(
            mock_coordinator,
            "history_day_0_total",
            "Day 1 Total Consumption",
            0,
            None,
            "mdi:test",
        )
        with _patch_sensor_now():
            assert sensor.native_value == 15.0

    def test_history_sensor_returns_none_when_target_date_missing(self, mock_coordinator):
        """Day 2 targets March 18, which is missing."""
        sensor = OVODailyHistorySensor(
            mock_coordinator,
            "history_day_1_total",
            "Day 2 Total Consumption",
            1,
            None,
            "mdi:test",
        )
        with _patch_sensor_now():
            assert sensor.native_value is None

    def test_history_sensor_attributes_show_target_date(self, mock_coordinator):
        """Extra attributes should expose the matched date."""
        sensor = OVODailyHistorySensor(
            mock_coordinator,
            "history_day_0_total",
            "Day 1 Total Consumption",
            0,
            None,
            "mdi:test",
        )
        with _patch_sensor_now():
            attrs = sensor.extra_state_attributes
            assert attrs["date"] == "2026-03-19"
            assert attrs["grid"] == 5.0
            assert attrs["solar"] == 10.0
