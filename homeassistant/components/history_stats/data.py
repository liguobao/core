"""Manage the history_stats data."""
from __future__ import annotations

from dataclasses import dataclass
import datetime

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers.template import Template
import homeassistant.util.dt as dt_util

from .helpers import async_calculate_period, floored_timestamp

MIN_TIME_UTC = datetime.datetime.min.replace(tzinfo=dt_util.UTC)


@dataclass
class HistoryStatsState:
    """The current stats of the history stats."""

    hours_matched: float | None
    match_count: int | None
    period: tuple[datetime.datetime, datetime.datetime]


class HistoryStats:
    """Manage history stats."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        entity_states: list[str],
        start: Template | None,
        end: Template | None,
        duration: datetime.timedelta | None,
    ) -> None:
        """Init the history stats manager."""
        self.hass = hass
        self.entity_id = entity_id
        self._period = (MIN_TIME_UTC, MIN_TIME_UTC)
        self._state: HistoryStatsState = HistoryStatsState(None, None, self._period)
        self._history_current_period: list[State] = []
        self._previous_run_before_start = False
        self._entity_states = set(entity_states)
        self._duration = duration
        self._start = start
        self._end = end

    async def async_update(self, event: Event | None) -> HistoryStatsState:
        """Update the stats at a given time."""
        # Get previous values of start and end
        previous_period_start, previous_period_end = self._period
        # Parse templates
        self._period = async_calculate_period(self._duration, self._start, self._end)
        # Get the current period
        current_period_start, current_period_end = self._period

        # Convert times to UTC
        current_period_start = dt_util.as_utc(current_period_start)
        current_period_end = dt_util.as_utc(current_period_end)
        previous_period_start = dt_util.as_utc(previous_period_start)
        previous_period_end = dt_util.as_utc(previous_period_end)

        # Compute integer timestamps
        current_period_start_timestamp = floored_timestamp(current_period_start)
        current_period_end_timestamp = floored_timestamp(current_period_end)
        previous_period_start_timestamp = floored_timestamp(previous_period_start)
        previous_period_end_timestamp = floored_timestamp(previous_period_end)
        utc_now = dt_util.utcnow()
        now_timestamp = floored_timestamp(utc_now)

        if current_period_start > utc_now:
            # History cannot tell the future
            self._history_current_period = []
            self._previous_run_before_start = True
            self._state = HistoryStatsState(None, None, self._period)
            return self._state
        #
        # We avoid querying the database if the below did NOT happen:
        #
        # - The previous run happened before the start time
        # - The start time changed
        # - The period shrank in size
        # - The previous period ended before now
        #
        if (
            not self._previous_run_before_start
            and current_period_start_timestamp == previous_period_start_timestamp
            and (
                current_period_end_timestamp == previous_period_end_timestamp
                or (
                    current_period_end_timestamp >= previous_period_end_timestamp
                    and previous_period_end_timestamp <= now_timestamp
                )
            )
        ):
            new_data = False
            if event and event.data["new_state"] is not None:
                new_state: State = event.data["new_state"]
                if (
                    current_period_start_timestamp
                    <= floored_timestamp(new_state.last_changed)
                    <= current_period_end_timestamp
                ):
                    self._history_current_period.append(new_state)
                    new_data = True
            if not new_data and current_period_end_timestamp < now_timestamp:
                # If period has not changed and current time after the period end...
                # Don't compute anything as the value cannot have changed
                return self._state
        else:
            self._history_current_period = await get_instance(
                self.hass
            ).async_add_executor_job(
                self._update_from_database,
                current_period_start,
                current_period_end,
            )
            self._previous_run_before_start = False

        hours_matched, match_count = self._async_compute_hours_and_changes(
            now_timestamp,
            current_period_start_timestamp,
            current_period_end_timestamp,
        )
        self._state = HistoryStatsState(hours_matched, match_count, self._period)
        return self._state

    def _update_from_database(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> list[State]:
        return history.state_changes_during_period(
            self.hass,
            start,
            end,
            self.entity_id,
            include_start_time_state=True,
            no_attributes=True,
        ).get(self.entity_id, [])

    def _async_compute_hours_and_changes(
        self, now_timestamp: float, start_timestamp: float, end_timestamp: float
    ) -> tuple[float, int]:
        """Compute the hours matched and changes from the history list and first state."""
        # state_changes_during_period is called with include_start_time_state=True
        # which is the default and always provides the state at the start
        # of the period
        previous_state_matches = (
            self._history_current_period
            and self._history_current_period[0].state in self._entity_states
        )
        last_state_change_timestamp = start_timestamp
        elapsed = 0.0
        match_count = 1 if previous_state_matches else 0

        # Make calculations
        for item in self._history_current_period:
            current_state_matches = item.state in self._entity_states
            state_change_timestamp = item.last_changed.timestamp()

            if previous_state_matches:
                elapsed += state_change_timestamp - last_state_change_timestamp
            elif current_state_matches:
                match_count += 1

            previous_state_matches = current_state_matches
            last_state_change_timestamp = state_change_timestamp

        # Count time elapsed between last history state and end of measure
        if previous_state_matches:
            measure_end = min(end_timestamp, now_timestamp)
            elapsed += measure_end - last_state_change_timestamp

        # Save value in hours
        hours_matched = elapsed / 3600
        return hours_matched, match_count
