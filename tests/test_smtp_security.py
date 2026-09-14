"""SMTP regressions: real report paths and TLS handshakes, without network I/O.

Run from the repository root: python3 -m unittest discover -s tests -v
Requires the application's existing requests/openpyxl dependencies and openssl
on PATH to generate temporary, test-only certificates; no keys are committed.
"""

from contextlib import ExitStack
import importlib
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "CLICKUP_API_TOKEN": "TEST_ONLY_clickup_token",
    "SMTP_PASSWORD": "TEST_ONLY_smtp_password",
    "SMTP_HOST": "smtp.fixture.test",
    "SMTP_PORT": "2465",
    "SMTP_FROM": "sender@example.test",
    "MAIL_TO": "weekly@example.test",
    "MAIL_TO_DAILY": "daily@example.test",
}
PATHS = ("weekly", "daily", "first_run")


def generate_tls_fixtures(directory):
    """Generate a short-lived CA and server pair inside a private temp folder."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("SMTP TLS tests require the openssl CLI on PATH; install it and rerun.")
    (directory / "tls.cnf").write_text("""\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = smtp.fixture.test
[ca]
basicConstraints = critical,CA:true,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[server]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:smtp.fixture.test
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
""", encoding="utf-8")

    def run(*args):
        try:
            result = subprocess.run(
                [openssl, *args], cwd=str(directory), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Test-only openssl certificate generation exceeded 30 seconds.") from None
        except OSError as error:
            raise RuntimeError(f"Cannot run test-only openssl certificate generation: {error}") from None
        if result.returncode:
            # These commands write all keys to files, so stderr holds diagnostics
            # only. Bound output and never print generated files or stdout.
            raise RuntimeError(
                f"Test-only openssl {args[0]} failed (exit {result.returncode}): "
                f"{result.stderr.strip()[-1000:]}"
            )

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "2",
        "-config", "tls.cnf", "-extensions", "ca", "-subj", "/CN=TEST ONLY SMTP CA",
        "-keyout", "TEST_ONLY_smtp_ca_key.pem", "-out", "TEST_ONLY_smtp_ca.pem")
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-config", "tls.cnf",
        "-keyout", "TEST_ONLY_smtp_server_key.pem", "-out", "server.csr")
    run("x509", "-req", "-in", "server.csr", "-CA", "TEST_ONLY_smtp_ca.pem",
        "-CAkey", "TEST_ONLY_smtp_ca_key.pem", "-set_serial", "2", "-days", "2", "-sha256",
        "-extfile", "tls.cnf", "-extensions", "server", "-out", "TEST_ONLY_smtp_server.pem")


def memory_tls_handshake(client_context, hostname, server_context):
    """Exchange actual TLS records via MemoryBIO; never open a socket."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(100):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        if client_out.pending:
            server_in.write(client_out.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        if server_out.pending:
            client_in.write(server_out.read())
        if client_done and server_done:
            return
    raise AssertionError("In-memory TLS handshake did not finish")


class SmtpSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        stack.enter_context(mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("Network connections are forbidden in SMTP tests"),
        ))
        cls.fixtures = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="clickup-smtp-tests-")))
        generate_tls_fixtures(cls.fixtures)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(
            str(cls.fixtures / "TEST_ONLY_smtp_server.pem"),
            str(cls.fixtures / "TEST_ONLY_smtp_server_key.pem"),
        )
        # Fresh imports must use dummy credentials even if another test imported
        # these modules first. Restore the process's original modules afterward.
        previous = {name: sys.modules.pop(name, None)
                    for name in ("clickup_due_report", "clickup_bot")}
        try:
            with mock.patch.dict(os.environ, ENV, clear=True):
                with mock.patch.object(sys, "path", [str(ROOT)] + sys.path):
                    cls.weekly = importlib.import_module("clickup_bot")
                    cls.daily = importlib.import_module("clickup_due_report")
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous_cwd = os.getcwd()
        os.chdir(temp.name)
        self.addCleanup(os.chdir, previous_cwd)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, ENV, clear=True))
        self.weekly_log = self.stack.enter_context(mock.patch.object(self.weekly, "log"))
        self.daily_log = self.stack.enter_context(mock.patch.object(self.daily, "log"))

    def invoke_report(self, path):
        me = {"id": 1, "username": "Test User", "email": "user@example.test"}
        if path == "weekly":
            self.weekly.excel_ve_mail([
                {"ad": "Test Account", "sirket": "Test Company", "bakiye": 12.5},
            ])
        elif path == "daily":
            Path("daily-report.xlsx").write_bytes(b"TEST ONLY attachment")
            self.daily.send_mail("daily-report.xlsx", me, {
                "degisti_yorumlu": 1, "degisti_yorumsuz": 0, "kaldirildi": 0,
            }, False)
        else:
            snapshot = {"test-task": {"due_date": "1234"}}
            with ExitStack() as stack:
                for name, value in {
                    "get_session": mock.sentinel.session,
                    "get_me": me,
                    "build_current_snapshot": (snapshot, []),
                    "load_previous_snapshot": None,
                }.items():
                    stack.enter_context(mock.patch.object(self.daily, name, return_value=value))
                save = stack.enter_context(mock.patch.object(self.daily, "save_snapshot"))
                self.daily.main()
                save.assert_called_once_with(snapshot)

    def exercise(self, path, handshake=None, rejected=False):
        transport = mock.MagicMock(name="TEST_ONLY_SMTP_transport")
        transport.__enter__.return_value = transport
        verification_errors = []

        def connect(host, port, *, timeout, context):
            self.assertEqual((host, port), (ENV["SMTP_HOST"], int(ENV["SMTP_PORT"])))
            self.assertEqual(timeout, 20 if path == "weekly" else 30)
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            if handshake:
                try:
                    handshake(context, host)
                except ssl.SSLCertVerificationError as error:
                    verification_errors.append(error)
                    raise
            return transport

        with mock.patch.object(self.weekly.smtplib, "SMTP_SSL", side_effect=connect) as smtp:
            if rejected and path == "daily":
                with self.assertRaises(ssl.SSLCertVerificationError):
                    self.invoke_report(path)
            else:
                self.invoke_report(path)
        smtp.assert_called_once()  # A failed handshake must not trigger an insecure retry.
        if path == "weekly":
            self.assertEqual(list(Path.cwd().glob("Cari_Rapor_*.xlsx")), [])
        if rejected:
            self.assertEqual(len(verification_errors), 1)
            transport.__enter__.assert_not_called()
            transport.login.assert_not_called()
            transport.send_message.assert_not_called()
            if path != "daily":
                log = self.weekly_log if path == "weekly" else self.daily_log
                self.assertTrue(any("Mail Hatası" in str(c) for c in log.call_args_list))
            return
        transport.login.assert_called_once_with(ENV["SMTP_FROM"], ENV["SMTP_PASSWORD"])
        transport.send_message.assert_called_once()
        message = transport.send_message.call_args.args[0]
        self.assertEqual(message["From"], ENV["SMTP_FROM"])
        self.assertEqual(message["To"], ENV["MAIL_TO" if path == "weekly" else "MAIL_TO_DAILY"])
        attachments = [p for p in message.walk() if p.get_content_disposition() == "attachment"]
        self.assertEqual(len(attachments), 0 if path == "first_run" else 1)
        if path == "first_run":
            body = message.get_payload(0).get_payload(decode=True).decode("utf-8")
            self.assertIn("İlk Snapshot Oluşturuldu", body)
        else:
            self.assertTrue(attachments[0].get_filename().endswith(".xlsx"))
            self.assertTrue(attachments[0].get_payload(decode=True))

    def test_all_mail_paths_keep_settings_and_deliver(self):
        for path in PATHS:
            with self.subTest(path=path):
                self.exercise(path)

    def test_certificate_failure_prevents_login_and_send(self):
        def reject(context, hostname):
            raise ssl.SSLCertVerificationError("TEST ONLY certificate rejected")

        for path in PATHS:
            with self.subTest(path=path):
                self.exercise(path, reject, rejected=True)

    def test_real_tls_rejects_untrusted_certificate(self):
        def untrusted(context, hostname):
            memory_tls_handshake(context, hostname, self.server_context)

        for path in PATHS:
            with self.subTest(path=path):
                self.exercise(path, untrusted, rejected=True)

    def test_real_tls_rejects_trusted_certificate_for_wrong_hostname(self):
        def wrong_hostname(context, hostname):
            context.load_verify_locations(cafile=str(self.fixtures / "TEST_ONLY_smtp_ca.pem"))
            memory_tls_handshake(context, "wrong-host.fixture.test", self.server_context)

        for path in PATHS:
            with self.subTest(path=path):
                self.exercise(path, wrong_hostname, rejected=True)

    def test_real_tls_accepts_trusted_certificate_for_matching_hostname(self):
        def trusted(context, hostname):
            context.load_verify_locations(cafile=str(self.fixtures / "TEST_ONLY_smtp_ca.pem"))
            memory_tls_handshake(context, hostname, self.server_context)

        for path in PATHS:
            with self.subTest(path=path):
                self.exercise(path, trusted)


if __name__ == "__main__":
    unittest.main()
