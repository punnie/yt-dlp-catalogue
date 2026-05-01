{
  description = "yt-archive: a thin wrapper around yt-dlp that catalogues downloads in SQLite";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # The package builder. Exposed as a function of `pkgs` so consumers can
      # call it against their own nixpkgs, or use `.override { yt-dlp = ...; }`
      # on the resulting derivation to swap dependencies (notably yt-dlp,
      # which often needs to track upstream master to keep up with site
      # breakage).
      packageFn =
        { lib, python3, ffmpeg, yt-dlp ? python3.pkgs.yt-dlp }:
        python3.pkgs.buildPythonApplication {
          pname = "yt-archive";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [ python3.pkgs.setuptools ];

          # `yt-dlp` is taken as an override-able input. Pass either
          # `python3Packages.yt-dlp` (a Python module) or a derivation built
          # the same way — anything that exposes the `yt_dlp` import.
          dependencies = [ yt-dlp ];

          doCheck = false;

          # yt-dlp needs ffmpeg at runtime for merging/conversion
          makeWrapperArgs = [
            "--prefix" "PATH" ":" "${lib.makeBinPath [ ffmpeg ]}"
          ];

          meta = {
            description = "A thin wrapper around yt-dlp that catalogues downloads in SQLite";
            mainProgram = "yt-archive";
          };
        };

      mkPackage = pkgs: pkgs.callPackage packageFn { };
    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPackage pkgs;
        yt-archive = mkPackage pkgs;
      });

      overlays.default = final: prev: {
        yt-archive = mkPackage final;
      };

      nixosModules.default = import ./nix/module.nix { inherit self; };
      nixosModules.yt-archive = self.nixosModules.default;

      # Exposed so downstream flakes can build the package against their own
      # nixpkgs / overrides without going through `packages.${system}`.
      lib = {
        inherit packageFn mkPackage;
      };

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.yt-dlp ]))
            pkgs.ffmpeg
          ];
        };
      });
    };
}
