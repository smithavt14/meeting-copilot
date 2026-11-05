"""Silero VAD through onnxruntime: one speech probability per 32 ms chunk of 16 kHz audio."""

from __future__ import annotations

from pathlib import Path

import numpy as np

RATE = 16000
CHUNK = 512  # samples per Silero call at 16 kHz (32 ms)
CONTEXT = 64  # Silero v5 wants the last 64 samples of the previous chunk in front of each one


class SileroVAD:
    def __init__(self, model_path: str | Path) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        names = {i.name for i in self.session.get_inputs()}
        if "state" not in names:
            raise RuntimeError("expected a Silero v5 model (inputs input, state, sr)")
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        """Speech probability for exactly CHUNK samples."""
        x = np.concatenate([self._context, chunk.astype(np.float32)])[None, :]
        out, self._state = self.session.run(None, {"input": x, "state": self._state, "sr": np.array(RATE, dtype=np.int64)})
        self._context = chunk[-CONTEXT:].astype(np.float32)
        return float(out.reshape(-1)[0])
