# Development shell for the Blinding llama.cpp measurement lab.
#
# The shell carries the host side of the workflow. Python 3.12 and uv run the
# package and the make gates. The run records take the commit and tree state
# from git, and scripts/sandbox.sh drives podman. ROCm, llama.cpp, and hipcc
# stay in the images under sandbox/, because a measurement is taken there.
#
# scripts/render_figures.sh expects the excalidraw CLI on the host. Figure
# rendering is authoring work, so the shell does not install the CLI.
#
# uv sync runs at shell entry and installs from pyproject.toml and uv.lock.
# devenv keeps the environment under .devenv/state, exports it as
# UV_PROJECT_ENVIRONMENT, and activates it, so the make targets reach their
# tools through uv run.

{ pkgs, config, ... }:

{
  languages.python = {
    enable = true;
    version = "3.12";
    venv.enable = true;
    uv = {
      enable = true;
      sync.enable = true;
    };
  };

  # Host tools for the Makefile, the scripts, and manual checks from the
  # shell. The GPU stack and llama.cpp are built into the container images.
  packages = with pkgs; [
    bash
    curl
    git
    gnumake
    podman
  ];

  # Keep uv's cache inside the checkout, the way scripts/sandbox.sh does for
  # container runs. The working notes put writable state under .sandbox/.
  env.UV_CACHE_DIR = "${config.devenv.root}/.sandbox/uv-cache";
}
