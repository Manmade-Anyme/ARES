Vendored from https://github.com/shiyu-coder/Kronos, commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` (master).

MIT License, Copyright (c) 2025 ShiYu. Full license text: `model/LICENSE`.

Files `model/__init__.py`, `model/kronos.py`, `model/module.py` are copied
verbatim, unmodified, from the upstream `model/` package.

`paths.py` (sibling to `model/`, in this directory) is **not** upstream code —
it is a small derivative of `model.kronos.auto_regressive_inference` /
`KronosPredictor.predict`, written for ARES (TASK-184). Upstream's public
`predict()` internally samples `sample_count` stochastic forecast paths but
returns only their mean (`kronos.py`, `auto_regressive_inference`, final
`preds = np.mean(preds, axis=1)`) — individual paths are never exposed.
ARES's Monte-Carlo barrier-hit probability needs the actual path distribution,
not its mean, so `paths.py` reimplements the same autoregressive decode loop
with that averaging step removed, importing the unmodified tokenizer/model
primitives from `model.module` / `model.kronos`. Kept separate from `model/`
so the vendored package itself stays verbatim and diffable against upstream.
