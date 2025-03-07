"""The tests for the Google Calendar component."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import datetime
import http
import time
from typing import Any
from unittest.mock import Mock, patch

import pytest

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.components.google import DOMAIN, SERVICE_ADD_EVENT
from homeassistant.components.google.const import CONF_CALENDAR_ACCESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow

from .conftest import (
    CALENDAR_ID,
    TEST_API_ENTITY,
    TEST_API_ENTITY_NAME,
    TEST_YAML_ENTITY,
    TEST_YAML_ENTITY_NAME,
    ApiResult,
    ComponentSetup,
)

from tests.common import MockConfigEntry
from tests.test_util.aiohttp import AiohttpClientMocker

EXPIRED_TOKEN_TIMESTAMP = datetime.datetime(2022, 4, 8).timestamp()

# Typing helpers
HassApi = Callable[[], Awaitable[dict[str, Any]]]


def assert_state(actual: State | None, expected: State | None) -> None:
    """Assert that the two states are equal."""
    if actual is None or expected is None:
        assert actual == expected
        return
    assert actual.entity_id == expected.entity_id
    assert actual.state == expected.state
    assert actual.attributes == expected.attributes


@pytest.fixture
def setup_config_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Fixture to initialize the config entry."""
    config_entry.add_to_hass(hass)


async def test_unload_entry(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test load and unload of a ConfigEntry."""
    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    "token_scopes", ["https://www.googleapis.com/auth/calendar.readonly"]
)
async def test_existing_token_missing_scope(
    hass: HomeAssistant,
    token_scopes: list[str],
    component_setup: ComponentSetup,
    config_entry: MockConfigEntry,
) -> None:
    """Test setup where existing token does not have sufficient scopes."""
    config_entry.add_to_hass(hass)
    assert await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"


@pytest.mark.parametrize("config_entry_options", [{CONF_CALENDAR_ACCESS: "read_only"}])
async def test_config_entry_scope_reauth(
    hass: HomeAssistant,
    token_scopes: list[str],
    component_setup: ComponentSetup,
    config_entry: MockConfigEntry,
) -> None:
    """Test setup where the config entry options requires reauth to match the scope."""
    config_entry.add_to_hass(hass)
    assert await component_setup()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"


@pytest.mark.parametrize("calendars_config", [[{"cal_id": "invalid-schema"}]])
async def test_calendar_yaml_missing_required_fields(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    calendars_config: list[dict[str, Any]],
    mock_calendars_yaml: None,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test setup with a missing schema fields, ignores the error and continues."""
    assert await component_setup()

    assert not hass.states.get(TEST_YAML_ENTITY)


@pytest.mark.parametrize("calendars_config", [[{"missing-cal_id": "invalid-schema"}]])
async def test_invalid_calendar_yaml(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    calendars_config: list[dict[str, Any]],
    mock_calendars_yaml: None,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test setup with missing entity id fields fails to load the platform."""
    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})

    assert await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert not hass.states.get(TEST_YAML_ENTITY)
    assert not hass.states.get(TEST_API_ENTITY)


async def test_calendar_yaml_error(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test setup with yaml file not found."""
    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})

    with patch("homeassistant.components.google.open", side_effect=FileNotFoundError()):
        assert await component_setup()

    assert not hass.states.get(TEST_YAML_ENTITY)
    assert hass.states.get(TEST_API_ENTITY)


@pytest.mark.parametrize("calendars_config", [[]])
async def test_found_calendar_from_api(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_yaml: None,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test finding a calendar from the API."""

    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})
    assert await component_setup()

    state = hass.states.get(TEST_API_ENTITY)
    assert state
    assert state.name == TEST_API_ENTITY_NAME
    assert state.state == STATE_OFF

    # No yaml config loaded that overwrites the entity name
    assert not hass.states.get(TEST_YAML_ENTITY)


@pytest.mark.parametrize(
    "calendars_config,google_config,config_entry_options",
    [([], {}, {CONF_CALENDAR_ACCESS: "read_write"})],
)
async def test_load_application_credentials(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_yaml: None,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test loading an application credentials and a config entry."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "device_auth"
    )

    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})
    assert await component_setup()

    state = hass.states.get(TEST_API_ENTITY)
    assert state
    assert state.name == TEST_API_ENTITY_NAME
    assert state.state == STATE_OFF

    # No yaml config loaded that overwrites the entity name
    assert not hass.states.get(TEST_YAML_ENTITY)


@pytest.mark.parametrize(
    "calendars_config_track,expected_state,google_config_track_new",
    [
        (
            True,
            State(
                TEST_YAML_ENTITY,
                STATE_OFF,
                attributes={
                    "offset_reached": False,
                    "friendly_name": TEST_YAML_ENTITY_NAME,
                },
            ),
            None,
        ),
        (
            True,
            State(
                TEST_YAML_ENTITY,
                STATE_OFF,
                attributes={
                    "offset_reached": False,
                    "friendly_name": TEST_YAML_ENTITY_NAME,
                },
            ),
            True,
        ),
        (
            True,
            State(
                TEST_YAML_ENTITY,
                STATE_OFF,
                attributes={
                    "offset_reached": False,
                    "friendly_name": TEST_YAML_ENTITY_NAME,
                },
            ),
            False,  # Has no effect
        ),
        (False, None, None),
        (False, None, True),
        (False, None, False),
    ],
)
async def test_calendar_config_track_new(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_yaml: None,
    mock_calendars_list: ApiResult,
    mock_events_list: ApiResult,
    test_api_calendar: dict[str, Any],
    calendars_config_track: bool,
    expected_state: State,
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test calendar config that overrides whether or not a calendar is tracked."""

    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})
    assert await component_setup()

    state = hass.states.get(TEST_YAML_ENTITY)
    assert_state(state, expected_state)


async def test_add_event_missing_required_fields(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    setup_config_entry: MockConfigEntry,
) -> None:
    """Test service call that adds an event missing required fields."""

    assert await component_setup()

    with pytest.raises(ValueError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_EVENT,
            {
                "calendar_id": CALENDAR_ID,
                "summary": "Summary",
                "description": "Description",
            },
            blocking=True,
        )


@pytest.mark.parametrize(
    "date_fields,start_timedelta,end_timedelta",
    [
        (
            {"in": {"days": 3}},
            datetime.timedelta(days=3),
            datetime.timedelta(days=4),
        ),
        (
            {"in": {"weeks": 1}},
            datetime.timedelta(days=7),
            datetime.timedelta(days=8),
        ),
    ],
    ids=["in_days", "in_weeks"],
)
async def test_add_event_date_in_x(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_list: ApiResult,
    mock_insert_event: Callable[[..., dict[str, Any]], None],
    test_api_calendar: dict[str, Any],
    date_fields: dict[str, Any],
    start_timedelta: datetime.timedelta,
    end_timedelta: datetime.timedelta,
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test service call that adds an event with various time ranges."""

    mock_calendars_list({})
    assert await component_setup()

    now = datetime.datetime.now()
    start_date = now + start_timedelta
    end_date = now + end_timedelta

    mock_insert_event(
        calendar_id=CALENDAR_ID,
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_EVENT,
        {
            "calendar_id": CALENDAR_ID,
            "summary": "Summary",
            "description": "Description",
            **date_fields,
        },
        blocking=True,
    )
    assert len(aioclient_mock.mock_calls) == 2
    assert aioclient_mock.mock_calls[1][2] == {
        "summary": "Summary",
        "description": "Description",
        "start": {"date": start_date.date().isoformat()},
        "end": {"date": end_date.date().isoformat()},
    }


async def test_add_event_date(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_list: ApiResult,
    mock_insert_event: Callable[[str, dict[str, Any]], None],
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test service call that sets a date range."""

    mock_calendars_list({})
    assert await component_setup()

    now = utcnow()
    today = now.date()
    end_date = today + datetime.timedelta(days=2)

    mock_insert_event(
        calendar_id=CALENDAR_ID,
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_EVENT,
        {
            "calendar_id": CALENDAR_ID,
            "summary": "Summary",
            "description": "Description",
            "start_date": today.isoformat(),
            "end_date": end_date.isoformat(),
        },
        blocking=True,
    )
    assert len(aioclient_mock.mock_calls) == 2
    assert aioclient_mock.mock_calls[1][2] == {
        "summary": "Summary",
        "description": "Description",
        "start": {"date": today.isoformat()},
        "end": {"date": end_date.isoformat()},
    }


async def test_add_event_date_time(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_list: ApiResult,
    mock_insert_event: Callable[[str, dict[str, Any]], None],
    test_api_calendar: dict[str, Any],
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test service call that adds an event with a date time range."""

    mock_calendars_list({})
    assert await component_setup()

    start_datetime = datetime.datetime.now()
    delta = datetime.timedelta(days=3, hours=3)
    end_datetime = start_datetime + delta

    mock_insert_event(
        calendar_id=CALENDAR_ID,
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_EVENT,
        {
            "calendar_id": CALENDAR_ID,
            "summary": "Summary",
            "description": "Description",
            "start_date_time": start_datetime.isoformat(),
            "end_date_time": end_datetime.isoformat(),
        },
        blocking=True,
    )
    assert len(aioclient_mock.mock_calls) == 2
    assert aioclient_mock.mock_calls[1][2] == {
        "summary": "Summary",
        "description": "Description",
        "start": {
            "dateTime": start_datetime.isoformat(timespec="seconds"),
            "timeZone": "America/Regina",
        },
        "end": {
            "dateTime": end_datetime.isoformat(timespec="seconds"),
            "timeZone": "America/Regina",
        },
    }


@pytest.mark.parametrize(
    "config_entry_token_expiry", [datetime.datetime.max.timestamp() + 1]
)
async def test_invalid_token_expiry_in_config_entry(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Exercise case in issue #69623 with invalid token expiration persisted."""

    # The token is refreshed and new expiration values are returned
    expires_in = 86400
    expires_at = time.time() + expires_in
    aioclient_mock.post(
        "https://oauth2.googleapis.com/token",
        json={
            "refresh_token": "some-refresh-token",
            "access_token": "some-updated-token",
            "expires_at": expires_at,
            "expires_in": expires_in,
        },
    )

    assert await component_setup()

    # Verify token expiration values are updated
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is ConfigEntryState.LOADED
    assert entries[0].data["token"]["access_token"] == "some-updated-token"
    assert entries[0].data["token"]["expires_in"] == expires_in


@pytest.mark.parametrize("config_entry_token_expiry", [EXPIRED_TOKEN_TIMESTAMP])
async def test_expired_token_refresh_internal_error(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Generic errors on reauth are treated as a retryable setup error."""

    aioclient_mock.post(
        "https://oauth2.googleapis.com/token",
        status=http.HTTPStatus.INTERNAL_SERVER_ERROR,
    )

    assert await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    "config_entry_token_expiry",
    [EXPIRED_TOKEN_TIMESTAMP],
)
async def test_expired_token_requires_reauth(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    setup_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test case where reauth is required for token that cannot be refreshed."""

    aioclient_mock.post(
        "https://oauth2.googleapis.com/token",
        status=http.HTTPStatus.BAD_REQUEST,
    )

    assert await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"


@pytest.mark.parametrize(
    "calendars_config,expect_write_calls",
    [
        (
            [
                {
                    "cal_id": "ignored",
                    "entities": {"device_id": "existing", "name": "existing"},
                }
            ],
            True,
        ),
        ([], False),
    ],
    ids=["has_yaml", "no_yaml"],
)
async def test_calendar_yaml_update(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    mock_calendars_yaml: Mock,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    setup_config_entry: MockConfigEntry,
    calendars_config: dict[str, Any],
    expect_write_calls: bool,
) -> None:
    """Test updating the yaml file with a new calendar."""

    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})
    assert await component_setup()

    mock_calendars_yaml().read.assert_called()
    mock_calendars_yaml().write.called is expect_write_calls

    state = hass.states.get(TEST_API_ENTITY)
    assert state
    assert state.name == TEST_API_ENTITY_NAME
    assert state.state == STATE_OFF

    # No yaml config loaded that overwrites the entity name
    assert not hass.states.get(TEST_YAML_ENTITY)


async def test_update_will_reload(
    hass: HomeAssistant,
    component_setup: ComponentSetup,
    setup_config_entry: Any,
    mock_calendars_list: ApiResult,
    test_api_calendar: dict[str, Any],
    mock_events_list: ApiResult,
    config_entry: MockConfigEntry,
) -> None:
    """Test updating config entry options will trigger a reload."""
    mock_calendars_list({"items": [test_api_calendar]})
    mock_events_list({})
    await component_setup()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.options == {}  # read_write is default

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload",
        return_value=None,
    ) as mock_reload:
        # No-op does not reload
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_CALENDAR_ACCESS: "read_write"}
        )
        await hass.async_block_till_done()
        mock_reload.assert_not_called()

        # Data change does not trigger reload
        hass.config_entries.async_update_entry(
            config_entry,
            data={
                **config_entry.data,
                "example": "field",
            },
        )
        await hass.async_block_till_done()
        mock_reload.assert_not_called()

        # Reload when options changed
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_CALENDAR_ACCESS: "read_only"}
        )
        await hass.async_block_till_done()
        mock_reload.assert_called_once()
