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
          name = "pythonEnv-fhs";
          targetPkgs = pkgs: with pkgs; [
            neovim
            git
            fish
            pythonEnv
          ];
          multiPkgs = pkgs: multiPackages;
          profile = "export LC_ALL=C.UTF-8";
        };
      in
      {
        # Expose the FHS environment directly as an app/run target
        apps.default = {
          type = "app";
          program = "${fhs}/bin/pythonEnv-fhs";
        };

        # Standard devShell that drops you straight into the FHS environment via `nix develop`
        devShells.default = pkgs.mkShell {
          nativeBuildInputs = [ fhs ];
          shellHook = "exec pythonEnv-fhs";
        };
      }
    );
}
