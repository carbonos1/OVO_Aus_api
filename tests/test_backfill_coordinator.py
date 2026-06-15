"""Tests for coordinator backfill and hourly caching behaviour."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ovo_energy_au.coordinator import OVOEnergyAUDataUpdateCoordinator
from custom_components.ovo_energy_au.models import PlanConfig


@pytest.fixture
def coordinator() -> OVOEnergyAUDataUpdateCoordinator:
    """Return a coordinator with a mocked client."""
    hass = MagicMock()
    client = MagicMock()
    client.get_interval_data = AsyncMock()
    client.get_product_agreements = AsyncMock()
    client.get_hourly_data = AsyncMock()
    client.get_hourly_data_for_date = AsyncMock()
    client.get_contact_info = AsyncMock()
    client.get_usage_info = AsyncMock()

    coord = OVOEnergyAUDataUpdateCoordinator(
        hass=hass,
        client=client,
        account_id="12345",
        plan_config=PlanConfig(),
    )
    coord.data = None
    return coord


@pytest.fixture
def interval_with_gap() -> dict:
    """Interval data missing 2026-03-16."""
    return {
        "daily": {
            "solar": [],
            "export": [
                {"periodFrom": "2026-03-15T00:00:00Z", "consumption": 5.0,
                 "charge": {"value": 1.5, "type": "DEBIT"}, "rates": []},
                {"periodFrom": "2026-03-17T00:00:00Z", "consumption": 6.0,
                 "charge": {"value": 1.8, "type": "DEBIT"}, "rates": []},
            ],
        },
    }


@pytest.fixture
def hourly_for_missing_day() -> dict:
    """Hourly data covering the missing AU day 2026-03-16."""
    export_entries = []
    for hour in range(24):
        export_entries.append({
            "periodFrom": f"2026-03-16T{hour:02d}:00:00+11:00",
            "periodTo": f"2026-03-16T{hour+1 if hour < 23 else 23}:59:00+11:00",
            "consumption": 0.5,
            "readType": "ACTUAL",
            "charge": {"value": 0.15, "type": "DEBIT"},
            "rates": [
                {
                    "type": "OTHER",
                    "consumption": 0.5,
                    "charge": {"value": 0.15, "type": "DEBIT"},
                    "percentOfTotal": 1.0,
                }
            ],
        })
    return {"solar": [], "export": export_entries}


class TestBackfill:
    """Tests for daily backfill from hourly data."""

    def test_backfill_inserts_synthetic_day(
        self, coordinator, interval_with_gap, hourly_for_missing_day
    ):
        """Missing day should be backfilled and aggregations recomputed."""
        from custom_components.ovo_energy_au.analytics.interval import process_interval_data

        processed = process_interval_data(interval_with_gap)
        coordinator.client.get_hourly_data_for_date.return_value = hourly_for_missing_day

        asyncio.run(coordinator._backfill_missing_days(processed, date(2026, 3, 17)))

        dates = {d["date"] for d in processed["all_daily_entries"]}
        assert "2026-03-16" in dates
        synthetic = next(
            d for d in processed["all_daily_entries"] if d.get("date") == "2026-03-16"
        )
        assert synthetic.get("synthetic") is True
        assert synthetic["grid_consumption"] == 12.0
        # Month-to-date should now include all three days
        assert processed["month_to_date"]["grid_consumption"] == 23.0

    def test_backfill_skips_old_missing_days(
        self, coordinator, interval_with_gap, hourly_for_missing_day
    ):
        """Missing days outside the hourly window should not be backfilled."""
        from custom_components.ovo_energy_au.analytics.interval import process_interval_data

        processed = process_interval_data(interval_with_gap)
        coordinator.client.get_hourly_data_for_date.return_value = hourly_for_missing_day

        # Pretend today is well past the missing day so it is outside the window
        asyncio.run(coordinator._backfill_missing_days(processed, date(2026, 3, 30)))

        dates = {d["date"] for d in processed["all_daily_entries"]}
        assert "2026-03-16" not in dates
        coordinator.client.get_hourly_data_for_date.assert_not_awaited()


class TestHourlyCaching:
    """Tests for preserving hourly data across failed updates."""

    def test_hourly_data_cached_on_failure(self, coordinator, interval_with_gap):
        """A failed hourly fetch should reuse the previous hourly payload."""
        previous_hourly = {
            "solar_entries": [{"periodFrom": "2026-03-15T00:00:00Z", "consumption": 1.0}],
            "grid_entries": [],
            "return_to_grid_entries": [],
        }
        coordinator.data = {"hourly": previous_hourly}
        coordinator.client.get_interval_data.return_value = interval_with_gap
        coordinator.client.get_product_agreements.return_value = {}
        coordinator.client.get_hourly_data.side_effect = Exception("API error")
        coordinator.client.get_hourly_data_for_date.return_value = {}
        coordinator.client.get_contact_info.side_effect = Exception("contact error")
        coordinator.client.get_usage_info.side_effect = Exception("usage error")

        result = asyncio.run(coordinator._async_update_data())

        assert result["hourly"] is previous_hourly
        assert "missing_daily_dates" in result
