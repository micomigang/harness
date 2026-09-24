from pathlib import Path

import httpx

from app.providers.seedream import SeedreamProvider


def test_reference_image_generates_and_downloads(tmp_path: Path):
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            payloads.append(request.read().decode())
            return httpx.Response(
                200, json={"data": [{"url": "https://files.example/reference.png"}]}
            )
        if str(request.url) == "https://files.example/reference.png":
            return httpx.Response(200, content=b"fake-png")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="doubao-seedream-5-0-pro-260628",
        output_dir=tmp_path,
        max_assets=1,
        client=client,
    )
    result = provider.generate(
        "reference_images",
        {
            "workspace": {"id": "workspace-1"},
            "artifacts": [
                {
                    "kind": "characters",
                    "content": {
                        "items": [
                            {
                                "id": "char_helene",
                                "name": "Hélène",
                                "description": "silver hair and a French country coat",
                            }
                        ]
                    },
                }
            ],
        },
    )

    item = result["items"][0]
    assert item["url"] == "/media/workspace-1/reference_images/characters-char_helene.png"
    assert Path(item["local_path"]).read_bytes() == b"fake-png"
    assert '"size":"2K"' in payloads[0]


def test_reference_image_honors_director_prompt_and_asset_cap(tmp_path: Path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(request.read().decode())
            return httpx.Response(200, json={"data": [{"url": "https://files.example/reference.png"}]})
        if str(request.url) == "https://files.example/reference.png":
            return httpx.Response(200, content=b"fake-png")
        return httpx.Response(404)

    provider = SeedreamProvider(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        model="seedream",
        output_dir=tmp_path,
        max_assets=6,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(
        "reference_images",
        {
            "workspace": {"id": "w"},
            "execution_directive": {
                "prompt_addendum": "Contemporary Paris, understated French TV realism.",
                "parameter_overrides": {"max_assets": 1},
            },
            "artifacts": [
                {"kind": "characters", "content": {"items": [
                    {"id": "a", "name": "A", "description": "first"},
                    {"id": "b", "name": "B", "description": "second"},
                ]}},
            ],
        },
    )
    assert result["requested"] == 1
    assert "Contemporary Paris" in posted[0]
