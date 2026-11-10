# Working notes for agents

Blinding is a local llama.cpp lab. These rules apply to every agent that edits
the repo or runs a command in it.

## Write boundary

Write only under this checkout. The project keeps its podman store, model cache,
virtualenv, and temporary files under `.sandbox/`. Do not write to `$HOME`,
`~/.cache`, `~/.config`, `/etc`, `/nix`, `/tmp` outside the sandbox, or another
checkout. Do not run a host package manager, `sudo`, or a command that edits a
dotfile.

The container is started by `scripts/sandbox.sh`. It has a read-only root, the
checkout as its only bind mount, a tmpfs `/tmp`, and only `/dev/kfd` and
`/dev/dri/renderD128` passed for GPU work. If a command needs to write outside
the checkout, stop and report it.

## Verification

Run the gates inside the CPU image:

```bash
scripts/sandbox.sh run --cpu make check
```

Run a benchmark or training job through the same wrapper. A run outside the
sandbox is not evidence. The sandbox is created with `scripts/sandbox.sh init`
and removed with `scripts/sandbox.sh clean`; leave no containers, images, or
caches behind.

## Numbers

A number enters the README only when a `results/<run-id>/run.json` file backs it.
The record carries the commit, host, device, model hash, config, and validity.
Do not add a placeholder cell, a "documented" value, or a projected row. If a
measurement cannot be made, say so in prose and leave the row out.

## Prose

`tools/check_prose.py` is the mechanical gate. Run `make prose` after any change
to a document, a comment, or a script header. The rules are the ones in
`/home/mei/nixos/agent-settings.nix`: no em dash, no banned vocabulary, no
paired negation, no significance inflation, no list padded to three, and no
closing paragraph that restates the section.

## Figures

Scenes and PNGs live together in `docs/figures/`. Use the generator in
`docs/figures/build.py`, keep each figure to one claim, use the palette from
`/home/mei/nixos/agents/excalidraw.nix`, and inspect the rendered PNG before
committing it. Do not edit a PNG by hand.

## Git

The Lead commits. Do not push. A run that will be quoted in the README must be
made on a clean tree, then the results are committed after it.
