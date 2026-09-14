"""Download only the expected model files; no shell, symlinks or unsafe extraction."""
from pathlib import Path
import shutil
import tarfile
import urllib.request

MODEL = Path(__file__).resolve().parents[1] / "models" / "parakeet-v3"
URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


def download():
    MODEL.mkdir(parents=True, exist_ok=True)
    if all((MODEL / name).is_file() and (MODEL / name).stat().st_size > 0 for name in FILES):
        print(f"Modell vorhanden: {MODEL}", flush=True)
        return
    archive = MODEL.parent / "parakeet-v3-int8.tar.bz2"
    partial = archive.with_suffix(".part")
    if not archive.exists():
        print("Sprachmodell wird heruntergeladen (~465 MB) …", flush=True)
        with urllib.request.urlopen(URL, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        partial.replace(archive)
    print("Entpacke Sprachmodell …", flush=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        for name in FILES:
            matches = [member for member in bundle.getmembers() if member.isfile() and Path(member.name).name == name]
            if len(matches) != 1:
                raise RuntimeError(f"Modelldatei fehlt oder mehrdeutig: {name}")
            target = MODEL / (name + ".part")
            with bundle.extractfile(matches[0]) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.replace(MODEL / name)
    print(f"Fertig: {MODEL}", flush=True)


if __name__ == "__main__":
    download()
