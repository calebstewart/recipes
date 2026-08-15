{
  description = "Recipe collection and its static site generator";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # build.py declares its dependencies in a PEP 723 header. CI runs it
        # with `uv run`, which resolves that header; here the same packages come
        # from nixpkgs so a plain `python3 build.py` works in the dev shell.
        python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);

        build = pkgs.writeShellApplication {
          name = "recipes-build";
          runtimeInputs = [ python ];
          text = ''python3 build.py "$@"'';
        };

        serve = pkgs.writeShellApplication {
          name = "recipes-serve";
          runtimeInputs = [ python ];
          text = ''
            python3 build.py --base-url / --out dist
            echo "Serving http://localhost:''${PORT:-8000}/ (Ctrl-C to stop)"
            python3 -m http.server "''${PORT:-8000}" --directory dist
          '';
        };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            build
            serve
          ];

          shellHook = ''
            echo "recipes dev shell"
            echo "  recipes-build           build to dist/ (Pages base URL)"
            echo "  recipes-serve           build with base URL / and serve on :8000"
            echo "  python3 build.py --help other options"
            echo "  uv run build.py         resolve deps from the PEP 723 header instead"
          '';
        };

        apps = {
          build = {
            type = "app";
            program = "${build}/bin/recipes-build";
          };
          serve = {
            type = "app";
            program = "${serve}/bin/recipes-serve";
          };
          default = {
            type = "app";
            program = "${serve}/bin/recipes-serve";
          };
        };

        formatter = pkgs.nixfmt-rfc-style;
      }
    );
}
