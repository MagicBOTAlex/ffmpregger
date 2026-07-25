{
  description = "Ffmpregger";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
          overlays = [
            (final: prev: {
              python312 = prev.python312.override {
                packageOverrides = self: super: {
                  jaraco-test = super.jaraco-test.overridePythonAttrs (oldAttrs: {
                    doCheck = false;
                  });
                };
              };
            })
          ];
        };

        pythonEnv = pkgs.python312.withPackages (ps: [
          ps.pillow
          ps.tqdm
        ]);

        # Build 32-bit packages only on x86_64-linux, as 32-bit x86 packages (pkgsi686Linux) do not exist on ARM
        multiPackages =
          with pkgs;
          [
            zlib
            ncurses5
            openssl
            libusb1
            glib
            glibc
            glibc.dev
            libjpeg
            stdenv.cc.cc.lib
          ]
          ++ pkgs.lib.optionals (system == "x86_64-linux") [
            pkgsi686Linux.glibc
            pkgsi686Linux.ncurses5
            pkgsi686Linux.stdenv.cc.cc.lib
          ];

        fhs = pkgs.buildFHSEnv {
          name = "pio-shell";
          targetPkgs =
            pkgs: with pkgs; [
              neovim
              git
              fish
              pythonEnv
            ];
          multiPkgs = pkgs: multiPackages;

          runScript = ''
          '';

          profile = "export LC_ALL=C.UTF-8";
        };
      in
      {
        devShells.default = fhs.env;
      }
    );
}

