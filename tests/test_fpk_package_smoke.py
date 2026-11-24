"""Build and inspect an FPK archive from the current source tree."""
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]


def test_packager_has_fixed_archive_mtime():
    import sys
    sys.path.insert(0, str(REPO / "packaging" / "fpk"))
    import fpkpack

    info = fpkpack._dirinfo("cmd")
    assert info.mtime == fpkpack.REPRODUCIBLE_MTIME


def test_packager_rejects_host_temp_paths():
    """临时目录/盘符路径必须被拒，避免测试产物混进 app.tgz。"""
    import sys
    sys.path.insert(0, str(REPO / "packaging" / "fpk"))
    import fpkpack

    bad_names = (
        "repo/C:UsersxuAppDataLocalTemppytest-of-xupytest-1dist/app.tgz",
        "repo/C?UsersxuAppDataLocalTempfnmedia-v122-final-stage/app/repo",
        "repo/Users/x/AppDataLocalTemp/x.py",
        "repo/pytest-of-xu/pytest-1/app",
        "/abs/path",
        "repo/../escape",
        "repo//double",
    )
    for name in bad_names:
        with pytest.raises(fpkpack.PackError):
            fpkpack._validate_member_name(name)

    for name in ("repo/proxy/app.py", "ui/config", "cmd/main",
                 "repo/tests/test_x.py"):
        fpkpack._validate_member_name(name)   # 不抛异常


def _build_fpk(tmp_path):
    output = tmp_path / "dist"
    proc = subprocess.run(
        ["bash", "packaging/fpk/build.sh", "--out", str(output)],
        cwd=REPO, check=True, capture_output=True, text=True, timeout=120,
    )
    packages = list(output.glob("*.fpk"))
    assert len(packages) == 1, proc.stdout + proc.stderr
    package = packages[0]
    with gzip.open(package, "rb") as stream:
        outer_blob = stream.read()
    with tarfile.open(fileobj=io.BytesIO(outer_blob), mode="r:") as outer:
        outer_members = {member.name: member for member in outer.getmembers()}
        manifest = outer.extractfile("manifest").read().decode("utf-8")
        app_blob = outer.extractfile("app.tgz").read()
    with tarfile.open(fileobj=io.BytesIO(app_blob), mode="r:gz") as app:
        app_members = {member.name.rstrip("/"): member for member in app.getmembers()}
    return package, outer_members, app_members, manifest


@pytest.mark.skipif(os.name == "nt", reason="FPK shell packaging requires POSIX path semantics")
def test_fpk_archive_contents_and_integrity(tmp_path):
    package, outer, app, manifest = _build_fpk(tmp_path)
    assert package.stat().st_size > 0
    assert "cmd/main" in outer
    assert outer["cmd/main"].mode & 0o111
    assert "ui/config" in app
    assert "repo/proxy/line_identity.py" in app
    assert "repo/proxy/playback_target.py" in app
    assert "repo/proxy/assets/drpy/sidecar.mjs" in app
    assert "repo/proxy/assets/drpy/drpy3.js" in app
    assert "repo/proxy/assets/drpy/node_vendor/cheerio.min.js" in app
    assert "repo/proxy/drpy.py" in app
    assert "repo/proxy/runner_client.py" in app
    assert "repo/proxy/runner_errors.py" in app

    version = (REPO / "VERSION").read_text().splitlines()[0].strip().split("-")[0]
    fields = {key.strip(): value.strip() for line in manifest.splitlines() if "=" in line
              for key, value in [line.split("=", 1)]}
    assert fields["version"] == version
    assert fields["checksum"] == hashlib.md5(_extract_app_blob(package)).hexdigest()

    for member in app:
        path = member.lstrip("./")
        lower = path.lower()
        assert not lower.endswith((".jar", ".dex", ".apk", ".class")), path
        assert "/android/" not in f"/{lower}/", path
        assert "csp" not in lower, path
        assert "catvod" not in lower or lower == "repo/proxy/catvod.py", path
        assert not path.startswith("repo/spider-runner/android/"), path
        assert not path.startswith("repo/proxy/assets/catvod/"), path
        assert not path.startswith("repo/proxy/assets/guard/"), path


def _extract_app_blob(package):
    with gzip.open(package, "rb") as stream:
        raw = stream.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as outer:
        return outer.extractfile("app.tgz").read()
