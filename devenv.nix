{ pkgs, config, ... }:
{
  # Full InferenceOps toolchain (nixpkgs attrs; note helm == kubernetes-helm).
  packages = [
    pkgs.kind
    pkgs.kubectl
    pkgs.terraform
    pkgs.kubernetes-helm
    pkgs.k6
    pkgs.trivy
    pkgs.docker
    pkgs.shellcheck
    pkgs.actionlint
    pkgs.ast-grep
    pkgs.jq
    pkgs.act
    pkgs.uv
    pkgs.kustomize
    pkgs.hadolint
  ];

  languages.python = {
    enable = true;
    version = "3.12";
    venv.enable = true;
    uv.enable = true;
    uv.sync.enable = true;
  };

  # Docker runs rootless on a custom per-user socket; wire DOCKER_HOST + kind provider.
  enterShell = ''
    export DOCKER_HOST=unix:///run/user/1000/docker-me.sock
    export KIND_EXPERIMENTAL_PROVIDER=docker
  '';
}
