"""Embedding fine-tune loop — InfoNCE over mined contrastive triplets.

Fine-tunes the base embedding model (Nomic Embed v1.5) with
MultipleNegativesRankingLoss (the sentence-transformers InfoNCE objective) on
``(query, gold_passage, hard_negatives)`` triplets, and saves the candidate
checkpoint to ``output_dir``.

The heavy training backend is injectable (``train_fn``), mirroring how
``Embedder`` injects ``encode_fn``: unit tests pass a stub that writes a sentinel
checkpoint, so the loop's orchestration is tested without loading the 0.5 GB
model or running gradient descent. The default backend lazily imports
sentence-transformers and runs the real fit.

Determinism: the trainer seeds torch/numpy/python RNGs from ``config.seed`` so
two runs over the same triplets produce the same checkpoint (the research thesis
needs reproducible candidates).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from os import PathLike
from pathlib import Path

from helix.rag.trainer.triplets import Triplet

DEFAULT_BASE_MODEL = "nomic-ai/nomic-embed-text-v1.5"


@dataclass(frozen=True)
class TrainConfig:
    """Fine-tune hyperparameters (mirrors data-model.md embedding_jobs.config)."""

    lr: float = 2e-5
    batch_size: int = 16
    epochs: int = 3
    loss: str = "info_nce"
    negatives_per_query: int = 4
    warmup_ratio: float = 0.1
    seed: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class TrainResult:
    base_model: str
    output_dir: str
    triplets_count: int
    config: TrainConfig = field(default_factory=TrainConfig)


# train_fn(base_model, triplets, output_dir, config) -> None  (saves to output_dir)
TrainFn = Callable[[str, list[Triplet], str, TrainConfig], None]


def _seed_everything(seed: int) -> None:
    import random

    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except ImportError:  # pragma: no cover - numpy ships with torch
        pass
    try:
        import torch

        torch.manual_seed(seed)
    except ImportError:  # pragma: no cover - torch ships with sentence-transformers
        pass


# The Nomic Embed v1.5 remote modeling code round-trips its transformer weights
# under a doubled key prefix: ``save_pretrained`` writes ``encoder.encoder.layers.*``
# but the model structure expects ``encoder.layers.*``, so a reload silently drops
# every fine-tuned transformer-layer tensor (only the 4 embedding/layernorm keys
# bind) and falls back to base weights. The candidate then *looks* trained but
# embeds like the base model — a silent no-op that poisons every canary. We strip
# the erroneous doubling from the saved checkpoint so it round-trips.
_DOUBLED_PREFIX = "encoder.encoder."
_CANONICAL_PREFIX = "encoder."


def _normalize_nomic_checkpoint(output_dir: str) -> int:
    """Rewrite saved ``*.safetensors`` to de-double the ``encoder.encoder.`` prefix.

    Idempotent and self-limiting: it only touches keys that carry the doubled
    prefix, so a checkpoint saved by a future (fixed) library version is left
    untouched. Returns the number of keys rewritten across all shards.
    """
    from safetensors import safe_open
    from safetensors.torch import save_file

    rewritten = 0
    for shard in Path(output_dir).glob("*.safetensors"):
        with safe_open(shard, framework="pt") as handle:
            metadata = handle.metadata() or {"format": "pt"}
            tensors = {key: handle.get_tensor(key) for key in handle.keys()}
        if not any(key.startswith(_DOUBLED_PREFIX) for key in tensors):
            continue
        fixed = {
            (_CANONICAL_PREFIX + key[len(_DOUBLED_PREFIX) :])
            if key.startswith(_DOUBLED_PREFIX)
            else key: value
            for key, value in tensors.items()
        }
        rewritten += sum(1 for key in tensors if key.startswith(_DOUBLED_PREFIX))
        save_file(fixed, shard, metadata=metadata)
    return rewritten


def _default_train_fn(
    base_model: str, triplets: list[Triplet], output_dir: str, config: TrainConfig
) -> None:
    """Real backend: sentence-transformers MultipleNegativesRankingLoss fit + save."""
    from sentence_transformers import InputExample, SentenceTransformer
    from sentence_transformers.losses import MultipleNegativesRankingLoss
    from torch.utils.data import DataLoader

    _seed_everything(config.seed)

    model = SentenceTransformer(base_model, trust_remote_code=True)
    examples = [InputExample(texts=t.to_texts()) for t in triplets]
    # A list of InputExample is the canonical sentence-transformers map-style
    # dataset (supports __getitem__/__len__); torch's stub only accepts Dataset.
    loader: DataLoader[InputExample] = DataLoader(
        examples,  # type: ignore[arg-type]
        shuffle=True,
        batch_size=config.batch_size,
    )
    loss = MultipleNegativesRankingLoss(model)
    warmup_steps = int(len(loader) * config.epochs * config.warmup_ratio)
    model.fit(
        train_objectives=[(loader, loss)],
        epochs=config.epochs,
        warmup_steps=warmup_steps,
        optimizer_params={"lr": config.lr},
        show_progress_bar=False,
    )
    model.save(output_dir)
    _normalize_nomic_checkpoint(output_dir)


def train_embedding(
    triplets: list[Triplet],
    output_dir: str | PathLike[str],
    *,
    base_model: str = DEFAULT_BASE_MODEL,
    config: TrainConfig | None = None,
    train_fn: TrainFn | None = None,
) -> TrainResult:
    """Fine-tune ``base_model`` on ``triplets`` and save to ``output_dir``.

    Raises ``ValueError`` when ``triplets`` is empty — there is nothing to train
    on, and a no-op checkpoint would silently masquerade as a fine-tune.
    """
    if not triplets:
        raise ValueError("train_embedding requires at least one triplet")

    cfg = config or TrainConfig()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    backend = train_fn or _default_train_fn
    backend(base_model, triplets, str(out), cfg)

    return TrainResult(
        base_model=base_model,
        output_dir=str(out),
        triplets_count=len(triplets),
        config=cfg,
    )
