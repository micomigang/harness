"""Local MiniMax H3 Ref2VA adapter using ComfyUI's HTTP API."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.image_provenance import image_format
from .seedance import SeedanceError, SeedanceProvider


class ComfyH3Provider(SeedanceProvider):
    """Keep Harness's shot, review and batch contract; replace Ark execution."""

    name = "comfyui-h3"
    MODEL_FILE = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
    AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
    TURBO_LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"

    def __init__(
        self, *, base_url: str, models_dir: Path, output_dir: Path,
        resolution: str = "384p", ratio: str = "9:16", generate_audio: bool = True,
        timeout_seconds: int = 3600, poll_interval_seconds: float = 5,
        ffmpeg_path: str = "ffmpeg", client: httpx.Client | None = None,
        sleeper=time.sleep,
    ):
        super().__init__(
            api_key="local-comfyui", base_url=base_url,
            model="minimax-h3-ref2va-pruned-int8-convrot", output_dir=output_dir,
            resolution=resolution, ratio=ratio, generate_audio=generate_audio,
            poll_timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            batch_concurrency=1, preflight_enabled=True,
            ffmpeg_path=ffmpeg_path, client=client, sleeper=sleeper,
        )
        self.models_dir = Path(models_dir)

    def _require_models(self) -> None:
        required = {
            "diffusion_models": self.MODEL_FILE,
            "text_encoders": self.TEXT_ENCODER,
            "vae": self.VIDEO_VAE,
            "vae_audio": self.AUDIO_VAE,
            "loras": self.TURBO_LORA,
        }
        for folder, filename in required.items():
            actual_folder = "vae" if folder == "vae_audio" else folder
            if not (self.models_dir / actual_folder / filename).is_file():
                raise SeedanceError(f"本地 H3 模型文件缺失：{actual_folder}/{filename}")

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        self._require_models()
        if stage == "batch_video":
            preview = next(
                (artifact for artifact in context.get("artifacts", [])
                 if artifact.get("kind") == "preview"), None,
            )
            content = (preview or {}).get("content") or {}
            if content.get("model") != self.model:
                raise SeedanceError("H3 批量视频需要先批准由本地 H3 生成的单镜预览")
        result = super().generate(stage, context)
        if stage == "batch_video":
            result["execution_mode"] = "local_sequential"
            result["note"] = "本地 H3 按镜头顺序生成；失败后停止后续镜头。"
        return result

    def _reusable_batch_items(
        self, context: dict[str, Any], selected_indices: list[int]
    ) -> dict[int, dict[str, Any]]:
        # A previously approved Seedance clip is not an H3 preview or batch result.
        scoped = dict(context)
        artifacts = []
        for artifact in context.get("artifacts", []):
            if artifact.get("kind") == "preview":
                if (artifact.get("content") or {}).get("model") != self.model:
                    continue
            elif artifact.get("kind") == "batch_video":
                artifact = dict(artifact)
                content = dict(artifact.get("content") or {})
                content["items"] = [
                    item for item in content.get("items", [])
                    if isinstance(item, dict) and item.get("model") == self.model
                ]
                artifact["content"] = content
            artifacts.append(artifact)
        scoped["artifacts"] = artifacts
        return super()._reusable_batch_items(scoped, selected_indices)

    def _preflight_reference_urls(
        self, context: dict[str, Any], keys: list[str], urls: list[str]
    ) -> list[str]:
        if len(keys) != len(urls):
            raise SeedanceError("H3 参考图键与输入数量不一致")
        by_key = {
            str(item.get("canonical_key") or item.get("reference_key") or ""): item
            for item in self._reference_items(context)
        }
        paths = []
        for key in keys:
            item = by_key.get(key)
            if not item:
                raise SeedanceError(f"H3 参考图 {key} 缺少当前绑定资产")
            path = Path(str(item.get("local_path") or ""))
            if not path.is_file():
                raise SeedanceError(f"H3 参考图 {key} 的本地文件缺失：{path}")
            with path.open("rb") as source:
                detected = image_format(source.read(16))
            if detected not in {"jpeg", "png", "webp"}:
                raise SeedanceError(f"H3 参考图 {key} 不是可识别的 JPEG/PNG/WebP 文件")
            paths.append(str(path.resolve()))
        return paths

    def _preflight_audit_report(
        self, context: dict[str, Any], keys: list[str], urls: list[str]
    ) -> dict[str, Any]:
        return {
            "status": "pass", "provider": "local-comfyui-h3",
            "references": [
                {"canonical_key": key, "transport": "local_original_or_selected_file"}
                for key in keys
            ],
            "note": "本地 H3 使用已绑定文件；Ark 人像受信期不适用于本地推理。",
        }

    @staticmethod
    def _dimensions(resolution: str, ratio: str) -> tuple[int, int]:
        try:
            short = int(resolution.lower().removesuffix("p"))
            left, right = (int(value) for value in ratio.split(":"))
        except (ValueError, AttributeError) as exc:
            raise SeedanceError(f"H3 分辨率或画幅比例无效：{resolution}, {ratio}") from exc
        if not 256 <= short <= 768 or left <= 0 or right <= 0:
            raise SeedanceError("H3 短边须为 256–768，画幅比例须为正数")
        width = short if left <= right else round(short * left / right)
        height = short if right <= left else round(short * right / left)
        width = max(32, round(width / 32) * 32)
        height = max(32, round(height / 32) * 32)
        if width * height > 768 * 1344:
            raise SeedanceError("H3 输出尺寸超过模型画布上限")
        return width, height

    @staticmethod
    def _frame_count(duration: int) -> int:
        if not 4 <= duration <= 15:
            raise SeedanceError("本地 H3 单镜时长必须为 4–15 秒")
        frames = math.ceil(duration * 24)
        return frames + ((5 - frames) % 17)

    def _workflow(
        self, *, prompt: str, uploads: list[str], width: int, height: int,
        frames: int, seed: int, generate_audio: bool,
    ) -> dict[str, dict[str, Any]]:
        def node(kind: str, **inputs: Any) -> dict[str, Any]:
            return {"class_type": kind, "inputs": inputs}

        graph = {
            "1": node("UNETLoader", unet_name=self.MODEL_FILE, weight_dtype="default"),
            "2": node("CLIPLoader", clip_name=self.TEXT_ENCODER, type="minimax", device="default"),
            "3": node("VAELoader", vae_name=self.VIDEO_VAE),
            "4": node("VAELoader", vae_name=self.AUDIO_VAE),
            "5": node("LoraLoaderModelOnly", model=["1", 0], lora_name=self.TURBO_LORA, strength_model=1.0),
            "6": node("MiniMaxH3ReferenceToVideo", clip=["2", 0], vae=["3", 0],
                      audio_vae=["4", 0], prompt=prompt, width=width, height=height,
                      length=frames, ref_image_size="match"),
            "7": node("BasicGuider", model=["5", 0], conditioning=["6", 0]),
            "8": node("BasicScheduler", model=["5", 0], scheduler="simple", steps=4, denoise=1.0),
            "9": node("RandomNoise", noise_seed=seed),
            "10": node("KSamplerSelect", sampler_name="res_multistep"),
            "11": node("SamplerCustomAdvanced", noise=["9", 0], guider=["7", 0],
                       sampler=["10", 0], sigmas=["8", 0], latent_image=["6", 1]),
            "12": node("VAEDecode", samples=["11", 0], vae=["3", 0]),
            "14": node("CreateVideo", images=["12", 0], fps=24.0),
            "15": node("SaveVideo", video=["14", 0], filename_prefix="harness/h3",
                       format="mp4", codec={"codec": "auto"}),
        }
        for index, filename in enumerate(uploads):
            load_id = str(20 + index)
            graph[load_id] = node("LoadImage", image=filename)
            graph["6"]["inputs"][f"ref_images.ref_image_{index}"] = [load_id, 0]
        if generate_audio:
            graph["13"] = node("VAEDecodeAudio", samples=["11", 0], vae=["4", 0])
            graph["14"]["inputs"]["audio"] = ["13", 0]
        return graph

    @staticmethod
    def _saved_video(history: dict[str, Any]) -> dict[str, str]:
        outputs = history.get("outputs") or {}
        saved = outputs.get("15") or {}
        for value in saved.values():
            if not isinstance(value, list):
                continue
            for entry in value:
                if isinstance(entry, dict) and str(entry.get("filename") or "").lower().endswith(".mp4"):
                    return entry
        raise SeedanceError("ComfyUI 任务完成，但没有找到 H3 MP4 输出")

    def _generate_one(
        self, workspace_id: str, shot: dict[str, Any], group: str,
        reference_urls: list[str], reference_keys: list[str],
        dialogue_plan: dict[str, Any], sound_plan: dict[str, Any],
        options: dict[str, Any] | None = None, *, context: dict[str, Any] | None = None,
        preflight_checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_models()
        options = options or {}
        context = context or {}
        shot_index = int(shot.get("index", 0))
        if shot_index < 1:
            raise SeedanceError("H3 镜头缺少有效 shot_index")
        duration = int(shot.get("duration_seconds") or 10)
        frames = self._frame_count(duration)
        resolution = str(options.get("resolution") or self.resolution)
        ratio = str(options.get("ratio") or self.ratio)
        width, height = self._dimensions(resolution, ratio)
        generate_audio = bool(options.get("generate_audio", self.generate_audio))
        prompt = self._prompt(shot, duration, dialogue_plan, sound_plan, options)
        if reference_keys:
            prompt += "\nReference images (use only the listed visual identities):\n" + "\n".join(
                f"<Picture {index}> = {key}" for index, key in enumerate(reference_keys, 1)
            )
        assembly_log = self._assembly_log(context, shot, dialogue_plan, sound_plan)
        client, owns_client = self._get_client()
        try:
            uploads: list[str] = []
            for key, path_text in zip(reference_keys, reference_urls):
                path = Path(path_text)
                with path.open("rb") as source:
                    response = client.post(
                        f"{self.base_url}/upload/image",
                        files={"image": (f"harness-{uuid.uuid4().hex}{path.suffix.lower()}", source)},
                        data={"type": "input", "overwrite": "false"},
                    )
                response.raise_for_status()
                uploaded = response.json().get("name")
                if not uploaded:
                    raise SeedanceError(f"H3 参考图 {key} 上传后缺少文件名")
                uploads.append(str(uploaded))
            graph = self._workflow(
                prompt=prompt, uploads=uploads, width=width, height=height,
                frames=frames, seed=uuid.uuid4().int % (2**63),
                generate_audio=generate_audio,
            )
            response = client.post(
                f"{self.base_url}/prompt",
                json={"prompt": graph, "client_id": str(uuid.uuid4())},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("error") or body.get("node_errors"):
                raise SeedanceError(f"ComfyUI H3 工作流校验失败：{json.dumps(body, ensure_ascii=False)[:1200]}")
            job_id = str(body.get("prompt_id") or "")
            if not job_id:
                raise SeedanceError("ComfyUI 未返回 H3 prompt_id")
            deadline = time.monotonic() + self.poll_timeout_seconds
            while time.monotonic() < deadline:
                response = client.get(f"{self.base_url}/history/{job_id}")
                response.raise_for_status()
                task = response.json().get(job_id)
                if task:
                    status = task.get("status") or {}
                    if status.get("status_str") == "error" or status.get("completed") is False:
                        raise SeedanceError(f"ComfyUI H3 推理失败：{json.dumps(status, ensure_ascii=False)[:1000]}")
                    if status.get("completed"):
                        break
                self._sleep(self.poll_interval_seconds)
            else:
                raise SeedanceError(f"ComfyUI H3 推理超时（{self.poll_timeout_seconds} 秒），任务 {job_id}")
            saved = self._saved_video(task)
            filename = str(saved["filename"])
            subfolder = str(saved.get("subfolder") or "")
            if Path(filename).name != filename or ".." in Path(subfolder).parts:
                raise SeedanceError("ComfyUI 返回了不安全的输出路径")
            response = client.get(
                f"{self.base_url}/view",
                params={"filename": filename, "subfolder": subfolder, "type": "output"},
            )
            response.raise_for_status()
            if len(response.content) < 12 or response.content[4:8] != b"ftyp":
                raise SeedanceError("ComfyUI H3 输出不是有效的 MP4 文件")
            target_dir = self.output_dir / workspace_id / group
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"shot-{shot_index:03d}.mp4"
            temporary = target.with_suffix(".mp4.tmp")
            temporary.write_bytes(response.content)
            os.replace(temporary, target)
            local_url = f"/media/{workspace_id}/{group}/shot-{shot_index:03d}.mp4"
            digest = self._request_payload_digest(
                model=self.model, prompt=prompt, reference_keys=reference_keys,
                duration=duration, resolution=f"{width}x{height}", ratio=ratio,
                generate_audio=generate_audio,
                upstream_revisions=assembly_log.get("upstream_revisions") or {},
                shot_index=shot_index,
            )
            return {
                "shot_index": shot_index, "provider_job_id": job_id,
                "model": self.model, "url": local_url, "remote_url": "",
                "local_path": str(target), "duration_seconds": frames / 24,
                "resolution": f"{width}x{height}", "ratio": ratio,
                "generate_audio": generate_audio,
                "reference_image_count": len(reference_keys),
                "reference_keys": reference_keys,
                "request_payload_digest": digest,
                "assembly_log": assembly_log,
                "preflight_checks": preflight_checks or {"status": "pass"},
                "provider_generate_audio_echo": generate_audio,
                "audio_probe": self._audio_probe(target),
                "status": "succeeded",
            }
        except httpx.HTTPError as exc:
            raise SeedanceError(f"本地 ComfyUI H3 接口调用失败：{exc}") from exc
        finally:
            if owns_client:
                client.close()
