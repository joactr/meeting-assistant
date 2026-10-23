"""Multilingual E5 ONNX, exact prefixing, attention pooling and normalization."""

from pathlib import Path

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import serve


class E5Runtime:
    def __init__(self, model_directory: Path, options: dict):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model_directory / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.tokenizer.no_truncation()
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = int(options.get("num_threads", 4))
        self.session = ort.InferenceSession(
            str(model_directory / "model.onnx"),
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )

    def capabilities(self):
        return {
            "dimensions": 384,
            "embedding_tokens": 512,
            "prefixes": {"query": "query: ", "document": "passage: "},
            "normalization": "l2",
            "execution": "local",
        }

    def run(self, operation, payload, cancelled):
        if operation == "tokenize":
            encoded = self.tokenizer.encode_batch(payload["texts"])
            return {
                "counts": [sum(item.attention_mask) for item in encoded],
                "ids": [item.ids[: sum(item.attention_mask)] for item in encoded],
                "offsets": [item.offsets[: sum(item.attention_mask)] for item in encoded],
                "tokenizer": "multilingual-e5-small-pinned",
            }
        if operation != "embed":
            raise ValueError("Unsupported E5 operation")
        purpose = payload["purpose"]
        if purpose not in {"query", "document"}:
            raise ValueError("Unknown embedding purpose")
        prefix = "query: " if purpose == "query" else "passage: "
        texts = payload["texts"]
        vectors = []
        for offset in range(0, len(texts), 32):
            if cancelled.is_set():
                raise InterruptedError()
            encoded = self.tokenizer.encode_batch(
                [prefix + text for text in texts[offset : offset + 32]]
            )
            if any(sum(item.attention_mask) > 512 for item in encoded):
                raise ValueError("E5 input exceeds 512 tokens including prefix and special tokens")
            ids = self.np.asarray([item.ids for item in encoded], dtype=self.np.int64)
            masks = self.np.asarray([item.attention_mask for item in encoded], dtype=self.np.int64)
            inputs = {"input_ids": ids, "attention_mask": masks}
            if any(item.name == "token_type_ids" for item in self.session.get_inputs()):
                inputs["token_type_ids"] = self.np.zeros_like(ids)
            output = self.session.run(None, inputs)[0]
            pooled = (output * masks[:, :, None]).sum(axis=1) / masks.sum(axis=1, keepdims=True)
            norms = self.np.linalg.norm(pooled, axis=1, keepdims=True)
            if (
                pooled.shape[1] != 384
                or self.np.any(norms == 0)
                or not self.np.isfinite(pooled).all()
            ):
                raise ValueError("E5 returned invalid vector space")
            vectors.extend((pooled / norms).tolist())
        return {
            "vectors_by_input_index": vectors,
            "dimensions": 384,
            "fingerprint": payload["model_fingerprint"],
            "usage": None,
        }

    def close(self):
        self.session = None
        self.tokenizer = None


if __name__ == "__main__":
    serve(E5Runtime, "embeddings")
