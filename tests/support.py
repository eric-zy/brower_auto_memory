"""只用于本地测试；生成的 MP4 是不可播放的占位文件。"""
import base64
from pathlib import Path

from kuaishou_upload.schema import file_hash
from kuaishou_upload.state import atomic_json


def make_materials(folder: Path, *, name="demo", episode=1, test_mode=False):
    folder.mkdir(parents=True, exist_ok=True)
    video, proof, cover = folder / f"{name}.mp4", folder / "proof.png", folder / "cover.png"
    video.write_bytes(b"LOCAL TEST ONLY - NOT A VIDEO: " + name.encode())
    pixel = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR1cAAAAASUVORK5CYII=")
    proof.write_bytes(pixel)
    cover.write_bytes(pixel)
    sidecar = video.with_suffix(".kuaishou.json")
    atomic_json(sidecar, {
        "seriesTitle": "本地测试短剧", "synopsis": "仅测试代码，不上传", "testMode": test_mode,
        "episodes": [{"number": episode, "title": f"第{episode}集", "sourceFile": str(video), "paid": False, "price": 0}],
        "mediaInfo": {"sha256": file_hash(video), "durationSeconds": 120},
        "copyright": {"proofType": "权属声明", "proofFile": str(proof), "authorizedForUpload": True,
                      "validFrom": "2020-01-01", "validUntil": "2099-12-31", "allowSublicense": False},
        "filing": {"hasNumber": True, "number": "V00000000000000"}, "coverFile": str(cover), "sellingMode": "free",
        "platformFields": {}, "platformHistory": {"contentId": None, "uploadedEpisodes": []},
    })
    return sidecar
