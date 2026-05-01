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

      mkPackage = pkgs:
        let
          python = pkgs.python3;
        in
        python.pkgs.buildPythonApplication {
          pname = "yt-archive";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [ python.pkgs.setuptools ];

          dependencies = [ python.pkgs.yt-dlp ];

          doCheck = false;

          # yt-dlp needs ffmpeg at runtime for merging/conversion
          makeWrapperArgs = [
            "--prefix" "PATH" ":" "${pkgs.lib.makeBinPath [ pkgs.ffmpeg ]}"
          ];

          meta = {
            description = "A thin wrapper around yt-dlp that catalogues downloads in SQLite";
            mainProgram = "yt-archive";
          };
        };
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
