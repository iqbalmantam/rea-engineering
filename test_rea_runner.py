"""Uji rea_runner dengan CLI tiruan (tanpa jaringan, tanpa REA asli). Jalankan: python test_rea_runner.py"""

import json
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

import rea_runner as rr

FAKE = """#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
if a[0] == "analyze-javascript-application":
    print(json.dumps({"kind": "app", "modules": [{"id": i} for i in range(5)]}))
elif a[0] == "inspect-analysis-view":
    req = json.load(open(a[1]))
    print(json.dumps({"view": req["view"], "n_modules": len(req["source"]["evidence"]["modules"])}))
elif a[0] == "fail":
    sys.stderr.write("boom"); sys.exit(1)
elif a[0] == "slow":
    import time; time.sleep(5)
else:
    print(json.dumps({"argv": a}))
"""


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rea_test_"))
    fake = tmp / "fake_rea.py"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    prefix = [sys.executable, str(fake)]

    # 1. alur JS: analisis -> view streaming
    r = rr.run_cli(prefix, rr.build_args("js", Path("/x/app.asar"), {}), tmp, out_name="a.json")
    assert r.ok, r
    data, note = rr.load_json(r.stdout_path)
    assert data["kind"] == "app" and note is None
    req = rr.write_view_request(r.stdout_path, {"kind": "summary"}, tmp / "req.json")
    assert json.loads(req.read_text())["view"] == {"kind": "summary"}
    v = rr.run_cli(prefix, ["inspect-analysis-view", str(req), "--json"], tmp, out_name="v.json")
    assert json.loads(v.stdout_path.read_text())["n_modules"] == 5

    # 2. kegagalan dan timeout
    f = rr.run_cli(prefix, ["fail"], tmp, out_name="f.json")
    assert not f.ok and f.returncode == 1 and "boom" in f.stderr
    t = rr.run_cli(prefix, ["slow"], tmp, timeout=1, out_name="t.json")
    assert t.timed_out and not t.ok
    n = rr.run_cli(["/nonexistent/binary"], [], tmp, out_name="n.json")
    assert n.error and not n.ok

    # 3. argumen perintah
    assert rr.build_args("sqlite", Path("/d.db"), {"table": "t", "row_limit": 5}) == [
        "inspect-sqlite-database", "/d.db", "--json", "--table", "t", "--row-limit", "5"]
    assert rr.build_args("sqlite", Path("/d.db"), {}) == ["inspect-sqlite-database", "/d.db", "--json"]
    assert rr.build_members_args(Path("/a.dll"), ["A.B", " ", "C"]) == [
        "inspect-managed-members", "/a.dll", "--json", "--type", "A.B", "--type", "C"]

    # 4. JSON tidak valid / terlalu besar / kosong
    bad = tmp / "bad.json"; bad.write_text("{nope")
    assert rr.load_json(bad)[0] is None
    big = tmp / "big.json"; big.write_text("[1,2,3]")
    assert rr.load_json(big, limit=3)[0] is None
    (tmp / "empty.json").write_text("")
    assert rr.load_json(tmp / "empty.json")[0] is None

    # 5. ZIP aman vs zip-slip, dan folder tunggal dibuka otomatis
    ok_zip = tmp / "ok.zip"
    with zipfile.ZipFile(ok_zip, "w") as z:
        z.writestr("myapp/package.json", "{}")
        z.writestr("myapp/main.js", "x")
    root = rr.extract_zip_safe(ok_zip, tmp / "ex1")
    assert root.name == "myapp" and (root / "main.js").exists()
    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", "x")
    try:
        rr.extract_zip_safe(evil, tmp / "ex2")
        raise AssertionError("zip-slip lolos")
    except rr.UnsafeArchive:
        pass
    assert not (tmp.parent / "escape.txt").exists()

    # 6. nama berkas, versi Node, penemuan perintah
    assert rr.safe_filename("../../etc/pass wd.asar") == "pass_wd.asar"
    assert rr.safe_filename("...") == "target"
    for ver, exp in [("v22.22.0", True), ("v22.18.9", False), ("v24.11.0", True),
                     ("v24.10.0", False), ("v26.0.0", True), ("v20.1.0", False), (None, False)]:
        assert rr.node_version_ok(ver) is exp, ver
    os.environ["REA_COMMAND"] = "myrea --flag"
    assert rr.find_rea_command() == ["myrea", "--flag"]
    del os.environ["REA_COMMAND"]

    # 7. PATH diawali folder Python (tempat nodejs-wheel-binaries memasang node/npx)
    env = rr.tool_env()
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert "npm_config_cache" in env

    # 8. Penemuan Node dari paket pip (nodejs_wheel) dengan struktur tiruan
    wheel = tmp / "site" / "nodejs_wheel"
    (wheel / "bin").mkdir(parents=True)
    (wheel / "lib" / "node_modules" / "npm" / "bin").mkdir(parents=True)
    (wheel / "__init__.py").write_text("")
    fake_node = wheel / "bin" / "node"
    fake_node.write_text("#!/bin/sh\necho v22.20.0\n")
    fake_node.chmod(0o755)
    (wheel / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js").write_text("")
    sys.path.insert(0, str(tmp / "site"))
    try:
        node, npx_cli = rr.bundled_node()
        assert node == fake_node and npx_cli.name == "npx-cli.js"
        assert rr.tool_env()["PATH"].split(os.pathsep)[0] == str(fake_node.parent)
        cmd = rr.find_rea_command()
        assert cmd[:3] == [str(fake_node), str(npx_cli), "-y"], cmd
        assert rr.node_version() == "v22.20.0"
        d = rr.diagnostics()
        assert d["node"] == str(fake_node) and "bin" in d["isi_folder"]
    finally:
        sys.path.remove(str(tmp / "site"))
        sys.modules.pop("nodejs_wheel", None)

    rr.cleanup(tmp)
    print("SEMUA UJI LULUS")


if __name__ == "__main__":
    main()
