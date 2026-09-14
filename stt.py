"""Platform-independent offline Parakeet recognition."""
import os
import threading
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
MODEL_DIR = Path(os.environ.get("LAUT_MODEL_DIR", Path(__file__).resolve().parent / "models" / "parakeet-v3"))


class Engine:
    def __init__(self):
        import sherpa_onnx

        def pick(*names):
            for name in names:
                path = MODEL_DIR / name
                if path.is_file():
                    return str(path)
            raise FileNotFoundError(f"Modelldatei {names} fehlt in {MODEL_DIR}. Bitte install-windows.ps1 ausführen.")

        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=pick("encoder.int8.onnx", "encoder.onnx"),
            decoder=pick("decoder.int8.onnx", "decoder.onnx"),
            joiner=pick("joiner.int8.onnx", "joiner.onnx"),
            tokens=pick("tokens.txt"),
            num_threads=max(1, int(os.environ.get("LAUT_THREADS", "6"))),
            sample_rate=SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search", model_type="nemo_transducer",
        )
        self.lock = threading.Lock()

    def decode(self, samples: np.ndarray, sample_rate=SAMPLE_RATE):
        if not samples.size:
            return ""
        with self.lock:
            stream = self.rec.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self.rec.decode_stream(stream)
            return stream.result.text.strip()
