# Flake entry point for the Blinding development shell.
#
# devenv.nix holds the shell definition for both entry points: the devenv CLI
# reads devenv.yaml, and nix develop reads this file. Each lock file pins the
# inputs for its own path.
#
# The devenv input is the release tag the host CLI comes from, and its nixpkgs
# follows the flake nixpkgs so evaluation fetches one copy of it.

{
  description = "Blinding llama.cpp measurement lab development shell";

  inputs = {
    nixpkgs.url = "github:cachix/devenv-nixpkgs/rolling";

    nixpkgs-python.url = "github:cachix/nixpkgs-python";
    nixpkgs-python.inputs.nixpkgs.follows = "nixpkgs";

    devenv.url = "github:cachix/devenv/v2.4.0";
    devenv.inputs.nixpkgs.follows = "nixpkgs";
  };

  nixConfig = {
    extra-trusted-public-keys = "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=";
    extra-substituters = "https://devenv.cachix.org";
  };

  outputs =
    { nixpkgs, devenv, ... }@inputs:
    {
      devShells.x86_64-linux.default = devenv.lib.mkShell {
        inherit inputs;
        pkgs = nixpkgs.legacyPackages.x86_64-linux;
        modules = [ ./devenv.nix ];
      };

      formatter.x86_64-linux = nixpkgs.legacyPackages.x86_64-linux.nixfmt-rfc-style;
    };
}
