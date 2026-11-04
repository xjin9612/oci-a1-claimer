"""claimer.py 的單元測試：以 mock 取代 OCI client，不會呼叫任何真實 API。

執行：python -m unittest discover -s tests -v
"""

import io
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oci  # noqa: E402
from oci.exceptions import ServiceError  # noqa: E402

import claimer  # noqa: E402


def service_error(status, code, message=""):
    return ServiceError(status, code, {}, message)


def capacity_error():
    return service_error(500, "InternalError", "Out of host capacity.")


def resp(data, has_next_page=False, next_page=None):
    return SimpleNamespace(data=data, has_next_page=has_next_page, next_page=next_page)


def make_instance(**kw):
    base = dict(
        id="ocid1.instance.oc1..new", display_name="a1-claimer", availability_domain="AD-1",
        compartment_id="ocid1.compartment.oc1..c", shape=claimer.SHAPE, lifecycle_state="PROVISIONING",
        shape_config=SimpleNamespace(ocpus=4.0, memory_in_gbs=24.0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make_image(name, version, created_day=1):
    return SimpleNamespace(
        id=f"ocid1.image..{name}", display_name=name, operating_system="Canonical Ubuntu",
        operating_system_version=version,
        time_created=datetime(2026, 1, created_day, tzinfo=timezone.utc),
    )


SPEC_PENDING = {"image_id": "ocid1.image..old", "ocpus": 2.0, "memory_gb": 12.0, "boot_volume_gb": 50}

# 規格與額度上限為必填設定；測試統一用這組值
SPEC = dict(ocpus=2.0, memory_gb=12.0, free_ocpu_limit=2.0, free_memory_gb_limit=12.0)
SPEC_ENV = {"OCPUS": "2", "MEMORY_GB": "12", "FREE_OCPU_LIMIT": "2", "FREE_MEMORY_GB_LIMIT": "12"}
SPEC_FILE = "".join(f"{k}={v}\n" for k, v in SPEC_ENV.items())


class FakeSleeper:
    """記錄每次 sleep 秒數，並推進假時鐘（不真的等待）。"""

    def __init__(self):
        self.calls = []
        self.now = 0.0

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def wall(self):
        return 1_700_000_000 + self.now


class ClaimerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmpdir = Path(self.tmp.name)
        key = tmpdir / "id.pub"
        key.write_text("ssh-ed25519 AAAATEST user@test\n")
        self.settings = claimer.Settings(
            subnet_id="ocid1.subnet.oc1..s", ssh_public_key_file=str(key), **SPEC,
            compartment_id="ocid1.compartment.oc1..c", state_dir=str(tmpdir / "state"),
            interval_seconds=60, jitter_seconds=10, per_ad_delay_seconds=1,
            rate_limit_sleep_seconds=300, rate_limit_max_sleep_seconds=1800,
            transient_retry_base_seconds=5, transient_retry_max_seconds=60,
            running_timeout_seconds=100, public_ip_timeout_seconds=100,
        )
        self.compute = mock.MagicMock()
        self.network = mock.MagicMock()
        self.identity = mock.MagicMock()
        self.clients = claimer.OciClients(self.compute, self.network, self.identity,
                                          tenancy_id="ocid1.tenancy.oc1..t", region="ap-tokyo-1")
        self.notifier = mock.MagicMock()
        self.sleeper = FakeSleeper()
        self.tokens = iter(f"token{i}" for i in range(1000))
        self.claimer = self.new_claimer()
        # 預設的唯讀 API 回應
        self.identity.list_region_subscriptions.return_value = resp([
            SimpleNamespace(region_name="ap-tokyo-1", region_key="NRT", is_home_region=True),
            SimpleNamespace(region_name="ap-osaka-1", region_key="KIX", is_home_region=False)])
        self.identity.list_availability_domains.return_value = resp(
            [SimpleNamespace(name="xyz:AP-TOKYO-1-AD-1"), SimpleNamespace(name="xyz:AP-TOKYO-1-AD-2")])
        self.network.get_subnet.return_value = resp(SimpleNamespace(
            display_name="pub", cidr_block="10.0.0.0/24", prohibit_public_ip_on_vnic=False))
        self.compute.list_images.return_value = resp([make_image("Canonical-Ubuntu-24.04-aarch64", "24.04")])
        self.compute.list_instances.return_value = resp([])
        self.compute.get_instance.return_value = resp(make_instance(lifecycle_state="RUNNING"))
        self.compute.list_vnic_attachments.return_value = resp(
            [SimpleNamespace(lifecycle_state="ATTACHED", vnic_id="ocid1.vnic..v")])
        self.network.get_vnic.return_value = resp(SimpleNamespace(
            is_primary=True, public_ip="203.0.113.10", private_ip="10.0.0.5"))

    def new_claimer(self):
        return claimer.Claimer(
            self.settings, self.clients, self.notifier, sleep_fn=self.sleeper,
            token_factory=lambda: next(self.tokens), clock=self.sleeper.clock, wall_clock=self.sleeper.wall,
        )

    @property
    def state_path(self):
        return Path(self.settings.state_dir) / claimer.STATE_FILE

    @property
    def result_path(self):
        return Path(self.settings.state_dir) / claimer.RESULT_FILE


class TestErrorClassification(unittest.TestCase):
    def test_table(self):
        K = claimer.ErrorKind
        cases = [
            (service_error(500, "InternalError", "Out of host capacity."), K.CAPACITY),
            (service_error(500, "InternalServerError", "Out of host capacity"), K.CAPACITY),
            (service_error(500, "InternalServerError", "boom"), K.TRANSIENT),
            (service_error(503, "ServiceUnavailable", ""), K.TRANSIENT),
            (service_error(429, "TooManyRequests", ""), K.RATE_LIMIT),
            (service_error(400, "LimitExceeded", "x"), K.QUOTA),
            (service_error(400, "QuotaExceeded", "x"), K.QUOTA),
            (service_error(400, "InvalidParameter", "service limit reached"), K.QUOTA),
            (service_error(400, "InvalidParameter", "bad shape"), K.CONFIG),
            (service_error(401, "NotAuthenticated", ""), K.CONFIG),
            (service_error(404, "NotAuthorizedOrNotFound", ""), K.CONFIG),
            (service_error(409, "IncorrectState", ""), K.TRANSIENT),
            (service_error(409, "LockConflict", ""), K.TRANSIENT),
            (service_error(409, "Conflict", ""), K.CONFIG),
        ]
        for exc, expected in cases:
            with self.subTest(status=exc.status, code=exc.code):
                self.assertIs(claimer.classify_service_error(exc), expected)


class TestLaunchLoop(ClaimerTestBase):
    def test_capacity_rotates_ads_then_sleeps_round(self):
        self.compute.launch_instance.side_effect = [
            capacity_error(), capacity_error(),          # 第 1 輪兩個 AD 都沒貨
            capacity_error(), resp(make_instance()),      # 第 2 輪 AD-2 成功
        ]
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        ads = [c.args[0].availability_domain for c in self.compute.launch_instance.call_args_list]
        self.assertEqual(ads, ["xyz:AP-TOKYO-1-AD-1", "xyz:AP-TOKYO-1-AD-2"] * 2)
        # 每次 launch 都帶 token，而且容量不足後會換新 token
        tokens = [c.kwargs["opc_retry_token"] for c in self.compute.launch_instance.call_args_list]
        self.assertEqual(len(set(tokens)), 4)
        # launch 一律明確關閉 SDK 自動重試
        for c in self.compute.launch_instance.call_args_list:
            self.assertIsInstance(c.kwargs["retry_strategy"], oci.retry.NoneRetryStrategy)
        # 整輪間隔在 [interval, interval + jitter] 之間
        round_sleeps = [s for s in self.sleeper.calls if s >= self.settings.interval_seconds]
        self.assertEqual(len(round_sleeps), 1)
        self.assertLessEqual(round_sleeps[0], self.settings.interval_seconds + self.settings.jitter_seconds)

    def test_rate_limit_long_backoff_and_escalation(self):
        self.compute.launch_instance.side_effect = [
            service_error(429, "TooManyRequests"),
            service_error(429, "TooManyRequests"),
            resp(make_instance()),
        ]
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        backoffs = [s for s in self.sleeper.calls if s >= self.settings.rate_limit_sleep_seconds]
        self.assertEqual(len(backoffs), 2)
        self.assertGreaterEqual(backoffs[0], 300)
        self.assertLess(backoffs[0], 300 + self.settings.jitter_seconds + 1)
        self.assertGreaterEqual(backoffs[1], 600)

    def test_quota_exceeded_stops_immediately(self):
        self.compute.launch_instance.side_effect = [service_error(400, "LimitExceeded", "limit")]
        with self.assertRaises(claimer.QuotaExhausted):
            self.claimer.run()
        self.assertEqual(self.compute.launch_instance.call_count, 1)
        self.assertFalse(self.state_path.exists())

    def test_existing_usage_blocks_before_launch(self):
        self.compute.list_instances.return_value = resp([make_instance(id="old", lifecycle_state="RUNNING")])
        with self.assertRaisesRegex(claimer.QuotaExhausted, "已用滿"):
            self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_terminated_and_other_shapes_not_counted(self):
        self.compute.list_instances.return_value = resp([
            make_instance(id="gone", lifecycle_state="TERMINATED"),
            make_instance(id="going", lifecycle_state="TERMINATING"),
            make_instance(id="x86", shape="VM.Standard.E2.1.Micro", lifecycle_state="RUNNING"),
        ])
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_usage_counts_all_pages_and_extra_compartments(self):
        """分頁與額外 compartment 都要加總，算少了會申請到付費實例。"""
        half = SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0)
        self.settings.usage_extra_compartment_ids = ["ocid1.compartment.oc1..c", "ocid1.compartment.oc1..extra"]
        pages = {
            ("ocid1.compartment.oc1..c", None): resp([make_instance(id="p1", shape_config=half)], True, "pg2"),
            ("ocid1.compartment.oc1..c", "pg2"): resp([make_instance(id="p2", shape_config=half)]),
            ("ocid1.compartment.oc1..extra", None): resp([make_instance(id="e1", shape_config=half)]),
        }
        self.compute.list_instances.side_effect = lambda compartment_id, page=None, **_: pages[(compartment_id, page)]
        used_cpu, used_mem, found = self.claimer.current_a1_usage()
        self.assertEqual((used_cpu, used_mem), (3.0, 18.0))
        self.assertEqual([i.id for i in found], ["p1", "p2", "e1"])  # 主 compartment 不重複計算

    def test_partial_usage_rejects_oversized_request(self):
        """STOPPED 也計入；已用 1/6 + 申請 2/12 超過上限 2/12。"""
        self.compute.list_instances.return_value = resp([
            make_instance(id="old", lifecycle_state="STOPPED",
                          shape_config=SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0)),
        ])
        with self.assertRaisesRegex(claimer.QuotaExhausted, "剩餘額度不足"):
            self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_usage_exactly_at_limit_is_allowed(self):
        self.settings.free_ocpu_limit, self.settings.free_memory_gb_limit = 3.0, 18.0
        self.compute.list_instances.return_value = resp([
            make_instance(id="old", shape_config=SimpleNamespace(ocpus=1.0, memory_in_gbs=6.0))])
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_success_path_writes_files_and_notifies(self):
        self.compute.launch_instance.return_value = resp(make_instance())
        self.compute.get_instance.side_effect = [
            resp(make_instance(lifecycle_state="PROVISIONING")),
            resp(make_instance(lifecycle_state="RUNNING")),
        ]
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["public_ip"], "203.0.113.10")
        self.assertEqual(result["instance_id"], "ocid1.instance.oc1..new")
        self.assertEqual((result["ocpus"], result["memory_gb"]), (4.0, 24.0))  # 取自實例，不是設定
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.result_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        # 開始通知 + 成功通知
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertIn("開始申請", self.notifier.send.call_args_list[0].args[0])
        self.assertIn("203.0.113.10", self.notifier.send.call_args.args[0])

    def test_post_launch_failure_does_not_relaunch(self):
        """建立後步驟失敗 → PostLaunchError；state 已落盤，再次執行只續做、不重新申請。"""
        self.compute.launch_instance.return_value = resp(make_instance())
        # 取 VNIC 一直失敗，直到超過 PUBLIC_IP_TIMEOUT_SECONDS
        self.network.get_vnic.side_effect = service_error(500, "InternalServerError", "boom")
        with self.assertRaises(claimer.PostLaunchError) as ctx:
            self.claimer.run()
        self.assertEqual(ctx.exception.instance_id, "ocid1.instance.oc1..new")
        self.assertEqual(self.compute.launch_instance.call_count, 1)
        self.assertEqual(json.loads(self.state_path.read_text())["status"], "launched")
        self.assertFalse(self.result_path.exists())

        # 第二次執行（例如手動重啟）：進續做模式
        self.network.get_vnic.side_effect = None
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        self.assertEqual(self.compute.launch_instance.call_count, 1)  # 沒有再申請
        self.assertTrue(self.result_path.exists())

        # 第三次執行：state=completed，直接退出
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_instance_terminated_while_waiting_is_post_launch_error(self):
        self.compute.launch_instance.return_value = resp(make_instance())
        self.compute.get_instance.return_value = resp(make_instance(lifecycle_state="TERMINATED"))
        with self.assertRaises(claimer.PostLaunchError):
            self.claimer.run()
        self.assertEqual(self.compute.launch_instance.call_count, 1)

    def test_network_error_retries_with_same_token(self):
        """網路錯誤：不 crash，退避後以同一個 token 重送（冪等）。"""
        self.compute.launch_instance.side_effect = [
            oci.exceptions.RequestException("connection reset"),
            oci.exceptions.ConnectTimeout("timeout"),
            resp(make_instance()),
        ]
        rc = self.claimer.run()
        self.assertEqual(rc, claimer.EXIT_OK)
        tokens = [c.kwargs["opc_retry_token"] for c in self.compute.launch_instance.call_args_list]
        self.assertEqual(len(tokens), 3)
        self.assertEqual(len(set(tokens)), 1)
        # 指數退避：第二次的等待比第一次長
        self.assertGreater(self.sleeper.calls[1], self.sleeper.calls[0])

    def test_network_error_during_startup_is_retried(self):
        self.identity.list_availability_domains.side_effect = [
            ConnectionError("dns"),
            resp([SimpleNamespace(name="xyz:AD-1")]),
        ]
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_transient_5xx_retries_then_moves_on(self):
        self.settings.transient_max_attempts_per_ad = 2
        boom = service_error(502, "BadGateway", "")
        self.compute.launch_instance.side_effect = [boom, boom, resp(make_instance())]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        tokens = [c.kwargs["opc_retry_token"] for c in self.compute.launch_instance.call_args_list]
        self.assertEqual(tokens[0], tokens[1])      # 同 AD 內同 token
        self.assertNotEqual(tokens[1], tokens[2])   # 換 AD 換 token
        ads = [c.args[0].availability_domain for c in self.compute.launch_instance.call_args_list]
        self.assertEqual(ads, ["xyz:AP-TOKYO-1-AD-1", "xyz:AP-TOKYO-1-AD-1", "xyz:AP-TOKYO-1-AD-2"])

    def test_auth_error_is_config_error(self):
        self.compute.launch_instance.side_effect = [service_error(404, "NotAuthorizedOrNotFound", "")]
        with self.assertRaises(claimer.ConfigError):
            self.claimer.run()

    def test_private_subnet_with_public_ip_is_config_error(self):
        self.network.get_subnet.return_value = resp(SimpleNamespace(
            display_name="priv", cidr_block="10.0.1.0/24", prohibit_public_ip_on_vnic=True))
        with self.assertRaises(claimer.ConfigError):
            self.claimer.run()

    def test_non_home_region_is_refused(self):
        """非 home region 建立的實例不是 Always Free，預設拒絕。"""
        self.clients.region = "ap-osaka-1"
        with self.assertRaises(claimer.ConfigError):
            self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_home_region_matched_by_region_key(self):
        self.clients.region = "nrt"
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_non_home_region_allowed_when_opted_out(self):
        self.clients.region = "ap-osaka-1"
        self.settings.require_home_region = False
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_dry_run_network_failure_times_out(self):
        self.identity.list_region_subscriptions.side_effect = oci.exceptions.RequestException("down")
        with self.assertRaises(TimeoutError):
            self.claimer.dry_run()
        self.assertLessEqual(self.sleeper.now, claimer.DRY_RUN_TIMEOUT_SECONDS)
        for c in self.identity.list_region_subscriptions.call_args_list:
            self.assertIsInstance(c.kwargs["retry_strategy"], oci.retry.NoneRetryStrategy)

    def test_dry_run_does_not_launch(self):
        self.assertEqual(self.claimer.dry_run(), claimer.EXIT_OK)
        self.compute.launch_instance.assert_not_called()


class TestMainExitCodes(unittest.TestCase):
    def _run_main_with(self, exc, started_at=""):
        fake = mock.MagicMock(started_at=started_at)
        fake.run.side_effect = exc
        self.sent = []
        with mock.patch.object(claimer, "load_settings", return_value=mock.MagicMock(
                tg_bot_token="tok", tg_chat_id="1", log_level="INFO")), \
             mock.patch.object(claimer.TelegramNotifier, "send", lambda _self, t: self.sent.append(t)), \
             mock.patch.object(claimer, "build_clients", return_value=mock.MagicMock()), \
             mock.patch.object(claimer, "Claimer", return_value=fake), \
             mock.patch.object(claimer, "check_file_permissions"), \
             mock.patch.object(claimer, "setup_logging"), \
             mock.patch.object(claimer.signal, "signal"), \
             mock.patch.object(claimer, "log"):
            return claimer.main(["-c", "nonexistent.env"])

    def test_codes(self):
        cases = [
            (claimer.ConfigError("x"), claimer.EXIT_CONFIG),
            (claimer.QuotaExhausted("x"), claimer.EXIT_QUOTA),
            (claimer.PostLaunchError("id", "x"), claimer.EXIT_POST_LAUNCH),
            (claimer.StateFileError("x"), claimer.EXIT_POST_LAUNCH),
            (claimer.UncertainLaunchError("x"), claimer.EXIT_POST_LAUNCH),
            (TimeoutError("x"), claimer.EXIT_UNEXPECTED),
            (claimer.Interrupted(), claimer.EXIT_INTERRUPTED),
            (KeyboardInterrupt(), claimer.EXIT_INTERRUPTED),
            (RuntimeError("x"), claimer.EXIT_UNEXPECTED),
        ]
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self._run_main_with(exc), code)

    def test_stops_that_need_attention_notify(self):
        for exc in (claimer.QuotaExhausted("x"), claimer.PostLaunchError("id", "x"),
                    claimer.StateFileError("x"), claimer.UncertainLaunchError("x")):
            with self.subTest(exc=type(exc).__name__):
                self._run_main_with(exc)
                self.assertEqual(len(self.sent), 1)

    def test_config_error_notifies_only_after_claiming_started(self):
        self._run_main_with(claimer.ConfigError("401 NotAuthenticated"))
        self.assertEqual(self.sent, [])  # 啟動時的設定錯誤：使用者就在現場
        self._run_main_with(claimer.ConfigError("401 NotAuthenticated"), started_at="2026-11-04 10:00 +0800")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("401 NotAuthenticated", self.sent[0])


class TestImagePicking(unittest.TestCase):
    def setUp(self):
        self.s = claimer.Settings(subnet_id="s", ssh_public_key_file="k", **SPEC)

    def test_prefers_lts_and_excludes_minimal(self):
        images = [
            make_image("Canonical-Ubuntu-25.10-aarch64", "25.10", created_day=20),
            make_image("Canonical-Ubuntu-24.04-Minimal-aarch64", "24.04", created_day=15),
            make_image("Canonical-Ubuntu-24.04-aarch64", "24.04", created_day=10),
            make_image("Canonical-Ubuntu-22.04-aarch64", "22.04", created_day=12),
        ]
        self.assertEqual(claimer.pick_image(images, self.s).display_name, "Canonical-Ubuntu-24.04-aarch64")

    def test_settings_can_disable_filters(self):
        self.s.image_prefer_lts = False
        self.s.image_exclude_minimal = False
        images = [
            make_image("Canonical-Ubuntu-25.10-aarch64", "25.10"),
            make_image("Canonical-Ubuntu-24.04-Minimal-aarch64", "24.04"),
        ]
        self.assertEqual(claimer.pick_image(images, self.s).operating_system_version, "25.10")

    def test_lts_detection(self):
        self.assertTrue(claimer.is_ubuntu_lts("24.04"))
        self.assertFalse(claimer.is_ubuntu_lts("23.04"))
        self.assertFalse(claimer.is_ubuntu_lts("24.10"))


class TestConfigAndFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_env_overrides_file(self):
        cfg = self.dir / "config.env"
        cfg.write_text("# 註解\nSUBNET_ID=from-file\nSSH_PUBLIC_KEY_FILE='~/.ssh/k.pub'\n" + SPEC_FILE +
                       "ASSIGN_PUBLIC_IP=false\nAVAILABILITY_DOMAINS=AD-1, AD-3\n", encoding="utf-8")
        s = claimer.load_settings(cfg, environ={"SUBNET_ID": "from-env", "OCPUS": "1"})
        self.assertEqual(s.subnet_id, "from-env")
        self.assertEqual(s.ssh_public_key_file, str(Path("~/.ssh/k.pub").expanduser()))
        self.assertEqual(s.ocpus, 1.0)
        self.assertFalse(s.assign_public_ip)
        self.assertEqual(s.availability_domains, ["AD-1", "AD-3"])

    def test_missing_required(self):
        with self.assertRaises(claimer.ConfigError):
            claimer.load_settings(None, environ={"SUBNET_ID": "x"})
        # 規格與額度上限沒有程式內預設值
        with self.assertRaises(claimer.ConfigError) as ctx:
            claimer.load_settings(None, environ={"SUBNET_ID": "x", "SSH_PUBLIC_KEY_FILE": "k"})
        self.assertIn("OCPUS", str(ctx.exception))

    def test_bad_bool(self):
        with self.assertRaises(claimer.ConfigError):
            claimer.load_settings(None, environ={"SUBNET_ID": "x", "SSH_PUBLIC_KEY_FILE": "k", **SPEC_ENV,
                                                 "ASSIGN_PUBLIC_IP": "maybe"})

    def test_permission_warning(self):
        f = self.dir / "config.env"
        f.write_text("X=1")
        f.chmod(0o644)
        with self.assertLogs("oci-a1-claimer", level="WARNING"):
            self.assertFalse(claimer.check_file_permissions(f, "設定檔"))
        f.chmod(0o600)
        self.assertTrue(claimer.check_file_permissions(f, "設定檔"))

    def test_atomic_write_replaces(self):
        p = self.dir / "sub" / "r.json"
        claimer.write_json_atomic(p, {"a": 1})
        claimer.write_json_atomic(p, {"a": 2})
        self.assertEqual(json.loads(p.read_text()), {"a": 2})
        self.assertEqual([x.name for x in p.parent.iterdir()], ["r.json"])  # 沒有殘留暫存檔


class TestSecrets(unittest.TestCase):
    def test_redacting_formatter_covers_message_and_traceback(self):
        token = "123456:SECRET-TOKEN"
        fmt = claimer.RedactingFormatter("%(message)s", [token])
        try:
            raise RuntimeError(f"https://x/bot{token}/send")
        except RuntimeError:
            exc_info = sys.exc_info()
        rec = claimer.logging.LogRecord("x", 40, "f", 1, "url=%s", (f"https://x/bot{token}/send",), exc_info)
        out = fmt.format(rec)
        self.assertNotIn(token, out)
        self.assertIn("***", out)

    def test_telegram_http_error_does_not_log_token(self):
        token = "123456:SECRET-TOKEN"
        n = claimer.TelegramNotifier(token, "42")
        err = claimer.urllib.error.HTTPError(f"https://api.telegram.org/bot{token}/sendMessage",
                                             401, "Unauthorized", {}, None)
        with mock.patch.object(claimer.urllib.request, "urlopen", side_effect=err), \
             self.assertLogs("oci-a1-claimer", level="WARNING") as logs:
            self.assertFalse(n.send("hi"))
        self.assertNotIn(token, "\n".join(logs.output))

    def test_telegram_error_shows_api_description(self):
        token = "123456:SECRET-TOKEN"
        n = claimer.TelegramNotifier(token, "42")
        body = io.BytesIO(b'{"ok":false,"description":"Forbidden: bot was blocked by the user"}')
        err = claimer.urllib.error.HTTPError(f"https://api.telegram.org/bot{token}/sendMessage",
                                             403, "Forbidden", {}, body)
        with mock.patch.object(claimer.urllib.request, "urlopen", side_effect=err), \
             self.assertLogs("oci-a1-claimer", level="WARNING") as logs:
            n.send("hi")
        out = "\n".join(logs.output)
        self.assertIn("bot was blocked", out)
        self.assertNotIn(token, out)


class TestHeartbeat(ClaimerTestBase):
    def _messages(self):
        return [c.args[0] for c in self.notifier.send.call_args_list]

    def test_heartbeat_reports_progress(self):
        """每隔 HEARTBEAT_HOURS 回報一次累計嘗試次數，用來確認程式仍在執行。"""
        self.settings.heartbeat_hours = 0.1  # 6 分鐘；每輪約 61~71 秒
        self.compute.launch_instance.side_effect = [capacity_error()] * 20 + [resp(make_instance())]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        beats = [m for m in self._messages() if "仍在執行" in m]
        # 6 分鐘間隔、每輪 61~71 秒、共 10 輪：恰好回報一次，且帶當下累計次數
        self.assertEqual(len(beats), 1)
        self.assertRegex(beats[0], r"已嘗試 1[0-9] 次")
        self.assertIn("容量不足", beats[0])
        self.assertEqual(self.claimer.stats["attempts"], 21)
        self.assertEqual(self.claimer.stats["capacity"], 20)

    def test_heartbeat_disabled(self):
        self.settings.heartbeat_hours = 0
        self.compute.launch_instance.side_effect = [capacity_error()] * 20 + [resp(make_instance())]
        self.claimer.run()
        msgs = self._messages()
        self.assertFalse([m for m in msgs if "仍在執行" in m])
        self.assertIn("未啟用定期回報", msgs[0])

    def test_no_start_notice_in_resume_mode(self):
        claimer.write_json_atomic(self.state_path, {"status": "launched", "instance_id": "ocid1.instance.oc1..new"})
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        self.assertFalse([m for m in self._messages() if "開始申請" in m])


class TestUncertainLaunch(ClaimerTestBase):
    """網路錯誤造成「結果不明」後的冪等保護。"""

    def _calls(self):
        return [(c.args[0].availability_domain, c.kwargs["opc_retry_token"])
                for c in self.compute.launch_instance.call_args_list]

    def test_uncertain_token_survives_429_and_5xx(self):
        """結果不明後遇到 429 / 多次 5xx：不換 AD、不換 token，直到拿到明確結果。"""
        boom = service_error(504, "GatewayTimeout", "")
        self.compute.launch_instance.side_effect = [
            oci.exceptions.RequestException("read timeout"),
            service_error(429, "TooManyRequests"),
            boom, boom, boom, boom,               # 超過 TRANSIENT_MAX_ATTEMPTS_PER_AD
            capacity_error(),                     # 明確結果：先前也沒建立
            resp(make_instance()),                # 下一個 AD、新 token
        ]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        calls = self._calls()
        self.assertEqual({c for c in calls[:7]}, {("xyz:AP-TOKYO-1-AD-1", "token0")})
        self.assertEqual(calls[7], ("xyz:AP-TOKYO-1-AD-2", "token1"))
        self.assertFalse(self.claimer.pending_path.exists())

    def test_pending_token_resumed_after_restart(self):
        """結果不明期間被停止：pending 落盤，下次啟動先以原 AD + 原 token 重送。"""
        def stop_on_sleep(_seconds):
            raise claimer.Interrupted()
        c1 = claimer.Claimer(self.settings, self.clients, self.notifier, sleep_fn=stop_on_sleep,
                             token_factory=lambda: next(self.tokens), wall_clock=self.sleeper.wall)
        self.compute.launch_instance.side_effect = [oci.exceptions.ConnectTimeout("t")]
        with self.assertRaises(claimer.Interrupted):
            c1.run()
        pending = json.loads(c1.pending_path.read_text())
        self.assertEqual(pending["token"], "token0")

        # 重啟：原請求其實已成功，重送回傳同一台
        self.compute.launch_instance.side_effect = [resp(make_instance())]
        self.compute.list_instances.return_value = resp([make_instance(lifecycle_state="PROVISIONING")])
        c2 = self.new_claimer()
        self.assertEqual(c2.run(), claimer.EXIT_OK)
        self.assertEqual(self._calls()[-1], ("xyz:AP-TOKYO-1-AD-1", "token0"))
        self.assertFalse(c2.pending_path.exists())
        self.assertTrue(self.result_path.exists())

    def test_uncertain_409_conflict_keeps_retrying(self):
        self.compute.launch_instance.side_effect = [
            oci.exceptions.RequestException("reset"),
            service_error(409, "Conflict", "request with this retry token is in progress"),
            resp(make_instance()),
        ]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        self.assertEqual({t for _, t in self._calls()}, {"token0"})

    def test_expired_pending_requires_manual_check(self):
        claimer.write_json_atomic(self.claimer.pending_path, {
            "availability_domain": "AD-1", "token": "old", "created_at": 0, "spec": SPEC_PENDING})
        with self.assertRaisesRegex(claimer.StateFileError, "已超過"):
            self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_invalidated_token_after_uncertain_stops(self):
        self.compute.launch_instance.side_effect = [
            oci.exceptions.RequestException("reset"),
            service_error(409, "InvalidatedRetryToken", ""),
        ]
        with self.assertRaises(claimer.UncertainLaunchError):
            self.claimer.run()
        self.assertTrue(self.claimer.pending_path.exists())

    def test_invalidated_token_without_uncertainty_rotates(self):
        self.compute.launch_instance.side_effect = [
            service_error(409, "InvalidatedRetryToken", ""),
            resp(make_instance()),
        ]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        self.assertEqual([t for _, t in self._calls()], ["token0", "token1"])


class TestDuplicateLaunchGuards(ClaimerTestBase):
    """任何中斷都不能讓同一次申請換新 token 重送。"""

    def _write_pending(self, **overrides):
        data = {"availability_domain": "xyz:AP-TOKYO-1-AD-1", "token": "old",
                "created_at": self.sleeper.wall(), "spec": SPEC_PENDING, **overrides}
        claimer.write_json_atomic(self.claimer.pending_path, data)

    def test_pending_written_before_launch_and_kept_on_unexpected_error(self):
        """回應解析失敗等非網路例外：送出前已寫入的 pending 保留，重啟以同一 token 重送。"""
        seen = []

        def launch(details, **kw):
            seen.append(json.loads(self.claimer.pending_path.read_text())["token"])
            raise ValueError("cannot deserialize response")

        self.compute.launch_instance.side_effect = launch
        with self.assertRaises(ValueError):
            self.claimer.run()
        self.assertEqual(seen, ["token0"])
        self.assertEqual(json.loads(self.claimer.pending_path.read_text())["token"], "token0")

        self.compute.launch_instance.side_effect = [resp(make_instance())]
        self.assertEqual(self.new_claimer().run(), claimer.EXIT_OK)
        self.assertEqual(self.compute.launch_instance.call_args.kwargs["opc_retry_token"], "token0")
        self.assertFalse(self.claimer.pending_path.exists())

    def test_definite_result_clears_pending(self):
        self.compute.launch_instance.side_effect = [capacity_error(), resp(make_instance())]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        self.assertFalse(self.claimer.pending_path.exists())

    def test_resume_resends_stored_spec_not_current_image(self):
        """重啟期間出了新映像檔：重送仍用 pending 記錄的映像檔與規格。"""
        self._write_pending()
        self.compute.list_images.return_value = resp([make_image("Canonical-Ubuntu-24.04-new", "24.04", 9)])
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        details = self.compute.launch_instance.call_args.args[0]
        self.assertEqual(details.source_details.image_id, "ocid1.image..old")
        self.assertEqual(self.compute.launch_instance.call_args.kwargs["opc_retry_token"], "old")

    def test_uncertain_retry_stops_when_token_expires(self):
        """結果不明期間持續斷線：超過 token 有效期就停止並保留 pending，不再重送。"""
        # 有限次數：過期檢查失效時以 StopIteration 失敗，而不是無限重送卡住 CI
        self.compute.launch_instance.side_effect = [oci.exceptions.RequestException("down")] * 2000
        with self.assertRaises(claimer.UncertainLaunchError):
            self.claimer.run()
        self.assertGreater(self.sleeper.now, claimer.PENDING_TOKEN_TTL_SECONDS)
        self.assertLess(self.sleeper.now, claimer.PENDING_TOKEN_TTL_SECONDS + 2 * self.settings.transient_retry_max_seconds)
        self.assertTrue(self.claimer.pending_path.exists())

    def test_bad_pending_needs_manual_check(self):
        cases = {
            "not a dict": [1, 2],
            "missing spec": {"availability_domain": "AD-1", "token": "t", "created_at": self.sleeper.wall()},
            "bad created_at": {"availability_domain": "AD-1", "token": "t", "created_at": "x", "spec": SPEC_PENDING},
        }
        for name, data in cases.items():
            with self.subTest(name):
                claimer.write_json_atomic(self.claimer.pending_path, data)
                with self.assertRaises(claimer.StateFileError):
                    self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_save_state_failure_keeps_pending(self):
        """建立成功但寫 state 失敗：以結束碼 4 停止，pending 保留讓重啟能以原 token 找回同一台。"""
        self.compute.launch_instance.return_value = resp(make_instance())
        with mock.patch.object(self.claimer, "save_state", side_effect=OSError("disk full")), \
             self.assertRaises(claimer.PostLaunchError):
            self.claimer.run()
        self.assertEqual(json.loads(self.claimer.pending_path.read_text())["token"], "token0")

    def test_second_process_on_same_state_dir_is_refused(self):
        with self.new_claimer()._state_dir_lock():
            with self.assertRaisesRegex(claimer.ConfigError, "另一個"):
                self.claimer.run()
        self.compute.launch_instance.assert_not_called()
        self.compute.launch_instance.return_value = resp(make_instance())
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)  # 鎖釋放後可正常執行

    def test_dry_run_with_pending_skips_usage_check(self):
        """先前結果不明的那台可能已建立；dry-run 與正式執行一樣略過用量檢查。"""
        self._write_pending()
        self.compute.list_instances.return_value = resp([make_instance(lifecycle_state="RUNNING")])
        self.assertEqual(self.claimer.dry_run(), claimer.EXIT_OK)


class TestPostLaunchResilience(ClaimerTestBase):
    def test_network_outage_recovers_before_deadline(self):
        """建立後網路中斷多次，只要在時限內恢復就完成，不會以結束碼 4 停止。"""
        self.settings.running_timeout_seconds = 3600
        self.compute.launch_instance.return_value = resp(make_instance())
        self.compute.get_instance.side_effect = (
            [oci.exceptions.RequestException("down")] * 8 + [resp(make_instance(lifecycle_state="RUNNING"))])
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)
        # 建立後的查詢關閉 SDK 內建重試
        for c in self.compute.get_instance.call_args_list:
            self.assertIsInstance(c.kwargs["retry_strategy"], oci.retry.NoneRetryStrategy)

    def test_eventual_consistency_404_tolerated(self):
        self.compute.launch_instance.return_value = resp(make_instance())
        self.compute.get_instance.side_effect = [
            service_error(404, "NotAuthorizedOrNotFound", ""),
            resp(make_instance(lifecycle_state="RUNNING")),
        ]
        self.assertEqual(self.claimer.run(), claimer.EXIT_OK)

    def test_corrupt_state_is_not_treated_as_empty(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(claimer.StateFileError):
            self.claimer.run()
        self.compute.launch_instance.assert_not_called()

    def test_dry_run_with_state_skips_usage_check(self):
        claimer.write_json_atomic(self.state_path, {"status": "completed", "instance_id": "x"})
        self.compute.list_instances.return_value = resp([make_instance(lifecycle_state="RUNNING")])
        self.assertEqual(self.claimer.dry_run(), claimer.EXIT_OK)


class TestBuildClients(unittest.TestCase):
    def test_bad_private_key_is_config_error(self):
        s = claimer.Settings(subnet_id="s", ssh_public_key_file="k", oci_config_file="/nonexistent/cfg", **SPEC)
        fake_cfg = {"tenancy": "t", "region": "r", "key_file": "/nonexistent/key.pem"}
        with mock.patch.object(claimer.oci.config, "from_file", return_value=fake_cfg), \
             mock.patch.object(claimer.oci.config, "validate_config"), \
             mock.patch.object(claimer.oci.core, "ComputeClient",
                               side_effect=oci.exceptions.InvalidPrivateKey("bad key")):
            with self.assertRaises(claimer.ConfigError):
                claimer.build_clients(s)

    def test_region_setting_overrides_profile(self):
        s = claimer.Settings(subnet_id="s", ssh_public_key_file="k", oci_config_file="/nonexistent/cfg",
                             region="ap-osaka-1", **SPEC)
        fake_cfg = {"tenancy": "t", "region": "ap-tokyo-1"}
        with mock.patch.object(claimer.oci.config, "from_file", return_value=fake_cfg), \
             mock.patch.object(claimer.oci.config, "validate_config"), \
             mock.patch.object(claimer.oci.core, "ComputeClient"), \
             mock.patch.object(claimer.oci.core, "VirtualNetworkClient"), \
             mock.patch.object(claimer.oci.identity, "IdentityClient"):
            clients = claimer.build_clients(s)
        self.assertEqual(clients.region, "ap-osaka-1")

    def test_missing_config_file_is_config_error(self):
        s = claimer.Settings(subnet_id="s", ssh_public_key_file="k", oci_config_file="/nonexistent/cfg", **SPEC)
        with self.assertRaises(claimer.ConfigError):
            claimer.build_clients(s)


class TestMainNotifications(unittest.TestCase):
    def test_dry_run_never_notifies(self):
        fake = mock.MagicMock()
        fake.dry_run.side_effect = claimer.QuotaExhausted("full")
        sent = []
        settings = mock.MagicMock(tg_bot_token="tok", tg_chat_id="1", log_level="INFO")
        with mock.patch.object(claimer, "load_settings", return_value=settings), \
             mock.patch.object(claimer, "build_clients", return_value=mock.MagicMock()), \
             mock.patch.object(claimer, "Claimer", return_value=fake), \
             mock.patch.object(claimer, "check_file_permissions"), \
             mock.patch.object(claimer, "setup_logging"), \
             mock.patch.object(claimer.signal, "signal"), \
             mock.patch.object(claimer.TelegramNotifier, "send", lambda self, t: self.enabled and sent.append(t)), \
             self.assertLogs("oci-a1-claimer", level="ERROR"):
            rc = claimer.main(["--dry-run", "-c", "x.env"])
        self.assertEqual(rc, claimer.EXIT_QUOTA)
        self.assertEqual(sent, [])


class TestConfigValidation(unittest.TestCase):
    BASE = {"SUBNET_ID": "s", "SSH_PUBLIC_KEY_FILE": "k.pub", **SPEC_ENV}

    def test_rejects_nan_inf_negative(self):
        for key, val in [("OCPUS", "nan"), ("INTERVAL_SECONDS", "inf"), ("PER_AD_DELAY_SECONDS", "-1"),
                         ("RATE_LIMIT_SLEEP_SECONDS", "-5"), ("MEMORY_GB", "0")]:
            with self.subTest(key=key, val=val), self.assertRaises(claimer.ConfigError):
                claimer.load_settings(None, environ={**self.BASE, key: val})

    def test_empty_env_overrides_file_value(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.env"
            cfg.write_text("SUBNET_ID=s\nSSH_PUBLIC_KEY_FILE=k.pub\nTG_BOT_TOKEN=abc\n" + SPEC_FILE, encoding="utf-8")
            self.assertEqual(claimer.load_settings(cfg, environ={}).tg_bot_token, "abc")
            self.assertEqual(claimer.load_settings(cfg, environ={"TG_BOT_TOKEN": ""}).tg_bot_token, "")

    def test_relative_paths_resolve_to_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.env"
            cfg.write_text("SUBNET_ID=s\nSSH_PUBLIC_KEY_FILE=k.pub\nSTATE_DIR=./state\n" + SPEC_FILE, encoding="utf-8")
            s = claimer.load_settings(cfg, environ={})
            base = Path(d).resolve()
            self.assertEqual(Path(s.state_dir), base / "state")
            self.assertEqual(Path(s.ssh_public_key_file), base / "k.pub")

    def test_group_read_allowed_other_read_warned(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "k"
            f.write_text("x")
            f.chmod(0o640)
            self.assertTrue(claimer.check_file_permissions(f, "k"))
            f.chmod(0o604)
            with self.assertLogs("oci-a1-claimer", level="WARNING"):
                self.assertFalse(claimer.check_file_permissions(f, "k"))


if __name__ == "__main__":
    unittest.main()
