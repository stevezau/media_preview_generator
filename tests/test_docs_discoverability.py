"""Production analytics gates are exercised through the real Jekyll build."""

from __future__ import annotations

import json
from pathlib import Path

from tests.docs_toolchain import DOCS_DIR, run_bundle


def test_jekyll_analytics_gates_and_social_image_metadata(tmp_path: Path) -> None:
    code = """require 'jekyll'; require 'json'
      cases = JSON.parse(ARGV[0])
      cases.each do |c|
        ENV['JEKYLL_ENV'] = c['environment']
        config = Jekyll.configuration({'source'=>Dir.pwd, 'destination'=>c['out'], 'disable_disk_cache'=>true,
          'url'=>c['url'], 'goatcounter_endpoint'=>c['endpoint']})
        Jekyll::Site.new(config).process
      end"""
    cases = [
        {"name": "disabled", "environment": "production", "endpoint": "", "url": "https://mediapreviewgenerator.dev"},
        {
            "name": "enabled",
            "environment": "production",
            "endpoint": "https://mpg-test.goatcounter.com/count",
            "url": "https://mediapreviewgenerator.dev",
        },
        {
            "name": "development",
            "environment": "development",
            "endpoint": "https://mpg-test.goatcounter.com/count",
            "url": "https://mediapreviewgenerator.dev",
        },
        {
            "name": "preview",
            "environment": "production",
            "endpoint": "https://mpg-test.goatcounter.com/count",
            "url": "https://preview.example.com",
        },
    ]
    for case in cases:
        case["out"] = str(tmp_path / case["name"])
    result = run_bundle(["ruby", "-e", code, json.dumps(cases)], out_dir=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    for case in cases:
        root = Path(case["out"])
        html = (root / "index.html").read_text()
        assert ("data-endpoint=" in html) == (case["name"] == "enabled")
        assert ('id="docs-analytics-toggle"' in html) == (case["name"] == "enabled")
        assert "data-endpoint=" not in (root / "404.html").read_text()
        assert '<meta property="og:image:width" content="1280"' in html
        assert '<meta property="og:image:height" content="640"' in html
        assert '<meta property="og:image:alt"' in html
    # No analytics script can accidentally be imported into the private Flask app.
    templates = DOCS_DIR.parent / "media_preview_generator/web/templates"
    assert not any("goatcounter" in p.read_text() or "analytics.js" in p.read_text() for p in templates.rglob("*.html"))
