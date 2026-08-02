"""Persist session state changes through a Google ADK session service."""



import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from google.adk.events import Event, EventActions
from google.adk.sessions import BaseSessionService, Session


class SessionStateService:
    """Create, update, and delete ADK sessions without bypassing persistence.

    State updates are written as ADK events instead of mutating ``session.state``
    directly. This keeps the update in the event history and works with
    persistent session services as well as ``InMemorySessionService``.
    """

    def __init__(self, session_service: BaseSessionService) -> None:
        self._session_service = session_service

    async def save(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Create a session and save its initial state."""
        return await self._session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            state=dict(state or {}),
        )

    async def update(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        state: Mapping[str, Any],
    ) -> Session:
        """Merge ``state`` into an existing session and persist the change."""
        session = await self._get_required_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

        event = Event(
            invocation_id=f"session-state-{uuid4()}",
            author="system",
            actions=EventActions(state_delta=dict(state)),
            timestamp=time.time(),
        )
        await self._session_service.append_event(session, event)

        return await self._get_required_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

    async def delete(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Delete a session, including its state and event history."""
        await self._session_service.delete_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

    async def _get_required_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> Session:
        session = await self._session_service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise ValueError(
                f"Session '{session_id}' was not found for user '{user_id}'."
            )
        return session
