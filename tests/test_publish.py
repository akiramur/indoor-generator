import json

from indoor_gen import publish


def write(out, slug, name):
    d = out / slug
    d.mkdir(parents=True)
    fc = {"type": "FeatureCollection", "metadata": {"building": name},
          "features": [{"type": "Feature", "properties": {"id": "a", "type": "room", "level": 1, "source": None},
                        "geometry": {"type": "Point", "coordinates": [0, 0]}}]}
    (d / "indoor.geojson").write_text(json.dumps(fc), encoding="utf-8")
    return d


def test_collect_skips_samples_and_tags_buildings(tmp_path):
    out = tmp_path / "output"
    write(out, "real", "本物")
    s = write(out, "sample", "サンプル")
    (s / "build_info.json").write_text(json.dumps({"config_path": str(tmp_path / "samples" / "x.json")}))
    (tmp_path / "samples").mkdir()
    found, skipped = publish.collect(out, tmp_path)
    assert [s for s, _ in found] == ["real"] and skipped == ["sample"]
    nd, counts = publish.to_ndjson(found)
    feat = json.loads(nd.splitlines()[0])
    assert counts == {"real": 1}
    assert feat["properties"]["building"] == "real" and feat["properties"]["building_name"] == "本物"
    assert "source" not in feat["properties"]  # nulls dropped


def test_recipe_points_at_the_source():
    r = publish.recipe("example-user", "indoor", 14, 16)
    assert r["layers"]["indoor"]["source"] == "mapbox://tileset-source/example-user/indoor"


def test_viewer_page_is_filled_in_and_script_safe():
    fc = {"features": [{"properties": {"type": "structure", "id": "structure-x", "name": "</script>X",
                                       "center_lon": 1.0, "center_lat": 2.0}}]}
    page = publish.viewer_html("__TILESET_ID_JSON__ __STRUCTURES__ __SNIPPET__ __TOKEN__ __TILESET__", "example-user", "indoor-dev",
                               [("x", fc)], "pk.test")
    assert "__" not in page and '"example-user.indoor-dev"' in page and "structure-x" in page
    assert "</script>" not in page  # escaped


def test_resolve_target_defaults_to_dev_and_prod_is_explicit():
    cfg = {"default_target": "dev", "targets": {"dev": "indoor-dev", "prod": "indoor"}}
    assert publish.resolve_target(cfg) == ("dev", "indoor-dev")
    assert publish.resolve_target(cfg, target="prod") == ("prod", "indoor")
    assert publish.resolve_target(cfg, tileset="indoor-test") == ("custom", "indoor-test")
    import pytest
    with pytest.raises(publish.PublishError):
        publish.resolve_target(cfg, target="staging")


def test_style_snippet_draws_point_pois_and_lists_their_badges():
    snip = publish.style_snippet("u", "indoor", ["structure-x"])
    layer = next(l for l in snip["layers"] if l["id"] == "indoor-poi")
    assert layer["type"] == "symbol" and layer["layout"]["icon-image"] == ["concat", "poi-", ["get", "icon"]]
    assert ["==", ["get", "shape_type"], "poi"] in layer["filter"]
    assert {"elevator", "stairs", "toilet", "toilet-male", "toilet-female"} <= set(snip["poi_badges"])


def test_preview_gets_the_poi_badges_from_publish():
    from indoor_gen import build_preview

    page = build_preview.render({"type": "FeatureCollection", "metadata": {}, "features": []})
    assert "__POI_BADGES__" not in page and '"toilet-male"' in page
