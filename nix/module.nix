{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.yt-archive;

  configFile = pkgs.writeText "yt-archive-config.json"
    (builtins.toJSON cfg.settings);
in
{
  options.services.yt-archive = {
    enable = lib.mkEnableOption "yt-archive scheduled sync";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression
        "yt-archive.packages.\${system}.default";
      description = "The yt-archive package to use.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "yt-archive";
      description = ''
        User the scheduled sync runs as. If left at the default,
        a system user with this name is created automatically.
      '';
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "yt-archive";
      description = ''
        Group the scheduled sync runs under. If left at the default,
        a group with this name is created automatically.
      '';
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/yt-archive";
      description = ''
        Directory where yt-archive keeps its state. The SQLite catalogue
        lives at <literal>''${dataDir}/db.sqlite</literal> by default.
      '';
    };

    database = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.dataDir}/db.sqlite";
      defaultText = lib.literalExpression ''"''${dataDir}/db.sqlite"'';
      description = "Path to the SQLite catalogue.";
    };

    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.anything;
      default = { };
      example = lib.literalExpression ''
        {
          paths = {
            home = "/mnt/archive/youtube";
            temp = "/var/lib/yt-archive/tmp";
          };
          outtmpl = "%(uploader)s (%(uploader_id)s)/%(upload_date)s - %(title)s [%(id)s].%(ext)s";
          merge_output_format = "mkv";
          writesubtitles = true;
          writethumbnail = true;
          subtitleslangs = [ "all" "-live_chat" ];
          sleep_interval = 2;
          max_sleep_interval = 6;
        }
      '';
      description = ''
        yt-dlp options passed straight through to <literal>YoutubeDL</literal>.
        Rendered to JSON and stored in the Nix store, so do not put
        secrets here. To use cookies, set <literal>cookiefile</literal>
        to a path on disk that the service user can read (e.g. a file
        deployed via sops-nix or agenix).
      '';
    };

    onCalendar = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 03:00:00";
      example = "hourly";
      description = ''
        Systemd <literal>OnCalendar</literal> expression for the sync
        timer. The scheduled command honours each collection's own
        <literal>sync_interval_days</literal>, so this only needs to
        fire often enough to catch the shortest interval in use.
      '';
    };

    randomizedDelaySec = lib.mkOption {
      type = lib.types.str;
      default = "1h";
      description = ''
        Maximum random delay added before the timer fires. Spreads
        load when many machines share the same schedule.
      '';
    };

    persistent = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        If true, the timer fires immediately when the system comes
        back up after having missed a scheduled trigger.
      '';
    };

    extraSyncArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "--config" "/run/secrets/yt-archive-extra.json" ];
      description = ''
        Extra arguments appended to
        <literal>yt-archive sync --scheduled</literal>.
      '';
    };

    extraReadWritePaths = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      example = [ "/mnt/archive/youtube" ];
      description = ''
        Additional paths the service is allowed to write to. The data
        directory is always writable; if your config writes downloads
        or temp files outside it (e.g. <literal>paths.home</literal>
        on a separate mount), list those paths here.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.users = lib.mkIf (cfg.user == "yt-archive") {
      yt-archive = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.dataDir;
        description = "yt-archive scheduled sync";
      };
    };

    users.groups = lib.mkIf (cfg.group == "yt-archive") {
      yt-archive = { };
    };

    environment.systemPackages = [ cfg.package ];

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.group} - -"
    ];

    systemd.services.yt-archive-sync = {
      description = "yt-archive scheduled sync";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        YT_ARCHIVE_DB = cfg.database;
        YT_ARCHIVE_CONFIG = "${configFile}";
        HOME = cfg.dataDir;
      };

      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = lib.escapeShellArgs (
          [ "${cfg.package}/bin/yt-archive" "sync" "--scheduled" ]
          ++ cfg.extraSyncArgs
        );

        ReadWritePaths = [ cfg.dataDir ] ++ cfg.extraReadWritePaths;

        # Hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectHostname = true;
        ProtectClock = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectProc = "invisible";
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallArchitectures = "native";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
      };
    };

    systemd.timers.yt-archive-sync = {
      description = "yt-archive scheduled sync timer";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.onCalendar;
        Persistent = cfg.persistent;
        RandomizedDelaySec = cfg.randomizedDelaySec;
        Unit = "yt-archive-sync.service";
      };
    };
  };
}
