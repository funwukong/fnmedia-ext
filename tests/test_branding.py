"""关于页品牌：开发作者与更新源必须是自己的仓库（funwukong/fnmedia-ext），
不能再指向上游 yujian0815。"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import updater

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_updater_repo_is_own():
    assert updater.REPO_SLUG == "funwukong/fnmedia-ext"
    assert updater.API_BASE.startswith(
        "https://api.github.com/repos/funwukong/fnmedia-ext/releases")


def test_manifest_maintainer_is_own():
    manifest = open(os.path.join(ROOT, "packaging", "fpk", "manifest.in"),
                    encoding="utf-8").read()
    assert "maintainer            = funwukong" in manifest
    assert "https://github.com/funwukong/fnmedia-ext" in manifest
    assert "yujian0815" not in manifest


def test_about_page_names_own_author():
    ui = open(os.path.join(ROOT, "proxy", "admin_ui.py"),
              encoding="utf-8").read()
    start = ui.find('id="page-about"')
    end = ui.find('</section>', start)
    about = ui[start:end]
    assert "funwukong" in about
    assert "https://github.com/funwukong/fnmedia-ext" in about
    assert "yujian0815" not in about
