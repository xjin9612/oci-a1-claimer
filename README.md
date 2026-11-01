<p align="center">
  <img src="assets/icon.svg" width="128" height="128" alt="oci-a1-claimer">
</p>

<h1 align="center">oci-a1-claimer</h1>

<p align="center">English | <a href="README.zh-TW.md">繁體中文</a></p>

Keeps requesting an OCI Always Free `VM.Standard.A1.Flex` (ARM) instance through the official OCI API until it succeeds.

- Tries every availability domain (AD) in turn; on "out of capacity" it moves to the next one and sleeps with random jitter after each round.
- Long backoff on HTTP 429; stops immediately when the free allowance is used up; network errors are retried with backoff instead of crashing.
- On success it waits for RUNNING, fetches the public IP, writes a result file, sends an optional notification, and exits.
- Depends only on the official [`oci` Python SDK](https://pypi.org/project/oci/) plus the standard library; Python 3.10+; the whole program is `claimer.py`, with `setup.sh` for one-step installation.
- Log, script, and code-comment messages are in Traditional Chinese.

## Before you start

- **The official free allowance is the source of truth**: see [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) and Console → Governance → Limits, Quotas and Usage. Shape size (`OCPUS`, `MEMORY_GB`) and allowance limits (`FREE_OCPU_LIMIT`, `FREE_MEMORY_GB_LIMIT`) are required in `config.env` and have no built-in defaults, so a policy change only needs a config edit.
- **Home region**: Always Free compute instances can only be created in the home region, which cannot be changed after sign-up ([Managing Regions](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/managingregions.htm)). The tool checks at startup that the target region is the home region and refuses to run otherwise.
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

The script verifies the token, finds the chat id, sends a test message, and writes `TG_BOT_TOKEN` and `TG_CHAT_ID` to `config.env`. Restart the claimer for it to take effect. Notifications are sent when an instance is created (with its public IP), when the tool stops because the allowance is used up, and when the instance was created but a follow-up step failed.

If sending fails, the log shows Telegram's error description, such as `chat not found` (wrong chat id) or `bot was blocked by the user`. If the token leaks, send `/revoke` to @BotFather to issue a new one.

### Configuration

- Every setting is documented in `config.example.env`.
- Choose the config file with `-c` or the `CLAIMER_CONFIG` environment variable.
- Environment variables with the same name override the file; an empty value means "use the default".
- Relative paths in `SSH_PUBLIC_KEY_FILE`, `STATE_DIR`, and `OCI_CONFIG_FILE` are resolved against the config file's directory.
- To change region, set `REGION` (for example `ap-tokyo-1`) or edit `region` in `secrets/oci_config`.

## How it works

```
start
 ├─ state.json exists?
 │    ├─ completed → already done, exit (0)
 │    ├─ launched  → resume mode: finish post-launch steps only, never re-launch
 │    └─ corrupted → needs manual check, exit (4)
 ├─ check SSH key, home region, availability domains, subnet, image
 ├─ pending_launch.json exists? → resend with the same AD + token first, continue only on a definite result
 ├─ usage check: used + requested > limit → exit (3)
 └─ loop: call launch_instance for each AD
        out of capacity → next AD
        429             → long backoff (doubles when repeated, capped), then next AD
        quota exceeded  → exit (3)
        auth / params   → exit (2)
        network error   → outcome unknown: write pending, resend only with the same AD + token until a definite result
        other 5xx       → retry a few times with the same token, then next AD
      end of round → sleep INTERVAL_SECONDS + 0~JITTER_SECONDS
 success → write state.json → wait for RUNNING → get public IP → write result.json → notify → exit (0)
```

### Exit codes

| Code | Meaning | Restarted by systemd |
|---|---|---|
| 0 | Success, dry-run passed, or already completed earlier | No |
| 1 | Unexpected error | Yes |
| 2 | Config / permission error (including unreadable config, private key, or SSH key; non-home region) | No |
| 3 | Free allowance used up, or `LimitExceeded` / `QuotaExceeded` | No |
| 4 | Instance created but a follow-up step failed, or state is uncertain and needs a manual check | No |
| 130 | Received SIGINT / SIGTERM | No |

### State files

All live in `STATE_DIR`, are written atomically, and have mode `0600`.

| File | Content |
|---|---|
| `state.json` | Written the moment an instance is created. While it exists, no new launch is attempted; delete it to claim another instance |
| `result.json` | Final result: instance OCID, AD, public / private IP |
| `pending_launch.json` | AD and retry token when a network error leaves the outcome unknown; removed once a definite result arrives |

### Error classification

Based mainly on HTTP status and OCI error code, with the message as a fallback ([API Errors](https://docs.oracle.com/en-us/iaas/Content/API/References/apierrors.htm)):

| Condition | Class |
|---|---|
| 429 | Rate limited |
| `LimitExceeded` / `QuotaExceeded` | Allowance exceeded |
| 5xx with `capacity` in the message | Out of capacity |
| Other 5xx; 409 `IncorrectState` / `ExternalServerIncorrectState` / `LockConflict` | Transient |
| Other 400, 401, 403, 404, 409 | Config error |
| `oci.exceptions.RequestException` / `ConnectTimeout`, `ConnectionError`, `TimeoutError` | Network error |

"Out of capacity" is not in the official error table; the actual response is `500 / InternalError / "Out of host capacity."`. If the message format ever changes, it is treated as a transient error and retried with backoff rather than stopping the tool.

### Idempotency

- Every `launch_instance` call carries a self-generated `opc_retry_token` and passes `NoneRetryStrategy()`, so the SDK does not auto-retry 429s and defeat the long backoff.
- After a network error the server may or may not have created the instance, so the tool resends only with the same AD and token: if it was created, the server returns that same instance. Meanwhile 429, 5xx, and 409 only trigger backoff, never a new token; if the tool is stopped, the next start finishes this resend first.
- Retry tokens are valid for 24 hours; a pending entry older than 23 hours stops the tool and asks for a manual check.
- The SDK circuit breaker is explicitly disabled on all clients, so consecutive out-of-capacity 500s do not block later calls.

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
- A dry-run is executed as the service user before installation; installation is aborted if it fails.
- Re-running does not overwrite existing files in `/etc`; differences are reported, and `--update-config` overwrites them.
- `config.env` must use the default `secrets/` paths. Keep secrets such as the Telegram token in `config.env`, not in the unit's `Environment=`.
- On WSL, first set `systemd=true` under `[boot]` in `/etc/wsl.conf` and restart WSL.

## Least-privilege IAM

Run the tool with a dedicated IAM user, not the administrator's API key. Console steps for creating the user, group, and API key are in [docs/oracle-setup.md](docs/oracle-setup.md).

The policy below is scoped by compartment. `<instance-compartment>` holds the instance and `<network-compartment>` holds the VCN / subnet; use the same name if they are the same. If everything lives in the root compartment, replace `compartment <name>` with `tenancy`.

```text
Allow group oci-a1-claimer to inspect tenancies in tenancy
Allow group oci-a1-claimer to inspect compartments in tenancy
Allow group oci-a1-claimer to read app-catalog-listing in tenancy
Allow group oci-a1-claimer to manage instances in compartment <instance-compartment>
Allow group oci-a1-claimer to read instance-images in compartment <instance-compartment>
Allow group oci-a1-claimer to inspect vnic-attachments in compartment <instance-compartment>
Allow group oci-a1-claimer to use vnics in compartment <instance-compartment>
Allow group oci-a1-claimer to use subnets in compartment <network-compartment>
Allow group oci-a1-claimer to use network-security-groups in compartment <network-compartment>
```

| Statement | Purpose |
|---|---|
| `inspect tenancies in tenancy` | `ListRegionSubscriptions` (home region check) |
| `inspect compartments in tenancy` | `ListAvailabilityDomains` |
| `read app-catalog-listing in tenancy` | Required by `LaunchInstance` |
| `manage instances` | `LaunchInstance`; covers `ListInstances` / `GetInstance` |
| `read instance-images` | Required by `LaunchInstance`; covers `ListImages` / `GetImage` |
| `inspect vnic-attachments` | `ListVnicAttachments` |
| `use vnics` | Required by `LaunchInstance`; covers `GetVnic` |
| `use subnets` | Required by `LaunchInstance`; covers `GetSubnet` |
| `use network-security-groups` | Required by `LaunchInstance` |

- `manage instance-family`, `use volume-family`, and any VCN create / delete permissions are intentionally not granted. `manage instances` still includes terminating instances; it is the minimum verb for `LaunchInstance`.
- When `USAGE_EXTRA_COMPARTMENT_IDS` is set, those compartments also need `read instances`.
- If you hit 404 `NotAuthorizedOrNotFound`, try Oracle's broader template first to rule out permissions, then narrow it down:

```text
Allow group oci-a1-claimer to manage instance-family in compartment <instance-compartment>
Allow group oci-a1-claimer to read app-catalog-listing in tenancy
Allow group oci-a1-claimer to use virtual-network-family in compartment <network-compartment>
```

References:
- [Details for the Core Services](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)
- [Details for IAM with Identity Domains](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/iampolicyreference.htm)
- [Common Policies](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/commonpolicies.htm)
- [Policy Syntax](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/policysyntax.htm)

## Tests

```bash
python -m unittest discover -s tests -v
```

The OCI clients are mocked, so no real API calls are made. Covered: AD rotation, 429 backoff, quota stop, usage check, home region check, the success path, post-launch failures and resume, idempotency when the outcome is unknown, network recovery, exit codes, config parsing, file permissions, and log redaction.

## Known limitations

- The usage check only looks at `COMPARTMENT_ID` and `USAGE_EXTRA_COMPARTMENT_IDS`; the free allowance applies to the whole tenancy.
- Instances in `STOPPED` state also count toward usage (conservative).
- Deleting `pending_launch.json` or changing the shape while an outcome is unknown defeats the idempotency protection.
- "Resending with the same token after an unknown outcome and getting out-of-capacity" is treated as "the original request created nothing". This follows from retry-token semantics but is not stated verbatim in Oracle's docs.
- If writing `state.json` fails right after a successful launch (for example, disk full), the tool exits with code 4 and notifies you, but the next start will not know that instance exists.
- In resume mode, if the instance has been terminated, the tool keeps exiting with code 4; delete `state.json` manually after checking.
- Whether assigning a public IP needs an extra `public-ips` permission is not yet verified (it can only be confirmed after a successful launch).
- File permissions are not checked on Windows; access is governed by the folder's ACL.

## License

MIT
