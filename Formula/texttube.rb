# Package a self-contained Homebrew install of the Homebrew-scheduled TextTube service.

class Texttube < Formula
  # Keep the generated launcher on a predictable Homebrew-safe PATH.
  SERVICE_PATH_ENV = "#{HOMEBREW_PREFIX}/bin:/usr/bin:/bin".freeze
  SERVICE_SCHEDULE_HOUR = "__TEXTTUBE_SERVICE_HOUR__".to_i
  SERVICE_SCHEDULE_MINUTE = "__TEXTTUBE_SERVICE_MINUTE__".to_i
  # Copy only the runtime files the Homebrew package needs to execute TextTube.
  APP_FILES = %w[
    SUMMARIZER.md
    requirements.txt
    texttube_app.py
  ].freeze
  SECRETS_FILE = ".secrets".freeze

  desc "Homebrew formula for scheduled TextTube runs with a local mlx-whisper helper"
  homepage "https://github.com/ivribalko/texttube"
  url "file://__TEXTTUBE_SOURCE_ARCHIVE__"
  version "1.0.0"
  sha256 "__TEXTTUBE_SOURCE_SHA256__"

  depends_on "ffmpeg"
  depends_on "ollama"
  depends_on "python@3.14"

  def install
    # Stage the app sources into libexec so Homebrew owns a self-contained copy.
    APP_FILES.each do |relative_path|
      libexec.install buildpath/relative_path
    end

    # Prepare the mutable state and log directories under Homebrew's var tree.
    state_root = var/"texttube"
    venv_root = state_root/"venv"
    (state_root/"var/logs").mkpath
    odie "Missing packaged .secrets; re-run ./texttube install" unless (buildpath/SECRETS_FILE).exist?
    cp buildpath/SECRETS_FILE, state_root/SECRETS_FILE

    # Build the private virtualenv and install the app dependencies into it.
    python = formula_opt_bin("python@3.14")/"python3"
    rm_rf venv_root
    system python, "-m", "venv", venv_root
    system venv_root/"bin/pip", "install", "--upgrade", "pip"
    system venv_root/"bin/pip", "install", "--requirement", libexec/"requirements.txt"

    # Write the Homebrew launcher that invokes the packaged app directly.
    (bin/"texttube").write <<~EOS
      #!/bin/bash
      # Fail on unset variables, nonzero commands, and broken pipelines.
      set -euo pipefail
      # Export the packaged runtime environment expected by the app.
      export PATH="#{SERVICE_PATH_ENV}"
      export PYTHONPATH="#{opt_libexec}"
      export TEXTTUBE_HOME="#{state_root}"
      exec "#{state_root}/venv/bin/python" "#{opt_libexec}/texttube_app.py" "$@"
    EOS
    chmod 0755, bin/"texttube"

  end

  service do
    # Recreate the mutable state root inside the service definition for plist generation.
    state_root = var/"texttube"
    # Launch one packaged scheduled pass per Homebrew calendar trigger.
    run [opt_bin/"texttube"]
    run_type :cron
    cron "#{SERVICE_SCHEDULE_MINUTE} #{SERVICE_SCHEDULE_HOUR} * * *"
    run_at_load false
    # Expose the same runtime environment to the service manager that the wrapper expects.
    environment_variables(
      PATH:          SERVICE_PATH_ENV,
      PYTHONPATH:    opt_libexec.to_s,
      TEXTTUBE_HOME: state_root.to_s,
    )
    # Run the service from its state directory and keep a single combined log file.
    working_dir state_root
    keep_alive false
    log_path state_root/"var/logs/texttube.log"
    error_log_path state_root/"var/logs/texttube.log"
  end

  def caveats
    # Show the operator where to configure secrets and find runtime artifacts.
    <<~EOS
      Installed service secrets from checkout .secrets to:
        #{var}/texttube/.secrets

      Runtime state and logs live in:
        #{var}/texttube/var
        #{var}/texttube/var/state/last_subscription_window_end_utc.txt
        #{var}/texttube/var/logs/texttube.log

      Service schedule:
        Daily at #{format("%02d:%02d", SERVICE_SCHEDULE_HOUR, SERVICE_SCHEDULE_MINUTE)} local macOS time
    EOS
  end

  test do
    # Verify the install produced the expected launcher and packaged app file.
    assert_path_exists bin/"texttube"
    assert_path_exists libexec/"texttube_app.py"
  end
end
