# To run this code you need to install the following dependencies:
# pip install google-genai

import os

from google import genai
from google.genai import types


def generate():
    client = genai.Client(
        api_key=os.environ.get("GOOGLE_API_KEY"),
    )

    model = "gemini-3.5-flash-lite"
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="""Hôm nay Hà nội bao nhiêu độ"""),
            ],
        ),
    ]
    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(
            thinking_level="MINIMAL",
        ),
        audio_transcription_config=types.AudioTranscriptionConfig(),
        response_mime_type="application/json",
        response_schema=genai.types.Schema(
            type=genai.types.Type.OBJECT,
            properties={
                "response": genai.types.Schema(
                    type=genai.types.Type.STRING,
                ),
            },
        ),
    )

    for chunk in client.models.generate_content_stream(
        model=model,
        contents=contents,
        config=generate_content_config,
    ):
        if text := chunk.text:
            print(text, end="")


if __name__ == "__main__":
    generate()
