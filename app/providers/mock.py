from __future__ import annotations

from typing import Any

from .base import WorkflowProvider


class MockProvider(WorkflowProvider):
    """Deterministic provider used to exercise the full workflow without API keys."""

    name = "mock"

    def generate(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        brief = context["workspace"]["brief"]
        title = context["workspace"]["title"]
        assets = {item["kind"]: item["content"] for item in context["artifacts"]}

        if stage == "analysis":
            return {
                "logline": f"围绕《{title}》的本地化竖屏短剧样片。",
                "source_summary": brief,
                "genre": "家庭/身份反差/爽剧",
                "constraints": [
                    "9:16 竖屏",
                    "同一角色外观描述必须跨镜复用",
                    "先单镜验证，再批量生成",
                    "所有生成结果保留来源、版本和审批状态",
                ],
                "questions": ["目标市场与主要语言", "单集时长", "允许的模型和预算"],
            }
        if stage == "script":
            return {
                "title": title,
                "language": "English with light French phrases",
                "scenes": [
                    {
                        "index": 1,
                        "slug": "INT. COUNTRY FARMHOUSE - MORNING",
                        "summary": "主人公整理礼物并决定低调回家。",
                        "dialogue": "I only want to see my family. Keep my identity quiet.",
                    },
                    {
                        "index": 2,
                        "slug": "INT. PARIS MANOR - DAY",
                        "summary": "朴素外表与豪宅形成阶层反差，冲突建立。",
                        "dialogue": "Bonjour. I brought something from the farm.",
                    },
                    {
                        "index": 3,
                        "slug": "INT. GUEST CORRIDOR - DAY",
                        "summary": "身份被轻视，结尾留下反转钩子。",
                        "dialogue": "A true family should not need a title to show respect.",
                    },
                ],
                "continuity_rules": [
                    "保持现代法国背景",
                    "不引入未在本集素材出现的历史项目设定",
                    "结尾只留钩子，不提前兑现后续剧情",
                ],
            }
        if stage == "asset_manifest":
            return {
                "items": [
                    {"manifest_id": "CHAR_001", "asset_type": "character", "script_identity": "Mamie", "canonical_key": "CHAR_MAMIE", "source_function": "protagonist", "used_in": ["S01", "S02"], "continuity_lock": ["identity"], "decision": "CREATE", "library_asset_id": None, "confidence": 0.0, "reason": "mock library empty", "variant_requirements": [], "generation_requirements": ["create reusable character spec"]},
                    {"manifest_id": "LOC_001", "asset_type": "scene", "script_identity": "Manor hall", "canonical_key": "LOC_MANOR_HALL", "source_function": "class contrast", "used_in": ["S01", "S02"], "continuity_lock": ["layout"], "decision": "CREATE", "library_asset_id": None, "confidence": 0.0, "reason": "mock library empty", "variant_requirements": [], "generation_requirements": ["create reusable scene spec"]},
                    {"manifest_id": "PROP_001", "asset_type": "prop", "script_identity": "Travel bag", "canonical_key": "PROP_TRAVEL_BAG", "source_function": "cliffhanger", "used_in": ["S01", "S02"], "continuity_lock": ["identity"], "decision": "CREATE", "library_asset_id": None, "confidence": 0.0, "reason": "mock library empty", "variant_requirements": [], "generation_requirements": ["create reusable prop spec"]},
                ],
                "reuse_count": 0, "variant_count": 0, "create_count": 3, "unresolved_matches": [],
            }
        if stage == "characters":
            return {
                "items": [
                    {
                        "id": "char_helene",
                        "manifest_id": "CHAR_001",
                        "canonical_key": "CHAR_MAMIE",
                        "name": "Mamie Hélène",
                        "description": "62岁，银发盘起，法国乡村粗呢外套，慈祥但有掌控力。",
                        "voice": "warm, restrained, authoritative",
                        "reference_status": "placeholder",
                    },
                    {
                        "id": "char_antoine",
                        "name": "Antoine",
                        "description": "三件套西装，体面但犹豫，肢体语言拘谨。",
                        "voice": "soft, hesitant",
                        "reference_status": "placeholder",
                    },
                    {
                        "id": "char_bouchard",
                        "name": "Madame Bouchard",
                        "description": "高级定制套装、丝巾与珠宝，姿态高傲。",
                        "voice": "precise, condescending",
                        "reference_status": "placeholder",
                    },
                ]
            }
        if stage == "scenes":
            return {
                "items": [
                    {
                        "id": "scene_farm",
                        "manifest_id": "LOC_001",
                        "canonical_key": "LOC_MANOR_HALL",
                        "name": "法国乡村雪原农庄",
                        "description": "奥弗涅风格石屋，冬日晨光，真实欧洲乡村质感。",
                    },
                    {
                        "id": "scene_manor",
                        "name": "巴黎近郊庄园门厅",
                        "description": "水晶吊灯、大理石楼梯、克制的法式奢华。",
                    },
                ]
            }
        if stage == "props":
            return {
                "items": [
                    {"id": "prop_basket", "manifest_id": "PROP_001", "canonical_key": "PROP_TRAVEL_BAG", "name": "藤编提篮", "description": "手工旧藤篮"},
                    {"id": "prop_gifts", "name": "农庄礼物", "description": "奶酪、果酱与蜂蜜"},
                ]
            }
        if stage == "reference_images":
            items = []
            for source_kind in ("characters", "scenes", "props"):
                for item in assets.get(source_kind, {}).get("items", []):
                    items.append(
                        {
                            "source_kind": source_kind,
                            "source_id": item.get("id"),
                            "name": item.get("name"),
                            "status": "placeholder",
                            "note": "Mock 模式不生成真实参考图。",
                        }
                    )
            return {"items": items, "status": "mock"}
        if stage == "storyboard":
            script = assets.get("script", {})
            scenes = script.get("scenes", [])
            shots = []
            requested = int(context["workspace"].get("settings", {}).get("storyboard_count", 8))
            for index in range(1, requested + 1):
                scene = scenes[min((index - 1) // 3, max(len(scenes) - 1, 0))] if scenes else {}
                shots.append(
                    {
                        "index": index,
                        "duration_seconds": 10 if index != requested else 15,
                        "scene": scene.get("slug", "INT. SET - DAY"),
                        "story_beat": scene.get("summary", brief),
                        "asset_bindings": {
                            "characters": ["char_helene"],
                            "scene": "scene_farm" if index <= 3 else "scene_manor",
                            "props": ["prop_basket"] if index <= 3 else [],
                        },
                        "camera": "medium cinematic vertical composition",
                        "blocking": "subject action stays readable in the central safe area",
                        "visual_prompt": f"Vertical cinematic shot {index}; {scene.get('summary', brief)}",
                        "continuity": ["same cast reference", "same wardrobe", "9:16"],
                        "status": "planned",
                    }
                )
            return {"shots": shots, "estimated_seconds": sum(s["duration_seconds"] for s in shots)}
        if stage == "dialogue_plan":
            shots = assets.get("storyboard", {}).get("shots", [])
            script_scenes = assets.get("script", {}).get("scenes", [])
            items = []
            for shot in shots:
                index = int(shot["index"])
                scene = script_scenes[min((index - 1) // 3, max(len(script_scenes) - 1, 0))] if script_scenes else {}
                text = scene.get("dialogue", "") if index % 2 else ""
                items.append(
                    {
                        "shot_index": index,
                        "speaker_id": "char_helene" if text else None,
                        "text": text,
                        "language": "English with light French phrases",
                        "timing": "0.8s to 7.5s" if text else None,
                        "subtitle": text,
                        "lip_sync": "exact visible lip sync" if text else "no mouth movement",
                        "status": "planned" if text else "silent",
                    }
                )
            return {"items": items}
        if stage == "sound_plan":
            shots = assets.get("storyboard", {}).get("shots", [])
            return {
                "items": [
                    {
                        "shot_index": shot["index"],
                        "ambience": "restrained room tone matching the bound scene",
                        "foley": ["fabric movement", "natural footsteps"],
                        "cues": [{"at": "action", "sound": "story action Foley"}],
                        "ducking": "-5 dB while dialogue is present",
                        "negative_audio": ["no invented speech", "no loud background music"],
                        "status": "planned",
                    }
                    for shot in shots
                ]
            }
        if stage == "review":
            return {
                "checks": [
                    {"name": "timeline", "status": "pass", "evidence": "未检测到时代错配", "owner": "screenwriter", "remediation": "none"},
                    {"name": "character continuity", "status": "pass", "evidence": "角色引用已绑定", "owner": "character_designer", "remediation": "none"},
                    {"name": "story continuity", "status": "warn", "evidence": "建议人工复核倒数第二镜到末镜", "owner": "storyboard_director", "remediation": "人工检查转场"},
                    {"name": "cost gate", "status": "pass", "evidence": "先生成 1 个预览镜头", "owner": "art_director", "remediation": "none"},
                ],
                "blocking_failures": [],
            }
        if stage == "preview":
            return {
                "shot_index": 1,
                "provider_job_id": "mock-preview-001",
                "url": "mock://video/preview/1",
                "duration_seconds": 10,
                "cost_units": 12,
                "status": "succeeded",
            }
        if stage == "batch_video":
            shots = assets.get("storyboard", {}).get("shots", [])
            return {
                "items": [
                    {
                        "shot_index": shot["index"],
                        "provider_job_id": f"mock-video-{shot['index']:03d}",
                        "url": f"mock://video/shot/{shot['index']}",
                        "status": "succeeded",
                    }
                    for shot in shots
                ],
                "note": "Mock 模式只生成任务与元数据，不生成真实视频文件。",
            }
        if stage == "music_plan":
            shots = assets.get("storyboard", {}).get("shots", [])
            last_shot = shots[-1]["index"] if shots else 1
            return {
                "global_style": "French light comedy, restrained and warm",
                "cues": [
                    {
                        "start_shot": 1,
                        "end_shot": last_shot,
                        "mood": "warm tension building to a reveal",
                        "instrumentation": ["accordion", "jazz piano", "warm cello"],
                        "intensity": "low to medium",
                        "dialogue_avoidance": "duck under every spoken line",
                    }
                ],
                "ducking": "-8 dB under dialogue",
                "render_mode": "optional independent music API",
                "status": "planned",
            }
        if stage == "music":
            return {
                "plan": assets.get("music_plan", {}),
                "provider_job_id": "mock-music-001",
                "url": "mock://audio/bgm/1",
                "status": "succeeded",
            }
        if stage == "compose":
            return {
                "url": "mock://video/final/1",
                "resolution": "496x864",
                "duration_seconds": 85,
                "status": "succeeded",
                "note": "配置真实媒体 provider 与 FFmpeg 后替换为可播放文件。",
            }
        if stage == "delivery_qa":
            return {
                "status": "pass",
                "checks": [
                    {"name": "mock_master", "status": "pass", "evidence": "Mock 成片元数据存在"},
                    {"name": "shot_count", "status": "pass", "evidence": "所有 Mock 镜头均成功"},
                ],
                "blocking_failures": [],
                "note": "真实模式由本地 FFprobe 检查文件、流、时长和画幅。",
            }
        raise ValueError(f"Unknown stage: {stage}")
