import asyncio

from google.adk.agents.invocation_context import (
    InvocationContext,
    new_invocation_context_id,
)
from google.adk.artifacts.in_memory_artifact_service import (
    InMemoryArtifactService,
)
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import app, root_agent


def test_app_plugin_saves_uploaded_file_as_artifact():
    async def run():
        artifact_service = InMemoryArtifactService()
        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name=app.name,
            user_id="user",
            session_id="upload-session",
        )
        context = InvocationContext(
            artifact_service=artifact_service,
            session_service=session_service,
            invocation_id=new_invocation_context_id(),
            agent=root_agent,
            session=session,
        )
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    inline_data=types.Blob(
                        data=b"# Product\n\nCode: CARD-001\n",
                        display_name="product.md",
                        mime_type="text/markdown",
                    )
                ),
                types.Part(text="Ingest this document into Neo4j"),
            ],
        )

        plugin = app.plugins[0]
        processed = await plugin.on_user_message_callback(
            invocation_context=context,
            user_message=message,
        )

        saved = await artifact_service.load_artifact(
            app_name=app.name,
            user_id="user",
            session_id=session.id,
            filename="product.md",
        )

        assert saved is not None
        assert saved.inline_data is not None
        assert saved.inline_data.data == b"# Product\n\nCode: CARD-001\n"
        assert processed is not None
        assert any(
            part.text == '[Uploaded Artifact: "product.md"]'
            for part in processed.parts or []
        )

    asyncio.run(run())
