# yt-archive

A thin wrapper around [yt-dlp](https://github.com/yt-dlp/yt-dlp) that
catalogues downloads in a SQLite database, with a per-collection sync
schedule and a "scheduled" mode that only syncs collections whose
interval is due.

## Quick start

```sh
yt-archive add my-channel https://www.youtube.com/@SomeChannel
yt-archive set-interval my-channel 7        # weekly
yt-archive sync --scheduled                 # run by cron/systemd
```

`yt-archive --help` lists the rest (`list`, `remove`, `import-archive`,
`fetch-metadata`, `status`).

Configuration lives in a JSON file (default
`~/.config/yt-archive/config.json`, override with `YT_ARCHIVE_CONFIG`)
and the database in `~/.local/share/yt-archive/db.sqlite` (override
with `YT_ARCHIVE_DB`). See `config.example.json` for the option set;
keys are passed straight through to `yt_dlp.YoutubeDL`.

## NixOS module

The flake exports `nixosModules.default`, which adds
`services.yt-archive` and runs `yt-archive sync --scheduled` on a
systemd timer. Minimal usage:

```nix
{
  inputs.yt-archive.url = "github:punnie/yt-dlp-catalogue";

  outputs = { self, nixpkgs, yt-archive, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        yt-archive.nixosModules.default
        ({ ... }: {
          services.yt-archive = {
            enable = true;
            settings = {
              paths.home = "/mnt/archive/youtube";
              merge_output_format = "mkv";
              writesubtitles = true;
              writethumbnail = true;
              subtitleslangs = [ "all" "-live_chat" ];
              sleep_interval = 2;
              max_sleep_interval = 6;
            };
            extraReadWritePaths = [ "/mnt/archive/youtube" ];
          };
        })
      ];
    };
  };
}
```

Notable options:

| Option | Default | Notes |
| --- | --- | --- |
| `enable` | `false` | Enable the timer + sync service. |
| `package` | flake's `packages.${system}.default` | Override to pin a different build. |
| `user` / `group` | `yt-archive` | A system user/group with this name is created automatically. Set both to an existing account (e.g. a media user that owns your archive mount) to skip that. |
| `dataDir` | `/var/lib/yt-archive` | Created via `tmpfiles`, owned by `user:group`. |
| `database` | `${dataDir}/db.sqlite` | Passed via `YT_ARCHIVE_DB`. |
| `settings` | `{}` | Nix attrs rendered to JSON and passed via `YT_ARCHIVE_CONFIG`. **Stored in the Nix store, so do not put secrets here** — point `cookiefile` at a path deployed via sops-nix/agenix instead. |
| `onCalendar` | `*-*-* 03:00:00` | systemd `OnCalendar` expression. The app honours each collection's own `sync_interval_days`, so this only needs to fire often enough to catch the shortest interval in use. |
| `randomizedDelaySec` | `"1h"` | Spreads load when many machines share a schedule. |
| `persistent` | `true` | Catch up after downtime. |
| `extraSyncArgs` | `[]` | Appended to `yt-archive sync --scheduled`. |
| `extraReadWritePaths` | `[]` | The unit is sandboxed with `ProtectSystem=strict` + `ProtectHome=true`. List any download/temp directories outside `dataDir` (e.g. `/mnt/archive/youtube`) so the service can write to them. |

A wrapper around the `yt-archive` CLI is added to
`environment.systemPackages`, so you can manage collections
(`yt-archive add`, `set-interval`, `list`, …) directly on the host.
The wrapper bakes in `YT_ARCHIVE_DB` and `YT_ARCHIVE_CONFIG`, so
invocations automatically hit the same database and config as the
timer. Run as the service user so writes use the right uid:

```sh
sudo -u yt-archive yt-archive add my-channel https://www.youtube.com/@SomeChannel
sudo -u yt-archive yt-archive set-interval my-channel 7
```

The wrapper only sets those env vars when they are unset, so you can
still override either by exporting them explicitly.

You can trigger an off-schedule run with `systemctl start
yt-archive-sync.service` and check the timer with `systemctl
list-timers yt-archive-sync.timer`.

If you'd rather pull in just the package (no service), the flake also
exports `overlays.default`, which adds `pkgs.yt-archive`.

### Using a newer yt-dlp

yt-dlp tends to break whenever YouTube ships changes, and fixes often
land in the upstream `master` branch days before they reach this
flake's pinned `nixpkgs`. The package is structured so you can swap
the `yt-dlp` it depends on without forking the flake. There are two
good patterns; pick whichever fits your setup.

**Pattern A — pin yt-dlp via a `nixpkgs` follower or `master` input.**
Add a second nixpkgs input that tracks `master` (or any branch with
the fix you need) and graft its `yt-dlp` source onto the package's
own `yt-dlp` derivation. Reusing the flake's own `python3` avoids the
`Python version mismatch` error you'd otherwise get when mixing
derivations from two different nixpkgs.

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixpkgs-master.url = "github:NixOS/nixpkgs/master";
    yt-archive.url = "github:punnie/yt-dlp-catalogue";
  };

  outputs = { self, nixpkgs, nixpkgs-master, yt-archive, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        yt-archive.nixosModules.default
        ({ pkgs, ... }: {
          services.yt-archive = {
            enable = true;
            package = yt-archive.packages.${pkgs.system}.default.override {
              yt-dlp =
                let
                  flakePkgs = yt-archive.inputs.nixpkgs.legacyPackages.${pkgs.system};
                  master    = nixpkgs-master.legacyPackages.${pkgs.system};
                in
                flakePkgs.python3.pkgs.yt-dlp.overridePythonAttrs (_: {
                  inherit (master.python3.pkgs.yt-dlp) version src;
                });
            };
            settings = { /* ... */ };
          };
        })
      ];
    };
  };
}
```

This grafts master's `version` + `src` onto the build recipe from the
flake's nixpkgs. If master added or removed a Python dependency,
this won't work and you want Pattern B.

**Pattern B — build the whole package against your own nixpkgs.**
The flake exposes `lib.mkPackage`, which takes a `pkgs` set and
returns the package built against it. Point it at any nixpkgs you
like — `nixpkgs-master`, your system's `pkgs`, or a custom overlay
that ships a newer `python3Packages.yt-dlp`:

```nix
services.yt-archive = {
  enable = true;
  package = yt-archive.lib.mkPackage nixpkgs-master.legacyPackages.${pkgs.system};
};
```

or, equivalently, with an overlay that bumps `yt-dlp`:

```nix
services.yt-archive = {
  enable = true;
  package = yt-archive.lib.mkPackage (import nixpkgs {
    inherit (pkgs) system;
    overlays = [
      (final: prev: {
        python3 = prev.python3.override {
          packageOverrides = pyFinal: pyPrev: {
            yt-dlp = pyPrev.yt-dlp.overridePythonAttrs (_: {
              version = "2025.99.99";
              src = prev.fetchFromGitHub {
                owner = "yt-dlp"; repo = "yt-dlp";
                rev = "<commit>"; hash = "sha256-...";
              };
            });
          };
        };
      })
    ];
  });
};
```

Pattern B is heavier (builds Python deps against a different nixpkgs)
but has zero version-mismatch risk and works even when yt-dlp's
dependency closure changes.

If you only need the CLI on a non-NixOS host, `nix run
github:punnie/yt-dlp-catalogue --override-input nixpkgs
github:NixOS/nixpkgs/master -- sync ...` is the quick-and-dirty
equivalent — overrides the whole nixpkgs input for that one
invocation.
