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

        # build.py is stdlib-only on purpose, so a bare interpreter is the
        # entire toolchain. CI installs the same thing via actions/setup-python.
        python = pkgs.python3;

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
            build
            serve
          ];

          shellHook = ''
            echo "recipes dev shell"
            echo "  recipes-build           build to dist/ (Pages base URL)"
            echo "  recipes-serve           build with base URL / and serve on :8000"
            echo "  python3 build.py --help other options"
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
