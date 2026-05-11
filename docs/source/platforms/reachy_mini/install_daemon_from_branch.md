# Install the Daemon from a Specific Branch

> [!WARNING]
> _⚠️ For Developers and Testers Only_
>
> This guide explains how to install the Reachy Mini daemon from a specific GitHub branch before it is officially released. Use this for testing new features or bug fixes.


## Prerequisites

- SSH access to your Reachy Mini robot (`pollen@reachy-mini.local`, password: `root`)
- The robot must be connected to your Wi-Fi network (or accessible through its hotspot)

## Option A: Local Development Setup

> [!NOTE]
> This option is intended for active development and fast debugging cycles. It allows you to safely test changes without affecting the system-wide installation.
>
> ⚠️ Avoid installing apps with this option as any changes made to the local `reachy_mini` version won’t be propagated correctly.

### Steps:

1. **Connect to the robot via SSH:**
   ```bash
   ssh pollen@reachy-mini.local
   # Password: root
   ```

2. **Clone the Reachy Mini repository with the specific branch:**
   ```bash
   git clone -b <branch-name> https://github.com/pollen-robotics/reachy_mini.git
   cd reachy_mini
   ```

3. **Set up the virtual environment:**
   ```bash
   uv venv --python /venvs/mini_daemon/bin/python .venv
   source .venv/bin/activate
   uv sync --extra gstreamer --extra wireless-version
   ```

4. **Stop the system daemon service:**
   ```bash
   sudo systemctl stop reachy-mini-daemon
   ```
   
   > [!TIP]
   > This step must be repeated after each reboot since the system service restarts automatically.

5. **Start the local daemon for testing:**
   ```bash
   reachy-mini-daemon --wireless-version
   ```

Now you can modify the code in `~/reachy_mini` and test your changes without affecting the system installation.

## Option A2: Editable Install into the System Venv

> [!NOTE]
> Same fast-iteration workflow as Option A — edit the clone, restart the daemon, see changes immediately — but without duplicating the ~1 GB of dependencies (numpy, scipy, placo, gstreamer bindings, …) that already live in `/venvs/mini_daemon`. The trade-off is that this mutates the system daemon venv, so testing changes the production install until you roll back.
>
> Prefer this over Option A when storage or SD-card wear matters; prefer Option A when you want a fully isolated environment.

### Steps:

1. **Connect to the robot via SSH:**
   ```bash
   ssh pollen@reachy-mini.local
   # Password: root
   ```

2. **Stop the system daemon service:**
   ```bash
   sudo systemctl stop reachy-mini-daemon
   ```

3. **Clone the Reachy Mini repository with the specific branch:**
   ```bash
   git clone -b <branch-name> https://github.com/pollen-robotics/reachy_mini.git
   cd reachy_mini
   ```

4. **Editable-install the clone into the system venv:**
   ```bash
   source /venvs/mini_daemon/bin/activate
   uv pip install -e ".[gstreamer,wireless-version]"
   ```
   `reachy_mini` in `/venvs/mini_daemon` now points at your clone — edits are picked up without a reinstall.

5. **Run the daemon:**
   ```bash
   # Foreground, for fast iteration:
   reachy-mini-daemon --wireless-version
   # …or through systemd, for end-to-end testing:
   sudo systemctl start reachy-mini-daemon
   ```

6. **Roll back when you're done:** see [Rolling Back to Factory Version](#rolling-back-to-factory-version). The editable install is removed by reinstalling the released wheel into the same venv, or by triggering SOFTWARE_RESET.

> [!WARNING]
> The `gpio-shutdown-daemon` service also runs out of this venv. If your branch changes anything under `src/reachy_mini/daemon/app/services/gpio_shutdown/`, also run `sudo systemctl restart gpio-shutdown-daemon` after the editable install.

## Option B: System-Wide Custom Installation

> [!NOTE]
> This option installs a branch build of reachy-mini as the system-wide daemon. It's better suited for thorough, end-to-end testing and supports seamless app installation from Reachy Mini Control.

### Steps:

1. **Connect to the robot via SSH:**
   ```bash
   ssh pollen@reachy-mini.local
   # Password: root
   ```

2. **Activate the daemon's virtual environment:**
   ```bash
   source /venvs/mini_daemon/bin/activate
   ```

3. **Install the specific branch:**
   ```bash
   pip install --no-cache-dir --force-reinstall \
     "reachy_mini[gstreamer,wireless-version] @ git+https://github.com/pollen-robotics/reachy_mini.git@<branch-name>"
   ```
   Replace `<branch-name>` with the branch you want to test (e.g., `develop`, `feature/my-feature`, `bugfix/issue-123`).

   > [!NOTE]
   > We have to use `pip` here and not `uv` because `uv pip install` [does not work correctly with `git lfs`](https://github.com/astral-sh/uv/issues/3312).

4. **(Only for versions ≤ 1.2.13)** Repeat steps 2 and 3 using `/venvs/apps_venv`.

5. **Restart the daemon service:**
   ```bash
   sudo systemctl restart reachy-mini-daemon
   ```

6. **Verify the installation was successful:**
   ```bash
   pip show reachy-mini | grep Version
   ```
   This should display the version corresponding to your installed branch.

## Rolling Back to Factory Version

If you encounter issues with the branch installation, you can restore the factory daemon:

1. **Trigger the SOFTWARE_RESET command** via Bluetooth to reinstall the original factory daemon
2. **Refer to the [Reset Guide](reset.md)** for detailed step-by-step instructions

## Important Notes

- **Backup your work** before switching between different branch installations
- **Test thoroughly** in local development mode before doing system-wide installations
- **Monitor system logs** after installation: `journalctl -u reachy-mini-daemon -f`
- **Performance impact:** Some development branches may have reduced performance or stability
