"""CI 发布 tag 派生（P0-5）：.github/workflows/build-fpk.yml 里的 Release 步。

GitHub Actions 本地跑不了，这里把 workflow 里两个 Release 步的 run: 脚本
原文取出来，在桩 gh 下用 bash 真实执行，覆盖两条触发路径：
- tag push：GITHUB_REF=refs/tags/v<VERSION>-fpk；
- workflow_dispatch + publish：GITHUB_REF=refs/heads/<分支>，此时
  GITHUB_REF_NAME 是分支名（如 main），不能用它拼 tag，必须由 VERSION
  派生 v<VERSION>-fpk（与校验步同款写法）。
只用标准库解析（CI 测试环境不装 PyYAML），按固定缩进取 run: 块。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/build-fpk.yml"


def _release_scripts() -> dict[str, str]:
    """取出 full/public 两 job 里 Release 步的 run: 脚本原文。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    scripts: dict[str, str] = {}
    job = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("  ") and not line.startswith("   ") \
                and line.strip().endswith(":"):
            job = line.strip().rstrip(":")
        if line.strip().startswith("- name: 发布 Release"):
            j = i + 1
            while j < len(lines) and lines[j].strip() != "run: |":
                j += 1
            assert j < len(lines), f"Release 步缺少 run: 块（{job}）"
            body: list[str] = []
            k = j + 1
            while k < len(lines) and (lines[k].startswith("          ")
                                      or not lines[k].strip()):
                body.append(lines[k][10:] if lines[k].startswith("          ")
                            else lines[k])
                k += 1
            scripts[job] = "\n".join(body).rstrip("\n") + "\n"
            i = k
            continue
        i += 1
    assert scripts, "未在 build-fpk.yml 里找到 Release 步"
    return scripts


class BuildWorkflowTagTests(unittest.TestCase):
    def test_release_tag_is_version_fpk_on_both_paths(self):
        version = (REPO / "VERSION").read_text(
            encoding="utf-8").splitlines()[0].strip()
        expected = f"v{version}-fpk"
        scripts = _release_scripts()
        self.assertEqual(set(scripts), {"full", "public"})
        cases = [
            ("tag-push", {"GITHUB_REF": f"refs/tags/{expected}",
                          "GITHUB_REF_NAME": expected}),
            ("dispatch-分支", {"GITHUB_REF": "refs/heads/main",
                               "GITHUB_REF_NAME": "main"}),
            ("dispatch-未设REF_NAME", {"GITHUB_REF": "refs/heads/main"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "VERSION").write_text(version + "\n", encoding="utf-8")
            dist = tmpdir / "dist"
            dist.mkdir()
            (dist / "dummy.fpk").write_text("dummy", encoding="utf-8")
            bindir = tmpdir / "bin"
            bindir.mkdir()
            gh = bindir / "gh"
            gh.write_text('#!/bin/sh\necho "GH-CALLED: $@"\nexit 0\n',
                          encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                     | stat.S_IXOTH)
            for job, script in sorted(scripts.items()):
                script_path = tmpdir / f"run_release_{job}.sh"
                script_path.write_text(script, encoding="utf-8")
                for label, extra in cases:
                    env = dict(os.environ)
                    env["PATH"] = f"{bindir}:{env['PATH']}"
                    env.pop("GITHUB_REF", None)
                    env.pop("GITHUB_REF_NAME", None)
                    env.update(extra)
                    proc = subprocess.run(
                        ["bash", "-e", str(script_path)],
                        cwd=tmpdir, env=env, capture_output=True,
                        text=True, timeout=60)
                    self.assertEqual(proc.returncode, 0,
                                     f"{job}/{label}: {proc.stdout} {proc.stderr}")
                    called = [ln for ln in proc.stdout.splitlines()
                              if ln.startswith("GH-CALLED:")]
                    self.assertEqual(len(called), 1, f"{job}/{label}: {proc.stdout}")
                    tag = called[0].split()[3]
                    self.assertEqual(tag, expected, f"{job}/{label}")

    @unittest.skipUnless((REPO / "tools/check_tag_version.py").exists(),
                          "公开构建已剥离 tools/，校验脚本不存在时跳过")
    def test_check_tag_version_accepts_fpk_and_rejects_bare(self):
        version = (REPO / "VERSION").read_text(
            encoding="utf-8").splitlines()[0].strip()
        for tag, want_ok in ((f"v{version}-fpk", True),
                             (f"v{version}", False)):
            proc = subprocess.run(
                [sys.executable, "tools/check_tag_version.py",
                 "--tag", tag],
                cwd=REPO, capture_output=True, text=True, timeout=60)
            if want_ok:
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("OK", proc.stdout)
            else:
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("FAIL", proc.stdout)


    @unittest.skipUnless((REPO / "tools/check_tag_version.py").exists(),
                          "公开构建已剥离 tools/，校验脚本不存在时跳过")
    def test_check_tag_version_no_tag_skips_ok(self):
        # P1-23(6)：无 tag 输入（且环境里无 GITHUB_REF_NAME）直接测脚本本身：
        # 返回 0 并打印 SKIP，不依赖 workflow 的 if 兜底。
        env = dict(os.environ)
        env.pop("GITHUB_REF_NAME", None)
        proc = subprocess.run(
            [sys.executable, "tools/check_tag_version.py"],
            cwd=REPO, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SKIP", proc.stdout)

    @unittest.skipUnless((REPO / "tools/check_tag_version.py").exists(),
                          "公开构建已剥离 tools/，校验脚本不存在时跳过")
    def test_check_tag_version_rejects_bad_and_mismatched_tags(self):
        # P1-23(6)：非法格式与版本不匹配都返回非零并打印 FAIL。
        version = (REPO / "VERSION").read_text(
            encoding="utf-8").splitlines()[0].strip()
        env = dict(os.environ)
        env.pop("GITHUB_REF_NAME", None)
        for tag in ("v1.2-fpk", "v1.2.3", "not-a-tag",
                    "v9.9.9-fpk", f"v{version}-beta"):
            proc = subprocess.run(
                [sys.executable, "tools/check_tag_version.py",
                 "--tag", tag],
                cwd=REPO, capture_output=True, text=True, timeout=60,
                env=env)
            self.assertNotEqual(proc.returncode, 0,
                                f"{tag}: {proc.stdout}")
            self.assertIn("FAIL", proc.stdout, tag)


if __name__ == "__main__":
    unittest.main()
