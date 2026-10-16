import httpx
from fastapi.testclient import TestClient
from openai import OpenAI

from llm_gateway.app import create_app


def test_openai_sdk_core_phase_two_endpoints(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "features:\n"
        "  responses_api: true\n"
        "  embeddings_api: true\n"
        "  moderations_api: true\n"
        "  audio_api: true\n"
        "  images_api: true\n"
        "providers:\n"
        "  - name: p\n"
        "    base_url: https://provider.example/v1\n"
        "    api_key_value: test-only\n"
        "    models:\n"
        "      - name: universal\n"
        "        upstream_model: upstream\n"
        "        capabilities: [chat, vision, tools, json, embeddings, audio_speech, image_generation]\n",
        encoding="utf-8",
    )

    async def complete_for(payload, *_args):
        return ({
            "id": "chatcmpl-sdk", "object": "chat.completion", "created": 1,
            "model": "universal",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "compatible"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }, "p", "upstream")

    async def embed_for(_payload, *_args):
        return ({
            "object": "list", "model": "universal",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }, "p", "upstream")

    async def speech_for(_payload, *_args):
        return b"test-audio", "p", "upstream", httpx.Headers()

    async def generate_images_for(_payload, *_args):
        return ({
            "created": 1,
            "data": [{"url": "https://images.example/generated.png"}],
        }, "p", "upstream")

    with TestClient(create_app(str(config)), raise_server_exceptions=False) as client:
        client.app.state.router.complete_for = complete_for
        client.app.state.router.embed_for = embed_for
        client.app.state.router.speech_for = speech_for
        client.app.state.router.generate_images_for = generate_images_for
        sdk = OpenAI(
            base_url="http://testserver/v1/", api_key="test-only", http_client=client,
        )

        assert sdk.models.list().data[0].id == "universal"
        chat = sdk.chat.completions.create(
            model="universal", messages=[{"role": "user", "content": "hello"}],
        )
        assert chat.choices[0].message.content == "compatible"
        response = sdk.responses.create(model="universal", input="hello")
        assert response.output_text == "compatible"
        embedding = sdk.embeddings.create(model="universal", input="hello")
        assert embedding.data[0].embedding == [0.1, 0.2]
        moderation = sdk.moderations.create(input="hello")
        assert moderation.results[0].flagged is False
        speech = sdk.audio.speech.create(model="universal", voice="alloy", input="hello")
        assert speech.read() == b"test-audio"
        image = sdk.images.generate(model="universal", prompt="a test image")
        assert image.data[0].url == "https://images.example/generated.png"
