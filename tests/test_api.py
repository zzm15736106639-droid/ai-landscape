from backend.app import ALLOWED_JOB_FIELDS, create_app


def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_job_schema_is_intentionally_small():
    assert ALLOWED_JOB_FIELDS == {
        "videos",
        "output_dir",
        "gpu_mode",
        "workers",
        "output_video_bitrate_k",
        "ai_crop_bounds",
        "subtitle_configs",
        "effect_all_template_ids",
        "effect_random_template_ids",
    }


def test_removed_fields_are_rejected_before_media_work():
    response = client().post('/api/ai-landscape', json={
        "videos": [],
        "output_dir": "x",
        "blur_regions": [],
    })
    assert response.status_code == 400
    assert "已移除参数" in response.get_json()["error"]


def test_unknown_fields_are_rejected():
    response = client().post('/api/ai-landscape', json={
        "videos": [],
        "output_dir": "x",
        "mystery": True,
    })
    assert response.status_code == 400
    assert "未知参数" in response.get_json()["error"]
