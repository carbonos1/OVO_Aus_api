"""Data coordinator for OVO Energy Australia."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .analytics.hourly import (
    aggregate_hourly_to_daily,
    process_hourly_data,
    prune_hourly_raw_data,
)
from .analytics.insights import compute_insights
from .analytics.interval import process_interval_data, recompute_aggregations
from .api import (
    OVOEnergyAUApiClient,
    OVOEnergyAUApiClientAuthenticationError,
    OVOEnergyAUApiClientCommunicationError,
    OVOEnergyAUApiClientError,
)
from .const import DOMAIN, FAST_UPDATE_INTERVAL
from .models import PlanConfig

_LOGGER = logging.getLogger(__name__)


class OVOEnergyAUDataUpdateCoordinator(TimestampDataUpdateCoordinator):
    """Fetch and process data from OVO Energy Australia API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OVOEnergyAUApiClient,
        account_id: str,
        plan_config: PlanConfig | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.account_id = account_id
        self.plan_config = plan_config or PlanConfig()

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=FAST_UPDATE_INTERVAL,
        )

    async def _backfill_missing_days(self, processed: dict, now_date: date) -> None:
        """Detect missing daily interval entries and backfill from hourly data."""
        missing = processed.get("missing_daily_dates", [])
        if not missing:
            return

        cutoff = now_date - timedelta(days=8)
        backfilled = False

        for date_str in missing:
            try:
                target = date.fromisoformat(date_str)
            except ValueError:
                continue

            # Only backfill recent days where we actually fetched hourly data.
            if target < cutoff:
                _LOGGER.debug("Skipping backfill for %s: outside hourly window", date_str)
                continue

            try:
                hourly_raw = await self.client.get_hourly_data_for_date(
                    self.account_id, date_str
                )
                synthetic = aggregate_hourly_to_daily(
                    process_hourly_data(hourly_raw or {}, self.plan_config),
                    target,
                )
                if synthetic:
                    processed["all_daily_entries"].append(synthetic)
                    backfilled = True
            except Exception as err:
                _LOGGER.debug("Could not backfill %s: %s", date_str, err)

        if backfilled:
            processed["all_daily_entries"].sort(
                key=lambda x: x["date"], reverse=True
            )
            recompute_aggregations(processed, dt_util.now())

    async def _async_update_data(self) -> dict:
        """Fetch data from OVO Energy API."""
        try:
            # 1. Interval data (daily/monthly/yearly)
            interval_data = await self.client.get_interval_data(self.account_id)
            processed = process_interval_data(interval_data)

            # 2. Product agreements (plan info)
            try:
                processed["product_agreements"] = await self.client.get_product_agreements(
                    self.account_id
                )
            except Exception as err:
                _LOGGER.error("Failed to fetch product agreements: %s", err)
                processed["product_agreements"] = None

            # 3. Hourly data - fetch last 8 days to cover all 7-day-ago sensors
            # and handle month boundaries (e.g., yesterday on the 1st)
            now = dt_util.now()
            now_date = now.date()
            query_start = (now - timedelta(days=8)).strftime("%Y-%m-%d")
            query_end = now.strftime("%Y-%m-%d")

            previous_hourly = (self.data or {}).get("hourly", {}) if self.data else {}

            try:
                hourly_raw = await self.client.get_hourly_data(
                    self.account_id, query_start, query_end
                )
                pruned_raw = prune_hourly_raw_data(
                    hourly_raw or {}, now_date - timedelta(days=8)
                )
                processed["hourly"] = process_hourly_data(
                    pruned_raw or {}, self.plan_config
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch hourly data: %s", err)
                processed["hourly"] = previous_hourly

            # 4. Backfill missing daily entries from hourly data
            await self._backfill_missing_days(processed, now_date)

            # 5. Analytics insights (after backfill so they include synthetic days)
            compute_insights(processed)

            # 6. Calculate bill estimate
            try:
                # Get standing charge from product agreements
                standing_daily = 0
                if processed.get("product_agreements"):
                    agreements = processed["product_agreements"].get("productAgreements", [])
                    if agreements:
                        standing_cents = agreements[0].get("product", {}).get("standingChargeCentsPerDay", 0) or 0
                        standing_daily = standing_cents / 100  # Convert to AUD

                # Month-to-date bill
                mtd = processed.get("month_to_date", {})
                mtd_days = mtd.get("days", 0) or 0
                mtd_grid = mtd.get("grid_charge", 0) or 0
                mtd_solar_credit = abs(mtd.get("solar_charge", 0) or 0)
                mtd_standing = standing_daily * mtd_days
                mtd_bill = mtd_grid + mtd_standing - mtd_solar_credit

                # Project full month
                if mtd_days > 0:
                    import calendar
                    from datetime import datetime

                    from .const import AU_TIMEZONE

                    now_au = datetime.now(AU_TIMEZONE)
                    days_in_month = calendar.monthrange(now_au.year, now_au.month)[1]
                    daily_avg_net = mtd_bill / mtd_days
                    projected_bill = daily_avg_net * days_in_month
                    remaining_bill = daily_avg_net * (days_in_month - mtd_days)

                    processed["bill_estimate"] = {
                        "mtd_bill": round(mtd_bill, 2),
                        "mtd_grid_cost": round(mtd_grid, 2),
                        "mtd_solar_credit": round(mtd_solar_credit, 2),
                        "mtd_standing_charge": round(mtd_standing, 2),
                        "mtd_days": mtd_days,
                        "standing_charge_daily": round(standing_daily, 2),
                        "projected_bill": round(projected_bill, 2),
                        "remaining_estimate": round(remaining_bill, 2),
                        "daily_average_net": round(daily_avg_net, 2),
                        "days_in_month": days_in_month,
                        "days_remaining": days_in_month - mtd_days,
                    }
                else:
                    processed["bill_estimate"] = {}
            except Exception as err:
                _LOGGER.debug("Failed to calculate bill estimate: %s", err)
                processed["bill_estimate"] = {}

            # 7. Account balance from contact info
            try:
                contact_info = await self.client.get_contact_info()
                accounts = contact_info.get("accounts", [])
                active = [a for a in accounts if not a.get("closed", False)]
                if active:
                    processed["account_balance"] = active[0].get("customerOrientatedBalance")
                    processed["has_solar"] = active[0].get("hasSolar", False)
            except Exception as err:
                _LOGGER.debug("Failed to fetch contact info: %s", err)
                processed["account_balance"] = None
                processed["has_solar"] = None

            # 8. Usage info (timezone, meter type)
            try:
                usage_info = await self.client.get_usage_info(self.account_id)
                usage_v2 = (usage_info or {}).get("usageV2") or {}
                processed["meter_type"] = usage_v2.get("meterType")
                processed["api_timezone"] = usage_v2.get("timezone")
                last_read = (usage_v2.get("lastMeterRead") or {}).get("date")
                processed["last_meter_read"] = last_read
            except Exception as err:
                _LOGGER.debug("Failed to fetch usage info: %s", err)

            return processed

        except OVOEnergyAUApiClientAuthenticationError as err:
            raise ConfigEntryAuthFailed(err) from err
        except OVOEnergyAUApiClientCommunicationError as err:
            raise UpdateFailed(f"Communication error: {err}") from err
        except OVOEnergyAUApiClientError as err:
            raise UpdateFailed(f"API error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching OVO Energy data")
            raise UpdateFailed(f"Error fetching data: {err}") from err

