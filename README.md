<p align="center">
  <img src="assets/icon.svg" width="128" height="128" alt="oci-a1-claimer">
</p>

<h1 align="center">oci-a1-claimer</h1>

<p align="center">English | <a href="README.zh-TW.md">繁體中文</a></p>

<p align="center"><a href="https://github.com/xjin9612/oci-a1-claimer/actions/workflows/ci.yml"><img src="https://github.com/xjin9612/oci-a1-claimer/actions/workflows/ci.yml/badge.svg" alt="CI"></a></p>

Keeps requesting an OCI Always Free `VM.Standard.A1.Flex` (ARM) instance through the official OCI API until it succeeds.

- Tries every availability domain (AD) in turn; on "out of capacity" it moves to the next one and sleeps with random jitter after each round.
- Long backoff on HTTP 429; stops immediately when the free allowance is used up; network errors are retried with backoff instead of crashing.
- On success it waits for RUNNING, fetches the public IP, writes a result file, sends an optional notification, and exits.
- Depends only on the official [`oci` Python SDK](https://pypi.org/project/oci/) plus the standard library; Python 3.10+; the whole program is `claimer.py`, with `setup.sh` for one-step installation.
- Log, script, and code-comment messages are in Traditional Chinese.

## Before you start

- **The official free allowance is the source of truth**: see [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) and Console → Governance → Limits, Quotas and Usage. Shape size (`OCPUS`, `MEMORY_GB`) and allowance limits (`FREE_OCPU_LIMIT`, `FREE_MEMORY_GB_LIMIT`) are required in `config.env` and have no built-in defaults, so a policy change only needs a config edit.
- **Home region**: Always Free compute instances can only be created in the home region, which cannot be changed after sign-up ([Managing Regions](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/managingregions.htm)). The tool checks at startup that the target region is the home region and refuses to run otherwise (with `REQUIRE_HOME_REGION=false` it only warns).
- **Account type**:
  - On a free account, A1 instances exceeding the free allowance are disabled and then deleted.
  - On a PAYG (Pay As You Go) account, usage above the free allowance **is billed**. Make sure `OCPUS` / `MEMORY_GB` stay within your account's free allowance.
- **One account per person**: Oracle allows a single free account per person; creating multiple accounts may get them suspended or terminated ([Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)).

## Quick start

**No Oracle account or API key yet?** Follow [docs/oracle-setup.md](docs/oracle-setup.md) (Traditional Chinese) to sign up and prepare the network, IAM, and API key.

Works on Linux, macOS, and WSL. On Windows, run it inside WSL and keep the project on the WSL file system (for example `~/projects/oci-a1-claimer`): on Windows drives such as `/mnt/c` or `/mnt/d`, `chmod` has no effect and key permissions cannot be restricted.

1. Put three files in `secrets/`:

   | File | Content |
   |---|---|
   | `secrets/oci_config` | OCI config file: the snippet shown when you add an API key in the Console |
   | `secrets/oci_api_key.pem` | API private key |
   | `secrets/ssh.pub` | SSH public key to inject into the instance |

2. Run `./setup.sh`. The first run creates `config.env` from the template and asks you to fill it in.
3. Edit `config.env`, set `SUBNET_ID` and the shape size, then run `./setup.sh` again.
4. Start claiming:

```bash
./setup.sh                    # create .venv, tighten permissions, set key_file, run dry-run
./setup.sh --telegram         # optional: set up Telegram notifications
.venv/bin/python claimer.py   # run in the foreground; Ctrl+C stops safely
sudo ./setup.sh --systemd     # or install as a long-running systemd service
```

For foreground runs, use `tmux` so closing the terminal does not stop it:

```bash
tmux new -s a1                # then run .venv/bin/python claimer.py inside the session
                              # Ctrl+B then D to detach; tmux attach -t a1 to come back
```

Repeated `容量不足` (out of capacity) and `本輪無果` (no luck this round) log lines are normal: the tool is waiting for capacity to free up. WSL stops when Windows sleeps, so disable sleep for long runs.

In local mode, `setup.sh`:
- Checks the Python version, creates `.venv`, and installs dependencies.
- Sets `secrets/` to 700 and its files plus `config.env` to 600.
- Points `key_file` in `secrets/oci_config` to `secrets/oci_api_key.pem`.
- Verifies the files are present and `config.env` is filled in, then runs `--dry-run`.

`--dry-run` checks config and authentication, home region, availability domains, subnet, image, and current usage, then exits without notifying; it waits at most 2 minutes when the network is unreachable.

### Telegram notifications

```bash
./setup.sh --telegram
```

Follow the prompts:
1. In Telegram, search for **@BotFather** (exact handle, verified badge) and send `/newbot` to create a bot.
2. Paste the token. Input is hidden and does not go into shell history.
3. Press Start in the new bot's chat, send it any message, then press Enter in the terminal.
4. Pick your own account from the listed chats. Anyone can message a bot, so the script never picks one automatically.

The script verifies the token, finds the chat id, sends a test message, and writes `TG_BOT_TOKEN` and `TG_CHAT_ID` to `config.env`. Restart the claimer for it to take effect. Notifications are sent when claiming starts, every `HEARTBEAT_HOURS` hours with progress (default 24; `0` disables), when an instance is created (with its public IP), when the tool stops because the allowance is used up, when the instance was created but a follow-up step failed, when it stops for a manual check (unknown launch outcome or damaged state file), and when a config error appears after claiming has started (for example, an expired API key). If the periodic report stops arriving, the tool is no longer running (for example, the host went to sleep).

If sending fails, the log shows Telegram's error description, such as `chat not found` (wrong chat id) or `bot was blocked by the user`. If the token leaks, send `/revoke` to @BotFather to issue a new one.

### Configuration

- Every setting is documented in `config.example.env`.
- Choose the config file with `-c` or the `CLAIMER_CONFIG` environment variable.
- Environment variables with the same name override the file; an empty value means "use the default".
- Relative paths in `SSH_PUBLIC_KEY_FILE`, `STATE_DIR`, and `OCI_CONFIG_FILE` are resolved against the config file's directory.
- To change region, set `REGION` (for example `ap-tokyo-1`) or edit `region` in `secrets/oci_config`.

## Running as a systemd service

```bash
sudo ./setup.sh --systemd                  # install and start
sudo ./setup.sh --systemd --update-config  # overwrite the installed config with the project's config.env and secrets/
sudo ./setup.sh --uninstall                # remove the service and program, keep config and results
sudo ./setup.sh --uninstall --purge        # remove everything, including the service user

journalctl -u oci-a1-claimer -f            # follow the log
sudo cat /var/lib/oci-a1-claimer/result.json
```

Installed layout:

| Location | Content | Owner / mode |
|---|---|---|
| `/opt/oci-a1-claimer/` | Program and `.venv` | `root`, 755 / 644 |
| `/etc/oci-a1-claimer/` | `config.env` and `secrets/` | `root:oci-claimer`, 750 / 640 |
| `/var/lib/oci-a1-claimer/` | State and result files | systemd `StateDirectory`, 700 |

- Runs as the dedicated non-root user `oci-claimer` with `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, and other hardening; logs go to journald.
- Config and keys are owned by root and readable by the service group, so the service cannot modify its own configuration.
- `RestartPreventExitStatus=2 3 4`: only unexpected errors trigger an automatic restart.
- After the program and config are copied to `/opt` and `/etc`, a dry-run is executed as the service user; if it fails, the unit is not installed or started.
- Re-running does not overwrite existing files in `/etc`; differences are reported, and `--update-config` overwrites them.
- `config.env` must use the default `secrets/` paths. Keep secrets such as the Telegram token in `config.env`, not in the unit's `Environment=`.
- On WSL, first set `systemd=true` under `[boot]` in `/etc/wsl.conf` and restart WSL.

## Exit codes and result files

| Code | Meaning | Restarted by systemd |
|---|---|---|
| 0 | Success, dry-run passed, or already completed earlier | No |
| 1 | Unexpected error | Yes |
| 2 | Config / permission error (including unreadable config, private key, or SSH key; non-home region) | No |
| 3 | Free allowance used up, or `LimitExceeded` / `QuotaExceeded` | No |
| 4 | Instance created but a follow-up step failed, or state is uncertain and needs a manual check | No |
| 130 | Received SIGINT / SIGTERM | No |

- The result is written to `result.json` in `STATE_DIR` (instance OCID, AD, public / private IP).
- `state.json` is written the moment an instance is created; while it exists no new launch is attempted, so delete it to claim another instance.
- On exit code 4, the log and the Telegram message explain the cause and what to do.

## Least-privilege IAM

Run the tool with a dedicated IAM user, not the administrator's API key. Steps for creating the user, group, and policy, and what each policy statement is for, are in [docs/oracle-setup.md](docs/oracle-setup.md#6-建立-policy) (Traditional Chinese).

## Known limitations

- The usage check only looks at `COMPARTMENT_ID` and `USAGE_EXTRA_COMPARTMENT_IDS`; the free allowance applies to the whole tenancy.
- Instances in `STOPPED` state also count toward usage (conservative).
- Only one process may claim from a `STATE_DIR` at a time. A local run and the systemd service use different `STATE_DIR`s and cannot detect each other, so do not run both.
- Do not delete `pending_launch.json` yourself, or an instance may be created twice; delete it only when the tool asks for a manual check and you have confirmed in the Console that no extra instance exists.
- In resume mode, if the instance has been terminated, the tool keeps exiting with code 4; delete `state.json` manually after checking.
- Whether assigning a public IP needs an extra `public-ips` permission is not yet verified (it can only be confirmed after a successful launch).

## Development

How it works, error classification, idempotency, tests, and dependencies are described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (Traditional Chinese).

## License

MIT
